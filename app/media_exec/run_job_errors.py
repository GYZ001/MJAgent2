"""``_run_job``'s ``except`` handlers, one function per exception type (see
``run_job.py``'s module docstring for the split map).

None of these call ``_assert_review_dependency_fence_async`` / ``ensure_
source_excerpt_in_prompt`` / ``_provider_wait_policy`` / ``_load_shot_model``
-- the four names ``tests/test_process_restart_recovery.py``'s bare
subprocess script patches directly on ``app.media_exec.run_job`` (see that
test's own comment) without going through ``tests.conftest.patch_worker_
everywhere``. Moving this code here is therefore safe without touching that
test. Each ``except`` clause in ``_run_job`` calls straight into its handler
here and returns/re-raises exactly what the handler does; moved verbatim
out of the pre-split single function otherwise.
"""
from __future__ import annotations

from typing import Any

from app import config, errors
from app.completion_grant import VideoBudgetAuthorizationError
from app.db import now
from app.orchestration import media_scheduler
from app.orchestration.media_runs import mark_media_job_state

from .common import _retry_tasks
from .enqueue import _row_value, recover_equivalent_stale_provider_jobs, reconcile_episode_generation_status
from .fences import (
    ProviderCreateUnresolved,
    ReviewDependencyFence,
    VideoInflightAdmissionDeferred,
    VideoInputRepairRequired,
    VideoPlanStaleFence,
)
from .checkpoints import _commit_provider_create_unresolved
from .job_state import _set_job, _set_version
from .retry_scheduling import _requeue_after


def _handle_admission_deferred(
    conn: Any, job_id: str, owner: str, exc: VideoInflightAdmissionDeferred,
) -> None:
    import asyncio

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


def _handle_budget_authorization_error(
    conn: Any,
    job: Any,
    job_id: str,
    owner: str,
    version: Any,
    meta: dict[str, Any],
    exc: VideoBudgetAuthorizationError,
) -> None:
    """Retry a transient budget-claim miss a bounded number of times, then pause for a human.

    预算认领在"cap 与已认领总额零余量"附近可能被瞬时挤掉（同集其它镜头的认领/
    释放时序），不代表这一集真的超支——claim_video_submit_slot 用的仍是人已经
    批准过的同一个 cap，没有申请新额度。像"槽位已满"一样先给几次有限退避重试
    的机会，比一次没挤上就直接卡死等人手再点一次 /generate 更合理；重试耗尽仍
    未通过，才落回真正需要人工处理的 paused_budget 终态——预算保护本身没有放
    宽，只是不再让一次瞬时失败必须靠人手恢复。
    """
    message = str(exc)
    retry_count = int(meta.get("budget_pause_auto_retry_count") or 0)
    if retry_count < config.VIDEO_JOB_MAX_RETRIES:
        _retry_budget_authorization(conn, job, job_id, owner, version, meta, message, retry_count)
        return
    _pause_for_budget_authorization(conn, job, job_id, owner, version, message)


def _retry_budget_authorization(
    conn: Any,
    job: Any,
    job_id: str,
    owner: str,
    version: Any,
    meta: dict[str, Any],
    message: str,
    retry_count: int,
) -> None:
    """Requeue the job for a bounded auto-retry within the already-approved budget."""
    import asyncio
    import json

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
    if retried.rowcount != 1:
        conn.rollback()
        return
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


def _pause_for_budget_authorization(
    conn: Any, job: Any, job_id: str, owner: str, version: Any, message: str,
) -> None:
    """Pause the job for human review once its bounded auto-retries are exhausted."""
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


def _handle_video_plan_stale(job: Any, job_id: str, owner: str, version: Any, exc: VideoPlanStaleFence) -> None:
    public = str(exc)
    if _set_job(job_id, "stale", public, lease_owner=owner):
        _set_version(version["id"], status="stale", error=public)
        media_scheduler.settle_budget(job_id, 0.0, success=False)
        reconcile_episode_generation_status(job["episode_id"])
        # A sibling-only replan may preserve this shot's complete execution
        # contract. Recover the accepted provider handle in place when that
        # equivalence can be proven; never issue another create call.
        recover_equivalent_stale_provider_jobs(job["episode_id"])


def _handle_review_dependency_fence(job: Any, job_id: str, owner: str, version: Any, exc: ReviewDependencyFence) -> None:
    public = str(exc)
    if _set_job(job_id, "failed", public, lease_owner=owner):
        _set_version(version["id"], status="failed", error=public)
        media_scheduler.settle_budget(job_id, 0.0, success=False)
        reconcile_episode_generation_status(job["episode_id"])


def _repair_mode_label_and_code(meta: dict[str, Any]) -> tuple[str, str, str]:
    """Resolve the human label and reason code for a video-input-repair fence.

    Returns ``(repair_mode, repair_label, repair_code)``.
    """
    from app import video_modes

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
    return repair_mode, repair_label, repair_code


def _handle_video_input_repair_required(
    conn: Any,
    job: Any,
    job_id: str,
    owner: str,
    version: Any,
    meta: dict[str, Any],
    exc: VideoInputRepairRequired,
) -> None:
    repair_mode, repair_label, repair_code = _repair_mode_label_and_code(meta)
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


def _handle_provider_create_unresolved(
    conn: Any, job: Any, job_id: str, owner: str, version: Any, exc: ProviderCreateUnresolved,
) -> None:
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


