"""道具/场景「原文字面称呼」通道：source_wording（2026-09-30 派单）。

真实证据（proj_ca86b15ab7d7 EP2，ep_7623b7b0a49a）：模型给的 display_name/
label 经常是概括/省略/规范化写法（场景「回民街巷子」、道具「小木星星」/
「缠银丝木簪」），原文却只在别处写了更短或不同的字面（"回民街"、"木星星"、
"缠着细银丝的木簪"）。既有的逐字核验（quote/label 本身必须逐字出现，这条
不许放松）因此把这些真实存在的场景/道具当成"没有本集依据"拒掉——道具侧
直接静默 ``continue`` 丢弃，没有任何日志或可见记录；场景侧因三路候选
（canonical_scene_name/name/quote）都不是连续原文字面而判未解析。

修法（本次改动，见各自模块 docstring）：``_ModelSceneMention``/
``_ModelPropMention`` 新增必填字段 ``source_wording``——模型把"这个地点/
道具在原文里的称呼"单独逐字交出来，与 display_name/label 分开，核验仍然
100% 逐字，只是多了一条候选。场景侧经
``asset_lookup._prep_pack_group_scene_quotes_by_canonical`` 并入锚点候选池；
道具侧 ``discovery._prep_pack_prop_mention_binding`` 与
``app.props.card_match.match_existing_prop_card`` 各自新增一条判据分支。
核验不过时道具提及进 ``asset_manifest.unanchored_prop_mentions`` 可见记录
并打日志，不再静默丢弃；场景侧沿用既有 ``degrade_unresolved_scene`` 降级
记录，本文件不重复覆盖。

本文件只钉住 source_wording 这条新增通道本身；既有的 label/quote 逐字核验、
card_match 唯一胜者规则、场景两遍解析等已有 tests/test_prop_card_binding.py
/tests/test_prep_pack_asset_discovery.py 覆盖，不在这里重复。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app import scenes
from app.db import get_conn, now
from app.production import prep_pack
from app.production.prep_pack.discovery import _prep_pack_build_prop_manifest
from app.props import judge, service
from app.schemas import Prop
from app.source_excerpt import index_source_segments
from tests.conftest import patch_prep_pack_everywhere


@pytest.fixture(autouse=True)
def _stub_narration_appellations(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件不测试 WS2-A 叙述向称谓归属——不桩会触发真实模型调用，同
    tests/test_prep_pack_asset_discovery.py 的既有处置。"""
    patch_prep_pack_everywhere(
        monkeypatch, "resolve_narration_appellations", lambda *a, **k: asyncio.sleep(0),
    )


def _card(name: str, aliases: list[str] | None = None) -> Prop:
    return Prop(name=name, appearance_canonical=f"{name}的标准外观描述", aliases=aliases or [])


def _mention(label: str, segment_indexes: list[int], **overrides) -> dict:
    base = {
        "label": label, "description": "一件道具", "segment_indexes": segment_indexes,
        "plot_significant": False, "plot_significant_quote": "", "source_wording": "",
        "known_prop_name": "",
    }
    base.update(overrides)
    return base


def _make_conn() -> sqlite3.Connection:
    """最小 _resolve_assets 夹具，同 tests/test_prep_pack_asset_discovery.py
    的 _make_conn 同一张表结构，只在本文件需要跑完整 _resolve_assets（④）
    时使用。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.execute(
        "CREATE TABLE scene_references(id TEXT, project_id TEXT, scene_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.execute(
        "CREATE TABLE episodes(id TEXT, project_id TEXT, episode_no INTEGER, "
        "source_chapters TEXT, screenplay_json TEXT)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute(
        "INSERT INTO projects(id, bible_json) VALUES ('p1', ?)",
        (json.dumps({
            "characters": [], "scenes": [],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()
    return conn


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


# ---------------------------------------------------------------------------
# ①②③ discovery._prep_pack_build_prop_manifest：道具侧 source_wording 通道
# ---------------------------------------------------------------------------

def test_label_equals_card_name_but_only_source_wording_is_literal() -> None:
    """①真实案例：label「小木星星」恰与既有卡同名，但原文只写了「木星星」——
    label 本身不逐字出现，旧逻辑会判定「无字面依据」；模型经 known_prop_name
    明确提名「小木星星」、且 source_wording「木星星」逐字出现在证据里（提名
    核验通过），必须绑定到既有卡，anchor 用真正逐字命中的那个字面（木星星），
    不是模型概括出的 label（主会话复核后改为「模型提名、代码核验」，纯包含
    关系不再单独作为判据，见 app.props.card_match 模块 docstring）。"""
    segments = index_source_segments("手心攥着那枚旧旧的木星星。")
    cards = [_card("小木星星")]
    props = _prep_pack_build_prop_manifest(
        [_mention("小木星星", [1], source_wording="木星星", known_prop_name="小木星星")],
        segments, cards=cards,
    )
    assert len(props) == 1
    entry = props[0]
    assert entry["label"] == "小木星星"
    assert entry["canonical_name"] == "小木星星"
    assert entry["provenance"]["method"] == "direct"
    assert entry["provenance"]["anchor_phrase"] == "木星星"
    assert entry["provenance"]["anchor_segments"] == [1]


def test_prop_anchored_via_source_wording_when_label_is_a_paraphrase() -> None:
    """②真实案例：原文「一支缠着细银丝的木簪」，label 被模型概括成
    「缠银丝木簪」（不逐字），source_wording 原样摘录「缠着细银丝的木簪」
    （逐字）——没有既有卡可绑，但这条提及本身不得被丢弃，必须凭
    source_wording 的逐字锚点正常发布。"""
    segments = index_source_segments("她鬓边斜插着一支缠着细银丝的木簪。")
    mention = _mention("缠银丝木簪", [1], source_wording="缠着细银丝的木簪")
    unanchored: list[dict] = []
    props = _prep_pack_build_prop_manifest([mention], segments, cards=[], unanchored=unanchored)
    assert len(props) == 1
    entry = props[0]
    assert entry["label"] == "缠银丝木簪"
    assert entry["canonical_name"] is None, "没有既有卡可绑，仍按新道具正常发布"
    assert entry["provenance"]["method"] == "direct"
    assert entry["provenance"]["anchor_phrase"] == "缠着细银丝的木簪"
    assert unanchored == [], "锚定成功的提及不进 unanchored"


def test_prop_rescued_via_trailing_anchor_when_source_wording_not_literal() -> None:
    """③（2026-10-01 PREP_PACK_VERSION 2.0.10 起不再是这个结果）
    source_wording「妈妈写的字条」本身不是原文字面，但尾部退让到「字条」
    （退到 2 字，同 app/production/scene_evidence.py::scene_label_evidence
    同一口径）在声明段落里逐字命中——这条提及不再判未锚定，而是按退让后的
    字面重新锚定，provenance 打 trailing_anchor 标记（见
    tests/test_prep_pack_trailing_anchor.py 的完整覆盖；本文件只钉住
    source_wording 这条通道本身不受影响——仍然是触发锚定的候选来源）。"""
    segments = index_source_segments("桌上放着一张字条。")
    mention = _mention("妈妈的字条", [1], source_wording="妈妈写的字条")
    unanchored: list[dict] = []
    props = _prep_pack_build_prop_manifest(
        [mention], segments, cards=[], unanchored=unanchored,
    )
    assert len(props) == 1
    entry = props[0]
    assert entry["provenance"]["method"] == "direct"
    assert entry["provenance"]["anchor_phrase"] == "字条"
    assert entry["provenance"]["trailing_anchor"] is True
    assert unanchored == [], "尾部退让命中后不再进 unanchored"


def test_source_wording_stitched_from_nonadjacent_text_is_not_anchored() -> None:
    """⑤反向验证——fail-closed 不因新增候选而放松：source_wording 把原文里
    两处真实存在、但并不相邻的文字拼接在一起，中间跳过了字，不是连续原文，
    依旧不得被当成合法锚点，必须落进 unanchored。"""
    segments = index_source_segments("手心攥着那枚旧旧的木星星，又摸到了口袋里那支断了尖的铅笔。")
    stitched = "旧旧的木星星断了尖的铅笔"  # 中间跳过了「，又摸到了口袋里那支」
    mention = _mention("神秘小物件", [1], source_wording=stitched)
    unanchored: list[dict] = []
    props = _prep_pack_build_prop_manifest(
        [mention], segments, cards=[], unanchored=unanchored,
    )
    assert props == [], "拼接字符串不是原文连续字面，不得被当成有效锚点发布"
    assert len(unanchored) == 1
    assert unanchored[0]["label"] == "神秘小物件"


# ---------------------------------------------------------------------------
# ④ resolve_assets.py 场景锚点候选表：source_wording 经
#    asset_lookup._prep_pack_group_scene_quotes_by_canonical 并入候选池
# ---------------------------------------------------------------------------

def test_scene_source_wording_anchors_newly_discovered_scene(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """④真实案例：场景「回民街巷子」是本集新发现的地点，模型这次的 quote
    留空（真实现场三路候选全灭），但 source_wording「回民街」逐字出现在
    第2段——必须成功锚定，不再判未解析。"""
    conn = _make_conn()

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels, evidence=None):
        assert labels == ["回民街巷子"]
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
            "VALUES ('sr-hms','p1','回民街巷子',2,NULL)"
        )
        conn.commit()
        return {
            "added": [{"name": "回民街巷子"}], "errors": [], "ready_scenes": ["回民街巷子"],
            "resolved_names": {"回民街巷子": "回民街巷子"},
        }

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    source_text = (
        "占位第一段内容。"
        "\n\n两人走在回民街的青石板路上，人挤人的巷子里飘着孜然与蜂蜜的甜香。"
        "\n\n他们继续往前走，脚步声混在人群中。"
    )
    scene_mentions = [{
        "display_name": "回民街巷子", "suspected_true_name": None,
        "segment_indexes": [2, 3], "quote": "", "source_wording": "回民街",
    }]

    result = asyncio.run(prep_pack._resolve_assets(
        conn, project_id="p1", episode_id="ep-test", episode_no=2,
        source_text=source_text, character_mentions=[], scene_mentions=scene_mentions,
        prop_mentions=[], run_id=None,
    ))
    _characters, scene_list, _props, _functional_extras, errors, _stats = result[:6]

    assert errors == []
    assert len(scene_list) == 1
    entry = scene_list[0]
    assert entry["display_name"] == "回民街巷子"
    assert entry["provenance"]["method"] == "discovery"
    assert entry["provenance"]["anchor_phrase"] == "回民街"
    assert entry["provenance"]["anchor_segments"] == [2]


# ---------------------------------------------------------------------------
# ⑥ card_match 两个调用点（discovery.py / service.py）必须对同一份
#    source_wording/known_prop_name 得到同一判定
# ---------------------------------------------------------------------------

async def _explode_chat_structured(*_a, **_k):
    raise AssertionError("命中既有卡片就不该再发起模型调用")


async def test_card_match_two_call_sites_agree_on_same_nomination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """discovery.py（映射台清单构建）与 service.py（道具库反应式登记）必须
    对同一条提及（同一份 label/source_wording/known_prop_name）得到同一个
    判定——都绑定到既有卡「小木星星」，不是各判各的（CLAUDE.md「模型契约
    两侧必须对齐」）。label「星星挂饰」既不是卡名、也不逐字出现在原文，
    绑定依据是模型提名 known_prop_name「小木星星」+ source_wording「木星星」
    逐字出现在证据里（提名核验通过，纯包含关系已不再是独立判据）。"""
    source_text = "她手心攥着那枚旧旧的木星星。\n\n她把它贴身收好，舍不得放下。"
    segments = index_source_segments(source_text)
    mention = {
        "label": "星星挂饰", "description": "一件小巧的挂饰",
        "segment_indexes": [1, 2], "source_wording": "木星星", "known_prop_name": "小木星星",
    }

    props = _prep_pack_build_prop_manifest(
        [_mention(
            mention["label"], mention["segment_indexes"],
            source_wording=mention["source_wording"], known_prop_name=mention["known_prop_name"],
        )],
        segments, cards=[_card("小木星星")],
    )
    assert len(props) == 1
    assert props[0]["canonical_name"] == "小木星星"

    _seed_project("p-align", [{
        "name": "小木星星", "appearance_canonical": "掌心大小的木刻星星玩具", "aliases": [],
    }])
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _explode_chat_structured)

    result = await service.ensure_props_for_labels("p-align", 3, [mention], source_text=source_text)

    assert result == {"added": [], "errors": []}, "不该被当成全新道具重复建卡"
    conn = get_conn()
    bible = json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p-align'").fetchone()["bible_json"]
    )
    assert [p["name"] for p in bible["props"]] == ["小木星星"]
    assert "星星挂饰" in bible["props"][0]["aliases"]


# ---------------------------------------------------------------------------
# schema 两侧对齐：Pydantic 模型与发给模型的 JSON schema 都要求这个字段
# ---------------------------------------------------------------------------

def test_scene_and_prop_mention_schemas_require_source_wording() -> None:
    from app.production.prep_pack.schemas import _ModelPropMention, _ModelSceneMention

    assert "source_wording" in _ModelSceneMention.model_fields
    assert "source_wording" in _ModelPropMention.model_fields
    assert "source_wording" in _ModelSceneMention.model_json_schema()["required"]
    assert "source_wording" in _ModelPropMention.model_json_schema()["required"]


# ---------------------------------------------------------------------------
# 「模型提名、代码核验」（主会话复核后改；见 app.props.card_match 模块
# docstring「已知局限」一节）：纯包含关系已从判据里删除——`水晶球` ⊂ `老式
# 水晶球`（不同实物）与 `木星星` ⊂ `小木星星`（同一实物）结构完全同形，字符
# 串判据分不清两者。归属只能来自：a) 卡名/别名本身逐字出现在证据里（不变的
# 常规判据，不需要提名）；b) 模型经 known_prop_name 明确提名 + 代码核验证据。
# ---------------------------------------------------------------------------

def test_no_nomination_label_containment_alone_does_not_bind() -> None:
    """无提名时，纯包含关系不再是判据：label「木星星」本身不是卡名，卡
    「小木星星」也不逐字出现在证据里，不得被静默归并——诚实地不绑，按新
    道具处理（不猜，不是需要阻断的失败）。"""
    segments = index_source_segments("手心攥着那枚旧旧的木星星。")
    props = _prep_pack_build_prop_manifest(
        [_mention("木星星", [1])], segments, cards=[_card("小木星星")],
    )
    assert len(props) == 1
    assert props[0]["canonical_name"] is None, "无提名、卡名不逐字在证据里，不得归并到既有卡"
    assert props[0]["label"] == "木星星"


def test_literal_card_name_in_evidence_still_matches_without_nomination() -> None:
    """常规判据不受影响：「旧行李箱」场景下 label 与 source_wording 都会写
    「旧行李箱」，证据里逐字出现了卡名「行李箱」——不需要提名也能唯一裁决；
    另一张卡「旧行李箱三件套」只与原文写法存在包含关系（已不是判据），不
    会被当成候选，不产生歧义。"""
    from app.props.card_match import match_existing_prop_card

    cards = [_card("行李箱"), _card("旧行李箱三件套")]
    evidence = "她拖着那只旧行李箱，深一脚浅一脚地往前走。"
    card = match_existing_prop_card("旧行李箱", evidence, cards, source_wording="旧行李箱")
    assert card is not None and card.name == "行李箱"


def test_nomination_binds_even_when_card_name_absent_from_evidence() -> None:
    """模型提名 known_prop_name「行李箱」+ label/source_wording 任一逐字在
    证据里 → 直接绑定，不需要卡名本身出现在证据里。"""
    segments = index_source_segments("她拖着那只旧箱子，深一脚浅一脚地往前走。")
    props = _prep_pack_build_prop_manifest(
        [_mention("旧箱子", [1], known_prop_name="行李箱")],
        segments, cards=[_card("行李箱")],
    )
    assert len(props) == 1
    assert props[0]["canonical_name"] == "行李箱"


def test_nomination_of_unknown_card_falls_back_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """模型提名的卡在素材库里根本不存在——不失败、不兜底，打
    `[PROP_CARD_NOMINATION_UNKNOWN]` 前缀日志后退回常规判据；常规判据本身
    也判不出来时诚实地不绑。"""
    segments = index_source_segments("桌上摆着一枚奇怪的徽章。")
    with caplog.at_level("WARNING"):
        props = _prep_pack_build_prop_manifest(
            [_mention("奇怪的徽章", [1], known_prop_name="传家宝徽章")],
            segments, cards=[_card("行李箱")],
        )
    assert len(props) == 1
    assert props[0]["canonical_name"] is None
    assert "[PROP_CARD_NOMINATION_UNKNOWN]" in caplog.text
    assert "传家宝徽章" in caplog.text


async def test_production_shaped_crystal_ball_is_not_merged_into_unrelated_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产真实形状回归（见 tests/test_prop_card_binding.py::
    test_ensure_props_for_labels_ignores_unrelated_card_mentioned_outside_
    own_segment 的原始反例，这里额外验证"真带上 source_wording 后依旧不
    误绑"）：既有卡「老式水晶球」是博物馆展品，本集第2段另一个"水晶球"是
    完全不相关的新道具，source_wording「水晶球」与卡名存在纯包含关系，但
    模型没有提名（known_prop_name 为空）——不得被误并成同一张卡，必须新建。
    """
    _seed_project("p-crystal", [{
        "name": "老式水晶球", "appearance_canonical": "博物馆展出的老式水晶球，表面布满灰尘", "aliases": [],
    }])

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
    mentions = [{
        "label": "水晶球", "description": "她取出的水晶球", "segment_indexes": [2],
        "source_wording": "水晶球", "known_prop_name": "",
    }]

    result = await service.ensure_props_for_labels("p-crystal", 3, mentions, source_text=source_text)

    assert [item["name"] for item in result["added"]] == ["水晶球"], "必须新建独立卡，不能被无关的既有卡污染"
    conn = get_conn()
    bible = json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p-crystal'").fetchone()["bible_json"]
    )
    assert {p["name"] for p in bible["props"]} == {"老式水晶球", "水晶球"}
