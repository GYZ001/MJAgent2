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
    split = stages._merge_roll_call_candidates([
        [candidate(primary_appellation="小胖子", aliases=["胖子", "李富贵"])],
        [candidate(primary_appellation="王有材", aliases=["胖子", "有材大哥"])],
    ])
    assert {item.primary_appellation for item in split} == {"小胖子", "王有材"}
    same_formal = stages._merge_roll_call_candidates([
        [candidate(primary_appellation="小胖子", formal_name="李富贵")],
        [candidate(primary_appellation="王有材", formal_name="李富贵")],
    ])
    assert {item.primary_appellation for item in same_formal} == {"小胖子", "王有材"}


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
async def test_mentioned_only_character_still_gets_detail_and_portrait(monkeypatch) -> None:
    async def fake_chat(*_args, **_kwargs):
        return json.dumps({
            "appearance_canonical": "白发苍颜，面容威严，身形高瘦，目光如炬，气场强横",
            "period_costume_canonical": "着玄色绣金广袖道袍，云纹玉冠束发，脚踏云纹道靴，禁用现代元素",
            "personality": "霸道",
            "speech_style": "语气专断，少有商量余地",
            "relationships": [], "aliases": [], "source_evidence": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    entry = stages._BibleRosterEntry(
        name="靠山老祖",
        role="关键伏笔角色",
        presence_status="mentioned_only",
        importance_score=18.4,
        importance_signals=["fulltext_mentions:3", "retained_by_plot_authority"],
        portrait_eligible=True,
        appearance_status="grounded",
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
    assert character.appearance_status == "grounded"
    assert character.portrait_eligible is True
    assert "白发苍颜" in character.appearance_canonical
    assert not character.appearance_canonical.startswith("女性")
    assert not character.appearance_canonical.startswith("男性")


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
        "portrait_eligible": True,
        "appearance_status": "grounded",
        "presence_status": "mentioned_only",
    }) is True


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
    assert normalized.characters[1].appearance_status == "grounded"
    assert normalized.characters[1].portrait_eligible is True


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


def test_formal_name_cannot_replace_more_common_appellation() -> None:
    chapters = [{
        "idx": 1,
        "content": (
            "靠山老祖即白主，负手而立。"
            + "靠山老祖负手而立。" * 20
            + "白主冷冷开口。" * 5
            + "「许师姐。」这女子正是许清。" * 8
        ),
    }]
    kaoshan, demoted = stages._pick_canonical_display_name("靠山老祖", "白主", chapters)
    assert kaoshan == "靠山老祖"
    assert demoted == ["白主"]

    xu, xu_demoted = stages._pick_canonical_display_name("许师姐", "许清", chapters)
    assert xu == "许清"
    assert xu_demoted == ["许师姐"]


@pytest.mark.asyncio
async def test_claimed_formal_name_is_dropped_when_model_cannot_confirm(monkeypatch) -> None:
    """点名顺手填的真名也要复核；复核不过就退回称呼，程序不靠字符黑名单。"""
    chapters = [{
        "idx": 1,
        "content": "王腾飞踏入阵法。这阵法忽然发光。王腾飞看了看这阵法。",
    }]

    async def fake_unrevealed(*_args, **_kwargs):
        return stages._RosterTrueNameResolution(
            verdict="unrevealed", true_name="", supporting_chapter_index=-1,
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_unrevealed)
    resolved = await stages._discover_roster_true_names(
        [stages._RosterCandidate(primary_appellation="王腾飞", formal_name="这阵法")],
        chapters,
        project_id="p1",
    )
    assert resolved[0].primary_appellation == "王腾飞"
    assert resolved[0].formal_name == ""


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
    # name_form 由上游资格裁决里的模型给出，这里模拟它已经判完的状态。
    result = await stages._resolve_generic_character_candidates(
        [
            candidate(
                primary_appellation="孟浩", formal_name="孟浩",
                name_form="personal_name",
                onstage_evidence=[ev(chapter_index=1, quote="精明中年男子对孟浩点头。")],
            ),
            candidate(
                primary_appellation="精明中年男子", name_form="referential",
                onstage_evidence=[ev(chapter_index=1, quote="精明中年男子对孟浩点头。")],
            ),
            candidate(
                primary_appellation="虎头虎脑少年", name_form="referential",
                onstage_evidence=[ev(chapter_index=1, quote="虎头虎脑少年跟着走。")],
            ),
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


def test_ambiguous_appellations_come_from_the_data_not_a_word_list() -> None:
    """分不出人的称呼由本次点名结果推导：被多个候选共用才算，不查词表。"""
    candidates = [
        stages._RosterCandidate(primary_appellation="李富贵", aliases=["胖子", "小胖子"]),
        stages._RosterCandidate(primary_appellation="王有材", aliases=["胖子"]),
        stages._RosterCandidate(primary_appellation="孟浩"),
    ]
    ambiguous = stages._shared_appellations(candidates)
    assert "胖子" in ambiguous
    assert "小胖子" not in ambiguous
    assert "孟浩" not in ambiguous
    # 同一个绰号只属于一个人时不算歧义，哪怕它长得像类别词。
    solo = stages._shared_appellations([
        stages._RosterCandidate(primary_appellation="少年", aliases=["那少年"]),
    ])
    assert solo == set()


def test_identity_resolution_routing_follows_model_name_form() -> None:
    """送不送身份归一由模型判的称呼形态决定，程序不认识任何具体词。"""
    candidate = stages._RosterCandidate

    referential = candidate(primary_appellation="青衫少年", name_form="referential")
    assert stages._roster_label_needs_identity_resolution(referential, set(), set())

    named = candidate(primary_appellation="青衫少年", name_form="personal_name")
    assert not stages._roster_label_needs_identity_resolution(named, set(), set())

    honorific = candidate(primary_appellation="许师姐", name_form="honorific")
    assert not stages._roster_label_needs_identity_resolution(honorific, set(), set())

    shared = candidate(primary_appellation="胖子", name_form="personal_name")
    assert stages._roster_label_needs_identity_resolution(shared, set(), {"胖子"})


def test_spread_named_segments_covers_first_hit_and_later_chapters() -> None:
    """证据检索跨全书取样：首次出现的章一定在，后文揭示身份的章也进得来。"""
    chapters = {index: "无关内容。" for index in range(1, 60)}
    chapters[34] = "许清第一次出现在这里。"
    chapters[37] = "这女子正是许清，如她的名字一样，冷冷清清。"
    chapters[50] = "许清收剑而立。"
    blocks = stages._spread_named_segments(["许清"], chapters, limit=3)
    picked = {item["chapter_idx"] for item in blocks}
    assert 34 in picked
    assert picked == {34, 37, 50}


def test_true_name_dossier_batches_cover_a_chapter_single_sampling_skips() -> None:
    """揭示只在一章时，单批均匀取样会跳过它；分批交错必须把它铺进来。"""
    chapters = {index: f"许师姐第{index}次出现。" for index in range(1, 69)}
    chapters[38] = "「许师姐。」孟浩抱拳一拜。这女子正是许清，如她的名字一样。"
    single = stages._spread_named_segments(["许师姐"], chapters, limit=12)
    assert 38 not in {item["chapter_idx"] for item in single}

    batches = stages._roster_true_name_dossier_batches(["许师姐"], chapters)
    covered = {item["chapter_idx"] for batch in batches for item in batch}
    assert 38 in covered
    # 各批互不重叠，合起来才是更大的跨度覆盖。
    seen: set[int] = set()
    for batch in batches:
        current = {item["chapter_idx"] for item in batch}
        assert not (current & seen)
        seen |= current


@pytest.mark.asyncio
async def test_true_name_pins_against_any_confirmed_appellation(monkeypatch) -> None:
    """揭示章原文只写了别名时，钉证要认这个别名，不能硬要求主名逐字出现。"""
    reveal = "他有个师弟叫李富贵，你放心。胖子当时就在旁边。"

    async def fake_structured(*_args, **_kwargs):
        return stages._RosterTrueNameResolution(
            verdict="revealed", true_name="李富贵",
            supporting_chapter_index=1070, supporting_quote="他有个师弟叫李富贵",
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    resolved = await stages._discover_roster_true_names(
        [stages._RosterCandidate(primary_appellation="小胖子", aliases=["胖子"])],
        [{"idx": 1070, "content": reveal}],
        project_id="p1",
    )
    assert resolved[0].formal_name == "李富贵"
    assert "小胖子" in resolved[0].aliases


@pytest.mark.asyncio
async def test_true_name_tries_next_batch_when_one_batch_fails(monkeypatch) -> None:
    """一批卷宗答不出不能判死这个人，还有别的批要读。"""
    chapters = [{"idx": index, "content": f"许师姐第{index}次出现。"} for index in range(1, 69)]
    chapters[37] = {"idx": 38, "content": "「许师姐。」这女子正是许清，如她的名字一样。"}
    calls = 0

    async def fake_structured(messages, **_kwargs):
        nonlocal calls
        calls += 1
        prompt = messages[-1]["content"]
        if "许清" not in prompt:
            raise ValueError("坏 JSON")
        return stages._RosterTrueNameResolution(
            verdict="revealed", true_name="许清",
            supporting_chapter_index=38, supporting_quote="这女子正是许清",
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    resolved = await stages._discover_roster_true_names(
        [stages._RosterCandidate(primary_appellation="许师姐")], chapters, project_id="p1",
    )
    assert resolved[0].formal_name == "许清"
    assert "许师姐" in resolved[0].aliases
    assert calls > 1


def test_pin_roster_name_accepts_unique_one_char_source_variant() -> None:
    assert stages._pin_roster_name_to_source("王有材", ["王有材走过来。"]) == "王有材"
    assert stages._pin_roster_name_to_source("陆煊", ["陆烘冷冷看了他一眼。"]) == "陆烘"
    assert stages._pin_roster_name_to_source("铜镜灵", ["孟浩伸手拿起铜镜。"]) == ""


def test_composite_appellation_is_detected_by_grammar_not_by_word_list() -> None:
    """带属格的组合指称说的是关系，不是身份；判据是「的」这个结构，不是词表。"""
    assert stages._is_composite_appellation("昨日孟浩的第一位客人", {"孟浩"}) is True
    assert stages._is_composite_appellation("赵武刚师兄", {"赵武刚"}) is False
    assert stages._is_composite_appellation("铜镜", {"孟浩"}) is False
    assert stages._is_composite_appellation("此人的对手", set()) is True
    assert stages._is_composite_appellation("某人的客人", set()) is True
    assert stages._is_composite_appellation("靠山老祖", set()) is False


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
async def test_demonstrative_description_is_not_kept_as_character(monkeypatch) -> None:
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
                primary_appellation="此人的对手",
                onstage_evidence=[ev(chapter_index=13, quote="此人的对手站在角落。")],
            ),
        ],
        {13: "孟浩走进客栈。此人的对手站在角落。"},
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


def test_conflicting_formal_names_fail_closed_unless_owner_is_present() -> None:
    """同一真名撞车时只有主名就是它的候选能留住；都不是就两边都退回称呼。"""
    resolved = stages._resolve_conflicting_formal_names([
        stages._RosterCandidate(primary_appellation="小胖子", formal_name="李富贵"),
        stages._RosterCandidate(primary_appellation="王有材", formal_name="李富贵"),
    ])
    assert [item.formal_name for item in resolved] == ["", ""]

    with_owner = stages._resolve_conflicting_formal_names([
        stages._RosterCandidate(primary_appellation="李富贵", formal_name="李富贵"),
        stages._RosterCandidate(primary_appellation="王有材", formal_name="李富贵"),
    ])
    by_name = {item.primary_appellation: item.formal_name for item in with_owner}
    assert by_name["李富贵"] == "李富贵"
    assert by_name["王有材"] == ""


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


def test_attach_roster_source_appellations_uses_existing_aliases_as_anchors() -> None:
    from app.schemas import Character, CharacterAlias

    character = Character(
        name="白主",
        role="重要配角",
        appearance_canonical="待测",
        aliases=[CharacterAlias(
            text="师尊",
            name_kind="honorific",
            evidence_chapter_index=1,
            evidence_quote="靠山老祖被弟子称为师尊。",
        )],
    )
    entry = stages._BibleRosterEntry(
        name="白主", role="重要配角", source_appellations=["靠山老祖"],
    )
    stages._attach_roster_source_appellations(
        character, entry,
        [{"idx": 1, "content": "靠山老祖被弟子称为师尊。" + ("无关。" * 40) + "白主冷冷开口。"}],
    )
    assert "靠山老祖" in {item.text for item in character.aliases}


def test_model_declared_alias_does_not_get_the_roster_free_pass() -> None:
    """点名模型顺口报的别名不走免检通道——共现闸挡不住"两拨人出现在同一句"。

    真实故障 ERR-20260828-9fcabe（《罗刹海市》EP1）：点名把「大夫」报成主角马骥的
    别名。共现闸在「那些士绅大夫争着想开开眼界，便叫村民邀请马骥前去」这一句里
    同时看到两个词就放行了——可这句话里大夫是发出邀请的那拨人，马骥是被邀请的那
    一个，恰恰不是同一个人。「大夫」就此成为马骥的登记称谓，进了
    reserved_authority_labels；映射台随后正确地把本集朝堂上的众大夫判成
    functional，撞上「current functional 不得冒用已登记身份称谓：大夫」，整集失败
    且重试必然复现。

    这条免检通道本身是对的，前提是「这个称呼是名单赖以成立的身份标识」——候选能
    进必收名单靠的就是它，在场证据已经逐条过了结构闸、裁决闸和段号钉证。模型
    随手申报的 aliases 没有这层保证，只能走详情侧那条正规闸。
    """
    from app.schemas import Character

    chapters = [{
        "idx": 1,
        "content": "那些士绅大夫争着想开开眼界，便叫村民邀请马骥前去。",
    }]

    character = Character(name="马骥", role="主角", appearance_canonical="待测")
    stages._attach_roster_source_appellations(
        character,
        stages._BibleRosterEntry(
            name="马骥", role="主角",
            source_appellations=["大夫"], unverified_appellations=["大夫"],
        ),
        chapters,
    )
    assert "大夫" not in {item.text for item in character.aliases}, (
        "未经核验的申报别名不该零核验入谱"
    )

    # 同一句原文、同一个词，只是这次它被标成「名单赖以成立的身份标识」：免检通道
    # 照旧放行。这既是修复前行为的独立观察点，也守住这条通道没被整个关掉。
    identified = Character(name="马骥", role="主角", appearance_canonical="待测")
    stages._attach_roster_source_appellations(
        identified,
        stages._BibleRosterEntry(
            name="马骥", role="主角", source_appellations=["大夫"],
        ),
        chapters,
    )
    assert "大夫" in {item.text for item in identified.aliases}


def test_roster_marks_model_aliases_unverified_but_not_identity_names() -> None:
    """名单归一化要分清哪些称呼是名单的身份标识，哪些只是模型申报的别名。

    身份标识（primary_appellation / formal_name / 被降级的那个显示名）是这个候选
    进必收名单所依据的东西，名单成立就意味着它们成立；candidate.aliases 一路没被
    核对过指的是不是同一个人。检索用途照旧吃全集，只有「登记进人物谱 aliases」
    这一步必须把两者分开。
    """
    draft = stages._normalize_roster_against_candidates(
        stages._BibleRosterDraft(
            characters=[],
            world={"visual_style_canonical": "国漫三维动画电影质感，统一自然光影与细腻材质"},
        ),
        [("马骥", "马龙媒", 3, 20, 4, ["大夫", "俊人"])],
    )

    entry = draft.characters[0]
    assert entry.name == "马龙媒"
    assert set(entry.source_appellations) == {"马骥", "大夫", "俊人"}, (
        "检索键照旧收全，别名的检索用途没有被这次改动收窄"
    )
    assert set(entry.unverified_appellations) == {"大夫", "俊人"}
    assert "马骥" not in entry.unverified_appellations, (
        "primary_appellation 是这个候选进名单的身份标识，不是待核验的申报"
    )


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


def test_roster_card_is_portrait_eligible_even_when_only_mentioned() -> None:
    draft = stages._BibleRosterDraft(
        characters=[],
        world={"visual_style_canonical": "国漫三维动画电影质感，统一自然光影与细腻材质"},
    )
    chapters = [{"idx": i, "content": "王腾飞未出场，只被弟子提起。"} for i in range(1, 1617)]
    normalized = stages._normalize_roster_against_candidates(draft, [
        ("王腾飞", "", 0, 65, 30, []),
        ("靠山老祖", "", 0, 4, 3, []),
    ], chapters)
    by_name = {entry.name: entry for entry in normalized.characters}
    assert by_name["王腾飞"].portrait_eligible is True
    assert by_name["王腾飞"].appearance_status == "grounded"
    assert by_name["靠山老祖"].portrait_eligible is True
    assert by_name["靠山老祖"].appearance_status == "grounded"

