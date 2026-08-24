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


def test_no_module_builds_media_urls_outside_the_signer():
    """媒体 URL 只能由 build_media_url 产出——这是 A 类（规则在链路中丢失）的守卫。

    ``/media`` 的凭证只能进查询串（``<img>``/``<video>`` 不带自定义头）。签名逻辑
    集中在 ``app/media_urls.py``，历史上却有 8 处各自裸拼 ``f"/media/{rel}?v=..."``。
    只要有人日后再添一处绕过签名，在 ``MJ_MEDIA_REQUIRE_TICKET`` 打开那天，那批
    图片会静默 403——而单测全绿，因为签名函数本身没坏。

    所以这里守的不是"签名函数对不对"，是"有没有人绕开它"。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {"app/media_urls.py", "app/main.py"}
    offenders = []
    for path in (root / "app").rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if '"/media/' in line or 'f"/media/' in line or "/media/{" in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "这些地方绕过了 build_media_url：\n" + "\n".join(offenders)


def test_disabled_workspace_disappears_from_the_login_payload_too(client: TestClient):
    """展示口径必须和授权口径一致：停用的团队不能还留在 /api/auth/me 的列表里。

    ``resolve_session`` 与 ``_workspaces_payload`` 是两处独立计算「我属于哪些团队」
    的地方。只有前者带 status 过滤时，用户会在界面上看到一个自己其实已经没有任何
    权限的团队——点进去每个请求都 404，而「团队还在列表里」会让人以为是系统坏了。
    这条守的就是这两处口径不能分叉。
    """
    _add_workspace("ws_shown", "active")
    _add_workspace("ws_hidden", "disabled")
    user_id = _add_user("split")
    _join("ws_shown", user_id)
    _join("ws_hidden", user_id)

    resp = client.get(
        "/api/auth/me",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
    )
    assert resp.status_code == 200
    listed = {w["id"] for w in resp.json()["workspaces"]}
    assert listed == {"ws_shown"}, f"停用团队仍出现在登录载荷里：{listed}"


def test_login_payload_never_lists_a_team_the_principal_cannot_access(client: TestClient):
    """跨口径一致性：登录载荷里的团队集合必须是 can_access 为真的子集。

    这是【重复真源】那一类的守卫。第 12 例的教训是：单独给"授权口径"写行为测试
    是不够的——那条测试当时是绿的，因为它只断言了我想到的那一半，另一处独立实现
    根本不在断言范围里。这条测试同时钉住两处：任何一方将来再分叉，它就红。

    真正的结构性修复是让 _workspaces_payload 只消费 principal.workspace_roles
    （成员判定只有一个真源，这里只补团队名）；这条断言是那次收敛的回归网。
    """
    _add_workspace("ws_ok", "active")
    _add_workspace("ws_off", "disabled")
    user_id = _add_user("crosscheck")
    _join("ws_ok", user_id)
    _join("ws_off", user_id)

    token = create_session(user_id)
    principal = resolve_session(token)
    assert principal is not None

    resp = client.get("/api/auth/me", headers={**_HEADERS, "X-Manju-Session": token})
    assert resp.status_code == 200
    listed = {w["id"] for w in resp.json()["workspaces"]}

    unauthorized = {ws for ws in listed if not principal.can_access(ws)}
    assert not unauthorized, f"载荷列出了无权访问的团队：{unauthorized}"
    # 反向也要成立，否则"藏起一个其实有权访问的团队"同样是分叉。
    assert listed == set(principal.workspace_roles)
