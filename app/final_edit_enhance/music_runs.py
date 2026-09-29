"""配乐时间轴的纯计算：把编排计划给出的「换曲点」稀疏映射
（``expand_sparse_cues``）展开成逐段稠密映射，再把逐段（``shot_no``）曲目
提名折叠成连续播放的"run"（``build_music_runs``），最后判定哪些相邻 run
之间要做交叉淡化。纯函数，不碰 ffmpeg/IO，方便独立测试。

展开：模型只在真正换曲的 shot_no 给一条 cue（见
``app.final_edit_enhance.plan_schema.MusicCueDraft``），``expand_sparse_cues``
把它按 shot_no 顺序延伸到下一个换曲点为止；换曲点之前的段落保持缺席（诚实的
静音，不是要延续的音乐）。

折叠：展开后相邻段若是同一首（或都未提名，落空 = 静默），合并成一个连续
run——同一首歌不会被反复从头播放。

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


def expand_sparse_cues(cues: dict[int, str], shot_order: list[int]) -> dict[int, str]:
    """把「换曲点」稀疏映射（只在真正换曲的 shot_no 有条目）按 ``shot_order``
    （本集参与合成的段号，按时间轴顺序排列）展开成逐段稠密映射：从某个换曲点
    开始，同一首曲子沿用到下一个换曲点为止，交给 ``build_music_runs`` 折叠。

    换曲点之前的段（本集片头还没轮到第一条换曲点）保持缺席——``build_music_runs``
    把缺席的段落当静音处理，这是诚实的空白，不是要延续的音乐（见
    ``app.final_edit_enhance.plan_validate.validate_music_sections`` 对
    「首条换曲点不在片头」的处理说明；那里只把这种情况当语义错误触发重试，
    不强行编造一条覆盖片头的换曲点）。
    """
    dense: dict[int, str] = {}
    current: str | None = None
    for shot_no in shot_order:
        if shot_no in cues:
            current = cues[shot_no]
        if current is not None:
            dense[shot_no] = current
    return dense


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
