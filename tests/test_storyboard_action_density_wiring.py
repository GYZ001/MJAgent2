"""单镜动作密度闸门的接线验收（2026-09-30 评审确认补写）。

tests/test_storyboard_action_density.py 只测
``app.production.storyboard_action_density`` 模块内的纯函数（直接 new 一个
``ActionDensitySoftCheck``/手工调用 ``segment_advisories``，不经过
``storyboard_pack`` 里真实的实例化/拼接代码）；本文件补上端到端一层——真的走
``_generate_all_segment_prompts`` 的 ``validate`` 回调，证明 ``action_gate.
filter(value.shot_action_beats, shot_count=value.shot_count)`` 与
``_action_density.segment_advisories(draft.shot_action_beats, shot_count=
draft.shot_count)`` 这两处接线确实生效，不是定义了却没接线（与
tests/test_storyboard_music_bed.py 端到端用例同一先例：完整 stub 掉
``model_gateway.chat_structured``，因此需要手动补一次 ``validate`` 调用才能
覆盖到同一条代码路径，不只是断言源码字符串存在）。
"""
from __future__ import annotations

import pytest

from app.production.storyboard_action_density import ShotActionBeats
from app.production.storyboard_pack import (
    _AiBeat,
    _AiBeatSheetDraft,
    _AiCameraDigest,
    _AiContinuityMemo,
    _AiSegmentPlan,
    _AiStoryboardSegmentDraft,
    _generate_all_segment_prompts,
)
from app.source_excerpt import SourceSegment


def _draft(prompt_text: str) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(
        prompt_text=prompt_text,
        shot_count=3,
        camera_digest=_AiCameraDigest(),
        # continuity_memo.time_of_day 是与动作密度无关的另一条阻断判据，默认
        # 空串会让 validate 永远非空、混淆本文件要测的信号，这里填一个合法值
        # 排除干扰（同 tests/test_storyboard_pack_wiring.py 的 ``_draft`` 先例）。
        continuity_memo=_AiContinuityMemo(time_of_day="白天"),
    )


def _single_segment_beat_draft() -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="占位", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"])],
    )


def _single_source_segment() -> list[SourceSegment]:
    return [SourceSegment(segment_id="s1", text="占位原文。", start_offset=0, end_offset=4)]


@pytest.mark.asyncio
async def test_over_limit_retries_then_degrades_with_visible_signal(monkeypatch):
    """shot_action_beats 超过 MAX_KEY_ACTIONS_PER_SHOT 时：前 hard_attempts=2
    次必须被打回重试，第 3 次用尽后放行，但最终 degraded_capabilities 必须
    独立出现 STORYBOARD_PACK_ACTION_DENSITY 标记——放行不等于没检查过
    （CLAUDE.md「空集合不等于无需检查」「放行分支必须是产品里的可见信号」）。
    """
    import app.production.storyboard_pack as storyboard_pack_module

    over_limit_beats = [ShotActionBeats(shot_no=1, key_actions=["推门", "开灯", "跟进"])]
    attempts_seen: list[int] = []

    async def fake_chat_structured(messages, **kwargs):
        draft = _draft("提示词-段1").model_copy(update={"shot_action_beats": over_limit_beats})
        # 真实 model_gateway.chat_structured 内部会对同一份 validate 回调连续
        # 调用最多 semantic_retry_limit+1 次（前两次超限打回，第三次 action_gate
        # 用尽 hard_attempts 降级放行）——这里手工补齐同一条代码路径。
        for attempt in range(1, 4):
            attempts_seen.append(attempt)
            errors = kwargs["validate"](draft)
            if not errors:
                break
        else:
            raise AssertionError("超出模拟重试上限仍未放行")
        return draft

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    result = await _generate_all_segment_prompts(
        episode_id="ep-action-density-over-limit", episode_no=1, beat_draft=_single_segment_beat_draft(),
        segments=_single_source_segment(), payload={}, target_video_model="hiagent",
        bible=None, conn=None, project_id="", aspect_ratio="9:16", enhance_music_bed=False,
        required_dialogue_by_segment_no={},
    )

    assert attempts_seen == [1, 2, 3], "前两次应被打回重试，第三次用尽 hard_attempts 才放行"
    tagged = [c for c in result[1].degraded_capabilities if "STORYBOARD_PACK_ACTION_DENSITY" in c]
    assert tagged, "超限自报清单在重试耗尽后放行，degraded_capabilities 必须仍留下可见信号"
    assert "[STORYBOARD_PACK_ACTION_DENSITY]" in tagged[0]
    assert "未拦截" in tagged[0]


@pytest.mark.asyncio
async def test_undeclared_shots_retry_then_degrade_with_distinct_visible_signal(monkeypatch):
    """完全不申报 shot_action_beats（或只申报部分镜头）时：同样先被打回重试
    （给模型真实机会补申报），用尽 hard_attempts 后放行，但 degraded_
    capabilities 必须出现区别于「超限」的独立标记
    STORYBOARD_PACK_ACTION_DENSITY_UNDECLARED——不能和「真的检查过且合规」
    共用同一个空列表出口（评审确认问题：模型可以靠完全不声明该字段来让闸门
    静默放行且不留痕）。
    """
    import app.production.storyboard_pack as storyboard_pack_module

    attempts_seen: list[int] = []

    async def fake_chat_structured(messages, **kwargs):
        draft = _draft("提示词-段1")  # shot_action_beats 保持默认空列表——完全不申报
        for attempt in range(1, 4):
            attempts_seen.append(attempt)
            errors = kwargs["validate"](draft)
            if not errors:
                break
        else:
            raise AssertionError("超出模拟重试上限仍未放行")
        return draft

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    result = await _generate_all_segment_prompts(
        episode_id="ep-action-density-undeclared", episode_no=1, beat_draft=_single_segment_beat_draft(),
        segments=_single_source_segment(), payload={}, target_video_model="hiagent",
        bible=None, conn=None, project_id="", aspect_ratio="9:16", enhance_music_bed=False,
        required_dialogue_by_segment_no={},
    )

    assert attempts_seen == [1, 2, 3], "前两次应被打回重试，第三次用尽 hard_attempts 才放行"
    tagged = [c for c in result[1].degraded_capabilities if "STORYBOARD_PACK_ACTION_DENSITY_UNDECLARED" in c]
    assert tagged, "完全不申报在重试耗尽后放行，degraded_capabilities 必须仍留下可见信号"
    assert "未拦截" in tagged[0]
    assert not any(
        c.startswith("[STORYBOARD_PACK_ACTION_DENSITY]") for c in result[1].degraded_capabilities
    ), "未申报不能被误报成「超限」——两者是不同判据，标记必须能区分"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
