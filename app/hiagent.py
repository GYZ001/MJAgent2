"""HiAgent 网关客户端。全部真实调用，禁止 mock（PRD 原则 P1）；失败必须携带原始报文向上抛（P2）。

API 形态依据 M0 实测（docs/HIAGENT_INTEGRATION.md）：
- chat/completions：OpenAI 兼容；文本模型为推理模型，只读 message.content。
- 视频：POST /contents/generations/tasks 创建（网关无同步参数校验！），GET /tasks/{id} 轮询，
  succeeded 后 content.video_url 7 天过期，必须立即下载。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import json
import re
import sqlite3
import shutil
import subprocess
import time
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urljoin

import httpx

from app import config
from app.atomic_io import atomic_write_bytes
from app.db import (finish_provider_call, get_conn, get_setting, log_provider_call,
                    provider_operation_id, start_provider_call,
                    update_provider_call_progress, update_provider_call_request)
from app.harness.hiagent_input_image_privacy import (
    INPUT_IMAGE_PRIVACY_REJECTED_KIND, is_input_image_privacy_rejection,
)
from app.harness.hiagent_stream_evidence import (
    INTERRUPTED_STREAM_TEXT_CHARS, classify_interrupted_stream,
    interrupted_stream_evidence, remember_unconsumed_stream_frame,
)
from app.model_capabilities import (
    active_model_token_limits,
)

BAILIAN_TEXT_FREE_MODELS = (
    "qwen3.7-max-2026-06-08",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-05-17",
    "qwen3.7-max-preview",
    "qwen3.7-plus-2026-05-26",
)
BAILIAN_TEXT_BASE_MODELS = ("qwen3.7-max", "qwen3.7-plus")
BAILIAN_VLM_FREE_MODELS = ("qwen3.7-plus-2026-05-26",)
BAILIAN_VLM_BASE_MODELS = ("qwen3.7-plus",)
_BAILIAN_FAILED_MODELS: dict[str, set[str]] = {"text": set(), "vlm": set()}
_IMAGE_SEMAPHORES: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _provider_request_matches(
    stored_request: Any,
    current_payload: dict[str, Any],
) -> bool:
    """Compare semantic request bytes while ignoring SSE transport toggles."""
    if stored_request == current_payload:
        return True
    if not isinstance(stored_request, dict):
        return False
    normalized = dict(stored_request)
    normalized.pop("stream", None)
    normalized.pop("stream_options", None)
    return normalized == current_payload


def _reusable_operation_candidate_rows(
    kind: str,
    model: str,
    payload: dict[str, Any],
    meta: dict[str, Any] | None,
):
    """Yield ``(row, parsed_response)`` for durable rows of this idempotent
    operation, most-recent first, after the identity gates (reuse opt-in,
    operation id, contract version, request hash/body) that decide whether a
    stored row is even a candidate for *this* operation at all.

    Delivery-shape judgement (truncated vs. content-delivered vs. nothing
    delivered) is deliberately left to callers.
    ``_cached_successful_provider_response`` and
    ``_durable_operation_proven_undelivered`` both need "which rows belong to
    this operation" to be the exact same set, or the guard and the cache
    could reach contradictory conclusions about the same history from two
    independently-drifting queries.
    """
    if not bool((meta or {}).get("reuse_successful_operation")):
        return
    if not (
        str((meta or {}).get("operation_id") or "").strip()
        or str((meta or {}).get("semantic_attempt_id") or "").strip()
    ):
        # A byte-identical prompt is not proof of semantic success. Reuse is
        # allowed only when the caller names a durable business operation.
        return
    # A caller may provide a durable semantic operation id.  This is required
    # for repair workflows: the same semantic attempt must be recoverable after
    # a process crash, while a *new* repair attempt must never reuse an older
    # answer merely because its base prompt happens to be byte-identical.
    operation_id = str((meta or {}).get("operation_id") or "").strip() \
        or provider_operation_id(kind, model, payload)
    operation_ids = [operation_id]
    legacy_operation_id = str(
        (meta or {}).get("legacy_success_operation_id") or ""
    ).strip()
    if legacy_operation_id and legacy_operation_id != operation_id:
        operation_ids.append(legacy_operation_id)
    try:
        marks = ",".join("?" for _ in operation_ids)
        rows = get_conn().execute(
            "SELECT id,response_json,meta,request_json,request_hash,"
            "contract_version FROM provider_calls "
            f"WHERE operation_id IN ({marks}) AND kind=? AND model=? "
            "AND status IN ('OK','SUCCESS','SUCCEEDED') "
            "AND response_json IS NOT NULL ORDER BY id DESC LIMIT 20",
            (*operation_ids, kind, model),
        ).fetchall()
    except sqlite3.Error:
        return
    expected_contract = str((meta or {}).get("contract_version") or "").strip()
    try:
        for row in rows:
            if expected_contract:
                stored_meta = json.loads(row["meta"] or "{}")
                stored_contract = str(
                    row["contract_version"]
                    or stored_meta.get("contract_version")
                    or ""
                ).strip()
                # Legacy lossy meta may omit this field.  Exact operation,
                # model and full request_hash remain sufficient authority;
                # an explicitly different stored contract still fences reuse.
                if stored_contract and stored_contract != expected_contract:
                    continue
            value = json.loads(row["response_json"])
            if not isinstance(value, dict):
                continue
            from app.db import provider_request_hash

            stored_hash = str(row["request_hash"] or "")
            if stored_hash:
                if stored_hash != provider_request_hash(payload):
                    continue
            else:
                try:
                    stored_request = json.loads(row["request_json"] or "null")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not _provider_request_matches(stored_request, payload):
                    continue
            yield row, value
    except (json.JSONDecodeError, TypeError, ValueError):
        return


def _cached_successful_provider_response(
    kind: str,
    model: str,
    payload: dict[str, Any],
    meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """恢复时复用同一幂等 operation 的成功响应，避免成功结果因状态竞态丢失。"""
    for row, value in _reusable_operation_candidate_rows(kind, model, payload, meta):
        # A finish_reason=length response is truncated output: every
        # consumer rejects it via ``_reject_truncated_chat_response``, so
        # it is NOT a valid durable success. Replaying it only reproduces
        # the same failure on every run (and forces a pointless format
        # repair). Skip such rows so a later, valid attempt wins.
        try:
            finish_reason = value["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            finish_reason = None
        if finish_reason == "length":
            continue
        # A response with no finish_reason and no accounted completion
        # tokens is the same "provider delivered nothing" shape as the
        # length-truncated case above (see ``_content_delivery_absent``
        # / ``OUTPUT_MISSING`` in ``chat()``): the crash-recovery replay
        # this cache exists for must not hand a fresh attempt the exact
        # broken row it is retrying past, or the one-shot retry in
        # ``chat()`` would just refetch this same empty answer from the
        # database instead of ever reaching the network again.
        try:
            stored_content = value["choices"][0].get("message", {}).get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            stored_content = None
        if not (stored_content or "").strip() and _content_delivery_absent(value):
            continue
        operation_id = str((meta or {}).get("operation_id") or "").strip() \
            or provider_operation_id(kind, model, payload)
        log_provider_call(
            "provider_cache_hit",
            model,
            "REUSED",
            None,
            0,
            meta={
                **(meta or {}),
                "operation_id": operation_id,
                "source_provider_call_id": int(row["id"]),
            },
        )
        return value
    return None


def _durable_operation_proven_undelivered(
    kind: str,
    model: str,
    payload: dict[str, Any],
    meta: dict[str, Any] | None,
) -> bool:
    """Whether every durable row on record for this operation is objective
    proof the provider delivered nothing and billed nothing for it.

    This reuses ``_reusable_operation_candidate_rows`` -- the exact identity
    gates ``_cached_successful_provider_response`` applies -- so the two
    functions can never disagree about *which rows belong to this
    operation*, only about what a delivered-nothing row implies for each of
    them (one treats it as "not a valid replay", the other as "safe to
    resend").

    A row only counts if it is content-delivery-absent per
    ``_content_delivery_absent`` (no terminal ``finish_reason`` and no
    accounted ``completion_tokens``) -- the same bar ``chat()``'s own
    in-process OUTPUT_MISSING one-shot retry already uses to decide a resend
    is free of double-billing risk. A ``finish_reason == "length"`` row is
    proof of the *opposite*: the provider ran to its full output budget and
    was billed for it, so it must never satisfy this check even though
    ``_cached_successful_provider_response`` also treats it as "not a valid
    replay" -- conflating the two would let a truncated (real-cost) attempt
    silently authorize an unreserved resend.

    If no row at all is on record, this returns ``False``: an operation with
    zero history might still have a request in flight that this process
    cannot see, so "no receipt" must keep failing closed.
    """
    found_undelivered = False
    for _row, value in _reusable_operation_candidate_rows(kind, model, payload, meta):
        try:
            finish_reason = value["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            finish_reason = None
        if finish_reason == "length":
            return False
        try:
            stored_content = value["choices"][0].get("message", {}).get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            stored_content = None
        if (stored_content or "").strip() or not _content_delivery_absent(value):
            # A row with real content, or with finish_reason/token evidence
            # of a completed run, is a *delivered* answer -- reaching this
            # branch means ``_cached_successful_provider_response`` would
            # already have returned it as a valid replay, so ``data`` would
            # not have been ``None`` and this function would never have been
            # called for that row. Treat the mismatch as "not proven safe"
            # rather than assume it away.
            return False
        found_undelivered = True
    return found_undelivered


def _require_cached_replay_or_raise(
    data: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    *,
    kind: str | None = None,
    model: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    if data is not None or not bool(
        (meta or {}).get("require_cached_successful_operation")
    ):
        return
    if (
        kind is not None
        and model is not None
        and payload is not None
        and _durable_operation_proven_undelivered(kind, model, payload, meta)
    ):
        # ``claim()`` (app/stages.py) skipped a fresh budget reservation for
        # this attempt because raw DB status told it this operation already
        # succeeded. That premise just turned out false, but the specific
        # way it is false matters: the row it relied on is objective proof
        # of zero delivered content and zero billed tokens (see
        # ``_durable_operation_proven_undelivered``). There is nothing to
        # replay and nothing to double-charge, so falling through to a live
        # request here carries exactly the same (zero) revenue risk as the
        # one-shot "undelivered retry" ``chat()`` already performs
        # in-process for the identical evidence shape (OUTPUT_MISSING,
        # replay_safe=True) -- this only extends that same policy across a
        # process restart / fresh top-level attempt, instead of silently
        # denying it merely because the evidence now lives in the database
        # instead of the current call stack.
        return
    raise ProviderError(
        "durable provider 成功回执缺失，禁止未重新预留预算即重发",
        failure_kind="durable_replay_missing",
        delivery_state="not_sent",
        replay_safe=True,
    )


def _latest_provider_operation_request(
    kind: str,
    operation_id: str,
) -> dict[str, Any] | None:
    """Load request identity across models so a restart cannot reuse a key after model drift."""
    try:
        rows = get_conn().execute(
            """SELECT request_json FROM provider_calls
               WHERE kind=? AND operation_id=? AND request_json IS NOT NULL
               ORDER BY id DESC""",
            (kind, operation_id),
        ).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        try:
            request = json.loads(row["request_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(request, dict):
            return request
    return None


class ProviderFailureCategory(str, Enum):
    TECHNICAL = "technical"
    MODEL_REJECTION = "model_rejection"


class ProviderFailureDisposition(str, Enum):
    AUTOMATIC_RETRY = "automatic_retry"
    MANUAL_REVIEW = "manual_review"
    EXTERNAL_TERMINAL = "external_terminal"


class ProviderFailureKind(str, Enum):
    UNCLASSIFIED = "unclassified_provider_failure"
    EXECUTION_FAILED = "provider_execution_failed"
    TASK_NOT_FOUND = "provider_task_not_found"
    OUTPUT_MISSING = "provider_output_missing"
    OUTPUT_TRUNCATED = "output_truncated"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_REJECTED = "provider_rejected"
    PROMPT_PROVIDER_REJECTED = "prompt_provider_rejected"


@dataclass(frozen=True)
class ProviderFailure:
    category: ProviderFailureCategory
    kind: str
    disposition: ProviderFailureDisposition
    retryable: bool

    @classmethod
    def technical(
        cls,
        kind: str | ProviderFailureKind,
        *,
        retryable: bool = False,
        requires_explicit_retry: bool = False,
    ) -> ProviderFailure:
        disposition = (
            ProviderFailureDisposition.AUTOMATIC_RETRY
            if retryable and not requires_explicit_retry
            else ProviderFailureDisposition.MANUAL_REVIEW
        )
        return cls(
            category=ProviderFailureCategory.TECHNICAL,
            kind=kind.value if isinstance(kind, ProviderFailureKind) else str(kind),
            disposition=disposition,
            retryable=retryable,
        )

    @classmethod
    def model_rejection(
        cls,
        kind: str | ProviderFailureKind = ProviderFailureKind.PROVIDER_REJECTED,
    ) -> ProviderFailure:
        return cls(
            category=ProviderFailureCategory.MODEL_REJECTION,
            kind=kind.value if isinstance(kind, ProviderFailureKind) else str(kind),
            disposition=ProviderFailureDisposition.EXTERNAL_TERMINAL,
            retryable=False,
        )

    @classmethod
    def from_provider_payload(
        cls,
        payload: Any,
        *,
        default_kind: str | ProviderFailureKind = ProviderFailureKind.EXECUTION_FAILED,
    ) -> ProviderFailure:
        """Normalize typed adapter fields without inspecting provider error prose."""
        default = (
            default_kind.value
            if isinstance(default_kind, ProviderFailureKind)
            else str(default_kind)
        )
        if not isinstance(payload, dict):
            return cls.technical(default)
        kind = str(payload.get("kind") or default)
        if payload.get("category") == ProviderFailureCategory.MODEL_REJECTION.value:
            return cls.model_rejection(kind)
        return cls.technical(kind, retryable=payload.get("retryable") is True)

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "kind": self.kind,
            "disposition": self.disposition.value,
            "retryable": self.retryable,
        }

    @property
    def reason_code(self) -> str:
        suffix = "".join(
            character if character.isalnum() else "_"
            for character in self.kind.upper()
        ).strip("_")
        return f"VIDEO_{suffix or 'PROVIDER_FAILURE'}"


def has_repeated_terminal_poll_failure(
    messages: Sequence[str],
    *,
    min_repeats: int = 2,
) -> bool:
    """纯行为判据：同一供应商任务标识连续 ``min_repeats`` 次返回终态失败，且
    ``error.message`` 字节级相同，判定为供应商对这次请求的确定性拒绝。

    这里判断的是结构——同一任务、同一结果、连续重复出现——不是内容：不得
    改成按关键词（例如 "copyright"）匹配 message 里写了什么词。真实供应商
    失败往往连 ``error.code``、``failure`` 结构化字段都没有（见
    ``ProviderFailure.from_provider_payload`` 对非结构化 payload 的兜底），
    行为重复本身就是这里唯一可用、也足够强的信号：同一请求签名对同一输入
    连续给出完全相同的结果，说明这不是瞬时抖动。

    ``messages`` 必须按时间顺序（旧到新）排列，且约定调用方已经把"本次"也
    纳入这个序列（例如直接查询已落库的调用账本，本次失败在写入判据前就已
    经提交），所以这里只看序列尾部的连续相同游程，不单独接收"当前值"参数。

    职责边界：本函数不做任何 I/O、不触碰数据库，只接受调用方已经取好的历史
    消息列表——跨调用行为比对（查询 provider_calls）是调用方的事，不属于
    ``ProviderFailure.from_provider_payload`` 解析供应商结构化字段的职责，
    也不属于这个纯判断函数本身。
    """
    if min_repeats < 2:
        min_repeats = 2
    if len(messages) < min_repeats:
        return False
    tail = list(messages[-min_repeats:])
    anchor = tail[0]
    if not anchor:
        return False
    return all(message == anchor for message in tail)


def provider_failure_from_http_payload(payload: Any) -> ProviderFailure | None:
    """Extract the typed provider failure shared by HTTP adapters."""
    if not isinstance(payload, dict):
        return None
    error_payload = payload.get("error")
    failure_payload = (
        error_payload.get("failure")
        if isinstance(error_payload, dict)
        else payload.get("failure")
    )
    if not isinstance(failure_payload, dict):
        return None
    return ProviderFailure.from_provider_payload(failure_payload)


class ProviderError(Exception):
    """对外调用失败。message 面向 UI，包含分类结论 + 原始报文摘要。"""

    def __init__(self, message: str, *, retryable: bool = False, raw: str = "",
                 timeout_phase: str | None = None, failure_kind: str = "",
                 delivery_state: str = "unknown", replay_safe: bool = False,
                 requires_explicit_retry: bool = False,
                 create_not_accepted: bool = False,
                 received_chars: int = 0,
                 failure: ProviderFailure | None = None):
        super().__init__(message)
        if failure is None:
            failure = ProviderFailure.technical(
                failure_kind or ProviderFailureKind.UNCLASSIFIED,
                retryable=retryable,
                requires_explicit_retry=requires_explicit_retry,
            )
        self.failure = failure
        self.retryable = failure.retryable
        self.raw = raw[:500]
        self.timeout_phase = timeout_phase
        self.failure_kind = failure.kind
        self.failure_category = failure.category.value
        self.failure_disposition = failure.disposition.value
        self.delivery_state = delivery_state
        self.replay_safe = replay_safe
        self.requires_explicit_retry = requires_explicit_retry
        self.create_not_accepted = bool(create_not_accepted)
        self.received_chars = int(received_chars or 0)


def _channel_semaphore(
    store: weakref.WeakKeyDictionary[Any, asyncio.Semaphore],
    limit: int,
) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = store.get(loop)
    desired = max(1, int(limit))
    if semaphore is None or getattr(semaphore, "_mj_limit", None) != desired:
        semaphore = asyncio.Semaphore(desired)
        semaphore._mj_limit = desired  # type: ignore[attr-defined]
        store[loop] = semaphore
    return semaphore


def _image_semaphore() -> asyncio.Semaphore:
    try:
        from app.media_pipeline.concurrency import channel_limit
        from app.media_pipeline import stages as media_stages
        limit = channel_limit(media_stages.RESOURCE_IMAGE)
    except Exception:  # noqa: BLE001
        limit = getattr(config, "IMAGE_REQUEST_CONCURRENCY", config.MEDIA_REQUEST_CONCURRENCY)
    return _channel_semaphore(_IMAGE_SEMAPHORES, limit)


def _timeout_phase(exc: httpx.TimeoutException) -> str:
    if isinstance(exc, httpx.WriteTimeout):
        return "write"
    if isinstance(exc, httpx.ReadTimeout):
        return "read"
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool"
    return "unknown"


def _timeout_diagnostic_label(
    client: httpx.AsyncClient, meta: dict | None, kind: str,
    *, override_threshold_s: float | None = None,
) -> str:
    """一句话诊断标签，附在每条超时错误里：哪种调用、配了多少读超时。

    调用类型优先取 ``stage_key``（本文件超时分派的实际判据），没有就退到
    ``stage``，再没有就退到 ``kind``（chat/vlm_qa/image_generate 等最外层
    请求类别）——三者总有一个能定位到具体是哪一类调用。读超时默认读
    ``client.timeout.read``，即这次请求实际生效的 httpx 超时对象，而不是
    重新跑一遍 ``_chat_read_timeout_s`` 推导：这个函数在 chat/vlm/image 等
    所有 kind 下都通用，client 自己的超时配置永远是唯一真实来源，不用
    也不该关心当前 kind 具体是靠哪条分派逻辑算出来的。``override_threshold_s``
    仅用于流式请求的外层总时长看门狗（``text_stream_total_timeout_s``）：
    那个上限不是 httpx 的 read 超时，client.timeout.read 在那种超时下答不对。
    """
    call_type = str((meta or {}).get("stage_key") or (meta or {}).get("stage") or kind or "unknown")
    if override_threshold_s is not None:
        read_timeout = override_threshold_s
    else:
        try:
            read_timeout = client.timeout.read
        except AttributeError:
            read_timeout = None
    threshold = f"{read_timeout:.0f}s" if read_timeout is not None else "unknown"
    return f"call_type={call_type} configured_read_timeout={threshold}"


def _transport_replay_state(exc: httpx.HTTPError) -> tuple[str, bool]:
    """Return delivery evidence without inferring from provider error text."""
    if isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError)):
        return "not_sent", True
    return "unknown", False


def _stream_timeout_replay_state(
    exc: httpx.HTTPError | None, received_chars: int
) -> tuple[str, bool]:
    """Only explicit pre-delivery connection failures are replay-safe.

    Receiving zero characters does not prove that the upstream request was not
    accepted or is no longer running. Read/unknown HTTP timeouts and the outer
    total-duration timeout therefore remain outcome-unknown and require an
    explicit retry, regardless of how many characters reached the caller.
    """
    del received_chars  # Delivery volume cannot establish that the request was not sent.
    if exc is not None:
        delivery_state, replay_safe = _transport_replay_state(exc)
        if replay_safe:
            return delivery_state, True
    return "unknown", False


def _transport_provider_error(
    exc: httpx.HTTPError,
    message: str,
    *,
    raw: str,
    timeout_phase: str | None = None,
) -> ProviderError:
    delivery_state, replay_safe = _transport_replay_state(exc)
    uncertain_suffix = (
        ""
        if replay_safe
        else "；请求结果不确定，已禁止自动重试，请在页面确认后重试"
    )
    return ProviderError(
        message + uncertain_suffix,
        retryable=True,
        raw=raw,
        timeout_phase=timeout_phase,
        failure_kind="connection_failed" if replay_safe else "request_outcome_unknown",
        delivery_state=delivery_state,
        replay_safe=replay_safe,
        requires_explicit_retry=not replay_safe,
    )


def _request_size_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _compress_image_bytes(raw: bytes) -> bytes:
    """用项目已依赖的 ffmpeg 缩小输入图。压缩失败或反而变大时保留原图。"""
    if not raw or not shutil.which("ffmpeg"):
        return raw
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-vf", f"scale={config.MEDIA_INPUT_MAX_EDGE}:{config.MEDIA_INPUT_MAX_EDGE}:force_original_aspect_ratio=decrease",
        "-frames:v", "1", "-q:v", str(config.MEDIA_INPUT_JPEG_QUALITY),
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    try:
        result = subprocess.run(command, input=raw, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return raw
    return result.stdout if result.returncode == 0 and 0 < len(result.stdout) < len(raw) else raw


async def _prepare_image_data_urls(values: list[str]) -> tuple[list[str], dict[str, Any]]:
    prepared: list[str] = []
    original_bytes = 0
    sent_bytes = 0
    compressed_count = 0
    for value in values:
        if not value.startswith("data:") or ";base64," not in value[:100]:
            prepared.append(value)
            continue
        _, encoded = value.split(";base64,", 1)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            prepared.append(value)
            continue
        compressed = await asyncio.to_thread(_compress_image_bytes, raw)
        original_bytes += len(raw)
        sent_bytes += len(compressed)
        if len(compressed) < len(raw):
            compressed_count += 1
            prepared.append("data:image/jpeg;base64," + base64.b64encode(compressed).decode("ascii"))
        else:
            prepared.append(value)
    stats: dict[str, Any] = {
        "media_input_count": len(values),
        "media_input_bytes_original": original_bytes,
        "media_input_bytes_sent": sent_bytes,
        "media_input_compressed_count": compressed_count,
    }
    if original_bytes:
        stats["media_input_compression_ratio"] = round(sent_bytes / original_bytes, 3)
    return prepared, stats


def _structured_failure_from_http_body(body: str) -> ProviderFailure | None:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return provider_failure_from_http_payload(payload)


def _classify_http_error(status: int, body: str, key_name: str = "HIAGENT_API_KEY") -> ProviderError:
    structured_failure = _structured_failure_from_http_body(body)
    explicit_not_accepted = refused = False
    try:
        raw_payload = json.loads(body)
        error_obj = raw_payload.get("error") if isinstance(raw_payload, dict) else None
        failure_payload = (
            error_obj.get("failure") if isinstance(error_obj, dict)
            else raw_payload.get("failure") if isinstance(raw_payload, dict) else None
        )
        explicit_not_accepted = isinstance(failure_payload, dict) and failure_payload.get("create_not_accepted") is True
        # 4xx + 结构化 error 信封（OpenAI 风格）= 网关明确拒绝了这次请求，任务没有被创建，
        # create 不必再按「结果不确定」处理；408（超时）/409（冲突）结果仍不确定，不在此列。
        refused = isinstance(error_obj, dict) and 400 <= status < 500 and status not in (408, 409)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    if structured_failure is not None:
        return ProviderError(
            f"上游请求失败（HTTP {status}）：{body[:300]}",
            raw=body,
            delivery_state="responded",
            create_not_accepted=explicit_not_accepted,
            failure=structured_failure,
        )
    if is_input_image_privacy_rejection(body):
        return ProviderError(
            f"上游请求失败（HTTP {status}）：{body[:300]}", raw=body, delivery_state="responded",
            failure=ProviderFailure.model_rejection(INPUT_IMAGE_PRIVACY_REJECTED_KIND),
        )
    if status in (401, 403):
        return ProviderError(
            f"鉴权失败，请检查 .env 中的 {key_name}（HTTP {status}）：{body[:300]}",
            raw=body,
            failure_kind="authentication",
            delivery_state="responded",
        )
    if status == 429:
        return ProviderError(
            f"网关限流（HTTP 429）：{body[:200]}",
            retryable=True,
            raw=body,
            failure_kind="rate_limited",
            delivery_state="responded",
        )
    if status >= 500:
        return ProviderError(
            f"网关/上游故障（HTTP {status}）：{body[:300]}",
            retryable=True,
            raw=body,
            failure_kind="upstream_unavailable",
            delivery_state="responded",
        )
    return ProviderError(
        f"请求被拒绝（HTTP {status}）：{body[:300]}",
        raw=body, failure_kind="provider_rejected", delivery_state="responded",
        create_not_accepted=refused,
    )


def _header_idempotency_key(operation_id: str) -> str:
    """把 operation_id 投影成能进 HTTP 头的值。

    operation_id 是给人看的业务标识，允许含中文（`character_bible_detail:{项目}:
    {角色名}:{attempt}`）；HTTP 头只能承载可打印 ASCII。直接赋值会在 httpx 编码
    请求头时抛 UnicodeEncodeError，而那发生在请求送出之前、被外层当成一次普通的
    调用失败记下来——2026-08-28 四个项目的人物谱就是这么全线塌掉的：点名照常拿到
    名单，逐个补详情时每个角色的三次尝试全挂在这一行，最后整份人物谱只剩 stub。

    净化只发生在头这一层：库里 provider_calls.operation_id 仍存原值，去重查询也仍
    按原值匹配，可读性与既有记录都不受影响。

    判据取自传输约束本身而非字符集黑名单：能原样进头的（可打印 ASCII 且长度合规）
    逐字返回，保证既有 id 的头值一个字节都不变、不打断供应商侧已建立的去重；其余
    取 sha256，同一 operation_id 恒定映射到同一个键，重试仍然幂等。

    与 `app/minimax_h3.py::_idempotency_key` 是同一口径，前缀不同以便区分来源。
    """
    value = str(operation_id or "").strip()
    if re.fullmatch(r"[!-~]{1,200}", value):
        return value
    return f"oid_{hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()}"


def _headers() -> dict[str, str]:
    if not config.HIAGENT_API_KEY:
        raise ProviderError("未配置 HIAGENT_API_KEY，请在项目根目录 .env 中填写")
    return {"Authorization": f"Bearer {config.HIAGENT_API_KEY}", "Content-Type": "application/json"}


def _deepseek_headers() -> dict[str, str]:
    if not config.DEEPSEEK_API_KEY:
        raise ProviderError("未配置 DEEPSEEK_API_KEY，请在项目根目录 .env 中填写，或在监制房切回其他文本模型")
    return {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}", "Content-Type": "application/json"}


def _zhipu_headers() -> dict[str, str]:
    if not config.ZHIPU_API_KEY:
        raise ProviderError("未配置 ZHIPU_API_KEY，请在项目根目录 .env 中填写，或在监制房切回其他文本模型")
    return {"Authorization": f"Bearer {config.ZHIPU_API_KEY}", "Content-Type": "application/json"}


def active_provider(kind: str) -> str:
    """当前职责选中的模型库条目（provider 就是条目的唯一标识）。

    没有内置 provider 可回落了：模型库是唯一来源。设置里指向的条目不存在或不
    具备该能力时，退而选模型库里第一条具备该能力的条目——这让"刚加完模型还没
    保存分配"也能跑起来；一条都没有时返回空串，由调用方报"未配置模型"。
    """
    from app import model_registry

    configured = (get_setting(f"model_{kind}_provider") or "").strip()
    if configured and model_registry.catalog_item_for_kind(configured, kind):
        return configured
    candidates = model_registry.items_for_kind(kind)
    return str(candidates[0].get("provider") or "").strip() if candidates else ""


def _model_connection(provider: str, model: str, fallback_url: str = "", fallback_key: str = "") -> tuple[str, dict[str, str]]:
    """读取单模型连接信息；旧环境变量仅作为尚未迁移时的兼容兜底。"""
    try:
        custom = json.loads(get_setting("custom_models") or "[]")
    except (TypeError, json.JSONDecodeError):
        custom = []
    item = next((m for m in custom if m.get("provider") == provider and m.get("model") == model), {})
    item_id = item.get("id") or f"builtin:{provider}:{model}"
    try:
        credentials = json.loads(get_setting("model_credentials") or "{}")
    except (TypeError, json.JSONDecodeError):
        credentials = {}
    saved = credentials.get(item_id, {}) if isinstance(credentials, dict) else {}
    base_url = str(saved.get("base_url") or item.get("base_url") or fallback_url).strip().rstrip("/")
    api_key = str(saved.get("api_key") or item.get("api_key") or fallback_key).strip()
    if not base_url:
        raise ProviderError(f"模型 {model} 未配置 Base URL")
    if not api_key:
        raise ProviderError(f"模型 {model} 未配置 API Key，请在模型中心配置")
    return base_url, {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _absolute_provider_url(value: str, base_url: str) -> str:
    """媒体网关有时返回站内相对地址，下载前统一补成完整 URL。"""
    value = (value or "").strip()
    if not value or value.startswith(("http://", "https://", "data:")):
        return value
    return urljoin(base_url.rstrip("/") + "/", value)


def active_model(kind: str, provider: str | None = None) -> str:
    """当前职责选中的模型 ID；只从模型库解析。

    代码里不再内嵌任何模型：模型库是唯一来源，所以选不到就是真的没配，
    返回空串让调用方以"未配置模型"报错，而不是悄悄回落到某个写死的 ID。
    """
    from app import model_registry

    provider = provider or active_provider(kind)
    item = model_registry.catalog_item_for_kind(provider, kind)
    return str((item or {}).get("model") or "").strip()


def _dedupe_models(models: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for model in models:
        model = (model or "").strip()
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered


def _bailian_model_groups(kind: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if kind == "vlm":
        return BAILIAN_VLM_FREE_MODELS, BAILIAN_VLM_BASE_MODELS
    return BAILIAN_TEXT_FREE_MODELS, BAILIAN_TEXT_BASE_MODELS


def _remember_bailian_failure(kind: str, model: str) -> None:
    _, base_models = _bailian_model_groups(kind)
    if model.startswith("qwen3.7") and model not in base_models:
        _BAILIAN_FAILED_MODELS.setdefault(kind, set()).add(model)


def _bailian_fallback_models(kind: str, preferred: str) -> list[str]:
    preferred = (preferred or "").strip()
    free_models, base_models = _bailian_model_groups(kind)
    if preferred and not preferred.startswith("qwen3.7"):
        return [preferred]
    candidates = _dedupe_models([preferred, *free_models, *base_models])
    failed = _BAILIAN_FAILED_MODELS.setdefault(kind, set())
    return [model for model in candidates if model not in failed or model in base_models]


def _chat_content(data: dict, *, label: str = "chat") -> str:
    """从 OpenAI 兼容响应取 message.content（推理字段一律丢弃）。
    个别 provider 经 OpenRouter 返回 content 为分块列表，这里兜底拼接其中的文本块。"""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"{label} 响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return content or ""


def _reasoning_used_all_output_budget(data: dict) -> bool:
    """判断推理模型是否在生成正文前已用完输出预算。

    OpenRouter 的 reasoning 与 message.content 共用 max_tokens。部分模型会在
    finish_reason=length 时只返回 reasoning、将 content 留为 null。
    """
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        return False
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    return choice.get("finish_reason") == "length" and bool(reasoning)


def _empty_content_detail(data: dict) -> str:
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError):
        return "响应结构中无可用 choice"
    finish_reason = choice.get("finish_reason") or "unknown"
    reasoning_present = bool(message.get("reasoning") or message.get("reasoning_content"))
    usage = data.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    return (f"finish_reason={finish_reason}, reasoning_present={reasoning_present}, "
            f"completion_tokens={completion_tokens}")


def _reject_truncated_chat_response(data: dict) -> None:
    try:
        finish_reason = data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return
    if finish_reason != "length":
        return
    detail = _empty_content_detail(data)
    raise ProviderError(
        f"模型输出因响应 token 预算耗尽而截断（{detail}）",
        retryable=False,
        raw=detail,
        failure_kind=ProviderFailureKind.OUTPUT_TRUNCATED,
        delivery_state="responded",
        replay_safe=False,
    )


def _content_delivery_absent(data: dict) -> bool:
    """Whether an OpenAI 兼容响应完全没有交付证据（既非答案也非可诊断的拒答）。

    一次真正跑完的补全，无论模型说了什么，最终 chunk 都会盖上一个终态
    ``finish_reason``（stop/length/content_filter/tool_calls/...），并且
    ``usage.completion_tokens`` 会如实反映它花掉的预算——即便答案是"什么都
    不说"。当这两个证据同时缺席（没有 finish_reason，且已入账的
    completion_tokens 为 0 或未上报）、content 又为空时，供应商没有对这次请求
    做出任何决定：这与 ``_stream_chat_completion`` 里"流在 [DONE] 前中断"
    （见 ``provider_answer_undelivered``）是同一种情况，只是这次流最终还是吐出
    了 ``[DONE]``，因此被记成了正常的 200。既然没有答案可挑选、也没有判断可
    保留，原样重放这份确定性请求一次是安全的——与 ``_reject_truncated_chat_response``
    对 ``OUTPUT_TRUNCATED`` 的论证同构，只是这里对应的是生成中途夭折，而不是
    预算耗尽。

    反之，只要 finish_reason 或 completion_tokens 任一项证明供应商确实跑完了
    这次请求（哪怕答案就是空字符串），就不属于这里——那是一次交付了的坏答案，
    必须继续 fail-closed，不能被这条重放豁免。
    """
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return False
    if choice.get("finish_reason"):
        return False
    usage = data.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    return not (isinstance(completion_tokens, int) and completion_tokens > 0)


def _empty_chat_content_error(data: dict) -> ProviderError:
    """构造"content 为空"的分类化 ProviderError（供 ``chat()`` 两条 provider 分支复用）。

    判定逻辑统一走 ``_content_delivery_absent``，两处不再各写一套、避免以后
    分叉出不一致的重试资格。
    """
    detail = _empty_content_detail(data)
    message = f"模型返回空内容（content 为空；{detail}）"
    if _content_delivery_absent(data):
        return ProviderError(
            message,
            raw=detail,
            failure_kind=ProviderFailureKind.OUTPUT_MISSING,
            delivery_state="responded",
            replay_safe=True,
        )
    return ProviderError(message)


def _notify_completion_usage(
    data: dict,
    usage_callback: Callable[[dict[str, Any]], None] | None,
    *,
    reused: bool,
) -> None:
    if usage_callback is None:
        return
    usage = data.get("usage") if isinstance(data, dict) else None
    completion_tokens = (
        usage.get("completion_tokens")
        if isinstance(usage, dict)
        else None
    )
    usage_callback({
        "completion_tokens": (
            completion_tokens
            if isinstance(completion_tokens, int) and completion_tokens >= 0
            else None
        ),
        "reused": reused,
    })


def _infer_callsite_meta() -> dict[str, Any]:
    frame = inspect.currentframe()
    try:
        current = frame.f_back if frame else None
        while current:
            module = str(current.f_globals.get("__name__") or "").strip()
            if module and module != __name__:
                file_path = str(current.f_code.co_filename or "")
                rel_file = file_path
                marker = "/app/"
                if marker in file_path:
                    rel_file = "app/" + file_path.split(marker, 1)[1]
                return {
                    "caller_module": module,
                    "caller_function": current.f_code.co_name,
                    "caller_file": rel_file,
                    "caller_line": current.f_lineno,
                }
            current = current.f_back
    finally:
        del frame
    return {}


def _merge_call_meta(meta: dict | None) -> dict | None:
    merged = dict(meta or {})
    inferred = _infer_callsite_meta()
    for key, value in inferred.items():
        merged.setdefault(key, value)
    if merged.get("caller_module") and merged.get("caller_function"):
        merged.setdefault("initiator", f"{merged['caller_module']}.{merged['caller_function']}")
    return merged or None


async def _post_json(client: httpx.AsyncClient, url: str, payload: dict, *,
                     kind: str, model: str, retries: int = 2,
                     headers: dict | None = None, key_name: str = "HIAGENT_API_KEY",
                     meta: dict | None = None,
                     idempotency_key: str | None = None,
                     preserve_exact_request: bool = False) -> dict:
    last_err: ProviderError | None = None
    merged_meta = _merge_call_meta(meta)
    req_headers = dict(headers if headers is not None else _headers())
    if idempotency_key:
        # Providers that implement the conventional header deduplicate the
        # accept-before-local-commit crash window; providers that ignore it still
        # receive the same durable operation identifier in observability metadata.
        req_headers["Idempotency-Key"] = _header_idempotency_key(idempotency_key)
    request_bytes = _request_size_bytes(payload)
    harness_text_request = bool(
        kind == "chat"
        and (merged_meta or {}).get("gateway") == "execution_harness"
    )
    for attempt in range(retries + 1):
        start = time.time()
        attempt_meta = {
            "http_attempt": attempt + 1,
            "http_attempts_max": retries + 1,
            "request_bytes": request_bytes,
            **(merged_meta or {}),
        }
        call_id = start_provider_call(kind, model, meta=attempt_meta, request_json=payload)
        if preserve_exact_request:
            update_provider_call_request(call_id, payload, preserve_exact=True)
        try:
            resp = await client.post(url, json=payload, headers=req_headers)
            latency = int((time.time() - start) * 1000)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    snippet = (resp.text or "")[:500]
                    err = ProviderError(
                        f"上游返回非法 JSON：{exc}",
                        retryable=True,
                        raw=snippet,
                        failure_kind="malformed_response",
                        delivery_state="responded",
                    )
                    finish_provider_call(
                        call_id, "FAILED", 200, latency, error=str(err),
                        response_json={"status_code": 200, "body": snippet},
                    )
                    last_err = err
                    raise err
                finish_provider_call(call_id, "OK", 200, latency, response_json=data)
                return data
            err = _classify_http_error(resp.status_code, resp.text, key_name)
            finish_provider_call(
                call_id, "FAILED", resp.status_code, latency, error=str(err),
                response_json={"status_code": resp.status_code, "body": resp.text})
            if not err.retryable:
                raise err
            last_err = err
            # HTTP 错误证明请求已经到达对端。即使错误本身可恢复，也不在
            # adapter/Harness 内静默创建第二次文本生成。
            if harness_text_request or not err.replay_safe:
                raise err
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            latency = int((time.time() - start) * 1000)
            phase = _timeout_phase(exc)
            diag = _timeout_diagnostic_label(client, merged_meta, kind)
            detail = (f"{type(exc).__name__}(phase={phase}, latency_ms={latency}, "
                      f"request_bytes={request_bytes}, {diag}): {exc!r}")
            last_err = _transport_provider_error(
                exc,
                f"调用{phase}阶段超时（{latency}ms，请求 {request_bytes} bytes，{diag}）",
                raw=detail,
                timeout_phase=phase,
            )
            finish_provider_call(
                call_id,
                "TIMEOUT" if last_err.replay_safe else "INTERRUPTED",
                None,
                latency,
                error=detail,
            )
            if harness_text_request or not last_err.replay_safe:
                raise last_err
        except httpx.HTTPError as exc:
            latency = int((time.time() - start) * 1000)
            detail = f"{type(exc).__name__}(latency_ms={latency}): {exc!r}"
            last_err = _transport_provider_error(
                exc,
                f"网络错误：{exc}",
                raw=detail,
            )
            finish_provider_call(
                call_id,
                "NETWORK_ERROR" if last_err.replay_safe else "INTERRUPTED",
                None,
                latency,
                error=detail,
            )
            if harness_text_request or not last_err.replay_safe:
                raise last_err
        except Exception as exc:
            latency = int((time.time() - start) * 1000)
            finish_provider_call(call_id, "FAILED", None, latency, error=str(exc))
            raise
        if attempt < retries:
            await asyncio.sleep(1.5 * (2 ** attempt))
    assert last_err is not None
    raise last_err


async def _post_bailian_chat_with_fallback(client: httpx.AsyncClient, payload: dict, *,
                                           fallback_kind: str, log_kind: str,
                                           preferred_model: str,
                                           meta: dict | None = None) -> tuple[dict, str, bool]:
    base_url, headers = _model_connection("bailian", preferred_model, config.BAILIAN_BASE_URL, config.BAILIAN_API_KEY)
    url = f"{base_url}/chat/completions"
    models = _bailian_fallback_models(fallback_kind, preferred_model)
    strict_replay = bool(
        (meta or {}).get("require_cached_successful_operation")
    )
    if strict_replay:
        for candidate in models:
            attempt_payload = {**payload, "model": candidate}
            data = _cached_successful_provider_response(
                log_kind, candidate, attempt_payload, meta,
            )
            if data is not None:
                return data, candidate, True
        _require_cached_replay_or_raise(None, meta)
        raise AssertionError("strict replay gate must raise")
    if bool((meta or {}).get("disable_provider_candidate_fallback")):
        models = [preferred_model]
    errors: list[str] = []
    last_err: ProviderError | None = None
    for candidate in models:
        attempt_payload = {**payload, "model": candidate}
        try:
            data = _cached_successful_provider_response(
                log_kind, candidate, attempt_payload, meta,
            )
            reused = data is not None
            if data is None:
                data = await _post_json(
                    client, url, attempt_payload, kind=log_kind, model=candidate,
                    headers=headers, key_name="BAILIAN_API_KEY", meta=meta,
                )
            return data, candidate, reused
        except ProviderError as exc:
            if not exc.replay_safe:
                raise
            _remember_bailian_failure(fallback_kind, candidate)
            last_err = exc
            errors.append(f"{candidate}: {exc}")
    detail = "；".join(errors)[:500]
    if last_err is None:
        raise ProviderError("百炼模型候选列表为空，请检查模型配置")
    raise ProviderError(f"百炼 {fallback_kind} 模型全部请求失败，已按降级序列尝试：{detail}",
                        retryable=last_err.retryable, raw=last_err.raw,
                        timeout_phase=last_err.timeout_phase,
                        failure_kind=last_err.failure_kind,
                        delivery_state=last_err.delivery_state,
                        replay_safe=last_err.replay_safe,
                        requires_explicit_retry=last_err.requires_explicit_retry)


async def _stream_bailian_chat_with_fallback(
    client: httpx.AsyncClient, payload: dict, *, fallback_kind: str, log_kind: str,
    preferred_model: str, meta: dict | None,
    on_token: Callable[[str, str], None],
) -> tuple[dict, str]:
    """百炼的逐模型流式降级链。

    只有连接阶段能证明请求未送达时才可尝试下一个模型；尚未收到 token
    不能证明上游没有开始生成。
    """
    base_url, headers = _model_connection(
        "bailian", preferred_model, config.BAILIAN_BASE_URL, config.BAILIAN_API_KEY)
    url = f"{base_url}/chat/completions"
    models = _bailian_fallback_models(fallback_kind, preferred_model)
    errors: list[str] = []
    last_err: ProviderError | None = None
    emitted = 0

    def _forward(kind: str, text: str) -> None:
        nonlocal emitted
        emitted += 1
        on_token(kind, text)

    for candidate in models:
        attempt_payload = {**payload, "model": candidate}
        try:
            data = await _stream_or_fallback(
                client, url, attempt_payload, kind=log_kind, model=candidate,
                headers=headers, key_name="BAILIAN_API_KEY", meta=meta,
                on_token=_forward,
            )
            return data, candidate
        except ProviderError as exc:
            if emitted or not exc.replay_safe:
                raise
            _remember_bailian_failure(fallback_kind, candidate)
            last_err = exc
            errors.append(f"{candidate}: {exc}")
    detail = "；".join(errors)[:500]
    if last_err is None:
        raise ProviderError("百炼模型候选列表为空，请检查模型配置")
    raise ProviderError(
        f"百炼 {fallback_kind} 模型全部请求失败，已按降级序列尝试：{detail}",
        retryable=last_err.retryable, raw=last_err.raw,
        timeout_phase=last_err.timeout_phase,
        failure_kind=last_err.failure_kind,
        delivery_state=last_err.delivery_state,
        replay_safe=last_err.replay_safe,
        requires_explicit_retry=last_err.requires_explicit_retry,
    )


async def _chat_with_reasoning_fallback(client: httpx.AsyncClient, url: str, payload: dict, *,
                                     kind: str, model: str, headers: dict | None, key_name: str,
                                     temperature: float, call_meta: dict | None = None,
                                     usage_callback: Callable[[dict[str, Any]], None] | None = None) -> str:
    """封装推理模型的降级重试逻辑：若首轮因推理过长导致 content 为空，则关闭推理重试一次。"""
    data = _cached_successful_provider_response(kind, model, payload, call_meta)
    _require_cached_replay_or_raise(data, call_meta, kind=kind, model=model, payload=payload)
    reused = data is not None
    if data is None:
        data = await _plain_chat_request(
            client, url, payload, kind=kind, model=model,
            headers=headers, key_name=key_name, meta=call_meta,
        )
    content = _chat_content(data, label=kind)
    _notify_completion_usage(data, usage_callback, reused=reused)
    if not content.strip() and _reasoning_used_all_output_budget(data):
        if bool((call_meta or {}).get("disable_reasoning_fallback")):
            raise ProviderError(
                "模型推理耗尽输出预算，该业务操作禁止隐式第二次付费请求",
                raw=_empty_content_detail(data),
                failure_kind=ProviderFailureKind.OUTPUT_TRUNCATED,
                delivery_state="responded",
                replay_safe=False,
            )
        # 思考过长时重试；移除 OpenRouter reasoning 参数，使用普通生成。
        fallback_payload = {**payload, "temperature": temperature}
        # 移除 OpenRouter 风格的 reasoning 参数
        fallback_payload.pop("reasoning", None)
        if thinking_disabled(call_meta):
            fallback_payload["thinking"] = {"type": "disabled"}
        # DeepSeek/智谱等路径原请求可能根本没有 reasoning 字段。此时所谓降级请求
        # 与首轮逐字相同，重复发送只会再次耗尽预算并产生费用，必须直接结束。
        if fallback_payload == payload:
            raise ProviderError(
                f"模型推理耗尽输出预算，且当前接口不支持关闭推理（{_empty_content_detail(data)}）"
            )
        fallback_meta = {
            **(call_meta or {}),
            "reasoning_fallback": True,
            "reasoning_fallback_cause": "reasoning_budget_exhausted",
        }
        data = await _post_json(client, url, fallback_payload, kind=kind, model=model, retries=0,
                                headers=headers, key_name=key_name, meta=fallback_meta)
        _notify_completion_usage(data, usage_callback, reused=False)
        content = _chat_content(data, label=f"{kind} reasoning fallback")
    _reject_truncated_chat_response(data)
    if not content.strip():
        raise _empty_chat_content_error(data)
    return content


def provider_answer_undelivered(exc: object) -> bool:
    """Whether a failed provider call left no authored answer to preserve.

    Two shapes qualify, and neither is an answer being re-rolled:

    * a read/total timeout before a single streamed character -- the provider
      never began answering; and
    * a stream that ended without ``[DONE]``.  That marker is the provider's
      own completion signal, and :func:`_stream_chat_completion` discards the
      partial reconstruction outright, so nothing survives no matter how many
      characters arrived.  In production one such cut delivered 22 characters
      and still ended the whole episode.

    Every other failure class -- including a delivered answer that failed
    validation -- is excluded, so the strict one-call rules stay intact.
    """
    kind = str(getattr(exc, "failure_kind", "") or "")
    if kind == "stream_interrupted":
        return True
    if kind != "request_outcome_unknown":
        return False
    try:
        return int(getattr(exc, "received_chars", 0) or 0) == 0
    except (TypeError, ValueError):
        return False


def deterministic_undelivered_error(
    last_error: "ProviderError", *, attempts: int,
) -> "ProviderError":
    """Re-label an undelivered outcome that survived a backed-off retry.

    A single transport stall is transient, so "结果不确定，稍后重试" is the right
    thing to say once.  Surviving several attempts spread across a backoff
    window means something else: measured on this pipeline the provider sheds
    load at the batch's peak concurrency and answers the shed requests with a
    canned refusal, so the useful lever is less concurrency rather than another
    immediate retry.  Whatever bounded evidence the transport captured travels
    with the message so the actual cause stays visible.
    """
    evidence = str(getattr(last_error, "raw", "") or "").strip()
    return ProviderError(
        f"供应商连续 {attempts} 次在退避重试后仍未送达答案；"
        "通常是并发高峰下的限流丢弃，请降低并发或稍后整体重跑，"
        "对同一请求立即重试无效"
        + (f"（{evidence[:200]}）" if evidence else ""),
        retryable=False,
        raw=evidence,
        failure_kind="deterministic_rejection",
        delivery_state="responded",
        requires_explicit_retry=False,
        received_chars=int(getattr(last_error, "received_chars", 0) or 0),
    )


def _chat_read_timeout_s(call_meta: dict | None) -> float:
    """为长结构化生成使用独立读超时，其他文本请求保持通用上限。"""
    stage_key = str((call_meta or {}).get("stage_key") or "").strip().lower()
    stage = str((call_meta or {}).get("stage") or "").strip().lower()
    if stage_key == "screenplay_scene_shards":
        return max(
            config.TIMEOUT_CHAT_READ,
            config.TIMEOUT_CHAT_SCENE_SHARD_READ,
        )
    if stage_key in {
        "screenplay_scene_shard_semantic_review",
        "screenplay_scene_shard_semantic_repair",
    }:
        return max(config.TIMEOUT_CHAT_READ, config.TIMEOUT_CHAT_BASELINE_READ)
    if stage_key == "storyboard_outline":
        return max(
            config.TIMEOUT_CHAT_READ,
            config.TIMEOUT_CHAT_STORYBOARD_OUTLINE_READ,
        )
    if stage_key.startswith("storyboard_pack_"):
        # 整个分镜包一族共用一个上限：按前缀匹配而不是逐个列 key，新增分段阶段
        # 时不会再像 storyboard_pack_segment 那样静默掉回通用 300s。
        return max(
            config.TIMEOUT_CHAT_READ,
            config.TIMEOUT_CHAT_STORYBOARD_PACK_READ,
        )
    if stage_key == "storyboard" or stage_key.startswith("storyboard_shot_"):
        return max(config.TIMEOUT_CHAT_READ, config.TIMEOUT_CHAT_BASELINE_READ)
    if stage_key == "scene_bible" or stage_key.startswith("scene_bible_"):
        # 同样按前缀收整族，将来拆分片时不会静默掉回通用 300s。
        return max(
            config.TIMEOUT_CHAT_READ,
            config.TIMEOUT_CHAT_SCENE_BIBLE_READ,
        )
    if stage_key == "screenplay_character_discovery":
        # Same reasoning as the blueprint shard below, and sharper: the identity
        # contracts forbid an automatic retry, so a stalled call does not just
        # waste the wait -- it ends the episode. Expose the stall early instead.
        return config.TIMEOUT_CHAT_IDENTITY_READ
    if stage_key == "screenplay_blueprint_shard":
        # Deliberately not max()-ed against the generic ceiling: this stage is
        # short enough that the generic 300s only delays a stall, and a stalled
        # shard costs an explicit Production Grant on top of the dead wait.
        return config.TIMEOUT_CHAT_BLUEPRINT_SHARD_READ
    if stage_key == "screenplay_blueprint_review":
        return max(
            config.TIMEOUT_CHAT_READ,
            config.TIMEOUT_CHAT_BLUEPRINT_REVIEW_READ,
        )
    if stage_key == "screenplay_source_paratext":
        # Deliberately not max()-ed against the generic ceiling, same reasoning
        # as the blueprint shard above: this call is normally fast (see
        # config.TIMEOUT_CHAT_PARATEXT_READ's derivation), so sharing the
        # generic 300s only turns a real stall into a longer dead wait instead
        # of catching it sooner.
        return config.TIMEOUT_CHAT_PARATEXT_READ
    if stage_key == "episode_prep_pack_event_chain":
        # Opposite failure mode from paratext: this stage's own healthy calls
        # routinely approach the generic 300s ceiling (see
        # config.TIMEOUT_CHAT_EVENT_CHAIN_READ's derivation), so sharing it
        # risks cutting off calls that were going to succeed. max()-ed so an
        # operator raising the generic floor never accidentally tightens this.
        return max(config.TIMEOUT_CHAT_READ, config.TIMEOUT_CHAT_EVENT_CHAIN_READ)
    if stage == "episode_video_mode_plan":
        return max(
            config.TIMEOUT_CHAT_READ,
            config.TIMEOUT_CHAT_VIDEO_PLAN_READ,
        )
    if (
        "baseline" in stage
        or stage == "screenplay_narrative_patch"
        or stage_key == "narrative_graph_patch"
    ):
        return max(config.TIMEOUT_CHAT_READ, config.TIMEOUT_CHAT_BASELINE_READ)
    return config.TIMEOUT_CHAT_READ


def _plain_chat_streaming_enabled(call_meta: dict | None) -> bool:
    """业务文本长请求优先流式读取，避免代理等待完整响应时断开空闲连接。"""
    return bool((call_meta or {}).get("gateway") == "execution_harness")


def reasoning_token_reserve(*, model: str | None) -> int:
    """How much of one completion budget a thinking model spends before answering.

    Every business stage sizes ``max_tokens`` from the answer it expects.  The
    provider, however, charges reasoning tokens against the *same* completion
    budget (see ``_reject_truncated_chat_response``), and for this model class
    reasoning is the overwhelming majority of it.  The reserve therefore belongs
    to the provider/model, not to any single call site, and is applied once at
    the only place that turns a business budget into a provider ``max_tokens``.

    ``model`` 必传（可以显式传 ``None`` 表示这条路径确实不知道打给谁）。这个
    预留的正确值完全取决于是哪个模型在跑——火山 seed 从不思考、``glm-5.3-
    flash`` 能思考 30839 token——所以让调用方「不写就默认」等于把最容易判错的
    参数悄悄糊过去。漏传现在是 ``TypeError``，在测试里就拦住。

    优先级：运维显式覆盖 > 该模型的观测画像 > 全局默认。观测画像见
    ``app.model_runtime_profile``；样本不足时它返回 ``None``，此处回落到全局
    默认，绝不把「没观测到」当成「不需要预留」——但反过来，「足量样本里次次
    观测到 0」是确凿的否定证据，那时预留就是 0。模型自身的
    ``max_output_tokens`` 始终夹紧结果。
    """
    override = (get_setting("text_reasoning_token_reserve") or "").strip()
    if override:
        try:
            return max(0, int(override))
        except ValueError:
            pass
    from app.model_runtime_profile import model_runtime_profile

    observed = model_runtime_profile(model).reasoning_ceiling
    if observed is not None:
        if observed == 0:
            # 供应商在足量样本里**逐次**回报「本次思考了 0 个 token」，这是对
            # 「它会思考」的否定证据，不是字段缺失（缺失时 json_extract 给
            # NULL，样本根本进不了统计）。火山 seed 2799 次调用无一例外，再给
            # 它留 16384 就是把它 32768 输出上限的一半直接扔掉。
            return 0
        # 观测到它确实会思考、只是近期任务偏轻时，全局默认转为下限：样本没覆盖
        # 到重任务不等于重任务不会来，此时收紧预留会把风险留给下一次重任务。
        return max(int(observed), int(config.TEXT_REASONING_TOKEN_RESERVE))
    return max(0, int(config.TEXT_REASONING_TOKEN_RESERVE))


def thinking_disabled(call_meta: dict | None) -> bool:
    """Whether this call must not spend a thinking/reasoning budget.

    Short JSON gates (人物点名、身份归一、在场裁决) empirically waste 6k–8k
    reasoning tokens to emit 1–2KB of JSON. The flag is opt-in per call.
    """
    return bool((call_meta or {}).get("disable_thinking"))


def text_reasoning_effort(call_meta: dict | None) -> str:
    """本次调用希望模型思考到什么程度；空串表示不表态，沿用模型自身默认。

    智谱 GLM-5.2 起提供 ``reasoning_effort``（low/high/max，默认 max），是这类
    「强制思考、关不掉」的模型上唯一能压住思考开销的旋钮——GLM-5.3 的
    ``thinking.type`` 只接受 ``enabled``，官方迁移指引给出的 disabled 替代方案
    正是 ``enabled`` 配 ``reasoning_effort="low"``。

    取值原样透传，不做白名单校验：档位是供应商定义的开放集合（GLM-5.2 时期有
    7 档，5.3 收成 3 档），穷举一份枚举只会在它下次改档位时误伤合法值；真填错
    了供应商会自己拒绝，或按它文档写的回落到默认档。

    优先级：调用点声明 > 运维设置 > 环境默认。调用点优先是因为「这一步要想多
    深」是任务的性质，不是全局口味——短 JSON 判定和整集分镜创作不该共用一档。
    """
    meta_value = (call_meta or {}).get("reasoning_effort")
    if meta_value is not None:
        return str(meta_value).strip()
    override = (get_setting("text_reasoning_effort") or "").strip()
    if override:
        return override
    return config.TEXT_REASONING_EFFORT


def _first_token_timeout_s(call_meta: dict | None) -> float | None:
    """Deadline for the first streamed content/reasoning character.

    Explicit ``first_token_timeout_s`` wins; ``disable_thinking`` implies the
    shared short-JSON default. ``0`` or a non-numeric value turns the cap off.
    """
    meta = call_meta or {}
    if "first_token_timeout_s" in meta:
        try:
            value = float(meta["first_token_timeout_s"])
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    if thinking_disabled(meta):
        return float(config.TIMEOUT_CHAT_FIRST_TOKEN_S)
    return None


def text_request_token_limits(
    *,
    requested_max_tokens: int,
    provider: str | None = None,
    model: str | None = None,
    disable_thinking: bool = False,
) -> tuple[str, str, int]:
    selected_provider = provider or active_provider("text")
    selected_model = model or active_model("text", selected_provider)
    limits = active_model_token_limits(
        selected_provider,
        selected_model,
        get_setting,
    )
    answer_budget = max(1, int(requested_max_tokens))
    model_cap = int(limits["max_output_tokens"])
    if disable_thinking:
        # The thinking reserve is an invitation to think. Short JSON calls that
        # already opted out must not send a 16k-token thinking budget.
        effective = min(answer_budget, model_cap)
    else:
        # ``requested_max_tokens`` is an *answer* budget.  Add the model's thinking
        # reserve so a correctly-sized answer budget cannot be truncated by the
        # reasoning that precedes it, then clamp to what the model can emit at all.
        effective = min(
            answer_budget + reasoning_token_reserve(model=selected_model), model_cap
        )
    return selected_provider, selected_model, effective


def text_request_semantic_settings(provider: str) -> dict[str, Any]:
    from app import text_providers

    if text_providers.protocol_for_provider(provider) == "openrouter":
        effort = (
            config.OPENROUTER_TEXT_REASONING_EFFORT or ""
        ).strip().lower()
        return {
            "reasoning_effort": effort,
            "uses_temperature": not effort or effort == "none",
        }
    return {"uses_temperature": True}


async def _plain_chat_request(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    *,
    kind: str,
    model: str,
    headers: dict | None,
    key_name: str,
    meta: dict | None,
) -> dict:
    if _plain_chat_streaming_enabled(meta):
        return await _stream_or_fallback(
            client,
            url,
            payload,
            kind=kind,
            model=model,
            headers=headers,
            key_name=key_name,
            meta=meta,
            on_token=lambda _kind, _text: None,
        )
    return await _post_json(
        client,
        url,
        payload,
        kind=kind,
        model=model,
        headers=headers,
        key_name=key_name,
        meta=meta,
    )


# response_format_required=True 调用命中"看起来是 response_format 不支持"的 400 时，
# 在原地重放同一份请求（不降级）的次数上限。见 chat() 内 response_format_required 分支
# 的注释：真实实验显示这类 400 是约 3.8% 的独立背景噪声，与请求内容无关，重放大概率
# 会成功；这不是一个"猜的"数字，是"建议 2 次"的既定结论，超出后老实抛错。
_RESPONSE_FORMAT_REQUIRED_JSON_SCHEMA_RETRIES = 2


async def chat(messages: list[dict], *, model: str | None = None, provider: str | None = None,
               temperature: float = 0.7,
               max_tokens: int = 65535, call_meta: dict | None = None,
               usage_callback: Callable[[dict[str, Any]], None] | None = None,
               response_format: dict[str, Any] | None = None) -> str:
    """文本 LLM 对话，返回 message.content（推理模型的 reasoning 一律丢弃）。
    按设置在火山 HiAgent、OpenRouter、阿里云百炼、DeepSeek、智谱官方 API 之间路由（后两者仅文本，
    图像/视频始终走火山）。

    response_format 用于让网关在生成时就约束输出为合法 JSON（json_object / json_schema）。
    这些供应商都实现 OpenAI 兼容协议，普遍支持该字段；若某 provider/model 以客户端错误
    明确拒绝该字段，普通调用会记为不支持并去掉该字段重试一次。声明
    ``response_format_required`` 的权威调用则原样失败，绝不降级结构化约束。

    ``provider`` 显式指定时覆盖 ``active_provider("text")``（世界书/映射台/分镜台
    的分环节模型选择用它连带换对连接，而不是只换模型 ID——同一 provider 下的
    base_url/api_key/协议才是一致的；不传时行为与此前完全一致。"""
    timeout = httpx.Timeout(connect=10, read=_chat_read_timeout_s(call_meta), write=30, pool=10)
    disable_thinking = thinking_disabled(call_meta)
    provider, selected_model, effective_max_tokens = text_request_token_limits(
        requested_max_tokens=max_tokens,
        provider=provider,
        model=model,
        disable_thinking=disable_thinking,
    )
    token_limits = active_model_token_limits(provider, selected_model, get_setting)
    requested_max_tokens = max(1, int(max_tokens))
    runtime_output_limit = int(token_limits["max_output_tokens"])
    max_tokens = effective_max_tokens
    call_meta = {
        **(call_meta or {}),
        "model_context_window_tokens": int(token_limits["context_window_tokens"]),
        "model_max_output_tokens": int(token_limits["max_output_tokens"]),
        "runtime_output_limit_tokens": runtime_output_limit,
        "token_limits_source": token_limits["token_limits_source"],
        "requested_max_tokens": requested_max_tokens,
        "effective_max_tokens": max_tokens,
    }

    async def _dispatch(attempt_response_format: dict[str, Any] | None) -> tuple[str, dict]:
        """执行一次真实的 provider 调用。attempt_response_format 非空时注入到 payload，
        让网关在生成阶段就约束合法 JSON；返回 (content, data) 供空内容兜底。"""
        def _with_rf(payload: dict[str, Any]) -> dict[str, Any]:
            if attempt_response_format is not None:
                payload["messages"] = _messages_for_response_format(
                    payload["messages"], attempt_response_format,
                )
                payload["response_format"] = attempt_response_format
            return payload

        from app import text_providers

        protocol = text_providers.protocol_for_provider(provider)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if protocol == "openrouter":
                or_model = selected_model
                base_url, model_headers = _model_connection(provider, or_model, config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY)
                payload: dict[str, Any] = {"model": or_model, "messages": messages, "max_tokens": max_tokens}
                effort = (config.OPENROUTER_TEXT_REASONING_EFFORT or "").strip().lower()
                if disable_thinking or not effort or effort == "none":
                    payload["temperature"] = temperature
                else:
                    payload["reasoning"] = {"effort": effort}
                _with_rf(payload)
                content = await _chat_with_reasoning_fallback(
                    client, f"{base_url}/chat/completions", payload,
                    kind="chat", model=or_model, headers=model_headers,
                    key_name="OPENROUTER_API_KEY", temperature=temperature, call_meta=call_meta,
                    usage_callback=usage_callback)
                data = {}
            elif protocol == "bailian":
                bailian_model = selected_model
                payload = _with_rf({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
                data, _, reused = await _post_bailian_chat_with_fallback(
                    client, payload, fallback_kind="text", log_kind="chat",
                    preferred_model=bailian_model, meta=call_meta)
                _notify_completion_usage(data, usage_callback, reused=reused)
                _reject_truncated_chat_response(data)
                content = _chat_content(data, label="chat")
            elif protocol == "deepseek":
                deepseek_model = selected_model
                try:
                    base_url, model_headers = _model_connection(provider, deepseek_model, config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY)
                except ProviderError:
                    base_url, model_headers = config.DEEPSEEK_BASE_URL, _deepseek_headers()
                payload = _with_rf({"model": deepseek_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens})
                if disable_thinking:
                    payload["thinking"] = {"type": "disabled"}
                content = await _chat_with_reasoning_fallback(
                    client, f"{base_url}/chat/completions", payload,
                    kind="chat", model=deepseek_model, headers=model_headers,
                    key_name="DEEPSEEK_API_KEY", temperature=temperature, call_meta=call_meta,
                    usage_callback=usage_callback)
                data = {}
            elif protocol == "zhipu":
                zhipu_model = selected_model
                try:
                    base_url, model_headers = _model_connection(provider, zhipu_model, config.ZHIPU_BASE_URL, config.ZHIPU_API_KEY)
                except ProviderError:
                    base_url, model_headers = config.ZHIPU_BASE_URL, _zhipu_headers()
                payload = _with_rf({"model": zhipu_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens})
                if disable_thinking:
                    payload["thinking"] = {"type": "disabled"}
                effort = text_reasoning_effort(call_meta)
                if effort:
                    payload["reasoning_effort"] = effort
                content = await _chat_with_reasoning_fallback(
                    client, f"{base_url}/chat/completions", payload,
                    kind="chat", model=zhipu_model, headers=model_headers,
                    key_name="ZHIPU_API_KEY", temperature=temperature, call_meta=call_meta,
                    usage_callback=usage_callback)
                data = {}
            else:
                custom_model = selected_model
                base_url, headers = _model_connection(
                    provider, custom_model,
                    config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY,
                )
                payload = _with_rf({"model": custom_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens})
                data = _cached_successful_provider_response(
                    "chat", custom_model, payload, call_meta,
                )
                _require_cached_replay_or_raise(
                    data, call_meta, kind="chat", model=custom_model, payload=payload,
                )
                reused = data is not None
                if data is None:
                    data = await _plain_chat_request(
                        client,
                        f"{base_url}/chat/completions",
                        payload,
                        kind="chat",
                        model=custom_model,
                        headers=headers,
                        key_name=f"model:{custom_model}",
                        meta=call_meta,
                    )
                _notify_completion_usage(data, usage_callback, reused=reused)
                _reject_truncated_chat_response(data)
                content = _chat_content(data, label="chat")
        return content, data

    response_format_required = bool(
        (call_meta or {}).get("response_format_required")
    )
    if response_format_required and response_format is None:
        raise ValueError(
            "response_format_required needs an explicit response_format"
        )
    attempt_response_format = (
        response_format
        if (
            response_format
            and (
                response_format_required
                or not _response_format_known_unsupported(
                    provider,
                    selected_model,
                )
            )
        )
        else None
    )
    if (
        attempt_response_format is not None
        and not response_format_required
        and _json_schema_known_unsupported(provider, selected_model)
    ):
        # 该缓存只是"跳过一次大概率会 400 的往返"的性能优化，永远不能替调用方
        # 决定放弃它显式要求的强约束——required 调用必须每次都真枪实弹地发出
        # 原始 response_format，即便这个 (provider, model) 之前被记过不支持。
        attempt_response_format = (
            _json_object_response_format(attempt_response_format)
            or attempt_response_format
        )
    truncation_escalated = False
    undelivered_retry_used = False
    required_format_attempt = 0
    while True:
        try:
            content, data = await _dispatch(attempt_response_format)
            if not content or not content.strip():
                # 判定与重放资格统一在 _empty_chat_content_error /
                # _content_delivery_absent 里做出；这里只负责把"content 为空"
                # 从一次表面成功的 _dispatch 里揪出来，交给下面的 except 分支
                # 决定是重放还是照常 fail-closed，绝不能在这一步就把它当成
                # json_schema 的真实成功去清空拒绝计数。
                raise _empty_chat_content_error(data)
            if (
                attempt_response_format is not None
                and attempt_response_format.get("type") == "json_schema"
            ):
                # 拿到一次真实成功，证明该 (provider, model) 当下能承接 json_schema，
                # 清空"连续拒绝"计数，避免几次分散的背景噪声被累积计入拉黑阈值。
                _forget_json_schema_reject_streak(provider, selected_model)
            break
        except ProviderError as exc:
            if (
                exc.failure_kind == ProviderFailureKind.OUTPUT_TRUNCATED.value
                and not truncation_escalated
                and max_tokens < runtime_output_limit
            ):
                # 截断意味着供应商**没有交付任何可用答案**（content 为空或半句），
                # 和"流中断"同类：没有判断可保留，也没有答案可挑选，所以这不是
                # 在重摇一个语义答案，而是把同一个确定性请求放到它本来就应该有的
                # 输出上限上再发一次。只做一次，且只在还没顶到模型上限时做；
                # 顶到上限仍截断则照常失败。思考预留（text_request_token_limits）
                # 覆盖常态，这一跳负责分布尾部。
                truncation_escalated = True
                max_tokens = runtime_output_limit
                call_meta = {
                    **call_meta,
                    "effective_max_tokens": max_tokens,
                    "output_truncation_escalated": True,
                }
                continue
            if (
                exc.failure_kind == ProviderFailureKind.OUTPUT_MISSING.value
                and not undelivered_retry_used
            ):
                # content 为空、且响应既没有终态 finish_reason 也没有已入账的
                # completion_tokens（判定见 _content_delivery_absent）：供应商
                # 对这次请求什么都没交付，和 OUTPUT_TRUNCATED 同理——不是在重摇
                # 一个语义答案，原样重放同一份请求一次是安全的。实测库存证据：
                # 全库 4712 次成功 kind=chat 调用里，这个指纹只出现过 1 次
                # （EP6 蓝图局部修复，2026-08-23），概率≈0.02%，不是系统性模式，
                # 不需要抬高 max_tokens（该次 completion_tokens=0，远未顶到预算）。
                # 只做一次；重放后仍未交付就照常 fail-closed，不无限重试。
                undelivered_retry_used = True
                continue
            if attempt_response_format is not None and _looks_like_response_format_unsupported(exc):
                if response_format_required:
                    # 契约说明（对应 app/portraits.py 三处身份判定调用的
                    # call_meta["disable_provider_retries"]=True，由
                    # app/harness/model_gateway.py 的
                    # "forbids provider retries" 断言强制）：那个标志只网关一层
                    # 针对 exc.retryable 的 5xx/超时重试循环，本函数从不读它，
                    # 下面这段 400 原样重放对它是刻意豁免、不是疏漏。二者不是同一
                    # 件事——disable_provider_retries 禁的是"换一次语义答案再摇一次
                    # 骰子"的业务重试；这里的重放是同一份 payload 原样再发一次的
                    # 传输层背景噪声修复（约 3.8% 与内容无关的独立 400、原样重放约
                    # 96% 成功、命中即代表 provider 在生成前就拒绝、不产生 token
                    # 成本），既不重新摇答案也不放宽任何校验，所以不违反这些身份判定
                    # "一律 fail-closed" 的既定原则。
                    # required 调用绝不静默降级：json_schema → json_object → 纯文本这条
                    # 能力阶梯只对"允许弱化"的调用开放。真实 API 实验显示该类 400 约
                    # 3.8% 概率发生、与 schema 大小/结构无关，是间歇性背景噪声而非真的
                    # 不支持；原地重放同一份请求约 96% 概率成功，所以这里选择"原样重试"
                    # 而不是"下探到弱约束"。仍然把这次拒绝计入(provider, model)的连续
                    # 拒绝计数，供其它非 required 调用参考（见 _remember_json_schema_unsupported
                    # / _json_schema_known_unsupported 前面的 not response_format_required 判断，
                    # required 调用自己永远不读这份缓存）。重试预算用尽后原样抛出，不吞异常、
                    # 不悄悄换成弱约束继续跑。
                    if attempt_response_format.get("type") == "json_schema":
                        _remember_json_schema_unsupported(provider, selected_model)
                    else:
                        _remember_response_format_unsupported(provider, selected_model)
                    if required_format_attempt < _RESPONSE_FORMAT_REQUIRED_JSON_SCHEMA_RETRIES:
                        # 退避沿用本文件 _post_json 已有的 1.5 * 2**attempt 节奏，不发明新策略。
                        await asyncio.sleep(1.5 * (2 ** required_format_attempt))
                        required_format_attempt += 1
                        # payload 逐字节不变（同一份 response_format 原样重放），所以
                        # provider_operation_id（app/db.py 里纯 kind/model/payload 的哈希，
                        # 不读 meta）算出的 operation_id 和上一次尝试完全相同——这是必须
                        # 保留的既有语义，不能因为重试就让它跳变。但落库时 400 记的是
                        # FAILED，而 app/db.py 的 attempt_no/supersedes_call_id 拼接默认
                        # 只认上一条状态是 INTERRUPTED；不显式声明的话这几次原样重放会在
                        # /api/system/calls 里显示成互不相关的重复调用而不是一条重试链。
                        # required_format_retry_attempt 只是给人看的调试字段；真正让
                        # app/db.py 把这次 FAILED 也接进链路的是下面这个显式 opt-in 开关，
                        # 范围严格限定在这一条重放路径，不影响其它 FAILED 状态的默认不链接行为。
                        call_meta = {
                            **call_meta,
                            "required_format_retry_attempt": required_format_attempt,
                            "provider_call_retry_of_failed": True,
                        }
                        continue
                    raise
                # 能力阶梯：json_schema → json_object → 纯文本。
                # 每一级只在网关明确拒绝上一级时下探一次，并把该能力缺失按
                # provider:model 记住，之后的调用直接从可用的那一级开始。
                # 无论降到哪一级，本地 schema 校验与业务校验始终是权威判据，
                # 纯文本还会经过 extract_json 修复，所以降级不会放过错误答案，
                # 只是把"合法 JSON 由谁保证"从网关移回本地。
                degraded = _json_object_response_format(attempt_response_format)
                if degraded is not None:
                    _remember_json_schema_unsupported(provider, selected_model)
                    attempt_response_format = degraded
                    continue
                _remember_response_format_unsupported(provider, selected_model)
                attempt_response_format = None
                continue
            raise

    # content 的非空校验已经在循环内的 _dispatch() 之后立刻做过（见上方
    # "if not content or not content.strip()"），break 只会在校验通过后发生，
    # 这里不需要也不应该重复判断。
    return content


# ---------------------------------------------------------------------------
# 原生工具调用（OpenAI function calling）。现有 `chat` 保持不变；此处新增，供对话
# Agent 编排器使用。工具*执行*仍由 Command Bus 负责，本层只做协议解析。
# ---------------------------------------------------------------------------

# 已知支持 OpenAI 原生 function calling 的文本供应商。其余（或网关能力未知）时可通过
# 设置 agent_native_tools=off 强制回退到手写 JSON 协议（见 _chat_tools_via_json_protocol）。
_NATIVE_TOOL_PROVIDERS = frozenset({"openrouter", "bailian", "deepseek", "zhipu", "hiagent"})

_JSON_PROTOCOL_INSTRUCTION = (
    "本网关不支持原生工具调用。请严格只返回一个 JSON 对象，不要 Markdown 代码块或任何额外文字：\n"
    '{"reply": "给用户看的简短中文说明", "tool_calls": [{"tool": "工具名", "arguments": {...}}], "done": true 或 false}\n'
    "tool_calls 最多 1 个元素；若已可回答用户或无更多可执行动作，tool_calls 传空数组并令 done=true。"
)


@dataclass
class ToolCall:
    """模型请求的一次工具调用（已解析）。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # 原始 arguments JSON 串：回填 assistant 消息时优先使用，避免重新序列化产生漂移。
    arguments_raw: str | None = None


@dataclass
class AssistantTurn:
    """一次带工具能力的模型回合结果。tool_calls 为空表示这是最终回复（content）。

    reasoning：推理模型的思考过程原文（OpenAI 兼容 `reasoning`/`reasoning_content`）。
    过去被一律丢弃；现在保留下来供对话助手展示「思考过程」。
    """

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    reasoning: str = ""


def _provider_supports_tools(provider: str) -> bool:
    override = (get_setting("agent_native_tools") or "").strip().lower()
    if override in {"0", "off", "false", "no", "disabled"}:
        return False
    if override in {"1", "on", "true", "yes", "enabled"}:
        return True
    if provider.startswith("custom:"):
        return True
    return provider in _NATIVE_TOOL_PROVIDERS


def _looks_like_tools_unsupported(exc: ProviderError) -> bool:
    """网关以客户端错误明确拒绝 tools 字段时才回退，避免把限流/故障误判为不支持。"""
    if exc.retryable:
        return False
    blob = f"{exc} {exc.raw}".lower()
    return ("tool" in blob or "function" in blob) and (
        "support" in blob or "unknown" in blob or "unrecognized" in blob or "invalid" in blob
    )


# 运行期记录“已证实拒绝 response_format 的 provider:model”，避免每次都先试一遍再回退。
# 这是能力协商（按运行期真实反馈学习），不是内容白/黑名单；重启即清空、按需重学。
_RESPONSE_FORMAT_UNSUPPORTED: set[str] = set()

_JSON_OBJECT_SYSTEM_INSTRUCTION = (
    "Return exactly one valid JSON object. Do not use Markdown or prose outside JSON."
)


def _message_content_mentions_json(value: Any) -> bool:
    """Return whether textual message content already names the JSON contract."""
    if isinstance(value, str):
        return "json" in value.casefold()
    if isinstance(value, dict):
        return any(_message_content_mentions_json(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_message_content_mentions_json(item) for item in value)
    return False


def _messages_for_response_format(
    messages: list[dict], response_format: dict[str, Any] | None,
) -> list[dict]:
    """Make ``json_object``/``json_schema`` requests valid for strict OpenAI-compatible gateways.

    Some gateways reject any JSON ``response_format`` unless a message explicitly
    contains the word ``JSON`` (DeepSeek V4 Pro via HiAgent enforces it for
    ``json_schema`` too: ERR-20260902-3d19ef, 2026-09-02).  Add that protocol hint
    at the provider boundary, without mutating caller-owned messages.  Other
    response formats and ordinary text calls keep their byte-for-byte payload.
    """
    if not response_format or response_format.get("type") not in {"json_object", "json_schema"}:
        return messages
    copied = [dict(message) for message in messages]
    if any(
        _message_content_mentions_json(message.get("content"))
        for message in copied
    ):
        return copied
    return [
        {"role": "system", "content": _JSON_OBJECT_SYSTEM_INSTRUCTION},
        *copied,
    ]


def _response_format_capability_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _response_format_known_unsupported(provider: str, model: str) -> bool:
    return _response_format_capability_key(provider, model) in _RESPONSE_FORMAT_UNSUPPORTED


def _remember_response_format_unsupported(provider: str, model: str) -> None:
    _RESPONSE_FORMAT_UNSUPPORTED.add(_response_format_capability_key(provider, model))


# A model can reject the *schema* flavour while still honouring plain
# ``json_object``.  Production: `json_schema is not supported by this model`
# (HTTP 400) from a gateway that accepts json_object fine.
#
# Measured against the real gateway, a single such 400 is not reliable evidence
# of incapability: the rejection rate is ~3.8% and behaves like independent
# background noise -- uncorrelated with schema size, $ref/$defs, nesting depth,
# enum size, or anyOf/oneOf. Replaying the *identical* request usually succeeds.
# Blacklisting a (provider, model) after one hit therefore poisons this
# process for the rest of its life over what is most likely noise. Require a
# short streak of *consecutive* (uninterrupted by an intervening success)
# rejections before caching "unsupported". Threshold=3: assuming independent
# ~3.8% events, three in a row has probability ~5e-5, which is low enough to
# treat as a real signal while one or two isolated hits are absorbed as noise.
# This is a process-lifetime, best-effort optimisation only -- it exists to
# skip a round trip that will *probably* 400 again for opportunistic callers.
# It must never be consulted for response_format_required=True calls (see
# chat()): those always send the caller's real response_format and, on a 400,
# retry the same request in place rather than downgrading or reading this
# cache.
_JSON_SCHEMA_UNSUPPORTED_STREAK_THRESHOLD = 3
_JSON_SCHEMA_REJECT_STREAK: dict[str, int] = {}
_JSON_SCHEMA_UNSUPPORTED: set[str] = set()


def _json_schema_known_unsupported(provider: str, model: str) -> bool:
    return _response_format_capability_key(
        provider, model
    ) in _JSON_SCHEMA_UNSUPPORTED


def _remember_json_schema_unsupported(provider: str, model: str) -> None:
    """Record one json_schema rejection; blacklist only after a consecutive streak.

    Called for every observed rejection, including those from
    ``response_format_required=True`` retries -- the event is real regardless
    of who saw it. What differs is *consumption*: required calls never check
    ``_json_schema_known_unsupported`` (see chat()), so this bookkeeping only
    ever helps future non-required callers skip a likely-doomed round trip.
    """
    key = _response_format_capability_key(provider, model)
    streak = _JSON_SCHEMA_REJECT_STREAK.get(key, 0) + 1
    _JSON_SCHEMA_REJECT_STREAK[key] = streak
    if streak >= _JSON_SCHEMA_UNSUPPORTED_STREAK_THRESHOLD:
        _JSON_SCHEMA_UNSUPPORTED.add(key)


def _forget_json_schema_reject_streak(provider: str, model: str) -> None:
    """Reset the consecutive-rejection streak after an observed success.

    Keeps "consecutive" meaningful: a success in between rejections proves the
    (provider, model) pair can deliver json_schema, so unrelated noise hits
    before/after it must not accumulate toward the blacklist threshold.
    """
    _JSON_SCHEMA_REJECT_STREAK.pop(
        _response_format_capability_key(provider, model), None
    )


def _json_object_response_format(
    response_format: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Degrade a json_schema request to json_object, keeping a JSON guarantee.

    ``json_object`` still makes the gateway return syntactically valid JSON, so
    the entire "no JSON object ever decoded" failure class stays closed.  What
    it drops is provider-side *shape* enforcement -- and the caller validates
    the full schema plus its business rules locally regardless, which is what
    actually decides acceptance.  Falling all the way back to free text would
    be the real weakening; this is the rung between them that was missing.
    """
    if not isinstance(response_format, dict):
        return None
    if response_format.get("type") != "json_schema":
        return None
    return {"type": "json_object"}


def _looks_like_response_format_unsupported(exc: ProviderError) -> bool:
    """仅当网关以客户端错误明确拒绝 response_format 字段时才判定不支持。

    限流/超时/5xx 等都不算，以免把可恢复故障误判为能力缺失而永久放弃结构化约束。
    """
    if exc.retryable:
        return False
    blob = f"{exc} {exc.raw}".lower()
    names_format = (
        "response_format" in blob
        or "json_schema" in blob
        or "json schema" in blob
    )
    if not names_format:
        return False
    # This is a malformed request contract, not evidence that the model lacks
    # response_format support.  Silently removing response_format would hide a
    # first-attempt 400 and weaken the caller's structured-output guarantee.
    if "json" in blob and ("message" in blob or "messages" in blob) and any(
        marker in blob
        for marker in (
            "must contain",
            "must include",
            "should contain",
            "should include",
            "contain the word",
            "mention the word",
        )
    ):
        return False
    # Remember only an explicit capability/field rejection.  A generic
    # InvalidParameter may describe the prompt, schema, or another caller bug.
    return any(
        marker in blob
        for marker in (
            "unsupported parameter",
            "unsupported field",
            "not supported",
            "does not support",
            "unknown parameter",
            "unknown field",
            "unrecognized parameter",
            "unrecognized field",
            "unexpected parameter",
            "unexpected field",
            "not allowed",
        )
    )


def _resolve_text_connection(
    provider: str, model_override: str | None = None
) -> tuple[str, str, dict[str, str], str]:
    """返回 (chat_completions_url, model, headers, key_name)。bailian 需多模型回退，另行处理。

    连接一律从模型库条目解析；config 里的 base_url/key 只作为尚未迁移完成时的兜底。
    """
    from app import text_providers

    protocol = text_providers.protocol_for_provider(provider)
    model = model_override or active_model("text", provider)
    fallback_url, fallback_key, key_name = {
        "openrouter": (
            config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY, "OPENROUTER_API_KEY",
        ),
        "deepseek": (
            config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY, "DEEPSEEK_API_KEY",
        ),
        "zhipu": (
            config.ZHIPU_BASE_URL, config.ZHIPU_API_KEY, "ZHIPU_API_KEY",
        ),
    }.get(protocol, (config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY, f"model:{model}"))
    base_url, headers = _model_connection(provider, model, fallback_url, fallback_key)
    return f"{base_url}/chat/completions", model, headers, key_name


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        try:
            from app.schemas import extract_json

            parsed = extract_json(raw)
        except ValueError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_assistant_turn(data: dict, *, label: str) -> AssistantTurn:
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"{label} 响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    tool_calls: list[ToolCall] = []
    for idx, raw_call in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw_call, dict):
            continue
        fn = raw_call.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args_raw = fn.get("arguments")
        tool_calls.append(ToolCall(
            id=str(raw_call.get("id") or f"call_{idx}"),
            name=name,
            arguments=_parse_tool_arguments(args_raw),
            arguments_raw=args_raw if isinstance(args_raw, str) else None,
        ))
    if not tool_calls and not (content or "").strip():
        raise ProviderError(f"模型返回空内容且无工具调用（{_empty_content_detail(data)}）")
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
    return AssistantTurn(
        content=content or "", tool_calls=tool_calls, finish_reason=choice.get("finish_reason"),
        reasoning=reasoning if isinstance(reasoning, str) else "",
    )


def _tools_as_text(tools: list[dict[str, Any]]) -> str:
    lines = ["可用工具（tool 字段填工具名，arguments 按其参数）："]
    for tool in tools:
        fn = tool.get("function") or {}
        params = (fn.get("parameters") or {}).get("properties") or {}
        arg_names = "、".join(params.keys()) or "无"
        lines.append(f"  - {fn.get('name')}（参数：{arg_names}）：{fn.get('description', '')}")
    return "\n".join(lines)


def _flatten_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把原生格式（含 role=tool、assistant.tool_calls）压平成纯文本对话，供 JSON 回退协议使用。"""
    flat: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        if role == "tool":
            flat.append({"role": "user", "content": f"[工具结果] {text}"})
        elif role == "assistant" and msg.get("tool_calls"):
            names = "、".join(str((tc.get("function") or {}).get("name") or "") for tc in msg["tool_calls"])
            flat.append({"role": "assistant", "content": f"{text}（已请求调用工具：{names}）".strip()})
        else:
            flat.append({"role": role or "user", "content": text or ""})
    return flat


async def _chat_tools_via_json_protocol(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
    temperature: float, max_tokens: int, call_meta: dict | None,
    on_token: Callable[[str, str], None] | None = None,
) -> AssistantTurn:
    """回退协议：网关不支持原生 tools 时，用手写 JSON 协议模拟一次工具调用回合。"""
    from app.schemas import extract_json

    flat = _flatten_tool_messages(messages)
    flat.append({"role": "system", "content": _JSON_PROTOCOL_INSTRUCTION + "\n" + _tools_as_text(tools)})
    fallback_meta = {**(call_meta or {}), "tool_protocol": "json_fallback"}
    want_stream = on_token is not None and _streaming_enabled()
    streamed_reply = False
    if want_stream:
        assert on_token is not None
        reply_streamer = _JsonReplyStreamer(on_token)
        # 流式分支自己拼 payload，不经过 chat()，所以在这里把答案预算换算成
        # 供应商 max_tokens；非流式分支交给 chat() 换算，两条路各换算一次。
        _provider, _model, stream_max_tokens = text_request_token_limits(
            requested_max_tokens=max_tokens,
        )
        raw = await _stream_plain_chat(
            flat, temperature=temperature, max_tokens=stream_max_tokens,
            call_meta=fallback_meta, on_token=reply_streamer.feed,
        )
        streamed_reply = reply_streamer.emitted
    else:
        raw = await chat(
            flat, temperature=temperature, max_tokens=max_tokens,
            call_meta=fallback_meta)
    try:
        plan = extract_json(raw)
    except ValueError:
        if want_stream and on_token is not None and not streamed_reply and raw.strip():
            on_token("content", raw.strip())
        return AssistantTurn(content=raw.strip(), tool_calls=[])
    tool_calls: list[ToolCall] = []
    raw_calls = plan.get("tool_calls")
    if isinstance(raw_calls, list):
        for idx, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            name = str(call.get("tool") or call.get("name") or "").strip()
            if not name:
                continue
            args = call.get("arguments")
            tool_calls.append(ToolCall(id=f"call_{idx}", name=name,
                                       arguments=args if isinstance(args, dict) else {}))
    reply = str(plan.get("reply") or "").strip()
    # 流式网关在首帧前降级为非流式时不会触发 feed，收尾时补发一次，
    # 保证前端的事件契约不因 provider 能力而改变。
    if want_stream and on_token is not None and not streamed_reply and reply:
        on_token("content", reply)
    return AssistantTurn(content=reply, tool_calls=tool_calls)


class _JsonReplyStreamer:
    """从 JSON 协议的原始 token 中只抽取 `reply` 字符串，避免把工具 JSON 露给 UI。"""

    def __init__(self, on_token: Callable[[str, str], None]) -> None:
        self._on_token = on_token
        self._raw = ""
        self._sent = 0

    @property
    def emitted(self) -> bool:
        return self._sent > 0

    @staticmethod
    def _decoded_reply_prefix(raw: str) -> str:
        import re

        match = re.search(r'"reply"\s*:\s*"', raw)
        if not match:
            return ""
        source = raw[match.end():]
        out: list[str] = []
        i = 0
        escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                   "n": "\n", "r": "\r", "t": "\t"}
        while i < len(source):
            char = source[i]
            if char == '"':
                break
            if char != "\\":
                out.append(char)
                i += 1
                continue
            if i + 1 >= len(source):
                break
            marker = source[i + 1]
            if marker in escapes:
                out.append(escapes[marker])
                i += 2
                continue
            if marker == "u":
                digits = source[i + 2:i + 6]
                if len(digits) < 4:
                    break
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    break
                i += 6
                continue
            # 非法转义：不猜测，等最终 extract_json 给出明确结果。
            break
        return "".join(out)

    def feed(self, _kind: str, text: str) -> None:
        if _kind != "content":
            return
        self._raw += text
        decoded = self._decoded_reply_prefix(self._raw)
        if len(decoded) <= self._sent:
            return
        delta = decoded[self._sent:]
        self._sent = len(decoded)
        self._on_token("content", delta)


def _streaming_enabled() -> bool:
    """对话助手逐 token 流式开关；默认开启，可用 agent_stream_tokens=off 一键回退非流式。"""
    val = (get_setting("agent_stream_tokens") or "").strip().lower()
    return val not in {"0", "off", "false", "no", "disabled"}


def _accumulate_stream_chunk(
    chunk: dict[str, Any], *,
    content_parts: list[str], reasoning_parts: list[str],
    tool_slots: dict[int, dict[str, Any]], state: dict[str, Any],
    on_token: Callable[[str, str], None] | None,
) -> None:
    """把一帧 SSE delta 累积进重组缓冲，并按需触发 on_token（content / reasoning）。"""
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        state["usage"] = usage
    choices = chunk.get("choices") or []
    if not choices:
        return
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str) and content:
        content_parts.append(content)
        if on_token:
            on_token("content", content)
    reasoning = delta.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        reasoning_parts.append(reasoning)
        if on_token:
            on_token("reasoning", reasoning)
    for raw_call in delta.get("tool_calls") or []:
        if not isinstance(raw_call, dict):
            continue
        idx = raw_call.get("index")
        idx = int(idx) if isinstance(idx, int) else 0
        slot = tool_slots.setdefault(idx, {"id": None, "name": None, "args": []})
        if raw_call.get("id"):
            slot["id"] = str(raw_call["id"])
        fn = raw_call.get("function") or {}
        if fn.get("name"):
            slot["name"] = str(fn["name"])
        args_piece = fn.get("arguments")
        if isinstance(args_piece, str) and args_piece:
            slot["args"].append(args_piece)
    if choice.get("finish_reason"):
        state["finish_reason"] = choice["finish_reason"]


def _reconstruct_stream_data(
    content_parts: list[str], reasoning_parts: list[str],
    tool_slots: dict[int, dict[str, Any]], state: dict[str, Any],
) -> dict[str, Any]:
    """把流式增量重组成与非流式 `data` 等价的结构，交给 `_parse_assistant_turn` 复用。"""
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    reasoning_text = "".join(reasoning_parts)
    if reasoning_text:
        message["reasoning"] = reasoning_text
    if tool_slots:
        message["tool_calls"] = [
            {
                "id": slot["id"] or f"call_{idx}",
                "type": "function",
                "function": {"name": slot["name"] or "", "arguments": "".join(slot["args"])},
            }
            for idx, slot in sorted(tool_slots.items())
        ]
    data: dict[str, Any] = {"choices": [{"index": 0, "finish_reason": state.get("finish_reason"), "message": message}]}
    if state.get("usage"):
        data["usage"] = state["usage"]
    return data


class _FirstTokenTimeout(TimeoutError):
    """Stream produced no content/reasoning token before the first-token deadline."""


def _interrupted_stream_failure(
    *, call_id: int, latency: int, received_chars: int,
    content_parts: list[str], reasoning_parts: list[str],
    unconsumed_frames: list[str], state: dict[str, Any],
) -> ProviderError:
    """Build (not raise) the exception for a stream that ended without [DONE].

    ``classify_interrupted_stream`` decides whether this is a deterministic
    provider content-review rejection (ERR-20260831-4c9132: HiAgent sent the
    refusal as ordinary content then closed the connection early) or a
    genuine, possibly-transient interruption. The former is a definitive
    terminal outcome and must say so -- retrying cannot change it and must
    not be offered; the latter keeps the existing fail-closed "结果不确定，
    请手动确认后重试" contract unchanged.
    """
    evidence = interrupted_stream_evidence(
        content_parts=content_parts, reasoning_parts=reasoning_parts,
        unconsumed_frames=unconsumed_frames, state=state,
    )
    detail = (
        "stream interrupted before [DONE] "
        f"(latency_ms={latency}, received_chars={received_chars})"
    )
    if evidence.get("summary"):
        detail = f"{detail}: {evidence['summary']}"
    finish_provider_call(
        call_id, "INTERRUPTED", 200, latency,
        error=detail, response_json={"interrupted_stream": evidence},
    )
    if classify_interrupted_stream(
        get_conn(), call_id, evidence.get("finish_reason"), INTERRUPTED_STREAM_TEXT_CHARS,
    ):
        quote = evidence.get("summary") or ""
        return ProviderError(
            "供应商内容审核已明确拒绝本次请求"
            + (f"，原文：{quote}" if quote else "")
            + "；重试不会改变结果，请调整内容后重新生成，或更换供应商模型",
            raw=detail,
            delivery_state="responded",
            failure=ProviderFailure.model_rejection(),
        )
    return ProviderError(
        "流式响应在 [DONE] 前中断，结果不确定；"
        "已丢弃不完整结果并禁止自动重试，请在页面确认后重试",
        retryable=True,
        raw=detail,
        failure_kind="stream_interrupted",
        delivery_state="unknown",
        requires_explicit_retry=True,
        received_chars=received_chars,
    )


async def _stream_chat_completion(
    client: httpx.AsyncClient, url: str, payload: dict, *,
    kind: str, model: str, headers: dict | None = None, key_name: str = "HIAGENT_API_KEY",
    meta: dict | None = None, on_token: Callable[[str, str], None] | None = None,
) -> dict:
    """SSE 流式消费 chat/completions，逐 token 回调 on_token，最终重组为非流式等价 `data`。

    不做重试：请求一旦送达，是否已收到 token 都不能证明上游没有开始生成。

    ``received_chars`` 是逐帧累加的真实收字数（content delta + reasoning
    delta，每帧只加这一帧新增的长度，见下方循环），落库时不经任何裁剪。
    它与 ``finish_provider_call`` 落的 ``response_json`` 不是同一条链路：
    后者要经 ``app/db.py:_trim_for_call_log`` 按 120,000 字符裁剪单个字符串
    （超限会截断并附加 ``"...[truncated N chars]"``）。核对二者时如果直接
    比较 ``received_chars`` 和 ``len(response_json...content)``，在内容超过
    120,000 字符时会得出「received_chars 多算」的假结论——2026-08-29 抽查
    过全部 6335 条 status=OK 的 chat 记录（含全部 60 条 finish_reason=length），
    把裁剪标记还原回真实长度后 received_chars 与真实内容 0 处不符，此前报出
    的 5 处「多算」全部是这个裁剪导致的比对假象，不是累加逻辑本身的 bug。
    """
    merged_meta = _merge_call_meta(meta)
    req_headers = dict(headers if headers is not None else _headers())
    operation_id = str((merged_meta or {}).get("operation_id") or "").strip()
    if operation_id:
        req_headers["Idempotency-Key"] = _header_idempotency_key(operation_id)
    stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    request_bytes = _request_size_bytes(stream_payload)
    start = time.time()
    attempt_meta = {
        "http_attempt": 1, "http_attempts_max": 1, "request_bytes": request_bytes,
        "streaming": True, **(merged_meta or {}),
    }
    call_id = start_provider_call(kind, model, meta=attempt_meta, request_json=stream_payload)
    unconsumed_frames: list[str] = []
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_slots: dict[int, dict[str, Any]] = {}
    state: dict[str, Any] = {}
    received_chars = 0
    last_progress_chars = 0
    last_progress_at = start
    saw_done = False
    first_token_timeout_s = _first_token_timeout_s(merged_meta)
    try:
        total_timeout_s = max(
            60.0,
            float(get_setting("text_stream_total_timeout_s") or 1200),
        )
    except (TypeError, ValueError):
        total_timeout_s = 1200.0
    try:
        async def consume_stream() -> None:
            nonlocal received_chars, last_progress_chars, last_progress_at, saw_done
            async with client.stream("POST", url, json=stream_payload, headers=req_headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    text = body.decode("utf-8", "replace")
                    err = _classify_http_error(resp.status_code, text, key_name)
                    finish_provider_call(
                        call_id, "FAILED", resp.status_code, int((time.time() - start) * 1000),
                        error=str(err), response_json={"status_code": resp.status_code, "body": text})
                    raise err
                line_iter = resp.aiter_lines()
                while True:
                    if received_chars == 0 and first_token_timeout_s is not None:
                        remaining = first_token_timeout_s - (time.time() - start)
                        if remaining <= 0:
                            raise _FirstTokenTimeout("first streamed token did not arrive")
                        try:
                            line = await asyncio.wait_for(anext(line_iter), timeout=remaining)
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            raise _FirstTokenTimeout("first streamed token did not arrive") from exc
                    else:
                        try:
                            line = await anext(line_iter)
                        except StopAsyncIteration:
                            break
                    if not line or not line.startswith("data:"):
                        # An SSE ``event: error`` frame, or any non-data line,
                        # used to vanish here and the stream simply ended
                        # without [DONE] -- leaving no evidence of why.
                        remember_unconsumed_stream_frame(
                            unconsumed_frames, line,
                        )
                        continue
                    chunk_str = line[len("data:"):].strip()
                    if not chunk_str or chunk_str == "[DONE]":
                        if chunk_str == "[DONE]":
                            saw_done = True
                            break
                        continue
                    try:
                        chunk = json.loads(chunk_str)
                    except (json.JSONDecodeError, ValueError):
                        remember_unconsumed_stream_frame(
                            unconsumed_frames, chunk_str,
                        )
                        continue
                    content_count = len(content_parts)
                    reasoning_count = len(reasoning_parts)
                    _accumulate_stream_chunk(
                        chunk, content_parts=content_parts, reasoning_parts=reasoning_parts,
                        tool_slots=tool_slots, state=state, on_token=on_token)
                    received_chars += sum(
                        len(value)
                        for value in content_parts[content_count:]
                    ) + sum(
                        len(value)
                        for value in reasoning_parts[reasoning_count:]
                    )
                    stamp = time.time()
                    if (
                        received_chars > 0
                        and (
                            last_progress_chars == 0
                            or received_chars - last_progress_chars >= 8192
                            or stamp - last_progress_at >= 2.0
                        )
                    ):
                        update_provider_call_progress(
                            call_id,
                            received_chars=received_chars,
                            chunk_at=stamp,
                        )
                        last_progress_chars = received_chars
                        last_progress_at = stamp
        await asyncio.wait_for(consume_stream(), timeout=total_timeout_s)
        latency = int((time.time() - start) * 1000)
        if received_chars > last_progress_chars:
            update_provider_call_progress(
                call_id,
                received_chars=received_chars,
                chunk_at=time.time(),
            )
        if not saw_done:
            raise _interrupted_stream_failure(
                call_id=call_id, latency=latency, received_chars=received_chars,
                content_parts=content_parts, reasoning_parts=reasoning_parts,
                unconsumed_frames=unconsumed_frames, state=state,
            )
        data = _reconstruct_stream_data(content_parts, reasoning_parts, tool_slots, state)
        finish_provider_call(call_id, "OK", 200, latency, response_json=data)
        return data
    except asyncio.CancelledError:
        latency = int((time.time() - start) * 1000)
        detail = (
            "流式请求被取消，供应商结果未知 "
            f"(latency_ms={latency}, received_chars={received_chars})"
        )
        finish_provider_call(
            call_id,
            "INTERRUPTED",
            None,
            latency,
            error=detail,
        )
        raise
    except ProviderError:
        raise
    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
        latency = int((time.time() - start) * 1000)
        first_token_timeout = _first_token_timeout_s(merged_meta)
        if isinstance(exc, _FirstTokenTimeout):
            phase = "首字"
            diag = _timeout_diagnostic_label(
                client, merged_meta, kind,
                override_threshold_s=first_token_timeout,
            )
        else:
            phase = (
                "总时长"
                if isinstance(exc, asyncio.TimeoutError)
                else _timeout_phase(exc)
            )
            diag = _timeout_diagnostic_label(
                client, merged_meta, kind,
                override_threshold_s=total_timeout_s if isinstance(exc, asyncio.TimeoutError) else None,
            )
        detail = f"{type(exc).__name__}(phase={phase}, latency_ms={latency}, {diag}): {exc!r}"
        http_exc = exc if isinstance(exc, httpx.HTTPError) else None
        delivery_state, replay_safe = _stream_timeout_replay_state(http_exc, received_chars)
        if replay_safe:
            message = f"流式调用{phase}阶段超时（{latency}ms，{diag}）"
            failure_kind = "connection_failed" if delivery_state == "not_sent" else "request_outcome_unknown"
        else:
            message = (
                f"流式调用{phase}阶段超时（{latency}ms，{diag}）；"
                "请求结果不确定，已禁止自动重试，请在页面确认后重试"
            )
            failure_kind = "request_outcome_unknown"
        err = ProviderError(
            message,
            retryable=True,
            raw=detail,
            timeout_phase=phase,
            failure_kind=failure_kind,
            delivery_state=delivery_state,
            replay_safe=replay_safe,
            requires_explicit_retry=not replay_safe,
            received_chars=received_chars,
        )
        finish_provider_call(
            call_id,
            "TIMEOUT" if err.replay_safe else "INTERRUPTED",
            None,
            latency,
            error=detail,
        )
        raise err
    except httpx.HTTPError as exc:
        latency = int((time.time() - start) * 1000)
        detail = f"{type(exc).__name__}(latency_ms={latency}): {exc!r}"
        err = _transport_provider_error(
            exc,
            f"流式网络错误：{exc}",
            raw=detail,
        )
        finish_provider_call(
            call_id,
            "NETWORK_ERROR" if err.replay_safe else "INTERRUPTED",
            None,
            latency,
            error=detail,
        )
        raise err


async def _stream_or_fallback(
    client: httpx.AsyncClient, url: str, payload: dict, *,
    kind: str, model: str, headers: dict | None, key_name: str,
    meta: dict | None, on_token: Callable[[str, str], None],
) -> dict:
    """优先流式；仅在能证明请求未送达时降级为非流式。"""
    emitted = 0

    def _wrapped(token_kind: str, text: str) -> None:
        nonlocal emitted
        emitted += 1
        on_token(token_kind, text)

    try:
        return await _stream_chat_completion(
            client, url, payload, kind=kind, model=model, headers=headers,
            key_name=key_name, meta=meta, on_token=_wrapped)
    except ProviderError as exc:
        if (
            emitted > 0
            or not exc.replay_safe
            or (meta or {}).get("gateway") == "execution_harness"
            or _looks_like_tools_unsupported(exc)
        ):
            raise
        fallback_meta = {**(meta or {}), "stream_degraded": True, "stream_degraded_cause": str(exc)[:120]}
        return await _post_json(client, url, payload, kind=kind, model=model,
                                headers=headers, key_name=key_name, meta=fallback_meta)


async def _stream_plain_chat(
    messages: list[dict[str, Any]], *, temperature: float, max_tokens: int,
    call_meta: dict | None, on_token: Callable[[str, str], None],
) -> str:
    """无原生 tools 的流式文本调用，仅供 Agent JSON 协议回退使用。"""
    from app import text_providers

    provider = active_provider("text")
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_CHAT_READ, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if text_providers.protocol_for_provider(provider) == "bailian":
            preferred = active_model("text", provider)
            payload: dict[str, Any] = {
                "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
            }
            data, _ = await _stream_bailian_chat_with_fallback(
                client, payload, fallback_kind="text", log_kind="chat_tools_json_fallback",
                preferred_model=preferred, meta=call_meta, on_token=on_token,
            )
        else:
            url, resolved_model, headers, key_name = _resolve_text_connection(provider)
            payload = {
                "model": resolved_model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens,
            }
            if text_providers.protocol_for_provider(provider) == "openrouter":
                effort = (config.OPENROUTER_TEXT_REASONING_EFFORT or "").strip().lower()
                if thinking_disabled(call_meta) or not effort or effort == "none":
                    payload["temperature"] = temperature
                elif effort and effort != "none":
                    payload["reasoning"] = {"effort": effort}
            data = await _stream_or_fallback(
                client, url, payload, kind="chat_tools_json_fallback", model=resolved_model,
                headers=headers, key_name=key_name, meta=call_meta, on_token=on_token,
            )
    return _chat_content(data, label="chat_tools_json_fallback")


async def chat_with_tools(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
    tool_choice: str = "auto", model: str | None = None, temperature: float = 0.7,
    max_tokens: int = 65535, call_meta: dict | None = None,
    on_token: Callable[[str, str], None] | None = None,
) -> AssistantTurn:
    """带原生工具调用的文本对话。返回 AssistantTurn（content + reasoning + 已解析的 tool_calls）。

    - 供应商路由复用 `chat` 的连接解析；payload 携带 `tools` / `tool_choice`。
    - 供应商不支持 `tools`（能力标志关闭或网关以客户端错误拒绝）时，回退到手写 JSON 协议，
      对上层返回同一种 AssistantTurn，编排器无需感知差异。
    - 传入 `on_token(kind, text)` 且未关闭 agent_stream_tokens 时，走 SSE 流式并逐 token 回调
      （kind ∈ {"content","reasoning"}）；只有能证明请求未送达时才降级为非流式。百炼多模型回退
      路径与 JSON 回退协议暂不流式，最终结果一致。
    """
    provider = active_provider("text")
    # 调用方传的是「答案预算」。原生 tools 路径自己拼 payload，所以这里换算成
    # 供应商 max_tokens（叠加思考预留并夹紧到模型真实上限）；此前这条路径完全
    # 绕开该入口，默认值 65535 甚至已经超过模型默认上限 32768。
    #
    # JSON 协议回退路径**必须继续拿原始答案预算**：它内部要么走 chat()（自己换算），
    # 要么走 _stream_plain_chat（在那里换算）。传换算后的值会让思考预留被叠加两次。
    answer_max_tokens = max(1, int(max_tokens))
    _provider, _model, max_tokens = text_request_token_limits(
        requested_max_tokens=answer_max_tokens,
        provider=provider,
        model=model,
    )
    if not _provider_supports_tools(provider):
        return await _chat_tools_via_json_protocol(
            messages, tools, temperature=temperature,
            max_tokens=answer_max_tokens,
            call_meta=call_meta, on_token=on_token)
    from app import text_providers

    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_CHAT_READ, write=30, pool=10)
    stream = on_token is not None and _streaming_enabled()
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if text_providers.protocol_for_provider(provider) == "bailian":
                bailian_model = active_model("text", provider)
                payload: dict[str, Any] = {
                    "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                    "tools": tools, "tool_choice": tool_choice,
                }
                if stream:
                    data, _ = await _stream_bailian_chat_with_fallback(
                        client, payload, fallback_kind="text", log_kind="chat_tools",
                        preferred_model=bailian_model, meta=call_meta, on_token=on_token)
                else:
                    data, _, _ = await _post_bailian_chat_with_fallback(
                        client, payload, fallback_kind="text", log_kind="chat_tools",
                        preferred_model=bailian_model, meta=call_meta)
            else:
                url, resolved_model, headers, key_name = _resolve_text_connection(provider, model)
                payload = {
                    "model": resolved_model, "messages": messages, "temperature": temperature,
                    "max_tokens": max_tokens, "tools": tools, "tool_choice": tool_choice,
                }
                if stream:
                    data = await _stream_or_fallback(
                        client, url, payload, kind="chat_tools", model=resolved_model,
                        headers=headers, key_name=key_name, meta=call_meta, on_token=on_token)
                else:
                    data = await _post_json(client, url, payload, kind="chat_tools", model=resolved_model,
                                            headers=headers, key_name=key_name, meta=call_meta)
        except ProviderError as exc:
            if _looks_like_tools_unsupported(exc):
                return await _chat_tools_via_json_protocol(
                    messages, tools, temperature=temperature,
                    max_tokens=answer_max_tokens,
                    call_meta=call_meta, on_token=on_token)
            raise
    # 被截断的工具调用参数是**残缺的 JSON**，编排器却会照常执行它。
    # 和普通文本调用同等对待：截断即失败，带上可诊断的 OUTPUT_TRUNCATED。
    _reject_truncated_chat_response(data)
    return _parse_assistant_turn(data, label="chat_tools")


async def create_video_task(
    prompt_text: str,
    *,
    image_urls: list[tuple[str, str]] | None = None,
    video_urls: list[tuple[str, str]] | None = None,
    return_last_frame: bool = False,
    call_meta: dict | None = None,
) -> str:
    """创建 Seedance 任务；图片角色与 reference_video 角色在本地先做互斥校验。"""
    def reject_before_create(message: str, **kwargs: Any) -> ProviderError:
        return ProviderError(
            message,
            delivery_state="not_sent",
            replay_safe=True,
            create_not_accepted=True,
            **kwargs,
        )

    image_roles = [str(role) for _url, role in (image_urls or [])]
    video_roles = [str(role) for _url, role in (video_urls or [])]
    valid_image_roles = {"first_frame", "last_frame", "reference_image"}
    if any(role not in valid_image_roles for role in image_roles):
        raise reject_before_create(f"非法视频图片输入角色：{image_roles}")
    if any(role != "reference_video" for role in video_roles):
        raise reject_before_create(f"非法视频输入角色：{video_roles}")
    if video_roles and image_roles:
        raise reject_before_create("reference_video 不能与 reference_image/first_frame/last_frame 混用")
    if "reference_image" in image_roles and (
        "first_frame" in image_roles or "last_frame" in image_roles
    ):
        raise reject_before_create("reference_image 不能与 first_frame/last_frame 混用")
    if "last_frame" in image_roles and "first_frame" not in image_roles:
        raise reject_before_create("last_frame 不能脱离 first_frame 单独提交")
    for url, _role in video_urls or []:
        if str(url).startswith("data:") or not str(url).startswith(("http://", "https://")):
            raise reject_before_create("reference_video 必须是供应商可访问的 http(s) Web URL")

    from app import video_providers

    return await video_providers.resolve(
        active_provider("video")
    ).create_video_task(
        prompt_text,
        image_urls=image_urls,
        video_urls=video_urls,
        return_last_frame=return_last_frame,
        call_meta=call_meta,
    )


async def poll_video_task(task_id: str, *, call_meta: dict | None = None) -> dict:
    """轮询单次；按 task_id 归属路由到对应供应商适配器。"""
    from app import video_providers

    adapter = video_providers.adapter_for_task_id(task_id)
    if adapter is None:
        adapter = video_providers.resolve(active_provider("video"))
    return await adapter.poll_video_task(task_id, call_meta=call_meta)


async def _download_once(url: str, dest_path: str) -> None:
    """单次下载；按产物 URL 归属路由，未被认领的一律走通用公网下载。"""
    from app import video_providers

    adapter = video_providers.adapter_for_output_url(url)
    if adapter is not None:
        await adapter.download_output(url, dest_path)
        return
    await _download_public_url(url, dest_path)


async def _download_public_url(url: str, dest_path: str) -> None:
    """通用媒体下载；禁止 SSRF：仅允许公网 http(s)，跟随重定向后再次校验。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    def _assert_public_download_url(candidate: str) -> None:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise ProviderError("下载 URL 必须是 http(s)")
        host = (parsed.hostname or "").strip().lower()
        if not host:
            raise ProviderError("下载 URL 缺少主机名")
        if host in {"localhost", "metadata", "metadata.google.internal"} or host.endswith(".local"):
            raise ProviderError("拒绝下载本机或链路本地地址")
        if host == "169.254.169.254":
            raise ProviderError("拒绝下载云元数据地址")
        candidates: list[str] = []
        try:
            ipaddress.ip_address(host)
            candidates = [host]
        except ValueError:
            try:
                infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise ProviderError(f"无法解析下载主机：{host}") from exc
            for info in infos:
                addr = info[4][0]
                if addr:
                    candidates.append(addr)
        for addr in candidates:
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            ):
                raise ProviderError("拒绝下载内网或保留地址")

    _assert_public_download_url(url)
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_DOWNLOAD, write=30, pool=10)

    async def _on_redirect(response: httpx.Response) -> None:
        location = response.headers.get("location")
        if location:
            next_url = str(response.url.join(location))
            _assert_public_download_url(next_url)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        event_hooks={"response": [_on_redirect]},
    ) as client:
        resp = await client.get(url)
        # 最终落地 URL 再校验一次（防 DNS 重绑定后的内网跳转）。
        _assert_public_download_url(str(resp.url))
        if resp.status_code != 200:
            raise ProviderError(f"视频下载失败 HTTP {resp.status_code}（URL 可能已过期，有效期 7 天）")
        atomic_write_bytes(dest_path, resp.content)


async def download(url: str, dest_path: str) -> None:
    """Download provider media with bounded retries for transient transport faults."""
    attempts = 3
    last_error: ProviderError | None = None
    for attempt in range(attempts):
        try:
            await _download_once(url, dest_path)
            return
        except ProviderError as exc:
            if not exc.retryable:
                raise
            last_error = exc
        except httpx.RequestError as exc:
            phase = _timeout_phase(exc) if isinstance(exc, httpx.TimeoutException) else None
            last_error = ProviderError(
                f"媒体下载网络异常：{type(exc).__name__}: {exc}",
                retryable=True,
                raw=repr(exc),
                timeout_phase=phase,
            )
        if attempt + 1 < attempts:
            await asyncio.sleep(1.5 * (2 ** attempt))
    assert last_error is not None
    raise last_error


async def generate_image(prompt: str, *, size: str = "1024x1024",
                         image_inputs: list[str] | None = None,
                         call_meta: dict | None = None,
                         log_kind: str | None = None) -> dict:
    """Seedream 图像生成。返回 {url 或 b64_json}。
    image_inputs：可选的参考图（data URL 列表），用于让生成图保持角色/场景一致性。
    网关是否支持参考图未知，调用方应 try-with-fallback（带参考图失败则不带重试）。"""
    from app import image_providers

    provider = active_provider("image")
    model = active_model("image", provider)
    base_url, model_headers = _model_connection(
        provider, model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY,
    )
    payload: dict[str, Any] = {
        "model": model, "prompt": prompt, "n": 1, "size": size, "watermark": False,
    }
    media_meta: dict[str, Any] = {}
    if image_inputs:
        prepared_inputs, media_meta = await _prepare_image_data_urls(image_inputs)
        image_providers.apply_reference_images(
            payload,
            prepared_inputs,
            protocol=image_providers.protocol_for_provider(provider),
        )
    kind = log_kind or ("image_edit" if image_inputs else "image_generate")
    operation_id = (
        str((call_meta or {}).get("operation_id") or "").strip()
        or provider_operation_id(kind, model, payload)
    )
    request_meta = {
        **(call_meta or {}),
        **media_meta,
        "operation_id": operation_id,
    }
    data = _cached_successful_provider_response(
        kind, model, payload, request_meta,
    )
    if data is None:
        timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_IMAGE_READ,
                                write=config.TIMEOUT_IMAGE_WRITE, pool=10)
        async with _image_semaphore():
            async with httpx.AsyncClient(timeout=timeout) as client:
                data = await _post_json(
                    client, f"{base_url}/images/generations", payload,
                    kind=kind, model=model, headers=model_headers, key_name=f"model:{model}",
                    meta=request_meta,
                    idempotency_key=operation_id)
    items = data.get("data") or []
    if not items:
        raise ProviderError(f"图像生成响应为空：{json.dumps(data, ensure_ascii=False)[:300]}")
    item = dict(items[0])
    if item.get("url"):
        item["url"] = _absolute_provider_url(item["url"], base_url)
    return item


def encode_image_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def data_url_from_file(path: str) -> str:
    """本地图片 → data URL。实测网关接受 base64 data URL 作为参考图，无需外部托管。"""
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{encode_image_file(path)}"
