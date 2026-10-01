"""接线守卫：人数锁定/服装延续/收尾人物可见/拟物材质四条新规则确实接进了
``_generate_all_segment_prompts``，不是写好了函数却没调用（CLAUDE.md「拆包/新增判据必须
配套守卫测试」同一精神——这里守的不是 monkeypatch 盲区，是「定义了但没接线」这一类同样
静默的缺口）。2026-09-28，《顾念长安》第 1 集真实回归驱动。
"""
from __future__ import annotations

import inspect

from app.production import storyboard_pack


def _generate_all_segment_prompts_source() -> str:
    return inspect.getsource(storyboard_pack._generate_all_segment_prompts)


def test_cast_lock_and_wardrobe_backfill_are_called_in_validate_callback():
    source = _generate_all_segment_prompts_source()
    assert "_cast_lock.ensure_cast_lock_in_prompt(value)" in source
    assert "ensure_wardrobe_continuity_in_prompt(value)" in source


def test_shot_mandates_rule_is_concatenated_into_dialect_instructions():
    source = _generate_all_segment_prompts_source()
    assert "_shot_mandates.shot_mandates_dialect_rule(profile.render_format)" in source
    # decisive_action 规则（既有）不能被本次改动顶掉——两条都要拼进同一个字符串。
    assert "_action_beats.decisive_action_dialect_rule(profile.render_format)" in source


def test_resource_prop_model_still_accepts_label_and_description():
    """``_AiResourceProp``（``_AiSegmentResources.props`` 的元素类型）的 label/
    description 两字段形状保持可序列化。2026-10-01 起
    ``ensure_wardrobe_continuity_in_prompt`` 不再登记服装条目，原先为此传入的
    ``prop_factory`` 参数已随之退场——这条测试不再依赖
    那条已退场的路径，只独立验证模型本身的序列化形状没有被这次改动意外破坏。"""
    from app.production.storyboard_pack import _AiResourceProp, _AiSegmentResources

    resources = _AiSegmentResources()
    prop = _AiResourceProp(label="旧行李箱", description="棕色帆布旅行箱")
    resources.props.append(prop)
    assert resources.model_dump(mode="json")["props"] == [{"label": "旧行李箱", "description": "棕色帆布旅行箱"}]
