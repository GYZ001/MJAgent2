"""Human one-watch calibration for narrative review predictions.

The protocol is deliberately content-agnostic.  It validates identity,
isolation, pair coverage and statistical estimability; genres, formats and any
additional dimensions remain open values supplied by the study.  Sparse or
unstable evidence is reported as ``needs_review`` and never converted into a
synthetic perfect score.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import math
from statistics import fmean
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.narrative import NARRATIVE_CONTRACT_VERSION
from app.narrative_review import (
    BLIND_READER_PROMPT_VERSION,
    COMPARATOR_PROMPT_VERSION,
)
from app.schemas import EpisodeScreenplay, NarrativeReviewReport


HUMAN_ONE_WATCH_CONTRACT_VERSION = "human-one-watch.v1"
HUMAN_CALIBRATION_CONTRACT_VERSION = "narrative-human-calibration.v1"
DEFAULT_CROSS_CONTENT_DIMENSIONS = ("genre", "form")
GLOBAL_CALIBRATION_SCOPE_ID = "global-narrative-continuity"
DEFAULT_MINIMUM_CORRELATION = 0.6
DEFAULT_HUMAN_SUCCESS_THRESHOLD = 0.8

__all__ = [
    "CalibrationContractError",
    "CurrentCalibrationAuthority",
    "CalibrationReport",
    "DimensionCalibrationResult",
    "HumanOneWatchFreeze",
    "HumanOneWatchObservation",
    "HumanTargetDeltaObservation",
    "ModelTargetEstimate",
    "build_calibration_report",
    "assert_report_meets_current_calibration",
    "calibrate_and_persist_human_one_watch",
    "persist_calibration_report",
    "persist_human_one_watch_freeze",
    "persist_human_one_watch_observation",
    "require_current_calibration_authority",
    "validate_human_one_watch_observation",
]


class CalibrationContractError(ValueError):
    """Raised for invalid protocol data, IDs or evidence lineage."""

    def __init__(self, errors: Iterable[str]):
        self.errors = list(dict.fromkeys(str(item) for item in errors if str(item)))
        super().__init__("；".join(self.errors[:8]))


class _OpenSemanticModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class HumanTargetDeltaObservation(_OpenSemanticModel):
    """A backend-adjudicated result bound to one prior/target pair.

    ``observed_interpretation`` and ``elicitation_context`` are intentionally
    open JSON.  They describe what the participant actually expressed; they
    are not a list of allowed story meanings.
    """

    audience_prior_id: str
    target_delta_id: str
    observed_score: float = Field(ge=0.0, le=1.0)
    observed_interpretation: dict[str, Any] = Field(default_factory=dict)
    supporting_response_refs: list[str] = Field(default_factory=list)
    elicitation_context: dict[str, Any] = Field(default_factory=dict)
    adjudication_status: str = "observed"
    uncertainty: str | None = None


class HumanOneWatchFreeze(_OpenSemanticModel):
    """Immutable first-pass recall captured before targets become visible."""

    observation_id: str
    participant_id_hash: str
    scope_id: str
    audience_prior_id: str
    narrative_review_artifact_id: str
    watched_once: bool
    watch_count: int = Field(default=1, ge=0)
    replay_or_seek_used: bool = False
    source_material_seen: bool = False
    target_answers_seen: bool = False
    director_intent_seen: bool = False
    spontaneous_recall_frozen: bool = True
    spontaneous_recall: dict[str, Any] = Field(default_factory=dict)
    content_dimensions: dict[str, Any] = Field(default_factory=dict)
    collection_context: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_freeze(self) -> "HumanOneWatchFreeze":
        errors: list[str] = []
        if not self.watched_once or self.watch_count != 1:
            errors.append("[HUMAN_WATCH_COUNT_INVALID] watched_once=true 且 watch_count=1 才是一次观看")
        if self.replay_or_seek_used:
            errors.append("[HUMAN_REPLAY_USED] 首轮观察不得回放或拖动")
        if self.source_material_seen:
            errors.append("[HUMAN_SOURCE_EXPOSED] 首轮观众已看过原文")
        if self.target_answers_seen or self.director_intent_seen:
            errors.append("[HUMAN_TARGET_EXPOSED] 冻结前不得展示目标答案或导演意图")
        if not self.spontaneous_recall_frozen:
            errors.append("[HUMAN_RECALL_NOT_FROZEN] 首轮自由复述必须冻结")
        if not self.observation_id.strip() or not self.participant_id_hash.strip():
            errors.append("[HUMAN_OBSERVATION_ID_MISSING] observation_id/participant_id_hash 不能为空")
        if not self.scope_id.strip() or not self.audience_prior_id.strip():
            errors.append("[HUMAN_SCOPE_PRIOR_MISSING] scope_id/audience_prior_id 不能为空")
        if not self.narrative_review_artifact_id.strip():
            errors.append("[HUMAN_REVIEW_LINEAGE_MISSING] 必须绑定 narrative review artifact")
        if not self.spontaneous_recall:
            errors.append("[HUMAN_SPONTANEOUS_RECALL_EMPTY] 首轮自由复述不能为空")
        if errors:
            raise ValueError("；".join(errors))
        return self


class HumanOneWatchObservation(_OpenSemanticModel):
    """One isolated human viewing, with protocol facts made explicit."""

    observation_id: str
    participant_id_hash: str
    scope_id: str
    audience_prior_id: str
    narrative_review_artifact_id: str
    watched_once: bool
    watch_count: int = Field(default=1, ge=0)
    replay_or_seek_used: bool = False
    source_material_seen: bool = False
    target_answers_seen: bool = False
    director_intent_seen: bool = False
    spontaneous_recall_frozen: bool = True
    spontaneous_recall: dict[str, Any] = Field(default_factory=dict)
    neutral_followup_observations: list[dict[str, Any] | str] = Field(default_factory=list)
    target_delta_observations: list[HumanTargetDeltaObservation] = Field(default_factory=list)
    content_dimensions: dict[str, Any] = Field(default_factory=dict)
    collection_context: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _one_watch_isolation_gate(self) -> "HumanOneWatchObservation":
        errors: list[str] = []
        if not self.watched_once or self.watch_count != 1:
            errors.append("[HUMAN_WATCH_COUNT_INVALID] watched_once=true 且 watch_count=1 才是一次观看")
        if self.replay_or_seek_used:
            errors.append("[HUMAN_REPLAY_USED] 一次观看观察不得回放或拖动")
        if self.source_material_seen:
            errors.append("[HUMAN_SOURCE_EXPOSED] 观众已看过原文，不得作为冷启动样本")
        if self.target_answers_seen or self.director_intent_seen:
            errors.append("[HUMAN_TARGET_EXPOSED] 观众已看过目标答案/导演意图")
        if not self.spontaneous_recall_frozen:
            errors.append("[HUMAN_RECALL_NOT_FROZEN] 自由复述必须在中性追问前冻结")
        if not self.observation_id.strip() or not self.participant_id_hash.strip():
            errors.append("[HUMAN_OBSERVATION_ID_MISSING] observation_id/participant_id_hash 不能为空")
        if not self.scope_id.strip() or not self.audience_prior_id.strip():
            errors.append("[HUMAN_SCOPE_PRIOR_MISSING] scope_id/audience_prior_id 不能为空")
        if not self.narrative_review_artifact_id.strip():
            errors.append("[HUMAN_REVIEW_LINEAGE_MISSING] 必须绑定 narrative review artifact")
        seen: set[tuple[str, str]] = set()
        for item in self.target_delta_observations:
            pair = (item.audience_prior_id, item.target_delta_id)
            if item.audience_prior_id != self.audience_prior_id:
                errors.append(
                    f"[HUMAN_PAIR_PRIOR_MISMATCH] {pair} 不属于本轮先验 {self.audience_prior_id}"
                )
            if pair in seen:
                errors.append(f"[HUMAN_PAIR_DUPLICATE] 重复观察 {pair}")
            seen.add(pair)
        if not self.target_delta_observations:
            errors.append("[HUMAN_TARGET_OBSERVATIONS_EMPTY] 缺少逐目标观察")
        if errors:
            raise ValueError("；".join(errors))
        return self


class ModelTargetEstimate(_OpenSemanticModel):
    scope_id: str
    audience_prior_id: str
    target_delta_id: str
    predicted_score: float = Field(ge=0.0, le=1.0)
    narrative_review_artifact_id: str
    estimate_context: dict[str, Any] = Field(default_factory=dict)


class CalibrationPairResult(_OpenSemanticModel):
    scope_id: str
    audience_prior_id: str
    target_delta_id: str
    human_sample_count: int = 0
    human_mean_score: float | None = None
    human_score_variance: float | None = None
    model_predicted_score: float | None = None
    absolute_error: float | None = None
    status: str = "needs_review"


class DimensionCalibrationResult(_OpenSemanticModel):
    dimension_name: str
    dimension_value: str
    observation_count: int = 0
    paired_target_count: int = 0
    correlation: float | None = None
    status: str = "insufficient"
    reason: str = ""


class CalibrationReport(_OpenSemanticModel):
    calibration_report_id: str
    calibration_scope_id: str
    contract_version: str = HUMAN_CALIBRATION_CONTRACT_VERSION
    narrative_contract_version: str = NARRATIVE_CONTRACT_VERSION
    blind_prompt_version: str = BLIND_READER_PROMPT_VERSION
    comparator_prompt_version: str = COMPARATOR_PROMPT_VERSION
    scope_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    narrative_review_artifact_ids: list[str] = Field(default_factory=list)
    required_dimension_axes: list[str] = Field(default_factory=list)
    sample_summary: dict[str, Any] = Field(default_factory=dict)
    pair_results: list[CalibrationPairResult] = Field(default_factory=list)
    dimension_results: list[DimensionCalibrationResult] = Field(default_factory=list)
    correlation_analysis: dict[str, Any] = Field(default_factory=dict)
    coverage_gaps: list[str] = Field(default_factory=list)
    stability_issues: list[str] = Field(default_factory=list)
    calibration_score: float | None = None
    minimum_correlation: float = DEFAULT_MINIMUM_CORRELATION
    human_success_threshold: float = DEFAULT_HUMAN_SUCCESS_THRESHOLD
    recommended_model_pass_threshold: float | None = None
    threshold_analysis: dict[str, Any] = Field(default_factory=dict)
    confidence_status: str = "needs_review"
    decision: str = "needs_review"
    reason: str = ""
    evidence_lineage: dict[str, Any] = Field(default_factory=dict)


PairKey = tuple[str, str, str]


@dataclass(frozen=True)
class CurrentCalibrationAuthority:
    artifact_id: str
    artifact_hash: str
    report: CalibrationReport
    model_pass_threshold: float


def _expected_pairs(
    screenplays: Sequence[EpisodeScreenplay],
) -> tuple[dict[PairKey, Any], dict[tuple[str, str], set[str]]]:
    pairs: dict[PairKey, Any] = {}
    by_prior: dict[tuple[str, str], set[str]] = defaultdict(set)
    scope_ids: set[str] = set()
    errors: list[str] = []
    for screenplay in screenplays:
        plan = screenplay.narrative_plan
        if plan is None:
            errors.append(f"[CALIBRATION_PLAN_MISSING] episode_no={screenplay.episode_no}")
            continue
        scope_id = plan.scope_id.strip()
        if not scope_id:
            errors.append(f"[CALIBRATION_SCOPE_MISSING] episode_no={screenplay.episode_no}")
            continue
        if scope_id in scope_ids:
            errors.append(f"[CALIBRATION_SCOPE_DUPLICATE] {scope_id}")
            continue
        scope_ids.add(scope_id)
        prior_ids = {item.audience_prior_id for item in plan.audience_priors}
        for intent in plan.experience_intents:
            for path in intent.audience_paths:
                if path.audience_prior_id not in prior_ids:
                    errors.append(
                        f"[CALIBRATION_PRIOR_UNKNOWN] {scope_id}/{path.audience_prior_id}"
                    )
                for delta in path.target_deltas:
                    key = (scope_id, path.audience_prior_id, delta.target_delta_id)
                    if key in pairs:
                        errors.append(f"[CALIBRATION_PAIR_DUPLICATE] {key}")
                    pairs[key] = delta
                    by_prior[(scope_id, path.audience_prior_id)].add(delta.target_delta_id)
    if errors:
        raise CalibrationContractError(errors)
    if not pairs:
        raise CalibrationContractError(["[CALIBRATION_PAIRS_EMPTY] 叙事合同没有可校准的 prior/target_delta 对"])
    return pairs, by_prior


def validate_human_one_watch_observation(
    observation: HumanOneWatchObservation,
    screenplay: EpisodeScreenplay,
) -> list[str]:
    """Validate one observation against its authoritative audience paths."""
    try:
        expected, by_prior = _expected_pairs([screenplay])
    except CalibrationContractError as exc:
        return exc.errors
    plan = screenplay.narrative_plan
    assert plan is not None
    errors: list[str] = []
    if observation.scope_id != plan.scope_id:
        errors.append(
            f"[HUMAN_SCOPE_MISMATCH] observation={observation.scope_id} plan={plan.scope_id}"
        )
    expected_targets = by_prior.get((plan.scope_id, observation.audience_prior_id))
    if expected_targets is None:
        errors.append(
            f"[HUMAN_PRIOR_UNKNOWN] {observation.audience_prior_id} 不属于 {plan.scope_id}"
        )
        expected_targets = set()
    actual_targets = {item.target_delta_id for item in observation.target_delta_observations}
    missing = expected_targets - actual_targets
    extra = actual_targets - expected_targets
    if missing:
        errors.append(f"[HUMAN_PAIR_MISSING] {observation.observation_id} 缺少 {sorted(missing)}")
    if extra:
        errors.append(f"[HUMAN_PAIR_UNKNOWN] {observation.observation_id} 包含 {sorted(extra)}")
    for item in observation.target_delta_observations:
        key = (observation.scope_id, item.audience_prior_id, item.target_delta_id)
        if key not in expected:
            errors.append(f"[HUMAN_PAIR_UNKNOWN] {key}")
    return list(dict.fromkeys(errors))


def _require_review_lineage(artifact_ids: Sequence[str]) -> list[dict[str, Any]]:
    ids = list(dict.fromkeys(str(item) for item in artifact_ids if str(item)))
    if not ids:
        raise CalibrationContractError(["[CALIBRATION_REVIEW_LINEAGE_MISSING] 缺少 narrative review artifact"])
    artifacts: list[dict[str, Any]] = []
    errors: list[str] = []
    for artifact_id in ids:
        artifact = evidence_repository.get_artifact(artifact_id)
        if artifact is None:
            errors.append(f"[CALIBRATION_PARENT_MISSING] {artifact_id}")
            continue
        if artifact.get("type") != "narrative_review_report":
            errors.append(f"[CALIBRATION_REVIEW_REPORT_MISSING] {artifact_id} 不是 narrative_review_report")
            continue
        if artifact.get("status") in {
            "stale", "rejected", "superseded", "needs_revision",
        }:
            errors.append(f"[CALIBRATION_REVIEW_REPORT_INVALID] {artifact_id} 状态不可用")
            continue
        try:
            current_hash = evidence_repository.content_hash(
                artifact.get("content"),
                artifact.get("file_path"),
            )
        except (OSError, TypeError, ValueError):
            current_hash = ""
        if current_hash != artifact.get("content_hash"):
            errors.append(f"[CALIBRATION_REVIEW_HASH_DRIFT] {artifact_id}")
            continue
        artifacts.append(artifact)
    if errors:
        raise CalibrationContractError(errors)
    return artifacts


def persist_human_one_watch_freeze(
    freeze: HumanOneWatchFreeze,
    *,
    screenplay: EpisodeScreenplay,
    narrative_review_artifact_ids: Sequence[str],
) -> dict[str, Any]:
    """Persist the first pass before any target-scored observation is accepted."""
    plan = screenplay.narrative_plan
    errors: list[str] = []
    if plan is None:
        errors.append("[CALIBRATION_PLAN_MISSING] 首轮观察缺少叙事合同")
    else:
        if freeze.scope_id != plan.scope_id:
            errors.append(
                f"[HUMAN_SCOPE_MISMATCH] observation={freeze.scope_id} plan={plan.scope_id}"
            )
        if freeze.audience_prior_id not in {
            item.audience_prior_id for item in plan.audience_priors
        }:
            errors.append(f"[HUMAN_PRIOR_UNKNOWN] {freeze.audience_prior_id}")
    review_ids = list(dict.fromkeys(
        str(item) for item in narrative_review_artifact_ids if str(item)
    ))
    _require_review_lineage(review_ids)
    if freeze.narrative_review_artifact_id not in review_ids:
        errors.append(
            "[HUMAN_REVIEW_LINEAGE_MISMATCH] freeze.narrative_review_artifact_id "
            "必须在父证据链中"
        )
    if errors:
        raise CalibrationContractError(errors)
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="human_one_watch_spontaneous_recall",
        scope_type="episode",
        scope_id=freeze.scope_id,
        status="validated",
        trust_level="T4",
        content=freeze.model_dump(mode="json"),
        parent_artifact_ids=review_ids,
        contract_version=HUMAN_ONE_WATCH_CONTRACT_VERSION,
    ))
    evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="human",
            evaluator_name="one_watch_freeze_gate",
            evaluator_version=HUMAN_ONE_WATCH_CONTRACT_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            score_status="observation_only",
            runtime_blocking=True,
            score=None,
            evidence={
                "observation_id": freeze.observation_id,
                "audience_prior_id": freeze.audience_prior_id,
                "spontaneous_recall_frozen": True,
                "narrative_review_artifact_id": (
                    freeze.narrative_review_artifact_id
                ),
            },
            confidence=freeze.confidence,
        ),
    )
    return artifact


def persist_human_one_watch_observation(
    observation: HumanOneWatchObservation,
    *,
    screenplay: EpisodeScreenplay,
    narrative_review_artifact_ids: Sequence[str],
    frozen_recall_artifact_id: str,
) -> dict[str, Any]:
    """Validate and persist one human observation with review lineage."""
    errors = validate_human_one_watch_observation(observation, screenplay)
    if errors:
        raise CalibrationContractError(errors)
    review_ids = list(dict.fromkeys(str(item) for item in narrative_review_artifact_ids if str(item)))
    _require_review_lineage(review_ids)
    if observation.narrative_review_artifact_id not in review_ids:
        raise CalibrationContractError([
            "[HUMAN_REVIEW_LINEAGE_MISMATCH] observation.narrative_review_artifact_id "
            "必须在父证据链中"
        ])
    freeze_artifact = evidence_repository.get_artifact(
        str(frozen_recall_artifact_id)
    )
    if (
        freeze_artifact is None
        or freeze_artifact.get("type") != "human_one_watch_spontaneous_recall"
        or freeze_artifact.get("status") != "validated"
    ):
        raise CalibrationContractError([
            "[HUMAN_FREEZE_ARTIFACT_INVALID] 目标评分前必须先持久化有效首轮冻结"
        ])
    try:
        freeze = HumanOneWatchFreeze.model_validate(
            freeze_artifact.get("content") or {}
        )
    except Exception as exc:
        raise CalibrationContractError([
            f"[HUMAN_FREEZE_ARTIFACT_INVALID] {exc}"
        ]) from exc
    if (
        freeze.observation_id != observation.observation_id
        or freeze.participant_id_hash != observation.participant_id_hash
        or freeze.scope_id != observation.scope_id
        or freeze.audience_prior_id != observation.audience_prior_id
        or freeze.narrative_review_artifact_id
        != observation.narrative_review_artifact_id
        or freeze.spontaneous_recall != observation.spontaneous_recall
        or freeze.content_dimensions != observation.content_dimensions
    ):
        raise CalibrationContractError([
            "[HUMAN_FREEZE_OBSERVATION_DRIFT] 最终观察改写了已冻结首轮身份、复述或样本维度"
        ])
    freeze_gates = [
        item
        for item in evidence_repository.get_evaluations(
            str(frozen_recall_artifact_id)
        )
        if item.get("evaluator_name") == "one_watch_freeze_gate"
        and item.get("evaluator_version") == HUMAN_ONE_WATCH_CONTRACT_VERSION
        and item.get("evaluation_role") == "runtime_gate"
        and bool(item.get("runtime_blocking"))
        and item.get("status") == "passed"
        and bool(item.get("hard_gate_passed"))
    ]
    if len(freeze_gates) != 1:
        raise CalibrationContractError([
            "[HUMAN_FREEZE_GATE_INVALID] 首轮冻结缺少唯一通过的隔离门禁"
        ])
    parents = list(dict.fromkeys([
        *review_ids,
        str(frozen_recall_artifact_id),
    ]))
    for artifact_id in parents:
        if evidence_repository.get_artifact(artifact_id) is None:
            raise CalibrationContractError([f"[CALIBRATION_PARENT_MISSING] {artifact_id}"])
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="human_one_watch_observation",
        scope_type="episode",
        scope_id=observation.scope_id,
        status="validated",
        trust_level="T4",
        content=observation.model_dump(mode="json"),
        parent_artifact_ids=parents,
        contract_version=HUMAN_ONE_WATCH_CONTRACT_VERSION,
    ))
    evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="human",
            evaluator_name="one_watch_protocol",
            evaluator_version=HUMAN_ONE_WATCH_CONTRACT_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="score_only",
            score_status="observation_only",
            runtime_blocking=False,
            score=None,
            evidence={
                "watched_once": True,
                "source_material_seen": False,
                "target_answers_seen": False,
                "audience_prior_id": observation.audience_prior_id,
                "paired_target_delta_ids": [
                    item.target_delta_id for item in observation.target_delta_observations
                ],
                "narrative_review_artifact_ids": review_ids,
            },
            confidence=observation.confidence,
        ),
    )
    return artifact


def _verified_human_observation_artifact(
    artifact_id: str,
    *,
    allowed_review_ids: set[str],
) -> tuple[HumanOneWatchObservation | None, list[str]]:
    artifact = evidence_repository.get_artifact(artifact_id)
    errors: list[str] = []
    if (
        artifact is None
        or artifact.get("type") != "human_one_watch_observation"
        or artifact.get("status")
        in {"stale", "rejected", "superseded", "needs_revision"}
    ):
        return None, [f"[CALIBRATION_OBSERVATION_TYPE_INVALID] {artifact_id}"]
    try:
        current_hash = evidence_repository.content_hash(
            artifact.get("content"),
            artifact.get("file_path"),
        )
    except (OSError, TypeError, ValueError):
        current_hash = ""
    if current_hash != artifact.get("content_hash"):
        errors.append(f"[CALIBRATION_OBSERVATION_HASH_DRIFT] {artifact_id}")
    try:
        observation = HumanOneWatchObservation.model_validate(
            artifact.get("content") or {}
        )
    except Exception as exc:
        return None, [
            *errors,
            f"[CALIBRATION_OBSERVATION_SCHEMA_INVALID] {artifact_id}: {exc}",
        ]
    if observation.narrative_review_artifact_id not in allowed_review_ids:
        errors.append(
            f"[CALIBRATION_OBSERVATION_REVIEW_MISMATCH] {artifact_id}"
        )
    parents = set(artifact.get("parent_artifact_ids") or [])
    if observation.narrative_review_artifact_id not in parents:
        errors.append(
            f"[CALIBRATION_OBSERVATION_REVIEW_LINEAGE_MISSING] {artifact_id}"
        )
    freeze_rows = [
        evidence_repository.get_artifact(str(parent_id))
        for parent_id in parents
    ]
    freezes = [
        item for item in freeze_rows
        if item is not None
        and item.get("type") == "human_one_watch_spontaneous_recall"
        and item.get("status") == "validated"
    ]
    if len(freezes) != 1:
        errors.append(f"[CALIBRATION_OBSERVATION_FREEZE_INVALID] {artifact_id}")
    else:
        freeze_artifact = freezes[0]
        try:
            freeze_hash = evidence_repository.content_hash(
                freeze_artifact.get("content"),
                freeze_artifact.get("file_path"),
            )
        except (OSError, TypeError, ValueError):
            freeze_hash = ""
        if freeze_hash != freeze_artifact.get("content_hash"):
            errors.append(
                f"[CALIBRATION_OBSERVATION_FREEZE_HASH_DRIFT] {artifact_id}"
            )
        try:
            freeze = HumanOneWatchFreeze.model_validate(
                freeze_artifact.get("content") or {}
            )
        except Exception as exc:
            errors.append(
                f"[CALIBRATION_OBSERVATION_FREEZE_SCHEMA_INVALID] {artifact_id}: {exc}"
            )
        else:
            if (
                freeze.observation_id != observation.observation_id
                or freeze.participant_id_hash != observation.participant_id_hash
                or freeze.scope_id != observation.scope_id
                or freeze.audience_prior_id != observation.audience_prior_id
                or freeze.narrative_review_artifact_id
                != observation.narrative_review_artifact_id
                or freeze.spontaneous_recall != observation.spontaneous_recall
                or freeze.content_dimensions != observation.content_dimensions
            ):
                errors.append(
                    f"[CALIBRATION_OBSERVATION_FREEZE_DRIFT] {artifact_id}"
                )
        freeze_gates = [
            item
            for item in evidence_repository.get_evaluations(
                str(freeze_artifact["id"])
            )
            if item.get("evaluator_name") == "one_watch_freeze_gate"
            and item.get("evaluator_version") == HUMAN_ONE_WATCH_CONTRACT_VERSION
            and item.get("evaluation_role") == "runtime_gate"
            and bool(item.get("runtime_blocking"))
            and item.get("status") == "passed"
            and bool(item.get("hard_gate_passed"))
        ]
        if len(freeze_gates) != 1:
            errors.append(
                f"[CALIBRATION_OBSERVATION_FREEZE_GATE_INVALID] {artifact_id}"
            )
    observation_gates = [
        item
        for item in evidence_repository.get_evaluations(artifact_id)
        if item.get("evaluator_name") == "one_watch_protocol"
        and item.get("evaluator_version") == HUMAN_ONE_WATCH_CONTRACT_VERSION
        and item.get("status") == "passed"
        and bool(item.get("hard_gate_passed"))
    ]
    if len(observation_gates) != 1:
        errors.append(f"[CALIBRATION_OBSERVATION_GATE_INVALID] {artifact_id}")
    return observation, errors


def _variance(values: Sequence[float]) -> float | None:
    if not values:
        return None
    mean = fmean(values)
    return fmean((value - mean) ** 2 for value in values)


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    x_mean, y_mean = fmean(xs), fmean(ys)
    x_var = sum((value - x_mean) ** 2 for value in xs)
    y_var = sum((value - y_mean) ** 2 for value in ys)
    if x_var <= 0.0 or y_var <= 0.0:
        return None
    value = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(xs, ys, strict=True)
    ) / math.sqrt(x_var * y_var)
    return max(-1.0, min(1.0, value))


def _dimension_values(value: Any) -> list[str]:
    if value is None:
        return []
    raw_values = value if isinstance(value, (list, tuple, set)) else [value]
    values: list[str] = []
    for item in raw_values:
        if isinstance(item, str):
            canonical = item.strip()
        else:
            canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical and canonical not in values:
            values.append(canonical)
    return values


def _aggregate_pair_scores(
    observations: Sequence[HumanOneWatchObservation],
) -> dict[PairKey, list[float]]:
    scores: dict[PairKey, list[float]] = defaultdict(list)
    for observation in observations:
        for result in observation.target_delta_observations:
            scores[(
                observation.scope_id,
                result.audience_prior_id,
                result.target_delta_id,
            )].append(result.observed_score)
    return scores


def _derive_model_pass_threshold(
    paired_results: Sequence[CalibrationPairResult],
    *,
    human_success_threshold: float,
) -> tuple[float | None, dict[str, Any], list[str]]:
    """Derive one content-agnostic model threshold from human outcomes.

    Candidate thresholds come only from observed model scores.  Selection
    maximizes balanced accuracy against the human one-watch pass label, with
    recall and the stricter threshold used as deterministic tie breakers.
    """
    rows = [
        (
            float(item.model_predicted_score),
            float(item.human_mean_score) >= human_success_threshold,
        )
        for item in paired_results
        if item.model_predicted_score is not None
        and item.human_mean_score is not None
    ]
    positives = sum(label for _score, label in rows)
    negatives = len(rows) - positives
    if not rows or not positives or not negatives:
        return None, {
            "paired_count": len(rows),
            "human_positive_count": positives,
            "human_negative_count": negatives,
        }, ["[CALIBRATION_THRESHOLD_NOT_ESTIMABLE] 人类样本必须同时包含达成与未达成目标"]

    scores = sorted({score for score, _label in rows})
    candidates = sorted({
        0.0,
        1.0,
        *scores,
        *[
            (left + right) / 2.0
            for left, right in zip(scores, scores[1:], strict=False)
        ],
    })
    ranked: list[dict[str, Any]] = []
    for threshold in candidates:
        true_positive = sum(
            score >= threshold and label for score, label in rows
        )
        true_negative = sum(
            score < threshold and not label for score, label in rows
        )
        false_positive = negatives - true_negative
        false_negative = positives - true_positive
        recall = true_positive / positives
        specificity = true_negative / negatives
        ranked.append({
            "threshold": threshold,
            "balanced_accuracy": (recall + specificity) / 2.0,
            "recall": recall,
            "specificity": specificity,
            "false_positive_count": false_positive,
            "false_negative_count": false_negative,
        })
    selected = max(
        ranked,
        key=lambda item: (
            item["balanced_accuracy"],
            item["recall"],
            item["specificity"],
            item["threshold"],
        ),
    )
    issues = []
    if selected["balanced_accuracy"] < 0.7:
        issues.append(
            "[CALIBRATION_THRESHOLD_UNSTABLE] 数据驱动阈值的平衡准确率低于 0.7"
        )
    return float(selected["threshold"]), {
        "selection_rule": (
            "最大化人类一次观看标签的 balanced_accuracy；"
            "并列时优先 recall、specificity 与更严格阈值"
        ),
        "human_success_threshold": human_success_threshold,
        "paired_count": len(rows),
        "human_positive_count": positives,
        "human_negative_count": negatives,
        "selected": selected,
    }, issues


def _dimension_result(
    *,
    axis: str,
    value: str,
    observations: Sequence[HumanOneWatchObservation],
    estimate_map: dict[PairKey, ModelTargetEstimate],
) -> DimensionCalibrationResult:
    selected = [
        observation for observation in observations
        if value in _dimension_values(observation.content_dimensions.get(axis))
    ]
    scores = _aggregate_pair_scores(selected)
    paired = [
        (estimate_map[key].predicted_score, fmean(human_scores))
        for key, human_scores in scores.items()
        if key in estimate_map and human_scores
    ]
    correlation = _pearson(
        [item[0] for item in paired],
        [item[1] for item in paired],
    )
    return DimensionCalibrationResult(
        dimension_name=axis,
        dimension_value=value,
        observation_count=len(selected),
        paired_target_count=len(paired),
        correlation=correlation,
        status="estimable" if correlation is not None else "insufficient",
        reason=(
            "该分组可估计模型与人类的相关性"
            if correlation is not None
            else "该分组的成对目标数不足或分数无方差"
        ),
    )


def build_calibration_report(
    *,
    calibration_report_id: str,
    calibration_scope_id: str,
    screenplays: Sequence[EpisodeScreenplay],
    observations: Sequence[HumanOneWatchObservation],
    model_estimates: Sequence[ModelTargetEstimate],
    required_dimension_axes: Sequence[str] = DEFAULT_CROSS_CONTENT_DIMENSIONS,
    minimum_correlation: float = DEFAULT_MINIMUM_CORRELATION,
    human_success_threshold: float = DEFAULT_HUMAN_SUCCESS_THRESHOLD,
) -> CalibrationReport:
    """Build a conservative calibration report from paired human/model data."""
    expected, by_prior = _expected_pairs(screenplays)
    screenplay_by_scope = {
        screenplay.narrative_plan.scope_id: screenplay
        for screenplay in screenplays
        if screenplay.narrative_plan is not None
    }
    structural_errors: list[str] = []
    observation_ids: set[str] = set()
    human_review_ids_by_pair: dict[PairKey, set[str]] = defaultdict(set)
    for observation in observations:
        if observation.observation_id in observation_ids:
            structural_errors.append(f"[HUMAN_OBSERVATION_DUPLICATE] {observation.observation_id}")
        observation_ids.add(observation.observation_id)
        screenplay = screenplay_by_scope.get(observation.scope_id)
        if screenplay is None:
            structural_errors.append(f"[HUMAN_SCOPE_UNKNOWN] {observation.scope_id}")
            continue
        structural_errors.extend(validate_human_one_watch_observation(observation, screenplay))
        for item in observation.target_delta_observations:
            human_review_ids_by_pair[(
                observation.scope_id,
                item.audience_prior_id,
                item.target_delta_id,
            )].add(observation.narrative_review_artifact_id)
    estimate_map: dict[PairKey, ModelTargetEstimate] = {}
    for estimate in model_estimates:
        key = (estimate.scope_id, estimate.audience_prior_id, estimate.target_delta_id)
        if key not in expected:
            structural_errors.append(f"[MODEL_ESTIMATE_PAIR_UNKNOWN] {key}")
        if key in estimate_map:
            structural_errors.append(f"[MODEL_ESTIMATE_PAIR_DUPLICATE] {key}")
        human_review_ids = human_review_ids_by_pair.get(key, set())
        if len(human_review_ids) > 1:
            structural_errors.append(
                f"[HUMAN_REVIEW_LINEAGE_CONFLICT] {key} 的人类观察跨越多个审读版本"
            )
        elif human_review_ids and estimate.narrative_review_artifact_id not in human_review_ids:
            structural_errors.append(
                f"[MODEL_HUMAN_REVIEW_LINEAGE_MISMATCH] {key} 的模型估计与"
                "人类观察不属于同一 narrative_review_artifact"
            )
        estimate_map[key] = estimate
    if structural_errors:
        raise CalibrationContractError(structural_errors)

    human_scores = _aggregate_pair_scores(observations)
    human_participants: dict[PairKey, set[str]] = defaultdict(set)
    for observation in observations:
        for result in observation.target_delta_observations:
            human_participants[(
                observation.scope_id,
                result.audience_prior_id,
                result.target_delta_id,
            )].add(observation.participant_id_hash)
    pair_results: list[CalibrationPairResult] = []
    coverage_gaps: list[str] = []
    for key in sorted(expected):
        scores = human_scores.get(key, [])
        estimate = estimate_map.get(key)
        if not scores:
            coverage_gaps.append(f"[HUMAN_PAIR_SAMPLE_MISSING] {key}")
        elif len(human_participants.get(key, set())) < 2:
            # Two independent viewers is the minimum needed to observe any
            # within-pair human variability.  This is an estimability rule,
            # not a content/category threshold.
            coverage_gaps.append(
                f"[HUMAN_PAIR_REPLICATION_INSUFFICIENT] {key} "
                f"participants={len(human_participants.get(key, set()))}"
            )
        if estimate is None:
            coverage_gaps.append(f"[MODEL_ESTIMATE_MISSING] {key}")
        human_mean = fmean(scores) if scores else None
        predicted = estimate.predicted_score if estimate is not None else None
        pair_results.append(CalibrationPairResult(
            scope_id=key[0],
            audience_prior_id=key[1],
            target_delta_id=key[2],
            human_sample_count=len(scores),
            human_mean_score=human_mean,
            human_score_variance=_variance(scores),
            model_predicted_score=predicted,
            absolute_error=(
                abs(human_mean - predicted)
                if human_mean is not None and predicted is not None else None
            ),
            status="paired" if human_mean is not None and predicted is not None else "needs_review",
        ))

    paired_results = [
        item for item in pair_results
        if item.human_mean_score is not None and item.model_predicted_score is not None
    ]
    model_values = [float(item.model_predicted_score) for item in paired_results]
    human_values = [float(item.human_mean_score) for item in paired_results]
    model_constant = bool(model_values) and _variance(model_values) == 0.0
    human_constant = bool(human_values) and _variance(human_values) == 0.0
    overall_correlation = _pearson(model_values, human_values)
    stability_issues: list[str] = []
    if model_constant:
        stability_issues.append("[MODEL_SCORES_CONSTANT] 模型配对分数为常量，无法证明区分能力")
    if human_constant:
        stability_issues.append("[HUMAN_SCORES_CONSTANT] 人类配对分数为常量，无法估计相关性")
    if overall_correlation is None:
        stability_issues.append("[CORRELATION_NOT_ESTIMABLE] 成对样本不足或分数无方差")
    elif overall_correlation < minimum_correlation:
        stability_issues.append(
            f"[CORRELATION_BELOW_THRESHOLD] 模型与人类观察相关性 "
            f"{overall_correlation:.3f} 低于统一阈值 {minimum_correlation:.3f}"
        )

    axes = list(dict.fromkeys(str(axis).strip() for axis in required_dimension_axes if str(axis).strip()))
    dimension_results: list[DimensionCalibrationResult] = []
    for axis in axes:
        missing_observation_ids = [
            item.observation_id for item in observations
            if not _dimension_values(item.content_dimensions.get(axis))
        ]
        if missing_observation_ids:
            coverage_gaps.append(
                f"[DIMENSION_VALUE_MISSING] axis={axis} observations={sorted(missing_observation_ids)}"
            )
        values = sorted({
            value
            for observation in observations
            for value in _dimension_values(observation.content_dimensions.get(axis))
        })
        if len(values) < 2:
            coverage_gaps.append(
                f"[CROSS_DIMENSION_SAMPLE_INSUFFICIENT] axis={axis} distinct_values={len(values)}"
            )
        for value in values:
            result = _dimension_result(
                axis=axis,
                value=value,
                observations=observations,
                estimate_map=estimate_map,
            )
            dimension_results.append(result)
            if result.correlation is None:
                stability_issues.append(
                    f"[DIMENSION_CORRELATION_NOT_ESTIMABLE] axis={axis} value={value}"
                )
            elif result.correlation < minimum_correlation:
                stability_issues.append(
                    f"[DIMENSION_CORRELATION_BELOW_THRESHOLD] axis={axis} "
                    f"value={value} correlation={result.correlation:.3f}"
                )

    estimated_dimension_correlations = [
        item.correlation for item in dimension_results if item.correlation is not None
    ]
    if overall_correlation is not None and estimated_dimension_correlations:
        if any(value == 0.0 or value * overall_correlation <= 0.0
               for value in estimated_dimension_correlations):
            stability_issues.append(
                "[CORRELATION_UNSTABLE_ACROSS_DIMENSIONS] 分组相关方向与总体不一致"
            )

    # Leave-one-scope-out checks whether a single work determines the result.
    leave_one_scope_out: dict[str, float | None] = {}
    scope_ids = sorted(screenplay_by_scope)
    if len(scope_ids) < 2:
        coverage_gaps.append(
            f"[CROSS_SCOPE_SAMPLE_INSUFFICIENT] distinct_scopes={len(scope_ids)}"
        )
    else:
        for held_out in scope_ids:
            subset = [item for item in paired_results if item.scope_id != held_out]
            correlation = _pearson(
                [float(item.model_predicted_score) for item in subset],
                [float(item.human_mean_score) for item in subset],
            )
            leave_one_scope_out[held_out] = correlation
            if correlation is None:
                stability_issues.append(
                    f"[LEAVE_ONE_SCOPE_CORRELATION_NOT_ESTIMABLE] held_out={held_out}"
                )
            elif overall_correlation is not None and (
                correlation == 0.0 or correlation * overall_correlation <= 0.0
            ):
                stability_issues.append(
                    f"[CORRELATION_UNSTABLE_BY_SCOPE] held_out={held_out}"
                )

    model_pass_threshold, threshold_analysis, threshold_issues = (
        _derive_model_pass_threshold(
            paired_results,
            human_success_threshold=human_success_threshold,
        )
    )
    stability_issues.extend(threshold_issues)
    coverage_gaps = list(dict.fromkeys(coverage_gaps))
    stability_issues = list(dict.fromkeys(stability_issues))
    calibrated = (
        not coverage_gaps
        and not stability_issues
        and overall_correlation is not None
        and model_pass_threshold is not None
    )
    reason_items = [*coverage_gaps, *stability_issues]
    return CalibrationReport(
        calibration_report_id=calibration_report_id,
        calibration_scope_id=calibration_scope_id,
        scope_ids=scope_ids,
        observation_ids=sorted(observation_ids),
        narrative_review_artifact_ids=sorted({
            *[item.narrative_review_artifact_id for item in observations],
            *[item.narrative_review_artifact_id for item in model_estimates],
        }),
        required_dimension_axes=axes,
        sample_summary={
            "observation_count": len(observations),
            "participant_count": len({item.participant_id_hash for item in observations}),
            "expected_pair_count": len(expected),
            "paired_target_count": len(paired_results),
            "scope_count": len(scope_ids),
            "per_prior_expected_target_counts": {
                f"{scope}/{prior}": len(targets)
                for (scope, prior), targets in sorted(by_prior.items())
            },
        },
        pair_results=pair_results,
        dimension_results=dimension_results,
        correlation_analysis={
            "overall_pearson": overall_correlation,
            "leave_one_scope_out": leave_one_scope_out,
            "model_scores_constant": model_constant,
            "human_scores_constant": human_constant,
            "stability_rule": (
                "总体及可估计的跨维度/留一作品相关方向必须一致；"
                "无法估计时保持 needs_review"
            ),
        },
        coverage_gaps=coverage_gaps,
        stability_issues=stability_issues,
        calibration_score=overall_correlation if calibrated else None,
        minimum_correlation=minimum_correlation,
        human_success_threshold=human_success_threshold,
        recommended_model_pass_threshold=(
            model_pass_threshold if calibrated else None
        ),
        threshold_analysis=threshold_analysis,
        confidence_status="supported" if calibrated else "needs_review",
        decision="calibrated" if calibrated else "needs_review",
        reason=(
            "跨作品与跨维度成对证据可估计且方向稳定"
            if calibrated else "；".join(reason_items) or "校准证据需人工复核"
        ),
        evidence_lineage={
            "observation_contract_version": HUMAN_ONE_WATCH_CONTRACT_VERSION,
            "model_estimate_review_artifact_ids": sorted({
                item.narrative_review_artifact_id for item in model_estimates
            }),
        },
    )


def persist_calibration_report(
    report: CalibrationReport,
    *,
    observation_artifact_ids: Sequence[str],
    narrative_review_artifact_ids: Sequence[str],
) -> dict[str, Any]:
    """Persist a calibration report and bind every raw/review parent."""
    review_ids = list(dict.fromkeys(str(item) for item in narrative_review_artifact_ids if str(item)))
    _require_review_lineage(review_ids)
    if set(report.narrative_review_artifact_ids) - set(review_ids):
        raise CalibrationContractError([
            "[CALIBRATION_REVIEW_LINEAGE_INCOMPLETE] report 引用的 review artifact 未全部纳入父链"
        ])
    observation_ids = list(dict.fromkeys(str(item) for item in observation_artifact_ids if str(item)))
    observed_report_ids: set[str] = set()
    errors: list[str] = []
    for artifact_id in observation_ids:
        observation, observation_errors = _verified_human_observation_artifact(
            artifact_id,
            allowed_review_ids=set(review_ids),
        )
        errors.extend(observation_errors)
        if observation is not None:
            observed_report_ids.add(observation.observation_id)
    missing_observations = set(report.observation_ids) - observed_report_ids
    extra_observations = observed_report_ids - set(report.observation_ids)
    if missing_observations:
        errors.append(
            f"[CALIBRATION_OBSERVATION_LINEAGE_INCOMPLETE] {sorted(missing_observations)}"
        )
    if extra_observations:
        errors.append(
            f"[CALIBRATION_OBSERVATION_LINEAGE_EXTRA] {sorted(extra_observations)}"
        )
    if errors:
        raise CalibrationContractError(errors)
    parents = list(dict.fromkeys([*review_ids, *observation_ids]))
    calibrated = report.decision == "calibrated"
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="human_one_watch_calibration_report",
        scope_type="calibration",
        scope_id=report.calibration_scope_id,
        status="candidate" if calibrated else "needs_revision",
        trust_level="T4",
        content=report.model_dump(mode="json"),
        parent_artifact_ids=parents,
        contract_version=HUMAN_CALIBRATION_CONTRACT_VERSION,
    ))
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="human_one_watch_calibration",
        evaluator_version=HUMAN_CALIBRATION_CONTRACT_VERSION,
        status="passed" if calibrated else "warning",
        hard_gate_passed=calibrated,
        evaluation_role="runtime_gate" if calibrated else "score_only",
        score_status="calibrated" if calibrated else "unknown",
        runtime_blocking=calibrated,
        score=(
            max(0.0, min(100.0, float(report.calibration_score) * 100.0))
            if calibrated and report.calibration_score is not None else None
        ),
        evidence={
            "decision": report.decision,
            "coverage_gaps": report.coverage_gaps,
            "stability_issues": report.stability_issues,
            "narrative_review_artifact_ids": review_ids,
            "observation_artifact_ids": observation_ids,
            "model_pass_threshold": report.recommended_model_pass_threshold,
            "narrative_contract_version": report.narrative_contract_version,
            "blind_prompt_version": report.blind_prompt_version,
            "comparator_prompt_version": report.comparator_prompt_version,
        },
    )
    if calibrated:
        return evidence_repository.commit_artifact(
            None,
            artifact["id"],
            [evaluation],
        )
    evidence_repository.create_evaluation(artifact["id"], evaluation)
    return evidence_repository.get_artifact(artifact["id"]) or artifact


def require_current_calibration_authority(
    *,
    expected_artifact_id: str | None = None,
) -> CurrentCalibrationAuthority:
    """Resolve the active cross-content human calibration or fail closed."""
    conn = evidence_repository.get_conn()
    row = conn.execute(
        """SELECT id FROM artifacts
           WHERE type='human_one_watch_calibration_report'
             AND scope_type='calibration' AND scope_id=?
             AND status='approved'
           ORDER BY version DESC LIMIT 1""",
        (GLOBAL_CALIBRATION_SCOPE_ID,),
    ).fetchone()
    if row is None:
        raise CalibrationContractError([
            "[NARRATIVE_CALIBRATION_REQUIRED] 尚无通过跨作品真人一次观看校准的当前权威"
        ])
    artifact_id = str(row["id"])
    if expected_artifact_id and artifact_id != str(expected_artifact_id):
        raise CalibrationContractError([
            "[NARRATIVE_CALIBRATION_STALE] 分镜绑定的真人校准版本已不是当前权威"
        ])
    artifact = evidence_repository.get_artifact(artifact_id)
    if artifact is None:
        raise CalibrationContractError([
            "[NARRATIVE_CALIBRATION_ARTIFACT_MISSING] 当前校准 Artifact 不存在"
        ])
    current_hash = evidence_repository.content_hash(
        artifact.get("content"),
        artifact.get("file_path"),
    )
    if current_hash != artifact.get("content_hash"):
        raise CalibrationContractError([
            "[NARRATIVE_CALIBRATION_HASH_DRIFT] 当前校准内容与存储指纹不一致"
        ])
    try:
        report = CalibrationReport.model_validate(artifact.get("content") or {})
    except Exception as exc:  # noqa: BLE001 - immutable authority boundary
        raise CalibrationContractError([
            f"[NARRATIVE_CALIBRATION_SCHEMA_INVALID] {exc}"
        ]) from exc
    errors: list[str] = []
    if report.calibration_scope_id != GLOBAL_CALIBRATION_SCOPE_ID:
        errors.append("[NARRATIVE_CALIBRATION_SCOPE_INVALID] 校准作用域不是全局叙事合同")
    if report.decision != "calibrated" or report.confidence_status != "supported":
        errors.append("[NARRATIVE_CALIBRATION_NOT_SUPPORTED] 当前校准结论未达到 supported")
    if report.narrative_contract_version != NARRATIVE_CONTRACT_VERSION:
        errors.append("[NARRATIVE_CALIBRATION_CONTRACT_DRIFT] 叙事合同版本已变化")
    if report.blind_prompt_version != BLIND_READER_PROMPT_VERSION:
        errors.append("[NARRATIVE_CALIBRATION_BLIND_PROMPT_DRIFT] 冷观众合同版本已变化")
    if report.comparator_prompt_version != COMPARATOR_PROMPT_VERSION:
        errors.append("[NARRATIVE_CALIBRATION_COMPARATOR_DRIFT] 比较器合同版本已变化")
    threshold = report.recommended_model_pass_threshold
    if threshold is None or not 0 <= float(threshold) <= 1:
        errors.append("[NARRATIVE_CALIBRATION_THRESHOLD_MISSING] 当前校准没有有效模型门槛")
    if (
        report.calibration_score is None
        or report.calibration_score < report.minimum_correlation
    ):
        errors.append("[NARRATIVE_CALIBRATION_CORRELATION_LOW] 当前相关性未达到校准阈值")
    parents = set(artifact.get("parent_artifact_ids") or [])
    if not set(report.narrative_review_artifact_ids).issubset(parents):
        errors.append("[NARRATIVE_CALIBRATION_LINEAGE_INCOMPLETE] 当前校准缺少审读父证据")
    try:
        _require_review_lineage(report.narrative_review_artifact_ids)
    except CalibrationContractError as exc:
        errors.extend(exc.errors)
    observation_parent_ids = [
        str(parent_id)
        for parent_id in parents
        if (
            (parent := evidence_repository.get_artifact(str(parent_id)))
            is not None
            and parent.get("type") == "human_one_watch_observation"
        )
    ]
    verified_observation_ids: set[str] = set()
    for observation_artifact_id in observation_parent_ids:
        observation, observation_errors = _verified_human_observation_artifact(
            observation_artifact_id,
            allowed_review_ids=set(report.narrative_review_artifact_ids),
        )
        errors.extend(observation_errors)
        if observation is not None:
            verified_observation_ids.add(observation.observation_id)
    if verified_observation_ids != set(report.observation_ids):
        errors.append(
            "[NARRATIVE_CALIBRATION_OBSERVATION_LINEAGE_INVALID] "
            "校准报告与真人观察父证据不完全一致"
        )
    gate_rows = [
        item
        for item in evidence_repository.get_evaluations(artifact_id)
        if item.get("evaluator_name") == "human_one_watch_calibration"
        and item.get("evaluator_version") == HUMAN_CALIBRATION_CONTRACT_VERSION
        and item.get("evaluation_role") == "runtime_gate"
        and bool(item.get("runtime_blocking"))
        and item.get("status") == "passed"
        and bool(item.get("hard_gate_passed"))
    ]
    if len(gate_rows) != 1:
        errors.append("[NARRATIVE_CALIBRATION_GATE_INVALID] 当前校准缺少唯一通过的 runtime gate")
    if errors:
        raise CalibrationContractError(errors)
    return CurrentCalibrationAuthority(
        artifact_id=artifact_id,
        artifact_hash=current_hash,
        report=report,
        model_pass_threshold=float(threshold),
    )


def assert_report_meets_current_calibration(
    report: NarrativeReviewReport,
    *,
    expected_calibration_artifact_id: str | None = None,
) -> CurrentCalibrationAuthority:
    """Apply the human-derived threshold to one frozen AI review report."""
    authority = require_current_calibration_authority(
        expected_artifact_id=expected_calibration_artifact_id,
    )
    failed = [
        (
            result.audience_prior_id,
            result.target_delta_id,
            result.predicted_score,
        )
        for result in report.target_delta_results
        if result.predicted_score is None
        or float(result.predicted_score) < authority.model_pass_threshold
    ]
    if failed:
        raise CalibrationContractError([
            "[NARRATIVE_REVIEW_BELOW_HUMAN_CALIBRATED_THRESHOLD] "
            f"以下逐先验目标低于 {authority.model_pass_threshold:.3f}：{failed}"
        ])
    return authority


def calibrate_and_persist_human_one_watch(
    *,
    calibration_report_id: str,
    calibration_scope_id: str,
    screenplays: Sequence[EpisodeScreenplay],
    observations: Sequence[HumanOneWatchObservation],
    model_estimates: Sequence[ModelTargetEstimate],
    observation_artifact_ids: Sequence[str],
    narrative_review_artifact_ids: Sequence[str],
    required_dimension_axes: Sequence[str] = DEFAULT_CROSS_CONTENT_DIMENSIONS,
    minimum_correlation: float = DEFAULT_MINIMUM_CORRELATION,
    human_success_threshold: float = DEFAULT_HUMAN_SUCCESS_THRESHOLD,
) -> tuple[CalibrationReport, dict[str, Any]]:
    """Public end-to-end service for building and persisting calibration."""
    report = build_calibration_report(
        calibration_report_id=calibration_report_id,
        calibration_scope_id=calibration_scope_id,
        screenplays=screenplays,
        observations=observations,
        model_estimates=model_estimates,
        required_dimension_axes=required_dimension_axes,
        minimum_correlation=minimum_correlation,
        human_success_threshold=human_success_threshold,
    )
    artifact = persist_calibration_report(
        report,
        observation_artifact_ids=observation_artifact_ids,
        narrative_review_artifact_ids=narrative_review_artifact_ids,
    )
    return report, artifact
