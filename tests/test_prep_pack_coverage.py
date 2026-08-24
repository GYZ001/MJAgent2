"""Red-first hard gate for episode_prep_pack (screenplay contract 6.0.0).

User-mandated invariant (决策②, docs/TRANSFORM_FREEZE_PLAN.md §3): every indexed
source segment must be accounted for by the event chain's declared source_span.
Any segment left uncovered must block publish and the error must name the
exact missing segment_index values -- "禁止静默删戏".

Coverage accounting is span-based (see app.validators.build_prep_pack_span_ledger
and app.production.prep_pack module docstring for the full history of why this
replaced an earlier per-segment disposition-declaration design). Exactly three
things are fatal: a) a gap in the span union ("洞即删戏"), b) an event with no
verbatim-anchored quote inside its own span, or a quote outside it
("引文锚地"), c) spans that cross or regress instead of advancing in order
("跨度有序"). Everything else is unrestricted.

This module tests the deterministic ledger builder/gate directly (no model
calls), matching the existing style of validators.py coverage tests.
"""
from __future__ import annotations

import pytest

from app.source_excerpt import index_source_segments
from app.validators import (
    PREP_PACK_SPAN_LAZINESS_MULTIPLIER,
    assert_prep_pack_coverage_complete,
    assert_prep_pack_span_union_matches_ledger,
    build_prep_pack_span_ledger,
)


SOURCE = (
    "孟浩推开柴门，看见院子里落满黄叶。\n\n"
    "他叹了口气，转身回屋取来扫帚，开始清扫满地的落叶。\n\n"
    "邻居王婶端着一碗热汤走了过来，说道：\u201c浩儿，快趁热喝了。\u201d\n\n"
    "孟浩接过汤碗，心中涌起一阵暖意，向王婶道谢。"
)


def test_source_fixture_has_four_segments():
    # Guard the fixture itself so the rest of the assertions are meaningful.
    assert len(index_source_segments(SOURCE)) == 4


def _seg_text(source: str, index: int) -> str:
    return index_source_segments(source)[index - 1].text


# ---------------------------------------------------------------------------
# a) 洞即删戏：中部挖洞必须阻断，报出具体缺失编号
# ---------------------------------------------------------------------------

def test_hole_in_middle_of_span_union_blocks_publish():
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 2,
            "source_evidence": [{"segment_index": 1, "quote": _seg_text(SOURCE, 1)}],
        },
        # segment 3 is left out of every span -- a hole.
        {
            "event_id": "ev_002", "order": 2, "from_segment": 4, "to_segment": 4,
            "source_evidence": [{"segment_index": 4, "quote": _seg_text(SOURCE, 4)}],
        },
    ]
    ledger, errors = build_prep_pack_span_ledger(SOURCE, events=events)
    assert errors == []
    assert ledger["uncovered"] == [3]
    with pytest.raises(ValueError) as exc_info:
        assert_prep_pack_coverage_complete(ledger)
    assert "PREP_PACK_COVERAGE_INCOMPLETE" in str(exc_info.value)
    assert "3" in str(exc_info.value)


# ---------------------------------------------------------------------------
# b) 完整跨度覆盖 → 通过
# ---------------------------------------------------------------------------

def test_full_span_coverage_passes():
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 2,
            "source_evidence": [{"segment_index": 1, "quote": _seg_text(SOURCE, 1)}],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 3, "to_segment": 4,
            "source_evidence": [{"segment_index": 4, "quote": _seg_text(SOURCE, 4)}],
        },
    ]
    ledger, errors = build_prep_pack_span_ledger(SOURCE, events=events)
    assert errors == []
    assert ledger["uncovered"] == []
    assert ledger["delivered"] == [1, 4]
    assert ledger["retained_as_context"] == [2, 3]
    assert ledger["merged"] == []
    assert ledger["proven_duplicates"] == []
    assert_prep_pack_coverage_complete(ledger)  # must not raise


# ---------------------------------------------------------------------------
# c) 引文不在申报 span 内 → 阻断
# ---------------------------------------------------------------------------

def test_quote_outside_declared_span_blocks():
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 2,
            # segment_index 4 is outside this event's own [1,2] span.
            "source_evidence": [{"segment_index": 4, "quote": _seg_text(SOURCE, 4)}],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 3, "to_segment": 4,
            "source_evidence": [{"segment_index": 4, "quote": _seg_text(SOURCE, 4)}],
        },
    ]
    ledger, errors = build_prep_pack_span_ledger(SOURCE, events=events)
    # This is a rule-(b) contradiction (an event's own evidence disagrees
    # with its own declared span), reported via `errors` -- a different gate
    # from assert_prep_pack_coverage_complete's uncovered check. The caller
    # (app.production.prep_pack._generate_prep_pack_once) blocks on either
    # being non-empty; here both spans still union to full coverage, so
    # uncovered is legitimately empty and errors is where this must surface.
    assert any("落在其自己声明的 span" in message for message in errors)
    assert any("没有任何逐字引文命中原文" in message for message in errors)
    assert ledger["uncovered"] == []


# ---------------------------------------------------------------------------
# d) 巨跨度单引文 → 阻断（反懒惰护栏）
# ---------------------------------------------------------------------------

LONGER_SOURCE = "\n\n".join(f"第{i}段占位叙事内容用于跨度懒惰护栏测试编号{i}。" for i in range(1, 17))


def test_oversized_span_with_insufficient_spread_blocks():
    segments = index_source_segments(LONGER_SOURCE)
    assert len(segments) == 16
    # 4 events, one covering 13/16 segments -- average span is 16/4=4,
    # threshold is 4*PREP_PACK_SPAN_LAZINESS_MULTIPLIER, so a 13-segment span
    # must justify itself with >=2 quotes spread across its front/back
    # halves. Give it only one quote, from the very front, to trigger it.
    threshold = (16 / 4) * PREP_PACK_SPAN_LAZINESS_MULTIPLIER
    assert 13 > threshold  # the fixture below only makes sense if this holds
    big_quote_text = segments[0].text
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 13,
            "source_evidence": [{"segment_index": 1, "quote": big_quote_text}],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 14, "to_segment": 14,
            "source_evidence": [{"segment_index": 14, "quote": segments[13].text}],
        },
        {
            "event_id": "ev_003", "order": 3, "from_segment": 15, "to_segment": 15,
            "source_evidence": [{"segment_index": 15, "quote": segments[14].text}],
        },
        {
            "event_id": "ev_004", "order": 4, "from_segment": 16, "to_segment": 16,
            "source_evidence": [{"segment_index": 16, "quote": segments[15].text}],
        },
    ]
    ledger, errors = build_prep_pack_span_ledger(LONGER_SOURCE, events=events)
    assert any("疑似整段打包偷懒" in message for message in errors)
    # A laziness-guardrail violation is a rule-(b) ledger error, independent
    # of whether the segments happen to be span-covered (uncovered may well
    # be empty here) -- the caller (prep_pack.py) blocks on any non-empty
    # errors list regardless of uncovered.


def test_oversized_span_with_proper_spread_passes_laziness_guardrail():
    segments = index_source_segments(LONGER_SOURCE)
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 13,
            "source_evidence": [
                {"segment_index": 1, "quote": segments[0].text},
                {"segment_index": 13, "quote": segments[12].text},
            ],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 14, "to_segment": 14,
            "source_evidence": [{"segment_index": 14, "quote": segments[13].text}],
        },
        {
            "event_id": "ev_003", "order": 3, "from_segment": 15, "to_segment": 15,
            "source_evidence": [{"segment_index": 15, "quote": segments[14].text}],
        },
        {
            "event_id": "ev_004", "order": 4, "from_segment": 16, "to_segment": 16,
            "source_evidence": [{"segment_index": 16, "quote": segments[15].text}],
        },
    ]
    ledger, errors = build_prep_pack_span_ledger(LONGER_SOURCE, events=events)
    assert not any("疑似整段打包偷懒" in message for message in errors)
    assert ledger["uncovered"] == []


# ---------------------------------------------------------------------------
# e) 相邻事件共享边界段 → 通过且归一化（不因为"重复覆盖"报错）
# ---------------------------------------------------------------------------

def test_adjacent_events_sharing_boundary_segment_passes():
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 2,
            "source_evidence": [{"segment_index": 2, "quote": _seg_text(SOURCE, 2)}],
        },
        # shares segment 2 with ev_001 -- allowed, not a crossing/regression.
        {
            "event_id": "ev_002", "order": 2, "from_segment": 2, "to_segment": 4,
            "source_evidence": [{"segment_index": 4, "quote": _seg_text(SOURCE, 4)}],
        },
    ]
    ledger, errors = build_prep_pack_span_ledger(SOURCE, events=events)
    assert errors == []
    assert ledger["uncovered"] == []
    assert 2 in ledger["delivered"]  # anchored by ev_001's own evidence
    assert_prep_pack_coverage_complete(ledger)  # must not raise


def test_span_crossing_is_fatal():
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 3,
            "source_evidence": [{"segment_index": 1, "quote": _seg_text(SOURCE, 1)}],
        },
        # from_segment=2 is *less than* ev_001's to_segment=3 -- real overlap,
        # not a shared boundary -- must be fatal.
        {
            "event_id": "ev_002", "order": 2, "from_segment": 2, "to_segment": 4,
            "source_evidence": [{"segment_index": 4, "quote": _seg_text(SOURCE, 4)}],
        },
    ]
    ledger, errors = build_prep_pack_span_ledger(SOURCE, events=events)
    assert any("交叉或倒退" in message for message in errors)
    with pytest.raises(ValueError):
        assert_prep_pack_coverage_complete(ledger)


# ---------------------------------------------------------------------------
# prep_pack_version 1.1.0 (coordinator amendment): published event objects now
# carry source_span; the published payload's span union must exactly match
# its own coverage_ledger projection, or the artifact is self-contradictory.
# ---------------------------------------------------------------------------

def test_published_span_union_matching_ledger_passes():
    ledger = {
        "total_segments": 4, "delivered": [1, 4], "merged": [],
        "retained_as_context": [2, 3], "proven_duplicates": [], "uncovered": [],
    }
    event_spans = [
        {"from_segment": 1, "to_segment": 2},
        {"from_segment": 3, "to_segment": 4},
    ]
    assert_prep_pack_span_union_matches_ledger(event_spans=event_spans, ledger=ledger)  # no raise


def test_published_span_union_missing_a_ledger_segment_blocks():
    """The red case the coordinator asked for: the published event objects'
    spans disagree with the artifact's own coverage_ledger -- self-
    contradictory, must block regardless of what build_prep_pack_span_ledger
    itself concluded from the (possibly different) raw model data."""
    ledger = {
        "total_segments": 4, "delivered": [1, 4], "merged": [],
        "retained_as_context": [2, 3], "proven_duplicates": [], "uncovered": [],
    }
    # Event objects only cover segments 1-3 -- segment 4 is missing from the
    # published spans even though the ledger claims it is delivered.
    event_spans = [
        {"from_segment": 1, "to_segment": 3},
    ]
    with pytest.raises(ValueError) as exc_info:
        assert_prep_pack_span_union_matches_ledger(event_spans=event_spans, ledger=ledger)
    assert "PREP_PACK_SPAN_LEDGER_MISMATCH" in str(exc_info.value)
    assert "4" in str(exc_info.value)


def test_published_span_union_with_extra_segment_blocks():
    """The reverse mismatch: published spans claim more than the ledger
    recorded -- also self-contradictory, also fatal."""
    ledger = {
        "total_segments": 4, "delivered": [1], "merged": [],
        "retained_as_context": [2], "proven_duplicates": [], "uncovered": [3, 4],
    }
    event_spans = [
        {"from_segment": 1, "to_segment": 4},  # claims to cover 3 and 4 too
    ]
    with pytest.raises(ValueError) as exc_info:
        assert_prep_pack_span_union_matches_ledger(event_spans=event_spans, ledger=ledger)
    assert "PREP_PACK_SPAN_LEDGER_MISMATCH" in str(exc_info.value)

