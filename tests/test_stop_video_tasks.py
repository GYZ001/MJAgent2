import sqlite3
import json

from app import db, worker
from app.media_pipeline.status import episode_pipeline_statuses
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
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e','p',1,'generating',0)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e',1,5)"
    )
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v1','s1',1,'p','k1','queued',0)"
    )
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v2','s1',2,'p','k2','running',1)"
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,created_at,updated_at
           ) VALUES('j1','video','s1','v1','e','p','queued',0,0,0)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,created_at,updated_at
           ) VALUES('j2','video','s1','v2','e','p','running',1,1,1)"""
    )
    conn.commit()
    return conn


def test_stop_shot_cancels_every_active_video_and_reconciles_episode(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)

    result = worker.stop_shot_video_tasks("s1")

    assert result["stopped_count"] == 2
    assert result["provider_may_continue"] is True
    assert result["resume_supported"] is False
    jobs = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id,status,cancellation_requested,abandoned FROM jobs"
        ).fetchall()
    }
    assert jobs["j1"] == {
        "id": "j1",
        "status": "cancelled",
        "cancellation_requested": 1,
        "abandoned": 0,
    }
    assert jobs["j2"] == {
        "id": "j2",
        "status": "abandoned",
        "cancellation_requested": 1,
        "abandoned": 1,
    }
    versions = {
        row["id"]: (row["status"], row["error"])
        for row in conn.execute(
            "SELECT id,status,error FROM shot_versions ORDER BY id"
        ).fetchall()
    }
    assert versions == {
        "v1": ("cancelled", "用户已停止视频任务"),
        "v2": ("abandoned", "用户已停止视频任务"),
    }
    assert conn.execute(
        "SELECT status FROM episodes WHERE id='e'"
    ).fetchone()["status"] == "confirmed"


def test_stop_shot_is_idempotent_and_does_not_overwrite_finished_job(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)

    first = worker.stop_shot_video_tasks("s1")
    second = worker.stop_shot_video_tasks("s1")
    assert first["stopped_count"] == 2
    assert second["stopped_count"] == 0

    conn.execute(
        "UPDATE jobs SET status='succeeded', cancellation_requested=0 WHERE id='j1'"
    )
    conn.commit()
    result = media_scheduler.request_cancel("j1")
    assert result["cancelled"] is False
    assert result["status"] == "succeeded"
    assert conn.execute(
        "SELECT status,cancellation_requested FROM jobs WHERE id='j1'"
    ).fetchone()[:] == ("succeeded", 0)


def test_episode_pause_and_resume_is_reversible_and_cyclic(monkeypatch) -> None:
    conn = _conn()
    conn.execute("UPDATE shot_versions SET provider_task_id='provider-2' WHERE id='v2'")
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *args, **kwargs: None)

    first_pause = worker.pause_episode_video_tasks("e")
    assert first_pause["paused_jobs"] == 2
    assert first_pause["resume_supported"] is True
    assert first_pause["provider_may_continue"] is True
    assert [row["status"] for row in conn.execute(
        "SELECT status FROM jobs ORDER BY id"
    )] == ["paused", "paused"]
    assert [row["status"] for row in conn.execute(
        "SELECT status FROM shot_versions ORDER BY id"
    )] == ["paused", "paused"]
    assert conn.execute("SELECT status FROM episodes WHERE id='e'").fetchone()[0] == "confirmed"

    first_resume = worker.resume_episode_video_tasks("e")
    assert first_resume["resumed_jobs"] == 2
    assert [row["status"] for row in conn.execute(
        "SELECT status FROM jobs ORDER BY id"
    )] == ["queued", "waiting_provider"]
    assert [row["status"] for row in conn.execute(
        "SELECT status FROM shot_versions ORDER BY id"
    )] == ["queued", "queued"]
    assert conn.execute("SELECT status FROM episodes WHERE id='e'").fetchone()[0] == "generating"

    second_pause = worker.pause_episode_video_tasks("e")
    second_resume = worker.resume_episode_video_tasks("e")
    assert second_pause["paused_jobs"] == 2
    assert second_resume["resumed_jobs"] == 2


def test_episode_pause_does_not_touch_completed_jobs(monkeypatch) -> None:
    conn = _conn()
    conn.execute("UPDATE jobs SET status='succeeded' WHERE id='j1'")
    conn.execute("UPDATE shot_versions SET status='succeeded' WHERE id='v1'")
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *args, **kwargs: None)

    result = worker.pause_episode_video_tasks("e")

    assert result["paused_jobs"] == 1
    assert conn.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()[0] == "succeeded"
    assert conn.execute("SELECT status FROM shot_versions WHERE id='v1'").fetchone()[0] == "succeeded"


def test_paused_episode_status_is_not_reported_as_generating(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *args, **kwargs: None)
    worker.pause_episode_video_tasks("e")

    statuses, summary = episode_pipeline_statuses("e", conn=conn)

    assert summary["paused"] == 1
    assert statuses["s1"]["pipeline_status"] == "paused"
    assert statuses["s1"]["video_status"] == "pending_generation"


def test_resume_recovers_existing_provider_handle_without_resubmitting(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """UPDATE jobs
              SET provider_operation_id='video-create-v2',
                  provider_create_state='accepted', provider_non_cancellable=1
            WHERE id='j2'"""
    )
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,status,operation_id,response_json
           ) VALUES(10,'video_create','OK','video-create-v2',?)""",
        (json.dumps({"id": "provider-existing-2"}),),
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)

    worker.pause_episode_video_tasks("e")
    result = worker.resume_episode_video_tasks("e")

    assert result.get("requires_provider_confirmation", False) is False
    assert result["resumed_jobs"] == 2
    job = conn.execute(
        "SELECT status,provider_create_state FROM jobs WHERE id='j2'"
    ).fetchone()
    assert dict(job) == {
        "status": "waiting_provider",
        "provider_create_state": "accepted",
    }
    assert conn.execute(
        "SELECT provider_task_id FROM shot_versions WHERE id='v2'"
    ).fetchone()["provider_task_id"] == "provider-existing-2"


def test_resume_refuses_duplicate_charge_when_provider_handle_is_unknown(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """UPDATE jobs
              SET provider_operation_id='video-create-v2',
                  provider_create_state='unknown', provider_non_cancellable=1
            WHERE id='j2'"""
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *args, **kwargs: None)

    worker.pause_episode_video_tasks("e")
    result = worker.resume_episode_video_tasks("e")

    assert result["requires_provider_confirmation"] is True
    assert result["resumed_jobs"] == 0
    assert result["unresolved_provider_jobs"] == [{
        "job_id": "j2",
        "provider_operation_id": "video-create-v2",
        "reason": "供应商可能已接单，但本地尚未确认原任务号",
    }]
    assert [row["status"] for row in conn.execute(
        "SELECT status FROM jobs ORDER BY id"
    )] == ["paused", "paused"]


def test_fresh_supervisor_takes_over_exact_paused_jobs(monkeypatch) -> None:
    conn = _conn()
    snapshot = json.dumps({
        "review_dependency_snapshot": {"qualification_version": "release-q1"},
    })
    conn.execute(
        """UPDATE episodes
              SET video_completion_mode='complete', active_video_run_id='run-new'
            WHERE id='e'"""
    )
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id IN ('v1','v2')",
        (snapshot,),
    )
    for job_id in ("j1", "j2"):
        conn.execute(
            """INSERT INTO budget_reservations(
                   id,job_id,scope_type,scope_id,amount_cny,status,created_at
               ) VALUES(?,?, 'episode','e',4,'reserved',0)""",
            (f"budget-{job_id}", job_id),
        )
    conn.execute(
        """UPDATE jobs
              SET provider_operation_id='video-create-v2',
                  provider_create_state='accepted', provider_non_cancellable=1
            WHERE id='j2'"""
    )
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,status,operation_id,response_json
           ) VALUES(10,'video_create','OK','video-create-v2',?)""",
        (json.dumps({"id": "provider-existing-2"}),),
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    queued: list[str] = []
    monkeypatch.setattr(
        worker, "_enqueue_for_current_status", lambda job_id: queued.append(job_id),
    )
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *args, **kwargs: None)

    worker.pause_episode_video_tasks("e")
    resumed_local = worker._resume_reused_paused_job(
        "v1",
        supervisor_run_id="run-new",
        dependency_snapshot={"qualification_version": "release-q1"},
    )
    resumed_provider = worker._resume_reused_paused_job(
        "v2",
        supervisor_run_id="run-new",
        dependency_snapshot={"qualification_version": "release-q1"},
    )

    assert resumed_local == {
        "resumed": True,
        "job_id": "j1",
        "provider_task_id": None,
        "provider_already_accepted": False,
    }
    assert resumed_provider == {
        "resumed": True,
        "job_id": "j2",
        "provider_task_id": "provider-existing-2",
        "provider_already_accepted": True,
    }
    assert [tuple(row) for row in conn.execute(
        "SELECT id,status,owner_run_id FROM jobs ORDER BY id"
    )] == [
        ("j1", "queued", "run-new"),
        ("j2", "waiting_provider", "run-new"),
    ]
    assert [tuple(row) for row in conn.execute(
        "SELECT id,status,provider_task_id FROM shot_versions ORDER BY id"
    )] == [
        ("v1", "queued", None),
        ("v2", "queued", "provider-existing-2"),
    ]
    assert queued == ["j1", "j2"]


def test_fresh_supervisor_refuses_paused_job_with_changed_release(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """UPDATE episodes
              SET video_completion_mode='complete', active_video_run_id='run-new'
            WHERE id='e'"""
    )
    conn.execute(
        """UPDATE shot_versions
              SET image_inputs='{"review_dependency_snapshot":{"qualification_version":"release-old"}}'
            WHERE id='v1'"""
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES('budget-j1','j1','episode','e',4,'reserved',0)"""
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *args, **kwargs: None)
    worker.pause_episode_video_tasks("e")

    try:
        worker._resume_reused_paused_job(
            "v1",
            supervisor_run_id="run-new",
            dependency_snapshot={"qualification_version": "release-new"},
        )
    except ValueError as exc:
        assert "[REVIEW_DEPENDENCY_STALE]" in str(exc)
    else:
        raise AssertionError("changed release must fail closed")

    assert conn.execute(
        "SELECT status,owner_run_id FROM jobs WHERE id='j1'"
    ).fetchone()[:] == ("paused", None)
