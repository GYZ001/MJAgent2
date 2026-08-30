"""Core compact-IR Pydantic models: identities, beats, coverage groups, scene units and scenes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.renderability import SCENE_STORY_FUNCTION_MIN_CHARS
from app.schemas import ActionAgency, TextProvenance

from .contract_validation import (
    _as_list,
    _validate_text_provenance,
    derive_action_agency_payload,
    derive_text_provenance_payload,
)


class IRIdentity(BaseModel):
    key: str
    display_name: str
    authority_id: str = ""
    source_names: list[str] = Field(default_factory=list)
    kind: str = "contextual_character"
    visual_policy: Literal[
        "canonical", "contextual", "collective", "offscreen_only",
    ] = "contextual"
    visual_canonical: str = ""
    asset_requirement: Literal["required", "optional", "forbidden"] = "optional"
    voice_canonical: str = ""
    role_type: Literal[
        "named_character", "functional_character", "narrator",
    ] = "functional_character"
    rationale: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_policy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        visual_policy = str(
            normalized.get("visual_policy") or "contextual"
        ).strip().lower()
        visual_aliases = {
            "persistent": "canonical",
            "character": "canonical",
            "visible": "contextual",
            "transient": "contextual",
            "group": "collective",
            "crowd": "collective",
            "voice_only": "offscreen_only",
            "offscreen": "offscreen_only",
        }
        visual_policy = visual_aliases.get(visual_policy, visual_policy)
        if visual_policy not in {
            "canonical", "contextual", "collective", "offscreen_only",
        }:
            visual_policy = "contextual"
        normalized["visual_policy"] = visual_policy

        asset_requirement = str(
            normalized.get("asset_requirement") or ""
        ).strip().lower()
        if asset_requirement not in {"required", "optional", "forbidden"}:
            asset_requirement = (
                "required" if visual_policy == "canonical"
                else "forbidden" if visual_policy == "offscreen_only"
                else "optional"
            )
        normalized["asset_requirement"] = asset_requirement

        role_type = str(
            normalized.get("role_type") or "functional_character"
        ).strip().lower()
        role_aliases = {
            "named": "named_character",
            "functional": "functional_character",
            "voice": "narrator",
        }
        role_type = role_aliases.get(role_type, role_type)
        if role_type not in {
            "named_character", "functional_character", "narrator",
        }:
            role_type = "functional_character"
        normalized["role_type"] = role_type
        return normalized


class IRBeat(BaseModel):
    key: str
    who: str
    does: str
    turn: str
    purpose: str = ""
    source_segment_ids: list[str] = Field(default_factory=list)
    must_keep: bool = True

    @field_validator("source_segment_ids", mode="before")
    @classmethod
    def _normalize_source_ids(cls, value: Any) -> list[Any]:
        return _as_list(value)


class IRCoverageGroup(BaseModel):
    source_segment_ids: list[str] = Field(default_factory=list)
    disposition: Literal[
        "deliver", "merge", "context", "duplicate", "audit_only",
    ] = "context"
    projection_policy: Literal[
        "picture", "context_only", "audit_only",
    ] = "context_only"
    beat_keys: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_model_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["source_segment_ids"] = _as_list(
            normalized.get("source_segment_ids")
            or normalized.get("segment_ids")
            or normalized.get("segments")
        )
        normalized["beat_keys"] = _as_list(
            normalized.get("beat_keys")
            or normalized.get("beats")
        )
        normalized["reason"] = str(
            normalized.get("reason")
            or normalized.get("context_note")
            or normalized.get("note")
            or ""
        ).strip()
        raw_disposition = str(
            normalized.get("disposition")
            or normalized.get("coverage_type")
            or ""
        ).strip().lower()
        if raw_disposition not in {
            "deliver", "merge", "context", "duplicate", "audit_only",
        }:
            if normalized.get("duplicate_of"):
                raw_disposition = "duplicate"
            elif normalized["beat_keys"]:
                raw_disposition = "merge"
            else:
                # A coverage exception without beat ownership is, by
                # structure, retained context. This handles provider labels
                # such as "merged_into_character_background" without a
                # story-word or role-name whitelist.
                raw_disposition = "context"
        normalized["disposition"] = raw_disposition
        normalized.setdefault(
            "projection_policy",
            (
                "picture"
                if raw_disposition in {"deliver", "merge"}
                else "audit_only"
                if raw_disposition == "audit_only"
                else "context_only"
            ),
        )
        return normalized

    @model_validator(mode="after")
    def _validate_projection_policy(self) -> IRCoverageGroup:
        expected = (
            "picture"
            if self.disposition in {"deliver", "merge"}
            else "audit_only"
            if self.disposition == "audit_only"
            else "context_only"
        )
        if self.projection_policy != expected:
            raise ValueError(
                f"{self.disposition} coverage 必须使用 "
                f"projection_policy={expected}"
            )
        if self.disposition == "audit_only" and self.beat_keys:
            raise ValueError("audit_only coverage 不得绑定 beat_keys")
        return self


class IRActionParticipantDelivery(BaseModel):
    participant_key: str
    observable_claim: str
    audible: bool = False
    visible_effect: bool = False
    visible_reaction: bool = False

    @property
    def is_perceivable(self) -> bool:
        return self.audible or self.visible_effect or self.visible_reaction


class IRSceneUnit(BaseModel):
    kind: Literal["action", "dialogue"]
    text: str
    event_key: str
    unit_key: str = ""
    narrative_layer: Literal["story", "paratext"] = "story"
    event_priority: Literal["causal", "supporting", "connective"] = "causal"
    render_policy: Literal[
        "standalone", "merge_adjacent", "exclude_from_spine",
    ] = "standalone"
    source_segment_ids: list[str] = Field(default_factory=list)
    actor_keys: list[str] = Field(default_factory=list)
    target_keys: list[str] = Field(default_factory=list)
    # Exact frozen identity keys physically present in this unit.  Dialogue
    # references and people who can perceive a line are separate relations.
    onscreen_entity_keys: list[str] = Field(default_factory=list)
    participant_deliveries: list[IRActionParticipantDelivery] = Field(
        default_factory=list
    )
    action_agency: ActionAgency
    text_provenance: TextProvenance
    required_text: str = ""
    prop_text: str = ""
    on_screen_text: str = ""
    resulting_state: str = ""
    speaker_key: str | None = None
    # Compiler-owned state ownership.  ``environment_only`` is an explicit
    # typed assertion, never the fallback for missing character attribution.
    state_subject_key: str = ""
    state_subject_keys: list[str] = Field(default_factory=list)
    environment_only: bool = False
    function: str = "statement"
    source_text: str = ""
    chain_key: str = ""
    performance: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = derive_action_agency_payload(
            value,
            actor_field="actor_keys",
            target_field="target_keys",
            speaker_field="speaker_key",
            source_field="source_segment_ids",
        )
        kind = str(normalized.get("kind") or "action").strip().lower()
        if kind in {"speech", "spoken", "line", "voice"}:
            kind = "dialogue"
        elif kind not in {"action", "dialogue"}:
            kind = "action"
        normalized["kind"] = kind
        return derive_text_provenance_payload(
            normalized,
            actor_field="actor_keys",
            target_field="target_keys",
            speaker_field="speaker_key",
            source_field="source_segment_ids",
            dialogue=kind == "dialogue",
        )

    @field_validator(
        "source_segment_ids", "actor_keys", "target_keys",
        "onscreen_entity_keys", "participant_deliveries",
        "state_subject_keys", mode="before",
    )
    @classmethod
    def _normalize_source_ids(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @model_validator(mode="after")
    def _validate_action_agency(self) -> "IRSceneUnit":
        if self.state_subject_key and not self.state_subject_keys:
            self.state_subject_keys = [self.state_subject_key]
        identity_bearing = bool(
            self.actor_keys or self.target_keys or self.speaker_key
        )
        if self.action_agency.identity_bearing != identity_bearing:
            raise ValueError(
                "action_agency.identity_bearing 必须与 "
                "actor_keys/target_keys/speaker_key 等价"
            )
        if self.action_agency.is_character_agency and not identity_bearing:
            raise ValueError(
                "character action_agency 必须由 "
                "actor_keys/target_keys/speaker_key 承载"
            )
        if self.action_agency.source_segment_ids != self.source_segment_ids:
            raise ValueError(
                "action_agency.source_segment_ids 必须与 unit 来源等价"
            )
        if self.state_subject_key and self.state_subject_keys != [
            self.state_subject_key
        ]:
            raise ValueError(
                "unit state_subject_key 必须等于唯一 state_subject_keys 成员"
            )
        if self.environment_only and self.state_subject_keys:
            raise ValueError(
                "unit 不得同时声明 state_subject_keys 与 environment_only"
            )
        _validate_text_provenance(
            provenance=self.text_provenance,
            relation_keys=[
                *self.actor_keys,
                *self.target_keys,
                *([self.speaker_key] if self.speaker_key else []),
            ],
            source_segment_ids=self.source_segment_ids,
            dialogue=self.kind == "dialogue",
            dialogue_text=self.text if self.kind == "dialogue" else "",
            required_text=self.required_text,
            prop_text=self.prop_text,
            on_screen_text=self.on_screen_text,
            label="unit",
        )
        return self


class IRScene(BaseModel):
    key: str
    scene_heading: str
    story_function: str = Field(min_length=SCENE_STORY_FUNCTION_MIN_CHARS)
    character_keys: list[str] = Field(default_factory=list)
    summary: str
    conflict: str = ""
    turn: str = ""
    source_basis: str = ""
    previous_scene_exit_state: str = ""
    opening_image: str = ""
    agency_contracts: list[dict[str, str]] = Field(default_factory=list)
    entry_state: str = ""
    exit_state: str = ""
    context_requirements: list[str] = Field(default_factory=list)
    units: list[IRSceneUnit] = Field(default_factory=list)

    @field_validator(
        "character_keys", "context_requirements", "units", mode="before",
    )
    @classmethod
    def _normalize_lists(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @field_validator("story_function")
    @classmethod
    def _validate_story_function(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < SCENE_STORY_FUNCTION_MIN_CHARS:
            raise ValueError(
                "story_function 必须完整说明本场戏剧功能，"
                f"至少 {SCENE_STORY_FUNCTION_MIN_CHARS} 个字符"
            )
        return normalized
