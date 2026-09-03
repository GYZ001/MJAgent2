"""一个地点一个场景（2026-09-02《神墓》proj_facfc3964f69）。

场景圣经提示词曾以「夜晚密林」举例，模型据此把同一地点拆成「白日神魔陵园」
「夜晚神魔陵园」；映射台事件链只给裸地点「神魔陵园」，assess_new_scene 一次拒绝猜
时段（整集失败，ERR-20260902-033f75），一次「默认对应白日」（侥幸通过）。两处提示词
现在共用 app.scene_contract 的两条正面陈述，本文件钉住它们确实进了发出去的提示词。
"""
from __future__ import annotations

import asyncio
import json

from app.harness import model_gateway
from app.refs import SCENE_CANONICAL_MAX_CHARS
from app.scene_contract import SCENE_ONE_LOCATION_RULE, SCENE_SAME_LOCATION_MATCH_RULE
from app.schemas import Bible, Scene, World


def _bible() -> Bible:
    return Bible(characters=[], world=World(visual_style_canonical="国漫风格", genre="玄幻"), scenes=[])


def test_scene_bible_prompt_states_one_location_one_scene(monkeypatch) -> None:
    from app.stages import generate_scene_bible

    seen: dict[str, str] = {}

    async def capture(messages, **_kwargs):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps({"scenes": [{
            "name": "神魔陵园",
            "scene_canonical": "碑" * SCENE_CANONICAL_MAX_CHARS,
            "location_kind": "室外",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", capture)
    asyncio.run(generate_scene_bible(
        [{"idx": 1, "title": "第一章", "content": "神魔陵园白天仙气氤氲，夜晚魔气汹涌。"}], _bible(),
    ))
    prompt = seen["prompt"]
    assert SCENE_ONE_LOCATION_RULE in prompt
    assert "夜晚密林" not in prompt, "示例名字带时段，模型会照着把一个地点拆成两个场景"
    assert "时段与天气限定" in SCENE_ONE_LOCATION_RULE


def test_assess_new_scene_prompt_treats_time_qualified_names_as_same_location(monkeypatch) -> None:
    from app import scenes

    seen: dict[str, str] = {}

    async def capture(messages, **_kwargs):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps({
            "important": False, "existing_scene_name": "白日神魔陵园",
            "reason": "同一地点", "name": "", "scene_canonical": "", "location_kind": "",
            "location_key": "", "role": "transitional", "era_anchor": "", "anchor_phrase": "",
        }, ensure_ascii=False)

    known_scenes = [
        Scene(name="白日神魔陵园", scene_canonical="日间神魔陵园碑林成片，仙气氤氲，青灰石碑，国漫厚涂"),
        Scene(name="夜晚神魔陵园", scene_canonical="夜间神魔陵园碑林成片，魔气汹涌，青灰石碑，国漫厚涂"),
    ]
    monkeypatch.setattr(model_gateway, "chat", capture)
    verdict = asyncio.run(scenes.assess_new_scene(
        "神魔陵园", "陵园内碑林成片", style="国漫风格",
        known_scenes=known_scenes, ep_label="第 1 集",
    ))
    assert SCENE_SAME_LOCATION_MATCH_RULE in seen["prompt"]
    assert verdict["important"] is False
    assert verdict["existing_scene_name"] == "白日神魔陵园"
