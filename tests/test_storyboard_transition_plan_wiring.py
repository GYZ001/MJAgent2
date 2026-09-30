"""转场先定后写的接线验收（真实故障：2026-09-30《顾念长安》EP1 第19段——字段是叠化，模型
却在镜头1正文按硬切写，因为旧流程里模型写正文时只拿到纯文本判据的猜测，字段要等模型自己
写完 resources.scenes 之后才会升级，模型永远看不到最终结论）。

tests/test_storyboard_transition_plan.py 只测 ``app.production.storyboard_transition_plan``
模块内的纯函数；本文件补端到端一层——真的走 ``_generate_all_segment_prompts`` 的生成循环，
证明 ``resolve_transition_before_generation``/``finalize_transition_after_generation`` 两处
接线确实生效（与 tests/test_storyboard_action_density_wiring.py 同一先例，
tests/test_storyboard_pack.py 本轮已经没有行数余量，新接线验收放这个新文件）。
"""
from __future__ import annotations

import json

import pytest

from app.production.screenplay_markers import SCENE_CHANGE_TRANSITION
from app.production.storyboard_pack import (
    _AiBeat,
    _AiBeatSheetDraft,
    _AiCameraDigest,
    _AiSegmentPlan,
    _AiSegmentResources,
    _AiStoryboardSegmentDraft,
    _generate_all_segment_prompts,
)
from app.source_excerpt import SourceSegment


def _draft(prompt_text: str) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(prompt_text=prompt_text, shot_count=3, camera_digest=_AiCameraDigest())


def _two_segment_beat_draft() -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="她走出了房间", segment_indexes=[1, 2])],
        segments=[
            _AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"]),
            _AiSegmentPlan(segment_no=2, synopsis="段2", source_segment_indexes=[2], beat_ids=["B1"]),
        ],
    )


def _two_source_segments() -> list[SourceSegment]:
    return [
        SourceSegment(segment_id="s1", text="她站在窗边，没有任何结构标记。", start_offset=0, end_offset=14),
        SourceSegment(segment_id="s2", text="她走出房间，来到街上。", start_offset=14, end_offset=25),
    ]


def _manifest_with_distinct_planned_scenes() -> dict:
    """映射台为第 1/2 段各自登记了不同场景（scn_room/scn_street）——这是生成前就有的产出侧
    数据，不是模型这次生成才决定的东西。"""
    return {
        "asset_manifest": {
            "characters": [], "props": [], "functional_extras": [],
            "scenes": [
                {"scene_id": "scn_room", "display_name": "房间", "segment_indexes": [1]},
                {"scene_id": "scn_street", "display_name": "街道", "segment_indexes": [2]},
            ],
        },
    }


@pytest.mark.asyncio
async def test_transition_told_to_model_before_it_writes_prose(monkeypatch):
    """只要映射台已经为本段登记了计划场景，且与上一段已生成场景不同，发给模型的 task_payload
    （模型写提示词之前）里 transition_from_previous 就已经是升级后的「叠化」，不用等模型自己
    报 resources.scenes——这正是「先定后写」：字段先确定，模型才动笔。"""
    import app.production.storyboard_pack as storyboard_pack_module

    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        segment_no = payload["segment_no"]
        scene_id = "scn_room" if segment_no == 1 else "scn_street"  # 与模型被告知的计划场景一致
        return _draft(f"提示词-段{segment_no}").model_copy(
            update={"resources": _AiSegmentResources(scenes=[{"scene_id": scene_id}])}
        )

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    result = await _generate_all_segment_prompts(
        episode_id="ep-transition-told-before-write",
        episode_no=1,
        beat_draft=_two_segment_beat_draft(),
        segments=_two_source_segments(),
        payload=_manifest_with_distinct_planned_scenes(),
        target_video_model="hiagent",
        bible=None, conn=None, project_id="", aspect_ratio="9:16", enhance_music_bed=False,
        required_dialogue_by_segment_no={},
    )

    # calls[1] 是第 2 段的 task_payload，在模型写第 2 段提示词之前发出：此刻模型自己还没说
    # 本段场景是 scn_street，转场却已经是升级后的「叠化」——因为映射台为第 2 段登记的计划场景
    # （scn_street，与第 1 段计划场景 scn_room 不同）在生成前就已知。
    assert calls[1]["transition_from_previous"] == SCENE_CHANGE_TRANSITION
    assert calls[1]["scene_change"] is True
    assert result[2].camera_digest.transition_from_previous == SCENE_CHANGE_TRANSITION


@pytest.mark.asyncio
async def test_actual_scenes_diverging_from_plan_still_resolve_correctly_with_warning(monkeypatch, caplog):
    """映射台登记的计划场景与模型实际生成的 resources.scenes 不一致（场景映射本身不准）：
    最终字段仍以生成后的真实推导为准（不是生成前的计划），且要留一条可见告警供人工核查——
    不静默改写模型已经写好的正文。"""
    import logging

    import app.production.storyboard_pack as storyboard_pack_module

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        segment_no = payload["segment_no"]
        # 两段模型实际登记的场景都是同一个——与计划（scn_room/scn_street 不同）不一致。
        return _draft(f"提示词-段{segment_no}").model_copy(
            update={"resources": _AiSegmentResources(scenes=[{"scene_id": "scn_room"}])}
        )

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    with caplog.at_level(logging.WARNING, logger="app.production.storyboard_transition_plan"):
        result = await _generate_all_segment_prompts(
            episode_id="ep-transition-plan-drift",
            episode_no=1,
            beat_draft=_two_segment_beat_draft(),
            segments=_two_source_segments(),
            payload=_manifest_with_distinct_planned_scenes(),
            target_video_model="hiagent",
            bible=None, conn=None, project_id="", aspect_ratio="9:16", enhance_music_bed=False,
            required_dialogue_by_segment_no={},
        )

    # 第 2 段实际场景与第 1 段相同（scn_room），按同一套推导应该维持「硬切」，不是生成前告诉
    # 模型的「叠化」。
    from app.production.screenplay_markers import SAME_SCENE_TRANSITION

    assert result[2].camera_digest.transition_from_previous == SAME_SCENE_TRANSITION
    assert any("STORYBOARD_TRANSITION_DRIFT" in r.message for r in caplog.records)
