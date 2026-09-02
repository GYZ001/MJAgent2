"""退场回归锁：人物谱谱写成功后不再自动级联场景清单/场景图（2026-08-31）。

架构转向前，这个文件验的是「_bible_task 谱写成功 → 写 pending_scene_regen
票据 → 自动启动 _start_scene_bible_preparation → 场景清单落盘后
_scene_bible_task 消费票据 → 自动发起 _start_scene_refs_generation」这整条
「一次确认驱动人物+场景两条腿」的级联。

首版人物谱改为只判定世界观（app.stages.generate_bible，characters 恒为
[]），generate_scene_bible 批量场景清单生成同一批退出首版流程（场景改为
app.scenes.assess_new_scene 反应式发现，见该函数 docstring）：`_bible_task`
不再写 pending_scene_regen 票据，`_scene_bible_task` 也不再消费它去自动续跑
场景图（消费函数 `_consume_pending_scene_regen_if_ready` 已随之删除，见
app/domain/bible_ops/scene_bible_prep.py 顶部注释）。场景图现在只有两个显式
入口：场景库页手动确认（POST /projects/{id}/scene-bible）与画风切换
（POST /projects/{id}/bible/style），两者都直接调用
_start_scene_refs_generation，不经过任何票据——这两条已由
tests/test_scene_routes.py 与 tests/test_bible_style_endpoint.py 覆盖，本文件
只钉住「退场后不再自动级联」这一条新契约。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app import api, task_registry
from app.schemas import Bible, Scene, World
from tests.conftest import patch_api_everywhere


class _StubRecorder:
    """_scene_bible_task 用的最小 WorkflowRecorder 替身：跳过完整 workflow_runs schema。"""

    run_id = "run-scene-bible"

    def start(self) -> None:
        pass

    async def step(self, *_args, **_kwargs):
        return "step-scene-bible", self._scenes

    def succeed(self, _message: str, conn=None) -> None:
        pass

    def cancel(self, conn=None) -> None:
        pass

    def fail(self, _exc: Exception, conn=None) -> None:  # pragma: no cover - not expected in these tests
        raise AssertionError("scene bible task should not fail in this test")

    def __init__(self, scenes: list[Scene]) -> None:
        self._scenes = scenes


def test_bible_task_success_does_not_write_pending_scene_regen(monkeypatch) -> None:
    """_bible_task 谱写成功后不再写 pending_scene_regen=1——写票据的那段级联
    （连同场景清单自动准备）已随首版人物谱只产出 world 一起删除。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT, "
        "scene_refs_status TEXT DEFAULT 'idle', scene_refs_error TEXT, "
        "pending_scene_regen INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_status, status, scene_refs_status) "
        "VALUES('proj_test', 'running', 'ingested', 'idle')"
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(world=World(visual_style_canonical="国风水墨"), characters=[])

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "generate_bible", fake_generate_bible)
    patch_api_everywhere(monkeypatch, "_start_refs_generation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        task_registry, "spawn",
        lambda *_args, **_kwargs: pytest.fail("人物谱谱写成功不应再自动 spawn 任何场景任务"),
    )

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute("SELECT pending_scene_regen FROM projects WHERE id='proj_test'").fetchone()
    assert row["pending_scene_regen"] == 0


def test_scene_bible_task_no_longer_auto_starts_scene_refs(monkeypatch) -> None:
    """`_scene_bible_task` 落盘场景清单后不再自动发起场景图生成，即便数据库里
    残留旧版本写入的 pending_scene_regen=1（消费函数已删除，这一列不再被
    _scene_bible_task 读取）。场景图现在只能由场景库页手动确认或画风切换
    触发（各自的 _start_scene_refs_generation 调用点，不在本文件覆盖范围）。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "scene_refs_status TEXT, scene_refs_error TEXT, pending_scene_regen INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    bible_json = json.dumps({
        "characters": [], "world": {"visual_style_canonical": "国漫风格", "era": "", "genre": "仙侠"},
    }, ensure_ascii=False)
    # 残留的 pending_scene_regen=1（例如升级前留下的历史数据）不应再被消费。
    conn.execute(
        "INSERT INTO projects(id, bible_json, scene_refs_status, pending_scene_regen) "
        "VALUES('p', ?, 'running', 1)",
        (bible_json,),
    )
    conn.execute("INSERT INTO chapters VALUES('p', 1, '青山脚下')")
    conn.commit()

    scenes = [Scene(name="青山脚下", scene_canonical="青山脚下的山路，晨雾弥漫，古树与石阶环绕", location_kind="室外")]
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(api.WorkflowRecorder, "create", lambda **_kwargs: _StubRecorder(scenes))
    patch_api_everywhere(monkeypatch, "_start_scene_refs_generation",
        lambda *_args, **_kwargs: pytest.fail("场景清单准备完成不应再自动触发付费场景图生成"),
    )

    asyncio.run(api._scene_bible_task("p"))

    # 残留票据原样保留（不再被这条路径读写），场景状态回到 idle 等待用户手动确认。
    row = conn.execute(
        "SELECT scene_refs_status, pending_scene_regen FROM projects WHERE id='p'"
    ).fetchone()
    assert row["scene_refs_status"] == "idle"
    assert row["pending_scene_regen"] == 1
