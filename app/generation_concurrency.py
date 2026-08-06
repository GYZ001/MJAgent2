"""Process-wide concurrency gates for long-running text generation workflows."""
from __future__ import annotations

import asyncio
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
        while self.waiters:
            _priority, _sequence, future = heapq.heappop(self.waiters)
            if future.cancelled():
                continue
            future.set_result(None)
            return
        self.active = max(0, self.active - 1)


_loop_gates: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, _PriorityGate],
] = weakref.WeakKeyDictionary()


def _resource_key(workflow_type: str) -> str:
    if workflow_type in {"screenplay", "storyboard"}:
        return "text_generation"
    return workflow_type


def _configured_limit(_workflow_type: str) -> int:
    raw = get_setting("storyboard_concurrency")
    try:
        return max(1, int(raw or 2))
    except (TypeError, ValueError):
        return 2


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
    try:
        return await operation()
    finally:
        gate.release()
