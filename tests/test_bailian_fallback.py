import asyncio

import pytest

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


def test_bailian_chat_tries_next_model_after_not_sent_failure(monkeypatch) -> None:
    hiagent._BAILIAN_FAILED_MODELS["text"].clear()
    first = "qwen3.7-max-2026-06-08"
    second = "qwen3.7-max-2026-05-20"
    calls: list[str] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name="", meta=None):
        calls.append(model)
        if model == first:
            raise hiagent.ProviderError(
                "connect failed",
                retryable=True,
                failure_kind="connection_failed",
                delivery_state="not_sent",
                replay_safe=True,
            )
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


def test_bailian_stream_tries_next_model_only_when_request_was_not_sent(monkeypatch) -> None:
    hiagent._BAILIAN_FAILED_MODELS["text"].clear()
    first = "qwen3.7-max-2026-06-08"
    second = "qwen3.7-max-2026-05-20"
    calls: list[str] = []
    tokens: list[tuple[str, str]] = []

    async def fake_stream(client, url, payload, *, model, on_token, **kwargs):
        calls.append(model)
        if model == first:
            raise hiagent.ProviderError(
                "connect failed",
                retryable=True,
                failure_kind="connection_failed",
                delivery_state="not_sent",
                replay_safe=True,
            )
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


def test_bailian_does_not_switch_model_after_ambiguous_read_failure(monkeypatch) -> None:
    hiagent._BAILIAN_FAILED_MODELS["text"].clear()
    first = "qwen3.7-max-2026-06-08"
    calls: list[str] = []

    async def fake_post_json(
        client, url, payload, *, kind, model, retries=2, headers=None,
        key_name="", meta=None,
    ):
        calls.append(model)
        raise hiagent.ProviderError(
            "read timeout after delivery",
            retryable=True,
            failure_kind="request_outcome_unknown",
            delivery_state="unknown",
            requires_explicit_retry=True,
        )

    monkeypatch.setattr(
        hiagent, "_model_connection",
        lambda *args: ("https://bailian.test/v1", {}),
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    async def run():
        return await hiagent._post_bailian_chat_with_fallback(
            object(),
            {"messages": []},
            fallback_kind="text",
            log_kind="chat",
            preferred_model=first,
        )

    with pytest.raises(hiagent.ProviderError, match="read timeout") as caught:
        asyncio.run(run())

    assert caught.value.requires_explicit_retry is True
    assert calls == [first]
    assert first not in hiagent._BAILIAN_FAILED_MODELS["text"]


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


def test_bailian_strict_replay_scans_later_successful_candidate(monkeypatch) -> None:
    first = "qwen3.7-max-2026-06-08"
    second = "qwen3.7-max-2026-05-20"
    sends = 0

    def fake_cached(_kind, model, _payload, _meta):
        if model == second:
            return {
                "choices": [{"message": {"content": "replayed"}}],
                "usage": {"completion_tokens": 3},
            }
        return None

    async def forbidden_send(*_args, **_kwargs):
        nonlocal sends
        sends += 1
        raise AssertionError("strict replay must not send")

    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args: ("https://bailian.test/v1", {}),
    )
    monkeypatch.setattr(
        hiagent,
        "_bailian_fallback_models",
        lambda *_args: [first, second],
    )
    monkeypatch.setattr(
        hiagent,
        "_cached_successful_provider_response",
        fake_cached,
    )
    monkeypatch.setattr(hiagent, "_post_json", forbidden_send)

    data, model, reused = asyncio.run(
        hiagent._post_bailian_chat_with_fallback(
            object(),
            {"messages": []},
            fallback_kind="text",
            log_kind="chat",
            preferred_model=first,
            meta={
                "require_cached_successful_operation": True,
                "disable_provider_candidate_fallback": True,
            },
        )
    )

    assert data["choices"][0]["message"]["content"] == "replayed"
    assert model == second
    assert reused is True
    assert sends == 0


def test_bailian_strict_replay_all_miss_never_sends(monkeypatch) -> None:
    sends = 0
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args: ("https://bailian.test/v1", {}),
    )
    monkeypatch.setattr(
        hiagent,
        "_bailian_fallback_models",
        lambda *_args: ["first", "second"],
    )
    monkeypatch.setattr(
        hiagent,
        "_cached_successful_provider_response",
        lambda *_args: None,
    )

    async def forbidden_send(*_args, **_kwargs):
        nonlocal sends
        sends += 1
        raise AssertionError("strict replay must not send")

    monkeypatch.setattr(hiagent, "_post_json", forbidden_send)

    with pytest.raises(hiagent.ProviderError, match="durable provider"):
        asyncio.run(hiagent._post_bailian_chat_with_fallback(
            object(),
            {"messages": []},
            fallback_kind="text",
            log_kind="chat",
            preferred_model="first",
            meta={"require_cached_successful_operation": True},
        ))

    assert sends == 0


def test_bailian_blueprint_mode_disables_fresh_candidate_fallback(monkeypatch) -> None:
    first = "first"
    sends: list[str] = []

    async def not_sent(
        _client, _url, _payload, *, model, **_kwargs,
    ):
        sends.append(model)
        raise hiagent.ProviderError(
            "not sent",
            retryable=True,
            delivery_state="not_sent",
            replay_safe=True,
        )

    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args: ("https://bailian.test/v1", {}),
    )
    monkeypatch.setattr(
        hiagent,
        "_bailian_fallback_models",
        lambda *_args: [first, "second"],
    )
    monkeypatch.setattr(hiagent, "_post_json", not_sent)

    with pytest.raises(hiagent.ProviderError, match="not sent"):
        asyncio.run(hiagent._post_bailian_chat_with_fallback(
            object(),
            {"messages": []},
            fallback_kind="text",
            log_kind="chat",
            preferred_model=first,
            meta={"disable_provider_candidate_fallback": True},
        ))

    assert sends == [first]
