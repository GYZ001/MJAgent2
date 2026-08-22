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

    # 网关要求 messages 显式提到 JSON 是请求契约错误，不是能力缺失；
    # 不得记入 unsupported 后悄然去掉结构化约束。
    missing_instruction = hiagent.ProviderError(
        "请求被拒绝（HTTP 400）：'messages' must contain the word 'json' "
        "in some form, to use 'response_format' of type 'json_object'",
        retryable=False,
        raw='{"error":{"code":"InvalidParameter"}}',
    )
    assert hiagent._looks_like_response_format_unsupported(missing_instruction) is False


def test_json_object_adds_json_instruction_without_mutating_messages() -> None:
    messages = [{"role": "user", "content": "只返回一个对象"}]

    normalized = hiagent._messages_for_response_format(
        messages, {"type": "json_object"},
    )

    assert messages == [{"role": "user", "content": "只返回一个对象"}]
    assert normalized is not messages
    assert normalized[0]["role"] == "system"
    assert "json" in normalized[0]["content"].lower()
    assert normalized[1:] == messages


def test_json_object_does_not_duplicate_existing_json_instruction() -> None:
    messages = [
        {"role": "system", "content": "Return valid JSON only."},
        {"role": "user", "content": "生成结果"},
    ]

    normalized = hiagent._messages_for_response_format(
        messages, {"type": "json_object"},
    )

    assert normalized == messages
    assert normalized is not messages
    assert sum(
        "json" in str(message.get("content") or "").lower()
        for message in normalized
    ) == 1


def test_non_json_response_format_does_not_change_messages() -> None:
    messages = [{"role": "user", "content": "写一段自由文本"}]

    assert hiagent._messages_for_response_format(messages, None) is messages
    assert hiagent._messages_for_response_format(
        messages, {"type": "text"},
    ) is messages


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
        captured["messages"] = messages
        captured.update(kwargs)
        return '{"ok": true}'

    async def passthrough_slot(fn):
        return await fn()

    monkeypatch.setattr(hiagent, "chat", fake_hiagent_chat)
    monkeypatch.setattr(
        "app.generation_concurrency.run_with_provider_call_slot",
        passthrough_slot,
    )

    original = [{"role": "user", "content": "只返回一个对象"}]
    result = await model_gateway.chat(
        original,
        call_meta={"expected_json": True, "stage_key": "screenplay_blueprint_shard"},
    )
    assert result == '{"ok": true}'
    assert captured.get("response_format") == {"type": "json_object"}
    sent_messages = captured["messages"]
    assert sent_messages is not original
    assert sent_messages[0]["role"] == "system"
    assert "json" in sent_messages[0]["content"].lower()
    assert sent_messages[1:] == original
    assert original == [{"role": "user", "content": "只返回一个对象"}]


@pytest.mark.asyncio
async def test_non_json_call_does_not_attach_response_format(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_hiagent_chat(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return "plain text answer"

    async def passthrough_slot(fn):
        return await fn()

    monkeypatch.setattr(hiagent, "chat", fake_hiagent_chat)
    monkeypatch.setattr(
        "app.generation_concurrency.run_with_provider_call_slot",
        passthrough_slot,
    )

    original = [{"role": "user", "content": "写一段自由文本"}]
    await model_gateway.chat(
        original,
        call_meta={"stage_key": "free_text"},
    )
    assert "response_format" not in captured
    assert captured["messages"] is original


@pytest.mark.asyncio
async def test_json_object_first_provider_payload_contains_json_instruction(
    monkeypatch,
) -> None:
    """The first provider request is valid; no 400-driven fallback is needed."""
    payloads: list[dict] = []

    monkeypatch.setattr(
        hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("hiagent", "test-model", 256),
    )
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {
            "context_window_tokens": 8192,
            "max_output_tokens": 256,
            "token_limits_source": "test",
        },
    )
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args, **_kwargs: ("https://example.invalid", {"x-test": "1"}),
    )
    monkeypatch.setattr(
        hiagent,
        "_cached_successful_provider_response",
        lambda *_args, **_kwargs: None,
    )

    async def fake_request(_client, _url, payload, **_kwargs):
        payloads.append(payload)
        assert payload["response_format"] == {"type": "json_object"}
        assert any(
            "json" in str(message.get("content") or "").lower()
            for message in payload["messages"]
        )
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(hiagent, "_plain_chat_request", fake_request)

    original = [{"role": "user", "content": "只返回对象"}]
    result = await hiagent.chat(
        original,
        response_format={"type": "json_object"},
        max_tokens=256,
        call_meta={"operation_id": "op-json-contract"},
    )

    assert result == '{"ok":true}'
    assert original == [{"role": "user", "content": "只返回对象"}]
    assert len(payloads) == 1


@pytest.mark.asyncio
async def test_response_format_capability_ladder_degrades_step_by_step(
    monkeypatch,
) -> None:
    payloads: list[dict] = []
    provider, model = "hiagent", "required-schema-model"
    capability_key = hiagent._response_format_capability_key(provider, model)
    hiagent._RESPONSE_FORMAT_UNSUPPORTED.discard(capability_key)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "required_schema",
            "strict": True,
            "schema": schema,
        },
    }
    monkeypatch.setattr(
        hiagent,
        "text_request_token_limits",
        lambda **_kwargs: (provider, model, 256),
    )
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {
            "context_window_tokens": 8192,
            "max_output_tokens": 256,
            "token_limits_source": "test",
        },
    )
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args, **_kwargs: (
            "https://example.invalid",
            {"x-test": "1"},
        ),
    )
    monkeypatch.setattr(
        hiagent,
        "_cached_successful_provider_response",
        lambda *_args, **_kwargs: None,
    )

    async def rejected_request(_client, _url, payload, **_kwargs):
        payloads.append(payload)
        raise hiagent.ProviderError(
            "请求被拒绝（HTTP 400）：unsupported parameter json_schema",
            retryable=False,
            raw=(
                '{"error":{"message":"response_format json_schema '
                'is not supported"}}'
            ),
        )

    monkeypatch.setattr(hiagent, "_plain_chat_request", rejected_request)
    hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
    try:
        with pytest.raises(hiagent.ProviderError, match="unsupported parameter"):
            await hiagent.chat(
                [{"role": "user", "content": "Return JSON."}],
                response_format=response_format,
                max_tokens=256,
                call_meta={
                    "operation_id": "op-required-json-schema",
                    "response_format_required": True,
                },
            )
        # 能力阶梯：json_schema → json_object → 纯文本。每一级只在网关明确拒绝
        # 上一级时下探一次，能力缺失按 provider:model 记住，后续调用直接从可用的
        # 那一级开始，不会每次都重新试探。
        assert [item.get("response_format") for item in payloads] == [
            response_format,
            {"type": "json_object"},
            None,
        ]
        assert hiagent._json_schema_known_unsupported(provider, model) is True
        assert (
            hiagent._response_format_known_unsupported(provider, model)
            is True
        )
    finally:
        hiagent._RESPONSE_FORMAT_UNSUPPORTED.discard(capability_key)
        hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
