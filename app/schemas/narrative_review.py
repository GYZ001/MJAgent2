"""盲审契约：冷读观众的自发回忆快照与目标增量达成结果的可审计对比。

BlindSpontaneousRecall 是封闭契约（extra=forbid）；supporting_observation_ids/
supporting_evidence_ids 刻意不出现在导演侧目标契约里，避免冷读结论被提示词
预先"喂答案"。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .narrative_core import NarrativeAnchor

class BlindSpontaneousRecall(BaseModel):
    """Closed model-output contract; unknown fields fail schema validation."""

    model_config = ConfigDict(extra="forbid")

    recognized_entities: list[Any]
    inferred_propositions: list[Any]
    causal_hypotheses: list[Any]
    character_goal_hypotheses: list[Any]
    active_question_ids: list[str]


class BlindAudienceObservation(BaseModel):
    observation_id: str
    audience_prior_id: str
    anchor: NarrativeAnchor
    spontaneous_recall: BlindSpontaneousRecall
    neutral_followup_observations: list[dict | str] = Field(default_factory=list)
    noticed_attention_target_ids: list[str] = Field(default_factory=list)
    spatial_temporal_model: dict = Field(default_factory=dict)
    felt_affective_state: dict = Field(default_factory=dict)
    perceived_relationship_deltas: list[dict] = Field(default_factory=list)
    perceived_stakes: list[str] = Field(default_factory=list)
    experienced_pressure_curve: list[dict] = Field(default_factory=list)
    experienced_rhythm: dict = Field(default_factory=dict)
    next_event_expectations: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    # Evidence handles explicitly present in the frozen, unprompted first pass.
    # Follow-up observations may add to supporting_evidence_ids but can never
    # retroactively populate this ledger.
    spontaneous_supporting_evidence_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class TargetDeltaResult(BaseModel):
    audience_prior_id: str
    target_delta_id: str
    result: str
    predicted_score: float | None = None
    # The comparator must make every conclusion auditable.  Observation IDs
    # point at frozen first-pass recalls; evidence IDs point at opaque handles
    # the cold reader actually saw.  They are deliberately absent from the
    # director-facing target contract and therefore cannot prompt the answer.
    supporting_observation_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class NarrativeReviewReport(BaseModel):
    narrative_review_report_id: str
    scope_id: str
    experience_intent_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    target_delta_results: list[TargetDeltaResult] = Field(default_factory=list)
    character_goal_readability_result: dict = Field(default_factory=dict)
    attention_alignment_result: dict = Field(default_factory=dict)
    spatial_temporal_orientation_result: dict = Field(default_factory=dict)
    affective_alignment_result: dict = Field(default_factory=dict)
    relationship_change_result: dict = Field(default_factory=dict)
    stakes_readability_result: dict = Field(default_factory=dict)
    pressure_rhythm_result: dict = Field(default_factory=dict)
    action_functional_repetition_result: dict = Field(default_factory=dict)
    next_expectation_result: dict = Field(default_factory=dict)
    intentional_ambiguity_result: dict = Field(default_factory=dict)
    low_percentile_result: dict = Field(default_factory=dict)
    inference_variance: float = 0.0
    evidence_gap_ids: list[str] = Field(default_factory=list)
    unintended_inference_ids: list[str] = Field(default_factory=list)
    decision: str = "needs_human_review"
    reason: str = ""
