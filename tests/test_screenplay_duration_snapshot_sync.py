from __future__ import annotations

import pytest

from app import db
from app.domain.common import _apply_compact_target, _storyboard_target_for_source
from app.orchestration.state_machine import StateConflict
from app.production.screenplay_repair import _persist_screenplay_duration_expansion


PROJECT_ID = "project-duration-snapshot"
EPISODE_ID = "episode-duration-snapshot"
RUN_ID = "run-duration-snapshot"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-duration-snapshot.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed_episode(conn, *, target: int, planning: int, run_id: str | None) -> None:
    """Model an episode whose last successful run expanded to a non-rounded value.

    ``target`` / ``planning`` both start at 791 (a legacy expanded duration), so a
    fresh compact pass must re-round them to 790 in BOTH the DB row and the
    in-memory snapshot handed to the production pipeline.
    """
    conn.execute(
        "INSERT INTO projects(id,name,created_at) VALUES(?,?,?)",
        (PROJECT_ID, "duration snapshot", db.now()),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,
               target_duration_s,planning_target_duration_s,
               planning_duration_source,target_duration_authority,
               active_screenplay_run_id,
               status,screenplay_status,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            EPISODE_ID,
            PROJECT_ID,
            1,
            "Fixture",
            "[1]",
            target,
            planning,
            "screenplay_source_capacity_estimate",
            "planning_estimate",
            run_id,
            "planned",
            "pending",
            db.now(),
        ),
    )
    conn.commit()


def test_compact_target_syncs_db_and_memory_snapshot() -> None:
    conn = db.get_conn()
    _seed_episode(conn, target=791, planning=791, run_id=RUN_ID)

    # Snapshot copied from the DB row, exactly as `_screenplay_task` does.
    ep_data = dict(conn.execute(
        "SELECT * FROM episodes WHERE id=?", (EPISODE_ID,)
    ).fetchone())

    # Source length is irrelevant to the compact rounding; 791 -> 790.
    compact_target = _storyboard_target_for_source(
        ep_data.get("target_duration_s"), 5000
    )
    assert compact_target == 790
    assert compact_target != ep_data.get("target_duration_s")

    _apply_compact_target(conn, EPISODE_ID, ep_data, compact_target)

    # 1) DB row: every rewritten column reflects the compact target.
    row = conn.execute(
        """SELECT target_duration_s,planning_target_duration_s,
                  planning_duration_source,target_duration_authority
             FROM episodes WHERE id=?""",
        (EPISODE_ID,),
    ).fetchone()
    assert tuple(row) == (
        790,
        790,
        "screenplay_source_capacity_estimate",
        "planning_estimate",
    )

    # 2) In-memory snapshot: planning no longer stale at 791; all synced.
    assert ep_data["target_duration_s"] == 790
    assert ep_data["planning_target_duration_s"] == 790
    assert ep_data["planning_duration_source"] == "screenplay_source_capacity_estimate"
    assert ep_data["target_duration_authority"] == "planning_estimate"

    # 3) Downstream baseline duration-expansion CAS: expected_planning_s taken
    #    from the (now synced) snapshot matches the DB, so rowcount==1 and no
    #    StateConflict is raised (the exact production failure that motivated the fix).
    _persist_screenplay_duration_expansion(
        conn,
        episode_id=EPISODE_ID,
        expected_target_s=790,
        expected_planning_s=ep_data["planning_target_duration_s"],
        expected_duration_authority="planning_estimate",
        expected_active_run_id=RUN_ID,
        required_target_s=810,
    )
    conn.commit()

    expanded = conn.execute(
        "SELECT target_duration_s,planning_target_duration_s FROM episodes WHERE id=?",
        (EPISODE_ID,),
    ).fetchone()
    assert tuple(expanded) == (810, 810)


def test_stale_snapshot_would_break_cas_without_sync() -> None:
    """Guard: prove the CAS fails when the snapshot is NOT synced (the old bug).

    This locks in that the fix is load-bearing: feeding the pre-compact planning
    value (791) into the CAS while the DB holds 790 must raise StateConflict.
    """
    conn = db.get_conn()
    _seed_episode(conn, target=791, planning=791, run_id=RUN_ID)

    ep_data = dict(conn.execute(
        "SELECT * FROM episodes WHERE id=?", (EPISODE_ID,)
    ).fetchone())
    stale_planning = ep_data["planning_target_duration_s"]  # 791, captured before compact

    _apply_compact_target(
        conn, EPISODE_ID, ep_data, _storyboard_target_for_source(791, 5000)
    )

    with pytest.raises(StateConflict) as exc_info:
        _persist_screenplay_duration_expansion(
            conn,
            episode_id=EPISODE_ID,
            expected_target_s=790,
            expected_planning_s=stale_planning,  # 791: the drifted value
            expected_duration_authority="planning_estimate",
            expected_active_run_id=RUN_ID,
            required_target_s=810,
        )
    assert exc_info.value.entity == "screenplay_duration"
