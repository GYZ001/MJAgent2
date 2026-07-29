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
