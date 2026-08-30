"""并发准入改为严格同事务后的红绿证明。

``app.quota.check_module_concurrency`` 的 docstring 明确写着 ``active_count``
是"调用方查到的、不含本次即将新建的这一个的当前活跃数"——计数与创建
workflow_run 分成两步、不共享事务，是教科书式的 check-then-act TOCTOU：两个
真正并发的请求可以都读到"还没到上限"、都通过、都各自建一行，把档位上限
撑破。

复现证据（未收进本文件，因为它验证的是"修复前"的行为，留在这里会和修复后的
断言矛盾；证据已经贴进任务报告）：用两个真实 OS 线程（各自独立的 sqlite
连接，指向同一个磁盘文件）在 count 与 create 之间插一个 ``threading.Barrier``，
强制两边都读到 0 个已存在的 run 之后才各自建行——free 档（并发上限 1）最终
两个都被判定"未超限"，出现 2 个同时活跃的 workflow_run。

修复：count + check_module_concurrency + WorkflowRecorder.create() 三步收进
同一个 ``BEGIN IMMEDIATE`` 事务（照抄 ``media_scheduler.reserve_budget`` 的
``owns_transaction`` 惯例），四个模块各自在自己的创建关口收口：
- ``bible_ops.refs_generation._reserve_refs_recorder``（portrait）
- ``bible_ops.scene_bible_prep._reserve_scene_refs_recorder``（scene_ref）
- ``screenplay_ops.task_body._reserve_screenplay_concurrency_slot``
- ``storyboard_ops.task_run._reserve_storyboard_concurrency_slot``

本文件用同样的真线程 + Barrier 手法验证：修复后，两个真正并发的请求里，
先拿到 ``BEGIN IMMEDIATE`` 写锁的那个看到 0 个活跃 run 并顺利建行，后拿到锁
的那个此时已经能读到前者刚提交的那一行，从而被正确拦下——不会再出现两个都
判"未超限"的窗口。另外覆盖：占位失败后释放（下一次请求不会被永久锁死）、
四个模块各自独立计数（不互相挤占）。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db, quota
from app.db import get_conn, new_id, now
from app.domain.bible_ops.refs_generation import _reserve_refs_recorder
from app.domain.bible_ops.scene_bible_prep import _reserve_scene_refs_recorder
from app.domain.screenplay_ops.task_body import _reserve_screenplay_concurrency_slot
from app.domain.storyboard_ops.task_run import _reserve_storyboard_concurrency_slot
from app.orchestration.engine import fingerprint


@pytest.fixture
def atomic_db(tmp_path, monkeypatch):
    """真实磁盘文件 DB：两个 OS 线程各自开自己的 sqlite 连接指向同一个文件，
    才能真正制造 BEGIN IMMEDIATE 互斥的场景（同一个内存态测试连接做不到）。
    与 tests/test_completion_grant_atomicity.py 同一套既有约定。"""
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "quota-atomicity.db")
    db._local.conn = None
    db.init_db()
    yield db.get_conn()


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


_episode_no_seq: dict[str, int] = {}


def _make_episode(owner_user_id: str, project_id: str) -> str:
    conn = get_conn()
    episode_id = new_id("ep")
    episode_no = _episode_no_seq.get(project_id, 0) + 1
    _episode_no_seq[project_id] = episode_no
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, status, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (episode_id, project_id, episode_no, f"E{episode_no}", "planned", now()),
    )
    conn.commit()
    return episode_id


def _active_run_count(conn, workflow_type: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM workflow_runs "
        "WHERE workflow_type=? AND status IN ('CREATED','RUNNING')",
        (workflow_type,),
    ).fetchone()["c"]


def _run_two_threads_racing(target, args_a, args_b):
    """两个真实线程各自打开自己的 sqlite 连接（db.get_conn() 按线程缓存），
    同时调用 ``target``；用 ThreadPoolExecutor 收集各自的结果/异常。"""

    def worker(args):
        # 每个线程需要拿到自己的连接：db._local 是 threading.local，新线程里
        # get_conn() 会自己开一条新连接指向同一个磁盘文件。
        conn = db.get_conn()
        try:
            return ("ok", target(conn, *args))
        except quota.QuotaExceeded as exc:
            return ("blocked", exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_a = pool.submit(worker, args_a)
        f_b = pool.submit(worker, args_b)
        return f_a.result(timeout=10), f_b.result(timeout=10)


# ---------------------------------------------------------------------------
# portrait（character_references）
# ---------------------------------------------------------------------------

def test_refs_reservation_admits_exactly_one_under_real_concurrency(atomic_db):
    owner = _make_user("free")  # 并发上限 1
    project_a = _make_project(owner)
    project_b = _make_project(owner)
    barrier = threading.Barrier(2, timeout=5)

    def call(conn, project_id):
        barrier.wait()  # 强制两个线程几乎同时到达 BEGIN IMMEDIATE 门口
        return _reserve_refs_recorder(
            conn, project_id, None, None,
            resume=False, fresh_after=None, parent_run_id=None,
            requested_by="user", trigger_type="manual",
        )

    result_a, result_b = _run_two_threads_racing(call, (project_a,), (project_b,))
    outcomes = [result_a[0], result_b[0]]
    assert outcomes.count("ok") == 1, f"应当恰好 1 个被放行，实际：{outcomes}"
    assert outcomes.count("blocked") == 1

    check = db.get_conn()
    assert _active_run_count(check, "character_references") == 1


# ---------------------------------------------------------------------------
# scene_ref（scene_references）
# ---------------------------------------------------------------------------

def test_scene_refs_reservation_admits_exactly_one_under_real_concurrency(atomic_db):
    owner = _make_user("free")
    project_a = _make_project(owner)
    project_b = _make_project(owner)
    barrier = threading.Barrier(2, timeout=5)

    def call(conn, project_id):
        barrier.wait()
        return _reserve_scene_refs_recorder(
            conn, project_id, None,
            requested_by="user", trigger_type="manual", parent_run_id=None,
        )

    result_a, result_b = _run_two_threads_racing(call, (project_a,), (project_b,))
    outcomes = [result_a[0], result_b[0]]
    assert outcomes.count("ok") == 1, f"应当恰好 1 个被放行，实际：{outcomes}"
    assert outcomes.count("blocked") == 1

    check = db.get_conn()
    assert _active_run_count(check, "scene_references") == 1


# ---------------------------------------------------------------------------
# screenplay
# ---------------------------------------------------------------------------

def _screenplay_kwargs(episode_id: str, tag: str) -> dict:
    return dict(
        workflow_type="screenplay", scope_type="episode", scope_id=episode_id,
        input_fingerprint=fingerprint(episode_id, tag),
        requested_by="user", trigger_type="manual",
    )


def test_screenplay_reservation_admits_exactly_one_under_real_concurrency(atomic_db):
    owner = _make_user("free")
    project = _make_project(owner)
    episode_a = _make_episode(owner, project)
    episode_b = _make_episode(owner, project)
    barrier = threading.Barrier(2, timeout=5)

    def call(conn, episode_id):
        barrier.wait()
        return _reserve_screenplay_concurrency_slot(
            conn, episode_id, _screenplay_kwargs(episode_id, "race"),
        )

    result_a, result_b = _run_two_threads_racing(call, (episode_a,), (episode_b,))
    outcomes = [result_a[0], result_b[0]]
    assert outcomes.count("ok") == 1, f"应当恰好 1 个被放行，实际：{outcomes}"
    assert outcomes.count("blocked") == 1

    check = db.get_conn()
    assert _active_run_count(check, "screenplay") == 1


# ---------------------------------------------------------------------------
# storyboard
# ---------------------------------------------------------------------------

def _storyboard_kwargs(episode_id: str, tag: str) -> dict:
    return dict(
        workflow_type="storyboard", scope_type="episode", scope_id=episode_id,
        input_fingerprint=fingerprint(episode_id, tag),
        requested_by="user", trigger_type="manual",
    )


def test_storyboard_reservation_admits_exactly_one_under_real_concurrency(atomic_db):
    owner = _make_user("free")
    project = _make_project(owner)
    episode_a = _make_episode(owner, project)
    episode_b = _make_episode(owner, project)
    barrier = threading.Barrier(2, timeout=5)

    def call(conn, episode_id):
        barrier.wait()
        return _reserve_storyboard_concurrency_slot(
            conn, episode_id, _storyboard_kwargs(episode_id, "race"),
        )

    result_a, result_b = _run_two_threads_racing(call, (episode_a,), (episode_b,))
    outcomes = [result_a[0], result_b[0]]
    assert outcomes.count("ok") == 1, f"应当恰好 1 个被放行，实际：{outcomes}"
    assert outcomes.count("blocked") == 1

    check = db.get_conn()
    assert _active_run_count(check, "storyboard") == 1


# ---------------------------------------------------------------------------
# 释放路径：占位失败/任务结束后，账号不会被永久锁死
# ---------------------------------------------------------------------------

def test_released_slot_allows_immediate_retry_after_cancel(atomic_db):
    """第一个 run 被 cancel（模拟任务失败/中止）之后，同账号立刻能再申请到——
    释放路径必须可靠，否则一次失败会把账号永久锁死在"并发已满"上。"""
    conn = get_conn()
    owner = _make_user("free")
    project = _make_project(owner)
    episode = _make_episode(owner, project)

    first = _reserve_screenplay_concurrency_slot(
        conn, episode, _screenplay_kwargs(episode, "first"),
    )
    with pytest.raises(quota.QuotaExceeded):
        _reserve_screenplay_concurrency_slot(
            conn, episode, _screenplay_kwargs(episode, "second"),
        )

    first.cancel("模拟任务失败", conn=None)

    third = _reserve_screenplay_concurrency_slot(
        conn, episode, _screenplay_kwargs(episode, "third"),
    )
    assert third.run_id != first.run_id


def test_failed_reservation_rolls_back_and_does_not_leak_a_row(atomic_db):
    """被拦下的那次尝试必须整体回滚——不会在 workflow_runs 里留下一行"已创建
    但从未被承认"的幽灵占位（CLAUDE.md：回滚必须是异常处理器第一条语句）。"""
    conn = get_conn()
    owner = _make_user("free")
    project = _make_project(owner)
    episode_a = _make_episode(owner, project)
    episode_b = _make_episode(owner, project)

    _reserve_screenplay_concurrency_slot(
        conn, episode_a, _screenplay_kwargs(episode_a, "a"),
    )
    before = conn.execute(
        "SELECT COUNT(*) AS c FROM workflow_runs WHERE workflow_type='screenplay'"
    ).fetchone()["c"]
    with pytest.raises(quota.QuotaExceeded):
        _reserve_screenplay_concurrency_slot(
            conn, episode_b, _screenplay_kwargs(episode_b, "b"),
        )
    after = conn.execute(
        "SELECT COUNT(*) AS c FROM workflow_runs WHERE workflow_type='screenplay'"
    ).fetchone()["c"]
    assert after == before
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# 模块独立计数：screenplay 与 storyboard 互不挤占
# ---------------------------------------------------------------------------

def test_screenplay_and_storyboard_do_not_share_the_same_pool(atomic_db):
    conn = get_conn()
    owner = _make_user("free")  # 并发上限 1（每个模块各自 1）
    project = _make_project(owner)
    episode = _make_episode(owner, project)

    screenplay_run = _reserve_screenplay_concurrency_slot(
        conn, episode, _screenplay_kwargs(episode, "sp"),
    )
    # storyboard 是独立的 module token，不因 screenplay 已经用满账号的
    # screenplay 槽位而被误伤。
    storyboard_run = _reserve_storyboard_concurrency_slot(
        conn, episode, _storyboard_kwargs(episode, "sb"),
    )
    assert screenplay_run.run_id != storyboard_run.run_id

    with pytest.raises(quota.QuotaExceeded) as exc_info:
        _reserve_screenplay_concurrency_slot(
            conn, episode, _screenplay_kwargs(episode, "sp2"),
        )
    assert exc_info.value.detail["gate"] == "concurrency"
