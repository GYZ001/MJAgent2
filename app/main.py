"""漫剧 Agent 2.0 入口。启动：uvicorn app.main:app --port 8230"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app import errors, task_registry, worker
from app.agent.api import router as agent_conversation_router
from app.agent_api import router as agent_capabilities_router
from app.api import purge_legacy_screenplays, router
from app.capabilities import ensure_catalog_loaded
from app.config import PROJECTS_DIR, ROOT
from app.db import init_db
from app.mcp import router as mcp_router
from app.capabilities.bus import set_request_approval_token
from app.local_session import (
    APPROVAL_HEADER,
    bind_verified_session,
    ensure_session_secret,
    public_session_payload,
    require_local_session,
    set_request_session_id,
)
from app.mcp.auth import ensure_bootstrap_token
from app.planning import router as planning_router
from app.recovery import (
    acquire_runtime_recovery_lock,
    record_passive_instance,
    recover_all,
    release_runtime_recovery_lock,
)
from app.orchestration.api import router as orchestration_router
from app.observability.api import router as observability_router
from app.system_api import public_router as system_public_router
from app.system_api import router as system_router

# 除 health / session 领取外，全部 /api/* 强制本机会话（Todolist T1）。
_SESSION_DEPS = [Depends(require_local_session)]


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 热重载时新旧 worker 会短暂重叠；允许新 worker 有界等待旧锁释放。
    # 真正的第二实例超时后仍会按被动实例启动，不会中断主实例任务。
    recovery_owner = acquire_runtime_recovery_lock(wait_timeout_s=5.0)
    init_db(reconcile_interrupted=recovery_owner)
    ensure_catalog_loaded()
    ensure_bootstrap_token()
    ensure_session_secret()
    if recovery_owner:
        purge_legacy_screenplays()
        await recover_all()
        from app.video_supervisor import video_supervisor_watchdog_loop
        task_registry.spawn(
            "system", "video_supervisor_watchdog", video_supervisor_watchdog_loop(),
        )
    else:
        record_passive_instance()
    try:
        yield
    finally:
        # 取消常驻 worker，保证 reload/退出能干净停机，不卡在 "Waiting for connections to close"
        await task_registry.stop_all()
        await worker.stop()
        if recovery_owner:
            release_runtime_recovery_lock()


app = FastAPI(title="漫剧 Agent 2.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=3)


@app.middleware("http")
async def _inject_session_and_approval(request: Request, call_next):
    """注入本机会话与 approval_token，供 Command Bus 绑定批准令牌（Todolist T4）。"""
    token = request.headers.get(APPROVAL_HEADER)
    set_request_approval_token(token)
    bind_verified_session(request)
    try:
        return await call_next(request)
    finally:
        set_request_approval_token(None)
        set_request_session_id(None)


@app.get("/api/session")
def get_local_session(request: Request):
    """本机前端领取会话秘密（无鉴权；仅本机 Origin/Host 可领取）。"""
    from app.local_session import assert_session_bootstrap_allowed

    assert_session_bootstrap_allowed(request)
    return public_session_payload()


_SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "password", "secret", "token", "access_token"}


def _redact_sensitive(value: Any) -> Any:
    """递归清理错误上下文，任何密钥都不得进入 error_logs。"""
    if isinstance(value, dict):
        return {key: ("***" if str(key).lower() in _SENSITIVE_KEYS else _redact_sensitive(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


async def _request_context(request: Request) -> dict[str, Any]:
    """抓取报错时的请求动作上下文（留后端日志，凭 error_id 可复盘）。"""
    ctx: dict[str, Any] = {
        "method": request.method,
        "path": request.url.path,
        "path_params": dict(request.path_params or {}),
        "query": dict(request.query_params or {}),
        "client": request.client.host if request.client else None,
    }
    try:
        # FastAPI 解析请求体时已缓存到 request._body，端点内抛错时通常可再取到。
        raw = await request.body()
        if raw:
            try:
                ctx["body"] = _redact_sensitive(json.loads(raw))
            except Exception:  # noqa: BLE001 非 JSON 体，截断存原文
                ctx["body"] = raw[:2000].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 取不到 body 不影响主流程
        pass
    return ctx


def _error_json(
    rec: errors.ErrorRecord,
    *,
    headers: dict | None = None,
    detail: Any | None = None,
) -> JSONResponse:
    public_detail: Any = detail if isinstance(detail, dict) else rec.public
    if isinstance(public_detail, dict):
        public_detail = {
            **public_detail,
            "error_ref": {
                "code": rec.code,
                "category": rec.category_label,
                "error_id": rec.error_id,
            },
        }
    return JSONResponse(
        status_code=rec.http_status or 500,
        content={"detail": public_detail, "code": rec.code,
                 "category": rec.category_label, "error_id": rec.error_id},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def _on_request_validation(request: Request, exc: RequestValidationError):
    ctx = await _request_context(request)
    rec = errors.log_error(
        exc, action=f"{request.method} {request.url.path}", context=ctx, http_status=422,
        message=json.dumps(exc.errors(), ensure_ascii=False, default=str),
        public_message="请求参数不合法，请检查必填项与字段类型",
    )
    return _error_json(rec)


@app.exception_handler(HTTPException)
async def _on_http_exception(request: Request, exc: HTTPException):
    ctx = await _request_context(request)
    rec = errors.log_error(
        exc, action=f"{request.method} {request.url.path}", context=ctx,
        http_status=exc.status_code,
    )
    return _error_json(
        rec,
        headers=getattr(exc, "headers", None),
        detail=exc.detail,
    )


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    ctx = await _request_context(request)
    rec = errors.log_error(
        exc, action=f"{request.method} {request.url.path}", context=ctx, http_status=500,
    )
    return _error_json(rec)


app.include_router(system_public_router)  # health 等公开探活，不要求会话
app.include_router(router, dependencies=_SESSION_DEPS)
app.include_router(planning_router, dependencies=_SESSION_DEPS)
app.include_router(orchestration_router, dependencies=_SESSION_DEPS)
app.include_router(observability_router, dependencies=_SESSION_DEPS)
app.include_router(system_router, dependencies=_SESSION_DEPS)
app.include_router(agent_capabilities_router, prefix="/api", dependencies=_SESSION_DEPS)
app.include_router(agent_conversation_router, prefix="/api")  # 路由自身已带 session deps
# /mcp 必须在 StaticFiles("/") 挂载之前注册，否则会被前端静态资源路由抢先吞掉。
# MCP 使用 Bearer Token，不叠本机会话闸门。
app.include_router(mcp_router)
app.mount("/media", StaticFiles(directory=PROJECTS_DIR), name="media")

frontend_dist = ROOT / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
