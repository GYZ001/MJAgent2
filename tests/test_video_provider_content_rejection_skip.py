"""供应商终态失败的收口契约（2026-09-05）。

1. 供应商已报告任务终态失败后，绝不能再轮询这个任务：无结构化分类的失败保持
   技术故障、换**新任务**重试——`provider_poll_required` 清零，错误处理器走
   `_schedule_job_retry` 而不是 `_defer_provider_poll`（我欲封天第 21 集第 3 镜：
   一个死任务 2 小时被轮询 109 次刷出 82 条报错，换新任务的第 2 次一次就过）。
2. 同一镜头在 ≥3 个**不同**供应商任务上给出逐字相同的终态失败，才判定为真实的
   模型拒绝：外部终态、`provider_create_state='model_rejected'`，用户提示语说明
   本镜按跳过处理、成片不含本镜。
3. 覆盖账本把「最新 job 以真实拒绝告终」的镜头按已跳过计入覆盖，供应商拒绝的
   镜头不再阻塞整集收口，但整集至少要有一镜可采用。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from app import worker
from app.db import get_conn
from app.media_exec.job_state import (
    CONTENT_REJECTION_MIN_TASKS, PROVIDER_CONTENT_REJECTED_KIND, rejection_repeated_across_tasks,
)
from app.video_supervisor.coverage import _latest_video_jobs
from app.video_supervisor.models import CoverageLedger, ShotCoverageEntry
from tests.conftest import patch_worker_everywhere

VENDOR_MESSAGE = (
    "The request failed because the output video may contain sensitive information. "
    "Request id: 02178859995537500000000000000"
)


def _seed_job(conn: sqlite3.Connection, *, poll_required: int = 1) -> None:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e1','p1',1,'generating',1)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition
           ) VALUES('s1','e1',1,5,'中景','固定','室内','[]','人物站定','[]','硬切')"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,image_inputs,created_at
           ) VALUES('v1','s1',1,'prompt','idem','running','task-3','{}',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               lease_owner,lease_expires_at,provider_operation_id,
               provider_create_state,provider_non_cancellable,
               provider_submitted_at,provider_poll_required,created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','running',
               'worker-1',9999999999,'video-create-v1','accepted',1,1,?,1,1
           )""",
        (poll_required,),
    )
    conn.commit()


def _insert_poll_failure(conn: sqlite3.Connection, task_id: str, ts: float, message: str) -> None:
    conn.execute(
        """INSERT INTO provider_calls(ts, kind, model, status, http_status, latency_ms, error, meta)
           VALUES(?, 'video_poll', 'seedance', 'TASK_FAILED', 200, 10, ?, ?)""",
        (ts, message, json.dumps({"shot_id": "s1", "task_id": task_id})),
    )
    conn.commit()


def _wire(monkeypatch, poll_fn) -> None:
    from app.media_pipeline import concurrency, stage_state

    class Permit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def no_sleep(_delay: float) -> None:
        return None

    async def no_fence(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    patch_worker_everywhere(monkeypatch, "_assert_review_dependency_fence_async", no_fence)
    patch_worker_everywhere(monkeypatch, "_assert_job_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.hiagent, "poll_video_task", poll_fn)
    monkeypatch.setattr(concurrency, "semaphore_for", lambda _resource: Permit())
    monkeypatch.setattr(concurrency, "report_congestion", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(concurrency, "report_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stage_state, "set_pipeline_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.media_scheduler, "renew_lease", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker.media_scheduler, "settle_budget", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "mark_media_job_state", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(
        monkeypatch, "reconcile_episode_generation_status", lambda *_args, **_kwargs: None,
    )


def _failed_poll(task_id: str, *, ts: float):
    async def poll(polled_task_id, *, call_meta=None):
        assert polled_task_id == task_id
        _insert_poll_failure(get_conn(), task_id, ts, VENDOR_MESSAGE)
        return {
            "status": "failed", "video_url": "", "last_frame_url": "",
            "error": VENDOR_MESSAGE, "failure": None,
        }
    return poll


# ---------------------------------------------------------------- 纯判据
def test_rejection_needs_identical_failure_on_distinct_tasks():
    same_task = [("a", VENDOR_MESSAGE)] * 5
    assert rejection_repeated_across_tasks(same_task) is False, "同一任务复轮不算独立证据"
    two_tasks = [("a", VENDOR_MESSAGE), ("b", VENDOR_MESSAGE)]
    assert rejection_repeated_across_tasks(two_tasks) is False
    three_tasks = two_tasks + [("c", VENDOR_MESSAGE)]
    assert rejection_repeated_across_tasks(three_tasks) is True
    mixed = [("a", VENDOR_MESSAGE), ("b", "S3 upload 500"), ("c", VENDOR_MESSAGE), ("d", VENDOR_MESSAGE)]
    assert rejection_repeated_across_tasks(mixed) is True
    assert rejection_repeated_across_tasks([("a", ""), ("b", ""), ("c", "")]) is False
    assert rejection_repeated_across_tasks([]) is False
    assert CONTENT_REJECTION_MIN_TASKS == 3


# ---------------------------------------------------------- 终态失败不复轮
def test_first_unstructured_terminal_failure_resubmits_instead_of_repolling(monkeypatch):
    conn = get_conn()
    _seed_job(conn)
    _wire(monkeypatch, _failed_poll("task-3", ts=3.0))
    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))
    job = conn.execute(
        """SELECT status, retry_count, provider_poll_required, provider_create_state,
                  provider_failure_category
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert job["provider_poll_required"] == 0, "供应商已报终态，不得再轮询这个任务"
    assert job["status"] == "queued" and job["retry_count"] == 1, "应换新任务重试而不是复轮"
    assert job["provider_create_state"] != "model_rejected"
    assert job["provider_failure_category"] == "technical"
    version = conn.execute("SELECT status FROM shot_versions WHERE id='v1'").fetchone()
    assert version["status"] == "queued"


# ---------------------------------------------- 三个独立任务相同 → 真实拒绝
def test_identical_failure_on_three_tasks_is_terminal_rejection(monkeypatch):
    conn = get_conn()
    _seed_job(conn)
    _insert_poll_failure(conn, "task-1", 1.0, VENDOR_MESSAGE)
    _insert_poll_failure(conn, "task-2", 2.0, VENDOR_MESSAGE)
    _wire(monkeypatch, _failed_poll("task-3", ts=3.0))
    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))
    job = conn.execute(
        """SELECT status, provider_create_state, provider_failure_category,
                  provider_failure_kind, error
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert job["status"] == "failed"
    assert job["provider_create_state"] == "model_rejected"
    assert job["provider_failure_category"] == "model_rejection"
    assert job["provider_failure_kind"] == PROVIDER_CONTENT_REJECTED_KIND
    assert "跳过" in job["error"] and "不含本镜" in job["error"]
    assert VENDOR_MESSAGE in job["error"], "供应商原文必须逐字转述"
    version = conn.execute("SELECT status FROM shot_versions WHERE id='v1'").fetchone()
    assert version["status"] == "failed"


def test_same_task_repolled_three_times_is_not_rejection(monkeypatch):
    conn = get_conn()
    _seed_job(conn)
    _insert_poll_failure(conn, "task-3", 1.0, VENDOR_MESSAGE)
    _insert_poll_failure(conn, "task-3", 2.0, VENDOR_MESSAGE)
    _wire(monkeypatch, _failed_poll("task-3", ts=3.0))
    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))
    job = conn.execute("SELECT status, provider_create_state FROM jobs WHERE id='j1'").fetchone()
    assert job["provider_create_state"] != "model_rejected"
    assert job["status"] == "queued"


# ------------------------------------------------------------ 覆盖账本
def _entry(no: int, **kw) -> ShotCoverageEntry:
    return ShotCoverageEntry(shot_no=no, shot_id=f"s{no}", grade=kw.pop("grade", "C"), **kw)


def test_rejected_shot_counts_as_skipped_in_coverage():
    ledger = CoverageLedger(
        episode_id="ep", shots_total=3,
        entries=[
            _entry(1, grade="A", adopted_version_id="v1"),
            _entry(2, provider_rejected=True),
            _entry(3, grade="A", adopted_version_id="v3"),
        ],
    )
    assert ledger.covered_within_quota() is True
    assert ledger.count_uncovered() == 0
    assert ledger.actionable() == []
    assert ledger.exhausted_but_technically_ok() == []
    assert ledger.rejected_shot_nos() == [2]


def test_rejected_shot_does_not_hide_real_gaps_or_empty_film():
    with_gap = CoverageLedger(
        episode_id="ep", shots_total=3,
        entries=[
            _entry(1, grade="A", adopted_version_id="v1"),
            _entry(2, provider_rejected=True),
            _entry(3),
        ],
    )
    assert with_gap.covered_within_quota() is False
    assert with_gap.count_uncovered() == 1
    assert [e.shot_no for e in with_gap.actionable()] == [3]
    all_rejected = CoverageLedger(
        episode_id="ep", shots_total=2,
        entries=[_entry(1, provider_rejected=True), _entry(2, provider_rejected=True)],
    )
    assert all_rejected.covered_within_quota() is False, "整集没有一镜可用就没有东西可合"


def test_latest_video_jobs_marks_rejection_only_when_it_is_the_last_word():
    conn = get_conn()
    _seed_job(conn)
    conn.execute(
        """UPDATE jobs SET status='failed', provider_create_state='model_rejected',
                  lease_owner=NULL WHERE id='j1'"""
    )
    conn.commit()
    active, rejected = _latest_video_jobs(conn, ["s1"], None)
    assert rejected == {"s1"} and active == {}
    # 用户手工重抽：更新的 job 出现后，拒绝不再是本镜的最后结论
    conn.execute(
        """INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,
                            provider_create_state,created_at,updated_at)
           VALUES('j2','video','s1','v1','e1','p1','queued','not_started',5,5)"""
    )
    conn.commit()
    active, rejected = _latest_video_jobs(conn, ["s1"], None)
    assert rejected == set() and active == {"s1": "j2"}
