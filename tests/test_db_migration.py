import sqlite3
import threading

import pytest

from app import completion_grant, db


def _create_legacy_provider_claim_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """CREATE TABLE episode_video_budget_authorities (
               episode_id TEXT PRIMARY KEY,
               baseline_cny REAL NOT NULL DEFAULT 0,
               cap_cny REAL NOT NULL,
               source TEXT NOT NULL,
               authorized_at REAL NOT NULL,
               updated_at REAL NOT NULL,
               FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
           );
           CREATE TABLE provider_video_budget_claims (
               operation_id TEXT PRIMARY KEY,
               episode_id TEXT NOT NULL,
               job_id TEXT NOT NULL,
               version_id TEXT NOT NULL,
               amount_cny REAL NOT NULL,
               status TEXT NOT NULL,
               created_at REAL NOT NULL,
               updated_at REAL NOT NULL,
               FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
               FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
               FOREIGN KEY(version_id) REFERENCES shot_versions(id) ON DELETE CASCADE
           );
           CREATE INDEX idx_provider_video_budget_episode
               ON provider_video_budget_claims(episode_id,status);"""
    )


def test_init_db_drops_obsolete_storyboard_branch_columns(tmp_path, monkeypatch) -> None:
    database = tmp_path / "obsolete-storyboard-columns.db"
    conn = sqlite3.connect(database)
    conn.executescript(db.SCHEMA)
    conn.execute(
        "ALTER TABLE shots ADD COLUMN storyboard_adopted INTEGER NOT NULL DEFAULT 1"
    )
    conn.execute(
        "ALTER TABLE episodes ADD COLUMN storyboard_completion_mode TEXT "
        "NOT NULL DEFAULT 'ready_for_manual_confirm'"
    )
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','旧项目','ready',1)"
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'scripted',1)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,storyboard_adopted) "
        "VALUES('s1','e1',1,5,0)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())

    db.init_db()

    migrated = db.get_conn()
    shot_columns = {row[1] for row in migrated.execute("PRAGMA table_info(shots)")}
    episode_columns = {row[1] for row in migrated.execute("PRAGMA table_info(episodes)")}
    assert "storyboard_adopted" not in shot_columns
    assert "storyboard_completion_mode" not in episode_columns
    assert migrated.execute("SELECT COUNT(*) FROM shots WHERE id='s1'").fetchone()[0] == 1
    migrated.close()


def test_init_db_migrates_old_supporting_keyframe_default_once(tmp_path, monkeypatch) -> None:
    database = tmp_path / "supporting-keyframes.db"
    conn = sqlite3.connect(database)
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('video_supporting_keyframe_candidates', '1')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())

    db.init_db()

    migrated = db.get_conn()
    value = migrated.execute(
        "SELECT value FROM settings WHERE key='video_supporting_keyframe_candidates'"
    ).fetchone()[0]
    assert value == "3"

    # 迁移完成后，用户仍可主动把候选数改回 1，后续启动不得再次覆盖。
    migrated.execute(
        "UPDATE settings SET value='1' WHERE key='video_supporting_keyframe_candidates'"
    )
    migrated.commit()
    db.init_db()
    value = migrated.execute(
        "SELECT value FROM settings WHERE key='video_supporting_keyframe_candidates'"
    ).fetchone()[0]
    assert value == "1"
    migrated.close()


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
    project_columns = {row[1] for row in migrated.execute("PRAGMA table_info(projects)").fetchall()}
    indexes = {row[1] for row in migrated.execute("PRAGMA index_list(jobs)").fetchall()}
    assert {"next_retry_at", "lease_expires_at", "retry_count"}.issubset(columns)
    assert {
        "refs_resume", "refs_batch_started_at", "scene_refs_batch_started_at",
    }.issubset(project_columns)
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

    db.init_db(reconcile_interrupted=True)

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


def test_plain_init_db_does_not_interrupt_live_work(tmp_path, monkeypatch) -> None:
    database = tmp_path / "non-destructive-init.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO workflow_runs(id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at) "
        "VALUES('run_live','storyboard','episode','e1','RUNNING','fp',1)"
    )
    conn.execute(
        "INSERT INTO step_runs(id,run_id,step_key,status,started_at) "
        "VALUES('step_live','run_live','storyboard','RUNNING',1)"
    )
    conn.commit()

    db.init_db()

    assert conn.execute("SELECT status FROM workflow_runs WHERE id='run_live'").fetchone()[0] == "RUNNING"
    assert conn.execute("SELECT status FROM step_runs WHERE id='step_live'").fetchone()[0] == "RUNNING"


def test_recovery_releases_only_interrupted_command_claims(tmp_path, monkeypatch) -> None:
    database = tmp_path / "command-claims.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS command_idempotency(
               idem_key TEXT PRIMARY KEY, command TEXT NOT NULL, status TEXT NOT NULL,
               result_json TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL
           )"""
    )
    conn.executemany(
        "INSERT INTO command_idempotency VALUES(?,?,?,?,?,?)",
        [
            ("running", "project.import_novel", "running", "{}", 1, 9999999999),
            ("done", "project.import_novel", "succeeded", "{}", 1, 9999999999),
        ],
    )
    conn.commit()

    db.init_db()
    assert conn.execute("SELECT COUNT(*) FROM command_idempotency").fetchone()[0] == 2

    db.init_db(reconcile_interrupted=True)
    rows = conn.execute(
        "SELECT idem_key,status FROM command_idempotency ORDER BY idem_key"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("done", "succeeded")]


def test_init_db_migrates_provider_claims_to_project_owned_ledger(
    monkeypatch,
) -> None:
    migration_dir = db.DATA_DIR / "provider-claim-migration"
    migration_dir.mkdir(parents=True, exist_ok=True)
    database = migration_dir / "legacy-provider-claims.db"
    database.unlink(missing_ok=True)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    _create_legacy_provider_claim_tables(conn)
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) "
        "VALUES('p1','P','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e1','p1',1,'confirmed',1)"""
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) "
        "VALUES('s1','e1',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v1','s1',1,'prompt','idem','succeeded',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_operation_id,provider_create_state,
               created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','succeeded',
               'op1','accepted',1,1
           )"""
    )
    conn.execute(
        """INSERT INTO episode_video_budget_authorities(
               episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
           ) VALUES('e1',0,10,'legacy',1,1)"""
    )
    conn.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,episode_id,job_id,version_id,amount_cny,status,
               created_at,updated_at
           ) VALUES('op1','e1','j1','v1',4,'settled',1,2)"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", migration_dir)
    monkeypatch.setattr(db, "_local", threading.local())

    db.init_db()

    migrated = db.get_conn()
    claim = migrated.execute(
        """SELECT project_id,episode_id,shot_id,job_id,version_id,
                  origin_episode_id,origin_shot_id,origin_job_id,
                  origin_version_id,status,accepted_at,settled_at
             FROM provider_video_budget_claims
            WHERE operation_id='op1'"""
    ).fetchone()
    assert dict(claim) == {
        "project_id": "p1",
        "episode_id": "e1",
        "shot_id": "s1",
        "job_id": "j1",
        "version_id": "v1",
        "origin_episode_id": "e1",
        "origin_shot_id": "s1",
        "origin_job_id": "j1",
        "origin_version_id": "v1",
        "status": "settled",
        "accepted_at": 2.0,
        "settled_at": 2.0,
    }
    foreign_keys = {
        (row["from"], row["table"]): row["on_delete"]
        for row in migrated.execute(
            "PRAGMA foreign_key_list(provider_video_budget_claims)"
        ).fetchall()
    }
    assert foreign_keys == {
        ("project_id", "projects"): "CASCADE",
        ("episode_id", "episodes"): "SET NULL",
        ("shot_id", "shots"): "SET NULL",
        ("job_id", "jobs"): "SET NULL",
        ("version_id", "shot_versions"): "SET NULL",
    }

    migrated.execute("DELETE FROM shot_versions WHERE id='v1'")
    migrated.commit()
    detached = migrated.execute(
        """SELECT project_id,episode_id,shot_id,job_id,version_id,
                  origin_job_id,origin_version_id
             FROM provider_video_budget_claims WHERE operation_id='op1'"""
    ).fetchone()
    assert dict(detached) == {
        "project_id": "p1",
        "episode_id": "e1",
        "shot_id": "s1",
        "job_id": None,
        "version_id": None,
        "origin_job_id": "j1",
        "origin_version_id": "v1",
    }
    assert completion_grant.project_video_budget_snapshot(
        "p1",
        conn=migrated,
    )["used_cny"] == 4

    migrated.execute("DELETE FROM episodes WHERE id='e1'")
    migrated.commit()
    owner = migrated.execute(
        """SELECT project_id,episode_id,shot_id,origin_episode_id,origin_shot_id
             FROM provider_video_budget_claims WHERE operation_id='op1'"""
    ).fetchone()
    assert dict(owner) == {
        "project_id": "p1",
        "episode_id": None,
        "shot_id": None,
        "origin_episode_id": "e1",
        "origin_shot_id": "s1",
    }
    assert completion_grant.project_video_budget_snapshot(
        "p1",
        conn=migrated,
    )["used_cny"] == 4
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []

    migrated.execute("DELETE FROM projects WHERE id='p1'")
    migrated.commit()
    assert migrated.execute(
        "SELECT COUNT(*) FROM provider_video_budget_claims"
    ).fetchone()[0] == 0
    migrated.close()


def test_provider_claim_migration_rejects_missing_project_owner(
    monkeypatch,
) -> None:
    migration_dir = db.DATA_DIR / "provider-claim-orphan-migration"
    migration_dir.mkdir(parents=True, exist_ok=True)
    database = migration_dir / "orphan-provider-claim.db"
    database.unlink(missing_ok=True)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    _create_legacy_provider_claim_tables(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,episode_id,job_id,version_id,amount_cny,status,
               created_at,updated_at
           ) VALUES('orphan','missing-e','missing-j','missing-v',4,'settled',1,2)"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", migration_dir)
    monkeypatch.setattr(db, "_local", threading.local())

    with pytest.raises(RuntimeError, match="project ownership"):
        db.init_db()
