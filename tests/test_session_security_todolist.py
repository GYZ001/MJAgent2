"""2026-07-27 Todolist T1–T7：鉴权 / 脱敏 / 目录 grant 合同回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests
from app.capabilities.dispatch import waiting_approval_payload
from app.capabilities.policy import reset_approvals_for_tests
from app.db import set_setting
from app.local_session import (
    reset_session_secret_for_tests,
    set_request_session_id,
)
from app.main import app
from tests.conftest import SessionTestClient


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "session-gate.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    projects = tmp_path / "projects"
    data = tmp_path / "data"
    projects.mkdir()
    data.mkdir()
    monkeypatch.setattr("app.config.PROJECTS_DIR", projects)
    monkeypatch.setattr("app.config.DATA_DIR", data)
    monkeypatch.setattr("app.system_api.config.PROJECTS_DIR", projects)
    monkeypatch.setattr("app.system_api.config.DATA_DIR", data)
    db.init_db()
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()
    reset_session_secret_for_tests()
    set_request_session_id(None)
    yield
    set_request_session_id(None)


@pytest.fixture
def anon() -> TestClient:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def authed(anon: TestClient) -> SessionTestClient:
    return SessionTestClient(anon)


def test_public_health_and_session_need_no_auth(anon: TestClient) -> None:
    assert anon.get("/api/system/health").status_code == 200
    body = anon.get("/api/session", headers={"Origin": "http://127.0.0.1:5230"}).json()
    assert body["session_token"]
    assert body["header"] == "X-Manju-Session"
    # 异源攻击：恶意页 Origin 与后端 Host 不一致 → 拒绝（同源公网域名则允许）
    blocked = anon.get(
        "/api/session",
        headers={"Origin": "https://evil.example", "Host": "127.0.0.1:8230"},
    )
    assert blocked.status_code == 403


def test_same_host_public_origin_can_bootstrap_session(anon: TestClient) -> None:
    """同域反代：公网 Origin 与 Host 一致时可领取会话；异源仍拒绝。"""
    ok = anon.get(
        "/api/session",
        headers={
            "Origin": "https://manju.example.com",
            "Host": "manju.example.com",
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["session_token"]

    via_forwarded = anon.get(
        "/api/session",
        headers={
            "Origin": "https://manju.example.com",
            "Host": "127.0.0.1:8230",
            "X-Forwarded-Host": "manju.example.com",
        },
    )
    assert via_forwarded.status_code == 200, via_forwarded.text

    cross = anon.get(
        "/api/session",
        headers={
            "Origin": "https://evil.example",
            "Host": "manju.example.com",
        },
    )
    assert cross.status_code == 403


def test_same_host_public_origin_can_call_api(anon: TestClient) -> None:
    """领取到的会话可带着公网同源 Origin 访问受保护 API。"""
    token = anon.get(
        "/api/session",
        headers={"Origin": "https://app.example.com", "Host": "app.example.com"},
    ).json()["session_token"]
    resp = anon.get(
        "/api/settings",
        headers={
            "Origin": "https://app.example.com",
            "Host": "app.example.com",
            "X-Manju-Session": token,
        },
    )
    assert resp.status_code == 200, resp.text


def test_keys_and_credentials_require_confirm(
    authed: SessionTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[dict[str, str]] = []

    def save_keys(keys: dict[str, str]) -> list[str]:
        saved.append(keys)
        return list(keys)

    monkeypatch.setattr(config, "save_keys_to_env", save_keys)
    denied = authed.put("/api/keys", json={"hiagent": "sk-test"})
    assert denied.status_code == 422
    assert "confirm" in denied.text
    accepted = authed.put("/api/keys", json={"confirm": True, "hiagent": "sk-test-key"})
    assert accepted.status_code == 200
    assert accepted.json().get("ok") is True
    assert saved == [{"HIAGENT_API_KEY": "sk-test-key"}]


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("DELETE", "/api/projects/proj_x", {}),
        ("PUT", "/api/keys", {"json": {"hiagent": "sk-test"}}),
        ("GET", "/api/settings", {}),
        ("POST", "/api/system/mcp-tokens", {"json": {"scopes": ["manju:read"]}}),
        ("GET", "/api/system/browse", {}),
        ("GET", "/api/system/calls", {}),
        ("GET", "/api/system/errors/err_x", {}),
    ],
)
def test_sensitive_routes_require_session(anon: TestClient, method: str, path: str, kwargs: dict) -> None:
    resp = anon.request(method, path, **kwargs)
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code} {resp.text}"


def test_settings_never_leaks_credentials(authed: SessionTestClient) -> None:
    set_setting(
        "model_credentials",
        json.dumps({"model_1": {"base_url": "https://example.com", "api_key": "sk-secret-value"}}, ensure_ascii=False),
    )
    set_setting(
        "custom_models",
        json.dumps([{"id": "m1", "model": "x", "api_key": "sk-custom-secret"}], ensure_ascii=False),
    )
    resp = authed.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    blob = json.dumps(body, ensure_ascii=False)
    assert "sk-secret-value" not in blob
    assert "sk-custom-secret" not in blob
    assert "api_key" not in blob
    assert body.get("model_credentials") == "{}"


def test_mcp_token_create_defaults_to_read_scope(authed: SessionTestClient) -> None:
    resp = authed.post("/api/system/mcp-tokens", json={"name": "t1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["scopes"] == ["manju:read"]
    assert body["token"].startswith("mcp_")


def test_anonymous_delete_does_not_return_approval_token(anon: TestClient) -> None:
    """无会话时即便走到业务层，HTTP 载荷也不得下发 approval_token。"""
    from app.capabilities.schemas import CommandResult, CommandStatus, PreflightResult, RiskLevel

    result = CommandResult(
        status=CommandStatus.WAITING_APPROVAL,
        summary="需要批准",
        command="project.delete",
        data={"approval_id": "appr_x", "approval_token": "appr_x.sig", "expires_at": 1},
        preflight=PreflightResult(
            command="project.delete",
            allowed=True,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="删除",
            state_fingerprint="sha256:x",
            requires_confirmation=True,
        ),
    )
    payload = waiting_approval_payload(result, session_id=None)
    assert "approval_token" not in payload
    assert payload["approval_id"] == "appr_x"

    with_session = waiting_approval_payload(result, session_id="sess")
    assert with_session["approval_token"] == "appr_x.sig"


def test_browse_defaults_to_grant_roots_not_home(authed: SessionTestClient, tmp_path: Path) -> None:
    home_secret = Path.home() / ".ssh"
    resp = authed.get("/api/system/browse")
    assert resp.status_code == 200
    body = resp.json()
    paths = {item["path"] for item in body["dirs"]}
    assert paths  # projects/data
    # 家目录不在默认根列表中
    assert str(Path.home()) not in paths
    assert str(home_secret) not in paths

    denied = authed.get("/api/system/browse", params={"path": str(Path.home())})
    assert denied.status_code == 403


def test_mkdir_requires_directory_grant(authed: SessionTestClient, tmp_path: Path) -> None:
    """system.mkdir 不再走"未批准先 202、批准后才真正执行"（2026-08-30 产品拍板：
    除了删除资源，否则不需要弹窗；system.mkdir 不是删除资源，已从
    confirmation=ALWAYS 降到 NEVER），第一次调用就直接执行到域层——这条用例
    真正要守住的"路径必须在 directory_grant 白名单内"不变，只是现在这个拒绝
    在第一次调用就直接发生，不需要先换一个 approval_token 再被拒绝。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    denied = authed.post("/api/system/mkdir", json={"path": str(outside), "name": "child"})
    assert denied.status_code == 403, denied.text

    from app.config import PROJECTS_DIR

    granted_parent = PROJECTS_DIR
    done = authed.post("/api/system/mkdir", json={"path": str(granted_parent), "name": "child_ok"})
    assert done.status_code == 200, done.text
    assert (granted_parent / "child_ok").is_dir()


def test_calls_list_omits_request_response_bodies(authed: SessionTestClient) -> None:
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,model,status,http_status,latency_ms,error,request_json,response_json
           ) VALUES(1,'chat','m','OK',200,1,'','{"prompt":"secret"}','{"text":"secret"}')"""
    )
    conn.commit()
    resp = authed.get("/api/system/calls")
    assert resp.status_code == 200
    rows = resp.json()
    assert rows
    blob = json.dumps(rows, ensure_ascii=False)
    assert "request_json" not in blob
    assert "response_json" not in blob
    assert "secret" not in blob


def test_keys_preview_does_not_expose_prefix(authed: SessionTestClient, monkeypatch) -> None:
    monkeypatch.setenv("HIAGENT_API_KEY", "sk-abcdefghijklmnopqrstuvwxyz")

    resp = authed.get("/api/keys")
    assert resp.status_code == 200
    preview = resp.json()["hiagent"]["preview"]
    assert preview == "已配置"
    assert "sk-abc" not in preview


@pytest.fixture
def remote_anon() -> TestClient:
    """非回环客户端：模拟浏览器直连后端（不经 vite 反代）。"""
    with TestClient(app, client=("203.0.113.9", 51515)) as client:
        yield client


def test_same_origin_get_without_origin_header_can_bootstrap(
    remote_anon: TestClient,
) -> None:
    """浏览器同源 GET 不发 Origin；后端直服构建产物时不能因此拒发凭证。

    回归 2026-08-21：后端从 127.0.0.1 改绑 0.0.0.0 直接服务 frontend/dist 后，
    /api/session 对同源 GET 返回 403，前端拿不到凭证，页面报「项目加载失败」。
    """
    resp = remote_anon.get("/api/session", headers={"Host": "manju.example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_token"]


def test_cross_origin_bootstrap_still_rejected_from_remote_client(
    remote_anon: TestClient,
) -> None:
    """放行「无 Origin」不得放松跨源：带异源 Origin 仍必须拒绝。"""
    resp = remote_anon.get(
        "/api/session",
        headers={"Origin": "https://evil.example", "Host": "manju.example.com"},
    )
    assert resp.status_code == 403, resp.text


def test_remote_client_still_needs_token_for_protected_api(
    remote_anon: TestClient,
) -> None:
    """凭证闸门本身不变：非回环客户端不带凭证访问受保护 API 仍是 401。"""
    resp = remote_anon.get("/api/projects", headers={"Host": "manju.example.com"})
    assert resp.status_code == 401, resp.text


def test_no_origin_bootstrap_is_not_covered_by_loopback_exemptions() -> None:
    """守住上一条测试不空转：放行必须来自新增的 Host 分支，而非回环豁免。"""
    from app.local_session import _is_loopback_host

    assert not _is_loopback_host("manju.example.com")
    assert "203.0.113.9" not in {"127.0.0.1", "::1", "localhost", "testclient"}


def test_bootstrap_requires_host_when_origin_absent(remote_anon: TestClient) -> None:
    """既无 Origin 也无 Host 的请求仍然拒绝。"""
    resp = remote_anon.get("/api/session", headers={"Host": ""})
    assert resp.status_code == 403, resp.text
