"""供应商流式调用的心跳写入（provider_calls.first_chunk_at/last_chunk_at/received_chars）。

从 ``app.db`` 挪出：它跑在事件循环线程上，是纯诊断写入，**绝不能为了等写锁把整个
事件循环卡住**。2026-09-04 B 实测并行三集时主线程在这里按 30 秒的 busy_timeout
反复等锁，循环整体冻结、网站打不开。现在 busy_timeout 临时归零：锁住就放弃这一拍，
并把持锁任务记进日志（见 ``write_lock_holders``）。
"""
from __future__ import annotations

import sqlite3

from app.db import _is_transient_sqlite_lock, get_conn, now
from app.observability.write_lock_holders import log_open_write_holders


def update_provider_call_progress(
    call_id: int,
    *,
    received_chars: int,
    chunk_at: float | None = None,
) -> None:
    """Persist bounded stream heartbeat data without affecting business flow."""
    if not call_id:
        return
    stamp = float(chunk_at or now())
    conn = get_conn()
    conn.execute("PRAGMA busy_timeout=0")
    try:
        conn.execute(
            """UPDATE provider_calls
                  SET first_chunk_at=COALESCE(first_chunk_at,?),
                      last_chunk_at=?,
                      received_chars=MAX(received_chars,?)
                WHERE id=? AND status='RUNNING'""",
            (stamp, stamp, max(0, int(received_chars)), call_id),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if _is_transient_sqlite_lock(exc):
            log_open_write_holders("provider_call_progress")
    finally:
        conn.execute("PRAGMA busy_timeout=30000")
