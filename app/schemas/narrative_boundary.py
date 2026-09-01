"""镜头切换边界契约：状态差异的可审计依据与认知桥接（叙事整合）规划。

BoundaryStateTransition 的 basis 描述结构性关系（时间线/视角/空间模型/动作
阶段），从不用剧情对象或分类；未知关系仍可用 ``other`` 表达，但发布前需人审。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

class BoundaryStateTransition(BaseModel):
    """Auditable reason for a world-state difference across a cut.

    The basis describes a structural relation (timeline, viewpoint, spatial
    model or an action phase), never a story object/category.  Unknown
    relations remain representable through ``other`` but require human review
    before narrative-ready publication.
    """

    transition_id: str
    basis_type: str
    source_fact_id: str | None = None
    target_fact_id: str | None = None
    basis_action_phase_id: str | None = None
    custom_basis: str | None = None
    reason: str = ""


class NarrativeBoundaryContract(BaseModel):
    boundary_id: str
    previous_shot_id: str
    next_shot_id: str
    narrative_relation: str
    required_state_invariants: list[str] = Field(default_factory=list)
    allowed_state_deltas: list[str] = Field(default_factory=list)
    state_delta_transitions: list[BoundaryStateTransition] = Field(default_factory=list)
    forbidden_replay_action_ids: list[str] = Field(default_factory=list)
    handoff_action_phase_id: str | None = None
    spatial_orientation_contract: dict = Field(default_factory=dict)
    temporal_orientation_contract: dict = Field(default_factory=dict)
    audience_state_handoffs: list[dict] = Field(default_factory=list)
    affective_handoff: dict = Field(default_factory=dict)
    cut_motivation: str


class CognitiveBridgePlan(BaseModel):
    bridge_plan_id: str
    assimilation_task_ids: list[str] = Field(default_factory=list)
    candidate_changes: list[dict] = Field(default_factory=list)
    expected_audience_delta: dict = Field(default_factory=dict)
    affected_shot_ids: list[str] = Field(default_factory=list)
    added_shot_ids: list[str] = Field(default_factory=list)
    removed_shot_ids: list[str] = Field(default_factory=list)
    estimated_screen_time_delta: float = 0.0
    deletion_test_result: dict = Field(default_factory=dict)
    marginal_gain_result: dict = Field(default_factory=dict)
    selection_reason: str = ""
