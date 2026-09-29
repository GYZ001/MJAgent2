"""``app.final_edit_enhance.music_mix``/``monologue_audio.build_monologue_track``：
真实跑一次短样 ffmpeg（lavfi 生成的正弦波/测试图案），验证混音输出的时长与
响度——不是单纯断言 ffmpeg 命令字符串包含某个滤镜名。

需要系统 ``ffmpeg``/``ffprobe``；CI/沙箱如果没有会跳过，不误判红。
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.final_edit_enhance import monologue_audio, music_mix
from app.final_edit_enhance.monologue_audio import MonologueAudioItem
from app.final_edit_enhance.music_library import MusicLibrary, MusicTrack
from app.final_edit_enhance.music_runs import MusicRun

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="需要系统 ffmpeg/ffprobe")


def _probe_duration_s(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def _mean_volume_db(path) -> float:
    result = subprocess.run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    line = next(line for line in result.stderr.splitlines() if "mean_volume" in line)
    return float(line.split(":")[1].strip().split(" ")[0])


def _make_tone(path, freq: int, duration_s: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration_s}",
         "-ar", "48000", "-ac", "2", str(path)],
        check=True,
    )


def _make_video_with_tone(path, freq: int, duration_s: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=24:duration={duration_s}",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration_s}",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


def test_build_music_bed_total_duration_matches_sum_of_runs(tmp_path) -> None:
    _make_tone(tmp_path / "t1.wav", 440, 5.0)
    _make_tone(tmp_path / "t2.wav", 220, 5.0)
    library = MusicLibrary(
        tracks=(MusicTrack("t1", "t1", (), 5.0, tmp_path / "t1.wav"), MusicTrack("t2", "t2", (), 5.0, tmp_path / "t2.wav")),
        dropped=(),
    )
    runs = [MusicRun(0.0, 3.0, "t1"), MusicRun(3.0, 3.0, "t2")]
    out = music_mix.build_music_bed(runs, library, tmp_path)
    assert abs(_probe_duration_s(out) - 6.0) < 0.05
    assert _mean_volume_db(out) > -90  # 不是纯静音（混音管线没有把音频路由丢了）


def test_build_monologue_track_pads_to_total_duration(tmp_path) -> None:
    _make_tone(tmp_path / "mono.wav", 660, 2.0)
    item = MonologueAudioItem(start_s=1.0, duration_s=2.0, text="test", character_name="顾屿", audio_path=tmp_path / "mono.wav")
    track = monologue_audio.build_monologue_track([item], 6.0, tmp_path)
    assert track is not None
    assert abs(_probe_duration_s(track) - 6.0) < 0.05


def test_build_monologue_track_returns_none_for_empty_items(tmp_path) -> None:
    assert monologue_audio.build_monologue_track([], 6.0, tmp_path) is None


def test_mix_audio_track_with_music_and_monologue_has_expected_duration_and_is_not_silent(tmp_path) -> None:
    _make_video_with_tone(tmp_path / "candidate.mp4", 880, 6.0)
    _make_tone(tmp_path / "t1.wav", 440, 6.0)
    library = MusicLibrary(tracks=(MusicTrack("t1", "t1", (), 6.0, tmp_path / "t1.wav"),), dropped=())
    music_bed = music_mix.build_music_bed([MusicRun(0.0, 6.0, "t1")], library, tmp_path)

    _make_tone(tmp_path / "mono.wav", 660, 2.0)
    item = MonologueAudioItem(start_s=1.0, duration_s=2.0, text="test", character_name="顾屿", audio_path=tmp_path / "mono.wav")
    mono_track = monologue_audio.build_monologue_track([item], 6.0, tmp_path)

    out = music_mix.mix_audio_track(tmp_path / "candidate.mp4", music_bed, mono_track, [(4.0, 6.0)], 6.0, tmp_path)
    assert abs(_probe_duration_s(out) - 6.0) < 0.1
    assert _mean_volume_db(out) > -90


def test_mix_audio_track_without_music_bed_still_mixes_monologue(tmp_path) -> None:
    """只开独白、不开配乐时 ``music_bed_path=None``——混音不应该因为少一路输入
    就崩溃或产出静音。"""
    _make_video_with_tone(tmp_path / "candidate.mp4", 880, 4.0)
    _make_tone(tmp_path / "mono.wav", 660, 1.5)
    item = MonologueAudioItem(start_s=0.5, duration_s=1.5, text="test", character_name="顾屿", audio_path=tmp_path / "mono.wav")
    mono_track = monologue_audio.build_monologue_track([item], 4.0, tmp_path)

    out = music_mix.mix_audio_track(tmp_path / "candidate.mp4", None, mono_track, [], 4.0, tmp_path)
    assert abs(_probe_duration_s(out) - 4.0) < 0.1
    assert _mean_volume_db(out) > -90
