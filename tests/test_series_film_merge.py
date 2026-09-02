"""连播成片合并（``app.domain.series_ops.merge``）的真实 ffmpeg 回归。

照 tests/test_concat_av_normalization.py 的写法：样本全部在测试内用 ffmpeg
自行生成，不依赖 projects/ 下任何素材；ffmpeg/ffprobe 不可用时整文件 skip。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import config
from app.domain.series_ops import merge
from app.media_exec.concat import _final_video_path

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")


def _make_clip(path: Path, *, duration_s: float, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=24:d={duration_s}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration_s}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100",
            str(path),
        ],
        check=True, capture_output=True, timeout=30,
    )


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    return tmp_path


def test_build_series_film_two_episodes_concatenates_and_reports_chapters(project_dir) -> None:
    project_id = "proj-merge"
    ep1 = _final_video_path(project_id, 1)
    ep2 = _final_video_path(project_id, 2)
    _make_clip(ep1, duration_s=2.0, color="red")
    _make_clip(ep2, duration_s=2.0, color="blue")

    report = merge.build_series_film(project_id, 1, 2, [1, 2])

    out_dir = merge.series_film_dir(project_id, 1, 2)
    film_path = out_dir / "film.mp4"
    assert film_path.is_file()
    assert (out_dir / "film.report.json").is_file()
    assert not (out_dir / "film.tmp.mp4").exists()

    assert report["episode_from"] == 1 and report["episode_to"] == 2
    assert len(report["chapters"]) == 2
    assert report["chapters"][0]["episode_no"] == 1
    assert report["chapters"][0]["start_s"] == pytest.approx(0.0, abs=0.05)
    assert report["chapters"][1]["episode_no"] == 2
    assert report["chapters"][1]["start_s"] == pytest.approx(2.0, rel=0.15)
    assert report["duration_s"] == pytest.approx(4.0, rel=0.15)
    assert film_path.stat().st_size == report["size_bytes"]


def test_build_series_film_single_episode_span_is_legal(project_dir) -> None:
    """用户拍板：单集连播也合法（episode_from == episode_to），合并步骤照常执行。"""
    project_id = "proj-merge-single"
    ep1 = _final_video_path(project_id, 5)
    _make_clip(ep1, duration_s=2.0, color="green")

    report = merge.build_series_film(project_id, 5, 5, [5])

    assert len(report["chapters"]) == 1
    assert report["chapters"][0] == {"episode_no": 5, "start_s": 0.0, "duration_s": pytest.approx(2.0, rel=0.1)}
    film_path = merge.series_film_dir(project_id, 5, 5) / "film.mp4"
    assert film_path.is_file()


def test_merge_is_current_tracks_input_fingerprints(project_dir) -> None:
    project_id = "proj-merge-current"
    ep1 = _final_video_path(project_id, 1)
    _make_clip(ep1, duration_s=2.0, color="yellow")
    merge.build_series_film(project_id, 1, 1, [1])

    assert merge.merge_is_current(project_id, 1, 1, [1]) is True

    # 触碰输入（重新生成同一集成片）后指纹漂移，判据必须翻成 False。
    _make_clip(ep1, duration_s=2.0, color="yellow")
    assert merge.merge_is_current(project_id, 1, 1, [1]) is False


def test_build_series_film_missing_episode_raises_and_leaves_no_output(project_dir) -> None:
    project_id = "proj-merge-missing"
    ep1 = _final_video_path(project_id, 1)
    _make_clip(ep1, duration_s=2.0, color="red")
    # 第 2 集没有成片。

    with pytest.raises(RuntimeError, match="尚无成片"):
        merge.build_series_film(project_id, 1, 2, [1, 2])

    out_dir = merge.series_film_dir(project_id, 1, 2)
    assert not (out_dir / "film.mp4").exists()


def test_film_for_range_and_latest_film_projection(project_dir) -> None:
    project_id = "proj-merge-projection"
    ep1 = _final_video_path(project_id, 1)
    _make_clip(ep1, duration_s=2.0, color="purple")
    merge.build_series_film(project_id, 1, 1, [1])

    projected = merge.film_for_range(project_id, 1, 1)
    assert projected is not None
    assert projected["url"] is not None
    assert projected["episode_from"] == 1 and projected["episode_to"] == 1
    assert projected["duration_s"] == pytest.approx(2.0, rel=0.15)

    latest = merge.latest_film(project_id)
    assert latest is not None
    assert latest["path"] == projected["path"]

    assert merge.film_for_range(project_id, 9, 9) is None
    assert merge.latest_film("no-such-project") is None
