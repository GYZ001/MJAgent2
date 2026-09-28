"""集成测试：证明 P0-A/C（情绪转折/伏笔）的新判据只接入 ``_generate_beat_
sheet`` 的 ``EmotionalTurnSoftCheck``/``ForeshadowingSoftCheck``，**没有**被
同时写进 ``_validate_beat_sheet_draft``——这是对已确认缺陷 D-α（软检查被
自己的硬校验废掉）的直接修正，也是本次改造的验收红线。

红线的可观测形状：``_validate_beat_sheet_draft`` 没有重试-放行逃生阀，
是无条件阻断；如果新判据混进了它，同一份「有问题的 emotional_turns/
foreshadowing_beats」草稿在第 3 次调用（``EmotionalTurnSoftCheck``/
``ForeshadowingSoftCheck`` 的"最后一次放行"）时，``_generate_beat_sheet``
组装的完整 ``validate`` 闭包仍会报错——因为 ``_validate_beat_sheet_draft``
自己会一直报同样的错，没有重试-放行的机会。本测试直接调用 ``_generate_
beat_sheet`` 真实组装出的 ``validate`` 闭包（通过打桩 ``model_gateway.
chat_structured`` 捕获它，不手写一份等价重实现），驱动同一份"有问题"的
草稿走完整个重试预算，断言最后一次调用整份 validate() 返回空列表——这就是
"最后一次放行时整份草稿能通过 chat_structured 的 validate"。
"""
from __future__ import annotations

import pytest

from app.harness import model_gateway
from app.production.storyboard_beat_sheet import (
    _AiBeat,
    _AiBeatSheetDraft,
    _AiSegmentPlan,
    _BEAT_SHEET_SEMANTIC_RETRY_LIMIT,
    _generate_beat_sheet,
    _validate_beat_sheet_draft,
)
from app.production.storyboard_beat_sheet_schemas import _AiEmotionalTurn, _AiForeshadowingBeat
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.source_excerpt import SourceSegment


def _fixture_segments() -> list[SourceSegment]:
    return [
        SourceSegment(segment_id="s1", text="孟浩扔掉了葫芦。他喃喃自语：“我命由我不由天。”", start_offset=0, end_offset=10),
        SourceSegment(segment_id="s2", text="黄总抓起猫，猫“喵”地叫了一声。", start_offset=10, end_offset=20),
    ]


def _fixture_payload() -> dict:
    return {
        "asset_manifest": {
            "characters": [{"identity_id": "bible:c1", "display_name": "孟浩", "aliases": [], "segment_indexes": [1]}],
            "scenes": [], "props": [],
        },
        "coverage_ledger": {"paratext": []},
    }


def _fixture_quotes() -> list[DialogueQuote]:
    return [DialogueQuote(quote_id="Q01", source_segment_index=1, text="我命由我不由天。", content_chars=8, speaker="孟浩")]


def _draft_with_unclaimed_nominations() -> _AiBeatSheetDraft:
    """``emotional_turns``/``foreshadowing_beats`` 都提名了 B1，但 segments[0]
    没有把 B1 放进自己的 ``beat_ids``（默认空列表）——这条判据（「节拍没有被
    任何段的 beat_ids 引用」）在两个新模块里都会报错，而 ``_validate_beat_
    sheet_draft`` 压根不检查这两个字段，天然不受影响。其余字段与 ``test_
    storyboard_short_drama_schemas.py`` 的忠实档指纹夹具同构，已知能让
    ``_validate_beat_sheet_draft`` 返回空列表。"""
    turn = _AiEmotionalTurn(
        beat_id="B1", turn_kind="decisive_action", turn_evidence_quote="孟浩扔掉了葫芦",
        stimulus_beat_id="", stimulus_evidence_quote="", stimulus_missing_reason="原文没写清楚诱因",
    )
    signal = _AiForeshadowingBeat(beat_id="B1", signal_kind="foreshadowing", evidence_quote="他喃喃自语")
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1])],
        emotional_turns=[turn],
        foreshadowing_beats=[signal],
    )


def test_validate_beat_sheet_draft_ignores_emotional_turns_and_foreshadowing():
    """直接证据：无论 emotional_turns/foreshadowing_beats 里的提名合不合法，
    ``_validate_beat_sheet_draft`` 都不检查它们——它的返回值完全由其余字段
    决定。这是"最后一次放行"能生效的前提，不是巧合。"""
    draft = _draft_with_unclaimed_nominations()
    errors = _validate_beat_sheet_draft(
        draft, source_segments=_fixture_segments(), dialogue_quotes=_fixture_quotes(), adaptation_mode="faithful",
    )
    assert errors == []


@pytest.mark.asyncio
async def test_generate_beat_sheet_validate_closure_passes_on_last_attempt(monkeypatch):
    """驱动 ``_generate_beat_sheet`` 真实组装的 validate 闭包走完整个重试
    预算：前 ``_BEAT_SHEET_SEMANTIC_RETRY_LIMIT`` 次必须报错（因为 emotional_
    turns/foreshadowing_beats 的提名不合法），最后一次必须放行——整份
    validate() 返回空列表，``chat_structured`` 才不会耗尽预算后
    ``raise StructuredSemanticError``。"""
    draft = _draft_with_unclaimed_nominations()
    captured = {}

    async def _stub(*args, **kwargs):
        captured["validate"] = kwargs["validate"]
        return draft

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    await _generate_beat_sheet(
        episode_id="ep_soft_check_wiring", episode_no=1, segments=_fixture_segments(), payload=_fixture_payload(),
        dialogue_quotes=_fixture_quotes(), contract_version="2.4.1", adaptation_mode="faithful",
    )
    validate_fn = captured["validate"]

    # 前 _BEAT_SHEET_SEMANTIC_RETRY_LIMIT 次（attempt 0..limit-1）：EmotionalTurnSoftCheck/
    # ForeshadowingSoftCheck 各自内部计数器未到 retry_limit，仍报错，同一份草稿被打回。
    for attempt in range(_BEAT_SHEET_SEMANTIC_RETRY_LIMIT):
        errors = validate_fn(draft)
        assert errors, f"第 {attempt + 1} 次（attempt {attempt}）应打回，同一份不合法提名不能被静默放行"

    # 最后一次（attempt == retry_limit）：两个 SoftCheck 都降级为放行，且
    # _validate_beat_sheet_draft/soft_cap/budget_cap/hook_check 本就对这份忠实档
    # 草稿全部无异议——整份 validate() 必须返回空列表。
    final_errors = validate_fn(draft)
    assert final_errors == [], (
        "最后一次调用必须放行：如果新判据混进了 _validate_beat_sheet_draft，"
        "它没有重试-放行逃生阀，会一直报同样的错，这里就不会是空列表"
    )
