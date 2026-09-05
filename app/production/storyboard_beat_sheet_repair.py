"""节拍表草稿的确定性修补：机械规则不交给模型自律。

2026-09-05 对连播台 18 集失败的复盘：分镜台被校验打回的 23 次里，同场戏色温不一致 5、
单元范围越界 4、原文单元有洞 3、范围回退重叠 2、同一原文段多条范围 1——这五类都有唯一
确定的修法，原来却把整份节拍表打回让模型重来，重试耗尽整集失败。这里在校验之前先把
它们修掉，只把真正需要判断的问题（台词归属、叙事切分）留给校验与模型。

修补只动 ``palette`` 与 ``source_unit_ranges``，不改段的划分与叙事内容；每一处修改都
返回一条人话记录，由调用方写进日志，不静默。
"""
from __future__ import annotations

from typing import Any

from app.production.storyboard_segment_ranges import split_source_units


def repair_beat_sheet_draft(
    draft: Any, source_segments: list[Any], paratext_indexes: set[int],
) -> list[str]:
    """就地修补节拍表草稿；返回修改记录（空表示没动）。"""
    unit_counts = {
        i: len(split_source_units(seg.text)) for i, seg in enumerate(source_segments, start=1)
    }
    notes: list[str] = []
    notes.extend(unify_scene_palettes(draft.segments))
    for plan in draft.segments:
        notes.extend(merge_duplicate_ranges(plan))
        notes.extend(clamp_unit_ranges(plan, unit_counts, paratext_indexes))
    notes.extend(fix_order_and_fill_holes(draft.segments, unit_counts))
    return notes


def unify_scene_palettes(segments: list[Any]) -> list[str]:
    """相邻两段引用完全相同的原文段落（同一场戏）时色温以前一段为准；
    空 palette 是模型漏填的信号，不在这里兜底，留给校验去报。"""
    notes: list[str] = []
    for prev, cur in zip(segments, segments[1:]):
        if list(prev.source_segment_indexes) != list(cur.source_segment_indexes):
            continue
        if not prev.palette or not cur.palette or prev.palette == cur.palette:
            continue
        notes.append(
            f"第 {cur.segment_no} 段 palette「{cur.palette}」改为与同场戏第 {prev.segment_no} 段"
            f"逐字相同「{prev.palette}」"
        )
        cur.palette = prev.palette
    return notes


def merge_duplicate_ranges(plan: Any) -> list[str]:
    """同一段对同一原文段声明多条范围：相接/重叠的合并成一条；彼此分离的保留最长的一条，
    其余单元由 ``fix_order_and_fill_holes`` 按相邻段补齐。"""
    notes: list[str] = []
    by_index: dict[int, list[Any]] = {}
    for r in plan.source_unit_ranges:
        by_index.setdefault(r.source_segment_index, []).append(r)
    kept: list[Any] = []
    for index, entries in by_index.items():
        if len(entries) == 1:
            kept.extend(entries)
            continue
        entries.sort(key=lambda r: (r.from_unit, r.to_unit))
        merged = [entries[0]]
        for r in entries[1:]:
            last = merged[-1]
            if r.from_unit <= last.to_unit + 1:
                last.to_unit = max(last.to_unit, r.to_unit)
            else:
                merged.append(r)
        winner = max(merged, key=lambda r: r.to_unit - r.from_unit)
        notes.append(
            f"第 {plan.segment_no} 段对原文段 {index} 声明了 {len(entries)} 条范围，合并为 "
            f"S{winner.from_unit:02d}-S{winner.to_unit:02d}"
        )
        kept.append(winner)
    if len(kept) != len(plan.source_unit_ranges):
        plan.source_unit_ranges = kept
    return notes


def clamp_unit_ranges(plan: Any, unit_counts: dict[int, int], paratext_indexes: set[int]) -> list[str]:
    """越界的范围裁进 1..该原文段单元数；from > to 时收成单点。"""
    notes: list[str] = []
    for r in plan.source_unit_ranges:
        total = unit_counts.get(r.source_segment_index)
        if total is None or r.source_segment_index in paratext_indexes:
            continue
        before = (r.from_unit, r.to_unit)
        r.from_unit = max(1, min(r.from_unit, total))
        r.to_unit = max(r.from_unit, min(r.to_unit, total))
        if (r.from_unit, r.to_unit) != before:
            notes.append(
                f"第 {plan.segment_no} 段对原文段 {r.source_segment_index} 的范围 "
                f"S{before[0]:02d}-S{before[1]:02d} 裁为 S{r.from_unit:02d}-S{r.to_unit:02d}"
                f"（该段共 {total} 个单元）"
            )
    return notes


def fix_order_and_fill_holes(segments: list[Any], unit_counts: dict[int, int]) -> list[str]:
    """同一原文段被多段引用时：按段序不回退（允许重叠一个单元），并集覆盖全部单元。
    回退的 from 提到前一段的 to；洞由前一段的 to 延伸补齐；首段延到 1、末段延到总数。"""
    notes: list[str] = []
    by_source: dict[int, list[tuple[int, Any]]] = {}
    for plan in segments:
        for r in plan.source_unit_ranges:
            if r.source_segment_index in unit_counts:
                by_source.setdefault(r.source_segment_index, []).append((plan.segment_no, r))
    for index, entries in sorted(by_source.items()):
        total = unit_counts[index]
        entries.sort(key=lambda item: (item[0], item[1].from_unit))
        prev_no, prev = None, None
        for seg_no, r in entries:
            if prev is not None and r.from_unit < prev.to_unit:
                notes.append(
                    f"原文段 {index}：第 {seg_no} 段范围从 S{r.from_unit:02d} 回退到第 {prev_no} 段的"
                    f" S{prev.to_unit:02d} 之前，from 提到 S{prev.to_unit:02d}"
                )
                r.from_unit = prev.to_unit
                r.to_unit = max(r.to_unit, r.from_unit)
            prev_no, prev = seg_no, r
        first_no, first = entries[0]
        if first.from_unit > 1:
            notes.append(f"原文段 {index}：单元 S01-S{first.from_unit - 1:02d} 无人覆盖，并入第 {first_no} 段")
            first.from_unit = 1
        for (a_no, a), (_b_no, b) in zip(entries, entries[1:]):
            if b.from_unit > a.to_unit + 1:
                notes.append(
                    f"原文段 {index}：单元 S{a.to_unit + 1:02d}-S{b.from_unit - 1:02d} 无人覆盖，并入第 {a_no} 段"
                )
                a.to_unit = b.from_unit - 1
        last_no, last = entries[-1]
        if last.to_unit < total:
            notes.append(f"原文段 {index}：单元 S{last.to_unit + 1:02d}-S{total:02d} 无人覆盖，并入第 {last_no} 段")
            last.to_unit = total
    return notes


def restore_undroppable_lines(draft: Any, quotes: list[Any], source_segments: list[Any]) -> list[str]:
    """模型把整句台词（有说话人、正文超过语气词长度）塞进 dropped_lines 时，放回 kept_lines：
    先归到单元范围覆盖它的段，否则归到引用其原文段且必保台词字数最少的段；没有任何段引用
    它的原文段就留在 dropped_lines，由 undroppable_quote_errors 报「新增段落」。
    2026-09-05 我欲封天第 3 集：Q22「我爹是财主……」被弃置，三次重试仍打回。"""
    from app.production.storyboard_dialogue_ledger import _AiKeptLine
    from app.production.storyboard_beat_sheet import DROPPABLE_MAX_CHARS
    from app.production.storyboard_segment_ranges import quote_unit_index

    by_id = {q.quote_id: q for q in quotes}
    chars: dict[int, int] = {}
    for item in draft.kept_lines:
        q = by_id.get(item.quote_id)
        if q is not None:
            chars[item.segment_no] = chars.get(item.segment_no, 0) + int(q.content_chars or 0)
    notes: list[str] = []
    remaining = []
    for item in draft.dropped_lines:
        quote = by_id.get(item.quote_id)
        if quote is None or not getattr(quote, "speaker", "") or quote.content_chars <= DROPPABLE_MAX_CHARS:
            remaining.append(item)
            continue
        idx = quote.source_segment_index
        unit_no = quote_unit_index(quote, source_segments[idx - 1].text) if 1 <= idx <= len(source_segments) else -1
        covering = [
            p.segment_no for p in draft.segments
            for r in p.source_unit_ranges
            if r.source_segment_index == idx and unit_no >= 1 and r.from_unit <= unit_no <= r.to_unit
        ]
        candidates = covering or [p.segment_no for p in draft.segments if idx in list(p.source_segment_indexes)]
        if not candidates:
            remaining.append(item)
            continue
        target = min(candidates, key=lambda no: (chars.get(no, 0), no))
        chars[target] = chars.get(target, 0) + int(quote.content_chars or 0)
        draft.kept_lines.append(_AiKeptLine(quote_id=quote.quote_id, segment_no=target))
        notes.append(f"{quote.quote_id}「{quote.text[:16]}」不可弃置，从 dropped_lines 放回第 {target} 段")
    draft.dropped_lines = remaining
    return notes
