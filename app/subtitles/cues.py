"""对齐结果 → 镜内本地字幕条（PRD §6，2026-09-15 按协调方端到端实测修订）。
纯函数，只依赖标准库。

按句读把台词切成小句，相邻小句在时间与长度允许时合并；超出 max_chars 的小句
一律拆成多条单行 cue（不再折两行插 ``\\N``——B 上端到端实测发现无标点长小句
按中点折行会把「赵某」「别人的」这类词拆断，观感不可接受，见
``_pick_split_point`` 文档）。合并/拆分全部在原始（未按倍速换算的）ASR 时间域
完成，最后统一除以 ``rate``、加提前量/尾量、裁到有效时长，再解决相邻 cue 的
重叠与最短时长。只对 ``status == "aligned"`` 的台词出 cue。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.subtitles.align import LineAlignment, ShotAlignment

LEAD_S = 0.10
TAIL_EXTRA_S = 0.30
MIN_CUE_S = 0.7
GAP_S = 0.04
MERGE_GAP_S = 0.6

_DELIM_RE = re.compile(r"[，。！？；：、…]")


@dataclass(frozen=True)
class Cue:
    shot_no: int
    utterance_id: str
    text: str
    start_s: float
    end_s: float
    speaker: str = ""


def _char_timeline(line: LineAlignment) -> list[tuple[str, float]]:
    """给原文（含标点）每个字符配一个时间：alnum 字符取 char_times 的真实时间，
    标点继承前一个已知字符的时间（若标点在句首则继承后一个）。"""
    timeline: list[tuple[str, float | None]] = []
    alnum_idx = 0
    for ch in line.text:
        if ch.isalnum():
            timeline.append((ch, line.char_times[alnum_idx].start_s))
            alnum_idx += 1
        else:
            prev_time = timeline[-1][1] if timeline else None
            timeline.append((ch, prev_time))
    first_time = next((t for _ch, t in timeline if t is not None), 0.0)
    return [(ch, t if t is not None else first_time) for ch, t in timeline]


def _split_clauses(timeline: list[tuple[str, float]]) -> list[tuple[int, int]]:
    """按句读切小句：连续的标点串整体归入前一个小句（不逐字符切），标点保留在
    小句内部；仅整句末尾那一串标点整体被排除在区间外（「效果……」不能只去掉
    一个省略号字符变成「效果…」，必须把这一整串都去掉）。"""
    spans: list[tuple[int, int]] = []
    start = i = 0
    n = len(timeline)
    while i < n:
        if _DELIM_RE.match(timeline[i][0]):
            j = i
            while j < n and _DELIM_RE.match(timeline[j][0]):
                j += 1
            spans.append((start, j))
            start = i = j
        else:
            i += 1
    if start < n:
        spans.append((start, n))
    if spans:
        s, e = spans[-1]
        while e > s and _DELIM_RE.match(timeline[e - 1][0]):
            e -= 1
        spans[-1] = (s, e)
    return [(s, e) for s, e in spans if e > s]


def _merge_spans(
    timeline: list[tuple[str, float]], spans: list[tuple[int, int]], max_chars: int
) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    cur_start, cur_end = spans[0]
    for nxt_start, nxt_end in spans[1:]:
        combined_len = nxt_end - cur_start
        gap = timeline[nxt_start][1] - timeline[cur_end - 1][1]
        if combined_len <= max_chars and gap < MERGE_GAP_S:
            cur_end = nxt_end
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = nxt_start, nxt_end
    merged.append((cur_start, cur_end))
    return merged


def _pick_split_point(timeline: list[tuple[str, float]], start: int, end: int, max_chars: int) -> int:
    """在 [start, end) 里选一个拆点 k，三层收窄候选范围，避免「甚 | 至引起了
    其他三大宗门的干扰，」这类偏到句子一端的拆分（局部噪声凑巧比中段大）：

    1. 可行区间：拆开后两段都 <= max_chars（若整段超过 2*max_chars、这个区间
       为空，退化成左侧贪心窗口 [start+1, start+max_chars]，保证第一段落在
       预算内、余下递归继续拆）。
    2. 中间三分之一：[start+ceil(len/3), start+floor(2*len/3)]——拆点必须落在
       句子中段，不能是首尾少数字的停顿噪声。与 1 取交集；交集为空（可行区间
       本身就很窄）时退回整个可行区间。
    3. 在最终候选区间里选相邻字时间间隔最大的位置——停顿是说话人自己的短语
       边界，是从这一句的真实数据里推出来的，不需要分词库。间隔全相等说明这
       段全是线性插值或匀速念白（没有任何真实停顿信号），此时退回候选区间的
       中点，不假装某个位置更有依据。真实 ASR 时间戳减法会有 1e-13 量级的浮点
       噪声，「并列最大」不能用裸 `>` 比较（会让噪声决定选中哪个、在真实数据
       上复现过一次错误结果）——在 1e-9 容差内并列的候选一律算平局，平局取离
       候选区间中点最近的一个，让选择只依赖真实间隔差异，不依赖浮点表示噪声。
    """
    length = end - start
    feasible_lo = max(start + 1, end - max_chars)
    feasible_hi = min(end - 1, start + max_chars)
    if feasible_lo > feasible_hi:
        feasible_lo, feasible_hi = start + 1, start + max_chars
    mid_third_lo = start + (length + 2) // 3  # ceil(length/3)
    mid_third_hi = start + (length * 2) // 3  # floor(2*length/3)
    lo = max(feasible_lo, mid_third_lo)
    hi = min(feasible_hi, mid_third_hi)
    if lo > hi:
        lo, hi = feasible_lo, feasible_hi
    gaps = [timeline[k][1] - timeline[k - 1][1] for k in range(lo, hi + 1)]
    max_gap = max(gaps)
    if max_gap - min(gaps) < 1e-9:
        return (lo + hi) // 2
    window_mid = (len(gaps) - 1) / 2
    best_index = min(
        (i for i, g in enumerate(gaps) if max_gap - g < 1e-9),
        key=lambda i: abs(i - window_mid),
    )
    return lo + best_index


def _split_by_gap(
    timeline: list[tuple[str, float]], start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    """递归地把 [start, end) 拆成若干段，每段 <= max_chars；不折行，只拆分。"""
    if end - start <= max_chars:
        return [(start, end)]
    k = _pick_split_point(timeline, start, end, max_chars)
    return _split_by_gap(timeline, start, k, max_chars) + _split_by_gap(timeline, k, end, max_chars)


def _emit_pieces(
    timeline: list[tuple[str, float]], span: tuple[int, int], max_chars: int
) -> list[tuple[str, float, float]]:
    """返回 (text, 首字时间, 末字时间) 三元组列表：每条恒为单行、<= max_chars；
    超长的按最大相邻字间隔拆成多条顺序 cue（各自的时间取自真实逐字时间）。"""
    start, end = span
    pieces = []
    for s, e in _split_by_gap(timeline, start, end, max_chars):
        text = "".join(ch for ch, _t in timeline[s:e])
        pieces.append((text, timeline[s][1], timeline[e - 1][1]))
    return pieces


def _line_cue_pieces(line: LineAlignment, max_chars: int) -> list[tuple[str, float, float]]:
    timeline = _char_timeline(line)
    spans = _split_clauses(timeline)
    if not spans:
        return []
    merged = _merge_spans(timeline, spans, max_chars)
    pieces: list[tuple[str, float, float]] = []
    for span in merged:
        pieces.extend(_emit_pieces(timeline, span, max_chars))
    return pieces


def _collect_raw_cues(
    shot_no: int, alignment: ShotAlignment, max_chars_per_line: int
) -> list[tuple[int, str, str, str, float, float]]:
    """(shot_no, utterance_id, speaker, text, 原始首字时间, 原始末字时间+tail)。"""
    raw: list[tuple[int, str, str, str, float, float]] = []
    for line in alignment.lines:
        if line.status != "aligned":
            continue
        tail_raw = line.end_s - line.char_times[-1].start_s
        for text, first_t, last_t in _line_cue_pieces(line, max_chars_per_line):
            raw.append((shot_no, line.utterance_id, line.speaker, text, first_t, last_t + tail_raw))
    return raw


def _scale_cue(raw: tuple[int, str, str, str, float, float], rate: float) -> Cue:
    shot_no, utterance_id, speaker, text, raw_start, raw_end = raw
    start_s = raw_start / rate - LEAD_S
    end_s = raw_end / rate + TAIL_EXTRA_S
    return Cue(
        shot_no=shot_no, utterance_id=utterance_id, text=text, speaker=speaker,
        start_s=start_s, end_s=max(start_s, end_s),
    )


def _clip_cue(cue: Cue, effective_duration_s: float) -> Cue | None:
    start_s = max(0.0, min(cue.start_s, effective_duration_s))
    end_s = max(0.0, min(cue.end_s, effective_duration_s))
    if end_s <= start_s:
        return None
    return replace(cue, start_s=start_s, end_s=end_s)


def _resolve_overlaps(cues: list[Cue], effective_duration_s: float) -> list[Cue]:
    ordered = sorted(cues, key=lambda c: c.start_s)
    for i in range(len(ordered) - 1):
        limit = ordered[i + 1].start_s - GAP_S
        if ordered[i].end_s > limit:
            ordered[i] = replace(ordered[i], end_s=max(ordered[i].start_s, limit))
    for i in range(len(ordered)):
        duration = ordered[i].end_s - ordered[i].start_s
        if duration >= MIN_CUE_S:
            continue
        ceiling = (ordered[i + 1].start_s - GAP_S) if i + 1 < len(ordered) else effective_duration_s
        new_end = min(ordered[i].start_s + MIN_CUE_S, ceiling)
        if new_end > ordered[i].end_s:
            ordered[i] = replace(ordered[i], end_s=new_end)
    return [c for c in ordered if c.end_s > c.start_s]


def build_cues(
    shot_no: int, alignment: ShotAlignment, *, rate: float, effective_duration_s: float, max_chars_per_line: int
) -> list[Cue]:
    raw_cues = _collect_raw_cues(shot_no, alignment, max_chars_per_line)
    scaled = [_scale_cue(c, rate) for c in raw_cues]
    clipped = [c for c in (_clip_cue(x, effective_duration_s) for x in scaled) if c is not None]
    return _resolve_overlaps(clipped, effective_duration_s)
