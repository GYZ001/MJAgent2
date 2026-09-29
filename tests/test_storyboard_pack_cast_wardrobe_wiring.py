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
    assert "ensure_wardrobe_continuity_in_prompt(value, prop_factory=_AiResourceProp)" in source


def test_shot_mandates_rule_is_concatenated_into_dialect_instructions():
    source = _generate_all_segment_prompts_source()
    assert "_shot_mandates.shot_mandates_dialect_rule(profile.render_format)" in source
    # decisive_action 规则（既有）不能被本次改动顶掉——两条都要拼进同一个字符串。
    assert "_action_beats.decisive_action_dialect_rule(profile.render_format)" in source


def test_wardrobe_backfill_reuses_the_same_prop_model_as_resources_props():
    """``prop_factory`` 必须是 ``_AiStoryboardSegmentDraft.resources.props`` 同一个元素
    类型（``_AiResourceProp``），否则回填进去的条目在 ``model_dump(mode="json")`` 时
    会序列化错误——这条断言把「用了正确的类」钉死，不依赖运行一次真实生成。"""
    from app.production.storyboard_pack import _AiResourceProp, _AiSegmentResources

    resources = _AiSegmentResources()
    prop = _AiResourceProp(label="温念的服装", description="红色围巾")
    resources.props.append(prop)
    assert resources.model_dump(mode="json")["props"] == [{"label": "温念的服装", "description": "红色围巾"}]
