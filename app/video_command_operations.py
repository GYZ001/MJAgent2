"""Durable domain receipts for paid video generation commands.

The generic CommandBus receipt is deliberately short lived and its abandoned
``running`` claims are reclaimed at startup.  Paid generation therefore also
needs a domain receipt plus content-addressed plan/version keys so a retry after
an unknown HTTP response recovers the exact durable work.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.db import get_conn


class VideoCommandOperationConflict(ValueError):
    """The same operation key was presented for a different canonical request."""


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS video_command_operation_receipts (
            operation_key TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )


def claim_video_command_operation(
    *,
    command: str,
    idempotency_key: str,
    request_fingerprint: str,
    scope_type: str,
    scope_id: str,
) -> dict[str, Any] | None:
    """Claim or resume one operation; return its exact completed domain result."""
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_key:
        raise VideoCommandOperationConflict("视频命令缺少稳定幂等键")
    operation_key = f"{command}:{normalized_key}"
    conn = get_conn()
    _ensure_table(conn)
    stamp = time.time()
    try:
        conn.execute(
            """INSERT INTO video_command_operation_receipts(
                   operation_key,command,request_fingerprint,scope_type,scope_id,
                   status,result_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,'running','{}',?,?)""",
            (
                operation_key,
                command,
                request_fingerprint,
                scope_type,
                scope_id,
                stamp,
                stamp,
            ),
        )
        conn.commit()
        return None
    except Exception:  # noqa: BLE001 -- unique conflict is resolved by exact readback
        conn.rollback()
    row = conn.execute(
        """SELECT * FROM video_command_operation_receipts
           WHERE operation_key=?""",
        (operation_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError("视频命令 receipt 占用失败")
    if (
        str(row["command"]) != command
        or str(row["request_fingerprint"]) != request_fingerprint
        or str(row["scope_type"]) != scope_type
        or str(row["scope_id"]) != scope_id
    ):
        raise VideoCommandOperationConflict(
            "相同 idempotency_key 已绑定不同的视频生成请求"
        )
    if str(row["status"]) != "succeeded":
        # The generic bus prevents live concurrent owners.  Seeing ``running``
        # here means startup/retry recovery; the domain's stable plan/version
        # keys make re-execution an exact recovery operation.
        return None
    try:
        payload = json.loads(str(row["result_json"] or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("视频命令 receipt 结果损坏") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("视频命令 receipt 结果不是对象")
    return payload


def finish_video_command_operation(
    *,
    command: str,
    idempotency_key: str,
    request_fingerprint: str,
    result: dict[str, Any],
) -> None:
    conn = get_conn()
    _ensure_table(conn)
    updated = conn.execute(
        """UPDATE video_command_operation_receipts
              SET status='succeeded',result_json=?,updated_at=?
            WHERE operation_key=? AND command=? AND request_fingerprint=?
              AND status IN ('running','succeeded')""",
        (
            json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
            time.time(),
            f"{command}:{str(idempotency_key or '').strip()}",
            command,
            request_fingerprint,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise VideoCommandOperationConflict("视频命令 receipt 已被不同请求占用")
    conn.commit()

