"""场景发现追加了新场景之后，同一轮的第二遍解析必须看得见它（ERR-20260902-982a95）。

《三国演义》第二回：事件链给出裸地点「曲阳」「平原县」，第一遍解析不到，场景发现判为新场景并
写进人物谱（02:45:41 / 02:45:50）；但 `_resolve_assets` 里的 `bible` 快照只在人物发现之后
重读过、场景发现之后没有重读，第二遍解析的「场景卡已存在、只是没出图」豁免用的是旧快照，
新场景永远不在里面——本轮必失败，只能指望下一轮时后台已经出好图、scene_references 有了行。
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
            "characters": [], "scenes": [],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()
    return conn


def test_scene_added_by_discovery_is_resolved_in_the_same_attempt(monkeypatch) -> None:
    conn = _conn()
    calls: list[list[str]] = []

    async def fake_discover_new_scenes(conn_, *, project_id, episode_no, labels):
        # 与真实 ensure_scenes_for_labels 同样的副作用：把新场景写进 projects.bible_json。
        calls.append(list(labels))
        row = conn_.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        data = json.loads(row["bible_json"])
        data["scenes"].append({
            "name": "曲阳郊野场地",
            "scene_canonical": "东汉末年曲阳郊外室外场地，晴日柔和光影，衰草与低矮土丘，古朴写实。",
            "location_kind": "室外",
            "aliases": ["曲阳"],
        })
        conn_.execute("UPDATE projects SET bible_json=?, bible_version=bible_version+1 WHERE id=?",
                      (json.dumps(data, ensure_ascii=False), project_id))
        conn_.commit()
        return {"added": [{"name": "曲阳郊野场地"}], "errors": [], "resolved_names": {"曲阳": "曲阳郊野场地"}}

    patch_prep_pack_everywhere(monkeypatch, "_discover_new_scenes", fake_discover_new_scenes)

    characters, scenes, _props, _extras, errors, stats, *_rest = asyncio.run(prep_pack._resolve_assets(
        conn, project_id="p1", episode_id="ep-test", episode_no=2, source_text=SOURCE,
        character_mentions=[],
        scene_mentions=[{
            "display_name": "曲阳", "quote": "是时曹操自跟皇甫嵩讨张梁，大战于曲阳。",
            "segment_indexes": [1], "suspected_true_name": None,
        }],
        prop_mentions=[], run_id=None,
    ))

    assert calls == [["曲阳"]]
    assert errors == [], errors
    assert stats["scene_discovery_calls"] == 1
    assert [s["display_name"] for s in scenes] == ["曲阳郊野场地"]
    assert scenes[0]["scene_reference_id"] is None  # 出图在后台，本轮没有行是正常的
    assert scenes[0]["provenance"]["method"] == "discovery"
    assert scenes[0]["provenance"]["anchor_phrase"]
