"""剧本发布状态快照、发布物内容差异、卡司影响、二创复核资格与权威字段。

从 app/domain/screenplay_ops.py 按原样搬移；被本包其余大多数子模块依赖，是本包唯一没有反向依赖的基础层。
"""
from __future__ import annotations

import json

from app import task_registry
from app.db import get_conn
from app.domain.common import (
    _project_bible_or_placeholder,
    _screenplay_ready,
)
from app.evidence import repository as evidence_repository
from app.schemas import EpisodeScreenplay


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
        artifact.get("type")
        not in (
            # "screenplay_document" is the retired heavy-pipeline shape
            # (contract major < 6); "episode_prep_pack" is the lightweight
            # replacement (contract 6.0.0+, docs/TRANSFORM_FREEZE_PLAN.md).
            # Both are legitimate published_screenplay_artifact_id targets --
            # see app.production.certificate.issue_completion_certificate's
            # identical two-type set for the same "screenplay" stage. A
            # prep_pack artifact parses fine below:
            # screenplay_from_artifact_record dispatches on its own
            # "prep_pack_version" marker (app.production.patch), and
            # assert_screenplay_matches_validated_v7_source no-ops for it
            # (no v7 shard/merged-IR lineage exists to check).
            {"screenplay_document", "episode_prep_pack"}
        )
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

def _screenplay_production_state(episode_id: str) -> dict:
    """Expose the actual production phase used by ScriptPage controls.

    ``screenplay_status`` is a delivery status and cannot distinguish the one
    allowed Baseline call from later Patch activations.  The revision ledger is
    the authority for that distinction.
    """
    from app.production.revision import screenplay_production_state

    return screenplay_production_state(episode_id)
