"""人物认知层 · 章级认知卡（层 B）+ 裁决闸提示词注入（层 C）：
对应 docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.2 / §4.3 / §9 P0 第 3、4 项。

覆盖范围：
1. `stages.build_chapter_cognition_card` 的确定性组装规则——三类事实的时间语义
   （§3）：身份事实（别名）恒真不受章节号 N 影响；状态事实（归属/关系）按"截至第 N
   章"过滤有效区间，区间重叠时同一对象只取最近生效的一条；条数/字数上限的确定性
   截断。
2. `stages._alias_verdict_call` 接入认知卡后的提示词：认知卡为空时提示词与注入前
   逐字一致；认知卡非空时含明确分区标注与"不得仅凭认知卡下结论"的措辞，且原有的
   卷宗/候选/任务指令段落不变。
3. 端到端回归（`_alias_evidence_resolution` 传入 `bible`）：真实误登记事故 2
   （王腾飞←王师弟，第 189 章）接入认知卡后必须继续被拒绝——认知卡只辅助区分候选，
   不放松既有裁决闸门。
"""
from __future__ import annotations

import asyncio

from app.schemas import (
    Bible, Character, CharacterAffiliation, CharacterAlias, CharacterRelation, World,
)

APPEARANCE = "二十岁女子，墨发高马尾，银色素面长袍，身形清瘦，背后一柄银色长剑，眉目清冷"
WORLD = World(visual_style_canonical="国漫3D动画电影质感，精致光影")


def _character(**overrides: object) -> Character:
    base = dict(
        name="孟浩",
        role="主角",
        appearance_canonical=APPEARANCE,
        personality="坚韧，重情义",
        speech_style="直接，偶尔冷幽默",
    )
    base.update(overrides)
    return Character(**base)


def _capturing_chat_structured(
    selected_candidate: str,
    supporting_segment_index: int = 1,
    supporting_quote: str = "",
):
    """与既有别名/状态事实测试同款假实现，额外把发出的 prompt 正文记到 captured 里，
    供断言提示词分区/措辞。"""
    captured: dict[str, str] = {}

    async def fake(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        model_type = kwargs["model_type"]
        return model_type(
            selected_candidate=selected_candidate,
            supporting_segment_index=supporting_segment_index,
            supporting_quote=supporting_quote,
            is_exclusive_reference=True,
        )

    return fake, captured


# ---------- 1. build_chapter_cognition_card：状态事实按 N 过滤 ----------

def test_card_includes_affiliation_when_interval_covers_target_chapter() -> None:
    from app import stages

    character = _character(
        name="王腾飞",
        affiliations=[CharacterAffiliation(
            org="靠山宗", relation_kind="membership",
            evidence_chapter_index=5, evidence_quote="王腾飞拜入靠山宗",
            valid_from_chapter=5, valid_to_chapter=100,
        )],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [
        {"idx": 5, "content": "王腾飞加入靠山宗"},
        {"idx": 50, "content": "王腾飞依旧在场"},
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 50)
    entry = card.present_characters[0]
    assert entry.affiliations_as_of == ["靠山宗（membership），第5章证据"]


def test_card_excludes_affiliation_when_interval_does_not_cover_target_chapter() -> None:
    """红灯核心用例：valid_to_chapter=100 的归属，在第 150 章查询时区间已不覆盖，
    不得出现在认知卡里——避免用后期状态事实描写早期，也避免用早期区间描写后期。"""
    from app import stages

    character = _character(
        name="王腾飞",
        affiliations=[CharacterAffiliation(
            org="靠山宗", relation_kind="membership",
            evidence_chapter_index=5, evidence_quote="王腾飞拜入靠山宗",
            valid_from_chapter=5, valid_to_chapter=100,
        )],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [
        {"idx": 5, "content": "王腾飞加入靠山宗"},
        {"idx": 150, "content": "王腾飞依旧在场"},
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 150)
    entry = card.present_characters[0]
    assert entry.affiliations_as_of == []


def test_card_excludes_affiliation_before_valid_from_chapter() -> None:
    """区间起点之前同样不该出现（不是只测终点）。"""
    from app import stages

    character = _character(
        name="王有材",
        affiliations=[CharacterAffiliation(
            org="血妖宗", relation_kind="弟子",
            evidence_chapter_index=50, evidence_quote="王有材拜入血妖宗",
            valid_from_chapter=50,
        )],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [
        {"idx": 3, "content": "王有材尚未拜入任何门派，独自游历"},
        {"idx": 50, "content": "王有材正式拜入血妖宗"},
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 3)
    entry = card.present_characters[0]
    assert entry.affiliations_as_of == []


def test_card_includes_relation_when_interval_covers_target_chapter_and_excludes_after() -> None:
    from app import stages

    character = _character(
        name="孟浩",
        relations=[CharacterRelation(
            to="王有材", relation_kind="并肩作战",
            evidence_chapter_index=10, evidence_quote="孟浩与王有材并肩作战",
            valid_from_chapter=10, valid_to_chapter=60,
        )],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [
        {"idx": 30, "content": "孟浩继续赶路"},
        {"idx": 90, "content": "孟浩独自一人"},
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card_within = stages.build_chapter_cognition_card(bible, chapters_by_idx, 30)
    assert card_within.present_characters[0].relations_as_of == [
        "王有材（并肩作战），第10章证据",
    ]

    card_after = stages.build_chapter_cognition_card(bible, chapters_by_idx, 90)
    assert card_after.present_characters[0].relations_as_of == []


def test_card_dedups_same_org_by_latest_valid_from_chapter() -> None:
    """区间重叠时"最近生效的一条优先"（§4.2 point 2，与 character_portraits 的
    ORDER BY ep_start DESC LIMIT 1 惯例同构）：同一角色对同一 org 有两条记录，第 80
    章查询时两条区间都覆盖，应只保留 valid_from_chapter 更大（更晚生效）的一条。"""
    from app import stages

    character = _character(
        name="王有材",
        affiliations=[
            CharacterAffiliation(
                org="血妖宗", relation_kind="初期成员",
                evidence_chapter_index=3, evidence_quote="王有材加入血妖宗外门",
                valid_from_chapter=3,
            ),
            CharacterAffiliation(
                org="血妖宗", relation_kind="核心弟子",
                evidence_chapter_index=50, evidence_quote="王有材升为血妖宗核心弟子",
                valid_from_chapter=50,
            ),
        ],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [{"idx": 80, "content": "王有材依旧活跃"}]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 80)
    entry = card.present_characters[0]
    assert entry.affiliations_as_of == ["血妖宗（核心弟子），第50章证据"]


def test_card_keeps_multiple_distinct_orgs_independently() -> None:
    """不同 org 之间不互相竞争：一个角色可以同时对多个不同势力各有一条独立生效的
    归属事实（去重只发生在同一 org 内部）。"""
    from app import stages

    character = _character(
        name="孟浩",
        affiliations=[
            CharacterAffiliation(
                org="靠山宗", relation_kind="",
                evidence_chapter_index=1, evidence_quote="孟浩出身靠山宗",
                valid_from_chapter=1,
            ),
            CharacterAffiliation(
                org="散修联盟", relation_kind="",
                evidence_chapter_index=20, evidence_quote="孟浩加入散修联盟",
                valid_from_chapter=20,
            ),
        ],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [{"idx": 30, "content": "孟浩依旧在场"}]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 30)
    entry = card.present_characters[0]
    assert entry.affiliations_as_of == [
        "靠山宗，第1章证据", "散修联盟，第20章证据",
    ]


# ---------- 2. 恒真事实（别名）不受 N 影响 ----------

def test_alias_based_presence_not_gated_by_chapter_distance() -> None:
    """身份事实恒真：别名在证据章节（第 2 章）核验通过后，对全书任何章节都成立——
    在远离证据章节的第 800 章，只要别名文本逐字命中该章原文，依然应判定"在场"，
    不因章节号 N 而受限（与状态事实的区间过滤形成对照）。"""
    from app import stages

    character = _character(
        name="李富贵",
        aliases=[CharacterAlias(
            text="小胖子", name_kind="referential",
            evidence_chapter_index=2, evidence_quote="小胖子跑了过来，正是李富贵",
        )],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [
        {"idx": 2, "content": "小胖子跑了过来，正是李富贵"},
        {"idx": 800, "content": "小胖子依旧那副憨态可掬的模样"},
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 800)
    assert len(card.present_characters) == 1
    entry = card.present_characters[0]
    assert entry.name == "李富贵"
    assert "小胖子" in entry.matched_surface_forms


# ---------- 3. 空数据优雅退化 ----------

def test_card_with_no_status_facts_has_empty_summaries_and_lines() -> None:
    """当前真实状态：backfill_character_status_facts 尚未真实跑过，affiliations/
    relations 均为空——认知卡组装不得报错，只是没有状态事实块。"""
    from app import stages

    character = _character(name="孟浩")
    bible = Bible(characters=[character], world=WORLD)
    chapters = [{"idx": 1, "content": "孟浩独自走在山道上"}]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 1)
    entry = card.present_characters[0]
    assert entry.affiliations_as_of == []
    assert entry.relations_as_of == []
    assert stages._cognition_status_lines(card) == []


def test_cognition_status_lines_none_card_returns_empty() -> None:
    from app import stages

    assert stages._cognition_status_lines(None) == []


# ---------- 4. 确定性截断 ----------

def test_card_truncates_present_characters_at_max_constant() -> None:
    from app import stages

    names = [f"角色{i}" for i in range(1, stages.CHAPTER_COGNITION_CARD_MAX_CHARACTERS + 2)]
    characters = [_character(name=n) for n in names]
    bible = Bible(characters=characters, world=WORLD)
    content = "、".join(names) + "同时登场。"
    chapters = [{"idx": 1, "content": content}]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 1)
    assert len(card.present_characters) == stages.CHAPTER_COGNITION_CARD_MAX_CHARACTERS
    assert [e.name for e in card.present_characters] == names[:stages.CHAPTER_COGNITION_CARD_MAX_CHARACTERS]


def test_card_truncates_facts_per_kind_at_max_constant() -> None:
    from app import stages

    n = stages.CHAPTER_COGNITION_FACTS_MAX_PER_KIND + 1
    character = _character(
        name="王有材",
        affiliations=[
            CharacterAffiliation(
                org=f"势力{i}", relation_kind="",
                evidence_chapter_index=1, evidence_quote="...",
                valid_from_chapter=1,
            )
            for i in range(1, n + 1)
        ],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [{"idx": 1, "content": "王有材同时挂靠多方势力"}]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card = stages.build_chapter_cognition_card(bible, chapters_by_idx, 1)
    entry = card.present_characters[0]
    assert len(entry.affiliations_as_of) == stages.CHAPTER_COGNITION_FACTS_MAX_PER_KIND
    assert entry.affiliations_as_of == [
        f"势力{i}，第1章证据" for i in range(1, stages.CHAPTER_COGNITION_FACTS_MAX_PER_KIND + 1)
    ]


# ---------- 5. 可复现（同一输入任何时候重建结果逐字节相同） ----------

def test_card_build_is_deterministic_across_repeated_calls() -> None:
    from app import stages

    character = _character(
        name="王有材",
        affiliations=[CharacterAffiliation(
            org="血妖宗", relation_kind="", evidence_chapter_index=3,
            evidence_quote="王有材拜入血妖宗", valid_from_chapter=3,
        )],
    )
    bible = Bible(characters=[character], world=WORLD)
    chapters = [{"idx": 3, "content": "王有材站在原地"}, {"idx": 4, "content": "王有材继续修炼"}]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    card1 = stages.build_chapter_cognition_card(bible, chapters_by_idx, 3)
    card2 = stages.build_chapter_cognition_card(bible, chapters_by_idx, 3)
    assert card1 == card2
    assert card1.model_dump() == card2.model_dump()


# ---------- 6. _cognition_status_lines 渲染 ----------

def test_cognition_status_lines_render_bullet_with_both_affiliation_and_relation() -> None:
    from app import stages

    entry = stages.ChapterCognitionEntry(
        name="王有材",
        affiliations_as_of=["血妖宗，第3章证据"],
        relations_as_of=["王腾飞（同门），第5章证据"],
    )
    card = stages.ChapterCognitionCard(
        chapter_idx=10, forward_window_chapters=20, present_characters=[entry],
    )
    lines = stages._cognition_status_lines(card)
    assert lines == ["- 王有材：归属 血妖宗，第3章证据；关系 王腾飞（同门），第5章证据"]


def test_cognition_status_lines_skips_entry_with_no_facts() -> None:
    """在场但没有任何归属/关系摘要的角色不出现在候选人已知状态文本块里。"""
    from app import stages

    entry_with_facts = stages.ChapterCognitionEntry(
        name="王有材", affiliations_as_of=["血妖宗，第3章证据"],
    )
    entry_without_facts = stages.ChapterCognitionEntry(name="孟浩")
    card = stages.ChapterCognitionCard(
        chapter_idx=10, forward_window_chapters=20,
        present_characters=[entry_with_facts, entry_without_facts],
    )
    lines = stages._cognition_status_lines(card)
    assert lines == ["- 王有材：归属 血妖宗，第3章证据"]


# ---------- 7. _alias_verdict_call 提示词注入 ----------

_DOSSIER = [{"chapter_idx": 5, "segment_index": 1, "text": "孟浩说了句话"}]


def test_prompt_identical_when_cognition_card_is_none(monkeypatch) -> None:
    """空数据（cognition_card=None）时提示词必须与注入前逐字一致。"""
    from app import stages
    from app.harness import model_gateway

    fake, captured = _capturing_chat_structured("孟浩")
    monkeypatch.setattr(model_gateway, "chat_structured", fake)
    asyncio.run(stages._alias_verdict_call(
        alias="小孟", true_name="孟浩", dossier=_DOSSIER, candidates=["孟浩"],
        project_id=None, cognition_card=None,
    ))
    prompt = captured["prompt"]
    assert prompt.startswith('下面是原著第 5 章中包含称谓"小孟"的原文段落')
    assert "候选人已知状态" not in prompt


def test_prompt_identical_when_cognition_card_has_no_facts(monkeypatch) -> None:
    """红灯核心用例：认知卡对象存在但没有任何角色带归属/关系摘要（当前真实状态）时，
    提示词必须与不传认知卡时逐字相同——不留空标题、不留占位噪声。"""
    from app import stages
    from app.harness import model_gateway

    empty_card = stages.ChapterCognitionCard(
        chapter_idx=5, forward_window_chapters=20,
        present_characters=[stages.ChapterCognitionEntry(name="孟浩", matched_surface_forms=["孟浩"])],
    )

    fake_none, captured_none = _capturing_chat_structured("孟浩")
    monkeypatch.setattr(model_gateway, "chat_structured", fake_none)
    asyncio.run(stages._alias_verdict_call(
        alias="小孟", true_name="孟浩", dossier=_DOSSIER, candidates=["孟浩"],
        project_id=None, cognition_card=None,
    ))

    fake_empty, captured_empty = _capturing_chat_structured("孟浩")
    monkeypatch.setattr(model_gateway, "chat_structured", fake_empty)
    asyncio.run(stages._alias_verdict_call(
        alias="小孟", true_name="孟浩", dossier=_DOSSIER, candidates=["孟浩"],
        project_id=None, cognition_card=empty_card,
    ))

    assert captured_none["prompt"] == captured_empty["prompt"]


def test_prompt_with_cognition_card_has_clear_sections_and_hard_requirement(monkeypatch) -> None:
    """认知卡非空时：分区标注清晰（认知卡段落在前，原文段落在后，各自明确标注），
    且必须含"不得仅凭认知卡下结论"的措辞；原有卷宗/候选/任务指令段落不变。"""
    from app import stages
    from app.harness import model_gateway

    card = stages.ChapterCognitionCard(
        chapter_idx=5, forward_window_chapters=20,
        present_characters=[
            stages.ChapterCognitionEntry(
                name="王有材", affiliations_as_of=["血妖宗，第3章证据"],
            ),
            stages.ChapterCognitionEntry(
                name="王腾飞", affiliations_as_of=["靠山宗，第5章证据"],
            ),
        ],
    )
    fake, captured = _capturing_chat_structured("王有材")
    monkeypatch.setattr(model_gateway, "chat_structured", fake)
    asyncio.run(stages._alias_verdict_call(
        alias="王师弟", true_name="王腾飞", dossier=_DOSSIER,
        candidates=["王有材", "王腾飞"], project_id=None, cognition_card=card,
    ))
    prompt = captured["prompt"]

    assert "候选人已知状态" in prompt
    assert "不得仅凭以上认知卡下结论" in prompt
    assert "血妖宗，第3章证据" in prompt
    assert "靠山宗，第5章证据" in prompt
    # 分区顺序：认知卡背景参考在前，卷宗原文段落标注在后。
    assert prompt.index("候选人已知状态") < prompt.index("下面是原著第")
    # 既有卷宗/候选/任务指令段落原样保留，不被认知卡注入改写。
    assert "孟浩说了句话" in prompt
    assert "该章出场的人物谱角色候选" in prompt
    assert (
        '任务一（选人）：仅依据以上原文段落本身，判断称谓"王师弟"最可能指代上面候选中的'
        '哪一位\n本人。'
    ) in prompt
    # 排他性判据（任务二）是本次新增内容，与任务一结构上不同的问题，认知卡分区
    # 与任务指令段落变化互不干扰。
    assert "任务二（判排他性，与任务一是两个不同的问题）" in prompt
    assert "is_exclusive_reference" in prompt


# ---------- 8. 端到端回归：真实误登记事故 2 接入认知卡后不倒退 ----------

WANG_TENGFEI_CHAPTER = [
    {"idx": 189, "title": "第一百八十九章", "content": (
        "血妖宗那里，王有材默默的站起身，一语不发，但却站在了孟浩的身后。\n\n"
        "王腾飞身子向前迈出一步，一脸杀气，眼中杀机毕露，更是双眼露出寒芒，"
        "死死的盯着孟浩。\n\n"
        "“虽然你那顶帽子很让人厌烦，但看在王师弟的份上，我血妖宗也算一个，"
        "倒要看看今日，谁敢动你。”李诗琪冷声开口。"
    )},
]


def test_end_to_end_cognition_card_injected_and_real_accident_still_rejected(monkeypatch) -> None:
    """真实误登记事故 2（王腾飞←王师弟，第 189 章，见 tests/test_character_alias.py
    的原样复刻）接入认知卡（`_alias_evidence_resolution(bible=...)`）后的回归：认知卡
    确实把王有材的历史归属（血妖宗）注入了提示词，但裁决闸的既有安全网（候选枚举、
    段号钉证、选中候选集里的其他人一律拒绝）必须继续拦下这条错误申报——不能因为
    认知卡的引入放松已有防线（§11 判据 4）。"""
    from app import stages
    from app.harness import model_gateway

    characters = [
        _character(name="孟浩"),
        _character(name="王有材", affiliations=[CharacterAffiliation(
            org="血妖宗", relation_kind="弟子",
            evidence_chapter_index=3, evidence_quote="王有材拜入血妖宗",
            valid_from_chapter=3,
        )]),
        _character(name="李诗琪"),
        _character(name="王腾飞"),
    ]
    bible = Bible(characters=characters, world=WORLD)
    chapters_by_idx = stages._chapters_by_idx(WANG_TENGFEI_CHAPTER)
    roster = stages._alias_verdict_roster(bible)

    fake, captured = _capturing_chat_structured("王有材")
    monkeypatch.setattr(model_gateway, "chat_structured", fake)

    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"王腾飞"}, "王师弟", "王腾飞", 189,
        "但看在王师弟的份上",
        roster=roster, bible=bible,
    ))

    assert resolved["accepted"] is False
    assert resolved["reason"] == "candidate_mismatch"
    prompt = captured["prompt"]
    assert "候选人已知状态" in prompt
    assert "血妖宗" in prompt
    assert "不得仅凭以上认知卡下结论" in prompt


def test_end_to_end_without_bible_keeps_prompt_unaffected(monkeypatch) -> None:
    """不传 bible（现有调用方式，或未来任何未更新的调用点）时行为与认知卡引入前完全
    一致：不组装认知卡、不注入提示词。"""
    from app import stages
    from app.harness import model_gateway

    roster = {"孟浩": ["孟浩"], "王有材": ["王有材"], "李诗琪": ["李诗琪"], "王腾飞": ["王腾飞"]}
    chapters_by_idx = stages._chapters_by_idx(WANG_TENGFEI_CHAPTER)

    fake, captured = _capturing_chat_structured("王有材")
    monkeypatch.setattr(model_gateway, "chat_structured", fake)

    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"王腾飞"}, "王师弟", "王腾飞", 189,
        "但看在王师弟的份上",
        roster=roster,
    ))

    assert resolved["accepted"] is False
    assert resolved["reason"] == "candidate_mismatch"
    assert "候选人已知状态" not in captured["prompt"]
