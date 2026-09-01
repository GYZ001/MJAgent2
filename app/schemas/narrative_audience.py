"""观众认知契约：角色戏剧状态、观众先验/信念快照与体验意图路径。

这一组模型描述"观众在每个时间点知道什么、相信什么、被引导去经历什么"，
是叙事一致性校验判断信息是否被正确交付给观众的基础。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .narrative_core import NarrativeAnchor

class CharacterDramaticState(BaseModel):
    character_state_id: str
    character_id: str
    anchor: NarrativeAnchor
    goal_proposition_ids: list[str] = Field(default_factory=list)
    stakes_proposition_ids: list[str] = Field(default_factory=list)
    relationship_state: dict = Field(default_factory=dict)
    emotion: dict = Field(default_factory=dict)
    pressure: float = 0.0
    tactic: str = ""


class BeliefItem(BaseModel):
    proposition_id: str
    stance: str = "unknown"
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)


class CharacterBeliefSnapshot(BaseModel):
    character_belief_id: str
    character_id: str
    anchor: NarrativeAnchor
    perceived_evidence_ids: list[str] = Field(default_factory=list)
    beliefs: list[BeliefItem] = Field(default_factory=list)
    misbelief_proposition_ids: list[str] = Field(default_factory=list)
    decision_proposition_ids: list[str] = Field(default_factory=list)
    decision_basis_ids: list[str] = Field(default_factory=list)
    decision_action_ids: list[str] = Field(default_factory=list)


class AudienceStateSnapshot(BaseModel):
    audience_state_id: str
    audience_prior_id: str
    anchor: NarrativeAnchor
    beliefs: list[BeliefItem] = Field(default_factory=list)
    causal_hypotheses: list[dict | str] = Field(default_factory=list)
    character_goal_hypotheses: dict = Field(default_factory=dict)
    spatial_model: dict = Field(default_factory=dict)
    temporal_model: dict = Field(default_factory=dict)
    active_question_ids: list[str] = Field(default_factory=list)
    working_memory: list[dict] = Field(default_factory=list)
    attention_residue_ids: list[str] = Field(default_factory=list)
    affective_state: dict = Field(default_factory=dict)


class AudiencePriorContract(BaseModel):
    audience_prior_id: str
    scope_id: str = "episode"
    audience_description: str
    assumed_known_proposition_ids: list[str] = Field(default_factory=list)
    assumed_unknown_proposition_ids: list[str] = Field(default_factory=list)
    familiarity_assumptions: list[dict] = Field(default_factory=list)
    language_and_context_assumptions: list[str] = Field(default_factory=list)
    attention_memory_assumptions: dict = Field(default_factory=dict)
    calibration_source: str = "needs_review"


class TargetDelta(BaseModel):
    target_delta_id: str
    dimension: str
    proposition_ids: list[str] = Field(default_factory=list)
    description: str
    from_state: dict = Field(default_factory=dict)
    to_state: dict = Field(default_factory=dict)
    target_confidence: float | None = None
    required_processing_s: float = 0.0
    deadline_event_id: str
    primary_delivery_window_id: str | None = None
    custom_dimension: str | None = None


class AudiencePath(BaseModel):
    audience_path_id: str
    audience_prior_id: str
    audience_state_in_id: str
    audience_state_out_target_id: str
    target_deltas: list[TargetDelta] = Field(default_factory=list)


class WithheldProposition(BaseModel):
    proposition_id: str
    reason: str
    future_disclosure_anchor: NarrativeAnchor | None = None
    carried_question_id: str | None = None


class ExperienceIntent(BaseModel):
    experience_intent_id: str
    scope_id: str
    anchor_event_ids: list[str] = Field(default_factory=list)
    director_objective: str
    attention_target_ids: list[str] = Field(default_factory=list)
    audience_paths: list[AudiencePath] = Field(default_factory=list)
    withheld_propositions: list[WithheldProposition] = Field(default_factory=list)
    forbidden_misconceptions: list[str] = Field(default_factory=list)


class AssimilationTask(BaseModel):
    assimilation_task_id: str
    experience_intent_id: str
    audience_path_id: str
    target_delta_id: str
    required_prior_proposition_ids: list[str] = Field(default_factory=list)
    downstream_dependency_event_ids: list[str] = Field(default_factory=list)
    satisfaction_criteria: str
    status: str = "open"
