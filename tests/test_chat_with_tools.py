"""`hiagent.chat_with_tools` 契约：原生 function calling 解析 + 无原生能力时的 JSON 回退。

不打真实网关：native 路径 monkeypatch `_post_json`，回退路径 monkeypatch `chat`。
"""
from __future__ import annotations

import asyncio

import httpx

from app import hiagent

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "resource.read",
            "description": "读取只读资源",
            "parameters": {"type": "object", "properties": {"uri": {"type": "string"}}, "required": ["uri"]},
        },
    }
]


def test_parse_tool_arguments_handles_valid_and_broken_json() -> None:
    assert hiagent._parse_tool_arguments('{"uri": "manju://projects"}') == {"uri": "manju://projects"}
    assert hiagent._parse_tool_arguments({"already": "dict"}) == {"already": "dict"}
    assert hiagent._parse_tool_arguments("not json") == {}
    assert hiagent._parse_tool_arguments(None) == {}


def test_chat_with_tools_parses_native_tool_calls(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None,
                             key_name="", meta=None, idempotency_key=None):
        captured["payload"] = payload
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None,
            "tool_calls": [{
                "id": "call_abc", "type": "function",
                "function": {"name": "resource.read", "arguments": '{"uri": "manju://projects"}'},
            }],
        }}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "glm-5.2")
    monkeypatch.setattr(hiagent, "get_setting", lambda key: "")
    monkeypatch.setattr(hiagent, "_zhipu_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr(hiagent.config, "ZHIPU_BASE_URL", "https://z/api")
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    turn = asyncio.run(hiagent.chat_with_tools([{"role": "user", "content": "hi"}], _TOOLS))

    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.id == "call_abc"
    assert call.name == "resource.read"
    assert call.arguments == {"uri": "manju://projects"}
    assert call.arguments_raw == '{"uri": "manju://projects"}'
    # payload 确实携带原生 tools / tool_choice
    assert captured["payload"]["tools"] == _TOOLS
    assert captured["payload"]["tool_choice"] == "auto"


def test_chat_with_tools_returns_final_content_when_no_tool_calls(monkeypatch) -> None:
    async def fake_post_json(client, url, payload, **kwargs):
        return {"choices": [{"finish_reason": "stop", "message": {"content": "已完成，无需工具。"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "glm-5.2")
    monkeypatch.setattr(hiagent, "get_setting", lambda key: "")
    monkeypatch.setattr(hiagent, "_zhipu_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr(hiagent.config, "ZHIPU_BASE_URL", "https://z/api")
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    turn = asyncio.run(hiagent.chat_with_tools([{"role": "user", "content": "hi"}], _TOOLS))
    assert turn.tool_calls == []
    assert turn.content == "已完成，无需工具。"


def test_chat_with_tools_falls_back_to_json_protocol_when_disabled(monkeypatch) -> None:
    async def fake_chat(messages, **kwargs):
        assert kwargs.get("call_meta", {}).get("tool_protocol") == "json_fallback"
        return '{"reply":"看列表","tool_calls":[{"tool":"resource.read","arguments":{"uri":"manju://projects"}}],"done":false}'

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "get_setting", lambda key: "off" if key == "agent_native_tools" else "")
    monkeypatch.setattr(hiagent, "chat", fake_chat)

    turn = asyncio.run(hiagent.chat_with_tools([{"role": "user", "content": "hi"}], _TOOLS))
    assert turn.content == "看列表"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "resource.read"
    assert turn.tool_calls[0].arguments == {"uri": "manju://projects"}


def test_json_fallback_treats_unparseable_output_as_plain_reply(monkeypatch) -> None:
    async def fake_chat(messages, **kwargs):
        return "就绪，无需调用任何工具。"

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "get_setting", lambda key: "off" if key == "agent_native_tools" else "")
    monkeypatch.setattr(hiagent, "chat", fake_chat)

    turn = asyncio.run(hiagent.chat_with_tools([{"role": "user", "content": "hi"}], _TOOLS))
    assert turn.tool_calls == []
    assert turn.content == "就绪，无需调用任何工具。"


def test_stream_chat_reconstructs_reasoning_content_and_fragmented_tool_calls(monkeypatch) -> None:
    frames = [
        {"choices": [{"delta": {"reasoning_content": "先查"}}]},
        {"choices": [{"delta": {"reasoning": "证据"}}]},
        {"choices": [{"delta": {"content": "我来处理。"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "function": {"name": "resource.read", "arguments": '{"uri":'}},
            {"index": 1, "id": "call_b", "function": {"name": "episode.check", "arguments": '{"id":'}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": '"e1"}'}},
            {"index": 0, "function": {"arguments": '"manju://projects"}'}},
        ]}, "finish_reason": "tool_calls"}], "usage": {"completion_tokens": 9}},
    ]
    body = "".join(f"data: {hiagent.json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames)
    body += "data: [DONE]\n\n"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(hiagent, "start_provider_call", lambda *args, **kwargs: "pc1")
    monkeypatch.setattr(hiagent, "finish_provider_call", lambda *args, **kwargs: None)
    tokens: list[tuple[str, str]] = []

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await hiagent._stream_chat_completion(
                client, "https://provider.test/chat/completions", {"model": "m", "messages": []},
                kind="chat_tools", model="m", headers={}, on_token=lambda kind, text: tokens.append((kind, text)),
            )

    data = asyncio.run(run())
    turn = hiagent._parse_assistant_turn(data, label="test")

    assert tokens == [("reasoning", "先查"), ("reasoning", "证据"), ("content", "我来处理。")]
    assert turn.reasoning == "先查证据"
    assert turn.content == "我来处理。"
    assert [(call.id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("call_a", "resource.read", {"uri": "manju://projects"}),
        ("call_b", "episode.check", {"id": "e1"}),
    ]
    assert data["usage"] == {"completion_tokens": 9}


def test_chat_with_tools_stream_switch_can_disable_streaming(monkeypatch) -> None:
    callbacks: list[tuple[str, str]] = []
    called = {"post": 0, "stream": 0}

    async def fake_post_json(client, url, payload, **kwargs):
        called["post"] += 1
        return {"choices": [{"finish_reason": "stop", "message": {"content": "非流式完成"}}]}

    async def fake_stream(*args, **kwargs):
        called["stream"] += 1
        raise AssertionError("关闭开关后不应进入流式路径")

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "glm-5.2")
    monkeypatch.setattr(hiagent, "get_setting", lambda key: "off" if key == "agent_stream_tokens" else "")
    monkeypatch.setattr(hiagent, "_zhipu_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr(hiagent.config, "ZHIPU_BASE_URL", "https://z/api")
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)
    monkeypatch.setattr(hiagent, "_stream_or_fallback", fake_stream)

    turn = asyncio.run(hiagent.chat_with_tools(
        [{"role": "user", "content": "hi"}], _TOOLS,
        on_token=lambda kind, text: callbacks.append((kind, text)),
    ))

    assert turn.content == "非流式完成"
    assert callbacks == []
    assert called == {"post": 1, "stream": 0}


def test_json_protocol_streams_only_decoded_reply_not_tool_json(monkeypatch) -> None:
    async def fake_stream_plain(messages, **kwargs):
        callback = kwargs["on_token"]
        for piece in ['```json\n{"rep', 'ly":"正在\\n', '查询","tool_calls":[',
                      '{"tool":"resource.read","arguments":{"uri":"manju://projects"}}]}\n```']:
            callback("content", piece)
        return ('```json\n{"reply":"正在\\n查询","tool_calls":'
                '[{"tool":"resource.read","arguments":{"uri":"manju://projects"}}]}\n```')

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(
        hiagent, "get_setting",
        lambda key: "off" if key == "agent_native_tools" else "",
    )
    monkeypatch.setattr(hiagent, "_stream_plain_chat", fake_stream_plain)
    callbacks: list[tuple[str, str]] = []

    turn = asyncio.run(hiagent.chat_with_tools(
        [{"role": "user", "content": "hi"}], _TOOLS,
        on_token=lambda kind, text: callbacks.append((kind, text)),
    ))

    assert "".join(text for kind, text in callbacks if kind == "content") == "正在\n查询"
    assert all("tool_calls" not in text and "resource.read" not in text for _, text in callbacks)
    assert turn.content == "正在\n查询"
    assert turn.tool_calls[0].name == "resource.read"
