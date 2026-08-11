"""持久化命令幂等存储（PRD §6.5）。

只在调用方显式提供 ``idempotency_key`` 时生效；禁止按参数自动生成永久键，
否则合法重跑（如从第 7 镜 resume）会误命中旧结果。
结果写入 SQLite，带 TTL，进程重启后仍可去重，过期后允许再次执行。

并发保护：执行前原子 claim ``running`` 槽位，避免双请求同时穿透缓存重复付费。
"""
from __future__ import annotations

import json
import time

from app.capabilities.schemas import CommandResult, CommandStatus
from app.db import get_conn

# 付费/创建类结果默认保留 24h，足以覆盖断线重试，又不永久锁死合法重跑。
DEFAULT_TTL_S = 24 * 60 * 60

_TERMINAL_OK = {CommandStatus.ACCEPTED, CommandStatus.SUCCEEDED}
_RUNNING = "running"


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
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(command_idempotency)")}
    if "request_fingerprint" not in columns:
        conn.execute(
            "ALTER TABLE command_idempotency ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    if "claim_token" not in columns:
        conn.execute(
            "ALTER TABLE command_idempotency ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_command_idempotency_expires "
        "ON command_idempotency(expires_at)"
    )


def _parse_result(row) -> CommandResult | None:
    try:
        payload = json.loads(row["result_json"])
        return CommandResult.model_validate(payload)
    except Exception:  # noqa: BLE001 损坏记录视为未命中
        return None


def _request_mismatch(command: str) -> CommandResult:
    return CommandResult(
        status=CommandStatus.REJECTED,
        summary="相同 idempotency_key 已绑定不同的命令参数",
        command=command,
        error_code="idempotency_request_mismatch",
    )


def lookup(idem_key: str, *, request_fingerprint: str) -> CommandResult | None:
    if not idem_key:
        return None
    conn = get_conn()
    _ensure_table(conn)
    now = time.time()
    conn.execute("DELETE FROM command_idempotency WHERE expires_at < ?", (now,))
    row = conn.execute(
        """SELECT command,status,result_json,expires_at,request_fingerprint
             FROM command_idempotency WHERE idem_key=?""",
        (idem_key,),
    ).fetchone()
    conn.commit()
    if not row:
        return None
    if float(row["expires_at"]) < now:
        return None
    if str(row["request_fingerprint"] or "") != request_fingerprint:
        return _request_mismatch(str(row["command"] or ""))
    if row["status"] == _RUNNING:
        return CommandResult(
            status=CommandStatus.ACCEPTED,
            summary="相同幂等键的命令正在执行中",
            command="",
            error_code="idempotency_in_progress",
            data={"idempotency_in_progress": True},
        )
    return _parse_result(row)


def claim(
    idem_key: str,
    *,
    command: str,
    request_fingerprint: str,
    claim_token: str,
    ttl_s: int = DEFAULT_TTL_S,
) -> CommandResult | None:
    """原子占用执行槽。返回已缓存/进行中结果；返回 None 表示本调用方获得执行权。"""
    if not idem_key:
        return None
    conn = get_conn()
    _ensure_table(conn)
    now = time.time()
    conn.execute("DELETE FROM command_idempotency WHERE expires_at < ?", (now,))
    existing = conn.execute(
        """SELECT command,status,result_json,expires_at,request_fingerprint
             FROM command_idempotency WHERE idem_key=?""",
        (idem_key,),
    ).fetchone()
    if existing and float(existing["expires_at"]) >= now:
        if str(existing["request_fingerprint"] or "") != request_fingerprint:
            conn.commit()
            return _request_mismatch(str(existing["command"] or command))
        if existing["status"] == _RUNNING:
            conn.commit()
            return CommandResult(
                status=CommandStatus.ACCEPTED,
                summary="相同幂等键的命令正在执行中",
                command=command,
                error_code="idempotency_in_progress",
                data={"idempotency_in_progress": True},
            )
        parsed = _parse_result(existing)
        conn.commit()
        if parsed is not None:
            return parsed
        # 损坏记录：删掉后重新占用
        conn.execute("DELETE FROM command_idempotency WHERE idem_key=?", (idem_key,))
    placeholder = CommandResult(
        status=CommandStatus.ACCEPTED,
        summary="执行中",
        command=command,
        data={"idempotency_in_progress": True},
    )
    try:
        conn.execute(
            """
            INSERT INTO command_idempotency(
                idem_key,command,status,result_json,created_at,expires_at,
                request_fingerprint,claim_token
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                idem_key,
                command,
                _RUNNING,
                json.dumps(placeholder.model_dump(mode="json"), ensure_ascii=False, default=str),
                now,
                now + max(60, ttl_s),
                request_fingerprint,
                claim_token,
            ),
        )
        conn.commit()
        return None
    except Exception:  # noqa: BLE001 并发插入冲突 → 回读
        conn.rollback()
        return lookup(idem_key, request_fingerprint=request_fingerprint) or CommandResult(
            status=CommandStatus.ACCEPTED,
            summary="相同幂等键的命令正在执行中",
            command=command,
            error_code="idempotency_in_progress",
            data={"idempotency_in_progress": True},
        )


def release_if_running(
    idem_key: str,
    *,
    request_fingerprint: str,
    claim_token: str,
) -> None:
    """执行失败/非成功终态时释放 running 槽，允许同键重试。"""
    if not idem_key:
        return
    conn = get_conn()
    _ensure_table(conn)
    conn.execute(
        """DELETE FROM command_idempotency
            WHERE idem_key=? AND status=? AND request_fingerprint=? AND claim_token=?""",
        (idem_key, _RUNNING, request_fingerprint, claim_token),
    )
    conn.commit()


def store(
    idem_key: str,
    *,
    command: str,
    request_fingerprint: str,
    claim_token: str,
    result: CommandResult,
    ttl_s: int = DEFAULT_TTL_S,
) -> None:
    if not idem_key or result.status not in _TERMINAL_OK:
        return
    conn = get_conn()
    _ensure_table(conn)
    now = time.time()
    updated = conn.execute(
        """
        UPDATE command_idempotency
           SET status=?,result_json=?,created_at=?,expires_at=?
         WHERE idem_key=? AND command=? AND status=?
           AND request_fingerprint=? AND claim_token=?
        """,
        (
            result.status.value,
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str),
            now,
            now + max(60, ttl_s),
            idem_key,
            command,
            _RUNNING,
            request_fingerprint,
            claim_token,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise RuntimeError("幂等命令 receipt 已被新 owner 接管，拒绝迟到结果覆盖")
    conn.commit()


def clear_for_tests() -> None:
    conn = get_conn()
    _ensure_table(conn)
    conn.execute("DELETE FROM command_idempotency")
    conn.commit()


def make_key(command: str, raw_key: str) -> str:
    return f"{command}:{raw_key}"
