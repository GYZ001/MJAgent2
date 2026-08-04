from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app import api, db, storyboard_workspace
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation
from app.narrative_review import NarrativeReviewError, run_blind_audience_review
from app.production.publish import publish_screenplay
from app.production.revision import ensure_production_revision, mark_baseline_generated
from app.production.screenplay_authority import (
    SCREENPLAY_QA_PROFILE_VERSION,
    resolve_current_screenplay_authority,
    screenplay_authority_fingerprint,
)
from tests.test_narrative_continuity import _board, _screenplay
from tests.test_narrative_review import _persist_review_projection


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-authority.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _published_case():
    screenplay = _screenplay()
    artifact, _shot_artifacts = _persist_review_projection(screenplay, _board())
    authority = resolve_current_screenplay_authority("episode-generic")
    return screenplay, artifact, authority


@pytest.mark.parametrize(
    "drift",
    [
        "published_pointer",
        "artifact_payload",
        "projection",
        "certificate",
        "revision",
        "evaluation",
        "evaluation_fingerprint",
        "source",
    ],
)
def test_resolver_fails_closed_on_every_authority_layer_drift(drift: str) -> None:
    screenplay, artifact, authority = _published_case()
    conn = db.get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone()

    if drift == "published_pointer":
        conn.execute(
            "UPDATE episodes SET published_screenplay_artifact_id='other' "
            "WHERE id='episode-generic'"
        )
    elif drift == "artifact_payload":
        changed = screenplay.model_copy(deep=True)
        changed.title = "Mutated after publication"
        # Even coordinated mutation of both JSON projections cannot preserve an
        # old Artifact hash/certificate.
        payload = changed.model_dump(mode="json")
        conn.execute(
            "UPDATE artifacts SET content_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), artifact["id"]),
        )
        conn.execute(
            "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
            (changed.model_dump_json(),),
        )
    elif drift == "projection":
        changed = screenplay.model_copy(deep=True)
        changed.title = "Projection only drift"
        conn.execute(
            "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
            (changed.model_dump_json(),),
        )
    elif drift == "certificate":
        conn.execute(
            "UPDATE completion_certificates SET input_fingerprint='drift' WHERE id=?",
            (authority.certificate_id,),
        )
    elif drift == "revision":
        conn.execute(
            "UPDATE production_revisions SET status='superseded' WHERE id=?",
            (episode["screenplay_production_revision_id"],),
        )
    elif drift == "evaluation":
        conn.execute(
            "UPDATE evaluations SET status='failed',hard_gate_passed=0 "
            "WHERE artifact_id=? AND evaluator_name='screenplay_production_qa'",
            (artifact["id"],),
        )
    elif drift == "evaluation_fingerprint":
        conn.execute(
            "UPDATE evaluations SET evidence_json='{}' "
            "WHERE artifact_id=? AND evaluator_name='screenplay_production_qa'",
            (artifact["id"],),
        )
    elif drift == "source":
        conn.execute(
                """UPDATE chapters SET title=?,content=?
                    WHERE project_id=? AND idx=?""",
                ("Changed source", "New source authority.", "project-generic", 1),
        )
        conn.execute(
            "UPDATE episodes SET source_chapters='[1]' WHERE id='episode-generic'"
        )
    conn.commit()

    with pytest.raises(ValueError):
        resolve_current_screenplay_authority("episode-generic")


@pytest.mark.asyncio
async def test_blind_review_rejects_supplied_screenplay_drift_before_model_use() -> None:
    screenplay, artifact, _authority = _published_case()
    supplied = screenplay.model_copy(deep=True)
    supplied.title = "Caller-side mutable draft"

    with pytest.raises(NarrativeReviewError, match="REVIEW_INPUT_SCREENPLAY_DRIFT"):
        await run_blind_audience_review(
            episode_id="episode-generic",
            screenplay=supplied,
            board=_board(),
            screenplay_artifact_id=artifact["id"],
        )


def test_modern_published_plan_null_can_resolve_without_narrative_downgrade() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan = None
    artifact, _shot_artifacts = _persist_review_projection(screenplay, _board())
    contract_version = "screenplay-legacy-published.v1"
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET contract_version=? WHERE id=?",
        (contract_version, artifact["id"]),
    )
    conn.commit()
    input_fingerprint = screenplay_authority_fingerprint(
        "episode-generic",
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    revision = ensure_production_revision(
        episode_id="episode-generic",
        kind="screenplay",
        input_fingerprint=input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    qa_gate = evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            score=100,
            evidence={"authority_input_fingerprint": input_fingerprint},
        ),
    )
    publish_screenplay(
        episode_id="episode-generic",
        revision_id=revision.id,
        artifact_id=artifact["id"],
        artifact_hash=artifact["content_hash"],
        evaluation_ids=[qa_gate["id"]],
        input_fingerprint=input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=False,
    )

    resolved = resolve_current_screenplay_authority(
        "episode-generic",
        require_narrative=False,
    )
    assert resolved.screenplay.narrative_plan is None
    with pytest.raises(ValueError, match="缺少叙事权威合同"):
        resolve_current_screenplay_authority("episode-generic", require_narrative=True)


def test_common_readiness_rejects_modern_projection_plan_downgrade() -> None:
    screenplay, _artifact, _authority = _published_case()
    downgraded = screenplay.model_copy(deep=True)
    downgraded.narrative_plan = None
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
        (downgraded.model_dump_json(),),
    )
    conn.commit()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone()

    # Import through the composed domain namespace used by production routes.
    from app.domain import common

    assert common._screenplay_ready(episode) is False


@pytest.mark.parametrize("drift", ["projection_downgrade", "artifact_pointer"])
def test_storyboard_structure_rejects_authority_drift_before_preview(
    drift: str,
) -> None:
    screenplay, _artifact, _authority = _published_case()
    conn = db.get_conn()
    if drift == "projection_downgrade":
        downgraded = screenplay.model_copy(deep=True)
        downgraded.narrative_plan = None
        conn.execute(
            "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
            (downgraded.model_dump_json(),),
        )
    else:
        conn.execute(
            "UPDATE episodes SET screenplay_artifact_id='unpublished-draft' "
            "WHERE id='episode-generic'"
        )
    conn.commit()

    with pytest.raises(HTTPException) as caught:
        api.preview_storyboard_structure("episode-generic", {
            "operation": "duplicate_after",
            "shot_id": "shot-row-1",
            "target_index": 0,
        })

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "storyboard_screenplay_authority_invalid"
    assert conn.execute(
        "SELECT COUNT(*) FROM shots WHERE episode_id='episode-generic'"
    ).fetchone()[0] == len(_board().shots)


def test_narrative_semantic_edit_requires_candidate_release_pipeline() -> None:
    _published_case()
    session = storyboard_workspace.create_edit_session("shot-row-1")

    with pytest.raises(HTTPException) as caught:
        api.preview_shot_edit_impact("shot-row-1", {
            "edit_session_token": session["edit_session_token"],
            "changes": {"primary_action": "A different semantic action."},
        })

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "narrative_semantic_repair_required"
    action = caught.value.detail["action"]
    for stage in ("candidate", "全板叙事验证", "冷观众盲审", "原子发布"):
        assert stage in action


def test_narrative_shot_id_migration_is_dry_run_only() -> None:
    _published_case()
    conn = db.get_conn()
    row = conn.execute(
        "SELECT shot_contract_json FROM shots WHERE id='shot-row-1'"
    ).fetchone()
    contract = json.loads(row["shot_contract_json"] or "{}")
    contract["story_event_id"] = "S01"
    conn.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id='shot-row-1'",
        (json.dumps(contract, ensure_ascii=False),),
    )
    conn.commit()
    before = conn.execute(
        "SELECT shot_contract_json FROM shots WHERE id='shot-row-1'"
    ).fetchone()[0]

    preview = api.migrate_episode_shot_ids(
        "episode-generic", {"dry_run": True},
    )
    assert preview["changed"]
    with pytest.raises(HTTPException) as caught:
        api.migrate_episode_shot_ids("episode-generic", {"dry_run": False})

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "narrative_semantic_repair_required"
    assert conn.execute(
        "SELECT shot_contract_json FROM shots WHERE id='shot-row-1'"
    ).fetchone()[0] == before
