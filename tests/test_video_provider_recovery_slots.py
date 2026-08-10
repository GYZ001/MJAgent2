from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app import db, worker
from app.completion_grant import ensure_video_budget_authority_tables
from app.orchestration import media_scheduler


def _create_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    ensure_video_budget_authority_tables(conn)
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) "
        "VALUES('project','Project','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('episode','project',1,'generating',1)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,adopted_version_id
           ) VALUES('shot','episode',1,5,NULL)"""
    )
    conn.executemany(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        [
            (
                "version-owner",
                "shot",
                1,
                "owner prompt",
                "owner-idem",
                "running",
                "provider-task-owner",
                1,
            ),
            (
                "version-history",
                "shot",
                2,
                "history prompt",
                "history-idem",
                "running",
                "provider-task-history",
                2,
            ),
        ],
    )
    conn.executemany(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_operation_id,provider_create_state,
               provider_non_cancellable,provider_submitted_at,
               created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "job-owner",
                "video",
                "shot",
                "version-owner",
                "episode",
                "project",
                "waiting_provider",
                "video-create-owner",
                "accepted",
                1,
                10,
                1,
                10,
            ),
            (
                "job-history",
                "video",
                "shot",
                "version-history",
                "episode",
                "project",
                "waiting_provider",
                "video-create-history",
                "accepted",
                1,
                11,
                2,
                11,
            ),
        ],
    )
    conn.executemany(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES(?,?,'episode','episode',1.5,'reserved',1)""",
        [
            ("budget-owner", "job-owner"),
            ("budget-history", "job-history"),
        ],
    )
    conn.executemany(
        """INSERT INTO provider_video_budget_claims(
               operation_id,project_id,episode_id,shot_id,job_id,version_id,
               origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
               amount_cny,status,created_at,updated_at,accepted_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,1.5,'accepted',1,1,1)""",
        [
            (
                "video-create-owner",
                "project",
                "episode",
                "shot",
                "job-owner",
                "version-owner",
                "episode",
                "shot",
                "job-owner",
                "version-owner",
            ),
            (
                "video-create-history",
                "project",
                "episode",
                "shot",
                "job-history",
                "version-history",
                "episode",
                "shot",
                "job-history",
                "version-history",
            ),
        ],
    )
    conn.commit()
    return conn


def test_startup_migration_keeps_every_accepted_task_pollable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "accepted-duplicate-migration.db"
    conn = _create_database(database)
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())

    db.init_db(reconcile_interrupted=True)

    migrated = db.get_conn()
    jobs = migrated.execute(
        """SELECT id,status,video_slot_active,cancellation_requested,abandoned,
                  provider_poll_required,provider_result_adoptable
             FROM jobs ORDER BY id"""
    ).fetchall()
    assert len(jobs) == 2
    assert all(row["status"] == "waiting_provider" for row in jobs)
    assert all(row["cancellation_requested"] == 0 for row in jobs)
    assert all(row["abandoned"] == 0 for row in jobs)
    assert sum(row["video_slot_active"] for row in jobs) == 1
    assert sum(row["provider_poll_required"] for row in jobs) == 2
    assert all(
        row["provider_result_adoptable"] == row["video_slot_active"]
        for row in jobs
    )
    claims = migrated.execute(
        """SELECT operation_id,status FROM provider_video_budget_claims
           ORDER BY operation_id"""
    ).fetchall()
    assert [tuple(row) for row in claims] == [
        ("video-create-history", "accepted"),
        ("video-create-owner", "accepted"),
    ]
    reservations = migrated.execute(
        "SELECT job_id,status,actual_cost_cny FROM budget_reservations ORDER BY job_id"
    ).fetchall()
    assert [tuple(row) for row in reservations] == [
        ("job-history", "reserved", None),
        ("job-owner", "reserved", None),
    ]


def test_restart_polls_both_accepted_tasks_and_quarantines_late_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _create_database(tmp_path / "accepted-duplicate-restart.db")
    conn.execute(
        """UPDATE jobs
              SET video_slot_active=1,provider_poll_required=1,
                  provider_result_adoptable=1
            WHERE id='job-owner'"""
    )
    conn.execute(
        "UPDATE shot_versions SET video_slot_active=1 WHERE id='version-owner'"
    )
    conn.execute(
        "UPDATE shots SET adopted_version_id='version-owner' WHERE id='shot'"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)

    stopped = media_scheduler.request_cancel(
        "job-history",
        reason="重启前释放历史生成槽",
    )

    assert stopped["status"] == "waiting_provider"
    assert stopped["result_isolated"] is True
    history_job = conn.execute(
        """SELECT video_slot_active,provider_poll_required,
                  provider_result_adoptable,cancellation_requested,abandoned
             FROM jobs WHERE id='job-history'"""
    ).fetchone()
    assert dict(history_job) == {
        "video_slot_active": 0,
        "provider_poll_required": 1,
        "provider_result_adoptable": 0,
        "cancellation_requested": 0,
        "abandoned": 0,
    }

    dispatched: list[str] = []

    def capture_queue(queue, job_id: str) -> None:
        if queue is worker._poll_queue:
            dispatched.append(job_id)

    monkeypatch.setattr(worker, "_queue_job", capture_queue)
    monkeypatch.setattr(worker, "_poll_worker_target", 2)

    dispatch = worker._dispatch_due_jobs_legacy()

    assert dispatch["poll"] == 2
    assert dispatched == ["job-owner", "job-history"]

    conn.execute(
        """UPDATE jobs SET status='running',lease_owner='restart-worker',
                  lease_expires_at=9999999999
           WHERE id='job-history'"""
    )
    conn.commit()
    adoptable = worker._commit_video_result_checkpoint_in_transaction(
        conn,
        job_id="job-history",
        version_id="version-history",
        owner="restart-worker",
        operation_id="video-create-history",
        video_path=str(tmp_path / "late-history.mp4"),
        last_frame_url="https://provider.invalid/last-frame.jpg",
        cost_cny=1.5,
        latency_s=30,
        image_inputs='{"late_provider_result":true}',
    )

    history = conn.execute(
        """SELECT status,video_path,cost_cny FROM shot_versions
           WHERE id='version-history'"""
    ).fetchone()
    assert adoptable is False
    assert dict(history) == {
        "status": "quarantined",
        "video_path": str(tmp_path / "late-history.mp4"),
        "cost_cny": 1.5,
    }
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='shot'"
    ).fetchone()["adopted_version_id"] == "version-owner"
    claim = conn.execute(
        """SELECT status,settled_at FROM provider_video_budget_claims
           WHERE operation_id='video-create-history'"""
    ).fetchone()
    assert claim["status"] == "settled"
    assert claim["settled_at"] is not None
    reservation = conn.execute(
        """SELECT status,actual_cost_cny FROM budget_reservations
           WHERE job_id='job-history'"""
    ).fetchone()
    assert dict(reservation) == {
        "status": "settled",
        "actual_cost_cny": 1.5,
    }
