"""人物谱/场景库共用的统一画风配置端点：POST /projects/{id}/bible/style。

覆盖点：
- 画风未变化：幂等短路，不进入报价/确认流程，不产生任何费用。
- 画风有变化：标准预检→确认两段式；确认时**同一次请求内**依次发起人物定妆照
  与场景图两条全量重生成——不是把「要不要触发另一条腿」交给前端等用户以后
  访问某个页面。这是本文件要验证的核心判据（coordinator 反馈：发起动作必须
  落在后端或一次性完成，不能挂在用户会不会访问某个页面上）。
- 场景清单未就绪时人物腿仍正常发起，场景腿因为没有可生成的场景本来就发起
  不了（scene_bible_ready=False），不是静默跳过。
- 反复确认同一报价：幂等重放，不重复触发生成、不重复扣费。
- 不新造删除逻辑：两条腿都是 resume=false 全量重生成，复用既有生成路径。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi import HTTPException

from app.domain import bible_ops


def _make_conn(bible_json: str | None, *, bible_version: int = 0, bible_style_name: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_style_name TEXT)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_style_name) VALUES(?,?,?,?)",
        ("p1", bible_json, bible_version, bible_style_name),
    )
    conn.commit()
    return conn


def _bible_json(style: str, *, scenes: list[dict] | None = None, ref_image_path: str | None = None) -> str:
    return json.dumps({
        "characters": [{
            "name": "甲一", "role": "主角",
            "appearance_canonical": "黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩",
            "ref_image_path": ref_image_path,
        }],
        "world": {"era": "", "genre": "仙侠", "visual_style_canonical": style},
        "scenes": scenes or [],
    }, ensure_ascii=False)


def _patch_project(monkeypatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(
        bible_ops, "_project_or_404",
        lambda _pid: dict(conn.execute("SELECT * FROM projects WHERE id='p1'").fetchone()),
    )


def _patch_spawns(monkeypatch) -> dict:
    """把两条生成线换成记录调用的桩，不真的起 asyncio 任务。"""
    calls: dict = {"refs": [], "scene_refs": []}

    def fake_start_refs(project_id, only_character, **kwargs):
        calls["refs"].append((project_id, only_character, kwargs))
        return {"status": "accepted", "task_id": f"refs:{project_id}", "run_id": "run-refs"}

    def fake_start_scene_refs(project_id, only_scene, **kwargs):
        calls["scene_refs"].append((project_id, only_scene, kwargs))
        return True

    monkeypatch.setattr(bible_ops, "_start_refs_generation", fake_start_refs)
    monkeypatch.setattr(bible_ops, "_start_scene_refs_generation", fake_start_scene_refs)
    return calls


def _confirm_required_quote(monkeypatch, conn, body: dict) -> dict:
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bible_ops.set_bible_visual_style("p1", body))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "PAYMENT_CONFIRM_REQUIRED"
    return exc_info.value.detail["precheck"]


def test_set_bible_style_no_change_short_circuits_without_quote(monkeypatch) -> None:
    prompt = "写实人像摄影质感，虚构数字角色、非真人照片，自然光影，肤质轻度精修，电影质感。"
    conn = _make_conn(_bible_json(prompt), bible_version=1, bible_style_name="精修真人风")
    _patch_project(monkeypatch, conn)
    calls = _patch_spawns(monkeypatch)

    result = asyncio.run(bible_ops.set_bible_visual_style(
        "p1", {"style_name": "精修真人风", "expected_version": 1},
    ))

    assert result["changed"] is False
    assert result["bible_version"] == 1
    assert calls["refs"] == [] and calls["scene_refs"] == [], "画风未变化不该触发任何生成"
    bible_ops._ensure_character_payment_quotes(conn)
    quote_rows = conn.execute("SELECT COUNT(*) c FROM character_payment_quotes").fetchone()
    assert quote_rows["c"] == 0, "画风未变化不该产生任何报价凭证"


def test_set_bible_style_change_requires_confirm_first(monkeypatch) -> None:
    """画风有变化但未带 confirm：必须先拿到合并报价，不能直接生效。"""
    old_prompt = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    conn = _make_conn(_bible_json(old_prompt), bible_version=3, bible_style_name="国漫电影风")
    _patch_project(monkeypatch, conn)
    calls = _patch_spawns(monkeypatch)

    precheck = _confirm_required_quote(monkeypatch, conn, {
        "style_name": "古典水墨风", "expected_version": 3,
    })

    assert precheck["scene_bible_ready"] is False
    assert precheck["scenes"] is None
    assert precheck["characters"]["character_count"] == 1
    assert precheck["total_estimated_cost_cny"] == precheck["characters"]["estimated_cost_cny"]
    assert calls["refs"] == [] and calls["scene_refs"] == [], "未确认前不能发起任何生成"
    row = conn.execute("SELECT bible_version FROM projects WHERE id='p1'").fetchone()
    assert row["bible_version"] == 3, "未确认前不能写库"
    quote_row = conn.execute(
        "SELECT * FROM character_payment_quotes WHERE quote_id=?", (precheck["quote_id"],),
    ).fetchone()
    assert quote_row is not None, "预检必须把报价持久化，否则确认时查不到"


def test_set_bible_style_confirm_spawns_both_legs_in_one_request(monkeypatch) -> None:
    """核心判据：确认一次，人物与场景两条生成线在同一次请求里都被真正发起——
    不依赖用户之后停留在哪个页面、是否访问场景库、浏览器是否还开着。"""
    old_prompt = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    scenes = [
        {"name": "山门", "scene_canonical": "宗门山门前的青石广场，云雾缭绕，古树参天，石阶蜿蜒而上"},
        {"name": "藏经阁", "scene_canonical": "藏经阁内木架林立，昏黄灯火，尘埃浮动，古籍层叠堆放"},
    ]
    conn = _make_conn(_bible_json(old_prompt, scenes=scenes), bible_version=2)
    _patch_project(monkeypatch, conn)
    calls = _patch_spawns(monkeypatch)

    precheck = _confirm_required_quote(monkeypatch, conn, {
        "style_name": "真人摄影风", "expected_version": 2,
    })
    assert precheck["scene_bible_ready"] is True
    assert precheck["scenes"]["scene_count"] == 2
    assert precheck["total_estimated_cost_cny"] == round(
        precheck["characters"]["estimated_cost_cny"] + precheck["scenes"]["estimated_cost_cny"], 2,
    )

    result = asyncio.run(bible_ops.set_bible_visual_style("p1", {
        "style_name": "真人摄影风", "expected_version": 2,
        "confirm": True, "quote_id": precheck["quote_id"],
    }))

    assert result["changed"] is True
    assert result["refs_started"] is True
    assert result["scene_refs_started"] is True
    # 两条腿都在这一次函数调用里被同步发起，不是排队等某个 effect 或某次页面
    # 访问才触发。
    assert len(calls["refs"]) == 1
    assert calls["refs"][0][0] == "p1" and calls["refs"][0][1] is None
    assert calls["refs"][0][2]["resume"] is False
    assert len(calls["scene_refs"]) == 1
    assert calls["scene_refs"][0][0] == "p1"
    assert set(calls["scene_refs"][0][1]) == {"山门", "藏经阁"}
    assert calls["scene_refs"][0][2]["resume"] is False

    row = conn.execute("SELECT bible_version,bible_style_name FROM projects WHERE id='p1'").fetchone()
    assert row["bible_version"] == 3
    assert row["bible_style_name"] == "真人摄影风"


def test_set_bible_style_confirm_idempotent_replay_does_not_respawn(monkeypatch) -> None:
    """反复确认同一报价：第二次是幂等重放，不重复触发生成、不重复扣费。"""
    old_prompt = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    scenes = [{"name": "山门", "scene_canonical": "宗门山门前的青石广场，云雾缭绕，古树参天，石阶蜿蜒而上"}]
    conn = _make_conn(_bible_json(old_prompt, scenes=scenes), bible_version=0)
    _patch_project(monkeypatch, conn)
    calls = _patch_spawns(monkeypatch)

    precheck = _confirm_required_quote(monkeypatch, conn, {
        "style_name": "古典水墨风", "expected_version": 0,
    })
    body = {
        "style_name": "古典水墨风", "expected_version": 0,
        "confirm": True, "quote_id": precheck["quote_id"],
    }

    first = asyncio.run(bible_ops.set_bible_visual_style("p1", body))
    assert first["changed"] is True
    assert len(calls["refs"]) == 1
    assert len(calls["scene_refs"]) == 1

    # 用户网络重试/重复点击确认：同一 quote_id 再来一次。
    second = asyncio.run(bible_ops.set_bible_visual_style("p1", body))
    assert second.get("idempotent_replay") is True
    assert len(calls["refs"]) == 1, "重复确认不该再触发一次定妆照生成"
    assert len(calls["scene_refs"]) == 1, "重复确认不该再触发一次场景图生成"


def test_set_bible_style_scene_not_ready_still_spawns_characters_only(monkeypatch) -> None:
    """场景清单未就绪：人物腿照常发起；场景腿没有可生成的场景，本来就发起
    不了（scene_bible_ready=False），不是静默跳过，响应里明确带出信号。"""
    old_prompt = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    conn = _make_conn(_bible_json(old_prompt), bible_version=0)
    _patch_project(monkeypatch, conn)
    calls = _patch_spawns(monkeypatch)

    precheck = _confirm_required_quote(monkeypatch, conn, {
        "style_name": "古典水墨风", "expected_version": 0,
    })
    assert precheck["scene_bible_ready"] is False
    assert precheck["scenes"] is None

    result = asyncio.run(bible_ops.set_bible_visual_style("p1", {
        "style_name": "古典水墨风", "expected_version": 0,
        "confirm": True, "quote_id": precheck["quote_id"],
    }))

    assert result["changed"] is True
    assert result["scene_bible_ready"] is False
    assert result["refs_started"] is True
    assert result["scene_refs_started"] is False
    assert len(calls["refs"]) == 1
    assert calls["scene_refs"] == [], "没有场景清单时不该尝试发起场景图生成"


def test_set_bible_style_confirm_rejects_stale_or_missing_quote(monkeypatch) -> None:
    old_prompt = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    conn = _make_conn(_bible_json(old_prompt), bible_version=0)
    _patch_project(monkeypatch, conn)
    _patch_spawns(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bible_ops.set_bible_visual_style("p1", {
            "style_name": "古典水墨风", "expected_version": 0,
            "confirm": True, "quote_id": "quote-does-not-exist",
        }))
    assert exc_info.value.status_code == 409


def test_set_bible_style_does_not_touch_existing_character_assets(monkeypatch) -> None:
    """旧画风下已生成的定妆照路径原样保留：切换风格字段本身不做任何清理，
    重新生成走的是既有「新包完成后原子切换」路径，不是先删后建。"""
    old_prompt = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    conn = _make_conn(_bible_json(old_prompt, ref_image_path="/media/refs/jia_yi.png"), bible_version=0)
    _patch_project(monkeypatch, conn)
    _patch_spawns(monkeypatch)

    precheck = _confirm_required_quote(monkeypatch, conn, {
        "style_name": "古典水墨风", "expected_version": 0,
    })
    asyncio.run(bible_ops.set_bible_visual_style("p1", {
        "style_name": "古典水墨风", "expected_version": 0,
        "confirm": True, "quote_id": precheck["quote_id"],
    }))

    row = conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()
    saved = json.loads(row["bible_json"])
    assert saved["characters"][0]["ref_image_path"] == "/media/refs/jia_yi.png", (
        "切换风格字段本身不应清除或改动旧的定妆照路径；重新生成由下游生成任务负责替换"
    )


def test_set_bible_style_requires_existing_bible(monkeypatch) -> None:
    conn = _make_conn(None, bible_version=0)
    _patch_project(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bible_ops.set_bible_visual_style(
            "p1", {"style_name": "国漫电影风", "expected_version": 0},
        ))
    assert exc_info.value.status_code == 409


def test_set_bible_style_rejects_stale_version(monkeypatch) -> None:
    old_prompt = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    conn = _make_conn(_bible_json(old_prompt), bible_version=5)
    _patch_project(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bible_ops.set_bible_visual_style(
            "p1", {"style_name": "真人摄影风", "expected_version": 4},
        ))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "BIBLE_VERSION_CONFLICT"

    row = conn.execute("SELECT bible_version FROM projects WHERE id='p1'").fetchone()
    assert row["bible_version"] == 5, "版本冲突时不能写库"


def test_set_bible_style_rejects_unknown_style_name(monkeypatch) -> None:
    old_prompt = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    conn = _make_conn(_bible_json(old_prompt), bible_version=0)
    _patch_project(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bible_ops.set_bible_visual_style(
            "p1", {"style_name": "赛博朋克风（不存在的预设）", "expected_version": 0},
        ))
    assert exc_info.value.status_code == 422
