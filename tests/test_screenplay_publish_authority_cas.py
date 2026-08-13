from __future__ import annotations

import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.production.patch import screenplay_artifact_payload
from app.production.publish import publish_screenplay
from app.production.revision import ensure_production_revision, mark_baseline_generated
from app.production.screenplay_authority import (
    SCREENPLAY_QA_PROFILE_VERSION,
    resolve_current_screenplay_authority,
    screenplay_authority_fingerprint,
)
from tests.test_screenplay_authority import _source_projection_case


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-publish-cas.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _pending_publish(*, target_duration_s: int, planning_duration_s: int) -> dict:
    case = _source_projection_case()
    conn = db.get_conn()
    conn.execute(
        """UPDATE episodes
              SET target_duration_s=?,planning_target_duration_s=?,
                  target_duration_authority='planning_estimate'
            WHERE id=?""",
        (target_duration_s, planning_duration_s, case["episode_id"]),
    )
    conn.commit()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[case["merged_artifact_id"]],
        contract_version="4.0.0",
        model_snapshot={"compiler_version": "screenplay-ir-compiler.v6"},
    ))
    fingerprint = screenplay_authority_fingerprint(
        case["episode_id"],
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    revision = ensure_production_revision(
        episode_id=case["episode_id"],
        kind="screenplay",
        input_fingerprint=fingerprint,
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    evaluation = evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="score_only",
            runtime_blocking=False,
            score=100,
            evidence={
                "artifact_id": artifact["id"],
                "artifact_hash": artifact["content_hash"],
                "authority_input_fingerprint": fingerprint,
                "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
            },
        ),
    )
    return {
        **case,
        "artifact": artifact,
        "fingerprint": fingerprint,
        "revision_id": revision.id,
        "evaluation_id": evaluation["id"],
    }


def _publish(case: dict) -> dict:
    return publish_screenplay(
        episode_id=case["episode_id"],
        revision_id=case["revision_id"],
        artifact_id=case["artifact"]["id"],
        artifact_hash=case["artifact"]["content_hash"],
        evaluation_ids=[case["evaluation_id"]],
        input_fingerprint=case["fingerprint"],
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=True,
    )


def test_duration_expansion_bound_as_planning_input_survives_publish_cleanup() -> None:
    # run_21a expanded the generated duration before QA.  The planning input
    # must advance with it so downstream retirement cannot revert the signed
    # fingerprint while publishing.
    case = _pending_publish(target_duration_s=1801, planning_duration_s=1801)

    result = _publish(case)

    authority = resolve_current_screenplay_authority(case["episode_id"])
    row = db.get_conn().execute(
        "SELECT target_duration_s,planning_target_duration_s FROM episodes WHERE id=?",
        (case["episode_id"],),
    ).fetchone()
    assert tuple(row) == (1801, 1801)
    assert result["artifact_id"] == case["artifact"]["id"]
    assert authority.input_fingerprint == case["fingerprint"]


def test_publish_rolls_back_if_downstream_cleanup_changes_authority_fingerprint() -> None:
    # Exact production failure shape: QA signed the expanded target, but an old
    # planning value would be restored while retiring the storyboard.
    case = _pending_publish(target_duration_s=1801, planning_duration_s=1800)
    conn = db.get_conn()
    certificates_before = conn.execute(
        "SELECT COUNT(*) FROM completion_certificates WHERE scope_id=?",
        (case["episode_id"],),
    ).fetchone()[0]

    with pytest.raises(ValueError, match="发布事务中权威输入已变化"):
        _publish(case)

    episode = conn.execute(
        "SELECT target_duration_s,screenplay_artifact_id FROM episodes WHERE id=?",
        (case["episode_id"],),
    ).fetchone()
    revision = conn.execute(
        "SELECT status,published_artifact_id FROM production_revisions WHERE id=?",
        (case["revision_id"],),
    ).fetchone()
    assert tuple(episode) == (1801, None)
    assert tuple(revision) == ("active", None)
    assert conn.execute(
        "SELECT status FROM artifacts WHERE id=?", (case["artifact"]["id"],),
    ).fetchone()[0] == "candidate"
    assert conn.execute(
        "SELECT COUNT(*) FROM completion_certificates WHERE scope_id=?",
        (case["episode_id"],),
    ).fetchone()[0] == certificates_before


def test_publish_transaction_rejects_authority_cas_drift_and_rolls_back(
    monkeypatch,
) -> None:
    case = _pending_publish(target_duration_s=1801, planning_duration_s=1801)
    from app import storyboard_authority

    original = storyboard_authority.clear_storyboard_outline_authority

    def drift_after_cleanup(episode_id: str, *, conn=None, clear_outline=True):
        original(episode_id, conn=conn, clear_outline=clear_outline)
        conn.execute("UPDATE episodes SET title='concurrent drift' WHERE id=?", (episode_id,))

    monkeypatch.setattr(
        storyboard_authority,
        "clear_storyboard_outline_authority",
        drift_after_cleanup,
    )

    with pytest.raises(ValueError, match="发布事务中权威输入已变化"):
        _publish(case)

    conn = db.get_conn()
    assert conn.execute(
        "SELECT title FROM episodes WHERE id=?", (case["episode_id"],),
    ).fetchone()[0] == "Fixture"
    assert conn.execute(
        "SELECT COUNT(*) FROM completion_certificates WHERE scope_id=?",
        (case["episode_id"],),
    ).fetchone()[0] == 0


def test_successful_publish_replay_does_not_issue_duplicate_certificate() -> None:
    case = _pending_publish(target_duration_s=1801, planning_duration_s=1801)
    first = _publish(case)
    conn = db.get_conn()

    with pytest.raises(ValueError, match="production revision 已失效"):
        _publish(case)

    certificates = conn.execute(
        "SELECT id FROM completion_certificates WHERE scope_id=?",
        (case["episode_id"],),
    ).fetchall()
    assert [row["id"] for row in certificates] == [first["certificate_id"]]
    assert resolve_current_screenplay_authority(
        case["episode_id"],
    ).certificate_id == first["certificate_id"]
