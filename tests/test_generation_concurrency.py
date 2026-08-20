from __future__ import annotations

import asyncio

import pytest

from app import generation_concurrency, hiagent, task_registry


def _single_provider_slot(monkeypatch) -> None:
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: "1" if key == "text_generation_concurrency" else None,
    )


def test_screenplay_and_storyboard_share_one_process_wide_limit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: "2" if key == "storyboard_concurrency" else None,
    )

    async def scenario() -> int:
        active = 0
        peak = 0
        release = asyncio.Event()
        first_two_started = asyncio.Event()

        async def operation() -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                first_two_started.set()
            await release.wait()
            active -= 1

        tasks = [
            asyncio.create_task(
                generation_concurrency.run_with_generation_slot(
                    workflow_type,
                    operation,
                )
            )
            for workflow_type in (
                "screenplay",
                "storyboard",
                "screenplay",
                "storyboard",
                "screenplay",
            )
        ]
        await asyncio.wait_for(first_two_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert active == 2
        release.set()
        await asyncio.gather(*tasks)
        return peak

    assert asyncio.run(scenario()) == 2


def test_live_resize_releases_existing_generation_waiters(monkeypatch) -> None:
    configured = {"value": "2"}
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: configured["value"]
        if key == "text_generation_concurrency"
        else None,
    )

    async def scenario() -> int:
        active = 0
        peak = 0
        release = asyncio.Event()
        first_two_started = asyncio.Event()
        all_started = asyncio.Event()

        async def operation() -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                first_two_started.set()
            if active == 10:
                all_started.set()
            await release.wait()
            active -= 1

        tasks = [
            asyncio.create_task(
                generation_concurrency.run_with_generation_slot(
                    "screenplay",
                    operation,
                    priority=generation_concurrency.PRIORITY_BATCH,
                )
            )
            for _ in range(10)
        ]
        await asyncio.wait_for(first_two_started.wait(), timeout=1)
        assert active == 2

        configured["value"] = "10"
        assert generation_concurrency.reload_generation_limits() >= 1
        await asyncio.wait_for(all_started.wait(), timeout=1)
        assert active == 10

        release.set()
        await asyncio.gather(*tasks)
        return peak

    assert asyncio.run(scenario()) == 10


def test_interactive_generation_jumps_ahead_of_queued_batch_work(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: "1" if key == "storyboard_concurrency" else None,
    )

    async def scenario() -> list[str]:
        order: list[str] = []
        releases = {
            name: asyncio.Event()
            for name in ("batch-active", "batch-waiting", "interactive")
        }
        started = {
            name: asyncio.Event()
            for name in releases
        }

        async def operation(name: str) -> None:
            order.append(name)
            started[name].set()
            await releases[name].wait()

        active = asyncio.create_task(
            generation_concurrency.run_with_generation_slot(
                "screenplay",
                lambda: operation("batch-active"),
                priority=generation_concurrency.PRIORITY_BATCH,
            )
        )
        await asyncio.wait_for(started["batch-active"].wait(), timeout=1)
        batch_waiting = asyncio.create_task(
            generation_concurrency.run_with_generation_slot(
                "screenplay",
                lambda: operation("batch-waiting"),
                priority=generation_concurrency.PRIORITY_BATCH,
            )
        )
        interactive = asyncio.create_task(
            generation_concurrency.run_with_generation_slot(
                "storyboard",
                lambda: operation("interactive"),
                priority=generation_concurrency.PRIORITY_INTERACTIVE,
            )
        )
        await asyncio.sleep(0)

        releases["batch-active"].set()
        await asyncio.wait_for(started["interactive"].wait(), timeout=1)
        releases["interactive"].set()
        await asyncio.wait_for(started["batch-waiting"].wait(), timeout=1)
        releases["batch-waiting"].set()
        await asyncio.gather(active, batch_waiting, interactive)
        return order

    assert asyncio.run(scenario()) == [
        "batch-active",
        "interactive",
        "batch-waiting",
    ]


def test_two_phase_batch_cancel_does_not_start_waiters(monkeypatch) -> None:
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: "1" if key == "storyboard_concurrency" else None,
    )

    async def scenario() -> list[str]:
        started: list[str] = []
        hold = asyncio.Event()

        async def operation(name: str) -> None:
            started.append(name)
            await hold.wait()

        tasks = []
        for name in ("one", "two", "three"):
            task = asyncio.create_task(
                generation_concurrency.run_with_generation_slot(
                    "screenplay",
                    lambda name=name: operation(name),
                    priority=generation_concurrency.PRIORITY_BATCH,
                )
            )
            task_registry.register("screenplay", name, task, project_id="p1")
            tasks.append(task)
        while not started:
            await asyncio.sleep(0)
        assert await task_registry.cancel_many_and_wait(
            "screenplay",
            ["one", "two", "three"],
        ) == 3
        await asyncio.gather(*tasks, return_exceptions=True)
        return started

    assert asyncio.run(scenario()) == ["one"]


def test_provider_slot_is_reentrant_for_same_structured_operation(
    monkeypatch,
) -> None:
    _single_provider_slot(monkeypatch)

    async def scenario() -> list[str]:
        calls: list[str] = []

        async def nested_provider_call() -> str:
            calls.append("provider")
            return "ok"

        async def structured_operation() -> str:
            calls.append("structured")
            return await generation_concurrency.run_with_provider_call_slot(
                nested_provider_call
            )

        result = await asyncio.wait_for(
            generation_concurrency.run_with_provider_call_slot(
                structured_operation
            ),
            timeout=1,
        )
        assert result == "ok"
        return calls

    assert asyncio.run(scenario()) == ["structured", "provider"]


def test_reentrant_provider_attempt_inherits_outer_abort_predicate(
    monkeypatch,
) -> None:
    _single_provider_slot(monkeypatch)

    async def scenario() -> tuple[BaseException, list[int]]:
        abort = asyncio.Event()
        attempts: list[int] = []

        async def provider_attempt(attempt: int) -> str:
            attempts.append(attempt)
            if attempt == 0:
                abort.set()
                return "malformed"
            raise AssertionError("retry crossed the inherited abort fence")

        async def structured_operation() -> None:
            await generation_concurrency.run_with_provider_call_slot(
                lambda: provider_attempt(0)
            )
            await generation_concurrency.run_with_provider_call_slot(
                lambda: provider_attempt(1)
            )

        result = await asyncio.gather(
            generation_concurrency.run_with_provider_call_slot(
                structured_operation,
                abort_predicate=abort.is_set,
            ),
            return_exceptions=True,
        )
        return result[0], attempts

    result, attempts = asyncio.run(scenario())
    assert isinstance(result, asyncio.CancelledError)
    assert attempts == [0]


@pytest.mark.parametrize("failure_kind", ["provider", "local_validation"])
def test_provider_slot_publishes_abort_before_waking_queued_peer(
    monkeypatch,
    failure_kind: str,
) -> None:
    _single_provider_slot(monkeypatch)

    async def scenario() -> tuple[
        BaseException,
        BaseException,
        BaseException,
        list[str],
    ]:
        abort = asyncio.Event()
        release_owner = asyncio.Event()
        owner_started = asyncio.Event()
        calls: list[str] = []
        if failure_kind == "provider":
            original: BaseException = hiagent.ProviderError(
                "63178 length",
                failure_kind="output_truncated",
            )
        else:
            original = RuntimeError("local structured validation failed")

        async def owner_operation() -> None:
            calls.append("owner-ledger")
            owner_started.set()
            await release_owner.wait()
            raise original

        async def peer_operation() -> None:
            calls.append("peer-ledger")

        owner = asyncio.create_task(
            generation_concurrency.run_with_provider_call_slot(
                owner_operation,
                on_failure=abort.set,
            )
        )
        await asyncio.wait_for(owner_started.wait(), timeout=1)
        peer = asyncio.create_task(
            generation_concurrency.run_with_provider_call_slot(
                peer_operation,
                abort_predicate=abort.is_set,
            )
        )
        gate = generation_concurrency.gate_for("text_provider_calls")
        while not gate.waiters:
            await asyncio.sleep(0)

        release_owner.set()
        owner_result, peer_result = await asyncio.gather(
            owner,
            peer,
            return_exceptions=True,
        )
        return original, owner_result, peer_result, calls

    original, owner_result, peer_result, calls = asyncio.run(scenario())
    assert owner_result is original
    assert isinstance(peer_result, asyncio.CancelledError)
    assert calls == ["owner-ledger"]


def test_provider_slot_preserves_user_cancellation_and_fences_peer(
    monkeypatch,
) -> None:
    _single_provider_slot(monkeypatch)

    async def scenario() -> tuple[BaseException, BaseException, list[str]]:
        abort = asyncio.Event()
        owner_started = asyncio.Event()
        calls: list[str] = []

        async def owner_operation() -> None:
            calls.append("owner-ledger")
            owner_started.set()
            await asyncio.Future()

        async def peer_operation() -> None:
            calls.append("peer-ledger")

        owner = asyncio.create_task(
            generation_concurrency.run_with_provider_call_slot(
                owner_operation,
                on_failure=abort.set,
            )
        )
        await asyncio.wait_for(owner_started.wait(), timeout=1)
        peer = asyncio.create_task(
            generation_concurrency.run_with_provider_call_slot(
                peer_operation,
                abort_predicate=abort.is_set,
            )
        )
        gate = generation_concurrency.gate_for("text_provider_calls")
        while not gate.waiters:
            await asyncio.sleep(0)

        owner.cancel()
        owner_result, peer_result = await asyncio.gather(
            owner,
            peer,
            return_exceptions=True,
        )
        return owner_result, peer_result, calls

    owner_result, peer_result, calls = asyncio.run(scenario())
    assert isinstance(owner_result, asyncio.CancelledError)
    assert isinstance(peer_result, asyncio.CancelledError)
    assert calls == ["owner-ledger"]
