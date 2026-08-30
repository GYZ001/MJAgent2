"""视频作业主协程 ``_run_job``，及本包子模块拆分前既有名字的再导出中枢。

``run_job.py`` 原来是 5,333 行的单文件（`app/media_exec` 真包化之后仍未进一步
拆分）；本次把授权判定（``.authority``）、结果落库（``.checkpoints``）、四种
输入模式准备（``.input_boundary``/``.input_reference``/``.input_first_frame_last``/
``.input_video_mode``）、job/version 状态与轮询策略（``.job_state``）、参考图
进度（``.reference_progress``）、调度分派与 worker 生命周期
（``.worker_lifecycle``，2026-08-30 进一步拆出 ``.dispatch`` 承接其中的持久
派发部分，见该文件模块 docstring）、worker 主循环（``.worker_loop``）、恢复/
对账（``.job_recovery``）、延迟重排与预算恢复（``.retry_scheduling``）、五个
围栏异常（``.fences``）都移到了各自独立的子模块（移动，未重写）。

``_run_job`` 本身（1,157 行的单一协程，视频作业从领取到终态判决的完整状态机）
留在本文件——它是全部上述子模块的编排者，逐行搬移到任何一个子模块都会让该
子模块反过来依赖几乎所有其它子模块，而不是让它继续依赖它们，属于
``app/FILE_CONVENTIONS.toml`` 里「移动未重写的既有巨型单函数」豁免类别的同类
情况（与 ``compile_screenplay_ir``、``validate_screenplay_narrative`` 等同理）。

本文件因此还承担第二个角色：``app/media_exec/__init__.py`` 现有的
``from .run_job import (...)`` 一次性导入了 85 个名字（拆分前它们都定义在这
个文件里），拆分后其中 84 个改由本文件从对应子模块 ``from .xxx import name``
显式再导入、本文件只新增 ``_run_job`` 自己的定义——``__init__.py`` 与
``app/worker.py`` 因此不用改一行，全仓 ``from app.media_exec.run_job import
name`` / ``app.media_exec.run_job.name`` 的既有调用点（含
``tests/test_keyframe_outer_accounting.py``、
``tests/test_process_restart_recovery.py`` 子进程脚本对
``media_run_job.X = stub`` 的直接属性打桩）不用改一行：``_run_job`` 内部对这
些名字的引用是裸名（非 ``module.name`` 限定），解析到的正是本文件自己这份
``from .xxx import name`` 绑定的对象，打桩换掉这份绑定，``_run_job`` 下一次
裸名查找就会读到替身。

``__all__`` 用静态列表（不是其余切片常见的
``[name for name in globals() if not name.startswith("__")]``）：本文件里
84 个名字导入后只在别处的子模块里被真正调用，动态 ``globals()`` 形式的
``__all__`` 无法让 ruff 静态识别出它们是刻意再导出（同一问题、同一修法见
``app/media_exec/__init__.py``/``app/worker.py`` 的静态 ``__all__``）。
"""

from __future__ import annotations

import asyncio
import json
import time

from app import config, errors, hiagent, video_modes
from app.artifacts import _invalidate_final_video
from app.compiler import ensure_source_excerpt_in_prompt, shot_cost_cny
from app.completion_grant import VideoBudgetAuthorizationError
from app.db import get_conn, now
from app.hiagent import ProviderError
from app.evidence import media as media_evidence
from app.orchestration import media_scheduler
from app.orchestration.media_runs import mark_media_job_state
from app.observability.tracing import set_worker_trace

from .common import LeaseLost, _retry_tasks
from .enqueue import (
    _load_shot_model,
    _row_value,
    _video_path,
    enqueue_shot,
    reconcile_episode_generation_status,
    recover_equivalent_stale_provider_jobs,
)
from .fences import (
    ProviderCreateUnresolved,
    ReviewDependencyFence,
    VideoInflightAdmissionDeferred,
    VideoInputRepairRequired,
    VideoPlanStaleFence,
)
from .authority import (
    _assert_current_storyboard_completion_authority,
    _assert_job_lease,
    _assert_provider_create_resolved,
    _assert_review_dependency_fence,
    _assert_review_dependency_fence_async,
    _assert_video_provider_submission_authority,
    _assert_video_provider_submission_authority_async,
    _authority_checks_can_use_worker_thread,
    _claim_job_without_blocking_loop,
    _connection_for_heartbeat_operation,
    _provider_create_outcome_unknown,
    _release_pre_call_video_claim,
)
from .checkpoints import (
    _await_with_job_lease_heartbeat,
    _commit_provider_acceptance,
    _commit_provider_acceptance_in_transaction,
    _commit_provider_create_unresolved,
    _commit_provider_terminal_failure,
    _commit_provider_terminal_failure_in_transaction,
    _commit_video_result_checkpoint,
    _commit_video_result_checkpoint_in_transaction,
    _run_in_memory_write_transaction,
)
from .input_boundary import (
    _ContinuityWait,
    _image_dimensions,
    _load_boundary_asset,
    _normalize_boundary_pair,
    _persist_boundary_asset,
    _resolve_current_execution_plan,
)
from .input_first_frame_last import (
    _prepare_first_frame_mode_inputs,
    _prepare_first_last_mode_inputs,
)
from .input_reference import _prepare_reference_mode_inputs
from .input_video_mode import (
    _ensure_ai_video_prompt,
    _prepare_planned_mode_inputs,
    _prepare_video_input_mode,
)
from .job_recovery import (
    _block_orphaned_continuity_job,
    _recover_one_media_job,
    reconcile_stalled_video_jobs,
    recover_and_start,
    recover_media_jobs,
)
from .job_state import (
    _paid_video_attempt_count,
    _prior_task_poll_failure_messages,
    _provider_submitted_at,
    _provider_wait_policy,
    _recover_paid_video_task,
    _set_job,
    _set_version,
    _video_image_inputs_from_meta,
    _video_model_rejection_guidance,
)
from .reference_progress import (
    _auto_retake,
    _completed_reference_slots,
    _narrative_keyframe_candidate_progress,
    _reference_gallery_ready,
)
from .retry_scheduling import (
    _defer_provider_poll,
    _requeue_after,
    _schedule_job_retry,
    retry_paused,
)
from .worker_lifecycle import (
    _SWEEPER_INTERVAL_SECONDS,
    _dispatcher_task,
    _drain_memory_queue,
    _poll_worker_target,
    _reference_worker_target,
    _stale_lease_sweeper,
    _sweeper_task,
    _video_ready_worker_target,
    _worker_target,
    ensure_workers,
    start_stale_lease_sweeper,
    stop,
)
from .dispatch import (
    _dispatch_due_jobs,
    _dispatch_due_jobs_legacy,
    _dispatch_due_jobs_stage_aware,
    _durable_dispatcher,
    _enqueue_for_current_status,
    _queue_job,
    _start_durable_dispatcher,
)
from .worker_loop import (
    _maybe_auto_qa,
    _release_interrupted_worker_job,
    _video_mode_input_roles_valid,
    _wait_for_worker_job,
    _worker_loop,
)


async def _run_job(job_id: str, *, lease_owner: str | None = None) -> None:
    from app.media_pipeline.stage_state import set_pipeline_stage

    # Workers are spawned during application recovery.  Give the lifespan and
    # HTTP server a scheduling boundary before any JSON decoding, authority
    # verification, or reference preparation below; otherwise a recovered
    # cohort can monopolize the event loop before the socket starts listening.
    await asyncio.sleep(1.0)
    conn = get_conn()
    owner = lease_owner or f"direct-{id(asyncio.current_task())}"
    if lease_owner is None:
        if not await _claim_job_without_blocking_loop(
            job_id,
            owner,
            lease_seconds=180.0,
        ):
            return
        run_row = conn.execute(
            "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if run_row:
            mark_media_job_state(run_row["run_id"], run_row["step_run_id"], "running")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job["status"] != "running" or job["lease_owner"] != owner:
        return
    # 这个 worker task 会在同一个 asyncio Task/Context 里连续 await 很多个 job；
    # 不重新绑定的话，provider_calls（尤其 video_create/video_poll）会带着上一个
    # job 的 trace，或者干脆一直是启动时的空 trace，链路树永远关联不到它们。
    # 详见 set_worker_trace 的说明：这里必须在本 job 的第一次供应商调用之前、
    # 每个 job 都调用一次，即使 run_id 为空也要显式清空上一个 job 留下的痕迹。
    set_worker_trace(job["run_id"], job["step_run_id"])
    if job["kind"] != "video":
        # 旧版关键帧 job 可能在升级前已持久化。它们不再恢复或执行，避免继续消耗图片额度，
        # 同时清除造成前端长期显示“生成中”的遗留状态。
        conn.execute("UPDATE shots SET scene_status='none' WHERE id=?", (job["shot_id"],))
        conn.commit()
        if _set_job(
            job["id"], "cancelled", "关键帧功能已下线；请从参考图视频入口重新生成",
            lease_owner=owner,
        ):
            media_scheduler.settle_budget(job["id"], 0.0, success=False)
        return
    version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (job["version_id"],)).fetchone()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (job["episode_id"],)).fetchone()

    meta = json.loads(version["image_inputs"] or "{}")
    result_adoptable = bool(
        job["video_slot_active"] and job["provider_result_adoptable"]
    )
    provider_recovery_only = bool(
        job["provider_poll_required"] and not result_adoptable
    )
    task_id = version["provider_task_id"]

    started = time.time()
    try:
        if not provider_recovery_only:
            await _assert_review_dependency_fence_async(
                job, version["id"], "worker_start",
            )
        provider_operation_id = (
            _row_value(job, "provider_operation_id")
            or f"video-create-{version['id']}"
        )
        recovered_at = None
        if not task_id:
            recovered = _recover_paid_video_task(conn, provider_operation_id)
            if recovered:
                task_id, recovered_at = recovered
        _assert_provider_create_resolved(job, task_id)
        provider_submitted_at = (
            recovered_at
            if recovered_at is not None
            else (
                _provider_submitted_at(
                    conn,
                    job,
                    task_id,
                    lease_owner=owner,
                )
                if task_id
                else None
            )
        )
        result = None
        if task_id:
            await _commit_provider_acceptance(
                conn,
                job_id=job_id,
                version_id=version["id"],
                owner=owner,
                operation_id=provider_operation_id,
                task_id=task_id,
                submitted_at=provider_submitted_at,
            )
        prompt_text = version["prompt_text"]
        if not provider_recovery_only:
            shot_model_for_prompt = _load_shot_model(shot)
            # 分镜台 2.0.0（app.production.storyboard_pack）行：prompt_text 已由
            # 模型直接产出并原样持久化（见该模块 persist_storyboard_pack 的文
            # 档），必须逐字送达供应商。ensure_source_excerpt_in_prompt 末尾会
            # 跑 sanitize_seedance_prompt——这类段落的 prompt_text 不含旧架构的
            # "[...]" 分段标记，会落进它的兜底分支 ``re.sub(r"\s+", " ", body)``，
            # 把模型写的镜头换行全部压成空格，等于在最后一公里悄悄改写了模型
            # 产出（实测复现：EP1 第 2 段入队时 858 字符、四行分镜头文本，经这
            # 一步变成 1143 字符的单行文本）。这道防线本身是为旧架构"一行 = 一
            # 个连续镜头"设计的原文重合擦除，对这类一段 = 3-4 镜的自由文本不适
            # 用也不必要，跳过。``getattr`` 防的是测试把 ``_load_shot_model``
            # 换成不带这个字段的替身对象。
            is_storyboard_pack_shot = (
                getattr(shot_model_for_prompt, "storyboard_pack_segment", None)
                is not None
            )
            if not is_storyboard_pack_shot:
                prompt_text = ensure_source_excerpt_in_prompt(
                    prompt_text,
                    shot_model_for_prompt,
                )
                if prompt_text != version["prompt_text"]:
                    _set_version(version["id"], prompt_text=prompt_text)
        try:
            if not task_id:
                operation_conn = _connection_for_heartbeat_operation(conn)
                await _assert_video_provider_submission_authority_async(
                    conn=conn,
                    job=job,
                    meta=meta,
                    actual_mode=str(
                        meta.get("planned_mode")
                        or meta.get("mode")
                        or video_modes.REFERENCE_IMAGE_MODE
                    ),
                    write_point="video_prompt_generate",
                )
                meta, prompt_text = await _await_with_job_lease_heartbeat(
                    _ensure_ai_video_prompt(
                        operation_conn, job, version, shot, ep, meta, prompt_text,
                    ),
                    job_id=job_id,
                    owner=owner,
                )
                meta, prompt_text = await _await_with_job_lease_heartbeat(
                    _prepare_planned_mode_inputs(
                        operation_conn, job, version, shot, ep, meta, prompt_text,
                        lease_owner=owner,
                    ),
                    job_id=job_id,
                    owner=owner,
                )
        except _ContinuityWait as wait_exc:
            wait = 15.0
            note = wait_exc.reason
            from app.media_pipeline import stages as media_stages
            set_pipeline_stage(
                job_id,
                (
                    media_stages.STAGE_WAITING_DEPENDENCY
                    if meta.get("shot_plan_id")
                    else media_stages.STAGE_WAITING_CONTINUITY
                ),
                reason_code=(
                    wait_exc.reason_code
                    if meta.get("shot_plan_id")
                    else "WAITING_CONTINUITY_ANCHOR"
                ),
                reason_text=note,
                conn=conn,
            )
            conn.execute(
                """UPDATE jobs SET status='queued', error=?, next_retry_at=?,
                          lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (note, now() + wait, now(), job_id, owner),
            )
            conn.execute(
                "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
                (job_id,),
            )
            conn.commit()
            task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
            _retry_tasks.add(task)
            task.add_done_callback(_retry_tasks.discard)
            return
        _assert_job_lease(job_id, owner)
        if not provider_recovery_only:
            await _assert_review_dependency_fence_async(
                job, version["id"], "provider_input_adoption",
            )

        # 连续镜调度级依赖：无可用尾帧时不得提交 Seedance
        if job["after_shot_id"] and not task_id:
            from app.media_pipeline.scheduler import continuity_anchor_ready
            from app.media_pipeline import stages as media_stages
            ready, reason = continuity_anchor_ready(
                conn,
                job["after_shot_id"],
                require_adopted=bool(meta.get("shot_plan_id")),
            )
            if not ready:
                wait = 15.0
                note = reason or "等待上一镜连续锚点"
                status = "waiting_human" if "人工" in note else "queued"
                set_pipeline_stage(
                    job_id,
                    (
                        media_stages.STAGE_WAITING_HUMAN
                        if status == "waiting_human"
                        else (
                            media_stages.STAGE_WAITING_DEPENDENCY
                            if meta.get("shot_plan_id")
                            else media_stages.STAGE_WAITING_CONTINUITY
                        )
                    ),
                    reason_code=(
                        "WAITING_VIDEO_PLAN_DEPENDENCY"
                        if meta.get("shot_plan_id")
                        else "WAITING_CONTINUITY_ANCHOR"
                    ),
                    reason_text=note,
                    conn=conn,
                )
                conn.execute(
                    """UPDATE jobs SET status=?, error=?, next_retry_at=?,
                              lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE id=? AND lease_owner=?""",
                    (status, note, now() + wait, now(), job_id, owner),
                )
                conn.execute(
                    "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
                    (job_id,),
                )
                conn.commit()
                if status == "queued":
                    task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
                    _retry_tasks.add(task)
                    task.add_done_callback(_retry_tasks.discard)
                return

        # 视频提交配额：首轮优先，重抽限额
        if not task_id:
            from app.media_pipeline.scheduler import can_admit_video_submit
            from app.media_pipeline import stages as media_stages
            is_retake = int(meta.get("auto_retake_count") or 0) > 0
            ok, reason = can_admit_video_submit(
                episode_id=job["episode_id"], project_id=job["project_id"], is_auto_retake=is_retake,
            )
            if not ok:
                wait = 20.0
                set_pipeline_stage(
                    job_id, media_stages.STAGE_WAITING_VIDEO_SLOT,
                    reason_code="EPISODE_VIDEO_INFLIGHT_FULL",
                    reason_text=reason or "等待视频槽位",
                    conn=conn,
                )
                conn.execute(
                    """UPDATE jobs SET status='queued', error=?, next_retry_at=?,
                              lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE id=? AND lease_owner=?""",
                    (reason or "等待视频槽位", now() + wait, now(), job_id, owner),
                )
                conn.execute(
                    "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
                    (job_id,),
                )
                conn.commit()
                task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
                _retry_tasks.add(task)
                task.add_done_callback(_retry_tasks.discard)
                return

        image_inputs: list[tuple[str, str]] | None = None
        video_inputs: list[tuple[str, str]] | None = None

        while True:
            if not task_id:  # 重启恢复时可能已有 task_id，直接续轮询
                _assert_job_lease(job_id, owner)
                if image_inputs is None:
                    image_inputs = _video_image_inputs_from_meta(meta)
                    video_inputs = video_modes.build_seedance_video_inputs(meta)
                    actual_mode = str(meta.get("mode") or video_modes.REFERENCE_IMAGE_MODE)
                    meta["actual_mode"] = actual_mode
                    if meta.get("mode") == video_modes.REFERENCE_IMAGE_MODE:
                        meta["reference_image_used"] = bool(image_inputs)
                        meta["first_frame_used"] = False
                        meta["last_frame_used"] = False
                        meta["reference_video_used"] = False
                    elif meta.get("mode") == video_modes.FIRST_FRAME_MODE:
                        meta["reference_image_used"] = False
                        meta["first_frame_used"] = any(
                            role == "first_frame" for _, role in image_inputs
                        )
                        meta["last_frame_used"] = False
                        meta["reference_video_used"] = False
                    elif meta.get("mode") == video_modes.FIRST_LAST_FRAME_MODE:
                        meta["reference_image_used"] = False
                        meta["first_frame_used"] = any(
                            role == "first_frame" for _, role in image_inputs
                        )
                        meta["last_frame_used"] = any(role == "last_frame" for _, role in image_inputs)
                        meta["reference_video_used"] = False
                    else:
                        meta["reference_image_used"] = False
                        meta["first_frame_used"] = False
                        meta["last_frame_used"] = False
                        meta["reference_video_used"] = bool(video_inputs)
                    _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
                try:
                    from app.media_pipeline import stages as media_stages
                    set_pipeline_stage(job_id, media_stages.STAGE_VIDEO_SUBMITTING, conn=conn)
                    submitting = conn.execute(
                        "UPDATE jobs SET provider_operation_id=?, provider_create_state='submitting', "
                        "updated_at=? WHERE id=? AND status='running' AND lease_owner=? "
                        "AND cancellation_requested=0",
                        (provider_operation_id, now(), job_id, owner),
                    )
                    if submitting.rowcount != 1:
                        conn.rollback()
                        raise LeaseLost(f"video submit lost lease: {job_id} / {owner}")
                    conn.commit()
                    from app.media_pipeline.concurrency import (
                        report_congestion, report_healthy, semaphore_for,
                    )
                    async with semaphore_for(media_stages.RESOURCE_VIDEO_SUBMIT):
                        _assert_job_lease(job_id, owner)
                        await _assert_review_dependency_fence_async(
                            job, version["id"], "provider_submit",
                        )
                        await _assert_video_provider_submission_authority_async(
                            conn=conn,
                            job=job,
                            meta=meta,
                            actual_mode=actual_mode,
                            write_point="provider_non_cancellable",
                        )
                        from app.media_pipeline.scheduler import claim_video_submit_slot

                        slot_claimed, slot_reason = claim_video_submit_slot(
                            job_id=job_id,
                            lease_owner=owner,
                            episode_id=str(job["episode_id"]),
                            project_id=str(job["project_id"]),
                            version_id=str(version["id"]),
                            operation_id=provider_operation_id,
                            amount_cny=shot_cost_cny(int(shot["duration_s"] or 0)),
                            is_auto_retake=int(meta.get("auto_retake_count") or 0) > 0,
                            conn=conn,
                        )
                        if not slot_claimed and slot_reason == "VIDEO_BUDGET_NOT_AUTHORIZED":
                            raise VideoBudgetAuthorizationError(
                                "本集缺少有效的视频费用授权，或本次供应商视频调用将超过"
                                "用户已批准的费用上限；任务已在付费调用前暂停"
                            )
                        if not slot_claimed:
                            raise VideoInflightAdmissionDeferred(
                                slot_reason or "等待视频槽位"
                            )
                        try:
                            try:
                                await _assert_video_provider_submission_authority_async(
                                    conn=conn,
                                    job=job,
                                    meta=meta,
                                    actual_mode=actual_mode,
                                    write_point="provider_create",
                                )
                            except BaseException:
                                # Provider create has not started. Every fence,
                                # cancellation and local failure must atomically
                                # release both the slot and payable budget claim.
                                _release_pre_call_video_claim(
                                    conn,
                                    job_id=job_id,
                                    owner=owner,
                                    operation_id=provider_operation_id,
                                )
                                raise
                            # From this line onward the transport may have sent
                            # the request. Unknown outcomes retain the durable
                            # claim and require explicit reconciliation.
                            task_id = await hiagent.create_video_task(
                                prompt_text,
                                image_urls=image_inputs,
                                video_urls=video_inputs,
                                return_last_frame=False,
                                call_meta={
                                    "asset_kind": "video",
                                    "planned_mode": meta.get("planned_mode"),
                                    "actual_mode": meta.get("actual_mode"),
                                    "video_input_intent": meta.get("video_input_intent"),
                                    "shot_plan_id": meta.get("shot_plan_id"),
                                    "capability_snapshot_id": meta.get("capability_snapshot_id"),
                                    "episode_id": ep["id"],
                                    "episode_no": ep["episode_no"],
                                    "shot_id": shot["id"],
                                    "shot_no": shot["shot_no"],
                                    "duration_s": shot["duration_s"],
                                    "version_id": version["id"],
                                    "version_no": version["version_no"],
                                    "operation_id": provider_operation_id,
                                })
                            await _commit_provider_acceptance(
                                conn,
                                job_id=job_id,
                                version_id=version["id"],
                                owner=owner,
                                operation_id=provider_operation_id,
                                task_id=task_id,
                            )
                            report_healthy(media_stages.RESOURCE_VIDEO_SUBMIT)
                        except ProviderError as submit_exc:
                            if submit_exc.retryable:
                                report_congestion(media_stages.RESOURCE_VIDEO_SUBMIT, reason="submit")
                            raise
                    _assert_job_lease(job_id, owner)
                except ProviderError as exc:
                    _assert_job_lease(job_id, owner)
                    create_outcome_unknown = _provider_create_outcome_unknown(exc)
                    create_state = (
                        "unknown" if create_outcome_unknown else "not_started"
                    )
                    changed = conn.execute(
                        """UPDATE jobs
                              SET provider_create_state=?,provider_non_cancellable=?,
                                  updated_at=?
                            WHERE id=? AND status='running' AND lease_owner=?
                              AND cancellation_requested=0""",
                        (
                            create_state,
                            int(create_outcome_unknown),
                            now(),
                            job_id,
                            owner,
                        ),
                    )
                    if changed.rowcount != 1:
                        conn.rollback()
                        raise LeaseLost(
                            f"video submit error lost lease: {job_id} / {owner}"
                        )
                    if not create_outcome_unknown:
                        released_at = now()
                        conn.execute(
                            """UPDATE provider_video_budget_claims
                                  SET status='released',updated_at=?,released_at=?
                                WHERE operation_id=? AND job_id=?""",
                            (
                                released_at,
                                released_at,
                                provider_operation_id,
                                job_id,
                            ),
                        )
                    conn.commit()
                    if create_outcome_unknown:
                        raise ProviderCreateUnresolved(
                            "[VIDEO_PROVIDER_CREATE_UNRESOLVED] Seedance create "
                            "结果不确定且本地没有 task id，已禁止自动重复 create；"
                            f"请在页面核对供应商任务（operation_id={provider_operation_id}, "
                            f"delivery_state={exc.delivery_state}, "
                            f"replay_safe={exc.replay_safe}, "
                            "requires_explicit_retry="
                            f"{exc.requires_explicit_retry}）"
                        ) from exc
                    raise
                if meta.get("shot_plan_id"):
                    from app.video_plan import (
                        VideoGenerationMode,
                        get_shot_plan,
                        record_mode_attempt,
                    )
                    active_shot_plan = get_shot_plan(job["shot_id"], conn=conn)
                    if (
                        not active_shot_plan
                        or active_shot_plan.shot_plan_id != meta.get("shot_plan_id")
                    ):
                        raise VideoPlanStaleFence("供应商接单后计划已失效，结果不得自动采用")
                    record_mode_attempt(
                        version_id=version["id"],
                        shot_plan=active_shot_plan,
                        actual_mode=VideoGenerationMode(meta["actual_mode"]),
                        status="provider_running",
                        provider_task_id=task_id,
                        conn=conn,
                    )
                try:
                    from app.media_pipeline import stages as media_stages
                    set_pipeline_stage(job_id, media_stages.STAGE_VIDEO_GENERATING, conn=conn)
                except Exception:  # noqa: BLE001
                    pass
                conn.commit()
                provider_submitted_at = conn.execute(
                    "SELECT provider_submitted_at FROM jobs WHERE id=?", (job_id,)
                ).fetchone()["provider_submitted_at"]

            # Phase 1：单次查询后立即释放 worker；供应商仍在跑则写入 waiting_provider。
            # 不再用 15 分钟连续占槽窗口（该预算常量已随功能下线一并删除）。
            state = conn.execute(
                "SELECT cancellation_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if state and state["cancellation_requested"]:
                media_scheduler.settle_budget(job_id, 0.0, success=False)
                return
            _assert_job_lease(job_id, owner)
            from app.media_pipeline.concurrency import (
                report_congestion, report_healthy, semaphore_for,
            )
            from app.media_pipeline import stages as media_stages
            async with semaphore_for(media_stages.RESOURCE_VIDEO_POLL):
                if not provider_recovery_only:
                    await _assert_review_dependency_fence_async(
                        job, version["id"], "provider_poll",
                    )
                try:
                    result = await hiagent.poll_video_task(
                        task_id,
                        call_meta={
                            "asset_kind": "video",
                            "episode_id": ep["id"],
                            "episode_no": ep["episode_no"],
                            "shot_id": shot["id"],
                            "shot_no": shot["shot_no"],
                            "version_id": version["id"],
                            "version_no": version["version_no"],
                            "task_id": task_id,
                        })
                    report_healthy(media_stages.RESOURCE_VIDEO_POLL)
                except ProviderError as poll_exc:
                    if poll_exc.retryable:
                        report_congestion(media_stages.RESOURCE_VIDEO_POLL, reason="poll")
                    raise
            _assert_job_lease(job_id, owner)
            if result is None or result["status"] not in ("succeeded", "failed"):
                policy = _provider_wait_policy(
                    task_id,
                    result or {},
                    meta,
                    duration_s=float(shot["duration_s"] or 5),
                    provider_submitted_at=float(
                        provider_submitted_at or time.time()
                    ),
                )
                if policy["meta_changed"]:
                    _set_version(
                        version["id"],
                        image_inputs=json.dumps(meta, ensure_ascii=False),
                    )
                if policy.get("stage_progress"):
                    set_pipeline_stage(
                        job_id,
                        media_stages.STAGE_VIDEO_GENERATING,
                        stage_progress=policy["stage_progress"],
                        conn=conn,
                    )
                    conn.commit()
                if policy["elapsed_s"] >= policy["timeout_s"]:
                    raise ProviderError(
                        f"{policy['scope']} {task_id} 已持续 "
                        f"{policy['elapsed_s'] / 60:.1f} 分钟，超过 "
                        f"{policy['timeout_s'] / 60:.1f} 分钟保护上限；"
                        "任务可能卡在上游，请联系供应商核查"
                    )
                if _defer_provider_poll(
                    job_id,
                    task_id,
                    lease_owner=owner,
                    delay=policy["poll_delay_s"],
                ):
                    return
                raise LeaseLost(f"provider poll defer lost lease: {job_id} / {owner}")
            if result["status"] == "failed":
                error_text = result["error"][:400]
                provider_label = str(
                    result.get("provider_label") or "视频模型"
                )
                failure = hiagent.ProviderFailure.from_provider_payload(
                    result.get("failure")
                )
                if failure.category is hiagent.ProviderFailureCategory.TECHNICAL:
                    # 供应商没有给出任何结构化分类信号时（真实案例：Seedance
                    # 版权拒绝，error.code 为空、无 failure 子对象），唯一可用
                    # 的是行为判据——同一 task_id 连续轮询给出字节级相同的
                    # 终态失败，判定为确定性拒绝而不是瞬时故障，升级分类。
                    # 判断的是结构（同一任务、同一结果、重复出现），不是内容
                    # （不看 message 里写了什么词），因此不构成关键词黑名单。
                    history = _prior_task_poll_failure_messages(conn, task_id)
                    if hiagent.has_repeated_terminal_poll_failure(history):
                        failure = hiagent.ProviderFailure.model_rejection(failure.kind)
                raise ProviderError(
                    f"{provider_label} 任务失败：{error_text}",
                    raw=error_text,
                    failure=failure,
                )
            break

        _assert_job_lease(job_id, owner)
        meta["provider_video_source_url"] = result["video_url"]
        # Current provider contract advertises a seven-day URL. Keep a
        # conservative six-day reuse window so downstream jobs never race expiry.
        meta["provider_video_source_url_expires_at"] = now() + 6 * 24 * 3600
        dest = _video_path(job["project_id"], ep["episode_no"], shot["shot_no"], version["version_no"])
        await hiagent.download(result["video_url"], str(dest))
        _assert_job_lease(job_id, owner)
        if not provider_recovery_only and meta.get("shot_plan_id"):
            from app.video_plan import active_plan_is_current
            if not active_plan_is_current(str(meta["shot_plan_id"]), conn=conn):
                raise VideoPlanStaleFence("视频生成完成时计划已失效，候选已隔离")
        supervisor_owner = _row_value(job, "owner_run_id")
        if supervisor_owner and not provider_recovery_only:
            current_owner = get_conn().execute(
                "SELECT active_video_run_id, video_completion_mode FROM episodes WHERE id=?",
                (job["episode_id"],),
            ).fetchone()
            fenced = (
                not current_owner
                or current_owner["video_completion_mode"] != "complete"
                or current_owner["active_video_run_id"] != supervisor_owner
            )
            if not fenced:
                try:
                    from app.video_supervisor import TERMINAL_SUPERVISOR_PHASES, load_latest_checkpoint
                    owner_cp = load_latest_checkpoint(job["episode_id"])
                    fenced = bool(
                        owner_cp
                        and (
                            owner_cp.dispatch_fenced_at is not None
                            or owner_cp.phase in TERMINAL_SUPERVISOR_PHASES
                        )
                    )
                except Exception:  # noqa: BLE001 — active run ownership remains the fallback fence
                    pass
            if fenced:
                from app.observability.metrics import inc
                inc(
                    "video_supervisor_orphan_provider_result_total",
                    episode_id=job["episode_id"],
                    owner_run_id=supervisor_owner,
                )
                media_scheduler.request_cancel(
                    job_id,
                    reason="结果到达时所属 Supervisor 已收口；候选已隔离，不参与自动采用",
                )
                return
        if not provider_recovery_only:
            await _assert_review_dependency_fence_async(
                job, version["id"], "candidate",
            )
        latency = round(time.time() - started, 1)
        paid_attempts = max(
            1,
            int(meta.get("provider_paid_attempts") or 0),
            _paid_video_attempt_count(conn, version["id"]),
        )
        meta["provider_paid_attempts"] = paid_attempts
        cost = shot_cost_cny(shot["duration_s"]) * paid_attempts
        result_adoptable = await _commit_video_result_checkpoint(
            conn,
            job_id=job_id,
            version_id=version["id"],
            owner=owner,
            operation_id=provider_operation_id,
            video_path=str(dest),
            last_frame_url=result["last_frame_url"],
            cost_cny=cost,
            latency_s=latency,
            image_inputs=json.dumps(meta, ensure_ascii=False),
        )
        if not result_adoptable:
            mark_media_job_state(
                _row_value(job, "run_id"),
                _row_value(job, "step_run_id"),
                "succeeded",
                "历史供应商任务结果已隔离",
            )
            reconcile_episode_generation_status(job["episode_id"])
            return
        if meta.get("shot_plan_id"):
            from app.video_plan import VideoGenerationMode, get_shot_plan, record_mode_attempt
            active_shot_plan = get_shot_plan(job["shot_id"], conn=conn)
            if active_shot_plan and active_shot_plan.shot_plan_id == meta.get("shot_plan_id"):
                record_mode_attempt(
                    version_id=version["id"],
                    shot_plan=active_shot_plan,
                    actual_mode=VideoGenerationMode(meta["actual_mode"]),
                    status="succeeded",
                    provider_task_id=task_id,
                    conn=conn,
                )
        # 生成台产生了新片段，旧的整集合成视频即过期 → 删除，避免成片台展示陈旧成品
        _invalidate_final_video(job["project_id"], ep["episode_no"])
        # 自动 QA 可能跑满 VLM 读超时（默认 300s），超过默认 180s lease 会被 sweeper
        # 抢占：原协程仍会跑完但无法 settle，新 worker 则对已成功版本重跑付费链路。
        _assert_job_lease(
            job_id,
            owner,
            lease_seconds=max(180.0, float(config.TIMEOUT_VLM_READ) + 60.0),
        )
        # 完整补齐模式只有 Supervisor 有权重抽和采用；Worker 只执行、校验并产出候选。
        supervisor_controlled = False
        try:
            ep_mode = get_conn().execute(
                "SELECT video_completion_mode FROM episodes WHERE id=?",
                (job["episode_id"],),
            ).fetchone()
            supervisor_controlled = bool(
                ep_mode and ep_mode["video_completion_mode"] == "complete"
            )
        except Exception:  # noqa: BLE001
            pass
        force_best = await _maybe_auto_qa(
            job,
            version["id"],
            str(dest),
            allow_autonomous_retake=not supervisor_controlled,
        )
        if supervisor_controlled:
            force_best = False
        _assert_job_lease(job_id, owner)
        await _assert_review_dependency_fence_async(
            job, version["id"], "candidate_evidence",
        )
        media_evidence.record_video_candidate(
            version["id"], step_run_id=_row_value(job, "step_run_id")
        )
        technical = json.loads(conn.execute(
            "SELECT technical_validation_json FROM shot_versions WHERE id=?", (version["id"],)
        ).fetchone()["technical_validation_json"] or "{}")
        if meta.get("shot_plan_id") and not _video_mode_input_roles_valid(meta):
            raise ProviderError("视频供应商输入角色与已发布模式计划不一致")
        if not technical.get("passed"):
            # 技术校验失败：在 technical_resubmit_limit 内自动新建版本重提
            from app.media_pipeline.retry_policy import technical_resubmit_limit
            resubmits = 0
            try:
                meta = json.loads(version["image_inputs"] or "{}")
                resubmits = int(meta.get("technical_resubmit_count") or 0)
            except Exception:  # noqa: BLE001
                resubmits = 0
            if not supervisor_controlled and resubmits < technical_resubmit_limit():
                if _set_job(job_id, "succeeded", lease_owner=owner):
                    media_scheduler.settle_budget(job_id, cost, success=True)
                    reconcile_episode_generation_status(job["episode_id"])
                    replacement = enqueue_shot(
                        job["shot_id"],
                        reroll=True,
                        after_shot_id=job["after_shot_id"],
                        auto_retake_count=resubmits + 1,
                        dependency_snapshot=meta.get("review_dependency_snapshot"),
                    )
                    # 标记新版本的 technical_resubmit_count（尽力而为）
                    try:
                        new_version_id = replacement.get("version_id")
                        new_ver = (
                            get_conn().execute(
                                "SELECT id,image_inputs FROM shot_versions WHERE id=?",
                                (new_version_id,),
                            ).fetchone()
                            if new_version_id else None
                        )
                        if new_ver:
                            import json as _json
                            m = _json.loads(new_ver["image_inputs"] or "{}")
                            if isinstance(m, dict):
                                m["technical_resubmit_count"] = resubmits + 1
                                get_conn().execute(
                                    "UPDATE shot_versions SET image_inputs=? WHERE id=?",
                                    (_json.dumps(m, ensure_ascii=False), new_ver["id"]),
                                )
                                get_conn().commit()
                    except Exception:  # noqa: BLE001
                        pass
                return
            raise ProviderError("视频文件技术校验失败，候选不可采用")
        if not supervisor_controlled:
            await _assert_review_dependency_fence_async(
                job, version["id"], "adoption_relation",
            )
            media_evidence.select_best_video_candidate(
                job["shot_id"], force_best=force_best
            )
            adopted = conn.execute(
                "SELECT adopted_version_id FROM shots WHERE id=?",
                (job["shot_id"],),
            ).fetchone()
            if adopted and adopted["adopted_version_id"]:
                from app.video_plan import reconcile_adopted_revision
                reconcile_adopted_revision(
                    job["shot_id"], adopted["adopted_version_id"], conn=conn,
                )
        if _set_job(job_id, "succeeded", lease_owner=owner):
            media_scheduler.settle_budget(job_id, cost, success=True)
            reconcile_episode_generation_status(job["episode_id"])
    except LeaseLost:
        return
    except VideoInflightAdmissionDeferred as exc:
        message = str(exc)
        changed = conn.execute(
            """UPDATE jobs
                  SET status='queued',error=?,reason_code='EPISODE_VIDEO_INFLIGHT_FULL',
                      reason_text=?,provider_non_cancellable=0,
                      provider_create_state='not_started',
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                  AND provider_non_cancellable=0""",
            (message, message, now() + 20.0, now(), job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return
        conn.execute(
            "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
            (job_id,),
        )
        conn.commit()
        task = asyncio.get_running_loop().create_task(_requeue_after(job_id, 20.0))
        _retry_tasks.add(task)
        task.add_done_callback(_retry_tasks.discard)
        return
    except VideoBudgetAuthorizationError as exc:
        message = str(exc)
        # 预算认领在"cap 与已认领总额零余量"附近可能被瞬时挤掉（同集其它镜
        # 头的认领/释放时序），不代表这一集真的超支——claim_video_submit_
        # slot 用的仍是人已经批准过的同一个 cap，没有申请新额度。像"槽位已
        # 满"（见上面 VideoInflightAdmissionDeferred 分支）一样先给几次有限
        # 退避重试的机会，比一次没挤上就直接卡死等人手再点一次 /generate 更
        # 合理；重试耗尽仍未通过，才落回下面真正需要人工处理的 paused_budget
        # 终态——预算保护本身没有放宽，只是不再让一次瞬时失败必须靠人手恢复。
        retry_count = int(meta.get("budget_pause_auto_retry_count") or 0)
        if retry_count < config.VIDEO_JOB_MAX_RETRIES:
            retry_count += 1
            delay = config.VIDEO_JOB_RETRY_BASE_DELAY * (2 ** (retry_count - 1))
            meta["budget_pause_auto_retry_count"] = retry_count
            note = (
                f"本集视频预算认领未通过，已在人工已批准的额度内自动重试第 "
                f"{retry_count}/{config.VIDEO_JOB_MAX_RETRIES} 次（约 {int(delay)} 秒后），"
                f"不会申请新的预算；若重试耗尽仍未通过才会暂停等待人工处理。原因：{message}"
            )
            retried = conn.execute(
                """UPDATE jobs
                      SET status='queued',error=?,reason_code='VIDEO_BUDGET_NOT_AUTHORIZED',
                          reason_text=?,provider_non_cancellable=0,
                          provider_create_state='not_started',
                          lease_owner=NULL,lease_expires_at=NULL,next_retry_at=?,
                          updated_at=?
                    WHERE id=? AND status='running' AND lease_owner=?""",
                (note, note, now() + delay, now(), job_id, owner),
            )
            if retried.rowcount == 1:
                conn.execute(
                    """UPDATE budget_reservations SET status='reserved'
                        WHERE job_id=? AND status='running'""",
                    (job_id,),
                )
                conn.commit()
                _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
                mark_media_job_state(
                    _row_value(job, "run_id"), _row_value(job, "step_run_id"), "queued", note,
                )
                task = asyncio.get_running_loop().create_task(_requeue_after(job_id, delay))
                _retry_tasks.add(task)
                task.add_done_callback(_retry_tasks.discard)
                return
            conn.rollback()
            return
        changed = conn.execute(
            """UPDATE jobs
                  SET status='paused_budget',error=?,reason_code='VIDEO_BUDGET_NOT_AUTHORIZED',
                      reason_text=?,provider_non_cancellable=0,
                      provider_create_state='not_started',
                      video_slot_active=0,
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,
                      updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?""",
            (message, message, now(), job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return
        _set_version(version["id"], status="paused_budget", error=message)
        conn.execute(
            "UPDATE shot_versions SET video_slot_active=0 WHERE id=?",
            (version["id"],),
        )
        conn.execute(
            """UPDATE budget_reservations
                  SET status='released',settled_at=?,actual_cost_cny=0
                WHERE job_id=? AND status IN ('reserved','running')""",
            (now(), job_id),
        )
        conn.commit()
        mark_media_job_state(
            _row_value(job, "run_id"),
            _row_value(job, "step_run_id"),
            "paused_budget",
            message,
        )
        reconcile_episode_generation_status(job["episode_id"])
        return
    except VideoPlanStaleFence as exc:
        public = str(exc)
        if _set_job(job_id, "stale", public, lease_owner=owner):
            _set_version(version["id"], status="stale", error=public)
            media_scheduler.settle_budget(job_id, 0.0, success=False)
            reconcile_episode_generation_status(job["episode_id"])
            # A sibling-only replan may preserve this shot's complete execution
            # contract. Recover the accepted provider handle in place when that
            # equivalence can be proven; never issue another create call.
            recover_equivalent_stale_provider_jobs(job["episode_id"])
        return
    except ReviewDependencyFence as exc:
        public = str(exc)
        if _set_job(job_id, "failed", public, lease_owner=owner):
            _set_version(version["id"], status="failed", error=public)
            media_scheduler.settle_budget(job_id, 0.0, success=False)
            reconcile_episode_generation_status(job["episode_id"])
        return
    except VideoInputRepairRequired as exc:
        repair_mode = str(
            meta.get("mode") or meta.get("planned_mode")
            or video_modes.REFERENCE_IMAGE_MODE
        )
        repair_label = {
            video_modes.FIRST_FRAME_MODE: "上一视频尾帧首帧",
            video_modes.FIRST_LAST_FRAME_MODE: "首尾帧",
            video_modes.REFERENCE_IMAGE_MODE: "参考图",
            video_modes.VIDEO_INPUT_MODE: "参考视频",
        }.get(repair_mode, "视频输入")
        repair_code = {
            video_modes.FIRST_FRAME_MODE: "FIRST_FRAME_REPAIR_REQUIRED",
            video_modes.FIRST_LAST_FRAME_MODE: "FIRST_LAST_FRAME_REPAIR_REQUIRED",
            video_modes.REFERENCE_IMAGE_MODE: "REFERENCE_IMAGE_REPAIR_REQUIRED",
            video_modes.VIDEO_INPUT_MODE: "VIDEO_INPUT_REPAIR_REQUIRED",
        }.get(repair_mode, "VIDEO_INPUT_REPAIR_REQUIRED")
        record = errors.log_error(
            exc,
            action="video_mode_input_repair",
            context={
                "shot_id": job["shot_id"],
                "version_id": version["id"],
                "job_id": job_id,
                "mode": meta.get("mode"),
            },
        )
        message = (
            f"{repair_label}输入仍需修复：{exc}。本镜保持 "
            f"{repair_mode}，"
            "未切换生成方式，也未提交不合格输入。"
            f"（{repair_code} · {record.error_id}）"
        )
        changed = conn.execute(
            """UPDATE jobs
                  SET status='waiting_human',error=?,
                      reason_code=?,
                      reason_text=?,lease_owner=NULL,lease_expires_at=NULL,
                      next_retry_at=NULL,video_slot_active=0,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?""",
            (message, repair_code, message, now(), job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return
        _set_version(version["id"], status="waiting_human", error=message, video_slot_active=0)
        conn.execute(
            """UPDATE shot_video_generation_plans
                  SET status='waiting_asset',updated_at=?
                WHERE id=?""",
            (now(), str(meta.get("shot_plan_id") or "")),
        )
        conn.commit()
        media_scheduler.settle_budget(job_id, 0.0, success=False)
        mark_media_job_state(
            _row_value(job, "run_id"),
            _row_value(job, "step_run_id"),
            "waiting_human",
            message,
        )
        reconcile_episode_generation_status(job["episode_id"])
        return
    except ProviderCreateUnresolved as exc:
        message = str(exc)
        if not _commit_provider_create_unresolved(
            conn,
            job_id=job_id,
            version_id=version["id"],
            owner=owner,
            message=message,
        ):
            return
        mark_media_job_state(
            _row_value(job, "run_id"),
            _row_value(job, "step_run_id"),
            "waiting_human",
            message,
        )
        reconcile_episode_generation_status(job["episode_id"])
        return
    except (ProviderError, Exception) as exc:  # noqa: BLE001 失败要响：原文进日志，前端给码+分类
        from app.harness.model_gateway import StructuredProviderRejection

        if isinstance(exc, StructuredProviderRejection):
            exc = ProviderError(
                "AI 视频提示词服务拒绝当前内容",
                raw=str(exc),
                failure=hiagent.ProviderFailure.model_rejection(
                    hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED
                ),
                delivery_state="not_sent",
                replay_safe=True,
            )
        if not media_scheduler.renew_lease(job_id, owner, lease_seconds=180.0):
            return
        record = errors.log_error(
            exc, action="shot_video_generate",
            context={"shot_id": job["shot_id"], "version_id": version["id"], "job_id": job_id})
        poll_state = conn.execute(
            "SELECT provider_poll_required FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        guidance = (
            _video_model_rejection_guidance(meta, exc)
            if isinstance(exc, ProviderError)
            else None
        )
        provider_failure = exc.failure if isinstance(exc, ProviderError) else None
        reason_code = (
            guidance[0]
            if guidance
            else (provider_failure.reason_code if provider_failure else None)
        )
        public = (
            f"{guidance[1]}（{guidance[0]} · {record.error_id}）"
            if guidance else record.public
        )
        provider_poll_pending = bool(
            task_id
            and poll_state is not None
            and poll_state["provider_poll_required"]
        )
        if (
            provider_poll_pending
            and (
                not isinstance(exc, ProviderError)
                or bool(provider_failure and provider_failure.retryable)
            )
            and _defer_provider_poll(
                job_id,
                task_id,
                lease_owner=owner,
            )
        ):
            return
        # 仅结构化 retryable 故障自动重排；重试耗尽后转人工，不改变原始类别。
        if isinstance(exc, ProviderError) and _schedule_job_retry(
            job_id, exc, lease_owner=owner
        ):
            _set_version(version["id"], status="queued")
            return
        external_terminal = bool(
            provider_failure
            and provider_failure.disposition
            is hiagent.ProviderFailureDisposition.EXTERNAL_TERMINAL
        )
        if provider_poll_pending and external_terminal:
            await _commit_provider_terminal_failure(
                conn,
                job_id=job_id,
                version_id=version["id"],
                owner=owner,
                operation_id=provider_operation_id,
                message=public,
                reason_code=reason_code or provider_failure.reason_code,
                failure=provider_failure,
            )
            if (
                provider_failure.kind
                != hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED.value
            ):
                conn.execute(
                    """UPDATE jobs SET provider_create_state='model_rejected'
                        WHERE id=? AND status='failed'""",
                    (job_id,),
                )
                conn.commit()
            mark_media_job_state(
                _row_value(job, "run_id"),
                _row_value(job, "step_run_id"),
                "failed",
                public,
            )
            reconcile_episode_generation_status(job["episode_id"])
            return
        final_status = (
            "waiting_human"
            if provider_failure and not external_terminal
            else "failed"
        )
        if _set_job(job_id, final_status, public, lease_owner=owner):
            conn.execute(
                """UPDATE video_generation_attempts
                      SET status='failed',error=?,updated_at=?
                    WHERE version_id=? AND status='provider_running'""",
                (str(exc)[:2000], now(), version["id"]),
            )
            if meta.get("shot_plan_id"):
                conn.execute(
                    """UPDATE shot_video_generation_plans
                          SET status='failed',updated_at=? WHERE id=?""",
                    (now(), str(meta["shot_plan_id"])),
                )
            if provider_failure:
                persisted_disposition = (
                    hiagent.ProviderFailureDisposition.EXTERNAL_TERMINAL
                    if external_terminal
                    else hiagent.ProviderFailureDisposition.MANUAL_REVIEW
                )
                conn.execute(
                    """UPDATE jobs
                          SET reason_code=?,reason_text=?,
                              provider_failure_category=?,
                              provider_failure_kind=?,
                              provider_failure_disposition=?,
                              provider_failure_retryable=?
                        WHERE id=? AND status=?""",
                    (
                        reason_code,
                        public,
                        provider_failure.category.value,
                        provider_failure.kind,
                        persisted_disposition.value,
                        int(provider_failure.retryable),
                        job_id,
                        final_status,
                    ),
                )
                if (
                    external_terminal
                    and provider_failure.kind
                    != hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED.value
                ):
                    conn.execute(
                        """UPDATE jobs SET provider_create_state='model_rejected'
                            WHERE id=? AND status='failed'""",
                        (job_id,),
                    )
            conn.commit()
            _set_version(version["id"], status=final_status, error=public)
            if provider_poll_pending:
                conn.execute(
                    """UPDATE budget_reservations
                          SET status='reserved'
                        WHERE job_id=? AND status='running'""",
                    (job_id,),
                )
                conn.commit()
            else:
                media_scheduler.settle_budget(job_id, 0.0, success=False)
            reconcile_episode_generation_status(job["episode_id"])


__all__ = [
    "ProviderCreateUnresolved",
    "ReviewDependencyFence",
    "VideoInflightAdmissionDeferred",
    "VideoInputRepairRequired",
    "VideoPlanStaleFence",
    "_ContinuityWait",
    "_SWEEPER_INTERVAL_SECONDS",
    "_assert_current_storyboard_completion_authority",
    "_assert_job_lease",
    "_assert_provider_create_resolved",
    "_assert_review_dependency_fence",
    "_assert_review_dependency_fence_async",
    "_assert_video_provider_submission_authority",
    "_assert_video_provider_submission_authority_async",
    "_authority_checks_can_use_worker_thread",
    "_auto_retake",
    "_await_with_job_lease_heartbeat",
    "_block_orphaned_continuity_job",
    "_claim_job_without_blocking_loop",
    "_commit_provider_acceptance",
    "_commit_provider_acceptance_in_transaction",
    "_commit_provider_create_unresolved",
    "_commit_provider_terminal_failure",
    "_commit_provider_terminal_failure_in_transaction",
    "_commit_video_result_checkpoint",
    "_commit_video_result_checkpoint_in_transaction",
    "_completed_reference_slots",
    "_connection_for_heartbeat_operation",
    "_defer_provider_poll",
    "_dispatch_due_jobs",
    "_dispatch_due_jobs_legacy",
    "_dispatch_due_jobs_stage_aware",
    "_dispatcher_task",
    "_drain_memory_queue",
    "_durable_dispatcher",
    "_enqueue_for_current_status",
    "_ensure_ai_video_prompt",
    "_image_dimensions",
    "_load_boundary_asset",
    "_maybe_auto_qa",
    "_narrative_keyframe_candidate_progress",
    "_normalize_boundary_pair",
    "_paid_video_attempt_count",
    "_persist_boundary_asset",
    "_poll_worker_target",
    "_prepare_first_frame_mode_inputs",
    "_prepare_first_last_mode_inputs",
    "_prepare_planned_mode_inputs",
    "_prepare_reference_mode_inputs",
    "_prepare_video_input_mode",
    "_prior_task_poll_failure_messages",
    "_provider_create_outcome_unknown",
    "_provider_submitted_at",
    "_provider_wait_policy",
    "_queue_job",
    "_recover_one_media_job",
    "_recover_paid_video_task",
    "_reference_gallery_ready",
    "_reference_worker_target",
    "_release_interrupted_worker_job",
    "_release_pre_call_video_claim",
    "_requeue_after",
    "_resolve_current_execution_plan",
    "_run_in_memory_write_transaction",
    "_run_job",
    "_schedule_job_retry",
    "_set_job",
    "_set_version",
    "_stale_lease_sweeper",
    "_start_durable_dispatcher",
    "_sweeper_task",
    "_video_image_inputs_from_meta",
    "_video_mode_input_roles_valid",
    "_video_model_rejection_guidance",
    "_video_ready_worker_target",
    "_wait_for_worker_job",
    "_worker_loop",
    "_worker_target",
    "ensure_workers",
    "reconcile_stalled_video_jobs",
    "recover_and_start",
    "recover_media_jobs",
    "retry_paused",
    "start_stale_lease_sweeper",
    "stop",
]
