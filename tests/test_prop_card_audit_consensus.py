"""道具卡复核「两次独立判定取交集」的合并逻辑与存储（``app.props.
card_audit_consensus``/``card_audit_rules`` 的 owner 归属证据核验/
``card_audit_store`` 的 doubts 持久化）。

与 ``tests/test_prop_card_audit.py``（单次调用的子句/别名核验、三类真实
场景、原子重出图、CAS）互补：那个文件钉住"两次判定都一致时算得对不对"，
这个文件钉住"两次判定不一致/归属没有卡/模型自述拿不准时，是不是老老实实
转成存疑而不是悄悄删或悄悄留"。``tests/test_prop_card_audit_doubts.py``
再往下接"存疑的人工确认/保留端点"。背景见本次派单（2026-10-03-v2）。
"""
from __future__ import annotations

import json

import pytest

from app.db import get_conn, now
from app.props import card_audit, card_audit_consensus, card_audit_rules, card_audit_store
from app.schemas import Prop


def _seed_project(project_id: str, *, props_list: list[dict] | None = None) -> None:
    bible = {
        "characters": [], "scenes": [], "props": props_list or [],
        "world": {"era": "", "genre": "", "visual_style_canonical": "写实"},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), now()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1) card_audit_rules：owner 归属证据核验、uncertain 自述拿不准
# ---------------------------------------------------------------------------

def test_verify_clause_removal_accepts_person_or_action_owner() -> None:
    removed, _records, _missing, doubts = card_audit_rules.verify_clause_removal_verdicts(
        1, [{"index": 1, "remove": True, "category": "other_object_or_mark",
             "owner": card_audit_rules.OWNER_PERSON_OR_ACTION, "reason": "蹭上的痕迹"}],
        frozenset(),
    )
    assert removed == {1} and doubts == []


def test_verify_clause_removal_downgrades_owner_without_card_to_doubt() -> None:
    """真实案例：止血丹外观前几句写的是装它的瓷瓶，瓷瓶没有自己的道具卡——
    降级为存疑，不删除，这条外观信息不会因为"容器没有卡"而从所有卡里消失。"""
    removed, records, missing, doubts = card_audit_rules.verify_clause_removal_verdicts(
        1, [{"index": 1, "remove": True, "category": "other_object_or_mark", "owner": "瓷瓶", "reason": "容器外观"}],
        frozenset({"止血丹"}),
    )
    assert removed == set() and records == []
    assert missing == set()  # 模型确实给出了判定，只是降级，不算"缺失判定"
    assert doubts == [{"index": 1, "doubt_type": "owner_without_card", "reason": "容器外观", "owner": "瓷瓶"}]


def test_verify_clause_removal_uncertain_clause_becomes_self_doubt() -> None:
    removed, records, missing, doubts = card_audit_rules.verify_clause_removal_verdicts(
        1, [{"index": 1, "remove": True, "category": "other_object_or_mark",
             "owner": card_audit_rules.OWNER_PERSON_OR_ACTION,
             "uncertain": True, "reason": "分不清是容器还是本体"}],
        frozenset(),
    )
    assert removed == set() and records == [] and missing == set()
    assert doubts == [{"index": 1, "doubt_type": "model_self_doubt", "reason": "分不清是容器还是本体"}]


def test_catalog_text_for_prompt_lists_other_cards_excluding_self() -> None:
    prop = Prop(name="止血丹", appearance_canonical="x", aliases=[])
    bottle = Prop(name="瓷瓶", appearance_canonical="y", aliases=["白瓷药瓶"])
    text = card_audit_rules.catalog_text_for_prompt(prop, [prop, bottle])
    assert "瓷瓶" in text and "白瓷药瓶" in text and "止血丹" not in text


# ---------------------------------------------------------------------------
# 2) card_audit_consensus：两次判定取交集——一致才删，不一致/降级都转存疑
# ---------------------------------------------------------------------------

def test_merge_clause_judgments_both_agree_removes() -> None:
    raw_a = [{"index": 1, "remove": True, "category": "plot_state", "reason": "A"}]
    raw_b = [{"index": 1, "remove": True, "category": "plot_state", "reason": "B"}]
    merged = card_audit_consensus.merge_clause_judgments(1, raw_a, raw_b, frozenset(), ["泡水后发蔫"])
    assert merged["removed_indexes"] == {1}
    assert merged["doubts"] == []
    assert "A" in merged["removed_records"][0]["reason"] and "B" in merged["removed_records"][0]["reason"]


def test_merge_clause_judgments_disagreement_becomes_doubt_not_deletion() -> None:
    """真实不稳定样本：泡面铝箔盖一次判删一次判留——两次不一致就不删，转存疑。"""
    raw_a = [{"index": 1, "remove": True, "category": "plot_state", "reason": "已开盖"}]
    raw_b = [{"index": 1, "remove": False, "reason": "未提及"}]
    merged = card_audit_consensus.merge_clause_judgments(1, raw_a, raw_b, frozenset(), ["铝箔盖"])
    assert merged["removed_indexes"] == set()
    assert len(merged["doubts"]) == 1
    doubt = merged["doubts"][0]
    assert doubt["doubt_type"] == card_audit_consensus.DOUBT_TYPE_INCONSISTENT
    assert doubt["text"] == "铝箔盖" and doubt["reason_a"] == "已开盖" and doubt["reason_b"] == "（B 判定不删除）"


def test_merge_clause_judgments_owner_without_card_surfaces_as_doubt_even_if_agreed() -> None:
    """两次都判定「别的物件」但归属都没有卡——两次一致也不能删，必须降级，且
    只产出一条存疑（审查发现：此前对同一下标 A/B 各自降级会各产出一条，造成
    同一 ``doubt_key`` 下两条重复记录，前端 React key 冲突）。"""
    raw = [{"index": 1, "remove": True, "category": "other_object_or_mark", "owner": "汤碗", "reason": "容器"}]
    merged = card_audit_consensus.merge_clause_judgments(1, raw, raw, frozenset({"小馄饨"}), ["盛着热汤"])
    assert merged["removed_indexes"] == set()
    assert len(merged["doubts"]) == 1
    doubt = merged["doubts"][0]
    assert doubt["doubt_type"] == card_audit_consensus.DOUBT_TYPE_OWNER_WITHOUT_CARD
    assert doubt["owner"] == "汤碗"
    assert doubt["reason_a"] == "容器" and doubt["reason_b"] == "容器"


def test_merge_clause_judgments_one_call_removes_other_downgrades_single_doubt() -> None:
    """A 判定删除成功、B 判定同一下标但 owner 没有卡——结果不是"A 的不一致 +
    B 的降级"两条，必须合并成同一条下标只产出一条存疑。"""
    raw_a = [{"index": 1, "remove": True, "category": "other_object_or_mark",
              "owner": "瓷瓶", "reason": "容器A"}]
    raw_b = [{"index": 1, "remove": True, "category": "other_object_or_mark",
              "owner": "没卡的容器", "reason": "容器B"}]
    merged = card_audit_consensus.merge_clause_judgments(1, raw_a, raw_b, frozenset({"瓷瓶"}), ["装它的瓷瓶"])
    assert merged["removed_indexes"] == set()
    assert len(merged["doubts"]) == 1
    doubt = merged["doubts"][0]
    assert doubt["doubt_type"] == card_audit_consensus.DOUBT_TYPE_OWNER_WITHOUT_CARD
    assert doubt["reason_a"] == "容器A" and doubt["reason_b"] == "容器B"


def test_merge_clause_judgments_invalid_category_surfaces_as_single_doubt() -> None:
    """模型判定 remove=true 却给了三类之外的 category：不静默丢弃，降级为
    ``invalid_category`` 存疑；两次都如此也只产出一条，不重复。"""
    raw = [{"index": 1, "remove": True, "category": "weird_category", "reason": "说不清"}]
    merged = card_audit_consensus.merge_clause_judgments(1, raw, raw, frozenset(), ["某条外观"])
    assert merged["removed_indexes"] == set()
    assert len(merged["doubts"]) == 1
    assert merged["doubts"][0]["doubt_type"] == card_audit_rules.DOUBT_TYPE_INVALID_CATEGORY
    assert merged["doubts"][0]["category"] == "weird_category"


def test_merge_alias_judgments_disagreement_becomes_doubt() -> None:
    raw_a = [{"alias": "椅子", "category_only": True, "reason": "泛称"}]
    raw_b = [{"alias": "椅子", "category_only": False, "reason": "可指认"}]
    merged = card_audit_consensus.merge_alias_judgments(["椅子"], raw_a, raw_b)
    assert merged["removed"] == []
    assert merged["doubts"][0]["alias"] == "椅子"
    assert merged["doubts"][0]["doubt_type"] == card_audit_consensus.DOUBT_TYPE_INCONSISTENT


def test_filter_doubts_against_kept_decisions_suppresses_matching_key() -> None:
    doubts = [
        {"kind": "clause", "index": 1, "text": "瓷瓶外观", "doubt_type": "owner_without_card"},
        {"kind": "alias", "alias": "椅子", "doubt_type": "inconsistent_between_two_judgments"},
    ]
    kept = frozenset({card_audit_consensus.clause_doubt_key("瓷瓶外观")})
    out = card_audit_consensus.filter_doubts_against_kept_decisions(doubts, kept)
    assert len(out) == 1 and out[0]["kind"] == "alias"


# ---------------------------------------------------------------------------
# 3) compute_prop_card_audit 全流程：两次调用真的各自独立采样（call_tag 区分）
# ---------------------------------------------------------------------------

async def test_compute_audit_two_calls_disagree_keeps_clause_and_records_doubt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prop = Prop(name="泡面", appearance_canonical="红色包装袋、方形桶装、铝箔密封盖", aliases=[])

    async def _fake(_prop, clauses, _owner_catalog_text, *, call_tag):
        verdicts = [{"index": i + 1, "remove": False} for i in range(len(clauses))]
        if call_tag == "a":
            verdicts[-1] = {"index": len(clauses), "remove": True, "category": "plot_state", "reason": "已开盖"}
        return {"clauses": verdicts, "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)

    result = await card_audit.compute_prop_card_audit(prop, [prop])
    assert result["appearance_changed"] is False
    assert result["new_appearance"] == prop.appearance_canonical
    assert len(result["doubts"]) == 1
    assert result["doubts"][0]["doubt_type"] == card_audit_consensus.DOUBT_TYPE_INCONSISTENT


async def test_compute_audit_respects_kept_doubt_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    prop = Prop(name="泡面2", appearance_canonical="红色包装袋、方形桶装、铝箔密封盖", aliases=[])

    async def _fake(_prop, clauses, _owner_catalog_text, *, call_tag):
        verdicts = [{"index": i + 1, "remove": False} for i in range(len(clauses))]
        if call_tag == "a":
            verdicts[-1] = {"index": len(clauses), "remove": True, "category": "plot_state", "reason": "已开盖"}
        return {"clauses": verdicts, "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)

    kept = frozenset({card_audit_consensus.clause_doubt_key("铝箔密封盖")})
    result = await card_audit.compute_prop_card_audit(prop, [prop], kept)
    assert result["doubts"] == []  # 人工已保留过，同规则版本内不再呈现


# ---------------------------------------------------------------------------
# 4) card_audit_store：doubts_json 持久化、kept 决定查询
# ---------------------------------------------------------------------------

def test_update_ready_persists_doubts_and_audits_for_project_exposes_them() -> None:
    _seed_project("p-doubts-1", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    conn = get_conn()
    card_audit_store.ensure_tables_on_connection(conn)
    card_audit_store.insert_running(
        conn, row_id="r1", project_id="p-doubts-1", prop_name="道具A", rules_version="v1", stamp=now(),
    )
    doubts = [{"kind": "clause", "index": 1, "text": "x", "doubt_type": "model_self_doubt"}]
    card_audit_store.update_ready(
        conn, row_id="r1", old_appearance="x", new_appearance="x", removed_clauses=[], removed_aliases=[],
        reimaged=False, feature_shortfall=False, stamp=now(), doubts=doubts,
    )
    conn.commit()
    items = card_audit.audits_for_project(conn, "p-doubts-1")
    assert items[0]["doubts"] == doubts


def test_get_kept_doubt_keys_only_returns_kept_not_deleted() -> None:
    conn = get_conn()
    card_audit_store.ensure_tables_on_connection(conn)
    card_audit_store.record_doubt_decision(
        conn, project_id="p-kept-1", prop_name="道具A", rules_version="v1",
        doubt_key="clause:瓷瓶外观", decision="kept", stamp=now(),
    )
    card_audit_store.record_doubt_decision(
        conn, project_id="p-kept-1", prop_name="道具A", rules_version="v1",
        doubt_key="clause:已删除的", decision="deleted", stamp=now(),
    )
    conn.commit()
    keys = card_audit_store.get_kept_doubt_keys(conn, project_id="p-kept-1", prop_name="道具A", rules_version="v1")
    assert keys == frozenset({"clause:瓷瓶外观"})


def test_get_kept_doubt_keys_empty_after_rules_version_advances() -> None:
    conn = get_conn()
    card_audit_store.ensure_tables_on_connection(conn)
    card_audit_store.record_doubt_decision(
        conn, project_id="p-kept-2", prop_name="道具A", rules_version="v1",
        doubt_key="clause:x", decision="kept", stamp=now(),
    )
    conn.commit()
    keys = card_audit_store.get_kept_doubt_keys(conn, project_id="p-kept-2", prop_name="道具A", rules_version="v2")
    assert keys == frozenset()
