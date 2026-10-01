"""道具外观/参考图只服务画面里看得见的道具（``app.production.storyboard_prop_visibility``，
2026-10-01，第 1 集第五版 35 段真实成片逐帧复查：第 35/8 段被完全遮住的星盘仍被写出完整
外观并送了参考图；第 28 段背景里可见的已登记行李箱没有列进 resources.props，模型因此
现编了另一个外观）。

第二版（同一集第 13/17 段复查）：二分可见/不可见规则对「只露出一截」的分层衣物/道具
（扣好的外套盖住大半的开衫/长裙）仍会诱发同一问题——判"可见"就逐字抄全部标准外观
（含被遮住部位的款式细节），模型据此把外套画成敞开。改成三态：完全可见/部分可见/
完全不可见，部分可见只写露出部分的颜色/花纹/材质，不抄被遮住部位的款式细节。

第三版（第 19/20 段复查）：resources.props 超过参考图张数上限时，``ref_pack_priority``
此前按随机 id 取舍道具，补了一句正面陈述要求模型按显眼程度/跨段一致性重要程度给
resources.props 排序（机制侧的 resources_order 透传见 ``app.video_modes.prop_
references``/``app.multiview`` 测试）。

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


def test_seedance_rule_covers_partial_visibility_without_copying_hidden_details():
    """真实故障：第 13 段「外套5颗扣子全部扣好」仍逐字抄全被盖住的开衫/长裙标准外观
    （含领口形状、腰身剪裁这类只有完全可见才看得到的细节），模型据此把外套画成敞开。
    规则要明确：部分可见时列资源、但只抄露出部分的颜色/花纹/材质，不抄被遮住部位的
    款式细节，哪怕标准外观文案整段都在。"""
    assert "部分可见" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "扣好的外套下摆以下露出的" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "列进本段 resources.props" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "不写被遮住部分的款式与细节" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "领口形状、袖子长短、腰身剪裁、内侧标签" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "不逐字整段抄" in SEEDANCE_PROP_VISIBILITY_RULE


def test_seedance_rule_requires_ordering_resources_props_by_prominence():
    """真实故障：第 20 段 resources.props 列了 8 项超过参考图上限，``ref_pack_
    priority`` 按随机 id 丢弃了行李箱。规则要求模型按显眼程度/跨段一致性给
    resources.props 排序，越重要越靠前，因为排后面的会先被舍弃。"""
    assert "resources.props 的列出顺序也有意义" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "从高到低排列" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "排在后面的道具会最先被舍弃" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "不按道具名字或类型排序" in SEEDANCE_PROP_VISIBILITY_RULE


def test_h3_rule_covers_all_three_visibility_states():
    assert "Fully visible" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "Partially visible" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "Not visible at all" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "only appears in the background" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "resources.props" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "fully covered by clothing" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "even if the asset library has a standard-appearance card" in MINIMAX_H3_PROP_VISIBILITY_RULE


def test_h3_rule_partial_visibility_forbids_covered_portion_details():
    assert "a dress hem peeking out below a buttoned coat" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "do not write the covered portion's cut or detail" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "never the full paragraph verbatim" in MINIMAX_H3_PROP_VISIBILITY_RULE


def test_h3_rule_requires_ordering_resources_props_by_prominence():
    assert "The order items appear in resources.props matters too" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "a hard cap" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "never by name or type" in MINIMAX_H3_PROP_VISIBILITY_RULE


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
