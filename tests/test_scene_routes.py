from app.main import app


def test_scene_library_routes_accept_post() -> None:
    methods_by_path = {
        getattr(route, "path", ""): set(getattr(route, "methods", set()))
        for route in app.routes
    }

    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-bible"]
    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-refs"]
    assert "POST" in methods_by_path["/api/projects/{project_id}/scene-refs/cancel"]
