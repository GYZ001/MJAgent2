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
