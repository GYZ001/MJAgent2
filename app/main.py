"""漫剧 Agent 2.0 入口。启动：uvicorn app.main:app --port 8230"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from app import errors, task_registry, worker
from app.agent.api import router as agent_conversation_router
from app.api import purge_legacy_screenplays, router
from app.audit import activity as audit_activity
from app.audit.api import router as audit_router
from app.audit.recorder import begin_http_request, finish_http_request, note_error_id
from app.audit.redact import _redact_sensitive as _redact_sensitive
from app.audit.redact import _SENSITIVE_KEYS as _SENSITIVE_KEYS
from app.audit.retention import operation_audit_sweep_loop
from app.audit.store import ensure_schema as ensure_audit_schema
from app.auth.admin_api import router as auth_admin_router
from app.auth.api import router as auth_router
from app.auth.deps import require_system_admin
from app.auth.principal import set_current_principal
from app.authz import require_project_owner_access
from app.config import PROJECTS_DIR, ROOT
from app.db import init_db
from app.mcp import router as mcp_router
from app.capabilities.bus import set_request_approval_token
from app.capabilities.loader import ensure_catalog_loaded
from app.local_session import (
    APPROVAL_HEADER,
    bind_request_principal,
    clear_principal_token,
    bind_verified_session,
    ensure_session_secret,
    public_session_payload,
    require_local_session,
    set_request_session_id,
)
from app.mcp.auth import ensure_bootstrap_token
from app.media_urls import media_ticket_required, verify_media_ticket
from app.planning import router as planning_router
from app.recovery import (
    acquire_runtime_recovery_lock,
    record_passive_instance,
    recover_all,
    release_runtime_recovery_lock,
)
from app.orchestration.api import router as orchestration_router
from app.observability.api import router as observability_router
from app.payments.routes import public_router as payments_public_router
from app.payments.routes import router as payments_router
from app.provider_task_zero_cost_api import router as provider_task_zero_cost_router
from app.system_api import public_router as system_public_router
from app.system_api import router as system_router

# app.db.init_db() looks up its per-table bootstrap/migration steps by name
# through app.db_schema instead of importing these business modules directly
# (P0-3 dependency inversion, see docs/coupling_review_2026-08-29.md 第2步).
# Each of these registers itself with app.db_schema at import time; something
# at the entry layer has to import them at least once before init_db() runs
# below, or the registry lookup raises KeyError. This is that one place for
# the running service (tests/conftest.py does the same for test isolation).
import app.artifacts  # noqa: F401
import app.completion_grant  # noqa: F401
import app.delivery  # noqa: F401
import app.model_migration  # noqa: F401
import app.production.certificate  # noqa: F401
import app.production.grant  # noqa: F401
import app.production.revision  # noqa: F401
import app.production.shot_uid  # noqa: F401

# 除 health / session 领取外，全部 /api/* 强制本机会话（Todolist T1）。
_SESSION_DEPS = [Depends(require_local_session)]
# RBAC 第四阶段：账号级项目归属隔离。必须排在 require_local_session 之后——
# require_project_owner_access 只读 get_current_principal()，执行到这里时
# Principal 必定已由中间件（bind_request_principal）解析好；session 闸门
# 自身没通过的请求也不会走到这一步。
_PROJECT_OWNER_DEPS = _SESSION_DEPS + [Depends(require_project_owner_access)]


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 热重载时新旧 worker 会短暂重叠；允许新 worker 有界等待旧锁释放。
    # 真正的第二实例超时后仍会按被动实例启动，不会中断主实例任务。
    recovery_owner = acquire_runtime_recovery_lock(wait_timeout_s=5.0)
    init_db(reconcile_interrupted=recovery_owner)
    ensure_audit_schema()
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
        # 软删除项目的回收站 24 小时自动彻底清理；只在恢复协调者实例上跑一份，
        # 避免热重载重叠的第二实例重复巡检同一批到期项目。
        from app.recovery import project_recycle_bin_sweep_loop
        task_registry.spawn(
            "system", "project_recycle_bin_sweep", project_recycle_bin_sweep_loop(),
        )
        # 软删除账号（管理员删账号）的回收站 30 天自动彻底清理；同一份恢复
        # 协调者独占逻辑，理由与上面的 project_recycle_bin_sweep 一致。
        from app.recovery import account_recycle_bin_sweep_loop
        task_registry.spawn(
            "system", "account_recycle_bin_sweep", account_recycle_bin_sweep_loop(),
        )
        # 已过期付费档位账号自动降级回 free + 裁剪超额项目；同一份恢复协调者
        # 独占逻辑，理由与上面两个回收站巡检一致。
        from app.recovery import expired_membership_sweep_loop
        task_registry.spawn(
            "system", "expired_membership_sweep", expired_membership_sweep_loop(),
        )
        # monitor_audit 独立连接抢不到写锁时的本地缓冲补写；同一份恢复协调者
        # 独占逻辑，避免两个实例同时截断同一份缓冲文件。
        from app.recovery import monitor_audit_flush_loop
        task_registry.spawn(
            "system", "monitor_audit_flush", monitor_audit_flush_loop(),
        )
        # operation_audit 365 天保留期巡检；同一份恢复协调者独占逻辑，理由同上。
        task_registry.spawn(
            "system", "operation_audit_sweep", operation_audit_sweep_loop(),
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
# compresslevel 6 而非默认 9：9 对这份产物只多省不到 1KB，CPU 却翻倍。
# 实测（frontend/dist，2026-08-25）：入口 JS 214K -> gzip3 75.4K / gzip6 68.4K，
# 入口 CSS 273K -> gzip3 59.2K / gzip6 48.7K，首屏关键路径合计少 17.5KB。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.middleware("http")
async def _inject_session_and_approval(request: Request, call_next):
    """注入本机会话与 approval_token，供 Command Bus 绑定批准令牌（Todolist T4）；
    同时开始/收尾本请求的操作审计上下文（app.audit.recorder）与最近活跃打点。
    """
    token = request.headers.get(APPROVAL_HEADER)
    set_request_approval_token(token)
    bind_verified_session(request)
    # Principal 必须在这里注入：同步依赖里写 ContextVar 会被 threadpool 丢弃，
    # 详见 local_session.bind_request_principal 的说明。
    principal = bind_request_principal(request)
    begin_http_request(request)
    if request.url.path.startswith("/api/"):
        audit_activity.touch(principal, request.url.path)
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        finish_http_request(request, response.status_code if response is not None else 500)
        set_request_approval_token(None)
        set_request_session_id(None)
        set_current_principal(None)
        clear_principal_token()


@app.get("/api/session")
def get_local_session(request: Request):
    """本机前端领取会话凭证。

    RBAC 第二阶段收紧：请求若带着一枚已登录的真实用户会话 token，原样确认
    返回（不下发共享秘密）；否则只在兼容开关开启期间（默认开启，Stage 8
    移除）继续下发旧的进程级共享秘密。开关关闭后没有有效用户会话一律 401——
    这正是本阶段要收紧的口子：不能再无条件把共享秘密发给任何同源请求。
    """
    from app.auth.sessions import resolve_session
    from app.local_session import (
        assert_session_bootstrap_allowed,
        extract_raw_session_token,
        legacy_shared_session_enabled,
    )

    assert_session_bootstrap_allowed(request)
    token = extract_raw_session_token(request)
    if token and resolve_session(token) is not None:
        return {"session_token": token, "header": "X-Manju-Session"}
    if legacy_shared_session_enabled():
        return public_session_payload()
    raise HTTPException(401, "缺少或无效的用户会话")


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
    note_error_id(rec.error_id)
    return _error_json(rec)


@app.exception_handler(HTTPException)
async def _on_http_exception(request: Request, exc: HTTPException):
    ctx = await _request_context(request)
    rec = errors.log_error(
        exc, action=f"{request.method} {request.url.path}", context=ctx,
        http_status=exc.status_code,
    )
    note_error_id(rec.error_id)
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
    note_error_id(rec.error_id)
    return _error_json(rec)


app.include_router(system_public_router)  # health 等公开探活，不要求会话
app.include_router(auth_router)  # /api/auth/*：login 本身必须公开，路由自身按需挂 session deps
app.include_router(auth_admin_router)  # /api/system/users：路由自身逐条挂 require_system_admin
app.include_router(audit_router)  # /api/system/audit/*：路由自身逐条挂 require_system_admin
app.include_router(payments_router)  # /api/payments/orders*：账号级自助购买，路由自身挂 require_local_session
app.include_router(payments_public_router)  # /api/payments/notify/*：渠道回调，公开端点，验签是唯一防线
app.include_router(router, dependencies=_PROJECT_OWNER_DEPS)
app.include_router(planning_router, dependencies=_PROJECT_OWNER_DEPS)
app.include_router(orchestration_router, dependencies=_PROJECT_OWNER_DEPS)
# 观测数据（任务/运行/调用原文/链路/证据产物）只对租户管理员开放：普通账号在前端
# 连入口都没有（frontend/src/appSections.ts 把观测台标成 adminOnly），这里是真正的闸门。
# 挂在 include_router 而不是 APIRouter(dependencies=...) 上，是因为
# tests/test_project_observability.py 把这个 router 单挂到裸 FastAPI 上跑项目归属
# 回归，那批用例没有会话中间件、拿不到 Principal；闸门写在挂载点，真实应用的每条
# 观测路由都被覆盖（tests/test_observability_admin_only.py 守着）。
app.include_router(
    observability_router,
    dependencies=_PROJECT_OWNER_DEPS + [Depends(require_system_admin)],
)
app.include_router(system_router, dependencies=_PROJECT_OWNER_DEPS)
app.include_router(provider_task_zero_cost_router, dependencies=_PROJECT_OWNER_DEPS)
# agent_conversation_router 的 require_local_session 由路由自身声明（见
# app/agent/api.py 的 APIRouter(dependencies=...)），这里只需再叠一层工作空间隔离。
app.include_router(agent_conversation_router, prefix="/api", dependencies=[Depends(require_project_owner_access)])
# /mcp 必须在 StaticFiles("/") 挂载之前注册，否则会被前端静态资源路由抢先吞掉。
# MCP 使用 Bearer Token，不叠本机会话闸门。
app.include_router(mcp_router)
# /media 曾经是零鉴权的裸 StaticFiles 挂载：/api/* 已经有工作空间隔离，但浏览器的
# <img>/<video> 标签不会带 X-Manju-Session 头，那套方案在结构上保护不了 /media，
# 凭据必须放进 URL 里（见 app/media_urls.py 的 build_media_url + mt= 票据）。
# 这里不能直接 app.mount(StaticFiles)，因为要在真正读文件前插一道票据校验；
# Range/If-Range/ETag/Last-Modified/HEAD/416 仍然全部复用 StaticFiles.get_response
# 本身的实现（与下面 SpaStaticFiles 同一手法），不手撸 Range 解析。
_media_static = StaticFiles(directory=PROJECTS_DIR)


@app.get("/media/{path:path}")
async def _serve_media(path: str, request: Request):
    """鉴权收口的 /media：路径穿越防护 + 按天分桶票据校验，其余行为与旧挂载一致。"""
    try:
        target = (PROJECTS_DIR / path).resolve()
        target.relative_to(PROJECTS_DIR.resolve())
    except ValueError:
        raise HTTPException(404, "Not Found")
    # MJ_MEDIA_REQUIRE_TICKET 默认关闭：关闭时只签发票据不校验，行为与改造前的
    # 裸 StaticFiles 挂载完全一致，避免打断本机正在跑的多小时回归；观察一段时间
    # 稳定后再打开，此时页面里早已渲染出的 URL 也都带着合法票据，不需要重新加载。
    if media_ticket_required():
        ticket = request.query_params.get("mt")
        if not verify_media_ticket(path, ticket):
            raise HTTPException(403, "无效或缺失的媒体访问票据")
    return await _media_static.get_response(path, request.scope)


class SpaStaticFiles(StaticFiles):
    """构建产物的静态服务：SPA 深链回落 + 指纹资源长缓存。

    前端是 path-based 路由（``/projects/p1/board``），裸 StaticFiles 对这类路径
    只会 404，刷新深链就白屏；此处未命中文件时回落 index.html。
    ``/api`` ``/media`` ``/mcp`` 不回落，避免不存在的接口返回 HTML 骗过前端错误处理。
    """

    # 与路由前缀一致；StaticFiles 传进来的 path 不带前导斜杠。
    #
    # assets/ 也必须在列：vite 产物带内容指纹，发一次版旧指纹就消失。老标签页在
    # 内存里还留着旧的模块图，点开某页时会去拉一个已经不存在的 chunk。若这里回落
    # index.html，浏览器收到的是 200 + text/html，模块加载器只会报
    # "'text/html' is not a valid JavaScript MIME type"——一句和真实原因无关的错，
    # 前端也认不出来。老老实实返回 404，前端才能识别成「分包没取到」并自动重载。
    _NO_FALLBACK = ("api/", "api", "media/", "media", "mcp/", "mcp", "assets/", "assets")

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith(self._NO_FALLBACK):
                raise
            response = await super().get_response("index.html", scope)
            path = "index.html"
        # vite 产物带内容指纹（index-3R6yEkIO.js），改一次内容换一次文件名，
        # 可以放心长缓存；index.html 必须每次回源，否则拿不到新指纹。
        if path.startswith("assets/") and response.status_code == 200:
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        elif "text/html" in response.headers.get("content-type", ""):
            # 不设 cache-control 时浏览器会启发式缓存（常按 last-modified 距今的 10%
            # 估算），移动端 Safari 尤其激进：外壳一旦被缓存，它引用的旧 chunk 又是
            # immutable，整个旧版应用会被永久钉死，发版永远到不了用户。
            # no-cache 允许缓存但强制用 ETag 回源校验，未变更时走 304，几乎无额外开销。
            response.headers["cache-control"] = "no-cache"
        return response


frontend_dist = ROOT / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", SpaStaticFiles(directory=frontend_dist, html=True), name="frontend")
