"""开机恢复：连播任务台的半边状态清理 + 队列重启。

两件事：
1. 把旧单例模型遗留的 ``workflow_runs(workflow_type='series_film', status IN
   (CREATED/RUNNING/PAUSED_EXTERNAL))`` 统一标为 ``CANCELLED``（不会再有任何
   代码去推进它们，永远挂着会误导观测台）。
2. 把因进程重启卡在 ``running`` 的 ``series_tasks`` 复位为 ``queued``（进度
   保留），再按项目重启队列 runner（项目本身处于暂停状态则不自动重启，尊重
   用户此前的暂停）。
"""
from __future__ import annotations

from app.db import get_conn
from app.orchestration.state_machine import transition_run

from . import queue, tasks

_LEGACY_STATUSES = ("CREATED", "RUNNING", "PAUSED_EXTERNAL")
_LEGACY_MESSAGE = "连播台已升级为连播任务台，历史单例运行记录不再使用，自动标记为已取消。"


def _cancel_legacy_series_film_runs(conn) -> int:
    rows = conn.execute(
        "SELECT id, status FROM workflow_runs WHERE workflow_type='series_film' AND status IN (?,?,?)",
        _LEGACY_STATUSES,
    ).fetchall()
    for row in rows:
        transition_run(
            row["id"], row["status"], "CANCELLED", _LEGACY_MESSAGE,
            failure_code="SERIES_TASK_MIGRATION", conn=conn,
        )
    if rows:
        conn.commit()
    return len(rows)


def recover_series_film_runs() -> int:
    conn = get_conn()
    _cancel_legacy_series_film_runs(conn)
    project_ids = tasks.reset_running_to_queued(conn)
    resumed = 0
    for project_id in project_ids:
        if queue.resume_after_recovery(project_id):
            resumed += 1
    return resumed
