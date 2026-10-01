"""app.production.storyboard_continuity_advisories：连贯性备忘里「检测到了却只
写日志」的两类缺陷补产物信号——layout 跨段变化（此前只 log.warning，看不见）+
道具位置（location）跨段变化（此前完全不检查）。

真实案例（《顾念长安》第 1 集第三轮分镜，经多代理核查确认，
``/tmp/mjtest/ep1_redo/segments_r3.json``）：第 26 段相对 23-25 段左右站位整体
翻转、``layout_change_source_quote`` 为空，本来命中了 ``layout_change_
advisories`` 却只进了日志，没人看得到；行李箱第 12→13 段 ``location`` 从
「床尾旁」变成「床头一侧墙边」，期间没有任何人碰它，此前完全没有判据覆盖这类
道具位置漂移。
"""
from __future__ import annotations

from types import SimpleNamespace

import app.production.storyboard_continuity_advisories as advisories_mod
from app.production.storyboard_continuity_advisories import (
    continuity_memo_location_advisories,
    prop_location_change_advisories,
    segment_continuity_location_advisories,
)
from app.production.storyboard_continuity_memo import _AiContinuityMemo, _AiPropState


def _memo(*, layout: str = "", props: list[_AiPropState] | None = None) -> _AiContinuityMemo:
    return _AiContinuityMemo(layout=layout, props=props or [])


# ---------------------------------------------------------------------------
# prop_location_change_advisories：道具位置跨段变化（此前完全不检查）
# ---------------------------------------------------------------------------

def test_prop_location_change_without_evidence_is_advised():
    """真实案例：行李箱第 12→13 段 location 从「床尾旁」变成「床头一侧墙边」，
    期间没有任何人碰它——当前契约没有专门的变化依据字段，恒记一条告警。"""
    previous = _memo(props=[_AiPropState(name="行李箱", location="床尾旁")])
    memo = _memo(props=[_AiPropState(name="行李箱", location="床头一侧墙边")])
    advisories = prop_location_change_advisories(memo, previous)
    assert len(advisories) == 1
    assert "[STORYBOARD_CONTINUITY_MEMO_PROP_LOCATION][未拦截]" in advisories[0]
    assert "行李箱" in advisories[0] and "床尾旁" in advisories[0] and "床头一侧墙边" in advisories[0]


def test_prop_location_unchanged_is_silent():
    previous = _memo(props=[_AiPropState(name="行李箱", location="床尾旁")])
    memo = _memo(props=[_AiPropState(name="行李箱", location="床尾旁")])
    assert prop_location_change_advisories(memo, previous) == []


def test_prop_location_without_previous_memo_is_silent():
    memo = _memo(props=[_AiPropState(name="行李箱", location="床头一侧墙边")])
    assert prop_location_change_advisories(memo, None) == []


def test_prop_location_newly_recorded_without_previous_value_is_silent():
    """上一段这件道具根本没有记录位置（首次出现）——不是"变化"，不告警。"""
    previous = _memo(props=[_AiPropState(name="行李箱", location="")])
    memo = _memo(props=[_AiPropState(name="行李箱", location="床头一侧墙边")])
    assert prop_location_change_advisories(memo, previous) == []


def test_prop_location_different_name_is_silent():
    """不同名字的道具互不比对——名字就是比对键。"""
    previous = _memo(props=[_AiPropState(name="行李箱", location="床尾旁")])
    memo = _memo(props=[_AiPropState(name="背包", location="床头一侧墙边")])
    assert prop_location_change_advisories(memo, previous) == []


# ---------------------------------------------------------------------------
# continuity_memo_location_advisories：layout + 道具位置两条合并出口
# ---------------------------------------------------------------------------

def test_layout_change_without_quote_is_advised_with_tag():
    """真实案例：第 26 段相对 23-25 段左右站位整体翻转，没给
    layout_change_source_quote——此前只 log.warning，现在必须是产物信号。"""
    previous = _memo(layout="黄总站在长桌远端，李麦麦坐在近端角落")
    memo = _memo(layout="黄总坐在近端角落，李麦麦站在长桌远端")
    advisories = continuity_memo_location_advisories(memo, previous, "本段原文没有提到任何走动")
    assert any("[STORYBOARD_CONTINUITY_MEMO_LAYOUT][未拦截]" in a for a in advisories)
    assert any("没有给出" in a for a in advisories)


def test_combines_layout_and_prop_location_advisories():
    previous = _memo(layout="甲在左，乙在右", props=[_AiPropState(name="行李箱", location="床尾旁")])
    memo = _memo(layout="甲在右，乙在左", props=[_AiPropState(name="行李箱", location="床头一侧墙边")])
    advisories = continuity_memo_location_advisories(memo, previous, "原文没有写任何移动")
    assert len(advisories) == 2
    assert any("[STORYBOARD_CONTINUITY_MEMO_LAYOUT]" in a for a in advisories)
    assert any("[STORYBOARD_CONTINUITY_MEMO_PROP_LOCATION]" in a for a in advisories)


def test_no_previous_memo_produces_no_advisories():
    memo = _memo(layout="甲在左", props=[_AiPropState(name="行李箱", location="床头一侧墙边")])
    assert continuity_memo_location_advisories(memo, None, "") == []


# ---------------------------------------------------------------------------
# segment_continuity_location_advisories：封装"取上一段备忘+本段原文"两步，
# 供 storyboard_pack 两处调用点各自一行接入
# ---------------------------------------------------------------------------

def test_segment_helper_wires_previous_memo_and_source_text(monkeypatch):
    """纯接线测试：确认它取 ``by_segment_no[plan.segment_no - 1]`` 的备忘、把
    ``segment_source_payload`` 算出的 ``source_text_by_segment`` 转交下去，不在
    这里重复验证 ``segment_source_payload`` 自身的单元切片逻辑（那是
    ``storyboard_segment_ranges`` 自己的测试范围）。"""
    captured: dict[str, object] = {}

    def fake_payload(plan, segments, paratext_indexes):
        captured["plan"] = plan
        captured["segments"] = segments
        captured["paratext_indexes"] = paratext_indexes
        return {"source_text_by_segment": "本段原文占位"}

    monkeypatch.setattr(advisories_mod, "segment_source_payload", fake_payload)

    previous_draft = SimpleNamespace(continuity_memo=_memo(layout="甲在左，乙在右"))
    draft = SimpleNamespace(continuity_memo=_memo(layout="甲在右，乙在左"))
    plan = SimpleNamespace(segment_no=2)
    by_segment_no = {1: previous_draft}

    result = segment_continuity_location_advisories(draft, plan, by_segment_no, [], set())

    assert captured["plan"] is plan
    assert any("[STORYBOARD_CONTINUITY_MEMO_LAYOUT][未拦截]" in a for a in result)


def test_segment_helper_without_previous_segment_is_silent(monkeypatch):
    """本集第一段（``by_segment_no`` 里没有上一段）：没有可比对的备忘，不告警。"""
    monkeypatch.setattr(
        advisories_mod, "segment_source_payload",
        lambda plan, segments, paratext_indexes: {"source_text_by_segment": ""},
    )
    draft = SimpleNamespace(continuity_memo=_memo(layout="甲在右"))
    plan = SimpleNamespace(segment_no=1)
    assert segment_continuity_location_advisories(draft, plan, {}, [], set()) == []
