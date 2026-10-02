"""refs_progress 面板与 compute_refs_precheck(resume=True) 对「存量角色只有
旧视角、没有 face_closeup」这一真实落库形态的判定。

2026-10-01 定妆照双视角改造把 CHARACTER_REQUIRED_VIEWS 从三视角改成
(front_full, face_closeup)。改造前生产里全部已采纳角色都只有旧三视角（或
单独 front_full），没有 face_closeup——这是 100% 存量角色的真实快照形态。
如果 refs_progress 与 compute_refs_precheck 的 resume 分支仍按
CHARACTER_REQUIRED_VIEWS 判断「是否完整」，这类角色会被整行标红成
missing、被「补齐缺失的定妆照」按钮的默认范围圈入，点击后对全部存量角色
发起一次真实付费出图——与「存量项目已有的定妆包不得被任何闸门判不可用」
直接矛盾。两个入口都必须改用 CHARACTER_PRODUCTION_REQUIRED_VIEWS（只要
front_full）判定 ready/missing，这里用仓库里真实存量快照形状锁住。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.domain import bible_ops
from tests.conftest import patch_api_everywhere


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects(
          id TEXT PRIMARY KEY,
          bible_json TEXT,
          bible_version INTEGER DEFAULT 0,
          bible_artifact_id TEXT,
          bible_status TEXT DEFAULT 'idle',
          refs_status TEXT DEFAULT 'idle',
          refs_error TEXT,
          refs_target TEXT
        );
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


def _seed_legacy_character(conn: sqlite3.Connection, name: str, *, views: list[str]) -> None:
    bible = {
        "world": {"visual_style_canonical": "国风", "era": "古代", "genre": "玄幻"},
        "characters": [{
            "name": name, "role": "主角",
            "appearance_canonical": f"{name}占位外观，黑发劲装，目光坚定，身形修长",
            "personality": "坚韧", "speech_style": "沉稳", "relationships": [],
        }],
        "scenes": [],
    }
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_artifact_id, bible_status, refs_status) "
        "VALUES('proj_test', ?, 1, 'art_bible_1', 'ready', 'ready')",
        (json.dumps(bible, ensure_ascii=False),),
    )
    conn.execute(
        "INSERT INTO character_portraits(id,project_id,character_name,ep_start,ep_end,"
        "image_path,pack_status,created_at) VALUES('p1','proj_test',?,1,NULL,'/tmp/x.jpg','ready',1.0)",
        (name,),
    )
    for role in views:
        conn.execute(
            "INSERT INTO character_portrait_views(id,portrait_id,view_role,image_path,status) "
            "VALUES(?,?,?,?,'ready')",
            (f"p1-{role}", "p1", role, "/tmp/x.jpg"),
        )
    conn.commit()


def test_refs_progress_reports_ready_for_legacy_front_full_only_pack(monkeypatch) -> None:
    conn = _conn()
    _seed_legacy_character(conn, "甲一", views=["front_full", "three_quarter", "profile"])
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))
    patch_api_everywhere(monkeypatch, "_refs_generation_busy", lambda _pid: False)

    progress = asyncio.run(bible_ops.refs_progress("proj_test"))
    assert progress["ready"] == 1
    assert progress["missing"] == 0
    by_status = {item["character"]: item["status"] for item in progress["items"]}
    assert by_status == {"甲一": "ready"}


def test_refs_progress_still_flags_missing_front_full(monkeypatch) -> None:
    """front_full 本身缺失（哪怕 face_closeup 已经有了）才是真缺口——不能把
    判据改松到对任何缺口都不敏感。"""
    conn = _conn()
    _seed_legacy_character(conn, "乙二", views=["face_closeup"])
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))
    patch_api_everywhere(monkeypatch, "_refs_generation_busy", lambda _pid: False)

    progress = asyncio.run(bible_ops.refs_progress("proj_test"))
    assert progress["missing"] == 1
    assert progress["ready"] == 0


def test_refs_precheck_resume_excludes_legacy_front_full_only_pack(monkeypatch) -> None:
    conn = _conn()
    _seed_legacy_character(conn, "甲一", views=["front_full", "three_quarter", "profile"])
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    quote = bible_ops.compute_refs_precheck("proj_test", resume=True)
    assert quote["character_count"] == 1
    assert quote["image_count"] == 0
    assert quote["scope"] == []
