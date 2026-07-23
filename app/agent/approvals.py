"""批准/拒绝 tool_call：签发/消费 Capability Policy 的 approval_token。

原始 token 只保存在进程内存（与 `app.capabilities.policy._APPROVALS` 的做法一致），
数据库只落 `token_hash` 供审计追溯，绝不落原始 token（PRD §10.3 备注）。
"""
from __future__ import annotations

import hashlib
import threading
import time

from app.agent import store

_lock = threading.Lock()
_PENDING_TOKENS: dict[str, str] = {}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register_pending(
    tool_call_id: str, *, token: str, impact_snapshot: dict, state_fingerprint: str, expires_at: float,
) -> dict:
    """orchestrator 在收到 WAITING_APPROVAL 后调用：记内存 token + 落审计行。"""
    with _lock:
        _PENDING_TOKENS[tool_call_id] = token
    return store.create_approval(
        tool_call_id,
        impact_snapshot=impact_snapshot,
        state_fingerprint=state_fingerprint,
        token_hash=hash_token(token),
        expires_at=expires_at,
    )


def peek_pending_token(tool_call_id: str) -> str | None:
    with _lock:
        return _PENDING_TOKENS.get(tool_call_id)


def consume_pending_token(tool_call_id: str) -> str | None:
    with _lock:
        return _PENDING_TOKENS.pop(tool_call_id, None)


def discard_pending_token(tool_call_id: str) -> None:
    with _lock:
        _PENDING_TOKENS.pop(tool_call_id, None)


def record_decision(tool_call_id: str, *, decision: str, decided_by: str | None, reason: str | None) -> None:
    approval = store.get_approval_by_tool_call(tool_call_id)
    if not approval:
        return
    store.update_approval(
        approval["id"], decision=decision, decided_by=decided_by, reason=reason, used_at=time.time(),
    )
