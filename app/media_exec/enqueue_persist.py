"""``_enqueue_shot_impl`` 的落库阶段：image_inputs 元数据组装、
``shot_versions``/``jobs`` 事务写入、预算预扣与首次派发。

拆自 ``app/media_exec/enqueue.py``（原 743 行的单一函数），移动未重写。
``persist_new_video_version`` 内部的子 helper 只用调用方传入的同一个
``conn`` 执行 SQL，不自行 ``commit()``/``rollback()``——事务边界（含
「异常处理器第一条语句必须是 rollback」）完整保留在
``persist_new_video_version`` 自身，不下放给子 helper（CLAUDE.md「不得在
调用方的连接上隐式提交」）。命名与循环导入规避手法同 ``.enqueue_context``。
"""
from __future__ import annotations

import json
from typing import Any

from app.db import new_id, now
from app.orchestration import media_scheduler
from app.orchestration.media_runs import ensure_media_trace


def build_base_image_meta(
    decision, shot, prompt_text: str, is_storyboard_pack_shot: bool, *,
    chain_after_shot_id, chain_after_version_id, continuity_mode,
    prompt_prev_state_out, incoming_transition, outgoing_transition,
    auto_retake_count: int, supervisor_run_id, target_prompt_profile,
    target_video_provider, target_video_model, prompt_override, critique,
    previous_prompt_version, previous_prompt_fingerprint, previous_prompt_text,
    first_frame_source, boundary_source_shot_id, boundary_relation_edit,
    boundary_relation_action, boundary_relation_reason, boundary_start_state,
):
    """组装 image_inputs 的基础字段（不含 shot_plan 专属字段与可选附加项）。"""
    from app import video_modes
    from app.compiler import VIDEO_PROMPT_CONTRACT_VERSION
    from app.continuity import shot_contract_dict
    from app.video_prompt_ai import AI_VIDEO_PROMPT_CONTRACT_VERSION

    return {
        "mode": decision.mode,
        "mode_decision": video_modes.decision_to_dict(decision),
        "after_shot_id": chain_after_shot_id,
        "after_version_id": chain_after_version_id,
        "after_shot_no": None,
        "continuity_mode": continuity_mode,
        "prev_state_out": prompt_prev_state_out,
        "incoming_transition": incoming_transition,
        "outgoing_transition": outgoing_transition,
        "auto_retake_count": max(0, int(auto_retake_count)),
        "supervisor_run_id": supervisor_run_id,
        "shot_contract_json": json.dumps(shot_contract_dict(shot), ensure_ascii=False),
        "video_prompt_contract_version": VIDEO_PROMPT_CONTRACT_VERSION,
        "ai_video_prompt_required": not is_storyboard_pack_shot,
        "ai_video_prompt_contract_target": AI_VIDEO_PROMPT_CONTRACT_VERSION,
        "ai_video_prompt_profile_target": target_prompt_profile.profile_id,
        "ai_video_prompt_profile_version_target": target_prompt_profile.version,
        "ai_video_prompt_target_provider": target_video_provider,
        "ai_video_prompt_target_model": target_video_model,
        "continuity_contract_prompt": prompt_text,
        "prompt_user_instruction": (prompt_override or "").strip(),
        "prompt_critique": [
            str(item).strip() for item in (critique or []) if str(item).strip()
        ],
        "previous_prompt_version_id": (
            previous_prompt_version["id"] if previous_prompt_version is not None else None
        ),
        "previous_prompt_fingerprint": previous_prompt_fingerprint or None,
        "previous_prompt_inherited": bool(previous_prompt_text and not prompt_override),
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "boundary_prompt_contract": {
            "video_generation_mode": (
                decision.mode
            ),
            "first_frame_source": first_frame_source,
            "source_shot_id": boundary_source_shot_id,
            "relation_edit": boundary_relation_edit,
            "relation_action": boundary_relation_action,
            "relation_normalization_reason": boundary_relation_reason,
            "start_state": boundary_start_state,
        },
    }


def apply_shot_plan_meta(image_meta: dict, shot_plan) -> None:
    """有已发布视频计划时，把计划专属字段并入 image_inputs（原地修改）。"""
    if shot_plan is None:
        return
    image_meta["boundary_prompt_contract"]["video_generation_mode"] = shot_plan.mode.value
    image_meta.update({
        "episode_video_plan_id": shot_plan.episode_video_plan_id,
        "shot_plan_id": shot_plan.shot_plan_id,
        "plan_revision": shot_plan.plan_revision,
        "source_storyboard_revision_id": shot_plan.source_storyboard_revision_id,
        "capability_snapshot_id": shot_plan.capability_snapshot_id,
        "input_revision_fingerprints": dict(shot_plan.input_revision_fingerprints),
        "planned_mode": shot_plan.mode.value,
        "actual_mode": shot_plan.mode.value,
        "video_input_intent": (
            shot_plan.video_input_intent.value
            if shot_plan.video_input_intent is not None else None
        ),
        "depends_on_shot_id": shot_plan.depends_on_shot_id,
    })


def apply_optional_meta(
    image_meta: dict, *, preflight_repair, dependency_snapshot, critique_sources,
    reference_gallery,
) -> None:
    """把可选附加项（预检自动修复、依赖快照、评语来源、参考画廊）并入 image_inputs。"""
    if preflight_repair:
        image_meta["preflight_auto_repair"] = preflight_repair
    if dependency_snapshot:
        # This immutable token is checked again by the worker before every
        # candidate/QA/adoption write.
        image_meta["review_dependency_snapshot"] = {
            "qualification_version": dependency_snapshot.get("qualification_version"),
            "published_screenplay_artifact_id": dependency_snapshot.get("published_screenplay_artifact_id"),
            "confirmed_storyboard_artifact_id": dependency_snapshot.get("confirmed_storyboard_artifact_id"),
            "screenplay_revision": dependency_snapshot.get("screenplay_revision"),
            "storyboard_revision": dependency_snapshot.get("storyboard_revision"),
            "asset_status": dependency_snapshot.get("asset_status"),
            "asset_inputs": dependency_snapshot.get("asset_inputs") or [],
            "asset_soft_warnings": dependency_snapshot.get("asset_soft_warnings") or [],
            "captured_at": dependency_snapshot.get("server_time"),
        }
    if critique_sources:
        image_meta["critique_sources"] = critique_sources
    if not reference_gallery:
        return
    image_meta["reference_images"] = reference_gallery["reference_images"]
    image_meta["reference_gallery_source_version_id"] = reference_gallery["source_version_id"]
    image_meta["reference_gallery_fingerprint"] = reference_gallery["fingerprint"]
    if reference_gallery.get("keyframe_contract_fingerprint"):
        image_meta["keyframe_contract_fingerprint"] = reference_gallery["keyframe_contract_fingerprint"]
    if isinstance(reference_gallery.get("keyframe_sequence"), dict):
        image_meta["keyframe_sequence"] = reference_gallery["keyframe_sequence"]
    if isinstance(reference_gallery.get("reference_manifest"), dict):
        image_meta["reference_manifest"] = reference_gallery["reference_manifest"]
        image_meta["reference_manifest_frozen"] = True
    if reference_gallery["revision"] is not None:
        image_meta["reference_gallery_revision"] = reference_gallery["revision"]
    if reference_gallery["edited"]:
        image_meta["reference_gallery_edited"] = True
    if reference_gallery.get("contract_override"):
        image_meta["reference_gallery_contract_override"] = True


def _insert_shot_version_row(conn, *, version_id, shot_id, version_no, prompt_text, key, image_meta):
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               video_slot_active,created_at,image_inputs
           ) VALUES(?,?,?,?,?,'queued',1,?,?)""",
        (
            version_id, shot_id, version_no, prompt_text, key, now(),
            json.dumps(image_meta, ensure_ascii=False),
        ),
    )


def _attach_job_to_version(
    conn, *, job_id, shot_id, version_id, ep, project, preflight_job_id, preflight_owner,
    chain_after_shot_id, chain_after_version_id, run_id, step_run_id, supervisor_run_id,
):
    """把新版本挂到既有预检 job（原地更新），或全新插入一条 job 行。"""
    if preflight_job_id:
        updated = conn.execute(
            """UPDATE jobs
                  SET version_id=?,episode_id=?,project_id=?,status='queued',
                      video_slot_active=1,error=NULL,next_retry_at=NULL,retry_count=0,
                      reason_code=NULL,reason_text=NULL,stage_progress_json=NULL,
                      after_shot_id=?,after_version_id=?,run_id=?,
                      owner_run_id=?,step_run_id=?,
                      lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                WHERE id=? AND shot_id=? AND kind='video' AND version_id IS NULL
                  AND video_slot_active=1 AND lease_owner=?""",
            (
                version_id, ep["id"], project["id"], chain_after_shot_id,
                chain_after_version_id, run_id, supervisor_run_id, step_run_id,
                now(), job_id, shot_id, preflight_owner,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("视频输入校验任务状态已变化，请刷新后重试")
        return
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               video_slot_active,created_at,updated_at,after_shot_id,
               after_version_id,run_id,owner_run_id,step_run_id
           ) VALUES(
               ?,'video',?,?,?,?,'queued',1,?,?,?,?,?,?,?
           )""",
        (
            job_id, shot_id, version_id, ep["id"], project["id"], now(), now(),
            chain_after_shot_id, chain_after_version_id, run_id, supervisor_run_id, step_run_id,
        ),
    )


def _mark_job_stage_queued(conn, job_id: str) -> None:
    try:
        from app.media_pipeline import stages as media_stages
        from app.media_pipeline.stage_state import set_pipeline_stage
        set_pipeline_stage(job_id, media_stages.STAGE_JOB_QUEUED, conn=conn)
    except Exception:  # noqa: BLE001
        pass


def _reserve_or_pause_budget(conn, *, job_id, version_id, episode_id, estimate, budget_limit) -> bool:
    reserved = media_scheduler.reserve_budget(job_id, episode_id, estimate, budget_limit, conn=conn)
    if not reserved:
        conn.execute(
            """UPDATE jobs
                  SET video_slot_active=0,lease_owner=NULL,lease_expires_at=NULL,
                      updated_at=?
                WHERE id=?""",
            (now(), job_id),
        )
        conn.execute(
            """UPDATE shot_versions
                  SET status='paused_budget',video_slot_active=0,
                      error='集预算不足，任务已暂停'
                WHERE id=?""",
            (version_id,),
        )
    return reserved


def _bind_new_operation_if_requested(
    conn, *, operation_idempotency_key, operation_request_fingerprint, operation_claim_token,
    operation_command, shot_plan, version_id, job_id, shot_id, reserved,
) -> None:
    if not (operation_idempotency_key and operation_request_fingerprint and operation_claim_token):
        return
    from app.video_command_operations import bind_video_command_operation

    domain_result: dict[str, Any] = {
        "reused": False, "version_id": version_id, "job_id": job_id, "task_accepted": True,
    }
    if not reserved:
        domain_result["paused_budget"] = True
    bind_video_command_operation(
        command=operation_command,
        idempotency_key=operation_idempotency_key,
        request_fingerprint=operation_request_fingerprint,
        claim_token=operation_claim_token,
        binding={
            "plan_id": (shot_plan.episode_video_plan_id if shot_plan else None),
            "version_id": version_id,
            "job_id": job_id,
            "provider_operation_id": None,
            "result": domain_result,
            **({"append_enqueued": {"shot_id": shot_id, **domain_result}}
               if operation_command == "video.generate_episode" else {}),
        },
        conn=conn,
        merge=operation_command == "video.generate_episode",
    )


def persist_new_video_version(
    conn, *, shot_id, version_id, prompt_text, key, image_meta, preflight_job_id,
    preflight_owner, ep, project, chain_after_shot_id, chain_after_version_id,
    supervisor_run_id, estimate, budget_limit, operation_idempotency_key,
    operation_request_fingerprint, operation_claim_token, operation_command, shot_plan,
) -> dict[str, Any]:
    """在单个 BEGIN IMMEDIATE 事务里写入新版本/job、预扣预算、绑定操作、推进分集状态。

    子 helper 只执行 SQL，不自行提交/回滚——事务边界与「异常处理器第一条语句
    必须是 rollback」完整保留在本函数。
    """
    job_id = preflight_job_id or new_id("job")
    # 仅用于下面 ensure_media_trace 的 tracing payload：与事务内重新查询的
    # 「真实」version_no 是两次独立查询（原实现如此，事务内那次才是权威值，
    # 用于避免与并发入队的 TOCTOU 竞争）。
    pretrace_version_no = (conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS m FROM shot_versions WHERE shot_id=?",
        (shot_id,)).fetchone()["m"]) + 1
    run_id, step_run_id = ensure_media_trace(
        workflow_type="video_generation", scope_id=shot_id,
        input_value={"prompt": prompt_text, "version": pretrace_version_no},
        budget_limit_cny=budget_limit,
    )
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        version_no = (conn.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM shot_versions WHERE shot_id=?",
            (shot_id,),
        ).fetchone()["n"])
        _insert_shot_version_row(
            conn, version_id=version_id, shot_id=shot_id, version_no=version_no,
            prompt_text=prompt_text, key=key, image_meta=image_meta,
        )
        _attach_job_to_version(
            conn, job_id=job_id, shot_id=shot_id, version_id=version_id, ep=ep, project=project,
            preflight_job_id=preflight_job_id, preflight_owner=preflight_owner,
            chain_after_shot_id=chain_after_shot_id, chain_after_version_id=chain_after_version_id,
            run_id=run_id, step_run_id=step_run_id, supervisor_run_id=supervisor_run_id,
        )
        _mark_job_stage_queued(conn, job_id)
        reserved = _reserve_or_pause_budget(
            conn, job_id=job_id, version_id=version_id, episode_id=ep["id"],
            estimate=estimate, budget_limit=budget_limit,
        )
        _bind_new_operation_if_requested(
            conn, operation_idempotency_key=operation_idempotency_key,
            operation_request_fingerprint=operation_request_fingerprint,
            operation_claim_token=operation_claim_token, operation_command=operation_command,
            shot_plan=shot_plan, version_id=version_id, job_id=job_id, shot_id=shot_id,
            reserved=reserved,
        )
        conn.execute(
            "UPDATE episodes SET status='generating' WHERE id=? AND status='confirmed'",
            (ep["id"],),
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {"job_id": job_id, "reserved": reserved, "version_no": version_no}


def dispatch_new_video_job(conn, *, job_id: str, shot_id: str, episode_id: str) -> bool:
    """首次派发到实时调度；失败不阻塞入队，持久 dispatcher 会重新发现该 job。"""
    from app import errors

    try:
        from .dispatch import _enqueue_for_current_status

        _enqueue_for_current_status(job_id)
        return False
    except Exception as exc:  # durable dispatcher continuously rebuilds queues from jobs
        errors.record_and_format(
            exc, action="video_initial_dispatch",
            context={"job_id": job_id, "shot_id": shot_id, "episode_id": episode_id},
        )
        conn.execute(
            "UPDATE jobs SET error=?, updated_at=? WHERE id=? AND status='queued'",
            (
                "任务已写入持久队列；实时调度通知暂未送达，系统将自动重新发现，无需重复点击",
                now(), job_id,
            ),
        )
        conn.commit()
        return True
