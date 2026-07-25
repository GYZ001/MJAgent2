"""视频 Supervisor 协作控制：pause / handoff（进程内 + DB 持久化）。"""
from __future__ import annotations

import json
import threading
from typing import Literal

from app.db import get_conn, now

ControlAction = Literal["pause", "handoff", "clear", "retry_now"]

_lock = threading.Lock()
_memory: dict[str, str] = {}


def _ensure_column(conn=None) -> None:
    db = conn or get_conn()
    try:
        db.execute("ALTER TABLE episodes ADD COLUMN video_control_json TEXT")
        db.commit()
    except Exception:  # noqa: BLE001 duplicate column
        pass


def request_control(episode_id: str, action: ControlAction) -> dict:
    """签发控制请求；pause/handoff/retry_now 在下一 tick 边界生效。"""
    if action not in {"pause", "handoff", "clear", "retry_now"}:
        raise ValueError(f"未知控制动作：{action}")
    _ensure_column()
    payload = {"action": action, "requested_at": now()}
    with _lock:
        if action == "clear":
            _memory.pop(episode_id, None)
        else:
            _memory[episode_id] = action
    conn = get_conn()
    if action == "clear":
        conn.execute(
            "UPDATE episodes SET video_control_json=NULL WHERE id=?",
            (episode_id,),
        )
    else:
        conn.execute(
            "UPDATE episodes SET video_control_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), episode_id),
        )
    conn.commit()
    return {"episode_id": episode_id, **payload}


def peek_control(episode_id: str) -> str | None:
    with _lock:
        mem = _memory.get(episode_id)
    if mem:
        return mem
    _ensure_column()
    row = get_conn().execute(
        "SELECT video_control_json FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if not row:
        return None
    try:
        raw = row["video_control_json"]
    except (KeyError, IndexError, TypeError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    action = (data or {}).get("action")
    return action if action in {"pause", "handoff", "retry_now"} else None


def consume_control(episode_id: str) -> str | None:
    action = peek_control(episode_id)
    if action:
        request_control(episode_id, "clear")
    return action


def control_snapshot(episode_id: str) -> dict | None:
    action = peek_control(episode_id)
    if not action:
        return None
    return {"action": action, "pending": True}
