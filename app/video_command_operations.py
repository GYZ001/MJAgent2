"""Durable domain receipts for paid video generation commands.

The generic CommandBus receipt is deliberately short lived and its abandoned
``running`` claims are reclaimed at startup.  Paid generation therefore also
needs a domain receipt plus content-addressed plan/version keys so a retry after
an unknown HTTP response recovers the exact durable work.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.db import get_conn


class VideoCommandOperationConflict(ValueError):
    """The same operation key was presented for a different canonical request."""


class VideoCommandOperationInProgress(ValueError):
    """Another live owner still holds the paid-operation lease."""


_LEASE_S = 10 * 60


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
            binding_json TEXT NOT NULL DEFAULT '{}',
            claim_token TEXT NOT NULL DEFAULT '',
            lease_expires_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    columns = {str(row[1]) for row in conn.execute(
        "PRAGMA table_info(video_command_operation_receipts)"
    )}
    for name, ddl in (
        ("binding_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("claim_token", "TEXT NOT NULL DEFAULT ''"),
        ("lease_expires_at", "REAL NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE video_command_operation_receipts ADD COLUMN {name} {ddl}"
            )


def claim_video_command_operation(
    *,
    command: str,
    idempotency_key: str,
    request_fingerprint: str,
    scope_type: str,
    scope_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Claim or resume one operation; return its exact completed domain result."""
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_key:
        raise VideoCommandOperationConflict("视频命令缺少稳定幂等键")
    operation_key = f"{command}:{normalized_key}"
    conn = get_conn()
    _ensure_table(conn)
    stamp = time.time()
    owner = uuid.uuid4().hex
    try:
        conn.execute(
            """INSERT INTO video_command_operation_receipts(
                   operation_key,command,request_fingerprint,scope_type,scope_id,
                   status,result_json,binding_json,claim_token,lease_expires_at,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,'running','{}','{}',?,?,?,?)""",
            (
                operation_key,
                command,
                request_fingerprint,
                scope_type,
                scope_id,
                owner,
                stamp + _LEASE_S,
                stamp,
                stamp,
            ),
        )
        conn.commit()
        return owner, None
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
        if str(row["status"]) == "running" and float(row["lease_expires_at"] or 0) > stamp:
            raise VideoCommandOperationInProgress("相同视频生成操作正在执行")
        try:
            binding = json.loads(str(row["binding_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("视频命令 binding 损坏") from exc
        if (
            isinstance(binding, dict)
            and isinstance(binding.get("result"), dict)
            and (command == "video.generate_shot" or binding.get("operation_complete") is True)
        ):
            recovered = dict(binding["result"])
            conn.execute(
                """UPDATE video_command_operation_receipts
                      SET status='succeeded',result_json=?,lease_expires_at=0,updated_at=?
                    WHERE operation_key=? AND request_fingerprint=?
                      AND status!='succeeded' AND lease_expires_at<=?""",
                (
                    json.dumps(recovered, ensure_ascii=False, sort_keys=True),
                    stamp,
                    operation_key,
                    request_fingerprint,
                    stamp,
                ),
            )
            conn.commit()
            return None, recovered
        updated = conn.execute(
            """UPDATE video_command_operation_receipts
                  SET status='running',claim_token=?,lease_expires_at=?,updated_at=?
                WHERE operation_key=? AND request_fingerprint=?
                  AND (status!='running' OR lease_expires_at<=?)""",
            (
                owner,
                stamp + _LEASE_S,
                stamp,
                operation_key,
                request_fingerprint,
                stamp,
            ),
        )
        if updated.rowcount != 1:
            conn.rollback()
            raise VideoCommandOperationInProgress("视频命令 receipt 接管 CAS 冲突")
        conn.commit()
        return owner, None
    try:
        payload = json.loads(str(row["result_json"] or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("视频命令 receipt 结果损坏") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("视频命令 receipt 结果不是对象")
    return None, payload


def bind_video_command_operation(
    *,
    command: str,
    idempotency_key: str,
    request_fingerprint: str,
    claim_token: str,
    binding: dict[str, Any],
    conn,
    merge: bool = False,
) -> None:
    """Bind exact plan/version/job IDs in the same transaction as enqueue."""
    _ensure_table(conn)
    payload = dict(binding)
    if merge:
        row = conn.execute(
            """SELECT binding_json FROM video_command_operation_receipts
               WHERE operation_key=? AND request_fingerprint=? AND claim_token=?""",
            (
                f"{command}:{str(idempotency_key or '').strip()}",
                request_fingerprint,
                claim_token,
            ),
        ).fetchone()
        try:
            current = json.loads(str(row["binding_json"] or "{}")) if row else {}
        except json.JSONDecodeError:
            current = {}
        if isinstance(current, dict):
            payload = {**current, **payload}
            if "append_enqueued" in binding:
                items = list(current.get("enqueued") or [])
                appended = dict(binding["append_enqueued"])
                items = [item for item in items if item.get("shot_id") != appended.get("shot_id")]
                items.append(appended)
                payload["enqueued"] = items
                payload.pop("append_enqueued", None)
    updated = conn.execute(
        """UPDATE video_command_operation_receipts
              SET binding_json=?,updated_at=?
            WHERE operation_key=? AND command=? AND request_fingerprint=?
              AND claim_token=? AND status='running' AND lease_expires_at>?""",
        (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            time.time(),
            f"{command}:{str(idempotency_key or '').strip()}",
            command,
            request_fingerprint,
            claim_token,
            time.time(),
        ),
    )
    if updated.rowcount != 1:
        raise VideoCommandOperationConflict("视频命令 receipt 绑定 CAS 冲突")


def read_video_command_operation_binding(
    *, command: str, idempotency_key: str, request_fingerprint: str,
) -> dict[str, Any]:
    conn = get_conn()
    _ensure_table(conn)
    row = conn.execute(
        """SELECT binding_json FROM video_command_operation_receipts
           WHERE operation_key=? AND command=? AND request_fingerprint=?""",
        (
            f"{command}:{str(idempotency_key or '').strip()}",
            command,
            request_fingerprint,
        ),
    ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(str(row["binding_json"] or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("视频命令 binding 损坏") from exc
    return value if isinstance(value, dict) else {}


def renew_video_command_operation(
    *, command: str, idempotency_key: str, request_fingerprint: str, claim_token: str,
) -> None:
    conn = get_conn()
    _ensure_table(conn)
    stamp = time.time()
    updated = conn.execute(
        """UPDATE video_command_operation_receipts
              SET lease_expires_at=?,updated_at=?
            WHERE operation_key=? AND command=? AND request_fingerprint=?
              AND claim_token=? AND status='running'""",
        (
            stamp + _LEASE_S,
            stamp,
            f"{command}:{str(idempotency_key or '').strip()}",
            command,
            request_fingerprint,
            claim_token,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise VideoCommandOperationConflict("视频命令 owner 已被接管")
    conn.commit()


def finish_video_command_operation(
    *,
    command: str,
    idempotency_key: str,
    request_fingerprint: str,
    claim_token: str,
    result: dict[str, Any],
) -> None:
    conn = get_conn()
    _ensure_table(conn)
    updated = conn.execute(
        """UPDATE video_command_operation_receipts
              SET status='succeeded',result_json=?,updated_at=?
            WHERE operation_key=? AND command=? AND request_fingerprint=?
              AND claim_token=? AND status IN ('running','succeeded')""",
        (
            json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
            time.time(),
            f"{command}:{str(idempotency_key or '').strip()}",
            command,
            request_fingerprint,
            claim_token,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise VideoCommandOperationConflict("视频命令 receipt 已被不同请求占用")
    conn.commit()


def fail_video_command_operation(
    *,
    command: str,
    idempotency_key: str,
    request_fingerprint: str,
    claim_token: str,
) -> None:
    """Release a known-failed handler owner without erasing durable bindings."""
    conn = get_conn()
    _ensure_table(conn)
    updated = conn.execute(
        """UPDATE video_command_operation_receipts
              SET status='failed',lease_expires_at=0,updated_at=?
            WHERE operation_key=? AND command=? AND request_fingerprint=?
              AND claim_token=? AND status='running'""",
        (
            time.time(),
            f"{command}:{str(idempotency_key or '').strip()}",
            command,
            request_fingerprint,
            claim_token,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise VideoCommandOperationConflict("视频命令失败 receipt owner 已失效")
    conn.commit()
