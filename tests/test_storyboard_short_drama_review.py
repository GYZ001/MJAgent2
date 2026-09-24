"""短剧节奏档删减复核（``app.production.storyboard_short_drama_review`` +
``storyboard_short_drama_review_items``）：纯函数单元测试 + 顶层编排的简单
形态（忠实档/无删减/复核失败/复核结果处理）。第二遍确定性强制的端到端形态
另见 ``tests/test_storyboard_short_drama_review_second_pass.py``——拆开是本
文件自己的 500 行棘轮零余量，不是关注点不相关。

2026-09-24 第二轮（单元粒度）：条目收集与单元拆分/合并移到
``storyboard_short_drama_review_items``（``_collect_review_items``/
``_split_span_into_items`` 等），本文件同时覆盖两个模块——判据从数据来，
不是关注点跨文件混装。本文件不少测试的夹具原本用「句一。句二。」这类两字
短句：``DROPPABLE_MAX_CHARS=4`` 下这类句子的 ``content_char_count`` 恰好
不超过阈值，会被新的「极短单元不送审」规则跳过，因此凡是要验证"单元被
真正送审/裁剪"的用例都换成了非极短的句子（"速去爬山"一类，用于制造与
业务语义无关但足够长的占位句），只有专门测试"极短单元不送审/放行"的用例
才保留两字短句。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.harness import model_gateway
from app.production.storyboard_beat_sheet import _AiBeat, _AiBeatSheetDraft
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_short_drama_review import (
    _allowed_sets,
    _clip_spans_to_allowed_units,
    _item_payload,
    _must_keep_record,
    _resolve_review_verdicts,
    _review_response_model,
    _revert_disallowed_lines,
    _skipped_review,
    generate_beat_sheet_with_drop_review,
)
from app.production.storyboard_short_drama_review_items import (
    _DropReviewItem,
    _collect_review_items,
    _split_span_into_items,
    _unit_range_text,
)
from app.production.storyboard_short_drama_schemas import _AiShortDramaBeat
from tests.test_storyboard_short_drama import _draft, _range_plan, _sources


def _payload(paratext=()):
    return {"asset_manifest": {"characters": [], "scenes": [], "props": []}, "coverage_ledger": {"paratext": list(paratext)}}


# ---------------------------------------------------------------------------
# _unit_range_text / _split_span_into_items / _collect_review_items
# ---------------------------------------------------------------------------

def test_unit_range_text_extracts_verbatim_unit_range():
    sources = _sources("句一。句二。句三。句四。")
    assert _unit_range_text(1, 2, 3, sources) == "句二。句三。"


def test_unit_range_text_out_of_range_returns_empty():
    sources = _sources("句一。句二。")
    assert _unit_range_text(1, 5, 9, sources) == ""


def test_split_span_into_items_skips_trivial_units():
    """单元 1、2「句一。」「句二。」口播实际字数 2 ≤ DROPPABLE_MAX_CHARS(4)，
    不构成送审条目；单元 3 是真正的一句话，正常送审。"""
    sources = _sources("句一。句二。今天天气好去爬山。")
    span = SimpleNamespace(source_segment_index=1, from_unit=1, to_unit=3, reason="x")
    items = _split_span_into_items(span, sources)
    assert [it.item_id for it in items] == ["unit:1:3"]
    assert items[0].kind == "unit" and items[0].text == "今天天气好去爬山。"


def test_split_span_into_items_merges_large_span_into_groups_of_three():
    """31 个单元 > 30，按位置相邻最多 3 个一组合并送审：10 组整 + 1 组余 1。"""
    sources = _sources("甲乙丙丁戊。" * 31)
    span = SimpleNamespace(source_segment_index=1, from_unit=1, to_unit=31, reason="过场描写，压缩篇幅")
    items = _split_span_into_items(span, sources)
    assert len(items) == 11
    assert [(it.from_unit, it.to_unit) for it in items[:3]] == [(1, 3), (4, 6), (7, 9)]
    assert (items[-1].from_unit, items[-1].to_unit) == (31, 31), "余下 1 个单元单独成组"
    assert items[0].text == "甲乙丙丁戊。" * 3
    assert all(it.kind == "unit" and it.reason == "过场描写，压缩篇幅" for it in items)
    assert all(it.region_label for it in items), "合并送审的条目也带所属区间标识，供模型理解上下文"


def test_collect_review_items_includes_span_and_out_of_span_line():
    sources = _sources("句一。句二。今天天气好去爬山。句四。")
    plan = _range_plan(1, 1, 1, 2, beat_ids=["B1"])
    quote = DialogueQuote(quote_id="Q1", source_segment_index=1, text="句四。", content_chars=6, speaker="老王")
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1], importance="key"),
        _AiShortDramaBeat(beat_id="B2", summary="闲笔", segment_indexes=[1], importance="optional"),
    ]
    draft = _draft(
        [plan], beat_sheet=beat_sheet,
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 3, "to_unit": 3, "reason": "闲笔A", "beat_id": "B2"}],
        dropped_lines=[{"quote_id": "Q1", "reason": "与主线无关", "beat_id": "B2"}],
    )
    items = _collect_review_items(draft, [quote], sources)
    assert [it.item_id for it in items] == ["unit:1:3", "line:Q1"]
    assert items[0].kind == "unit" and items[0].text == "今天天气好去爬山。"
    assert items[1].kind == "line" and items[1].text == "句四。" and items[1].quote_id == "Q1"


def test_collect_review_items_skips_span_forced_and_trivial_lines():
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    quote_short = DialogueQuote(quote_id="Q1", source_segment_index=1, text="句二。", content_chars=2, speaker="老王")
    quote_no_speaker = DialogueQuote(quote_id="Q2", source_segment_index=1, text="句三。", content_chars=6, speaker="")
    draft = _draft(
        [plan],
        dropped_lines=[
            {"quote_id": "Q1", "reason": "随原文区间删减：闲笔", "beat_id": "B1"},  # 区间强制，应跳过
            {"quote_id": "Q1", "reason": "语气词", "beat_id": "B1"},  # 极短，应跳过
            {"quote_id": "Q2", "reason": "无说话人", "beat_id": "B1"},  # 无说话人，应跳过
        ],
    )
    items = _collect_review_items(draft, [quote_short, quote_no_speaker], sources)
    assert items == []


# ---------------------------------------------------------------------------
# _review_response_model：动态 Literal 取值域
# ---------------------------------------------------------------------------

def test_review_response_model_rejects_unknown_item_id():
    model = _review_response_model(["unit:1:1"])
    with pytest.raises(Exception):
        model(items=[{"item_id": "unit:9:9", "must_keep": True, "evidence_quote": "x"}])


def test_review_response_model_accepts_known_item_id():
    model = _review_response_model(["unit:1:1"])
    instance = model(items=[{"item_id": "unit:1:1", "must_keep": False, "evidence_quote": ""}])
    assert instance.items[0].item_id == "unit:1:1"


# ---------------------------------------------------------------------------
# _resolve_review_verdicts：逐条对齐 + 无效判 droppable + 记日志
# ---------------------------------------------------------------------------

def _item(item_id="i1", text="今天天气不错，孟浩要在一周内突破凝气期。") -> _DropReviewItem:
    return _DropReviewItem(item_id=item_id, kind="line", source_segment_index=1, from_unit=1, to_unit=1, text=text, reason="x")


def _verdict(item_id, must_keep, evidence_quote):
    return SimpleNamespace(item_id=item_id, must_keep=must_keep, evidence_quote=evidence_quote)


def test_resolve_verdicts_valid_must_keep_with_verbatim_evidence():
    item = _item()
    response = SimpleNamespace(items=[_verdict("i1", True, "一周内突破凝气期")])
    must_keep, droppable, notes = _resolve_review_verdicts([item], response)
    assert [i.item_id for i, _q in must_keep] == ["i1"]
    assert must_keep[0][1] == "一周内突破凝气期"
    assert droppable == [] and notes == []


def test_resolve_verdicts_must_keep_with_non_verbatim_evidence_becomes_droppable():
    item = _item()
    response = SimpleNamespace(items=[_verdict("i1", True, "这句话原文里根本没有")])
    must_keep, droppable, notes = _resolve_review_verdicts([item], response)
    assert must_keep == []
    assert [i.item_id for i in droppable] == ["i1"]
    assert notes and "归一化后不是原文子串" in notes[0]


def test_resolve_verdicts_evidence_valid_with_missing_or_extra_punctuation():
    """模型引用时常见的标点差异：原文内部有逗号被模型引用时略去、或原文该
    处是逗号却被模型改写成句号——用 textmatch.condense 去标点空白后应仍判
    有效（协调方指出：原精确子串比较会把这类差异误判成不是逐字子串，从而
    错误丢弃本该保留的关键内容，方向与本功能目标相反）。"""
    item = _DropReviewItem(
        item_id="i1", kind="line", source_segment_index=1, from_unit=1, to_unit=1,
        text="一周后，你若到了凝气一层，成了外宗弟子。", reason="x",
    )
    missing_comma = SimpleNamespace(items=[_verdict("i1", True, "一周后你若到了凝气一层")])
    must_keep, _droppable, _notes = _resolve_review_verdicts([item], missing_comma)
    assert [i.item_id for i, _q in must_keep] == ["i1"], "少了原文内部的逗号仍应判有效"
    extra_period = SimpleNamespace(items=[_verdict("i1", True, "成了外宗弟子。")])
    must_keep2, _droppable2, _notes2 = _resolve_review_verdicts([item], extra_period)
    assert [i.item_id for i, _q in must_keep2] == ["i1"], "原文该处是逗号，引用时改成句号仍应判有效"


def test_resolve_verdicts_evidence_from_another_item_or_rewritten_is_invalid():
    """放宽标点比对不等于放宽"必须来自这一条自己的原文"：引用别的条目的原文、
    或改写/概括过的文字（归一化后都不是本条目原文的子串）仍必须判无效。"""
    item = _DropReviewItem(item_id="i1", kind="line", source_segment_index=1, from_unit=1, to_unit=1, text="孟浩扔掉了葫芦。", reason="x")
    other_items_text = SimpleNamespace(items=[_verdict("i1", True, "一周后你若到了凝气一层，成了外宗弟子。")])
    must_keep, droppable, notes = _resolve_review_verdicts([item], other_items_text)
    assert must_keep == [] and [i.item_id for i in droppable] == ["i1"]
    assert notes and "归一化后不是原文子串" in notes[0]
    rewritten = SimpleNamespace(items=[_verdict("i1", True, "孟浩把葫芦丢了")])
    must_keep2, droppable2, _notes2 = _resolve_review_verdicts([item], rewritten)
    assert must_keep2 == [] and [i.item_id for i in droppable2] == ["i1"]


def test_resolve_verdicts_must_keep_with_empty_evidence_becomes_droppable():
    item = _item()
    response = SimpleNamespace(items=[_verdict("i1", True, "")])
    must_keep, droppable, notes = _resolve_review_verdicts([item], response)
    assert must_keep == [] and [i.item_id for i in droppable] == ["i1"]


def test_resolve_verdicts_missing_item_defaults_to_droppable_and_logs():
    item = _item()
    response = SimpleNamespace(items=[])
    must_keep, droppable, notes = _resolve_review_verdicts([item], response)
    assert must_keep == []
    assert [i.item_id for i in droppable] == ["i1"]
    assert notes and "复核未覆盖" in notes[0]


def test_resolve_verdicts_duplicate_item_id_keeps_first_and_logs():
    item = _item()
    response = SimpleNamespace(items=[_verdict("i1", True, "一周内突破凝气期"), _verdict("i1", False, "")])
    must_keep, droppable, notes = _resolve_review_verdicts([item], response)
    assert [i.item_id for i, _q in must_keep] == ["i1"], "取第一次判断（must_keep）"
    assert notes and "重复出现" in notes[0]


def test_resolve_verdicts_false_must_keep_is_droppable():
    item = _item()
    response = SimpleNamespace(items=[_verdict("i1", False, "")])
    must_keep, droppable, notes = _resolve_review_verdicts([item], response)
    assert must_keep == [] and [i.item_id for i in droppable] == ["i1"] and notes == []


# ---------------------------------------------------------------------------
# _item_payload / _must_keep_record / _allowed_sets
# ---------------------------------------------------------------------------

def test_item_payload_unit_and_line_shapes():
    unit_item = _DropReviewItem(item_id="unit:1:2-3", kind="unit", source_segment_index=1, from_unit=2, to_unit=3, text="x", reason="r")
    line_item = _DropReviewItem(item_id="line:Q1", kind="line", source_segment_index=1, from_unit=1, to_unit=1, text="x", reason="r", quote_id="Q1")
    assert _item_payload(unit_item) == {"item_id": "unit:1:2-3", "kind": "unit", "source_segment_index": 1, "text": "x", "from_unit": 2, "to_unit": 3}
    assert _item_payload(line_item) == {"item_id": "line:Q1", "kind": "line", "source_segment_index": 1, "text": "x", "quote_id": "Q1"}


def test_must_keep_record_truncates_long_text_and_keeps_evidence():
    item = _DropReviewItem(item_id="i1", kind="line", source_segment_index=1, from_unit=1, to_unit=1, text="甲" * 80, reason="r")
    record = _must_keep_record(item, "证据")
    assert record["item_id"] == "i1" and record["evidence_quote"] == "证据"
    assert len(record["text"]) == 61 and record["text"].endswith("…")


def test_allowed_sets_splits_by_kind():
    unit_item = _DropReviewItem(item_id="unit:1:2-3", kind="unit", source_segment_index=1, from_unit=2, to_unit=3, text="x", reason="r")
    line_item = _DropReviewItem(item_id="line:Q1", kind="line", source_segment_index=2, from_unit=1, to_unit=1, text="x", reason="r", quote_id="Q1")
    quote_ids, span_units = _allowed_sets([unit_item, line_item])
    assert quote_ids == {"Q1"}
    assert span_units == frozenset({(1, 2), (1, 3)})


# ---------------------------------------------------------------------------
# _revert_disallowed_lines / _clip_spans_to_allowed_units：孤立单测
# ---------------------------------------------------------------------------

def test_revert_disallowed_lines_restores_non_candidate_and_keeps_candidate():
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    q_keep = DialogueQuote(quote_id="Q1", source_segment_index=1, text="句二。", content_chars=6, speaker="老王")
    q_drop = DialogueQuote(quote_id="Q2", source_segment_index=1, text="句三。", content_chars=6, speaker="老王")
    draft = _draft(
        [plan],
        dropped_lines=[{"quote_id": "Q1", "reason": "模型又删了一次", "beat_id": "B2"},
                        {"quote_id": "Q2", "reason": "候选内合法弃置", "beat_id": "B2"}],
    )
    _revert_disallowed_lines(draft, [q_keep, q_drop], sources, allowed_quote_ids={"Q2"})
    assert [d.quote_id for d in draft.dropped_lines] == ["Q2"], "候选内的弃置保持不变"
    assert [k.quote_id for k in draft.kept_lines] == ["Q1"], "候选外的弃置被强制放回"


def test_revert_disallowed_lines_leaves_trivial_untouched():
    sources = _sources("句一。句二。")
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    filler = DialogueQuote(quote_id="Q1", source_segment_index=1, text="嗯", content_chars=1, speaker="老王")
    draft = _draft([plan], dropped_lines=[{"quote_id": "Q1", "reason": "语气词", "beat_id": "B2"}])
    _revert_disallowed_lines(draft, [filler], sources, allowed_quote_ids=set())
    assert [d.quote_id for d in draft.dropped_lines] == ["Q1"], "语气词不受候选约束"
    assert draft.kept_lines == []


def test_clip_spans_to_allowed_units_keeps_only_candidate_units():
    sources = _sources("句一。句二。今天天气好。速去爬山吧。")
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    draft = _draft(
        [plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 4, "reason": "x", "beat_id": "B1"}],
    )
    _clip_spans_to_allowed_units(draft, sources, allowed_span_units=frozenset({(1, 2), (1, 3)}))
    assert len(draft.dropped_source_spans) == 1
    span = draft.dropped_source_spans[0]
    assert (span.from_unit, span.to_unit) == (2, 3), "单元 4 不在候选内且非极短，被裁掉"


def test_clip_spans_to_allowed_units_removes_span_entirely_when_no_overlap():
    sources = _sources("句一。速去爬山吧。")
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    draft = _draft([plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 2, "to_unit": 2, "reason": "x", "beat_id": "B1"}])
    _clip_spans_to_allowed_units(draft, sources, allowed_span_units=frozenset())
    assert draft.dropped_source_spans == []


def test_clip_spans_to_allowed_units_passes_through_trivial_units_even_outside_candidates():
    """极短单元本来就不会被送审（见 storyboard_short_drama_review_items 模块
    docstring 规则 1），第二遍确定性强制不能因为它没出现在候选清单里就误判
    成未经允许的删减而裁掉——否则一次与它无关的必保项会连累撤销这段本来
    合理的极短删减，时长因此不降反升（第五轮真实验证的直接教训）。"""
    sources = _sources("句一。句二。速去爬山吧。")
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    draft = _draft([plan], dropped_source_spans=[
        {"source_segment_index": 1, "from_unit": 2, "to_unit": 3, "reason": "x", "beat_id": "B1"},
    ])
    _clip_spans_to_allowed_units(draft, sources, allowed_span_units=frozenset())
    assert len(draft.dropped_source_spans) == 1
    span = draft.dropped_source_spans[0]
    assert (span.from_unit, span.to_unit) == (2, 2), "单元 2「句二。」极短，放行；单元 3 非极短且候选外，裁掉"


# ---------------------------------------------------------------------------
# generate_beat_sheet_with_drop_review：顶层编排的简单形态
# ---------------------------------------------------------------------------

def _faithful_stub_draft():
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[{"segment_no": 1, "synopsis": "x", "source_segment_indexes": [1]}],
    )


@pytest.mark.asyncio
async def test_faithful_mode_calls_model_once_and_skips_review(monkeypatch):
    calls = []

    async def _stub(*args, **kwargs):
        calls.append(kwargs)
        return _faithful_stub_draft()

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    draft, projected, drop_review = await generate_beat_sheet_with_drop_review(
        episode_id="ep1", episode_no=1, segments=_sources("句一。"), payload=_payload(),
        dialogue_quotes=[], contract_version="2.4.1", adaptation_mode="faithful",
    )
    assert len(calls) == 1, "忠实档只应调用一次模型，不发起复核"
    assert drop_review == _skipped_review()
    assert draft.beat_sheet[0].beat_id == "B1"
    assert projected is None


@pytest.mark.asyncio
async def test_short_drama_without_drops_skips_review(monkeypatch):
    def _no_drop_draft():
        from app.production.storyboard_short_drama_schemas import _AiShortDramaBeatSheetDraft
        return _AiShortDramaBeatSheetDraft(
            beat_sheet=[_AiShortDramaBeat(beat_id="B1", summary="x", segment_indexes=[1], importance="key")],
            segments=[{"segment_no": 1, "synopsis": "x", "source_segment_indexes": [1]}],
        )

    calls = []

    async def _stub(*args, **kwargs):
        calls.append(kwargs)
        return _no_drop_draft()

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    _, _, drop_review = await generate_beat_sheet_with_drop_review(
        episode_id="ep1", episode_no=1, segments=_sources("句一。"), payload=_payload(),
        dialogue_quotes=[], contract_version="2.4.1", adaptation_mode="short_drama",
    )
    assert len(calls) == 1, "没有任何删减时不应发起复核"
    assert drop_review == _skipped_review()


@pytest.mark.asyncio
async def test_review_call_failure_does_not_fail_the_episode(monkeypatch, caplog):
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    first_pass_draft = _draft(
        [plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 1, "reason": "闲笔", "beat_id": "B1"}],
    )
    calls = []

    async def _stub(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return first_pass_draft
        raise RuntimeError("供应商 500")

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    with caplog.at_level(logging.WARNING):
        draft, _projected, drop_review = await generate_beat_sheet_with_drop_review(
            episode_id="ep1", episode_no=1, segments=_sources("速去爬山砍柴。"), payload=_payload(),
            dialogue_quotes=[], contract_version="2.4.1", adaptation_mode="short_drama",
        )
    assert len(calls) == 2, "复核调用失败后不应再尝试第二遍"
    assert draft is first_pass_draft, "复核失败仍返回第一遍草稿，整集不失败"
    assert drop_review == {"status": "failed", "reviewed_count": 1, "must_keep": [], "second_pass": False}
    assert "删减复核调用失败" in caplog.text


@pytest.mark.asyncio
async def test_review_all_droppable_skips_second_pass(monkeypatch):
    plan = _range_plan(1, 1, 1, 1, beat_ids=["B1"])
    first_pass_draft = _draft(
        [plan], dropped_source_spans=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 1, "reason": "闲笔", "beat_id": "B1"}],
    )
    calls = []

    async def _stub(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return first_pass_draft
        model_type = kwargs["model_type"]
        return model_type(items=[{"item_id": "unit:1:1", "must_keep": False, "evidence_quote": ""}])

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    draft, _projected, drop_review = await generate_beat_sheet_with_drop_review(
        episode_id="ep1", episode_no=1, segments=_sources("速去爬山砍柴。"), payload=_payload(),
        dialogue_quotes=[], contract_version="2.4.1", adaptation_mode="short_drama",
    )
    assert len(calls) == 2, "全部判 droppable 时不应触发第二遍"
    assert draft is first_pass_draft
    assert drop_review == {"status": "ok", "reviewed_count": 1, "must_keep": [], "second_pass": False}
