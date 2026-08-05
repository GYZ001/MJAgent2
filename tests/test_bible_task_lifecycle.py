import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from app import api, db, task_registry
from app.api import BIBLE_INTERRUPTED_ERROR, _recover_orphan_bible_row
from app.schemas import Bible, Character, World


def test_orphan_running_bible_status_is_recovered() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_status TEXT, bible_error TEXT)")
    conn.execute(
        "INSERT INTO projects(id, bible_status, bible_error) VALUES('proj_test', 'running', NULL)"
    )
    conn.commit()
    task_registry.cancel("bible", "proj_test")

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

    def fake_start_refs(project_id: str, only_character: str | None) -> bool:
        started["args"] = (project_id, only_character)
        return True

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", fake_start_refs)

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute("SELECT * FROM projects WHERE id='proj_test'").fetchone()
    assert row["bible_status"] == "ready"
    assert row["status"] == "bible_ready"
    assert row["bible_version"] == 1
    assert started["args"] == ("proj_test", None)


def test_bible_task_does_not_start_unquoted_scene_generation(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT, "
        "scene_refs_status TEXT DEFAULT 'idle', scene_refs_error TEXT)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_status, status, scene_refs_status) "
        "VALUES('proj_test', 'running', 'ingested', 'idle')"
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="国风水墨"),
            characters=[
                Character(
                    name="萧炎",
                    role="主角",
                    appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩",
                )
            ],
        )

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", lambda *_args: True)
    monkeypatch.setattr(
        task_registry,
        "spawn",
        lambda *_args, **_kwargs: pytest.fail("场景任务必须在场景库完成独立费用确认后启动"),
    )

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute(
        "SELECT bible_status,scene_refs_status,scene_refs_error FROM projects "
        "WHERE id='proj_test'"
    ).fetchone()
    assert dict(row) == {
        "bible_status": "ready",
        "scene_refs_status": "idle",
        "scene_refs_error": None,
    }


def test_bible_completion_preserves_planned_project_status(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, plan_status TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_status, bible_error, plan_status, status) "
        "VALUES('proj_planned', NULL, 0, 'running', NULL, 'ready', 'planned')"
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="ink animation"),
            characters=[
                Character(
                    name="Hero",
                    role="lead",
                    appearance_canonical="black hair, blue coat, tall build",
                )
            ],
        )

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", lambda *_args: True)

    asyncio.run(api._bible_task("proj_planned", trigger_full_refs=True))

    row = conn.execute("SELECT status, bible_status FROM projects WHERE id='proj_planned'").fetchone()
    assert row["bible_status"] == "ready"
    assert row["status"] == "planned"


@pytest.mark.asyncio
async def test_bible_spawn_failure_restores_state_and_keeps_quote(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bible-spawn.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_status,bible_error,created_at) "
        "VALUES('p1','P','ingested','idle','上次状态',1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','正文',2)"
    )
    conn.commit()
    monkeypatch.setattr(api, "_require_harness_engine", lambda _project_id: None)
    precheck = api._compute_bible_generate_precheck("p1")
    quote = api._issue_payment_quote(precheck)

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)

    with pytest.raises(HTTPException) as exc_info:
        await api._start_bible_core(
            "p1",
            "保留这条反馈",
            confirm=True,
            quote_id=quote["quote_id"],
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "BIBLE_START_FAILED"
    project = conn.execute(
        "SELECT bible_status,bible_error,bible_feedback FROM projects WHERE id='p1'"
    ).fetchone()
    assert dict(project) == {
        "bible_status": "idle",
        "bible_error": "上次状态",
        "bible_feedback": None,
    }
    assert conn.execute(
        "SELECT consumed_at FROM character_payment_quotes WHERE quote_id=?",
        (quote["quote_id"],),
    ).fetchone()["consumed_at"] is None


@pytest.mark.asyncio
async def test_bible_shutdown_keeps_running_projection_for_recovery(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bible-shutdown.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_status,created_at) "
        "VALUES('p1','P','ingested','running',1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','正文',2)"
    )
    conn.commit()

    async def interrupted(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "generate_bible", interrupted)
    monkeypatch.setattr(task_registry, "shutdown_in_progress", lambda: True)

    with pytest.raises(asyncio.CancelledError):
        await api._bible_task("p1")

    project = conn.execute(
        "SELECT bible_status,bible_error FROM projects WHERE id='p1'"
    ).fetchone()
    assert dict(project) == {"bible_status": "running", "bible_error": None}
