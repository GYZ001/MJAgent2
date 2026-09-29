"""``app.final_edit_enhance.music_runs``：配乐时间轴折叠 + 交叉淡化边界判定，
纯函数。核心不变式：折叠只发生在"相邻且同一首"，交叉淡化只发生在"相邻且都
非静默且曲目不同"，``tail_extend_s`` 的推导保证折叠后总时长精确等于各原始
段时长之和（不引入静音补偿或裁切）。
"""
from __future__ import annotations

from app.final_edit_enhance.music_runs import MusicRun, build_music_runs, edge_fades, is_crossfade_boundary, tail_extend_s


def test_build_music_runs_merges_consecutive_same_track() -> None:
    cue_by_shot = {1: "t1", 2: "t1", 3: "t2"}
    timeline = [(1, 0.0, 15.0), (2, 15.0, 15.0), (3, 30.0, 15.0)]
    runs = build_music_runs(cue_by_shot, timeline)
    assert runs == [MusicRun(0.0, 30.0, "t1"), MusicRun(30.0, 15.0, "t2")]


def test_build_music_runs_silence_for_shots_without_cue() -> None:
    timeline = [(1, 0.0, 15.0), (2, 15.0, 15.0)]
    runs = build_music_runs({1: "t1"}, timeline)
    assert [r.track_id for r in runs] == ["t1", None]
    assert runs[1].duration_s == 15.0


def test_build_music_runs_does_not_merge_across_gap_reuse() -> None:
    """同一首歌被非相邻的两段各自提名（中间隔着别的曲目）不应被折叠成一个
    run——折叠只发生在物理相邻的段之间，见模块 docstring。"""
    cue_by_shot = {1: "t1", 2: "t2", 3: "t1"}
    timeline = [(1, 0.0, 15.0), (2, 15.0, 15.0), (3, 30.0, 15.0)]
    runs = build_music_runs(cue_by_shot, timeline)
    assert [(r.track_id, r.duration_s) for r in runs] == [("t1", 15.0), ("t2", 15.0), ("t1", 15.0)]


def test_is_crossfade_boundary_requires_both_non_silence_and_different_track() -> None:
    a, b, c = MusicRun(0, 10, "t1"), MusicRun(10, 10, "t2"), MusicRun(20, 10, None)
    assert is_crossfade_boundary(a, b) is True
    assert is_crossfade_boundary(a, a) is False  # 同一首
    assert is_crossfade_boundary(a, c) is False  # 一侧静默
    assert is_crossfade_boundary(c, c) is False  # 两侧都静默


def test_edge_fades_only_on_non_crossfade_sides() -> None:
    runs = [MusicRun(0, 10, "t1"), MusicRun(10, 10, "t2"), MusicRun(20, 10, None)]
    # run0：首个 run，左侧无交叉淡化 -> fade_in；右侧与 run1 交叉淡化 -> 不 fade_out
    assert edge_fades(runs, 0) == (True, False)
    # run1：左侧交叉淡化 -> 不 fade_in；右侧与静默相邻 -> fade_out
    assert edge_fades(runs, 1) == (False, True)
    # run2：静默不需要任何淡化
    assert edge_fades(runs, 2) == (False, False)


def test_tail_extend_only_when_next_boundary_is_crossfade() -> None:
    runs = [MusicRun(0, 10, "t1"), MusicRun(10, 10, "t2"), MusicRun(20, 10, None)]
    assert tail_extend_s(runs, 0, 2.5) == 2.5  # 与 run1 交叉淡化
    assert tail_extend_s(runs, 1, 2.5) == 0.0  # 与静默相邻，硬接不延伸
    assert tail_extend_s(runs, 2, 2.5) == 0.0  # 最后一个 run 没有下一个


def test_folded_timeline_total_duration_matches_sum_of_runs() -> None:
    """交叉淡化的输出长度公式 len(A)+len(B)-d：只延伸左侧尾部 d，两两相加后
    折叠总长精确等于各原始 run 时长之和——这是 ``tail_extend_s`` 存在的唯一
    理由，用真实数字验证这条不变式，不只是断言函数返回值本身。"""
    runs = [MusicRun(0, 12.0, "t1"), MusicRun(12, 8.0, "t2"), MusicRun(20, 15.0, "t2")]
    crossfade_s = 2.5
    feed_lengths = [r.duration_s + tail_extend_s(runs, i, crossfade_s) for i, r in enumerate(runs)]
    # run1/run2 同曲目相邻——build_music_runs 早已把它们折叠成一个 run，这里
    # 手动构造是为了单独验证 tail_extend/acrossfade 的长度公式，不代表真实
    # 会出现"同曲目也交叉淡化"的场景。
    boundary_is_crossfade = [is_crossfade_boundary(runs[i], runs[i + 1]) for i in range(len(runs) - 1)]
    folded_len = feed_lengths[0]
    for i in range(1, len(runs)):
        folded_len = folded_len + feed_lengths[i] - (crossfade_s if boundary_is_crossfade[i - 1] else 0.0)
    assert abs(folded_len - sum(r.duration_s for r in runs)) < 1e-9
