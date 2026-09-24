"""快速拼接（draft_concat）路径的分段级 ffmpeg 支持：逐镜编码参数决策
（``piece_video_args``/``run_concat_demuxer``，从 ``app.media_exec.concat`` 原地
搬迁，行为不变）与 AI 标识首段重编码（``relabeled_prefix``，新增）。

新逻辑放在这里而不是 ``concat.py`` 本体，是因为该文件行数已顶到棘轮基线
（``app/FILE_CONVENTIONS.toml`` 的 line_count 零余量）——外移与压缩配合抵消行数，
不是随意拆分。``concat.py`` 仍是唯一调用方，两个搬迁函数保持模块级导入回去，
``monkeypatch.setattr(concat_mod, "_run_concat_demuxer", ...)`` 这类既有测试
patch 的是 concat.py 自己的模块属性，行为不受搬迁影响。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.media_pipeline.delivery_encode import (
    DELIVERY_VIDEO_ARGS, INTERMEDIATE_VIDEO_ARGS, canvas_filter, low_priority,
)

#: AI 标识必须覆盖片头至少这么多秒——与 app.subtitles.ass.AI_LABEL_DURATION_S 同值，
#: 但这里只需要一个纯数字做前缀分段的覆盖判断，不必为此额外依赖 app.subtitles。
LABEL_COVERAGE_S = 3.0


def _piece_video_args(rate: float, speed_change: bool, needs_scale: bool, width: int, height: int) -> list[str]:
    """不变速且分辨率已一致 → -c:v copy（无损最快，旧 720p 集保持 720p，放大不
    会凭空多出细节）；否则按需 setpts/canvas_filter 后走 INTERMEDIATE_VIDEO_ARGS。
    """
    if not speed_change and not needs_scale:
        return ["-c:v", "copy"]
    parts = [f"setpts=PTS/{rate:.6f}"] if speed_change else []
    if needs_scale:
        parts.append(canvas_filter(width, height))
    return ["-vf", ",".join(parts), *INTERMEDIATE_VIDEO_ARGS]


def _run_concat_demuxer(
    concat_in: list[str], concat_output: Path, timeout_s: float,
    uniform_ok: bool, audio_rate: int, *, ass_filter: str | None = None,
) -> None:
    """分辨率一致且不烧字幕才尝试 -c copy 无损直粘；否则回退 DELIVERY_VIDEO_ARGS
    全量重编码（不再用中间件参数顶替最终交付）；ass_filter 非空时插在最前。
    """
    if uniform_ok and ass_filter is None:
        try:
            subprocess.run(
                concat_in + ["-c", "copy", "-movflags", "+faststart", str(concat_output)],
                check=True, capture_output=True, timeout=timeout_s, preexec_fn=low_priority)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    vf_args = ["-vf", ass_filter] if ass_filter else []
    try:
        subprocess.run(
            concat_in + [*vf_args, *DELIVERY_VIDEO_ARGS, "-c:a", "aac", "-ar", str(audio_rate),
                         "-movflags", "+faststart", str(concat_output)],
            check=True, capture_output=True, timeout=timeout_s, preexec_fn=low_priority)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"整集合成超过 {int(timeout_s)} 秒，已停止本次任务；上一版成片仍保留，可稍后重试") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
        raise ValueError("整集合成失败，上一版成片仍保留，可检查片段后重试" + (f"：{detail}" if detail else "")) from exc


def _burn_label(source: Path, destination: Path, filter_arg: str, *, timeout_s: float) -> None:
    """对单个已归一分段叠 AI 标识 ASS 并按 INTERMEDIATE_VIDEO_ARGS 重编码——编码
    参数与 ``_piece_video_args`` 的重编码分支一致，使其余 ``-c copy`` 分段仍能
    与它在同一个 concat demuxer 结果里正确衔接（见模块 docstring）。
    """
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
             "-vf", filter_arg, *INTERMEDIATE_VIDEO_ARGS, "-c:a", "copy",
             "-movflags", "+faststart", str(destination)],
            check=True, capture_output=True, timeout=timeout_s, preexec_fn=low_priority,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("AI 标识首段重编码超时；上一版成片仍保留，可稍后重试") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
        raise ValueError("AI 标识首段重编码失败；上一版成片仍保留" + (f"：{detail}" if detail else "")) from exc
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise ValueError("AI 标识首段重编码未产出有效片段；上一版成片仍保留")


def relabeled_prefix(
    piece_paths: list[Path], piece_durations: list[float], filter_arg: str, *, timeout_s: float,
) -> list[Path]:
    """只重编码覆盖片头 ``LABEL_COVERAGE_S`` 秒所需的前缀分段（通常是第 1 段，
    短于 3 秒时顺延到第 2/3…段），其余分段路径原样返回、仍可 -c copy。

    ``piece_paths``/``piece_durations`` 一一对应、按拼接顺序、非空；``filter_arg``
    是 ``app.subtitles.ass.ffmpeg_ass_filter`` 产出的 ``ass=...:fontsdir=...``。
    """
    covered, prefix_count = 0.0, 0
    for duration in piece_durations:
        prefix_count += 1
        covered += duration
        if covered >= LABEL_COVERAGE_S:
            break
    out_paths = list(piece_paths)
    for index in range(prefix_count):
        source = piece_paths[index]
        relabeled = source.with_name(f"{source.stem}-label{source.suffix}")
        _burn_label(source, relabeled, filter_arg, timeout_s=timeout_s)
        out_paths[index] = relabeled
    return out_paths


__all__ = [name for name in globals() if not name.startswith("__")]
