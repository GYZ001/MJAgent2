"""Patch-related Pydantic models: node replacement, blueprint patch, semantic issue, state-subject ownership repair/patch, and the semantic review envelope."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models_core import NarrativeNode


class NarrativeNodeReplacement(BaseModel):
    node_key: str
    node: NarrativeNode | None = None
    nodes: list[NarrativeNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_nodes(self) -> NarrativeNodeReplacement:
        if not self.nodes and self.node is not None:
            self.nodes = [self.node]
        if not self.nodes:
            raise ValueError("replacement 必须提供 node 或 nodes")
        return self


class NarrativeBlueprintPatch(BaseModel):
    replacements: list[NarrativeNodeReplacement] = Field(
        default_factory=list,
    )
    delete_node_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_change(self) -> NarrativeBlueprintPatch:
        if not self.replacements and not self.delete_node_keys:
            raise ValueError("蓝图补丁必须替换或删除至少一个节点")
        return self


class BlueprintSemanticIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "timeline_conflict",
        "spatial_action_gap",
        "persistent_state_conflict",
        "motivation_gap",
        "agency_conflict",
        "setup_missing",
        "identity_or_role_conflict",
        "voice_identity_missing",
        "voice_identity_ambiguous",
        "voice_identity_conflict",
        "source_delivery_missing",
        "source_delivery_conflict",
        "source_delivery_identity_conflict",
        "state_subject_missing",
        "state_subject_ambiguous",
        "state_subject_unit_missing",
        "state_subject_unit_invalid",
        "state_subject_assignment_invalid",
        "state_subject_assignment_ambiguous",
        "state_subject_assignment_conflict",
        "state_subject_environment_duplicate",
        "state_subject_environment_invalid",
        "state_subject_environment_conflict",
        "state_subject_environment_non_picture",
        "state_subject_environment_misclassified",
        "state_subject_adjudication_invalid",
        "state_subject_perception_missing",
        "ending_payoff_gap",
    ]
    node_keys: list[str]
    source_segment_ids: list[str] = Field(default_factory=list)
    source_unit_keys: list[str] = Field(default_factory=list)
    message: str
    required_resolution: str
    must_fix: bool = True


def render_blueprint_shard_semantic_issue(
    issue: BlueprintSemanticIssue,
) -> str:
    """Render typed semantic issues exactly once for shard validation."""
    return (
        f"[BLUEPRINT_SHARD_{issue.code.upper()}] "
        f"{'、'.join(issue.node_keys)} "
        f"{'、'.join(issue.source_segment_ids)}：{issue.message}；"
        f"必须：{issue.required_resolution}"
    )


class BlueprintStateSubjectOwnershipRepair(BaseModel):
    """One exact-unit ownership replacement selected by the model."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["single", "joint", "environment"]
    identity_keys: list[str]

    @model_validator(mode="after")
    def _validate_mode_identity_shape(
        self,
    ) -> BlueprintStateSubjectOwnershipRepair:
        normalized = [
            str(identity_key or "").strip()
            for identity_key in self.identity_keys
        ]
        if any(not identity_key for identity_key in normalized):
            raise ValueError("ownership repair identity_keys 不得含空值")
        if len(normalized) != len(set(normalized)):
            raise ValueError("ownership repair identity_keys 不得重复")
        expected = {
            "single": len(normalized) == 1,
            "joint": len(normalized) >= 2,
            "environment": not normalized,
        }[self.mode]
        if not expected:
            raise ValueError(
                "single 必须恰有一个 identity，joint 必须至少两个唯一 "
                "identity，environment 必须为空"
            )
        self.identity_keys = normalized
        return self


class BlueprintStateSubjectOwnershipPatch(BaseModel):
    """Atomic repair map bound to one exact normalized shard candidate."""

    model_config = ConfigDict(extra="forbid")

    base_candidate_hash: str
    repairs: dict[str, BlueprintStateSubjectOwnershipRepair]

    @model_validator(mode="after")
    def _require_repairs(self) -> BlueprintStateSubjectOwnershipPatch:
        if not self.repairs:
            raise ValueError("ownership repair patch 不得为空")
        return self


class BlueprintSemanticReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[BlueprintSemanticIssue] = Field(default_factory=list)
