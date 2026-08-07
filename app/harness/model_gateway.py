from __future__ import annotations

import asyncio
import json
from typing import Any

from app import config, hiagent
from app.db import get_conn, now
from app.evidence import repository
from app.observability.tracing import current_trace
from app.orchestration.state_machine import transition_run


def _is_non_candidate_json_response(value: str) -> bool:
    """Detect transport-success responses that contain no task JSON candidate."""
    text = str(value or "").strip()
    if "{" not in text:
        return True
    candidate = text[text.find("{"):].strip()
    if candidate.endswith("```"):
        candidate = candidate[:-3].rstrip()
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    error_keys = {"code", "message", "error", "detail", "status"}
    return (
        isinstance(code, int)
        and code >= 400
        and bool(payload.get("message") or payload.get("error") or payload.get("detail"))
        and set(payload).issubset(error_keys)
    )


def _retry_can_pause_run(run_id: str, step_run_id: str | None, stage_key: str | None) -> bool:
    """Only pause a run when the traced step exclusively owns this stage.

    Dedicated storyboard/screenplay runs have a step key matching the model
    stage and can safely expose ``WAITING_RETRY``.
    """
    if not step_run_id or not stage_key:
        return False
    row = get_conn().execute(
        """SELECT wr.status AS run_status, sr.step_key
           FROM workflow_runs wr
           JOIN step_runs sr ON sr.run_id=wr.id
           WHERE wr.id=? AND sr.id=?""",
        (run_id, step_run_id),
    ).fetchone()
    return bool(
        row
        and row["run_status"] == "RUNNING"
        and row["step_key"] == stage_key
    )


def _append_retry_event(
    event_type: str,
    message: str,
    *,
    retry_no: int,
    max_retries: int,
    delay: float,
    call_meta: dict[str, Any],
) -> None:
    trace = current_trace()
    if not trace.run_id:
        return
    repository.append_event(
        trace.run_id,
        event_type,
        "warning" if event_type == "PROVIDER_RETRY_SCHEDULED" else "info",
        message,
        step_run_id=trace.step_run_id,
        trace_id=trace.trace_id,
        payload={
            "retry_no": retry_no,
            "max_retries": max_retries,
            "delay_s": delay,
            "next_retry_at": now() + delay if event_type == "PROVIDER_RETRY_SCHEDULED" else None,
            "stage_key": call_meta.get("stage_key"),
            "call_role": call_meta.get("call_role"),
        },
    )


async def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 8192,
    call_meta: dict[str, Any] | None = None,
) -> str:
    """The only text-model entry point for business stages.

    It enforces trace metadata at the harness boundary while retaining the
    provider adapter's retry, redaction and lifecycle recording.
    """
    trace = current_trace()
    meta = {
        "gateway": "execution_harness",
        "run_id": trace.run_id,
        "step_run_id": trace.step_run_id,
        "trace_id": trace.trace_id,
        **(call_meta or {}),
    }
    max_retries = config.TEXT_PROVIDER_MAX_RETRIES
    stage_key = str(meta.get("stage_key") or "") or None
    for failure_no in range(max_retries + 1):
        try:
            result = await hiagent.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                call_meta=meta,
            )
            if meta.get("expected_json") and _is_non_candidate_json_response(result):
                raise hiagent.ProviderError(
                    "文本模型未返回任务 JSON 候选",
                    retryable=True,
                    raw=result,
                )
            return result
        except hiagent.ProviderError as exc:
            if not exc.retryable or failure_no >= max_retries:
                raise

            retry_no = failure_no + 1
            delay = config.TEXT_PROVIDER_RETRY_BASE_DELAY * (2 ** failure_no)
            message = (
                f"文本模型临时限流/故障，约 {int(delay)} 秒后自动执行"
                f"第 {retry_no}/{max_retries} 次重试"
            )
            trace = current_trace()
            paused = bool(
                trace.run_id
                and _retry_can_pause_run(trace.run_id, trace.step_run_id, stage_key)
            )
            if paused:
                transition_run(trace.run_id, "RUNNING", "WAITING_RETRY", message)
            _append_retry_event(
                "PROVIDER_RETRY_SCHEDULED",
                message,
                retry_no=retry_no,
                max_retries=max_retries,
                delay=delay,
                call_meta=meta,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                # The recorder owns cancellation and can legally cancel a run
                # from WAITING_RETRY. Do not move it back to RUNNING here.
                raise
            if paused:
                transition_run(trace.run_id, "WAITING_RETRY", "RUNNING", "重试冷却结束，恢复执行")
            _append_retry_event(
                "PROVIDER_RETRY_RESUMED",
                "重试冷却结束，已恢复同一文本模型请求",
                retry_no=retry_no,
                max_retries=max_retries,
                delay=0.0,
                call_meta=meta,
            )

    raise AssertionError("unreachable text provider retry state")
