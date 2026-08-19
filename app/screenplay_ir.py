"""Compact screenplay generation IR and deterministic EpisodeScreenplay compiler.

The text model authors semantic decisions once. This module expands those
decisions into the verbose, reference-complete contract consumed by storyboard,
continuity, identity and certificate code. The published schema remains
``EpisodeScreenplay`` / ``NarrativeContinuityPlan``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app import config, textmatch
from app.character_policy import (
    resolution_declares_functional_identity,
)
from app.identity_authority import (
    backend_owned_identity_authority,
    identity_authority_registry,
    model_identity_authority_prompt_rule,
)
from app.narrative_blueprint import (
    BlueprintSourceAuditAnnotation,
    BlueprintSourceSemantics,
    _normalize_source_segment_id,
)
from app.schemas import (
    ActionAgency,
    Bible,
    EpisodeScreenplay,
    InformationItem,
    KeyDialogueChain,
    KeyDialogueTurn,
    NARRATIVE_CONTRACT_VERSION,
    NarrativeContinuityPlan,
    PlotSpine,
    PlotSpineBeat,
    ScriptScene,
    SourceCoverageDecision,
    StoryEvent,
    system_environment_entity_id,
    TextProvenance,
    VoiceCanonical,
)
from app.renderability import (
    DIALOGUE_CHAIN_TURNS_HARD_MAX,
    SCENE_STORY_FUNCTION_MIN_CHARS,
)
from app.source_excerpt import (
    align_source_excerpt,
    index_compact_source_segments,
    index_source_segments,
    structural_front_matter_ids,
)
from app.spoken_contract import content_char_count


IR_VERSION = "screenplay-generation-ir.v4"
IR_COMPILER_VERSION = "screenplay-ir-compiler.v8"
IR_MAX_SOURCE_SEGMENTS_PER_UNIT = 16
IR_MIN_ADAPTED_SOURCE_RATIO = 0.35
IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO = 0.18
IR_LOCAL_SOURCE_WINDOW = 12
_DIALOGUE_FUNCTIONS = {
    "trigger", "announcement", "question", "response", "decision", "statement",
}
_AUDIT_SOURCE_SEMANTICS = BlueprintSourceSemantics(
    narrative_layer="paratext",
    event_priority="connective",
    render_policy="exclude_from_spine",
    disposition="audit_only",
    projection_policy="audit_only",
)
_SourceSemanticIdentity = tuple[str, str, str, str, str, str]
_SourceAuditAnnotationIdentity = tuple[
    str,
    tuple[str, ...],
    str,
    str,
    str,
    str,
]


class ScreenplayIRIdentityConflictError(ValueError):
    """Typed preflight identities still disagree after structural resolution."""

    def __init__(
        self,
        message: str,
        *,
        issues: list[dict[str, Any]] | None = None,
    ) -> None:
        self.issues = list(issues or [])
        super().__init__(message)


class ScreenplayIRFidelityError(ValueError):
    """Typed signal that a structurally valid IR needs bounded fidelity repair."""


def _structural_context_authority_id(
    episode: dict[str, Any],
    identity_key: str,
) -> str:
    """Mint an identity ID for compiler-created context actors, not a person guess."""
    seed = json.dumps(
        {
            "episode_id": str(
                episode.get("id") or episode.get("episode_no") or ""
            ),
            "identity_key": str(identity_key or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "context:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def scene_heading_has_multiple_locations(heading: str) -> bool:
    text = str(heading or "")
    location = text.split("/", 1)[1] if "/" in text else text
    location = location.rstrip("】 ").strip()
    return bool(re.search(r"[、+，,/]", location))


def screenplay_beat_fields_repeat(
    does: str,
    turn: str,
) -> bool:
    """Return whether action and outcome carry effectively the same content."""
    action = textmatch.condense(str(does or ""))
    outcome = textmatch.condense(str(turn or ""))
    if not action or not outcome:
        return False
    if action == outcome:
        return True
    if min(len(action), len(outcome)) < 8:
        return False
    return bool(
        min(
            textmatch.longest_run_ratio(action, outcome),
            textmatch.longest_run_ratio(outcome, action),
        )
        >= 0.9
        and min(
            textmatch.bigram_coverage(action, outcome),
            textmatch.bigram_coverage(outcome, action),
        )
        >= 0.9
    )


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def derive_action_agency_payload(
    value: dict[str, Any],
    *,
    actor_field: str,
    target_field: str,
    source_field: str,
    speaker_field: str | None = None,
) -> dict[str, Any]:
    """Fill missing agency fields from the relation owner, never global defaults."""
    normalized = dict(value)
    relation_keys = [
        *_as_list(normalized.get(actor_field)),
        *_as_list(normalized.get(target_field)),
    ]
    if speaker_field and normalized.get(speaker_field):
        relation_keys.append(normalized[speaker_field])
    identity_bearing = any(
        bool(str(key or "").strip()) for key in relation_keys
    )
    raw_agency = normalized.get("action_agency")
    if isinstance(raw_agency, ActionAgency):
        agency = raw_agency.model_dump(mode="json")
    elif isinstance(raw_agency, dict):
        agency = dict(raw_agency)
    else:
        agency = {}
    if not str(agency.get("kind") or "").strip():
        agency["kind"] = (
            "character" if identity_bearing else "unattributed"
        )
    if agency.get("identity_bearing") is None:
        agency["identity_bearing"] = identity_bearing
    if agency.get("source_segment_ids") is None:
        agency["source_segment_ids"] = _as_list(
            normalized.get(source_field)
        )
    normalized["action_agency"] = agency
    return normalized


def derive_text_provenance_payload(
    value: dict[str, Any],
    *,
    actor_field: str,
    target_field: str,
    source_field: str,
    speaker_field: str | None = None,
    dialogue: bool = False,
) -> dict[str, Any]:
    """Derive text attribution from typed content and frozen relations."""
    normalized = dict(value)
    if normalized.get("text_provenance") is not None:
        return normalized
    relation_keys = [
        *_as_list(normalized.get(actor_field)),
        *_as_list(normalized.get(target_field)),
    ]
    if speaker_field and normalized.get(speaker_field):
        relation_keys.append(normalized[speaker_field])
    relation_keys = list(dict.fromkeys(
        str(key or "").strip()
        for key in relation_keys
        if str(key or "").strip()
    ))
    if str(normalized.get("required_text") or "").strip():
        provenance_kind = "required_text"
    elif str(normalized.get("prop_text") or "").strip():
        provenance_kind = "prop_text"
    elif str(normalized.get("on_screen_text") or "").strip():
        provenance_kind = "on_screen_text"
    elif dialogue or str(normalized.get("dialogue_text") or "").strip():
        provenance_kind = "dialogue"
    else:
        provenance_kind = "creative_action"
    normalized["text_provenance"] = {
        "kind": provenance_kind,
        "identity_keys": (
            []
            if provenance_kind in (
                "required_text", "prop_text", "on_screen_text",
            )
            else relation_keys
        ),
        "source_segment_ids": _as_list(normalized.get(source_field)),
    }
    return normalized


def _validate_text_provenance(
    *,
    provenance: TextProvenance,
    relation_keys: list[str],
    source_segment_ids: list[str],
    dialogue: bool,
    dialogue_text: str,
    required_text: str,
    prop_text: str,
    on_screen_text: str,
    label: str,
) -> None:
    explicit_kinds = [
        kind
        for kind, content in (
            ("dialogue", dialogue_text),
            ("required_text", required_text),
            ("prop_text", prop_text),
            ("on_screen_text", on_screen_text),
        )
        if str(content or "").strip()
    ]
    if len(explicit_kinds) > 1:
        raise ValueError(
            f"{label} dialogue/required_text/prop_text/on_screen_text "
            "最多声明一种"
        )
    expected_kind = (
        explicit_kinds[0]
        if explicit_kinds
        else "dialogue" if dialogue else "creative_action"
    )
    expected_identity_keys = (
        []
        if expected_kind in (
            "required_text", "prop_text", "on_screen_text",
        )
        else list(dict.fromkeys(
            str(key or "").strip()
            for key in relation_keys
            if str(key or "").strip()
        ))
    )
    if provenance.kind != expected_kind:
        raise ValueError(
            f"{label} text_provenance.kind 必须由 slot/content 结构确定"
        )
    if provenance.identity_keys != expected_identity_keys:
        raise ValueError(
            f"{label} text_provenance.identity_keys 必须由冻结关系确定"
        )
    if provenance.source_segment_ids != source_segment_ids:
        raise ValueError(
            f"{label} text_provenance.source_segment_ids 必须与来源等价"
        )


def screenplay_ir_version_key(value: object) -> tuple[int, int]:
    """Parse this contract family without enumerating accepted versions."""
    match = re.fullmatch(
        r"screenplay-generation-ir\.v(?P<major>\d+)(?:\.(?P<minor>\d+))?",
        str(value or "").strip(),
    )
    if match is None:
        return (0, 0)
    return (
        int(match.group("major")),
        int(match.group("minor") or 0),
    )


def screenplay_ir_missing_participant_delivery_paths(
    value: object,
) -> list[str]:
    """Report absent evidence fields without manufacturing empty contracts."""
    if not isinstance(value, dict):
        return ["$"]
    missing: list[str] = []
    for scene_index, scene in enumerate(value.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        for unit_index, unit in enumerate(scene.get("units") or []):
            if (
                isinstance(unit, dict)
                and "participant_deliveries" not in unit
            ):
                missing.append(
                    f"scenes[{scene_index}].units[{unit_index}]"
                    ".participant_deliveries"
                )
    for event_index, event in enumerate(value.get("events") or []):
        if (
            isinstance(event, dict)
            and "participant_deliveries" not in event
        ):
            missing.append(
                f"events[{event_index}].participant_deliveries"
            )
    return missing


def screenplay_ir_missing_event_semantic_paths(value: object) -> list[str]:
    """Require explicit story-layer and rendering decisions in current IR."""
    if not isinstance(value, dict):
        return ["$"]
    required = ("narrative_layer", "event_priority", "render_policy")
    required_source = (
        *required,
        "disposition",
        "projection_policy",
    )
    missing: list[str] = []
    source_semantics = value.get("source_semantics")
    if not isinstance(source_semantics, dict):
        missing.append("source_semantics")
        source_semantics = {}
    related_source_ids = {
        str(source_id)
        for source_id in (value.get("source_scene_owners") or {})
        if str(source_id)
    }
    for scene_index, scene in enumerate(value.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        for unit_index, unit in enumerate(scene.get("units") or []):
            if not isinstance(unit, dict):
                continue
            related_source_ids.update(
                str(source_id)
                for source_id in unit.get("source_segment_ids") or []
                if str(source_id)
            )
            for field in required:
                if field not in unit:
                    missing.append(
                        f"scenes[{scene_index}].units[{unit_index}].{field}"
                    )
    for event_index, event in enumerate(value.get("events") or []):
        if not isinstance(event, dict):
            continue
        related_source_ids.update(
            str(source_id)
            for source_id in event.get("source_segment_ids") or []
            if str(source_id)
        )
        for field in required:
            if field not in event:
                missing.append(f"events[{event_index}].{field}")
    for coverage_index, coverage in enumerate(value.get("coverage") or []):
        if not isinstance(coverage, dict):
            continue
        related_source_ids.update(
            str(source_id)
            for source_id in coverage.get("source_segment_ids") or []
            if str(source_id)
        )
        if "projection_policy" not in coverage:
            missing.append(
                f"coverage[{coverage_index}].projection_policy"
            )
    for source_id in sorted(related_source_ids):
        semantics = source_semantics.get(source_id)
        if not isinstance(semantics, dict):
            missing.append(f"source_semantics[{source_id}]")
            continue
        for field in required_source:
            if field not in semantics:
                missing.append(f"source_semantics[{source_id}].{field}")
    return missing


def _canonical_source_semantic_identity(
    source_id: object,
    semantics: BlueprintSourceSemantics,
) -> _SourceSemanticIdentity:
    return (
        _normalize_source_segment_id(source_id),
        semantics.narrative_layer,
        semantics.event_priority,
        semantics.render_policy,
        semantics.disposition,
        semantics.projection_policy,
    )


def _canonical_source_audit_annotation_identity(
    annotation: object,
) -> _SourceAuditAnnotationIdentity:
    typed = (
        annotation
        if isinstance(annotation, BlueprintSourceAuditAnnotation)
        else BlueprintSourceAuditAnnotation.model_validate(annotation)
    )
    return (
        typed.node_key.strip(),
        tuple(sorted(
            _normalize_source_segment_id(source_id)
            for source_id in typed.source_segment_ids
        )),
        typed.narrative_layer,
        typed.render_policy,
        typed.disposition,
        typed.projection_policy,
    )


def screenplay_ir_source_audit_contract_errors(
    value: object,
    *,
    expected_source_audit_annotations: list[object] | None = None,
) -> list[str]:
    """Validate the explicit audit authority carried by the current IR."""
    if not isinstance(value, dict):
        return ["[IR_SOURCE_AUDIT_CONTRACT] payload 必须是对象"]
    if str(value.get("format_version") or IR_VERSION) != IR_VERSION:
        return []
    if "source_audit_annotations" not in value:
        return [
            "[IR_SOURCE_AUDIT_FIELD_MISSING] "
            "source_audit_annotations 必须显式提供"
        ]
    annotations = value.get("source_audit_annotations")
    if not isinstance(annotations, list):
        return [
            "[IR_SOURCE_AUDIT_INVALID] "
            "source_audit_annotations 必须是数组"
        ]

    errors: list[str] = []
    annotation_identities: list[_SourceSemanticIdentity] = []
    annotation_authority_identities: list[
        _SourceAuditAnnotationIdentity
    ] = []
    annotation_node_keys: list[str] = []
    required_annotation_fields = set(
        BlueprintSourceAuditAnnotation.model_fields
    )
    for index, annotation in enumerate(annotations):
        if isinstance(annotation, BlueprintSourceAuditAnnotation):
            annotation = annotation.model_dump(mode="json")
        if not isinstance(annotation, dict):
            errors.append(
                f"[IR_SOURCE_AUDIT_INVALID] "
                f"source_audit_annotations[{index}] 必须是对象"
            )
            continue
        missing_fields = required_annotation_fields - set(annotation)
        if missing_fields:
            errors.append(
                "[IR_SOURCE_AUDIT_FIELD_MISSING] "
                f"source_audit_annotations[{index}] 缺少显式字段："
                + "、".join(sorted(missing_fields))
            )
        node_key = str(annotation.get("node_key") or "").strip()
        source_ids = annotation.get("source_segment_ids")
        if not node_key or not isinstance(source_ids, list) or not source_ids:
            errors.append(
                "[IR_SOURCE_AUDIT_INVALID] "
                f"source_audit_annotations[{index}] 缺少节点或来源"
            )
            continue
        try:
            typed_annotation = BlueprintSourceAuditAnnotation.model_validate(
                annotation
            )
        except ValueError:
            errors.append(
                "[IR_SOURCE_AUDIT_SEMANTIC_CONFLICT] "
                f"source_audit_annotations[{index}] 违反 audit 语义合同"
            )
            continue
        annotation_node_keys.append(typed_annotation.node_key.strip())
        annotation_authority_identities.append(
            _canonical_source_audit_annotation_identity(typed_annotation)
        )
        annotation_identities.extend(
            _canonical_source_semantic_identity(
                source_id,
                _AUDIT_SOURCE_SEMANTICS,
            )
            for source_id in typed_annotation.source_segment_ids
        )

    coverage_identities: list[_SourceSemanticIdentity] = []
    for group in value.get("coverage") or []:
        if isinstance(group, BaseModel):
            group = group.model_dump(mode="json")
        if not isinstance(group, dict) or not (
            group.get("disposition") == "audit_only"
            or group.get("projection_policy") == "audit_only"
        ):
            continue
        try:
            typed_group = IRCoverageGroup.model_validate(group)
        except ValueError:
            errors.append(
                "[IR_SOURCE_AUDIT_SEMANTIC_CONFLICT] "
                "coverage 违反 audit 语义合同"
            )
            continue
        coverage_identities.extend(
            _canonical_source_semantic_identity(
                source_id,
                _AUDIT_SOURCE_SEMANTICS,
            )
            for source_id in typed_group.source_segment_ids
        )
    source_semantics = value.get("source_semantics")
    semantic_identities: list[_SourceSemanticIdentity] = []
    for source_id, semantics in (
        source_semantics.items()
        if isinstance(source_semantics, dict)
        else ()
    ):
        if not isinstance(semantics, dict) or not (
            semantics.get("disposition") == "audit_only"
            or semantics.get("projection_policy") == "audit_only"
        ):
            continue
        try:
            typed_semantics = BlueprintSourceSemantics.model_validate(
                semantics
            )
        except ValueError:
            errors.append(
                "[IR_SOURCE_AUDIT_SEMANTIC_CONFLICT] "
                f"source_semantics[{source_id}] 违反来源语义合同"
            )
            continue
        semantic_identities.append(
            _canonical_source_semantic_identity(
                source_id,
                typed_semantics,
            )
        )
    for label, identities in (
        ("annotation node", annotation_node_keys),
        ("annotation source", annotation_identities),
        ("coverage audit source", coverage_identities),
        ("semantic audit source", semantic_identities),
    ):
        if len(identities) != len(set(identities)):
            errors.append(
                f"[IR_SOURCE_AUDIT_DUPLICATE] {label} 含重复 identity"
            )
    if (
        set(annotation_identities) != set(coverage_identities)
        or set(annotation_identities) != set(semantic_identities)
    ):
        errors.append(
            "[IR_SOURCE_AUDIT_COVERAGE_MISMATCH] "
            "source_audit_annotations、coverage 与 source_semantics "
            "必须完整一致："
            f"annotations={annotation_identities}, "
            f"coverage={coverage_identities}, "
            f"semantics={semantic_identities}"
        )
    if expected_source_audit_annotations is not None:
        try:
            expected_authority_identities = [
                _canonical_source_audit_annotation_identity(annotation)
                for annotation in expected_source_audit_annotations
            ]
        except ValueError:
            errors.append(
                "[IR_SOURCE_AUDIT_AUTHORITY_INVALID] "
                "Blueprint source_audit_annotations 违反 audit 语义合同"
            )
        else:
            if sorted(annotation_authority_identities) != sorted(
                expected_authority_identities
            ):
                errors.append(
                    "[IR_SOURCE_AUDIT_AUTHORITY_MISMATCH] "
                    "source_audit_annotations 必须保留 Blueprint 的 "
                    "node/source/semantics 完整绑定："
                    f"actual={sorted(annotation_authority_identities)}, "
                    f"expected={sorted(expected_authority_identities)}"
                )
    return list(dict.fromkeys(errors))


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


def normalize_screenplay_ir_payload(
    value: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize provider shape drift and return an auditable change ledger."""
    normalized = deepcopy(value)
    changes: list[dict[str, Any]] = []

    def record(path: str, before: Any, after: Any, reason: str) -> None:
        if before == after:
            return
        changes.append({
            "path": path,
            "from": before,
            "to": after,
            "reason": reason,
        })

    coverage = normalized.get("coverage")
    if isinstance(coverage, list):
        normalized_coverage = []
        for index, item in enumerate(coverage):
            if not isinstance(item, dict):
                normalized_coverage.append(item)
                continue
            row = dict(item)
            if "source_segment_ids" not in row:
                before = row.get("segment_ids") or row.get("segments")
                if before is not None:
                    row["source_segment_ids"] = _as_list(before)
                    record(
                        f"coverage[{index}].source_segment_ids",
                        None,
                        row["source_segment_ids"],
                        "provider_alias",
                    )
            if "disposition" not in row and row.get("coverage_type"):
                row["disposition"] = row["coverage_type"]
                record(
                    f"coverage[{index}].disposition",
                    None,
                    row["disposition"],
                    "provider_alias",
                )
            if "reason" not in row and row.get("context_note"):
                row["reason"] = row["context_note"]
                record(
                    f"coverage[{index}].reason",
                    None,
                    row["reason"],
                    "provider_alias",
                )
            normalized_coverage.append(row)
        normalized["coverage"] = normalized_coverage

    for index, event in enumerate(normalized.get("events") or []):
        if not isinstance(event, dict):
            continue
        for field in (
            "source_segment_ids", "actor_keys", "target_keys", "onscreen_entity_keys",
            "participant_deliveries", "causal_parent_keys", "action_phases", "perceivable_by",
            "information",
        ):
            before = event.get(field)
            if before is not None and not isinstance(before, list):
                event[field] = _as_list(before)
                record(
                    f"events[{index}].{field}",
                    before,
                    event[field],
                    "scalar_to_list",
                )
        if not str(event.get("source_excerpt") or "").strip():
            changes.append({
                "path": f"events[{index}].source_excerpt",
                "from": event.get("source_excerpt"),
                "to": "compiler_derived_from_source_segment_ids",
                "reason": "deterministic_source_alignment",
            })

    stance_aliases = {
        "neutral": "suspected",
        "unknown": "suspected",
        "undetermined": "suspected",
        "not_applicable": "suspected",
        "accepted": "believed",
        "true": "believed",
        "denied": "rejected",
        "false": "rejected",
    }
    for index, prior in enumerate(normalized.get("audience_priors") or []):
        if not isinstance(prior, dict):
            continue
        familiarity = prior.get("familiarity_assumptions")
        if isinstance(familiarity, list):
            projected = [
                item if isinstance(item, dict)
                else {"description": str(item)}
                for item in familiarity
            ]
            if projected != familiarity:
                prior["familiarity_assumptions"] = projected
                record(
                    f"audience_priors[{index}].familiarity_assumptions",
                    familiarity,
                    projected,
                    "string_to_structured_assumption",
                )
        stance = str(prior.get("target_stance") or "suspected").strip().lower()
        projected_stance = stance_aliases.get(stance, stance)
        if projected_stance not in {"believed", "suspected", "rejected"}:
            projected_stance = "suspected"
        if projected_stance != stance:
            prior["target_stance"] = projected_stance
            record(
                f"audience_priors[{index}].target_stance",
                stance,
                projected_stance,
                "open_stance_to_supported_belief_state",
            )

    if not normalized.get("beats"):
        changes.append({
            "path": "beats",
            "from": normalized.get("beats"),
            "to": "compiler_derived_from_events",
            "reason": "single_semantic_authority",
        })
    if len(normalized.get("audience_priors") or []) < 2:
        changes.append({
            "path": "audience_priors",
            "from": normalized.get("audience_priors"),
            "to": "compiler_derived_project_priors",
            "reason": "deterministic_audience_contract",
        })
    normalized["normalization_log"] = [
        *(normalized.get("normalization_log") or []),
        *changes,
    ]
    return normalized, changes


def _normalize_duplicate_ir_identity_displays(
    value: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    audit: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_display: defaultdict[str, list[IRIdentity]] = defaultdict(list)
    for identity in value.identities:
        display_name = str(identity.display_name or "").strip()
        if display_name:
            by_display[display_name].append(identity)
    duplicate_groups = {
        display_name: identities
        for display_name, identities in by_display.items()
        if len(identities) > 1
    }
    if not duplicate_groups:
        return []

    segment_list = index_source_segments(source_text)
    segments = {segment.segment_id: segment for segment in segment_list}
    source_order = {
        segment.segment_id: index
        for index, segment in enumerate(segment_list)
    }
    first_use: dict[str, int] = {}
    owned_source_ids: defaultdict[str, set[str]] = defaultdict(set)
    position = 0
    for scene in value.scenes:
        for unit in scene.units:
            position += 1
            if not unit.speaker_key:
                continue
            first_use.setdefault(unit.speaker_key, position)
            owned_source_ids[unit.speaker_key].update(
                unit.source_segment_ids
            )
    for event in value.events:
        position += 1
        for key in [*event.actor_keys, *event.target_keys]:
            first_use.setdefault(key, position)
            if owned_source_ids.get(key):
                continue
            owned_source_ids[key].update(event.source_segment_ids)

    resolutions = [
        item
        for item in (episode.get("character_resolutions") or [])
        if (
            isinstance(item, dict)
            and resolution_declares_functional_identity(item)
            and str(item.get("source_label") or "").strip()
            and str(item.get("canonical_name") or "").strip()
        )
    ]
    bible_names = {character.name for character in bible.characters}
    claimed_names = {
        str(identity.display_name or "").strip()
        for identity in value.identities
        if str(identity.display_name or "").strip()
    }
    changes: list[dict[str, Any]] = []
    for display_name, identities in duplicate_groups.items():
        keeper = min(
            identities,
            key=lambda identity: (
                display_name not in bible_names,
                identity.asset_requirement != "required",
                identity.visual_policy != "canonical",
                first_use.get(identity.key, 10**9),
                identity.key,
            ),
        )
        duplicates = sorted(
            (
                identity
                for identity in identities
                if identity.key != keeper.key
            ),
            key=lambda identity: (
                first_use.get(identity.key, 10**9),
                identity.key,
            ),
        )
        for identity in duplicates:
            local_source = "\n".join(
                segments[source_id].text
                for source_id in sorted(
                    owned_source_ids.get(identity.key, set()),
                    key=lambda source_id: source_order.get(
                        source_id, len(source_order)
                    ),
                )
                if source_id in segments
            )
            candidates = [
                resolution
                for resolution in resolutions
                if (
                    str(resolution.get("canonical_name") or "").strip()
                    not in claimed_names
                    and str(resolution.get("source_label") or "").strip()
                    in local_source
                )
            ]
            candidates.sort(
                key=lambda resolution: (
                    source_text.find(
                        str(resolution.get("source_label") or "").strip()
                    ),
                    str(resolution.get("canonical_name") or ""),
                )
            )
            if not candidates:
                continue
            selected = candidates[0]
            canonical_name = str(
                selected.get("canonical_name") or ""
            ).strip()
            identity.display_name = canonical_name
            claimed_names.add(canonical_name)
            change = {
                "path": f"identities.{identity.key}.display_name",
                "operation": "apply_functional_identity_resolution",
                "from": display_name,
                "to": canonical_name,
                "source_label": str(
                    selected.get("source_label") or ""
                ).strip(),
                "reason": (
                    "duplicate_display_token_resolved_by_owned_source_segment"
                ),
            }
            changes.append(change)
            audit.append(change)
    return changes


def _apply_authoritative_ir_identity_resolutions(
    value: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    bible: Bible,
    audit: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind IR identities through exact authority references only.

    The function intentionally performs no semantic inference.  Missing or
    conflicting bindings are surfaced to the async AI adjudication stage.
    """
    changes, issues = prepare_ir_identity_authorities(
        value,
        episode=episode,
        bible=bible,
        audit=audit,
    )
    if issues:
        first = issues[0]
        reason = str(first.get("reason") or "identity_authority_unresolved")
        if reason == "multiple_exact_authorities":
            message = (
                f"IR 身份 {first.get('identity_key')} 命中冲突的身份权威："
                + "、".join(first.get("candidate_authority_ids") or [])
            )
        else:
            message = (
                f"IR 身份 {first.get('identity_key')} 缺少可验证的身份权威"
            )
        raise ScreenplayIRIdentityConflictError(message, issues=issues)
    return changes


def _bind_ir_identity_authority(
    identity: IRIdentity,
    authority: dict[str, Any],
    *,
    bible_by_name: dict[str, Any],
    audit: list[dict[str, Any]],
) -> dict[str, Any] | None:
    authority_id = str(authority.get("authority_id") or "").strip()
    canonical_name = str(authority.get("canonical_name") or "").strip()
    if not authority_id or not canonical_name:
        return None
    before = {
        "authority_id": identity.authority_id,
        "display_name": identity.display_name,
        "source_names": list(identity.source_names),
        "role_type": identity.role_type,
    }
    identity.authority_id = authority_id
    identity.display_name = canonical_name
    identity.source_names = list(dict.fromkeys([
        *identity.source_names,
        *(
            str(value or "").strip()
            for value in authority.get("source_labels") or []
            if str(value or "").strip()
        ),
    ]))
    character = bible_by_name.get(canonical_name)
    if character is not None:
        identity.kind = character.role or identity.kind
        identity.visual_policy = "canonical"
        identity.visual_canonical = character.appearance_canonical
        identity.asset_requirement = "required"
        identity.voice_canonical = (
            character.speech_style
            or character.personality
            or identity.voice_canonical
        )
        identity.role_type = "named_character"
    elif str(authority.get("identity_kind") or "") == "functional":
        identity.role_type = "functional_character"
    after = {
        "authority_id": identity.authority_id,
        "display_name": identity.display_name,
        "source_names": list(identity.source_names),
        "role_type": identity.role_type,
    }
    if before == after:
        return None
    change = {
        "path": f"identities.{identity.key}",
        "operation": str(
            authority.get("binding_operation")
            or "bind_exact_identity_authority"
        ),
        "from": before,
        "to": after,
        "reason": str(
            authority.get("binding_reason")
            or "explicit_or_unique_exact_authority_reference"
        ),
    }
    audit.append(change)
    return change


def _rewrite_ir_identity_key(
    value: ScreenplayGenerationIR,
    old_key: str,
    new_key: str,
) -> None:
    def replace(tokens: list[str]) -> list[str]:
        return list(dict.fromkeys(
            new_key if token == old_key else token
            for token in tokens
        ))

    for scene in value.scenes:
        scene.character_keys = replace(scene.character_keys)
        for unit in scene.units:
            if unit.speaker_key == old_key:
                unit.speaker_key = new_key
            unit.actor_keys = replace(unit.actor_keys)
            unit.target_keys = replace(unit.target_keys)
            unit.onscreen_entity_keys = replace(unit.onscreen_entity_keys)
            unit.text_provenance.content_owner_keys = replace(
                unit.text_provenance.content_owner_keys
            )
            for delivery in unit.participant_deliveries:
                if delivery.participant_key == old_key:
                    delivery.participant_key = new_key
    for event in value.events:
        event.actor_keys = replace(event.actor_keys)
        event.target_keys = replace(event.target_keys)
        event.onscreen_entity_keys = replace(event.onscreen_entity_keys)
        event.perceivable_by = replace(event.perceivable_by)
        event.text_provenance.content_owner_keys = replace(
            event.text_provenance.content_owner_keys
        )
        for delivery in event.participant_deliveries:
            if delivery.participant_key == old_key:
                delivery.participant_key = new_key
    for beat in value.beats:
        if beat.who == old_key:
            beat.who = new_key


def _merge_ir_identities_with_same_authority(
    value: ScreenplayGenerationIR,
    *,
    explicit_identity_keys: set[str],
    audit: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_authority: defaultdict[str, list[IRIdentity]] = defaultdict(list)
    for identity in value.identities:
        if identity.authority_id:
            by_authority[identity.authority_id].append(identity)
    removed: set[str] = set()
    changes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for authority_id, identities in by_authority.items():
        if len(identities) < 2:
            continue
        identity_keys = {identity.key for identity in identities}
        if not identity_keys.issubset(explicit_identity_keys):
            issues.append({
                "identity_key": identities[0].key,
                "identity_keys": [identity.key for identity in identities],
                "reason": "shared_inferred_authority",
                "candidate_authority_ids": [authority_id],
            })
            continue
        keeper = identities[0]
        for duplicate in identities[1:]:
            _rewrite_ir_identity_key(value, duplicate.key, keeper.key)
            keeper.source_names = list(dict.fromkeys([
                *keeper.source_names,
                *duplicate.source_names,
            ]))
            removed.add(duplicate.key)
            change = {
                "path": f"identities.{duplicate.key}",
                "operation": "merge_same_identity_authority",
                "from": duplicate.key,
                "to": keeper.key,
                "authority_id": authority_id,
                "reason": "identical_explicit_authority_reference",
            }
            changes.append(change)
            audit.append(change)
    if removed:
        value.identities = [
            identity for identity in value.identities
            if identity.key not in removed
        ]
    return changes, issues


def prepare_ir_identity_authorities(
    value: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    bible: Bible,
    audit: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply exact bindings and return unresolved semantic cases for AI."""
    referenced_identity_keys = {
        key
        for scene in value.scenes
        for key in [
            *scene.character_keys,
            *(
                key
                for unit in scene.units
                for key in (
                    *unit.actor_keys,
                    *unit.target_keys,
                    *unit.onscreen_entity_keys,
                    *unit.text_provenance.content_owner_keys,
                )
            ),
            *(
                unit.speaker_key
                for unit in scene.units
                if unit.speaker_key
            ),
        ]
        if key
    }
    referenced_identity_keys.update(
        key
        for event in value.events
        for key in [
            *event.actor_keys,
            *event.target_keys,
            *event.onscreen_entity_keys,
            *event.text_provenance.content_owner_keys,
            *(
                perceiver_key
                for perceiver_key in event.perceivable_by
                if perceiver_key != "audience"
            ),
        ]
        if key
    )
    orphan_identities = [
        identity
        for identity in value.identities
        if identity.key not in referenced_identity_keys
        and not any(
            str(beat.who or "").strip()
            in {identity.key, identity.display_name}
            for beat in value.beats
        )
    ]
    if orphan_identities:
        orphan_keys = {identity.key for identity in orphan_identities}
        value.identities = [
            identity
            for identity in value.identities
            if identity.key not in orphan_keys
        ]
        for identity in orphan_identities:
            audit.append({
                "path": f"identities.{identity.key}",
                "operation": "remove_unreferenced_identity",
                "reason": "identity_has_no_structural_scene_dialogue_event_or_beat_reference",
            })
    registry = identity_authority_registry(
        bible,
        episode.get("character_resolutions") or [],
    )
    by_id = {
        str(item.get("authority_id") or "").strip(): item
        for item in registry
        if str(item.get("authority_id") or "").strip()
    }
    bible_by_name = {
        str(character.name or "").strip(): character
        for character in bible.characters
        if str(character.name or "").strip()
    }
    legacy_self_authority = bool(
        not str(value.format_version or "").startswith(
            "screenplay-generation-ir.v1.4"
        )
        and not (episode.get("character_resolutions") or [])
    )
    changes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    explicit_identity_keys = {
        identity.key
        for identity in value.identities
        if str(identity.authority_id or "").strip()
    }
    for identity in value.identities:
        explicit = str(identity.authority_id or "").strip()
        authority = by_id.get(explicit) if explicit else None
        if authority is None:
            authority = backend_owned_identity_authority(
                identity_key=identity.key,
                display_name=identity.display_name,
                role_type=identity.role_type,
                source_names=identity.source_names,
            )
        if (
            explicit
            and authority is None
            and identity.kind in {
                "source_backed_scene_context_actor",
                "event_referenced_contextual_identity",
            }
        ):
            expected_context_id = _structural_context_authority_id(
                episode,
                identity.key,
            )
            if explicit == expected_context_id:
                authority = {
                    "authority_id": explicit,
                    "canonical_name": identity.display_name or identity.key,
                    "identity_kind": "functional",
                    "source_labels": list(identity.source_names),
                }
        if explicit and authority is None and legacy_self_authority:
            legacy_seed = json.dumps(
                {
                    "key": identity.key,
                    "display_name": identity.display_name,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            expected_legacy_id = "legacy:" + hashlib.sha256(
                legacy_seed.encode("utf-8")
            ).hexdigest()[:16]
            if explicit == expected_legacy_id:
                authority = {
                    "authority_id": explicit,
                    "canonical_name": identity.display_name or identity.key,
                    "identity_kind": (
                        "named"
                        if identity.role_type == "named_character"
                        else "functional"
                    ),
                    "source_labels": list(identity.source_names),
                }
        if explicit and authority is None:
            issues.append({
                "identity_key": identity.key,
                "reason": "unknown_explicit_authority",
                "authority_id": explicit,
            })
            continue
        if authority is None:
            tokens = {
                str(identity.display_name or "").strip(),
                *(
                    str(name or "").strip()
                    for name in identity.source_names
                    if str(name or "").strip()
                ),
            }
            candidate_ids = {
                str(item.get("authority_id") or "").strip()
                for item in registry
                if (
                    str(item.get("canonical_name") or "").strip() in tokens
                    or bool(tokens.intersection({
                        str(label or "").strip()
                        for label in item.get("source_labels") or []
                        if str(label or "").strip()
                    }))
                )
            }
            candidate_ids.discard("")
            if len(candidate_ids) == 1:
                authority = by_id[next(iter(candidate_ids))]
            elif len(candidate_ids) > 1:
                issues.append({
                    "identity_key": identity.key,
                    "reason": "multiple_exact_authorities",
                    "tokens": sorted(tokens),
                    "candidate_authority_ids": sorted(candidate_ids),
                })
                continue
            else:
                if legacy_self_authority:
                    legacy_seed = json.dumps(
                        {
                            "key": identity.key,
                            "display_name": identity.display_name,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    authority = {
                        "authority_id": "legacy:" + hashlib.sha256(
                            legacy_seed.encode("utf-8")
                        ).hexdigest()[:16],
                        "canonical_name": (
                            identity.display_name or identity.key
                        ),
                        "identity_kind": (
                            "named"
                            if identity.role_type == "named_character"
                            else "functional"
                        ),
                        "source_labels": list(identity.source_names),
                    }
                else:
                    issues.append({
                        "identity_key": identity.key,
                        "reason": "missing_exact_authority",
                        "tokens": sorted(tokens),
                        "candidate_authority_ids": [],
                    })
                    continue
        change = _bind_ir_identity_authority(
            identity,
            authority,
            bible_by_name=bible_by_name,
            audit=audit,
        )
        if change:
            changes.append(change)

    merge_changes, merge_issues = _merge_ir_identities_with_same_authority(
        value,
        explicit_identity_keys=explicit_identity_keys,
        audit=audit,
    )
    changes.extend(merge_changes)
    issues.extend(merge_issues)
    return changes, issues


def recover_complete_screenplay_ir_prefix(raw: str) -> dict[str, Any] | None:
    """Decode complete top-level IR members from a length-truncated object.

    ``JSONDecoder.raw_decode`` is used member by member, so string contents and
    nested structures still follow the JSON grammar. If the scenes array itself
    is truncated, only fully decoded scene objects are retained; the incomplete
    scene is discarded and the missing source tail must be authored separately.
    """
    text = str(raw or "").lstrip()
    if not text.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    index = 1
    recovered: dict[str, Any] = {}

    def skip_space(position: int) -> int:
        while position < len(text) and text[position].isspace():
            position += 1
        return position

    while True:
        index = skip_space(index)
        if index >= len(text) or text[index] == "}":
            break
        try:
            key, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        if not isinstance(key, str):
            break
        index = skip_space(index)
        if index >= len(text) or text[index] != ":":
            break
        index = skip_space(index + 1)
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            if key != "scenes" or index >= len(text) or text[index] != "[":
                break
            scene_index = skip_space(index + 1)
            complete_scenes: list[dict[str, Any]] = []
            while scene_index < len(text):
                try:
                    scene, scene_end = decoder.raw_decode(text, scene_index)
                except json.JSONDecodeError:
                    break
                if (
                    not isinstance(scene, dict)
                    or not isinstance(scene.get("units"), list)
                ):
                    break
                complete_scenes.append(scene)
                scene_index = skip_space(scene_end)
                if (
                    scene_index >= len(text)
                    or text[scene_index] != ","
                ):
                    break
                scene_index = skip_space(scene_index + 1)
            if complete_scenes:
                recovered["scenes"] = complete_scenes
                recovered["_scene_prefix_truncated"] = True
            break
        recovered[key] = value
        index = skip_space(index)
        if index >= len(text) or text[index] != ",":
            break
        index += 1

    scenes = recovered.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return None
    if not all(
        isinstance(scene, dict)
        and isinstance(scene.get("units"), list)
        for scene in scenes
    ):
        return None
    recovered.pop("events", None)
    scene_prefix_truncated = bool(
        recovered.pop("_scene_prefix_truncated", False)
    )
    recovered["normalization_log"] = [
        *(recovered.get("normalization_log") or []),
        {
            "path": "scenes" if scene_prefix_truncated else "events",
            "from": "truncated_provider_suffix",
            "to": (
                "complete_scene_prefix_requires_source_tail_continuation"
                if scene_prefix_truncated
                else "compiler_derived_from_complete_scene_units"
            ),
            "reason": (
                "durable_complete_scene_prefix_recovery"
                if scene_prefix_truncated
                else "durable_prefix_recovery"
            ),
        },
    ]
    return recovered


def screenplay_ir_prompt_contract() -> str:
    """Compact output contract included in the generation prompt."""
    return """{
  "format_version":"__IR_VERSION__",
  "episode_no":1,
  "metadata":{
    "title":"", "logline":"", "script_format_note":"场次化台本稿",
    "dramatic_question":"", "protagonist_goal":"", "obstacle":"", "stakes":"",
    "emotional_curve":"", "ending_hook":"", "source_basis":"",
    "adaptation_direction":"", "opening":"", "development":"",
    "conflict":"", "climax":"", "episode_premise":"",
    "must_keep_ending":"", "drop_list":[],
    "approved_adaptations":[], "forbidden_additions":[]
  },
  "identities":[{
    "key":"person_a", "authority_id":"__IDENTITY_AUTHORITY_CONTRACT__",
    "display_name":"人物谱准确姓名或功能身份",
    "source_names":["该身份在本集原文中的逐字称谓"],
    "kind":"当前来源定义的开放身份语义",
    "visual_policy":"canonical|contextual|collective|offscreen_only",
    "visual_canonical":"可见身份的中性识别锚点",
    "asset_requirement":"required|optional|forbidden",
    "voice_canonical":"声音描述", "role_type":"named_character|functional_character|narrator",
    "rationale":"身份与视觉/声音策略的来源理由"
  }],
  "coverage":[],
  "scenes":[{
    "key":"sc1", "scene_heading":"【场1】日 / 地点", "story_function":"",
    "summary":"", "conflict":"", "turn":"",
    "units":[
      {"kind":"action","text":"可拍动作","event_key":"ev1",
       "narrative_layer":"story",
       "event_priority":"causal",
       "render_policy":"standalone",
       "actor_keys":["person_a"],"target_keys":[],
       "onscreen_entity_keys":["person_a"],
       "action_agency":{"kind":"character","identity_bearing":true,
         "source_segment_ids":["SRC0001"]},
       "participant_deliveries":[],
       "resulting_state":"该动作完成后新成立的局势，禁止复述 text",
       "source_segment_ids":["SRC0001"]},
      {"kind":"dialogue","text":"改编台词","event_key":"ev1",
       "narrative_layer":"story",
       "event_priority":"supporting",
       "render_policy":"merge_adjacent",
       "actor_keys":[],"target_keys":[],
       "onscreen_entity_keys":["person_a"],
       "action_agency":{"kind":"character_voice","identity_bearing":true,
         "source_segment_ids":["SRC0001"]},
       "participant_deliveries":[],
       "resulting_state":"该话轮交付后人物/信息/决策发生的变化，禁止复述 text",
       "source_segment_ids":["SRC0001"],
       "speaker_key":"person_a","function":"statement",
       "source_text":"原文逐字话语","chain_key":"dc1"}
    ]
  }],
  "experience":{
    "director_objective":"", "satisfaction_criteria":"",
    "required_processing_s":1.0, "forbidden_misconceptions":[]
  }
}

System contract: do not create an identity, speaker, actor, target, or visible
character for prose-only environment/establishing events. Leave their typed
identity relations empty. The deterministic compiler alone may assign the
reserved environment:<episode-scope> narrative subject; it is never a person,
voice, scene character, or asset identity.""".replace(
        "__IDENTITY_AUTHORITY_CONTRACT__",
        model_identity_authority_prompt_rule(),
    ).replace("__IR_VERSION__", IR_VERSION)


def screenplay_ir_bible_context(
    bible: Bible,
    *,
    source_text: str,
    episode_no: int,
    character_resolutions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the source-evidenced Bible closure needed by this episode.

    Selection is driven only by current source mentions, persisted identity
    resolutions and scene discovery evidence. It does not use a role/name
    whitelist. Relationship edges touching selected characters remain present
    so the model does not lose the meaning of an interaction.
    """
    source = re.sub(r"\s+", "", source_text or "")
    resolution_tokens = {
        str(item.get(field) or "").strip()
        for item in (character_resolutions or [])
        if isinstance(item, dict)
        for field in ("source_label", "canonical_name")
        if str(item.get(field) or "").strip()
    }
    selected_names = {
        character.name
        for character in bible.characters
        if (
            character.name in source
            or character.name in resolution_tokens
        )
    }
    if not selected_names:
        # Empty evidence usually means a placeholder/very short source. Keep
        # the existing authority instead of guessing which identity is safe.
        selected_names = {character.name for character in bible.characters}

    characters: list[dict[str, Any]] = []
    for character in bible.characters:
        if character.name not in selected_names:
            continue
        payload = character.model_dump(
            mode="json",
            exclude={"ref_image_path", "portrait_prompt_override"},
        )
        payload["relationships"] = [
            relationship
            for relationship in payload.get("relationships") or []
            if (
                str(relationship.get("to") or "") in selected_names
                or str(relationship.get("to") or "") in source
            )
        ]
        characters.append(payload)

    scenes: list[dict[str, Any]] = []
    for scene in bible.scenes:
        aliases = [str(value or "").strip() for value in scene.aliases or []]
        evidence = [str(value or "").strip() for value in scene.discovery_sources or []]
        selected = bool(
            scene.name in source
            or any(alias and alias in source for alias in aliases)
            or int(scene.first_episode or 0) == int(episode_no)
            or any(
                item
                and (
                    re.sub(r"\s+", "", item) in source
                    or source in re.sub(r"\s+", "", item)
                )
                for item in evidence
            )
        )
        if not selected:
            continue
        scenes.append(scene.model_dump(
            mode="json",
            exclude={"ref_image_path", "scene_prompt_override"},
        ))

    return {
        "characters": characters,
        "world": bible.world.model_dump(mode="json"),
        "scenes": scenes,
    }


def _unique_by_key(values: list[Any], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        key = str(getattr(value, "key", "") or "").strip()
        if not key:
            raise ValueError(f"{label} 存在空 key")
        if key in result:
            raise ValueError(f"{label} key 重复：{key}")
        result[key] = value
    return result


def _semantic_key(domain: str, statement: str) -> str:
    normalized = re.sub(r"\s+", "", statement).casefold()
    digest = hashlib.sha256(f"{domain}:{normalized}".encode("utf-8")).hexdigest()[:16]
    return f"{domain}:{digest}"


def _first_sentence(value: str, *, minimum: int = 8) -> str:
    for item in re.findall(r"[^。！？\n]+[。！？]?", value or ""):
        candidate = item.strip()
        if len(re.sub(r"\s+", "", candidate)) >= minimum:
            return candidate
    return (value or "").strip()


def _split_spoken_line(value: str, *, max_chars: int) -> list[str]:
    """Split one authored utterance without rewriting its words."""
    line = str(value or "").strip()
    if not line or content_char_count(line) <= max_chars:
        return [line] if line else []
    clauses = [
        item.strip()
        for item in re.findall(r".*?[，。！？；,.!?;]|.+$", line)
        if item.strip()
    ]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        if current and content_char_count(current + clause) > max_chars:
            chunks.append(current)
            current = ""
        if content_char_count(clause) <= max_chars:
            current += clause
            continue
        for character in clause:
            if (
                current
                and content_char_count(current + character) > max_chars
            ):
                chunks.append(current)
                current = ""
            current += character
    if current:
        chunks.append(current)
    return [item.strip() for item in chunks if item.strip()]


def _screenplay_action_text(value: str) -> str:
    """Preserve source-authored action prose; schema fields own semantics."""
    return re.sub(r"\s{2,}", " ", str(value or "").strip()).strip(" ，,；;")


def _source_location(
    excerpt: str,
    *,
    source_text: str,
    source_segment_ids: list[str],
    segments: dict[str, Any],
    authorized_source_chapters: dict[str, str],
) -> tuple[str, int, int, str]:
    candidates = [excerpt]
    for segment_id in source_segment_ids:
        if segment_id not in segments:
            continue
        segment_text = segments[segment_id].text
        candidates.extend([
            _first_sentence(segment_text),
            _first_sentence(
                re.sub(r"^\s*【[^】]+】\s*", "", segment_text),
            ),
        ])
    candidates = list(dict.fromkeys(
        str(candidate or "").strip()
        for candidate in candidates
        if str(candidate or "").strip()
    ))
    candidates = [
        *[
            candidate for candidate in candidates
            if len(re.sub(r"\s+", "", candidate)) >= 8
        ],
        *[
            candidate for candidate in candidates
            if len(re.sub(r"\s+", "", candidate)) < 8
        ],
    ]
    for candidate in candidates:
        for chapter_id, chapter_text in authorized_source_chapters.items():
            offset = chapter_text.find(candidate)
            if offset >= 0:
                return chapter_id, offset, offset + len(candidate), candidate
        candidate_chars = len(re.sub(r"\s+", "", candidate))
        aligned = align_source_excerpt(
            candidate,
            source_text,
            min_match_chars=min(8, max(2, candidate_chars)),
        )
        if aligned is not None and not authorized_source_chapters:
            return (
                "source",
                aligned.start_offset,
                aligned.end_offset,
                aligned.excerpt,
            )
    raise ValueError(
        "事件来源摘录无法在授权章节中精确定位："
        + (excerpt[:80] if excerpt else "未提供摘录")
    )


def _dialogue_source_text(value: str, source_text: str) -> str:
    raw = str(value or "").strip()
    if raw and raw in source_text:
        return raw
    aligned = align_source_excerpt(raw, source_text, min_match_chars=2)
    if aligned is not None:
        return aligned.excerpt
    raise ValueError(f"对白 source_text 未在本集原文中找到：{raw[:80] or '空'}")


def _default_metadata(episode: dict[str, Any]) -> IRMetadata:
    ending = str(episode.get("cliffhanger") or "").strip()
    title = str(episode.get("title") or f"第{episode.get('episode_no') or 1}集")
    premise = str(episode.get("synopsis") or title)
    return IRMetadata(
        title=title,
        logline=premise,
        script_format_note="场次化台本稿，含场标、动作段与对白段",
        dramatic_question=f"{title}中的核心目标能否实现？",
        protagonist_goal="推动本集核心事件完成",
        obstacle="人物目标受到当前局势与关系阻碍",
        stakes="失败将使当前矛盾继续升级",
        emotional_curve="从建立处境到冲突升级，最终完成本集状态变化",
        ending_hook=ending,
        source_basis="依据本集授权原文完成改编",
        adaptation_direction="保留完整因果链并转换为可导演台本",
        opening="建立人物处境与本集目标",
        development="事件推进并形成阻力",
        conflict="核心矛盾正面发生",
        climax="关键行动改变局势",
        episode_premise=premise,
        must_keep_ending=ending,
    )


def compile_screenplay_ir(
    value: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    audit: list[dict[str, Any]] | None = None,
) -> EpisodeScreenplay:
    """Compile compact semantic IR into the unchanged published contract."""
    compiler_audit = audit if audit is not None else []
    if value.legacy_screenplay is not None:
        legacy = value.legacy_screenplay.model_copy(deep=True)
        legacy.id = legacy.id or str(episode.get("id") or "")
        return legacy

    metadata = value.metadata or _default_metadata(episode)
    episode_no = int(episode.get("episode_no") or value.episode_no or 0)
    if value.episode_no and value.episode_no != episode_no:
        compiler_audit.append({
            "path": "episode_no",
            "operation": "bind_to_authority_input",
            "from": value.episode_no,
            "to": episode_no,
            "reason": "request_scope_is_authoritative",
        })
        value.episode_no = episode_no
    if not value.scenes:
        raise ValueError("IR scenes 不能为空")
    format_version = str(value.format_version or "")
    version_key = screenplay_ir_version_key(format_version)
    strict_unit_ownership = version_key >= (1, 3)
    typed_visual_unit_contract = version_key >= (1, 5)
    segments_list = (
        index_compact_source_segments(source_text)
        if format_version.startswith("screenplay-generation-ir.v1.2")
        else index_source_segments(source_text)
    )
    segments = {item.segment_id: item for item in segments_list}
    audit_only_source_ids = {
        _normalize_source_segment_id(source_id)
        for group in value.coverage
        if group.disposition == "audit_only"
        for source_id in group.source_segment_ids
    }
    annotated_audit_identities = {
        _canonical_source_semantic_identity(
            source_id,
            _AUDIT_SOURCE_SEMANTICS,
        )
        for annotation in value.source_audit_annotations
        for source_id in annotation.source_segment_ids
    }
    coverage_audit_identities = {
        _canonical_source_semantic_identity(
            source_id,
            _AUDIT_SOURCE_SEMANTICS,
        )
        for group in value.coverage
        if group.disposition == "audit_only"
        for source_id in group.source_segment_ids
    }
    if (
        value.source_audit_annotations
        and annotated_audit_identities != coverage_audit_identities
    ):
        raise ValueError(
            "source_audit_annotations 与 audit-only coverage 不一致"
        )
    unknown_audit_sources = audit_only_source_ids - set(segments)
    if unknown_audit_sources:
        raise ValueError(
            "audit-only coverage 引用了不存在的来源段："
            + "、".join(sorted(unknown_audit_sources))
        )
    if strict_unit_ownership:
        leaked_audit_units = [
            unit.unit_key or unit.event_key
            for scene in value.scenes
            for unit in scene.units
            if audit_only_source_ids.intersection(
                _normalize_source_segment_id(source_id)
                for source_id in unit.source_segment_ids
            )
        ]
        if leaked_audit_units:
            raise ValueError(
                "audit-only 来源不得进入 scene units："
                + "、".join(leaked_audit_units)
            )
        multi_location_scenes = [
            scene.key
            for scene in value.scenes
            if scene_heading_has_multiple_locations(scene.scene_heading)
        ]
        if multi_location_scenes:
            raise ScreenplayIRFidelityError(
                "IR v1.3 场次标题包含多个不连续地点，需要按连续时空重新分场："
                + "、".join(multi_location_scenes)
            )
    if strict_unit_ownership and value.events:
        compiler_audit.append({
            "path": "events",
            "operation": "discard_model_projection",
            "count": len(value.events),
            "reason": "v1.3_scene_units_are_the_only_authored_timeline",
        })
        value.events = []
    if strict_unit_ownership and value.beats:
        compiler_audit.append({
            "path": "beats",
            "operation": "discard_persisted_derived_projection",
            "count": len(value.beats),
            "reason": "v1.3_events_and_beats_are_rebuilt_from_scene_units",
        })
        value.beats = []
    if not value.events:
        flat_units = [
            (scene, unit)
            for scene in value.scenes
            for unit in scene.units
        ]
        if not flat_units:
            raise ValueError("IR scenes.units 不能为空")
        front_matter_ids = (
            structural_front_matter_ids(segments_list)
            if strict_unit_ownership else set()
        )
        dramatic_segments = [
            segment for segment in segments_list
            if (
                segment.segment_id not in front_matter_ids
                and segment.segment_id not in audit_only_source_ids
            )
        ]
        expected_source_ids = {
            segment.segment_id for segment in dramatic_segments
        }
        all_source_ids = {
            segment.segment_id for segment in segments_list
        }
        if strict_unit_ownership:
            source_order = {
                segment.segment_id: index
                for index, segment in enumerate(segments_list)
            }
            for scene in value.scenes:
                normalized_units: list[IRSceneUnit] = []
                for unit in scene.units:
                    positions = [
                        source_order[source_id]
                        for source_id in unit.source_segment_ids
                        if source_id in source_order
                    ]
                    discontinuous = (
                        len(positions) > 1
                        and positions[-1] - positions[0] + 1
                        != len(set(positions))
                    )
                    if not discontinuous:
                        normalized_units.append(unit)
                        continue
                    clauses = [
                        clause.strip()
                        for clause in re.split(
                            r"(?<=[，。！？；])",
                            unit.text,
                        )
                        if clause.strip()
                    ]
                    assignments: dict[str, list[str]] = defaultdict(list)
                    for clause in clauses:
                        ranked = sorted(
                            (
                                (
                                    max(
                                        textmatch.longest_run_ratio(
                                            clause,
                                            segments[source_id].text,
                                        ),
                                        textmatch.bigram_coverage(
                                            clause,
                                            segments[source_id].text,
                                        ),
                                    ),
                                    source_id,
                                )
                                for source_id in unit.source_segment_ids
                            ),
                            reverse=True,
                        )
                        if ranked and ranked[0][0] >= 0.08:
                            assignments[ranked[0][1]].append(clause)
                    declared_ids = list(dict.fromkeys(
                        unit.source_segment_ids
                    ))
                    if not all(assignments.get(source_id) for source_id in declared_ids):
                        normalized_units.append(unit)
                        continue
                    split_units: list[IRSceneUnit] = []
                    dialogue_excerpts: dict[str, str] = {}
                    if unit.kind == "dialogue":
                        for source_id in declared_ids:
                            aligned = align_source_excerpt(
                                unit.source_text or unit.text,
                                segments[source_id].text,
                                min_match_chars=2,
                            )
                            if aligned is None:
                                dialogue_excerpts = {}
                                break
                            dialogue_excerpts[source_id] = aligned.excerpt
                        if len(dialogue_excerpts) != len(declared_ids):
                            normalized_units.append(unit)
                            continue
                    for part_no, source_id in enumerate(
                        sorted(declared_ids, key=source_order.__getitem__),
                        start=1,
                    ):
                        split_unit = unit.model_copy(deep=True)
                        split_unit.event_key = (
                            f"{unit.event_key}-source-part-{part_no}"
                        )
                        split_unit.source_segment_ids = [source_id]
                        split_unit.text = "".join(assignments[source_id])
                        if unit.kind == "dialogue":
                            split_unit.source_text = dialogue_excerpts[source_id]
                            if part_no > 1 and split_unit.function == "response":
                                split_unit.function = "statement"
                        split_units.append(split_unit)
                    normalized_units.extend(split_units)
                    compiler_audit.append({
                        "path": f"scenes.{scene.key}.units.{unit.event_key}",
                        "operation": "split_discontinuous_source_unit",
                        "from": unit.source_segment_ids,
                        "to": [
                            split_unit.source_segment_ids
                            for split_unit in split_units
                        ],
                        "reason": (
                            "restore_intervening_source_playback_order"
                        ),
                    })
                scene.units = sorted(
                    normalized_units,
                    key=lambda item: min(
                        (
                            source_order[source_id]
                            for source_id in item.source_segment_ids
                            if source_id in source_order
                        ),
                        default=len(segments_list),
                    ),
                )
            flat_units = [
                (scene, unit)
                for scene in value.scenes
                for unit in scene.units
            ]
            unknown_source_ids = {
                source_id
                for _scene, unit in flat_units
                for source_id in unit.source_segment_ids
                if source_id not in all_source_ids
            }
            if unknown_source_ids:
                raise ScreenplayIRFidelityError(
                    "IR units 引用了不存在的细粒度来源段："
                    + "、".join(sorted(unknown_source_ids)[:20])
                )
            if value.source_scene_owners:
                ownership_payload = {
                    "source_scene_owners": value.source_scene_owners,
                        "source_semantics": value.source_semantics,
                    "scene_derivations": value.scene_derivations,
                }
                actual_ownership_hash = hashlib.sha256(
                    json.dumps(
                        ownership_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if (
                    value.source_ownership_hash
                    and value.source_ownership_hash
                    != actual_ownership_hash
                ):
                    raise ScreenplayIRFidelityError(
                        "IR source_ownership_hash 与结构化 owner 合同不一致"
                    )
                missing_owner_contract = (
                    expected_source_ids - set(value.source_scene_owners)
                )
                if missing_owner_contract:
                    raise ScreenplayIRFidelityError(
                        "IR source owner 合同漏掉来源段："
                        + "、".join(sorted(missing_owner_contract)[:20])
                    )
                owner_conflicts = [
                    (
                        source_id,
                        value.source_scene_owners.get(source_id),
                        scene.key,
                    )
                    for scene, unit in flat_units
                    for source_id in unit.source_segment_ids
                    if value.source_scene_owners.get(source_id) != scene.key
                ]
                if owner_conflicts:
                    source_id, expected_owner, consumer = owner_conflicts[0]
                    raise ScreenplayIRFidelityError(
                        f"IR 来源唯一归属冲突：{source_id} owner="
                        f"{expected_owner or '未定义'}，consumer={consumer}"
                    )
            units_without_source = [
                f"{scene.key}:{unit.event_key or index}"
                for index, (scene, unit) in enumerate(flat_units, start=1)
                if not unit.source_segment_ids
            ]
            if units_without_source:
                raise ScreenplayIRFidelityError(
                    "IR v1.3 每个 unit 必须声明 source_segment_ids："
                    + "、".join(units_without_source[:20])
                )
            owned_source_ids = {
                source_id
                for _scene, unit in flat_units
                for source_id in unit.source_segment_ids
            }
            missing_source_ids = expected_source_ids - owned_source_ids
            if missing_source_ids:
                raise ScreenplayIRFidelityError(
                    "IR v1.3 正文 units 漏掉细粒度来源段："
                    + "、".join(sorted(missing_source_ids)[:20])
                    + (
                        f"（另有 {len(missing_source_ids) - 20} 段）"
                        if len(missing_source_ids) > 20 else ""
                    )
                )
            source_owner_counts: defaultdict[str, int] = defaultdict(int)
            for _scene, unit in flat_units:
                for source_id in set(unit.source_segment_ids):
                    source_owner_counts[source_id] += 1
            for scene, unit in flat_units:
                source_ids = list(dict.fromkeys(unit.source_segment_ids))
                source_positions = [
                    source_order[source_id] for source_id in source_ids
                ]
                if source_positions == sorted(source_positions):
                    contiguous_runs: list[list[str]] = []
                    for source_id in source_ids:
                        if (
                            not contiguous_runs
                            or source_order[source_id]
                            != (
                                source_order[contiguous_runs[-1][-1]] + 1
                            )
                        ):
                            contiguous_runs.append([source_id])
                        else:
                            contiguous_runs[-1].append(source_id)
                    viable_runs = [
                        run
                        for run in contiguous_runs
                        if all(
                            (
                                source_id in run
                                or source_owner_counts[source_id] > 1
                            )
                            for source_id in source_ids
                        )
                    ]
                    if len(contiguous_runs) > 1:
                        candidate_runs = viable_runs or contiguous_runs
                        retained_run = max(
                            candidate_runs,
                            key=lambda run: (
                                int(
                                    unit.kind == "dialogue"
                                    and bool(unit.source_text.strip())
                                    and unit.source_text.strip() in "\n".join(
                                        segments[source_id].text
                                        for source_id in run
                                    )
                                ),
                                sum(
                                    source_owner_counts[source_id] == 1
                                    for source_id in run
                                ),
                                len(run),
                                max(
                                    (
                                        textmatch.bigram_coverage(
                                            unit.text,
                                            segments[source_id].text,
                                        )
                                        for source_id in run
                                    ),
                                    default=0.0,
                                ),
                            ),
                        )
                        unit.source_segment_ids = retained_run
                        compiler_audit.append({
                            "path": (
                                f"scenes.{scene.key}.units."
                                f"{unit.event_key}.source_segment_ids"
                            ),
                            "operation": (
                                "drop_redundant_noncontiguous_ownership"
                            ),
                            "from": source_ids,
                            "to": retained_run,
                            "reason": (
                                "retain_single_best_matching_contiguous_source_run"
                            ),
                        })
                        source_ids = retained_run
                if (
                    len(source_ids) <= IR_MAX_SOURCE_SEGMENTS_PER_UNIT
                    or not all(
                        source_owner_counts[source_id] > 1
                        for source_id in source_ids
                    )
                ):
                    continue
                scored_source_ids = sorted(
                    source_ids,
                    key=lambda source_id: max(
                        textmatch.longest_run_ratio(
                            unit.text,
                            segments[source_id].text,
                        ),
                        textmatch.bigram_coverage(
                            unit.text,
                            segments[source_id].text,
                        ),
                    ),
                    reverse=True,
                )
                anchor_index = source_ids.index(scored_source_ids[0])
                window_start = min(
                    max(0, anchor_index - 1),
                    max(0, len(source_ids) - 4),
                )
                unit.source_segment_ids = source_ids[
                    window_start:window_start + 4
                ]
                compiler_audit.append({
                    "path": (
                        f"scenes.{scene.key}.units.{unit.event_key}."
                        "source_segment_ids"
                    ),
                    "operation": "narrow_redundant_source_ownership",
                    "from_count": len(source_ids),
                    "to_count": len(unit.source_segment_ids),
                    "reason": (
                        "detailed_units_already_own_every_source_segment"
                    ),
                })
            normalized_owned_source_ids = {
                source_id
                for _scene, unit in flat_units
                for source_id in unit.source_segment_ids
            }
            normalized_missing_source_ids = (
                expected_source_ids - normalized_owned_source_ids
            )
            if normalized_missing_source_ids:
                raise ScreenplayIRFidelityError(
                    "IR v1.3 正文 units 漏掉细粒度来源段："
                    + "、".join(sorted(normalized_missing_source_ids)[:20])
                    + (
                        f"（另有 {len(normalized_missing_source_ids) - 20} 段）"
                        if len(normalized_missing_source_ids) > 20 else ""
                    )
                )
            first_owner_position: dict[str, int] = {}
            for unit_position, (_scene, unit) in enumerate(flat_units):
                unit.source_segment_ids = list(dict.fromkeys(
                    unit.source_segment_ids
                ))
                unit_positions = [
                    source_order[source_id]
                    for source_id in unit.source_segment_ids
                ]
                if unit_positions != sorted(unit_positions):
                    raise ValueError(
                        "IR v1.3 unit 内 source_segment_ids 必须按原文顺序："
                        f"{unit.event_key}"
                    )
                if len(unit_positions) > IR_MAX_SOURCE_SEGMENTS_PER_UNIT:
                    raise ValueError(
                        "IR v1.3 单个 unit 合并来源段过多："
                        f"{unit.event_key}={len(unit_positions)}"
                    )
                if unit_positions and (
                    unit_positions[-1] - unit_positions[0] + 1
                    != len(unit_positions)
                ):
                    raise ValueError(
                        "IR v1.3 单个 unit 只能合并连续来源段："
                        f"{unit.event_key}"
                    )
                if unit.kind == "dialogue" and unit.source_text.strip():
                    declared_source = "\n".join(
                        segments[source_id].text
                        for source_id in unit.source_segment_ids
                    )
                    if unit.source_text.strip() not in declared_source:
                        aligned = align_source_excerpt(
                            unit.source_text,
                            declared_source,
                            min_match_chars=2,
                        )
                        if aligned is None:
                            scene_source_ids = {
                                source_id
                                    for scene_unit in _scene.units
                                for source_id
                                in scene_unit.source_segment_ids
                            }
                            exact_matches = [
                                source_id
                                for source_id, segment in segments.items()
                                if (
                                    unit.source_text.strip()
                                    in segment.text
                                    and source_id in scene_source_ids
                                )
                            ]
                            global_exact_matches = [
                                source_id
                                for source_id, segment in segments.items()
                                if unit.source_text.strip() in segment.text
                            ]
                            selected_exact_matches = (
                                exact_matches
                                if len(exact_matches) == 1
                                else global_exact_matches
                            )
                            if (
                                len(selected_exact_matches) != 1
                                or not all(
                                    source_owner_counts[source_id] > 1
                                    for source_id
                                    in unit.source_segment_ids
                                )
                            ):
                                raise ValueError(
                                    "IR v1.3 对白 source_text 不属于声明的来源段："
                                    f"{unit.event_key}"
                                )
                            before_source_ids = list(
                                unit.source_segment_ids
                            )
                            unit.source_segment_ids = selected_exact_matches
                            compiler_audit.append({
                                "path": (
                                    f"scenes.units.{unit.event_key}."
                                    "source_segment_ids"
                                ),
                                "operation": (
                                    "rebind_dialogue_exact_source"
                                ),
                                "from": before_source_ids,
                                "to": selected_exact_matches,
                                "reason": (
                                    "verbatim_dialogue_uniquely_owned_by_"
                                    "another_source"
                                ),
                            })
                        else:
                            compiler_audit.append({
                                "path": (
                                    f"scenes.units.{unit.event_key}.source_text"
                                ),
                                "operation": "align_within_declared_source",
                                "from": unit.source_text,
                                "to": aligned.excerpt,
                                "reason": (
                                    "citation_joined_discontinuous_source_phrases"
                                ),
                            })
                            unit.source_text = aligned.excerpt
                for source_id in unit.source_segment_ids:
                    first_owner_position.setdefault(
                        source_id, unit_position,
                    )
            ownership_positions = [
                first_owner_position[segment.segment_id]
                for segment in dramatic_segments
            ]
            if ownership_positions != sorted(ownership_positions):
                raise ValueError(
                    "IR v1.3 来源段首次进入正文的顺序与原文不一致"
                )
            source_chars = sum(
                len(textmatch.condense(segment.text))
                for segment in dramatic_segments
            )
            adapted_chars = sum(
                len(textmatch.condense(unit.text))
                for _scene, unit in flat_units
            )
            adapted_ratio = adapted_chars / max(source_chars, 1)
            if (
                source_chars >= 200
                and adapted_ratio < IR_MIN_ADAPTED_SOURCE_RATIO
            ):
                raise ScreenplayIRFidelityError(
                    "IR v1.3 正文过度压缩："
                    f"改编净文本/原文={adapted_ratio:.1%}，"
                    f"最低要求={IR_MIN_ADAPTED_SOURCE_RATIO:.0%}"
                )
            weak_windows: list[str] = []
            for start in range(0, len(dramatic_segments), IR_LOCAL_SOURCE_WINDOW):
                window = dramatic_segments[
                    start:start + IR_LOCAL_SOURCE_WINDOW
                ]
                window_ids = {segment.segment_id for segment in window}
                window_source_chars = sum(
                    len(textmatch.condense(segment.text))
                    for segment in window
                )
                owner_units = [
                    unit
                    for _scene, unit in flat_units
                    if window_ids.intersection(unit.source_segment_ids)
                ]
                window_adapted_chars = sum(
                    len(textmatch.condense(unit.text))
                    for unit in owner_units
                )
                window_ratio = (
                    window_adapted_chars / max(window_source_chars, 1)
                )
                if (
                    window_source_chars >= 300
                    and window_ratio < IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO
                ):
                    weak_windows.append(
                        f"{window[0].segment_id}-{window[-1].segment_id}"
                        f"={window_ratio:.1%}"
                    )
            if weak_windows:
                raise ScreenplayIRFidelityError(
                    "IR v1.3 存在局部剧情过度压缩："
                    + "、".join(weak_windows[:10])
                )
            compiler_audit.append({
                "path": "scenes.units",
                "operation": "verify_source_fidelity",
                "source_segment_count": len(segments_list),
                "adapted_source_ratio": round(adapted_ratio, 4),
                "local_window_size": IR_LOCAL_SOURCE_WINDOW,
                "reason": "prevent_declared_coverage_without_dramatization",
            })

        def segment_index_for_offset(offset: int) -> int | None:
            return next(
                (
                    index
                    for index, segment in enumerate(segments_list)
                    if segment.start_offset <= offset < segment.end_offset
                ),
                None,
            )

        assigned_indices: list[int] = []
        if strict_unit_ownership:
            source_index = {
                segment.segment_id: index
                for index, segment in enumerate(segments_list)
            }
            assigned_indices = [
                source_index[unit.source_segment_ids[0]]
                for _scene, unit in flat_units
            ]
        else:
            anchored_indices: list[int | None] = []
            for _scene, unit in flat_units:
                candidate = (
                    unit.source_text
                    if unit.kind == "dialogue" and unit.source_text.strip()
                    else unit.text
                )
                exact_offset = source_text.find(candidate) if candidate else -1
                aligned = (
                    align_source_excerpt(
                        candidate,
                        source_text,
                        min_match_chars=4,
                    )
                    if exact_offset < 0 and candidate
                    else None
                )
                offset = (
                    exact_offset
                    if exact_offset >= 0
                    else aligned.start_offset if aligned is not None else -1
                )
                anchored_indices.append(
                    segment_index_for_offset(offset)
                    if offset >= 0 else None
                )

            previous_index = 0
            for unit_index, (_scene, unit) in enumerate(flat_units):
                anchored = anchored_indices[unit_index]
                if anchored is not None:
                    selected_index = max(previous_index, anchored)
                else:
                    next_anchor = next(
                        (
                            candidate
                            for candidate in anchored_indices[unit_index + 1:]
                            if (
                                candidate is not None
                                and candidate >= previous_index
                            )
                        ),
                        len(segments_list) - 1,
                    )
                    candidates = range(
                        previous_index,
                        max(previous_index, next_anchor) + 1,
                    )
                    selected_index = max(
                        candidates,
                        key=lambda index: (
                            max(
                                textmatch.longest_run_ratio(
                                    unit.text,
                                    segments_list[index].text,
                                ),
                                textmatch.bigram_coverage(
                                    unit.text,
                                    segments_list[index].text,
                                ),
                            ),
                            -abs(index - previous_index),
                        ),
                    )
                assigned_indices.append(selected_index)
                previous_index = selected_index

        identity_aliases: dict[str, set[str]] = {
            identity.key: {
                token
                for token in (
                    identity.key,
                    identity.display_name,
                )
                if token
            }
            for identity in value.identities
        }
        for resolution in episode.get("character_resolutions") or []:
            if not isinstance(resolution, dict):
                continue
            aliases = {
                str(resolution.get(field) or "").strip()
                for field in (
                    "source_label",
                    "canonical_name",
                    "functional_identity_key",
                )
                if str(resolution.get(field) or "").strip()
            }
            matching_keys = [
                key
                for key, tokens in identity_aliases.items()
                if aliases.intersection(tokens)
            ]
            if len(matching_keys) == 1:
                identity_aliases[matching_keys[0]].update(aliases)
        identity_display = {
            identity.key: identity.display_name
            for identity in value.identities
        }
        normalized_event_keys: dict[tuple[str, int], str] = {}
        explicit_actor_keys_by_event: defaultdict[tuple[str, str], list[str]] = (
            defaultdict(list)
        )
        explicit_target_keys_by_event: defaultdict[tuple[str, str], list[str]] = (
            defaultdict(list)
        )
        onscreen_keys_by_event: defaultdict[tuple[str, str], list[str]] = (
            defaultdict(list)
        )
        event_scene_owners: dict[str, str] = {}
        for unit_index, (scene, unit) in enumerate(flat_units, start=1):
            event_key = unit.event_key.strip() or f"derived-event-{unit_index}"
            unit.event_key = event_key
            previous_scene_key = event_scene_owners.setdefault(
                event_key,
                scene.key,
            )
            if previous_scene_key != scene.key:
                raise ValueError(
                    "IR scenes.units event_key 必须在本集唯一；"
                    f"{event_key} 同时出现在 {previous_scene_key} 与 {scene.key}"
                )
            normalized_event_keys[(scene.key, unit_index)] = event_key
            if (
                typed_visual_unit_contract
                and "onscreen_entity_keys" not in unit.model_fields_set
            ):
                raise ScreenplayIRFidelityError(
                    f"IR {format_version} {scene.key}.{event_key} 缺少显式 "
                    "onscreen_entity_keys，禁止从姓名词面推断在场关系"
                )
            if (
                typed_visual_unit_contract
                and unit.kind == "action"
                and "actor_keys" not in unit.model_fields_set
            ):
                raise ScreenplayIRFidelityError(
                    f"IR {format_version} {scene.key}.{event_key} 动作单元缺少显式 "
                    "actor_keys，禁止从动作文本猜测执行者"
                )
            if typed_visual_unit_contract and (
                "state_subject_key" not in unit.model_fields_set
                or "environment_only" not in unit.model_fields_set
            ):
                raise ScreenplayIRFidelityError(
                    f"IR {format_version} {scene.key}.{event_key} 缺少显式 "
                    "state_subject_key/environment_only "
                    "状态归属合同，"
                    "旧 IR 必须重建"
                )
            if typed_visual_unit_contract:
                subject_key = unit.state_subject_key.strip()
                subject_keys = list(dict.fromkeys(unit.state_subject_keys))
                if unit.kind == "dialogue":
                    if unit.environment_only:
                        raise ScreenplayIRFidelityError(
                            f"IR {format_version} {scene.key}.{event_key} 对白单元"
                            "不得声明 environment_only"
                        )
                    if (
                        not unit.speaker_key
                        or subject_keys != [unit.speaker_key]
                        or subject_key != unit.speaker_key
                    ):
                        raise ScreenplayIRFidelityError(
                            f"IR {format_version} {scene.key}.{event_key} 对白单元"
                            "state_subject_key 必须等于唯一 speaker_key"
                        )
                elif unit.environment_only:
                    if subject_keys or unit.actor_keys:
                        raise ScreenplayIRFidelityError(
                            f"IR {format_version} {scene.key}.{event_key} 纯环境单元"
                            "不得同时声明人物 state subject/actor"
                        )
                elif (
                    not subject_keys
                    or any(key not in unit.actor_keys for key in subject_keys)
                    or (
                        len(subject_keys) == 1
                        and subject_key != subject_keys[0]
                    )
                    or (len(subject_keys) > 1 and subject_key)
                ):
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} {scene.key}.{event_key} 动作单元"
                        "必须由 exact-unit typed actor 承载 single/joint "
                        "state_subject_keys，不得从 visible/roster 猜测"
                    )
            # Name occurrences remain a compatibility fallback for untyped IR.
            # Current contracts carry actor/target/on-screen relations as
            # frozen identity keys and never infer them from story words.
            visual_text = (
                f"{unit.text}\n{unit.resulting_state}"
                if unit.kind == "action" and not typed_visual_unit_contract
                else ""
            )
            mentioned = [
                key
                for key, aliases in identity_aliases.items()
                if any(alias and alias in visual_text for alias in aliases)
            ]
            explicit = list(dict.fromkeys([
                *(
                    [unit.speaker_key]
                    if unit.speaker_key
                    else []
                ),
                *(
                    unit.actor_keys
                    if unit.kind == "action" and typed_visual_unit_contract
                    else mentioned if unit.kind == "action" else []
                ),
            ]))
            targets = list(dict.fromkeys(unit.target_keys))
            onscreen = list(dict.fromkeys(
                unit.onscreen_entity_keys
                if typed_visual_unit_contract
                else unit.onscreen_entity_keys or explicit
            ))
            if typed_visual_unit_contract:
                relation_keys = {*explicit, *targets}
                delivery_keys: set[str] = set()
                delivery_errors: list[str] = []
                for delivery in unit.participant_deliveries:
                    participant_key = delivery.participant_key.strip()
                    if participant_key in delivery_keys:
                        delivery_errors.append(
                            f"{participant_key} 存在重复参与者交付合同"
                        )
                        continue
                    delivery_keys.add(participant_key)
                    if participant_key not in relation_keys:
                        delivery_errors.append(
                            f"{participant_key} 不是本 unit 的 actor/target/speaker"
                        )
                    if participant_key in onscreen:
                        delivery_errors.append(
                            f"{participant_key} 已入画，不得声明为画外参与者交付"
                        )
                    if not delivery.observable_claim.strip():
                        delivery_errors.append(
                            f"{participant_key} 缺少可感知 evidence claim"
                        )
                    if not delivery.is_perceivable:
                        delivery_errors.append(
                            f"{participant_key} 未声明可听、可见影响或可见反应"
                        )
                missing_deliveries = (
                    relation_keys - set(onscreen) - delivery_keys
                )
                if missing_deliveries:
                    delivery_errors.append(
                        "未入画参与者缺少结构化交付合同："
                        f"{sorted(missing_deliveries)}"
                    )
                if delivery_errors:
                    raise ScreenplayIRFidelityError(
                        f"IR v1.5 {scene.key}.{event_key} 动作参与者交付失败："
                        + "；".join(delivery_errors)
                    )
            event_actors = explicit_actor_keys_by_event[(scene.key, event_key)]
            event_actors.extend(
                key for key in explicit
                if key not in event_actors
            )
            event_targets = explicit_target_keys_by_event[(scene.key, event_key)]
            event_targets.extend(
                key for key in targets
                if key not in event_targets
            )
            event_onscreen = onscreen_keys_by_event[(scene.key, event_key)]
            event_onscreen.extend(
                key for key in onscreen
                if key not in event_onscreen
            )
        def merge_participant_deliveries(
            existing: list[IRActionParticipantDelivery],
            additions: list[IRActionParticipantDelivery],
        ) -> list[IRActionParticipantDelivery]:
            merged = [item.model_copy(deep=True) for item in existing]
            by_participant = {
                item.participant_key: item
                for item in merged
            }
            for addition in additions:
                current = by_participant.get(addition.participant_key)
                if current is None:
                    current = addition.model_copy(deep=True)
                    merged.append(current)
                    by_participant[current.participant_key] = current
                    continue
                current.audible = current.audible or addition.audible
                current.visible_effect = (
                    current.visible_effect or addition.visible_effect
                )
                current.visible_reaction = (
                    current.visible_reaction or addition.visible_reaction
                )
                current.observable_claim = "；".join(dict.fromkeys(filter(
                    None,
                    [
                        current.observable_claim.strip(),
                        addition.observable_claim.strip(),
                    ],
                )))
            return merged

        derived_by_key: dict[str, IREvent] = {}
        for unit_index, ((scene, unit), segment_index) in enumerate(
            zip(flat_units, assigned_indices, strict=True),
            start=1,
        ):
            event_key = normalized_event_keys[(scene.key, unit_index)]
            actor_keys = list(
                explicit_actor_keys_by_event[(scene.key, event_key)]
            )
            target_keys = list(
                explicit_target_keys_by_event[(scene.key, event_key)]
            )
            onscreen_entity_keys = list(
                onscreen_keys_by_event[(scene.key, event_key)]
            )
            contextual_actor = False
            if (
                not actor_keys
                and unit.kind == "action"
                and not typed_visual_unit_contract
            ):
                contextual_key = f"context_actor_{scene.key}"
                contextual_actor = True
                actor_keys = [contextual_key]
                if contextual_key not in identity_aliases:
                    display = (
                        f"{scene.scene_heading}中的未具名参与者"
                    )
                    value.identities.append(IRIdentity(
                        key=contextual_key,
                        authority_id=_structural_context_authority_id(
                            episode,
                            contextual_key,
                        ),
                        display_name=display,
                        kind="source_backed_scene_context_actor",
                        visual_policy="collective",
                        visual_canonical=(
                            f"仅按本场动作「{unit.text[:40]}」表现的未具名参与者"
                        ),
                        asset_requirement="optional",
                        voice_canonical="符合本场来源动作的环境或群体声线",
                        role_type="functional_character",
                        rationale=(
                            "动作单元未指向已登记人物，按当前场次和来源动作建立"
                            "局部 contextual actor，不跨场复用"
                        ),
                    ))
                    identity_aliases[contextual_key] = {
                        contextual_key,
                        display,
                    }
                    identity_display[contextual_key] = display
                if not onscreen_entity_keys:
                    onscreen_entity_keys = [contextual_key]
            source_ids = (
                list(dict.fromkeys(unit.source_segment_ids))
                if strict_unit_ownership
                else [segments_list[segment_index].segment_id]
            )
            if unit.kind == "dialogue":
                speaker = identity_display.get(
                    unit.speaker_key or "",
                    unit.speaker_key or "当前说话人",
                )
                adapted_statement = f"{speaker}说出对白「{unit.text.strip()}」"
            else:
                adapted_statement = unit.text.strip()
                if len(re.sub(r"\s+", "", adapted_statement)) < 8:
                    adapted_statement = (
                        f"{scene.summary or scene.story_function}中的动作："
                        f"{adapted_statement}"
                    )
            existing = derived_by_key.get(event_key)
            if existing is not None:
                semantic_contract = (
                    unit.narrative_layer,
                    unit.event_priority,
                    unit.render_policy,
                )
                if semantic_contract != (
                    existing.narrative_layer,
                    existing.event_priority,
                    existing.render_policy,
                ):
                    raise ValueError(
                        "同一 event_key 的语义优先级合同不一致："
                        f"{event_key}"
                    )
                if (
                    existing.state_subject_key != unit.state_subject_key
                    or existing.state_subject_keys != unit.state_subject_keys
                    or existing.environment_only != unit.environment_only
                ):
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} {scene.key}.{event_key} 同一事件"
                        "包含不一致的 state subject/environment 声明"
                    )
                existing.source_segment_ids = list(dict.fromkeys([
                    *existing.source_segment_ids,
                    *source_ids,
                ]))
                existing.actor_keys = list(dict.fromkeys([
                    *existing.actor_keys,
                    *actor_keys,
                ]))
                existing.target_keys = list(dict.fromkeys([
                    *existing.target_keys,
                    *target_keys,
                ]))
                existing.onscreen_entity_keys = list(dict.fromkeys([
                    *existing.onscreen_entity_keys,
                    *onscreen_entity_keys,
                ]))
                existing.participant_deliveries = merge_participant_deliveries(
                    existing.participant_deliveries,
                    unit.participant_deliveries,
                )
                agency_kinds = list(dict.fromkeys([
                    existing.action_agency.kind,
                    unit.action_agency.kind,
                ]))
                existing.action_agency = ActionAgency(
                    kind=(
                        agency_kinds[0]
                        if len(agency_kinds) == 1
                        else "mixed"
                    ),
                    identity_bearing=bool(
                        existing.actor_keys or existing.target_keys
                    ),
                    source_segment_ids=list(existing.source_segment_ids),
                )
                existing.text_provenance = TextProvenance(
                    kind=existing.text_provenance.kind,
                    identity_keys=(
                        []
                        if existing.text_provenance.kind in (
                            "required_text",
                            "prop_text",
                            "on_screen_text",
                        )
                        else list(dict.fromkeys([
                            *existing.actor_keys,
                            *existing.target_keys,
                        ]))
                    ),
                    content_owner_keys=list(dict.fromkeys([
                        *existing.text_provenance.content_owner_keys,
                        *unit.text_provenance.content_owner_keys,
                    ])),
                    source_segment_ids=list(existing.source_segment_ids),
                )
                if not screenplay_beat_fields_repeat(
                    existing.adapted_statement,
                    adapted_statement,
                ):
                    existing.adapted_statement = (
                        f"{existing.adapted_statement}；{adapted_statement}"
                    )
                if unit.resulting_state.strip():
                    existing.resulting_state = unit.resulting_state.strip()
                continue
            derived_by_key[event_key] = IREvent(
                key=event_key,
                scene_key=scene.key,
                narrative_layer=unit.narrative_layer,
                event_priority=unit.event_priority,
                render_policy=unit.render_policy,
                source_segment_ids=source_ids,
                adapted_statement=adapted_statement,
                resulting_state=unit.resulting_state.strip(),
                actor_keys=actor_keys,
                target_keys=target_keys,
                onscreen_entity_keys=onscreen_entity_keys,
                participant_deliveries=[
                    delivery.model_copy(deep=True)
                    for delivery in unit.participant_deliveries
                ],
                state_subject_key=unit.state_subject_key,
                state_subject_keys=list(unit.state_subject_keys),
                environment_only=unit.environment_only,
                action_agency=ActionAgency(
                    kind=unit.action_agency.kind,
                    identity_bearing=bool(actor_keys or target_keys),
                    source_segment_ids=source_ids,
                ),
                text_provenance=TextProvenance(
                    kind=unit.text_provenance.kind,
                    identity_keys=(
                        []
                        if unit.text_provenance.kind in (
                            "required_text",
                            "prop_text",
                            "on_screen_text",
                        )
                        else list(dict.fromkeys([
                            *actor_keys,
                            *target_keys,
                        ]))
                    ),
                    content_owner_keys=list(
                        unit.text_provenance.content_owner_keys
                    ),
                    source_segment_ids=source_ids,
                ),
                dialogue_text=(
                    unit.text.strip()
                    if unit.kind == "dialogue"
                    else ""
                ),
                required_text=unit.required_text,
                prop_text=unit.prop_text,
                on_screen_text=unit.on_screen_text,
                character_emotion="",
                decision_required=bool(actor_keys) and not contextual_actor,
                decision_reason=(
                    "来源动作没有人物 actor，不建立人物自主决策链"
                    if not actor_keys else ""
                ),
                must_keep=True,
            )
        value.events = list(derived_by_key.values())
        compiler_audit.append({
            "path": "events",
            "operation": "derive_from_scene_units",
            "count": len(value.events),
            "source_segment_count": len(segments_list),
            "reason": "scene_units_are_the_authored_playback_timeline",
        })
    if not value.events:
        raise ValueError("IR events 不能为空")

    excluded_event_keys: set[str] = set()
    excluded_source_ids: list[str] = []
    for event in value.events:
        if event.narrative_layer == "paratext":
            if event.render_policy != "exclude_from_spine":
                raise ValueError(
                    f"paratext 事件 {event.key} 必须 exclude_from_spine"
                )
            excluded_event_keys.add(event.key)
            excluded_source_ids.extend(event.source_segment_ids)
        elif event.render_policy == "exclude_from_spine":
            raise ValueError(
                f"story 事件 {event.key} 不得 exclude_from_spine"
            )
    if excluded_event_keys:
        excluded_ids = list(dict.fromkeys(excluded_source_ids))
        covered_as_audit = {
            source_id
            for group in value.coverage
            if group.disposition == "audit_only"
            for source_id in group.source_segment_ids
        }
        missing_audit_ids = [
            source_id
            for source_id in excluded_ids
            if source_id not in covered_as_audit
        ]
        if missing_audit_ids:
            value.coverage.append(IRCoverageGroup(
                source_segment_ids=missing_audit_ids,
                disposition="audit_only",
                projection_policy="audit_only",
                reason=(
                    "来源内容属于非剧情旁文本，保留来源审计，"
                    "不进入成片叙事 spine"
                ),
            ))
        value.events = [
            event for event in value.events
            if event.key not in excluded_event_keys
        ]
        value.beats = [
            beat for beat in value.beats
            if not set(beat.source_segment_ids).issubset(excluded_ids)
        ]
        retained_scenes: list[IRScene] = []
        for scene in value.scenes:
            scene.units = [
                unit for unit in scene.units
                if unit.event_key not in excluded_event_keys
            ]
            if scene.units:
                retained_scenes.append(scene)
        value.scenes = retained_scenes
        if not value.events or not value.scenes:
            raise ValueError("非剧情旁文本隔离后没有可成片剧情事件")
        metadata.must_keep_ending = (
            value.events[-1].resulting_state
            or value.events[-1].completion_condition
            or metadata.must_keep_ending
        )
        compiler_audit.append({
            "path": "events",
            "operation": "exclude_paratext_from_picture_spine",
            "event_keys": sorted(excluded_event_keys),
            "source_segment_ids": excluded_ids,
        })

    scene_by_key = _unique_by_key(value.scenes, "scenes")
    event_by_key = _unique_by_key(value.events, "events")
    identity_by_key = _unique_by_key(value.identities, "identities")
    for identity in identity_by_key.values():
        if not identity.source_names:
            source_name_match = re.search(
                r"原文(?:中的|中|称谓为?|称为)\s*[「“\"]?"
                r"([^，。；」”\"]+)",
                identity.rationale,
            )
            if source_name_match is not None:
                source_name = source_name_match.group(1).strip()
                if source_name in source_text:
                    identity.source_names = [source_name]
        source_names = list(dict.fromkeys(
            str(name).strip()
            for name in identity.source_names
            if str(name).strip() and str(name).strip() in source_text
        ))
        identity.source_names = source_names
        if source_names and identity.display_name != source_names[0]:
            compiler_audit.append({
                "path": f"identities.{identity.key}.display_name",
                "operation": "bind_source_canonical_name",
                "from": identity.display_name,
                "to": source_names[0],
                "reason": "identity_source_name_is_directly_authorized",
            })
            identity.display_name = source_names[0]
    _apply_authoritative_ir_identity_resolutions(
        value,
        episode=episode,
        bible=bible,
        audit=compiler_audit,
    )
    identity_by_key = _unique_by_key(value.identities, "identities")
    units_by_event: defaultdict[str, list[IRSceneUnit]] = defaultdict(list)
    for scene in value.scenes:
        for unit in scene.units:
            units_by_event[unit.event_key].append(unit)
    event_keys_by_scene: defaultdict[str, list[str]] = defaultdict(list)
    for event in value.events:
        event_keys_by_scene[event.scene_key].append(event.key)
    for index, event in enumerate(value.events):
        previous = value.events[index - 1] if index else None
        next_event = (
            value.events[index + 1]
            if index + 1 < len(value.events) else None
        )
        event_units = list(units_by_event.get(event.key, []))
        action_units = [
            unit.text.strip()
            for unit in event_units
            if unit.kind == "action" and unit.text.strip()
        ]
        derived_fields: list[str] = []
        if not event.adapted_statement.strip():
            event.adapted_statement = (
                "；".join(action_units)
                or event.resulting_state
                or event.action_intent
                or event.completion_condition
            )
            derived_fields.append("adapted_statement")
        if not event.precondition_state.strip():
            event.precondition_state = (
                previous.resulting_state
                if previous and previous.resulting_state.strip()
                else metadata.opening
                or "本集首个事件发生前的来源状态"
            )
            derived_fields.append("precondition_state")
        if not event.action_intent.strip():
            event.action_intent = event.adapted_statement
            derived_fields.append("action_intent")
        if not event.completion_condition.strip():
            event.completion_condition = (
                action_units[-1]
                if action_units else event.adapted_statement
            )
            derived_fields.append("completion_condition")
        if not event.resulting_state.strip():
            scene = scene_by_key[event.scene_key]
            scene_event_keys = event_keys_by_scene[event.scene_key]
            is_scene_terminal = (
                bool(scene_event_keys)
                and scene_event_keys[-1] == event.key
            )
            scene_outcome = next(
                (
                    candidate.strip()
                    for candidate in (
                        scene.exit_state,
                        scene.turn,
                    )
                    if (
                        candidate.strip()
                        and not screenplay_beat_fields_repeat(
                            event.action_intent,
                            candidate,
                        )
                    )
                ),
                "",
            )
            dialogue_units = [
                unit
                for unit in event_units
                if unit.kind == "dialogue"
            ]
            if is_scene_terminal and scene_outcome:
                event.resulting_state = scene_outcome
            elif dialogue_units:
                last_dialogue = dialogue_units[-1]
                speaker = identity_display.get(
                    last_dialogue.speaker_key or "",
                    last_dialogue.speaker_key or "当前说话人",
                )
                event.resulting_state = {
                    "question": (
                        f"{speaker}提出的问题成为下一话轮必须回应的焦点"
                    ),
                    "response": (
                        f"{speaker}完成回应，前一问题获得明确答复"
                    ),
                    "decision": (
                        f"{speaker}作出明确决定，后续行动条件已经成立"
                    ),
                    "announcement": (
                        f"{speaker}公布的信息成为在场者已知事实"
                    ),
                    "trigger": (
                        f"{speaker}的发言触发后续人物或局势反应"
                    ),
                    "statement": (
                        f"{speaker}表达的信息进入当前场景的共同认知"
                    ),
                }.get(
                    last_dialogue.function,
                    f"{speaker}完成本话轮信息交付",
                )
            elif (
                next_event is not None
                and next_event.scene_key == event.scene_key
            ):
                next_statement = (
                    next_event.action_intent
                    or next_event.adapted_statement
                    or next_event.completion_condition
                ).strip()
                event.resulting_state = (
                    "当前动作完成，局势推进到下一事件"
                    + (
                        f"「{next_statement[:80]}」发生前"
                        if next_statement else "的前置状态"
                    )
                )
            else:
                event.resulting_state = (
                    scene_outcome
                    or f"当前动作完成，本场「{scene.story_function}」进入下一阶段"
                )
            derived_fields.append("resulting_state")
        if not event.observable_claim.strip():
            event.observable_claim = (
                "；".join(action_units)
                or event.completion_condition
            )
            derived_fields.append("observable_claim")
        if not event.action_phases:
            phase_texts = action_units or [event.completion_condition]
            event.action_phases = [
                IRActionPhase(
                    start_condition=(
                        event.precondition_state
                        if phase_index == 0
                        else phase_texts[phase_index - 1]
                    ),
                    end_condition=phase_text,
                    estimated_min_s=1.0,
                    splittable_after=phase_index < len(phase_texts) - 1,
                )
                for phase_index, phase_text in enumerate(phase_texts)
            ]
            derived_fields.append("action_phases")
        if derived_fields:
            compiler_audit.append({
                "path": f"events.{event.key}",
                "operation": "derive_compact_event_fields",
                "fields": derived_fields,
                "reason": "ordered_events_and_scene_units_are_authoritative",
            })
        if screenplay_beat_fields_repeat(
            event.action_intent,
            event.resulting_state,
        ):
            raise ValueError(
                "IR 事件动作与结果状态语义重复："
                f"{event.key} does={event.action_intent!r} "
                f"turn={event.resulting_state!r}"
            )
    beats_were_derived = not value.beats
    if beats_were_derived:
        value.beats = [
            IRBeat(
                key=f"derived-beat-{index}",
                who="、".join(
                    identity_by_key[token].display_name
                    if token in identity_by_key else token
                    for token in event.actor_keys
                    if str(token).strip() != "audience"
                ) or "当前事件主体",
                does=(
                    event.action_intent
                    or event.observable_claim
                    or event.completion_condition
                ),
                turn=event.resulting_state,
                purpose=(
                    event.adapted_statement
                    or event.observable_claim
                    or event.completion_condition
                ),
                source_segment_ids=list(event.source_segment_ids),
                must_keep=event.must_keep,
            )
            for index, event in enumerate(value.events, start=1)
        ]
        compiler_audit.append({
            "path": "beats",
            "operation": "derive",
            "count": len(value.beats),
            "reason": "events_are_the_single_semantic_authority",
        })
    repeated_beats = [
        beat.key
        for beat in value.beats
        if screenplay_beat_fields_repeat(beat.does, beat.turn)
    ]
    if repeated_beats:
        raise ValueError(
            "IR 主线节拍 does 与 turn 语义重复，无法表达状态变化："
            + "、".join(repeated_beats[:20])
        )
    beat_by_key = _unique_by_key(value.beats, "beats")
    prior_values = list(value.audience_priors)
    if len(prior_values) < 2:
        prior_values = [
            *prior_values,
            *[
                IRAudiencePrior(
                    key=f"derived_prior_{index}",
                    description=description,
                    target_stance="suspected" if index == 2 else "believed",
                    target_confidence=0.65 if index == 2 else 0.8,
                )
                for index, description in (
                    (1, "不了解本集背景、仅凭当前画面进入的一次观看者"),
                    (2, "记得项目基础人物关系、但不知道本集结果的一次观看者"),
                )
                if len(prior_values) < index
            ],
        ]
        compiler_audit.append({
            "path": "audience_priors",
            "operation": "derive",
            "count": len(prior_values),
            "reason": "project_level_once_viewing_priors",
        })
    prior_by_key = _unique_by_key(prior_values, "audience_priors")

    expected_segment_ids = set(segments)
    beat_ids = {
        key: f"S{index:02d}"
        for index, key in enumerate(beat_by_key, start=1)
    }
    scene_ids = {
        key: f"SC{index:02d}"
        for index, key in enumerate(scene_by_key, start=1)
    }
    event_ids = {
        key: f"E{index}"
        for index, key in enumerate(event_by_key, start=1)
    }

    for beat in value.beats:
        unknown = set(beat.source_segment_ids) - expected_segment_ids
        if unknown:
            raise ValueError(f"beat {beat.key} 引用了不存在的来源段：{sorted(unknown)}")

    def segment_ordinal(segment_id: str) -> int:
        return int(re.sub(r"\D", "", segment_id) or 0)

    def nearest_event_for_segment(segment_id: str) -> IREvent:
        target = segment_ordinal(segment_id)
        return min(
            value.events,
            key=lambda event: min(
                (
                    abs(target - segment_ordinal(candidate))
                    for candidate in event.source_segment_ids
                ),
                default=10**9,
            ),
        )

    def beats_for_event(event: IREvent) -> list[IRBeat]:
        event_sources = set(event.source_segment_ids)
        overlaps = [
            (
                len(event_sources.intersection(beat.source_segment_ids)),
                beat,
            )
            for beat in value.beats
        ]
        best_overlap = max((score for score, _beat in overlaps), default=0)
        if best_overlap:
            return [
                beat for score, beat in overlaps if score == best_overlap
            ]
        target = min(
            (segment_ordinal(item) for item in event.source_segment_ids),
            default=0,
        )
        return [
            min(
                value.beats,
                key=lambda beat: min(
                    (
                        abs(target - segment_ordinal(candidate))
                        for candidate in beat.source_segment_ids
                    ),
                    default=10**9,
                ),
            )
        ]

    inferred_context_by_scene: defaultdict[str, list[str]] = defaultdict(list)

    def retain_as_scene_context(
        segment_id: str,
        *,
        reason: str = "",
    ) -> str:
        event = nearest_event_for_segment(segment_id)
        scene = scene_by_key[event.scene_key]
        excerpt = _first_sentence(segments[segment_id].text, minimum=4)
        context = (
            f"来源段 {segment_id} 作为本场人物、环境或因果上下文保留："
            f"{excerpt[:180]}"
        )
        if context not in inferred_context_by_scene[event.scene_key]:
            inferred_context_by_scene[event.scene_key].append(context)
        return reason.strip() or (
            f"作为「{scene.scene_heading}」的来源上下文保留，"
            "并写入该场 context_requirements"
        )

    seen_coverage: set[str] = set()
    coverage_rows: list[SourceCoverageDecision] = []
    for group in value.coverage:
        unknown_segments = set(group.source_segment_ids) - expected_segment_ids
        if unknown_segments:
            raise ValueError(f"coverage 引用了不存在的来源段：{sorted(unknown_segments)}")
        unknown_beats = set(group.beat_keys) - set(beat_by_key)
        if unknown_beats and beats_were_derived:
            previous_beat_keys = list(group.beat_keys)
            group.beat_keys = (
                []
                if group.disposition in {
                    "context", "duplicate", "audit_only",
                }
                else [
                    beat.key
                    for beat in value.beats
                    if set(beat.source_segment_ids).intersection(
                        group.source_segment_ids
                    )
                ]
            )
            compiler_audit.append({
                "path": "coverage.beat_keys",
                "operation": "rebind_legacy_reference",
                "from": previous_beat_keys,
                "to": group.beat_keys,
                "reason": "beats_are_compiler_derived",
            })
            unknown_beats = set(group.beat_keys) - set(beat_by_key)
        if unknown_beats:
            raise ValueError(f"coverage 引用了不存在的 beat：{sorted(unknown_beats)}")
        for segment_id in group.source_segment_ids:
            if segment_id in seen_coverage:
                raise ValueError(f"coverage 重复覆盖 {segment_id}")
            seen_coverage.add(segment_id)
            disposition = group.disposition
            owning_beat_keys = list(group.beat_keys)
            if disposition in {"deliver", "merge"} and not owning_beat_keys:
                owning_beat_keys = [
                    beat.key for beat in value.beats
                    if segment_id in beat.source_segment_ids
                ]
                if not owning_beat_keys:
                    disposition = "context"
                    group.projection_policy = "context_only"
            reason = group.reason
            if disposition == "context":
                reason = retain_as_scene_context(
                    segment_id,
                    reason=reason,
                )
            coverage_rows.append(SourceCoverageDecision(
                source_segment_id=segment_id,
                disposition=disposition,
                projection_policy=group.projection_policy,
                beat_ids=[beat_ids[key] for key in owning_beat_keys],
                duplicate_of=group.duplicate_of,
                reason=reason,
            ))
            if disposition != group.disposition or owning_beat_keys != group.beat_keys:
                compiler_audit.append({
                    "path": f"source_coverage.{segment_id}",
                    "operation": "normalize",
                    "from": {
                        "disposition": group.disposition,
                        "beat_keys": group.beat_keys,
                    },
                    "to": {
                        "disposition": disposition,
                        "beat_keys": owning_beat_keys,
                    },
                    "reason": "deterministic_coverage_link",
                })
    missing_coverage = expected_segment_ids - seen_coverage
    for segment_id in sorted(missing_coverage):
        owning_beats = [
            beat for beat in value.beats
            if segment_id in beat.source_segment_ids
        ]
        if owning_beats:
            coverage_rows.append(SourceCoverageDecision(
                source_segment_id=segment_id,
                disposition="merge" if len(owning_beats) > 1 else "deliver",
                projection_policy="picture",
                beat_ids=[beat_ids[beat.key] for beat in owning_beats],
                duplicate_of=None,
                reason="由已声明该来源段的主线节拍确定性补全覆盖回链",
            ))
            continue

        owning_events = [
            event for event in value.events
            if segment_id in event.source_segment_ids
        ]
        if owning_events:
            related_beats = list(dict.fromkeys(
                beat.key
                for event in owning_events
                for beat in beats_for_event(event)
            ))
            for beat_key in related_beats:
                beat = beat_by_key[beat_key]
                if segment_id not in beat.source_segment_ids:
                    beat.source_segment_ids.append(segment_id)
            coverage_rows.append(SourceCoverageDecision(
                source_segment_id=segment_id,
                disposition="merge",
                projection_policy="picture",
                beat_ids=[beat_ids[key] for key in related_beats],
                duplicate_of=None,
                reason="该来源段已进入事件语义，确定性合并到对应主线节拍",
            ))
            continue

        coverage_rows.append(SourceCoverageDecision(
            source_segment_id=segment_id,
            disposition="context",
            projection_policy="context_only",
            beat_ids=[],
            duplicate_of=None,
            reason=retain_as_scene_context(segment_id),
        ))
        compiler_audit.append({
            "path": f"source_coverage.{segment_id}",
            "operation": "derive_context",
            "reason": "source_segment_not_owned_by_event_or_beat",
        })
    coverage_rows.sort(
        key=lambda item: segment_ordinal(item.source_segment_id)
    )

    event_order = {event.key: index for index, event in enumerate(value.events)}
    for event in value.events:
        if event.scene_key not in scene_by_key:
            raise ValueError(f"event {event.key} 引用了不存在的 scene {event.scene_key}")
        referenced_identity_keys = {
            *event.actor_keys,
            *event.target_keys,
            *event.onscreen_entity_keys,
            *event.text_provenance.content_owner_keys,
            *(
                delivery.participant_key
                for delivery in event.participant_deliveries
            ),
            *(
                key
                for key in event.perceivable_by
                if key != "audience"
            ),
        }
        unknown_identity_keys = referenced_identity_keys - set(identity_by_key)
        if unknown_identity_keys:
            raise ValueError(
                f"event {event.key} 引用了不存在的 identity："
                f"{sorted(unknown_identity_keys)}"
            )
        invalid_onscreen_keys = [
            key
            for key in event.onscreen_entity_keys
            if identity_by_key[key].visual_policy == "offscreen_only"
        ]
        if invalid_onscreen_keys:
            raise ValueError(
                f"event {event.key}.onscreen_entity_keys 含仅允许画外的身份："
                f"{invalid_onscreen_keys}"
            )
        relation_keys = {*event.actor_keys, *event.target_keys}
        delivery_keys: set[str] = set()
        for delivery in event.participant_deliveries:
            participant_key = delivery.participant_key.strip()
            if participant_key in delivery_keys:
                raise ValueError(
                    f"event {event.key} 对 {participant_key} 重复声明参与者交付"
                )
            delivery_keys.add(participant_key)
            if participant_key not in relation_keys:
                raise ValueError(
                    f"event {event.key} 的参与者交付 {participant_key} "
                    "不属于 actor/target"
                )
            if participant_key in event.onscreen_entity_keys:
                raise ValueError(
                    f"event {event.key} 的参与者交付 {participant_key} 已入画"
                )
            if not delivery.observable_claim.strip() or not delivery.is_perceivable:
                raise ValueError(
                    f"event {event.key} 的参与者交付 {participant_key} "
                    "缺少结构化可感知证据"
                )
        if (
            typed_visual_unit_contract
            and "onscreen_entity_keys" in event.model_fields_set
        ):
            offscreen_relation_keys = (
                relation_keys - set(event.onscreen_entity_keys)
            )
        else:
            offscreen_relation_keys = {
                key
                for key in relation_keys
                if identity_by_key[key].visual_policy == "offscreen_only"
            }
        missing_deliveries = offscreen_relation_keys - delivery_keys
        if missing_deliveries:
            raise ValueError(
                f"event {event.key} 未入画 actor/target 缺少结构化参与者交付："
                f"{sorted(missing_deliveries)}"
            )
        unknown_sources = set(event.source_segment_ids) - expected_segment_ids
        if unknown_sources:
            raise ValueError(f"event {event.key} 来源段不存在：{sorted(unknown_sources)}")
        unknown_parents = set(event.causal_parent_keys) - set(event_by_key)
        if unknown_parents:
            raise ValueError(f"event {event.key} 原因事件不存在：{sorted(unknown_parents)}")
        future_parents = [
            key for key in event.causal_parent_keys
            if event_order[key] >= event_order[event.key]
        ]
        if future_parents:
            raise ValueError(
                f"event {event.key} 引用了未先发生的原因事件：{future_parents}"
            )
        derived_fields: list[str] = []
        if not event.adapted_statement.strip():
            event.adapted_statement = (
                event.observable_claim
                or event.completion_condition
                or event.resulting_state
                or event.action_intent
            )
            derived_fields.append("adapted_statement")
        if not event.observable_claim.strip():
            event.observable_claim = (
                event.completion_condition
                or event.resulting_state
                or event.action_intent
            )
            derived_fields.append("observable_claim")
        if not event.adaptation_reason.strip():
            event.adaptation_reason = (
                "按当前事件的来源段、动作意图和完成条件确定性建立改编关系"
            )
            derived_fields.append("adaptation_reason")
        if not event.perceivable_by:
            event.perceivable_by = list(dict.fromkeys([
                *event.actor_keys,
                *event.target_keys,
                "audience",
            ]))
            derived_fields.append("perceivable_by")
        if not event.onscreen_entity_keys:
            event.onscreen_entity_keys = list(dict.fromkeys(
                key
                for key in [*event.actor_keys, *event.target_keys]
                if identity_by_key[key].visual_policy != "offscreen_only"
            ))
            derived_fields.append("onscreen_entity_keys")
        if derived_fields:
            compiler_audit.append({
                "path": f"events.{event.key}",
                "operation": "derive_fields",
                "fields": derived_fields,
                "reason": "deterministic_event_projection",
            })

    bible_by_name = {item.name: item for item in bible.characters}
    # Identity keys are the structural reference contract.  A display name is
    # only a compatibility alias when it names exactly one identity.  Distinct
    # authority IDs may legitimately share the same source/display wording;
    # merely declaring them must not recreate a word-based identity conflict.
    identity_token_to_key: dict[str, str] = {
        key: key for key in identity_by_key
    }
    display_token_candidates: defaultdict[str, set[str]] = defaultdict(set)
    for key, identity in identity_by_key.items():
        display_name = str(identity.display_name or "").strip()
        if display_name:
            display_token_candidates[display_name].add(key)
    ambiguous_display_tokens: dict[str, list[str]] = {}
    for token, keys in display_token_candidates.items():
        if token in identity_by_key:
            continue
        if len(keys) == 1:
            identity_token_to_key[token] = next(iter(keys))
        else:
            ambiguous_display_tokens[token] = sorted(keys)
    for name, character in bible_by_name.items():
        if name in identity_token_to_key:
            continue
        identity_by_key[name] = IRIdentity(
            key=name,
            authority_id=f"bible:{name}",
            display_name=name,
            kind="bible_character",
            visual_policy="canonical",
            visual_canonical=character.appearance_canonical,
            asset_requirement="required",
            voice_canonical=character.speech_style or character.personality,
            role_type="named_character",
            rationale="角色圣经已登记的本集人物",
        )
        identity_token_to_key[name] = name

    def identity_key(token: str) -> str:
        raw = str(token or "").strip()
        key = identity_token_to_key.get(raw)
        if key:
            return key
        if raw in ambiguous_display_tokens:
            raise ScreenplayIRIdentityConflictError(
                f"IR 身份引用未使用唯一 identity_key：{raw}",
                issues=[{
                    "identity_key": "",
                    "reason": "ambiguous_identity_reference",
                    "display_name": raw,
                    "identity_keys": ambiguous_display_tokens[raw],
                }],
            )
        if not raw:
            raise ValueError("IR 引用了空身份")
        if raw == "audience":
            raise ValueError("audience 是观众感知主体，不是剧中身份")
        identity_by_key[raw] = IRIdentity(
            key=raw,
            authority_id=_structural_context_authority_id(episode, raw),
            display_name=raw,
            kind="event_referenced_contextual_identity",
            visual_policy="contextual",
            visual_canonical=f"当前事件中可由场次和动作关系识别的{raw}",
            asset_requirement="optional",
            voice_canonical=f"符合{raw}当前戏剧职责的稳定普通话声线",
            role_type="functional_character",
            rationale="该身份被当前 IR 的场次、动作、作用对象或声音关系实际引用",
        )
        identity_token_to_key[raw] = raw
        compiler_audit.append({
            "path": f"identities.{raw}",
            "operation": "derive_contextual_identity",
            "reason": "identity_is_referenced_by_event_or_scene",
        })
        return raw

    def resolve_unit_event_key(scene: IRScene, unit: IRSceneUnit) -> str:
        if unit.event_key in event_by_key:
            return unit.event_key
        candidates = [
            event for event in value.events
            if event.scene_key == scene.key
        ]
        if not candidates:
            raise ValueError(
                f"scene {scene.key} unit 引用了不存在的 event "
                f"{unit.event_key}，且本场没有可归属事件"
            )
        unit_number = int(
            re.sub(r"\D", "", str(unit.event_key or "")) or 0
        )

        def rank(event: IREvent) -> tuple[float, float, int]:
            semantic_text = " ".join(filter(None, (
                event.source_statement,
                event.adapted_statement,
                event.action_intent,
                event.completion_condition,
                event.observable_claim,
            )))
            similarity = max(
                textmatch.longest_run_ratio(unit.text, semantic_text),
                textmatch.bigram_coverage(unit.text, semantic_text),
            )
            candidate_number = int(
                re.sub(r"\D", "", str(event.key or "")) or 0
            )
            return similarity, -abs(unit_number - candidate_number), -event_order[event.key]

        selected = max(candidates, key=rank)
        compiler_audit.append({
            "path": f"scenes.{scene.key}.units.event_key",
            "operation": "repair_reference",
            "from": unit.event_key,
            "to": selected.key,
            "reason": "same_scene_semantic_and_ordinal_match",
        })
        unit.event_key = selected.key
        return selected.key

    used_identity_keys: set[str] = set()
    event_speaker_keys: defaultdict[str, list[str]] = defaultdict(list)
    for scene in value.scenes:
        used_identity_keys.update(identity_key(token) for token in scene.character_keys)
        for unit in scene.units:
            resolve_unit_event_key(scene, unit)
            used_identity_keys.update(
                identity_key(token)
                for token in unit.text_provenance.content_owner_keys
            )
            if unit.kind == "dialogue":
                if not unit.speaker_key:
                    raise ValueError(f"scene {scene.key} 对白缺少 speaker_key")
                speaker_key = identity_key(unit.speaker_key)
                used_identity_keys.add(speaker_key)
                if speaker_key not in event_speaker_keys[unit.event_key]:
                    event_speaker_keys[unit.event_key].append(speaker_key)
    for event in value.events:
        used_identity_keys.update(
            identity_key(token)
            for token in event.actor_keys
            if str(token).strip() != "audience"
        )
        used_identity_keys.update(
            identity_key(token)
            for token in event.target_keys
            if str(token).strip() != "audience"
        )
        used_identity_keys.update(
            identity_key(token)
            for token in event.onscreen_entity_keys
            if str(token).strip() != "audience"
        )
        used_identity_keys.update(
            identity_key(token)
            for token in event.perceivable_by
            if str(token).strip() != "audience"
        )
        used_identity_keys.update(
            identity_key(token)
            for token in event.text_provenance.content_owner_keys
        )
    ordered_used_keys = [
        key for key in identity_by_key if key in used_identity_keys
    ]
    final_identity_ids: dict[str, str] = {}
    identity_key_by_authority: dict[str, str] = {}
    for key in ordered_used_keys:
        identity = identity_by_key[key]
        authority_id = str(identity.authority_id or "").strip()
        if not authority_id:
            authority_id = (
                f"bible:{identity.display_name}"
                if identity.display_name in bible_by_name
                else _structural_context_authority_id(episode, key)
            )
            identity.authority_id = authority_id
            compiler_audit.append({
                "path": f"identities.{key}.authority_id",
                "operation": "bind_stable_authority_id",
                "to": authority_id,
                "reason": (
                    "compiled_graph_identity_ids_must_not_depend_on_order"
                ),
            })
        previous_key = identity_key_by_authority.get(authority_id)
        if previous_key is not None and previous_key != key:
            raise ScreenplayIRIdentityConflictError(
                f"authority_id={authority_id} 同时绑定多个 IR identity_key",
                issues=[{
                    "reason": "authority_bound_to_multiple_ir_identities",
                    "authority_id": authority_id,
                    "identity_keys": [previous_key, key],
                }],
            )
        identity_key_by_authority[authority_id] = key
        final_identity_ids[key] = authority_id

    def identity_id(token: str) -> str:
        return final_identity_ids[identity_key(token)]

    def display_name(token: str) -> str:
        return identity_by_key[identity_key(token)].display_name

    authorized_chapters = {
        str(key): str(text)
        for key, text in (
            episode.get("authorized_source_chapters")
            if isinstance(episode.get("authorized_source_chapters"), dict)
            else {}
        ).items()
        if str(text)
    }

    source_evidence: list[dict[str, Any]] = []
    propositions: list[dict[str, Any]] = []
    adaptation_decisions: list[dict[str, Any]] = []
    source_prop_by_statement: dict[str, str] = {}
    adapted_prop_by_statement: dict[str, str] = {}
    event_source_evidence_id: dict[str, str] = {}
    event_source_prop_id: dict[str, str] = {}
    event_adapted_prop_id: dict[str, str] = {}
    event_decision_id: dict[str, str] = {}
    environment_subject_id = system_environment_entity_id(
        episode.get("id") or f"episode-{episode_no}"
    )
    event_participant_ids: dict[str, list[str]] = {}
    event_state_subject_ids: dict[str, list[str]] = {}

    for position, event in enumerate(value.events, start=1):
        chapter_id, start, end, exact_excerpt = _source_location(
            event.source_excerpt,
            source_text=source_text,
            source_segment_ids=event.source_segment_ids,
            segments=segments,
            authorized_source_chapters=authorized_chapters,
        )
        evidence_id = f"SE-{position}"
        event_source_evidence_id[event.key] = evidence_id
        source_evidence.append({
            "source_evidence_id": evidence_id,
            "source_span": {
                "chapter_id": chapter_id,
                "start": start,
                "end": end,
            },
            "verbatim_excerpt": exact_excerpt,
            "confidence": 1.0,
        })

        actor_ids = [
            identity_id(token) for token in event.actor_keys
            if str(token).strip() != "audience"
        ]
        target_ids = [
            identity_id(token) for token in event.target_keys
            if str(token).strip() != "audience"
        ]
        speaker_ids = list(dict.fromkeys([
            final_identity_ids[key]
            for key in event_speaker_keys.get(event.key, [])
        ]))
        content_owner_ids = list(dict.fromkeys([
            identity_id(token)
            for token in event.text_provenance.content_owner_keys
        ]))
        if typed_visual_unit_contract:
            if event.environment_only:
                if event.state_subject_keys:
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} event {event.key} 同时声明"
                        "state_subject_keys 与 environment_only"
                    )
                state_subject_ids = [environment_subject_id]
            else:
                subject_keys = list(dict.fromkeys(event.state_subject_keys))
                if (
                    not subject_keys
                    or any(key not in event.actor_keys for key in subject_keys)
                ):
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} event {event.key} 缺少"
                        " exact-unit typed actor state_subject_keys"
                    )
                state_subject_ids = [
                    identity_id(subject_key) for subject_key in subject_keys
                ]
        else:
            non_actor_subject_ids = list(dict.fromkeys([
                *speaker_ids,
                *content_owner_ids,
            ]))
            state_subject_ids = [(
                actor_ids
                or target_ids
                or (
                    non_actor_subject_ids
                    if len(non_actor_subject_ids) == 1
                    else []
                )
                or [environment_subject_id]
            )[0]]
        event_state_subject_ids[event.key] = state_subject_ids
        participants = list(dict.fromkeys([
            *actor_ids,
            *target_ids,
            *[
                identity_id(token) for token in event.onscreen_entity_keys
                if str(token).strip() != "audience"
            ],
            *[
                identity_id(token)
                for token in event.perceivable_by
                if str(token).strip() != "audience"
            ],
            *speaker_ids,
            *content_owner_ids,
            *[
                identity_id(delivery.participant_key)
                for delivery in event.participant_deliveries
            ],
            *[
                identity_id(token)
                for token in scene_by_key[event.scene_key].character_keys
            ],
            *state_subject_ids,
        ]))
        if not participants and not typed_visual_unit_contract:
            participants = [final_identity_ids[ordered_used_keys[0]]]
        event_participant_ids[event.key] = participants

        source_statement = event.source_statement.strip() or exact_excerpt
        source_identity = re.sub(r"\s+", "", source_statement).casefold()
        source_prop_id = source_prop_by_statement.get(source_identity)
        if source_prop_id is None:
            source_prop_id = f"P-SOURCE-{len(source_prop_by_statement) + 1}"
            source_prop_by_statement[source_identity] = source_prop_id
            propositions.append({
                "proposition_id": source_prop_id,
                "semantic_identity_key": _semantic_key(
                    "source_canon", source_statement,
                ),
                "canonical_statement": source_statement,
                "narrative_domain": "source_canon",
                "entity_ids": participants,
                "direct_source_evidence_ids": [evidence_id],
                "domain_truth_status": "true",
            })
        else:
            existing = next(
                item for item in propositions
                if item["proposition_id"] == source_prop_id
            )
            existing["entity_ids"] = list(dict.fromkeys([
                *existing["entity_ids"], *participants,
            ]))
            existing["direct_source_evidence_ids"] = list(dict.fromkeys([
                *existing["direct_source_evidence_ids"], evidence_id,
            ]))
        event_source_prop_id[event.key] = source_prop_id

        adapted_statement = event.adapted_statement.strip()
        adapted_identity = re.sub(r"\s+", "", adapted_statement).casefold()
        adapted_prop_id = adapted_prop_by_statement.get(adapted_identity)
        if adapted_prop_id is None:
            adapted_prop_id = f"P-ADAPTED-{len(adapted_prop_by_statement) + 1}"
            adapted_prop_by_statement[adapted_identity] = adapted_prop_id
            propositions.append({
                "proposition_id": adapted_prop_id,
                "semantic_identity_key": _semantic_key(
                    "adapted_story", adapted_statement,
                ),
                "canonical_statement": adapted_statement,
                "narrative_domain": "adapted_story",
                "entity_ids": participants,
                "direct_source_evidence_ids": [],
                "domain_truth_status": "true",
            })
        else:
            existing = next(
                item for item in propositions
                if item["proposition_id"] == adapted_prop_id
            )
            existing["entity_ids"] = list(dict.fromkeys([
                *existing["entity_ids"], *participants,
            ]))
        event_adapted_prop_id[event.key] = adapted_prop_id

        decision_id = f"AD-{position}"
        event_decision_id[event.key] = decision_id
        adaptation_decisions.append({
            "adaptation_decision_id": decision_id,
            "source_proposition_ids": [source_prop_id],
            "adapted_proposition_ids": [adapted_prop_id],
            "relation": (
                event.adaptation_relation
                if event.adaptation_relation in {
                    "preserve", "condense", "split", "combine", "transform",
                    "omit", "invent", "other",
                }
                else "other"
            ),
            "custom_relation": (
                None
                if event.adaptation_relation in {
                    "preserve", "condense", "split", "combine", "transform",
                    "omit", "invent",
                }
                else event.adaptation_relation
            ),
            "creative_reason": event.adaptation_reason,
            "protected_causal_effect_ids": [adapted_prop_id],
            "affected_event_ids": [event_ids[event.key]],
            "uncertainty": None,
        })

    adapted_ids_in_order = list(dict.fromkeys(
        event_adapted_prop_id[event.key] for event in value.events
    ))
    final_adapted_prop_id = adapted_ids_in_order[-1]
    first_adapted_prop_id = adapted_ids_in_order[0]

    state_facts: list[dict[str, Any]] = []
    narrative_events: list[dict[str, Any]] = []
    atomic_actions: list[dict[str, Any]] = []
    narrative_evidence: list[dict[str, Any]] = []
    character_states: list[dict[str, Any]] = []
    character_beliefs: list[dict[str, Any]] = []
    legacy_events: list[StoryEvent] = []
    information_ledger: list[InformationItem] = []
    event_evidence_ids: dict[str, str] = {}
    event_action_ids: dict[str, str] = {}
    event_character_state_ids: defaultdict[str, list[str]] = defaultdict(list)
    effective_render_policy = {
        event.key: event.render_policy
        for event in value.events
    }
    if strict_unit_ownership:
        changed_render_policy_keys: list[str] = []
        for event_index, event in enumerate(value.events):
            if (
                event.narrative_layer != "story"
                or event.render_policy == "exclude_from_spine"
            ):
                continue
            next_event = (
                value.events[event_index + 1]
                if event_index + 1 < len(value.events)
                else None
            )
            projected_policy = (
                "merge_adjacent"
                if (
                    next_event is not None
                    and next_event.scene_key == event.scene_key
                    and next_event.narrative_layer == "story"
                    and next_event.render_policy
                    != "exclude_from_spine"
                )
                else "standalone"
            )
            effective_render_policy[event.key] = projected_policy
            if projected_policy != event.render_policy:
                changed_render_policy_keys.append(event.key)
        if changed_render_policy_keys:
            compiler_audit.append({
                "path": "events[*].render_policy",
                "operation": "project_contiguous_delivery_merge_policy",
                "count": len(changed_render_policy_keys),
                "sample_event_keys": changed_render_policy_keys[:20],
                "reason": (
                    "strict_unit_events_retain_identity_and_traceability_"
                    "while_same_scene_delivery_tasks_may_share_one_shot"
                ),
            })

    def state_fact_ids(position: int, count: int) -> list[str]:
        base = f"F-{position}"
        if count == 1:
            return [base]
        return [f"{base}-{index}" for index in range(1, count + 1)]

    initial_subjects = event_state_subject_ids[value.events[0].key]
    previous_fact_ids = state_fact_ids(0, len(initial_subjects))
    state_facts.extend({
        "fact_id": fact_id,
        "proposition_id": first_adapted_prop_id,
        "subject_id": subject_id,
        "predicate_id": "episode_state",
        "value": {"kind": "text", "data": value.events[0].precondition_state},
        "time_scope": "main@0",
        "visibility": "visible",
        "provenance": "screenplay",
        "confidence": 1.0,
    } for fact_id, subject_id in zip(previous_fact_ids, initial_subjects))

    for position, event in enumerate(value.events, start=1):
        event_id = event_ids[event.key]
        action_id = f"A-{position}"
        evidence_id = f"EV-{position}"
        subject_ids = event_state_subject_ids[event.key]
        current_fact_ids = state_fact_ids(position, len(subject_ids))
        pre_prop_id = (
            first_adapted_prop_id
            if position == 1
            else event_adapted_prop_id[value.events[position - 2].key]
        )
        adapted_prop_id = event_adapted_prop_id[event.key]
        actor_ids = [
            identity_id(token) for token in event.actor_keys
            if str(token).strip() != "audience"
        ]
        target_ids = [
            identity_id(token) for token in event.target_keys
            if str(token).strip() != "audience"
        ]
        onscreen_entity_ids = [
            identity_id(token) for token in event.onscreen_entity_keys
            if str(token).strip() != "audience"
        ]
        participant_delivery_rows: list[dict[str, Any]] = []
        participant_evidence_rows: list[dict[str, Any]] = []
        for delivery_position, delivery in enumerate(
            event.participant_deliveries,
            start=1,
        ):
            participant_id = identity_id(delivery.participant_key)
            participant_evidence_id = (
                f"{evidence_id}-PD{delivery_position}"
            )
            participant_delivery_rows.append({
                "action_id": action_id,
                "participant_id": participant_id,
                "evidence_ids": [participant_evidence_id],
                "audible": delivery.audible,
                "visible_effect": delivery.visible_effect,
                "visible_reaction": delivery.visible_reaction,
            })
            participant_evidence_rows.append({
                "evidence_id": participant_evidence_id,
                "anchor": {"type": "event", "id": event_id},
                "observable_claim": delivery.observable_claim,
                "perceivable_by": ["audience"],
                "supports_proposition_ids": [adapted_prop_id],
                "planned_salience": event.salience,
                "planned_duration_s": event.readability_s,
                "competing_attention_ids": [],
            })
        # State ownership follows the same complete authority relation used by
        # the event's propositions. Falling back only to the first actor (or
        # the episode's initial subject) made target/observer/speaker/scene
        # participants disappear and produced undeclared pseudo identities.
        state_facts.extend({
            "fact_id": fact_id,
            "proposition_id": adapted_prop_id,
            "subject_id": subject_id,
            "predicate_id": "episode_state",
            "value": {"kind": "text", "data": event.resulting_state},
            "time_scope": f"main@{position}",
            "visibility": "visible",
            "provenance": "screenplay",
            "confidence": 1.0,
        } for fact_id, subject_id in zip(current_fact_ids, subject_ids))

        phases = list(event.action_phases) or [
            IRActionPhase(
                start_condition=event.precondition_state,
                end_condition=event.completion_condition,
                estimated_min_s=1.0,
            )
        ]
        phase_rows = [
            {
                "phase_id": f"{action_id}/P{phase_index}",
                "start_condition": phase.start_condition,
                "end_condition": phase.end_condition,
                "estimated_min_s": phase.estimated_min_s,
            }
            for phase_index, phase in enumerate(phases, start=1)
        ]
        atomic_actions.append({
            "action_id": action_id,
            "actor_ids": actor_ids,
            "target_ids": target_ids,
            "action_agency": {
                "kind": event.action_agency.kind,
                "identity_bearing": bool(actor_ids or target_ids),
                "source_segment_ids": list(event.source_segment_ids),
            },
            "text_provenance": {
                "kind": event.text_provenance.kind,
                "identity_keys": [
                    identity_id(token)
                    for token in event.text_provenance.identity_keys
                ],
                "content_owner_keys": [
                    identity_id(token)
                    for token in event.text_provenance.content_owner_keys
                ],
                "source_segment_ids": list(
                    event.text_provenance.source_segment_ids
                ),
            },
            "dialogue_text": event.dialogue_text,
            "required_text": event.required_text,
            "prop_text": event.prop_text,
            "on_screen_text": event.on_screen_text,
            "participant_deliveries": participant_delivery_rows,
            "semantic_intent": event.action_intent,
            "precondition_fact_ids": list(previous_fact_ids),
            "effects_add": list(current_fact_ids),
            "effects_remove": list(previous_fact_ids),
            "completion_condition": event.completion_condition,
            "decision_requirement": (
                "applies" if event.decision_required and actor_ids
                else "not_applicable"
            ),
            "decision_not_applicable_reason": (
                None
                if event.decision_required and actor_ids
                else (
                    event.decision_reason
                    or "该事件由环境变化或非自主作用触发，不需要人物选择链"
                )
            ),
            "temporal_phases": phase_rows,
            "splittable_boundaries": [
                phase_rows[index]["phase_id"]
                for index, phase in enumerate(phases)
                if phase.splittable_after
            ],
        })
        event_action_ids[event.key] = action_id

        perceivable = list(dict.fromkeys([
            *(
                identity_id(token)
                for token in event.perceivable_by
                if str(token).strip() != "audience"
            ),
            *actor_ids,
            "audience",
        ]))
        narrative_evidence.append({
            "evidence_id": evidence_id,
            "anchor": {"type": "event", "id": event_id},
            "observable_claim": event.observable_claim,
            "perceivable_by": perceivable,
            "supports_proposition_ids": [adapted_prop_id],
            "planned_salience": event.salience,
            "planned_duration_s": event.readability_s,
            "competing_attention_ids": [],
        })
        narrative_evidence.extend(participant_evidence_rows)
        event_evidence_ids[event.key] = evidence_id

        parents = [
            event_ids[key] for key in event.causal_parent_keys
            if key in event_ids
        ]
        if position > 1:
            previous_event_id = event_ids[value.events[position - 2].key]
            if previous_event_id not in parents:
                parents.append(previous_event_id)
        downstream = (
            [event_ids[value.events[position].key]]
            if position < len(value.events)
            else []
        )
        narrative_events.append({
            "event_id": event_id,
            "proposition_ids": list(dict.fromkeys([
                pre_prop_id, adapted_prop_id,
            ])),
            "causal_parent_ids": parents,
            "precondition_fact_ids": list(previous_fact_ids),
            "action_ids": [action_id],
            "onscreen_entity_ids": onscreen_entity_ids,
            "effects_add": list(current_fact_ids),
            "effects_remove": list(previous_fact_ids),
            "character_goal_effects": [],
            "downstream_dependency_event_ids": downstream,
            "salience": event.salience,
            "irreversibility": event.irreversibility,
            "must_keep": event.must_keep,
            "narrative_layer": event.narrative_layer,
            "event_priority": event.event_priority,
            "render_policy": effective_render_policy[event.key],
            "delivery_scope_id": str(episode.get("id") or f"episode-{episode_no}"),
            "delivery_policy": "deliver",
            "primary_delivery_window_id": f"RW-{position}",
        })
        previous_fact_ids = current_fact_ids
        legacy_events.append(StoryEvent(
            event_id=event_id,
            source_span=",".join(event.source_segment_ids),
            source_fact=event.source_statement,
            state_in=event.precondition_state,
            trigger=event.action_intent,
            visible_change=event.observable_claim,
            state_out=event.resulting_state,
            must_keep=event.must_keep,
            narrative_layer=event.narrative_layer,
            event_priority=event.event_priority,
            render_policy=effective_render_policy[event.key],
            adaptation_addition=event.adaptation_relation == "invent",
            adaptation_reason=event.adaptation_reason,
            approved=event.adaptation_relation != "invent",
        ))

        info_values = event.information or [event.observable_claim]
        for content in info_values:
            information_ledger.append(InformationItem(
                info_id=f"I{len(information_ledger) + 1}",
                event_id=event_id,
                content=content,
                delivery_owner="visual_action",
                status="unassigned",
            ))

        for actor_position, actor_id in enumerate(actor_ids, start=1):
            state_id = f"CDS-{position}-{actor_position}"
            belief_id = f"CB-{position}-{actor_position}"
            event_character_state_ids[event.key].append(state_id)
            character_states.append({
                "character_state_id": state_id,
                "character_id": actor_id,
                "anchor": {"type": "event", "id": event_id},
                "goal_proposition_ids": [adapted_prop_id],
                "stakes_proposition_ids": [adapted_prop_id],
                "relationship_state": {},
                "emotion": {
                    "label": event.character_emotion or "受当前事件影响",
                    "intensity": max(0.1, event.salience),
                    "observable_evidence": [evidence_id],
                },
                "pressure": event.salience,
                "tactic": event.character_tactic or event.action_intent,
            })
            if event.decision_required:
                character_beliefs.append({
                    "character_belief_id": belief_id,
                    "character_id": actor_id,
                    "anchor": {"type": "event", "id": event_id},
                    "perceived_evidence_ids": [evidence_id],
                    "beliefs": [{
                        "proposition_id": adapted_prop_id,
                        "stance": "believed",
                        "confidence": max(0.6, event.salience),
                        "evidence_ids": [evidence_id],
                    }],
                    "misbelief_proposition_ids": [],
                    "decision_proposition_ids": [adapted_prop_id],
                    "decision_basis_ids": [evidence_id],
                    "decision_action_ids": [action_id],
                })

    dialogue_chain_rows: dict[str, list[KeyDialogueTurn]] = {}
    dialogue_chain_order: list[str] = []
    dialogue_chain_topics: dict[str, str] = {}
    dialogue_chain_scenes: dict[str, str] = {}
    key_line_event: dict[int, str] = {}
    script_lines: list[str] = []
    scene_outlines: list[ScriptScene] = []
    event_keys_by_scene: defaultdict[str, list[str]] = defaultdict(list)
    for event in value.events:
        event_keys_by_scene[event.scene_key].append(event.key)

    for scene_position, scene in enumerate(value.scenes, start=1):
        original_heading = scene.scene_heading.strip()
        heading_suffix = re.sub(
            r"^【场[^】]*】\s*",
            "",
            original_heading,
        )
        heading = f"【场{scene_position}】{heading_suffix}"
        if heading != original_heading:
            compiler_audit.append({
                "path": f"scenes.{scene.key}.scene_heading",
                "operation": "renumber",
                "from": original_heading,
                "to": heading,
                "reason": "published_scene_numbers_are_contiguous",
            })
            scene.scene_heading = heading
        script_lines.append(heading)
        for unit in scene.units:
            if unit.kind == "action":
                action_text = _screenplay_action_text(unit.text)
                if action_text:
                    script_lines.append(action_text)
                if action_text != unit.text.strip():
                    compiler_audit.append({
                        "path": f"scenes.{scene.key}.units.action",
                        "operation": "remove_directing_vocabulary",
                        "from": unit.text.strip(),
                        "to": action_text,
                        "reason": "screenplay_body_contract",
                    })
                continue
            speaker = display_name(unit.speaker_key or "")
            local_chain_key = (
                unit.chain_key.strip() or f"scene-{scene_position}"
            )
            chain_key = f"{scene.key}:{local_chain_key}"
            if chain_key not in dialogue_chain_rows:
                dialogue_chain_rows[chain_key] = []
                dialogue_chain_order.append(chain_key)
                dialogue_chain_topics[chain_key] = (
                    scene.story_function or scene.summary
                )
                dialogue_chain_scenes[chain_key] = f"SC{scene_position:02d}"
            dialogue_source_evidence = _dialogue_source_text(
                unit.source_text,
                source_text,
            )
            spoken_parts = _split_spoken_line(
                unit.text,
                max_chars=config.MAX_SPOKEN_CHARS_PER_SHOT,
            )
            if len(spoken_parts) > 1:
                compiler_audit.append({
                    "path": f"scenes.{scene.key}.units.dialogue",
                    "operation": "split_by_spoken_capacity",
                    "parts": len(spoken_parts),
                    "reason": "downstream_single_shot_voice_capacity",
                })
            for part_index, line in enumerate(spoken_parts):
                script_lines.append(f"{speaker}：{line}")
                dialogue_chain_rows[chain_key].append(KeyDialogueTurn(
                    speaker=speaker,
                    line=line,
                    function=(
                        unit.function
                        if (
                            part_index == 0
                            and unit.function in _DIALOGUE_FUNCTIONS
                        )
                        else "statement"
                    ),
                    source_text=dialogue_source_evidence,
                ))
                key_line_event[
                    sum(
                        len(dialogue_chain_rows[key])
                        for key in dialogue_chain_order
                    )
                ] = unit.event_key
        script_lines.append("")

        source_basis = scene.source_basis.strip()
        if len(source_basis) < 8:
            source_ids = list(dict.fromkeys(
                source_id
                for event_key in event_keys_by_scene.get(scene.key, [])
                for source_id in event_by_key[event_key].source_segment_ids
            ))
            source_basis = (
                "、".join(source_ids)
                + " 对应的授权原文事件与场次状态"
            )
            compiler_audit.append({
                "path": f"scenes.{scene.key}.source_basis",
                "operation": "derive",
                "reason": "scene_event_source_ownership",
            })
        scene_event_keys = event_keys_by_scene.get(scene.key, [])
        entry_state = scene.entry_state.strip() or (
            event_by_key[scene_event_keys[0]].precondition_state
            if scene_event_keys else ""
        )
        exit_state = scene.exit_state.strip() or (
            event_by_key[scene_event_keys[-1]].resulting_state
            if scene_event_keys else ""
        )
        if entry_state != scene.entry_state.strip():
            compiler_audit.append({
                "path": f"scenes.{scene.key}.entry_state",
                "operation": "derive",
                "reason": "first_scene_event_precondition",
            })
        if exit_state != scene.exit_state.strip():
            compiler_audit.append({
                "path": f"scenes.{scene.key}.exit_state",
                "operation": "derive",
                "reason": "last_scene_event_result",
            })
        scene.entry_state = entry_state
        scene.exit_state = exit_state
        scene_character_tokens = list(dict.fromkeys([
            *scene.character_keys,
            *[
                token
                for event_key in event_keys_by_scene.get(scene.key, [])
                for token in (
                    *event_by_key[event_key].actor_keys,
                    *event_by_key[event_key].target_keys,
                )
                if str(token).strip() != "audience"
            ],
        ]))
        visible_scene_characters = list(dict.fromkeys(
            display_name(token)
            for token in scene_character_tokens
            if (
                identity_by_key[identity_key(token)].visual_policy
                != "offscreen_only"
            )
        ))
        context_requirements = list(dict.fromkeys([
            *scene.context_requirements,
            *inferred_context_by_scene.get(scene.key, []),
        ]))
        if not context_requirements:
            context_requirements = [
                f"先建立{heading}的时间、地点与空间关系",
                (
                    "本场人物关系与当前局势："
                    + (scene.summary or scene.story_function)
                ),
            ]
            compiler_audit.append({
                "path": f"scenes.{scene.key}.context_requirements",
                "operation": "derive",
                "reason": "scene_heading_and_summary_define_required_context",
            })
        scene_turn = scene.turn.strip()
        if len(textmatch.condense(scene_turn)) < 8:
            scene_turn = (
                f"本场结束时，{exit_state}，"
                f"并完成「{scene.story_function or scene.summary}」"
            )
            compiler_audit.append({
                "path": f"scenes.{scene.key}.turn",
                "operation": "derive",
                "reason": "scene_exit_state_defines_handoff_change",
            })
            scene.turn = scene_turn
        scene_outlines.append(ScriptScene(
            scene_no=scene_position,
            scene_heading=heading,
            story_function=scene.story_function,
            characters=visible_scene_characters,
            summary=scene.summary,
            conflict=scene.conflict,
            turn=scene_turn,
            source_basis=source_basis,
            previous_scene_exit_state=scene.previous_scene_exit_state,
            opening_image=scene.opening_image or entry_state,
            agency_contracts=scene.agency_contracts,
            entry_state=entry_state,
            exit_state=exit_state,
            context_requirements=context_requirements,
        ))

    dialogue_chains: list[KeyDialogueChain] = []
    for key in dialogue_chain_order:
        turns = dialogue_chain_rows[key]
        for offset in range(0, len(turns), DIALOGUE_CHAIN_TURNS_HARD_MAX):
            chunk = [
                turn.model_copy(deep=True)
                for turn in turns[
                offset:offset + DIALOGUE_CHAIN_TURNS_HARD_MAX
                ]
            ]
            if (
                offset
                and chunk
                and chunk[0].function == "response"
            ):
                chunk[0].function = "statement"
            dialogue_chains.append(KeyDialogueChain(
                chain_id=f"DC{len(dialogue_chains) + 1}",
                scene_id=dialogue_chain_scenes[key],
                topic=(
                    dialogue_chain_topics[key]
                    + ("（续）" if offset else "")
                ),
                turns=chunk,
            ))

    info_ids_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for item in information_ledger:
        info_ids_by_event[item.event_id].append(item.info_id)
    key_ids_by_event: defaultdict[str, list[str]] = defaultdict(list)
    key_position = 0
    for chain in dialogue_chains:
        for _turn in chain.turns:
            key_position += 1
            local_event_key = key_line_event.get(key_position)
            if local_event_key:
                key_ids_by_event[event_ids[local_event_key]].append(
                    f"KL{key_position:02d}"
                )

    plot_beats = []
    for beat in value.beats:
        related_events = [
            event
            for event in value.events
            if set(event.source_segment_ids).intersection(
                beat.source_segment_ids
            )
        ]
        related_event_ids = [event_ids[event.key] for event in related_events]
        priority = (
            "causal"
            if any(event.event_priority == "causal" for event in related_events)
            else "supporting"
            if any(event.event_priority == "supporting" for event in related_events)
            else "connective"
        )
        related_render_policies = [
            effective_render_policy[event.key]
            for event in related_events
        ]
        render_policy = (
            "standalone"
            if "standalone" in related_render_policies
            else "merge_adjacent"
            if "merge_adjacent" in related_render_policies
            else "exclude_from_spine"
        )
        plot_beats.append(PlotSpineBeat(
            beat_id=beat_ids[beat.key],
            who=beat.who,
            does=beat.does,
            turn=beat.turn,
            must_keep=beat.must_keep,
            narrative_layer="story",
            event_priority=priority,
            render_policy=render_policy,
            source_segment_ids=beat.source_segment_ids,
            purpose=beat.purpose,
            information_ids=list(dict.fromkeys(
                info_id
                for event_id in related_event_ids
                for info_id in info_ids_by_event[event_id]
            )),
            key_line_ids=list(dict.fromkeys(
                key_id
                for event_id in related_event_ids
                for key_id in key_ids_by_event[event_id]
            )),
        ))

    full_script_text = "\n".join(script_lines).strip()
    for beat in plot_beats:
        delivery = f"{beat.who}{beat.does}"
        source_backed_units = [
            unit
            for event in value.events
            if set(event.source_segment_ids).intersection(
                beat.source_segment_ids
            )
            for unit in units_by_event.get(event.key, [])
            if unit.text.strip()
        ]
        if any(
            (
                _screenplay_action_text(unit.text)
                if unit.kind == "action"
                else unit.text.strip()
            ) in full_script_text
            for unit in source_backed_units
        ):
            continue
        if beat.must_keep and delivery and delivery not in full_script_text:
            scene_index = next(
                (
                    index for index, scene in enumerate(value.scenes)
                    if any(
                        set(event_by_key[event_key].source_segment_ids).intersection(
                            beat.source_segment_ids
                        )
                        for event_key in event_keys_by_scene.get(scene.key, [])
                    )
                ),
                0,
            )
            heading = scene_outlines[scene_index].scene_heading
            insertion = _screenplay_action_text(
                f"{beat.who}{beat.does}，{beat.turn}。"
            )
            marker = full_script_text.find(heading) + len(heading)
            full_script_text = (
                full_script_text[:marker]
                + "\n"
                + insertion
                + full_script_text[marker:]
            )

    # key_lines 与 document 投影共用同一派生算法：以 dialogue_chains 为权威源，
    # 依据已定稿的 full_script_text 正文出现顺序排列。必须在 full_script_text 完成
    # must_keep 插入后再派生，两条路径才能对同一输入逐字段相等（消除结构顺序漂移）。
    # 延迟 import：validators 在函数级反向引用本模块，顶层直连会成环。
    from app.validators import derive_key_lines

    key_lines = derive_key_lines(dialogue_chains, full_script_text)

    scope_id = str(episode.get("id") or f"episode-{episode_no}")
    dramatic_questions = [{
        "dramatic_question_id": "DQ-1",
        "question_text": metadata.dramatic_question,
        "target_proposition_ids": [final_adapted_prop_id],
        "open_anchor": {"type": "event", "id": event_ids[value.events[0].key]},
        "intended_resolution_scope_id": scope_id,
        "desired_state_while_open": "unknown",
        "resolution_anchor": {
            "type": "event",
            "id": event_ids[value.events[-1].key],
        },
        "status": "resolved",
    }]

    prior_ids = {
        key: f"AP-{position}"
        for position, key in enumerate(prior_by_key, start=1)
    }
    audience_priors: list[dict[str, Any]] = []
    audience_states: list[dict[str, Any]] = []
    audience_paths: list[dict[str, Any]] = []
    target_delta_ids: list[str] = []
    last_event_id = event_ids[value.events[-1].key]
    setup_memory = [{
        "proposition_id": first_adapted_prop_id,
        "retention_confidence": 1.0,
    }]
    experience = value.experience or IRExperience(
        director_objective="让观众理解本集完整因果链及最终局势变化",
        satisfaction_criteria="冷观众能复述关键事件、人物目标与最终状态变化",
    )
    experience_processing_s = min(
        float(config.VIDEO_DURATION_MAX_S),
        max(0.5, float(experience.required_processing_s or 0)),
    )
    for position, (key, prior) in enumerate(prior_by_key.items(), start=1):
        prior_id = prior_ids[key]
        state_in_id = f"AS-{position}-IN"
        state_out_id = f"AS-{position}-OUT"
        audience_priors.append({
            "audience_prior_id": prior_id,
            "scope_id": scope_id,
            "audience_description": prior.description,
            "assumed_known_proposition_ids": (
                [event_source_prop_id[value.events[0].key]]
                if position > 1 else []
            ),
            "assumed_unknown_proposition_ids": adapted_ids_in_order,
            "familiarity_assumptions": prior.familiarity_assumptions,
            "language_and_context_assumptions": (
                prior.language_and_context_assumptions
            ),
            "attention_memory_assumptions": prior.attention_memory_assumptions,
            "calibration_source": "needs_review",
        })
        in_beliefs = [
            {
                "proposition_id": proposition_id,
                "stance": "unknown",
                "confidence": 0.0,
                "evidence_ids": [],
            }
            for proposition_id in adapted_ids_in_order
        ]
        out_beliefs = [
            {
                "proposition_id": proposition_id,
                "stance": prior.target_stance,
                "confidence": prior.target_confidence,
                "evidence_ids": [
                    event_evidence_ids[next(
                        event.key for event in value.events
                        if event_adapted_prop_id[event.key] == proposition_id
                    )]
                ],
            }
            for proposition_id in adapted_ids_in_order
        ]
        audience_states.extend([
            {
                "audience_state_id": state_in_id,
                "audience_prior_id": prior_id,
                "anchor": {
                    "type": "event",
                    "id": event_ids[value.events[0].key],
                },
                "beliefs": in_beliefs,
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": setup_memory,
                "attention_residue_ids": [],
                "affective_state": {},
            },
            {
                "audience_state_id": state_out_id,
                "audience_prior_id": prior_id,
                "anchor": {"type": "event", "id": last_event_id},
                "beliefs": out_beliefs,
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                # Question closure is expressed by DramaticQuestion.status and
                # the episode arc. Keeping the active set stable avoids
                # inventing a second target delta solely for a mechanical
                # snapshot-field change.
                "active_question_ids": ["DQ-1"],
                "working_memory": setup_memory,
                "attention_residue_ids": [],
                "affective_state": {},
            },
        ])
        delta_id = f"XD-{position}-1"
        target_delta_ids.append(delta_id)
        audience_paths.append({
            "audience_path_id": f"XP-{position}-1",
            "audience_prior_id": prior_id,
            "audience_state_in_id": state_in_id,
            "audience_state_out_target_id": state_out_id,
            "target_deltas": [{
                "target_delta_id": delta_id,
                "dimension": "belief",
                "proposition_ids": adapted_ids_in_order,
                "description": experience.director_objective,
                "from_state": {
                    proposition_id: {
                        "stance": "unknown",
                        "confidence": 0.0,
                        "evidence_ids": [],
                    }
                    for proposition_id in adapted_ids_in_order
                },
                "to_state": {
                    proposition_id: {
                        "stance": prior.target_stance,
                        "confidence": prior.target_confidence,
                        "evidence_ids": [
                            event_evidence_ids[next(
                                event.key for event in value.events
                                if event_adapted_prop_id[event.key] == proposition_id
                            )]
                        ],
                    }
                    for proposition_id in adapted_ids_in_order
                },
                "target_confidence": prior.target_confidence,
                "required_processing_s": experience_processing_s,
                "deadline_event_id": last_event_id,
                "primary_delivery_window_id": f"RW-{len(value.events)}",
                "custom_dimension": None,
            }],
        })

    experience_intents = [{
        "experience_intent_id": "XI-1",
        "scope_id": scope_id,
        "anchor_event_ids": [event_ids[event.key] for event in value.events],
        "director_objective": experience.director_objective,
        "attention_target_ids": adapted_ids_in_order,
        "audience_paths": audience_paths,
        "withheld_propositions": [],
        "forbidden_misconceptions": experience.forbidden_misconceptions,
    }]
    assimilation_tasks = [
        {
            "assimilation_task_id": f"AT-{position}",
            "experience_intent_id": "XI-1",
            "audience_path_id": path["audience_path_id"],
            "target_delta_id": path["target_deltas"][0]["target_delta_id"],
            "required_prior_proposition_ids": [],
            "downstream_dependency_event_ids": [last_event_id],
            "satisfaction_criteria": experience.satisfaction_criteria,
            "status": "planned",
        }
        for position, path in enumerate(audience_paths, start=1)
    ]

    readability_windows: list[dict[str, Any]] = []
    for position, event in enumerate(value.events, start=1):
        is_last = position == len(value.events)
        window_delta_ids = target_delta_ids if is_last else []
        event_readability_s = min(
            float(config.VIDEO_DURATION_MAX_S),
            max(0.5, float(event.readability_s or 0)),
        )
        scheduled = max(
            event_readability_s,
            experience_processing_s if is_last else 0.0,
        )
        readability_windows.append({
            "readability_window_id": f"RW-{position}",
            "event_ids": [event_ids[event.key]],
            "proposition_ids": [event_adapted_prop_id[event.key]],
            "target_delta_ids": window_delta_ids,
            "shot_ids": [],
            "attention_target_ids": [event_adapted_prop_id[event.key]],
            "evidence_ids": [event_evidence_ids[event.key]],
            "scheduled_processing_s": scheduled,
            "planned_available_s": scheduled,
            "competing_attention_ids": [],
            "readability_reason": (
                f"在后续事件使用前交付并留出处理时间：{event.observable_claim}"
            ),
            "status": "planned",
        })

    setup_payoff_contracts: list[dict[str, Any]] = []
    if len(value.events) > 1:
        setup_payoff_contracts.append({
            "setup_payoff_id": "SP-1",
            "setup_proposition_ids": [first_adapted_prop_id],
            "setup_event_ids": [event_ids[value.events[0].key]],
            "payoff_event_ids": [last_event_id],
            "intended_inference_ids": [final_adapted_prop_id],
            "retention_deadline_event_id": last_event_id,
            "minimum_retention_confidence": 0.5,
            "recall_needed": False,
            "status": "paid_off",
        })

    scene_contracts: list[dict[str, Any]] = []
    for position, scene in enumerate(value.scenes, start=1):
        scene_event_keys = event_keys_by_scene.get(scene.key) or [
            value.events[min(position - 1, len(value.events) - 1)].key
        ]
        turn_event_key = scene_event_keys[-1]
        scene_prop_id = event_adapted_prop_id[turn_event_key]
        state_ids = [
            state_id
            for event_key in scene_event_keys
            for state_id in event_character_state_ids[event_key]
        ]
        scene_character_ids = [
            identity_id(key) for key in scene.character_keys
        ]
        scene_state_subject_ids = [
            subject_id
            for event_key in scene_event_keys
            for subject_id in event_state_subject_ids[event_key]
        ]
        pov_id = (
            scene_character_ids
            or [
                subject_id
                for subject_id in scene_state_subject_ids
                if subject_id != environment_subject_id
            ]
        )
        point_of_view_character_id = pov_id[0] if pov_id else None
        relationship_deltas = (
            []
            if scene.entry_state.strip() != scene.exit_state.strip()
            else [{"description": scene.turn or "场次关系发生变化"}]
        )
        scene_contracts.append({
            "scene_id": scene_ids[scene.key],
            "applicability": "applies",
            "not_applicable_reason": None,
            "alternative_dramatic_function": None,
            "scene_question_id": "DQ-1",
            "point_of_view_character_id": point_of_view_character_id,
            "audience_state_paths": [
                {
                    "audience_prior_id": prior_ids[key],
                    "audience_state_in_id": f"AS-{prior_position}-IN",
                    "audience_state_out_target_id": f"AS-{prior_position}-OUT",
                }
                for prior_position, key in enumerate(prior_by_key, start=1)
            ],
            "character_state_in_ids": state_ids[:1],
            "goal_proposition_ids": [scene_prop_id],
            "obstacle_proposition_ids": [scene_prop_id],
            "stakes_proposition_ids": [scene_prop_id],
            "pressure_curve": [{
                "anchor": {
                    "type": "event",
                    "id": event_ids[turn_event_key],
                },
                "value": event_by_key[turn_event_key].salience,
            }],
            "turn_event_ids": [event_ids[turn_event_key]],
            "value_polarity_in": scene.entry_state,
            "value_polarity_out": scene.exit_state,
            "relationship_deltas": relationship_deltas,
            "character_state_out_ids": state_ids[-1:],
            "scene_button": scene.turn,
        })

    if len(value.events) > 1:
        arc_contracts = [{
            "arc_id": "ARC-EPISODE",
            "scope": "episode",
            "applicability": "applies",
            "not_applicable_reason": None,
            "alternative_dramatic_function": None,
            "core_question_ids": ["DQ-1"],
            "promise_proposition_ids": [first_adapted_prop_id],
            "escalation_event_ids": [
                event_ids[event.key] for event in value.events[:-1]
            ],
            "climax_event_ids": [last_event_id],
            "payoff_contract_ids": ["SP-1"],
            "pressure_curve": [
                {
                    "anchor": {"type": "event", "id": event_ids[event.key]},
                    "value": event.salience,
                }
                for event in value.events
            ],
            "information_density_curve": [
                {
                    "anchor": {"type": "event", "id": event_ids[event.key]},
                    "value": min(1.0, 0.4 + 0.6 * event.salience),
                }
                for event in value.events
            ],
            "processing_beats": [{
                "anchor": {
                    "type": "event",
                    "id": event_ids[value.events[0].key],
                },
                "purpose": "建立本集因果起点并供观众处理关键信息",
            }],
            "ending_hook_question_ids": [],
            "resolved_question_ids": ["DQ-1"],
            "carried_question_ids": [],
        }]
    else:
        arc_contracts = [{
            "arc_id": "ARC-EPISODE",
            "scope": "episode",
            "applicability": "not_applicable",
            "not_applicable_reason": "本集仅包含一个不可再拆的完整事件",
            "alternative_dramatic_function": "以单一状态变化完成本集交付",
            "core_question_ids": ["DQ-1"],
            "promise_proposition_ids": [],
            "escalation_event_ids": [],
            "climax_event_ids": [],
            "payoff_contract_ids": [],
            "pressure_curve": [],
            "information_density_curve": [],
            "processing_beats": [],
            "ending_hook_question_ids": [],
            "resolved_question_ids": ["DQ-1"],
            "carried_question_ids": [],
        }]

    spoken_keys = {
        identity_key(unit.speaker_key or "")
        for scene in value.scenes
        for unit in scene.units
        if unit.kind == "dialogue" and unit.speaker_key
    }
    voice_bible = [
        VoiceCanonical(
            speaker_id=final_identity_ids[key],
            display_name=identity.display_name,
            voice_canonical=(
                identity.voice_canonical
                or (
                    bible_by_name[identity.display_name].speech_style
                    if identity.display_name in bible_by_name else ""
                )
                or "符合当前身份与情境的稳定普通话声线"
            ),
            language="普通话",
            role_type=identity.role_type,
        )
        for key, identity in identity_by_key.items()
        if key in spoken_keys
    ]

    identity_contracts: list[dict[str, Any]] = []
    for key in ordered_used_keys:
        identity = identity_by_key[key]
        related_events = [
            event for event in value.events
            if key in {
                *[
                    identity_key(token) for token in event.actor_keys
                    if str(token).strip() != "audience"
                ],
                *[
                    identity_key(token) for token in event.target_keys
                    if str(token).strip() != "audience"
                ],
                *[
                    identity_key(token)
                    for token
                    in event.text_provenance.content_owner_keys
                ],
                *event_speaker_keys.get(event.key, []),
            }
        ]
        if not related_events:
            related_events = [value.events[0]]
        related_source_ids = list(dict.fromkeys(
            event_source_evidence_id[event.key] for event in related_events
        ))
        related_prop_ids = list(dict.fromkeys(
            event_adapted_prop_id[event.key] for event in related_events
        ))
        related_decision_ids = list(dict.fromkeys(
            event_decision_id[event.key] for event in related_events
        ))
        is_bible = identity.display_name in bible_by_name
        visual_policy = "canonical" if is_bible else identity.visual_policy
        asset_requirement = "required" if is_bible else identity.asset_requirement
        visual_canonical = (
            bible_by_name[identity.display_name].appearance_canonical
            if is_bible else identity.visual_canonical
        )
        if visual_policy == "offscreen_only":
            asset_requirement = "forbidden"
            visual_canonical = ""
        identity_contracts.append({
            "identity_id": final_identity_ids[key],
            "display_name": identity.display_name,
            "kind": identity.kind,
            "visual_policy": visual_policy,
            "visual_canonical": visual_canonical,
            "asset_requirement": asset_requirement,
            "voice_ids": (
                [final_identity_ids[key]] if key in spoken_keys else []
            ),
            "evidence": {
                "source_evidence_ids": related_source_ids,
                "proposition_ids": related_prop_ids,
                "adaptation_decision_ids": related_decision_ids,
                "rationale": (
                    identity.rationale
                    or "该身份被本集事件、场次或对白实际引用"
                ),
            },
        })

    narrative_plan = NarrativeContinuityPlan.model_validate({
        "contract_version": NARRATIVE_CONTRACT_VERSION,
        "scope_id": scope_id,
        "source_evidence": source_evidence,
        "propositions": propositions,
        "adaptation_decisions": adaptation_decisions,
        "state_facts": state_facts,
        "initial_state_fact_ids": ["F-0"],
        "evidence": narrative_evidence,
        "dramatic_questions": dramatic_questions,
        "events": narrative_events,
        "atomic_actions": atomic_actions,
        "action_relation_audits": [],
        "character_states": character_states,
        "character_beliefs": character_beliefs,
        "audience_priors": audience_priors,
        "audience_states": audience_states,
        "experience_intents": experience_intents,
        "assimilation_tasks": assimilation_tasks,
        "readability_windows": readability_windows,
        "setup_payoff_contracts": setup_payoff_contracts,
        "scene_contracts": scene_contracts,
        "arc_contracts": arc_contracts,
        "identity_contracts": identity_contracts,
    })

    script = EpisodeScreenplay(
        episode_no=episode_no,
        id=scope_id,
        mode="full_script",
        source_text_range=value.format_version or IR_VERSION,
        title=metadata.title,
        logline=metadata.logline,
        script_format_note=metadata.script_format_note,
        dramatic_question=metadata.dramatic_question,
        protagonist_goal=metadata.protagonist_goal,
        obstacle=metadata.obstacle,
        stakes=metadata.stakes,
        key_lines=key_lines,
        dialogue_chains=dialogue_chains,
        key_plot_points=[
            f"{beat.who}{beat.does}，{beat.turn}"
            for beat in plot_beats if beat.must_keep
        ],
        plot_spine=PlotSpine(
            episode_premise=metadata.episode_premise,
            spine_beats=plot_beats,
            must_keep_ending=metadata.must_keep_ending,
            drop_list=metadata.drop_list,
        ),
        source_coverage=coverage_rows,
        scene_outline=scene_outlines,
        full_script_text=full_script_text,
        character_state_changes=[
            f"{event.precondition_state} → {event.resulting_state}"
            for event in value.events
        ],
        emotional_curve=metadata.emotional_curve,
        ending_hook=metadata.ending_hook,
        source_basis=metadata.source_basis,
        adaptation_direction=metadata.adaptation_direction,
        opening=metadata.opening,
        development=metadata.development,
        conflict=metadata.conflict,
        climax=metadata.climax,
        episode_premise=metadata.episode_premise,
        events=legacy_events,
        information_ledger=information_ledger,
        voice_bible=voice_bible,
        approved_adaptations=metadata.approved_adaptations,
        forbidden_additions=metadata.forbidden_additions,
        narrative_plan=narrative_plan,
    )
    object.__setattr__(
        script,
        "_ir_compiler_audit",
        [*value.normalization_log, *compiler_audit],
    )
    object.__setattr__(script, "_ir_compiler_version", IR_COMPILER_VERSION)
    return script
