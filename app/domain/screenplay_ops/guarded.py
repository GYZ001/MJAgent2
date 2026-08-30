"""剧本生成任务的批次台账刷新与「录制+守卫」包装（供 activation/batch/task_recovery 复用）。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 task_body 与 run_control，因此排在二者之后——
activation 的 _spawn_screenplay_activation 反过来依赖本模块，这是本文件唯一需要注意的方向。
"""
from __future__ import annotations

import asyncio

from app.db import (
    get_conn,
    now,
)
from app.evidence import repository as evidence_repository
from app.orchestration.engine import WorkflowRecorder
from app.orchestration.state_machine import StateConflict
from app.schemas import EpisodeScreenplay

from .run_control import _assert_screenplay_run_owner
from .task_body import _recorded_screenplay_task


def _refresh_screenplay_batch_run(batch_run_id: str) -> None:
    from app.orchestration.state_machine import StateConflict, transition_run

    conn = get_conn()
    parent = conn.execute(
        "SELECT status FROM workflow_runs WHERE id=? AND workflow_type='screenplay_batch'",
        (batch_run_id,),
    ).fetchone()
    if not parent or parent["status"] != "RUNNING":
        return
    rows = conn.execute(
        "SELECT status FROM workflow_runs WHERE parent_run_id=? "
        "AND workflow_type='screenplay'",
        (batch_run_id,),
    ).fetchall()
    if not rows:
        return
    statuses = [row["status"] for row in rows]
    terminal = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
    if any(status not in terminal for status in statuses):
        return
    failures = sum(status in {"PARTIAL", "FAILED", "CANCELLED"} for status in statuses)
    target = "SUCCEEDED" if failures == 0 else "PARTIAL"
    reason = (
        f"批量剧本全部完成，共 {len(statuses)} 集"
        if failures == 0
        else f"批量剧本已收口，共 {len(statuses)} 集，{failures} 集未成功"
    )
    try:
        transition_run(
            batch_run_id,
            "RUNNING",
            target,
            reason,
            failure_code="PARTIAL_RESULT" if failures else None, conn=None,
        )
    except StateConflict:
        return
    evidence_repository.append_event(
        batch_run_id,
        "BATCH_FINISHED",
        "info" if failures == 0 else "warning",
        reason,
        payload={"children": len(statuses), "unsuccessful": failures},
    )

async def _screenplay_guarded(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    priority: int = 0,
    batch_run_id: str | None = None,
):
    from app.generation_concurrency import run_with_generation_slot

    async def activate() -> EpisodeScreenplay | None:
        conn = get_conn()
        cursor = conn.execute(
            "UPDATE episodes SET screenplay_status='running',"
            "screenplay_error=COALESCE(screenplay_error, ?),screenplay_updated_at=? "
            "WHERE id=? AND active_screenplay_run_id=? "
            "AND screenplay_status IN ('queued','running','repairing')",
            ("正在生成人物上下文与首版剧本", now(), episode_id, recorder.run_id),
        )
        conn.commit()
        if cursor.rowcount != 1:
            _assert_screenplay_run_owner(episode_id, run_id=recorder.run_id)
            raise StateConflict(
                "screenplay_activation",
                episode_id,
                {"queued", "running", "repairing"},
                "episode_state_changed",
            )
        return await _recorded_screenplay_task(episode_id, recorder)

    try:
        await run_with_generation_slot(
            "screenplay",
            activate,
            priority=priority,
        )
    except asyncio.CancelledError:
        row = get_conn().execute(
            "SELECT status FROM workflow_runs WHERE id=?",
            (recorder.run_id,),
        ).fetchone()
        if row and row["status"] == "CREATED":
            try:
                recorder.cancel("排队中的剧本任务已取消", conn=None)
            except StateConflict:
                pass
        raise
    finally:
        if batch_run_id:
            _refresh_screenplay_batch_run(batch_run_id)
