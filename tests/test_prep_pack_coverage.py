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
    prep_pack_paratext_segment_indexes,
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
    ledger, errors, extensions = build_prep_pack_span_ledger(SOURCE, events=events)
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
    ledger, errors, extensions = build_prep_pack_span_ledger(SOURCE, events=events)
    assert errors == []
    assert ledger["uncovered"] == []
    assert ledger["delivered"] == [1, 4]
    assert ledger["retained_as_context"] == [2, 3]
    assert ledger["merged"] == []
    assert ledger["proven_duplicates"] == []
    assert_prep_pack_coverage_complete(ledger)  # must not raise


# ---------------------------------------------------------------------------
# c) 确定性跨度扩展（ERR-20260824-9babad，EP2 正式回归）：一个事件自己已核实
# 的引文落在申报 span 之外时，先扩展该事件自己的 span，再做有序/无洞检查——
# 而不是立刻把"落在 span 外"当错误。扩展只能被这个事件自己的核实引文推动；
# 扩展导致跟别的事件的真冲突（有序检查在扩展后的边界上失败）照旧致命。
# ---------------------------------------------------------------------------

def test_adjacent_out_of_span_quote_extends_and_records_one_extension():
    """引文 = span.to + 1（紧邻），归一通过，extensions 记录恰好 1 条。"""
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 1,
            "source_evidence": [
                {"segment_index": 1, "quote": _seg_text(SOURCE, 1)},
                # segment_index 2 == declared to_segment(1) + 1: adjacent overreach.
                {"segment_index": 2, "quote": _seg_text(SOURCE, 2)},
            ],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 3, "to_segment": 4,
            "source_evidence": [{"segment_index": 4, "quote": _seg_text(SOURCE, 4)}],
        },
    ]
    ledger, errors, extensions = build_prep_pack_span_ledger(SOURCE, events=events)
    assert errors == []
    assert ledger["uncovered"] == []
    assert len(extensions) == 1
    assert extensions[0]["event_id"] == "ev_001"
    assert extensions[0]["from"] == 1
    assert extensions[0]["to"] == 2
    assert extensions[0]["extended_by"] == [2]
    assert_prep_pack_coverage_complete(ledger)  # must not raise


def test_extension_reaching_into_next_events_territory_is_still_fatal():
    """引文深入另一事件独占区间：扩展本身发生，但扩展后的跨度使有序检查在
    处理下一个事件时失败——真冲突照旧阻断，扩展机制不豁免它。"""
    segments = index_source_segments(LONGER_SOURCE)
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 3,
            "source_evidence": [
                {"segment_index": 1, "quote": segments[0].text},
                # segment_index 10 sits deep inside ev_003's own exclusive
                # range below -- not a harmless adjacent overreach.
                {"segment_index": 10, "quote": segments[9].text},
            ],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 4, "to_segment": 8,
            "source_evidence": [{"segment_index": 4, "quote": segments[3].text}],
        },
        {
            "event_id": "ev_003", "order": 3, "from_segment": 9, "to_segment": 16,
            "source_evidence": [{"segment_index": 9, "quote": segments[8].text}],
        },
    ]
    ledger, errors, extensions = build_prep_pack_span_ledger(LONGER_SOURCE, events=events)
    # ev_001 does extend (its own verified evidence reaches segment 10)...
    assert any(item["event_id"] == "ev_001" and item["to"] == 10 for item in extensions)
    # ...but the extension swallows ev_002's declared start, which must
    # still surface as a fatal crossing/regression when ev_002 is processed.
    assert any("交叉或倒退" in message for message in errors)
    with pytest.raises(ValueError):
        assert_prep_pack_coverage_complete(ledger)


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
    ledger, errors, extensions = build_prep_pack_span_ledger(LONGER_SOURCE, events=events)
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
    ledger, errors, extensions = build_prep_pack_span_ledger(LONGER_SOURCE, events=events)
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
    ledger, errors, extensions = build_prep_pack_span_ledger(SOURCE, events=events)
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
    ledger, errors, extensions = build_prep_pack_span_ledger(SOURCE, events=events)
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


# ---------------------------------------------------------------------------
# prep_pack_version 1.4.0 (coordinator decision): paratext ledger account.
# Users do not want chapter-heading/author's-note text to become event-chain
# "events"; classification is 100% deterministic (no model call) and
# POSITION-constrained on purpose -- see app.validators' module comment above
# prep_pack_paratext_segment_indexes for the full false-positive argument
# (directly informed by app/source_paratext.py's own documented prior
# rejection of bare keyword/position classification).
# ---------------------------------------------------------------------------

PARATEXT_SOURCE = "\n\n".join([
    "第一章 山间清晨",                                            # 1: chapter heading -> paratext (①)
    "孟浩推开柴门，看见院子里落满黄叶。",                              # 2: real story
    "他叹了口气，转身回屋取来扫帚，开始清扫满地的落叶。",                  # 3: real story
    "邻居王婶端着一碗热汤走了过来，说道：“浩儿，快趁热喝了。”",  # 4: real story
    "感谢大家一直以来的支持，本书求收藏求推荐！",                        # 5: trailing author's note -> paratext (②)
    "新书已经上架，欢迎大家多多支持，谢谢！",                            # 6: trailing author's note -> paratext (②)
])


def _paratext_seg_text(index: int) -> str:
    return index_source_segments(PARATEXT_SOURCE)[index - 1].text


def test_paratext_fixture_classification_is_heading_plus_trailing_run_only():
    # Guard the fixture itself: exactly {1, 5, 6}, not more (segment 2's
    # story text must not bleed into "subtitle" absorption, and the
    # backward scan must stop the instant it hits segment 4's real content).
    assert prep_pack_paratext_segment_indexes(PARATEXT_SOURCE) == {1, 5, 6}


def test_paratext_segments_are_exempt_from_event_coverage_and_ledger_passes():
    """红灯 a）：首段章节名 + 尾部连续留言段 → 正确归入 paratext，五账全量
    覆盖通过，且不要求这些段落被任何事件的 span 覆盖。"""
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 2, "to_segment": 3,
            "source_evidence": [{"segment_index": 2, "quote": _paratext_seg_text(2)}],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 4, "to_segment": 4,
            "source_evidence": [{"segment_index": 4, "quote": _paratext_seg_text(4)}],
        },
    ]
    ledger, errors, extensions = build_prep_pack_span_ledger(PARATEXT_SOURCE, events=events)
    assert errors == []
    assert ledger["paratext"] == [1, 5, 6]
    assert ledger["uncovered"] == []  # 1/5/6 never claimed by any event span
    assert ledger["delivered"] == [2, 4]
    assert ledger["retained_as_context"] == [3]
    assert_prep_pack_coverage_complete(ledger)  # must not raise


def test_mid_body_recommendation_ticket_mention_is_not_classified_as_paratext():
    """红灯 b）（关键假阳性护栏，直接对应 app/source_paratext.py 文档记载的
    历史教训："正文里出现「收藏」等" 不得误伤）：正文中间一句提到"推荐票"的
    真实叙事/心理描写，绝不能被归入 paratext——从末段向前扫的规则只认一段
    "不中断的尾巴"，这句后面紧跟着不含关键词的正常故事段落，扫描早在到达
    它之前就已经停止。"""
    source = "\n\n".join([
        "第一章 山间清晨",
        "孟浩推开柴门，心里想着若能得几张推荐票就好了，随即又摇了摇头。",
        "他叹了口气，转身回屋取来扫帚，开始清扫满地的落叶。",
        "邻居王婶端着一碗热汤走了过来，说道：“浩儿，快趁热喝了。”",
    ])
    paratext = prep_pack_paratext_segment_indexes(source)
    assert paratext == {1}
    assert 2 not in paratext


def test_paratext_segment_reachable_by_an_event_span_is_a_fatal_contradiction():
    """红灯 c）：某段落同时出现在 paratext 与事件覆盖（delivered/
    retained_as_context）账里——两套判定互相矛盾，必须致命阻断，不能静默
    接受任何一边。"""
    events = [
        # ev_001's span erroneously reaches into segment 1 (the chapter
        # heading) and even quotes it as evidence -- the model ignored the
        # "these segment numbers don't need coverage" instruction.
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 3,
            "source_evidence": [{"segment_index": 1, "quote": _paratext_seg_text(1)}],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 4, "to_segment": 4,
            "source_evidence": [{"segment_index": 4, "quote": _paratext_seg_text(4)}],
        },
    ]
    ledger, errors, extensions = build_prep_pack_span_ledger(PARATEXT_SOURCE, events=events)
    assert 1 in ledger["paratext"]
    assert any("账本自相矛盾" in message for message in errors)
    # Same enforcement path as every other rule-(b)/(c) violation in this
    # file (e.g. test_oversized_span_with_insufficient_spread_blocks): the
    # caller (app.production.prep_pack) blocks on any non-empty `errors`
    # regardless of what `uncovered` says.

