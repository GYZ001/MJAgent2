"""启动时观测表保留期清理不得因 provider_calls 自引用外键让后端起不来。

2026-10-01 B 机真实故障：部署后后端连续 95 次启动失败（FOREIGN KEY constraint failed），
一条刚过保留期的旧调用仍被一条未过期的重试记录 supersedes_call_id 引用，旧写法整表
DELETE 在语句结束时外键检查失败，init_db 抛错。见 app/observability/retention.py。
"""
from __future__ import annotations

import logging
import sqlite3
import time

from app import db
from app.observability import retention

_DAY = 86400


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE provider_calls(
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
          supersedes_call_id INTEGER, superseded_by_call_id INTEGER,
          FOREIGN KEY(supersedes_call_id) REFERENCES provider_calls(id),
          FOREIGN KEY(superseded_by_call_id) REFERENCES provider_calls(id)
        );
        CREATE TABLE error_logs(id TEXT PRIMARY KEY, ts REAL);
        INSERT INTO settings VALUES('provider_call_retention_days','30');
        INSERT INTO settings VALUES('error_log_retention_days','30');
        """
    )
    return conn


def _call(conn, call_id, *, age_days, status="OK", supersedes=None, superseded_by=None):
    conn.execute(
        "INSERT INTO provider_calls(id, ts, kind, status, supersedes_call_id, superseded_by_call_id) "
        "VALUES(?,?,?,?,?,?)",
        (call_id, time.time() - age_days * _DAY, "chat", status, supersedes, superseded_by),
    )


def _ids(conn) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM provider_calls ORDER BY id")]


def test_expired_call_still_referenced_by_retained_retry_is_kept_not_crash() -> None:
    """真实故障形状：1 号已过期、2 号未过期且 supersedes_call_id=1。旧写法在这里抛
    IntegrityError；新写法保留 1 号（等 2 号也过期后再一起删），不抛错。"""
    conn = _conn()
    _call(conn, 1, age_days=31)
    _call(conn, 2, age_days=29, supersedes=1)
    _call(conn, 3, age_days=40)
    retention.prune_observability_logs(conn)
    assert _ids(conn) == [1, 2]


def test_expired_chain_referenced_transitively_is_kept() -> None:
    """1←2←3：1、2 都过期，3 未过期且指向 2，2 又指向 1——整条链都要保留。"""
    conn = _conn()
    _call(conn, 1, age_days=50)
    _call(conn, 2, age_days=40, supersedes=1)
    conn.execute("UPDATE provider_calls SET superseded_by_call_id=2 WHERE id=1")
    _call(conn, 3, age_days=5, supersedes=2)
    retention.prune_observability_logs(conn)
    assert _ids(conn) == [1, 2, 3]


def test_fully_expired_chain_is_deleted_together_in_one_statement() -> None:
    """两条都过期、互相引用：必须在同一条语句里一起删掉，分开删会在中途触发外键检查。"""
    conn = _conn()
    _call(conn, 1, age_days=50)
    _call(conn, 2, age_days=45, supersedes=1)
    conn.execute("UPDATE provider_calls SET superseded_by_call_id=2 WHERE id=1")
    _call(conn, 3, age_days=1)
    retention.prune_observability_logs(conn)
    assert _ids(conn) == [3]


def test_running_call_and_what_it_references_are_kept() -> None:
    conn = _conn()
    _call(conn, 1, age_days=50)
    _call(conn, 2, age_days=45, status="RUNNING", supersedes=1)
    retention.prune_observability_logs(conn)
    assert _ids(conn) == [1, 2]


def test_retention_failure_is_logged_and_does_not_raise(caplog) -> None:
    """清理是后台整理，不是启动前提：失败只记带堆栈的错误日志，不再让后端起不来。"""
    conn = _conn()
    conn.execute("DROP TABLE error_logs")
    with caplog.at_level(logging.ERROR):
        retention.prune_observability_logs(conn)
    assert "[OBSERVABILITY_RETENTION_FAILED]" in caplog.text


def test_init_db_uses_the_extracted_retention_module() -> None:
    assert db._prune_observability_logs is retention.prune_observability_logs
