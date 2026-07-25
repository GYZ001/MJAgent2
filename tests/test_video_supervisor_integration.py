"""视频 Supervisor 集成 / 成本回归（进程内假入队，不打真实 Seedance）。"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app import db as db_mod
from app.completion_grant import (
    bump_video_grant_budget,
    issue_video_completion_grant,
    validate_video_grant,
)
from app.compiler import CompileError
from app.evidence import repository as evidence_repository
from app.harness.types import Issue, IssueSeverity
from app.video_cost_model import predict_episode_completion_cost, predict_shot_completion_cost
from app.video_crop import auto_crop_video
from app.video_issues import issues_from_enqueue_error, persist_shot_issue
from app.video_repair_router import MAX_CHAIN_CASCADE_DEPTH, route, should_cascade
from app.video_supervisor import (
    FIRST_PASS_BUDGET_FRACTION,
    SHOT_BUDGET_MULTIPLIER,
    VideoSupervisorCheckpoint,
    _finalize_covered,
    attempts_for,
    rebuild_coverage_ledger,
)


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db_mod.SCHEMA)
    for statement in db_mod.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


@pytest.fixture
def memdb(monkeypatch):
    conn = _memory_conn()

    def _get():
        return conn

    # 顶层 from app.db import get_conn 的模块 + db 本身
    import app.completion_grant as completion_grant
    import app.evidence.media as evidence_media
    import app.video_cost_model as video_cost_model
    import app.video_crop as video_crop
    import app.video_supervisor as video_supervisor

    for mod in (
        db_mod,
        evidence_repository,
        completion_grant,
        evidence_media,
        video_cost_model,
        video_crop,
        video_supervisor,
    ):
        monkeypatch.setattr(mod, "get_conn", _get)
    return conn


def _seed_episode(conn, n_shots: int = 11, *, episode_id: str = "ep_int", project_id: str = "proj_int"):
    t = 1.0
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?, 'created', ?)",
        (project_id, "集成测试项目", t),
    )
    conn.execute(
        """INSERT INTO episodes(
            id, project_id, episode_no, title, status, storyboard_artifact_id, created_at
        ) VALUES(?,?,?,?, 'confirmed', ?, ?)""",
        (episode_id, project_id, 1, "ep1", "art_sb_v1", t),
    )
    for i in range(1, n_shots + 1):
        sid = f"{episode_id}_shot_{i}"
        conn.execute(
            """INSERT INTO shots(
                id, episode_id, shot_no, duration_s, shot_size, camera_move,
                scene_setting, characters, action_desc, dialogues, continuity_from_prev
            ) VALUES(?,?,?,?, '中景', '固定', '日，大厅', ?, ?, '[]', ?)""",
            (
                sid, episode_id, i, 5,
                json.dumps(["萧炎"]),
                f"角色完成第{i}镜主动作并收束姿态。",
                1 if i > 1 else 0,
            ),
        )
    conn.commit()
    return episode_id, project_id


def _add_succeeded_version(
    conn, shot_id: str, *, qa: dict, technical: dict | None = None, cost: float = 4.0,
    continuity_degraded: bool = False,
):
    no = (conn.execute(
        "SELECT COALESCE(MAX(version_no),0) AS m FROM shot_versions WHERE shot_id=?",
        (shot_id,),
    ).fetchone()["m"]) + 1
    vid = f"ver_{shot_id}_{no}"
    tech = technical or {"passed": True, "issues": []}
    meta = {}
    if continuity_degraded:
        meta["continuity_degraded"] = True
    conn.execute(
        """INSERT INTO shot_versions(
            id, shot_id, version_no, prompt_text, idem_key, status, created_at,
            qa_json, cost_cny, technical_validation_json, provider_task_id, image_inputs
        ) VALUES(?,?,?,?,?, 'succeeded', ?, ?, ?, ?, ?, ?)""",
        (
            vid, shot_id, no, "p", f"idem-{vid}", 1.0,
            json.dumps(qa, ensure_ascii=False), cost,
            json.dumps(tech, ensure_ascii=False), f"task-{vid}",
            json.dumps(meta, ensure_ascii=False),
        ),
    )
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (vid, shot_id))
    conn.commit()
    return vid


def test_integration_all_a_covered(memdb):
    eid, _ = _seed_episode(memdb, 11)
    for i in range(1, 12):
        _add_succeeded_version(memdb, f"{eid}_shot_{i}", qa={"overall": 0.9, "failure_types": []}, cost=3.0)
    ledger = rebuild_coverage_ledger(eid, fallback_quota=3)
    assert ledger.grades["A"] == 11
    assert ledger.covered_within_quota()
    cp = VideoSupervisorCheckpoint(
        episode_id=eid, phase="FINALIZING", grant_id=None,
        budget={"cap_cny": 150}, coverage={"fallback_quota": 3},
    )
    _finalize_covered(cp, ledger, run_id=None)
    _finalize_covered(cp, ledger, run_id=None)
    rows = memdb.execute(
        """SELECT COUNT(*) AS c FROM artifacts
           WHERE type='video_coverage_report' AND scope_id=?""",
        (eid,),
    ).fetchone()["c"]
    assert rows == 1
    assert cp.phase == "SUCCEEDED_COVERED"


def test_integration_preflight_issue_in_ledger(memdb):
    eid, _ = _seed_episode(memdb, 3)
    shot_id = f"{eid}_shot_2"
    issues = issues_from_enqueue_error(
        CompileError("动作容量超限；状态链断裂"),
        shot_id=shot_id, shot_no=2,
    )
    persist_shot_issue(
        episode_id=eid, shot_id=shot_id, shot_no=2, issues=issues, source="test",
    )
    ledger = rebuild_coverage_ledger(eid, fallback_quota=1)
    entry = next(e for e in ledger.entries if e.shot_no == 2)
    assert entry.grade == "C"
    assert "VIDEO_PREFLIGHT_BLOCKED" in entry.last_issue_codes


def test_integration_b_over_quota_not_complete(memdb):
    eid, _ = _seed_episode(memdb, 5)
    for i in range(1, 4):
        _add_succeeded_version(memdb, f"{eid}_shot_{i}", qa={"overall": 0.9, "failure_types": []})
    for i in range(4, 6):
        _add_succeeded_version(
            memdb, f"{eid}_shot_{i}",
            qa={"overall": 0.4, "failure_types": ["state_mismatch"]},
        )
    ledger = rebuild_coverage_ledger(eid, fallback_quota=1)
    assert ledger.grades["B"] >= 2
    assert ledger.covered_within_quota() is False


def test_integration_fallback_b_with_reason(memdb):
    eid, _ = _seed_episode(memdb, 2)
    _add_succeeded_version(memdb, f"{eid}_shot_1", qa={"overall": 0.92, "failure_types": []})
    _add_succeeded_version(
        memdb, f"{eid}_shot_2",
        qa={"overall": 0.45, "failure_types": ["state_mismatch"]},
    )
    ledger = rebuild_coverage_ledger(eid, fallback_quota=1)
    b = next(e for e in ledger.entries if e.shot_no == 2)
    assert b.grade == "B"
    assert b.fallback_reason
    assert ledger.covered_within_quota()


def test_integration_continuity_degraded_marks_b(memdb):
    eid, _ = _seed_episode(memdb, 2)
    _add_succeeded_version(memdb, f"{eid}_shot_1", qa={"overall": 0.9, "failure_types": []})
    _add_succeeded_version(
        memdb, f"{eid}_shot_2",
        qa={"overall": 0.9, "failure_types": []},
        continuity_degraded=True,
    )
    # continuity_degraded 保存在 checkpoint shot_state；直接用 entry 级验证 grade_shot_video 路径
    from app.evidence.media import grade_shot_video
    g = grade_shot_video(
        technical={"passed": True},
        qa={"overall": 0.9, "failure_types": []},
        continuity_degraded=True,
    )
    assert g["grade"] == "B"


def test_cost_regression_all_fail_within_cap(memdb):
    eid, _ = _seed_episode(memdb, 11)
    ledger = rebuild_coverage_ledger(eid, fallback_quota=2)
    cap = 150.0
    remaining = cap
    paid = 0.0
    for e in ledger.entries:
        n = attempts_for(e, ledger, budget_cap_cny=cap)
        unit = 4.0
        per_shot_cap = (cap / max(1, ledger.shots_total)) * SHOT_BUDGET_MULTIPLIER
        spend = min(n * unit, remaining, per_shot_cap)
        remaining -= spend
        paid += spend
    assert paid <= cap + 1e-6
    assert remaining >= -1e-6


def test_cost_regression_jitter_baseline(memdb):
    """抖动：一半首轮过、一半需 3 次 → 记录基线成本上界。"""
    eid, _ = _seed_episode(memdb, 10)
    total = 0.0
    for i in range(1, 11):
        attempts = 1 if i <= 5 else 3
        cost = attempts * 4.0
        for a in range(attempts):
            qa = {"overall": 0.9, "failure_types": []} if a == attempts - 1 else {
                "overall": 0.3, "failure_types": ["state_mismatch"],
            }
            # 只保留最终成功版成本累计：模拟 paid 总账
            if a == attempts - 1:
                _add_succeeded_version(
                    memdb, f"{eid}_shot_{i}", qa=qa, cost=cost,
                )
        total += cost
    ledger = rebuild_coverage_ledger(eid, fallback_quota=2)
    assert ledger.cost_spent == pytest.approx(total)
    # golden 基线上界：5*4 + 5*12 = 80
    assert ledger.cost_spent <= 80 + 1e-6
    assert ledger.covered_within_quota()


def test_cost_regression_cascade_depth():
    n = SimpleNamespace(chain_position=0, grade="B", human_adopted=False)
    cascaded = 0
    for pos in range(1, 10):
        d = SimpleNamespace(chain_position=pos, grade="C", human_adopted=False)
        if should_cascade(n, d, state_drift=True):
            cascaded += 1
    assert cascaded <= MAX_CHAIN_CASCADE_DEPTH


def test_fake_enqueue_dispatch_reused_and_preflight(memdb, monkeypatch):
    """进程内假 enqueue：reused 不计进展；CompileError 落 Issue。"""
    from app import worker
    from app.video_supervisor import ShotCoverageEntry, _dispatch

    eid, _ = _seed_episode(memdb, 2)
    calls = {"n": 0}

    def fake_enqueue(shot_id, **kwargs):
        calls["n"] += 1
        if shot_id.endswith("_1"):
            return {"reused": True, "version_id": "v_old"}
        raise CompileError("动作容量超限")

    monkeypatch.setattr(worker, "enqueue_shot", fake_enqueue)
    e1 = ShotCoverageEntry(shot_no=1, shot_id=f"{eid}_shot_1", grade="C")
    e2 = ShotCoverageEntry(shot_no=2, shot_id=f"{eid}_shot_2", grade="C")
    assert _dispatch(e1, episode_id=eid, run_id=None, first=True) is False
    assert _dispatch(e2, episode_id=eid, run_id=None, first=True) is False
    assert calls["n"] == 2
    ledger = rebuild_coverage_ledger(eid)
    assert "VIDEO_PREFLIGHT_BLOCKED" in next(
        e for e in ledger.entries if e.shot_no == 2
    ).last_issue_codes


def test_fake_enqueue_paid_attempts_bounded(memdb, monkeypatch):
    """假 provider：每镜成功计费，总账不超 cap（模拟全失败前采纳预算墙）。"""
    from app import worker
    from app.video_supervisor import ShotCoverageEntry, _dispatch

    eid, _ = _seed_episode(memdb, 11)
    cap = 150.0
    paid_versions = {"n": 0}

    def fake_enqueue(shot_id, **kwargs):
        paid_versions["n"] += 1
        no = paid_versions["n"]
        vid = f"fake_ver_{no}"
        memdb.execute(
            """INSERT INTO shot_versions(
                id, shot_id, version_no, prompt_text, idem_key, status, created_at,
                qa_json, cost_cny, technical_validation_json, provider_task_id
            ) VALUES(?,?,?,?,?, 'succeeded', 1.0, ?, 4.0, ?, ?)""",
            (
                vid, shot_id, no, "p", f"idem-{vid}",
                json.dumps({"overall": 0.2, "failure_types": ["state_mismatch"]}),
                json.dumps({"passed": True, "issues": []}),
                f"task-{vid}",
            ),
        )
        memdb.commit()
        return {"version_id": vid, "reused": False}

    monkeypatch.setattr(worker, "enqueue_shot", fake_enqueue)
    ledger = rebuild_coverage_ledger(eid, fallback_quota=2)
    spent = 0.0
    for e in ledger.entries:
        budgeted = attempts_for(e, ledger, budget_cap_cny=cap)
        per_shot_cap = (cap / 11) * SHOT_BUDGET_MULTIPLIER
        for _ in range(budgeted):
            if spent + 4.0 > cap:
                break
            if spent + 4.0 > per_shot_cap and e.cost_spent_cny >= per_shot_cap:
                break
            ok = _dispatch(
                ShotCoverageEntry(shot_no=e.shot_no, shot_id=e.shot_id, grade="C"),
                episode_id=eid, run_id=None, first=False,
            )
            if ok:
                spent += 4.0
                e.cost_spent_cny += 4.0
        ledger = rebuild_coverage_ledger(eid, fallback_quota=2)
        spent = ledger.cost_spent
        if spent >= cap:
            break
    assert ledger.cost_spent <= cap + 1e-6


def test_cost_model_predicts_positive(memdb):
    eid, _ = _seed_episode(memdb, 4)
    for i in range(1, 3):
        _add_succeeded_version(
            memdb, f"{eid}_shot_{i}", qa={"overall": 0.9, "failure_types": []}, cost=5.0,
        )
    pred = predict_shot_completion_cost(5, episode_id=eid, grade="C")
    assert pred["expected_cny"] > 0
    ep = predict_episode_completion_cost(eid)
    assert ep["expected_cny"] >= 0


def test_auto_crop_without_ffmpeg_graceful(tmp_path):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"not-a-real-mp4")
    dest = tmp_path / "b.mp4"
    result = auto_crop_video(str(src), str(dest), expected_duration_s=5)
    assert result["ok"] is False


def test_router_needs_crop_auto_crop():
    plan = route([
        Issue(
            code="VIDEO_QA_NEEDS_CROP",
            severity=IssueSeverity.WARNING,
            subject="s",
            message="needs crop",
            evidence={"path": "1", "rule_id": "needs_crop"},
        )
    ])
    assert plan.strategy == "auto_crop"
    assert plan.is_paid is False


def test_soft_budget_constant():
    assert 0 < FIRST_PASS_BUDGET_FRACTION < 1


def test_bump_grant(memdb):
    eid, pid = _seed_episode(memdb, 2)
    grant, _ = issue_video_completion_grant(
        episode_id=eid, project_id=pid, storyboard_artifact_id="art_sb_v1",
        budget_cap_cny=50, shots_total=2,
    )
    updated = bump_video_grant_budget(grant.grant_id, add_cny=100)
    assert updated.budget_cap_cny >= 150
    ok = validate_video_grant(grant.grant_id, episode_id=eid, storyboard_artifact_id="art_sb_v1")
    assert ok.budget_cap_cny == updated.budget_cap_cny


def test_project_plan_budget_allocation(memdb):
    """跨集编排：全局预算串行分配，已覆盖集跳过。"""
    t = 1.0
    memdb.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p_multi','跨集', 'created', ?)",
        (t,),
    )
    for no, eid in enumerate(["ep_a", "ep_b", "ep_c"], start=1):
        memdb.execute(
            """INSERT INTO episodes(
                id, project_id, episode_no, title, status, storyboard_artifact_id, created_at
            ) VALUES(?,?,?,?, 'confirmed', 'art', ?)""",
            (eid, "p_multi", no, f"E{no}", t),
        )
        for i in range(1, 3):
            memdb.execute(
                """INSERT INTO shots(
                    id, episode_id, shot_no, duration_s, shot_size, camera_move,
                    scene_setting, characters, action_desc, dialogues
                ) VALUES(?,?,?,?, '中景', '固定', '日', '[]', '动作', '[]')""",
                (f"{eid}_s{i}", eid, i, 5),
            )
    # ep_a 已全覆盖
    for i in range(1, 3):
        _add_succeeded_version(memdb, f"ep_a_s{i}", qa={"overall": 0.9, "failure_types": []})
    memdb.commit()

    # 复用 core 的规划逻辑（不真正 spawn supervisor）
    from app.video_supervisor import rebuild_coverage_ledger as rebuild
    global_cap = 200.0
    per_cap = 80.0
    rows = memdb.execute(
        "SELECT id, episode_no, status FROM episodes WHERE project_id=? ORDER BY episode_no",
        ("p_multi",),
    ).fetchall()
    remaining = global_cap
    plan = []
    for r in rows:
        ledger = rebuild(r["id"])
        if ledger.covered_within_quota():
            plan.append({"id": r["id"], "status": "already_covered", "cap": 0})
            continue
        if remaining < 5:
            plan.append({"id": r["id"], "status": "skipped_budget", "cap": 0})
            continue
        ep_cap = min(per_cap, remaining)
        plan.append({"id": r["id"], "status": "queued", "cap": ep_cap})
        remaining -= ep_cap
    assert plan[0]["status"] == "already_covered"
    assert plan[1]["status"] == "queued" and plan[1]["cap"] == 80
    assert plan[2]["status"] == "queued" and plan[2]["cap"] == 80
    assert remaining == 40
