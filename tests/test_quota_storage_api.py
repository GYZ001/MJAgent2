"""EP-04 第二阶段：存储治理的 HTTP 面（``/api/system/storage/*``）+
``/api/system/usage/*`` 接入 storage_bytes。CLAUDE.md「绕过扫描」要求：查看
采样/候选清单/执行清理全部有 HTTP 入口，本文件用真实 ``TestClient`` 验证，不
直接调用 Python 函数。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn, new_id, now
from app.main import app
from app.quota_policy import storage as quota_storage
from tests.rbac_isolation_helpers import _headers, _mk_project, _mk_user


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _episode_shot_version(conn, project_id: str, *, adopted: bool, video_path=None) -> tuple[str, str]:
    episode_id = new_id("ep")
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, status, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (episode_id, project_id, 1, "E1", "planned", now()),
    )
    shot_id = new_id("shot")
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,?)",
        (shot_id, episode_id, 1, 15),
    )
    version_id = new_id("ver")
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, "
        "video_path, created_at) VALUES(?,?,?,?,?,'succeeded',?,?)",
        (version_id, shot_id, 1, "prompt", new_id("idem"), str(video_path) if video_path else None, now()),
    )
    if adopted:
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.commit()
    return shot_id, version_id


# ---------------------------------------------------------------------------
# 鉴权：本人项目 / 系统管理员放行；无关账号拒绝
# ---------------------------------------------------------------------------


def test_storage_sample_owner_can_read_own_project(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    _mk_project(conn, "proj-x", owner)
    conn.commit()
    quota_storage._write_storage_sample("proj-x", owner, 12345, 0.1, "ok", None)

    resp = client.get("/api/system/storage/sample?project_id=proj-x", headers=_headers(owner))
    assert resp.status_code == 200, resp.text
    assert resp.json()["sample"]["bytes_total"] == 12345


def test_storage_sample_unrelated_user_is_denied(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    stranger = _mk_user(conn, f"stranger-{new_id('u')}")
    _mk_project(conn, "proj-y", owner)
    conn.commit()

    resp = client.get("/api/system/storage/sample?project_id=proj-y", headers=_headers(stranger))
    assert resp.status_code == 403


def test_storage_sample_system_admin_can_read_any_project(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    admin = _mk_user(conn, f"admin-{new_id('u')}", is_system_admin=True)
    _mk_project(conn, "proj-z", owner)
    conn.commit()
    quota_storage._write_storage_sample("proj-z", owner, 777, 0.1, "ok", None)

    resp = client.get("/api/system/storage/sample?project_id=proj-z", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["sample"]["bytes_total"] == 777


def test_storage_sample_missing_project_is_404(client: TestClient) -> None:
    conn = get_conn()
    stranger = _mk_user(conn, f"stranger-{new_id('u')}")
    conn.commit()
    resp = client.get("/api/system/storage/sample?project_id=nope", headers=_headers(stranger))
    assert resp.status_code == 404


def test_storage_sample_never_sampled_returns_null(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    _mk_project(conn, "proj-fresh", owner)
    conn.commit()
    resp = client.get("/api/system/storage/sample?project_id=proj-fresh", headers=_headers(owner))
    assert resp.status_code == 200, resp.text
    assert resp.json()["sample"] is None


# ---------------------------------------------------------------------------
# 清理候选与执行
# ---------------------------------------------------------------------------


def test_cleanup_candidates_endpoint_returns_unadopted_versions(client: TestClient, tmp_path) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    _mk_project(conn, "proj-cc", owner)
    conn.commit()
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * 999)
    _, version_id = _episode_shot_version(conn, "proj-cc", adopted=False, video_path=path)

    resp = client.get("/api/system/storage/cleanup_candidates?project_id=proj-cc", headers=_headers(owner))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["version_id"] == version_id
    assert items[0]["bytes"] == 999


def test_cleanup_endpoint_deletes_confirmed_versions_via_http(client: TestClient, tmp_path) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    _mk_project(conn, "proj-del", owner)
    conn.commit()
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * 500)
    _, version_id = _episode_shot_version(conn, "proj-del", adopted=False, video_path=path)

    resp = client.post(
        "/api/system/storage/cleanup", headers=_headers(owner),
        json={"project_id": "proj-del", "version_ids": [version_id]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == [version_id]
    assert body["bytes_freed"] == 500
    assert not path.exists()


def test_cleanup_endpoint_unrelated_user_cannot_delete(client: TestClient, tmp_path) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    stranger = _mk_user(conn, f"stranger-{new_id('u')}")
    _mk_project(conn, "proj-guard", owner)
    conn.commit()
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * 500)
    _, version_id = _episode_shot_version(conn, "proj-guard", adopted=False, video_path=path)

    resp = client.post(
        "/api/system/storage/cleanup", headers=_headers(stranger),
        json={"project_id": "proj-guard", "version_ids": [version_id]},
    )
    assert resp.status_code == 403
    assert path.exists(), "被拒绝的请求不能删任何文件"


def test_cleanup_endpoint_rejects_empty_project_id(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    conn.commit()
    resp = client.post(
        "/api/system/storage/cleanup", headers=_headers(owner),
        json={"project_id": "", "version_ids": []},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# usage_summary/usage_top 接入 storage_bytes
# ---------------------------------------------------------------------------


def test_usage_summary_includes_storage_bytes_and_sampled_at(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    _mk_project(conn, "proj-usage", owner)
    conn.commit()
    quota_storage._write_storage_sample("proj-usage", owner, 55555, 0.1, "ok", None)

    resp = client.get(f"/api/system/usage/summary?scope=user&id={owner}", headers=_headers(owner))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["usage"]["storage_bytes"] == 55555.0
    assert body["storage_sampled_at"] is not None


def test_usage_top_storage_bytes_supports_project_dimension_only(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, f"owner-{new_id('u')}")
    conn.commit()
    resp_ok = client.get(
        "/api/system/usage/top?dimension=project&resource=storage_bytes", headers=_headers(owner),
    )
    assert resp_ok.status_code == 200, resp_ok.text

    resp_bad = client.get(
        "/api/system/usage/top?dimension=user&resource=storage_bytes", headers=_headers(owner),
    )
    assert resp_bad.status_code == 422
