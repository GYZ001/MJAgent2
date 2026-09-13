"""公平调度排序键接入真实调度器（EP-04 §6 / EP-05 §10.5 第二条）。

三层验证：
1. 纯排序数学（不碰数据库）——``_score_only_sort``/``_fair_sort`` 的加权和公式，
   直接验证「团队 B 体量小但比例充裕，不会被团队 A 的体量压没」与「团队 A
   比例长期为 0，只要等够上限也不会被永久判死刑」这两条防饿死承诺。
2. 真实磁盘 db 上的比例计算/刷新节流——与
   ``tests/test_quota_project_concurrency_fairness.py::atomic_db`` 同一套既有
   约定（``:memory:`` 连接互相看不见，``fair_ordering`` 用独立连接直接读
   ``db.DB_PATH``，必须是真实文件）。
3. 端到端接入 ``app.worker._dispatch_due_jobs_stage_aware``——证明真的接进了
   热路径，不是只在模块内部自证。
"""
from __future__ import annotations

import time

import pytest

from app import db, worker
from app.db import new_id, now, set_setting
from app.media_exec import dispatch
from app.observability import metrics_registry
from app.orgs import store as orgs_store
from app.quota_policy import allocation, fair_ordering, plans
from tests.conftest import patch_worker_everywhere

# ---------------------------------------------------------------------------
# 共享 helper
# ---------------------------------------------------------------------------


def _row(job_id: str, project_id: str | None, created_at: float) -> dict:
    return {"id": job_id, "project_id": project_id, "created_at": created_at}


@pytest.fixture
def fair_db(tmp_path, monkeypatch):
    """真实磁盘文件 DB：fair_ordering 用独立连接（db._open_connection）直接
    读 db.DB_PATH，跟 test_quota_project_concurrency_fairness.py::atomic_db
    同一套既有约定——``:memory:`` 连接互相看不见，必须是真实文件才能让
    fair_ordering 自己开的连接看到测试里写的数据。"""
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fair-ordering.db")
    db._local.conn = None
    db.init_db()
    fair_ordering.reset_for_tests()
    yield db.get_conn()
    fair_ordering.reset_for_tests()


def _make_org(conn) -> str:
    return orgs_store.create_org(conn, name="Org", created_by="tester")


def _make_team(conn, org_id: str, name: str = "Team") -> str:
    return orgs_store.create_team(conn, org_id=org_id, name=name, description=None, created_by="tester")


def _make_user(conn) -> str:
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users(id, username, display_name, auth_provider, status, "
        "is_system_admin, must_change_password, created_at) VALUES(?,?,?,'local','active',0,0,?)",
        (user_id, f"u-{user_id}", "T", now()),
    )
    return user_id


def _add_member(conn, team_id: str, user_id: str) -> None:
    orgs_store.add_team_member(conn, team_id=team_id, user_id=user_id, role_id="member", created_by="tester")


def _make_project(conn, project_id: str, owner_user_id: str) -> None:
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at,owner_user_id) VALUES(?,?,?,?,?)",
        (project_id, "P", "created", now(), owner_user_id),
    )


def _set_team_video_limit(conn, team_id: str, org_id: str, limit: float) -> None:
    plan_id = plans.create_plan(
        conn, org_id=org_id, key=f"plan-{team_id}", name="P",
        limits={"video_seconds": limit}, period_days=30, created_by="tester",
    )
    allocation.set_allocation(
        conn, scope_type="team", scope_id=team_id, plan_id=plan_id,
        overrides=None, expires_at=None, created_by="tester",
    )


def _charge_video_seconds(conn, user_id: str, amount: float, *, created_at: float | None = None) -> None:
    conn.execute(
        "INSERT INTO quota_ledger(user_id,resource,period_index,attempt_key,reason,delta,created_at) "
        "VALUES(?,'video_seconds',0,?,'test',?,?)",
        (user_id, new_id("atk"), amount, created_at if created_at is not None else now()),
    )


def _seed_shot(conn, shot_id: str, shot_no: int, episode_id: str) -> None:
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,characters,dialogues) VALUES(?,?,?,5,'[]','[]')",
        (shot_id, episode_id, shot_no),
    )


def _seed_reference_job(
    conn, *, job_id: str, shot_id: str, version_id: str, episode_id: str, project_id: str, created_at: float,
) -> None:
    """未就绪的参考图 job：refs_ready=False、无 after_shot_id——必然落进
    ``reference_normal`` 车道（与 job_scheduler_score 的 first_pass 基线分数
    绑定，见 test_dispatch_stage_aware 系列用例的推导注释）。"""
    import json

    meta = {
        "reference_images": [], "reference_generation_complete": False,
        "video_input_manifest_frozen": False, "auto_retake_count": 0,
    }
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at,image_inputs) "
        "VALUES(?,?,1,'p',?,'queued',?,?)",
        (version_id, shot_id, version_id, created_at, json.dumps(meta)),
    )
    conn.execute(
        "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at) "
        "VALUES(?,'video',?,?,?,?,'queued',?,?)",
        (job_id, shot_id, version_id, episode_id, project_id, created_at, created_at),
    )


# ---------------------------------------------------------------------------
# 1. 纯排序数学（不碰数据库）
# ---------------------------------------------------------------------------


def test_score_only_sort_matches_pre_change_behavior() -> None:
    lane = [(5.0, _row("a", "p1", 0.0)), (10.0, _row("b", "p1", 0.0)), (1.0, _row("c", "p1", 0.0))]
    result = fair_ordering._score_only_sort(lane)
    assert [row["id"] for _, row in result] == ["b", "a", "c"]


def test_fair_sort_favors_higher_team_ratio_at_equal_score_and_age() -> None:
    fair_ordering.reset_for_tests()
    key = fair_ordering._db_key()
    fair_ordering._PROJECT_TEAM_CACHE[key] = {"p-a": ("team-a",), "p-b": ("team-b",)}
    fair_ordering._TEAM_RATIO_CACHE[key] = {"team-a": 0.0, "team-b": 1.0}
    stamp = 1_000_000.0
    lane = [(1000.0, _row("a1", "p-a", stamp)), (1000.0, _row("b1", "p-b", stamp))]
    result = fair_ordering._fair_sort(lane, stamp)
    assert [row["id"] for _, row in result] == ["b1", "a1"]


def test_fair_sort_prevents_low_volume_team_starvation_within_one_round() -> None:
    """饿死场景核心断言：团队 A 比例耗尽但持续用同分任务灌队列，团队 B 只有
    一个任务、比例充裕——必须在本轮（N=1，单次 _fair_sort 调用）就排到最前，
    不能被 A 的体量压没。"""
    fair_ordering.reset_for_tests()
    key = fair_ordering._db_key()
    fair_ordering._PROJECT_TEAM_CACHE[key] = {"p-a": ("team-a",), "p-b": ("team-b",)}
    fair_ordering._TEAM_RATIO_CACHE[key] = {"team-a": 0.0, "team-b": 1.0}
    stamp = 1_000_000.0
    lane = [(1000.0, _row(f"a{i}", "p-a", stamp)) for i in range(10)]
    lane.append((1000.0, _row("b1", "p-b", stamp)))
    result = fair_ordering._fair_sort(lane, stamp)
    ordered_ids = [row["id"] for _, row in result]
    assert ordered_ids[0] == "b1"
    assert set(ordered_ids) == {f"a{i}" for i in range(10)} | {"b1"}, "必须是纯排列，不丢不增"


def test_fair_sort_lets_aged_low_ratio_job_eventually_outrank_fresh_rival() -> None:
    """反向防饿死断言：团队 A 比例长期为 0，但它排队最久的任务一旦等够
    ``_FAIRNESS_AGE_CAP_MINUTES`` 上限（240 分钟），必须反超一个比例满但全新
    提交（wait≈0）的对手——否则"比例"这一个维度就会把 A 判永久死刑，等待
    时长项形同虚设。校准依据：AGE_WEIGHT_PER_MIN(2.5)*AGE_CAP(240)=600 严格
    大于 RATIO_WEIGHT(500)，见 app/quota_policy/fair_ordering.py 模块文档。"""
    fair_ordering.reset_for_tests()
    key = fair_ordering._db_key()
    fair_ordering._PROJECT_TEAM_CACHE[key] = {"p-a": ("team-a",), "p-b": ("team-b",)}
    fair_ordering._TEAM_RATIO_CACHE[key] = {"team-a": 0.0, "team-b": 1.0}
    stamp = 1_000_000.0
    old_created_at = stamp - fair_ordering._FAIRNESS_AGE_CAP_MINUTES * 60.0
    lane = [(1000.0, _row("a-old", "p-a", old_created_at)), (1000.0, _row("b-fresh", "p-b", stamp))]
    result = fair_ordering._fair_sort(lane, stamp)
    assert [row["id"] for _, row in result] == ["a-old", "b-fresh"]


def test_team_ratio_for_project_defaults_to_full_headroom_when_unknown() -> None:
    fair_ordering.reset_for_tests()
    assert fair_ordering._team_ratio_for_project(None) == 1.0
    assert fair_ordering._team_ratio_for_project("never-seen-project") == 1.0


def test_reorder_lanes_is_a_pure_permutation_in_both_healthy_and_degraded_states() -> None:
    """规则 3：改动前后任意一轮被调度的候选集合相同，只有顺序不同——本函数
    不得过滤/新增/丢弃任何候选，无论走健康态还是退化态分支。"""
    fair_ordering.reset_for_tests()
    key = fair_ordering._db_key()
    stamp = 1_000_000.0
    video_ready = [(3.0, _row("vr1", "p1", stamp))]
    reference_critical = [(2.0, _row("rc1", "p2", stamp)), (5.0, _row("rc2", "p1", stamp))]
    reference_normal = [(1.0, _row("rn1", "p3", stamp))]
    retake_jobs = [(-1.0, _row("rt1", "p1", stamp))]

    def ids(lanes):
        return {row["id"] for lane in lanes for _, row in lane}

    before = ids((video_ready, reference_critical, reference_normal, retake_jobs))

    # 退化态：跳过真实刷新（避免连真实 db.DB_PATH），_LAST_REFRESH_OK_AT 缺失
    fair_ordering._LAST_REFRESH_ATTEMPT_AT[key] = time.time()
    degraded = fair_ordering.reorder_lanes(video_ready, reference_critical, reference_normal, retake_jobs, stamp=stamp)
    assert ids(degraded) == before

    # 健康态：手工灌一份健康缓存，同样跳过真实刷新
    fair_ordering._TEAM_RATIO_CACHE[key] = {}
    fair_ordering._PROJECT_TEAM_CACHE[key] = {}
    fair_ordering._LAST_REFRESH_OK_AT[key] = time.time()
    fair_ordering._LAST_REFRESH_ATTEMPT_AT[key] = time.time()
    healthy = fair_ordering.reorder_lanes(video_ready, reference_critical, reference_normal, retake_jobs, stamp=stamp)
    assert ids(healthy) == before


def test_reorder_lanes_degrades_when_cache_missing_and_records_metric() -> None:
    fair_ordering.reset_for_tests()
    metrics_registry.reset_all()
    key = fair_ordering._db_key()
    fair_ordering._LAST_REFRESH_ATTEMPT_AT[key] = time.time()  # 跳过真实刷新
    stamp = 1_000_000.0
    lane = [(1000.0, _row("a", "p-a", stamp)), (500.0, _row("b", "p-a", stamp))]
    result = fair_ordering.reorder_lanes(lane, [], [], [], stamp=stamp)
    assert [row["id"] for _, row in result[0]] == ["a", "b"]
    text = metrics_registry.render_prometheus_text()
    assert 'manju_fair_ordering_degraded_total{reason="cache_stale_or_missing"}' in text


def test_reorder_lanes_degrades_when_cache_stale_and_records_metric() -> None:
    fair_ordering.reset_for_tests()
    metrics_registry.reset_all()
    key = fair_ordering._db_key()
    now_ts = time.time()
    fair_ordering._LAST_REFRESH_OK_AT[key] = now_ts - (fair_ordering._STALE_AFTER_S + 5.0)
    fair_ordering._LAST_REFRESH_ATTEMPT_AT[key] = now_ts
    fair_ordering._TEAM_RATIO_CACHE[key] = {"team-a": 0.0}
    fair_ordering._PROJECT_TEAM_CACHE[key] = {"p-a": ("team-a",)}
    stamp = 1_000_000.0
    lane = [(1000.0, _row("a", "p-a", stamp)), (500.0, _row("b", "p-a", stamp))]
    result = fair_ordering.reorder_lanes(lane, [], [], [], stamp=stamp)
    assert [row["id"] for _, row in result[0]] == ["a", "b"], "过期缓存必须退化为纯 -score 排序"
    text = metrics_registry.render_prometheus_text()
    assert 'manju_fair_ordering_degraded_total{reason="cache_stale_or_missing"}' in text


# ---------------------------------------------------------------------------
# 2. 真实磁盘 db：比例计算与刷新节流
# ---------------------------------------------------------------------------


def test_load_team_ratios_matches_hand_computed_expected_value(fair_db) -> None:
    conn = fair_db
    org_id = _make_org(conn)
    team_id = _make_team(conn, org_id)
    user_id = _make_user(conn)
    _add_member(conn, team_id, user_id)
    conn.commit()
    _set_team_video_limit(conn, team_id, org_id, 1000.0)
    _charge_video_seconds(conn, user_id, 300.0)
    _charge_video_seconds(conn, user_id, 100.0)
    conn.commit()

    ratios = fair_ordering._load_team_ratios(conn)
    # 独立观察点：手工复算期望值，不复用被测函数自己的任何中间结果
    expected = 1.0 - (300.0 + 100.0) / 1000.0
    assert ratios[team_id] == pytest.approx(expected)
    assert ratios[team_id] == pytest.approx(0.6)


def test_team_effective_video_limit_falls_back_to_org_when_team_unconfigured(fair_db) -> None:
    conn = fair_db
    org_id = _make_org(conn)
    team_id = _make_team(conn, org_id)
    conn.commit()
    plan_id = plans.create_plan(
        conn, org_id=org_id, key="org-plan", name="P",
        limits={"video_seconds": 2000.0}, period_days=30, created_by="tester",
    )
    allocation.set_allocation(
        conn, scope_type="org", scope_id=org_id, plan_id=plan_id,
        overrides=None, expires_at=None, created_by="tester",
    )
    conn.commit()
    assert fair_ordering._team_effective_video_limit(conn, team_id) == 2000.0


def test_team_ratio_uses_rolling_window_not_lifetime_sum(fair_db) -> None:
    conn = fair_db
    org_id = _make_org(conn)
    team_id = _make_team(conn, org_id)
    user_id = _make_user(conn)
    _add_member(conn, team_id, user_id)
    _set_team_video_limit(conn, team_id, org_id, 100.0)
    ancient = now() - fair_ordering._ROLLING_WINDOW_S - 3600.0
    _charge_video_seconds(conn, user_id, 100000.0, created_at=ancient)  # 窗口外的历史巨量消耗
    conn.commit()

    ratios = fair_ordering._load_team_ratios(conn)
    assert ratios[team_id] == pytest.approx(1.0), (
        "窗口外的历史用量不该把比例拖到 0——否则任何存活够久的团队最终都会"
        "永久判死刑，公平排序会随时间单调失效"
    )


def test_refresh_if_due_is_throttled_and_force_bypasses_it(fair_db, monkeypatch) -> None:
    conn = fair_db
    org_id = _make_org(conn)
    _make_team(conn, org_id)
    conn.commit()

    calls: list[int] = []
    original = fair_ordering._load_team_ratios

    def spy(c):
        calls.append(1)
        return original(c)

    monkeypatch.setattr(fair_ordering, "_load_team_ratios", spy)
    fair_ordering._refresh_if_due(force=True)
    assert len(calls) == 1
    fair_ordering._refresh_if_due()  # 未到刷新间隔，节流跳过
    assert len(calls) == 1
    fair_ordering._refresh_if_due(force=True)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 3. 端到端接入 app.worker._dispatch_due_jobs_stage_aware
# ---------------------------------------------------------------------------


def test_dispatch_stage_aware_promotes_low_quota_team_and_preserves_candidate_set(fair_db, monkeypatch) -> None:
    conn = fair_db
    stamp = now()

    org_id = _make_org(conn)
    team_a = _make_team(conn, org_id, name="TeamA")
    team_b = _make_team(conn, org_id, name="TeamB")
    owner_a = _make_user(conn)
    owner_b = _make_user(conn)
    _add_member(conn, team_a, owner_a)
    _add_member(conn, team_b, owner_b)
    conn.commit()

    _set_team_video_limit(conn, team_a, org_id, 100.0)
    _charge_video_seconds(conn, owner_a, 100.0)  # 团队 A 配额已耗尽：ratio=0
    conn.commit()
    # 团队 B 不配置任何 allocation：退到组织（同样未配置）→ 不限，ratio=1.0

    project_a, project_b = "proj-a", "proj-b"
    _make_project(conn, project_a, owner_a)
    _make_project(conn, project_b, owner_b)
    episode_a, episode_b = "ep-a", "ep-b"
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES(?,?,1,'generating',?)",
        (episode_a, project_a, stamp),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES(?,?,1,'generating',?)",
        (episode_b, project_b, stamp),
    )
    conn.commit()

    for i in range(10):
        shot_id = f"shot-a{i}"
        _seed_shot(conn, shot_id, i + 1, episode_a)
        _seed_reference_job(
            conn, job_id=f"job-a{i}", shot_id=shot_id, version_id=f"ver-a{i}",
            episode_id=episode_a, project_id=project_a, created_at=stamp,
        )
    shot_b = "shot-b0"
    _seed_shot(conn, shot_b, 1, episode_b)
    _seed_reference_job(
        conn, job_id="job-b0", shot_id=shot_b, version_id="ver-b0",
        episode_id=episode_b, project_id=project_b, created_at=stamp,
    )
    conn.commit()

    set_setting("media_scheduler_policy", "stage_aware")
    set_setting("video_ready_high_watermark", "50")
    set_setting("video_ready_low_watermark", "2")
    set_setting("reference_shot_cohort_limit", "50")

    patch_worker_everywhere(monkeypatch, "_worker_target", 30)
    patch_worker_everywhere(monkeypatch, "_reference_worker_target", 30)
    patch_worker_everywhere(monkeypatch, "_video_ready_worker_target", 5)
    patch_worker_everywhere(monkeypatch, "_poll_worker_target", 1)

    main_queue_ids: list[str] = []
    monkeypatch.setattr(worker._queue, "put_nowait", main_queue_ids.append)
    monkeypatch.setattr(worker._video_ready_queue, "put_nowait", lambda *a, **k: None)
    monkeypatch.setattr(worker._poll_queue, "put_nowait", lambda *a, **k: None)

    reorder_calls: list[int] = []
    real_reorder = fair_ordering.reorder_lanes

    def spy_reorder(*args, **kwargs):
        reorder_calls.append(1)
        return real_reorder(*args, **kwargs)

    monkeypatch.setattr(fair_ordering, "reorder_lanes", spy_reorder)
    monkeypatch.setattr(dispatch, "fair_ordering", fair_ordering)  # 确保拿到同一个（未被其它测试污染的）模块对象

    result = worker._dispatch_due_jobs_stage_aware()

    assert len(reorder_calls) == 1, "候选选取处必须恰好调用一次 fair_ordering.reorder_lanes"
    assert result["due"] == 11
    expected_ids = {f"job-a{i}" for i in range(10)} | {"job-b0"}
    assert set(main_queue_ids) == expected_ids, "被调度集合必须与派发前完全一致，只是顺序变了"
    assert main_queue_ids[0] == "job-b0", (
        "团队 B 比例充裕、只有一个任务；团队 A 比例耗尽却用 10 个同分任务灌队列，"
        "公平排序必须让 B 在本轮（N=1）排到最前，不被 A 的体量压没"
    )
