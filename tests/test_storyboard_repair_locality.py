from __future__ import annotations

import sqlite3

from app.storyboard_supervisor import (
    _contiguous_shot_rows,
    _delete_shot_window,
    _open_shot_gap,
)


def _conn(count: int = 5) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE shots("
        "id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, shot_no INTEGER NOT NULL, "
        "UNIQUE(episode_id, shot_no))"
    )
    conn.executemany(
        "INSERT INTO shots(id,episode_id,shot_no) VALUES(?, 'e1', ?)",
        [(f"s{shot_no}", shot_no) for shot_no in range(1, count + 1)],
    )
    return conn


def _rows(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT id,shot_no FROM shots WHERE episode_id='e1' ORDER BY shot_no"
    ).fetchall()


def test_local_delete_preserves_suffix_numbers_and_refills_same_gap(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(
        "app.worker.clear_shot_artifacts",
        lambda *_args, **_kwargs: None,
    )

    assert _delete_shot_window(conn, "e1", 3, 3) == 1
    assert [(row["id"], row["shot_no"]) for row in _rows(conn)] == [
        ("s1", 1),
        ("s2", 2),
        ("s4", 4),
        ("s5", 5),
    ]
    assert [row["shot_no"] for row in _contiguous_shot_rows(_rows(conn))] == [1, 2]

    conn.execute("INSERT INTO shots(id,episode_id,shot_no) VALUES('replacement','e1',3)")

    assert [(row["id"], row["shot_no"]) for row in _rows(conn)] == [
        ("s1", 1),
        ("s2", 2),
        ("replacement", 3),
        ("s4", 4),
        ("s5", 5),
    ]
    assert [row["shot_no"] for row in _contiguous_shot_rows(_rows(conn))] == [
        1, 2, 3, 4, 5,
    ]


def test_insert_opens_gap_without_deleting_suffix() -> None:
    conn = _conn(count=4)

    assert _open_shot_gap(conn, "e1", 3) == 2
    assert [(row["id"], row["shot_no"]) for row in _rows(conn)] == [
        ("s1", 1),
        ("s2", 2),
        ("s3", 4),
        ("s4", 5),
    ]
    assert [row["shot_no"] for row in _contiguous_shot_rows(_rows(conn))] == [1, 2]

    conn.execute("INSERT INTO shots(id,episode_id,shot_no) VALUES('inserted','e1',3)")

    assert [(row["id"], row["shot_no"]) for row in _rows(conn)] == [
        ("s1", 1),
        ("s2", 2),
        ("inserted", 3),
        ("s3", 4),
        ("s4", 5),
    ]


def test_insert_preserves_immutable_checkpoint_until_publish_rebind() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE shots("
        "id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, shot_no INTEGER NOT NULL, "
        "storyboard_artifact_id TEXT, UNIQUE(episode_id, shot_no))"
    )
    conn.execute(
        "CREATE TABLE artifacts("
        "id TEXT PRIMARY KEY,type TEXT,scope_type TEXT,scope_id TEXT,version INTEGER,"
        "status TEXT,superseded_by_artifact_id TEXT,stale_reason TEXT,approved_at REAL)"
    )
    conn.executemany(
        "INSERT INTO artifacts VALUES(?, 'storyboard_shot', 'storyboard_checkpoint', ?, 1, ?, ?, NULL, 1)",
        [
            ("a2", "e1:2", "superseded", "replacement"),
            ("a3", "e1:3", "approved", None),
        ],
    )
    conn.executemany(
        "INSERT INTO shots VALUES(?, 'e1', ?, ?)",
        [("s2", 2, "a2"), ("s3", 3, "a3")],
    )

    assert _open_shot_gap(conn, "e1", 2) == 2

    rows = conn.execute(
        """SELECT s.id,s.shot_no,s.storyboard_artifact_id,
                  a.scope_id,a.status,a.superseded_by_artifact_id
             FROM shots s JOIN artifacts a ON a.id=s.storyboard_artifact_id
            ORDER BY s.shot_no"""
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("s2", 3, "a2", "e1:2", "superseded", "replacement"),
        ("s3", 4, "a3", "e1:3", "approved", None),
    ]
