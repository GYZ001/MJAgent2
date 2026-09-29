"""ffmpeg/ffprobe 子进程小工具：本包内多个模块共用，避免各自重复拼子进程调用。

不从 ``app.media_exec.concat``/``app.final_edit`` 借用同名私有函数——那两个
模块分别是 L5/L4，本包是 L4，向 L5 反向 import 会是禁止的上行边（见
``app/LAYERS.toml`` 本包声明的行内注释）；``app.final_edit`` 与本包同层，理论
上可以借，但它的 ``_run_ffmpeg`` 是模块私有名字，另起一份同样十行的小函数
比跨包借私有符号更不脆弱。
"""
from __future__ import annotations

import json
import subprocess

from app.media_pipeline.delivery_encode import low_priority

_PROBE_TIMEOUT_S = 30.0


def run_ffmpeg(command: list[str], *, timeout: float, context: str) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=timeout, preexec_fn=low_priority)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{context}超时") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[-1600:].strip()
        raise RuntimeError(f"{context}失败" + (f"：{detail}" if detail else "")) from exc
    except OSError as exc:
        raise RuntimeError(f"{context}无法执行：{exc}") from exc


def probe_duration_s(path: str) -> float:
    """容器时长（秒）；探测失败抛 ``ValueError``（调用方决定是否降级）。"""
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(f"ffprobe 探测时长失败：{exc}") from exc
    try:
        duration = float(json.loads(completed.stdout or "{}").get("format", {}).get("duration"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"ffprobe 未返回有效时长：{exc}") from exc
    if duration <= 0:
        raise ValueError(f"ffprobe 返回非正时长：{duration!r}")
    return duration


def has_audio_stream(path: str) -> bool:
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
             "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
        streams = json.loads(completed.stdout or "{}").get("streams") or []
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(streams)
