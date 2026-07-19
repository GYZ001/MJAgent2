from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.db import new_id


@dataclass(frozen=True, slots=True)
class TraceContext:
    run_id: str | None = None
    step_run_id: str | None = None
    trace_id: str | None = None


_TRACE: ContextVar[TraceContext] = ContextVar("mjagent_trace", default=TraceContext())


def current_trace() -> TraceContext:
    return _TRACE.get()


@contextmanager
def bind_trace(run_id: str, step_run_id: str, trace_id: str | None = None) -> Iterator[TraceContext]:
    context = TraceContext(
        run_id=run_id,
        step_run_id=step_run_id,
        trace_id=trace_id or new_id("trace"),
    )
    token = _TRACE.set(context)
    try:
        yield context
    finally:
        _TRACE.reset(token)
