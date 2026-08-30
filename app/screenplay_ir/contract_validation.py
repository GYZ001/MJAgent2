"""Structural preflight helpers and the screenplay-IR contract-error producers (participant delivery / event semantic paths / source-audit identity keys)."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app import textmatch
from app.narrative_blueprint import (
    BlueprintSourceAuditAnnotation,
    BlueprintSourceSemantics,
    _normalize_source_segment_id,
)
from app.schemas import ActionAgency, TextProvenance

from .constants import _SourceAuditAnnotationIdentity, _SourceSemanticIdentity


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
