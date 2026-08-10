from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

import app.storyboard_supervisor as supervisor
from app import db
from app.storyboard_supervisor import SupervisorCheckpoint


@pytest.mark.asyncio
async def test_projection_transaction_runs_off_loop_for_file_database(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "storyboard-async-write.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE projection(value TEXT NOT NULL)")
    conn.commit()
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def write_projection(write_conn) -> None:
        worker_threads.append(threading.get_ident())
        write_conn.execute("INSERT INTO projection(value) VALUES('committed')")

    await supervisor._run_storyboard_projection_transaction(
        conn,
        write_projection,
    )

    assert worker_threads and worker_threads[0] != event_loop_thread
    assert conn.execute("SELECT value FROM projection").fetchone()[0] == "committed"
    conn.close()


@pytest.mark.asyncio
async def test_projection_transaction_reuses_in_memory_connection() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projection(value TEXT NOT NULL)")
    connection_ids: list[int] = []

    def write_projection(write_conn) -> None:
        connection_ids.append(id(write_conn))
        write_conn.execute("INSERT INTO projection(value) VALUES('committed')")

    await supervisor._run_storyboard_projection_transaction(
        conn,
        write_projection,
    )

    assert connection_ids == [id(conn)]
    assert conn.execute("SELECT value FROM projection").fetchone()[0] == "committed"
    conn.close()


@pytest.mark.asyncio
async def test_checkpoint_and_event_run_off_loop_for_file_database(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "storyboard-checkpoint-write.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE marker(value TEXT)")
    conn.commit()
    event_loop_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []

    def fake_event(*_args, **_kwargs) -> str:
        calls.append(("event", threading.get_ident()))
        return "evt-1"

    def fake_checkpoint(*_args, **_kwargs) -> str:
        calls.append(("checkpoint", threading.get_ident()))
        return "checkpoint-1"

    monkeypatch.setattr(
        supervisor.evidence_repository,
        "append_event",
        fake_event,
    )
    monkeypatch.setattr(supervisor, "save_checkpoint", fake_checkpoint)

    await supervisor._persist_high_frequency_checkpoint(
        conn,
        SupervisorCheckpoint(episode_id="e1"),
        run_id="run-1",
        event_type="SHOT_CHECKPOINT_VALIDATED",
        severity="info",
        message="第 1 镜已通过",
        payload={"shot_no": 1},
    )

    assert [name for name, _thread_id in calls] == ["event", "checkpoint"]
    assert all(thread_id != event_loop_thread for _name, thread_id in calls)
    conn.close()


@pytest.mark.asyncio
async def test_cancelled_checkpoint_waits_for_writer_before_propagating(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "storyboard-cancelled-checkpoint.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE marker(value TEXT)")
    conn.commit()
    checkpoint_started = threading.Event()
    checkpoint_release = threading.Event()
    checkpoint_finished = threading.Event()
    commits: list[str] = []

    def blocking_checkpoint(*_args, **_kwargs) -> str:
        checkpoint_started.set()
        checkpoint_release.wait()
        commits.append("checkpoint")
        checkpoint_finished.set()
        return "checkpoint-1"

    monkeypatch.setattr(supervisor, "save_checkpoint", blocking_checkpoint)
    task = asyncio.create_task(supervisor._persist_high_frequency_checkpoint(
        conn,
        SupervisorCheckpoint(episode_id="e1"),
        run_id=None,
        event_type="SHOT_CHECKPOINT_VALIDATED",
        severity="info",
        message="第 1 镜已通过",
    ))

    try:
        assert await asyncio.to_thread(checkpoint_started.wait, 0.5)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert not checkpoint_finished.is_set()
        checkpoint_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        checkpoint_release.set()
        conn.close()

    assert checkpoint_finished.is_set()
    assert commits == ["checkpoint"]
    await asyncio.sleep(0.01)
    assert commits == ["checkpoint"]
