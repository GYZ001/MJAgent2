from __future__ import annotations

import sqlite3

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
