from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from app.harness import model_gateway


class _Payload(BaseModel):
    value: int


def test_structured_runner_recovers_complete_trailing_json(monkeypatch) -> None:
    calls = 0
    attempts: list[dict] = []

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return '草稿 {"value":\n最终 {"value":7}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "return json"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.trailing-json:v1:abc",
        max_tokens=128,
        on_attempt=attempts.append,
    ))
    assert result.value == 7
    assert calls == 1
    assert attempts[0]["outcome"] == "validated"
    assert attempts[0]["local_recovery"] is True


def test_structured_runner_uses_one_format_repair(monkeypatch) -> None:
    prompts: list[str] = []
    metas: list[dict] = []

    async def fake_chat(messages, **kwargs):
        prompts.append(messages[0]["content"])
        metas.append(kwargs["call_meta"])
        return "not-json" if len(prompts) == 1 else '{"value":3}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original large context"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.format-repair:v1:abc",
        max_tokens=128,
        format_retry_limit=1,
    ))
    assert result.value == 3
    assert len(prompts) == 2
    assert "Schema" in prompts[1]
    assert "not-json" in prompts[1]
    assert "original large context" not in prompts[1]
    assert [meta["format_attempt"] for meta in metas] == [0, 1]
    assert [meta["semantic_attempt"] for meta in metas] == [0, 0]


def test_structured_runner_semantic_retry_has_independent_budget(monkeypatch) -> None:
    prompts: list[str] = []
    metas: list[dict] = []

    async def fake_chat(messages, **kwargs):
        prompts.append(messages[0]["content"])
        metas.append(kwargs["call_meta"])
        return '{"value":-1}' if len(prompts) == 1 else '{"value":2}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_Payload,
        validate=lambda value: [] if value.value > 0 else ["value 必须为正数"],
        operation_id="test.semantic-retry:v1:abc",
        max_tokens=128,
        format_retry_limit=0,
        semantic_retry_limit=1,
        repair_context="only this context",
    ))
    assert result.value == 2
    assert "value 必须为正数" in prompts[1]
    assert "only this context" in prompts[1]
    assert [meta["format_attempt"] for meta in metas] == [0, 0]
    assert [meta["semantic_attempt"] for meta in metas] == [0, 1]


def test_structured_runner_does_not_accept_malformed_http_200(monkeypatch) -> None:
    async def fake_chat(*_args, **_kwargs):
        return "HTTP 200 but no task object"

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "original"}],
            model_type=_Payload,
            validate=None,
            operation_id="test.malformed:v1:abc",
            max_tokens=128,
            format_retry_limit=0,
        ))
