"""app.media_pipeline.delivery_encode 的单元与真实 ffmpeg 冒烟测试。

真实样片只读，不写：720p ``projects/proj_b100192826cb/episodes/3/shots/2/v1.mp4``、
1080p ``projects/proj_a5d711b0a337/episodes/1/shots/10/v1.mp4``；所有产出写到
``tmp_path``（pytest 提供的临时目录），不落回 projects/ 下。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.media_pipeline.delivery_encode import (
    DELIVERY_HEIGHT,
    DELIVERY_VIDEO_ARGS,
    DELIVERY_WIDTH,
    INTERMEDIATE_VIDEO_ARGS,
    canvas_filter,
    encode_timeout_s,
    probe_resolution,
    uniform_resolution,
)

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
_SAMPLE_720P = Path("projects/proj_b100192826cb/episodes/3/shots/2/v1.mp4")
_SAMPLE_1080P = Path("projects/proj_a5d711b0a337/episodes/1/shots/10/v1.mp4")


def test_encode_timeout_s_lower_bound_is_300() -> None:
    assert encode_timeout_s(0.0) == 300.0
    assert encode_timeout_s(1.0) == 300.0


def test_encode_timeout_s_upper_bound_is_four_hours() -> None:
    assert encode_timeout_s(10_000.0) == 4 * 3600.0


def test_encode_timeout_s_formula_between_bounds() -> None:
    # 300 < total*4+120 < 14400 时应严格按公式计算，不触碰任一端的夹子。
    total = 500.0
    expected = total * 4.0 + 120.0
    assert 300.0 < expected < 4 * 3600.0
    assert encode_timeout_s(total) == pytest.approx(expected)


def test_delivery_video_args_uses_medium_crf20_high_profile() -> None:
    assert "medium" in DELIVERY_VIDEO_ARGS
    assert "20" in DELIVERY_VIDEO_ARGS
    assert "-profile:v" in DELIVERY_VIDEO_ARGS
    assert "high" in DELIVERY_VIDEO_ARGS


def test_intermediate_video_args_uses_veryfast_crf14() -> None:
    assert "veryfast" in INTERMEDIATE_VIDEO_ARGS
    assert "14" in INTERMEDIATE_VIDEO_ARGS


def test_canvas_filter_targets_delivery_resolution_with_lanczos() -> None:
    filter_str = canvas_filter()
    assert "lanczos" in filter_str
    assert f"scale={DELIVERY_WIDTH}:{DELIVERY_HEIGHT}" in filter_str
    assert f"crop={DELIVERY_WIDTH}:{DELIVERY_HEIGHT}" in filter_str


def test_canvas_filter_custom_flags() -> None:
    assert "flags=bicubic" in canvas_filter(flags="bicubic")


def test_uniform_resolution_empty_list_is_none() -> None:
    """空不等于「无需检查」：空列表必须返回 None，不能被当成「已确认一致」。"""
    assert uniform_resolution([]) is None


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")
def test_uniform_resolution_same_size_returns_size(tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    _make_clip(a, width=64, height=64, duration_s=0.5)
    _make_clip(b, width=64, height=64, duration_s=0.5)
    assert uniform_resolution([a, b]) == (64, 64)


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")
def test_uniform_resolution_different_size_returns_none(tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    _make_clip(a, width=64, height=64, duration_s=0.5)
    _make_clip(b, width=96, height=96, duration_s=0.5)
    assert uniform_resolution([a, b]) is None


def _make_clip(path: Path, *, width: int, height: int, duration_s: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=red:s={width}x{height}:r=24:d={duration_s}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True, timeout=30,
    )


def _decode_check(path: Path) -> None:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, timeout=60,
    )
    assert result.returncode == 0
    assert not result.stderr.strip(), result.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")
@pytest.mark.skipif(
    not (_SAMPLE_720P.is_file() and _SAMPLE_1080P.is_file()),
    reason="真实样片不存在于本环境",
)
def test_real_samples_normalize_and_concat_to_delivery_resolution(tmp_path: Path) -> None:
    """720p + 1080p 真实样片各取 3s，经 canvas_filter+INTERMEDIATE_VIDEO_ARGS 归一，
    再 concat + DELIVERY_VIDEO_ARGS：产物必须是 1080x1920 且能被 ffmpeg 无错解码。

    这就是 app.media_exec.concat 混合分辨率快速路径的最小复现：旧实现对混合
    分辨率片段直接 -c copy，rc=0 但容器分辨率与实际帧分辨率不一致，播放器花屏
    或错误缩放；本测试证明新路径必须先归一到同一画布再拼接。
    """
    normalized = []
    for index, sample in enumerate((_SAMPLE_720P, _SAMPLE_1080P)):
        out = tmp_path / f"norm-{index}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(sample), "-t", "3",
                "-vf", canvas_filter(), *INTERMEDIATE_VIDEO_ARGS,
                "-an", str(out),
            ],
            check=True, capture_output=True, timeout=120,
        )
        assert probe_resolution(out) == (DELIVERY_WIDTH, DELIVERY_HEIGHT)
        normalized.append(out)

    assert uniform_resolution(normalized) == (DELIVERY_WIDTH, DELIVERY_HEIGHT)

    listfile = tmp_path / "list.txt"
    listfile.write_text(
        "\n".join(f"file '{path}'" for path in normalized), encoding="utf-8",
    )
    concat_out = tmp_path / "concat.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", str(listfile), *DELIVERY_VIDEO_ARGS, "-an", str(concat_out),
        ],
        check=True, capture_output=True, timeout=180,
    )

    assert probe_resolution(concat_out) == (DELIVERY_WIDTH, DELIVERY_HEIGHT)
    _decode_check(concat_out)
