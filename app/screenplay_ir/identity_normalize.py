"""Normalizes a raw model-authored IR payload and collapses duplicate identity displays before authority resolution."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from app.character_policy import resolution_declares_functional_identity
from app.identity_authority import identity_resolution_is_authoritative
from app.schemas import Bible
from app.source_excerpt import index_source_segments

from .contract_validation import _as_list
from .models_core import IRIdentity
from .models_event import ScreenplayGenerationIR


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
            and identity_resolution_is_authoritative(item)
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
