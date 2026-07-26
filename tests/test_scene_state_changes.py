from __future__ import annotations

import asyncio
import json

import pytest

from app import scenes


@pytest.mark.parametrize("wrapped", [False, True])
def test_scene_state_change_parser_keeps_all_items(monkeypatch, wrapped: bool) -> None:
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
    payload = {"items": items} if wrapped else items

    async def fake_chat(*_args, **_kwargs):
        return "model output:\n```json\n" + json.dumps(payload) + "\n```"

    monkeypatch.setattr(scenes.model_gateway, "chat", fake_chat)
    result = asyncio.run(scenes.screen_scene_state_changes(
        [{"name": "hall", "current_canonical": "old", "fragments": ["x"]}],
        "episode 2",
    ))
    assert set(result) == {"hall", "yard"}
