"""候选选择题选「都不是/无法确定」时，supporting_entry_index 允许为 null（ERR-20260902-205c51）。

《西游记》第一回：人物卡合并判定问「玉帝」是否是「孙悟空」的别称，模型如实答「都不是/无法确定」
并把 supporting_entry_index 留 null——没有支撑段可引。契约却要求整数，两轮格式修复都拒绝改写
语义，整集失败。契约两侧必须对齐：无匹配时 null 合法；选了具体候选而不给编号，钉证仍失败关闭。
"""
from __future__ import annotations

from app.portraits.card_merge import _CardMergeVerdictResponse, _card_merge_pin_entry
from app.production.prep_pack.true_name import (
    _PrepPackTrueNameVerdictResponse,
    _prep_pack_true_name_pin_dossier_entry,
)

_DOSSIER = [{"entry_index": 1, "text": "玉帝厚恩，官赐天蓬元帅。"}, {"entry_index": 2, "text": "悟空大闹天宫。"}]


def test_card_merge_no_match_accepts_null_index() -> None:
    verdict = _CardMergeVerdictResponse.model_validate(
        {"selected_candidate": "都不是/无法确定", "supporting_entry_index": None, "supporting_quote": ""}
    )
    assert verdict.supporting_entry_index is None
    assert _card_merge_pin_entry(_DOSSIER, verdict.supporting_entry_index) is None  # 不会钉到任何一段


def test_card_merge_omitted_index_defaults_to_null() -> None:
    verdict = _CardMergeVerdictResponse.model_validate({"selected_candidate": "都不是/无法确定"})
    assert verdict.supporting_entry_index is None


def test_true_name_no_match_accepts_null_index() -> None:
    verdict = _PrepPackTrueNameVerdictResponse.model_validate(
        {"selected_candidate": "都不是/无法确定", "supporting_entry_index": None}
    )
    assert verdict.supporting_entry_index is None
    assert _prep_pack_true_name_pin_dossier_entry(_DOSSIER, verdict.supporting_entry_index) is None


def test_selected_candidate_with_integer_index_still_pins() -> None:
    verdict = _CardMergeVerdictResponse.model_validate(
        {"selected_candidate": "孙悟空", "supporting_entry_index": 2, "supporting_quote": ""}
    )
    assert _card_merge_pin_entry(_DOSSIER, verdict.supporting_entry_index) == _DOSSIER[1]
