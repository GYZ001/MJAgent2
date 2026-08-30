"""整项目批量发起/取消剧本生成的编排入口。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 task_body/run_control/activation/guarded。
"""
from __future__ import annotations

import asyncio

from app import (
    errors,
    task_registry,
)
from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _episode_or_404,
    _project_or_404,
    _require_harness_engine,
    router,
)
from app.harness.contracts import get_contract
from app.orchestration.engine import (
    WorkflowRecorder,
    fingerprint,
)
from app.orchestration.state_machine import StateConflict
from fastapi import HTTPException

from .activation import _spawn_screenplay_activation
from .guarded import _screenplay_guarded
from .run_control import (
    _cancel_persisted_screenplay_run,
    _screenplay_fallback_status,
    _screenplay_task_active,
)
from .task_body import _new_screenplay_recorder


@router.post("/projects/{project_id}/screenplay-all")
async def start_screenplay_all(project_id: str):
    from app.generation_concurrency import PRIORITY_BATCH
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.generate_batch", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,)).fetchall()
    selected = [
        r for r in rows
        if (
            r["screenplay_status"] in ("pending", "failed", "repairing")
            or (
                not r["screenplay_json"]
                and r["screenplay_status"] not in {"queued", "running"}
            )
            or (
                r["screenplay_status"] in {"queued", "running"}
                and not _screenplay_task_active(r["id"])
            )
        )
        and r["screenplay_status"] != "ready"
    ]
    if not selected:
        raise HTTPException(409, "没有待生成剧本的剧集")
    selected_ids = [row["id"] for row in selected]
    batch_recorder = WorkflowRecorder.create(
        workflow_type="screenplay_batch",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(
            project_id,
            selected_ids,
            get_contract("screenplay").version,
        ),
        requested_by="user",
        trigger_type="batch",
        policy_snapshot={
            "contract": f"screenplay@{get_contract('screenplay').version}",
            "selected_episode_ids": selected_ids,
            "queue_policy": "priority_fifo",
            "cancel_policy": "cancel_all_then_wait",
        },
    )
    batch_recorder.start()
    run_ids: list[str] = []
    failed_to_start: list[dict] = []
    for row in selected:
        eid = row["id"]
        if row["screenplay_status"] in {"queued", "running"} and not _screenplay_task_active(eid):
            conn.execute(
                "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, "
                "active_screenplay_run_id=NULL, screenplay_updated_at=? WHERE id=?",
                ("检测到上次剧本任务已中断，本次将从安全状态重新启动", now(), eid),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        recorder = None
        from app.production.revision import (
            resolve_screenplay_resume_eligibility,
        )

        eligibility = resolve_screenplay_resume_eligibility(eid)
        is_resume = eligibility.resumable
        try:
            recorder = _new_screenplay_recorder(
                eid,
                trigger_type="batch_resume" if is_resume else "batch",
                parent_run_id=batch_recorder.run_id,
            )
            _spawn_screenplay_activation(
                eid,
                recorder,
                project_id=project_id,
                status="queued",
                message=(
                    f"批量任务：{eligibility.label}已排队"
                    if is_resume
                    else "批量剧本已排队，等待文本生成槽位"
                ),
                expected_active_run_id=(
                    row["active_screenplay_run_id"]
                    if is_resume else None
                ),
                clear_unpublished_ir=not is_resume,
                resume_eligibility=eligibility if is_resume else None,
                task_factory=lambda eid=eid, recorder=recorder: _screenplay_guarded(
                    eid,
                    recorder,
                    priority=PRIORITY_BATCH,
                    batch_run_id=batch_recorder.run_id,
                ),
            )
            run_ids.append(recorder.run_id)
        except Exception as exc:  # noqa: BLE001 - one episode must not strand the batch
            public = errors.record_and_format(
                exc,
                action="screenplay_batch_spawn",
                context={"project_id": project_id, "episode_id": eid},
            )
            failed_to_start.append({
                "episode_id": eid,
                "error": public,
                "retryable": True,
            })
    if not run_ids:
        batch_recorder.fail(RuntimeError("批量剧本任务均未能进入持久化队列"), conn=None)
        raise HTTPException(503, {
            "code": "SCREENPLAY_BATCH_START_FAILED",
            "message": "批量剧本任务均未能启动，各集原状态已保留，可直接重试",
            "failed_to_start": failed_to_start,
        })
    return {
        "started": len(run_ids),
        "batch_run_id": batch_recorder.run_id,
        "run_ids": run_ids,
        "failed_to_start": failed_to_start,
        "retryable_failures": len(failed_to_start),
    }

@router.post("/projects/{project_id}/screenplay-all/cancel")
async def cancel_screenplay_all(project_id: str):
    """停止本项目所有正在进行的剧本生成：取消在跑任务，未开跑的回退状态。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.cancel", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, screenplay_json, active_screenplay_run_id FROM episodes "
        "WHERE project_id=? AND screenplay_status IN ('queued','running','repairing')",
        (project_id,)).fetchall()
    episode_ids = [row["id"] for row in rows]
    batch_rows = conn.execute(
        "SELECT id FROM workflow_runs WHERE workflow_type='screenplay_batch' "
        "AND scope_type='project' AND scope_id=? AND status='RUNNING'",
        (project_id,),
    ).fetchall()
    cancelled_batches = 0
    for batch in batch_rows:
        try:
            WorkflowRecorder(batch["id"]).cancel("用户停止批量剧本", conn=None)
            cancelled_batches += 1
        except StateConflict:
            continue
    local_stopped = await task_registry.cancel_many_and_wait(
        "screenplay",
        episode_ids,
    )
    persisted_stopped = 0
    released = 0
    for r in rows:
        eid = r["id"]
        run_id = r["active_screenplay_run_id"]
        try:
            persisted_stopped += int(_cancel_persisted_screenplay_run(
                eid,
                run_id,
                message="用户停止批量剧本任务",
            ))
        except StateConflict:
            continue
        full = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        fallback = _screenplay_fallback_status(full)
        if run_id:
            cursor = conn.execute(
                "UPDATE episodes SET screenplay_status=?,screenplay_error=NULL,"
                "active_screenplay_run_id=NULL,screenplay_updated_at=? "
                "WHERE id=? AND active_screenplay_run_id=?",
                (fallback, now(), eid, run_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE episodes SET screenplay_status=?,screenplay_error=NULL,"
                "active_screenplay_run_id=NULL,screenplay_updated_at=? "
                "WHERE id=? AND active_screenplay_run_id IS NULL",
                (fallback, now(), eid),
            )
        released += int(cursor.rowcount == 1)
    conn.commit()
    return {
        "stopped": released,
        "local_stopped": local_stopped,
        "persisted_stopped": persisted_stopped,
        "matched": len(rows),
        "cancelled_batches": cancelled_batches,
    }

@router.post("/episodes/{episode_id}/screenplay/cancel")
async def cancel_screenplay(episode_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.cancel", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = dict(_episode_or_404(episode_id))
    if str(ep.get("screenplay_error") or "").startswith("CANCELLING:"):
        if not _screenplay_task_active(episode_id):
            fallback = _screenplay_fallback_status(ep)
            conn = get_conn()
            conn.execute(
                "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=?, "
                "active_screenplay_run_id=NULL, screenplay_snapshot_version=screenplay_snapshot_version+1 "
                "WHERE id=?",
                (fallback, now(), episode_id),
            )
            conn.commit()
            return {
                "status": fallback,
                "run_id": ep.get("active_screenplay_run_id"),
                "requested_at": ep.get("screenplay_updated_at"),
                "finished_at": now(),
                "retained_working_copy": bool(ep.get("screenplay_json")),
                "resume_available": fallback == "repairing",
                "recovered_stale_cancellation": True,
            }
        return {
            "status": "cancelling",
            "run_id": ep.get("active_screenplay_run_id"),
            "requested_at": ep.get("screenplay_updated_at"),
            "deduplicated": True,
        }
    if ep["screenplay_status"] not in {"queued", "running", "repairing"} or not _screenplay_task_active(episode_id):
        raise HTTPException(409, "当前没有正在进行的剧本任务")
    conn = get_conn()
    requested_at = now()
    run_id = ep.get("active_screenplay_run_id")
    conn.execute(
        "UPDATE episodes SET screenplay_error=?, screenplay_updated_at=?, "
        "screenplay_snapshot_version=screenplay_snapshot_version+1 WHERE id=?",
        (f"CANCELLING: 正在取消运行 {run_id or '未知'}", requested_at, episode_id),
    )
    conn.commit()
    try:
        cancelled_locally = await asyncio.wait_for(
            task_registry.cancel_and_wait("screenplay", episode_id), timeout=15
        )
    except asyncio.TimeoutError:
        return {
            "status": "cancelling",
            "run_id": run_id,
            "requested_at": requested_at,
            "message": "worker 尚未返回终态，系统将继续观察，未宣称已停止",
        }
    if not cancelled_locally and run_id:
        # The owner may live in another service process.  Persist cancellation
        # first; clearing the episode lease below then fences that remote worker
        # from every screenplay/revision/shard write guarded by run ownership.
        try:
            _cancel_persisted_screenplay_run(
                episode_id,
                run_id,
                message="用户从其他服务实例取消剧本任务",
            )
        except StateConflict:
            raise HTTPException(409, "剧本运行状态已变化，请刷新后重试") from None
    latest = dict(_episode_or_404(episode_id))
    fallback = _screenplay_fallback_status(latest)
    retained = bool(
        latest.get("screenplay_json")
        or latest.get("working_screenplay_artifact_id")
    )
    cursor = conn.execute(
        "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=?, "
        "active_screenplay_run_id=NULL, screenplay_snapshot_version=screenplay_snapshot_version+1 "
        "WHERE id=? AND (active_screenplay_run_id=? OR active_screenplay_run_id IS NULL)",
        (fallback, now(), episode_id, run_id))
    if cursor.rowcount != 1:
        raise HTTPException(409, "剧本任务已被新的运行接管，未覆盖其状态")
    conn.commit()
    return {
        "status": fallback,
        "run_id": run_id,
        "requested_at": requested_at,
        "finished_at": now(),
        "retained_working_copy": retained,
        "resume_available": fallback == "repairing",
    }
