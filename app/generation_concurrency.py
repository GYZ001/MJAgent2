"""Process-wide concurrency gates for long-running text generation workflows."""
from __future__ import annotations

import asyncio
import contextvars
import heapq
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from app.db import get_setting

T = TypeVar("T")

PRIORITY_INTERACTIVE = 0
PRIORITY_RECOVERY = 10
PRIORITY_BATCH = 20
DEFAULT_TEXT_GENERATION_CONCURRENCY = 10
MAX_TEXT_GENERATION_CONCURRENCY = 16

_generation_priority: contextvars.ContextVar[int] = contextvars.ContextVar(
    "generation_priority",
    default=PRIORITY_INTERACTIVE,
)
_provider_call_slot_lease: contextvars.ContextVar[
    tuple[asyncio.Task[object] | None, Callable[[], bool] | None] | None
] = contextvars.ContextVar(
    "provider_call_slot_lease",
    default=None,
)


@dataclass
class _PriorityGate:
    limit: int
    active: int = 0
    sequence: int = 0
    waiters: list[tuple[int, int, asyncio.Future[None]]] = field(default_factory=list)

    async def acquire(self, priority: int) -> None:
        if self.active < self.limit and not self.waiters:
            self.active += 1
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self.sequence += 1
        heapq.heappush(self.waiters, (int(priority), self.sequence, future))
        try:
            await future
        except BaseException:
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
            raise

    def release(self) -> None:
        if self.active > self.limit:
            self.active -= 1
            return
        while self.waiters:
            _priority, _sequence, future = heapq.heappop(self.waiters)
            if future.cancelled():
                continue
            future.set_result(None)
            return
        self.active = max(0, self.active - 1)

    def resize(self, limit: int) -> None:
        new_limit = max(1, int(limit))
        previous = self.limit
        self.limit = new_limit
        if new_limit <= previous:
            return
        while self.active < self.limit and self.waiters:
            _priority, _sequence, future = heapq.heappop(self.waiters)
            if future.cancelled():
                continue
            self.active += 1
            future.set_result(None)


_loop_gates: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, _PriorityGate],
] = weakref.WeakKeyDictionary()


def _resource_key(workflow_type: str) -> str:
    if workflow_type in {"screenplay", "storyboard"}:
        return "text_generation_workflows"
    if workflow_type in {"text_provider", "text_provider_calls"}:
        return "text_provider_calls"
    return workflow_type


def _configured_limit(workflow_type: str) -> int:
    resource = _resource_key(workflow_type)
    if resource == "text_provider_calls":
        # 真实供应商请求槽位接入媒体流水线那套"拥塞减半 + 健康爬升"自适应状态
        # 机（app/media_pipeline/concurrency.py），取代对 text_generation_concurrency
        # 的静态直读：上限仍然来自这个设置，但生效值会随 429/5xx/超时/内存水位
        # 等真实信号自动升降，而不是永远钉在配置里的那个数字。
        from app.media_pipeline.concurrency import RESOURCE_TEXT_PROVIDER, channel_limit

        return max(1, min(MAX_TEXT_GENERATION_CONCURRENCY, channel_limit(RESOURCE_TEXT_PROVIDER)))
    if resource == "text_generation_workflows":
        raw = (
            get_setting("text_generation_workflow_concurrency")
            or get_setting("text_generation_concurrency")
        )
    else:
        raw = get_setting(f"{resource}_concurrency")
    try:
        return max(
            1,
            min(
                MAX_TEXT_GENERATION_CONCURRENCY,
                int(raw or DEFAULT_TEXT_GENERATION_CONCURRENCY),
            ),
        )
    except (TypeError, ValueError):
        return DEFAULT_TEXT_GENERATION_CONCURRENCY


def reload_generation_limits() -> int:
    """Apply persisted limits to existing gates, including tasks already waiting."""
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    updated = 0
    for loop, by_type in list(_loop_gates.items()):
        if loop.is_closed():
            continue
        changes = [
            (gate, _configured_limit(resource))
            for resource, gate in list(by_type.items())
        ]
        if not changes:
            continue

        def apply(changes=changes) -> None:
            for gate, limit in changes:
                gate.resize(limit)

        if loop is current_loop or not loop.is_running():
            apply()
        else:
            loop.call_soon_threadsafe(apply)
        updated += len(changes)
    return updated


def gate_for(workflow_type: str) -> _PriorityGate:
    """Return one shared priority gate per event loop and workflow resource."""
    loop = asyncio.get_running_loop()
    by_type = _loop_gates.setdefault(loop, {})
    resource = _resource_key(workflow_type)
    desired = _configured_limit(workflow_type)
    gate = by_type.get(resource)
    if gate is None:
        gate = _PriorityGate(desired)
        by_type[resource] = gate
    else:
        gate.resize(desired)
    return gate


async def run_with_generation_slot(
    workflow_type: str,
    operation: Callable[[], Awaitable[T]],
    *,
    priority: int = PRIORITY_INTERACTIVE,
) -> T:
    """Run an operation under the process-wide priority-aware concurrency limit."""
    gate = gate_for(workflow_type)
    await gate.acquire(priority)
    priority_token = _generation_priority.set(int(priority))
    try:
        return await operation()
    finally:
        _generation_priority.reset(priority_token)
        gate.release()


def current_generation_priority() -> int:
    """Return the workflow priority inherited by nested provider requests."""
    return int(_generation_priority.get())


#: 人物定妆照 / 场景定场图批量出图：各自独立的有界并发池，互不挤占彼此的槽位。
#: 与上面的文本生成优先级闸门是两套独立机制——图片批量出图没有优先级抢占需求，
#: 只需要"这一批最多同时跑几个"，故用最简单的按 event loop 缓存的 Semaphore。
CHARACTER_PORTRAIT_BATCH_CONCURRENCY = 5
SCENE_REFERENCE_BATCH_CONCURRENCY = 5

_image_batch_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _image_batch_semaphore(resource: str, limit: int) -> asyncio.Semaphore:
    """One bounded-concurrency gate per (event loop, resource).

    Mirrors ``gate_for``'s per-loop caching: a module-level ``asyncio.Semaphore``
    singleton would bind to whichever loop happened to be running at import
    time, which breaks under pytest's per-test event loops.  Keying by the
    currently running loop keeps each test (and each real process loop)
    isolated.
    """
    loop = asyncio.get_running_loop()
    by_resource = _image_batch_semaphores.setdefault(loop, {})
    semaphore = by_resource.get(resource)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        by_resource[resource] = semaphore
    return semaphore


def character_portrait_batch_semaphore() -> asyncio.Semaphore:
    """Independent pool: at most ``CHARACTER_PORTRAIT_BATCH_CONCURRENCY``
    character-portrait pipelines (image request + QA + multiview pack) run
    concurrently.  Scene reference generation has its own separate pool below
    so neither workflow can starve the other of concurrency."""
    return _image_batch_semaphore(
        "character_portrait_batch", CHARACTER_PORTRAIT_BATCH_CONCURRENCY,
    )


def scene_reference_batch_semaphore() -> asyncio.Semaphore:
    """Independent pool: at most ``SCENE_REFERENCE_BATCH_CONCURRENCY`` scene-
    reference pipelines run concurrently, isolated from the portrait pool."""
    return _image_batch_semaphore(
        "scene_reference_batch", SCENE_REFERENCE_BATCH_CONCURRENCY,
    )


# 认定为"供应商侧拥塞"的 ProviderError.failure_kind 取值：429（rate_limited）、
# 5xx 含 504（upstream_unavailable）、传输层超时的两种归类（connection_failed /
# request_outcome_unknown，覆盖 ReadTimeout 与 hiagent._FirstTokenTimeout）。协议或
# 数据完整性问题（如 stream_interrupted）不算——重试解决不了拥塞，误记会让自适应
# 状态失真。
_CONGESTION_FAILURE_KINDS = frozenset({
    "rate_limited",
    "upstream_unavailable",
    "connection_failed",
    "request_outcome_unknown",
})


def _congestion_reason(exc: BaseException) -> str | None:
    """从一次失败的真实供应商调用里识别拥塞证据；不是拥塞信号则返回 None。

    优先读 `hiagent.ProviderError` 已经做好的 retryable + failure_kind 分类
    （duck-typed via getattr，不 import hiagent，避免引入模块间耦合）；再退到
    stdlib/httpx 的超时类型与显式状态码兜底，覆盖任何尚未走到该分类的异常。
    """
    failure_kind = getattr(exc, "failure_kind", None)
    if getattr(exc, "retryable", False) and failure_kind in _CONGESTION_FAILURE_KINDS:
        return str(failure_kind)
    if isinstance(exc, TimeoutError):  # 含 asyncio.TimeoutError 与 _FirstTokenTimeout
        return "timeout"
    try:
        import httpx
    except ImportError:
        httpx = None  # type: ignore[assignment]
    if httpx is not None and isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    status_code = getattr(exc, "status_code", None)
    if status_code in (429, 504):
        return f"http_{status_code}"
    return None


def _report_text_provider_outcome(exc: BaseException | None) -> None:
    """把一次真实文本 provider 调用的结果接进媒体流水线那套"拥塞减半 + 健康爬升"
    自适应状态机（复用 app/media_pipeline/concurrency.py，不另写一套）。

    内存水位优先于调用结果判断：即使这次调用本身成功，可用内存跌破本机实测水位
    也要降档——那不是供应商的问题，是本机快扛不住了；成功与拥塞不能在同一次上报
    里并存，否则状态自相矛盾。调用失败但不属于拥塞证据（如业务校验失败）时，既
    不算拥塞也不算健康，不上报——同 L3 对不可重试失败的既有处理方式一致。
    """
    from app.media_pipeline.concurrency import (
        RESOURCE_TEXT_PROVIDER,
        memory_pressure_reason,
        report_congestion,
        report_healthy,
    )

    memory_reason = memory_pressure_reason()
    if memory_reason is not None:
        report_congestion(RESOURCE_TEXT_PROVIDER, reason=f"memory:{memory_reason}")
        return
    if exc is not None:
        reason = _congestion_reason(exc)
        if reason is not None:
            report_congestion(RESOURCE_TEXT_PROVIDER, reason=reason)
        return
    report_healthy(RESOURCE_TEXT_PROVIDER)


async def run_with_provider_call_slot(
    operation: Callable[[], Awaitable[T]],
    *,
    priority: int | None = None,
    abort_predicate: Callable[[], bool] | None = None,
    on_failure: Callable[[], None] | None = None,
) -> T:
    """Limit actual text-provider requests independently from active workflows.

    A workflow releases this slot between provider retries and while performing
    local validation.  Scene shards can therefore run concurrently without
    multiplying the global provider concurrency configured by operators.

    A caller may deliberately lease the slot around one complete structured
    operation.  Nested provider calls made by that same task are re-entrant;
    the outer lease can therefore publish a batch-abort fence before releasing
    the real process-wide slot.  Runtime guards are intentionally callbacks,
    not durable provider metadata.

    Every attempt (outer or re-entrant) reports its outcome into the shared
    congestion/health state machine so the gate's effective limit can grow or
    shrink with real signal instead of staying pinned to a static setting.
    """
    current_task = asyncio.current_task()
    inherited_lease = _provider_call_slot_lease.get()
    if (
        inherited_lease is not None
        and current_task is not None
        and inherited_lease[0] is current_task
    ):
        predicates = (inherited_lease[1], abort_predicate)
        if any(
            predicate is not None and predicate()
            for predicate in predicates
        ):
            raise asyncio.CancelledError
        try:
            result = await operation()
        except BaseException as exc:
            _report_text_provider_outcome(exc)
            raise
        _report_text_provider_outcome(None)
        return result

    gate = gate_for("text_provider_calls")
    await gate.acquire(
        current_generation_priority() if priority is None else int(priority)
    )
    owner_token = _provider_call_slot_lease.set(
        (current_task, abort_predicate)
    )
    try:
        if abort_predicate is not None and abort_predicate():
            raise asyncio.CancelledError
        try:
            result = await operation()
        except BaseException as exc:
            if on_failure is not None:
                try:
                    on_failure()
                except Exception as callback_exc:  # noqa: BLE001
                    exc.add_note(
                        "provider-slot failure callback failed: "
                        f"{callback_exc!r}"
                    )
            _report_text_provider_outcome(exc)
            raise
        _report_text_provider_outcome(None)
        return result
    finally:
        _provider_call_slot_lease.reset(owner_token)
        gate.release()
