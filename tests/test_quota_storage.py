"""EP-04 第二阶段：存储维度（app/quota_policy/storage.py + app/quota_project.py
的 assert_storage_capacity）。判据从数据推导：目录/文件实测字节数，不挂状态
字段；超限不删数据，只拒绝新生成 + 给出清理候选清单（CLAUDE.md「Gates and
Criteria」/「超限行为」）。

真实形状造数据（CLAUDE.md「测试数据按真实形状造」）：真实 shots/shot_versions/
episodes 行 + 真实磁盘文件，不用最小可复现的字面量占位。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app import quota, quota_project
from app.db import get_conn, new_id, now
from app.quota_policy import allocation as alloc
from app.quota_policy import plans as quota_plans
from app.quota_policy import storage as quota_storage
from app.quota_tiers import TIER_TABLE


def _make_user(tier: str = "free") -> str:
    conn = get_conn()
    user_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier,
               quota_period_started_at
           ) VALUES(?,?,?,'local','active',0,0,?,?,?)""",
        (user_id, f"{tier}-{user_id}", "测试账号", now(), tier, now()),
    )
    conn.commit()
    return user_id


def _make_project(owner_user_id: str, project_id: str | None = None) -> str:
    conn = get_conn()
    project_id = project_id or new_id("proj")
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, owner_user_id) VALUES(?,?,?,?,?)",
        (project_id, "P", "created", now(), owner_user_id),
    )
    conn.commit()
    return project_id


_episode_no_seq: dict[str, int] = {}


def _make_shot_version(
    conn: sqlite3.Connection, project_id: str, *, adopted: bool, status: str = "succeeded",
    video_path: Path | None = None,
) -> tuple[str, str]:
    """建一条真实的 episode/shot/shot_version，video_path 指向真实磁盘文件
    （不存在的路径会被 cleanup_candidates 的 stat() 静默跳过）。返回
    (shot_id, version_id)。每次调用各开一集（真实数据不会一集塞多个不同镜位
    的独立版本），episode_no 按项目自增，避免撞 UNIQUE(project_id, episode_no)。"""
    episode_no = _episode_no_seq.get(project_id, 0) + 1
    _episode_no_seq[project_id] = episode_no
    episode_id = new_id("ep")
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, status, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (episode_id, project_id, episode_no, f"E{episode_no}", "planned", now()),
    )
    shot_id = new_id("shot")
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,?)",
        (shot_id, episode_id, 1, 15),
    )
    version_id = new_id("ver")
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, "
        "video_path, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (version_id, shot_id, 1, "prompt", new_id("idem"), status, str(video_path) if video_path else None, now()),
    )
    if adopted:
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.commit()
    return shot_id, version_id


# ---------------------------------------------------------------------------
# 档位默认值
# ---------------------------------------------------------------------------


def test_storage_bytes_defaults_unlimited_across_all_five_tiers() -> None:
    """迁移零变化的延伸：五档 TIER_TABLE 的 storage_bytes 全部保持 None（不
    限），存储治理是新维度，默认不新增拦截面，管理员需要收紧时走
    quota_allocations 显式配置。"""
    for tier, limits in TIER_TABLE.items():
        assert limits.storage_bytes is None, f"{tier} 档 storage_bytes 默认应为 None"


def test_project_concurrency_defaults_are_half_of_account_concurrency_at_least_one() -> None:
    expected = {"free": 1, "starter": 1, "standard": 1, "pro": 3, "max": 5}
    for tier, limits in TIER_TABLE.items():
        assert limits.project_concurrency == expected[tier], (
            f"{tier} 档 project_concurrency 期望 {expected[tier]}，实际 {limits.project_concurrency}"
        )
        assert limits.project_concurrency >= 1


# ---------------------------------------------------------------------------
# 采样：异步 + 超时 + 独立连接写入 + 带时间戳
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sample_project_storage_writes_real_bytes_with_timestamp(tmp_path) -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    project_dir = tmp_path / project_id
    project_dir.mkdir()
    (project_dir / "a.mp4").write_bytes(b"x" * 1000)
    (project_dir / "b.mp4").write_bytes(b"y" * 2000)

    before = time.time()
    result = await quota_storage.sample_project_storage(project_id, owner, project_dir)
    after = time.time()

    assert result["status"] == "ok"
    assert result["bytes_total"] == 3000
    assert before <= result["sampled_at"] <= after + 1

    conn = get_conn()
    fetched = quota_storage.latest_sample(conn, project_id)
    assert fetched is not None
    assert fetched["bytes_total"] == 3000
    assert fetched["sampled_at"] == result["sampled_at"]


@pytest.mark.asyncio
async def test_sample_all_projects_once_samples_every_active_project_and_skips_deleted(tmp_path, monkeypatch) -> None:
    """周期性巡检入口：给全部未软删除项目各采一次样，软删除项目不参与。"""
    owner = _make_user("free")
    project_active = _make_project(owner)
    project_deleted = _make_project(owner)
    conn = get_conn()
    conn.execute("UPDATE projects SET deleted_at=? WHERE id=?", (now(), project_deleted))
    conn.commit()

    from app import config
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    (tmp_path / project_active).mkdir()
    (tmp_path / project_active / "f.mp4").write_bytes(b"x" * 42)

    result = await quota_storage.sample_all_projects_once()
    assert result["total"] == 1, "软删除项目不该被巡检采样"
    assert result["ok"] == 1

    sample = quota_storage.latest_sample(conn, project_active)
    assert sample is not None
    assert sample["bytes_total"] == 42
    assert quota_storage.latest_sample(conn, project_deleted) is None


@pytest.mark.asyncio
async def test_sample_project_storage_missing_directory_is_zero_not_error(tmp_path) -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    result = await quota_storage.sample_project_storage(project_id, owner, tmp_path / "does-not-exist")
    assert result["status"] == "ok"
    assert result["bytes_total"] == 0


@pytest.mark.asyncio
async def test_sample_project_storage_times_out_without_blocking_forever(tmp_path) -> None:
    """超时是真实约束，不是摆设：给一个会阻塞的采样函数打桩，验证
    ``asyncio.wait_for`` 真的会在超时后放行并落一行 status='timeout'。"""
    owner = _make_user("free")
    project_id = _make_project(owner)

    def _slow_walk(_root):
        time.sleep(0.5)
        return 999

    import app.quota_policy.storage as storage_mod
    original = storage_mod._dir_size_bytes
    storage_mod._dir_size_bytes = _slow_walk
    try:
        result = await quota_storage.sample_project_storage(project_id, owner, tmp_path, timeout_s=0.05)
    finally:
        storage_mod._dir_size_bytes = original
    assert result["status"] == "timeout"
    assert result["bytes_total"] == 0
    assert "超时" in result["error"]


def test_latest_sample_picks_the_most_recent_row() -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    quota_storage._write_storage_sample(project_id, owner, 100, 0.1, "ok", None)
    time.sleep(0.01)
    newest = quota_storage._write_storage_sample(project_id, owner, 500, 0.1, "ok", None)

    conn = get_conn()
    fetched = quota_storage.latest_sample(conn, project_id)
    assert fetched["bytes_total"] == 500
    assert fetched["id"] == newest["id"]


# ---------------------------------------------------------------------------
# 聚合：按账号名下全部项目求和，软删除项目不计入
# ---------------------------------------------------------------------------


def test_bytes_used_for_user_ids_sums_across_projects_and_reports_oldest_sample() -> None:
    owner = _make_user("free")
    project_a = _make_project(owner)
    project_b = _make_project(owner)
    quota_storage._write_storage_sample(project_a, owner, 1_000_000, 0.1, "ok", None)
    time.sleep(0.01)
    quota_storage._write_storage_sample(project_b, owner, 2_000_000, 0.1, "ok", None)

    conn = get_conn()
    total, oldest = quota_storage.bytes_used_for_user_ids(conn, [owner])
    assert total == 3_000_000.0
    assert oldest is not None


def test_bytes_used_for_user_ids_excludes_soft_deleted_projects() -> None:
    owner = _make_user("free")
    project_a = _make_project(owner)
    project_b = _make_project(owner)
    quota_storage._write_storage_sample(project_a, owner, 1_000_000, 0.1, "ok", None)
    quota_storage._write_storage_sample(project_b, owner, 5_000_000, 0.1, "ok", None)
    conn = get_conn()
    conn.execute("UPDATE projects SET deleted_at=? WHERE id=?", (now(), project_b))
    conn.commit()

    total, _ = quota_storage.bytes_used_for_user_ids(conn, [owner])
    assert total == 1_000_000.0


def test_bytes_used_for_user_ids_empty_when_never_sampled() -> None:
    owner = _make_user("free")
    _make_project(owner)
    conn = get_conn()
    total, sampled_at = quota_storage.bytes_used_for_user_ids(conn, [owner])
    assert total == 0.0
    assert sampled_at is None


# ---------------------------------------------------------------------------
# 清理候选：未被采纳的最大 N 个成功版本
# ---------------------------------------------------------------------------


def test_cleanup_candidates_excludes_adopted_and_non_succeeded_versions(tmp_path) -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    conn = get_conn()

    adopted_path = tmp_path / "adopted.mp4"
    adopted_path.write_bytes(b"a" * 9_999_999)  # 最大，但已采纳，不该出现
    _make_shot_version(conn, project_id, adopted=True, status="succeeded", video_path=adopted_path)

    failed_path = tmp_path / "failed.mp4"
    failed_path.write_bytes(b"f" * 500)
    _make_shot_version(conn, project_id, adopted=False, status="failed", video_path=failed_path)

    small_path = tmp_path / "small.mp4"
    small_path.write_bytes(b"s" * 100)
    _, small_version = _make_shot_version(conn, project_id, adopted=False, status="succeeded", video_path=small_path)

    big_path = tmp_path / "big.mp4"
    big_path.write_bytes(b"b" * 800)
    _, big_version = _make_shot_version(conn, project_id, adopted=False, status="succeeded", video_path=big_path)

    candidates = quota_storage.cleanup_candidates(conn, project_id)
    version_ids = [c["version_id"] for c in candidates]
    assert version_ids == [big_version, small_version], "只留未采纳的 succeeded 版本，按字节数降序"
    assert candidates[0]["bytes"] == 800
    assert candidates[1]["bytes"] == 100


def test_cleanup_candidates_respects_limit(tmp_path) -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    conn = get_conn()
    for i in range(5):
        path = tmp_path / f"v{i}.mp4"
        path.write_bytes(b"x" * (100 + i))
        _make_shot_version(conn, project_id, adopted=False, status="succeeded", video_path=path)
    candidates = quota_storage.cleanup_candidates(conn, project_id, limit=2)
    assert len(candidates) == 2


def test_cleanup_candidates_skips_files_missing_from_disk(tmp_path) -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    conn = get_conn()
    ghost_path = tmp_path / "ghost.mp4"  # 从不创建：文件已经不在磁盘上
    _make_shot_version(conn, project_id, adopted=False, status="succeeded", video_path=ghost_path)
    candidates = quota_storage.cleanup_candidates(conn, project_id)
    assert candidates == []


# ---------------------------------------------------------------------------
# 清理执行：DB 先落地再删文件（原子性），不删任何被采纳或未确认的版本
# ---------------------------------------------------------------------------


def test_execute_cleanup_deletes_db_row_and_unlinks_file(tmp_path) -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    conn = get_conn()
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * 12345)
    _, version_id = _make_shot_version(conn, project_id, adopted=False, status="succeeded", video_path=path)

    result = quota_storage.execute_cleanup(conn, project_id, [version_id], actor="tester")

    assert result["deleted"] == [version_id]
    assert result["skipped"] == []
    assert result["bytes_freed"] == 12345
    assert not path.exists(), "确认新状态落盘后应当真的删除磁盘文件"
    row = conn.execute("SELECT status, video_path FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    assert row["status"] == "rejected_cleanup"
    assert row["video_path"] is None


def test_execute_cleanup_refuses_currently_adopted_version(tmp_path) -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    conn = get_conn()
    path = tmp_path / "adopted.mp4"
    path.write_bytes(b"x" * 500)
    _, version_id = _make_shot_version(conn, project_id, adopted=True, status="succeeded", video_path=path)

    result = quota_storage.execute_cleanup(conn, project_id, [version_id], actor="tester")

    assert result["deleted"] == []
    assert result["skipped"][0]["version_id"] == version_id
    assert "采纳" in result["skipped"][0]["reason"]
    assert path.exists(), "拒绝清单里的文件不能被删"
    row = conn.execute("SELECT status FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    assert row["status"] == "succeeded", "DB 状态也不能被动"


def test_execute_cleanup_db_state_survives_disk_unlink_failure(tmp_path, monkeypatch) -> None:
    """CLAUDE.md「破坏性操作要有原子性」的另一半：DB 状态已经确认成功即使磁盘
    unlink 失败也不回滚——文件已被业务判定可丢弃，残留只是运维问题。"""
    owner = _make_user("free")
    project_id = _make_project(owner)
    conn = get_conn()
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * 42)
    _, version_id = _make_shot_version(conn, project_id, adopted=False, status="succeeded", video_path=path)

    def _boom(self):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(Path, "unlink", _boom)
    result = quota_storage.execute_cleanup(conn, project_id, [version_id], actor="tester")

    assert result["deleted"] == [version_id]
    row = conn.execute("SELECT status FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    assert row["status"] == "rejected_cleanup", "unlink 失败不影响已经提交的 DB 状态"


# ---------------------------------------------------------------------------
# assert_storage_capacity 闸门：接入三级取最紧判定，超限不删数据只拒绝新生成
# ---------------------------------------------------------------------------


def test_assert_storage_capacity_allows_when_no_allocation_configured() -> None:
    """默认（无组织/团队/用户级分配）storage_bytes 继承 builtin=None，不限，
    无论实测占用多大都不拦截——存储治理默认关闭，管理员按需开启。"""
    owner = _make_user("free")
    project_id = _make_project(owner)
    quota_storage._write_storage_sample(project_id, owner, 999_999_999_999, 0.1, "ok", None)
    conn = get_conn()
    quota_project.assert_storage_capacity(conn, owner, project_id)  # 不应抛异常


def test_assert_storage_capacity_blocks_when_allocation_sets_a_tight_limit() -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    quota_storage._write_storage_sample(project_id, owner, 500, 0.1, "ok", None)
    conn = get_conn()
    plan_id = quota_plans.create_plan(
        conn, org_id=None, key=f"tight-{new_id('k')}", name="紧存储策略",
        limits=quota_plans.validate_limits_payload({"storage_bytes": 100}),
        period_days=30, created_by="test",
    )
    alloc.set_allocation(
        conn, scope_type="user", scope_id=owner, plan_id=plan_id,
        overrides=None, expires_at=None, created_by="test",
    )
    conn.commit()

    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota_project.assert_storage_capacity(conn, owner, project_id)
    detail = exc_info.value.detail
    assert detail["gate"] == "storage"
    assert "存储占用已达" in detail["message"]
    assert project_id in detail["message"], "消息必须点名是哪个项目触发的拒绝"
    assert "清理建议清单" in detail["message"], "拦人必须给出路"


def test_assert_storage_capacity_allows_exactly_at_remaining_headroom() -> None:
    owner = _make_user("free")
    project_id = _make_project(owner)
    quota_storage._write_storage_sample(project_id, owner, 50, 0.1, "ok", None)
    conn = get_conn()
    plan_id = quota_plans.create_plan(
        conn, org_id=None, key=f"loose-{new_id('k')}", name="宽松存储策略",
        limits=quota_plans.validate_limits_payload({"storage_bytes": 100}),
        period_days=30, created_by="test",
    )
    alloc.set_allocation(
        conn, scope_type="user", scope_id=owner, plan_id=plan_id,
        overrides=None, expires_at=None, created_by="test",
    )
    conn.commit()
    quota_project.assert_storage_capacity(conn, owner, project_id)  # 50 < 100，不应抛异常


def test_assert_storage_capacity_does_not_delete_anything_on_block(tmp_path) -> None:
    """超限只拒绝新生成，绝不自动删除任何现有文件/数据（CLAUDE.md 明确要求）。"""
    owner = _make_user("free")
    project_id = _make_project(owner)
    conn = get_conn()
    path = tmp_path / "existing.mp4"
    path.write_bytes(b"x" * 500)
    _make_shot_version(conn, project_id, adopted=False, status="succeeded", video_path=path)
    quota_storage._write_storage_sample(project_id, owner, 500, 0.1, "ok", None)
    plan_id = quota_plans.create_plan(
        conn, org_id=None, key=f"tight2-{new_id('k')}", name="紧存储策略2",
        limits=quota_plans.validate_limits_payload({"storage_bytes": 10}),
        period_days=30, created_by="test",
    )
    alloc.set_allocation(
        conn, scope_type="user", scope_id=owner, plan_id=plan_id,
        overrides=None, expires_at=None, created_by="test",
    )
    conn.commit()

    with pytest.raises(quota.QuotaExceeded):
        quota_project.assert_storage_capacity(conn, owner, project_id)
    assert path.exists(), "拒绝生成不等于删数据"
    row = conn.execute("SELECT COUNT(*) AS c FROM shot_versions").fetchone()
    assert row["c"] == 1, "现有版本行必须原封不动"
