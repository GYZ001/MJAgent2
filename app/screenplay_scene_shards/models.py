"""Pydantic contracts for the screenplay envelope and scene-shard pipeline:
envelope metadata/experience/IR, unit-slot plans, shard plans, per-scene
input contracts, creative draft units/IR, semantic-review findings, and the
compiled scene-shard IR types.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

from app.narrative_blueprint import (
    BlueprintSceneDerivation,
    BlueprintSourceSemantics,
)
from app.schemas import ActionAgency
from app.screenplay_ir import (
    IRActionParticipantDelivery,
    IRExperience,
    IRMetadata,
    IRScene,
    IRSceneUnit,
    IR_VERSION,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from typing import (
    Any,
    Literal,
)

from .constants import (
    SCREENPLAY_ENVELOPE_VERSION,
    SCREENPLAY_SCENE_CREATIVE_VERSION,
    SCREENPLAY_SCENE_INPUT_VERSION,
    SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS,
    SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS,
    SCREENPLAY_SCENE_SHARD_VERSION,
    SCREENPLAY_SHARD_PLAN_VERSION,
)


class ScreenplayEnvelopeMetadata(BaseModel):
    title: str = ""
    logline: str = ""
    dramatic_question: str = ""
    protagonist_goal: str = ""
    obstacle: str = ""
    stakes: str = ""
    emotional_curve: str = ""
    ending_hook: str = ""
    source_basis: str = ""
    adaptation_direction: str = ""
    opening: str = ""
    development: str = ""
    conflict: str = ""
    climax: str = ""
    episode_premise: str = ""
    approved_adaptations: list[str] = Field(default_factory=list)
    forbidden_additions: list[str] = Field(default_factory=list)
    script_format_note: str = "场次化台本稿"
    must_keep_ending: str = ""
    drop_list: list[str] = Field(default_factory=list)

    def to_ir(self) -> IRMetadata:
        return IRMetadata.model_validate(self.model_dump(mode="json"))


class ScreenplayEnvelopeExperience(BaseModel):
    director_objective: str = ""
    satisfaction_criteria: str = ""
    required_processing_s: float = Field(default=1.0, ge=0)
    forbidden_misconceptions: list[str] = Field(default_factory=list)

    def to_ir(self) -> IRExperience:
        return IRExperience.model_validate(self.model_dump(mode="json"))


class ScreenplayEnvelopeIR(BaseModel):
    contract_version: Literal["screenplay-envelope.v2"] = SCREENPLAY_ENVELOPE_VERSION
    episode_no: int
    metadata: ScreenplayEnvelopeMetadata
    experience: ScreenplayEnvelopeExperience
    blueprint_hash: str
    identity_registry_hash: str


class ScreenplaySceneUnitSlotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_key: str
    event_key: str
    scene_key: str
    scene_order: int = Field(ge=1)
    unit_order: int = Field(ge=1)
    scene_unit_order: int = Field(ge=1)
    kind: Literal["action", "dialogue"]
    narrative_layer: Literal["story", "paratext"]
    event_priority: Literal["causal", "supporting", "connective"]
    render_policy: Literal[
        "standalone", "merge_adjacent", "exclude_from_spine",
    ]
    source_segment_ids: list[str] = Field(min_length=1)
    source_unit_key: str = ""
    source_text: str = ""
    source_surface: Literal["prose", "quoted_span"] = "prose"
    delivery_mode: Literal[
        "action",
        "spoken_dialogue",
        "offscreen_voice",
        "written_text",
        "sound_effect",
        "unspoken_reference",
    ] = "action"
    content_owner_key: str = ""
    performer_key: str = ""
    state_subject_key: str = ""
    environment_only: bool = False


class ScreenplaySceneCompiledUnitSlot(ScreenplaySceneUnitSlotPlan):
    actor_keys: list[str] = Field(default_factory=list)
    target_keys: list[str] = Field(default_factory=list)
    onscreen_entity_keys: list[str] = Field(default_factory=list)
    participant_deliveries: list[IRActionParticipantDelivery] = Field(
        default_factory=list,
    )
    speaker_key: str | None = None
    state_subject_keys: list[str] = Field(default_factory=list)
    action_agency: ActionAgency | None = None

    @model_validator(mode="after")
    def _derive_action_agency(self) -> "ScreenplaySceneCompiledUnitSlot":
        identity_bearing = bool(
            self.actor_keys or self.target_keys or self.speaker_key
        )
        if self.action_agency is None:
            self.action_agency = ActionAgency(
                kind=(
                    "character"
                    if identity_bearing
                    else "unattributed"
                ),
                identity_bearing=identity_bearing,
                source_segment_ids=list(self.source_segment_ids),
            )
        if self.action_agency.identity_bearing != identity_bearing:
            raise ValueError(
                "compiled slot action_agency.identity_bearing 必须与 "
                "actor_keys/target_keys/speaker_key 等价"
            )
        if self.action_agency.is_character_agency and not identity_bearing:
            raise ValueError(
                "compiled slot character action_agency 必须由 "
                "actor_keys/target_keys/speaker_key 承载"
            )
        if self.action_agency.source_segment_ids != self.source_segment_ids:
            raise ValueError(
                "compiled slot action_agency.source_segment_ids 必须与来源等价"
            )
        if self.state_subject_key and self.state_subject_keys != [
            self.state_subject_key
        ]:
            raise ValueError(
                "compiled slot state_subject_key 必须等于唯一 "
                "state_subject_keys 成员"
            )
        if self.environment_only and self.state_subject_keys:
            raise ValueError(
                "compiled slot 不得同时声明 state_subject_keys "
                "与 environment_only"
            )
        return self


class ScreenplaySceneShardPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["screenplay-scene-shard-plan.v6"] = SCREENPLAY_SHARD_PLAN_VERSION
    shard_id: str
    scene_plan_keys: list[str]
    source_segment_ids: list[str]
    source_scene_owners: dict[str, str]
    unit_slots: list[ScreenplaySceneUnitSlotPlan]
    derived_relations: list[BlueprintSceneDerivation] = Field(
        default_factory=list,
    )
    source_ownership_hash: str
    estimated_units: int = Field(ge=1)
    estimated_output_chars: int = Field(ge=1)
    boundary_state_in: dict[str, Any] = Field(default_factory=dict)
    boundary_state_out: dict[str, Any] = Field(default_factory=dict)
    source_hash: str
    boundary_hash: str
    blueprint_hash: str
    identity_registry_hash: str


class ScreenplaySceneSourceSegment(BaseModel):
    source_segment_id: str
    text: str


class ScreenplaySceneParticipantBinding(BaseModel):
    blueprint_key: str
    identity_key: str


class ScreenplaySceneActionParticipantEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_key: str
    source_segment_ids: list[str] = Field(default_factory=list)
    source_unit_keys: list[str] = Field(default_factory=list)
    usage: Literal["visible", "voice", "mentioned", "state_subject"]
    perception_channels: list[
        Literal["audible", "visible_effect", "visible_reaction"]
    ] = Field(default_factory=list)


class ScreenplaySceneStateSubjectAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_unit_key: str
    mode: Literal["joint"]
    identity_keys: list[str] = Field(min_length=2)


class ScreenplaySceneActionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_key: str
    source_segment_ids: list[str] = Field(default_factory=list)
    participants: list[ScreenplaySceneActionParticipantEvidence] = Field(
        default_factory=list,
    )
    state_subject_assignments: list[
        ScreenplaySceneStateSubjectAssignment
    ] = Field(default_factory=list)
    decision_actor_key: str | None = None
    environment_source_unit_keys: list[str] = Field(default_factory=list)


class ScreenplayActionParticipantDeliveryContract(BaseModel):
    contract_version: Literal["screenplay-generation-ir.v4"] = IR_VERSION
    evidence_schema: dict[str, Any] = Field(
        default_factory=IRActionParticipantDelivery.model_json_schema,
    )
    unit_field_required: Literal[True] = True
    offscreen_relation_requires_evidence: Literal[True] = True
    observable_claim_required: Literal[True] = True
    perceivable_channel_required: Literal[True] = True


class ScreenplaySceneInputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["screenplay-scene-input.v10"] = (
        SCREENPLAY_SCENE_INPUT_VERSION
    )
    scene_plan_key: str
    node_keys: list[str]
    source_segment_ids: list[str]
    source_semantics: dict[str, BlueprintSourceSemantics]
    source_segments: list[ScreenplaySceneSourceSegment]
    participant_bindings: list[ScreenplaySceneParticipantBinding]
    source_scene_owners: dict[str, str]
    derived_relations: list[BlueprintSceneDerivation] = Field(
        default_factory=list,
    )
    action_participant_delivery_contract: (
        ScreenplayActionParticipantDeliveryContract
    ) = Field(default_factory=ScreenplayActionParticipantDeliveryContract)
    action_evidence: list[ScreenplaySceneActionEvidence] = Field(
        default_factory=list,
    )
    unit_slots: list[ScreenplaySceneCompiledUnitSlot]
    identity_scaffold_hash: str = ""
    source_ownership_hash: str


class UnresolvedParticipant(BaseModel):
    source_label: str
    source_segment_ids: list[str] = Field(default_factory=list)
    scene_key: str = ""
    usage: str = "visible"
    reason: str = ""


class ScreenplaySceneShardCreativeUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    performance: str = ""
    resulting_state: str = ""
    function: str = "statement"
    required_text: str = ""
    prop_text: str = ""
    on_screen_text: str = ""

    @model_validator(mode="after")
    def _validate_text_content_shape(
        self,
    ) -> "ScreenplaySceneShardCreativeUnit":
        explicit_text_fields = sum(bool(value.strip()) for value in (
            self.required_text,
            self.prop_text,
            self.on_screen_text,
        ))
        if explicit_text_fields > 1:
            raise ValueError(
                "required_text/prop_text/on_screen_text 每个 slot 最多填写一种"
            )
        return self


class ScreenplaySceneShardCreativeIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["screenplay-scene-creative.v8"] = (
        SCREENPLAY_SCENE_CREATIVE_VERSION
    )
    slots: dict[str, ScreenplaySceneShardCreativeUnit]


class ScreenplaySceneShardSemanticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_key: str
    related_unit_keys: list[str]
    code: Literal[
        "state_subject_semantic_drift",
        "source_semantic_drift",
    ]
    violation_kinds: list[
        Literal[
            "wrong_subject",
            "unsupported_action",
            "source_contradiction",
            "cross_slot_duplication",
            "environment_personification",
        ]
    ] = Field(min_length=1, max_length=5)
    message: str = Field(
        min_length=1,
        max_length=SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS,
    )

    @model_validator(mode="before")
    @classmethod
    def _fill_missing_non_cross_related_unit_keys(
        cls,
        value: Any,
    ) -> Any:
        if (
            isinstance(value, dict)
            and "related_unit_keys" not in value
            and isinstance(value.get("violation_kinds"), list)
            and "cross_slot_duplication"
            not in value["violation_kinds"]
        ):
            return {**value, "related_unit_keys": []}
        return value

    @model_validator(mode="after")
    def _validate_typed_finding(
        self,
    ) -> "ScreenplaySceneShardSemanticFinding":
        self.unit_key = self.unit_key.strip()
        self.related_unit_keys = [
            unit_key.strip() for unit_key in self.related_unit_keys
        ]
        if len(self.violation_kinds) != len(set(self.violation_kinds)):
            raise ValueError("violation_kinds 不得重复")
        self.violation_kinds = [
            kind
            for kind in SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS
            if kind in self.violation_kinds
        ]
        if len(self.related_unit_keys) != len(
            set(self.related_unit_keys)
        ):
            raise ValueError("related_unit_keys 不得重复")
        if self.unit_key in self.related_unit_keys:
            raise ValueError("related_unit_keys 不得包含 finding 自身 unit_key")
        has_cross_slot_duplication = (
            "cross_slot_duplication" in self.violation_kinds
        )
        if (
            has_cross_slot_duplication
            and len(self.related_unit_keys) != 1
        ):
            raise ValueError(
                "cross_slot_duplication 必须恰好声明一个 related_unit_key"
            )
        if not has_cross_slot_duplication and self.related_unit_keys:
            raise ValueError(
                "非 cross_slot_duplication finding 的 "
                "related_unit_keys 必须为空"
            )
        return self


class ScreenplaySceneShardSemanticReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ScreenplaySceneShardSemanticFinding]

    @model_validator(mode="after")
    def _validate_unique_finding_keys(
        self,
    ) -> "ScreenplaySceneShardSemanticReview":
        finding_keys = [
            (finding.unit_key, finding.code)
            for finding in self.findings
        ]
        if len(finding_keys) != len(set(finding_keys)):
            raise ValueError("findings 中 (unit_key, code) 必须唯一")
        return self


class ScreenplaySceneShardUnit(IRSceneUnit):
    model_config = ConfigDict(extra="forbid")

    participant_deliveries: list[IRActionParticipantDelivery]


class ScreenplaySceneShardScene(IRScene):
    model_config = ConfigDict(extra="forbid")

    units: list[ScreenplaySceneShardUnit] = Field(default_factory=list)


class ScreenplaySceneShardIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["screenplay-scene-shard.v12"] = (
        SCREENPLAY_SCENE_SHARD_VERSION
    )
    episode_no: int
    shard_id: str
    scene_plan_keys: list[str]
    scenes: list[ScreenplaySceneShardScene]
    consumed_source_ids: list[str] = Field(default_factory=list)
    unresolved_participants: list[UnresolvedParticipant] = Field(default_factory=list)
    source_hash: str = ""
    boundary_hash: str = ""
    blueprint_hash: str = ""
    identity_registry_hash: str = ""
    source_ownership_hash: str = ""
    identity_scaffold_hash: str = ""
    generation_scaffold_hash: str = ""
