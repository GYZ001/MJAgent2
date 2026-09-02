"""映射台判定的背景交代段不得在分镜台单独成段（2026-09-02 西游一集/三国白话一集「段 01」）。

覆盖账 retained_as_context 的段（开篇诗、史评）没有事件、没有登记人物/场景。节拍表若把它们单独
切成一段，逐段提示词只能虚构外貌描述，生成台在参考图模式下没有任何参考图可绑，任务停在
「等待人工处理」且只有「重新生成」——重试必现。判据全部取自映射台的账。
"""
from __future__ import annotations

from app.production.storyboard_context_segments import (
    context_only_segment_errors,
    context_segment_indexes,
    context_segment_rule,
)
from app.production.storyboard_pack import _AiBeatSheetDraft, _beat_sheet_rules, _validate_beat_sheet_draft


def test_context_segment_indexes_reads_ledger_and_is_empty_when_missing() -> None:
    assert context_segment_indexes({"coverage_ledger": {"retained_as_context": [2, 3, True, "x"]}}) == {2, 3}
    assert context_segment_indexes({}) == set()
    assert context_segment_indexes({"coverage_ledger": {"retained_as_context": "2,3"}}) == set()


def _draft(sources_by_segment: dict[int, list[int]]) -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft.model_validate({
        "beat_sheet": [{"beat_id": f"B{no:02d}", "summary": "x", "segment_indexes": src} for no, src in sources_by_segment.items()],
        "segments": [
            {"segment_no": no, "synopsis": "x", "source_segment_indexes": src, "beat_ids": [f"B{no:02d}"]}
            for no, src in sources_by_segment.items()
        ],
        "kept_lines": [], "dropped_lines": [],
    })


def test_segment_sourced_only_from_context_segments_is_rejected_with_merge_guidance() -> None:
    draft = _draft({1: [2, 3], 2: [4, 5]})
    errors = _validate_beat_sheet_draft(
        draft, total_segments=13, dialogue_quotes=[], context_indexes={2, 3}, paratext_indexes={1},
    )
    assert len(errors) == 1, errors
    assert "第 1 段" in errors[0] and "全部是映射台判定的背景交代段" in errors[0]
    assert "并入相邻的事件段" in errors[0]


def test_context_segments_merged_into_an_event_segment_pass() -> None:
    draft = _draft({1: [2, 3, 4], 2: [5]})
    assert _validate_beat_sheet_draft(
        draft, total_segments=13, dialogue_quotes=[], context_indexes={2, 3}, paratext_indexes={1},
    ) == []


def test_no_context_ledger_keeps_legacy_behaviour() -> None:
    draft = _draft({1: [2, 3], 2: [4]})
    assert context_only_segment_errors(draft.segments, set(), {1}) == []
    assert _validate_beat_sheet_draft(draft, total_segments=13, dialogue_quotes=[]) == []


def test_rules_state_context_segments_positively() -> None:
    rules = _beat_sheet_rules(set(), {2, 3})
    assert any("背景交代" in r and "[2, 3]" in r and "不得单独成段" in r for r in rules)
    assert context_segment_rule(set()) is None
