"""叙事动作契约：原子动作、施动方、文本归属与语义关系审计。

AtomicAction 是最细粒度的可核验动作单元（施动者/受体/生效条件/文本归属），
ActionSemanticRelationAudit/NarrativeEvent 在其上聚合出剧情事件。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class AtomicActionPhase(BaseModel):
    phase_id: str
    start_condition: str = ""
    end_condition: str = ""
    estimated_min_s: float = 0.0


class ActionParticipantDelivery(BaseModel):
    """Structured proof that one offscreen action participant reaches viewers."""

    action_id: str
    participant_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    audible: bool = False
    visible_effect: bool = False
    visible_reaction: bool = False

    @property
    def is_perceivable(self) -> bool:
        return self.audible or self.visible_effect or self.visible_reaction


class ActionAgency(BaseModel):
    """Open semantic agency plus machine-checkable identity/source provenance."""

    kind: str = "character"
    identity_bearing: bool = True
    source_segment_ids: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: object) -> str:
        return str(value or "").strip() or "unattributed"

    @property
    def is_character_agency(self) -> bool:
        return self.kind == "character" or self.kind.startswith("character_")

    @model_validator(mode="after")
    def _validate_character_identity_bearing(self) -> "ActionAgency":
        if self.is_character_agency and not self.identity_bearing:
            raise ValueError(
                "character action_agency 必须声明 identity_bearing=true"
            )
        return self


class TextProvenance(BaseModel):
    """Compiler-owned attribution for authored text and its frozen sources."""

    model_config = ConfigDict(extra="forbid")

    kind: str = "creative_action"
    identity_keys: list[str] = Field(default_factory=list)
    content_owner_keys: list[str] = Field(default_factory=list)
    source_segment_ids: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: object) -> str:
        return str(value or "").strip() or "creative_action"

    @field_validator(
        "identity_keys",
        "content_owner_keys",
        "source_segment_ids",
        mode="before",
    )
    @classmethod
    def _normalize_keys(cls, value: object) -> list[str]:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return list(dict.fromkeys(
            normalized
            for item in values
            if (normalized := str(item or "").strip())
        ))


class AtomicAction(BaseModel):
    action_id: str
    actor_ids: list[str] = Field(default_factory=list)
    target_ids: list[str] = Field(default_factory=list)
    action_agency: ActionAgency
    text_provenance: TextProvenance
    dialogue_text: str = ""
    required_text: str = ""
    prop_text: str = ""
    on_screen_text: str = ""
    participant_deliveries: list[ActionParticipantDelivery]
    semantic_intent: str
    precondition_fact_ids: list[str] = Field(default_factory=list)
    effects_add: list[str] = Field(default_factory=list)
    effects_remove: list[str] = Field(default_factory=list)
    completion_condition: str
    decision_requirement: str = "applies"
    decision_not_applicable_reason: str | None = None
    temporal_phases: list[AtomicActionPhase] = Field(default_factory=list)
    splittable_boundaries: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _derive_missing_action_agency(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        actor_ids = list(normalized.get("actor_ids") or [])
        target_ids = list(normalized.get("target_ids") or [])
        identity_bearing = bool(actor_ids or target_ids)
        if normalized.get("action_agency") is None:
            normalized["action_agency"] = {
                "kind": "character" if identity_bearing else "unattributed",
                "identity_bearing": identity_bearing,
                "source_segment_ids": list(
                    normalized.get("source_segment_ids") or []
                ),
            }
        if normalized.get("text_provenance") is None:
            agency = normalized["action_agency"]
            source_segment_ids = (
                list(agency.source_segment_ids)
                if isinstance(agency, ActionAgency)
                else list(agency.get("source_segment_ids") or [])
            )
            if str(normalized.get("dialogue_text") or "").strip():
                provenance_kind = "dialogue"
            elif str(normalized.get("required_text") or "").strip():
                provenance_kind = "required_text"
            elif str(normalized.get("prop_text") or "").strip():
                provenance_kind = "prop_text"
            elif str(normalized.get("on_screen_text") or "").strip():
                provenance_kind = "on_screen_text"
            else:
                provenance_kind = "creative_action"
            normalized["text_provenance"] = {
                "kind": provenance_kind,
                "identity_keys": (
                    []
                    if provenance_kind not in ("creative_action", "dialogue")
                    else list(dict.fromkeys([*actor_ids, *target_ids]))
                ),
                "source_segment_ids": source_segment_ids,
            }
        return normalized

    @model_validator(mode="after")
    def _validate_action_agency_owner(self) -> "AtomicAction":
        identity_bearing = bool(self.actor_ids or self.target_ids)
        if self.action_agency.identity_bearing != identity_bearing:
            raise ValueError(
                "action_agency.identity_bearing 必须与 actor_ids/target_ids 等价"
            )
        if self.action_agency.is_character_agency and not identity_bearing:
            raise ValueError(
                "character action_agency 必须由 actor_ids/target_ids 承载"
            )
        explicit_text_kinds = [
            kind
            for kind, content in (
                ("dialogue", self.dialogue_text),
                ("required_text", self.required_text),
                ("prop_text", self.prop_text),
                ("on_screen_text", self.on_screen_text),
            )
            if content.strip()
        ]
        if len(explicit_text_kinds) > 1:
            raise ValueError(
                "dialogue/required_text/prop_text/on_screen_text "
                "每个 action 最多声明一种"
            )
        expected_provenance_kind = (
            explicit_text_kinds[0]
            if explicit_text_kinds
            else "creative_action"
        )
        expected_identity_keys = (
            []
            if expected_provenance_kind in (
                "required_text", "prop_text", "on_screen_text",
            )
            else list(dict.fromkeys([*self.actor_ids, *self.target_ids]))
        )
        if self.text_provenance.kind != expected_provenance_kind:
            raise ValueError(
                "text_provenance.kind 必须由显式文字结构字段确定"
            )
        if self.text_provenance.identity_keys != expected_identity_keys:
            raise ValueError(
                "text_provenance.identity_keys 必须由 actor_ids/target_ids 确定"
            )
        if (
            self.text_provenance.source_segment_ids
            != self.action_agency.source_segment_ids
        ):
            raise ValueError(
                "text_provenance.source_segment_ids 必须与 action agency 来源等价"
            )
        return self


class ActionSemanticRelationAudit(BaseModel):
    """AI semantic comparison for actions that may repeat across different IDs."""

    action_relation_audit_id: str
    action_ids: list[str] = Field(default_factory=list)
    semantically_equivalent: bool
    functional_repeat: bool | None = None
    added_target_delta_ids: list[str] = Field(default_factory=list)
    added_character_state_ids: list[str] = Field(default_factory=list)
    added_evidence_ids: list[str] = Field(default_factory=list)
    causal_basis_event_ids: list[str] = Field(default_factory=list)
    decision: str = "needs_review"
    reason: str = ""


class NarrativeEvent(BaseModel):
    event_id: str
    proposition_ids: list[str] = Field(default_factory=list)
    causal_parent_ids: list[str] = Field(default_factory=list)
    precondition_fact_ids: list[str] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    # Identities that are physically present and may be rendered while this
    # event is delivered.  This is deliberately distinct from
    # NarrativeEvidence.perceivable_by: an observer, addressee, or person
    # mentioned in dialogue is not thereby a visual subject.
    onscreen_entity_ids: list[str] = Field(default_factory=list)
    effects_add: list[str] = Field(default_factory=list)
    effects_remove: list[str] = Field(default_factory=list)
    character_goal_effects: list[dict] = Field(default_factory=list)
    downstream_dependency_event_ids: list[str] = Field(default_factory=list)
    salience: float = 0.0
    irreversibility: float = 0.0
    must_keep: bool = True
    narrative_layer: Literal["story", "paratext"] = "story"
    event_priority: Literal["causal", "supporting", "connective"] = "causal"
    render_policy: Literal["standalone", "merge_adjacent", "exclude_from_spine"] = "standalone"
    delivery_scope_id: str = "episode"
    delivery_policy: str = "deliver"
    primary_delivery_window_id: str | None = None
