"""短剧节奏档删减复核——第二遍端到端：必保清单触发第二遍、第二遍模型仍越界时
的确定性强制（放回/裁掉）。纯函数级单测见 ``tests/test_storyboard_short_
drama_review.py``；拆开是那个文件自己的 500 行棘轮零余量。

三个用例共用同一个「smart stub」手法：``model_gateway.chat_structured`` 被
整体替换，第二遍那次调用不是简单返回一个成品草稿，而是真的调用调用方传入的
``validate`` 回调（模拟真实 ``chat_structured`` 在语义校验通过时的行为），
这样才能观察到 ``_enforce_second_pass_constraints`` 挂进 validate 前置步骤后
对草稿的真实修改，而不是仅仅验证「payload 里带了必保清单」这种表面接线。
"""
from __future__ import annotations

import pytest

from app.harness import model_gateway
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_short_drama_review import generate_beat_sheet_with_drop_review
from app.production.storyboard_short_drama_schemas import _AiShortDramaBeat
from tests.test_storyboard_short_drama import _draft, _range_plan, _sources


def _payload():
    return {"asset_manifest": {"characters": [], "scenes": [], "props": []}, "coverage_ledger": {"paratext": []}}


def _beat_sheet():
    return [
        _AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1, 2], importance="key"),
        # segment_indexes 覆盖两个原文段——本文件的用例既有原文段1的区间删减，
        # 也有原文段2的整句台词弃置，都挂在同一个 optional 闲笔节拍下。
        _AiShortDramaBeat(beat_id="B2", summary="闲笔", segment_indexes=[1, 2], importance="optional"),
    ]


def _make_dispatcher(first_pass_draft, review_items, second_pass_factory):
    """按调用顺序分发：第 1 次返回第一遍草稿；第 2 次按 review_items 构造复核
    响应（``model_type`` 由调用方传入，直接用它构造，不需要单独 import 动态
    模型类）；第 3 次调用调用方的 ``validate``，模拟真实 chat_structured 在
    validate 无错误时返回（已校验/已修补的）候选本身。"""
    calls: list[dict] = []

    async def _dispatch(messages, *, validate=None, model_type=None, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs, "validate": validate, "model_type": model_type})
        idx = len(calls) - 1
        if idx == 0:
            return first_pass_draft
        if idx == 1:
            return model_type(items=review_items)
        if idx == 2:
            candidate = second_pass_factory()
            errors = validate(candidate) if validate else []
            assert errors == [], f"第二遍不应触发语义重试：{errors}"
            return candidate
        raise AssertionError(f"未预期的第 {idx + 1} 次模型调用")

    return calls, _dispatch


# ---------------------------------------------------------------------------
# 区间（span）必保：第二遍模型仍把它整块声明删除 -> 确定性裁掉声明、缺口回填
# ---------------------------------------------------------------------------

def _span_case_draft():
    plan1 = _range_plan(1, 1, 1, 2, beat_ids=["B1"])  # 原文段1 单元1-2
    plan2 = _range_plan(2, 2, 1, 2, beat_ids=["B1"])  # 原文段2 单元1-2（整段）
    return _draft(
        [plan1, plan2], beat_sheet=_beat_sheet(),
        dropped_source_spans=[{"source_segment_index": 1, "from_unit": 3, "to_unit": 4, "reason": "次要支线，压缩篇幅", "beat_id": "B2"}],
    )


@pytest.mark.asyncio
async def test_second_pass_triggered_and_still_dropped_span_is_restored(monkeypatch):
    sources = _sources("句一。句二。句三。句四。", "句五。句六。")
    first_pass_draft = _span_case_draft()
    review_items = [{"item_id": "span:1:3-4", "must_keep": True, "evidence_quote": "句三。"}]
    calls, dispatch = _make_dispatcher(first_pass_draft, review_items, _span_case_draft)
    monkeypatch.setattr(model_gateway, "chat_structured", dispatch)

    draft, _projected, drop_review = await generate_beat_sheet_with_drop_review(
        episode_id="ep1", episode_no=1, segments=sources, payload=_payload(),
        dialogue_quotes=[], contract_version="2.4.1", adaptation_mode="short_drama",
    )

    assert len(calls) == 3, "有效必保项必须触发第二遍"
    second_payload = calls[2]["kwargs"]
    assert second_payload["call_meta"]["call_role"] == "storyboard_beat_sheet_second_pass"
    assert drop_review == {
        "status": "ok", "reviewed_count": 1, "second_pass": True,
        "must_keep": [{"item_id": "span:1:3-4", "kind": "span", "text": "句三。句四。", "evidence_quote": "句三。"}],
    }
    # 确定性强制：第二遍模型仍把单元 3-4 声明删除，但候选清单是空的（唯一的
    # 复核条目判了 must_keep），声明被裁空，缺口按既有回填机制并入相邻段。
    assert draft.dropped_source_spans == []
    plan1 = next(p for p in draft.segments if p.source_segment_indexes == [1])
    covered = {u for r in plan1.source_unit_ranges for u in range(r.from_unit, r.to_unit + 1)}
    assert covered == {1, 2, 3, 4}, "必保单元 3-4 必须最终被某一段覆盖"


@pytest.mark.asyncio
async def test_second_pass_task_payload_carries_must_keep_and_candidates(monkeypatch):
    """同一场景下核对 payload 形状本身：must_keep_items/droppable_candidates
    与规则文案都带上了正确的条目——只验证第二遍请求内容，不复核强制结果
    （强制结果已由上一个测试核对）。第一遍还带了一条候选（区间外弃置台词
    Q9，复核判 droppable），确保 droppable_candidates 非空也被正确传递。"""
    sources = _sources("句一。句二。句三。句四。", "句五。句六。")
    quote = DialogueQuote(quote_id="Q9", source_segment_index=2, text="句五。", content_chars=6, speaker="老王")

    def _first_pass_with_candidate():
        plan1 = _range_plan(1, 1, 1, 2, beat_ids=["B1"])
        plan2 = _range_plan(2, 2, 1, 2, beat_ids=["B1"])
        return _draft(
            [plan1, plan2], beat_sheet=_beat_sheet(),
            dropped_source_spans=[{"source_segment_index": 1, "from_unit": 3, "to_unit": 4, "reason": "次要支线", "beat_id": "B2"}],
            dropped_lines=[{"quote_id": "Q9", "reason": "寒暄，画面已交代", "beat_id": "B2"}],
        )

    first_pass_draft = _first_pass_with_candidate()
    review_items = [
        {"item_id": "span:1:3-4", "must_keep": True, "evidence_quote": "句三。"},
        {"item_id": "line:Q9", "must_keep": False, "evidence_quote": ""},
    ]
    calls, dispatch = _make_dispatcher(first_pass_draft, review_items, _first_pass_with_candidate)
    monkeypatch.setattr(model_gateway, "chat_structured", dispatch)

    await generate_beat_sheet_with_drop_review(
        episode_id="ep1", episode_no=1, segments=sources, payload=_payload(),
        dialogue_quotes=[quote], contract_version="2.4.1", adaptation_mode="short_drama",
    )

    import json
    second_user_message = calls[2]["messages"][-1]["content"]
    second_task_payload = json.loads(second_user_message)
    assert second_task_payload["must_keep_items"] == [
        {"item_id": "span:1:3-4", "kind": "span", "source_segment_index": 1, "text": "句三。句四。", "from_unit": 3, "to_unit": 4},
    ]
    assert second_task_payload["droppable_candidates"] == [
        {"item_id": "line:Q9", "kind": "line", "source_segment_index": 2, "text": "句五。", "quote_id": "Q9"},
    ]
    assert any("must_keep_items" in rule and "droppable_candidates" in rule for rule in second_task_payload["rules"])


@pytest.mark.asyncio
async def test_second_pass_ignores_disallowed_new_line_drop_keeps_candidate_drop(monkeypatch):
    """第二遍模型：(a) 必保台词 Q1 已老实保留；(b) 候选台词 Q2 合法弃置；
    (c) 又新弃置了一句从未送审过的台词 Q3——(c) 必须被强制放回，(b) 保持弃置。
    """
    sources = _sources("句一。句二。句三。句四。")
    quotes = [
        DialogueQuote(quote_id="Q1", source_segment_index=1, text="句一。", content_chars=6, speaker="老王"),
        DialogueQuote(quote_id="Q2", source_segment_index=1, text="句二。", content_chars=6, speaker="老王"),
        DialogueQuote(quote_id="Q3", source_segment_index=1, text="句三。", content_chars=6, speaker="老王"),
    ]
    # 本用例只有一个原文段（sources 长度为 1），不能借用 _beat_sheet()——那
    # 是给别的两段式用例准备的，B2 覆盖 segment_indexes=[1,2] 会被判越界。
    beat_sheet = [
        _AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1], importance="key"),
        _AiShortDramaBeat(beat_id="B2", summary="闲笔", segment_indexes=[1], importance="optional"),
    ]

    def _base_plan():
        return _range_plan(1, 1, 1, 4, beat_ids=["B1"])

    first_pass_draft = _draft(
        [_base_plan()], beat_sheet=beat_sheet,
        dropped_lines=[
            {"quote_id": "Q1", "reason": "模型觉得不重要", "beat_id": "B2"},
            {"quote_id": "Q2", "reason": "与主线无关的寒暄", "beat_id": "B2"},
        ],
    )
    review_items = [
        {"item_id": "line:Q1", "must_keep": True, "evidence_quote": "句一"},
        {"item_id": "line:Q2", "must_keep": False, "evidence_quote": ""},
    ]

    def _second_pass_candidate():
        return _draft(
            [_base_plan()], beat_sheet=beat_sheet,
            kept_lines=[{"quote_id": "Q1", "segment_no": 1}],
            dropped_lines=[
                {"quote_id": "Q2", "reason": "与主线无关的寒暄", "beat_id": "B2"},
                {"quote_id": "Q3", "reason": "模型这次新弃置的，从未送审", "beat_id": "B2"},
            ],
        )

    calls, dispatch = _make_dispatcher(first_pass_draft, review_items, _second_pass_candidate)
    monkeypatch.setattr(model_gateway, "chat_structured", dispatch)

    draft, _projected, drop_review = await generate_beat_sheet_with_drop_review(
        episode_id="ep1", episode_no=1, segments=sources, payload=_payload(),
        dialogue_quotes=quotes, contract_version="2.4.1", adaptation_mode="short_drama",
    )

    assert len(calls) == 3
    assert drop_review["second_pass"] is True
    dropped_ids = sorted(d.quote_id for d in draft.dropped_lines)
    kept_ids = sorted(k.quote_id for k in draft.kept_lines)
    assert dropped_ids == ["Q2"], "只有候选内的 Q2 保持弃置"
    assert "Q1" in kept_ids, "必保台词 Q1 本就在 kept_lines"
    assert "Q3" in kept_ids, "候选外的新弃置 Q3 被强制放回"
