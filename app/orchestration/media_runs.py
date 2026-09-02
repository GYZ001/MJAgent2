from __future__ import annotations

import logging

from app.db import get_conn
from app.evidence import repository
from app.observability.tracing import current_trace
from app.orchestration.engine import WorkflowRecorder, fingerprint, refresh_run_cost
from app.orchestration.state_machine import transition_run, transition_step

logger = logging.getLogger(__name__)

MEDIA_WORKFLOWS = {"video_generation", "scene_generation"}


def ensure_media_trace(
    *, workflow_type: str, scope_id: str, input_value: object
) -> tuple[str | None, str | None]:
    """Create a durable Run/Step for a directly queued media task.

    When called inside another workflow step, the existing trace is preserved so
    the job remains attributable to its parent operation.

    历史列，退场后无写入者：这里曾接一个 ``budget_limit_cny`` 形参转手给
    ``WorkflowRecorder.create()``。视频生成路径的唯一上游
    ``episode_video_budget_limit()`` 恒返回 ``math.inf`` 哨兵（成本预算体系
    退场后的遗留兼容签名），写进 ``workflow_runs.budget_limit_cny`` 这个
    REAL 列后，任何原样吐出该行的接口 ``json.dumps`` 都会因不接受 ``inf``
    而 500（含 ``GET /api/system/jobs``）。2026-09-02 把
    ``episode_video_budget_limit()`` 与这里的形参一并删除：本函数不再传这
    个值，新建的 video_generation/scene_generation 运行落到
    ``WorkflowRecorder.create()`` 自身默认值 None，列值恒为 NULL。这个列和
    ``WorkflowRecorder.create()``/``evidence.repository.create_run()`` 的
    同名形参本身**没有**退场——``app.domain.bible_ops.view_redo``
    （人物/场景改版的支付额度校验，另一套业务）仍在给别的 workflow_type
    传有限值，本函数只是不再是它众多写入者之一。
    """
    trace = current_trace()
    if trace.run_id:
        return trace.run_id, trace.step_run_id
    try:
        recorder = WorkflowRecorder.create(
            workflow_type=workflow_type,
            scope_type="shot",
            scope_id=scope_id,
            input_fingerprint=fingerprint(input_value),
        )
        recorder.start()
        step_id = repository.create_step(
            recorder.run_id,
            workflow_type,
            agent_name=workflow_type,
            context_manifest={"scope_id": scope_id},
        )
        transition_step(step_id, "PENDING", "READY", "媒体任务已持久化", conn=None)
        transition_step(step_id, "READY", "RUNNING", "媒体任务已入队", conn=None)
        repository.append_event(
            recorder.run_id, "MEDIA_QUEUED", "info", "媒体任务已入队", step_run_id=step_id
        )
        return recorder.run_id, step_id
    except Exception:
        # Minimal unit-test databases may not contain orchestration tables.  The
        # production schema always does; queue correctness must not depend on UI telemetry.
        logger.exception(
            "ensure_media_trace failed workflow_type=%s scope_id=%s",
            workflow_type,
            scope_id,
        )
        return None, None


def mark_media_job_state(run_id: str | None, step_id: str | None, status: str, message: str | None = None) -> None:
    if not run_id or not step_id:
        return
    try:
        conn = get_conn()
        run = conn.execute("SELECT workflow_type, status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        step = conn.execute("SELECT status FROM step_runs WHERE id=?", (step_id,)).fetchone()
        if not run or run["workflow_type"] not in MEDIA_WORKFLOWS or not step:
            return
        reason = (message or status)[:1000]
        if status == "running":
            if run["status"] in {"WAITING_RETRY", "PAUSED_EXTERNAL"}:
                transition_run(run_id, run["status"], "RUNNING", "媒体任务继续执行", conn=None)
            if step["status"] == "READY":
                transition_step(step_id, "READY", "RUNNING", "恢复后的媒体任务开始执行", conn=None)
            return
        if status == "queued":
            if run["status"] == "RUNNING":
                transition_run(run_id, "RUNNING", "WAITING_RETRY", reason, conn=None)
            return
        if status == "paused":
            if run["status"] in {"RUNNING", "WAITING_RETRY"}:
                transition_run(run_id, run["status"], "PAUSED_EXTERNAL", reason, conn=None)
            repository.append_event(
                run_id, "MEDIA_PAUSED", "info", reason, step_run_id=step_id,
            )
            return
        if status == "waiting_human":
            if run["status"] in {
                "RUNNING", "WAITING_RETRY", "PAUSED_EXTERNAL",
            }:
                transition_run(
                    run_id,
                    run["status"],
                    "WAITING_HUMAN",
                    reason, conn=None,
                )
            repository.append_event(
                run_id,
                "MEDIA_WAITING_HUMAN",
                "info",
                reason,
                step_run_id=step_id,
            )
            return
        if status == "succeeded":
            if step["status"] == "RUNNING":
                transition_step(step_id, "RUNNING", "SUCCEEDED", reason, decision="accept", conn=None)
            current = conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()["status"]
            if current == "RUNNING":
                transition_run(run_id, "RUNNING", "SUCCEEDED", reason, conn=None)
        elif status in {"cancelled", "abandoned"}:
            if step["status"] == "RUNNING":
                transition_step(step_id, "RUNNING", "CANCELLED", reason, decision="cancel", conn=None)
            current = conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()["status"]
            if current in {"RUNNING", "WAITING_RETRY", "PAUSED_EXTERNAL"}:
                transition_run(run_id, current, "CANCELLED", reason, conn=None)
        elif status == "failed":
            if step["status"] == "RUNNING":
                transition_step(step_id, "RUNNING", "FAILED", reason, decision="escalate", error_code="MEDIA_FAILED", conn=None)
            current = conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()["status"]
            if current in {"RUNNING", "WAITING_RETRY", "PAUSED_EXTERNAL"}:
                transition_run(run_id, current, "FAILED", reason, failure_code="MEDIA_FAILED", conn=None)
        if status in {"succeeded", "cancelled", "abandoned", "failed"}:
            # 这条运行的整个生命周期都走这个函数而不是 WorkflowRecorder 实例方法，
            # 所以 refresh_cost 必须在这里手动补一次，否则 shot_versions.cost_cny
            # 已经记着 ¥12/段，workflow_runs.cost_cny 却永远停在 0。
            refresh_run_cost(run_id)
        repository.append_event(
            run_id, f"MEDIA_{status.upper()}", "error" if status == "failed" else "info",
            reason, step_run_id=step_id,
        )
    except Exception:
        # State transitions are CAS-protected. Another worker may have finalized it.
        logger.exception(
            "mark_media_job_state failed run_id=%s step_id=%s status=%s",
            run_id,
            step_id,
            status,
        )
        return
