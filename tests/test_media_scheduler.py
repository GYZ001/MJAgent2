import sqlite3

import pytest

from app import completion_grant, db, worker
from app.orchestration import media_scheduler


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,created_at) VALUES('e','p',1,0)")
    conn.execute("INSERT INTO jobs(id,kind,episode_id,project_id,status,created_at,updated_at) VALUES('j1','video','e','p','queued',0,0)")
    conn.execute("INSERT INTO jobs(id,kind,episode_id,project_id,status,created_at,updated_at) VALUES('j2','video','e','p','queued',0,0)")
    conn.commit()
    return conn


def test_budget_reservation_is_atomic_and_does_not_overrun(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    assert media_scheduler.reserve_budget("j1", "e", 6, 10, conn=conn)
    assert not media_scheduler.reserve_budget("j2", "e", 5, 10, conn=conn)
    assert conn.execute("SELECT status FROM jobs WHERE id='j2'").fetchone()["status"] == "paused_budget"
    assert conn.execute("SELECT SUM(amount_cny) FROM budget_reservations WHERE status='reserved'").fetchone()[0] == 6


def test_lease_claim_is_cas_and_expired_lease_can_be_reclaimed(monkeypatch) -> None:
    conn = _conn()
    clock = {"now": 100.0}
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "now", lambda: clock["now"])
    first = media_scheduler.claim_job("j1", "worker-a", lease_seconds=10)
    assert first and not first.recovered
    assert media_scheduler.claim_job("j1", "worker-b", lease_seconds=10) is None
    clock["now"] = 111.0
    recovered = media_scheduler.claim_job("j1", "worker-b", lease_seconds=10)
    assert recovered and recovered.recovered


def test_stale_worker_cannot_accept_provider_budget_after_lease_takeover(
    monkeypatch,
) -> None:
    conn = _conn()
    clock = {"now": 100.0}
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "now", lambda: clock["now"])
    monkeypatch.setattr(completion_grant, "now", lambda: clock["now"])
    completion_grant.ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO episode_video_budget_authorities(
               episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
           ) VALUES('e',0,10,'test',100,100)"""
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v1','s1',1,'p','idem','running',100)"""
    )
    conn.commit()

    assert media_scheduler.claim_job("j1", "worker-a", lease_seconds=5)
    assert completion_grant.reserve_provider_video_budget(
        episode_id="e",
        job_id="j1",
        version_id="v1",
        operation_id="op1",
        amount_cny=1,
        conn=conn,
    )

    clock["now"] = 106.0
    assert media_scheduler.claim_job("j1", "worker-b", lease_seconds=5)

    accepted = completion_grant.mark_provider_video_budget_claim(
        "op1",
        "accepted",
        job_id="j1",
        lease_owner="worker-a",
        conn=conn,
    )
    conn.commit()

    assert accepted is False
    assert conn.execute(
        """SELECT status FROM provider_video_budget_claims
           WHERE operation_id='op1'"""
    ).fetchone()["status"] == "reserved"
    with pytest.raises(worker.LeaseLost):
        worker._commit_provider_acceptance(
            conn,
            job_id="j1",
            version_id="v1",
            owner="worker-a",
            operation_id="op1",
            task_id="provider-task-stale",
        )
    with pytest.raises(worker.LeaseLost):
        worker._commit_video_result_checkpoint(
            conn,
            job_id="j1",
            version_id="v1",
            owner="worker-a",
            operation_id="op1",
            video_path="/tmp/stale.mp4",
            last_frame_url=None,
            cost_cny=1,
            latency_s=1,
            image_inputs="{}",
        )

    job = conn.execute(
        """SELECT lease_owner,provider_create_state
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    version = conn.execute(
        """SELECT status,provider_task_id,cost_cny
             FROM shot_versions WHERE id='v1'"""
    ).fetchone()
    assert dict(job) == {
        "lease_owner": "worker-b",
        "provider_create_state": "not_started",
    }
    assert dict(version) == {
        "status": "running",
        "provider_task_id": None,
        "cost_cny": 0.0,
    }
    assert conn.execute(
        """SELECT status FROM provider_video_budget_claims
           WHERE operation_id='op1'"""
    ).fetchone()["status"] == "reserved"


def test_current_worker_can_recover_legacy_provider_handle_without_budget_ledger(
    monkeypatch,
) -> None:
    conn = _conn()
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v1','s1',1,'p','idem','running',100)"""
    )
    conn.commit()
    assert media_scheduler.claim_job("j1", "worker-a", lease_seconds=30)

    worker._commit_provider_acceptance(
        conn,
        job_id="j1",
        version_id="v1",
        owner="worker-a",
        operation_id="legacy-op",
        task_id="legacy-task",
        submitted_at=90,
    )

    job = conn.execute(
        """SELECT provider_create_state,provider_submitted_at
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert dict(job) == {
        "provider_create_state": "accepted",
        "provider_submitted_at": 90.0,
    }
    assert conn.execute(
        "SELECT provider_task_id FROM shot_versions WHERE id='v1'"
    ).fetchone()["provider_task_id"] == "legacy-task"
    assert conn.execute(
        """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='provider_video_budget_claims'"""
    ).fetchone()


def test_recovery_keeps_future_retry_and_cancel_marks_provider_work_abandoned(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "now", lambda: 100.0)
    conn.execute("UPDATE jobs SET next_retry_at=105 WHERE id='j1'")
    conn.execute("UPDATE jobs SET status='running', provider_non_cancellable=1, run_id=NULL, step_run_id=NULL WHERE id='j2'")
    conn.commit()
    jobs = dict(media_scheduler.recoverable_jobs())
    assert jobs["j1"] == 5.0
    result = media_scheduler.request_cancel("j2")
    assert result["status"] == "abandoned" and result["provider_may_continue"] is True
    row = conn.execute("SELECT abandoned,cancellation_requested FROM jobs WHERE id='j2'").fetchone()
    assert tuple(row) == (1, 1)


def test_cancelled_job_cannot_be_written_back_to_running_version(monkeypatch) -> None:
    from app import worker

    conn = _conn()
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "now", lambda: 100.0)
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s','e',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v','s',1,'p','idem','running',1)"""
    )
    conn.execute(
        """UPDATE jobs
              SET shot_id='s',version_id='v',status='running',lease_owner='worker'
            WHERE id='j1'"""
    )
    conn.commit()

    result = media_scheduler.request_cancel("j1", reason="Supervisor 收口")

    assert result["status"] == "cancelled"
    assert worker._set_version("v", status="running", error=None) is False
    version = conn.execute(
        "SELECT status,error FROM shot_versions WHERE id='v'"
    ).fetchone()
    assert dict(version) == {"status": "cancelled", "error": "Supervisor 收口"}

    conn.execute("UPDATE shot_versions SET status='running' WHERE id='v'")
    conn.commit()
    assert media_scheduler.reconcile_cancelled_version_states(episode_id="e") == 1
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v'"
    ).fetchone()["status"] == "cancelled"


def test_heartbeat_operation_owns_file_database_connection(tmp_path) -> None:
    from app import worker

    file_conn = sqlite3.connect(tmp_path / "worker.db")
    memory_conn = sqlite3.connect(":memory:")
    try:
        assert worker._connection_for_heartbeat_operation(file_conn) is None
        assert worker._connection_for_heartbeat_operation(memory_conn) is memory_conn
    finally:
        file_conn.close()
        memory_conn.close()
