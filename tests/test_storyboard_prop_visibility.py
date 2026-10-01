"""道具外观/参考图只服务画面里看得见的道具（``app.production.storyboard_prop_visibility``，
2026-10-01，第 1 集第五版 35 段真实成片逐帧复查：第 35/8 段被完全遮住的星盘仍被写出完整
外观并送了参考图；第 28 段背景里可见的已登记行李箱没有列进 resources.props，模型因此
现编了另一个外观）。

结构照抄 ``tests/test_storyboard_shot_mandates.py``：按 render_format 选文案，静态、
无条件、不按画风分支；再加一组接线守卫确认确实被拼进 ``dialect_instructions``。
"""
from __future__ import annotations

import inspect

from app.production.storyboard_prop_visibility import (
    MINIMAX_H3_PROP_VISIBILITY_RULE,
    SEEDANCE_PROP_VISIBILITY_RULE,
    prop_visibility_dialect_rule,
)


def test_minimax_h3_render_format_returns_h3_rule():
    rule = prop_visibility_dialect_rule("minimax_h3_native_fields")
    assert rule == MINIMAX_H3_PROP_VISIBILITY_RULE
    assert SEEDANCE_PROP_VISIBILITY_RULE not in rule


def test_other_render_format_returns_seedance_rule():
    for render_format in ("seedance_2_native_fields", ""):
        rule = prop_visibility_dialect_rule(render_format)
        assert rule == SEEDANCE_PROP_VISIBILITY_RULE
        assert MINIMAX_H3_PROP_VISIBILITY_RULE not in rule


def test_rules_are_non_empty_strings():
    for rule in (SEEDANCE_PROP_VISIBILITY_RULE, MINIMAX_H3_PROP_VISIBILITY_RULE):
        assert isinstance(rule, str) and rule


def test_seedance_rule_requires_listing_and_writing_visible_background_props():
    """真实故障：第 28 段背景里可见的行李箱没有列进 resources.props，模型现编了外观。
    规则要明确覆盖「只出现在背景、没有人物与它互动」这种情形。"""
    assert "只出现在背景、没有人物与它互动" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "列进本段 resources.props" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "逐字沿用" in SEEDANCE_PROP_VISIBILITY_RULE or "自定至少三项" in SEEDANCE_PROP_VISIBILITY_RULE


def test_seedance_rule_forbids_writing_appearance_for_hidden_props():
    """真实故障：第 35/8 段星盘被卫衣完全盖住，仍被写出完整标准外观并送了参考图。
    规则要明确：看不见时不写外观、不列资源，哪怕素材库有标准外观卡片或全集锁定。"""
    assert "被衣物/容器/包裹完全遮住" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "不列进本段 resources.props" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "哪怕素材库给它建了标准外观卡片或全集已经锁定过它的外观" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "顶出一个圆形" in SEEDANCE_PROP_VISIBILITY_RULE  # 只写观众能看到的痕迹，不写被遮住的东西本身


def test_h3_rule_covers_both_directions():
    assert "actually visible" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "only appears in the background" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "resources.props" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "fully covered by clothing" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "even if the asset library has a standard-appearance card" in MINIMAX_H3_PROP_VISIBILITY_RULE


def test_rule_is_not_gated_by_photographic_style():
    """与 ``skin_blush`` 不同：遮挡导致的误画和画风无关，规则不接受 photographic 参数。"""
    sig = inspect.signature(prop_visibility_dialect_rule)
    assert list(sig.parameters) == ["render_format"]


def test_dialect_rule_is_concatenated_into_dialect_instructions():
    """接线守卫：确实被 ``storyboard_segment_chains._task_payload_dialect_instructions``
    无条件拼进 dialect_instructions，与 shot_mandates 同一先例（不依赖画风/任何开关）。"""
    import app.production.storyboard_segment_chains as chains_module

    source = inspect.getsource(chains_module._task_payload_dialect_instructions)
    assert "_prop_visibility.prop_visibility_dialect_rule(ctx.profile.render_format)" in source


def test_output_contract_resources_field_states_the_visibility_criterion():
    """CLAUDE.md「模型契约两侧必须对齐」：真实故障第 28 段里，``segment_output_
    contract`` 对 resources 的旧文案「本段实际用到的人物/场景/道具」让模型漏报了只在
    背景可见、没有人物与它互动的行李箱——schema 侧的字段说明要和 dialect_instructions
    里的道具可见性规则指向同一个判据，不能各写各的。"""
    from app.production.storyboard_segment_output import segment_output_contract

    contract = segment_output_contract([1], min_shots=2, max_shots=4)
    assert "只在背景出现、没有人物与它互动" in contract["resources"]
    assert "被遮住/收起/不在画面里的道具不列进 props" in contract["resources"]
