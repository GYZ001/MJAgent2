"""漫剧 Agent 2.0 入口。启动：uvicorn app.main:app --port 8230"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import worker
from app.api import router
from app.config import PROJECTS_DIR, ROOT
from app.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    worker.recover_and_start()
    yield


app = FastAPI(title="漫剧 Agent 2.0", lifespan=lifespan)
app.include_router(router)
app.mount("/media", StaticFiles(directory=PROJECTS_DIR), name="media")

frontend_dist = ROOT / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
