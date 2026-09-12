"""EP-03 §8 会话策略验收：空闲超时、最长时长、并发上限（踢最旧 + 给原因）、
管理员查看/强制下线、服务会话（``kind='service'``）豁免时长类策略但仍受角色
约束。全部经 HTTP + 裸 SQL 铺数据、直接操纵 ``user_sessions`` 时间戳来模拟
"已经空闲很久/已经很老"，不 sleep。

服务会话一节覆盖协调方裁决的真缺口："session_max_age_hours=12 会打死回归
脚本"——kind 是会话行上的事实，不按用户名/路径特判，证伪式对照 interactive
在同一条件下确实被拒、service 确实不受影响。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import session_policy
from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now, set_setting
from app.main import app

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
    conn.commit()
    return user_id


def _headers(token: str) -> dict[str, str]:
    return {**_HEADERS, "X-Manju-Session": token}


@pytest.fixture()
def admin() -> dict:
    conn = get_conn()
    user_id = _mk_user(conn, "root-sesspolicy", is_system_admin=True)
    return {"id": user_id, "headers": _headers(create_session(user_id))}


def test_idle_timeout_returns_401_with_specific_reason(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"idle-{new_id('u')}")
    set_setting("session_idle_timeout_min", "5")
    try:
        token = create_session(user_id)
        sid = token.split(".", 1)[0]
        conn.execute("UPDATE user_sessions SET last_seen_at=? WHERE id=?", (now() - 6 * 60, sid))
        conn.commit()
        resp = client.get("/api/auth/me", headers=_headers(token))
        assert resp.status_code == 401, resp.text
        assert "长时间无操作" in resp.json()["detail"]
    finally:
        set_setting("session_idle_timeout_min", "480")


def test_max_age_returns_401_with_specific_reason(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"maxage-{new_id('u')}")
    set_setting("session_max_age_hours", "1")
    try:
        token = create_session(user_id)
        sid = token.split(".", 1)[0]
        conn.execute(
            "UPDATE user_sessions SET created_at=?, last_seen_at=? WHERE id=?",
            (now() - 2 * 3600, now(), sid),
        )
        conn.commit()
        resp = client.get("/api/auth/me", headers=_headers(token))
        assert resp.status_code == 401, resp.text
        assert "最长时长" in resp.json()["detail"]
    finally:
        set_setting("session_max_age_hours", "12")


def test_concurrent_limit_kicks_oldest_with_reason(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"concurrent-{new_id('u')}")
    set_setting("session_max_concurrent", "2")
    try:
        token1 = create_session(user_id)
        token2 = create_session(user_id)
        token3 = create_session(user_id)

        oldest = client.get("/api/auth/me", headers=_headers(token1))
        assert oldest.status_code == 401, oldest.text
        assert "并发" in oldest.json()["detail"] or "挤下线" in oldest.json()["detail"]

        still_alive_2 = client.get("/api/auth/me", headers=_headers(token2))
        assert still_alive_2.status_code == 200, still_alive_2.text
        still_alive_3 = client.get("/api/auth/me", headers=_headers(token3))
        assert still_alive_3.status_code == 200, still_alive_3.text
    finally:
        set_setting("session_max_concurrent", "0")


def test_concurrent_limit_zero_means_unlimited(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"unlimited-{new_id('u')}")
    set_setting("session_max_concurrent", "0")
    tokens = [create_session(user_id) for _ in range(5)]
    for token in tokens:
        resp = client.get("/api/auth/me", headers=_headers(token))
        assert resp.status_code == 200, resp.text


def test_admin_can_list_and_revoke_user_session(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"target-{new_id('u')}")
    token = create_session(target_id)

    listed = client.get(f"/api/system/users/{target_id}/sessions", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    session_id = items[0]["id"]

    revoke = client.post(
        f"/api/system/users/{target_id}/sessions/{session_id}/revoke", headers=admin["headers"]
    )
    assert revoke.status_code == 200, revoke.text

    kicked = client.get("/api/auth/me", headers=_headers(token))
    assert kicked.status_code == 401, kicked.text
    assert "管理员" in kicked.json()["detail"]

    listed_after = client.get(f"/api/system/users/{target_id}/sessions", headers=admin["headers"])
    assert listed_after.json()["items"] == []


def test_admin_revoke_unknown_session_is_404(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"target404-{new_id('u')}")
    resp = client.post(
        f"/api/system/users/{target_id}/sessions/nonexistent/revoke", headers=admin["headers"]
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 服务会话（kind='service'）：豁免空闲超时/并发上限，仍受吊销/自身到期约束，
# 仍然绑定真实账号身份（Principal.can() 照常生效）。
# ---------------------------------------------------------------------------


def _issue_service(client: TestClient, admin: dict, target_id: str, ttl_days: float = 30) -> dict:
    resp = client.post(
        f"/api/system/users/{target_id}/service-sessions",
        json={"ttl_days": ttl_days}, headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_service_session_survives_idle_timeout_while_interactive_is_rejected(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"svc-idle-{new_id('u')}")
    set_setting("session_idle_timeout_min", "5")
    try:
        service_token = _issue_service(client, admin, target_id)["session_token"]
        interactive_token = create_session(target_id)
        stale = now() - 6 * 60
        conn.execute(
            "UPDATE user_sessions SET last_seen_at=? WHERE id IN (?,?)",
            (stale, service_token.split(".", 1)[0], interactive_token.split(".", 1)[0]),
        )
        conn.commit()

        service_resp = client.get("/api/auth/me", headers=_headers(service_token))
        assert service_resp.status_code == 200, service_resp.text

        interactive_resp = client.get("/api/auth/me", headers=_headers(interactive_token))
        assert interactive_resp.status_code == 401, interactive_resp.text
        assert "长时间无操作" in interactive_resp.json()["detail"]
    finally:
        set_setting("session_idle_timeout_min", "480")


def test_service_session_survives_max_age_while_interactive_is_rejected(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"svc-age-{new_id('u')}")
    set_setting("session_max_age_hours", "1")
    try:
        service_token = _issue_service(client, admin, target_id)["session_token"]
        interactive_token = create_session(target_id)
        old_created = now() - 2 * 3600
        conn.execute(
            "UPDATE user_sessions SET created_at=?, last_seen_at=? WHERE id=?",
            (old_created, now(), service_token.split(".", 1)[0]),
        )
        conn.execute(
            "UPDATE user_sessions SET created_at=?, last_seen_at=? WHERE id=?",
            (old_created, now(), interactive_token.split(".", 1)[0]),
        )
        conn.commit()

        assert client.get("/api/auth/me", headers=_headers(service_token)).status_code == 200
        interactive_resp = client.get("/api/auth/me", headers=_headers(interactive_token))
        assert interactive_resp.status_code == 401, interactive_resp.text
        assert "最长时长" in interactive_resp.json()["detail"]
    finally:
        set_setting("session_max_age_hours", "12")


def test_service_session_exempt_from_concurrent_limit(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"svc-concurrent-{new_id('u')}")
    set_setting("session_max_concurrent", "1")
    try:
        interactive_token = create_session(target_id)
        service_tokens = [_issue_service(client, admin, target_id)["session_token"] for _ in range(3)]

        # 服务会话互不驱逐，也不驱逐先建立的 interactive 会话。
        for token in service_tokens:
            assert client.get("/api/auth/me", headers=_headers(token)).status_code == 200
        assert client.get("/api/auth/me", headers=_headers(interactive_token)).status_code == 200
    finally:
        set_setting("session_max_concurrent", "0")


def test_service_session_revoked_returns_401_immediately(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"svc-revoke-{new_id('u')}")
    token = _issue_service(client, admin, target_id)["session_token"]
    sid = token.split(".", 1)[0]
    assert client.get("/api/auth/me", headers=_headers(token)).status_code == 200

    session_row = conn.execute("SELECT id FROM user_sessions WHERE id=?", (sid,)).fetchone()
    assert session_row is not None
    revoke = client.post(f"/api/system/users/{target_id}/sessions/{sid}/revoke", headers=admin["headers"])
    assert revoke.status_code == 200, revoke.text

    kicked = client.get("/api/auth/me", headers=_headers(token))
    assert kicked.status_code == 401, kicked.text
    assert "管理员" in kicked.json()["detail"]


def test_service_session_respects_its_own_expiry(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"svc-expiry-{new_id('u')}")
    token = _issue_service(client, admin, target_id, ttl_days=30)["session_token"]
    sid = token.split(".", 1)[0]
    # 直接把这枚服务会话自己的 expires_at 拨到过去，模拟"已经到期"——不设
    # 无限期的强制点就落在这一列，无论空闲/并发策略怎么豁免它都拦不住。
    conn.execute("UPDATE user_sessions SET expires_at=? WHERE id=?", (now() - 10, sid))
    conn.commit()
    resp = client.get("/api/auth/me", headers=_headers(token))
    assert resp.status_code == 401, resp.text


def test_service_session_issue_ttl_bounds_enforced(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"svc-bounds-{new_id('u')}")
    too_short = client.post(
        f"/api/system/users/{target_id}/service-sessions",
        json={"ttl_days": 0}, headers=admin["headers"],
    )
    assert too_short.status_code == 422, too_short.text
    too_long = client.post(
        f"/api/system/users/{target_id}/service-sessions",
        json={"ttl_days": session_policy.SERVICE_SESSION_MAX_TTL_DAYS + 1}, headers=admin["headers"],
    )
    assert too_long.status_code == 422, too_long.text
    assert "无限期" in too_long.json()["detail"]


def test_service_session_issue_requires_admin(client: TestClient):
    conn = get_conn()
    plain_id = _mk_user(conn, f"plain-{new_id('u')}")
    plain_headers = _headers(create_session(plain_id))
    resp = client.post(
        f"/api/system/users/{plain_id}/service-sessions",
        json={"ttl_days": 30}, headers=plain_headers,
    )
    assert resp.status_code == 403, resp.text


def test_service_session_still_bound_to_role_not_a_bypass(client: TestClient, admin: dict):
    """服务会话仍然绑定真实账号身份：给一个普通用户签发服务会话，那枚会话
    照样只有普通用户的权限——豁免的是会话时长策略，不是角色判定。"""
    conn = get_conn()
    plain_id = _mk_user(conn, f"svc-plain-{new_id('u')}")
    service_token = _issue_service(client, admin, plain_id)["session_token"]
    resp = client.get("/api/system/users", headers=_headers(service_token))
    assert resp.status_code == 403, resp.text


def test_service_session_listed_with_kind_and_admin_session_shows_interactive(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"svc-listed-{new_id('u')}")
    create_session(target_id)
    _issue_service(client, admin, target_id)

    items = client.get(f"/api/system/users/{target_id}/sessions", headers=admin["headers"]).json()["items"]
    kinds = sorted(item["kind"] for item in items)
    assert kinds == ["interactive", "service"]
