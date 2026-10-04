"""道具卡复核批量入口（``app.props.card_audit`` 的 ``launch_background_
audit``/``audit_project_prop_cards``/``audit_specific_prop_cards``）的并发
限流与"共享同一份分镜段扫描结果"（2026-10-04 审查发现并修复，从
``tests/test_prop_card_audit.py`` 拆出来，避免那个文件连续超出测试文件 500
行基线——这两条测试本身就是"批量入口"这一个主题，拆开后两个文件都在限额
内）。另外钉住 dry-run 路径的逐卡隔离（2026-10-04-v4 审查发现并修复）：
``audit_project_prop_cards(dry_run=True)`` 此前用 ``asyncio.gather`` 直接
收集，某张卡模型输出被截断导致 JSON 解析失败时异常会从 ``gather`` 整体
冒出，把同批其它卡已经算好的结果一起丢掉。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_conn, now
from app.props import card_audit


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


def _patch_no_change_judgment(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟模型对每条子句都显式给出"不删除"的判定——与 ``tests/test_prop_
    card_audit.py`` 同名 helper 同一套契约（对每条子句都要给出判定，空列表
    会被判定成"模型什么都没判定"走失败重试分支）。"""
    async def _fake(_prop, clauses, _owner_catalog_text, **_kwargs):
        return {"clauses": [{"index": i + 1, "remove": False} for i in range(len(clauses))], "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)


async def test_launch_background_audit_limits_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """``launch_background_audit`` 必须与 ``audit_project_prop_cards``/
    ``audit_specific_prop_cards`` 同一并发口径,不得裸起无限并发任务。"""
    _seed_project("p-concurrency-1")  # bible 必须存在，否则 launch_background_audit
    # 在 _load_batch_audit_context 里就会判定 bible is None，走 _missing_bible_audit_
    # result 短路，根本不会调用下面打桩的 _audit_one_quietly（2026-10-04 批量共享
    # label_segments 的重构后新增的前置判断）。
    in_flight = {"n": 0, "peak": 0}

    async def _fake_one(_project_id: str, name: str, **_kwargs: object) -> dict:
        in_flight["n"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
        await asyncio.sleep(0.01)
        in_flight["n"] -= 1
        return {"prop_name": name, "status": "ready"}

    monkeypatch.setattr(card_audit, "_audit_one_quietly", _fake_one)
    tasks = card_audit.launch_background_audit(
        project_id="p-concurrency-1", prop_names=[f"道具{i}" for i in range(10)],
    )
    await asyncio.gather(*tasks)
    assert in_flight["peak"] <= card_audit._MAX_CONCURRENT_AUDITS


async def test_batch_audit_entries_scan_label_segments_once_not_per_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """审查发现并修复：批量复核一批 N 张卡时，分镜段全表扫描
    （``card_audit_cooccurrence.label_segment_keys_for_project``）此前经由
    ``audit_one_prop_card`` 逐卡各自重新扫一遍，一批 N 张卡扫 N 次；现在
    ``audit_project_prop_cards``/``audit_specific_prop_cards``/``launch_
    background_audit`` 三个批量入口都只扫一次、三张卡共享同一份结果。"""
    _patch_no_change_judgment(monkeypatch)
    _seed_project("p-batch-scan-1", props_list=[
        {"name": f"道具{i}", "appearance_canonical": "红色、圆形、带柄把", "aliases": []} for i in range(3)
    ])
    scan_calls = {"n": 0}
    real_scan = card_audit.card_audit_cooccurrence.label_segment_keys_for_project

    def _counting_scan(conn, project_id):
        scan_calls["n"] += 1
        return real_scan(conn, project_id)

    monkeypatch.setattr(card_audit.card_audit_cooccurrence, "label_segment_keys_for_project", _counting_scan)

    await card_audit.audit_specific_prop_cards("p-batch-scan-1", [f"道具{i}" for i in range(3)])
    assert scan_calls["n"] == 1

    scan_calls["n"] = 0
    await card_audit.audit_project_prop_cards("p-batch-scan-1", dry_run=False)
    assert scan_calls["n"] == 1

    scan_calls["n"] = 0
    tasks = card_audit.launch_background_audit(
        project_id="p-batch-scan-1", prop_names=[f"道具{i}" for i in range(3)],
    )
    await asyncio.gather(*tasks)
    assert scan_calls["n"] == 1


# ---------------------------------------------------------------------------
# 3) dry-run 批量入口逐卡隔离（2026-10-04-v4 审查发现并修复）
# ---------------------------------------------------------------------------

async def test_dry_run_project_batch_isolates_failure_per_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """一张卡模型输出被截断、解析失败——此前 ``asyncio.gather`` 直接收集会让
    这一批结果全部丢失；现在逐卡 try/except，失败的卡单独标记，其它卡照常
    输出结果。"""
    _seed_project("p-dry-isolate-1", props_list=[
        {"name": "道具坏", "appearance_canonical": "红色、圆形、带柄把", "aliases": []},
        {"name": "道具好", "appearance_canonical": "蓝色、方形、带把手", "aliases": []},
    ])

    async def _fake(prop, _clauses, _owner_catalog_text, **_kwargs):
        if prop.name == "道具坏":
            raise ValueError("模型输出 JSON 解析失败：截断")
        return {"clauses": [{"index": i + 1, "remove": False} for i in range(3)], "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)

    output = await card_audit.audit_project_prop_cards("p-dry-isolate-1", dry_run=True)
    by_name = {r["prop_name"]: r for r in output["results"]}
    assert len(by_name) == 2
    assert by_name["道具坏"]["failed"] is True
    assert "截断" in by_name["道具坏"]["error"]
    assert by_name["道具好"]["failed"] is False
    assert by_name["道具好"]["appearance_changed"] is False


async def test_dry_run_single_card_isolates_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """``audit_one_prop_card(dry_run=True)`` 同样不让模型调用异常原样冒泡，
    转成带 ``failed``/``fail_reason`` 的结果（与非 dry-run 路径的隔离取舍
    一致）。"""
    _seed_project("p-dry-isolate-2", props_list=[
        {"name": "道具坏", "appearance_canonical": "红色、圆形、带柄把", "aliases": []},
    ])

    async def _raise(*_a, **_k):
        raise ValueError("模型输出 JSON 解析失败：截断")
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _raise)

    result = await card_audit.audit_one_prop_card("p-dry-isolate-2", "道具坏", dry_run=True)
    assert result["failed"] is True
    assert "截断" in result["error"]
    assert result["prop_name"] == "道具坏"
