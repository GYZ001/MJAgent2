"""app.production.storyboard_travel_direction：屏幕行进方向的规则文案 + 提示词回填判据。

2026-09-28 真实回归（《顾念长安（第二版）》EP1 opus5.5 分镜，proj_ca86b15ab7d7/
ep_a3c61162b4ce）：30 段里至少 14 段段尾的「本段行进方向」不是本段的走位，而是
更早段落的原话逐字带过来的——第 3 段「温念自画左（床）向画右（窗下墙角）移动，
之后静止蹲在墙角」被逐字带到第 4-9、14 段的咖啡馆/走廊段，且常与这些段正文自己
写的「本段人物不走动」「全部镜头固定机位」矛盾。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_travel_direction import (
    ensure_travel_direction_in_prompt,
    travel_direction_rule,
)

_BEDROOM_DIRECTION = "温念自画左（床）向画右（窗下墙角）移动，之后静止蹲在墙角"


def _memo(direction: str):
    return SimpleNamespace(travel_direction=direction)


def _draft(prompt_text: str, direction: str, degraded_capabilities: list[str] | None = None):
    return SimpleNamespace(
        prompt_text=prompt_text,
        continuity_memo=_memo(direction),
        degraded_capabilities=list(degraded_capabilities or []),
    )


# ---------------------------------------------------------------------------
# 红绿验证：修复前的逻辑（单参数、无沿用判断）在这份真实回归数据上会怎么错
# ---------------------------------------------------------------------------

def _legacy_ensure_travel_direction_in_prompt(draft) -> list[str]:
    """2026-09-14 版本 ``ensure_travel_direction_in_prompt`` 的逐字函数体副本
    （修复前，只有一个 draft 参数，看不到 previous_memo）：只要本段备忘非「静止」
    且正文没有走向词就无条件回填——这正是本次真实回归的根因，见模块 docstring。
    """
    direction_words = ("画左", "画右", "画近", "画远", "向左", "向右", "自左", "自右", "屏幕左", "屏幕右")
    memo = getattr(draft, "continuity_memo", None)
    direction = str(getattr(memo, "travel_direction", "") or "").strip()
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if not direction or direction == "静止" or not prompt.strip():
        return []
    if any(word in prompt for word in direction_words):
        return []
    draft.prompt_text = prompt.rstrip() + f"\n本段行进方向：{direction}；同行人物保持同一走向，跟拍与切换机位不反向。"
    return []


def test_red_legacy_logic_carries_stale_direction_into_unrelated_segment() -> None:
    """红：修复前的逻辑会把第 3 段卧室的走位钉进第 6 段咖啡馆段，制造矛盾。"""
    segment6_prompt = "镜头1：温念与顾屿隔桌对坐，顾屿把照片推过桌面。\n本段人物不走动，全部镜头固定机位。"
    legacy_draft = _draft(segment6_prompt, _BEDROOM_DIRECTION)
    _legacy_ensure_travel_direction_in_prompt(legacy_draft)
    assert "温念自画左（床）向画右（窗下墙角）移动" in legacy_draft.prompt_text
    assert "本段人物不走动" in legacy_draft.prompt_text  # 与刚回填的走位同段矛盾


def test_green_fixed_logic_does_not_carry_stale_direction() -> None:
    """绿：同样的数据喂给修复后的函数，不回填、正文保持原样，只记告警。"""
    segment6_prompt = "镜头1：温念与顾屿隔桌对坐，顾屿把照片推过桌面。\n本段人物不走动，全部镜头固定机位。"
    draft = _draft(segment6_prompt, _BEDROOM_DIRECTION)
    previous = _memo(_BEDROOM_DIRECTION)
    assert ensure_travel_direction_in_prompt(draft, previous) == []
    assert draft.prompt_text == segment6_prompt
    assert any("STORYBOARD_TRAVEL_DIRECTION_CARRIED_OVER" in note for note in draft.degraded_capabilities)


# ---------------------------------------------------------------------------
# 回填判据：沿用不回填+告警、本段新方向正常回填、静止不回填
# ---------------------------------------------------------------------------

def test_carried_over_verbatim_without_direction_words_is_not_appended() -> None:
    draft = _draft("镜头1：孟浩看向窗外。", "一行人自画左向画右沿山路行进")
    previous = _memo("一行人自画左向画右沿山路行进")
    assert ensure_travel_direction_in_prompt(draft, previous) == []
    assert draft.prompt_text == "镜头1：孟浩看向窗外。"
    assert len(draft.degraded_capabilities) == 1
    assert "一行人自画左向画右沿山路行进" in draft.degraded_capabilities[0]


def test_new_direction_this_segment_is_appended_normally() -> None:
    """本段方向与上一段不同——是本段自己的声明，正常回填（不比对上一段的值）。"""
    draft = _draft("镜头1：孟浩沿广场通道走向出口。", "自画右向画左沿广场通道往出口行进")
    previous = _memo("一行人自画左向画右沿山路行进")
    assert ensure_travel_direction_in_prompt(draft, previous) == []
    assert draft.prompt_text.endswith(
        "本段行进方向：自画右向画左沿广场通道往出口行进；同行人物保持同一走向，跟拍与切换机位不反向。"
    )
    assert draft.degraded_capabilities == []


def test_first_segment_direction_is_appended_normally() -> None:
    """没有上一段（previous_memo=None）时，非静止方向照常回填。"""
    draft = _draft("镜头1：孟浩沿广场通道走向出口。", "自画右向画左沿广场通道往出口行进")
    assert ensure_travel_direction_in_prompt(draft, None) == []
    assert "本段行进方向：自画右向画左沿广场通道往出口行进" in draft.prompt_text


def test_static_direction_is_never_appended() -> None:
    draft = _draft("镜头1：孟浩站定。", "静止")
    previous = _memo("一行人自画左向画右沿山路行进")
    assert ensure_travel_direction_in_prompt(draft, previous) == []
    assert draft.prompt_text == "镜头1：孟浩站定。"
    assert draft.degraded_capabilities == []


def test_prompt_already_stating_stillness_is_not_appended() -> None:
    """本段正文已用「静止」这个规范词自陈时不回填，避免与继承来的走位矛盾。"""
    draft = _draft("镜头1：温念蜷在床上，全程静止。", "温念自画左（床）向画右（窗下墙角）移动")
    previous = _memo("温念自画左（另一处）向画右移动")
    assert ensure_travel_direction_in_prompt(draft, previous) == []
    assert draft.prompt_text == "镜头1：温念蜷在床上，全程静止。"


def test_carried_over_with_unrelated_stillness_mention_still_reports_advisory() -> None:
    """评审确认（2026-09-28）：真实数据里长篇提示词经常出现与本段人物走位无关
    的「静止」提及（风铃、相框……）。沿用检测必须先于这类无关文本的「静止」
    命中，否则会被误判成「本段已自陈静止」而直接放行，沿用告警被静默吞掉。"""
    prompt = "镜头1：两人隔桌对坐。门框上方悬着一串金属风铃，此刻静止不动，无人触碰。"
    draft = _draft(prompt, _BEDROOM_DIRECTION)
    previous = _memo(_BEDROOM_DIRECTION)
    assert ensure_travel_direction_in_prompt(draft, previous) == []
    assert draft.prompt_text == prompt  # 不回填陈旧走位
    assert len(draft.degraded_capabilities) == 1
    assert "STORYBOARD_TRAVEL_DIRECTION_CARRIED_OVER" in draft.degraded_capabilities[0]


def test_prompt_already_has_direction_words_is_not_appended() -> None:
    """既有行为保留：正文已经自己写了走向词就不重复回填。"""
    draft = _draft("镜头1：孟浩自画右向画左走向出口。", "自画右向画左行进")
    assert ensure_travel_direction_in_prompt(draft, None) == []
    assert "本段行进方向" not in draft.prompt_text


# ---------------------------------------------------------------------------
# 规则文案：不提供「默认继承」起点，明确走位是一次性动作
# ---------------------------------------------------------------------------

def test_rule_with_previous_does_not_offer_default_inherit() -> None:
    text = travel_direction_rule("一行人自画左向画右沿山路行进")
    assert "仅供参考" in text and "不是本段的默认值" in text
    assert "只描述本段镜头里真实发生的位移" in text
    assert "不能直接照抄上一段的记录" in text


def test_rule_first_segment_has_no_previous_reference() -> None:
    text = travel_direction_rule("")
    assert "仅供参考" not in text
    assert "没有人物位移" in text and "静止" in text
