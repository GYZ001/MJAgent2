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
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(SOURCE, events=events)
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
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(SOURCE, events=events)
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
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(SOURCE, events=events)
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
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(LONGER_SOURCE, events=events)
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
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(LONGER_SOURCE, events=events)
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
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(LONGER_SOURCE, events=events)
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
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(SOURCE, events=events)
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
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(SOURCE, events=events)
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
# prep_pack_version 1.4.1 (coordinator decision, real round-15 EP2
# regression fix, retiring the 1.4.0 pure-keyword/position classifier the
# same day): paratext ledger account v3 -- the MODEL declares
# (paratext_segments in app.production.prep_pack._ChunkResponse) which of
# its own chunk's segments are non-story (chapter title / author's note),
# and build_prep_pack_span_ledger runs three independent deterministic veto
# gates over that declaration: (a) position (structural first-segment
# heading OR an exact contiguous tail-window run reaching the final
# segment), (b) no-dependency (not referenced by any event's
# source_evidence/key_lines), (c) exclusivity (not inside any event's
# validated span -- FATAL if violated, not a silent veto). A declaration
# that fails (a) or (b) is not fatal by itself -- it silently falls back to
# being ordinary content, still subject to the pre-existing "must be inside
# some event's span" gate.
#
# Fixture below is REAL corpus, not invented text (per coordinator's
# explicit instruction): segments correspond 1:1 to segments 44-50 of
# proj_3ac0b627fa46's chapters row idx=2 (real chapter "第二章靠山宗" of
# 我欲封天), pulled via app.source_excerpt.index_source_segments on the
# actual stored chapter content and re-verified to re-segment identically
# when rejoined standalone. Segment 5 here (original 48, "另外，耳根注意到
# 有读者察觉到了第一章的葫芦……") is the real "transition sentence" that
# broke the 1.4.0 keyword-scan design (the gap the coordinator's fix
# targets) -- under v3 it does not need to carry any signal of its own at
# all, since the model declares the whole block directly.
# ---------------------------------------------------------------------------

EP2_CH2_TAIL_SOURCE = "\n\n".join([
    # 1 (orig 44): real story, immediately before the author's note block.
    "孟浩看了小胖子半天，确定了此人有梦游的习惯后又看了一眼桌子角，隐隐觉得这小胖子睡觉时不可招惹，小心翼翼的挪远了一些，低头望着小册子，神色继续激动。",
    # 2 (orig 45): real story.
    "“凝气九层，仙灵之路，为仙人打工，给出可以成为仙人的机会，这就是最大的工钱，我就不信若是自己成了仙人，还做不成有钱人！”孟浩紧紧的抓住小册子，眼中露出强烈的光芒，他仿佛看到了自己除了读书之外的另一条路。",
    # 3 (orig 46): real story, the chapter's actual last narrative beat.
    "就在这时，突然屋舍的房门砰的一声，被人一脚踹开，一声冷哼随之传入房间。",
    # 4 (orig 47): author's note begins.
    "看到书评区知道子右君家的孩子满月，耳根在这里恭喜柚子，祝孩子健健康康，人中龙凤！",
    # 5 (orig 48): the real "transition sentence" with no strong signal word
    # of its own -- this is exactly what defeated the 1.4.0 keyword scan.
    "另外，耳根注意到有读者察觉到了第一章的葫芦……没错，那就是一个古代的漂流瓶……里面的纸条写的什么，嘿嘿，你们猜猜看，猜中奖龙套~~",
    # 6 (orig 49): author's note continues.
    "最后还是求收藏呀，收藏不多，推荐更少，求推荐票，诸位道友，2个月的免费公共期，耳根费了很大的力气才争取过来，还请诸位道友来，用推荐票支持我，一人一票，足以将耳根推上周推荐第一！",
    # 7 (orig 50): author's note, chapter's final segment.
    "我的渴望，就是周推荐第一！！道友们，你们能满足耳根这个要求么，耳根抱拳一拜二拜三拜！",
])


def _ep2_seg_text(index: int) -> str:
    return index_source_segments(EP2_CH2_TAIL_SOURCE)[index - 1].text


def test_ep2_ch2_tail_fixture_has_seven_segments():
    # Guard the fixture itself.
    assert len(index_source_segments(EP2_CH2_TAIL_SOURCE)) == 7


def test_real_ep2_declared_tail_block_passes_all_gates_and_lands_in_ledger():
    """红灯 a）（真实语料，协调方点名要求）：模型对真实 EP2 第2章尾部
    47-50（此夹具中重编号为 4-7，含无信号过渡句 5）申报为 paratext_segments，
    三闸全过，正确入账，且不要求这些段被任何事件的 span 覆盖。事件只覆盖
    1-3（真实故事内容，作者留言开始前的最后剧情节拍）。"""
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 3,
            "source_evidence": [{"segment_index": 1, "quote": _ep2_seg_text(1)}],
            "key_lines": [],
        },
    ]
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(
        EP2_CH2_TAIL_SOURCE, events=events,
        declared_paratext_segments=[4, 5, 6, 7],
    )
    assert errors == []
    assert rejected == []
    assert ledger["paratext"] == [4, 5, 6, 7]
    assert ledger["uncovered"] == []
    assert_prep_pack_coverage_complete(ledger)  # must not raise


def test_mid_body_over_claim_is_vetoed_by_position_gate_and_still_gate_blocked():
    """红灯 b）：模型对一个正文中段（真实语料，段 2 = 原文第45段，"凝气九层，
    仙灵之路..."，与"推荐票"/求票类内容毫无关系的纯粹叙事+心理描写）申报为
    paratext——位置闸否决（不是含最末段的连续尾窗块），回归正文账；events
    刻意覆盖除段 2 外的全部编号，让"漏了一个洞"只能是这一段，门禁照常拦截。"""
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 1,
            "source_evidence": [{"segment_index": 1, "quote": _ep2_seg_text(1)}],
            "key_lines": [],
        },
        {
            "event_id": "ev_002", "order": 2, "from_segment": 3, "to_segment": 7,
            "source_evidence": [{"segment_index": 3, "quote": _ep2_seg_text(3)}],
            "key_lines": [],
        },
    ]
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(
        EP2_CH2_TAIL_SOURCE, events=events,
        declared_paratext_segments=[2],
    )
    assert 2 not in ledger["paratext"]
    assert any(item["segment_index"] == 2 and item["gate"] == "position" for item in rejected)
    assert ledger["uncovered"] == [2]
    with pytest.raises(ValueError) as exc_info:
        assert_prep_pack_coverage_complete(ledger)
    assert "2" in str(exc_info.value)


def test_declared_paratext_segment_also_covered_by_event_span_is_fatal():
    """红灯 c）：段 4（通过位置闸的合法尾窗申报起点）同时被 ev_001 的 span
    [1,4] 覆盖——申报与事件覆盖矛盾，必须致命阻断，不是静默否决。"""
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 4,
            "source_evidence": [
                {"segment_index": 1, "quote": _ep2_seg_text(1)},
                {"segment_index": 2, "quote": _ep2_seg_text(2)},
            ],
            "key_lines": [],
        },
    ]
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(
        EP2_CH2_TAIL_SOURCE, events=events,
        declared_paratext_segments=[4, 5, 6, 7],
    )
    # Gates (a) and (b) both accept segment 4 (structural/tail-window shape
    # is fine, and it's not directly cited as evidence) -- only gate (c)
    # catches it, via the span [1,4] that covers it. Nothing is silently
    # vetoed here; this is a FATAL contradiction, not a rejected declaration.
    assert rejected == []
    assert any("账本自相矛盾" in message for message in errors)
    assert any("4" in message for message in errors if "账本自相矛盾" in message)


def test_declared_tail_window_with_a_gap_is_rejected_by_position_gate():
    """红灯 d）：申报尾窗内非连续（挖洞，跳过 5）——{4,6,7} 不是含最末段的
    连续块（应为 {5,6,7}），位置闸整体否决这批申报，全部回归正文账；没有
    事件覆盖 4/5/6/7，门禁照常拦截并点出全部缺口编号。"""
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 3,
            "source_evidence": [{"segment_index": 1, "quote": _ep2_seg_text(1)}],
            "key_lines": [],
        },
    ]
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(
        EP2_CH2_TAIL_SOURCE, events=events,
        declared_paratext_segments=[4, 6, 7],  # gap at 5
    )
    assert ledger["paratext"] == []
    rejected_indexes = {item["segment_index"] for item in rejected}
    assert rejected_indexes == {4, 6, 7}
    assert all(item["gate"] == "position" for item in rejected)
    assert ledger["uncovered"] == [4, 5, 6, 7]
    with pytest.raises(ValueError) as exc_info:
        assert_prep_pack_coverage_complete(ledger)
    for index in (4, 5, 6, 7):
        assert str(index) in str(exc_info.value)


def test_declared_segment_referenced_by_event_key_line_is_vetoed_by_dependency_gate():
    """补充覆盖 gate (b)（不在协调方点名的 a-d 之列，但属于三闸设计本身，
    单独补一条直接验证，避免它成为没有测试覆盖的死角）：段 7 通过位置闸
    （合法的单段尾窗，含最末段），但被 ev_001 的 key_lines 引用为台词出处
    ——无依赖闸否决，回归正文账；引用放在 key_lines 而非 source_evidence
    是刻意的，避免触发确定性跨度扩展机制，单独隔离 gate (b) 本身。"""
    events = [
        {
            "event_id": "ev_001", "order": 1, "from_segment": 1, "to_segment": 6,
            "source_evidence": [{"segment_index": 1, "quote": _ep2_seg_text(1)}],
            "key_lines": [{"segment_index": 7}],
        },
    ]
    ledger, errors, extensions, rejected = build_prep_pack_span_ledger(
        EP2_CH2_TAIL_SOURCE, events=events,
        declared_paratext_segments=[7],
    )
    assert 7 not in ledger["paratext"]
    assert any(
        item["segment_index"] == 7 and item["gate"] == "dependency" for item in rejected
    )
    assert ledger["uncovered"] == [7]
    with pytest.raises(ValueError) as exc_info:
        assert_prep_pack_coverage_complete(ledger)
    assert "7" in str(exc_info.value)
