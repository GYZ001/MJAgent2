"""短剧节奏档（``adaptation_mode="short_drama"``）确定性核验：有效删减四类
排除、台词强制入账、洞不被误填、key 节拍覆盖。忠实档在每一类都必须是无副
作用的空操作——这是「删了什么必须显式声明、留档、可追溯，没声明的原文照旧
零容忍必须被拍到」这句冻结契约的可执行版本。``SegmentCountSoftCap``（含
容量归一化预计段数）另见 ``tests/test_storyboard_short_drama_segment_cap.
py``；区间外弃置台词的 ``beat_id`` 核验另见 ``tests/test_storyboard_short_
drama_beat_guard.py``；区间（``dropped_source_spans``）自身的 beat 归属核验
端到端效果另见 ``tests/test_storyboard_short_drama_span_guard.py``、纯函数级
判据见 ``tests/test_storyboard_short_drama_schemas.py``——都是本文件同一批
改造新增，拆出去是本文件自己的 500 行棘轮零余量，不是关注点不相关。
"""
from __future__ import annotations

from app.production.storyboard_beat_sheet import (
    _AiBeat, _AiBeatSheetDraft, _AiSegmentPlan, _validate_beat_sheet_draft,
)
from app.production.storyboard_beat_sheet_repair import (
    append_segments_for_uncovered_sources, fix_order_and_fill_holes,
)
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_short_drama import (
    finalize_dropped_units,
    key_beat_coverage_errors,
    reconcile_dropped_units,
    required_beat_protected_units,
)
from app.production.storyboard_short_drama_schemas import _AiShortDramaBeat, _AiShortDramaBeatSheetDraft
from app.production.storyboard_segment_ranges import split_source_units
from app.source_excerpt import SourceSegment


def _sources(*texts: str) -> list[SourceSegment]:
    return [SourceSegment(segment_id=f"s{i}", text=t, start_offset=0, end_offset=len(t)) for i, t in enumerate(texts, start=1)]


def _draft(segments, *, dropped_source_spans=(), kept_lines=(), dropped_lines=(), beat_sheet=None):
    if beat_sheet is None:
        # optional（非 key）：绝大多数用例的 dropped_source_spans 都以 B1 为
        # beat_id，区间 beat 归属核验（见 storyboard_short_drama_schemas.
        # verify_dropped_source_spans）要求所属节拍 importance=optional，测试
        # key_beat_coverage_errors 的用例都显式传入自己的 beat_sheet，不受影响。
        beat_sheet = [_AiShortDramaBeat(beat_id="B1", summary="x", segment_indexes=[1], importance="optional")]
    return _AiShortDramaBeatSheetDraft(
        beat_sheet=beat_sheet, segments=segments, kept_lines=list(kept_lines), dropped_lines=list(dropped_lines),
        dropped_source_spans=list(dropped_source_spans),
    )


def _range_plan(no: int, index: int, from_unit: int, to_unit: int, **extra):
    return _AiSegmentPlan(
        segment_no=no, synopsis=extra.pop("synopsis", "x"), source_segment_indexes=[index],
        source_unit_ranges=[{"source_segment_index": index, "from_unit": from_unit, "to_unit": to_unit}], **extra,
    )


# ---------------------------------------------------------------------------
# reconcile_dropped_units：有效删减 = 声明 − 四类保护
# ---------------------------------------------------------------------------

def test_reconcile_basic_drop_with_no_conflicts():
    """区间 beat 归属核验的正面路径（_draft 默认 beat_sheet 是 B1/optional、
    segment_indexes=[1]，与本区间的 source_segment_index 一致）：beat 存在、
    importance=optional、覆盖该原文段——三条都满足，区间生效。"""
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 1)
    draft = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "闲笔", "beat_id": "B1"}])
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset({(1, 2), (1, 3)})


def test_reconcile_out_of_range_declared_span_does_not_invent_units():
    """声明区间整个落在 [1,total] 之外时（模型的 from_unit 直接超过该原文段
    真实单元数），必须是空交集，不能夹紧成"合法范围里的最后一个单元"——那
    是模型从未声明过的单元，夹紧等于凭空多删一个单元。"""
    sources = _sources("句一。句二。句三。")  # 原文段 1 共 3 个单元
    plan = _range_plan(1, 1, 1, 3)  # 全部 3 个单元都已被这一段覆盖，无缺口
    draft = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 5, "to_unit": 10, "reason": "越界", "beat_id": "B1"}])
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset(), "越界声明不产出任何单元，尤其不能是单元 3"


def test_reconcile_partially_out_of_range_span_keeps_only_the_in_range_part():
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 1)
    draft = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 99, "reason": "x", "beat_id": "B1"}])
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset({(1, 2), (1, 3)}), "只保留与 [1,total] 的交集部分（单元 2、3），不夹紧到单元 3 止步"


def test_reconcile_excludes_units_already_covered_by_another_segment():
    sources = _sources("句一。句二。句三。")
    plan1 = _range_plan(1, 1, 1, 1)
    plan2 = _range_plan(2, 1, 2, 3, synopsis="y")
    draft = _draft([plan1, plan2], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "x", "beat_id": "B1"}])
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset()


def test_reconcile_excludes_units_with_kept_dialogue():
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 1)
    quotes = [DialogueQuote(quote_id="Q1", source_segment_index=1, text="句二。", content_chars=2)]
    draft = _draft(
        [plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "x", "beat_id": "B1"}],
        kept_lines=[{"quote_id": "Q1", "segment_no": 1}],
    )
    result = reconcile_dropped_units(draft, sources, quotes, set(), set(), adaptation_mode="short_drama")
    assert result == frozenset({(1, 3)})
    assert [k.quote_id for k in draft.kept_lines] == ["Q1"], "已 kept 的台词不受影响"


def test_reconcile_excludes_required_beat_units():
    text = "句一。（钩子：远处传来警笛声）句三尾。"
    sources = _sources(text)
    units = split_source_units(text)
    assert len(units) == 2, "括号钩子内部无终止符，与后文合并成一个单元"
    plan = _range_plan(1, 1, 1, 1)
    draft = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "x", "beat_id": "B1"}])
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset(), "含作者点名必拍钩子的单元不得被判定为有效删减"


def test_reconcile_protects_required_beat_marker_split_across_two_units_by_an_internal_period():
    """标记内部含句号，被 split_source_units 切成两个单元——逐单元重新跑正则
    的旧写法会两个单元都判不出「完整括号」而漏保护，必须按整段原文的匹配
    位置重叠判断（app.production.screenplay_markers.required_beat_spans）。"""
    text = "句零。（钩子：远处传来警笛声。）句三尾。"
    sources = _sources(text)
    units = split_source_units(text)
    assert len(units) == 3, "标记内部句号把括号钩子切成跨两个单元（第 2、3 个单元）"
    plan = _range_plan(1, 1, 1, 1)
    draft = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "x", "beat_id": "B1"}])
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset(), "钩子标记横跨的两个单元都必须受保护，一个都不能判成有效删减"


def test_required_beat_protected_units_scans_every_source_segment():
    """公开入口（供 storyboard_beat_sheet/storyboard_beat_sheet_repair 判断
    "区间外整句弃置"豁免用）：与 reconcile_dropped_units 内部只扫"有声明
    删减区间的原文段"不同，这里必须扫全部原文段——没有声明删减区间的原文段
    照样可能含必拍标记。"""
    sources = _sources("句一。句二。", "（钩子：警笛声）句尾。")
    assert required_beat_protected_units(sources) == frozenset({(2, 1)})


def test_required_beat_protected_units_empty_without_any_marker():
    sources = _sources("句一。句二。句三。")
    assert required_beat_protected_units(sources) == frozenset()


def test_reconcile_excludes_paratext_and_context_indexes():
    sources = _sources("句一。句二。", "句三。句四。")
    plan = _range_plan(2, 2, 1, 1)
    draft_paratext = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 2, "reason": "x", "beat_id": "B1"}])
    assert reconcile_dropped_units(draft_paratext, sources, [], {1}, set(), adaptation_mode="short_drama") == frozenset()
    draft_context = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 2, "reason": "x", "beat_id": "B1"}])
    assert reconcile_dropped_units(draft_context, sources, [], set(), {1}, adaptation_mode="short_drama") == frozenset()


def test_reconcile_force_drops_quotes_with_traceable_reason_overriding_model_reason():
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 1)
    quotes = [DialogueQuote(quote_id="Q1", source_segment_index=1, text="句二。", content_chars=2)]
    draft = _draft(
        [plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "语气词闲笔", "beat_id": "B1"}],
        dropped_lines=[{"quote_id": "Q1", "reason": "模型自己的理由", "beat_id": "B1"}],
    )
    reconcile_dropped_units(draft, sources, quotes, set(), set(), adaptation_mode="short_drama")
    assert len(draft.dropped_lines) == 1
    assert draft.dropped_lines[0].quote_id == "Q1"
    assert draft.dropped_lines[0].reason == "随原文区间删减：语气词闲笔"


def test_reconcile_is_noop_for_faithful_mode():
    sources = _sources("句一。句二。")
    plan = _range_plan(1, 1, 1, 1)
    draft = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "x", "beat_id": "B1"}])
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="faithful")
    assert result == frozenset()
    assert len(draft.dropped_source_spans) == 1, "faithful 模式不篡改草稿，只是不生效"


def test_reconcile_is_noop_for_a_faithful_draft_without_the_field():
    sources = _sources("句一。")
    plan = _AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1])
    draft = _AiBeatSheetDraft(beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])], segments=[plan])
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset()


# ---------------------------------------------------------------------------
# 洞不被既有修补悄悄补回：fix_order_and_fill_holes / append_segments_for_uncovered_sources
# ---------------------------------------------------------------------------

def test_fix_order_and_fill_holes_leaves_fully_dropped_gap_as_hole():
    plan = _range_plan(1, 1, 1, 1)
    notes = fix_order_and_fill_holes([plan], {1: 3}, dropped_units=frozenset({(1, 2), (1, 3)}))
    assert notes == []
    assert (plan.source_unit_ranges[0].from_unit, plan.source_unit_ranges[0].to_unit) == (1, 1)


def test_fix_order_and_fill_holes_fills_partially_dropped_gap_anyway():
    """尾部缺口 [2,3]：单元 2 已声明删减，单元 3 没有——单元 3 是孤立的非删减
    单元（末段之后没有右邻可以单独吸收它），无法用一段连续范围既覆盖它、又
    不覆盖单元 2，按旧行为整体回填，单元 2 的删减声明随之失效。"""
    plan = _range_plan(1, 1, 1, 1)
    notes = fix_order_and_fill_holes([plan], {1: 3}, dropped_units=frozenset({(1, 2)}))
    assert notes
    assert (plan.source_unit_ranges[0].from_unit, plan.source_unit_ranges[0].to_unit) == (1, 3)


def test_fix_order_and_fill_holes_trailing_gap_only_fills_non_dropped_edge():
    """尾部缺口 [2,3]：单元 2 未声明删减（贴已有范围的边缘）、单元 3 已声明
    删减——只延伸覆盖单元 2，单元 3 继续留空（模型单元范围差一位的常见形态）。"""
    plan = _range_plan(1, 1, 1, 1)
    notes = fix_order_and_fill_holes([plan], {1: 3}, dropped_units=frozenset({(1, 3)}))
    assert notes
    assert (plan.source_unit_ranges[0].from_unit, plan.source_unit_ranges[0].to_unit) == (1, 2)


def test_fix_order_and_fill_holes_between_gap_fills_only_leading_edge():
    """between-gap [2,4]：单元 2 未声明删减（贴前段边缘），单元 3、4 已声明
    删减——只把前段延伸到覆盖单元 2，单元 3、4 继续留空。"""
    plan1 = _range_plan(1, 1, 1, 1)
    plan2 = _range_plan(2, 1, 5, 5, synopsis="y")
    notes = fix_order_and_fill_holes([plan1, plan2], {1: 5}, dropped_units=frozenset({(1, 3), (1, 4)}))
    assert notes
    assert (plan1.source_unit_ranges[0].from_unit, plan1.source_unit_ranges[0].to_unit) == (1, 2)
    assert (plan2.source_unit_ranges[0].from_unit, plan2.source_unit_ranges[0].to_unit) == (5, 5)


def test_fix_order_and_fill_holes_between_gap_fills_only_trailing_edge():
    """between-gap [2,4]：单元 2、3 已声明删减，单元 4 未声明（贴后段边缘）
    ——只把后段延伸到覆盖单元 4，单元 2、3 继续留空。"""
    plan1 = _range_plan(1, 1, 1, 1)
    plan2 = _range_plan(2, 1, 5, 5, synopsis="y")
    notes = fix_order_and_fill_holes([plan1, plan2], {1: 5}, dropped_units=frozenset({(1, 2), (1, 3)}))
    assert notes
    assert (plan1.source_unit_ranges[0].from_unit, plan1.source_unit_ranges[0].to_unit) == (1, 1)
    assert (plan2.source_unit_ranges[0].from_unit, plan2.source_unit_ranges[0].to_unit) == (4, 5)


def test_fix_order_and_fill_holes_between_gap_isolated_middle_unit_falls_back_to_full_fill():
    """between-gap [2,4]：单元 2、4 已声明删减，单元 3 夹在中间且两侧都够不到
    （紧邻它的都是删减单元）——无法用一段连续范围既覆盖单元 3、又不覆盖
    2/4，按旧行为整体回填，这个缺口的删减声明随之失效。"""
    plan1 = _range_plan(1, 1, 1, 1)
    plan2 = _range_plan(2, 1, 5, 5, synopsis="y")
    notes = fix_order_and_fill_holes([plan1, plan2], {1: 5}, dropped_units=frozenset({(1, 2), (1, 4)}))
    assert notes
    assert (plan1.source_unit_ranges[0].from_unit, plan1.source_unit_ranges[0].to_unit) == (1, 4)
    assert (plan2.source_unit_ranges[0].from_unit, plan2.source_unit_ranges[0].to_unit) == (5, 5)


def test_append_segments_skips_fully_dropped_original_segment():
    sources = _sources("句一。", "句二。")
    draft = _draft([_range_plan(1, 1, 1, 1)])
    notes = append_segments_for_uncovered_sources(draft, [], sources, set(), set(), dropped_units=frozenset({(2, 1)}))
    assert notes == []
    assert len(draft.segments) == 1


def test_append_segments_still_appends_when_source_not_declared_dropped():
    sources = _sources("句一。", "句二。")
    draft = _draft([_range_plan(1, 1, 1, 1)])
    notes = append_segments_for_uncovered_sources(draft, [], sources, set(), set(), dropped_units=frozenset())
    assert notes
    assert len(draft.segments) == 2, "没有声明删减的原文段，忠实档行为不变：照旧补段"


def test_append_segments_covers_only_non_dropped_min_to_max():
    """原文段 2 完全没被任何段引用、也没被整段声明删减（只有首尾各一个单元
    被声明删减）：补的段只覆盖「非删减单元的最小到最大」，不是整段 1..units
    ——不能把已声明删减的首尾单元重新补回来。"""
    sources = _sources("句一。", "甲。乙。丙。丁。")
    assert len(split_source_units("甲。乙。丙。丁。")) == 4
    draft = _draft([_range_plan(1, 1, 1, 1)])
    dropped_units = frozenset({(2, 1), (2, 4)})
    notes = append_segments_for_uncovered_sources(draft, [], sources, set(), set(), dropped_units=dropped_units)
    assert notes
    assert len(draft.segments) == 2
    added = draft.segments[1]
    assert (added.source_unit_ranges[0].from_unit, added.source_unit_ranges[0].to_unit) == (2, 3)


# ---------------------------------------------------------------------------
# 端到端：_validate_beat_sheet_draft 接住声明删减且不回填，区间外台词仍受保护
# ---------------------------------------------------------------------------

def test_validate_beat_sheet_draft_accepts_declared_gap_without_refilling_or_new_segments():
    sources = _sources("句一。句二。句三。句四。")
    plan1 = _range_plan(1, 1, 1, 1, synopsis="开场", beat_ids=["B1"])
    plan2 = _range_plan(2, 1, 4, 4, synopsis="收尾", beat_ids=["B1"])
    draft = _draft([plan1, plan2], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "闲笔与重复", "beat_id": "B1"}])
    errors = _validate_beat_sheet_draft(draft, source_segments=sources, dialogue_quotes=[], adaptation_mode="short_drama")
    assert errors == []
    assert len(draft.segments) == 2, "有效删减内的洞不应触发补段"
    assert (draft.dropped_source_spans[0].from_unit, draft.dropped_source_spans[0].to_unit) == (2, 3)


def test_validate_beat_sheet_draft_allows_dropping_dialogue_outside_the_declared_span_in_short_drama():
    """2026-09-24：短剧档放行「区间外、理由非空、beat_id 合法」的整句弃置——
    模型按台词预算主动决定丢弃非关键台词（不落在作者点名必拍单元内）、且
    标注的 beat_id 指向一个真实存在、覆盖这句台词原文段号的 optional 节拍
    时，不再被 restore_undroppable_lines 强制放回 kept_lines，也不再被
    restore_dropped_lines_with_invalid_beat（beat_id 核验，见
    tests/test_storyboard_short_drama_beat_guard.py）放回。旧行为（区间外
    一律不许弃置）仅对忠实档与"落在必拍单元内"两种情形保留，见下面两个测试。
    """
    sources = _sources("句一。句二。句三。句四。")
    plan1 = _range_plan(1, 1, 1, 1, synopsis="开场", beat_ids=["B1"])
    plan2 = _range_plan(2, 1, 4, 4, synopsis="收尾", beat_ids=["B1"])
    quotes = [DialogueQuote(quote_id="Q1", source_segment_index=1, text="句四。", content_chars=6, speaker="老王")]
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1], importance="key"),
        _AiShortDramaBeat(beat_id="B2", summary="寒暄", segment_indexes=[1], importance="optional"),
    ]
    draft = _draft(
        [plan1, plan2], beat_sheet=beat_sheet,
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "闲笔", "beat_id": "B2"}],
        dropped_lines=[{"quote_id": "Q1", "reason": "与主线无关的寒暄，画面已能交代", "beat_id": "B2"}],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=sources, dialogue_quotes=quotes, adaptation_mode="short_drama")
    assert errors == [], "区间外整句台词按理由弃置、beat_id 指向合法 optional 节拍，短剧档放行，不报错"
    assert [d.quote_id for d in draft.dropped_lines] == ["Q1"], "不再被强制放回 kept_lines"
    assert draft.kept_lines == []


def test_validate_beat_sheet_draft_faithful_still_protects_dialogue_outside_the_declared_span():
    """忠实档没有短剧档的新豁免：区间外整句台词仍按既有规则不许弃置（忠实档
    本就没有 dropped_source_spans 机制，这里传的声明区间不生效）。"""
    sources = _sources("句一。句二。句三。句四。")
    plan1 = _range_plan(1, 1, 1, 1, synopsis="开场", beat_ids=["B1"])
    plan2 = _range_plan(2, 1, 4, 4, synopsis="收尾", beat_ids=["B1"])
    quotes = [DialogueQuote(quote_id="Q1", source_segment_index=1, text="句四。", content_chars=6, speaker="老王")]
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])], segments=[plan1, plan2],
        dropped_lines=[{"quote_id": "Q1", "reason": "不重要"}],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=sources, dialogue_quotes=quotes, adaptation_mode="faithful")
    assert errors == [], "区间外整句台词被机械放回 kept_lines，不应再报错"
    assert [k.quote_id for k in draft.kept_lines] == ["Q1"]


def test_validate_beat_sheet_draft_still_protects_required_beat_dialogue_from_ad_hoc_dropping():
    """必拍单元（screenplay_markers.required_beat_spans）里的整句台词，短剧档
    即使没有声明删减区间也不能靠 dropped_lines 逃过——"区间外整句弃置"这条
    新豁免对必拍单元不生效，仍会被机械放回 kept_lines。"""
    text = "句零。（钩子：远处传来警笛声。）句三尾。"
    sources = _sources(text)
    units = split_source_units(text)
    assert len(units) == 3, "标记内部句号把括号钩子切成跨两个单元（第 2、3 个单元）"
    plan = _range_plan(1, 1, 1, 3, synopsis="开场", beat_ids=["B1"])
    # content_chars 必须 > DROPPABLE_MAX_CHARS（4）才算"整句台词"，短到像语气词
    # 的引用不受这条保护——用真实原文子串「远处传来警笛声」（7 字）。
    quotes = [DialogueQuote(quote_id="Q1", source_segment_index=1, text="远处传来警笛声", content_chars=7, speaker="老王")]
    draft = _draft([plan], dropped_lines=[{"quote_id": "Q1", "reason": "模型觉得不重要", "beat_id": "B1"}])
    errors = _validate_beat_sheet_draft(draft, source_segments=sources, dialogue_quotes=quotes, adaptation_mode="short_drama")
    assert errors == [], "被机械放回 kept_lines 后不再报错"
    assert [k.quote_id for k in draft.kept_lines] == ["Q1"]
    assert draft.dropped_lines == []


def test_validate_beat_sheet_draft_faithful_rejects_undeclared_gap_same_as_before():
    """忠实档没有声明机制，缺口照旧被判为漏拍——回归既有零容忍行为。"""
    sources = _sources("句一。句二。句三。句四。")
    plan1 = _range_plan(1, 1, 1, 1, synopsis="开场", beat_ids=["B1"])
    plan2 = _range_plan(2, 1, 4, 4, synopsis="收尾", beat_ids=["B1"])
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])], segments=[plan1, plan2],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=sources, dialogue_quotes=[], adaptation_mode="faithful")
    assert errors == [], "中间缺口应已被 fix_order_and_fill_holes 机械回填，不应报错"
    # 回填规则把缺口并入前一段（第 1 段），不是后一段——见 fix_order_and_fill_holes。
    assert (draft.segments[0].source_unit_ranges[0].from_unit, draft.segments[0].source_unit_ranges[0].to_unit) == (1, 3)
    assert (draft.segments[1].source_unit_ranges[0].from_unit, draft.segments[1].source_unit_ranges[0].to_unit) == (4, 4)


# ---------------------------------------------------------------------------
# finalize_dropped_units：修补跑完之后按最终覆盖状态重算
# ---------------------------------------------------------------------------

def test_finalize_excludes_units_that_repair_ended_up_covering_and_rescues_its_quote():
    """修补把单元 2 回填覆盖之后（模拟 fallback），单元 2 不再是有效删减，它
    先前被 _force_drop_quotes 强制丢弃的台词必须放回 kept_lines——「偏向保留」
    安全网在阶段二的延伸（见 storyboard_short_drama 模块 docstring）。"""
    sources = _sources("句一。句二。句三。句四。句五。")
    dropped_units = frozenset({(1, 2), (1, 3), (1, 4)})
    plan = _range_plan(1, 1, 1, 2, beat_ids=["B1"])  # 修补后单元 2 已经被覆盖（模拟 fallback 回填）
    quote = DialogueQuote(quote_id="Q1", source_segment_index=1, text="句二。", content_chars=6, speaker="老王")
    draft = _draft(
        [plan], dropped_source_spans=[
            {"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "闲笔A", "beat_id": "B1"},
            {"source_segment_index": 1, "from_unit": 3, "to_unit": 4, "reason": "闲笔B", "beat_id": "B1"},
        ],
        dropped_lines=[{"quote_id": "Q1", "reason": "随原文区间删减：闲笔A", "beat_id": "B1"}],
    )
    final_units, key_errors = finalize_dropped_units(draft, dropped_units, sources, [quote])
    assert final_units == frozenset({(1, 3), (1, 4)})
    assert key_errors == []
    spans = draft.dropped_source_spans
    assert len(spans) == 1
    assert (spans[0].from_unit, spans[0].to_unit, spans[0].reason) == (3, 4, "闲笔B")
    assert [d.quote_id for d in draft.dropped_lines] == []
    assert [(k.quote_id, k.segment_no) for k in draft.kept_lines] == [("Q1", 1)]


def test_finalize_does_not_rescue_quotes_whose_unit_remains_dropped():
    sources = _sources("句一。句二。句三。")
    dropped_units = frozenset({(1, 2)})
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    quote = DialogueQuote(quote_id="Q1", source_segment_index=1, text="句二。", content_chars=6, speaker="老王")
    draft = _draft(
        [plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "闲笔", "beat_id": "B1"}],
        dropped_lines=[{"quote_id": "Q1", "reason": "随原文区间删减：闲笔", "beat_id": "B1"}],
    )
    final_units, _ = finalize_dropped_units(draft, dropped_units, sources, [quote])
    assert final_units == frozenset({(1, 2)}), "单元 2 修补后依然没人覆盖，仍是有效删减"
    assert [d.quote_id for d in draft.dropped_lines] == ["Q1"], "仍在有效删减内的台词不应被救回"
    assert draft.kept_lines == []


def test_finalize_is_noop_when_nothing_was_dropped():
    sources = _sources("句一。")
    draft = _draft([_range_plan(1, 1, 1, 1)])
    final_units, key_errors = finalize_dropped_units(draft, frozenset(), sources, [])
    assert final_units == frozenset() and key_errors == []


# ---------------------------------------------------------------------------
# key_beat_coverage_errors
# ---------------------------------------------------------------------------

def test_key_beat_not_covered_by_any_segment_is_rejected():
    draft = _draft(
        [_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=[])],
        beat_sheet=[_AiShortDramaBeat(beat_id="B1", summary="必须拍到", segment_indexes=[1], importance="key")],
    )
    errors = key_beat_coverage_errors(draft, adaptation_mode="short_drama")
    assert len(errors) == 1 and "B1" in errors[0]


def test_key_beat_covered_passes():
    draft = _draft(
        [_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"])],
        beat_sheet=[_AiShortDramaBeat(beat_id="B1", summary="必须拍到", segment_indexes=[1], importance="key")],
    )
    assert key_beat_coverage_errors(draft, adaptation_mode="short_drama") == []


def test_optional_beat_not_covered_does_not_error():
    draft = _draft(
        [_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=[])],
        beat_sheet=[_AiShortDramaBeat(beat_id="B1", summary="可删", segment_indexes=[1], importance="optional")],
    )
    assert key_beat_coverage_errors(draft, adaptation_mode="short_drama") == []


def test_key_beat_coverage_errors_is_noop_for_faithful_mode():
    draft = _draft(
        [_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=[])],
        beat_sheet=[_AiShortDramaBeat(beat_id="B1", summary="x", segment_indexes=[1], importance="key")],
    )
    assert key_beat_coverage_errors(draft, adaptation_mode="faithful") == []
