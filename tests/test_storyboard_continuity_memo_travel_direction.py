"""连续性备忘的屏幕行进方向：默认逐字沿用，原文写到转向才可改并引用原句；只告警不阻断。

2026-09-14 用户看片：第 2 集走山路时三个人各走各的方向——此前提示词与备忘里没有任何屏幕方向信息。
"""
from __future__ import annotations

from app.production.storyboard_continuity_memo import (
    _AiContinuityMemo,
    continuity_memo_output_contract_text,
    continuity_memo_rules,
    travel_direction_advisories,
)
from app.production.storyboard_dialects import SEEDANCE_DIALECT_INSTRUCTIONS

SOURCE = "[段2·S03] 孟浩转身，向着来路折返而去。"


def _memo(**kw) -> _AiContinuityMemo:
    return _AiContinuityMemo(time_of_day="白日", **kw)


def test_direction_change_without_quote_is_advised_not_blocked() -> None:
    prev = _memo(travel_direction="一行人自画左向画右沿山路行进")
    memo = _memo(travel_direction="孟浩自画右向画左折返")
    assert len(travel_direction_advisories(memo, prev, SOURCE)) == 1


def test_direction_change_with_verbatim_quote_has_no_advisory() -> None:
    prev = _memo(travel_direction="一行人自画左向画右沿山路行进")
    memo = _memo(travel_direction="孟浩自画右向画左折返", travel_direction_change_source_quote="孟浩转身，向着来路折返而去")
    assert travel_direction_advisories(memo, prev, SOURCE) == []


def test_verbatim_inherit_and_first_segment_have_no_advisory() -> None:
    prev = _memo(travel_direction="一行人自画左向画右沿山路行进")
    assert travel_direction_advisories(_memo(travel_direction="一行人自画左向画右沿山路行进"), prev, SOURCE) == []
    assert travel_direction_advisories(_memo(travel_direction="静止"), None, SOURCE) == []


def test_rules_and_contract_and_dialect_all_state_the_direction_rule() -> None:
    with_prev = "".join(continuity_memo_rules(_memo(travel_direction="一行人自画左向画右沿山路行进")))
    assert "上一段记录的屏幕行进方向是「一行人自画左向画右沿山路行进」" in with_prev
    first = "".join(continuity_memo_rules(None))
    assert "由第一个行进镜头定下走向" in first
    assert "travel_direction" in continuity_memo_output_contract_text()
    assert "同一屏幕行进" in SEEDANCE_DIALECT_INSTRUCTIONS and "不反向" in SEEDANCE_DIALECT_INSTRUCTIONS
