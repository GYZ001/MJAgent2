"""Artifact-lineage compatibility checks for the envelope and scene-shard
artifacts: parent/content hashing helpers and the ``*_artifact_compatibility``
predicates that decide whether a cached artifact can still be reused.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import json
from app.evidence import repository as evidence_repository
from pydantic import ValidationError
from typing import Any

from .constants import (
    SCREENPLAY_ENVELOPE_VERSION,
    SCREENPLAY_SCENE_SEMANTIC_MAX_REPAIR_ROUNDS,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
)
from .models import (
    ScreenplayEnvelopeIR,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardSemanticFinding,
    ScreenplaySceneShardSemanticReview,
)
from .review_consensus import screenplay_scene_semantic_consensus


def _artifact_content(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    content = artifact.get("content")
    if isinstance(content, dict):
        return content
    raw_content = artifact.get("content_json")
    try:
        content = (
            json.loads(raw_content)
            if isinstance(raw_content, str)
            else raw_content
        )
    except (TypeError, json.JSONDecodeError):
        return None
    return content if isinstance(content, dict) else None


def _artifact_parent_ids(
    artifact: dict[str, Any],
) -> set[str] | None:
    parents = artifact.get("parent_artifact_ids")
    if parents is None:
        raw_parents = artifact.get("parent_artifact_ids_json")
        try:
            parents = (
                json.loads(raw_parents)
                if isinstance(raw_parents, str)
                else raw_parents
            )
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(parents, list):
        return None
    return {str(parent_id) for parent_id in parents if str(parent_id)}


def _artifact_model_snapshot(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    snapshot = artifact.get("model_snapshot")
    if isinstance(snapshot, dict):
        return snapshot
    raw_snapshot = artifact.get("model_snapshot_json")
    try:
        snapshot = (
            json.loads(raw_snapshot)
            if isinstance(raw_snapshot, str)
            else raw_snapshot
        )
    except (TypeError, json.JSONDecodeError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _scene_shard_semantic_review_compatibility(
    artifact: dict[str, Any],
    raw_artifact: dict[str, Any] | None,
    *,
    current_shard_content_hash: str,
) -> tuple[bool, str]:
    """Bind a reusable shard to a clean review of the exact creative root."""
    if raw_artifact is None:
        return False, "semantic_review_raw_missing"
    raw_content = _artifact_content(raw_artifact)
    snapshot = _artifact_model_snapshot(artifact)
    if not isinstance(raw_content, dict) or not isinstance(snapshot, dict):
        return False, "semantic_review_metadata_missing"
    evidence = raw_content.get("semantic_review_evidence")
    if not isinstance(evidence, dict):
        return False, "semantic_review_evidence_missing"
    if (
        evidence.get("contract_version")
        != SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
        or snapshot.get("semantic_review_version")
        != SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
    ):
        return False, "semantic_review_version"
    reviewed_shard_content_hash = str(
        evidence.get("reviewed_shard_content_hash") or ""
    )
    snapshot_shard_content_hash = str(
        snapshot.get("reviewed_shard_content_hash") or ""
    )
    if (
        not reviewed_shard_content_hash
        or not snapshot_shard_content_hash
        or reviewed_shard_content_hash != snapshot_shard_content_hash
        or reviewed_shard_content_hash != current_shard_content_hash
    ):
        return False, "semantic_review_shard_hash"
    initial_hash = str(evidence.get("initial_creative_hash") or "")
    reviewed_hash = str(evidence.get("reviewed_creative_hash") or "")
    if len(initial_hash) != 64 or len(reviewed_hash) != 64:
        return False, "semantic_review_hash_missing"
    if snapshot.get("reviewed_creative_hash") != reviewed_hash:
        return False, "semantic_review_hash_binding"
    phases = evidence.get("phases")
    if not isinstance(phases, list) or not phases:
        return False, "semantic_review_artifacts_missing"
    if any(not isinstance(phase, dict) for phase in phases):
        return False, "semantic_review_artifacts_missing"
    phase_names = [phase.get("phase") for phase in phases]
    if (
        len(phase_names) > SCREENPLAY_SCENE_SEMANTIC_MAX_REPAIR_ROUNDS + 1
        or phase_names[0] != "initial"
        or any(phase != "post_repair" for phase in phase_names[1:])
    ):
        return False, "semantic_review_phase"
    if str(phases[0].get("creative_hash") or "") != initial_hash:
        return False, "semantic_review_initial_candidate"
    if str(phases[-1].get("creative_hash") or "") != reviewed_hash:
        return False, "semantic_review_final_candidate"
    artifact_content = _artifact_content(artifact)
    try:
        validated_shard = ScreenplaySceneShardIR.model_validate(
            artifact_content
        )
    except (TypeError, ValidationError):
        return False, "semantic_review_artifact_schema"
    valid_unit_keys = {
        unit.unit_key
        for scene in validated_shard.scenes
        for unit in scene.units
    }
    recomputed_consensus: list[list[dict[str, Any]]] = []
    for phase_index, phase in enumerate(phases):
        reviews = phase.get("reviews")
        if not isinstance(reviews, list) or len(reviews) != 2:
            return False, "semantic_review_artifacts_missing"
        for review in reviews:
            findings = (
                review.get("findings")
                if isinstance(review, dict)
                else None
            )
            try:
                validated_findings = (
                    [
                        ScreenplaySceneShardSemanticFinding.model_validate(
                            finding
                        )
                        for finding in findings
                    ]
                    if isinstance(findings, list)
                    else []
                )
            except ValidationError:
                validated_findings = []
            if validated_findings:
                finding_keys = [
                    (finding.unit_key, finding.code)
                    for finding in validated_findings
                ]
                if len(finding_keys) != len(set(finding_keys)):
                    return False, "semantic_review_duplicate_finding"
        validated_reviews: list[ScreenplaySceneShardSemanticReview] = []
        for review in reviews:
            try:
                validated_reviews.append(
                    ScreenplaySceneShardSemanticReview.model_validate(review)
                )
            except ValidationError:
                return False, "semantic_review_schema"
        if any(
            finding.unit_key not in valid_unit_keys
            or any(
                related_unit_key not in valid_unit_keys
                for related_unit_key in finding.related_unit_keys
            )
            for review in validated_reviews
            for finding in review.findings
        ):
            return False, "semantic_review_unit_key"
        finding_keys = [
            [(finding.unit_key, finding.code) for finding in review.findings]
            for review in validated_reviews
        ]
        if any(len(keys) != len(set(keys)) for keys in finding_keys):
            return False, "semantic_review_duplicate_finding"
        expected_consensus = [
            finding.model_dump(mode="json")
            for finding in screenplay_scene_semantic_consensus(
                validated_reviews[0],
                validated_reviews[1],
            )
        ]
        if phase_index:
            allowed_unit_keys = {
                finding["unit_key"]
                for finding in recomputed_consensus[-1]
            }
            current_findings = [
                finding
                for review in validated_reviews
                for finding in review.findings
            ]
            if any(
                finding.unit_key not in allowed_unit_keys
                and set(finding.related_unit_keys).isdisjoint(
                    allowed_unit_keys
                )
                for finding in current_findings
            ):
                return False, "semantic_review_phase_contract"
        if phase.get("consensus") != expected_consensus:
            return False, "semantic_review_consensus"
        recomputed_consensus.append(expected_consensus)
    if (
        len(phase_names) == 1
        and recomputed_consensus[0]
    ) or (
        len(phase_names) > 1
        and (
            not all(recomputed_consensus[:-1])
            or recomputed_consensus[-1]
        )
    ):
        return False, "semantic_review_phase_contract"
    return True, ""


def screenplay_normalized_artifact_lineage_compatibility(
    artifact: dict[str, Any],
    raw_artifact: dict[str, Any] | None,
    *,
    expected_raw_type: str,
    expected_authority_artifact_ids: set[str],
) -> tuple[bool, str]:
    """Match the generator's normalized -> raw -> authority lineage."""
    normalized_parents = _artifact_parent_ids(artifact)
    if raw_artifact is None or normalized_parents != {str(raw_artifact.get("id") or "")}:
        return False, "normalized_parent"
    if raw_artifact.get("type") != expected_raw_type:
        return False, "raw_artifact_type"
    if raw_artifact.get("status") != "candidate":
        return False, "raw_artifact_status"
    if (
        raw_artifact.get("scope_type") != artifact.get("scope_type")
        or raw_artifact.get("scope_id") != artifact.get("scope_id")
    ):
        return False, "raw_artifact_scope"
    if (
        str(raw_artifact.get("contract_version") or "")
        != str(artifact.get("contract_version") or "")
    ):
        return False, "raw_artifact_contract_version"
    raw_content = _artifact_content(raw_artifact)
    try:
        raw_content_hash = evidence_repository.content_hash(
            raw_content,
            raw_artifact.get("file_path"),
        )
    except (OSError, TypeError, ValueError):
        return False, "raw_artifact_content_hash"
    if raw_content_hash != str(raw_artifact.get("content_hash") or ""):
        return False, "raw_artifact_content_hash"
    if _artifact_parent_ids(raw_artifact) != expected_authority_artifact_ids:
        return False, "raw_authority_parents"
    return True, ""


def screenplay_envelope_artifact_compatibility(
    artifact: dict[str, Any],
    *,
    expected_blueprint_hash: str,
    expected_identity_registry_hash: str,
    raw_artifact: dict[str, Any] | None = None,
    expected_authority_artifact_ids: set[str] | None = None,
) -> tuple[bool, str]:
    content = _artifact_content(artifact)
    if artifact.get("status") != "validated":
        return False, "artifact_status"
    if str(artifact.get("contract_version") or "") != SCREENPLAY_ENVELOPE_VERSION:
        return False, "artifact_contract_version"
    if not isinstance(content, dict):
        return False, "artifact_content"
    if evidence_repository.content_hash(content) != str(
        artifact.get("content_hash") or ""
    ):
        return False, "artifact_content_hash"
    try:
        envelope = ScreenplayEnvelopeIR.model_validate(content)
    except ValidationError:
        return False, "content_schema"
    if envelope.blueprint_hash != expected_blueprint_hash:
        return False, "blueprint_hash"
    if envelope.identity_registry_hash != expected_identity_registry_hash:
        return False, "identity_registry_hash"
    if expected_authority_artifact_ids is not None:
        return screenplay_normalized_artifact_lineage_compatibility(
            artifact,
            raw_artifact,
            expected_raw_type="screenplay_envelope_raw",
            expected_authority_artifact_ids=expected_authority_artifact_ids,
        )
    return True, ""


def screenplay_scene_shard_artifact_compatibility(
    artifact: dict[str, Any],
    *,
    expected_blueprint_hash: str = "",
    expected_identity_registry_hash: str = "",
    expected_generation_scaffold_hash: str = "",
    raw_artifact: dict[str, Any] | None = None,
    expected_authority_artifact_ids: set[str] | None = None,
) -> tuple[bool, str]:
    """Validate one persisted shard against the current resumable contract."""
    content = _artifact_content(artifact)
    if artifact.get("status") != "validated":
        return False, "artifact_status"
    if str(artifact.get("contract_version") or "") != SCREENPLAY_SCENE_SHARD_VERSION:
        return False, "artifact_contract_version"
    if not isinstance(content, dict):
        return False, "artifact_content"
    try:
        current_shard_content_hash = evidence_repository.content_hash(
            content,
            artifact.get("file_path"),
        )
    except (OSError, TypeError, ValueError):
        return False, "artifact_content_hash"
    if current_shard_content_hash != str(artifact.get("content_hash") or ""):
        return False, "artifact_content_hash"
    if str(content.get("contract_version") or "") != SCREENPLAY_SCENE_SHARD_VERSION:
        return False, "content_contract_version"
    if not expected_blueprint_hash or not expected_identity_registry_hash:
        return False, "expected_authority_hash_missing"
    if str(content.get("blueprint_hash") or "") != expected_blueprint_hash:
        return False, "blueprint_hash"
    if (
        str(content.get("identity_registry_hash") or "")
        != expected_identity_registry_hash
    ):
        return False, "identity_registry_hash"
    identity_hash = str(content.get("identity_scaffold_hash") or "")
    generation_hash = str(content.get("generation_scaffold_hash") or "")
    if not identity_hash or not generation_hash:
        return False, "scaffold_hash_missing"
    if (
        expected_generation_scaffold_hash
        and generation_hash != expected_generation_scaffold_hash
    ):
        return False, "generation_scaffold_hash"
    try:
        ScreenplaySceneShardIR.model_validate(content)
    except ValidationError:
        return False, "content_schema"
    if expected_authority_artifact_ids is not None:
        compatible, reason = screenplay_normalized_artifact_lineage_compatibility(
            artifact,
            raw_artifact,
            expected_raw_type="screenplay_scene_shard_raw",
            expected_authority_artifact_ids=expected_authority_artifact_ids,
        )
        if not compatible:
            return compatible, reason
        return _scene_shard_semantic_review_compatibility(
            artifact,
            raw_artifact,
            current_shard_content_hash=current_shard_content_hash,
        )
    return True, ""


_PARTICIPANT_PERCEPTION_CHANNELS = (
    "audible",
    "visible_effect",
    "visible_reaction",
)
