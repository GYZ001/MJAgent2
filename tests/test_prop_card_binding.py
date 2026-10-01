"""道具对既有卡片的解析（2026-09-30 派单）：道具此前是三类资产里唯一不对照
世界书既有卡片的一类——原文「行李箱」被映射台记成新标签「旧行李箱」，不对应
第1集已有卡片「行李箱」（alias「水泡坏的行李箱」，有参考图），分镜台按精确
label 查外观/参考图都查不到，只能让模型自编。本文件钉住四处修复：

1. ``app.props.card_match.match_existing_prop_card``——唯一胜者判据本身（唯一
   实现，``app.production.prep_pack.discovery`` 与 ``app.props.service`` 共用）。
2. ``app.production.prep_pack.discovery._prep_pack_build_prop_manifest``——清单
   构建阶段对照既有卡片绑定 ``canonical_name``，provenance 自校验必须仍然通过。
3. ``app.props.service.ensure_props_for_labels``——登记阶段命中既有卡就不新建、
   别名登记幂等。
4. ``app.production.storyboard_prop_assets``/``storyboard_prop_appearance_lock``——
   分镜台消费侧按 ``canonical_name`` 查外观/参考图/核验卡片。

以及抽取提示词新增 ``known_props``（仅供拼写对齐，话术同
``known_characters``/``known_scenes``）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_conn, now
from app.production.prep_pack import chunk_extraction as ce
from app.production.prep_pack.prop_manifest import _prep_pack_build_prop_manifest
from app.production.prep_pack.provenance_repair import verify_manifest_provenance_with_repair
from app.production.storyboard_prop_appearance_lock import known_prop_card_appearance_index
from app.production import storyboard_prop_assets as prop_assets
from app.props.card_match import evidence_text_for_segments, match_existing_prop_card
from app.props import judge, service
from app.schemas import Prop
from app.source_excerpt import index_source_segments


def _card(name: str, aliases: list[str] | None = None) -> Prop:
    return Prop(name=name, appearance_canonical=f"{name}的标准外观描述", aliases=aliases or [])


# ---------------------------------------------------------------------------
# match_existing_prop_card：唯一胜者判据本身
# ---------------------------------------------------------------------------

def test_match_returns_none_without_cards_or_evidence() -> None:
    assert match_existing_prop_card("旧行李箱", "随便什么原文", [], cards_with_prior_evidence=frozenset()) is None
    assert match_existing_prop_card("旧行李箱", "", [_card("行李箱")], cards_with_prior_evidence=frozenset()) is None
    assert match_existing_prop_card("", "行李箱在地上", [_card("行李箱")], cards_with_prior_evidence=frozenset()) is None


def test_match_binds_via_containment_card_name_is_substring_of_label() -> None:
    card = _card("行李箱", aliases=["水泡坏的行李箱"])
    matched = match_existing_prop_card(
        "旧行李箱", "她拖着那只旧行李箱走进来。", [card], cards_with_prior_evidence=frozenset(),
    )
    assert matched is card


def test_match_binds_via_containment_label_is_substring_of_card_name() -> None:
    card = _card("小木星星")
    matched = match_existing_prop_card(
        "木星星", "桌角摆着一枚小木星星，散发微光。", [card], cards_with_prior_evidence=frozenset(),
    )
    assert matched is card


def test_match_declines_when_two_cards_both_qualify_and_logs_signal(caplog: pytest.LogCaptureFixture) -> None:
    """两卡同时命中：结构上无法唯一裁决，宁可不绑，只留可见信号。"""
    cards = [_card("凝灵丹"), _card("灵石")]
    with caplog.at_level("WARNING"):
        matched = match_existing_prop_card(
            "凝灵丹与半块灵石", "桌上摆着凝灵丹与半块灵石，两件宝物一同发亮。", cards,
            cards_with_prior_evidence=frozenset(),
        )
    assert matched is None
    assert "PROP_CARD_MATCH_AMBIGUOUS" in caplog.text
    assert "凝灵丹" in caplog.text and "灵石" in caplog.text


def test_match_returns_none_when_no_identifier_hits_evidence() -> None:
    card = _card("行李箱")
    assert match_existing_prop_card(
        "背包", "她背着一个帆布背包。", [card], cards_with_prior_evidence=frozenset(),
    ) is None


def test_match_returns_none_when_evidence_hit_has_no_containment_with_label() -> None:
    """卡片的 name 确实逐字出现在原文里，但与这次的 label 没有包含关系——不绑。"""
    card = _card("行李箱")
    assert match_existing_prop_card(
        "背包", "地上放着一只行李箱。", [card], cards_with_prior_evidence=frozenset(),
    ) is None


# ---------------------------------------------------------------------------
# evidence_text_for_segments：discovery.py 与 service.py 共用的窄证据计算，
# 证据面必须只落在 mention 自己声明的段落上，不能悄悄扩大
# ---------------------------------------------------------------------------

def test_evidence_text_for_segments_excludes_unrelated_segment() -> None:
    segments = index_source_segments("第一段无关内容。\n\n第二段才是这条提及自己的段落。")
    text = evidence_text_for_segments(segments, [2])
    assert "第二段" in text
    assert "第一段" not in text


def test_evidence_text_for_segments_out_of_range_indexes_yield_empty_not_full_text() -> None:
    """下标越界时证据为空字符串，不回退到拼接全部段落——回退等于重新放开
    本模块要堵住的宽证据漏洞。"""
    segments = index_source_segments("唯一一段原文。")
    assert evidence_text_for_segments(segments, [2, 9]) == ""
    assert evidence_text_for_segments(segments, []) == ""


# ---------------------------------------------------------------------------
# _prep_pack_build_prop_manifest：清单构建阶段绑定 canonical_name
# ---------------------------------------------------------------------------

def _mention(label: str, segment_indexes: list[int], **overrides) -> dict:
    base = {
        "label": label, "description": "一件道具", "segment_indexes": segment_indexes,
        "plot_significant": False, "plot_significant_quote": "",
    }
    base.update(overrides)
    return base


def test_manifest_binds_old_label_to_existing_card() -> None:
    """「旧行李箱」→绑定既有卡「行李箱」（真实回归场景）。"""
    segments = index_source_segments("她拖着那只旧行李箱，深一脚浅一脚地往前走。")
    cards = [_card("行李箱", aliases=["水泡坏的行李箱"])]
    props = _prep_pack_build_prop_manifest(
        [_mention("旧行李箱", [1])], segments, cards=cards,
    )
    assert len(props) == 1
    assert props[0]["label"] == "旧行李箱"
    assert props[0]["canonical_name"] == "行李箱"
    assert props[0]["provenance"]["anchor_phrase"]


def test_manifest_binds_short_label_to_longer_card_name() -> None:
    """「木星星」+ 卡名「小木星星」→绑定。"""
    segments = index_source_segments("桌角摆着一枚小木星星，散发微光。")
    cards = [_card("小木星星")]
    props = _prep_pack_build_prop_manifest(
        [_mention("木星星", [1])], segments, cards=cards,
    )
    assert len(props) == 1
    assert props[0]["canonical_name"] == "小木星星"


def test_manifest_card_match_branch_anchors_within_its_own_segment() -> None:
    """label 本身在它声明的段落里不逐字命中，但该段落另有一个与 label 存在
    包含关系的卡片 identifier——method="card_match"，anchor_phrase 必须真的
    落在 anchor_segments 指向的那一段原文里（自校验判据）。"""
    segments = index_source_segments("画面里那只行李箱边角磕碰，锁扣生锈。")
    cards = [_card("行李箱")]
    props = _prep_pack_build_prop_manifest(
        [_mention("那只旧行李箱", [1])], segments, cards=cards,
    )
    assert len(props) == 1
    entry = props[0]
    assert entry["canonical_name"] == "行李箱"
    provenance = entry["provenance"]
    assert provenance["method"] == "card_match"
    anchor_segment = provenance["anchor_segments"][0]
    assert provenance["anchor_phrase"] in segments[anchor_segment - 1].text


def test_manifest_provenance_self_check_passes_for_card_match_entry() -> None:
    """card_match 绑定的条目必须能通过 verify_manifest_provenance_with_repair
    的 anchor_phrase 自校验，不能引入"来源证明自校验失败"的门禁拦截。"""
    segments = index_source_segments("画面里那只行李箱边角磕碰，锁扣生锈。")
    cards = [_card("行李箱")]
    props = _prep_pack_build_prop_manifest(
        [_mention("那只旧行李箱", [1])], segments, cards=cards,
    )
    errors = verify_manifest_provenance_with_repair(
        segments, {"characters": [], "scenes": [], "functional_extras": [], "props": props}, "",
    )
    assert errors == []


def test_manifest_two_cards_both_match_falls_back_to_raw_label() -> None:
    """两卡同时命中：不绑 canonical_name，label 本身逐字命中时仍正常发布
    （不是整条丢弃——道具本身确实在画面里出场，只是归属存疑）。"""
    segments = index_source_segments("桌上摆着凝灵丹与半块灵石，两件宝物一同发亮。")
    cards = [_card("凝灵丹"), _card("灵石")]
    props = _prep_pack_build_prop_manifest(
        [_mention("凝灵丹与半块灵石", [1])], segments, cards=cards,
    )
    assert len(props) == 1
    assert props[0]["canonical_name"] is None
    assert props[0]["label"] == "凝灵丹与半块灵石"


def test_manifest_without_cards_behaves_exactly_as_before() -> None:
    """不传 cards（默认 ``()``）时行为与旧版逐字 label 去重完全一致——向后兼容
    既有调用点（tests/test_prep_pack_prop_manifest_merge.py 不传这个参数）。"""
    segments = index_source_segments("旧猫包摆在桌上。")
    props = _prep_pack_build_prop_manifest([_mention("旧猫包", [1])], segments)
    assert len(props) == 1
    assert props[0]["canonical_name"] is None
    assert props[0]["label"] == "旧猫包"


# ---------------------------------------------------------------------------
# ensure_props_for_labels：登记阶段命中既有卡不新建 + 别名登记幂等
# ---------------------------------------------------------------------------

def _seed_project(project_id: str, props_list: list[dict]) -> None:
    bible = {
        "characters": [], "scenes": [], "props": props_list,
        "world": {"era": "", "genre": "", "visual_style_canonical": "国漫电影风"},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), now()),
    )
    conn.commit()


async def _explode_chat_structured(*_a, **_k):
    raise AssertionError("命中既有卡片就不该再发起模型调用")


async def test_ensure_props_for_labels_binds_existing_card_instead_of_creating_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_project("p1", [{"name": "行李箱", "appearance_canonical": "24寸卡其色硬壳拉杆箱", "aliases": []}])
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _explode_chat_structured)
    # segment_indexes=[1, 2]：真实对应 source_text 的两段（index_source_segments
    # 按空行切分）——既满足 is_key_prop_mention 的"跨 ≥2 段"结构判据，又能让
    # 喂给 match_existing_prop_card 的窄证据（见 service.ensure_props_for_labels
    # docstring）落在这条提及自己声明、且真实包含它的段落上。
    mentions = [{
        "label": "旧行李箱", "description": "旧行李箱", "segment_indexes": [1, 2],
    }]
    source_text = "她拖着那只旧行李箱，深一脚浅一脚地往前走。\n\n箱子轮子卡在石缝里，她费力地拽了几下。"

    result = await service.ensure_props_for_labels(
        "p1", 3, mentions, source_text=source_text, cards_with_prior_evidence=frozenset(),
    )

    assert result == {"added": [], "errors": []}
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert [p["name"] for p in bible["props"]] == ["行李箱"], "不该建出第二张卡"
    assert "旧行李箱" in bible["props"][0]["aliases"]


async def test_ensure_props_for_labels_alias_registration_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_project("p1", [{"name": "行李箱", "appearance_canonical": "24寸卡其色硬壳拉杆箱", "aliases": []}])
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _explode_chat_structured)
    mentions = [{"label": "旧行李箱", "description": "旧行李箱", "segment_indexes": [1, 2]}]
    source_text = "她拖着那只旧行李箱，深一脚浅一脚地往前走。\n\n箱子轮子卡在石缝里，她费力地拽了几下。"

    await service.ensure_props_for_labels(
        "p1", 3, mentions, source_text=source_text, cards_with_prior_evidence=frozenset(),
    )
    await service.ensure_props_for_labels(
        "p1", 4, mentions, source_text=source_text, cards_with_prior_evidence=frozenset(),
    )

    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert bible["props"][0]["aliases"].count("旧行李箱") == 1


async def test_ensure_props_for_labels_two_cards_ambiguous_falls_back_to_new_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两卡同时命中、不唯一时不绑既有卡——原有"逐字命中/结构判据够格就新建"
    的既定行为继续生效，不因新判据而漏登记，也不会被随意绑到两者之一。"""
    _seed_project("p1", [
        {"name": "剑", "appearance_canonical": "朴素铁剑", "aliases": []},
        {"name": "长剑", "appearance_canonical": "泛着寒光的长剑", "aliases": []},
    ])

    async def _fake_chat_structured(_messages, **_kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(appearance_canonical="剑鞘通体乌木，镶着一圈银边", aliases=[])

    async def _fake_generate_image(project_id, name, _prompt):
        return f"/fake/{project_id}/{name}.png"

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    mentions = [{
        "label": "长剑鞘", "description": "剑鞘通体乌木，镶着一圈银边", "segment_indexes": [1, 2],
    }]
    source_text = "她摘下那把长剑鞘，随手放在桌上。\n\n剑鞘入手微凉，边缘还留着几道划痕。"

    result = await service.ensure_props_for_labels(
        "p1", 6, mentions, source_text=source_text, cards_with_prior_evidence=frozenset(),
    )

    assert [item["name"] for item in result["added"]] == ["长剑鞘"], "不能被随意绑到「剑」或「长剑」任一方"
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert {p["name"] for p in bible["props"]} == {"剑", "长剑", "长剑鞘"}


async def test_ensure_props_for_labels_ignores_unrelated_card_mentioned_outside_own_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-30 修复评审 blocking 问题复现：早先这里传整集 source_text 给
    match_existing_prop_card，只要既有卡片的 name/alias 恰好出现在本集任意
    无关段落、且与当前 label 存在包含关系，就会被当成"证据"命中，把两个不
    相关的道具静默合并成一张卡。「老式水晶球」是第1段博物馆展品的卡，第2段
    桌上把玩的「水晶球」是完全不相关的新道具——mention 自己只声明了第2段，
    绑定判据必须只看第2段，不能被第1段里恰好出现的「老式水晶球」污染。"""
    _seed_project("p1", [
        {"name": "老式水晶球", "appearance_canonical": "博物馆展出的老式水晶球", "aliases": []},
    ])

    async def _fake_chat_structured(_messages, **_kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(appearance_canonical="她桌上那枚圆润的水晶球", aliases=[])

    async def _fake_generate_image(project_id, name, _prompt):
        return f"/fake/{project_id}/{name}.png"

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    seg1 = "博物馆橱窗里陈列着一只老式水晶球，玻璃罩落满灰尘。"
    seg2 = "她从背包里取出一枚圆润的水晶球，放在桌上对着烛光端详。"
    source_text = seg1 + "\n\n" + seg2
    mentions = [{"label": "水晶球", "description": "她取出的水晶球", "segment_indexes": [2]}]

    result = await service.ensure_props_for_labels(
        "p1", 3, mentions, source_text=source_text, cards_with_prior_evidence=frozenset(),
    )

    assert [item["name"] for item in result["added"]] == ["水晶球"], "必须新建独立卡，不能被无关段落的既有卡污染"
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert {p["name"] for p in bible["props"]} == {"老式水晶球", "水晶球"}


async def test_ensure_props_for_labels_evidence_scope_matches_discovery_for_ambiguous_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-30 修复评审 major 问题复现：discovery.py 用窄证据（mention 自己
    的段落）唯一判定绑「行李箱」，但 service.py 若用整集 source_text 反而会
    因为第1段里无关的「旧行李箱三件套」一起命中而判成两卡歧义，退化成新建
    重复卡「旧行李箱」。两个调用点的证据面必须对齐，结论才能一致。"""
    _seed_project("p1", [
        {"name": "行李箱", "appearance_canonical": "24寸卡其色硬壳拉杆箱", "aliases": []},
        {"name": "旧行李箱三件套", "appearance_canonical": "一套做旧的三件式行李箱", "aliases": []},
    ])
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _explode_chat_structured)
    seg1 = "柜子里放着一套旧行李箱三件套，落满灰尘。"
    seg2 = "她拖着那只旧行李箱，深一脚浅一脚地往前走。"
    source_text = seg1 + "\n\n" + seg2
    mentions = [{"label": "旧行李箱", "description": "旧行李箱", "segment_indexes": [2]}]

    result = await service.ensure_props_for_labels(
        "p1", 3, mentions, source_text=source_text, cards_with_prior_evidence=frozenset(),
    )

    assert result == {"added": [], "errors": []}, "必须绑既有卡「行李箱」，不能因为第1段的无关卡而判歧义新建"
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert {p["name"] for p in bible["props"]} == {"行李箱", "旧行李箱三件套"}, "不该多出第三张重复卡"
    assert "旧行李箱" in next(p for p in bible["props"] if p["name"] == "行李箱")["aliases"]


# ---------------------------------------------------------------------------
# storyboard_prop_assets：分镜台消费侧按 canonical_name 查外观/参考图
# ---------------------------------------------------------------------------

class _FakeProp:
    def __init__(self, name: str, appearance_canonical: str, aliases: list[str] | None = None) -> None:
        self.name = name
        self.appearance_canonical = appearance_canonical
        self.aliases = aliases or []


class _FakeBible:
    def __init__(self, props: list[_FakeProp]) -> None:
        self.props = props
        self.characters = []
        self.scenes = []


def test_enrich_prop_manifest_entries_uses_canonical_name_when_label_unmatched() -> None:
    """label「旧行李箱」按字面查不到卡片，但 canonical_name「行李箱」能查到——
    必须拿到卡片的真实外观，不是占位说明文字。"""
    manifest = {
        "characters": [], "scenes": [], "functional_extras": [],
        "props": [{
            "label": "旧行李箱", "canonical_name": "行李箱",
            "description": "一只旧行李箱", "segment_indexes": [1],
        }],
    }
    bible = _FakeBible([_FakeProp("行李箱", "24寸卡其色硬壳拉杆箱，边角磕碰。")])
    prop_assets.enrich_prop_manifest_entries(None, manifest, bible=bible)
    assert manifest["props"][0]["appearance"] == "24寸卡其色硬壳拉杆箱，边角磕碰。"


def test_enrich_prop_manifest_entries_ref_image_lookup_uses_canonical_name(monkeypatch) -> None:
    captured: list[str] = []

    def _fake_lookup(conn, project_id, name, episode_no):
        captured.append(name)
        return {"status": "ready", "image_path": "/tmp/does-not-matter.jpg", "appearance": "x"}

    monkeypatch.setattr(prop_assets, "_prop_reference_lookup", _fake_lookup)
    monkeypatch.setattr(prop_assets.Path, "is_file", lambda self: True)
    manifest = {
        "characters": [], "scenes": [], "functional_extras": [],
        "props": [{
            "label": "旧行李箱", "canonical_name": "行李箱",
            "description": "一只旧行李箱", "segment_indexes": [1],
        }],
    }
    prop_assets.enrich_prop_manifest_entries(
        object(), manifest, bible=None, project_id="proj-1", episode_no=3,
    )
    assert captured == ["行李箱"], "必须按规范卡名查参考图，不是原始 label"
    assert manifest["props"][0]["ref_image_path"] == "/tmp/does-not-matter.jpg"


def test_enrich_prop_manifest_entries_falls_back_to_label_without_canonical_name() -> None:
    """既有行为不能回退：没有 canonical_name 字段时仍按原始 label 查（未绑定
    到既有卡片的道具，包括老版本没有这个字段的历史数据）。"""
    manifest = {
        "characters": [], "scenes": [], "functional_extras": [],
        "props": [{"label": "旧猫包", "description": "破猫包", "segment_indexes": [1]}],
    }
    bible = _FakeBible([_FakeProp("旧猫包", "灰色网状帆布猫包。")])
    prop_assets.enrich_prop_manifest_entries(None, manifest, bible=bible)
    assert manifest["props"][0]["appearance"] == "灰色网状帆布猫包。"


# ---------------------------------------------------------------------------
# storyboard_prop_appearance_lock：known_prop_card_appearance_index 双键收录
# ---------------------------------------------------------------------------

def test_card_appearance_index_collects_both_label_and_canonical_name() -> None:
    payload = {
        "asset_manifest": {
            "props": [{
                "label": "旧行李箱", "canonical_name": "行李箱",
                "segment_indexes": [1], "appearance": "24寸卡其色硬壳拉杆箱",
            }],
        },
    }
    index = known_prop_card_appearance_index(payload)
    assert index == {"旧行李箱": "24寸卡其色硬壳拉杆箱", "行李箱": "24寸卡其色硬壳拉杆箱"}


def test_card_appearance_index_without_canonical_name_only_has_label_key() -> None:
    payload = {
        "asset_manifest": {
            "props": [{"label": "水泡坏的行李箱", "segment_indexes": [31], "appearance": "24寸……"}],
        },
    }
    assert known_prop_card_appearance_index(payload) == {"水泡坏的行李箱": "24寸……"}


# ---------------------------------------------------------------------------
# chunk_extraction：抽取提示词新增 known_props（仅供拼写对齐）
# ---------------------------------------------------------------------------

def test_extract_chunk_prompt_includes_known_props() -> None:
    from app.source_excerpt import SourceSegment

    seen: dict[str, str] = {}

    async def fake_call(**kwargs):
        seen["prompt"] = kwargs.get("prompt") or ""
        raise _Captured

    original = ce._call_structured
    ce._call_structured = fake_call
    try:
        segment = SourceSegment(
            segment_id="s1", start_offset=0, end_offset=10, text="她拖着旧行李箱走进来。",
        )
        with pytest.raises(_Captured):
            asyncio.run(ce._extract_chunk(
                chunk=[(1, segment)], known_characters=[], known_scenes=[],
                known_props=["行李箱", "小木星星"], attempt_hint="",
                run_id=None, episode_id="ep1", episode_no=1, chunk_index=0,
            ))
    finally:
        ce._call_structured = original

    assert "行李箱" in seen["prompt"]
    assert "小木星星" in seen["prompt"]
    assert "仅供拼写对齐" in seen["prompt"]


class _Captured(Exception):
    """只用来把控制权从被测协程里拿回来，不代表失败。"""
