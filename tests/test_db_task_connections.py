from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app import db


def test_async_connection_does_not_wait_synchronously_for_writer_lock(
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

    async def contend() -> tuple[sqlite3.OperationalError, float, int]:
        conn = db.get_conn()
        busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        started_at = time.monotonic()
        try:
            conn.execute("INSERT INTO events(id) VALUES('contender')")
        except sqlite3.OperationalError as exc:
            return exc, time.monotonic() - started_at, busy_timeout_ms
        raise AssertionError("contending write unexpectedly succeeded")

    try:
        error, elapsed, busy_timeout_ms = asyncio.run(contend())
    finally:
        writer.rollback()
        writer.close()

    assert (error.sqlite_errorcode & 0xFF) == sqlite3.SQLITE_BUSY
    assert busy_timeout_ms == 0
    assert elapsed < 0.5


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
