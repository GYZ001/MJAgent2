from __future__ import annotations

import asyncio
import json

import pytest

from app import config, errors, stages
from app.schemas import Character, character_is_portrait_eligible


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
    person_kept = stages._merge_roll_call_candidates([
        [candidate(primary_appellation="许师姐", personhood="person")],
        [candidate(primary_appellation="许师姐", personhood="uncertain")],
    ])
    assert person_kept[0].personhood == "person"


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
            "period_costume_canonical": "青布长衫布靴，束发挽髻，禁用现代面料拉链",
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


def test_character_is_portrait_eligible_defaults_and_gates() -> None:
    old = Character(
        name="甲一",
        role="主角",
        appearance_canonical="黑发少年，青色长衫，身形修长，目光坚定，腰系布带",
    )
    assert character_is_portrait_eligible(old) is True
    assert character_is_portrait_eligible({
        "name": "甲一",
        "appearance_canonical": "黑发少年",
    }) is True
    assert character_is_portrait_eligible({
        "name": "孟浩",
        "portrait_eligible": False,
        "appearance_status": "insufficient_evidence",
    }) is False
    assert character_is_portrait_eligible({
        "name": "王腾飞",
        "portrait_eligible": False,
        "appearance_status": "deferred",
    }) is False


def test_parse_character_detail_repairs_production_key_split_and_missing_key() -> None:
    split_raw = '''{
    "appearance_canonical": "眉眼清秀，气质坚韧，面有清苦感，眼底藏着对前路的思索，皮肤是常年读书的偏白质感，身形偏瘦", "
    :"period_costume_canonical", "身着青布书生直裰，棉麻面料，穿黑布皂靴，束发用木簪，禁用现代元素、绫罗绸缎等贵价面料",
    "personality": "聪颖坚韧",
    "speech_style": "谈吐直白坦诚，偶尔带自嘲，语气平实",
    "relationships": [],
    "aliases": [{"text": "孟才子", "name_kind": "美称", "evidence_chapter_index": null, "evidence_quote": ""}],
    "source_evidence": []
}'''
    missing_raw = '''{
    "appearance_canonical": "面容清俊，眉眼带着少年人的朝气，眼底藏着历经贫寒的沉静，肤色偏白净，身形挺拔，气质坚韧聪慧", "
    \t: "身着宗门制式青灰色交领短褐，粗棉面料，脚蹬黑布短靴，束发用木质发簪，禁用现代布料、金属拉链等元素",
    "personality": "聪颖坚韧",
    "speech_style": "语气平实，偶尔带自嘲，言辞恳切",
    "relationships": [],
    "aliases": [],
    "source_evidence": []
}'''
    split = stages._parse_character_detail_payload(split_raw)
    assert split["period_costume_canonical"].startswith("身着青布书生直裰")
    assert split["aliases"] == []
    stages._CharacterDetail.model_validate(split)

    missing = stages._parse_character_detail_payload(missing_raw)
    assert missing["period_costume_canonical"].startswith("身着宗门制式青灰色交领短褐")
    stages._CharacterDetail.model_validate(missing)


def test_sanitize_character_detail_drops_aliases_without_chapter_index() -> None:
    payload = {
        "appearance_canonical": "黑色短发，青色长衫，身形修长，腰系深色布带，脚穿布靴",
        "period_costume_canonical": "青布长衫布靴，束发挽髻，禁用现代面料拉链",
        "personality": "沉稳",
        "speech_style": "句式简短，语气平稳，少用修饰",
        "relationships": [],
        "aliases": [
            {
                "text": "孟才子",
                "name_kind": "honorific",
                "evidence_chapter_index": None,
                "evidence_quote": "孟才子救我",
            },
            {
                "text": "孟兄",
                "name_kind": "honorific",
                "evidence_chapter_index": 1,
                "evidence_quote": "孟兄来了",
            },
        ],
        "source_evidence": [
            {"evidence_chapter_index": None, "evidence_quote": "无效"},
            {"evidence_chapter_index": 1, "evidence_quote": "孟浩拔剑"},
        ],
    }
    cleaned = stages._sanitize_character_detail_payload(payload)
    assert [item["text"] for item in cleaned["aliases"]] == ["孟兄"]
    assert cleaned["source_evidence"] == [
        {"evidence_chapter_index": 1, "evidence_quote": "孟浩拔剑"},
    ]
    detail = stages._CharacterDetail.model_validate(cleaned)
    assert [item.text for item in detail.aliases] == ["孟兄"]


@pytest.mark.asyncio
async def test_generate_character_detail_keeps_character_when_alias_index_null(monkeypatch) -> None:
    async def fake_chat(messages, **_kwargs):
        return json.dumps({
            "appearance_canonical": "眉目清俊的少年书生模样，皮肤偏白，眼神藏着韧劲，身形偏瘦",
            "period_costume_canonical": "青布交领短褐粗棉面料，脚蹬粗布黑面布鞋，束发木簪",
            "personality": "聪颖坚韧",
            "speech_style": "语气平和带点少年人的自嘲，说话实在",
            "relationships": [],
            "aliases": [{
                "text": "孟才子",
                "name_kind": "honorific",
                "evidence_chapter_index": None,
                "evidence_quote": "孟才子救我",
            }],
            "source_evidence": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    result = await stages._generate_character_detail_batch(
        [stages._BibleRosterEntry(name="孟浩", role="主角")],
        [{"idx": 1, "content": "孟浩拔剑。"}],
        style="国漫三维动画电影质感，统一自然光影与细腻材质",
        chapters_by_idx={1: "孟浩拔剑。"},
        project_id="p1",
    )
    assert [item.name for item in result] == ["孟浩"]
    assert result[0].role == "主角"
    assert result[0].aliases == []


@pytest.mark.asyncio
async def test_generate_character_detail_batch_keeps_stub_when_detail_fails(monkeypatch) -> None:
    async def fake_chat(messages, **_kwargs):
        raise RuntimeError("forced detail failure")

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    result = await stages._generate_character_detail_batch(
        [stages._BibleRosterEntry(name="孟浩", role="主角")],
        [{"idx": 1, "content": "孟浩拔剑。"}],
        style="国漫三维动画电影质感，统一自然光影与细腻材质",
        chapters_by_idx={1: "孟浩拔剑。"},
        project_id="p1",
    )
    assert [item.name for item in result] == ["孟浩"]
    assert result[0].role == "主角"
    assert result[0].appearance_status == "insufficient_evidence"
    assert result[0].portrait_eligible is False


def test_bible_short_json_call_meta_keeps_explicit_first_token_timeout() -> None:
    meta = stages._bible_short_json_call_meta({
        "stage_key": "character_bible_detail",
        "first_token_timeout_s": stages.BIBLE_DETAIL_FIRST_TOKEN_TIMEOUT_S,
    })
    assert meta["first_token_timeout_s"] == stages.BIBLE_DETAIL_FIRST_TOKEN_TIMEOUT_S
    defaulted = stages._bible_short_json_call_meta({"stage_key": "character_roll_call"})
    assert defaulted["first_token_timeout_s"] == stages.BIBLE_FIRST_TOKEN_TIMEOUT_S
    # run_59d372954c0e：成功点名首字最慢 19.4s，20s 上限把仍在排队的流误杀。
    assert stages.BIBLE_FIRST_TOKEN_TIMEOUT_S == float(config.TIMEOUT_CHAT_FIRST_TOKEN_S)
    assert stages.BIBLE_FIRST_TOKEN_TIMEOUT_S >= 60.0


def test_bible_roll_call_chunk_failed_is_generation_not_sys() -> None:
    """ERR-20260827-a2f706：点名分块过多曾落到 SYS「服务器内部错误」。"""
    exc = stages._BibleRollCallChunkFailed("人物点名失败分块过多（8/20），名单不可信")
    assert errors.classify(exc) == ("generation", "GEN")
    assert "8/20" in str(exc)


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


@pytest.mark.asyncio
async def test_roll_call_disables_thinking(monkeypatch) -> None:
    metas: list[dict] = []

    async def fake_chat(messages, **kwargs):
        if (kwargs.get("call_meta") or {}).get("stage_key") != "character_roll_call":
            return kwargs["model_type"](verdict="onstage", supporting_segment_index=1)
        metas.append(kwargs.get("call_meta") or {})
        return json.dumps({"candidates": []})

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    await stages._recurring_character_names(
        [{"idx": 1, "title": "第1章", "content": "正文" * 100}]
    )
    assert metas
    assert all(item.get("disable_thinking") is True for item in metas)
    assert all(item.get("first_token_timeout_s") == stages.BIBLE_FIRST_TOKEN_TIMEOUT_S for item in metas)


@pytest.mark.asyncio
async def test_identity_resolution_runs_in_parallel_and_merges(monkeypatch) -> None:
    active = 0
    peak = 0
    metas: list[dict] = []

    async def fake_structured(_messages, **kwargs):
        nonlocal active, peak
        metas.append(kwargs.get("call_meta") or {})
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return stages._RosterIdentityResolution(
            verdict="same",
            canonical_appellation="孟浩",
            supporting_chapter_index=1,
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    chapter = "精明中年男子对孟浩点头。虎头虎脑少年跟着走。"
    ev = stages._RosterOnstageEvidence
    candidate = stages._RosterCandidate
    result = await stages._resolve_generic_character_candidates(
        [
            candidate(primary_appellation="孟浩", formal_name="孟浩", onstage_evidence=[
                ev(chapter_index=1, quote="精明中年男子对孟浩点头。"),
            ]),
            candidate(primary_appellation="精明中年男子", onstage_evidence=[
                ev(chapter_index=1, quote="精明中年男子对孟浩点头。"),
            ]),
            candidate(primary_appellation="虎头虎脑少年", onstage_evidence=[
                ev(chapter_index=1, quote="虎头虎脑少年跟着走。"),
            ]),
        ],
        {1: chapter},
        project_id="p1",
    )
    assert [item.primary_appellation for item in result] == ["孟浩"]
    assert peak > 1
    assert all(item.get("disable_thinking") is True for item in metas)
    aliases = set(result[0].aliases)
    assert "精明中年男子" in aliases
    assert "虎头虎脑少年" in aliases


def test_stable_nickname_is_not_a_generic_category() -> None:
    assert stages._is_generic_character_appellation("小胖子") is False
    assert stages._is_generic_character_appellation("胖子") is True
    assert stages._is_generic_character_appellation("精明中年男子") is True
    assert stages._is_generic_character_appellation("虎头虎脑少年") is True


def test_pin_roster_name_accepts_unique_one_char_source_variant() -> None:
    assert stages._pin_roster_name_to_source("王有材", ["王有材走过来。"]) == "王有材"
    assert stages._pin_roster_name_to_source("陆煊", ["陆烘冷冷看了他一眼。"]) == "陆烘"
    assert stages._pin_roster_name_to_source("铜镜灵", ["孟浩伸手拿起铜镜。"]) == ""


def test_dependent_descriptive_appellation_is_not_a_stable_identity() -> None:
    assert stages._is_dependent_descriptive_appellation("昨日孟浩的第一位客人", {"孟浩"}) is True
    assert stages._is_dependent_descriptive_appellation("赵武刚师兄", {"赵武刚"}) is False
    assert stages._is_dependent_descriptive_appellation("铜镜", {"孟浩"}) is False


@pytest.mark.asyncio
async def test_dependent_guest_description_is_not_kept_as_character(monkeypatch) -> None:
    async def fake_structured(*_args, **_kwargs):
        return stages._RosterIdentityResolution(
            verdict="uncertain", canonical_appellation="", supporting_chapter_index=-1,
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    ev = stages._RosterOnstageEvidence
    result = await stages._resolve_generic_character_candidates(
        [
            stages._RosterCandidate(primary_appellation="孟浩", formal_name="孟浩", onstage_evidence=[
                ev(chapter_index=13, quote="孟浩走进客栈。"),
            ]),
            stages._RosterCandidate(
                primary_appellation="昨日孟浩的第一位客人",
                onstage_evidence=[ev(chapter_index=13, quote="昨日孟浩的第一位客人坐在角落。")],
            ),
        ],
        {13: "孟浩走进客栈。昨日孟浩的第一位客人坐在角落。"},
        project_id="p1",
    )
    assert [item.primary_appellation for item in result] == ["孟浩"]


@pytest.mark.asyncio
async def test_personhood_gate_drops_treasure_and_keeps_person(monkeypatch) -> None:
    async def fake_structured(_messages, **kwargs):
        label = (kwargs.get("call_meta") or {}).get("character_name")
        if label == "铜镜":
            return stages._RosterPersonhoodResolution(
                verdict="non_person", supporting_chapter_index=4,
            )
        return stages._RosterPersonhoodResolution(verdict="person", supporting_chapter_index=1)

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    ev = stages._RosterOnstageEvidence
    kept = await stages._filter_non_person_roster_candidates(
        [
            stages._RosterCandidate(primary_appellation="孟浩", onstage_evidence=[
                ev(chapter_index=1, quote="孟浩走上前。"),
            ]),
            stages._RosterCandidate(primary_appellation="铜镜", onstage_evidence=[
                ev(chapter_index=4, quote="孟浩伸手拿起铜镜。"),
            ]),
        ],
        {1: "孟浩走上前。", 4: "孟浩伸手拿起铜镜。"},
        project_id="p1",
    )
    assert [item.primary_appellation for item in kept] == ["孟浩"]


@pytest.mark.asyncio
async def test_personhood_uncertain_keeps_named_character(monkeypatch) -> None:
    async def fake_structured(*_args, **_kwargs):
        return stages._RosterPersonhoodResolution(
            verdict="uncertain", supporting_chapter_index=-1,
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    ev = stages._RosterOnstageEvidence
    kept = await stages._filter_non_person_roster_candidates(
        [
            stages._RosterCandidate(primary_appellation="许师姐", onstage_evidence=[
                ev(chapter_index=1, quote="许师姐冷冷看了他一眼。"),
            ]),
            stages._RosterCandidate(
                primary_appellation="小胖子", formal_name="李富贵",
                onstage_evidence=[ev(chapter_index=2, quote="小胖子身子猛地哆嗦了一下。")],
            ),
        ],
        {
            1: "许师姐冷冷看了他一眼。",
            2: "小胖子身子猛地哆嗦了一下。",
        },
        project_id="p1",
    )
    assert [item.primary_appellation for item in kept] == ["许师姐", "小胖子"]
    assert {item.personhood for item in kept} == {"uncertain"}


@pytest.mark.asyncio
async def test_personhood_person_without_chapter_pin_still_keeps(monkeypatch) -> None:
    async def fake_structured(*_args, **_kwargs):
        return stages._RosterPersonhoodResolution(
            verdict="person", supporting_chapter_index=-1,
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    ev = stages._RosterOnstageEvidence
    kept = await stages._filter_non_person_roster_candidates(
        [stages._RosterCandidate(primary_appellation="许师姐", onstage_evidence=[
            ev(chapter_index=1, quote="许师姐冷冷看了他一眼。"),
        ])],
        {1: "许师姐冷冷看了他一眼。"},
        project_id="p1",
    )
    assert kept[0].personhood == "person"


@pytest.mark.asyncio
async def test_true_name_discovery_pins_later_chapter_reveal(monkeypatch) -> None:
    chapter_37 = "「许师姐。」孟浩抱拳一拜。这女子正是许清，如她的名字一样，冷冷清清。"

    async def fake_structured(*_args, **_kwargs):
        return stages._RosterTrueNameResolution(
            verdict="revealed",
            true_name="许清",
            supporting_chapter_index=37,
            supporting_quote="这女子正是许清，如她的名字一样，冷冷清清。",
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    result = await stages._discover_roster_true_names(
        [stages._RosterCandidate(primary_appellation="许师姐")],
        [
            {"idx": 1, "content": "许师姐冷冷看了他一眼。"},
            {"idx": 37, "content": chapter_37},
        ],
        project_id="p1",
    )
    assert result[0].formal_name == "许清"
    assert "许师姐" in result[0].aliases

    async def fake_invented(*_args, **_kwargs):
        return stages._RosterTrueNameResolution(
            verdict="revealed",
            true_name="王腾飞",
            supporting_chapter_index=37,
            supporting_quote="这女子正是许清，如她的名字一样，冷冷清清。",
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_invented)
    rejected = await stages._discover_roster_true_names(
        [stages._RosterCandidate(primary_appellation="许师姐")],
        [{"idx": 37, "content": chapter_37}],
        project_id="p1",
    )
    assert rejected[0].formal_name == ""


def test_bind_true_name_from_source_uses_identity_sentence() -> None:
    xu = stages._bind_true_name_from_source(
        [stages._RosterCandidate(primary_appellation="许师姐")],
        [{"idx": 37, "content": "「许师姐。」孟浩抱拳一拜。这女子正是许清，如她的名字一样，冷冷清清。"}],
    )
    assert xu[0].formal_name == "许清"
    assert "许师姐" in xu[0].aliases
    fatty = stages._bind_true_name_from_source(
        [stages._RosterCandidate(primary_appellation="小胖子")],
        [{"idx": 10, "content": "孟浩，你是我李富贵这一辈子的好朋友。”小胖子感慨连连。"}],
    )
    assert fatty[0].formal_name == "李富贵"
    self_id = stages._bind_true_name_from_source(
        [
            stages._RosterCandidate(primary_appellation="小胖子"),
            stages._RosterCandidate(primary_appellation="李富贵"),
        ],
        [{"idx": 10, "content": "孟浩，你是我李富贵这一辈子的好朋友。”小胖子感慨连连。"}],
    )
    assert self_id[0].formal_name == "李富贵"
    unbound = stages._bind_true_name_from_source(
        [stages._RosterCandidate(primary_appellation="铜镜")],
        [{"idx": 4, "content": "孟浩伸手拿起铜镜。便是靠山宗外那件宝物。"}],
    )
    assert unbound[0].formal_name == ""
    # 贪婪「正是+2~4字」会把「靠山宗外」「上官修身」当成真名；收紧后不得误绑。
    false_reveal = stages._bind_true_name_from_source(
        [stages._RosterCandidate(primary_appellation="许师姐")],
        [{"idx": 2, "content": "「许师姐。」这人正是靠山宗外门弟子。此人正是上官修身侧的随从。"}],
    )
    assert false_reveal[0].formal_name == ""
    # 「雕刻的正是王有材」出现在王伯场景：不得把已有另一候选的主名错绑过来。
    father_and_son = stages._bind_true_name_from_source(
        [
            stages._RosterCandidate(primary_appellation="王伯"),
            stages._RosterCandidate(primary_appellation="王有材"),
        ],
        [{"idx": 45, "content": "王伯坐在那里发呆，他的面前有一个木雕，雕刻的正是王有材，神色悲伤。"}],
    )
    assert father_and_son[0].formal_name == ""
    assert father_and_son[1].formal_name == ""
    # 「即便是」不得当成「便是」；台词里的「我李富贵」不得绑到旁边的孟浩。
    even_if = stages._bind_true_name_from_source(
        [stages._RosterCandidate(primary_appellation="小胖子")],
        [{"idx": 194, "content": "小胖子深吸口气，可即便是再谨慎，随着不断地前行。"}],
    )
    assert even_if[0].formal_name == ""
    trio = stages._bind_true_name_from_source(
        [
            stages._RosterCandidate(primary_appellation="孟浩"),
            stages._RosterCandidate(primary_appellation="小胖子"),
            stages._RosterCandidate(primary_appellation="许师姐"),
        ],
        [
            {"idx": 10, "content": "孟浩，你是我李富贵这一辈子的好朋友。”小胖子感慨连连。孟浩在不远处愣住。"},
            {"idx": 37, "content": "「许师姐。」孟浩抱拳一拜。这女子正是许清，如她的名字一样，冷冷清清。"},
        ],
    )
    by_name = {item.primary_appellation: item.formal_name for item in trio}
    assert by_name["孟浩"] == ""
    assert by_name["小胖子"] == "李富贵"
    assert by_name["许师姐"] == "许清"


def test_attach_roster_source_appellations_keeps_true_name_searchable() -> None:
    from app.schemas import Character

    character = Character(name="小胖子", role="重要配角", appearance_canonical="待测")
    entry = stages._BibleRosterEntry(
        name="小胖子", role="重要配角", source_appellations=["李富贵"],
    )
    stages._attach_roster_source_appellations(
        character, entry,
        [{"idx": 10, "content": "孟浩，你是我李富贵这一辈子的好朋友。”小胖子感慨连连。"}],
    )
    assert "李富贵" in {item.text for item in character.aliases}


def test_roster_personhood_dossier_keeps_segments_with_candidate_name() -> None:
    ev = stages._RosterOnstageEvidence
    dossier = stages._roster_personhood_dossier(
        stages._RosterCandidate(
            primary_appellation="孟浩",
            onstage_evidence=[ev(chapter_index=1, quote="孟兄，你也来了。")],
        ),
        {
            1: "孟兄，你也来了。孟浩走上前，冷冷看了一眼。",
        },
    )
    assert dossier
    assert all("孟浩" in item["text"] for item in dossier)


def test_roster_chapter_index_coerces_model_spellings() -> None:
    parsed = stages._RosterPersonhoodResolution.model_validate({
        "verdict": "person",
        "supporting_chapter_index": "第17章",
    })
    assert parsed.supporting_chapter_index == 17
    listed = stages._RosterPersonhoodResolution.model_validate({
        "verdict": "person",
        "supporting_chapter_index": [1, 5],
    })
    assert listed.supporting_chapter_index == 1


def test_uncertain_statistical_requires_agent_evidence() -> None:
    assert stages._appellation_used_as_agent("孟浩", [{"content": "孟浩走上前。"}])
    assert stages._appellation_used_as_agent("许师姐", [{"content": "许师姐冷冷看了他一眼。"}])
    assert not stages._appellation_used_as_agent(
        "铜镜", [{"content": "孟浩伸手拿起铜镜。铜镜中映出人影。"}],
    )
    # 贪婪「正是+2~4字」会把「靠山宗外」「上官修身」当成真名；收紧后不得误绑。
    false_reveal = stages._bind_true_name_from_source(
        [stages._RosterCandidate(primary_appellation="许师姐")],
        [{"idx": 2, "content": "「许师姐。」这人正是靠山宗外门弟子。此人正是上官修身侧的随从。"}],
    )
    assert false_reveal[0].formal_name == ""


@pytest.mark.asyncio
async def test_alias_verification_runs_per_character_in_parallel(monkeypatch) -> None:
    from app.schemas import Bible, Character, CharacterAlias, World

    active = 0
    peak = 0

    async def fake_resolution(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"accepted": False, "chapter_idx": None, "quote": "", "reason": "no"}

    monkeypatch.setattr(stages, "_alias_evidence_resolution", fake_resolution)
    appearance = "黑色短发，青色长衫，身形修长，腰系深色布带，脚穿布靴"
    bible = Bible(
        world=World(visual_style_canonical="国漫三维动画电影质感，统一自然光影与细腻材质"),
        characters=[
            Character(
                name="甲", role="主角", appearance_canonical=appearance,
                aliases=[CharacterAlias(
                    text="甲兄", name_kind="honorific",
                    evidence_chapter_index=1, evidence_quote="甲兄来了",
                )],
            ),
            Character(
                name="乙", role="重要配角", appearance_canonical=appearance,
                aliases=[CharacterAlias(
                    text="乙兄", name_kind="honorific",
                    evidence_chapter_index=1, evidence_quote="乙兄来了",
                )],
            ),
        ],
    )
    await stages._verify_character_aliases_for_subset(
        bible, bible.characters, {1: "甲兄来了。乙兄来了。"}, project_id="p1",
    )
    assert peak > 1
