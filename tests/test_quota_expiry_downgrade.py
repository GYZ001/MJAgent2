"""会员到期闸门（``app/quota_expiry.py``）与降级机制（``app/domain/projects/
downgrade.py``）的硬证明。

覆盖用户拍板的三条产品规则，每条各自独立可证伪：
1. 到期后不能发起新任务，但不掐在途任务——``assert_membership_active`` 直接
   单测 + 至少一条端到端接线（``app.media_exec.enqueue._begin_video_preflight_
   job``），外加"闸门函数本身不碰在途任务表"的结构性证明。
2. 降级后超额项目删最老的、保留最新的（按 ``created_at``），且是"全部预检通
   过才动手"的原子操作——中途失败时用独立连接重新读库，证明零项目被删。
3. 加量包余额长期保留，降级/到期完全不碰。

跟随 ``tests/test_quota.py`` 的既有约定：raw sqlite3 + ``get_conn()``/``now()``/
``new_id()``，不用 ORM；``_make_user``/``_make_project`` 风格的裸插入 helper。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import quota, quota_addon, quota_expiry
from app.auth.passwords import hash_password
from app.db import get_conn, new_id, now
from app.domain.projects import downgrade as downgrade_mod
from app.provider_task_clearance import ProviderTasksNotTerminalError


def _make_user(tier: str = "free", *, tier_expires_at: float | None = None) -> str:
    conn = get_conn()
    user_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier,
               quota_period_started_at, tier_expires_at
           ) VALUES(?,?,?,?,'local','active',0,0,?,?,?,?)""",
        (
            user_id, f"{tier}-{user_id}", "测试账号", hash_password("pw-test-000000"),
            now(), tier, now(), tier_expires_at,
        ),
    )
    conn.commit()
    return user_id


def _make_project(owner_user_id: str, *, created_at: float | None = None) -> str:
    conn = get_conn()
    project_id = new_id("proj")
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, owner_user_id) "
        "VALUES(?,?,?,?,?)",
        (project_id, "P", "created", created_at if created_at is not None else now(), owner_user_id),
    )
    conn.commit()
    return project_id


def _make_episode(project_id: str) -> str:
    conn = get_conn()
    episode_id = new_id("ep")
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) "
        "VALUES(?,?,?,?,?)",
        (episode_id, project_id, 1, "confirmed", now()),
    )
    conn.commit()
    return episode_id


def _make_shot(episode_id: str) -> str:
    conn = get_conn()
    shot_id = new_id("shot")
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,?)",
        (shot_id, episode_id, 1, 5),
    )
    conn.commit()
    return shot_id


# ---------------------------------------------------------------------------
# 规则 1：会员到期后拦新任务，不掐在途任务
# ---------------------------------------------------------------------------

def test_assert_membership_active_blocks_expired_tier():
    conn = get_conn()
    uid = _make_user("pro", tier_expires_at=now() - 3600.0)
    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota_expiry.assert_membership_active(conn, uid)
    detail = exc_info.value.detail
    assert detail["gate"] == "membership"
    assert detail["tier"] == "pro"
    assert "upgrade_path" in detail


def test_assert_membership_active_allows_null_and_future_expiry():
    conn = get_conn()
    never_expires = _make_user("pro", tier_expires_at=None)
    quota_expiry.assert_membership_active(conn, never_expires)  # 不抛，NULL=不过期

    future = _make_user("pro", tier_expires_at=now() + 3600.0)
    quota_expiry.assert_membership_active(conn, future)  # 不抛，还没到期

    free_tier = _make_user("free", tier_expires_at=None)
    quota_expiry.assert_membership_active(conn, free_tier)  # free 档天然不过期


def test_assert_membership_active_unknown_user_is_noop():
    conn = get_conn()
    quota_expiry.assert_membership_active(conn, "no-such-user-id")  # 不抛


def test_assert_membership_active_never_reads_inflight_task_tables():
    """闸门只读 users，结构上够不到任何在途任务——用产物信号验证：给一个已
    过期账号造一条"正在跑"的 job 与一条"正在跑"的 workflow_run，调用闸门（
    预期照样拦截），再原样核对这两行一个字段都没被动过。"""
    conn = get_conn()
    uid = _make_user("pro", tier_expires_at=now() - 10.0)
    project_id = _make_project(uid)
    episode_id = _make_episode(project_id)
    shot_id = _make_shot(episode_id)
    job_id = new_id("job")
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,episode_id,project_id,status,video_slot_active,
               created_at,updated_at
           ) VALUES(?,'video',?,?,?,'running',1,?,?)""",
        (job_id, shot_id, episode_id, project_id, now(), now()),
    )
    run_id = new_id("run")
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES(?,'storyboard','episode',?,'RUNNING','fp',?)""",
        (run_id, episode_id, now()),
    )
    conn.commit()
    job_before = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    run_before = dict(conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone())

    with pytest.raises(quota.QuotaExceeded):
        quota_expiry.assert_membership_active(conn, uid)

    job_after = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    run_after = dict(conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone())
    assert job_after == job_before, "在途任务不得被到期闸门碰到，哪怕一个字段"
    assert run_after == run_before, "在途 workflow_run 不得被到期闸门碰到，哪怕一个字段"


def test_video_preflight_job_creation_blocked_by_expired_membership():
    """端到端接线：直接调用生产代码里创建视频任务的真实函数，证明到期闸门
    真的接在了这条路径上（不是只有单元函数本身正确）。"""
    from app.media_exec import enqueue

    uid = _make_user("free", tier_expires_at=now() - 60.0)
    project_id = _make_project(uid)
    episode_id = _make_episode(project_id)
    shot_id = _make_shot(episode_id)
    conn = get_conn()

    with pytest.raises(quota.QuotaExceeded) as exc_info:
        enqueue._begin_video_preflight_job(shot_id, supervisor_run_id=None)
    assert exc_info.value.detail["gate"] == "membership"

    leftover = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE shot_id=?", (shot_id,)
    ).fetchone()["c"]
    assert leftover == 0, "被到期闸门拦截的视频任务不能留下任何 job 行"


# ---------------------------------------------------------------------------
# 规则 2：降级裁剪超额项目——最老先删、原子性
# ---------------------------------------------------------------------------

def test_trim_projects_to_tier_limit_keeps_newest_by_created_at():
    conn = get_conn()
    uid = _make_user("free")
    base = now() - 1000.0
    oldest = _make_project(uid, created_at=base)
    older = _make_project(uid, created_at=base + 10.0)
    newer = _make_project(uid, created_at=base + 20.0)
    newest = _make_project(uid, created_at=base + 30.0)

    import asyncio
    result = asyncio.run(downgrade_mod.trim_projects_to_tier_limit(conn, uid, "free"))

    assert result["kept_limit"] == 1
    assert set(result["deleted_project_ids"]) == {oldest, older, newer}
    assert result["deleted_count"] == 3

    rows = {
        row["id"]: row["deleted_at"]
        for row in conn.execute(
            "SELECT id, deleted_at FROM projects WHERE owner_user_id=?", (uid,)
        ).fetchall()
    }
    assert rows[newest] is None, "保留的最新项目不能被动"
    for pid in (oldest, older, newer):
        assert rows[pid] is not None, "超额项目必须是软删除（进回收站），不是硬删"


def test_trim_projects_to_tier_limit_noop_when_within_limit():
    conn = get_conn()
    uid = _make_user("standard")  # 限额 3
    p1 = _make_project(uid)
    p2 = _make_project(uid)

    import asyncio
    result = asyncio.run(downgrade_mod.trim_projects_to_tier_limit(conn, uid, "standard"))
    assert result == {"tier": "standard", "kept_limit": 3, "deleted_project_ids": [], "deleted_count": 0}
    for pid in (p1, p2):
        row = conn.execute("SELECT deleted_at FROM projects WHERE id=?", (pid,)).fetchone()
        assert row["deleted_at"] is None


def test_trim_projects_to_tier_limit_atomic_on_precheck_failure(monkeypatch):
    """中途失败必须"零项目被动"：预检全部跑完才真正开始删；第 2-倒数一个
    预检失败时，连已经预检通过的第一个也不能被删。用独立连接重新读库验证，
    不信任同一连接读自己的（本该没有的）写入。"""
    conn = get_conn()
    uid = _make_user("free")
    base = now() - 1000.0
    p1 = _make_project(uid, created_at=base)
    p2 = _make_project(uid, created_at=base + 10.0)
    p3 = _make_project(uid, created_at=base + 20.0)
    kept = _make_project(uid, created_at=base + 30.0)
    # excess（按 created_at 升序）= [p1, p2, p3]；"倒数第二个" = p2。
    fail_on = p2

    real_precheck = downgrade_mod.assert_provider_tasks_clearable

    def _fake_precheck(*, project_id, conn):
        if project_id == fail_on:
            raise ProviderTasksNotTerminalError({"safe_to_clear": False})
        return real_precheck(project_id=project_id, conn=conn)

    monkeypatch.setattr(downgrade_mod, "assert_provider_tasks_clearable", _fake_precheck)

    import asyncio
    with pytest.raises(ProviderTasksNotTerminalError):
        asyncio.run(downgrade_mod.trim_projects_to_tier_limit(conn, uid, "free"))

    from app import config
    verify_conn = sqlite3.connect(config.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    try:
        rows = {
            row["id"]: row["deleted_at"]
            for row in verify_conn.execute(
                "SELECT id, deleted_at FROM projects WHERE owner_user_id=?", (uid,)
            ).fetchall()
        }
    finally:
        verify_conn.close()
    for pid in (p1, p2, p3, kept):
        assert rows[pid] is None, f"预检阶段失败后不得有任何项目被删（{pid} 被删了）"


# ---------------------------------------------------------------------------
# 规则 3：加量包余额长期保留，降级/到期完全不碰
# ---------------------------------------------------------------------------

def test_sweep_expired_memberships_preserves_addon_balance():
    """加量包余额必须在"真的发生了裁剪"（有超额项目被删）的场景下也纹丝不
    动——只有账号名下无项目时才验证过于宽松，那样即便裁剪逻辑不小心碰了
    加量包也测不出来（trim 在无超额项目时直接早退，根本不会执行到那段代
    码），所以这里显式建够触发裁剪的项目数（pro 档限 6，建 8 个）。"""
    conn = get_conn()
    uid = _make_user("pro", tier_expires_at=now() - 10.0)
    base = now() - 1000.0
    for i in range(8):
        _make_project(uid, created_at=base + i * 10.0)
    quota_addon.grant_video_addon_seconds(conn, uid, packages=1, attempt_key="order:preserve")
    conn.commit()
    balance_before = quota.addon_video_seconds_balance(conn, uid)
    assert balance_before == 600.0

    import asyncio
    result = asyncio.run(downgrade_mod.sweep_expired_memberships())
    assert any(item["user_id"] == uid for item in result["downgraded"])

    balance_after = quota.addon_video_seconds_balance(conn, uid)
    assert balance_after == balance_before, "加量包余额降级/到期不得被动"

    row = conn.execute("SELECT tier, tier_expires_at FROM users WHERE id=?", (uid,)).fetchone()
    assert row["tier"] == "free"
    assert row["tier_expires_at"] is None


# ---------------------------------------------------------------------------
# 周期性扫描：翻档 + 裁剪，按账号隔离失败
# ---------------------------------------------------------------------------

def test_sweep_expired_memberships_downgrades_tier_and_trims_excess_projects():
    conn = get_conn()
    uid = _make_user("standard", tier_expires_at=now() - 10.0)  # standard 限 3
    base = now() - 1000.0
    ids = [_make_project(uid, created_at=base + i * 10.0) for i in range(5)]

    import asyncio
    result = asyncio.run(downgrade_mod.sweep_expired_memberships())

    assert result["downgraded_count"] == 1
    assert result["failed"] == []
    entry = result["downgraded"][0]
    assert entry["user_id"] == uid
    assert entry["from_tier"] == "standard"
    # free 档限 1：5 个项目应该只留最新的一个。
    assert set(entry["deleted_project_ids"]) == set(ids[:4])

    row = conn.execute("SELECT tier, tier_expires_at FROM users WHERE id=?", (uid,)).fetchone()
    assert row["tier"] == "free" and row["tier_expires_at"] is None


def test_sweep_expired_memberships_does_not_touch_unexpired_or_free_users():
    conn = get_conn()
    unexpired = _make_user("pro", tier_expires_at=now() + 3600.0)
    never_expires = _make_user("max", tier_expires_at=None)
    already_free = _make_user("free", tier_expires_at=now() - 10.0)

    import asyncio
    result = asyncio.run(downgrade_mod.sweep_expired_memberships())

    touched = {item["user_id"] for item in result["downgraded"]}
    assert unexpired not in touched
    assert never_expires not in touched
    assert already_free not in touched, "free 档没有可降的余地，不应出现在结果里"
    for uid, tier in ((unexpired, "pro"), (never_expires, "max"), (already_free, "free")):
        row = conn.execute("SELECT tier FROM users WHERE id=?", (uid,)).fetchone()
        assert row["tier"] == tier


def test_sweep_expired_memberships_isolates_failures_per_user(monkeypatch):
    """一个账号的裁剪失败（供应商任务未到终态）不得阻塞同一轮里其余到期
    账号的降级。"""
    conn = get_conn()
    broken_uid = _make_user("standard", tier_expires_at=now() - 10.0)
    base = now() - 1000.0
    broken_ids = [_make_project(broken_uid, created_at=base + i * 10.0) for i in range(4)]

    healthy_uid = _make_user("standard", tier_expires_at=now() - 10.0)
    for i in range(4):
        _make_project(healthy_uid, created_at=base + i * 10.0)

    real_precheck = downgrade_mod.assert_provider_tasks_clearable

    def _fake_precheck(*, project_id, conn):
        if project_id in broken_ids:
            raise ProviderTasksNotTerminalError({"safe_to_clear": False})
        return real_precheck(project_id=project_id, conn=conn)

    monkeypatch.setattr(downgrade_mod, "assert_provider_tasks_clearable", _fake_precheck)

    import asyncio
    result = asyncio.run(downgrade_mod.sweep_expired_memberships())

    downgraded_ids = {item["user_id"] for item in result["downgraded"]}
    failed_ids = {item["user_id"] for item in result["failed"]}
    assert healthy_uid in downgraded_ids, "健康账号不能被另一个账号的裁剪失败拖累"
    assert broken_uid in failed_ids

    healthy_kept = conn.execute(
        "SELECT COUNT(*) c FROM projects WHERE owner_user_id=? AND deleted_at IS NULL",
        (healthy_uid,),
    ).fetchone()["c"]
    assert healthy_kept == 1

    # broken_uid 的档位翻转是先提交的独立小步骤，即使随后裁剪失败也已经生效
    # （见 downgrade.sweep_expired_memberships 文档：翻转不回滚，下一轮重试裁剪）。
    row = conn.execute("SELECT tier FROM users WHERE id=?", (broken_uid,)).fetchone()
    assert row["tier"] == "free"
