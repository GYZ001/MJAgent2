"""EP-03 §6/§8 邀请链接的端到端验收：全部经 HTTP，只用裸 SQL/orgs_store 铺造
数据（团队/角色本身不是被测行为），行为断言一律"做一次真实操作看它是否真被
挡住/真的生效"。

覆盖：签发 + 一次性 token 只在响应里出现一次、预览、接受成功并直接登录、
密码策略不绕过（弱口令 422）、三类失效场景各自 410（过期/重复使用/撤销后）、
组织停用/角色被删时接受被拒并给出路、撤销的幂等与"已接受不可撤销"、
库中无明文（password_history 与 users.password_hash 只存哈希、
operation_audit.args_json 不含明文）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app
from app.orgs import schema as orgs_schema
from app.orgs import store as orgs_store
from app.provisioning import invitations as invitations_domain

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}
_STRONG_PW = "Correct-Horse-9battery"


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


def _headers_for(user_id: str) -> dict[str, str]:
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


@pytest.fixture()
def admin() -> dict:
    conn = get_conn()
    admin_id = _mk_user(conn, "root-invite", is_system_admin=True)
    conn.commit()
    return {"id": admin_id, "headers": _headers_for(admin_id)}


@pytest.fixture()
def team_and_role() -> dict:
    orgs_schema.ensure_schema()
    conn = get_conn()
    team_id = orgs_store.create_team(
        conn, org_id=orgs_store.ORG_DEFAULT_ID, name=f"team-{new_id('t')}",
        description=None, created_by="test",
    )
    role = orgs_store.get_role_by_key(conn, None, "member") or orgs_store.get_role_by_key(conn, None, "viewer")
    if role is None:
        role_id = orgs_store.create_role(
            conn, org_id=None, key="invite_test_role", name="邀请测试角色",
            description=None, builtin=False, created_by="test",
        )
    else:
        role_id = role["id"]
    conn.commit()
    return {"team_id": team_id, "role_id": role_id}


def _create_invitation(client: TestClient, admin: dict, **extra) -> dict:
    body = {"username": f"invitee-{new_id('u')}", "display_name": "被邀请人"}
    body.update(extra)
    resp = client.post("/api/system/invitations", json=body, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _preview(client: TestClient, token: str):
    return client.post("/api/invite/preview", json={"token": token}, headers=_HEADERS)


def _accept(client: TestClient, token: str, password: str = _STRONG_PW):
    return client.post("/api/invite/accept", json={"token": token, "password": password}, headers=_HEADERS)


def test_create_invitation_returns_token_once_and_list_hides_it(client: TestClient, admin: dict):
    created = _create_invitation(client, admin)
    assert created["token"]
    assert created["status"] == "pending"

    listed = client.get("/api/system/invitations", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    match = next(i for i in items if i["id"] == created["id"])
    assert "token" not in match
    assert "token_hash" not in match


def test_preview_then_accept_succeeds_and_logs_in(client: TestClient, admin: dict):
    created = _create_invitation(client, admin)
    token = created["token"]

    preview = _preview(client, token)
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "pending"
    assert preview.json()["username"] == created["username"]

    accept = _accept(client, token)
    assert accept.status_code == 200, accept.text
    body = accept.json()
    assert body["session_token"]
    assert body["username"] == created["username"]

    me = client.get("/api/auth/me", headers={**_HEADERS, "X-Manju-Session": body["session_token"]})
    assert me.status_code == 200, me.text
    assert me.json()["user"]["username"] == created["username"]


def test_accept_rejects_weak_password_with_specific_violations(client: TestClient, admin: dict):
    created = _create_invitation(client, admin)
    resp = _accept(client, created["token"], "abc")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "weak_password"
    assert detail["violations"], "必须逐条列出具体不满足的规则，不是笼统一句话"


def test_accept_twice_second_time_is_410(client: TestClient, admin: dict):
    created = _create_invitation(client, admin)
    token = created["token"]
    first = _accept(client, token)
    assert first.status_code == 200, first.text
    second = _accept(client, token)
    assert second.status_code == 410, second.text


def test_accept_expired_invitation_is_410(client: TestClient, admin: dict):
    created = _create_invitation(client, admin)
    conn = get_conn()
    conn.execute("UPDATE user_invitations SET expires_at=? WHERE id=?", (now() - 10, created["id"]))
    conn.commit()
    resp = _accept(client, created["token"])
    assert resp.status_code == 410, resp.text


def test_accept_revoked_invitation_is_410(client: TestClient, admin: dict):
    created = _create_invitation(client, admin)
    revoke = client.post(f"/api/system/invitations/{created['id']}/revoke", headers=admin["headers"])
    assert revoke.status_code == 200, revoke.text
    resp = _accept(client, created["token"])
    assert resp.status_code == 410, resp.text


def test_revoke_is_idempotent_and_rejects_after_accept(client: TestClient, admin: dict):
    created = _create_invitation(client, admin)
    first = client.post(f"/api/system/invitations/{created['id']}/revoke", headers=admin["headers"])
    assert first.status_code == 200, first.text
    second = client.post(f"/api/system/invitations/{created['id']}/revoke", headers=admin["headers"])
    assert second.status_code == 200, second.text

    accepted = _create_invitation(client, admin)
    ok = _accept(client, accepted["token"])
    assert ok.status_code == 200, ok.text
    revoke_after_accept = client.post(
        f"/api/system/invitations/{accepted['id']}/revoke", headers=admin["headers"]
    )
    assert revoke_after_accept.status_code == 409, revoke_after_accept.text


def test_accept_with_deleted_role_gives_actionable_error(client: TestClient, admin: dict, team_and_role: dict):
    created = _create_invitation(client, admin, team_id=team_and_role["team_id"], role_id=team_and_role["role_id"])
    conn = get_conn()
    conn.execute("DELETE FROM roles WHERE id=?", (team_and_role["role_id"],))
    conn.commit()
    resp = _accept(client, created["token"])
    assert resp.status_code == 409, resp.text
    assert "管理员" in resp.json()["detail"]


def test_accept_with_disabled_team_gives_actionable_error(client: TestClient, admin: dict, team_and_role: dict):
    created = _create_invitation(client, admin, team_id=team_and_role["team_id"], role_id=team_and_role["role_id"])
    conn = get_conn()
    orgs_store.update_team(conn, team_and_role["team_id"], name=None, description=None, status="disabled")
    conn.commit()
    resp = _accept(client, created["token"])
    assert resp.status_code == 409, resp.text
    assert "管理员" in resp.json()["detail"]


def test_accept_assigns_team_and_role_on_success(client: TestClient, admin: dict, team_and_role: dict):
    created = _create_invitation(client, admin, team_id=team_and_role["team_id"], role_id=team_and_role["role_id"])
    resp = _accept(client, created["token"])
    assert resp.status_code == 200, resp.text
    conn = get_conn()
    member = conn.execute(
        "SELECT role_id FROM team_members WHERE team_id=? AND user_id=?",
        (team_and_role["team_id"], resp.json()["user_id"]),
    ).fetchone()
    assert member is not None
    assert member["role_id"] == team_and_role["role_id"]


def test_no_plaintext_password_anywhere_after_accept(client: TestClient, admin: dict):
    created = _create_invitation(client, admin)
    token = created["token"]
    resp = _accept(client, token)
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["user_id"]

    conn = get_conn()
    stored_hash = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()["password_hash"]
    assert _STRONG_PW not in stored_hash
    assert stored_hash.startswith("scrypt$")

    rows = conn.execute(
        "SELECT args_json FROM operation_audit WHERE event LIKE 'provisioning.invitation%'"
    ).fetchall()
    assert rows, "邀请动作必须留痕"
    for row in rows:
        assert _STRONG_PW not in (row["args_json"] or "")

    # token 与口令同属一次性凭证：既不能落进 args_json，也不能落进任何一条
    # operation_audit 行的 path/target（POST /api/invite/accept 本身没有路径
    # 参数——token 走请求体，见 app.provisioning.invite_api 模块文档）。
    all_rows = conn.execute(
        "SELECT path, target, args_json FROM operation_audit WHERE path LIKE '/api/invite%'"
    ).fetchall()
    assert all_rows, "邀请接受端点本身也应留下 HTTP 级审计行"
    for row in all_rows:
        assert token not in (row["path"] or "")
        assert token not in (row["target"] or "")
        assert token not in (row["args_json"] or "")
        assert _STRONG_PW not in (row["args_json"] or "")


def test_sweep_expired_purges_only_stale_rows(client: TestClient, admin: dict):
    """过期清理（挂在 app.audit.retention 既有 6 小时巡检，见该模块文档）：
    宽限期外的失效行才真正删除；仍在宽限期内的失效行与尚未过期的 pending
    行都必须原样保留。"""
    stale = _create_invitation(client, admin)
    fresh_expired = _create_invitation(client, admin)
    still_pending = _create_invitation(client, admin)

    conn = get_conn()
    conn.execute(
        "UPDATE user_invitations SET expires_at=? WHERE id=?", (now() - 40 * 86400, stale["id"]),
    )
    conn.execute(
        "UPDATE user_invitations SET expires_at=? WHERE id=?", (now() - 10, fresh_expired["id"]),
    )
    conn.commit()

    deleted = invitations_domain.sweep_expired()
    assert deleted == 1

    remaining_ids = {
        r["id"] for r in conn.execute("SELECT id FROM user_invitations").fetchall()
    }
    assert stale["id"] not in remaining_ids
    assert fresh_expired["id"] in remaining_ids
    assert still_pending["id"] in remaining_ids


def test_create_invitation_team_role_must_be_paired(client: TestClient, admin: dict, team_and_role: dict):
    resp = client.post(
        "/api/system/invitations",
        json={"username": f"paired-{new_id('u')}", "team_id": team_and_role["team_id"]},
        headers=admin["headers"],
    )
    assert resp.status_code == 422, resp.text
