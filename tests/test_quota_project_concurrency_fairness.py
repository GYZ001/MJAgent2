"""EP-04 第二阶段：公平调度——项目级并发上限（CLAUDE.md 记录的老欠账：一个
项目可以把账号/团队的全部并发槽位吃满，饿死同账号下的其它项目）。

判据挂实际在跑的行（``workflow_runs``/``jobs`` 的活跃状态），不挂内存计数器
——与既有 ``check_module_concurrency`` 同一做法（见 app/quota_project.py）。
本文件同时覆盖：单元级判断（``check_project_concurrency`` 本身）、真实事务级
准入（复用 ``_reserve_screenplay_concurrency_slot`` 的 BEGIN IMMEDIATE 占位+
判断契约）、以及真线程并发下的原子性（同一手法见
``tests/test_quota_concurrency_atomicity.py``，必须连跑 ≥10 次验证不是单次
侥幸绿）。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db, quota, quota_project
from app.db import get_conn, new_id, now
from app.domain.screenplay_ops.task_body import _reserve_screenplay_concurrency_slot
from app.domain.series_ops import queue as series_queue
from app.orchestration.engine import fingerprint


def _make_user(tier: str = "pro") -> str:
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
        "INSERT INTO projects(id, name, status, created_at, owner_user_id) VALUES(?,?,?,?,?)",
        (project_id, "P", "created", now(), owner_user_id),
    )
    conn.commit()
    return project_id


_episode_no_seq: dict[str, int] = {}


def _make_episode(project_id: str) -> str:
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


def _screenplay_kwargs(episode_id: str, tag: str) -> dict:
    return dict(
        workflow_type="screenplay", scope_type="episode", scope_id=episode_id,
        input_fingerprint=fingerprint(episode_id, tag),
        requested_by="user", trigger_type="manual",
    )


# ---------------------------------------------------------------------------
# 单元级：check_project_concurrency 本身
# ---------------------------------------------------------------------------


def test_check_project_concurrency_blocks_at_limit_and_names_the_project() -> None:
    owner = _make_user("standard")  # project_concurrency=1
    project_id = _make_project(owner)
    conn = get_conn()
    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota_project.check_project_concurrency(
            conn, owner, project_id, "video", active_count=1,
        )
    detail = exc_info.value.detail
    assert detail["gate"] == "project_concurrency"
    assert project_id in detail["message"]
    assert "video" in detail["message"]
    assert "项目级上限" in detail["message"]


def test_check_project_concurrency_allows_below_limit() -> None:
    owner = _make_user("pro")  # project_concurrency=3
    project_id = _make_project(owner)
    conn = get_conn()
    quota_project.check_project_concurrency(conn, owner, project_id, "video", active_count=2)  # 不应抛异常


def test_check_project_concurrency_never_blocks_unlimited_admin() -> None:
    conn = get_conn()
    admin_id = new_id("user")
    conn.execute(
        "INSERT INTO users(id, username, display_name, auth_provider, status, "
        "is_system_admin, must_change_password, created_at) "
        "VALUES(?,?,?,'local','active',1,0,?)",
        (admin_id, "admin", "管理员", now()),
    )
    conn.commit()
    project_id = _make_project(admin_id)
    quota_project.check_project_concurrency(conn, admin_id, project_id, "video", active_count=999)


# ---------------------------------------------------------------------------
# 事务级：project_concurrency 与账号级 concurrency 是两把独立的尺子
# ---------------------------------------------------------------------------


def test_project_cap_trips_before_account_cap_leaving_room_for_other_projects() -> None:
    """pro 档账号并发上限 6、项目级上限 3：单个项目提交到第 4 个时被项目级挡
    住（不是账号级），账号还剩 3 个名额——这 3 个名额必须留给同账号下的其它
    项目，不能被这一个项目继续吃掉。"""
    owner = _make_user("pro")
    project_a = _make_project(owner)
    project_b = _make_project(owner)
    conn = get_conn()

    for _ in range(3):
        episode = _make_episode(project_a)
        _reserve_screenplay_concurrency_slot(conn, episode, _screenplay_kwargs(episode, "a"))

    fourth_episode = _make_episode(project_a)
    with pytest.raises(quota.QuotaExceeded) as exc_info:
        _reserve_screenplay_concurrency_slot(conn, fourth_episode, _screenplay_kwargs(fourth_episode, "a4"))
    assert exc_info.value.detail["gate"] == "project_concurrency", (
        "第 4 个应该被项目级上限挡住，不是账号级（账号级上限 6，此时账号总活跃数只有 3）"
    )

    # 另一个项目此时完全没有被占用过，必须仍能拿到槽位——饿死场景验证。
    episode_b = _make_episode(project_b)
    recorder_b = _reserve_screenplay_concurrency_slot(conn, episode_b, _screenplay_kwargs(episode_b, "b"))
    assert recorder_b is not None

    active_total = conn.execute(
        "SELECT COUNT(*) AS c FROM workflow_runs WHERE workflow_type='screenplay' "
        "AND status IN ('CREATED','RUNNING')",
    ).fetchone()["c"]
    assert active_total == 4, "项目 A 3 个 + 项目 B 1 个，账号级上限 6 还有余量"


# ---------------------------------------------------------------------------
# 真线程并发：项目级上限必须在真实竞态下也只放行恰好 N 个
# ---------------------------------------------------------------------------


@pytest.fixture
def atomic_db(tmp_path, monkeypatch):
    """真实磁盘文件 DB：两个 OS 线程各自开自己的 sqlite 连接指向同一个文件，
    才能真正制造 BEGIN IMMEDIATE 互斥的场景（同一个内存态测试连接做不到）。
    与 tests/test_quota_concurrency_atomicity.py 同一套既有约定。"""
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "project-fairness.db")
    db._local.conn = None
    db.init_db()
    yield db.get_conn()


def _run_n_threads_racing(target, args_list):
    def worker(args):
        conn = db.get_conn()
        try:
            return ("ok", target(conn, *args))
        except quota.QuotaExceeded as exc:
            return ("blocked", exc)

    with ThreadPoolExecutor(max_workers=len(args_list)) as pool:
        futures = [pool.submit(worker, args) for args in args_list]
        return [f.result(timeout=10) for f in futures]


def test_project_cap_admits_exactly_n_under_real_concurrency_and_other_project_still_gets_a_slot(atomic_db) -> None:
    """standard 档：账号级并发 3、项目级并发 1。项目 A 四个线程同时抢同一个
    项目级槽位（barrier 强制几乎同时到达）：必须恰好 1 个 ok、3 个 blocked。
    与此同时项目 B（同账号、不同项目）单独提交必须成功——不受项目 A 竞态影响，
    证明项目级上限不会误伤其它项目。"""
    conn = atomic_db
    owner = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier,
               quota_period_started_at
           ) VALUES(?,?,?,'local','active',0,0,?,?,?)""",
        (owner, "standard-owner", "测试账号", now(), "standard", now()),
    )
    project_a = new_id("proj")
    project_b = new_id("proj")
    for pid in (project_a, project_b):
        conn.execute(
            "INSERT INTO projects(id, name, status, created_at, owner_user_id) VALUES(?,?,?,?,?)",
            (pid, "P", "created", now(), owner),
        )
    conn.commit()

    episodes_a = []
    for i in range(4):
        eid = new_id("ep")
        conn.execute(
            "INSERT INTO episodes(id, project_id, episode_no, title, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (eid, project_a, i + 1, f"E{i + 1}", "planned", now()),
        )
        episodes_a.append(eid)
    conn.commit()

    barrier = threading.Barrier(4, timeout=5)

    def call(conn, episode_id):
        barrier.wait()
        return _reserve_screenplay_concurrency_slot(conn, episode_id, _screenplay_kwargs(episode_id, "race"))

    results = _run_n_threads_racing(call, [(eid,) for eid in episodes_a])
    outcomes = [r[0] for r in results]
    assert outcomes.count("ok") == 1, f"项目级上限 1，应当恰好 1 个放行：{outcomes}"
    assert outcomes.count("blocked") == 3

    check = db.get_conn()
    active_a = check.execute(
        "SELECT COUNT(*) AS c FROM workflow_runs wr JOIN episodes e ON e.id=wr.scope_id "
        "WHERE wr.workflow_type='screenplay' AND e.project_id=? AND wr.status IN ('CREATED','RUNNING')",
        (project_a,),
    ).fetchone()["c"]
    assert active_a == 1

    episode_b = new_id("ep")
    check.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, status, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (episode_b, project_b, 1, "E1", "planned", now()),
    )
    check.commit()
    recorder_b = _reserve_screenplay_concurrency_slot(
        check, episode_b, _screenplay_kwargs(episode_b, "b"),
    )
    assert recorder_b is not None, "项目 A 被打满不影响项目 B 拿槽位"


# ---------------------------------------------------------------------------
# 连播队列接入同一套判定（不再自建第二套并发常量）
# ---------------------------------------------------------------------------


def test_series_queue_effective_concurrency_is_capped_by_quota_project_concurrency(monkeypatch) -> None:
    owner = _make_user("standard")  # project_concurrency=1
    project_id = _make_project(owner)
    conn = get_conn()
    monkeypatch.setattr(series_queue, "queue_concurrency", lambda: 5)
    effective = series_queue._effective_task_concurrency(conn, project_id)
    assert effective == 1, "设置台配了 5，但配额只放行 1，必须取更紧的那个"


def test_series_queue_effective_concurrency_never_exceeds_configured_value(monkeypatch) -> None:
    """反向验证：配额上限比设置台的值更松时，不会把并行数抬高过设置台配置
    （只封顶，不兜底放大）。"""
    owner = _make_user("max")  # project_concurrency=5
    project_id = _make_project(owner)
    conn = get_conn()
    monkeypatch.setattr(series_queue, "queue_concurrency", lambda: 2)
    effective = series_queue._effective_task_concurrency(conn, project_id)
    assert effective == 2


def test_series_queue_effective_concurrency_falls_back_when_no_owner(monkeypatch) -> None:
    """项目没有归属账号（legacy-shared 兼容路径）时，退回设置台原值，不拦截。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("legacy-proj", "P", "created", now()),
    )
    conn.commit()
    monkeypatch.setattr(series_queue, "queue_concurrency", lambda: 4)
    effective = series_queue._effective_task_concurrency(conn, "legacy-proj")
    assert effective == 4
