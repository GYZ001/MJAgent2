"""operation_audit / user_activity 的表结构与独立连接写入原语。

app/db.py 已经零行余量（3573/3573 基线），两张新表不走 app.db_schema 注册，
改由本模块自己 lazy 建表——``ensure_schema()`` 按当前 ``db.DB_PATH`` 幂等记忆，
main.py 的 lifespan 在 ``init_db()`` 之后显式调一次，写入/查询前也各自兜底调用
一次（多数测试不经 lifespan，必须靠这条兜底）。

写入复用 ``app.db._run_write_transaction_once``（独立连接 + ``timeout=0`` +
``BEGIN IMMEDIATE``，见该函数 docstring）：绝不在调用方持有的 ``get_conn()``
连接上 commit——业务事务中途调用时那样做会把半途状态一起提交进库（CLAUDE.md
「不得在调用方的连接上隐式提交」，已记录三次真实事故）。写失败（多数是抢
不到写锁）落进 ``app.monitor_audit_buffer`` 新增的 operation_audit 通道，由
已有的 ``monitor_audit_flush_loop`` 定期补写。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app import db, monitor_audit_buffer

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS operation_audit (
  id TEXT PRIMARY KEY, ts REAL NOT NULL,
  user_id TEXT, username TEXT, is_system_admin INTEGER,
  source TEXT NOT NULL,
  event TEXT NOT NULL,
  event_label TEXT,
  method TEXT, path TEXT,
  project_id TEXT, episode_id TEXT, target TEXT,
  outcome TEXT NOT NULL,
  http_status INTEGER, error_id TEXT, error_code TEXT,
  summary TEXT, duration_ms INTEGER, ip TEXT, user_agent TEXT,
  args_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_operation_audit_ts ON operation_audit(ts);
CREATE INDEX IF NOT EXISTS idx_operation_audit_user_ts ON operation_audit(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_operation_audit_event_ts ON operation_audit(event, ts);
CREATE INDEX IF NOT EXISTS idx_operation_audit_project_ts ON operation_audit(project_id, ts);
CREATE TABLE IF NOT EXISTS user_activity (
  user_id TEXT PRIMARY KEY, last_active_at REAL NOT NULL, last_path TEXT
);
"""

_INSERT_COLUMNS = (
    "id", "ts", "user_id", "username", "is_system_admin", "source", "event",
    "event_label", "method", "path", "project_id", "episode_id", "target",
    "outcome", "http_status", "error_id", "error_code", "summary",
    "duration_ms", "ip", "user_agent", "args_json",
)
_INSERT_SQL = (
    "INSERT INTO operation_audit(" + ",".join(_INSERT_COLUMNS) + ") VALUES("
    + ",".join(f":{c}" for c in _INSERT_COLUMNS) + ")"
)

_ensured_paths: set[str] = set()


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。

    ``db.DB_PATH`` 而不是 ``app.config.DB_PATH``：测试隔离下前者才是各模块
    实际用来开连接的、被 conftest 逐测试覆盖的那个绑定（见 tests/conftest.py
    ``_restore_isolated_runtime``），键上它才能保证每个测试独占的新库都会
    重新建表。
    """
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def operation(conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_DDL)

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞调用方
        return
    _ensured_paths.add(key)


def insert_operation_audit_row(row: dict[str, Any]) -> None:
    """独立连接写一行；抢不到写锁或其它失败一律落本地缓冲，绝不上抛。"""
    ensure_schema()

    def operation(conn: sqlite3.Connection) -> None:
        conn.execute(_INSERT_SQL, row)

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 审计写入失败不能拖垮业务请求/命令执行
        monitor_audit_buffer.append_operation_audit(row)


def upsert_user_activity(user_id: str, ts: float, path: str | None) -> None:
    ensure_schema()

    def operation(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO user_activity(user_id, last_active_at, last_path) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_active_at=excluded.last_active_at, "
            "last_path=excluded.last_path",
            (user_id, ts, path),
        )

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 活跃度不是关键路径，静默跳过，下次请求再写
        pass


def delete_expired_operation_audit(cutoff_ts: float, batch_size: int) -> int:
    """分批删除 ``ts < cutoff_ts`` 的行，返回本次实际删除行数；失败返回 0，下一轮重试。"""
    ensure_schema()

    def operation(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "DELETE FROM operation_audit WHERE id IN "
            "(SELECT id FROM operation_audit WHERE ts < ? LIMIT ?)",
            (cutoff_ts, batch_size),
        )
        return cur.rowcount

    try:
        return db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 保留期巡检失败不影响下一轮，也不阻塞调用方
        return 0
