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
    （`app.stages._AliasVerdictResponse`）在状态事实回填里被直接复用，不需要另造。"""

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
    已失效）——与 character_portraits 表 ep_end IS NULL 惯例同构。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", 5, None, None,
    )
    assert result == (5, None)


def test_interval_start_accepted_when_boundary_chapter_has_support() -> None:
    """valid_from_chapter 与核心证据章不同，但申报的起点章节本身也能找到
    claim_text + anchor 共现——应当被接受（这里起点就等于核心证据章本身，
    最简单的"有支撑"场景）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", 40, 5, None,
    )
    # 起点 5 章：与核心证据章（40）不同，但第 5 章确实有"孟浩"+"血妖宗"共现支撑。
    assert result == (5, None)


def test_interval_end_accepted_when_boundary_chapter_has_support() -> None:
    """valid_to_chapter 申报为第 40 章（叛出宗门），该章确实有"孟浩"+"血妖宗"共现
    支撑（原文明确交代归属结束）——应当被接受。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", 5, None, 40,
    )
    assert result == (5, 40)


def test_interval_start_rejected_without_boundary_support() -> None:
    """红灯：valid_from_chapter 申报了一个不存在共现支撑的章节（第 9 章只有"孟浩"，
    没有"血妖宗"）——不能让模型随口给个区间，必须拒绝（返回 None）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", 5, 9, None,
    )
    assert result is None


def test_interval_end_rejected_without_boundary_support() -> None:
    """红灯：valid_to_chapter 同样没有共现支撑时必须拒绝。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", 5, None, 9,
    )
    assert result is None


def test_interval_start_rejected_when_declared_after_evidence_chapter() -> None:
    """红灯：起点不能晚于核心证据章——核心证据本身已经证明该事实在证据章成立，
    申报一个更晚的起点与此矛盾。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", 5, 40, None,
    )
    assert result is None


def test_interval_end_rejected_when_declared_before_evidence_chapter() -> None:
    """红灯：终点不能早于核心证据章，同一理由的镜像场景。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", 40, None, 5,
    )
    assert result is None


def test_interval_boundary_equal_to_evidence_chapter_is_trivially_accepted() -> None:
    """起止章申报为与核心证据章相同的值——不需要额外核验（本身就是已核验的证据点）。"""
    from app import stages

    chapters_by_idx = stages._chapters_by_idx(AFFILIATION_CHAPTERS)
    result = stages._status_fact_interval_resolution(
        chapters_by_idx, {"孟浩"}, "血妖宗", 5, 5, 5,
    )
    assert result == (5, 5)


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
    assert resolved["valid_to_chapter"] is None


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


def test_status_fact_rejected_when_declared_interval_has_no_support(monkeypatch) -> None:
    """红灯：核心证据本身可核验（候选判别通过），但申报的有效区间起点没有证据支撑
    （第 9 章只有孟浩，没有血妖宗）——即使核心事实成立，也必须整体拒绝，不做"部分
    采信、自动收窄区间"。"""
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
        9,  # declared_valid_from_chapter：第 9 章没有"血妖宗"，无支撑
        None,
        fact_noun="势力归属", roster={"孟浩": ["孟浩"]},
    ))
    assert resolved["accepted"] is False
    assert resolved["reason"] == "interval_unverified"


def test_status_fact_accepted_with_verified_interval_boundaries(monkeypatch) -> None:
    """绿灯：核心证据 + 起止章都能各自找到共现支撑 → 全部登记，区间与锚点正确。"""
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
    assert resolved["valid_to_chapter"] == 40


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
