"""``app.final_edit_enhance.silence``：静默区间计算是纯函数，判据是"字幕对齐
产出的台词区间的互补集合"，不写死镜号/段号（CLAUDE.md 禁止黑白名单）。
"""
from __future__ import annotations

from app.final_edit_enhance.silence import (
    merged_occupied_intervals, speech_free_windows, window_containing, windows_at_least,
)


def test_merged_occupied_intervals_merges_overlap_and_adjacent() -> None:
    assert merged_occupied_intervals([(0, 5), (3, 8), (10, 12)]) == [(0, 8), (10, 12)]


def test_merged_occupied_intervals_drops_non_positive_length() -> None:
    assert merged_occupied_intervals([(5, 5), (6, 4), (1, 3)]) == [(1, 3)]


def test_merged_occupied_intervals_handles_unordered_input() -> None:
    assert merged_occupied_intervals([(10, 12), (0, 2)]) == [(0, 2), (10, 12)]


def test_speech_free_windows_is_complement_of_occupied() -> None:
    # 台词占了 [2,5) 与 [8,10)，总时长 12 秒：静默应是 [0,2) [5,8) [10,12)。
    windows = speech_free_windows([(2, 5), (8, 10)], 12.0)
    assert windows == [(0.0, 2.0), (5.0, 8.0), (10.0, 12.0)]


def test_speech_free_windows_no_dialogue_is_one_full_window() -> None:
    assert speech_free_windows([], 30.0) == [(0.0, 30.0)]


def test_speech_free_windows_entire_duration_occupied_yields_no_window() -> None:
    assert speech_free_windows([(0, 30)], 30.0) == []


def test_speech_free_windows_non_positive_total_duration_yields_empty() -> None:
    assert speech_free_windows([(0, 5)], 0.0) == []
    assert speech_free_windows([(0, 5)], -1.0) == []


def test_speech_free_windows_clips_spans_to_total_duration() -> None:
    # 台词区间越界（对齐误差）不应产生负的或超界的静默窗口。
    windows = speech_free_windows([(-2, 3), (28, 40)], 30.0)
    assert windows == [(3.0, 28.0)]


def test_windows_at_least_filters_short_windows() -> None:
    windows = [(0.0, 5.0), (10.0, 45.0), (50.0, 52.0)]
    assert windows_at_least(windows, 30.0) == [(10.0, 45.0)]


def test_window_containing_finds_covering_window() -> None:
    windows = [(0.0, 5.0), (10.0, 45.0)]
    assert window_containing(windows, 20.0) == (10.0, 45.0)
    assert window_containing(windows, 7.0) is None
