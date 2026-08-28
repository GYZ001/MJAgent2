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


def _fake_verdict_chat_structured(
    selected_candidate: str,
    supporting_segment_index: int = 1,
    supporting_quote: str = "",
):
    """构造 model_gateway.chat_structured 的假实现，固定返回给定的裁决结论——测试里
    把"裁决闸"这一步钉死成确定性结果，不依赖真实模型调用。`model_type` 由调用方
    （`app.stages._alias_verdict_call`）在 kwargs 里传入，就是 `stages._AliasVerdictResponse`
    本身，用它构造返回值即可，测试文件不需要 import 这个私有类。

    `supporting_segment_index` 默认 1：本文件里绝大多数测试用的章节原文都是单一
    自然段（不含空行），`_alias_verdict_dossier` 对这种章节只会产出 segment_index=1
    这一条记录，默认值省得每个调用点都要重复写；需要构造"段号非法"场景的测试自己
    显式传一个卷宗里不存在的段号。"""

    async def fake(_messages, **kwargs):
        model_type = kwargs["model_type"]
        return model_type(
            selected_candidate=selected_candidate,
            supporting_segment_index=supporting_segment_index,
            supporting_quote=supporting_quote,
        )

    return fake


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


# ---------- 2a. 词形闸：切碎的短语残片不是称呼 ----------
#
# 真实事故（proj_195be7df1fd6，主角孟浩）：模型从原文「杂役处的师兄只让我们每天
# 每人十木」里截出「的师兄」登记成孟浩的别名——那句话是孟浩自己说的，说的是别人。
# 三条既有证据闸对它全都无能为力：引句逐字在原文里、「的师兄」确实是引句的子串、
# 该章也确实出现过「孟浩」。残片登记之后，下游按子串匹配用它（prep_pack 群演候选
# 集、认知卡在场判定），任何含「……的师兄」字样的章节都会把孟浩误拉进候选。

CHAPTERS_FRAGMENT = [
    {"idx": 3, "title": "第三章", "content": (
        "孟浩迟疑开口：可杂役处的师兄只让我们每天每人十木。"
    )},
]

QUOTE_FRAGMENT = "孟浩迟疑开口：可杂役处的师兄只让我们每天每人十木"


def test_alias_rejected_when_text_is_leading_particle_fragment() -> None:
    """以结构助词「的」起头的字符串是更长短语的残片，不指代任何人 → 拒绝登记。

    这条单独把词形闸隔离出来验证：三条证据闸在这份数据上全部满足（引句逐字在
    原文、别名是引句子串、本章出现过锚点「孟浩」），拒绝只能来自词形闸本身。
    """
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTERS_FRAGMENT)
    assert QUOTE_FRAGMENT in CHAPTERS_FRAGMENT[0]["content"]
    assert "的师兄" in QUOTE_FRAGMENT
    assert "孟浩" in CHAPTERS_FRAGMENT[0]["content"]
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"孟浩"}, "的师兄", 3, QUOTE_FRAGMENT,
    ) is False


def test_alias_accepted_when_text_merely_contains_particle() -> None:
    """词形闸只认起头那一个字：称呼内部含「的」不受影响。"""
    from app import stages

    chapters = [{"idx": 3, "title": "第三章", "content": (
        "众人都唤他做孟浩，背地里却叫他穷酸的书生。"
    )}]
    chapters_by_idx = stages._chapters_by_idx(chapters)
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"孟浩"}, "穷酸的书生", 3,
        "众人都唤他做孟浩，背地里却叫他穷酸的书生",
    ) is True


def test_alias_independence_gate_keeps_legitimate_particle_initial_names() -> None:
    """「地」「得」能合法起头（地煞老祖／得道真人），不得纳入词形闸误伤真称呼。"""
    from app import stages

    assert stages._alias_text_is_independent_appellation("地煞老祖") is True
    assert stages._alias_text_is_independent_appellation("得道真人") is True
    assert stages._alias_text_is_independent_appellation("的师兄") is False
    assert stages._alias_text_is_independent_appellation("  的胖子 ") is False
    assert stages._alias_text_is_independent_appellation("") is False


def test_roster_appellation_backfill_skips_phrase_fragment() -> None:
    """名单补录这条路径绕开了证据闸，词形闸必须在这里也拦住残片。

    真实事故里「的师兄」正是从这条路进的库：它只要求 80 字窗口内与角色名共现，
    拿 _alias_declaration_verified 回测该条目会得到 False，证明它从未过闸。
    """
    from app import stages
    from app.schemas import Character

    character = Character(name="孟浩", role="主角", appearance_canonical="书生模样")
    entry = stages._BibleRosterEntry(
        name="孟浩", role="主角", source_appellations=["的师兄", "孟才子"],
    )
    chapters = [{"idx": 3, "title": "第三章", "content": (
        "孟浩迟疑开口：可杂役处的师兄只让我们每天每人十木。孟才子救我，那少年喊道。"
    )}]

    stages._attach_roster_source_appellations(character, entry, chapters)

    assert [alias.text for alias in character.aliases] == ["孟才子"]


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


def test_bridge_chapter_found_when_model_cites_wrong_chapter(monkeypatch) -> None:
    """红灯先行场景：模型申报"许清→许清师妹"语义正确，但引用的第 20 章没有"许清"这个
    正式姓名，旧版逻辑（_alias_declaration_verified）必须拒绝——先确认这一步确实拒绝，
    再验证新入口 _alias_evidence_resolution 能在全书范围内找到真正的桥接章（第 34 章），
    过裁决闸（这里 mock 裁决结论为 same），并登记该章代码提取的引句（不是模型给的
    引句）。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(BRIDGE_CHAPTERS)
    model_quote = "许师姐的传说在坊间流传已久"

    # 红：旧核验函数在模型指错章时必须拒绝。
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "许清师妹", 20, model_quote,
    ) is False

    # 绿：新入口不直接拒绝，而是全书检索出第 34 章作为桥接章，再过裁决闸。
    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("许清"),
    )
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许清师妹", "许清", 20, model_quote,
        roster={"许清": ["许清"]},
    ))
    assert resolved["accepted"] is True
    assert resolved["chapter_idx"] == 34
    assert resolved["chapter_idx"] != 20  # 登记的不是模型指错的那一章
    assert resolved["quote"] != model_quote  # 登记的引句是代码从桥接章提取的，不是模型给的
    assert "许清师妹" in resolved["quote"]
    assert resolved["quote"] in chapters_by_idx[34]  # 引句必须是桥接章原文的逐字子串


def test_bridge_chapter_none_when_no_chapter_has_cooccurrence() -> None:
    """全书都没有"别名 + 正式姓名"共现的章节 → 维持拒绝（安全默认不放松）。裁决闸
    根本不会被触发（没有 mock model_gateway.chat_structured 也能通过，证明确实
    没有发起模型调用）。"""
    from app import stages

    chapters = [
        {"idx": 1, "title": "第一章", "content": "许清缓步走入大殿。"},  # 只有正式姓名
        {"idx": 2, "title": "第二章", "content": "许师姐的传说流传已久。"},  # 只有别名，永不共现
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)
    assert stages._find_alias_bridge_chapter(chapters_by_idx, {"许清"}, "许师姐") is None
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许师姐", "许清", 2, "许师姐的传说流传已久",
        roster={"许清": ["许清"]},
    ))
    assert resolved == {
        "accepted": False, "chapter_idx": None, "quote": "",
        "reason": "no_bridge_chapter",
    }


def test_bridge_chapter_none_when_alias_text_absent_from_every_chapter() -> None:
    """模型申报的别名文本压根不在任何章节原文里出现（比如模型记错了字）→ 全书检索
    也找不到任何桥接章，维持拒绝，同样不触发裁决闸。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(BRIDGE_CHAPTERS)
    assert stages._find_alias_bridge_chapter(
        chapters_by_idx, {"许清"}, "压根不存在的称呼ZZZZ",
    ) is None
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "压根不存在的称呼ZZZZ", "许清", 1, "随便一句引文",
        roster={"许清": ["许清"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "no_bridge_chapter"


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
    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("许清"),
    )
    added = asyncio.run(stages.backfill_character_aliases(bible, BRIDGE_CHAPTERS))

    assert added == {"许清": ["许清师妹"]}
    registered = bible.characters[0].aliases[0]
    assert registered.text == "许清师妹"
    assert registered.evidence_chapter_index == 34  # 代码检索出的真桥接章，不是模型申报的 20
    assert registered.evidence_quote != "许师姐的传说在坊间流传已久"  # 不是模型给的引句
    assert "许清师妹" in registered.evidence_quote
    assert registered.evidence_quote in stages._chapters_by_idx(BRIDGE_CHAPTERS)[34]


# ---------- 3. generate_bible 主链路后处理：_verify_character_aliases_in_place ----------

def test_verify_in_place_drops_unverified_and_keeps_verified(monkeypatch) -> None:
    from app import stages
    from app.harness import model_gateway

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

    # "编造的别名"在任何章节都不出现，桥接检索直接拒绝，不会走到裁决闸；
    # "银色长袍女子"直接通过声明核验，需要裁决闸放行才能最终登记。
    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("许清"),
    )
    added = asyncio.run(stages._verify_character_aliases_in_place(bible, CHAPTERS))

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
    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("许清"),
    )
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


# ---------- 5. 裁决闸：桥接章原文独立裁决（候选判别式） ----------
#
# 背景（真实误登记事故 1）：全书别名回填写库后核验发现"孟浩←虎爷爷"（第 3 章）被登记
# 进 bible——第 3 章原文里"虎爷爷"明确是欺负孟浩的另一个"魁梧大汉"，根本不是孟浩
# 本人。旧三闸（_alias_declaration_verified 的逐字命中 + 别名在引句里 + 章节内共现）
# 只能证明"同章出现"，证明不了"指同一人"：孟浩作为主角在第 3 章出现 59 次，几乎
# 和任何称谓都能满足共现条件。下面用真实引句复刻这个案例的形状，先证明"修前"（只用
# 旧三闸）会通过，再验证新入口 `_alias_evidence_resolution` 补上裁决闸后必须拒绝。
#
# 背景（真实误登记事故 2）：人工抽查又发现"王腾飞←王师弟"（第 189 章）被裁决闸放行，
# 而"王师弟"实际指的是同章另一个人（王有材）——王腾飞只是同章反复出现、恰好同姓的
# 敌对角色。根因是裁决问法本身是一道是非题（"称谓 X 是不是人名 Y 本人"），模型看到
# 卷宗里反复出现的名字就倾向点头，属于确认偏误，与具体是哪个姓氏无关。修复：裁决从
# "确认单一假设"改造成"从候选集中判别"——候选集是该章结构性命中的全部人物谱角色
# （`_alias_verdict_candidates`，零语义、不针对任何具体人名/姓氏特判）外加一个显式的
# "都不是/无法确定"选项，只有模型选中的候选恰好是本次申报的 true_name 才登记。
#
# 两条真实事故合起来说明裁决闸不针对任何具体称谓做特判，只是让模型看着桥接章真实
# 原文、在真实候选之间重新独立判别一次"称谓指代的到底是谁"。


def test_alias_verdict_dossier_includes_segments_with_anchor_texts_only() -> None:
    """真实回归：对生产项目跑复核时发现，桥接章里角色规范名（anchor_texts）经常单独
    出现在跟别名（text）不相邻的段落——只搜别名会把这些段落漏掉，模型只能被迫回答
    不确定（本该能判断的证据没有被喂给它）。卷宗检索必须同时搜别名和 anchor_texts，
    两者共现的段落优先，只含其中一个的段落也要收录进卷宗。"""
    from app import stages

    chapter_text = (
        "期间他没有看到闭关多月的许师姐，但却看到了那位穿着银袍的陈凡师兄。\n\n"
        "不知道许师姐什么时候出关，孟浩心想。\n\n"
        "小师弟不用害羞，许清师妹天生丽质，你偷偷喜欢也是正常，陈凡师兄微笑说道。\n\n"
        "许清？咳咳，没有没有，孟浩赶紧开口，连忙转开话题。"
    )
    dossier = stages._alias_verdict_dossier(34, chapter_text, "许师姐", {"许清"})
    catalog_texts = [item["text"] for item in dossier]
    assert any("许师姐" in text for text in catalog_texts)
    # 只含 anchor_texts（不含别名本身）的段落必须也被收录，否则模型看不到角色规范名
    # 单独出现的位置，无法判断两者是否指同一人。
    assert any("许清？" in text for text in catalog_texts)


def test_alias_verdict_dossier_reserves_anchor_only_quota_when_text_is_high_frequency() -> None:
    """红→绿（缺陷 B，"主角淹没预算"第四次复发，移植 prep_pack.py 0395a73/1f15844
    验证过两轮的按层保底配额修复，见 `_alias_verdict_dossier` docstring"第二个真实
    回归"一节）。

    构造 `text`（本测试模拟状态事实场景里高频出现的归属对象/关系对象，例如宗门名
    这类"结构上与主角名同样近乎每段都出现"的词）本身是章内高频词的卷宗：5 个自然段
    只含 `text`（"血妖宗"）、不含 `anchor_texts`（"孟浩"），每段 1160 字——
    `index_source_segments` 默认单段上限 900 字，会把每个自然段再切成 880+280 两块，
    合计 10 个 text_only 段落、总字数 5800；额外 1 段只含"孟浩"、不含"血妖宗"的
    anchor_only 段落（280 字）。

    旧实现"both 全部收录 → text_only 全部收录 → anchor_only 补足剩余"下可手工验证：
    10 个 text_only 段落按顺序累加（第一条不受字数预算约束这条历史豁免）恰好用掉
    5800 字，轮到 anchor_only 那 280 字段落时 5800+280=6080 超过 MAX_CHARS(6000)，
    被 continue 跳过——anchor_only 一条都进不了卷宗，候选方（"孟浩"）在这一章
    唯一的证据段落完全消失，模型只能在残缺材料上判断。

    新实现必须保证 anchor_only 层拿到不受字数预算挤占的保底名额：即使前面的
    text_only 段落已经把大半字数预算用掉，含"孟浩"的段落仍必须出现在卷宗里
    （可能被截断，但不能被整条排除）。

    变异验证：把 `_alias_verdict_dossier` 里的按层保底配额改回旧的
    `ordered_candidates = priority_indexes + anchor_only_by_proximity` 单一
    优先级列表（不分保底/flex），本测试会变红（anchor_only 段落从卷宗中消失）。
    """
    from app import stages

    filler = "血妖宗近来又生变故，弟子们议论纷纷，都说此事牵连甚广，恐怕还要闹出更大的风波来。"
    text_only_paragraph = filler * 29  # 1160 字，超过单段 900 字上限会被再切成两块
    anchor_only_paragraph = "远处传来孟浩低声说话的声音，" * 20  # 280 字，只含孟浩

    chapter_text = "\n\n".join([text_only_paragraph] * 5) + "\n\n" + anchor_only_paragraph

    # 前提校验：确保测试数据本身真的构造出了"anchor_only 唯一那段会被旧算法的字数
    # 预算挤掉"这个场景，不是数据没配对。
    segments = stages.index_source_segments(chapter_text)
    text_only_segments = [s for s in segments if "血妖宗" in s.text and "孟浩" not in s.text]
    anchor_only_segments = [s for s in segments if "孟浩" in s.text and "血妖宗" not in s.text]
    assert len(text_only_segments) >= 5, "前提：text_only 段落数量应足够多"
    assert len(anchor_only_segments) == 1, "前提：只有一段 anchor_only 证据"
    text_only_total_chars = sum(len(s.text) for s in text_only_segments)
    assert (
        text_only_total_chars + len(anchor_only_segments[0].text)
        > stages._ALIAS_VERDICT_DOSSIER_MAX_CHARS
    ), "前提：text_only 段落合计字数加上 anchor_only 那一段必须超过总字数预算"

    dossier = stages._alias_verdict_dossier(7, chapter_text, "血妖宗", {"孟浩"})

    assert any("孟浩" in item["text"] for item in dossier), (
        "anchor_only 层（候选方唯一的证据段落）必须拿到保底名额，不能被 text_only "
        "的字数预算挤掉"
    )
    assert len(dossier) <= stages._ALIAS_VERDICT_DOSSIER_MAX_ENTRIES
    assert sum(len(item["text"]) for item in dossier) <= stages._ALIAS_VERDICT_DOSSIER_MAX_CHARS


CHAPTER_3_LIKE = [
    {"idx": 3, "title": "第三章", "content": (
        "睡的挺早啊，都给你虎爷爷起来！随着那两扇房门的呼扇，从外面走进一个穿着"
        "杂役衫的魁梧大汉，他凶狠的看了孟浩与还在睡觉的小胖子一眼。"
    )},
]


def test_old_three_gate_alone_would_have_accepted_the_false_alias() -> None:
    """"修前"：只用旧三闸（不含裁决闸）判断"孟浩←虎爷爷"，逐字命中 + 别名在引句里 +
    章节内共现三个条件全部满足，会被判定通过——这正是已发生的真实误登记的根因，
    证明裁决闸补的是真实漏洞，不是臆造的假想敌。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(CHAPTER_3_LIKE)
    quote = "都给你虎爷爷起来"
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"孟浩"}, "虎爷爷", 3, quote,
    ) is True


def test_bridging_verdict_rejects_false_alias_when_selected_candidate_differs(
    monkeypatch,
) -> None:
    """"修后"：裁决闸看到桥接章真实原文后，从候选集（孟浩、大汉——"大汉"是该章原文
    里"魁梧大汉"这个真正称谓对象的结构性候选占位）里选中的不是本次申报的孟浩，即便
    旧三闸会放行，新入口也必须拒绝——这就是已确认错误的"孟浩←虎爷爷"必须被拒才算
    闸门有效的那条红灯用例。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(CHAPTER_3_LIKE)
    quote = "都给你虎爷爷起来"
    roster = {"孟浩": ["孟浩"], "大汉": ["大汉"]}

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("大汉"),
    )
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "虎爷爷", "孟浩", 3, quote,
        roster=roster,
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "candidate_mismatch"


def test_bridging_verdict_rejects_false_alias_when_uncertain(monkeypatch) -> None:
    """裁决结论"都不是/无法确定"（原文不足以确定，即使候选集里只有孟浩一个真实人物谱
    角色）同样必须拒绝——不确定不登记的安全默认，不是只拒绝"确定选了别人"这一种
    情况。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(CHAPTER_3_LIKE)
    quote = "都给你虎爷爷起来"
    roster = {"孟浩": ["孟浩"]}

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured(stages._ALIAS_VERDICT_NO_MATCH_LABEL),
    )
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "虎爷爷", "孟浩", 3, quote,
        roster=roster,
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "candidate_uncertain"


# 真实误登记事故 2 的原样复刻（第 189 章，人工抽查发现，见本节顶部大注释）：
# "王师弟"这句台词是李诗琪替另一个血妖宗弟子王有材求情（王有材当章已经站到孟浩
# 一边）；王腾飞是同章与孟浩敌对、正瞪着孟浩的另一个人，二者只是同姓，"王腾飞"在该
# 章反复出现、"王师弟"只出现一次且紧挨着王腾飞的戏份——这正是诱发"是非题式"裁决
# 确认偏误的真实条件。
WANG_TENGFEI_CHAPTER = [
    {"idx": 189, "title": "第一百八十九章", "content": (
        "血妖宗那里，王有材默默的站起身，一语不发，但却站在了孟浩的身后。\n\n"
        "王腾飞身子向前迈出一步，一脸杀气，眼中杀机毕露，更是双眼露出寒芒，"
        "死死的盯着孟浩。\n\n"
        "“虽然你那顶帽子很让人厌烦，但看在王师弟的份上，我血妖宗也算一个，"
        "倒要看看今日，谁敢动你。”李诗琪冷声开口。"
    )},
]


def test_bridging_verdict_rejects_when_selected_candidate_is_a_different_registered_character(
    monkeypatch,
) -> None:
    """真实误登记事故 2 复刻：申报"王腾飞←王师弟"实际是错的——候选判别式裁决必须把
    该章出场的其它人物谱角色（王有材、李诗琪）也摆上候选台面，一旦模型选中候选集里
    的其他人（这里选王有材，不是本次申报的王腾飞），必须拒绝，
    reason=candidate_mismatch。代码本身不对"王腾飞""王师弟"等具体姓氏/称谓做任何
    特判——候选集完全靠结构性扫描 roster（人物谱规范名/已登记别名的逐字子串命中）
    算出，换成任何其它同章出场、同姓或不同姓的角色组合都是同一套判断路径。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(WANG_TENGFEI_CHAPTER)
    quote = "但看在王师弟的份上"
    roster = {
        "孟浩": ["孟浩"], "王有材": ["王有材"],
        "李诗琪": ["李诗琪"], "王腾飞": ["王腾飞"],
    }

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("王有材"),
    )
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"王腾飞"}, "王师弟", "王腾飞", 189, quote,
        roster=roster,
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "candidate_mismatch"


XUQING_XUSHIJIE_CHAPTERS = [
    {"idx": 1, "title": "第一章", "content": "许清缓步走入大殿，无人知晓她的来历。"},
    {"idx": 34, "title": "第三十四章", "content": (
        "许师姐缓步走来，众人纷纷起身行礼，原来她正是许清。"
    )},
]


def test_bridging_verdict_accepts_true_alias_with_real_shape_positive_case(
    monkeypatch,
) -> None:
    """正例：许清←许师姐（第 34 章）——桥接章真实存在"许师姐"与"许清"同段出现，
    裁决闸看到原文后独立选中候选"许清"（即本次申报的 true_name），最终应当登记成功，
    证明裁决闸不会连正确的别名也一并拒绝。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(XUQING_XUSHIJIE_CHAPTERS)
    roster = {"许清": ["许清"]}

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("许清"),
    )
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许师姐", "许清", 34,
        "许师姐缓步走来，众人纷纷起身行礼，原来她正是许清",
        roster=roster,
    ))
    assert resolved["accepted"] is True
    assert resolved["chapter_idx"] == 34
    assert resolved["quote"] in chapters_by_idx[34]


def test_bridging_verdict_rejects_when_selected_segment_index_out_of_dossier(
    monkeypatch,
) -> None:
    """红灯：候选选对了（same/许清），但模型给出的 supporting_segment_index 不在
    本次卷宗实际收录的段号集合内（这里卷宗只有 segment_index=1 一条，模型却给了
    99）——钉证是结构性判断，段号越界必须拒绝，不能因为候选选对了就放行。这也是
    schema 层 enum 之外，代码侧必须再做一次结构校验的原因：provider 对 enum 的遵守
    不是可证明保证（见 `_alias_verdict_call` 里对 output_schema 的注释）。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(XUQING_XUSHIJIE_CHAPTERS)
    roster = {"许清": ["许清"]}

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("许清", supporting_segment_index=99),
    )
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许师姐", "许清", 34,
        "许师姐缓步走来，众人纷纷起身行礼，原来她正是许清",
        roster=roster,
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "segment_not_pinned"


def test_bridging_verdict_accepts_regardless_of_supporting_quote_transcription_noise(
    monkeypatch,
) -> None:
    """绿灯：真实回归——钉证曾经要求 supporting_quote 逐字命中卷宗，"李富贵←小胖子"
    （原文"小胖子、王有材、还有那虎头虎脑的少年，当初我们四人被一起带上靠山宗"）与
    "上官修←上官师叔"两条本该通过的正确别名，分别因模型转录跨段拼接/加省略号/微调
    标点被误杀，或同一输入两次给出不同结果。钉证已改为选段号（结构性核验，见
    `_alias_verdict_pin_segment`），supporting_quote 退化为纯观测字段——模型给出任何
    转录噪音甚至编造的引句都不应该影响登记结果，只要 selected_candidate 与
    supporting_segment_index 合法。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(XUQING_XUSHIJIE_CHAPTERS)
    roster = {"许清": ["许清"]}
    noisy_quote = "……编造的、卷宗原文里根本没有的一句转录噪音……"

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("许清", supporting_quote=noisy_quote),
    )
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许师姐", "许清", 34,
        "许师姐缓步走来，众人纷纷起身行礼，原来她正是许清",
        roster=roster,
    ))
    assert resolved["accepted"] is True
    assert resolved["reason"] == ""


def test_bridging_verdict_rejects_when_candidate_roster_is_empty(monkeypatch) -> None:
    """防御性分支：候选集为空（正常不应触发，见 `_alias_evidence_resolution` docstring
    ——true_name 或其已登记别名命中该章是走到这一步的前提，必然会被
    `_alias_verdict_candidates` 收进候选集）也必须拒绝，不能因为拿不到候选列表就跳过
    判别直接放行。不 mock chat_structured 也能通过，证明这一分支在发起裁决调用之前
    就已经拒绝。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(XUQING_XUSHIJIE_CHAPTERS)
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许师姐", "许清", 34,
        "许师姐缓步走来，众人纷纷起身行礼，原来她正是许清",
        roster={},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "no_verdict_candidates"


def test_pin_segment_returns_matching_dossier_record_when_index_is_valid() -> None:
    """钉证结构性核验：段号命中卷宗集合内某条记录即通过，返回该条记录（自带
    chapter_idx，供调用方记账）。"""
    from app import stages

    dossier = [
        {"chapter_idx": 5, "segment_index": 2, "text": "无关段落。"},
        {"chapter_idx": 5, "segment_index": 3, "text": "老夫上官修，今日放丹。"},
    ]
    pinned = stages._alias_verdict_pin_segment(dossier, 3)
    assert pinned is not None
    assert pinned["segment_index"] == 3
    assert pinned["text"] == "老夫上官修，今日放丹。"


def test_pin_segment_rejects_index_outside_dossier() -> None:
    """段号不在卷宗集合内——模型选错/瞎编——必须拒绝，不确定不登记的安全默认。"""
    from app import stages

    dossier = [{"chapter_idx": 5, "segment_index": 2, "text": "无关段落。"}]
    assert stages._alias_verdict_pin_segment(dossier, 99) is None


def test_pin_segment_rejects_non_integer_index() -> None:
    """代码侧对非法类型的防御：不信任 provider 一定严格遵守 schema 声明的 int 类型
    （enum/type 约束不是可证明保证，见 `_alias_verdict_call` 对 output_schema 的
    注释），非整数输入一律视为无效裁决而非报错崩溃。"""
    from app import stages

    dossier = [{"chapter_idx": 5, "segment_index": 2, "text": "无关段落。"}]
    assert stages._alias_verdict_pin_segment(dossier, "not-a-number") is None
    assert stages._alias_verdict_pin_segment(dossier, None) is None


def test_bridging_verdict_rejects_when_verdict_call_raises(monkeypatch) -> None:
    """裁决调用本身失败（网络/鉴权/供应商故障）按不确定处理——不能因为拿不到裁决
    结果就放行，同样是不确定不登记。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(XUQING_XUSHIJIE_CHAPTERS)

    async def failing_chat_structured(_messages, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(model_gateway, "chat_structured", failing_chat_structured)
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"许清"}, "许师姐", "许清", 34,
        "许师姐缓步走来，众人纷纷起身行礼，原来她正是许清",
        roster={"许清": ["许清"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "verdict_call_failed"


# ---------- 5b. 候选集与候选面快照：_alias_verdict_candidates / _alias_verdict_roster ----------

def test_alias_verdict_candidates_collects_all_roster_names_present_in_chapter() -> None:
    """结构判据：候选集就是该章原文里逐字命中的全部 roster 规范名，与命中次数、出现
    顺序无关，只看是否出现过；未出现的角色不进候选集。"""
    from app import stages

    chapter_text = "孟浩与王有材、李诗琪三人一同前往，路上偶遇王腾飞。"
    roster = {
        "孟浩": ["孟浩"], "王有材": ["王有材"], "李诗琪": ["李诗琪"],
        "王腾飞": ["王腾飞"], "许清": ["许清"],
    }
    candidates = stages._alias_verdict_candidates(chapter_text, roster)
    assert candidates == ["孟浩", "王有材", "李诗琪", "王腾飞"]  # 许清没出现，不进候选集


def test_alias_verdict_candidates_matches_via_confirmed_alias_not_only_canonical_name() -> None:
    """候选命中不局限于角色规范名本身，已登记别名命中也算这个角色在场——与
    `_alias_verdict_dossier` 的 anchor_texts 检索范围保持同一原则（候选集不能比
    共现闸更窄）。"""
    from app import stages

    chapter_text = "胖爷冷笑一声，转身离去。"
    roster = {"李富贵": ["李富贵", "小胖子", "胖爷"]}
    assert stages._alias_verdict_candidates(chapter_text, roster) == ["李富贵"]


def test_alias_verdict_roster_snapshots_names_and_confirmed_aliases() -> None:
    """`_alias_verdict_roster` 把 bible.characters 的规范名与已登记别名投影成
    候选面快照，键是规范名，值以规范名本身打头。"""
    from app import stages

    character = Character(
        name="李富贵", role="配角", appearance_canonical=APPEARANCE,
        personality="", speech_style="",
        aliases=[CharacterAlias(
            text="小胖子", name_kind="referential",
            evidence_chapter_index=1, evidence_quote="小胖子",
        )],
    )
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))
    roster = stages._alias_verdict_roster(bible)
    assert roster == {"李富贵": ["李富贵", "小胖子"]}


# ---------- 6. reverify_character_aliases：历史别名批次复核 ----------

def test_reverify_removes_alias_rejected_by_verdict_gate(monkeypatch) -> None:
    """复现"补闸前已落库"的真实场景：孟浩的 aliases 里混进了一条已通过旧三闸、
    但过不了新裁决闸的"虎爷爷"——reverify 必须把它从 bible 中移除并原地写回。这个
    bible 里只登记了"孟浩"一个角色，候选集里只有他自己，模型选"都不是/无法确定"
    即可复现"确实不是这个人"的拒绝结论（reason=candidate_uncertain）。"""
    from app import stages
    from app.harness import model_gateway

    character = Character(
        name="孟浩", role="主角",
        appearance_canonical=APPEARANCE,
        personality="坚韧",
        speech_style="直接",
        aliases=[CharacterAlias(
            text="虎爷爷", name_kind="personal_name",
            evidence_chapter_index=3, evidence_quote="都给你虎爷爷起来",
        )],
    )
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured(stages._ALIAS_VERDICT_NO_MATCH_LABEL),
    )
    report = asyncio.run(
        stages.reverify_character_aliases(bible, CHAPTER_3_LIKE)
    )

    assert report == {
        "孟浩": [{"text": "虎爷爷", "kept": False, "reason": "candidate_uncertain"}],
    }
    assert bible.characters[0].aliases == []


def test_reverify_keeps_alias_that_passes_verdict_gate(monkeypatch) -> None:
    """正确登记的别名重新过闸应当保留（幂等），reason 为空字符串。"""
    from app import stages
    from app.harness import model_gateway

    character = _character(aliases=[CharacterAlias(
        text="许师姐", name_kind="honorific",
        evidence_chapter_index=34,
        evidence_quote="许师姐缓步走来，众人纷纷起身行礼，原来她正是许清",
    )])
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("许清"),
    )
    report = asyncio.run(
        stages.reverify_character_aliases(bible, XUQING_XUSHIJIE_CHAPTERS)
    )

    assert report == {
        "许清": [{"text": "许师姐", "kept": True, "reason": ""}],
    }
    assert [a.text for a in bible.characters[0].aliases] == ["许师姐"]


def test_reverify_skips_characters_without_any_alias() -> None:
    """没有别名的角色不出现在复核报告里，也不会触发任何模型调用（不 mock
    chat_structured 也能通过，证明没有发起裁决）。"""
    from app import stages

    character = _character()
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    report = asyncio.run(stages.reverify_character_aliases(bible, CHAPTERS))

    assert report == {}
