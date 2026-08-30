"""失败安全终态标记与带心跳线程的韧性外壳。"""
from __future__ import annotations

import asyncio
import threading

from typing import Any

from app.db import get_conn, now
from app.evidence import repository as evidence_repository

from .checkpoint import (
    _refresh_supervisor_heartbeat,
    _run_checkpoint_write,
    _save_checkpoint_async,
    load_latest_checkpoint,
    save_checkpoint,
)
from .closeout import _deadline_closeout_async
from .constants import CONTROL_PLANE_MAX_RECOVERIES, LIFECYCLE_HEARTBEAT_INTERVAL_S, SUPERVISOR_HEARTBEAT_STALE_S
from .job_control import _release_episode_supervisor, _stop_supervised_video_jobs
from .models import VideoSupervisorCheckpoint
from .run_loop import run_video_completion_supervisor



def _mark_failed_closed(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    reason: str,
) -> VideoSupervisorCheckpoint:
    """连收口协议自身也失败时的最小安全终态。"""
    cp.phase = "FAILED_CLOSED"
    cp.outcome = "FAILED_CLOSED"
    cp.terminal_reason = reason
    cp.dispatch_fenced_at = cp.dispatch_fenced_at or now()
    cp.finished_at = now()
    cp.quality_target_missed = True
    try:
        _stop_supervised_video_jobs(cp.episode_id, run_id=run_id or cp.run_id, reason=reason)
    except Exception:  # noqa: BLE001
        pass
    try:
        save_checkpoint(cp, run_id=run_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        _release_episode_supervisor(cp.episode_id, run_id=run_id or cp.run_id)
    except Exception:  # noqa: BLE001
        # 最后一层仍尝试直接清掉假运行标记。
        conn = get_conn()
        conn.execute(
            """UPDATE episodes SET video_completion_mode='quick', active_video_run_id=NULL,
                      status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
               WHERE id=?""",
            (cp.episode_id,),
        )
        conn.commit()
    return cp


async def _mark_failed_closed_async(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    reason: str,
) -> VideoSupervisorCheckpoint:
    return await _run_checkpoint_write(
        _mark_failed_closed,
        cp,
        run_id=run_id,
        reason=reason,
    )


async def _run_video_completion_resilient_loop(
    episode_id: str,
    **kwargs: Any,
) -> VideoSupervisorCheckpoint:
    """控制面异常自动续跑；连续失败后仍执行候选收口并停止所有付费任务。"""
    run_id = kwargs.get("run_id")
    recoveries = 0
    while True:
        try:
            return await run_video_completion_supervisor(episode_id, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 控制面必须 fail closed，不能裸奔 Worker
            from app.observability.metrics import inc
            inc(
                "video_supervisor_failed_with_active_jobs_total",
                episode_id=episode_id,
                error_type=type(exc).__name__,
            )
            recoveries += 1
            cp = load_latest_checkpoint(episode_id)
            if run_id and (cp is None or cp.run_id != run_id):
                # A fresh run may fail before its first checkpoint is durable.
                # Never recover from an older execution epoch: its attempt
                # counters, budget and terminal fences belong to cleared work.
                cp = None
            cp = cp or VideoSupervisorCheckpoint(
                episode_id=episode_id,
                run_id=run_id,
                phase="RECOVERING_CONTROL_PLANE",
                started_at=now(),
            )
            cp.control_plane_recoveries = max(cp.control_plane_recoveries, recoveries)
            cp.phase = "RECOVERING_CONTROL_PLANE"
            cp.outcome = f"{type(exc).__name__}: {str(exc)[:500]}"
            if cp.deadline_at is None:
                wall_cap = float(kwargs.get("wall_clock_cap_s") or 4 * 3600)
                cp.deadline_at = (cp.started_at or now()) + wall_cap
            try:
                await _save_checkpoint_async(cp, run_id=run_id)
                if run_id:
                    evidence_repository.append_event(
                        run_id,
                        "VIDEO_CONTROL_PLANE_RECOVERING",
                        "error",
                        f"Supervisor 控制面异常，自动恢复 {recoveries}/{CONTROL_PLANE_MAX_RECOVERIES}",
                        payload={"error_type": type(exc).__name__, "message": str(exc)[:1000]},
                    )
            except Exception:  # noqa: BLE001 — 保留原异常，继续进入 fail-closed 路径
                pass
            if recoveries <= CONTROL_PLANE_MAX_RECOVERIES and now() < (cp.deadline_at or now()):
                kwargs["resume"] = True
                await asyncio.sleep(min(5.0, float(recoveries)))
                continue
            try:
                return await _deadline_closeout_async(
                    cp,
                    run_id=run_id,
                    reason="CONTROL_PLANE_FAILURE",
                )
            except Exception as close_exc:  # noqa: BLE001
                return await _mark_failed_closed_async(
                    cp,
                    run_id=run_id,
                    reason=f"CONTROL_PLANE_CLOSEOUT_FAILED: {type(close_exc).__name__}: {close_exc}",
                )


def _supervisor_lifecycle_heartbeat_worker(
    episode_id: str,
    run_id: str,
    stop: threading.Event,
    *,
    interval_s: float = LIFECYCLE_HEARTBEAT_INTERVAL_S,
) -> None:
    wait_s = max(
        0.01,
        min(float(interval_s), SUPERVISOR_HEARTBEAT_STALE_S / 3.0),
    )
    checkpoint = VideoSupervisorCheckpoint(
        episode_id=episode_id,
        run_id=run_id,
    )
    while not stop.wait(wait_s):
        try:
            if not _refresh_supervisor_heartbeat(
                checkpoint,
                run_id=run_id,
            ):
                return
        except Exception as exc:  # noqa: BLE001 - transient DB contention is retryable
            try:
                from app.errors import log_error

                log_error(
                    exc,
                    action="video_supervisor.lifecycle_heartbeat",
                    context={"episode_id": episode_id, "run_id": run_id},
                )
            except Exception:  # noqa: BLE001 - heartbeat retry must stay alive
                pass


async def run_video_completion_resilient(
    episode_id: str,
    **kwargs: Any,
) -> VideoSupervisorCheckpoint:
    """Run one completion epoch under an ownership-CAS lifecycle heartbeat."""
    heartbeat_interval_s = float(
        kwargs.pop(
            "_lifecycle_heartbeat_interval_s",
            LIFECYCLE_HEARTBEAT_INTERVAL_S,
        )
    )
    run_id = str(kwargs.get("run_id") or "").strip()
    if not run_id:
        return await _run_video_completion_resilient_loop(
            episode_id,
            **kwargs,
        )
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_supervisor_lifecycle_heartbeat_worker,
        args=(episode_id, run_id, stop),
        kwargs={"interval_s": heartbeat_interval_s},
        name=f"video-supervisor-heartbeat:{episode_id}",
        daemon=True,
    )
    heartbeat.start()
    try:
        return await _run_video_completion_resilient_loop(
            episode_id,
            **kwargs,
        )
    finally:
        stop.set()
        await asyncio.to_thread(heartbeat.join, 5.0)
