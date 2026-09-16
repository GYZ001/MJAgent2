"""映射台 2.0.5：同一编号多地点 + 用别处完整写法回指（见
``chunk_extraction._ASSET_DECLARATION_RULES`` 上方的完整根因记录）。

真实案例：龙猫出爪 EP1 第 14 段场次标题只写「人间·老街」，正文写「周晚站在门口」，
那个门口的完整写法只在第 3 段标题里出现过；于是「晚安宠物医院门口」只拿到
segment_indexes=[3]，分镜第 17 段的候选集里根本没有它，分镜模型即使在散文里写出
「站在宠物医院门口」也无从声明。

这里锁三件事，每一件失守都会让新规则悄悄失效而别处不报红。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import chunk_extraction as ce
from app.production.prep_pack.chunking import _prep_pack_gate_segment_indexes
from app.source_excerpt import SourceSegment


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
            segment_id="s14", start_offset=0, end_offset=30,
            text="【段 12｜人间·老街｜清晨】（周晚站在门口，看向街对面。）",
        )
        with pytest.raises(_Captured):
            asyncio.run(ce._extract_chunk(
                chunk=[(14, segment)], known_characters=["周晚"],
                known_scenes=["晚安宠物医院门口"], attempt_hint="",
                run_id=None, episode_id="ep1", episode_no=1, chunk_index=0,
            ))
    finally:
        ce._call_structured = original
    return seen["prompt"]


class _Captured(Exception):
    """只用来把控制权从被测协程里拿回来，不代表失败。"""


def test_rules_block_is_actually_interpolated_into_the_prompt() -> None:
    """规则块抽成模块级常量后，f-string 必须真的把它插进去。

    这条是这次改动最容易静默失败的地方：``_ASSET_DECLARATION_RULES`` 一旦漏了
    插值语法，提示词里留下的是字面量花括号串，模型收到的规则区变成一行垃圾，
    而所有既有测试照样全绿——它们没有一条断言发给模型的提示词长什么样。
    """
    prompt = _capture_prompt()
    assert "{_ASSET_DECLARATION_RULES}" not in prompt, "规则块没被插值，模型收到的是字面量"
    assert "segment_indexes 判据" in prompt
    assert "场景的持续性" in prompt


def test_prompt_carries_both_new_scene_rules() -> None:
    """两段新规则必须真的到达模型。

    删掉其中任何一段，映射台会安静地退回「一个编号只报一个主场景」，下游分镜的
    候选集随之收窄，最终表现是用户看到的「场景图没关联对」——而全仓不会有任何
    一条测试变红。
    """
    prompt = _capture_prompt()
    assert "同一编号里的多个地点" in prompt
    assert "用别处的完整写法回指同一地点" in prompt
    # 例外必须写在命名纪律那条自己身上，否则两条硬性规则在模型眼里互相打架
    assert "唯一的例外" in prompt


def test_segment_index_gate_admits_a_name_absent_from_that_segment() -> None:
    """段号闸门只校验编号范围，不要求 display_name 在那些编号里逐字出现。

    新规则让模型按「别处的完整写法」申报本编号，申报的名字本来就不在本编号原文里。
    如果这道闸改成要求逐字命中，新规则产出的申报会被整条丢掉，而提示词照旧写着
    「要申报」——发出去的规则与校验两侧不对齐，宽的那侧就是必然发生的线上故障。
    """
    verified = _prep_pack_gate_segment_indexes(
        "人间·晚安宠物医院·门口", [3, 14],
        chunk_global_indexes={3, 14}, chunk_by_index={3: object(), 14: object()},
    )
    assert verified == [3, 14], "同一地点跨编号申报必须整条通过，不能只留名字逐字出现的那段"

    out_of_range = _prep_pack_gate_segment_indexes(
        "人间·晚安宠物医院·门口", [3, 99],
        chunk_global_indexes={3, 14}, chunk_by_index={3: object(), 14: object()},
    )
    assert out_of_range == [3], "编号落在本次 chunk 之外仍然要被挡掉，这道结构闸不能一起放宽"
