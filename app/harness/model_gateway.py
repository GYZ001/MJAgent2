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


def _is_nested_json_candidate(text: str, start: int, end: int) -> bool:
    """Track only the JSON prefix states needed to prove child provenance."""
    decoder = json.JSONDecoder()
    containers: list[list[str]] = []
    damaged_root = False
    index = 0

    def close_container(kind: str) -> None:
        nonlocal damaged_root
        if not containers or containers[-1][0] != kind:
            damaged_root = True
            return
        containers.pop()
        if containers:
            containers[-1][1] = "comma"
        else:
            # A matching top-level closer is the only token that explicitly
            # ends an active root and permits a later independent candidate.
            damaged_root = False

    while index < start:
        char = text[index]
        if char.isspace():
            index += 1
            continue

        token = char
        token_end = index + 1
        if char == '"':
            try:
                value, token_size = decoder.raw_decode(text[index:start])
            except json.JSONDecodeError:
                # The candidate starts inside an unterminated JSON string.
                return True
            if not isinstance(value, str):
                damaged_root = True
            token = "string"
            token_end = index + token_size
        elif char in "-0123456789tfn":
            try:
                _, token_size = decoder.raw_decode(text[index:start])
            except json.JSONDecodeError:
                token = "syntax_error"
            else:
                token = "scalar"
                token_end = index + token_size
        elif char not in "{}[],:":
            if containers or damaged_root:
                damaged_root = True
            index += 1
            continue

        if not containers:
            if token == "{":
                containers.append(["object", "key"])
                damaged_root = False
            elif token == "[":
                containers.append(["array", "value"])
                damaged_root = False
            index = token_end
            continue

        while True:
            kind, state = containers[-1]
            is_value = token in {"{", "[", "string", "scalar"}
            if kind == "object":
                if state == "key":
                    if token == "}":
                        close_container("object")
                    elif token == "string":
                        containers[-1][1] = "colon"
                    else:
                        damaged_root = True
                    break
                if state == "colon":
                    if token == ":":
                        containers[-1][1] = "value"
                        break
                    if is_value:
                        containers[-1][1] = "value"
                        continue
                    damaged_root = True
                    break
                if state == "comma":
                    if token == ",":
                        containers[-1][1] = "key"
                        break
                    if token == "}":
                        close_container("object")
                        break
                    if token == "string":
                        containers[-1][1] = "key"
                        continue
                    damaged_root = True
                    break
            else:
                if state == "comma":
                    if token == ",":
                        containers[-1][1] = "value"
                        break
                    if token == "]":
                        close_container("array")
                        break
                    if is_value:
                        containers[-1][1] = "value"
                        continue
                    damaged_root = True
                    break
                if token == "]":
                    close_container("array")
                    break

            if not is_value:
                damaged_root = True
            elif token == "{":
                containers.append(["object", "key"])
            elif token == "[":
                containers.append(["array", "value"])
            else:
                containers[-1][1] = "comma"
            break
        index = token_end

    return bool(containers or damaged_root)


def _json_candidates(value: str) -> list[dict[str, Any]]:
    """Return complete root JSON objects, preferring a trailing response.

    Providers occasionally prepend commentary or leave a truncated object
    before emitting a complete corrected object.  ``raw_decode`` lets us find
    complete suffixes without trying to manufacture missing semantic content.
    Objects with explicit parent-container boundaries are nested values, not
    substitutes for the requested root payload.
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
        end += index
        if (
            isinstance(payload, dict)
            and not _is_nested_json_candidate(text, index, end)
        ):
            candidates.append((end, payload))
    return [payload for _end, payload in sorted(candidates, key=lambda item: item[0], reverse=True)]


def _latest_json_authority_root(value: str) -> tuple[str, str] | None:
    """Return the latest object/array root proven not to be a child."""
    text = str(value or "").strip()
    decoder = json.JSONDecoder()
    latest_root: tuple[str, str] | None = None
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            _, candidate_size = decoder.raw_decode(text[index:])
        except (TypeError, ValueError, json.JSONDecodeError):
            end = len(text)
        else:
            end = index + candidate_size
        if not _is_nested_json_candidate(text, index, end):
            root_type = "object" if char == "{" else "array"
            root_text = text[index:] if root_type == "object" else text[index:end]
            latest_root = (root_type, root_text)
    return latest_root


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
    response_format: dict[str, Any] | None = None,
) -> str:
    """The only text-model entry point for business stages.

    It enforces trace metadata at the harness boundary while retaining the
    provider adapter's retry, redaction and lifecycle recording.

    ``response_format`` 用于让网关在生成阶段就约束输出为合法 JSON（json_object /
    json_schema）；仅结构化调用会传入，供应商不支持时适配层自动去掉并重试。
    """
    trace = current_trace()
    meta = {
        "gateway": "execution_harness",
        "run_id": trace.run_id,
        "step_run_id": trace.step_run_id,
        "trace_id": trace.trace_id,
        **(call_meta or {}),
    }
    # 任何声明期望 JSON 的业务调用（chat_structured 及直接带 expected_json 的分片/蓝图等）
    # 都在生成阶段就约束合法 JSON。显式传入的 response_format 优先；否则按 expected_json 兜底。
    # 这样无需逐个改调用点，统一在 harness 边界收口，供应商不支持时适配层会自动去掉重试。
    effective_response_format = response_format
    if effective_response_format is None and meta.get("expected_json"):
        effective_response_format = {"type": "json_object"}
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
            if effective_response_format is not None:
                provider_kwargs["response_format"] = effective_response_format
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
    format_repair_context: str = "",
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
        parsed: T | None = None
        parse_error: Exception | None = None
        repair_payload: dict[str, Any] | None = None
        # Only the latest provenance-qualified root may represent this response.
        authority_root = _latest_json_authority_root(last_raw)
        root_type, recovery_root = authority_root or (None, None)
        repair_candidate_text = recovery_root or last_raw
        payload: dict[str, Any] | None = None
        decoded: Any = None
        repaired_locally = False
        if recovery_root is not None:
            local_recovery = bool(
                local_recovery
                or recovery_root.strip() != last_raw.strip()
            )
            if root_type == "array":
                parse_error = ValueError(
                    "JSON 根节点类型错误：期望 object，最新顶层 authority 为 array"
                )
            else:
                try:
                    decoded, _ = json.JSONDecoder().raw_decode(recovery_root)
                except (TypeError, ValueError, json.JSONDecodeError):
                    try:
                        from app.schemas import extract_json

                        decoded = extract_json(
                            recovery_root,
                            repair_unescaped_inner_quotes=True,
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        parse_error = exc
                    else:
                        repaired_locally = True
                if isinstance(decoded, dict):
                    payload = decoded
                elif parse_error is None:
                    parse_error = ValueError("JSON 根节点不是对象")

        if payload is not None:
            candidate_payload = (
                normalize_payload(payload)
                if normalize_payload is not None
                else payload
            )
            normalized_locally = candidate_payload != payload
            repair_payload = candidate_payload
            local_recovery = bool(
                local_recovery
                or repaired_locally
                or normalized_locally
                or not isinstance(direct_error, dict)
                or direct_error != candidate_payload
            )
            try:
                parsed = _coerce_structured(model_type, candidate_payload)
            except (TypeError, ValueError, ValidationError) as exc:
                parse_error = exc
            else:
                if repaired_locally:
                    explicit_fields = getattr(parsed, "model_fields_set", None)
                    if explicit_fields is not None and not explicit_fields:
                        parsed = None
                        parse_error = ValueError(
                            "修复后的 JSON 未显式提供任何模型字段"
                        )
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
                else repair_candidate_text
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
                        + (
                            "\n格式修复权威上下文：\n"
                            + format_repair_context
                            if format_repair_context
                            else ""
                        )
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
