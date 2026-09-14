"""容量归一化拆段时，拆点句单元被相邻两箱共用的情形。

2026-09-14 我欲封天第 3 集：一句超长独白被台账预拆成 Q24（49 字）+ Q25（9 字），同在
段3·S03，合计 58 字超过 54 字段容量，装箱必然分到两段；旧逻辑把拆点 S03 整个划给
后一段，前一段窗口只剩 S01–S02、不含自己的必保台词 Q24，阶段二「必保台词无法追溯」
三次修复仍失败、整集分镜失败。现在拆点单元同时留在两段范围里。
"""
from __future__ import annotations

from app import config
from app.production.storyboard_beat_sheet import _AiBeat, _AiBeatSheetDraft, _AiSegmentPlan
from app.production.storyboard_capacity_normalize import normalize_beat_sheet_capacity
from app.production.storyboard_dialogue_ledger import DialogueQuote, _AiKeptLine
from app.production.storyboard_segment_ranges import (
    _AiSourceUnitRange,
    kept_line_unit_binding_errors,
    quote_unit_index,
    split_source_units,
)
from app.source_excerpt import SourceSegment

PART_A = "之前读书时，经常背书到天明，早就饿习惯了，此刻这样的生活，虽说疲惫，可总归是一条出路，我孟浩就不信，自己科举不成，"
PART_B = "在这宗门修行也不成。"
TEXT = "时间就这样流逝，很快就到了黄昏。“" + PART_A + PART_B + "”孟浩目中越加执着，低头体悟。"


def _fixture():
    segments = [SourceSegment(segment_id="s1", text=TEXT, start_offset=0, end_offset=0)]
    assert len(split_source_units(TEXT)) == 3
    quotes = [
        DialogueQuote(quote_id="Q01", source_segment_index=1, text=PART_A, content_chars=49),
        DialogueQuote(quote_id="Q02", source_segment_index=1, text=PART_B, content_chars=9),
    ]
    assert quote_unit_index(quotes[0], TEXT) == quote_unit_index(quotes[1], TEXT) == 2
    plan = _AiSegmentPlan(
        segment_no=1, synopsis="砍柴归来看凝气卷", source_segment_indexes=[1], beat_ids=["B1"],
        source_unit_ranges=[_AiSourceUnitRange(source_segment_index=1, from_unit=1, to_unit=3)],
    )
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[plan],
        kept_lines=[_AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q02", segment_no=1)],
    )
    return segments, quotes, draft


def test_split_within_one_sentence_unit_keeps_that_unit_in_both_segments() -> None:
    assert config.MAX_SPOKEN_CHARS_PER_SHOT < 58
    segments, quotes, draft = _fixture()
    telemetry = normalize_beat_sheet_capacity(draft, quotes, source_segments=segments)

    assert telemetry == [{"original_segment_no": 1, "bin_count": 2, "new_segment_nos": [1, 2]}]
    first, second = draft.segments
    assert [(r.from_unit, r.to_unit) for r in first.source_unit_ranges] == [(1, 2)]
    assert [(r.from_unit, r.to_unit) for r in second.source_unit_ranges] == [(2, 3)]
    assert {k.quote_id: k.segment_no for k in draft.kept_lines} == {"Q01": 1, "Q02": 2}
    # 两段各自的必保台词都落在自己声明的单元范围里——阶段二的追溯才可能成功。
    assert kept_line_unit_binding_errors(draft.kept_lines, quotes, draft.segments, segments) == []


def test_split_between_different_units_stays_disjoint() -> None:
    """拆点与前一箱末条不在同一句单元时，范围照旧不重叠。"""
    text = "第一句。“甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲。”“乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙。”"
    segments = [SourceSegment(segment_id="s1", text=text, start_offset=0, end_offset=0)]
    quotes = [
        DialogueQuote(quote_id="Q01", source_segment_index=1, text="甲" * 30, content_chars=30),
        DialogueQuote(quote_id="Q02", source_segment_index=1, text="乙" * 30, content_chars=30),
    ]
    assert (quote_unit_index(quotes[0], text), quote_unit_index(quotes[1], text)) == (2, 3)
    plan = _AiSegmentPlan(
        segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"],
        source_unit_ranges=[_AiSourceUnitRange(source_segment_index=1, from_unit=1, to_unit=3)],
    )
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])], segments=[plan],
        kept_lines=[_AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q02", segment_no=1)],
    )
    normalize_beat_sheet_capacity(draft, quotes, source_segments=segments)
    first, second = draft.segments
    assert [(r.from_unit, r.to_unit) for r in first.source_unit_ranges] == [(1, 2)]
    assert [(r.from_unit, r.to_unit) for r in second.source_unit_ranges] == [(3, 3)]
