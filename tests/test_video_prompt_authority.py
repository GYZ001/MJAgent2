"""视频提示词的权威声轨来源（原属 tests/test_video_prompt_ai.py，超 500 行上限拆出）。

2026-09-10 实测「我欲封天」EP2-EP10 的 197 个镜头，shot_contract_json 里
``audio_timeline`` 全为空而 ``dialogues`` 有词。``_expected_dialogue`` 曾直读
``audio_timeline`` 裸字段，于是「dialogue 必须逐字保留权威声轨」这条比对拿空列表
当标准答案，实际生效的断言退化成「dialogue 数量必须为 0」——恒真。
"""
from __future__ import annotations

import asyncio

import pytest

from app import video_prompt_ai
from app.schemas import Dialogue, Shot

from tests.test_video_prompt_ai import _bible, _draft, _shot


# ---------------------------------------------------------------------------
# 权威声轨来源：2026-09-10 实测「我欲封天」EP2-EP10 的 197 个镜头，
# shot_contract_json 里 audio_timeline 全为空而 dialogues 有词。_expected_dialogue
# 曾直读 audio_timeline 裸字段，于是「dialogue 必须逐字保留权威声轨」这条比对拿空
# 列表当标准答案，实际生效的断言退化成「dialogue 数量必须为 0」——恒真。
# ---------------------------------------------------------------------------

def _shot_without_timeline() -> Shot:
    """线上真实形态：dialogues 有词、audio_timeline 空。"""
    shot = _shot()
    shot.audio_timeline = []
    shot.dialogues = [
        Dialogue(speaker="甲", line="别走。", emotion="坚定"),
    ]
    return shot


def test_empty_audio_timeline_falls_back_to_shot_dialogues() -> None:
    expected = video_prompt_ai._expected_dialogue(_shot_without_timeline())
    assert [item["text"] for item in expected] == ["别走。"]
    assert [item["speaker"] for item in expected] == ["甲"]
    assert expected[0]["start_s"] is None and expected[0]["end_s"] is None


def test_empty_timeline_no_longer_lets_a_silent_prompt_pass() -> None:
    """这正是恒真断言放过去的形态：台账记了台词，草稿一句不说。"""
    shot = _shot_without_timeline()
    draft = _draft()
    draft.dialogue = []

    errors = video_prompt_ai.validate_ai_video_prompt(draft, shot=shot)

    assert any("dialogue 数量必须为 1" in error for error in errors)


def test_reworded_line_is_rejected_even_without_a_timeline() -> None:
    """第 10 集实测：原文自带错别字「整个外宗五人不知」被提示词"顺手改正"成
    「无人不敬佩」。提示词规则明写「错别字均照录」，这里必须拦住。"""
    shot = _shot_without_timeline()
    draft = _draft()
    draft.dialogue[0].text = "别走啊。"

    errors = video_prompt_ai.validate_ai_video_prompt(draft, shot=shot)

    assert any("逐字" in error for error in errors)


def test_untimed_authority_does_not_demand_time_codes() -> None:
    """原文台词本来就不带时码，编一个出来再拿它当判据就是用猜测换猜测。
    说话人/原话/发声方式仍然逐字比。"""
    shot = _shot_without_timeline()
    draft = _draft()
    draft.dialogue[0].start_s = 1.9
    draft.dialogue[0].end_s = 3.3

    errors = video_prompt_ai.validate_ai_video_prompt(draft, shot=shot)

    assert not any("逐字" in error for error in errors)


def test_timeline_still_wins_when_it_has_real_speech() -> None:
    """timeline 有真实口播轨时它仍是权威（带时码），回退只在它为空时发生。"""
    shot = _shot()
    assert shot.audio_timeline
    expected = video_prompt_ai._expected_dialogue(shot)
    assert [item["text"] for item in expected] == ["别走。"]
    assert expected[0]["start_s"] == pytest.approx(0.4)
    draft = _draft()
    draft.dialogue[0].start_s = 1.9
    assert any("逐时码" in e for e in video_prompt_ai.validate_ai_video_prompt(draft, shot=shot))


def test_payload_carries_the_authoritative_dialogue_when_timeline_is_empty(monkeypatch) -> None:
    """模型答不出来时先查它有没有收到标准答案：payload 曾经喂的是恒空的
    shot.audio_timeline 裸字段，模型从没见过这一镜要说的话。"""
    import json

    shot = _shot_without_timeline()
    draft = _draft()
    captured: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        captured.append(json.loads(messages[-1]["content"]))
        return draft

    monkeypatch.setattr(video_prompt_ai.model_gateway, "chat_structured", fake_chat_structured)
    asyncio.run(video_prompt_ai.generate_ai_video_prompt(
        shot=shot, bible=_bible(), continuity_contract="[START STATE]\n两人尚未接触。",
        video_generation_mode="REFERENCE_IMAGE_MODE", operation_scope="ver_test",
    ))

    authority = captured[0]["shot_contract"]["authoritative_dialogue"]
    assert [item["text"] for item in authority] == ["别走。"]
    assert "audio_timeline" not in captured[0]["shot_contract"]


def test_contract_version_bumped_so_v2_drafts_are_recomputed() -> None:
    """旧草稿是在「权威恒空」下生成的，必须重算而不是命中缓存——
    app/media_exec/input_video_mode.py 按这个常量判定要不要复用。"""
    assert video_prompt_ai.AI_VIDEO_PROMPT_CONTRACT_VERSION == "ai_model_adaptive_prompt_v3"


def test_authority_helper_is_shared_not_duplicated() -> None:
    """校验判据与 payload 必须是同一个函数——两份副本迟早会分叉，而分叉的那一份
    就是模型收到的标准答案与验收标准不一致的那次事故。"""
    from app import video_prompt_payload

    assert video_prompt_ai._expected_dialogue is video_prompt_payload.authoritative_dialogue


def test_payload_module_does_not_import_back_into_video_prompt_ai() -> None:
    """依赖单向：video_prompt_ai → video_prompt_payload。反向 import 会成环。"""
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "app" / "video_prompt_payload.py").read_text(encoding="utf-8")
    modules = {
        node.module for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import) for alias in node.names
    }
    assert "app.video_prompt_ai" not in modules
