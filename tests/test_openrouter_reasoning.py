import asyncio
import json

import pytest

from app import hiagent


def test_openrouter_retries_without_reasoning_when_budget_is_exhausted(monkeypatch) -> None:
    calls: list[tuple[dict, int, dict | None]] = []
    responses = [
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": None, "reasoning": "thinking until the limit"},
            }],
            "usage": {"completion_tokens": 6000},
        },
        {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]},
    ]

    async def fake_post_json(client, url, payload, *, kind, model, retries=2,
                             headers=None, key_name="", meta=None):
        calls.append((payload, retries, meta))
        return responses.pop(0)

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "openrouter")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "z-ai/glm-5.2")
    monkeypatch.setattr(hiagent.config, "OPENROUTER_TEXT_REASONING_EFFORT", "high")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *args: ("https://openrouter.test/api/v1", {"Authorization": "Bearer test"}))
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat(
        [{"role": "user", "content": "return json"}],
        max_tokens=6000,
        call_meta={"stage": "分镜脚本"},
    ))

    assert content == '{"ok": true}'
    assert calls[0][0]["reasoning"] == {"effort": "high"}
    assert "reasoning" not in calls[1][0]
    assert calls[1][0]["temperature"] == 0.7
    assert calls[1][1] == 0
    assert calls[1][2]["reasoning_fallback"] is True
    assert calls[1][2]["reasoning_fallback_cause"] == "reasoning_budget_exhausted"


def test_nonempty_length_response_is_rejected_as_truncated(monkeypatch) -> None:
    calls = 0

    async def fake_post_json(client, url, payload, *, kind, model, retries=2,
                             headers=None, key_name="", meta=None):
        nonlocal calls
        calls += 1
        return {
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "content": (
                        '{"shard_id":"SS004","scenes":['
                        '{"key":"bp-sc013"}'
                    ),
                },
            }],
            "usage": {
                "prompt_tokens": 14507,
                "completion_tokens": 4526,
                "total_tokens": 19033,
            },
        }

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "openrouter")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda kind, provider=None: "vendor/ss004-replay",
    )
    monkeypatch.setattr(
        hiagent.config,
        "OPENROUTER_TEXT_REASONING_EFFORT",
        "none",
    )
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *args: (
            "https://openrouter.test/api/v1",
            {"Authorization": "Bearer test"},
        ),
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    with pytest.raises(hiagent.ProviderError, match="finish_reason=length") as exc:
        asyncio.run(hiagent.chat(
            [{"role": "user", "content": "return SS004 json"}],
            max_tokens=4526,
        ))

    assert exc.value.failure_kind == "output_truncated"
    assert exc.value.retryable is False
    assert exc.value.replay_safe is False
    assert calls == 1


def test_custom_provider_nonempty_length_response_is_rejected(
    monkeypatch,
) -> None:
    async def fake_post_json(client, url, payload, *, kind, model, retries=2,
                             headers=None, key_name="", meta=None):
        return {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": '{"shard_id":"SS004"'},
            }],
            "usage": {"completion_tokens": 4526},
        }

    monkeypatch.setattr(
        hiagent,
        "active_provider",
        lambda kind: "custom:ss004",
    )
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda kind, provider=None: "vendor/ss004-replay",
    )
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *args: (
            "https://custom.test/v1",
            {"Authorization": "Bearer test"},
        ),
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    with pytest.raises(hiagent.ProviderError) as exc:
        asyncio.run(hiagent.chat(
            [{"role": "user", "content": "return SS004 json"}],
            max_tokens=4526,
        ))

    assert exc.value.failure_kind == "output_truncated"
    assert "finish_reason=length" in str(exc.value)


def test_openrouter_does_not_retry_unrelated_empty_content(monkeypatch) -> None:
    calls = 0

    async def fake_post_json(client, url, payload, *, kind, model, retries=2,
                             headers=None, key_name="", meta=None):
        nonlocal calls
        calls += 1
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": None}}],
            "usage": {"completion_tokens": 0},
        }

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "openrouter")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "z-ai/glm-5.2")
    monkeypatch.setattr(hiagent.config, "OPENROUTER_TEXT_REASONING_EFFORT", "high")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *args: ("https://openrouter.test/api/v1", {"Authorization": "Bearer test"}))
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    try:
        asyncio.run(hiagent.chat([{"role": "user", "content": "return json"}]))
    except hiagent.ProviderError as exc:
        assert "finish_reason=stop" in str(exc)
        assert "reasoning_present=False" in str(exc)
    else:
        raise AssertionError("expected ProviderError")
    assert calls == 1


def test_reasoning_fallback_never_replays_an_identical_deepseek_request(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2,
                             headers=None, key_name="", meta=None):
        calls.append(payload)
        return {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": None, "reasoning_content": "thinking"},
            }],
            "usage": {"completion_tokens": 32768},
        }

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "deepseek")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "deepseek-reasoner")
    monkeypatch.setattr(
        hiagent, "_model_connection",
        lambda *args: ("https://deepseek.test/v1", {"Authorization": "Bearer test"}),
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    try:
        asyncio.run(hiagent.chat([{"role": "user", "content": "return json"}]))
    except hiagent.ProviderError as exc:
        assert "不支持关闭推理" in str(exc)
    else:
        raise AssertionError("expected ProviderError")

    assert len(calls) == 1


def test_text_requests_are_capped_by_active_model_output_limit(monkeypatch) -> None:
    captured: dict = {}
    provider = "custom:model_cap"
    model = "vendor/model-cap"

    async def fake_post_json(client, url, payload, *, kind, model, retries=2,
                             headers=None, key_name="", meta=None):
        captured["payload"] = payload
        captured["meta"] = meta
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    settings = {
        "custom_models": json.dumps([{
            "id": "model_cap",
            "provider": provider,
            "model": model,
            "kinds": ["text"],
            "context_window_tokens": 262144,
            "max_output_tokens": 16384,
            "token_limits_source": "provider_metadata",
        }]),
    }
    monkeypatch.setattr(hiagent, "get_setting", lambda key: settings.get(key, ""))
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: provider)
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: model)
    monkeypatch.setattr(
        hiagent, "_model_connection",
        lambda *args: ("https://custom.test/v1", {"Authorization": "Bearer test"}),
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat(
        [{"role": "user", "content": "return json"}],
        max_tokens=65535,
    ))

    assert content == "ok"
    assert captured["payload"]["max_tokens"] == 16384
    assert captured["meta"]["requested_max_tokens"] == 65535
    assert captured["meta"]["effective_max_tokens"] == 16384


def test_text_requests_use_provider_output_capacity(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post_json(client, url, payload, *, kind, model, retries=2,
                             headers=None, key_name="", meta=None):
        captured["payload"] = payload
        captured["meta"] = meta
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    settings = {
        "model_token_capabilities": json.dumps({
            "builtin:openrouter:vendor/large-output": {
                "context_window_tokens": 262144,
                "max_output_tokens": 49152,
                "token_limits_source": "provider_metadata",
            },
        }),
    }
    monkeypatch.setattr(hiagent, "get_setting", lambda key: settings.get(key, ""))
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "openrouter")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "vendor/large-output")
    monkeypatch.setattr(hiagent.config, "OPENROUTER_TEXT_REASONING_EFFORT", "none")
    monkeypatch.setattr(
        hiagent, "_model_connection",
        lambda *args: ("https://openrouter.test/v1", {"Authorization": "Bearer test"}),
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    assert asyncio.run(hiagent.chat(
        [{"role": "user", "content": "return json"}], max_tokens=65535,
    )) == "ok"
    assert captured["payload"]["max_tokens"] == 49152
    assert captured["meta"]["model_max_output_tokens"] == 49152
    assert captured["meta"]["runtime_output_limit_tokens"] == 49152
