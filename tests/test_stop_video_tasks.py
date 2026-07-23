import sqlite3

from app import db, worker
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
