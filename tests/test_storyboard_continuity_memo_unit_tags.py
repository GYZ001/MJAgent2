"""layout_change_source_quote 逐字匹配必须剥掉 2.4.0 的句单元标签「[段N·S07]」。

2026-09-14 我欲封天第 1–3 集 19 条「在本段原文里找不到逐字匹配（未拦截）」实测：9 条是跨句
引用夹着单元标签的假阳性（旧正则只认「[段N]」），另 9 条是模型压缩拼接、应当继续告警。
"""
from __future__ import annotations

from app.production.storyboard_continuity_memo import _quote_found_in_source

WINDOW = (
    "[段3·S04] 这小胖子居然一口狠狠的咬在了桌子角上，留下一个深深的牙印后，这才又回到了床上继续睡觉，"
    "\n[段3·S05] 孟浩看了小胖子半天，小心翼翼的挪远了一些。"
)


def test_quote_spanning_two_units_matches_after_stripping_unit_tags() -> None:
    quote = "留下一个深深的牙印后，这才又回到了床上继续睡觉，孟浩看了小胖子半天，小心翼翼的挪远了一些"
    assert _quote_found_in_source(quote, WINDOW)


def test_legacy_segment_tags_still_stripped() -> None:
    assert _quote_found_in_source("甲乙丙丁", "[段2] 甲乙\n[段3] 丙丁")


def test_paraphrase_still_rejected() -> None:
    assert not _quote_found_in_source("孟浩看了胖子半天，挪远了一些", WINDOW)
