"""provider_calls 账本写入护栏（2026-09-05）：任何异常都不得把开着的事务壳留在
任务连接上——B 上定妆照/分镜素材任务各握着 provider_calls 事务 3–5 分钟，整个
事件循环等锁。锁错误 best-effort 吞掉；其它异常回滚后照常抛出。"""

from __future__ import annotations

import sqlite3

import pytest

from app import db as db_mod


def _open_txn(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS _guard_probe(x INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO _guard_probe(x) VALUES(1)")
    assert conn.in_transaction


def test_bookkeeping_rolls_back_and_reraises_non_lock_errors():
    conn = db_mod.get_conn()

    def boom():
        _open_txn(conn)
        raise ValueError("记账钩子炸了")

    with pytest.raises(ValueError):
        db_mod._bookkeeping(conn, boom, default=None)
    assert not conn.in_transaction, "异常之后不得留下开着的事务壳"


def test_bookkeeping_swallows_lock_errors_and_rolls_back():
    conn = db_mod.get_conn()

    def locked():
        _open_txn(conn)
        raise sqlite3.OperationalError("database is locked")

    assert db_mod._bookkeeping(conn, locked, default=0) == 0
    assert not conn.in_transaction


def test_finish_provider_call_never_leaves_transaction_open(monkeypatch):
    conn = db_mod.get_conn()

    def fake_inner(c, *_args, **_kwargs):
        _open_txn(c)
        raise TypeError("response_json 形状不对")

    monkeypatch.setattr(db_mod, "_finish_provider_call_inner", fake_inner)
    with pytest.raises(TypeError):
        db_mod.finish_provider_call(7, "OK", 200, 12)
    assert not conn.in_transaction

