"""漫剧 Agent 2.0 入口。启动：uvicorn app.main:app --port 8230"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import worker
from app.api import purge_legacy_screenplays, router
from app.config import PROJECTS_DIR, ROOT
from app.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    purge_legacy_screenplays()
    worker.recover_and_start()
    try:
        yield
    finally:
        # 取消常驻 worker，保证 reload/退出能干净停机，不卡在 "Waiting for connections to close"
        await worker.stop()


app = FastAPI(title="漫剧 Agent 2.0", lifespan=lifespan)
app.include_router(router)
app.mount("/media", StaticFiles(directory=PROJECTS_DIR), name="media")

frontend_dist = ROOT / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
