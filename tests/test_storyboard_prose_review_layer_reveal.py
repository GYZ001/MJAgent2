"""分镜台「分镜正文复核」第十三类判据 ``layer_reveal_unspecified``（外层滑落未写
里层，2026-10-05，真人短剧《顾念长安》第 1 集第 1 段连续四轮重抽同错驱动——
完整背景见 ``app.production.storyboard_prose_review_rules`` 模块 docstring）。

本文件只覆盖这一类新增判据，不重复 ``tests/test_storyboard_prose_review.py``
已经覆盖的其余十二类与通用机制（丢弃路径、开关、``review_segment_inline``
等）；这一类不要求 ``previous_quote``（同一处描述都在本段 ``prompt_text``
内部，与 ``prop_duplication`` 同一取舍），用例构造方式照抄该文件里
``prop_duplication`` 相关用例。
"""
from __future__ import annotations

from app.domain.storyboard_ops import prop_continuity_minimal_patch as patch
from app.domain.storyboard_ops.prop_continuity_review import VALID_PROSE_REVIEW_KINDS
from app.production import storyboard_prose_review as prose_review
from app.production.storyboard_pack import _AiStoryboardSegmentDraft

_KIND = "layer_reveal_unspecified"


def _draft(prompt_text: str) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(prompt_text=prompt_text, shot_count=3, dialogue=[], degraded_capabilities=[])


# ---------------------------------------------------------------------------
# 取值集合：新类必须同时在 _KIND_RULES 与 VALID_PROSE_REVIEW_KINDS 里
# ---------------------------------------------------------------------------

def test_layer_reveal_unspecified_is_a_registered_kind():
    assert _KIND in prose_review._KIND_RULES
    assert _KIND in VALID_PROSE_REVIEW_KINDS


def test_layer_reveal_unspecified_appears_in_review_rules_text_regardless_of_photographic():
    """衣物层次写法与写实/非写实画风无关，两种画风都应该收到这条规则（与
    ``prop_duplication``/``opening_pose_break`` 同一取舍）。"""
    text_on = prose_review._review_rules_text(photographic=True, max_shots=4)
    text_off = prose_review._review_rules_text(photographic=False, max_shots=4)
    assert _KIND in text_on and _KIND in text_off


def test_layer_reveal_unspecified_does_not_require_previous_quote():
    """同一处滑落/露出描述都在本段 prompt_text 内部，复核模型通读本段正文本身
    即可判断，不需要对照上一段末镜。"""
    assert _KIND not in prose_review._NEEDS_PREVIOUS_QUOTE


def test_layer_reveal_unspecified_not_photographic_only():
    """衣物层次写法与写实/非写实画风无关，不按画风分支。"""
    assert _KIND not in prose_review._PHOTOGRAPHIC_ONLY_KINDS


# ---------------------------------------------------------------------------
# 规则文案：必须含「里层」「露出」「袖」「续接服装」等关键要素
# ---------------------------------------------------------------------------

def test_rule_text_contains_required_elements():
    rule_text = prose_review._KIND_RULES[_KIND]
    for keyword in ("里层", "露出", "袖", "续接服装", "滑落"):
        assert keyword in rule_text, f"规则文案缺少关键要素：{keyword}"


# ---------------------------------------------------------------------------
# 代码核验：quote 必须逐字核验到本段 prompt_text
# ---------------------------------------------------------------------------

def test_survives_code_verification_when_quote_is_verbatim():
    """真实案例形状：米白色针织开衫从一侧肩头滑下半截，露肩的那一刻没有交代
    里层浅蓝碎花长裙在这个部位是短袖还是无袖。"""
    draft = _draft(
        "镜头3：温念笑着后仰，米白色针织开衫从一侧肩头滑下半截。"
        "\n续接服装：@温念 米白色针织开衫，内搭浅蓝色碎花长裙。"
    )
    violation = prose_review.ProseViolation(
        kind=_KIND,
        quote="米白色针织开衫从一侧肩头滑下半截",
        fix="改成：米白色针织开衫从一侧肩头滑下半截，露出里面浅蓝碎花长裙的圆领与短袖袖口，袖口仍盖住肩头",
    )
    verified = prose_review._verified_violations([violation], segment_no=1, draft=draft, previous_draft=None)
    assert verified == [violation]


def test_discarded_when_quote_not_verbatim_in_current_segment():
    draft = _draft("镜头3：温念笑着后仰，米白色针织开衫从一侧肩头滑下半截。")
    violation = prose_review.ProseViolation(
        kind=_KIND,
        quote="这句话根本不在本段正文里出现过",
        fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=1, draft=draft, previous_draft=None)
    assert verified == []


def test_survives_without_previous_draft():
    """不要求 previous_quote，本集第一段（没有上一段）也能成立，与
    screen_side/prop_appearance/repeated_transition_action/opening_pose_break
    这四类跨段判据不同。"""
    draft = _draft("镜头1：米白色开衫半挂在肩头，温念没有去扶。")
    violation = prose_review.ProseViolation(
        kind=_KIND,
        quote="米白色开衫半挂在肩头",
        fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=1, draft=draft, previous_draft=None)
    assert verified == [violation]


# ---------------------------------------------------------------------------
# 存量分镜最小修改：新类可局部替换
# ---------------------------------------------------------------------------

def test_layer_reveal_unspecified_is_locally_patchable():
    """fix 只需要在滑落/敞开/脱下那句里补一小句露出部位的里层衣物样子，纯文本
    替换，与 screen_side/prop_state_regression 同判。"""
    assert patch.is_locally_patchable(_KIND) is True


def test_locally_patchable_kinds_still_cover_exactly_kind_rules_with_new_key():
    assert _KIND in patch._LOCALLY_PATCHABLE_KINDS
    assert set(patch._LOCALLY_PATCHABLE_KINDS) == set(prose_review._KIND_RULES)
