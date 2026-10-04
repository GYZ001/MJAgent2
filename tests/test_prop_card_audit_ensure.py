"""道具卡「按现行规则复核」的三个触发点接线、手动入口与 dry-run（``app.props.
card_audit``/``card_audit_ensure``/``app.domain.bible_ops.props_api``）。

与 ``tests/test_prop_card_audit.py``（子句/别名代码核验、三类真实场景、原子
重出图、CAS/规则版本/重试上限）互补：那个文件钉住"复核本身算得对不对"，这
个文件钉住"复核在产品里接在哪三个地方、手动入口与 dry-run 入口是否真的不
写库不出图"。背景见本次派单（2026-10-03，proj_ca86b15ab7d7 实测）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.db import get_conn, now
from app.props import card_audit, card_audit_ensure, card_audit_store, judge
from app.props import card_pending_ensure as ensure_mod
from app.props.card_pending_scan import load_episode_shot_rows
from app.schemas import Bible


def _seed_project(project_id: str, *, props_list: list[dict] | None = None, style: str = "写实") -> None:
    bible = {
        "characters": [], "scenes": [], "props": props_list or [],
        "world": {"era": "", "genre": "", "visual_style_canonical": style},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), now()),
    )
    conn.commit()


def _seed_episode(project_id: str, episode_id: str, episode_no: int) -> None:
    conn = get_conn()
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


def _seed_shot(episode_id: str, shot_no: int, props: list[dict]) -> None:
    segment = {"resources": {"props": props}}
    conn = get_conn()
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
        (f"{episode_id}_s{shot_no}", episode_id, shot_no, 10,
         json.dumps({"storyboard_pack_segment": segment}, ensure_ascii=False)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 7) card_audit_ensure：生成闸门懒复核（触发点②）
# ---------------------------------------------------------------------------

def test_stale_audit_props_for_shots_flags_card_without_audit_record() -> None:
    _seed_project("p-stale-1", props_list=[{"name": "白瓷杯", "appearance_canonical": "x", "aliases": []}])
    _seed_episode("p-stale-1", "ep-stale-1", 1)
    _seed_shot("ep-stale-1", 1, [{"label": "白瓷杯", "description": "a"}])
    conn = get_conn()
    bible = Bible.model_validate_json(conn.execute("SELECT bible_json FROM projects WHERE id='p-stale-1'").fetchone()["bible_json"])
    shot_rows = load_episode_shot_rows(conn, "ep-stale-1")
    stale = card_audit_ensure.stale_audit_props_for_shots(conn, "p-stale-1", bible, shot_rows, None)
    assert stale == ["白瓷杯"]


def test_stale_audit_props_for_shots_excludes_card_already_ready_under_current_version() -> None:
    _seed_project("p-stale-2", props_list=[{"name": "白瓷杯", "appearance_canonical": "x", "aliases": []}])
    _seed_episode("p-stale-2", "ep-stale-2", 1)
    _seed_shot("ep-stale-2", 1, [{"label": "白瓷杯", "description": "a"}])
    conn = get_conn()
    card_audit_store.ensure_tables_on_connection(conn)
    card_audit_store.insert_running(
        conn, row_id="r1", project_id="p-stale-2", prop_name="白瓷杯",
        rules_version=judge.PROP_CARD_RULES_VERSION, stamp=now(),
    )
    card_audit_store.update_ready(
        conn, row_id="r1", old_appearance="x", new_appearance="x", removed_clauses=[], removed_aliases=[],
        reimaged=False, feature_shortfall=False, stamp=now(),
    )
    conn.commit()
    bible = Bible.model_validate_json(conn.execute("SELECT bible_json FROM projects WHERE id='p-stale-2'").fetchone()["bible_json"])
    shot_rows = load_episode_shot_rows(conn, "ep-stale-2")
    stale = card_audit_ensure.stale_audit_props_for_shots(conn, "p-stale-2", bible, shot_rows, None)
    assert stale == []


async def test_pending_prop_card_gate_blocks_on_stale_audit_once_card_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """卡已建好（没有缺卡候选）时，闸门第二段改查复核版本是否落后——落后同样拦住并后台发起复核。"""
    launched = {"n": 0}
    def _counting_launch(**_kwargs):
        launched["n"] += 1
    monkeypatch.setattr(card_audit, "launch_background_audit", _counting_launch)
    img = tmp_path / "cup.png"
    img.write_bytes(b"x")
    _seed_project("p-gate-audit-1", props_list=[{
        "name": "白瓷杯", "appearance_canonical": "x", "aliases": [], "ref_image_path": str(img),
    }])
    _seed_episode("p-gate-audit-1", "ep-gate-audit-1", 1)
    _seed_shot("ep-gate-audit-1", 1, [{"label": "白瓷杯", "description": "a"}])
    _seed_shot("ep-gate-audit-1", 2, [{"label": "白瓷杯", "description": "b"}])
    conn = get_conn()
    from app.props.store import upsert_prop_reference
    upsert_prop_reference(
        conn, "p-gate-audit-1", "白瓷杯", 1, appearance="x", image_path=str(img),
        prompt="p", status="ready", qa={},
    )
    conn.commit()
    message = await ensure_mod.pending_prop_card_gate("p-gate-audit-1", "ep-gate-audit-1")
    assert message is not None and "复核" in message
    assert launched["n"] == 1


# ---------------------------------------------------------------------------
# 8) 触发点①：两条建卡路径都会在建卡成功后产生一条复核记录
# ---------------------------------------------------------------------------

async def test_ensure_props_for_labels_creates_audit_record_after_new_card(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.props import judge as judge_mod, service

    new_appearance = "灰色帆布材质、边角磨损、铜扣锁头"

    async def _fake_chat(_messages, **kwargs):
        if (kwargs.get("call_meta") or {}).get("stage") == "audit_prop_card":
            # 必须对 new_appearance 切出的每条子句都显式给出"不删除"判定——
            # 空列表会被"覆盖不全"核验判定成失败重试，不是"全部保留"。
            clause_count = len(judge_mod.split_appearance_clauses(new_appearance))
            return SimpleNamespace(
                clauses=[{"index": i + 1, "remove": False} for i in range(clause_count)], aliases=[],
            )
        return SimpleNamespace(appearance_canonical=new_appearance, aliases=[])

    async def _fake_image(project_id, name, _prompt):
        return f"/fake/{project_id}/{name}.png"

    monkeypatch.setattr(judge_mod.model_gateway, "chat_structured", _fake_chat)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_image)
    _seed_project("p-trigger-1")
    result = await service.ensure_props_for_labels(
        "p-trigger-1", 1, [{"label": "旧猫包", "description": "旧猫包", "segment_indexes": [2, 9]}],
        cards_with_prior_evidence=frozenset(),
    )
    assert [item["name"] for item in result["added"]] == ["旧猫包"]
    row = card_audit_store.get_audit(get_conn(), project_id="p-trigger-1", prop_name="旧猫包")
    assert row is not None and row["status"] == "ready"


# ---------------------------------------------------------------------------
# 9) 手动入口：POST /props/audit（后台受理）+ GET 结果
# ---------------------------------------------------------------------------

async def test_audit_props_route_accepts_and_returns_prop_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.domain.bible_ops import props_api

    def _fake_launch(project_id: str) -> list[str]:
        assert project_id == "p-manual-1"
        return ["道具A", "道具B"]
    monkeypatch.setattr(props_api, "launch_background_audit_for_project", _fake_launch)
    _seed_project("p-manual-1", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    response = await props_api.audit_props("p-manual-1")
    assert response == {"project_id": "p-manual-1", "accepted": ["道具A", "道具B"]}


async def test_audit_props_route_missing_bible_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.domain.bible_ops import props_api

    def _raise_missing(_project_id: str) -> list[str]:
        raise ValueError("项目不存在或人物谱未初始化")
    monkeypatch.setattr(props_api, "launch_background_audit_for_project", _raise_missing)
    _seed_project("p-manual-2")
    with pytest.raises(HTTPException) as excinfo:
        await props_api.audit_props("p-manual-2")
    assert excinfo.value.status_code == 409


async def test_list_prop_audits_route_returns_records() -> None:
    from app.domain.bible_ops import props_api

    _seed_project("p-manual-3", props_list=[{"name": "道具A", "appearance_canonical": "x", "aliases": []}])
    conn = get_conn()
    card_audit_store.ensure_tables_on_connection(conn)
    card_audit_store.insert_running(
        conn, row_id="r1", project_id="p-manual-3", prop_name="道具A",
        rules_version=judge.PROP_CARD_RULES_VERSION, stamp=now(),
    )
    card_audit_store.update_ready(
        conn, row_id="r1", old_appearance="x", new_appearance="x", removed_clauses=[], removed_aliases=[],
        reimaged=False, feature_shortfall=False, stamp=now(),
    )
    conn.commit()
    response = await props_api.list_prop_audits("p-manual-3")
    assert response["items"][0]["prop_name"] == "道具A"
    assert response["items"][0]["status"] == "ready"


# ---------------------------------------------------------------------------
# 10) dry-run：只跑模型判定 + 代码核验，不写库不出图
# ---------------------------------------------------------------------------

async def test_audit_project_prop_cards_dry_run_does_not_write_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_prop, _clauses, _owner_catalog_text=None, **_kwargs):
        return {"clauses": [], "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)
    _seed_project("p-dryrun-1", props_list=[
        {"name": "道具A", "appearance_canonical": "红色、圆形、带柄把", "aliases": []},
        {"name": "道具B", "appearance_canonical": "蓝色、方形、带提手", "aliases": []},
    ])
    result = await card_audit.audit_project_prop_cards("p-dryrun-1", dry_run=True)
    assert result["dry_run"] is True
    assert {r["prop_name"] for r in result["results"]} == {"道具A", "道具B"}
    assert card_audit_store.list_audits(get_conn(), project_id="p-dryrun-1") == []


# ---------------------------------------------------------------------------
# 11) scripts/prop_card_audit_dry_run.py：命令行外壳不写库不出图
# ---------------------------------------------------------------------------

async def test_dry_run_script_run_filters_by_prop_name_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    script = importlib.import_module("prop_card_audit_dry_run")

    async def _fake(_prop, _clauses, _owner_catalog_text=None, **_kwargs):
        return {"clauses": [], "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)
    _seed_project("p-script-1", props_list=[
        {"name": "道具A", "appearance_canonical": "红色、圆形、带柄把", "aliases": []},
        {"name": "道具B", "appearance_canonical": "蓝色、方形、带提手", "aliases": []},
    ])

    output = await script._run("p-script-1", ["道具A"])

    assert [r["prop_name"] for r in output["results"]] == ["道具A"]
    assert card_audit_store.list_audits(get_conn(), project_id="p-script-1") == []


async def test_dry_run_script_run_missing_prop_reports_error_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    script = importlib.import_module("prop_card_audit_dry_run")

    async def _fake(_prop, _clauses, _owner_catalog_text=None, **_kwargs):
        return {"clauses": [], "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)
    _seed_project("p-script-2")

    output = await script._run("p-script-2", ["不存在"])

    assert output["results"][0]["error"]
