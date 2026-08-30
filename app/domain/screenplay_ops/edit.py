"""已发布剧本的权威字段 payload、编辑影响预览与编辑落地。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 status_snapshot 与 run_control。
"""
from __future__ import annotations

from app import task_registry
from app.db import get_conn
from app.domain.common import (
    SCREENPLAY_WORKSPACE_WITHHELD_FIELDS,
    _episode_or_404,
    _episode_source_text,
    _load_screenplay,
    _prepare_screenplay_for_storage,
    _project_bible_or_placeholder,
    merge_withheld_screenplay_fields,
    router,
)
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import EvidenceArtifact
from app.schemas import (
    EpisodeScreenplay,
    schema_errors,
)
from fastapi import HTTPException

from .run_control import _screenplay_task_active
from .status_snapshot import _screenplay_field_diff


def _screenplay_payload_with_authority_fields(episode_id: str, payload):
    """Restore the fields the screenplay workspace was never given.

    ``view=script`` withholds pipeline-authored authority fields
    (``SCREENPLAY_WORKSPACE_WITHHELD_FIELDS``).  Every endpoint that accepts a
    screenplay body from that workspace must merge them back from the current
    authority *before* validation, routing or diffing, otherwise a page save
    would silently publish a screenplay with its narrative authority erased.
    """
    if not isinstance(payload, dict):
        return payload
    missing = [
        field
        for field in SCREENPLAY_WORKSPACE_WITHHELD_FIELDS
        if field not in payload
    ]
    if not missing:
        return payload
    row = get_conn().execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if row is None:
        return payload
    try:
        authority = _load_screenplay(row)
    except Exception:  # noqa: BLE001 - a stale authority cannot supply fields
        return payload
    return merge_withheld_screenplay_fields(payload, authority=authority)

@router.post("/episodes/{episode_id}/screenplay/impact-preview")
def preview_screenplay_edit_impact(episode_id: str, body: dict):
    """发布前的纯读影响预览：不建任务、不设栅栏、不写证据。"""
    ep = dict(_episode_or_404(episode_id))
    payload = _screenplay_payload_with_authority_fields(
        episode_id, body.get("screenplay", body)
    )
    expected_version = body.get("expected_version")
    current_version = ep.get("screenplay_artifact_id") or ""
    if expected_version is not None and str(expected_version) != str(current_version):
        raise HTTPException(409, {
            "code": "screenplay_version_conflict",
            "message": "当前剧本已被更新，我的草稿已保留",
            "expected_version": expected_version,
            "current_version": current_version,
            "diff": _screenplay_field_diff(_load_screenplay(ep), payload),
        })
    instance, validation_errors = schema_errors(EpisodeScreenplay, payload)
    if validation_errors:
        raise HTTPException(422, {
            "code": "screenplay_validation_failed",
            "message": "剧本结构校验未通过",
            "errors": validation_errors,
        })
    from app.production.screenplay_repair import (
        run_screenplay_qa,
        screenplay_identity_gate_issues,
    )
    from app.portraits import (
        apply_screenplay_character_resolutions,
        load_screenplay_character_resolutions_for_source,
        screenplay_unknown_identity_errors,
    )
    from app.validators import normalize_screenplay_candidate

    instance = normalize_screenplay_candidate(instance)
    conn = get_conn()
    source_text = _episode_source_text(conn, ep)
    resolutions = load_screenplay_character_resolutions_for_source(
        conn,
        episode_id,
        episode_no=int(ep.get("episode_no") or 0),
        source_text=source_text,
    )
    apply_screenplay_character_resolutions(instance, resolutions)
    instance = normalize_screenplay_candidate(instance)
    current_script = _load_screenplay(ep)
    comparable_current = (
        normalize_screenplay_candidate(current_script) if current_script else None
    )
    diff = _screenplay_field_diff(comparable_current, instance)
    qa_issues = []
    qa_evaluation = None
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = _project_bible_or_placeholder(project)
    identity_preflight_required = bool(
        screenplay_unknown_identity_errors(instance, bible)
    )
    if diff or identity_preflight_required:
        qa_issues, qa_evaluation = run_screenplay_qa(
            instance,
            bible=bible,
            source_text=source_text,
            episode={
                **ep,
                "character_resolutions": resolutions,
            },
        )
    hard_identity_issues = screenplay_identity_gate_issues(qa_issues)
    counts = {
        "shots": int(conn.execute(
            "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
        ).fetchone()["c"]),
        "shot_versions": int(conn.execute(
            "SELECT COUNT(*) AS c FROM shot_versions v JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?",
            (episode_id,),
        ).fetchone()["c"]),
        "shot_scenes": int(conn.execute(
            "SELECT COUNT(*) AS c FROM shot_scenes sc JOIN shots s ON s.id=sc.shot_id WHERE s.episode_id=?",
            (episode_id,),
        ).fetchone()["c"]),
    }
    active_runs = [kind for kind in ("storyboard", "video_completion") if task_registry.active(kind, episode_id)]
    downstream_exists = any(counts.values()) or bool(active_runs) or ep["status"] in {
        "scripting", "scripted", "script_failed", "confirmed", "generating", "done",
    }
    return {
        "read_only": True,
        "unchanged": not diff,
        "diff": diff,
        "changed_sections": sorted({item["section"] for item in diff}),
        "qa": {
            # 影响预览严格只读，不能在这里运行会建卡/持久化决议的模型预检。
            # 正式 PUT 会先执行未来 10 章模型消歧，只有仍无法落实时才拒绝发布。
            "passed": True,
            "score": qa_evaluation.score if qa_evaluation else 100,
            "evaluation_role": "score_only",
            "runtime_blocking": False,
            "gate_retry_exhausted": bool(qa_issues),
            "warnings": [issue.message for issue in qa_issues],
        },
        "character_identity_preflight": {
            "required": bool(hard_identity_issues),
            "status": "pending_model_resolution" if hard_identity_issues else "resolved",
            "lookahead_chapters": 10,
            "message": (
                "发布时会先由模型结合未来 10 章解析人物真名；无可靠真名时自动映射为路人角色"
                if hard_identity_issues else "人物身份已满足当前剧本合同"
            ),
        },
        "downstream": counts,
        "active_runs": active_runs,
        "requires_server_approval": downstream_exists,
        "impact": (
            "发布将安全停止运行中的下游，并清空受影响的分镜/媒体链路"
            if downstream_exists else "仅更新本集发布剧本，当前没有需清空的下游"
        ),
    }

@router.put("/episodes/{episode_id}/screenplay")
async def edit_screenplay(episode_id: str, body: dict):
    from app.capabilities.dispatch import ui_route
    payload = _screenplay_payload_with_authority_fields(
        episode_id, body.get("screenplay", body)
    )
    expected_version = body.get("expected_version")
    routed = await ui_route(
        "screenplay.update",
        {
            "episode_id": episode_id,
            "screenplay": payload,
            "reason": body.get("reason"),
            "expected_version": expected_version,
        },
    )
    if routed is not None:
        return routed
    ep = dict(_episode_or_404(episode_id))
    if _screenplay_task_active(episode_id):
        raise HTTPException(409, {
            "code": "screenplay_task_active",
            "message": "剧本流程正在运行；请先停止并等待任务退出，再发布人工草稿",
            "run_id": ep.get("active_screenplay_run_id"),
        })
    if ep.get("status") == "scripting" and not task_registry.active(
        "storyboard", episode_id
    ):
        raise HTTPException(
            409,
            "分镜状态显示运行中但找不到对应 worker；未发布草稿也未清空下游",
        )
    current_version = ep.get("screenplay_artifact_id") or ""
    if expected_version is not None and str(expected_version) != str(current_version):
        current_script = _load_screenplay(ep)
        raise HTTPException(409, {
            "code": "screenplay_version_conflict",
            "message": "当前剧本已被更新，我的草稿已保留",
            "expected_version": expected_version,
            "current_version": current_version,
            "diff": _screenplay_field_diff(current_script, payload),
        })
    instance, validation_errors = schema_errors(EpisodeScreenplay, payload)
    if validation_errors:
        raise HTTPException(422, {
            "code": "screenplay_validation_failed",
            "message": "剧本结构校验未通过",
            "errors": validation_errors,
        })
    from app.production.screenplay_repair import (
        run_screenplay_qa,
        screenplay_identity_gate_issues,
    )
    from app.portraits import (
        apply_screenplay_character_resolutions,
        load_screenplay_character_resolutions_for_source,
        screenplay_unknown_identity_errors,
    )
    from app.validators import normalize_screenplay_candidate

    instance = normalize_screenplay_candidate(instance)
    conn = get_conn()
    old_script = _load_screenplay(ep)
    normalized_old = normalize_screenplay_candidate(old_script) if old_script else None
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
        # 手工剧本只投影 typed identity-bearing fields 与 owned SRC，禁止全章重扫。
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
        # 人物预检可能新增真名角色卡，QA 必须使用最新 Bible。
        project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(project)
    # “内容相同”只能在身份映射落实后判断。否则历史发布版中的大汉/青衣人
    # 会绕过剧本闸门，并把成本问题推迟到分镜阶段。
    if normalized_old and not _screenplay_field_diff(normalized_old, instance):
        return {
            "saved": True,
            "unchanged": True,
            "artifact_id": current_version or None,
            "downstream_cleared": False,
        }
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
    qa_issues, qa_evaluation = run_screenplay_qa(
        instance,
        bible=bible,
        source_text=source_text,
        episode=qa_episode,
    )
    hard_identity_issues = screenplay_identity_gate_issues(qa_issues)
    if hard_identity_issues:
        raise HTTPException(422, {
            "code": "screenplay_character_identity_unresolved",
            "message": "剧本人物身份未解决，未发布也未清空分镜",
            "errors": [issue.message for issue in hard_identity_issues],
        })
    if (
        bool(qa_evaluation.runtime_blocking)
        and not bool(qa_evaluation.hard_gate_passed)
    ):
        raise HTTPException(422, {
            "code": "screenplay_qa_failed",
            "message": "剧本 QA 未通过，未发布也未清空分镜",
            "score": qa_evaluation.score,
            "errors": [issue.message for issue in qa_issues],
            "issues": [
                issue.model_dump(mode="json")
                for issue in qa_issues
            ],
        })
    instance = _prepare_screenplay_for_storage(
        ep, instance,
        keep_existing_id=(old_script.id if old_script else None),
        keep_created_at=(old_script.created_at if old_script else None),
    )
    # 原子互斥的第一步是持久化写入栅栏。分镜启动路由会检查此位，
    # 因此设置成功后不会再有新下游任务与本次发布竞争。
    conn.execute("BEGIN IMMEDIATE")
    owner_row = conn.execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    active_owner = (
        evidence_repository.get_active_scoped_run(
            owner_row["active_screenplay_run_id"],
            workflow_type="screenplay",
            scope_type="episode",
            scope_id=episode_id,
            conn=conn,
        )
        if owner_row
        else None
    )
    if active_owner:
        conn.rollback()
        raise HTTPException(409, {
            "code": "screenplay_task_active",
            "message": "剧本流程已在校验期间启动；未发布人工草稿",
            "run_id": active_owner["id"],
        })
    cursor = conn.execute(
        "UPDATE episodes SET screenplay_publish_fence=1, "
        "screenplay_snapshot_version=screenplay_snapshot_version+1 "
        "WHERE id=? AND screenplay_publish_fence=0",
        (episode_id,),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise HTTPException(409, "另一次剧本发布正在安全停止下游，请稍后查看进度")

    try:
        cancelled_kinds: list[str] = []
        for kind in ("storyboard", "video_completion"):
            if await task_registry.cancel_and_wait(kind, episode_id):
                cancelled_kinds.append(kind)
        if any(task_registry.active(kind, episode_id) for kind in ("storyboard", "video_completion")):
            raise HTTPException(409, "下游任务尚未终止，已保留草稿与当前发布版")

        latest = dict(_episode_or_404(episode_id))
        if latest.get("status") == "scripting" and "storyboard" not in cancelled_kinds:
            raise HTTPException(
                409,
                "分镜 worker 未能提供已退出证据，本次不发布、不清空下游",
            )
        latest_version = latest.get("screenplay_artifact_id") or ""
        if expected_version is not None and str(expected_version) != str(latest_version):
            raise HTTPException(409, {
                "code": "screenplay_version_conflict",
                "message": "停止下游期间剧本基线已变化，未发布草稿",
                "expected_version": expected_version,
                "current_version": latest_version,
                "diff": _screenplay_field_diff(_load_screenplay(latest), payload),
            })

        has_shots = conn.execute(
            "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
        ).fetchone()["c"] > 0
        from app.production.patch import screenplay_artifact_payload
        from app.production.publish import publish_screenplay
        from app.production.revision import (
            ensure_production_revision,
            mark_baseline_generated,
            mark_first_evaluation,
        )

        contract_version = str(qa_episode["screenplay_contract_version"])
        from app.production.screenplay_authority import (
            SCREENPLAY_QA_PROFILE_VERSION,
            screenplay_authority_fingerprint,
        )

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
        candidate = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_document",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T1",
                content=screenplay_artifact_payload(instance),
                parent_artifact_ids=[latest_version] if latest_version else [],
                contract_version=contract_version,
            )
        )
        revision = mark_baseline_generated(
            revision.id,
            baseline_artifact_id=candidate["id"],
            working_artifact_id=candidate["id"],
        )
        final_issues, final_evaluation = run_screenplay_qa(
            instance,
            bible=_project_bible_or_placeholder(project),
            source_text=source_text,
            episode=qa_episode,
            artifact_id=candidate["id"],
            artifact_hash=candidate["content_hash"],
        )
        hard_identity_issues = screenplay_identity_gate_issues(final_issues)
        if hard_identity_issues:
            raise HTTPException(422, {
                "code": "screenplay_character_identity_unresolved",
                "message": "发布前人物身份复核未通过",
                "errors": [issue.message for issue in hard_identity_issues],
            })
        if (
            bool(final_evaluation.runtime_blocking)
            and not bool(final_evaluation.hard_gate_passed)
        ):
            raise HTTPException(422, {
                "code": "screenplay_qa_failed",
                "message": "发布前 QA 复核未通过，当前发布版保持不变",
                "score": final_evaluation.score,
                "errors": [issue.message for issue in final_issues],
                "issues": [
                    issue.model_dump(mode="json")
                    for issue in final_issues
                ],
            })
        evaluation_row = evidence_repository.create_evaluation(
            candidate["id"], final_evaluation,
        )
        evaluation_id = (
            evaluation_row.get("id")
            if isinstance(evaluation_row, dict)
            else str(evaluation_row or "")
        ) or f"eval-{candidate['id']}"
        mark_first_evaluation(revision.id, evaluation_id)
        published = publish_screenplay(
            episode_id=episode_id,
            revision_id=revision.id,
            artifact_id=candidate["id"],
            artifact_hash=candidate["content_hash"],
            evaluation_ids=[evaluation_id] if evaluation_id else [],
            input_fingerprint=revision.input_fingerprint,
            contract_version=contract_version,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
            clear_downstream=True,
        )
        return {
            "saved": True,
            "unchanged": False,
            "artifact_id": published["artifact_id"],
            "certificate_id": published["certificate_id"],
            "revision_id": revision.id,
            "qa_score": final_evaluation.score,
            "qa_warnings": [issue.message for issue in final_issues],
            "gate_retry_exhausted": bool(final_issues),
            "downstream_cleared": has_shots,
            "cancelled_tasks": cancelled_kinds,
        }
    finally:
        conn = get_conn()
        if conn.in_transaction:
            conn.rollback()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE episodes SET screenplay_publish_fence=0, "
                "screenplay_snapshot_version=screenplay_snapshot_version+1 "
                "WHERE id=? AND screenplay_publish_fence=1",
                (episode_id,),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
