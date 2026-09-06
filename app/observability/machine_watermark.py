"""机器水位准入：并发以机器性能为上限，不靠人拍数字（2026-09-05 用户拍板）。

三个信号都从 /proc 取，不依赖 psutil（B 上没装）：
- 内存占用率：``(MemTotal - MemAvailable) / MemTotal``；
- 磁盘 IO 利用率：数据目录所在设备在 ``/proc/diskstats`` 里的 ``io_ticks``（毫秒）两次采样之差
  除以墙钟间隔——就是 iostat 的 %util；首次采样没有前值时按 0 处理；
- CPU：1 分钟负载除以核数。

阈值走设置台（``admission_memory_pct`` 70、``admission_disk_io_pct`` 70、
``admission_cpu_load_ratio`` 0.8）。任一超过就 ``throttle_reason()`` 非空，各准入点
（连播台集槽位、视频提交槽位、文本调用槽位）在放新并发前看一眼；已在跑的不受影响。
非 Linux / 读不到 /proc 时返回 None，调用方按「这次不检查」处理，不得当成健康。
采样带 2 秒缓存，准入点高频调用也不会反复读文件。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from app.db import get_setting

SETTING_MEMORY_PCT = "admission_memory_pct"
SETTING_DISK_IO_PCT = "admission_disk_io_pct"
SETTING_CPU_LOAD_RATIO = "admission_cpu_load_ratio"
DEFAULT_MEMORY_PCT = 70.0
DEFAULT_DISK_IO_PCT = 70.0
DEFAULT_CPU_LOAD_RATIO = 0.8
SAMPLE_TTL_S = 2.0
_MEMINFO = "/proc/meminfo"
_DISKSTATS = "/proc/diskstats"
_LOADAVG = "/proc/loadavg"


@dataclass
class Watermark:
    memory_pct: float | None = None
    disk_io_pct: float | None = None
    cpu_load_ratio: float | None = None
    sampled_at: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def throttled(self) -> bool:
        return bool(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_pct": self.memory_pct, "disk_io_pct": self.disk_io_pct,
            "cpu_load_ratio": self.cpu_load_ratio, "throttled": self.throttled, "reasons": list(self.reasons),
        }


def _threshold(key: str, default: float) -> float:
    try:
        raw = str(get_setting(key) or "").strip()
        return float(raw) if raw else default
    except Exception:  # noqa: BLE001 设置表未就绪/非法值都按默认阈值
        return default


def memory_pct_from_meminfo(text: str) -> float | None:
    total = available = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            available = int(line.split()[1])
    if not total or available is None:
        return None
    return round(100.0 * (total - available) / total, 1)


def io_ticks_from_diskstats(text: str, major: int, minor: int) -> int | None:
    """``/proc/diskstats`` 第 13 列是该设备累计忙碌毫秒数（io_ticks）。"""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 13 and parts[0] == str(major) and parts[1] == str(minor):
            return int(parts[12])
    return None


def cpu_load_ratio_from_loadavg(text: str, cpu_count: int) -> float | None:
    try:
        return round(float(text.split()[0]) / max(1, cpu_count), 2)
    except (ValueError, IndexError):
        return None


class MachineWatermark:
    def __init__(self, data_dir: str | None = None) -> None:
        self._data_dir = data_dir
        self._last_ticks: tuple[float, int] | None = None
        self._cached: Watermark | None = None

    def _device(self) -> tuple[int, int] | None:
        try:
            from app import config

            path = self._data_dir or str(config.PROJECTS_DIR)
            dev = os.stat(path).st_dev
            return os.major(dev), os.minor(dev)
        except Exception:  # noqa: BLE001 目录不存在/非 Linux 都当没有磁盘信号
            return None

    def _read(self, path: str) -> str | None:
        try:
            with open(path, encoding="ascii") as fh:
                return fh.read()
        except OSError:
            return None

    def sample(self, *, now_ts: float | None = None) -> Watermark:
        stamp = time.time() if now_ts is None else now_ts
        if self._cached is not None and stamp - self._cached.sampled_at < SAMPLE_TTL_S:
            return self._cached
        wm = Watermark(sampled_at=stamp)
        meminfo = self._read(_MEMINFO)
        wm.memory_pct = memory_pct_from_meminfo(meminfo) if meminfo else None
        loadavg = self._read(_LOADAVG)
        wm.cpu_load_ratio = cpu_load_ratio_from_loadavg(loadavg, os.cpu_count() or 1) if loadavg else None
        device = self._device()
        diskstats = self._read(_DISKSTATS) if device else None
        ticks = io_ticks_from_diskstats(diskstats, *device) if diskstats and device else None
        if ticks is not None:
            if self._last_ticks is not None and stamp > self._last_ticks[0]:
                elapsed_ms = (stamp - self._last_ticks[0]) * 1000.0
                wm.disk_io_pct = round(min(100.0, 100.0 * max(0, ticks - self._last_ticks[1]) / elapsed_ms), 1)
            else:
                wm.disk_io_pct = 0.0
            self._last_ticks = (stamp, ticks)
        wm.reasons = self._reasons(wm)
        self._cached = wm
        return wm

    @staticmethod
    def _reasons(wm: Watermark) -> list[str]:
        reasons: list[str] = []
        mem_cap = _threshold(SETTING_MEMORY_PCT, DEFAULT_MEMORY_PCT)
        io_cap = _threshold(SETTING_DISK_IO_PCT, DEFAULT_DISK_IO_PCT)
        cpu_cap = _threshold(SETTING_CPU_LOAD_RATIO, DEFAULT_CPU_LOAD_RATIO)
        if wm.memory_pct is not None and wm.memory_pct >= mem_cap:
            reasons.append(f"内存占用 {wm.memory_pct:.0f}% ≥ {mem_cap:.0f}%")
        if wm.disk_io_pct is not None and wm.disk_io_pct >= io_cap:
            reasons.append(f"磁盘 IO {wm.disk_io_pct:.0f}% ≥ {io_cap:.0f}%")
        if wm.cpu_load_ratio is not None and wm.cpu_load_ratio >= cpu_cap:
            reasons.append(f"CPU 负载/核 {wm.cpu_load_ratio:.2f} ≥ {cpu_cap:.2f}")
        return reasons


#: 进程内唯一采样器；测试通过 monkeypatch 替换本模块的 throttle_reason。
_sampler = MachineWatermark()


def current() -> Watermark:
    return _sampler.sample()


def throttle_reason() -> str | None:
    """超过任一水位时返回可读原因（准入点据此拒绝放新并发），否则 None。"""
    wm = current()
    return "；".join(wm.reasons) if wm.reasons else None


def snapshot() -> dict[str, Any]:
    return current().as_dict()


async def wait_until_admitted(poll_s: float = 2.0) -> None:
    """异步准入点用：水位超标就每 poll_s 秒看一次，直到回落。"""
    import asyncio

    while throttle_reason() is not None:
        await asyncio.sleep(poll_s)
