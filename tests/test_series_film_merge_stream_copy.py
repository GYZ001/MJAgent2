"""连播成片：各集成片流参数一致且为交付画布时流拷贝拼接，否则重编码（2026-09-05 六条任务并发整段重编码，8 核负载 43）。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import config
from app.domain.series_ops import merge
from app.media_exec.concat import _final_video_path

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe unavailable",
)


def _make_clip(path: Path, *, size: str, duration_s: float = 0.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=red:s={size}:r=24:d={duration_s}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration_s}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", "-ac", "2", str(path)],
        check=True, capture_output=True, timeout=60,
    )


@pytest.fixture
def spy(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(merge, "_storyboard_artifact_ids", lambda _p, _nos: {})
    commands: list[list[str]] = []
    real = merge._run_ffmpeg

    def record(command, *, timeout, context):
        commands.append(command)
        real(command, timeout=timeout, context=context)

    monkeypatch.setattr(merge, "_run_ffmpeg", record)
    return commands


def test_canvas_matching_finals_are_stream_copied(spy) -> None:
    for no in (1, 2):
        _make_clip(_final_video_path("p", no), size="1080x1920")
    report = merge.build_series_film("p", 1, 2, [1, 2])
    assert report["merge_mode"] == "stream_copy" and "-c" in spy[0] and "copy" in spy[0] and "concat" in spy[0]
    assert abs(report["duration_s"] - 1.0) < 0.2 and (report["width"], report["height"]) == (1080, 1920)
    assert not list(merge.series_film_dir("p", 1, 2).glob("*.concat.txt"))  # 清单文件已清理


def test_non_canvas_finals_still_reencode(spy) -> None:
    for no in (1, 2):
        _make_clip(_final_video_path("p", no), size="64x64")
    report = merge.build_series_film("p", 1, 2, [1, 2])
    assert report["merge_mode"] == "reencode" and "-filter_complex" in spy[0]


def test_probe_failure_falls_back_to_reencode(spy, monkeypatch) -> None:
    for no in (1, 2):
        _make_clip(_final_video_path("p", no), size="1080x1920")
    monkeypatch.setattr(merge, "_stream_signature", lambda _path: None)
    assert merge.build_series_film("p", 1, 2, [1, 2])["merge_mode"] == "reencode"
