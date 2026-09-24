"""AI 标识（项目级开关，默认关）：ASS 事件构造、compose_artifacts 开关组合、
draft_concat 首段重编码与其余 -c copy 分段混拼的真实兼容性、关闭时零额外开销。

lavfi 小尺寸短时长源，一次只跑一个 ffmpeg；标识开/关断言 ASS 事件计数，不做
像素比对（派单 D 项）。需要真实 CJK 字体（compose_artifacts 内部经
``app.final_edit._font_path`` 取字体家族名），本机没有则整组 skip。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.media_exec import concat_draft
from app.media_exec.concat import _draft_concat_pieces, _probe_concat_media
from app.media_pipeline.delivery_encode import INTERMEDIATE_VIDEO_ARGS
from app.subtitles import episode as subtitle_episode
from app.subtitles.align import LineAlignment, ShotAlignment
from app.subtitles.ass import AI_LABEL_TEXT, SubtitleStyle, ai_label_event, ffmpeg_ass_filter, font_family_from_file, render_ass
from app.subtitles.cues import Cue

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")


def _skip_without_font() -> Path:
    from app.final_edit import _font_path
    try:
        return _font_path()
    except RuntimeError:
        pytest.skip("test host has no configured CJK font")


# ---------------------------------------------------------------------------
# 1. ass.ai_label_event 本身的契约
# ---------------------------------------------------------------------------


def test_ai_label_event_is_single_dialogue_with_text():
    event = ai_label_event((1080, 1920))
    assert event.count("Dialogue:") == 1
    assert AI_LABEL_TEXT in event
    assert event.startswith("Dialogue: 1,0:00:00.00,0:00:03.00,Default,,0,0,0,,")


def test_ai_label_event_scale_identical_across_orientations():
    """min(width, height) 两种画幅都固定是 1080，字号/描边/外边距应完全相同。"""
    portrait = ai_label_event((1080, 1920)).split(",,", 1)[1].split(AI_LABEL_TEXT)[0]
    landscape = ai_label_event((1920, 1080)).split(",,", 1)[1].split(AI_LABEL_TEXT)[0]
    assert portrait == landscape


# ---------------------------------------------------------------------------
# 2. compose_artifacts：字幕/标识四种开关组合，只断言事件计数
# ---------------------------------------------------------------------------


def _caption_plan(font_path: Path) -> subtitle_episode.EpisodeSubtitlePlan:
    style = SubtitleStyle(font_family=font_family_from_file(font_path))
    line = LineAlignment(
        utterance_id="U01", text="你好", status="aligned", match_ratio=1.0,
        matched_chars=2, exact_chars=2, total_chars=2, char_times=(),
        start_s=0.05, end_s=0.5, reason="", speaker="", delivery_kind="narration",
    )
    alignment = ShotAlignment(lines=(line,), extra_speech=(), asr_text="你好")
    cue = Cue(shot_no=1, utterance_id="U01", text="你好", start_s=0.05, end_s=0.5)
    shot_plan = subtitle_episode.ShotSubtitlePlan(
        shot_no=1, version_id="v1", cues=(cue,), alignment=alignment, cache_hit=False,
    )
    return subtitle_episode.EpisodeSubtitlePlan(
        shots={1: shot_plan}, style=style, fonts_dir=font_path.parent,
        engine_id="test", model_id="test", asr_elapsed_s=0.0, asr_shots=0, cache_hits=0,
    )


def _dialogue_count(ass_path: Path) -> int:
    return ass_path.read_text(encoding="utf-8").count("Dialogue:")


def test_compose_artifacts_caption_and_label_both_on_adds_one_event(tmp_path):
    font_path = _skip_without_font()
    plan = _caption_plan(font_path)
    label = ai_label_event((1080, 1920))
    artifacts = subtitle_episode.compose_artifacts([{"shot_no": 1, "duration_s": 1.0}], [], plan, tmp_path, (1080, 1920), label)
    assert _dialogue_count(artifacts.ass_path) == 2  # 1 条台词 cue + 1 条标识


def test_compose_artifacts_caption_on_label_off_keeps_single_cue(tmp_path):
    font_path = _skip_without_font()
    plan = _caption_plan(font_path)
    artifacts = subtitle_episode.compose_artifacts([{"shot_no": 1, "duration_s": 1.0}], [], plan, tmp_path, (1080, 1920), None)
    assert _dialogue_count(artifacts.ass_path) == 1


def test_compose_artifacts_caption_off_label_on_writes_label_only_ass(tmp_path):
    _skip_without_font()
    label = ai_label_event((1080, 1920))
    artifacts = subtitle_episode.compose_artifacts([], [], None, tmp_path, (1080, 1920), label)
    assert artifacts is not None
    assert _dialogue_count(artifacts.ass_path) == 1
    assert AI_LABEL_TEXT in artifacts.ass_path.read_text(encoding="utf-8")


def test_compose_artifacts_both_off_returns_none_and_writes_nothing(tmp_path):
    artifacts = subtitle_episode.compose_artifacts([], [], None, tmp_path, (1080, 1920), None)
    assert artifacts is None
    assert not list(tmp_path.glob("*.ass"))  # 关闭时零额外产物、零额外编码


# ---------------------------------------------------------------------------
# 3. draft_concat 首段重编码：与其余 -c copy 分段混拼的真实兼容性
# ---------------------------------------------------------------------------


def _make_prepared_piece(path: Path, *, duration_s: float, color: str) -> None:
    """模拟 _piece_video_args 重编码分支的产物：INTERMEDIATE_VIDEO_ARGS 编码。"""
    subprocess.run(
        ["nice", "-n", "19", "ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=24:d={duration_s}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration_s}",
         *INTERMEDIATE_VIDEO_ARGS, "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", str(path)],
        check=True, capture_output=True, timeout=30,
    )


def _stream_params(path: Path) -> dict:
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,pix_fmt,r_frame_rate",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    return json.loads(raw)["streams"][0]


def _label_filter_arg(font_path: Path, work_dir: Path) -> str:
    style = SubtitleStyle(font_family=font_family_from_file(font_path))
    ass_text = render_ass((), style, (1080, 1920), extra_events=(ai_label_event((1080, 1920)),))
    ass_path = work_dir / "label.ass"
    ass_path.write_text(ass_text, encoding="utf-8")
    return ffmpeg_ass_filter(ass_path, font_path.parent)


def test_relabeled_prefix_mixes_cleanly_with_untouched_copy_segment(tmp_path):
    """首段（3.5s，已覆盖 3s 标识窗口）单独重编码叠字；第二段（1.0s）保持原样。
    两者用真实 concat demuxer -c copy 混拼，核对流参数一致、总时长正确、
    解码无花屏/报错——这就是派单要求的「必须实测」。
    """
    font_path = _skip_without_font()
    piece1, piece2 = tmp_path / "p1.mp4", tmp_path / "p2.mp4"
    _make_prepared_piece(piece1, duration_s=3.5, color="red")
    _make_prepared_piece(piece2, duration_s=1.0, color="green")
    filter_arg = _label_filter_arg(font_path, tmp_path)

    result = concat_draft.relabeled_prefix([piece1, piece2], [3.5, 1.0], filter_arg, timeout_s=60.0)

    assert result[0] != piece1 and result[0].is_file()  # 首段被替身文件取代
    assert result[1] == piece2  # 第二段原样返回，未重编码

    ref = _stream_params(piece2)
    relabeled = _stream_params(result[0])
    assert relabeled["codec_name"] == ref["codec_name"]
    assert relabeled["pix_fmt"] == ref["pix_fmt"]
    assert (relabeled["width"], relabeled["height"]) == (ref["width"], ref["height"])
    assert relabeled["r_frame_rate"] == ref["r_frame_rate"]

    listfile = tmp_path / "list.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in result), encoding="utf-8")
    merged = tmp_path / "merged.mp4"
    subprocess.run(
        ["nice", "-n", "19", "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(merged)],
        check=True, capture_output=True, timeout=60,
    )
    probed = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(merged)],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout
    )
    assert abs(float(probed["format"]["duration"]) - 4.5) < 0.3  # 时长连续：3.5+1.0
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(merged), "-f", "null", "-"],
        capture_output=True, timeout=60,
    )
    assert decode.returncode == 0 and not decode.stderr.strip(), decode.stderr.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 4. _draft_concat_pieces 整体接线：快速路径确实生效 / 关闭时零调用
# ---------------------------------------------------------------------------


def _make_clip(path: Path, *, duration_s: float) -> None:
    subprocess.run(
        ["nice", "-n", "19", "ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=blue:s=64x64:r=24:d={duration_s}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration_s}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", str(path)],
        check=True, capture_output=True, timeout=30,
    )


def test_draft_concat_pieces_label_on_captions_off_keeps_fast_copy_path(tmp_path, monkeypatch):
    _skip_without_font()
    import app.media_exec.concat as concat_mod

    captured: dict = {}
    real_demuxer = concat_mod._run_concat_demuxer

    def spy(*args, **kwargs):
        captured["kwargs"] = kwargs
        return real_demuxer(*args, **kwargs)

    monkeypatch.setattr(concat_mod, "_run_concat_demuxer", spy)
    clip1, clip2 = tmp_path / "shot1.mp4", tmp_path / "shot2.mp4"
    _make_clip(clip1, duration_s=3.5)
    _make_clip(clip2, duration_s=1.0)
    piece_specs = [(1, str(clip1), 1.0), (2, str(clip2), 1.0)]
    probe_by_shot = {no: _probe_concat_media(path) for no, path, _rate in piece_specs}
    label = ai_label_event((1080, 1920))

    total_dur, publish_candidate, artifacts, _cl = _draft_concat_pieces(
        piece_specs, probe_by_shot, tmp_path / "episode.mp4", concat_timeout_s=60.0, subtitle_plan=None,
        play_res=(1080, 1920), label_event=label,
    )

    assert captured["kwargs"]["ass_filter"] is None  # 首段已单独烧完，整体仍 -c copy 零转码
    assert publish_candidate.is_file()
    assert total_dur == pytest.approx(4.5, abs=0.3)
    # _draft_concat_pieces 用完即清理临时目录，ass_path 已不在盘上；用返回对象自带
    # 的 ass_text 断言（与最终交付进 episode.edit-report.json 的字段同一份数据）。
    assert artifacts is not None and AI_LABEL_TEXT in artifacts.ass_text


def test_draft_concat_pieces_label_off_never_calls_relabel(tmp_path, monkeypatch):
    import app.media_exec.concat as concat_mod

    def boom(*_a, **_k):
        raise AssertionError("label 关闭时 relabeled_prefix 不得被调用")

    monkeypatch.setattr(concat_mod.concat_draft, "relabeled_prefix", boom)
    clip1, clip2 = tmp_path / "shot1.mp4", tmp_path / "shot2.mp4"
    _make_clip(clip1, duration_s=1.0)
    _make_clip(clip2, duration_s=1.0)
    piece_specs = [(1, str(clip1), 1.0), (2, str(clip2), 1.0)]
    probe_by_shot = {no: _probe_concat_media(path) for no, path, _rate in piece_specs}

    _total_dur, publish_candidate, artifacts, _cl = _draft_concat_pieces(
        piece_specs, probe_by_shot, tmp_path / "episode.mp4", concat_timeout_s=60.0, subtitle_plan=None,
        play_res=(1080, 1920), label_event=None,
    )

    assert publish_candidate.is_file()
    assert artifacts is None
