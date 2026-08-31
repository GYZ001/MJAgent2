"""POST /projects/{id}/refs 在 characters 为空时的补图范围来源。

2026-08-31 架构转向后，人物只随映射台按需建卡，bible_json.characters 在项目
刚创建时恒为空——旧的「characters 为空就按 bible_json 取名单」会让这个按钮在
新架构下变成点了没反应的空转按钮。改为直接从 character_portraits 反查
「已建卡角色」，并只挑出其中缺图或出图失败的（已有整包不重复出图/烧钱）。
"""
from __future__ import annotations

import sqlite3

from app.domain.bible_ops.refs_generation import _established_portrait_gap_names


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE character_portraits(
          id TEXT PRIMARY KEY,
          project_id TEXT,
          character_name TEXT,
          ep_start INTEGER,
          ep_end INTEGER,
          image_path TEXT,
          pack_status TEXT,
          created_at REAL DEFAULT 0
        );
        CREATE TABLE character_portrait_views(
          id TEXT PRIMARY KEY,
          portrait_id TEXT,
          view_role TEXT,
          image_path TEXT,
          status TEXT
        );
        """
    )
    return conn


def _insert_portrait(conn, portrait_id, name, *, ep_start, ep_end, pack_status) -> None:
    conn.execute(
        "INSERT INTO character_portraits(id,project_id,character_name,ep_start,ep_end,"
        "image_path,pack_status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (portrait_id, "proj_test", name, ep_start, ep_end, "/tmp/x.jpg", pack_status, 1.0),
    )


def _insert_views(conn, portrait_id, roles) -> None:
    for role in roles:
        conn.execute(
            "INSERT INTO character_portrait_views(id,portrait_id,view_role,image_path,status) "
            "VALUES(?,?,?,?,'ready')",
            (f"{portrait_id}-{role}", portrait_id, role, "/tmp/x.jpg"),
        )


def test_ready_character_with_all_views_is_not_a_gap() -> None:
    conn = _conn()
    _insert_portrait(conn, "p1", "甲一", ep_start=5, ep_end=None, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter", "profile"])
    conn.commit()

    assert _established_portrait_gap_names(conn, "proj_test") == []


def test_failed_pack_status_is_a_gap() -> None:
    conn = _conn()
    _insert_portrait(conn, "p1", "乙二", ep_start=3, ep_end=None, pack_status="failed")
    conn.commit()

    assert _established_portrait_gap_names(conn, "proj_test") == ["乙二"]


def test_missing_required_view_is_a_gap() -> None:
    conn = _conn()
    _insert_portrait(conn, "p1", "丙三", ep_start=7, ep_end=None, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter"])  # profile 缺失
    conn.commit()

    assert _established_portrait_gap_names(conn, "proj_test") == ["丙三"]


def test_only_obsolete_negative_ep_start_slot_is_not_established() -> None:
    """负数 ep_start 是 promote_staged_initial_portrait 压入的已作废历史槽位；
    一个角色如果只有这类槽位（没有任何 ep_start>=0 的记录），不该被当成
    「已建卡角色」纳入补图范围——纳入会把重做过定妆照的角色错误地算成多张。
    """
    conn = _conn()
    _insert_portrait(conn, "p1", "丁四", ep_start=-1, ep_end=0, pack_status="ready")
    conn.commit()

    assert _established_portrait_gap_names(conn, "proj_test") == []


def test_no_current_open_row_is_a_gap() -> None:
    """只有历史版本（ep_end 已收口）、没有当前采用版本（ep_end IS NULL）时，
    视为缺口——没有「当前实际会用的那张」。"""
    conn = _conn()
    _insert_portrait(conn, "p1", "戊五", ep_start=1, ep_end=4, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter", "profile"])
    conn.commit()

    assert _established_portrait_gap_names(conn, "proj_test") == ["戊五"]


def test_scopes_to_project() -> None:
    conn = _conn()
    _insert_portrait(conn, "p1", "己六", ep_start=1, ep_end=None, pack_status="failed")
    conn.execute(
        "UPDATE character_portraits SET project_id='proj_other' WHERE id='p1'",
    )
    conn.commit()

    assert _established_portrait_gap_names(conn, "proj_test") == []


def test_mixed_established_characters_only_gaps_returned() -> None:
    conn = _conn()
    _insert_portrait(conn, "ready1", "已就绪", ep_start=2, ep_end=None, pack_status="ready")
    _insert_views(conn, "ready1", ["front_full", "three_quarter", "profile"])
    _insert_portrait(conn, "gap1", "待补图", ep_start=6, ep_end=None, pack_status="generating")
    conn.commit()

    assert _established_portrait_gap_names(conn, "proj_test") == ["待补图"]
