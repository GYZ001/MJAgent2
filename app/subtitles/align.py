"""台词账本文本 × 本地 ASR 时间戳的逐字对齐（PRD §5，2026-09-14 按协调方同音
等价修订）。纯函数，只依赖标准库 + pypinyin（纯 Python、零传递依赖）。

核心思路：把台词与 ASR 输出各自展开成逐字序列，转成无声调拼音后用
``difflib.SequenceMatcher`` 做一次单调匹配（同音字也算命中，因为 ASR 错字几乎
全是同音替换——「名动」听成「鸣动」、「灵石」听成「零食」），再把命中位置的
真实 ASR 时间戳映射回台词的每个字，缺口用线性插值补齐。
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from pypinyin import Style, lazy_pinyin

MIN_MATCH_RATIO = 0.60
SHORT_LINE_MAX_CHARS = 3
SHORT_LINE_TINY_MAX_CHARS = 2
SHORT_LINE_MIN_RUN = 2
EXTRA_SPEECH_MIN_CHARS = 6
CHAR_TAIL_MAX_S = 0.40
CHAR_TAIL_DEFAULT_S = 0.25

_SPECIAL_TOKEN_RE = re.compile(r"^<\|[^|]*\|>$")
_HAN_RE = re.compile(r"[一-鿿]")


@dataclass(frozen=True)
class AsrToken:
    text: str
    start_s: float


@dataclass(frozen=True)
class LineSpec:
    utterance_id: str
    text: str
    speaker: str = ""
    delivery_kind: str = ""


@dataclass(frozen=True)
class CharTime:
    char: str
    start_s: float
    matched: bool


@dataclass(frozen=True)
class LineAlignment:
    utterance_id: str
    text: str
    status: str  # "aligned" | "missing"
    match_ratio: float
    matched_chars: int  # 含同音命中
    exact_chars: int  # matched_chars 的子集：字面也相同
    total_chars: int
    char_times: tuple[CharTime, ...]
    start_s: float | None
    end_s: float | None
    reason: str  # "" | "not_found" | "no_audio" | "short_line_partial"
    speaker: str
    delivery_kind: str


@dataclass(frozen=True)
class ExtraSpeech:
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class ShotAlignment:
    lines: tuple[LineAlignment, ...]
    extra_speech: tuple[ExtraSpeech, ...]
    asr_text: str


def normalize_chars(text: str) -> list[str]:
    """去标点/空白/各类引号，保留汉字、字母、数字，返回逐字列表。"""
    return [ch for ch in text if ch.isalnum()]


def _pinyin_units(chars: list[str]) -> list[str]:
    """把逐字列表转成与其等长的拼音单元列表（用于同音匹配）。

    连续的汉字合并成一段调用 ``lazy_pinyin``（利用词典做多音字消歧，不逐字
    调用），非汉字字符（字母/数字，拼音转换会把连续非汉字合并成一个 token、
    破坏与原字符 1:1 对应）按原字符本身作为比较单元，不经过拼音转换。
    """
    units: list[str] = []
    i, n = 0, len(chars)
    while i < n:
        if _HAN_RE.match(chars[i]):
            j = i
            while j < n and _HAN_RE.match(chars[j]):
                j += 1
            units.extend(lazy_pinyin("".join(chars[i:j]), style=Style.NORMAL))
            i = j
        else:
            units.append(chars[i].lower())
            i += 1
    return units


def _expand_asr_chars(tokens: Sequence[AsrToken]) -> list[tuple[str, float]]:
    """token 逐字展开，每字继承 token 起始时间；丢弃特殊 token 与标点。"""
    pairs: list[tuple[str, float]] = []
    for token in tokens:
        if _SPECIAL_TOKEN_RE.match(token.text):
            continue
        for ch in normalize_chars(token.text):
            pairs.append((ch, token.start_s))
    return pairs


def _known_chars_and_spans(
    lines: Sequence[LineSpec],
) -> tuple[list[str], list[tuple[int, int]]]:
    known_chars: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for line in lines:
        chars = normalize_chars(line.text)
        known_chars.extend(chars)
        spans.append((cursor, cursor + len(chars)))
        cursor += len(chars)
    return known_chars, spans


def _apply_matching_blocks(
    blocks, known_chars: list[str], asr_pairs: list[tuple[str, float]]
) -> tuple[dict[int, float], dict[int, bool], list[bool]]:
    """把 SequenceMatcher（拼音域）匹配到的区块映射回字符域。"""
    hit_time: dict[int, float] = {}
    hit_exact: dict[int, bool] = {}
    asr_matched = [False] * len(asr_pairs)
    for block in blocks:
        for k in range(block.size):
            known_idx, asr_idx = block.a + k, block.b + k
            asr_char, asr_time = asr_pairs[asr_idx]
            hit_time[known_idx] = asr_time
            hit_exact[known_idx] = known_chars[known_idx] == asr_char
            asr_matched[asr_idx] = True
    return hit_time, hit_exact, asr_matched


def _max_contig_run(flags: list[bool]) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def _line_status(n: int, matched_chars: int, ratio: float, local_flags: list[bool]) -> tuple[str, str]:
    if n <= SHORT_LINE_TINY_MAX_CHARS:
        if matched_chars == n:
            return "aligned", ""
        return "missing", "short_line_partial"
    if n <= SHORT_LINE_MAX_CHARS:
        if _max_contig_run(local_flags) >= SHORT_LINE_MIN_RUN:
            return "aligned", ""
        return "missing", "short_line_partial"
    if ratio >= MIN_MATCH_RATIO:
        return "aligned", ""
    return "missing", "not_found"


def _edge_slope(idxs: list[int], hits: dict[int, float], *, from_start: bool) -> float:
    if len(idxs) < 2:
        return 0.0
    i1, i2 = (idxs[0], idxs[1]) if from_start else (idxs[-2], idxs[-1])
    span = i2 - i1
    return (hits[i2] - hits[i1]) / span if span else 0.0


def _char_times_for_line(hits: dict[int, float], n: int) -> list[tuple[float, bool]]:
    """命中字用真实时间；未命中字在相邻命中字之间线性插值，句首/句尾外推。"""
    result: list[tuple[float, bool]] = [(0.0, False)] * n
    for idx, t in hits.items():
        result[idx] = (t, True)
    idxs = sorted(hits)
    for a, b in zip(idxs, idxs[1:]):
        span = b - a
        for k in range(a + 1, b):
            result[k] = (hits[a] + (hits[b] - hits[a]) * (k - a) / span, False)
    head_slope = _edge_slope(idxs, hits, from_start=True)
    for k in range(idxs[0] - 1, -1, -1):
        result[k] = (result[k + 1][0] - head_slope, False)
    tail_slope = _edge_slope(idxs, hits, from_start=False)
    for k in range(idxs[-1] + 1, n):
        result[k] = (result[k - 1][0] + tail_slope, False)
    return result


def _line_tail(local_hits: dict[int, float]) -> float:
    times = [local_hits[i] for i in sorted(local_hits)]
    if len(times) < 2:
        return CHAR_TAIL_DEFAULT_S
    gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    if not gaps:
        return CHAR_TAIL_DEFAULT_S
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    return min(median, CHAR_TAIL_MAX_S)


def _missing_line(line: LineSpec, reason: str, ratio: float = 0.0, matched: int = 0) -> LineAlignment:
    n = len(normalize_chars(line.text))
    return LineAlignment(
        utterance_id=line.utterance_id, text=line.text, status="missing",
        match_ratio=ratio, matched_chars=matched, exact_chars=0, total_chars=n,
        char_times=(), start_s=None, end_s=None, reason=reason,
        speaker=line.speaker, delivery_kind=line.delivery_kind,
    )


def _build_line_alignment(
    line: LineSpec, span: tuple[int, int],
    hit_time: dict[int, float], hit_exact: dict[int, bool],
) -> LineAlignment:
    start, end = span
    n = end - start
    if n == 0:
        return _missing_line(line, "not_found")
    local_time = {k - start: hit_time[k] for k in range(start, end) if k in hit_time}
    local_flags = [i in local_time for i in range(n)]
    matched_chars = len(local_time)
    ratio = matched_chars / n
    status, reason = _line_status(n, matched_chars, ratio, local_flags)
    if status != "aligned":
        return _missing_line(line, reason, ratio, matched_chars)
    exact_chars = sum(1 for k in range(start, end) if hit_exact.get(k))
    chars = normalize_chars(line.text)
    positions = _char_times_for_line(local_time, n)
    char_times = tuple(CharTime(char=chars[i], start_s=t, matched=m) for i, (t, m) in enumerate(positions))
    tail = _line_tail(local_time)
    return LineAlignment(
        utterance_id=line.utterance_id, text=line.text, status="aligned",
        match_ratio=ratio, matched_chars=matched_chars, exact_chars=exact_chars, total_chars=n,
        char_times=char_times, start_s=char_times[0].start_s, end_s=char_times[-1].start_s + tail,
        reason="", speaker=line.speaker, delivery_kind=line.delivery_kind,
    )


def _extract_extra_speech(asr_pairs: list[tuple[str, float]], asr_matched: list[bool]) -> tuple[ExtraSpeech, ...]:
    runs: list[ExtraSpeech] = []
    i, n = 0, len(asr_pairs)
    while i < n:
        if asr_matched[i]:
            i += 1
            continue
        j = i
        while j < n and not asr_matched[j]:
            j += 1
        if j - i >= EXTRA_SPEECH_MIN_CHARS:
            text = "".join(c for c, _t in asr_pairs[i:j])
            end_s = asr_pairs[j][1] if j < n else asr_pairs[j - 1][1]
            runs.append(ExtraSpeech(text=text, start_s=asr_pairs[i][1], end_s=end_s))
        i = j
    return tuple(runs)


def align_shot(lines: Sequence[LineSpec], tokens: Sequence[AsrToken], *, has_audio: bool = True) -> ShotAlignment:
    asr_pairs = _expand_asr_chars(tokens)
    asr_text = "".join(c for c, _t in asr_pairs)
    if not has_audio:
        return ShotAlignment(
            lines=tuple(_missing_line(line, "no_audio") for line in lines),
            extra_speech=(), asr_text=asr_text,
        )
    known_chars, spans = _known_chars_and_spans(lines)
    asr_chars = [c for c, _t in asr_pairs]
    known_pinyin = _pinyin_units(known_chars)
    asr_pinyin = _pinyin_units(asr_chars)
    blocks = SequenceMatcher(None, known_pinyin, asr_pinyin, autojunk=False).get_matching_blocks()
    hit_time, hit_exact, asr_matched = _apply_matching_blocks(blocks, known_chars, asr_pairs)
    line_alignments = tuple(
        _build_line_alignment(line, span, hit_time, hit_exact) for line, span in zip(lines, spans)
    )
    extra = _extract_extra_speech(asr_pairs, asr_matched)
    return ShotAlignment(lines=line_alignments, extra_speech=extra, asr_text=asr_text)


def _line_to_dict(line: LineAlignment) -> dict:
    return {
        "utterance_id": line.utterance_id, "text": line.text, "status": line.status,
        "match_ratio": line.match_ratio, "matched_chars": line.matched_chars,
        "exact_chars": line.exact_chars, "total_chars": line.total_chars, "reason": line.reason,
        "speaker": line.speaker, "delivery_kind": line.delivery_kind,
        "start_s": line.start_s, "end_s": line.end_s,
        "char_times": [{"char": c.char, "start_s": c.start_s, "matched": c.matched} for c in line.char_times],
    }


def _line_from_dict(d: dict) -> LineAlignment:
    return LineAlignment(
        utterance_id=d["utterance_id"], text=d["text"], status=d["status"],
        match_ratio=d["match_ratio"], matched_chars=d["matched_chars"],
        exact_chars=d["exact_chars"], total_chars=d["total_chars"], reason=d["reason"],
        speaker=d["speaker"], delivery_kind=d["delivery_kind"],
        start_s=d["start_s"], end_s=d["end_s"],
        char_times=tuple(
            CharTime(char=c["char"], start_s=c["start_s"], matched=c["matched"]) for c in d["char_times"]
        ),
    )


def alignment_to_dict(a: ShotAlignment) -> dict:
    return {
        "asr_text": a.asr_text,
        "lines": [_line_to_dict(line) for line in a.lines],
        "extra_speech": [{"text": e.text, "start_s": e.start_s, "end_s": e.end_s} for e in a.extra_speech],
    }


def alignment_from_dict(d: dict) -> ShotAlignment:
    return ShotAlignment(
        asr_text=d["asr_text"],
        lines=tuple(_line_from_dict(x) for x in d["lines"]),
        extra_speech=tuple(
            ExtraSpeech(text=x["text"], start_s=x["start_s"], end_s=x["end_s"]) for x in d["extra_speech"]
        ),
    )
