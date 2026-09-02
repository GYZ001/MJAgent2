"""场景圣经落库不得覆盖并发追加的反应式场景（2026-09-02《神墓》proj_facfc3964f69）。

时序：项目级场景圣经任务 01:04:15 开跑；本集映射的反应式场景发现在 01:07:13 与
01:07:20 追加「上古神魔陵园」「冬日积雪雪枫林」（人物谱 v3、v4）；01:07:21 场景圣经
任务对 ``scenes`` 整体替换回写（v5：12 个规划场景，两条反应式场景消失）。随后两轮
映射都因「场景未解析到 scene_reference_id」失败。本文件钉住：规划清单合并进库、
已有场景保留、同名合并别名，以及版本号乐观锁的失败关闭。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.domain.bible_ops.scene_bible_prep import (
    _PLANNED_SCENES_CAS_ATTEMPTS,
    _merge_planned_scenes,
    _persist_planned_scenes,
)
from app.schemas import Bible, World


def _scene(name: str, aliases: list[str] | None = None) -> dict:
    return {
        "name": name,
        "scene_canonical": "室外旷野处的上古陵园，黄昏斜照的暖金冷灰光影，风化石墓碑林立，肃穆苍凉。",
        "location_kind": "室外",
        "aliases": list(aliases or []),
    }


def _conn(bible: dict, version: int = 4) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER, "
        "scene_refs_status TEXT, scene_refs_error TEXT)"
    )
    conn.execute(
        "INSERT INTO projects VALUES(?,?,?,?,?)",
        ("proj_x", json.dumps(bible, ensure_ascii=False), version, "running", "老错误"),
    )
    conn.commit()
    return conn


def _bible_dict(*scenes: dict) -> dict:
    return {
        "characters": [],
        "world": {"visual_style_canonical": "国漫电影风"},
        "scenes": list(scenes),
    }


def test_merge_keeps_reactive_scenes_absent_from_plan_and_unions_aliases() -> None:
    planned = [_scene("白日神魔陵园"), _scene("交界小镇街巷", ["小镇"])]
    existing = [
        _scene("上古神魔陵园", ["神魔陵园"]),
        _scene("交界小镇街巷", ["边境小镇"]),
        _scene("冬日积雪雪枫林", ["雪枫林"]),
    ]

    merged = _merge_planned_scenes(planned, existing)

    assert [s["name"] for s in merged] == [
        "白日神魔陵园", "交界小镇街巷", "上古神魔陵园", "冬日积雪雪枫林",
    ]
    by_name = {s["name"]: s for s in merged}
    assert by_name["交界小镇街巷"]["aliases"] == ["小镇", "边境小镇"]
    assert by_name["上古神魔陵园"]["aliases"] == ["神魔陵园"]


def test_persist_keeps_concurrently_added_scene_and_bumps_version() -> None:
    conn = _conn(_bible_dict(_scene("上古神魔陵园", ["神魔陵园"])), version=4)
    fallback = Bible(characters=[], world=World(visual_style_canonical="国漫电影风"))

    _persist_planned_scenes(conn, "proj_x", [_scene("白日神魔陵园")], fallback=fallback)

    row = conn.execute("SELECT * FROM projects WHERE id='proj_x'").fetchone()
    names = [s["name"] for s in json.loads(row["bible_json"])["scenes"]]
    assert names == ["白日神魔陵园", "上古神魔陵园"]
    assert row["bible_version"] == 5
    assert row["scene_refs_status"] == "idle"
    assert row["scene_refs_error"] is None


def test_persist_fails_closed_when_version_keeps_moving() -> None:
    """每次写入前版本都被别人推进：不盲写、不覆盖，重试耗尽后抛错。"""
    real = _conn(_bible_dict(), version=1)

    class _RacingConn:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner
            self.update_attempts = 0

        def execute(self, sql: str, params=()):
            if sql.lstrip().upper().startswith("UPDATE"):
                self.update_attempts += 1
                self._inner.execute(
                    "UPDATE projects SET bible_version=bible_version+1 WHERE id='proj_x'"
                )
            return self._inner.execute(sql, params)

        def commit(self) -> None:
            self._inner.commit()

    racing = _RacingConn(real)
    fallback = Bible(characters=[], world=World(visual_style_canonical="国漫电影风"))
    with pytest.raises(ValueError, match="并发修改"):
        _persist_planned_scenes(racing, "proj_x", [_scene("白日神魔陵园")], fallback=fallback)
    assert racing.update_attempts == _PLANNED_SCENES_CAS_ATTEMPTS
    assert json.loads(
        real.execute("SELECT bible_json FROM projects WHERE id='proj_x'").fetchone()["bible_json"]
    )["scenes"] == []
