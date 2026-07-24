"""recover_media_jobs：服务重启后自动恢复中断的媒体任务。

背景：init_db() 在重启时把 RUNNING 的 workflow_runs 标为 PAUSED_EXTERNAL +
failure_code='SERVICE_RESTART'，但底层 jobs 表的 lease（默认 180s）在重启那一刻
往往还没过期，media_scheduler.recoverable_jobs() 只扫 status='running' AND
lease_expires_at<now 的 job，因此不会恢复——结果用户看到的"任务卡在
'服务重启，可从安全检查点恢复'"。recover_media_jobs() 把这些 job 显式复位回
queued，随后由数据库驱动的持久调度器在下一轮重新发现并交给 worker。
"""
import asyncio
import sqlite3

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
