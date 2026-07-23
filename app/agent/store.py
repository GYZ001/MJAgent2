"""对话 Agent 存储层：conversations/messages/turns/tool_calls/approvals 的 CRUD。

所有落库前的 JSON 内容统一经 `redaction.redact_value` 过滤，防止密钥或
Authorization 头意外写入 SQLite（PRD §12.1）。
"""
from __future__ import annotations

import json
from typing import Any

from app.agent.redaction import redact_value
from app.db import get_conn, new_id, now, rows_to_dicts


def _dump(value: Any) -> str:
    return json.dumps(redact_value(value), ensure_ascii=False, default=str)


def _load(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ---------- conversations ----------

def create_conversation(*, title: str | None, project_id: str | None, created_by: str | None) -> dict[str, Any]:
    conv_id = new_id("conv")
    stamp = now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO agent_conversations(id, title, project_id, created_by, status, created_at, updated_at)
           VALUES(?,?,?,?,'active',?,?)""",
        (conv_id, title, project_id, created_by, stamp, stamp),
    )
    conn.commit()
    return get_conversation(conv_id)  # type: ignore[return-value]


def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM agent_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    return dict(row) if row else None


def touch_conversation(conversation_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE agent_conversations SET updated_at=? WHERE id=?", (now(), conversation_id))
    conn.commit()


# ---------- messages ----------

def append_message(
    conversation_id: str, role: str, content: Any, *,
    turn_id: str | None = None, model_visible: bool = True,
) -> dict[str, Any]:
    msg_id = new_id("msg")
    stamp = now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO agent_messages(id, conversation_id, turn_id, role, content_json, model_visible, created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (msg_id, conversation_id, turn_id, role, _dump(content), 1 if model_visible else 0, stamp),
    )
    conn.commit()
    touch_conversation(conversation_id)
    return get_message(msg_id)  # type: ignore[return-value]


def get_message(message_id: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["content"] = _load(out.pop("content_json"), {})
    out["model_visible"] = bool(out["model_visible"])
    return out


def list_messages(conversation_id: str, *, model_visible_only: bool = False) -> list[dict[str, Any]]:
    clause = "AND model_visible=1" if model_visible_only else ""
    rows = rows_to_dicts(get_conn().execute(
        f"SELECT * FROM agent_messages WHERE conversation_id=? {clause} ORDER BY created_at, id",
        (conversation_id,),
    ).fetchall())
    for row in rows:
        row["content"] = _load(row.pop("content_json"), {})
        row["model_visible"] = bool(row["model_visible"])
    return rows


# ---------- turns ----------

def create_turn(
    conversation_id: str, *, context_envelope: dict[str, Any] | None,
    model_provider: str | None, model: str | None, prompt_version: str | None,
) -> dict[str, Any]:
    turn_id = new_id("turn")
    conn = get_conn()
    conn.execute(
        """INSERT INTO agent_turns(
            id, conversation_id, status, context_envelope_json, model_provider, model,
            prompt_version, started_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (turn_id, conversation_id, "running", _dump(context_envelope or {}),
         model_provider, model, prompt_version, now()),
    )
    conn.commit()
    return get_turn(turn_id)  # type: ignore[return-value]


def get_turn(turn_id: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM agent_turns WHERE id=?", (turn_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["context_envelope"] = _load(out.pop("context_envelope_json"), {})
    return out


def update_turn(turn_id: str, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key}=?" for key in fields)
    conn = get_conn()
    conn.execute(f"UPDATE agent_turns SET {columns} WHERE id=?", (*fields.values(), turn_id))
    conn.commit()


# ---------- tool calls ----------

def create_tool_call(
    turn_id: str, *, command_name: str, command_version: str | None, arguments: dict[str, Any],
    risk: str | None, status: str, idempotency_key: str | None = None,
) -> dict[str, Any]:
    call_id = new_id("tc")
    conn = get_conn()
    conn.execute(
        """INSERT INTO agent_tool_calls(
            id, turn_id, command_name, command_version, arguments_json, risk, status,
            idempotency_key, started_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (call_id, turn_id, command_name, command_version, _dump(arguments), risk, status,
         idempotency_key, now()),
    )
    conn.commit()
    return get_tool_call(call_id)  # type: ignore[return-value]


def get_tool_call(tool_call_id: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM agent_tool_calls WHERE id=?", (tool_call_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["arguments"] = _load(out.pop("arguments_json"), {})
    out["result_summary"] = _load(out.pop("result_summary_json"), None)
    return out


def update_tool_call(tool_call_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "result_summary" in fields:
        fields["result_summary_json"] = _dump(fields.pop("result_summary"))
    if "arguments" in fields:
        fields["arguments_json"] = _dump(fields.pop("arguments"))
    columns = ", ".join(f"{key}=?" for key in fields)
    conn = get_conn()
    conn.execute(f"UPDATE agent_tool_calls SET {columns} WHERE id=?", (*fields.values(), tool_call_id))
    conn.commit()


def list_tool_calls(turn_id: str) -> list[dict[str, Any]]:
    rows = rows_to_dicts(get_conn().execute(
        "SELECT * FROM agent_tool_calls WHERE turn_id=? ORDER BY started_at, id", (turn_id,)
    ).fetchall())
    for row in rows:
        row["arguments"] = _load(row.pop("arguments_json"), {})
        row["result_summary"] = _load(row.pop("result_summary_json"), None)
    return rows


# ---------- approvals ----------

def create_approval(
    tool_call_id: str, *, impact_snapshot: dict[str, Any], state_fingerprint: str,
    token_hash: str, expires_at: float,
) -> dict[str, Any]:
    approval_id = new_id("agappr")
    conn = get_conn()
    conn.execute(
        """INSERT INTO agent_approvals(
            id, tool_call_id, impact_snapshot_json, state_fingerprint, token_hash,
            expires_at, created_at
        ) VALUES(?,?,?,?,?,?,?)""",
        (approval_id, tool_call_id, _dump(impact_snapshot), state_fingerprint, token_hash,
         expires_at, now()),
    )
    conn.commit()
    return get_approval(approval_id)  # type: ignore[return-value]


def get_approval(approval_id: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM agent_approvals WHERE id=?", (approval_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["impact_snapshot"] = _load(out.pop("impact_snapshot_json"), {})
    return out


def get_approval_by_tool_call(tool_call_id: str) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM agent_approvals WHERE tool_call_id=? ORDER BY created_at DESC LIMIT 1",
        (tool_call_id,),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["impact_snapshot"] = _load(out.pop("impact_snapshot_json"), {})
    return out


def update_approval(approval_id: str, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key}=?" for key in fields)
    conn = get_conn()
    conn.execute(f"UPDATE agent_approvals SET {columns} WHERE id=?", (*fields.values(), approval_id))
    conn.commit()
