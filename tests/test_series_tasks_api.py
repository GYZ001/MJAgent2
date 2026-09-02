"""连播任务台 REST 契约回归：列表/切分预览/生成/删除/详情 + 入队/取消/暂停/继续接线。

用最小化的独立 FastAPI test app（只挂 ``api.router``，不接项目所有权依赖，
照 tests/test_screenplay_draft_delete_isolation.py 的写法）——本文件只验证
路由自身的校验/响应契约，项目所有权鉴权由 test_rbac_project_isolation.py
之类的专门测试覆盖。

队列串行 runner 的深层行为（严格串行、失败继续、连续 3 次自动停队、暂停/
取消的进度保留）在 tests/test_series_queue.py 直接测 queue.py，不在这里重复；
本文件对 enqueue/cancel/queue 的覆盖只到「HTTP 契约接线对不对」为止。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import api, config, db
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests, set_request_approval_token
from app.capabilities.policy import reset_approvals_for_tests
from app.domain.series_ops import merge, queue
from app.domain.series_ops import stages as series_stages
from app.local_session import APPROVAL_HEADER, ensure_session_secret, set_request_session_id
from tests.conftest import SessionTestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "series-tasks.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
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


def _seed_episodes(project_id: str, episode_nos: list[int]) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO projects(id,name,created_at) VALUES(?,?,0)", (project_id, "演示项目"),
    )
    conn.executemany(
        """INSERT INTO episodes(id,project_id,episode_no,title,status,created_at)
           VALUES(?,?,?,?, 'planned', 0)""",
        [(f"{project_id}-ep{no}", project_id, no, f"第{no}集") for no in episode_nos],
    )
    conn.commit()


def _stub_all_stages_instantly_complete(monkeypatch) -> None:
    """五步全部判定已满足；merge_is_current 故意留 False——True 会让 enqueue
    的「已完成默认跳过」判据直接把任务打进 skipped，永远轮不到 runner 跑。
    build_series_film 桩成空操作，orchestrator 走到 merge 步骤时不会真跑 ffmpeg。
    """
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: True)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})


def _stub_stages_block_forever(monkeypatch) -> None:
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)

    async def _blocked(_stage, _episode_id, _run_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(series_stages, "run_stage", _blocked)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)


def _cancel_series_queue(project_id: str) -> None:
    """测试收尾：把仍在阻塞等待的连播队列后台任务收掉，避免悬挂到进程退出。

    同步 ``cancel()``（不是 ``cancel_and_wait``）：runner 跑在 TestClient 自己的
    事件循环/portal 线程上，测试主线程这里没有那个循环。
    """
    from app import task_registry

    task_registry.cancel(queue.TASK_KIND, project_id)


# --------------------------------------------------------------------- plan

def test_plan_groups_splits_project_episodes_exactly(client) -> None:
    _seed_episodes("p1", list(range(1, 1601)))
    resp = client.get("/api/projects/p1/series-tasks/plan", params={"group_size": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_groups"] == 160
    assert body["new_groups"] == 160
    assert body["existing_groups"] == 0
    assert body["episodes"] == {"total": 1600, "min_no": 1, "max_no": 1600}
    assert body["truncated"] is False  # 160 组 < 200 条上限，不截断
    assert len(body["groups"]) == 160
    assert body["groups"][0] == {
        "episode_from": 1, "episode_to": 10, "exists": False, "missing_episode_nos": [],
    }


def test_plan_groups_reports_missing_episode_numbers_in_gaps(client) -> None:
    _seed_episodes("p1", [1, 2, 3, 4, 5, 6, 7, 8, 10])  # 缺第 9 集
    resp = client.get("/api/projects/p1/series-tasks/plan", params={"group_size": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_groups"] == 1
    assert body["groups"][0]["episode_from"] == 1
    assert body["groups"][0]["episode_to"] == 10
    assert body["groups"][0]["missing_episode_nos"] == [9]


def test_plan_groups_rejects_group_size_out_of_range(client) -> None:
    _seed_episodes("p1", [1, 2])
    resp = client.get("/api/projects/p1/series-tasks/plan", params={"group_size": 11})
    assert resp.status_code == 422, resp.text
    resp = client.get("/api/projects/p1/series-tasks/plan", params={"group_size": 0})
    assert resp.status_code == 422, resp.text


# ----------------------------------------------------------------- generate

def test_generate_tasks_is_idempotent_across_repeated_calls(client) -> None:
    _seed_episodes("p1", list(range(1, 26)))  # 25 集 -> group_size=10 时 3 组

    # 走能力总线：响应在契约字段之外还会带 command_id/summary，只锁契约字段本身。
    first = client.post("/api/projects/p1/series-tasks", json={"group_size": 10})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert (first_body["created"], first_body["existing"], first_body["tasks_total"]) == (3, 0, 3)

    second = client.post("/api/projects/p1/series-tasks", json={"group_size": 10})
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert (second_body["created"], second_body["existing"], second_body["tasks_total"]) == (0, 3, 3)

    listed = client.get("/api/projects/p1/series-tasks").json()
    assert listed["totals"]["all"] == 3


def test_generate_tasks_requires_exactly_one_of_group_size_or_ranges(client) -> None:
    _seed_episodes("p1", [1, 2])
    resp = client.post("/api/projects/p1/series-tasks", json={})
    assert resp.status_code == 422, resp.text

    resp = client.post(
        "/api/projects/p1/series-tasks",
        json={"group_size": 10, "ranges": [{"episode_from": 1, "episode_to": 2}]},
    )
    assert resp.status_code == 422, resp.text


def test_generate_tasks_ranges_rejects_span_over_ten_and_inversion(client) -> None:
    _seed_episodes("p1", list(range(1, 20)))
    resp = client.post(
        "/api/projects/p1/series-tasks",
        json={"ranges": [{"episode_from": 1, "episode_to": 11}]},
    )
    assert resp.status_code == 422, resp.text

    resp = client.post(
        "/api/projects/p1/series-tasks",
        json={"ranges": [{"episode_from": 5, "episode_to": 1}]},
    )
    assert resp.status_code == 422, resp.text


def test_generate_tasks_accepts_explicit_ranges(client) -> None:
    _seed_episodes("p1", list(range(1, 20)))
    resp = client.post(
        "/api/projects/p1/series-tasks",
        json={"ranges": [{"episode_from": 1, "episode_to": 5}, {"episode_from": 6, "episode_to": 10}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["created"], body["existing"], body["tasks_total"]) == (2, 0, 2)


# --------------------------------------------------------------------- list

def test_list_tasks_pagination_and_totals(client) -> None:
    _seed_episodes("p1", list(range(1, 41)))
    client.post("/api/projects/p1/series-tasks", json={"group_size": 10})

    page1 = client.get("/api/projects/p1/series-tasks", params={"offset": 0, "limit": 2}).json()
    assert page1["offset"] == 0 and page1["limit"] == 2
    assert [t["index"] for t in page1["tasks"]] == [1, 2]
    assert page1["totals"] == {
        "all": 4, "idle": 4, "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
    }
    assert page1["max_span"] == 10 and page1["default_group_size"] == 10

    page2 = client.get("/api/projects/p1/series-tasks", params={"offset": 2, "limit": 2}).json()
    assert [t["index"] for t in page2["tasks"]] == [3, 4]
    assert page2["tasks"][0]["title"] == "第 21-30 集"


def test_list_tasks_limit_clamped_to_200(client) -> None:
    _seed_episodes("p1", [1, 2])
    resp = client.get("/api/projects/p1/series-tasks", params={"limit": 999})
    assert resp.status_code == 200, resp.text
    assert resp.json()["limit"] == 200


# ------------------------------------------------------------------- detail

def test_get_task_detail_includes_episode_entries_and_film_stale(client) -> None:
    _seed_episodes("p1", [1, 2, 3])
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 3}]})
    task_id = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["task_id"]

    resp = client.get(f"/api/projects/p1/series-tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["episode_no"] for e in body["episodes"]] == [1, 2, 3]
    assert body["episodes"][0]["stages"] == {
        "screenplay": "pending", "storyboard": "pending",
        "confirm": "pending", "video": "pending", "final": "pending",
    }
    assert body["film"] is None
    assert body["film_stale"] is False


def test_get_task_detail_missing_task_returns_404(client) -> None:
    _seed_episodes("p1", [1])
    resp = client.get("/api/projects/p1/series-tasks/st_missing")
    assert resp.status_code == 404, resp.text


# ------------------------------------------------------------------- delete

def test_delete_idle_task_succeeds_and_says_film_kept(client) -> None:
    _seed_episodes("p1", [1, 2])
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 2}]})
    task_id = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["task_id"]

    resp = client.delete(f"/api/projects/p1/series-tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    assert "成片保留" in resp.json()["note"]
    assert client.get("/api/projects/p1/series-tasks").json()["totals"]["all"] == 0


def test_delete_running_task_conflicts(client, monkeypatch) -> None:
    _seed_episodes("p1", [1])
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 1}]})
    task_id = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["task_id"]

    _stub_stages_block_forever(monkeypatch)
    enq = client.post("/api/projects/p1/series-tasks/enqueue", json={"task_ids": [task_id]})
    assert enq.status_code == 200, enq.text
    # enqueue 的响应在 runner 后台任务真正被事件循环调度前就已经返回（fire-and-
    # forget spawn，见 queue.py::_ensure_runner），这里再发一次请求把循环pump
    # 一轮，runner 才会真的跑起来卡在 block_forever 桩上。
    snapshot = client.get("/api/projects/p1/series-tasks").json()
    assert snapshot["queue"]["running_task_id"] == task_id

    resp = client.delete(f"/api/projects/p1/series-tasks/{task_id}")
    assert resp.status_code == 409, resp.text

    _cancel_series_queue("p1")


# ------------------------------------------------------------- enqueue 接线

def test_enqueue_runs_task_to_success_and_updates_status(client, monkeypatch) -> None:
    _seed_episodes("p1", [1, 2])
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 2}]})
    task_id = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["task_id"]

    _stub_all_stages_instantly_complete(monkeypatch)
    resp = client.post("/api/projects/p1/series-tasks/enqueue", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text

    detail = client.get(f"/api/projects/p1/series-tasks/{task_id}").json()
    assert detail["status"] == "succeeded"


def test_enqueue_skips_task_with_missing_episodes(client) -> None:
    _seed_episodes("p1", [1, 2, 3])
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 3}]})
    task_id = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["task_id"]

    db.get_conn().execute("DELETE FROM episodes WHERE id=?", ("p1-ep2",))
    db.get_conn().commit()

    resp = client.post("/api/projects/p1/series-tasks/enqueue", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued"] == 0
    assert body["skipped"][0]["task_id"] == task_id
    assert "缺集" in body["skipped"][0]["reason"]


def test_enqueue_pause_then_resume_flow(client, monkeypatch) -> None:
    _seed_episodes("p1", [1])
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 1}]})
    task_id = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["task_id"]

    _stub_stages_block_forever(monkeypatch)
    started = client.post("/api/projects/p1/series-tasks/enqueue", json={"task_ids": [task_id]})
    assert started.status_code == 200, started.text
    snapshot = client.get("/api/projects/p1/series-tasks").json()
    assert snapshot["queue"]["running_task_id"] == task_id

    paused = client.post("/api/projects/p1/series-tasks/queue/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"

    detail = client.get(f"/api/projects/p1/series-tasks/{task_id}").json()
    assert detail["status"] == "queued"  # 暂停时退回排队中，不是 idle

    resumed = client.post("/api/projects/p1/series-tasks/queue/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "running"

    _cancel_series_queue("p1")


def test_resume_without_queued_tasks_conflicts(client) -> None:
    _seed_episodes("p1", [1])
    resp = client.post("/api/projects/p1/series-tasks/queue/resume")
    assert resp.status_code == 409, resp.text


def test_pause_without_active_queue_conflicts(client) -> None:
    _seed_episodes("p1", [1])
    resp = client.post("/api/projects/p1/series-tasks/queue/pause")
    assert resp.status_code == 409, resp.text


def test_cancel_queued_task_returns_it_to_idle(client, monkeypatch) -> None:
    _seed_episodes("p1", [1, 2])
    client.post(
        "/api/projects/p1/series-tasks",
        json={"ranges": [{"episode_from": 1, "episode_to": 1}, {"episode_from": 2, "episode_to": 2}]},
    )
    tasks_list = client.get("/api/projects/p1/series-tasks").json()["tasks"]
    first_id, second_id = tasks_list[0]["task_id"], tasks_list[1]["task_id"]

    _stub_stages_block_forever(monkeypatch)
    client.post("/api/projects/p1/series-tasks/enqueue", json={"task_ids": [first_id, second_id]})

    cancelled = client.post("/api/projects/p1/series-tasks/cancel", json={"task_ids": [second_id]})
    assert cancelled.status_code == 200, cancelled.text
    assert second_id in cancelled.json()["cancelled"]
    detail = client.get(f"/api/projects/p1/series-tasks/{second_id}").json()
    assert detail["status"] == "idle"

    _cancel_series_queue("p1")


# --------------------------------------------------------------- exports 接线

def test_list_series_exports_wraps_in_object(client) -> None:
    _seed_episodes("p1", [1])
    resp = client.get("/api/projects/p1/series-exports")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"exports": []}


# ------------------------------------------------- 完成判据挂产物而不是状态字段

def test_idle_task_with_current_film_is_reported_succeeded(client, monkeypatch) -> None:
    """从没跑过、但成片已在盘上且未过期的任务，列表里必须显示「已完成」。

    这种任务真实存在（旧单例连播台留下的成片、重新切分后与旧区间重合的任务）。
    照 series_tasks.status 字段显示会让列表说「未开始」，而点开始时入队又判
    「已完成，成片未过期」把它跳过——界面与实际行为对不上。
    """
    _seed_episodes("p1", [1, 2])
    client.post("/api/projects/p1/series-tasks", json={"group_size": 2})
    listed = client.get("/api/projects/p1/series-tasks").json()
    task = listed["tasks"][0]
    assert task["status"] == "idle" and task["film"] is None  # 前提：此刻确实没产物

    monkeypatch.setattr(
        merge, "film_for_range",
        lambda *_a: {"url": "/media/x.mp4", "duration_s": 1.0, "size_bytes": 1, "created_at": 0.0,
                     "path": "x.mp4", "episode_from": 1, "episode_to": 2, "chapters": []},
    )
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: True)

    refreshed = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]
    assert refreshed["status"] == "succeeded"

    # 成片过期（输入指纹对不上）时不许冒充完成——退回真实字段值。
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    assert client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["status"] == "idle"
