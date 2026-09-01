"""回归测试：后台补图触发器不得只挂在 prep_pack 成功路径上。

ERR-20260831-63a9d2：生产库只读实测 EP1 的 prep_pack discovery 阶段已经建出
3 张角色卡（``character_portraits`` 却是 0 行，``portraits_status`` 停在
idle），因为 ``start_background_portraits`` 原来只在
``run_episode_prep_pack`` 成功返回后才调用——闸门缺图报错、并发围栏冲突等
任何内部失败都会让触发器永远不跑，图也就永远补不上，下次重跑仍然抛同一个
异常，陷入死循环。

修复：``app/domain/screenplay_ops/task_body.py::_screenplay_task`` 把触发器
挪到 ``finally``，成功/失败/真实用户取消都触发；只排除进程热更/停机
（``asyncio.CancelledError`` + ``task_registry.shutdown_in_progress()``）
那一支——那不是用户取消，服务马上重启，新 worker 续跑时会有自己的成功/
失败路径去触发，这里触发只会重复。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db, task_registry
from app.domain.screenplay_ops import background_portraits, task_body


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-refs-trigger.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_p", "触发器回归", "created", db.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, created_at, screenplay_status) "
        "VALUES(?,?,?,?,?,?)",
        ("ep_p", "proj_p", 1, "第一集", db.now(), "pending"),
    )
    conn.commit()
    yield


def _patch_prep_pack(monkeypatch, prep_pack):
    monkeypatch.setattr(task_body, "_assert_screenplay_run_owner", lambda *a, **k: None)
    import app.production.prep_pack as prep_pack_mod

    monkeypatch.setattr(prep_pack_mod, "run_episode_prep_pack", prep_pack)
    import app.model_registry as model_registry_mod

    monkeypatch.setattr(
        model_registry_mod, "resolve_stage_text_provider", lambda *_a, **_k: "provider-x",
    )


@pytest.mark.asyncio
async def test_background_refs_trigger_fires_when_prep_pack_raises(monkeypatch):
    """卡片已经在 discovery 阶段建好，随后闸门因缺图抛异常——触发器仍要跑。"""
    calls: list[str] = []
    monkeypatch.setattr(
        task_body, "start_background_portraits", lambda project_id: calls.append(project_id),
    )

    async def boom(**_kwargs):
        raise RuntimeError("闸门因缺图报错，卡片已经建好")

    _patch_prep_pack(monkeypatch, boom)

    result = await task_body._screenplay_task("ep_p")

    assert result is None
    assert calls == ["proj_p"], calls


@pytest.mark.asyncio
async def test_background_refs_trigger_fires_on_success(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        task_body, "start_background_portraits", lambda project_id: calls.append(project_id),
    )

    async def ok(**_kwargs):
        return {"characters": [], "scenes": []}

    _patch_prep_pack(monkeypatch, ok)

    result = await task_body._screenplay_task("ep_p")

    assert result == {"characters": [], "scenes": []}
    assert calls == ["proj_p"], calls


@pytest.mark.asyncio
async def test_background_refs_trigger_skipped_on_shutdown_cancel(monkeypatch):
    """进程热更/停机不是用户取消：新 worker 会自己续跑并触发，这里不该重复。"""
    calls: list[str] = []
    monkeypatch.setattr(
        task_body, "start_background_portraits", lambda project_id: calls.append(project_id),
    )
    monkeypatch.setattr(task_registry, "shutdown_in_progress", lambda: True)

    async def cancelled(**_kwargs):
        raise asyncio.CancelledError()

    _patch_prep_pack(monkeypatch, cancelled)

    with pytest.raises(asyncio.CancelledError):
        await task_body._screenplay_task("ep_p")

    assert calls == [], calls


@pytest.mark.asyncio
async def test_background_refs_trigger_fires_on_real_user_cancel(monkeypatch):
    """真实用户取消：卡片可能已经建好，仍要触发后台补图。"""
    calls: list[str] = []
    monkeypatch.setattr(
        task_body, "start_background_portraits", lambda project_id: calls.append(project_id),
    )
    monkeypatch.setattr(task_registry, "shutdown_in_progress", lambda: False)

    async def cancelled(**_kwargs):
        raise asyncio.CancelledError()

    _patch_prep_pack(monkeypatch, cancelled)

    with pytest.raises(asyncio.CancelledError):
        await task_body._screenplay_task("ep_p")

    assert calls == ["proj_p"], calls


def _seed_bible_project(project_id: str, *, characters: list, scenes: list) -> None:
    """种一个带指定角色/场景集合的项目，供下面「空集合不触发后台补图」用例复用。"""
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json) VALUES(?,?,?,?,?)",
        (
            project_id,
            "空世界书回归",
            "created",
            db.now(),
            json.dumps({
                "characters": characters,
                "scenes": scenes,
                "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
            }, ensure_ascii=False),
        ),
    )
    conn.commit()


def test_background_portraits_skips_scene_refs_when_bible_has_no_scenes(monkeypatch):
    """ERR-20260831-a144a0 回归：世界书有角色但 0 场景时，
    start_background_portraits 不得触发 _start_scene_refs_generation——那会让
    后台任务 _scene_refs_task 跑进 generate_scene_refs 内部
    `if not bible.scenes: raise ValueError("还没有场景圣经，请先生成场景清单")`，
    异常发生在后台任务自己的调用栈上，不在本函数 try/except 覆盖范围内，最终
    把 scene_refs_status 写成 failed 并弹一条「请把错误码反馈给技术人员」的
    假报警。有角色的一侧不受影响，仍要正常触发。"""
    _seed_bible_project("proj_scene_empty", characters=[{"name": "甲"}], scenes=[])

    scene_calls: list[str] = []
    char_calls: list[str] = []
    import app.domain.bible_ops.refs_generation as refs_generation_mod
    import app.domain.bible_ops.scene_bible_prep as scene_bible_prep_mod

    monkeypatch.setattr(
        scene_bible_prep_mod, "_start_scene_refs_generation",
        lambda project_id, *_a, **_k: scene_calls.append(project_id),
    )
    monkeypatch.setattr(
        refs_generation_mod, "_start_refs_generation",
        lambda project_id, *_a, **_k: char_calls.append(project_id),
    )

    background_portraits.start_background_portraits("proj_scene_empty")

    assert scene_calls == [], "0 场景时不得触发场景后台补图"
    assert char_calls == ["proj_scene_empty"], "有角色时仍要正常触发角色后台补图"


def test_background_portraits_skips_refs_when_bible_has_no_characters(monkeypatch):
    """对称场景：0 角色但有场景时，只该跳过角色那条，场景那条正常触发。"""
    _seed_bible_project("proj_char_empty", characters=[], scenes=[{"name": "乙地"}])

    scene_calls: list[str] = []
    char_calls: list[str] = []
    import app.domain.bible_ops.refs_generation as refs_generation_mod
    import app.domain.bible_ops.scene_bible_prep as scene_bible_prep_mod

    monkeypatch.setattr(
        scene_bible_prep_mod, "_start_scene_refs_generation",
        lambda project_id, *_a, **_k: scene_calls.append(project_id),
    )
    monkeypatch.setattr(
        refs_generation_mod, "_start_refs_generation",
        lambda project_id, *_a, **_k: char_calls.append(project_id),
    )

    background_portraits.start_background_portraits("proj_char_empty")

    assert char_calls == [], "0 角色时不得触发角色后台补图"
    assert scene_calls == ["proj_char_empty"], "有场景时仍要正常触发场景后台补图"


def test_background_portraits_skips_both_when_bible_is_fully_empty(monkeypatch):
    """EP1 真实故障形状：映射在建卡之前就失败，世界书 0 角色 0 场景。两条
    后台任务都不该触发。"""
    _seed_bible_project("proj_fully_empty", characters=[], scenes=[])

    scene_calls: list[str] = []
    char_calls: list[str] = []
    import app.domain.bible_ops.refs_generation as refs_generation_mod
    import app.domain.bible_ops.scene_bible_prep as scene_bible_prep_mod

    monkeypatch.setattr(
        scene_bible_prep_mod, "_start_scene_refs_generation",
        lambda project_id, *_a, **_k: scene_calls.append(project_id),
    )
    monkeypatch.setattr(
        refs_generation_mod, "_start_refs_generation",
        lambda project_id, *_a, **_k: char_calls.append(project_id),
    )

    background_portraits.start_background_portraits("proj_fully_empty")

    assert scene_calls == []
    assert char_calls == []


@pytest.mark.asyncio
async def test_background_portraits_does_not_fail_or_log_error_when_bible_is_empty():
    """端到端复现 ERR-20260831-a144a0，不 mock 任何深层函数：只要最终产物状态
    对了，这个测试就该一直绿——哪怕将来守卫换一种写法实现。

    验证过这条测试本身有效：临时把 `_bible_collection` monkeypatch 成永远
    判定"有东西可补"（模拟守卫被删掉/失效），再跑同样的流程并等真正的后台
    任务跑完，`scene_refs_status` 确实变成了 `failed`、`scene_refs_error`
    确实是同一句「请把错误码反馈给技术人员」——说明下面这个等待方式真的会
    在守卫失效时抓到回归，不是等不到任务完成就侥幸通过的假绿。

    必须用 `task_registry.get()` 拿到真实的 `asyncio.Task` 显式 await，不能
    只 `asyncio.sleep(0)` 几次凑数——`_scene_refs_task`/`_refs_task` 内部有
    多个 await 点（recorder 事务、generate_scene_refs 的 model_validate 前
    还有一次 DB round-trip），固定次数的 sleep(0) 不保证任务已经跑到会更新
    DB 的那一步，会把"任务根本没来得及失败"误判成"任务失败不了"。"""
    _seed_bible_project("proj_e2e_empty", characters=[], scenes=[])
    conn = db.get_conn()
    error_count_before = conn.execute(
        "SELECT COUNT(*) AS n FROM error_logs"
    ).fetchone()["n"]

    background_portraits.start_background_portraits("proj_e2e_empty")

    # 若守卫失效，这里会真的调用 _start_scene_refs_generation/_start_refs_
    # generation，两者各自把任务登记进 task_registry；守卫生效时二者都不会
    # 被调用，两个 get() 都返回 None，直接跳过等待。
    for kind in ("scene_refs", "refs"):
        task = task_registry.get(kind, "proj_e2e_empty")
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)

    row = conn.execute(
        "SELECT refs_status, scene_refs_status FROM projects WHERE id='proj_e2e_empty'"
    ).fetchone()
    error_count_after = conn.execute(
        "SELECT COUNT(*) AS n FROM error_logs"
    ).fetchone()["n"]

    assert row["scene_refs_status"] != "failed", row["scene_refs_status"]
    assert row["refs_status"] != "failed", row["refs_status"]
    assert error_count_after == error_count_before
