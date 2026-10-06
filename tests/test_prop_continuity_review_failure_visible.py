"""复核调用失败必须可见，不得被静默当成「没有违规」（2026-10-05 生产 B 实测
缺陷：分镜台模型账号额度耗尽时第15、17段复核调用抛 ``ProviderError``，
``_review_segment`` 旧版吞成空列表 ``[]``，与「调用成功、确实没有违规」无法
区分，预览与快照里显示成「已复核、干净」，实际根本没复核）。

本文件覆盖：

(a) ``_review_segment`` 本身——调用失败返回 ``None``，调用成功且没有违规
    返回 ``[]``，两者必须能区分；
(b) ``review_existing_episode_segments``——某段复核失败标 ``review_failed``
    且 ``error`` 非空、不进 ``flagged_segment_nos``，不拖累其它段；
(c) GET 接口响应含顶层 ``review_failed_segment_nos``；
(d) 快照往返保留 ``review_failed``，旧快照没有这个键按 ``False`` 处理；
(e) POST 确认了复核失败段时给出专门的 ``skip_reason``，不发模型调用；
(f) 重写后复核调用失败标 ``post_rewrite_review_failed``，不当成「重写后
    已干净」；
(g) ``review_segment_inline``（生成主链路）仍按既有取舍把失败当「没有
    违规」处理，不抛异常、不阻断。

与 ``tests/test_prop_continuity_review.py``/``tests/test_prop_continuity_
review_api.py``/``tests/test_storyboard_prose_review.py`` 同一套夹具与
``model_gateway.chat_structured`` 打桩方式（按请求体有没有 ``"rules"`` 键
区分「复核调用」与「最小替换提案调用」），不重开第二套。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.domain.storyboard_ops import prop_continuity_review as batch
from app.domain.storyboard_ops import prop_continuity_review_api as api
from app.harness import model_gateway
from app.production import storyboard_prose_review as prose_review
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_prose_review import ProseViolation
from tests.test_prop_continuity_review import _bible, fixture  # noqa: F401 -- 复用同一套夹具，不重开第二份


def _draft(prompt_text: str) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(prompt_text=prompt_text, shot_count=3, dialogue=[], degraded_capabilities=[])


# ---------------------------------------------------------------------------
# (a) _review_segment：失败返回 None，成功无违规返回 []——两者必须区分
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_segment_returns_none_on_call_failure(monkeypatch):
    draft = _draft("镜头1：她看向窗外。")

    async def failing(*args, **kwargs):
        raise RuntimeError("供应商 502 session limit")

    monkeypatch.setattr(model_gateway, "chat_structured", failing)
    result = await prose_review._review_segment(
        episode_id="ep1", segment_no=1, draft=draft, previous_draft=None, photographic=False, max_shots=4,
    )
    assert result is None, "调用失败必须是 None，不能与『调用成功、没有违规』混用同一个空列表"


@pytest.mark.asyncio
async def test_review_segment_returns_empty_list_on_success_without_violations(monkeypatch):
    draft = _draft("镜头1：她看向窗外。")

    async def clean(messages, **kwargs):
        return kwargs["model_type"](violations=[])

    monkeypatch.setattr(model_gateway, "chat_structured", clean)
    result = await prose_review._review_segment(
        episode_id="ep1", segment_no=1, draft=draft, previous_draft=None, photographic=False, max_shots=4,
    )
    assert result == [], "调用成功且确实没有违规时，仍然是一个（空）列表，不是 None"


# ---------------------------------------------------------------------------
# (b) review_existing_episode_segments：失败段标记可见，不拖累其它段
# ---------------------------------------------------------------------------

def test_review_existing_episode_segments_marks_failed_segment_without_blocking_others(fixture, monkeypatch):
    conn, episode, _payload = fixture

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            raise RuntimeError("供应商 502 session limit")
        return kwargs["model_type"](violations=[])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    outcomes = asyncio.run(batch.review_existing_episode_segments(conn, episode=episode, bible=_bible()))
    assert outcomes[0].segment_no == 1 and outcomes[0].review_failed is True
    assert outcomes[0].error, "必须给出中文说明，不能只留个布尔位"
    assert outcomes[0].violations == [], "没有已核验违规，不是『确认干净』"
    assert outcomes[1].segment_no == 2 and outcomes[1].review_failed is False, "第 1 段失败不拖累第 2 段"
    assert batch.flagged_segment_nos(outcomes) == [], "复核失败段没有已核验违规，不应出现在待重写集合里"
    assert batch.review_failed_segment_nos(outcomes) == [1]


# ---------------------------------------------------------------------------
# (c) GET 接口响应含 review_failed_segment_nos
# ---------------------------------------------------------------------------

def test_get_preview_response_contains_review_failed_segment_nos(fixture, monkeypatch):
    conn, episode, _payload = fixture

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            raise RuntimeError("供应商 502 session limit")
        return kwargs["model_type"](violations=[])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    result = asyncio.run(api.prop_continuity_review(episode["id"]))
    assert result["review_failed_segment_nos"] == [1]
    assert result["segments"][0]["review_failed"] is True
    assert result["segments"][0]["error"]
    assert result["segments"][1]["review_failed"] is False


# ---------------------------------------------------------------------------
# (d) 快照往返保留 review_failed；旧快照没有这个键按 False 处理
# ---------------------------------------------------------------------------

def test_build_and_restore_snapshot_round_trips_review_failed(fixture, monkeypatch):
    conn, episode, _payload = fixture

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            raise RuntimeError("供应商 502 session limit")
        return kwargs["model_type"](violations=[])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    outcomes = asyncio.run(batch.review_existing_episode_segments(conn, episode=episode, bible=_bible()))
    snapshot_segments = batch.build_review_snapshot_segments(conn, episode["id"], outcomes)
    assert snapshot_segments[0]["review_failed"] is True
    assert snapshot_segments[1]["review_failed"] is False
    restored = batch.outcomes_from_snapshot(snapshot_segments)
    assert restored[0].review_failed is True
    assert restored[1].review_failed is False


def test_outcomes_from_snapshot_defaults_review_failed_to_false_for_legacy_snapshot():
    """本次改动前落的旧快照没有 ``review_failed`` 这个键——必须按 False 处理，
    不能因为缺键就报错或误判成失败。"""
    legacy_snapshot_segments = [{"segment_no": 1, "shot_id": "s1", "prompt_text_hash": "abc", "violations": []}]
    restored = batch.outcomes_from_snapshot(legacy_snapshot_segments)
    assert restored[0].review_failed is False


# ---------------------------------------------------------------------------
# (e) POST 确认了复核失败段：专门的 skip_reason，不静默当成干净
# ---------------------------------------------------------------------------

def test_rewrite_gives_skip_reason_for_confirmed_review_failed_segment(fixture, monkeypatch):
    conn, episode, _payload = fixture

    async def preview_chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            raise RuntimeError("供应商 502 session limit")
        return kwargs["model_type"](violations=[{
            "kind": "prop_state_regression", "shot_label": "镜头1", "prop_name": "插座与插头",
            "quote": "墙根插座上插着白色插头", "previous_quote": "插座两孔空着", "fix": "改回已拔下",
        }])

    monkeypatch.setattr(model_gateway, "chat_structured", preview_chat)
    preview = asyncio.run(api.prop_continuity_review(episode["id"]))
    assert preview["review_failed_segment_nos"] == [1]

    async def rewrite_chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "rules" in request:  # 局部修改后的复核调用：确认新正文不再违规
            return kwargs["model_type"](violations=[])
        return kwargs["model_type"](replacements=[{"quote": "墙根插座上插着白色插头", "replacement": "插座两孔空着"}])

    monkeypatch.setattr(model_gateway, "chat_structured", rewrite_chat)
    # 复核失败段（第1段）与正常待重写段（第2段）一起确认——不应整批 409。
    result = asyncio.run(api.prop_continuity_rewrite(
        episode["id"], api.PropContinuityRewriteBody(snapshot_id=preview["snapshot_id"], confirmed_segment_nos=[1, 2]),
    ))
    seg1 = result["segments"][0]
    assert seg1["rewritten"] is False and seg1["error"] is None
    assert seg1["skip_reason"] == "本段预览时复核调用失败，没有可用的违规清单，请重新预览"
    seg2 = result["segments"][1]
    assert seg2["rewritten"] is True, "复核失败段不应该拖累同批里正常待重写的段"


# ---------------------------------------------------------------------------
# (f) 重写后复核失败：post_rewrite_review_failed=True，不当成「重写后已干净」
# ---------------------------------------------------------------------------

def test_rewrite_marks_post_rewrite_review_failed_when_post_review_call_fails(fixture, monkeypatch):
    conn, episode, _payload = fixture
    violation = ProseViolation(
        kind="prop_state_regression", shot_label="镜头1", prop_name="插座与插头",
        quote="墙根插座上插着白色插头", previous_quote="插座两孔空着", fix="改回已拔下",
    )
    outcomes = [batch.SegmentReviewOutcome(segment_no=2, shot_id="s2", violations=[violation])]

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "rules" in request:  # 重写后复核调用：模拟供应商额度耗尽
            raise RuntimeError("供应商 502 session limit")
        return kwargs["model_type"](replacements=[{"quote": "墙根插座上插着白色插头", "replacement": "插座两孔空着"}])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    result = asyncio.run(batch.rewrite_flagged_segments(
        conn, episode=episode, bible=_bible(), outcomes=outcomes, confirmed_segment_nos={2},
    ))
    assert result[0].rewritten is True and result[0].artifact_id, "保存已经发生，不回滚"
    assert result[0].error is None
    assert result[0].post_rewrite_review_failed is True
    assert result[0].remaining_violations == [], "没有复核成，remaining_violations 恒为空，但不代表『重写后已干净』"


# ---------------------------------------------------------------------------
# (g) review_segment_inline（生成主链路）：失败仍按「没有违规」处理，不阻断
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_segment_inline_treats_call_failure_as_no_violation(monkeypatch):
    draft = _draft("镜头1：她没有往里走。")

    async def failing(*args, **kwargs):
        raise RuntimeError("供应商 502 session limit")

    monkeypatch.setattr(model_gateway, "chat_structured", failing)
    revision_text = await prose_review.review_segment_inline(
        draft, previous_draft=None, episode_id="ep1", segment_no=1, photographic=False, max_shots=4,
        attempt=0, enabled=True,
    )
    assert revision_text == "", "没有下一轮修改意见，按既有取舍不阻断"
    assert draft.degraded_capabilities == [], "按『没有违规』处理，不写 degraded_capabilities"
