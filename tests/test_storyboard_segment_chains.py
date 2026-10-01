"""分镜台阶段二换场并行链（用户拍板，2026-10-01）：
``app.production.storyboard_segment_chains``/``storyboard_segment_chain_plan``。

覆盖任务验收的六点：
1. 切分点只在换场处（同一场戏多段不切）——``plan_chains`` 单元测试。
2. 只有一条链时与串行逐字一致——没有换场的多段场景走 1 条链，
   ``previous_segment_prompt``/``recent_camera_language`` 在全部段落间正常串联
   （不是被切成互不相干的孤岛）。
3. 同时在跑的链数 ≤ ``MAX_CONCURRENT_CHAINS``（真实并发：fake 里用一次真正的
   ``await asyncio.sleep`` 让任务真的交叠，不是没有让出控制权的伪并发）。
4. 重放状态与串行一致：claim-once 的情绪转折/伏笔/道具入场/服装变化在换场
   切开的两条链之间仍只被认领一次。
5. 接缝复核触发链首段重写，且重写时拿到的是合并后真实的上一段（不是链内
   近似的 ``None``）。
6. 链首段台词与前一条链末段重复时被接缝的跨段台词重复检查发现并触发重写。

两层测试：组 A 直接构造 ``SegmentChainContext`` 测 ``plan_chains``（纯函数，
不需要 fake 模型调用）；其余组走公开入口 ``_generate_all_segment_prompts``
（与仓库里其它分镜台接线测试同一套 fake 写法，见
``tests/test_storyboard_pack_inline_review.py`` 模块 docstring）。
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from app.production.storyboard_beat_sheet_schemas import (
    _AiEmotionalTurn, _AiForeshadowingBeat, _AiPropEntrance, _AiWardrobeState,
)
from app.production.storyboard_continuity_memo import _AiContinuityMemo, _AiPropState
from app.production.storyboard_pack import (
    _AiBeat,
    _AiBeatSheetDraft,
    _AiCameraDigest,
    _AiSegmentPlan,
    _AiStoryboardSegmentDraft,
    _generate_all_segment_prompts,
)
from app.production.storyboard_segment_chain_plan import MAX_CONCURRENT_CHAINS, plan_chains
from app.production.storyboard_segment_chains import SegmentChainContext
from app.source_excerpt import SourceSegment

import app.production.storyboard_pack as storyboard_pack_module


def _stage_key(kwargs: dict) -> str:
    return kwargs["call_meta"]["stage_key"]


def _segment_draft(prompt_text: str, **overrides) -> _AiStoryboardSegmentDraft:
    base = dict(prompt_text=prompt_text, shot_count=3, camera_digest=_AiCameraDigest())
    base.update(overrides)
    return _AiStoryboardSegmentDraft(**base)


def _plan(no: int, *, beat_ids: list[str], source_index: int | None = None) -> _AiSegmentPlan:
    return _AiSegmentPlan(
        segment_no=no, synopsis=f"段{no}", source_segment_indexes=[source_index or no], beat_ids=beat_ids,
    )


def _source(texts: dict[int, str]) -> list[SourceSegment]:
    """按 1-based 原文段号构造 ``SourceSegment`` 列表。"""
    ordered = [texts[i] for i in sorted(texts)]
    segments = []
    offset = 0
    for text in ordered:
        segments.append(SourceSegment(segment_id=f"s{offset}", text=text, start_offset=offset, end_offset=offset + len(text)))
        offset += len(text)
    return segments


# ---------------------------------------------------------------------------
# 组 A：切分点只在换场处（``plan_chains`` 单元测试，不需要 fake 模型调用）
# ---------------------------------------------------------------------------


def _minimal_ctx(*, beat_draft, segments, relevant_assets_by_segment_no) -> SegmentChainContext:
    return SegmentChainContext(
        episode_id="ep", episode_no=1, beat_draft=beat_draft, segments=segments, payload={}, bible=None,
        required_dialogue_by_segment_no={}, conn=None, project_id="", aspect_ratio="9:16",
        enhance_music_bed=False, narrator_voice_character="", target_video_model="hiagent", contract_version="x",
        profile=None, target_model_literal="seedance_2", dialect_instructions="",
        beats_by_id={b.beat_id: b for b in beat_draft.beat_sheet}, paratext_indexes=set(),
        visual_style="", visual_style_is_photographic=False, shared_rules=[],
        props_plan=[], appearance_locks={}, relevant_assets_by_segment_no=relevant_assets_by_segment_no,
        enable_prose_review=False, draft_cls=None, validate_segment_draft=lambda *a, **k: [],
        ensure_budget=lambda: None, camera_digest_window=4, segment_content_advisories=lambda *a, **k: [],
        min_shots=2, max_shots=4, answer_tokens=2400,
    )


def _chain_numbers(chains: list[list]) -> list[list[int]]:
    return [[p.segment_no for p in chain] for chain in chains]


def test_plan_chains_splits_only_at_resource_scene_change():
    """5 段，场景分组 room/room/street/street/yard——判据来自各段
    ``relevant_assets.scenes``（``scene_changed_by_resource_scenes``），同一场戏
    的段落（1-2、3-4）始终在同一条链，不会被多切。"""
    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2, 3, 4, 5])],
        segments=[_plan(i, beat_ids=["B1"]) for i in range(1, 6)],
    )
    scenes_by_segment = {1: "scn_room", 2: "scn_room", 3: "scn_street", 4: "scn_street", 5: "scn_yard"}
    relevant = {no: {"scenes": [{"scene_id": scene_id}]} for no, scene_id in scenes_by_segment.items()}
    segments = _source({i: f"第{i}段正文，没有结构标记。" for i in range(1, 6)})

    chains = plan_chains(_minimal_ctx(beat_draft=beat_draft, segments=segments, relevant_assets_by_segment_no=relevant))

    assert _chain_numbers(chains) == [[1, 2], [3, 4], [5]]


def test_plan_chains_splits_at_explicit_scene_header_text():
    """文本判据（``screenplay_markers.scene_changed``）独立成立：
    relevant_assets.scenes 两侧都为空时该信号本身判不出换场（见
    ``scene_changed_by_resource_scenes`` 文档），只靠段头文本本身。"""
    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2, 3])],
        segments=[_plan(i, beat_ids=["B1"]) for i in range(1, 4)],
    )
    relevant = {i: {"scenes": []} for i in (1, 2, 3)}
    segments = _source({
        1: "【段1｜客厅｜日】他坐在沙发上。",
        2: "【段2｜客厅｜日】她走进来坐下。",
        3: "【段3｜街道｜夜】他独自走在路上。",
    })

    chains = plan_chains(_minimal_ctx(beat_draft=beat_draft, segments=segments, relevant_assets_by_segment_no=relevant))

    assert _chain_numbers(chains) == [[1, 2], [3]]


def test_plan_chains_single_chain_when_no_scene_change_anywhere():
    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2, 3])],
        segments=[_plan(i, beat_ids=["B1"]) for i in range(1, 4)],
    )
    relevant = {i: {"scenes": [{"scene_id": "scn_room"}]} for i in (1, 2, 3)}
    segments = _source({i: f"第{i}段，仍在同一个房间。" for i in range(1, 4)})

    chains = plan_chains(_minimal_ctx(beat_draft=beat_draft, segments=segments, relevant_assets_by_segment_no=relevant))

    assert _chain_numbers(chains) == [[1, 2, 3]]


# ---------------------------------------------------------------------------
# 组 B：只有一条链时与串行逐字一致
# ---------------------------------------------------------------------------


def _same_scene_payload() -> dict:
    return {"asset_manifest": {"scenes": [{"scene_id": "scn_room", "display_name": "房间", "segment_indexes": [1, 2, 3]}]}}


@pytest.mark.asyncio
async def test_single_chain_keeps_full_serial_continuity(monkeypatch, caplog):
    """3 段都在同一场戏（没有换场）：[STORYBOARD_CHAIN_PLAN] 只报 1 条链；
    第 2、3 段的 previous_segment_prompt 分别是第 1、2 段的真实定稿，证明
    没有被当成链首段、previous_draft 没有被近似成 None——与串行逐字一致。"""
    calls: dict[int, dict] = {}

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if _stage_key(kwargs) != "storyboard_pack_segment":
            return kwargs["model_type"](violations=[])
        segment_no = payload["segment_no"]
        calls[segment_no] = payload
        return _segment_draft(f"镜头1：段{segment_no}定稿。")

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2, 3])],
        segments=[_plan(i, beat_ids=["B1"]) for i in range(1, 4)],
    )
    source = _source({i: f"第{i}段，仍在同一个房间。" for i in range(1, 4)})

    with caplog.at_level(logging.INFO):
        result = await _generate_all_segment_prompts(
            episode_id="ep-single-chain", episode_no=1, beat_draft=beat_draft, segments=source,
            payload=_same_scene_payload(), target_video_model="hiagent", bible=None, conn=None, project_id="",
            aspect_ratio="9:16", enhance_music_bed=False, required_dialogue_by_segment_no={},
        )

    assert "[STORYBOARD_CHAIN_PLAN]" in caplog.text and "chains=1" in caplog.text
    assert calls[2]["previous_segment_prompt"] == "镜头1：段1定稿。"
    assert calls[3]["previous_segment_prompt"] == "镜头1：段2定稿。"
    assert [item["segment_no"] for item in calls[3]["recent_camera_language"]] == [1, 2]
    assert result[1].prompt_text == "镜头1：段1定稿。"


# ---------------------------------------------------------------------------
# 组 C：同时在跑的链数 ≤ MAX_CONCURRENT_CHAINS
# ---------------------------------------------------------------------------


def _three_scene_payload() -> dict:
    return {
        "asset_manifest": {
            "scenes": [
                {"scene_id": "scn_a", "display_name": "a", "segment_indexes": [1]},
                {"scene_id": "scn_b", "display_name": "b", "segment_indexes": [2]},
                {"scene_id": "scn_c", "display_name": "c", "segment_indexes": [3]},
            ],
        },
    }


@pytest.mark.asyncio
async def test_at_most_two_chains_run_concurrently(monkeypatch, caplog):
    """3 条链（3 个不同场景，各 1 段），并发上限固定 2：fake 里用一次真正的
    ``await asyncio.sleep`` 制造交叠窗口，断言峰值并发恰好是 2（不是因为没有
    真正让出控制权而意外测出 1，也不会因为调度漏洞冲到 3）。"""
    current = 0
    max_seen = 0

    async def fake_chat_structured(messages, **kwargs):
        nonlocal current, max_seen
        payload = json.loads(messages[1]["content"])
        if _stage_key(kwargs) != "storyboard_pack_segment":
            return kwargs["model_type"](violations=[])
        current += 1
        max_seen = max(max_seen, current)
        await asyncio.sleep(0.02)
        current -= 1
        return _segment_draft(f"镜头1：段{payload['segment_no']}定稿。")

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2, 3])],
        segments=[_plan(i, beat_ids=["B1"]) for i in range(1, 4)],
    )
    source = _source({1: "甲地的一段正文。", 2: "乙地的一段正文。", 3: "丙地的一段正文。"})

    with caplog.at_level(logging.INFO):
        await _generate_all_segment_prompts(
            episode_id="ep-concurrency", episode_no=1, beat_draft=beat_draft, segments=source,
            payload=_three_scene_payload(), target_video_model="hiagent", bible=None, conn=None, project_id="",
            aspect_ratio="9:16", enhance_music_bed=False, required_dialogue_by_segment_no={},
        )

    assert "chains=3" in caplog.text
    assert max_seen == MAX_CONCURRENT_CHAINS == 2, "3 条链可跑，但同一时刻最多 2 条在跑"


# ---------------------------------------------------------------------------
# 组 D：重放状态与串行一致——claim-once 在换场切开的两条链之间仍只认领一次
# ---------------------------------------------------------------------------


def _two_chain_payload() -> dict:
    return {
        "asset_manifest": {
            "characters": [{"identity_id": "bible:c1", "display_name": "温念", "aliases": [], "segment_indexes": [1, 2]}],
            "scenes": [
                {"scene_id": "scn_a", "display_name": "房间", "segment_indexes": [1]},
                {"scene_id": "scn_b", "display_name": "街道", "segment_indexes": [2]},
            ],
        },
    }


@pytest.mark.asyncio
async def test_claim_once_nominations_not_duplicated_or_lost_across_chains(monkeypatch):
    """换场切成两条链（段1/段2 各一条）；情绪转折/伏笔/道具入场/服装变化全部
    挂在段1、段2 共享的 beat_id="B1" 上（模拟容量拆分续段完整继承 beat_ids 的
    真实形状）。链 2（段2）起始态重放段1 的认领，不应该在段2 重新认领。"""
    calls: dict[int, dict] = {}

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if _stage_key(kwargs) != "storyboard_pack_segment":
            return kwargs["model_type"](violations=[])
        calls[payload["segment_no"]] = payload
        return _segment_draft(f"镜头1：段{payload['segment_no']}定稿。")

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2])],
        segments=[_plan(1, beat_ids=["B1"]), _plan(2, beat_ids=["B1"])],
        emotional_turns=[_AiEmotionalTurn(
            beat_id="B1", turn_kind="decisive_action", turn_evidence_quote="他攥紧了拳头",
            stimulus_missing_reason="原文未写明诱因",
        )],
        foreshadowing_beats=[_AiForeshadowingBeat(beat_id="B1", signal_kind="foreshadowing", evidence_quote="桌上一根黑色羽毛")],
        prop_entrances=[_AiPropEntrance(label="旧怀表", beat_id="B1", entrance_description="从口袋里取出")],
        wardrobe_plan=[_AiWardrobeState(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫", change_reason="首次出场")],
    )
    source = _source({1: "他站在窗边，攥紧了拳头。", 2: "她独自走在街上。"})

    await _generate_all_segment_prompts(
        episode_id="ep-claim-once", episode_no=1, beat_draft=beat_draft, segments=source,
        payload=_two_chain_payload(), target_video_model="hiagent", bible=None, conn=None, project_id="",
        aspect_ratio="9:16", enhance_music_bed=False, required_dialogue_by_segment_no={},
    )

    rules1 = "".join(calls[1]["rules"])
    rules2 = "".join(calls[2]["rules"])

    for marker in ("他攥紧了拳头", "桌上一根黑色羽毛", "旧怀表"):
        assert marker in rules1, f"{marker} 应该被段1（claim-once 的第一个归属段）认领"
        assert marker not in rules2, f"{marker} 不应该在段2（链2 起始态已重放认领）被重复认领"

    # 服装：段1 是「首次出场」的新变化；段2 没有新变化，但起始着装（look_start）
    # 应该正确继承段1 定下的着装——这是「状态延续」而不是「重复认领」，两者
    # 不能混为一谈：段2 不该再出现"本段内着装变化"，但应该看到继承的着装本身。
    # 注意不能直接拿「本段内着装变化」裸字符串判断——共享规则文案里还有一句
    # 泛泛提到这个词组本身（「...或『本段内着装变化』规则，wardrobe 必须以它
    # 为准...」），两段都会出现；真正只在「有新变化」时才出现的是
    # ``storyboard_wardrobe_plan.segment_rule_text`` 产出的那一整句（冒号 + @
    # 人名紧跟其后），用这句的开头子串才不会被共享文案误判。
    change_marker = "本段内着装变化：@温念"
    assert change_marker in rules1 and "米白色针织开衫" in rules1
    assert change_marker not in rules2
    assert "本段着装（全集服装表）" in rules2 and "米白色针织开衫" in rules2


# ---------------------------------------------------------------------------
# 组 E/F：接缝复核
# ---------------------------------------------------------------------------


def _review_payload_previous_shot(kwargs: dict, messages) -> str:
    return json.loads(messages[1]["content"]).get("previous_segment_last_shot", "")


@pytest.mark.asyncio
async def test_seam_review_rewrites_chain_head_with_real_previous_draft(monkeypatch):
    """链2（段2）链内生成时没有可参考的上一段（previous_segment_last_shot==""），
    复核放行；合并后用真实段1定稿做接缝复核时同一份正文判出 screen_side 违规，
    触发重写——重写那次生成调用拿到的 previous_segment_prompt 必须是段1的真实
    定稿（不是链内近似的 None）。"""
    generation_calls: dict[int, int] = {}
    rewrite_payload: dict = {}

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if _stage_key(kwargs) == "storyboard_pack_segment":
            segment_no = payload["segment_no"]
            generation_calls[segment_no] = generation_calls.get(segment_no, 0) + 1
            if segment_no == 2 and generation_calls[segment_no] == 2:
                rewrite_payload.update(payload)
                return _segment_draft("镜头1：她走进房间，在画面左侧站定，看向窗外。")
            if segment_no == 1:
                return _segment_draft("镜头1：她站在窗边，望向远方，双手插兜站在画面左侧。")
            return _segment_draft("镜头1：她走进房间，在画面右侧站定，看向窗外。")
        model_type = kwargs["model_type"]
        if _review_payload_previous_shot(kwargs, messages):
            return model_type(violations=[{
                "kind": "screen_side", "shot_label": "镜头1", "quote": "在画面右侧站定",
                "previous_quote": "站在画面左侧", "fix": "改回与上一段一致的站位",
            }])
        return model_type(violations=[])

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2])],
        segments=[_plan(1, beat_ids=["B1"]), _plan(2, beat_ids=["B1"])],
    )
    source = _source({1: "她站在窗边。", 2: "她走进了另一个房间。"})

    result = await _generate_all_segment_prompts(
        episode_id="ep-seam-rewrite", episode_no=1, beat_draft=beat_draft, segments=source,
        payload=_two_chain_payload(), target_video_model="hiagent", bible=None, conn=None, project_id="",
        aspect_ratio="9:16", enhance_music_bed=False, required_dialogue_by_segment_no={}, enable_prose_review=True,
    )

    assert generation_calls[2] == 2, "链内初次生成 + 接缝重写各一次"
    assert rewrite_payload["previous_segment_prompt"] == "镜头1：她站在窗边，望向远方，双手插兜站在画面左侧。", (
        "重写时必须拿到合并后真实的段1定稿，不是链内近似的 None"
    )
    assert result[2].prompt_text == "镜头1：她走进房间，在画面左侧站定，看向窗外。"


@pytest.mark.asyncio
async def test_seam_dialogue_repeat_check_catches_cross_chain_duplicate(monkeypatch):
    """段2（链2 链首段）的台词与段1（链1 末段）逐字重复——链内生成时各自只看
    得到自己这条链的 delivered_lines（链2 从空开始），看不穿前一条链，必须靠
    合并后的跨段台词重复检查（任务 3「并做跨段台词重复检查」）发现并触发重写。
    """
    generation_calls: dict[int, int] = {}
    rewrite_rules_text = ""
    #: 身份合同要求的最小合法形状（见 tests/test_storyboard_pack.py
    #: test_generate_passes_required_dialogue_into_payload_and_rules 同一先例）：
    #: resources.characters 声明发声主体 + prompt_text 带 {{speech:U01}} 占位符
    #: + dialogue[].utterance_id 一一对应，否则 finalize_generated_identity 会
    #: 拒绝整段。
    _RESOURCES = {"characters": [{
        "identity_id": "bible:c1", "display_name": "温念", "subject_kind": "character", "visibility": "visible",
    }]}

    def _dialogue_draft(prefix: str, line: str) -> _AiStoryboardSegmentDraft:
        return _segment_draft(
            f"{prefix}{{{{speech:U01}}}}", resources=_RESOURCES,
            dialogue=[{
                "utterance_id": "U01", "speaker_identity_id": "bible:c1", "line": line,
                "source_segment_index": 1, "source_quote_id": "", "delivery_kind": "spoken_dialogue",
            }],
        )

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if _stage_key(kwargs) == "storyboard_pack_segment":
            segment_no = payload["segment_no"]
            generation_calls[segment_no] = generation_calls.get(segment_no, 0) + 1
            if segment_no == 1:
                return _dialogue_draft("镜头1：她开口说话。", "你愿意跟我走吗？")
            if generation_calls[segment_no] == 1:
                return _dialogue_draft("镜头1：她也开口说话。", "你愿意跟我走吗？")
            nonlocal rewrite_rules_text
            rewrite_rules_text = "".join(payload["rules"])
            return _segment_draft("镜头1：她沉默地看着窗外。")
        return kwargs["model_type"](violations=[])  # 正文复核本身保持干净，只让台词重复检查触发

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2])],
        segments=[_plan(1, beat_ids=["B1"]), _plan(2, beat_ids=["B1"])],
    )
    source = _source({1: "她问了一句话。", 2: "她在另一个地方又问了一遍。"})

    result = await _generate_all_segment_prompts(
        episode_id="ep-seam-dialogue", episode_no=1, beat_draft=beat_draft, segments=source,
        payload=_two_chain_payload(), target_video_model="hiagent", bible=None, conn=None, project_id="",
        aspect_ratio="9:16", enhance_music_bed=False, required_dialogue_by_segment_no={}, enable_prose_review=True,
    )

    assert generation_calls[2] == 2, "链内初次生成 + 接缝触发的重写各一次"
    assert "跨段台词重复" in rewrite_rules_text
    assert result[2].dialogue == [], "重写稿不再重复段1已经说过的台词"


# 组 G：独立审查发现 1 回归——链首段 rules 诚实、接缝补 continuity_memo 核验
@pytest.mark.asyncio
async def test_mid_episode_chain_head_honest_rules_and_seam_catches_continuity_drift(monkeypatch):
    """段2（链首段）本地生成时：① rules 不出现「本集第一段」误导文案（见
    storyboard_narrative_arc._continuity_memo_rules_mid_episode_scene_change）；
    ② props 与真实段1不一致时链内生成看不到真实段1，要靠接缝核验
    （``_seam_continuity_memo_errors``）发现并触发一次重写。"""
    generation_calls: dict[int, int] = {}
    first_call_rules: list[str] = []
    rewrite_payload: dict = {}

    def _memo(form: str) -> _AiContinuityMemo:
        return _AiContinuityMemo(
            time_of_day="清晨", time_of_day_basis="inferred",
            props=[_AiPropState(name="猫包", form=form, location="桌上", state="拉链闭合")],
        )

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if _stage_key(kwargs) != "storyboard_pack_segment":
            return kwargs["model_type"](violations=[])
        segment_no = payload["segment_no"]
        generation_calls[segment_no] = generation_calls.get(segment_no, 0) + 1
        if segment_no == 1:
            return _segment_draft("镜头1：段1定稿，猫包放在桌上。", continuity_memo=_memo("网状"))
        if generation_calls[segment_no] == 1:
            first_call_rules.extend(payload["rules"])
            return _segment_draft("镜头1：段2初稿，猫包放在桌上。", continuity_memo=_memo("透明"))
        rewrite_payload.update(payload)
        return _segment_draft("镜头1：段2改稿，猫包放在桌上。", continuity_memo=_memo("网状"))

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2])],
        segments=[_plan(1, beat_ids=["B1"]), _plan(2, beat_ids=["B1"])],
    )
    source = _source({1: "她把猫包放在桌上。", 2: "她在另一个房间里。"})

    await _generate_all_segment_prompts(
        episode_id="ep-seam-memo", episode_no=1, beat_draft=beat_draft, segments=source,
        payload=_two_chain_payload(), target_video_model="hiagent", bible=None, conn=None, project_id="",
        aspect_ratio="9:16", enhance_music_bed=False, required_dialogue_by_segment_no={}, enable_prose_review=True,
    )

    rules2_text = "".join(first_call_rules)
    assert "本集第一段" not in rules2_text, "链首段不是真正的第一段，rules 不该这样误导模型"
    assert "本段与上一段之间发生了换场" in rules2_text

    assert generation_calls[2] == 2, "链内初次生成 + 接缝核验触发的重写各一次"
    assert rewrite_payload["previous_segment_prompt"] == "镜头1：段1定稿，猫包放在桌上。"
    assert "外观（form）从上一段的" in "".join(rewrite_payload["rules"])
