"""人物谱别名位（层一）：schemas.CharacterAlias 结构 + stages.py 窄口径核验/回填。
对应 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1 / §6 P0 第 1、2 项。

核验原则贯穿全文件：模型申报 + 代码核验，不确定不登记；判据只看结构
（逐字子串命中 + 章节内共现），不针对任何具体称谓做特判。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.schemas import Bible, Character, CharacterAlias, World


APPEARANCE = "二十岁女子，墨发高马尾，银色素面长袍，身形清瘦，背后一柄银色长剑，眉目清冷"


def _character(**overrides: object) -> Character:
    base = dict(
        name="许清",
        role="重要配角",
        appearance_canonical=APPEARANCE,
        personality="外冷内热，寡言少语",
        speech_style="话少句短，语气平淡",
    )
    base.update(overrides)
    return Character(**base)


# ---------- 1. CharacterAlias 结构 + Character.aliases 字段 ----------

def test_character_alias_requires_all_evidence_fields() -> None:
    alias = CharacterAlias(
        text="银色长袍女子",
        name_kind="referential",
        evidence_chapter_index=13,
        evidence_quote="这位便是许清，也就是当年那位银色长袍女子",
    )
    assert alias.text == "银色长袍女子"
    assert alias.name_kind == "referential"
    assert alias.evidence_chapter_index == 13


def test_character_alias_missing_field_rejected() -> None:
    with pytest.raises(Exception):
        CharacterAlias(text="银色长袍女子", name_kind="referential")


def test_character_aliases_default_empty() -> None:
    character = _character()
    assert character.aliases == []


def test_character_aliases_round_trip_through_bible_json() -> None:
    character = _character(aliases=[
        CharacterAlias(
            text="银色长袍女子", name_kind="referential",
            evidence_chapter_index=13,
            evidence_quote="这位便是许清，也就是当年那位银色长袍女子",
        ),
    ])
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))
    dumped = json.loads(bible.model_dump_json())
    restored = Bible.model_validate(dumped)
    assert restored.characters[0].aliases[0].text == "银色长袍女子"


def test_old_bible_json_without_aliases_field_loads_compatibly() -> None:
    """既有 bible_json（新增字段前落库的数据）没有 aliases 键，反序列化不得失败。"""
    legacy_bible_json = {
        "characters": [{
            "name": "许清",
            "role": "重要配角",
            "appearance_canonical": APPEARANCE,
            "personality": "外冷内热",
            "speech_style": "话少句短",
            "relationships": [],
        }],
        "world": {"visual_style_canonical": "国漫3D动画电影质感，精致光影"},
    }
    bible = Bible.model_validate(legacy_bible_json)
    assert bible.characters[0].aliases == []
    assert bible.characters[0].name == "许清"


# ---------- 2. app/stages.py 代码核验：_alias_declaration_verified ----------

CHAPTERS = [
    {"idx": 1, "title": "第一章", "content": (
        "银色长袍女子缓步走入大殿，无人知晓她的姓名，众人只当她是寻常弟子。"
    )},
    {"idx": 7, "title": "第七章", "content": (
        "银色长袍女子的传说在坊间流传已久，无人知晓来历，只当是过路修士。"
    )},
    {"idx": 13, "title": "第十三章", "content": (
        "掌门朗声道：这位便是许清，也就是当年那位银色长袍女子。"
    )},
    {"idx": 20, "title": "第二十章", "content": (
        "许清转身离去，衣袂翻飞，再未提起银袍旧事。"
    )},
]

QUOTE_13 = "这位便是许清，也就是当年那位银色长袍女子"


def test_alias_verified_when_quote_hits_and_cooccurs() -> None:
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS)
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "银色长袍女子", 13, QUOTE_13,
    ) is True


def test_alias_rejected_when_quote_not_verbatim_in_chapter() -> None:
    """引句被模型改写/近似复述（不是原文逐字子串）→ 拒绝登记。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS)
    paraphrased = "这位就是许清，正是当年那银袍女子"  # 与原文不完全一致
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "银色长袍女子", 13, paraphrased,
    ) is False


def test_alias_rejected_when_evidence_chapter_index_wrong() -> None:
    """引句真实存在，但申报的章节序号是错的——第 20 章里有角色规范名（共现条件本身满足），
    但没有这句引句，仍必须拒绝登记（隔离验证「逐字命中」这一条件本身）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS)
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "银色长袍女子", 20, QUOTE_13,
    ) is False


def test_alias_rejected_when_no_cooccurrence_in_chapter() -> None:
    """引句逐字命中，但该章节里找不到角色规范名或已确认别名 → 拒绝登记（张冠李戴防护）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS)
    quote_7 = "银色长袍女子的传说在坊间流传已久"
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "银色长袍女子", 7, quote_7,
    ) is False


def test_alias_rejected_when_alias_text_not_in_quote() -> None:
    """申报的别名文本本身不在引句里 → 证据文不对题，拒绝登记。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS)
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "不存在的别名", 13, QUOTE_13,
    ) is False


# ---------- 3. generate_bible 主链路后处理：_verify_character_aliases_in_place ----------

def test_verify_in_place_drops_unverified_and_keeps_verified() -> None:
    character = _character(aliases=[
        CharacterAlias(
            text="银色长袍女子", name_kind="referential",
            evidence_chapter_index=13, evidence_quote=QUOTE_13,
        ),
        CharacterAlias(
            text="编造的别名", name_kind="referential",
            evidence_chapter_index=13, evidence_quote="编造的、原文里根本没有的一句话",
        ),
    ])
    snapshot = character.model_dump(exclude={"aliases"})
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    from app import stages
    added = stages._verify_character_aliases_in_place(bible, CHAPTERS)

    assert added == {"许清": ["银色长袍女子"]}
    kept = bible.characters[0].aliases
    assert [a.text for a in kept] == ["银色长袍女子"]
    # 冻结其它一切既有字段：核验只允许改写 aliases。
    assert bible.characters[0].model_dump(exclude={"aliases"}) == snapshot


# ---------- 4. backfill_character_aliases：全书上下文一次性回填 ----------

def test_backfill_registers_verified_alias_and_freezes_other_fields(monkeypatch) -> None:
    from app import stages
    from app.harness import model_gateway

    character = _character()
    snapshot = character.model_dump(exclude={"aliases"})
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({"aliases": [
            {
                "character_name": "许清", "text": "银色长袍女子", "name_kind": "referential",
                "evidence_chapter_index": 13, "evidence_quote": QUOTE_13,
            },
            {  # 引句是编造的，不该被登记
                "character_name": "许清", "text": "冒牌别名", "name_kind": "referential",
                "evidence_chapter_index": 13, "evidence_quote": "编造的、原文里没有的引句",
            },
            {  # 角色列表里没有这个人，应被忽略
                "character_name": "不存在的角色", "text": "路人", "name_kind": "referential",
                "evidence_chapter_index": 1, "evidence_quote": "路人甲",
            },
        ]}, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    added = asyncio.run(stages.backfill_character_aliases(bible, CHAPTERS))

    assert added == {"许清": ["银色长袍女子"]}
    assert [a.text for a in bible.characters[0].aliases] == ["银色长袍女子"]
    assert bible.characters[0].model_dump(exclude={"aliases"}) == snapshot


def test_backfill_is_idempotent_on_rerun(monkeypatch) -> None:
    """同一条别名重复申报（比如重跑回填）不应重复追加。"""
    from app import stages
    from app.harness import model_gateway

    character = _character(aliases=[CharacterAlias(
        text="银色长袍女子", name_kind="referential",
        evidence_chapter_index=13, evidence_quote=QUOTE_13,
    )])
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({"aliases": [
            {
                "character_name": "许清", "text": "银色长袍女子", "name_kind": "referential",
                "evidence_chapter_index": 13, "evidence_quote": QUOTE_13,
            },
        ]}, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    added = asyncio.run(stages.backfill_character_aliases(bible, CHAPTERS))

    assert added == {}
    assert len(bible.characters[0].aliases) == 1


def test_backfill_returns_empty_and_keeps_bible_on_model_failure(monkeypatch) -> None:
    from app import stages
    from app.harness import model_gateway

    character = _character()
    snapshot = character.model_dump()
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    async def failing_chat(_messages, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(model_gateway, "chat", failing_chat)
    added = asyncio.run(stages.backfill_character_aliases(bible, CHAPTERS))

    assert added == {}
    assert bible.characters[0].model_dump() == snapshot
