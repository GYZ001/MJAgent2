"""开机恢复因服务重启而中断的连播台运行。

镜像 ``app.domain.video_ops.project_queue_run.recover_project_video_completion_queues``
的写法：只抢救「因进程重启而暂停」的运行（``failure_code='SERVICE_RESTART'``），
用户主动暂停（``failure_code='USER_PAUSED'``）的运行不在这里恢复，等用户自己
在连播台点「继续」。
"""
from __future__ import annotations

import json

from app import errors, task_registry
from app.db import get_conn, now
from app.orchestration.engine import WorkflowRecorder

from . import orchestrator, state


def recover_series_film_runs() -> int:
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM workflow_runs
           WHERE workflow_type=? AND status='PAUSED_EXTERNAL' AND failure_code='SERVICE_RESTART'
             AND recovered_by_run_id IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS：开机恢复扫描全部所有者的
               -- 中断连播台运行，排除软删除（回收站）项目，避免重新点燃残留任务
                WHERE p.id=workflow_runs.scope_id AND p.deleted_at IS NOT NULL
             )
           ORDER BY updated_at""",
        (state.WORKFLOW_TYPE,),
    ).fetchall()
    resumed = 0
    for row in rows:
        if _recover_one(conn, dict(row)):
            resumed += 1
    return resumed


def _recover_one(conn, row: dict) -> bool:
    project_id = row["scope_id"]
    if task_registry.active(state.TASK_KIND, project_id):
        return False
    recorder = None
    coro = None
    try:
        run_state = state.load_state(row)
        if not run_state:
            raise ValueError("连播台恢复参数不完整")
        recorder = WorkflowRecorder.create(
            workflow_type=state.WORKFLOW_TYPE,
            scope_type="project",
            scope_id=project_id,
            input_fingerprint=row["input_fingerprint"],
            requested_by="system",
            trigger_type="resume",
            policy_snapshot=json.loads(row["policy_snapshot_json"] or "{}"),
            config_snapshot={"series_state": run_state},
            parent_run_id=row["id"],
        )
        coro = orchestrator.run_series_film(project_id, run_state, recorder)
        task_registry.spawn(state.TASK_KIND, project_id, coro, project_id=project_id)
        return True
    except Exception as exc:  # noqa: BLE001
        if coro is not None:
            coro.close()
        if recorder is not None:
            try:
                recorder.cancel("连播台恢复任务未能启动", conn=None)
            except Exception:  # noqa: BLE001
                pass
        errors.record_and_format(
            exc, action="series_film_recovery",
            context={"project_id": project_id, "run_id": row["id"]},
        )
        conn.execute(
            """UPDATE workflow_runs
               SET status='FAILED', failure_code='RECOVERY_START_FAILED',
                   failure_message='连播台恢复任务未能启动，可重新提交', updated_at=?
               WHERE id=? AND status='PAUSED_EXTERNAL'""",
            (now(), row["id"]),
        )
        conn.commit()
        return False
