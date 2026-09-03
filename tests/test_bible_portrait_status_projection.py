"""WS13：人物谱页「未出图」角标缺理由的投影测试。

覆盖两层：
1. ``app.domain.bible_ops.portrait_status`` 的纯函数单测（内存造四种
   bible_auto_changes_json 状态，不落库）。
2. 端到端投影：``app.domain.projects.project_detail(view="bible")`` 返回的
   ``bible.characters[]`` 必须带上 portrait_status/portrait_reason，且已出图
   的角色不能被误判成未出图（哪怕队列里恰好有一条陈旧的 pending/failed 记录）。
"""
from __future__ import annotations

import json
import sqlite3

from app import config, db
from app.domain import common, projects
from app.domain.bible_ops.portrait_status import (
    attach_portrait_projection,
    character_portrait_projection,
)
from tests.conftest import patch_projects_everywhere


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


# ---------------------------------------------------------------------------
# 纯函数单测
# ---------------------------------------------------------------------------

def test_character_with_ref_image_url_is_ready_even_with_stale_pending_change() -> None:
    """已经出图的角色不能被队列里一条陈旧的 pending 记录误判成未出图——
    有图优先，是最高优先级的事实来源。"""
    character = {"name": "孟浩", "ref_image_url": "/media/mh.png"}
    changes = [{
        "kind": "new_character", "character": "孟浩",
        "status": "auto_applied_asset_pending", "decision_reason": "戏份不足……",
    }]
    status, reason = character_portrait_projection(character, changes)
    assert status == "ready"
    assert reason == ""


def test_character_with_ready_portrait_segment_but_no_ref_image_url_is_ready() -> None:
    character = {
        "name": "孟浩", "ref_image_url": None,
        "portraits": [{"image_url": "/media/seg.png", "pack_status": "ready"}],
    }
    status, _ = character_portrait_projection(character, [])
    assert status == "ready"


def test_deferred_status_carries_verbatim_decision_reason() -> None:
    """戏份不足路径：decision_reason 必须逐字透出，不能被改写或截断。"""
    reason_text = (
        "戏份不足（原文仅一句话提及/单次在场，未达到在场 ≥2 段或对白+动作齐备的"
        "门槛），人物卡已登记但未自动出图；角色后续如在剧本里实际出场会自动补图，"
        "也可在人物谱页手动生成定妆照"
    )
    character = {"name": "德科", "ref_image_url": None, "portraits": []}
    changes = [{
        "kind": "character_discovery", "character": "德科",
        "status": "auto_applied_asset_pending", "decision_reason": reason_text,
        "created_at": 100,
    }]
    status, reason = character_portrait_projection(character, changes)
    assert status == "deferred"
    assert reason == reason_text


def test_deferred_status_also_covers_the_generic_structural_defer_reason() -> None:
    """跨项目验证实测（B 库 proj_ecabd38b7261/proj_ce9fcf749b23）：桓帝/张角/
    德科等角色的 auto_applied_asset_pending 记录 decision_reason 并不是
    portrait_generation_decision 的"戏份不足"长句，而是身份消歧确认真名后
    generate_portrait=False 的通用兜底句——与戏份多少无关。两种成因都归
    deferred，reason 逐字透出，不得在这两种情形上编造统一的"戏份不足"归因。"""
    character = {"name": "桓帝", "ref_image_url": None, "portraits": []}
    changes = [{
        "kind": "new_character", "character": "桓帝",
        "status": "auto_applied_asset_pending",
        "decision_reason": "人物卡已加入；定妆包等待独立资产环节确认",
        "created_at": 200,
    }]
    status, reason = character_portrait_projection(character, changes)
    assert status == "deferred"
    assert reason == "人物卡已加入；定妆包等待独立资产环节确认"


def test_failed_status_covers_both_asset_and_card_write_failures() -> None:
    character = {"name": "张角", "ref_image_url": None, "portraits": []}
    for failing_status in ("auto_applied_asset_failed", "auto_apply_failed"):
        changes = [{
            "kind": "new_character", "character": "张角",
            "status": failing_status, "decision_reason": "定妆包生成失败[ERR-x]",
        }]
        status, reason = character_portrait_projection(character, changes)
        assert status == "failed"
        assert reason == "定妆包生成失败[ERR-x]"


def test_generating_status_reflects_in_progress_queue_entry() -> None:
    character = {"name": "张宝", "ref_image_url": None, "portraits": []}
    changes = [{
        "kind": "new_bible_character", "character": "张宝", "status": "processing",
    }]
    status, reason = character_portrait_projection(character, changes)
    assert status == "generating"
    assert reason == ""


def test_missing_status_without_change_record_does_not_fabricate_a_reason() -> None:
    """初始批次角色/从未走过 discovery 队列：没有数据就不编造原因（CLAUDE.md
    「不得兜底填充」）。"""
    character = {"name": "神秘人", "ref_image_url": None, "portraits": []}
    status, reason = character_portrait_projection(character, [])
    assert status == "missing"
    assert reason == ""


def test_missing_status_when_change_succeeded_but_image_is_gone() -> None:
    """auto_applied 成功过，但当前确实没有可用图（例如画风切换后被清空）：
    如实标未出图，不能借用一条"生成成功"的旧 decision_reason 冒充失败原因。"""
    character = {"name": "老王", "ref_image_url": None, "portraits": []}
    changes = [{
        "kind": "new_character", "character": "老王", "status": "auto_applied",
        "decision_reason": "AI 判定需要人物卡并已自动生成定妆包",
    }]
    status, reason = character_portrait_projection(character, changes)
    assert status == "missing"
    assert reason == ""


def test_picks_the_most_recently_decided_change_when_several_match() -> None:
    character = {"name": "老李", "ref_image_url": None, "portraits": []}
    changes = [
        {
            "kind": "new_character", "character": "老李",
            "status": "auto_applied_asset_failed", "decision_reason": "旧的失败",
            "created_at": 10, "decided_at": 20,
        },
        {
            "kind": "new_character", "character": "老李",
            "status": "auto_applied_asset_pending", "decision_reason": "戏份不足（最新一次判定）",
            "created_at": 30, "decided_at": 40,
        },
    ]
    status, reason = character_portrait_projection(character, changes)
    assert status == "deferred"
    assert reason == "戏份不足（最新一次判定）"


def test_attach_portrait_projection_tolerates_malformed_json_without_raising() -> None:
    bible = {"characters": [{"name": "A", "ref_image_url": None, "portraits": []}]}
    attach_portrait_projection(bible, "{not valid json")
    assert bible["characters"][0]["portrait_status"] == "missing"
    attach_portrait_projection(bible, None)
    assert bible["characters"][0]["portrait_status"] == "missing"
    attach_portrait_projection(bible, json.dumps({"not": "a list"}))
    assert bible["characters"][0]["portrait_status"] == "missing"


# ---------------------------------------------------------------------------
# 端到端：project_detail(view="bible")
# ---------------------------------------------------------------------------

def _seed_project_with_bible(conn: sqlite3.Connection) -> None:
    bible = {
        "world": {"visual_style_canonical": "国风水墨"},
        "characters": [
            {"name": "孟浩", "role": "主角", "appearance_canonical": "青衫剑客",
             "personality": "", "speech_style": "", "relationships": []},
            {"name": "德科", "role": "配角", "appearance_canonical": "球场队员",
             "personality": "", "speech_style": "", "relationships": []},
            {"name": "张角", "role": "配角", "appearance_canonical": "黄巾道人",
             "personality": "", "speech_style": "", "relationships": []},
            {"name": "张宝", "role": "配角", "appearance_canonical": "黄巾道人",
             "personality": "", "speech_style": "", "relationships": []},
        ],
        "scenes": [],
    }
    changes = [
        {
            "id": "chg1", "kind": "character_discovery", "character": "德科",
            "status": "auto_applied_asset_pending",
            "decision_reason": "戏份不足（原文仅一句话提及），人物卡已登记但未自动出图",
            "created_at": 10,
        },
        {
            "id": "chg2", "kind": "new_character", "character": "张角",
            "status": "auto_applied_asset_failed",
            "decision_reason": "定妆包生成失败[ERR-20260903-abcdef]",
            "created_at": 20,
        },
        {
            "id": "chg3", "kind": "new_bible_character", "character": "张宝",
            "status": "processing", "created_at": 30,
        },
    ]
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json, "
        "bible_auto_changes_json) VALUES('p1','demo','created',1,?,?)",
        (json.dumps(bible, ensure_ascii=False), json.dumps(changes, ensure_ascii=False)),
    )
    conn.commit()


def test_project_detail_bible_view_projects_portrait_status_and_reason(monkeypatch) -> None:
    conn = _conn()
    _seed_project_with_bible(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    patch_projects_everywhere(monkeypatch, "get_conn", lambda: conn)

    payload = projects.project_detail("p1", view="bible")
    by_name = {c["name"]: c for c in payload["bible"]["characters"]}

    # 孟浩没有落盘图、也没有 discovery 队列记录——诚实的"未出图"，无归因原因。
    assert by_name["孟浩"]["portrait_status"] == "missing"
    assert by_name["孟浩"]["portrait_reason"] == ""

    assert by_name["德科"]["portrait_status"] == "deferred"
    assert "戏份不足" in by_name["德科"]["portrait_reason"]

    assert by_name["张角"]["portrait_status"] == "failed"
    assert "定妆包生成失败" in by_name["张角"]["portrait_reason"]

    assert by_name["张宝"]["portrait_status"] == "generating"
    assert by_name["张宝"]["portrait_reason"] == ""


def test_project_detail_bible_view_ready_character_wins_over_stale_pending_record(
    monkeypatch, tmp_path,
) -> None:
    """已经手动补出图的角色（队列里仍留着旧的 pending 记录）必须显示已出图，
    不能因为队列没同步更新就被判成"未出图·戏份不足"。"""
    # build_media_url 只认落在 config.PROJECTS_DIR 下的路径（见 app/media_urls.py），
    # 落盘图必须放进沙盒 PROJECTS_DIR，不能用裸 tmp_path——否则 ref_image_url 恒为
    # None，这个用例就测不出"已出图优先"这条判据。
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    image_path = tmp_path / "de_ke.png"
    image_path.write_bytes(b"fake-png")
    conn = _conn()
    bible = {
        "world": {"visual_style_canonical": "国风水墨"},
        "characters": [
            {"name": "德科", "role": "配角", "appearance_canonical": "球场队员",
             "personality": "", "speech_style": "", "relationships": [],
             "ref_image_path": str(image_path)},
        ],
        "scenes": [],
    }
    changes = [{
        "id": "chg1", "kind": "character_discovery", "character": "德科",
        "status": "auto_applied_asset_pending", "decision_reason": "戏份不足……",
        "created_at": 10,
    }]
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json, "
        "bible_auto_changes_json) VALUES('p1','demo','created',1,?,?)",
        (json.dumps(bible, ensure_ascii=False), json.dumps(changes, ensure_ascii=False)),
    )
    conn.commit()
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    patch_projects_everywhere(monkeypatch, "get_conn", lambda: conn)

    payload = projects.project_detail("p1", view="bible")
    character = payload["bible"]["characters"][0]
    assert character["portrait_status"] == "ready"
    assert character["portrait_reason"] == ""
