import sqlite3

from app.api import BIBLE_INTERRUPTED_ERROR, _bible_tasks, _recover_orphan_bible_row


def test_orphan_running_bible_status_is_recovered() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_status TEXT, bible_error TEXT)")
    conn.execute(
        "INSERT INTO projects(id, bible_status, bible_error) VALUES('proj_test', 'running', NULL)"
    )
    conn.commit()
    _bible_tasks.pop("proj_test", None)

    row = conn.execute("SELECT * FROM projects WHERE id='proj_test'").fetchone()
    recovered = _recover_orphan_bible_row(conn, row)

    assert recovered["bible_status"] == "failed"
    assert recovered["bible_error"] == BIBLE_INTERRUPTED_ERROR
