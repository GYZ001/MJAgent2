"""持久调度分派（拆分自 ``worker_lifecycle.py``，2026-08-30）。

``worker_lifecycle.py`` 原来把「调度分派」与「worker 池生命周期」两件事放在
一个文件里，加宽 ``deleted_at`` 判据（挡掉软删除项目残留的排队视频 job 被继
续派发）后单文件超过 600 行的文件规范上限，因此按文件自己的模块 docstring
早就点名的两个关注点切开：本文件拿走「派发」（``_enqueue_for_current_status``/
``_queue_job``/``_dispatch_due_jobs_legacy``/``_dispatch_due_jobs_stage_aware``/
``_dispatch_due_jobs``/``_durable_dispatcher``/``_start_durable_dispatcher``），
``worker_lifecycle.py`` 留下「worker 池生命周期」（``ensure_workers()``/
``stop()``/``_stale_lease_sweeper()``）。

``_worker_target``/``_reference_worker_target``/``_video_ready_worker_target``/
``_poll_worker_target``/``_dispatcher_task`` 五个全局状态**物理声明留在
``worker_lifecycle.py``**，没有跟着搬——前四个只被该文件的 ``ensure_workers()``
用 ``global`` 语句重新赋值，``_dispatcher_task`` 只被该文件的 ``stop()`` 用
``global`` 语句重新赋值；Python 的 ``global`` 只能重绑定语句所在函数的那个模
块自己的命名空间，本文件若用 ``from .worker_lifecycle import _worker_target``
之类的裸名字导入，拿到的只是导入那一刻的整数快照，之后 ``ensure_workers()``/
``stop()`` 的重新赋值本文件永远看不到。因此本文件全程用**限定属性访问**
``worker_lifecycle._worker_target``（读）/``worker_lifecycle._dispatcher_task
= ...``（写，``_start_durable_dispatcher()`` 里）——属性赋值直接落在
``worker_lifecycle`` 模块自己的 ``__dict__``，不受 ``global`` 语句的作用域限
制，与该文件内部的 ``global`` 读写天然是同一份，不会产生第二份副本。

依赖方向单向：本文件顶层 ``from . import worker_lifecycle``；反过来
``worker_lifecycle.py`` 的 ``ensure_workers()``/``stop()`` 都不需要本文件任何
名字（``stop()`` 自己直接管 ``_dispatcher_task`` 的取消，见该文件），没有借
用本文件做任何调用，因此不构成双向依赖，``worker_lifecycle.py`` 侧不需要为
本文件加任何 import（既有的 ``.job_recovery`` 双向依赖用惰性导入打破，与本
次拆分无关，见 ``job_recovery.py``/``worker_lifecycle.py`` 各自模块 docstring）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app import errors
from app.db import get_conn, now, rows_to_dicts

from . import worker_lifecycle
from .common import (
    _DISPATCH_BACKLOG_PER_WORKER,
    _DISPATCH_INTERVAL_SECONDS,
    _poll_queue,
    _queue,
    _video_ready_queue,
)
from .reference_progress import _auto_retake, _completed_reference_slots, _reference_gallery_ready


def _enqueue_for_current_status(job_id: str) -> None:
    """按阶段路由到 finalize / video-ready / reference 通道。

    三通道仍共用同一 durable job 行与 CAS lease；拆分只影响调度优先级：
    已接单/待收尾绝不能排在整集参考图后面。
    """
    row = get_conn().execute(
        """SELECT j.status, j.pipeline_stage, v.provider_task_id, v.image_inputs, j.after_shot_id
           FROM jobs j LEFT JOIN shot_versions v ON v.id=j.version_id
           WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        return
    if row["status"] == "waiting_provider" or row["provider_task_id"]:
        _queue_job(_poll_queue, job_id)
        return
    from app.media_pipeline.scheduler import continuity_anchor_ready, is_true_video_ready, scheduler_policy
    from app.media_pipeline import stages as media_stages
    meta = {}
    try:
        meta = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    continuity_ok = True
    if row["after_shot_id"]:
        continuity_ok = continuity_anchor_ready(
            get_conn(),
            row["after_shot_id"],
            require_adopted=bool(meta.get("shot_plan_id")),
        )[0]
    stage = row["pipeline_stage"]
    ready = (
        stage == media_stages.STAGE_VIDEO_READY
        or is_true_video_ready(meta, continuity_ok=continuity_ok)
    )
    if scheduler_policy() == "stage_aware" and ready:
        _queue_job(_video_ready_queue, job_id)
    else:
        _queue_job(_queue, job_id)


def _queue_job(queue: asyncio.Queue[str], job_id: str) -> None:
    """Route durable work to an asyncio queue from loop or worker threads."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        dispatcher = worker_lifecycle._dispatcher_task
        if dispatcher is not None and not dispatcher.done():
            dispatcher.get_loop().call_soon_threadsafe(
                queue.put_nowait,
                job_id,
            )
        else:
            queue.put_nowait(job_id)
        return
    queue.put_nowait(job_id)


def _dispatch_due_jobs_legacy() -> dict[str, int]:
    """旧调度：poll 优先 + 主队列混合参考图/视频提交。"""
    conn = get_conn()
    stamp = now()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id, j.status, j.created_at, j.after_shot_id,
                  v.provider_task_id, v.image_inputs, s.shot_no
           FROM jobs j
           LEFT JOIN shot_versions v ON v.id=j.version_id
           LEFT JOIN shots s ON s.id=j.shot_id
           WHERE j.kind='video'
             AND j.status IN ('queued','waiting_provider')
             AND (j.next_retry_at IS NULL OR j.next_retry_at<=?)
             AND j.cancellation_requested=0 AND j.abandoned=0
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS: durable dispatcher
               -- scans every owner's due media jobs once per second while
               -- the process is running; excludes soft-deleted (recycle-bin)
               -- projects so their residual jobs are not dispatched to a
               -- worker and do not burn quota after project deletion
                WHERE p.id=j.project_id AND p.deleted_at IS NOT NULL
             )""",
        (stamp,),
    ).fetchall())

    poll_candidates: list[dict[str, Any]] = []
    main_candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    blocked_reference_candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    continuity_cache: dict[tuple[str, bool], bool] = {}

    for row in rows:
        if row.get("status") == "waiting_provider" or row.get("provider_task_id"):
            poll_candidates.append(row)
            continue
        try:
            dependency_meta = json.loads(row.get("image_inputs") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            dependency_meta = {}
        after_shot_id = row.get("after_shot_id")
        if after_shot_id:
            require_adopted = bool(dependency_meta.get("shot_plan_id"))
            cache_key = (after_shot_id, require_adopted)
            ready = continuity_cache.get(cache_key)
            if ready is None:
                from app.media_pipeline.scheduler import continuity_anchor_ready
                ready = continuity_anchor_ready(
                    conn, after_shot_id, require_adopted=require_adopted,
                )[0]
                continuity_cache[cache_key] = ready
        else:
            ready = True
        refs_ready = _reference_gallery_ready(row.get("image_inputs"))
        is_retake = _auto_retake(row.get("image_inputs"))
        age_key = float(row.get("created_at") or stamp)
        shot_key = int(row.get("shot_no") or 10**9)
        if ready:
            rank = 2 if is_retake else (0 if refs_ready else 1)
            main_candidates.append(((rank, age_key, shot_key), row))
        elif not refs_ready:
            rank = 1 if is_retake else 0
            blocked_reference_candidates.append(((rank, age_key, shot_key), row))

    poll_candidates.sort(key=lambda row: float(row.get("created_at") or stamp))
    main_candidates.sort(key=lambda item: item[0])
    blocked_reference_candidates.sort(key=lambda item: item[0])

    poll_capacity = max(1, worker_lifecycle._poll_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    poll_slots = max(0, poll_capacity - _poll_queue.qsize())
    main_capacity = max(1, worker_lifecycle._worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    main_slots = max(0, main_capacity - _queue.qsize())

    poll_enqueued = 0
    for row in poll_candidates[:poll_slots]:
        _queue_job(_poll_queue, row["id"])
        poll_enqueued += 1

    chosen = [row for _, row in main_candidates[:main_slots]]
    remaining = max(0, main_slots - len(chosen))
    if remaining:
        from app.media_pipeline.retry_policy import prepared_reference_backlog
        speculative_limit = min(remaining, prepared_reference_backlog())
        chosen.extend(row for _, row in blocked_reference_candidates[:speculative_limit])
    for row in chosen:
        _queue_job(_queue, row["id"])

    return {"poll": poll_enqueued, "main": len(chosen), "due": len(rows), "video_ready": 0, "reference": len(chosen)}


def _dispatch_due_jobs_stage_aware() -> dict[str, int]:
    """QPSP：finalize > video_ready > reference(cohort) > retake；高低水位背压。"""
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.scheduler import (
        classify_scheduler_lane,
        continuity_anchor_ready,
        continuity_chain_remaining,
        is_true_video_ready,
        job_scheduler_score,
        should_start_more_reference_work,
    )
    from app.media_pipeline.stage_state import set_pipeline_stage

    conn = get_conn()
    stamp = now()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id, j.status, j.created_at, j.after_shot_id, j.episode_id, j.pipeline_stage,
                  v.provider_task_id, v.image_inputs, s.shot_no, s.id AS shot_pk
           FROM jobs j
           LEFT JOIN shot_versions v ON v.id=j.version_id
           LEFT JOIN shots s ON s.id=j.shot_id
           WHERE j.kind='video'
             AND j.status IN ('queued','waiting_provider')
             AND (j.next_retry_at IS NULL OR j.next_retry_at<=?)
             AND j.cancellation_requested=0 AND j.abandoned=0
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS: durable dispatcher
               -- scans every owner's due media jobs once per second while
               -- the process is running; excludes soft-deleted (recycle-bin)
               -- projects so their residual jobs are not dispatched to a
               -- worker and do not burn quota after project deletion
                WHERE p.id=j.project_id AND p.deleted_at IS NOT NULL
             )""",
        (stamp,),
    ).fetchall())

    poll_candidates: list[dict[str, Any]] = []
    video_ready: list[tuple[float, dict[str, Any]]] = []
    reference_critical: list[tuple[float, dict[str, Any]]] = []
    reference_normal: list[tuple[float, dict[str, Any]]] = []
    retake_jobs: list[tuple[float, dict[str, Any]]] = []
    continuity_cache: dict[tuple[str, bool], bool] = {}
    stage_updates: list[tuple[str, str, dict[str, Any]]] = []

    for row in rows:
        if row.get("status") == "waiting_provider" or row.get("provider_task_id"):
            poll_candidates.append(row)
            continue
        meta = {}
        try:
            meta = json.loads(row.get("image_inputs") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        after_shot_id = row.get("after_shot_id")
        if after_shot_id:
            require_adopted = bool(meta.get("shot_plan_id"))
            cache_key = (after_shot_id, require_adopted)
            ready = continuity_cache.get(cache_key)
            if ready is None:
                ready = continuity_anchor_ready(
                    conn, after_shot_id, require_adopted=require_adopted,
                )[0]
                continuity_cache[cache_key] = ready
        else:
            ready = True
        refs_ready = _reference_gallery_ready(row.get("image_inputs"))
        static_waiting = bool(meta.get("reference_static_ready")) and not refs_ready
        true_ready = (
            row.get("pipeline_stage") == media_stages.STAGE_VIDEO_READY
            or is_true_video_ready(meta, continuity_ok=ready)
        )
        is_retake = _auto_retake(row.get("image_inputs"))
        age_min = max(0.0, (stamp - float(row.get("created_at") or stamp)) / 60.0)
        chain = 0
        if row.get("episode_id") and row.get("shot_pk"):
            chain = continuity_chain_remaining(conn, row["episode_id"], row["shot_pk"])
        completed = _completed_reference_slots(row.get("image_inputs"))
        score = job_scheduler_score(
            first_pass=not is_retake,
            continuity_remaining=chain,
            completed_slots=completed,
            wait_age_minutes=age_min,
            auto_retake=is_retake,
        )
        critical = chain > 0 or bool(after_shot_id)
        lane = classify_scheduler_lane(
            refs_ready=true_ready or refs_ready,
            continuity_ok=ready,
            is_retake=is_retake,
            static_ready_waiting=static_waiting,
            critical_path=critical,
        )
        if true_ready and ready:
            stage_updates.append((
                row["id"],
                media_stages.STAGE_VIDEO_READY,
                {
                    "scheduler_lane": media_stages.LANE_VIDEO_READY,
                    "priority_class": "first_pass" if not is_retake else "retake",
                },
            ))
        elif not ready and (refs_ready or static_waiting):
            stage_updates.append((
                row["id"],
                (
                    media_stages.STAGE_WAITING_DEPENDENCY
                    if meta.get("shot_plan_id")
                    else media_stages.STAGE_WAITING_CONTINUITY
                ),
                {
                    "reason_code": (
                        "WAITING_VIDEO_PLAN_DEPENDENCY"
                        if meta.get("shot_plan_id")
                        else "WAITING_CONTINUITY_ANCHOR"
                    ),
                    "reason_text": (
                        "等待上一镜采用素材"
                        if meta.get("shot_plan_id")
                        else (
                            f"等待镜尾帧（{after_shot_id}）"
                            if after_shot_id else "等待上一镜尾帧"
                        )
                    ),
                    "scheduler_lane": media_stages.LANE_REFERENCE_CRITICAL,
                },
            ))

        if true_ready and ready:
            video_ready.append((score, row))
        elif is_retake:
            retake_jobs.append((score, row))
        elif not ready and refs_ready:
            # 参考已齐但等尾帧：不占参考图 cohort，也不进 video_ready
            continue
        elif lane == media_stages.LANE_REFERENCE_CRITICAL or critical:
            reference_critical.append((score, row))
        else:
            reference_normal.append((score, row))

    # Keep recursive continuity reads outside the single-writer transaction.
    # Otherwise the first stage UPDATE holds SQLite's writer lock while the
    # remaining dependency chains are still being traversed.
    try:
        for job_id, stage, kwargs in stage_updates:
            set_pipeline_stage(job_id, stage, conn=conn, **kwargs)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    poll_candidates.sort(key=lambda row: float(row.get("created_at") or stamp))
    video_ready.sort(key=lambda item: -item[0])
    reference_critical.sort(key=lambda item: -item[0])
    reference_normal.sort(key=lambda item: -item[0])
    retake_jobs.sort(key=lambda item: -item[0])

    poll_capacity = max(1, worker_lifecycle._poll_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    poll_slots = max(0, poll_capacity - _poll_queue.qsize())
    vr_capacity = max(1, worker_lifecycle._video_ready_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    vr_slots = max(0, vr_capacity - _video_ready_queue.qsize())
    ref_capacity = max(1, worker_lifecycle._reference_worker_target or worker_lifecycle._worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    ref_slots = max(0, ref_capacity - _queue.qsize())

    poll_enqueued = 0
    for row in poll_candidates[:poll_slots]:
        _queue_job(_poll_queue, row["id"])
        poll_enqueued += 1

    vr_enqueued = 0
    for _, row in video_ready[:vr_slots]:
        _queue_job(_video_ready_queue, row["id"])
        vr_enqueued += 1

    # 参考图：cohort + 高低水位；关键路径优先
    allow, demand = should_start_more_reference_work(conn=conn)
    ref_enqueued = 0
    if allow and demand > 0 and ref_slots > 0:
        budget = min(demand, ref_slots)
        ordered = reference_critical + reference_normal + retake_jobs
        for _, row in ordered[:budget]:
            _queue_job(_queue, row["id"])
            ref_enqueued += 1
    elif ref_slots > 0 and reference_critical:
        # 水位满时仍允许完成已接近完成的关键路径（只取 critical，且仅当已有 slot 进度）
        for _, row in reference_critical:
            if ref_enqueued >= ref_slots:
                break
            if _completed_reference_slots(row.get("image_inputs")) > 0:
                _queue_job(_queue, row["id"])
                ref_enqueued += 1

    return {
        "poll": poll_enqueued,
        "main": vr_enqueued + ref_enqueued,
        "due": len(rows),
        "video_ready": vr_enqueued,
        "reference": ref_enqueued,
    }


def _dispatch_due_jobs() -> dict[str, int]:
    """Continuously rebuild the runnable queues from durable job state."""
    from app.media_pipeline.scheduler import scheduler_policy
    if scheduler_policy() == "legacy":
        return _dispatch_due_jobs_legacy()
    return _dispatch_due_jobs_stage_aware()


async def _durable_dispatcher() -> None:
    """DB-backed dispatcher; in-memory queue loss heals within one interval."""
    try:
        while True:
            try:
                await asyncio.to_thread(_dispatch_due_jobs)
                # Recreate an unexpectedly dead worker without changing the
                # configured target. Worker loops catch job errors themselves,
                # so this is primarily protection against lifecycle regressions.
                if worker_lifecycle._worker_target > 0 or worker_lifecycle._video_ready_worker_target > 0:
                    worker_lifecycle.ensure_workers()
            except Exception as exc:  # noqa: BLE001 dispatcher must remain alive
                errors.record_and_format(exc, action="durable_media_dispatch")
            await asyncio.sleep(_DISPATCH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        return


def _start_durable_dispatcher() -> None:
    # 限定属性赋值，不是 global：_dispatcher_task 的物理声明与 stop() 的
    # global 读写都在 worker_lifecycle.py，见本文件模块 docstring。
    if worker_lifecycle._dispatcher_task is not None and not worker_lifecycle._dispatcher_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    worker_lifecycle._dispatcher_task = loop.create_task(
        _durable_dispatcher(), name="durable-media-dispatcher"
    )


__all__ = [name for name in globals() if not name.startswith("__")]
