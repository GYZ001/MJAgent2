"""人物认知层状态事实（层 A）：schemas.CharacterAffiliation/CharacterRelation 结构 +
stages.py 窄口径核验/回填。对应 docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.1 / §9 P0
第 1、2 项。

核验原则贯穿全文件：模型申报 + 代码核验，不确定不登记；判据只看结构（逐字子串命中 +
章节内共现 + 候选判别 + 区间证据支撑），不针对任何具体人名/势力名/称谓做特判。核验
管线复用层一别名回填已经过三次真实事故验证的机制（见 tests/test_character_alias.py），
本文件只新增"状态事实"独有的部分：有效区间核验，以及归属/关系专用的候选判别提问措辞。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.schemas import Bible, Character, CharacterAffiliation, CharacterRelation, World

APPEARANCE = "二十岁女子，墨发高马尾，银色素面长袍，身形清瘦，背后一柄银色长剑，眉目清冷"


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


def _fake_verdict_chat_structured(
    selected_candidate: str,
    supporting_segment_index: int = 1,
    supporting_quote: str = "",
):
    """与 tests/test_character_alias.py 同名 helper 同一实现：构造
    model_gateway.chat_structured 的假实现，固定返回给定裁决结论。裁决响应类
    （`app.stages._AliasVerdictResponse`）在状态事实回填里被直接复用，不需要另造。

    不带 `is_exclusive_reference`：那是别名场景专用的排他性判据字段，只属于
    `app.stages._AliasExclusivityVerdictResponse`（`_alias_verdict_call` 专用子类）；
    状态事实这条路径（`_status_fact_verdict_call`）继续用不含这个字段的基类
    `_AliasVerdictResponse`，两条路径各自的 schema 只包含各自 prompt 真正问过的
    字段，这里不需要也不应该补这个字段。"""

    async def fake(_messages, **kwargs):
        model_type = kwargs["model_type"]
        return model_type(
            selected_candidate=selected_candidate,
            supporting_segment_index=supporting_segment_index,
            supporting_quote=supporting_quote,
        )

    return fake


# ---------- 1. CharacterAffiliation / CharacterRelation 结构 + Character 字段 ----------

def test_character_affiliation_requires_all_evidence_fields() -> None:
    aff = CharacterAffiliation(
        org="血妖宗", relation_kind="membership",
        evidence_chapter_index=5, evidence_quote="欢迎孟浩加入我血妖宗",
        valid_from_chapter=5,
    )
    assert aff.org == "血妖宗"
    assert aff.valid_from_chapter == 5
    assert aff.valid_to_chapter is None


def test_character_affiliation_missing_field_rejected() -> None:
    with pytest.raises(Exception):
        CharacterAffiliation(org="血妖宗", relation_kind="membership")


def test_character_relation_requires_all_evidence_fields() -> None:
    rel = CharacterRelation(
        to="王有材", relation_kind="ally",
        evidence_chapter_index=8, evidence_quote="孟浩与王有材并肩作战",
        valid_from_chapter=8,
    )
    assert rel.to == "王有材"
    assert rel.valid_to_chapter is None


def test_character_relation_missing_field_rejected() -> None:
    with pytest.raises(Exception):
        CharacterRelation(to="王有材", relation_kind="ally")


def test_character_affiliations_and_relations_default_empty() -> None:
    character = _character()
    assert character.affiliations == []
    assert character.relations == []


def test_character_status_facts_round_trip_through_bible_json() -> None:
    character = _character(
        affiliations=[CharacterAffiliation(
            org="血妖宗", relation_kind="membership",
            evidence_chapter_index=5, evidence_quote="欢迎孟浩加入我血妖宗",
            valid_from_chapter=5, valid_to_chapter=40,
        )],
        relations=[CharacterRelation(
            to="王有材", relation_kind="ally",
            evidence_chapter_index=8, evidence_quote="孟浩与王有材并肩作战",
            valid_from_chapter=8,
        )],
    )
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))
    dumped = json.loads(bible.model_dump_json())
    restored = Bible.model_validate(dumped)
    assert restored.characters[0].affiliations[0].org == "血妖宗"
    assert restored.characters[0].affiliations[0].valid_to_chapter == 40
    assert restored.characters[0].relations[0].to == "王有材"


def test_old_bible_json_without_status_fact_fields_loads_compatibly() -> None:
    """既有 bible_json（新增字段前落库的数据，甚至层一 aliases 也可能缺失）没有
    affiliations/relations 键，反序列化不得失败。"""
    legacy_bible_json = {
        "characters": [{
            "name": "孟浩",
            "role": "主角",
            "appearance_canonical": APPEARANCE,
            "personality": "坚韧",
            "speech_style": "直接",
            "relationships": [],
        }],
        "world": {"visual_style_canonical": "国漫3D动画电影质感，精致光影"},
    }
    bible = Bible.model_validate(legacy_bible_json)
    assert bible.characters[0].affiliations == []
    assert bible.characters[0].relations == []
    assert bible.characters[0].aliases == []


# ---------- 2. 有效区间核验：_status_fact_interval_resolution（纯函数，无模型调用） ----------

AFFILIATION_CHAPTERS = [
    {"idx": 5, "title": "第五章", "content": (
        "孟浩来到血妖宗的山门前，只见宗主亲自出迎：欢迎孟浩加入我血妖宗。"
    )},
    {"idx": 9, "title": "第九章", "content": (
        "孟浩独自一人在城中闲逛，并未提及宗门之事。"
    )},
    {"idx": 40, "title": "第四十章", "content": (
        "孟浩转身离开血妖宗，从此再不理会宗门事务，与血妖宗恩断义绝。"
    )},
]


def test_interval_defaults_to_evidence_chapter_when_not_declared() -> None:
    """起止章都不申报时：起点回退为核心证据所在章，终点回退为 None（尚无证据表明
    已失效）——与 character_portraits 表 ep_end IS NULL 惯例同构。未申报不是"外推被
    丢弃"，所以两个回落标注都是 False。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 5, None, None,
    )
    assert result == (5, False, None, False)


def test_interval_start_accepted_when_boundary_chapter_has_support() -> None:
    """valid_from_chapter 与核心证据章不同，但申报的起点章节本身也能找到
    claim_text + anchor 共现——应当被接受（这里起点就等于核心证据章本身，
    最简单的"有支撑"场景），且不是回落值（是模型申报并独立核验通过的边界）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 40, 5, None,
    )
    # 起点 5 章：与核心证据章（40）不同，但第 5 章确实有"孟浩"+"血妖宗"共现支撑。
    assert result == (5, False, None, False)


def test_interval_end_accepted_when_boundary_chapter_has_support() -> None:
    """valid_to_chapter 申报为第 40 章（叛出宗门），该章确实有"孟浩"+"血妖宗"共现
    支撑（原文明确交代归属结束）——应当被接受，非回落值。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 5, None, 40,
    )
    assert result == (5, False, 40, False)


def test_interval_start_declared_but_unsupported_falls_back_and_is_flagged() -> None:
    """红灯先行核心场景（本次事故修复的直接对象）：核心证据在第 40 章（不矛盾），但
    申报的起点边界（第 9 章）没有独立共现支撑（该章只有"孟浩"，没有"血妖宗"）——修前
    这会让整条事实被拒绝（interval_contradiction 之外的情形也返回 None）；修后应保留
    核心证据、起点回落为核心证据章本身，并标注 valid_from_is_fallback=True，如实反映
    "这是代码回落值，不是模型申报并核验通过的边界"。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 40, 9, None,
    )
    assert result == (40, True, None, False)


def test_interval_end_declared_but_unsupported_falls_back_and_is_flagged() -> None:
    """红灯先行：valid_to_chapter 同样没有共现支撑时（第 9 章只有"孟浩"，没有
    "血妖宗"，且 9 不早于证据章 5，不构成矛盾）——回落为开放终点（None）并标注
    valid_to_is_fallback=True，不再拒绝整条事实。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 5, None, 9,
    )
    assert result == (5, False, None, True)


def test_interval_start_rejected_when_declared_after_evidence_chapter() -> None:
    """反例（守边界）：起点不能晚于核心证据章——核心证据本身已经证明该事实在证据章
    成立，申报一个更晚的起点与此矛盾（自相矛盾，不是外推不足），必须整条拒绝。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 5, 40, None,
    )
    assert result is None


def test_interval_end_rejected_when_declared_before_evidence_chapter() -> None:
    """反例（守边界）：终点不能早于核心证据章，同一理由的镜像场景，必须整条拒绝。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 40, None, 5,
    )
    assert result is None


def test_interval_boundary_equal_to_evidence_chapter_is_trivially_accepted() -> None:
    """起止章申报为与核心证据章相同的值——不需要额外核验（本身就是已核验的证据点），
    非回落值。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 5, 5, 5,
    )
    assert result == (5, False, 5, False)


# 缺陷 A 专用章节：主体是"几乎每章无处不在"的主角，边界章第 20 章里主体与对象各自
# 都出现过，但分别落在不同自然段（用空行隔开），从未在同一段里连接起来——与
# `_status_fact_quote_dual_anchor_verified` docstring 引用的真实事故"王腾飞→韩宗"
# 同一形状：第27章章级共现闸判定通过（王腾飞出现5次），但被登记的引句其实是韩宗
# 对孟浩说话，句中根本没有王腾飞。
BOUNDARY_CHAPTER_WITH_CHAPTER_WIDE_BUT_NOT_SEGMENT_LEVEL_COOCCURRENCE = {
    "idx": 20, "title": "第二十章", "content": (
        "孟浩独自一人在城中闲逛，心里想着修炼之事。\n\n"
        "韩宗冷淡开口：血妖宗要对付的可不止一人，你们都得小心。"
    ),
}


def test_interval_boundary_chapter_wide_cooccurrence_without_dual_anchor_falls_back() -> None:
    """红灯先行（缺陷 A 核心场景）：declared_valid_from_chapter=20 这一章里，"孟浩"
    （主体锚点）只出现在第一段，"血妖宗"（对象）只出现在第二段——旧实现只做章级
    共现（claim_text 与 anchor 之一整章任意位置都出现即通过）会误判该边界"经过
    独立核验"（valid_from_is_fallback=False），但该章其实从未把这条边界与主体接到
    一起，与 `_status_fact_quote_dual_anchor_verified` docstring 引用的真实事故
    同一形状。核心证据本身在别处（第40章）已核验，这里只测边界判定本身：新实现
    改用与核心证据同一条双锚定原语按自然段核验，该章任何一段都不同时含"孟浩"与
    "血妖宗"，必须回落为 valid_from_is_fallback=True，起点回落为核心证据章（40）。

    变异验证：把 `_status_fact_interval_resolution` 里的边界判定改回
    `claim_text in boundary_text and any(anchor in boundary_text for anchor in
    anchor_texts)` 这种章级共现（去掉双锚定），本测试会变红（拿到
    (20, False, None, False)，即误判为已核验）。"""
    from app import stages

    chapters = AFFILIATION_CHAPTERS + [
        BOUNDARY_CHAPTER_WITH_CHAPTER_WIDE_BUT_NOT_SEGMENT_LEVEL_COOCCURRENCE,
    ]
    chapters_by_idx = stages._chapters_by_idx(chapters)
    # 章级共现前提校验：确保这确实是"整章共现通过、但没有任何一段同时锚定"的场景，
    # 不是测试数据本身没构造对。
    boundary_text = chapters_by_idx[20]
    assert "孟浩" in boundary_text and "血妖宗" in boundary_text, "前提：整章共现应通过"
    assert not any(
        "孟浩" in seg.text and "血妖宗" in seg.text
        for seg in stages.index_source_segments(boundary_text)
    ), "前提：任何一段都不应同时含主体与对象"

    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 40, 20, None,
    )
    assert result == (40, True, None, False)


def test_interval_boundary_accepted_when_segment_level_dual_anchor_present() -> None:
    """对照正例（不得误伤）：同一份 AFFILIATION_CHAPTERS 第 5 章本身就是单段落，
    "孟浩"与"血妖宗"同段共现——应当被接受为独立核验通过的边界，非回落值。与
    `test_interval_start_accepted_when_boundary_chapter_has_support` 覆盖同一
    场景，这里换用新签名（object_anchor_texts 为集合）与新判据（段级双锚定）
    重新确认一遍，防止双锚定改造引入回归。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, {"血妖宗"}, 40, 5, None,
    )
    assert result == (5, False, None, False)


# ---------- 3. 核心证据判定 + 候选判别裁决：_status_fact_evidence_resolution ----------

def test_status_fact_accepted_when_declaration_verified_and_candidate_matches(monkeypatch) -> None:
    """正例：申报「孟浩在第5章加入血妖宗」——声明核验通过（引句逐字命中 + 共现），
    候选判别裁决选中孟浩本人，应当登记成功，区间回退为默认（起点=证据章，终点=None）。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    roster = {"孟浩": ["孟浩"]}
    quote = "欢迎孟浩加入我血妖宗"

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("孟浩"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", "孟浩", 5, quote, None, None,
        fact_noun="势力归属", roster=roster,
    ))
    assert resolved["accepted"] is True
    assert resolved["chapter_idx"] == 5
    assert resolved["quote"] == quote
    assert resolved["valid_from_chapter"] == 5
    assert resolved["valid_from_is_fallback"] is False
    assert resolved["valid_to_chapter"] is None
    assert resolved["valid_to_is_fallback"] is False


def test_status_fact_rejected_when_org_never_cooccurs_with_subject() -> None:
    """红灯：申报的势力名（靠山宗）在全书任何章节都没有与角色本人共现出现过——
    声明核验失败，桥接检索全书都找不到桥接章 → no_bridge_chapter（不发起裁决调用
    也能拒绝）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "靠山宗", "孟浩", 5,
        "编造的、原文里根本没有的一句话", None, None,
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "no_bridge_chapter"


CANDIDATE_CONFUSION_CHAPTER = [
    {"idx": 12, "title": "第十二章", "content": (
        "血妖宗的李诗琪缓步上前，欲替孟浩说情。孟浩皱眉看着眼前这群血妖宗弟子，"
        "警惕不敢松懈，始终未曾归附血妖宗。"
    )},
]


def test_status_fact_rejected_when_candidate_verdict_selects_a_different_character(
    monkeypatch,
) -> None:
    """红灯（真实误登记事故同型复刻）：孟浩是主角，几乎每章都出场，与"血妖宗"这个
    势力名同章共现不代表他本人归属血妖宗——这一章真正与血妖宗有归属关系的是李诗琪，
    孟浩反而"始终未曾归附"。旧三闸（声明核验）只看共现，会误放行；候选判别裁决必须
    把李诗琪也纳入候选台面，一旦模型独立判别选中李诗琪（不是本次申报的孟浩），必须
    拒绝，reason=candidate_mismatch。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(CANDIDATE_CONFUSION_CHAPTER)
    quote = "血妖宗的李诗琪缓步上前，欲替孟浩说情"
    roster = {"孟浩": ["孟浩"], "李诗琪": ["李诗琪"]}

    # 先确认：只用旧声明闸（不含候选判别）会误放行——证明候选判别裁决补的是真实漏洞。
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"孟浩"}, "血妖宗", 12, quote,
    ) is True

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("李诗琪"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", "孟浩", 12, quote, None, None,
        fact_noun="势力归属", roster=roster,
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "candidate_mismatch"


def test_status_fact_rejected_when_candidate_verdict_uncertain(monkeypatch) -> None:
    """红灯：候选判别裁决选"都不是/无法确定"——同样必须拒绝，不确定不登记的安全默认
    不是只拒绝"确定选了别人"这一种情况。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    quote = "欢迎孟浩加入我血妖宗"

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured(stages._ALIAS_VERDICT_NO_MATCH_LABEL),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", "孟浩", 5, quote, None, None,
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "candidate_uncertain"


def test_status_fact_rejected_when_declared_interval_contradicts_evidence(monkeypatch) -> None:
    """反例（守边界）：核心证据本身可核验（候选判别通过），但申报的起点（第 9 章）
    晚于核心证据章（第 5 章）——这是自相矛盾（核心证据已证明该事实在第 5 章成立，
    起点不能晚于它），不是外推不足，必须整条拒绝，reason=interval_contradiction。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    quote = "欢迎孟浩加入我血妖宗"

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("孟浩"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", "孟浩", 5, quote,
        9,  # declared_valid_from_chapter 晚于核心证据章 5：自相矛盾
        None,
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "interval_contradiction"


def test_status_fact_accepted_when_declared_interval_unverifiable_falls_back_and_is_flagged(
    monkeypatch,
) -> None:
    """红灯先行核心场景（本次事故修复的直接对象，复现真实项目 proj_3ac0b627fa46 的
    根因形状）：核心证据本身可核验（候选判别通过、核心证据在第 40 章），但申报的起点
    边界（第 9 章，早于证据章、不构成矛盾）没有独立共现支撑（第 9 章只有"孟浩"，没有
    "血妖宗"）——修前旧代码会让整条事实被拒绝（区间核验不通过就整体拒绝，不区分"矛盾"
    与"外推不足"）；修后应保留已核验的核心事实，起点回落为核心证据章本身，并标注
    valid_from_is_fallback=True，如实反映这是回落值、不是模型申报并核验通过的边界。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    quote = "孟浩转身离开血妖宗，从此再不理会宗门事务，与血妖宗恩断义绝。"

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("孟浩"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", "孟浩", 40, quote,
        9,  # declared_valid_from_chapter：第 9 章没有"血妖宗"，无独立支撑，但不矛盾（9<40）
        None,
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is True
    assert resolved["chapter_idx"] == 40
    assert resolved["valid_from_chapter"] == 40  # 回落为核心证据章，不采信申报的第 9 章
    assert resolved["valid_from_is_fallback"] is True
    assert resolved["valid_to_chapter"] is None
    assert resolved["valid_to_is_fallback"] is False


def test_status_fact_accepted_with_verified_interval_boundaries(monkeypatch) -> None:
    """绿灯：核心证据 + 起止章都能各自找到共现支撑 → 全部登记，区间与锚点正确，且
    两个边界都不是回落值（模型申报的边界本身独立核验通过，原样采信）。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    quote = "欢迎孟浩加入我血妖宗"

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("孟浩"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", "孟浩", 5, quote,
        None, 40,  # declared_valid_to_chapter：第 40 章确有"孟浩"+"血妖宗"共现（叛出宗门）
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is True
    assert resolved["chapter_idx"] == 5
    assert resolved["valid_from_chapter"] == 5
    assert resolved["valid_from_is_fallback"] is False
    assert resolved["valid_to_chapter"] == 40
    assert resolved["valid_to_is_fallback"] is False


def test_status_fact_rejected_when_verdict_call_raises(monkeypatch) -> None:
    """裁决调用本身失败（网络/鉴权/供应商故障）按不确定处理——不能因为拿不到裁决
    结果就放行。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)

    async def failing_chat_structured(_messages, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(model_gateway, "chat_structured", failing_chat_structured)
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", "孟浩", 5,
        "欢迎孟浩加入我血妖宗", None, None,
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "verdict_call_failed"


def test_status_fact_rejected_when_segment_index_out_of_dossier(monkeypatch) -> None:
    """红灯：候选选对了，但 supporting_segment_index 不在本次卷宗实际收录的段号
    集合内——钉证是结构性判断，段号越界必须拒绝。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("孟浩", supporting_segment_index=99),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", "孟浩", 5,
        "欢迎孟浩加入我血妖宗", None, None,
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "segment_not_pinned"


# ---------- 3b. 关系（relation）场景：claim_text 是另一个人物谱角色的规范名 ----------

RELATION_CHAPTERS = [
    {"idx": 8, "title": "第八章", "content": (
        "危难之际，王有材与孟浩并肩作战，二人结为过命之交。"
    )},
]


def test_status_fact_relation_accepted_when_target_is_another_character(monkeypatch) -> None:
    """正例：孟浩与王有材结为盟友——claim_text=王有材（另一角色规范名），候选判别
    裁决选中孟浩本人（即被测的关系主体），应当登记成功。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(RELATION_CHAPTERS)
    roster = {"孟浩": ["孟浩"], "王有材": ["王有材"]}
    quote = "王有材与孟浩并肩作战，二人结为过命之交"

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("孟浩"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "王有材", "孟浩", 8, quote, None, None,
        fact_noun="人物关系", roster=roster,
    ))
    assert resolved["accepted"] is True
    assert resolved["chapter_idx"] == 8


def test_status_fact_verdict_excludes_claim_text_from_candidates_for_relations(
    monkeypatch,
) -> None:
    """回归测试（本次"状态事实回填 100% 拒绝"事故的真实根因，不是有效区间核验）：
    关系事实的 claim_text（关系对象 to）本身就是候选集里的另一个已收录角色——旧提示
    词问的是"这段证据所描述的{fact_noun}（对象：'{claim_text}'）实际说的是候选中的
    哪一位本人"，对关系事实这是道错题：模型会老实回答"'{claim_text}'这个名字指的就是
    候选里的它自己"（真实项目 proj_3ac0b627fa46 provider_calls 10692/10693/10695 等
    可查：claim_text=韩宗/曹阳/上官修时 selected_candidate 精确等于 claim_text），但
    调用方比对的是 `selected_candidate != subject_name`（subject_name 是关系发起方，
    结构上恒不等于 to）——问题与比对目标错位，导致人物关系 100% 必然 candidate_
    mismatch，与证据是否真实成立无关；区间核验从未被触及。修复后 claim_text 必须从
    传给裁决模型的候选枚举（call_meta 记账 + JSON Schema enum 两处）里剔除，结构上
    让模型不可能选中它自己作为答案。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(RELATION_CHAPTERS)
    roster = {"孟浩": ["孟浩"], "王有材": ["王有材"]}
    quote = "王有材与孟浩并肩作战，二人结为过命之交"

    seen: dict[str, object] = {}

    async def fake_chat_structured(_messages, **kwargs):
        seen["schema"] = kwargs["output_schema"]
        seen["candidates_meta"] = kwargs["call_meta"]["candidates"]
        model_type = kwargs["model_type"]
        return model_type(
            selected_candidate="孟浩", supporting_segment_index=1, supporting_quote="",
        )

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "王有材", "孟浩", 8, quote, None, None,
        fact_noun="人物关系", roster=roster,
    ))
    assert resolved["accepted"] is True
    assert "王有材" not in seen["candidates_meta"]
    assert "王有材" not in seen["schema"]["properties"]["selected_candidate"]["enum"]
    assert "孟浩" in seen["candidates_meta"]


RELATION_CANDIDATE_CONFUSION_CHAPTER = [
    {"idx": 20, "title": "第二十章", "content": (
        "王有材与李诗琪结为道侣，孟浩在一旁笑着道贺，却并未与王有材有更深交情。"
    )},
]


def test_status_fact_relation_rejected_when_candidate_verdict_selects_different_character(
    monkeypatch,
) -> None:
    """红灯：孟浩与王有材同章出现、且都与"王有材"这个名字共现，但原文真正与王有材
    结为关系的是李诗琪，孟浩"并未与王有材有更深交情"——候选判别必须能拒绝这种张冠
    李戴。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(RELATION_CANDIDATE_CONFUSION_CHAPTER)
    roster = {"孟浩": ["孟浩"], "王有材": ["王有材"], "李诗琪": ["李诗琪"]}
    quote = "王有材与李诗琪结为道侣，孟浩在一旁笑着道贺"

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("李诗琪"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "王有材", "孟浩", 20, quote, None, None,
        fact_noun="人物关系", roster=roster,
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "candidate_mismatch"


# ---------- 3c. 引句双锚定闸：_status_fact_quote_dual_anchor_verified /
# reason=quote_missing_dual_anchor（事故修复：章级共现闸通过≠被登记引句里真锚定了
# 主体，见 app/stages.py 模块顶部大注释与函数 docstring） ----------

def test_dual_anchor_rejects_quote_missing_subject() -> None:
    """纯函数红灯：引句里只有对象（韩宗），没有主体（王腾飞）——真实事故"王腾飞→韩宗"
    的引句形状（引句是韩宗对孟浩说话，与王腾飞无关）。"""
    from app import stages

    assert stages._status_fact_quote_dual_anchor_verified(
        "\"孟浩，上官师叔寻你有事，你随我走一趟吧。\"韩宗看都不看其他人一眼，"
        "望着孟浩，冷淡开口。",
        {"王腾飞"}, {"韩宗"},
    ) is False


def test_dual_anchor_rejects_quote_missing_object() -> None:
    """纯函数红灯：引句里只有主体，没有对象——三条真实事故之外的镜像场景，直接对这个
    纯函数验证（结构上通过整条 `_status_fact_evidence_resolution` 管线不可能触发，
    因为 claim_text 本身已经被 `_alias_declaration_verified`/`_find_alias_bridge_chapter`
    保证出现在引句里；这里单独验证闸门本身两侧都真的在把关，不是只查了一侧）。"""
    from app import stages

    assert stages._status_fact_quote_dual_anchor_verified(
        "孟浩心情不错，独自离开，并未多言。", {"孟浩"}, {"血妖宗"},
    ) is False


def test_dual_anchor_rejects_quote_containing_only_org() -> None:
    """纯函数红灯：引句只剩组织名三个字——真实事故"许清→靠山宗"的引句形状（引句
    「靠山宗。」，什么主体都没锚定）。"""
    from app import stages

    assert stages._status_fact_quote_dual_anchor_verified(
        "靠山宗。", {"许清"}, {"靠山宗"},
    ) is False


def test_dual_anchor_accepted_when_both_subject_and_object_present() -> None:
    """正例（不得误伤）：主体与对象都在引句里的真实形状。"""
    from app import stages

    assert stages._status_fact_quote_dual_anchor_verified(
        "孟浩，你是我李富贵这一辈子的好朋友", {"李富贵"}, {"孟浩"},
    ) is True


def test_dual_anchor_accepts_alternate_confirmed_alias_of_either_side() -> None:
    """主体/对象只要命中各自集合中的任意一项已确认别名即可，不要求恰好是规范名——
    集合语义，不是单一字符串相等。"""
    from app import stages

    assert stages._status_fact_quote_dual_anchor_verified(
        "孟兄与血妖宗定下盟约", {"孟浩", "孟兄"}, {"血妖宗"},
    ) is True


def test_dual_anchor_rejects_when_either_anchor_set_is_empty() -> None:
    """防御性红灯：调用方传入空锚点集合（理论上不该发生）时不能因为“没有约束”而
    误判通过——空集合视为该侧锚点缺失，直接拒绝。"""
    from app import stages

    assert stages._status_fact_quote_dual_anchor_verified(
        "孟浩与血妖宗结盟", set(), {"血妖宗"},
    ) is False
    assert stages._status_fact_quote_dual_anchor_verified(
        "孟浩与血妖宗结盟", {"孟浩"}, set(),
    ) is False


def test_dual_anchor_tolerates_paired_quote_wrapping_around_whole_quote() -> None:
    """引句被整体包了一层配对引号（全角/半角）不应引入假阴性——复用
    `_quote_comparison_variants` 同一容错（别名回填今晚踩过的同一个坑）。"""
    from app import stages

    assert stages._status_fact_quote_dual_anchor_verified(
        "“孟浩与靠山宗结拜为盟”", {"孟浩"}, {"靠山宗"},
    ) is True


ACCIDENT_MENG_KAOSHAN_CHAPTER = [
    {"idx": 2, "title": "第二章", "content": (
        "这里是北区杂役处，靠山宗不养废物，你二人既来到了此地，做半甲子杂役。"
        "孟浩站在一旁，默默听着，一言不发。"
    )},
]


def test_status_fact_rejected_when_quote_missing_subject_meng_kaoshan_shape() -> None:
    """端到端红灯（真实事故复刻 1）："孟浩→靠山宗"：章级共现闸能通过（"孟浩"确实
    出现在第 2 章），但被登记引用的这一句原文里没有"孟浩"二字——修前会走到候选判别
    甚至登记成功，修后必须在候选判别之前就被引句双锚定闸拒绝。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(ACCIDENT_MENG_KAOSHAN_CHAPTER)
    quote = "这里是北区杂役处，靠山宗不养废物，你二人既来到了此地，做半甲子杂役"
    # 先确认：旧声明闸（不含双锚定）确实会误放行——证明这道新闸补的是真实漏洞。
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"孟浩"}, "靠山宗", 2, quote,
    ) is True

    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"孟浩"}, "靠山宗", "孟浩", 2, quote, None, None,
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "quote_missing_dual_anchor"


ACCIDENT_XU_QING_CHAPTER = [
    {"idx": 1, "title": "第一章", "content": (
        "许清抬头看了一眼，淡淡道：靠山宗。"
    )},
]


def test_status_fact_rejected_when_quote_is_bare_org_xu_qing_shape() -> None:
    """端到端红灯（真实事故复刻 2）："许清→靠山宗"：引句只剩组织名三个字，什么都没
    锚定——同样必须在候选判别之前被拒绝。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(ACCIDENT_XU_QING_CHAPTER)
    quote = "靠山宗。"
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"许清"}, "靠山宗", 1, quote,
    ) is True

    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"许清"}, "靠山宗", "许清", 1, quote, None, None,
        fact_noun="势力归属", roster={"许清": ["许清"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "quote_missing_dual_anchor"


ACCIDENT_WANG_HAN_CHAPTER = [
    {"idx": 27, "title": "第二十七章", "content": (
        "王腾飞冷笑一声，转身离去，未再多言。"
        "\"孟浩，上官师叔寻你有事，你随我走一趟吧。\"韩宗看都不看其他人一眼，"
        "望着孟浩，冷淡开口。王腾飞在一旁冷眼旁观，并未说话，脸色阴沉。"
    )},
]


def test_status_fact_relation_rejected_when_quote_missing_subject_wang_han_shape() -> None:
    """端到端红灯（真实事故复刻 3，彻头彻尾的假事实）："王腾飞 同党/同门→韩宗"：
    章级共现闸通过（"王腾飞"在第 27 章确实出现多次），但被登记引用的这一句是韩宗对
    孟浩说话，句中根本没有王腾飞——必须被引句双锚定闸拒绝，不能让"章级共现"顶替
    "引句本身包含主体"。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(ACCIDENT_WANG_HAN_CHAPTER)
    quote = (
        "\"孟浩，上官师叔寻你有事，你随我走一趟吧。\"韩宗看都不看其他人一眼，"
        "望着孟浩，冷淡开口。"
    )
    assert stages._alias_declaration_verified(
        chapters_by_idx, {"王腾飞"}, "韩宗", 27, quote,
    ) is True

    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"王腾飞"}, "韩宗", "王腾飞", 27, quote, None, None,
        fact_noun="人物关系", roster={"王腾飞": ["王腾飞"], "韩宗": ["韩宗"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "quote_missing_dual_anchor"


def test_status_fact_accepted_when_quote_dual_anchored_positive_shape(monkeypatch) -> None:
    """正例（不得误伤）：主体与对象都在引句里的真实形状——李富贵与孟浩的友情关系，
    加闸前后都应当通过（不是这道闸要拒绝的对象）。"""
    from app import stages
    from app.harness import model_gateway

    chapters = [{"idx": 3, "title": "第三章", "content": (
        "李富贵拍了拍孟浩的肩膀：\"孟浩，你是我李富贵这一辈子的好朋友。\""
    )}]
    chapters_by_idx = stages._chapters_by_idx(chapters)
    quote = "孟浩，你是我李富贵这一辈子的好朋友。"
    roster = {"李富贵": ["李富贵"], "孟浩": ["孟浩"]}

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("李富贵"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"李富贵"}, "孟浩", "李富贵", 3, quote, None, None,
        fact_noun="人物关系", roster=roster,
    ))
    assert resolved["accepted"] is True
    assert resolved["chapter_idx"] == 3
    assert resolved["quote"] == quote


# ---------- 3d. 桥接取句双锚定优先级：`_alias_bridge_dual_anchor_quote` /
# `_find_alias_bridge_chapter`（事故修复：第四闸上线后状态事实产出率跌到个位数，主力
# 拒绝原因不是"申报为假"，是桥接取句选丢了主体——旧版只看分段里有没有对象，取全书第一个
# 含对象的分段，与被测主体是谁无关，随后必然被引句双锚定闸拒绝） ----------

BRIDGE_DUAL_ANCHOR_CHAPTER = [
    {"idx": 10, "title": "第十章", "content": (
        "靠山宗使者登门，王腾飞躬身相迎，态度恭敬。"
        "\n\n"
        "赵武刚上前一步，对着靠山宗躬身行礼，郑重表示愿意加入靠山宗。"
    )},
]


def test_bridge_quote_prefers_segment_with_subject_over_first_object_match() -> None:
    """红→绿纯函数验证：全书第一个含对象（靠山宗）的分段是王腾飞那一段，不含被测
    主体（赵武刚）；同一章后面还有一段同时含"靠山宗"与"赵武刚"。旧策略（仍保留、未删除
    的 `_alias_bridge_quote`，不看主体）会取到王腾飞那一段——这里先证明这一点确实成立
    （证明红灯场景真实存在，不是构造了个不可能出现的形状），再验证新入口
    `_find_alias_bridge_chapter` 取到的是双锚定那一段。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(BRIDGE_DUAL_ANCHOR_CHAPTER)
    content = chapters_by_idx[10]

    # 红：不看主体的旧取句函数（仍是回落路径，未删除）确实会选到不含被测主体的那一段。
    old_style_quote = stages._alias_bridge_quote(content, "靠山宗")
    assert old_style_quote is not None
    assert "王腾飞" in old_style_quote
    assert "赵武刚" not in old_style_quote

    # 绿：新入口优先选双锚定分段。
    resolved = stages._find_alias_bridge_chapter(chapters_by_idx, {"赵武刚"}, "靠山宗")
    assert resolved is not None
    resolved_chapter_index, resolved_quote = resolved
    assert resolved_chapter_index == 10
    assert "赵武刚" in resolved_quote
    assert "靠山宗" in resolved_quote
    assert resolved_quote in content  # 逐字子串，不是拼接


def test_bridge_quote_falls_back_to_first_object_match_when_no_dual_anchor_segment_exists() -> None:
    """反例（不得放宽共现要求）：全书不存在任何双锚定分段——章级仍然共现（"血妖宗"与
    "李四"各自都出现在本章），但没有哪一段同时包含两者。必须退回原有行为（第一个含对象
    的分段），不能因为取句策略改动就凭空找出一个双锚定分段。"""
    from app import stages

    chapters = [{"idx": 6, "title": "第六章", "content": (
        "血妖宗宗主在殿上训话，众弟子俯首听命。"
        "\n\n"
        "李四远远站在人群边缘，低头不语，未曾靠近。"
    )}]
    chapters_by_idx = stages._chapters_by_idx(chapters)

    resolved = stages._find_alias_bridge_chapter(chapters_by_idx, {"李四"}, "血妖宗")
    assert resolved is not None
    resolved_chapter_index, resolved_quote = resolved
    assert resolved_chapter_index == 6
    assert "李四" not in resolved_quote  # 确实退回了不含主体的原有取句结果
    assert resolved_quote == stages._alias_bridge_quote(chapters_by_idx[6], "血妖宗")

    # 端到端：这条退回的引句仍然会被第四闸拒绝，取句策略改动不得放行任何原本该拒绝的
    # 申报。
    end_to_end = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"李四"}, "血妖宗", "李四", 99,
        "编造的、原文里根本没有的一句话", None, None,
        fact_noun="势力归属", roster={"李四": ["李四"]},
    ))
    assert end_to_end["accepted"] is False
    assert end_to_end["reason"] == "quote_missing_dual_anchor"


def test_status_fact_accepted_end_to_end_when_bridge_quote_dual_anchored(monkeypatch) -> None:
    """端到端绿灯：模型申报的章节/引句没通过声明核验，代码退一步做桥接检索——桥接章内
    存在双锚定分段，取句策略修复后应当选中它、通过第四闸、候选判别裁决选中赵武刚本人，
    最终登记成功。"""
    from app import stages
    from app.harness import model_gateway

    chapters_by_idx = stages._chapters_by_idx(BRIDGE_DUAL_ANCHOR_CHAPTER)

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("赵武刚"),
    )
    resolved = asyncio.run(stages._status_fact_evidence_resolution(
        chapters_by_idx, {"赵武刚"}, "靠山宗", "赵武刚", 999,
        "编造的、原文里根本没有的一句话", None, None,
        fact_noun="势力归属", roster={"赵武刚": ["赵武刚"], "王腾飞": ["王腾飞"]},
    ))
    assert resolved["accepted"] is True
    assert resolved["chapter_idx"] == 10
    assert "赵武刚" in resolved["quote"]
    assert "靠山宗" in resolved["quote"]


# ---------- 4. backfill_character_status_facts：全书上下文一次性回填 ----------

def test_backfill_registers_verified_affiliation_and_relation_and_freezes_other_fields(
    monkeypatch,
) -> None:
    from app import stages
    from app.harness import model_gateway

    meng_hao = _character()
    wang_youcai = Character(
        name="王有材", role="重要配角", appearance_canonical=APPEARANCE,
        personality="", speech_style="",
    )
    snapshot = meng_hao.model_dump(exclude={"affiliations", "relations"})
    bible = Bible(
        characters=[meng_hao, wang_youcai],
        world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"),
    )
    chapters = AFFILIATION_CHAPTERS + RELATION_CHAPTERS

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "affiliations": [{
                "character_name": "孟浩", "org": "血妖宗", "relation_kind": "membership",
                "evidence_chapter_index": 5, "evidence_quote": "欢迎孟浩加入我血妖宗",
            }],
            "relations": [{
                "character_name": "孟浩", "to": "王有材", "relation_kind": "ally",
                "evidence_chapter_index": 8,
                "evidence_quote": "王有材与孟浩并肩作战，二人结为过命之交",
            }],
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("孟浩"),
    )
    added = asyncio.run(stages.backfill_character_status_facts(bible, chapters))

    assert added == {
        "affiliations": {"孟浩": ["血妖宗"]},
        "relations": {"孟浩": ["王有材"]},
    }
    assert [a.org for a in meng_hao.affiliations] == ["血妖宗"]
    assert meng_hao.affiliations[0].valid_from_chapter == 5
    assert meng_hao.affiliations[0].valid_to_chapter is None
    assert [r.to for r in meng_hao.relations] == ["王有材"]
    # 冻结其它一切既有字段：本函数只允许改写 affiliations/relations。
    assert meng_hao.model_dump(exclude={"affiliations", "relations"}) == snapshot


def test_backfill_is_idempotent_on_rerun(monkeypatch) -> None:
    """同一条归属重复申报（比如重跑回填）不应重复追加。"""
    from app import stages
    from app.harness import model_gateway

    character = _character(affiliations=[CharacterAffiliation(
        org="血妖宗", relation_kind="membership",
        evidence_chapter_index=5, evidence_quote="欢迎孟浩加入我血妖宗",
        valid_from_chapter=5,
    )])
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "affiliations": [{
                "character_name": "孟浩", "org": "血妖宗", "relation_kind": "membership",
                "evidence_chapter_index": 5, "evidence_quote": "欢迎孟浩加入我血妖宗",
            }],
            "relations": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    added = asyncio.run(stages.backfill_character_status_facts(bible, AFFILIATION_CHAPTERS))

    assert added == {"affiliations": {}, "relations": {}}
    assert len(bible.characters[0].affiliations) == 1


def test_backfill_ignores_relation_target_outside_roster_or_self(monkeypatch) -> None:
    """关系对象必须是人物谱里已有的另一个人：目标不在人物谱里、或目标就是角色本人，
    一律忽略，不发起任何裁决调用。"""
    from app import stages
    from app.harness import model_gateway

    character = _character()
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "affiliations": [],
            "relations": [
                {  # 目标不在人物谱里
                    "character_name": "孟浩", "to": "路人甲", "relation_kind": "stranger",
                    "evidence_chapter_index": 8, "evidence_quote": "路人甲随口一说",
                },
                {  # 目标是角色本人
                    "character_name": "孟浩", "to": "孟浩", "relation_kind": "self",
                    "evidence_chapter_index": 8, "evidence_quote": "孟浩自言自语",
                },
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    added = asyncio.run(stages.backfill_character_status_facts(bible, RELATION_CHAPTERS))

    assert added == {"affiliations": {}, "relations": {}}
    assert bible.characters[0].relations == []


def test_backfill_returns_empty_and_keeps_bible_on_model_failure(monkeypatch) -> None:
    from app import stages
    from app.harness import model_gateway

    character = _character()
    snapshot = character.model_dump()
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    async def failing_chat(_messages, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(model_gateway, "chat", failing_chat)
    added = asyncio.run(stages.backfill_character_status_facts(bible, AFFILIATION_CHAPTERS))

    assert added == {"affiliations": {}, "relations": {}}
    assert bible.characters[0].model_dump() == snapshot


def test_backfill_rejects_unverifiable_affiliation_via_candidate_mismatch(monkeypatch) -> None:
    """端到端红灯：模型申报的归属在候选判别裁决里被拒绝（选中了另一个候选人）——
    不登记，人物谱保持原样。"""
    from app import stages
    from app.harness import model_gateway

    meng_hao = _character()
    li_shiqi = Character(
        name="李诗琪", role="重要配角", appearance_canonical=APPEARANCE,
        personality="", speech_style="",
    )
    bible = Bible(
        characters=[meng_hao, li_shiqi],
        world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"),
    )

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "affiliations": [{
                "character_name": "孟浩", "org": "血妖宗", "relation_kind": "membership",
                "evidence_chapter_index": 12,
                "evidence_quote": "血妖宗的李诗琪缓步上前，欲替孟浩说情",
            }],
            "relations": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("李诗琪"),
    )
    added = asyncio.run(stages.backfill_character_status_facts(
        bible, CANDIDATE_CONFUSION_CHAPTER,
    ))

    assert added == {"affiliations": {}, "relations": {}}
    assert meng_hao.affiliations == []


def test_backfill_prompt_explains_interval_and_no_fabrication_rules(monkeypatch) -> None:
    """提示词必须讲清楚：不确定就不要申报有效区间边界，也不要编造/改写引句——与别名
    回填提示词的纪律一致（tests/test_character_alias.py 对应用例）。"""
    from app import stages
    from app.harness import model_gateway

    character = _character()
    bible = Bible(characters=[character], world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"))

    seen: dict[str, object] = {}

    async def fake_chat(messages, **_kwargs):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps({"affiliations": [], "relations": []}, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    asyncio.run(stages.backfill_character_status_facts(bible, AFFILIATION_CHAPTERS))

    prompt = str(seen["prompt"])
    assert "不确定就不要填这个字段" in prompt
    assert "不要自己" in prompt and "加引号包裹" in prompt
    assert "不确定就不要申报" in prompt
