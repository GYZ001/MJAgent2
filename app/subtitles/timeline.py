"""镜内本地 cue 时间 → 整集时间轴（PRD §7）。纯函数，只依赖标准库。

偏移公式取自 ``app/final_edit.py::_compose`` 里 ``cumulative``/``offset`` 的
实际算式（xfade 让后一镜提前 ``duration`` 秒进入，见该函数）：

    offset_i = Σ_{j<i} duration_j − Σ_{j≤i} xfade_before_j

即第 i 段自己的入场转场时长也要算进被减去的部分——已用 3 段手算核对与
``_compose`` 逐帧一致（见 tests/test_subtitles_timeline.py）。``draft_concat``
路径全部 xfade=0，公式退化为纯累加时长。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from app.subtitles.cues import GAP_S, Cue


@dataclass(frozen=True)
class PieceTiming:
    shot_no: int
    duration_s: float
    xfade_before_s: float = 0.0


def _shift_cues(shot_cues: Sequence[Cue], offset: float, cap: float) -> list[Cue]:
    shifted: list[Cue] = []
    for cue in shot_cues:
        start = cue.start_s + offset
        end = min(cue.end_s + offset, cap)
        if end > start:
            shifted.append(replace(cue, start_s=start, end_s=end))
    return shifted


def _trim_cross_piece_overlaps(cues: list[Cue]) -> list[Cue]:
    trimmed = list(cues)
    for i in range(len(trimmed) - 1):
        limit = trimmed[i + 1].start_s - GAP_S
        if trimmed[i].end_s > limit:
            trimmed[i] = replace(trimmed[i], end_s=max(trimmed[i].start_s, limit))
    return [c for c in trimmed if c.end_s > c.start_s]


def place_on_timeline(cues_by_shot: Mapping[int, Sequence[Cue]], pieces: Sequence[PieceTiming]) -> list[Cue]:
    placed: list[Cue] = []
    duration_sum = 0.0
    xfade_sum = 0.0
    for piece in pieces:
        xfade_sum += piece.xfade_before_s
        offset = duration_sum - xfade_sum
        cap = offset + piece.duration_s
        placed.extend(_shift_cues(cues_by_shot.get(piece.shot_no, ()), offset, cap))
        duration_sum += piece.duration_s
    placed.sort(key=lambda c: c.start_s)
    return _trim_cross_piece_overlaps(placed)
