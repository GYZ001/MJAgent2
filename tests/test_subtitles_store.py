"""``app.subtitles.store``：``subtitle_alignments`` 缓存表的懒建表与读写。

每个测试自动拿到独立克隆的 SQLite（``tests/conftest.py`` 的
``_reset_capability_runtime`` autouse fixture），不需要额外的数据库隔离样板；
``conn`` 一律用 ``app.db.get_conn()`` 取（线程/任务局部连接，见
``docs``/CLAUDE.md「get_conn 是线程/任务局部的」）。
"""
from __future__ import annotations

import pytest

from app.db import get_conn
from app.subtitles import store


def _result(text: str = "台词原话") -> dict:
    return {"text": text, "tokens": [[text[:1], 0.1]] if text else []}


def test_ensure_schema_is_idempotent():
    store.ensure_schema()
    store.ensure_schema()  # 第二次不应重跑 DDL 也不应报错
    conn = get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='subtitle_alignments'"
    ).fetchone()
    assert row is not None


def test_ensure_tables_adds_algo_version_column_to_pre_existing_table():
    """模拟"表已经在没有 algo_version 列的旧版本建过"：懒建表的 ``CREATE TABLE
    IF NOT EXISTS`` 对已存在的表不会补列，必须靠 ``_add_column_if_missing``
    幂等追加，否则老库升级后所有读写都会因缺列直接报错（见模块文档）。"""
    conn = get_conn()
    conn.execute("DROP TABLE IF EXISTS subtitle_alignments")
    conn.execute(
        """CREATE TABLE subtitle_alignments(
            shot_version_id TEXT PRIMARY KEY, media_sha256 TEXT NOT NULL, engine_id TEXT NOT NULL,
            model_id TEXT NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL
        )"""
    )
    conn.commit()

    store.ensure_tables_on_connection(conn)  # 同连接兜底入口，不走独立连接分支/不查 _ensured_paths 记忆

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(subtitle_alignments)").fetchall()}
    assert "algo_version" in cols


def test_get_alignment_returns_none_when_no_row():
    conn = get_conn()
    assert store.get_alignment(
        conn, shot_version_id="ver_missing", media_sha256="sha-a", model_id="model-a", algo_version="v1",
    ) is None


def test_put_then_get_hits():
    ok = store.put_alignment(
        shot_version_id="ver_1", media_sha256="sha-1", engine_id="sherpa-onnx/1.13.8",
        model_id="model-1", algo_version="v1", result=_result("你好世界"),
    )
    assert ok is True

    conn = get_conn()
    hit = store.get_alignment(conn, shot_version_id="ver_1", media_sha256="sha-1", model_id="model-1", algo_version="v1")
    assert hit == _result("你好世界")


@pytest.mark.parametrize("mismatch_field", ["shot_version_id", "media_sha256", "model_id", "algo_version"])
def test_get_alignment_none_when_any_key_mismatches(mismatch_field):
    store.put_alignment(
        shot_version_id="ver_2", media_sha256="sha-2", engine_id="sherpa-onnx/1.13.8",
        model_id="model-2", algo_version="v1", result=_result(),
    )
    keys = {"shot_version_id": "ver_2", "media_sha256": "sha-2", "model_id": "model-2", "algo_version": "v1"}
    keys[mismatch_field] = "wrong-value"

    conn = get_conn()
    assert store.get_alignment(conn, **keys) is None


def test_put_alignment_replaces_existing_row_for_same_shot_version():
    store.put_alignment(
        shot_version_id="ver_3", media_sha256="sha-old", engine_id="sherpa-onnx/1.13.8",
        model_id="model-3", algo_version="v1", result=_result("旧结果"),
    )
    store.put_alignment(
        shot_version_id="ver_3", media_sha256="sha-new", engine_id="sherpa-onnx/1.13.8",
        model_id="model-3", algo_version="v1", result=_result("新结果"),
    )

    conn = get_conn()
    assert store.get_alignment(conn, shot_version_id="ver_3", media_sha256="sha-old", model_id="model-3", algo_version="v1") is None
    hit = store.get_alignment(conn, shot_version_id="ver_3", media_sha256="sha-new", model_id="model-3", algo_version="v1")
    assert hit == _result("新结果")


# ---------------------------------------------------------------------------
# 调用方事务中途调用 ensure_schema() 不会毁掉调用方未提交的写
# 照 tests/test_lazy_schema_under_write_txn.py 的形状：先 BEGIN IMMEDIATE、写一
# 行未提交数据，再调用 ensure_schema()，断言事务未被提交/回滚、数据仍可见。
# ---------------------------------------------------------------------------


@pytest.fixture
def open_write_txn():
    conn = get_conn()
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.rollback()


def test_ensure_schema_under_caller_write_txn_preserves_uncommitted_write(open_write_txn):
    conn = open_write_txn
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)", ("subtitles_test_probe_key", "probe-value"),
    )
    assert conn.in_transaction

    store.ensure_schema()

    assert conn.in_transaction, "ensure_schema() 不应该提交/回滚调用方的事务"
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", ("subtitles_test_probe_key",),
    ).fetchone()
    assert row is not None and row["value"] == "probe-value"
    table_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='subtitle_alignments'"
    ).fetchone()
    assert table_row is not None, "subtitle_alignments 表没建成"


def test_get_alignment_under_caller_write_txn_does_not_raise_no_such_table(open_write_txn):
    conn = open_write_txn
    result = store.get_alignment(conn, shot_version_id="ver_x", media_sha256="sha-x", model_id="model-x", algo_version="v1")
    assert result is None
    assert conn.in_transaction
