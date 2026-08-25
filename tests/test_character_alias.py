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


# ---------- 2b. 引号规范化：修复 A（回填 dry-run 12 条只过 0 条的真因） ----------
#
# 真实 dry-run 复现的 bug：模型申报 evidence_quote 时自己套了一层 ASCII 直引号
# `"…"`，而原文该处用的是全角引号 `“…”`（或者原文根本没有引号）；逐字子串比对因为
# 多出/换了一层引号字符而判定"不在原文中"，本来完全正确的引句被误拒。

CHAPTERS_FULLWIDTH_QUOTE = [
    {"idx": 3, "title": "第三章", "content": (
        f"掌门朗声道：“{QUOTE_13}”"
    )},
]


def test_alias_verified_after_stripping_ascii_quotes_model_added_around_fullwidth_original() -> None:
    """模型给引句套了一层 ASCII 直引号，原文实际用全角引号包裹同一句话——
    脱掉模型这一层引号后内文仍是原文逐字子串，应判定通过（dry-run 复现场景）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS_FULLWIDTH_QUOTE)
    ascii_wrapped_quote = '"' + QUOTE_13 + '"'
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "银色长袍女子", 3, ascii_wrapped_quote,
    ) is True


def test_alias_verified_after_stripping_fullwidth_quotes_model_added_around_plain_original() -> None:
    """反向场景：原文本身没有任何引号，模型却自己加了一层全角引号——同样应该脱一层后
    命中，证明全角引号也被覆盖，不止 ASCII 一种。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS)  # 第 13 章原文没有引号包裹
    fullwidth_wrapped_quote = "“" + QUOTE_13 + "”"
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "银色长袍女子", 13, fullwidth_wrapped_quote,
    ) is True


def test_alias_rejected_when_quote_has_unpaired_leading_quote_mark() -> None:
    """引句只有开头多了一个引号字符，没有配对的收尾——不能被当成"整体套了一层引号"脱掉，
    否则会把原文本身的一部分误当引号裁掉，让本不该通过的证据蒙混过关；正确实现仍需拒绝。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS)
    unpaired_quote = '"' + QUOTE_13  # 只有开头引号，没有配对的结尾引号
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "银色长袍女子", 13, unpaired_quote,
    ) is False


def test_alias_rejected_when_fabricated_quote_wrapped_in_quotes() -> None:
    """引号规范化只脱格式层的引号，编造的引句套一层引号也不能蒙混过关——共现闸不能因为
    引号规范化被间接放松。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS_FULLWIDTH_QUOTE)
    fabricated_quote = '"编造的、原文里根本没有的一句话"'
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "银色长袍女子", 3, fabricated_quote,
    ) is False


def test_quote_comparison_variants_strips_paired_ascii_quotes() -> None:
    from app import stages

    assert stages._quote_comparison_variants('"内容"') == ['"内容"', "内容"]


def test_quote_comparison_variants_strips_paired_fullwidth_quotes() -> None:
    from app import stages

    assert stages._quote_comparison_variants("“内容”") == ["“内容”", "内容"]


def test_quote_comparison_variants_keeps_unpaired_quote_untouched() -> None:
    """成对才脱，不成对不动：只有一侧引号、或首尾引号字符不属于同一对，都原样保留。"""
    from app import stages

    assert stages._quote_comparison_variants('"内容') == ['"内容']
    assert stages._quote_comparison_variants('内容"') == ['内容"']
    assert stages._quote_comparison_variants('"内容”') == ['"内容”']  # 首尾不是同一对


# ---------- 2c. 桥接章确定性检索：_find_alias_bridge_chapter / _alias_evidence_resolution ----------
#
# 真实回归：模型申报的语义假设（character_name+text）是对的，但 evidence_chapter_index
# 报错了章——它引用的章节里没有角色正式姓名，旧版共现闸直接拒绝。这里验证新分工：
# 模型申报的章节没通过共现闸时，代码退一步在全书范围内确定性检索"同时含别名文本与
# 角色正式姓名（或已确认别名）"的桥接章，命中就用代码从该章逐字截取的引句登记；
# 全书都没有这样的章节，或别名文本压根不在任何章节出现，仍然维持拒绝。

BRIDGE_CHAPTERS = [
    {"idx": 1, "title": "第一章", "content": (
        "许清缓步走入大殿，无人知晓她的来历，众人只当她是寻常弟子。"  # 有正式姓名，无别名
    )},
    {"idx": 20, "title": "第二十章", "content": (
        "许师姐的传说在坊间流传已久，众人只当是过路修士。"  # 只有别名，无正式姓名（模型误引此章）
    )},
    {"idx": 34, "title": "第三十四章", "content": (
        "许清师妹快步跑来，众人纷纷侧目，原来她正是许清，昔日的银袍女子。"  # 真正的桥接章：两者共现
    )},
]


def test_bridge_chapter_found_when_model_cites_wrong_chapter() -> None:
    """红灯先行场景：模型申报"许清→许清师妹"语义正确，但引用的第 20 章没有"许清"这个
    正式姓名，旧版逻辑（_alias_declaration_verified）必须拒绝——先确认这一步确实拒绝，
    再验证新入口 _alias_evidence_resolution 能在全书范围内找到真正的桥接章（第 34 章）
    并登记该章代码提取的引句（不是模型给的引句）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(BRIDGE_CHAPTERS)
    model_quote = "许师姐的传说在坊间流传已久"

    # 红：旧核验函数在模型指错章时必须拒绝。
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "许清师妹", 20, model_quote,
    ) is False

    # 绿：新入口不直接拒绝，而是全书检索出第 34 章作为桥接章。
    resolved = stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许清师妹", 20, model_quote,
    )
    assert resolved is not None
    resolved_chapter_index, resolved_quote = resolved
    assert resolved_chapter_index == 34
    assert resolved_chapter_index != 20  # 登记的不是模型指错的那一章
    assert resolved_quote != model_quote  # 登记的引句是代码从桥接章提取的，不是模型给的
    assert "许清师妹" in resolved_quote
    assert resolved_quote in chapters_by_idx[34]  # 引句必须是桥接章原文的逐字子串


def test_bridge_chapter_none_when_no_chapter_has_cooccurrence() -> None:
    """全书都没有"别名 + 正式姓名"共现的章节 → 维持拒绝（安全默认不放松）。"""
    from app import stages

    chapters = [
        {"idx": 1, "title": "第一章", "content": "许清缓步走入大殿。"},  # 只有正式姓名
        {"idx": 2, "title": "第二章", "content": "许师姐的传说流传已久。"},  # 只有别名，永不共现
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)
    assert stages._find_alias_bridge_chapter(chapters_by_idx, {"许清"}, "许师姐") is None
    assert stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许师姐", 2, "许师姐的传说流传已久",
    ) is None


def test_bridge_chapter_none_when_alias_text_absent_from_every_chapter() -> None:
    """模型申报的别名文本压根不在任何章节原文里出现（比如模型记错了字）→ 全书检索
    也找不到任何桥接章，维持拒绝。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(BRIDGE_CHAPTERS)
    assert stages._find_alias_bridge_chapter(
        chapters_by_idx, {"许清"}, "压根不存在的称呼ZZZZ",
    ) is None
    assert stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "压根不存在的称呼ZZZZ", 1, "随便一句引文",
    ) is None


def test_bridge_chapter_picks_earliest_when_multiple_hits() -> None:
    """多个桥接章都能满足共现时，按章节序号升序取最早出现的那一章（确定性规则，
    避免同一输入不同次运行选到不同的桥接章）。用"银色长袍女子"而非"许清师妹"是刻意
    选择——"许清师妹"本身以"许清"为前缀，单独出现就会让共现闸trivially 通过，无法
    干净地区分"只满足一半"与"两者都满足"这两种章节，"银色长袍女子"与"许清"互不
    包含，才能构造出真正意义上的多候选桥接章场景。"""
    from app import stages

    chapters = [
        {"idx": 5, "title": "第五章", "content": "银色长袍女子的传说流传已久。"},  # 只有别名
        {"idx": 9, "title": "第九章", "content": "许清缓步走入大殿。"},  # 只有正式姓名
        {"idx": 40, "title": "第四十章", "content": "银色长袍女子缓缓走来，她正是许清。"},  # 首个双满足
        {"idx": 55, "title": "第五十五章", "content": "许清身披银色长袍女子的旧衣，感慨万分。"},  # 也双满足，但更晚
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)
    resolved = stages._find_alias_bridge_chapter(chapters_by_idx, {"许清"}, "银色长袍女子")
    assert resolved is not None
    resolved_chapter_index, _resolved_quote = resolved
    assert resolved_chapter_index == 40  # 第 5、9 章各只满足一半，第 40 章才是第一个双满足的


def test_bridge_chapter_quote_is_verbatim_and_bounded() -> None:
    """代码提取的引句必须是章节原文逐字子串、包含别名文本本身，且有长度上限（不整段
    照搬全文）——覆盖引句提取规则本身的确定性与可复现性。"""
    from app import stages

    long_content = "无关内容。" * 80 + "许清师妹缓缓抬头。" + "又是无关内容。" * 80 + "她正是许清。"
    chapters_by_idx = {1: long_content}
    resolved = stages._find_alias_bridge_chapter(chapters_by_idx, {"许清"}, "许清师妹")
    assert resolved is not None
    _resolved_chapter_index, quote = resolved
    assert quote in long_content
    assert "许清师妹" in quote
    assert len(quote) <= stages._ALIAS_BRIDGE_QUOTE_MAX_CHARS


def test_backfill_registers_bridge_chapter_not_models_declared_chapter(monkeypatch) -> None:
    """集成场景：backfill_character_aliases 端到端验证——模型申报的章节没通过共现闸，
    最终登记进 bible 的 evidence_chapter_index/evidence_quote 必须是代码检索出的桥接章
    与代码提取的引句，不是模型申报的那一份。"""
    from app import stages
    from app.harness import model_gateway

    character = _character()
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({"aliases": [
            {
                "character_name": "许清", "text": "许清师妹", "name_kind": "honorific",
                "evidence_chapter_index": 20,  # 模型指错的章节（该章没有"许清"这个正式姓名）
                "evidence_quote": "许师姐的传说在坊间流传已久",
            },
        ]}, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    added = asyncio.run(stages.backfill_character_aliases(bible, BRIDGE_CHAPTERS))

    assert added == {"许清": ["许清师妹"]}
    registered = bible.characters[0].aliases[0]
    assert registered.text == "许清师妹"
    assert registered.evidence_chapter_index == 34  # 代码检索出的真桥接章，不是模型申报的 20
    assert registered.evidence_quote != "许师姐的传说在坊间流传已久"  # 不是模型给的引句
    assert "许清师妹" in registered.evidence_quote
    assert registered.evidence_quote in stages._chapters_by_idx(BRIDGE_CHAPTERS)[34]


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


def test_backfill_prompt_explains_bridging_chapter_requirement(monkeypatch) -> None:
    """修复 B：提示词必须讲清楚 evidence_chapter_index 要指向别名与正式姓名（或已确认别名）
    共现的那一章，不是该别名第一次出现的章节——这是全书回填 dry-run 12 条只过 0 条的
    第二个真因：模型倾向于引用别名首次出现的章节，那一章往往还没揭晓真名，共现闸必然
    拒绝。也必须讲清楚不要自己给引句加引号包裹（修复 A 对应的另一半：源头上少犯错）。"""
    from app import stages
    from app.harness import model_gateway

    character = _character()
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    seen: dict[str, object] = {}

    async def fake_chat(messages, **_kwargs):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps({"aliases": []}, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    asyncio.run(stages.backfill_character_aliases(bible, CHAPTERS))

    prompt = str(seen["prompt"])
    assert "不是该别名第一次出现" in prompt
    assert "同时出现" in prompt
    assert "不要自己在引句前后加引号包裹" in prompt


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
