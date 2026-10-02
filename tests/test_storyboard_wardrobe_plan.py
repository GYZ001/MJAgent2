"""P0-D 全集服装表（wardrobe_plan）：schema 默认值/旧存量兼容、按段推进
（开场着装/段内变化/未涉及人物不注入）、规则文案、未知 identity_id/beat_id
剔除+advisory、留档三态，以及一条端到端接线测试证明规则文本真的进了
``_generate_all_segment_prompts`` 发给模型的 task_payload。与
``tests/test_storyboard_beat_foreshadowing.py`` 同构（CLAUDE.md「派单必带
架构约束」——照抄已验证过的伏笔测试骨架）。
"""
from __future__ import annotations

import json

import pytest

from app.production.storyboard_beat_sheet_schemas import (
    _AiBeat, _AiBeatSheetDraft, _AiSegmentPlan, _AiWardrobeState,
)
from app.production.storyboard_wardrobe_plan import (
    WardrobePlanState,
    build_wardrobe_state,
    known_identity_ids,
    segment_rule_text,
    wardrobe_plan_beat_sheet_rules,
    wardrobe_plan_summary,
)
from app.source_excerpt import SourceSegment

_PAYLOAD = {
    "asset_manifest": {
        "characters": [
            {"identity_id": "bible:c1", "display_name": "温念", "aliases": [], "segment_indexes": [1, 2]},
            {"identity_id": "bible:c2", "display_name": "顾屿", "aliases": [], "segment_indexes": [1, 2]},
        ],
    },
}


def _beat_sheet() -> list[_AiBeat]:
    return [
        _AiBeat(beat_id="B1", summary="咖啡馆见面", segment_indexes=[1]),
        _AiBeat(beat_id="B2", summary="围上围巾", segment_indexes=[1]),
    ]


def _segments() -> list[_AiSegmentPlan]:
    return [
        _AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"]),
        _AiSegmentPlan(segment_no=2, synopsis="x", source_segment_indexes=[1], beat_ids=["B2"]),
    ]


def _state(**overrides) -> _AiWardrobeState:
    defaults = dict(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫", change_reason="首次出场")
    defaults.update(overrides)
    return _AiWardrobeState(**defaults)


# ---------------------------------------------------------------------------
# schema 默认值/旧存量兼容
# ---------------------------------------------------------------------------

def test_wardrobe_plan_defaults_to_empty_list():
    draft = _AiBeatSheetDraft(beat_sheet=_beat_sheet(), segments=_segments())
    assert draft.wardrobe_plan == []
    assert draft.prop_entrances == []


def test_old_stored_draft_without_wardrobe_plan_key_still_validates():
    """模拟 storyboard_identity_regenerate._existing_plan 重建旧存量：字典里
    根本没有 wardrobe_plan/prop_entrances 这两个键，验证仍必须通过。"""
    stored = {"beat_sheet": [b.model_dump(mode="json") for b in _beat_sheet()], "segments": [s.model_dump(mode="json") for s in _segments()]}
    draft = _AiBeatSheetDraft.model_validate(stored)
    assert draft.wardrobe_plan == []
    assert draft.prop_entrances == []


# ---------------------------------------------------------------------------
# wardrobe_plan_beat_sheet_rules：正面陈述，取值域明确来自 known_assets
# ---------------------------------------------------------------------------

def test_rules_are_positive_statements_referencing_known_assets():
    rules = wardrobe_plan_beat_sheet_rules()
    assert rules and all(isinstance(r, str) and r for r in rules)
    joined = "".join(rules)
    assert "known_assets.characters" in joined
    assert "wardrobe_plan" in joined
    assert "change_reason" in joined
    assert "进门" in joined and "入睡" in joined  # 情境边界示例


# ---------------------------------------------------------------------------
# known_identity_ids / build_wardrobe_state：未知 identity_id/beat_id 剔除+advisory
# ---------------------------------------------------------------------------

def test_known_identity_ids_reads_asset_manifest():
    assert known_identity_ids(_PAYLOAD) == {"bible:c1", "bible:c2"}


def test_build_wardrobe_state_drops_unknown_identity_and_logs(caplog):
    states = [_state(identity_id="bible:c1"), _state(identity_id="bible:ghost", beat_id="B1")]
    with caplog.at_level("WARNING"):
        wardrobe_state = build_wardrobe_state(states, _PAYLOAD, {"B1", "B2"})
    assert "bible:ghost" in caplog.text
    assert "已从着装推进中剔除" in caplog.text
    start, changes, _defaults = wardrobe_state.advance(["B1"])
    assert [c.identity_id for c in changes] == ["bible:c1"]


def test_build_wardrobe_state_drops_unknown_beat_id_and_logs(caplog):
    states = [_state(beat_id="B_GHOST")]
    with caplog.at_level("WARNING"):
        wardrobe_state = build_wardrobe_state(states, _PAYLOAD, {"B1", "B2"})
    assert "B_GHOST" in caplog.text
    start, changes, _defaults = wardrobe_state.advance(["B1", "B2"])
    assert changes == []


def test_build_wardrobe_state_keeps_valid_entries_silently():
    states = [_state()]
    wardrobe_state = build_wardrobe_state(states, _PAYLOAD, {"B1", "B2"})
    start, changes, _defaults = wardrobe_state.advance(["B1"])
    assert len(changes) == 1


# ---------------------------------------------------------------------------
# WardrobePlanState.advance：开场着装快照 / 段内变化 / 认领一次
# ---------------------------------------------------------------------------

def test_advance_reports_start_snapshot_and_change_for_first_segment():
    plan_state = WardrobePlanState([_state(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫")])
    start, changes, defaults = plan_state.advance(["B1"])
    assert start == {}  # 本段之前还没有任何着装记录
    assert len(changes) == 1 and changes[0].wardrobe == "米白色针织开衫"
    assert defaults == {"bible:c1": True}  # 首次出场=默认造型


def test_advance_carries_look_forward_into_next_segment_start():
    plan_state = WardrobePlanState([_state(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫")])
    plan_state.advance(["B1"])
    start, changes, _defaults = plan_state.advance(["B2"])
    assert start["bible:c1"].wardrobe == "米白色针织开衫"
    assert changes == []


def test_advance_reports_in_segment_change_and_updates_current_look():
    plan_state = WardrobePlanState([
        _state(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫"),
        _state(identity_id="bible:c1", beat_id="B2", wardrobe="颈间绕着深灰色围巾", change_reason="顾屿把围巾解下来绕在她脖子上"),
    ])
    plan_state.advance(["B1"])
    start, changes, defaults = plan_state.advance(["B2"])
    assert start["bible:c1"].wardrobe == "米白色针织开衫"
    assert len(changes) == 1 and changes[0].wardrobe == "颈间绕着深灰色围巾"
    assert defaults == {"bible:c1": False}  # 已换装，不再是默认造型
    next_start, _, _ = plan_state.advance(["B3"])
    assert next_start["bible:c1"].wardrobe == "颈间绕着深灰色围巾"


def test_advance_claims_a_change_only_once_across_split_segments():
    plan_state = WardrobePlanState([_state(beat_id="B1")])
    plan_state.advance(["B1"])
    _, second_claim, _defaults = plan_state.advance(["B1"])
    assert second_claim == []


def test_character_not_in_plan_produces_no_start_entry():
    """未在计划里出现过的人物：开场快照里没有它，advance 不发明任何默认值。"""
    plan_state = WardrobePlanState([_state(identity_id="bible:c1", beat_id="B1")])
    start, _, _defaults = plan_state.advance(["B1"])
    assert "bible:c2" not in start


# ---------------------------------------------------------------------------
# segment_rule_text：规则文案含开场着装/变化+原因；无关人物不注入
# ---------------------------------------------------------------------------

def test_segment_rule_text_contains_planned_look():
    look_start = {"bible:c1": _state(identity_id="bible:c1", wardrobe="米白色针织开衫")}
    lines = segment_rule_text(look_start, [], {}, _PAYLOAD, _PAYLOAD["asset_manifest"]["characters"])
    assert len(lines) == 1
    assert "@温念" in lines[0] and "米白色针织开衫" in lines[0]
    assert "全集服装表" in lines[0]


def test_segment_rule_text_contains_change_and_reason():
    change = _state(identity_id="bible:c1", beat_id="B2", wardrobe="颈间绕着深灰色围巾", change_reason="顾屿把围巾解下来绕在她脖子上")
    lines = segment_rule_text({}, [change], {}, _PAYLOAD, _PAYLOAD["asset_manifest"]["characters"])
    assert len(lines) == 1
    assert "@温念" in lines[0]
    assert "颈间绕着深灰色围巾" in lines[0]
    assert "顾屿把围巾解下来绕在她脖子上" in lines[0]


def test_segment_rule_text_skips_character_not_relevant_to_this_segment():
    """开场快照里有这个人物，但本段 relevant_assets.characters 不包含
    她——不把与本段无关的人物塞进提示词，也不做任何兜底替换。"""
    look_start = {"bible:c1": _state(identity_id="bible:c1")}
    lines = segment_rule_text(look_start, [], {}, _PAYLOAD, [])
    assert lines == []


def test_segment_rule_text_empty_when_nothing_to_report():
    assert segment_rule_text({}, [], {}, _PAYLOAD, _PAYLOAD["asset_manifest"]["characters"]) == []


# ---------------------------------------------------------------------------
# wardrobe_plan_summary：留档三态
# ---------------------------------------------------------------------------

def test_summary_no_plan_nominated():
    draft = _AiBeatSheetDraft(beat_sheet=_beat_sheet(), segments=_segments())
    assert wardrobe_plan_summary(draft, _PAYLOAD) == {"status": "no_plan_nominated", "problem_count": 0}


def test_summary_ok():
    draft = _AiBeatSheetDraft(beat_sheet=_beat_sheet(), segments=_segments(), wardrobe_plan=[_state()])
    assert wardrobe_plan_summary(draft, _PAYLOAD) == {"status": "ok", "problem_count": 0}


def test_summary_warning_counts_invalid_entries():
    draft = _AiBeatSheetDraft(
        beat_sheet=_beat_sheet(), segments=_segments(),
        wardrobe_plan=[_state(), _state(identity_id="bible:ghost")],
    )
    assert wardrobe_plan_summary(draft, _PAYLOAD) == {"status": "warning", "problem_count": 1}


# ---------------------------------------------------------------------------
# 端到端接线：规则文本真的进了 _generate_all_segment_prompts 的 task_payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_all_segment_prompts_injects_wardrobe_rule_text(monkeypatch):
    """段 1 声明首次着装、段 2 声明换装：照抄 tests/test_storyboard_continuity_
    memo.py 的接线测试写法（fake chat_structured 捕获 messages[1]["content"]）。"""
    import app.production.storyboard_pack as storyboard_pack_module
    from app.production.storyboard_pack import _AiStoryboardSegmentDraft

    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        return _AiStoryboardSegmentDraft(prompt_text=f"提示词-段{payload['segment_no']}", shot_count=3)

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=_beat_sheet(), segments=_segments(),
        wardrobe_plan=[
            _state(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫", change_reason="首次出场"),
            _state(identity_id="bible:c1", beat_id="B2", wardrobe="颈间绕着深灰色围巾", change_reason="顾屿把围巾解下来绕在她脖子上"),
        ],
    )
    source = [SourceSegment(segment_id="s1", text="两人在咖啡馆见面。他把围巾解下来绕在她脖子上。", start_offset=0, end_offset=20)]

    await storyboard_pack_module._generate_all_segment_prompts(
        episode_id="ep-wardrobe", episode_no=1, beat_draft=beat_draft, segments=source,
        payload=_PAYLOAD, target_video_model="hiagent", bible=None, conn=None, project_id="", aspect_ratio="9:16",
        enhance_music_bed=False, required_dialogue_by_segment_no={},
    )

    rules_seg1 = "".join(calls[0]["rules"])
    rules_seg2 = "".join(calls[1]["rules"])
    assert "本段着装（全集服装表）" in rules_seg1 and "米白色针织开衫" in rules_seg1
    assert "本段内着装变化" in rules_seg2 and "颈间绕着深灰色围巾" in rules_seg2
    assert "顾屿把围巾解下来绕在她脖子上" in rules_seg2


# ---------------------------------------------------------------------------
# advance()：default_flags——当前认领的记录是否为该人物第一条（= 默认造型）
# ---------------------------------------------------------------------------

def test_advance_default_flags_true_on_first_claim():
    plan_state = WardrobePlanState([_state(identity_id="bible:c1", beat_id="B1")])
    _, _, defaults = plan_state.advance(["B1"])
    assert defaults == {"bible:c1": True}


def test_advance_default_flags_false_after_later_change():
    plan_state = WardrobePlanState([
        _state(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫"),
        _state(identity_id="bible:c1", beat_id="B2", wardrobe="颈间绕着深灰色围巾"),
    ])
    plan_state.advance(["B1"])
    _, _, defaults = plan_state.advance(["B2"])
    assert defaults == {"bible:c1": False}


def test_advance_default_flags_omits_identity_with_no_plan_entry():
    """未在计划里出现过的人物：default_flags 里没有它，不得当成 False。"""
    plan_state = WardrobePlanState([_state(identity_id="bible:c1", beat_id="B1")])
    _, _, defaults = plan_state.advance(["B1"])
    assert "bible:c2" not in defaults


# ---------------------------------------------------------------------------
# segment_rule_text：wardrobe_matches_default 的 yes/no 正面陈述
# ---------------------------------------------------------------------------

def test_segment_rule_text_tells_model_yes_for_default_look():
    lines = segment_rule_text(
        {}, [], {"bible:c1": True}, _PAYLOAD, _PAYLOAD["asset_manifest"]["characters"],
    )
    joined = "".join(lines)
    assert "wardrobe_matches_default 必须填 yes" in joined and "温念" in joined


def test_segment_rule_text_tells_model_no_for_changed_look():
    lines = segment_rule_text(
        {}, [], {"bible:c1": False}, _PAYLOAD, _PAYLOAD["asset_manifest"]["characters"],
    )
    joined = "".join(lines)
    assert "wardrobe_matches_default 必须填 no" in joined and "温念" in joined


def test_segment_rule_text_silent_when_no_plan_entry_for_relevant_character():
    """relevant_characters 里的人物不在 default_flags 里（没有任何服装表记录）
    时，不追加 yes/no 规则——交给模型自判或填 unsure，不兜底。"""
    lines = segment_rule_text({}, [], {}, _PAYLOAD, _PAYLOAD["asset_manifest"]["characters"])
    assert lines == []
