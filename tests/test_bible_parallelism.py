from __future__ import annotations

import asyncio
import json

import pytest

from app import stages


def test_merge_roll_call_candidates_merges_formal_name_and_caps_evidence() -> None:
    ev = stages._RosterOnstageEvidence
    candidate = stages._RosterCandidate
    merged = stages._merge_roll_call_candidates([
        [candidate(primary_appellation="小胖子", formal_name="李富贵", onstage_evidence=[
            ev(chapter_index=1, quote="小胖子甲"), ev(chapter_index=2, quote="小胖子乙")
        ])],
        [candidate(primary_appellation="李富贵", formal_name="李富贵", onstage_evidence=[
            ev(chapter_index=3, quote="李富贵丙"), ev(chapter_index=4, quote="李富贵丁")
        ])],
    ])
    assert len(merged) == 1
    assert merged[0].formal_name == "李富贵"
    assert len(merged[0].onstage_evidence) == stages.BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE


def test_character_detail_evidence_pack_is_bounded_and_relevant() -> None:
    chapters = [
        {"idx": i, "content": ("无关内容。" * 500) + ("孟浩拔剑。" if i % 2 == 0 else "")}
        for i in range(1, 30)
    ]
    pack = stages._character_detail_evidence_pack(chapters, ["孟浩"])
    assert "孟浩" in pack
    assert len(pack) <= stages.BIBLE_DETAIL_EVIDENCE_MAX_CHARS
    assert pack.count("·证据】") <= stages.BIBLE_DETAIL_EVIDENCE_MAX_SEGMENTS


@pytest.mark.asyncio
async def test_generate_character_detail_batch_retries_only_failed_character(monkeypatch) -> None:
    attempts: dict[str, int] = {}

    async def fake_chat(messages, **_kwargs):
        prompt = messages[-1]["content"]
        name = "甲" if "目标角色：甲" in prompt else "乙"
        attempts[name] = attempts.get(name, 0) + 1
        if name == "甲" and attempts[name] == 1:
            return "{}"
        return json.dumps({
            "appearance_canonical": "黑色短发，青色长衫，身形修长，腰系深色布带，脚穿布靴",
            "personality": "沉稳",
            "speech_style": "句式简短，语气平稳，少用修饰",
            "relationships": [], "aliases": [], "source_evidence": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    entries = [
        stages._BibleRosterEntry(name="甲", role="主角"),
        stages._BibleRosterEntry(name="乙", role="重要配角"),
    ]
    result = await stages._generate_character_detail_batch(
        entries,
        [{"idx": 1, "content": "甲与乙同行。"}],
        style="国漫三维动画电影质感，统一自然光影与细腻材质",
        chapters_by_idx={1: "甲与乙同行。"},
        project_id="p1",
    )
    assert [item.name for item in result] == ["甲", "乙"]
    assert attempts == {"甲": 2, "乙": 1}


@pytest.mark.asyncio
async def test_mentioned_only_character_skips_detail_model_and_portrait() -> None:
    entry = stages._BibleRosterEntry(
        name="靠山老祖",
        role="关键伏笔角色",
        presence_status="mentioned_only",
        importance_score=18.4,
        importance_signals=["fulltext_mentions:3", "retained_by_plot_authority"],
        portrait_eligible=False,
        appearance_status="deferred",
    )
    result = await stages._generate_character_detail_batch(
        [entry],
        [{"idx": 1, "content": "靠山老祖定下门规。"}],
        style="国漫三维动画电影质感，统一自然光影与细腻材质",
        chapters_by_idx={1: "靠山老祖定下门规。"},
        project_id="p1",
    )
    assert len(result) == 1
    character = result[0]
    assert character.presence_status == "mentioned_only"
    assert character.appearance_status == "deferred"
    assert character.portrait_eligible is False
    assert character.source_evidence == []


def test_normalize_roster_prefers_real_name_and_marks_mentioned_only() -> None:
    draft = stages._BibleRosterDraft(
        characters=[stages._BibleRosterEntry(name="小胖子", role="重要配角")],
        world={"visual_style_canonical": "国漫三维动画电影质感，统一自然光影与细腻材质"},
    )
    normalized = stages._normalize_roster_against_candidates(draft, [
        ("小胖子", "李富贵", 2, 16, 6, ["虎头虎脑少年"]),
        ("靠山老祖", "", 0, 4, 3, []),
    ])
    assert [entry.name for entry in normalized.characters] == ["李富贵", "靠山老祖"]
    assert normalized.characters[0].source_appellations == ["小胖子", "虎头虎脑少年"]
    assert normalized.characters[0].portrait_eligible is True
    assert normalized.characters[1].presence_status == "mentioned_only"
    assert normalized.characters[1].appearance_status == "deferred"
    assert normalized.characters[1].portrait_eligible is False


def test_high_frequency_nickname_stays_primary_name_and_real_name_is_searchable() -> None:
    """真名罕见时保留高频绰号作主名，真名仍作为检索键映射到同一角色。"""
    chapters = [{"idx": i, "content": "小胖子说道。" * 20} for i in range(1, 11)]
    chapters[0]["content"] += "他本名李富贵。"
    canonical, demoted = stages._pick_canonical_display_name("小胖子", "李富贵", chapters)
    assert canonical == "小胖子"
    assert demoted == ["李富贵"]

    draft = stages._BibleRosterDraft(
        characters=[stages._BibleRosterEntry(name="李富贵", role="重要配角")],
        world={"visual_style_canonical": "国漫三维动画电影质感，统一自然光影与细腻材质"},
    )
    normalized = stages._normalize_roster_against_candidates(
        draft, [("小胖子", "李富贵", 2, 200, 10, [])], chapters,
    )
    entry = normalized.characters[0]
    assert entry.name == "小胖子"
    assert "李富贵" in entry.source_appellations


def test_protagonist_is_assigned_by_fulltext_signals_not_model() -> None:
    """覆盖最广的角色必须成为主角，即使在场裁决一条都没通过。"""
    chapters = [{"idx": i, "content": "孟浩走上前。" * 30} for i in range(1, 21)]
    draft = stages._BibleRosterDraft(
        characters=[stages._BibleRosterEntry(name="李富贵", role="主角")],
        world={"visual_style_canonical": "国漫三维动画电影质感，统一自然光影与细腻材质"},
    )
    normalized = stages._normalize_roster_against_candidates(draft, [
        ("孟浩", "", 0, 991, 20, []),
        ("李富贵", "", 3, 30, 4, []),
    ], chapters)
    roles = {entry.name: entry.role for entry in normalized.characters}
    assert roles["孟浩"] == "主角"
    assert roles["李富贵"] == "重要配角"
    protagonist = normalized.characters[0]
    assert protagonist.presence_status == "onstage"
    assert protagonist.portrait_eligible is True
    assert "presence_by_fulltext_coverage" in protagonist.importance_signals


@pytest.mark.asyncio
async def test_roll_call_sends_small_parallel_chunks(monkeypatch) -> None:
    active = 0
    peak = 0
    inputs: list[str] = []

    async def fake_chat(messages, **kwargs):
        nonlocal active, peak
        if (kwargs.get("call_meta") or {}).get("stage_key") != "character_roll_call":
            model_type = kwargs["model_type"]
            return model_type(verdict="onstage", supporting_segment_index=1)
        inputs.append(messages[-1]["content"])
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return json.dumps({"candidates": []})

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    chapters = [{"idx": i, "title": f"第{i}章", "content": "正文" * 100} for i in range(1, 11)]
    await stages._recurring_character_names(chapters)
    assert len(inputs) == 10
    assert peak > 1
    assert all(len(item) < stages.BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS + 2000 for item in inputs)
