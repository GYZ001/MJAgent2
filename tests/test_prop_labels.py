"""道具标签归一：并列拆件、数量前缀与领属前缀剥掉，原标签作别名（2026-09-06 第 11 轮物件库核查）。"""
from __future__ import annotations

from app.props.labels import normalize_prop_label


def test_quantifier_and_possessive_prefixes_are_stripped() -> None:
    assert normalize_prop_label("两只野鸡") == ["野鸡"]
    assert normalize_prop_label("半块灵石") == ["灵石"]
    assert normalize_prop_label("九根柱子") == ["柱子"]
    assert normalize_prop_label("养丹坊分店的大旗") == ["大旗"]
    assert normalize_prop_label("陆烘的紫阳剑") == ["紫阳剑"]


def test_compound_labels_split_into_members() -> None:
    assert normalize_prop_label("凝灵丹与半块灵石") == ["凝灵丹", "灵石"]
    assert normalize_prop_label("野鸡和狍子") == ["野鸡", "狍子"]


def test_plain_labels_and_short_residues_are_kept_as_is() -> None:
    assert normalize_prop_label("储物袋") == ["储物袋"]
    assert normalize_prop_label("一石") == ["一石"]  # 剥完只剩一个字，保留原样
    assert normalize_prop_label("") == []
