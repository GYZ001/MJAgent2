from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import db
from app.domain import video_ops
from app.production.publish import publish_storyboard
from app.video_plan import current_storyboard_release_manifest
from tests.test_narrative_publish_gate import (
    _artifact,
    _reviewed_publish_candidate,
)
from tests.test_narrative_continuity import _board, _screenplay
from tests.test_narrative_review import _persist_review_projection


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "narrative-video-gate.db")
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()


async def _published_narrative_case(monkeypatch) -> dict:
    case = await _reviewed_publish_candidate(monkeypatch)
    publish_storyboard(
        **case["publish_kwargs"],
        evaluation_ids=case["evaluation_ids"],
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET status='confirmed', narrative_status='ready', "
        "narrative_review_artifact_id=? WHERE id='episode-generic'",
        (case["report_artifact"]["id"],),
    )
    conn.commit()
    return case


def _episode_and_live_evaluation(case: dict):
    conn = db.get_conn()
    episode = conn.execute("SELECT * FROM episodes WHERE id='episode-generic'").fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id='project-generic'").fetchone()
    evaluation = video_ops.evaluate_storyboard_for_confirmation(
        episode,
        case["board"],
        case["screenplay"],
        video_ops._project_bible_or_placeholder(project),
        has_real_bible=False,
        record_metrics=False,
    )
    return episode, evaluation


def test_storyboard_authority_projection_excludes_score_only_risk_tags() -> None:
    from app.narrative import storyboard_authority_projection

    before = _board()
    after = before.model_copy(deep=True)
    after.shots[0].risk_tags = ["duration_gt5_needs_review"]

    assert storyboard_authority_projection(after) == storyboard_authority_projection(
        before
    )


@pytest.mark.asyncio
async def test_live_projection_drift_blocks_confirmation_but_not_active_revision(
    monkeypatch,
) -> None:
    case = await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    project = conn.execute(
        "SELECT * FROM projects WHERE id='project-generic'"
    ).fetchone()
    drifted = case["board"].model_copy(deep=True)
    drifted.shots[0].action_desc += " Drifted projection."

    blocked = video_ops.evaluate_storyboard_for_confirmation(
        episode,
        drifted,
        case["screenplay"],
        video_ops._project_bible_or_placeholder(project),
        has_real_bible=False,
        record_metrics=False,
    )

    assert any(
        "STORYBOARD_AUTHORITY_PROJECTION_DRIFT" in error
        for error in blocked.errors
    )

    active_episode = dict(episode)
    active_episode["active_storyboard_run_id"] = "run-active-revision"
    active = video_ops.evaluate_storyboard_for_confirmation(
        active_episode,
        drifted,
        case["screenplay"],
        video_ops._project_bible_or_placeholder(project),
        has_real_bible=False,
        record_metrics=False,
    )
    assert not any(
        "STORYBOARD_AUTHORITY_PROJECTION_DRIFT" in error
        for error in active.errors
    )


def _assert_optional_review_does_not_revoke_authority(case: dict) -> None:
    episode, evaluation = _episode_and_live_evaluation(case)
    assert video_ops._has_current_storyboard_completion_certificate(db.get_conn(), episode) is True
    assert not any("NARRATIVE_REVIEW_MISSING" in error for error in evaluation.errors)
    video_ops._assert_storyboard_generation_gate("episode-generic")


@pytest.mark.asyncio
async def test_live_evaluation_is_diagnostic_and_cannot_replace_narrative_certificate(
    monkeypatch,
) -> None:
    await _reviewed_publish_candidate(monkeypatch)
    monkeypatch.setattr(
        video_ops,
        "_has_current_storyboard_completion_certificate",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        video_ops,
        "evaluate_storyboard_for_confirmation",
        lambda *_args, **_kwargs: SimpleNamespace(errors=[]),
    )

    with pytest.raises(HTTPException) as caught:
        video_ops._assert_storyboard_generation_gate("episode-generic")

    assert caught.value.status_code == 409
    assert any(
        "NARRATIVE_CERTIFICATE_REQUIRED" in error
        for error in caught.value.detail["errors"]
    )


@pytest.mark.asyncio
async def test_internal_enqueue_checks_narrative_authority_before_creating_job(
    monkeypatch,
) -> None:
    await _reviewed_publish_candidate(monkeypatch)
    from app import worker

    began_preflight = False

    def should_not_begin(*_args, **_kwargs):
        nonlocal began_preflight
        began_preflight = True
        raise AssertionError("preflight job must not exist before authority passes")

    monkeypatch.setattr(worker, "_begin_video_preflight_job", should_not_begin)

    with pytest.raises(ValueError, match="STORYBOARD_CONFIRMATION_REQUIRED"):
        worker.enqueue_shot("shot-row-1")

    assert began_preflight is False


def test_internal_enqueue_preserves_explicit_plan_null_compatibility() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan = None
    _persist_review_projection(screenplay, _board())
    db.get_conn().execute(
        "UPDATE episodes SET status='confirmed' WHERE id='episode-generic'"
    )
    db.get_conn().commit()
    from app import worker

    worker._assert_enqueue_storyboard_authority("shot-row-1")


def test_reviewed_projection_cannot_downgrade_to_plan_null_legacy_path() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan = None
    _persist_review_projection(screenplay, _board())
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET narrative_status='ready', narrative_review_artifact_id='review-old' "
        "WHERE id='episode-generic'"
    )
    conn.commit()
    from app import worker

    with pytest.raises(ValueError, match="权威链|漂移"):
        worker._assert_enqueue_storyboard_authority("shot-row-1")


@pytest.mark.asyncio
async def test_storyboard_publish_uses_immutable_screenplay_to_reject_plan_downgrade(
    monkeypatch,
) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)
    conn = db.get_conn()
    projection = json.loads(
        conn.execute(
            "SELECT screenplay_json FROM episodes WHERE id='episode-generic'"
        ).fetchone()["screenplay_json"]
    )
    projection["narrative_plan"] = None
    conn.execute(
        "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()

    with pytest.raises(ValueError, match="漂移|权威"):
        publish_storyboard(
            **case["publish_kwargs"],
            evaluation_ids=case["evaluation_ids"],
        )


@pytest.mark.asyncio
async def test_missing_mutable_projection_cannot_enter_legacy_paid_path(
    monkeypatch,
) -> None:
    await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_json=NULL WHERE id='episode-generic'"
    )
    conn.commit()
    from app import worker

    with pytest.raises(ValueError, match="权威|投影"):
        worker._assert_enqueue_storyboard_authority("shot-row-1")


@pytest.mark.parametrize("downgrade", ["missing_projection", "plan_null"])
@pytest.mark.asyncio
async def test_video_release_manifest_cannot_downgrade_modern_authority_to_legacy(
    monkeypatch,
    downgrade: str,
) -> None:
    await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    manifest = current_storyboard_release_manifest("episode-generic", conn=conn)
    assert manifest["completion_certificate_id"]
    assert manifest["narrative_review_artifact_id"] == ""

    if downgrade == "missing_projection":
        conn.execute(
            "UPDATE episodes SET screenplay_json=NULL WHERE id='episode-generic'"
        )
    else:
        projection = json.loads(conn.execute(
            "SELECT screenplay_json FROM episodes WHERE id='episode-generic'"
        ).fetchone()["screenplay_json"])
        projection["narrative_plan"] = None
        conn.execute(
            "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
            (json.dumps(projection, ensure_ascii=False),),
        )
    conn.commit()

    with pytest.raises(ValueError, match="权威|投影|漂移"):
        current_storyboard_release_manifest("episode-generic", conn=conn)


@pytest.mark.asyncio
async def test_narrative_enqueue_never_runs_legacy_in_place_repair(
    monkeypatch,
) -> None:
    await _published_narrative_case(monkeypatch)
    from app import api, worker

    def forbidden_repair(*_args, **_kwargs):
        raise AssertionError("narrative authority must not run legacy row repair")

    monkeypatch.setattr(worker, "_auto_repair_embedded_source_dialogue", forbidden_repair)
    monkeypatch.setattr(worker, "_auto_expand_source_dialogue_duration", forbidden_repair)
    monkeypatch.setattr(worker, "_auto_normalize_functional_speaker", forbidden_repair)
    monkeypatch.setattr(worker, "_begin_video_preflight_job", lambda *_a, **_k: "preflight")
    monkeypatch.setattr(
        api,
        "_review_upstream_snapshot",
        lambda *_a, **_k: {
            "eligible_for_production": True,
            "qualification_version": "authority:qualification",
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        worker,
        "_enqueue_shot_impl",
        lambda *_a, **_k: {"reused": False, "job_id": "job"},
    )

    result = worker.enqueue_shot("shot-row-1")

    assert result["job_id"] == "job"


@pytest.mark.asyncio
async def test_narrative_enqueue_rejects_free_text_prompt_override_before_job(
    monkeypatch,
) -> None:
    await _published_narrative_case(monkeypatch)
    from app import worker

    began_preflight = False

    def should_not_begin(*_args, **_kwargs):
        nonlocal began_preflight
        began_preflight = True
        raise AssertionError("prompt authority must fail before job creation")

    monkeypatch.setattr(worker, "_begin_video_preflight_job", should_not_begin)
    with pytest.raises(
        ValueError,
        match="NARRATIVE_PROMPT_OVERRIDE_REQUIRES_CANDIDATE",
    ):
        worker.enqueue_shot(
            "shot-row-1",
            prompt_override="Replace the reviewed story action with another one.",
        )
    assert began_preflight is False


@pytest.mark.asyncio
async def test_narrative_enqueue_cannot_default_when_video_plan_is_missing(
    monkeypatch,
) -> None:
    await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET status='confirmed' WHERE id='episode-generic'"
    )
    conn.commit()
    from app import worker
    from app.media_exec import enqueue as enqueue_module

    original_json_loads = json.loads
    monkeypatch.setattr(
        enqueue_module.json,
        "loads",
        lambda value, *args, **kwargs: (
            {
                "characters": [],
                "world": {"visual_style_canonical": "test style"},
                "scenes": [],
            }
            if value is None
            else original_json_loads(value, *args, **kwargs)
        ),
    )

    with pytest.raises(ValueError, match="VIDEO_PLAN_REQUIRED"):
        worker._enqueue_shot_impl("shot-row-1")


@pytest.mark.asyncio
async def test_candidate_observation_does_not_mutate_published_storyboard(
    monkeypatch,
) -> None:
    case = await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    before = dict(conn.execute(
        "SELECT observed_state_out,shot_contract_json FROM shots WHERE id='shot-row-1'"
    ).fetchone())
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,qa_json,created_at
           ) VALUES('observed-version','shot-row-1',1,'p','observed','succeeded','{}',0)"""
    )
    conn.commit()
    from app.evidence.media import persist_candidate_observed_state_out

    persist_candidate_observed_state_out(
        "observed-version",
        "candidate-only observed end state",
    )

    after = dict(conn.execute(
        "SELECT observed_state_out,shot_contract_json FROM shots WHERE id='shot-row-1'"
    ).fetchone())
    qa = json.loads(conn.execute(
        "SELECT qa_json FROM shot_versions WHERE id='observed-version'"
    ).fetchone()["qa_json"])
    assert after == before
    assert qa["observed_state_out"] == "candidate-only observed end state"
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    from app.production.certificate import (
        verify_current_storyboard_completion_authority,
    )

    verify_current_storyboard_completion_authority(
        episode=episode,
        current_storyboard_content=case["board"].model_dump(mode="json"),
    )


@pytest.mark.asyncio
async def test_display_episode_renumber_keeps_storyboard_authority_current(
    monkeypatch,
) -> None:
    case = await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET episode_no=7 WHERE id='episode-generic'"
    )
    conn.commit()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id='episode-generic' ORDER BY shot_no"
    ).fetchall()
    current_board = video_ops._board_from_shot_rows(rows, 7)
    from app.production.certificate import (
        verify_current_storyboard_completion_authority,
    )

    verify_current_storyboard_completion_authority(
        episode=episode,
        current_storyboard_content=current_board.model_dump(mode="json"),
    )
    assert case["board"].episode_no != current_board.episode_no


@pytest.mark.asyncio
async def test_worker_shot_loader_preserves_stable_uid_for_authority(
    monkeypatch,
) -> None:
    await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id='episode-generic' ORDER BY shot_no"
    ).fetchall()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    from app import worker
    from app.production.certificate import (
        verify_current_storyboard_completion_authority,
    )
    from app.schemas import Storyboard

    board = Storyboard(
        episode_no=int(episode["episode_no"]),
        shots=[worker._load_shot_model(row) for row in rows],
    )

    verify_current_storyboard_completion_authority(
        episode=episode,
        current_storyboard_content=board.model_dump(mode="json"),
    )


@pytest.mark.asyncio
async def test_confirmation_authority_failure_never_mutates_target_duration(
    monkeypatch,
) -> None:
    await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    projection = json.loads(conn.execute(
        "SELECT screenplay_json FROM episodes WHERE id='episode-generic'"
    ).fetchone()["screenplay_json"])
    projection["narrative_plan"] = None
    conn.execute(
        "UPDATE episodes SET target_duration_s=61,screenplay_json=? "
        "WHERE id='episode-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()
    from app import storyboard_workspace

    monkeypatch.setattr(storyboard_workspace, "require_preview", lambda *_a, **_k: None)
    monkeypatch.setattr(storyboard_workspace, "consume_preview", lambda *_a, **_k: None)

    with pytest.raises(ValueError, match="漂移|权威"):
        video_ops.confirm_episode_core(
            "episode-generic",
            preview_token="preview",
        )

    target = conn.execute(
        "SELECT target_duration_s FROM episodes WHERE id='episode-generic'"
    ).fetchone()["target_duration_s"]
    assert target == 61


@pytest.mark.asyncio
async def test_video_gate_ignores_stale_optional_review_behind_current_certificate(
    monkeypatch,
) -> None:
    case = await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET status='stale', stale_reason=? WHERE id=?",
        ("reviewed storyboard changed", case["report_artifact"]["id"]),
    )
    conn.commit()

    _assert_optional_review_does_not_revoke_authority(case)


@pytest.mark.asyncio
async def test_confirmed_idempotency_ignores_stale_optional_review(
    monkeypatch,
) -> None:
    case = await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET status='confirmed' WHERE id='episode-generic'"
    )
    conn.execute(
        "UPDATE artifacts SET status='stale', stale_reason=? WHERE id=?",
        ("reviewed storyboard changed", case["report_artifact"]["id"]),
    )
    conn.commit()

    result = video_ops.confirm_episode_core("episode-generic")
    assert result["confirmed"] is True
    assert result["idempotent"] is True


@pytest.mark.asyncio
async def test_video_gate_ignores_optional_review_pointer_lineage(
    monkeypatch,
) -> None:
    case = await _published_narrative_case(monkeypatch)
    unrelated_report = _artifact(
        artifact_type="narrative_review_report",
        content=case["report"].model_dump(mode="json"),
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET narrative_review_artifact_id=? WHERE id='episode-generic'",
        (unrelated_report["id"],),
    )
    conn.commit()

    _assert_optional_review_does_not_revoke_authority(case)


@pytest.mark.asyncio
async def test_video_gate_ignores_optional_review_input_lineage(
    monkeypatch,
) -> None:
    case = await _published_narrative_case(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET parent_artifact_ids_json=? WHERE id=?",
        (
            json.dumps([case["screenplay_artifact"]["id"]]),
            case["review_input"]["id"],
        ),
    )
    conn.commit()

    _assert_optional_review_does_not_revoke_authority(case)
