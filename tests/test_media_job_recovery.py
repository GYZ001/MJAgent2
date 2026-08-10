"""recover_media_jobs：服务重启后自动恢复中断的媒体任务。

背景：init_db() 在重启时把 RUNNING 的 workflow_runs 标为 PAUSED_EXTERNAL +
failure_code='SERVICE_RESTART'，但底层 jobs 表的 lease（默认 180s）在重启那一刻
往往还没过期，media_scheduler.recoverable_jobs() 只扫 status='running' AND
lease_expires_at<now 的 job，因此不会恢复——结果用户看到的"任务卡在
'服务重启，可从安全检查点恢复'"。recover_media_jobs() 把这些 job 显式复位回
queued，随后由数据库驱动的持久调度器在下一轮重新发现并交给 worker。
"""
import asyncio
import json
import sqlite3

import pytest
from fastapi import HTTPException

from app import db, worker


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def _seed_restart_interrupted_job(conn: sqlite3.Connection, job_id: str = "j1") -> str:
    """模拟服务重启后 DB 的状态：
    - workflow_runs: status=PAUSED_EXTERNAL, failure_code=SERVICE_RESTART
    - step_runs: status=FAILED (init_db 在重启时置位)
    - jobs: status='running', lease_expires_at=未来（lease 还没过期，所以
      recoverable_jobs() 不会扫到——正是 recover_media_jobs 要解决的场景）
    """
    run_id = "run_test1"
    step_id = "step_test1"
    conn.execute(
        "INSERT INTO workflow_runs(id, workflow_type, scope_type, scope_id, status, "
        "input_fingerprint, failure_code, failure_message, started_at, updated_at) "
        "VALUES(?, 'video_generation', 'shot', 'shot_x', 'PAUSED_EXTERNAL', 'fp', "
        "'SERVICE_RESTART', '服务重启，可从安全检查点恢复', 1.0, 1.0)",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO step_runs(id, run_id, step_key, status, error_code, error_message, "
        "started_at) "
        "VALUES(?, ?, 'video_generation', 'FAILED', 'SERVICE_RESTART', "
        "'服务重启，步骤已中断', 1.0)",
        (step_id, run_id),
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, shot_id, version_id, episode_id, project_id, status, "
        "run_id, step_run_id, lease_owner, lease_expires_at, created_at, updated_at) "
        "VALUES(?, 'video', 'shot_x', 'ver_x', 'ep_x', 'proj_x', 'running', ?, ?, "
        "'w0', 999999999.0, 1.0, 1.0)",
        (job_id, run_id, step_id),
    )
    conn.commit()
    return run_id


def test_restart_interrupted_job_is_resumed(monkeypatch) -> None:
    conn = _conn()
    _seed_restart_interrupted_job(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    enqueued: list[str] = []
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: enqueued.append(jid))

    resumed = worker.recover_media_jobs()
    assert resumed == 1
    # Recovery only repairs durable state; the DB-backed dispatcher owns queue
    # reconstruction so a 50-shot restart cannot flood an arbitrary FIFO order.
    assert enqueued == []
    worker._dispatch_due_jobs()
    assert enqueued == ["j1"]

    job = conn.execute("SELECT status, lease_owner, lease_expires_at, error FROM jobs WHERE id='j1'").fetchone()
    assert job["status"] == "queued"
    assert job["lease_owner"] is None
    assert job["lease_expires_at"] is None
    assert job["error"] is None

    old_step = conn.execute(
        "SELECT status, error_code, error_message FROM step_runs WHERE id='step_test1'"
    ).fetchone()
    assert old_step["status"] == "FAILED"
    assert old_step["error_code"] == "SERVICE_RESTART"

    job_step_id = conn.execute("SELECT step_run_id FROM jobs WHERE id='j1'").fetchone()["step_run_id"]
    assert job_step_id != "step_test1"
    retry_step = conn.execute(
        "SELECT status, iteration_no, parent_step_run_id FROM step_runs WHERE id=?", (job_step_id,)
    ).fetchone()
    assert dict(retry_step) == {
        "status": "READY", "iteration_no": 2, "parent_step_run_id": "step_test1",
    }
    run = conn.execute(
        "SELECT status, failure_message FROM workflow_runs WHERE id='run_test1'"
    ).fetchone()
    assert run["status"] == "WAITING_RETRY"
    assert "自动恢复" in run["failure_message"]


def test_paused_budget_jobs_are_not_resumed(monkeypatch) -> None:
    """集预算不足暂停的 job 不应被自动恢复（需显式 retry_paused 释放预算后重试）。"""
    conn = _conn()
    # PAUSED_BUDGET 的 run，不是 SERVICE_RESTART
    conn.execute(
        "INSERT INTO workflow_runs(id, workflow_type, scope_type, scope_id, status, "
        "input_fingerprint, failure_message, started_at, updated_at) "
        "VALUES('run_b', 'video_generation', 'shot', 'shot_b', 'PAUSED_BUDGET', 'fp', "
        "'集预算不足，任务已暂停', 1.0, 1.0)"
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, status, run_id, step_run_id, created_at, updated_at) "
        "VALUES('j_b', 'video', 'paused_budget', 'run_b', NULL, 1.0, 1.0)"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: None)

    resumed = worker.recover_media_jobs()
    assert resumed == 0
    job = conn.execute("SELECT status FROM jobs WHERE id='j_b'").fetchone()
    assert job["status"] == "paused_budget"


def test_page_approved_budget_overrides_static_safety_default(
    monkeypatch,
) -> None:
    import app.completion_grant as completion_grant

    monkeypatch.setattr(worker, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(
        completion_grant,
        "episode_video_budget_snapshot",
        lambda _episode_id: {
            "baseline_cny": 0.0,
            "claimed_cny": 0.0,
            "used_cny": 0.0,
            "cap_cny": 440.0,
        },
    )
    monkeypatch.setattr(
        completion_grant,
        "active_video_grant_budget_cap",
        lambda _episode_id: None,
    )

    assert worker.episode_video_budget_limit("episode") == 440.0


def test_page_approved_budget_can_be_lower_than_static_default(
    monkeypatch,
) -> None:
    import app.completion_grant as completion_grant

    monkeypatch.setattr(worker, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(
        completion_grant,
        "episode_video_budget_snapshot",
        lambda _episode_id: {
            "baseline_cny": 0.0,
            "claimed_cny": 0.0,
            "used_cny": 0.0,
            "cap_cny": 50.0,
        },
    )
    monkeypatch.setattr(
        completion_grant,
        "active_video_grant_budget_cap",
        lambda _episode_id: None,
    )

    assert worker.episode_video_budget_limit("episode") == 50.0


def test_cancelled_jobs_are_not_resumed(monkeypatch) -> None:
    """人工取消的 job 不应被自动恢复。"""
    conn = _conn()
    conn.execute(
        "INSERT INTO workflow_runs(id, workflow_type, scope_type, scope_id, status, "
        "input_fingerprint, failure_code, started_at, updated_at) "
        "VALUES('run_c', 'video_generation', 'shot', 'shot_c', 'PAUSED_EXTERNAL', 'fp', "
        "'SERVICE_RESTART', 1.0, 1.0)"
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, status, run_id, step_run_id, cancellation_requested, "
        "created_at, updated_at) "
        "VALUES('j_c', 'video', 'running', 'run_c', NULL, 1, 1.0, 1.0)"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: None)

    resumed = worker.recover_media_jobs()
    assert resumed == 0


def test_legacy_keyframe_jobs_are_cancelled_instead_of_recovered(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, scene_status) "
        "VALUES('shot_scene', 'ep_x', 1, 5, 'generating')"
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, shot_id, episode_id, project_id, status, "
        "created_at, updated_at) "
        "VALUES('j_scene', 'scene', 'shot_scene', 'ep_x', 'proj_x', 'queued', 1.0, 1.0)"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: None)

    resumed = worker.recover_media_jobs()

    assert resumed == 0
    job = conn.execute(
        "SELECT status, cancellation_requested FROM jobs WHERE id='j_scene'"
    ).fetchone()
    assert dict(job) == {"status": "cancelled", "cancellation_requested": 1}
    assert conn.execute(
        "SELECT scene_status FROM shots WHERE id='shot_scene'"
    ).fetchone()["scene_status"] == "none"


def test_provider_poll_budget_uses_original_submission_time_after_restart(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO jobs(id, kind, status, provider_operation_id, attempt_started_at, "
        "created_at, updated_at) "
        "VALUES('j_poll', 'video', 'running', 'video-create-v1', 50.0, 50.0, 999.0)"
    )
    conn.execute(
        "INSERT INTO provider_calls(ts, kind, status, operation_id) "
        "VALUES(100.0, 'video_create', 'OK', 'video-create-v1')"
    )
    conn.commit()

    submitted_at = worker._provider_submitted_at(
        conn,
        conn.execute("SELECT * FROM jobs WHERE id='j_poll'").fetchone(),
        "provider-task-1",
    )

    assert submitted_at == 100.0
    assert conn.execute(
        "SELECT provider_submitted_at FROM jobs WHERE id='j_poll'"
    ).fetchone()["provider_submitted_at"] == 100.0


def test_paid_provider_handle_is_recovered_from_successful_call() -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO provider_calls(
               ts, kind, status, operation_id, response_json
           ) VALUES(100.0, 'video_create', 'OK', 'video-create-v1', ?)""",
        (json.dumps({"id": "provider-task-1"}),),
    )
    conn.execute(
        """INSERT INTO provider_calls(
               ts, kind, status, operation_id, response_json
           ) VALUES(101.0, 'video_create', 'FAILED', 'video-create-v1', ?)""",
        (json.dumps({"id": "wrong-task"}),),
    )
    conn.commit()

    recovered = worker._recover_paid_video_task(conn, "video-create-v1")

    assert recovered == ("provider-task-1", 100.0)
    assert worker._recover_paid_video_task(conn, "video-create-other") is None


@pytest.mark.parametrize("create_state", ["submitting", "unknown"])
def test_unresolved_provider_create_is_fail_closed(create_state: str) -> None:
    job = {
        "provider_operation_id": "video-create-v1",
        "provider_create_state": create_state,
        "provider_non_cancellable": 1,
    }

    with pytest.raises(
        worker.ProviderCreateUnresolved,
        match="VIDEO_PROVIDER_CREATE_UNRESOLVED",
    ):
        worker._assert_provider_create_resolved(job, None)


def test_recovered_provider_handle_allows_poll_resume() -> None:
    job = {
        "provider_operation_id": "video-create-v1",
        "provider_create_state": "submitting",
        "provider_non_cancellable": 1,
    }

    worker._assert_provider_create_resolved(job, "provider-task-1")


def test_restarted_submitting_job_never_calls_provider_create(monkeypatch) -> None:
    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'generating',1)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e1',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('v1','s1',1,'prompt','idem','running','{}',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               lease_owner,lease_expires_at,provider_operation_id,
               provider_create_state,provider_non_cancellable,created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','running',
               'restart-worker',9999999999,'video-create-v1',
               'submitting',1,1,1
           )"""
    )
    conn.commit()
    create_calls: list[bool] = []

    async def no_sleep(_delay: float) -> None:
        return None

    async def no_fence(*_args, **_kwargs) -> None:
        return None

    async def create_task(*_args, **_kwargs) -> str:
        create_calls.append(True)
        return "duplicate-task"

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(worker, "_assert_review_dependency_fence_async", no_fence)
    monkeypatch.setattr(worker.hiagent, "create_video_task", create_task)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker, "reconcile_episode_generation_status", lambda *_args, **_kwargs: None,
    )

    asyncio.run(worker._run_job("j1", lease_owner="restart-worker"))

    job = conn.execute(
        "SELECT status,reason_code,provider_create_state FROM jobs WHERE id='j1'"
    ).fetchone()
    assert create_calls == []
    assert dict(job) == {
        "status": "waiting_human",
        "reason_code": "VIDEO_PROVIDER_CREATE_UNRESOLVED",
        "provider_create_state": "submitting",
    }


def test_paid_provider_attempts_count_distinct_operations() -> None:
    conn = _conn()
    conn.executemany(
        """INSERT INTO provider_calls(
               ts, kind, status, operation_id, response_json
           ) VALUES(?, 'video_create', 'OK', ?, ?)""",
        [
            (1, "video-create-v1", json.dumps({"id": "task-1"})),
            (2, "video-create-v1", json.dumps({"id": "task-1"})),
            (3, "video-create-v1-safety-1", json.dumps({"id": "task-2"})),
            (4, "video-create-v1-copyright-1", json.dumps({"id": "task-3"})),
        ],
    )
    conn.commit()

    assert worker._paid_video_attempt_count(conn, "v1") == 3


def test_resubmit_budget_extension_is_atomic_and_capped() -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO jobs(
               id, kind, status, episode_id, reserved_cost_cny, created_at, updated_at
           ) VALUES('j1','video','running','e1',5,1,1)"""
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES('b1','j1','episode','e1',5,'running',1)"""
    )
    conn.commit()

    assert worker.media_scheduler.extend_budget_reservation(
        "j1", "e1", 5, 9, conn=conn,
    ) is False
    assert conn.execute(
        "SELECT amount_cny FROM budget_reservations WHERE job_id='j1'",
    ).fetchone()["amount_cny"] == 5

    assert worker.media_scheduler.extend_budget_reservation(
        "j1", "e1", 5, 10, conn=conn,
    ) is True
    assert conn.execute(
        "SELECT amount_cny FROM budget_reservations WHERE job_id='j1'",
    ).fetchone()["amount_cny"] == 10


def test_video_resubmit_checkpoint_is_persisted_atomically() -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status,
               provider_task_id, image_inputs, created_at
           ) VALUES('v1','s1',1,'old','idem','failed','old-task','{}',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id, kind, status, version_id, provider_operation_id,
               provider_create_state, provider_non_cancellable,
               provider_submitted_at, created_at, updated_at
           ) VALUES(
               'j1','video','running','v1','video-create-v1','accepted',1,50,1,1
           )"""
    )
    conn.commit()

    worker._persist_video_resubmit(
        conn,
        job_id="j1",
        version_id="v1",
        prompt_text="sanitized",
        meta={"seedance_safety_retry": True},
        operation_id="video-create-v1-safety-1",
    )

    version = conn.execute(
        "SELECT prompt_text, provider_task_id, image_inputs FROM shot_versions WHERE id='v1'"
    ).fetchone()
    job = conn.execute(
        """SELECT provider_operation_id, provider_create_state,
                  provider_non_cancellable, provider_submitted_at
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert dict(version) == {
        "prompt_text": "sanitized",
        "provider_task_id": None,
        "image_inputs": '{"seedance_safety_retry": true}',
    }
    assert dict(job) == {
        "provider_operation_id": "video-create-v1-safety-1",
        "provider_create_state": "not_started",
        "provider_non_cancellable": 0,
        "provider_submitted_at": None,
    }


def test_reference_mode_submission_authority_failure_precedes_paid_marker(
    monkeypatch,
) -> None:
    import app.video_plan as video_plan

    conn = _conn()
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,status,provider_non_cancellable,created_at,updated_at
           ) VALUES('j-plan','video','s-plan','running',0,1,1)"""
    )
    conn.commit()
    job = conn.execute("SELECT * FROM jobs WHERE id='j-plan'").fetchone()
    calls: list[dict] = []

    def reject_submission(**kwargs):
        calls.append(kwargs)
        raise video_plan.VideoPlanValidationError([{
            "code": "SHOT_CONTRACT_FINGERPRINT_STALE",
            "shot_id": "s-plan",
        }])

    monkeypatch.setattr(
        video_plan,
        "assert_video_provider_submission_authority",
        reject_submission,
    )

    with pytest.raises(worker.VideoPlanStaleFence):
        worker._assert_video_provider_submission_authority(
            conn,
            job=job,
            meta={
                "mode": "REFERENCE_IMAGE_MODE",
                "shot_plan_id": "svp-plan",
                "capability_snapshot_id": "cap-plan",
            },
            actual_mode="REFERENCE_IMAGE_MODE",
            write_point="provider_non_cancellable",
        )

    assert calls == [{
        "shot_id": "s-plan",
        "shot_plan_id": "svp-plan",
        "actual_mode": "REFERENCE_IMAGE_MODE",
        "expected_capability_snapshot_id": "cap-plan",
        "conn": conn,
    }]
    assert conn.execute(
        "SELECT provider_non_cancellable FROM jobs WHERE id='j-plan'",
    ).fetchone()["provider_non_cancellable"] == 0


def test_manual_retry_distinguishes_poll_from_new_submission(monkeypatch) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status,
               provider_task_id, created_at
           ) VALUES('v-paid','s1',1,'p','i1','failed','provider-task-1',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id, kind, status, version_id, created_at, updated_at
           ) VALUES('j-paid','video','failed','v-paid',1,1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id, kind, status, episode_id, created_at, updated_at
           ) VALUES('j-new','video','failed','e1',1,1)"""
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(system_api, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)

    paid = system_api.retry_job("j-paid")
    new = system_api.retry_job("j-new")

    assert paid["retryability"]["action"] == "continue_poll"
    assert paid["retryability"]["will_submit_new_provider_task"] is False
    assert paid["job"]["status"] == "waiting_provider"
    assert new["retryability"]["action"] == "new_submission"
    assert new["retryability"]["will_submit_new_provider_task"] is True
    assert new["job"]["status"] == "queued"


def test_manual_budget_retry_only_resumes_requested_job(monkeypatch) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    conn.executemany(
        """INSERT INTO jobs(
               id, kind, status, episode_id, reserved_cost_cny, created_at, updated_at
           ) VALUES(?, 'video', 'paused_budget', 'e1', 1, 1, 1)""",
        [("j-budget-1",), ("j-budget-2",)],
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(system_api, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)

    result = system_api.retry_job("j-budget-1")

    assert result["retryability"]["action"] == "resume_budget_paused"
    statuses = {
        row["id"]: row["status"]
        for row in conn.execute(
            "SELECT id,status FROM jobs ORDER BY id",
        ).fetchall()
    }
    assert statuses == {
        "j-budget-1": "queued",
        "j-budget-2": "paused_budget",
    }


def test_manual_retry_recovers_persisted_provider_handle_before_queueing(monkeypatch) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status, created_at
           ) VALUES('v-recover','s1',1,'p','i1','failed',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id, kind, status, version_id, episode_id, provider_operation_id,
               provider_create_state, provider_non_cancellable, created_at, updated_at
           ) VALUES(
               'j-recover','video','failed','v-recover','e1','video-create-v-recover',
               'unknown',1,1,1
           )"""
    )
    conn.execute(
        """INSERT INTO provider_calls(
               ts, kind, status, operation_id, response_json
           ) VALUES(100, 'video_create', 'OK', 'video-create-v-recover', ?)""",
        (json.dumps({"id": "provider-task-recovered"}),),
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)

    result = system_api.retry_job("j-recover")

    assert result["retryability"]["action"] == "continue_poll"
    assert result["job"]["status"] == "waiting_provider"
    assert conn.execute(
        "SELECT provider_task_id FROM shot_versions WHERE id='v-recover'",
    ).fetchone()["provider_task_id"] == "provider-task-recovered"


def test_manual_retry_requires_confirmation_for_unresolved_provider_create(monkeypatch) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status, created_at
           ) VALUES('v-unknown','s1',1,'p','i1','failed',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id, kind, status, version_id, episode_id, provider_operation_id,
               provider_create_state, provider_non_cancellable, reserved_cost_cny,
               reason_code, created_at, updated_at
           ) VALUES(
               'j-unknown','video','waiting_human','v-unknown','e1','video-create-v-unknown',
               'not_started',0,1,'VIDEO_PROVIDER_CREATE_UNRESOLVED',1,1
           )"""
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(system_api, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)

    with pytest.raises(HTTPException) as rejected:
        system_api.retry_job("j-unknown")

    assert rejected.value.detail["code"] == "PROVIDER_HANDLE_UNCONFIRMED"
    assert rejected.value.detail["retryability"]["action"] == "confirm_new_submission"
    assert conn.execute(
        "SELECT status FROM jobs WHERE id='j-unknown'",
    ).fetchone()["status"] == "waiting_human"

    confirmed = system_api.retry_job(
        "j-unknown",
        {"allow_new_submission": True},
    )
    assert confirmed["retryability"]["action"] == "new_submission_after_unconfirmed_provider"
    assert confirmed["retryability"]["will_submit_new_provider_task"] is True
    assert confirmed["job"]["status"] == "queued"


def test_manual_retry_rejects_typed_terminal_provider_failure(monkeypatch) -> None:
    import app.system_api as system_api

    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status,
               provider_task_id, created_at
           ) VALUES('v-failed','s1',1,'p','i1','failed','provider-task-1',1)"""
    )
    conn.execute(
            """INSERT INTO jobs(
                   id, kind, status, version_id, error, provider_create_state,
                   created_at, updated_at
               ) VALUES(
                   'j-failed','video','failed','v-failed',
                   '任意上游错误','model_rejected',1,1
               )"""
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)

    with pytest.raises(HTTPException) as rejected:
        system_api.retry_job("j-failed")

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "PROVIDER_TASK_TERMINAL_FAILED"
    assert rejected.value.detail["retryability"]["action"] == "create_new_version"


def test_manual_retry_resumes_video_input_repair_waiting_human(
    monkeypatch,
) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e1','p1',1,'confirmed',1)"""
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s)
           VALUES('s1','e1',1,5)"""
    )
    conn.execute(
        """INSERT INTO provider_video_capability_snapshots(
               id,provider,model,capabilities_json,probe_time,probe_result,
               technical_success,created_at
           ) VALUES('cap','provider','model','{}',1,'succeeded',1,1)"""
    )
    conn.execute(
        """INSERT INTO episode_video_generation_plans(
               id,episode_id,plan_revision,source_storyboard_revision_id,
               capability_snapshot_id,status,created_at
           ) VALUES('evp','e1',1,'board','cap','valid',1)"""
    )
    conn.execute(
        """INSERT INTO shot_video_generation_plans(
               id,episode_video_plan_id,shot_id,shot_no,planned_mode,
               capability_snapshot_id,status,created_at,updated_at
           ) VALUES(
               'svp','evp','s1',1,'FIRST_LAST_FRAME_MODE','cap',
               'waiting_asset',1,1
           )"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES(
               'v1','s1',1,'p','i','waiting_human',
               '{"shot_plan_id":"svp"}',1
           )"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               reason_code,created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','waiting_human',
               'FIRST_LAST_FRAME_REPAIR_REQUIRED',1,1
           )"""
    )
    conn.commit()
    for module in (system_api, monitoring):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "episode_video_budget_limit", lambda _episode_id: 100)
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)

    result = system_api.retry_job("j1")

    assert result["job"]["status"] == "queued"
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v1'"
    ).fetchone()["status"] == "queued"
    assert conn.execute(
        "SELECT status FROM shot_video_generation_plans WHERE id='svp'"
    ).fetchone()["status"] == "planned"
    assert conn.execute(
        "SELECT status FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()["status"] == "reserved"


def test_episode_leaves_generating_when_no_video_job_is_active(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, hook, cliffhanger, "
        "source_chapters, target_duration_s, status, created_at) "
        "VALUES('ep_idle', 'proj_x', 1, 't', 'h', 'c', '[]', 60, 'generating', 1.0)"
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, episode_id, status, created_at, updated_at) "
        "VALUES('j_failed', 'video', 'ep_idle', 'failed', 1.0, 1.0)"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    assert worker.reconcile_episode_generation_status("ep_idle") is True
    assert conn.execute(
        "SELECT status FROM episodes WHERE id='ep_idle'"
    ).fetchone()["status"] == "confirmed"


def test_episode_stays_generating_while_a_video_job_is_active(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, hook, cliffhanger, "
        "source_chapters, target_duration_s, status, created_at) "
        "VALUES('ep_busy', 'proj_x', 1, 't', 'h', 'c', '[]', 60, 'generating', 1.0)"
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, episode_id, status, created_at, updated_at) "
        "VALUES('j_running', 'video', 'ep_busy', 'running', 1.0, 1.0)"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    assert worker.reconcile_episode_generation_status("ep_busy") is False
    assert conn.execute(
        "SELECT status FROM episodes WHERE id='ep_busy'"
    ).fetchone()["status"] == "generating"


def test_stale_lease_sweeper_reclaims_expired_lease(monkeypatch) -> None:
    """worker 崩溃/OOM 后 lease 过期的 job 被周期 sweeper 回收。"""
    conn = _conn()
    # lease 已过期的 running job（非服务重启场景）
    conn.execute(
        "INSERT INTO workflow_runs(id, workflow_type, scope_type, scope_id, status, "
        "input_fingerprint, started_at, updated_at) "
        "VALUES('run_s', 'video_generation', 'shot', 'shot_s', 'RUNNING', 'fp', 1.0, 1.0)"
    )
    conn.execute(
        "INSERT INTO step_runs(id, run_id, step_key, status, started_at) "
        "VALUES('step_s', 'run_s', 'video_generation', 'RUNNING', 1.0)"
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, status, run_id, step_run_id, lease_owner, "
        "lease_expires_at, created_at, updated_at) "
        "VALUES('j_s', 'video', 'running', 'run_s', 'step_s', 'w_dead', 1.0, 1.0, 1.0)"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    enqueued: list[str] = []
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: enqueued.append(jid))

    async def run() -> list[str]:
        # 直接调用 sweeper 的一次循环逻辑（不等 sleep）
        from app.db import now as db_now
        rows = worker.rows_to_dicts(conn.execute(
            "SELECT id, run_id, step_run_id FROM jobs WHERE status='running' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ? "
            "AND cancellation_requested=0 AND abandoned=0",
            (db_now(),),
        ))
        for r in rows:
            worker._recover_one_media_job(
                conn, r["id"], r["run_id"], r["step_run_id"],
                "lease 过期，自动回收并重新入队",
            )
        conn.commit()
        assert enqueued == []
        worker._dispatch_due_jobs()
        return enqueued

    result = asyncio.run(run())
    assert result == ["j_s"]
    job = conn.execute("SELECT status, lease_owner FROM jobs WHERE id='j_s'").fetchone()
    assert job["status"] == "queued"
    assert job["lease_owner"] is None


def test_media_run_resume_adapter_requeues_exact_paused_job(monkeypatch) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api
    from app.evidence import repository
    from app.orchestration import api as orchestration_api
    from app.orchestration import media_scheduler

    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'confirmed',1)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e1',1,5)"
    )
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v1','s1',1,'p','i','paused',1)"
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               failure_code,updated_at
           ) VALUES(
               'run-media','video_generation','shot','s1','PAUSED_EXTERNAL','fp',
               'USER_PAUSED',1
           )"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,run_id,
               provider_create_state,created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','paused','run-media',
               'not_started',1,1
           )"""
    )
    conn.commit()
    for module in (
        worker, monitoring, system_api, orchestration_api, media_scheduler, repository,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)
    monkeypatch.setattr(system_api, "get_setting", lambda *_args: "100")

    result = asyncio.run(
        orchestration_api.resume_run("run-media", allow_new_submission=True)
    )

    assert result["accepted"] is True
    assert result["job"]["id"] == "j1"
    assert result["job"]["status"] == "queued"
    assert conn.execute(
        "SELECT status FROM jobs WHERE id='j1'"
    ).fetchone()["status"] == "queued"
