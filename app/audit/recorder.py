"""请求级审计上下文 + 总线命令记录入口。

HTTP 中间件在请求进入时 ``begin_http_request()``，收尾时 ``finish_http_request()``；
Command Bus 的审计包装（``app.capabilities.bus_audit.run_audited``/
``run_audited_sync``，由 ``CommandBus.execute``/``execute_async`` 调用）在每次
执行完成（含异常）时调用 ``record_bus_outcome()``。

两者通过同一个可变的 ``_HttpAuditContext`` 对象串联：Starlette 中间件
``call_next`` 返回时响应可能还在流式发送，且业务逻辑（同步 handler 走
``run_in_threadpool``，衍生的 asyncio task 走 ``copy_context()``）在另一个
线程/task 里拿到的是**同一个对象引用**——因此用对象上的 ``finished`` 标志
判断"这次记录该排队还是该立即落库"，不靠 ContextVar 的 set/reset 时序。
"""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.audit import store
from app.audit.redact import redact_and_truncate
from app.auth.principal import get_current_principal
from app.db import new_id
from app.db import now as db_now

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXCLUDED_HTTP_PATHS = frozenset({"/api/system/monitor/events", "/api/session"})
_MAX_TARGET_CHARS = 160
_MAX_FIELD_CHARS = 40
_MAX_LABEL_CHARS = 40
_MAX_SUMMARY_CHARS = 2000
_TARGET_EXCLUDED_KEYS = frozenset({"idempotency_key", "approval_token", "project_id"})

# CommandStatus 是 app.capabilities.schemas 的枚举，本模块不 import 它（见模块
# 文档"绝不 import app.capabilities.*"）——record_bus_outcome 只读 .value 这个
# 纯字符串做鸭子类型匹配，这张表就是那份映射的落地。
_COMMAND_STATUS_OUTCOME = {
    "succeeded": "ok", "accepted": "ok", "waiting_approval": "waiting_approval",
    "rejected": "rejected", "conflict": "rejected", "cancelled": "rejected",
    "failed": "failed",
}


@dataclass
class _HttpAuditContext:
    method: str
    path: str
    ip: str | None
    user_agent: str | None
    started_at: float
    finished: bool = False
    bus_rows: list[dict[str, Any]] = field(default_factory=list)
    actor_user_id: str | None = None
    actor_username: str | None = None
    actor_is_admin: bool | None = None
    actor_explicit: bool = False
    error_id: str | None = None


_CTX: contextvars.ContextVar[_HttpAuditContext | None] = contextvars.ContextVar(
    "audit_http_ctx", default=None
)
_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar("audit_source", default="system")
_SOURCE_ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "audit_source_actor", default=None
)


def begin_http_request(request: Any) -> _HttpAuditContext:
    """中间件调用：为本请求建立审计上下文并绑定 ContextVar；/api/* 默认来源 ui。"""
    ctx = _HttpAuditContext(
        method=request.method,
        path=request.url.path,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        started_at=time.time(),
    )
    _CTX.set(ctx)
    _SOURCE.set("ui" if request.url.path.startswith("/api/") else "system")
    return ctx


def finish_http_request(request: Any, status_code: int) -> None:
    """中间件收尾调用：把请求期间排队的总线行落库；若一行都没有，按需补一条 HTTP 级行。"""
    ctx = _CTX.get()
    if ctx is None or ctx.finished:
        return
    ctx.finished = True
    if ctx.bus_rows:
        for row in ctx.bus_rows:
            store.insert_operation_audit_row(_fill_http_fields(row, ctx, status_code))
        return
    if not _should_emit_http_row(ctx.method, ctx.path):
        return
    store.insert_operation_audit_row(_build_http_row(request, ctx, status_code))


def note_actor(user_id: str | None, username: str | None, is_system_admin: bool) -> None:
    """登录端点在 principal 尚未建立（成功/失败均是）前显式声明本次请求的执行者。"""
    ctx = _CTX.get()
    if ctx is None:
        return
    ctx.actor_user_id = user_id
    ctx.actor_username = username
    ctx.actor_is_admin = is_system_admin
    ctx.actor_explicit = True


def note_error_id(error_id: str) -> None:
    """main.py 的三个 exception handler 把 log_error 生成的 error_id 塞回本次请求。"""
    ctx = _CTX.get()
    if ctx is not None:
        ctx.error_id = error_id


def current_source() -> str:
    return _SOURCE.get()


@contextmanager
def source_context(source: str, actor_username: str | None = None):
    """agent/mcp 调用总线时临时覆盖来源标签，退出后还原成调用前的值。"""
    source_token = _SOURCE.set(source)
    actor_token = _SOURCE_ACTOR.set(actor_username)
    try:
        yield
    finally:
        _SOURCE.reset(source_token)
        _SOURCE_ACTOR.reset(actor_token)


def record_command(
    name: str, title: str | None, source: str, status: str, error_code: str | None,
    summary: str | None, command_id: str | None, run_id: str | None,
    args: dict[str, Any] | None, duration_ms: int | None, exc_type: str | None = None,
) -> None:
    """标准记录入口：上下文存在且未 finished（HTTP 请求仍在处理）→ 排队，
    finish 时统一补 http_status/method/path/ip/user_agent 并落库；否则视为
    脱离 HTTP 请求的调用（agent 后台任务、脚本、测试直接调用），立即落库。
    """
    user_id, username, is_admin = _resolve_actor()
    ctx = _CTX.get()
    clean_args = args or {}
    summary_text = f"[{exc_type}] {summary or ''}".strip() if exc_type else summary
    row = {
        "id": new_id("opaudit"), "ts": db_now(),
        "user_id": user_id, "username": username, "is_system_admin": _as_int(is_admin),
        "source": source, "event": name, "event_label": title,
        "method": None, "path": None,
        "project_id": _scalar_str(clean_args.get("project_id")),
        "episode_id": _scalar_str(clean_args.get("episode_id")),
        "target": _target_from_args(clean_args),
        "outcome": status, "http_status": None,
        "error_id": ctx.error_id if ctx else None,
        "error_code": error_code or exc_type,
        "summary": _truncate(summary_text, _MAX_SUMMARY_CHARS),
        "duration_ms": duration_ms,
        "ip": ctx.ip if ctx else None, "user_agent": ctx.user_agent if ctx else None,
        "args_json": redact_and_truncate(clean_args),
    }
    if ctx is not None and not ctx.finished:
        ctx.bus_rows.append(row)
        return
    store.insert_operation_audit_row(row)


def record_bus_outcome(
    name: str, spec: Any, raw_args: Any, result: Any, exc: BaseException | None, nested: bool = False,
) -> None:
    """总线执行完成后的记录入口；由 ``app.capabilities.bus_audit.run_audited``/
    ``run_audited_sync`` 调用。

    ``spec``/``result`` 按 ``Any`` 处理（只做属性访问的鸭子类型），不 import
    ``app.capabilities.*`` 的枚举/dataclass 类型，维持本模块对高层模块零依赖。
    ``nested``（即 ``app.capabilities.direct.in_handler()``）为真表示这是
    handler 内部再次进入总线的嵌套调用，不重复记录——判断逻辑放在这里而不是
    调用侧，是因为 ``nested`` 的取值本身就该和其它记录字段一起在这一个函数
    里决定，不是行数腾挪的产物。
    """
    if nested:
        return
    title = getattr(spec, "title", None)
    args = _args_from_raw(raw_args)
    if exc is not None:
        record_command(
            name, title, current_source(), "error", None, str(exc), None, None,
            args, None, type(exc).__name__,
        )
        return
    status_value = getattr(getattr(result, "status", None), "value", "failed")
    record_command(
        name, title, current_source(), _COMMAND_STATUS_OUTCOME.get(status_value, "failed"),
        getattr(result, "error_code", None), getattr(result, "summary", None),
        getattr(result, "command_id", None), getattr(result, "run_id", None), args, None,
    )


def _resolve_actor() -> tuple[str | None, str | None, bool | None]:
    ctx = _CTX.get()
    if ctx is not None and ctx.actor_explicit:
        return ctx.actor_user_id, ctx.actor_username, ctx.actor_is_admin
    principal = get_current_principal()
    if principal is not None:
        if principal.user_id == "legacy-shared":
            return None, "legacy-shared", principal.is_system_admin
        return principal.user_id, principal.username, principal.is_system_admin
    return None, _SOURCE_ACTOR.get(), None


def _as_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit]


def _scalar_str(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _target_from_args(args: dict[str, Any]) -> str | None:
    """从入参顶层标量字段派生（不写字段白名单），排除幂等/批准/项目 id，累计截断。"""
    parts: list[str] = []
    total = 0
    for key, value in args.items():
        if key in _TARGET_EXCLUDED_KEYS or isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        piece = f"{key}={_truncate(str(value), _MAX_FIELD_CHARS)}"
        if total + len(piece) > _MAX_TARGET_CHARS:
            break
        parts.append(piece)
        total += len(piece) + 3
    return " · ".join(parts) if parts else None


def _args_from_raw(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    model_dump = getattr(raw_args, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
    return {}


def _should_emit_http_row(method: str, path: str) -> bool:
    if method not in _WRITE_METHODS or not path.startswith("/api/"):
        return False
    return path not in _EXCLUDED_HTTP_PATHS


def _label_from_docstring(doc: str | None) -> str | None:
    if not doc:
        return None
    text = doc.strip()
    if not text:
        return None
    cut = len(text)
    for sep in ("。", "；", "\n"):
        idx = text.find(sep)
        if idx != -1:
            cut = min(cut, idx)
    text = text[:cut].strip()
    return _truncate(text, _MAX_LABEL_CHARS) if text else None


def _target_from_path_params(path_params: dict[str, Any]) -> str | None:
    if not path_params:
        return None
    parts = [f"{k}={_truncate(str(v), _MAX_FIELD_CHARS)}" for k, v in path_params.items()]
    return _truncate(" · ".join(parts), _MAX_TARGET_CHARS)


def _http_outcome(status_code: int) -> str:
    if 200 <= status_code < 400:
        return "ok"
    if status_code in (401, 403):
        return "rejected"
    if 400 <= status_code < 500:
        return "failed"
    return "error"


def _build_http_row(request: Any, ctx: _HttpAuditContext, status_code: int) -> dict[str, Any]:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None) or ctx.path
    endpoint = getattr(route, "endpoint", None)
    user_id, username, is_admin = _resolve_actor()
    return {
        "id": new_id("opaudit"), "ts": db_now(),
        "user_id": user_id, "username": username, "is_system_admin": _as_int(is_admin),
        "source": current_source(), "event": f"{ctx.method} {route_path}",
        "event_label": _label_from_docstring(getattr(endpoint, "__doc__", None)),
        "method": ctx.method, "path": ctx.path,
        "project_id": None, "episode_id": None,
        "target": _target_from_path_params(dict(request.path_params or {})),
        "outcome": _http_outcome(status_code), "http_status": status_code,
        "error_id": ctx.error_id, "error_code": None,
        "summary": None, "duration_ms": int((time.time() - ctx.started_at) * 1000),
        "ip": ctx.ip, "user_agent": ctx.user_agent, "args_json": None,
    }


def _fill_http_fields(row: dict[str, Any], ctx: _HttpAuditContext, status_code: int) -> dict[str, Any]:
    row["http_status"] = status_code
    row["method"] = ctx.method
    row["path"] = ctx.path
    row["ip"] = row["ip"] or ctx.ip
    row["user_agent"] = row["user_agent"] or ctx.user_agent
    row["error_id"] = row["error_id"] or ctx.error_id
    row["duration_ms"] = int((time.time() - ctx.started_at) * 1000)
    return row
