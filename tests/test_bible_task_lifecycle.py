import asyncio
import sqlite3

from app import api
from app.api import BIBLE_INTERRUPTED_ERROR, _bible_tasks, _recover_orphan_bible_row
from app.schemas import Bible, Character, World


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


def test_bible_task_starts_full_refs_after_success(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_status, bible_error, status) "
        "VALUES('proj_test', NULL, 0, 'running', NULL, NULL)"
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="国风水墨"),
            characters=[Character(name="萧炎", role="主角", appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩")],
        )

    started: dict[str, object] = {}

    def fake_start_refs(project_id: str, only_character: str | None, *, with_segmentation: bool) -> bool:
        started["args"] = (project_id, only_character, with_segmentation)
        return True

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", fake_start_refs)

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute("SELECT * FROM projects WHERE id='proj_test'").fetchone()
    assert row["bible_status"] == "ready"
    assert row["status"] == "bible_ready"
    assert row["bible_version"] == 1
    assert started["args"] == ("proj_test", None, True)
