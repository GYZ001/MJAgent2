"""集级视频补齐 Supervisor（Episode Video Completion Supervisor）。

协调者（reconciler）：维护覆盖台账、Issue 化失败、经 Repair Router 重新入队，
不接管 _run_job / media_pipeline 调度器。
"""
from __future__ import annotations

import asyncio
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.completion_grant import (
    DEFAULT_VIDEO_BUDGET_CAP_CNY,
    VideoCompletionGrant,
    consume_grant,
    get_video_grant,
    validate_video_grant,
    GrantValidationError,
)
from app.db import get_conn, get_setting, new_id, now
from app.evidence import repository as evidence_repository
from app.evidence.media import grade_shot_video, select_best_video_candidate
from app.harness.types import Evaluation, EvidenceArtifact, Issue
from app.media_pipeline.stages import ACTIVE_JOB_STATUSES
from app.video_control import consume_control
from app.video_issues import (
    is_fatal,
    issues_from_enqueue_error,
    issues_from_job_failure,
    issues_from_qa,
    load_persisted_shot_issues,
    persist_shot_issue,
)
from app.video_repair_router import (
    MAX_CHAIN_CASCADE_DEPTH,
    RepairLevel,
    VideoRepairPlan,
    bump_fingerprint_count,
    route as route_video_repair,
    should_cascade,
    state_drift_significant,
)

SupervisorPhase = Literal[
    "CREATED",
    "PREFLIGHT",
    "PLANNING_COVERAGE",
    "DISPATCHING",
    "OBSERVING",
    "EVALUATING",
    "REPAIRING",
    "FINALIZING",
    "SUCCEEDED_COVERED",
    "WAITING_RETRY",
    "PAUSED_EXTERNAL",
    "PAUSED_BUDGET",
    "WAITING_AUTHORIZATION",
    "WAITING_HUMAN",
    "CANCELLED",
]

SUPERVISOR_TICK_INTERVAL_S = 10.0
SUPERVISOR_TICK_MAX_INTERVAL_S = 60.0
MAX_REPAIR_EPOCHS = 8
MIN_ATTEMPTS_PER_SHOT = 2
MAX_ATTEMPTS_PER_SHOT = 6
FIRST_PASS_BUDGET_FRACTION = 0.65
SHOT_BUDGET_MULTIPLIER = 3.0
CHECKPOINT_ARTIFACT_TYPE = "video_supervisor_checkpoint"
REPORT_ARTIFACT_TYPE = "video_coverage_report"
COST_PER_SECOND_CNY = 0.8


class ShotCoverageEntry(BaseModel):
    shot_no: int
    shot_id: str
    grade: Literal["A", "B", "C"] = "C"
    adopted_version_id: str | None = None
    best_version_id: str | None = None
    best_qa_overall: float | None = None
    qa_gain_last_2: float | None = None
    attempts_paid: int = 0
    attempts_budgeted: int = MIN_ATTEMPTS_PER_SHOT
    cost_spent_cny: float = 0.0
    last_issue_codes: list[str] = Field(default_factory=list)
    issue_fingerprint_counts: dict[str, int] = Field(default_factory=dict)
    repair_level: RepairLevel | str = "L0"
    chain_head_shot_no: int | None = None
    chain_position: int = 0
    chain_len: int = 1
    blocked_by_shot_no: int | None = None
    chain_stale: bool = False
    active_job_id: str | None = None
    human_adopted: bool = False
    continuity_degraded: bool = False
    never_attempted: bool = True
    qa_history: list[float] = Field(default_factory=list)
    rebuilt_reference: bool = False
    fatal_repeat_count: int = 0
    fallback_reason: str | None = None
    video_stale: bool = False

    def is_stalled(self) -> bool:
        return any(v >= 2 for v in self.issue_fingerprint_counts.values())


class CoverageLedger(BaseModel):
    episode_id: str
    shots_total: int = 0
    grades: dict[str, int] = Field(default_factory=lambda: {"A": 0, "B": 0, "C": 0})
    coverage_rate: float = 0.0
    fallback_quota: int = 0
    entries: list[ShotCoverageEntry] = Field(default_factory=list)
    cost_spent: float = 0.0

    def count_uncovered(self) -> int:
        return int(self.grades.get("C", 0)) + sum(
            1 for e in self.entries if e.chain_stale or e.video_stale
        )

    def covered_within_quota(self) -> bool:
        if self.shots_total <= 0:
            return False
        if any(e.grade == "C" or e.chain_stale or e.video_stale for e in self.entries):
            return False
        return int(self.grades.get("B", 0)) <= int(self.fallback_quota)

    def has_active_jobs(self) -> bool:
        return any(e.active_job_id for e in self.entries)

    def actionable(self) -> list[ShotCoverageEntry]:
        out = []
        for e in self.entries:
            if e.human_adopted:
                continue
            if e.active_job_id:
                continue
            if e.grade == "A" and not e.chain_stale and not e.video_stale:
                continue
            out.append(e)
        return out

    def exhausted_but_technically_ok(self) -> list[ShotCoverageEntry]:
        """attempt 配额用尽但有技术合格候选、可 B 级兜底。"""
        out = []
        for e in self.entries:
            if e.human_adopted or e.active_job_id:
                continue
            if e.grade in {"A", "B"} and not e.chain_stale and not e.video_stale:
                continue
            if e.attempts_paid < e.attempts_budgeted:
                continue
            if e.best_version_id:
                out.append(e)
        return out


class VideoSupervisorCheckpoint(BaseModel):
    episode_id: str
    run_id: str | None = None
    goal: Literal["complete_episode_video"] = "complete_episode_video"
    phase: SupervisorPhase = "CREATED"
    repair_epoch: int = 0
    tick_no: int = 0
    started_at: float = 0.0
    grant_id: str | None = None
    storyboard_artifact_id: str | None = None
    budget: dict[str, float] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    shot_state: dict[str, dict[str, Any]] = Field(default_factory=dict)
    last_plan: dict[str, Any] | None = None
    outcome: str | None = None
    idle_ticks: int = 0
    tick_interval_s: float = SUPERVISOR_TICK_INTERVAL_S
    first_pass_done: bool = False


def load_latest_checkpoint(episode_id: str) -> VideoSupervisorCheckpoint | None:
    conn = get_conn()
    row = conn.execute(
        """SELECT id, content_json FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC LIMIT 1""",
        (CHECKPOINT_ARTIFACT_TYPE, episode_id),
    ).fetchone()
    if not row:
        return None
    try:
        raw = json.loads(row["content_json"] or "{}")
        return VideoSupervisorCheckpoint.model_validate(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def save_checkpoint(cp: VideoSupervisorCheckpoint, *, run_id: str | None = None) -> str:
    rid = run_id or cp.run_id
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type=CHECKPOINT_ARTIFACT_TYPE,
        scope_type="episode",
        scope_id=cp.episode_id,
        status="validated",
        trust_level="T2",
        content=cp.model_dump(mode="json"),
        contract_version="video-supervisor-1.0.0",
    ))
    evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="video_supervisor",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"phase": cp.phase, "repair_epoch": cp.repair_epoch, "run_id": rid},
        ),
    )
    if rid:
        evidence_repository.append_event(
            rid,
            "VIDEO_SUPERVISOR_CHECKPOINT",
            "info",
            f"Video supervisor checkpoint phase={cp.phase} epoch={cp.repair_epoch}",
            payload={"phase": cp.phase, "coverage": cp.coverage, "tick_no": cp.tick_no},
        )
    return artifact["id"]


def public_checkpoint_projection(cp: VideoSupervisorCheckpoint | None) -> dict[str, Any] | None:
    if cp is None:
        return None
    from app.video_control import control_snapshot
    return {
        "phase": cp.phase,
        "goal": cp.goal,
        "repair_epoch": cp.repair_epoch,
        "tick_no": cp.tick_no,
        "started_at": cp.started_at,
        "grant_id": cp.grant_id,
        "storyboard_artifact_id": cp.storyboard_artifact_id,
        "budget": cp.budget,
        "coverage": cp.coverage,
        "shot_state": cp.shot_state,
        "last_plan": cp.last_plan,
        "outcome": cp.outcome,
        "pending_control": control_snapshot(cp.episode_id),
    }


def _human_adopted(conn, shot_id: str) -> bool:
    row = conn.execute(
        """SELECT id FROM gate_decisions
           WHERE gate_key='video_adoption' AND decision IN ('approve','approve_with_risk')
             AND artifact_id IN (
               SELECT artifact_id FROM shot_versions WHERE shot_id=? AND artifact_id IS NOT NULL
             )
           LIMIT 1""",
        (shot_id,),
    ).fetchone()
    if row:
        return True
    # 兼容：若迁移后存在 payload_json，按 shot_id 兜底匹配
    try:
        row2 = conn.execute(
            """SELECT id FROM gate_decisions
               WHERE gate_key='video_adoption' AND decision IN ('approve','approve_with_risk')
                 AND (payload_json LIKE ? OR payload_json LIKE ?)
               LIMIT 1""",
            (f'%"{shot_id}"%', f"%{shot_id}%"),
        ).fetchone()
        return bool(row2)
    except Exception:  # noqa: BLE001 — 列不存在时忽略
        return False


def _compute_chains(shot_rows: list[Any]) -> dict[str, tuple[int, int, int]]:
    """shot_id → (chain_head_no, chain_position, chain_len)。"""
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    from app.schemas import Shot

    def to_model(row) -> Shot:
        return Shot(
            shot_no=row["shot_no"],
            duration_s=row["duration_s"] or 5,
            shot_size=row["shot_size"] or "中景",
            camera_move=row["camera_move"] or "固定",
            scene_setting=row["scene_setting"] or "",
            characters=json.loads(row["characters"] or "[]"),
            action_desc=row["action_desc"] or "",
            continuity_from_prev=bool(row["continuity_from_prev"]),
            continuity_mode=(row["continuity_mode"] if "continuity_mode" in row.keys() else None),
        )

    models = [to_model(r) for r in shot_rows]
    uses_tail = []
    for i, m in enumerate(models):
        prev = models[i - 1] if i > 0 else None
        mode = derive_continuity_mode(m, prev)
        uses_tail.append(uses_previous_tail_frame(mode) and i > 0)

    # 分段
    result: dict[str, tuple[int, int, int]] = {}
    i = 0
    n = len(shot_rows)
    while i < n:
        head = i
        j = i + 1
        while j < n and uses_tail[j]:
            j += 1
        length = j - head
        for k in range(head, j):
            result[shot_rows[k]["id"]] = (
                int(shot_rows[head]["shot_no"]),
                k - head,
                length,
            )
        i = j
    return result


def _video_stale_for_shot(conn, shot_row, episode_storyboard_id: str | None) -> bool:
    """分镜变更后旧视频失效。"""
    adopted = shot_row["adopted_version_id"]
    if not adopted:
        return False
    # 镜级 storyboard artifact 与 episode 当前不一致
    shot_art = None
    try:
        shot_art = shot_row["storyboard_artifact_id"]
    except (KeyError, IndexError, TypeError):
        shot_art = None
    if episode_storyboard_id and shot_art and shot_art != episode_storyboard_id:
        return True
    ver = conn.execute(
        "SELECT artifact_id FROM shot_versions WHERE id=?", (adopted,)
    ).fetchone()
    if not ver or not ver["artifact_id"]:
        return False
    art = conn.execute(
        "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
        (ver["artifact_id"],),
    ).fetchone()
    if not art:
        return False
    try:
        parents = json.loads(art["parent_artifact_ids_json"] or "[]")
    except (TypeError, ValueError):
        parents = []
    if not episode_storyboard_id:
        return False
    if not parents:
        return False
    return episode_storyboard_id not in parents


def rebuild_coverage_ledger(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint | None = None,
    fallback_quota: int | None = None,
) -> CoverageLedger:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    shot_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    chains = _compute_chains(shot_rows)
    ep_sb = ep["storyboard_artifact_id"] if ep else None

    # 批量读 jobs / versions / costs
    shot_ids = [r["id"] for r in shot_rows]
    active_jobs: dict[str, str] = {}
    if shot_ids:
        placeholders = ",".join("?" * len(shot_ids))
        status_list = tuple(ACTIVE_JOB_STATUSES)
        status_ph = ",".join("?" * len(status_list))
        for row in conn.execute(
            f"""SELECT id, shot_id FROM jobs
                WHERE shot_id IN ({placeholders}) AND kind='video'
                  AND status IN ({status_ph})
                ORDER BY created_at DESC""",
            (*shot_ids, *status_list),
        ).fetchall():
            if row["shot_id"] not in active_jobs:
                active_jobs[row["shot_id"]] = row["id"]

    cost_map: dict[str, float] = {}
    attempts_map: dict[str, int] = {}
    best_map: dict[str, dict[str, Any]] = {}
    if shot_ids:
        placeholders = ",".join("?" * len(shot_ids))
        for row in conn.execute(
            f"""SELECT shot_id, id, qa_json, technical_validation_json, cost_cny,
                       provider_task_id, status, image_inputs, version_no
                FROM shot_versions WHERE shot_id IN ({placeholders})""",
            shot_ids,
        ).fetchall():
            sid = row["shot_id"]
            cost_map[sid] = cost_map.get(sid, 0.0) + float(row["cost_cny"] or 0)
            if row["provider_task_id"] or row["status"] in {"succeeded", "failed", "running", "queued"}:
                # 产生过 provider 任务或进入执行的版本计为 paid attempt
                if row["provider_task_id"] or row["status"] == "succeeded":
                    attempts_map[sid] = attempts_map.get(sid, 0) + 1
            if row["status"] != "succeeded":
                continue
            qa = json.loads(row["qa_json"] or "{}")
            try:
                score = float(qa.get("overall")) if qa.get("overall") is not None else -1.0
            except (TypeError, ValueError):
                score = -1.0
            cur = best_map.get(sid)
            if cur is None or score >= cur["score"]:
                best_map[sid] = {
                    "id": row["id"],
                    "score": score,
                    "qa": qa,
                    "technical": json.loads(row["technical_validation_json"] or "{}"),
                    "image_inputs": row["image_inputs"],
                }

    quota = fallback_quota
    if quota is None and cp and cp.coverage:
        quota = int(cp.coverage.get("fallback_quota") or 0)
    if quota is None:
        quota = max(1, int(math.ceil(len(shot_rows) * 0.2)))

    entries: list[ShotCoverageEntry] = []
    grades = {"A": 0, "B": 0, "C": 0}
    total_cost = 0.0
    prev_state = (cp.shot_state if cp else {}) or {}

    for row in shot_rows:
        sid = row["id"]
        saved = prev_state.get(str(row["shot_no"])) or prev_state.get(sid) or {}
        chain_head, chain_pos, chain_len = chains.get(sid, (row["shot_no"], 0, 1))
        best = best_map.get(sid)
        graded = grade_shot_video(
            sid,
            technical=(best or {}).get("technical"),
            qa=(best or {}).get("qa"),
            version_row={
                "id": (best or {}).get("id"),
                "image_inputs": (best or {}).get("image_inputs"),
                "technical_validation_json": json.dumps((best or {}).get("technical") or {}),
                "qa_json": json.dumps((best or {}).get("qa") or {}),
            } if best else None,
            continuity_degraded=bool(saved.get("continuity_degraded")),
        )
        grade = graded["grade"]
        stale = _video_stale_for_shot(conn, row, ep_sb)
        if stale and grade in {"A", "B"}:
            grade = "C"
        grades[grade] = grades.get(grade, 0) + 1
        cost = float(cost_map.get(sid, 0.0))
        total_cost += cost
        qa_history = list(saved.get("qa_history") or [])
        if graded["qa_overall"] is not None:
            if not qa_history or qa_history[-1] != graded["qa_overall"]:
                qa_history = (qa_history + [float(graded["qa_overall"])])[-8:]
        gain = None
        if len(qa_history) >= 2:
            gain = qa_history[-1] - qa_history[-2]

        persisted = load_persisted_shot_issues(sid)
        last_codes = list(saved.get("last_issue_codes") or [])
        if persisted:
            last_codes = [i.code for i in persisted]

        # 若有失败 job 无成功版，补充 issue
        if grade == "C" and not best:
            fail_job = conn.execute(
                """SELECT * FROM jobs WHERE shot_id=? AND kind='video'
                   AND status IN ('failed','paused_budget','waiting_human')
                   ORDER BY created_at DESC LIMIT 1""",
                (sid,),
            ).fetchone()
            if fail_job:
                fail_ver = None
                if fail_job["version_id"]:
                    fail_ver = conn.execute(
                        "SELECT * FROM shot_versions WHERE id=?", (fail_job["version_id"],)
                    ).fetchone()
                job_issues = issues_from_job_failure(
                    dict(fail_job), dict(fail_ver) if fail_ver else None,
                    shot_id=sid, shot_no=row["shot_no"],
                )
                if job_issues:
                    last_codes = [i.code for i in job_issues]

        entry = ShotCoverageEntry(
            shot_no=int(row["shot_no"]),
            shot_id=sid,
            grade=grade,  # type: ignore[arg-type]
            adopted_version_id=row["adopted_version_id"],
            best_version_id=(best or {}).get("id"),
            best_qa_overall=graded["qa_overall"],
            qa_gain_last_2=gain,
            attempts_paid=int(saved.get("attempts_paid") or attempts_map.get(sid, 0)),
            attempts_budgeted=int(saved.get("attempts_budgeted") or MIN_ATTEMPTS_PER_SHOT),
            cost_spent_cny=cost,
            last_issue_codes=last_codes,
            issue_fingerprint_counts=dict(saved.get("issue_fingerprint_counts") or {}),
            repair_level=saved.get("repair_level") or "L0",
            chain_head_shot_no=chain_head,
            chain_position=chain_pos,
            chain_len=chain_len,
            chain_stale=bool(saved.get("chain_stale")),
            active_job_id=active_jobs.get(sid),
            human_adopted=_human_adopted(conn, sid),
            continuity_degraded=bool(saved.get("continuity_degraded") or graded.get("continuity_degraded")),
            never_attempted=attempts_map.get(sid, 0) == 0 and not saved.get("attempts_paid"),
            qa_history=qa_history,
            rebuilt_reference=bool(saved.get("rebuilt_reference")),
            fatal_repeat_count=int(saved.get("fatal_repeat_count") or 0),
            fallback_reason=graded.get("fallback_reason") or saved.get("fallback_reason"),
            video_stale=stale,
        )
        # blocked_by
        if entry.active_job_id:
            job = conn.execute(
                "SELECT after_shot_id, pipeline_stage FROM jobs WHERE id=?",
                (entry.active_job_id,),
            ).fetchone()
            if job and job["after_shot_id"] and (job["pipeline_stage"] or "").endswith("waiting_continuity"):
                prev = conn.execute(
                    "SELECT shot_no FROM shots WHERE id=?", (job["after_shot_id"],)
                ).fetchone()
                if prev:
                    entry.blocked_by_shot_no = int(prev["shot_no"])
        entries.append(entry)

    covered = grades["A"] + grades["B"]
    total = len(entries)
    return CoverageLedger(
        episode_id=episode_id,
        shots_total=total,
        grades=grades,
        coverage_rate=(covered / total) if total else 0.0,
        fallback_quota=int(quota),
        entries=entries,
        cost_spent=total_cost,
    )


def attempts_for(entry: ShotCoverageEntry, ledger: CoverageLedger, *, budget_cap_cny: float) -> int:
    duration = 5.0
    conn = get_conn()
    row = conn.execute(
        "SELECT duration_s, episode_id FROM shots WHERE id=?", (entry.shot_id,)
    ).fetchone()
    episode_id = None
    if row:
        duration = float(row["duration_s"] or 5)
        episode_id = row["episode_id"]
    try:
        from app.video_cost_model import predict_shot_completion_cost
        pred = predict_shot_completion_cost(
            duration, episode_id=episode_id, grade=entry.grade,
        )
        est = float(pred["unit_cny"])
    except Exception:  # noqa: BLE001
        est = duration * COST_PER_SECOND_CNY + 1.0
    remaining = max(0.0, budget_cap_cny - ledger.cost_spent)
    uncovered = max(1, ledger.count_uncovered())
    affordable = int(remaining / (uncovered * max(est, 0.5))) if est > 0 else 0
    base = MIN_ATTEMPTS_PER_SHOT
    if entry.chain_position == 0 and entry.chain_len > 1:
        base += 1
    if entry.grade == "C":
        base += 1
    return max(MIN_ATTEMPTS_PER_SHOT, min(MAX_ATTEMPTS_PER_SHOT, min(base + affordable, MAX_ATTEMPTS_PER_SHOT)))


def _budget_view(cp: VideoSupervisorCheckpoint, ledger: CoverageLedger) -> dict[str, float]:
    cap = float((cp.budget or {}).get("cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY)
    return {
        "cap_cny": cap,
        "spent_cny": float(ledger.cost_spent),
        "first_pass_soft_cap_cny": cap * FIRST_PASS_BUDGET_FRACTION,
        "per_shot_cap_cny": (cap / max(1, ledger.shots_total)) * SHOT_BUDGET_MULTIPLIER,
        "remaining_cny": max(0.0, cap - ledger.cost_spent),
    }


def _merge_shot_state(cp: VideoSupervisorCheckpoint, ledger: CoverageLedger) -> None:
    state: dict[str, dict[str, Any]] = {}
    for e in ledger.entries:
        state[str(e.shot_no)] = {
            "grade": e.grade,
            "shot_id": e.shot_id,
            "attempts_paid": e.attempts_paid,
            "attempts_budgeted": e.attempts_budgeted,
            "repair_level": e.repair_level,
            "issue_fingerprint_counts": e.issue_fingerprint_counts,
            "qa_history": e.qa_history,
            "continuity_degraded": e.continuity_degraded,
            "chain_stale": e.chain_stale,
            "rebuilt_reference": e.rebuilt_reference,
            "fatal_repeat_count": e.fatal_repeat_count,
            "last_issue_codes": e.last_issue_codes,
            "fallback_reason": e.fallback_reason,
            "video_stale": e.video_stale,
        }
    cp.shot_state = state
    cp.coverage = {
        "A": ledger.grades.get("A", 0),
        "B": ledger.grades.get("B", 0),
        "C": ledger.grades.get("C", 0),
        "total": ledger.shots_total,
        "fallback_quota": ledger.fallback_quota,
        "coverage_rate": ledger.coverage_rate,
    }
    cp.budget = _budget_view(cp, ledger)


def _after_shot_id(episode_id: str, shot_no: int, *, degrade: bool = False) -> str | None:
    if degrade or shot_no <= 1:
        return None
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    from app.schemas import Shot

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? AND shot_no IN (?, ?) ORDER BY shot_no",
        (episode_id, shot_no - 1, shot_no),
    ).fetchall()
    if len(rows) < 2:
        return None
    prev_row, cur_row = rows[0], rows[1]

    def to_model(r):
        return Shot(
            shot_no=r["shot_no"], duration_s=r["duration_s"] or 5,
            shot_size=r["shot_size"] or "中景", camera_move=r["camera_move"] or "固定",
            scene_setting=r["scene_setting"] or "",
            characters=json.loads(r["characters"] or "[]"),
            action_desc=r["action_desc"] or "",
            continuity_from_prev=bool(r["continuity_from_prev"]),
        )

    if uses_previous_tail_frame(derive_continuity_mode(to_model(cur_row), to_model(prev_row))):
        return prev_row["id"]
    return None


def _dispatch(
    entry: ShotCoverageEntry,
    *,
    episode_id: str,
    run_id: str | None,
    plan: VideoRepairPlan | None = None,
    first: bool = False,
) -> bool:
    """入队；失败 Issue 化。返回是否产生新进展（非 reused）。"""
    from app import worker
    from app.compiler import CompileError

    kwargs: dict[str, Any] = {}
    degrade = bool(plan and plan.degrade_chain)
    if not first:
        kwargs["reroll"] = True
    if plan:
        if plan.extra_negative:
            kwargs["extra_negative"] = plan.extra_negative
        if plan.critique:
            kwargs["critique"] = plan.critique
        if plan.prompt_aggressive:
            # 软化：由 enqueue 侧 prompt_override 处理；此处标记进 image_inputs via worker 扩展
            kwargs["prompt_override"] = None  # 让 compile 走默认，aggressive 在 meta
    after = _after_shot_id(episode_id, entry.shot_no, degrade=degrade)
    kwargs["after_shot_id"] = after
    kwargs["auto_retake_count"] = entry.attempts_paid
    kwargs["supervisor_meta"] = {
        "supervisor_run_id": run_id,
        "supervisor_repair_level": (plan.level if plan else entry.repair_level),
        "supervisor_strategy": (plan.strategy if plan else "first_attempt"),
        "supervisor_issue_codes": (plan.issue_codes if plan else entry.last_issue_codes),
        "continuity_degraded": degrade or entry.continuity_degraded,
        "rebuild_reference": bool(plan and plan.rebuild_reference),
    }

    try:
        # rebuild_reference：清参考图目录标记，让 enqueue 重建
        if plan and plan.rebuild_reference:
            conn = get_conn()
            conn.execute(
                """UPDATE shot_versions SET image_inputs=json_set(
                     COALESCE(image_inputs, '{}'), '$.reference_images', json('[]'),
                     '$.force_rebuild_reference', 1)
                   WHERE shot_id=? AND status='succeeded'""",
                (entry.shot_id,),
            )
            # sqlite json_set 可能不可用：容错
            try:
                conn.commit()
            except Exception:  # noqa: BLE001
                conn.rollback()
        result = worker.enqueue_shot(entry.shot_id, **{
            k: v for k, v in kwargs.items() if k != "supervisor_meta"
        })
        # 把 supervisor meta 写入新建 version
        if result.get("version_id") and kwargs.get("supervisor_meta"):
            _patch_version_supervisor_meta(result["version_id"], kwargs["supervisor_meta"])
    except (CompileError, ValueError) as exc:
        issues = issues_from_enqueue_error(exc, shot_id=entry.shot_id, shot_no=entry.shot_no)
        persist_shot_issue(
            episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
            issues=issues, source="supervisor_enqueue",
        )
        entry.last_issue_codes = [i.code for i in issues]
        for issue in issues:
            entry.issue_fingerprint_counts = bump_fingerprint_count(
                entry.issue_fingerprint_counts, issue.fingerprint
            )
        return False
    except Exception as exc:  # noqa: BLE001
        issues = issues_from_enqueue_error(exc, shot_id=entry.shot_id, shot_no=entry.shot_no)
        persist_shot_issue(
            episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
            issues=issues, source="supervisor_enqueue",
        )
        entry.last_issue_codes = [i.code for i in issues]
        return False

    if result.get("paused_budget"):
        issues = issues_from_enqueue_error(
            ValueError("预算不足，任务暂停"), shot_id=entry.shot_id, shot_no=entry.shot_no,
        )
        persist_shot_issue(
            episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
            issues=issues, source="supervisor_budget",
        )
        entry.last_issue_codes = [i.code for i in issues]
        return False
    if result.get("reused"):
        return False
    if degrade:
        entry.continuity_degraded = True
    if plan and plan.rebuild_reference:
        entry.rebuilt_reference = True
    return True


def _patch_version_supervisor_meta(version_id: str, meta: dict[str, Any]) -> None:
    conn = get_conn()
    row = conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?", (version_id,)
    ).fetchone()
    if not row:
        return
    try:
        data = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update(meta)
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id=?",
        (json.dumps(data, ensure_ascii=False), version_id),
    )
    conn.commit()


def _collect_issues(entry: ShotCoverageEntry) -> list[Issue]:
    issues = load_persisted_shot_issues(entry.shot_id)
    conn = get_conn()
    if entry.best_version_id:
        row = conn.execute(
            "SELECT * FROM shot_versions WHERE id=?", (entry.best_version_id,)
        ).fetchone()
        if row:
            qa = json.loads(row["qa_json"] or "{}")
            technical = json.loads(row["technical_validation_json"] or "{}")
            issues.extend(issues_from_qa(
                qa, technical, shot_id=entry.shot_id,
                version_id=row["id"], shot_no=entry.shot_no,
            ))
    if not issues and entry.last_issue_codes:
        issues = [
            Issue(
                code=code,
                severity="blocker",  # type: ignore[arg-type]
                subject=entry.shot_id,
                message=code,
                evidence={"shot_no": entry.shot_no, "path": str(entry.shot_no), "rule_id": code},
            )
            for code in entry.last_issue_codes
        ]
    return issues


def _apply_cascade(entry: ShotCoverageEntry, ledger: CoverageLedger, cp: VideoSupervisorCheckpoint) -> list[int]:
    """标记下游 chain_stale；超深度则 degrade。"""
    cascaded: list[int] = []
    observed = None
    if entry.best_version_id:
        conn = get_conn()
        row = conn.execute(
            "SELECT qa_json FROM shot_versions WHERE id=?", (entry.best_version_id,)
        ).fetchone()
        if row:
            qa = json.loads(row["qa_json"] or "{}")
            observed = qa.get("observed_state_out")
    planned = None
    conn = get_conn()
    shot = conn.execute("SELECT state_out FROM shots WHERE id=?", (entry.shot_id,)).fetchone()
    if shot:
        planned = shot["state_out"]
    drift = state_drift_significant(planned, observed)

    for other in ledger.entries:
        if other.shot_no <= entry.shot_no:
            continue
        if other.chain_head_shot_no != entry.chain_head_shot_no:
            continue
        if other.chain_position - entry.chain_position > MAX_CHAIN_CASCADE_DEPTH:
            # 超深度：降链而非重烧
            other.continuity_degraded = True
            cp.shot_state.setdefault(str(other.shot_no), {})["continuity_degraded"] = True
            continue
        if should_cascade(entry, other, state_drift=drift):
            other.chain_stale = True
            cascaded.append(other.shot_no)
    return cascaded


def _adopt_fallback(entry: ShotCoverageEntry, *, episode_id: str, run_id: str | None) -> bool:
    result = select_best_video_candidate(entry.shot_id, force_best=True)
    if not result:
        return False
    entry.grade = result.get("grade") or "B"  # type: ignore[assignment]
    if entry.grade == "A":
        entry.grade = "B"
    entry.fallback_reason = result.get("fallback_reason") or result.get("reason")
    entry.adopted_version_id = result.get("version_id")
    if run_id:
        evidence_repository.append_event(
            run_id, "VIDEO_FALLBACK_ADOPTED", "warning",
            f"第 {entry.shot_no} 镜 B 级兜底采纳",
            payload={
                "shot_no": entry.shot_no,
                "version_id": entry.adopted_version_id,
                "fallback_reason": entry.fallback_reason,
            },
        )
    return True


def _amend_storyboard(
    entry: ShotCoverageEntry,
    *,
    grant: VideoCompletionGrant,
    plan: VideoRepairPlan | None = None,
    run_id: str | None = None,
) -> bool:
    """L5：微调分镜（需授权）。允许：调 duration_s、删不可渲染细节、拆镜。

    禁止改 dialogues/narration/characters/key_lines。改后跑确定性校验，失败回滚。
    """
    if not grant.allow_storyboard_edit:
        return False
    from app.completion_grant import refresh_video_grant_storyboard_artifact
    from app.schemas import Shot, Storyboard
    from app.validators import normalize_action_desc, validate_storyboard

    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (entry.shot_id,)).fetchone()
    if not row:
        return False
    episode_id = row["episode_id"]
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    all_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()

    def _row_to_shot(r) -> Shot:
        return Shot(
            shot_no=int(r["shot_no"]),
            duration_s=int(r["duration_s"] or 5),
            shot_size=r["shot_size"] or "中景",
            camera_move=r["camera_move"] or "固定",
            scene_setting=r["scene_setting"] or "",
            characters=json.loads(r["characters"] or "[]"),
            action_desc=r["action_desc"] or "",
            first_frame_desc=(r["first_frame_desc"] if "first_frame_desc" in r.keys() else "") or "",
            last_frame_desc=(r["last_frame_desc"] if "last_frame_desc" in r.keys() else "") or "",
            source_excerpt=(r["source_excerpt"] if "source_excerpt" in r.keys() else "") or "",
            narration=r["narration"] if "narration" in r.keys() else None,
            dialogues=json.loads(r["dialogues"] or "[]"),
            continuity_from_prev=bool(r["continuity_from_prev"]),
        )

    backup = {
        "duration_s": row["duration_s"],
        "action_desc": row["action_desc"],
    }
    codes = set((plan.issue_codes if plan else entry.last_issue_codes) or [])
    changed = False

    # 1) 时长合同
    if "VIDEO_DURATION_CONTRACT" in codes or not codes:
        cur = int(row["duration_s"] or 5)
        new_dur = 5 if cur > 7 else 8
        new_dur = max(5, min(10, new_dur))
        if new_dur == cur:
            new_dur = 6 if cur != 6 else 7
        if new_dur != cur:
            conn.execute("UPDATE shots SET duration_s=? WHERE id=?", (new_dur, entry.shot_id))
            changed = True

    # 2) 删除不可渲染细节：压缩过长 action_desc / 去掉显式快切标记
    action = normalize_action_desc(row["action_desc"] or "") or (row["action_desc"] or "")
    original_action = action
    # 去掉括号内后期指示
    import re
    action = re.sub(r"[（(][^）)]{0,40}(?:后期|裁切|字幕|特效|配音)[^）)]*[）)]", "", action)
    action = re.sub(r"(?:快切|闪回|蒙太奇|分屏)[，,]?", "", action)
    if len(action) > 80:
        action = action[:78].rstrip("，,。；; ") + "。"
    if action.strip() and action.strip() != (original_action or "").strip():
        conn.execute("UPDATE shots SET action_desc=? WHERE id=?", (action.strip(), entry.shot_id))
        changed = True

    # 3) 拆镜：仅当 preflight 容量类且 action 明显含两个主动作时，拆成两镜
    split_done = False
    if "VIDEO_PREFLIGHT_BLOCKED" in codes and "拆" not in (row["action_desc"] or ""):
        text = (action or row["action_desc"] or "")
        # 简单启发：出现两个「然后/接着」以上视为可拆
        if text.count("然后") + text.count("接着") + text.count("随后") >= 2:
            parts = re.split(r"(?:然后|接着|随后)", text, maxsplit=1)
            if len(parts) == 2 and len(parts[0].strip()) >= 12 and len(parts[1].strip()) >= 12:
                shot_no = int(row["shot_no"])
                # 后续镜号 +1
                later = [r for r in all_rows if int(r["shot_no"]) > shot_no]
                for r in reversed(later):
                    conn.execute(
                        "UPDATE shots SET shot_no=? WHERE id=?",
                        (int(r["shot_no"]) + 1, r["id"]),
                    )
                new_shot_id = new_id("shot")
                half = max(5, min(10, int((row["duration_s"] or 8) // 2) or 5))
                conn.execute(
                    "UPDATE shots SET action_desc=?, duration_s=?, last_frame_desc=? WHERE id=?",
                    (parts[0].strip().rstrip("，,") + "。", half,
                     (row["first_frame_desc"] if "first_frame_desc" in row.keys() else "") or "",
                     entry.shot_id),
                )
                conn.execute(
                    """INSERT INTO shots(
                        id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting,
                        characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt,
                        narration, dialogues, continuity_from_prev, transition
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (
                        new_shot_id, episode_id, shot_no + 1, half,
                        row["shot_size"] or "中景", row["camera_move"] or "固定",
                        row["scene_setting"] or "", row["characters"] or "[]",
                        parts[1].strip().rstrip("，,") + "。",
                        (row["last_frame_desc"] if "last_frame_desc" in row.keys() else "") or "",
                        (row["last_frame_desc"] if "last_frame_desc" in row.keys() else "") or "",
                        (row["source_excerpt"] if "source_excerpt" in row.keys() else "") or "",
                        row["narration"], row["dialogues"] or "[]",
                        row["transition"] if "transition" in row.keys() else "硬切",
                    ),
                )
                split_done = True
                changed = True

    if not changed:
        return False
    conn.commit()

    # 校验整集；失败回滚
    try:
        refreshed = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
        ).fetchall()
        board = Storyboard(
            episode_no=int(ep["episode_no"]),
            shots=[_row_to_shot(r) for r in refreshed],
        )
        errors = validate_storyboard(board)
        hard = [e for e in (errors or []) if "硬下限" in e or "必须" in e or "禁止" in e]
        if hard:
            raise ValueError(";".join(hard[:3]))
    except Exception:  # noqa: BLE001
        # 回滚本镜 + 拆镜
        conn.execute(
            "UPDATE shots SET duration_s=?, action_desc=? WHERE id=?",
            (backup["duration_s"], backup["action_desc"], entry.shot_id),
        )
        if split_done:
            # 删除 shot_no+1 的新镜并恢复后续编号——简化：删掉刚插入的、把后续 -1
            conn.execute(
                "DELETE FROM shots WHERE episode_id=? AND shot_no=? AND id!=?",
                (episode_id, int(row["shot_no"]) + 1, entry.shot_id),
            )
            # 不完美恢复，但避免脏数据扩散：重新从 backup 后的编号不强制
        conn.commit()
        return False

    # 写新 storyboard artifact 指纹并刷新 grant
    try:
        from app.evidence import repository as evidence_repository
        from app.harness.types import EvidenceArtifact
        art = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content={
                "amended_by": "video_supervisor_l5",
                "shot_id": entry.shot_id,
                "shot_no": entry.shot_no,
            },
            contract_version="storyboard-amend-1.0.0",
        ))
        conn.execute(
            "UPDATE episodes SET storyboard_artifact_id=? WHERE id=?",
            (art["id"], episode_id),
        )
        conn.execute(
            "UPDATE shots SET storyboard_artifact_id=? WHERE id=?",
            (art["id"], entry.shot_id),
        )
        conn.commit()
        refresh_video_grant_storyboard_artifact(grant.grant_id, art["id"])
        if run_id:
            evidence_repository.append_event(
                run_id, "VIDEO_STORYBOARD_AMENDED", "info",
                f"第 {entry.shot_no} 镜 L5 微调分镜",
                payload={"shot_id": entry.shot_id, "artifact_id": art["id"], "split": split_done},
            )
    except Exception:  # noqa: BLE001
        pass
    return True


# 兼容旧名
def _amend_storyboard_duration(entry: ShotCoverageEntry, *, grant: VideoCompletionGrant) -> bool:
    return _amend_storyboard(entry, grant=grant)


def _try_auto_crop(entry: ShotCoverageEntry, *, run_id: str | None) -> bool:
    if not entry.best_version_id:
        return False
    from app.video_crop import try_auto_crop_shot_version
    result = try_auto_crop_shot_version(entry.best_version_id)
    if not result or not result.get("ok"):
        return False
    if run_id:
        evidence_repository.append_event(
            run_id, "VIDEO_REPAIR_PLAN_SELECTED", "info",
            f"第 {entry.shot_no} 镜自动裁切",
            payload={"shot_no": entry.shot_no, "version_id": result.get("version_id")},
        )
    return True


def _finalize_covered(
    cp: VideoSupervisorCheckpoint, ledger: CoverageLedger, *, run_id: str | None
) -> VideoSupervisorCheckpoint:
    cp.phase = "FINALIZING"
    save_checkpoint(cp, run_id=run_id)
    report = {
        "episode_id": cp.episode_id,
        "grades": ledger.grades,
        "fallback_quota": ledger.fallback_quota,
        "cost_spent_cny": ledger.cost_spent,
        "budget_cap_cny": (cp.budget or {}).get("cap_cny"),
        "shots": [
            {
                "shot_no": e.shot_no,
                "shot_id": e.shot_id,
                "grade": e.grade,
                "adopted_version_id": e.adopted_version_id or e.best_version_id,
                "qa_overall": e.best_qa_overall,
                "cost_spent_cny": e.cost_spent_cny,
                "repair_level": e.repair_level,
                "continuity_degraded": e.continuity_degraded,
                "fallback_reason": e.fallback_reason,
                "attempts_paid": e.attempts_paid,
                "attempts_budgeted": e.attempts_budgeted,
                "last_issue_codes": e.last_issue_codes,
                "repair_history": {
                    "level": e.repair_level,
                    "issue_fingerprint_counts": e.issue_fingerprint_counts,
                    "qa_history": e.qa_history,
                    "rebuilt_reference": e.rebuilt_reference,
                    "fatal_repeat_count": e.fatal_repeat_count,
                },
            }
            for e in ledger.entries
        ],
        "last_plan": cp.last_plan,
        "repair_epoch": cp.repair_epoch,
    }
    # 幂等：已有报告则不重复
    existing = get_conn().execute(
        """SELECT id FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('validated','approved')
           ORDER BY created_at DESC LIMIT 1""",
        (REPORT_ARTIFACT_TYPE, cp.episode_id),
    ).fetchone()
    if not existing:
        art = evidence_repository.create_artifact(EvidenceArtifact(
            type=REPORT_ARTIFACT_TYPE,
            scope_type="episode",
            scope_id=cp.episode_id,
            status="validated",
            trust_level="T2",
            content=report,
            contract_version="video-coverage-1.0.0",
        ))
        evidence_repository.create_evaluation(
            art["id"],
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="video_coverage_report",
                evaluator_version="1.0.0",
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={"grades": ledger.grades},
            ),
        )
    cp.phase = "SUCCEEDED_COVERED"
    cp.outcome = "SUCCEEDED_COVERED"
    if cp.grant_id:
        try:
            consume_grant(cp.grant_id)
        except Exception:  # noqa: BLE001
            pass
    save_checkpoint(cp, run_id=run_id)
    if run_id:
        evidence_repository.append_event(
            run_id, "VIDEO_COVERAGE_COMPLETED", "info",
            f"全片覆盖完成 A={ledger.grades.get('A')} B={ledger.grades.get('B')}",
            payload=report,
        )
    return cp


def _assert_storyboard_version(cp: VideoSupervisorCheckpoint) -> bool:
    if not cp.grant_id or not cp.storyboard_artifact_id:
        return True
    conn = get_conn()
    ep = conn.execute(
        "SELECT storyboard_artifact_id FROM episodes WHERE id=?", (cp.episode_id,)
    ).fetchone()
    current = (ep["storyboard_artifact_id"] if ep else None) or ""
    return current == (cp.storyboard_artifact_id or "")


async def run_video_completion_supervisor(
    episode_id: str,
    *,
    resume: bool = True,
    grant_id: str | None = None,
    run_id: str | None = None,
    budget_cap_cny: float | None = None,
    wall_clock_cap_s: float | None = None,
    allow_fallback_adopt: bool = True,
    max_fallback_shots: int | None = None,
    allow_storyboard_edit: bool = False,
) -> VideoSupervisorCheckpoint:
    """主循环：周期性 reconciler。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"剧集不存在：{episode_id}")

    cp = load_latest_checkpoint(episode_id) if resume else None
    if cp is None:
        shots_total = conn.execute(
            "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
        ).fetchone()["c"]
        from app.completion_grant import default_max_fallback_shots
        quota = max_fallback_shots if max_fallback_shots is not None else default_max_fallback_shots(int(shots_total or 0))
        cap = float(budget_cap_cny if budget_cap_cny is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY)
        cp = VideoSupervisorCheckpoint(
            episode_id=episode_id,
            run_id=run_id,
            phase="PREFLIGHT",
            started_at=now(),
            grant_id=grant_id,
            storyboard_artifact_id=ep["storyboard_artifact_id"],
            budget={
                "cap_cny": cap,
                "spent_cny": 0.0,
                "first_pass_soft_cap_cny": cap * FIRST_PASS_BUDGET_FRACTION,
                "per_shot_cap_cny": (cap / max(1, int(shots_total or 1))) * SHOT_BUDGET_MULTIPLIER,
            },
            coverage={"fallback_quota": quota, "A": 0, "B": 0, "C": int(shots_total or 0), "total": int(shots_total or 0)},
        )
    else:
        if run_id:
            cp.run_id = run_id
        if grant_id:
            cp.grant_id = grant_id
        if budget_cap_cny is not None:
            cp.budget["cap_cny"] = float(budget_cap_cny)

    if cp.phase in {"SUCCEEDED_COVERED", "CANCELLED"}:
        return cp

    grant: VideoCompletionGrant | None = None
    if cp.grant_id:
        try:
            grant = validate_video_grant(
                cp.grant_id,
                episode_id=episode_id,
                storyboard_artifact_id=ep["storyboard_artifact_id"],
            )
            cp.budget["cap_cny"] = float(grant.budget_cap_cny)
            cp.coverage["fallback_quota"] = int(grant.max_fallback_shots)
            allow_fallback_adopt = grant.allow_fallback_adopt
            allow_storyboard_edit = grant.allow_storyboard_edit
            wall_clock_cap_s = float(grant.wall_clock_cap_s)
        except GrantValidationError as exc:
            cp.phase = "WAITING_AUTHORIZATION"
            cp.outcome = exc.code
            save_checkpoint(cp, run_id=run_id)
            return cp

    if run_id:
        evidence_repository.append_event(
            run_id, "VIDEO_SUPERVISOR_STARTED", "info",
            "视频补齐 Supervisor 启动",
            payload={"resume": resume, "grant_id": cp.grant_id},
        )

    wall_cap = float(wall_clock_cap_s if wall_clock_cap_s is not None else (cp.budget.get("wall_clock_cap_s") or 4 * 3600))
    if grant:
        wall_cap = float(grant.wall_clock_cap_s)

    while True:
        action = consume_control(episode_id)
        if action == "pause":
            cp.phase = "PAUSED_EXTERNAL"
            save_checkpoint(cp, run_id=run_id)
            if run_id:
                evidence_repository.append_event(
                    run_id, "VIDEO_SUPERVISOR_PAUSED", "info", "用户暂停",
                )
            return cp
        if action == "handoff":
            cp.phase = "WAITING_HUMAN"
            save_checkpoint(cp, run_id=run_id)
            if run_id:
                evidence_repository.append_event(
                    run_id, "VIDEO_SUPERVISOR_HANDOFF", "warning", "转交人工",
                )
            return cp
        if action == "retry_now":
            cp.tick_interval_s = SUPERVISOR_TICK_INTERVAL_S
            cp.idle_ticks = 0

        if not _assert_storyboard_version(cp):
            cp.phase = "WAITING_AUTHORIZATION"
            cp.outcome = "VIDEO_STORYBOARD_CHANGED"
            save_checkpoint(cp, run_id=run_id)
            return cp

        # 刷新 grant 预算（可能被抬额）
        if cp.grant_id:
            g = get_video_grant(cp.grant_id)
            if g and g.revoked_at:
                cp.phase = "CANCELLED"
                save_checkpoint(cp, run_id=run_id)
                return cp
            if g:
                cp.budget["cap_cny"] = float(g.budget_cap_cny)
                wall_cap = float(g.wall_clock_cap_s)
                allow_fallback_adopt = g.allow_fallback_adopt
                allow_storyboard_edit = g.allow_storyboard_edit
                grant = g

        cp.tick_no += 1
        cp.phase = "PLANNING_COVERAGE"
        ledger = rebuild_coverage_ledger(
            episode_id, cp=cp,
            fallback_quota=int(cp.coverage.get("fallback_quota") or 0),
        )
        # 动态分配 attempt budget
        cap = float(cp.budget.get("cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY)
        for e in ledger.entries:
            e.attempts_budgeted = attempts_for(e, ledger, budget_cap_cny=cap)
        _merge_shot_state(cp, ledger)
        save_checkpoint(cp, run_id=run_id)

        if ledger.covered_within_quota():
            return _finalize_covered(cp, ledger, run_id=run_id)

        spent = float(ledger.cost_spent)
        if spent >= cap:
            cp.phase = "WAITING_AUTHORIZATION"
            cp.outcome = "VIDEO_BUDGET_EXHAUSTED"
            save_checkpoint(cp, run_id=run_id)
            if run_id:
                evidence_repository.append_event(
                    run_id, "VIDEO_BUDGET_WALL_REACHED", "warning",
                    f"预算墙：已花 ¥{spent:.1f} / ¥{cap:.1f}",
                )
            return cp
        if now() - (cp.started_at or now()) >= wall_cap:
            cp.phase = "WAITING_AUTHORIZATION"
            cp.outcome = "VIDEO_WALL_CLOCK_EXCEEDED"
            save_checkpoint(cp, run_id=run_id)
            return cp
        if cp.repair_epoch > MAX_REPAIR_EPOCHS:
            cp.phase = "WAITING_HUMAN"
            cp.outcome = "repair_epochs_exhausted"
            save_checkpoint(cp, run_id=run_id)
            return cp

        if ledger.has_active_jobs() and not ledger.actionable():
            cp.phase = "OBSERVING"
            save_checkpoint(cp, run_id=run_id)
            await asyncio.sleep(cp.tick_interval_s)
            continue

        cp.phase = "EVALUATING"
        progressed = False
        soft_cap = cap * FIRST_PASS_BUDGET_FRACTION
        per_shot_cap = (cap / max(1, ledger.shots_total)) * SHOT_BUDGET_MULTIPLIER

        for entry in ledger.actionable():
            # 单镜上限
            if entry.cost_spent_cny >= per_shot_cap and entry.grade == "C":
                if allow_fallback_adopt and entry.best_version_id:
                    if _adopt_fallback(entry, episode_id=episode_id, run_id=run_id):
                        progressed = True
                continue

            if entry.never_attempted:
                # 首轮软预算
                if not cp.first_pass_done and spent >= soft_cap:
                    continue
                cp.phase = "DISPATCHING"
                if _dispatch(entry, episode_id=episode_id, run_id=run_id, first=True):
                    progressed = True
                    spent = float(rebuild_coverage_ledger(episode_id, cp=cp).cost_spent)
                continue

            issues = _collect_issues(entry)
            # 更新 fatal 计数
            if any(is_fatal(i) for i in issues):
                entry.fatal_repeat_count += 1

            plan = route_video_repair(
                issues,
                entry=entry,
                budget=_budget_view(cp, ledger),
                fingerprint_counts=entry.issue_fingerprint_counts,
                current_level=entry.repair_level,  # type: ignore[arg-type]
                allow_storyboard_edit=allow_storyboard_edit,
                qa_history=entry.qa_history,
                rebuilt_reference=entry.rebuilt_reference,
                fatal_repeat_count=entry.fatal_repeat_count,
            )
            entry.repair_level = plan.level
            cp.last_plan = {
                "shot_no": entry.shot_no,
                "level": plan.level,
                "strategy": plan.strategy,
                "reason": plan.reason,
                "issue_codes": plan.issue_codes,
            }
            if plan.fingerprint:
                entry.issue_fingerprint_counts = bump_fingerprint_count(
                    entry.issue_fingerprint_counts, plan.fingerprint
                )
            if plan.pause_state:
                cp.phase = plan.pause_state  # type: ignore[assignment]
                cp.outcome = plan.reason
                _merge_shot_state(cp, ledger)
                save_checkpoint(cp, run_id=run_id)
                if run_id:
                    evidence_repository.append_event(
                        run_id, "VIDEO_REPAIR_PLAN_SELECTED", "warning",
                        plan.reason, payload=cp.last_plan,
                    )
                return cp

            if plan.strategy == "handoff_human":
                continue

            if entry.attempts_paid >= entry.attempts_budgeted and plan.is_paid:
                continue

            if plan.strategy == "amend_storyboard":
                if grant and _amend_storyboard(entry, grant=grant, plan=plan, run_id=run_id):
                    progressed = True
                else:
                    cp.phase = "WAITING_AUTHORIZATION"
                    save_checkpoint(cp, run_id=run_id)
                    return cp
                continue

            if plan.strategy == "auto_crop":
                if _try_auto_crop(entry, run_id=run_id):
                    progressed = True
                    continue
                # 裁切失败则降为定向重抽
                plan = plan.model_copy(update={
                    "strategy": "retake_directed",
                    "is_paid": True,
                    "reason": (plan.reason or "") + "；自动裁切失败，改定向重抽",
                })

            if plan.strategy == "requeue_no_charge":
                # 重置活动/失败 job 的 retry_count 并重排
                job = conn.execute(
                    """SELECT id FROM jobs WHERE shot_id=? AND kind='video'
                       AND status IN ('failed','waiting_retry','paused_budget')
                       ORDER BY created_at DESC LIMIT 1""",
                    (entry.shot_id,),
                ).fetchone()
                if job:
                    conn.execute(
                        "UPDATE jobs SET status='queued', retry_count=0, error=NULL, updated_at=? WHERE id=?",
                        (now(), job["id"]),
                    )
                    conn.commit()
                    progressed = True
                    continue
                # 无旧 job → 走新入队
            cp.phase = "REPAIRING"
            if run_id:
                evidence_repository.append_event(
                    run_id, "VIDEO_REPAIR_PLAN_SELECTED", "info",
                    plan.reason, payload=cp.last_plan,
                )
            if _dispatch(entry, episode_id=episode_id, run_id=run_id, plan=plan):
                progressed = True
                cascaded = _apply_cascade(entry, ledger, cp)
                if cascaded and run_id:
                    evidence_repository.append_event(
                        run_id, "VIDEO_CHAIN_INVALIDATED", "info",
                        f"第 {entry.shot_no} 镜级联 {cascaded}",
                        payload={"shot_no": entry.shot_no, "cascade": cascaded},
                    )
                if plan.degrade_chain and run_id:
                    evidence_repository.append_event(
                        run_id, "VIDEO_CHAIN_DEGRADED", "warning",
                        f"第 {entry.shot_no} 镜降链",
                        payload={"shot_no": entry.shot_no},
                    )

        # B 级兜底
        if allow_fallback_adopt:
            # 刷新 ledger 状态到 cp
            _merge_shot_state(cp, ledger)
            ledger2 = rebuild_coverage_ledger(episode_id, cp=cp)
            for entry in ledger2.exhausted_but_technically_ok():
                b_count = sum(1 for e in ledger2.entries if e.grade == "B")
                if b_count >= ledger2.fallback_quota and entry.grade != "B":
                    # 超配额时挑最低分继续修，不在此兜底新的
                    continue
                if _adopt_fallback(entry, episode_id=episode_id, run_id=run_id):
                    progressed = True

        # 首轮：若所有 never_attempted 都处理过或软预算触顶
        if not any(e.never_attempted for e in ledger.entries):
            cp.first_pass_done = True

        _merge_shot_state(cp, ledger)
        if not progressed and not ledger.has_active_jobs():
            cp.repair_epoch += 1
            cp.idle_ticks += 1
            cp.tick_interval_s = min(
                SUPERVISOR_TICK_MAX_INTERVAL_S,
                cp.tick_interval_s * 1.5,
            )
        else:
            if progressed:
                cp.idle_ticks = 0
                cp.tick_interval_s = SUPERVISOR_TICK_INTERVAL_S
        save_checkpoint(cp, run_id=run_id)
        await asyncio.sleep(cp.tick_interval_s)


def recover_video_completion_runs() -> int:
    """服务重启后恢复未完成的视频补齐 Supervisor。"""
    from app import task_registry
    from app.orchestration.engine import WorkflowRecorder, fingerprint

    conn = get_conn()
    # 确保列存在
    for stmt in (
        "ALTER TABLE episodes ADD COLUMN active_video_run_id TEXT",
        "ALTER TABLE episodes ADD COLUMN video_completion_mode TEXT NOT NULL DEFAULT 'quick'",
    ):
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception:  # noqa: BLE001
            pass

    rows = conn.execute(
        """SELECT id, project_id, active_video_run_id, video_completion_mode, storyboard_artifact_id
           FROM episodes WHERE video_completion_mode='complete'"""
    ).fetchall()
    resumed = 0
    for row in rows:
        episode_id = row["id"]
        if task_registry.active("video_completion", episode_id):
            continue
        cp = load_latest_checkpoint(episode_id)
        if cp is None:
            continue
        if cp.phase in {"SUCCEEDED_COVERED", "CANCELLED", "WAITING_AUTHORIZATION", "WAITING_HUMAN"}:
            continue
        # 用户取消的 run 不恢复
        cancelled = conn.execute(
            """SELECT id FROM workflow_runs
               WHERE workflow_type='episode_video_completion' AND scope_type='episode'
                 AND scope_id=? AND status='CANCELLED'
               ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        latest = conn.execute(
            """SELECT status FROM workflow_runs
               WHERE workflow_type='episode_video_completion' AND scope_type='episode'
                 AND scope_id=? ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        if cancelled and latest and latest["status"] == "CANCELLED":
            continue
        if cp.grant_id:
            try:
                validate_video_grant(
                    cp.grant_id,
                    episode_id=episode_id,
                    storyboard_artifact_id=row["storyboard_artifact_id"],
                )
            except GrantValidationError:
                cp.phase = "WAITING_AUTHORIZATION"
                save_checkpoint(cp)
                continue

        recorder = WorkflowRecorder.create(
            workflow_type="episode_video_completion",
            scope_type="episode",
            scope_id=episode_id,
            input_fingerprint=fingerprint(row["storyboard_artifact_id"], cp.grant_id),
            requested_by="system",
            trigger_type="resume",
            policy_snapshot={"supervisor": "video_completion", "resume": True},
            parent_run_id=row["active_video_run_id"],
        )

        async def _task(eid=episode_id, rid=recorder.run_id, gid=cp.grant_id):
            try:
                result = await run_video_completion_supervisor(
                    eid, resume=True, grant_id=gid, run_id=rid,
                )
                if result.phase == "SUCCEEDED_COVERED":
                    recorder.succeed(result.outcome or "SUCCEEDED_COVERED")
                elif result.phase in {"WAITING_AUTHORIZATION", "WAITING_HUMAN", "PAUSED_EXTERNAL", "PAUSED_BUDGET"}:
                    recorder.partial(result.outcome or result.phase)
                elif result.phase == "CANCELLED":
                    recorder.cancel()
                else:
                    recorder.partial(result.phase)
            except asyncio.CancelledError:
                recorder.cancel()
                raise
            except Exception as exc:
                recorder.fail(exc)
                raise

        task_registry.spawn(
            "video_completion", episode_id, _task(), project_id=row["project_id"],
        )
        conn.execute(
            "UPDATE episodes SET active_video_run_id=? WHERE id=?",
            (recorder.run_id, episode_id),
        )
        conn.commit()
        resumed += 1
    return resumed
