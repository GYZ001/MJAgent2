"""短剧节奏档：``SegmentCountSoftCap`` 打回文案「具体在哪一段」提示（2026-09-24）。

背景（B 机沙箱第三轮真实验证）：打回文案原来只给"预计 N 段，超过上限 M 段"
一句总量提示，模型不知道该在哪一段动手——我欲封天 EP3 两次按预计段数打回后
模型都没有减少（12 段/180s 原样不动，直到最后一次降级放行、如实 over_target）。
这里按容量归一化的拆分遥测（``storyboard_capacity_normalize.
normalize_beat_sheet_capacity`` 已经算出"哪个原段被拆成几箱"，不重新猜）反查
每个原段保留台词字数，按超出量降序给模型最多几段的具体数字，让它知道具体
删哪一段的台词或者怎么重新分段。

放在独立叶子模块（不进 ``storyboard_short_drama.py``）：那个文件当时已经没有
行数余量（见其 2026-09-24 changelog）。本模块只依赖 ``app.config`` 与调用方
传入的 ``draft``/``quotes``/``telemetry``，不依赖 ``storyboard_short_drama``
的任何符号——``storyboard_short_drama_budget.py`` 已经单向 import
``storyboard_short_drama`` 取 ``DIALOGUE_BUDGET_CHARS``，若本模块反过来塞进
``storyboard_short_drama_budget.py``，``budget → short_drama → (import
budget)`` 就会成环；``storyboard_short_drama.SegmentCountSoftCap`` 单向
import 本模块，方向与 budget.py 相反但同样是单向，不构成循环。
"""
from __future__ import annotations

from typing import Any

from app.config import MAX_SPOKEN_CHARS_PER_SHOT

#: 打回文案最多点名几个段——列全部反而让模型抓不住重点，见 oversized_segment_hint。
_MAX_HINTED_SEGMENTS = 5


def _kept_chars_by_segment(draft: Any, quotes: list[Any]) -> dict[int, int]:
    """每个（原）段号当前 kept_lines 台词的纯文字字数合计，口径与
    ``storyboard_short_drama_budget.kept_dialogue_chars`` 相同的
    ``DialogueQuote.content_chars`` 求和，只是按 segment_no 分组。"""
    quotes_by_id = {q.quote_id: q for q in quotes}
    chars: dict[int, int] = {}
    for item in draft.kept_lines:
        quote = quotes_by_id.get(item.quote_id)
        if quote is not None:
            chars[item.segment_no] = chars.get(item.segment_no, 0) + int(quote.content_chars or 0)
    return chars


def oversized_segment_hint(draft: Any, quotes: list[Any], telemetry: list[dict[str, Any]]) -> str:
    """按保留台词字数（超出量的排序等价，因为容量是常数）降序，最多列出
    ``_MAX_HINTED_SEGMENTS`` 个会被容量归一化拆分的段：段号、保留台词字数、
    容量、拆成几段。``telemetry`` 直接取自 ``normalize_beat_sheet_capacity``
    的返回值（``bin_count>1`` 才是真被拆分的段），不重新实现装箱判断。没有
    任何段会被拆分时返回空串，调用方据此决定是否往打回文案里插这一句。
    """
    kept_chars = _kept_chars_by_segment(draft, quotes)
    rows = sorted(
        (
            (record["original_segment_no"], kept_chars.get(record["original_segment_no"], 0), record["bin_count"])
            for record in telemetry if record["bin_count"] > 1
        ),
        key=lambda row: row[1], reverse=True,
    )[:_MAX_HINTED_SEGMENTS]
    return "；".join(
        f"第 {no} 段保留台词 {chars} 字（容量 {MAX_SPOKEN_CHARS_PER_SHOT} 字/段，将被拆成 {bins} 段）"
        for no, chars, bins in rows
    )
