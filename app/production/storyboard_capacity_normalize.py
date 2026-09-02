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
本模块的 normalize_and_assert_capacity；本模块自己的顶层只依赖 app.config
与同层的 app.production.storyboard_dialogue_ledger，不构成循环。
"""
from __future__ import annotations

from typing import Any

from app import config
from app.production.storyboard_dialogue_ledger import DialogueQuote, dialogue_ledger_errors


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


def _split_one_segment(
    segment: Any,
    quote_ids: list[str],
    quotes_by_id: dict[str, DialogueQuote],
    *,
    max_chars: int,
    segment_plan_cls: type,
    new_segments: list[Any],
    quote_id_to_index: dict[str, int],
) -> dict[str, Any] | None:
    """处理一个原段：不超容就原样收进 new_segments；超容则贪心装箱拆成多段。

    原地追加进调用方共享的累积容器 new_segments / quote_id_to_index；返回
    这段的拆分遥测（未拆分返回 None）。段号此时仍是旧值，靠调用方事后按
    new_segments 的最终顺序统一重排（见 normalize_beat_sheet_capacity）。
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
    for extra_bin in bins[1:]:
        spawned = segment_plan_cls(
            segment_no=0,
            synopsis=f"{segment.synopsis}（容量拆分·承接前段台词）",
            source_segment_indexes=list(segment.source_segment_indexes),
            beat_ids=list(segment.beat_ids),
        )
        new_segments.append(spawned)
        indices.append(len(new_segments) - 1)
        for qid in extra_bin:
            quote_id_to_index[qid] = indices[-1]
    return {"original_segment_no": segment.segment_no, "bin_count": len(bins), "_indices": indices}


def normalize_beat_sheet_capacity(draft: Any, quotes: list[DialogueQuote]) -> list[dict[str, Any]]:
    """对每个 kept 台词合计超容的段，贪心装箱拆成多段；返回拆分遥测。

    首箱留在原段（synopsis 不变）；其余箱各生成一个新段，继承原段的
    source_segment_indexes/beat_ids，synopsis 追加「（容量拆分·承接前段
    台词）」可见标记——不发明任何内容，只是把模型已确认保留的台词重新排布。
    随后全量重排 segments[].segment_no 连续 1..N，同步改写 kept_lines 的
    segment_no。原地修改 draft，同时返回遥测供落库审计
    （dialogue_ledger_summary.capacity_normalization）。
    """
    from app.production.storyboard_pack import _AiSegmentPlan

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


def normalize_and_assert_capacity(draft: Any, quotes: list[DialogueQuote]) -> list[dict[str, Any]]:
    """归一化 + 复核断言：归一化后仍超容说明算法本身有 bug，fail-closed 抛出。

    这才是允许把容量错误亮给人看的唯一场景——模型职责已经在
    normalize_beat_sheet_capacity 这一步被代码接管，不该再让人以为是模型
    没修好（复用 dialogue_ledger_errors 同一份判据，不另起一套检查逻辑）。
    """
    telemetry = normalize_beat_sheet_capacity(draft, quotes)
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
