"""剧本 IR 保真——蓝图权威快照的定位、校验与 IR 候选恢复。"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


from app import textmatch
from app.db import get_conn
from app.errors import ArtifactNeedsRebuildError
from app.narrative_blueprint import (
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    blueprint_authority_validator_fingerprint,
)
from app.schemas import (extract_json)
from app.source_excerpt import (
    index_source_segments,
    structural_front_matter_ids,
)
from app.source_facts import (
    SOURCE_FACT_VERSION,
)
from app.screenplay_ir import (
    IR_LOCAL_SOURCE_WINDOW,
    IR_MIN_ADAPTED_SOURCE_RATIO,
    IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO,
    IR_VERSION,
    ScreenplayGenerationIR,
    normalize_screenplay_ir_payload,
    recover_complete_screenplay_ir_prefix,
    screenplay_ir_missing_event_semantic_paths,
    screenplay_ir_missing_participant_delivery_paths,
    screenplay_ir_source_audit_contract_errors,
)

from .constants import (
    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
    SCREENPLAY_BASELINE_PROMPT_VERSION,
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


def screenplay_ir_fidelity_budget(source_text: str) -> dict[str, Any]:
    segments = index_source_segments(source_text)
    front_matter_ids = structural_front_matter_ids(segments)
    dramatic = [
        segment for segment in segments
        if segment.segment_id not in front_matter_ids
    ]
    source_chars = sum(
        len(textmatch.condense(segment.text))
        for segment in dramatic
    )
    windows: list[dict[str, Any]] = []
    for start in range(0, len(dramatic), IR_LOCAL_SOURCE_WINDOW):
        window = dramatic[start:start + IR_LOCAL_SOURCE_WINDOW]
        chars = sum(
            len(textmatch.condense(segment.text))
            for segment in window
        )
        windows.append({
            "first_source_id": window[0].segment_id,
            "last_source_id": window[-1].segment_id,
            "source_chars": chars,
            "minimum_adapted_chars": math.ceil(
                chars * IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO
            ),
        })
    return {
        "front_matter_ids": sorted(front_matter_ids),
        "dramatic_source_chars": source_chars,
        "minimum_adapted_chars": math.ceil(
            source_chars * IR_MIN_ADAPTED_SOURCE_RATIO
        ),
        "windows": windows,
    }


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


def _screenplay_ir_blueprint_snapshot_matches(
    model_snapshot: dict[str, Any],
    expected_blueprint_hash: str,
) -> bool:
    recorded_hash = str(model_snapshot.get("blueprint_hash") or "")
    return bool(
        not expected_blueprint_hash
        or not recorded_hash
        or recorded_hash == expected_blueprint_hash
    )


def _recover_screenplay_ir_candidate(
    episode_id: str,
    *,
    blueprint_hash: str = "",
    expected_source_audit_annotations: list[object] | None = None,
) -> tuple[ScreenplayGenerationIR, str] | None:
    """Load the latest IR produced for the same authority input."""
    from app.evidence import repository as evidence_repository
    from app.observability.tracing import current_trace

    trace = current_trace()
    if not trace.run_id:
        return None
    conn = get_conn()
    current_run = conn.execute(
        "SELECT input_fingerprint FROM workflow_runs WHERE id=?",
        (trace.run_id,),
    ).fetchone()
    if current_run is None:
        return None
    input_fingerprint = str(current_run["input_fingerprint"] or "")
    lineage_rows = conn.execute(
        """WITH RECURSIVE lineage(id, parent_run_id) AS (
               SELECT id,parent_run_id
                 FROM workflow_runs
                WHERE id=?
               UNION ALL
               SELECT wr.id,wr.parent_run_id
                 FROM workflow_runs wr
                 JOIN lineage ON wr.id=lineage.parent_run_id
           )
           SELECT id FROM lineage""",
        (trace.run_id,),
    ).fetchall()
    lineage_run_ids = [str(row["id"]) for row in lineage_rows]
    if not lineage_run_ids:
        return None
    lineage_marks = ",".join("?" for _ in lineage_run_ids)
    rows = conn.execute(
        f"""SELECT a.id,a.type,a.content_json,a.content_hash,a.contract_version,
                  a.prompt_version,
                  a.model_snapshot_json,
                  wr.input_fingerprint AS artifact_input_fingerprint
             FROM artifacts a
             JOIN step_runs sr ON sr.id=a.created_by_step_run_id
             JOIN workflow_runs wr ON wr.id=sr.run_id
            WHERE a.scope_type='episode' AND a.scope_id=?
              AND a.contract_version LIKE 'screenplay-generation-ir.v%'
              AND a.status!='stale'
              AND a.type IN (
                    'screenplay_generation_ir',
                    'screenplay_generation_ir_raw',
                    'episode_screenplay'
              )
              AND wr.input_fingerprint=?
              AND wr.id IN ({lineage_marks})
            ORDER BY CASE
                         WHEN a.prompt_version=? AND a.contract_version=?
                         THEN 0
                         WHEN a.contract_version=? THEN 1
                         ELSE 2
                     END,
                     a.created_at DESC
            LIMIT 20""",
        (
            episode_id,
            input_fingerprint,
            *lineage_run_ids,
            SCREENPLAY_BASELINE_PROMPT_VERSION,
            IR_VERSION,
            IR_VERSION,
        ),
    ).fetchall()
    for row in rows:
        try:
            model_snapshot = json.loads(
                row["model_snapshot_json"] or "{}"
            )
            if not _screenplay_ir_blueprint_snapshot_matches(
                model_snapshot,
                blueprint_hash,
            ):
                continue
            content = json.loads(row["content_json"] or "{}")
            if (
                not str(row["content_hash"] or "")
                or str(row["content_hash"])
                != evidence_repository.content_hash(content)
            ):
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(row["id"]),
                    artifact_type=str(row["type"]),
                    reason="IR Artifact 内容与存储指纹漂移",
                )
            raw = content.get("raw_output") if isinstance(content, dict) else None
            if isinstance(raw, str):
                try:
                    payload = extract_json(
                        raw,
                        repair_unescaped_inner_quotes=True,
                    )
                except ValueError:
                    payload = recover_complete_screenplay_ir_prefix(raw)
            else:
                payload = content
            if not isinstance(payload, dict):
                continue
            missing_paths = [
                *screenplay_ir_missing_participant_delivery_paths(payload),
                *screenplay_ir_missing_event_semantic_paths(payload),
            ]
            audit_errors = screenplay_ir_source_audit_contract_errors(
                payload,
                expected_source_audit_annotations=(
                    expected_source_audit_annotations
                ),
            )
            if missing_paths or audit_errors:
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(row["id"]),
                    artifact_type=str(row["type"]),
                    reason=(
                        "缺少当前合同要求的显式字段 "
                        + "、".join(missing_paths[:10])
                        + (
                            "；" + "；".join(audit_errors[:10])
                            if audit_errors else ""
                        )
                    ),
                )
            artifact_contract = str(row["contract_version"] or "")
            payload_contract = str(payload.get("format_version") or "")
            if artifact_contract == IR_VERSION and payload_contract != IR_VERSION:
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(row["id"]),
                    artifact_type=str(row["type"]),
                    reason=(
                        f"Artifact 合同为 {IR_VERSION}，"
                        f"内容合同为 {payload_contract or 'missing'}"
                    ),
                )
            if artifact_contract != IR_VERSION:
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(row["id"]),
                    artifact_type=str(row["type"]),
                    reason=(
                        f"Artifact 合同 {artifact_contract or 'missing'} "
                        f"与当前 {IR_VERSION} 不一致，需要重建"
                    ),
                )
            payload, _changes = normalize_screenplay_ir_payload(payload)
            candidate = ScreenplayGenerationIR.model_validate(payload)
        except ArtifactNeedsRebuildError as exc:
            conn.execute(
                "UPDATE artifacts SET status='stale',stale_reason=? "
                "WHERE id=? AND status!='rejected'",
                (str(exc), row["id"]),
            )
            conn.commit()
            continue
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        return candidate, str(row["id"])
    return None
