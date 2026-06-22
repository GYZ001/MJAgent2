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
