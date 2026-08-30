"""叙事蓝图分片——未报告台词对/状态主体归属的冻结固化。"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import re
from typing import Any


from app.narrative_blueprint import (
    AUDIBLE_SOURCE_DELIVERY_MODES,
    NarrativeBlueprintShard,
)


_BLUEPRINT_SOURCE_UNIT_KEY_PATTERN = re.compile(r"\bSRC\d+:unit:\d+\b")


def _freeze_unreported_voice_pairs(
    candidate_payload: dict[str, Any],
    *,
    previous_candidate: dict[str, Any],
    validation_errors: list[str],
) -> dict[str, Any]:
    """Restore only unchanged, valid audible pairs omitted by a retry."""

    candidate = deepcopy(candidate_payload)
    mutable_unit_keys = {
        unit_key
        for error in validation_errors
        for unit_key in _BLUEPRINT_SOURCE_UNIT_KEY_PATTERN.findall(error)
    }

    def node_index(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        indexed: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        nodes = payload.get("nodes")
        if not isinstance(nodes, list):
            return indexed
        for node in nodes:
            if not isinstance(node, dict):
                continue
            key = node.get("key")
            if isinstance(key, str):
                indexed[key].append(node)
        return indexed

    previous_nodes = node_index(previous_candidate)
    candidate_nodes = node_index(candidate)
    for node_key, previous_matches in previous_nodes.items():
        candidate_matches = candidate_nodes.get(node_key, [])
        if len(previous_matches) != 1 or len(candidate_matches) != 1:
            continue
        previous_node = previous_matches[0]
        candidate_node = candidate_matches[0]
        previous_source_ids = previous_node.get("source_segment_ids")
        candidate_source_ids = candidate_node.get("source_segment_ids")
        if (
            not isinstance(previous_source_ids, list)
            or not isinstance(candidate_source_ids, list)
            or candidate_source_ids != previous_source_ids
        ):
            continue

        previous_deliveries = previous_node.get(
            "source_unit_deliveries",
            [],
        )
        previous_evidence = previous_node.get("participant_evidence", [])
        candidate_deliveries = candidate_node.get(
            "source_unit_deliveries",
            [],
        )
        candidate_evidence = candidate_node.get("participant_evidence", [])
        if not all(
            isinstance(value, list)
            for value in (
                previous_deliveries,
                previous_evidence,
                candidate_deliveries,
                candidate_evidence,
            )
        ):
            continue

        def deliveries_by_unit(
            rows: list[Any],
        ) -> defaultdict[str, list[dict[str, Any]]]:
            indexed: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                unit_key = row.get("source_unit_key")
                if isinstance(unit_key, str):
                    indexed[unit_key].append(row)
            return indexed

        def voice_claims_by_unit(
            rows: list[Any],
        ) -> defaultdict[str, list[dict[str, Any]]]:
            indexed: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                if not isinstance(row, dict) or row.get("usage") != "voice":
                    continue
                unit_keys = row.get("source_unit_keys")
                if not isinstance(unit_keys, list):
                    continue
                for unit_key in unit_keys:
                    if isinstance(unit_key, str):
                        indexed[unit_key].append(row)
            return indexed

        previous_delivery_by_unit = deliveries_by_unit(previous_deliveries)
        previous_claims_by_unit = voice_claims_by_unit(previous_evidence)
        candidate_delivery_by_unit = deliveries_by_unit(candidate_deliveries)
        candidate_claims_by_unit = voice_claims_by_unit(candidate_evidence)
        for unit_key, unit_deliveries in previous_delivery_by_unit.items():
            if (
                unit_key in mutable_unit_keys
                or _BLUEPRINT_SOURCE_UNIT_KEY_PATTERN.fullmatch(unit_key) is None
                or len(unit_deliveries) != 1
            ):
                continue
            previous_delivery = unit_deliveries[0]
            performer_key = previous_delivery.get("performer_key")
            if (
                previous_delivery.get("mode")
                not in AUDIBLE_SOURCE_DELIVERY_MODES
                or not isinstance(performer_key, str)
                or not performer_key.strip()
            ):
                continue
            previous_claims = previous_claims_by_unit.get(unit_key, [])
            if len(previous_claims) != 1:
                continue
            previous_claim = previous_claims[0]
            unit_source_id = unit_key.split(":unit:", 1)[0]
            evidence_source_ids = previous_claim.get("source_segment_ids")
            if (
                previous_claim.get("identity_key") != performer_key
                or not isinstance(evidence_source_ids, list)
                or unit_source_id not in evidence_source_ids
            ):
                continue

            unit_candidate_deliveries = candidate_delivery_by_unit.get(
                unit_key,
                [],
            )
            unit_candidate_claims = candidate_claims_by_unit.get(unit_key, [])
            if unit_candidate_claims or len(unit_candidate_deliveries) > 1:
                continue
            restored_claim = deepcopy(previous_claim)
            restored_claim["source_unit_keys"] = [unit_key]
            if unit_candidate_deliveries:
                if unit_candidate_deliveries[0] != previous_delivery:
                    continue
                candidate_evidence.append(restored_claim)
                continue
            candidate_deliveries.append(deepcopy(previous_delivery))
            candidate_evidence.append(restored_claim)
    return candidate


def _freeze_unreported_state_subject_ownership(
    candidate: NarrativeBlueprintShard,
    *,
    previous_candidate: dict[str, Any],
    validation_errors: list[str],
) -> None:
    """Keep retry ownership changes local to units named by validation."""

    previous = NarrativeBlueprintShard.model_validate(previous_candidate)
    mutable_unit_keys = {
        unit_key
        for error in validation_errors
        for unit_key in _BLUEPRINT_SOURCE_UNIT_KEY_PATTERN.findall(error)
    }
    candidate_nodes = {node.key: node for node in candidate.nodes}
    previous_nodes = {node.key: node for node in previous.nodes}

    def ownership_keys(node: Any) -> set[str]:
        return {
            unit_key
            for evidence in node.participant_evidence
            if evidence.usage == "state_subject"
            for unit_key in evidence.source_unit_keys
        } | {
            assignment.source_unit_key
            for assignment in node.state_subject_assignments
        } | set(node.environment_source_unit_keys)

    previous_owner_by_unit = {
        unit_key: node.key
        for node in previous.nodes
        for unit_key in ownership_keys(node)
    }
    candidate_owned_keys = {
        unit_key
        for node in candidate.nodes
        for unit_key in ownership_keys(node)
    }
    frozen_unit_keys = {
        unit_key
        for unit_key in candidate_owned_keys | set(previous_owner_by_unit)
        if (
            unit_key not in mutable_unit_keys
            and (
                unit_key not in previous_owner_by_unit
                or previous_owner_by_unit[unit_key] in candidate_nodes
            )
        )
    }

    for node in candidate.nodes:
        retained_evidence = []
        for evidence in node.participant_evidence:
            if evidence.usage != "state_subject":
                retained_evidence.append(evidence)
                continue
            retained_keys = [
                unit_key
                for unit_key in evidence.source_unit_keys
                if unit_key not in frozen_unit_keys
            ]
            if retained_keys:
                evidence.source_unit_keys = retained_keys
                retained_evidence.append(evidence)
        node.participant_evidence = retained_evidence
        node.state_subject_assignments = [
            assignment
            for assignment in node.state_subject_assignments
            if assignment.source_unit_key not in frozen_unit_keys
        ]
        node.environment_source_unit_keys = [
            unit_key
            for unit_key in node.environment_source_unit_keys
            if unit_key not in frozen_unit_keys
        ]

    for node_key, previous_node in previous_nodes.items():
        node = candidate_nodes.get(node_key)
        if node is None:
            continue
        for evidence in previous_node.participant_evidence:
            if evidence.usage != "state_subject":
                continue
            retained_keys = [
                unit_key
                for unit_key in evidence.source_unit_keys
                if unit_key in frozen_unit_keys
            ]
            if retained_keys:
                restored = deepcopy(evidence)
                restored.source_unit_keys = retained_keys
                node.participant_evidence.append(restored)
        node.state_subject_assignments.extend(
            deepcopy(assignment)
            for assignment in previous_node.state_subject_assignments
            if assignment.source_unit_key in frozen_unit_keys
        )
        node.environment_source_unit_keys.extend(
            unit_key
            for unit_key in previous_node.environment_source_unit_keys
            if unit_key in frozen_unit_keys
        )

    for node in candidate.nodes:
        node.participants = list(dict.fromkeys(
            [
                evidence.identity_key
                for evidence in node.participant_evidence
                if evidence.identity_key.strip()
            ]
            + [
                identity_key
                for assignment in node.state_subject_assignments
                for identity_key in assignment.identity_keys
                if identity_key.strip()
            ]
        ))
