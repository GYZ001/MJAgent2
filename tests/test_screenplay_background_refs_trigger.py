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

import pytest

from app import db, task_registry
from app.domain.screenplay_ops import task_body


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
