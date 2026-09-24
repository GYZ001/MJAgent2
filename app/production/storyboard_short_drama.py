"""短剧节奏档（``adaptation_mode="short_drama"``）的确定性核验与修补。

2026-09-23 用户拍板新增「改编强度档位」：``faithful``（忠实，现行行为）/
``short_drama``（短剧节奏，单集约 90 秒）。短剧档允许模型把非关键原文整块
删掉，但删了什么必须显式声明、留档、可追溯——没声明的原文照旧零容忍必须
被拍到（本模块不改这条：所有豁免只对模型显式声明、且经本模块确定性核验
仍然有效的区间生效）。

**两阶段核验，理由见 CLAUDE.md「修补器与校验器死锁」教训**（repair 删掉的
正是 validator 要的会让整集失败）：

1. ``reconcile_dropped_units``（``_validate_beat_sheet_draft`` 早期调用，
   ``storyboard_beat_sheet_repair`` 的既有修补跑之前）：把模型声明的
   ``dropped_source_spans`` 展开成 (source_segment_index, unit_no) 集合，减去
   四类保护——已被某段 ``source_unit_ranges`` 覆盖的单元、已进 kept_lines 的
   单元、作者点名必拍的单元（``screenplay_markers.required_beats``）、
   paratext/背景交代段单元——全部用**模型原始声明时的快照**判断，不等后续
   修补跑完。冲突一律确定性裁掉删减（偏向保留），不打回模型。命中的台词立即
   强制并入 ``dropped_lines``，reason 可追溯到区间理由（``_SPAN_DROP_REASON_
   PREFIX``）。
2. ``finalize_dropped_units``（``repair_beat_sheet_draft`` 跑完之后）：
   ``storyboard_beat_sheet_repair.fix_order_and_fill_holes`` 对「整个缺口都在
   删减区间内」的洞会保持不填、对「缺口只有贴边部分不在删减区间内」只延伸
   相邻段覆盖贴边部分（见该模块 ``_gap_fill_plan``），删减单元继续留空；只有
   「缺口中间夹着两侧都够不到的非删减单元」这种没法用一段连续范围表达的
   情形，才会整体回填、让这部分删减声明失效。这一步按修补后的 ``source_
   unit_ranges`` 重新交集一遍，只留下「修补后依然没人覆盖」的部分作为最终
   有效删减，写回 ``draft.dropped_source_spans``；修补后被回填、不再有效的
   单元，如果对应台词是阶段一被 ``_force_drop_quotes`` 强制丢弃的（reason
   带 ``_SPAN_DROP_REASON_PREFIX`` 前缀），一律放回 ``kept_lines``——这段原文
   既然最终会被拍到，台词就不该继续被强制噤声，是「偏向保留」这条主线原则
   在阶段二的延伸（``_rescue_quotes``）。最后跑 key 节拍覆盖检查（必须放在
   修补跑完、段落归属稳定之后，否则会报出后续会被修补掉的假阳性）。
"""
from __future__ import annotations

import logging
import math
from typing import Any

from app.config import MAX_SPOKEN_CHARS_PER_SHOT, SHORT_DRAMA_TARGET_DURATION_S
from app.production.storyboard_beat_sheet_schemas import SEGMENT_DURATION_S
from app.production.storyboard_capacity_normalize import normalize_beat_sheet_capacity
from app.production.storyboard_dialogue_ledger import _AiDroppedLine, _AiKeptLine
from app.production.storyboard_short_drama_schemas import _AiDroppedSourceSpan
from app.production.screenplay_markers import required_beat_spans
from app.production.storyboard_segment_ranges import quote_unit_index, split_source_units

_LOGGER = logging.getLogger(__name__)

#: 6 = ceil(90 / 15)：目标时长换算成目标段数，真源是 app.config.SHORT_DRAMA_
#: TARGET_DURATION_S（单元 0 已落地），不在本模块重复定义。
TARGET_SEGMENT_COUNT = -(-SHORT_DRAMA_TARGET_DURATION_S // SEGMENT_DURATION_S)

#: 8 = ceil(6 * 1.3)：段数软上限。模型对「精确 N 段」历来不可靠（本仓 4 次
#: 语义重试耗尽整集失败的事故都与段数/容量类字面量约束有关），留 30% 浮动。
MAX_SEGMENT_COUNT = math.ceil(TARGET_SEGMENT_COUNT * 1.3)

#: 432 = 8 * 54：台词预算，口径与段数软上限同一套——全集 kept_lines 纯文字
#: 字数上限，按"每段 15 秒最多说 54 字"（config.MAX_SPOKEN_CHARS_PER_SHOT，
#: 与 storyboard_capacity_normalize 的容量归一化同一常量）乘段数软上限换算。
#: 2026-09-24 真实三集验证：模型规划段数达标但几乎不删台词，容量归一化按
#: 15 秒口播容量机械拆段把 6-8 段撑回 11-16 段——段数软上限只管"模型自己声明
#: 几段"，管不住"保留的台词多到必须拆出这么多段"，需要在台词量这一维度上
#: 单独给模型一个可执行的预算，而不是等它间接撞上拆分后的段数。
DIALOGUE_BUDGET_CHARS = MAX_SEGMENT_COUNT * MAX_SPOKEN_CHARS_PER_SHOT

#: 有效删减强制丢弃的台词，reason 统一带这个前缀，供事后追溯"这句台词是被
#: 哪个机制丢的"，也供 generate_storyboard_pack 从 dropped_lines 里筛出
#: dropped_line_quote_ids（判据是字符串前缀，不新起一个字段）。
_SPAN_DROP_REASON_PREFIX = "随原文区间删减："

#: beat_sheet_dialogue_ledger_rules()（storyboard_dialogue_ledger.py，已在
#: line_count 棘轮基线上，不许碰）里"容量装不下就拆段，段数不设上限"这句说的
#: 是单个原文段的台词容量拆分，与短剧档"全集总段数软上限"是不同维度，但字面
#: 上的"不设上限"会让模型误以为软上限不存在——短剧档下替换成不冲突的措辞，
#: 忠实档原样不动（见 adjust_faithful_rules_for_short_drama）。
_UNCAPPED_SEGMENT_PHRASE = "装不下就把这一段拆成更多段落，段数不设上限，不要为了凑少数段而丢弃或压缩台词"
_CAPPED_SEGMENT_PHRASE = (
    "装不下就把这一段拆成更多段落——这是单个原文段自己的台词容量拆分，不受"
    "上面全集总段数软上限约束，不要为了凑少数段而丢弃或压缩台词"
)


def adjust_faithful_rules_for_short_drama(rules: list[str]) -> list[str]:
    """把继承自忠实档的规则文案里"段数不设上限"那句改写成不与短剧档软上限
    冲突的措辞；不含该短语的规则原样返回（``str.replace`` 无命中即无操作）。"""
    return [r.replace(_UNCAPPED_SEGMENT_PHRASE, _CAPPED_SEGMENT_PHRASE) for r in rules]


def short_drama_beat_sheet_rules() -> list[str]:
    """短剧节奏档阶段一 rules[] 新增的正面陈述（CLAUDE.md Prompts：写清楚
    取值从哪来、必须怎么做，不写"不许怎样"的禁令）。与忠实档共用的既有规则
    （单元范围声明、色温弧线、对白台账等）原样保留，这些是追加规则。
    """
    return [
        f"这一集是短剧节奏改编：目标总时长约 {SHORT_DRAMA_TARGET_DURATION_S} 秒、"
        f"约 {TARGET_SEGMENT_COUNT} 段（每段固定 15 秒），全集总段数不得超过 "
        f"{MAX_SEGMENT_COUNT} 段。",
        "beat_sheet 里的每个节拍都必须显式标注 importance：key 表示这个节拍"
        "推动主线情节、塑造人物关系、交代关键设定，或是本章末尾的悬念/钩子"
        "——必须有至少一个段的 beat_ids 引用它；optional 表示可以整段压缩或"
        "删掉的闲笔、重复描写、或与主线无关的过场，不标注这个字段无法通过"
        "校验。",
        "non-key（optional）的原文内容如果你判断整块可以不拍，把它对应的"
        "原文区间写进 dropped_source_spans（每条给 source_segment_index、"
        "from_unit、to_unit、reason），reason 写清具体为什么可以删（例如"
        "「与主线无关的环境描写，删除不影响理解」）；被删区间内的原文台词"
        "随区间一起删除，不需要在 dropped_lines 里为它们单独写理由。区间"
        "以外的原文——包括所有 key 节拍覆盖的原文——仍按下面的对白台账"
        "规则逐句决定去留，整句台词默认保留。",
        "dialogue_density_by_source_segment 里的 min_segments 是按该原文段"
        "全部台词字数估算的下限；如果你计划把这个原文段的一部分整块写进 "
        "dropped_source_spans，被删部分的台词字数不需要计入这个原文段实际"
        "所需的段数。",
        "dropped_lines 里按理由弃置的每一条台词（区间外的个别弃置，不是"
        "dropped_source_spans 那种整段区间删除）都必须给出 beat_id，指向 "
        "beat_sheet 里一个真实存在、importance=optional、且 segment_indexes "
        "覆盖这句台词原文段号的节拍——三条任一不满足会被机械放回 kept_lines，"
        "不打回重试；key 节拍推动主线情节、人物关系、关键设定或悬念钩子，它"
        "覆盖的台词一律保留，不能靠 dropped_lines 绕过。",
        "上面「同一原文段号被多个段引用时必须合并覆盖全部句单元、不能留洞」"
        "这条规则，对你在 dropped_source_spans 里整块声明删除的原文区间不"
        "适用：这部分单元本就不需要出现在任何段的 source_unit_ranges 里，"
        "这不算留洞，是你主动做出的删减决定。",
    ]


def _required_beat_units(source_segments: list[Any], indexes: set[int]) -> set[tuple[int, int]]:
    """作者点名必拍的括号镜（格局镜/钩子/定场镜/收尾镜）所在句单元：这些单元
    永远不允许被判定为有效删减，不管是不是短剧档。判据是**位置重叠**——按
    ``screenplay_markers.required_beat_spans`` 在整段原文上找匹配区间，再看
    哪些句单元的 [start,end) 与某个匹配区间有重叠，而不是把每个单元的子串
    单独喂给正则重新匹配一遍：标记内容如果含句末标点，``split_source_units``
    会把它切成两个单元，切开后每个子串都定位不到完整的「（标签：…）」闭合
    括号，逐单元重新匹配会漏判其中被切开的那一半（真实回归见
    ``tests/test_storyboard_short_drama.py`` 的跨单元必拍保护用例）。
    """
    protected: set[tuple[int, int]] = set()
    for index in indexes:
        if not (1 <= index <= len(source_segments)):
            continue
        text = source_segments[index - 1].text
        marker_spans = required_beat_spans(text)
        if not marker_spans:
            continue
        for unit_no, (u_start, u_end) in enumerate(split_source_units(text), start=1):
            if any(u_start < m_end and m_start < u_end for m_start, m_end in marker_spans):
                protected.add((index, unit_no))
    return protected


def required_beat_protected_units(source_segments: list[Any]) -> frozenset[tuple[int, int]]:
    """全集范围的作者点名必拍单元集合（公开入口，供
    ``storyboard_beat_sheet``/``storyboard_beat_sheet_repair`` 判断"这句台词
    能不能在短剧档被按理由弃置"用）；与 ``reconcile_dropped_units`` 内部只扫
    "有声明删减区间的原文段"不同，这里要扫**全部**原文段——个别台词弃置不
    像整块区间声明那样有天然的候选范围收窄。"""
    return frozenset(_required_beat_units(source_segments, set(range(1, len(source_segments) + 1))))


def _declared_units(spans: list[Any], source_segments: list[Any]) -> dict[tuple[int, int], str]:
    """模型声明的删减区间展开成 {(source_segment_index, unit_no): reason}；
    与该原文段的合法单元范围 [1, total] 取**交集**，不夹紧（clamp）越界部分。
    夹紧会把"模型声明的区间整个落在合法范围之外"误判成"声明了合法范围里的
    最后一个单元"——那是一个模型从未声明过的单元，被凭空计入删减声明，等于
    删掉了模型没说要删的内容。交集为空（例如 from_unit 本身就超过 total）
    就是这条声明对这个原文段完全不生效，不产出任何单元。"""
    result: dict[tuple[int, int], str] = {}
    for span in spans:
        index = span.source_segment_index
        if not (1 <= index <= len(source_segments)):
            continue
        total = len(split_source_units(source_segments[index - 1].text))
        if total < 1:
            continue
        start = max(span.from_unit, 1)
        end = min(max(span.to_unit, span.from_unit), total)
        if start > end:
            continue
        for unit_no in range(start, end + 1):
            result.setdefault((index, unit_no), span.reason)
    return result


def _kept_unit_set(kept_lines: list[Any], quotes_by_id: dict[str, Any], source_segments: list[Any]) -> set[tuple[int, int]]:
    result: set[tuple[int, int]] = set()
    for item in kept_lines:
        quote = quotes_by_id.get(item.quote_id)
        if quote is None or not (1 <= quote.source_segment_index <= len(source_segments)):
            continue
        unit_no = quote_unit_index(quote, source_segments[quote.source_segment_index - 1].text)
        if unit_no >= 1:
            result.add((quote.source_segment_index, unit_no))
    return result


def _covered_unit_set(segments: list[Any]) -> set[tuple[int, int]]:
    result: set[tuple[int, int]] = set()
    for plan in segments:
        for r in plan.source_unit_ranges:
            result.update((r.source_segment_index, u) for u in range(r.from_unit, r.to_unit + 1))
    return result


def _force_drop_quotes(draft: Any, quotes: list[Any], source_segments: list[Any], dropped: dict[tuple[int, int], str]) -> None:
    """有效删减区间内的台词强制并入 dropped_lines，reason 可追溯到区间理由；
    覆盖模型自己对这些 quote_id 的原有决定（命中的只会是模型没保留、或还没
    决定的台词——kept_units 已经在调用方从 dropped 里排除掉）。"""
    if not dropped:
        return
    hit: dict[str, str] = {}
    for quote in quotes:
        index = quote.source_segment_index
        if not (1 <= index <= len(source_segments)):
            continue
        unit_no = quote_unit_index(quote, source_segments[index - 1].text)
        reason = dropped.get((index, unit_no))
        if reason is not None:
            hit[quote.quote_id] = reason
    if not hit:
        return
    draft.kept_lines = [item for item in draft.kept_lines if item.quote_id not in hit]
    draft.dropped_lines = [item for item in draft.dropped_lines if item.quote_id not in hit]
    draft.dropped_lines.extend(
        _AiDroppedLine(quote_id=qid, reason=f"{_SPAN_DROP_REASON_PREFIX}{reason}") for qid, reason in hit.items()
    )


def reconcile_dropped_units(
    draft: Any, source_segments: list[Any], dialogue_quotes: list[Any],
    paratext_indexes: set[int], context_indexes: set[int], *, adaptation_mode: str,
) -> frozenset[tuple[int, int]]:
    """短剧档第一阶段核验，见模块 docstring。忠实档、或草稿没有
    ``dropped_source_spans`` 字段（不是短剧档草稿）时直接返回空集——分支收在
    这一个函数里，调用方不需要各自判断模式。
    """
    spans = getattr(draft, "dropped_source_spans", None)
    if adaptation_mode != "short_drama" or not spans:
        return frozenset()
    declared = _declared_units(spans, source_segments)
    protected = _required_beat_units(source_segments, {index for index, _ in declared})
    quotes_by_id = {q.quote_id: q for q in dialogue_quotes}
    kept_units = _kept_unit_set(draft.kept_lines, quotes_by_id, source_segments)
    covered_units = _covered_unit_set(draft.segments)
    dropped = {
        key: reason for key, reason in declared.items()
        if key not in protected and key not in kept_units and key not in covered_units
        and key[0] not in paratext_indexes and key[0] not in context_indexes
    }
    _force_drop_quotes(draft, dialogue_quotes, source_segments, dropped)
    return frozenset(dropped)


def _canonical_spans(units: frozenset[tuple[int, int]], reasons: dict[tuple[int, int], str]) -> list[_AiDroppedSourceSpan]:
    """(source_segment_index, unit_no) 集合合并回连续区间——落库/展示用，不
    逐单元列一行。同一原文段号内连续单元号合并成一条，reason 取区间首个单元
    的声明理由（同一条模型声明展开出的各单元理由本就相同）。"""
    by_index: dict[int, list[int]] = {}
    for index, unit_no in units:
        by_index.setdefault(index, []).append(unit_no)
    spans: list[_AiDroppedSourceSpan] = []
    for index in sorted(by_index):
        unit_nos = sorted(by_index[index])
        start = prev = unit_nos[0]
        for unit_no in unit_nos[1:]:
            if unit_no == prev + 1:
                prev = unit_no
                continue
            spans.append(_AiDroppedSourceSpan(
                source_segment_index=index, from_unit=start, to_unit=prev,
                reason=reasons.get((index, start)) or "原始理由缺失（重算删减区间时未匹配到声明）",
            ))
            start = prev = unit_no
        spans.append(_AiDroppedSourceSpan(
            source_segment_index=index, from_unit=start, to_unit=prev,
            reason=reasons.get((index, start)) or "原始理由缺失（重算删减区间时未匹配到声明）",
        ))
    return spans


def key_beat_coverage_errors(draft: Any, *, adaptation_mode: str) -> list[str]:
    """key 节拍必须被至少一个段的 beat_ids 引用，否则作为业务错误打回模型。"""
    if adaptation_mode != "short_drama":
        return []
    referenced = {bid for seg in draft.segments for bid in seg.beat_ids}
    missing = [
        beat.beat_id for beat in draft.beat_sheet
        if getattr(beat, "importance", "key") == "key" and beat.beat_id not in referenced
    ]
    if not missing:
        return []
    return [
        f"key 节拍 {missing} 没有被任何段的 beat_ids 引用：key 节拍推动主线/"
        "人物关系/关键设定/章末钩子，必须至少有一个段承载它，请把它分配给"
        "覆盖对应原文范围的段，或调整该段的 beat_ids"
    ]


def _quote_unit_key(quotes_by_id: dict[str, Any], source_segments: list[Any], quote_id: str) -> tuple[int, int] | None:
    quote = quotes_by_id.get(quote_id)
    if quote is None or not (1 <= quote.source_segment_index <= len(source_segments)):
        return None
    unit_no = quote_unit_index(quote, source_segments[quote.source_segment_index - 1].text)
    return (quote.source_segment_index, unit_no) if unit_no >= 1 else None


def _covering_segment_no(segments: list[Any], index: int, unit_no: int) -> int | None:
    for plan in segments:
        for r in plan.source_unit_ranges:
            if r.source_segment_index == index and r.from_unit <= unit_no <= r.to_unit:
                return plan.segment_no
    return None


def _rescue_quotes(
    draft: Any, source_segments: list[Any], dialogue_quotes: list[Any], rescued_units: frozenset[tuple[int, int]],
) -> None:
    """修补后被回填、不再是有效删减的单元：把阶段一被 ``_force_drop_quotes``
    强制丢弃的台词（reason 带 ``_SPAN_DROP_REASON_PREFIX``）放回 kept_lines
    ——这段原文既然最终会被拍到，台词就不该继续被强制噤声。segment_no 先取
    覆盖该单元的任意一段，后面紧接着的 reassign_kept_lines_to_covering_
    segments 会再按容量/归属校正，这里不必精确。"""
    if not rescued_units:
        return
    quotes_by_id = {q.quote_id: q for q in dialogue_quotes}
    remaining: list[Any] = []
    for item in draft.dropped_lines:
        unit_key = _quote_unit_key(quotes_by_id, source_segments, item.quote_id)
        segment_no = (
            _covering_segment_no(draft.segments, *unit_key)
            if str(item.reason).startswith(_SPAN_DROP_REASON_PREFIX) and unit_key in rescued_units
            else None
        )
        if segment_no is None:
            remaining.append(item)
            continue
        draft.kept_lines.append(_AiKeptLine(quote_id=item.quote_id, segment_no=segment_no))
        _LOGGER.info(
            "[STORYBOARD_SHORT_DRAMA] %s 所在单元修补后已被覆盖，放回 kept_lines（第 %s 段）",
            item.quote_id, segment_no,
        )
    draft.dropped_lines = remaining


def finalize_dropped_units(
    draft: Any, dropped_units: frozenset[tuple[int, int]], source_segments: list[Any], dialogue_quotes: list[Any],
) -> tuple[frozenset[tuple[int, int]], list[str]]:
    """``repair_beat_sheet_draft`` 跑完之后重新核对：修补对"整个缺口都在删减
    区间内"的洞才会保持不填，对"缺口只有贴边部分不在删减区间内"只延伸相邻段
    覆盖贴边部分（``fix_order_and_fill_holes``/``_gap_fill_plan``），删减单元
    继续留空；只有中间夹着两侧都够不到的非删减单元这种情形才整体回填。这一步
    按修补后的 source_unit_ranges 重新交集一遍，只留"修补后依然没人覆盖"的
    部分作为最终有效删减，写回 draft.dropped_source_spans 作为落库记录；
    修补后被回填、不再有效的单元，其被强制丢弃的台词放回 kept_lines
    （``_rescue_quotes``）。最后跑 key 节拍覆盖检查（必须放在这里：段落归属
    要在修补跑完之后才稳定，提前跑会把后续会被修补掉的情况误判成错误）。
    """
    if not dropped_units:
        return frozenset(), []
    covered = _covered_unit_set(draft.segments)
    final_units = frozenset(key for key in dropped_units if key not in covered)
    _rescue_quotes(draft, source_segments, dialogue_quotes, dropped_units - final_units)
    reasons = _declared_units(getattr(draft, "dropped_source_spans", None) or [], source_segments)
    draft.dropped_source_spans = _canonical_spans(final_units, reasons)
    return final_units, key_beat_coverage_errors(draft, adaptation_mode="short_drama")


def dropped_line_quote_ids(draft: Any) -> list[str]:
    """从 dropped_lines 里筛出被有效删减区间机制强制丢弃的 quote_id（判据是
    reason 前缀，见 _SPAN_DROP_REASON_PREFIX）——供 generate_storyboard_pack
    组装留档用，不新起一个状态字段。"""
    return [
        item.quote_id for item in getattr(draft, "dropped_lines", [])
        if str(item.reason).startswith(_SPAN_DROP_REASON_PREFIX)
    ]


def adaptation_summary(
    *, adaptation_mode: str, planned_segment_count: int, segment_count: int,
    dropped_spans: list[_AiDroppedSourceSpan], dropped_quote_ids: list[str],
    kept_dialogue_chars: int, projected_segment_count: int | None,
) -> dict[str, Any]:
    """``generate_storyboard_pack`` 用它拼出 ``StoryboardPack.adaptation``
    （章节偏移/原文摘录留给持久化阶段的 ``storyboard_pack_evidence``——那里
    才有 conn/章节内容，生成阶段没有）。

    2026-09-24 真实三集验证：``over_target`` 曾按模型规划的 ``planned_
    segment_count``（容量归一化拆段**之前**）判定，归一化把 6-8 段撑回
    11-16 段后这个字段结构上恒为 false——界面"超出短剧上限"的提示永远不
    出现，是一句假的界面承诺。改按**最终段数**（``segment_count``，容量
    归一化之后、真正落库的段数，调用方 ``generate_storyboard_pack`` 传入的
    ``len(pack_segments)`` 本就已经是这个值）判定；旧语义（"模型规划在最后
    一次语义重试仍超上限"）不丢，改名 ``planned_over_cap`` 继续留档，供事后
    区分"模型没规划够"与"规划够了但台词太多被归一化拆段撑大"两类根因。

    ``projected_segment_count``（2026-09-24 新增）：调用方传入 ``_generate_
    beat_sheet`` 返回的 ``SegmentCountSoftCap.last_projected_count``——最后
    一次校验时、按生产同源容量归一化预测出的段数（见该类文档）。与
    ``segment_count``（真正落库的最终段数）通常接近但不保证相等：两者之间
    还有 ``_strip_paratext_from_beat_draft`` 这一道不经过 ``validate()`` 循环
    的防御性兜底，会在真正归一化之前再改一次台词分布。忠实档恒 ``None``。
    """
    is_short_drama = adaptation_mode == "short_drama"
    return {
        "adaptation_mode": adaptation_mode,
        "target_duration_s": SHORT_DRAMA_TARGET_DURATION_S if is_short_drama else None,
        "target_segment_count": TARGET_SEGMENT_COUNT if is_short_drama else None,
        "max_segment_count": MAX_SEGMENT_COUNT if is_short_drama else None,
        "max_duration_s": MAX_SEGMENT_COUNT * SEGMENT_DURATION_S if is_short_drama else None,
        "planned_segment_count": planned_segment_count,
        "projected_segment_count": projected_segment_count if is_short_drama else None,
        "segment_count": segment_count,
        "final_duration_s": segment_count * SEGMENT_DURATION_S,
        "over_target": bool(is_short_drama and segment_count > MAX_SEGMENT_COUNT),
        "planned_over_cap": bool(is_short_drama and planned_segment_count > MAX_SEGMENT_COUNT),
        "kept_dialogue_chars": kept_dialogue_chars,
        "dialogue_budget_chars": DIALOGUE_BUDGET_CHARS if is_short_drama else None,
        "dropped_source_spans": [span.model_dump(mode="json") for span in dropped_spans],
        "dropped_line_quote_ids": list(dropped_quote_ids),
    }


def _projected_segment_count(draft: Any, quotes: list[Any], source_segments: list[Any]) -> int:
    """深拷贝草稿后跑一遍与生产同源的确定性容量归一化（复用 ``storyboard_
    capacity_normalize.normalize_beat_sheet_capacity`` 这个真源函数，不另起
    一套口径），只取拆分后的段数——不落回传入的 draft，调用方在同一次
    ``validate()`` 里多次调用互不干扰，也不影响真正落库的归一化。

    背景（2026-09-24 真实三集验证）：模型自己声明的段数达标（如 6 段）不等于
    按 15 秒口播容量拆分后的真实段数也达标——台词在几段里分布不匀，单段一旦
    超过 ``MAX_SPOKEN_CHARS_PER_SHOT``（54 字）就会被拆成更多段，实测撑到
    11-13 段。``SegmentCountSoftCap`` 用这个函数的返回值判定，模型不能再靠
    "自己声明的段数达标"蒙混过关。
    """
    projected = draft.model_copy(deep=True)
    normalize_beat_sheet_capacity(projected, quotes, source_segments=source_segments)
    return len(projected.segments)


class SegmentCountSoftCap:
    """段数软上限：按**容量归一化后的预计段数**（``_projected_segment_
    count``，2026-09-24 起，不再是模型自己声明的 ``len(draft.segments)``）
    超过 ``MAX_SEGMENT_COUNT`` 时前几次调用当业务错误打回模型语义重试，最后
    一次（``chat_structured`` 语义重试预算耗尽前的最后一次 ``validate`` 调用）
    降级为警告而不是继续打回——本仓至少 4 次真实事故都是"语义重试耗尽整集
    失败"，段数这种模型历来不精确的维度不该是压垮整集的最后一根稻草。
    ``retry_limit`` 必须是调用方传给 ``chat_structured`` 的同一个
    ``semantic_retry_limit`` 值（不重复写字面量，见 ``storyboard_beat_sheet.
    _BEAT_SHEET_SEMANTIC_RETRY_LIMIT``）。忠实档（``adaptation_mode !=
    "short_drama"``）永远不产生任何错误、永远不计算预计段数。
    """

    def __init__(
        self, *, adaptation_mode: str, retry_limit: int, quotes: list[Any], source_segments: list[Any],
    ) -> None:
        self._active = adaptation_mode == "short_drama"
        self._retry_limit = retry_limit
        self._attempt = 0
        self._quotes = quotes
        self._source_segments = source_segments
        #: adaptation_summary 的 projected_segment_count 留档字段直接读这个
        #: 属性（最后一次 errors() 调用算出的值）；忠实档恒 None——_active=
        #: False 时 errors() 提前返回，从不写它。
        self.last_projected_count: int | None = None

    def errors(self, draft: Any) -> list[str]:
        if not self._active:
            return []
        projected = _projected_segment_count(draft, self._quotes, self._source_segments)
        self.last_projected_count = projected
        is_last_attempt = self._attempt >= self._retry_limit
        self._attempt += 1
        if projected <= MAX_SEGMENT_COUNT or is_last_attempt:
            return []
        cut_hint = (projected - MAX_SEGMENT_COUNT) * MAX_SPOKEN_CHARS_PER_SHOT
        return [
            f"节拍表分了 {len(draft.segments)} 段，按 15 秒口播容量拆分后预计需要 "
            f"{projected} 段（约 {projected * SEGMENT_DURATION_S} 秒），超过短剧节奏档"
            f"软上限 {MAX_SEGMENT_COUNT} 段（约 {MAX_SEGMENT_COUNT * SEGMENT_DURATION_S} "
            f"秒）：请从 importance=optional 节拍覆盖的原文里再弃置约 {cut_hint} 字台词"
            "（写进 dropped_lines 并标注 beat_id），或合并信息量不足以单独成段的相邻"
            "节拍——只减少你自己声明的段数不会生效，容量拆分后的段数才是这条软上限"
            "真正判定的数字"
        ]
