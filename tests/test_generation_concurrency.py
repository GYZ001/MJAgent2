from __future__ import annotations

import asyncio
import logging

import pytest

from app import generation_concurrency, hiagent, task_registry
from app.media_pipeline import concurrency as media_concurrency


def _single_provider_slot(monkeypatch) -> None:
    # "text_provider_calls" 的生效上限现在来自 media_pipeline 那套自适应通道
    # （见 generation_concurrency._configured_limit），不再直读 get_setting；直接
    # 钉死 channel_limit 的返回值，同时把该模块自己的 get_setting 也短路掉，避免
    # report_healthy/report_congestion 在首次初始化通道时打到真实数据库。
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: "1" if key == "text_generation_concurrency" else None,
    )
    monkeypatch.setattr(media_concurrency, "get_setting", lambda key: None)
    monkeypatch.setattr(media_concurrency, "channel_limit", lambda resource: 1)
    monkeypatch.setattr(media_concurrency, "memory_pressure_reason", lambda: None)


def test_screenplay_and_storyboard_share_one_process_wide_limit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: "2" if key == "text_generation_workflow_concurrency" else None,
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
        lambda key: "1" if key == "text_generation_workflow_concurrency" else None,
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
        lambda key: "1" if key == "text_generation_workflow_concurrency" else None,
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


# ---------------------------------------------------------------------------
# L2 自适应拥塞反馈环：复用 app/media_pipeline/concurrency.py 的通道状态机，
# 而不是钉死 text_generation_concurrency 静态值。
# ---------------------------------------------------------------------------


def _reset_text_provider_channel(monkeypatch, *, hard_limit: str = "8") -> None:
    media_concurrency._channels.pop(media_concurrency.RESOURCE_TEXT_PROVIDER, None)
    monkeypatch.setattr(
        media_concurrency,
        "get_setting",
        lambda key: hard_limit if key == "text_generation_concurrency" else None,
    )
    monkeypatch.setattr(media_concurrency, "memory_pressure_reason", lambda: None)


def test_congestion_reason_classifies_429_5xx_timeout_and_ignores_other_failures() -> None:
    rate_limited = hiagent.ProviderError(
        "网关限流（HTTP 429）", retryable=True, failure_kind="rate_limited",
    )
    upstream_5xx = hiagent.ProviderError(
        "网关/上游故障（HTTP 504）", retryable=True, failure_kind="upstream_unavailable",
    )
    outcome_unknown_timeout = hiagent.ProviderError(
        "流式调用读超时", retryable=True, failure_kind="request_outcome_unknown",
    )
    # _FirstTokenTimeout(TimeoutError) 与 asyncio.TimeoutError 都是内建 TimeoutError
    # 的子类；直接用 TimeoutError 覆盖同一条 isinstance 分支，不用伸进 hiagent 私有类。
    first_token_timeout = TimeoutError("首字超时")
    # 不是拥塞证据：业务校验失败、以及协议/数据完整性问题（重试也解决不了拥塞）。
    business_failure = ValueError("结构化输出校验失败")
    stream_interrupted = hiagent.ProviderError(
        "流式响应在 [DONE] 前中断", retryable=True, failure_kind="stream_interrupted",
    )

    assert generation_concurrency._congestion_reason(rate_limited) == "rate_limited"
    assert generation_concurrency._congestion_reason(upstream_5xx) == "upstream_unavailable"
    assert (
        generation_concurrency._congestion_reason(outcome_unknown_timeout)
        == "request_outcome_unknown"
    )
    assert generation_concurrency._congestion_reason(first_token_timeout) == "timeout"
    assert generation_concurrency._congestion_reason(business_failure) is None
    assert generation_concurrency._congestion_reason(stream_interrupted) is None


def test_provider_call_slot_halves_channel_limit_after_two_congestion_failures(
    monkeypatch,
) -> None:
    _reset_text_provider_channel(monkeypatch)

    async def scenario() -> tuple[int, int]:
        before = media_concurrency.channel_limit(media_concurrency.RESOURCE_TEXT_PROVIDER)

        async def failing() -> None:
            raise hiagent.ProviderError(
                "网关限流（HTTP 429）", retryable=True, failure_kind="rate_limited",
            )

        for _ in range(2):
            with pytest.raises(hiagent.ProviderError):
                await generation_concurrency.run_with_provider_call_slot(failing)
        after = media_concurrency.channel_limit(media_concurrency.RESOURCE_TEXT_PROVIDER)
        return before, after

    before, after = asyncio.run(scenario())
    assert before == 8
    assert after == 4  # 连续两次 429 触发减半


def test_provider_call_slot_does_not_downgrade_on_non_congestion_failure(
    monkeypatch,
) -> None:
    _reset_text_provider_channel(monkeypatch)

    async def scenario() -> int:
        async def failing() -> None:
            raise ValueError("结构化输出校验失败")

        for _ in range(5):
            with pytest.raises(ValueError):
                await generation_concurrency.run_with_provider_call_slot(failing)
        return media_concurrency.channel_limit(media_concurrency.RESOURCE_TEXT_PROVIDER)

    limit = asyncio.run(scenario())
    assert limit == 8  # 业务失败不是拥塞证据，不触发降档


def test_memory_pressure_downgrades_even_on_successful_call(monkeypatch) -> None:
    _reset_text_provider_channel(monkeypatch)
    monkeypatch.setattr(
        media_concurrency,
        "memory_pressure_reason",
        lambda: "available_memory=100MB < floor=512MB",
    )

    async def scenario() -> int:
        async def ok() -> str:
            return "ok"

        for _ in range(2):
            result = await generation_concurrency.run_with_provider_call_slot(ok)
            assert result == "ok"
        return media_concurrency.channel_limit(media_concurrency.RESOURCE_TEXT_PROVIDER)

    after = asyncio.run(scenario())
    assert after == 4  # 调用本身成功也挡不住内存拥塞信号触发降档


def test_congestion_downgrade_and_memory_pressure_log_a_visible_warning(
    monkeypatch, caplog,
) -> None:
    _reset_text_provider_channel(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.media_pipeline.concurrency"):
        media_concurrency.report_congestion(
            media_concurrency.RESOURCE_TEXT_PROVIDER, reason="rate_limited",
        )
        media_concurrency.report_congestion(
            media_concurrency.RESOURCE_TEXT_PROVIDER, reason="rate_limited",
        )

    assert "concurrency-downgrade" in caplog.text
    assert media_concurrency.RESOURCE_TEXT_PROVIDER in caplog.text
    assert "rate_limited" in caplog.text


def test_memory_pressure_reason_uses_measured_floor(monkeypatch) -> None:
    floor = media_concurrency.MEMORY_AVAILABLE_FLOOR_KB
    monkeypatch.setattr(media_concurrency, "_available_memory_kb", lambda: floor - 1)
    reason = media_concurrency.memory_pressure_reason()
    assert reason is not None
    assert "available_memory" in reason

    monkeypatch.setattr(media_concurrency, "_available_memory_kb", lambda: floor + 1)
    assert media_concurrency.memory_pressure_reason() is None

    # 读取失败（非 Linux/权限问题）必须是"这次不检查"，不能被当成"内存充足"
    # 而静默放行——但也不能因此假装拥塞；两种情形都用同一个 None 表达。
    monkeypatch.setattr(media_concurrency, "_available_memory_kb", lambda: None)
    assert media_concurrency.memory_pressure_reason() is None


def test_report_healthy_grows_channel_after_ten_minutes_without_congestion(
    monkeypatch,
) -> None:
    _reset_text_provider_channel(monkeypatch)
    state = media_concurrency.ensure_channel(media_concurrency.RESOURCE_TEXT_PROVIDER)
    state.current = 4  # 模拟此前已经降档过

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(media_concurrency.time, "time", lambda: clock["now"])

    media_concurrency.report_healthy(media_concurrency.RESOURCE_TEXT_PROVIDER)
    assert state.current == 4  # 第一次健康只记起点，不涨

    clock["now"] += 601.0
    media_concurrency.report_healthy(media_concurrency.RESOURCE_TEXT_PROVIDER)
    assert state.current == 5  # 连续 10 分钟健康后 +1，直到 hard_limit


def test_configured_limit_for_text_provider_calls_delegates_to_adaptive_channel(
    monkeypatch,
) -> None:
    _reset_text_provider_channel(monkeypatch)
    monkeypatch.setattr(media_concurrency, "channel_limit", lambda resource: 3)
    assert generation_concurrency._configured_limit("text_provider_calls") == 3

    # 硬顶（MAX_TEXT_GENERATION_CONCURRENCY）仍然生效，不因为改走自适应通道被绕过。
    monkeypatch.setattr(media_concurrency, "channel_limit", lambda resource: 999)
    assert (
        generation_concurrency._configured_limit("text_provider_calls")
        == generation_concurrency.MAX_TEXT_GENERATION_CONCURRENCY
    )
