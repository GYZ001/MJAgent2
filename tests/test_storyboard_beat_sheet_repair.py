"""节拍表确定性修补：色温统一、范围裁剪/合并、补洞、不回退——修完必须过原校验。"""
from __future__ import annotations

from app.production.storyboard_beat_sheet import _AiBeatSheetDraft, _AiSegmentPlan
from app.production.storyboard_beat_sheet_repair import repair_beat_sheet_draft
from app.production.storyboard_narrative_arc import palette_scene_consistency_errors
from app.production.storyboard_segment_ranges import segment_unit_range_errors, split_source_units
from tests.test_storyboard_segment_ranges import _SCENE_4_TEXT, _scene_4_segments


def _plan(no, ranges, *, palette="暖黄", index=4):
    return _AiSegmentPlan(
        segment_no=no, synopsis=f"段{no}", source_segment_indexes=[index], palette=palette,
        source_unit_ranges=[{"source_segment_index": index, "from_unit": a, "to_unit": b} for a, b in ranges],
    )


def _draft(plans):
    return _AiBeatSheetDraft(beat_sheet=[{"beat_id": "b1", "summary": "x", "segment_indexes": [4]}], segments=plans)


def test_repair_makes_real_failure_shapes_pass_validation():
    """真实打回形态一次修全：越界（S17>16）、洞（S11 无人覆盖）、回退、多条范围、同场戏色温不同。"""
    assert len(split_source_units(_SCENE_4_TEXT)) == 12
    plans = [
        _plan(1, [(1, 2), (5, 6)], palette="暖黄日光"),   # 同一原文段两条范围
        _plan(2, [(3, 4)], palette="淡青晨光"),            # 同场戏色温不同
        _plan(3, [(6, 9)], palette="暖黄日光"),            # 与段 1 的 S5-6 回退重叠
        _plan(4, [(11, 17)], palette="暖黄日光"),          # 越界 + 与段 3 之间 S10 是洞
    ]
    draft = _draft(plans)
    notes = repair_beat_sheet_draft(draft, _scene_4_segments(), set())
    assert notes, "应有修补记录"
    assert segment_unit_range_errors(draft.segments, _scene_4_segments(), set()) == []
    assert palette_scene_consistency_errors(draft.segments) == []
    assert [s.palette for s in draft.segments] == ["暖黄日光"] * 4
    ranges = [(s.source_unit_ranges[0].from_unit, s.source_unit_ranges[0].to_unit) for s in draft.segments]
    assert ranges[0][0] == 1 and ranges[-1][1] == 12
    covered = set()
    for a, b in ranges:
        covered.update(range(a, b + 1))
    assert covered == set(range(1, 13))


def test_repair_is_noop_on_valid_draft():
    plans = [_plan(1, [(1, 6)]), _plan(2, [(7, 12)])]
    draft = _draft(plans)
    assert repair_beat_sheet_draft(draft, _scene_4_segments(), set()) == []
    assert [(s.source_unit_ranges[0].from_unit, s.source_unit_ranges[0].to_unit) for s in draft.segments] == [(1, 6), (7, 12)]


def test_empty_palette_is_not_filled_in():
    """空 palette 是漏填信号，不兜底沿用（保持校验去报）。"""
    plans = [_plan(1, [(1, 6)], palette="暖黄"), _plan(2, [(7, 12)], palette="")]
    draft = _draft(plans)
    repair_beat_sheet_draft(draft, _scene_4_segments(), set())
    assert draft.segments[1].palette == ""


def test_undroppable_dropped_line_is_restored_to_a_covering_segment():
    """第 3 集真实形态：整句台词被塞进 dropped_lines，放回覆盖其单元的段，不再打回模型。"""
    from types import SimpleNamespace
    from app.production.storyboard_beat_sheet import undroppable_quote_errors
    from app.production.storyboard_beat_sheet_repair import restore_undroppable_lines
    from app.production.storyboard_dialogue_ledger import DialogueQuote

    plans = [_plan(1, [(1, 3)]), _plan(2, [(4, 12)])]
    draft = _draft(plans)
    draft.kept_lines = []
    draft.dropped_lines = [SimpleNamespace(quote_id="Q22", reason="未在当前节拍中保留")]
    quotes = [DialogueQuote(quote_id="Q22", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=17, speaker="小胖子")]
    notes = restore_undroppable_lines(draft, quotes, _scene_4_segments())
    assert notes and draft.dropped_lines == []
    assert [(k.quote_id, k.segment_no) for k in draft.kept_lines] == [("Q22", 1)]  # S03 在第 1 段范围内
    assert undroppable_quote_errors(draft.dropped_lines, quotes) == []


def test_droppable_filler_stays_dropped():
    from types import SimpleNamespace
    from app.production.storyboard_beat_sheet_repair import restore_undroppable_lines
    from app.production.storyboard_dialogue_ledger import DialogueQuote

    draft = _draft([_plan(1, [(1, 12)])])
    draft.kept_lines = []
    draft.dropped_lines = [SimpleNamespace(quote_id="Q01", reason="语气词")]
    quotes = [DialogueQuote(quote_id="Q01", source_segment_index=4, text="喵", content_chars=1, speaker="橘座")]
    assert restore_undroppable_lines(draft, quotes, _scene_4_segments()) == []
    assert len(draft.dropped_lines) == 1


def test_missing_quote_decisions_are_completed_then_restored_by_rule():
    """2026-09-05 第 3 集 Q41：模型既没 kept 也没 dropped。整句先补进 dropped，再由不可弃置规则放回；
    语气词留在弃置区。"""
    from app.production.storyboard_beat_sheet_repair import complete_missing_quote_decisions, restore_undroppable_lines
    from app.production.storyboard_dialogue_ledger import DialogueQuote

    draft = _draft([_plan(1, [(1, 3)]), _plan(2, [(4, 12)])])
    draft.kept_lines = []
    draft.dropped_lines = []
    quotes = [
        DialogueQuote(quote_id="Q41", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=17, speaker="小胖子"),
        DialogueQuote(quote_id="Q42", source_segment_index=4, text="喵", content_chars=1, speaker="橘座"),
    ]
    notes = complete_missing_quote_decisions(draft, quotes)
    assert len(notes) == 2 and {d.quote_id for d in draft.dropped_lines} == {"Q41", "Q42"}
    restore_undroppable_lines(draft, quotes, _scene_4_segments())
    assert [k.quote_id for k in draft.kept_lines] == ["Q41"]
    assert [d.quote_id for d in draft.dropped_lines] == ["Q42"]
    assert complete_missing_quote_decisions(draft, quotes) == [], "已决定去留的不再重复补"


def test_beat_and_segment_key_confusion_is_normalized_before_validation():
    """第 2 集真实形态：第 15 段写成 summary（段 schema 要 synopsis）；格式修复又把 beat15 写成段的形状。"""
    from app.production.storyboard_beat_sheet import _AiBeatSheetDraft, _normalize_beat_sheet_payload

    payload = {
        "beat_sheet": [
            {"beat_id": "beat1", "summary": "开篇", "segment_indexes": [1]},
            {"beat_id": "beat15", "synopsis": "孟浩安慰小胖子", "source_segment_indexes": [3], "beat_ids": ["beat15"],
             "palette": "暖调", "source_unit_ranges": [{"source_segment_index": 3, "from_unit": 5, "to_unit": 12}]},
        ],
        "segments": [
            {"segment_no": 1, "synopsis": "开篇", "source_segment_indexes": [1], "beat_ids": ["beat1"]},
            {"segment_no": 15, "summary": "孟浩安慰小胖子", "segment_indexes": [3], "beat_ids": ["beat15"]},
        ],
        "kept_lines": [], "dropped_lines": [],
    }
    draft = _AiBeatSheetDraft.model_validate(_normalize_beat_sheet_payload(payload))
    assert draft.beat_sheet[1].summary == "孟浩安慰小胖子" and draft.beat_sheet[1].segment_indexes == [3]
    assert draft.segments[1].synopsis == "孟浩安慰小胖子" and draft.segments[1].source_segment_indexes == [3]
    assert draft.beat_sheet[0].summary == "开篇", "本来就对的字段不动"


def test_uncovered_source_segment_with_required_lines_gets_a_synthesized_segment():
    """第 2 集真实形态：segments 只覆盖前两个原文段，原文段 4 的必保台词没有任何段覆盖。"""
    from app.production.storyboard_beat_sheet_repair import append_segments_for_uncovered_sources
    from app.production.storyboard_dialogue_ledger import DialogueQuote, _AiKeptLine
    from app.production.storyboard_segment_ranges import reassign_kept_lines_to_covering_segments

    draft = _draft([_plan(1, [(1, 1)], index=3)])
    draft.kept_lines = [_AiKeptLine(quote_id="Q14", segment_no=1)]
    quotes = [DialogueQuote(quote_id="Q14", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=17, speaker="小胖子")]
    notes = append_segments_for_uncovered_sources(draft, quotes, _scene_4_segments(), set())
    assert len(notes) == 1
    assert [s.segment_no for s in draft.segments] == [1, 2]
    added = draft.segments[1]
    assert added.source_segment_indexes == [4] and added.beat_ids == ["b1"] and added.palette == "暖黄"
    assert (added.source_unit_ranges[0].from_unit, added.source_unit_ranges[0].to_unit) == (1, 12)
    assert segment_unit_range_errors(draft.segments, _scene_4_segments(), set()) == []
    reassign_kept_lines_to_covering_segments(draft.kept_lines, quotes, draft.segments, _scene_4_segments())
    assert draft.kept_lines[0].segment_no == 2, "补段后台词按单元归位到新段"
    assert append_segments_for_uncovered_sources(draft, quotes, _scene_4_segments(), set()) == [], "已覆盖不再补"
