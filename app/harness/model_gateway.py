from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app import config, hiagent
from app.db import get_conn, now
from app.evidence import repository
from app.observability.tracing import current_trace
from app.orchestration.state_machine import transition_run

T = TypeVar("T")


class StructuredOutputError(ValueError):
    """Base class for content failures after a successful provider transport."""


class StructuredFormatError(StructuredOutputError):
    pass


class StructuredSemanticError(StructuredOutputError):
    pass


def _json_candidates(value: str) -> list[dict[str, Any]]:
    """Return complete JSON objects, preferring a valid trailing response.

    Providers occasionally prepend commentary or leave a truncated object
    before emitting a complete corrected object.  ``raw_decode`` lets us find
    complete suffixes without trying to manufacture missing semantic content.
    """
    text = str(value or "").strip()
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, dict[str, Any]]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, end = decoder.raw_decode(text[index:])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            candidates.append((index + end, payload))
    return [payload for _end, payload in sorted(candidates, key=lambda item: item[0], reverse=True)]


def _model_schema(model_type: Any) -> dict[str, Any]:
    schema = getattr(model_type, "model_json_schema", None)
    return schema() if callable(schema) else {"type": "object"}


def _coerce_structured(model_type: Any, payload: dict[str, Any]) -> Any:
    validator = getattr(model_type, "model_validate", None)
    if callable(validator):
        return validator(payload)
    if callable(model_type):
        return model_type(payload)
    return payload


def _validation_messages(value: Any) -> list[str]:
    if value in (None, True, []):
        return []
    if value is False:
        return ["业务校验未通过"]
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [
            str(getattr(item, "message", item))
            for item in value
            if item not in (None, "")
        ]
    return [str(value)]


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
            from app.generation_concurrency import run_with_provider_call_slot

            result = await run_with_provider_call_slot(
                lambda: hiagent.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    call_meta=meta,
                )
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


async def chat_structured(
    messages: list[dict[str, str]],
    *,
    model_type: type[T] | Callable[[dict[str, Any]], T],
    validate: Callable[[T], Any] | None,
    operation_id: str,
    max_tokens: int,
    format_retry_limit: int = 1,
    semantic_retry_limit: int = 1,
    temperature: float = 0.1,
    call_meta: dict[str, Any] | None = None,
    repair_context: str = "",
) -> T:
    """Run one typed model operation with separate format/semantic budgets.

    Transport retries remain owned by :func:`chat`.  A malformed HTTP-200 is
    never returned as success: it is recovered locally when a complete trailing
    object exists, otherwise only the schema and response tail are sent for one
    bounded format repair.  Business validation uses its own retry budget.
    """
    if not operation_id.strip():
        raise ValueError("structured operation_id is required")
    base_messages = [dict(message) for message in messages]
    current_messages = base_messages
    format_attempt = 0
    semantic_attempt = 0
    last_raw = ""
    local_recovery = False
    while True:
        meta = {
            **(call_meta or {}),
            "operation_id": operation_id,
            "expected_json": True,
            "format_attempt": format_attempt,
            "semantic_attempt": semantic_attempt,
            "local_recovery": local_recovery,
        }
        last_raw = await chat(
            current_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            call_meta=meta,
        )
        candidates = _json_candidates(last_raw)
        parsed: T | None = None
        parse_error: Exception | None = None
        for candidate_no, payload in enumerate(candidates):
            try:
                parsed = _coerce_structured(model_type, payload)
            except (TypeError, ValueError, ValidationError) as exc:
                parse_error = exc
                continue
            local_recovery = candidate_no > 0 or not last_raw.lstrip().startswith("{")
            break
        if parsed is None:
            if format_attempt >= max(0, int(format_retry_limit)):
                detail = str(parse_error or "找不到完整 JSON 对象")
                raise StructuredFormatError(
                    f"{operation_id} 结构化输出失败：{detail}"
                ) from parse_error
            format_attempt += 1
            tail = last_raw[-6000:]
            current_messages = [
                {
                    "role": "user",
                    "content": (
                        "只修复下面响应的 JSON 格式和 Schema，不改写其语义。"
                        "只输出一个完整 JSON 对象。\nSchema:\n"
                        + json.dumps(_model_schema(model_type), ensure_ascii=False)
                        + "\n坏响应尾部：\n"
                        + tail
                    ),
                }
            ]
            continue

        semantic_errors = _validation_messages(
            validate(parsed) if validate is not None else None
        )
        if not semantic_errors:
            return parsed
        if semantic_attempt >= max(0, int(semantic_retry_limit)):
            raise StructuredSemanticError(
                f"{operation_id} 业务校验失败：" + "；".join(semantic_errors[:10])
            )
        semantic_attempt += 1
        current_messages = [
            {
                "role": "user",
                "content": (
                    "只修复以下业务问题，保持其余已验证字段不变；只输出完整 JSON。\n"
                    "问题：\n- "
                    + "\n- ".join(semantic_errors[:20])
                    + ("\n最小修复上下文：\n" + repair_context if repair_context else "")
                    + "\n当前候选：\n"
                    + json.dumps(
                        parsed.model_dump(mode="json")
                        if isinstance(parsed, BaseModel) else parsed,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ),
            }
        ]
