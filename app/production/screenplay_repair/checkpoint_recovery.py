"""Working-artifact completion and checkpoint/recovery helpers: completing a
screenplay from its working artifact, deciding whether a checkpoint is
reusable, and identifying which recovery document/evaluation to resume from.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import json
from app.db import (
    get_conn,
    now,
)
from app.evidence import repository as evidence_repository
from app.harness.types import (
    EvidenceArtifact,
    Issue,
)
from app.production.patch import (
    load_screenplay_from_artifact,
    screenplay_artifact_payload,
)
from app.production.publish import publish_screenplay
from app.production.revision import (
    get_production_revision,
    mark_first_evaluation,
    rebind_input_fingerprint,
    save_checkpoint,
    update_working_artifact,
)
from app.schemas import (
    Bible,
    EpisodeScreenplay,
)
from typing import Any

from .gates import (
    ScreenplayIdentityGateError,
    _eval_id_from_create,
    non_waivable_screenplay_issues,
)
from .narrative_graph_normalize import _normalize_screenplay_narrative_graph
from .qa import run_screenplay_qa


async def ensure_source_characters_incremental(
    episode_id: str,
    source_text: str,
    draft_text: str = "",
) -> dict[str, Any]:
    """增量追加 source-backed 角色，不触发完整 regenerate。"""
    from app.domain import screenplay_ops
    return await screenplay_ops._screenplay_character_discovery(
        episode_id, source_text, draft_text=draft_text,
    )


def _complete_screenplay_from_working_artifact(
    *,
    episode_id: str,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    revision_id: str,
    run_id: str | None,
    checkpoint: dict[str, Any],
    activation_no: int,
) -> EpisodeScreenplay:
    """Validate every production gate and publish only a zero-blocker artifact."""
    from app.harness.contracts import get_contract
    from app.portraits import (
        apply_screenplay_character_resolutions,
        normalize_screenplay_voice_ids,
        screenplay_character_resolution_errors,
        screenplay_unknown_identity_errors,
    )
    from app.production.screenplay_authority import (
        SCREENPLAY_QA_PROFILE_VERSION,
        screenplay_authority_fingerprint,
    )

    conn = get_conn()
    rev = get_production_revision(revision_id)
    if rev is None or not rev.working_artifact_id:
        raise RuntimeError("剧本结构校验缺少 working Artifact")
    checkpoint = {
        **checkpoint,
        **dict(rev.checkpoint_json or {}),
        "open_issue_ids": [],
        "last_issue_fingerprints": [],
    }
    working_id = rev.working_artifact_id
    artifact = evidence_repository.get_artifact(working_id)
    if artifact is None:
        raise RuntimeError("剧本 working Artifact 不存在")
    artifact_hash = evidence_repository.verified_artifact_content_hash(artifact)
    script = load_screenplay_from_artifact(working_id)

    save_checkpoint(revision_id, {
        **checkpoint,
        "phase": "STRUCTURE_VALIDATION",
        "activation_no": activation_no,
        "working_artifact_id": working_id,
        "yield_reason": None,
    })
    conn.execute(
        "UPDATE episodes SET screenplay_status='running',screenplay_error=?,"
        "screenplay_updated_at=? WHERE id=?",
        ("正在校验剧本结构与人物上下文", now(), episode_id),
    )
    conn.commit()
    if run_id:
        evidence_repository.append_event(
            run_id,
            "STRUCTURE_VALIDATION_STARTED",
            "info",
            "开始校验剧本结构与人物上下文",
            payload={"artifact_id": working_id},
        )

    normalization_changes = apply_screenplay_character_resolutions(
        script,
        episode.get("character_resolutions") or [],
    )
    normalization_changes.extend(normalize_screenplay_voice_ids(script, bible))
    normalization_changes.extend(_normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters=episode.get("authorized_source_chapters"),
    ))
    if normalization_changes:
        payload = screenplay_artifact_payload(script)
        normalized_hash = evidence_repository.content_hash(payload)
        if normalized_hash != artifact_hash:
            normalized = evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_document",
                    scope_type="episode",
                    scope_id=episode_id,
                    status="candidate",
                    trust_level="T1",
                    content=payload,
                    parent_artifact_ids=[working_id],
                    contract_version=rev.contract_version or None,
                )
            )
            update_working_artifact(
                revision_id,
                normalized["id"],
                expected_hash=artifact_hash,
            )
            working_id = normalized["id"]
            artifact_hash = normalized["content_hash"]

    identity_errors = list(dict.fromkeys([
        *screenplay_character_resolution_errors(
            script,
            episode.get("character_resolutions") or [],
        ),
        *screenplay_unknown_identity_errors(
            script,
            bible,
            episode.get("character_resolutions") or [],
        ),
    ]))
    if identity_errors:
        message = ("剧本缺少可确定的人物身份上下文：" + "；".join(identity_errors[:5]))[:800]
        conn.execute(
            "UPDATE episodes SET screenplay_status='failed',screenplay_error=?,"
            "screenplay_updated_at=? WHERE id=?",
            (message, now(), episode_id),
        )
        conn.commit()
        save_checkpoint(revision_id, {
            **checkpoint,
            "phase": "STRUCTURE_FAILED",
            "activation_no": activation_no,
            "working_artifact_id": working_id,
            "yield_reason": "character_identity_context_missing",
            "structural_errors": identity_errors,
        })
        raise ScreenplayIdentityGateError(message)

    save_checkpoint(revision_id, {
        **checkpoint,
        "phase": "QUALITY_SCORING",
        "activation_no": activation_no,
        "working_artifact_id": working_id,
        "yield_reason": None,
    })
    conn.execute(
        "UPDATE episodes SET screenplay_error=?,screenplay_updated_at=? WHERE id=?",
        ("结构校验已通过，正在记录质量评分", now(), episode_id),
    )
    conn.commit()
    issues, evaluation = run_screenplay_qa(
        script,
        bible=bible,
        source_text=source_text,
        episode=episode,
        artifact_id=working_id,
        artifact_hash=artifact_hash,
    )
    evaluation_row = evidence_repository.create_evaluation(working_id, evaluation)
    evaluation_id = _eval_id_from_create(evaluation_row)
    if not rev.first_evaluation_done:
        rev = mark_first_evaluation(
            revision_id,
            evaluation_id or f"eval-{working_id}",
        )

    contract_version = get_contract("screenplay").version
    current_fingerprint = screenplay_authority_fingerprint(
        episode_id,
        conn=conn,
        source_text=source_text,
        # Discovery in another concurrent episode may advance the persisted
        # composite Bible after this run loaded its generation snapshot.
        # Publication binds the current durable authority projection.
        bible=None,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    if rev.input_fingerprint != current_fingerprint:
        rev = rebind_input_fingerprint(
            revision_id,
            input_fingerprint=current_fingerprint,
            expected_working_artifact_id=working_id,
        )

    save_checkpoint(revision_id, {
        **checkpoint,
        "phase": "PUBLISHING",
        "activation_no": activation_no,
        "working_artifact_id": working_id,
        "quality_issue_count": len(issues),
        "quality_score": evaluation.score,
        "yield_reason": None,
    })
    conn.execute(
        "UPDATE episodes SET screenplay_error=?,screenplay_updated_at=? WHERE id=?",
        ("质量评分已记录，正在原子发布剧本", now(), episode_id),
    )
    conn.commit()
    published = publish_screenplay(
        episode_id=episode_id,
        revision_id=revision_id,
        artifact_id=working_id,
        artifact_hash=artifact_hash,
        evaluation_ids=[evaluation_id] if evaluation_id else [],
        input_fingerprint=current_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=True,
    )
    save_checkpoint(revision_id, {
        **checkpoint,
        "phase": "SUCCEEDED",
        "activation_no": activation_no,
        "working_artifact_id": working_id,
        "quality_issue_count": len(issues),
        "quality_score": evaluation.score,
        "gate_retry_exhausted": bool(issues),
        "yield_reason": None,
    })
    if run_id:
        evidence_repository.append_event(
            run_id,
            "PUBLISHED",
            "info",
            "剧本结构已发布，质量问题仅作为评分提示",
            payload={
                **published,
                "quality_score": evaluation.score,
                "quality_issue_count": len(issues),
            },
        )
    return load_screenplay_from_artifact(working_id)


def _checkpoint_after_baseline_generation(
    previous: dict[str, Any],
    revision,
) -> dict[str, Any]:
    """Merge in progress persisted while the Baseline await was in flight."""
    latest = dict(getattr(revision, "checkpoint_json", None) or {})
    return {**previous, **latest}


def _artifact_descends_from(
    artifact_id: str,
    ancestor_artifact_id: str,
) -> bool:
    """Check immutable Artifact ancestry without accepting a label match."""
    if not artifact_id or not ancestor_artifact_id:
        return False
    if artifact_id == ancestor_artifact_id:
        return True
    pending = [artifact_id]
    seen: set[str] = set()
    while pending:
        current_id = pending.pop()
        if current_id in seen:
            continue
        seen.add(current_id)
        artifact = evidence_repository.get_artifact(current_id)
        if artifact is None:
            continue
        for parent_id in artifact.get("parent_artifact_ids") or []:
            parent = str(parent_id or "")
            if parent == ancestor_artifact_id:
                return True
            if parent and parent not in seen:
                pending.append(parent)
    return False


def _screenplay_recovery_hard_issues(
    script: EpisodeScreenplay,
    *,
    artifact_id: str,
    artifact_hash: str,
    bible: Bible,
    source_text: str,
    episode: dict[str, Any],
) -> list[Issue]:
    issues, _evaluation = run_screenplay_qa(
        script,
        bible=bible,
        source_text=source_text,
        episode=episode,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
    )
    return non_waivable_screenplay_issues(issues)


def _reusable_recovery_document(
    *,
    episode_id: str,
    content_hash: str,
    merged_ir_artifact_id: str,
    merged_content_hash: str,
    contract_version: str,
) -> dict[str, Any] | None:
    """Reuse only an exact deterministic recovery output after a CAS retry."""
    from app.screenplay_ir import IR_COMPILER_VERSION, IR_VERSION
    from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION

    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM artifacts WHERE type='screenplay_document' "
        "AND scope_type='episode' AND scope_id=? AND content_hash=? "
        "AND contract_version=? AND status IN ('candidate','validated','approved') "
        "ORDER BY created_at DESC",
        (episode_id, content_hash, contract_version),
    ).fetchall()
    for row in rows:
        artifact = evidence_repository.get_artifact(str(row["id"]), conn=conn)
        if artifact is None:
            continue
        snapshot = artifact.get("model_snapshot") or {}
        if (
            {str(value) for value in artifact.get("parent_artifact_ids") or []}
            != {merged_ir_artifact_id}
            or snapshot.get("recovery_contract")
            != "screenplay-working-recovery.v1"
            or snapshot.get("qa_profile_version")
            != SCREENPLAY_QA_PROFILE_VERSION
            or snapshot.get("compiler_version") != IR_COMPILER_VERSION
            or snapshot.get("generation_contract") != IR_VERSION
            or snapshot.get("source_merged_content_hash")
            != merged_content_hash
            or evidence_repository.content_hash(artifact.get("content"))
            != content_hash
        ):
            continue
        return artifact
    return None


def _reusable_recovery_evaluation(
    *,
    artifact_id: str,
    artifact_hash: str,
    input_fingerprint: str,
) -> dict[str, Any] | None:
    """Find the exact gate proof already committed before a failed CAS."""
    from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION

    conn = get_conn()
    rows = conn.execute(
        "SELECT id,evaluator_type,evaluation_role,score_status,status,"
        "hard_gate_passed,runtime_blocking,issues_json,"
        "evidence_json FROM evaluations WHERE artifact_id=? "
        "AND evaluator_name='screenplay_production_qa' "
        "AND evaluator_version=? ORDER BY created_at DESC",
        (artifact_id, SCREENPLAY_QA_PROFILE_VERSION),
    ).fetchall()
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
            issues = json.loads(row["issues_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            row["evaluator_type"] != "deterministic"
            or row["evaluation_role"] != "score_only"
            or row["score_status"] != "scored"
            or row["status"] in {"failed", "error"}
            or not bool(row["hard_gate_passed"])
            or bool(row["runtime_blocking"])
            or str(evidence.get("artifact_hash") or "") != artifact_hash
            or str(evidence.get("artifact_id") or "") != artifact_id
            or str(evidence.get("qa_profile_version") or "")
            != SCREENPLAY_QA_PROFILE_VERSION
            or str(evidence.get("authority_input_fingerprint") or "")
            != input_fingerprint
            or any(
                isinstance(issue, dict)
                and (
                    bool(issue.get("must_fix"))
                    or bool((issue.get("evidence") or {}).get("must_fix"))
                    or bool(
                        (issue.get("evidence") or {}).get("runtime_blocking")
                    )
                )
                for issue in issues
            )
        ):
            continue
        return {"id": str(row["id"])}
    return None


def _activation_retry_grant_id(run_id: str | None) -> str | None:
    """Return the retry grant this activation was authorised with, if any."""
    if not run_id:
        return None
    from app.db import get_conn

    row = get_conn().execute(
        "SELECT config_snapshot_json FROM workflow_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        snapshot = json.loads(row["config_snapshot_json"] or "{}")
    except (TypeError, ValueError):
        return None
    return str(snapshot.get("blueprint_retry_grant_id") or "") or None


