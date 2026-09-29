"""连续性备忘的屏幕行进方向规则文案（app.production.storyboard_continuity_memo
里的接线部分）：回填/沿用判据本身已拆到 app.production.storyboard_travel_direction
（见 tests/test_storyboard_travel_direction.py），本文件只覆盖
``continuity_memo_rules``/``continuity_memo_output_contract_text`` 是否正确
把 travel_direction 的规则文案接进去，以及方言层是否配套。

2026-09-14 用户看片：第 2 集走山路时三个人各走各的方向——此前提示词与备忘里没有任何屏幕方向信息。
2026-09-28 改版：travel_direction 不再「默认沿用上一段」，规则只把上一段的值当参考，不当默认值。
"""
from __future__ import annotations

from app.production.storyboard_continuity_memo import (
    _AiContinuityMemo,
    continuity_memo_output_contract_text,
    continuity_memo_rules,
)
from app.production.storyboard_dialects import SEEDANCE_DIALECT_INSTRUCTIONS


def _memo(**kw) -> _AiContinuityMemo:
    return _AiContinuityMemo(time_of_day="白日", **kw)


def test_rules_and_contract_and_dialect_all_state_the_direction_rule() -> None:
    with_prev = "".join(continuity_memo_rules(_memo(travel_direction="一行人自画左向画右沿山路行进")))
    assert "上一段记录的屏幕行进方向是「一行人自画左向画右沿山路行进」" in with_prev
    assert "不是本段的默认值" in with_prev
    first = "".join(continuity_memo_rules(None))
    assert "没有人物位移" in first and "静止" in first
    assert "travel_direction" in continuity_memo_output_contract_text()
    assert "同一屏幕行进" in SEEDANCE_DIALECT_INSTRUCTIONS and "不反向" in SEEDANCE_DIALECT_INSTRUCTIONS


def test_output_contract_text_does_not_promise_default_inherit() -> None:
    """契约文案不再暗示「默认沿用」，明确写清即使方向恰好相同也要本段自行确认。"""
    text = continuity_memo_output_contract_text()
    assert "不是上一段的默认延续" in text
