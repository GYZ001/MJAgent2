"""场景判定的并发复核：快照后人物谱多了场景就用新名单重判，没变不重判（2026-09-06 第 11 轮三个广场）。"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.production.scene_refresh import refresh_verdict_if_scenes_changed
from app.schemas import Bible, Scene, World


def _conn(scene_names: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    bible = Bible(world=World(visual_style_canonical="国漫"), characters=[], scenes=[
        Scene(name=n, scene_canonical=f"{n}的固定环境描述", location_kind="室外") for n in scene_names
    ])
    conn.execute("INSERT INTO projects(id, bible_json) VALUES('p1', ?)", (json.dumps(bible.model_dump(), ensure_ascii=False),))
    conn.commit()
    return conn


def test_unchanged_scene_list_returns_original_verdict_without_reassessing() -> None:
    conn = _conn(["靠山宗广场"])
    snapshot = list(Bible.model_validate(json.loads(conn.execute("SELECT bible_json FROM projects").fetchone()["bible_json"])).scenes)
    calls = 0

    async def assess(_fresh):
        nonlocal calls
        calls += 1
        return {"name": "x"}

    verdict = {"name": "放丹的广场", "important": True}
    scenes, out = asyncio.run(refresh_verdict_if_scenes_changed(conn, "p1", snapshot, verdict, assess=assess))
    assert out is verdict and [s.name for s in scenes] == ["靠山宗广场"] and calls == 0


def test_new_scene_since_snapshot_triggers_one_reassessment_with_fresh_list() -> None:
    conn = _conn(["靠山宗广场", "东峰之路"])  # 「东峰之路」是别的并发集刚建的
    stale = [Scene(name="靠山宗广场", scene_canonical="广场", location_kind="室外")]
    seen: list[list[str]] = []

    async def assess(fresh):
        seen.append([s.name for s in fresh])
        return {"name": "东峰之路", "existing_scene_name": "东峰之路", "important": True}

    scenes, out = asyncio.run(refresh_verdict_if_scenes_changed(conn, "p1", stale, {"name": "东峰山路"}, assess=assess))
    assert seen == [["靠山宗广场", "东峰之路"]]
    assert out["existing_scene_name"] == "东峰之路" and [s.name for s in scenes] == ["靠山宗广场", "东峰之路"]
