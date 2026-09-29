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

# 2026-09-28：局部窗口重试（插话不再被同镜头其它行的全局单调匹配挤掉）+
# partial 状态（未达 aligned 阈值但确有命中的行保留真实时间戳，见
# ``_line_status``/``_line_result``）。缓存键纳入这个版本号，算法一变旧缓存
# 自动失效重算，不会悄悄复用旧算法产出（见 app.subtitles.store）。
#
# v2（同日）：``_retry_unmatched_lines`` 的候选池改成按行逐次收窄——v1 版本
# 里同一镜头内多条待重试的行共享同一份循环外算一次、从不收窄的候选池，会
# 出现两条行各自独立匹配到同一段残留音轨、都判成 aligned 却在下游重叠裁剪
# 里静默丢一条字幕（实测复现，见 tests 里的 double-claim 用例）。用 v1 缓存
# 键跑过的镜头可能命中过这个 bug，必须重新计算，不能继续复用。
ALGO_VERSION = "2026-09-28-local-retry-v2"

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
    status: str  # "aligned" | "partial" | "missing"
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
    """三选一：``aligned``/``partial``/``missing``。``partial`` 是 2026-09-28
    新增——只在**短句**（<=``SHORT_LINE_MAX_CHARS``）没达到 aligned 的严格阈值
    （非连续命中）但确实有 >=1 个字命中时才用，不再整行判 missing 丢弃时间戳：
    那会把「守……印……人……」这类因同音偏差/发声不完整触发严格阈值、却有真实
    音轨证据的台词连人带时间一起吞掉。短句字数少，一个真实命中就是有分量的
    证据；长句（ratio 分支）不适用同样的放宽——几个字外的一次同音巧合命中
    对一整句「压根没念」的台词而言只是噪声，不构成"部分命中"的证据，仍判
    missing（真正 0 命中的场景同样是 missing，没有任何锚点可用）。"""
    if n <= SHORT_LINE_TINY_MAX_CHARS:
        if matched_chars == n:
            return "aligned", ""
        return ("partial" if matched_chars > 0 else "missing"), "short_line_partial"
    if n <= SHORT_LINE_MAX_CHARS:
        if _max_contig_run(local_flags) >= SHORT_LINE_MIN_RUN:
            return "aligned", ""
        return ("partial" if matched_chars > 0 else "missing"), "short_line_partial"
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


def _line_result(line: LineSpec, n: int, local_time: dict[int, float], local_exact: dict[int, bool]) -> LineAlignment:
    """给定单行已经算好的（该行内部 0..n-1 偏移的）命中字典，产出这一行最终的
    ``LineAlignment``（aligned/partial/missing 三选一）；供全局匹配的按 span
    切片与局部窗口重试（``_retry_line_in_window``）共用同一份判定逻辑，不能
    各写一份、判据跑偏。"""
    if n == 0:
        return _missing_line(line, "not_found")
    local_flags = [i in local_time for i in range(n)]
    matched_chars = len(local_time)
    ratio = matched_chars / n
    status, reason = _line_status(n, matched_chars, ratio, local_flags)
    if status == "missing":
        return _missing_line(line, reason, ratio, matched_chars)
    exact_chars = sum(1 for i in range(n) if local_exact.get(i))
    chars = normalize_chars(line.text)
    positions = _char_times_for_line(local_time, n)
    char_times = tuple(CharTime(char=chars[i], start_s=t, matched=m) for i, (t, m) in enumerate(positions))
    tail = _line_tail(local_time)
    return LineAlignment(
        utterance_id=line.utterance_id, text=line.text, status=status,
        match_ratio=ratio, matched_chars=matched_chars, exact_chars=exact_chars, total_chars=n,
        char_times=char_times, start_s=char_times[0].start_s, end_s=char_times[-1].start_s + tail,
        reason=reason, speaker=line.speaker, delivery_kind=line.delivery_kind,
    )


def _build_line_alignment(
    line: LineSpec, span: tuple[int, int],
    hit_time: dict[int, float], hit_exact: dict[int, bool],
) -> LineAlignment:
    start, end = span
    n = end - start
    local_time = {k - start: hit_time[k] for k in range(start, end) if k in hit_time}
    local_exact = {k - start: hit_exact[k] for k in range(start, end) if k in hit_time}
    return _line_result(line, n, local_time, local_exact)


def _retry_line_in_window(
    line: LineSpec, residual_pairs: list[tuple[str, float]], residual_indices: list[int],
) -> tuple[LineAlignment, frozenset[int]] | None:
    """插话/短句被同镜头其它行的全局单调匹配挤掉时的局部补救：全局匹配后没被
    任何行认领的剩余 ASR 字符（``residual_pairs``，保持原始时间顺序）往往正是
    这一行自己真正的音轨——具体机制见模块文档「插话被挤掉」：台账顺序（先说
    完整句、插话另起一行）与实际语序（说到一半被插话打断、之后接着说完）不
    一致时，插话的已知文字段和它真正的音轨会被拆进两个互不相交的递归子
    问题，全局单调匹配天然找不到，但插话的真实字符仍然原样留在「没人认领」
    的剩余音轨里。只把这一行自己的字与剩余音轨重新做一次拼音匹配，不再和
    整镜头其它已认领的文字竞争；``residual_indices`` 把 ``residual_pairs`` 的
    下标映回 ``asr_pairs`` 的原始下标，供调用方把新认领的字符从「多余语音」
    候选里摘掉（否则会在 extra_speech 里被重复计一遍）。调用方
    （``_retry_unmatched_lines``）只在原始结果不是 aligned 时才调用，且只在
    新结果命中数严格更多才采用，不会让任何行变差。"""
    chars = normalize_chars(line.text)
    if not chars or not residual_pairs:
        return None
    line_pinyin = _pinyin_units(chars)
    residual_pinyin = _pinyin_units([c for c, _t in residual_pairs])
    blocks = SequenceMatcher(None, line_pinyin, residual_pinyin, autojunk=False).get_matching_blocks()
    local_time: dict[int, float] = {}
    local_exact: dict[int, bool] = {}
    claimed: set[int] = set()
    for block in blocks:
        for k in range(block.size):
            line_idx, w_idx = block.a + k, block.b + k
            asr_char, asr_time = residual_pairs[w_idx]
            local_time[line_idx] = asr_time
            local_exact[line_idx] = chars[line_idx] == asr_char
            claimed.add(residual_indices[w_idx])
    return _line_result(line, len(chars), local_time, local_exact), frozenset(claimed)


def _retry_unmatched_lines(
    lines: Sequence[LineSpec], alignments: tuple[LineAlignment, ...],
    asr_pairs: list[tuple[str, float]], asr_matched: list[bool],
) -> tuple[tuple[LineAlignment, ...], list[bool]]:
    """全局单调匹配把插话/短句挤掉时的局部重试：候选音轨是全局匹配后剩下没被
    任何行认领的 ASR 字符（``asr_matched`` 里 False 的那些，保持时间顺序，见
    ``_retry_line_in_window``）。只处理未完全对齐的行，只在新结果命中数严格
    更多时才替换，不会让任何行变差；被新认领的字符同步在返回的 ``asr_matched``
    副本里标记，调用方据此重算 extra_speech，避免同一段字符被计两遍。

    候选池按行**逐次收窄**（每次循环都从最新的 ``updated_matched`` 重新算未认领
    下标，不是循环外算一次不变）：一镜内同时需要重试的多条行会互斥分配残留
    音轨，避免两条行各自独立匹配到同一段字符、都拿到相同的真实时间戳却互不
    知情——那会让其中一条在 ``cues.py::_resolve_overlaps`` 的重叠裁剪里被完全
    裁掉，而它的 ``status`` 仍然是 ``aligned``（不进 ``missing``/``estimated``），
    字幕消失却没有任何可见信号（2026-09-28 实测复现，CLAUDE.md「缺失要有可见
    信号」）。台账里排在前面的行按顺序优先认领，这与模块其它地方「按台账顺序」
    的既有假设一致。"""
    result = list(alignments)
    updated_matched = list(asr_matched)
    for i, la in enumerate(result):
        if la.status == "aligned":
            continue
        residual_indices = [idx for idx, matched in enumerate(updated_matched) if not matched]
        residual_pairs = [asr_pairs[idx] for idx in residual_indices]
        retried = _retry_line_in_window(lines[i], residual_pairs, residual_indices)
        if retried is None:
            continue
        candidate, claimed_indices = retried
        if candidate.matched_chars > la.matched_chars:
            result[i] = candidate
            for idx in claimed_indices:
                updated_matched[idx] = True
    return tuple(result), updated_matched


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
    line_alignments, asr_matched = _retry_unmatched_lines(lines, line_alignments, asr_pairs, asr_matched)
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
