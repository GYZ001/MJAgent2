import asyncio

from app import hiagent


def test_bailian_fallback_models_skip_failed_free_model() -> None:
    hiagent._BAILIAN_FAILED_MODELS["text"].clear()

    first = "qwen3.7-max-2026-06-08"
    hiagent._remember_bailian_failure("text", first)
    models = hiagent._bailian_fallback_models("text", first)

    assert first not in models
    assert models[:4] == [
        "qwen3.7-max-2026-05-20",
        "qwen3.7-max-2026-05-17",
        "qwen3.7-max-preview",
        "qwen3.7-plus-2026-05-26",
    ]
    assert models[-2:] == ["qwen3.7-max", "qwen3.7-plus"]

    hiagent._BAILIAN_FAILED_MODELS["text"].clear()


def test_bailian_chat_tries_next_model_after_request_failure(monkeypatch) -> None:
    hiagent._BAILIAN_FAILED_MODELS["text"].clear()
    first = "qwen3.7-max-2026-06-08"
    second = "qwen3.7-max-2026-05-20"
    calls: list[str] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name="", meta=None):
        calls.append(model)
        if model == first:
            raise hiagent.ProviderError("quota exhausted")
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "bailian")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: first)
    monkeypatch.setattr(hiagent, "_model_connection", lambda *args: ("https://bailian.test/v1", {}))
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat([{"role": "user", "content": "hi"}]))

    assert content == "ok"
    assert calls == [first, second]
    assert first in hiagent._BAILIAN_FAILED_MODELS["text"]

    hiagent._BAILIAN_FAILED_MODELS["text"].clear()


def test_bailian_stream_tries_next_model_only_before_any_token(monkeypatch) -> None:
    hiagent._BAILIAN_FAILED_MODELS["text"].clear()
    first = "qwen3.7-max-2026-06-08"
    second = "qwen3.7-max-2026-05-20"
    calls: list[str] = []
    tokens: list[tuple[str, str]] = []

    async def fake_stream(client, url, payload, *, model, on_token, **kwargs):
        calls.append(model)
        if model == first:
            raise hiagent.ProviderError("quota exhausted")
        on_token("content", "ok")
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "_model_connection", lambda *args: ("https://bailian.test/v1", {}))
    monkeypatch.setattr(hiagent, "_stream_or_fallback", fake_stream)

    async def run():
        return await hiagent._stream_bailian_chat_with_fallback(
            object(), {"messages": []}, fallback_kind="text", log_kind="chat_tools",
            preferred_model=first, meta=None,
            on_token=lambda kind, text: tokens.append((kind, text)),
        )

    data, model = asyncio.run(run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert model == second
    assert calls == [first, second]
    assert tokens == [("content", "ok")]
    assert first in hiagent._BAILIAN_FAILED_MODELS["text"]

    hiagent._BAILIAN_FAILED_MODELS["text"].clear()


def test_bailian_stream_never_replays_after_a_token_was_emitted(monkeypatch) -> None:
    hiagent._BAILIAN_FAILED_MODELS["text"].clear()
    first = "qwen3.7-max-2026-06-08"
    calls: list[str] = []

    async def fake_stream(client, url, payload, *, model, on_token, **kwargs):
        calls.append(model)
        on_token("content", "partial")
        raise hiagent.ProviderError("connection dropped", retryable=True)

    monkeypatch.setattr(hiagent, "_model_connection", lambda *args: ("https://bailian.test/v1", {}))
    monkeypatch.setattr(hiagent, "_stream_or_fallback", fake_stream)

    async def run():
        return await hiagent._stream_bailian_chat_with_fallback(
            object(), {"messages": []}, fallback_kind="text", log_kind="chat_tools",
            preferred_model=first, meta=None, on_token=lambda *_: None,
        )

    try:
        asyncio.run(run())
        assert False, "已产出 token 后应直接报错"
    except hiagent.ProviderError as exc:
        assert "connection dropped" in str(exc)
    assert calls == [first]

    hiagent._BAILIAN_FAILED_MODELS["text"].clear()
