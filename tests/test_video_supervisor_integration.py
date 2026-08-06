"""视频 Supervisor 集成 / 成本回归（进程内假入队，不打真实 Seedance）。"""
from __future__ import annotations

import asyncio
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
    CoverageLedger,
    ShotCoverageEntry,
    VideoSupervisorCheckpoint,
    _apply_cascade,
    _deadline_closeout,
    _finalize_covered,
    _reconcile_terminal_continuity_blocks,
    attempts_for,
    preview_video_completion_repair,
    reconcile_stale_video_supervisors,
    recover_video_completion_runs,
    rebuild_coverage_ledger,
    run_video_completion_resilient,
    save_checkpoint,
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
    import app.orchestration.media_scheduler as media_scheduler
    import app.orchestration.state_machine as state_machine
    import app.video_cost_model as video_cost_model
    import app.video_crop as video_crop
    import app.video_supervisor as video_supervisor

    for mod in (
        db_mod,
        evidence_repository,
        completion_grant,
        evidence_media,
        media_scheduler,
        state_machine,
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


def test_cleared_versions_do_not_count_as_current_epoch_attempts(memdb) -> None:
    eid, _ = _seed_episode(memdb, 1)
    shot_id = f"{eid}_shot_1"
    memdb.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,cost_cny,
               provider_task_id,image_inputs,created_at
           ) VALUES(
               'cleared-old',?,1,'old','old-key','cleared',12,
               'old-provider','{"provider_paid_attempts":3}',1
           )""",
        (shot_id,),
    )
    memdb.commit()

    entry = rebuild_coverage_ledger(eid).entries[0]

    assert entry.best_version_id is None
    assert entry.attempts_paid == 0
    assert entry.attempts_dispatched == 0
    assert entry.never_attempted is True
    assert entry.cost_spent_cny == 0


@pytest.mark.asyncio
async def test_fresh_control_plane_recovery_never_loads_previous_run_checkpoint(
    monkeypatch,
) -> None:
    import app.observability.metrics as metrics
    import app.video_supervisor as supervisor

    old = VideoSupervisorCheckpoint(
        episode_id="e",
        run_id="run-old",
        phase="OBSERVING",
        started_at=1,
        shot_state={"1": {"attempts_paid": 5}},
        budget={"spent_cny": 99},
    )
    saved: list[VideoSupervisorCheckpoint] = []
    calls = 0

    async def run_once(_episode_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fresh run failed before first checkpoint")
        assert kwargs["resume"] is True
        return saved[-1]

    monkeypatch.setattr(supervisor, "run_video_completion_supervisor", run_once)
    monkeypatch.setattr(
        supervisor,
        "load_latest_checkpoint",
        lambda _episode_id: saved[-1] if saved else old,
    )
    monkeypatch.setattr(
        supervisor,
        "save_checkpoint",
        lambda cp, **_kwargs: saved.append(cp.model_copy(deep=True)) or "checkpoint",
    )
    monkeypatch.setattr(supervisor.evidence_repository, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(metrics, "inc", lambda *a, **k: None)
    monkeypatch.setattr(supervisor.asyncio, "sleep", lambda _seconds: _immediate())

    async def execute():
        return await run_video_completion_resilient(
            "e",
            run_id="run-new",
            resume=False,
            wall_clock_cap_s=60,
        )

    async def _noop():
        return None

    async def _run_with_sleep_patch():
        return await execute()

    # ``asyncio.sleep`` must remain awaitable under the deterministic patch.
    async def _immediate():
        await _noop()

    result = await _run_with_sleep_patch()

    assert result.run_id == "run-new"
    assert result.shot_state == {}
    assert result.budget == {}
    assert saved[0].run_id == "run-new"


def test_coverage_ledger_counts_provider_attempts_inside_one_version(memdb):
    eid, _ = _seed_episode(memdb, 1)
    version_id = _add_succeeded_version(
        memdb,
        f"{eid}_shot_1",
        qa={"overall": 0.9, "failure_types": []},
        cost=12.0,
    )
    memdb.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id=?",
        (json.dumps({"provider_paid_attempts": 3}), version_id),
    )
    memdb.commit()

    ledger = rebuild_coverage_ledger(eid)

    assert ledger.entries[0].attempts_paid == 3
    assert ledger.entries[0].cost_spent_cny == 12.0


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


@pytest.mark.asyncio
async def test_fresh_completion_with_all_adopted_finishes_without_enqueue(memdb, monkeypatch):
    """点击“补齐”时若全部已有采用版，应立即完成且零派发。"""
    from app import worker
    from app.video_supervisor import run_video_completion_supervisor

    eid, _ = _seed_episode(memdb, 3)
    for i in range(1, 4):
        _add_succeeded_version(
            memdb,
            f"{eid}_shot_{i}",
            qa={"overall": 0.2, "failure_types": ["state_mismatch"]},
        )
    memdb.execute(
        "UPDATE shots SET storyboard_artifact_id='old_storyboard' WHERE episode_id=?",
        (eid,),
    )
    memdb.commit()

    def forbidden_enqueue(*_args, **_kwargs):
        raise AssertionError("已有采用版时不得派发视频任务")

    monkeypatch.setattr(worker, "enqueue_shot", forbidden_enqueue)

    result = await run_video_completion_supervisor(
        eid,
        resume=False,
        wall_clock_cap_s=60,
    )

    assert result.phase == "SUCCEEDED_COVERED"
    assert result.coverage["adopted"] == 3
    assert result.coverage["unadopted"] == 0
    assert memdb.execute(
        "SELECT COUNT(*) AS c FROM jobs WHERE episode_id=? AND kind='video'",
        (eid,),
    ).fetchone()["c"] == 0


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


def test_integration_adopted_b_over_quota_is_still_complete(memdb):
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
    assert ledger.covered_within_quota() is True
    assert ledger.actionable() == []


def test_adopted_stale_shot_is_protected_and_only_unadopted_is_actionable(memdb):
    eid, _ = _seed_episode(memdb, 2)
    adopted = _add_succeeded_version(
        memdb, f"{eid}_shot_1", qa={"overall": 0.9, "failure_types": []},
    )
    memdb.execute(
        "UPDATE shots SET storyboard_artifact_id='old_storyboard' WHERE id=?",
        (f"{eid}_shot_1",),
    )
    memdb.commit()

    ledger = rebuild_coverage_ledger(eid, fallback_quota=0)

    protected = next(entry for entry in ledger.entries if entry.shot_no == 1)
    missing = next(entry for entry in ledger.entries if entry.shot_no == 2)
    assert protected.adopted_version_id == adopted
    assert protected.video_stale is True
    assert protected.grade == "C"
    assert ledger.count_uncovered() == 1
    assert [entry.shot_no for entry in ledger.actionable()] == [2]
    assert ledger.coverage_rate == pytest.approx(0.5)
    assert missing.adopted_version_id is None


def test_per_shot_artifact_inside_current_episode_aggregate_is_not_stale(memdb):
    from app.domain.storyboard_ops import _shot_video_is_stale
    from app.harness.types import EvidenceArtifact

    eid, _ = _seed_episode(memdb, 1)
    shot_id = f"{eid}_shot_1"
    shot_art = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot",
        scope_type="storyboard_checkpoint",
        scope_id=f"{eid}:1",
        status="approved",
        trust_level="T2",
        content={"shot_no": 1},
    ))
    episode_art = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard",
        scope_type="episode",
        scope_id=eid,
        status="approved",
        trust_level="T4",
        content={"episode_no": 1},
        parent_artifact_ids=[shot_art["id"]],
    ))
    version_id = _add_succeeded_version(
        memdb, shot_id, qa={"overall": 0.9, "failure_types": []},
    )
    video_art = evidence_repository.create_artifact(EvidenceArtifact(
        type="shot_video",
        scope_type="shot",
        scope_id=shot_id,
        status="validated",
        trust_level="T3",
        content={"version_id": version_id},
        parent_artifact_ids=[shot_art["id"]],
    ))
    memdb.execute(
        "UPDATE episodes SET storyboard_artifact_id=? WHERE id=?",
        (episode_art["id"], eid),
    )
    memdb.execute(
        "UPDATE shots SET storyboard_artifact_id=? WHERE id=?",
        (shot_art["id"], shot_id),
    )
    memdb.execute(
        "UPDATE shot_versions SET artifact_id=? WHERE id=?",
        (video_art["id"], version_id),
    )
    memdb.commit()

    shot_row = memdb.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    ledger = rebuild_coverage_ledger(eid, fallback_quota=0)

    assert ledger.entries[0].video_stale is False
    assert _shot_video_is_stale(memdb, shot_row, episode_art["id"]) is False


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


def test_rebuild_ledger_never_moves_attempt_count_back_to_stale_checkpoint(memdb):
    eid, _ = _seed_episode(memdb, 1)
    shot_id = f"{eid}_shot_1"
    _add_succeeded_version(
        memdb, shot_id, qa={"overall": 0.2, "failure_types": ["state_mismatch"]},
    )
    _add_succeeded_version(
        memdb, shot_id, qa={"overall": 0.3, "failure_types": ["state_mismatch"]},
    )
    cp = VideoSupervisorCheckpoint(
        episode_id=eid,
        shot_state={"1": {"attempts_paid": 1}},
        coverage={"fallback_quota": 1},
    )

    ledger = rebuild_coverage_ledger(eid, cp=cp)

    assert ledger.entries[0].attempts_paid == 2


def test_apply_cascade_reads_planned_state_from_contract_on_production_schema(memdb):
    eid, _ = _seed_episode(memdb, 2)
    shot_id = f"{eid}_shot_1"
    version_id = _add_succeeded_version(
        memdb,
        shot_id,
        qa={
            "overall": 0.8,
            "failure_types": [],
            "observed_state_out": "角色倒在地上",
        },
    )
    memdb.execute(
        "UPDATE shots SET shot_contract_json=?, last_frame_desc=? WHERE id=?",
        (json.dumps({"state_out": "角色站在门口"}), "角色站在门口", shot_id),
    )
    memdb.commit()
    current = ShotCoverageEntry(
        shot_no=1,
        shot_id=shot_id,
        grade="B",
        best_version_id=version_id,
        chain_head_shot_no=1,
        chain_position=0,
    )
    downstream = ShotCoverageEntry(
        shot_no=2,
        shot_id=f"{eid}_shot_2",
        grade="C",
        chain_head_shot_no=1,
        chain_position=1,
    )
    ledger = CoverageLedger(
        episode_id=eid,
        shots_total=2,
        grades={"A": 0, "B": 1, "C": 1},
        entries=[current, downstream],
    )
    cp = VideoSupervisorCheckpoint(episode_id=eid)

    cascaded = _apply_cascade(current, ledger, cp)

    assert cascaded == [2]
    assert downstream.chain_stale is True


def test_deadline_closeout_adopts_best_candidate_without_image_fallback(memdb, tmp_path, monkeypatch):
    eid, _ = _seed_episode(memdb, 4)
    memdb.execute(
        "UPDATE episodes SET status='generating', video_completion_mode='complete' WHERE id=?",
        (eid,),
    )
    # 镜3已经采用；镜4存在技术可播但 QA 不达标的候选，必须在截止时采用。
    _add_succeeded_version(
        memdb, f"{eid}_shot_3", qa={"overall": 0.9, "failure_types": []},
    )
    candidate = _add_succeeded_version(
        memdb, f"{eid}_shot_4", qa={"overall": 0.2, "failure_types": ["state_mismatch"]},
    )
    candidate_path = tmp_path / "candidate-shot-4.mp4"
    candidate_path.write_bytes(b"candidate")
    memdb.execute("UPDATE shot_versions SET video_path=? WHERE id=?", (str(candidate_path), candidate))
    memdb.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (f"{eid}_shot_4",))
    # 镜2永远等待镜1尾帧；收口必须把它停止，不能继续显示 active。
    memdb.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at)
           VALUES('v_wait',?,1,'p','wait','queued',1)""",
        (f"{eid}_shot_2",),
    )
    memdb.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at,
               after_shot_id,pipeline_stage
           ) VALUES('j_wait','video',?,'v_wait',?,'proj_int','queued',1,1,?,'waiting_continuity_anchor')""",
        (f"{eid}_shot_2", eid, f"{eid}_shot_1"),
    )
    # 镜3已有采用版；它的独立重抽不属于“补齐”，截止收口也不能误停。
    memdb.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at)
           VALUES('v_adopted_manual_retake',?,2,'p','adopted-manual-retake','queued',1)""",
        (f"{eid}_shot_3",),
    )
    memdb.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at)
           VALUES('j_adopted_manual_retake','video',?,'v_adopted_manual_retake',?,'proj_int','queued',1,1)""",
        (f"{eid}_shot_3", eid),
    )
    memdb.commit()
    cp = VideoSupervisorCheckpoint(
        episode_id=eid,
        phase="PLANNING_COVERAGE",
        started_at=1,
        deadline_at=2,
        budget={"cap_cny": 150},
        coverage={"fallback_quota": 1},
    )

    result = _deadline_closeout(cp, run_id=None)

    assert result.phase == "PARTIAL_NO_USABLE_CANDIDATE"
    assert result.missing_shots == [1, 2]
    assert {item["shot_no"] for item in result.closeout_adoptions} == {4}
    assert memdb.execute(
        "SELECT adopted_version_id FROM shots WHERE id=?", (f"{eid}_shot_4",),
    ).fetchone()["adopted_version_id"] == candidate
    assert memdb.execute("SELECT status FROM jobs WHERE id='j_wait'").fetchone()["status"] == "cancelled"
    assert memdb.execute(
        "SELECT status FROM jobs WHERE id='j_adopted_manual_retake'",
    ).fetchone()["status"] == "queued"
    episode = memdb.execute(
        "SELECT status,video_completion_mode,active_video_run_id FROM episodes WHERE id=?", (eid,),
    ).fetchone()
    assert dict(episode) == {
        "status": "confirmed", "video_completion_mode": "quick", "active_video_run_id": None,
    }


def test_deadline_closeout_finishes_when_every_shot_has_technical_candidate(memdb):
    eid, _ = _seed_episode(memdb, 2)
    memdb.execute(
        "UPDATE episodes SET status='generating', video_completion_mode='complete' WHERE id=?",
        (eid,),
    )
    for i in (1, 2):
        _add_succeeded_version(
            memdb, f"{eid}_shot_{i}", qa={"overall": 0.2, "failure_types": ["state_mismatch"]},
        )
        memdb.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (f"{eid}_shot_{i}",))
    memdb.commit()
    cp = VideoSupervisorCheckpoint(
        episode_id=eid, started_at=1, deadline_at=2,
        budget={"cap_cny": 150}, coverage={"fallback_quota": 0},
    )

    result = _deadline_closeout(cp, run_id=None)

    assert result.phase == "COMPLETED_DEADLINE_FALLBACK"
    assert result.missing_shots == []
    assert len(result.closeout_adoptions) == 2
    assert result.quality_target_missed is True

    # Closeout is an irreversible terminal transition, so a watchdog/retry may
    # safely call it again without duplicating adoption or coverage reports.
    repeated = _deadline_closeout(result, run_id=None)
    assert repeated.phase == "COMPLETED_DEADLINE_FALLBACK"
    assert len(repeated.closeout_adoptions) == 2
    reports = memdb.execute(
        """SELECT COUNT(*) AS c FROM artifacts
           WHERE type='video_coverage_report' AND scope_id=?""",
        (eid,),
    ).fetchone()["c"]
    assert reports == 1


def test_repair_preview_is_strictly_read_only(memdb):
    eid, _ = _seed_episode(memdb, 2)
    candidate = _add_succeeded_version(
        memdb, f"{eid}_shot_2", qa={"overall": 0.1, "failure_types": ["state_mismatch"]},
    )
    memdb.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (f"{eid}_shot_2",))
    memdb.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at)
           VALUES('v_preview_wait',?,1,'p','preview-wait','queued',1)""",
        (f"{eid}_shot_1",),
    )
    memdb.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at,
               after_shot_id,pipeline_stage
           ) VALUES('j_preview_wait','video',?,'v_preview_wait',?,'proj_int','queued',1,1,?,
                    'waiting_continuity_anchor')""",
        (f"{eid}_shot_1", eid, "missing_anchor"),
    )
    memdb.commit()
    before = {
        "adopted": [tuple(row) for row in memdb.execute(
            "SELECT id,adopted_version_id FROM shots WHERE episode_id=? ORDER BY shot_no", (eid,),
        ).fetchall()],
        "jobs": [tuple(row) for row in memdb.execute(
            "SELECT id,status,reason_code FROM jobs WHERE episode_id=? ORDER BY id", (eid,),
        ).fetchall()],
        "artifacts": memdb.execute(
            "SELECT COUNT(*) AS c FROM artifacts WHERE scope_id=?", (eid,),
        ).fetchone()["c"],
        "reasons": [tuple(row) for row in memdb.execute(
            """SELECT id,adoption_reason FROM shot_versions
               WHERE shot_id IN (SELECT id FROM shots WHERE episode_id=?) ORDER BY id""",
            (eid,),
        ).fetchall()],
    }

    preview = preview_video_completion_repair(eid)

    assert preview["dry_run"] is True
    assert preview["will_start_generation"] is False
    assert preview["will_delete_media"] is False
    assert [item["shot_no"] for item in preview["would_mark_missing"]] == [1]
    assert preview["would_adopt"][0]["selected_version_id"] == candidate
    after = {
        "adopted": [tuple(row) for row in memdb.execute(
            "SELECT id,adopted_version_id FROM shots WHERE episode_id=? ORDER BY shot_no", (eid,),
        ).fetchall()],
        "jobs": [tuple(row) for row in memdb.execute(
            "SELECT id,status,reason_code FROM jobs WHERE episode_id=? ORDER BY id", (eid,),
        ).fetchall()],
        "artifacts": memdb.execute(
            "SELECT COUNT(*) AS c FROM artifacts WHERE scope_id=?", (eid,),
        ).fetchone()["c"],
        "reasons": [tuple(row) for row in memdb.execute(
            """SELECT id,adoption_reason FROM shot_versions
               WHERE shot_id IN (SELECT id FROM shots WHERE episode_id=?) ORDER BY id""",
            (eid,),
        ).fetchall()],
    }
    assert after == before


@pytest.mark.asyncio
async def test_watchdog_closes_stale_run_when_task_record_is_missing(memdb, monkeypatch, tmp_path):
    import app.video_supervisor as video_supervisor

    eid, _ = _seed_episode(memdb, 1)
    candidate = _add_succeeded_version(
        memdb, f"{eid}_shot_1", qa={"overall": 0.2, "failure_types": ["state_mismatch"]},
    )
    candidate_path = tmp_path / "watchdog-candidate.mp4"
    candidate_path.write_bytes(b"candidate")
    memdb.execute("UPDATE shot_versions SET video_path=? WHERE id=?", (str(candidate_path), candidate))
    memdb.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (f"{eid}_shot_1",))
    run_id = evidence_repository.create_run(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id=eid,
        input_fingerprint="watchdog-missing-task",
        deadline_at=200,
    )
    memdb.execute(
        "UPDATE workflow_runs SET status='RUNNING', started_at=1, updated_at=1 WHERE id=?",
        (run_id,),
    )
    memdb.execute(
        """UPDATE episodes SET status='generating', video_completion_mode='complete',
                   active_video_run_id=? WHERE id=?""",
        (run_id, eid),
    )
    memdb.commit()
    cp = VideoSupervisorCheckpoint(
        episode_id=eid,
        run_id=run_id,
        phase="PLANNING_COVERAGE",
        started_at=1,
        deadline_at=200,
        budget={"cap_cny": 150},
        coverage={"fallback_quota": 0},
    )
    monkeypatch.setattr(video_supervisor, "now", lambda: 100.0)
    save_checkpoint(cp, run_id=run_id)
    monkeypatch.setattr(video_supervisor, "now", lambda: 1000.0)

    recovered = await reconcile_stale_video_supervisors()

    assert recovered == 1
    assert memdb.execute(
        "SELECT adopted_version_id FROM shots WHERE id=?", (f"{eid}_shot_1",),
    ).fetchone()["adopted_version_id"] == candidate
    episode = memdb.execute(
        "SELECT status,video_completion_mode,active_video_run_id FROM episodes WHERE id=?", (eid,),
    ).fetchone()
    assert dict(episode) == {
        "status": "confirmed", "video_completion_mode": "quick", "active_video_run_id": None,
    }


def test_startup_recovery_spawn_failure_restores_episode_state(memdb, monkeypatch):
    import app.task_registry as task_registry
    import app.video_supervisor as video_supervisor

    eid, _ = _seed_episode(memdb, 1)
    parent_run_id = evidence_repository.create_run(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id=eid,
        input_fingerprint="recovery-parent",
        deadline_at=500,
    )
    memdb.execute(
        "UPDATE workflow_runs SET status='RUNNING', started_at=1, updated_at=1 WHERE id=?",
        (parent_run_id,),
    )
    memdb.execute(
        """UPDATE episodes SET status='confirmed', video_completion_mode='complete',
                   active_video_run_id=? WHERE id=?""",
        (parent_run_id, eid),
    )
    memdb.commit()
    checkpoint = VideoSupervisorCheckpoint(
        episode_id=eid,
        run_id=parent_run_id,
        phase="PLANNING_COVERAGE",
        started_at=1,
        deadline_at=500,
    )
    monkeypatch.setattr(video_supervisor, "now", lambda: 100.0)
    save_checkpoint(checkpoint, run_id=parent_run_id)
    monkeypatch.setattr(task_registry, "active", lambda *_args: False)
    monkeypatch.setattr(
        task_registry,
        "spawn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )

    assert recover_video_completion_runs() == 0
    episode = memdb.execute(
        "SELECT status,active_video_run_id FROM episodes WHERE id=?",
        (eid,),
    ).fetchone()
    assert dict(episode) == {
        "status": "confirmed",
        "active_video_run_id": parent_run_id,
    }


@pytest.mark.asyncio
async def test_asset_preparation_heartbeat_prevents_watchdog_takeover(memdb, monkeypatch):
    import app.video_supervisor as video_supervisor

    eid, _ = _seed_episode(memdb, 1)
    run_id = evidence_repository.create_run(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id=eid,
        input_fingerprint="slow-reference-preparation",
        deadline_at=500,
    )
    memdb.execute(
        "UPDATE workflow_runs SET status='RUNNING', started_at=1, updated_at=1 WHERE id=?",
        (run_id,),
    )
    memdb.execute(
        """UPDATE episodes SET status='generating', video_completion_mode='complete',
                   active_video_run_id=? WHERE id=?""",
        (run_id, eid),
    )
    memdb.commit()
    cp = VideoSupervisorCheckpoint(
        episode_id=eid,
        run_id=run_id,
        phase="PREPARING_ASSETS",
        started_at=1,
        deadline_at=500,
        budget={"cap_cny": 150},
        coverage={"fallback_quota": 0},
    )
    clock = {"now": 100.0}
    monkeypatch.setattr(video_supervisor, "now", lambda: clock["now"])
    stop = asyncio.Event()
    task = asyncio.create_task(
        video_supervisor._asset_prep_heartbeat(
            cp, run_id=run_id, stop=stop, interval_s=0.01,
        )
    )
    await asyncio.sleep(0.03)
    stop.set()
    await task

    assert memdb.execute(
        "SELECT updated_at FROM workflow_runs WHERE id=?", (run_id,),
    ).fetchone()["updated_at"] == 100.0
    clock["now"] = 150.0
    assert await reconcile_stale_video_supervisors() == 0
    assert memdb.execute(
        "SELECT active_video_run_id FROM episodes WHERE id=?", (eid,),
    ).fetchone()["active_video_run_id"] == run_id


def test_terminal_continuity_wait_becomes_routable_issue(memdb):
    eid, _ = _seed_episode(memdb, 2)
    memdb.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at)
           VALUES('v_blocked',?,1,'p','blocked','queued',1)""",
        (f"{eid}_shot_2",),
    )
    memdb.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at,
               after_shot_id,pipeline_stage
           ) VALUES('j_blocked','video',?,'v_blocked',?,'proj_int','queued',1,1,?,'waiting_continuity_anchor')""",
        (f"{eid}_shot_2", eid, f"{eid}_shot_1"),
    )
    memdb.commit()

    assert _reconcile_terminal_continuity_blocks(eid) == 1
    job = memdb.execute(
        "SELECT status,reason_code FROM jobs WHERE id='j_blocked'",
    ).fetchone()
    assert dict(job) == {"status": "waiting_human", "reason_code": "VIDEO_CHAIN_ANCHOR_BLOCKED"}
    ledger = rebuild_coverage_ledger(eid)
    blocked = next(e for e in ledger.entries if e.shot_no == 2)
    assert blocked.active_job_id is None
    assert "VIDEO_CHAIN_ANCHOR_BLOCKED" in blocked.last_issue_codes


def test_terminal_continuity_wait_does_not_touch_adopted_shot(memdb):
    """补齐的死锁清理也必须跳过已有采用版镜头。"""
    eid, _ = _seed_episode(memdb, 2)
    _add_succeeded_version(
        memdb,
        f"{eid}_shot_2",
        qa={"overall": 0.9, "failure_types": []},
    )
    memdb.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at)
           VALUES('v_adopted_retake',?,2,'p','adopted-retake','queued',1)""",
        (f"{eid}_shot_2",),
    )
    memdb.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at,
               after_shot_id,pipeline_stage
           ) VALUES('j_adopted_retake','video',?,'v_adopted_retake',?,'proj_int','queued',1,1,?,'waiting_continuity_anchor')""",
        (f"{eid}_shot_2", eid, f"{eid}_shot_1"),
    )
    memdb.commit()

    assert _reconcile_terminal_continuity_blocks(eid) == 0
    job = memdb.execute(
        "SELECT status,reason_code FROM jobs WHERE id='j_adopted_retake'",
    ).fetchone()
    assert dict(job) == {"status": "queued", "reason_code": None}


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
    protected = ShotCoverageEntry(
        shot_no=1,
        shot_id=f"{eid}_shot_1",
        grade="C",
        adopted_version_id="v_adopted",
        video_stale=True,
    )
    e1 = ShotCoverageEntry(shot_no=1, shot_id=f"{eid}_shot_1", grade="C")
    e2 = ShotCoverageEntry(shot_no=2, shot_id=f"{eid}_shot_2", grade="C")
    assert _dispatch(protected, episode_id=eid, run_id=None, first=True) is False
    assert calls["n"] == 0
    assert _dispatch(e1, episode_id=eid, run_id=None, first=True) is False
    assert _dispatch(e2, episode_id=eid, run_id=None, first=True) is False
    assert calls["n"] == 2
    # entry 还是未采用的旧快照，但用户已在派发前采用候选：数据库终检必须拒绝。
    _add_succeeded_version(
        memdb,
        f"{eid}_shot_2",
        qa={"overall": 0.9, "failure_types": []},
    )
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


def test_router_keeps_qa_crop_finding_score_only():
    plan = route([
        Issue(
            code="VIDEO_QA_NEEDS_CROP",
            severity=IssueSeverity.WARNING,
            subject="s",
            message="needs crop",
            evidence={"path": "1", "rule_id": "needs_crop"},
        )
    ])
    assert plan.level == "L0"
    assert plan.strategy == "handoff_human"
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
