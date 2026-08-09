from __future__ import annotations

import json
from pathlib import Path
import threading

from pydantic import ValidationError
import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.narrative_calibration import (
    CalibrationContractError,
    HumanOneWatchFreeze,
    HumanOneWatchObservation,
    HumanTargetDeltaObservation,
    ModelTargetEstimate,
    assert_report_meets_current_calibration,
    build_calibration_report,
    persist_ai_one_watch_simulation_authority,
    persist_calibration_report,
    persist_human_one_watch_freeze,
    persist_human_one_watch_observation,
    require_current_calibration_authority,
    validate_human_one_watch_observation,
)
from app.schemas import EpisodeScreenplay, NarrativeReviewReport
from app.stages import _narrative_plan_schema_example


def _screenplay(scope_id: str) -> EpisodeScreenplay:
    return EpisodeScreenplay(
        id=f"script-{scope_id}",
        episode_no=1,
        title=scope_id,
        narrative_plan=json.loads(_narrative_plan_schema_example(scope_id)),
    )


def _delta_id(prior_id: str) -> str:
    return "XD-AP1-1" if prior_id == "AP-1" else "XD-AP2-1"


def _observation(
    scope_id: str,
    prior_id: str,
    score: float,
    *,
    review_artifact_id: str | None = None,
    dimensions: dict | None = None,
    ordinal: int = 1,
) -> HumanOneWatchObservation:
    return HumanOneWatchObservation(
        observation_id=f"H-{scope_id}-{prior_id}-{ordinal}",
        participant_id_hash=f"participant-{scope_id}-{prior_id}-{ordinal}",
        scope_id=scope_id,
        audience_prior_id=prior_id,
        narrative_review_artifact_id=review_artifact_id or f"review-{scope_id}",
        watched_once=True,
        watch_count=1,
        replay_or_seek_used=False,
        source_material_seen=False,
        target_answers_seen=False,
        director_intent_seen=False,
        spontaneous_recall_frozen=True,
        spontaneous_recall={"free_text": "只记录一次观看后自然想起的内容"},
        target_delta_observations=[HumanTargetDeltaObservation(
            audience_prior_id=prior_id,
            target_delta_id=_delta_id(prior_id),
            observed_score=score,
            observed_interpretation={"free_semantics": "人类观察的开放语义"},
        )],
        content_dimensions=dimensions or {"genre": "g1", "form": "f1"},
    )


def _estimate(scope_id: str, prior_id: str, score: float) -> ModelTargetEstimate:
    return ModelTargetEstimate(
        scope_id=scope_id,
        audience_prior_id=prior_id,
        target_delta_id=_delta_id(prior_id),
        predicted_score=score,
        narrative_review_artifact_id=f"review-{scope_id}",
    )


def _persist_freeze(
    observation: HumanOneWatchObservation,
    screenplay: EpisodeScreenplay,
    review_artifact_id: str,
) -> dict:
    freeze = HumanOneWatchFreeze.model_validate(
        observation.model_dump(
            mode="json",
            exclude={"neutral_followup_observations", "target_delta_observations"},
        )
    )
    return persist_human_one_watch_freeze(
        freeze,
        screenplay=screenplay,
        narrative_review_artifact_ids=[review_artifact_id],
    )


@pytest.fixture()
def calibration_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "narrative-calibration.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    yield db.get_conn()


@pytest.mark.parametrize(
    "overrides",
    [
        {"watched_once": False},
        {"watch_count": 2},
        {"replay_or_seek_used": True},
        {"source_material_seen": True},
        {"target_answers_seen": True},
        {"director_intent_seen": True},
        {"spontaneous_recall_frozen": False},
    ],
)
def test_human_observation_strictly_enforces_blind_single_watch(overrides) -> None:
    payload = _observation("scope-1", "AP-1", 0.5).model_dump(mode="json")
    payload.update(overrides)

    with pytest.raises(ValidationError):
        HumanOneWatchObservation.model_validate(payload)


def test_observation_must_cover_exact_prior_target_pairs() -> None:
    screenplay = _screenplay("scope-1")
    observation = _observation("scope-1", "AP-1", 0.5)
    observation.target_delta_observations[0].target_delta_id = "XD-NOT-IN-PLAN"

    errors = validate_human_one_watch_observation(observation, screenplay)

    assert any("HUMAN_PAIR_MISSING" in item for item in errors)
    assert any("HUMAN_PAIR_UNKNOWN" in item for item in errors)


def test_sparse_constant_cross_content_sample_stays_needs_review() -> None:
    screenplay = _screenplay("scope-1")
    observations = [
        _observation("scope-1", "AP-1", 1.0),
        _observation("scope-1", "AP-2", 1.0),
    ]
    estimates = [
        _estimate("scope-1", "AP-1", 1.0),
        _estimate("scope-1", "AP-2", 1.0),
    ]

    report = build_calibration_report(
        calibration_report_id="CAL-1",
        calibration_scope_id="corpus-1",
        screenplays=[screenplay],
        observations=observations,
        model_estimates=estimates,
    )

    assert report.decision == "needs_review"
    assert report.calibration_score is None
    assert report.confidence_status == "needs_review"
    assert any("CROSS_DIMENSION_SAMPLE_INSUFFICIENT" in item for item in report.coverage_gaps)
    assert any("CROSS_SCOPE_SAMPLE_INSUFFICIENT" in item for item in report.coverage_gaps)
    assert any("HUMAN_PAIR_REPLICATION_INSUFFICIENT" in item for item in report.coverage_gaps)
    assert any("MODEL_SCORES_CONSTANT" in item for item in report.stability_issues)
    assert any("HUMAN_SCORES_CONSTANT" in item for item in report.stability_issues)


def test_cross_dimension_stable_nonconstant_pairs_can_be_calibrated() -> None:
    dimensions = [
        {"genre": "g1", "form": "f1"},
        {"genre": "g1", "form": "f2"},
        {"genre": "g2", "form": "f1"},
        {"genre": "g2", "form": "f2"},
    ]
    scores = [(0.1, 0.2), (0.3, 0.4), (0.6, 0.7), (0.8, 0.9)]
    screenplays: list[EpisodeScreenplay] = []
    observations: list[HumanOneWatchObservation] = []
    estimates: list[ModelTargetEstimate] = []
    for index, (content_dimensions, pair_scores) in enumerate(
        zip(dimensions, scores, strict=True), start=1,
    ):
        scope_id = f"scope-{index}"
        screenplays.append(_screenplay(scope_id))
        for prior_id, score in zip(("AP-1", "AP-2"), pair_scores, strict=True):
            observations.extend(
                _observation(
                    scope_id,
                    prior_id,
                    score,
                    dimensions=content_dimensions,
                    ordinal=ordinal,
                )
                for ordinal in (1, 2)
            )
            estimates.append(_estimate(scope_id, prior_id, score))

    report = build_calibration_report(
        calibration_report_id="CAL-STABLE",
        calibration_scope_id="corpus-stable",
        screenplays=screenplays,
        observations=observations,
        model_estimates=estimates,
    )

    assert report.decision == "calibrated"
    assert report.calibration_score == pytest.approx(1.0)
    assert report.coverage_gaps == []
    assert report.stability_issues == []
    assert all(item.correlation == pytest.approx(1.0) for item in report.dimension_results)


def test_opposing_cross_dimension_correlations_stay_needs_review() -> None:
    dimensions = [
        {"genre": "g1", "form": "f1"},
        {"genre": "g1", "form": "f2"},
        {"genre": "g2", "form": "f1"},
        {"genre": "g2", "form": "f2"},
    ]
    model_scores = [(0.1, 0.2), (0.3, 0.4), (0.6, 0.7), (0.8, 0.9)]
    human_scores = [(0.1, 0.2), (0.3, 0.4), (0.9, 0.8), (0.7, 0.6)]
    screenplays: list[EpisodeScreenplay] = []
    observations: list[HumanOneWatchObservation] = []
    estimates: list[ModelTargetEstimate] = []
    for index, (content_dimensions, predicted, observed) in enumerate(
        zip(dimensions, model_scores, human_scores, strict=True), start=1,
    ):
        scope_id = f"unstable-{index}"
        screenplays.append(_screenplay(scope_id))
        for prior_id, predicted_score, observed_score in zip(
            ("AP-1", "AP-2"), predicted, observed, strict=True,
        ):
            observations.extend(
                _observation(
                    scope_id,
                    prior_id,
                    observed_score,
                    dimensions=content_dimensions,
                    ordinal=ordinal,
                )
                for ordinal in (1, 2)
            )
            estimates.append(_estimate(scope_id, prior_id, predicted_score))

    report = build_calibration_report(
        calibration_report_id="CAL-UNSTABLE",
        calibration_scope_id="corpus-unstable",
        screenplays=screenplays,
        observations=observations,
        model_estimates=estimates,
    )

    assert report.decision == "needs_review"
    assert report.calibration_score is None
    assert any(
        "CORRELATION_UNSTABLE_ACROSS_DIMENSIONS" in item
        for item in report.stability_issues
    )


def test_model_and_human_scores_must_bind_the_same_review_artifact() -> None:
    screenplay = _screenplay("scope-1")
    observations = [
        _observation("scope-1", "AP-1", 0.2),
        _observation("scope-1", "AP-2", 0.9),
    ]
    estimates = [
        _estimate("scope-1", "AP-1", 0.2),
        _estimate("scope-1", "AP-2", 0.9),
    ]
    estimates[0].narrative_review_artifact_id = "another-review-version"

    with pytest.raises(
        CalibrationContractError,
        match="MODEL_HUMAN_REVIEW_LINEAGE_MISMATCH",
    ):
        build_calibration_report(
            calibration_report_id="CAL-LINEAGE",
            calibration_scope_id="corpus-lineage",
            screenplays=[screenplay],
            observations=observations,
            model_estimates=estimates,
        )


def test_human_and_calibration_artifacts_preserve_review_lineage(
    calibration_db,
) -> None:
    screenplay = _screenplay("scope-1")
    review = evidence_repository.create_artifact(EvidenceArtifact(
        id="review-scope-1",
        type="narrative_review_report",
        scope_type="episode",
        scope_id="scope-1",
        status="validated",
        trust_level="T2",
        content={"decision": "pass"},
    ))
    observations = [
        _observation("scope-1", "AP-1", 1.0, review_artifact_id=review["id"]),
        _observation("scope-1", "AP-2", 1.0, review_artifact_id=review["id"]),
    ]
    observation_artifacts = []
    for observation in observations:
        freeze_artifact = _persist_freeze(
            observation,
            screenplay,
            review["id"],
        )
        observation_artifacts.append(
            persist_human_one_watch_observation(
                observation,
                screenplay=screenplay,
                narrative_review_artifact_ids=[review["id"]],
                frozen_recall_artifact_id=freeze_artifact["id"],
            )
        )
    estimates = [
        ModelTargetEstimate(
            scope_id="scope-1",
            audience_prior_id=prior_id,
            target_delta_id=_delta_id(prior_id),
            predicted_score=1.0,
            narrative_review_artifact_id=review["id"],
        )
        for prior_id in ("AP-1", "AP-2")
    ]
    report = build_calibration_report(
        calibration_report_id="CAL-PERSISTED",
        calibration_scope_id="corpus-persisted",
        screenplays=[screenplay],
        observations=observations,
        model_estimates=estimates,
    )

    artifact = persist_calibration_report(
        report,
        observation_artifact_ids=[item["id"] for item in observation_artifacts],
        narrative_review_artifact_ids=[review["id"]],
    )

    assert artifact["status"] == "needs_revision"
    assert set(artifact["parent_artifact_ids"]) == {
        review["id"],
        *(item["id"] for item in observation_artifacts),
    }
    evaluations = evidence_repository.get_evaluations(artifact["id"])
    assert evaluations[-1]["score"] is None
    assert evaluations[-1]["score_status"] == "unknown"
    assert evaluations[-1]["evidence"]["decision"] == "needs_review"


def test_observation_persistence_rejects_missing_review_report_lineage(
    calibration_db,
) -> None:
    screenplay = _screenplay("scope-1")
    wrong_parent = evidence_repository.create_artifact(EvidenceArtifact(
        id="not-a-review",
        type="storyboard_document",
        scope_type="episode",
        scope_id="scope-1",
        status="validated",
        trust_level="T2",
        content={},
    ))
    observation = _observation(
        "scope-1", "AP-1", 0.5, review_artifact_id=wrong_parent["id"],
    )

    with pytest.raises(CalibrationContractError, match="CALIBRATION_REVIEW_REPORT_MISSING"):
        persist_human_one_watch_observation(
            observation,
            screenplay=screenplay,
            narrative_review_artifact_ids=[wrong_parent["id"]],
            frozen_recall_artifact_id="missing-freeze",
        )


def test_final_human_observation_cannot_rewrite_frozen_recall(
    calibration_db,
) -> None:
    screenplay = _screenplay("scope-1")
    review = evidence_repository.create_artifact(EvidenceArtifact(
        id="review-scope-1",
        type="narrative_review_report",
        scope_type="episode",
        scope_id="scope-1",
        status="validated",
        trust_level="T2",
        content={"decision": "pass"},
    ))
    observation = _observation(
        "scope-1",
        "AP-1",
        0.8,
        review_artifact_id=review["id"],
    )
    freeze = _persist_freeze(observation, screenplay, review["id"])
    observation.spontaneous_recall = {"free_text": "rewritten after targets"}

    with pytest.raises(
        CalibrationContractError,
        match="HUMAN_FREEZE_OBSERVATION_DRIFT",
    ):
        persist_human_one_watch_observation(
            observation,
            screenplay=screenplay,
            narrative_review_artifact_ids=[review["id"]],
            frozen_recall_artifact_id=freeze["id"],
        )


def test_board_ui_exposes_two_stage_human_calibration_without_internal_codes() -> None:
    source = (
        Path(__file__).parents[1]
        / "frontend"
        / "src"
        / "pages"
        / "BoardPage.tsx"
    ).read_text(encoding="utf-8")

    assert "narrative-calibration/freeze" in source
    assert "narrative-calibration/observations" in source
    assert "narrative-calibration/ai-simulate" in source
    assert "运行 AI 一次观看模拟" in source
    assert "不会伪造真人参与者或观察记录" in source
    assert "真人一次观看校准" in source
    assert source.index("冻结首次复述") < source.index("提交真人观察")
    assert "title={freezeBlockedReason || undefined}" in source
    assert "human_one_watch_calibration_report" not in source
    assert "NARRATIVE_CALIBRATION_REQUIRED" not in source


def test_ai_one_watch_waives_only_the_extra_layer_when_simulation_is_weak(
    calibration_db,
) -> None:
    report = NarrativeReviewReport(
        narrative_review_report_id="NRR-WEAK-SIMULATION",
        scope_id="scope-1",
        target_delta_results=[{
            "audience_prior_id": "AP-1",
            "target_delta_id": "XD-AP1-1",
            "result": "satisfied",
            "predicted_score": 0.4,
        }],
        decision="pass",
        reason="The blind review gate passed but its confidence is weak.",
    )
    review = evidence_repository.create_artifact(EvidenceArtifact(
        type="narrative_review_report",
        scope_type="episode",
        scope_id="scope-1",
        status="validated",
        trust_level="T2",
        content=report.model_dump(mode="json"),
    ))

    artifact = persist_ai_one_watch_simulation_authority(
        report,
        narrative_review_artifact_id=review["id"],
    )
    authority = require_current_calibration_authority(
        expected_artifact_id=artifact["id"],
    )

    assert authority.authority_mode == "waived"
    assert authority.model_pass_threshold == 0.0
    assert authority.report.sample_summary["simulation_supported"] is False
    assert_report_meets_current_calibration(
        report,
        expected_calibration_artifact_id=artifact["id"],
    )
