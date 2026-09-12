"""EP-03 §8 密码策略验收：强度（长度+字符类）、历史口令去重、有效期强制改密。

全部经 HTTP + 裸 SQL 铺数据；策略阈值走真实 settings（``password_min_length``
等），不 mock 掉 ``get_setting``——这样才能验证 ``app.monitoring.SETTINGS_SCHEMA``
与 ``app.auth.password_policy`` 各自的默认值确实一致生效。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password, verify_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now, set_setting
from app.main import app

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}
_STRONG_PW_A = "Correct-Horse-9battery"
_STRONG_PW_B = "Another-Str0ng!Pass"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _mk_user(conn, username: str, password: str, *, is_system_admin: bool = False) -> str:
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status, "
        "is_system_admin, must_change_password, created_at) "
        "VALUES(?,?,?,?,'active',?,0,?)",
        (user_id, username, username, hash_password(password), int(is_system_admin), now()),
    )
    conn.commit()
    return user_id


def _headers_for(user_id: str) -> dict[str, str]:
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


@pytest.fixture()
def admin() -> dict:
    conn = get_conn()
    user_id = _mk_user(conn, "root-pwpolicy", "irrelevant-admin-pw-A1", is_system_admin=True)
    return {"id": user_id, "headers": _headers_for(user_id)}


def test_admin_create_user_rejects_weak_password_with_reasons(client: TestClient, admin: dict):
    resp = client.post(
        "/api/system/users",
        json={"username": f"weak-{new_id('u')}", "password": "short"},
        headers=admin["headers"],
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "weak_password"
    assert any("长度" in v for v in detail["violations"])


def test_admin_create_user_rejects_single_character_class(client: TestClient, admin: dict):
    resp = client.post(
        "/api/system/users",
        json={"username": f"weak2-{new_id('u')}", "password": "aaaaaaaaaaaaaaaa"},
        headers=admin["headers"],
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any("类字符" in v for v in detail["violations"])


def test_admin_create_user_accepts_strong_password(client: TestClient, admin: dict):
    resp = client.post(
        "/api/system/users",
        json={"username": f"strong-{new_id('u')}", "password": _STRONG_PW_A},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text


def test_self_change_password_rejects_same_as_current(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"selfsame-{new_id('u')}", _STRONG_PW_A)
    resp = client.post(
        "/api/auth/change-password",
        json={"old_password": _STRONG_PW_A, "new_password": _STRONG_PW_A},
        headers=_headers_for(user_id),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "password_reused"


def test_self_change_password_history_reuse_rejected(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"selfhist-{new_id('u')}", _STRONG_PW_A)
    headers = _headers_for(user_id)

    step1 = client.post(
        "/api/auth/change-password",
        json={"old_password": _STRONG_PW_A, "new_password": _STRONG_PW_B},
        headers=headers,
    )
    assert step1.status_code == 200, step1.text
    new_token = step1.json()["session_token"]
    headers2 = {**_HEADERS, "X-Manju-Session": new_token}

    # 改回最初的口令：password_history 里已经归档了被替换掉的 _STRONG_PW_A，必须被拒。
    step2 = client.post(
        "/api/auth/change-password",
        json={"old_password": _STRONG_PW_B, "new_password": _STRONG_PW_A},
        headers=headers2,
    )
    assert step2.status_code == 422, step2.text
    assert step2.json()["detail"]["code"] == "password_reused"

    history = conn.execute(
        "SELECT password_hash FROM password_history WHERE user_id=?", (user_id,)
    ).fetchall()
    assert history, "历史口令必须落库"
    assert all(_STRONG_PW_A not in r["password_hash"] and _STRONG_PW_B not in r["password_hash"] for r in history)
    assert any(verify_password(_STRONG_PW_A, r["password_hash"]) for r in history)


def test_self_change_password_succeeds_with_new_strong_password(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"selfok-{new_id('u')}", _STRONG_PW_A)
    resp = client.post(
        "/api/auth/change-password",
        json={"old_password": _STRONG_PW_A, "new_password": _STRONG_PW_B},
        headers=_headers_for(user_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_token"]
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    assert verify_password(_STRONG_PW_B, row["password_hash"])


def test_admin_reset_password_also_enforces_policy(client: TestClient, admin: dict):
    conn = get_conn()
    target_id = _mk_user(conn, f"resettarget-{new_id('u')}", _STRONG_PW_A)
    weak = client.put(f"/api/system/users/{target_id}", json={"password": "weak"}, headers=admin["headers"])
    assert weak.status_code == 422, weak.text
    strong = client.put(
        f"/api/system/users/{target_id}", json={"password": _STRONG_PW_B}, headers=admin["headers"]
    )
    assert strong.status_code == 200, strong.text


def test_password_max_age_forces_change_on_next_login(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"aged-{new_id('u')}", _STRONG_PW_A)
    # 账号创建于"2 天前"，且从未改过密（password_changed_at 恒 NULL）。
    conn.execute("UPDATE users SET created_at=? WHERE id=?", (now() - 2 * 86400, user_id))
    conn.commit()
    set_setting("password_max_age_days", "1")
    try:
        resp = client.post(
            "/api/auth/login",
            json={"username": conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()["username"], "password": _STRONG_PW_A},
            headers=_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["must_change_password"] is True
    finally:
        set_setting("password_max_age_days", "0")


def test_password_max_age_zero_never_forces_change(client: TestClient):
    conn = get_conn()
    user_id = _mk_user(conn, f"notaged-{new_id('u')}", _STRONG_PW_A)
    conn.execute("UPDATE users SET created_at=? WHERE id=?", (now() - 3650 * 86400, user_id))
    conn.commit()
    set_setting("password_max_age_days", "0")
    username = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()["username"]
    resp = client.post("/api/auth/login", json={"username": username, "password": _STRONG_PW_A}, headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["must_change_password"] is False
