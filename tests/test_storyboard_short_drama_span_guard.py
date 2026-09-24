"""区间（``dropped_source_spans``）的 beat 归属核验对整条流水线的影响
（2026-09-24）：区间标 key 节拍/beat 不覆盖其原文段——整条区间不认，其中
的台词不被强制弃置，原文单元退回既有洞检测/补段安全网被机械补回。纯函数级
核验（``verify_dropped_source_spans`` 存在性/importance/segment_indexes 三条
判据）见 ``tests/test_storyboard_short_drama_schemas.py``；本文件测这三条
判据接入 ``reconcile_dropped_units``/``_validate_beat_sheet_draft`` 之后的
端到端效果。从 ``tests/test_storyboard_short_drama.py`` 拆出（同一批改造，
那个文件自己的 500 行棘轮已无余量，见其模块 docstring），``_draft``/
``_range_plan``/``_sources`` 复用同一份夹具，与 ``tests/test_storyboard_
short_drama_beat_guard.py``/``_segment_cap.py`` 同一种既有写法。
"""
from __future__ import annotations

from app.production.storyboard_beat_sheet import _validate_beat_sheet_draft
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_short_drama import reconcile_dropped_units
from app.production.storyboard_short_drama_schemas import _AiShortDramaBeat
from tests.test_storyboard_short_drama import _draft, _range_plan, _sources


def test_reconcile_rejects_span_pointing_to_key_beat():
    """区间标 key 节拍——整条区间不认，不产出任何单元（2026-09-24 B 机沙箱
    第三轮真实验证的直接诱因：模型用这条豁免整块删掉了目标铺垫台词）。"""
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 1)
    beat_sheet = [_AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1], importance="key")]
    draft = _draft(
        [plan], beat_sheet=beat_sheet,
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "目标铺垫", "beat_id": "B1"}],
    )
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset(), "beat_id 指向 key 节拍，整条区间按未声明处理"


def test_reconcile_rejects_span_when_beat_does_not_cover_segment():
    """区间的 source_segment_index 不在所属节拍的 segment_indexes 里——beat
    本身合法且 optional，但管的不是这个原文段，整条区间仍不认。"""
    sources = _sources("句一。句二。", "另一段。")
    plan = _range_plan(1, 1, 1, 1)
    other_segment_beat = _AiShortDramaBeat(beat_id="B2", summary="别的段", segment_indexes=[2], importance="optional")
    beat_sheet = [_AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1], importance="key"), other_segment_beat]
    draft = _draft(
        [plan], beat_sheet=beat_sheet,
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "x", "beat_id": "B2"}],
    )
    result = reconcile_dropped_units(draft, sources, [], set(), set(), adaptation_mode="short_drama")
    assert result == frozenset(), "beat 的 segment_indexes 未覆盖这个原文段，整条区间不认"


def test_reconcile_rejected_span_does_not_force_drop_its_quotes():
    """区间被拒后，覆盖在其范围内的台词不会被 _force_drop_quotes 强制并入
    dropped_lines——它们退回既有的逐句台账/beat_guard 判断，不受区间机制
    牵连（Goal A：不被强制弃置）。"""
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 1)
    beat_sheet = [_AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1], importance="key")]
    quote = DialogueQuote(quote_id="Q1", source_segment_index=1, text="句二。", content_chars=6, speaker="老王")
    draft = _draft(
        [plan], beat_sheet=beat_sheet,
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "目标铺垫", "beat_id": "B1"}],
    )
    reconcile_dropped_units(draft, sources, [quote], set(), set(), adaptation_mode="short_drama")
    assert draft.dropped_lines == [], "被拒区间不强制丢弃台词"
    assert draft.kept_lines == []


def test_validate_beat_sheet_draft_refills_gap_when_span_beat_is_key_not_optional():
    """区间标 key 节拍不认之后，原文单元退回既有洞检测安全网，被机械补回
    某个段的覆盖范围（Goal A 第三点：单元被补回），不再留空洞、也不报错。"""
    sources = _sources("句一。句二。句三。句四。")
    plan1 = _range_plan(1, 1, 1, 1, synopsis="开场", beat_ids=["B1"])
    plan2 = _range_plan(2, 1, 4, 4, synopsis="收尾", beat_ids=["B1"])
    beat_sheet = [_AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1], importance="key")]
    draft = _draft(
        [plan1, plan2], beat_sheet=beat_sheet,
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "目标铺垫", "beat_id": "B1"}],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=sources, dialogue_quotes=[], adaptation_mode="short_drama")
    assert errors == [], "被拒区间退回既有安全网机械回填，不应报错"
    covered = {u for plan in draft.segments for r in plan.source_unit_ranges for u in range(r.from_unit, r.to_unit + 1)}
    assert covered == {1, 2, 3, 4}, "单元 2、3 必须被某个段补回覆盖，不再是洞"
