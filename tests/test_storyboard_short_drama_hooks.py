"""短剧节奏档开篇/结尾钩子（``opening_hook``/``ending_hook``）确定性核验：
四条判据（beat 存在、importance=key、被首/末段引用、evidence_quote 是节拍
覆盖原文的子串）、加固项（证据句所在单元不在删减区间内）、语义重试-降级
（``HookBeatSoftCheck``）、留档事后重算（``hook_summary``）。``_draft``/
``_range_plan``/``_sources`` 复用 ``tests/test_storyboard_short_drama.py``
的既有夹具，与 ``test_storyboard_short_drama_segment_cap.py`` 同一种写法。
"""
from __future__ import annotations

from app.production.storyboard_short_drama_hooks import (
    HookBeatSoftCheck,
    _hook_problems,
    _quote_unit_keys,
    hook_beat_errors,
    hook_summary,
    short_drama_hook_rules,
)
from app.production.storyboard_short_drama_schemas import _AiHookNomination, _AiShortDramaBeat
from tests.test_storyboard_short_drama import _draft, _range_plan, _sources

_SOURCES = _sources("老王早起去砍柴。天色微亮路难行。", "山下传来阵阵鸡鸣。远处飘来阵阵炊烟。")
_BEAT_SHEET = [
    _AiShortDramaBeat(beat_id="B1", summary="开篇冲突", segment_indexes=[1], importance="key"),
    _AiShortDramaBeat(beat_id="B2", summary="结尾悬念", segment_indexes=[2], importance="key"),
    _AiShortDramaBeat(beat_id="B3", summary="闲笔", segment_indexes=[1], importance="optional"),
]


def _plans():
    return [
        _range_plan(1, 1, 1, 2, beat_ids=["B1"]),
        _range_plan(2, 2, 1, 2, beat_ids=["B2"]),
    ]


def _good_draft(**overrides):
    opening = overrides.pop("opening_hook", _AiHookNomination(beat_id="B1", evidence_quote="老王早起去砍柴"))
    ending = overrides.pop("ending_hook", _AiHookNomination(beat_id="B2", evidence_quote="远处飘来阵阵炊烟"))
    return _draft(_plans(), beat_sheet=_BEAT_SHEET, opening_hook=opening, ending_hook=ending, **overrides)


# ---------------------------------------------------------------------------
# short_drama_hook_rules：正面陈述，不是关键词黑名单
# ---------------------------------------------------------------------------

def test_hook_rules_are_positive_statements_not_a_blacklist():
    rules = short_drama_hook_rules()
    assert rules and all(isinstance(r, str) and r for r in rules)
    joined = "".join(rules)
    assert "opening_hook" in joined and "ending_hook" in joined
    assert "开篇" in joined and "结尾" in joined


# ---------------------------------------------------------------------------
# hook_beat_errors：四条判据 + 加固项，忠实档空操作
# ---------------------------------------------------------------------------

def test_hook_beat_errors_faithful_mode_is_noop():
    draft = _good_draft()
    assert hook_beat_errors(draft, _SOURCES, adaptation_mode="faithful") == []


def test_hook_beat_errors_passes_when_everything_lines_up():
    assert hook_beat_errors(_good_draft(), _SOURCES, adaptation_mode="short_drama") == []


def test_hook_beat_errors_reports_missing_beat():
    draft = _good_draft(opening_hook=_AiHookNomination(beat_id="B_NOT_EXIST", evidence_quote="x"))
    errors = hook_beat_errors(draft, _SOURCES, adaptation_mode="short_drama")
    assert any("opening_hook" in e and "不存在" in e for e in errors)


def test_hook_beat_errors_reports_non_key_beat():
    draft = _good_draft(opening_hook=_AiHookNomination(beat_id="B3", evidence_quote="老王早起去砍柴"))
    errors = hook_beat_errors(draft, _SOURCES, adaptation_mode="short_drama")
    assert any("importance=key" in e for e in errors)


def test_hook_beat_errors_reports_not_referenced_by_first_or_last_segment():
    """B2 是 key 节拍，但没有被第 1 段（segments[0]）的 beat_ids 引用。"""
    draft = _good_draft(opening_hook=_AiHookNomination(beat_id="B2", evidence_quote="山下传来阵阵鸡鸣"))
    errors = hook_beat_errors(draft, _SOURCES, adaptation_mode="short_drama")
    assert any("没有被对应段" in e for e in errors)


def test_hook_beat_errors_reports_evidence_quote_not_substring():
    draft = _good_draft(opening_hook=_AiHookNomination(beat_id="B1", evidence_quote="凭空编造的一句话"))
    errors = hook_beat_errors(draft, _SOURCES, adaptation_mode="short_drama")
    assert any("不是节拍 B1 覆盖原文的子串" in e for e in errors)


def test_hook_beat_errors_reports_quote_unit_inside_declared_drop():
    """加固项：证据句所在的原文单元已被声明为删减区间——钩子与删减自相矛盾。
    B1 覆盖原文段 1，第 1 单元「老王早起去砍柴。」整段声明删除。"""
    draft = _good_draft(
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 1, "reason": "闲笔", "beat_id": "B3"}],
    )
    errors = hook_beat_errors(draft, _SOURCES, adaptation_mode="short_drama")
    assert any("已被声明为删减区间" in e for e in errors)


def test_hook_beat_errors_ending_quote_still_valid_when_only_opening_span_dropped():
    """加固项只命中真正重叠的钩子，不误伤另一侧——结尾钩子的证据句在段 2，
    段 1 的删减声明不应牵连它。"""
    draft = _good_draft(
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 1, "reason": "闲笔", "beat_id": "B3"}],
    )
    errors = hook_beat_errors(draft, _SOURCES, adaptation_mode="short_drama")
    assert not any(e.startswith("ending_hook") for e in errors)


def test_hook_beat_errors_not_fooled_by_duplicate_phrase_in_unrelated_dropped_unit():
    """加固项不消歧位置，原文里字面重复的短句会让 evidence_quote 同时命中
    多个候选单元——只要存在一个未被删减的候选，就不判定为自相矛盾，避免
    合法钩子被另一句无关、且被正当声明删除的重复台词连累打回。"""
    sources = _sources("他说我爱你。他又说我爱你。", "远处飘来阵阵炊烟。")
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="开篇冲突", segment_indexes=[1], importance="key"),
        _AiShortDramaBeat(beat_id="B2", summary="结尾悬念", segment_indexes=[2], importance="key"),
        _AiShortDramaBeat(beat_id="B3", summary="重复的第二次表白", segment_indexes=[1], importance="optional"),
    ]
    draft = _draft(
        [_range_plan(1, 1, 1, 2, beat_ids=["B1"]), _range_plan(2, 2, 1, 1, beat_ids=["B2"])],
        beat_sheet=beat_sheet,
        opening_hook=_AiHookNomination(beat_id="B1", evidence_quote="我爱你"),
        ending_hook=_AiHookNomination(beat_id="B2", evidence_quote="远处飘来阵阵炊烟"),
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "重复的第二次表白，删掉", "beat_id": "B3"}],
    )
    errors = hook_beat_errors(draft, sources, adaptation_mode="short_drama")
    assert not any("已被声明为删减区间" in e for e in errors)


def test_hook_beat_errors_reports_when_all_candidate_units_are_dropped():
    """反例：evidence_quote 的全部候选单元都落在删减区间（没有安全落点）时，
    加固项仍要判定自相矛盾——上一个测试的「存在安全单元即放行」不能被滥用
    成整段重复短句都删了也放行。"""
    sources = _sources("他说我爱你。他又说我爱你。", "远处飘来阵阵炊烟。")
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="开篇冲突", segment_indexes=[1], importance="key"),
        _AiShortDramaBeat(beat_id="B2", summary="结尾悬念", segment_indexes=[2], importance="key"),
    ]
    draft = _draft(
        [_range_plan(1, 1, 1, 2, beat_ids=["B1"]), _range_plan(2, 2, 1, 1, beat_ids=["B2"])],
        beat_sheet=beat_sheet,
        opening_hook=_AiHookNomination(beat_id="B1", evidence_quote="我爱你"),
        ending_hook=_AiHookNomination(beat_id="B2", evidence_quote="远处飘来阵阵炊烟"),
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 2, "reason": "整段删掉", "beat_id": "B1"}],
    )
    errors = hook_beat_errors(draft, sources, adaptation_mode="short_drama")
    assert any("opening_hook" in e and "已被声明为删减区间" in e for e in errors)


# ---------------------------------------------------------------------------
# _quote_unit_keys：双向 condense 子串包含判定单元覆盖
# ---------------------------------------------------------------------------

def test_quote_unit_keys_finds_the_unit_the_quote_lives_in():
    # _SOURCES[0].text == "老王早起去砍柴。天色微亮路难行。"
    assert _quote_unit_keys("老王早起去砍柴", [1], _SOURCES) == {(1, 1)}
    assert _quote_unit_keys("天色微亮路难行", [1], _SOURCES) == {(1, 2)}


def test_quote_unit_keys_empty_when_quote_not_found():
    assert _quote_unit_keys("完全不存在的句子", [1], _SOURCES) == set()


# ---------------------------------------------------------------------------
# HookBeatSoftCheck：语义重试-降级（同 SegmentCountSoftCap 同一套让步策略）
# ---------------------------------------------------------------------------

def test_soft_check_blocks_first_attempts_then_warns_on_last():
    check = HookBeatSoftCheck(adaptation_mode="short_drama", retry_limit=2, source_segments=_SOURCES)
    bad = _good_draft(opening_hook=_AiHookNomination(beat_id="B_NOT_EXIST", evidence_quote="x"))
    assert check.errors(bad) != [], "第 1 次（attempt 0）应打回"
    assert check.errors(bad) != [], "第 2 次（attempt 1）应打回"
    assert check.errors(bad) == [], "第 3 次（attempt 2 == retry_limit，最后一次）应降级为放行"


def test_soft_check_never_errors_when_already_valid():
    check = HookBeatSoftCheck(adaptation_mode="short_drama", retry_limit=2, source_segments=_SOURCES)
    good = _good_draft()
    assert check.errors(good) == []
    assert check.errors(good) == []


def test_soft_check_faithful_mode_always_empty():
    check = HookBeatSoftCheck(adaptation_mode="faithful", retry_limit=2, source_segments=_SOURCES)
    bad = _good_draft(opening_hook=_AiHookNomination(beat_id="B_NOT_EXIST", evidence_quote="x"))
    assert check.errors(bad) == []


# ---------------------------------------------------------------------------
# hook_summary：留档事后重算，忠实档/缺字段恒 None
# ---------------------------------------------------------------------------

def test_hook_summary_faithful_mode_returns_none():
    assert hook_summary(_good_draft(), _SOURCES, adaptation_mode="faithful") is None


def test_hook_summary_ok_when_everything_passes():
    summary = hook_summary(_good_draft(), _SOURCES, adaptation_mode="short_drama")
    assert summary == {
        "status": "ok",
        "opening": {"beat_id": "B1", "evidence_quote": "老王早起去砍柴", "problems": []},
        "ending": {"beat_id": "B2", "evidence_quote": "远处飘来阵阵炊烟", "problems": []},
    }


def test_hook_summary_warning_when_opening_fails():
    draft = _good_draft(opening_hook=_AiHookNomination(beat_id="B_NOT_EXIST", evidence_quote="x"))
    summary = hook_summary(draft, _SOURCES, adaptation_mode="short_drama")
    assert summary["status"] == "warning"
    assert summary["opening"]["problems"] and summary["ending"]["problems"] == []


def test_hook_summary_truncates_long_evidence_quote_for_display():
    long_quote = "老王早起去砍柴" * 20
    draft = _good_draft(opening_hook=_AiHookNomination(beat_id="B1", evidence_quote=long_quote))
    summary = hook_summary(draft, _SOURCES, adaptation_mode="short_drama")
    assert len(summary["opening"]["evidence_quote"]) == 60


# ---------------------------------------------------------------------------
# _hook_problems：纯函数，直接核对（不经 _draft 包装）
# ---------------------------------------------------------------------------

def test_hook_problems_pure_function_all_pass():
    beats_by_id = {b.beat_id: b for b in _BEAT_SHEET}
    nomination = _AiHookNomination(beat_id="B1", evidence_quote="老王早起去砍柴")
    problems = _hook_problems(nomination, beats_by_id, {"B1"}, _SOURCES, frozenset())
    assert problems == []
