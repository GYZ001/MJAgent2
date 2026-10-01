"""映射台 2.0.13 缺陷①：已登记道具名单压过了申报判据（见
``chunk_extraction.py`` ``_PROP_SEGMENT_CRITERIA`` 上方 PREP_PACK_VERSION
2.0.13 changelog）。

真实案例（顾念长安第2集，用准备包 2.0.12 重跑映射台）：第2段原文「顾屿换下了
昨晚那身深灰大衣，套了一件浅灰色卫衣……温念仍是那件米白针织开衫和浅蓝碎花
长裙」——这四件正穿着/画外换下的衣服，全部被申报成了道具，因为项目物件库里
第1集遗留了同名的衣服卡（大衣/浅灰色卫衣/米白色针织开衫/浅蓝色碎花长裙）。
``_PROP_SEGMENT_CRITERIA`` 早就明确写了"人物身上正穿着的衣物……不作为道具
申报"，但已登记名单里存在同名卡时模型照样申报——名单的"仅供拼写对齐"措辞
从未说清"存在同名卡不是申报的理由"，模型把"名单里有它"读成了"它该报"。

本文件钉住修复：判据常量新增一段完整正面陈述（不是只堵"衣服"这一种禁令，
对一切素材类型同样成立），抽取提示词与复核提示词共用同一个单源常量
（``_PROP_SEGMENT_CRITERIA``，二者本就逐字共享这个常量，见
``prop_recheck._prompt``），不在两处各写一份测辞。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import chunk_extraction as ce
from app.production.prep_pack import prop_recheck
from app.source_excerpt import SourceSegment


class _Captured(Exception):
    """只用来把控制权从被测协程里拿回来，不代表失败，同
    ``test_prep_pack_prop_declaration_rule._Captured`` 同一手法。"""


def _capture_extraction_prompt(known_props: list[str]) -> str:
    """跑一次 _extract_chunk，截获真正发给模型的提示词全文。"""
    seen: dict[str, str] = {}

    async def fake_call(**kwargs):
        seen["prompt"] = kwargs.get("prompt") or ""
        raise _Captured

    original = ce._call_structured
    ce._call_structured = fake_call
    try:
        segment = SourceSegment(
            segment_id="s1", start_offset=0, end_offset=60,
            text="顾屿换下了昨晚那身深灰大衣，套了一件浅灰色卫衣，配一条黑色休闲裤。",
        )
        with pytest.raises(_Captured):
            asyncio.run(ce._extract_chunk(
                chunk=[(1, segment)], known_characters=["顾屿"],
                known_scenes=[], known_props=known_props, attempt_hint="",
                run_id=None, episode_id="ep2", episode_no=2, chunk_index=0,
            ))
    finally:
        ce._call_structured = original
    return seen["prompt"]


# ---------------------------------------------------------------------------
# 判据常量本身：完整正面陈述，不是只堵"衣服"这一种写法的禁令
# ---------------------------------------------------------------------------


def test_criteria_states_known_list_purpose_is_alignment_and_nomination_only() -> None:
    """已登记道具名单的用途只有两个——对齐写法、以及在 known_prop_name 里
    提名同一件实物，判据常量必须把这句话写清楚，而不是让名单的存在本身
    暗示"该报"。"""
    assert "已登记道具名单只做两件事" in ce._PROP_SEGMENT_CRITERIA
    assert "对齐这件东西的写法" in ce._PROP_SEGMENT_CRITERIA
    assert "在 known_prop_name 里提名" in ce._PROP_SEGMENT_CRITERIA


def test_criteria_states_list_presence_is_not_a_declare_reason() -> None:
    """名单里存在同名/相近名的卡，既不是申报的理由，也不是不申报的理由——
    申报与否只看①②两条判据。"""
    assert "名单里存在同名或名字相近的卡，既不是申报的理由，也不是不申报的" in ce._PROP_SEGMENT_CRITERIA


def test_criteria_states_worn_clothing_is_not_exempted_by_list_presence() -> None:
    """正穿着的衣物即使名单里有同名道具卡，也不会因此被当成例外——这是
    真实事故的直接根因，必须在判据里明确排除这条误读。"""
    assert (
        "人物身上正穿着的衣物，哪怕名单里有同名的道具卡，也不会因此被当成例外："
        "判断仍然只看它此刻是穿在身上，还是已经脱下、拿在手里、递交或放在某处"
    ) in ce._PROP_SEGMENT_CRITERIA


def test_criteria_statement_is_not_scoped_to_clothing_only() -> None:
    """新增陈述要对一切素材类型同样成立，不是只堵"衣服"这一种写法——
    CLAUDE.md「写完整的正面陈述，不写禁令」：只堵一种具体写法，模型换个
    变体照样越界。"""
    assert "这条对一切素材类型同样成立，不止衣物" in ce._PROP_SEGMENT_CRITERIA


def test_old_two_criteria_and_clothing_exemption_still_present() -> None:
    """2.0.11 既有的"衣物正穿着不作为道具申报"规则必须逐字保留，新增陈述是
    补充而不是替换。"""
    assert "人物身上正穿着的衣物属于人物造型" in ce._PROP_SEGMENT_CRITERIA
    assert "衣物被脱下、拿在手里、递交或放在某处、作为一件物件出现时，才按上面两条判断" in ce._PROP_SEGMENT_CRITERIA


# ---------------------------------------------------------------------------
# 单源常量：抽取与复核两处提示词都要带上这段新增陈述
# ---------------------------------------------------------------------------


def test_extraction_prompt_carries_the_new_statement_when_known_list_is_non_empty() -> None:
    """真实事故场景复现：已登记名单里有"大衣""浅灰色卫衣"等同名卡时，抽取
    提示词仍然要完整带上"名单存在不构成申报理由"这句话。"""
    prompt = _capture_extraction_prompt(["大衣", "浅灰色卫衣", "米白色针织开衫", "浅蓝色碎花长裙"])
    assert "已登记道具名单只做两件事" in prompt
    assert "名单里存在同名或名字相近的卡，既不是申报的理由，也不是不申报的" in prompt


def test_recheck_prompt_carries_the_same_statement_verbatim() -> None:
    """复核提示词通过 ``_PROP_SEGMENT_CRITERIA`` 单源常量带上同一段文字，
    不在 prop_recheck.py 里另外复制一份措辞（结构性保证：两处字符串级相等，
    同 test_prep_pack_prop_recheck.py::test_prompt_uses_the_same_criteria_
    sentence_as_extraction）。"""
    prompt = prop_recheck._prompt("（原文）", ["大衣", "浅灰色卫衣"])
    assert "已登记道具名单只做两件事" in prompt
    assert ce._PROP_SEGMENT_CRITERIA in prompt
