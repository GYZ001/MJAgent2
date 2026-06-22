"""漫剧 Agent 2.0 入口。启动：uvicorn app.main:app --port 8230"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import errors, worker
from app.api import purge_legacy_screenplays, recover_bible_tasks, recover_screenplay_tasks, router
from app.config import PROJECTS_DIR, ROOT
from app.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    purge_legacy_screenplays()
    worker.recover_and_start()
    recover_bible_tasks()  # 进程重启后续跑中断的人物谱任务，而非判孤儿报错
    recover_screenplay_tasks()  # 剧本热更后续跑，避免状态假 running 却无模型调用
    try:
        yield
    finally:
        # 取消常驻 worker，保证 reload/退出能干净停机，不卡在 "Waiting for connections to close"
        await worker.stop()


app = FastAPI(title="漫剧 Agent 2.0", lifespan=lifespan)


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
                ctx["body"] = json.loads(raw)
            except Exception:  # noqa: BLE001 非 JSON 体，截断存原文
                ctx["body"] = raw[:2000].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 取不到 body 不影响主流程
        pass
    return ctx


def _error_json(rec: errors.ErrorRecord, *, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=rec.http_status or 500,
        content={"detail": rec.public, "code": rec.code,
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
    return _error_json(rec, headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    ctx = await _request_context(request)
    rec = errors.log_error(
        exc, action=f"{request.method} {request.url.path}", context=ctx, http_status=500,
    )
    return _error_json(rec)


app.include_router(router)
app.mount("/media", StaticFiles(directory=PROJECTS_DIR), name="media")

frontend_dist = ROOT / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
