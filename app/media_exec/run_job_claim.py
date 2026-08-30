"""``_run_job``'s pre-``try`` job-claim and row-load phase (see
``run_job.py``'s module docstring for the split map). Doesn't call
``_assert_review_dependency_fence_async`` / ``ensure_source_excerpt_in_prompt``
/ ``_provider_wait_policy`` / ``_load_shot_model`` (see ``run_job_errors.py``'s
module docstring for why that matters), so moving it here doesn't affect
``tests/test_process_restart_recovery.py``'s bare-subprocess monkeypatching.
Moved verbatim out of the pre-split single function -- only the wrapping
into a function returning ``None`` (caller must return) on every early-exit
path, instead of ``_run_job`` returning directly, is new.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from app.db import get_conn
from app.observability.tracing import set_worker_trace
from app.orchestration import media_scheduler
from app.orchestration.media_runs import mark_media_job_state

from .authority import _claim_job_without_blocking_loop
from .job_state import _set_job


@dataclass
class _ClaimedJob:
    """Everything ``_run_job`` needs from the claimed job/version/shot/episode rows."""

    conn: Any
    owner: str
    job: Any
    version: Any
    shot: Any
    ep: Any
    meta: dict[str, Any]
    result_adoptable: bool
    provider_recovery_only: bool
    task_id: str | None
    started: float


async def _claim_and_load_job(job_id: str, lease_owner: str | None) -> _ClaimedJob | None:
    """Claim the job (if not already leased) and load its version/shot/episode rows.

    Returns ``None`` when ``_run_job`` must return immediately (lease not
    claimed, row vanished/reassigned, or a non-video legacy job was
    cancelled in place).
    """
    # Workers are spawned during application recovery.  Give the lifespan and
    # HTTP server a scheduling boundary before any JSON decoding, authority
    # verification, or reference preparation below; otherwise a recovered
    # cohort can monopolize the event loop before the socket starts listening.
    await asyncio.sleep(1.0)
    conn = get_conn()
    owner = lease_owner or f"direct-{id(asyncio.current_task())}"
    job = await _claim_job_row(conn, job_id, owner, lease_owner)
    if job is None:
        return None
    if job["kind"] != "video":
        _cancel_legacy_keyframe_job(conn, job, owner)
        return None
    return _load_claimed_job_rows(conn, owner, job)


async def _claim_job_row(conn: Any, job_id: str, owner: str, lease_owner: str | None) -> Any | None:
    """Claim the lease (if not already held) and load the ``jobs`` row.

    Returns ``None`` when ``_run_job`` must return immediately.
    """
    if lease_owner is None:
        if not await _claim_job_without_blocking_loop(
            job_id,
            owner,
            lease_seconds=180.0,
        ):
            return None
        run_row = conn.execute(
            "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if run_row:
            mark_media_job_state(run_row["run_id"], run_row["step_run_id"], "running")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job["status"] != "running" or job["lease_owner"] != owner:
        return None
    # 这个 worker task 会在同一个 asyncio Task/Context 里连续 await 很多个 job；
    # 不重新绑定的话，provider_calls（尤其 video_create/video_poll）会带着上一个
    # job 的 trace，或者干脆一直是启动时的空 trace，链路树永远关联不到它们。
    # 详见 set_worker_trace 的说明：这里必须在本 job 的第一次供应商调用之前、
    # 每个 job 都调用一次，即使 run_id 为空也要显式清空上一个 job 留下的痕迹。
    set_worker_trace(job["run_id"], job["step_run_id"])
    return job


def _cancel_legacy_keyframe_job(conn: Any, job: Any, owner: str) -> None:
    """Cancel a pre-upgrade legacy keyframe job in place; these no longer run."""
    # 旧版关键帧 job 可能在升级前已持久化。它们不再恢复或执行，避免继续消耗图片额度，
    # 同时清除造成前端长期显示"生成中"的遗留状态。
    conn.execute("UPDATE shots SET scene_status='none' WHERE id=?", (job["shot_id"],))
    conn.commit()
    if _set_job(
        job["id"], "cancelled", "关键帧功能已下线；请从参考图视频入口重新生成",
        lease_owner=owner,
    ):
        media_scheduler.settle_budget(job["id"], 0.0, success=False)


def _load_claimed_job_rows(conn: Any, owner: str, job: Any) -> _ClaimedJob:
    """Load the version/shot/episode rows and derive the job's video-recovery flags."""
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

    return _ClaimedJob(
        conn=conn,
        owner=owner,
        job=job,
        version=version,
        shot=shot,
        ep=ep,
        meta=meta,
        result_adoptable=result_adoptable,
        provider_recovery_only=provider_recovery_only,
        task_id=task_id,
        started=time.time(),
    )
