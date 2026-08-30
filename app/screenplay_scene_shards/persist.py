"""Persists the frozen identity registry and the merged generation IR as
artifacts, plus a small helper that summarizes shard progress for status
reporting.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.observability.tracing import current_trace
from app.screenplay_ir import (
    IR_VERSION,
    ScreenplayGenerationIR,
)
from typing import Any

from .artifact_compat import _artifact_parent_ids
from .common import _hash
from .constants import SCREENPLAY_MERGED_IR_VERSION
from .identity_registry import _assert_episode_owner
from .shard_merge import _latest_validated_artifact


def persist_identity_registry(
    *,
    episode_id: str,
    identity_registry: list[dict[str, Any]],
    identity_registry_hash: str,
    parent_artifact_ids: list[str] | None = None,
) -> str:
    _assert_episode_owner(episode_id)
    registry_contract = "screenplay-identity-registry.v1"
    calculated_registry_hash = _hash(identity_registry)
    if calculated_registry_hash != identity_registry_hash:
        raise ValueError("identity registry 内容与声明指纹不匹配")
    cached = _latest_validated_artifact(
        episode_id=episode_id,
        artifact_type="screenplay_identity_registry",
        predicate=lambda content: (
            content.get("contract_version") == registry_contract
            and content.get("identity_registry_hash") == identity_registry_hash
            and content.get("identities") == identity_registry
            and _hash(content.get("identities")) == identity_registry_hash
        ),
    )
    expected_parents = {
        str(parent_id)
        for parent_id in parent_artifact_ids or []
        if str(parent_id)
    }
    if (
        cached
        and str(cached.get("contract_version") or "") == registry_contract
        and _artifact_parent_ids(cached) == expected_parents
    ):
        return str(cached["id"])
    trace = current_trace()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_registry",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": registry_contract,
                "identity_registry_hash": identity_registry_hash,
                "identities": identity_registry,
            },
            parent_artifact_ids=list(parent_artifact_ids or []),
            contract_version=registry_contract,
        ),
        step_run_id=trace.step_run_id,
    )
    return str(artifact["id"])


def persist_merged_ir(
    *,
    episode_id: str,
    ir: ScreenplayGenerationIR,
    parent_artifact_ids: list[str],
    blueprint_hash: str,
    identity_registry_hash: str,
) -> str:
    _assert_episode_owner(episode_id)
    trace = current_trace()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir_merged",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content=ir.model_dump(mode="json"),
            parent_artifact_ids=list(dict.fromkeys(parent_artifact_ids)),
            contract_version=SCREENPLAY_MERGED_IR_VERSION,
            model_snapshot={
                "generation_contract": IR_VERSION,
                "blueprint_hash": blueprint_hash,
                "identity_registry_hash": identity_registry_hash,
                "scene_count": len(ir.scenes),
                "unit_count": sum(len(scene.units) for scene in ir.scenes),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    object.__setattr__(ir, "evidence_artifact_id", artifact["id"])
    return str(artifact["id"])


def shard_progress(rows: list[dict[str, Any]] | None) -> dict[str, int]:
    values = list(rows or [])
    return {
        "total": len(values),
        "validated": sum(item.get("status") == "validated" for item in values),
        "running": sum(item.get("status") == "running" for item in values),
        "failed": sum(item.get("status") == "failed" for item in values),
    }
