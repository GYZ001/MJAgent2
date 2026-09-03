"""WS12：``app.production.storyboard_extras_reconcile`` 纯函数单测。

判据原文（与模块 docstring 一致）：段落重叠（本镜 source_segment_indexes 与
候选群演 segment_indexes 交集非空）+ 文本包含（label 与描述性文字互为子串）
两个条件同时满足才归并；多候选记歧义 note、不归并；0 候选保持沉默。

fixture 用三国白话 ep1 真实数据（proj_ecabd38b7261 / ep_9357bedfc843，B 机
2026-09-03 只读实测）：映射包登记的 functional_extras 只有「督邮」
（segment_indexes=[9,10,11,12]，entity:f76c5058c82ed3a7）/「县吏」
（[10]，entity:b96a5a12a8841b62）/「老人」（[11]，entity:e194a4373accd857）；
shot1（source_segment_indexes=[2,3]）的三个描述性群演在这份映射包里从未
被登记过——那是映射台发现覆盖率的独立问题，不在本模块判据范围内（0 候选，
保持沉默，见模块 docstring 最后一段）。
"""
from __future__ import annotations

from app.production.storyboard_extras_reconcile import (
    reconcile_descriptive_extra,
    reconcile_persisted_extra_ids,
)

_DUYOU = {
    "label": "督邮", "segment_indexes": [9, 10, 11, 12],
    "visual_entity_id": "entity:f76c5058c82ed3a7",
}
_XIANLI = {
    "label": "县吏", "segment_indexes": [10],
    "visual_entity_id": "entity:b96a5a12a8841b62",
}
_LAOREN = {
    "label": "老人", "segment_indexes": [11],
    "visual_entity_id": "entity:e194a4373accd857",
}


def test_merges_when_segment_overlaps_and_label_is_substring():
    result = reconcile_descriptive_extra("身着藏青色官袍的督邮", [9], [_DUYOU, _XIANLI, _LAOREN])
    assert result.merged is True
    assert result.resolved_id == "entity:f76c5058c82ed3a7"
    assert result.note is None


def test_merges_when_raw_text_is_substring_of_label():
    """反方向：描述性文字比 label 更短、逐字是 label 的子串（label 本身就是
    一句浓缩描述的情形，见模块 docstring 判据②——神墓真实登记标签
    「半百老道士」，模型这次只写了「老道士」三个字）。"""
    banbai_laodaoshi = {
        "label": "半百老道士", "segment_indexes": [3], "visual_entity_id": "entity:shenmu001",
    }
    result = reconcile_descriptive_extra("老道士", [3], [banbai_laodaoshi])
    assert result.merged is True
    assert result.resolved_id == "entity:shenmu001"


def test_no_merge_without_segment_overlap_even_if_label_matches_literally():
    result = reconcile_descriptive_extra("督邮打人", [2, 3], [_DUYOU])
    assert result.merged is False
    assert result.resolved_id == "督邮打人"
    assert result.note is None


def test_no_merge_without_label_substring_even_with_segment_overlap():
    result = reconcile_descriptive_extra("不相关的路人甲", [9], [_DUYOU])
    assert result.merged is False
    assert result.resolved_id == "不相关的路人甲"
    assert result.note is None


def test_real_prod_data_three_kingdoms_ep1_shot1_extras_stay_unmerged_silently():
    """三国白话 ep1 shot1 的三条真实描述性文字，映射包里没有任何候选覆盖
    它们的段落范围——0 命中，原样保留、不记 note。"""
    registered = [_DUYOU, _XIANLI, _LAOREN]
    for raw_text in [
        "中年留三绺长须的藏青色官袍官员",
        "身着粗布麻衣的年轻男子",
        "身着粗布麻衣的中年路人",
    ]:
        result = reconcile_descriptive_extra(raw_text, [2, 3], registered)
        assert result.merged is False
        assert result.resolved_id == raw_text
        assert result.note is None


def test_ambiguous_candidates_are_not_merged_and_produce_a_visible_note():
    # 两个候选各自都是这句描述性文字的逐字子串（"杂役"⊂"…杂役衫…"，
    # "大汉"⊂"…魁梧大汉"）——同段落范围内真的没法唯一确定归并到哪一个。
    zaji = {"label": "杂役", "segment_indexes": [5], "visual_entity_id": "entity:aaa"}
    dahan = {"label": "大汉", "segment_indexes": [5], "visual_entity_id": "entity:bbb"}
    result = reconcile_descriptive_extra("穿杂役衫的魁梧大汉", [5], [zaji, dahan])
    assert result.merged is False
    assert result.resolved_id == "穿杂役衫的魁梧大汉"
    assert result.note is not None
    assert "STORYBOARD_PACK_EXTRA_RECONCILE_AMBIGUOUS" in result.note
    assert "杂役" in result.note and "大汉" in result.note


def test_single_char_label_never_participates_in_merge():
    """单字 label（"人"之类）子串判据几乎必然假阳性——最小长度闸门直接排除，
    既不命中也不制造歧义。"""
    short = {"label": "人", "segment_indexes": [1], "visual_entity_id": "entity:short"}
    result = reconcile_descriptive_extra("一个陌生人走过", [1], [short])
    assert result.merged is False
    assert result.resolved_id == "一个陌生人走过"
    assert result.note is None


def test_empty_raw_text_does_not_match_every_candidate():
    result = reconcile_descriptive_extra("", [9], [_DUYOU, _XIANLI, _LAOREN])
    assert result.merged is False
    assert result.resolved_id == ""
    assert result.note is None


def test_missing_visual_entity_id_falls_back_unmerged():
    broken = {"label": "督邮", "segment_indexes": [9], "visual_entity_id": ""}
    result = reconcile_descriptive_extra("督邮", [9], [broken])
    assert result.merged is False
    assert result.resolved_id == "督邮"


def test_batch_helper_preserves_order_and_collects_notes_only_for_ambiguous():
    zaji = {"label": "杂役", "segment_indexes": [5], "visual_entity_id": "entity:aaa"}
    dahan = {"label": "大汉", "segment_indexes": [5], "visual_entity_id": "entity:bbb"}
    resolved, notes = reconcile_persisted_extra_ids(
        ["身着藏青色官袍的督邮", "穿杂役衫的魁梧大汉", "无关描述"],
        [5, 9], [_DUYOU, zaji, dahan],
    )
    assert resolved == ["entity:f76c5058c82ed3a7", "穿杂役衫的魁梧大汉", "无关描述"]
    assert len(notes) == 1
    assert "STORYBOARD_PACK_EXTRA_RECONCILE_AMBIGUOUS" in notes[0]
