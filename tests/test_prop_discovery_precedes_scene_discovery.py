"""道具发现必须先于场景发现执行（2026-09-28 顺序修复）。

真实缺陷：《顾念长安（第二版）》proj_ca86b15ab7d7 EP1，剧情道具「小木星星」和
新场景「顾屿家客房」在同一次映射里一起产出。``app.production.scene_discovery_
assess.assess_new_scene`` 的场景卡/道具卡边界核验（见该模块 docstring）要从
``bible.props`` 读出"本次映射已经建卡的道具"，如果道具发现仍在场景发现之后才
执行，场景卡产出时这批道具压根还没落库，边界核验永远看不到同一次映射刚建的
卡。本测试只钉住调用顺序这一个结构事实，不重复 ``_resolve_assets``/
``app.scenes.ensure_scenes_for_labels`` 各自的判定逻辑（那些已有专门的测试）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.production import prep_pack
from tests.conftest import patch_prep_pack_everywhere

SOURCE = "是时曹操自跟皇甫嵩讨张梁，大战于曲阳。玄德引军前来助战。"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, ep_start INTEGER, ep_end INTEGER)"
    )
    conn.execute(
        "CREATE TABLE scene_references(id TEXT, project_id TEXT, scene_name TEXT, ep_start INTEGER, ep_end INTEGER)"
    )
    conn.execute(
        "CREATE TABLE episodes(id TEXT, project_id TEXT, episode_no INTEGER, source_chapters TEXT, screenplay_json TEXT)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES ('p1', ?, 1)",
        (json.dumps({
            "characters": [], "scenes": [], "props": [],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()
    return conn


def test_discover_new_props_runs_before_discover_new_scenes(monkeypatch) -> None:
    conn = _conn()
    call_order: list[str] = []

    async def fake_discover_new_props(
        conn_, *, project_id, episode_no, props_payload, source_text, cards_with_prior_evidence=frozenset(),
    ):
        call_order.append("props")
        return props_payload

    async def fake_discover_new_scenes(conn_, *, project_id, episode_no, labels, segments=None):
        call_order.append("scenes")
        return {"added": [], "errors": [], "resolved_names": {}}

    patch_prep_pack_everywhere(monkeypatch, "_discover_new_props", fake_discover_new_props)
    patch_prep_pack_everywhere(monkeypatch, "_discover_new_scenes", fake_discover_new_scenes)

    asyncio.run(prep_pack._resolve_assets(
        conn, project_id="p1", episode_id="ep-test", episode_no=2, source_text=SOURCE,
        character_mentions=[],
        scene_mentions=[{
            "display_name": "曲阳", "quote": "是时曹操自跟皇甫嵩讨张梁，大战于曲阳。",
            "segment_indexes": [1], "suspected_true_name": None,
        }],
        prop_mentions=[{"label": "占位道具", "description": "占位", "segment_indexes": [1]}],
        run_id=None,
    ))

    assert call_order == ["props", "scenes"], (
        "道具发现必须先于场景发现执行，否则本次映射同时产出的道具卡赶不上同一次场景边界核验"
    )
