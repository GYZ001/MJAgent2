"""EP-01 第二阶段：``app/orgs/api.py`` REST 路由的行为断言。

一律"做一次真实操作,看它是否真被挡住/真被放行"，不写"检查某字段等于某值"
（同 ``tests/test_org_rbac_matrix.py`` 文件头的规矩）。鉴权模型分两档，各自
用真实 HTTP 请求验证：
- 团队/角色写端点要求 ``_require_org_admin``（系统管理员或本组织 org_admin）；
- 项目授权写端点要求 ``_require_project_manage_access``（项目所有者/组织
  org_admin/系统管理员，看得见但角色不够的协作者应得到 403 而非静默放行）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.sessions import create_session
from app.db import get_conn
from app.main import app
from app.orgs import service as orgs_service
from app.orgs import store as orgs_store
from tests.rbac_isolation_helpers import _mk_project, _mk_user

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


def _set_user_org(conn, user_id: str, org_id: str) -> None:
    conn.execute("UPDATE users SET org_id=? WHERE id=?", (org_id, user_id))


def _login(user_id: str) -> dict[str, str]:
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


def _make_org_admin(conn, username: str) -> str:
    user_id = _mk_user(conn, username)
    _set_user_org(conn, user_id, orgs_store.ORG_DEFAULT_ID)
    org_admin_role = orgs_store.get_role_by_key(conn, None, "org_admin")
    team_id = orgs_service.create_team(
        org_id=orgs_store.ORG_DEFAULT_ID, name=f"{username}-admin-team", description=None, created_by="test",
    )
    orgs_service.add_team_members(team_id=team_id, members=[(user_id, org_admin_role["id"])], created_by="test")
    conn.commit()
    return user_id


def _make_plain_org_user(conn, username: str) -> str:
    user_id = _mk_user(conn, username)
    _set_user_org(conn, user_id, orgs_store.ORG_DEFAULT_ID)
    conn.commit()
    return user_id


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/orgs/current
# ---------------------------------------------------------------------------


def test_get_current_org_reports_org_and_admin_flag(client: TestClient) -> None:
    conn = get_conn()
    admin = _make_org_admin(conn, "orgs-api-current-admin")
    resp = client.get("/api/orgs/current", headers=_login(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org"]["id"] == orgs_store.ORG_DEFAULT_ID
    assert body["is_org_admin"] is True
    assert body["role_governed"] is True


# ---------------------------------------------------------------------------
# /api/teams
# ---------------------------------------------------------------------------


def test_non_admin_cannot_create_team(client: TestClient) -> None:
    conn = get_conn()
    plain = _make_plain_org_user(conn, "orgs-api-plain-1")
    resp = client.post("/api/teams", json={"name": "偷偷建的团队"}, headers=_login(plain))
    assert resp.status_code == 403


def test_org_admin_can_create_update_and_manage_team_members(client: TestClient) -> None:
    conn = get_conn()
    admin = _make_org_admin(conn, "orgs-api-team-admin")
    member = _mk_user(conn, "orgs-api-team-member")
    conn.commit()
    headers = _login(admin)

    created = client.post(
        "/api/teams", json={"name": "剪辑组", "description": "外包剪辑"}, headers=headers,
    )
    assert created.status_code == 200, created.text
    team = created.json()
    team_id = team["id"]
    assert team["name"] == "剪辑组"
    assert team["members"] == []

    renamed = client.put(
        "/api/teams/" + team_id, json={"name": "剪辑组（新）", "status": "disabled"}, headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "剪辑组（新）"
    assert renamed.json()["status"] == "disabled"

    viewer_role = orgs_store.get_role_by_key(conn, None, "viewer")
    added = client.post(
        "/api/teams/" + team_id + "/members",
        json=[{"user_id": member, "role_id": viewer_role["id"]}],
        headers=headers,
    )
    assert added.status_code == 200, added.text
    assert [m["user_id"] for m in added.json()["members"]] == [member]

    removed = client.delete(f"/api/teams/{team_id}/members/{member}", headers=headers)
    assert removed.status_code == 200, removed.text
    assert removed.json()["members"] == []


def test_team_in_other_org_is_404_not_403(client: TestClient) -> None:
    conn = get_conn()
    other_org = orgs_service.create_org(name="团队隔离测试组织", created_by="test")
    other_team_id = orgs_service.create_team(
        org_id=other_org, name="别的组织的团队", description=None, created_by="test",
    )
    admin = _make_org_admin(conn, "orgs-api-cross-org-admin")

    resp = client.put(
        "/api/teams/" + other_team_id, json={"name": "改名"}, headers=_login(admin),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/roles ＋ /api/permissions
# ---------------------------------------------------------------------------


def test_list_roles_includes_builtin_and_permission_catalog(client: TestClient) -> None:
    conn = get_conn()
    admin = _make_org_admin(conn, "orgs-api-roles-list-admin")
    resp = client.get("/api/roles", headers=_login(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    keys = {item["key"] for item in body["items"]}
    assert {"org_admin", "producer", "reviewer", "viewer", "owner"} <= keys
    assert body["permission_catalog"], "permission_catalog 不应为空"


def test_get_permissions_returns_catalog_with_metadata(client: TestClient) -> None:
    conn = get_conn()
    plain = _make_plain_org_user(conn, "orgs-api-permissions-plain")
    resp = client.get("/api/permissions", headers=_login(plain))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert any(item["key"] == "video.generate_shot" for item in items)
    sample = next(item for item in items if item["key"] == "video.generate_shot")
    assert sample["title"]
    assert sample["risk"]


def test_create_role_rejects_unknown_permission_key(client: TestClient) -> None:
    conn = get_conn()
    admin = _make_org_admin(conn, "orgs-api-role-422-admin")
    resp = client.post(
        "/api/roles",
        json={"key": "bogus", "name": "假角色", "permission_keys": ["not.a.real.permission"]},
        headers=_login(admin),
    )
    assert resp.status_code == 422


def test_create_role_from_template_inherits_permissions(client: TestClient) -> None:
    conn = get_conn()
    admin = _make_org_admin(conn, "orgs-api-role-template-admin")
    resp = client.post(
        "/api/roles",
        json={"key": "reviewer-copy", "name": "审校复制", "from_template": "reviewer"},
        headers=_login(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "storyboard.confirm" in body["permission_keys"]
    assert "video.generate_shot" not in body["permission_keys"]


def test_builtin_role_permission_edit_is_rejected_via_api(client: TestClient) -> None:
    conn = get_conn()
    admin = _make_org_admin(conn, "orgs-api-builtin-edit-admin")
    reviewer_role = orgs_store.get_role_by_key(conn, None, "reviewer")
    resp = client.put(
        "/api/roles/" + reviewer_role["id"], json={"permission_keys": []}, headers=_login(admin),
    )
    assert resp.status_code == 422


def test_delete_referenced_role_returns_409_with_referrers_then_succeeds(client: TestClient) -> None:
    conn = get_conn()
    admin = _make_org_admin(conn, "orgs-api-role-delete-admin")
    headers = _login(admin)
    created = client.post(
        "/api/roles",
        json={"key": "temp-stopper", "name": "临时停止者", "permission_keys": ["video.stop_shot"]},
        headers=headers,
    )
    role_id = created.json()["id"]
    team_id = orgs_service.create_team(
        org_id=orgs_store.ORG_DEFAULT_ID, name="临时引用团队", description=None, created_by="test",
    )
    other_user = _mk_user(conn, "orgs-api-role-delete-member")
    conn.commit()
    orgs_service.add_team_members(team_id=team_id, members=[(other_user, role_id)], created_by="test")

    conflict = client.delete("/api/roles/" + role_id, headers=headers)
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["team_members"], "409 响应体必须列出引用方，不能只给数字"
    assert detail["team_members"][0]["team_id"] == team_id

    orgs_service.remove_team_member(team_id=team_id, user_id=other_user)
    ok = client.delete("/api/roles/" + role_id, headers=headers)
    assert ok.status_code == 200, ok.text


def test_role_in_other_org_is_404(client: TestClient) -> None:
    conn = get_conn()
    other_org = orgs_service.create_org(name="角色隔离测试组织", created_by="test")
    other_role_id = orgs_service.create_custom_role(
        org_id=other_org, key="foreign", name="别的组织的角色", description=None,
        permission_keys=frozenset(), created_by="test",
    )
    admin = _make_org_admin(conn, "orgs-api-role-cross-org-admin")
    resp = client.delete("/api/roles/" + other_role_id, headers=_login(admin))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/projects/{project_id}/grants
# ---------------------------------------------------------------------------


def test_project_owner_can_grant_and_revoke_access(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, "orgs-api-grant-owner")
    grantee = _mk_user(conn, "orgs-api-grant-target")
    _mk_project(conn, "proj_orgs_api_grant", owner)
    conn.commit()
    viewer_role = orgs_store.get_role_by_key(conn, None, "viewer")
    headers = _login(owner)

    created = client.post(
        "/api/projects/proj_orgs_api_grant/grants",
        json={"subject_type": "user", "subject_id": grantee, "role_id": viewer_role["id"]},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    assert any(g["subject_id"] == grantee for g in created.json()["items"])

    assert client.get(
        "/api/projects/proj_orgs_api_grant", headers=_login(grantee)
    ).status_code == 200

    revoked = client.delete(
        f"/api/projects/proj_orgs_api_grant/grants/user/{grantee}", headers=headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert client.get(
        "/api/projects/proj_orgs_api_grant", headers=_login(grantee)
    ).status_code == 404


def test_granted_viewer_cannot_manage_grants_of_project_they_do_not_own(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, "orgs-api-grant-owner-2")
    viewer_user = _mk_user(conn, "orgs-api-grant-viewer-2")
    bystander = _mk_user(conn, "orgs-api-grant-bystander-2")
    _mk_project(conn, "proj_orgs_api_grant_403", owner)
    conn.commit()
    viewer_role = orgs_store.get_role_by_key(conn, None, "viewer")
    orgs_service.grant_project_access(
        project_id="proj_orgs_api_grant_403", subject_type="user", subject_id=viewer_user,
        role_id=viewer_role["id"], created_by="test",
    )

    resp = client.post(
        "/api/projects/proj_orgs_api_grant_403/grants",
        json={"subject_type": "user", "subject_id": bystander, "role_id": viewer_role["id"]},
        headers=_login(viewer_user),
    )
    assert resp.status_code == 403


def test_delete_nonexistent_grant_is_404(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, "orgs-api-grant-owner-3")
    _mk_project(conn, "proj_orgs_api_grant_404", owner)
    conn.commit()
    resp = client.delete(
        "/api/projects/proj_orgs_api_grant_404/grants/user/nobody", headers=_login(owner),
    )
    assert resp.status_code == 404
