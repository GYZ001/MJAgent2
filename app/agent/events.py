"""Turn 事件：单调递增 event_id 落库 + SSE 格式化（PRD §10.2）。

事件持久化到 SQLite 而非仅内存队列，这样 `GET /turns/{id}/events` 天然支持
`Last-Event-ID` 断线重连续传，且不会因进程重启丢失。
"""
from __future__ import annotations

import json
import threading
from typing import Any

from app.agent.redaction import redact_value
from app.db import get_conn, now, rows_to_dicts

_event_id_lock = threading.Lock()


def append_event(turn_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """追加一条 turn 事件，event_id 在该 turn 内单调递增。"""
    conn = get_conn()
    with _event_id_lock:
        row = conn.execute(
            "SELECT COALESCE(MAX(event_id), 0) + 1 AS next_id FROM agent_turn_events WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        next_id = int(row["next_id"])
        conn.execute(
            """INSERT INTO agent_turn_events(turn_id, event_id, event_type, payload_json, created_at)
               VALUES(?,?,?,?,?)""",
            (turn_id, next_id, event_type, json.dumps(redact_value(payload or {}), ensure_ascii=False, default=str), now()),
        )
        conn.commit()
    return {"turn_id": turn_id, "event_id": next_id, "event_type": event_type,
            "payload": payload or {}, "created_at": now()}


def list_events(turn_id: str, *, after_event_id: int | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    clause = "AND event_id > ?" if after_event_id is not None else ""
    params: list[Any] = [turn_id]
    if after_event_id is not None:
        params.append(after_event_id)
    params.append(max(1, min(limit, 5000)))
    rows = rows_to_dicts(get_conn().execute(
        f"SELECT * FROM agent_turn_events WHERE turn_id=? {clause} ORDER BY event_id LIMIT ?",
        params,
    ).fetchall())
    for row in rows:
        try:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            row["payload"] = {}
    return rows


def format_sse(event: dict[str, Any]) -> str:
    """按 SSE 格式渲染单条事件，`id:` 字段供浏览器 Last-Event-ID 续传。"""
    data = json.dumps(
        {"turn_id": event["turn_id"], "event_type": event["event_type"], "payload": event["payload"],
         "created_at": event["created_at"]},
        ensure_ascii=False, default=str,
    )
    return f"id: {event['event_id']}\nevent: {event['event_type']}\ndata: {data}\n\n"
