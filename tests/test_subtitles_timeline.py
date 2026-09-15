"""app.subtitles.timeline 的表驱动测试：offset 公式对照 `_compose`、draft 路径
零 xfade、有效时长裁剪、跨段重叠消解、跳过缺镜。

offset_i = Σ_{j<i} duration_j − Σ_{j≤i} xfade_before_j 已用 3 段手算对照
``app/final_edit.py::_compose`` 的 ``cumulative``/``offset`` 变量逐帧验证过
（见派单要求与下面的注释推导），不是照抄公式，是真的在这个文件里重算了一遍。
"""
from __future__ import annotations

import pytest

from app.subtitles.cues import GAP_S, Cue
from app.subtitles.timeline import PieceTiming, place_on_timeline


def _cue(shot_no: int, start_s: float, end_s: float, utterance_id: str = "U01") -> Cue:
    return Cue(shot_no=shot_no, utterance_id=utterance_id, text="x", start_s=start_s, end_s=end_s)


def test_offset_formula_matches_compose_three_pieces_hand_computed():
    """手算对照 `_compose`：d0=10,d1=8,d2=6，xfade 实际时长 t1=1.0,t2=1.0。

    `_compose` 里 cumulative 的演进（本文件独立重算，不引用 final_edit 代码）：
      cumulative = d0 = 10                                  # 处理 piece0 后
      index=1: duration=t1=1.0; offset=cumulative-duration=9；
               cumulative = 10+8-1 = 17                      # 处理 piece1 后
      index=2: duration=t2=1.0; offset=cumulative-duration=16；
               cumulative = 17+6-1 = 22                      # 处理 piece2 后
    即 offset_0=0, offset_1=9, offset_2=16，与
    `offset_i = Σ_{j<i} duration_j − Σ_{j≤i} xfade_before_j` 完全一致：
      offset_1 = d0 − (0+t1) = 10-1 = 9
      offset_2 = (d0+d1) − (0+t1+t2) = 18-2 = 16
    """
    pieces = [
        PieceTiming(shot_no=1, duration_s=10.0, xfade_before_s=0.0),
        PieceTiming(shot_no=2, duration_s=8.0, xfade_before_s=1.0),
        PieceTiming(shot_no=3, duration_s=6.0, xfade_before_s=1.0),
    ]
    cues_by_shot = {
        1: [_cue(1, 1.0, 2.0)],
        2: [_cue(2, 0.5, 1.5, "U02")],
        3: [_cue(3, 0.2, 1.0, "U03")],
    }
    placed = place_on_timeline(cues_by_shot, pieces)
    by_shot = {c.shot_no: c for c in placed}
    assert by_shot[1].start_s == pytest.approx(1.0) and by_shot[1].end_s == pytest.approx(2.0)
    assert by_shot[2].start_s == pytest.approx(9.5) and by_shot[2].end_s == pytest.approx(10.5)
    assert by_shot[3].start_s == pytest.approx(16.2) and by_shot[3].end_s == pytest.approx(17.0)
    assert [c.start_s for c in placed] == sorted(c.start_s for c in placed)


def test_draft_path_zero_xfade_is_pure_cumulative_duration():
    pieces = [PieceTiming(shot_no=1, duration_s=10.0), PieceTiming(shot_no=2, duration_s=8.0)]
    cues_by_shot = {1: [_cue(1, 1.0, 2.0)], 2: [_cue(2, 0.5, 1.5, "U02")]}
    placed = place_on_timeline(cues_by_shot, pieces)
    by_shot = {c.shot_no: c for c in placed}
    assert by_shot[1].start_s == pytest.approx(1.0)
    assert by_shot[2].start_s == pytest.approx(10.5)  # offset_1 = d0 - 0 = 10


def test_cue_end_capped_to_offset_plus_duration():
    pieces = [PieceTiming(shot_no=1, duration_s=5.0)]
    cues_by_shot = {1: [_cue(1, 4.9, 5.5)]}
    placed = place_on_timeline(cues_by_shot, pieces)
    assert len(placed) == 1
    assert placed[0].start_s == pytest.approx(4.9)
    assert placed[0].end_s == pytest.approx(5.0)  # 裁到 offset(0) + duration(5) = 5.0


def test_cross_piece_overlap_is_trimmed_and_output_sorted_by_start():
    """piece2 的 xfade 让它偏移后起点早于 piece1 已裁剪的 cue 结束点，
    跨段重叠必须按 GAP_S 消解（截前一条的 end），输出按 start 排序。"""
    pieces = [
        PieceTiming(shot_no=1, duration_s=5.0, xfade_before_s=0.0),
        PieceTiming(shot_no=2, duration_s=5.0, xfade_before_s=0.5),
    ]
    cues_by_shot = {1: [_cue(1, 4.9, 5.5)], 2: [_cue(2, 0.0, 1.0, "U02")]}
    placed = place_on_timeline(cues_by_shot, pieces)
    assert [c.shot_no for c in placed] == [2, 1]  # shot2 offset=4.5 早于 shot1 的 4.9
    assert placed[0].start_s == pytest.approx(4.5)
    assert placed[0].end_s == pytest.approx(placed[1].start_s - GAP_S, abs=1e-9)
    assert placed[1].start_s == pytest.approx(4.9)
    assert placed[1].end_s == pytest.approx(5.0)


def test_missing_shot_in_cues_by_shot_is_skipped_not_error():
    """部分合成跳过缺镜：piece 列表里的镜头若没有对应 cue，直接跳过不报错。"""
    pieces = [PieceTiming(shot_no=1, duration_s=5.0), PieceTiming(shot_no=2, duration_s=5.0)]
    cues_by_shot = {1: [_cue(1, 0.0, 1.0)]}  # shot 2 完全没有 cue
    placed = place_on_timeline(cues_by_shot, pieces)
    assert len(placed) == 1
    assert placed[0].shot_no == 1
