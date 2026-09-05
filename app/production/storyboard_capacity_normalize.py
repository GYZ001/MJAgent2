"""确定性容量归一化：分镜台 2.1.2（见 app.production.storyboard_pack 的
STORYBOARD_PACK_VERSION changelog）。

背景：真实 EP1 两轮回归证明"这段台词合计超 54 字，该怎么重新分配/拆段"是
算术 + 结构重排问题，不是语义判断，模型做不好——2.1.1 的报错已经给了逐条
字数、挪动目标、拆段许可，三次语义重试仍未修出（甚至出现"循环指路"：两个
相邻段互相点名对方当挪动目标，而对方同样超容，ERR-20260901-b1c349）。
结论：容量维度不该再打回模型做语义重试。模型只管"决定台词去留"
（kept_lines/dropped_lines，这是语义判断，理应由它做）；代码负责"把已确认
保留的台词排布进合规的段数里"——纯机械操作，不发明任何内容，也不改变模型
已经做出的去留决定。

依赖边界：延迟从 app.production.storyboard_pack 导入 _AiSegmentPlan（构造
新段用），避免模块级循环导入——storyboard_pack.py 反过来在模块顶层 import
本模块的 normalize_and_assert_capacity；本模块自己的顶层只依赖 app.config、
同层的 app.production.storyboard_dialogue_ledger，以及叶子模块
app.production.storyboard_segment_ranges，不构成循环。

2.4.0（见 app.production.storyboard_pack 的 STORYBOARD_PACK_VERSION
changelog）：拆段时必须同步拆 ``_AiSegmentPlan.source_unit_ranges``——原段被
贪心装箱拆成多箱后，若原样把整段的单元范围复制给每个新段，会让好几个新段
声明同一批句单元，违反"每段各占一块、不回退、不重叠"的新契约（真实故障与
完整判据见 storyboard_segment_ranges 模块 docstring）。拆点取"被挪走的第一条
台词所在单元"——``_split_ranges_at_unit`` 只切开命中这个原文段号的那一条
范围，与拆分无关的其它原文段号范围整条留在前一箱；因此除首箱外，后续箱的
``source_segment_indexes`` 会被裁剪成"只剩它实际分到范围的那些原文段号"
（不再照抄原段的完整列表），避免产出"引用了某段号却没有对应范围"的悬空引用。
定位不到拆分点（``quote_unit_index`` 找不到——只应发生在 DialogueQuote 既无
有效 start_offset、text 又搜不到原文的极端情形）时按兜底处理：那个新段保持
默认空 source_unit_ranges、source_segment_indexes 保持继承原段完整列表（与
2.4.0 之前的行为一致），已知限制见本模块 docstring 末尾。
"""
from __future__ import annotations

from typing import Any

from app import config
from app.production.storyboard_dialogue_ledger import DialogueQuote, dialogue_ledger_errors
from app.production.storyboard_segment_ranges import (_AiSourceUnitRange, quote_unit_index,
                                                      reassign_kept_lines_to_covering_segments)
from app.source_excerpt import SourceSegment


class StoryboardCapacityNormalizationError(RuntimeError):
    """归一化后复核仍然超容——说明归一化算法本身有 bug，不是模型的错。

    正常路径下归一化后必然合规（贪心装箱保证每箱 ≤ max_chars_per_segment）；
    这条异常只应该在实现有缺陷时触发，触发时才允许把错误亮给人看。
    """


def _bin_pack_quote_ids(
    quote_ids: list[str], quotes_by_id: dict[str, DialogueQuote], *, max_chars: int,
) -> list[list[str]]:
    """按传入顺序（调用方已按原文出现顺序排好）贪心装箱，每箱 ≤max_chars。"""
    bins: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for quote_id in quote_ids:
        chars = quotes_by_id[quote_id].content_chars
        if current and current_chars + chars > max_chars:
            bins.append(current)
            current, current_chars = [], 0
        current.append(quote_id)
        current_chars += chars
    if current:
        bins.append(current)
    return bins


def _kept_ids_by_segment(draft: Any, quotes_by_id: dict[str, DialogueQuote]) -> dict[int, list[str]]:
    """按 segment_no 分组 kept quote_id，组内按原文出现顺序（quote_id 数字部分）排序。"""
    grouped: dict[int, list[str]] = {}
    for item in draft.kept_lines:
        if item.quote_id in quotes_by_id:
            grouped.setdefault(item.segment_no, []).append(item.quote_id)
    for ids in grouped.values():
        ids.sort(key=lambda qid: int(qid[1:]))
    return grouped


def _split_ranges_at_unit(
    ranges: list[_AiSourceUnitRange], split_source_index: int, split_unit: int,
) -> tuple[list[_AiSourceUnitRange], list[_AiSourceUnitRange]]:
    """按 (split_source_index, split_unit) 把范围列表切成"留在前一箱"/"划入
    后一箱"两组：只切开命中 ``split_source_index`` 的那一条范围本身
    （``[from, split_unit)`` 留前、``[split_unit, to]`` 划后）；其余原文段号
    的范围与这次拆分无关，整条留在前一箱——因此除首箱外，后续箱只会继承它
    实际分到范围的那个原文段号，不会带着与它无关的旧引用。
    """
    before: list[_AiSourceUnitRange] = []
    after: list[_AiSourceUnitRange] = []
    for r in ranges:
        if r.source_segment_index != split_source_index:
            before.append(r)
        elif split_unit <= r.from_unit:
            after.append(r)
        elif split_unit > r.to_unit:
            before.append(r)
        else:
            before.append(_AiSourceUnitRange(
                source_segment_index=r.source_segment_index, from_unit=r.from_unit, to_unit=split_unit - 1,
            ))
            after.append(_AiSourceUnitRange(
                source_segment_index=r.source_segment_index, from_unit=split_unit, to_unit=r.to_unit,
            ))
    return before, after


def _split_point_for_bin(
    quote_id: str, quotes_by_id: dict[str, DialogueQuote], source_segments: list[SourceSegment],
) -> tuple[int, int] | None:
    """这个 bin 第一条台词的 (原文段号, 句单元号) 拆分点；定位不到返回 None
    （见 quote_unit_index 文档——只应发生在 DialogueQuote 既无有效
    start_offset、text 又搜不到原文的极端情形）。"""
    quote = quotes_by_id[quote_id]
    index = quote.source_segment_index
    if not (1 <= index <= len(source_segments)):
        return None
    unit = quote_unit_index(quote, source_segments[index - 1].text)
    return (index, unit) if unit >= 1 else None


def _split_one_segment(
    segment: Any,
    quote_ids: list[str],
    quotes_by_id: dict[str, DialogueQuote],
    *,
    max_chars: int,
    segment_plan_cls: type,
    new_segments: list[Any],
    quote_id_to_index: dict[str, int],
    source_segments: list[SourceSegment],
) -> dict[str, Any] | None:
    """处理一个原段：不超容就原样收进 new_segments；超容则贪心装箱拆成多段。

    原地追加进调用方共享的累积容器 new_segments / quote_id_to_index；返回
    这段的拆分遥测（未拆分返回 None）。段号此时仍是旧值，靠调用方事后按
    new_segments 的最终顺序统一重排（见 normalize_beat_sheet_capacity）。
    2.4.0：同步拆 source_unit_ranges，拆点=每个新箱第一条台词所在单元。
    """
    new_segments.append(segment)
    first_index = len(new_segments) - 1
    total = sum(quotes_by_id[qid].content_chars for qid in quote_ids)
    if total <= max_chars:
        for qid in quote_ids:
            quote_id_to_index[qid] = first_index
        return None
    bins = _bin_pack_quote_ids(quote_ids, quotes_by_id, max_chars=max_chars)
    for qid in bins[0]:
        quote_id_to_index[qid] = first_index
    indices = [first_index]
    remaining_ranges = list(segment.source_unit_ranges)
    for extra_bin in bins[1:]:
        split_point = _split_point_for_bin(extra_bin[0], quotes_by_id, source_segments)
        if split_point is not None:
            finished, remaining_ranges = _split_ranges_at_unit(remaining_ranges, *split_point)
            new_segments[indices[-1]].source_unit_ranges = finished
        spawned = segment_plan_cls(
            segment_no=0,
            synopsis=f"{segment.synopsis}（容量拆分·承接前段台词）",
            source_segment_indexes=list(segment.source_segment_indexes),
            beat_ids=list(segment.beat_ids),
            # 拆出的新段仍是同一场戏：色温方向必须原样继承，否则阶段二会把空 palette
            # 当成「换了色温」要求写渐变（EP1 重跑实测：段 5/段 10 为空，灯光又闪了两次）。
            palette=segment.palette,
        )
        new_segments.append(spawned)
        indices.append(len(new_segments) - 1)
        for qid in extra_bin:
            quote_id_to_index[qid] = indices[-1]
    new_segments[indices[-1]].source_unit_ranges = remaining_ranges
    for idx in indices:
        covered = sorted({r.source_segment_index for r in new_segments[idx].source_unit_ranges})
        if covered:
            new_segments[idx].source_segment_indexes = covered
    return {"original_segment_no": segment.segment_no, "bin_count": len(bins), "_indices": indices}


def normalize_beat_sheet_capacity(
    draft: Any, quotes: list[DialogueQuote], *, source_segments: list[SourceSegment],
) -> list[dict[str, Any]]:
    """对每个 kept 台词合计超容的段，贪心装箱拆成多段；返回拆分遥测。

    首箱留在原段（synopsis 不变）；其余箱各生成一个新段，继承原段的
    beat_ids，synopsis 追加「（容量拆分·承接前段台词）」可见标记——不发明
    任何内容，只是把模型已确认保留的台词重新排布。``source_segment_indexes``
    2.4.0 起不再无条件整段照抄原段——``_split_one_segment`` 会按拆出的
    ``source_unit_ranges`` 反推每个新段实际覆盖了哪些原文段号（见该函数
    docstring）。随后全量重排 segments[].segment_no 连续 1..N，同步改写
    kept_lines 的 segment_no。原地修改 draft，同时返回遥测供落库审计
    （dialogue_ledger_summary.capacity_normalization）。``source_segments`` 是
    真实原文（算句单元用），2.4.0 新增必传参数——不留默认值，漏传在调用那
    一刻就是 TypeError，而不是悄悄退化成不拆分单元范围。
    """
    from app.production.storyboard_beat_sheet import _AiSegmentPlan

    quotes_by_id = {q.quote_id: q for q in quotes}
    max_chars = config.MAX_SPOKEN_CHARS_PER_SHOT
    kept_ids_by_segment = _kept_ids_by_segment(draft, quotes_by_id)

    new_segments: list[Any] = []
    quote_id_to_index: dict[str, int] = {}
    telemetry: list[dict[str, Any]] = []
    for segment in draft.segments:
        quote_ids = kept_ids_by_segment.get(segment.segment_no, [])
        record = _split_one_segment(
            segment, quote_ids, quotes_by_id, max_chars=max_chars, segment_plan_cls=_AiSegmentPlan,
            new_segments=new_segments, quote_id_to_index=quote_id_to_index,
            source_segments=source_segments,
        )
        if record is not None:
            telemetry.append(record)

    for index, segment in enumerate(new_segments, start=1):
        segment.segment_no = index
    for item in draft.kept_lines:
        if item.quote_id in quote_id_to_index:
            item.segment_no = new_segments[quote_id_to_index[item.quote_id]].segment_no
    draft.segments = new_segments
    for record in telemetry:
        record["new_segment_nos"] = [new_segments[i].segment_no for i in record.pop("_indices")]
    return telemetry


def normalize_and_assert_capacity(
    draft: Any, quotes: list[DialogueQuote], *,
    source_segments: list[SourceSegment], paratext_indexes: set[int],
) -> list[dict[str, Any]]:
    """归一化 + 复核断言：归一化后仍超容说明算法本身有 bug，fail-closed 抛出。

    这才是允许把容量错误亮给人看的唯一场景——模型职责已经在
    normalize_beat_sheet_capacity 这一步被代码接管，不该再让人以为是模型
    没修好（复用 dialogue_ledger_errors 同一份判据，不另起一套检查逻辑）。

    已知限制（2.4.0）：这里不复核 segment_unit_range_errors——生产路径下
    beat_draft 在进入这一步之前已经过 ``_validate_beat_sheet_draft`` 的单元
    范围校验，理论上只有归一化算法自身的 bug 才会引入新的范围违规，但要在这
    里独立复核需要为"未被拆分、因而未被本函数触碰"的段也补出自洽的单元范围
    前置条件，成本与当前收益不成比例，留作已知限制，未在这次改造里做。
    ``paratext_indexes`` 参数暂时只是为了让调用方签名保持一致（供将来补这项
    复核时使用），当前函数体不读它。
    """
    telemetry = normalize_beat_sheet_capacity(draft, quotes, source_segments=source_segments)
    _ = paratext_indexes
    # 拆段后台词的段号可能落到不再引用它原文段的段上（2026-09-05 第 23 集 Q31 → 第 28 段只覆盖 [4]）：
    # 复核前先按单元位置/原文段号确定性归位，归位不了的才是真正的算法 bug。
    reassign_kept_lines_to_covering_segments(draft.kept_lines, quotes, draft.segments, source_segments)
    segment_source_indexes = {s.segment_no: s.source_segment_indexes for s in draft.segments}
    errors = dialogue_ledger_errors(
        quotes=quotes,
        kept_lines=draft.kept_lines,
        dropped_lines=draft.dropped_lines,
        segment_source_indexes=segment_source_indexes,
        max_chars_per_segment=config.MAX_SPOKEN_CHARS_PER_SHOT,
    )
    if errors:
        raise StoryboardCapacityNormalizationError(
            "对白台账容量归一化后复核仍不合规（这是归一化算法自身的 bug，不是模型"
            f"没修好，需要工程排查）：{errors}"
        )
    return telemetry
