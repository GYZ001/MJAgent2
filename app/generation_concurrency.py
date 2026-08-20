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
    if resource == "text_generation_workflows":
        raw = (
            get_setting("text_generation_workflow_concurrency")
            or get_setting("text_generation_concurrency")
            or get_setting("storyboard_concurrency")
        )
    elif resource == "text_provider_calls":
        raw = get_setting("text_generation_concurrency")
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
        return await operation()

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
            return await operation()
        except BaseException as exc:
            if on_failure is not None:
                try:
                    on_failure()
                except Exception as callback_exc:  # noqa: BLE001
                    exc.add_note(
                        "provider-slot failure callback failed: "
                        f"{callback_exc!r}"
                    )
            raise
    finally:
        _provider_call_slot_lease.reset(owner_token)
        gate.release()
