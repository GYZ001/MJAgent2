"""建表助手不得在调用方的事务上隐式提交。

第 24 集实测：合片发布在 ``BEGIN IMMEDIATE`` 锁内复核分镜权威，复核链路走到
``get_completion_certificate`` → ``ensure_completion_certificates_table()``，后者在
线程局部连接（与调用方同一条）上 ``commit()``，把发布事务提前结束、写锁放掉；另一
个发布者随即抢走租约，先到者的发布 CAS 冲突而失败。这里把同一族的建表助手全部
钉死：调用方已在事务里就绝不提交，只有自己开启的事务才由自己提交。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import db, monitoring
from app.domain import review_wall
from app.production import certificate, grant, revision


def _memory_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    return conn


_ENSURE_HELPERS = [
    certificate.ensure_completion_certificates_table,
    grant.ensure_production_grants_table,
    revision.ensure_production_revisions_table,
    review_wall._ensure_review_wall_tables,
]


@pytest.mark.parametrize("ensure", _ENSURE_HELPERS)
def test_ensure_table_keeps_callers_transaction_open(ensure) -> None:
    conn = _memory_database()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    ensure(conn)
    assert conn.in_transaction, "建表助手把调用方的事务提交掉了"
    conn.rollback()
    # 回滚真的撤销了调用方的写入，说明中途没有任何隐式提交。
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


@pytest.mark.parametrize("ensure", _ENSURE_HELPERS)
def test_ensure_table_commits_only_its_own_transaction(ensure) -> None:
    conn = _memory_database()
    ensure(conn)
    assert not conn.in_transaction


def test_certificate_lookup_without_conn_keeps_thread_local_transaction(monkeypatch) -> None:
    """线上事故形态：调用方没把 conn 传下去，助手自己 get_conn() 拿到的正是调用方
    那条线程局部连接。"""
    conn = _memory_database()
    monkeypatch.setattr(certificate, "get_conn", lambda: conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    assert certificate.get_completion_certificate("missing") is None
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_monitor_audit_table_keeps_thread_local_transaction(monkeypatch) -> None:
    conn = _memory_database()
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    monitoring.ensure_monitor_audit_table()
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    monitoring.ensure_monitor_audit_table()
    assert not conn.in_transaction
