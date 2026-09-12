"""EP-02 第一阶段：JIT 自动开户与角色映射、账号绑定/解绑、强制 SSO 开关、
break-glass 应急通道的行为断言（"做一次真实操作，看它是否真被挡住/真被
放行"）。

OIDC 网络边界同 ``test_sso_oidc_flow.py`` 用 ``patch_sso_everywhere`` 打桩；
本文件更关注 ``app.sso.provision``（规则匹配/团队角色落地/审计）与
``app.sso.admin_api``（强制 SSO 开关的连通性自检闸门）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app
from app.orgs import store as orgs_store
from tests.conftest import patch_sso_everywhere
from tests.sso_test_helpers import (
    HEADERS,
    create_oidc_idp,
    default_id_token_payload,
    generate_rsa_keypair,
    read_auth_request,
    sign_id_token,
    start_login,
)

ISSUER = "https://idp.example.com/"
CLIENT_ID = "test-client-id"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _mk_local_user(conn, username: str, password: str, *, is_system_admin: bool = False) -> str:
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, auth_provider, status, "
        "is_system_admin, must_change_password, created_at) VALUES(?,?,?,?,'local','active',?,0,?)",
        (user_id, username, username, hash_password(password), int(is_system_admin), now()),
    )
    conn.commit()
    return user_id


def _login_headers(user_id: str) -> dict[str, str]:
    return {**HEADERS, "X-Manju-Session": create_session(user_id)}


def _login_via_password(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password}, headers=HEADERS)


def _do_oidc_login(client, monkeypatch, idp_id, *, subject: str, raw_claims_overrides=None):
    """跑一遍完整 OIDC 回调，返回响应对象（供调用方检查状态码/Location）。"""
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    state, _location, _query = start_login(client, idp_id)
    auth_req = read_auth_request(conn, state)
    payload = default_id_token_payload(
        issuer=ISSUER, client_id=CLIENT_ID, subject=subject, nonce=auth_req["nonce"],
        **(raw_claims_overrides or {}),
    )
    id_token = sign_id_token(private_key, {"alg": "RS256", "kid": jwk["kid"], "typ": "JWT"}, payload)

    async def fake_exchange_code(endpoints, **kwargs):
        return {"id_token": id_token, "access_token": "fake"}

    async def fake_fetch_jwks(jwks_url, *, force: bool = False):
        return [jwk]

    patch_sso_everywhere(monkeypatch, "exchange_code", fake_exchange_code)
    patch_sso_everywhere(monkeypatch, "_fetch_jwks", fake_fetch_jwks)
    return client.get(
        f"/api/auth/sso/{idp_id}/callback", params={"code": "fake-code", "state": state},
        headers=HEADERS, follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# JIT：规则命中落到正确团队/角色；管理员标记不会自动映射成 is_system_admin
# ---------------------------------------------------------------------------


def test_rule_match_assigns_team_and_role(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    team_id = orgs_store.create_team(conn, org_id=orgs_store.ORG_DEFAULT_ID, name="内容中心", description=None, created_by="test")
    role = orgs_store.get_role_by_key(conn, None, "producer")
    conn.commit()
    provision_cfg = {
        "auto_create": True,
        "rules": [{"claim": "department", "op": "equals", "value": "内容中心", "team_id": team_id, "role_id": role["id"]}],
        "on_no_match": "default",
    }
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID, provision=provision_cfg)

    resp = _do_oidc_login(client, monkeypatch, idp_id, subject="ext-rule-hit", raw_claims_overrides={"department": "内容中心"})
    assert resp.status_code == 302, resp.text

    identity = conn.execute(
        "SELECT user_id FROM user_identities WHERE idp_id=? AND external_subject=?", (idp_id, "ext-rule-hit")
    ).fetchone()
    assert identity is not None
    member = conn.execute(
        "SELECT role_id FROM team_members WHERE team_id=? AND user_id=?", (team_id, identity["user_id"])
    ).fetchone()
    assert member is not None
    assert member["role_id"] == role["id"]


def test_rule_role_only_falls_back_to_default_team(client: TestClient, monkeypatch) -> None:
    """PRD EP-02 §5 第二条示例规则的形状：只写 role_id，team_id 从
    default_team_id 补齐。"""
    conn = get_conn()
    default_team_id = orgs_store.create_team(conn, org_id=orgs_store.ORG_DEFAULT_ID, name="默认团队", description=None, created_by="test")
    org_admin_role = orgs_store.get_role_by_key(conn, None, "org_admin")
    conn.commit()
    provision_cfg = {
        "auto_create": True, "default_team_id": default_team_id,
        "rules": [{"claim": "groups", "op": "contains", "value": "manju-admins", "role_id": org_admin_role["id"]}],
        "on_no_match": "default",
    }
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID, provision=provision_cfg)

    resp = _do_oidc_login(
        client, monkeypatch, idp_id, subject="ext-group-hit", raw_claims_overrides={"groups": ["manju-admins", "other"]},
    )
    assert resp.status_code == 302, resp.text
    identity = conn.execute(
        "SELECT user_id FROM user_identities WHERE idp_id=? AND external_subject=?", (idp_id, "ext-group-hit")
    ).fetchone()
    member = conn.execute(
        "SELECT role_id FROM team_members WHERE team_id=? AND user_id=?", (default_team_id, identity["user_id"])
    ).fetchone()
    assert member is not None
    assert member["role_id"] == org_admin_role["id"]


def test_idp_admin_claim_never_elevates_to_system_admin(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    resp = _do_oidc_login(
        client, monkeypatch, idp_id, subject="ext-fake-admin",
        raw_claims_overrides={"is_admin": True, "role": "admin", "groups": ["manju-admins", "domain-admins"]},
    )
    assert resp.status_code == 302, resp.text
    row = conn.execute(
        "SELECT u.is_system_admin FROM users u JOIN user_identities i ON i.user_id=u.id "
        "WHERE i.idp_id=? AND i.external_subject=?", (idp_id, "ext-fake-admin"),
    ).fetchone()
    assert row is not None
    assert row["is_system_admin"] == 0


def test_auto_create_false_blocks_unknown_subject_with_visible_audit(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID, provision={"auto_create": False})
    resp = _do_oidc_login(client, monkeypatch, idp_id, subject="ext-no-account")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "no_account"

    audit_row = conn.execute(
        "SELECT outcome FROM operation_audit WHERE event='sso.provision_no_account' AND target=?",
        ("ext-no-account",),
    ).fetchone()
    assert audit_row is not None and audit_row["outcome"] == "rejected"


def test_on_no_match_reject_blocks_unmapped_claims(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    provision_cfg = {
        "auto_create": True,
        "rules": [{"claim": "department", "op": "equals", "value": "不存在的部门", "role_id": "role_x"}],
        "on_no_match": "reject",
    }
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID, provision=provision_cfg)
    resp = _do_oidc_login(client, monkeypatch, idp_id, subject="ext-unmapped", raw_claims_overrides={"department": "别的部门"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "provision_rejected"


# ---------------------------------------------------------------------------
# 绑定/解绑：已有本地账号绑定后可用两种方式登录；解绑最后一个登录方式 -> 422
# ---------------------------------------------------------------------------


def test_link_existing_account_then_unlink_with_password_remaining(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    user_id = _mk_local_user(conn, "link-target", "correct-horse-1")

    link_resp = client.post(
        "/api/auth/sso/link", json={"idp_id": idp_id, "redirect_to": "/settings"},
        headers=_login_headers(user_id),
    )
    assert link_resp.status_code == 200, link_resp.text
    authorize_url = link_resp.json()["authorize_url"]
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    auth_req = read_auth_request(conn, state)
    assert auth_req["link_user_id"] == user_id

    private_key, jwk = generate_rsa_keypair()
    payload = default_id_token_payload(issuer=ISSUER, client_id=CLIENT_ID, subject="ext-link-1", nonce=auth_req["nonce"])
    id_token = sign_id_token(private_key, {"alg": "RS256", "kid": jwk["kid"], "typ": "JWT"}, payload)

    async def fake_exchange_code(endpoints, **kwargs):
        return {"id_token": id_token, "access_token": "fake"}

    async def fake_fetch_jwks(jwks_url, *, force: bool = False):
        return [jwk]

    patch_sso_everywhere(monkeypatch, "exchange_code", fake_exchange_code)
    patch_sso_everywhere(monkeypatch, "_fetch_jwks", fake_fetch_jwks)
    cb = client.get(f"/api/auth/sso/{idp_id}/callback", params={"code": "c", "state": state}, headers=HEADERS, follow_redirects=False)
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/settings"  # 绑定流程不改签会话，也不追加 session_token

    identity = conn.execute(
        "SELECT user_id FROM user_identities WHERE idp_id=? AND external_subject=?", (idp_id, "ext-link-1")
    ).fetchone()
    assert identity["user_id"] == user_id

    # 还有本地口令，解绑允许
    unlink = client.delete(f"/api/auth/sso/link/{idp_id}", headers=_login_headers(user_id))
    assert unlink.status_code == 200, unlink.text


def test_unlink_last_login_method_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    resp = _do_oidc_login(client, monkeypatch, idp_id, subject="ext-sso-only")
    assert resp.status_code == 302
    identity = conn.execute(
        "SELECT user_id FROM user_identities WHERE idp_id=? AND external_subject=?", (idp_id, "ext-sso-only")
    ).fetchone()
    user_id = identity["user_id"]
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    assert not row["password_hash"]  # SSO 开户账号没有本地口令，这是它唯一的登录方式

    unlink = client.delete(f"/api/auth/sso/link/{idp_id}", headers=_login_headers(user_id))
    assert unlink.status_code == 422, unlink.text


# ---------------------------------------------------------------------------
# 强制 SSO 开关：admin_only / disabled + 连通性自检闸门 + break-glass
# ---------------------------------------------------------------------------


def _set_policy(client, admin_headers, policy: str):
    return client.put("/api/admin/sso/local-login-policy", json={"policy": policy}, headers=admin_headers)


def test_admin_only_policy_blocks_regular_user_login(client: TestClient) -> None:
    conn = get_conn()
    admin_id = _mk_local_user(conn, "policy-admin", "admin-pass-1", is_system_admin=True)
    _mk_local_user(conn, "policy-user", "user-pass-1")
    admin_headers = _login_headers(admin_id)

    resp = _set_policy(client, admin_headers, "admin_only")
    assert resp.status_code == 200, resp.text

    blocked = _login_via_password(client, "policy-user", "user-pass-1")
    assert blocked.status_code == 401, blocked.text
    allowed = _login_via_password(client, "policy-admin", "admin-pass-1")
    assert allowed.status_code == 200, allowed.text


def test_disable_without_enabled_idp_is_rejected(client: TestClient) -> None:
    conn = get_conn()
    admin_id = _mk_local_user(conn, "disable-admin-1", "admin-pass-2", is_system_admin=True)
    resp = _set_policy(client, _login_headers(admin_id), "disabled")
    assert resp.status_code == 422, resp.text

    current = client.get("/api/admin/sso/local-login-policy", headers=_login_headers(admin_id))
    assert current.json()["policy"] == "enabled"


def test_disable_with_failing_connectivity_check_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    admin_id = _mk_local_user(conn, "disable-admin-2", "admin-pass-3", is_system_admin=True)
    create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)

    async def failing_check(idp_row):
        return False, "模拟：token 端点连不通"

    patch_sso_everywhere(monkeypatch, "check_connectivity", failing_check)
    resp = _set_policy(client, _login_headers(admin_id), "disabled")
    assert resp.status_code == 422, resp.text
    assert "连通" in json.dumps(resp.json(), ensure_ascii=False)

    current = client.get("/api/admin/sso/local-login-policy", headers=_login_headers(admin_id))
    assert current.json()["policy"] == "enabled"


def test_disable_then_local_login_401_and_break_glass_works(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    admin_id = _mk_local_user(conn, "bg-admin", "admin-pass-4", is_system_admin=True)
    create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)

    async def passing_check(idp_row):
        return True, "ok"

    patch_sso_everywhere(monkeypatch, "check_connectivity", passing_check)
    switched = _set_policy(client, _login_headers(admin_id), "disabled")
    assert switched.status_code == 200, switched.text

    blocked = _login_via_password(client, "bg-admin", "admin-pass-4")
    assert blocked.status_code == 401, blocked.text

    import hashlib
    import secrets

    from app.sso.store import create_break_glass_code

    code = secrets.token_urlsafe(16)
    create_break_glass_code(conn, user_id=admin_id, code_hash=hashlib.sha256(code.encode()).hexdigest(), created_by="test-script")
    conn.commit()

    wrong = client.post("/api/auth/sso/break-glass", json={"username": "bg-admin", "code": "not-the-real-code"}, headers=HEADERS)
    assert wrong.status_code == 401

    ok = client.post("/api/auth/sso/break-glass", json={"username": "bg-admin", "code": code}, headers=HEADERS)
    assert ok.status_code == 200, ok.text
    assert ok.json()["must_change_password"] is True
    row = conn.execute("SELECT must_change_password FROM users WHERE id=?", (admin_id,)).fetchone()
    assert row["must_change_password"] == 1

    reused = client.post("/api/auth/sso/break-glass", json={"username": "bg-admin", "code": code}, headers=HEADERS)
    assert reused.status_code == 401, reused.text
