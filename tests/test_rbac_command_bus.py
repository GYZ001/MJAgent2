"""Command Bus 对 ``CommandSpec.admin_only`` 的强制执行。

账号即项目空间落地后，团队角色（workspace_admin/production/review/readonly）
连同它们对 ``CommandSpec.scopes`` 的差异化判定一并退场：任何已登录账号对自己
名下的项目天然拥有全部操作 scope（``Principal.all_scopes`` 恒为
``ALL_SCOPES``，见 ``app/auth/principal.py``），Command Bus 层不再需要、也不再
能够按 scope 区分"这个角色能不能做这类操作"——唯一还有意义的判据是
``admin_only``（是不是系统管理员专属命令）。「这个具体资源是不是你的项目」
不在这一层判断，见 ``app/authz/resolve.py``。

``tests/conftest.py`` 的 autouse fixture 默认给每个测试注入一个系统管理员
Principal（历史测试不需要逐个改造）。本文件里凡是要验证非管理员的行为，都
通过 ``_as_principal`` 临时换成普通账号身份，用完立即还原，避免污染后续测试。
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
        "INSERT INTO projects(id, name, status, owner_user_id, created_at) VALUES(?,?,?,?,?)",
        ("proj_x", "测试项目", "created", "test-plain-user", db.now()),
    )
    conn.commit()
    yield


@contextmanager
def _as_principal(*, is_system_admin: bool, user_id: str = "test-plain-user"):
    """临时切换当前 Principal，退出时还原成进入前的身份。"""
    previous = get_current_principal()
    set_current_principal(
        Principal(user_id=user_id, username=user_id, is_system_admin=is_system_admin)
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
# 普通账号：对自己名下的项目拥有全部操作 scope（不再有 readonly/review 差异化）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_account_allowed_on_generation_and_delivery_commands() -> None:
    """普通账号不再按角色区分 generation-media / delivery 等 scope——只要不是
    admin_only 命令，都不会被 Command Bus 这一层拦。"""
    bus = get_command_bus()
    with _as_principal(is_system_admin=False):
        generation = await bus.execute_async("video.generate_shot", {"shot_id": "shot_x"})
        delivery = await bus.execute_async(
            "delivery.submit_feedback",
            {"episode_id": "ep_missing_for_test", "feedback": "客户反馈内容"},
        )
        gate = await bus.execute_async(
            "storyboard.confirm", {"episode_id": "ep_missing_for_test"}
        )
    for result in (generation, delivery, gate):
        _assert_not_authz_rejected(result)


# ---------------------------------------------------------------------------
# admin_only：即便是已登录账号，非系统管理员也不能碰
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_system_admin_rejected_on_admin_only_command() -> None:
    bus = get_command_bus()
    with _as_principal(is_system_admin=False):
        result = await bus.execute_async("system.update_settings", {"patch": {}})
    assert result.status == CommandStatus.REJECTED
    assert result.error_code == "forbidden_admin_only"


# ---------------------------------------------------------------------------
# 系统管理员：以上全部放行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_admin_authorized_for_all_of_the_above() -> None:
    bus = get_command_bus()
    with _as_principal(is_system_admin=True, user_id="test-sysadmin"):
        generation_case = await bus.execute_async("video.generate_shot", {"shot_id": "shot_x"})
        delivery_case = await bus.execute_async(
            "delivery.submit_feedback",
            {"episode_id": "ep_missing_for_test", "feedback": "客户反馈内容"},
        )
        gate_case = await bus.execute_async(
            "storyboard.confirm", {"episode_id": "ep_missing_for_test"}
        )
        admin_only_case = await bus.execute_async("system.update_settings", {"patch": {}})
    for result in (generation_case, delivery_case, gate_case, admin_only_case):
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

    这里用系统管理员先跑一次带 idempotency_key 的 admin_only 命令（真实获得
    授权并写入幂等缓存），再用普通账号身份重放同一份参数：必须被当场拒绝，
    而不是把缓存里的成功结果吐回来。角色差异化退场后，普通账号与系统管理员
    之间唯一还有意义的鉴权轴就是 admin_only，所以这条回归改用它来复现同一个
    问题。
    """
    bus = get_command_bus()
    args = {"patch": {}, "idempotency_key": "rbac-cache-regression", "dry_run": True}

    with _as_principal(is_system_admin=True, user_id="test-sysadmin"):
        first = await bus.execute_async("system.update_settings", args)
    assert first.status == CommandStatus.SUCCEEDED, (first.status, first.summary)

    with _as_principal(is_system_admin=False):
        replay = await bus.execute_async("system.update_settings", args)
    assert replay.status == CommandStatus.REJECTED
    assert replay.error_code == "forbidden_admin_only"
