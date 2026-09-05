"""进度树异步落盘：写锁被别人握着时等的是 asyncio.sleep（不冻结事件循环），锁释放后写成功。"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app import db
from app.domain.series_ops import state


@pytest.mark.asyncio
async def test_persist_progress_async_waits_without_blocking_loop_then_writes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "progress.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO series_tasks(id, project_id, title, episode_from, episode_to, status,
           progress_json, created_at, updated_at) VALUES ('st_a','p','',1,1,'running','{}',0,0)"""
    )
    conn.commit()
    other = sqlite3.connect(str(tmp_path / "progress.db"))
    other.execute("BEGIN IMMEDIATE")  # 别人握着写锁
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.02)
            ticks += 1

    async def release_later() -> None:
        await asyncio.sleep(0.3)
        other.rollback()

    progress = state.new_progress([state.new_episode_entry("e1", 1)])
    progress["episodes"][0]["stages"]["screenplay"] = "running"
    t0 = time.monotonic()
    ok, *_ = await asyncio.gather(state.persist_progress_async("st_a", progress), heartbeat(), release_later())
    other.close()
    assert ok is True
    assert ticks >= 10, "等锁期间事件循环必须还在转（心跳协程要能推进）"
    assert time.monotonic() - t0 < 5
    row = db.get_conn().execute("SELECT progress_json FROM series_tasks WHERE id='st_a'").fetchone()
    assert '"running_episode_nos": [1]' in row["progress_json"]


@pytest.mark.asyncio
async def test_open_write_holders_reports_last_uncommitted_statement(tmp_path, monkeypatch) -> None:
    from app.observability import write_lock_holders

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "holders.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        conn = db.get_conn()
        conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p2','P',0)")
        started.set()
        await release.wait()
        conn.commit()

    task = asyncio.create_task(holder(), name="holder-task")
    await started.wait()
    holders = write_lock_holders.open_write_holders()
    assert holders and holders[0]["last_sql"].startswith("INSERT INTO projects")
    release.set()
    await task
