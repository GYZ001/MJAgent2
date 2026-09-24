"""短剧节奏档删减复核：条目收集与单元拆分——``storyboard_short_drama_review.py``
的送审输入从哪来。

背景（2026-09-24 B 机沙箱第五轮真实验证，我欲封天 EP3/EP9）：第一版删减复核
（见 ``storyboard_short_drama_review.py`` 模块 docstring「三步流程」）把整条
``dropped_source_spans`` 区间当成一条送审条目，关键内容全部保住，但代价是
时长不降反升——EP3 389 字的删减区间里只有两句是目标铺垫，其余是与主线无关
的环境描写，可整条区间只要有一句关键就整条判 must_keep，389 字全部保留，
第二遍因此没有任何候选可删（405→240 秒）。粒度太粗：复核的"一条"和最终
"保不保"的判断单位不一致。

本模块把送审粒度从「整条区间」下沉到「句单元」（``storyboard_segment_ranges.
split_source_units``，与 ``dropped_source_spans.from_unit/to_unit``、留档
``source_unit_ranges`` 同一套编号，不新起一套切分），使复核结果能只救回
真正关键的那一两句，其余单元仍是「允许删减的候选」——第二遍的确定性强制
（``storyboard_short_drama_review._clip_spans_to_allowed_units``）本就是按
``(source_segment_index, unit_no)`` 集合做交集，天然支持这种更细的粒度，
不需要改它的核心逻辑，只需要喂给它更细的候选集合。

放在独立叶子模块（不留在 ``storyboard_short_drama_review.py``）：那个文件
已在 500 行棘轮零余量（见其模块 docstring），且"从草稿里收集哪些内容要送审"
与"怎么调用模型、怎么执行第二遍强制"是两个可以独立理解的关注点——前者只
依赖草稿字段与原文切分，后者依赖 ``model_gateway``/``chat_structured``。
依赖方向单向：本模块 import ``storyboard_short_drama`` 取
``_SPAN_DROP_REASON_PREFIX`` 常量，反过来 ``storyboard_short_drama`` 不
import 本模块，不构成循环；``storyboard_short_drama_review.py`` 同时 import
本模块与 ``storyboard_short_drama``。

## 三条控制条目数量/粒度的规则

1. **极短单元不送审、直接按可删**：单元原文的「口播实际字数」（``app.
   spoken_contract.content_char_count``——去空白与 Unicode 标点，与
   ``DROPPABLE_MAX_CHARS`` 判断整句台词是否"语气词"同一口径，不另起一套
   算法）不超过 ``DROPPABLE_MAX_CHARS`` 时（含纯标点/空白，此时该函数恒
   返回 0），这个单元不构成送审条目——它在"是否交代关键信息"这件事上
   信息量太小，不值得占用一条送审名额，且第一版已证明"完全不送审"不等于
   "不能删"：``storyboard_short_drama_review._clip_spans_to_allowed_units``
   对这类单元单独放行（见该函数 docstring），不因为它没出现在复核候选
   清单里就被误裁掉。
2. **单条区间单元数 > 30 时，相邻单元最多合并 3 个成一条送审**：控制单条
   区间可能把条目数摊得过多（例如一整章的背景交代）。合并只按原文位置
   连续分组，不跳着合并；合并后的条目文本是这几个单元的原文拼接（无分隔
   符——``split_source_units`` 保证单元之间不重叠，拼接后即是这段原文的
   逐字子串），复核判 ``must_keep`` 时整组一起保留/删除，不再细分——这是
   为控制条目数接受的粒度上限，本模块只解决"整条区间"这一种最粗粒度的
   问题，不追求无限细分。
3. **区间外整句弃置台词仍逐句一条**：这部分的判据与第一版完全相同（未
   改动，直接沿用），不受上面两条规则影响——台词天然是逐句的，没有"整条
   区间"这种更粗粒度需要拆。

条目的 ``region_label``（仅区间来源的条目非空）是这条内容所属原始声明区间
的人话描述（原文段号 + 完整单元范围），随条目一起送给模型，帮它判断"这句
内容是不是从一段更长的、正被考虑整体删除的文字里摘出来的"——不是复核判据
本身（判据仍然只看这条内容自己的文字，见 ``storyboard_short_drama_review.
_REVIEW_TASK``），只是上下文，且不改变 ``item_id`` 取值域（``item_id`` 仍然
逐条唯一，``region_label`` 是展示字段，不参与 Literal 枚举）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.production.storyboard_beat_sheet_schemas import DROPPABLE_MAX_CHARS
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_segment_ranges import quote_unit_index, split_source_units
from app.production.storyboard_short_drama import _SPAN_DROP_REASON_PREFIX
from app.source_excerpt import SourceSegment
from app.spoken_contract import content_char_count

#: 单条区间的单元数超过这个数才触发合并送审（见模块 docstring 规则 2）。
_MERGE_THRESHOLD_UNITS = 30

#: 合并送审时每条最多覆盖的单元数——原文位置连续，不跳着合并。
_MERGE_GROUP_SIZE = 3


@dataclass(frozen=True)
class _DropReviewItem:
    """一条待复核的删减内容：来自删减区间的一个或若干相邻句单元
    （``kind="unit"``，2026-09-24 起不再是整条区间）、或区间外弃置的整句
    台词（``kind="line"``）。``item_id`` 既是复核 schema 的枚举取值域，也是
    第二遍 payload 里必保/候选清单的稳定键，两处必须逐字同源，不能各自
    派生。``from_unit``/``to_unit`` 对 ``kind="line"`` 恒相等（台词定位到的
    单一单元号）；对 ``kind="unit"`` 是这条条目覆盖的单元范围（常见情形
    ``from_unit == to_unit``，超长区间合并送审时可以是最多 3 个连续单元）。
    """

    item_id: str
    kind: str  # "unit" | "line"
    source_segment_index: int
    from_unit: int
    to_unit: int
    text: str
    reason: str
    quote_id: str = ""  # 仅 kind="line" 有效
    region_label: str = ""  # 仅 kind="unit" 有效，见模块 docstring


def _unit_range_text(index: int, from_unit: int, to_unit: int, source_segments: list[SourceSegment]) -> str:
    """按单元范围切出原文全文（闭区间、1 起）；越界/定位不到时返回空串，
    调用方据此跳过——防御性兜底，正常路径下范围来自已核验的声明或切分
    结果本身，必然合法。"""
    if not (1 <= index <= len(source_segments)):
        return ""
    text = source_segments[index - 1].text
    units = split_source_units(text)
    start_i, end_i = from_unit - 1, to_unit - 1
    if not (0 <= start_i <= end_i < len(units)):
        return ""
    return text[units[start_i][0]:units[end_i][1]]


def _is_trivial_unit_text(text: str) -> bool:
    """口播实际字数不超过 ``DROPPABLE_MAX_CHARS`` 即「极短」——与判断整句
    台词是否语气词同一口径（``quote.content_chars <= DROPPABLE_MAX_CHARS``），
    纯标点/空白单元的 ``content_char_count`` 恒为 0，天然落在这条判据内，
    不需要另外判断"纯标点"。"""
    return content_char_count(text) <= DROPPABLE_MAX_CHARS


def unit_is_trivial_at(index: int, unit_no: int, source_segments: list[SourceSegment]) -> bool:
    """供 ``storyboard_short_drama_review._clip_spans_to_allowed_units`` 判断
    单个声明单元是否「极短」——极短单元本就不送审（见模块 docstring 规则 1），
    第二遍确定性强制不能因为它没出现在复核候选清单里就把它当成"未经允许的
    删减"裁掉，否则第一版已经生效的合理删减会被一次不相关的必保项连累撤销。
    """
    return _is_trivial_unit_text(_unit_range_text(index, unit_no, unit_no, source_segments))


def _split_span_into_items(span: Any, source_segments: list[SourceSegment]) -> list[_DropReviewItem]:
    """一条声明删减区间拆成若干送审条目，见模块 docstring 规则 1、2。"""
    idx = span.source_segment_index
    if not (1 <= idx <= len(source_segments)):
        return []
    total_units = len(split_source_units(source_segments[idx - 1].text))
    from_unit, to_unit = span.from_unit, span.to_unit
    if not (1 <= from_unit <= to_unit <= total_units):
        return []
    group_size = _MERGE_GROUP_SIZE if (to_unit - from_unit + 1) > _MERGE_THRESHOLD_UNITS else 1
    region_label = f"原文段{idx}单元S{from_unit:02d}-S{to_unit:02d}整块删减区间的一部分"
    items: list[_DropReviewItem] = []
    g_start = from_unit
    while g_start <= to_unit:
        g_end = min(g_start + group_size - 1, to_unit)
        text = _unit_range_text(idx, g_start, g_end, source_segments)
        if text and not _is_trivial_unit_text(text):
            item_id = f"unit:{idx}:{g_start}" if g_start == g_end else f"unit:{idx}:{g_start}-{g_end}"
            items.append(_DropReviewItem(
                item_id=item_id, kind="unit", source_segment_index=idx, from_unit=g_start, to_unit=g_end,
                text=text, reason=span.reason, region_label=region_label,
            ))
        g_start = g_end + 1
    return items


def _line_items(draft: Any, quotes: list[DialogueQuote], source_segments: list[SourceSegment]) -> list[_DropReviewItem]:
    """区间外弃置的整句台词——判据与第一版逐字相同（未改动，见模块
    docstring 规则 3）：有说话人、正文超过 ``DROPPABLE_MAX_CHARS``、且不是
    随区间强制弃置（``_SPAN_DROP_REASON_PREFIX`` 前缀，已由所属区间送审，
    不重复）。"""
    items: list[_DropReviewItem] = []
    quotes_by_id = {q.quote_id: q for q in quotes}
    for line in draft.dropped_lines:
        if str(line.reason).startswith(_SPAN_DROP_REASON_PREFIX):
            continue
        quote = quotes_by_id.get(line.quote_id)
        if quote is None or not quote.speaker or quote.content_chars <= DROPPABLE_MAX_CHARS:
            continue
        idx = quote.source_segment_index
        unit_no = quote_unit_index(quote, source_segments[idx - 1].text) if 1 <= idx <= len(source_segments) else -1
        items.append(_DropReviewItem(
            item_id=f"line:{quote.quote_id}", kind="line", source_segment_index=idx, from_unit=unit_no,
            to_unit=unit_no, text=quote.text, reason=line.reason, quote_id=quote.quote_id,
        ))
    return items


def _collect_review_items(
    draft: Any, quotes: list[DialogueQuote], source_segments: list[SourceSegment],
) -> list[_DropReviewItem]:
    """第一遍通过校验后，汇总需要送审的删减条目：有效删减区间按单元拆分
    （``draft.dropped_source_spans``，已经过 ``finalize_dropped_units`` 核验）
    + 区间外弃置的整句台词，见模块 docstring 三条规则。"""
    items: list[_DropReviewItem] = []
    for span in getattr(draft, "dropped_source_spans", None) or []:
        items.extend(_split_span_into_items(span, source_segments))
    items.extend(_line_items(draft, quotes, source_segments))
    return items
