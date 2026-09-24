"""参考音频裁片与响度归一：纯同步 ffmpeg 子进程调用。

调用方必须用 ``asyncio.to_thread`` 包裹，绝不允许直接在事件循环里跑——本仓库
已因同步合成冻结过整个后端（CLAUDE.md「合成曾冻结整个后端」），ffmpeg 子进程
调用是同一类地雷。

裁片规则（探针实测，见
docs/角色固定音色_声音生成接口调研与实施方案_2026-09-23.md §10）：用
``silencedetect`` 找停顿，在 5 秒内最后一个停顿处截断；再用「测响度 → 算线性
增益 → volume+alimiter」两步把响度归一到约 -20dBFS，不用两遍分析的
``loudnorm`` 动态算法（避免过度处理把音色特征也磨平）。
"""
from __future__ import annotations

import re
import subprocess
import wave
from pathlib import Path

MAX_CLIP_S = 5.0
MIN_CLIP_S = 2.0
SILENCE_DB = "-35dB"
SILENCE_MIN_D = 0.18
TARGET_DBFS = -20.0
LIMITER_LIMIT = 0.95
FFMPEG_TIMEOUT_S = 30.0

_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


class VoiceClipError(Exception):
    """裁片/响度归一失败，面向界面的中文原因。"""


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, check=False,
        )
    except FileNotFoundError as exc:
        raise VoiceClipError("未找到 ffmpeg 可执行文件；请安装 ffmpeg 并确保在 PATH 中") from exc
    except subprocess.TimeoutExpired as exc:
        raise VoiceClipError("ffmpeg 处理超时") from exc
    return proc.stderr or ""


def _probe_duration_and_pauses(source: Path) -> tuple[float, list[float]]:
    stderr = _run([
        "ffmpeg", "-nostdin", "-i", str(source),
        "-af", f"silencedetect=noise={SILENCE_DB}:d={SILENCE_MIN_D}",
        "-f", "null", "-",
    ])
    duration_match = _DURATION_RE.search(stderr)
    if not duration_match:
        raise VoiceClipError("无法读取音频时长（ffmpeg 未返回 Duration）")
    hours, minutes, seconds = duration_match.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    pauses = [float(x) for x in _SILENCE_START_RE.findall(stderr)]
    return total, pauses


def _cut_point(total_s: float, pauses: list[float]) -> float:
    if total_s < MIN_CLIP_S:
        raise VoiceClipError(f"音频时长不足 {MIN_CLIP_S:.0f} 秒（实得 {total_s:.2f} 秒），无法裁出参考片段")
    candidates = [p for p in pauses if MIN_CLIP_S <= p <= MAX_CLIP_S]
    if candidates:
        return max(candidates)
    return min(total_s, MAX_CLIP_S)


def _mean_volume_db(source: Path) -> float:
    stderr = _run(["ffmpeg", "-nostdin", "-i", str(source), "-af", "volumedetect", "-f", "null", "-"])
    match = _MEAN_VOLUME_RE.search(stderr)
    if not match:
        raise VoiceClipError("无法测得音频响度（ffmpeg 未返回 mean_volume）")
    return float(match.group(1))


def _clip_duration_s(dest: Path) -> float:
    with wave.open(str(dest), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate() or 1)


def cut_and_normalize_clip(source: Path, dest: Path) -> float:
    """裁出 ``dest``（≤5 秒、≥2 秒，24kHz 单声道 wav，响度归一到约 -20dBFS）。

    返回裁出片段的时长（秒）；任何一步失败都抛 ``VoiceClipError``（中文原因），
    调用方据此把整条声音行标记为 failed，不产出残缺文件（fail closed）。
    """
    total_s, pauses = _probe_duration_and_pauses(source)
    cut_point = _cut_point(total_s, pauses)
    raw_cut = dest.with_suffix(".precut.wav")
    try:
        _run([
            "ffmpeg", "-nostdin", "-y", "-i", str(source), "-t", f"{cut_point:.3f}",
            "-ar", "24000", "-ac", "1", str(raw_cut),
        ])
        if not raw_cut.is_file() or raw_cut.stat().st_size == 0:
            raise VoiceClipError("ffmpeg 裁片未产出文件")
        gain_db = TARGET_DBFS - _mean_volume_db(raw_cut)
        _run([
            "ffmpeg", "-nostdin", "-y", "-i", str(raw_cut),
            "-af", f"volume={gain_db:.2f}dB,alimiter=limit={LIMITER_LIMIT}",
            "-ar", "24000", "-ac", "1", str(dest),
        ])
    finally:
        raw_cut.unlink(missing_ok=True)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise VoiceClipError("ffmpeg 响度归一未产出文件")
    duration = _clip_duration_s(dest)
    if duration < MIN_CLIP_S - 0.05:  # 浮点/编码误差容忍
        raise VoiceClipError(f"裁出的参考片段过短（{duration:.2f} 秒）")
    return duration
