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


def _seed_shot_with_adoption(conn: sqlite3.Connection, *, version_status: str, video_path: str | None) -> None:
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('project-1','P','ready',0)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('episode-1','project-1',1,'generating',0)"""
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s,adopted_version_id)
           VALUES('shot-1','episode-1',1,5,'version-1')"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES('version-1','shot-1',1,'p','k',?,?,0)""",
        (version_status, video_path),
    )
    conn.commit()


def test_repair_dangling_video_adoption_clears_pointer_to_non_succeeded_version() -> None:
    """复现 EP1 段5/6/7：采用指针指向一条状态已被改写为 failed 的版本。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    _seed_shot_with_adoption(conn, version_status="failed", video_path="/tmp/does-not-exist.mp4")

    cleared = db._repair_dangling_video_adoption(conn)

    assert cleared == 1
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='shot-1'"
    ).fetchone()[0] is None
    conn.close()


def test_repair_dangling_video_adoption_ignores_missing_video_file(tmp_path) -> None:
    """启动路径刻意不做 stat()：succeeded 但文件缺失属于运维层面的外部删除，
    不属于这条修复的职责——``test_video_provider_recovery_slots.py`` 里固定
    在 ``status='succeeded'`` 的历史行上放不存在的占位路径（只测状态机，不
    落真实文件），这里必须保留指针，不能把这类合法测试夹具当脏数据清空。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    missing = tmp_path / "gone.mp4"
    _seed_shot_with_adoption(conn, version_status="succeeded", video_path=str(missing))

    cleared = db._repair_dangling_video_adoption(conn)

    assert cleared == 0
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='shot-1'"
    ).fetchone()[0] == "version-1"
    conn.close()


def test_repair_dangling_video_adoption_keeps_valid_pointer() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    _seed_shot_with_adoption(conn, version_status="succeeded", video_path="/tmp/real.mp4")

    cleared = db._repair_dangling_video_adoption(conn)

    assert cleared == 0
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='shot-1'"
    ).fetchone()[0] == "version-1"
    conn.close()


def test_guard_adopted_version_terminal_status_trigger_clears_pointer_on_status_change() -> None:
    """采用之后同一版本被别的路径判定失败，触发器必须在同一事务内释放采用指针。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    _seed_shot_with_adoption(conn, version_status="succeeded", video_path="/tmp/real.mp4")
    conn.executescript(db.INTEGRITY_SCHEMA)

    conn.execute("UPDATE shot_versions SET status='failed',error='qa_result stale' WHERE id='version-1'")
    conn.commit()

    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='shot-1'"
    ).fetchone()[0] is None


def test_guard_adopted_version_terminal_status_trigger_ignores_other_columns() -> None:
    """触发器只在 status 列被写入时触发；写其它列不得误清有效采用。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    _seed_shot_with_adoption(conn, version_status="succeeded", video_path="/tmp/real.mp4")
    conn.executescript(db.INTEGRITY_SCHEMA)

    conn.execute("UPDATE shot_versions SET adoption_reason='kept' WHERE id='version-1'")
    conn.commit()

    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='shot-1'"
    ).fetchone()[0] == "version-1"
