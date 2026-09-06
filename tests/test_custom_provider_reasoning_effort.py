"""HiAgent 自定义路径也要带 reasoning_effort（2026-09-06 第 13 轮实测：阶段表上线后 148 次请求 0 次带档位——此前只有智谱内置路径设这个字段）。"""
from __future__ import annotations

import asyncio

from app import config, hiagent


def _capture_custom_chat(monkeypatch) -> list[dict]:
    payloads: list[dict] = []

    async def fake_plain_chat_request(client, url, payload, *args, **kwargs):
        payloads.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}],
                "usage": {"completion_tokens": 20, "prompt_tokens": 10}}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "custom:model_07030243d87e")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "d7ev7il5boeaebtf4sgg")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *a, **k: ("https://hia.test/api/aigw/v1", {"Authorization": "Bearer t"}))
    monkeypatch.setattr(hiagent, "_plain_chat_request", fake_plain_chat_request)
    monkeypatch.setattr(hiagent, "_cached_successful_provider_response", lambda *a, **k: None)
    monkeypatch.setattr(hiagent, "get_setting", lambda key: "")
    monkeypatch.setattr(config, "TEXT_REASONING_EFFORT", "")
    return payloads


def test_stage_table_low_effort_reaches_the_custom_provider_payload(monkeypatch) -> None:
    payloads = _capture_custom_chat(monkeypatch)
    asyncio.run(hiagent.chat([{"role": "user", "content": "判定"}], max_tokens=600, call_meta={"stage": "assess_new_scene"}))
    assert payloads and payloads[0].get("reasoning_effort") == "low"


def test_writing_stage_leaves_the_model_default(monkeypatch) -> None:
    payloads = _capture_custom_chat(monkeypatch)
    asyncio.run(hiagent.chat([{"role": "user", "content": "写一段"}], max_tokens=600, call_meta={"stage_key": "storyboard_pack_segment"}))
    assert payloads and "reasoning_effort" not in payloads[0]


def test_tool_chat_path_carries_the_stage_effort_too(monkeypatch) -> None:
    """身份调查的工具对话（chat_with_tools）是映射台最大头：49 次/9 分钟、p50 29s、思考 3.6k，此前不带档位。"""
    payloads: list[dict] = []

    async def fake_post_json(client, url, payload, *args, **kwargs):
        payloads.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "调查完毕", "tool_calls": []}}],
                "usage": {"completion_tokens": 20, "prompt_tokens": 10}}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "custom:model_07030243d87e")
    monkeypatch.setattr(hiagent, "_provider_supports_tools", lambda provider: True)
    monkeypatch.setattr(hiagent, "text_request_token_limits", lambda **k: ("custom:model_07030243d87e", "d7ev7il5boeaebtf4sgg", 4096))
    monkeypatch.setattr(hiagent, "_resolve_text_connection", lambda provider, model: ("https://hia.test/api/aigw/v1/chat/completions", "d7ev7il5boeaebtf4sgg", {}, "k"))
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)
    monkeypatch.setattr(hiagent, "get_setting", lambda key: "")
    monkeypatch.setattr(config, "TEXT_REASONING_EFFORT", "")
    from app import text_providers
    monkeypatch.setattr(text_providers, "protocol_for_provider", lambda provider: "openai")
    turn = asyncio.run(hiagent.chat_with_tools(
        [{"role": "user", "content": "调查"}], [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        call_meta={"stage": "discover_character_candidates"},
    ))
    assert turn.content == "调查完毕"
    assert payloads and payloads[0].get("reasoning_effort") == "low"
