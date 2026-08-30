"""Builds the state-subject ownership repair contract/schema and applies an ownership patch."""
from __future__ import annotations

from typing import Any

from app.source_facts import SourceFact, source_facts

from .models_core import (
    NarrativeBlueprint,
    NarrativeBlueprintShard,
    NarrativeParticipantEvidence,
    NarrativeStateSubjectAssignment,
)
from .models_patch import BlueprintStateSubjectOwnershipPatch
from .state_subject_misclassification_patch import (
    apply_blueprint_state_subject_misclassification_patch,
    blueprint_state_subject_misclassification_patch_schema,
)
from .state_subject_perception import _node_identity_has_perception_evidence, blueprint_shard_candidate_hash


def _blueprint_state_subject_repair_contract(
    candidate: NarrativeBlueprintShard | dict[str, Any],
    target_unit_keys: list[str],
    source_text: str,
) -> tuple[
    NarrativeBlueprintShard,
    dict[str, SourceFact],
    dict[str, int],
    dict[str, list[str]],
]:
    shard = (
        candidate
        if isinstance(candidate, NarrativeBlueprintShard)
        else NarrativeBlueprintShard.model_validate(candidate)
    )
    targets = [str(key or "").strip() for key in target_unit_keys]
    if (
        not targets
        or any(not key for key in targets)
        or len(targets) != len(set(targets))
    ):
        raise ValueError("ownership repair targets 必须非空且唯一")

    facts_by_key = {
        fact.source_unit_key: fact
        for fact in source_facts(source_text)
    }
    target_facts: dict[str, SourceFact] = {}
    owner_indexes: dict[str, int] = {}
    allowed_identities: dict[str, list[str]] = {}
    for unit_key in targets:
        fact = facts_by_key.get(unit_key)
        if fact is None or fact.projection != "action":
            raise ValueError(
                f"ownership repair target 必须是 action unit：{unit_key}"
            )
        owners = [
            index
            for index, node in enumerate(shard.nodes)
            if fact.source_segment_id in node.source_segment_ids
        ]
        if len(owners) != 1:
            raise ValueError(
                f"ownership repair target 必须有唯一 SRC owner：{unit_key}"
            )
        owner_index = owners[0]
        identities = list(dict.fromkeys(
            identity_key.strip()
            for identity_key in shard.nodes[owner_index].participants
            if identity_key.strip()
        ))
        target_facts[unit_key] = fact
        owner_indexes[unit_key] = owner_index
        allowed_identities[unit_key] = identities
    return shard, target_facts, owner_indexes, allowed_identities


def blueprint_state_subject_ownership_patch_schema(
    candidate: (
        NarrativeBlueprint
        | NarrativeBlueprintShard
        | dict[str, Any]
    ),
    target_unit_keys: list[str],
    source_text: str,
) -> dict[str, Any]:
    """Build a compact exact-key schema for one ownership repair attempt."""
    if (
        isinstance(candidate, NarrativeBlueprint)
        or (
            isinstance(candidate, dict)
            and "shard_index" not in candidate
        )
    ):
        return blueprint_state_subject_misclassification_patch_schema(
            candidate,
            target_unit_keys,
            source_text,
        )
    (
        shard,
        _target_facts,
        _owner_indexes,
        allowed_identities,
    ) = _blueprint_state_subject_repair_contract(
        candidate,
        target_unit_keys,
        source_text,
    )
    targets = list(target_unit_keys)
    definitions: dict[str, Any] = {}
    definition_by_identities: dict[tuple[str, ...], str] = {}
    repair_properties: dict[str, Any] = {}
    for unit_key in targets:
        identities = tuple(allowed_identities[unit_key])
        definition_name = definition_by_identities.get(identities)
        if definition_name is None:
            definition_name = f"r{len(definition_by_identities)}"
            definition_by_identities[identities] = definition_name
            options: list[dict[str, Any]] = [{
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "mode": {"const": "environment"},
                    "identity_keys": {
                        "type": "array",
                        "maxItems": 0,
                    },
                },
                "required": ["mode", "identity_keys"],
            }]
            if identities:
                identity_items = {"enum": list(identities)}
                options.insert(0, {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mode": {"const": "single"},
                        "identity_keys": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 1,
                            "uniqueItems": True,
                            "items": identity_items,
                        },
                    },
                    "required": ["mode", "identity_keys"],
                })
            if len(identities) >= 2:
                options.insert(1, {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mode": {"const": "joint"},
                        "identity_keys": {
                            "type": "array",
                            "minItems": 2,
                            "uniqueItems": True,
                            "items": {"enum": list(identities)},
                        },
                    },
                    "required": ["mode", "identity_keys"],
                })
            definitions[definition_name] = {"oneOf": options}
        repair_properties[unit_key] = {
            "$ref": f"#/$defs/{definition_name}",
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "base_candidate_hash": {
                "const": blueprint_shard_candidate_hash(shard),
            },
            "repairs": {
                "type": "object",
                "additionalProperties": False,
                "properties": repair_properties,
                "required": targets,
            },
        },
        "required": ["base_candidate_hash", "repairs"],
        "$defs": definitions,
    }


def apply_blueprint_state_subject_ownership_patch(
    previous_candidate: (
        NarrativeBlueprint
        | NarrativeBlueprintShard
        | dict[str, Any]
    ),
    patch: BlueprintStateSubjectOwnershipPatch | dict[str, Any],
    *,
    target_unit_keys: list[str],
    source_text: str,
) -> NarrativeBlueprint | NarrativeBlueprintShard:
    """Apply one exact ownership map without exposing any other shard fields."""
    if (
        isinstance(previous_candidate, NarrativeBlueprint)
        or (
            isinstance(previous_candidate, dict)
            and "shard_index" not in previous_candidate
        )
    ):
        return apply_blueprint_state_subject_misclassification_patch(
            previous_candidate,
            patch,
            target_unit_keys=target_unit_keys,
            source_text=source_text,
        )
    patch_value = (
        patch
        if isinstance(patch, BlueprintStateSubjectOwnershipPatch)
        else BlueprintStateSubjectOwnershipPatch.model_validate(patch)
    )
    expected_hash = blueprint_shard_candidate_hash(previous_candidate)
    if patch_value.base_candidate_hash != expected_hash:
        raise ValueError("ownership repair base_candidate_hash 漂移")
    targets = list(target_unit_keys)
    if set(patch_value.repairs) != set(targets) or (
        len(targets) != len(set(targets))
    ):
        raise ValueError("ownership repair target 集合必须完全相等")

    (
        previous,
        target_facts,
        owner_indexes,
        allowed_identities,
    ) = _blueprint_state_subject_repair_contract(
        previous_candidate,
        targets,
        source_text,
    )
    for unit_key in targets:
        repair = patch_value.repairs[unit_key]
        invalid_identities = (
            set(repair.identity_keys) - set(allowed_identities[unit_key])
        )
        if invalid_identities:
            raise ValueError(
                f"ownership repair identity 不在 owner participants：{unit_key}"
            )

    candidate = previous.model_copy(deep=True)
    target_set = set(targets)
    for node in candidate.nodes:
        retained_evidence: list[NarrativeParticipantEvidence] = []
        for evidence in node.participant_evidence:
            if evidence.usage != "state_subject":
                retained_evidence.append(evidence)
                continue
            retained_keys = [
                unit_key
                for unit_key in evidence.source_unit_keys
                if unit_key not in target_set
            ]
            if retained_keys:
                retained = evidence.model_copy(deep=True)
                retained.source_unit_keys = retained_keys
                retained_evidence.append(retained)
        node.participant_evidence = retained_evidence
        node.state_subject_assignments = [
            assignment
            for assignment in node.state_subject_assignments
            if assignment.source_unit_key not in target_set
        ]
        node.environment_source_unit_keys = [
            unit_key
            for unit_key in node.environment_source_unit_keys
            if unit_key not in target_set
        ]

    for unit_key in targets:
        repair = patch_value.repairs[unit_key]
        owner = candidate.nodes[owner_indexes[unit_key]]
        if repair.mode == "single":
            identity_key = repair.identity_keys[0]
            owner.participant_evidence.append(NarrativeParticipantEvidence(
                identity_key=identity_key,
                source_segment_ids=[
                    target_facts[unit_key].source_segment_id
                ],
                source_unit_keys=[unit_key],
                usage="state_subject",
            ))
            if not _node_identity_has_perception_evidence(
                owner,
                identity_key=identity_key,
                source_unit_key=unit_key,
            ):
                owner.participant_evidence.append(
                    NarrativeParticipantEvidence(
                        identity_key=identity_key,
                        source_segment_ids=[
                            target_facts[unit_key].source_segment_id
                        ],
                        source_unit_keys=[unit_key],
                        usage="visible",
                    )
                )
        elif repair.mode == "joint":
            owner.state_subject_assignments.append(
                NarrativeStateSubjectAssignment(
                    source_unit_key=unit_key,
                    mode="joint",
                    identity_keys=list(repair.identity_keys),
                )
            )
            for identity_key in repair.identity_keys:
                if _node_identity_has_perception_evidence(
                    owner,
                    identity_key=identity_key,
                    source_unit_key=unit_key,
                ):
                    continue
                owner.participant_evidence.append(
                    NarrativeParticipantEvidence(
                        identity_key=identity_key,
                        source_segment_ids=[
                            target_facts[unit_key].source_segment_id
                        ],
                        source_unit_keys=[unit_key],
                        usage="visible",
                    )
                )
        else:
            owner.environment_source_unit_keys.append(unit_key)

    for node in candidate.nodes:
        authority_order = list(dict.fromkeys([
            evidence.identity_key
            for evidence in node.participant_evidence
            if evidence.identity_key.strip()
        ] + [
            identity_key
            for assignment in node.state_subject_assignments
            for identity_key in assignment.identity_keys
            if identity_key.strip()
        ]))
        authority_set = set(authority_order)
        node.participants = [
            identity_key
            for identity_key in node.participants
            if identity_key in authority_set
        ]
        node.participants.extend(
            identity_key
            for identity_key in authority_order
            if identity_key not in node.participants
        )

    return NarrativeBlueprintShard.model_validate(
        candidate.model_dump(mode="json")
    )
