"""配乐时间轴的纯计算：把逐段（``shot_no``）曲目提名折叠成连续播放的"run"，
再判定哪些相邻 run 之间要做交叉淡化。纯函数，不碰 ffmpeg/IO，方便独立测试。

折叠：相邻段若提名同一首（或都未提名，落空 = 静默），合并成一个连续 run——
同一首歌不会被反复从头播放，这是"相邻情绪相近的段复用同一首减少切歌"在
时间轴层面的落点（模型只需要按段提名，不需要管"从第几秒继续播"）。

交叉淡化只发生在两个都不是静默、且提名了不同曲目的相邻 run 之间；run 与静默
相邻时用淡入淡出而不是交叉淡化（另一侧没有内容可混）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class MusicRun:
    start_s: float
    duration_s: float
    track_id: str | None  # None = 静默（这一段没有配乐提名）


def build_music_runs(cue_by_shot: dict[int, str], shot_timeline: list[tuple[int, float, float]]) -> list[MusicRun]:
    """``shot_timeline``：按最终时间轴顺序排列的 (shot_no, start_s, duration_s)。"""
    runs: list[MusicRun] = []
    for shot_no, start_s, duration_s in shot_timeline:
        track_id = cue_by_shot.get(shot_no)
        if runs and runs[-1].track_id == track_id:
            runs[-1] = replace(runs[-1], duration_s=runs[-1].duration_s + duration_s)
        else:
            runs.append(MusicRun(start_s=start_s, duration_s=duration_s, track_id=track_id))
    return runs


def is_crossfade_boundary(left: MusicRun, right: MusicRun) -> bool:
    return left.track_id is not None and right.track_id is not None and left.track_id != right.track_id


def edge_fades(runs: list[MusicRun], index: int) -> tuple[bool, bool]:
    """``(fade_in, fade_out)``：run 在不做交叉淡化的一侧（紧邻静默或位于首/尾）
    需要一个 2.5 秒的淡入/淡出，避免音乐突然起止。"""
    run = runs[index]
    if run.track_id is None:
        return False, False
    fade_in = index == 0 or not is_crossfade_boundary(runs[index - 1], run)
    fade_out = index == len(runs) - 1 or not is_crossfade_boundary(run, runs[index + 1])
    return fade_in, fade_out


def tail_extend_s(runs: list[MusicRun], index: int, crossfade_s: float) -> float:
    """本 run 是否要为"与下一个 run 交叉淡化"而多取一段尾巴（见模块 docstring
    的推导：只延伸左侧/前一个 run 的尾部，交叉淡化的右侧不需要延伸，这样折叠
    后整条时间轴总长精确等于各 run 时长之和，不需要额外补偿静默/裁切）。"""
    if index == len(runs) - 1:
        return 0.0
    return crossfade_s if is_crossfade_boundary(runs[index], runs[index + 1]) else 0.0
