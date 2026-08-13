import json
import sqlite3
from pathlib import Path

from app import db


def test_integrity_repair_backs_up_and_emits_machine_report(tmp_path, monkeypatch) -> None:
    database = tmp_path / "legacy.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, created_at
           ) VALUES('orphan-version', 'missing-shot', 1, 'p', 'k', 0)"""
    )
    conn.commit()
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)

    report = db._repair_integrity(conn)

    assert report["repair_count"] == 1
    assert report["remaining_count"] == 0
    assert report["before"]["orphan_shot_versions"]["identifiers"] == ["orphan-version"]
    assert Path(report["backup_path"]).is_file()
    saved = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    assert saved["backup_path"] == report["backup_path"]
    assert conn.execute("SELECT COUNT(*) FROM shot_versions").fetchone()[0] == 0
    conn.close()


def test_clean_integrity_check_does_not_emit_empty_report(tmp_path, monkeypatch) -> None:
    database = tmp_path / "clean.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)

    report = db._repair_integrity(conn)

    assert report["repair_count"] == 0
    assert report["remaining_count"] == 0
    assert report["backup_path"] is None
    assert "report_path" not in report
    assert not (tmp_path / "integrity_reports").exists()
    conn.close()


def test_integrity_repair_reconciles_video_slots_without_provider_ledger() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) "
        "VALUES('project-1','Legacy','ready',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('episode-1','project-1',1,'planned',1)"""
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s)
           VALUES('shot-1','episode-1',1,5)"""
    )
    conn.executemany(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,provider_task_id,
               status,video_slot_active,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        [
            ("version-owner", "shot-1", 1, "owner", "owner-key", None, "queued", 1, 1),
            (
                "version-provider",
                "shot-1",
                2,
                "provider",
                "provider-key",
                "provider-task-1",
                "waiting_provider",
                1,
                2,
            ),
        ],
    )
    conn.executemany(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               video_slot_active,created_at,updated_at,provider_operation_id,
               provider_create_state,provider_poll_required,
               provider_result_adoptable
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "job-owner",
                "video",
                "shot-1",
                "version-owner",
                "episode-1",
                "project-1",
                "queued",
                1,
                1,
                1,
                None,
                "not_started",
                0,
                1,
            ),
            (
                "job-provider",
                "video",
                "shot-1",
                "version-provider",
                "episode-1",
                "project-1",
                "waiting_provider",
                1,
                2,
                2,
                "operation-1",
                "accepted",
                1,
                0,
            ),
        ],
    )

    report = db._repair_integrity(conn)

    assert report["video_slot_repairs"] == 1
    assert tuple(
        conn.execute(
            "SELECT status,video_slot_active,provider_result_adoptable "
            "FROM jobs WHERE id='job-owner'"
        ).fetchone()
    ) == ("queued", 1, 1)
    assert tuple(
        conn.execute(
            "SELECT status,video_slot_active,provider_poll_required,"
            "provider_result_adoptable FROM jobs WHERE id='job-provider'"
        ).fetchone()
    ) == ("waiting_provider", 0, 1, 0)
    assert conn.execute(
        "SELECT video_slot_active FROM shot_versions WHERE id='version-owner'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT video_slot_active FROM shot_versions WHERE id='version-provider'"
    ).fetchone()[0] == 0
    assert not db._table_exists(conn, "provider_video_budget_claims")
    conn.close()
