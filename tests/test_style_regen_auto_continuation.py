"""风格确认后「人物 + 场景」两条腿的后端编排。

覆盖点（coordinator 反馈的核心判据）：确认一次之后，两条生成线必须真正被
发起，不能依赖用户之后停留在哪个页面、是否访问场景库页面、或浏览器是否
还开着。项目初始（还没有场景清单）场景下，这条链路是：
    _bible_task 谱写成功
        → 写下 pending_scene_regen 票据（同一次成功写入内完成）
        → 既有级联自动启动 _start_scene_bible_preparation（免费清单，不变）
        → 场景清单真正落盘后，_scene_bible_task 消费这张票据
        → 自动发起 _start_scene_refs_generation（场景图，无需用户再点一次）

这条链路完全在后端函数调用内完成；测试里除了显式打桩的外部 I/O（LLM 调用、
真正的 asyncio 任务调度）之外，其余环节全部按生产代码路径真实执行，用来
证明「一次确认后两边都被发起」不是靠某个前端 effect 撞对用户访问时机凑出
来的。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app import api, task_registry
from app.schemas import Bible, Character, Scene, World


def _projects_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT, "
        "scene_refs_status TEXT DEFAULT 'idle', scene_refs_error TEXT, "
        "pending_scene_regen INTEGER NOT NULL DEFAULT 0)"
    )


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


def test_bible_task_marks_pending_scene_regen_on_success(monkeypatch) -> None:
    """_bible_task 谱写成功后必须写下待续跑票据——这是场景腿能在无人盯着页面
    的情况下自动继续的前提。单独验证这一步，不牵扯 _scene_bible_task。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    _projects_schema(conn)
    conn.execute(
        "INSERT INTO projects(id, bible_status, status, scene_refs_status) "
        "VALUES('proj_test', 'running', 'ingested', 'idle')"
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="国风水墨"),
            characters=[Character(
                name="甲一", role="主角",
                appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩",
            )],
        )

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(api, "_start_scene_bible_preparation", lambda *_args, **_kwargs: True)

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute("SELECT pending_scene_regen FROM projects WHERE id='proj_test'").fetchone()
    assert row["pending_scene_regen"] == 1


def test_scene_bible_task_without_pending_flag_does_not_start_images(monkeypatch) -> None:
    """票据不存在（flag=0，日常场景清单刷新）时行为不变：只准备免费清单，
    绝不静默启动付费图片任务。这是对既有回归锁的补充覆盖——既有测试只覆盖了
    「列缺失」，这里覆盖「列存在但为 0」，是往后更常见的实际状态。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, "
        "scene_refs_status TEXT, scene_refs_error TEXT, pending_scene_regen INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    bible_json = json.dumps({
        "characters": [], "world": {"visual_style_canonical": "国漫风格", "era": "", "genre": "仙侠"},
    }, ensure_ascii=False)
    conn.execute(
        "INSERT INTO projects(id, bible_json, scene_refs_status, pending_scene_regen) "
        "VALUES('p', ?, 'running', 0)",
        (bible_json,),
    )
    conn.execute("INSERT INTO chapters VALUES('p', 1, '青山脚下')")
    conn.commit()

    scenes = [Scene(name="青山脚下", scene_canonical="青山脚下的山路，晨雾弥漫，古树与石阶环绕", location_kind="室外")]
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api.WorkflowRecorder, "create", lambda **_kwargs: _StubRecorder(scenes))
    monkeypatch.setattr(
        api, "_start_scene_refs_generation",
        lambda *_args, **_kwargs: pytest.fail("没有待续跑票据时不该启动场景图生成"),
    )

    asyncio.run(api._scene_bible_task("p"))

    row = conn.execute("SELECT pending_scene_regen FROM projects WHERE id='p'").fetchone()
    assert row["pending_scene_regen"] == 0


def test_scene_bible_task_consumes_flag_and_starts_scene_refs_exactly_once(monkeypatch) -> None:
    """票据存在时：场景清单落盘后自动发起场景图生成；同一张票据不会因为
    _scene_bible_task 被重复调用（比如恢复重启）而触发两次。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, "
        "scene_refs_status TEXT, scene_refs_error TEXT, pending_scene_regen INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    bible_json = json.dumps({
        "characters": [], "world": {"visual_style_canonical": "国漫风格", "era": "", "genre": "仙侠"},
    }, ensure_ascii=False)
    conn.execute(
        "INSERT INTO projects(id, bible_json, scene_refs_status, pending_scene_regen) "
        "VALUES('p', ?, 'running', 1)",
        (bible_json,),
    )
    conn.execute("INSERT INTO chapters VALUES('p', 1, '青山脚下')")
    conn.commit()

    scenes = [Scene(name="青山脚下", scene_canonical="青山脚下的山路，晨雾弥漫，古树与石阶环绕", location_kind="室外")]
    started: list[tuple] = []
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api.WorkflowRecorder, "create", lambda **_kwargs: _StubRecorder(scenes))
    monkeypatch.setattr(
        api, "_start_scene_refs_generation",
        lambda project_id, only_scene, **kwargs: started.append((project_id, only_scene, kwargs)) or True,
    )

    asyncio.run(api._scene_bible_task("p"))

    assert started == [("p", None, {"resume": False})]
    row = conn.execute("SELECT pending_scene_regen FROM projects WHERE id='p'").fetchone()
    assert row["pending_scene_regen"] == 0, "票据必须在消费后立即清零"

    # 模拟恢复重启等原因让 _scene_bible_task 再跑一次：票据已经是 0，不该再触发。
    asyncio.run(api._scene_bible_task("p"))
    assert started == [("p", None, {"resume": False})], "同一张票据不能被消费两次"


def test_confirming_style_once_drives_both_legs_without_any_page_visit(monkeypatch) -> None:
    """端到端证据：从「用户确认一次人物谱谱写」这一个动作出发，完整走
    _bible_task → 既有级联 → _scene_bible_task → 消费票据 → 场景图生成，
    全程只 mock 真正的外部 I/O（LLM 调用、asyncio 任务调度本身），其余环节
    按生产代码路径真实执行。断言的是 _start_scene_refs_generation 最终确实
    被调用——不依赖任何前端页面访问、不依赖浏览器是否还开着。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    _projects_schema(conn)
    conn.execute(
        "INSERT INTO projects(id, bible_status, status, scene_refs_status) "
        "VALUES('proj_e2e', 'running', 'ingested', 'idle')"
    )
    conn.execute("INSERT INTO chapters VALUES('proj_e2e', 1, '青山脚下')")
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="国风水墨"),
            characters=[Character(
                name="甲一", role="主角",
                appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩",
            )],
        )

    scenes = [Scene(name="青山脚下", scene_canonical="青山脚下的山路，晨雾弥漫，古树与石阶环绕", location_kind="室外")]

    scene_refs_started: list[tuple] = []
    captured_scene_bible_coro = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        if kind == "scene_bible":
            captured_scene_bible_coro["coro"] = coro
        else:
            coro.close()
        return None

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(api.WorkflowRecorder, "create", lambda **_kwargs: _StubRecorder(scenes))
    monkeypatch.setattr(
        api, "_start_scene_refs_generation",
        lambda project_id, only_scene, **kwargs: scene_refs_started.append(
            (project_id, only_scene, kwargs)
        ) or True,
    )
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    # 步骤一：用户在人物谱页确认一次风格 + 谱写。这一步之后测试不再模拟任何
    # 用户交互——不打开场景库页面，不触发任何前端 effect。
    asyncio.run(api._bible_task("proj_e2e", trigger_full_refs=True))

    assert "coro" in captured_scene_bible_coro, "谱写成功必须自动 spawn 场景清单准备任务（既有级联，未被破坏）"
    row = conn.execute("SELECT pending_scene_regen FROM projects WHERE id='proj_e2e'").fetchone()
    assert row["pending_scene_regen"] == 1

    # 步骤二：生产环境里这个协程由事件循环真正驱动执行；测试里手动驱动一次，
    # 代表「场景清单准备任务真正跑完」这个后端事件，与前端是否在场无关。
    asyncio.run(captured_scene_bible_coro["coro"])

    assert scene_refs_started == [("proj_e2e", None, {"resume": False})], (
        "场景清单就绪后必须自动发起场景图生成，全过程不依赖任何页面访问"
    )
    final_row = conn.execute(
        "SELECT pending_scene_regen FROM projects WHERE id='proj_e2e'"
    ).fetchone()
    assert final_row["pending_scene_regen"] == 0
