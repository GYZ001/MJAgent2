"""全集服装表同场可见状态变化复核（``app.production.storyboard_wardrobe_
recheck``）：核验分支 + 合并逻辑 + 编排层真实接线 + 失败不阻断整集。真实回归
见该模块 docstring（proj_ca86b15ab7d7《顾念长安》EP1「替她把外套的扣子扣好」
5/5 次漏记）。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.harness import model_gateway
from app.production.storyboard_beat_sheet import _AiBeat, _AiBeatSheetDraft
from app.production.storyboard_beat_sheet_schemas import _AiSegmentPlan, _AiWardrobeState
from app.production.storyboard_wardrobe_recheck import (
    _merge_recheck_changes,
    _quote_is_verbatim,
    _segment_to_beat,
    _skipped_recheck,
    _validate_recheck_response,
    generate_beat_sheet_with_wardrobe_recheck,
    recheck_wardrobe_mid_scene_changes,
)
from tests.test_storyboard_short_drama import _sources

_SEGMENT_1_TEXT = "顾屿站起身，先替她把外套的扣子扣好，才说要陪她过去看看。"


def _payload(characters):
    return {"asset_manifest": {"characters": list(characters)}, "coverage_ledger": {"paratext": []}}


def _beat_draft(wardrobe_plan=()):
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1])],
        wardrobe_plan=list(wardrobe_plan),
    )


def _mention(
    identity_id="bible:c1", source_segment_index=1,
    quote="先替她把外套的扣子扣好", wardrobe_after="浅灰色卫衣，外套扣好",
):
    return SimpleNamespace(
        identity_id=identity_id, source_segment_index=source_segment_index,
        quote=quote, wardrobe_after=wardrobe_after,
    )


# ---------------------------------------------------------------------------
# _quote_is_verbatim：结构归一化空白后的逐字子串判据
# ---------------------------------------------------------------------------

def test_quote_is_verbatim_tolerates_whitespace_not_rewrite():
    assert _quote_is_verbatim(" 先 替 她 把 外套 的 扣子 扣好 ", _SEGMENT_1_TEXT)
    assert not _quote_is_verbatim("她把外套的扣子解开", _SEGMENT_1_TEXT)
    assert not _quote_is_verbatim("", _SEGMENT_1_TEXT)


# ---------------------------------------------------------------------------
# _validate_recheck_response：四条结构判据各自独立触发
# ---------------------------------------------------------------------------

def test_validate_rejects_unknown_identity():
    response = SimpleNamespace(changes=[_mention(identity_id="bible:unknown")])
    problems = _validate_recheck_response(
        response, identity_ids={"bible:c1"}, segments_by_index={1: _SEGMENT_1_TEXT},
    )
    assert problems and "不在本集人物名单里" in problems[0]


def test_validate_rejects_out_of_range_segment():
    response = SimpleNamespace(changes=[_mention(source_segment_index=9)])
    problems = _validate_recheck_response(
        response, identity_ids={"bible:c1"}, segments_by_index={1: _SEGMENT_1_TEXT},
    )
    assert problems and "不在任何节拍覆盖的原文段范围内" in problems[0]


def test_validate_rejects_fabricated_quote():
    response = SimpleNamespace(changes=[_mention(quote="她把围巾摘下来了")])
    problems = _validate_recheck_response(
        response, identity_ids={"bible:c1"}, segments_by_index={1: _SEGMENT_1_TEXT},
    )
    assert problems and "不是第1段原文的逐字子串" in problems[0]


def test_validate_rejects_blank_wardrobe_after():
    response = SimpleNamespace(changes=[_mention(wardrobe_after="   ")])
    problems = _validate_recheck_response(
        response, identity_ids={"bible:c1"}, segments_by_index={1: _SEGMENT_1_TEXT},
    )
    assert problems and "wardrobe_after 不能是空白" in problems[0]


def test_validate_accepts_grounded_item():
    response = SimpleNamespace(changes=[_mention()])
    problems = _validate_recheck_response(
        response, identity_ids={"bible:c1"}, segments_by_index={1: _SEGMENT_1_TEXT},
    )
    assert problems == []


# ---------------------------------------------------------------------------
# _segment_to_beat：按声明顺序取第一个覆盖该段号的节拍
# ---------------------------------------------------------------------------

def test_segment_to_beat_keeps_first_declaring_beat():
    beats = [
        _AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2]),
        _AiBeat(beat_id="B2", summary="y", segment_indexes=[2, 3]),
    ]
    mapping = _segment_to_beat(beats)
    assert mapping[1].beat_id == "B1"
    assert mapping[2].beat_id == "B1"
    assert mapping[3].beat_id == "B2"


# ---------------------------------------------------------------------------
# _merge_recheck_changes：补登新记录 / 已有记录不重复
# ---------------------------------------------------------------------------

def test_merge_appends_new_entry_mapped_to_covering_beat():
    draft = _beat_draft()
    added = _merge_recheck_changes(draft, [_mention()])
    assert added == 1
    assert len(draft.wardrobe_plan) == 1
    entry = draft.wardrobe_plan[0]
    assert (entry.identity_id, entry.beat_id) == ("bible:c1", "B1")
    assert entry.wardrobe == "浅灰色卫衣，外套扣好"
    assert entry.change_reason == "先替她把外套的扣子扣好"


def test_merge_skips_identity_beat_pair_already_recorded(caplog):
    existing = _AiWardrobeState(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫", change_reason="首次出场")
    draft = _beat_draft(wardrobe_plan=[existing])
    with caplog.at_level(logging.INFO):
        added = _merge_recheck_changes(draft, [_mention()])
    assert added == 0
    assert draft.wardrobe_plan == [existing]
    assert "不重复追加" in caplog.text


# ---------------------------------------------------------------------------
# recheck_wardrobe_mid_scene_changes：跳过 / 成功合并 / 失败不阻断
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recheck_skips_when_no_known_characters(monkeypatch):
    calls = []

    async def _stub(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    result = await recheck_wardrobe_mid_scene_changes(
        episode_id="ep1", contract_version="2.4.1", beat_draft=_beat_draft(),
        segments=_sources(_SEGMENT_1_TEXT), payload=_payload([]),
    )
    assert result == _skipped_recheck()
    assert calls == []


@pytest.mark.asyncio
async def test_recheck_skips_when_only_covered_segment_is_paratext(monkeypatch):
    """唯一被节拍覆盖的段号是 paratext（作者的话）——没有可复核的正文段号。"""
    calls = []

    async def _stub(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    draft = _beat_draft()
    payload = _payload([{"identity_id": "bible:c1", "display_name": "顾屿"}])
    payload["coverage_ledger"]["paratext"] = [1]
    result = await recheck_wardrobe_mid_scene_changes(
        episode_id="ep1", contract_version="2.4.1", beat_draft=draft,
        segments=_sources(_SEGMENT_1_TEXT), payload=payload,
    )
    assert result == _skipped_recheck()
    assert calls == []


@pytest.mark.asyncio
async def test_recheck_success_merges_change_into_wardrobe_plan(monkeypatch):
    async def _stub(messages, **kwargs):
        model_type = kwargs["model_type"]
        return model_type(changes=[{
            "identity_id": "bible:c1", "source_segment_index": 1,
            "quote": "先替她把外套的扣子扣好", "wardrobe_after": "浅灰色卫衣，外套扣好",
        }])

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    draft = _beat_draft()
    payload = _payload([{"identity_id": "bible:c1", "display_name": "顾屿"}])
    result = await recheck_wardrobe_mid_scene_changes(
        episode_id="ep1", contract_version="2.4.1", beat_draft=draft,
        segments=_sources(_SEGMENT_1_TEXT), payload=payload,
    )
    assert result == {"status": "ok", "changes_found": 1, "added_count": 1}
    assert draft.wardrobe_plan[0].beat_id == "B1"
    assert draft.wardrobe_plan[0].change_reason == "先替她把外套的扣子扣好"


@pytest.mark.asyncio
async def test_recheck_wires_validate_with_real_segment_text(monkeypatch):
    """核验（validate）真的接住了 ``_run_wardrobe_recheck`` 为这次调用构造的
    segments_by_index——伪造 quote 必须被拦住，不是只在单元测试里孤立成立。"""
    captured = {}

    async def _stub(messages, **kwargs):
        captured["validate"] = kwargs["validate"]
        model_type = kwargs["model_type"]
        return model_type(changes=[])

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    payload = _payload([{"identity_id": "bible:c1", "display_name": "顾屿"}])
    await recheck_wardrobe_mid_scene_changes(
        episode_id="ep1", contract_version="2.4.1", beat_draft=_beat_draft(),
        segments=_sources(_SEGMENT_1_TEXT), payload=payload,
    )
    fabricated = SimpleNamespace(changes=[_mention(quote="她把围巾摘下来了")])
    problems = captured["validate"](fabricated)
    assert problems and "不是第1段原文的逐字子串" in problems[0]


@pytest.mark.asyncio
async def test_recheck_failure_keeps_original_wardrobe_plan(monkeypatch, caplog):
    async def _stub(*args, **kwargs):
        raise RuntimeError("供应商 500")

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    existing = _AiWardrobeState(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫", change_reason="首次出场")
    draft = _beat_draft(wardrobe_plan=[existing])
    payload = _payload([{"identity_id": "bible:c1", "display_name": "顾屿"}])
    with caplog.at_level(logging.WARNING):
        result = await recheck_wardrobe_mid_scene_changes(
            episode_id="ep1", contract_version="2.4.1", beat_draft=draft,
            segments=_sources(_SEGMENT_1_TEXT), payload=payload,
        )
    assert result == {"status": "failed", "changes_found": 0, "added_count": 0}
    assert draft.wardrobe_plan == [existing]
    assert "服装表同场变化复核调用失败" in caplog.text


# ---------------------------------------------------------------------------
# generate_beat_sheet_with_wardrobe_recheck：编排层真实接线（桩打在真实绑定上）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestration_calls_drop_review_then_wardrobe_recheck(monkeypatch):
    import app.production.storyboard_short_drama_review as drop_review_module

    stub_draft = _beat_draft()
    calls = []

    async def _stub_drop_review(**kwargs):
        calls.append(("drop_review", kwargs["adaptation_mode"]))
        return stub_draft, None, {"status": "skipped", "reviewed_count": 0, "must_keep": [], "second_pass": False}

    monkeypatch.setattr(drop_review_module, "_beat_sheet_with_drop_review", _stub_drop_review)
    # 人物名单为空 -> 服装表复核自动走 skip 分支，不需要再桩 model_gateway.chat_structured。
    payload = _payload([])
    beat_draft, projected, drop_review, wardrobe_recheck = await generate_beat_sheet_with_wardrobe_recheck(
        episode_id="ep1", episode_no=1, segments=_sources(_SEGMENT_1_TEXT), payload=payload,
        dialogue_quotes=[], contract_version="2.4.1", adaptation_mode="faithful",
    )
    assert calls == [("drop_review", "faithful")]
    assert beat_draft is stub_draft
    assert projected is None
    assert drop_review == {"status": "skipped", "reviewed_count": 0, "must_keep": [], "second_pass": False}
    assert wardrobe_recheck == _skipped_recheck()
