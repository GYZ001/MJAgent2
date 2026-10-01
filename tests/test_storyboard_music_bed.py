"""项目级「统一配乐」开关（``enhance_music_bed``）在分镜台阶段二的接线与回填
（2026-09-28 新增）。

三层覆盖：
1. ``app.production.storyboard_music_bed`` 纯函数单测——方言追加规则的开关分支、
   确定性回填的替换/追加/幂等三种形态（含红绿验证：手写一份「回填前」的朴素版本，
   证明它在自身输出上重跑会把目标短语二次嵌套，本模块的实现不会）。
2. 接线守卫（``inspect.getsource``）——两个函数确实被
   ``_generate_all_segment_prompts`` 调用，不是定义了却没接线，同
   ``tests/test_storyboard_pack_cast_wardrobe_wiring.py`` 一个先例。
3. 端到端：真的跑一遍 ``_generate_all_segment_prompts``（mock ``chat_structured``），
   验证开关关闭时 ``dialect_instructions``/``prompt_text`` 与关闭前逐字相同，开启时
   两者都按预期改写。
"""
from __future__ import annotations

import inspect
import json

import pytest

from app.production import storyboard_music_bed as music_bed
from app.production.storyboard_action_beats import decisive_action_dialect_rule
from app.production.storyboard_action_density import shot_action_beats_rule
from app.production.storyboard_pack import MAX_SHOTS_PER_SEGMENT
from app.production.storyboard_dialects import SEEDANCE_DIALECT_INSTRUCTIONS
from app.production.storyboard_pack import (
    _AiBeat,
    _AiBeatSheetDraft,
    _AiCameraDigest,
    _AiSegmentPlan,
    _AiStoryboardSegmentDraft,
    _generate_all_segment_prompts,
)
from app.production.storyboard_prop_visibility import prop_visibility_dialect_rule
from app.production.storyboard_shot_mandates import shot_mandates_dialect_rule
from app.source_excerpt import SourceSegment

_SEEDANCE_FORMAT = "seedance_compact_director_brief"
_H3_FORMAT = "minimax_h3_native_fields"


def _draft(prompt_text: str) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(prompt_text=prompt_text, shot_count=3, camera_digest=_AiCameraDigest())


# ---------------------------------------------------------------------------
# 1. 纯函数单测
# ---------------------------------------------------------------------------


def test_dialect_addendum_disabled_returns_empty_string():
    assert music_bed.music_bed_dialect_addendum(_SEEDANCE_FORMAT, enabled=False) == ""
    assert music_bed.music_bed_dialect_addendum(_H3_FORMAT, enabled=False) == ""


def test_dialect_addendum_enabled_selects_matching_dialect():
    seedance_text = music_bed.music_bed_dialect_addendum(_SEEDANCE_FORMAT, enabled=True)
    assert seedance_text.startswith("\n")
    assert music_bed.SEEDANCE_MUSIC_BED_RULE in seedance_text
    assert music_bed.NO_MUSIC_SEEDANCE_PHRASE in seedance_text

    h3_text = music_bed.music_bed_dialect_addendum(_H3_FORMAT, enabled=True)
    assert h3_text.startswith("\n")
    assert music_bed.MINIMAX_H3_MUSIC_BED_RULE in h3_text
    assert "non_diegetic_music" in h3_text


def test_backfill_disabled_leaves_prompt_text_untouched():
    original = "镜头1：……全片贯穿：环境音风声；配乐为悲伤小提琴；风格：写实；约束：面部一致。"
    draft = _draft(original)
    errors = music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_SEEDANCE_FORMAT, enabled=False)
    assert errors == []
    assert draft.prompt_text == original


def test_backfill_replaces_existing_music_clause():
    draft = _draft("全片贯穿：环境音风声呼啸；配乐为轻柔钢琴曲，逐渐增强；风格：写实；约束：面部一致。")
    errors = music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_SEEDANCE_FORMAT, enabled=True)
    assert errors == []
    assert music_bed.NO_MUSIC_SEEDANCE_PHRASE in draft.prompt_text
    assert "钢琴" not in draft.prompt_text
    assert "环境音风声呼啸" in draft.prompt_text  # 环境音描述本身不受影响
    assert "风格：写实" in draft.prompt_text  # 风格/约束不受影响


def test_backfill_appends_when_tail_has_no_music_clause():
    draft = _draft("全片贯穿：环境音风声呼啸；风格：写实；约束：面部一致。")
    music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_SEEDANCE_FORMAT, enabled=True)
    assert music_bed.NO_MUSIC_SEEDANCE_PHRASE in draft.prompt_text
    assert "环境音风声呼啸" in draft.prompt_text


def test_backfill_appends_when_tail_marker_missing_entirely():
    draft = _draft("镜头1：她站在窗边，望向远方。")
    music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_SEEDANCE_FORMAT, enabled=True)
    assert draft.prompt_text.startswith("镜头1：她站在窗边，望向远方。")
    assert music_bed.NO_MUSIC_SEEDANCE_PHRASE in draft.prompt_text


def test_backfill_blank_prompt_text_stays_blank():
    """空 prompt_text 不追加任何内容——不是本函数的职责去兜底一个从未生成的段落。"""
    draft = _draft(" ")
    music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_SEEDANCE_FORMAT, enabled=True)
    assert draft.prompt_text == " "


def test_backfill_h3_rewrites_non_diegetic_music_field_to_na():
    draft = _draft(
        "integrated_multimodal_description: [Shot 1] ...\n\n"
        "overall_soundscape: wind and distant footsteps\n\n"
        "non_diegetic_music: a slow solo piano with a swelling low string"
    )
    music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_H3_FORMAT, enabled=True)
    assert "non_diegetic_music: N/A" in draft.prompt_text
    assert "piano" not in draft.prompt_text
    assert "overall_soundscape: wind and distant footsteps" in draft.prompt_text  # 环境音字段不受影响


def test_backfill_h3_appends_field_when_missing():
    draft = _draft("integrated_multimodal_description: [Shot 1] ...\n\noverall_soundscape: wind")
    music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_H3_FORMAT, enabled=True)
    assert draft.prompt_text.strip().endswith("non_diegetic_music: N/A")


def _naive_seedance_rewrite_before_idempotency_guard(prompt_text: str) -> str:
    """手写的「回填前」朴素版本：与 ``music_bed._rewrite_seedance_music_phrase`` 相同
    的正则替换，但**没有**「目标短语已存在则跳过」的幂等短路——用于下面的红绿验证，
    不是线上代码的一部分，不导入自 ``app.production.storyboard_music_bed``。
    """
    import re

    clause_re = re.compile(r"配乐[：:为]?[^；;。\n]*")
    idx = prompt_text.rfind(music_bed._TAIL_MARKER)
    if idx < 0:
        return prompt_text
    head, tail = prompt_text[:idx], prompt_text[idx:]
    if not clause_re.search(tail):
        return prompt_text
    return head + clause_re.sub(music_bed.NO_MUSIC_SEEDANCE_PHRASE, tail, count=1)


def test_red_naive_rewrite_without_idempotency_guard_double_nests_on_replay():
    """红：朴素版本（无幂等短路）在自己的输出上重跑，会把目标短语嵌套进它自己——
    目标短语本身含有「配乐」两个字（"...成片统一配乐)..."），朴素正则会把它当成
    又一次命中再替换一遍。这正是 ``ensure_no_music_bed_in_prompt`` 必须先检查
    「目标短语是否已存在」才能动手的原因。
    """
    original = "全片贯穿：环境音风声呼啸；配乐为轻柔钢琴曲；风格：写实；约束：面部一致。"
    once = _naive_seedance_rewrite_before_idempotency_guard(original)
    twice = _naive_seedance_rewrite_before_idempotency_guard(once)
    assert once != twice, "朴素版本应当在第二次重跑时继续改写（暴露非幂等缺陷）"
    assert once.count(music_bed.NO_MUSIC_SEEDANCE_PHRASE) == 1
    assert twice.count(music_bed.NO_MUSIC_SEEDANCE_PHRASE) >= 1
    assert "无任何背景音乐（成片统一无任何背景音乐" in twice, "朴素版本把目标短语嵌套进了自己"


def test_green_actual_backfill_is_idempotent_on_replay():
    """绿：线上实现对同一段 prompt_text 重复应用两次，结果与只应用一次完全相同。"""
    draft = _draft("全片贯穿：环境音风声呼啸；配乐为轻柔钢琴曲；风格：写实；约束：面部一致。")
    music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_SEEDANCE_FORMAT, enabled=True)
    once = draft.prompt_text
    music_bed.ensure_no_music_bed_in_prompt(draft, render_format=_SEEDANCE_FORMAT, enabled=True)
    assert draft.prompt_text == once
    assert draft.prompt_text.count(music_bed.NO_MUSIC_SEEDANCE_PHRASE) == 1


# ---------------------------------------------------------------------------
# 2. 接线守卫：确实被 _generate_all_segment_prompts 调用，不是定义了没接线
# ---------------------------------------------------------------------------


def _dialect_instructions_source() -> str:
    """2026-10-01 换场并行链：dialect_instructions 拼接搬进
    ``storyboard_segment_chains._task_payload_dialect_instructions``（原在
    ``_generate_all_segment_prompts`` 的 task_payload 字面量里），见该模块
    docstring。"""
    import app.production.storyboard_segment_chains as chains_module

    return inspect.getsource(chains_module._task_payload_dialect_instructions)


def _segment_validate_source() -> str:
    """2026-10-01 换场并行链：原 validate=lambda 回调搬进
    ``storyboard_segment_chains._segment_validate``（普通函数，不再是 lambda），
    见该模块 docstring。"""
    import app.production.storyboard_segment_chains as chains_module

    return inspect.getsource(chains_module._segment_validate)


def test_music_bed_dialect_addendum_is_concatenated_into_dialect_instructions():
    source = _dialect_instructions_source()
    assert "_music_bed.music_bed_dialect_addendum(ctx.profile.render_format, enabled=ctx.enhance_music_bed)" in source


def test_music_bed_backfill_is_called_in_validate_callback():
    source = _segment_validate_source()
    assert (
        "_music_bed.ensure_no_music_bed_in_prompt(value, render_format=ctx.profile.render_format, "
        "enabled=ctx.enhance_music_bed)"
    ) in source


def test_enhance_music_bed_is_an_explicit_required_parameter():
    """所有权必须显式（CLAUDE.md）：这不是一个带默认值的可选参数，漏传在调用
    那一刻就是 TypeError，不会悄悄按「关闭」处理某个忘了传的调用方。"""
    import app.production.storyboard_pack as storyboard_pack_module

    sig = inspect.signature(storyboard_pack_module._generate_all_segment_prompts)
    assert sig.parameters["enhance_music_bed"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# 3. 端到端：真的跑一遍 _generate_all_segment_prompts
# ---------------------------------------------------------------------------


def _single_segment_beat_draft() -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="她走出了房间", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"])],
    )


def _single_source_segment() -> list[SourceSegment]:
    return [SourceSegment(segment_id="s1", text="她站在窗边，没有说话。", start_offset=0, end_offset=11)]


@pytest.mark.asyncio
async def test_disabled_leaves_dialect_instructions_and_prompt_text_byte_identical(monkeypatch):
    """开关关闭时的回归：dialect_instructions 与最终 prompt_text 都必须与本次改动
    之前逐字相同（CLAUDE.md「废止/新增功能时未启用分支必须逐字不变」同一精神）。
    """
    import app.production.storyboard_pack as storyboard_pack_module

    captured: dict = {}
    model_prompt_text = "全片贯穿：环境音风声呼啸；配乐为轻柔钢琴曲；风格：写实；约束：面部一致。"

    async def fake_chat_structured(messages, **kwargs):
        captured["dialect_instructions"] = json.loads(messages[1]["content"])["dialect_instructions"]
        draft = _draft(model_prompt_text)
        # 真实生产路径里 model_gateway.chat_structured 会调用这个 validate 回调
        # （见 _generate_all_segment_prompts 的 validate=lambda ...），本次改动的
        # 确定性回填就挂在它身上；这里完整 stub 掉 chat_structured 本身，因此需要
        # 手动补一次这次调用才能覆盖到同一条代码路径，不只是断言源码字符串存在。
        kwargs["validate"](draft)
        return draft

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    result = await _generate_all_segment_prompts(
        episode_id="ep-music-bed-off", episode_no=1, beat_draft=_single_segment_beat_draft(),
        segments=_single_source_segment(), payload={}, target_video_model="hiagent",
        bible=None, conn=None, project_id="", aspect_ratio="9:16", enhance_music_bed=False,
        required_dialogue_by_segment_no={},
    )

    expected_dialect_instructions = (
        f"{SEEDANCE_DIALECT_INSTRUCTIONS}\n{decisive_action_dialect_rule(_SEEDANCE_FORMAT)}"
        f"\n{shot_action_beats_rule(max_shots=MAX_SHOTS_PER_SEGMENT)}"
        f"\n{shot_mandates_dialect_rule(_SEEDANCE_FORMAT)}"
        f"\n{prop_visibility_dialect_rule(_SEEDANCE_FORMAT)}"
    )
    assert captured["dialect_instructions"] == expected_dialect_instructions
    assert result[1].prompt_text == model_prompt_text  # 没有任何确定性回填改写它


@pytest.mark.asyncio
async def test_enabled_extends_dialect_instructions_and_backfills_prompt_text(monkeypatch):
    import app.production.storyboard_pack as storyboard_pack_module

    captured: dict = {}
    model_prompt_text = "全片贯穿：环境音风声呼啸；配乐为轻柔钢琴曲；风格：写实；约束：面部一致。"

    async def fake_chat_structured(messages, **kwargs):
        captured["dialect_instructions"] = json.loads(messages[1]["content"])["dialect_instructions"]
        draft = _draft(model_prompt_text)
        # 真实生产路径里 model_gateway.chat_structured 会调用这个 validate 回调
        # （见 _generate_all_segment_prompts 的 validate=lambda ...），本次改动的
        # 确定性回填就挂在它身上；这里完整 stub 掉 chat_structured 本身，因此需要
        # 手动补一次这次调用才能覆盖到同一条代码路径，不只是断言源码字符串存在。
        kwargs["validate"](draft)
        return draft

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    result = await _generate_all_segment_prompts(
        episode_id="ep-music-bed-on", episode_no=1, beat_draft=_single_segment_beat_draft(),
        segments=_single_source_segment(), payload={}, target_video_model="hiagent",
        bible=None, conn=None, project_id="", aspect_ratio="9:16", enhance_music_bed=True,
        required_dialogue_by_segment_no={},
    )

    assert music_bed.SEEDANCE_MUSIC_BED_RULE in captured["dialect_instructions"]
    assert music_bed.NO_MUSIC_SEEDANCE_PHRASE in result[1].prompt_text
    assert "钢琴" not in result[1].prompt_text
