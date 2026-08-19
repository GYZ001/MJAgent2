from __future__ import annotations

import inspect

import pytest

from app import hiagent
from app.harness import model_gateway


def test_chat_accepts_response_format_keyword() -> None:
    parameter = inspect.signature(hiagent.chat).parameters["response_format"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None

    gateway_parameter = inspect.signature(model_gateway.chat).parameters[
        "response_format"
    ]
    assert gateway_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert gateway_parameter.default is None


def test_response_format_unsupported_detection_only_on_explicit_client_rejection() -> None:
    # 明确拒绝 response_format 字段的客户端错误 → 判定不支持。
    explicit = hiagent.ProviderError(
        "请求被拒绝（HTTP 400）：unknown field response_format",
        retryable=False,
        raw='{"error":{"message":"unsupported parameter: response_format"}}',
    )
    assert hiagent._looks_like_response_format_unsupported(explicit) is True

    # 限流/超时/5xx（retryable）绝不能被误判为能力缺失。
    rate_limited = hiagent.ProviderError(
        "网关限流（HTTP 429）：response_format json_schema",
        retryable=True,
    )
    assert hiagent._looks_like_response_format_unsupported(rate_limited) is False

    # 与 response_format 无关的普通拒绝 → 不判定。
    unrelated = hiagent.ProviderError(
        "请求被拒绝（HTTP 400）：invalid model id",
        retryable=False,
    )
    assert hiagent._looks_like_response_format_unsupported(unrelated) is False


def test_response_format_capability_memory_roundtrip() -> None:
    provider, model = "test-provider", "test-model-cap"
    key = hiagent._response_format_capability_key(provider, model)
    hiagent._RESPONSE_FORMAT_UNSUPPORTED.discard(key)
    try:
        assert hiagent._response_format_known_unsupported(provider, model) is False
        hiagent._remember_response_format_unsupported(provider, model)
        assert hiagent._response_format_known_unsupported(provider, model) is True
    finally:
        hiagent._RESPONSE_FORMAT_UNSUPPORTED.discard(key)


@pytest.mark.asyncio
async def test_expected_json_meta_auto_attaches_json_object(monkeypatch) -> None:
    """任何 expected_json 的业务调用（含直接 chat 的蓝图分片）都应在生成阶段约束 JSON。"""
    captured: dict[str, object] = {}

    async def fake_hiagent_chat(messages, **kwargs):
        captured.update(kwargs)
        return '{"ok": true}'

    async def passthrough_slot(fn):
        return await fn()

    monkeypatch.setattr(hiagent, "chat", fake_hiagent_chat)
    monkeypatch.setattr(
        "app.generation_concurrency.run_with_provider_call_slot",
        passthrough_slot,
    )

    result = await model_gateway.chat(
        [{"role": "user", "content": "x"}],
        call_meta={"expected_json": True, "stage_key": "screenplay_blueprint_shard"},
    )
    assert result == '{"ok": true}'
    assert captured.get("response_format") == {"type": "json_object"}


@pytest.mark.asyncio
async def test_non_json_call_does_not_attach_response_format(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_hiagent_chat(messages, **kwargs):
        captured.update(kwargs)
        return "plain text answer"

    async def passthrough_slot(fn):
        return await fn()

    monkeypatch.setattr(hiagent, "chat", fake_hiagent_chat)
    monkeypatch.setattr(
        "app.generation_concurrency.run_with_provider_call_slot",
        passthrough_slot,
    )

    await model_gateway.chat(
        [{"role": "user", "content": "写一段自由文本"}],
        call_meta={"stage_key": "free_text"},
    )
    assert "response_format" not in captured
