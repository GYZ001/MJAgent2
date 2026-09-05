"""节拍表确定性修补：色温统一、范围裁剪/合并、补洞、不回退——修完必须过原校验。"""
from __future__ import annotations

from app.production.storyboard_beat_sheet import _AiBeatSheetDraft, _AiSegmentPlan
from app.production.storyboard_beat_sheet_repair import repair_beat_sheet_draft
from app.production.storyboard_narrative_arc import palette_scene_consistency_errors
from app.production.storyboard_segment_ranges import segment_unit_range_errors, split_source_units
from tests.test_storyboard_segment_ranges import _SCENE_4_TEXT, _scene_4_segments


def _plan(no, ranges, *, palette="暖黄", index=4):
    return _AiSegmentPlan(
        segment_no=no, synopsis=f"段{no}", source_segment_indexes=[index], palette=palette,
        source_unit_ranges=[{"source_segment_index": index, "from_unit": a, "to_unit": b} for a, b in ranges],
    )


def _draft(plans):
    return _AiBeatSheetDraft(beat_sheet=[{"beat_id": "b1", "summary": "x", "segment_indexes": [4]}], segments=plans)


def test_repair_makes_real_failure_shapes_pass_validation():
    """真实打回形态一次修全：越界（S17>16）、洞（S11 无人覆盖）、回退、多条范围、同场戏色温不同。"""
    assert len(split_source_units(_SCENE_4_TEXT)) == 12
    plans = [
        _plan(1, [(1, 2), (5, 6)], palette="暖黄日光"),   # 同一原文段两条范围
        _plan(2, [(3, 4)], palette="淡青晨光"),            # 同场戏色温不同
        _plan(3, [(6, 9)], palette="暖黄日光"),            # 与段 1 的 S5-6 回退重叠
        _plan(4, [(11, 17)], palette="暖黄日光"),          # 越界 + 与段 3 之间 S10 是洞
    ]
    draft = _draft(plans)
    notes = repair_beat_sheet_draft(draft, _scene_4_segments(), set())
    assert notes, "应有修补记录"
    assert segment_unit_range_errors(draft.segments, _scene_4_segments(), set()) == []
    assert palette_scene_consistency_errors(draft.segments) == []
    assert [s.palette for s in draft.segments] == ["暖黄日光"] * 4
    ranges = [(s.source_unit_ranges[0].from_unit, s.source_unit_ranges[0].to_unit) for s in draft.segments]
    assert ranges[0][0] == 1 and ranges[-1][1] == 12
    covered = set()
    for a, b in ranges:
        covered.update(range(a, b + 1))
    assert covered == set(range(1, 13))


def test_repair_is_noop_on_valid_draft():
    plans = [_plan(1, [(1, 6)]), _plan(2, [(7, 12)])]
    draft = _draft(plans)
    assert repair_beat_sheet_draft(draft, _scene_4_segments(), set()) == []
    assert [(s.source_unit_ranges[0].from_unit, s.source_unit_ranges[0].to_unit) for s in draft.segments] == [(1, 6), (7, 12)]


def test_empty_palette_is_not_filled_in():
    """空 palette 是漏填信号，不兜底沿用（保持校验去报）。"""
    plans = [_plan(1, [(1, 6)], palette="暖黄"), _plan(2, [(7, 12)], palette="")]
    draft = _draft(plans)
    repair_beat_sheet_draft(draft, _scene_4_segments(), set())
    assert draft.segments[1].palette == ""
