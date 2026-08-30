"""_revalidate_or_rebuild_resume_working: decides whether a resumed run's
working artifact can be revalidated in place or must be rebuilt.

Split out of app/production/screenplay_repair.py. Kept as one function
verbatim (moved, not rewritten).
"""
from __future__ import annotations

import hashlib
import json
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.production.patch import (
    load_screenplay_from_artifact,
    screenplay_artifact_payload,
)
from app.production.revision import recover_screenplay_working_authority
from app.schemas import Bible
from typing import Any

from .checkpoint_recovery import (
    _artifact_descends_from,
    _reusable_recovery_document,
    _reusable_recovery_evaluation,
    _screenplay_recovery_hard_issues,
)
from .gates import (
    SCREENPLAY_REPAIR_PLANNER_VERSION,
    ScreenplayNarrativeGateError,
    _eval_id_from_create,
    non_waivable_screenplay_issues,
)
from .narrative_graph_normalize import _normalize_screenplay_narrative_graph
from .qa import run_screenplay_qa


def _revalidate_or_rebuild_resume_working(
    *,
    episode_id: str,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    revision,
    entry_eligibility,
    input_fingerprint: str,
    contract_version: str,
    run_id: str | None,
):
    """Migrate an old QA profile without trusting its mutable repair descendant."""
    from app.portraits import (
        apply_screenplay_character_resolutions,
        normalize_screenplay_voice_ids,
    )
    from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION
    from app.production.revision import resolve_screenplay_resume_eligibility
    from app.screenplay_ir import (
        IR_COMPILER_VERSION,
        IR_VERSION,
        ScreenplayGenerationIR,
        compile_screenplay_ir,
    )
    from app.screenplay_scene_shards import SCREENPLAY_MERGED_IR_VERSION
    from app.validators import normalize_screenplay_candidate

    if entry_eligibility.reason_code != "WORKING_REVALIDATION_REQUIRED":
        return revision
    if (
        entry_eligibility.revision_id != revision.id
        or entry_eligibility.working_artifact_id != revision.working_artifact_id
        or not revision.working_artifact_id
    ):
        raise RuntimeError("screenplay recovery entry eligibility 已漂移")

    # Re-resolve immediately before reading the authority.  The final write
    # repeats this check under BEGIN IMMEDIATE, closing the TOCTOU window.
    current_eligibility = resolve_screenplay_resume_eligibility(episode_id)
    if (
        current_eligibility.reason_code != "WORKING_REVALIDATION_REQUIRED"
        or current_eligibility.revision_id != revision.id
        or current_eligibility.working_artifact_id != revision.working_artifact_id
    ):
        raise RuntimeError("screenplay recovery eligibility 已变化")

    old_working_id = str(revision.working_artifact_id)
    old_working_artifact = evidence_repository.get_artifact(old_working_id)
    if old_working_artifact is None:
        raise RuntimeError("screenplay recovery working Artifact 不存在")
    old_working_hash = evidence_repository.verified_artifact_content_hash(
        old_working_artifact
    )
    working_script = load_screenplay_from_artifact(old_working_id)
    qa_episode = {
        **episode,
        "screenplay_contract_version": contract_version,
    }
    working_hard_issues = _screenplay_recovery_hard_issues(
        working_script,
        artifact_id=old_working_id,
        artifact_hash=old_working_hash,
        bible=bible,
        source_text=source_text,
        episode=qa_episode,
    )

    action = "working_revalidated"
    source_authority_id = old_working_id
    replacement_artifact = old_working_artifact
    merged_ir_id = str(
        current_eligibility.reusable_checkpoint.get(
            "merged_ir_artifact_id"
        ) or ""
    )
    if working_hard_issues:
        action = "working_rebuilt"
        if merged_ir_id:
            merged_artifact = evidence_repository.get_artifact(merged_ir_id)
            trusted_parent_ids = {
                str(current_eligibility.reusable_checkpoint.get(key) or "")
                for key in (
                    "blueprint_artifact_id",
                    "identity_artifact_id",
                    "envelope_artifact_id",
                )
                if str(current_eligibility.reusable_checkpoint.get(key) or "")
            }
            trusted_parent_ids.update(
                str(item.get("normalized_artifact_id") or "")
                for item in (
                    current_eligibility.reusable_checkpoint.get("shards") or []
                )
                if isinstance(item, dict)
                and str(item.get("normalized_artifact_id") or "")
            )
            if (
                merged_artifact is None
                or merged_artifact.get("type")
                != "screenplay_generation_ir_merged"
                or merged_artifact.get("scope_type") != "episode"
                or merged_artifact.get("scope_id") != episode_id
                or merged_artifact.get("status") != "validated"
                or merged_artifact.get("trust_level") != "T1"
                or str(merged_artifact.get("contract_version") or "")
                != SCREENPLAY_MERGED_IR_VERSION
                or not trusted_parent_ids
                or trusted_parent_ids != {
                    str(value)
                    for value in merged_artifact.get("parent_artifact_ids") or []
                }
                or str(merged_artifact.get("content_hash") or "")
                != evidence_repository.content_hash(
                    merged_artifact.get("content")
                )
            ):
                raise RuntimeError("screenplay recovery merged IR 权威复验失败")
            merged_ir = ScreenplayGenerationIR.model_validate(
                merged_artifact.get("content") or {}
            )
            compiler_audit: list[dict[str, Any]] = []
            rebuilt_script = compile_screenplay_ir(
                merged_ir,
                episode=qa_episode,
                source_text=source_text,
                bible=bible,
                audit=compiler_audit,
            )
            apply_screenplay_character_resolutions(
                rebuilt_script,
                episode.get("character_resolutions") or [],
            )
            normalize_screenplay_voice_ids(rebuilt_script, bible)
            _normalize_screenplay_narrative_graph(
                rebuilt_script,
                authorized_source_chapters=episode.get(
                    "authorized_source_chapters"
                ),
            )
            rebuilt_script = normalize_screenplay_candidate(
                rebuilt_script,
                source_text=source_text,
            )
            rebuilt_payload = screenplay_artifact_payload(rebuilt_script)
            rebuilt_hash = evidence_repository.content_hash(rebuilt_payload)
            rebuilt_hard_issues = _screenplay_recovery_hard_issues(
                rebuilt_script,
                artifact_id=merged_ir_id,
                artifact_hash=rebuilt_hash,
                bible=bible,
                source_text=source_text,
                episode=qa_episode,
            )
            if rebuilt_hard_issues:
                raise ScreenplayNarrativeGateError(
                    "validated merged IR 按当前编译器重建后仍未通过确定性门禁："
                    + "；".join(
                        issue.message for issue in rebuilt_hard_issues[:5]
                    )
                )

            # A clean immutable Baseline may be reused only when it is an
            # ancestor and canonically identical to the trusted recompile.
            baseline_id = str(revision.baseline_artifact_id or "")
            baseline_artifact = (
                evidence_repository.get_artifact(baseline_id)
                if baseline_id
                and _artifact_descends_from(old_working_id, baseline_id)
                else None
            )
            if (
                baseline_artifact is not None
                and baseline_artifact.get("type") == "screenplay_document"
                and baseline_artifact.get("scope_id") == episode_id
                and baseline_artifact.get("status")
                in {"candidate", "validated", "approved"}
                and str(baseline_artifact.get("contract_version") or "")
                == contract_version
                and str(baseline_artifact.get("content_hash") or "")
                == rebuilt_hash
                and str(
                    (baseline_artifact.get("model_snapshot") or {}).get(
                        "compiler_version"
                    ) or ""
                ) == IR_COMPILER_VERSION
                and {
                    str(value)
                    for value in baseline_artifact.get(
                        "parent_artifact_ids"
                    ) or []
                    if str(value)
                } == {merged_ir_id}
            ):
                replacement_artifact = baseline_artifact
                source_authority_id = merged_ir_id
            else:
                replacement_artifact = _reusable_recovery_document(
                    episode_id=episode_id,
                    content_hash=rebuilt_hash,
                    merged_ir_artifact_id=merged_ir_id,
                    merged_content_hash=str(
                        merged_artifact.get("content_hash") or ""
                    ),
                    contract_version=contract_version,
                )
                if replacement_artifact is None:
                    from app.observability.tracing import current_trace

                    replacement_artifact = evidence_repository.create_artifact(
                        EvidenceArtifact(
                            type="screenplay_document",
                            scope_type="episode",
                            scope_id=episode_id,
                            status="candidate",
                            trust_level="T1",
                            content=rebuilt_payload,
                            parent_artifact_ids=[merged_ir_id],
                            contract_version=contract_version,
                            model_snapshot={
                                "recovery_contract": "screenplay-working-recovery.v1",
                                "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
                                "compiler_version": IR_COMPILER_VERSION,
                                "generation_contract": IR_VERSION,
                                "source_merged_content_hash": str(
                                    merged_artifact.get("content_hash") or ""
                                ),
                                "compiler_audit_count": len(compiler_audit),
                                "source_revision_id": revision.id,
                            },
                        ),
                        step_run_id=current_trace().step_run_id,
                    )
                source_authority_id = merged_ir_id
        else:
            raise RuntimeError(
                "screenplay recovery 的旧 working 未通过当前门禁，且缺少"
                "resolver 认可的 validated merged IR"
            )

    replacement_id = str(replacement_artifact.get("id") or "")
    replacement_hash = str(replacement_artifact.get("content_hash") or "")
    replacement_script = load_screenplay_from_artifact(replacement_id)
    revalidation_issues, revalidation_evaluation = run_screenplay_qa(
        replacement_script,
        bible=bible,
        source_text=source_text,
        episode=qa_episode,
        artifact_id=replacement_id,
        artifact_hash=replacement_hash,
    )
    if non_waivable_screenplay_issues(revalidation_issues):
        raise ScreenplayNarrativeGateError(
            "screenplay recovery replacement 未通过持久化 gate-3 复验"
        )
    revalidation_row = _reusable_recovery_evaluation(
        artifact_id=replacement_id,
        artifact_hash=replacement_hash,
        input_fingerprint=input_fingerprint,
    )
    if revalidation_row is None:
        from app.observability.tracing import current_trace

        revalidation_row = evidence_repository.create_evaluation(
            replacement_id,
            revalidation_evaluation,
            step_run_id=current_trace().step_run_id,
        )
    revalidation_evaluation_id = _eval_id_from_create(revalidation_row)
    if not revalidation_evaluation_id:
        raise RuntimeError("screenplay recovery gate-3 复验证据未持久化")
    old_checkpoint = dict(revision.checkpoint_json or {})
    recovery_history = list(old_checkpoint.get("recovery_history") or [])
    recovery_history.append({
        "action": action,
        "source_revision_id": revision.id,
        "old_working_artifact_id": old_working_id,
        "replacement_artifact_id": replacement_id,
        "source_authority_artifact_id": source_authority_id,
        "merged_ir_artifact_id": merged_ir_id or None,
        "old_first_evaluation_id": revision.first_evaluation_id,
        "old_issue_strategy_history": dict(
            old_checkpoint.get("issue_strategy_history") or {}
        ),
        "old_patch_artifact_ids": list(
            old_checkpoint.get("patch_artifact_ids") or []
        ),
        "from_qa_profile_version": revision.qa_profile_version,
        "to_qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
    })
    recovery_checkpoint = {
        **old_checkpoint,
        "phase": "STRUCTURE_VALIDATION",
        "planner_version": SCREENPLAY_REPAIR_PLANNER_VERSION,
        "working_artifact_id": replacement_id,
        "source_revision_id": revision.id,
        "recovery_history": recovery_history,
        "issue_strategy_history": {},
        "patch_artifact_ids": [],
        "open_issue_ids": [],
        "last_issue_fingerprints": [],
        "cleared_fingerprints": [],
        "quality_issue_count": 0,
        "gate_retry_exhausted": False,
        "yield_reason": "qa_profile_revalidated",
    }
    recovered_revision = recover_screenplay_working_authority(
        revision.id,
        replacement_id,
        expected_working_artifact_id=old_working_id,
        expected_working_hash=old_working_hash,
        expected_checkpoint_hash=hashlib.sha256(
            json.dumps(
                old_checkpoint,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        expected_first_evaluation_id=revision.first_evaluation_id,
        expected_replacement_hash=replacement_hash,
        trusted_merged_ir_artifact_id=(
            merged_ir_id if replacement_id != old_working_id else ""
        ),
        revalidation_evaluation_id=revalidation_evaluation_id,
        input_fingerprint=input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        checkpoint=recovery_checkpoint,
    )
    if run_id:
        evidence_repository.append_event(
            run_id,
            (
                "SCREENPLAY_WORKING_AUTHORITY_REBUILT"
                if action == "working_rebuilt"
                else "SCREENPLAY_WORKING_AUTHORITY_REVALIDATED"
            ),
            "warning" if action == "working_rebuilt" else "info",
            (
                "旧门禁工作稿未通过当前校验，已从可信不可变上游重建"
                if action == "working_rebuilt"
                else "旧门禁工作稿已通过当前校验并绑定到新 revision"
            ),
            payload={
                "old_revision_id": revision.id,
                "new_revision_id": recovered_revision.id,
                "old_working_artifact_id": old_working_id,
                "working_artifact_id": replacement_id,
                "source_authority_artifact_id": source_authority_id,
            },
        )
    return recovered_revision


