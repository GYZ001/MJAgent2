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

from app.db import _last_write_sql, _task_connections, _task_connections_lock, _thread_connections

_LOGGER = logging.getLogger(__name__)
_LAST_DUMP_AT = 0.0
DUMP_INTERVAL_S = 30.0


def _await_chain_frames(task: Any, limit: int = 12) -> list[str]:
    """沿 ``cr_await`` 一路走到任务此刻真正挂起的那个 await。
    ``Task.get_stack()`` 对挂起的协程只给最外层一帧（Python 文档如此约定），
    实测只能看到 ``task_body.py:42 _prepare_storyboard_assets_background`` 这种
    入口，定位不到握着事务的具体 await 点；逐层展开 ``cr_await``/``gi_yieldfrom``
    才能拿到整条链，最后一帧就是持锁时正在等的地方。
    """
    frames: list[str] = []
    try:
        node = task.get_coro()
        while node is not None and len(frames) < limit:
            frame = getattr(node, "cr_frame", None) or getattr(node, "gi_frame", None) \
                or getattr(node, "ag_frame", None)
            if frame is None:
                frames.append(repr(node)[:120])
                break
            frames.append(f"{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}")
            node = getattr(node, "cr_await", None) or getattr(node, "gi_yieldfrom", None) \
                or getattr(node, "ag_await", None)
    except Exception:  # noqa: BLE001 -- 诊断路径，任务状态随时会变
        pass
    return frames


def open_write_holders() -> list[dict[str, Any]]:
    """哪些 asyncio 任务的连接正处于未提交事务，附整条 await 链（最后一帧即挂起点）。"""
    holders: list[dict[str, Any]] = []
    with _task_connections_lock:
        items = list(_task_connections.items())
    for task, conn in items:
        try:
            if not conn.in_transaction:
                continue
        except sqlite3.ProgrammingError:  # 连接已关闭
            continue
        holders.append({
            "task": task.get_name(), "coro": repr(task.get_coro())[:160],
            "frames": _await_chain_frames(task),
            "last_sql": _last_write_sql.get(id(conn)),
        })
    with _task_connections_lock:
        thread_items = list(_thread_connections.items())
    for thread_id, conn in thread_items:
        try:
            if not conn.in_transaction:
                continue
        except sqlite3.ProgrammingError:
            continue
        holders.append({
            "task": f"thread-{thread_id}", "coro": "（线程局部连接，无协程栈）", "frames": [],
            "last_sql": _last_write_sql.get(id(conn)),
        })
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
            "写锁争用（%s）：任务 %s 握着未提交事务 %s\n    最后一条写语句：%s\n    %s",
            reason, holder["task"], holder["coro"], holder.get("last_sql") or "（未记录）",
            "\n    ".join(holder["frames"]) or "（无栈）",
        )


LONG_TRANSACTION_THRESHOLD_S = 8.0
_SEEN_OPEN: dict[str, float] = {}
_LAST_LONG_LOG: dict[str, float] = {}


def holders_older_than(now: float, seen: dict[str, float], holders: list[dict[str, Any]], threshold: float) -> list[tuple[dict[str, Any], float]]:
    """按「任务名」跨 tick 追踪未提交事务的持续时间；返回超过阈值的 (holder, 已持续秒数)。
    连续两次 tick 都在事务里才计龄——单次采样区分不了「刚开始」和「一直没提交」。"""
    current = {h["task"] for h in holders}
    for key in list(seen):
        if key not in current:
            seen.pop(key)
    aged: list[tuple[dict[str, Any], float]] = []
    for holder in holders:
        started = seen.setdefault(holder["task"], now)
        age = now - started
        if age >= threshold:
            aged.append((holder, age))
    return aged


async def long_transaction_watchdog(interval_s: float = 5.0, threshold_s: float = LONG_TRANSACTION_THRESHOLD_S) -> None:
    """每 interval 秒看一次哪些连接握着未提交事务；同一任务连续超过 threshold 秒就记
    [LONG_WRITE_TRANSACTION]（每任务 60 秒限频）。这是「写事务跨 await」在生产上的直接证据：
    2026-09-05 B 上 9 集并行起跑时事件循环反复冻结 30 秒，py-spy 只能看到等锁的一方。"""
    import asyncio

    while True:
        try:
            now = time.monotonic()
            for holder, age in holders_older_than(now, _SEEN_OPEN, open_write_holders(), threshold_s):
                if now - _LAST_LONG_LOG.get(holder["task"], 0.0) < 60.0:
                    continue
                _LAST_LONG_LOG[holder["task"]] = now
                _LOGGER.warning(
                    "[LONG_WRITE_TRANSACTION] 任务 %s 的未提交事务已持续 %.0f 秒 %s\n    最后一条写语句：%s\n    %s",
                    holder["task"], age, holder["coro"], holder.get("last_sql") or "（未记录）",
                    "\n    ".join(holder["frames"]) or "（无栈）",
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 诊断循环不得因自身异常退出
            _LOGGER.exception("write lock watchdog tick failed")
        await asyncio.sleep(interval_s)


def rollback_before_long_wait(conn: sqlite3.Connection, where: str) -> None:
    """长等待（出图信号量、供应商调用）之前把任务连接上还开着的事务回滚掉，并记一条告警。
    2026-09-05 B 实测：场景库出图任务在某次异常后带着未提交的 scene_reference_views 插入
    去等图片生成信号量，一等 5 分钟，写锁把 9 个并行映射台全部拖成 database is locked。
    这里的回滚是 fail-closed：半截状态不提交、锁不带进长等待；告警里的 ``where`` 用来定位
    是哪条路径把事务留开了。"""
    try:
        if conn.in_transaction:
            conn.rollback()
            _LOGGER.warning("[OPEN_TXN_BEFORE_WAIT] %s：长等待前发现未提交事务，已回滚（最后写语句：%s）",
                            where, str(_last_write_sql.get(id(conn)) or "")[:160])
    except sqlite3.ProgrammingError:
        pass
