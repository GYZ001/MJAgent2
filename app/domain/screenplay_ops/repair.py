"""剧本草稿的定向修复（单一大函数，见模块内注释）。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 task_body/status_snapshot/run_control/activation/edit。
"""
from __future__ import annotations

from app import task_registry
from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _as_body_dict,
    _episode_or_404,
    _episode_source_text,
    _load_screenplay,
    _prepare_screenplay_for_storage,
    _project_bible_or_placeholder,
    _require_harness_engine,
    router,
)
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import EvidenceArtifact
from app.schemas import (
    EpisodeScreenplay,
    schema_errors,
)
from fastapi import (
    Body,
    HTTPException,
)

from .activation import _spawn_screenplay_activation
from .edit import _screenplay_payload_with_authority_fields
from .run_control import _screenplay_task_active
from .status_snapshot import _screenplay_field_diff
from .task_body import _new_screenplay_recorder


@router.post("/episodes/{episode_id}/screenplay/repair-draft")
async def repair_screenplay_draft(episode_id: str, body: dict | None = Body(None)):
    """把 QA 未通过的人工草稿交给 Repair；QA 自身始终只读。"""
    from app.capabilities.dispatch import ui_route
    from app.production.patch import screenplay_artifact_payload
    from app.production.revision import (
        ensure_production_revision,
        mark_baseline_generated,
        mark_first_evaluation,
        save_checkpoint,
    )
    from app.production.screenplay_repair import (
        SCREENPLAY_REPAIR_PLANNER_VERSION,
        run_screenplay_qa,
        screenplay_identity_gate_issues,
    )
    from app.portraits import (
        apply_screenplay_character_resolutions,
        load_screenplay_character_resolutions_for_source,
        screenplay_unknown_identity_errors,
    )
    from app.validators import normalize_screenplay_candidate

    body = _as_body_dict(body)
    payload = _screenplay_payload_with_authority_fields(
        episode_id, body.get("screenplay", body)
    )
    expected_version = body.get("expected_version")
    routed = await ui_route("screenplay.repair_draft", {
        "episode_id": episode_id,
        "screenplay": payload,
        "expected_version": expected_version,
        "idempotency_key": body.get("idempotency_key"),
    })
    if routed is not None:
        return routed

    ep = dict(_episode_or_404(episode_id))
    _require_harness_engine(ep["project_id"])
    if _screenplay_task_active(episode_id):
        raise HTTPException(409, "剧本任务进行中")
    current_version = ep.get("screenplay_artifact_id") or ""
    if expected_version is not None and str(expected_version) != str(current_version):
        raise HTTPException(409, {
            "code": "screenplay_version_conflict",
            "message": "当前剧本已被更新，工作草稿仍保留",
            "expected_version": expected_version,
            "current_version": current_version,
            "diff": _screenplay_field_diff(_load_screenplay(ep), payload),
        })
    instance, schema_validation = schema_errors(EpisodeScreenplay, payload)
    if schema_validation:
        raise HTTPException(422, {
            "code": "screenplay_validation_failed",
            "message": "剧本结构校验未通过",
            "errors": schema_validation,
        })
    instance = _prepare_screenplay_for_storage(
        ep,
        normalize_screenplay_candidate(instance),
        keep_existing_id=(_load_screenplay(ep).id if _load_screenplay(ep) else None),
        keep_created_at=(_load_screenplay(ep).created_at if _load_screenplay(ep) else None),
    )
    conn = get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    source_text = _episode_source_text(conn, ep)
    resolutions = load_screenplay_character_resolutions_for_source(
        conn,
        episode_id,
        episode_no=int(ep.get("episode_no") or 0),
        source_text=source_text,
    )
    apply_screenplay_character_resolutions(instance, resolutions)
    instance = normalize_screenplay_candidate(instance)
    bible = _project_bible_or_placeholder(project)
    if screenplay_unknown_identity_errors(instance, bible):
        from app.identity_adjudication import (
            adjudicate_screenplay_document_identities,
        )
        try:
            await adjudicate_screenplay_document_identities(
                instance,
                episode={**ep, "character_resolutions": resolutions},
                source_text=source_text,
                bible=bible,
            )
        except Exception as exc:
            raise HTTPException(422, {
                "code": "screenplay_identity_adjudication_failed",
                "message": "剧本未决人物身份仲裁未通过",
                "errors": [str(exc)],
            }) from exc
        resolutions = load_screenplay_character_resolutions_for_source(
            conn,
            episode_id,
            episode_no=int(ep.get("episode_no") or 0),
            source_text=source_text,
        )
        apply_screenplay_character_resolutions(instance, resolutions)
        instance = normalize_screenplay_candidate(instance)
        project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(project)
    contract_row = (
        conn.execute(
            "SELECT contract_version FROM artifacts WHERE id=?",
            (current_version,),
        ).fetchone()
        if current_version
        else None
    )
    qa_episode = {
        **ep,
        "character_resolutions": resolutions,
        "screenplay_contract_version": (
            contract_row["contract_version"]
            if contract_row and contract_row["contract_version"]
            else ("2.0.0" if current_version else get_contract("screenplay").version)
        ),
    }
    issues, evaluation = run_screenplay_qa(
        instance,
        bible=bible,
        source_text=source_text,
        episode=qa_episode,
    )
    hard_identity_issues = screenplay_identity_gate_issues(issues)
    if hard_identity_issues:
        raise HTTPException(422, {
            "code": "screenplay_character_identity_unresolved",
            "message": "剧本人物身份未解决，未启动 Repair",
            "errors": [issue.message for issue in hard_identity_issues],
        })
    if not issues:
        raise HTTPException(409, {
            "code": "screenplay_qa_already_passed",
            "message": "当前草稿已通过 QA，请直接发布，不需要启动 Repair",
        })

    for kind in ("storyboard", "video_completion"):
        await task_registry.cancel_and_wait(kind, episode_id)
    if any(task_registry.active(kind, episode_id) for kind in ("storyboard", "video_completion")):
        raise HTTPException(409, "下游任务尚未终止，未启动剧本 Repair")

    contract_version = get_contract("screenplay").version
    from app.production.screenplay_authority import (
        SCREENPLAY_QA_PROFILE_VERSION,
        screenplay_authority_fingerprint,
    )

    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=episode_id,
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(instance),
        parent_artifact_ids=[current_version] if current_version else [],
        contract_version=contract_version,
    ))
    revision = ensure_production_revision(
        episode_id=episode_id,
        kind="screenplay",
        input_fingerprint=screenplay_authority_fingerprint(
            episode_id,
            conn=conn,
            source_text=source_text,
            bible=bible,
            contract_version=contract_version,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        ),
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    revision = mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    bound_issues, bound_evaluation = run_screenplay_qa(
        instance,
        bible=bible,
        source_text=source_text,
        episode=qa_episode,
        artifact_id=artifact["id"],
        artifact_hash=artifact["content_hash"],
    )
    evaluation_row = evidence_repository.create_evaluation(
        artifact["id"], bound_evaluation,
    )
    evaluation_id = (
        evaluation_row.get("id")
        if isinstance(evaluation_row, dict)
        else str(evaluation_row or "")
    ) or f"eval-{artifact['id']}"
    mark_first_evaluation(revision.id, evaluation_id)
    save_checkpoint(revision.id, {
        "planner_version": SCREENPLAY_REPAIR_PLANNER_VERSION,
        "phase": "QA_FAILED",
        "activation_no": 0,
        "working_artifact_id": artifact["id"],
        "open_issue_ids": [issue.fingerprint for issue in bound_issues],
        "last_issue_fingerprints": [issue.fingerprint for issue in bound_issues],
        "issue_strategy_history": {},
        "patch_artifact_ids": [],
        "yield_reason": None,
    })
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
        "working_screenplay_artifact_id=?, screenplay_updated_at=?, "
        "screenplay_snapshot_version=screenplay_snapshot_version+1 WHERE id=?",
        (
            f"QA 未通过（{len(bound_issues)} 项），已进入独立 Repair 环节",
            artifact["id"],
            now(),
            episode_id,
        ),
    )
    conn.commit()
    try:
        recorder = _new_screenplay_recorder(episode_id, trigger_type="manual_repair")
        _spawn_screenplay_activation(
            episode_id,
            recorder,
            project_id=ep["project_id"],
            status="repairing",
            message="Repair 正在按 QA 问题局部修复；完成后会重新执行 QA",
            expected_active_run_id=ep["active_screenplay_run_id"],
        )
    except Exception as exc:
        raise HTTPException(503, {
            "code": "SCREENPLAY_REPAIR_START_FAILED",
            "message": "Repair 未能启动，工作副本和 QA 结果已保留，可继续局部修复",
            "action": "retry_resume",
        }) from exc
    return {
        "status": "repairing",
        "run_id": recorder.run_id,
        "revision_id": revision.id,
        "artifact_id": artifact["id"],
        "qa_score": evaluation.score,
        "issue_count": len(bound_issues),
        "mode": "manual_repair",
    }
