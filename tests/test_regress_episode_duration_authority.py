from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import regress_episode_duration_authority as regression


class _SnapshotCreated(Exception):
    pass


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def test_run_regression_copies_committed_wal_snapshot_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_db = tmp_path / "production.db"
    copy_db = tmp_path / "regression.db"
    writer = sqlite3.connect(source_db)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("PRAGMA foreign_keys=ON")
        writer.executescript(
            """
            CREATE TABLE projects(id TEXT PRIMARY KEY);
            CREATE TABLE episodes(
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id)
            );
            CREATE TABLE provider_calls(
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL
            );
            """
        )
        writer.commit()
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
            0,
            0,
            0,
        )

        writer.execute("INSERT INTO projects(id) VALUES('project-1')")
        writer.execute(
            "INSERT INTO episodes(id, project_id) VALUES('episode-1', 'project-1')"
        )
        writer.executemany(
            "INSERT INTO provider_calls(id, status) VALUES(?, 'DONE')",
            [(1,), (2,)],
        )
        writer.commit()
        assert Path(f"{source_db}-wal").stat().st_size > 32

        snapshot_validation: dict[str, object] = {}
        create_snapshot = regression._create_verified_database_snapshot

        def capture_snapshot(source: Path, destination: Path) -> dict:
            result = create_snapshot(source, destination)
            snapshot_validation.update(result)
            return result

        monkeypatch.setattr(
            regression,
            "_create_verified_database_snapshot",
            capture_snapshot,
        )
        monkeypatch.setattr(
            regression,
            "replay",
            lambda **_kwargs: {
                "authoritative_target_duration_s": 60,
                "authoritative_first_pass_budget_cny": 1.5,
                "story_event_coverage": {"covered": 2, "total": 2},
                "information_coverage": {"covered": 1, "total": 1},
                "source_coverage": {"covered": 3, "total": 3},
                "screenplay_errors": [],
                "outline_errors": [],
            },
        )
        monkeypatch.setattr(
            regression,
            "_reset_connection",
            lambda _path: (_ for _ in ()).throw(_SnapshotCreated()),
        )

        with pytest.raises(_SnapshotCreated):
            regression.run_regression(
                source_db=source_db,
                artifact_id="artifact-1",
                project_id="project-1",
                copy_path=copy_db,
                expected_duration_s=60,
                expected_cost_cny=1.5,
                expected_story_events=2,
                expected_source_segments=3,
            )

        assert snapshot_validation == {
            "source_matches_copy": True,
            "table_counts": {
                "episodes": 1,
                "projects": 1,
                "provider_calls": 2,
            },
            "provider_calls": 2,
            "provider_call_max_id": 2,
            "quick_check": ["ok"],
            "integrity_check": ["ok"],
            "foreign_key_check": [],
        }
        with _open_readonly(source_db) as source, _open_readonly(copy_db) as copied:
            assert copied.execute(
                "SELECT COUNT(*) FROM provider_calls"
            ).fetchone()[0] == source.execute(
                "SELECT COUNT(*) FROM provider_calls"
            ).fetchone()[0]
            assert copied.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
            assert copied.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
            assert copied.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert copied.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        writer.close()
