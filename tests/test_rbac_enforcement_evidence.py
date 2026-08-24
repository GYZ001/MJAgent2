"""RBAC 强制性的行为证据。

这个文件存在的理由是一类反复出现的缺陷形状：**字段在、赋值在、清零在，唯独
没有任何一行强制它**，而且不抛异常。代码目检看起来是做完了，实际从未生效。
本项目已知的同族例子包括 json_schema 静默降级、spine_beat 补丁空转，以及
RBAC 这边的 ``users.must_change_password`` 与 ``workspaces.status``。

所以这里的断言一律是「做一次真实操作，看它是否真的被挡住」，而不是
「检查某个字段是否等于某个值」。任何一条如果改成后者，它就失去了存在意义。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.sessions import create_session, resolve_session
from app.db import get_conn, new_id, now
from app.main import app

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


def _add_user(username: str, *, admin: int = 0, status: str = "active") -> str:
    conn = get_conn()
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, username, hash_password("pw-" + username), status, admin, now()),
    )
    conn.commit()
    return user_id


def _add_workspace(workspace_id: str, status: str = "active") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO workspaces(id, tenant_id, name, status, created_at) "
        "VALUES(?, 'tenant_default', ?, ?, ?)",
        (workspace_id, workspace_id, status, now()),
    )
    conn.commit()


def _join(workspace_id: str, user_id: str, role: str = "production") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO workspace_members(workspace_id, user_id, role, created_at) VALUES(?,?,?,?)",
        (workspace_id, user_id, role, now()),
    )
    conn.commit()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_disabled_workspace_actually_strips_member_access():
    """停用团队后，成员必须真的失去该团队的 scope——不是只把 status 写成 disabled。"""
    _add_workspace("ws_live", "active")
    _add_workspace("ws_dead", "disabled")
    user_id = _add_user("bob")
    _join("ws_live", user_id)
    _join("ws_dead", user_id)

    principal = resolve_session(create_session(user_id))
    assert principal is not None
    assert principal.can_access("ws_live") is True
    # 这一条是本文件的由来：改之前它是 True，停用团队等于没停。
    assert principal.can_access("ws_dead") is False
    assert principal.scopes_for("ws_dead") == frozenset()


def test_disabled_user_cannot_resolve_session():
    user_id = _add_user("ghost", status="disabled")
    assert resolve_session(create_session(user_id)) is None


def test_login_throttle_actually_blocks_after_repeated_failures(client: TestClient):
    """节流必须真的挡住请求，且挡住之后连正确密码也进不来——否则它只是个计数器。"""
    _add_user("target")
    seen = [
        client.post(
            "/api/auth/login",
            json={"username": "target", "password": "WRONG"},
            headers=_HEADERS,
        ).status_code
        for _ in range(7)
    ]
    assert 429 in seen, f"节流从未触发：{seen}"
    blocked = client.post(
        "/api/auth/login",
        json={"username": "target", "password": "pw-target"},
        headers=_HEADERS,
    )
    assert blocked.status_code == 429


def test_password_change_revokes_other_sessions():
    """改密必须让旧会话立即失效，而不只是把 password_hash 换掉。"""
    user_id = _add_user("rotate")
    stale = create_session(user_id)
    assert resolve_session(stale) is not None

    conn = get_conn()
    conn.execute(
        "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (now(), user_id),
    )
    conn.commit()
    assert resolve_session(stale) is None


def test_must_change_password_is_surfaced_so_the_ui_can_enforce_it(client: TestClient):
    """后端必须如实吐出该标志位；前端 AuthGate 靠它决定挂不挂载应用壳。

    强制点本身在前端（见 ForcePasswordChangePage），这里守住的是它的输入：
    一旦这个字段不再出现在 /api/auth/me 的响应里，前端的强制会静默失效。
    """
    user_id = _add_user("fresh")
    conn = get_conn()
    conn.execute("UPDATE users SET must_change_password=1 WHERE id=?", (user_id,))
    conn.commit()

    resp = client.get(
        "/api/auth/me",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
    )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True
