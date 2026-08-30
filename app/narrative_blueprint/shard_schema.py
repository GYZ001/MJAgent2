"""The provider-facing Blueprint shard JSON schema and truncated-output prefix recovery."""
from __future__ import annotations

import json
import re
from typing import Any

from .constants import BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE, BLUEPRINT_VERSION
from .models_core import NarrativeBlueprintShard, NarrativeNode
from .provider_normalize import _PARATEXT_EMPTY_LIST_FIELDS, normalize_blueprint_raw_json


def blueprint_shard_provider_schema(
    source_payload: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the provider schema with explicit delivery evidence surfaces."""
    schema = NarrativeBlueprintShard.model_json_schema()
    definitions = schema.get("$defs", {})
    node_schema = definitions.get("NarrativeNode")
    if not isinstance(node_schema, dict):
        return schema
    source_ids: list[str] = []
    source_unit_keys: list[str] = []
    action_unit_keys: list[str] = []
    quoted_unit_keys: list[str] = []
    for source in source_payload or []:
        source_id = str(source.get("source_segment_id") or "")
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
        for fact in source.get("source_facts") or []:
            source_unit_key = str(fact.get("source_unit_key") or "")
            if not source_unit_key or source_unit_key in source_unit_keys:
                continue
            source_unit_keys.append(source_unit_key)
            if fact.get("projection") == "action":
                action_unit_keys.append(source_unit_key)
            elif fact.get("projection") == "quoted":
                quoted_unit_keys.append(source_unit_key)

    if source_ids:
        schema["properties"]["source_segment_ids"]["items"] = {
            "enum": source_ids,
        }
    node_properties = node_schema.get("properties", {})
    if source_ids:
        node_properties["source_segment_ids"]["items"] = {
            "enum": source_ids,
        }
    node_properties["source_segment_ids"]["minItems"] = 1
    node_properties["source_segment_ids"]["maxItems"] = (
        BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
    )
    if action_unit_keys:
        node_properties["environment_source_unit_keys"]["items"] = {
            "enum": action_unit_keys,
        }
    else:
        node_properties["environment_source_unit_keys"]["maxItems"] = 0
        node_properties["state_subject_assignments"]["maxItems"] = 0
    node_properties["state_subject_adjudicated_unit_keys"] = {
        "type": "array",
        "maxItems": 0,
        "items": {"type": "string"},
        "description": (
            "Compiler-owned exact-unit adjudication proof; providers must "
            "always return an empty list."
        ),
    }
    node_properties.get("participants", {})["description"] = (
        "Ordered identity roster. Its unique identity set must exactly equal "
        "the union of participant_evidence.identity_key and "
        "state_subject_assignments.identity_keys; never add an identity "
        "without owned source evidence."
    )
    node_properties.get("participant_evidence", {})["description"] = (
        "Source-backed identity evidence. Together with exact-unit joint "
        "state_subject assignments, its identity set must exactly equal "
        "participants."
    )
    location_schema = node_properties.get("location_label")
    if isinstance(location_schema, dict):
        location_schema["pattern"] = r"^(?!.*(?:、|/|\+|内外)).+$"
        location_schema["description"] = (
            "Exactly one primary location; never combine locations."
        )
    evidence_schema = definitions.get("NarrativeParticipantEvidence")
    if isinstance(evidence_schema, dict):
        evidence_properties = evidence_schema.get("properties", {})
        evidence_properties.get("identity_key", {})["minLength"] = 1
        if source_ids:
            evidence_properties["source_segment_ids"]["items"] = {
                "enum": source_ids,
            }
        evidence_properties["source_segment_ids"]["description"] = (
            "Backend-derived from source_unit_keys; only fill it for a row "
            "that cites no source unit at all."
        )
        if source_unit_keys:
            evidence_properties["source_unit_keys"]["items"] = {
                "enum": source_unit_keys,
            }
        else:
            evidence_properties["source_unit_keys"]["maxItems"] = 0
        evidence_schema["description"] = (
            "Every participants identity must have at least one matching "
            "evidence object with owned source_segment_ids."
        )
        evidence_schema.setdefault("allOf", []).append({
            "if": {
                "properties": {
                    "usage": {
                        "enum": ["voice", "state_subject"],
                    },
                },
                "required": ["usage"],
            },
            "then": {
                "properties": {
                    "identity_key": {"minLength": 1},
                    "source_unit_keys": {"minItems": 1},
                },
                "required": [
                    "identity_key",
                    "source_unit_keys",
                ],
            },
        })
        # Once a row cites an exact unit, its owning SRC is derivable, so the
        # provider must not spend output restating it.
        evidence_schema.setdefault("allOf", []).append({
            "if": {
                "properties": {"source_unit_keys": {"minItems": 1}},
                "required": ["source_unit_keys"],
            },
            "then": {
                "properties": {"source_segment_ids": {"maxItems": 0}},
            },
        })
    assignment_schema = definitions.get("NarrativeStateSubjectAssignment")
    if isinstance(assignment_schema, dict) and action_unit_keys:
        assignment_schema["properties"]["source_unit_key"] = {
            "enum": action_unit_keys,
        }
    delivery_schema = definitions.get("NarrativeSourceUnitDelivery")
    if isinstance(delivery_schema, dict):
        if quoted_unit_keys:
            delivery_schema["properties"]["source_unit_key"] = {
                "enum": quoted_unit_keys,
            }
        else:
            node_properties["source_unit_deliveries"]["maxItems"] = 0
        delivery_schema.setdefault("allOf", []).append({
            "if": {
                "properties": {
                    "mode": {
                        "enum": [
                            "spoken_dialogue",
                            "offscreen_voice",
                        ],
                    },
                },
                "required": ["mode"],
            },
            "then": {
                "properties": {
                    "performer_key": {"minLength": 1},
                },
                "required": ["performer_key"],
            },
        })
    required = node_schema.setdefault("required", [])
    for field_name in (
        "participants",
        "participant_evidence",
        "state_subject_assignments",
        "environment_source_unit_keys",
        "source_unit_deliveries",
    ):
        if field_name not in required:
            required.append(field_name)
    node_schema.setdefault("allOf", []).append({
        "if": {
            "properties": {
                "source_unit_deliveries": {
                    "contains": {
                        "properties": {
                            "mode": {
                                "enum": [
                                    "spoken_dialogue",
                                    "offscreen_voice",
                                ],
                            },
                        },
                        "required": ["mode"],
                    },
                },
            },
            "required": ["source_unit_deliveries"],
        },
        "then": {
            "properties": {
                "participant_evidence": {
                    "contains": {
                        "properties": {
                            "usage": {"const": "voice"},
                        },
                        "required": [
                            "identity_key",
                            "source_unit_keys",
                            "usage",
                        ],
                    },
                },
            },
            "required": ["participant_evidence"],
        },
    })
    node_schema.setdefault("allOf", []).append({
        "if": {
            "properties": {
                "participants": {"minItems": 1},
            },
            "required": ["participants"],
        },
        "then": {
            "properties": {
                "participant_evidence": {"minItems": 1},
            },
            "required": ["participant_evidence"],
        },
    })
    paratext_properties: dict[str, Any] = {
        field_name: {"const": []}
        for field_name in _PARATEXT_EMPTY_LIST_FIELDS
    }
    paratext_properties.update({
        "decision": {"const": None},
        "exit_state": {"const": ""},
    })
    node_schema.setdefault("allOf", []).append({
        "if": {
            "properties": {"narrative_layer": {"const": "paratext"}},
            "required": ["narrative_layer"],
        },
        "then": {"properties": paratext_properties},
    })
    return schema


def recover_complete_blueprint_prefix(raw: str) -> dict[str, Any] | None:
    """Recover complete timeline nodes when a long blueprint hits max_tokens."""
    text = normalize_blueprint_raw_json(str(raw or ""))
    nodes_match = re.search(r'"nodes"\s*:\s*\[', text)
    if nodes_match is None:
        return None
    decoder = json.JSONDecoder()
    cursor = nodes_match.end()
    nodes: list[dict[str, Any]] = []
    while cursor < len(text):
        while cursor < len(text) and (
            text[cursor].isspace() or text[cursor] == ","
        ):
            cursor += 1
        if cursor >= len(text) or text[cursor] == "]":
            break
        try:
            value, cursor = decoder.raw_decode(text, cursor)
            node = NarrativeNode.model_validate(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            break
        nodes.append(node.model_dump(mode="json"))
    if not nodes:
        return None
    episode_match = re.search(r'"episode_no"\s*:\s*(\d+)', text)
    return {
        "format_version": BLUEPRINT_VERSION,
        "episode_no": (
            int(episode_match.group(1))
            if episode_match is not None
            else 1
        ),
        "nodes": nodes,
        "scene_plans": [],
    }
