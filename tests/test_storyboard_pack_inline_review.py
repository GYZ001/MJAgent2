"""边写边审（2026-10-01）：``app.production.storyboard_pack._generate_all_
segment_prompts`` 把正文复核挪进自己的逐段循环后的集成级行为。

单元级判据（复核规则本身、代码核验、``review_segment_inline``/``log_review_
summary`` 的各条分支）已在 ``tests/test_storyboard_prose_review.py`` 覆盖，
不在本文件重复；这里只覆盖「跨越整段生成循环才能看见」的行为：第 N 段重写后
第 N+1 段衔接的是修正稿、claim-once 提名（情绪转折/伏笔/服装/道具）在重写时
不丢失、重写仍违规写 degraded_capabilities、复核失败不重写不阻断、开关关闭时
零复核调用且产物与禁用边写边审前逐字一致、整集结束打一条汇总日志。

另立新文件而不是加进 ``tests/test_storyboard_pack.py``：那个文件 3152/3160
行，棘轮基线只剩 8 行余量，装不下这组测试（CLAUDE.md「棘轮只降不升」）。

monkeypatch 策略同 ``tests/test_storyboard_prose_review.py`` 模块 docstring：
``model_gateway`` 是 ``from app.harness import model_gateway`` 的模块引用，
打在 ``model_gateway.chat_structured`` 上对 ``storyboard_pack`` 与
``storyboard_prose_review`` 两处消费方同时生效，用 ``call_meta["stage_key"]``
区分这一次调用是"写分镜"还是"复核"。
"""
from __future__ import annotations

import json
import logging

import pytest

import app.production.storyboard_pack as storyboard_pack_module
from app.production.storyboard_pack import (
    _AiBeat,
    _AiBeatSheetDraft,
    _AiCameraDigest,
    _AiSegmentPlan,
    _AiStoryboardSegmentDraft,
    _generate_all_segment_prompts,
)
from app.production.storyboard_prose_review import PROSE_REVIEW_SETTING_KEY
from app.source_excerpt import SourceSegment


def _segment_draft(prompt_text: str) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(prompt_text=prompt_text, shot_count=3, camera_digest=_AiCameraDigest())


def _two_segment_beat_draft() -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="他扔掉了理想", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(segment_no=index, synopsis=f"段{index}", source_segment_indexes=[1], beat_ids=["B1"])
            for index in (1, 2)
        ],
    )


_SOURCE = [SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7)]


async def _run(beat_draft: _AiBeatSheetDraft, *, enable_prose_review: bool) -> dict[int, _AiStoryboardSegmentDraft]:
    return await _generate_all_segment_prompts(
        episode_id="ep-inline-review", episode_no=1, beat_draft=beat_draft, segments=_SOURCE, payload={},
        target_video_model="hiagent", bible=None, conn=None, project_id="", aspect_ratio="9:16",
        enhance_music_bed=False, required_dialogue_by_segment_no={}, enable_prose_review=enable_prose_review,
    )


def _stage_key(kwargs: dict) -> str:
    return kwargs["call_meta"]["stage_key"]


@pytest.mark.asyncio
async def test_rewrite_feeds_corrected_draft_forward_as_previous_segment(monkeypatch):
    """第 1 段首次生成被判违规、重写一次；第 2 段生成时拿到的
    previous_segment_prompt 必须是重写后的稿，不是首次生成的原稿。"""
    generation_calls: dict[int, int] = {}
    review_calls: dict[int, int] = {}
    seg2_payload: dict = {}

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        segment_no = payload["segment_no"]
        if _stage_key(kwargs) == "storyboard_pack_segment":
            generation_calls[segment_no] = generation_calls.get(segment_no, 0) + 1
            if segment_no == 2:
                seg2_payload.update(payload)
            return _segment_draft(f"段{segment_no}-第{generation_calls[segment_no]}版")
        review_calls[segment_no] = review_calls.get(segment_no, 0) + 1
        model_type = kwargs["model_type"]
        if segment_no == 1 and review_calls[segment_no] == 1:
            return model_type(violations=[{"kind": "negated_action", "shot_label": "镜头1", "quote": "第1版", "fix": "改成正面写法"}])
        return model_type(violations=[])

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    result = await _run(_two_segment_beat_draft(), enable_prose_review=True)

    assert generation_calls[1] == 2, "第 1 段：首次生成 + 重写一次"
    assert result[1].prompt_text == "段1-第2版", "最终定稿是重写稿"
    assert seg2_payload["previous_segment_prompt"] == "段1-第2版", "第 2 段衔接的是修正后的第 1 段"
    assert result[2].prompt_text == "段2-第1版"
    assert generation_calls[2] == 1, "第 2 段本身干净，不触发重写"


@pytest.mark.asyncio
async def test_rewrite_does_not_lose_claim_once_nominations(monkeypatch):
    """情绪转折等 claim-once 提名只在本段第一次尝试时认领；重写时如果把这次
    计算放进 attempt 循环重算，covered 集合已消耗会返回空、规则从提示词里
    消失——这里断言重写后仍能看到第一次尝试认领到的同一条情绪转折规则。"""
    from app.production.storyboard_beat_sheet_schemas import _AiEmotionalTurn

    generation_calls: dict[int, int] = {}
    seen_rules_on_rewrite: list[str] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        segment_no = payload["segment_no"]
        if _stage_key(kwargs) == "storyboard_pack_segment":
            generation_calls[segment_no] = generation_calls.get(segment_no, 0) + 1
            if generation_calls[segment_no] == 2:
                seen_rules_on_rewrite.extend(payload["rules"])
            return _segment_draft(f"段{segment_no}-第{generation_calls[segment_no]}版")
        model_type = kwargs["model_type"]
        if generation_calls.get(segment_no, 0) == 1:
            return model_type(violations=[{"kind": "negated_action", "shot_label": "镜头1", "quote": "第1版", "fix": "改成正面写法"}])
        return model_type(violations=[])

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="他扔掉了理想", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"])],
        emotional_turns=[_AiEmotionalTurn(beat_id="B1", turn_kind="decisive_action", turn_evidence_quote="他扔掉了毕业证")],
    )

    await _run(beat_draft, enable_prose_review=True)

    assert any("他扔掉了毕业证" in rule for rule in seen_rules_on_rewrite), (
        "重写稿的 rules 里应该仍带着第一次尝试认领到的情绪转折，不能因为 covered 集合"
        "已消耗而在重写时悄悄消失"
    )


@pytest.mark.asyncio
async def test_rewrite_still_violating_writes_degraded_capabilities(monkeypatch):
    """用尽 INLINE_MAX_ATTEMPTS 次尝试仍违规：不再继续重写，把剩余违规写进
    degraded_capabilities，不阻断整集。"""
    generation_calls: dict[int, int] = {}

    async def always_violating(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        segment_no = payload["segment_no"]
        if _stage_key(kwargs) == "storyboard_pack_segment":
            generation_calls[segment_no] = generation_calls.get(segment_no, 0) + 1
            return _segment_draft("镜头1：她没有往里走。")
        model_type = kwargs["model_type"]
        return model_type(violations=[{"kind": "negated_action", "shot_label": "镜头1", "quote": "她没有往里走", "fix": "改成正面写法"}])

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", always_violating)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"])],
    )
    result = await _run(beat_draft, enable_prose_review=True)

    from app.production.storyboard_prose_review import INLINE_MAX_ATTEMPTS

    assert generation_calls[1] == INLINE_MAX_ATTEMPTS, "不超过约定的最大尝试次数，不会无限重写"
    assert any("[STORYBOARD_PROSE_REVIEW_REMAINING][未拦截]" in note for note in result[1].degraded_capabilities)


@pytest.mark.asyncio
async def test_review_failure_does_not_rewrite_or_block(monkeypatch, caplog):
    """复核调用本身失败（供应商错误）时不重写、不阻断整集，原稿原样保留。"""
    generation_calls: dict[int, int] = {}

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if _stage_key(kwargs) == "storyboard_pack_segment":
            generation_calls[payload["segment_no"]] = generation_calls.get(payload["segment_no"], 0) + 1
            return _segment_draft("镜头1：干净的一段。")
        raise RuntimeError("供应商 500")

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"])],
    )
    with caplog.at_level(logging.WARNING):
        result = await _run(beat_draft, enable_prose_review=True)

    assert generation_calls[1] == 1, "复核失败不触发重写"
    assert result[1].prompt_text == "镜头1：干净的一段。"
    assert not any("PROSE_REVIEW" in note for note in result[1].degraded_capabilities), "复核失败不写任何复核相关的 degraded_capabilities"
    assert "[STORYBOARD_PROSE_REVIEW_FAILED]" in caplog.text


@pytest.mark.asyncio
async def test_disabled_switch_makes_zero_review_calls_and_identical_output(monkeypatch):
    """enable_prose_review=False（开关关闭，或 storyboard_identity_regenerate
    不接复核的现状）：不发起任何复核调用，每段只生成一次，产物与禁用边写边审
    前逐字一致。"""
    generation_calls: dict[int, int] = {}
    review_calls: dict[int, int] = {}

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        segment_no = payload["segment_no"]
        if _stage_key(kwargs) == "storyboard_pack_segment":
            generation_calls[segment_no] = generation_calls.get(segment_no, 0) + 1
            return _segment_draft(f"段{segment_no}-定稿")
        review_calls[segment_no] = review_calls.get(segment_no, 0) + 1
        return kwargs["model_type"](violations=[])

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    result = await _run(_two_segment_beat_draft(), enable_prose_review=False)

    assert review_calls == {}, "开关关闭时不发起任何复核调用"
    assert generation_calls == {1: 1, 2: 1}, "每段只生成一次，不会因为没传 enabled 而意外重试"
    assert result[1].prompt_text == "段1-定稿" and result[2].prompt_text == "段2-定稿"


@pytest.mark.asyncio
async def test_summary_log_reports_episode_totals(monkeypatch, caplog):
    """整集结束打一条 [STORYBOARD_PROSE_REVIEW_SUMMARY] 汇总日志，数字与实际
    发生的重写次数、剩余违规数一致。"""
    generation_calls: dict[int, int] = {}
    review_calls: dict[int, int] = {}

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        segment_no = payload["segment_no"]
        if _stage_key(kwargs) == "storyboard_pack_segment":
            generation_calls[segment_no] = generation_calls.get(segment_no, 0) + 1
            return _segment_draft(f"段{segment_no}-第{generation_calls[segment_no]}版")
        review_calls[segment_no] = review_calls.get(segment_no, 0) + 1
        model_type = kwargs["model_type"]
        if segment_no == 1 and review_calls[segment_no] == 1:
            return model_type(violations=[{"kind": "negated_action", "shot_label": "镜头1", "quote": "第1版", "fix": "改"}])
        return model_type(violations=[])

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    with caplog.at_level(logging.INFO):
        await _run(_two_segment_beat_draft(), enable_prose_review=True)

    assert "[STORYBOARD_PROSE_REVIEW_SUMMARY]" in caplog.text
    assert "episode=ep-inline-review" in caplog.text
    assert "segments=2" in caplog.text and "rewritten=1" in caplog.text and "remaining_violations=0" in caplog.text


@pytest.mark.asyncio
async def test_identity_regenerate_path_still_not_wired_to_review(monkeypatch):
    """storyboard_identity_regenerate「修订本段」直接调用
    _generate_all_segment_prompts、不传 enable_prose_review（默认 False）——
    继续不接复核，是本次改动显式保留的现状。"""
    review_calls = 0

    async def fake_chat_structured(messages, **kwargs):
        nonlocal review_calls
        payload = json.loads(messages[1]["content"])
        if _stage_key(kwargs) == "storyboard_pack_segment":
            return _segment_draft(f"段{payload['segment_no']}-定稿")
        review_calls += 1
        return kwargs["model_type"](violations=[])

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"])],
    )
    await _generate_all_segment_prompts(
        episode_id="ep-identity-regen", episode_no=1, beat_draft=beat_draft, segments=_SOURCE, payload={},
        target_video_model="hiagent", bible=None, conn=None, project_id="", aspect_ratio="9:16",
        enhance_music_bed=False, required_dialogue_by_segment_no={},
    )

    assert review_calls == 0


def test_prose_review_setting_key_matches_constant_used_by_generate_storyboard_pack():
    """generate_storyboard_pack 用 storyboard_prose_review_enabled() 的返回值
    驱动 enable_prose_review，这里只核对设置键没有被改名——真正的开关行为由
    上面几条 _generate_all_segment_prompts 级测试与
    tests/test_storyboard_prose_review.py 的开关测试共同覆盖。"""
    assert PROSE_REVIEW_SETTING_KEY == "storyboard_prose_review_enabled"
