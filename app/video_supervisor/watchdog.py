"""跨重启存量 Supervisor 的巡检恢复与看门狗循环。"""
from __future__ import annotations

import asyncio

from app.completion_grant import GrantValidationError
from app.db import get_conn, now

from .authority import _record_grant_validation_failure, _verify_supervisor_paid_authority
from .checkpoint import load_latest_checkpoint, save_checkpoint
from .closeout import _deadline_closeout
from .constants import SUPERVISOR_HEARTBEAT_STALE_S, TERMINAL_SUPERVISOR_PHASES
from .resilience import _mark_failed_closed



async def reconcile_stale_video_supervisors() -> int:
    """接管 heartbeat 超时但内存 task 仍占位的 Supervisor。"""
    from app import task_registry
    from app.errors import log_error
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.observability.metrics import inc

    conn = get_conn()
    rows = conn.execute(
        """SELECT e.id, e.project_id, e.storyboard_artifact_id, e.active_video_run_id,
                  r.updated_at AS run_updated_at
           FROM episodes e
           LEFT JOIN workflow_runs r ON r.id=e.active_video_run_id
           WHERE e.video_completion_mode='complete' AND e.active_video_run_id IS NOT NULL"""
    ).fetchall()
    recovered = 0

    async def reconcile_one(row) -> bool:
        episode_id = row["id"]
        cp = load_latest_checkpoint(episode_id)
        if cp is None or cp.phase in TERMINAL_SUPERVISOR_PHASES or cp.phase in {
            "PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_AUTHORIZATION", "WAITING_HUMAN",
        }:
            return False
        heartbeat = max(float(cp.last_heartbeat_at or 0), float(row["run_updated_at"] or 0))
        task_running = task_registry.active("video_completion", episode_id)
        # A checkpoint created before absolute deadlines existed is a legacy
        # incident.  Do not mutate it automatically: the repair-preview +
        # explicit confirmation path owns that migration.
        if not task_running and cp.deadline_at is None:
            return False
        if heartbeat and now() - heartbeat <= SUPERVISOR_HEARTBEAT_STALE_S:
            return False
        try:
            _verify_supervisor_paid_authority(cp, stage="watchdog_takeover")
        except GrantValidationError as exc:
            cp.phase = "WAITING_AUTHORIZATION"
            cp.outcome = exc.code
            save_checkpoint(cp, run_id=cp.run_id)
            _record_grant_validation_failure(
                cp, exc, run_id=cp.run_id, stage="watchdog_takeover",
            )
            return False
        current = conn.execute(
            """SELECT e.active_video_run_id, e.video_completion_mode,
                      r.updated_at AS run_updated_at
                 FROM episodes e
                 LEFT JOIN workflow_runs r ON r.id=e.active_video_run_id
                WHERE e.id=?""",
            (episode_id,),
        ).fetchone()
        latest_cp = load_latest_checkpoint(episode_id)
        current_heartbeat = max(
            float((latest_cp.last_heartbeat_at if latest_cp else 0) or 0),
            float((current["run_updated_at"] if current else 0) or 0),
        )
        if (
            current is None
            or current["video_completion_mode"] != "complete"
            or current["active_video_run_id"] != row["active_video_run_id"]
            or (
                current_heartbeat
                and now() - current_heartbeat <= SUPERVISOR_HEARTBEAT_STALE_S
            )
        ):
            return False
        task_running = task_registry.active("video_completion", episode_id)
        if task_running:
            await task_registry.cancel_and_wait("video_completion", episode_id)
        recorder = WorkflowRecorder.create(
            workflow_type="episode_video_completion",
            scope_type="episode",
            scope_id=episode_id,
            input_fingerprint=fingerprint(
                row["storyboard_artifact_id"], cp.grant_id, "watchdog_closeout",
            ),
            requested_by="system",
            trigger_type="watchdog",
            policy_snapshot={"supervisor": "video_completion", "watchdog_takeover": True},
            deadline_at=cp.deadline_at,
            parent_run_id=row["active_video_run_id"],
        )
        recorder.start()
        claimed = conn.execute(
            """UPDATE episodes SET active_video_run_id=?, status='generating'
               WHERE id=? AND active_video_run_id=?""",
            (recorder.run_id, episode_id, row["active_video_run_id"]),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            recorder.cancel("检测到更新的补齐运行，watchdog 放弃接管", conn=None)
            return False
        conn.commit()
        cp.run_id = recorder.run_id
        cp.phase = "RECOVERING_CONTROL_PLANE"
        cp.control_plane_recoveries += 1
        try:
            result = _deadline_closeout(
                cp,
                run_id=recorder.run_id,
                reason="SUPERVISOR_HEARTBEAT_STALE",
            )
            recorder.partial(result.outcome or result.phase, conn=None)
        except Exception as exc:  # noqa: BLE001
            # 必须在 _mark_failed_closed / recorder.fail 之前回滚，且回滚要放在这
            # 个 except 块的第一条语句：_deadline_closeout 内部与本函数共用同一
            # 个 task 缓存连接，它对每个镜头调用
            # app.evidence.media.select_best_video_candidate 采用最佳候选——那
            # 个函数先 UPDATE shots.adopted_version_id 与
            # shot_versions.adoption_reason，再调用
            # invalidate_episode_delivery_authority 写 delivery_packages，最后才
            # 一次性 conn.commit()；这几条语句之间没有中间提交点。如果
            # _deadline_closeout 在这个窗口内抛出异常，这份半途的采用写入就会挂
            # 在 conn 上。_mark_failed_closed 内部会调用 save_checkpoint，
            # 它的写入逻辑是「如果 conn 已经在事务中就不再开新事务，直接复用」
            # （见 app/video_supervisor/checkpoint.py::save_checkpoint），所以它的
            # conn.commit() 会把上面挂起的半途采用一并提交下去；recorder.fail()
            # 的 refresh_cost()/transition_run() 同理。回滚只丢弃这次失败尝试自
            # 己产生的未提交写入，不影响 _deadline_closeout 已经在别处提交过的
            # 状态（例如它自己那次 conn.commit() 已经落盘的镜头）。
            if conn.in_transaction:
                conn.rollback()
            _mark_failed_closed(
                cp,
                run_id=recorder.run_id,
                reason=f"WATCHDOG_CLOSEOUT_FAILED: {type(exc).__name__}: {exc}",
            )
            recorder.fail(exc, conn=None)
        inc("video_supervisor_watchdog_takeover_total", episode_id=episode_id)
        return True

    for row in rows:
        episode_id = row["id"]
        try:
            if await reconcile_one(row):
                recovered += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            log_error(
                exc,
                action="video_supervisor.watchdog_episode",
                context={
                    "episode_id": episode_id,
                    "active_video_run_id": row["active_video_run_id"],
                },
                meta={"stage": "video_supervisor_watchdog", "isolation": "episode"},
            )
            inc(
                "video_supervisor_watchdog_episode_error_total",
                episode_id=episode_id,
                error_type=type(exc).__name__,
            )
    return recovered


async def video_supervisor_watchdog_loop(interval_s: float = 30.0) -> None:
    while True:
        try:
            # 轻量级业务巡检始终运行，不要求用户显式开启全片补齐授权。
            # 它只恢复已请求任务或降级孤儿连续性，不会自行创建新的付费范围。
            from app import worker
            await asyncio.to_thread(worker.reconcile_stalled_video_jobs)
            await reconcile_stale_video_supervisors()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — watchdog 自身不得因单集坏数据退出
            from app.errors import log_error
            from app.observability.metrics import inc
            log_error(
                exc,
                action="video_supervisor.watchdog_loop",
                context={"interval_s": interval_s},
                meta={"stage": "video_supervisor_watchdog", "isolation": "loop"},
            )
            inc(
                "video_supervisor_watchdog_loop_error_total",
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(max(5.0, min(float(interval_s), 60.0)))
