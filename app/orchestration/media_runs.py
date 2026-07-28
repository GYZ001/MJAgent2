from __future__ import annotations

import logging

from app.db import get_conn
from app.evidence import repository
from app.observability.tracing import current_trace
from app.orchestration.engine import WorkflowRecorder, fingerprint
from app.orchestration.state_machine import transition_run, transition_step

logger = logging.getLogger(__name__)

MEDIA_WORKFLOWS = {"video_generation", "scene_generation"}


def ensure_media_trace(
    *, workflow_type: str, scope_id: str, input_value: object, budget_limit_cny: float | None
) -> tuple[str | None, str | None]:
    """Create a durable Run/Step for a directly queued media task.

    When called inside another workflow step, the existing trace is preserved so
    the job remains attributable to its parent operation.
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
            budget_limit_cny=budget_limit_cny,
        )
        recorder.start()
        step_id = repository.create_step(
            recorder.run_id,
            workflow_type,
            agent_name=workflow_type,
            context_manifest={"scope_id": scope_id},
        )
        transition_step(step_id, "PENDING", "READY", "媒体任务已持久化")
        transition_step(step_id, "READY", "RUNNING", "媒体任务已入队")
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
            if run["status"] in {"WAITING_RETRY", "PAUSED_BUDGET", "PAUSED_EXTERNAL"}:
                transition_run(run_id, run["status"], "RUNNING", "媒体任务继续执行")
            if step["status"] == "READY":
                transition_step(step_id, "READY", "RUNNING", "恢复后的媒体任务开始执行")
            return
        if status == "queued":
            if run["status"] == "RUNNING":
                transition_run(run_id, "RUNNING", "WAITING_RETRY", reason)
            return
        if status == "paused_budget":
            if run["status"] == "RUNNING":
                transition_run(run_id, "RUNNING", "PAUSED_BUDGET", reason)
            return
        if status == "paused":
            if run["status"] in {"RUNNING", "WAITING_RETRY"}:
                transition_run(run_id, run["status"], "PAUSED_EXTERNAL", reason)
            repository.append_event(
                run_id, "MEDIA_PAUSED", "info", reason, step_run_id=step_id,
            )
            return
        if status == "succeeded":
            if step["status"] == "RUNNING":
                transition_step(step_id, "RUNNING", "SUCCEEDED", reason, decision="accept")
            current = conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()["status"]
            if current == "RUNNING":
                transition_run(run_id, "RUNNING", "SUCCEEDED", reason)
        elif status in {"cancelled", "abandoned"}:
            if step["status"] == "RUNNING":
                transition_step(step_id, "RUNNING", "CANCELLED", reason, decision="cancel")
            current = conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()["status"]
            if current in {"RUNNING", "WAITING_RETRY", "PAUSED_BUDGET", "PAUSED_EXTERNAL"}:
                transition_run(run_id, current, "CANCELLED", reason)
        elif status == "failed":
            if step["status"] == "RUNNING":
                transition_step(step_id, "RUNNING", "FAILED", reason, decision="escalate", error_code="MEDIA_FAILED")
            current = conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()["status"]
            if current in {"RUNNING", "WAITING_RETRY", "PAUSED_BUDGET", "PAUSED_EXTERNAL"}:
                transition_run(run_id, current, "FAILED", reason, failure_code="MEDIA_FAILED")
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
