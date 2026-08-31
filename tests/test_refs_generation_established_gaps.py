"""POST /projects/{id}/refs 在 characters 为空时的补图范围来源。

2026-08-31 架构转向后，人物只随映射台按需建卡，bible_json.characters 在项目
刚创建时恒为空——旧的「characters 为空就按 bible_json 取名单」会让这个按钮在
新架构下变成点了没反应的空转按钮。第一次修复改成反查 character_portraits
里的「已建卡角色」，只挑出其中缺图或出图失败的（``_established_portrait_gap_names``）。

那次修复本身留了一个更深的口子：换画风会把 character_portraits 整表清空，
而新架构下角色本就只随映射台按需建卡——「人物谱里有、但从未建卡」的角色对
一个只反查 character_portraits 的名单来源结构上不可见。实战撞到：换画风后
5 个角色只有 1 个被重新出图，用户点「补齐缺失定妆照」按钮后另外 4 个依旧
一张图都没有。2026-08-31 第二次修复把名单来源换成
``_incomplete_portrait_eligible_names``——与 refs_status 的产物判据同一口
径，覆盖人物谱里**全部**具备定妆资格的角色，不止「已建卡」的一部分。
``_established_portrait_gap_names`` 本身已删除（生产代码零调用方，与其留一
份文档撒谎的死函数，不如按 CLAUDE.md「Retiring Features」的要求一次删干
净）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.db import get_conn, new_id, now
from app.domain.bible_ops.refs_generation import (
    _incomplete_portrait_eligible_names,
    _refs_task,
)
from app.multiview import CHARACTER_REQUIRED_VIEWS


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


def _conn_with_bible(names: list[str]) -> sqlite3.Connection:
    conn = _conn()
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    bible_json = json.dumps({
        "characters": [
            {"name": n, "role": "配角", "appearance_canonical": f"{n}占位外观"}
            for n in names
        ],
    }, ensure_ascii=False)
    conn.execute("INSERT INTO projects(id, bible_json) VALUES('proj_test', ?)", (bible_json,))
    return conn


# ---------------------------------------------------------------------------
# _incomplete_portrait_eligible_names：既是 refs_status='ready' 的产物判据，
# 也是 POST /refs 在 characters 为空时的补图范围来源（同一份实现，两处
# 调用方复用，不重写第二份相似判据）。名单口径覆盖人物谱里**全部**具备定妆
# 资格的角色——包括从未在 character_portraits 出现过的角色。
# ---------------------------------------------------------------------------

def test_ready_character_with_all_views_is_not_incomplete() -> None:
    conn = _conn_with_bible(["甲一"])
    _insert_portrait(conn, "p1", "甲一", ep_start=5, ep_end=None, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter", "profile"])
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == []


def test_failed_pack_status_is_incomplete() -> None:
    conn = _conn_with_bible(["乙二"])
    _insert_portrait(conn, "p1", "乙二", ep_start=3, ep_end=None, pack_status="failed")
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == ["乙二"]


def test_missing_required_view_is_incomplete() -> None:
    conn = _conn_with_bible(["丙三"])
    _insert_portrait(conn, "p1", "丙三", ep_start=7, ep_end=None, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter"])  # profile 缺失
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == ["丙三"]


def test_only_obsolete_negative_ep_start_slot_counts_as_incomplete() -> None:
    """负数 ep_start 是 promote_staged_initial_portrait 压入的已作废历史槽位
    （ep_end 随之收口，不再是 NULL）。一个角色如果只有这类槽位，_character_
    pack_incomplete 的「当前采用包」查询（ep_end IS NULL）找不到任何行，视为
    无包、判定残缺——不会把作废的历史槽位误当成「有图」。"""
    conn = _conn_with_bible(["丁四"])
    _insert_portrait(conn, "p1", "丁四", ep_start=-1, ep_end=0, pack_status="ready")
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == ["丁四"]


def test_no_current_open_row_is_incomplete() -> None:
    """只有历史版本（ep_end 已收口）、没有当前采用版本（ep_end IS NULL）时，
    视为缺口——没有「当前实际会用的那张」。"""
    conn = _conn_with_bible(["戊五"])
    _insert_portrait(conn, "p1", "戊五", ep_start=1, ep_end=4, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter", "profile"])
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == ["戊五"]


def test_scopes_to_project() -> None:
    """另一个项目的完整定妆包不能跨项目冒充「本项目已有图」。"""
    conn = _conn_with_bible(["己六"])
    _insert_portrait(conn, "p1", "己六", ep_start=1, ep_end=None, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter", "profile"])
    conn.execute("UPDATE character_portraits SET project_id='proj_other' WHERE id='p1'")
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == ["己六"]


def test_mixed_eligible_characters_only_incomplete_ones_returned() -> None:
    conn = _conn_with_bible(["已就绪", "待补图"])
    _insert_portrait(conn, "ready1", "已就绪", ep_start=2, ep_end=None, pack_status="ready")
    _insert_views(conn, "ready1", ["front_full", "three_quarter", "profile"])
    _insert_portrait(conn, "gap1", "待补图", ep_start=6, ep_end=None, pack_status="generating")
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == ["待补图"]


def test_all_characters_ready_means_no_incomplete_names() -> None:
    conn = _conn_with_bible(["甲一", "乙二"])
    for pid, name in (("p1", "甲一"), ("p2", "乙二")):
        _insert_portrait(conn, pid, name, ep_start=1, ep_end=None, pack_status="ready")
        _insert_views(conn, pid, ["front_full", "three_quarter", "profile"])
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == []


def test_never_established_character_counts_as_incomplete() -> None:
    """核心回归锁：换画风把 character_portraits 整表清空后，5 个角色里只有 1
    个（甲一）被重新出图——其余 4 个从未出现在 character_portraits 里，
    「已建卡角色」口径看不见它们，但产物判据必须能看见。"""
    conn = _conn_with_bible(["甲一", "乙二", "丙三", "丁四", "戊五"])
    _insert_portrait(conn, "p1", "甲一", ep_start=1, ep_end=None, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter", "profile"])
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == [
        "乙二", "丙三", "丁四", "戊五",
    ]


def test_established_but_incomplete_pack_counts_as_incomplete() -> None:
    conn = _conn_with_bible(["甲一"])
    _insert_portrait(conn, "p1", "甲一", ep_start=1, ep_end=None, pack_status="ready")
    _insert_views(conn, "p1", ["front_full", "three_quarter"])  # profile 缺失
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == ["甲一"]


def test_no_bible_json_returns_empty_not_error() -> None:
    conn = _conn()
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute("INSERT INTO projects(id, bible_json) VALUES('proj_test', NULL)")
    conn.commit()

    assert _incomplete_portrait_eligible_names(conn, "proj_test") == []


# ---------------------------------------------------------------------------
# 端到端钉住：_refs_task 在没有显式指定 character(s)（POST /refs 不带
# characters 时的真实调用形态）时，必须把上面这份「全部具备定妆资格的角色
# 中尚无完整包」的名单原样传给 generate_refs，而不是退化成只看
# character_portraits 已有记录的旧口径。
# ---------------------------------------------------------------------------

def _make_real_project_with_bible(names: list[str]) -> str:
    conn = get_conn()
    project_id = new_id("proj")
    bible_json = json.dumps({
        "characters": [
            {"name": n, "role": "配角", "appearance_canonical": f"{n}占位外观"}
            for n in names
        ],
    }, ensure_ascii=False)
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json) "
        "VALUES(?,?,?,?,?)",
        (project_id, "P", "created", now(), bible_json),
    )
    conn.commit()
    return project_id


def test_refs_task_scopes_all_five_never_established_characters_when_called_bare(monkeypatch) -> None:
    """人物谱里 5 个合格角色，character_portraits 为空（换画风清空后的真实
    状态）。_refs_task(project_id, None) 就是 POST /refs 不带 characters 时
    的调用形态——必须把 5 个都排进 generate_refs 的 only_characters，而不是
    0 个（用户点补图按钮后什么都不会发生的那类故障）。"""
    import app.refs as refs_module

    names = ["孟浩", "王有材", "上官修", "赵武刚", "王腾飞"]
    project_id = _make_real_project_with_bible(names)
    captured: dict = {}

    async def fake_generate_refs(pid, only_character, *, only_characters=None, **_kw):
        captured["only_characters"] = only_characters

    monkeypatch.setattr(refs_module, "generate_refs", fake_generate_refs)

    asyncio.run(_refs_task(project_id, None))

    assert captured["only_characters"] is not None
    assert sorted(captured["only_characters"]) == sorted(names)
