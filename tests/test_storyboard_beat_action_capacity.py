"""P0：阶段一（节拍表/分段）段级动作容量——与既有台词容量同一个形状，给
"一场戏要不要拆成多段"补上动作维度。真实证据见 ``app.production.storyboard_
beat_action_capacity`` 模块 docstring（第 1 集重做第二轮分镜逐段核查）。

覆盖范围：
1. ``segment_key_actions_rule``/``segment_action_capacity_errors``：正面规则
   文案、阻断判据（含简化总承载量 8 的边界）。
2. ``SegmentActionCapacitySoftCheck``：重试-降级节奏（与 ``EmotionalTurnSoft
   Check`` 同构）。
3. ``segment_advisories_for_plan``：耗尽重试后的可见信号，独立于 SoftCheck
   内部计数。
4. 未超限：行为不变（不产生任何错误/告警）。
5. ``storyboard_action_density`` 新增两条共享口径——双人合并计数、台词镜上限
   收紧到 1——以及 ``key_action_definition`` 文本本身包含这两条。
6. 规则原文生成侧（``storyboard_beat_sheet._beat_sheet_rules``）与正文复核侧
   （``storyboard_prose_review._review_rules_text``）同源（只读调用，不改
   该模块）。
7. 端到端：真的驱动 ``_generate_beat_sheet`` 组装出的 validate 闭包走完整个
   重试预算，证明接线生效（同 tests/test_storyboard_beat_sheet_soft_check_
   wiring.py 先例）。
8. 漂移告警：本模块/``storyboard_beat_sheet`` 里复制的 ``max_shots=4`` 字面量
   与 ``storyboard_pack.MAX_SHOTS_PER_SEGMENT`` 必须同值。
"""
from __future__ import annotations

import pytest

from app.harness import model_gateway
from app.production.storyboard_action_density import (
    ActionDensitySoftCheck,
    ShotActionBeats,
    action_density_errors,
    dialogue_shot_numbers,
    key_action_definition,
    segment_advisories,
)
from app.production.storyboard_beat_action_capacity import (
    SegmentActionCapacitySoftCheck,
    segment_action_capacity_errors,
    segment_advisories_for_plan,
    segment_key_actions_rule,
)
from app.production.storyboard_beat_sheet import (
    _MAX_SHOTS_PER_SEGMENT,
    _AiBeat,
    _AiBeatSheetDraft,
    _BEAT_SHEET_SEMANTIC_RETRY_LIMIT,
    _beat_sheet_rules,
    _generate_beat_sheet,
)
from app.production.storyboard_beat_sheet_schemas import _AiSegmentPlan
from app.production.storyboard_pack import MAX_SHOTS_PER_SEGMENT
from app.production.storyboard_prose_review import _review_rules_text
from app.source_excerpt import SourceSegment


def _plan(segment_no: int, *key_actions: str) -> _AiSegmentPlan:
    return _AiSegmentPlan(
        segment_no=segment_no, synopsis="x", source_segment_indexes=[1], key_actions=list(key_actions),
    )


# ---------------------------------------------------------------------------
# 漂移告警：本模块复制的字面量必须与真源同值
# ---------------------------------------------------------------------------

def test_local_max_shots_literal_matches_storyboard_pack():
    assert _MAX_SHOTS_PER_SEGMENT == MAX_SHOTS_PER_SEGMENT


# ---------------------------------------------------------------------------
# segment_key_actions_rule：正面陈述
# ---------------------------------------------------------------------------

def test_rule_states_capacity_and_positive_way_out():
    rule = segment_key_actions_rule(max_shots=4)
    assert key_action_definition() in rule
    assert "8" in rule  # 4 * 2
    assert "拆成更多 15 秒段落" in rule
    assert "原文节拍一个都不删" in rule


def test_rule_respects_custom_max_per_shot():
    rule = segment_key_actions_rule(max_shots=4, max_per_shot=3)
    assert "12" in rule  # 4 * 3


# ---------------------------------------------------------------------------
# segment_action_capacity_errors：阻断判据，简化总承载量
# ---------------------------------------------------------------------------

def test_no_errors_when_within_capacity():
    plan = _plan(1, *[f"动作{i}" for i in range(8)])  # 恰好 8 个，不超限
    assert segment_action_capacity_errors([plan], max_shots=4) == []


def test_flags_segment_exceeding_capacity():
    plan = _plan(1, *[f"动作{i}" for i in range(9)])  # 9 > 8
    errors = segment_action_capacity_errors([plan], max_shots=4)
    assert len(errors) == 1
    assert "第 1 段" in errors[0]
    assert "9 个关键动作" in errors[0]
    assert "承载量 8 个" in errors[0]


def test_only_flags_offending_segments():
    under = _plan(1, "a", "b")
    over = _plan(2, *[f"动作{i}" for i in range(9)])
    errors = segment_action_capacity_errors([under, over], max_shots=4)
    assert len(errors) == 1
    assert "第 2 段" in errors[0]


def test_empty_key_actions_produces_no_errors():
    """旧存量 beat_draft（storyboard_identity_regenerate._existing_plan 重建）
    不产出 key_actions 字段，默认空列表——不能被这条判据误报。"""
    plan = _plan(1)
    assert segment_action_capacity_errors([plan], max_shots=4) == []


# ---------------------------------------------------------------------------
# SegmentActionCapacitySoftCheck：重试-降级节奏
# ---------------------------------------------------------------------------

def test_soft_check_blocks_first_attempts_then_degrades():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_plan(1, *[f"动作{i}" for i in range(9)])],
    )
    check = SegmentActionCapacitySoftCheck(retry_limit=2, max_shots=4)
    assert check.errors(draft) != [], "第 1 次（attempt 0）应打回"
    assert check.errors(draft) != [], "第 2 次（attempt 1）应打回"
    assert check.errors(draft) == [], "第 3 次（attempt 2 == retry_limit）应降级为放行"


def test_soft_check_never_errors_when_within_capacity():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_plan(1, "a", "b")],
    )
    check = SegmentActionCapacitySoftCheck(retry_limit=2, max_shots=4)
    assert check.errors(draft) == []
    assert check.errors(draft) == []
    assert check.errors(draft) == []


# ---------------------------------------------------------------------------
# segment_advisories_for_plan：耗尽重试后的可见信号，独立重算
# ---------------------------------------------------------------------------

def test_advisories_empty_when_within_capacity():
    assert segment_advisories_for_plan(_plan(1, "a", "b"), max_shots=4) == []


def test_advisories_flag_over_capacity_with_visible_tag():
    plan = _plan(1, *[f"动作{i}" for i in range(9)])
    advisories = segment_advisories_for_plan(plan, max_shots=4)
    assert len(advisories) == 1
    assert "STORYBOARD_PACK_SEGMENT_ACTION_CAPACITY" in advisories[0]
    assert "未拦截" in advisories[0]


def test_advisories_recompute_independent_of_soft_check_attempt_count():
    plan = _plan(1, *[f"动作{i}" for i in range(9)])
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])], segments=[plan],
    )
    check = SegmentActionCapacitySoftCheck(retry_limit=1, max_shots=4)
    check.errors(draft)  # attempt 0，仍打回
    assert check.errors(draft) == []  # attempt 1 == retry_limit，放行
    assert segment_advisories_for_plan(plan, max_shots=4) != []


# ---------------------------------------------------------------------------
# storyboard_action_density 共享口径新增两条：双人合并计数、台词镜上限 1
# ---------------------------------------------------------------------------

def test_key_action_definition_mentions_merge_and_dialogue_shot_cap():
    definition = key_action_definition()
    assert "合并计入" in definition
    assert "{{speech:Uxx}}" in definition and "最多算 1 个关键动作" in definition


def test_action_density_merges_multiple_people_reported_separately():
    """同一镜两条申报（按人物分开报），各自 2 个在限额内，合并后 4 个超限。"""
    beats = [
        ShotActionBeats(shot_no=1, key_actions=["甲起身", "甲开门"]),
        ShotActionBeats(shot_no=1, key_actions=["乙起身", "乙开门"]),
    ]
    errors = action_density_errors(beats)
    assert len(errors) == 1
    assert "镜头 1" in errors[0]
    assert "4 个关键动作" in errors[0]


def test_action_density_single_entry_unaffected_by_merge():
    beats = [ShotActionBeats(shot_no=1, key_actions=["推门", "开灯"])]
    assert action_density_errors(beats) == []


def test_action_density_dialogue_shot_capped_at_one():
    beats = [ShotActionBeats(shot_no=1, key_actions=["推门", "开灯"])]
    assert action_density_errors(beats) == [], "非台词镜，2 个在默认上限 2 以内"
    errors = action_density_errors(beats, dialogue_shot_nos=frozenset({1}))
    assert len(errors) == 1
    assert "镜头 1" in errors[0]
    assert "每镜 1 个的上限" in errors[0]
    assert "台词" in errors[0]


def test_dialogue_shot_numbers_detects_speech_placeholder():
    prompt_text = "镜头1：她推门进来。{{speech:U01}}（发声者张嘴，其他人不跟随口型）\n镜头2：他开灯。"
    assert dialogue_shot_numbers(prompt_text) == frozenset({1})


def test_dialogue_shot_numbers_empty_when_no_placeholder():
    assert dialogue_shot_numbers("镜头1：她推门进来。") == frozenset()


def test_segment_advisories_thread_dialogue_shot_nos():
    beats = [ShotActionBeats(shot_no=1, key_actions=["推门", "开灯"])]
    assert segment_advisories(beats, shot_count=1, dialogue_shot_nos=frozenset({1})) != []
    assert segment_advisories(beats, shot_count=1) == []


def test_action_density_soft_check_filter_threads_dialogue_shot_nos():
    beats = [ShotActionBeats(shot_no=1, key_actions=["推门", "开灯"])]
    check = ActionDensitySoftCheck(hard_attempts=0, segment_no=1)
    # hard_attempts=0：calls=1 > hard_attempts=0，第一次就已经是「最后一次」，
    # 非台词镜放行、台词镜（上限收紧到 1）仍然超限。
    assert check.filter(beats, shot_count=1) == []
    check2 = ActionDensitySoftCheck(hard_attempts=0, segment_no=1)
    assert check2.filter(beats, shot_count=1, dialogue_shot_nos=frozenset({1})) == []  # calls=1>0 同样放行
    check3 = ActionDensitySoftCheck(hard_attempts=1, segment_no=1)
    assert check3.filter(beats, shot_count=1, dialogue_shot_nos=frozenset({1})) != [], "calls=1<=hard_attempts=1，应打回"


# ---------------------------------------------------------------------------
# 规则原文生成侧与复核侧同源（只读调用 storyboard_prose_review，不改该模块）
# ---------------------------------------------------------------------------

def test_prose_review_rules_text_shares_key_action_definition():
    review_text = _review_rules_text(photographic=True, max_shots=4)
    assert "合并计入" in review_text
    assert "{{speech:Uxx}}" in review_text and "最多算 1 个关键动作" in review_text


def test_beat_sheet_rules_includes_segment_key_actions_rule():
    rules = _beat_sheet_rules(set(), adaptation_mode="faithful")
    assert segment_key_actions_rule(max_shots=_MAX_SHOTS_PER_SEGMENT) in rules


# ---------------------------------------------------------------------------
# 端到端：驱动 _generate_beat_sheet 真实组装的 validate 闭包
# ---------------------------------------------------------------------------

def _fixture_segments() -> list[SourceSegment]:
    return [
        SourceSegment(segment_id="s1", text="孟浩扔掉了葫芦。他喃喃自语：“我命由我不由天。”", start_offset=0, end_offset=10),
    ]


def _fixture_payload() -> dict:
    return {
        "asset_manifest": {"characters": [], "scenes": [], "props": []},
        "coverage_ledger": {"paratext": []},
    }


def _overflowing_draft() -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_plan(1, *[f"动作{i}" for i in range(9)])],
    )


@pytest.mark.asyncio
async def test_generate_beat_sheet_validate_closure_retries_then_degrades(monkeypatch):
    draft = _overflowing_draft()
    captured = {}

    async def _stub(*args, **kwargs):
        captured["validate"] = kwargs["validate"]
        return draft

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    await _generate_beat_sheet(
        episode_id="ep_action_capacity_wiring", episode_no=1, segments=_fixture_segments(),
        payload=_fixture_payload(), dialogue_quotes=[], contract_version="2.4.1", adaptation_mode="faithful",
    )
    validate_fn = captured["validate"]

    for attempt in range(_BEAT_SHEET_SEMANTIC_RETRY_LIMIT):
        errors = validate_fn(draft)
        assert errors, f"第 {attempt + 1} 次（attempt {attempt}）应打回，超限草稿不能被静默放行"

    final_errors = validate_fn(draft)
    assert final_errors == [], "最后一次调用必须放行，否则会耗尽 chat_structured 的重试预算导致整集失败"


@pytest.mark.asyncio
async def test_generate_beat_sheet_validate_closure_passes_when_within_capacity(monkeypatch):
    """未超限：行为不变——不占用任何重试预算，第一次就通过。"""
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])], segments=[_plan(1, "a", "b")],
    )
    captured = {}

    async def _stub(*args, **kwargs):
        captured["validate"] = kwargs["validate"]
        return draft

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    await _generate_beat_sheet(
        episode_id="ep_action_capacity_ok", episode_no=1, segments=_fixture_segments(),
        payload=_fixture_payload(), dialogue_quotes=[], contract_version="2.4.1", adaptation_mode="faithful",
    )
    assert captured["validate"](draft) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
