"""定妆照候选行插入撞唯一约束时必须改用已存在的行，而不是把整集分镜打死。

生产根因（2026-09-03 橘座在上「张姐」）：映射台角色参考图任务与分镜台人物谱补图
重试并行给同一个新角色出图，后落盘的一方 ``UNIQUE(project_id, character_name,
ep_start)`` 失败，``retry_auto_character_portrait`` 直接抛错。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.portraits.portrait_insert import insert_portrait_row_or_existing


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE character_portraits(
               id TEXT PRIMARY KEY, project_id TEXT, character_name TEXT,
               ep_start INTEGER, ep_end INTEGER, image_path TEXT, created_at TEXT,
               UNIQUE(project_id, character_name, ep_start))"""
    )
    return conn


def _values(portrait_id: str, created_at: str) -> dict:
    return {
        "id": portrait_id, "project_id": "p1", "character_name": "张姐",
        "ep_start": 1, "ep_end": 1, "image_path": f"/tmp/{portrait_id}.png", "created_at": created_at,
    }


def test_fresh_slot_inserts_and_returns_own_row() -> None:
    conn = _conn()
    row = insert_portrait_row_or_existing(conn, _values("por_a", "2026-09-03T17:12:25"))
    assert row["id"] == "por_a"
    assert conn.execute("SELECT count(*) FROM character_portraits").fetchone()[0] == 1


def test_taken_slot_returns_existing_row_instead_of_raising() -> None:
    conn = _conn()
    insert_portrait_row_or_existing(conn, _values("por_first", "2026-09-03T17:12:25"))
    row = insert_portrait_row_or_existing(conn, _values("por_second", "2026-09-03T17:13:40"))
    assert row["id"] == "por_first"
    assert conn.execute("SELECT count(*) FROM character_portraits").fetchone()[0] == 1
    assert not conn.in_transaction


def test_non_slot_integrity_error_still_raises() -> None:
    """只有槽位冲突才接管；其它完整性错误（这里是主键重复但槽位不同）照样抛出。"""
    conn = _conn()
    insert_portrait_row_or_existing(conn, _values("por_dup", "2026-09-03T17:12:25"))
    other = _values("por_dup", "2026-09-03T17:13:40")
    other["character_name"] = "李姐"
    with pytest.raises(sqlite3.IntegrityError):
        insert_portrait_row_or_existing(conn, other)
