from __future__ import annotations

import sqlite3

import pytest

from app.evidence import repository


class _LockedConnection:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0
        self.commits = 0
        self.rollbacks = 0

    def execute(self, *_args, **_kwargs):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise sqlite3.OperationalError("database is locked")
        return self

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_append_event_retries_transient_database_lock(monkeypatch) -> None:
    conn = _LockedConnection(failures=2)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(repository, "new_id", lambda _prefix: "evt_retry")
    monkeypatch.setattr(repository.time, "sleep", lambda _delay: None)

    event_id = repository.append_event("run_1", "STEP", "info", "ok")

    assert event_id == "evt_retry"
    assert conn.attempts == 3
    assert conn.rollbacks == 2
    assert conn.commits == 1


def test_append_event_drops_only_observation_after_persistent_lock(monkeypatch) -> None:
    conn = _LockedConnection(failures=10)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(repository.time, "sleep", lambda _delay: None)

    event_id = repository.append_event("run_1", "STEP", "warning", "busy")

    assert event_id == ""
    assert conn.attempts == 4
    assert conn.rollbacks == 4
    assert conn.commits == 0


@pytest.mark.asyncio
async def test_async_append_event_retries_with_async_sleep(monkeypatch) -> None:
    conn = _LockedConnection(failures=0)
    attempts = 0
    sleeps: list[float] = []

    async def run_write(operation, *, retry_delays):
        nonlocal attempts
        assert retry_delays == ()
        attempts += 1
        if attempts <= 2:
            raise sqlite3.OperationalError("database is locked")
        return operation(conn)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(repository, "run_write_transaction", run_write)
    monkeypatch.setattr(repository.asyncio, "sleep", sleep)
    monkeypatch.setattr(repository, "new_id", lambda _prefix: "evt_async_retry")

    event_id = await repository.async_append_event(
        "run_1", "STEP", "info", "ok"
    )

    assert event_id == "evt_async_retry"
    assert attempts == 3
    assert sleeps == list(repository._EVENT_LOCK_RETRY_DELAYS_S[:2])
    assert conn.attempts == 1


@pytest.mark.asyncio
async def test_async_append_event_drops_after_persistent_lock(monkeypatch) -> None:
    attempts = 0

    async def run_write(_operation, *, retry_delays):
        nonlocal attempts
        assert retry_delays == ()
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    async def sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(repository, "run_write_transaction", run_write)
    monkeypatch.setattr(repository.asyncio, "sleep", sleep)

    event_id = await repository.async_append_event(
        "run_1", "STEP", "warning", "busy"
    )

    assert event_id == ""
    assert attempts == len(repository._EVENT_LOCK_RETRY_DELAYS_S) + 1
