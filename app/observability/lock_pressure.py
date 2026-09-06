"""SQLite 写锁争用作为机器水位信号（2026-09-06 第 13 轮：写锁风暴让心跳写不进、watchdog 误收口 7 集生成台）。

内存/磁盘/CPU 水位管不到「同一个 SQLite 文件上有多少写者在排队」——30 集同时进生成台时，
provider_call_progress / persist_progress / resolve_session 一分钟里十几次 database is locked。
这里只做一件事：把每次写锁争用记一个时间戳，准入闸在最近 ``WINDOW_S`` 秒内争用次数 ≥
``THRESHOLD`` 时不再放新调用进来（已在跑的不动），让写者自然退潮。判据来自本进程真实撞锁
次数，不是配置里的猜测；退潮后自动放开。
"""
from __future__ import annotations

import collections
import threading
import time

WINDOW_S = 60.0
THRESHOLD = 5
_events: collections.deque[float] = collections.deque(maxlen=512)
_lock = threading.Lock()


def note_lock_contention() -> None:
    """一次真实的写锁争用（database is locked / 等锁超时）。"""
    with _lock:
        _events.append(time.monotonic())


def recent_contention_count(now: float | None = None) -> int:
    cutoff = (now if now is not None else time.monotonic()) - WINDOW_S
    with _lock:
        return sum(1 for stamp in _events if stamp >= cutoff)


def lock_pressure_reason() -> str | None:
    count = recent_contention_count()
    if count >= THRESHOLD:
        return f"SQLite 写锁争用 {count} 次/{int(WINDOW_S)}s ≥ {THRESHOLD}"
    return None
