"""app.portraits.timeline_anchor_key：时间线锚点 -> 规范键/展示文本。

覆盖：阿拉伯数字与中文数字两种 value 写法都能解析；era/relative 与解析失败
的 age/year 一律返回 None（不强行凑一个查询键）。
"""
from __future__ import annotations

from app.portraits.timeline_anchor_key import (
    anchor_key,
    display_label,
    normalized_age,
    normalized_year,
)


def test_normalized_age_parses_arabic_digits():
    assert normalized_age("9岁") == 9
    assert normalized_age("35岁") == 35


def test_normalized_age_parses_chinese_numerals():
    assert normalized_age("八岁") == 8
    assert normalized_age("十六岁") == 16
    assert normalized_age("十八岁") == 18
    assert normalized_age("二十岁") == 20
    assert normalized_age("三十五岁") == 35
    assert normalized_age("一百二十岁") == 120


def test_normalized_age_returns_none_for_unparseable_text():
    assert normalized_age("") is None
    assert normalized_age("很小的时候") is None


def test_normalized_year_takes_leading_digits():
    assert normalized_year("2004年") == 2004
    assert normalized_year("2009年10月") == 2009


def test_normalized_year_returns_none_without_digits():
    assert normalized_year("民国初年") is None


def test_anchor_key_age_and_year():
    assert anchor_key("age", "八岁") == "age:8"
    assert anchor_key("year", "2004年") == "year:2004"


def test_anchor_key_none_for_era_and_relative():
    assert anchor_key("era", "东汉末年") is None
    assert anchor_key("relative", "三年后") is None


def test_anchor_key_none_when_age_unparseable():
    assert anchor_key("age", "很小的时候") is None


def test_display_label_normalizes_age_and_year_but_keeps_era_verbatim():
    assert display_label("age", "八岁") == "8岁"
    assert display_label("year", "2004年") == "2004年"
    assert display_label("era", "东汉末年") == "东汉末年"
    assert display_label("relative", "三年后") == "三年后"
