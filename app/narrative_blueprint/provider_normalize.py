"""Repairs and normalizes a raw provider-authored blueprint payload before validation (fingerprint, brace repair, cross-field drift)."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.source_facts import SOURCE_FACT_VERSION

from .constants import (
    AUDIBLE_SOURCE_DELIVERY_MODES,
    BLUEPRINT_PROMPT_VERSION,
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_VERSION,
    STATE_SUBJECT_ADJUDICATION_VERSION,
)


def blueprint_authority_validator_fingerprint() -> str:
    material = {
        "contract_version": BLUEPRINT_VERSION,
        "prompt_version": BLUEPRINT_PROMPT_VERSION,
        "source_fact_version": SOURCE_FACT_VERSION,
        "shard_policy_version": BLUEPRINT_SHARD_POLICY_VERSION,
        "local_authority_validator_version": (
            BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
        ),
        "split_manifest_version": BLUEPRINT_SPLIT_MANIFEST_VERSION,
        "state_subject_adjudication_version": (
            STATE_SUBJECT_ADJUDICATION_VERSION
        ),
    }
    return hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalize_source_segment_id(value: Any) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"SRC0*(\d+)", raw, flags=re.IGNORECASE)
    if match is None:
        return raw
    return f"SRC{int(match.group(1)):04d}"


def normalize_blueprint_raw_json(raw: str) -> str:
    """Repair a provider's redundant node-closing brace mechanically."""
    normalized = re.sub(
        r"\}\}\},\s*(\{\"key\"\s*:)",
        r"}},\1",
        raw,
    )
    return re.sub(
        r"\}\}\]\},\s*(\"delete_node_keys\"\s*:)",
        r"}]}],\1",
        normalized,
    )


_PARATEXT_EMPTY_LIST_FIELDS = (
    "participants",
    "participant_evidence",
    "state_subject_assignments",
    "environment_source_unit_keys",
    "state_subject_adjudicated_unit_keys",
    "source_unit_deliveries",
    "state_requirements",
    "state_changes",
    "released_constraints_for",
)


def _evidence_segment_ids_from_units(value: Any) -> list[str]:
    """Project the owning SRC ids out of exact source unit keys, in order."""
    segment_ids: list[str] = []
    for unit_key in value if isinstance(value, list) else []:
        source_segment_id, marker, unit_id = str(unit_key or "").partition(
            ":unit:"
        )
        if not marker or not source_segment_id or not unit_id:
            return []
        if source_segment_id not in segment_ids:
            segment_ids.append(source_segment_id)
    return segment_ids


def normalize_blueprint_provider_payload(payload: Any) -> Any:
    """Normalize provider-only cross-field drift without inventing authority.

    Provider bytes remain preserved in the raw T0 artifact.  This projection is
    limited to explicit provider claims: paratext fields are emptied, evidence
    identities are added to the participant roster, and voice claims are
    removed only for the exact units explicitly classified as non-audible.
    Missing evidence is never synthesized and participants are never deleted.
    """
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return normalized
    normalized_nodes: list[Any] = []
    for value in nodes:
        if not isinstance(value, dict):
            normalized_nodes.append(value)
            continue
        node = dict(value)
        if value.get("narrative_layer") == "paratext":
            for field_name in _PARATEXT_EMPTY_LIST_FIELDS:
                node[field_name] = []
            node["decision"] = None
            node["exit_state"] = ""
            normalized_nodes.append(node)
            continue

        non_audible_units = {
            str(delivery.get("source_unit_key") or "")
            for delivery in value.get("source_unit_deliveries") or []
            if (
                isinstance(delivery, dict)
                and delivery.get("mode") not in AUDIBLE_SOURCE_DELIVERY_MODES
            )
        }
        evidence_values: list[Any] = []
        evidence_identities: list[str] = []
        for evidence_value in value.get("participant_evidence") or []:
            if not isinstance(evidence_value, dict):
                evidence_values.append(evidence_value)
                continue
            evidence = dict(evidence_value)
            if evidence.get("usage") == "voice":
                source_unit_keys = evidence.get("source_unit_keys") or []
                retained_keys = [
                    key
                    for key in source_unit_keys
                    if str(key) not in non_audible_units
                ]
                if source_unit_keys and not retained_keys:
                    continue
                evidence["source_unit_keys"] = retained_keys
            # ``source_unit_keys`` is the authoritative binding; the owning SRC
            # is literally its prefix.  Restating it is redundant output the
            # provider can also get wrong, so the projection derives it here
            # and the shard contract no longer asks for it.  Rows that cite no
            # unit (segment-level ``visible``/``mentioned``) keep their own.
            derived_segment_ids = _evidence_segment_ids_from_units(
                evidence.get("source_unit_keys")
            )
            if derived_segment_ids:
                evidence["source_segment_ids"] = derived_segment_ids
            evidence_values.append(evidence)
            identity_key = str(evidence.get("identity_key") or "").strip()
            if identity_key and identity_key not in evidence_identities:
                evidence_identities.append(identity_key)
        for assignment in value.get("state_subject_assignments") or []:
            if not isinstance(assignment, dict):
                continue
            for identity_key_value in assignment.get("identity_keys") or []:
                identity_key = str(identity_key_value or "").strip()
                if identity_key and identity_key not in evidence_identities:
                    evidence_identities.append(identity_key)
        node["participant_evidence"] = evidence_values
        # This proof is compiler-owned and can only be created by the bounded
        # exact-unit ownership adjudication path.
        node["state_subject_adjudicated_unit_keys"] = []
        # Evidence rows and exact-unit joint assignments are the two typed
        # source-backed identity authorities. An independently authored roster
        # would create a third truth that can survive into downstream IR.
        node["participants"] = evidence_identities
        normalized_nodes.append(node)
    normalized["nodes"] = normalized_nodes
    return normalized
