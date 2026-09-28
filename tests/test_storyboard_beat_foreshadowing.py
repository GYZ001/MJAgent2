"""P0-C 伏笔/类型信号保全核验：判据通过/不通过、忠实档与短剧档、
拆段继承 beat_ids 时只认领一次、advisory 文案、零提名三态、
``ForeshadowingSoftCheck`` 语义重试-降级。与
``tests/test_storyboard_beat_causality.py`` 同构（结构相同，去掉
stimulus/时序）。
"""
from __future__ import annotations

from app.production.storyboard_beat_foreshadowing import (
    ForeshadowingSoftCheck,
    _signal_problems,
    foreshadowing_beat_sheet_rules,
    foreshadowing_signal_errors,
    foreshadowing_summary,
    moments_for_segment,
    segment_advisories,
    segment_rule_text,
)
from app.production.storyboard_beat_sheet_schemas import (
    _AiBeat, _AiBeatSheetDraft, _AiForeshadowingBeat, _AiSegmentPlan,
)
from app.production.storyboard_short_drama_schemas import (
    _AiHookNomination, _AiShortDramaBeat, _AiShortDramaBeatSheetDraft,
)
from app.source_excerpt import SourceSegment

_PLACEHOLDER_HOOK = _AiHookNomination(beat_id="B1", evidence_quote="x")


def _sources(*texts: str) -> list[SourceSegment]:
    return [SourceSegment(segment_id=f"s{i}", text=t, start_offset=0, end_offset=len(t)) for i, t in enumerate(texts, start=1)]


def _faithful_draft(beat_sheet, segments, foreshadowing_beats=()):
    return _AiBeatSheetDraft(beat_sheet=beat_sheet, segments=segments, foreshadowing_beats=list(foreshadowing_beats))


def _short_drama_draft(beat_sheet, segments, foreshadowing_beats=(), dropped_source_spans=()):
    return _AiShortDramaBeatSheetDraft(
        beat_sheet=beat_sheet, segments=segments, foreshadowing_beats=list(foreshadowing_beats),
        dropped_source_spans=list(dropped_source_spans),
        opening_hook=_PLACEHOLDER_HOOK, ending_hook=_PLACEHOLDER_HOOK,
    )


_SOURCES = _sources("窗外掠过一只黑鸟。", "远处传来纸鹤般的钟声。")
_BEAT_SHEET = [
    _AiBeat(beat_id="B1", summary="黑鸟跟踪", segment_indexes=[1]),
    _AiBeat(beat_id="B2", summary="钟声异响", segment_indexes=[2]),
]
_SEGMENTS = [
    _AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"]),
    _AiSegmentPlan(segment_no=2, synopsis="x", source_segment_indexes=[2], beat_ids=["B2"]),
]


def _good_signal(**overrides):
    defaults = dict(beat_id="B1", signal_kind="foreshadowing", evidence_quote="窗外掠过一只黑鸟")
    defaults.update(overrides)
    return _AiForeshadowingBeat(**defaults)


# ---------------------------------------------------------------------------
# foreshadowing_beat_sheet_rules：正面陈述，不是关键词黑名单
# ---------------------------------------------------------------------------

def test_rules_are_positive_statements_not_a_blacklist():
    rules = foreshadowing_beat_sheet_rules()
    assert rules and all(isinstance(r, str) and r for r in rules)
    joined = "".join(rules)
    assert "foreshadowing_beats" in joined
    assert "genre_signal" in joined
    assert "黑鸟" in joined  # 真实案例锚点


# ---------------------------------------------------------------------------
# foreshadowing_signal_errors：判据通过/不通过，忠实档与短剧档
# ---------------------------------------------------------------------------

def test_errors_empty_when_everything_lines_up_faithful():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal()])
    assert foreshadowing_signal_errors(draft, _SOURCES) == []


def test_errors_empty_when_everything_lines_up_short_drama():
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="黑鸟跟踪", segment_indexes=[1], importance="key"),
        _AiShortDramaBeat(beat_id="B2", summary="钟声异响", segment_indexes=[2], importance="key"),
    ]
    draft = _short_drama_draft(beat_sheet, _SEGMENTS, [_good_signal()])
    assert foreshadowing_signal_errors(draft, _SOURCES) == []


def test_reports_missing_beat():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal(beat_id="B_GHOST")])
    errors = foreshadowing_signal_errors(draft, _SOURCES)
    assert any("不存在" in e for e in errors)


def test_reports_evidence_quote_not_substring():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal(evidence_quote="凭空编造的一句话")])
    errors = foreshadowing_signal_errors(draft, _SOURCES)
    assert any("evidence_quote 不是节拍 B1 覆盖原文的子串" in e for e in errors)


def test_reports_beat_not_referenced_by_any_segment():
    segments = [_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1, 2], beat_ids=[])]
    draft = _faithful_draft(_BEAT_SHEET, segments, [_good_signal()])
    errors = foreshadowing_signal_errors(draft, _SOURCES)
    assert any("节拍 B1 没有被任何段的 beat_ids 引用" in e for e in errors)


def test_reports_evidence_quote_inside_declared_drop():
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="黑鸟跟踪", segment_indexes=[1], importance="optional"),
        _AiShortDramaBeat(beat_id="B2", summary="钟声异响", segment_indexes=[2], importance="key"),
    ]
    draft = _short_drama_draft(
        beat_sheet, _SEGMENTS, [_good_signal()],
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 1, "reason": "闲笔", "beat_id": "B1"}],
    )
    errors = foreshadowing_signal_errors(draft, _SOURCES)
    assert any("已被声明为删减区间" in e for e in errors)


def test_faithful_mode_has_no_dropped_source_spans_and_still_works():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal()])
    assert not hasattr(draft, "dropped_source_spans")
    assert foreshadowing_signal_errors(draft, _SOURCES) == []


# ---------------------------------------------------------------------------
# _signal_problems：纯函数，直接核对
# ---------------------------------------------------------------------------

def test_signal_problems_pure_function_all_pass():
    beats_by_id = {b.beat_id: b for b in _BEAT_SHEET}
    problems = _signal_problems(_good_signal(), beats_by_id, {"B1"}, _SOURCES, frozenset())
    assert problems == []


# ---------------------------------------------------------------------------
# ForeshadowingSoftCheck：前几次打回、最后一次放行
# ---------------------------------------------------------------------------

def test_soft_check_blocks_first_attempts_then_warns_on_last():
    check = ForeshadowingSoftCheck(retry_limit=2, source_segments=_SOURCES)
    bad = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal(beat_id="B_GHOST")])
    assert check.errors(bad) != [], "第 1 次（attempt 0）应打回"
    assert check.errors(bad) != [], "第 2 次（attempt 1）应打回"
    assert check.errors(bad) == [], "第 3 次（attempt 2 == retry_limit，最后一次）应降级为放行"


def test_soft_check_never_errors_when_already_valid():
    check = ForeshadowingSoftCheck(retry_limit=2, source_segments=_SOURCES)
    good = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal()])
    assert check.errors(good) == []
    assert check.errors(good) == []


# ---------------------------------------------------------------------------
# foreshadowing_summary：零提名三态
# ---------------------------------------------------------------------------

def test_summary_no_signals_nominated():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [])
    assert foreshadowing_summary(draft, _SOURCES) == {"status": "no_signals_nominated", "problem_count": 0}


def test_summary_ok():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal()])
    assert foreshadowing_summary(draft, _SOURCES) == {"status": "ok", "problem_count": 0}


def test_summary_warning_counts_bad_nominations():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal(beat_id="B_GHOST"), _good_signal()])
    summary = foreshadowing_summary(draft, _SOURCES)
    assert summary == {"status": "warning", "problem_count": 1}


def test_summary_covers_faithful_mode_unlike_hook_summary():
    draft = _faithful_draft(_BEAT_SHEET, _SEGMENTS, [_good_signal()])
    summary = foreshadowing_summary(draft, _SOURCES)
    assert summary is not None
    assert summary["status"] == "ok"


# ---------------------------------------------------------------------------
# moments_for_segment：拆段继承 beat_ids 时只认领一次
# ---------------------------------------------------------------------------

def test_moments_for_segment_claims_only_once_across_split_segments():
    signals = [_good_signal()]
    covered: set[str] = set()
    first_claim = moments_for_segment(["B1"], signals, covered)
    assert first_claim == [signals[0]]
    assert covered == {"B1"}
    second_claim = moments_for_segment(["B1"], signals, covered)
    assert second_claim == []


def test_moments_for_segment_ignores_unrelated_beat_ids():
    signals = [_good_signal()]
    covered: set[str] = set()
    claim = moments_for_segment(["B_UNRELATED"], signals, covered)
    assert claim == []
    assert covered == set()


# ---------------------------------------------------------------------------
# segment_rule_text
# ---------------------------------------------------------------------------

def test_segment_rule_text_mentions_evidence_and_not_main_plot():
    rules = segment_rule_text([_good_signal()], ["B1"])
    assert len(rules) == 1
    assert "窗外掠过一只黑鸟" in rules[0]
    assert "不能因为不是主线情节就略过" in rules[0]


def test_segment_rule_text_empty_when_no_signals():
    assert segment_rule_text([], ["B1"]) == []


# ---------------------------------------------------------------------------
# segment_advisories：给出路，不断言代码做不到的判断
# ---------------------------------------------------------------------------

def test_advisory_not_shown_gives_a_way_out_and_does_not_overclaim():
    advisories = segment_advisories([_good_signal()], prompt_text="镜头1：一间空荡荡的房间。")
    assert advisories
    assert all("STORYBOARD_PACK_FORESHADOWING_NOT_SHOWN" in a for a in advisories)
    assert any("分镜台编辑本段镜头稿补上" in a for a in advisories)
    assert any("判不出是否单独成镜" in a for a in advisories)


def test_advisory_empty_when_evidence_is_shown_in_prompt():
    advisories = segment_advisories([_good_signal()], prompt_text="镜头1：窗外掠过一只黑鸟。")
    assert advisories == []
