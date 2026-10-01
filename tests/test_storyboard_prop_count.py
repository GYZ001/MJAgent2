"""道具不得凭空分身（``app.production.storyboard_prop_count``，2026-10-01，第 1 集
「修订本段」验收后第二轮逐帧复查：第 20 段镜头 4 顾屿手里凭空多拉了一只与温念那只
外观一致的行李箱；第 28 段同一画面里行李箱同时出现在床头柜与床尾两处）。

与 ``storyboard_cast_lock`` 同源但处理方式不同：人物数量能从 ``resources.
characters`` 按 identity 去重可靠推出一个确定的 N，道具没有数量字段、一条
``resources.props`` 记录可能本来就代表"一组"（两碗馄饨），强行假设"一条=一件"
会误伤本来就该多件的道具，因此本模块只给规则正面陈述，不生成确定性锁定句——
结构照抄 ``tests/test_storyboard_shot_mandates.py``：按 render_format 选文案，
静态、无条件、不按画风分支；再加接线守卫确认确实被拼进 ``dialect_instructions``。
"""
from __future__ import annotations

import inspect

from app.production.storyboard_prop_count import (
    MINIMAX_H3_PROP_COUNT_RULE,
    SEEDANCE_PROP_COUNT_RULE,
    prop_count_dialect_rule,
)


def test_minimax_h3_render_format_returns_h3_rule():
    rule = prop_count_dialect_rule("minimax_h3_native_fields")
    assert rule == MINIMAX_H3_PROP_COUNT_RULE
    assert SEEDANCE_PROP_COUNT_RULE not in rule


def test_other_render_format_returns_seedance_rule():
    for render_format in ("seedance_2_native_fields", ""):
        rule = prop_count_dialect_rule(render_format)
        assert rule == SEEDANCE_PROP_COUNT_RULE
        assert MINIMAX_H3_PROP_COUNT_RULE not in rule


def test_rules_are_non_empty_strings():
    for rule in (SEEDANCE_PROP_COUNT_RULE, MINIMAX_H3_PROP_COUNT_RULE):
        assert isinstance(rule, str) and rule


def test_seedance_rule_locks_single_instance_to_one_holder() -> None:
    """真实故障：原文只写温念拖箱、顾屿空手，顾屿手里却凭空多出一只同款箱子。
    规则要明确单件道具跟着原文/上一段末镜交代的持有人走，不因为"另一人也在场"
    就顺手多配一件。"""
    rule = SEEDANCE_PROP_COUNT_RULE
    assert "原文只写了一件" in rule
    assert "跟着原文（或上一段末镜）交代的持有人或所在位置走" in rule
    assert "不能因为另一个人也在场" in rule
    assert "不能在两个人手里或两个位置各出现一份" in rule


def test_seedance_rule_does_not_misfire_on_legitimately_plural_props() -> None:
    """不能误伤本来就该多件的道具（两碗馄饨、两个勺子）——规则要正面给出"原文写了
    几件就画几件"，不是一刀切锁成一件。"""
    rule = SEEDANCE_PROP_COUNT_RULE
    assert "两碗馄饨" in rule and "两个勺子" in rule
    assert "原文写了几件" in rule and "就照样画几件" in rule
    assert "不多画也不少画" in rule


def test_h3_rule_covers_single_and_plural_cases() -> None:
    rule = MINIMAX_H3_PROP_COUNT_RULE
    assert "the number of physical copies actually shown on screen must match" in rule
    assert "a second, visually identical copy" in rule
    assert "two bowls of wontons" in rule and "two spoons" in rule
    assert "never more, never fewer" in rule


def test_rule_is_not_gated_by_photographic_style() -> None:
    """与 ``skin_blush`` 不同：道具分身和写实/非写实画风无关，规则不接受
    photographic 参数。"""
    sig = inspect.signature(prop_count_dialect_rule)
    assert list(sig.parameters) == ["render_format"]


def test_dialect_rule_is_concatenated_into_dialect_instructions() -> None:
    """接线守卫：确实被 ``storyboard_segment_chains._task_payload_dialect_instructions``
    无条件拼进 dialect_instructions，与 shot_mandates/prop_visibility 同一先例。"""
    import app.production.storyboard_segment_chains as chains_module

    source = inspect.getsource(chains_module._task_payload_dialect_instructions)
    assert "_prop_count.prop_count_dialect_rule(ctx.profile.render_format)" in source


def test_no_determinate_lock_sentence_is_generated() -> None:
    """与 ``storyboard_cast_lock`` 的刻意不同：本模块只导出规则文案与选择函数，
    没有任何 ``ensure_*_in_prompt``/生成确定性句子的函数——数量从 resources.props
    结构推不出来（没有数量字段，一条记录可能代表一组），强行生成写死数字的句子
    会在"两碗馄饨"这类合法多件道具上出现新的误伤，见模块 docstring。"""
    import app.production.storyboard_prop_count as prop_count_module

    assert not any(name.startswith("ensure_") for name in dir(prop_count_module))
    assert prop_count_module.__all__ == [
        "SEEDANCE_PROP_COUNT_RULE", "MINIMAX_H3_PROP_COUNT_RULE", "prop_count_dialect_rule",
    ]
