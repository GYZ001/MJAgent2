"""分镜台「分镜正文复核」第十二类判据 ``opening_pose_break``（起幅人物姿态
断档，2026-10-05，真人短剧《顾念长安》第 1 集第 8→9 段实测缺陷驱动——完整
背景见 ``app.production.storyboard_prose_review_rules`` 模块 docstring）。

本文件只覆盖这一类新增判据，不重复 ``tests/test_storyboard_prose_review.py``
已经覆盖的其余十一类与通用机制（丢弃路径、开关、``review_segment_inline``
等）；``previous_quote`` 核验机制与 ``screen_side`` 同一套（逐字核验到上一段
``prompt_text``），用例构造方式照抄该文件里 ``screen_side``/
``repeated_transition_action`` 相关用例。
"""
from __future__ import annotations

from app.domain.storyboard_ops import prop_continuity_minimal_patch as patch
from app.domain.storyboard_ops.prop_continuity_review import VALID_PROSE_REVIEW_KINDS
from app.production import storyboard_prose_review as prose_review
from app.production.storyboard_pack import _AiStoryboardSegmentDraft

_KIND = "opening_pose_break"


def _draft(prompt_text: str) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(prompt_text=prompt_text, shot_count=3, dialogue=[], degraded_capabilities=[])


# ---------------------------------------------------------------------------
# 取值集合：新类必须同时在 _KIND_RULES 与 VALID_PROSE_REVIEW_KINDS 里
# ---------------------------------------------------------------------------

def test_opening_pose_break_is_a_registered_kind():
    assert _KIND in prose_review._KIND_RULES
    assert _KIND in VALID_PROSE_REVIEW_KINDS


def test_opening_pose_break_appears_in_review_rules_text_regardless_of_photographic():
    """姿态/神情接续与写实/非写实画风无关，两种画风都应该收到这条规则（与
    ``prop_duplication``/``prop_state_regression`` 同一取舍）。"""
    text_on = prose_review._review_rules_text(photographic=True, max_shots=4)
    text_off = prose_review._review_rules_text(photographic=False, max_shots=4)
    assert _KIND in text_on and _KIND in text_off


def test_opening_pose_break_requires_previous_quote():
    assert _KIND in prose_review._NEEDS_PREVIOUS_QUOTE


# ---------------------------------------------------------------------------
# 规则文案：必须含「过渡动作」「神情」「上一段末镜」等要素，且写清与
# repeated_transition_action 的区别
# ---------------------------------------------------------------------------

def test_rule_text_contains_required_elements():
    rule_text = prose_review._KIND_RULES[_KIND]
    for keyword in ("过渡动作", "神情", "上一段末镜", "手部位置", "repeated_transition_action"):
        assert keyword in rule_text, f"规则文案缺少关键要素：{keyword}"


# ---------------------------------------------------------------------------
# 代码核验：previous_quote 必须逐字核验到上一段 prompt_text
# ---------------------------------------------------------------------------

def test_survives_code_verification_when_previous_quote_is_verbatim():
    """真实案例形状：上一段末镜她攥拳收在胸前、提高音量喊出一句话；本段起幅
    她却手掌朝上伸在桌面上方，中间没有过渡动作。"""
    previous = _draft(
        "镜头4：温念右手五指慢慢蜷起，攥住自己的外套袖口收回胸前，眉头拧起，"
        "张口，提高音量：『还给我！』"
    )
    draft = _draft(
        "镜头1：起幅接上一段末镜，温念上身前倾，右手掌心朝上伸过桌面，停在半空。"
    )
    violation = prose_review.ProseViolation(
        kind=_KIND,
        quote="温念上身前倾，右手掌心朝上伸过桌面，停在半空",
        previous_quote="温念右手五指慢慢蜷起，攥住自己的外套袖口收回胸前，眉头拧起",
        fix="起幅直接承接攥拳收在胸前、眉头拧起的状态，如需伸手过桌面先写出松开拳头、把手伸过桌面的过渡动作",
    )
    verified = prose_review._verified_violations([violation], segment_no=9, draft=draft, previous_draft=previous)
    assert verified == [violation]


def test_discarded_when_previous_quote_is_not_verbatim_in_previous_segment():
    previous = _draft("镜头4：温念右手五指慢慢蜷起，攥住自己的外套袖口收回胸前。")
    draft = _draft("镜头1：起幅接上一段末镜，温念上身前倾，右手掌心朝上伸过桌面，停在半空。")
    violation = prose_review.ProseViolation(
        kind=_KIND,
        quote="温念上身前倾，右手掌心朝上伸过桌面，停在半空",
        previous_quote="编造的上一段末镜原文，上一段根本没有这句话",
        fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=9, draft=draft, previous_draft=previous)
    assert verified == []


def test_discarded_when_quote_itself_not_verbatim_in_current_segment():
    previous = _draft("镜头4：温念右手五指慢慢蜷起，攥住自己的外套袖口收回胸前。")
    draft = _draft("镜头1：起幅接上一段末镜，温念上身前倾，右手掌心朝上伸过桌面，停在半空。")
    violation = prose_review.ProseViolation(
        kind=_KIND,
        quote="这句话根本不在本段正文里出现过",
        previous_quote="温念右手五指慢慢蜷起，攥住自己的外套袖口收回胸前",
        fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=9, draft=draft, previous_draft=previous)
    assert verified == []


def test_discarded_without_previous_draft():
    """本集第一段没有上一段末镜可比对时，这类违规结构上不可能成立，与
    screen_side/prop_appearance/repeated_transition_action 同一取舍。"""
    draft = _draft("镜头1：起幅接上一段末镜，温念上身前倾，右手掌心朝上伸过桌面，停在半空。")
    violation = prose_review.ProseViolation(
        kind=_KIND,
        quote="温念上身前倾，右手掌心朝上伸过桌面，停在半空",
        previous_quote="温念右手五指慢慢蜷起，攥住自己的外套袖口收回胸前",
        fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=1, draft=draft, previous_draft=None)
    assert verified == []


# ---------------------------------------------------------------------------
# 存量分镜最小修改：新类可局部替换
# ---------------------------------------------------------------------------

def test_opening_pose_break_is_locally_patchable():
    """fix 只需要把起幅那句姿态/神情描述换成承接上一段末镜状态的写法（必要时
    补一句过渡动作），纯文本替换，与 screen_side/prop_state_regression 同判。"""
    assert patch.is_locally_patchable(_KIND) is True


def test_locally_patchable_kinds_still_cover_exactly_kind_rules_with_new_key():
    assert _KIND in patch._LOCALLY_PATCHABLE_KINDS
    assert set(patch._LOCALLY_PATCHABLE_KINDS) == set(prose_review._KIND_RULES)
