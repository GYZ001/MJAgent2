"""app.production.storyboard_narrative_arc -- 色温弧线按场戏而不是按段分配。

2026-09-03 真实故障（项目「橘座在上」第 1 集）：分镜台阶段一给每个 15 秒段
自报一个色温方向 palette，阶段二只要本段 palette 与上一段不同就要求写渐变。
模型每段都新拟一个色温，同一间会议室、同一分钟内的 6 段（source_segment_
indexes 全是 [4]，同一原文段=同一场戏）灯光变了四次：冷调灰蓝→冷白惨白→
暖柔亮白→冷白偏黄→冷调灰蓝，观众看到的是灯一直在闪。

本文件覆盖两项修复：
① beat_sheet_narrative_arc_rules() 的色温方向规则文案改为按场戏约束——
   同一 source_segment_indexes 的相邻段 palette 必须逐字相同。
② palette_scene_consistency_errors() 阻断式校验函数：节拍表草稿里同一场戏
   （相邻两段 source_segment_indexes 完全相同）但 palette 不同即报错。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_narrative_arc import (
    beat_sheet_narrative_arc_rules,
    palette_scene_consistency_errors,
    segment_narrative_arc_rules,
)


def _seg(segment_no: int, source_segment_indexes: list[int], palette: str) -> SimpleNamespace:
    return SimpleNamespace(
        segment_no=segment_no, source_segment_indexes=source_segment_indexes, palette=palette,
    )


# ---------------------------------------------------------------------------
# beat_sheet_narrative_arc_rules(): 色温方向规则文案按场戏约束
# ---------------------------------------------------------------------------

def test_beat_sheet_rules_palette_direction_requires_verbatim_match_within_same_scene():
    rules = beat_sheet_narrative_arc_rules()
    palette_rule = next(r for r in rules if "segments[].palette" in r)
    assert "逐字相同" in palette_rule
    assert "source_segment_indexes" in palette_rule
    assert "同一场戏" in palette_rule


def test_beat_sheet_rules_palette_direction_still_flags_direction_and_contrast_phrase():
    """确认原有的两条既有断言点（其它代理/既有测试依赖的关键短语）没有被
    本次改写破坏：色温字样 + segments[].palette 字段名、以及转折拉开差异
    的措辞。"""
    rules = beat_sheet_narrative_arc_rules()
    assert any("色温" in r and "segments[].palette" in r for r in rules)
    assert any("色温方向明显拉开差异" in r for r in rules)


def test_beat_sheet_rules_palette_direction_allows_new_direction_on_scene_change_or_source_time_shift():
    rules = beat_sheet_narrative_arc_rules()
    palette_rule = next(r for r in rules if "segments[].palette" in r)
    assert "不同的" in palette_rule and "原文段落" in palette_rule
    assert "时间推移" in palette_rule


# ---------------------------------------------------------------------------
# palette_scene_consistency_errors(): 节拍表草稿阻断式校验
# ---------------------------------------------------------------------------

def test_palette_scene_consistency_errors_flags_same_scene_different_palette():
    """真实故障复现：相邻两段引用完全相同的原文段落（同一场戏），palette
    却不同——必须报错，文案要点名两段段号与各自的 palette。"""
    segments = [
        _seg(4, [4], "冷调灰蓝"),
        _seg(5, [4], "冷白惨白"),
    ]
    errors = palette_scene_consistency_errors(segments)
    assert len(errors) == 1
    assert "第 4 段" in errors[0] and "第 5 段" in errors[0]
    assert "冷调灰蓝" in errors[0] and "冷白惨白" in errors[0]


def test_palette_scene_consistency_errors_silent_when_same_scene_same_palette():
    segments = [
        _seg(4, [4], "冷调灰蓝"),
        _seg(5, [4], "冷调灰蓝"),
    ]
    assert palette_scene_consistency_errors(segments) == []


def test_palette_scene_consistency_errors_silent_across_different_source_segments():
    """换到不同原文段落（换场）允许色温改变，不检查。"""
    segments = [
        _seg(4, [4], "冷调灰蓝"),
        _seg(5, [5], "夕阳暖金"),
    ]
    assert palette_scene_consistency_errors(segments) == []


def test_palette_scene_consistency_errors_treats_empty_vs_nonempty_as_different():
    """模型漏填 palette 不能被兜底当作"沿用上一段"——空 vs 非空同样算不同，
    必须报错。"""
    segments = [
        _seg(4, [4], "冷调灰蓝"),
        _seg(5, [4], ""),
    ]
    errors = palette_scene_consistency_errors(segments)
    assert len(errors) == 1
    assert "（空）" in errors[0]


def test_palette_scene_consistency_errors_only_checks_adjacent_pairs():
    """真实故障是 6 段连续同场戏，覆盖多段场景：4 个相邻违规对，
    只报相邻的、不跨段比较。"""
    segments = [
        _seg(4, [4], "冷调灰蓝"),
        _seg(5, [4], "冷白惨白"),
        _seg(6, [4], "暖柔亮白"),
        _seg(7, [4], "冷白偏黄"),
        _seg(8, [4], "冷调灰蓝"),
    ]
    errors = palette_scene_consistency_errors(segments)
    assert len(errors) == 4


def test_palette_scene_consistency_errors_reports_the_fix_pointing_at_later_segment():
    segments = [_seg(4, [4], "冷调灰蓝"), _seg(5, [4], "冷白惨白")]
    errors = palette_scene_consistency_errors(segments)
    assert "把第 5 段的 palette 改成与第 4 段逐字相同" in errors[0]


def test_palette_scene_consistency_errors_empty_and_single_segment_lists():
    assert palette_scene_consistency_errors([]) == []
    assert palette_scene_consistency_errors([_seg(1, [1], "冷调灰蓝")]) == []


# ---------------------------------------------------------------------------
# segment_narrative_arc_rules(): 色温延续时禁止假渐变
# ---------------------------------------------------------------------------

def test_segment_narrative_arc_rules_forbids_fake_transition_when_palette_unchanged():
    rules = segment_narrative_arc_rules(palette_current="冷调灰蓝", palette_previous="冷调灰蓝")
    rule = next(r for r in rules if "完全相同" in r and "冷调灰蓝" in r)
    assert "time_of_day_basis" in rule
    assert "inherited" in rule
    assert "渐变" in rule


def test_segment_narrative_arc_rules_silent_on_fake_transition_when_palette_differs():
    rules = segment_narrative_arc_rules(palette_current="夕阳暖金", palette_previous="冷调灰蓝")
    assert not any("完全相同" in r and "time_of_day_basis" in r for r in rules)
