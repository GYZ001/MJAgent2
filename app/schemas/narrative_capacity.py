"""镜头容量与戏剧结构契约：单镜时间预算、可读性窗口与场/集戏剧弧光。

ShotCapacityBudget 描述的是观看工作量维度，不是剧情分类；确定性校验据此从
动作阶段/口播文本/证据/目标增量推导下限，再核对单镜时长是否装得下。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

class ShotContribution(BaseModel):
    shot_contribution_id: str
    experience_intent_ids: list[str] = Field(default_factory=list)
    target_delta_ids: list[str] = Field(default_factory=list)
    assimilation_task_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    story_delta_fact_ids: list[str] = Field(default_factory=list)
    character_state_delta_ids: list[str] = Field(default_factory=list)
    audience_state_delta_ids: list[str] = Field(default_factory=list)
    affective_delta: dict = Field(default_factory=dict)
    spatial_temporal_delta: dict = Field(default_factory=dict)
    dramatic_pressure_delta: float = 0.0


class ShotCapacityBudget(BaseModel):
    """Joint single-shot time budget proposed by AI and relation-checked.

    The dimensions describe viewing work, not story categories.  Deterministic
    validation derives lower bounds from action phases, spoken/on-screen text,
    evidence and target deltas, then verifies the joint total against the shot.
    """

    action_phase_s: float = 0.0
    spoken_and_text_s: float = 0.0
    attention_switch_s: float = 0.0
    inference_processing_s: float = 0.0
    reaction_registration_s: float = 0.0
    spatial_reorientation_s: float = 0.0
    entry_exit_settle_s: float = 0.0
    other_s: float = 0.0
    other_reason: str | None = None


class ReadabilityWindow(BaseModel):
    readability_window_id: str
    event_ids: list[str] = Field(default_factory=list)
    proposition_ids: list[str] = Field(default_factory=list)
    target_delta_ids: list[str] = Field(default_factory=list)
    shot_ids: list[str] = Field(default_factory=list)
    attention_target_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    scheduled_processing_s: float = 0.0
    planned_available_s: float = 0.0
    competing_attention_ids: list[str] = Field(default_factory=list)
    readability_reason: str = ""
    status: str = "planned"


class SetupPayoffContract(BaseModel):
    setup_payoff_id: str
    setup_proposition_ids: list[str] = Field(default_factory=list)
    setup_event_ids: list[str] = Field(default_factory=list)
    payoff_event_ids: list[str] = Field(default_factory=list)
    intended_inference_ids: list[str] = Field(default_factory=list)
    retention_deadline_event_id: str = ""
    minimum_retention_confidence: float = 0.0
    recall_needed: bool | None = None
    status: str = "open"


class AudienceStatePathRef(BaseModel):
    audience_prior_id: str
    audience_state_in_id: str
    audience_state_out_target_id: str


class SceneDramaticContract(BaseModel):
    scene_id: str
    applicability: str = "applies"
    not_applicable_reason: str | None = None
    alternative_dramatic_function: str | None = None
    scene_question_id: str | None = None
    point_of_view_character_id: str | None = None
    audience_state_paths: list[AudienceStatePathRef] = Field(default_factory=list)
    character_state_in_ids: list[str] = Field(default_factory=list)
    goal_proposition_ids: list[str] = Field(default_factory=list)
    obstacle_proposition_ids: list[str] = Field(default_factory=list)
    stakes_proposition_ids: list[str] = Field(default_factory=list)
    pressure_curve: list[dict] = Field(default_factory=list)
    turn_event_ids: list[str] = Field(default_factory=list)
    value_polarity_in: str = ""
    value_polarity_out: str = ""
    relationship_deltas: list[dict] = Field(default_factory=list)
    character_state_out_ids: list[str] = Field(default_factory=list)
    scene_button: str = ""


class NarrativeArcContract(BaseModel):
    arc_id: str
    scope: str = "episode"
    applicability: str = "applies"
    not_applicable_reason: str | None = None
    alternative_dramatic_function: str | None = None
    core_question_ids: list[str] = Field(default_factory=list)
    promise_proposition_ids: list[str] = Field(default_factory=list)
    escalation_event_ids: list[str] = Field(default_factory=list)
    climax_event_ids: list[str] = Field(default_factory=list)
    payoff_contract_ids: list[str] = Field(default_factory=list)
    pressure_curve: list[dict] = Field(default_factory=list)
    information_density_curve: list[dict] = Field(default_factory=list)
    processing_beats: list[dict] = Field(default_factory=list)
    ending_hook_question_ids: list[str] = Field(default_factory=list)
    resolved_question_ids: list[str] = Field(default_factory=list)
    carried_question_ids: list[str] = Field(default_factory=list)
