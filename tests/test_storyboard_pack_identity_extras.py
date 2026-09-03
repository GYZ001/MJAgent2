"""WS12：``resolve_persisted_character_ids`` 群演描述性措辞归并接线测试。

判据原文与纯函数单测见 ``tests/test_storyboard_extras_reconcile.py`` /
``app.production.storyboard_extras_reconcile`` 模块 docstring；这里只测
``app.production.storyboard_pack_identity.resolve_persisted_character_ids``
把三道候选（asset_manifest.characters / appellation_map / WS12 结构性归并）
接起来之后的整体行为，包括与既有 WS2-B 正名替换、旁白剔除共存时的顺序。

fixture 用三国白话 ep1 真实数据（proj_ecabd38b7261 / ep_9357bedfc843，B 机
2026-09-03 只读实测）：映射包登记的 functional_extras 只有「督邮」
（segment_indexes=[9,10,11,12]，entity:f76c5058c82ed3a7）——shot1
（source_segment_indexes=[2,3]）三个描述性群演在这份映射包里从未被登记过，
0 候选、保持沉默，是映射台发现覆盖率的独立问题，不在本模块判据范围内。
"""
from __future__ import annotations

from app.production.storyboard_pack_identity import resolve_persisted_character_ids


def _fe_payload(*extras: dict) -> dict:
    return {
        "asset_manifest": {"characters": [], "functional_extras": list(extras)},
        "appellation_map": [],
    }


def test_merges_descriptive_extra_into_registered_label():
    payload = _fe_payload({
        "label": "督邮", "segment_indexes": [9, 10, 11, 12],
        "visual_entity_id": "entity:f76c5058c82ed3a7",
    })
    resolved, notes = resolve_persisted_character_ids(
        payload, ["身着藏青色官袍的督邮"], segment_source_indexes=[9],
    )
    assert resolved == ["entity:f76c5058c82ed3a7"]
    assert notes == []


def test_does_not_merge_when_segments_do_not_overlap():
    """判据两个条件缺一不可：label 逐字命中但段落范围不重叠，绝不归并
    （三国白话 ep1 shot1 的官员描述与后文「督邮」不是同一个人，只是恰好
    在完全不同的原文范围各自出现）。"""
    payload = _fe_payload({
        "label": "督邮", "segment_indexes": [9, 10, 11, 12],
        "visual_entity_id": "entity:f76c5058c82ed3a7",
    })
    resolved, notes = resolve_persisted_character_ids(
        payload, ["身着藏青色官袍的督邮"], segment_source_indexes=[2, 3],
    )
    assert resolved == ["身着藏青色官袍的督邮"]
    assert notes == []


def test_leaves_ambiguous_match_unmerged_with_note():
    # 两个候选各自都是这句描述性文字的逐字子串（"杂役"⊂"…杂役衫…"，
    # "大汉"⊂"…魁梧大汉"）——同段落范围内真的没法唯一确定归并到哪一个。
    payload = _fe_payload(
        {"label": "杂役", "segment_indexes": [5], "visual_entity_id": "entity:aaa"},
        {"label": "大汉", "segment_indexes": [5], "visual_entity_id": "entity:bbb"},
    )
    resolved, notes = resolve_persisted_character_ids(
        payload, ["穿杂役衫的魁梧大汉"], segment_source_indexes=[5],
    )
    assert resolved == ["穿杂役衫的魁梧大汉"]
    assert len(notes) == 1
    assert "STORYBOARD_PACK_EXTRA_RECONCILE_AMBIGUOUS" in notes[0]
    assert "杂役" in notes[0] and "大汉" in notes[0]


def test_no_candidate_stays_silent():
    """0 命中不记 note——已经由 [STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN]
    报告过一次，本模块重复报告没有信息增量。"""
    payload = _fe_payload({
        "label": "督邮", "segment_indexes": [9], "visual_entity_id": "entity:f76c5058c82ed3a7",
    })
    resolved, notes = resolve_persisted_character_ids(
        payload, ["中年留三绺长须的藏青色官袍官员"], segment_source_indexes=[2, 3],
    )
    assert resolved == ["中年留三绺长须的藏青色官袍官员"]
    assert notes == []


def test_empty_identity_id_never_matches_everything():
    """空 identity_id 不能因为「''是任何字符串的子串」被误判成匹配全部候选。"""
    payload = _fe_payload({
        "label": "督邮", "segment_indexes": [9], "visual_entity_id": "entity:f76c5058c82ed3a7",
    })
    resolved, notes = resolve_persisted_character_ids(
        payload, [""], segment_source_indexes=[9],
    )
    assert resolved == [""]
    assert notes == []


def test_named_character_appellation_and_ws12_extra_merge_coexist_in_one_call():
    """一镜里同时出现"已由 appellation_map 正名的叙述向称谓"和"待 WS12
    归并的群演描述"，两条路径互不干扰、顺序保留。"""
    payload = {
        "asset_manifest": {
            "characters": [{"identity_id": "bible:里奥"}],
            "functional_extras": [{
                "label": "督邮", "segment_indexes": [9],
                "visual_entity_id": "entity:f76c5058c82ed3a7",
            }],
        },
        "appellation_map": [{"raw_mention": "少年", "identity_id": "bible:里奥"}],
    }
    resolved, notes = resolve_persisted_character_ids(
        payload, ["少年", "身着藏青色官袍的督邮", "旁白"], segment_source_indexes=[9],
    )
    assert resolved == ["bible:里奥", "entity:f76c5058c82ed3a7"]
    assert notes == []
