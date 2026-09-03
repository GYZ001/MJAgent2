"""app.portraits.portrait_lookup：按锚点选段的定妆照/外观查询。

覆盖：
1. ``anchor_key`` 列懒迁移（幂等，多次调用不报错）。
2. 命中锚点造型时优先于集段判据，``look_mismatch`` 为 None。
3. 请求了锚点但未命中时回退集段判据，``look_mismatch`` 报告 "想要哪个/用了哪个"。
4. 完全没有可用定妆照时的空结果。
5. 不传 ``time_anchor`` 时行为与既有集段查询完全一致（不回归）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.portraits.portrait_lookup import _ensure_anchor_key_column, portrait_lookup_for_episode


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "visual_entity_id TEXT, ep_start INTEGER, ep_end INTEGER, appearance TEXT, image_path TEXT, "
        "created_at REAL)"
    )
    conn.commit()
    _ensure_anchor_key_column(conn)  # 懒迁移：测试库同样从"没有这一列"起步
    return conn


_SEQ = [0.0]


def _insert(conn, *, id_, name, ep_start, ep_end, appearance, image_path, anchor_key=None) -> None:
    _SEQ[0] += 1.0
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, image_path, anchor_key, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (id_, "p1", name, ep_start, ep_end, appearance, image_path, anchor_key, _SEQ[0]),
    )
    conn.commit()


def _real_file(tmp_path: Path, name: str) -> str:
    path = tmp_path / f"{name}.jpg"
    path.write_bytes(b"fake")
    return str(path)


def _has_anchor_column(conn) -> bool:
    return "anchor_key" in [row[1] for row in conn.execute("PRAGMA table_info(character_portraits)")]


def test_ensure_anchor_key_column_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER, appearance TEXT, image_path TEXT)"
    )
    conn.commit()
    assert not _has_anchor_column(conn)
    _ensure_anchor_key_column(conn)
    assert _has_anchor_column(conn)
    _ensure_anchor_key_column(conn)  # 第二次调用不应报错（duplicate column name 被吞掉）
    assert _has_anchor_column(conn)


def test_anchor_hit_takes_priority_over_episode_segment(tmp_path) -> None:
    conn = _make_conn()
    _insert(
        conn, id_="seg1", name="里奥", ep_start=1, ep_end=None,
        appearance="十六七岁少年", image_path=_real_file(tmp_path, "seg1"),
    )
    _insert(
        conn, id_="anchor1", name="里奥", ep_start=1, ep_end=None,
        appearance="八岁瘦小男孩，光脚踢球", image_path=_real_file(tmp_path, "anchor1"),
        anchor_key="age:8",
    )
    result = portrait_lookup_for_episode("p1", "里奥", 1, time_anchor="age:8", conn=conn)
    assert result["look_mismatch"] is None
    assert result["portrait_id"] == "anchor1"
    assert result["appearance"] == "八岁瘦小男孩，光脚踢球"


def test_anchor_miss_falls_back_to_episode_segment_with_mismatch_signal(tmp_path) -> None:
    conn = _make_conn()
    _insert(
        conn, id_="seg1", name="里奥", ep_start=1, ep_end=None,
        appearance="十六七岁少年", image_path=_real_file(tmp_path, "seg1"),
    )
    result = portrait_lookup_for_episode("p1", "里奥", 1, time_anchor="age:35", conn=conn)
    assert result["look_mismatch"] == {"wanted": "age:35", "used": "episode_segment"}
    assert result["portrait_id"] == "seg1"
    assert result["appearance"] == "十六七岁少年"


def test_no_portrait_at_all_returns_empty_result_with_mismatch(tmp_path) -> None:
    conn = _make_conn()
    result = portrait_lookup_for_episode("p1", "里奥", 1, time_anchor="age:35", conn=conn)
    assert result == {
        "image_path": None, "appearance": None, "portrait_id": None,
        "look_mismatch": {"wanted": "age:35", "used": "none"},
    }


def test_without_time_anchor_behaves_like_plain_episode_lookup(tmp_path) -> None:
    conn = _make_conn()
    _insert(
        conn, id_="seg1", name="里奥", ep_start=1, ep_end=None,
        appearance="十六七岁少年", image_path=_real_file(tmp_path, "seg1"),
    )
    result = portrait_lookup_for_episode("p1", "里奥", 1, conn=conn)
    assert result["look_mismatch"] is None
    assert result["portrait_id"] == "seg1"


def test_anchor_row_with_missing_file_is_treated_as_miss(tmp_path) -> None:
    conn = _make_conn()
    _insert(
        conn, id_="seg1", name="里奥", ep_start=1, ep_end=None,
        appearance="十六七岁少年", image_path=_real_file(tmp_path, "seg1"),
    )
    _insert(
        conn, id_="anchor1", name="里奥", ep_start=1, ep_end=None,
        appearance="八岁瘦小男孩", image_path=str(tmp_path / "missing.jpg"),
        anchor_key="age:8",
    )
    result = portrait_lookup_for_episode("p1", "里奥", 1, time_anchor="age:8", conn=conn)
    assert result["look_mismatch"] == {"wanted": "age:8", "used": "episode_segment"}
    assert result["portrait_id"] == "seg1"
