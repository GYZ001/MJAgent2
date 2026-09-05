"""句单元切分与跨段引用范围：分镜台 2.4.0（见 app.production.storyboard_pack
的 STORYBOARD_PACK_VERSION changelog）。

背景（2026-09-03「橘座在上」EP1 真实回归）：节拍表把原文场 1-3（761 字、无
空行的一个原文段）拆成 6 段，但 6 段的 ``source_segment_indexes`` 全是
``[4]``——每段都拿到**整段**原文，只靠一句节拍摘要区分彼此该拍哪一块。后果：
猫跳上桌在段 4、段 6 各拍一次，黄总抓猫在段 7/8/9 拍了三次，全集最后一句
内心独白在段 4 就说了、段 6 又说一遍。旧校验只查段号越界，不查"各段是否按
顺序各占一块"——因为节拍表压根没有"块"这个概念可查。

本模块把"块"变成一个可校验的一等字段：把每个原文段确定性切成"句单元"
（不依赖模型、不需要额外调用），节拍表阶段必须为它引用的每个原文段声明一段
连续、不回退、不留洞的单元范围（``source_unit_ranges``），阶段二只喂模型
"本段范围内的单元 + 前后各两个单元的衔接上下文"，而不是整段原文——同一原文
段被 6 段引用时，模型物理上看不到别的段负责的那几句，也就不可能把同一句话
拍两遍。

设计上是叶子模块：只依赖标准库，不 import ``app.production.storyboard_pack``
或 ``storyboard_beat_sheet``（那两个模块反过来 import 本模块），避免循环
导入——``_PARATEXT_PLACEHOLDER_TEXT`` 因此也从 ``storyboard_pack.py`` 搬到
这里作为唯一定义，两边都从这个真源导入，不重复定义、不借道转手。
"""
from __future__ import annotations

import re

from typing import Any

from pydantic import BaseModel, Field

from app.source_excerpt import SourceSegment

#: 占位说明不含任何原文字符（2.0.4 立下的底线，沿用至今）：paratext 段落的
#: 原文不能出现在喂给模型的任何文本里，段号本身仍然保留在 "[段N] ..." 里。
_PARATEXT_PLACEHOLDER_TEXT = (
    "（作者的话，非正文——已按映射台 coverage_ledger.paratext 账略去原文，"
    "不要据此生成画面、台词或节拍）"
)

_SENTENCE_END_CHARS = "。！？!?；;…"
_CLOSING_QUOTES = "”」』\""


def _line_spans(text: str) -> list[tuple[int, int]]:
    """按 ``\\n`` 切出每行的 [start, end) 偏移（不含换行符本身）。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            spans.append((start, i))
            start = i + 1
    spans.append((start, len(text)))
    return spans


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int]:
    """去掉 [start, end) 内的首尾空白，返回收紧后的偏移；全空白时 start==end。"""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def split_source_units(text: str) -> list[tuple[int, int]]:
    """确定性切句：先按换行切，再按句末标点切；句末标点后紧跟的闭引号并入
    前一句；空白单元丢弃；单元覆盖全部非空白字符、互不重叠。无模型调用。
    """
    units: list[tuple[int, int]] = []
    for line_start, line_end in _line_spans(text):
        line = text[line_start:line_end]
        n = len(line)
        unit_start = 0
        i = 0
        while i < n:
            if line[i] in _SENTENCE_END_CHARS:
                j = i + 1
                # 「？！」「……」这类连续终止符是同一句的结尾，闭引号同理；
                # 否则会切出只含一个「！」或「…」的伪单元。
                while j < n and (line[j] in _SENTENCE_END_CHARS or line[j] in _CLOSING_QUOTES):
                    j += 1
                start, end = _trimmed_span(line, unit_start, j)
                if end > start:
                    units.append((line_start + start, line_start + end))
                unit_start = j
                i = j
                continue
            i += 1
        start, end = _trimmed_span(line, unit_start, n)
        if end > start:
            units.append((line_start + start, line_start + end))
    return _merge_punctuation_only_units(text, units)


def _merge_punctuation_only_units(text: str, units: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """没有任何文字（只有标点/符号）的单元并入前一个单元；开头就是纯标点的保留原样。"""
    merged: list[tuple[int, int]] = []
    for start, end in units:
        if merged and not re.search(r"\w", text[start:end]):
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def render_source_units(index: int, text: str, placeholder: str | None) -> str:
    """渲染成多行 "[段N·S07] 句子原文"（S 编号 1 起）；``placeholder`` 非
    None 时说明这是 paratext 段，整段渲染成一行 "[段N] {placeholder}"，不切
    单元——占位文本本身不是原文，切出的单元号对模型没有意义。
    """
    if placeholder is not None:
        return f"[段{index}] {placeholder}"
    units = split_source_units(text)
    lines = [
        f"[段{index}·S{unit_no:02d}] {text[start:end]}"
        for unit_no, (start, end) in enumerate(units, start=1)
    ]
    return "\n".join(lines)


class _AiSourceUnitRange(BaseModel):
    """节拍表段落对某一个原文段声明的单元范围（闭区间、1 起）。

    ``from_unit``/``to_unit`` 只做类型与下界校验，"1 ≤ from ≤ to ≤ 该段单元数"
    与跨段有序/覆盖检查都交给 ``segment_unit_range_errors``——放在这里做会
    产出一条不可操作的 pydantic 报错，不如统一走带段号/单元号的详细报错。
    """

    source_segment_index: int
    from_unit: int = Field(ge=1)
    to_unit: int = Field(ge=1)


def quote_unit_index(quote: Any, segment_text: str) -> int:
    """台词在 ``segment_text`` 里的单元号（1 起）；定位不到返回 -1。

    优先用 ``quote.start_offset``（>=0 时可信）；该字段缺失或仍是默认值 -1
    时退回按 ``quote.text`` 在 ``segment_text`` 里的首次出现位置定位——用
    ``getattr`` 是因为 ``DialogueQuote.start_offset``/``end_offset`` 由另一
    位代理同期新增，取值域契约按这里约定，不假设字段一定存在。
    """
    units = split_source_units(segment_text)
    start_offset = getattr(quote, "start_offset", -1)
    if start_offset is None or start_offset < 0:
        text = getattr(quote, "text", "") or ""
        start_offset = segment_text.find(text)
        if start_offset < 0:
            return -1
    for unit_no, (start, end) in enumerate(units, start=1):
        if start <= start_offset < end:
            return unit_no
    return -1


def _plan_range_lookup(plan: Any) -> dict[int, list[_AiSourceUnitRange]]:
    lookup: dict[int, list[_AiSourceUnitRange]] = {}
    for r in plan.source_unit_ranges:
        lookup.setdefault(r.source_segment_index, []).append(r)
    return lookup


def _plan_own_range_errors(
    plan: Any, unit_counts: dict[int, int], paratext_indexes: set[int],
) -> list[str]:
    """单个段自己的范围声明是否完整、合法——不看跨段顺序/覆盖（那是另一半）。"""
    errors: list[str] = []
    lookup = _plan_range_lookup(plan)
    wanted = [i for i in plan.source_segment_indexes if i not in paratext_indexes and i in unit_counts]
    for index in wanted:
        entries = lookup.get(index, [])
        if len(entries) != 1:
            errors.append(
                f"第 {plan.segment_no} 段引用了原文段 {index}，但 source_unit_ranges 里对应"
                f"声明了 {len(entries)} 条范围（应恰好 1 条）：请为原文段 {index} 补一条唯一的"
                "{from_unit, to_unit} 范围"
            )
            continue
        entry = entries[0]
        total = unit_counts[index]
        if not (1 <= entry.from_unit <= entry.to_unit <= total):
            errors.append(
                f"第 {plan.segment_no} 段对原文段 {index} 声明的单元范围 "
                f"S{entry.from_unit:02d}-S{entry.to_unit:02d} 不合法：原文段 {index} 共有 "
                f"{total} 个单元，范围必须满足 1 ≤ from_unit ≤ to_unit ≤ {total}"
            )
    extraneous = sorted(set(lookup) - set(wanted))
    for index in extraneous:
        errors.append(
            f"第 {plan.segment_no} 段为原文段 {index} 声明了 source_unit_ranges，但该段号不在"
            f"本段 source_segment_indexes 引用范围内（或是 paratext 段）：请删除这条范围声明，"
            f"或把 {index} 加入本段 source_segment_indexes"
        )
    return errors


def _cross_plan_order_and_coverage_errors(
    plans: list[Any], unit_counts: dict[int, int],
) -> list[str]:
    """同一原文段号被多段引用时的顺序（不回退）与覆盖（无洞）检查。"""
    by_source: dict[int, list[tuple[int, _AiSourceUnitRange]]] = {}
    for plan in plans:
        for r in plan.source_unit_ranges:
            by_source.setdefault(r.source_segment_index, []).append((plan.segment_no, r))
    errors: list[str] = []
    for source_index, entries in sorted(by_source.items()):
        entries.sort(key=lambda item: item[0])
        prev_seg_no, prev_range = None, None
        for seg_no, r in entries:
            if prev_range is not None and r.from_unit < prev_range.to_unit:
                errors.append(
                    f"原文段 {source_index}：第 {prev_seg_no} 段范围到 S{prev_range.to_unit:02d}，"
                    f"第 {seg_no} 段却从 S{r.from_unit:02d} 开始，发生了回退或重叠超过一个单元；"
                    f"唯一修法是把第 {seg_no} 段的 from_unit 改成 {prev_range.to_unit} 或更大"
                )
            prev_seg_no, prev_range = seg_no, r
        total = unit_counts.get(source_index)
        if total is None:
            continue
        covered: set[int] = set()
        for _seg_no, r in entries:
            covered.update(range(r.from_unit, r.to_unit + 1))
        missing = sorted(set(range(1, total + 1)) - covered)
        if missing:
            missing_labels = [f"S{m:02d}" for m in missing]
            errors.append(
                f"原文段 {source_index} 的单元 {missing_labels} 没有被任何段的 source_unit_ranges "
                "覆盖（洞即删戏）：请把这些单元补进某一段的范围声明"
            )
    return errors


def segment_unit_range_errors(
    plans: list[Any], source_segments: list[SourceSegment], paratext_indexes: set[int],
) -> list[str]:
    """阻断式校验：每段各占一块原文、按顺序不回退、并集覆盖全部单元。

    ``plans`` 用鸭子类型（``.segment_no``/``.source_segment_indexes``/
    ``.source_unit_ranges``），不绑定具体 pydantic 模型，方便节拍表草稿与
    容量归一化产出的中间态双方复用。
    """
    unit_counts = {
        i: len(split_source_units(seg.text)) for i, seg in enumerate(source_segments, start=1)
    }
    errors: list[str] = []
    for plan in plans:
        errors.extend(_plan_own_range_errors(plan, unit_counts, paratext_indexes))
    errors.extend(_cross_plan_order_and_coverage_errors(plans, unit_counts))
    return errors


def kept_line_unit_binding_errors(
    kept_lines: list[Any], quotes: list[Any], plans: list[Any], source_segments: list[SourceSegment],
) -> list[str]:
    """每条 kept 台词的单元号必须落在它被分到的那一段声明的范围内。

    与 ``storyboard_dialogue_ledger._kept_segment_binding_errors``（段号绑定）
    是同一族检查的更细粒度版本：那边只查"台词的原文段号是否在这一段引用范围
    内"，这里进一步查"台词具体落在哪个句单元，是否也在这一段声明的单元
    范围内"——同一原文段被多段引用时，段号绑定合法不代表单元绑定也合法。
    """
    quotes_by_id = {q.quote_id: q for q in quotes}
    plans_by_no = {p.segment_no: p for p in plans}
    ranges_by_source: dict[int, list[tuple[int, _AiSourceUnitRange]]] = {}
    for plan in plans:
        for r in plan.source_unit_ranges:
            ranges_by_source.setdefault(r.source_segment_index, []).append((plan.segment_no, r))
    errors: list[str] = []
    for item in kept_lines:
        quote = quotes_by_id.get(item.quote_id)
        plan = plans_by_no.get(item.segment_no)
        if quote is None or plan is None:
            continue  # 已在台账 partition/binding 检查里报过，这里不重复报
        source_index = quote.source_segment_index
        if not (1 <= source_index <= len(source_segments)):
            continue
        unit_no = quote_unit_index(quote, source_segments[source_index - 1].text)
        if unit_no < 1:
            errors.append(
                f"{item.quote_id} 在原文段 {source_index} 中定位不到句单元（按偏移或原文都未"
                "匹配到），请核对台账抽取是否正确"
            )
            continue
        own_ranges = [r for r in plan.source_unit_ranges if r.source_segment_index == source_index]
        if any(r.from_unit <= unit_no <= r.to_unit for r in own_ranges):
            continue
        # 同一段对同一原文段可以声明多个范围（如 S1-2 与 S5-6），元组首元素相同时
        # 不能落到比较范围对象本身——那会 TypeError 把整集分镜打死（2026-09-04 B 实测）。
        covering_no = next(
            (no for no, r in sorted(
                ranges_by_source.get(source_index, []),
                key=lambda item: (item[0], item[1].from_unit, item[1].to_unit),
            ) if r.from_unit <= unit_no <= r.to_unit),
            None,
        )
        where = f"覆盖单元 S{unit_no:02d} 的是第 {covering_no} 段" if covering_no is not None else (
            f"没有任何段的 source_unit_ranges 覆盖单元 S{unit_no:02d}"
        )
        own_desc = "不存在" if not own_ranges else "是 " + "、".join(
            f"S{r.from_unit:02d}-S{r.to_unit:02d}" for r in own_ranges
        )
        errors.append(
            f"kept_lines 的 {item.quote_id}（原文段 {source_index} 单元 S{unit_no:02d}）被分到第 "
            f"{item.segment_no} 段，但该段对原文段 {source_index} 声明的单元范围{own_desc}未覆盖"
            f"这个单元；{where}，请把这条台词的 segment_no 改到覆盖它的那一段，或调整该段的 "
            "source_unit_ranges"
        )
    return errors


_CONTEXT_WINDOW = 2
_CONTEXT_NOTE = "仅供衔接参考，不要拍进本段"


def _own_range_for(plan: Any, index: int) -> _AiSourceUnitRange | None:
    return next((r for r in plan.source_unit_ranges if r.source_segment_index == index), None)


def segment_source_payload(
    plan: Any, source_segments: list[SourceSegment], paratext_indexes: set[int],
) -> dict[str, Any]:
    """阶段二喂给模型的原文字段：本段范围内的单元 + 前后各
    ``_CONTEXT_WINDOW`` 个单元的衔接上下文（在同一原文段号内取，不跨原文段
    借上下文——多原文段的衔接由 ``previous_segment_prompt`` 承担）。
    """
    body_lines: list[str] = []
    context_before: list[str] = []
    context_after: list[str] = []
    for index in plan.source_segment_indexes:
        if not (1 <= index <= len(source_segments)):
            continue
        segment = source_segments[index - 1]
        if index in paratext_indexes:
            body_lines.append(render_source_units(index, segment.text, _PARATEXT_PLACEHOLDER_TEXT))
            continue
        own_range = _own_range_for(plan, index)
        if own_range is None:
            continue
        for unit_no, (start, end) in enumerate(split_source_units(segment.text), start=1):
            rendered = f"[段{index}·S{unit_no:02d}] {segment.text[start:end]}"
            if own_range.from_unit <= unit_no <= own_range.to_unit:
                body_lines.append(rendered)
            elif own_range.from_unit - _CONTEXT_WINDOW <= unit_no < own_range.from_unit:
                context_before.append(rendered)
            elif own_range.to_unit < unit_no <= own_range.to_unit + _CONTEXT_WINDOW:
                context_after.append(rendered)
    return {
        "source_text_by_segment": "\n".join(body_lines),
        "context_before": context_before,
        "context_after": context_after,
        "context_note": _CONTEXT_NOTE,
    }


def reassign_kept_lines_to_covering_segments(
    kept_lines: list[Any], quotes: list[Any], plans: list[Any], source_segments: list[SourceSegment],
) -> list[dict[str, Any]]:
    """确定性归一化：kept 台词落在别的段声明的单元范围里时，直接把它挪到覆盖它的那一段。

    EP1 试验跑实测（2026-09-04）：模型三次都把 Q08 分给第 8 段，而第 8 段声明的范围是
    S15-S20、Q08 在 S21、覆盖 S21 的是第 9 段——这是唯一确定性的修法，打回模型重试三次仍
    没改对，整集分镜因此失败。范围声明是主事实（它决定各段拍哪块原文），台词的段号只是
    冗余信息，按单元位置归一化不引入任何猜测；多段共享边界单元时归到最早的那段。
    返回 ``[{"quote_id", "from_segment_no", "to_segment_no", "unit"}]`` 供观测。
    """
    quotes_by_id = {q.quote_id: q for q in quotes}
    plans_by_no = {p.segment_no: p for p in plans}
    moves: list[dict[str, Any]] = []
    for item in kept_lines:
        quote = quotes_by_id.get(item.quote_id)
        plan = plans_by_no.get(item.segment_no)
        if quote is None or plan is None or not (1 <= quote.source_segment_index <= len(source_segments)):
            continue
        source_index = quote.source_segment_index
        unit_no = quote_unit_index(quote, source_segments[source_index - 1].text)
        if unit_no >= 1 and any(
            r.source_segment_index == source_index and r.from_unit <= unit_no <= r.to_unit
            for r in plan.source_unit_ranges
        ):
            continue
        covering: list[int] = []
        if unit_no >= 1:
            covering = sorted(
                p.segment_no for p in plans
                for r in p.source_unit_ranges
                if r.source_segment_index == source_index and r.from_unit <= unit_no <= r.to_unit
            )
        if not covering:
            # 2026-09-05 我欲封天第 23 集：台词的原文段号根本不在它所在段的 source_segment_indexes 里
            # （容量归一化拆段后尤其常见），而没有任何段的单元范围覆盖它——退一步按原文段号归到
            # 最早引用该原文段的段；连这都没有才留给 dialogue_ledger_errors 报「新增段落」。
            covering = sorted(
                p.segment_no for p in plans
                if source_index in list(getattr(p, "source_segment_indexes", None) or [])
            )
            if not covering or item.segment_no in covering:
                continue
        moves.append({"quote_id": item.quote_id, "from_segment_no": item.segment_no,
                      "to_segment_no": covering[0], "unit": unit_no})
        item.segment_no = covering[0]
    return moves
