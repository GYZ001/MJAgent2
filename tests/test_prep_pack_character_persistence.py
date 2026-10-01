"""映射台 2.0.12：人物在场的持续性（见
``chunk_extraction._ASSET_DECLARATION_RULES`` 新增段落的完整根因记录）。

真实案例：《顾念长安》第 1 集第三轮分镜（经多代理核查确认，
``/tmp/mjtest/ep1_redo/segments_r3.json``）：顾屿在原文第 17-19 段（温念当着他
接房东电话）、第 31-33 段始终在场，原文没有写他离开，但这些段没有他的具体动作
描写，映射没有把这些编号计入他的 segment_indexes；分镜台按段号交集取人物资产
（``storyboard_pack._segment_relevant_assets``），这些段因此拿不到他的定妆照
锚点。新规则与既有「场景的持续性」对称——写法、位置、校验方式全部照抄同一先例。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import chunk_extraction as ce
from app.source_excerpt import SourceSegment


class _Captured(Exception):
    """只用来把控制权从被测协程里拿回来，不代表失败。"""


def _capture_prompt() -> str:
    """跑一次 _extract_chunk，截获真正发给模型的提示词全文。"""
    seen: dict[str, str] = {}

    async def fake_call(**kwargs):
        seen["prompt"] = kwargs.get("prompt") or ""
        raise _Captured

    original = ce._call_structured
    ce._call_structured = fake_call
    try:
        segment = SourceSegment(
            segment_id="s17", start_offset=0, end_offset=30,
            text="温念接起房东的电话，顾屿在一旁安静地看着她，没有说话。",
        )
        with pytest.raises(_Captured):
            asyncio.run(ce._extract_chunk(
                chunk=[(17, segment)], known_characters=["顾屿", "温念"],
                known_scenes=[], known_props=[], attempt_hint="",
                run_id=None, episode_id="ep1", episode_no=1, chunk_index=0,
            ))
    finally:
        ce._call_structured = original
    return seen["prompt"]


def test_character_persistence_rule_is_in_the_prompt() -> None:
    """新规则必须真的插值进发给模型的提示词，不是留在常量里从没被用到。"""
    prompt = _capture_prompt()
    assert "{_ASSET_DECLARATION_RULES}" not in prompt, "规则块没被插值，模型收到的是字面量"
    assert "人物在场的持续性（仅适用于 characters，硬性）" in prompt
    assert "只要原文没有写明他离开" in prompt
    assert "挂断电话后转身离开、被送走、被打发走等" in prompt
    assert "要一并计入他的 segment_indexes" in prompt
    assert "不能因为某个编号本身没有写到他就" in prompt
    assert "只有当原文明确写他已经离开，或情节转移到另一个他不在场的地方" in prompt


def test_character_persistence_rule_does_not_disturb_existing_rules() -> None:
    """新增段落只是在场景持续性之后追加一段，既有规则必须逐字不变——否则这次
    改动会悄悄连带改坏场景/道具命名纪律等不相关判据。"""
    prompt = _capture_prompt()
    assert "segment_indexes 判据（硬性，对 characters/scenes/props 都适用）" in prompt
    assert "场景的持续性（仅适用于 scenes，硬性）" in prompt
    assert "同一编号里的多个地点（仅适用于 scenes，硬性）" in prompt
    assert "用别处的完整写法回指同一地点（仅适用于 scenes，硬性）" in prompt
    assert "命名纪律（关于 characters/scenes 的 display_name，硬性）" in prompt
