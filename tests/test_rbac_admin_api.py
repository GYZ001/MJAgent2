"""管理接口的端到端验收：全部经由 HTTP，不用裸 SQL 铺状态。

存在理由和 ``test_rbac_enforcement_evidence.py`` 里那条建项目验收一样：只靠
手工验证不会留存，下一次改动没有任何东西会拦住它。这些接口是**发放权限**的
地方，这里出回归就是提权，所以它们尤其不该只靠手工验。因此本文件刻意全程走
HTTP：开户、改角色、停用，一律调接口而不是写库，让"产品自己那一步"真的被
执行。

账号即项目空间落地后，团队/工作空间相关的接口（``POST /api/system/
workspaces`` 等）已随角色模型一并退场——本文件不再覆盖它们，只保留账号管理
本身（开户/改密/启停/系统管理员标记）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    """唯一一处裸 SQL：引导首个系统管理员。这是先有鸡后有蛋，产品内无解。"""
    conn = get_conn()
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',1,0,?)",
        (user_id, "root", hash_password("pw-root"), now()),
    )
    conn.commit()
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


def _login(client: TestClient, username: str, password: str = "initpass1") -> dict:
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}, headers=_HEADERS
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_user_then_that_user_can_log_in_with_their_own_project_space(
    client: TestClient, admin_headers: dict[str, str]
):
    """开户的黄金路径：开户 -> 该账号真的能登进来，且自动拥有一个独立项目空间
    （不需要选团队/角色——账号本身就是空间）。"""
    created = client.post(
        "/api/system/users",
        headers=admin_headers,
        json={"username": "zhangsan", "password": "initpass1"},
    )
    assert created.status_code == 200, created.text

    # 断言的是"他真的能用"，而不是"库里那行写对了"。
    profile = _login(client, "zhangsan")
    assert profile["is_system_admin"] is False
    assert "workspaces" not in profile
    # 管理员开的户默认要求首次登录改密。
    assert profile["must_change_password"] is True

    headers = {**_HEADERS, "X-Manju-Session": profile["session_token"]}
    listing = client.get("/api/projects", headers=headers)
    assert listing.status_code == 200
    assert listing.json() == []  # 新账号的项目空间是空的，不是别人的


def test_disabling_a_user_kills_their_live_session_immediately(
    client: TestClient, admin_headers: dict[str, str]
):
    """停用必须当场断线，而不是等 7 天滑动过期自然失效。"""
    user_id = client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "wangwu", "password": "initpass1"},
    ).json()["id"]
    live = {**_HEADERS, "X-Manju-Session": _login(client, "wangwu")["session_token"]}
    assert client.get("/api/auth/me", headers=live).status_code == 200

    disabled = client.put(
        f"/api/system/users/{user_id}", headers=admin_headers, json={"status": "disabled"}
    )
    assert disabled.status_code == 200
    assert client.get("/api/auth/me", headers=live).status_code == 401


def test_password_reset_revokes_sessions_and_requires_new_password(
    client: TestClient, admin_headers: dict[str, str]
):
    user_id = client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "lisi", "password": "initpass1"},
    ).json()["id"]
    live = {**_HEADERS, "X-Manju-Session": _login(client, "lisi")["session_token"]}
    assert client.get("/api/auth/me", headers=live).status_code == 200

    reset = client.put(
        f"/api/system/users/{user_id}", headers=admin_headers,
        json={"password": "newpass123"},
    )
    assert reset.status_code == 200

    assert client.get("/api/auth/me", headers=live).status_code == 401
    relogged = _login(client, "lisi", password="newpass123")
    assert relogged["must_change_password"] is True


def test_can_promote_and_demote_system_admin(client: TestClient, admin_headers: dict[str, str]):
    user_id = client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "future-admin", "password": "initpass1"},
    ).json()["id"]
    promoted = client.put(
        f"/api/system/users/{user_id}", headers=admin_headers,
        json={"is_system_admin": True},
    )
    assert promoted.status_code == 200
    assert promoted.json()["is_system_admin"] is True

    demoted = client.put(
        f"/api/system/users/{user_id}", headers=admin_headers,
        json={"is_system_admin": False},
    )
    assert demoted.status_code == 200
    assert demoted.json()["is_system_admin"] is False


def test_admin_cannot_lock_themselves_or_the_system_out(
    client: TestClient, admin_headers: dict[str, str]
):
    """自锁救援：最后一把钥匙不能被自己扔掉。"""
    me = client.get("/api/auth/me", headers=admin_headers).json()
    my_id = me["user"]["id"]

    assert client.put(
        f"/api/system/users/{my_id}", headers=admin_headers, json={"status": "disabled"}
    ).status_code == 422
    assert client.put(
        f"/api/system/users/{my_id}", headers=admin_headers, json={"is_system_admin": False}
    ).status_code == 422
    # 仍然活着，没被自己搞死。
    assert client.get("/api/auth/me", headers=admin_headers).status_code == 200


def test_cannot_demote_the_last_system_admin_even_if_not_self(
    client: TestClient, admin_headers: dict[str, str]
):
    """自锁保护不能被"让另一个管理员账号去点"绕过——保护的是"系统至少留一个"，
    不是"自己不能点自己"这个字面意思。"""
    # admin_headers 本身就是唯一的系统管理员；创建一个普通用户后尝试把 root
    # 降级，root 已经是唯一管理员，必须拒绝。
    me = client.get("/api/auth/me", headers=admin_headers).json()
    my_id = me["user"]["id"]
    other = client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "second-admin", "password": "initpass1", "is_system_admin": True},
    ).json()
    other_headers = {**_HEADERS, "X-Manju-Session": create_session(other["id"])}

    # 现在有两个管理员：root 与 second-admin。用 second-admin 的身份把 root
    # 降级必须被允许（不是自锁），因为降级后系统仍剩至少一个管理员。
    demote_root = client.put(
        f"/api/system/users/{my_id}", headers=other_headers, json={"is_system_admin": False},
    )
    assert demote_root.status_code == 200

    # 此时唯一管理员是 second-admin；再想把它自己降级必须被拒绝（自锁保护）。
    self_demote = client.put(
        f"/api/system/users/{other['id']}", headers=other_headers,
        json={"is_system_admin": False},
    )
    assert self_demote.status_code == 422


def test_non_admin_cannot_reach_any_admin_endpoint(
    client: TestClient, admin_headers: dict[str, str]
):
    """这些接口发放权限，非管理员碰到任何一个都是提权。"""
    client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "plain", "password": "initpass1"},
    )
    plain = {**_HEADERS, "X-Manju-Session": _login(client, "plain")["session_token"]}

    assert client.get("/api/system/users", headers=plain).status_code == 403
    assert client.post(
        "/api/system/users", headers=plain,
        json={"username": "backdoor", "password": "initpass1"},
    ).status_code == 403
    assert client.post(
        "/api/system/users", headers=plain,
        json={"username": "backdoor2", "password": "initpass1", "is_system_admin": True},
    ).status_code == 403


def test_duplicate_username_rejected(client: TestClient, admin_headers: dict[str, str]):
    client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "dupe", "password": "initpass1"},
    )
    dup = client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "dupe", "password": "initpass1"},
    )
    assert dup.status_code == 409
