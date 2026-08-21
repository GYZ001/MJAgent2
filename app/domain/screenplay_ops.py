from __future__ import annotations

import math

from app.narrative_blueprint import (
    BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD,
)
from app.orchestration.state_machine import StateConflict

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *


_SCREENPLAY_IR_WORKING_TYPES = (
    "screenplay_identity_discovery_raw",
    "screenplay_identity_discovery",
    "screenplay_identity_registry",
    "screenplay_envelope_raw",
    "screenplay_envelope",
    "screenplay_scene_shard_plan",
    "screenplay_scene_shard_raw",
    "screenplay_scene_shard",
    "screenplay_scene_shard_patch_raw",
    "screenplay_scene_shard_patch",
    "screenplay_generation_ir_merged",
    "screenplay_generation_ir",
    "screenplay_generation_ir_raw",
    "screenplay_generation_ir_fidelity_patch",
    "screenplay_generation_ir_fidelity_patch_raw",
    "screenplay_generation_ir_scene_partition_raw",
)

from app import screenplay_retry_authority as _retry_authority

_SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL = (
    _retry_authority.SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL
)


def _enter_screenplay_command_bus_retry_approval(evidence: dict[str, Any]):
    return _retry_authority.enter_screenplay_command_bus_retry_approval(
        evidence
    )


def _exit_screenplay_command_bus_retry_approval(token) -> None:
    _retry_authority.exit_screenplay_command_bus_retry_approval(token)


def _clear_unpublished_screenplay_ir(
    episode_id: str,
    *,
    run_id: str | None = None,
    conn=None,
    commit: bool = True,
) -> int:
    """Delete retry-only IR while preserving published screenplay lineage."""
    conn = conn or get_conn()
    type_marks = ",".join("?" for _ in _SCREENPLAY_IR_WORKING_TYPES)
    params: list[object] = [
        episode_id,
        *_SCREENPLAY_IR_WORKING_TYPES,
    ]
    run_filter = ""
    if run_id:
        run_filter = """
          AND a.created_by_step_run_id IN (
                SELECT sr.id
                  FROM step_runs sr
                 WHERE sr.run_id IN (
                       WITH RECURSIVE lineage(id) AS (
                           SELECT ?
                           UNION ALL
                           SELECT wr.parent_run_id
                             FROM workflow_runs wr
                             JOIN lineage ON wr.id=lineage.id
                            WHERE wr.parent_run_id IS NOT NULL
                       )
                       SELECT id FROM lineage
                 )
          )"""
        params.append(run_id)
    rows = conn.execute(
        f"""SELECT a.id
              FROM artifacts a
             WHERE a.scope_type='episode'
               AND a.scope_id=?
               AND a.type IN ({type_marks})
               {run_filter}
               AND NOT EXISTS (
                   SELECT 1
                     FROM artifacts published,
                          json_each(
                              COALESCE(
                                  published.parent_artifact_ids_json,
                                  '[]'
                              )
                          ) parent
                    WHERE published.type='screenplay_document'
                      AND published.scope_type='episode'
                      AND published.scope_id=a.scope_id
                      AND published.status='approved'
                      AND parent.value=a.id
               )""",
        params,
    ).fetchall()
    candidate_ids = {str(row["id"]) for row in rows}
    # Status is not release authority: compatibility checks intentionally mark
    # an old published Document stale before baseline rebuild.  Preserve roots
    # held by the episode, revision or certificate ledgers and all ancestors.
    protected = evidence_repository.protected_release_lineage_ids(
        scope_type="episode",
        scope_id=episode_id,
        conn=conn,
    )
    artifact_ids = sorted(candidate_ids - protected)
    if not artifact_ids:
        return 0
    marks = ",".join("?" for _ in artifact_ids)
    conn.execute(
        f"UPDATE step_runs SET output_artifact_id=NULL "
        f"WHERE output_artifact_id IN ({marks})",
        artifact_ids,
    )
    conn.execute(
        f"DELETE FROM gate_decisions WHERE artifact_id IN ({marks})",
        artifact_ids,
    )
    conn.execute(
        f"DELETE FROM evaluations WHERE artifact_id IN ({marks})",
        artifact_ids,
    )
    conn.execute(
        f"DELETE FROM completion_certificates "
        f"WHERE artifact_id IN ({marks})",
        artifact_ids,
    )
    conn.execute(
        f"UPDATE artifacts SET superseded_by_artifact_id=NULL "
        f"WHERE superseded_by_artifact_id IN ({marks})",
        artifact_ids,
    )
    conn.execute(
        f"DELETE FROM artifacts WHERE id IN ({marks})",
        artifact_ids,
    )
    if commit:
        conn.commit()
    return len(artifact_ids)


def _screenplay_content_payload(value) -> dict:
    """只比较用户可编辑的语义内容，不让时间戳造成假 diff。"""
    if isinstance(value, EpisodeScreenplay):
        payload = value.model_dump(mode="json")
    elif isinstance(value, dict):
        payload = dict(value)
    else:
        payload = {}
    for key in ("created_at", "updated_at"):
        payload.pop(key, None)
    return payload


def _screenplay_field_diff(current, proposed) -> list[dict]:
    before = _screenplay_content_payload(current)
    after = _screenplay_content_payload(proposed)
    labels = {
        "plot_spine": "主线", "full_script_text": "正文", "scene_outline": "场次",
        "source_basis": "依据", "source_text_range": "原文范围",
        "character_state_changes": "人物状态", "key_lines": "主线台词",
    }
    changed: list[dict] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        old = before.get(key)
        new = after.get(key)
        changed.append({
            "field": key,
            "section": labels.get(key, "依据与状态"),
            "before_chars": len(json.dumps(old, ensure_ascii=False, default=str)),
            "after_chars": len(json.dumps(new, ensure_ascii=False, default=str)),
        })
    return changed


def _screenplay_cast_impact(conn, ep: dict, source_text: str) -> dict:
    """Pure read-only cast impact; semantic identity count remains unknown."""
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = _project_bible_or_placeholder(project)
    known = {character.name for character in bible.characters}
    return {
        "known_character_count": len(known),
        "candidate_new_characters": [],
        "candidate_count": None,
        "requires_model_resolution": True,
        "note": "新增人物数量需由来源证据模型判断；预检不使用姓名/职业/称谓词表猜测",
        "screenplay_stage": {
            "auto_add_text_cards": True,
            "generate_portraits": False,
        },
        "portrait_asset_stage": {
            "deferred": True,
            "views_per_character": 3,
            "estimated_images": None,
            "estimated_cost_cny": None,
            "note": "剧本任务不出图；实际新增人物经模型确认后，定妆包在独立资产环节确认费用并补齐",
        },
    }


def _screenplay_rebuild_state(snapshot: dict, exc) -> dict:
    """Project a typed stale error without discarding runtime resume state."""
    return {
        **snapshot,
        **exc.http_detail(
            recommended_action=snapshot["recommended_action"],
        ),
    }


def _screenplay_status_snapshot(ep, *, shot_count: int, production: dict | None = None) -> dict:
    production = production or {}
    screenplay_active = bool(production.get("task_active"))
    storyboard_active = task_registry.active("storyboard", ep["id"]) or ep["status"] == "scripting"
    screenplay_status = ep["screenplay_status"] or "pending"
    screenplay_ready = _screenplay_ready(ep)
    can_resume = bool(
        production.get("can_resume_repair")
        or production.get("can_resume_baseline")
    )
    checkpoint = shot_count if shot_count > 0 and ep["status"] == "script_failed" else 0
    cancelling = str(ep.get("screenplay_error") or "").startswith("CANCELLING:")
    if cancelling:
        code, message, action = "screenplay_cancelling", "正在取消剧本任务，等待 worker 退出", "view_cancel_progress"
    elif bool(ep.get("screenplay_publish_fence")):
        code, message, action = "save_stopping_downstream", "正在安全停止下游任务", "view_save_progress"
    elif screenplay_active and screenplay_status == "queued":
        code, message, action = (
            "screenplay_queued",
            "剧本任务排队中，尚未占用模型生成槽位",
            "stop_screenplay",
        )
    elif screenplay_active:
        operation = production.get("operation") or "baseline"
        code = (
            "baseline_running"
            if operation in {"baseline", "baseline_rebuild"}
            else "finalize_running"
        )
        message = str(
            production.get("phase_label")
            or (
                "正在生成首版剧本"
                if operation in {"baseline", "baseline_rebuild"}
                else "正在完成剧本"
            )
        )
        action = "stop_screenplay"
    elif can_resume:
        resume_point = (
            str(production.get("mode_label") or "继续首版生成")
            if production.get("can_resume_baseline")
            else str(production.get("mode_label") or "继续完整剧本校验")
        )
        stop_reason = str(production.get("stage_stop_reason") or "paused")
        if stop_reason == "failed":
            code = "workflow_failed_recoverable"
            message = f"剧本流程因技术异常中断，可执行：{resume_point}"
        elif stop_reason == "blocked":
            code = "workflow_gate_blocked"
            message = f"剧本生产门禁未通过，可执行：{resume_point}"
        else:
            code = "workflow_paused"
            message = f"剧本流程已暂停，可执行：{resume_point}"
        action = "resume_screenplay"
    elif screenplay_status == "repairing":
        code, message, action = (
            "repair_restart_required",
            "当前无兼容 checkpoint，将重新走生成预检；旧工作副本与证据将保留",
            "generate_screenplay",
        )
    elif screenplay_status == "ready" and not screenplay_ready:
        code, message, action = (
            "qa_certificate_invalid",
            "上游版本已变化，需重新校验剧本并签发完成凭证",
            "refresh",
        )
    elif screenplay_status == "ready" and storyboard_active:
        code, message, action = "ready_storyboard_running", "剧本已交付｜分镜生成中", "view_storyboard"
    elif screenplay_status == "ready" and checkpoint:
        code, message, action = "ready_storyboard_failed", f"剧本已交付｜分镜停在第 {checkpoint} 镜", "resume_storyboard"
    elif screenplay_status == "ready" and shot_count == 0:
        code, message, action = "ready_storyboard_empty", "剧本已交付，尚无分镜", "generate_storyboard"
    elif screenplay_status == "ready" and ep["status"] == "scripted":
        code, message, action = "ready_storyboard_review", "剧本已交付｜分镜待人工确认", "view_storyboard"
    elif screenplay_status == "ready" and ep["status"] in {"confirmed", "generating", "done"}:
        code, message, action = "ready_storyboard_confirmed", "剧本已交付｜分镜已确认", "view_storyboard"
    elif screenplay_status == "ready":
        code, message, action = "ready", "剧本已交付", "view_storyboard"
    elif screenplay_status in {"pending", "failed"}:
        code, message, action = "pending", "尚未生成可交付剧本", "generate_screenplay"
    else:
        code, message, action = "unknown", "状态同步中", "refresh"
    return {
        "version": int(ep.get("screenplay_snapshot_version") or 0),
        "code": code,
        "message": message,
        "recommended_action": action,
        "can_resume": can_resume,
        "screenplay_status": screenplay_status,
        "storyboard_status": ep["status"],
        "screenplay_run_id": ep.get("active_screenplay_run_id"),
        "storyboard_run_id": ep.get("active_storyboard_run_id"),
        "checkpoint_shot": checkpoint or None,
        "storyboard_running": storyboard_active,
        "publish_blocked": storyboard_active or bool(ep.get("screenplay_publish_fence")),
        "reason": "分镜运行中可继续编辑草稿，发布需先安全停止下游" if storyboard_active else "",
    }


_SCREENPLAY_REBUILD_ERROR_UNSET = object()


def _published_screenplay_revalidation_eligibility(
    ep: dict,
    *,
    conn=None,
) -> dict:
    """Resolve whether immutable published content can enter revalidation."""
    from app.errors import ArtifactNeedsRebuildError
    from app.production.patch import screenplay_from_artifact_record
    from app.production.screenplay_authority import (
        assert_screenplay_matches_validated_v7_source,
        published_stale_screenplay_rebuild_error,
    )

    db = conn or get_conn()
    episode_id = str(ep["id"])
    artifact_id = str(ep.get("published_screenplay_artifact_id") or "")

    def blocked(code: str, message: str, *, error=None) -> dict:
        return {
            "eligible": False,
            "code": code,
            "message": message,
            "artifact_id": artifact_id or None,
            "artifact": None,
            "screenplay": None,
            "error": error,
            "rebuild_error": (
                error
                if isinstance(error, ArtifactNeedsRebuildError)
                else None
            ),
        }

    if not artifact_id:
        return blocked(
            "published_screenplay_missing",
            "当前剧本没有可复验的 published Artifact",
        )
    try:
        rebuild_error = published_stale_screenplay_rebuild_error(ep, conn=db)
    except Exception as exc:  # noqa: BLE001 - eligibility must fail closed
        return blocked(
            "published_screenplay_revalidation_check_failed",
            "published 剧本复验资格检查失败，请刷新后重试",
            error=exc,
        )
    if rebuild_error is not None:
        return blocked(
            rebuild_error.code,
            str(rebuild_error),
            error=rebuild_error,
        )

    try:
        artifact = evidence_repository.get_artifact(artifact_id, conn=db)
    except Exception as exc:  # noqa: BLE001 - eligibility must fail closed
        return blocked(
            "published_screenplay_revalidation_check_failed",
            "published 剧本复验资格检查失败，请刷新后重试",
            error=exc,
        )
    if artifact is None:
        return blocked(
            "published_screenplay_artifact_missing",
            "published 剧本指向的 Artifact 不存在",
        )
    if (
        artifact.get("type") != "screenplay_document"
        or artifact.get("scope_type") != "episode"
        or artifact.get("scope_id") != episode_id
    ):
        return blocked(
            "published_screenplay_authority_invalid",
            "published 剧本 Artifact 的类型或作用域不匹配",
        )
    if artifact.get("status") != "approved":
        return blocked(
            "published_screenplay_not_approved",
            "published 剧本 Artifact 未处于 approved 状态",
        )
    try:
        screenplay = screenplay_from_artifact_record(artifact)
    except Exception as exc:  # noqa: BLE001 - parse failures are ineligible
        return blocked(
            "published_screenplay_unreadable",
            "published 剧本 Artifact 无法按当前合同解析",
            error=exc,
        )
    try:
        assert_screenplay_matches_validated_v7_source(
            episode_id=episode_id,
            artifact=artifact,
            screenplay=screenplay,
            conn=db,
            mark_stale=False,
        )
    except ArtifactNeedsRebuildError as exc:
        return blocked(exc.code, str(exc), error=exc)
    except Exception as exc:  # noqa: BLE001 - unknown checks are ineligible
        return blocked(
            "published_screenplay_revalidation_check_failed",
            "published 剧本复验资格检查失败，请刷新后重试",
            error=exc,
        )
    return {
        "eligible": True,
        "code": "published_screenplay_revalidation_eligible",
        "message": "published 剧本可从原文档进入重新校验",
        "artifact_id": artifact_id,
        "artifact": artifact,
        "screenplay": screenplay,
        "error": None,
        "rebuild_error": None,
    }


def _screenplay_authority_state(
    ep,
    *,
    shot_count: int,
    production: dict | None = None,
    rebuild_error=_SCREENPLAY_REBUILD_ERROR_UNSET,
) -> dict:
    from app.production.screenplay_authority import (
        published_stale_screenplay_rebuild_error,
    )

    snapshot = _screenplay_status_snapshot(
        ep,
        shot_count=shot_count,
        production=production,
    )
    if (
        rebuild_error is not _SCREENPLAY_REBUILD_ERROR_UNSET
        and rebuild_error is not None
    ):
        return _screenplay_rebuild_state(snapshot, rebuild_error)
    if (
        snapshot["code"] == "qa_certificate_invalid"
        and not snapshot["can_resume"]
    ):
        eligibility = _published_screenplay_revalidation_eligibility(dict(ep))
        if eligibility["rebuild_error"] is not None:
            return _screenplay_rebuild_state(
                snapshot,
                eligibility["rebuild_error"],
            )
        if eligibility["eligible"]:
            return {
                **snapshot,
                "recommended_action": "resume_screenplay",
                "can_resume": True,
                "resume_capability": "published_screenplay_revalidation",
            }
        return {
            **snapshot,
            "code": eligibility["code"],
            "message": eligibility["message"],
            "recommended_action": "refresh",
            "can_resume": False,
            "blocker": {
                "code": eligibility["code"],
                "message": eligibility["message"],
                "artifact_id": eligibility["artifact_id"],
            },
        }
    if rebuild_error is _SCREENPLAY_REBUILD_ERROR_UNSET:
        rebuild_error = published_stale_screenplay_rebuild_error(ep)
    if rebuild_error is None:
        return snapshot
    return _screenplay_rebuild_state(snapshot, rebuild_error)


@router.get("/episodes/{episode_id}/screenplay/status")
def screenplay_lightweight_status(episode_id: str):
    """运行期轻量状态：不返回正文、台词库、镜头或证据。"""
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"])
    production = _screenplay_production_state(episode_id)
    snapshot = _screenplay_authority_state(
        ep,
        shot_count=shot_count,
        production=production,
    )
    return {
        "id": episode_id,
        "screenplay_status": ep["screenplay_status"],
        "screenplay_error": ep["screenplay_error"],
        "screenplay_updated_at": ep["screenplay_updated_at"],
        "status": ep["status"],
        "script_error": ep["script_error"],
        "shot_count": shot_count,
        "active_storyboard_run_id": ep.get("active_storyboard_run_id"),
        "screenplay_production": production,
        "screenplay_state": snapshot,
        "active": bool(
            production.get("task_active")
            or snapshot["storyboard_running"]
            or snapshot["code"] in {"screenplay_cancelling", "save_stopping_downstream"}
        ),
    }


def _screenplay_task_active(episode_id: str) -> bool:
    if task_registry.active("screenplay", episode_id):
        return True
    conn = get_conn()
    episode = conn.execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    return bool(
        episode
        and evidence_repository.get_active_scoped_run(
            episode["active_screenplay_run_id"],
            workflow_type="screenplay",
            scope_type="episode",
            scope_id=episode_id,
            conn=conn,
        )
    )


def _cancel_persisted_screenplay_run(
    episode_id: str,
    run_id: str | None,
    *,
    message: str,
) -> bool:
    run = evidence_repository.get_active_scoped_run(
        run_id,
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=episode_id,
    )
    if not run:
        return False
    try:
        WorkflowRecorder(str(run["id"])).cancel(message)
        return True
    except StateConflict:
        latest = evidence_repository.get_run(str(run["id"]))
        if latest and latest.get("status") in {
            "CANCELLED", "FAILED", "SUCCEEDED", "PARTIAL",
        }:
            return False
        raise


def _assert_screenplay_run_owner(
    episode_id: str,
    *,
    run_id: str | None = None,
) -> None:
    if run_id is None:
        from app.observability.tracing import current_trace

        run_id = current_trace().run_id
    if not run_id:
        return
    row = get_conn().execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    actual = str(row["active_screenplay_run_id"] or "") if row else "missing"
    if not row or actual != run_id:
        raise StateConflict(
            "screenplay_owner",
            episode_id,
            {run_id},
            actual,
        )


@router.put("/episodes/{episode_id}/target-duration")
def update_episode_target_duration(
    episode_id: str, body: dict | None = Body(None)
):
    """修改首版剧本生成前的整集节奏预算。"""
    body = _as_body_dict(body)
    raw_target = body.get("target_duration_s")
    if isinstance(raw_target, bool):
        raw_target = None
    try:
        numeric_target = float(raw_target)
        target = int(numeric_target) if numeric_target.is_integer() else -1
    except (TypeError, ValueError, OverflowError):
        target = -1
    suggested = list(config.EPISODE_TARGET_CHOICES)
    if (
        target < config.EPISODE_TARGET_MIN_S
        or target % config.EPISODE_TARGET_STEP_S != 0
    ):
        raise HTTPException(422, {
            "code": "invalid_episode_target_duration",
            "message": (
                f"目标时长至少为 {config.EPISODE_TARGET_MIN_S} 秒，"
                f"按 {config.EPISODE_TARGET_STEP_S} 秒递增；不设上限"
            ),
            "minimum_s": config.EPISODE_TARGET_MIN_S,
            "step_s": config.EPISODE_TARGET_STEP_S,
            "suggested_choices": suggested,
        })

    ep = dict(_episode_or_404(episode_id))
    current = int(ep.get("target_duration_s") or config.EPISODE_TARGET_DEFAULT_S)
    if target == current:
        return {
            "saved": True,
            "unchanged": True,
            "episode_id": episode_id,
            "target_duration_s": current,
            "suggested_choices": suggested,
            "constraint_version": int(ep.get("screenplay_constraint_version") or 0),
        }

    production = _screenplay_production_state(episode_id)
    active_runs = [
        kind for kind in ("screenplay", "storyboard", "video_completion")
        if task_registry.active(kind, episode_id)
    ]
    if active_runs or production.get("task_active"):
        raise HTTPException(409, {
            "code": "episode_target_duration_locked",
            "message": "本集正在制作中，不能同时修改目标时长；请等待任务结束后重试",
            "active_runs": active_runs,
        })
    if (
        production.get("can_resume_repair")
        or production.get("can_resume_baseline")
    ):
        raise HTTPException(409, {
            "code": "episode_target_duration_locked",
            "message": "本集已有可恢复的剧本生产状态，目标时长已被该约束版本锁定",
        })

    conn = get_conn()
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"])
    has_screenplay = bool(ep.get("screenplay_json") or ep.get("screenplay_artifact_id"))
    downstream_status = ep.get("status") not in {"planned", "drafting"}
    locked_status = ep.get("screenplay_status") not in {"pending", "failed"}
    if has_screenplay or shot_count or downstream_status or locked_status or ep.get("screenplay_publish_fence"):
        raise HTTPException(409, {
            "code": "episode_target_duration_locked",
            "message": "本集已有剧本或下游产物；为避免版本不一致，不能直接修改目标时长",
            "screenplay_status": ep.get("screenplay_status"),
            "storyboard_status": ep.get("status"),
            "shot_count": shot_count,
        })

    cursor = conn.execute(
        "UPDATE episodes SET target_duration_s=?, "
        "planning_target_duration_s=?, "
        "planning_duration_source='episode_target_duration_editor', "
        "target_duration_authority='planning_estimate', "
        "screenplay_constraint_version=screenplay_constraint_version+1, "
        "screenplay_snapshot_version=screenplay_snapshot_version+1 "
        "WHERE id=? AND screenplay_publish_fence=0 "
        "AND screenplay_status IN ('pending','failed') "
        "AND status IN ('planned','drafting') "
        "AND COALESCE(screenplay_json,'')='' "
        "AND COALESCE(screenplay_artifact_id,'')='' "
        "AND NOT EXISTS(SELECT 1 FROM shots WHERE episode_id=?)",
        (target, target, episode_id, episode_id),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise HTTPException(409, {
            "code": "episode_target_duration_conflict",
            "message": "本集状态刚刚发生变化，目标时长未修改；请刷新后重试",
        })
    saved = dict(_episode_or_404(episode_id))
    return {
        "saved": True,
        "unchanged": False,
        "episode_id": episode_id,
        "previous_target_duration_s": current,
        "target_duration_s": target,
        "suggested_choices": suggested,
        "constraint_version": int(saved.get("screenplay_constraint_version") or 0),
        "snapshot_version": int(saved.get("screenplay_snapshot_version") or 0),
    }


def _screenplay_production_state(episode_id: str) -> dict:
    """Expose the actual production phase used by ScriptPage controls.

    ``screenplay_status`` is a delivery status and cannot distinguish the one
    allowed Baseline call from later Patch activations.  The revision ledger is
    the authority for that distinction.
    """
    from app.production.revision import screenplay_production_state

    return screenplay_production_state(episode_id)


def _project_screenplay_runtime_failure(
    episode_id: str,
    *,
    run_id: str | None,
    public_error: str,
) -> bool:
    """Preserve durable baseline evidence and project an actionable UI state."""
    conn = get_conn()
    production_state = _screenplay_production_state(episode_id)
    has_recovery_point = bool(
        production_state.get("has_working_baseline")
        or production_state.get("has_resumable_baseline")
    )
    if not has_recovery_point:
        _clear_unpublished_screenplay_ir(
            episode_id,
            run_id=run_id,
        )
    conn.execute(
        "UPDATE episodes SET screenplay_status=?, screenplay_error=?, "
        "screenplay_updated_at=? WHERE id=?",
        (
            "repairing" if has_recovery_point else "failed",
            (
                "剧本流程在后续阶段暂停；已验证产物和安全恢复点已保留，"
                f"可继续流程。{public_error}"
                if has_recovery_point else public_error
            ),
            now(),
            episode_id,
        ),
    )
    conn.commit()
    return has_recovery_point


def _screenplay_fallback_status(ep) -> str:
    data = dict(ep)
    if not ep["screenplay_json"]:
        return "repairing" if data.get("working_screenplay_artifact_id") else "pending"
    artifact_id = ep["screenplay_artifact_id"] if "screenplay_artifact_id" in ep.keys() else None
    if not artifact_id:
        return "ready"
    artifact = evidence_repository.get_artifact(artifact_id)
    if artifact and artifact["status"] == "approved":
        return "ready"
    return "repairing" if data.get("working_screenplay_artifact_id") else "failed"


def recover_screenplay_tasks() -> int:
    """Resume only work that was actually interrupted by a service restart."""
    from app.errors import ArtifactNeedsRebuildError
    from app.generation_concurrency import PRIORITY_RECOVERY
    from app.production.patch import load_screenplay_from_artifact
    from app.production.screenplay_authority import (
        resolve_current_screenplay_authority,
    )

    conn = get_conn()
    published_rows = conn.execute(
        """SELECT *
             FROM episodes
            WHERE screenplay_artifact_id IS NOT NULL
              AND (
                    screenplay_status='ready'
                    OR (
                        screenplay_status='failed'
                        AND active_screenplay_run_id IS NULL
                    )
                  )"""
    ).fetchall()
    for published in published_rows:
        published_artifact_id = str(
            published["screenplay_artifact_id"] or ""
        )
        try:
            if published_artifact_id:
                load_screenplay_from_artifact(published_artifact_id)
            has_immutable_authority = bool(
                published["screenplay_completion_certificate_id"]
                or published["screenplay_production_revision_id"]
            )
            if has_immutable_authority:
                resolve_current_screenplay_authority(
                    str(published["id"]),
                    conn=conn,
                )
            if published["screenplay_status"] == "ready":
                valid = _screenplay_ready(dict(published))
            elif not has_immutable_authority:
                resolve_current_screenplay_authority(
                    str(published["id"]),
                    conn=conn,
                )
                valid = True
            else:
                valid = True
        except ArtifactNeedsRebuildError as exc:
            conn.execute(
                "UPDATE episodes SET screenplay_status='failed',"
                "screenplay_error=?,active_screenplay_run_id=NULL,"
                "screenplay_updated_at=? WHERE id=? "
                "AND screenplay_artifact_id=?",
                (
                    str(exc),
                    now(),
                    published["id"],
                    published_artifact_id,
                ),
            )
            continue
        except Exception:
            valid = False
        if valid:
            if published["screenplay_status"] == "failed":
                conn.execute(
                    "UPDATE episodes SET screenplay_status='ready',"
                    "screenplay_error=NULL,screenplay_updated_at=? "
                    "WHERE id=? AND screenplay_status='failed' "
                    "AND active_screenplay_run_id IS NULL "
                    "AND screenplay_artifact_id=?",
                    (
                        now(),
                        published["id"],
                        published["screenplay_artifact_id"],
                    ),
                )
            continue
        if published["screenplay_status"] != "ready":
            continue
        conn.execute(
            "UPDATE episodes SET screenplay_status='failed',screenplay_error=?,"
            "active_screenplay_run_id=NULL,screenplay_updated_at=? "
            "WHERE id=? AND screenplay_status='ready' "
            "AND screenplay_artifact_id=?",
            (
                "现有完成凭证未通过当前生产门禁；旧剧本与证据已保留，"
                "请重新发起剧本生成",
                now(),
                published["id"],
                published["screenplay_artifact_id"],
            ),
        )
    conn.commit()
    rows = conn.execute(
        """SELECT e.*
             FROM episodes e
            WHERE (
                    e.screenplay_status IN ('queued','running')
                    AND COALESCE(e.screenplay_error, '') NOT LIKE 'CANCELLING:%'
                    AND NOT EXISTS(
                        SELECT 1 FROM workflow_runs cancelled
                         WHERE cancelled.id=e.active_screenplay_run_id
                           AND cancelled.status IN ('CANCELLED','CANCELLING')
                    )
                  )
               OR (
                    e.screenplay_status='repairing'
                    AND EXISTS(
                        SELECT 1 FROM workflow_runs wr
                         WHERE wr.id=e.active_screenplay_run_id
                           AND wr.workflow_type='screenplay'
                           AND wr.status='PAUSED_EXTERNAL'
                           AND wr.recovered_by_run_id IS NULL
                    )
               )"""
    ).fetchall()
    resumed = 0
    for row in rows:
        episode_id = row["id"]
        # Startup recovery deliberately ignores a persisted PAUSED_EXTERNAL
        # owner: there cannot yet be a local worker, and this loop is the code
        # responsible for replacing that interrupted run.
        if task_registry.active("screenplay", episode_id):
            continue
        orphan_run = conn.execute(
            "SELECT status FROM workflow_runs WHERE id=?",
            (row["active_screenplay_run_id"],),
        ).fetchone()
        if orphan_run and orphan_run["status"] == "CREATED":
            try:
                WorkflowRecorder(row["active_screenplay_run_id"]).cancel(
                    "服务重启前尚在排队，已由恢复运行接管"
                )
            except StateConflict:
                pass
        from app.production.revision import (
            resolve_screenplay_resume_eligibility,
        )

        eligibility = resolve_screenplay_resume_eligibility(
            episode_id,
            conn=conn,
        )
        if not eligibility.resumable:
            conn.execute(
                "UPDATE episodes SET screenplay_status='repairing',screenplay_error=?,"
                "active_screenplay_run_id=NULL,screenplay_updated_at=? "
                "WHERE id=? AND active_screenplay_run_id=?",
                (
                    eligibility.reason,
                    now(),
                    episode_id,
                    row["active_screenplay_run_id"],
                ),
            )
            conn.commit()
            continue
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='screenplay' "
            "AND scope_type='episode' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        batch_parent = conn.execute(
            "SELECT parent.id,parent.status FROM workflow_runs child "
            "JOIN workflow_runs parent ON parent.id=child.parent_run_id "
            "WHERE child.id=? AND parent.workflow_type='screenplay_batch' "
            "AND parent.status IN ('RUNNING','PAUSED_EXTERNAL')",
            (row["active_screenplay_run_id"],),
        ).fetchone()
        batch_run_id = batch_parent["id"] if batch_parent else None
        if batch_parent and batch_parent["status"] == "PAUSED_EXTERNAL":
            try:
                WorkflowRecorder(batch_run_id).start()
            except StateConflict:
                pass
        recorder = None
        try:
            recorder = _new_screenplay_recorder(
                episode_id,
                requested_by="recovery",
                trigger_type="resume",
                parent_run_id=batch_run_id or (parent["id"] if parent else None),
            )
            _spawn_screenplay_activation(
                episode_id,
                recorder,
                project_id=row["project_id"],
                status="queued",
                message=f"{eligibility.label}已排队，等待文本生成槽位",
                preserve_started_at=True,
                expected_active_run_id=row["active_screenplay_run_id"],
                resume_eligibility=eligibility,
                task_factory=lambda episode_id=episode_id, recorder=recorder, batch_run_id=batch_run_id: _screenplay_guarded(
                    episode_id,
                    recorder,
                    priority=PRIORITY_RECOVERY,
                    batch_run_id=batch_run_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - recover remaining episodes independently
            public = errors.record_and_format(
                exc,
                action="screenplay_recovery_spawn",
                context={"episode_id": episode_id, "previous_run_id": row["active_screenplay_run_id"]},
            )
            retry_status = "repairing" if row["screenplay_status"] == "repairing" else "failed"
            retry_hint = (
                "工作副本已保留，请点击「继续剧本流程」"
                if retry_status == "repairing"
                else "原文与约束已保留，请重新发起首版剧本"
            )
            conn.execute(
                "UPDATE episodes SET screenplay_status=?, screenplay_error=?, "
                "active_screenplay_run_id=NULL, screenplay_updated_at=? "
                "WHERE id=? AND active_screenplay_run_id=?",
                (
                    retry_status,
                    f"服务重启后的自动恢复未能启动；{retry_hint}。{public}",
                    now(),
                    episode_id,
                    row["active_screenplay_run_id"],
                ),
            )
            conn.commit()
            continue
        resumed += 1
    return resumed


async def _screenplay_character_discovery(
    episode_id: str,
    source_text: str,
    *,
    draft_text: str = "",
) -> dict:
    """Run the required incremental cast pass for one screenplay generation."""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("新人物发现", ["剧集不存在"])
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if not project:
        raise StageError("新人物发现", ["项目不存在"])
    _assert_screenplay_run_owner(episode_id)
    if not (project["bible_json"] or "").strip():
        # 剧本允许先于完整人物谱生产，但人物身份不能因此绕过预检。先原子写入
        # 最小骨架，后续仍由既有增量流程建文字卡；bible_status 保持原值，
        # 不把这个骨架伪装成用户已完成的人物谱。
        placeholder = _project_bible_or_placeholder(project)
        conn.execute(
            "UPDATE projects SET bible_json=? "
            "WHERE id=? AND COALESCE(TRIM(bible_json), '')=''",
            (placeholder.model_dump_json(), ep["project_id"]),
        )
        conn.commit()
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
        ).fetchone()
    bible = _project_bible_or_placeholder(project)
    from app.portraits import (
        ensure_cards_for_text,
        persist_screenplay_character_resolutions,
        screenplay_identity_scope_fingerprint,
    )

    try:
        result = await ensure_cards_for_text(
            ep["project_id"],
            ep["episode_no"],
            source_text,
            bible,
            draft_text=draft_text,
            generate_portraits=False,
            write_guard=lambda: _assert_screenplay_run_owner(episode_id),
        )
    except (StageError, StateConflict):
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成剧本阶段可恢复诊断
        from app.errors import code_ref

        public = code_ref(
            exc,
            action="screenplay_character_discovery",
            context={"episode_id": episode_id, "project_id": ep["project_id"]},
        )
        raise StageError(
            "新人物发现",
            [f"人物身份模型暂未完成本集预检，请在剧本阶段重试（{public}）"],
        ) from exc
    if result.get("errors"):
        raise StageError("新人物发现", list(result["errors"]))
    _assert_screenplay_run_owner(episode_id)
    from app.observability.tracing import current_trace

    expected_run_id = current_trace().run_id
    result["resolutions"] = persist_screenplay_character_resolutions(
        conn,
        episode_id,
        result.get("resolutions") or [],
        retire_legacy_future_identity=True,
        expected_active_run_id=expected_run_id,
        replace_identity_scope=screenplay_identity_scope_fingerprint(
            int(ep["episode_no"]), source_text
        ),
    )
    for warning in result.get("warnings") or []:
        errors.log_error(
            None,
            action="screenplay_character_discovery_warning",
            context={
                "project_id": ep["project_id"],
                "episode_id": episode_id,
                "episode_no": ep["episode_no"],
            },
            message=warning,
        )
    return result


async def _screenplay_task(
    episode_id: str,
    *,
    preflight_result: dict | None = None,
) -> EpisodeScreenplay | None:
    """一次 Baseline + Production Repair Agent 局部自愈；仅证书通过后写入 published。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    try:
        _assert_screenplay_run_owner(episode_id)
        ep_data = dict(ep)
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        source_text = _episode_source_text(conn, ep)
        from app.production.screenplay_authority import (
            screenplay_authorized_source_chapters,
        )

        ep_data["authorized_source_chapters"] = (
            screenplay_authorized_source_chapters(
                episode_id,
                conn=conn,
            )
        )
        if preflight_result is None:
            preflight_result = await _screenplay_character_discovery(episode_id, source_text)
        # 身份映射必须随 Baseline、恢复 Patch 和手工发布重放。
        # 持久化的只是姓名决议与证据，不传递后续章节剧情。
        from app.portraits import (
            load_screenplay_character_resolutions,
            merge_screenplay_character_resolutions,
            screenplay_character_resolutions_for_source,
        )
        ep_data["character_resolutions"] = (
            screenplay_character_resolutions_for_source(
                merge_screenplay_character_resolutions(
                    load_screenplay_character_resolutions(conn, episode_id),
                    preflight_result.get("resolutions") or [],
                ),
                episode_no=int(ep_data.get("episode_no") or 0),
                source_text=source_text,
            )
        )
        # Other episodes can add a character while this run is in discovery.
        # Always bind generation to the latest persisted Bible authority.
        p = conn.execute(
            "SELECT * FROM projects WHERE id=?",
            (ep["project_id"],),
        ).fetchone()
        bible = _project_bible_or_placeholder(p)
        from app.portraits import (
            bible_with_pending_characters_for_text,
            bible_with_provisional_characters,
        )
        bible = bible_with_provisional_characters(bible, preflight_result)
        bible = bible_with_pending_characters_for_text(
            ep["project_id"], bible, source_text,
        )
        compact_target = _storyboard_target_for_source(ep_data.get("target_duration_s"), len(source_text))
        if compact_target != ep_data.get("target_duration_s"):
            # 单一真源：UPDATE 与内存快照 ep_data 都由 _apply_compact_target
            # 从同一份 _compact_target_columns 取值，保证“写了什么就同步什么”，
            # 杜绝内存快照与 DB 漂移（历史上 planning 停留在非整十旧值会导致
            # 下游 duration-expansion CAS 冲突）。
            _apply_compact_target(conn, episode_id, ep_data, compact_target)
        prev = conn.execute(
            "SELECT cliffhanger FROM episodes WHERE project_id=? AND episode_no=?",
            (ep["project_id"], ep["episode_no"] - 1)).fetchone()

        # Delivery 状态必须与真实 production phase 一致：Baseline 尚未落库时
        # 仍是 running；已有 working baseline 时从确定性阶段继续。
        production_state = _screenplay_production_state(episode_id)
        conn.execute(
            "UPDATE episodes SET screenplay_status=?, screenplay_error=?, screenplay_updated_at=? WHERE id=?",
            (
                "running",
                (
                    "从完整工作副本继续结构校验、评分与发布"
                    if production_state["operation"] == "finalize"
                    else "从已验证场次恢复首版生成"
                    if production_state.get("can_resume_baseline")
                    else "正在生成人物上下文与首版剧本"
                ),
                now(),
                episode_id,
            ),
        )
        conn.commit()

        from app.production.screenplay_repair import run_screenplay_production
        from app.observability.tracing import current_trace

        run_id = None
        try:
            run_id = current_trace().run_id
        except Exception:  # noqa: BLE001
            run_id = None

        # 有 active revision 时继续工作副本；全量 Baseline 只允许一次。
        resume = True
        script = await run_screenplay_production(
            episode_id=episode_id,
            episode=ep_data,
            source_text=source_text,
            bible=bible,
            prev_ending=prev["cliffhanger"] if prev else "",
            run_id=run_id,
            resume=resume,
        )

        # run_screenplay_production 在成功时已 publish；若仍 repairing 则不要写成 ready
        row = conn.execute(
            "SELECT screenplay_status, screenplay_json FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row and row["screenplay_status"] == "ready" and row["screenplay_json"]:
            return _load_screenplay(row) or script

        # 未发布：保持 repairing，不写 warning 候选到页面交付位
        if row and row["screenplay_status"] == "repairing":
            # 工作副本仅存 working artifact；兼容字段不覆盖 published screenplay_json
            return script

        # 兜底：若 publish 已写入
        return script
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            # 进程热更/停机不是用户取消；保留 running 让新 worker 续跑。
            raise
        from app.observability.tracing import current_trace
        try:
            current_run_id = current_trace().run_id
        except Exception:  # noqa: BLE001
            current_run_id = None
        owner = conn.execute(
            "SELECT active_screenplay_run_id FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if (
            not current_run_id
            or (
                owner is not None
                and owner["active_screenplay_run_id"] == current_run_id
            )
        ):
            from app.production.revision import get_active_production_revision

            active_revision = get_active_production_revision(
                episode_id, "screenplay"
            )
            checkpoint = dict(
                active_revision.checkpoint_json or {}
            ) if active_revision else {}
            has_validated_shards = any(
                isinstance(item, dict)
                and item.get("status") == "validated"
                for item in checkpoint.get("shards") or []
            )
            has_document = bool(
                active_revision
                and active_revision.baseline_done
                and active_revision.working_artifact_id
            )
            if not has_document and not has_validated_shards:
                _clear_unpublished_screenplay_ir(
                    episode_id,
                    run_id=current_run_id,
                )
            if active_revision:
                from app.production.revision import save_checkpoint

                save_checkpoint(active_revision.id, {
                    **checkpoint,
                    "yield_reason": "user_cancelled",
                    "phase": (
                        "STRUCTURE_VALIDATION" if has_document
                        else checkpoint.get("phase") or "BLUEPRINT_GENERATION"
                    ),
                })
            conn.execute(
                "UPDATE episodes SET screenplay_status=?, screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (
                    "repairing" if has_document else "failed",
                    (
                        "完整剧本校验已取消，工作副本已保留，可继续校验。"
                        if has_document else
                        "首版生成已取消，已验证场次分片已保留，可继续首版生成。"
                        if has_validated_shards else
                        "剧本生成已取消，可重新发起。"
                    ),
                    now(),
                    episode_id,
                ))
            conn.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        from app.observability.tracing import current_trace
        try:
            current_run_id = current_trace().run_id
        except Exception:  # noqa: BLE001
            current_run_id = None
        owner = conn.execute(
            "SELECT active_screenplay_run_id FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if (
            current_run_id
            and (
                owner is None
                or owner["active_screenplay_run_id"] != current_run_id
            )
        ):
            # 已被恢复任务替代的旧协程可能在 socket 返回后才观察到围栏；
            # 它不得覆盖新运行的剧集状态。
            raise
        from app.production.screenplay_repair import ScreenplayNarrativeGateError

        if isinstance(exc, ScreenplayNarrativeGateError):
            # The repair engine has already projected WAITING_HUMAN and saved
            # the working artifact/checkpoint.  Keep that durable recovery
            # point; the Run may fail, but the episode remains resumable.
            raise
        msg = str(exc)
        if msg.startswith("WAITING_INPUT"):
            conn.execute(
                "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (msg[:800], now(), episode_id))
            conn.commit()
            return None
        if type(exc).__name__ == "ScreenplayIdentityGateError":
            _clear_unpublished_screenplay_ir(
                episode_id,
                run_id=current_run_id,
            )
            conn.execute(
                "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, "
                "screenplay_updated_at=? WHERE id=?",
                (msg[:800], now(), episode_id),
            )
            conn.commit()
            return None
        public = errors.record_and_format(exc, action="screenplay_generate", context={"episode_id": episode_id})
        _project_screenplay_runtime_failure(
            episode_id,
            run_id=current_run_id,
            public_error=public,
        )
        return None


def _new_screenplay_recorder(
    episode_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    from app import hiagent
    from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION
    from app.stages import (
        SCREENPLAY_BASELINE_PROMPT_VERSION,
        SCREENPLAY_STRUCTURAL_BOOTSTRAP_ITERATIONS,
    )

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT bible_version FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    source_text = _episode_source_text(conn, ep)
    active_text_provider = hiagent.active_provider("text")
    active_text_model = hiagent.active_model("text", provider=active_text_provider)
    return WorkflowRecorder.create(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            episode_id,
            ep["source_chapters"],
            source_text,
            project["bible_version"] if project else 0,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "contract": f"screenplay@{get_contract('screenplay').version}",
            "max_iterations": SCREENPLAY_STRUCTURAL_BOOTSTRAP_ITERATIONS,
            "stall_rounds": 2,
            "min_quality_gain": 0.03,
            "baseline_only": True,
            "repair_activation_patch_limit": 12,
            "repair_activation_pass_limit": 32,
        },
        config_snapshot={
            "pipeline_version": "screenplay-compact-ir-pipeline-5.0.0",
            "prompt_version": SCREENPLAY_BASELINE_PROMPT_VERSION,
            "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
            "provider": active_text_provider,
            "model": active_text_model,
            "text_generation_concurrency": (
                get_setting("text_generation_concurrency")
                or get_setting("storyboard_concurrency")
                or "10"
            ),
            "duration_policy": "content_derived_unbounded",
            "blueprint_budget_lineage_fingerprint": fingerprint(
                episode_id,
                ep["source_chapters"],
                source_text,
                project["bible_version"] if project else 0,
            ),
            "blueprint_retry_grant_id": "",
            "blueprint_retry_receipts_hash": "",
        },
        parent_run_id=parent_run_id,
    )


def _screenplay_blueprint_budget_projection(
    episode_id: str,
    *,
    run_id: str | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    """Read-only budget/grant projection shared by preflight and activation."""
    from app.source_excerpt import index_source_segments
    from app.stages import (
        BLUEPRINT_SHARD_MIN_TOKENS,
        _BlueprintGenerationBudget,
        _partition_blueprint_segments,
        blueprint_retry_receipts_hash,
    )

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if ep is None:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT bible_version FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    source_text = _episode_source_text(conn, ep)
    input_fp = fingerprint(
        episode_id,
        ep["source_chapters"],
        source_text,
        project["bible_version"] if project else 0,
    )
    revision_row = conn.execute(
        """SELECT id,grant_id FROM production_revisions
             WHERE episode_id=? AND kind='screenplay' AND status='active'
             ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    revision = dict(revision_row) if revision_row is not None else None
    current_grant_id = str(revision.get("grant_id") or "") if revision else ""
    budget = _BlueprintGenerationBudget.from_durable_calls(
        run_id=run_id,
        started_at_epoch=started_at,
        episode_id=episode_id,
        input_fingerprint=input_fp,
        retry_grant_id=current_grant_id,
    )
    if current_grant_id and budget.unknown_receipts:
        grant_row = conn.execute(
            """SELECT issued_by,input_artifact_hash,consumed_at
                 FROM production_grants
                WHERE id=? AND episode_id=? AND kind='screenplay'
                  AND production_revision_id=?
                  AND revoked_at IS NULL AND expires_at>?
                  AND consumed_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM workflow_runs wr
                       WHERE json_extract(
                           wr.config_snapshot_json,
                           '$.blueprint_retry_grant_id'
                       )=production_grants.id
                  )""",
            (
                current_grant_id,
                episode_id,
                str(revision["id"]),
                now(),
            ),
        ).fetchone()
        if (
            grant_row is not None
            and str(grant_row["issued_by"] or "") == "user_retry_approval"
            and str(grant_row["input_artifact_hash"] or "")
            == blueprint_retry_receipts_hash(budget.unknown_receipts)
        ):
            budget.authorize_unknown_retry(current_grant_id)
    # Size the runaway breakers from the same deterministic partition the
    # stage will plan from.  Cached leaves and dynamic splits can only add
    # leaves, so this uncached count is a lower bound: the fence here is never
    # more permissive than the runtime budget, and never rejects an episode
    # purely for being long.
    budget.adopt_shard_plan(
        len(_partition_blueprint_segments(index_source_segments(source_text)))
    )
    token_admissible = (
        budget.charged_output_tokens + BLUEPRINT_SHARD_MIN_TOKENS
        <= budget.max_output_tokens
    )
    call_admissible = budget.provider_calls < budget.max_provider_calls
    return {
        "budget": budget,
        "input_fingerprint": input_fp,
        "revision": revision,
        "current_grant_id": current_grant_id,
        "requires_fresh_retry_grant": budget.requires_fresh_retry_grant,
        "unknown_receipts": budget.unknown_receipts,
        "provider_calls": budget.provider_calls,
        "charged_output_tokens": budget.charged_output_tokens,
        "unknown_output_tokens": budget.unknown_output_tokens,
        "token_admissible": token_admissible,
        "call_admissible": call_admissible,
        "admissible_after_approval": token_admissible and call_admissible,
    }

def _spawn_screenplay_activation(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    project_id: str,
    status: str,
    message: str | None,
    preserve_started_at: bool = False,
    task_factory=None,
    expected_active_run_id: str | None = None,
    clear_unpublished_ir: bool = False,
    resume_eligibility=None,
    authorize_blueprint_retry: bool = False,
    expected_blueprint_unknown_receipts: list[dict[str, Any]] | None = None,
):
    """Atomically claim one episode before registering its in-process task."""
    conn = get_conn()
    previous: dict | None = None
    registered_task = None
    prepared_revision = None
    activation_retry_grant_id = ""
    activation_retry_receipts_hash = ""
    activation_retry_revision_id = ""
    try:
        conn.execute("BEGIN IMMEDIATE")
        activation_stamp = now()
        retry_approval_evidence = (
            _retry_authority.consume_screenplay_command_bus_retry_approval()
        )
        previous_row = conn.execute(
            "SELECT screenplay_status, screenplay_error, screenplay_started_at, "
            "screenplay_updated_at, active_screenplay_run_id, "
            "screenplay_publish_fence, "
            "screenplay_character_resolutions, screenplay_required_dialogues, "
            "screenplay_required_dialogue_occurrences "
            "FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if not previous_row:
            raise ValueError(f"episode not found: {episode_id}")
        previous = dict(previous_row)
        if previous["screenplay_publish_fence"]:
            raise StateConflict(
                "screenplay_publish_fence",
                episode_id,
                {"0"},
                str(previous["screenplay_publish_fence"]),
            )
        previous_run_id = str(
            previous["active_screenplay_run_id"] or ""
        )
        if expected_active_run_id is not None:
            if previous_run_id != str(expected_active_run_id or ""):
                raise StateConflict(
                    "screenplay_owner",
                    episode_id,
                    {str(expected_active_run_id or "")},
                    previous_run_id,
                )
        elif (
            previous["screenplay_status"] in {
                "queued", "running", "repairing",
            }
            and previous_run_id
        ):
            owner = conn.execute(
                "SELECT status FROM workflow_runs WHERE id=?",
                (previous_run_id,),
            ).fetchone()
            if owner and owner["status"] not in {
                "FAILED", "CANCELLED", "SUCCEEDED", "PARTIAL",
            }:
                raise StateConflict(
                    "screenplay_owner",
                    episode_id,
                    {""},
                    previous_run_id,
                )
        if resume_eligibility is not None:
            from app.production.revision import (
                rebase_screenplay_revision_for_resume,
                resolve_screenplay_resume_eligibility,
            )

            current_eligibility = resolve_screenplay_resume_eligibility(
                episode_id,
                conn=conn,
            )
            if (
                current_eligibility.mode != resume_eligibility.mode
                or current_eligibility.revision_action
                != resume_eligibility.revision_action
                or current_eligibility.revision_id
                != resume_eligibility.revision_id
                or current_eligibility.working_artifact_id
                != resume_eligibility.working_artifact_id
            ):
                raise StateConflict(
                    "screenplay_resume_eligibility",
                    episode_id,
                    {resume_eligibility.mode},
                    current_eligibility.mode,
                )
            if current_eligibility.revision_action == "rebase":
                prepared_revision = rebase_screenplay_revision_for_resume(
                    current_eligibility,
                    conn=conn,
                )
        if clear_unpublished_ir:
            _clear_unpublished_screenplay_ir(
                episode_id,
                conn=conn,
                commit=False,
            )
            conn.execute(
                "UPDATE episodes SET screenplay_character_resolutions='[]', "
                "screenplay_required_dialogues='[]', "
                "screenplay_required_dialogue_occurrences='[]' "
                "WHERE id=?",
                (episode_id,),
            )
        budget_projection = _screenplay_blueprint_budget_projection(
            episode_id,
            run_id=recorder.run_id,
            started_at=activation_stamp,
        )
        budget = budget_projection["budget"]
        if budget_projection["requires_fresh_retry_grant"]:
            trusted_retry_approval = bool(
                authorize_blueprint_retry
                and retry_approval_evidence
            )
            from app.stages import blueprint_retry_receipts_hash

            current_receipts_hash = blueprint_retry_receipts_hash(
                budget_projection["unknown_receipts"]
            )

            if (
                trusted_retry_approval
                and str(retry_approval_evidence.get("receipts_hash") or "")
                != current_receipts_hash
            ):
                raise StateConflict(
                    "blueprint_unknown_retry_approval",
                    episode_id,
                    {
                        str(
                            retry_approval_evidence.get("receipts_hash")
                            or ""
                        )
                    },
                    current_receipts_hash,
                )
            if not trusted_retry_approval:
                budget.assert_activation_admissible()
            expected_receipts = expected_blueprint_unknown_receipts or []
            if expected_receipts != budget_projection["unknown_receipts"]:
                raise StateConflict(
                    "blueprint_unknown_retry_receipts",
                    episode_id,
                    {fingerprint(expected_receipts)},
                    fingerprint(budget_projection["unknown_receipts"]),
                )
            revision = budget_projection["revision"]
            if revision is None:
                raise RuntimeError(
                    "BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED: 缺少可绑定的 active revision"
                )
            from app.production.grant import issue_production_grant
            grant, _token = issue_production_grant(
                episode_id=episode_id,
                project_id=project_id,
                production_revision_id=str(revision["id"]),
                kind="screenplay",
                input_artifact_hash=blueprint_retry_receipts_hash(
                    budget_projection["unknown_receipts"]
                ),
                issued_by="user_retry_approval",
                conn=conn,
                commit=False,
            )
            budget.authorize_unknown_retry(grant.grant_id)
            activation_retry_grant_id = grant.grant_id
            activation_retry_receipts_hash = current_receipts_hash
            activation_retry_revision_id = str(revision["id"])
            run_row = conn.execute(
                "SELECT config_snapshot_json FROM workflow_runs WHERE id=?",
                (recorder.run_id,),
            ).fetchone()
            config_snapshot = json.loads(
                run_row["config_snapshot_json"] or "{}"
            ) if run_row is not None else {}
            config_snapshot.update({
                "blueprint_retry_grant_id": grant.grant_id,
                "blueprint_retry_receipts_hash": blueprint_retry_receipts_hash(
                    budget_projection["unknown_receipts"]
                ),
                "blueprint_retry_receipts": list(
                    budget_projection["unknown_receipts"]
                ),
            })
            conn.execute(
                "UPDATE workflow_runs SET config_snapshot_json=?,updated_at=? "
                "WHERE id=?",
                (
                    json.dumps(config_snapshot, ensure_ascii=False),
                    activation_stamp,
                    recorder.run_id,
                ),
            )
        elif budget.unknown_receipts and budget.retry_grant_id:
            # A legacy unconsumed exact grant may authorize one activation.
            # It is consumed below only after the task registry accepts the
            # worker, in the same transaction as the run snapshot and owner.
            activation_retry_grant_id = budget.retry_grant_id
            from app.stages import blueprint_retry_receipts_hash

            activation_retry_receipts_hash = blueprint_retry_receipts_hash(
                budget.unknown_receipts
            )
            revision = budget_projection["revision"]
            activation_retry_revision_id = (
                str(revision["id"]) if revision is not None else ""
            )
            run_row = conn.execute(
                "SELECT config_snapshot_json FROM workflow_runs WHERE id=?",
                (recorder.run_id,),
            ).fetchone()
            config_snapshot = json.loads(
                run_row["config_snapshot_json"] or "{}"
            ) if run_row is not None else {}
            config_snapshot.update({
                "blueprint_retry_grant_id": activation_retry_grant_id,
                "blueprint_retry_receipts_hash": (
                    activation_retry_receipts_hash
                ),
                "blueprint_retry_receipts": list(budget.unknown_receipts),
            })
            conn.execute(
                "UPDATE workflow_runs SET config_snapshot_json=?,updated_at=? "
                "WHERE id=?",
                (
                    json.dumps(config_snapshot, ensure_ascii=False),
                    activation_stamp,
                    recorder.run_id,
                ),
            )
        budget.assert_activation_admissible()
        stamp = activation_stamp
        started_at = (
            previous["screenplay_started_at"]
            if preserve_started_at else stamp
        )
        if started_at is None:
            started_at = stamp
        cursor = conn.execute(
            "UPDATE episodes SET screenplay_status=?, screenplay_error=?, "
            "screenplay_started_at=?, screenplay_updated_at=?, "
            "active_screenplay_run_id=? "
            "WHERE id=? AND COALESCE(active_screenplay_run_id, '')=?",
            (
                status,
                message,
                started_at,
                stamp,
                recorder.run_id,
                episode_id,
                previous_run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise StateConflict(
                "screenplay_owner",
                episode_id,
                {previous_run_id},
                "changed_during_claim",
            )
        task_coro = (
            task_factory()
            if task_factory is not None
            else _screenplay_guarded(episode_id, recorder)
        )
        registered_task = task_registry.spawn(
            "screenplay",
            episode_id,
            task_coro,
            project_id=project_id,
        )
        if activation_retry_grant_id:
            consumed = conn.execute(
                "UPDATE production_grants SET consumed_at=? "
                "WHERE id=? AND episode_id=? AND project_id=? "
                "AND production_revision_id=? AND kind='screenplay' "
                "AND issued_by='user_retry_approval' "
                "AND input_artifact_hash=? "
                "AND consumed_at IS NULL AND revoked_at IS NULL "
                "AND expires_at>? AND EXISTS ("
                " SELECT 1 FROM production_revisions r "
                "  WHERE r.id=production_grants.production_revision_id "
                "    AND r.episode_id=production_grants.episode_id "
                "    AND r.kind='screenplay' AND r.status='active' "
                "    AND r.grant_id=production_grants.id"
                ")",
                (
                    activation_stamp,
                    activation_retry_grant_id,
                    episode_id,
                    project_id,
                    activation_retry_revision_id,
                    activation_retry_receipts_hash,
                    activation_stamp,
                ),
            )
            if consumed.rowcount != 1:
                raise StateConflict(
                    "blueprint_retry_grant_consumption",
                    episode_id,
                    {activation_retry_grant_id},
                    "already_consumed_or_inactive",
                )
        # ``spawn`` only schedules the coroutine; it cannot run until this
        # synchronous function yields back to the event loop.  Commit after the
        # registry accepts it so a registration failure rolls back the owner
        # claim, identity columns and deleted retry-only IR in one transaction.
        conn.commit()
        return prepared_revision
    except BaseException:
        if registered_task is not None:
            task_registry.cancel("screenplay", episode_id)
        if conn.in_transaction:
            conn.rollback()
        try:
            recorder.cancel("任务未能启动，剧集状态已回滚")
        except Exception:  # noqa: BLE001 - rollback must not be hidden by run bookkeeping
            pass
        raise


def _screenplay_context_pack(episode_id: str) -> tuple[list[str], dict]:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    source_text = _episode_source_text(conn, ep)
    bible_artifact = evidence_repository.latest_artifact(
        "character_bible", "project", ep["project_id"]
    )
    mapping_artifact = evidence_repository.latest_artifact(
        "episode_mapping", "project", ep["project_id"]
    )
    from app.production.revision import resolve_screenplay_resume_eligibility

    eligibility = resolve_screenplay_resume_eligibility(
        episode_id,
        conn=conn,
    )
    # Published episode pointers remain populated while an incompatible
    # screenplay is rebuilt. They describe release history, not the input
    # authority of the new Baseline. Only a resolver-approved finalize path
    # may expose a working Document as this step's patch/revalidation input.
    working_artifact_id = (
        eligibility.working_artifact_id
        if eligibility.mode == "finalize"
        else None
    )
    input_ids = [
        artifact_id
        for artifact_id in (
            bible_artifact["id"] if bible_artifact else None,
            mapping_artifact["id"] if mapping_artifact else None,
            working_artifact_id,
        )
        if artifact_id
    ]
    pack = ContextPack(
        goal=f"生成第 {ep['episode_no']} 集可拍剧本",
        metadata={
            "episode_id": episode_id,
            "episode_no": ep["episode_no"],
            "contract_version": get_contract("screenplay").version,
        },
    )
    pack.add_text(
        "source_text",
        source_text,
        limit=SCREENPLAY_SOURCE_BUDGET_CHARS,
        truncation_strategy="head_with_truncation_notice",
    )
    bible_json = project["bible_json"] or "{}"
    pack.add_text(
        "character_bible",
        bible_json,
        limit=max(len(bible_json), 1),
        source_artifact_id=bible_artifact["id"] if bible_artifact else None,
        truncation_strategy="none",
    )
    return list(dict.fromkeys(input_ids)), pack.manifest()


async def _recorded_screenplay_task(
    episode_id: str,
    recorder: WorkflowRecorder,
) -> EpisodeScreenplay | None:
    async def operation(preflight: dict) -> EpisodeScreenplay:
        generated = await _screenplay_task(episode_id, preflight_result=preflight)
        if generated is None:
            row = get_conn().execute(
                "SELECT screenplay_error FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            raise RuntimeError(row["screenplay_error"] if row else "剧本任务未产生结果")
        return generated

    try:
        recorder.start()
        discovery_conn = get_conn()
        discovery_episode = discovery_conn.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        discovery_source = _episode_source_text(
            discovery_conn,
            discovery_episode,
        )
        production_state = _screenplay_production_state(episode_id)
        if (
            production_state["operation"] == "finalize"
            or production_state.get("has_resumable_baseline")
        ):
            # 完整 Document 续跑复用已冻结决议；未知身份由 typed structural
            # QA gate 暴露，禁止回到全章人物发现。
            from app.portraits import (
                load_screenplay_character_resolutions,
                screenplay_identity_resolution_is_current_for_source,
            )

            conn = get_conn()
            persisted_resolutions = [
                item
                for item in load_screenplay_character_resolutions(
                    conn, episode_id,
                )
                if screenplay_identity_resolution_is_current_for_source(
                    item,
                    episode_no=int(discovery_episode["episode_no"]),
                    source_text=discovery_source,
                )
            ]
            preflight = {
                "added": [],
                "resolutions": persisted_resolutions,
                "skipped": (
                    "baseline_identity_already_resolved"
                    if production_state["operation"] == "finalize"
                    else "prebaseline_identity_checkpoint_reused"
                ),
            }
            evidence_repository.append_event(
                recorder.run_id,
                "CHARACTER_DISCOVERY_SKIPPED",
                "info",
                (
                    "已有完整 Document，继续时复用持久化人物决议"
                    if production_state["operation"] == "finalize"
                    else "已有首版安全检查点，继续时复用持久化人物决议"
                ),
                payload={"episode_id": episode_id},
            )
        else:
            _, preflight = await recorder.step(
                "character_discovery",
                lambda: _screenplay_character_discovery(episode_id, discovery_source),
                agent_name="screenplay_character_discovery",
                context_manifest={
                    "episode_id": episode_id,
                    "source_chars": len(discovery_source),
                    "phase": "before_screenplay",
                },
            )
        _assert_screenplay_run_owner(episode_id, run_id=recorder.run_id)
        # Discovery may advance bible_version. Refresh the persisted fingerprint and
        # context pack before the screenplay step so evidence describes the inputs
        # actually used by generation.
        fingerprint_ep = get_conn().execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        fingerprint_project = get_conn().execute(
            "SELECT bible_version FROM projects WHERE id=?", (fingerprint_ep["project_id"],)
        ).fetchone()
        get_conn().execute(
            "UPDATE workflow_runs SET input_fingerprint=?, updated_at=? WHERE id=?",
            (
                fingerprint(
                    episode_id,
                    fingerprint_ep["source_chapters"],
                    discovery_source,
                    fingerprint_project["bible_version"] if fingerprint_project else 0,
                ),
                now(),
                recorder.run_id,
            ),
        )
        get_conn().commit()
        input_artifact_ids, context_manifest = _screenplay_context_pack(episode_id)
        _, script = await recorder.step(
            "screenplay_document",
            lambda: operation(preflight),
            contract_key="screenplay",
            agent_name="screenplay_agent_loop",
            input_artifact_ids=input_artifact_ids,
            context_manifest=context_manifest,
        )
        row = get_conn().execute(
            "SELECT screenplay_status, screenplay_error FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row:
            raise RuntimeError("剧本任务完成后剧集记录不存在")
        if row["screenplay_status"] == "ready":
            recorder.succeed("剧本已通过完成凭证并发布")
        elif row["screenplay_status"] == "repairing":
            recorder.partial(row["screenplay_error"] or "剧本自动修复中/等待续跑")
        else:
            recorder.succeed("剧本任务结束")
        return script
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，剧本运行等待自动续跑")
        else:
            recorder.cancel("剧本生成已取消")
        raise
    except StateConflict:
        # 旧运行已被新的恢复运行围栏；不再回写剧集，也不把这种协调竞态报成内容失败。
        return None
    except Exception as exc:  # noqa: BLE001 -- failure is persisted for Run Center
        from app.production.screenplay_repair import ScreenplayNarrativeGateError

        if isinstance(exc, ScreenplayNarrativeGateError):
            errors.log_error(
                exc,
                action="screenplay_repair",
                context={"episode_id": episode_id, "phase": "narrative_gate"},
            )
            try:
                recorder.fail(exc)
            except StateConflict:
                pass
            return None
        row = get_conn().execute(
            "SELECT screenplay_status, screenplay_error,active_screenplay_run_id "
            "FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row or row["active_screenplay_run_id"] != recorder.run_id:
            # A remote cancellation or a replacement run owns the terminal
            # projection now.  The stale worker may only observe the conflict.
            return None
        if row and row["screenplay_status"] == "running":
            public = errors.record_and_format(
                exc,
                action="screenplay_generate",
                context={"episode_id": episode_id, "phase": "character_discovery"},
            )
            get_conn().execute(
                "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (public, now(), episode_id),
            )
            get_conn().commit()
        elif row and row["screenplay_status"] == "repairing":
            if str(row["screenplay_error"] or "").startswith("WAITING_INPUT"):
                try:
                    recorder.partial(row["screenplay_error"])
                except StateConflict:
                    pass
                return None
            public = errors.record_and_format(
                exc,
                action="screenplay_repair",
                context={"episode_id": episode_id},
            )
            get_conn().execute(
                "UPDATE episodes SET screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (
                    f"剧本后续阶段已暂停，工作副本已保留，可继续流程。{public}",
                    now(),
                    episode_id,
                ),
            )
            get_conn().commit()
        try:
            recorder.fail(exc)
        except StateConflict:
            return None
        return None


def _screenplay_generation_preflight(episode_id: str):
    """首次生成的纯读预检；只报告输入范围和人物资产影响。"""
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    source_text = _episode_source_text(conn, ep)
    chapters = json.loads(ep["source_chapters"] or "[]")
    cast_impact = _screenplay_cast_impact(conn, ep, source_text)
    from app.source_excerpt import index_source_segments

    source_segment_count = len(index_source_segments(source_text))
    estimated_blueprint_shards = max(
        1,
        math.ceil(
            source_segment_count
            / BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD
        ),
    )
    estimated_scene_shards = max(1, math.ceil(source_segment_count / 16))
    reusable_rows = conn.execute(
        """SELECT id,type,status,contract_version,content_json FROM artifacts
             WHERE scope_type='episode' AND scope_id=? AND status='validated'
               AND type IN (
                 'screenplay_identity_discovery','screenplay_narrative_blueprint',
                 'screenplay_identity_registry','screenplay_envelope',
                 'screenplay_scene_shard','screenplay_generation_ir_merged'
               )""",
        (episode_id,),
    ).fetchall()
    from app.production.revision import (
        get_active_production_revision,
        get_production_revision,
        resolve_screenplay_resume_eligibility,
    )

    revision_id = str(ep.get("screenplay_production_revision_id") or "")
    revision = (
        get_production_revision(revision_id)
        if revision_id
        else get_active_production_revision(episode_id, "screenplay")
    )
    eligibility = resolve_screenplay_resume_eligibility(
        episode_id,
        revision=revision,
        conn=conn,
    )
    reusable_shard_ids = {
        str(item.get("normalized_artifact_id") or "")
        for item in eligibility.reusable_checkpoint.get("shards") or []
        if isinstance(item, dict)
    }
    reusable_counts: dict[str, int] = {}
    for reusable_row in reusable_rows:
        row = dict(reusable_row)
        if (
            row["type"] == "screenplay_scene_shard"
            and str(row["id"]) not in reusable_shard_ids
        ):
            continue
        artifact_type = str(row["type"])
        reusable_counts[artifact_type] = (
            reusable_counts.get(artifact_type, 0) + 1
        )
    budget_projection = _screenplay_blueprint_budget_projection(episode_id)
    return {
        "action": "generate_screenplay",
        "episode_id": episode_id,
        "input": {
            "source_chapters": chapters,
            "source_chars": len(source_text),
            "source_segment_count": source_segment_count,
            "estimated_blueprint_shards": estimated_blueprint_shards,
            "estimated_scene_writing_shards": estimated_scene_shards,
        },
        "wait_estimate": None,
        "cost_estimate_cny": None,
        "cast_impact": cast_impact,
        "reusable_validated_artifacts": reusable_counts,
        "blueprint_budget": {
            key: budget_projection[key]
            for key in (
                "requires_fresh_retry_grant",
                "unknown_receipts",
                "provider_calls",
                "charged_output_tokens",
                "unknown_output_tokens",
                "token_admissible",
                "call_admissible",
                "admissible_after_approval",
            )
        },
        "idempotency_scope": {
            "baseline": ep.get("screenplay_artifact_id") or "empty",
            "constraint_version": int(ep.get("screenplay_constraint_version") or 0),
        },
    }


@router.post("/episodes/{episode_id}/screenplay/preflight")
def screenplay_generation_preflight(episode_id: str):
    """返回首次生成的只读输入预检，不创建任务。"""
    return _screenplay_generation_preflight(episode_id)


@router.get("/episodes/{episode_id}/screenplay/draft")
def get_screenplay_draft(episode_id: str):
    _episode_or_404(episode_id)
    row = get_conn().execute(
        "SELECT * FROM screenplay_drafts WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if not row:
        return {"draft": None}
    value = dict(row)
    raw = value.pop("content_json")
    value.pop("constraint_json", None)
    if raw is None:
        return {"draft": None}
    try:
        value["content"] = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"draft": None}
    return {"draft": value}


@router.put("/episodes/{episode_id}/screenplay/draft")
def save_screenplay_draft(episode_id: str, body: dict):
    ep = dict(_episode_or_404(episode_id))
    content = body.get("content")
    if content is None:
        raise HTTPException(422, "草稿内容不能为空")
    baseline = body.get("baseline_artifact_id")
    current = ep.get("screenplay_artifact_id")
    validation: dict = {"baseline_current": str(baseline or "") == str(current or "")}
    if content is not None:
        _, schema_validation = schema_errors(EpisodeScreenplay, content)
        validation["schema_errors"] = schema_validation
    stamp = now()
    conn = get_conn()
    draft_id = str(body.get("draft_id") or new_id("scrdraft"))
    conn.execute(
        """INSERT INTO screenplay_drafts(
               id, episode_id, baseline_artifact_id,
               content_json, constraint_json, dirty_at, updated_at
           ) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(episode_id) DO UPDATE SET
               baseline_artifact_id=excluded.baseline_artifact_id,
               content_json=excluded.content_json,
               constraint_json=excluded.constraint_json,
               dirty_at=excluded.dirty_at, updated_at=excluded.updated_at""",
        (
            draft_id, episode_id, baseline,
            json.dumps(content, ensure_ascii=False),
            "{}",
            stamp, stamp,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM screenplay_drafts WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    return {"saved": True, "draft_id": row["id"], "updated_at": stamp, "validation": validation}


@router.delete("/episodes/{episode_id}/screenplay/draft")
def delete_screenplay_draft(episode_id: str):
    _episode_or_404(episode_id)
    conn = get_conn()
    cursor = conn.execute(
        "DELETE FROM screenplay_drafts WHERE episode_id=?",
        (episode_id,),
    )
    conn.commit()
    return {"deleted": bool(cursor.rowcount)}


@router.post("/episodes/{episode_id}/screenplay")
async def start_screenplay(episode_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route
    body = _as_body_dict(body)
    routed = await ui_route(
        "screenplay.generate",
        {
            "episode_id": episode_id,
            "idempotency_key": body.get("idempotency_key"),
        },
    )
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中，不能同时重写剧本")
    if ep["screenplay_status"] in {"queued", "running", "repairing"} and _screenplay_task_active(episode_id):
        return {
            "status": ep["screenplay_status"],
            "run_id": ep["active_screenplay_run_id"],
            "mode": "repair" if ep["screenplay_status"] == "repairing" else "baseline",
            "deduplicated": True,
        }

    # 已有 published 产品时仍要求显式删除；未发布恢复统一由 resolver 决定。
    from app.production.revision import resolve_screenplay_resume_eligibility
    eligibility = resolve_screenplay_resume_eligibility(episode_id)
    published_id = None
    try:
        published_id = ep["published_screenplay_artifact_id"] if "published_screenplay_artifact_id" in ep.keys() else None
    except Exception:  # noqa: BLE001
        published_id = None
    has_product = bool(ep["screenplay_json"]) and ep["screenplay_status"] in {"ready", "repairing"}
    if has_product and (
        eligibility.revision_id
        or published_id
        or ep["screenplay_status"] == "ready"
    ):
        if ep["screenplay_status"] == "ready":
            raise HTTPException(
                409,
                "本集已有通过凭证的剧本；如需重新生成，请先删除当前剧本。",
            )
        # repairing → 续跑 Repair（不新建 Baseline）
        pass

    resume_existing = eligibility.resumable
    resume_mode = eligibility.mode if resume_existing else "baseline"
    try:
        recorder = _new_screenplay_recorder(
            episode_id,
            trigger_type="resume" if resume_existing else "manual",
            parent_run_id=ep["active_screenplay_run_id"] if resume_existing else None,
        )
        _spawn_screenplay_activation(
            episode_id,
            recorder,
            project_id=ep["project_id"],
            status="queued",
            message=(
                f"{eligibility.label}已排队，等待文本生成槽位"
                if resume_existing
                else "剧本任务已排队，等待文本生成槽位"
            ),
            expected_active_run_id=(
                ep["active_screenplay_run_id"]
                if resume_existing else None
            ),
            clear_unpublished_ir=not resume_existing,
            resume_eligibility=eligibility if resume_existing else None,
            authorize_blueprint_retry=bool(
                body.get("authorize_blueprint_retry")
            ),
            expected_blueprint_unknown_receipts=(
                body.get("expected_blueprint_unknown_receipts")
                if isinstance(
                    body.get("expected_blueprint_unknown_receipts"), list
                )
                else None
            ),
        )
    except Exception as exc:
        cause = errors.log_error(
            exc,
            action="screenplay_start_activation",
            context={
                "episode_id": episode_id,
                "run_id": getattr(locals().get("recorder"), "run_id", None),
                "resume_existing": resume_existing,
            },
        )
        raise HTTPException(503, {
            "code": "SCREENPLAY_START_FAILED",
            "message": "剧本任务未能启动，原状态已恢复，请重试",
            "action": "retry_resume" if resume_existing else "retry_generate",
            "cause_error_id": cause.error_id,
        }) from exc
    return {
        "status": "queued",
        "run_id": recorder.run_id,
        "mode": resume_mode,
    }


def _prepare_published_screenplay_revalidation(ep: dict):
    """Create a new revision that revalidates immutable published content."""
    from app.harness.contracts import get_contract
    from app.production.revision import (
        ensure_production_revision,
        mark_baseline_generated,
        save_checkpoint,
    )
    from app.production.screenplay_authority import (
        SCREENPLAY_QA_PROFILE_VERSION,
        screenplay_authority_fingerprint,
    )

    episode_id = str(ep["id"])
    conn = get_conn()
    eligibility = _published_screenplay_revalidation_eligibility(ep, conn=conn)
    if not eligibility["eligible"]:
        error = eligibility.get("error")
        raise HTTPException(409, {
            "code": eligibility["code"],
            "message": eligibility["message"],
            "artifact_id": eligibility["artifact_id"],
            "action": "refresh",
        }) from error
    artifact_id = str(eligibility["artifact_id"])
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    bible = _project_bible_or_placeholder(project)
    source_text = _episode_source_text(conn, ep)
    contract = get_contract("screenplay")
    input_fingerprint = screenplay_authority_fingerprint(
        episode_id,
        conn=conn,
        source_text=source_text,
        bible=bible,
        contract_version=contract.version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    revision = ensure_production_revision(
        episode_id=episode_id,
        kind="screenplay",
        input_fingerprint=input_fingerprint,
        contract_version=contract.version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    revision = mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact_id,
        working_artifact_id=artifact_id,
    )
    save_checkpoint(revision.id, {
        "phase": "REVALIDATING_PUBLISHED",
        "working_artifact_id": artifact_id,
        "source_revision_id": ep.get("screenplay_production_revision_id"),
        "yield_reason": "upstream_input_fingerprint_changed",
    })
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing',screenplay_error=?,"
        "screenplay_updated_at=? WHERE id=?",
        ("上游版本已变化，正在重新校验已发布剧本", now(), episode_id),
    )
    conn.commit()
    return revision


@router.post("/episodes/{episode_id}/screenplay/resume")
async def resume_screenplay(episode_id: str, body: dict | None = Body(None)):
    """Continue either pre-Document shards or post-Document validation."""
    from app.capabilities.dispatch import ui_route
    from app.production.revision import (
        get_active_production_revision,
        resolve_screenplay_resume_eligibility,
    )

    body = _as_body_dict(body)
    routed = await ui_route("screenplay.resume", {
        "episode_id": episode_id,
        "idempotency_key": body.get("idempotency_key"),
    })
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中，不能同时修复剧本")
    if _screenplay_task_active(episode_id):
        active_state = _screenplay_production_state(episode_id)
        return {
            "status": ep["screenplay_status"],
            "run_id": ep["active_screenplay_run_id"],
            "mode": active_state.get("mode") or active_state.get("operation"),
            "deduplicated": True,
        }
    rev = get_active_production_revision(episode_id, "screenplay")
    if (
        rev is None
        and ep["screenplay_status"] == "ready"
        and ep["published_screenplay_artifact_id"]
        and not _screenplay_ready(ep)
    ):
        rev = _prepare_published_screenplay_revalidation(dict(ep))
    eligibility = resolve_screenplay_resume_eligibility(
        episode_id,
        revision=rev,
    )
    if not rev or not eligibility.resumable:
        raise HTTPException(409, "没有可继续的首版检查点或完整剧本工作副本")
    resume_mode = eligibility.mode

    try:
        recorder = _new_screenplay_recorder(
            episode_id,
            trigger_type="resume",
            parent_run_id=ep["active_screenplay_run_id"],
        )
        _spawn_screenplay_activation(
            episode_id,
            recorder,
            project_id=ep["project_id"],
            status="queued",
            message=(
                f"{eligibility.label}已排队，等待文本生成槽位"
            ),
            expected_active_run_id=ep["active_screenplay_run_id"],
            resume_eligibility=eligibility,
        )
    except Exception as exc:
        raise HTTPException(503, {
            "code": "SCREENPLAY_RESUME_FAILED",
            "message": "剧本后续阶段未能启动，工作副本和恢复点均已保留，请稍后重试",
            "action": "retry_resume",
        }) from exc
    return {
        "status": "queued",
        "run_id": recorder.run_id,
        "revision_id": (
            get_active_production_revision(episode_id, "screenplay").id
        ),
        "mode": resume_mode,
    }


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
    payload = body.get("screenplay", body)
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


@router.delete("/episodes/{episode_id}/screenplay")
async def delete_screenplay(episode_id: str):
    """Delete the current screenplay projection and invalidate every downstream pointer.

    Immutable artifacts/revisions remain as audit evidence.  The user-selected
    source dialogue requirements intentionally remain on the episode so the
    next Baseline can regenerate against the same explicit contract.
    """
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("screenplay.delete", {"episode_id": episode_id})
    if routed is not None:
        return routed
    episode = dict(_episode_or_404(episode_id))
    screenplay_run_id = episode.get("active_screenplay_run_id")

    cancelled = 0
    for kind in ("screenplay", "storyboard", "video_completion"):
        cancelled += int(await task_registry.cancel_and_wait(kind, episode_id))
    try:
        cancelled += int(_cancel_persisted_screenplay_run(
            episode_id,
            screenplay_run_id,
            message="用户删除剧本，终止持久化剧本运行",
        ))
    except StateConflict:
        raise HTTPException(409, "剧本运行状态已变化，请刷新后重试删除") from None

    conn = get_conn()
    expected_owner = str(screenplay_run_id or "")
    try:
        conn.execute("BEGIN IMMEDIATE")
        latest_owner = conn.execute(
            "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        actual_owner = str(
            latest_owner["active_screenplay_run_id"] or ""
        ) if latest_owner else "missing"
        if not latest_owner or actual_owner != expected_owner:
            raise StateConflict(
                "screenplay_owner",
                episode_id,
                {expected_owner},
                actual_owner,
            )
        shot_count = conn.execute(
            "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
        ).fetchone()["c"]
        worker.delete_episode_shots(episode_id, conn=conn, commit=False)
        stamp = now()

        # Revisions and grants are historical audit records; revoke/supersede
        # them instead of deleting them.
        conn.execute(
            "UPDATE production_revisions SET status='superseded', updated_at=? "
            "WHERE episode_id=? AND status='active'",
            (stamp, episode_id),
        )
        conn.execute(
            "UPDATE production_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE episode_id=?",
            (stamp, episode_id),
        )
        conn.execute(
            "UPDATE completion_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE episode_id=?",
            (stamp, episode_id),
        )
        conn.execute(
            "UPDATE delivery_packages SET status='superseded' "
            "WHERE episode_id=? AND status NOT IN ('rejected','superseded')",
            (episode_id,),
        )
        cursor = conn.execute(
            """UPDATE episodes SET
            screenplay_json=NULL,
            screenplay_character_resolutions='[]',
            screenplay_required_dialogues='[]',
            screenplay_required_dialogue_occurrences='[]',
            screenplay_status='pending',
            screenplay_error=NULL,
            screenplay_started_at=NULL,
            screenplay_updated_at=?,
            screenplay_artifact_id=NULL,
            active_screenplay_run_id=NULL,
            working_screenplay_artifact_id=NULL,
            published_screenplay_artifact_id=NULL,
            screenplay_production_revision_id=NULL,
            screenplay_completion_certificate_id=NULL,
            storyboard_outline_json=NULL,
            storyboard_artifact_id=NULL,
            storyboard_warning=NULL,
            active_storyboard_run_id=NULL,
            working_storyboard_artifact_id=NULL,
            published_storyboard_artifact_id=NULL,
            storyboard_production_revision_id=NULL,
            storyboard_completion_certificate_id=NULL,
            active_video_run_id=NULL,
            video_control_json=NULL,
            delivery_artifact_id=NULL,
            delivery_status='not_ready',
            status='planned',
            script_error=NULL
        WHERE id=? AND COALESCE(active_screenplay_run_id, '')=?""",
            (stamp, episode_id, expected_owner),
        )
        if cursor.rowcount != 1:
            raise StateConflict(
                "screenplay_owner",
                episode_id,
                {expected_owner},
                "changed_during_delete",
            )
        # The revision a blueprint retry grant must bind to is superseded
        # above.  An unknown provider outcome left over from the deleted
        # production would therefore demand a grant that can no longer be
        # issued, and every later Baseline would fail activation with
        # BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED.  Give those calls the
        # terminal disposition the delete actually is; the rows, their cost
        # and their responses stay untouched as audit evidence.
        from app.stages import BLUEPRINT_CALL_ABANDONED_BY_DELETE

        conn.execute(
            """UPDATE provider_calls SET recovery_disposition=?
                WHERE status IN ('INTERRUPTED','RUNNING')
                  AND superseded_by_call_id IS NULL
                  AND recovery_disposition IS NULL
                  AND json_extract(meta,'$.stage_key') IN (
                      'screenplay_blueprint_shard',
                      'screenplay_blueprint_patch',
                      'screenplay_blueprint_review'
                  )
                  AND (
                      json_extract(meta,'$.episode_id')=?
                      OR run_id IN (
                          SELECT id FROM workflow_runs
                           WHERE scope_type='episode' AND scope_id=?
                      )
                  )""",
            (BLUEPRINT_CALL_ABANDONED_BY_DELETE, episode_id, episode_id),
        )
        from app.storyboard_authority import (
            clear_storyboard_outline_authority,
        )

        clear_storyboard_outline_authority(
            episode_id,
            conn=conn,
        )
        conn.commit()
    except StateConflict:
        if conn.in_transaction:
            conn.rollback()
        raise HTTPException(409, "剧本已被新的运行接管，未执行删除") from None
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {
        "deleted": episode_id,
        "downstream_shots_cleared": int(shot_count or 0),
        "cancelled_tasks": cancelled,
    }


def _refresh_screenplay_batch_run(batch_run_id: str) -> None:
    from app.orchestration.state_machine import StateConflict, transition_run

    conn = get_conn()
    parent = conn.execute(
        "SELECT status FROM workflow_runs WHERE id=? AND workflow_type='screenplay_batch'",
        (batch_run_id,),
    ).fetchone()
    if not parent or parent["status"] != "RUNNING":
        return
    rows = conn.execute(
        "SELECT status FROM workflow_runs WHERE parent_run_id=? "
        "AND workflow_type='screenplay'",
        (batch_run_id,),
    ).fetchall()
    if not rows:
        return
    statuses = [row["status"] for row in rows]
    terminal = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
    if any(status not in terminal for status in statuses):
        return
    failures = sum(status in {"PARTIAL", "FAILED", "CANCELLED"} for status in statuses)
    target = "SUCCEEDED" if failures == 0 else "PARTIAL"
    reason = (
        f"批量剧本全部完成，共 {len(statuses)} 集"
        if failures == 0
        else f"批量剧本已收口，共 {len(statuses)} 集，{failures} 集未成功"
    )
    try:
        transition_run(
            batch_run_id,
            "RUNNING",
            target,
            reason,
            failure_code="PARTIAL_RESULT" if failures else None,
        )
    except StateConflict:
        return
    evidence_repository.append_event(
        batch_run_id,
        "BATCH_FINISHED",
        "info" if failures == 0 else "warning",
        reason,
        payload={"children": len(statuses), "unsuccessful": failures},
    )


async def _screenplay_guarded(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    priority: int = 0,
    batch_run_id: str | None = None,
):
    from app.generation_concurrency import run_with_generation_slot

    async def activate() -> EpisodeScreenplay | None:
        conn = get_conn()
        cursor = conn.execute(
            "UPDATE episodes SET screenplay_status='running',"
            "screenplay_error=COALESCE(screenplay_error, ?),screenplay_updated_at=? "
            "WHERE id=? AND active_screenplay_run_id=? "
            "AND screenplay_status IN ('queued','running','repairing')",
            ("正在生成人物上下文与首版剧本", now(), episode_id, recorder.run_id),
        )
        conn.commit()
        if cursor.rowcount != 1:
            _assert_screenplay_run_owner(episode_id, run_id=recorder.run_id)
            raise StateConflict(
                "screenplay_activation",
                episode_id,
                {"queued", "running", "repairing"},
                "episode_state_changed",
            )
        return await _recorded_screenplay_task(episode_id, recorder)

    try:
        await run_with_generation_slot(
            "screenplay",
            activate,
            priority=priority,
        )
    except asyncio.CancelledError:
        row = get_conn().execute(
            "SELECT status FROM workflow_runs WHERE id=?",
            (recorder.run_id,),
        ).fetchone()
        if row and row["status"] == "CREATED":
            try:
                recorder.cancel("排队中的剧本任务已取消")
            except StateConflict:
                pass
        raise
    finally:
        if batch_run_id:
            _refresh_screenplay_batch_run(batch_run_id)


@router.post("/projects/{project_id}/screenplay-all")
async def start_screenplay_all(project_id: str):
    from app.generation_concurrency import PRIORITY_BATCH
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.generate_batch", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,)).fetchall()
    selected = [
        r for r in rows
        if (
            r["screenplay_status"] in ("pending", "failed", "repairing")
            or (
                not r["screenplay_json"]
                and r["screenplay_status"] not in {"queued", "running"}
            )
            or (
                r["screenplay_status"] in {"queued", "running"}
                and not _screenplay_task_active(r["id"])
            )
        )
        and r["screenplay_status"] != "ready"
    ]
    if not selected:
        raise HTTPException(409, "没有待生成剧本的剧集")
    selected_ids = [row["id"] for row in selected]
    batch_recorder = WorkflowRecorder.create(
        workflow_type="screenplay_batch",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(
            project_id,
            selected_ids,
            get_contract("screenplay").version,
        ),
        requested_by="user",
        trigger_type="batch",
        policy_snapshot={
            "contract": f"screenplay@{get_contract('screenplay').version}",
            "selected_episode_ids": selected_ids,
            "queue_policy": "priority_fifo",
            "cancel_policy": "cancel_all_then_wait",
        },
    )
    batch_recorder.start()
    run_ids: list[str] = []
    failed_to_start: list[dict] = []
    for row in selected:
        eid = row["id"]
        if row["screenplay_status"] in {"queued", "running"} and not _screenplay_task_active(eid):
            conn.execute(
                "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, "
                "active_screenplay_run_id=NULL, screenplay_updated_at=? WHERE id=?",
                ("检测到上次剧本任务已中断，本次将从安全状态重新启动", now(), eid),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        recorder = None
        from app.production.revision import (
            resolve_screenplay_resume_eligibility,
        )

        eligibility = resolve_screenplay_resume_eligibility(eid)
        is_resume = eligibility.resumable
        try:
            recorder = _new_screenplay_recorder(
                eid,
                trigger_type="batch_resume" if is_resume else "batch",
                parent_run_id=batch_recorder.run_id,
            )
            _spawn_screenplay_activation(
                eid,
                recorder,
                project_id=project_id,
                status="queued",
                message=(
                    f"批量任务：{eligibility.label}已排队"
                    if is_resume
                    else "批量剧本已排队，等待文本生成槽位"
                ),
                expected_active_run_id=(
                    row["active_screenplay_run_id"]
                    if is_resume else None
                ),
                clear_unpublished_ir=not is_resume,
                resume_eligibility=eligibility if is_resume else None,
                task_factory=lambda eid=eid, recorder=recorder: _screenplay_guarded(
                    eid,
                    recorder,
                    priority=PRIORITY_BATCH,
                    batch_run_id=batch_recorder.run_id,
                ),
            )
            run_ids.append(recorder.run_id)
        except Exception as exc:  # noqa: BLE001 - one episode must not strand the batch
            public = errors.record_and_format(
                exc,
                action="screenplay_batch_spawn",
                context={"project_id": project_id, "episode_id": eid},
            )
            failed_to_start.append({
                "episode_id": eid,
                "error": public,
                "retryable": True,
            })
    if not run_ids:
        batch_recorder.fail(RuntimeError("批量剧本任务均未能进入持久化队列"))
        raise HTTPException(503, {
            "code": "SCREENPLAY_BATCH_START_FAILED",
            "message": "批量剧本任务均未能启动，各集原状态已保留，可直接重试",
            "failed_to_start": failed_to_start,
        })
    return {
        "started": len(run_ids),
        "batch_run_id": batch_recorder.run_id,
        "run_ids": run_ids,
        "failed_to_start": failed_to_start,
        "retryable_failures": len(failed_to_start),
    }


@router.post("/projects/{project_id}/screenplay-all/cancel")
async def cancel_screenplay_all(project_id: str):
    """停止本项目所有正在进行的剧本生成：取消在跑任务，未开跑的回退状态。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.cancel", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, screenplay_json, active_screenplay_run_id FROM episodes "
        "WHERE project_id=? AND screenplay_status IN ('queued','running','repairing')",
        (project_id,)).fetchall()
    episode_ids = [row["id"] for row in rows]
    batch_rows = conn.execute(
        "SELECT id FROM workflow_runs WHERE workflow_type='screenplay_batch' "
        "AND scope_type='project' AND scope_id=? AND status='RUNNING'",
        (project_id,),
    ).fetchall()
    cancelled_batches = 0
    for batch in batch_rows:
        try:
            WorkflowRecorder(batch["id"]).cancel("用户停止批量剧本")
            cancelled_batches += 1
        except StateConflict:
            continue
    local_stopped = await task_registry.cancel_many_and_wait(
        "screenplay",
        episode_ids,
    )
    persisted_stopped = 0
    released = 0
    for r in rows:
        eid = r["id"]
        run_id = r["active_screenplay_run_id"]
        try:
            persisted_stopped += int(_cancel_persisted_screenplay_run(
                eid,
                run_id,
                message="用户停止批量剧本任务",
            ))
        except StateConflict:
            continue
        full = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        fallback = _screenplay_fallback_status(full)
        if run_id:
            cursor = conn.execute(
                "UPDATE episodes SET screenplay_status=?,screenplay_error=NULL,"
                "active_screenplay_run_id=NULL,screenplay_updated_at=? "
                "WHERE id=? AND active_screenplay_run_id=?",
                (fallback, now(), eid, run_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE episodes SET screenplay_status=?,screenplay_error=NULL,"
                "active_screenplay_run_id=NULL,screenplay_updated_at=? "
                "WHERE id=? AND active_screenplay_run_id IS NULL",
                (fallback, now(), eid),
            )
        released += int(cursor.rowcount == 1)
    conn.commit()
    return {
        "stopped": released,
        "local_stopped": local_stopped,
        "persisted_stopped": persisted_stopped,
        "matched": len(rows),
        "cancelled_batches": cancelled_batches,
    }


@router.post("/episodes/{episode_id}/screenplay/cancel")
async def cancel_screenplay(episode_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.cancel", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = dict(_episode_or_404(episode_id))
    if str(ep.get("screenplay_error") or "").startswith("CANCELLING:"):
        if not _screenplay_task_active(episode_id):
            fallback = _screenplay_fallback_status(ep)
            conn = get_conn()
            conn.execute(
                "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=?, "
                "active_screenplay_run_id=NULL, screenplay_snapshot_version=screenplay_snapshot_version+1 "
                "WHERE id=?",
                (fallback, now(), episode_id),
            )
            conn.commit()
            return {
                "status": fallback,
                "run_id": ep.get("active_screenplay_run_id"),
                "requested_at": ep.get("screenplay_updated_at"),
                "finished_at": now(),
                "retained_working_copy": bool(ep.get("screenplay_json")),
                "resume_available": fallback == "repairing",
                "recovered_stale_cancellation": True,
            }
        return {
            "status": "cancelling",
            "run_id": ep.get("active_screenplay_run_id"),
            "requested_at": ep.get("screenplay_updated_at"),
            "deduplicated": True,
        }
    if ep["screenplay_status"] not in {"queued", "running", "repairing"} or not _screenplay_task_active(episode_id):
        raise HTTPException(409, "当前没有正在进行的剧本任务")
    conn = get_conn()
    requested_at = now()
    run_id = ep.get("active_screenplay_run_id")
    conn.execute(
        "UPDATE episodes SET screenplay_error=?, screenplay_updated_at=?, "
        "screenplay_snapshot_version=screenplay_snapshot_version+1 WHERE id=?",
        (f"CANCELLING: 正在取消运行 {run_id or '未知'}", requested_at, episode_id),
    )
    conn.commit()
    try:
        cancelled_locally = await asyncio.wait_for(
            task_registry.cancel_and_wait("screenplay", episode_id), timeout=15
        )
    except asyncio.TimeoutError:
        return {
            "status": "cancelling",
            "run_id": run_id,
            "requested_at": requested_at,
            "message": "worker 尚未返回终态，系统将继续观察，未宣称已停止",
        }
    if not cancelled_locally and run_id:
        # The owner may live in another service process.  Persist cancellation
        # first; clearing the episode lease below then fences that remote worker
        # from every screenplay/revision/shard write guarded by run ownership.
        try:
            _cancel_persisted_screenplay_run(
                episode_id,
                run_id,
                message="用户从其他服务实例取消剧本任务",
            )
        except StateConflict:
            raise HTTPException(409, "剧本运行状态已变化，请刷新后重试") from None
    latest = dict(_episode_or_404(episode_id))
    fallback = _screenplay_fallback_status(latest)
    retained = bool(
        latest.get("screenplay_json")
        or latest.get("working_screenplay_artifact_id")
    )
    cursor = conn.execute(
        "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=?, "
        "active_screenplay_run_id=NULL, screenplay_snapshot_version=screenplay_snapshot_version+1 "
        "WHERE id=? AND (active_screenplay_run_id=? OR active_screenplay_run_id IS NULL)",
        (fallback, now(), episode_id, run_id))
    if cursor.rowcount != 1:
        raise HTTPException(409, "剧本任务已被新的运行接管，未覆盖其状态")
    conn.commit()
    return {
        "status": fallback,
        "run_id": run_id,
        "requested_at": requested_at,
        "finished_at": now(),
        "retained_working_copy": retained,
        "resume_available": fallback == "repairing",
    }


@router.post("/episodes/{episode_id}/screenplay/impact-preview")
def preview_screenplay_edit_impact(episode_id: str, body: dict):
    """发布前的纯读影响预览：不建任务、不设栅栏、不写证据。"""
    ep = dict(_episode_or_404(episode_id))
    payload = body.get("screenplay", body)
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
    payload = body.get("screenplay", body)
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
    payload = body.get("screenplay", body)
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

__all__ = [name for name in globals() if not name.startswith("__")]
