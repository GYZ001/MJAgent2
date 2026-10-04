"""道具卡复核 owner 归属证据第二层核验的数据源（``app.props.
card_audit_cooccurrence``）：分镜段/原文段共现数据怎么扫、怎么在两个数据源
之间二选一、以及拿这份数据判定某个 owner 候选是否真的与本卡共现过。背景见
本次派单（2026-10-03-v3，B 沙箱第 3 轮真实模型实测：「热牛奶」被以
owner="白色陶瓷杯" 误删——那是另一场戏的另一只杯子）。
"""
from __future__ import annotations

import json

import pytest

from app.db import get_conn, now
from app.props import card_audit, card_audit_consensus, card_audit_cooccurrence as cooc, card_audit_rules, judge
from app.schemas import Prop


def _seed_episode(project_id: str, episode_id: str, episode_no: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps({
            "characters": [], "scenes": [], "props": [],
            "world": {"era": "", "genre": "", "visual_style_canonical": "写实"},
        }, ensure_ascii=False), now()),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) VALUES(?,?,?,?)",
        (project_id, 1, "第一章", "正文占位。"),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, source_chapters, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (episode_id, project_id, episode_no, "scripted", json.dumps([1]), now()),
    )
    conn.commit()


def _seed_shot(episode_id: str, shot_no: int, segment_no: int, labels: list[str]) -> None:
    segment = {"segment_no": segment_no, "resources": {"props": [{"label": label} for label in labels]}}
    conn = get_conn()
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
        (f"{episode_id}_s{shot_no}", episode_id, shot_no, 10,
         json.dumps({"storyboard_pack_segment": segment}, ensure_ascii=False)),
    )
    conn.commit()


def _seed_prep_pack_artifact(episode_id: str, *, props: list[dict], status: str = "approved") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO artifacts(id, type, scope_type, scope_id, version, status, trust_level, "
        "content_json, content_hash, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            f"art_{episode_id}", "episode_prep_pack", "episode", episode_id, 1, status, "T2",
            json.dumps({"asset_manifest": {"props": props}}, ensure_ascii=False), "hash", now(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1) storyboard_label_segment_keys：按 (episode_id, segment_no) 聚合
# ---------------------------------------------------------------------------

def test_storyboard_label_segment_keys_groups_by_episode_and_segment_no() -> None:
    _seed_episode("p-cooc-1", "ep-1", 1)
    _seed_shot("ep-1", 1, 20, ["浅灰色卫衣", "星盘"])
    _seed_shot("ep-1", 2, 20, ["浅灰色卫衣"])  # 同一段的另一镜，同样算同一个 segment_no
    _seed_shot("ep-1", 3, 28, ["热牛奶"])
    conn = get_conn()
    out = cooc.storyboard_label_segment_keys(conn, "p-cooc-1")
    assert out["浅灰色卫衣"] == frozenset({("ep-1", 20)})
    assert out["星盘"] == frozenset({("ep-1", 20)})
    assert out["热牛奶"] == frozenset({("ep-1", 28)})


def test_storyboard_label_segment_keys_empty_when_project_has_no_shots() -> None:
    _seed_episode("p-cooc-2", "ep-2", 1)
    conn = get_conn()
    assert cooc.storyboard_label_segment_keys(conn, "p-cooc-2") == {}


# ---------------------------------------------------------------------------
# 2) asset_manifest_label_segment_keys：映射台退路数据源
# ---------------------------------------------------------------------------

def test_asset_manifest_label_segment_keys_reads_latest_approved_artifact() -> None:
    _seed_episode("p-cooc-3", "ep-3", 1)
    _seed_prep_pack_artifact("ep-3", props=[
        {"label": "旧星盘", "segment_indexes": [14]},
        {"label": "浅灰色卫衣", "segment_indexes": [34]},
    ])
    conn = get_conn()
    out = cooc.asset_manifest_label_segment_keys(conn, "p-cooc-3")
    assert out["旧星盘"] == frozenset({("ep-3", 14)})
    assert out["浅灰色卫衣"] == frozenset({("ep-3", 34)})


def test_asset_manifest_label_segment_keys_ignores_episode_without_artifact() -> None:
    _seed_episode("p-cooc-4", "ep-4", 1)
    conn = get_conn()
    assert cooc.asset_manifest_label_segment_keys(conn, "p-cooc-4") == {}


# ---------------------------------------------------------------------------
# 3) label_segment_keys_for_project：按项目整体是否有分镜数据二选一
# ---------------------------------------------------------------------------

def test_label_segment_keys_for_project_prefers_storyboard_when_present() -> None:
    _seed_episode("p-cooc-5", "ep-5", 1)
    _seed_shot("ep-5", 1, 1, ["道具甲"])
    _seed_prep_pack_artifact("ep-5", props=[{"label": "道具甲", "segment_indexes": [99]}])
    conn = get_conn()
    out = cooc.label_segment_keys_for_project(conn, "p-cooc-5")
    assert out["道具甲"] == frozenset({("ep-5", 1)})  # 分镜数据优先，不是原文段号 99


def test_label_segment_keys_for_project_falls_back_to_asset_manifest_when_no_shots() -> None:
    _seed_episode("p-cooc-6", "ep-6", 1)
    _seed_prep_pack_artifact("ep-6", props=[{"label": "道具甲", "segment_indexes": [7]}])
    conn = get_conn()
    out = cooc.label_segment_keys_for_project(conn, "p-cooc-6")
    assert out["道具甲"] == frozenset({("ep-6", 7)})


# ---------------------------------------------------------------------------
# 4) cooccurring_owners_for_prop：纯计算，data 推导共现
# ---------------------------------------------------------------------------

def test_cooccurring_owners_for_prop_matches_via_alias() -> None:
    """owner 候选的识别要展开到那张卡的全部别名——即使模型 owner 字段写的是
    「星盘」，那张卡登记的卡名是「旧星盘」，共现数据只认实际出现过的 label。"""
    label_segments = {"浅灰色卫衣": frozenset({("ep1", 20)}), "星盘": frozenset({("ep1", 20)})}
    prop = Prop(name="浅灰色卫衣", appearance_canonical="x", aliases=[])
    other = Prop(name="旧星盘", appearance_canonical="y", aliases=["星盘"])
    out = cooc.cooccurring_owners_for_prop(label_segments, prop, frozenset({"星盘"}), [prop, other])
    assert out == frozenset({"星盘"})


def test_cooccurring_owners_for_prop_excludes_non_cooccurring_candidate() -> None:
    """真实案例：热牛奶与白色陶瓷杯从未同段出现——不能因为项目里有这张卡就
    认定共现。"""
    label_segments = {
        "热牛奶": frozenset({("ep1", 28)}),
        "白色陶瓷杯": frozenset({("ep1", 5)}),
    }
    prop = Prop(name="热牛奶", appearance_canonical="x", aliases=[])
    other = Prop(name="白色陶瓷杯", appearance_canonical="y", aliases=[])
    out = cooc.cooccurring_owners_for_prop(label_segments, prop, frozenset({"白色陶瓷杯"}), [prop, other])
    assert out == frozenset()


def test_cooccurring_owners_for_prop_empty_data_fails_closed() -> None:
    """没有任何共现数据（项目还没有分镜/映射数据）：不是跳过检查，是对全部
    候选都判定"未共现"（CLAUDE.md「空集合不等于无需检查」）。"""
    prop = Prop(name="热牛奶", appearance_canonical="x", aliases=[])
    other = Prop(name="白色陶瓷杯", appearance_canonical="y", aliases=[])
    out = cooc.cooccurring_owners_for_prop({}, prop, frozenset({"白色陶瓷杯"}), [prop, other])
    assert out == frozenset()


# ---------------------------------------------------------------------------
# 4b) owner_evidence_in_clause_text：归属证据的第二条路径（2026-10-04-v4，
#     与共现二选一满足即可）
# ---------------------------------------------------------------------------

def test_owner_evidence_in_clause_text_matches_cards_own_name() -> None:
    """真实案例：卫衣子句"胸前正中区域有长期放置旧星盘形成的浅淡压痕"本身就
    含"旧星盘"四个字——子句自己已经把来源写出来了，视为归属证据成立。"""
    other = Prop(name="旧星盘", appearance_canonical="黄铜材质", aliases=["星盘"])
    clause = "胸前正中区域有长期放置旧星盘形成的浅淡压痕"
    assert cooc.owner_evidence_in_clause_text("旧星盘", clause, [other]) is True


def test_owner_evidence_in_clause_text_ignores_aliases() -> None:
    """只认卡名、不认别名：别名正是复核要清理的对象，泛称别名逐字出现在
    不相干子句里是常态。"""
    other = Prop(name="旧星盘", appearance_canonical="黄铜材质", aliases=["星盘"])
    clause = "胸前有星盘压痕"
    assert cooc.owner_evidence_in_clause_text("旧星盘", clause, [other]) is False


def test_owner_evidence_in_clause_text_rejects_generic_alias_hit() -> None:
    """真实回归（2026-10-04 B 沙箱）：「白色陶瓷杯」卡带着泛称别名「马克杯」，
    「热牛奶」的「容器为纯白色无印花直身陶瓷马克杯」逐字含「马克杯」，按别名
    算就会判归属成立、把顾屿家那只杯子的外观删掉。按真实别名构造，必须不成立。"""
    other = Prop(
        name="白色陶瓷杯", appearance_canonical="竖纹马克杯",
        aliases=["白色陶瓷圆口马克杯", "纯白色陶瓷圆口马克杯", "马克杯"],
    )
    clause = "容器为纯白色无印花直身陶瓷马克杯"
    assert cooc.owner_evidence_in_clause_text("白色陶瓷杯", clause, [other]) is False
    assert cooc.owner_evidence_in_clause_text("马克杯", clause, [other]) is False


# ---------------------------------------------------------------------------
# 5) verify_clause_removal_verdicts / compute_prop_card_audit 全流程接线：
#    owner 共现核验真的接进了复核主流程（不只是 cooccurring_owners_for_prop
#    这一个纯函数自己对）。
# ---------------------------------------------------------------------------

def test_verify_clause_removal_downgrades_owner_not_cooccurring_to_doubt() -> None:
    """真实案例：热牛奶被以 owner="白色陶瓷杯" 删除，但那是另一场戏的另一只
    杯子——owner 落在 other_identifiers 里（项目确实有这张卡），但从未与本卡
    共现过，必须降级为存疑，不能只凭"同品类卡存在"就删（2026-10-03-v3）。"""
    removed, records, missing, doubts = card_audit_rules.verify_clause_removal_verdicts(
        ["甲", "容器为纯白色无印花直身陶瓷马克杯", "丙"],
        [{"index": 2, "remove": True, "category": "other_object_or_mark", "owner": "白色陶瓷杯", "reason": "容器外观"}],
        frozenset({"白色陶瓷杯"}), cooccurring_owners=frozenset(), all_props=[],
    )
    assert removed == set() and records == []
    assert missing == {1, 3}
    assert doubts == [{
        "index": 2, "doubt_type": card_audit_rules.DOUBT_TYPE_OWNER_NOT_COOCCURRING,
        "reason": "容器外观", "owner": "白色陶瓷杯",
    }]


def test_verify_clause_removal_accepts_owner_via_literal_text_evidence_when_not_cooccurring() -> None:
    """共现证据缺失（``cooccurring_owners=frozenset()``，模拟"星盘被卫衣遮住、
    分镜段从未把它列为可见道具"这一已知局限），但子句原文本身已经点名了
    owner 卡的卡名——``all_props`` 传入后第二条证据路径成立，照常采信删除
    （2026-10-04-v4）。"""
    removed, records, missing, doubts = card_audit_rules.verify_clause_removal_verdicts(
        ["甲", "胸前正中区域有长期放置旧星盘形成的浅淡压痕", "丙"],
        [{"index": 2, "remove": True, "category": "other_object_or_mark", "owner": "旧星盘", "reason": "压痕"}],
        frozenset({"旧星盘", "星盘"}), cooccurring_owners=frozenset(),
        all_props=[Prop(name="旧星盘", appearance_canonical="黄铜材质", aliases=["星盘"])],
    )
    assert removed == {2}
    assert records[0]["category"] == "other_object_or_mark"
    assert missing == {1, 3} and doubts == []


def _mock_judgment(monkeypatch: pytest.MonkeyPatch, clauses: list[dict]) -> None:
    """两次独立调用都打同一个桩，模拟"两次判定一致"——与
    ``tests/test_prop_card_audit.py`` 同名 helper 同一用途，各自持有一份
    （测试文件之间不互相 import，同 CLAUDE.md「与 app.video_modes.scene_
    state_views 同一类小解析器各自持有一份」的同一精神）。"""
    async def _fake(_prop, _clauses, _owner_catalog_text, **_kwargs):
        return {"clauses": clauses, "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)


async def test_compute_audit_removes_other_object_mark_clause_when_owner_cooccurs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """头号案例：卫衣外观把"胸前有星盘压痕"写进去——星盘与卫衣在同一个分镜段
    共现过，owner 归属通过共现核验，正常删除（2026-10-03-v3，规则优先级修复
    后应当恢复成能自动删除，与 B 沙箱第 1、2 轮判定一致）。"""
    prop = Prop(name="浅灰色卫衣", appearance_canonical="雾感哑光浅灰色棉质，圆领宽松版型，胸前有星盘压痕", aliases=[])
    other = Prop(name="旧星盘", appearance_canonical="黄铜材质", aliases=["星盘"])
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    _mock_judgment(monkeypatch, [
        *[{"index": i + 1, "remove": False} for i in range(len(clauses) - 1)],
        {"index": len(clauses), "remove": True, "category": "other_object_or_mark", "owner": "星盘", "reason": "星盘压痕"},
    ])
    label_segments = {"浅灰色卫衣": frozenset({("ep1", 20)}), "星盘": frozenset({("ep1", 20)})}
    result = await card_audit.compute_prop_card_audit(prop, [prop, other], label_segments=label_segments)
    assert result["appearance_changed"] is True
    assert result["new_appearance"] == judge.rebuild_appearance_excluding(prop.appearance_canonical, {len(clauses)})
    assert "压痕" not in result["new_appearance"]
    assert result["removed_clauses"][0]["category"] == "other_object_or_mark"
    assert result["doubts"] == []


async def test_compute_audit_downgrades_owner_claim_without_cooccurrence_to_doubt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实案例：《顾念长安》"热牛奶"外观被以 owner="白色陶瓷杯" 删除，但那只
    白色陶瓷杯是另一场戏（咖啡馆）的马克杯，与热牛奶的杯子从未同段共现——
    即使项目里确有这张卡，也要降级为存疑，不能删（2026-10-03-v3）。"""
    prop = Prop(
        name="热牛奶", appearance_canonical="奶白色液体，容器为纯白色无印花直身陶瓷马克杯，杯身高约10cm", aliases=[],
    )
    other = Prop(name="白色陶瓷杯", appearance_canonical="竖纹马克杯", aliases=[])
    _mock_judgment(monkeypatch, [
        {"index": 1, "remove": False},
        {"index": 2, "remove": True, "category": "other_object_or_mark", "owner": "白色陶瓷杯", "reason": "容器外观"},
        {"index": 3, "remove": True, "category": "other_object_or_mark", "owner": "白色陶瓷杯", "reason": "容器外观"},
    ])
    # 热牛奶出现在第 1 集第 28-29 段；白色陶瓷杯出现在完全不同的段落，两者
    # 从未共现——与真实数据一致（见派单 B 沙箱实测记录）。
    label_segments = {
        "热牛奶": frozenset({("ep1", 28), ("ep1", 29)}),
        "白色陶瓷杯": frozenset({("ep1", 5), ("ep1", 6)}),
    }
    result = await card_audit.compute_prop_card_audit(prop, [prop, other], label_segments=label_segments)
    assert result["removed_clauses"] == []
    assert result["new_appearance"] == prop.appearance_canonical
    assert result["appearance_changed"] is False
    doubt_types = {d["doubt_type"] for d in result["doubts"]}
    assert doubt_types == {card_audit_consensus.DOUBT_TYPE_OWNER_NOT_COOCCURRING}
    assert all(d["owner"] == "白色陶瓷杯" for d in result["doubts"])


async def test_compute_audit_accepts_owner_evidence_from_clause_text_when_hidden_from_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实案例的全流程修复（2026-10-04-v4，B 沙箱第 4 轮实测）：星盘贴身戴在
    卫衣下面，分镜段的可见道具清单里从不列它——``cooccurring_owners`` 对这类
    被遮住的物件结构性永远是空集合，``label_segments`` 不含"浅灰色卫衣"/
    "星盘"任何一条。但子句原文本身已经点名"旧星盘"，第二条证据路径成立，
    照常删除，不再停留在存疑。"""
    prop = Prop(
        name="浅灰色卫衣",
        appearance_canonical="雾感哑光浅灰色棉质，圆领宽松版型，胸前正中区域有长期放置旧星盘形成的浅淡压痕",
        aliases=[],
    )
    other = Prop(name="旧星盘", appearance_canonical="黄铜材质", aliases=["星盘"])
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    _mock_judgment(monkeypatch, [
        *[{"index": i + 1, "remove": False} for i in range(len(clauses) - 1)],
        {
            "index": len(clauses), "remove": True, "category": "other_object_or_mark",
            "owner": "旧星盘", "reason": "星盘压痕",
        },
    ])
    result = await card_audit.compute_prop_card_audit(prop, [prop, other], label_segments={})
    assert result["appearance_changed"] is True
    assert "压痕" not in result["new_appearance"]
    assert result["removed_clauses"][0]["category"] == "other_object_or_mark"
    assert result["doubts"] == []
