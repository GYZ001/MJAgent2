"""``app.final_edit_enhance.teaser``：真实跑一次短样 ffmpeg 验证预告片剪辑/
拼接/首尾淡入淡出/接回正片的时长——不是断言命令字符串。
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.final_edit_enhance import teaser
from app.final_edit_enhance.plan_generate import ResolvedTeaserClip
from app.final_edit_enhance.plan_schema import TEASER_CLIP_LENGTH_S

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="需要系统 ffmpeg/ffprobe")

PLAY_RES = (320, 240)


def _probe_duration_s(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def _make_shot(path, pattern: str, freq: int, duration_s: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"{pattern}=size=320x240:rate=24:duration={duration_s}",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration_s}",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


def test_build_teaser_duration_matches_sum_of_clip_lengths(tmp_path) -> None:
    """``ResolvedTeaserClip`` 不再带 ``end_s``：片长固定为 ``TEASER_CLIP_LENGTH_S``，
    ``build_teaser`` 自己用 ``start_s + TEASER_CLIP_LENGTH_S`` 算结束秒数。"""
    _make_shot(tmp_path / "shot1.mp4", "testsrc", 440, 15.0)
    _make_shot(tmp_path / "shot2.mp4", "testsrc2", 330, 15.0)
    piece_specs = [(1, str(tmp_path / "shot1.mp4"), 1.0), (2, str(tmp_path / "shot2.mp4"), 1.0)]
    clips = (
        ResolvedTeaserClip(shot_no=1, start_s=2.0, reason="心动瞬间"),
        ResolvedTeaserClip(shot_no=2, start_s=3.0, reason="转折"),
    )
    out = teaser.build_teaser(clips, piece_specs, PLAY_RES, tmp_path)
    expected_s = 2 * TEASER_CLIP_LENGTH_S  # 两段固定片长相加；24fps 帧量化误差留 0.2s 容差。
    assert abs(_probe_duration_s(out) - expected_s) < 0.2


def test_build_teaser_applies_playback_rate_to_source_seek(tmp_path) -> None:
    """``rate=2.0``（该段实际是 2 倍速播放）时，段自己时间轴上的 1 秒对应源
    文件里的 2 秒——``_source_seconds`` 换算错的话，这里会剪到别的内容或直接
    越界报错。输出时长应等于固定片长 ``TEASER_CLIP_LENGTH_S``（换算正确时
    倍速不改变段自己时间轴上的片长）。"""
    _make_shot(tmp_path / "shot1.mp4", "testsrc", 440, 15.0)
    piece_specs = [(1, str(tmp_path / "shot1.mp4"), 2.0)]
    clips = (ResolvedTeaserClip(shot_no=1, start_s=0.0, reason="x"),)
    out = teaser.build_teaser(clips, piece_specs, PLAY_RES, tmp_path)
    assert abs(_probe_duration_s(out) - TEASER_CLIP_LENGTH_S) < 0.2


def test_prepend_teaser_total_duration_is_sum_of_both(tmp_path) -> None:
    _make_shot(tmp_path / "shot1.mp4", "testsrc", 440, 15.0)
    piece_specs = [(1, str(tmp_path / "shot1.mp4"), 1.0)]
    clips = (ResolvedTeaserClip(shot_no=1, start_s=0.0, reason="x"),)
    teaser_path = teaser.build_teaser(clips, piece_specs, PLAY_RES, tmp_path)
    teaser_duration = _probe_duration_s(teaser_path)

    _make_shot(tmp_path / "main.mp4", "testsrc2", 220, 6.0)
    merged = teaser.prepend_teaser(teaser_path, tmp_path / "main.mp4", PLAY_RES, tmp_path)
    assert abs(_probe_duration_s(merged) - (teaser_duration + 6.0)) < 0.2
