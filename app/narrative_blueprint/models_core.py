"""Core NarrativeBlueprint Pydantic models (state requirements/changes/decisions, participant/state-subject/source-unit evidence, source semantics, nodes, scene plans, source-audit/ownership/occurrence types), the source-occurrence issue producer, and the top-level NarrativeBlueprint/NarrativeBlueprintShard envelope models. These two envelope classes are kept with the other models (not with their own shard validator) because nearly every validator function elsewhere in this package takes one as a parameter type -- keeping them next to validate_narrative_blueprint_shard would make every one of those a circular import."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import AUDIBLE_SOURCE_DELIVERY_MODES, BLUEPRINT_VERSION
from .provider_normalize import _normalize_source_segment_id


class BlueprintStateRequirement(BaseModel):
    state_key: str
    required_fact_key: str = ""
    expected_value: str = ""
    reason: str
    assumed_prior: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_expected_value(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["expected_value"] = str(
            normalized.get("expected_value")
            or normalized.get("value")
            or normalized.get("required_value")
            or normalized.get("state_value")
            or ""
        )
        normalized.setdefault(
            "required_fact_key",
            normalized.get("fact_key")
            or normalized.get("depends_on_fact_key")
            or "",
        )
        return normalized


class BlueprintStateChange(BaseModel):
    fact_key: str
    state_key: str
    value: str
    reason: str
    supersedes_fact_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.setdefault(
            "value",
            normalized.get("new_value")
            or normalized.get("expected_value")
            or normalized.get("state_value")
            or normalized.get("current_value"),
        )
        return normalized


class BlueprintDecision(BaseModel):
    actor_key: str
    choice: str
    impact: Literal["routine", "major"] = "routine"
    setup_node_keys: list[str] = Field(default_factory=list)
    pressure: str = ""
    desire: str = ""
    agency_mode: Literal[
        "voluntary", "reluctant", "coerced", "incapacitated", "unclear",
    ] = "unclear"
    agency_change_reason: str = ""
    constraint_fact_key: str = ""
    constraint_release_node_keys: list[str] = Field(default_factory=list)
    narrative_attribution: Literal[
        "voluntary_choice",
        "external_coercion",
        "impaired_capacity",
        "unclear",
    ] = "unclear"

    @model_validator(mode="after")
    def _bind_agency_attribution(self) -> BlueprintDecision:
        self.narrative_attribution = {
            "voluntary": "voluntary_choice",
            "reluctant": "voluntary_choice",
            "coerced": "external_coercion",
            "incapacitated": "impaired_capacity",
            "unclear": "unclear",
        }[self.agency_mode]
        return self


class NarrativeParticipantEvidence(BaseModel):
    identity_key: str
    source_segment_ids: list[str] = Field(default_factory=list)
    source_unit_keys: list[str] = Field(default_factory=list)
    usage: Literal["visible", "voice", "mentioned", "state_subject"]

    @field_validator("source_segment_ids", mode="before")
    @classmethod
    def _normalize_source_ids(cls, value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        return [_normalize_source_segment_id(item) for item in values]


class NarrativeStateSubjectAssignment(BaseModel):
    """Typed ownership for one structurally indivisible joint action."""

    model_config = ConfigDict(extra="forbid")

    source_unit_key: str
    mode: Literal["joint"]
    identity_keys: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def _validate_joint_identities(self) -> NarrativeStateSubjectAssignment:
        normalized = [
            str(identity_key or "").strip()
            for identity_key in self.identity_keys
        ]
        if any(not identity_key for identity_key in normalized):
            raise ValueError("joint state subject identity_keys 不得为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("joint state subject identity_keys 不得重复")
        self.identity_keys = normalized
        return self


class NarrativeSourceUnitDelivery(BaseModel):
    """Semantic delivery decision for one structurally quoted source unit."""

    model_config = ConfigDict(extra="forbid")

    source_unit_key: str
    mode: Literal[
        "spoken_dialogue",
        "offscreen_voice",
        "written_text",
        "sound_effect",
        "unspoken_reference",
    ]
    content_owner_key: str = ""
    performer_key: str = ""

    @model_validator(mode="after")
    def _validate_delivery_roles(self) -> "NarrativeSourceUnitDelivery":
        audible = self.mode in {
            "spoken_dialogue",
            "offscreen_voice",
        }
        if audible and not self.performer_key.strip():
            raise ValueError(
                "可听引用单元必须填写 performer_key"
            )
        if not audible and self.performer_key.strip():
            raise ValueError(
                "非声音交付不得填写 performer_key"
            )
        return self


class BlueprintSourceSemantics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative_layer: Literal["story", "paratext"]
    event_priority: Literal["causal", "connective"]
    render_policy: Literal["standalone", "exclude_from_spine"]
    disposition: Literal["deliver", "audit_only"]
    projection_policy: Literal["picture", "audit_only"]

    @model_validator(mode="after")
    def _validate_projection(self) -> BlueprintSourceSemantics:
        expected = (
            ("causal", "standalone", "deliver", "picture")
            if self.narrative_layer == "story"
            else (
                "connective",
                "exclude_from_spine",
                "audit_only",
                "audit_only",
            )
        )
        actual = (
            self.event_priority,
            self.render_policy,
            self.disposition,
            self.projection_policy,
        )
        if actual != expected:
            raise ValueError(
                f"{self.narrative_layer} 来源语义必须为 {expected}"
            )
        return self


class NarrativeNode(BaseModel):
    key: str
    source_segment_ids: list[str]
    summary: str
    narrative_layer: Literal["story", "paratext"]
    event_priority: Literal["causal", "connective"]
    render_policy: Literal["standalone", "exclude_from_spine"]
    temporal_domain_key: str
    time_label: str
    time_relation: Literal[
        "episode_start",
        "continuous",
        "elapsed",
        "jump",
        "flashback_enter",
        "flashback_continue",
        "flashback_exit",
        "montage",
    ]
    location_key: str
    location_label: str
    participants: list[str] = Field(default_factory=list)
    participant_evidence: list[NarrativeParticipantEvidence] = Field(
        default_factory=list,
    )
    state_subject_assignments: list[
        NarrativeStateSubjectAssignment
    ] = Field(default_factory=list)
    # Every prose/action source unit must either have one identity-bearing
    # state_subject evidence row or be explicitly classified as environment.
    # Absence is never interpreted as environment: that used to turn missing
    # character attribution into a synthetic environment state silently.
    environment_source_unit_keys: list[str] = Field(default_factory=list)
    # Proof that a bounded ownership adjudicator independently resolved the
    # exact unit. It prevents a confirmed environment classification from
    # being raised again by later semantic review rounds.
    state_subject_adjudicated_unit_keys: list[str] = Field(
        default_factory=list,
    )
    source_unit_deliveries: list[NarrativeSourceUnitDelivery] = Field(
        default_factory=list,
    )
    scene_boundary_before: bool = False
    transition_cue: str = ""
    opening_image: str = ""
    exit_state: str = ""
    scene_role: Literal["bridge", "setup", "action", "turn", "reaction"] = (
        "action"
    )
    dramatic_load: int = Field(default=1, ge=1, le=3)
    action_logic: str
    adaptation_kind: Literal[
        "source_direct", "source_inferred", "logic_bridge",
    ] = "source_direct"
    bridge_rationale: str = ""
    state_requirements: list[BlueprintStateRequirement] = Field(
        default_factory=list,
    )
    state_changes: list[BlueprintStateChange] = Field(default_factory=list)
    released_constraints_for: list[str] = Field(default_factory=list)
    decision: BlueprintDecision | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_location_label(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["source_segment_ids"] = [
            _normalize_source_segment_id(source_id)
            for source_id in (
                normalized.get("source_segment_ids") or []
            )
        ]
        if not normalized.get("location_label"):
            normalized["location_label"] = str(
                normalized.get("location_key") or "未标注地点"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_narrative_semantics(self) -> NarrativeNode:
        expected = (
            ("causal", "standalone")
            if self.narrative_layer == "story"
            else ("connective", "exclude_from_spine")
        )
        if (self.event_priority, self.render_policy) != expected:
            raise ValueError(
                f"{self.narrative_layer} 节点必须使用 "
                f"event_priority={expected[0]}、render_policy={expected[1]}"
            )
        if self.narrative_layer == "paratext" and any((
            self.participants,
            self.participant_evidence,
            self.state_subject_assignments,
            self.environment_source_unit_keys,
            self.state_subject_adjudicated_unit_keys,
            self.source_unit_deliveries,
            self.exit_state.strip(),
            self.state_requirements,
            self.state_changes,
            self.released_constraints_for,
            self.decision is not None,
        )):
            raise ValueError(
                "paratext 节点不得承载人物、决定或剧情状态合同"
            )
        return self

    def source_semantics(self) -> BlueprintSourceSemantics:
        return BlueprintSourceSemantics(
            narrative_layer=self.narrative_layer,
            event_priority=self.event_priority,
            render_policy=self.render_policy,
            disposition=(
                "deliver"
                if self.narrative_layer == "story"
                else "audit_only"
            ),
            projection_policy=(
                "picture"
                if self.narrative_layer == "story"
                else "audit_only"
            ),
        )


class BlueprintScenePlan(BaseModel):
    key: str
    node_keys: list[str]
    source_segment_ids: list[str]
    source_semantics: dict[str, BlueprintSourceSemantics]
    temporal_domain_key: str
    time_label: str
    location_key: str
    location_label: str
    transition_cue: str
    previous_scene_exit_state: str = ""
    opening_image: str = ""
    exit_state: str = ""
    dramatic_load: int = 1
    agency_contracts: list[dict[str, str]] = Field(default_factory=list)
    participant_keys: list[str] = Field(default_factory=list)
    scene_heading: str


class BlueprintSourceAuditAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_key: str
    source_segment_ids: list[str]
    narrative_layer: Literal["paratext"] = "paratext"
    render_policy: Literal["exclude_from_spine"] = "exclude_from_spine"
    disposition: Literal["audit_only"] = "audit_only"
    projection_policy: Literal["audit_only"] = "audit_only"


class BlueprintSceneDerivation(BaseModel):
    relation_key: str
    relation_type: str
    source_scene_plan_key: str
    target_scene_plan_key: str
    source_node_key: str
    target_node_key: str
    reference_key: str = ""
    summary: str = ""


class BlueprintSourceOwnershipError(ValueError):
    def __init__(self, conflicts: dict[str, list[str]]):
        self.conflicts = {
            source_id: list(scene_keys)
            for source_id, scene_keys in conflicts.items()
        }
        self.errors = [
            "[BLUEPRINT_SOURCE_OWNER_CONFLICT] "
            f"{source_id} 同时归属 " + "、".join(scene_keys)
            for source_id, scene_keys in self.conflicts.items()
        ]
        super().__init__("；".join(self.errors))


class BlueprintSourceOccurrenceError(ValueError):
    def __init__(
        self,
        duplicates: dict[str, list[str]],
        *,
        partition_conflicts: set[str] | None = None,
    ):
        self.duplicates = {
            source_id: list(node_keys)
            for source_id, node_keys in duplicates.items()
        }
        conflicts = set(partition_conflicts or ())
        self.errors = []
        for source_id, node_keys in self.duplicates.items():
            code = (
                "[BLUEPRINT_SOURCE_PARTITION_CONFLICT] "
                if source_id in conflicts
                else "[BLUEPRINT_SOURCE_OCCURRENCE_DUPLICATE] "
            )
            self.errors.append(
                code
                + f"{source_id} 被 picture/audit timeline nodes 重复拥有："
                + "、".join(node_keys)
            )
        super().__init__("；".join(self.errors))


class BlueprintSourceOccurrenceIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_segment_id: str
    node_keys: list[str]
    error: str


def blueprint_source_occurrence_issues(
    nodes: list[NarrativeNode],
    *,
    prefix: str = "BLUEPRINT",
) -> list[BlueprintSourceOccurrenceIssue]:
    owners: defaultdict[str, list[str]] = defaultdict(list)
    partitions: defaultdict[str, list[str]] = defaultdict(list)
    for node in nodes:
        partition = node.source_semantics().projection_policy
        for source_id in node.source_segment_ids:
            owners[source_id].append(node.key)
            partitions[source_id].append(partition)
    issues: list[BlueprintSourceOccurrenceIssue] = []
    for source_id, node_keys in owners.items():
        if len(node_keys) <= 1:
            continue
        partition_names = set(partitions[source_id])
        if len(partition_names) > 1:
            code = f"{prefix}_SOURCE_PARTITION_CONFLICT"
        elif partition_names == {"audit_only"}:
            code = f"{prefix}_AUDIT_SOURCE_DUPLICATE"
        else:
            code = f"{prefix}_PICTURE_SOURCE_DUPLICATE"
        issues.append(
            BlueprintSourceOccurrenceIssue(
                source_segment_id=source_id,
                node_keys=node_keys,
                error=(
                    f"[{code}] {source_id} 必须恰由一个 timeline node 拥有，"
                    "实际：" + "、".join(node_keys)
                ),
            )
        )
    return issues


class NarrativeBlueprint(BaseModel):
    format_version: Literal["screenplay-narrative-blueprint.v7"] = (
        BLUEPRINT_VERSION
    )
    episode_no: int
    nodes: list[NarrativeNode]
    scene_plans: list[BlueprintScenePlan] = Field(default_factory=list)
    source_scene_owners: dict[str, str] = Field(default_factory=dict)
    source_semantics: dict[str, BlueprintSourceSemantics] = Field(
        default_factory=dict,
    )
    source_audit_annotations: list[BlueprintSourceAuditAnnotation] = Field(
        default_factory=list,
    )
    scene_derivations: list[BlueprintSceneDerivation] = Field(
        default_factory=list,
    )


class NarrativeBlueprintShard(BaseModel):
    format_version: Literal["screenplay-narrative-blueprint.v7"] = (
        BLUEPRINT_VERSION
    )
    episode_no: int
    shard_index: int
    source_segment_ids: list[str]
    nodes: list[NarrativeNode]
    source_hash: str = ""
    boundary_hash: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_version(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["source_segment_ids"] = [
            _normalize_source_segment_id(source_id)
            for source_id in (
                normalized.get("source_segment_ids") or []
            )
        ]
        return normalized

    @model_validator(mode="after")
    def _require_typed_voice_delivery_evidence(
        self,
    ) -> "NarrativeBlueprintShard":
        """Make audible performer ownership part of the v7 typed response."""
        for node in self.nodes:
            voice_claims: defaultdict[
                str,
                list[NarrativeParticipantEvidence],
            ] = defaultdict(list)
            for evidence in node.participant_evidence:
                if evidence.usage != "voice":
                    continue
                for source_unit_key in dict.fromkeys(
                    evidence.source_unit_keys
                ):
                    voice_claims[source_unit_key].append(evidence)

            deliveries = {
                delivery.source_unit_key: delivery
                for delivery in node.source_unit_deliveries
            }
            for delivery in node.source_unit_deliveries:
                claims = voice_claims.get(delivery.source_unit_key, [])
                audible = delivery.mode in AUDIBLE_SOURCE_DELIVERY_MODES
                if not audible and claims:
                    raise ValueError(
                        f"{delivery.source_unit_key} 非声音 delivery "
                        "不得包含 usage=voice participant_evidence"
                    )
                if not audible:
                    continue
                if len(claims) != 1:
                    raise ValueError(
                        f"{delivery.source_unit_key} 声音 delivery 必须恰有一条"
                        "精确 source_unit_key 的 usage=voice "
                        "participant_evidence；performer_key 不能替代该证据"
                    )
                claim = claims[0]
                if claim.identity_key != delivery.performer_key:
                    raise ValueError(
                        f"{delivery.source_unit_key} 的 usage=voice "
                        "identity_key 必须等于 performer_key"
                    )
            for source_unit_key in voice_claims:
                delivery = deliveries.get(source_unit_key)
                if (
                    delivery is None
                    or delivery.mode not in AUDIBLE_SOURCE_DELIVERY_MODES
                ):
                    raise ValueError(
                        f"{source_unit_key} 的 usage=voice "
                        "participant_evidence 缺少对应声音 delivery"
                    )
        return self
