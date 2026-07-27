"""本地会话秘密：保护 /api/* 免受恶意网页跨站调用（PRD §12.2 / 2026-07-27 Todolist T1）。

启动时生成（或复用落盘）随机会话秘密；前端通过 ``/api/session`` 领取后，
以 ``X-Manju-Session`` 头携带。仅绑定本机 Origin allowlist。
"""
from __future__ import annotations

import contextvars
import hmac
import secrets
import threading

from fastapi import Header, HTTPException, Request

from app.config import DATA_DIR

SESSION_PATH = DATA_DIR / "local_session_secret.txt"
SESSION_HEADER = "x-manju-session"
APPROVAL_HEADER = "x-manju-approval-token"

_DEFAULT_ORIGINS = frozenset({
    "http://localhost:5230",
    "http://127.0.0.1:5230",
    "http://localhost:8230",
    "http://127.0.0.1:8230",
})

_lock = threading.Lock()
_secret: str | None = None

# 由 HTTP 中间件注入：仅在 verify 通过后写入，供 Command Bus 绑定 approval。
_request_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_session_id", default=None
)


def set_request_session_id(session_id: str | None) -> None:
    _request_session_id.set(session_id)


def get_request_session_id() -> str | None:
    return _request_session_id.get()


def ensure_session_secret() -> str:
    global _secret
    with _lock:
        if _secret:
            return _secret
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if SESSION_PATH.exists():
            value = SESSION_PATH.read_text(encoding="utf-8").strip()
            if value:
                _secret = value
                return _secret
        value = secrets.token_urlsafe(32)
        SESSION_PATH.write_text(value + "\n", encoding="utf-8")
        try:
            SESSION_PATH.chmod(0o600)
        except OSError:
            pass
        _secret = value
        return _secret


def reset_session_secret_for_tests() -> str:
    global _secret
    with _lock:
        _secret = secrets.token_urlsafe(32)
        return _secret


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    expected = ensure_session_secret()
    return hmac.compare_digest(token, expected)


def require_local_session(
    request: Request,
    x_manju_session: str | None = Header(default=None, alias="X-Manju-Session"),
) -> str:
    """FastAPI Depends：校验本机会话秘密；可选校验 Origin。

    EventSource 无法自定义 Header，因此也接受 ``?session=`` 查询参数。
    """
    origin = request.headers.get("origin")
    if origin and origin not in _DEFAULT_ORIGINS:
        raise HTTPException(403, "不允许的 Origin")
    token = x_manju_session or request.query_params.get("session")
    if not verify_session_token(token):
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    return token or ""


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    hostname = host.split(":", 1)[0].strip().lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def assert_session_bootstrap_allowed(request: Request) -> None:
    """``GET /api/session`` 无鉴权，必须限制在本机开发 Origin / loopback。"""
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        if origin not in _DEFAULT_ORIGINS:
            raise HTTPException(403, "不允许的 Origin")
        return
    host = (request.headers.get("host") or "").strip()
    if _is_loopback_host(host):
        return
    client_host = request.client.host if request.client else None
    if client_host in {"127.0.0.1", "::1", "localhost", "testclient"}:
        # testclient：Starlette TestClient 本机回环；生产反向代理不应伪造为 testclient。
        return
    raise HTTPException(403, "会话领取仅允许本机 Host")


def public_session_payload() -> dict[str, str]:
    return {"session_token": ensure_session_secret(), "header": "X-Manju-Session"}


def extract_raw_session_token(request: Request) -> str | None:
    """从 Header 或 ``?session=`` 取出原始会话凭证（未校验）。"""
    header = request.headers.get("X-Manju-Session") or request.headers.get(SESSION_HEADER)
    return header or request.query_params.get("session")


def bind_verified_session(request: Request) -> str | None:
    """校验通过则写入 ContextVar 并返回 token；否则清空并返回 None。"""
    raw = extract_raw_session_token(request)
    if verify_session_token(raw):
        set_request_session_id(raw)
        return raw
    set_request_session_id(None)
    return None
