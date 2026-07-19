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
    migrated.close()
