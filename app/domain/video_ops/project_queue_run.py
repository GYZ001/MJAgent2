"""整项目视频补齐队列的单次运行与开机恢复。

从 app/domain/video_ops.py 按原样搬移；依赖 completion_core 与 project_queue_core。
"""
from __future__ import annotations

import json

from app import (
    errors,
    task_registry,
)
from app.db import (
    get_conn,
    now,
)

from .completion_core import _complete_episode_core
from .project_queue_core import (
    _finish_project_video_completion_queue,
    _persist_project_video_queue,
    _project_video_queue_pause_requests,
    _project_video_spent,
    _propagate_project_video_child_status,
)


async def _run_project_video_completion_queue(
    project_id: str,
    state: dict,
    recorder,
) -> None:
    import asyncio

    plan = state.get("plan") or []
    recorder.start()
    _persist_project_video_queue(recorder.run_id, state)
    try:
        for item in plan:
            item_status = item.get("status")
            if item_status not in {
                "queued", "started", "waiting", "already_running",
                "failed_to_schedule",
            }:
                continue
            episode_id = item["episode_id"]
            if item_status == "already_running" and not item.get("run_id"):
                active_run = get_conn().execute(
                    "SELECT active_video_run_id FROM episodes WHERE id=?",
                    (episode_id,),
                ).fetchone()
                if active_run:
                    item["run_id"] = active_run["active_video_run_id"]
            # A recovered per-episode Supervisor always owns the episode first.
            while any(
                task_registry.active("video_completion", candidate["episode_id"])
                for candidate in plan
                if candidate.get("episode_id")
            ):
                await asyncio.sleep(5)
            if item_status in {"started", "waiting", "already_running"}:
                _propagate_project_video_child_status(item)
                _persist_project_video_queue(recorder.run_id, state)
                continue
            try:
                from app.video_supervisor import rebuild_coverage_ledger

                ledger = rebuild_coverage_ledger(episode_id)
                if ledger.covered_within_quota():
                    item["status"] = "success"
                    _persist_project_video_queue(recorder.run_id, state)
                    continue
            except Exception:  # noqa: BLE001
                pass
            room_now = max(
                0.0,
                float(state["global_budget_cap_cny"]) - _project_video_spent(project_id),
            )
            if room_now < 5:
                item["status"] = "skipped_budget"
                item["allocated_cny"] = 0
                _persist_project_video_queue(recorder.run_id, state)
                continue
            item["allocated_cny"] = min(float(item["allocated_cny"]), room_now)
            try:
                result = await _complete_episode_core(episode_id, {
                    "mode": "fresh",
                    "budget_cap_cny": item["allocated_cny"],
                    "wall_clock_cap_s": state["wall_clock_cap_s"],
                    "allow_fallback_adopt": state["allow_fallback_adopt"],
                    "allow_storyboard_edit": state["allow_storyboard_edit"],
                    "idempotency_key": (
                        f"{state['idempotency_key']}:episode:{episode_id}"
                        if state.get("idempotency_key")
                        else None
                    ),
                })
                item["status"] = "started"
                item["run_id"] = result.get("run_id")
                item["completion_grant_id"] = result.get("completion_grant_id")
                _persist_project_video_queue(recorder.run_id, state)
                while task_registry.active("video_completion", episode_id):
                    await asyncio.sleep(8)
                _propagate_project_video_child_status(item)
            except Exception as exc:  # noqa: BLE001
                item["status"] = "failed"
                item["error"] = str(exc)[:500]
            _persist_project_video_queue(recorder.run_id, state)
        _finish_project_video_completion_queue(plan, recorder)
    except asyncio.CancelledError:
        _persist_project_video_queue(recorder.run_id, state)
        pause_requested = project_id in _project_video_queue_pause_requests
        _project_video_queue_pause_requests.discard(project_id)
        if task_registry.shutdown_in_progress() or pause_requested:
            recorder.pause_external(
                "用户暂停，项目补齐剩余队列已保留"
                if pause_requested else "服务重启，项目补齐剩余队列等待自动恢复", conn=None
            )
            if pause_requested:
                conn = get_conn()
                conn.execute(
                    "UPDATE workflow_runs SET failure_code='USER_PAUSED' WHERE id=?",
                    (recorder.run_id,),
                )
                conn.commit()
        else:
            recorder.cancel("项目补齐队列已取消", conn=None)
        raise
    except Exception as exc:
        _persist_project_video_queue(recorder.run_id, state)
        recorder.fail(exc, conn=None)
        raise

def recover_project_video_completion_queues() -> int:
    from app.orchestration.engine import WorkflowRecorder

    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM workflow_runs
           WHERE workflow_type='project_video_completion_queue'
             AND status='PAUSED_EXTERNAL' AND failure_code='SERVICE_RESTART'
             AND recovered_by_run_id IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS: startup recovery scans
               -- every owner's paused project-wide video completion queues
               -- after a process reload/restart; excludes soft-deleted
               -- (recycle-bin) projects so their residual video generation
               -- is not re-armed and does not burn quota
                WHERE p.id=workflow_runs.scope_id AND p.deleted_at IS NOT NULL
             )
           ORDER BY updated_at"""
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["scope_id"]
        if task_registry.active("video_completion_project", project_id):
            continue
        recorder = None
        coro = None
        try:
            snapshot = json.loads(row["config_snapshot_json"] or "{}")
            state = snapshot["queue_state"]
            if not isinstance(state, dict) or not isinstance(state.get("plan"), list):
                raise ValueError("项目补齐恢复参数不完整")
            recorder = WorkflowRecorder.create(
                workflow_type="project_video_completion_queue",
                scope_type="project",
                scope_id=project_id,
                input_fingerprint=row["input_fingerprint"],
                requested_by="system",
                trigger_type="resume",
                policy_snapshot=json.loads(row["policy_snapshot_json"] or "{}"),
                config_snapshot={"queue_state": state},
                budget_limit_cny=row["budget_limit_cny"],
                parent_run_id=row["id"],
            )
            coro = _run_project_video_completion_queue(project_id, state, recorder)
            task_registry.spawn(
                "video_completion_project",
                project_id,
                coro,
                project_id=project_id,
            )
            resumed += 1
        except Exception as exc:  # noqa: BLE001
            if coro is not None:
                coro.close()
            if recorder is not None:
                try:
                    recorder.cancel("项目补齐队列恢复任务未能启动", conn=None)
                except Exception:  # noqa: BLE001
                    pass
            errors.record_and_format(
                exc,
                action="project_video_completion_recovery",
                context={"project_id": project_id, "run_id": row["id"]},
            )
            conn.execute(
                """UPDATE workflow_runs
                   SET status='FAILED',failure_code='RECOVERY_START_FAILED',
                       failure_message='项目补齐队列恢复任务未能启动，可重新提交',updated_at=?
                   WHERE id=? AND status='PAUSED_EXTERNAL'""",
                (now(), row["id"]),
            )
            conn.commit()
            continue
    return resumed
