"""SQLite 写锁争用作为机器水位信号（2026-09-06 第 13 轮：写锁风暴让心跳写不进、watchdog 误收口 7 集生成台）。

内存/磁盘/CPU 水位管不到「同一个 SQLite 文件上有多少写者在排队」——30 集同时进生成台时，
provider_call_progress / persist_progress / resolve_session 一分钟里十几次 database is locked。
这里只做一件事：把每次写锁争用记一个时间戳，准入闸在最近 ``WINDOW_S`` 秒内争用次数 ≥
``THRESHOLD`` 时不再放新调用进来（已在跑的不动），让写者自然退潮。判据来自本进程真实撞锁
次数，不是配置里的猜测；退潮后自动放开。

计的是**等满 busy_timeout 仍拿不到写锁**的失败（``app.db.run_write_transaction`` 的重试分支），
不计 ``provider_heartbeat`` 那种 busy_timeout=0 探针的瞬时碰撞——30 个写者并发时探针碰撞是常态，
按它计数会让准入闸在正常负载下常开。
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
    """一次真实的写锁争用（database is locked / 等锁超时）。

    EP-06 指标（``manju_db_write_lock_wait_seconds``，事件循环冻结事故的直接
    观测点）在这里顺带记一次等待时长观测：``app.db._run_write_transaction_once``
    以 ``timeout=WRITE_TXN_BUSY_TIMEOUT_S`` 打开连接，Python sqlite3 的忙等
    处理器只在拿到锁或等满这个 timeout 之后才返回——本函数被调用这一刻，说明
    刚刚正是等满了整段 timeout 才失败，所以可以如实把它当作这次等待的耗时，
    不是凭空估算。局限：只覆盖"等到超时仍失败"的这部分；等待后成功拿到锁的
    调用不经过这里，没有单独打点（app/db.py 不许碰，见 CLAUDE.md 派单约束），
    因此这个直方图是"至少这么久的失败等待"，不是全部写事务的排队耗时分布。
    """
    with _lock:
        _events.append(time.monotonic())
    # 延迟 import：app.db 反过来在模块级 import 本模块（记录争用事件），模块级
    # 互相 import 会成环；只取一个只读常量，延迟到函数体内没有任何功能损失。
    from app.db import WRITE_TXN_BUSY_TIMEOUT_S
    from app.observability import metrics_registry
    metrics_registry.record_db_write_lock_wait(WRITE_TXN_BUSY_TIMEOUT_S)


def recent_contention_count(now: float | None = None) -> int:
    cutoff = (now if now is not None else time.monotonic()) - WINDOW_S
    with _lock:
        return sum(1 for stamp in _events if stamp >= cutoff)


def lock_pressure_reason() -> str | None:
    count = recent_contention_count()
    if count >= THRESHOLD:
        return f"SQLite 写锁争用 {count} 次/{int(WINDOW_S)}s ≥ {THRESHOLD}"
    return None
