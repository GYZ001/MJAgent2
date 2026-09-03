"""剧本格式台词抽取：app.production.storyboard_dialogue_ledger 的姊妹模块。

背景（真实故障，2026-09-03，项目「橘座在上」第 1 集，ERR 见调用方 changelog）：
阶段一的台词账本只用引号正则（``_QUOTE_PATTERNS``：“” 「」 『』ASCII""）抽台
词。该集原文是剧本格式，13 句真台词全写成「李麦麦（叹气，os）：我自己都快
养不活了……」这种「说话人（备注）：台词」行，没有引号，一句都没抽到；反而
把动作行里的拟声词和引用词（“嗖”“没有感情的摆件”）当成了台词，下游据此让
猫「说」出「嗖」、真台词却丢了 5 句。

本模块加一条并行判据：一行如果是「行首说话人 + 可选备注 + 冒号 + 台词」，
且说话人（去空白后）能在调用方传入的 ``speaker_names``（本集人物谱正名与
别名）里逐字命中，整行冒号之后的部分算一条台词；台词里嵌着的引号短语是这
句话的一部分，不再单独成条。一个原文段只要出现过 ≥1 条这样的说话人行，就
按剧本格式处理：段内非说话人行（动作、场景、出场人物）里的引号短语不算台
词。判据是「说话人是否在 speaker_names 里逐字命中」这一数据事实，不是关键
词黑白名单——没有任何行首前缀被硬编码排除或收录。

一个原文段里一条说话人行都没有的（普通小说体），逐字复用旧的引号抽取逻辑
——同一份 ``_QUOTE_PATTERNS`` 正则对象与同一份 ``_quote_parts`` 预拆函数，只
是多算一份 [start, end) 偏移，保证在纯小说体输入上与旧函数结果逐字一致（见
``tests/test_storyboard_dialogue_extract.py`` 的对照断言）。

依赖边界：只 import 标准库、app.config（口播容量唯一口径）、
app.spoken_contract（口播字数统计）、app.source_excerpt（SourceSegment）与
同层的 app.production.storyboard_dialogue_ledger（见 app/LAYERS.toml
"app.production" = 4），不依赖 app.production.storyboard_pack 本身。
"""
from __future__ import annotations

import re

from app import config, spoken_contract
from app.production.storyboard_dialogue_ledger import (
    _QUOTE_PATTERNS,
    DialogueQuote,
    _quote_parts,
)
from app.source_excerpt import SourceSegment

# 「说话人（可选备注）：台词」。说话人前缀非贪婪匹配到左括号/冒号为止——是否
# 真是说话人由调用方拿去跟 speaker_names 逐字比对（数据判据，不是正则本身
# 筛选）；备注支持全角（）与半角 ()，冒号支持全角：与半角:。
_SPEAKER_LINE_RE = re.compile(
    r"^(?P<speaker>[^\n（(：:]{1,30}?)\s*(?:[（(](?P<note>[^）)]*)[）)])?\s*[：:]\s*(?P<dialogue>.+)$"
)

#: 单行抽取结果：(speaker, note, text, start_offset, end_offset)，偏移相对
#: 该原文段 segment.text。
_LineHit = tuple[str, str, str, int, int]


def _iter_lines(text: str) -> list[tuple[int, str]]:
    """按 ``\\n`` 切行，返回每行相对 text 的起始偏移与原始行内容（含前导/尾随空白）。"""
    lines: list[tuple[int, str]] = []
    cursor = 0
    for raw_line in text.split("\n"):
        lines.append((cursor, raw_line))
        cursor += len(raw_line) + 1
    return lines


def _strip_span(text: str, start: int) -> tuple[str, int, int]:
    """去首尾空白后的文本及其在原始坐标系里的 [start, end)；全空白返回空串与 -1。"""
    stripped = text.strip()
    if not stripped:
        return "", -1, -1
    lead = len(text) - len(text.lstrip())
    begin = start + lead
    return stripped, begin, begin + len(stripped)


def _speaker_line_match(line: str, speaker_names: set[str]) -> tuple[str, str, re.Match[str]] | None:
    """判定一行是否是「说话人：台词」；说话人必须在 speaker_names 里逐字命中。"""
    match = _SPEAKER_LINE_RE.match(line)
    if match is None:
        return None
    speaker = match.group("speaker").strip()
    if speaker not in speaker_names:
        return None
    note = (match.group("note") or "").strip()
    return speaker, note, match


def _segment_has_speaker_line(segment_text: str, speaker_names: set[str]) -> bool:
    return any(
        _speaker_line_match(line, speaker_names) is not None
        for _offset, line in _iter_lines(segment_text)
    )


def _quote_parts_with_offsets(text: str, base_offset: int, *, max_chars: int) -> list[tuple[str, int, int]]:
    """``_quote_parts`` 预拆后逐条换算回原始坐标系里的 [start, end)。

    ``_split_overlong_quote`` 只按句读切分、逐字符原样保留，不改写用词、不
    颠倒顺序，所以各拆分片段在 text 中按出现顺序、依次 find 即可确定偏移。
    """
    parts = _quote_parts(text, max_chars=max_chars)
    results: list[tuple[str, int, int]] = []
    cursor = 0
    for part in parts:
        idx = text.find(part, cursor)
        if idx < 0:
            idx = cursor
        results.append((part, base_offset + idx, base_offset + idx + len(part)))
        cursor = idx + len(part)
    return results


def _extract_script_segment(segment_text: str, speaker_names: set[str], *, max_chars: int) -> list[_LineHit]:
    """剧本格式段：逐行只取说话人行冒号之后的台词部分。"""
    results: list[_LineHit] = []
    for line_start, line in _iter_lines(segment_text):
        matched = _speaker_line_match(line, speaker_names)
        if matched is None:
            continue
        speaker, note, match = matched
        dialogue_raw = match.group("dialogue")
        stripped, begin, _end = _strip_span(dialogue_raw, line_start + match.start("dialogue"))
        if not stripped:
            continue
        for part, part_start, part_end in _quote_parts_with_offsets(stripped, begin, max_chars=max_chars):
            results.append((speaker, note, part, part_start, part_end))
    return results


def _extract_prose_segment(segment_text: str, *, max_chars: int) -> list[_LineHit]:
    """普通小说体段：逐字复用旧的引号正则 + _quote_parts，只是多算一份偏移。

    排序键沿用旧函数的 ``match.start()``（整个匹配含引号符号的起点），保证
    与旧函数在同一输入上产出完全相同的顺序；真实偏移另算自 ``line`` 分组的
    起点（不含引号符号本身）。
    """
    matches: list[tuple[int, int, str]] = []
    for pattern in _QUOTE_PATTERNS:
        for match in pattern.finditer(segment_text):
            stripped, begin, _end = _strip_span(match.group("line"), match.start("line"))
            if stripped:
                matches.append((match.start(), begin, stripped))
    matches.sort(key=lambda item: item[0])
    results: list[_LineHit] = []
    for _sort_key, begin, stripped in matches:
        for part, part_start, part_end in _quote_parts_with_offsets(stripped, begin, max_chars=max_chars):
            results.append(("", "", part, part_start, part_end))
    return results


def extract_dialogue_targets(
    segments: list[SourceSegment],
    paratext_indexes: set[int],
    *,
    speaker_names: list[str],
) -> list[DialogueQuote]:
    """从非 paratext 原文段确定性抽取全部台词，按出现顺序编号 quote_id。

    段落判据（数据推导，不是关键词黑白名单）：段内出现 ≥1 条「说话人（可选
    备注）：台词」行——说话人能在 speaker_names 里逐字命中——就整段按剧本格
    式处理，段内其余行的引号短语不算台词；否则整段沿用旧的引号抽取逻辑（见
    ``_extract_prose_segment``，与 storyboard_dialogue_ledger.extract_
    dialogue_targets 逐字一致）。单条超过 config.MAX_SPOKEN_CHARS_PER_SHOT
    的仍按旧逻辑 ``_quote_parts`` 预拆，quote_id 连续编号。
    """
    max_chars = config.MAX_SPOKEN_CHARS_PER_SHOT
    names = {name.strip() for name in speaker_names if name and name.strip()}
    quotes: list[DialogueQuote] = []
    counter = 0
    for index, segment in enumerate(segments, start=1):
        if index in paratext_indexes:
            continue
        if names and _segment_has_speaker_line(segment.text, names):
            hits = _extract_script_segment(segment.text, names, max_chars=max_chars)
        else:
            hits = _extract_prose_segment(segment.text, max_chars=max_chars)
        for speaker, note, text, start, end in hits:
            counter += 1
            quotes.append(DialogueQuote(
                quote_id=f"Q{counter:02d}",
                source_segment_index=index,
                text=text,
                content_chars=spoken_contract.content_char_count(text),
                speaker=speaker,
                note=note,
                start_offset=start,
                end_offset=end,
            ))
    return quotes
