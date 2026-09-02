"""剧本 IR 保真——蓝图权威快照的定位、校验与 IR 候选恢复。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


from app.narrative_blueprint import (
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    blueprint_authority_validator_fingerprint,
)
from app.source_excerpt import (
    index_source_segments,
)
from app.source_facts import (
    SOURCE_FACT_VERSION,
)

from .constants import (
    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
    SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
    SCREENPLAY_IR_MAX_TOKENS,
    SCREENPLAY_IR_MIN_TOKENS,
)


def screenplay_ir_token_budget(source_text: str) -> int:
    """Bound output by source complexity without reserving 36K for short chapters."""
    source_segments = len(index_source_segments(source_text))
    estimated = 8192 + source_segments * 48
    return min(
        SCREENPLAY_IR_MAX_TOKENS,
        max(SCREENPLAY_IR_MIN_TOKENS, estimated),
    )


def _narrative_blueprint_content_hash(
    blueprint: NarrativeBlueprint | None,
) -> str:
    if blueprint is None:
        return ""
    return hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _current_blueprint_authority_snapshot(
    source_text: str,
    *,
    generation_mode: str,
    generation_budget: Any | None = None,
    shard_count: int | None = None,
) -> dict[str, Any]:
    """One versioned authority binding for every final Blueprint producer."""
    validator_material = {
        "contract_version": BLUEPRINT_VERSION,
        "prompt_version": SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "source_fact_version": SOURCE_FACT_VERSION,
        "shard_policy_version": BLUEPRINT_SHARD_POLICY_VERSION,
        "local_authority_validator_version": (
            BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
        ),
        "split_manifest_version": BLUEPRINT_SPLIT_MANIFEST_VERSION,
    }
    snapshot: dict[str, Any] = {
        "generation_mode": generation_mode,
        **validator_material,
        "source_corpus_hash": hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest(),
        "validator_fingerprint": (
            blueprint_authority_validator_fingerprint()
        ),
    }
    if generation_mode == "semantic_reviewed":
        snapshot["review_policy_version"] = (
            BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
        )
    if shard_count is not None:
        snapshot["shard_count"] = int(shard_count)
    if generation_budget is not None:
        snapshot.update({
            "provider_call_count": generation_budget.provider_calls,
            "requested_output_tokens": (
                generation_budget.requested_output_tokens
            ),
            "actual_output_tokens": generation_budget.actual_output_tokens,
            "unknown_output_tokens": generation_budget.unknown_output_tokens,
            "charged_output_tokens": generation_budget.charged_output_tokens,
            "active_reserved_output_tokens": (
                generation_budget.reserved_output_tokens
            ),
        })
    return snapshot


def _blueprint_authority_snapshot_is_current(
    snapshot: dict[str, Any],
    source_text: str,
) -> bool:
    expected = _current_blueprint_authority_snapshot(
        source_text,
        generation_mode=str(snapshot.get("generation_mode") or "authority"),
    )
    authority_keys = [
        "contract_version",
        "prompt_version",
        "source_fact_version",
        "shard_policy_version",
        "local_authority_validator_version",
        "split_manifest_version",
        "source_corpus_hash",
        "validator_fingerprint",
    ]
    if str(snapshot.get("generation_mode") or "") == "semantic_reviewed":
        authority_keys.append("review_policy_version")
    return all(
        snapshot.get(key) == expected.get(key)
        for key in authority_keys
    )


def _select_current_blueprint_artifact(
    rows: list[Any],
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> tuple[str | None, str | None]:
    """Prefer a current wrapper; retain an old same-hash id only as lineage."""
    expected_hash = _narrative_blueprint_content_hash(blueprint)
    legacy_same_hash_id: str | None = None
    for row in rows:
        try:
            raw_content = json.loads(row["content_json"] or "{}")
            if not _artifact_json_content_is_sealed(row, raw_content):
                continue
            row_blueprint = NarrativeBlueprint.model_validate(raw_content)
            if _narrative_blueprint_content_hash(row_blueprint) != expected_hash:
                continue
            artifact_id = str(row["id"])
            if legacy_same_hash_id is None:
                legacy_same_hash_id = artifact_id
            snapshot = json.loads(row["model_snapshot_json"] or "{}")
            if (
                str(row["contract_version"] or "") == BLUEPRINT_VERSION
                and str(row["prompt_version"] or "")
                == SCREENPLAY_BLUEPRINT_PROMPT_VERSION
                and _blueprint_authority_snapshot_is_current(
                    snapshot,
                    source_text,
                )
            ):
                return artifact_id, legacy_same_hash_id
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None, legacy_same_hash_id


def _artifact_json_content_is_sealed(row: Any, content: object) -> bool:
    """Verify a DB artifact wrapper before any cache/recovery projection."""
    from app.evidence import repository as evidence_repository

    try:
        stored_hash = str(row["content_hash"] or "")
    except (KeyError, IndexError, TypeError):
        return False
    return bool(
        stored_hash
        and stored_hash == evidence_repository.content_hash(content)
    )
