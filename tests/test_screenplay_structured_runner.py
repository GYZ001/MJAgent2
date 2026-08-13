from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from app.harness import model_gateway


class _Payload(BaseModel):
    value: int


class _Scene(BaseModel):
    story_function: str = Field(min_length=6)


class _ScenePayload(BaseModel):
    scenes: list[_Scene]


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
    assert metas[0]["operation_id"] != metas[1]["operation_id"]
    assert metas[0]["base_operation_id"] == metas[1]["base_operation_id"]


def test_format_repair_keeps_outer_candidate_validation_error(
    monkeypatch,
) -> None:
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        if len(prompts) == 1:
            return '{"scenes":[{"story_function":"setup"}]}'
        return '{"scenes":[{"story_function":"建立本场冲突"}]}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_ScenePayload,
        validate=None,
        operation_id="test.outer-format-error:v1:abc",
        max_tokens=128,
        format_retry_limit=1,
    ))

    assert result.scenes[0].story_function == "建立本场冲突"
    assert "String should have at least 6 characters" in prompts[1]
    assert '"story_function":"setup"' in prompts[1]
    assert "Field required" not in prompts[1]


def test_structured_runner_applies_local_payload_normalizer(
    monkeypatch,
) -> None:
    calls = 0
    attempts: list[dict] = []

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return '{"value":1}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.local-normalizer:v1:abc",
        max_tokens=128,
        normalize_payload=lambda payload: {**payload, "value": 7},
        on_attempt=attempts.append,
    ))

    assert result.value == 7
    assert calls == 1
    assert attempts[0]["local_recovery"] is True


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
    assert metas[0]["operation_id"] != metas[1]["operation_id"]
    assert metas[0]["base_operation_id"] == metas[1]["base_operation_id"]


def test_structured_runner_builds_repair_schema_from_failed_candidate(
    monkeypatch,
) -> None:
    prompts: list[str] = []
    repair_candidates: list[int] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return '{"value":-1}' if len(prompts) == 1 else '{"value":2}'

    def build_repair_schema(value: _Payload) -> dict:
        repair_candidates.append(value.value)
        return {
            "type": "object",
            "x-schema-phase": "semantic-repair",
            "x-failed-value": value.value,
        }

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_Payload,
        validate=lambda value: [] if value.value > 0 else ["value 必须为正数"],
        operation_id="test.dynamic-semantic-schema:v1:abc",
        max_tokens=128,
        semantic_retry_limit=1,
        output_schema={
            "type": "object",
            "x-schema-phase": "initial-output",
        },
        repair_schema=build_repair_schema,
    ))

    assert result.value == 2
    assert repair_candidates == [-1]
    assert '"x-schema-phase": "semantic-repair"' in prompts[1]
    assert '"x-failed-value": -1' in prompts[1]
    assert '"x-schema-phase": "initial-output"' not in prompts[1]


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


def test_structured_runner_does_not_schema_repair_provider_error_envelope(
    monkeypatch,
) -> None:
    calls = 0
    attempts: list[dict] = []

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return '{"error":"content policy rejected"}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(
        model_gateway.StructuredProviderRejection,
        match="content policy rejected",
    ):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "original"}],
            model_type=_Payload,
            validate=None,
            operation_id="test.provider-rejected:v1:abc",
            max_tokens=128,
            format_retry_limit=2,
            on_attempt=attempts.append,
        ))

    assert calls == 1
    assert attempts[0]["outcome"] == "provider_rejected"
