"""Builds the semantic-review and patch provider schemas and normalizes a semantic-review payload (including fact-version and requirement-state-key normalization)."""
from __future__ import annotations

import json
from typing import Any

from .models_core import BlueprintStateChange, NarrativeBlueprint, NarrativeNode
from .models_patch import BlueprintSemanticReview, NarrativeBlueprintPatch
from .provider_normalize import _PARATEXT_EMPTY_LIST_FIELDS, _normalize_source_segment_id


def blueprint_semantic_review_schema(
    canonical_node_keys: list[str],
    canonical_source_segment_ids: list[str] | None = None,
    canonical_source_unit_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Bind reviewer references to one ordered Blueprint projection."""
    identities = [str(key).strip() for key in canonical_node_keys]
    if (
        not identities
        or any(not key for key in identities)
        or len(identities) != len(set(identities))
    ):
        raise ValueError("canonical node identities must be non-empty and unique")

    schema = BlueprintSemanticReview.model_json_schema()
    schema["x-canonical-timeline-node-keys"] = identities
    issue_schema = schema["$defs"]["BlueprintSemanticIssue"]
    issue_schema["properties"]["node_keys"] = {
        "title": "Canonical Node References",
        "type": "array",
        "minItems": 1,
        "items": {
            "oneOf": [
                {
                    "type": "string",
                    "enum": identities,
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "ordinal": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": len(identities),
                        },
                    },
                    "required": ["ordinal"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "identity": {
                            "type": "string",
                            "enum": identities,
                        },
                    },
                    "required": ["identity"],
                },
            ],
        },
    }
    if canonical_source_segment_ids is not None:
        source_ids = [
            _normalize_source_segment_id(source_id)
            for source_id in canonical_source_segment_ids
        ]
        if (
            any(not source_id for source_id in source_ids)
            or len(source_ids) != len(set(source_ids))
        ):
            raise ValueError(
                "canonical source segment identities must be non-empty "
                "and unique"
            )
        issue_schema["properties"]["source_segment_ids"] = {
            "title": "Canonical Source Segment References",
            "type": "array",
            "items": {
                "type": "string",
                "enum": source_ids,
            },
        }
    if canonical_source_unit_keys is not None:
        source_unit_keys = [
            str(source_unit_key or "").strip()
            for source_unit_key in canonical_source_unit_keys
        ]
        if (
            any(not source_unit_key for source_unit_key in source_unit_keys)
            or len(source_unit_keys) != len(set(source_unit_keys))
        ):
            raise ValueError(
                "canonical source unit identities must be non-empty "
                "and unique"
            )
        issue_schema["properties"]["source_unit_keys"] = {
            "title": "Canonical Source Unit References",
            "type": "array",
            "uniqueItems": True,
            "items": {
                "type": "string",
                "enum": source_unit_keys,
            },
        }
        schema["x-canonical-source-unit-keys"] = source_unit_keys
    issue_schema.setdefault("allOf", []).append({
        "if": {
            "properties": {
                "code": {
                    "const": "state_subject_environment_misclassified",
                },
            },
            "required": ["code"],
        },
        "then": {
            "properties": {
                "node_keys": {
                    "minItems": 1,
                    "maxItems": 1,
                },
                "source_segment_ids": {
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "source_unit_keys": {
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
            "required": [
                "node_keys",
                "source_segment_ids",
                "source_unit_keys",
            ],
        },
    })
    return schema


def blueprint_patch_schema(
    blueprint: NarrativeBlueprint,
    replaceable_node_keys: list[str],
) -> dict[str, Any]:
    """Bind each replacement to its current projection authority."""
    node_map = {node.key: node for node in blueprint.nodes}
    keys = [str(key).strip() for key in replaceable_node_keys]
    if (
        not keys
        or any(not key or key not in node_map for key in keys)
        or len(keys) != len(set(keys))
    ):
        raise ValueError(
            "replaceable node identities must exist and be unique"
        )

    schema = NarrativeBlueprintPatch.model_json_schema()
    canonical_node_keys = [node.key for node in blueprint.nodes]
    schema["x-canonical-timeline-node-keys"] = canonical_node_keys
    alternatives: list[dict[str, Any]] = []
    for key in keys:
        node = node_map[key]
        semantics = node.source_semantics()
        node_contract = {
            "type": "object",
            "properties": {
                "key": {"const": key},
                "source_segment_ids": {
                    "const": list(node.source_segment_ids),
                },
                "narrative_layer": {
                    "const": semantics.narrative_layer,
                },
                "event_priority": {
                    "const": semantics.event_priority,
                },
                "render_policy": {
                    "const": semantics.render_policy,
                },
            },
            "required": list(NarrativeNode.model_fields),
        }
        if semantics.narrative_layer == "paratext":
            node_contract["properties"].update({
                field_name: {"const": []}
                for field_name in _PARATEXT_EMPTY_LIST_FIELDS
            })
            node_contract["properties"].update({
                "decision": {"const": None},
                "exit_state": {"const": ""},
            })
        alternatives.append({
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "node_key": {"const": key},
                "node": {
                    "allOf": [
                        {"$ref": "#/$defs/NarrativeNode"},
                        node_contract,
                    ],
                },
            },
            "required": ["node_key", "node"],
        })
    schema["properties"]["replacements"]["items"] = {
        "oneOf": alternatives,
    }
    schema["properties"]["delete_node_keys"] = {
        "type": "array",
        "maxItems": 0,
    }
    return schema


def normalize_blueprint_semantic_review_payload(
    payload: dict[str, Any],
    canonical_node_keys: list[str],
) -> dict[str, Any]:
    """Resolve only exact identity or one-based ordinal node references."""
    identities = tuple(str(key).strip() for key in canonical_node_keys)
    identity_set = set(identities)
    normalized = dict(payload)
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return normalized

    normalized_issues: list[Any] = []
    for issue in issues:
        if not isinstance(issue, dict):
            normalized_issues.append(issue)
            continue
        normalized_issue = dict(issue)
        references = issue.get("node_keys")
        if not isinstance(references, list):
            normalized_issues.append(normalized_issue)
            continue

        normalized_references: list[str] = []
        for reference in references:
            resolved: str | None = None
            if isinstance(reference, str):
                resolved = reference
            elif isinstance(reference, dict):
                if set(reference) == {"identity"}:
                    identity = reference.get("identity")
                    if isinstance(identity, str) and identity in identity_set:
                        resolved = identity
                elif set(reference) == {"ordinal"}:
                    ordinal = reference.get("ordinal")
                    if (
                        isinstance(ordinal, int)
                        and not isinstance(ordinal, bool)
                        and 1 <= ordinal <= len(identities)
                    ):
                        resolved = identities[ordinal - 1]
            if resolved is None:
                resolved = (
                    "[INVALID_BLUEPRINT_NODE_REFERENCE]"
                    + json.dumps(
                        reference,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            normalized_references.append(resolved)
        normalized_issue["node_keys"] = normalized_references
        normalized_issues.append(normalized_issue)

    normalized["issues"] = normalized_issues
    return normalized


def normalize_blueprint_fact_versions(
    blueprint: NarrativeBlueprint,
) -> int:
    """Convert repeated authored fact handles into deterministic SSA keys."""
    latest_versions: dict[str, str] = {}
    used_keys: set[str] = set()
    changes = 0
    for node in blueprint.nodes:
        for requirement in node.state_requirements:
            requirement.required_fact_key = latest_versions.get(
                requirement.required_fact_key,
                requirement.required_fact_key,
            )
        for change_index, change in enumerate(
            node.state_changes,
            start=1,
        ):
            original_key = change.fact_key
            change.supersedes_fact_keys = [
                latest_versions.get(fact_key, fact_key)
                for fact_key in change.supersedes_fact_keys
            ]
            if original_key in used_keys:
                versioned_key = (
                    f"{original_key}--{node.key}-{change_index}"
                )
                while versioned_key in used_keys:
                    versioned_key += "x"
                change.fact_key = versioned_key
                changes += 1
            used_keys.add(change.fact_key)
            latest_versions[original_key] = change.fact_key
        if node.decision is not None:
            node.decision.constraint_fact_key = latest_versions.get(
                node.decision.constraint_fact_key,
                node.decision.constraint_fact_key,
            )
    return changes


def normalize_blueprint_requirement_state_keys(
    blueprint: NarrativeBlueprint,
) -> int:
    """Converge requirement.state_key onto the referenced fact's authoritative state_key.

    ``requirement.state_key`` is a free-text label authored independently of the
    ``BlueprintStateChange`` it references, whereas the authoritative state_key is
    the one carried by that referenced fact.  When the two texts disagree the
    UNESTABLISHED / KEY_MISMATCH / SUPERSEDED gates can never all close because
    there is no deterministic way to reconcile two free-text labels.  This pass
    removes the second authority: for every requirement that resolves to a known
    fact it rewrites ``requirement.state_key`` to the fact's ``state_key`` so all
    three gates key off the single authoritative source.

    Must run after ``normalize_blueprint_fact_versions`` so that
    ``required_fact_key`` already points at the final deterministic SSA key.
    """
    facts: dict[str, BlueprintStateChange] = {}
    for node in blueprint.nodes:
        for change in node.state_changes:
            facts[change.fact_key] = change
    changes = 0
    for node in blueprint.nodes:
        for requirement in node.state_requirements:
            if requirement.assumed_prior:
                continue
            fact = facts.get(requirement.required_fact_key)
            if fact is None:
                # A missing fact is a genuine "dependency not established"
                # error.  Leave the label untouched so
                # BLUEPRINT_STATE_UNESTABLISHED still surfaces it.
                continue
            if fact.state_key != requirement.state_key:
                requirement.state_key = fact.state_key
                changes += 1
    return changes
