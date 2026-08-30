"""MCP `/mcp` Streamable HTTP（简化 JSON-RPC）端点合同测试（PRD AGENT_MCP_CAPABILITY M4）。

只挂载 `app.mcp.server.router`，不启动完整 app/DB：tools/resources 元数据来自
Capability Registry（内存），dry_run / waiting_approval 场景不触碰真实业务 handler。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.principal import Principal, set_current_principal
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests
from app.capabilities.policy import reset_approvals_for_tests
from app.mcp import auth as mcp_auth
from app.mcp import rate_limit
from app.mcp.server import router as mcp_router

ORIGIN = "http://localhost:5230"

# 两个账号，各自拥有一个项目——用于证明 MCP token 归属校验真的按账号隔离
# （不是只挂了个样子）。proj_x/ep_x 是既有测试大量复用的夹具数据，保留原名不
# 动；owner_x 是它的账号，_token() 默认绑定到它，历史用例因此不需要改动。
OWNER_X = "user_owner_x"
OWNER_Y = "user_owner_y"
ADMIN_ID = "user_sys_admin"


@pytest.fixture(autouse=True)
def _isolated_mcp_state(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mcp-test.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()
    monkeypatch.setattr(mcp_auth, "TOKENS_PATH", tmp_path / "mcp_tokens.json")
    rate_limit.reset_rate_limiter_for_tests()
    conn = db.get_conn()
    for user_id, username, is_admin in (
        (OWNER_X, "owner-x", 0),
        (OWNER_Y, "owner-y", 0),
        (ADMIN_ID, "sys-admin", 1),
    ):
        conn.execute(
            "INSERT INTO users(id, username, display_name, auth_provider, status, "
            "is_system_admin, created_at) VALUES(?,?,?,'local','active',?,?)",
            (user_id, username, username, is_admin, db.now()),
        )
    conn.execute(
        "INSERT INTO projects(id, name, status, owner_user_id, created_at) VALUES(?,?,?,?,?)",
        ("proj_x", "测试项目", "created", OWNER_X, db.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, status, created_at) VALUES(?,?,?,?,?,?)",
        ("ep_x", "proj_x", 1, "第一集", "scripted", db.now()),
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, owner_user_id, created_at) VALUES(?,?,?,?,?)",
        ("proj_y", "另一个账号的项目", "created", OWNER_Y, db.now()),
    )
    conn.commit()
    yield
    rate_limit.reset_rate_limiter_for_tests()
    set_current_principal(None)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(mcp_router)
    return TestClient(app)


def _token_for(
    user_id: str | None,
    scopes: list[str] | None = None,
    *,
    is_system_admin: bool = False,
) -> str:
    """按账号签发 token：模拟真实 HTTP 流程——``POST /api/system/mcp-tokens``
    挂在 ``require_local_session`` 之后，调用 ``mcp_auth.create_token()`` 时
    ``get_current_principal()`` 已经由中间件注入了签发者的账号；token 因此
    绑定这个账号（见 ``app/mcp/auth.py::create_token``）。``user_id=None``
    模拟没有会话上下文时创建的 token（bootstrap token / 脚本直接调用），
    应当落到「未绑定账号」一档。"""
    principal = (
        Principal(user_id=user_id, username=user_id, is_system_admin=is_system_admin)
        if user_id is not None
        else None
    )
    set_current_principal(principal)
    try:
        plaintext, _ = mcp_auth.create_token(scopes=scopes, ttl_s=None, name="test")
    finally:
        set_current_principal(None)
    return plaintext


def _token(scopes: list[str] | None = None) -> str:
    return _token_for(OWNER_X, scopes)


def _rpc(
    client: TestClient,
    method: str,
    params: dict | None = None,
    *,
    token: str | None = None,
    origin: str | None = ORIGIN,
    rpc_id: int = 1,
):
    headers: dict[str, str] = {}
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers)


def test_initialize_returns_protocol_info(client: TestClient) -> None:
    resp = _rpc(client, "initialize", {}, token=_token())
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["serverInfo"]["name"]
    assert result["protocolVersion"]


def test_tools_list_includes_bible_generate_but_hides_admin_only(client: TestClient) -> None:
    resp = _rpc(client, "tools/list", token=_token(["manju:read"]))
    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "bible.generate" in names
    assert "delivery.review" in names
    # admin_only 命令默认不对外暴露给 MCP（catalog 层已过滤）
    assert "system.update_settings" not in names
    assert "system.model_create" not in names

    bible = next(t for t in tools if t["name"] == "bible.generate")
    assert bible["inputSchema"]["type"] == "object"
    assert "annotations" in bible
    assert bible["annotations"]["readOnlyHint"] is False


def test_missing_token_is_rejected_with_401(client: TestClient) -> None:
    resp = _rpc(client, "tools/list", token=None)
    assert resp.status_code == 401


def test_invalid_token_is_rejected_with_401(client: TestClient) -> None:
    resp = _rpc(client, "tools/list", token="mcp_bogus_notreal")
    assert resp.status_code == 401


def test_revoked_token_is_rejected(client: TestClient) -> None:
    plaintext, record = mcp_auth.create_token(scopes=["manju:read"], ttl_s=None)
    mcp_auth.revoke_token(record["id"])
    resp = _rpc(client, "tools/list", token=plaintext)
    assert resp.status_code == 401


def test_disallowed_origin_is_rejected_with_403(client: TestClient) -> None:
    resp = _rpc(client, "tools/list", token=_token(), origin="https://evil.example.com")
    assert resp.status_code == 403


def test_no_origin_header_still_requires_token(client: TestClient) -> None:
    # 没有 Origin（例如非浏览器客户端）不能借此绕过鉴权。
    resp = _rpc(client, "tools/list", token=None, origin=None)
    assert resp.status_code == 401
    resp_ok = _rpc(client, "tools/list", token=_token(), origin=None)
    assert resp_ok.status_code == 200


def test_resources_list_contains_projects(client: TestClient) -> None:
    resp = _rpc(client, "resources/list", token=_token())
    assert resp.status_code == 200
    uris = {r["uri"] for r in resp.json()["result"]["resources"]}
    assert "manju://projects" in uris
    assert "manju://system/health" in uris


def test_resources_templates_list_contains_project_template(client: TestClient) -> None:
    resp = _rpc(client, "resources/templates/list", token=_token())
    templates = {r["uriTemplate"] for r in resp.json()["result"]["resourceTemplates"]}
    assert "manju://projects/{project_id}" in templates
    assert "manju://shots/{shot_id}" in templates


def test_resources_read_unknown_uri_returns_jsonrpc_error(client: TestClient) -> None:
    resp = _rpc(client, "resources/read", {"uri": "manju://not-a-real-thing"}, token=_token())
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32001


def test_tools_call_dry_run_does_not_require_approval(client: TestClient) -> None:
    resp = _rpc(
        client,
        "tools/call",
        {
            "name": "project.delete",
            "arguments": {"project_id": "proj_x", "dry_run": True, "idempotency_key": "mcp-dry-1"},
        },
        token=_token(["manju:project-write"]),
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["status"] == "succeeded"
    assert structured["data"]["dry_run"] is True


def test_tools_call_destructive_without_approval_waits_and_includes_preflight(client: TestClient) -> None:
    # project.delete 现在是软删除（NEVER 确认）；project.purge（彻底清理）才是
    # 现在的 R3_DESTRUCTIVE + ALWAYS 命令，撑起这条"未批准就必须等待"的用例。
    resp = _rpc(
        client,
        "tools/call",
        {
            "name": "project.purge",
            "arguments": {"project_id": "proj_x", "idempotency_key": "mcp-appr-1"},
        },
        token=_token(["manju:project-write"]),
    )
    assert resp.status_code == 200
    structured = resp.json()["result"]["structuredContent"]
    assert structured["status"] == "waiting_approval"
    assert structured["preflight"] is not None
    assert "approval_id" in structured["data"]
    # P0：MCP 响应绝不能携带可直接重放的 approval_token
    assert "approval_token" not in (structured.get("data") or {})
    # 未批准就等于未执行，绝不能假装已经删除了项目
    assert structured["status"] != "succeeded"


def test_tool_annotations_cannot_downgrade_server_risk(client: TestClient) -> None:
    """即使客户端/模型认为某操作“无害”，服务端仍必须按 CommandSpec 的真实风险执行批准流程。"""
    resp = _rpc(
        client,
        "tools/call",
        {
            "name": "video.clear_episode",
            "arguments": {"episode_id": "ep_x", "idempotency_key": "mcp-clear-1"},
        },
        token=_token(["manju:project-write"]),
    )
    structured = resp.json()["result"]["structuredContent"]
    assert structured["status"] == "waiting_approval"
    assert structured["preflight"]["risk"] == "R3"
    assert structured["preflight"]["requires_confirmation"] is True


def test_tools_call_insufficient_scope_rejected_with_403(client: TestClient) -> None:
    resp = _rpc(
        client,
        "tools/call",
        {"name": "video.clear_episode", "arguments": {"episode_id": "ep_x"}},
        token=_token(["manju:read"]),
    )
    assert resp.status_code == 403


def test_tools_call_unknown_tool_returns_jsonrpc_error(client: TestClient) -> None:
    resp = _rpc(client, "tools/call", {"name": "does.not.exist", "arguments": {}}, token=_token())
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32602


def test_prompts_list_contains_core_prompts(client: TestClient) -> None:
    resp = _rpc(client, "prompts/list", token=_token())
    names = {p["name"] for p in resp.json()["result"]["prompts"]}
    assert {
        "continue_project",
        "diagnose_run",
        "revise_shot",
        "prepare_episode_delivery",
        "cost_preview",
    } <= names


def test_prompts_get_renders_arguments(client: TestClient) -> None:
    resp = _rpc(
        client,
        "prompts/get",
        {"name": "diagnose_run", "arguments": {"run_id": "run_123"}},
        token=_token(),
    )
    assert resp.status_code == 200
    messages = resp.json()["result"]["messages"]
    assert "run_123" in messages[0]["content"]["text"]


def test_prompts_get_missing_required_argument_is_rejected(client: TestClient) -> None:
    resp = _rpc(client, "prompts/get", {"name": "diagnose_run", "arguments": {}}, token=_token())
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# 账号级项目隔离：MCP token 必须能解析出 principal，并复用
# app.domain.common._project_or_404 这条已有的归属校验咽喉点——不是只挂了
# Bearer/scope 就完事。这里同时证明两半：跨账号必须 404（不是 403，不能让
# 调用方分辨出"对象存在但你无权"和"对象不存在"），本账号自己的项目必须照常
# 可用。见 app/mcp/server.py::_principal_from_claims。
# ---------------------------------------------------------------------------


def test_tools_call_cross_account_project_is_denied_as_not_found(client: TestClient) -> None:
    """token 绑定 owner-y，对 owner-x 的 proj_x 发起 bible.cancel：
    ``_cancel_bible_core`` 内部第一步就是 ``_project_or_404(project_id)``，
    没有自定义 preflight 短路（confirmation=NEVER 直接执行 handler），是验证
    这条咽喉点最干净的写路径。"""
    resp = _rpc(
        client,
        "tools/call",
        {"name": "bible.cancel", "arguments": {"project_id": "proj_x"}},
        token=_token_for(OWNER_Y, ["manju:generation-text"]),
    )
    assert resp.status_code == 200
    structured = resp.json()["result"]["structuredContent"]
    assert structured["status"] == "failed"
    assert structured["error_code"] == "http_404"
    assert resp.json()["result"]["isError"] is True


def test_tools_call_own_account_project_still_works(client: TestClient) -> None:
    resp = _rpc(
        client,
        "tools/call",
        {"name": "bible.cancel", "arguments": {"project_id": "proj_x"}},
        token=_token_for(OWNER_X, ["manju:generation-text"]),
    )
    assert resp.status_code == 200
    structured = resp.json()["result"]["structuredContent"]
    assert structured["status"] == "succeeded", structured
    assert resp.json()["result"]["isError"] is False


def test_resources_read_cross_account_project_is_not_found(client: TestClient) -> None:
    resp = _rpc(
        client,
        "resources/read",
        {"uri": "manju://projects/proj_x"},
        token=_token_for(OWNER_Y),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32001
    assert body["error"]["data"]["code"] == "not_found"


def test_resources_read_own_account_project_still_works(client: TestClient) -> None:
    resp = _rpc(
        client,
        "resources/read",
        {"uri": "manju://projects/proj_x"},
        token=_token_for(OWNER_X),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    assert body["result"]["contents"][0]["uri"] == "manju://projects/proj_x"


def test_resources_read_projects_list_scoped_to_token_owner(client: TestClient) -> None:
    resp_x = _rpc(client, "resources/read", {"uri": "manju://projects"}, token=_token_for(OWNER_X))
    text_x = resp_x.json()["result"]["contents"][0]["text"]
    assert "proj_x" in text_x
    assert "proj_y" not in text_x

    resp_y = _rpc(client, "resources/read", {"uri": "manju://projects"}, token=_token_for(OWNER_Y))
    text_y = resp_y.json()["result"]["contents"][0]["text"]
    assert "proj_y" in text_y
    assert "proj_x" not in text_y


def test_system_admin_token_reaches_both_accounts(client: TestClient) -> None:
    token = _token_for(ADMIN_ID, is_system_admin=True)
    own = _rpc(client, "resources/read", {"uri": "manju://projects/proj_x"}, token=token)
    other = _rpc(client, "resources/read", {"uri": "manju://projects/proj_y"}, token=token)
    assert "error" not in own.json()
    assert "error" not in other.json()


def test_unbound_token_has_no_project_access(client: TestClient) -> None:
    """没有登录上下文时创建的 token（bootstrap token、脚本/测试直接调用
    ``create_token()``）拿不到任何账号的项目——"空集合不等于无需检查"：未知
    归属必须解释成"什么都不合法"，不能解释成"不受限制"（这条正是本次要关的
    缺口：修复前任何 token 在 MCP 路径上都能触达任意账号的项目）。"""
    token = _token_for(None, ["manju:read"])
    resp = _rpc(client, "resources/read", {"uri": "manju://projects/proj_x"}, token=token)
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["data"]["code"] == "not_found"

    listing = _rpc(client, "resources/read", {"uri": "manju://projects"}, token=token)
    text = listing.json()["result"]["contents"][0]["text"]
    assert "proj_x" not in text
    assert "proj_y" not in text
