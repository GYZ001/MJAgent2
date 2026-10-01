"""启动时按保留期清理观测表（provider_calls / error_logs），从 app.db 拆出。

2026-10-01 B 机实测故障：部署后后端连续 95 次启动失败（``sqlite3.IntegrityError:
FOREIGN KEY constraint failed``，见 logs/backend.log），部署脚本判健康检查超时并回滚。
根因：provider_calls 有两条指回自身的外键（``supersedes_call_id`` /
``superseded_by_call_id``，NO ACTION），旧写法 ``DELETE ... WHERE ts < 截止``
会删掉一条刚过保留期、却仍被一条尚未过期的重试记录引用的旧调用——语句结束时
外键检查失败，整个 ``init_db`` 抛错，后端起不来。等引用它的那条也跨过截止线、
两条在同一条语句里一起删掉时冲突才消失（回滚后那次启动「恰好」成功），所以这
是一个随时间窗口反复出现的潜伏故障，任何一次重启都可能撞上。

修法两层：
1. 只删「过期且不被任何保留记录引用（含间接引用链）」的行：先从未过期/仍在运行
   的记录出发，沿两条自引用指针求闭包，闭包里的旧记录本次保留，等引用它的记录
   也过期后再一起删；其余过期记录在同一条 DELETE 里删除（分多条语句删会在中途
   触发外键检查）。
2. 清理是后台整理，不是启动前提：整段失败只记一条错误日志，不让后端起不来。
   这不是吞错——日志带完整堆栈，且清理下次启动会重试；而让整站因为整理旧日志
   失败而宕机，是把次要职责的故障放大成全站故障。
"""
from __future__ import annotations

import logging
import sqlite3
import time

log = logging.getLogger(__name__)

_POINTER_COLUMNS = ("supersedes_call_id", "superseded_by_call_id")


def _retention_days(conn: sqlite3.Connection, key: str, fallback: int) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        return max(1, int(row["value"] if row else fallback))
    except (TypeError, ValueError):
        return fallback


def _has_pointer_columns(conn: sqlite3.Connection) -> bool:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_calls)").fetchall()}
    return all(column in columns for column in _POINTER_COLUMNS)


def _referenced_expired_ids(conn: sqlite3.Connection, cutoff: float) -> set[int]:
    """被保留记录（未过期或仍在运行）直接或间接引用的过期记录 id——本次不能删。"""
    rows = conn.execute(
        "SELECT id, supersedes_call_id, superseded_by_call_id, "
        "(ts < ? AND status != 'RUNNING') AS expired FROM provider_calls "
        "WHERE supersedes_call_id IS NOT NULL OR superseded_by_call_id IS NOT NULL",
        (cutoff,),
    ).fetchall()
    pointers = {int(r[0]): [int(p) for p in (r[1], r[2]) if p is not None] for r in rows}
    frontier = [p for r in rows if not r[3] for p in pointers[int(r[0])]]
    keep: set[int] = set()
    while frontier:
        target = frontier.pop()
        if target in keep:
            continue
        keep.add(target)
        frontier.extend(pointers.get(target, ()))
    return keep


def _delete_expired_calls(conn: sqlite3.Connection, cutoff: float) -> None:
    if not _has_pointer_columns(conn):
        conn.execute("DELETE FROM provider_calls WHERE ts < ? AND status != 'RUNNING'", (cutoff,))
        return
    keep = _referenced_expired_ids(conn, cutoff)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _retention_keep(id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM temp._retention_keep")
    conn.executemany("INSERT OR IGNORE INTO temp._retention_keep(id) VALUES(?)", [(i,) for i in keep])
    conn.execute(
        "DELETE FROM provider_calls WHERE ts < ? AND status != 'RUNNING' "
        "AND id NOT IN (SELECT id FROM temp._retention_keep)",
        (cutoff,),
    )
    conn.execute("DELETE FROM temp._retention_keep")


def prune_observability_logs(conn: sqlite3.Connection) -> None:
    """Bound diagnostic tables so routine monitoring cannot grow the DB forever."""
    stamp = time.time()
    calls_cutoff = stamp - _retention_days(conn, "provider_call_retention_days", 30) * 86400
    errors_cutoff = stamp - _retention_days(conn, "error_log_retention_days", 30) * 86400
    try:
        _delete_expired_calls(conn, calls_cutoff)
        conn.execute("DELETE FROM error_logs WHERE ts < ?", (errors_cutoff,))
    except sqlite3.Error:
        log.error("[OBSERVABILITY_RETENTION_FAILED] 启动时清理过期观测记录失败，本次跳过、下次启动重试", exc_info=True)


__all__ = ["prune_observability_logs"]
