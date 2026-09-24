"""短剧节奏档台词预算：``dialogue_budget_payload``/``dialogue_budget_rule``/
``kept_dialogue_chars``/``DialogueBudgetSoftCap``，以及 ``_beat_sheet_task_
payload`` 对短剧档的接线（忠实档逐字节不受影响——指纹冻结测试见
``tests/test_storyboard_short_drama_schemas.py``，本文件只补预算相关内容的
正向覆盖，不重复指纹断言）。
"""
from __future__ import annotations

from app.production.storyboard_beat_sheet import _beat_sheet_draft_cls, _beat_sheet_task_payload
from app.production.storyboard_dialogue_ledger import DialogueQuote, _AiKeptLine
from app.production.storyboard_short_drama import DIALOGUE_BUDGET_CHARS, MAX_SEGMENT_COUNT
from app.production.storyboard_short_drama_budget import (
    DialogueBudgetSoftCap, dialogue_budget_payload, dialogue_budget_rule, kept_dialogue_chars,
)
from app.source_excerpt import SourceSegment


def _quotes(*chars_list: int) -> list[DialogueQuote]:
    return [
        DialogueQuote(quote_id=f"Q{i:02d}", source_segment_index=1, text="x" * c, content_chars=c)
        for i, c in enumerate(chars_list, start=1)
    ]


# ---------------------------------------------------------------------------
# 预算口径：8 * 54 = 432，从既有常量推导，不写字面量
# ---------------------------------------------------------------------------

def test_budget_constant_derived_from_segment_count_and_shot_capacity():
    assert MAX_SEGMENT_COUNT == 8
    assert DIALOGUE_BUDGET_CHARS == MAX_SEGMENT_COUNT * 54 == 432


def test_dialogue_budget_payload_under_budget():
    assert dialogue_budget_payload(_quotes(100, 100)) == {"total_chars": 200, "budget_chars": 432, "over_budget_chars": 0}


def test_dialogue_budget_payload_over_budget():
    assert dialogue_budget_payload(_quotes(300, 300)) == {"total_chars": 600, "budget_chars": 432, "over_budget_chars": 168}


def test_dialogue_budget_rule_mentions_numbers_and_verbatim_requirement():
    rule = dialogue_budget_rule({"total_chars": 600, "budget_chars": 432, "over_budget_chars": 168})
    assert "432" in rule and "600" in rule and "168" in rule
    assert "kept_lines" in rule and "dropped_lines" in rule
    assert "逐字取自原文" in rule, "台词逐字取自原文不改写，是硬约束不是可选建议"


def test_dialogue_budget_rule_under_budget_states_not_over():
    rule = dialogue_budget_rule({"total_chars": 100, "budget_chars": 432, "over_budget_chars": 0})
    assert "未超预算" in rule


# ---------------------------------------------------------------------------
# kept_dialogue_chars：与 dialogue_ledger_summary 同一口径（content_chars 求和）
# ---------------------------------------------------------------------------

def test_kept_dialogue_chars_sums_only_kept_ones():
    quotes = _quotes(10, 20, 30)
    kept = [_AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q03", segment_no=2)]
    assert kept_dialogue_chars(kept, quotes) == 40


def test_kept_dialogue_chars_ignores_unknown_quote_ids():
    quotes = _quotes(10)
    kept = [_AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q99", segment_no=1)]
    assert kept_dialogue_chars(kept, quotes) == 10


def test_kept_dialogue_chars_empty_is_zero():
    assert kept_dialogue_chars([], []) == 0


# ---------------------------------------------------------------------------
# DialogueBudgetSoftCap：前几次打回，最后一次降级为警告；忠实档永远不产生错误
# （与 SegmentCountSoftCap 同构，见 tests/test_storyboard_short_drama.py 对照）
# ---------------------------------------------------------------------------

class _FakeDraft:
    def __init__(self, kept: list[_AiKeptLine]) -> None:
        self.kept_lines = kept


def test_soft_cap_blocks_first_attempts_then_warns_on_last():
    quotes = _quotes(266, 266)  # 合计 532 字，超过预算 432
    kept = [_AiKeptLine(quote_id=q.quote_id, segment_no=1) for q in quotes]
    cap = DialogueBudgetSoftCap(adaptation_mode="short_drama", retry_limit=2, quotes=quotes)
    over = _FakeDraft(kept)
    assert cap.errors(over) != [], "第 1 次（attempt 0）应打回"
    assert cap.errors(over) != [], "第 2 次（attempt 1）应打回"
    assert cap.errors(over) == [], "第 3 次（attempt 2 == retry_limit，最后一次）应降级为警告"


def test_soft_cap_never_errors_when_within_budget():
    quotes = _quotes(100)
    kept = [_AiKeptLine(quote_id="Q01", segment_no=1)]
    cap = DialogueBudgetSoftCap(adaptation_mode="short_drama", retry_limit=2, quotes=quotes)
    within = _FakeDraft(kept)
    assert cap.errors(within) == []
    assert cap.errors(within) == []
    assert cap.errors(within) == []


def test_soft_cap_is_noop_for_faithful_mode():
    quotes = _quotes(1000)
    kept = [_AiKeptLine(quote_id="Q01", segment_no=1)]
    cap = DialogueBudgetSoftCap(adaptation_mode="faithful", retry_limit=2, quotes=quotes)
    huge = _FakeDraft(kept)
    assert cap.errors(huge) == []
    assert cap.errors(huge) == []
    assert cap.errors(huge) == []


# ---------------------------------------------------------------------------
# _beat_sheet_task_payload：短剧档接线，忠实档不受影响
# ---------------------------------------------------------------------------

def _fixture_segments() -> list[SourceSegment]:
    return [SourceSegment(segment_id="s1", text="孟浩说：“我命由我不由天。”", start_offset=0, end_offset=10)]


def _fixture_payload() -> dict:
    return {"asset_manifest": {"characters": [], "scenes": [], "props": []}, "coverage_ledger": {"paratext": []}}


def test_task_payload_short_drama_includes_dialogue_budget():
    quotes = [DialogueQuote(quote_id="Q01", source_segment_index=1, text="我命由我不由天。", content_chars=8)]
    payload = _beat_sheet_task_payload(
        episode_no=1, payload=_fixture_payload(), segments=_fixture_segments(),
        paratext_indexes=set(), context_indexes=set(), dialogue_quotes=quotes,
        adaptation_mode="short_drama", draft_cls=_beat_sheet_draft_cls("short_drama"),
    )
    assert payload["dialogue_budget"] == {"total_chars": 8, "budget_chars": 432, "over_budget_chars": 0}
    assert any("台词预算" in r for r in payload["rules"])


def test_task_payload_faithful_has_no_dialogue_budget_key_or_rule():
    quotes = [DialogueQuote(quote_id="Q01", source_segment_index=1, text="我命由我不由天。", content_chars=8)]
    payload = _beat_sheet_task_payload(
        episode_no=1, payload=_fixture_payload(), segments=_fixture_segments(),
        paratext_indexes=set(), context_indexes=set(), dialogue_quotes=quotes,
        adaptation_mode="faithful", draft_cls=_beat_sheet_draft_cls("faithful"),
    )
    assert "dialogue_budget" not in payload
    assert not any("台词预算" in r for r in payload["rules"])
