"""P0-A 情绪转折/决定性动作因果闭环核验：判据通过/不通过、忠实档与短剧档、
恰好二选一约束、时序、拆段继承 beat_ids 时只认领一次、advisory 文案、
零提名三态、``EmotionalTurnSoftCheck`` 语义重试-降级。写法与
``tests/test_storyboard_short_drama_hooks.py`` 同构。
"""
from __future__ import annotations

from app.production.storyboard_beat_causality import (
    EmotionalTurnSoftCheck,
    _turn_problems,
    causality_beat_sheet_rules,
    causality_summary,
    emotional_turn_errors,
    moments_for_segment,
    segment_advisories,
    segment_rule_text,
)
from app.production.storyboard_beat_sheet_schemas import (
    _AiBeat, _AiBeatSheetDraft, _AiEmotionalTurn, _AiSegmentPlan,
)
from app.production.storyboard_short_drama_schemas import (
    _AiHookNomination, _AiShortDramaBeat, _AiShortDramaBeatSheetDraft,
)
from app.source_excerpt import SourceSegment

_PLACEHOLDER_HOOK = _AiHookNomination(beat_id="B1", evidence_quote="x")


def _sources(*texts: str) -> list[SourceSegment]:
    return [SourceSegment(segment_id=f"s{i}", text=t, start_offset=0, end_offset=len(t)) for i, t in enumerate(texts, start=1)]


def _faithful_draft(beat_sheet, segments, emotional_turns=()):
    return _AiBeatSheetDraft(beat_sheet=beat_sheet, segments=segments, emotional_turns=list(emotional_turns))


def _short_drama_draft(beat_sheet, segments, emotional_turns=(), dropped_source_spans=()):
    return _AiShortDramaBeatSheetDraft(
        beat_sheet=beat_sheet, segments=segments, emotional_turns=list(emotional_turns),
        dropped_source_spans=list(dropped_source_spans),
        opening_hook=_PLACEHOLDER_HOOK, ending_hook=_PLACEHOLDER_HOOK,
    )


_SOURCES = _sources("他握紧行李箱拉杆。", "慢慢点了点头。")
_BEAT_SHEET = [
    _AiBeat(beat_id="B1", summary="收到消息", segment_indexes=[1]),
    _AiBeat(beat_id="B2", summary="下定决心", segment_indexes=[2]),
]
_SEGMENTS = [
    _AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"]),
    _AiSegmentPlan(segment_no=2, synopsis="x", source_segment_indexes=[2], beat_ids=["B2"]),
]


def _good_turn(**overrides):
    defaults = dict(
        beat_id="B2", turn_kind="decisive_action", turn_evidence_quote="慢慢点了点头",
        stimulus_beat_id="B1", stimulus_evidence_quote="他握紧行李箱拉杆", stimulus_missing_reason="",
    )
    defaults.update(overrides)
    return _AiEmotionalTurn(**defaults)


def _no_stimulus_turn(**overrides):
    defaults = dict(
        beat_id="B2", turn_kind="emotional_reaction", turn_evidence_quote="慢慢点了点头",
        stimulus_beat_id="", stimulus_evidence_quote="", stimulus_missing_reason="原文没写清楚动机",
    )
    defaults.update(overrides)
    return _AiEmotionalTurn(**defaults)


# ---------------------------------------------------------------------------
# causality_beat_sheet_rules：正面陈述，不是关键词黑名单
# ---------------------------------------------------------------------------

def test_rules_are_positive_statements_not_a_blacklist():
    rules = causality_beat_sheet_rules()
    assert rules and all(isinstance(r, str) and r for r in rules)
    joined = "".join(rules)
    assert "emotional_turns" in joined
    assert "stimulus_missing_reason" in joined
    assert "恰好二选一" in joined or "恰好给出一个" in joined


# ---------------------------------------------------------------------------
# emotional_turn_errors：判据通过/不通过，忠实档与短剧档
# ---------------------------------------------------------------------------

def test_errors_empty_when_everything_lines_up_faithful():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn()])
    assert emotional_turn_errors(draft, _SOURCES) == []


def test_errors_empty_when_everything_lines_up_short_drama():
    draft = _short_drama_draft(
        [_AiShortDramaBeat(beat_id="B1", summary="收到消息", segment_indexes=[1], importance="key"),
         _AiShortDramaBeat(beat_id="B2", summary="下定决心", segment_indexes=[2], importance="key")],
        _SEGMENTS, [_good_turn()],
    )
    assert emotional_turn_errors(draft, _SOURCES) == []


def test_reports_missing_beat():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn(beat_id="B_GHOST")])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("不存在" in e for e in errors)


def test_reports_evidence_quote_not_substring():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn(turn_evidence_quote="凭空编造的一句话")])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("turn_evidence_quote 不是节拍 B2 覆盖原文的子串" in e for e in errors)


def test_reports_beat_not_referenced_by_any_segment():
    segments = [_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1, 2], beat_ids=["B1"])]
    draft = _faithful_draft(_BEAT_SHEET, segments, [_good_turn()])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("节拍 B2 没有被任何段的 beat_ids 引用" in e for e in errors)


def test_reports_evidence_quote_inside_declared_drop():
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="收到消息", segment_indexes=[1], importance="key"),
        _AiShortDramaBeat(beat_id="B2", summary="下定决心", segment_indexes=[2], importance="optional"),
    ]
    draft = _short_drama_draft(
        beat_sheet, _SEGMENTS, [_good_turn()],
        dropped_source_spans=[{"source_segment_index": 2, "from_unit": 1, "to_unit": 1, "reason": "闲笔", "beat_id": "B2"}],
    )
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("已被声明为删减区间" in e and "turn_evidence_quote" in e for e in errors)


def test_faithful_mode_has_no_dropped_source_spans_and_still_works():
    """忠实档草稿没有 dropped_source_spans 字段，getattr 兜底为空集合，不报错、不抛异常。"""
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn()])
    assert not hasattr(draft, "dropped_source_spans")
    assert emotional_turn_errors(draft, _SOURCES) == []


# ---------------------------------------------------------------------------
# 恰好二选一约束
# ---------------------------------------------------------------------------

def test_reports_when_both_stimulus_and_reason_given():
    turn = _good_turn(stimulus_missing_reason="也写了理由")  # stimulus_beat_id 非空同时 reason 也非空
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [turn])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("恰好给出一个" in e for e in errors)


def test_reports_when_neither_stimulus_nor_reason_given():
    turn = _good_turn(stimulus_beat_id="", stimulus_evidence_quote="", stimulus_missing_reason="")
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [turn])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("恰好给出一个" in e for e in errors)


def test_reports_when_stimulus_beat_id_set_but_quote_empty():
    turn = _good_turn(stimulus_evidence_quote="")
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [turn])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("stimulus_evidence_quote 也必须非空" in e for e in errors)


def test_no_stimulus_with_reason_only_passes():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_no_stimulus_turn()])
    assert emotional_turn_errors(draft, _SOURCES) == []


# ---------------------------------------------------------------------------
# 有 stimulus_beat_id 时的四条判据 + 时序
# ---------------------------------------------------------------------------

def test_reports_stimulus_beat_missing():
    turn = _good_turn(stimulus_beat_id="B_GHOST")
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [turn])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("引用的刺激节拍 B_GHOST 不存在" in e for e in errors)


def test_reports_stimulus_quote_not_substring():
    turn = _good_turn(stimulus_evidence_quote="完全不存在的刺激")
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [turn])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("stimulus_evidence_quote 不是刺激节拍 B1 覆盖原文的子串" in e for e in errors)


def test_reports_stimulus_beat_not_referenced_by_any_segment():
    segments = [
        _AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=[]),
        _AiSegmentPlan(segment_no=2, synopsis="x", source_segment_indexes=[2], beat_ids=["B2"]),
    ]
    draft = _faithful_draft(_BEAT_SHEET, segments, [_good_turn()])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("刺激节拍 B1 没有被任何段的 beat_ids 引用" in e for e in errors)


def test_reports_stimulus_quote_inside_declared_drop():
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="收到消息", segment_indexes=[1], importance="optional"),
        _AiShortDramaBeat(beat_id="B2", summary="下定决心", segment_indexes=[2], importance="key"),
    ]
    draft = _short_drama_draft(
        beat_sheet, _SEGMENTS, [_good_turn()],
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 1, "reason": "闲笔", "beat_id": "B1"}],
    )
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("已被声明为删减区间" in e and "stimulus_evidence_quote" in e for e in errors)


def test_reports_stimulus_timing_after_turn():
    """刺激节拍的 segment_indexes 比转折节拍靠后——时序违规。"""
    beat_sheet = [
        _AiBeat(beat_id="B1", summary="下定决心", segment_indexes=[1]),
        _AiBeat(beat_id="B2", summary="收到消息（其实在后面才发生）", segment_indexes=[2]),
    ]
    segments = [
        _AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"]),
        _AiSegmentPlan(segment_no=2, synopsis="x", source_segment_indexes=[2], beat_ids=["B2"]),
    ]
    turn = _AiEmotionalTurn(
        beat_id="B1", turn_kind="decisive_action", turn_evidence_quote="他握紧行李箱拉杆",
        stimulus_beat_id="B2", stimulus_evidence_quote="慢慢点了点头", stimulus_missing_reason="",
    )
    draft = _faithful_draft(beat_sheet, segments, [turn])
    errors = emotional_turn_errors(draft, _SOURCES)
    assert any("时序晚于" in e for e in errors)


# ---------------------------------------------------------------------------
# _turn_problems：纯函数，直接核对
# ---------------------------------------------------------------------------

def test_turn_problems_pure_function_all_pass():
    beats_by_id = {b.beat_id: b for b in _BEAT_SHEET}
    problems = _turn_problems(_good_turn(), beats_by_id, {"B1", "B2"}, _SOURCES, frozenset())
    assert problems == []


# ---------------------------------------------------------------------------
# EmotionalTurnSoftCheck：前几次打回、最后一次放行
# ---------------------------------------------------------------------------

def test_soft_check_blocks_first_attempts_then_warns_on_last():
    check = EmotionalTurnSoftCheck(retry_limit=2, source_segments=_SOURCES)
    bad = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn(beat_id="B_GHOST")])
    assert check.errors(bad) != [], "第 1 次（attempt 0）应打回"
    assert check.errors(bad) != [], "第 2 次（attempt 1）应打回"
    assert check.errors(bad) == [], "第 3 次（attempt 2 == retry_limit，最后一次）应降级为放行"


def test_soft_check_never_errors_when_already_valid():
    check = EmotionalTurnSoftCheck(retry_limit=2, source_segments=_SOURCES)
    good = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn()])
    assert check.errors(good) == []
    assert check.errors(good) == []


# ---------------------------------------------------------------------------
# causality_summary：零提名三态
# ---------------------------------------------------------------------------

def test_summary_no_turns_nominated():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [])
    assert causality_summary(draft, _SOURCES) == {"status": "no_turns_nominated", "problem_count": 0, "missing_stimulus_count": 0}


def test_summary_ok():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn()])
    assert causality_summary(draft, _SOURCES) == {"status": "ok", "problem_count": 0, "missing_stimulus_count": 0}


def test_summary_warning_counts_bad_nominations():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn(beat_id="B_GHOST"), _good_turn()])
    summary = causality_summary(draft, _SOURCES)
    assert summary == {"status": "warning", "problem_count": 1, "missing_stimulus_count": 0}


def test_summary_counts_missing_stimulus_without_marking_nomination_bad():
    """原文没写诱因是剧本层问题：如实声明的提名合法（status 仍 ok），但要单独计数给面板看。"""
    missing = _good_turn(stimulus_beat_id="", stimulus_evidence_quote="", stimulus_missing_reason="原文没写她为何改变主意")
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [missing, _good_turn()])
    assert causality_summary(draft, _SOURCES) == {"status": "ok", "problem_count": 0, "missing_stimulus_count": 1}


def test_summary_covers_faithful_mode_unlike_hook_summary():
    """两个新 summary 函数对忠实档一视同仁地计算，不像 hook_summary 那样返回 None。"""
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_turn()])
    summary = causality_summary(draft, _SOURCES)
    assert summary is not None
    assert summary["status"] == "ok"


# ---------------------------------------------------------------------------
# moments_for_segment：拆段继承 beat_ids 时只认领一次
# ---------------------------------------------------------------------------

def test_moments_for_segment_claims_only_once_across_split_segments():
    """容量拆分后续段完整继承原段 beat_ids（storyboard_capacity_normalize.
    _split_one_segment），同一处决定性动作不能被连续几个续段反复认领。"""
    turns = [_good_turn()]
    covered: set[str] = set()
    first_segment_claim = moments_for_segment(["B1", "B2"], turns, covered)
    assert first_segment_claim == [turns[0]]
    assert covered == {"B2"}
    second_segment_claim = moments_for_segment(["B1", "B2"], turns, covered)
    assert second_segment_claim == []


def test_moments_for_segment_ignores_unrelated_beat_ids():
    turns = [_good_turn()]
    covered: set[str] = set()
    claim = moments_for_segment(["B_UNRELATED"], turns, covered)
    assert claim == []
    assert covered == set()


# ---------------------------------------------------------------------------
# segment_rule_text：三分支
# ---------------------------------------------------------------------------

def test_segment_rule_text_no_stimulus_branch():
    rules = segment_rule_text([_no_stimulus_turn()], ["B2"])
    assert len(rules) == 1
    assert "没有写出" in rules[0] or "没有明确诱因" in rules[0] or "没有写出明确诱因" in rules[0]
    assert "不要编造原文没有的刺激" in rules[0]


def test_segment_rule_text_stimulus_in_same_segment_branch():
    rules = segment_rule_text([_good_turn()], ["B1", "B2"])
    assert len(rules) == 1
    assert "先把刺激画成一镜" in rules[0]


def test_segment_rule_text_stimulus_in_earlier_segment_branch():
    rules = segment_rule_text([_good_turn()], ["B2"])
    assert len(rules) == 1
    assert "已在更早段落交代过" in rules[0]


# ---------------------------------------------------------------------------
# segment_advisories：advisory 文案给出路，不断言代码做不到的判断
# ---------------------------------------------------------------------------

def test_advisory_no_stimulus_gives_a_way_out():
    advisories = segment_advisories([_no_stimulus_turn()], prompt_text="镜头1：他慢慢点了点头。")
    assert any("STORYBOARD_PACK_EMOTIONAL_TURN_NO_STIMULUS" in a for a in advisories)
    assert any("原文层面的问题" in a and "重跑本集分镜" in a for a in advisories)


def test_advisory_not_shown_gives_a_way_out_and_does_not_overclaim():
    advisories = segment_advisories([_good_turn()], prompt_text="镜头1：一间空荡荡的房间。")
    not_shown = [a for a in advisories if "STORYBOARD_PACK_EMOTIONAL_TURN_NOT_SHOWN" in a]
    assert not_shown
    assert any("分镜台编辑本段镜头稿补上" in a for a in not_shown)
    assert any("判不出是否单独成镜" in a for a in not_shown)


def test_advisory_empty_when_evidence_is_shown_in_prompt():
    prompt = "镜头1：他握紧行李箱拉杆。镜头2：慢慢点了点头。"
    advisories = segment_advisories([_good_turn()], prompt_text=prompt)
    assert advisories == []


def test_advisory_moments_skips_stimulus_check_when_stimulus_is_in_an_earlier_segment():
    """刺激在更早段落时，本段告警只核对转折本身，不拿本段镜头稿核对刺激原句（曾误报）。"""
    from app.production.storyboard_beat_causality import advisory_moments

    turn = _good_turn(beat_id="B2", stimulus_beat_id="B1")
    claimed = advisory_moments(["B2"], [turn], set())
    assert [t.beat_id for t in claimed] == ["B2"]
    assert claimed[0].stimulus_evidence_quote == ""
    assert turn.stimulus_evidence_quote == "他握紧行李箱拉杆"  # 原提名不被改动
    prompt = "镜头2：特写 她慢慢点了点头。"
    assert segment_advisories(claimed, prompt) == []
    assert any("他握紧行李箱拉杆" in a for a in segment_advisories([turn], prompt))


def test_advisory_moments_keeps_stimulus_check_when_stimulus_is_in_this_segment():
    from app.production.storyboard_beat_causality import advisory_moments

    turn = _good_turn(beat_id="B2", stimulus_beat_id="B1")
    claimed = advisory_moments(["B1", "B2"], [turn], set())
    assert claimed[0].stimulus_evidence_quote == "他握紧行李箱拉杆"
