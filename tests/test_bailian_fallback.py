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

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name=""):
        calls.append(model)
        if model == first:
            raise hiagent.ProviderError("quota exhausted")
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "bailian")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: first)
    monkeypatch.setattr(hiagent, "_bailian_headers", lambda: {})
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat([{"role": "user", "content": "hi"}]))

    assert content == "ok"
    assert calls == [first, second]
    assert first in hiagent._BAILIAN_FAILED_MODELS["text"]

    hiagent._BAILIAN_FAILED_MODELS["text"].clear()
