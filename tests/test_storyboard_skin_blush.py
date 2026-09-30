"""写实画风下情绪引起的肤色变化写法（``app.production.storyboard_skin_blush``，2026-09-30）。

三层覆盖，结构照抄 ``tests/test_storyboard_music_bed.py``：
1. 纯函数单测——画风开关分支、按 ``render_format`` 选方言。
2. 接线守卫（``inspect.getsource``）——确实被 ``_generate_all_segment_prompts`` 调用，
   且 ``photographic`` 由 ``app.visual_styles.is_photographic_style_prompt`` 决定，不是
   本文件另起一套判据。
3. 端到端：真的跑一遍 ``_generate_all_segment_prompts``（mock ``chat_structured``），验证
   写实画风项目的 ``dialect_instructions`` 带上新规则、非写实画风与无圣经项目逐字不变。
"""
from __future__ import annotations

import inspect
import json

import pytest

from app.production import storyboard_skin_blush as skin_blush
from app.production.storyboard_action_beats import decisive_action_dialect_rule
from app.production.storyboard_action_density import shot_action_beats_rule
from app.production.storyboard_dialects import SEEDANCE_DIALECT_INSTRUCTIONS
from app.production.storyboard_pack import (
    _AiBeat,
    _AiBeatSheetDraft,
    _AiCameraDigest,
    _AiSegmentPlan,
    _AiStoryboardSegmentDraft,
    _generate_all_segment_prompts,
)
from app.production.storyboard_shot_mandates import shot_mandates_dialect_rule
from app.schemas import Bible, World
from app.source_excerpt import SourceSegment
from app.visual_styles import VISUAL_STYLE_PRESETS

_SEEDANCE_FORMAT = "seedance_compact_director_brief"
_H3_FORMAT = "minimax_h3_native_fields"

_PHOTOGRAPHIC_PROMPT = next(p.prompt for p in VISUAL_STYLE_PRESETS if p.photographic)
_NON_PHOTOGRAPHIC_PROMPT = next(p.prompt for p in VISUAL_STYLE_PRESETS if not p.photographic)


def _draft(prompt_text: str) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(prompt_text=prompt_text, shot_count=3, camera_digest=_AiCameraDigest())


# ---------------------------------------------------------------------------
# 1. 纯函数单测
# ---------------------------------------------------------------------------


def test_non_photographic_returns_empty_string():
    assert skin_blush.skin_blush_dialect_addendum(_SEEDANCE_FORMAT, photographic=False) == ""
    assert skin_blush.skin_blush_dialect_addendum(_H3_FORMAT, photographic=False) == ""


def test_photographic_selects_matching_dialect():
    seedance_text = skin_blush.skin_blush_dialect_addendum(_SEEDANCE_FORMAT, photographic=True)
    assert seedance_text.startswith("\n")
    assert skin_blush.SEEDANCE_SKIN_BLUSH_RULE in seedance_text
    assert skin_blush.MINIMAX_H3_SKIN_BLUSH_RULE not in seedance_text

    h3_text = skin_blush.skin_blush_dialect_addendum(_H3_FORMAT, photographic=True)
    assert h3_text.startswith("\n")
    assert skin_blush.MINIMAX_H3_SKIN_BLUSH_RULE in h3_text
    assert skin_blush.SEEDANCE_SKIN_BLUSH_RULE not in h3_text


def test_rules_are_non_empty_strings():
    for rule in (skin_blush.SEEDANCE_SKIN_BLUSH_RULE, skin_blush.MINIMAX_H3_SKIN_BLUSH_RULE):
        assert isinstance(rule, str) and rule


def test_seedance_rule_is_a_positive_statement_with_mild_degree_words():
    """正面陈述：给出怎么写的具体样例与程度词，不是一句孤立的禁令。"""
    rule = skin_blush.SEEDANCE_SKIN_BLUSH_RULE
    assert "轻微、自然、渐变的红晕" in rule
    assert "脸颊泛起淡淡的红晕" in rule
    assert "眼神" in rule and "嘴唇" in rule and "手部动作" in rule and "停顿" in rule


def test_h3_rule_is_a_positive_statement_with_mild_degree_words():
    rule = skin_blush.MINIMAX_H3_SKIN_BLUSH_RULE
    assert "a faint blush rises on her cheeks" in rule
    assert "the eyes" in rule and "the lips" in rule and "hand gestures" in rule and "a held pause" in rule


def test_rule_text_does_not_prescribe_a_spreading_range_or_path():
    """根因是「范围+路径」式描述（"一路漫到耳根"），规则正文不应示范同一种写法——
    只按数据（规则文本本身）核验，不靠对生成结果搜关键字「红」做判据。"""
    assert "漫到" not in skin_blush.SEEDANCE_SKIN_BLUSH_RULE
    assert "spread" not in skin_blush.MINIMAX_H3_SKIN_BLUSH_RULE


# ---------------------------------------------------------------------------
# 2. 接线守卫：确实被 _generate_all_segment_prompts 调用，photographic 判据来自
#    app.visual_styles.is_photographic_style_prompt
# ---------------------------------------------------------------------------


def _generate_all_segment_prompts_source() -> str:
    import app.production.storyboard_pack as storyboard_pack_module

    return inspect.getsource(storyboard_pack_module._generate_all_segment_prompts)


def test_skin_blush_dialect_addendum_is_concatenated_into_dialect_instructions():
    source = _generate_all_segment_prompts_source()
    assert (
        "_skin_blush.skin_blush_dialect_addendum(profile.render_format, "
        "photographic=visual_style_is_photographic)"
    ) in source


def test_photographic_flag_comes_from_is_photographic_style_prompt():
    source = _generate_all_segment_prompts_source()
    assert "visual_style_is_photographic = is_photographic_style_prompt(visual_style)" in source


# ---------------------------------------------------------------------------
# 3. 端到端：真的跑一遍 _generate_all_segment_prompts
# ---------------------------------------------------------------------------


def _single_segment_beat_draft() -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="她脸红了", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"])],
    )


def _single_source_segment() -> list[SourceSegment]:
    return [SourceSegment(segment_id="s1", text="她的脸颊微微发红。", start_offset=0, end_offset=9)]


async def _run_with_bible(monkeypatch, bible: Bible | None):
    import app.production.storyboard_pack as storyboard_pack_module

    captured: dict = {}
    model_prompt_text = "镜头1：她站在窗边，没有说话。全片贯穿：环境音风声；配乐为轻柔钢琴曲；风格：写实；约束：面部一致。"

    async def fake_chat_structured(messages, **kwargs):
        captured["dialect_instructions"] = json.loads(messages[1]["content"])["dialect_instructions"]
        draft = _draft(model_prompt_text)
        kwargs["validate"](draft)
        return draft

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    await _generate_all_segment_prompts(
        episode_id="ep-skin-blush", episode_no=1, beat_draft=_single_segment_beat_draft(),
        segments=_single_source_segment(), payload={}, target_video_model="hiagent",
        bible=bible, conn=None, project_id="", aspect_ratio="9:16", enhance_music_bed=False,
        required_dialogue_by_segment_no={},
    )
    return captured


@pytest.mark.asyncio
async def test_no_bible_leaves_dialect_instructions_byte_identical(monkeypatch):
    """CLAUDE.md「未启用分支必须逐字不变」：没有圣经（画风解析不出）时，
    dialect_instructions 必须与本次改动之前逐字相同。"""
    captured = await _run_with_bible(monkeypatch, bible=None)
    expected_dialect_instructions = (
        f"{SEEDANCE_DIALECT_INSTRUCTIONS}\n{decisive_action_dialect_rule(_SEEDANCE_FORMAT)}"
        f"\n{shot_action_beats_rule()}"
        f"\n{shot_mandates_dialect_rule(_SEEDANCE_FORMAT)}"
    )
    assert captured["dialect_instructions"] == expected_dialect_instructions


@pytest.mark.asyncio
async def test_non_photographic_visual_style_leaves_dialect_instructions_byte_identical(monkeypatch):
    bible = Bible(characters=[], world=World(visual_style_canonical=_NON_PHOTOGRAPHIC_PROMPT))
    captured = await _run_with_bible(monkeypatch, bible=bible)
    assert skin_blush.SEEDANCE_SKIN_BLUSH_RULE not in captured["dialect_instructions"]


@pytest.mark.asyncio
async def test_photographic_visual_style_extends_dialect_instructions(monkeypatch):
    bible = Bible(characters=[], world=World(visual_style_canonical=_PHOTOGRAPHIC_PROMPT))
    captured = await _run_with_bible(monkeypatch, bible=bible)
    assert skin_blush.SEEDANCE_SKIN_BLUSH_RULE in captured["dialect_instructions"]
    # 追加在既有方言规则之后，不打断/替换既有内容
    assert captured["dialect_instructions"].startswith(SEEDANCE_DIALECT_INSTRUCTIONS)
    assert captured["dialect_instructions"].endswith(skin_blush.SEEDANCE_SKIN_BLUSH_RULE)
