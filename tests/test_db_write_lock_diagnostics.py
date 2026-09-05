"""写锁争用诊断：能点名握着未提交事务的 asyncio 任务，且心跳写入锁住时不阻塞。"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import db
from app.observability import provider_heartbeat, write_lock_holders


@pytest.mark.asyncio
async def test_open_write_holders_names_task_with_uncommitted_transaction(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "holders.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        conn = db.get_conn()
        conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")  # 未提交
        started.set()
        await release.wait()
        conn.commit()

    task = asyncio.create_task(holder(), name="holder-task")
    await started.wait()
    holders = write_lock_holders.open_write_holders()
    assert [h["task"] for h in holders] == ["holder-task"]
    assert any("holder" in f for h in holders for f in h["frames"])
    release.set()
    await task
    assert write_lock_holders.open_write_holders() == []


def test_progress_heartbeat_gives_up_instead_of_waiting_for_the_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "heartbeat.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    call_id = db.start_provider_call("chat", "m", meta={}, request_json={})
    other = sqlite3.connect(str(tmp_path / "heartbeat.db"))
    other.execute("BEGIN IMMEDIATE")  # 另一个连接握着写锁
    try:
        import time
        t0 = time.monotonic()
        provider_heartbeat.update_provider_call_progress(call_id, received_chars=10, chunk_at=1.0)
        assert time.monotonic() - t0 < 2.0, "心跳不得等 30 秒的 busy_timeout"
    finally:
        other.rollback(); other.close()
    row = db.get_conn().execute("SELECT received_chars FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    assert row["received_chars"] == 0  # 这一拍放弃了，没有写进去
    provider_heartbeat.update_provider_call_progress(call_id, received_chars=10, chunk_at=1.0)
    assert db.get_conn().execute("SELECT received_chars FROM provider_calls WHERE id=?", (call_id,)).fetchone()["received_chars"] == 10


def test_holders_older_than_counts_only_transactions_seen_across_ticks() -> None:
    from app.observability.write_lock_holders import holders_older_than

    seen: dict = {}
    a = {"task": "A", "coro": "", "frames": [], "last_sql": "UPDATE x"}
    b = {"task": "B", "coro": "", "frames": [], "last_sql": "INSERT y"}
    assert holders_older_than(100.0, seen, [a, b], threshold=8) == []          # 第一次看到：计时开始
    assert holders_older_than(105.0, seen, [a], threshold=8) == []             # B 已提交，从追踪里移除
    assert "B" not in seen
    aged = holders_older_than(109.0, seen, [a, b], threshold=8)                 # A 已 9 秒；B 重新开始计时
    assert [(h["task"], round(age)) for h, age in aged] == [("A", 9)]


def test_await_chain_frames_reach_the_suspended_await_point():
    """Task.get_stack() 对挂起协程只给最外层一帧，定位不到握着事务的具体 await；
    沿 cr_await 展开才能看到内层。"""
    import asyncio

    async def inner():
        await asyncio.sleep(10)

    async def outer():
        await inner()

    async def main():
        task = asyncio.create_task(outer())
        await asyncio.sleep(0.01)
        frames = write_lock_holders._await_chain_frames(task)
        task.cancel()
        return frames

    frames = asyncio.run(main())
    assert any(f.endswith(" outer") for f in frames)
    assert any(f.endswith(" inner") for f in frames)
    assert frames.index(next(f for f in frames if f.endswith(" outer"))) < frames.index(
        next(f for f in frames if f.endswith(" inner"))
    )
