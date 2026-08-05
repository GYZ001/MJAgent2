"""剧本删除与生成前原文台词多选的回归测试。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import api, db
from app.capabilities import ensure_catalog_loaded
from app.capabilities.direct import enter_handler
from app.capabilities.registry import get_registry
from app.production.revision import (
    ensure_production_revision,
    get_active_production_revision,
    mark_baseline_generated,
    save_checkpoint,
    screenplay_production_state,
    set_published_artifact,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-delete.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','ready',?)",
        (db.now(),),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content, char_count) VALUES(?,?,?,?,?)",
        (
            "p1",
            1,
            "第一章",
            "测验员：斗之力，三段！\n萧炎：只有三段？\n测验员：结果无误。",
            40,
        ),
    )
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters,
               screenplay_json, screenplay_status, screenplay_required_dialogues,
               screenplay_artifact_id, working_screenplay_artifact_id,
               published_screenplay_artifact_id, status, created_at
           ) VALUES('e1','p1',1,'第一集','[1]',?,'ready',?,
                    'art-published','art-working','art-published','scripted',?)""",
        (
            json.dumps({"episode_no": 1, "title": "第一集"}, ensure_ascii=False),
            json.dumps(["斗之力，三段！", "只有三段？"], ensure_ascii=False),
            db.now(),
        ),
    )
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES('shot1','e1',1,5)"
    )
    conn.commit()

    revision = ensure_production_revision(
        episode_id="e1", kind="screenplay", resume=False
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id="art-baseline",
        working_artifact_id="art-working",
    )
    yield


def test_script_view_lists_all_source_dialogues_for_multi_select() -> None:
    detail = api.episode_detail("e1", view="script")

    assert [item["text"] for item in detail["source_dialogue_occurrences"]] == [
        "斗之力，三段！", "只有三段？", "结果无误。",
    ]
    assert detail["required_dialogue_lines"] == ["斗之力，三段！", "只有三段？"]


def test_delete_screenplay_resets_to_fresh_baseline_but_keeps_dialogue_selection() -> None:
    with enter_handler():
        result = asyncio.run(api.delete_screenplay("e1"))

    conn = db.get_conn()
    episode = conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    assert result["downstream_shots_cleared"] == 1
    assert result["required_dialogue_lines"] == ["斗之力，三段！", "只有三段？"]
    assert episode["screenplay_json"] is None
    assert episode["screenplay_status"] == "pending"
    assert episode["screenplay_artifact_id"] is None
    assert episode["working_screenplay_artifact_id"] is None
    assert episode["published_screenplay_artifact_id"] is None
    assert episode["status"] == "planned"
    assert json.loads(episode["screenplay_required_dialogues"]) == [
        "斗之力，三段！",
        "只有三段？",
    ]
    assert conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'").fetchone()["c"] == 0
    state = screenplay_production_state("e1")
    assert state["operation"] == "baseline"
    assert state["baseline_done"] is False
    assert state["can_resume_repair"] is False


def test_delete_published_screenplay_does_not_project_historical_completed_stages() -> None:
    revision = get_active_production_revision("e1", "screenplay")
    assert revision is not None
    save_checkpoint(revision.id, {
        "phase": "SUCCEEDED",
        "quality_score": 20.0,
        "quality_issue_count": 0,
    })
    set_published_artifact(revision.id, "art-published")

    before = screenplay_production_state("e1")
    assert all(stage["status"] == "completed" for stage in before["stages"])

    with enter_handler():
        asyncio.run(api.delete_screenplay("e1"))

    after = screenplay_production_state("e1")
    assert after["operation"] == "baseline"
    assert "quality_score" not in after
    assert all(stage["status"] == "pending" for stage in after["stages"])
    historical = db.get_conn().execute(
        "SELECT status FROM production_revisions WHERE id=?",
        (revision.id,),
    ).fetchone()
    assert historical["status"] == "published"


def test_failed_working_baseline_can_be_deleted_before_any_version_is_published() -> None:
    conn = db.get_conn()
    conn.execute(
        """UPDATE episodes SET
               screenplay_json=NULL,
               screenplay_status='repairing',
               screenplay_error='WAITING_INPUT: 局部修复失败',
               screenplay_artifact_id=NULL,
               published_screenplay_artifact_id=NULL,
               status='planned'
           WHERE id='e1'"""
    )
    conn.commit()

    before = screenplay_production_state("e1")
    assert before["can_resume_repair"] is True
    assert conn.execute(
        "SELECT screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()["screenplay_json"] is None

    with enter_handler():
        asyncio.run(api.delete_screenplay("e1"))

    episode = conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    assert episode["screenplay_status"] == "pending"
    assert episode["screenplay_error"] is None
    assert episode["working_screenplay_artifact_id"] is None
    after = screenplay_production_state("e1")
    assert after["operation"] == "baseline"
    assert after["can_resume_repair"] is False


def test_delete_route_is_registered_as_destructive_capability() -> None:
    ensure_catalog_loaded()
    registry = get_registry()
    assert registry.rest_bindings[
        "DELETE /api/episodes/{episode_id}/screenplay"
    ] == "screenplay.delete"
    assert registry.commands["screenplay.delete"].title == "删除当前剧本"
