"""对白台账：分镜台 2.1.0 改造一（见 app.production.storyboard_pack 的
STORYBOARD_PACK_VERSION 2.1.0 changelog）。

背景：实测 EP1 原文 488 字引号对白，最终只有 68 字存活到分镜产出——阶段一
「合并同质描写」与阶段二「挑最要紧的一到两句」两处都在悄悄丢台词，且丢了
什么、丢了多少，此前完全没有留痕。本模块把「原文有哪些台词」变成一份确定性
台账（不依赖模型自报），阶段一必须对每一句台词显式决定去留（kept_lines /
dropped_lines）并说明弃置理由，阶段二据此拿到「这段必须说出口的台词」
（required_dialogue），台词预算从「模型看着办」变成「代码分配、模型执行」。

依赖边界：只依赖 app.config（口播容量唯一口径）、app.spoken_contract（口播
字数统计）、app.textmatch（模糊匹配阈值）与 app.source_excerpt（原文分段
类型），均为同层或更低层（见 app/LAYERS.toml "app.production" = 4、
"app.spoken_contract" = 4、"app.source_excerpt" = 1），不依赖
app.production.storyboard_pack 本身，避免循环导入。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app import config, spoken_contract, textmatch
from app.source_excerpt import SourceSegment

# 覆盖原文台词引用的四种既有引号写法：中文双引号“”、直角引号「」、双直角
# 引号『』、英文/全角直引号\"。与 app.validators.screenplay_text 的
# _SOURCE_QUOTED_UTTERANCE_RE 同一族判据（那边只覆盖前三种，因为它的调用方
# key_lines 从不用直引号标台词）；这里分开写成四个模式而不是一个字符类，
# 是因为直引号的开合是同一个字符，与三种中文引号共用一个「排除自身」的否定
# 字符类会连带排除中文引号本身，导致嵌套引用（"他说『你好』"）被截断。
_QUOTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"“(?P<line>[^”\n]{1,240})”"),
    re.compile(r"「(?P<line>[^」\n]{1,240})」"),
    re.compile(r"『(?P<line>[^『』\n]{1,240})』"),
    re.compile(r'"(?P<line>[^"\n]{1,240})"'),
)


class DialogueQuote(BaseModel):
    """原文里确定性抽取出的一句引号台词——阶段一的「取值域」本身。"""

    quote_id: str
    source_segment_index: int
    text: str
    content_chars: int


class _AiKeptLine(BaseModel):
    quote_id: str
    segment_no: int


class _AiDroppedLine(BaseModel):
    quote_id: str
    reason: str = Field(min_length=1)


def extract_dialogue_targets(
    segments: list[SourceSegment], paratext_indexes: set[int],
) -> list[DialogueQuote]:
    """从非 paratext 原文段确定性抽取全部引号台词，按出现顺序编号 quote_id。

    全部收录、不设长度黑名单——语气词、屏上文字这类要不要保留是模型在台账
    里显式弃置的判断（dropped_lines.reason），代码不预判、不过滤，否则「代码
    先过滤掉一批」和「模型再丢一批」会重犯同一个问题：谁都不为总丢失量负责。
    """
    quotes: list[DialogueQuote] = []
    counter = 0
    for index, segment in enumerate(segments, start=1):
        if index in paratext_indexes:
            continue
        matches: list[tuple[int, str]] = []
        for pattern in _QUOTE_PATTERNS:
            for match in pattern.finditer(segment.text):
                line = match.group("line").strip()
                if line:
                    matches.append((match.start(), line))
        matches.sort(key=lambda item: item[0])
        for _offset, line in matches:
            counter += 1
            quotes.append(DialogueQuote(
                quote_id=f"Q{counter:02d}",
                source_segment_index=index,
                text=line,
                content_chars=spoken_contract.content_char_count(line),
            ))
    return quotes


def _quote_id_partition_errors(
    quotes: list[DialogueQuote], kept: list[_AiKeptLine], dropped: list[_AiDroppedLine],
) -> list[str]:
    """quote_id 必须在 kept_lines ∪ dropped_lines 中恰好出现一次。

    valid_ids 为空（本章没有引号台词）时，任何被引用的 id 都落进
    unknown——空取值域等于「什么都不合法」，不是「不用查」（2.0.2 changelog
    记录过的同一类教训：真值短路会把空集合误判成跳过检查）。
    """
    errors: list[str] = []
    valid_ids = {q.quote_id for q in quotes}
    referenced = [k.quote_id for k in kept] + [d.quote_id for d in dropped]
    unknown = sorted({qid for qid in referenced if qid not in valid_ids})
    if unknown:
        errors.append(
            f"kept_lines/dropped_lines 引用了不存在的 quote_id {unknown}；"
            "合法取值只有 dialogue_targets 里逐字给出的 quote_id，不得虚构"
        )
    counts: dict[str, int] = {}
    for qid in referenced:
        counts[qid] = counts.get(qid, 0) + 1
    missing = sorted(qid for qid in valid_ids if counts.get(qid, 0) == 0)
    duplicated = sorted(qid for qid, c in counts.items() if qid in valid_ids and c > 1)
    if missing:
        errors.append(
            f"quote_id {missing} 既不在 kept_lines 也不在 dropped_lines 中；"
            "dialogue_targets 里的每一句原文台词都必须显式决定去留，不能不提"
        )
    if duplicated:
        errors.append(f"quote_id {duplicated} 在 kept_lines/dropped_lines 中重复出现，每句台词只能归为一处")
    return errors


def _kept_segment_binding_errors(
    kept: list[_AiKeptLine],
    quotes_by_id: dict[str, DialogueQuote],
    segment_source_indexes: dict[int, list[int]],
) -> list[str]:
    """kept 的 segment_no 必须存在，且这句台词的原文段号必须在那一段覆盖范围内。

    这条挡的是「台词跨段漂移」：模型把某句台词分给了一个根本没有引用到它
    原文段号的新段，会让下游台词与画面来源对不上。
    """
    errors: list[str] = []
    for item in kept:
        quote = quotes_by_id.get(item.quote_id)
        if quote is None:
            continue  # 已在 partition 检查里报过，这里不重复报同一个问题
        allowed = segment_source_indexes.get(item.segment_no)
        if allowed is None:
            errors.append(f"kept_lines 的 {item.quote_id} 引用了不存在的 segment_no={item.segment_no}")
        elif quote.source_segment_index not in allowed:
            errors.append(
                f"kept_lines 的 {item.quote_id}（原文段号 {quote.source_segment_index}）"
                f"被分到第 {item.segment_no} 段，但该段 source_segment_indexes 只覆盖 "
                f"{sorted(allowed)}；台词不得跨段漂移，必须分到覆盖它原文段号的那个段"
            )
    return errors


def _segment_capacity_errors(
    kept: list[_AiKeptLine],
    quotes_by_id: dict[str, DialogueQuote],
    *,
    max_chars_per_segment: int,
) -> list[str]:
    """同一段分到的必保台词合计字数不能超过 15 秒口播容量。

    错误文案必须给出合法出路——「拆成更多段」，不是「删台词」：段数是自由
    变量，用户已拍板连贯性优先于时长紧凑，不设段数上限。
    """
    totals: dict[int, int] = {}
    for item in kept:
        quote = quotes_by_id.get(item.quote_id)
        if quote is not None:
            totals[item.segment_no] = totals.get(item.segment_no, 0) + quote.content_chars
    errors = []
    for segment_no, total in sorted(totals.items()):
        if total > max_chars_per_segment:
            errors.append(
                f"第 {segment_no} 段分到的必保台词合计 {total} 字，超过 15 秒口播容量 "
                f"{max_chars_per_segment} 字；把这一段台词密集处拆成更多 15 秒段——"
                "段数由剧情密度决定、没有上限，不要为了少分段而丢弃或压缩台词"
            )
    return errors


def dialogue_ledger_errors(
    *,
    quotes: list[DialogueQuote],
    kept_lines: list[_AiKeptLine],
    dropped_lines: list[_AiDroppedLine],
    segment_source_indexes: dict[int, list[int]],
    max_chars_per_segment: int,
) -> list[str]:
    """阶段一对白台账的全部 blocking 校验，供 semantic retry 使用。"""
    quotes_by_id = {q.quote_id: q for q in quotes}
    errors = _quote_id_partition_errors(quotes, kept_lines, dropped_lines)
    errors.extend(_kept_segment_binding_errors(kept_lines, quotes_by_id, segment_source_indexes))
    errors.extend(_segment_capacity_errors(kept_lines, quotes_by_id, max_chars_per_segment=max_chars_per_segment))
    return errors


def required_dialogue_for_segments(
    kept_lines: list[_AiKeptLine], quotes: list[DialogueQuote],
) -> dict[int, list[dict[str, Any]]]:
    """按 segment_no 归组「这一段必须说出口的原文台词」，供阶段二逐段调用使用。

    产出形状是阶段二 task_payload.required_dialogue 与
    StoryboardPackSegment.required_dialogue 共用的同一份数据——两处都直接用
    这个函数的返回值，不各自派生一份可能漂移的副本。
    """
    quotes_by_id = {q.quote_id: q for q in quotes}
    result: dict[int, list[dict[str, Any]]] = {}
    for item in kept_lines:
        quote = quotes_by_id.get(item.quote_id)
        if quote is None:
            continue
        result.setdefault(item.segment_no, []).append({
            "quote_id": quote.quote_id,
            "text": quote.text,
            "source_segment_index": quote.source_segment_index,
        })
    return result


def dialogue_ledger_summary(
    quotes: list[DialogueQuote],
    kept_lines: list[_AiKeptLine],
    dropped_lines: list[_AiDroppedLine],
) -> dict[str, Any]:
    """episode 级完整台账：kept + dropped + 弃置率统计。

    落成 ``storyboard_pack_dialogue_ledger`` 类型的 EvidenceArtifact（见
    ``persist_storyboard_pack``），保证「原文有多少台词、丢了哪句、丢了多少
    字」事后可查——本次改造要解决的正是这一类此前完全没有留痕的静默丢失。
    """
    quotes_by_id = {q.quote_id: q for q in quotes}
    total_chars = sum(q.content_chars for q in quotes)
    dropped_chars = sum(
        quotes_by_id[item.quote_id].content_chars
        for item in dropped_lines if item.quote_id in quotes_by_id
    )
    return {
        "total_quotes": len(quotes),
        "total_chars": total_chars,
        "kept_lines": [item.model_dump(mode="json") for item in kept_lines],
        "dropped_lines": [
            {
                **item.model_dump(mode="json"),
                "text": quotes_by_id[item.quote_id].text if item.quote_id in quotes_by_id else "",
            }
            for item in dropped_lines
        ],
        "dropped_count": len(dropped_lines),
        "dropped_char_ratio": round(dropped_chars / total_chars, 4) if total_chars else 0.0,
    }


def beat_sheet_dialogue_ledger_rules() -> list[str]:
    """阶段一 rules[] 里对白台账的两条正面陈述，供 storyboard_pack._beat_sheet_rules 拼接。"""
    return [
        "dialogue_targets 列出了本章全部原文引号台词：每一条 quote_id 都必须在"
        "kept_lines 或 dropped_lines 里逐字复制、出现且只出现一次，不得虚构、遗漏"
        "或重复。台词默认保留（进 kept_lines，写清分到哪一段 segment_no，必须是"
        "这句台词原文所在段号被分到的那个新段）；只有重复寒暄、纯语气词感叹"
        "（如「啊」「哦」）、或者本来就不是台词的引用（牌匾/书信/标题等屏上文字）"
        "才可以放进 dropped_lines，并在 reason 里写清具体理由，不得留空",
        "分到同一段的 kept_lines 台词，合计纯文字字数不能超过这段 15 秒的口播容量"
        f"（{config.MAX_SPOKEN_CHARS_PER_SHOT} 字）；装不下就把这一段拆成更多段落，"
        "段数不设上限，不要为了凑少数段而丢弃或压缩台词",
    ]


def required_dialogue_rule(required_dialogue: list[dict[str, Any]]) -> str:
    """阶段二必保台词的正面陈述规则（容量已在阶段一分配好，重申"不得再挑拣"）。"""
    if not required_dialogue:
        return (
            "本段 required_dialogue 为空——上一阶段判定这段没有必须保留的原文"
            "台词，你可以据情节需要自行决定是否安排台词，本段全部台词加起来"
            f"仍不能超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字。"
        )
    return (
        "本段 required_dialogue 列出了上一阶段已经按 15 秒容量分配好、必须"
        "原样体现的原文台词（每条给 quote_id/text/source_segment_index）："
        "这些台词的主干必须逐字保留（允许「说」字之类的衔接性微调，不得整句"
        "改写或省略），每一条都要同时出现在 dialogue[] 与 prompt_text 里，"
        "不得因为篇幅紧张自行弃置某一条。除了这些之外，你可以补充少量衔接性"
        f"台词，但本段全部台词加起来仍不能超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字。"
    )


def required_dialogue_missing_errors(
    required_dialogue: list[dict[str, Any]], dialogue_line_texts: list[str],
) -> list[str]:
    """阶段二 blocking 检查：必保台词是否真的被说出口（模糊匹配，允许衔接性微调）。

    阈值复用 app.textmatch.KEY_LINE_PRESENT_RATIO——与
    app/validators/screenplay_text.py 的同名常量是同一个数字，不新造口径。
    """
    if not required_dialogue:
        return []
    haystack = "".join(textmatch.condense(text) for text in dialogue_line_texts)
    errors: list[str] = []
    for item in required_dialogue:
        text = str(item.get("text") or "")
        needle = textmatch.condense(text)
        if not needle or needle in haystack:
            continue
        if textmatch.longest_run_ratio(text, haystack) >= textmatch.KEY_LINE_PRESENT_RATIO:
            continue
        errors.append(
            f"必保台词 {item.get('quote_id')}「{text}」未出现在 dialogue[] 中；"
            "这句台词的容量已经在上一阶段分配好，不得在本段自行弃置或省略——"
            "允许衔接性微调，但主干必须保留"
        )
    return errors
