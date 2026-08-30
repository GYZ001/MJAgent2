"""定妆照/场景图批量生成入口的账号维度并发准入（补的是此前完全没有账号并发
闸门的缺口：free 档账号此前可以在多个项目里同时点「全部生成」，互不相干，
账号并发上限形同虚设）。

与 app/domain/screenplay_ops/guarded.py::_screenplay_guarded /
app/domain/storyboard_ops/task_run.py::_storyboard_guarded_recorded 同一套判据
（app.quota.check_module_concurrency，挂 workflow_runs 里实际活跃的行数，不挂
内存计数器）——本文件证明 _refs_task/_scene_refs_task 也接上了同一套闸门。
"""
from __future__ import annotations

import asyncio

import app.refs as refs_module
import app.scenes as scenes_module
from app import quota
from app.db import get_conn, new_id, now
from app.domain.bible_ops.refs_generation import _refs_task
from app.domain.bible_ops.scene_bible_prep import _scene_refs_task
from app.orchestration.engine import WorkflowRecorder, fingerprint


def _make_user(tier: str) -> str:
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


def _make_project(owner_user_id: str) -> str:
    conn = get_conn()
    project_id = new_id("proj")
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, owner_user_id) "
        "VALUES(?,?,?,?,?)",
        (project_id, "P", "created", now(), owner_user_id),
    )
    conn.commit()
    return project_id


def _start_active_run(project_id: str, workflow_type: str) -> str:
    """建一个已经在跑（RUNNING）的同类 run，模拟"账号名下已有一个批量出图任务
    在跑"——与 _new_refs_recorder/_scene_refs_task 内部真实创建 run 的方式完全
    一致（同一个 WorkflowRecorder.create + .start()），不是手搓一行数据库记录。
    """
    recorder = WorkflowRecorder.create(
        workflow_type=workflow_type,
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, workflow_type, "existing"),
    )
    recorder.start()
    return recorder.run_id


async def _never_called(*_args, **_kwargs):
    raise AssertionError("generate_refs/generate_scene_refs 不应该在配额被拦下之后还被调用")


def test_refs_task_blocked_when_account_already_at_concurrency_limit(monkeypatch) -> None:
    """free 档（并发上限 1）：账号名下已有一个 character_references 批次在跑时，
    第二个批次必须被账号并发闸门拦下，且不会触发任何真实出图调用。"""
    owner = _make_user("free")
    project_id = _make_project(owner)
    _start_active_run(project_id, "character_references")

    monkeypatch.setattr(refs_module, "generate_refs", _never_called)

    asyncio.run(_refs_task(project_id, None))

    conn = get_conn()
    row = conn.execute(
        "SELECT refs_status, refs_error FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    assert row["refs_status"] == "failed"
    assert "portrait" in row["refs_error"]
    assert "free 档上限" in row["refs_error"]
    assert "1 个" in row["refs_error"]


def test_scene_refs_task_blocked_when_account_already_at_concurrency_limit(monkeypatch) -> None:
    """场景图批量出图同理：scene_references 与 character_references 各自独立
    的 module 标签，互不挤占彼此的账号并发额度，但各自单独遵守账号上限。"""
    owner = _make_user("free")
    project_id = _make_project(owner)
    _start_active_run(project_id, "scene_references")

    monkeypatch.setattr(scenes_module, "generate_scene_refs", _never_called)

    asyncio.run(_scene_refs_task(project_id, None))

    conn = get_conn()
    row = conn.execute(
        "SELECT scene_refs_status, scene_refs_error FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    assert row["scene_refs_status"] == "failed"
    assert "scene_ref" in row["scene_refs_error"]
    assert "free 档上限" in row["scene_refs_error"]


def test_refs_task_proceeds_when_account_is_within_concurrency_limit(monkeypatch) -> None:
    """账号名下没有其它在跑的批次时，闸门不应该误伤——账号并发准入只拦超限，
    不拦一切（CLAUDE.md「空集合不等于无需检查」的对偶面：也不能反过来变成
    "有配额系统就该拦一切"）。"""
    owner = _make_user("free")
    project_id = _make_project(owner)

    called = {"hit": False}

    async def fake_generate_refs(*_args, **_kwargs):
        called["hit"] = True
        return {"generated": [], "gate_retry_exhausted": False, "warnings": []}

    monkeypatch.setattr(refs_module, "generate_refs", fake_generate_refs)

    asyncio.run(_refs_task(project_id, None))

    assert called["hit"] is True
    conn = get_conn()
    row = conn.execute("SELECT refs_status FROM projects WHERE id=?", (project_id,)).fetchone()
    assert row["refs_status"] == "ready"


def test_refs_task_ignores_concurrency_from_a_different_account(monkeypatch) -> None:
    """账号隔离：别的账号名下有活跃批次，不应该影响本账号的准入判断——并发
    上限是账号维度的，不是全局共享的一个数字。"""
    owner_a = _make_user("free")
    owner_b = _make_user("free")
    project_a = _make_project(owner_a)
    project_b = _make_project(owner_b)
    _start_active_run(project_b, "character_references")

    called = {"hit": False}

    async def fake_generate_refs(*_args, **_kwargs):
        called["hit"] = True
        return {"generated": [], "gate_retry_exhausted": False, "warnings": []}

    monkeypatch.setattr(refs_module, "generate_refs", fake_generate_refs)

    asyncio.run(_refs_task(project_a, None))

    assert called["hit"] is True
    conn = get_conn()
    row = conn.execute("SELECT refs_status FROM projects WHERE id=?", (project_a,)).fetchone()
    assert row["refs_status"] == "ready"


def test_max_tier_allows_up_to_ten_concurrent_refs_batches(monkeypatch) -> None:
    """max 档（并发上限 10）：账号名下已有 9 个 character_references 批次在跑
    时，第 10 个仍应放行；第 11 个才被拦。档位数值直接取自 quota.TIER_TABLE，
    不在测试里重复硬编码。"""
    owner = _make_user("max")
    limit = quota.TIER_TABLE["max"].concurrency
    assert limit == 10
    project_id = _make_project(owner)
    for _ in range(limit - 1):
        sibling = _make_project(owner)
        _start_active_run(sibling, "character_references")

    called = {"hit": False}

    async def fake_generate_refs(*_args, **_kwargs):
        called["hit"] = True
        return {"generated": [], "gate_retry_exhausted": False, "warnings": []}

    monkeypatch.setattr(refs_module, "generate_refs", fake_generate_refs)

    # active=9（<10）时，第 10 个批次应该放行——这一次调用会同步跑完整个
    # _refs_task（mock 立即返回），run 随之转终态，验证过之后不再计入活跃数，
    # 所以下面单独再补一个 _start_active_run 把活跃数补回 10，才能验证第 11
    # 个批次真的会被拦（不能指望这次调用完的 run 还占着槽位）。
    asyncio.run(_refs_task(project_id, None))
    assert called["hit"] is True

    # 补回活跃数到 limit（9 个原有 sibling + 1 个新的），第 limit+1 个批次
    # （账号总共已有 limit 个活跃）必须被拦。
    tenth_sibling = _make_project(owner)
    _start_active_run(tenth_sibling, "character_references")
    overflow_project = _make_project(owner)
    monkeypatch.setattr(refs_module, "generate_refs", _never_called)
    asyncio.run(_refs_task(overflow_project, None))
    conn = get_conn()
    row = conn.execute(
        "SELECT refs_status, refs_error FROM projects WHERE id=?", (overflow_project,)
    ).fetchone()
    assert row["refs_status"] == "failed"
    assert "portrait" in row["refs_error"]
    assert "max 档上限" in row["refs_error"]
    assert "10 个" in row["refs_error"]
