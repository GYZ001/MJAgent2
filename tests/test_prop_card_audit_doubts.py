"""道具卡复核「存疑」人工确认/保留端点（``app.props.card_audit_doubts``/
``app.domain.bible_ops.props_api`` 的 POST /props/audit/confirm 与
/props/audit/keep）。

与 ``tests/test_prop_card_audit_consensus.py``（两次判定怎么合并出存疑）
互补：这个文件钉住"存疑产生之后，人工点确认删除/保留，端点是不是真的按
卡名+原文定位、走同一条核验+重出图+原子回滚、决定是不是真的持久化到让
同一规则版本不再重复询问"。背景见本次派单（2026-10-03-v2）。
"""
from __future__ import annotations

import json

import pytest

from app.db import get_conn, now
from app.props import card_audit, card_audit_consensus, card_audit_doubts, card_audit_store
from app.schemas import Prop


def _seed_project(project_id: str, *, props_list: list[dict]) -> None:
    bible = {
        "characters": [], "scenes": [], "props": props_list,
        "world": {"era": "", "genre": "", "visual_style_canonical": "写实"},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), now()),
    )
    conn.commit()


def _seed_audit_with_doubts(project_id: str, prop_name: str, doubts: list[dict], rules_version: str = "v1") -> str:
    conn = get_conn()
    card_audit_store.ensure_tables_on_connection(conn)
    row_id = f"row-{prop_name}"
    card_audit_store.insert_running(
        conn, row_id=row_id, project_id=project_id, prop_name=prop_name, rules_version=rules_version, stamp=now(),
    )
    card_audit_store.update_ready(
        conn, row_id=row_id, old_appearance="x", new_appearance="x", removed_clauses=[], removed_aliases=[],
        reimaged=False, feature_shortfall=False, stamp=now(), doubts=doubts,
    )
    conn.commit()
    return row_id


# ---------------------------------------------------------------------------
# 1) confirm_doubt：子句/别名，按原文逐字定位，走同一条核验+重出图
# ---------------------------------------------------------------------------

async def test_confirm_doubt_deletes_clause_and_reimages(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实案例：止血丹卡的外观把装它的瓷瓶样子也写进去——瓷瓶没有自己的卡，
    自动复核降级成存疑；人工看过原文确认这条确实该删。"""
    appearance = "朱红色圆丸、瓷瓶外观为青花纹样、略有磕碰"
    _seed_project("p-confirm-1", props_list=[{"name": "止血丹", "appearance_canonical": appearance, "aliases": []}])
    doubt_key = card_audit_consensus.clause_doubt_key("瓷瓶外观为青花纹样")
    _seed_audit_with_doubts("p-confirm-1", "止血丹", [
        {"kind": "clause", "index": 2, "text": "瓷瓶外观为青花纹样", "doubt_type": "owner_without_card",
         "reason_a": "容器", "reason_b": ""},
    ])

    async def _fake_image(_project_id, _name, _prompt):
        return "/fake/new.png"
    monkeypatch.setattr(card_audit.image, "generate_prop_reference_image", _fake_image)

    result = await card_audit_doubts.confirm_doubt("p-confirm-1", "止血丹", doubt_key)
    assert result == {"prop_name": "止血丹", "doubt_key": doubt_key, "status": "deleted", "reimaged": True}

    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-confirm-1'").fetchone()["bible_json"])
    assert bible["props"][0]["appearance_canonical"] == "朱红色圆丸、略有磕碰"
    row = card_audit_store.get_audit(conn, project_id="p-confirm-1", prop_name="止血丹")
    assert json.loads(row["doubts_json"]) == []
    decision = conn.execute(
        "SELECT decision FROM prop_card_audit_doubt_decisions WHERE project_id='p-confirm-1' AND doubt_key=?",
        (doubt_key,),
    ).fetchone()
    assert decision["decision"] == "deleted"


async def test_confirm_doubt_deletes_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p-confirm-2", props_list=[{"name": "椅子甲", "appearance_canonical": "x", "aliases": ["椅子"]}])
    doubt_key = card_audit_consensus.alias_doubt_key("椅子")
    _seed_audit_with_doubts("p-confirm-2", "椅子甲", [
        {"kind": "alias", "alias": "椅子", "text": "椅子", "doubt_type": "inconsistent_between_two_judgments",
         "reason_a": "泛称", "reason_b": "可指认"},
    ])
    result = await card_audit_doubts.confirm_doubt("p-confirm-2", "椅子甲", doubt_key)
    assert result["status"] == "deleted" and result["reimaged"] is False
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-confirm-2'").fetchone()["bible_json"])
    assert bible["props"][0]["aliases"] == []


async def test_confirm_doubt_missing_key_raises() -> None:
    _seed_project("p-confirm-3", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    _seed_audit_with_doubts("p-confirm-3", "道具A", [])
    with pytest.raises(ValueError, match="不存在或已被处理"):
        await card_audit_doubts.confirm_doubt("p-confirm-3", "道具A", "clause:不存在的原文")


async def test_confirm_doubt_stale_text_raises_without_mutating(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡在存疑产生之后又被改过，原文已经找不到逐字匹配——必须拒绝，不能瞎删。"""
    _seed_project("p-confirm-4", props_list=[{"name": "道具A", "appearance_canonical": "红色、圆形、带柄把", "aliases": []}])
    doubt_key = card_audit_consensus.clause_doubt_key("早已不存在的子句")
    _seed_audit_with_doubts("p-confirm-4", "道具A", [
        {"kind": "clause", "index": 9, "text": "早已不存在的子句", "doubt_type": "model_self_doubt",
         "reason_a": "x", "reason_b": ""},
    ])
    with pytest.raises(ValueError, match="找不到逐字匹配"):
        await card_audit_doubts.confirm_doubt("p-confirm-4", "道具A", doubt_key)
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-confirm-4'").fetchone()["bible_json"])
    assert bible["props"][0]["appearance_canonical"] == "红色、圆形、带柄把"


async def test_confirm_doubt_reimage_failure_rolls_back_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    appearance = "红色、圆形、带柄把"
    _seed_project("p-confirm-5", props_list=[{"name": "道具A", "appearance_canonical": appearance, "aliases": []}])
    doubt_key = card_audit_consensus.clause_doubt_key("带柄把")
    _seed_audit_with_doubts("p-confirm-5", "道具A", [
        {"kind": "clause", "index": 3, "text": "带柄把", "doubt_type": "model_self_doubt", "reason_a": "x", "reason_b": ""},
    ])

    async def _fail_image(_project_id, _name, _prompt):
        return None
    monkeypatch.setattr(card_audit.image, "generate_prop_reference_image", _fail_image)

    with pytest.raises(ValueError, match="重新出图失败"):
        await card_audit_doubts.confirm_doubt("p-confirm-5", "道具A", doubt_key)

    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-confirm-5'").fetchone()["bible_json"])
    assert bible["props"][0]["appearance_canonical"] == appearance  # 旧外观原封不动
    row = card_audit_store.get_audit(conn, project_id="p-confirm-5", prop_name="道具A")
    assert len(json.loads(row["doubts_json"])) == 1  # 存疑原样保留，没有被悄悄摘掉


async def test_confirm_doubt_persist_failure_reports_clearly_not_bare_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审查发现：外观已经真的删除并重出图成功之后，如果存疑记录这一步的独立
    写事务遇到非 ``ValueError`` 异常（例如数据库瞬时故障），不能让异常原样
    冒泡——那会被路由层当成裸 500，且用户看不出"卡其实已经改了"。必须转成
    明确说明"已删除但记录没更新、请刷新"的 ``ValueError``（转 409）。"""
    appearance = "红色、圆形、带柄把"
    _seed_project("p-confirm-6", props_list=[{"name": "道具A", "appearance_canonical": appearance, "aliases": []}])
    doubt_key = card_audit_consensus.clause_doubt_key("带柄把")
    _seed_audit_with_doubts("p-confirm-6", "道具A", [
        {"kind": "clause", "index": 3, "text": "带柄把", "doubt_type": "model_self_doubt", "reason_a": "x", "reason_b": ""},
    ])

    async def _fake_image(_project_id, _name, _prompt):
        return "/fake/new.png"
    monkeypatch.setattr(card_audit.image, "generate_prop_reference_image", _fake_image)

    async def _boom(**_kwargs):
        raise RuntimeError("模拟数据库瞬时故障")
    monkeypatch.setattr(card_audit_doubts, "_persist_doubt_resolution", _boom)

    with pytest.raises(ValueError, match="存疑记录更新失败"):
        await card_audit_doubts.confirm_doubt("p-confirm-6", "道具A", doubt_key)

    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-confirm-6'").fetchone()["bible_json"])
    assert "带柄把" not in bible["props"][0]["appearance_canonical"]  # 外观确实已经改完并提交


# ---------------------------------------------------------------------------
# 1b) _persist_doubt_resolution：并发安全（finding 2 的直接回归）
# ---------------------------------------------------------------------------

async def test_persist_doubt_resolution_rereads_fresh_doubts_avoids_lost_update() -> None:
    """并发场景的确定性复现：模拟"另一个请求"先摘掉了存疑 A 并提交，这个
    请求只认领存疑 B——必须读到"A 已被摘掉"之后的最新状态再摘 B，不能拿自己
    请求开始时缓存的旧快照（仍含 A）整表覆写、把刚被摘掉的 A 复活回来。"""
    _seed_project("p-race-1", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    doubt_a = {"kind": "clause", "index": 1, "text": "存疑A", "doubt_type": "model_self_doubt", "reason_a": "x", "reason_b": ""}
    doubt_b = {"kind": "clause", "index": 2, "text": "存疑B", "doubt_type": "model_self_doubt", "reason_a": "y", "reason_b": ""}
    row_id = _seed_audit_with_doubts("p-race-1", "道具A", [doubt_a, doubt_b], rules_version="v1")

    conn = get_conn()
    card_audit_store.update_doubts(conn, row_id=row_id, doubts=[doubt_b])  # 模拟并发请求已先摘掉 A
    conn.commit()

    await card_audit_doubts._persist_doubt_resolution(
        row_id=row_id, project_id="p-race-1", prop_name="道具A", rules_version="v1",
        doubt_key=card_audit_consensus.clause_doubt_key("存疑B"), decision="kept",
    )

    row = card_audit_store.get_audit(conn, project_id="p-race-1", prop_name="道具A")
    assert json.loads(row["doubts_json"]) == []  # 不能把并发请求刚摘掉的 A 复活回来


# ---------------------------------------------------------------------------
# 2) keep_doubt：持久化保留决定，同规则版本不再重复呈现
# ---------------------------------------------------------------------------

async def test_keep_doubt_removes_from_list_and_persists_decision() -> None:
    _seed_project("p-keep-1", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    doubt_key = card_audit_consensus.clause_doubt_key("瓷瓶外观")
    _seed_audit_with_doubts("p-keep-1", "道具A", [
        {"kind": "clause", "index": 1, "text": "瓷瓶外观", "doubt_type": "owner_without_card", "reason_a": "x", "reason_b": ""},
    ], rules_version="v1")

    result = await card_audit_doubts.keep_doubt("p-keep-1", "道具A", doubt_key)
    assert result == {"prop_name": "道具A", "doubt_key": doubt_key, "status": "kept"}

    conn = get_conn()
    row = card_audit_store.get_audit(conn, project_id="p-keep-1", prop_name="道具A")
    assert json.loads(row["doubts_json"]) == []
    kept = card_audit_store.get_kept_doubt_keys(conn, project_id="p-keep-1", prop_name="道具A", rules_version="v1")
    assert kept == frozenset({doubt_key})


async def test_keep_doubt_missing_key_raises() -> None:
    _seed_project("p-keep-2", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    _seed_audit_with_doubts("p-keep-2", "道具A", [])
    with pytest.raises(ValueError, match="不存在或已被处理"):
        await card_audit_doubts.keep_doubt("p-keep-2", "道具A", "clause:不存在")


async def test_keep_doubt_then_recompute_does_not_resurface_same_doubt(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：人工保留一条存疑后，同一规则版本内重新复核不再把它判成存疑
    呈现给用户——即使模型两次判定仍然不一致。"""
    prop = Prop(name="泡面3", appearance_canonical="红色包装袋、方形桶装、铝箔密封盖", aliases=[])

    async def _fake(_prop, clauses, _owner_catalog_text, *, call_tag):
        verdicts = [{"index": i + 1, "remove": False} for i in range(len(clauses))]
        if call_tag == "a":
            verdicts[-1] = {"index": len(clauses), "remove": True, "category": "plot_state", "reason": "已开盖"}
        return {"clauses": verdicts, "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)

    first = await card_audit.compute_prop_card_audit(prop, [prop])
    assert len(first["doubts"]) == 1
    doubt_key = card_audit_consensus.doubt_key(first["doubts"][0])

    card_audit_store.ensure_tables_on_connection(get_conn())
    card_audit_store.record_doubt_decision(
        get_conn(), project_id="p-keep-3", prop_name=prop.name, rules_version="v-fixed",
        doubt_key=doubt_key, decision="kept", stamp=now(),
    )
    get_conn().commit()
    kept = card_audit_store.get_kept_doubt_keys(
        get_conn(), project_id="p-keep-3", prop_name=prop.name, rules_version="v-fixed",
    )
    second = await card_audit.compute_prop_card_audit(prop, [prop], kept)
    assert second["doubts"] == []


# ---------------------------------------------------------------------------
# 3) 路由层：POST /props/audit/confirm 与 /props/audit/keep——ValueError 转 409
# ---------------------------------------------------------------------------

async def test_confirm_route_delegates_and_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.domain.bible_ops import props_api

    async def _fake_confirm(project_id: str, prop_name: str, doubt_key: str) -> dict:
        assert (project_id, prop_name, doubt_key) == ("p-route-1", "道具A", "clause:x")
        return {"prop_name": prop_name, "doubt_key": doubt_key, "status": "deleted", "reimaged": True}
    monkeypatch.setattr(props_api, "confirm_doubt", _fake_confirm)
    _seed_project("p-route-1", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    body = props_api.PropAuditDoubtBody(prop_name="道具A", doubt_key="clause:x")
    response = await props_api.confirm_prop_audit_doubt("p-route-1", body)
    assert response["status"] == "deleted"


async def test_confirm_route_translates_value_error_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.domain.bible_ops import props_api

    async def _raise(*_a, **_k):
        raise ValueError("存疑不存在或已被处理")
    monkeypatch.setattr(props_api, "confirm_doubt", _raise)
    _seed_project("p-route-2", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    body = props_api.PropAuditDoubtBody(prop_name="道具A", doubt_key="clause:x")
    with pytest.raises(HTTPException) as excinfo:
        await props_api.confirm_prop_audit_doubt("p-route-2", body)
    assert excinfo.value.status_code == 409


async def test_keep_route_delegates_and_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.domain.bible_ops import props_api

    async def _fake_keep(project_id: str, prop_name: str, doubt_key: str) -> dict:
        return {"prop_name": prop_name, "doubt_key": doubt_key, "status": "kept"}
    monkeypatch.setattr(props_api, "keep_doubt", _fake_keep)
    _seed_project("p-route-3", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    body = props_api.PropAuditDoubtBody(prop_name="道具A", doubt_key="clause:x")
    response = await props_api.keep_prop_audit_doubt("p-route-3", body)
    assert response["status"] == "kept"
