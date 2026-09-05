"""文本对话的流式中断（[DONE] 前断流）在网关内自动重放（2026-09-05）。"""

from __future__ import annotations

import asyncio

import pytest

from app import config, hiagent
from app.harness import model_gateway, model_gateway_moderation


def _interrupted() -> hiagent.ProviderError:
    return hiagent.ProviderError(
        "流式响应在 [DONE] 前中断，结果不确定；已丢弃不完整结果并禁止自动重试，请在页面确认后重试",
        retryable=True, raw="x", failure_kind="stream_interrupted",
        delivery_state="unknown", requires_explicit_retry=True,
    )


def test_stream_interruption_is_replay_safe_for_chat():
    assert model_gateway_moderation.replay_safe_stream_interruption(_interrupted()) is True
    other = hiagent.ProviderError("网络", retryable=True, delivery_state="unknown")
    assert model_gateway_moderation.replay_safe_stream_interruption(other) is False


def test_chat_replays_once_after_stream_interruption(monkeypatch):
    calls = {"n": 0}

    async def fake_chat(messages, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _interrupted()
        return '{"ok": true}'

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(model_gateway_moderation, "get_setting", lambda _key: "", raising=False)
    result = asyncio.run(model_gateway.chat(
        [{"role": "user", "content": "hi"}], call_meta={"stage_key": "test_stream_retry"},
    ))
    assert result == '{"ok": true}'
    assert calls["n"] == 2, "断流后应重放一次，而不是丢弃结果转人工"


def test_chat_gives_up_after_retry_budget(monkeypatch):
    async def always_interrupted(messages, **_kwargs):
        raise _interrupted()

    monkeypatch.setattr(model_gateway.hiagent, "chat", always_interrupted)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 0)
    with pytest.raises(hiagent.ProviderError):
        asyncio.run(model_gateway.chat(
            [{"role": "user", "content": "hi"}], call_meta={"stage_key": "test_stream_retry"},
        ))
