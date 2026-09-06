"""外观子句落地：通用形态保留，标志性特征必须有原文依据，否则删子句不删卡（2026-09-06 许师姐虎皮）。"""
from __future__ import annotations

from app.portraits.appearance_grounding import ground_appearance

_CH1_FRAGMENTS = (
    "“许师姐好手段，出门一次竟带回了四个拥有资质的小娃。”两个男子中的一人，带着恭维向着那女子说道。"
    "“将他们带去杂役处。”那女子神情冷漠，看都不看孟浩四人一眼，迈步间整个人化作了一道长虹。"
    "“许师姐已经到了凝气第七层，被掌教赐了风幡，没到筑基便可飞行，让人羡慕。”"
)
_CH5_FRAGMENTS = _CH1_FRAGMENTS + "其旁穿着银袍的许姓女子自然也是一愣，她虽说修为已是凝气七层，是内门弟子。"


def test_fabricated_tiger_pelt_and_wrong_sect_are_dropped_but_generic_shape_kept() -> None:
    kept, dropped = ground_appearance("年轻女性，乌黑长发披肩，身着外宗弟子灰布劲装，披着一张宽大的黄褐色虎皮", _CH1_FRAGMENTS)
    assert dropped == ["身着外宗弟子灰布劲装", "披着一张宽大的黄褐色虎皮"]
    assert kept == "年轻女性，乌黑长发披肩"


def test_clauses_backed_by_the_source_are_kept_and_decorated_ones_dropped() -> None:
    text = "二十许岁女修士，高束乌黑长发，身着银袍，内门弟子，身形挺拔利落"
    kept, dropped = ground_appearance(text, _CH5_FRAGMENTS)
    assert dropped == [] and kept == text
    # 颜色/镶边是通用款式描述，「内门修士」有原文依据：保留
    kept, dropped = ground_appearance("身着银纹镶边的内门修士长袍", _CH5_FRAGMENTS)
    assert dropped == [] and kept == "身着银纹镶边的内门修士长袍"
    # 原文只说她冷漠、会飞：皮甲、佩刀、眉疤都是编的，整句删
    kept, dropped = ground_appearance("成年黑发男子，身穿深灰色皮甲短衫，腰间佩刀，体格壮实，左眉留有一道浅疤", "丁力听令后带人巡查山门。")
    assert kept == "成年黑发男子，体格壮实" and dropped == ["身穿深灰色皮甲短衫", "腰间佩刀", "左眉留有一道浅疤"]


def test_generic_garment_with_colour_is_allowed_without_evidence() -> None:
    kept, dropped = ground_appearance("十六七岁少年，黑发高束，身着灰蓝色宗门劲装，腰间悬储物袋", "孟浩腰间的储物袋鼓鼓囊囊。")
    assert "身着灰蓝色宗门劲装" in dropped  # 「宗门」不是款式/颜色，也不在原文里
    assert kept == "十六七岁少年，黑发高束，腰间悬储物袋"  # 储物袋有原文依据


def test_empty_and_fully_grounded_inputs_pass_through() -> None:
    assert ground_appearance("", "x") == ("", [])
    assert ground_appearance("须发皆白的老者", "两个老者盘膝坐在山顶，须发皆白。") == ("须发皆白的老者", [])
