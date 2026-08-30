"""Event-level compact-IR Pydantic models (action phases, events, audience priors/experience, metadata) and the top-level ScreenplayGenerationIR envelope."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.narrative_blueprint import BlueprintSourceAuditAnnotation
from app.schemas import ActionAgency, EpisodeScreenplay, TextProvenance

from .constants import IR_VERSION
from .contract_validation import (
    _as_list,
    _validate_text_provenance,
    derive_action_agency_payload,
    derive_text_provenance_payload,
    screenplay_ir_missing_event_semantic_paths,
    screenplay_ir_missing_participant_delivery_paths,
)
from .models_core import IRActionParticipantDelivery, IRBeat, IRCoverageGroup, IRIdentity, IRScene
from .source_audit import screenplay_ir_source_audit_contract_errors


class IRActionPhase(BaseModel):
    start_condition: str
    end_condition: str
    estimated_min_s: float = Field(default=1.0, ge=0)
    splittable_after: bool = False


class IREvent(BaseModel):
    key: str
    scene_key: str
    narrative_layer: Literal["story", "paratext"] = "story"
    event_priority: Literal["causal", "supporting", "connective"] = "causal"
    render_policy: Literal[
        "standalone", "merge_adjacent", "exclude_from_spine",
    ] = "standalone"
    source_segment_ids: list[str] = Field(default_factory=list)
    source_excerpt: str = ""
    source_statement: str = ""
    adapted_statement: str = ""
    adaptation_relation: str = "preserve"
    adaptation_reason: str = ""
    actor_keys: list[str] = Field(default_factory=list)
    target_keys: list[str] = Field(default_factory=list)
    onscreen_entity_keys: list[str] = Field(default_factory=list)
    participant_deliveries: list[IRActionParticipantDelivery] = Field(
        default_factory=list
    )
    state_subject_key: str = ""
    state_subject_keys: list[str] = Field(default_factory=list)
    environment_only: bool = False
    action_agency: ActionAgency
    text_provenance: TextProvenance
    dialogue_text: str = ""
    required_text: str = ""
    prop_text: str = ""
    on_screen_text: str = ""
    causal_parent_keys: list[str] = Field(default_factory=list)
    precondition_state: str = ""
    resulting_state: str = ""
    action_intent: str = ""
    completion_condition: str = ""
    action_phases: list[IRActionPhase] = Field(default_factory=list)
    decision_required: bool = True
    decision_reason: str = ""
    observable_claim: str = ""
    perceivable_by: list[str] = Field(default_factory=list)
    character_goal: str = ""
    character_stakes: str = ""
    character_emotion: str = ""
    character_tactic: str = ""
    information: list[str] = Field(default_factory=list)
    salience: float = Field(default=0.7, ge=0, le=1)
    irreversibility: float = Field(default=0.5, ge=0, le=1)
    readability_s: float = Field(default=1.0, ge=0)
    must_keep: bool = True

    @field_validator(
        "source_segment_ids", "actor_keys", "target_keys", "onscreen_entity_keys",
        "participant_deliveries", "causal_parent_keys", "action_phases", "perceivable_by",
        "information", "state_subject_keys", mode="before",
    )
    @classmethod
    def _normalize_lists(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_numeric_ranges(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = derive_action_agency_payload(
            value,
            actor_field="actor_keys",
            target_field="target_keys",
            source_field="source_segment_ids",
        )
        normalized = derive_text_provenance_payload(
            normalized,
            actor_field="actor_keys",
            target_field="target_keys",
            source_field="source_segment_ids",
        )
        for field, default in (
            ("salience", 0.7),
            ("irreversibility", 0.5),
        ):
            try:
                normalized[field] = min(
                    1.0, max(0.0, float(normalized.get(field, default))),
                )
            except (TypeError, ValueError):
                normalized[field] = default
        try:
            normalized["readability_s"] = max(
                0.0, float(normalized.get("readability_s", 1.0)),
            )
        except (TypeError, ValueError):
            normalized["readability_s"] = 1.0
        return normalized

    @model_validator(mode="after")
    def _validate_action_agency(self) -> "IREvent":
        if self.state_subject_key and not self.state_subject_keys:
            self.state_subject_keys = [self.state_subject_key]
        identity_bearing = bool(self.actor_keys or self.target_keys)
        if self.action_agency.identity_bearing != identity_bearing:
            raise ValueError(
                "event.action_agency.identity_bearing 必须与 "
                "actor_keys/target_keys 等价"
            )
        if self.action_agency.is_character_agency and not identity_bearing:
            raise ValueError(
                "event.character action_agency 必须由 "
                "actor_keys/target_keys 承载"
            )
        if self.action_agency.source_segment_ids != self.source_segment_ids:
            raise ValueError(
                "event.action_agency.source_segment_ids 必须与事件来源等价"
            )
        if self.state_subject_key and self.state_subject_keys != [
            self.state_subject_key
        ]:
            raise ValueError(
                "event state_subject_key 必须等于唯一 state_subject_keys 成员"
            )
        if self.environment_only and self.state_subject_keys:
            raise ValueError(
                "event 不得同时声明 state_subject_keys 与 environment_only"
            )
        _validate_text_provenance(
            provenance=self.text_provenance,
            relation_keys=[*self.actor_keys, *self.target_keys],
            source_segment_ids=self.source_segment_ids,
            dialogue=False,
            dialogue_text=self.dialogue_text,
            required_text=self.required_text,
            prop_text=self.prop_text,
            on_screen_text=self.on_screen_text,
            label="event",
        )
        return self


class IRAudiencePrior(BaseModel):
    key: str
    description: str
    familiarity_assumptions: list[dict[str, Any]] = Field(default_factory=list)
    language_and_context_assumptions: list[str] = Field(default_factory=list)
    attention_memory_assumptions: dict[str, Any] = Field(default_factory=dict)
    target_stance: Literal["believed", "suspected", "rejected"] = "believed"
    target_confidence: float = Field(default=0.75, ge=0, le=1)

    @field_validator("target_stance", mode="before")
    @classmethod
    def _normalize_target_stance(cls, value: Any) -> str:
        stance = str(value or "suspected").strip().lower()
        aliases = {
            "neutral": "suspected",
            "unknown": "suspected",
            "undetermined": "suspected",
            "not_applicable": "suspected",
            "accepted": "believed",
            "true": "believed",
            "denied": "rejected",
            "false": "rejected",
        }
        stance = aliases.get(stance, stance)
        return stance if stance in {
            "believed", "suspected", "rejected",
        } else "suspected"

    @field_validator("familiarity_assumptions", mode="before")
    @classmethod
    def _normalize_familiarity(cls, value: Any) -> list[dict[str, Any]]:
        return [
            item if isinstance(item, dict) else {"description": str(item)}
            for item in _as_list(value)
            if item not in (None, "")
        ]

    @field_validator("language_and_context_assumptions", mode="before")
    @classmethod
    def _normalize_language_assumptions(cls, value: Any) -> list[str]:
        return [str(item) for item in _as_list(value) if str(item).strip()]

    @field_validator("attention_memory_assumptions", mode="before")
    @classmethod
    def _normalize_attention_memory(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        values = _as_list(value)
        return {"assumptions": values} if values else {}


class IRExperience(BaseModel):
    director_objective: str
    satisfaction_criteria: str
    required_processing_s: float = Field(default=1.0, ge=0)
    forbidden_misconceptions: list[str] = Field(default_factory=list)

    @field_validator("forbidden_misconceptions", mode="before")
    @classmethod
    def _normalize_forbidden_misconceptions(cls, value: Any) -> list[str]:
        return [str(item) for item in _as_list(value) if str(item).strip()]


class IRMetadata(BaseModel):
    title: str
    logline: str
    script_format_note: str
    dramatic_question: str
    protagonist_goal: str
    obstacle: str
    stakes: str
    emotional_curve: str
    ending_hook: str
    source_basis: str
    adaptation_direction: str
    opening: str
    development: str
    conflict: str
    climax: str
    episode_premise: str
    must_keep_ending: str
    drop_list: list[str] = Field(default_factory=list)
    approved_adaptations: list[str] = Field(default_factory=list)
    forbidden_additions: list[str] = Field(default_factory=list)


class ScreenplayGenerationIR(BaseModel):
    """Compact model-authored representation.

    ``legacy_screenplay`` accepts persisted tests and interrupted deployments
    that still return the former full contract. New prompts never request it.
    """

    format_version: str = IR_VERSION
    episode_no: int = 0
    metadata: IRMetadata | None = None
    identities: list[IRIdentity] = Field(default_factory=list)
    beats: list[IRBeat] = Field(default_factory=list)
    coverage: list[IRCoverageGroup] = Field(default_factory=list)
    scenes: list[IRScene] = Field(default_factory=list)
    events: list[IREvent] = Field(default_factory=list)
    audience_priors: list[IRAudiencePrior] = Field(default_factory=list)
    experience: IRExperience | None = None
    normalization_log: list[dict[str, Any]] = Field(default_factory=list)
    source_scene_owners: dict[str, str] = Field(default_factory=dict)
    source_semantics: dict[str, dict[str, str]] = Field(
        default_factory=dict,
    )
    source_audit_annotations: list[
        BlueprintSourceAuditAnnotation
    ] = Field(default_factory=list)
    scene_derivations: list[dict[str, Any]] = Field(default_factory=list)
    source_ownership_hash: str = ""
    legacy_screenplay: EpisodeScreenplay | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_screenplay(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "legacy_screenplay" in value:
            return value
        if "full_script_text" in value or "narrative_plan" in value:
            return {
                "format_version": "legacy-episode-screenplay",
                "episode_no": int(value.get("episode_no") or 0),
                "legacy_screenplay": value,
            }
        if str(value.get("format_version") or IR_VERSION) == IR_VERSION:
            missing = [
                *screenplay_ir_missing_participant_delivery_paths(value),
                *screenplay_ir_missing_event_semantic_paths(value),
            ]
            if missing:
                raise ValueError(
                    "[IR_CONTRACT_FIELD_MISSING] 当前 IR 合同缺少显式字段："
                    + "、".join(missing[:10])
                )
            audit_errors = screenplay_ir_source_audit_contract_errors(value)
            if audit_errors:
                raise ValueError("；".join(audit_errors))
        normalized = dict(value)
        coverage = normalized.get("coverage")
        if isinstance(coverage, dict):
            if isinstance(coverage.get("items"), list):
                normalized["coverage"] = coverage["items"]
            elif isinstance(coverage.get("exceptions"), list):
                normalized["coverage"] = coverage["exceptions"]
            else:
                normalized["coverage"] = [
                    {"source_segment_ids": [key], **item}
                    if isinstance(item, dict)
                    else {
                        "source_segment_ids": [key],
                        "disposition": "context",
                        "reason": str(item),
                    }
                    for key, item in coverage.items()
                ]
        elif coverage is not None and not isinstance(coverage, list):
            normalized["coverage"] = _as_list(coverage)
        return normalized

    @model_validator(mode="after")
    def _validate_source_audit_contract(self) -> ScreenplayGenerationIR:
        if self.format_version != IR_VERSION or self.legacy_screenplay is not None:
            return self
        errors = screenplay_ir_source_audit_contract_errors(
            self.model_dump(mode="json")
        )
        if errors:
            raise ValueError("；".join(errors))
        return self


def merge_scene_shards(**kwargs: Any) -> ScreenplayGenerationIR:
    """Merge only validated scene-shard models into the compiler's full IR.

    The implementation lives in the pre-document shard module; this lazy
    boundary keeps ``ScreenplayGenerationIR`` as the sole compiler input while
    avoiding a module import cycle.
    """
    from app.screenplay_scene_shards import (
        ScreenplaySceneShardIR,
        merge_screenplay_scene_shards,
    )

    shards = kwargs.get("shards") or []
    if not all(isinstance(shard, ScreenplaySceneShardIR) for shard in shards):
        raise TypeError("merge_scene_shards only accepts validated typed shards")
    return merge_screenplay_scene_shards(**kwargs)
