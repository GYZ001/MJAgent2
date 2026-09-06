"""disable_provider_retries 禁的是"换一次语义答案再摇一次"，未送达的流中断仍按退避重放
（2026-09-06 第 7 轮：身份判定调用带此标志，过载拒绝波里两次快速重采样后整台失败）。"""
from __future__ import annotations

import asyncio

import pytest

from app import config, hiagent
from app.harness import model_gateway


def _interrupted() -> hiagent.ProviderError:
    return hiagent.ProviderError(
        "流式响应在 [DONE] 前中断，结果不确定", retryable=True, failure_kind="stream_interrupted",
        delivery_state="unknown", requires_explicit_retry=True, received_chars=22,
    )


def test_stream_interruption_is_replayed_even_with_semantic_retries_disabled(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 30.0)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_MAX_DELAY", 120.0)
    attempts = 0
    slept: list[float] = []

    async def cut_twice_then_answer(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise _interrupted()
        return "answer"

    async def record_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(model_gateway.hiagent, "chat", cut_twice_then_answer)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", record_sleep)
    result = asyncio.run(model_gateway.chat(
        [{"role": "user", "content": "x"}], call_meta={"disable_provider_retries": True},
    ))
    assert result == "answer" and attempts == 3 and slept == [30.0, 60.0]


def test_not_sent_transport_failure_still_honours_the_flag(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 0.0)
    attempts = 0

    async def always_not_sent(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError(
            "connect failed", retryable=True, failure_kind="connection_failed",
            delivery_state="not_sent", replay_safe=True,
        )

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(model_gateway.hiagent, "chat", always_not_sent)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", no_wait)
    with pytest.raises(hiagent.ProviderError, match="connect failed"):
        asyncio.run(model_gateway.chat(
            [{"role": "user", "content": "x"}], call_meta={"disable_provider_retries": True},
        ))
    assert attempts == 1


def test_backoff_is_capped_by_max_delay(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 5)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 30.0)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_MAX_DELAY", 120.0)
    slept: list[float] = []

    async def always_cut(*_args, **_kwargs):
        raise _interrupted()

    async def record_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(model_gateway.hiagent, "chat", always_cut)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", record_sleep)
    with pytest.raises(hiagent.ProviderError):
        asyncio.run(model_gateway.chat([{"role": "user", "content": "x"}]))
    assert slept == [30.0, 60.0, 120.0, 120.0, 120.0]
