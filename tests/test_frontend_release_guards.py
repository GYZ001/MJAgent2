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

        # 只断言「不是 immutable」不够：完全不设 cache-control 也能通过，而那正是
        # 浏览器启发式缓存的触发条件——外壳被缓存后，它引用的旧 chunk 因 immutable
        # 永不更新，发版就再也到不了用户。必须正向要求回源校验。
        for shell in ("/index.html", "/", "/projects/p1/board"):
            response = client.get(shell)
            assert response.status_code == 200, shell
            cache_control = response.headers.get("cache-control") or ""
            assert "no-cache" in cache_control, f"{shell} 缺少回源校验：{cache_control!r}"
            assert "immutable" not in cache_control, shell


@pytest.mark.skipif(not _dist_ready(), reason="未构建 frontend/dist；先跑 scripts/dev.sh build")
def test_stale_asset_requests_404_instead_of_index_html() -> None:
    """指纹对不上的 chunk 必须 404，不能回落 index.html。

    2026-08-25 线上实况：发版后老标签页里还留着旧模块图，点开某页时去拉
    /assets/MonitorPage-<旧指纹>.js。这个文件已经不存在了，如果服务端把
    index.html 当兜底返回，浏览器拿到 200 + text/html，模块加载器报的是
    "'text/html' is not a valid JavaScript MIME type for module script"——
    一句和真实原因无关的错，前端也没法识别成「分包没取到」去自助重载。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        for path in (
            "/assets/MonitorPage-DEADBEEF.js",
            "/assets/index-00000000.css",
            "/assets/nested/thing-00000000.js",
        ):
            response = client.get(path)
            assert response.status_code == 404, path
            assert "text/html" not in response.headers.get("content-type", ""), path
            assert "<div id=\"root\">" not in response.text, path


@pytest.mark.skipif(not _dist_ready(), reason="未构建 frontend/dist；先跑 scripts/dev.sh build")
def test_real_assets_still_serve_after_the_404_guard() -> None:
    """上一条只该拦掉不存在的指纹，真实产物必须照常发得出去。"""
    from fastapi.testclient import TestClient

    from app.main import app

    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "assets"
    entry = next(dist.glob("index-*.js"))
    with TestClient(app) as client:
        response = client.get(f"/assets/{entry.name}")
        assert response.status_code == 200
        assert "javascript" in response.headers.get("content-type", "")
        assert response.headers.get("cache-control") == "public, max-age=31536000, immutable"
