"""映射台 2.0.9：props 申报判据从举例式框定改成完整正面陈述（见
``chunk_extraction.py`` props 字段上方 PREP_PACK_VERSION 2.0.9 changelog）。

三路只读调查第①项：旧提示词"本段原文中画面里明确出现、有辨识度的物品/道具
（不是随口一提，例如武器、信物、法宝、书信、照片、纪念品等）"用举例框定让
模型从不申报被人物反复操作的日常物件（手机、脸盆、电闸、勺子、大衣）——第
1 集重做分镜 143 条 resources.props 只有 7 件建了卡、38 条有图。新判据不举
任何具体物件名，只看「是否被动作操作」「是否贯穿出现」两条结构性正面条件。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import chunk_extraction as ce
from app.source_excerpt import SourceSegment


class _Captured(Exception):
    """只用来把控制权从被测协程里拿回来，不代表失败，同
    ``test_prep_pack_scene_multi_location._Captured`` 同一手法。"""


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
            segment_id="s1", start_offset=0, end_offset=20,
            text="温念从包里摸出手机，看了一眼又放回去。",
        )
        with pytest.raises(_Captured):
            asyncio.run(ce._extract_chunk(
                chunk=[(1, segment)], known_characters=["温念"],
                known_scenes=[], known_props=[], attempt_hint="",
                run_id=None, episode_id="ep1", episode_no=1, chunk_index=0,
            ))
    finally:
        ce._call_structured = original
    return seen["prompt"]


def test_props_rule_no_longer_frames_by_example() -> None:
    """旧的举例式框定（"不是随口一提，例如武器、信物、法宝、书信、照片、纪念品等"）
    必须彻底消失——留着任何一个例子都会让模型继续按"这件东西像不像举例里那种
    值钱/特殊的道具"去筛，而不是按动作/贯穿两条结构性判据。"""
    prompt = _capture_prompt()
    assert "不是随口一提" not in prompt
    assert "法宝" not in prompt
    assert "信物" not in prompt
    assert "纪念品" not in prompt


def test_props_rule_states_two_positive_conditions() -> None:
    """新判据必须同时交代两条正面条件，并且两条都不设物件名单——判据从数据推导，
    不是从举例反推。"""
    prompt = _capture_prompt()
    assert "被本段某个角色的身体动作明确操作" in prompt
    assert "拿起、放下、递给、接过、使用、穿戴、开关、移动、交接" in prompt
    assert "在本集其他段落的原文里还会再次出现" in prompt
    assert "贯穿性的视觉线索" in prompt


def test_props_rule_leaves_judging_to_downstream_verdict() -> None:
    """申报这一步不替建卡判定取舍——建卡仍然由 app.props.judge 四选一决定，
    这条提示词只负责"报不报"，不负责"建不建卡"，两件事不能被提示词的新措辞
    混为一谈。"""
    prompt = _capture_prompt()
    assert "是否正式建卡由后续判定核验" in prompt
    assert "申报这一步不替它取舍" in prompt


def test_props_rule_only_background_and_single_occurrence_is_excluded() -> None:
    """唯一的不报情形要同时满足三件事：背景陈设一笔带过、无人物互动、本集
    全文只出现一次——少一个条件都不该被这句话劝退。"""
    prompt = _capture_prompt()
    assert "只有在背景陈设里一笔带过、没有任何角色与它互动、且" in prompt
    assert "本集全文只出现这一次的物件才不报" in prompt


def test_other_json_field_descriptions_are_untouched() -> None:
    """这次只替换 props 字段开头那句举例式框定，plot_significant/source_wording/
    known_prop_name 等既有 JSON 字段描述必须逐字不变——否则就不是"只替换这句"
    而是顺手改了判据契约的其它部分，范围被悄悄扩大。"""
    prompt = _capture_prompt()
    assert '"plot_significant": true/false, "plot_significant_quote": "从上面 segment_indexes' in prompt
    assert "不超过约40字），要能证明这件物品在这段剧情里被某个" in prompt
    assert "角色拿起/递给/接过/放下、被贴身佩戴或收藏、被镜头意味着特写描写" in prompt
    assert '"known_prop_name": "这件道具如果就是已登记道具名单中的某一件' in prompt
