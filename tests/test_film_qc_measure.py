"""红/绿驱动 scripts/film_qc_measure.py。

背景：2026-09-23 一个只读子任务在生产机上用 ffmpeg 手工量了 5 部成片
（/tmp/mjscan_a2/），发现接缝音量跳变 12~28 dB、开场静止等问题。本文件先于
scripts/film_qc_measure.py 落地写成，脚本不存在时应当收集失败（ImportError），
这就是本文件的「红」；脚本实现后转绿。

样本在测试内用 ffmpeg lavfi 现场合成（不依赖 projects/ 下任何真实产物、不
依赖数据库），64x64、三段各 2 秒拼接：
  段1（0~2s）：纯色静止画面 + -8dBFS 正弦音——用来验证「静止检出」与
               「开场 0~3 秒冻结判定」，且是响的一段。
  段2（2~4s）：testsrc2 动态画面 + -30dBFS 正弦音——安静的一段。
  段3（4~6s）：testsrc2 动态画面 + -8dBFS 正弦音——又变响的一段。
段1/2 与段2/3 两个接缝的响度差都应 ≈ 22dB（-8 - (-30)），远超 6dB 判定阈值；
段1 应触发 freezedetect 且落在开场窗口内。

配套一份手写的最小 edit-report.json（3 镜、2 个零重叠硬切接缝），验证分段
重建；另一个测试不给 edit-report，断言分段指标是 None 而不是被均分编造出来
的假数字。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="需要 ffmpeg/ffprobe")

_SEG_S = 2.0


def _synthesize(path: Path) -> None:
    """现场合成 3 段拼接的极小样本，各段口径见文件头 docstring。"""
    filter_complex = (
        "[1:a]volume=-8dB[a0];"
        "[3:a]volume=-30dB[a1];"
        "[5:a]volume=-8dB[a2];"
        "[0:v][a0][2:v][a1][4:v][a2]concat=n=3:v=1:a=1[v][a]"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=gray:s=64x64:r=24:d={_SEG_S}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={_SEG_S}",
        "-f", "lavfi", "-i", f"testsrc2=s=64x64:r=24:d={_SEG_S}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={_SEG_S}",
        "-f", "lavfi", "-i", f"testsrc2=s=64x64:r=24:d={_SEG_S}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={_SEG_S}",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)


def _write_edit_report(mp4_path: Path, *, total_duration_s: float) -> None:
    """最小 edit-report：3 镜、2 个零重叠硬切接缝——与 _synthesize 的直接拼接一致
    （没有 xfade，所以 transitions[].duration_s 如实写 0）。
    """
    report = {
        "total_duration_s": total_duration_s,
        "timeline": {"included_shot_nos": [1, 2, 3]},
        "transitions": [
            {"from_shot_no": 1, "to_shot_no": 2, "edit_type": "cut", "duration_s": 0.0},
            {"from_shot_no": 2, "to_shot_no": 3, "edit_type": "cut", "duration_s": 0.0},
        ],
        "video_delivery_manifest": {
            "items": [
                {"shot_no": 1, "adopted_version_id": "ver_1"},
                {"shot_no": 2, "adopted_version_id": "ver_2"},
                {"shot_no": 3, "adopted_version_id": "ver_3"},
            ]
        },
    }
    report_path = mp4_path.with_name(mp4_path.stem + ".edit-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(scope="module")
def synthetic_episode(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("film_qc_fixture")
    mp4 = d / "episode.mp4"
    _synthesize(mp4)
    _write_edit_report(mp4, total_duration_s=6.0)
    return mp4


def test_segments_seam_jump_and_opening_freeze(synthetic_episode: Path) -> None:
    """核心断言：分段数正确、接缝跳变≥6dB 被检出且量级合理、开场冻结判定正确。"""
    from scripts.film_qc_measure import measure_episode

    result = measure_episode(
        synthetic_episode,
        edit_report_path=synthetic_episode.with_name("episode.edit-report.json"),
        srt_path=None,
    )

    segs = result["segments"]
    assert segs is not None and "error" not in segs
    assert segs["n_segments"] == 3

    jumps = {(j["from_shot_no"], j["to_shot_no"]): j["jump_db"] for j in segs["seam_jumps"]}
    jump01 = jumps[(1, 2)]
    assert jump01 is not None and 6.0 <= abs(jump01) <= 35.0
    assert segs["seam_jump_count_ge_threshold"] >= 1
    assert segs["seam_jump_max_abs_db"] >= 6.0

    freeze = result["cuts_freeze_black"]["freeze"]
    assert freeze["total_s"] > 0.0
    assert freeze["opening_frozen"] is True
    assert freeze["opening_frozen_s"] >= 1.0


def test_no_edit_report_segments_not_fabricated(synthetic_episode: Path) -> None:
    """没有 edit-report 时必须退化为「不给分段指标」，不得均分猜一份假数字。"""
    from scripts.film_qc_measure import measure_episode

    result = measure_episode(synthetic_episode, edit_report_path=None, srt_path=None)

    assert result["segments"] is None


def test_missing_input_exits_with_input_error() -> None:
    """输入不可读 -> 退出码 2（契约见文件头 docstring 的「退出码」章节）。"""
    from scripts.film_qc_measure import EXIT_INPUT_ERROR, main

    rc = main(["/tmp/definitely_missing_dir_xyz_123/episode.mp4"])
    assert rc == EXIT_INPUT_ERROR


def test_estimate_segment_bounds_equal_duration_is_exact() -> None:
    """无交叠、时长真实相等场景下，代数估计应与真值完全吻合（纯函数单测，不跑 ffmpeg）。"""
    from scripts.film_qc_measure import estimate_segment_bounds

    report = {
        "video_delivery_manifest": {"items": [{"shot_no": 1}, {"shot_no": 2}, {"shot_no": 3}]},
        "transitions": [
            {"from_shot_no": 1, "to_shot_no": 2, "duration_s": 0.0},
            {"from_shot_no": 2, "to_shot_no": 3, "duration_s": 0.0},
        ],
    }
    bounds = estimate_segment_bounds(report, probed_duration_s=6.0)
    assert bounds is not None
    assert [b["start"] for b in bounds] == [0.0, 2.0, 4.0]
    assert [b["end"] for b in bounds] == [2.0, 4.0, 6.0]


def test_refine_bounds_with_cuts_snaps_within_tolerance() -> None:
    """代数估计有漂移时，真实 scdet 切点应把接缝纠正回精确值。"""
    from scripts.film_qc_measure import refine_bounds_with_cuts

    bounds = [
        {"shot_no": 1, "start": 0.0, "end": 2.05},
        {"shot_no": 2, "start": 2.05, "end": 4.10},
        {"shot_no": 3, "start": 4.10, "end": 6.0},
    ]
    refine_bounds_with_cuts(bounds, [2.0, 4.0], 1.0)
    assert bounds[0]["end"] == 2.0
    assert bounds[1]["start"] == 2.0
    assert bounds[1]["end"] == 4.0
    assert bounds[2]["start"] == 4.0
