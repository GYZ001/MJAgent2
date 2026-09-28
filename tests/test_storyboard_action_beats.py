"""P0-B 决定性动作方言规则：按 render_format 选对应文案，不改
``storyboard_dialects.py`` 任何既有常量。"""
from __future__ import annotations

from app.production.storyboard_action_beats import (
    MINIMAX_H3_DECISIVE_ACTION_RULE,
    SEEDANCE_DECISIVE_ACTION_RULE,
    decisive_action_dialect_rule,
)


def test_minimax_h3_render_format_returns_h3_rule():
    assert decisive_action_dialect_rule("minimax_h3_native_fields") is MINIMAX_H3_DECISIVE_ACTION_RULE


def test_other_render_format_returns_seedance_rule():
    assert decisive_action_dialect_rule("seedance_2_native_fields") is SEEDANCE_DECISIVE_ACTION_RULE
    assert decisive_action_dialect_rule("") is SEEDANCE_DECISIVE_ACTION_RULE


def test_rules_are_non_empty_strings():
    assert isinstance(SEEDANCE_DECISIVE_ACTION_RULE, str) and SEEDANCE_DECISIVE_ACTION_RULE
    assert isinstance(MINIMAX_H3_DECISIVE_ACTION_RULE, str) and MINIMAX_H3_DECISIVE_ACTION_RULE


def test_seedance_rule_mentions_shot_ordering_in_chinese():
    assert "紧跟在触发它的刺激画面" in SEEDANCE_DECISIVE_ACTION_RULE
    assert "不要把刺激和决定动作揉进同一镜头" in SEEDANCE_DECISIVE_ACTION_RULE


def test_minimax_h3_rule_mentions_shot_ordering_in_english():
    assert "immediately following the [Shot N]" in MINIMAX_H3_DECISIVE_ACTION_RULE
    assert "do not fold them into a single [Shot N]" in MINIMAX_H3_DECISIVE_ACTION_RULE
