"""Builds the state-subject misclassification repair contract/schema and applies a misclassification patch."""
from __future__ import annotations

from typing import Any

from app.source_facts import SourceFact, source_facts

from .models_core import NarrativeBlueprint, NarrativeParticipantEvidence, NarrativeStateSubjectAssignment
from .models_patch import BlueprintStateSubjectOwnershipPatch
from .state_subject_perception import _node_state_subject_repairable_identities, blueprint_candidate_hash


def _blueprint_state_subject_misclassification_contract(
    candidate: NarrativeBlueprint | dict[str, Any],
    target_unit_keys: list[str],
    source_text: str,
) -> tuple[
    NarrativeBlueprint,
    dict[str, SourceFact],
    dict[str, int],
    dict[str, list[str]],
]:
    """Resolve exact targets without inferring identities or source meaning."""
    blueprint = (
        candidate
        if isinstance(candidate, NarrativeBlueprint)
        else NarrativeBlueprint.model_validate(candidate)
    )
    targets = [str(key or "").strip() for key in target_unit_keys]
    if (
        not targets
        or any(not key for key in targets)
        or len(targets) != len(set(targets))
    ):
        raise ValueError(
            "misclassified environment targets 必须非空且唯一"
        )

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
                "misclassified environment target 必须是 canonical action "
                f"unit：{unit_key}"
            )
        owners = [
            index
            for index, node in enumerate(blueprint.nodes)
            if fact.source_segment_id in node.source_segment_ids
        ]
        if len(owners) != 1:
            raise ValueError(
                "misclassified environment target 必须有唯一 SRC owner："
                f"{unit_key}"
            )
        owner_index = owners[0]
        owner = blueprint.nodes[owner_index]
        if unit_key not in owner.environment_source_unit_keys:
            raise ValueError(
                "misclassified environment target 当前必须由 owner 的 "
                f"environment_source_unit_keys 拥有：{unit_key}"
            )
        if unit_key in owner.state_subject_adjudicated_unit_keys:
            raise ValueError(
                f"misclassified environment target 已完成 adjudication：{unit_key}"
            )
        identities = _node_state_subject_repairable_identities(
            owner,
            source_unit_key=unit_key,
        )
        if not identities:
            raise ValueError(
                "misclassified environment target 没有 existing participant "
                f"visible/voice authority：{unit_key}"
            )
        target_facts[unit_key] = fact
        owner_indexes[unit_key] = owner_index
        allowed_identities[unit_key] = identities
    return (
        blueprint,
        target_facts,
        owner_indexes,
        allowed_identities,
    )


def blueprint_state_subject_misclassification_patch_schema(
    candidate: NarrativeBlueprint | dict[str, Any],
    target_unit_keys: list[str],
    source_text: str,
) -> dict[str, Any]:
    """Build the exact single/joint repair schema for reviewed environment units."""
    (
        blueprint,
        _target_facts,
        _owner_indexes,
        allowed_identities,
    ) = _blueprint_state_subject_misclassification_contract(
        candidate,
        target_unit_keys,
        source_text,
    )
    targets = [str(key or "").strip() for key in target_unit_keys]
    definitions: dict[str, Any] = {}
    definition_by_identities: dict[tuple[str, ...], str] = {}
    repair_properties: dict[str, Any] = {}
    for unit_key in targets:
        identities = tuple(allowed_identities[unit_key])
        definition_name = definition_by_identities.get(identities)
        if definition_name is None:
            definition_name = f"r{len(definition_by_identities)}"
            definition_by_identities[identities] = definition_name
            identity_items = {"enum": list(identities)}
            options: list[dict[str, Any]] = [{
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
            }]
            if len(identities) >= 2:
                options.append({
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mode": {"const": "joint"},
                        "identity_keys": {
                            "type": "array",
                            "minItems": 2,
                            "uniqueItems": True,
                            "items": identity_items,
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
                "const": blueprint_candidate_hash(blueprint),
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


def _blueprint_non_ownership_projection(
    blueprint: NarrativeBlueprint,
) -> dict[str, Any]:
    projection = blueprint.model_dump(mode="json")
    for node in projection["nodes"]:
        node["participant_evidence"] = [
            evidence
            for evidence in node["participant_evidence"]
            if evidence["usage"] != "state_subject"
        ]
        node.pop("state_subject_assignments")
        node.pop("environment_source_unit_keys")
    return projection


def _blueprint_non_target_ownership_projection(
    blueprint: NarrativeBlueprint,
    target_unit_keys: set[str],
) -> list[dict[str, Any]]:
    projection: list[dict[str, Any]] = []
    for node in blueprint.nodes:
        evidence_projection: list[dict[str, Any]] = []
        for evidence in node.participant_evidence:
            if evidence.usage != "state_subject":
                continue
            had_target = bool(
                set(evidence.source_unit_keys) & target_unit_keys
            )
            retained_keys = [
                unit_key
                for unit_key in evidence.source_unit_keys
                if unit_key not in target_unit_keys
            ]
            if had_target and not retained_keys:
                continue
            value = evidence.model_dump(mode="json")
            value["source_unit_keys"] = retained_keys
            evidence_projection.append(value)
        projection.append({
            "node_key": node.key,
            "participant_evidence": evidence_projection,
            "state_subject_assignments": [
                assignment.model_dump(mode="json")
                for assignment in node.state_subject_assignments
                if assignment.source_unit_key not in target_unit_keys
            ],
            "environment_source_unit_keys": [
                unit_key
                for unit_key in node.environment_source_unit_keys
                if unit_key not in target_unit_keys
            ],
        })
    return projection


def apply_blueprint_state_subject_misclassification_patch(
    previous_candidate: NarrativeBlueprint | dict[str, Any],
    patch: BlueprintStateSubjectOwnershipPatch | dict[str, Any],
    *,
    target_unit_keys: list[str],
    source_text: str,
) -> NarrativeBlueprint:
    """Replace reviewed environment ownership and freeze every other field."""
    patch_value = (
        patch
        if isinstance(patch, BlueprintStateSubjectOwnershipPatch)
        else BlueprintStateSubjectOwnershipPatch.model_validate(patch)
    )
    expected_hash = blueprint_candidate_hash(previous_candidate)
    if patch_value.base_candidate_hash != expected_hash:
        raise ValueError(
            "misclassified environment patch base_candidate_hash 漂移"
        )
    (
        previous,
        target_facts,
        owner_indexes,
        allowed_identities,
    ) = _blueprint_state_subject_misclassification_contract(
        previous_candidate,
        target_unit_keys,
        source_text,
    )
    targets = [str(key or "").strip() for key in target_unit_keys]
    if set(patch_value.repairs) != set(targets):
        raise ValueError(
            "misclassified environment patch target 集合必须完全相等"
        )
    for unit_key in targets:
        repair = patch_value.repairs[unit_key]
        if repair.mode not in {"single", "joint"}:
            raise ValueError(
                "misclassified environment patch 只允许 single/joint，"
                "不允许 environment"
            )
        invalid_identities = (
            set(repair.identity_keys) - set(allowed_identities[unit_key])
        )
        if invalid_identities:
            raise ValueError(
                "misclassified environment patch identity 缺少 existing "
                f"visible/voice authority：{unit_key}"
            )

    target_set = set(targets)
    before_non_ownership = _blueprint_non_ownership_projection(previous)
    before_non_target_ownership = (
        _blueprint_non_target_ownership_projection(previous, target_set)
    )
    candidate = previous.model_copy(deep=True)
    for node in candidate.nodes:
        retained_evidence: list[NarrativeParticipantEvidence] = []
        for evidence in node.participant_evidence:
            if (
                evidence.usage != "state_subject"
                or not target_set.intersection(evidence.source_unit_keys)
            ):
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
            owner.participant_evidence.append(
                NarrativeParticipantEvidence(
                    identity_key=repair.identity_keys[0],
                    source_segment_ids=[
                        target_facts[unit_key].source_segment_id
                    ],
                    source_unit_keys=[unit_key],
                    usage="state_subject",
                )
            )
        else:
            owner.state_subject_assignments.append(
                NarrativeStateSubjectAssignment(
                    source_unit_key=unit_key,
                    mode="joint",
                    identity_keys=list(repair.identity_keys),
                )
            )

    repaired = NarrativeBlueprint.model_validate(
        candidate.model_dump(mode="json")
    )
    if _blueprint_non_ownership_projection(
        repaired
    ) != before_non_ownership:
        raise RuntimeError(
            "misclassified environment patch changed non-ownership fields"
        )
    if _blueprint_non_target_ownership_projection(
        repaired,
        target_set,
    ) != before_non_target_ownership:
        raise RuntimeError(
            "misclassified environment patch changed non-target ownership"
        )
    return repaired
