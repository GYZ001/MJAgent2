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
def detached_trace() -> Iterator[TraceContext]:
    """Run independent background work without inheriting a parent workflow."""
    context = TraceContext()
    token = _TRACE.set(context)
    try:
        yield context
    finally:
        _TRACE.reset(token)


@contextmanager
def bind_trace(
    run_id: str,
    step_run_id: str | None,
    trace_id: str | None = None,
) -> Iterator[TraceContext]:
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


def set_worker_trace(run_id: str | None, step_run_id: str | None) -> TraceContext:
    """Rebind the ambient trace before a persistent worker task handles one job.

    ``app.media_exec.run_job._worker_loop`` is a single long-lived asyncio
    Task that ``await``s many jobs one after another *in the same Task* --
    unlike a per-request handler, it never returns between jobs, so it never
    gets a fresh Context the way ``bind_trace``'s ``with`` block assumes. A
    context manager scoped to "just this job" would require indenting the
    entire multi-hundred-line job handler; this function instead does a plain
    unconditional overwrite, called once at the very top of every job the
    worker picks up, before that job makes any provider call.

    This is not the ContextVar-in-threadpool trap that once made auth
    fail-open (writes inside a *sync* dependency executed via Starlette's
    threadpool never propagate back to the async caller because the thread
    runs a copy of the context). Here, this call and every provider call it
    precedes execute in the *same* asyncio Task and therefore the *same*
    Context -- a plain ``ContextVar.set()`` is visible to them exactly like
    any other Task-local state, no propagation across a thread boundary is
    involved.

    What the plain overwrite must guard against is the worker reusing its
    Context across jobs: without it, a job with no durable ``run_id`` (e.g. a
    legacy pre-migration row) would silently keep showing whatever trace the
    previous job in the same loop iteration left behind. So this is always
    called, even when ``run_id`` is falsy -- that explicitly clears the
    ambient trace back to empty rather than leaving stale identity in place.
    """
    context = TraceContext(
        run_id=str(run_id) if run_id else None,
        step_run_id=str(step_run_id) if step_run_id else None,
        trace_id=new_id("trace") if run_id else None,
    )
    _TRACE.set(context)
    return context
