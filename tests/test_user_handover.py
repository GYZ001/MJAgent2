"""EP-03 §5/§8 离职移交的端到端验收：全部经 HTTP，只用裸 SQL 铺造数据
（引导系统管理员、种项目/团队/在途任务——这些不是被测行为本身），行为断言
一律"做一次真实操作看它是否真被挡住/真的生效"。

覆盖：资产查询、未处置资产时删除 409（响应体带清单与 handover 参数）、
移交给用户（owner_user_id 迁移 + project_grants 连带）、移交给团队（owner
清空 + 团队 project_grants，团队成员真的能访问）、移交后删除放行。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app
from app.orgs import service as orgs_service
from app.orgs import store as orgs_store

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _mk_user(conn, username: str, *, is_system_admin: bool = False) -> str:
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status, "
        "is_system_admin, must_change_password, created_at) "
        "VALUES(?,?,?,?,'active',?,0,?)",
        (user_id, username, username, hash_password("pw-" + username), int(is_system_admin), now()),
    )
    return user_id


def _mk_project(conn, project_id: str, owner_user_id: str, *, name: str = "proj") -> None:
    conn.execute(
        "INSERT INTO projects(id, name, status, owner_user_id, created_at) VALUES(?,?,'created',?,?)",
        (project_id, name, owner_user_id, now()),
    )


def _mk_job(conn, job_id: str, project_id: str, status: str = "running") -> None:
    conn.execute(
        "INSERT INTO jobs(id, kind, project_id, status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (job_id, "video", project_id, status, now(), now()),
    )


def _headers_for(user_id: str) -> dict[str, str]:
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


@pytest.fixture()
def admin() -> dict:
    conn = get_conn()
    admin_id = _mk_user(conn, "root-handover", is_system_admin=True)
    conn.commit()
    return {"id": admin_id, "headers": _headers_for(admin_id)}


def test_assets_endpoint_reports_owned_projects_and_in_flight_jobs(client: TestClient, admin: dict):
    conn = get_conn()
    leaver = _mk_user(conn, "leaver-assets")
    _mk_project(conn, "proj-assets-1", leaver, name="项目一")
    _mk_project(conn, "proj-assets-2", leaver, name="项目二")
    _mk_job(conn, "job-assets-1", "proj-assets-1", status="running")
    _mk_job(conn, "job-assets-2", "proj-assets-2", status="succeeded")  # 非在途，不计数
    conn.commit()

    resp = client.get(f"/api/system/users/{leaver}/assets", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owned_projects_count"] == 2
    assert {p["id"] for p in body["owned_projects"]} == {"proj-assets-1", "proj-assets-2"}
    assert body["in_flight_jobs_count"] == 1


def test_delete_blocked_with_409_and_asset_list_when_unresolved(client: TestClient, admin: dict):
    conn = get_conn()
    leaver = _mk_user(conn, "leaver-blocked")
    _mk_project(conn, "proj-blocked-1", leaver)
    conn.commit()

    resp = client.delete(f"/api/system/users/{leaver}", headers=admin["headers"])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["assets"]["owned_projects_count"] == 1
    assert detail["handover_endpoint"] == f"/api/system/users/{leaver}/handover"
    assert "to_user_id" in detail["handover_params"] and "to_team_id" in detail["handover_params"]

    # 账号真的还在（没有被静默删掉）。
    users = client.get("/api/system/users", headers=admin["headers"]).json()["items"]
    assert any(u["id"] == leaver for u in users)


def test_handover_to_user_transfers_ownership_and_new_owner_can_access(client: TestClient, admin: dict):
    conn = get_conn()
    leaver = _mk_user(conn, "leaver-to-user")
    receiver = _mk_user(conn, "receiver-user")
    _mk_project(conn, "proj-handover-user", leaver)
    conn.commit()

    # 移交前：接收者访问不到这个项目。
    receiver_headers = _headers_for(receiver)
    before = client.get("/api/projects/proj-handover-user", headers=receiver_headers)
    assert before.status_code == 404

    resp = client.post(
        f"/api/system/users/{leaver}/handover", headers=admin["headers"],
        json={"to_user_id": receiver},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transferred_projects"] == ["proj-handover-user"]

    # 行为断言：新主人真的能打开这个项目。
    after = client.get("/api/projects/proj-handover-user", headers=receiver_headers)
    assert after.status_code == 200, after.text

    row = conn.execute("SELECT owner_user_id FROM projects WHERE id='proj-handover-user'").fetchone()
    assert row["owner_user_id"] == receiver

    # 名下已无未处置资产，现在可以真正删除了。
    delete_resp = client.delete(f"/api/system/users/{leaver}", headers=admin["headers"])
    assert delete_resp.status_code == 200, delete_resp.text


def test_handover_to_team_grants_access_to_team_member(client: TestClient, admin: dict):
    conn = get_conn()
    leaver = _mk_user(conn, "leaver-to-team")
    member = _mk_user(conn, "team-member-receiver")
    _mk_project(conn, "proj-handover-team", leaver)
    conn.commit()

    org_id = orgs_store.ORG_DEFAULT_ID
    team_id = orgs_service.create_team(org_id=org_id, name="接收团队", description=None, created_by="test")
    viewer_role = orgs_store.get_role_by_key(conn, None, "viewer")
    assert viewer_role is not None
    orgs_service.add_team_members(
        team_id=team_id, members=[(member, viewer_role["id"])], created_by="test",
    )

    member_headers = _headers_for(member)
    before = client.get("/api/projects/proj-handover-team", headers=member_headers)
    assert before.status_code == 404

    resp = client.post(
        f"/api/system/users/{leaver}/handover", headers=admin["headers"],
        json={"to_team_id": team_id},
    )
    assert resp.status_code == 200, resp.text

    row = conn.execute("SELECT owner_user_id FROM projects WHERE id='proj-handover-team'").fetchone()
    assert row["owner_user_id"] == ""

    # 行为断言：团队里原本毫无关系的成员现在能看到这个项目了。
    after = client.get("/api/projects/proj-handover-team", headers=member_headers)
    assert after.status_code == 200, after.text


def test_handover_requires_exactly_one_target(client: TestClient, admin: dict):
    conn = get_conn()
    leaver = _mk_user(conn, "leaver-bad-target")
    conn.commit()

    neither = client.post(f"/api/system/users/{leaver}/handover", headers=admin["headers"], json={})
    assert neither.status_code == 422

    both = client.post(
        f"/api/system/users/{leaver}/handover", headers=admin["headers"],
        json={"to_user_id": admin["id"], "to_team_id": "team_x"},
    )
    assert both.status_code == 422


def test_handover_and_delete_require_system_admin(client: TestClient):
    conn = get_conn()
    plain = _mk_user(conn, "not-admin-handover")
    target = _mk_user(conn, "handover-target-noop")
    conn.commit()
    headers = _headers_for(plain)

    assert client.get(f"/api/system/users/{target}/assets", headers=headers).status_code == 403
    assert client.post(
        f"/api/system/users/{target}/handover", headers=headers, json={"to_user_id": plain}
    ).status_code == 403
    assert client.delete(f"/api/system/users/{target}", headers=headers).status_code == 403
