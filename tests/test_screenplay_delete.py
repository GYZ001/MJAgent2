"""剧本删除回归测试。"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app import api, db
from app.capabilities import ensure_catalog_loaded
from app.capabilities import inputs as capability_inputs
from app.capabilities import preflight as capability_preflight
from app.capabilities.direct import enter_handler
from app.capabilities.registry import get_registry
from app.evidence import repository
from app.orchestration import api as orchestration_api
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


def test_script_view_does_not_expose_removed_dialogue_selection_fields() -> None:
    detail = api.episode_detail("e1", view="script")

    assert "source_dialogue_occurrences" not in detail
    assert "required_dialogue_lines" not in detail
    assert "required_dialogue_occurrence_ids" not in detail


def test_delete_screenplay_resets_to_fresh_baseline_and_clears_legacy_selection() -> None:
    with enter_handler():
        result = asyncio.run(api.delete_screenplay("e1"))

    conn = db.get_conn()
    episode = conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    assert result["downstream_shots_cleared"] == 1
    assert "required_dialogue_lines" not in result
    assert episode["screenplay_json"] is None
    assert episode["screenplay_status"] == "pending"
    assert episode["screenplay_artifact_id"] is None
    assert episode["working_screenplay_artifact_id"] is None
    assert episode["published_screenplay_artifact_id"] is None
    assert episode["status"] == "planned"
    assert json.loads(episode["screenplay_required_dialogues"]) == []
    assert json.loads(episode["screenplay_required_dialogue_occurrences"]) == []
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
    assert before["operation"] == "baseline_rebuild"
    assert before["can_resume_repair"] is False
    assert before["can_resume_baseline"] is True
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


def test_delete_preflight_allows_failed_revision_without_projection() -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM shots WHERE episode_id='e1'")
    conn.execute(
        """UPDATE episodes SET
               screenplay_json=NULL,
               screenplay_status='failed',
               screenplay_artifact_id=NULL,
               working_screenplay_artifact_id=NULL,
               published_screenplay_artifact_id=NULL,
               screenplay_production_revision_id=NULL,
               screenplay_completion_certificate_id=NULL,
               active_screenplay_run_id='run-failed',
               status='planned'
           WHERE id='e1'"""
    )
    conn.commit()

    result = capability_preflight.screenplay_delete(
        capability_inputs.ScreenplayDeleteInput(episode_id="e1"),
    )

    assert result.allowed is True
    assert result.requires_confirmation is True
    assert result.affected.episodes == ["e1"]
    assert result.affected.shot_count == 0


def test_delete_route_is_registered_as_destructive_capability() -> None:
    ensure_catalog_loaded()
    registry = get_registry()
    assert registry.rest_bindings[
        "DELETE /api/episodes/{episode_id}/screenplay"
    ] == "screenplay.delete"
    assert registry.commands["screenplay.delete"].title == "删除当前剧本"


def test_delete_rolls_back_shot_cleanup_when_later_step_fails(monkeypatch) -> None:
    original = api.worker.delete_episode_shots

    def fail_after_cleanup(episode_id: str, **kwargs):
        original(episode_id, **kwargs)
        raise RuntimeError("injected delete failure")

    monkeypatch.setattr(api.worker, "delete_episode_shots", fail_after_cleanup)

    with enter_handler(), pytest.raises(RuntimeError, match="injected"):
        asyncio.run(api.delete_screenplay("e1"))

    conn = db.get_conn()
    episode = conn.execute(
        "SELECT screenplay_status,screenplay_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(episode) == {
        "screenplay_status": "ready",
        "screenplay_artifact_id": "art-published",
    }
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'"
    ).fetchone()["c"] == 1
    assert get_active_production_revision("e1", "screenplay") is not None


@pytest.mark.asyncio
async def test_deleted_screenplay_cannot_be_revived_from_historical_run(
    monkeypatch,
) -> None:
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="historical",
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET active_screenplay_run_id=? WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    with enter_handler():
        await api.delete_screenplay("e1")

    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: pytest.fail("历史 Run 不得创建恢复运行"),
    )
    with pytest.raises(HTTPException) as exc_info:
        await orchestration_api.retry_run(run_id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "SCREENPLAY_RUN_NO_LONGER_OWNS_EPISODE"
    episode = conn.execute(
        "SELECT screenplay_status,active_screenplay_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(episode) == {
        "screenplay_status": "pending",
        "active_screenplay_run_id": None,
    }
