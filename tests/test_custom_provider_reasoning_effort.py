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
