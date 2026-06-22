import asyncio

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
    monkeypatch.setattr(hiagent, "_openrouter_headers", lambda: {"Authorization": "Bearer test"})
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
    monkeypatch.setattr(hiagent, "_openrouter_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    try:
        asyncio.run(hiagent.chat([{"role": "user", "content": "return json"}]))
    except hiagent.ProviderError as exc:
        assert "finish_reason=stop" in str(exc)
        assert "reasoning_present=False" in str(exc)
    else:
        raise AssertionError("expected ProviderError")
    assert calls == 1
