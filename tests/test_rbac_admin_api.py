"""管理接口的端到端验收：全部经由 HTTP，不用裸 SQL 铺状态。

存在理由和 ``test_rbac_enforcement_evidence.py`` 里那条建项目验收一样：扫描发现
`POST /api/system/users`、`POST /api/system/workspaces`、`PUT .../{id}` 这几个接口
**一条自动化测试都没有经过**，测试里全是 7 处裸 `INSERT INTO users/workspaces/
workspace_members`。我此前只在线上手工点过两遍——手工验证不会留存，下一次改动
没有任何东西会拦住它。

这些接口是**发放权限**的地方，这里出回归就是提权，所以它们尤其不该只靠手工验。
因此本文件刻意全程走 HTTP：建团队、开户、改角色、停用，一律调接口而不是写库，
让"产品自己那一步"真的被执行。
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


def test_create_team_then_user_then_that_user_can_log_in_with_the_granted_role(
    client: TestClient, admin_headers: dict[str, str]
):
    """开户的黄金路径：建团队 -> 开户并授角色 -> 该账号真的能登进来且角色正确。"""
    ws = client.post("/api/system/workspaces", headers=admin_headers, json={"name": "制作二组"})
    assert ws.status_code == 200, ws.text
    workspace_id = ws.json()["id"]

    created = client.post(
        "/api/system/users",
        headers=admin_headers,
        json={"username": "zhangsan", "password": "initpass1",
              "workspace_id": workspace_id, "role": "production"},
    )
    assert created.status_code == 200, created.text

    # 断言的是"他真的能用"，而不是"库里那行写对了"。
    profile = _login(client, "zhangsan")
    assert profile["is_system_admin"] is False
    assert profile["workspaces"] == [
        {"id": workspace_id, "name": "制作二组", "role": "production"}
    ]
    # 管理员开的户默认要求首次登录改密。
    assert profile["must_change_password"] is True


def test_role_change_takes_effect_on_next_login(client: TestClient, admin_headers: dict[str, str]):
    workspace_id = client.post(
        "/api/system/workspaces", headers=admin_headers, json={"name": "T"}
    ).json()["id"]
    user_id = client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "lisi", "password": "initpass1",
              "workspace_id": workspace_id, "role": "readonly"},
    ).json()["id"]
    assert _login(client, "lisi")["workspaces"][0]["role"] == "readonly"

    changed = client.put(
        f"/api/system/workspaces/{workspace_id}/members/{user_id}",
        headers=admin_headers, json={"role": "review"},
    )
    assert changed.status_code == 200, changed.text
    assert _login(client, "lisi")["workspaces"][0]["role"] == "review"


def test_disabling_a_user_kills_their_live_session_immediately(
    client: TestClient, admin_headers: dict[str, str]
):
    """停用必须当场断线，而不是等 12 小时滑动过期自然失效。"""
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


def test_disabling_a_team_revokes_its_members_through_the_api(
    client: TestClient, admin_headers: dict[str, str]
):
    """这条对应线上验收时我只能靠裸 SQL 完成的那一步——现在它有产品入口了。"""
    workspace_id = client.post(
        "/api/system/workspaces", headers=admin_headers, json={"name": "待停用组"}
    ).json()["id"]
    client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "member", "password": "initpass1",
              "workspace_id": workspace_id, "role": "production"},
    )
    assert [w["id"] for w in _login(client, "member")["workspaces"]] == [workspace_id]

    off = client.put(
        f"/api/system/workspaces/{workspace_id}", headers=admin_headers,
        json={"status": "disabled"},
    )
    assert off.status_code == 200
    assert _login(client, "member")["workspaces"] == []

    # 可逆：停错了要能救回来。
    client.put(
        f"/api/system/workspaces/{workspace_id}", headers=admin_headers, json={"status": "active"}
    )
    assert [w["id"] for w in _login(client, "member")["workspaces"]] == [workspace_id]


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


def test_non_admin_cannot_reach_any_admin_endpoint(
    client: TestClient, admin_headers: dict[str, str]
):
    """这些接口发放权限，非管理员碰到任何一个都是提权。"""
    workspace_id = client.post(
        "/api/system/workspaces", headers=admin_headers, json={"name": "X"}
    ).json()["id"]
    client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "plain", "password": "initpass1",
              "workspace_id": workspace_id, "role": "workspace_admin"},
    )
    plain = {**_HEADERS, "X-Manju-Session": _login(client, "plain")["session_token"]}

    assert client.get("/api/system/users", headers=plain).status_code == 403
    assert client.get("/api/system/workspaces", headers=plain).status_code == 403
    assert client.post(
        "/api/system/workspaces", headers=plain, json={"name": "偷建的"}
    ).status_code == 403
    assert client.post(
        "/api/system/users", headers=plain,
        json={"username": "backdoor", "password": "initpass1", "is_system_admin": True},
    ).status_code == 403
    # 空间管理员是团队内的最高角色，但依然不是系统管理员——这条区分不能塌。
    assert client.put(
        f"/api/system/workspaces/{workspace_id}", headers=plain, json={"status": "disabled"}
    ).status_code == 403
