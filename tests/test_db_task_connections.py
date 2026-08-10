from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from app import db


def test_async_write_retries_until_lock_release_without_blocking_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "async-writer-lock.db"
    writer = sqlite3.connect(database_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE events(id TEXT PRIMARY KEY)")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(db, "DB_PATH", database_path)

    async def contend() -> tuple[int, int]:
        heartbeat_ticks = 0
        started_at = time.monotonic()
        write_task = asyncio.create_task(db.run_write_transaction(
            lambda conn: conn.execute(
                "INSERT INTO events(id) VALUES('contender')"
            ).rowcount,
            retry_delays=(0.01, 0.02, 0.04, 0.08, 0.16),
        ))
        while time.monotonic() - started_at < 0.06:
            heartbeat_ticks += 1
            await asyncio.sleep(0.005)
        assert write_task.done() is False
        writer.rollback()
        rowcount = await asyncio.wait_for(write_task, timeout=1)
        return heartbeat_ticks, rowcount

    try:
        heartbeat_ticks, rowcount = asyncio.run(contend())
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()

    verification = sqlite3.connect(database_path)
    try:
        rows = verification.execute("SELECT id FROM events").fetchall()
    finally:
        verification.close()

    assert heartbeat_ticks >= 5
    assert rowcount == 1
    assert rows == [("contender",)]


def test_async_write_does_not_retry_non_lock_operational_error(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "non-lock-error.db")

    async def fail() -> None:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await db.run_write_transaction(
                lambda conn: conn.execute("INSERT INTO missing VALUES(1)"),
                retry_delays=(0, 0),
            )

    asyncio.run(fail())


def test_async_write_cancellation_waits_for_transaction_to_finish(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "cancelled-write.db"
    setup = sqlite3.connect(database_path)
    setup.execute("CREATE TABLE events(id TEXT PRIMARY KEY)")
    setup.commit()
    setup.close()
    monkeypatch.setattr(db, "DB_PATH", database_path)
    write_started = threading.Event()
    write_release = threading.Event()

    def delayed_write(conn: sqlite3.Connection) -> None:
        write_started.set()
        write_release.wait()
        conn.execute("INSERT INTO events(id) VALUES('committed-before-cancel')")

    async def exercise() -> None:
        task = asyncio.create_task(db.run_write_transaction(delayed_write))
        while not write_started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        write_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        write_release.set()

    verification = sqlite3.connect(database_path)
    try:
        rows = verification.execute("SELECT id FROM events").fetchall()
    finally:
        verification.close()
    assert rows == [("committed-before-cancel",)]


def test_async_tasks_have_isolated_transactions_and_release_connections(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "task-isolation.db"
    setup = sqlite3.connect(database_path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE events(id TEXT PRIMARY KEY)")
    setup.commit()
    setup.close()
    monkeypatch.setattr(db, "DB_PATH", database_path)

    async def scenario() -> tuple[sqlite3.Connection, sqlite3.Connection]:
        owner_ready = asyncio.Event()
        observer_ready = asyncio.Event()
        owner_started = asyncio.Event()
        observer_committed = asyncio.Event()

        async def owner() -> sqlite3.Connection:
            conn = db.get_conn()
            owner_ready.set()
            await observer_ready.wait()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO events(id) VALUES('owner-uncommitted')")
            owner_started.set()
            await observer_committed.wait()
            assert conn.in_transaction is True
            conn.rollback()
            return conn

        async def observer() -> sqlite3.Connection:
            conn = db.get_conn()
            observer_ready.set()
            await owner_ready.wait()
            await owner_started.wait()
            conn.commit()
            observer_committed.set()
            return conn

        owner_task = asyncio.create_task(owner())
        observer_task = asyncio.create_task(observer())
        owner_conn, observer_conn = await asyncio.gather(
            owner_task,
            observer_task,
        )
        await asyncio.sleep(0)
        return owner_conn, observer_conn

    owner_conn, observer_conn = asyncio.run(scenario())

    assert owner_conn is not observer_conn
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        owner_conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observer_conn.execute("SELECT 1")
    verification = sqlite3.connect(database_path)
    try:
        assert verification.execute("SELECT id FROM events").fetchall() == []
    finally:
        verification.close()
