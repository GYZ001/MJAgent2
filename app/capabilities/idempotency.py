"""持久化命令幂等存储（PRD §6.5）。

只在调用方显式提供 ``idempotency_key`` 时生效；禁止按参数自动生成永久键，
否则合法重跑（如从第 7 镜 resume）会误命中旧结果。
结果写入 SQLite，带 TTL，进程重启后仍可去重，过期后允许再次执行。
"""
from __future__ import annotations

import json
import time

from app.capabilities.schemas import CommandResult, CommandStatus
from app.db import get_conn

# 付费/创建类结果默认保留 24h，足以覆盖断线重试，又不永久锁死合法重跑。
DEFAULT_TTL_S = 24 * 60 * 60

_TERMINAL_OK = {CommandStatus.ACCEPTED, CommandStatus.SUCCEEDED}


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS command_idempotency (
            idem_key TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_command_idempotency_expires "
        "ON command_idempotency(expires_at)"
    )


def lookup(idem_key: str) -> CommandResult | None:
    if not idem_key:
        return None
    conn = get_conn()
    _ensure_table(conn)
    now = time.time()
    conn.execute("DELETE FROM command_idempotency WHERE expires_at < ?", (now,))
    row = conn.execute(
        "SELECT result_json, expires_at FROM command_idempotency WHERE idem_key=?",
        (idem_key,),
    ).fetchone()
    conn.commit()
    if not row:
        return None
    if float(row["expires_at"]) < now:
        return None
    try:
        payload = json.loads(row["result_json"])
        return CommandResult.model_validate(payload)
    except Exception:  # noqa: BLE001 损坏记录视为未命中
        return None


def store(idem_key: str, *, command: str, result: CommandResult, ttl_s: int = DEFAULT_TTL_S) -> None:
    if not idem_key or result.status not in _TERMINAL_OK:
        return
    conn = get_conn()
    _ensure_table(conn)
    now = time.time()
    conn.execute(
        """
        INSERT INTO command_idempotency(idem_key, command, status, result_json, created_at, expires_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(idem_key) DO UPDATE SET
            status=excluded.status,
            result_json=excluded.result_json,
            created_at=excluded.created_at,
            expires_at=excluded.expires_at
        """,
        (
            idem_key,
            command,
            result.status.value,
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str),
            now,
            now + max(60, ttl_s),
        ),
    )
    conn.commit()


def clear_for_tests() -> None:
    conn = get_conn()
    _ensure_table(conn)
    conn.execute("DELETE FROM command_idempotency")
    conn.commit()


def make_key(command: str, raw_key: str) -> str:
    return f"{command}:{raw_key}"
