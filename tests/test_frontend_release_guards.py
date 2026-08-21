from pathlib import Path

import pytest


def test_frontend_contains_no_local_debug_event_post() -> None:
    root = Path(__file__).resolve().parents[1] / "frontend"
    forbidden = "127.0.0.1:7778/event"
    offenders = []
    for directory in (root / "src", root / "dist"):
        for path in directory.rglob("*"):
            if path.is_file() and forbidden in path.read_text(
                encoding="utf-8",
                errors="ignore",
            ):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


def _dist_ready() -> bool:
    root = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    return (root / "index.html").is_file()


@pytest.mark.skipif(not _dist_ready(), reason="未构建 frontend/dist；先跑 scripts/dev.sh build")
def test_spa_deep_links_fall_back_to_index() -> None:
    """构建产物必须能直接刷新深链。

    前端是 path-based 路由，裸 StaticFiles 对 /projects/p1/board 只会 404，
    外部访问改走 :8230 后一刷新就白屏。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        for path in (
            "/",
            "/projects/p1/board",
            "/projects/p1/episodes/e1/wall",
            "/projects/p1/observability/jobs",
            "/system/settings",
            "/workspaces",
        ):
            response = client.get(path)
            assert response.status_code == 200, path
            assert '<div id="root">' in response.text, path


@pytest.mark.skipif(not _dist_ready(), reason="未构建 frontend/dist；先跑 scripts/dev.sh build")
def test_unknown_api_paths_do_not_fall_back_to_html() -> None:
    """回落不得吞掉 /api、/media、/mcp 的 404，否则前端错误处理会拿到 HTML。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        for path in ("/api/does-not-exist", "/media/does-not-exist.jpg", "/mcp/does-not-exist"):
            response = client.get(path)
            assert response.status_code == 404, f"{path} -> {response.status_code}"
            assert '<div id="root">' not in response.text, path


@pytest.mark.skipif(not _dist_ready(), reason="未构建 frontend/dist；先跑 scripts/dev.sh build")
def test_fingerprinted_assets_are_immutably_cached() -> None:
    """vite 产物带内容指纹，长缓存才能让重复访问不再回源；index.html 必须每次回源。"""
    from fastapi.testclient import TestClient

    from app.main import app

    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "assets"
    asset = next(path for path in dist.iterdir() if path.suffix == ".js")
    with TestClient(app) as client:
        hashed = client.get(f"/assets/{asset.name}")
        assert hashed.status_code == 200
        assert "immutable" in hashed.headers.get("cache-control", "")

        index = client.get("/index.html")
        assert "immutable" not in (index.headers.get("cache-control") or "")
