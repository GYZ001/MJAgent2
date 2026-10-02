"""画面人数与身份闸门：单帧疑点的加密复核（2026-10-01）。

生产实测（proj_ca86b15ab7d7 第 1 集第 33 段 ver_153e163b0c1d）：视频模型在两个
镜头之间自作主张叠化了约 0.95 秒（画面显示约 7.85-8.8 秒区间顾屿侧脸与正脸半
透明叠在一起，观众看就是「两个顾屿」）。``character_count_gate`` 稀疏抽样
（``subtitle_gate.FRAME_INTERVAL_S``≈1.5 秒一帧）命中了第 6 帧（约 7.5 秒）两个
顾屿，但异常持续不到一个抽样间隔，相邻的第 5/7 帧各自只报一个顾屿——
``MIN_CONSECUTIVE_FRAMES=2`` 的连续性判据因此永远拿不到第二帧佐证，
``character_duplicated`` 判 False，自动重抽没有触发。

修法：不改连续性判据本身（单帧噪声过滤原则不变），只是把「连续」放到足够细的
时间粒度上。``character_count_gate.suspect_window`` 已经判出首轮任一帧单帧命中
（headcount 超员或同一具名角色重复，不要求连续）的时间窗；本模块围绕这个窗口
用 ffmpeg 加密抽帧，复用同一套 VLM 提名（``character_count_gate.build_
messages``/``parse_verdict``）与代码核验（``evaluate_headcount_and_
duplication``，连续阈值不变）重新判一次——加密帧连续命中才改判成立。

取舍：加密复核调用失败按既有「未判定不拦」放行，原判（稀疏首轮结论）保留，只
在 ``dense_recheck`` 键里留痕（时间窗、是否命中、原因）供人工核对；干净视频
（首轮没有任何单帧疑点）不会走到这个模块，不多发任何调用。

循环导入说明：本模块模块级反向导入 ``character_count_gate`` 的
``build_messages``/``parse_verdict``/``evaluate_headcount_and_duplication``/
``DENSE_FRAME_INTERVAL_S``（复用判据，不复刻）；``character_count_gate`` 因此
只能在真正触发加密复核时才函数内 import 本模块（已在其 ``evaluate_version``
里就地写明），不能在模块级引入，否则两边互相 import 立刻循环。
"""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app import hiagent

from . import subtitle_gate
from .character_count_gate import (
    DENSE_FRAME_INTERVAL_S,
    build_messages,
    evaluate_headcount_and_duplication,
    parse_verdict,
)


def _sample_window_frames(video_path: str, *, start_s: float, frame_count: int) -> list[bytes]:
    """从 ``start_s`` 起每 ``DENSE_FRAME_INTERVAL_S`` 秒抽一帧，共 ``frame_count``
    帧。``-ss`` 放在 ``-i`` 之后走精确寻址（而不是更快但基于关键帧、在这个
    0.25 秒粒度下不够准的快速寻址）——加密复核只在疑点命中时触发，不频繁，
    牺牲一点速度换时间粒度的准确性是值得的。"""
    with tempfile.TemporaryDirectory() as td:
        pattern = Path(td) / "f%02d.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-ss", f"{max(0.0, start_s):.3f}",
             "-vf", f"fps=1/{DENSE_FRAME_INTERVAL_S},scale={subtitle_gate.FRAME_WIDTH}:-2",
             "-frames:v", str(frame_count), "-q:v", "4", str(pattern)],
            check=True, capture_output=True,
        )
        return [path.read_bytes() for path in sorted(Path(td).glob("f*.jpg"))]


async def run_dense_recheck(
    verdict: dict[str, Any], window: tuple[float, float], roster: list[dict[str, str]],
    video_path: str, call_meta: dict[str, Any],
) -> dict[str, Any]:
    """围绕疑点时间窗加密抽帧，重跑一次同样的 VLM 提名+代码核验；加密帧连续
    命中才改判成立，调用失败按未判定不拦、原判（``verdict``）保留，详见模块
    文档。``verdict`` 已经带着 ``allowed_headcount``/``roster_names``（来自
    ``character_count_gate._judge_parsed_verdict``），不需要调用方另传。
    """
    start, end = window
    frame_count = max(2, round((end - start) / DENSE_FRAME_INTERVAL_S) + 1)
    try:
        frames = await asyncio.to_thread(_sample_window_frames, video_path, start_s=start, frame_count=frame_count)
        if not frames:
            raise ValueError("加密复核抽不出任何帧")
        raw = await hiagent.chat(
            build_messages(frames, roster), temperature=0, max_tokens=2000,
            call_meta={"kind": "vlm_character_count_gate_dense", **call_meta},
            response_format={"type": "json_object"},
        )
        dense_parsed = parse_verdict(raw, len(frames))
    except Exception as exc:  # noqa: BLE001 加密复核失败按未判定不拦，原判保留
        return {**verdict, "dense_recheck": {"window": list(window), "error": f"{type(exc).__name__}: {exc}"[:300]}}
    dense_judged = evaluate_headcount_and_duplication(
        dense_parsed, allowed_headcount=verdict.get("allowed_headcount"), roster_names=verdict.get("roster_names") or [],
        seconds_for_index=lambda idx: start + (idx - 1) * DENSE_FRAME_INTERVAL_S,
    )
    confirmed = dense_judged["headcount_exceeded"] or dense_judged["character_duplicated"]
    record = {"window": list(window), "frames_checked": dense_parsed["frames_checked"], "confirmed": confirmed}
    if confirmed:
        return {**verdict, **dense_judged, "checked": True, "dense_recheck": record}
    return {**verdict, "dense_recheck": record}
