"""Perception-evidence helpers for state-subject repair and the blueprint/shard candidate hash functions."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from .models_core import (
    NarrativeBlueprint,
    NarrativeBlueprintShard,
    NarrativeNode,
    NarrativeParticipantEvidence,
    NarrativeStateSubjectAssignment,
)


def _node_identity_has_perception_evidence(
    node: NarrativeNode,
    *,
    identity_key: str,
    source_unit_key: str,
) -> bool:
    source_segment_id = source_unit_key.split(":unit:", 1)[0]
    return any(
        evidence.identity_key == identity_key
        and evidence.usage in {"visible", "voice"}
        and source_segment_id in evidence.source_segment_ids
        and (
            not evidence.source_unit_keys
            or source_unit_key in evidence.source_unit_keys
        )
        for evidence in node.participant_evidence
    )


def _node_state_subject_repairable_identities(
    node: NarrativeNode,
    *,
    source_unit_key: str,
) -> list[str]:
    """Return existing participants with perception authority for one unit."""
    return [
        identity_key
        for identity_key in dict.fromkeys(
            value.strip()
            for value in node.participants
            if value.strip()
        )
        if _node_identity_has_perception_evidence(
            node,
            identity_key=identity_key,
            source_unit_key=source_unit_key,
        )
    ]


def normalize_blueprint_state_subject_perception(
    blueprint: NarrativeBlueprint | NarrativeBlueprintShard,
) -> int:
    """Add grouped visible evidence for valid exact-unit state subjects.

    Only nodes/units that already resolve to exactly one state subject are
    touched, so ambiguous, conflicting, missing and environment units are left
    for the model to adjudicate.  Shard generation and the merged-blueprint
    repair loop both run this before validation.
    """
    added = 0
    for node in blueprint.nodes:
        if node.narrative_layer != "story":
            continue

        owned_sources = set(node.source_segment_ids)
        participant_keys = set(node.participants)
        environment_keys = set(node.environment_source_unit_keys)
        claims_by_unit: defaultdict[
            str, list[NarrativeParticipantEvidence]
        ] = defaultdict(list)
        assignments_by_unit: defaultdict[
            str, list[NarrativeStateSubjectAssignment]
        ] = defaultdict(list)
        ordered_unit_keys: list[str] = []

        def owned_source_for_unit(source_unit_key: str) -> str:
            source_segment_id, marker, unit_id = source_unit_key.partition(
                ":unit:"
            )
            if (
                not marker
                or not source_segment_id
                or not unit_id
                or ":" in unit_id
                or source_segment_id not in owned_sources
            ):
                return ""
            return source_segment_id

        for evidence in node.participant_evidence:
            if (
                evidence.usage != "state_subject"
                or not evidence.source_unit_keys
                or evidence.identity_key not in participant_keys
                or set(evidence.source_segment_ids) - owned_sources
            ):
                continue
            evidence_sources = set(evidence.source_segment_ids)
            for source_unit_key in evidence.source_unit_keys:
                source_segment_id = owned_source_for_unit(source_unit_key)
                if (
                    not source_segment_id
                    or source_segment_id not in evidence_sources
                ):
                    continue
                claims_by_unit[source_unit_key].append(evidence)
                if source_unit_key not in ordered_unit_keys:
                    ordered_unit_keys.append(source_unit_key)

        for assignment in node.state_subject_assignments:
            source_unit_key = assignment.source_unit_key
            if (
                not owned_source_for_unit(source_unit_key)
                or set(assignment.identity_keys) - participant_keys
            ):
                continue
            assignments_by_unit[source_unit_key].append(assignment)
            if source_unit_key not in ordered_unit_keys:
                ordered_unit_keys.append(source_unit_key)

        missing_by_identity_source: dict[
            tuple[str, str], list[str]
        ] = {}
        for source_unit_key in ordered_unit_keys:
            claims = claims_by_unit[source_unit_key]
            assignments = assignments_by_unit[source_unit_key]
            if source_unit_key in environment_keys:
                continue
            if len(claims) == 1 and not assignments:
                identity_keys = [claims[0].identity_key]
            elif len(assignments) == 1 and not claims:
                identity_keys = list(assignments[0].identity_keys)
            else:
                continue
            source_segment_id = owned_source_for_unit(source_unit_key)
            for identity_key in identity_keys:
                if _node_identity_has_perception_evidence(
                    node,
                    identity_key=identity_key,
                    source_unit_key=source_unit_key,
                ):
                    continue
                aggregate_key = (identity_key, source_segment_id)
                missing_by_identity_source.setdefault(
                    aggregate_key, []
                ).append(source_unit_key)

        for (
            identity_key,
            source_segment_id,
        ), source_unit_keys in missing_by_identity_source.items():
            node.participant_evidence.append(NarrativeParticipantEvidence(
                identity_key=identity_key,
                source_segment_ids=[source_segment_id],
                source_unit_keys=list(dict.fromkeys(source_unit_keys)),
                usage="visible",
            ))
            added += 1
    return added


def blueprint_candidate_hash(
    candidate: NarrativeBlueprint | dict[str, Any],
) -> str:
    """Hash one complete Blueprint for an atomic ownership-only patch."""
    blueprint = (
        candidate
        if isinstance(candidate, NarrativeBlueprint)
        else NarrativeBlueprint.model_validate(candidate)
    )
    return hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def blueprint_shard_candidate_hash(
    candidate: NarrativeBlueprintShard | dict[str, Any],
) -> str:
    """Hash one normalized shard candidate for an atomic ownership patch."""
    shard = (
        candidate
        if isinstance(candidate, NarrativeBlueprintShard)
        else NarrativeBlueprintShard.model_validate(candidate)
    )
    return hashlib.sha256(
        json.dumps(
            shard.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
