"""RBAC 第三阶段：Command Bus 对 ``CommandSpec.scopes`` / ``admin_only`` 的强制执行。

``tests/conftest.py`` 的 autouse fixture 默认给每个测试注入一个系统管理员
Principal（历史测试不需要逐个改造）。本文件里凡是要验证某个具体角色的行为，
都通过 ``_as_principal`` 临时换成目标角色，用完立即还原，避免污染后续测试。
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.auth.principal import Principal, get_current_principal, set_current_principal
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.bus import get_command_bus, reset_command_bus_for_tests
from app.capabilities.policy import reset_approvals_for_tests
from app.capabilities.schemas import CommandStatus

_AUTHZ_ERROR_CODES = {"forbidden_scope", "forbidden_admin_only"}


@pytest.fixture(autouse=True)
def _ready(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rbac-test.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_x", "测试项目", "created", db.now()),
    )
    conn.commit()
    yield


@contextmanager
def _as_principal(*, role: str | None, is_system_admin: bool = False, workspace_id: str = "ws_test"):
    """临时切换当前 Principal，退出时还原成进入前的身份。"""
    previous = get_current_principal()
    workspace_roles = {} if role is None else {workspace_id: role}
    set_current_principal(
        Principal(
            user_id=f"test-{role or 'sysadmin'}",
            username=f"test-{role or 'sysadmin'}",
            is_system_admin=is_system_admin,
            workspace_roles=workspace_roles,
        )
    )
    try:
        yield
    finally:
        set_current_principal(previous)


def _assert_not_authz_rejected(result) -> None:
    """只断言鉴权结论，不断言业务结果：命令可能因无关的业务原因失败或
    进入待批准态，但绝不能因为 RBAC 被挡下。"""
    if result.status == CommandStatus.REJECTED:
        assert result.error_code not in _AUTHZ_ERROR_CODES, (result.error_code, result.summary)


# ---------------------------------------------------------------------------
# readonly：只读角色不能碰任何写命令
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_principal_rejected_on_r2_command() -> None:
    bus = get_command_bus()
    with _as_principal(role="readonly"):
        result = await bus.execute_async("video.generate_shot", {"shot_id": "shot_x"})
    assert result.status == CommandStatus.REJECTED
    assert result.error_code == "forbidden_scope"


@pytest.mark.asyncio
async def test_readonly_principal_rejected_on_r3_command() -> None:
    bus = get_command_bus()
    with _as_principal(role="readonly"):
        result = await bus.execute_async(
            "delivery.review",
            {"episode_id": "ep_missing_for_test", "decision": "approve"},
        )
    assert result.status == CommandStatus.REJECTED
    assert result.error_code == "forbidden_scope"


# ---------------------------------------------------------------------------
# production：生成 / 项目写入可以，交付（delivery）不行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_production_principal_allowed_on_generation_text_command() -> None:
    bus = get_command_bus()
    with _as_principal(role="production"):
        result = await bus.execute_async("screenplay.generate", {"episode_id": "ep_missing_for_test"})
    _assert_not_authz_rejected(result)


@pytest.mark.asyncio
async def test_production_principal_rejected_on_delivery_command() -> None:
    bus = get_command_bus()
    with _as_principal(role="production"):
        result = await bus.execute_async(
            "delivery.submit_feedback",
            {"episode_id": "ep_missing_for_test", "feedback": "客户反馈内容"},
        )
    assert result.status == CommandStatus.REJECTED
    assert result.error_code == "forbidden_scope"


# ---------------------------------------------------------------------------
# review：只有 {read, delivery}，但人工门禁白名单必须放行审校本职工作
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_principal_rejected_on_paid_generation_command() -> None:
    """审校不能替 production 花钱生成——不能因为白名单的存在被连带放宽。"""
    bus = get_command_bus()
    with _as_principal(role="review"):
        result = await bus.execute_async("video.generate_shot", {"shot_id": "shot_x"})
    assert result.status == CommandStatus.REJECTED
    assert result.error_code == "forbidden_scope"


@pytest.mark.asyncio
async def test_review_principal_allowed_on_storyboard_confirm_via_whitelist() -> None:
    bus = get_command_bus()
    with _as_principal(role="review"):
        result = await bus.execute_async(
            "storyboard.confirm", {"episode_id": "ep_missing_for_test"}
        )
    _assert_not_authz_rejected(result)


@pytest.mark.asyncio
async def test_review_principal_allowed_on_video_adopt_version_via_whitelist() -> None:
    bus = get_command_bus()
    with _as_principal(role="review"):
        result = await bus.execute_async(
            "video.adopt_version", {"shot_id": "shot_x", "version_id": "v1"}
        )
    _assert_not_authz_rejected(result)


@pytest.mark.asyncio
async def test_readonly_principal_cannot_use_human_gate_whitelist() -> None:
    """白名单的门槛是 manju:delivery，不是所有角色都有的 manju:read：
    否则只读角色也会被放行去确认分镜 / 采纳定稿，那是提权而不是审校的便利。"""
    bus = get_command_bus()
    with _as_principal(role="readonly"):
        confirm = await bus.execute_async(
            "storyboard.confirm", {"episode_id": "ep_missing_for_test"}
        )
        adopt = await bus.execute_async(
            "video.adopt_version", {"shot_id": "shot_x", "version_id": "v1"}
        )
    for result in (confirm, adopt):
        assert result.status == CommandStatus.REJECTED
        assert result.error_code == "forbidden_scope"


# ---------------------------------------------------------------------------
# admin_only：即便持有对应 scope，非系统管理员也不能碰
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_system_admin_rejected_on_admin_only_command() -> None:
    bus = get_command_bus()
    with _as_principal(role="workspace_admin"):
        result = await bus.execute_async("system.update_settings", {"patch": {}})
    assert result.status == CommandStatus.REJECTED
    assert result.error_code == "forbidden_admin_only"


# ---------------------------------------------------------------------------
# 系统管理员：以上全部放行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_admin_authorized_for_all_of_the_above() -> None:
    bus = get_command_bus()
    with _as_principal(role=None, is_system_admin=True):
        readonly_case = await bus.execute_async("video.generate_shot", {"shot_id": "shot_x"})
        delivery_case = await bus.execute_async(
            "delivery.submit_feedback",
            {"episode_id": "ep_missing_for_test", "feedback": "客户反馈内容"},
        )
        gate_case = await bus.execute_async(
            "storyboard.confirm", {"episode_id": "ep_missing_for_test"}
        )
        admin_only_case = await bus.execute_async("system.update_settings", {"patch": {}})
    for result in (readonly_case, delivery_case, gate_case, admin_only_case):
        _assert_not_authz_rejected(result)


# ---------------------------------------------------------------------------
# get_current_principal() is None：MCP / 内部调用路径必须保持放行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_principal_short_circuits_to_allowed() -> None:
    bus = get_command_bus()
    previous = get_current_principal()
    set_current_principal(None)
    try:
        result = await bus.execute_async("video.generate_shot", {"shot_id": "shot_x"})
    finally:
        set_current_principal(previous)
    _assert_not_authz_rejected(result)


# ---------------------------------------------------------------------------
# P0 回归：鉴权必须先于幂等缓存查找
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorization_precedes_idempotency_cache() -> None:
    """``_gate`` 在幂等缓存命中之后才跑；如果鉴权检查被挪到 ``_gate`` 附近，
    未授权调用者用完全相同的参数重放一次已授权请求，就会直接命中缓存、拿到
    别人那次调用的成功结果——读到了本不该看到的数据。

    这里用 production 角色先跑一次带 idempotency_key 的 dry_run（真实获得
    授权并写入幂等缓存），再用 readonly 角色重放同一份参数：必须被当场拒绝，
    而不是把缓存里的成功结果吐回来。
    """
    bus = get_command_bus()
    args = {"project_id": "proj_x", "idempotency_key": "rbac-cache-regression", "dry_run": True}

    with _as_principal(role="production"):
        first = await bus.execute_async("project.delete", args)
    assert first.status == CommandStatus.SUCCEEDED
    assert first.data.get("dry_run") is True

    with _as_principal(role="readonly"):
        replay = await bus.execute_async("project.delete", args)
    assert replay.status == CommandStatus.REJECTED
    assert replay.error_code == "forbidden_scope"
