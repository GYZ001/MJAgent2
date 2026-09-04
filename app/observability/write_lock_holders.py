"""写锁争用诊断：点名握着未提交事务（即可能握着 SQLite 写锁）的 asyncio 任务。

连接按 asyncio 任务局部登记（``app.db._task_connections``），所以能直接列出「谁在
await 里握着写事务」——py-spy 只看得到线程栈，挂起的协程在任何线程栈上都不出现。
2026-09-04 B 实测主线程卡在流式心跳写入等写锁、整个事件循环冻结，就是靠这一类
线索才能定位到持锁方。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from app.db import _task_connections, _task_connections_lock

_LOGGER = logging.getLogger(__name__)
_LAST_DUMP_AT = 0.0
DUMP_INTERVAL_S = 30.0


def open_write_holders() -> list[dict[str, Any]]:
    """哪些 asyncio 任务的连接正处于未提交事务，附最近 8 帧协程栈。"""
    holders: list[dict[str, Any]] = []
    with _task_connections_lock:
        items = list(_task_connections.items())
    for task, conn in items:
        try:
            if not conn.in_transaction:
                continue
        except sqlite3.ProgrammingError:  # 连接已关闭
            continue
        frames: list[str] = []
        try:
            for frame in task.get_stack(limit=8):
                frames.append(f"{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}")
        except Exception:  # noqa: BLE001 -- 诊断路径，任务状态随时会变
            pass
        holders.append({"task": task.get_name(), "coro": repr(task.get_coro())[:160], "frames": frames})
    return holders


def log_open_write_holders(reason: str) -> None:
    """写锁争用时（限频 30 秒一次）把持锁任务连同协程栈写进日志。"""
    global _LAST_DUMP_AT
    stamp = time.monotonic()
    if stamp - _LAST_DUMP_AT < DUMP_INTERVAL_S:
        return
    _LAST_DUMP_AT = stamp
    holders = open_write_holders()
    if not holders:
        _LOGGER.warning("写锁争用（%s）：进程内没有任务连接处于未提交事务，锁在别处（线程局部连接或其它进程）", reason)
        return
    for holder in holders:
        _LOGGER.warning(
            "写锁争用（%s）：任务 %s 握着未提交事务 %s\n    %s",
            reason, holder["task"], holder["coro"], "\n    ".join(holder["frames"]) or "（无栈）",
        )
