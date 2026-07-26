import sqlite3

from app import api, task_registry
from app.api import router


def test_scene_library_routes_accept_post() -> None:
    methods_by_path = {
        getattr(route, "path", ""): set(getattr(route, "methods", set()))
        for route in router.routes
    }

    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-bible"]
    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-refs"]
    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-refs/cancel"]
    assert "POST" in methods_by_path[
        "/api/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}/adopt"
    ]


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

    monkeypatch.setattr(api, "get_conn", lambda: conn)
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
