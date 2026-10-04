"""定妆照写实画风生成侧肤色局部色块规则（``app.portraits.portrait_skin_blush``，
2026-10-04）。三层覆盖，结构照抄 ``tests/test_storyboard_skin_blush.py``：

1. 纯函数单测——画风开关、规则文案本身。
2. 接线守卫——确实挂在 ``app.refs.character_visual_style_lock`` 这一个汇聚点。
3. 端到端：写实画风预设命中规则文案，非写实画风预设逐字不含该文案。
"""
from __future__ import annotations

import inspect

from app.portraits import portrait_skin_blush as skin_blush
from app.refs import character_visual_style_lock, portrait_prompt
from app.visual_styles import VISUAL_STYLE_PRESETS

_PHOTOGRAPHIC_PROMPT = next(p.prompt for p in VISUAL_STYLE_PRESETS if p.photographic)
_NON_PHOTOGRAPHIC_PROMPT = next(p.prompt for p in VISUAL_STYLE_PRESETS if not p.photographic)


# ---------------------------------------------------------------------------
# 1. 纯函数单测
# ---------------------------------------------------------------------------


def test_non_photographic_addendum_is_empty_string():
    assert skin_blush.portrait_skin_blush_addendum(photographic=False) == ""


def test_photographic_addendum_contains_rule_with_leading_punctuation():
    text = skin_blush.portrait_skin_blush_addendum(photographic=True)
    assert text.startswith("。")
    assert skin_blush.PORTRAIT_SKIN_BLUSH_RULE in text


def test_rule_is_a_positive_statement_covering_every_color_source():
    """正面陈述：写清整张脸应该是什么样（素颜、各部位同一种均匀肤色、只有光线
    明暗），一句话覆盖所有局部颜色来源，而不是逐个点名要避开的颜色。"""
    rule = skin_blush.PORTRAIT_SKIN_BLUSH_RULE
    assert isinstance(rule, str) and rule
    for keyword in ("素颜", "均匀", "脸颊", "眼皮", "柔和明暗", "神情与气色"):
        assert keyword in rule


def test_image_prompt_texts_do_not_name_the_colors_to_avoid():
    """文生图模型不理解否定：提示词里出现「腮红」「红晕」本身就会提高画出它们的
    概率。生成侧规则、判否后的加强重画措辞、锚点冲突说明都只写应该是什么样。"""
    from app.portraits.portrait_skin_blush_check import _strengthen_prompt
    from app.refs import _PORTRAIT_ANCHOR_STYLE_PRIORITY_NOTE

    texts = (
        skin_blush.PORTRAIT_SKIN_BLUSH_RULE,
        _strengthen_prompt("定妆照"),
        _PORTRAIT_ANCHOR_STYLE_PRIORITY_NOTE,
    )
    for text in texts:
        for word in ("腮红", "红晕", "眼影", "唇彩", "口红", "彩光"):
            assert word not in text, (word, text)


def test_rule_version_is_a_non_empty_string():
    assert isinstance(skin_blush.PORTRAIT_SKIN_BLUSH_RULE_VERSION, str)
    assert skin_blush.PORTRAIT_SKIN_BLUSH_RULE_VERSION


# ---------------------------------------------------------------------------
# 2. 接线守卫
# ---------------------------------------------------------------------------


def test_character_visual_style_lock_calls_addendum():
    source = inspect.getsource(character_visual_style_lock)
    assert "portrait_skin_blush_addendum" in source


# ---------------------------------------------------------------------------
# 3. 端到端：真实画风预设
# ---------------------------------------------------------------------------


def test_photographic_preset_lock_text_includes_rule():
    locked = character_visual_style_lock(_PHOTOGRAPHIC_PROMPT)
    assert skin_blush.PORTRAIT_SKIN_BLUSH_RULE in locked


def test_non_photographic_preset_lock_text_excludes_rule():
    locked = character_visual_style_lock(_NON_PHOTOGRAPHIC_PROMPT)
    assert skin_blush.PORTRAIT_SKIN_BLUSH_RULE not in locked


# ---------------------------------------------------------------------------
# 4. 外观锚点与肤色规则的冲突优先级（2026-10-04 复查：外观锚点常逐字写着
# "唇色淡粉""蜜桃色腮红"，与肤色规则在同一条提示词里字面矛盾）
# ---------------------------------------------------------------------------

_BLUSH_ANCHOR = "二十四岁的年轻女性，唇色淡粉，近乎素颜的淡妆，淡淡的蜜桃色腮红，穿米白色宽松针织开衫。"


def test_photographic_prompt_declares_priority_over_conflicting_anchor_text():
    from app.refs import _PORTRAIT_ANCHOR_STYLE_PRIORITY_NOTE
    prompt = portrait_prompt(_PHOTOGRAPHIC_PROMPT, _BLUSH_ANCHOR)
    assert _PORTRAIT_ANCHOR_STYLE_PRIORITY_NOTE in prompt
    # 优先级声明必须出现在锚点原文（含冲突颜色词）之前，模型按阅读顺序先看到"冲突时
    # 画风优先"，再看到锚点里的"唇色淡粉"——顺序颠倒会让后出现的具体颜色词更可能压过
    # 前面的抽象规则（真实踩坑：原实现规则句在锚点之前但没有这条优先级声明）。
    assert prompt.index(_PORTRAIT_ANCHOR_STYLE_PRIORITY_NOTE) < prompt.index("唇色淡粉")


def test_non_photographic_prompt_has_no_priority_note_and_keeps_anchor_color_words():
    prompt = portrait_prompt(_NON_PHOTOGRAPHIC_PROMPT, _BLUSH_ANCHOR)
    from app.refs import _PORTRAIT_ANCHOR_STYLE_PRIORITY_NOTE
    assert _PORTRAIT_ANCHOR_STYLE_PRIORITY_NOTE not in prompt
    assert "唇色淡粉" in prompt  # 非写实画风不是问题，锚点原文不受影响、不做任何过滤
