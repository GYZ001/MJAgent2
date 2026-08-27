import asyncio

from app import hiagent


def test_zhipu_chat_uses_official_route(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name="", meta=None):
        calls.append((url, model, key_name))
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "glm-5.2")
    monkeypatch.setattr(hiagent.config, "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setattr(hiagent, "_zhipu_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat([{"role": "user", "content": "hi"}]))

    assert content == "ok"
    assert calls == [("https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-5.2", "ZHIPU_API_KEY")]


def test_zhipu_disable_thinking_sends_thinking_disabled(monkeypatch) -> None:
    payloads: list[dict] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name="", meta=None):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "glm-5.3-flash")
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {
            "context_window_tokens": 131072,
            "max_output_tokens": 32768,
            "token_limits_source": "test",
        },
    )
    monkeypatch.setattr(hiagent.config, "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setattr(hiagent, "_zhipu_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat(
        [{"role": "user", "content": "hi"}],
        call_meta={"disable_thinking": True, "stage_key": "character_roll_call"},
        max_tokens=512,
    ))

    assert content == "ok"
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["max_tokens"] == 512
