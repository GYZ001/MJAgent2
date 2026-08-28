from __future__ import annotations

import asyncio
import json

from app import scenes


def test_scene_state_change_parser_keeps_all_items(monkeypatch) -> None:
    """升级到 chat_structured 后，根形状由 _SceneStateChangeResponse 在生成层约束成
    {"items":[...]} 对象；模型输出带 Markdown 围栏/前言时，网关的 authority-root
    提取照旧能取到那个对象，两个 item 都保留。"""
    items = [
        {
            "name": "hall", "changed": True, "persistence": "persistent",
            "change_dimensions": ["damage"],
            "new_scene_canonical": "A" * 35,
            "reason": "burned", "evidence_excerpt": "the hall burned",
        },
        {
            "name": "yard", "changed": True, "persistence": "episode",
            "change_dimensions": ["rebuild"],
            "new_scene_canonical": "B" * 36,
            "reason": "rebuilt", "evidence_excerpt": "the yard was rebuilt",
        },
    ]

    async def fake_chat(*_args, **_kwargs):
        return "model output:\n```json\n" + json.dumps({"items": items}) + "\n```"

    monkeypatch.setattr(scenes.model_gateway, "chat", fake_chat)
    result = asyncio.run(scenes.screen_scene_state_changes(
        [{"name": "hall", "current_canonical": "old", "fragments": ["x"]}],
        "episode 2",
    ))
    assert set(result) == {"hall", "yard"}
