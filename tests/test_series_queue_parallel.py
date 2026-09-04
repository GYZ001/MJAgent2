"""连播队列的并行、韧性与残留标记用例（从 test_series_queue.py 拆出，夹具共用那边的）。"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app import task_registry
from app.domain.series_ops import merge, queue, tasks
from app.domain.series_ops import stages as series_stages
from tests.test_series_queue import _await_runner, _conn, _patch_conn, _seed_task, _task_row


# --------------------------------------------------------------------- 并行
@pytest.mark.asyncio
async def test_queue_runs_tasks_concurrently_up_to_limit(monkeypatch) -> None:
    """并发度 2、三个任务：前两个必须交错运行，第三个只能在某个任务整体结束后开跑。"""
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(queue, "queue_concurrency", lambda: 2)
    for tid, no in (("st_a", 1), ("st_b", 2), ("st_c", 3)):
        _seed_task(conn, tid, [no])
    order: list[str] = []
    done_pairs: set[tuple[str, str]] = set()

    def fake_complete(stage, _conn, episode_id):
        return (stage, episode_id) in done_pairs

    async def fake_run_stage(stage, episode_id, _run_id):
        order.append(f"{episode_id}:{stage}:start")
        await asyncio.sleep(0.005)
        order.append(f"{episode_id}:{stage}:end")
        done_pairs.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", fake_complete)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})

    result = await queue.enqueue("p", ["st_a", "st_b", "st_c"], False)
    assert result["enqueued"] == 3 and result["queue"]["concurrency"] == 2
    await _await_runner("p", timeout=10.0)
    assert all(_task_row(conn, tid)["status"] == "succeeded" for tid in ("st_a", "st_b", "st_c"))
    first_e2 = min(i for i, ev in enumerate(order) if ev.startswith("e2:"))
    last_e1 = max(i for i, ev in enumerate(order) if ev.startswith("e1:"))
    assert first_e2 < last_e1, "e1/e2 必须交错（并行），不是串行"
    first_e3 = min(i for i, ev in enumerate(order) if ev.startswith("e3:"))
    last_e2 = max(i for i, ev in enumerate(order) if ev.startswith("e2:"))
    assert first_e3 > min(last_e1, last_e2), "e3 只能在某个任务整体结束后开跑（并发上限 2）"


@pytest.mark.asyncio
async def test_cancel_one_running_task_keeps_the_other_running(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(queue, "queue_concurrency", lambda: 2)
    _seed_task(conn, "st_a", [1])
    _seed_task(conn, "st_b", [2])
    release = asyncio.Event()
    started: set[str] = set()
    done_pairs: set[tuple[str, str]] = set()

    async def fake_run_stage(stage, episode_id, _run_id):
        started.add(episode_id)
        await release.wait()
        done_pairs.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", lambda stage, _c, eid: (stage, eid) in done_pairs)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})

    await queue.enqueue("p", ["st_a", "st_b"], False)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if started >= {"e1", "e2"}:
            break
    assert started >= {"e1", "e2"}
    snap = queue.queue_snapshot(conn, "p")
    assert set(snap["running_task_ids"]) == {"st_a", "st_b"}

    result = await queue.cancel("p", ["st_a"])
    assert result["cancelled"] == ["st_a"]
    assert _task_row(conn, "st_a")["status"] == "idle"
    assert _task_row(conn, "st_b")["status"] == "running"
    assert task_registry.active(queue.TASK_KIND, "p")

    release.set()
    await _await_runner("p", timeout=10.0)
    assert _task_row(conn, "st_b")["status"] == "succeeded"


# ------------------------------------------------------------- runner 韧性 / 残留标记
@pytest.mark.asyncio
async def test_runner_survives_transient_db_error_and_still_runs_the_task(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_a", [1])
    real_next = tasks.next_queued_task
    calls = {"n": 0}

    def flaky_next(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_next(*args, **kwargs)
    monkeypatch.setattr(tasks, "next_queued_task", flaky_next)

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    import app.errors as errors_mod
    monkeypatch.setattr(errors_mod, "log_error", lambda *_a, **_k: None)
    done_pairs: set[tuple[str, str]] = set()

    async def fake_run_stage(stage, episode_id, _run_id):
        done_pairs.add((stage, episode_id))
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda stage, _c, eid: (stage, eid) in done_pairs)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})
    await queue.enqueue("p", ["st_a"], False)
    await _await_runner("p")
    assert calls["n"] >= 2
    assert _task_row(conn, "st_a")["status"] == "succeeded"


def test_init_progress_resets_stale_running_markers_from_previous_process() -> None:
    conn = _conn()
    _seed_task(conn, "st_a", [1, 2])
    stale = {
        "episodes": [
            {"episode_id": "e1", "episode_no": 1, "error": None, "waiting": "第1集正被映射台的任务占用",
             "stages": {"screenplay": "done", "storyboard": "running", "confirm": "pending", "video": "pending", "final": "pending"}},
            {"episode_id": "e2", "episode_no": 2, "error": None, "waiting": None,
             "stages": {"screenplay": "running", "storyboard": "pending", "confirm": "pending", "video": "pending", "final": "pending"}},
        ],
        "current_episode_no": 1, "current_stage": "storyboard", "running_episode_nos": [1, 2], "error": None,
    }
    conn.execute("UPDATE series_tasks SET progress_json=? WHERE id='st_a'", (json.dumps(stale),))
    conn.commit()
    row = dict(conn.execute("SELECT * FROM series_tasks WHERE id='st_a'").fetchone())
    progress = queue._init_progress(conn, "p", row)
    assert progress["episodes"][0]["stages"]["storyboard"] == "pending"
    assert progress["episodes"][0]["stages"]["screenplay"] == "done"  # 已完成的不动
    assert progress["episodes"][1]["stages"]["screenplay"] == "pending"
    assert all(e["waiting"] is None for e in progress["episodes"])
