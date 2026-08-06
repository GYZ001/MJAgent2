from __future__ import annotations

import asyncio

from app import generation_concurrency, task_registry


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
