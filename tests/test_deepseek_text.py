import asyncio

from app import hiagent


def test_deepseek_chat_uses_deepseek_route(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name=""):
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
