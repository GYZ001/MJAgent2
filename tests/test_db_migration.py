import sqlite3
import threading

from app import db


def test_init_db_migrates_legacy_jobs_before_creating_claim_index(tmp_path, monkeypatch) -> None:
    database = tmp_path / "legacy.db"
    conn = sqlite3.connect(database)
    conn.execute(
        """CREATE TABLE jobs (
               id TEXT PRIMARY KEY, kind TEXT NOT NULL, shot_id TEXT, version_id TEXT,
               episode_id TEXT, project_id TEXT, status TEXT DEFAULT 'queued', error TEXT,
               created_at REAL NOT NULL, updated_at REAL NOT NULL
           )"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())

    db.init_db()

    migrated = db.get_conn()
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(jobs)").fetchall()}
    indexes = {row[1] for row in migrated.execute("PRAGMA index_list(jobs)").fetchall()}
    assert {"next_retry_at", "lease_expires_at", "retry_count"}.issubset(columns)
    assert "idx_jobs_claim" in indexes
    assert "idx_jobs_run" in indexes
    migrated.close()


def test_init_db_interrupts_text_run_waiting_for_retry(tmp_path, monkeypatch) -> None:
    database = tmp_path / "waiting-retry.db"
    conn = sqlite3.connect(database)
    conn.executescript(db.SCHEMA)
    conn.execute(
        """INSERT INTO workflow_runs(
               id, workflow_type, scope_type, scope_id, status,
               input_fingerprint, current_step_key, started_at, updated_at
           ) VALUES('run_wait', 'storyboard', 'episode', 'e1', 'WAITING_RETRY',
                    'fp', 'storyboard', 1, 1)"""
    )
    conn.execute(
        """INSERT INTO step_runs(id, run_id, step_key, status, started_at)
           VALUES('step_wait', 'run_wait', 'storyboard', 'RUNNING', 1)"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())

    db.init_db()

    recovered = db.get_conn()
    run = recovered.execute(
        "SELECT status, failure_code, failure_message FROM workflow_runs WHERE id='run_wait'"
    ).fetchone()
    step = recovered.execute(
        "SELECT status, error_code FROM step_runs WHERE id='step_wait'"
    ).fetchone()
    assert run["status"] == "PAUSED_EXTERNAL"
    assert run["failure_code"] == "SERVICE_RESTART"
    assert "安全检查点" in run["failure_message"]
    assert step["status"] == "FAILED"
    assert step["error_code"] == "SERVICE_RESTART"
    recovered.close()
