from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app import config, hiagent
from app.db import get_conn, now
from app.evidence import repository
from app.harness.model_gateway_moderation import attempt_moderation_fallback, replay_safe_stream_interruption
from app.observability.tracing import current_trace
from app.orchestration.state_machine import transition_run

T = TypeVar("T")


class StructuredOutputError(ValueError):
    """Base class for content failures after a successful provider transport."""


class StructuredFormatError(StructuredOutputError):
    """The response could not be turned into the contracted object.

    ``unparseable`` separates the two very different causes this covers: True
    means the provider never delivered a syntactically complete answer of its
    own -- either nothing decoded at all, or the bytes were corrupt/truncated
    and only became an object after local repair closed containers the model
    left open.  False means a well-formed JSON object arrived and simply
    disagrees with the schema (the provider *did* author an answer, it is just
    the wrong one).  Callers under a no-retry contract use that distinction: an
    undelivered answer may be resampled, a wrong answer must not be.
    """

    unparseable: bool = False


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

def _json_authority_candidates(text: str) -> list[tuple[int, str, str]]:
    """Enumerate every object/array root proven not to be a child, in order.

    A single corrupted response can contain more than one substring that
    independently proves out as an unnested top-level JSON value (a
    duplicated/malformed key mid-stream can close a container early and what
    follows then reads as a fresh root). This only *enumerates* them -- it
    makes no claim about which one, if any, the provider actually intended
    as its answer; that judgement belongs to the caller
    (:func:`_latest_json_authority_root`).
    """
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, str, str]] = []
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
            candidates.append((index, root_type, root_text))
    return candidates

# A candidate is a "dangling container child" when the text immediately
# before it (skipping whitespace) is either a bare comma, or a JSON string
# literal directly followed by a colon (a dict "key": prefix). Both are
# syntax proof -- not a guess -- that this candidate was authored as a
# *value inside* some enclosing list/object, never as a fresh top-level
# answer: valid JSON allows exactly one top-level value, so a raw "," or
# '"key":' can only legally precede another element of the same container.
# The only reason such a candidate ever reads as "top-level" at all is that
# its true parent container was closed early by earlier corruption (see
# ERR-20260824-7ab7cb: duplicated/malformed keys mid-stream). This is
# deliberately narrower than "does it look incomplete" -- prose, an explicit
# correction marker ("以下是最新修正版"/"最终答案:"), or simply nothing
# (start of the response) never match, so a model's own free-standing
# restart is never misclassified as a dangling fragment.
_DANGLING_CONTAINER_CHILD_RE = re.compile(r'(,|"(?:[^"\\]|\\.)*"\s*:)\s*\Z')

def _is_dangling_container_child(text: str, index: int) -> bool:
    return bool(_DANGLING_CONTAINER_CHILD_RE.search(text[:index]))

def _latest_json_authority_root(value: str) -> tuple[str, str, int] | None:
    """Return the latest object/array root proven not to be a child, plus how
    many independently well-formed roots were in play.

    This restores the original "latest wins" position rule -- a real,
    tested design (``tests/test_screenplay_structured_runner.py``): the
    provider's most recent complete/attempted top-level answer is treated as
    authoritative even when it is the wrong root type or outright
    unparseable, and the run must fail rather than silently resurrect an
    older, already-superseded draft (a stale-data bug is just as real as a
    dropped-field bug, in the opposite direction).

    The one narrowing on top of that rule: a candidate proven to be a
    dangling container child (see ``_is_dangling_container_child``) is
    excluded from "latest" consideration entirely, unless every candidate
    found is one. This is what fixes ERR-20260824-7ab7cb without touching
    the general rule -- the EP7 corruption's trailing ``scenes`` array (and
    every other array in that response) is provably a stranded dict value,
    not a fresh restart, so it never competes for "latest" against the
    earlier, genuine, complete object that actually carried ``characters``.
    A response with no such stranded fragments behaves identically to
    before: whichever candidate is positionally last simply wins.

    The third tuple element is the total number of independently
    well-formed roots found, regardless of eligibility; the caller treats
    anything above 1 as "more than one candidate existed" and must surface
    that on its own (see ``_append_recovery_discard_event``) even when the
    chosen one later validates fine -- a discarded sibling is invisible to
    schema validation of the winner alone.
    """
    text = str(value or "").strip()
    candidates = _json_authority_candidates(text)
    if not candidates:
        return None
    eligible = [
        c for c in candidates if not _is_dangling_container_child(text, c[0])
    ]
    pool = eligible or candidates
    _, root_type, root_text = max(pool, key=lambda c: c[0])
    return root_type, root_text, len(candidates)

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


def _append_recovery_discard_event(
    *,
    operation_id: str,
    root_type: str | None,
    candidate_count: int,
    call_meta: dict[str, Any],
) -> None:
    """Make a discarded JSON authority-root candidate visible, never blocking.

    ``_latest_json_authority_root`` had to choose one root among several
    independently well-formed ones; this records that a choice was made and
    something was left behind, even when the choice turns out fine and the
    call ultimately validates -- schema validation of the winner alone can
    never distinguish "this field was legitimately empty" from "this field
    had data in a candidate we didn't pick" (see ERR-20260824-7ab7cb, where
    the picked candidate validated to an empty ``characters`` list without
    raising anything). Visible, not blocking: this never changes control
    flow, it only leaves a trail so a repeat of this failure shape can be
    found instead of silently reproducing the same data loss.
    """
    trace = current_trace()
    if not trace.run_id:
        return
    repository.append_event(
        trace.run_id,
        "STRUCTURED_JSON_RECOVERY_CANDIDATE_DISCARDED",
        "warning",
        (
            f"{operation_id}：响应中存在多个互不嵌套的顶层 JSON 根，"
            "已按“最新且非悬空容器子片段”选取一个，其余候选未被采用"
            "（可能包含未被采用的有效数据）"
        ),
        step_run_id=trace.step_run_id,
        trace_id=trace.trace_id,
        payload={
            "chosen_root_type": root_type,
            "candidate_count": candidate_count,
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
    provider: str | None = None,
) -> str:
    """The only text-model entry point for business stages.

    It enforces trace metadata at the harness boundary while retaining the
    provider adapter's retry, redaction and lifecycle recording.

    ``response_format`` 用于让网关在生成阶段就约束输出为合法 JSON（json_object /
    json_schema）；仅结构化调用会传入，供应商不支持时适配层自动去掉并重试。

    ``provider`` 显式传入时优先；否则读取
    ``app.harness.text_provider_scope.current_stage_text_provider()``——世界书/
    映射台/分镜台的领域任务入口用 ``stage_text_provider(...)`` 包一层后，本函数
    下游所有调用不必逐个改调用点就能拿到该环节配置的专属文本 provider；两者都
    没有时为 None，即此前的默认行为（走 ``active_provider("text")``）。
    """
    if provider is None:
        from app.harness.text_provider_scope import current_stage_text_provider

        provider = current_stage_text_provider()
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
    # Strict OpenAI-compatible gateways require the prompt itself to name JSON
    # when ``json_object`` is requested.  Normalize once at the harness boundary
    # so the first provider attempt, durable request hash and any retry all use
    # the same caller-independent message snapshot.
    provider_messages = hiagent._messages_for_response_format(
        messages,
        effective_response_format,
    )
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
            if provider is not None:
                provider_kwargs["provider"] = provider
            if usage_callback is not None:
                provider_kwargs["usage_callback"] = usage_callback
            if effective_response_format is not None:
                provider_kwargs["response_format"] = effective_response_format
            result = await run_with_provider_call_slot(
                lambda: hiagent.chat(provider_messages, **provider_kwargs)
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
            if exc.failure_category == "model_rejection":  # WS1b 换路，见 model_gateway_moderation
                fallback_result = await attempt_moderation_fallback(
                    provider_messages, provider_kwargs, meta,
                )
                if fallback_result is not None:
                    return fallback_result
            if (
                not exc.retryable
                or not (exc.replay_safe or replay_safe_stream_interruption(exc))
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
                transition_run(trace.run_id, "RUNNING", "WAITING_RETRY", message, conn=None)
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
                transition_run(trace.run_id, "WAITING_RETRY", "RUNNING", "重试冷却结束，恢复执行", conn=None)
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
    response_format: dict[str, Any] | None = None,
    require_response_format: bool = False,
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
    if require_response_format and response_format is None:
        raise ValueError(
            "require_response_format needs an explicit response_format"
        )
    stage_key = str((call_meta or {}).get("stage_key") or "")
    substage = str((call_meta or {}).get("substage") or "")
    identity_response_format_names = {
        "current_identity": "screenplay_current_identity_discovery_v11",
        "future_identity": "screenplay_future_identity_resolution_v10",
        "structural_coverage": "screenplay_structural_identity_coverage_v6",
    }
    strict_identity_substage = (
        stage_key == "screenplay_character_discovery"
        and substage in identity_response_format_names
    )
    strict_json_schema_contract = (
        stage_key == "screenplay_scene_shard_semantic_repair"
        or strict_identity_substage
    )
    # Scope of the block below: it only validates the *caller's* arguments to
    # this function (the Python-level contract at this call boundary) -- it
    # never inspects the bytes actually sent over HTTP, so on its own it
    # cannot prove the provider received this exact json_schema. What closes
    # that gap is `require_response_format=True` (forced True here) flowing
    # into `call_meta["response_format_required"]` and from there into
    # `app.hiagent.chat()`: that function structurally guarantees a
    # response_format_required call either sends the caller's response_format
    # unmodified (retrying the identical request in place on a transient-
    # looking 400) or raises -- it never silently downgrades json_schema to
    # json_object/None the way opportunistic calls may. So the two layers
    # together do guarantee the wire payload, but neither one alone does;
    # do not read this block as a payload-level assertion by itself.
    if strict_json_schema_contract:
        json_schema = (
            response_format.get("json_schema")
            if isinstance(response_format, dict)
            else None
        )
        if (
            not require_response_format
            or not isinstance(response_format, dict)
            or response_format.get("type") != "json_schema"
            or not isinstance(json_schema, dict)
            or json_schema.get("strict") is not True
            or not isinstance(json_schema.get("schema"), dict)
        ):
            contract_name = "/".join(
                value for value in (stage_key, substage) if value
            )
            raise ValueError(
                f"{contract_name} requires strict json_schema "
                "response_format"
            )
        if strict_identity_substage:
            expected_name = identity_response_format_names[substage]
            if json_schema.get("name") != expected_name:
                raise ValueError(
                    f"{stage_key}/{substage} requires response_format "
                    f"name={expected_name}"
                )
            if (
                int(format_retry_limit) != 0
                or int(semantic_retry_limit) != 0
            ):
                raise ValueError(
                    f"{stage_key}/{substage} forbids structured retries"
                )
            if not bool((call_meta or {}).get("disable_provider_retries")):
                raise ValueError(
                    f"{stage_key}/{substage} forbids provider retries"
                )
            if not bool(
                (call_meta or {}).get("disable_provider_candidate_fallback")
            ):
                raise ValueError(
                    f"{stage_key}/{substage} forbids provider candidate fallback"
                )
            if not bool((call_meta or {}).get("disable_reasoning_fallback")):
                raise ValueError(
                    f"{stage_key}/{substage} forbids reasoning fallback"
                )
            if (call_meta or {}).get("reuse_successful_operation") is not False:
                raise ValueError(
                    f"{stage_key}/{substage} forbids raw provider success reuse"
                )
    structured_schema = output_schema or _model_schema(model_type)
    base_messages = [dict(message) for message in messages]
    current_messages = base_messages
    format_attempt = 0
    semantic_attempt = 0
    last_raw = ""
    local_recovery = False
    recovery_candidate_discarded = False
    while True:
        attempt_operation_id = operation_id
        if format_attempt or semantic_attempt:
            attempt_identity_payload = {
                "base_operation_id": operation_id,
                "format_attempt": format_attempt,
                "semantic_attempt": semantic_attempt,
                "messages": current_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "structured_schema": structured_schema,
            }
            if response_format is not None:
                attempt_identity_payload.update({
                    "response_format": response_format,
                    "require_response_format": require_response_format,
                })
            attempt_identity = repository.content_hash(
                attempt_identity_payload
            )
            attempt_operation_id = (
                f"{operation_id}:structured-attempt:{attempt_identity}"
            )
        meta: dict[str, Any] = {
            **(call_meta or {}),
            "operation_id": attempt_operation_id,
            "base_operation_id": operation_id,
            "expected_json": True,
            "format_attempt": format_attempt,
            "semantic_attempt": semantic_attempt,
            "local_recovery": local_recovery,
            "recovery_candidate_discarded": recovery_candidate_discarded,
        }
        if require_response_format:
            meta["response_format_required"] = True
        last_raw = await chat(
            current_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            call_meta=meta,
            usage_callback=usage_callback,
            response_format=response_format,
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
        # The latest non-nested root wins (see _latest_json_authority_root's
        # docstring) unless it is proven to be a dangling container child --
        # a fragment stranded by earlier corruption, not a fresh answer.
        authority_root = _latest_json_authority_root(last_raw)
        root_type, recovery_root, candidate_count = (
            authority_root or (None, None, 0)
        )
        if candidate_count > 1:
            recovery_candidate_discarded = True
            _append_recovery_discard_event(
                operation_id=attempt_operation_id,
                root_type=root_type,
                candidate_count=candidate_count,
                call_meta=meta,
            )
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
                error = StructuredFormatError(
                    f"{operation_id} 结构化输出失败：{detail}"
                )
                # "Did the provider deliver an authored answer?" is answered by
                # what it actually emitted, not by what we could salvage from
                # it.  ``payload is None`` covers the case where nothing decoded
                # at all; ``repaired_locally`` covers the case where the bytes
                # were syntactically broken and ``extract_json`` had to close
                # containers the model never closed.  Both mean the same thing:
                # there is no complete answer of the model's own authorship to
                # preserve, so a resample is a real second chance rather than
                # re-rolling a wrong answer until it passes.
                error.unparseable = payload is None or repaired_locally
                raise error from parse_error
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
