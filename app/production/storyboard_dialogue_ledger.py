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

2.1.1（真实 EP1 回归，ERR-20260901-bcfa58/run_4e66c18f6713）：容量闸门本身
判得对（Q09(12)+Q10(3)+Q11(9)+Q12(31)=55 字全归第 5 段，超 54 字上限），但
三次语义重试全部没修出、耗尽预算致整集失败——三次响应长度几乎不变
（4840/4863/4861），说明模型没找到可执行的修法。根因三处，全在报错/提示词
的可操作性，不在闸门阈值（54 不动）：① 报错只给合计数，模型自己数台词字数
必然不准，尤其是要在"挪去别的段"和"拆成两段"之间选一个可行方案时；② 规则
从未正面说过"多个段可以共享同一原文段号"，模型不敢往这个方向想；③
model_gateway 的通用修复包装写死"保持其余已验证字段不变"，把这次真正需要
的结构性修复（挪行、加段、重排 segment_no）一并劝退了（第 4 段同样覆盖
Q09-12 的原文段号且只用了 18 字，挪一条过去两边都合规，模型三次都没想到）。
修复：`_segment_capacity_errors` 补齐逐条字数明细、反查可挪去的其它
segment_no、显式声明这类修复不受"保持其余字段不变"限制；`extract_
dialogue_targets` 在抽取期预拆单条超容量的引句（结构性无解地雷，EP1 最长
39 字未触发但必须堵上）；`beat_sheet_dialogue_ledger_rules` 补两条正面陈述。
只改生成路径的报错文案与抽取逻辑，落库形状不变。
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
    """原文里确定性抽取出的一句台词——阶段一的「取值域」本身。speaker/note/
    start_offset/end_offset 供 storyboard_dialogue_extract 的说话人行路径
    使用；本文件的 extract_dialogue_targets（纯引号正则）留默认值不填。"""

    quote_id: str
    source_segment_index: int
    text: str
    content_chars: int
    speaker: str = ""
    note: str = ""
    start_offset: int = -1  # 相对 segment.text 的偏移，-1 表示未知
    end_offset: int = -1


class _AiKeptLine(BaseModel):
    quote_id: str
    segment_no: int


class _AiDroppedLine(BaseModel):
    quote_id: str
    reason: str = Field(min_length=1)


_CLAUSE_RE = re.compile(r".*?[，。！？；,.!?;]|.+$")


def _split_overlong_quote(text: str, *, max_chars: int) -> list[str]:
    """按句读确定性拆成多条、每条 ≤max_chars，不改写用词（ERR-20260901-bcfa58
    预防：单条引句超过单段容量是结构性无解地雷——不能跨段、装不下、又不许
    弃置）。口径参照老管线 app.screenplay_ir.prompt_context._split_spoken_line：
    先按标点切分句读、攒到装不下另起一条；单个句读本身仍超限才逐字符切。
    """
    clauses = [c for c in _CLAUSE_RE.findall(text) if c.strip()]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        if current and spoken_contract.content_char_count(current + clause) > max_chars:
            chunks.append(current)
            current = ""
        if spoken_contract.content_char_count(clause) <= max_chars:
            current += clause
            continue
        for character in clause:
            if current and spoken_contract.content_char_count(current + character) > max_chars:
                chunks.append(current)
                current = ""
            current += character
    if current:
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


def _quote_parts(line: str, *, max_chars: int) -> list[str]:
    """单条引句是否需要预拆；恰好等于 max_chars 不拆。"""
    if spoken_contract.content_char_count(line) <= max_chars:
        return [line]
    return _split_overlong_quote(line, max_chars=max_chars)


def extract_dialogue_targets(
    segments: list[SourceSegment], paratext_indexes: set[int],
) -> list[DialogueQuote]:
    """从非 paratext 原文段确定性抽取全部引号台词，按出现顺序编号 quote_id。

    全部收录、不设长度黑名单——语气词、屏上文字这类要不要保留是模型在台账
    里显式弃置的判断（dropped_lines.reason），代码不预判、不过滤，否则「代码
    先过滤掉一批」和「模型再丢一批」会重犯同一个问题：谁都不为总丢失量负责。
    单条超过 config.MAX_SPOKEN_CHARS_PER_SHOT 的引句在这里预拆成多条（见
    _split_overlong_quote），quote_id 仍按出现顺序连续编号，拆出的各部分只是
    编号相邻，不再是同一条。
    """
    max_chars = config.MAX_SPOKEN_CHARS_PER_SHOT
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
            for part in _quote_parts(line, max_chars=max_chars):
                counter += 1
                quotes.append(DialogueQuote(
                    quote_id=f"Q{counter:02d}",
                    source_segment_index=index,
                    text=part,
                    content_chars=spoken_contract.content_char_count(part),
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
    uncovered: dict[int, list[str]] = {}
    for item in kept:
        quote = quotes_by_id.get(item.quote_id)
        if quote is None:
            continue  # 已在 partition 检查里报过，这里不重复报同一个问题
        allowed = segment_source_indexes.get(item.segment_no)
        if allowed is not None and quote.source_segment_index in allowed:
            continue
        covering = sorted(
            no for no, indexes in segment_source_indexes.items() if quote.source_segment_index in indexes
        )
        if not covering:
            # 没有任何段覆盖这句台词的原文段：单说「分到覆盖它的那个段」是让模型
            # 去找一个不存在的目标（ERR-20260902-b2db9f：模型只切了 1 段却要安置
            # 7 个原文段的 46 句台词，三轮修复都在虚构 segment_no）。按原文段聚合，
            # 下面统一给出「新增段落」这条唯一可行的修法。
            uncovered.setdefault(quote.source_segment_index, []).append(item.quote_id)
            continue
        where = f"覆盖它原文段号的是第 {covering} 段，必须分到其中一段"
        if allowed is None:
            errors.append(f"kept_lines 的 {item.quote_id} 引用了不存在的 segment_no={item.segment_no}；{where}")
        else:
            errors.append(
                f"kept_lines 的 {item.quote_id}（原文段号 {quote.source_segment_index}）"
                f"被分到第 {item.segment_no} 段，但该段 source_segment_indexes 只覆盖 "
                f"{sorted(allowed)}；台词不得跨段漂移，{where}"
            )
    for source_index, quote_ids in sorted(uncovered.items()):
        errors.append(
            f"原文段 {source_index} 的必保台词 {quote_ids} 没有任何段覆盖它（当前 segments 的 "
            f"source_segment_indexes 只覆盖 {sorted({i for v in segment_source_indexes.values() for i in v})}）："
            f"请在 segments 里新增一段、source_segment_indexes 含 {source_index}，重排全部 segment_no，"
            "并把这些台词分到新段；新增段落属于修复本身，不受「保持其余已验证字段不变」的限制"
        )
    return errors


def _segment_overflow_breakdown(quotes: list[DialogueQuote]) -> str:
    """`Q09(12字)+Q10(3字)+Q11(9字)+Q12(31字)=55字` 逐条明细，供模型直接读数，
    不用自己数字数（ERR-20260901-bcfa58：三次响应长度几乎不变，根因之一是
    报错只给合计数，模型自己数字数必然不准）。"""
    ordered = sorted(quotes, key=lambda q: q.quote_id)
    parts = "+".join(f"{q.quote_id}({q.content_chars}字)" for q in ordered)
    return f"{parts}={sum(q.content_chars for q in ordered)}字"


def _segment_move_targets(
    quotes: list[DialogueQuote],
    segment_no: int,
    segment_source_indexes: dict[int, list[int]],
    segment_totals: dict[int, int],
    *,
    max_chars_per_segment: int,
) -> list[int]:
    """同样覆盖这些台词原文段号、且**确有剩余容量**的其它 segment_no。

    ERR-20260901-b1c349（真实 EP1 二次回归，2.1.2）：上一版只反查"覆盖同一
    原文段号"，不核对目标段自己是否也满/超，制造了"循环指路"——两个相邻段
    互相点名对方当挪动目标，而对方同样超容，模型三次重试都在两个死路之间
    打转。这里额外要求 segment_totals[other_no] < max_chars_per_segment。
    """
    source_indexes = {q.source_segment_index for q in quotes}
    return sorted(
        other_no for other_no, allowed in segment_source_indexes.items()
        if other_no != segment_no
        and source_indexes & set(allowed)
        and segment_totals.get(other_no, 0) < max_chars_per_segment
    )


def _segment_capacity_errors(
    kept: list[_AiKeptLine],
    quotes_by_id: dict[str, DialogueQuote],
    segment_source_indexes: dict[int, list[int]],
    *,
    max_chars_per_segment: int,
) -> list[str]:
    """同一段分到的必保台词合计字数不能超过 15 秒口播容量。

    2.1.2 起只在归一化后的断言路径使用（见 storyboard_capacity_normalize.
    normalize_and_assert_capacity）——容量维度不再打回模型语义重试，但文案
    仍必须正确：断言真的触发时说明归一化算法本身有 bug，需要给排查线索。
    逐条字数明细（不用人自己数）、只点名确有剩余容量的目标段、给出按
    ceil(total/54) 算出的确切拆分段数、明确声明这类结构性修复不受
    chat_structured 通用修复包装"保持其余已验证字段不变"的限制。
    """
    by_segment: dict[int, list[DialogueQuote]] = {}
    for item in kept:
        quote = quotes_by_id.get(item.quote_id)
        if quote is not None:
            by_segment.setdefault(item.segment_no, []).append(quote)
    segment_totals = {no: sum(q.content_chars for q in qs) for no, qs in by_segment.items()}
    errors = []
    for segment_no, quotes in sorted(by_segment.items()):
        total = segment_totals[segment_no]
        if total <= max_chars_per_segment:
            continue
        targets = _segment_move_targets(
            quotes, segment_no, segment_source_indexes, segment_totals,
            max_chars_per_segment=max_chars_per_segment,
        )
        needed = -(-total // max_chars_per_segment)  # ceil(total / max_chars_per_segment)
        move_hint = (
            f"可将其中若干条挪到第 {targets} 段——这些段目前还有剩余容量；" if targets else ""
        )
        errors.append(
            f"第 {segment_no} 段分到的必保台词 {_segment_overflow_breakdown(quotes)}，超过 15 秒"
            f"口播容量 {max_chars_per_segment} 字。{move_hint}也可以把本段拆成 {needed} 段，多段"
            "引用同一原文段号本就合法（多个段共享一个原文段号是允许的）。此类修复允许并通常需要"
            "新增段落、重排全部 segments[].segment_no，并同步更新 kept_lines 里对应的 "
            "segment_no 引用——这些改动属于修复本身，不受「保持其余已验证字段不变」的限制"
        )
    return errors


def dialogue_ledger_errors(
    *,
    quotes: list[DialogueQuote],
    kept_lines: list[_AiKeptLine],
    dropped_lines: list[_AiDroppedLine],
    segment_source_indexes: dict[int, list[int]],
    max_chars_per_segment: int,
    include_capacity: bool = True,
) -> list[str]:
    """阶段一对白台账的 blocking 校验。

    2.1.2（ERR-20260901-bcfa58/b1c349 两轮真实回归）：容量检查（一段 kept
    台词合计是否超容）不再是模型职责——"怎么重新分配/拆段"是算术+结构重排，
    模型两轮实测都做不好（上一版连 55 对 54 差 1 字都没修出，这一版三次重试
    全部撞在"循环指路"里），改由 normalize_beat_sheet_capacity 确定性归一化
    兜底。include_capacity=False（阶段一 in-loop 用）只跑 partition/binding
    ——这两条仍是语义判断，理应由模型在 semantic retry 里修；归一化后的
    fail-closed 断言用默认 True 复核归一化本身有没有 bug。
    """
    quotes_by_id = {q.quote_id: q for q in quotes}
    errors = _quote_id_partition_errors(quotes, kept_lines, dropped_lines)
    errors.extend(_kept_segment_binding_errors(kept_lines, quotes_by_id, segment_source_indexes))
    if include_capacity:
        errors.extend(_segment_capacity_errors(
            kept_lines, quotes_by_id, segment_source_indexes, max_chars_per_segment=max_chars_per_segment,
        ))
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
    capacity_normalization: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """episode 级完整台账：kept + dropped + 弃置率统计 + 容量归一化遥测。

    落成 ``storyboard_pack_dialogue_ledger`` 类型的 EvidenceArtifact（见
    ``persist_storyboard_pack``），保证「原文有多少台词、丢了哪句、丢了多少
    字、哪段因为容量被机械拆过」事后可查——2.1.2 起 capacity_normalization
    是 ``storyboard_capacity_normalize.normalize_beat_sheet_capacity`` 的
    返回值直接落盘，不另起一套统计。
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
        "capacity_normalization": capacity_normalization or [],
    }


def dialogue_density_by_source_segment(
    quotes: list[DialogueQuote], *, max_chars_per_segment: int,
) -> dict[int, dict[str, int]]:
    """按原文段号统计台词总字数与至少需要的段数（ceil(chars/54)）。

    2.1.2（ERR-20260901-b1c349）：给模型一份数据化的分段依据，让它第一次
    分段就切够，而不是全靠事后的容量归一化兜底——归一化管"切错了怎么机械
    修正"，这个函数管"一开始怎么少犯错"。只从 dialogue_targets 确定性推导，
    没有台词的原文段号不出现在返回值里。
    """
    totals: dict[int, int] = {}
    for quote in quotes:
        totals[quote.source_segment_index] = totals.get(quote.source_segment_index, 0) + quote.content_chars
    return {
        index: {"quote_chars": chars, "min_segments": -(-chars // max_chars_per_segment)}
        for index, chars in totals.items()
    }


def beat_sheet_dialogue_ledger_rules() -> list[str]:
    """阶段一 rules[] 里对白台账的正面陈述，供 storyboard_pack._beat_sheet_rules 拼接。

    第 3、4 条是 ERR-20260901-bcfa58 真实回归补的：模型三次重试都没想到
    "同一原文段号可以给多个段引用"和"拆段重排是预期动作"，因为规则从没正面
    说过。第 5 条是 ERR-20260901-b1c349 二次回归补的：给模型数据化的分段
    依据，第一次就切够，减少事后再靠归一化机械修正的次数。
    """
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
        "多个段可以引用同一原文段号：对白密集的原文段落经常需要拆成多个 15 秒段，"
        "这些段的 source_segment_indexes 出现重叠、都指向同一个原文段号是完全合法"
        "的写法，不是错误。修复台词超容量报错时，新增段落并重排全部 "
        "segments[].segment_no 是预期动作，不是意外，也不受其它字段"
        "「已验证不用改」的惯例限制",
        "dialogue_targets 里少数 quote_id 是原文一句台词过长（超过单段 15 秒容量）"
        "被预先按标点拆成的多条，quote_id 连续编号、内容是同一句话的不同部分；"
        "这些条目如果保留，应尽量分到相邻或同一段落，保持原有先后顺序，不要打乱"
        "语序或分散安排到不相关的段落",
        "dialogue_density_by_source_segment 给出了每个原文段号至少需要的段数"
        "（min_segments=该段号台词总字数 ÷ 15 秒容量、向上取整）：全集分段数不得"
        "低于这些 min_segments 的量级；对白密集的原文段号，直接按它的 "
        "min_segments 切成对应数量的段，不要先按叙事单元切一遍再发现装不下",
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
