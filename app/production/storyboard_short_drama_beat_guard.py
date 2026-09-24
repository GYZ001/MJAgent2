"""短剧节奏档：个别（区间外）弃置台词的 ``beat_id`` 核验与放回。

背景（2026-09-24 B 机沙箱真实验证，我欲封天 EP3/EP9）：短剧档允许模型把
非关键整句台词写进 ``dropped_lines``（台词预算驱动，见 ``storyboard_short_
drama_budget.dialogue_budget_rule``），``undroppable_quote_errors``/
``restore_undroppable_lines`` 对这类"区间外、理由非空"的弃置不再打回（见两者
2026-09-24 changelog）。实测证明理由写得通顺不等于删得对：模型用这条豁免删掉
了修炼口诀（Q26「凝气入体，融散全身……」，理由「设定类旁白可通过画面交代」）
与两句目标铺垫（Q28/Q33，理由「非关键铺垫」）——这些都是推动主线的关键节拍，
不是真的可删内容，但没有任何机制核对"这句台词是不是真的对应一个可以删的
节拍"。

本模块补上这道核验：弃置台词必须显式标注 ``beat_id``（schema 层面见
``storyboard_short_drama_schemas._AiShortDramaDroppedLine``，字段必填），且这
个 ``beat_id`` 必须经得起三条检查——存在、所属节拍 ``importance="optional"``、
台词的原文段号落在该节拍 ``segment_indexes`` 覆盖范围内。三条任一不满足，
确定性放回 ``kept_lines``（偏向保留），不打回模型语义重试——与
``storyboard_short_drama`` 模块"两阶段核验"同一立场（CLAUDE.md「修补器与
校验器死锁」：机械可判的问题不该消耗语义重试预算）。

放在独立叶子模块（不进 ``storyboard_short_drama.py``）：那个文件同一批改造
还要扩展 ``SegmentCountSoftCap``（容量归一化预测段数），两块内容一起放不进
500 行——拆分点选在"新增的第三类核验"这条自然边界上，与 ``storyboard_short_
drama_budget.py`` 同一个拆分理由。依赖方向单向：本模块 import
``storyboard_short_drama`` 取 ``_SPAN_DROP_REASON_PREFIX`` 常量，反过来
``storyboard_short_drama`` 不 import 本模块，不构成循环；``storyboard_beat_
sheet.py`` 同时 import 两者并在 ``_validate_beat_sheet_draft`` 里各调一次。

调用时机（见 ``storyboard_beat_sheet._validate_beat_sheet_draft``）：必须放在
``restore_undroppable_lines`` 之后——只有该函数把"作者点名必拍单元"
（``protected_units``）强制放回 ``kept_lines`` 之后，``draft.dropped_lines``
里剩下的才是短剧档豁免下真正的"模型主动弃置"候选集合，本模块只对这部分做
``beat_id`` 核验，避免与该函数的既有保护重复判定同一条台词。

2026-09-24（S3 遗留边角修复）：必须放在 ``append_segments_for_uncovered_
sources`` **之后**（原来紧跟在 ``restore_undroppable_lines`` 之后、在它之前）
——模型把整个原文段漏排时，覆盖该原文段的段要等 ``append_segments_for_
uncovered_sources`` 跑完才存在；本模块的 ``_find_covering_segment_no`` 找不到
覆盖段时会把条目原样留在 ``dropped_lines`` 里出不来，即使它的 ``beat_id``
确实无效、本该被放回，也没法放回。挪到之后不会引入新风险：本模块只读
``draft.segments`` 定位覆盖段、只写 ``kept_lines``/``dropped_lines``；
``append_segments_for_uncovered_sources`` 只读 ``draft.beat_sheet``/
``dropped_units`` 添加新段、完全不读 ``dropped_lines``——两者互不依赖对方的
输出重新判断，挪动顺序不会让 ``append_segments_for_uncovered_sources`` 已经
做出的补段决定被推翻，也不会让本模块的放回被它撤销；仍必须放在 ``repair_
beat_sheet_draft`` 之前——与 ``restore_undroppable_lines`` 同样的理由，此时
``source_unit_ranges`` 可能还没修补完，放回 ``kept_lines`` 时用"覆盖范围
优先、否则按 ``source_segment_indexes`` 兜底"的两层查找，与 ``restore_
undroppable_lines`` 一致，不追求精确（后续 ``reassign_kept_lines_to_
covering_segments`` 会再校正）。

2026-09-24 三处改动为什么不会形成「修补后又被下一道校验打回同一件事」的
循环（``storyboard_short_drama.py`` 模块 docstring 指到这里）：本模块的
beat_id 核验只读/写 ``dropped_lines``/``kept_lines``，不改变 ``draft.
segments`` 的覆盖范围；``storyboard_short_drama.verify_dropped_source_
spans`` 只读 ``draft.beat_sheet``，在 ``reconcile_dropped_units`` 里一次性
过滤 ``dropped_source_spans``，不会在同一轮 ``validate()`` 里被后续修补步骤
重新触发；``SegmentCountSoftCap`` 打回文案的新增内容只是把同一个既有判据
说得更具体，不改变触发条件。三者的输入互不依赖对方在本轮 ``validate()``
调用里刚做出的输出，因此不存在"A 的产出触发 B 打回、B 的修补又让 A 的判断
失效"的循环。
"""
from __future__ import annotations

from typing import Any

from app.production.storyboard_beat_sheet_schemas import DROPPABLE_MAX_CHARS
from app.production.storyboard_dialogue_ledger import _AiKeptLine
from app.production.storyboard_segment_ranges import quote_unit_index
from app.production.storyboard_short_drama import _SPAN_DROP_REASON_PREFIX


def _find_covering_segment_no(draft: Any, source_index: int, unit_no: int) -> int | None:
    """优先找 ``source_unit_ranges`` 真正覆盖这个单元的段；找不到（常见于
    ``repair_beat_sheet_draft`` 还没跑、范围未补齐）退而求其次，找任意一个
    ``source_segment_indexes`` 引用了这个原文段号的段——与
    ``storyboard_beat_sheet_repair.restore_undroppable_lines`` 同一套两层
    兜底，不重新发明一套判据。"""
    for plan in draft.segments:
        for r in plan.source_unit_ranges:
            if r.source_segment_index == source_index and r.from_unit <= unit_no <= r.to_unit:
                return plan.segment_no
    return next(
        (p.segment_no for p in draft.segments if source_index in list(p.source_segment_indexes)), None,
    )


def _beat_id_is_valid(beats_by_id: dict[str, Any], quote: Any, item: Any) -> tuple[bool, str]:
    """三条核验，见模块 docstring；返回 ``(是否合法, 不合法时的人话原因)``。"""
    beat = beats_by_id.get(getattr(item, "beat_id", "") or "")
    if beat is None:
        return False, "beat_id 缺失或指向不存在的节拍"
    if getattr(beat, "importance", None) != "optional":
        return False, "所属节拍不是 optional（key 节拍的台词不许靠 dropped_lines 弃置）"
    if quote.source_segment_index not in beat.segment_indexes:
        return False, "所属节拍的 segment_indexes 没有覆盖这句台词的原文段号"
    return True, ""


def restore_dropped_lines_with_invalid_beat(
    draft: Any, quotes: list[Any], source_segments: list[Any], *, adaptation_mode: str,
) -> list[str]:
    """短剧档新增第三类确定性核验，见模块 docstring；忠实档无副作用直接返回
    空表。只检查"整句台词"（正文超过 ``DROPPABLE_MAX_CHARS``）——语气词/
    屏上文字与随原文区间强制弃置（reason 带 ``_SPAN_DROP_REASON_PREFIX``
    前缀）两类不受这条新规则约束，理由同 ``undroppable_quote_errors``/
    ``restore_undroppable_lines`` 对它们的既有豁免：这两类从来都是可以无
    条件弃置的，不是短剧档台词预算新引入的自由度，不该被新规则收紧。

    2026-09-24（S6 修复）：不再要求 ``quote.speaker`` 非空——小说体台词的
    speaker 可能确定性归属失败留空（我欲封天 EP3 Q26/Q33 即是此例），用它
    当豁免会让这类台词的弃置永远跳过 beat_id 核验，等于放弃保护；是否
    语气词只看 ``content_chars``，与是否抽到说话人无关，见
    ``storyboard_short_drama_review_items._line_items`` 同日同理由的修复。
    """
    if adaptation_mode != "short_drama":
        return []
    quotes_by_id = {q.quote_id: q for q in quotes}
    beats_by_id = {beat.beat_id: beat for beat in draft.beat_sheet}
    notes: list[str] = []
    remaining: list[Any] = []
    for item in draft.dropped_lines:
        quote = quotes_by_id.get(item.quote_id)
        if (
            str(item.reason).startswith(_SPAN_DROP_REASON_PREFIX)
            or quote is None
            or quote.content_chars <= DROPPABLE_MAX_CHARS
        ):
            remaining.append(item)
            continue
        valid, why = _beat_id_is_valid(beats_by_id, quote, item)
        if valid:
            remaining.append(item)
            continue
        idx = quote.source_segment_index
        unit_no = quote_unit_index(quote, source_segments[idx - 1].text) if 1 <= idx <= len(source_segments) else -1
        segment_no = _find_covering_segment_no(draft, idx, unit_no)
        if segment_no is None:
            remaining.append(item)
            continue
        draft.kept_lines.append(_AiKeptLine(quote_id=quote.quote_id, segment_no=segment_no))
        notes.append(
            f"{quote.quote_id}「{quote.text[:16]}」弃置声明的 beat_id={getattr(item, 'beat_id', '')!r} 无效"
            f"（{why}），放回 kept_lines（第 {segment_no} 段）"
        )
    draft.dropped_lines = remaining
    return notes
