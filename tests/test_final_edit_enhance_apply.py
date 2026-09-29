"""``app.final_edit_enhance.apply``：唯一编排入口的三条验收线——

1. 三个开关全关时零 IO、``video_path``/``subtitles`` 原样透传（逐字节不变）；
2. 给定一份已核验计划时，三项分别落地并把结果写回报告；
3. 某一项失败（如 TTS 拒绝）只让那一项可见地跳过，不拖垮其余项。

真实 ffmpeg（缺失则 skip）；模型调用与语音合成用 monkeypatch 注入，不打真实
网络——这两层各自的请求形状/响应解析已经在
``tests/test_final_edit_enhance_plan_generate.py``/
``tests/test_voice_qwen_synthesize_speech.py`` 里单独验证过，这里只验证
``apply._run`` 把三者编排起来的接线本身。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.db import get_conn
from app.final_edit_enhance import monologue_audio, plan_generate
from app.final_edit_enhance.apply import apply_enhancements_sync
from app.final_edit_enhance.plan_generate import EnhancementPlan, ResolvedMonologueLine, ResolvedMusicCue, ResolvedTeaserClip

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")

_DURATION_S = 6.0


def _make_clip(path: Path, *, freq: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=24:duration={_DURATION_S}",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={_DURATION_S}",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True, capture_output=True,
    )


def _segment(text: str) -> dict:
    return {"storyboard_pack_segment": {
        "prompt_text": text,
        "dialogue": [{"utterance_id": "U01", "line": text, "speaker_identity_id": "顾屿"}],
    }}


def _seed_episode(conn, tmp_path: Path) -> tuple[str, Path]:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) VALUES('e','p',1,'E','confirmed',0)")
    shot_dir = tmp_path / "shots"
    shot_dir.mkdir()
    path1 = shot_dir / "shot-1.mp4"
    _make_clip(path1, freq=440)
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_contract_json) VALUES('s1','e',1,?,?)",
        (_DURATION_S, json.dumps(_segment("我到底该不该相信他"), ensure_ascii=False)),
    )
    conn.commit()
    return "e", path1


def _probe_duration_s(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


@pytest.fixture
def episode(tmp_path):
    conn = get_conn()
    episode_id, shot_path = _seed_episode(conn, tmp_path)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return conn, ep, episode_id, shot_path


def _call(conn, ep, episode_id, candidate_path, work_dir, *, base_duration_s=_DURATION_S, subtitles=None):
    return apply_enhancements_sync(
        conn, ep_row=ep, episode_id=episode_id, candidate_path=candidate_path, final_path=work_dir / ".." / "episode.mp4",
        subtitles_section=subtitles if subtitles is not None else {"enabled": False},
        piece_specs=[(1, str(candidate_path), 1.0)], play_res=(320, 240), style=None,
        base_duration_s=base_duration_s, work_dir=work_dir, video_delivery_manifest_hash="hash-1",
    )


def test_all_switches_off_is_zero_io_and_byte_identical(episode, tmp_path):
    conn, ep, episode_id, shot_path = episode
    before_hash = hashlib.sha256(shot_path.read_bytes()).hexdigest()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = _call(conn, ep, episode_id, shot_path, work_dir)

    assert result.video_path == shot_path
    assert result.duration_s == _DURATION_S
    assert result.subtitles == {"enabled": False}
    for feature in ("music_bed", "teaser", "monologue"):
        assert result.enhancements[feature]["applied"] is False
    assert hashlib.sha256(shot_path.read_bytes()).hexdigest() == before_hash
    assert list(work_dir.iterdir()) == []


def test_switches_off_skips_even_with_valid_library_and_plan_available(episode, tmp_path, monkeypatch):
    """比"什么都没配置"更严格的验证：即使曲库/计划都真实可用，开关关闭时也
    必须原样透传——不能靠"曲库缺失"这类旁路条件顺带蒙对"开关关闭"这条判据。
    """
    conn, ep, episode_id, shot_path = episode
    before_hash = hashlib.sha256(shot_path.read_bytes()).hexdigest()

    library_dir = tmp_path / "music_lib"
    library_dir.mkdir()
    (library_dir / "converted").mkdir()
    track_path = library_dir / "converted" / "t1.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=10", str(track_path)],
        check=True, capture_output=True,
    )
    (library_dir / "manifest.json").write_text(json.dumps({"tracks": [
        {"track_id": "t1", "title": "曲一", "duration_s": 10.0, "mood_tags": [], "converted_file": "converted/t1.wav"},
    ]}), encoding="utf-8")
    from app.db import set_setting

    set_setting("music_library_dir", str(library_dir))

    plan = EnhancementPlan(
        music_cues=(ResolvedMusicCue(1, "t1"),), teaser_clips=(ResolvedTeaserClip(1, 0.0, "开场心动"),),
        monologue_lines=(), dropped=(), teaser_total_duration_s=2.0,
    )

    async def fake_generate_plan(**_kwargs):
        return plan

    monkeypatch.setattr(plan_generate, "generate_plan", fake_generate_plan)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = _call(conn, ep, episode_id, shot_path, work_dir)

    assert result.video_path == shot_path
    for feature in ("music_bed", "teaser", "monologue"):
        assert result.enhancements[feature]["applied"] is False
    assert hashlib.sha256(shot_path.read_bytes()).hexdigest() == before_hash
    assert list(work_dir.iterdir()) == []


def test_full_pipeline_with_all_three_switches_on(episode, tmp_path, monkeypatch):
    conn, ep, episode_id, shot_path = episode
    from app.project_settings import update_project_settings

    update_project_settings(
        conn, "p", adaptation_mode=None, aspect_ratio=None, ai_label_enabled=None,
        enhance_music_bed=True, enhance_teaser=True, enhance_monologue=True,
        narrator_voice_character=None,
    )
    conn.commit()

    library_dir = tmp_path / "music_lib"
    library_dir.mkdir()
    (library_dir / "converted").mkdir()
    track_path = library_dir / "converted" / "t1.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=10", str(track_path)],
        check=True, capture_output=True,
    )
    (library_dir / "manifest.json").write_text(json.dumps({"tracks": [
        {"track_id": "t1", "title": "曲一", "duration_s": 10.0, "mood_tags": [], "converted_file": "converted/t1.wav"},
    ]}), encoding="utf-8")
    from app.db import set_setting
    set_setting("music_library_dir", str(library_dir))

    plan = EnhancementPlan(
        music_cues=(ResolvedMusicCue(1, "t1"),),
        teaser_clips=(ResolvedTeaserClip(1, 0.0, "开场心动"),),
        monologue_lines=(ResolvedMonologueLine(3.0, 5.5, "顾屿", "我到底该不该相信他"),),
        dropped=(), teaser_total_duration_s=3.0,
    )

    async def fake_generate_plan(**_kwargs):
        return plan

    monkeypatch.setattr(plan_generate, "generate_plan", fake_generate_plan)

    async def fake_synthesize(_conn, _project_id, lines, work_dir):
        items = []
        for i, line in enumerate(lines):
            audio_path = work_dir / f"mono-{i}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=600:duration=2", str(audio_path)],
                check=True, capture_output=True,
            )
            items.append(monologue_audio.MonologueAudioItem(
                start_s=line.start_s, duration_s=2.0, text=line.text, character_name=line.character_name, audio_path=audio_path,
            ))
        return items, []

    monkeypatch.setattr(monologue_audio, "synthesize_monologue_lines", fake_synthesize)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    subtitles = {"enabled": True, "cues_timeline": [
        {"shot_no": 1, "utterance_id": "U01", "text": "我到底该不该相信他", "start_s": 0.0, "end_s": 2.5, "estimated": False},
    ], "cues": 1, "ass_text": "old", "ass_sha256": "old", "srt_sha256": "old"}

    result = _call(conn, ep, episode_id, shot_path, work_dir, subtitles=subtitles)

    assert result.enhancements["music_bed"]["applied"] is True
    assert result.enhancements["teaser"]["applied"] is True
    assert result.enhancements["monologue"]["applied"] is True
    assert result.duration_s > _DURATION_S  # 预告片时长已经并入
    assert abs(_probe_duration_s(result.video_path) - result.duration_s) < 0.2
    # 正片字幕整体后移了预告时长
    shifted_dialogue = next(c for c in result.subtitles["cues_timeline"] if c["shot_no"] == 1)
    assert shifted_dialogue["start_s"] > 0.0
    assert any(c["shot_no"] == -1 for c in result.subtitles["cues_timeline"])  # 独白 cue 已追加


def test_monologue_failure_is_visible_and_does_not_block_music_bed(episode, tmp_path, monkeypatch):
    conn, ep, episode_id, shot_path = episode
    from app.project_settings import update_project_settings

    update_project_settings(
        conn, "p", adaptation_mode=None, aspect_ratio=None, ai_label_enabled=None,
        enhance_music_bed=True, enhance_teaser=False, enhance_monologue=True,
        narrator_voice_character=None,
    )
    conn.commit()

    library_dir = tmp_path / "music_lib"
    library_dir.mkdir()
    (library_dir / "converted").mkdir()
    track_path = library_dir / "converted" / "t1.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=10", str(track_path)],
        check=True, capture_output=True,
    )
    (library_dir / "manifest.json").write_text(json.dumps({"tracks": [
        {"track_id": "t1", "title": "曲一", "duration_s": 10.0, "mood_tags": [], "converted_file": "converted/t1.wav"},
    ]}), encoding="utf-8")
    from app.db import set_setting
    set_setting("music_library_dir", str(library_dir))

    plan = EnhancementPlan(
        music_cues=(ResolvedMusicCue(1, "t1"),), teaser_clips=(),
        monologue_lines=(ResolvedMonologueLine(3.0, 5.5, "顾屿", "我到底该不该相信他"),),
        dropped=(), teaser_total_duration_s=0.0,
    )

    async def fake_generate_plan(**_kwargs):
        return plan

    monkeypatch.setattr(plan_generate, "generate_plan", fake_generate_plan)

    async def fake_synthesize_fails(_conn, _project_id, lines, _work_dir):
        return [], [{"item": {"character_name": lines[0].character_name}, "reason": "语音合成失败：模拟的供应商拒绝"}]

    monkeypatch.setattr(monologue_audio, "synthesize_monologue_lines", fake_synthesize_fails)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = _call(conn, ep, episode_id, shot_path, work_dir)

    assert result.enhancements["monologue"]["applied"] is False
    assert "模拟的供应商拒绝" in result.enhancements["monologue"]["reason"]
    assert result.enhancements["music_bed"]["applied"] is True


def _seed_music_library(tmp_path: Path) -> Path:
    library_dir = tmp_path / "music_lib"
    library_dir.mkdir()
    (library_dir / "converted").mkdir()
    track_path = library_dir / "converted" / "t1.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=10", str(track_path)],
        check=True, capture_output=True,
    )
    (library_dir / "manifest.json").write_text(json.dumps({"tracks": [
        {"track_id": "t1", "title": "曲一", "duration_s": 10.0, "mood_tags": [], "converted_file": "converted/t1.wav"},
    ]}), encoding="utf-8")
    return library_dir


def test_dropped_plan_items_are_surfaced_as_rejected_in_report(episode, tmp_path, monkeypatch):
    """模型提名过、被代码核验丢弃的条目必须出现在报告里——不能只写进内部
    缓存文件（2026-09-28 评审发现）。"""
    conn, ep, episode_id, shot_path = episode
    from app.db import set_setting
    from app.project_settings import update_project_settings

    update_project_settings(
        conn, "p", adaptation_mode=None, aspect_ratio=None, ai_label_enabled=None,
        enhance_music_bed=True, enhance_teaser=False, enhance_monologue=False,
        narrator_voice_character=None,
    )
    conn.commit()
    set_setting("music_library_dir", str(_seed_music_library(tmp_path)))

    plan = EnhancementPlan(
        music_cues=(ResolvedMusicCue(1, "t1"),), teaser_clips=(), monologue_lines=(),
        dropped=({"feature": "music_bed", "item": {"shot_no": 99, "track_id": "ghost"}, "reason": "曲目 ID「ghost」不在曲库清单中"},),
        teaser_total_duration_s=0.0,
    )

    async def fake_generate_plan(**_kwargs):
        return plan

    monkeypatch.setattr(plan_generate, "generate_plan", fake_generate_plan)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = _call(conn, ep, episode_id, shot_path, work_dir)

    assert result.enhancements["music_bed"]["applied"] is True
    rejected = result.enhancements["music_bed"]["rejected"]
    assert len(rejected) == 1
    assert "ghost" in rejected[0]["reason"]
    assert "rejected" not in result.enhancements["teaser"]  # 没有被丢弃的同 feature 条目就不该出现这个键


def test_music_bed_only_does_not_duck_arbitrary_silence_windows(episode, tmp_path, monkeypatch):
    """只开配乐、不开独白时传给 ``music_mix.mix_audio_track`` 的独白播放区间
    必须是空列表——不能是"任意候选静默窗口"，否则没有独白播放时音乐也会被
    莫名压低（2026-09-28 评审发现）。用 ``base_duration_s=40`` 让静默窗口计算
    出的候选窗口非空（>= 30s 才会入选，见 ``apply.MONOLOGUE_MIN_WINDOW_S``），
    才能真正区分"传候选窗口"和"传实际独白播放区间（此处应为空）"这两种实现。
    ``mix_audio_track`` 本身直接打桩（不跑真实 ffmpeg）：真实混音输出已由
    ``tests/test_final_edit_enhance_music_mix.py`` 单独验证，这里只验证接线。
    """
    conn, ep, episode_id, shot_path = episode
    from app.db import set_setting
    from app.project_settings import update_project_settings

    update_project_settings(
        conn, "p", adaptation_mode=None, aspect_ratio=None, ai_label_enabled=None,
        enhance_music_bed=True, enhance_teaser=False, enhance_monologue=False,
        narrator_voice_character=None,
    )
    conn.commit()
    set_setting("music_library_dir", str(_seed_music_library(tmp_path)))

    plan = EnhancementPlan(
        music_cues=(ResolvedMusicCue(1, "t1"),), teaser_clips=(), monologue_lines=(), dropped=(), teaser_total_duration_s=0.0,
    )

    async def fake_generate_plan(**_kwargs):
        return plan

    monkeypatch.setattr(plan_generate, "generate_plan", fake_generate_plan)

    from app.final_edit_enhance import music_mix

    captured: dict = {}

    def fake_mix(candidate_path, music_bed_path, monologue_track_path, monologue_windows, total_duration_s, work_dir):
        captured["monologue_windows"] = monologue_windows
        return candidate_path

    monkeypatch.setattr(music_mix, "mix_audio_track", fake_mix)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _call(conn, ep, episode_id, shot_path, work_dir, base_duration_s=40.0)

    assert "monologue_windows" in captured  # 混音阶段确实被触发了（配乐已应用）
    assert captured["monologue_windows"] == []
