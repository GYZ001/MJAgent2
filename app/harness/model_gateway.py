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


class StructuredProviderRejection(StructuredOutputError):
    """The provider returned an explicit error envelope instead of model output."""


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


def _append_interrupted_event(
    exc: hiagent.ProviderError,
    call_meta: dict[str, Any],
) -> None:
    trace = current_trace()
    if not trace.run_id:
        return
    repository.append_event(
        trace.run_id,
        "PROVIDER_RESULT_INTERRUPTED",
        "error",
        "文本模型请求已发送但结果不确定，已停止自动重试；请在页面确认后重试",
        step_run_id=trace.step_run_id,
        trace_id=trace.trace_id,
        payload={
            "delivery_state": exc.delivery_state,
            "failure_kind": exc.failure_kind,
            "requires_explicit_retry": True,
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
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
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
    max_retries = (
        0
        if meta.get("disable_provider_retries")
        else config.TEXT_PROVIDER_MAX_RETRIES
    )
    stage_key = str(meta.get("stage_key") or "") or None
    for failure_no in range(max_retries + 1):
        try:
            from app.generation_concurrency import run_with_provider_call_slot

            provider_kwargs: dict[str, Any] = {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "call_meta": meta,
            }
            if usage_callback is not None:
                provider_kwargs["usage_callback"] = usage_callback
            result = await run_with_provider_call_slot(
                lambda: hiagent.chat(messages, **provider_kwargs)
            )
            if meta.get("expected_json") and _is_non_candidate_json_response(result):
                raise hiagent.ProviderError(
                    "文本模型未返回任务 JSON 候选",
                    retryable=True,
                    raw=result,
                )
            return result
        except hiagent.ProviderError as exc:
            if exc.requires_explicit_retry:
                _append_interrupted_event(exc, meta)
            if (
                not exc.retryable
                or not exc.replay_safe
                or failure_no >= max_retries
            ):
                raise

            retry_no = failure_no + 1
            delay = config.TEXT_PROVIDER_RETRY_BASE_DELAY * (2 ** failure_no)
            message = (
                f"文本模型请求明确未送达，约 {int(delay)} 秒后自动执行"
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
    output_schema: dict[str, Any] | None = None,
    repair_schema: Callable[[T], dict[str, Any]] | None = None,
    normalize_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    on_attempt: Callable[[dict[str, Any]], Any] | None = None,
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
) -> T:
    """Run one typed model operation with separate format/semantic budgets.

    Transport retries remain owned by :func:`chat`.  A malformed HTTP-200 is
    never returned as success: it is recovered locally when a complete trailing
    object exists, otherwise only the schema and response tail are sent for one
    bounded format repair.  Business validation uses its own retry budget.
    """
    if not operation_id.strip():
        raise ValueError("structured operation_id is required")
    structured_schema = output_schema or _model_schema(model_type)
    base_messages = [dict(message) for message in messages]
    current_messages = base_messages
    format_attempt = 0
    semantic_attempt = 0
    last_raw = ""
    local_recovery = False
    while True:
        attempt_operation_id = operation_id
        if format_attempt or semantic_attempt:
            attempt_identity = repository.content_hash({
                "base_operation_id": operation_id,
                "format_attempt": format_attempt,
                "semantic_attempt": semantic_attempt,
                "messages": current_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "structured_schema": structured_schema,
            })
            attempt_operation_id = (
                f"{operation_id}:structured-attempt:{attempt_identity}"
            )
        meta = {
            **(call_meta or {}),
            "operation_id": attempt_operation_id,
            "base_operation_id": operation_id,
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
            usage_callback=usage_callback,
        )
        try:
            direct_error = json.loads(last_raw.strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            direct_error = None
        if (
            isinstance(direct_error, dict)
            and set(direct_error) == {"error"}
            and "error" not in model_type.model_fields
            and str(direct_error["error"] or "").strip()
        ):
            message = str(direct_error["error"]).strip()
            if on_attempt is not None:
                on_attempt({
                    **meta,
                    "outcome": "provider_rejected",
                    "raw_response": last_raw,
                    "output_chars": len(last_raw),
                    "local_recovery": False,
                    "validation_errors": [message],
                })
            raise StructuredProviderRejection(message)
        candidates = _json_candidates(last_raw)
        # Reuse the repository's conservative JSON repair for provider output
        # that is structurally complete but contains an unescaped quote/newline
        # inside a declared string field.  This is a local format recovery, so it
        # does not consume either the provider transport budget or the semantic
        # correction budget.
        if not candidates:
            try:
                from app.schemas import extract_json

                recovered = extract_json(
                    last_raw,
                    repair_unescaped_inner_quotes=True,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                recovered = None
            if isinstance(recovered, dict):
                candidates = [recovered]
                local_recovery = True
        parsed: T | None = None
        parse_error: Exception | None = None
        repair_payload: dict[str, Any] | None = None
        for candidate_no, payload in enumerate(candidates):
            candidate_payload = (
                normalize_payload(payload)
                if normalize_payload is not None
                else payload
            )
            normalized_locally = candidate_payload != payload
            try:
                parsed = _coerce_structured(model_type, candidate_payload)
            except (TypeError, ValueError, ValidationError) as exc:
                # Candidates are ordered from the latest complete outer object
                # to its nested objects. Preserve the outer object's error;
                # overwriting it with a nested unit's missing-root-fields error
                # sends the format repair down the wrong path.
                if parse_error is None:
                    parse_error = exc
                    repair_payload = candidate_payload
                continue
            try:
                direct_payload = json.loads(last_raw.strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                direct_payload = None
            local_recovery = bool(
                local_recovery
                or normalized_locally
                or candidate_no > 0
                or not isinstance(direct_payload, dict)
                or direct_payload != candidate_payload
            )
            break
        if parsed is None:
            if on_attempt is not None:
                on_attempt({
                    **meta,
                    "outcome": "format_error",
                    "raw_response": last_raw,
                    "output_chars": len(last_raw),
                    "local_recovery": local_recovery,
                    "validation_errors": [
                        str(parse_error or "找不到完整 JSON 对象")
                    ],
                })
            if format_attempt >= max(0, int(format_retry_limit)):
                detail = str(parse_error or "找不到完整 JSON 对象")
                raise StructuredFormatError(
                    f"{operation_id} 结构化输出失败：{detail}"
                ) from parse_error
            format_attempt += 1
            candidate_text = (
                json.dumps(
                    repair_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if repair_payload is not None
                else last_raw
            )
            current_messages = [
                {
                    "role": "user",
                    "content": (
                        "只修复下面响应的 JSON 格式和 Schema，不改写其语义。"
                        "只输出一个完整 JSON 对象。\nSchema:\n"
                        + json.dumps(structured_schema, ensure_ascii=False)
                        + "\nSchema 校验错误：\n"
                        + str(parse_error or "找不到完整 JSON 对象")
                        + "\n完整候选：\n"
                        + candidate_text
                    ),
                }
            ]
            continue

        try:
            validation_result = validate(parsed) if validate is not None else None
        except Exception as exc:  # business validators may be fail-fast
            validation_result = [str(exc)]
        semantic_errors = _validation_messages(validation_result)
        if not semantic_errors:
            if on_attempt is not None:
                on_attempt({
                    **meta,
                    "outcome": "validated",
                    "raw_response": last_raw,
                    "output_chars": len(last_raw),
                    "local_recovery": local_recovery,
                    "validation_errors": [],
                })
            return parsed
        if on_attempt is not None:
            on_attempt({
                **meta,
                "outcome": "semantic_error",
                "raw_response": last_raw,
                "output_chars": len(last_raw),
                "local_recovery": local_recovery,
                "validation_errors": semantic_errors[:20],
            })
        if semantic_attempt >= max(0, int(semantic_retry_limit)):
            raise StructuredSemanticError(
                f"{operation_id} 业务校验失败：" + "；".join(semantic_errors[:10])
            )
        semantic_attempt += 1
        semantic_schema = (
            repair_schema(parsed)
            if repair_schema is not None
            else structured_schema
        )
        if not isinstance(semantic_schema, dict):
            raise TypeError("repair_schema must return a JSON Schema object")
        current_messages = [
            {
                "role": "user",
                "content": (
                    "只修复以下业务问题，保持其余已验证字段不变；只输出完整 JSON。\n"
                    "问题：\n- "
                    + "\n- ".join(semantic_errors[:20])
                    + ("\n最小修复上下文：\n" + repair_context if repair_context else "")
                    + "\n输出 Schema：\n"
                    + json.dumps(semantic_schema, ensure_ascii=False)
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
