"""收尾镜人物可见 + 非生命体拟物材质：按 render_format 选对应文案，不改
``storyboard_dialects.py`` 任何既有常量（app.production.storyboard_shot_mandates）。"""
from __future__ import annotations

from app.production.storyboard_shot_mandates import (
    MINIMAX_H3_ENDING_VISIBILITY_RULE,
    MINIMAX_H3_INANIMATE_MATERIAL_RULE,
    SEEDANCE_ENDING_VISIBILITY_RULE,
    SEEDANCE_INANIMATE_MATERIAL_RULE,
    shot_mandates_dialect_rule,
)


def test_minimax_h3_render_format_returns_h3_rules():
    combined = shot_mandates_dialect_rule("minimax_h3_native_fields")
    assert MINIMAX_H3_ENDING_VISIBILITY_RULE in combined
    assert MINIMAX_H3_INANIMATE_MATERIAL_RULE in combined
    assert SEEDANCE_ENDING_VISIBILITY_RULE not in combined


def test_other_render_format_returns_seedance_rules():
    for render_format in ("seedance_2_native_fields", ""):
        combined = shot_mandates_dialect_rule(render_format)
        assert SEEDANCE_ENDING_VISIBILITY_RULE in combined
        assert SEEDANCE_INANIMATE_MATERIAL_RULE in combined
        assert MINIMAX_H3_ENDING_VISIBILITY_RULE not in combined


def test_rules_are_non_empty_strings():
    for rule in (
        SEEDANCE_ENDING_VISIBILITY_RULE, SEEDANCE_INANIMATE_MATERIAL_RULE,
        MINIMAX_H3_ENDING_VISIBILITY_RULE, MINIMAX_H3_INANIMATE_MATERIAL_RULE,
    ):
        assert isinstance(rule, str) and rule


def test_seedance_ending_rule_requires_position_and_action_and_at_mention():
    """真实故障：第 30 段（全片收尾段）最后一镜大远景里人物完全消失——补的规则要求
    写清位置与动作、并保留 @ 点名，不是只要求景别。"""
    assert "人物必须清晰可见" in SEEDANCE_ENDING_VISIBILITY_RULE
    assert "具体位置" in SEEDANCE_ENDING_VISIBILITY_RULE
    assert "@正名" in SEEDANCE_ENDING_VISIBILITY_RULE
    assert "凭空消失" in SEEDANCE_ENDING_VISIBILITY_RULE


def test_h3_ending_rule_requires_position_and_action_and_at_mention():
    assert "must still show every remaining character clearly" in MINIMAX_H3_ENDING_VISIBILITY_RULE
    assert "concrete position in frame" in MINIMAX_H3_ENDING_VISIBILITY_RULE
    assert "@Name mention" in MINIMAX_H3_ENDING_VISIBILITY_RULE
    assert "vanish from the final Shot" in MINIMAX_H3_ENDING_VISIBILITY_RULE


def test_seedance_material_rule_names_the_paper_crane_incident():
    """真实故障：第 25 段「白色纸鹤」被画成了振翅的真鸟。"""
    assert "纸鹤" in SEEDANCE_INANIMATE_MATERIAL_RULE
    assert "材质" in SEEDANCE_INANIMATE_MATERIAL_RULE
    assert "振翅的活鸟" in SEEDANCE_INANIMATE_MATERIAL_RULE


def test_h3_material_rule_names_the_paper_crane_incident():
    assert "a paper crane" in MINIMAX_H3_INANIMATE_MATERIAL_RULE
    assert "material and how it is made" in MINIMAX_H3_INANIMATE_MATERIAL_RULE
    assert "flapping its wings like a real bird" in MINIMAX_H3_INANIMATE_MATERIAL_RULE


def test_ending_rule_does_not_replace_existing_wide_shot_requirement():
    """规则一是补充（人物是否可见），不是替换（景别本身不变）——既有的「全片收尾段…
    大远景」句子必须原样保留在 storyboard_dialects.py 里。"""
    from app.production.storyboard_dialects import SEEDANCE_DIALECT_INSTRUCTIONS

    assert "若这是全片收尾段，最后一镜必须是大远景或缓慢升起拉远的格局镜" in SEEDANCE_DIALECT_INSTRUCTIONS
