"""视频补齐 Supervisor 单元测试：grade / issues / router / budget / cascade / grant。"""
from __future__ import annotations

import math

import pytest

from app.harness.types import Issue, IssueSeverity
from app.video_issues import (
    DEFAULT_FATAL_FAILURE_TYPES,
    is_fatal,
    is_fatal_failure_code,
    issues_from_enqueue_error,
    issues_from_job_failure,
    issues_from_qa,
)
from app.video_repair_router import (
    MAX_CHAIN_CASCADE_DEPTH,
    bump_fingerprint_count,
    route,
    should_cascade,
    state_drift_significant,
    upgrade_level,
)
from app.video_supervisor import (
    MAX_ATTEMPTS_PER_SHOT,
    MIN_ATTEMPTS_PER_SHOT,
    CoverageLedger,
    ShotCoverageEntry,
    attempts_for,
)
from app.evidence.media import grade_shot_video
from app.compiler import CompileError


def test_grade_shot_video_a_b_c():
    a = grade_shot_video(
        technical={"passed": True},
        qa={"overall": 0.9, "failure_types": []},
    )
    assert a["grade"] == "A"

    b = grade_shot_video(
        technical={"passed": True},
        qa={"overall": 0.4, "failure_types": ["state_mismatch"]},
    )
    assert b["grade"] == "B"
    assert b["fallback_reason"]

    c = grade_shot_video(
        technical={"passed": False},
        qa={"overall": 0.9},
    )
    assert c["grade"] == "C"

    fatal = grade_shot_video(
        technical={"passed": True},
        qa={"overall": 0.95, "failure_types": ["character_duplicate"]},
    )
    assert fatal["grade"] == "C"
    assert fatal["identity_integrity_failures"] == ["character_duplicate"]

    degraded = grade_shot_video(
        technical={"passed": True},
        qa={"overall": 0.95, "failure_types": []},
        continuity_degraded=True,
    )
    assert degraded["grade"] == "B"


def test_fatal_failure_types_default():
    assert is_fatal_failure_code("character_duplicate")
    assert is_fatal_failure_code("wrong_identity")
    assert is_fatal_failure_code("wrong_outfit")
    assert is_fatal_failure_code("text_error")
    assert not is_fatal_failure_code("state_mismatch")
    assert set(DEFAULT_FATAL_FAILURE_TYPES) == {
        "character_duplicate",
        "wrong_identity",
        "wrong_outfit",
        "text_error",
    }


def test_issues_from_qa_and_job_and_enqueue():
    qa_issues = issues_from_qa(
        {"overall": 0.3, "failure_types": ["state_mismatch"]},
        {"passed": True},
        shot_id="shot_1",
        version_id="ver_1",
        shot_no=9,
    )
    codes = {i.code for i in qa_issues}
    assert "VIDEO_QA_STATE_MISMATCH" in codes
    assert "VIDEO_QA_LOW_SCORE" not in codes  # 有硬失败时不叠加低分

    job_issues = issues_from_job_failure(
        {"id": "job_1", "shot_id": "shot_1", "error": "内容安全拒绝", "status": "failed"},
        None,
        shot_id="shot_1",
        shot_no=3,
    )
    assert job_issues[0].code == "VIDEO_PROVIDER_SAFETY"

    enq = issues_from_enqueue_error(
        CompileError("动作容量超限；状态链断裂"),
        shot_id="shot_1",
        shot_no=6,
    )
    assert enq[0].code == "VIDEO_PREFLIGHT_BLOCKED"
    assert is_fatal(Issue(
        code="VIDEO_QA_CHARACTER_DUPLICATE",
        severity=IssueSeverity.BLOCKER,
        subject="s",
        message="x",
        evidence={"rule_id": "character_duplicate"},
    ))


def test_repair_router_levels_and_upgrade():
    plan = route([
        Issue(
            code="VIDEO_QA_STATE_MISMATCH",
            severity=IssueSeverity.BLOCKER,
            subject="shot_x",
            message="状态不匹配",
            evidence={"shot_no": 9, "path": "9", "rule_id": "state_mismatch"},
        )
    ])
    assert plan.level == "L0"
    assert plan.strategy == "handoff_human"
    assert plan.is_paid is False
    assert plan.pause_state is None

    # QA-only issues never escalate into the repair router.
    fp = plan.fingerprint
    counts = bump_fingerprint_count({}, fp)
    counts = bump_fingerprint_count(counts, fp)
    plan2 = route(
        [Issue(
            code="VIDEO_QA_STATE_MISMATCH",
            severity=IssueSeverity.BLOCKER,
            subject="shot_x",
            message="状态不匹配",
            evidence={"shot_no": 9, "path": "9", "rule_id": "state_mismatch"},
        )],
        fingerprint_counts=counts,
        current_level="L2",
    )
    assert plan2.level == "L0"
    assert plan2.strategy == "handoff_human"
    assert plan2.is_paid is False
    assert plan2.pause_state is None

    # L5 without auth
    plan5 = route([
        Issue(
            code="VIDEO_PREFLIGHT_BLOCKED",
            severity=IssueSeverity.BLOCKER,
            subject="shot_x",
            message="preflight",
            evidence={"path": "6", "rule_id": "preflight"},
        )
    ], allow_storyboard_edit=False)
    assert plan5.pause_state == "WAITING_AUTHORIZATION"

    assert upgrade_level("L5") == "L6"


def test_qa_gain_never_enters_paid_repair():
    issue = Issue(
        code="VIDEO_QA_LOW_SCORE",
        severity=IssueSeverity.WARNING,
        subject="s",
        message="low",
        evidence={"path": "1", "rule_id": "low_score"},
    )
    plan = route([issue], current_level="L1", qa_history=[0.40, 0.41])
    assert plan.level == "L0"
    assert plan.strategy == "handoff_human"
    assert plan.is_paid is False


def test_attempts_for_budget():
    entry = ShotCoverageEntry(shot_no=1, shot_id="s1", grade="C", chain_position=0, chain_len=3)
    ledger = CoverageLedger(
        episode_id="ep",
        shots_total=10,
        grades={"A": 0, "B": 0, "C": 10},
        coverage_rate=0.0,
        fallback_quota=2,
        entries=[entry],
        cost_spent=0.0,
    )
    n = attempts_for(entry, ledger, budget_cap_cny=150.0)
    assert MIN_ATTEMPTS_PER_SHOT <= n <= MAX_ATTEMPTS_PER_SHOT


def test_first_pass_soft_budget_and_per_shot_cap():
    from app.video_supervisor import FIRST_PASS_BUDGET_FRACTION, SHOT_BUDGET_MULTIPLIER

    cap = 150.0
    soft = cap * FIRST_PASS_BUDGET_FRACTION
    assert soft == pytest.approx(97.5)
    # 首轮：已花费 >= soft 时不应再派未尝试镜（契约常量 + 主循环守卫）
    assert soft < cap
    per_shot = (cap / 11) * SHOT_BUDGET_MULTIPLIER
    assert per_shot == pytest.approx((150 / 11) * 3.0)
    assert per_shot < cap


def test_should_cascade_branches():
    class E:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    n = E(chain_position=0, grade="B", human_adopted=False)
    # 人工采用
    assert should_cascade(n, E(chain_position=1, grade="C", human_adopted=True), state_drift=True) is False
    # A 级无漂移
    assert should_cascade(n, E(chain_position=1, grade="A", human_adopted=False), state_drift=False) is False
    # 超深度
    assert should_cascade(
        n, E(chain_position=0 + MAX_CHAIN_CASCADE_DEPTH + 1, grade="C", human_adopted=False),
        state_drift=True,
    ) is False
    # 正常级联
    assert should_cascade(n, E(chain_position=1, grade="C", human_adopted=False), state_drift=True) is True


def test_state_drift():
    assert state_drift_significant("站在门口微笑", "站在门口微笑") is False
    assert state_drift_significant("站在门口微笑", "倒在血泊中") is True


def test_covered_within_quota():
    entries = [
        ShotCoverageEntry(
            shot_no=i, shot_id=f"s{i}", grade="A", adopted_version_id=f"v{i}",
        )
        for i in range(1, 10)
    ]
    entries.append(ShotCoverageEntry(
        shot_no=10, shot_id="s10", grade="B", adopted_version_id="v10",
    ))
    ledger = CoverageLedger(
        episode_id="ep",
        shots_total=10,
        grades={"A": 9, "B": 1, "C": 0},
        coverage_rate=1.0,
        fallback_quota=2,
        entries=entries,
    )
    assert ledger.covered_within_quota() is True

    entries2 = entries + [ShotCoverageEntry(
        shot_no=11, shot_id="s11", grade="B", adopted_version_id="v11",
    )]
    # rebuild grades
    ledger2 = CoverageLedger(
        episode_id="ep",
        shots_total=11,
        grades={"A": 9, "B": 2, "C": 0},
        coverage_rate=1.0,
        fallback_quota=1,
        entries=entries2,
    )
    assert ledger2.covered_within_quota() is True  # 已采用版本不因 B 配额被撤销或重烧

    unadopted = entries.copy()
    unadopted[0] = unadopted[0].model_copy(update={"adopted_version_id": None})
    ledger3 = ledger.model_copy(update={"entries": unadopted})
    assert ledger3.covered_within_quota() is False  # 有候选但未采用不能冒充完成


def test_video_grant_episode_binding(tmp_path, monkeypatch):
    from app import db
    from app.completion_grant import (
        issue_video_completion_grant,
        validate_video_grant,
        GrantValidationError,
    )

    db.init_db()
    conn = db.get_conn()
    episode_id = db.new_id("ep_test_grant")
    project_id = db.new_id("proj_x")
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES(?,?, 'created', 1)",
        (project_id, "grant scope"),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,status,storyboard_artifact_id,created_at
           ) VALUES(?,?,1,'grant episode','confirmed','art_sb_v1',1)""",
        (episode_id, project_id),
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,continuity_from_prev
           ) VALUES(?,?,1,5,'中景','固定','室内','[]',
                    '人物在空间中完成一个可见且连续的主动作。','[]',0)""",
        (f"{episode_id}_shot", episode_id),
    )
    conn.commit()
    grant, token = issue_video_completion_grant(
        episode_id=episode_id,
        project_id=project_id,
        storyboard_artifact_id="art_sb_v1",
        budget_cap_cny=100,
        shots_total=10,
    )
    assert token
    ok = validate_video_grant(
        grant.grant_id,
        episode_id=episode_id,
        storyboard_artifact_id="art_sb_v1",
    )
    assert ok.budget_cap_cny == 100

    with pytest.raises(GrantValidationError) as ei:
        validate_video_grant(
            grant.grant_id,
            episode_id="ep_other",
            storyboard_artifact_id="art_sb_v1",
        )
    assert ei.value.code == "GRANT_EPISODE_MISMATCH"

    with pytest.raises(GrantValidationError) as ei2:
        validate_video_grant(
            grant.grant_id,
            episode_id=episode_id,
            storyboard_artifact_id="art_sb_CHANGED",
        )
    assert ei2.value.code == "UPSTREAM_VERSION_CHANGED"


def test_reused_not_progress_documented():
    """契约：enqueue 返回 reused=True 时 Supervisor 不计进展（由 _dispatch 保证）。"""
    from app.video_supervisor import _dispatch
    # 纯文档/签名存在性：函数可导入
    assert callable(_dispatch)


def test_default_fallback_quota():
    from app.completion_grant import default_max_fallback_shots
    assert default_max_fallback_shots(11) == max(1, int(math.ceil(11 * 0.2)))
