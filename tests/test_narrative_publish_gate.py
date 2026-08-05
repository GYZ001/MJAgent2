from __future__ import annotations

import json

import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.narrative_review import COMPARATOR_PROMPT_VERSION
from app.narrative_calibration import (
    GLOBAL_CALIBRATION_SCOPE_ID,
    HUMAN_CALIBRATION_CONTRACT_VERSION,
    CalibrationReport,
    HumanOneWatchFreeze,
    HumanOneWatchObservation,
)
from app.production.certificate import (
    consume_completion_certificate,
    issue_completion_certificate,
    verify_completion_certificate,
)
from app.production.patch import screenplay_artifact_payload
from app.production.publish import publish_storyboard
from app.production.revision import (
    ensure_production_revision,
    get_production_revision,
    mark_baseline_generated,
    set_published_artifact,
    update_working_artifact,
)
from app.production.screenplay_authority import (
    SCREENPLAY_QA_PROFILE_VERSION,
    screenplay_authority_fingerprint,
)
from app.narrative_review import run_blind_audience_review
from tests.test_narrative_continuity import _board, _screenplay
from tests.test_narrative_review import _observation, _persist_review_projection


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "narrative-publish.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _artifact(
    *,
    artifact_type: str,
    content: dict,
    parent_artifact_ids: list[str] | None = None,
    contract_version: str | None = None,
) -> dict:
    return evidence_repository.create_artifact(
        EvidenceArtifact(
            type=artifact_type,
            scope_type="episode",
            scope_id="episode-generic",
            status="validated",
            trust_level="T2",
            content=content,
            parent_artifact_ids=list(parent_artifact_ids or []),
            contract_version=contract_version,
        )
    )


def _runtime_gate(
    artifact_id: str,
    *,
    evaluator_name: str,
    evaluator_version: str = "narrative-continuity.v1",
) -> dict:
    return evidence_repository.create_evaluation(
        artifact_id,
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name=evaluator_name,
            evaluator_version=evaluator_version,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            score=100,
        ),
    )


def _install_passing_review_model(monkeypatch) -> None:
    async def fake_chat(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if kwargs["call_meta"]["call_role"] == "blind_reader_first_pass":
            return json.dumps(_observation(payload), ensure_ascii=False)
        if kwargs["call_meta"]["call_role"] == "blind_reader_neutral_followup":
            contract = payload["output_contract"]
            return json.dumps(
                {
                    **contract,
                    "neutral_followup_observations": [],
                    "supporting_evidence_ids": ["EV-1"],
                },
                ensure_ascii=False,
            )
        contract = payload["output_contract"]
        return json.dumps(
            {
                **contract,
                "target_delta_results": [
                    {
                        **item,
                        "result": "satisfied",
                        "predicted_score": 0.95,
                        "supporting_evidence_ids": ["EV-1"],
                        "reason": "The same-prior frozen recall registered the evidence.",
                    }
                    for item in contract["target_delta_results"]
                ],
                "low_percentile_result": {"passed": True, "rate": 1.0},
                "decision": "pass",
                "reason": "All prior paths passed.",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.narrative_review.model_gateway.chat", fake_chat)


def _install_global_calibration(review_artifact_id: str) -> dict:
    secondary_review = evidence_repository.create_artifact(EvidenceArtifact(
        type="narrative_review_report",
        scope_type="episode",
        scope_id="episode-generic-2",
        status="validated",
        trust_level="T2",
        content={"decision": "revise"},
    ))
    observation_artifacts = []
    rows = (
        (
            "episode-generic", review_artifact_id,
            "AP-cold", "XD-cold", 0.95, "genre-a", "form-a",
        ),
        (
            "episode-generic", review_artifact_id,
            "AP-context", "XD-context", 0.95, "genre-a", "form-a",
        ),
        (
            "episode-generic-2", secondary_review["id"],
            "AP-cold", "XD-cold", 0.2, "genre-b", "form-b",
        ),
        (
            "episode-generic-2", secondary_review["id"],
            "AP-context", "XD-context", 0.4, "genre-b", "form-b",
        ),
    )
    for ordinal, (
        scope_id, bound_review_id, prior_id, target_id, score, genre, form,
    ) in enumerate(rows, start=1):
        observation_id = f"human-{ordinal}"
        freeze = HumanOneWatchFreeze(
            observation_id=observation_id,
            participant_id_hash=f"participant-{ordinal}",
            scope_id=scope_id,
            audience_prior_id=prior_id,
            narrative_review_artifact_id=bound_review_id,
            watched_once=True,
            spontaneous_recall_frozen=True,
            spontaneous_recall={"free_text": "frozen test recall"},
            content_dimensions={"genre": genre, "form": form},
        )
        freeze_artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="human_one_watch_spontaneous_recall",
            scope_type="episode",
            scope_id=scope_id,
            status="validated",
            trust_level="T4",
            content=freeze.model_dump(mode="json"),
            parent_artifact_ids=[bound_review_id],
            contract_version="human-one-watch.v1",
        ))
        evidence_repository.create_evaluation(
            freeze_artifact["id"],
            Evaluation(
                evaluator_type="human",
                evaluator_name="one_watch_freeze_gate",
                evaluator_version="human-one-watch.v1",
                status="passed",
                hard_gate_passed=True,
                evaluation_role="runtime_gate",
                runtime_blocking=True,
            ),
        )
        observation = HumanOneWatchObservation(
            **freeze.model_dump(mode="json"),
            target_delta_observations=[{
                "audience_prior_id": prior_id,
                "target_delta_id": target_id,
                "observed_score": score,
            }],
        )
        observation_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="human_one_watch_observation",
                scope_type="episode",
                scope_id=scope_id,
                status="validated",
                trust_level="T4",
                content=observation.model_dump(mode="json"),
                parent_artifact_ids=[
                    bound_review_id,
                    freeze_artifact["id"],
                ],
                contract_version="human-one-watch.v1",
            )
        )
        evidence_repository.create_evaluation(
            observation_artifact["id"],
            Evaluation(
                evaluator_type="human",
                evaluator_name="one_watch_protocol",
                evaluator_version="human-one-watch.v1",
                status="passed",
                hard_gate_passed=True,
                evaluation_role="score_only",
                score_status="observation_only",
            ),
        )
        observation_artifacts.append(observation_artifact)
    report = CalibrationReport(
        calibration_report_id="CAL-TEST-AUTHORITY",
        calibration_scope_id=GLOBAL_CALIBRATION_SCOPE_ID,
        scope_ids=["episode-generic", "episode-generic-2"],
        observation_ids=[
            artifact["content"]["observation_id"]
            for artifact in observation_artifacts
        ],
        narrative_review_artifact_ids=[
            review_artifact_id,
            secondary_review["id"],
        ],
        required_dimension_axes=["genre", "form"],
        sample_summary={
            "observation_count": 4,
            "participant_count": 4,
            "expected_pair_count": 4,
            "paired_target_count": 4,
            "scope_count": 2,
        },
        pair_results=[
            {
                "scope_id": scope_id,
                "audience_prior_id": prior_id,
                "target_delta_id": target_id,
                "human_sample_count": 1,
                "human_mean_score": score,
                "model_predicted_score": score,
                "absolute_error": 0.0,
                "status": "paired",
            }
            for (
                scope_id, _review_id, prior_id, target_id, score, _genre, _form,
            ) in rows
        ],
        calibration_score=1.0,
        minimum_correlation=0.6,
        human_success_threshold=0.8,
        recommended_model_pass_threshold=0.8,
        threshold_analysis={"selected": {"balanced_accuracy": 1.0}},
        confidence_status="supported",
        decision="calibrated",
        reason="test-only cross-content authority fixture",
    )
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="human_one_watch_calibration_report",
        scope_type="calibration",
        scope_id=GLOBAL_CALIBRATION_SCOPE_ID,
        status="candidate",
        trust_level="T4",
        content=report.model_dump(mode="json"),
        parent_artifact_ids=[
            review_artifact_id,
            secondary_review["id"],
            *[artifact["id"] for artifact in observation_artifacts],
        ],
        contract_version=HUMAN_CALIBRATION_CONTRACT_VERSION,
    ))
    return evidence_repository.commit_artifact(
        None,
        artifact["id"],
        [Evaluation(
            evaluator_type="deterministic",
            evaluator_name="human_one_watch_calibration",
            evaluator_version=HUMAN_CALIBRATION_CONTRACT_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            score_status="calibrated",
            runtime_blocking=True,
            score=100,
            evidence={"model_pass_threshold": 0.8},
        )],
    )


async def _reviewed_publish_candidate(monkeypatch) -> dict:
    screenplay = _screenplay()
    board = _board()
    board.shots[-1].is_final = True
    screenplay_artifact, shot_artifacts = _persist_review_projection(screenplay, board)
    screenplay_contract = get_contract("screenplay").version
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET contract_version=? WHERE id=?",
        (screenplay_contract, screenplay_artifact["id"]),
    )
    screenplay_fingerprint = screenplay_authority_fingerprint(
        "episode-generic",
        contract_version=screenplay_contract,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    screenplay_gate = _runtime_gate(
        screenplay_artifact["id"],
        evaluator_name="screenplay_production_qa",
        evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    conn.execute(
        "UPDATE evaluations SET evidence_json=? WHERE id=?",
        (
            json.dumps({
                "authority_input_fingerprint": screenplay_fingerprint,
            }),
            screenplay_gate["id"],
        ),
    )
    screenplay_revision = ensure_production_revision(
        episode_id="episode-generic",
        kind="screenplay",
        input_fingerprint=screenplay_fingerprint,
        contract_version=screenplay_contract,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    mark_baseline_generated(
        screenplay_revision.id,
        baseline_artifact_id=screenplay_artifact["id"],
        working_artifact_id=screenplay_artifact["id"],
    )
    screenplay_certificate = issue_completion_certificate(
        kind="screenplay",
        scope_id="episode-generic",
        artifact_id=screenplay_artifact["id"],
        artifact_hash=screenplay_artifact["content_hash"],
        input_fingerprint=screenplay_fingerprint,
        contract_version=screenplay_contract,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        evaluation_ids=[screenplay_gate["id"]],
        production_revision_id=screenplay_revision.id,
    )
    consume_completion_certificate(screenplay_certificate.certificate_id)
    set_published_artifact(
        screenplay_revision.id,
        screenplay_artifact["id"],
        certificate_id=screenplay_certificate.certificate_id,
    )
    conn.execute(
        """UPDATE episodes
              SET published_screenplay_artifact_id=?,
                  screenplay_production_revision_id=?,
                  screenplay_completion_certificate_id=?
            WHERE id='episode-generic'""",
        (
            screenplay_artifact["id"],
            screenplay_revision.id,
            screenplay_certificate.certificate_id,
        ),
    )
    conn.commit()
    _install_passing_review_model(monkeypatch)
    _observations, report, review_artifact_ids = await run_blind_audience_review(
        episode_id="episode-generic",
        screenplay=screenplay,
        board=board,
        screenplay_artifact_id=screenplay_artifact["id"],
    )
    report_artifact_id = next(
        artifact_id
        for artifact_id in review_artifact_ids
        if evidence_repository.get_artifact(artifact_id)["type"]
        == "narrative_review_report"
    )
    calibration_artifact = _install_global_calibration(report_artifact_id)
    storyboard_contract = get_contract("storyboard").version
    working_candidate = _artifact(
        artifact_type="storyboard_document",
        content=board.model_dump(mode="json"),
        parent_artifact_ids=[
            *review_artifact_ids,
            calibration_artifact["id"],
        ],
        contract_version=storyboard_contract,
    )
    revision = ensure_production_revision(
        episode_id="episode-generic",
        kind="storyboard",
        contract_version=storyboard_contract,
        qa_profile_version="storyboard-full-gate-2",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=working_candidate["id"],
        working_artifact_id=working_candidate["id"],
    )
    exact_review = _runtime_gate(
        working_candidate["id"],
        evaluator_name="narrative_blind_comparator",
        evaluator_version=COMPARATOR_PROMPT_VERSION,
    )
    full_gate = _runtime_gate(
        working_candidate["id"],
        evaluator_name="storyboard_full_gate",
        evaluator_version=storyboard_contract,
    )
    artifacts = {
        artifact["type"]: artifact
        for artifact_id in review_artifact_ids
        if (artifact := evidence_repository.get_artifact(artifact_id)) is not None
    }
    observation_artifacts = [
        artifact
        for artifact_id in review_artifact_ids
        if (artifact := evidence_repository.get_artifact(artifact_id)) is not None
        and artifact["type"] == "blind_audience_observation"
    ]
    first_pass_artifacts = [
        artifact
        for artifact_id in review_artifact_ids
        if (artifact := evidence_repository.get_artifact(artifact_id)) is not None
        and artifact["type"] == "blind_audience_spontaneous_recall"
    ]
    return {
        "screenplay": screenplay,
        "screenplay_artifact": screenplay_artifact,
        "shot_artifacts": shot_artifacts,
        "board": board,
        "report": report,
        "review_artifact_ids": review_artifact_ids,
        "calibration_artifact": calibration_artifact,
        "review_input": artifacts["storyboard_review_input"],
        "observation_artifacts": observation_artifacts,
        "first_pass_artifacts": first_pass_artifacts,
        "report_artifact": artifacts["narrative_review_report"],
        "working_candidate": working_candidate,
        "revision": revision,
        "exact_review": exact_review,
        "full_gate": full_gate,
        "evaluation_ids": [exact_review["id"], full_gate["id"]],
        "publish_kwargs": {
            "episode_id": "episode-generic",
            "revision_id": revision.id,
            "artifact_id": working_candidate["id"],
            "artifact_hash": working_candidate["content_hash"],
            "shots_payload": [shot.model_dump(mode="json") for shot in board.shots],
        },
    }


def _assert_storyboard_not_published(revision_id: str) -> None:
    rejected_revision = get_production_revision(revision_id)
    rejected_episode = (
        db.get_conn().execute("SELECT storyboard_artifact_id FROM episodes WHERE id='episode-generic'").fetchone()
    )
    assert rejected_revision is not None
    assert rejected_revision.published_artifact_id is None
    assert rejected_episode["storyboard_artifact_id"] is None


def _bind_screenplay_revision(
    artifact: dict,
    *,
    contract_version: str,
    qa_profile_version: str,
):
    revision = ensure_production_revision(
        episode_id="episode-generic",
        kind="screenplay",
        input_fingerprint="screenplay-input-v1",
        contract_version=contract_version,
        qa_profile_version=qa_profile_version,
        resume=False,
    )
    return mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )


def test_screenplay_certificate_requires_gate_from_the_exact_artifact() -> None:
    screenplay_contract = get_contract("screenplay").version
    qa_profile = "screenplay-qa-gate-2"
    first = _screenplay()
    second = _screenplay()
    second.title = "A different working candidate"
    first_artifact = _artifact(
        artifact_type="screenplay_document",
        content=screenplay_artifact_payload(first),
        contract_version=screenplay_contract,
    )
    second_artifact = _artifact(
        artifact_type="screenplay_document",
        content=screenplay_artifact_payload(second),
        contract_version=screenplay_contract,
    )
    revision = _bind_screenplay_revision(
        second_artifact,
        contract_version=screenplay_contract,
        qa_profile_version=qa_profile,
    )
    wrong_evaluation = _runtime_gate(
        first_artifact["id"],
        evaluator_name="screenplay_production_qa",
        evaluator_version=qa_profile,
    )

    with pytest.raises(ValueError, match="其他 Artifact"):
        issue_completion_certificate(
            kind="screenplay",
            scope_id="episode-generic",
            artifact_id=second_artifact["id"],
            artifact_hash=second_artifact["content_hash"],
            input_fingerprint=revision.input_fingerprint,
            contract_version=screenplay_contract,
            qa_profile_version=qa_profile,
            evaluation_ids=[wrong_evaluation["id"]],
            production_revision_id=revision.id,
        )

    exact_evaluation = _runtime_gate(
        second_artifact["id"],
        evaluator_name="screenplay_production_qa",
        evaluator_version=qa_profile,
    )
    certificate = issue_completion_certificate(
        kind="screenplay",
        scope_id="episode-generic",
        artifact_id=second_artifact["id"],
        artifact_hash=second_artifact["content_hash"],
        input_fingerprint=revision.input_fingerprint,
        contract_version=screenplay_contract,
        qa_profile_version=qa_profile,
        evaluation_ids=[exact_evaluation["id"]],
        production_revision_id=revision.id,
    )

    assert certificate.artifact_id == second_artifact["id"]
    assert certificate.evaluation_ids == [exact_evaluation["id"]]

    db.get_conn().execute(
        "UPDATE evaluations SET evaluator_version='obsolete-qa-contract' WHERE id=?",
        (exact_evaluation["id"],),
    )
    db.get_conn().commit()
    with pytest.raises(ValueError, match="evaluator_version"):
        verify_completion_certificate(certificate)


def test_narrative_certificate_accepts_exact_score_only_evaluation() -> None:
    screenplay_contract = get_contract("screenplay").version
    qa_profile = "screenplay-qa-gate-2"
    artifact = _artifact(
        artifact_type="screenplay_document",
        content=_screenplay().model_dump(mode="json"),
        contract_version=screenplay_contract,
    )
    revision = _bind_screenplay_revision(
        artifact,
        contract_version=screenplay_contract,
        qa_profile_version=qa_profile,
    )
    diagnostic = evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=qa_profile,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="score_only",
            runtime_blocking=False,
            score=100,
        ),
    )

    certificate = issue_completion_certificate(
        kind="screenplay",
        scope_id="episode-generic",
        artifact_id=artifact["id"],
        artifact_hash=artifact["content_hash"],
        input_fingerprint=revision.input_fingerprint,
        contract_version=screenplay_contract,
        qa_profile_version=qa_profile,
        evaluation_ids=[diagnostic["id"]],
        production_revision_id=revision.id,
    )

    assert certificate.evaluation_ids == [diagnostic["id"]]


def test_narrative_certificate_rejects_evaluator_contract_drift() -> None:
    screenplay_contract = get_contract("screenplay").version
    qa_profile = "screenplay-qa-gate-2"
    artifact = _artifact(
        artifact_type="screenplay_document",
        content=_screenplay().model_dump(mode="json"),
        contract_version=screenplay_contract,
    )
    revision = _bind_screenplay_revision(
        artifact,
        contract_version=screenplay_contract,
        qa_profile_version=qa_profile,
    )
    stale_gate = _runtime_gate(
        artifact["id"],
        evaluator_name="screenplay_production_qa",
        evaluator_version="screenplay-qa-obsolete",
    )

    with pytest.raises(ValueError, match="evaluator_version"):
        issue_completion_certificate(
            kind="screenplay",
            scope_id="episode-generic",
            artifact_id=artifact["id"],
            artifact_hash=artifact["content_hash"],
            input_fingerprint=revision.input_fingerprint,
            contract_version=screenplay_contract,
            qa_profile_version=qa_profile,
            evaluation_ids=[stale_gate["id"]],
            production_revision_id=revision.id,
        )


@pytest.mark.asyncio
async def test_storyboard_publish_rejects_review_gate_from_another_candidate(
    monkeypatch,
) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)

    reviewed_candidate = _artifact(
        artifact_type="storyboard_document",
        content={"candidate": "reviewed-but-not-working"},
    )
    wrong_review = _runtime_gate(reviewed_candidate["id"], evaluator_name="narrative_blind_comparator")

    with pytest.raises(ValueError, match="冷观众多先验审读未通过或已失效"):
        publish_storyboard(
            **case["publish_kwargs"],
            evaluation_ids=[wrong_review["id"]],
        )

    _assert_storyboard_not_published(case["revision"].id)

    result = publish_storyboard(
        **case["publish_kwargs"],
        evaluation_ids=case["evaluation_ids"],
    )

    published_revision = get_production_revision(case["revision"].id)
    assert result["artifact_id"] == case["working_candidate"]["id"]
    assert published_revision is not None
    assert published_revision.published_artifact_id == case["working_candidate"]["id"]


@pytest.mark.asyncio
async def test_storyboard_publish_rejects_stale_review_report(monkeypatch) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET status='stale', stale_reason=? WHERE id=?",
        ("reviewed storyboard changed", case["report_artifact"]["id"]),
    )
    conn.commit()

    with pytest.raises(ValueError, match="NARRATIVE_REVIEW_REPORT_INVALID"):
        publish_storyboard(
            **case["publish_kwargs"],
            evaluation_ids=[case["exact_review"]["id"]],
        )

    _assert_storyboard_not_published(case["revision"].id)


@pytest.mark.asyncio
async def test_storyboard_publish_rejects_pass_json_without_report_gate(
    monkeypatch,
) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "DELETE FROM evaluations WHERE artifact_id=? AND evaluator_name='narrative_blind_comparator'",
        (case["report_artifact"]["id"],),
    )
    conn.commit()

    with pytest.raises(ValueError, match="NARRATIVE_REVIEW_GATE_MISSING"):
        publish_storyboard(
            **case["publish_kwargs"],
            evaluation_ids=[case["exact_review"]["id"]],
        )

    _assert_storyboard_not_published(case["revision"].id)


@pytest.mark.asyncio
async def test_storyboard_publish_rejects_review_input_without_current_shot_lineage(
    monkeypatch,
) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET parent_artifact_ids_json=? WHERE id=?",
        (
            json.dumps([case["screenplay_artifact"]["id"]]),
            case["review_input"]["id"],
        ),
    )
    conn.commit()

    with pytest.raises(ValueError, match="NARRATIVE_REVIEW_LINEAGE_DRIFT"):
        publish_storyboard(
            **case["publish_kwargs"],
            evaluation_ids=[case["exact_review"]["id"]],
        )

    _assert_storyboard_not_published(case["revision"].id)


@pytest.mark.asyncio
async def test_storyboard_publish_rejects_observation_without_review_input_lineage(
    monkeypatch,
) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)
    observation = case["observation_artifacts"][0]
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET parent_artifact_ids_json='[]' WHERE id=?",
        (observation["id"],),
    )
    conn.commit()

    with pytest.raises(ValueError, match="NARRATIVE_REVIEW_OBSERVATION_LINEAGE_INVALID"):
        publish_storyboard(
            **case["publish_kwargs"],
            evaluation_ids=[case["exact_review"]["id"]],
        )

    _assert_storyboard_not_published(case["revision"].id)


@pytest.mark.asyncio
async def test_storyboard_publish_rejects_observation_without_isolation_gate(
    monkeypatch,
) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)
    first_pass = case["first_pass_artifacts"][0]
    conn = db.get_conn()
    conn.execute(
        "DELETE FROM evaluations WHERE artifact_id=? AND evaluator_name='blind_review_isolation_gate'",
        (first_pass["id"],),
    )
    conn.commit()

    with pytest.raises(ValueError, match="NARRATIVE_REVIEW_ISOLATION_GATE_MISSING"):
        publish_storyboard(
            **case["publish_kwargs"],
            evaluation_ids=[case["exact_review"]["id"]],
        )

    _assert_storyboard_not_published(case["revision"].id)


@pytest.mark.asyncio
async def test_storyboard_publish_rejects_payload_from_another_working_artifact(
    monkeypatch,
) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)
    wrong_content = case["board"].model_dump(mode="json")
    wrong_content["shots"][0]["action_desc"] = "A different working candidate."
    wrong_candidate = _artifact(
        artifact_type="storyboard_document",
        content=wrong_content,
        parent_artifact_ids=case["review_artifact_ids"],
    )
    update_working_artifact(case["revision"].id, wrong_candidate["id"])
    wrong_candidate_gate = _runtime_gate(wrong_candidate["id"], evaluator_name="narrative_blind_comparator")

    with pytest.raises(ValueError, match="Artifact 内容不一致"):
        publish_storyboard(
            **{
                **case["publish_kwargs"],
                "artifact_id": wrong_candidate["id"],
                "artifact_hash": wrong_candidate["content_hash"],
            },
            evaluation_ids=[wrong_candidate_gate["id"]],
        )

    _assert_storyboard_not_published(case["revision"].id)
