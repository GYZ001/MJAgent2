"""P0 提权修复的行为档回归（EP-01 三阶段审查，2026-09-12）。

``tests/test_exemption_enforcement_guard.py`` 是结构档：只证明代码里挂着
``Depends(require_system_admin)``，不证明它在真实 HTTP 请求路径上生效——这正是
CLAUDE.md 记录过的 ContextVar fail-open 教训（鉴权依赖本身没错，但没接进请求
路径）。本文件是行为档：用真实 ``TestClient(app)`` 打 HTTP，非管理员（哪怕
已登录）必须在下列三条曾经零鉴权的路由上拿到 403，管理员拿 200/正常业务错误。

三条路由与档位判断依据见交付报告；这里只覆盖「普通登录用户是否被真正拦住」，
不重复验证业务逻辑本身（探针会创建真实付费任务，普通测试不应该真的调用它）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app


def _mk_user(conn, username: str, *, is_system_admin: bool = False) -> str:
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users(id, username, display_name, auth_provider, status, "
        "is_system_admin, created_at) VALUES(?,?,?,'local','active',?,?)",
        (user_id, username, username, int(is_system_admin), now()),
    )
    return user_id


def _headers(user_id: str) -> dict[str, str]:
    return {"X-Manju-Session": create_session(user_id)}


@pytest.fixture()
def seed():
    conn = get_conn()
    member = _mk_user(conn, "p0-member")
    admin = _mk_user(conn, "p0-admin", is_system_admin=True)
    conn.commit()
    return {"member": _headers(member), "admin": _headers(admin)}


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_member_cannot_self_issue_mcp_token(seed, client: TestClient) -> None:
    """核心漏洞回归：修复前任何登录用户（含只读角色）都能给自己签发一枚
    带 manju:admin scope 的 MCP token 再拿它提权。"""
    resp = client.post(
        "/api/system/mcp-tokens",
        json={"name": "steal", "scopes": ["manju:admin"]},
        headers=seed["member"],
    )
    assert resp.status_code == 403, resp.text


def test_member_cannot_list_mcp_tokens(seed, client: TestClient) -> None:
    resp = client.get("/api/system/mcp-tokens", headers=seed["member"])
    assert resp.status_code == 403, resp.text


def test_member_cannot_delete_mcp_token(seed, client: TestClient) -> None:
    """DELETE 带 token_id 路径参数，挂载点的 require_project_owner_access 在
    走到路由自身的 Depends(require_system_admin) 之前就已经按
    ``app/authz/resolve.py::_resolve_one`` 的 admin_only 解析把非管理员统一
    判成 404（不用 403，见该文件"不能让外部区分对象不存在/存在但无权"的既有
    约定）——这是该路由在本次修复前就已经具备的间接保护，与 POST/GET（无法被
    这套路径参数机制覆盖，本次修复前零鉴权）不是同一档，因此断言与另外两条
    不同的状态码，不是弱化验证。"""
    resp = client.delete("/api/system/mcp-tokens/tok_nonexistent", headers=seed["member"])
    assert resp.status_code == 404, resp.text


def test_member_cannot_probe_video_capability(seed, client: TestClient) -> None:
    resp = client.post(
        "/api/video-capabilities/hiagent/some-model/probe",
        json={"confirm": True, "capability": "reference_image", "reference_image_url": "https://x/y.png"},
        headers=seed["member"],
    )
    assert resp.status_code == 403, resp.text


def test_member_cannot_create_provider_media_publication(seed, client: TestClient) -> None:
    resp = client.post(
        "/api/provider-media-publications",
        json={"source_revision_id": "rev_x", "source_url": "https://example.com/a.mp4"},
        headers=seed["member"],
    )
    assert resp.status_code == 403, resp.text


def test_admin_can_issue_and_manage_mcp_tokens(seed, client: TestClient) -> None:
    """管理员仍然走得通：修复只收紧非管理员，不误伤本机操作者。"""
    created = client.post(
        "/api/system/mcp-tokens", json={"name": "ops"}, headers=seed["admin"],
    )
    assert created.status_code == 200, created.text
    token_id = created.json()["id"]

    listed = client.get("/api/system/mcp-tokens", headers=seed["admin"])
    assert listed.status_code == 200, listed.text

    deleted = client.delete(f"/api/system/mcp-tokens/{token_id}", headers=seed["admin"])
    assert deleted.status_code == 200, deleted.text
