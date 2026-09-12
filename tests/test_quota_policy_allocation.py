"""EP-04 第一阶段核心判定：三级（org/team/user）取最紧 + 迁移零变化 + 消息
内容（CLAUDE.md「断言要断言消息内容，不只断言状态码」）。

``app.quota.effective_limits`` 是全仓唯一入口，改造点只在它内部转交给
``app.quota_policy.allocation.resolve_effective_limits``；本文件同时覆盖两
层——直接调用 ``resolve_effective_limits`` 验证合并算法本身，和调用
``quota.effective_limits``/各配额闸门函数验证接线确实生效。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import config, quota
from app.auth.passwords import hash_password
from app.db import get_conn, new_id, now
from app.orgs import service as orgs_service
from app.orgs import store as orgs_store
from app.quota_policy import allocation as alloc
from app.quota_policy import plans as quota_plans
from app.quota_tiers import TIER_TABLE, VALID_TIERS


def _make_user(tier: str = "free", *, org_id: str | None = None) -> str:
    conn = get_conn()
    user_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier,
               quota_period_started_at, org_id
           ) VALUES(?,?,?,?,'local','active',0,0,?,?,?,?)""",
        (user_id, f"{tier}-{user_id}", "测试账号", hash_password("pw-test-000000"),
         now(), tier, now(), org_id),
    )
    conn.commit()
    return user_id


def _make_org_and_team(name: str = "内容中心") -> tuple[str, str]:
    org_id = orgs_service.create_org(name=f"组织-{new_id('x')}", created_by="test")
    team_id = orgs_service.create_team(org_id=org_id, name=name, description=None, created_by="test")
    return org_id, team_id


def _set_allocation(scope_type: str, scope_id: str, *, limits: dict, expires_at: float | None = None) -> str:
    conn = get_conn()
    plan_id = quota_plans.create_plan(
        conn, org_id=None, key=f"custom-{new_id('k')}", name="测试策略",
        limits=quota_plans.validate_limits_payload(limits), period_days=30, created_by="test",
    )
    alloc_id = alloc.set_allocation(
        conn, scope_type=scope_type, scope_id=scope_id, plan_id=plan_id,
        overrides=None, expires_at=expires_at, created_by="test",
    )
    conn.commit()
    return alloc_id


# ---------------------------------------------------------------------------
# 硬指标：迁移零变化
# ---------------------------------------------------------------------------


def test_migration_zero_change_across_all_five_tiers() -> None:
    """未配置任何组织/团队/用户级分配时，五档逐字段与改造前的 TIER_TABLE 完全
    相同（含 bound_by 为空）——这是本单元最高优先级的硬指标。"""
    conn = get_conn()
    for tier in sorted(VALID_TIERS):
        uid = _make_user(tier)
        result = quota.effective_limits(conn, uid)
        expected = TIER_TABLE[tier]
        for field in ("tier", "projects", "concurrency", "token", "video_seconds", "image"):
            assert getattr(result, field) == getattr(expected, field), (
                f"{tier} 档字段 {field} 迁移前后不一致：{getattr(result, field)!r} != "
                f"{getattr(expected, field)!r}"
            )
        assert dict(result.bound_by) == {}, f"{tier} 档默认状态下 bound_by 应为空: {result.bound_by}"


# ---------------------------------------------------------------------------
# 三级取最紧：逐维度验证
# ---------------------------------------------------------------------------


def test_org_level_allocation_tightens_concurrency() -> None:
    org_id, _ = _make_org_and_team()
    uid = _make_user("max", org_id=org_id)  # max 档 concurrency=10
    _set_allocation("org", org_id, limits={"concurrency": 2})
    result = quota.effective_limits(get_conn(), uid)
    assert result.concurrency == 2
    assert result.bound_by["concurrency"].startswith("org=")
    # 未被组织策略覆盖的维度保持 builtin 不变
    assert result.token == TIER_TABLE["max"].token


def test_team_level_tighter_than_org_wins() -> None:
    conn = get_conn()
    org_id, team_id = _make_org_and_team()
    uid = _make_user("max", org_id=org_id)
    orgs_store.add_team_member(conn, team_id=team_id, user_id=uid, role_id="role_dummy", created_by="test")
    conn.commit()
    _set_allocation("org", org_id, limits={"token": 500_000})
    _set_allocation("team", team_id, limits={"token": 100_000})
    result = quota.effective_limits(conn, uid)
    assert result.token == 100_000
    assert result.bound_by["token"].startswith("team=")


def test_org_tighter_than_team_still_wins_the_min() -> None:
    """取最紧不是"更具体的层级优先"，是"数值更小的优先"——团队分配的数字比
    组织松时，组织仍然是最终生效值。"""
    conn = get_conn()
    org_id, team_id = _make_org_and_team()
    uid = _make_user("max", org_id=org_id)
    orgs_store.add_team_member(conn, team_id=team_id, user_id=uid, role_id="role_dummy", created_by="test")
    conn.commit()
    _set_allocation("org", org_id, limits={"video_seconds": 60.0})
    _set_allocation("team", team_id, limits={"video_seconds": 600.0})
    result = quota.effective_limits(conn, uid)
    assert result.video_seconds == 60.0
    assert result.bound_by["video_seconds"].startswith("org=")


def test_user_level_allocation_can_be_the_tightest() -> None:
    conn = get_conn()
    org_id, _ = _make_org_and_team()
    uid = _make_user("max", org_id=org_id)
    _set_allocation("org", org_id, limits={"projects": 5})
    _set_allocation("user", uid, limits={"projects": 1})
    result = quota.effective_limits(conn, uid)
    assert result.projects == 1
    assert result.bound_by["projects"].startswith("user=")


def test_user_belongs_to_multiple_teams_takes_the_tightest_team() -> None:
    conn = get_conn()
    org_id, team_a = _make_org_and_team("团队A")
    _, team_b = _make_org_and_team("团队B")
    uid = _make_user("max", org_id=org_id)
    orgs_store.add_team_member(conn, team_id=team_a, user_id=uid, role_id="role_dummy", created_by="test")
    orgs_store.add_team_member(conn, team_id=team_b, user_id=uid, role_id="role_dummy", created_by="test")
    conn.commit()
    _set_allocation("team", team_a, limits={"image": 5_000_000})
    _set_allocation("team", team_b, limits={"image": 1_000_000})
    result = quota.effective_limits(conn, uid)
    assert result.image == 1_000_000
    assert "team=团队B" in result.bound_by["image"]


def test_none_override_does_not_loosen_below_builtin() -> None:
    """None（不限）只在上级也不限时才生效：builtin 永远是候选池里的具体数字，
    team 显式声明"这一维不限"不会让最终结果变成 None。"""
    conn = get_conn()
    org_id, team_id = _make_org_and_team()
    uid = _make_user("free", org_id=org_id)  # free 档 video_seconds=60
    orgs_store.add_team_member(conn, team_id=team_id, user_id=uid, role_id="role_dummy", created_by="test")
    conn.commit()
    _set_allocation("team", team_id, limits={"video_seconds": None})
    result = quota.effective_limits(conn, uid)
    assert result.video_seconds == 60.0
    assert "video_seconds" not in result.bound_by


def test_missing_key_in_plan_inherits_not_unlimited() -> None:
    """plan.limits_json 缺键＝未配置，继承上级，不是"这一维不限"。"""
    conn = get_conn()
    org_id, _ = _make_org_and_team()
    uid = _make_user("standard", org_id=org_id)
    _set_allocation("org", org_id, limits={"token": 100})  # 只设 token，其余四维缺键
    result = quota.effective_limits(conn, uid)
    assert result.token == 100
    assert result.projects == TIER_TABLE["standard"].projects
    assert result.concurrency == TIER_TABLE["standard"].concurrency
    assert result.image == TIER_TABLE["standard"].image


def test_expired_allocation_is_treated_as_absent() -> None:
    conn = get_conn()
    org_id, _ = _make_org_and_team()
    uid = _make_user("max", org_id=org_id)
    _set_allocation("org", org_id, limits={"concurrency": 1}, expires_at=now() - 3600)
    result = quota.effective_limits(conn, uid)
    assert result.concurrency == TIER_TABLE["max"].concurrency
    assert "concurrency" not in result.bound_by


def test_org_and_team_lookup_degrades_gracefully_on_minimal_test_double(monkeypatch) -> None:
    """``users.org_id``/``team_members`` 在手写简化 schema 里可能压根不存
    在——退化为"这个账号没有组织/团队"，不能让整条判定链路崩溃（与
    ``app.quota._user_row`` 对同类既有测试双的处理同一条约定）。"""
    from tests.conftest import patch_orgs_everywhere

    conn = get_conn()

    def _boom(*args, **kwargs):
        import sqlite3
        raise sqlite3.OperationalError("no such column: org_id")

    patch_orgs_everywhere(monkeypatch, "user_org_id", _boom)
    patch_orgs_everywhere(monkeypatch, "list_team_ids_for_user", _boom)
    uid = _make_user("free")
    result = quota.effective_limits(conn, uid)
    for field in ("tier", "projects", "concurrency", "token", "video_seconds", "image"):
        assert getattr(result, field) == getattr(TIER_TABLE["free"], field)


# ---------------------------------------------------------------------------
# QuotaExceeded 消息内容：写清是哪一级、哪条策略挡的
# ---------------------------------------------------------------------------


def test_concurrency_message_names_the_binding_team() -> None:
    conn = get_conn()
    org_id, team_id = _make_org_and_team("内容中心")
    uid = _make_user("max", org_id=org_id)
    orgs_store.add_team_member(conn, team_id=team_id, user_id=uid, role_id="role_dummy", created_by="test")
    conn.commit()
    _set_allocation("team", team_id, limits={"concurrency": 1})
    with pytest.raises(HTTPException) as exc_info:
        quota.check_module_concurrency(conn, uid, "video", active_count=1)
    detail = exc_info.value.detail
    assert "内容中心" in detail["message"]
    assert "team=内容中心" in detail["message"]
    assert detail["gate"] == "concurrency"
    assert detail["limit"] == 1


def test_token_message_stays_unchanged_when_no_allocation_configured() -> None:
    """默认状态（迁移零变化）：消息文案与改造前逐字相同。"""
    conn = get_conn()
    uid = _make_user("free")
    quota.charge_tokens(conn, uid, TIER_TABLE["free"].token, attempt_key="attempt-1")
    conn.commit()
    with pytest.raises(HTTPException) as exc_info:
        quota.assert_token_capacity(conn, uid)
    assert exc_info.value.detail["message"] == (
        f"30 天 token 额度已用尽（free 档上限 {int(TIER_TABLE['free'].token)}）"
    )


# ---------------------------------------------------------------------------
# 企业形态升级文案
# ---------------------------------------------------------------------------


def test_upgrade_path_default_saas_text_unchanged() -> None:
    conn = get_conn()
    uid = _make_user("free")
    with pytest.raises(HTTPException) as exc_info:
        quota.check_project_slot(conn, uid, active_count=TIER_TABLE["free"].projects)
    assert exc_info.value.detail["upgrade_path"].startswith("升级到入门档位")


def test_upgrade_path_enterprise_mentions_admin_contact(monkeypatch) -> None:
    """真实形状：生产库至少存在一个 is_system_admin=1 账号（不变式，见
    ``app/auth/admin_api.py`` 禁止删除最后一个管理员），所以先造一个，而不是
    在空库上验证"找不到联系人退化为空字符串"这条边界分支。"""
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "enterprise")
    conn = get_conn()
    admin_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier
           ) VALUES(?,?,?,?,'local','active',1,0,?,'max')""",
        (admin_id, f"admin-{admin_id}", "系统管理员", hash_password("pw-test-000000"), now()),
    )
    conn.commit()
    uid = _make_user("free")
    with pytest.raises(HTTPException) as exc_info:
        quota.check_project_slot(conn, uid, active_count=TIER_TABLE["free"].projects)
    upgrade_path = exc_info.value.detail["upgrade_path"]
    assert "联系组织管理员申请额度" in upgrade_path
    assert "升级到" not in upgrade_path
    assert "管理员：" in upgrade_path
