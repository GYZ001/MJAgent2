"""Generic async batching and hashing primitives shared across the scene-shard
pipeline: fail-fast task-group cancellation (``_gather_fail_fast`` /
``_FailFastScope``), the real-provider-slot lease wrapper
(``_SceneStructuredOperationGate``), the undelivered-answer retry
(``_scene_structured_with_undelivered_retry``), and small hash/setting
helpers used everywhere else in the package.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from app import (
    generation_concurrency,
    hiagent,
)
from app.db import get_setting
from collections.abc import (
    Awaitable,
    Callable,
)
from typing import Any

from .constants import (
    SCENE_SHARD_UNDELIVERED_BACKOFF_S,
    SCENE_SHARD_UNDELIVERED_RETRIES,
)


async def _scene_structured_with_undelivered_retry(
    call: Callable[[str], Awaitable[Any]],
    *,
    operation_id: str,
) -> Any:
    """Re-issue one scene-shard call whose answer was never delivered.

    A stall before the first streamed character, or a stream cut before the
    provider's own ``[DONE]`` (whose partial text the transport discards),
    leaves nothing authored to preserve -- so a fresh attempt under its own
    operation id is not an answer being re-rolled until it passes.  Anything
    the provider did deliver, including a candidate that failed validation,
    still fails on the first call.

    The retry deliberately sits *inside* the provider-slot lease: the lease is
    re-entrant for the same task, and the batch-abort callback fires when the
    lease sees a failure.  Retrying outside it would tear the whole batch down
    before the second attempt could run.
    """
    last_error: hiagent.ProviderError | None = None
    for attempt in range(SCENE_SHARD_UNDELIVERED_RETRIES + 1):
        if attempt:
            await asyncio.sleep(
                SCENE_SHARD_UNDELIVERED_BACKOFF_S[attempt - 1]
            )
        try:
            return await call(
                operation_id
                if not attempt
                else f"{operation_id}:undelivered:{attempt}"
            )
        except hiagent.ProviderError as exc:
            if not hiagent.provider_answer_undelivered(exc):
                raise
            last_error = exc
    assert last_error is not None
    raise hiagent.deterministic_undelivered_error(
        last_error, attempts=SCENE_SHARD_UNDELIVERED_RETRIES + 1,
    ) from last_error


class _FailFastScope:
    """Own one task batch and synchronously cancel peers on its first failure."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []
        self.failure_owner: asyncio.Task[Any] | None = None

    def bind(self, tasks: list[asyncio.Task[Any]]) -> None:
        if self.tasks:
            raise RuntimeError("fail-fast scope is already bound")
        self.tasks = tasks

    def fail(self, owner: asyncio.Task[Any]) -> bool:
        if self.failure_owner is not None:
            return False
        self.failure_owner = owner
        for peer in self.tasks:
            if peer is not owner and not peer.done():
                peer.cancel()
        return True


class _SceneStructuredOperationGate:
    """Lease the real provider slot for one complete structured operation."""

    def __init__(self, batch_abort: asyncio.Event) -> None:
        self._batch_abort = batch_abort

    async def run(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        on_failure: Callable[[], None] | None = None,
    ) -> Any:
        def abort() -> None:
            self._batch_abort.set()
            if on_failure is not None:
                on_failure()

        return await generation_concurrency.run_with_provider_call_slot(
            operation,
            abort_predicate=self._batch_abort.is_set,
            on_failure=abort,
        )


async def _gather_fail_fast(
    *factories: Callable[[], Awaitable[Any]],
    scope: _FailFastScope | None = None,
    on_failure: Callable[[], None] | None = None,
    cascades: Callable[[BaseException], bool] | None = None,
) -> list[Any]:
    """Run a batch in input order, cancelling and joining it on first failure.

    ``asyncio.gather`` propagates a child exception without cancelling its
    siblings.  That is unsafe for paid generation: those detached siblings can
    continue into later provider calls after the batch has already failed.

    ``cascades`` decides, per raised exception, whether that failure should
    cancel the rest of the batch.  Its default (``None``) treats every
    failure as cancel-worthy -- the historical behaviour, unchanged for every
    caller that does not pass it.  A caller that needs to isolate unrelated
    siblings (one shard's content failure must not cancel another shard's
    still-running work) passes a narrower predicate: exceptions it rejects
    fail only their own factory -- siblings keep running to their own
    outcome -- while the batch still waits for everything to finish and then
    raises the first such deferred failure, so a quiet return never hides
    one.
    """
    active_scope = scope or _FailFastScope()
    should_cascade = cascades if cascades is not None else (lambda _exc: True)
    tasks: list[asyncio.Task[Any]] = []

    async def run_child(factory: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await factory()
        except BaseException as exc:
            current = asyncio.current_task()
            if (
                current is not None
                and should_cascade(exc)
                and active_scope.fail(current)
            ):
                if on_failure is not None:
                    try:
                        on_failure()
                    except Exception as callback_exc:  # noqa: BLE001
                        exc.add_note(
                            "fail-fast propagation callback failed: "
                            f"{callback_exc!r}"
                        )
            raise

    for factory in factories:
        tasks.append(asyncio.create_task(run_child(factory)))
    active_scope.bind(tasks)
    pending = set(tasks)
    deferred_failure: asyncio.Task[Any] | None = None
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            owner = active_scope.failure_owner
            if owner is not None:
                if not owner.done():
                    # A batch can also be told about its failure from an abort
                    # callback that fires while the owning task is still
                    # unwinding -- there the owner is registered before it has
                    # any result at all.  Calling ``result()`` then raises
                    # asyncio's "Result is not set" and destroys the real
                    # cause; reporting a cancelled peer instead would hide it
                    # just as thoroughly.  The owner is still pending and its
                    # peers are already cancelled, so waiting one more round
                    # yields the actual exception.
                    continue
                owner.result()
            for task in done:
                # A non-cascading failure (per ``should_cascade``) never
                # became the scope's owner and never cancelled its peers, so
                # it will not surface via the ``owner`` branch above.  Record
                # the first one and keep waiting -- this task's siblings are
                # still entitled to reach their own outcome.
                if task.cancelled():
                    if deferred_failure is None:
                        deferred_failure = task
                    continue
                exc = task.exception()
                if exc is None:
                    continue
                if deferred_failure is None:
                    deferred_failure = task
        if deferred_failure is not None:
            # result() preserves the original exception (including
            # CancelledError) instead of wrapping it in an ExceptionGroup.
            deferred_failure.result()
        return [task.result() for task in tasks]
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Retrieve every terminal outcome before propagating the original one;
        # this prevents detached work and "exception was never retrieved".
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _setting_int(key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(get_setting(key) or default)))
    except (TypeError, ValueError):
        return default
