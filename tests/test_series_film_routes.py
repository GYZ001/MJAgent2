"""连播台四条 REST 路由的契约回归：启动/查询/暂停/继续。

用最小化的独立 FastAPI test app（只挂 ``api.router``，不接项目所有权依赖，
照 tests/test_screenplay_draft_delete_isolation.py 的写法）——本文件只验证
路由自身的校验/响应契约，项目所有权鉴权由 test_rbac_project_isolation.py
之类的专门测试覆盖。

后台编排协程本身（真实调五个工作台）不在这里跑：按场景 monkeypatch
``stages.stage_is_complete``/``stages.run_stage``/``merge.merge_is_current``，
只验证路由层的输入校验、409/422 语义与状态投影，不依赖真实剧本/分镜/视频
生成。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import api, db
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests, set_request_approval_token
from app.capabilities.policy import reset_approvals_for_tests
from app.domain.series_ops import merge, state
from app.domain.series_ops import stages as series_stages
from app.local_session import APPROVAL_HEADER, ensure_session_secret, set_request_session_id
from tests.conftest import SessionTestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "series-film-routes.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()

    test_app = FastAPI()

    @test_app.middleware("http")
    async def inject_approval_token(request: Request, call_next):
        set_request_approval_token(request.headers.get(APPROVAL_HEADER))
        set_request_session_id(ensure_session_secret())
        try:
            return await call_next(request)
        finally:
            set_request_approval_token(None)
            set_request_session_id(None)

    test_app.include_router(api.router)
    with TestClient(test_app) as test_client:
        yield SessionTestClient(test_client)


def _seed_episodes(episode_nos: list[int]) -> None:
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','演示项目',0)")
    for no in episode_nos:
        conn.execute(
            """INSERT INTO episodes(id,project_id,episode_no,title,status,created_at)
               VALUES(?,?,?,?, 'planned', 0)""",
            (f"ep{no}", "p1", no, f"第{no}集"),
        )
    conn.commit()


def _stub_all_stages_instantly_complete(monkeypatch) -> None:
    """五步与合并都判定为已满足——后台协程几乎立刻跑到 succeeded 终态。"""
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: True)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: True)


def _stub_stages_block_forever(monkeypatch) -> None:
    """第一步判定未完成、启动后永久挂起——用来测试暂停会真的打断在跑的任务。"""
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)

    async def _blocked(_stage, _episode_id, _run_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(series_stages, "run_stage", _blocked)


def test_start_accepts_single_episode_span(client, monkeypatch) -> None:
    """用户明确单集也合法：episode_from == episode_to。"""
    _seed_episodes([1, 2, 3])
    _stub_all_stages_instantly_complete(monkeypatch)

    resp = client.post(
        "/api/projects/p1/series-film",
        json={"episode_from": 2, "episode_to": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["episode_from"] == 2 and body["episode_to"] == 2
    assert [e["episode_no"] for e in body["episodes"]] == [2]
    assert body["episodes"][0]["stages"] == {
        "screenplay": "pending", "storyboard": "pending",
        "confirm": "pending", "video": "pending", "final": "pending",
    }


def test_start_rejects_missing_episode(client, monkeypatch) -> None:
    _seed_episodes([1, 2])
    _stub_all_stages_instantly_complete(monkeypatch)

    resp = client.post(
        "/api/projects/p1/series-film",
        json={"episode_from": 1, "episode_to": 3},
    )
    assert resp.status_code == 422, resp.text
    assert "3" in resp.text


def test_start_rejects_span_over_ten(client, monkeypatch) -> None:
    _seed_episodes(list(range(1, 12)))
    _stub_all_stages_instantly_complete(monkeypatch)

    resp = client.post(
        "/api/projects/p1/series-film",
        json={"episode_from": 1, "episode_to": 11},
    )
    assert resp.status_code == 422, resp.text


def test_start_conflicts_when_already_active(client, monkeypatch) -> None:
    _seed_episodes([1, 2])
    _stub_stages_block_forever(monkeypatch)

    first = client.post(
        "/api/projects/p1/series-film",
        json={"episode_from": 1, "episode_to": 2},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/projects/p1/series-film",
        json={"episode_from": 1, "episode_to": 2},
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "SERIES_FILM_ALREADY_ACTIVE"

    _cancel_series_task("p1")


def test_get_reflects_no_run_then_running(client, monkeypatch) -> None:
    _seed_episodes([1])
    resp = client.get("/api/projects/p1/series-film")
    assert resp.status_code == 200, resp.text
    empty = resp.json()
    assert empty["run"] is None
    assert empty["film"] is None
    assert [e["episode_no"] for e in empty["episodes_available"]] == [1]

    _stub_stages_block_forever(monkeypatch)
    started = client.post(
        "/api/projects/p1/series-film",
        json={"episode_from": 1, "episode_to": 1},
    )
    assert started.status_code == 200, started.text

    after = client.get("/api/projects/p1/series-film").json()
    assert after["run"]["status"] == "running"
    assert after["run"]["episode_from"] == 1
    assert after["run"]["run_id"] == started.json()["run_id"]

    _cancel_series_task("p1")


def test_pause_then_resume_flow(client, monkeypatch) -> None:
    _seed_episodes([1])
    _stub_stages_block_forever(monkeypatch)

    started = client.post(
        "/api/projects/p1/series-film",
        json={"episode_from": 1, "episode_to": 1},
    )
    assert started.status_code == 200, started.text
    first_run_id = started.json()["run_id"]

    paused = client.post("/api/projects/p1/series-film/pause")
    assert paused.status_code == 200, paused.text
    # 走能力总线：响应在契约字段之外还会带 command_id/summary，只锁契约字段本身。
    paused_body = paused.json()
    assert paused_body["ok"] is True
    assert paused_body["status"] == "paused"

    snapshot = client.get("/api/projects/p1/series-film").json()
    assert snapshot["run"]["status"] == "paused"
    assert snapshot["run"]["run_id"] == first_run_id

    resumed = client.post("/api/projects/p1/series-film/resume")
    assert resumed.status_code == 200, resumed.text
    resumed_body = resumed.json()
    assert resumed_body["ok"] is True
    assert resumed_body["status"] == "running"
    assert resumed_body["run_id"] != first_run_id

    _cancel_series_task("p1")


def test_resume_without_any_run_conflicts(client) -> None:
    _seed_episodes([1])
    resp = client.post("/api/projects/p1/series-film/resume")
    assert resp.status_code == 409, resp.text


def test_pause_without_active_run_conflicts(client) -> None:
    _seed_episodes([1])
    resp = client.post("/api/projects/p1/series-film/pause")
    assert resp.status_code == 409, resp.text


def _cancel_series_task(project_id: str) -> None:
    """测试收尾：把仍在阻塞等待的连播台后台任务收掉，避免悬挂到进程退出。

    用同步 ``cancel()``（而不是 ``cancel_and_wait``）：任务跑在
    ``TestClient`` 自己内部的事件循环/portal 线程上，测试主线程这里没有那个
    循环，``await``/``asyncio.run`` 会撞上「Future attached to a different
    loop」；``cancel()`` 内部用 ``call_soon_threadsafe`` 调度取消，天然跨线程
    安全，不需要在这里等它跑完。
    """
    from app import task_registry

    task_registry.cancel(state.TASK_KIND, project_id)
