"""对话类调用未送达时按退避表重放；已送达但不可用的答案不重摇（2026-09-06 第 11 轮第 23 集 read 超时）。"""
from __future__ import annotations

import asyncio

import pytest

from app import config, hiagent
from app.harness import undelivered_replay as ur


def _read_timeout() -> hiagent.ProviderError:
    return hiagent.ProviderError(
        "调用read阶段超时（300010ms）；请求结果不确定，已禁止自动重试", retryable=True,
        failure_kind="request_outcome_unknown", delivery_state="unknown", requires_explicit_retry=True, received_chars=0,
    )


def test_zero_byte_read_timeout_is_replayed_with_capped_backoff(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 30.0)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_MAX_DELAY", 120.0)
    monkeypatch.setattr(ur, "log_provider_call", lambda *a, **kw: None)
    slept: list[float] = []

    async def record(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(ur.asyncio, "sleep", record)
    attempts = 0

    async def op():
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise _read_timeout()
        return "turn"

    assert asyncio.run(ur.replay_undelivered(op)) == "turn"
    assert attempts == 3 and slept == [30.0, 60.0]


def test_delivered_but_unusable_answer_is_not_replayed(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    monkeypatch.setattr(ur, "log_provider_call", lambda *a, **kw: None)
    attempts = 0

    async def op():
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError("业务校验失败", retryable=False, failure_kind="request_outcome_unknown",
                                    delivery_state="responded", received_chars=900)

    with pytest.raises(hiagent.ProviderError, match="业务校验失败"):
        asyncio.run(ur.replay_undelivered(op))
    assert attempts == 1


def test_budget_exhaustion_raises_the_last_error(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(ur, "log_provider_call", lambda *a, **kw: None)
    attempts = 0

    async def op():
        nonlocal attempts
        attempts += 1
        raise _read_timeout()

    with pytest.raises(hiagent.ProviderError, match="read阶段超时"):
        asyncio.run(ur.replay_undelivered(op))
    assert attempts == 3
