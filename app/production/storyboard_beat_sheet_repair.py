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

from app.production.storyboard_beat_sheet_schemas import DROPPABLE_MAX_CHARS, _AiSegmentPlan
from app.production.storyboard_segment_ranges import _AiSourceUnitRange, split_source_units


def repair_beat_sheet_draft(
    draft: Any, source_segments: list[Any], paratext_indexes: set[int],
    *, dropped_units: frozenset[tuple[int, int]],
) -> list[str]:
    """就地修补节拍表草稿；返回修改记录（空表示没动）。

    ``dropped_units`` 是短剧节奏档（见 app.production.storyboard_short_drama）
    确定性核验后仍然有效的删减单元集合，忠实档调用方恒传空集合——必传、无
    默认值，漏传在调用那一刻就是 TypeError，不会静默把删减区间又补回来。
    """
    unit_counts = {
        i: len(split_source_units(seg.text)) for i, seg in enumerate(source_segments, start=1)
    }
    notes: list[str] = []
    notes.extend(unify_scene_palettes(draft.segments))
    notes.extend(fill_single_owner_ranges(draft.segments, unit_counts, paratext_indexes))
    for plan in draft.segments:
        notes.extend(merge_duplicate_ranges(plan))
        notes.extend(clamp_unit_ranges(plan, unit_counts, paratext_indexes))
    notes.extend(fix_order_and_fill_holes(draft.segments, unit_counts, dropped_units=dropped_units))
    return notes


def fill_single_owner_ranges(segments: list[Any], unit_counts: dict[int, int], paratext_indexes: set[int]) -> list[str]:
    """原文段仅被一个分镜引用时，缺失范围唯一等于整段；多人认领时留给原校验。"""
    owners: dict[int, list[Any]] = {}
    for plan in segments:
        for index in set(plan.source_segment_indexes):
            owners.setdefault(index, []).append(plan)
    notes = []
    for index, plans in owners.items():
        total = unit_counts.get(index, 0)
        if len(plans) != 1 or index in paratext_indexes or not total:
            continue
        if any(r.source_segment_index == index for plan in segments for r in plan.source_unit_ranges):
            continue
        plan = plans[0]
        plan.source_unit_ranges.append(_AiSourceUnitRange(source_segment_index=index, from_unit=1, to_unit=total))
        notes.append(f"原文段 {index} 仅由第 {plan.segment_no} 段引用，补齐其唯一完整范围 S01-S{total:02d}")
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


def _gap_fill_plan(
    index: int, g_start: int, g_end: int, dropped_units: frozenset[tuple[int, int]],
    *, has_left: bool, has_right: bool,
) -> tuple[int, int, bool]:
    """决定缺口 [g_start, g_end] 怎么处理，返回 ``(left_run_end, right_run_start,
    fallback_full)``：

    - 缺口整段都不在 ``dropped_units`` 内：``fallback_full=True``——没有任何
      单元被声明删减，按旧行为整体回填，调用方决定并入哪一段。
    - 缺口整段都在 ``dropped_units`` 内：``fallback_full=False``、两侧都不
      延伸（``left_run_end=g_start-1``、``right_run_start=g_end+1``），缺口
      原样保持空洞。
    - 缺口部分在 ``dropped_units`` 内（模型单元范围差一位是常态，这是最常见
      的形态）：非删减单元只贴着 ``has_left``/``has_right`` 一侧边缘出现时，
      ``fallback_full=False``，``left_run_end``/``right_run_start`` 标出各自
      能延伸到哪——调用方只把贴边的非删减部分并入对应相邻段，中间真正声明
      删减的单元保持空洞。如果去掉两侧贴边游程后，中间仍残留两侧都够不到的
      非删减单元（无法用一段连续范围覆盖），``fallback_full=True``：按旧行为
      整体回填，这部分删减声明随之失效（真实原因见 ``storyboard_short_drama``
      模块 docstring）。
    """
    if not any((index, u) in dropped_units for u in range(g_start, g_end + 1)):
        return g_start, g_end, True
    if all((index, u) in dropped_units for u in range(g_start, g_end + 1)):
        return g_start - 1, g_end + 1, False
    left_run_end = g_start - 1
    if has_left:
        u = g_start
        while u <= g_end and (index, u) not in dropped_units:
            left_run_end = u
            u += 1
    right_run_start = g_end + 1
    if has_right:
        u = g_end
        while u >= g_start and (index, u) not in dropped_units:
            right_run_start = u
            u -= 1
    if all((index, u) in dropped_units for u in range(left_run_end + 1, right_run_start)):
        return left_run_end, right_run_start, False
    return g_start, g_end, True


def _fill_leading_gap(index: int, entries: list[tuple[int, Any]], dropped_units: frozenset[tuple[int, int]]) -> list[str]:
    first_no, first = entries[0]
    if first.from_unit <= 1:
        return []
    g_start, g_end = 1, first.from_unit - 1
    _left, right_from, fallback = _gap_fill_plan(index, g_start, g_end, dropped_units, has_left=False, has_right=True)
    if fallback:
        first.from_unit = 1
        return [f"原文段 {index}：单元 S01-S{g_end:02d} 无人覆盖，并入第 {first_no} 段"]
    if right_from <= g_end:
        first.from_unit = right_from
        return [f"原文段 {index}：单元 S{right_from:02d}-S{g_end:02d} 无人覆盖（其余在有效删减范围内），并入第 {first_no} 段"]
    return []


def _fill_between_gaps(index: int, entries: list[tuple[int, Any]], dropped_units: frozenset[tuple[int, int]]) -> list[str]:
    notes: list[str] = []
    for (a_no, a), (b_no, b) in zip(entries, entries[1:]):
        if b.from_unit <= a.to_unit + 1:
            continue
        g_start, g_end = a.to_unit + 1, b.from_unit - 1
        left_to, right_from, fallback = _gap_fill_plan(index, g_start, g_end, dropped_units, has_left=True, has_right=True)
        if fallback:
            notes.append(f"原文段 {index}：单元 S{g_start:02d}-S{g_end:02d} 无人覆盖，并入第 {a_no} 段")
            a.to_unit = g_end
            continue
        if left_to >= g_start:
            notes.append(f"原文段 {index}：单元 S{g_start:02d}-S{left_to:02d} 无人覆盖（贴第 {a_no} 段边缘，其余在有效删减范围内），并入第 {a_no} 段")
            a.to_unit = left_to
        if right_from <= g_end:
            notes.append(f"原文段 {index}：单元 S{right_from:02d}-S{g_end:02d} 无人覆盖（贴第 {b_no} 段边缘，其余在有效删减范围内），并入第 {b_no} 段")
            b.from_unit = right_from
    return notes


def _fill_trailing_gap(index: int, entries: list[tuple[int, Any]], total: int, dropped_units: frozenset[tuple[int, int]]) -> list[str]:
    last_no, last = entries[-1]
    if last.to_unit >= total:
        return []
    g_start, g_end = last.to_unit + 1, total
    left_to, _right, fallback = _gap_fill_plan(index, g_start, g_end, dropped_units, has_left=True, has_right=False)
    if fallback:
        last.to_unit = total
        return [f"原文段 {index}：单元 S{g_start:02d}-S{total:02d} 无人覆盖，并入第 {last_no} 段"]
    if left_to >= g_start:
        last.to_unit = left_to
        return [f"原文段 {index}：单元 S{g_start:02d}-S{left_to:02d} 无人覆盖（其余在有效删减范围内），并入第 {last_no} 段"]
    return []


def fix_order_and_fill_holes(
    segments: list[Any], unit_counts: dict[int, int], *, dropped_units: frozenset[tuple[int, int]],
) -> list[str]:
    """同一原文段被多段引用时：按段序不回退（允许重叠一个单元），并集覆盖全部单元
    ——整段都在 ``dropped_units`` 内的缺口除外，那是短剧档已核验有效的删减，
    留空洞才是正确行为；缺口只有一部分在 ``dropped_units`` 内（模型单元范围差
    一位是常态）时，只把贴边的非删减单元并入相邻段，删减部分继续留空
    （见 ``_gap_fill_plan`` 与 ``storyboard_short_drama`` 模块 docstring）。
    回退的 from 提到前一段的 to；首段/末段贴边缺口分别延到 1/总数。"""
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
        notes.extend(_fill_leading_gap(index, entries, dropped_units))
        notes.extend(_fill_between_gaps(index, entries, dropped_units))
        notes.extend(_fill_trailing_gap(index, entries, total, dropped_units))
    return notes


def restore_undroppable_lines(
    draft: Any, quotes: list[Any], source_segments: list[Any], *, dropped_units: frozenset[tuple[int, int]],
) -> list[str]:
    """模型把整句台词（有说话人、正文超过语气词长度）塞进 dropped_lines 时，放回 kept_lines：
    先归到单元范围覆盖它的段，否则归到引用其原文段且必保台词字数最少的段；没有任何段引用
    它的原文段就留在 dropped_lines，由 undroppable_quote_errors 报「新增段落」。
    2026-09-05 我欲封天第 3 集：Q22「我爹是财主……」被弃置，三次重试仍打回。
    ``dropped_units``（短剧节奏档确定性核验后仍然有效的删减单元，忠实档恒传
    空集合，必传无默认值）内的台词不在此列——它们已被
    ``storyboard_short_drama.reconcile_dropped_units`` 强制并入 dropped_lines，
    这里必须放行而不是强行修回 kept_lines，否则短剧档的删减声明会被这道既有
    修补悄悄撤销。"""
    from app.production.storyboard_dialogue_ledger import _AiKeptLine
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
        if (idx, unit_no) in dropped_units:
            remaining.append(item)
            continue
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


def complete_missing_quote_decisions(draft: Any, quotes: list[Any]) -> list[str]:
    """dialogue_targets 里既不在 kept_lines 也不在 dropped_lines 的台词，先一律补进 dropped_lines
    （reason 注明由后端补齐），紧接着的 restore_undroppable_lines 会把其中不可弃置的整句放回覆盖它
    的段；真正的语气词/短句就留在弃置区。模型一次长调用漏掉一两句是常态（2026-09-05 第 3 集 Q41），
    按「必须显式决定去留」整份打回、三次重试后判整集失败，代价远大于确定性补齐。"""
    from app.production.storyboard_dialogue_ledger import _AiDroppedLine
    referenced = {k.quote_id for k in draft.kept_lines} | {d.quote_id for d in draft.dropped_lines}
    notes: list[str] = []
    for quote in quotes:
        if quote.quote_id in referenced:
            continue
        draft.dropped_lines.append(_AiDroppedLine(quote_id=quote.quote_id, reason="模型未决定去留，由后端按可弃置规则补齐"))
        notes.append(f"{quote.quote_id}「{quote.text[:16]}」模型未决定去留，先补进 dropped_lines 再按规则复核")
    return notes


def append_segments_for_uncovered_sources(
    draft: Any, quotes: list[Any], source_segments: list[Any], paratext_indexes: set[int],
    context_indexes: set[int] = frozenset(), *, dropped_units: frozenset[tuple[int, int]],
) -> list[str]:
    """模型把整个原文段漏排（segments 只覆盖 [1, 2]，原文段 3、4 的必保台词没有任何段覆盖）时，
    按原文顺序补一段：source_segment_indexes=[N]、单元范围覆盖整段（容量归一化会再按 15 秒拆），
    beat_ids 取节拍表里指向该原文段的节拍，色温沿用前一段（同场戏色温一致的既有规则）。
    synopsis 只写「按原文补齐」的事实，不编内容——阶段二读的是该段原文，不是这句概括。
    只对"有必保台词却无段覆盖"的原文段动手（判据从数据来），paratext 段不补。
    2026-09-05 第 2 集：三次重试模型都没补段，整集失败。
    ``dropped_units``（短剧节奏档确定性核验后仍然有效的删减单元，忠实档恒传
    空集合，必传无默认值）覆盖某个原文段全部单元时，这个原文段本就该整段不
    补——补段会让短剧档的删减声明失效，退回忠实档行为。"""
    _ = quotes  # 判据不再依赖必保台词：任何非副文本原文段没有段覆盖，交付门禁都会拦（2026-09-05 第 4 集尾段无台词被漏排）
    covered = {i for p in draft.segments for i in p.source_segment_indexes}
    needed = [i for i in range(1, len(source_segments) + 1) if i not in covered and i not in paratext_indexes]
    notes: list[str] = []
    for index in needed:
        units = len(split_source_units(source_segments[index - 1].text))
        if units < 1:
            continue
        # 只补「非删减单元」的最小到最大——不是整段 1..units：补进已声明删减的
        # 单元会让短剧档的删减声明失效（context 段从不进 dropped_units，见
        # storyboard_short_drama.reconcile_dropped_units 的 paratext/context 排除，
        # 这里对它算出的范围恒等于 1..units，行为不变）。
        remaining = [u for u in range(1, units + 1) if (index, u) not in dropped_units]
        if not remaining:
            continue
        from_unit, to_unit = remaining[0], remaining[-1]
        after = max([i for i, p in enumerate(draft.segments) if any(x < index for x in p.source_segment_indexes)], default=-1)
        prev = draft.segments[after] if after >= 0 else None
        if index in context_indexes and draft.segments:
            # 背景交代段没有可视化来源，规则只允许并入相邻事件段：挂到前一段（没有就挂到第一段）
            host = prev if prev is not None else draft.segments[0]
            host.source_segment_indexes = sorted(set(host.source_segment_indexes) | {index})
            host.source_unit_ranges.append(_AiSourceUnitRange(source_segment_index=index, from_unit=from_unit, to_unit=to_unit))
            notes.append(f"背景段 {index} 没有任何段覆盖，已并入第 {host.segment_no} 段")
            continue
        new_plan = _AiSegmentPlan(
            segment_no=0, synopsis=f"原文段 {index}（模型未排入，按原文补齐）",
            source_segment_indexes=[index],
            beat_ids=[b.beat_id for b in draft.beat_sheet if index in b.segment_indexes],
            palette=prev.palette if prev is not None else "",
            source_unit_ranges=[_AiSourceUnitRange(source_segment_index=index, from_unit=from_unit, to_unit=to_unit)],
        )
        draft.segments.insert(after + 1, new_plan)
        notes.append(f"原文段 {index} 没有任何段覆盖，已在第 {after + 2} 位补一段（S{from_unit:02d}-S{to_unit:02d}）")
    if notes:
        old_to_new = {}
        for pos, plan in enumerate(draft.segments, start=1):
            if plan.segment_no:
                old_to_new[plan.segment_no] = pos
            plan.segment_no = pos
        for item in draft.kept_lines:
            item.segment_no = old_to_new.get(item.segment_no, item.segment_no)
    return notes
