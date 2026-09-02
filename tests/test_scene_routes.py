import asyncio
import json
import sqlite3

import pytest
from fastapi import HTTPException

from app import api, task_registry
from app.api import router
from app.domain import bible_ops
from app.schemas import Scene
from tests.conftest import patch_stages_everywhere as _patch_stages, patch_api_everywhere


def test_scene_library_routes_accept_post() -> None:
    methods_by_path = {
        getattr(route, "path", ""): set(getattr(route, "methods", set()))
        for route in router.routes
    }

    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-bible"]
    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-refs"]
    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-refs/cancel"]
    # 候选采纳路由已随候选图能力退场（用户拍板 2026-09-01）：路由表里不该再有它，
    # 否则就是"能力说撤了、入口还在"的半退场。
    assert "/api/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}/adopt" \
        not in methods_by_path


def test_scene_bible_parent_does_not_block_scene_reference_handoff(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, scene_refs_status TEXT, "
        "scene_refs_error TEXT, scene_refs_target TEXT)"
    )
    conn.execute("INSERT INTO projects VALUES('p', 'running', NULL, NULL)")
    conn.commit()
    spawned: list[tuple[str, str]] = []

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(
        task_registry,
        "active",
        lambda kind, key: kind == "scene_bible" and key == "p",
    )

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.append((kind, key))
        coro.close()
        return None

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    assert api._scene_assets_task_active("p") is True
    assert api._start_scene_refs_generation("p", None) is True
    assert spawned == [("scene_refs", "p")]


def test_scene_bible_formal_request_keeps_confirmed_payload_out_of_legacy_bus(
    monkeypatch,
) -> None:
    async def unexpected_ui_route(_name: str, _args: dict):
        raise AssertionError("formal scene request must not lose its payload in Command Bus")

    monkeypatch.setattr("app.capabilities.dispatch.ui_route", unexpected_ui_route)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _project_id: {"bible_json": "{}"})
    patch_api_everywhere(monkeypatch, "_scene_assets_task_active", lambda _project_id: False)

    with pytest.raises(HTTPException) as preview_required:
        asyncio.run(bible_ops.start_scene_bible(
            "p",
            {"scenes": [], "confirm": True, "idempotency_key": "scene-once"},
        ))

    assert preview_required.value.status_code == 409
    assert preview_required.value.detail["code"] == "SCENE_PREVIEW_REQUIRED"


def test_scene_bible_preview_accepts_scene_list_return(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute("INSERT INTO chapters VALUES('p', 1, '青山脚下')")
    conn.commit()
    bible_json = json.dumps({
        "characters": [],
        "world": {"visual_style_canonical": "国漫风格", "era": "", "genre": "仙侠"},
    }, ensure_ascii=False)

    async def fake_generate_scene_bible(*_args, **_kwargs):
        return [Scene(name="青山脚下", scene_canonical="青山脚下的山路，晨雾弥漫，古树与石阶环绕", location_kind="室外")]

    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _project_id: {"bible_json": bible_json})
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    _patch_stages(monkeypatch, "generate_scene_bible", fake_generate_scene_bible)
    patch_api_everywhere(
        monkeypatch,
        "compute_scene_cost_precheck",
        lambda project_id, **kwargs: {"project_id": project_id, **kwargs},
    )
    patch_api_everywhere(monkeypatch, "_issue_scope_quote", lambda quote: {"quote_id": "quote-scene", **quote})

    result = asyncio.run(bible_ops.preview_scene_bible("p"))

    assert result["scenes"][0]["name"] == "青山脚下"
    assert result["precheck"]["quote_id"] == "quote-scene"
    assert result["generates_images"] is False


def test_scene_bible_preparation_persists_list_without_starting_images(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, scene_refs_status TEXT, scene_refs_error TEXT)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    bible_json = json.dumps({
        "characters": [],
        "world": {"visual_style_canonical": "国漫风格", "era": "", "genre": "仙侠"},
    }, ensure_ascii=False)
    conn.execute("INSERT INTO projects VALUES('p', ?, 'running', NULL)", (bible_json,))
    conn.execute("INSERT INTO chapters VALUES('p', 1, '青山脚下')")
    conn.commit()
    class Recorder:
        run_id = "run-scene-bible"

        def start(self) -> None:
            pass

        async def step(self, *_args, **_kwargs):
            return "step-scene-bible", [
                Scene(name="青山脚下", scene_canonical="青山脚下的山路，晨雾弥漫，古树与石阶环绕", location_kind="室外"),
            ]

        def succeed(self, _message: str, conn=None) -> None:
            pass

        def cancel(self, conn=None) -> None:
            pass

        def fail(self, _exc: Exception, conn=None) -> None:
            raise AssertionError("scene bible should not fail")

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops.WorkflowRecorder, "create", lambda **_kwargs: Recorder())
    patch_api_everywhere(
        monkeypatch,
        "_start_scene_refs_generation",
        lambda *_args, **_kwargs: pytest.fail("场景图片必须在独立费用确认后启动"),
    )

    asyncio.run(bible_ops._scene_bible_task("p"))

    project = conn.execute(
        "SELECT bible_json,scene_refs_status,scene_refs_error FROM projects WHERE id='p'"
    ).fetchone()
    payload = json.loads(project["bible_json"])
    assert payload["scenes"][0]["name"] == "青山脚下"
    assert project["scene_refs_status"] == "idle"
    assert project["scene_refs_error"] is None
