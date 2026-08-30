"""本地会话闸门：保护 /api/* 免受恶意网页跨站调用（PRD §12.2 / 2026-07-27 Todolist T1）。

RBAC 第二阶段起，真正的鉴权凭据是 ``app.auth.sessions`` 落在 SQLite 里的
per-user 会话；本模块只保留 Origin/CSRF 闸门与请求生命周期内的
ContextVar 绑定，不再是「唯一真源」。为了不让本机其它并行开发会话在中间
阶段随时重启后端就把应用打成不可用，这里临时保留旧的进程级共享秘密作为
回退（``MJ_LEGACY_SHARED_SESSION``，默认开启，Stage 8 移除）。

Origin 策略：
- 本机开发 allowlist（localhost / 127.0.0.1 的前后端端口）；
- 公网同域反代：Origin 主机名与请求 Host（或 X-Forwarded-Host）一致即放行，
  换域名无需改代码；异源恶意站点仍会被拒绝。
"""
from __future__ import annotations

import contextvars
import hmac
import logging
import os
import secrets
import threading
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request

from app.auth.principal import Principal, get_current_principal, set_current_principal
from app.auth.sessions import resolve_session
from app.config import DATA_DIR

SESSION_PATH = DATA_DIR / "local_session_secret.txt"
SESSION_HEADER = "x-manju-session"
APPROVAL_HEADER = "x-manju-approval-token"

_LOGGER = logging.getLogger(__name__)

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


# TODO(Stage 8): 移除。真实登录全量上线后，这条共享秘密回退通道应当整体删掉。
def legacy_shared_session_enabled() -> bool:
    """是否仍接受旧的进程级共享秘密作为兜底鉴权（默认开启）。"""
    raw = os.environ.get("MJ_LEGACY_SHARED_SESSION", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


# 记录当前 Principal 是由哪枚 token 解析而来，避免跨请求/跨夹具误用缓存身份。
_principal_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "principal_token", default=None
)

_legacy_warn_lock = threading.Lock()
_legacy_warned = False


def _legacy_shared_principal() -> Principal:
    return Principal(
        user_id="legacy-shared",
        username="legacy",
        is_system_admin=True,
    )


def _warn_legacy_once() -> None:
    global _legacy_warned
    if _legacy_warned:
        return
    with _legacy_warn_lock:
        if _legacy_warned:
            return
        _legacy_warned = True
        _LOGGER.warning(
            "MJ_LEGACY_SHARED_SESSION 兼容通道生效：仍接受旧的进程级共享会话秘密，"
            "该请求被视为系统管理员身份；Stage 8 将移除这条回退路径。"
        )


def _resolve_token_principal(token: str | None) -> Principal | None:
    """token 优先按真实用户会话解析；解析失败且兼容开关开启时回退共享秘密。"""
    principal = resolve_session(token)
    if principal is not None:
        return principal
    if legacy_shared_session_enabled() and verify_session_token(token):
        _warn_legacy_once()
        return _legacy_shared_principal()
    return None


def _hostname_of(value: str | None) -> str | None:
    """从 Origin URL 或 Host 头取出小写主机名（忽略端口与默认 80/443）。"""
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if "://" in raw:
        host = urlparse(raw).hostname
    else:
        # Host / X-Forwarded-Host 可能是 ``a, b`` 或 ``example.com:443``
        host = raw.split(",", 1)[0].strip()
        if host.startswith("["):
            # IPv6 literal ``[::1]:8230``
            end = host.find("]")
            host = host[1:end] if end != -1 else host.strip("[]")
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not host:
        return None
    return host.strip(".").lower() or None


def _request_public_hostname(request: Request) -> str | None:
    """优先 X-Forwarded-Host（反代保留的公网域名），否则 Host。"""
    forwarded = (request.headers.get("x-forwarded-host") or "").strip()
    if forwarded:
        return _hostname_of(forwarded)
    return _hostname_of(request.headers.get("host"))


def origin_allowed(origin: str | None, request: Request) -> bool:
    """浏览器 Origin 是否允许：本机白名单，或与请求公网 Host 同源。"""
    if not origin:
        return True
    normalized = origin.strip().rstrip("/")
    if normalized in _DEFAULT_ORIGINS:
        return True
    origin_host = _hostname_of(normalized)
    request_host = _request_public_hostname(request)
    return bool(origin_host and request_host and origin_host == request_host)


def require_local_session(
    request: Request,
    x_manju_session: str | None = Header(default=None, alias="X-Manju-Session"),
) -> str:
    """FastAPI Depends：校验真实用户会话（或兼容期共享秘密）；可选校验 Origin。

    EventSource 无法自定义 Header，因此也接受 ``?session=`` 查询参数。
    """
    origin = request.headers.get("origin")
    if origin and not origin_allowed(origin, request):
        raise HTTPException(403, "不允许的 Origin")
    token = x_manju_session or request.query_params.get("session")
    # 中间件（bind_request_principal）通常已经解析过，直接复用，避免每个请求
    # 重复查一次 user_sessions。中间件缺席时（比如测试里直接调依赖）再解析。
    if not token:
        # 没带凭证一律拒绝。不能因为上下文里恰好残留着一个 Principal 就放行——
        # 测试夹具会预置系统管理员 Principal，真按「有 Principal 就通过」写，
        # 无凭证请求会被静默放行（这条正是被 test_agent_requires_session_token
        # 和 test_monitor_prd 的 401 断言抓出来的）。
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    # 中间件通常已解析过同一枚 token，直接复用，省掉一次 user_sessions 查询；
    # 但必须确认缓存的 Principal 确实来自**这一枚** token，否则就退化成
    # 「上下文里有身份就放行」。
    principal = get_current_principal() if _principal_token.get() == token else None
    if principal is None:
        principal = _resolve_token_principal(token)
        if principal is not None:
            set_current_principal(principal)
            _principal_token.set(token)
    if principal is None:
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    # 保持 get_request_session_id() 可用：Command Bus / 审批令牌绑定的是
    # 这里写入 ContextVar 的原始 token，与 Principal 是谁无关。
    bind_verified_session(request)
    return token or ""


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    hostname = _hostname_of(host) or host.split(":", 1)[0].strip().lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def assert_session_bootstrap_allowed(request: Request) -> None:
    """``GET /api/session`` 无鉴权：带 Origin 时必须同源；不带 Origin 时要求有 Host。

    「不带 Origin」不是绕过口子：浏览器只在**同源 GET/HEAD** 时省略 Origin，
    跨源 fetch 一定带 Origin（走下面的同源校验被拒），且本响应没有任何 CORS 头，
    异源页面即便发出请求也读不到 body。真正的边界是端口可达性，由
    MJ_BACKEND_HOST 与反代决定，不由这里决定。

    这里原先靠「客户端是回环地址」放行，隐式依赖请求必须经 vite 反代到达；
    后端直接对外服务构建产物后，同源 GET 因缺 Origin 被误拒，页面拿不到凭证，
    表现为「项目加载失败」。
    """
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        if not origin_allowed(origin, request):
            raise HTTPException(403, "不允许的 Origin")
        return
    host = (request.headers.get("host") or "").strip()
    if _is_loopback_host(host):
        return
    client_host = request.client.host if request.client else None
    if client_host in {"127.0.0.1", "::1", "localhost", "testclient"}:
        # testclient：Starlette TestClient 本机回环；生产反向代理不应伪造为 testclient。
        return
    if host:
        return
    raise HTTPException(403, "会话领取需要 Host 或同源 Origin")


def public_session_payload() -> dict[str, str]:
    return {"session_token": ensure_session_secret(), "header": "X-Manju-Session"}


def extract_raw_session_token(request: Request) -> str | None:
    """从 Header 或 ``?session=`` 取出原始会话凭证（未校验）。"""
    header = request.headers.get("X-Manju-Session") or request.headers.get(SESSION_HEADER)
    return header or request.query_params.get("session")


def clear_principal_token() -> None:
    """请求收尾时清空 token 绑定，与 set_current_principal(None) 成对使用。"""
    _principal_token.set(None)


def bind_request_principal(request: Request) -> Principal | None:
    """在 HTTP 中间件里解析并注入本请求的 Principal。

    **必须在中间件（async 上下文）里做，不能只依赖 require_local_session。**
    后者是 *同步* 依赖，FastAPI 用 ``run_in_threadpool`` 执行它；线程内对
    ContextVar 的写入不会回传到请求上下文，于是路由处理器与 Command Bus
    读到的永远是 None。而 Bus 的授权分支把 ``principal is None`` 当作
    「MCP/内部调用，放行」——真按同步依赖写入的话，整套 RBAC 会**静默失效
    （fail-open）**，且表面上一切正常。中间件跑在请求自身的 async 上下文里，
    写进去的值才会被后续同步/异步代码看到。

    这里只解析、不拒绝：是否放行仍由 ``require_local_session`` 决定，
    未挂闸门的公开路由（/api/session、/api/auth/login、健康检查）不受影响。
    """
    token = extract_raw_session_token(request)
    principal = _resolve_token_principal(token) if token else None
    set_current_principal(principal)
    _principal_token.set(token if principal is not None else None)
    return principal


def bind_verified_session(request: Request) -> str | None:
    """校验通过则写入 ContextVar 并返回 token；否则清空并返回 None。

    校验口径与 ``require_local_session`` 完全一致（真实用户会话优先，兼容期
    共享秘密兜底），这样 ``get_request_session_id()`` 对两种 token 都能返回
    非空值，Command Bus 的审批绑定不会因为换成真实登录会话而失效。
    """
    raw = extract_raw_session_token(request)
    if _resolve_token_principal(raw) is not None:
        set_request_session_id(raw)
        return raw
    set_request_session_id(None)
    return None
