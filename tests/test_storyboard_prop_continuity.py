"""app.production.storyboard_prop_continuity -- 起幅道具/衣物状态续接正面陈述
+ continuity_memo.props[].name 的代码核验（P0，2026-10-04，用户反馈《顾念长安》
EP1 插座/插头驱动）。
"""
from __future__ import annotations

from app.production.storyboard_continuity_memo import _AiContinuityMemo, _AiPropState
from app.production.storyboard_prop_continuity import opening_shot_prop_state_rule, prop_name_advisories


def _memo(**overrides) -> _AiContinuityMemo:
    base = dict(time_of_day="白天", time_of_day_basis="inferred")
    base.update(overrides)
    return _AiContinuityMemo(**base)


def _prop(name: str, *, form: str = "", location: str = "", state: str = "") -> _AiPropState:
    return _AiPropState(name=name, form=form, location=location, state=state)


# ---------------------------------------------------------------------------
# opening_shot_prop_state_rule
# ---------------------------------------------------------------------------

def test_no_rule_when_no_previous_memo():
    assert opening_shot_prop_state_rule(None) is None


def test_no_rule_when_previous_memo_has_no_props():
    assert opening_shot_prop_state_rule(_memo(props=[])) is None


def test_rule_names_each_prop_location_and_state_verbatim():
    """真实案例：第 1→2 段插座/插头。"""
    previous = _memo(props=[_prop(
        "插座与插头",
        location="插座在床尾墙根贴近地板处；插头已拔出，在温念右手附近",
        state="已拔下，插座墙根留一缕淡淡焦黑痕迹；屋内顶灯熄灭断电",
    )])
    rule = opening_shot_prop_state_rule(previous)
    assert rule is not None
    assert "插座与插头" in rule
    assert "插座在床尾墙根贴近地板处；插头已拔出，在温念右手附近" in rule
    assert "已拔下，插座墙根留一缕淡淡焦黑痕迹；屋内顶灯熄灭断电" in rule
    assert "不要默认回到这件道具/衣物的常见默认样子" in rule
    assert "previous_continuity_memo.props" in rule


def test_rule_covers_multiple_props_and_drops_fully_blank_ones():
    """``行李箱`` 的 location/state 都留空——没有可续接的信息，不该出现在
    规则里；写一句「（未记录）」会被模型当成要照抄的位置/状态描述原样写进
    画面（与 wardrobe 字段同一纪律：空着才是诚实的，不写说明性文字）。"""
    previous = _memo(props=[_prop("插座与插头", location="墙根", state="已拔下"), _prop("行李箱", location="", state="")])
    rule = opening_shot_prop_state_rule(previous)
    assert "插座与插头" in rule
    assert "行李箱" not in rule
    assert "未记录" not in rule


def test_rule_only_mentions_the_field_actually_recorded():
    """只记了 state 没记 location（或反过来）时，只写那一个字段，不补一句
    「位置=未记录」。"""
    previous = _memo(props=[_prop("插座与插头", location="", state="已拔下")])
    rule = opening_shot_prop_state_rule(previous)
    assert "状态=已拔下" in rule
    assert "位置=" not in rule
    assert "未记录" not in rule


def test_no_rule_when_all_props_are_fully_blank():
    previous = _memo(props=[_prop("行李箱", location="", state="")])
    assert opening_shot_prop_state_rule(previous) is None


# ---------------------------------------------------------------------------
# prop_name_advisories：模型提名、代码核验
# ---------------------------------------------------------------------------

def test_known_prop_label_passes_silently():
    memo = _memo(props=[_prop("深卡其色行李箱")])
    assert prop_name_advisories(memo, {"深卡其色行李箱"}, []) == []


def test_name_found_inside_a_wardrobe_description_passes():
    """围巾/扣子这类衣物状态走 wardrobe 字段，不是独立 resources.props 条目——
    prop.name 能在某个人物当前 wardrobe 描述里逐字找到也算核验通过。"""
    memo = _memo(props=[_prop("围巾")])
    assert prop_name_advisories(memo, set(), ["颈间绕着深灰色围巾"]) == []


def test_unknown_prop_name_is_flagged_but_not_blocking():
    memo = _memo(props=[_prop("插座与插头")])
    advisories = prop_name_advisories(memo, {"深卡其色行李箱"}, ["米白色针织开衫"])
    assert len(advisories) == 1
    assert "[STORYBOARD_PROP_CONTINUITY_NAME_UNKNOWN][未拦截]" in advisories[0]
    assert "插座与插头" in advisories[0]


def test_blank_prop_name_is_not_flagged():
    """空字符串不应该被当成"找不到"而报告——那只是模型没填，不是命名不对。"""
    memo = _memo(props=[_prop("")])
    assert prop_name_advisories(memo, set(), []) == []


def test_empty_wardrobe_strings_do_not_cause_false_positive_match():
    """人物 wardrobe 留空时不能让任意 prop.name 都被判定为"子串命中"（空串是
    一切字符串的子串，必须显式排除，否则核验形同虚设）。"""
    memo = _memo(props=[_prop("插座与插头")])
    advisories = prop_name_advisories(memo, set(), ["", ""])
    assert len(advisories) == 1
