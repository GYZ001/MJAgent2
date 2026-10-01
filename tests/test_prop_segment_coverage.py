"""道具段号全文补全（``app.production.prep_pack.prop_segment_coverage.
fill_prop_segment_coverage``，2026-10-01，三路只读调查第②项）。

真实背景：第 1 集重做分镜 143 条 resources.props 只有 38 条有图——手机在原文
第 7/16/20/24/54 段反复被温念摸出/放回/编辑/锁屏/响起，但抽取按 chunk 分批
进行，模型这次调用只报出了其中一段。本模块在 manifest 建好之后做一轮确定性
全文检索，把同一件道具在本集其它段落里按已核验写法命中的段号找出来。

2.0.13（见 tests/test_prop_segment_coverage_confirm.py 完整案情）：命中段落
只是候选，是否真的并入 segment_indexes 还要再经一次批量模型确认——这份文件
只验证确定性检索本身（哪些段落命中、哪些词有检索资格），所以用
``_run_confirming_all_candidates`` 把确认步骤短路成"全部候选都判定为同一件
实物"，等价于 2.0.13 之前的直接合并行为；确认步骤自己的判断逻辑（选择性
确认/未确认不并入/调用失败的可见信号）单独在 test_prop_segment_coverage_
confirm.py 验证，不在这里重复。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import prop_segment_coverage
from app.production.prep_pack.prop_segment_coverage import fill_prop_segment_coverage
from app.schemas import Prop
from app.source_excerpt import SourceSegment


def _run_confirming_all_candidates(props_payload, segments, *, cards=()):
    """跑一次 fill_prop_segment_coverage，短路掉 2.0.13 新增的模型确认调用，
    让本次出现的全部候选都判定"是同一件实物"——见模块 docstring。"""
    async def _confirm_all(ids, requests, _segments, *, run_id, episode_id):
        del run_id, episode_id, _segments
        return {
            req_id: set(request["candidate_indexes"])
            for req_id, request in zip(ids, requests)
        }

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(prop_segment_coverage, "_confirm_prop_coverage_candidates", _confirm_all)
        return asyncio.run(fill_prop_segment_coverage(props_payload, segments, cards=cards))


def _segments(overrides: dict[int, str], *, last_index: int) -> list[SourceSegment]:
    """按 1-based 编号构造 ``last_index`` 个原文段，``overrides`` 指定的编号用
    给定文字，其余用不含任何道具字样的填充句——真实原文段落大多数跟当前这件
    道具无关，这个形状比"每段都写满关键词"更接近实际分布。"""
    out: list[SourceSegment] = []
    for index in range(1, last_index + 1):
        text = overrides.get(index, f"第{index}段：天色渐暗，走廊很安静。")
        out.append(SourceSegment(segment_id=f"s{index}", text=text, start_offset=0, end_offset=len(text)))
    return out


def test_fills_in_segments_where_the_declared_literal_wording_reappears() -> None:
    """手机只被模型申报了第 7 段，但它在第 16/20/24/54 段原文里还会反复被
    操作——全文检索后 segment_indexes 要把这四段都补进来，anchor 不变。"""
    segments = _segments({
        7: "温念从包里摸出手机，看了一眼又放回去。",
        16: "手机在桌上震动了一下，她没有去碰。",
        20: "他低头编辑着手机里那条没发出去的消息。",
        24: "手机锁屏前最后一点电量显示在角落。",
        54: "手机突然响起来，划破了安静的走廊。",
    }, last_index=54)
    props_payload = [{
        "label": "手机", "canonical_name": None, "description": "一部手机",
        "segment_indexes": [7], "source_wording": "手机",
        "provenance": {"method": "direct", "anchor_segments": [7], "anchor_phrase": "手机"},
        "plot_significant": False, "plot_significant_quote": "", "known_prop_name": "",
    }]

    result = _run_confirming_all_candidates(props_payload, segments, cards=())

    assert result is props_payload, "原地更新并整体返回同一个列表"
    assert result[0]["segment_indexes"] == [7, 16, 20, 24, 54]
    # anchor/其它字段完全不动——这一步只扩展"出现在哪些段"，不改变"申报凭什么立住"
    assert result[0]["provenance"] == {"method": "direct", "anchor_segments": [7], "anchor_phrase": "手机"}
    assert result[0]["label"] == "手机"
    assert result[0]["description"] == "一部手机"


def test_card_bound_entry_retrieves_by_both_label_and_card_name() -> None:
    """真实事故同形：素材库卡叫「行李箱」，这次模型把同一件东西报成「旧行李箱」
    （card_match 绑定后 canonical_name="行李箱"，label 保留这次的原文写法）。
    声明段（第3段）里「旧行李箱」「行李箱」两种写法都逐字命中过，所以两个都
    有资格当检索词；第5段原文只用了卡名「行李箱」的写法，也要被找到。"""
    segments = _segments({
        3: "顾屿拖着那只旧行李箱走进门。",
        5: "她想起小时候跟着爸爸拖着那只行李箱去火车站。",
        7: "阁楼里还摆着一只旧皮箱，没人碰过。",
    }, last_index=7)
    card = Prop(name="行李箱", appearance_canonical="棕色帆布，边角磨损", aliases=["旧皮箱"])
    props_payload = [{
        "label": "旧行李箱", "canonical_name": "行李箱", "description": "一只旧行李箱",
        "segment_indexes": [3], "source_wording": "",
        "provenance": {"method": "card_match", "anchor_segments": [3], "anchor_phrase": "行李箱"},
        "plot_significant": False, "plot_significant_quote": "", "known_prop_name": "行李箱",
    }]

    result = _run_confirming_all_candidates(props_payload, segments, cards=[card])

    # 第5段靠卡名「行李箱」命中被补进来；第7段的别名「旧皮箱」从未在已核验
    # 段落里命中过，不具备检索资格，不会被当成同一件东西盲目并进来。
    assert result[0]["segment_indexes"] == [3, 5]


def test_unverified_candidate_words_are_never_used_for_retrieval() -> None:
    """source_wording 与 label 都没有在模型已核验的段落里逐字出现过（结构上不
    该发生，但防御性地验证边界）：不发起任何全文检索，segment_indexes 原样
    保留，不凭空扩大范围。"""
    segments = _segments({
        2: "桌上放着一盏煤油灯，火苗很小。",
        9: "伞被遗忘在玄关，没人注意到。",
    }, last_index=9)
    props_payload = [{
        "label": "伞", "canonical_name": None, "description": "一把伞",
        "segment_indexes": [2], "source_wording": "",
        "provenance": {"method": "direct", "anchor_segments": [2], "anchor_phrase": "伞"},
        "plot_significant": False, "plot_significant_quote": "", "known_prop_name": "",
    }]

    result = _run_confirming_all_candidates(props_payload, segments, cards=())

    assert result[0]["segment_indexes"] == [2], "label 没有在第2段原文里逐字出现过，不该补第9段"


def test_short_verified_word_is_not_penalised_for_being_short() -> None:
    """不设最短长度魔数：单字别名「伞」如果确实在已核验段落命中过，就该照常
    用它去检索，不能因为它短就被排除——长度不是风险来源，是否已核验才是。"""
    segments = _segments({
        2: "桌上放着一把伞，还没来得及收。",
        9: "伞被遗忘在玄关，没人注意到。",
    }, last_index=9)
    props_payload = [{
        "label": "伞", "canonical_name": None, "description": "一把伞",
        "segment_indexes": [2], "source_wording": "",
        "provenance": {"method": "direct", "anchor_segments": [2], "anchor_phrase": "伞"},
        "plot_significant": False, "plot_significant_quote": "", "known_prop_name": "",
    }]

    result = _run_confirming_all_candidates(props_payload, segments, cards=())

    assert result[0]["segment_indexes"] == [2, 9]


def test_out_of_range_declared_index_is_dropped_not_crashed() -> None:
    """``segment_indexes`` 里混进越界编号（上游数据异常）时只忽略它，不抛错、
    不把越界编号当成"已核验段"去拼声明文本。"""
    segments = _segments({1: "温念拿起手机看了一眼。"}, last_index=1)
    props_payload = [{
        "label": "手机", "canonical_name": None, "description": "一部手机",
        "segment_indexes": [1, 99], "source_wording": "",
        "provenance": {"method": "direct", "anchor_segments": [1], "anchor_phrase": "手机"},
        "plot_significant": False, "plot_significant_quote": "", "known_prop_name": "",
    }]

    result = _run_confirming_all_candidates(props_payload, segments, cards=())

    assert result[0]["segment_indexes"] == [1]
