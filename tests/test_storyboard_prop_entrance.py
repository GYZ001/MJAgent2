"""P0-D 道具入场计划（prop_entrances）：schema 默认值/旧存量兼容、规则文案、
按段认领（只认领一次）、规则文本、未知 beat_id 剔除+advisory、"计划要求出场
但提示词没写"的事后 advisory、留档三态。与
``tests/test_storyboard_beat_foreshadowing.py`` 同构。
"""
from __future__ import annotations

from app.production.storyboard_beat_sheet_schemas import (
    _AiBeat, _AiBeatSheetDraft, _AiPropEntrance, _AiSegmentPlan,
)
from app.production.storyboard_prop_entrance import (
    moments_for_segment,
    prop_entrance_beat_sheet_rules,
    prop_entrance_summary,
    segment_advisories,
    segment_rule_text,
    valid_prop_entrances,
)

_BEAT_SHEET = [
    _AiBeat(beat_id="B1", summary="门槛外", segment_indexes=[1]),
    _AiBeat(beat_id="B2", summary="箱子拖出门", segment_indexes=[2]),
]
_SEGMENTS = [
    _AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"]),
    _AiSegmentPlan(segment_no=2, synopsis="x", source_segment_indexes=[2], beat_ids=["B2"]),
]


def _entry(**overrides) -> _AiPropEntrance:
    defaults = dict(label="水浸行李箱", beat_id="B2", entrance_description="从屋内拖出到门口")
    defaults.update(overrides)
    return _AiPropEntrance(**defaults)


# ---------------------------------------------------------------------------
# schema 默认值/旧存量兼容
# ---------------------------------------------------------------------------

def test_prop_entrances_defaults_to_empty_list():
    draft = _AiBeatSheetDraft(beat_sheet=_BEAT_SHEET, segments=_SEGMENTS)
    assert draft.prop_entrances == []


# ---------------------------------------------------------------------------
# prop_entrance_beat_sheet_rules：正面陈述
# ---------------------------------------------------------------------------

def test_rules_are_positive_statements():
    rules = prop_entrance_beat_sheet_rules()
    assert rules and all(isinstance(r, str) and r for r in rules)
    joined = "".join(rules)
    assert "prop_entrances" in joined
    assert "entrance_description" in joined
    assert "凭空出现" in joined


# ---------------------------------------------------------------------------
# valid_prop_entrances：未知 beat_id 剔除 + 记日志
# ---------------------------------------------------------------------------

def test_valid_prop_entrances_keeps_known_beat_id():
    kept = valid_prop_entrances([_entry()], {"B1", "B2"})
    assert kept == [_entry()]


def test_valid_prop_entrances_drops_unknown_beat_id_and_logs(caplog):
    with caplog.at_level("WARNING"):
        kept = valid_prop_entrances([_entry(beat_id="B_GHOST")], {"B1", "B2"})
    assert kept == []
    assert "B_GHOST" in caplog.text
    assert "已剔除" in caplog.text


def test_valid_prop_entrances_mixed_list_keeps_only_valid():
    good, bad = _entry(), _entry(label="幽灵道具", beat_id="B_GHOST")
    kept = valid_prop_entrances([good, bad], {"B1", "B2"})
    assert kept == [good]


# ---------------------------------------------------------------------------
# moments_for_segment：认领一次，覆盖拆段续段场景
# ---------------------------------------------------------------------------

def test_moments_for_segment_claims_only_once():
    entries = [_entry()]
    covered: set[str] = set()
    first = moments_for_segment(["B2"], entries, covered)
    assert first == entries
    second = moments_for_segment(["B2"], entries, covered)
    assert second == []


def test_moments_for_segment_ignores_unrelated_beat_ids():
    covered: set[str] = set()
    assert moments_for_segment(["B1"], [_entry()], covered) == []
    assert covered == set()


# ---------------------------------------------------------------------------
# segment_rule_text
# ---------------------------------------------------------------------------

def test_segment_rule_text_contains_label_and_entrance_description():
    lines = segment_rule_text([_entry()])
    assert len(lines) == 1
    assert "水浸行李箱" in lines[0]
    assert "从屋内拖出到门口" in lines[0]


def test_segment_rule_text_empty_when_no_entrances():
    assert segment_rule_text([]) == []


# ---------------------------------------------------------------------------
# segment_advisories：计划要求出场但提示词没写
# ---------------------------------------------------------------------------

def test_advisory_when_prop_not_mentioned_in_prompt():
    advisories = segment_advisories([_entry()], prompt_text="镜头1：两人站在门槛外。")
    assert advisories
    assert all("STORYBOARD_PACK_PROP_ENTRANCE_NOT_SHOWN" in a for a in advisories)
    assert any("分镜台编辑本段镜头稿补上" in a for a in advisories)


def test_advisory_empty_when_prop_is_mentioned_in_prompt():
    advisories = segment_advisories([_entry()], prompt_text="镜头1：她把水浸行李箱从屋内拖到门口。")
    assert advisories == []


# ---------------------------------------------------------------------------
# prop_entrance_summary：留档三态
# ---------------------------------------------------------------------------

def test_summary_no_entrances_nominated():
    draft = _AiBeatSheetDraft(beat_sheet=_BEAT_SHEET, segments=_SEGMENTS)
    assert prop_entrance_summary(draft) == {"status": "no_entrances_nominated", "problem_count": 0}


def test_summary_ok():
    draft = _AiBeatSheetDraft(beat_sheet=_BEAT_SHEET, segments=_SEGMENTS, prop_entrances=[_entry()])
    assert prop_entrance_summary(draft) == {"status": "ok", "problem_count": 0}


def test_summary_warning_counts_invalid_entries():
    draft = _AiBeatSheetDraft(
        beat_sheet=_BEAT_SHEET, segments=_SEGMENTS,
        prop_entrances=[_entry(), _entry(beat_id="B_GHOST")],
    )
    assert prop_entrance_summary(draft) == {"status": "warning", "problem_count": 1}
