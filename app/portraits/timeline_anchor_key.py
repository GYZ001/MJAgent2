"""WS9：时间线锚点 -> 可查询/可展示的规范键（不改 ``timeline_anchors.py`` 一行）。

``app.portraits.timeline_anchors.TimelineAnchor.value`` 对 ``age`` 类锚点必须保留
原文数字表述（"八岁"/"9岁"）——那是提取契约本身的逐字核验要求，不能改成阿拉伯
数字。但 ``app.portraits.portrait_lookup.portrait_lookup_for_episode`` 的
``time_anchor``/``character_portraits.anchor_key`` 需要一个稳定、可比较的键——
``tests/test_portrait_anchor_lookup.py`` 已经约定了这个键的形状："age:8"/
"year:2022"，纯数字，供未来人物谱"按年龄段建卡"UI 直接拼出同一个键。

本模块只做这一件事：年龄/年份锚点 -> 规范键 + 人类可读展示文本；``era``/
``relative`` 锚点没有单点数值，不构成可查询键，一律返回 None——不强行凑一个
（CLAUDE.md「不得兜底填充」）。中文数字解析有界（0~999，覆盖故事人物年龄/公历
年份场景足够），遇到未知字符 fail-closed 返回 None，不猜。
"""
from __future__ import annotations

import re

_CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100}


def _cn_number_to_int(text: str) -> int | None:
    """有界中文数字解析；纯阿拉伯数字直接转换；遇到未知字符返回 None。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if any(ch not in _CN_DIGIT and ch not in _CN_UNIT for ch in text):
        return None
    total = 0
    current = 0
    for ch in text:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
        else:
            total += (current or 1) * _CN_UNIT[ch]
            current = 0
    total += current
    return total or None


def _leading_number_token(text: str) -> str | None:
    match = re.match(r"\s*(\d+|[零一二两三四五六七八九十百]+)", text or "")
    return match.group(1) if match else None


def normalized_age(value: str) -> int | None:
    """从 age 锚点的 ``value``（如"八岁"/"9岁"）解析出岁数；解析不出返回 None。"""
    token = _leading_number_token((value or "").strip())
    return _cn_number_to_int(token) if token else None


def normalized_year(value: str) -> int | None:
    """从 year 锚点的 ``value``（如"2004年"）取开头的年份数字。"""
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def anchor_key(kind: str, value: str) -> str | None:
    """锚点 -> ``character_portraits.anchor_key``/``portrait_lookup_for_episode``
    ``time_anchor`` 同形状的查询键；不可归一化（era/relative，或数字解析失败）
    时返回 None。"""
    if kind == "age":
        age = normalized_age(value)
        return f"age:{age}" if age is not None else None
    if kind == "year":
        year = normalized_year(value)
        return f"year:{year}" if year is not None else None
    return None


def display_label(kind: str, value: str) -> str:
    """人类可读展示文本，供 ``scene_time``/告警文案使用；数字解析不出时原样展示
    ``value``（例如 era/relative 锚点本来就是文字表述，不需要数字化）。"""
    if kind == "age":
        age = normalized_age(value)
        return f"{age}岁" if age is not None else value
    if kind == "year":
        year = normalized_year(value)
        return f"{year}年" if year is not None else value
    return value


__all__ = ["anchor_key", "display_label", "normalized_age", "normalized_year"]
