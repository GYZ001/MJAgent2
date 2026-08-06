import asyncio

from app import hiagent


def test_deepseek_chat_uses_deepseek_route(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name="", meta=None):
        calls.append((url, model, key_name))
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "deepseek")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "deepseek-v4-pro")
    monkeypatch.setattr(hiagent.config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setattr(hiagent, "_deepseek_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat([{"role": "user", "content": "hi"}]))

    assert content == "ok"
    assert calls == [("https://api.deepseek.com/v1/chat/completions", "deepseek-v4-pro", "DEEPSEEK_API_KEY")]


def test_harness_custom_text_chat_prefers_streaming(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def fake_stream_or_fallback(
        client,
        url,
        payload,
        *,
        kind,
        model,
        headers=None,
        key_name="",
        meta=None,
        on_token=None,
    ):
        calls.append((url, model, key_name))
        assert meta["gateway"] == "execution_harness"
        assert callable(on_token)
        return {"choices": [{"message": {"content": "ok"}}]}

    async def forbidden_post_json(*_args, **_kwargs):
        raise AssertionError("execution harness text calls should prefer streaming")

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "custom:model_1")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "deepseek-via-gateway")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_args: ("https://gateway.example/v1", {"x": "y"}))
    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(hiagent, "_stream_or_fallback", fake_stream_or_fallback)
    monkeypatch.setattr(hiagent, "_post_json", forbidden_post_json)

    content = asyncio.run(hiagent.chat(
        [{"role": "user", "content": "hi"}],
        call_meta={"gateway": "execution_harness"},
    ))

    assert content == "ok"
    assert calls == [("https://gateway.example/v1/chat/completions", "deepseek-via-gateway", "model:deepseek-via-gateway")]
