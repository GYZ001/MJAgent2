"""调度分派与 worker 池生命周期（拆分自 ``run_job.py``）。

``_worker_target``/``_reference_worker_target``/``_video_ready_worker_target``/
``_poll_worker_target``/``_dispatcher_task``/``_sweeper_task`` 只被本文件自己的
``ensure_workers()``/``stop()``/``_start_durable_dispatcher()``/
``start_stale_lease_sweeper()`` 用 ``global`` 重新赋值，也只被本文件的
``_queue_job``/``_dispatch_due_jobs_legacy``/``_dispatch_due_jobs_stage_aware``
读取——``global`` 只重绑定函数所在模块自己的命名空间，定义处/写者/读者必须同
模块，拆分时未挪出本文件（详见 ``__init__.py`` 模块 docstring）。
``ensure_workers()`` 需要 ``.worker_loop._worker_loop``；``_stale_lease_sweeper()``
需要 ``.job_recovery`` 的恢复函数，而 ``.job_recovery.recover_and_start`` 反过来
需要本文件四个名字——真正的双向依赖，惰性导入打在 ``.job_recovery`` 一侧，本
文件保持顶层导入（与 ``.enqueue``/``.run_job`` 的既有惰性导入同一做法）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app import errors
from app.db import get_conn, now, rows_to_dicts

from .common import (
    _DISPATCH_BACKLOG_PER_WORKER,
    _DISPATCH_INTERVAL_SECONDS,
    _poll_queue,
    _poll_workers,
    _queue,
    _retry_tasks,
    _video_ready_queue,
    _video_ready_workers,
    _worker_retire_events,
    _workers,
)
from .job_recovery import _recover_one_media_job, reconcile_stalled_video_jobs
from .reference_progress import _auto_retake, _completed_reference_slots, _reference_gallery_ready
from .worker_loop import _worker_loop


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
        dispatcher = _dispatcher_task
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
             AND j.cancellation_requested=0 AND j.abandoned=0""",
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

    poll_capacity = max(1, _poll_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    poll_slots = max(0, poll_capacity - _poll_queue.qsize())
    main_capacity = max(1, _worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
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
             AND j.cancellation_requested=0 AND j.abandoned=0""",
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

    poll_capacity = max(1, _poll_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    poll_slots = max(0, poll_capacity - _poll_queue.qsize())
    vr_capacity = max(1, _video_ready_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    vr_slots = max(0, vr_capacity - _video_ready_queue.qsize())
    ref_capacity = max(1, _reference_worker_target or _worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
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
                if _worker_target > 0 or _video_ready_worker_target > 0:
                    ensure_workers()
            except Exception as exc:  # noqa: BLE001 dispatcher must remain alive
                errors.record_and_format(exc, action="durable_media_dispatch")
            await asyncio.sleep(_DISPATCH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        return


def _start_durable_dispatcher() -> None:
    global _dispatcher_task
    if _dispatcher_task is not None and not _dispatcher_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _dispatcher_task = loop.create_task(_durable_dispatcher(), name="durable-media-dispatcher")


def _drain_memory_queue(queue: asyncio.Queue[str]) -> None:
    """Drop startup duplicates; every durable row is rediscovered immediately."""
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        else:
            queue.task_done()


# 这五个名字必须放在真正用 global 语句重新赋值它们的模块里（ensure_workers()/
# stop() 是唯一的写者）：global 只重绑定当前模块自己的命名空间，放在别处会让
# 写入创建出一份外部模块永远看不到的私有副本，详见 common.py 模块 docstring。
_worker_target = 0
_reference_worker_target = 0
_video_ready_worker_target = 0
_poll_worker_target = 0
_dispatcher_task: asyncio.Task | None = None

_SWEEPER_INTERVAL_SECONDS = 60.0
_sweeper_task: asyncio.Task | None = None


async def _stale_lease_sweeper(interval_seconds: float = _SWEEPER_INTERVAL_SECONDS) -> None:
    """周期性回收卡死的媒体 job 的过期 lease。

    worker 进程被 kill -9、容器 OOM、协程异常退出等情况会让 job 卡在
    status='running' 且 lease_expires_at<now；recoverable_jobs() 只在启动时扫一次，
    启动后过期的 lease 不会被自动回收。本协程每 interval_seconds 秒扫一次，
    把过期 lease 的 job 复位回 queued，交给持久调度器在下一轮重新发现。

    幂等：多次扫到同一 job 时，第二次 CAS 会因 status 已是 'queued' 而 rowcount=0，
    不会重复恢复。"""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                conn = get_conn()
                stamp = now()
                rows = rows_to_dicts(conn.execute(
                    """SELECT id, run_id, step_run_id FROM jobs
                       WHERE status='running'
                         AND lease_expires_at IS NOT NULL
                         AND lease_expires_at < ?
                         AND cancellation_requested=0
                         AND abandoned=0""",
                    (stamp,),
                ))
                resumed = 0
                for r in rows:
                    if _recover_one_media_job(
                        conn, r["id"], r["run_id"], r["step_run_id"],
                        "lease 过期，自动回收并重新入队",
                    ):
                        resumed += 1
                conn.commit()
                reconcile_stalled_video_jobs()
            except Exception:  # noqa: BLE001 周期任务不能死
                pass
    except asyncio.CancelledError:
        return


def start_stale_lease_sweeper(interval_seconds: float = _SWEEPER_INTERVAL_SECONDS) -> None:
    """启动周期 lease 回收协程；多次调用幂等（已有任务在跑则不重启）。
    覆盖 worker 崩溃/OOM 等非服务重启场景下的中断恢复需求。"""
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _sweeper_task = loop.create_task(_stale_lease_sweeper(interval_seconds))
    _retry_tasks.add(_sweeper_task)
    _sweeper_task.add_done_callback(_retry_tasks.discard)


def ensure_workers(n: int | None = None) -> None:
    """分别维护参考图 / 视频提交 / 轮询三通道 worker。

    ``n`` 若给出，覆盖参考图通道目标；视频提交与 poll 始终读通道配置，
    修复「热更新只跟 video_submit、启动却取 max」的不一致。
    """
    global _worker_target, _reference_worker_target, _video_ready_worker_target, _poll_worker_target
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.concurrency import channel_limit

    ref_n = max(0, int(n if n is not None else channel_limit(media_stages.RESOURCE_REFERENCE)))
    video_n = max(0, int(channel_limit(media_stages.RESOURCE_VIDEO_SUBMIT)))
    poll_n = max(0, int(channel_limit(media_stages.RESOURCE_VIDEO_POLL)))

    _reference_worker_target = ref_n
    _worker_target = ref_n  # 兼容旧字段：代表参考图 worker
    _video_ready_worker_target = video_n
    _poll_worker_target = poll_n

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def _discard_worker(pool: list[asyncio.Task], task: asyncio.Task) -> None:
        _worker_retire_events.pop(task, None)
        try:
            pool.remove(task)
        except ValueError:
            pass

    def _resize(pool: list[asyncio.Task], target: int, prefix: str, queue: asyncio.Queue[str]) -> None:
        for task in tuple(pool):
            if task.done():
                _discard_worker(pool, task)
        accepting = [
            task for task in pool
            if not _worker_retire_events[task].is_set()
        ]
        while len(accepting) < target:
            used_names = {task.get_name() for task in pool}
            index = 0
            while f"{prefix}{index}" in used_names:
                index += 1
            name = f"{prefix}{index}"
            retirement = asyncio.Event()
            task = loop.create_task(
                _worker_loop(name, queue, retirement),
                name=name,
            )
            _worker_retire_events[task] = retirement
            task.add_done_callback(
                lambda done, worker_pool=pool: _discard_worker(worker_pool, done)
            )
            pool.append(task)
            accepting.append(task)
        for task in reversed(accepting[target:]):
            _worker_retire_events[task].set()

    _resize(_workers, ref_n, "ref", _queue)
    _resize(_video_ready_workers, video_n, "vr", _video_ready_queue)
    _resize(_poll_workers, poll_n, "poll", _poll_queue)


async def stop() -> None:
    """优雅停机：取消常驻 worker 循环。否则 uvicorn --reload/退出时会卡在
    'Waiting for connections to close'——常驻 while-True 任务不退出，停机就挂起。"""
    global _sweeper_task, _dispatcher_task, _worker_target, _poll_worker_target
    global _reference_worker_target, _video_ready_worker_target
    try:
        from app.media_pipeline.bootstrap import stop_media_pipeline
        await stop_media_pipeline()
    except Exception:  # noqa: BLE001
        pass
    if _sweeper_task is not None:
        _sweeper_task.cancel()
    if _dispatcher_task is not None:
        _dispatcher_task.cancel()
        try:
            await _dispatcher_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _dispatcher_task = None
    for t in _retry_tasks:
        t.cancel()
    if _retry_tasks:
        await asyncio.gather(*tuple(_retry_tasks), return_exceptions=True)
    _retry_tasks.clear()
    for t in (*_workers, *_video_ready_workers, *_poll_workers):
        retirement = _worker_retire_events.get(t)
        if retirement is not None:
            retirement.set()
        t.cancel()
    for t in (*_workers, *_video_ready_workers, *_poll_workers):
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _workers.clear()
    _video_ready_workers.clear()
    _poll_workers.clear()
    _worker_retire_events.clear()
    _worker_target = 0
    _reference_worker_target = 0
    _video_ready_worker_target = 0
    _poll_worker_target = 0
    _drain_memory_queue(_queue)
    _drain_memory_queue(_video_ready_queue)
    _drain_memory_queue(_poll_queue)

__all__ = [name for name in globals() if not name.startswith("__")]
