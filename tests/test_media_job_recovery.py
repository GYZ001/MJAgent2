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
from tests.conftest import patch_video_plan_everywhere, patch_worker_everywhere


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


def _authorize_video_retry(
    conn: sqlite3.Connection,
    episode_id: str,
    *,
    cap_cny: float = 100.0,
) -> None:
    from app.completion_grant import ensure_video_budget_authority_tables

    ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO episode_video_budget_authorities(
               episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
           ) VALUES(?,0,?,'test-retry',1,1)
           ON CONFLICT(episode_id) DO UPDATE SET cap_cny=excluded.cap_cny""",
        (episode_id, cap_cny),
    )
    conn.commit()


def _seed_retry_episode(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e1','p1',1,'confirmed',1)"""
    )


def _seed_retryable_video_job(
    conn: sqlite3.Connection,
    *,
    job_id: str = "j-retry",
    operation_id: str = "video-create-v-retry-old",
) -> None:
    conn.execute(
        "INSERT INTO projects(id,name,created_at) VALUES('p-retry','P',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e-retry','p-retry',1,'confirmed',1)"""
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s)
           VALUES('s-retry','e-retry',1,5)"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v-retry','s-retry',1,'p','i-retry','failed',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_operation_id,provider_create_state,reserved_cost_cny,
               created_at,updated_at
           ) VALUES(?, 'video','s-retry','v-retry','e-retry','p-retry','failed',
                    ?,'not_started',0.25,1,1)""",
        (job_id, operation_id),
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES('budget-old',?,'episode','e-retry',0.25,'reserved',1)""",
        (job_id,),
    )
    conn.commit()


def _seed_unresolved_provider_create(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               video_slot_active,created_at
           ) VALUES('v1','s1',1,'prompt','idem','running',1,1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,version_id,status,lease_owner,lease_expires_at,
               video_slot_active,created_at,updated_at
           ) VALUES('j1','video','v1','running','worker-1',9999999999,1,1,1)"""
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES('b1','j1','episode','e1',1,'running',1)"""
    )
    conn.commit()


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
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: None)

    resumed = worker.recover_media_jobs()
    assert resumed == 0
    job = conn.execute("SELECT status FROM jobs WHERE id='j_b'").fetchone()
    assert job["status"] == "paused_budget"


def test_soft_deleted_project_job_is_not_resumed(monkeypatch) -> None:
    """回收站项目残留的中断媒体任务不应被启动恢复重新拉起继续烧算力，
    未删除项目的同类残留任务照常恢复（不能把恢复功能整个关掉）。"""
    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('proj-deleted','P',1)")
    conn.execute("UPDATE projects SET deleted_at=999 WHERE id='proj-deleted'")
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('proj-live','P2',1)")
    for suffix, project_id in (("deleted", "proj-deleted"), ("live", "proj-live")):
        run_id, step_id, job_id = f"run_{suffix}", f"step_{suffix}", f"j_{suffix}"
        conn.execute(
            "INSERT INTO workflow_runs(id, workflow_type, scope_type, scope_id, status, "
            "input_fingerprint, failure_code, failure_message, started_at, updated_at) "
            "VALUES(?, 'video_generation', 'shot', ?, 'PAUSED_EXTERNAL', 'fp', "
            "'SERVICE_RESTART', '服务重启，可从安全检查点恢复', 1.0, 1.0)",
            (run_id, f"shot_{suffix}"),
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
            "VALUES(?, 'video', ?, ?, ?, ?, 'running', ?, ?, 'w0', 999999999.0, 1.0, 1.0)",
            (job_id, f"shot_{suffix}", f"ver_{suffix}", f"ep_{suffix}", project_id, run_id, step_id),
        )
    conn.commit()
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: None)

    resumed = worker.recover_media_jobs()

    assert resumed == 1
    deleted_job = conn.execute("SELECT status FROM jobs WHERE id='j_deleted'").fetchone()
    assert deleted_job["status"] == "running", "回收站项目的残留任务不应被复位为可调度状态"
    live_job = conn.execute("SELECT status FROM jobs WHERE id='j_live'").fetchone()
    assert live_job["status"] == "queued", "未删除项目的残留任务必须照常恢复"


def test_page_approved_budget_overrides_static_safety_default(
    monkeypatch,
) -> None:
    import app.completion_grant as completion_grant

    patch_worker_everywhere(monkeypatch, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(
        completion_grant,
        "episode_video_budget_snapshot",
        # conn 已改为必传（app/completion_grant.py，连接所有权显式化），桩要跟着收
        lambda _episode_id, *, conn: {
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

    patch_worker_everywhere(monkeypatch, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(
        completion_grant,
        "episode_video_budget_snapshot",
        # conn 已改为必传（app/completion_grant.py，连接所有权显式化），桩要跟着收
        lambda _episode_id, *, conn: {
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
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
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
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
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


def test_unresolved_provider_create_transition_is_atomic() -> None:
    conn = _conn()
    _seed_unresolved_provider_create(conn)

    assert worker._commit_provider_create_unresolved(
        conn,
        job_id="j1",
        version_id="v1",
        owner="worker-1",
        message="provider create unresolved",
    )

    job = conn.execute(
        """SELECT status,reason_code,lease_owner,video_slot_active
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert dict(job) == {
        "status": "waiting_human",
        "reason_code": "VIDEO_PROVIDER_CREATE_UNRESOLVED",
        "lease_owner": None,
        "video_slot_active": 0,
    }
    version = conn.execute(
        "SELECT status,video_slot_active FROM shot_versions WHERE id='v1'"
    ).fetchone()
    assert version["status"] == "waiting_human"
    # 死锁根因：转人工后若不清 video_slot_active，_begin_video_preflight_job
    # 的镜头级独占锁永远不会释放，重新生成会被永久短路成假的 reused:True。
    assert version["video_slot_active"] == 0
    assert conn.execute(
        "SELECT status FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()["status"] == "reserved"


def test_unresolved_provider_create_transition_rolls_back_every_write() -> None:
    conn = _conn()
    _seed_unresolved_provider_create(conn)
    conn.execute(
        """CREATE TRIGGER fail_unresolved_budget_transition
           BEFORE UPDATE OF status ON budget_reservations
           WHEN NEW.job_id='j1'
           BEGIN
               SELECT RAISE(ABORT, 'budget transition failed');
           END"""
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="budget transition failed"):
        worker._commit_provider_create_unresolved(
            conn,
            job_id="j1",
            version_id="v1",
            owner="worker-1",
            message="provider create unresolved",
        )

    job = conn.execute(
        "SELECT status,reason_code,lease_owner FROM jobs WHERE id='j1'"
    ).fetchone()
    assert dict(job) == {
        "status": "running",
        "reason_code": None,
        "lease_owner": "worker-1",
    }
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v1'"
    ).fetchone()["status"] == "running"
    assert conn.execute(
        "SELECT status FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()["status"] == "running"


def test_unresolved_provider_create_transition_keeps_lease_cas() -> None:
    conn = _conn()
    _seed_unresolved_provider_create(conn)

    assert not worker._commit_provider_create_unresolved(
        conn,
        job_id="j1",
        version_id="v1",
        owner="stale-worker",
        message="provider create unresolved",
    )

    assert conn.execute(
        "SELECT status FROM jobs WHERE id='j1'"
    ).fetchone()["status"] == "running"
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v1'"
    ).fetchone()["status"] == "running"
    assert conn.execute(
        "SELECT status FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()["status"] == "running"


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

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    patch_worker_everywhere(monkeypatch, "_assert_review_dependency_fence_async", no_fence)
    monkeypatch.setattr(worker.hiagent, "create_video_task", create_task)
    patch_worker_everywhere(monkeypatch, "mark_media_job_state", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "reconcile_episode_generation_status", lambda *_args, **_kwargs: None,
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


@pytest.mark.parametrize(
    "provider_failure",
    [
        pytest.param(("responded", "http-409", False, False), id="http-409"),
        pytest.param(("responded", "http-429", False, False), id="http-429"),
        pytest.param(("responded", "http-500", False, False), id="http-5xx"),
        pytest.param(("unknown", "malformed-2xx", False, False), id="malformed-2xx"),
        pytest.param(("unknown", "read-timeout", False, False), id="read-timeout"),
        pytest.param(("unknown", "write-timeout", False, False), id="write-timeout"),
        pytest.param(("not_sent", "connect-timeout", True, False), id="connect-timeout"),
        pytest.param(("responded", "typed-reject", False, True), id="typed-not-accepted"),
    ],
)
def test_create_response_without_task_id_waits_for_human_and_never_replays(
    monkeypatch, provider_failure: tuple[str, str, bool, bool],
) -> None:
    from app import completion_grant
    from app.media_pipeline import scheduler, stage_state
    from app.media_pipeline import concurrency

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
               lease_owner,lease_expires_at,provider_create_state,
               created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','running',
               'worker-1',9999999999,'not_started',1,1
           )"""
    )
    completion_grant.ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,project_id,episode_id,shot_id,job_id,version_id,
               origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
               amount_cny,status,created_at,updated_at
           ) VALUES(
               'video-create-v1','p1','e1','s1','j1','v1',
               'e1','s1','j1','v1',1,'reserved',1,1
           )"""
    )
    conn.commit()
    create_calls: list[str] = []
    _, _, replay_safe, create_not_accepted = provider_failure
    async def no_sleep(_delay: float) -> None:
        return None

    async def no_fence(*_args, **_kwargs) -> None:
        return None

    async def direct_await(awaitable, **_kwargs):
        return await awaitable

    async def prepare_inputs(
        _conn, _job, _version, _shot, _episode, meta, prompt, **_kwargs,
    ):
        return meta, prompt

    async def create_without_handle(*_args, **_kwargs) -> str:
        create_calls.append("create")
        delivery_state, label, replay_safe, create_not_accepted = provider_failure
        raise worker.ProviderError(
            label,
            retryable=False,
            delivery_state=delivery_state,
            replay_safe=replay_safe,
            requires_explicit_retry=not replay_safe,
            create_not_accepted=create_not_accepted,
        )

    class Permit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    patch_worker_everywhere(monkeypatch, "_assert_job_lease", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "_assert_review_dependency_fence_async", no_fence)
    patch_worker_everywhere(monkeypatch, "_assert_video_provider_submission_authority_async", no_fence,
    )
    patch_worker_everywhere(monkeypatch, "_await_with_job_lease_heartbeat", direct_await)
    patch_worker_everywhere(monkeypatch, "_ensure_ai_video_prompt", prepare_inputs)
    patch_worker_everywhere(monkeypatch, "_prepare_planned_mode_inputs", prepare_inputs)
    patch_worker_everywhere(monkeypatch, "ensure_source_excerpt_in_prompt", lambda prompt, _shot: prompt,
    )
    patch_worker_everywhere(monkeypatch, "_load_shot_model", lambda _shot: object())
    patch_worker_everywhere(monkeypatch, "_video_image_inputs_from_meta", lambda _meta: [])
    monkeypatch.setattr(
        worker.video_modes, "build_seedance_video_inputs", lambda _meta: [],
    )
    monkeypatch.setattr(worker.hiagent, "create_video_task", create_without_handle)

    def admit(**_kwargs):
        return True, None

    monkeypatch.setattr(scheduler, "can_admit_video_submit", admit)
    monkeypatch.setattr(concurrency, "semaphore_for", lambda _resource: Permit())
    monkeypatch.setattr(concurrency, "report_congestion", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(concurrency, "report_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stage_state, "set_pipeline_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        completion_grant, "reserve_provider_video_budget", lambda **_kwargs: True,
    )
    patch_worker_everywhere(monkeypatch, "mark_media_job_state", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "reconcile_episode_generation_status", lambda *_args, **_kwargs: None,
    )

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))

    job = conn.execute(
        """SELECT status,reason_code,provider_create_state,
                  provider_non_cancellable
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    claim_status = conn.execute(
        """SELECT status FROM provider_video_budget_claims
           WHERE operation_id='video-create-v1'"""
    ).fetchone()["status"]

    create_unknown = not replay_safe and not create_not_accepted
    if not create_unknown:
        assert job["status"] != "waiting_human"
        assert job["provider_create_state"] == "not_started"
        assert job["provider_non_cancellable"] == 0
        assert claim_status == "released"
        assert create_calls == ["create"]
        return

    assert dict(job) == {
        "status": "waiting_human",
        "reason_code": "VIDEO_PROVIDER_CREATE_UNRESOLVED",
        "provider_create_state": "unknown",
        "provider_non_cancellable": 1,
    }, create_calls
    assert claim_status == "reserved"

    conn.execute(
        """UPDATE jobs
              SET status='running',lease_owner='worker-2',lease_expires_at=9999999999
            WHERE id='j1'"""
    )
    conn.commit()
    asyncio.run(worker._run_job("j1", lease_owner="worker-2"))

    assert create_calls == ["create"]
    assert conn.execute(
        "SELECT status FROM jobs WHERE id='j1'"
    ).fetchone()["status"] == "waiting_human"


@pytest.mark.parametrize(
    ("failure_payload", "expected_job", "expected_version_status"),
    [
        pytest.param(
            {
                "category": "technical",
                "kind": "provider_task_not_found",
                "disposition": "manual_review",
                "retryable": False,
            },
            {
                "status": "waiting_human",
                "provider_create_state": "accepted",
                "provider_failure_category": "technical",
                "provider_failure_kind": "provider_task_not_found",
                "provider_failure_disposition": "manual_review",
                "provider_failure_retryable": 0,
                "reason_code": "VIDEO_PROVIDER_TASK_NOT_FOUND",
            },
            "waiting_human",
            id="technical-failure-waits-for-human",
        ),
        pytest.param(
            {
                "category": "model_rejection",
                "kind": "provider_rejected",
                "disposition": "external_terminal",
                "retryable": False,
            },
            {
                "status": "failed",
                "provider_create_state": "model_rejected",
                "provider_failure_category": "model_rejection",
                "provider_failure_kind": "provider_rejected",
                "provider_failure_disposition": "external_terminal",
                "provider_failure_retryable": 0,
                "reason_code": "VIDEO_PROVIDER_MODEL_REJECTED",
            },
            "failed",
            id="explicit-model-rejection-is-external-terminal",
        ),
    ],
)
def test_run_job_persists_structured_provider_failure_outcome(
    monkeypatch,
    failure_payload: dict,
    expected_job: dict,
    expected_version_status: str,
) -> None:
    from app.media_pipeline import concurrency, stage_state

    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e1','p1',1,'generating',1)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition
           ) VALUES(
               's1','e1',1,5,'中景','固定','室内','[]','人物站定','[]','硬切'
           )"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,image_inputs,created_at
           ) VALUES(
               'v1','s1',1,'prompt','idem','running',
               'minimax_h3:missing-task','{}',1
           )"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               lease_owner,lease_expires_at,provider_operation_id,
               provider_create_state,provider_non_cancellable,
               provider_submitted_at,created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','running',
               'worker-1',9999999999,'video-create-v1',
               'accepted',1,1,1,1
           )"""
    )
    conn.commit()

    class Permit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class ErrorRecord:
        error_id = "ERR-test"
        public = "供应商技术失败（LLM · ERR-test）"

    logged_errors: list[BaseException] = []

    def log_error(exc: BaseException, **_kwargs) -> ErrorRecord:
        logged_errors.append(exc)
        return ErrorRecord()

    async def no_sleep(_delay: float) -> None:
        return None

    async def no_fence(*_args, **_kwargs) -> None:
        return None

    async def poll_missing_task(*_args, **_kwargs) -> dict:
        return {
            "status": "failed",
            "video_url": "",
            "last_frame_url": "",
            "error": "MiniMaxH3 队列和历史中均找不到该任务",
            "failure": failure_payload,
        }

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    patch_worker_everywhere(monkeypatch, "_assert_review_dependency_fence_async", no_fence)
    patch_worker_everywhere(monkeypatch, "_assert_job_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.hiagent, "poll_video_task", poll_missing_task)
    monkeypatch.setattr(concurrency, "semaphore_for", lambda _resource: Permit())
    monkeypatch.setattr(concurrency, "report_congestion", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(concurrency, "report_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stage_state, "set_pipeline_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.errors, "log_error", log_error)
    monkeypatch.setattr(worker.media_scheduler, "renew_lease", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker.media_scheduler, "settle_budget", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "mark_media_job_state", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "reconcile_episode_generation_status", lambda *_args, **_kwargs: None,
    )

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))

    assert [
        f"{type(error).__name__}: {error}" for error in logged_errors
    ] == ["ProviderError: 视频模型 任务失败：MiniMaxH3 队列和历史中均找不到该任务"]
    job = conn.execute(
        """SELECT status,provider_create_state,provider_failure_category,
                  provider_failure_kind,provider_failure_disposition,
                  provider_failure_retryable,reason_code
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert dict(job) == expected_job
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v1'"
    ).fetchone()["status"] == expected_version_status


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

    patch_video_plan_everywhere(
        monkeypatch,
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
    _seed_retry_episode(conn)
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
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s-new','e1',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v-new','s-new',1,'p','i-new','failed',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,status,shot_id,version_id,episode_id,created_at,updated_at
           ) VALUES('j-new','video','failed','s-new','v-new','e1',1,1)"""
    )
    conn.commit()
    _authorize_video_retry(conn, "e1")
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(system_api, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

    paid = system_api.retry_job("j-paid")
    new = system_api.retry_job("j-new")

    assert paid["retryability"]["action"] == "continue_poll"
    assert paid["retryability"]["will_submit_new_provider_task"] is False
    assert paid["job"]["status"] == "waiting_provider"
    assert new["retryability"]["action"] == "new_submission"
    assert new["retryability"]["will_submit_new_provider_task"] is True
    assert new["job"]["status"] == "queued"


def test_new_submission_retry_recalculates_budget_and_double_click_is_idempotent(
    monkeypatch,
) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api
    from app.completion_grant import ensure_video_budget_authority_tables

    conn = _conn()
    _seed_retryable_video_job(conn)
    _authorize_video_retry(conn, "e-retry")
    ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,project_id,episode_id,shot_id,job_id,version_id,
               origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
               amount_cny,status,created_at,updated_at
           ) VALUES(
               'video-create-v-retry-old','p-retry','e-retry','s-retry',
               'j-retry','v-retry','e-retry','s-retry','j-retry','v-retry',
               0.25,'reserved',1,1
           )"""
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

    result = system_api.retry_job("j-retry", {"expected_version": 0})
    new_operation_id = result["job"]["provider_operation_id"]

    assert new_operation_id != "video-create-v-retry-old"
    assert new_operation_id.startswith("video-create-v-retry-epoch_")
    reservation = conn.execute(
        """SELECT amount_cny,status FROM budget_reservations
           WHERE job_id='j-retry'"""
    ).fetchone()
    assert dict(reservation) == {"amount_cny": 4.0, "status": "reserved"}
    claims = conn.execute(
        """SELECT operation_id,amount_cny,status
           FROM provider_video_budget_claims ORDER BY created_at,operation_id"""
    ).fetchall()
    assert [dict(row) for row in claims] == [
        {
            "operation_id": "video-create-v-retry-old",
            "amount_cny": 0.25,
            "status": "released",
        },
        {
            "operation_id": new_operation_id,
            "amount_cny": 4.0,
            "status": "reserved",
        },
    ]

    with pytest.raises(HTTPException) as duplicate:
        system_api.retry_job("j-retry", {"expected_version": 0})
    assert duplicate.value.detail["code"] == "JOB_STATE_CONFLICT"
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_video_budget_claims"
    ).fetchone()[0] == 2


def test_new_submission_retry_fails_closed_without_episode_authority(
    monkeypatch,
) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    _seed_retryable_video_job(conn)
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

    with pytest.raises(HTTPException) as blocked:
        system_api.retry_job("j-retry")

    assert blocked.value.detail["code"] == "JOB_RETRY_AUTHORITY_MISSING"
    job = conn.execute(
        "SELECT status,reserved_cost_cny,reason_code FROM jobs WHERE id='j-retry'"
    ).fetchone()
    assert dict(job) == {
        "status": "paused_budget",
        "reserved_cost_cny": 0.0,
        "reason_code": "VIDEO_BUDGET_NOT_AUTHORIZED",
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_video_budget_claims"
    ).fetchone()[0] == 0


def test_new_submission_retry_blocks_when_authority_budget_is_insufficient(
    monkeypatch,
) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    _seed_retryable_video_job(conn)
    _authorize_video_retry(conn, "e-retry", cap_cny=3.0)
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

    with pytest.raises(HTTPException) as blocked:
        system_api.retry_job("j-retry")

    assert blocked.value.detail["code"] == "JOB_RETRY_BUDGET_BLOCKED"
    assert conn.execute(
        "SELECT status FROM jobs WHERE id='j-retry'"
    ).fetchone()["status"] == "paused_budget"
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_video_budget_claims"
    ).fetchone()[0] == 0


def test_manual_budget_retry_only_resumes_requested_job(monkeypatch) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    _seed_retry_episode(conn)
    conn.executemany(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,?,5)",
        [("s-budget-1", "e1", 1), ("s-budget-2", "e1", 2)],
    )
    conn.executemany(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES(?,?,1,'p',?,'paused_budget',1)""",
        [
            ("v-budget-1", "s-budget-1", "i-budget-1"),
            ("v-budget-2", "s-budget-2", "i-budget-2"),
        ],
    )
    conn.executemany(
        """INSERT INTO jobs(
               id,kind,status,shot_id,version_id,episode_id,reserved_cost_cny,
               created_at,updated_at
           ) VALUES(?, 'video', 'paused_budget', ?, ?, 'e1', 1, 1, 1)""",
        [
            ("j-budget-1", "s-budget-1", "v-budget-1"),
            ("j-budget-2", "s-budget-2", "v-budget-2"),
        ],
    )
    conn.commit()
    _authorize_video_retry(conn, "e1")
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(system_api, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_setting", lambda *_args: "100")
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

    result = system_api.retry_job("j-budget-1")

    assert result["retryability"]["action"] == "new_submission"
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
    _seed_retry_episode(conn)
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) "
        "VALUES('s1','e1',1,5)"
    )
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
    _authorize_video_retry(conn, "e1")
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

    result = system_api.retry_job("j-recover")

    assert result["retryability"]["action"] == "continue_poll"
    assert result["job"]["status"] == "waiting_provider"
    assert conn.execute(
        "SELECT provider_task_id FROM shot_versions WHERE id='v-recover'",
    ).fetchone()["provider_task_id"] == "provider-task-recovered"


def test_manual_retry_recovers_abandoned_provider_task_as_isolated_poll(
    monkeypatch,
) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    _seed_retry_episode(conn)
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) "
        "VALUES('s-abandoned','e1',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v-abandoned','s-abandoned',1,'p','i-abandoned','abandoned',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,status,shot_id,version_id,episode_id,project_id,
               provider_operation_id,provider_create_state,
               provider_non_cancellable,cancellation_requested,abandoned,
               provider_poll_required,provider_result_adoptable,video_slot_active,
               created_at,updated_at
           ) VALUES(
               'j-abandoned','video','abandoned','s-abandoned','v-abandoned',
               'e1','p1','video-create-v-abandoned','submitting',
               1,1,1,1,1,0,1,1
           )"""
    )
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,status,operation_id,response_json
           ) VALUES(100,'video_create','OK','video-create-v-abandoned',?)""",
        (json.dumps({"id": "provider-task-abandoned"}),),
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

    result = system_api.retry_job(
        "j-abandoned",
        {"isolate_provider_result": True},
    )

    assert result["retryability"]["action"] == "continue_poll"
    assert result["retryability"]["result_isolated"] is True
    job = conn.execute(
        """SELECT status,cancellation_requested,abandoned,video_slot_active,
                  provider_poll_required,provider_result_adoptable
             FROM jobs WHERE id='j-abandoned'"""
    ).fetchone()
    assert dict(job) == {
        "status": "waiting_provider",
        "cancellation_requested": 0,
        "abandoned": 0,
        "video_slot_active": 0,
        "provider_poll_required": 1,
        "provider_result_adoptable": 0,
    }
    version = conn.execute(
        """SELECT provider_task_id,status,video_slot_active
             FROM shot_versions WHERE id='v-abandoned'"""
    ).fetchone()
    assert dict(version) == {
        "provider_task_id": "provider-task-abandoned",
        "status": "waiting_provider",
        "video_slot_active": 0,
    }


def test_manual_retry_requires_confirmation_for_unresolved_provider_create(monkeypatch) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    _seed_retry_episode(conn)
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) "
        "VALUES('s1','e1',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status, created_at
           ) VALUES('v-unknown','s1',1,'p','i1','failed',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,status,shot_id,version_id,episode_id,project_id,error,
               provider_operation_id,provider_create_state,
               provider_non_cancellable,reserved_cost_cny,
               provider_submitted_at,reason_code,reason_text,created_at,updated_at
           ) VALUES(
               'j-unknown','video','waiting_human','s1','v-unknown','e1','p1',
               '旧人工阻塞','video-create-v-unknown','unknown',1,1,5,
               'VIDEO_PROVIDER_CREATE_UNRESOLVED','旧人工阻塞',1,1
           )"""
    )
    conn.commit()
    _authorize_video_retry(conn, "e1")
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(system_api, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

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
    reset = conn.execute(
        """SELECT error,provider_operation_id,provider_create_state,
                  provider_non_cancellable,provider_submitted_at,
                  reason_code,reason_text
             FROM jobs WHERE id='j-unknown'"""
    ).fetchone()
    assert reset["error"] is None
    assert reset["provider_operation_id"].startswith("video-create-v-unknown-epoch_")
    assert reset["provider_operation_id"] != "video-create-v-unknown"
    assert reset["provider_create_state"] == "not_started"
    assert reset["provider_non_cancellable"] == 0
    assert reset["provider_submitted_at"] is None
    assert reset["reason_code"] is None
    assert reset["reason_text"] is None
    worker._assert_provider_create_resolved(reset, None)


def test_manual_retry_cas_failure_rolls_back_new_epoch_budget(monkeypatch) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    _seed_retry_episode(conn)
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s)
           VALUES('s1','e1',1,5)"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v-race','s1',1,'p','i-race','failed',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,status,shot_id,version_id,episode_id,project_id,
               provider_operation_id,
               provider_create_state,provider_non_cancellable,reserved_cost_cny,
               reason_code,reason_text,created_at,updated_at
           ) VALUES(
               'j-race','video','waiting_human','s1','v-race','e1','p1',
               'video-create-v-race','unknown',1,1,
               'VIDEO_PROVIDER_CREATE_UNRESOLVED','旧人工阻塞',1,1
           )"""
    )
    conn.commit()
    _authorize_video_retry(conn, "e1")
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(system_api, "get_setting", lambda *_args: "100")
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)
    reserve_budget = worker.media_scheduler.reserve_budget

    def reserve_then_change_job(*args, **kwargs):
        assert reserve_budget(*args, **kwargs)
        conn.execute(
            """UPDATE jobs
                  SET state_revision=state_revision+1
                WHERE id='j-race'"""
        )
        return True

    monkeypatch.setattr(
        worker.media_scheduler,
        "reserve_budget",
        reserve_then_change_job,
    )

    with pytest.raises(HTTPException) as conflict:
        system_api.retry_job("j-race", {"allow_new_submission": True})

    assert conflict.value.status_code == 409
    assert conn.execute(
        "SELECT 1 FROM budget_reservations WHERE job_id='j-race'"
    ).fetchone() is None
    job = conn.execute(
        """SELECT status,reserved_cost_cny,provider_operation_id,
                  provider_create_state,reason_code
             FROM jobs WHERE id='j-race'"""
    ).fetchone()
    assert dict(job) == {
        "status": "waiting_human",
        "reserved_cost_cny": 1.0,
        "provider_operation_id": "video-create-v-race",
        "provider_create_state": "unknown",
        "reason_code": "VIDEO_PROVIDER_CREATE_UNRESOLVED",
    }


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


def test_manual_retry_requires_confirmation_for_technical_provider_failure(
    monkeypatch,
) -> None:
    import app.monitoring as monitoring
    import app.system_api as system_api

    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e1','p1',1,'generating',1)"""
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s)
           VALUES('s1','e1',1,5)"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,created_at
           ) VALUES(
               'v1','s1',1,'p','i1','waiting_human','provider-task-1',1
           )"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_operation_id,provider_create_state,
               provider_non_cancellable,provider_failure_category,
               provider_failure_kind,provider_failure_disposition,
               provider_failure_retryable,reason_code,created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','waiting_human',
               'video-create-v1','accepted',1,'technical',
               'provider_task_not_found','manual_review',0,
               'VIDEO_PROVIDER_TASK_NOT_FOUND',1,1
           )"""
    )
    conn.commit()
    _authorize_video_retry(conn, "e1")
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

    with pytest.raises(HTTPException) as confirmation:
        system_api.retry_job("j1")

    assert confirmation.value.status_code == 409
    assert confirmation.value.detail["code"] == "PROVIDER_TECHNICAL_FAILURE_CONFIRMATION_REQUIRED"
    assert confirmation.value.detail["retryability"]["action"] == "confirm_new_submission"

    result = system_api.retry_job("j1", {"allow_new_submission": True})

    assert result["job"]["status"] == "queued"
    assert result["retryability"]["action"] == "new_submission_after_technical_failure"
    reset = conn.execute(
        """SELECT provider_create_state,provider_failure_category,
                  provider_failure_kind,provider_failure_disposition,
                  provider_failure_retryable,reason_code
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert dict(reset) == {
        "provider_create_state": "not_started",
        "provider_failure_category": None,
        "provider_failure_kind": None,
        "provider_failure_disposition": None,
        "provider_failure_retryable": None,
        "reason_code": None,
    }
    assert conn.execute(
        "SELECT provider_task_id FROM shot_versions WHERE id='v1'"
    ).fetchone()["provider_task_id"] is None


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
    _authorize_video_retry(conn, "e1")
    for module in (system_api, monitoring):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.media_scheduler, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "episode_video_budget_limit", lambda _episode_id: 100)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)

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
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    _authorize_video_retry(conn, "e1")
    for module in (
        monitoring, system_api, orchestration_api, media_scheduler, repository,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    # ``worker`` can't go in the loop above: app.media_exec is a real package
    # now, and common.py/enqueue.py/legacy_keyframes.py/run_job.py/concat.py
    # each independently do ``from app.db import get_conn`` -- patching only
    # app.worker's re-export attribute leaves every one of those five copies
    # pointed at the real on-disk connection (see patch_worker_everywhere's
    # docstring in tests/conftest.py; this is the exact loop-variable-form
    # blind spot the guard test can't catch statically).
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)
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


def _seed_submission_ready_video_job(
    conn: sqlite3.Connection,
    *,
    job_id: str = "j-submit",
    version_id: str = "v-submit",
    shot_id: str = "s-submit",
    episode_id: str = "e-submit",
) -> None:
    """一个还没拿到 provider_task_id、正要走到 claim_video_submit_slot 的
    视频 job——复现 EP2 那种"认领没通过"落到 _run_job 的
    ``VideoBudgetAuthorizationError`` 分支的真实入口，而不是从半路的
    'accepted' 状态直接切入轮询。"""
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p-submit','P',1)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES(?,'p-submit',1,'generating',1)""",
        (episode_id,),
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition
           ) VALUES(?,?,1,5,'中景','固定','室内','[]','人物站定','[]','硬切')""",
        (shot_id, episode_id),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,image_inputs,created_at
           ) VALUES(?,?,1,'prompt','idem-submit','running',NULL,?,1)""",
        (version_id, shot_id, json.dumps({"mode": "REFERENCE_VIDEO_MODE"})),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               lease_owner,lease_expires_at,provider_create_state,
               provider_non_cancellable,created_at,updated_at
           ) VALUES(?,'video',?,?,?,'p-submit','running','worker-1',
               9999999999,'not_started',0,1,1)""",
        (job_id, shot_id, version_id, episode_id),
    )
    conn.commit()


def _patch_run_job_submission_scaffolding(monkeypatch) -> None:
    """把 claim_video_submit_slot 之前那些跟本次测试无关的前置门禁/装配全
    部短路成 no-op——聚焦在 VideoBudgetAuthorizationError 分支本身的行为，
    不重新验证参考图装配、审查依赖闸门这些别处已经覆盖的逻辑。"""
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

    async def passthrough_planned_inputs(
        _conn, _job, _version, _shot, _ep, meta, prompt_text, *, lease_owner,
    ):
        return meta, prompt_text

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    patch_worker_everywhere(monkeypatch, "_assert_review_dependency_fence_async", no_fence)
    patch_worker_everywhere(monkeypatch, "_assert_video_provider_submission_authority_async", no_fence,
    )
    # 不能只走"未知视频生成模式"报错的最短路径去够 claim_video_submit_
    # slot——那条路径本身要靠 _prepare_planned_mode_inputs 按 mode 分支到
    # 一个真正的参考图/首尾帧装配函数，装配细节别处已经覆盖，这里整个短路。
    patch_worker_everywhere(monkeypatch, "_prepare_planned_mode_inputs", passthrough_planned_inputs)
    patch_worker_everywhere(monkeypatch, "_assert_job_lease", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "_video_image_inputs_from_meta", lambda _meta: [])
    monkeypatch.setattr(worker.video_modes, "build_seedance_video_inputs", lambda _meta: [])
    # AI 提示词/装配之间那段用心跳影子任务保活 lease；心跳循环真的调用
    # media_scheduler.renew_lease，不给它一个总是成功的桩，心跳会判定 lease
    # 丢失，提前把整条路径拐去 LeaseLost，claim_video_submit_slot 根本不会
    # 被调用到。
    monkeypatch.setattr(worker.media_scheduler, "renew_lease", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(concurrency, "semaphore_for", lambda _resource: Permit())
    monkeypatch.setattr(concurrency, "report_congestion", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(concurrency, "report_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stage_state, "set_pipeline_stage", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "mark_media_job_state", lambda *_args, **_kwargs: None)
    patch_worker_everywhere(monkeypatch, "reconcile_episode_generation_status", lambda *_args, **_kwargs: None,
    )


def test_budget_pause_auto_retries_within_already_approved_cap_before_requiring_human(
    monkeypatch,
) -> None:
    """核心缺陷二修复的直接回归：claim_video_submit_slot 认领没通过时，
    _run_job 以前会立刻把 job 钉死在 paused_budget，必须人工再点一次
    /generate 才能续上。现在它应该在人已经批准过的同一个 cap 内，先按
    VIDEO_JOB_MAX_RETRIES 有限退避重试几次——重试期间既不新申请预算，也
    不改变认领函数本身；只有重试耗尽仍未通过，才落回需要人工处理的
    paused_budget 终态。"""
    from app import config

    conn = _conn()
    _seed_submission_ready_video_job(conn)
    _authorize_video_retry(conn, "e-submit", cap_cny=100.0)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    _patch_run_job_submission_scaffolding(monkeypatch)

    requeued: list[str] = []
    patch_worker_everywhere(monkeypatch, "_requeue_after",
        lambda job_id, _delay: requeued.append(job_id) or _noop_coro(),
    )

    from app.media_pipeline import scheduler as media_scheduler_module

    monkeypatch.setattr(
        media_scheduler_module, "claim_video_submit_slot",
        lambda **_kwargs: (False, "VIDEO_BUDGET_NOT_AUTHORIZED"),
    )

    asyncio.run(worker._run_job("j-submit", lease_owner="worker-1"))

    job = conn.execute(
        "SELECT status,reason_code FROM jobs WHERE id='j-submit'"
    ).fetchone()
    assert job["status"] == "queued"
    assert job["reason_code"] == "VIDEO_BUDGET_NOT_AUTHORIZED"
    assert requeued == ["j-submit"]
    meta = json.loads(
        conn.execute(
            "SELECT image_inputs FROM shot_versions WHERE id='v-submit'"
        ).fetchone()["image_inputs"]
    )
    assert meta["budget_pause_auto_retry_count"] == 1
    # shot_versions 状态没有被拖去 paused_budget——这仍是活跃的重试，不是终态。
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v-submit'"
    ).fetchone()["status"] == "running"

    # 把 job 重新置回 running（下一轮 worker 会做的事），驱动到重试耗尽为止。
    for _ in range(config.VIDEO_JOB_MAX_RETRIES - 1):
        conn.execute(
            "UPDATE jobs SET status='running',lease_owner='worker-1' WHERE id='j-submit'"
        )
        conn.commit()
        asyncio.run(worker._run_job("j-submit", lease_owner="worker-1"))

    job = conn.execute("SELECT status FROM jobs WHERE id='j-submit'").fetchone()
    assert job["status"] == "queued"
    assert len(requeued) == config.VIDEO_JOB_MAX_RETRIES

    # 最后一次：重试预算耗尽，落回需要人工处理的 paused_budget 终态。
    conn.execute(
        "UPDATE jobs SET status='running',lease_owner='worker-1' WHERE id='j-submit'"
    )
    conn.commit()
    asyncio.run(worker._run_job("j-submit", lease_owner="worker-1"))

    job = conn.execute(
        "SELECT status,reason_code FROM jobs WHERE id='j-submit'"
    ).fetchone()
    assert job["status"] == "paused_budget"
    assert job["reason_code"] == "VIDEO_BUDGET_NOT_AUTHORIZED"
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v-submit'"
    ).fetchone()["status"] == "paused_budget"
    # 重试没有申请过新预算——authority 的 cap 全程没变。
    assert conn.execute(
        "SELECT cap_cny FROM episode_video_budget_authorities WHERE episode_id='e-submit'"
    ).fetchone()["cap_cny"] == 100.0


async def _noop_coro() -> None:
    return None
