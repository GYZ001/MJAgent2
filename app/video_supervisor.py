"""集级视频补齐 Supervisor（Episode Video Completion Supervisor）。

协调者（reconciler）：维护覆盖台账、Issue 化失败、经 Repair Router 重新入队，
不接管 _run_job / media_pipeline 调度器。
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.completion_grant import (
    DEFAULT_VIDEO_BUDGET_CAP_CNY,
    VideoCompletionGrant,
    bind_video_grant_generation_plan,
    consume_grant,
    get_video_grant,
    validate_video_grant,
    GrantValidationError,
)
from app.continuity import classify_video_hard_failures
from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.evidence.media import (
    grade_shot_video,
    select_best_video_candidate,
    video_candidate_selection_score,
)
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.media_pipeline.stages import ACTIVE_JOB_STATUSES
from app.schemas import Shot, Storyboard, extract_json
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
    "PREPARING_ASSETS",
    "PLANNING_COVERAGE",
    "DISPATCHING",
    "OBSERVING",
    "EVALUATING",
    "REPAIRING",
    "FINALIZING",
    "DEADLINE_CLOSING",
    "SUCCEEDED_COVERED",
    "COMPLETED_DEADLINE_FALLBACK",
    "PARTIAL_NO_USABLE_CANDIDATE",
    "RECOVERING_CONTROL_PLANE",
    "FAILED_CLOSED",
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
CONTROL_PLANE_MAX_RECOVERIES = 3
SUPERVISOR_HEARTBEAT_STALE_S = 60.0
ASSET_PREP_HEARTBEAT_INTERVAL_S = 20.0
DISPATCH_HEARTBEAT_INTERVAL_S = 20.0
TERMINAL_SUPERVISOR_PHASES = {
    "SUCCEEDED_COVERED",
    "COMPLETED_DEADLINE_FALLBACK",
    "PARTIAL_NO_USABLE_CANDIDATE",
    "FAILED_CLOSED",
    "CANCELLED",
}

VIDEO_COMPLETION_BUSINESS_STAGES = (
    {
        "key": "authorization",
        "name": "确认补齐范围与生成授权",
        "description": "确认待补镜头、预算、截止时间和允许的自动修复范围",
    },
    {
        "key": "plan",
        "name": "制定全片视频生成方案",
        "description": "为每个镜头决定参考图、首帧或首尾帧等生成方式与衔接依赖",
    },
    {
        "key": "assets",
        "name": "核对人物、场景与连续性素材",
        "description": "检查人物定妆、场景参考和前后镜头衔接素材是否可用",
    },
    {
        "key": "coverage",
        "name": "盘点全片缺口与生成顺序",
        "description": "统计已采用和待补镜头，并按依赖关系安排生成顺序",
    },
    {
        "key": "shots",
        "name": "逐镜生成视频",
        "description": "逐镜准备输入、调用视频模型、下载并保存候选结果",
    },
    {
        "key": "quality",
        "name": "检查质量并自动修复",
        "description": "执行技术检查和内容质检，对未通过镜头定向重试或转人工",
    },
    {
        "key": "finalize",
        "name": "验收全片覆盖并收口",
        "description": "为每个镜头选择可用版本，确认全片无缺镜后结束任务",
    },
)

_PHASE_LABELS: dict[str, str] = {
    "CREATED": "任务已创建",
    "PREFLIGHT": "核对授权与基础条件",
    "PREPARING_ASSETS": "准备人物与场景参考素材",
    "PLANNING_COVERAGE": "盘点待补镜头",
    "DISPATCHING": "派发逐镜生成任务",
    "OBSERVING": "等待镜头生成结果",
    "EVALUATING": "检查镜头质量",
    "REPAIRING": "自动修复未通过镜头",
    "FINALIZING": "验收全片覆盖",
    "DEADLINE_CLOSING": "按截止时间收口",
    "SUCCEEDED_COVERED": "全片视频已补齐",
    "COMPLETED_DEADLINE_FALLBACK": "已按截止时间完成收口",
    "PARTIAL_NO_USABLE_CANDIDATE": "仍有镜头缺少可用视频",
    "RECOVERING_CONTROL_PLANE": "恢复全片调度状态",
    "FAILED_CLOSED": "任务已安全停止",
    "WAITING_RETRY": "等待自动重试",
    "PAUSED_EXTERNAL": "外部服务暂停",
    "PAUSED_BUDGET": "预算暂停",
    "WAITING_AUTHORIZATION": "等待补充授权",
    "WAITING_HUMAN": "等待人工处理",
    "CANCELLED": "任务已取消",
}


def phase_label(phase: str | None) -> str:
    value = str(phase or "")
    return _PHASE_LABELS.get(value, f"未知业务阶段（{value or '未记录'}）")

_REFERENCE_ASSET_PREP_LOCKS: dict[str, asyncio.Lock] = {}


class ShotCoverageEntry(BaseModel):
    shot_no: int
    shot_id: str
    grade: Literal["A", "B", "C"] = "C"
    adopted_version_id: str | None = None
    best_version_id: str | None = None
    best_qa_overall: float | None = None
    qa_gain_last_2: float | None = None
    attempts_paid: int = 0
    attempts_dispatched: int = 0
    attempts_budgeted: int = MIN_ATTEMPTS_PER_SHOT
    no_charge_requeues: int = 0
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
        return sum(1 for entry in self.entries if not entry.adopted_version_id)

    def covered_within_quota(self) -> bool:
        if self.shots_total <= 0:
            return False
        # “补齐”只填空位。adopted 是用户/Supervisor 已作出的最终选择；
        # QA、stale 与 fallback 配额只能形成风险提示，不能撤销采用并重烧。
        return all(entry.adopted_version_id for entry in self.entries)

    def has_active_jobs(self) -> bool:
        # 补齐 Supervisor 只观察未采用镜头；已有采用版的手工重抽不属于本次补齐。
        return any(e.active_job_id for e in self.entries if not e.adopted_version_id)

    def actionable(self) -> list[ShotCoverageEntry]:
        out = []
        for e in self.entries:
            if e.adopted_version_id:
                continue
            if e.active_job_id:
                continue
            # 技术有效候选一旦存在，就必须先采用并收口；QA 等级、连续性评分
            # 和内容风险不能把它重新送入付费修复循环。
            if e.best_version_id:
                continue
            out.append(e)
        return out

    def exhausted_but_technically_ok(self) -> list[ShotCoverageEntry]:
        """attempt 配额用尽但有技术合格候选，必须 best-effort 收口。"""
        out = []
        for e in self.entries:
            if e.adopted_version_id or e.active_job_id:
                continue
            if e.grade == "A" and not e.chain_stale and not e.video_stale:
                continue
            if max(e.attempts_paid, e.attempts_dispatched) < e.attempts_budgeted:
                continue
            if e.best_version_id:
                out.append(e)
        return out


def _adopted_video_is_usable(adopted_row: Any) -> bool:
    if adopted_row is None or adopted_row["status"] != "succeeded":
        return False
    try:
        metadata = json.loads(adopted_row["image_inputs"] or "{}")
        technical = json.loads(
            adopted_row["technical_validation_json"] or "{}"
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if metadata.get("delivery_fallback") or technical.get("passed") is not True:
        return False
    path = Path(str(adopted_row["video_path"] or ""))
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with path.open("rb") as handle:
            return b"ftyp" in handle.read(32)
    except OSError:
        return False


class VideoSupervisorCheckpoint(BaseModel):
    episode_id: str
    run_id: str | None = None
    goal: Literal["complete_episode_video"] = "complete_episode_video"
    phase: SupervisorPhase = "CREATED"
    repair_epoch: int = 0
    tick_no: int = 0
    started_at: float = 0.0
    deadline_at: float | None = None
    last_heartbeat_at: float | None = None
    dispatch_fenced_at: float | None = None
    closeout_started_at: float | None = None
    finished_at: float | None = None
    terminal_reason: str | None = None
    quality_target_missed: bool = False
    missing_shots: list[int] = Field(default_factory=list)
    closeout_adoptions: list[dict[str, Any]] = Field(default_factory=list)
    control_plane_recoveries: int = 0
    grant_id: str | None = None
    storyboard_artifact_id: str | None = None
    episode_video_plan_id: str | None = None
    episode_video_plan_revision: int | None = None
    video_plan_release_hash: str | None = None
    capability_snapshot_id: str | None = None
    budget: dict[str, float] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    shot_state: dict[str, dict[str, Any]] = Field(default_factory=dict)
    last_plan: dict[str, Any] | None = None
    outcome: str | None = None
    idle_ticks: int = 0
    tick_interval_s: float = SUPERVISOR_TICK_INTERVAL_S
    first_pass_done: bool = False


class StoryboardRepairAffectedAuthority(BaseModel):
    """Narrative graph identities whose ownership/readability may be affected."""

    action_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    action_phase_ids: list[str] = Field(default_factory=list)
    experience_intent_ids: list[str] = Field(default_factory=list)


class StoryboardRepairProposal(BaseModel):
    """Typed semantic proposal; it is evidence, never an official mutation."""

    proposal_id: str = Field(min_length=1)
    base_shot_id: str = Field(min_length=1)
    operation: Literal["replace", "split"]
    reason: str = Field(min_length=1)
    expected_total_duration_s: int = Field(gt=0)
    affected_authority: StoryboardRepairAffectedAuthority = Field(
        default_factory=StoryboardRepairAffectedAuthority
    )
    candidate_shots: list[Shot] = Field(min_length=1, max_length=2)


def _verify_supervisor_paid_authority(
    cp: VideoSupervisorCheckpoint,
    *,
    stage: str,
) -> VideoCompletionGrant | None:
    """One fail-closed verifier shared by every Supervisor paid boundary."""
    del stage  # retained in the signature for boundary-specific audit callers
    conn = get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (cp.episode_id,),
    ).fetchone()
    if episode is None:
        raise GrantValidationError("GRANT_SCOPE_MISSING", "视频补齐分集已不存在")
    current_storyboard_id = str(episode["storyboard_artifact_id"] or "")
    if not cp.grant_id:
        # Explicit compatibility boundary for historical plan-null episodes.
        # Any durable screenplay authority makes an unauthorised paid run a
        # hard failure even if the mutable projection was stripped.
        from app.production.screenplay_authority import (
            episode_requires_immutable_screenplay_authority,
            resolve_downstream_screenplay,
        )

        immutable_required = episode_requires_immutable_screenplay_authority(
            episode,
            conn=conn,
        )
        try:
            context = resolve_downstream_screenplay(cp.episode_id, conn=conn)
        except ValueError as exc:
            if immutable_required:
                raise GrantValidationError(
                    "RELEASE_QUALIFICATION_INVALID",
                    f"当前剧本发布权威无法解析：{exc}",
                ) from exc
            context = None
        if immutable_required or (
            context is not None and context.immutable_authority_required
        ):
            raise GrantValidationError(
                "VIDEO_COMPLETION_GRANT_REQUIRED",
                "带耐久发布权威的分集在付费阶段前必须有补齐授权",
            )
        return None
    grant = validate_video_grant(
        cp.grant_id,
        episode_id=cp.episode_id,
        storyboard_artifact_id=current_storyboard_id,
    )
    plan_binding = dict(grant.release_qualification.get("generation_plan") or {})
    if plan_binding.get("applicable"):
        expected = {
            "episode_video_plan_id": grant.episode_video_plan_id,
            "episode_video_plan_revision": grant.episode_video_plan_revision,
            "video_plan_release_hash": grant.video_plan_release_hash,
            "capability_snapshot_id": grant.capability_snapshot_id,
        }
        actual = {
            "episode_video_plan_id": cp.episode_video_plan_id,
            "episode_video_plan_revision": cp.episode_video_plan_revision,
            "video_plan_release_hash": cp.video_plan_release_hash,
            "capability_snapshot_id": cp.capability_snapshot_id,
        }
        if actual != expected:
            raise GrantValidationError(
                "CHECKPOINT_PLAN_BINDING_CHANGED",
                "Supervisor checkpoint 与授权的当前视频计划不一致",
            )
    return grant


def _supervisor_checks_can_use_worker_thread() -> bool:
    """A private in-memory SQLite connection must stay on its owner thread."""
    try:
        database_rows = get_conn().execute("PRAGMA database_list").fetchall()
        return any(str(row[2] or "").strip() for row in database_rows)
    except Exception:
        return False


async def _verify_supervisor_paid_authority_async(
    cp: VideoSupervisorCheckpoint,
    *,
    stage: str,
) -> VideoCompletionGrant | None:
    """Keep modern grant verification off-loop; legacy plan-null stays local."""
    if not cp.grant_id:
        return _verify_supervisor_paid_authority(cp, stage=stage)
    if not _supervisor_checks_can_use_worker_thread():
        return _verify_supervisor_paid_authority(cp, stage=stage)
    return await asyncio.to_thread(
        _verify_supervisor_paid_authority,
        cp,
        stage=stage,
    )


async def _verify_episode_plan_current_async(plan) -> bool:
    from app.video_plan import verify_episode_plan_is_current

    if not _supervisor_checks_can_use_worker_thread():
        return verify_episode_plan_is_current(
            plan,
            conn=get_conn(),
            mark_stale=False,
        )
    return await asyncio.to_thread(
        verify_episode_plan_is_current,
        plan,
        mark_stale=False,
    )


async def _ensure_supervisor_video_plan(
    cp: VideoSupervisorCheckpoint,
) -> VideoCompletionGrant | None:
    """Generate/validate and bind the plan before any paid asset or video call."""
    # #region debug-point C-D:supervisor-plan-probe
    def _debug_video_plan(hypothesis_id: str, message: str, data: dict[str, Any]) -> None:
        import urllib.request

        url = "http://127.0.0.1:7778/event"
        session_id = "video-generation-noop"
        try:
            with open(".dbg/video-generation-noop.env", encoding="utf-8") as env_file:
                for line in env_file:
                    if line.startswith("DEBUG_SERVER_URL="):
                        url = line.strip().split("=", 1)[1]
                    elif line.startswith("DEBUG_SESSION_ID="):
                        session_id = line.strip().split("=", 1)[1]
            payload = {
                "sessionId": session_id,
                "runId": "post-fix",
                "hypothesisId": hypothesis_id,
                "location": "app/video_supervisor.py:_ensure_supervisor_video_plan",
                "msg": f"[DEBUG] {message}",
                "data": data,
                "ts": int(time.time() * 1000),
            }
            request = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(request, timeout=0.5).read()
        except Exception:
            pass
    # #endregion

    checkpoint_binding = (
        cp.episode_video_plan_id,
        cp.episode_video_plan_revision,
        cp.video_plan_release_hash,
        cp.capability_snapshot_id,
    )
    if cp.grant_id and not any(value is not None for value in checkpoint_binding):
        # New and pre-migration checkpoints acquire their binding exactly once
        # from the content-addressed grant before paid work begins.
        if _supervisor_checks_can_use_worker_thread():
            grant = await asyncio.to_thread(
                validate_video_grant,
                cp.grant_id,
                episode_id=cp.episode_id,
                storyboard_artifact_id=cp.storyboard_artifact_id,
            )
        else:
            grant = validate_video_grant(
                cp.grant_id,
                episode_id=cp.episode_id,
                storyboard_artifact_id=cp.storyboard_artifact_id,
            )
    else:
        grant = await _verify_supervisor_paid_authority_async(
            cp,
            stage="video_plan_preflight",
        )
    if grant is None:
        return None
    from app.video_plan import (
        VideoPlanValidationError,
        generate_episode_plan,
        load_latest_plan,
        video_plan_provider_selection_is_current,
    )

    conn = get_conn()
    plan = load_latest_plan(cp.episode_id, conn=conn)
    # #region debug-point C-D:supervisor-plan-loaded
    _debug_video_plan("C", "Supervisor loaded video plan", {
        "episodeId": cp.episode_id,
        "runId": cp.run_id,
        "checkpointPhase": cp.phase,
        "checkpointBinding": checkpoint_binding,
        "grantId": grant.grant_id,
        "grantPlanId": grant.episode_video_plan_id,
        "grantPlanRevision": grant.episode_video_plan_revision,
        "planId": plan.episode_video_plan_id if plan else None,
        "planRevision": plan.plan_revision if plan else None,
        "planStatus": plan.status if plan else None,
        "planBlockers": plan.blockers if plan else None,
        "capabilitySnapshotId": plan.capability_snapshot_id if plan else None,
    })
    # #endregion
    if (
        plan is None
        or plan.status != "valid"
        or not await _verify_episode_plan_current_async(plan)
        or not video_plan_provider_selection_is_current(plan, conn=conn)
    ):
        # Planning may itself call an external model, so recheck the release
        # immediately before it.  A pending plan slot is the only permitted
        # mutation of the grant after this point.
        await _verify_supervisor_paid_authority_async(
            cp,
            stage="video_plan_generation",
        )
        try:
            plan = await generate_episode_plan(
                cp.episode_id,
                force=plan is not None,
                conn=conn,
            )
        except (ValueError, VideoPlanValidationError) as exc:
            # #region debug-point C:supervisor-plan-generation-error
            _debug_video_plan("C", "Video plan generation failed", {
                "episodeId": cp.episode_id,
                "runId": cp.run_id,
                "errorType": type(exc).__name__,
                "errorMessage": str(exc),
                "issues": getattr(exc, "issues", None),
            })
            # #endregion
            raise GrantValidationError("VIDEO_PLAN_INVALID", str(exc)) from exc
    if (
        plan.status != "valid"
        or not await _verify_episode_plan_current_async(plan)
        or not video_plan_provider_selection_is_current(plan, conn=conn)
    ):
        # #region debug-point C-D:supervisor-plan-postcheck-invalid
        _debug_video_plan("C", "Generated video plan failed current-plan checks", {
            "episodeId": cp.episode_id,
            "runId": cp.run_id,
            "planId": plan.episode_video_plan_id,
            "planRevision": plan.plan_revision,
            "planStatus": plan.status,
            "planBlockers": plan.blockers,
            "capabilitySnapshotId": plan.capability_snapshot_id,
        })
        # #endregion
        raise GrantValidationError(
            "VIDEO_PLAN_INVALID",
            "Supervisor 启动前未取得当前有效的整集视频计划",
        )
    if not grant.episode_video_plan_id:
        grant = bind_video_grant_generation_plan(
            grant.grant_id,
            episode_id=cp.episode_id,
            storyboard_artifact_id=cp.storyboard_artifact_id,
        )
    if (
        grant.episode_video_plan_id != plan.episode_video_plan_id
        or grant.episode_video_plan_revision != int(plan.plan_revision)
        or grant.video_plan_release_hash != plan.release_qualification_hash
        or grant.capability_snapshot_id != plan.capability_snapshot_id
    ):
        raise GrantValidationError(
            "VIDEO_PLAN_BINDING_CHANGED",
            "当前有效视频计划与用户授权的计划不一致",
        )
    checkpoint_binding = (
        cp.episode_video_plan_id,
        cp.episode_video_plan_revision,
        cp.video_plan_release_hash,
        cp.capability_snapshot_id,
    )
    if any(value is not None for value in checkpoint_binding) and checkpoint_binding != (
        grant.episode_video_plan_id,
        grant.episode_video_plan_revision,
        grant.video_plan_release_hash,
        grant.capability_snapshot_id,
    ):
        raise GrantValidationError(
            "CHECKPOINT_PLAN_BINDING_CHANGED",
            "Supervisor checkpoint 已绑定另一个视频计划 revision",
        )
    cp.episode_video_plan_id = grant.episode_video_plan_id
    cp.episode_video_plan_revision = grant.episode_video_plan_revision
    cp.video_plan_release_hash = grant.video_plan_release_hash
    cp.capability_snapshot_id = grant.capability_snapshot_id
    return await _verify_supervisor_paid_authority_async(
        cp,
        stage="video_plan_bound",
    )


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
    cp.last_heartbeat_at = now()
    if rid:
        db = get_conn()
        db.execute(
            """UPDATE workflow_runs
               SET updated_at=?, deadline_at=COALESCE(deadline_at, ?)
               WHERE id=? AND status IN (
                   'CREATED','RUNNING','WAITING_RETRY','WAITING_HUMAN',
                   'WAITING_AUTHORIZATION','PAUSED_BUDGET','PAUSED_EXTERNAL'
               )""",
            (cp.last_heartbeat_at, cp.deadline_at, rid),
        )
        db.commit()

    existing = get_conn().execute(
        """SELECT id, content_json, created_at FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC LIMIT 1""",
        (CHECKPOINT_ARTIFACT_TYPE, cp.episode_id),
    ).fetchone()
    durable_every_time = TERMINAL_SUPERVISOR_PHASES | {
        "DEADLINE_CLOSING", "RECOVERING_CONTROL_PLANE", "FAILED_CLOSED",
        "WAITING_AUTHORIZATION", "WAITING_HUMAN", "PAUSED_EXTERNAL", "PAUSED_BUDGET",
    }
    if existing and cp.phase not in durable_every_time:
        try:
            previous = json.loads(existing["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            previous = {}
        loop_phases = {"PLANNING_COVERAGE", "OBSERVING", "EVALUATING"}
        previous_phase = previous.get("phase")
        phase_equivalent = (
            previous_phase == cp.phase
            or (previous_phase in loop_phases and cp.phase in loop_phases)
        )
        semantic_same = all(previous.get(key) == value for key, value in {
            "coverage": cp.coverage,
            "shot_state": cp.shot_state,
            "last_plan": cp.last_plan,
            "outcome": cp.outcome,
            "dispatch_fenced_at": cp.dispatch_fenced_at,
            "grant_id": cp.grant_id,
            "storyboard_artifact_id": cp.storyboard_artifact_id,
            "episode_video_plan_id": cp.episode_video_plan_id,
            "episode_video_plan_revision": cp.episode_video_plan_revision,
            "video_plan_release_hash": cp.video_plan_release_hash,
            "capability_snapshot_id": cp.capability_snapshot_id,
        }.items())
        if (
            phase_equivalent
            and semantic_same
            and cp.last_heartbeat_at - float(existing["created_at"] or 0) < 60
        ):
            return str(existing["id"])
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


def _refresh_supervisor_heartbeat(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
) -> bool:
    """Refresh liveness without writing a full checkpoint artifact."""
    rid = run_id or cp.run_id
    stamp = now()
    if not rid:
        cp.last_heartbeat_at = stamp
        return True
    conn = get_conn()
    cursor = conn.execute(
        """UPDATE workflow_runs
              SET updated_at=?, deadline_at=COALESCE(deadline_at, ?)
            WHERE id=? AND finished_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM episodes e
                   WHERE e.id=? AND e.active_video_run_id=workflow_runs.id
                     AND e.video_completion_mode='complete'
              )""",
        (stamp, cp.deadline_at, rid, cp.episode_id),
    )
    conn.commit()
    if cursor.rowcount != 1:
        return False
    cp.last_heartbeat_at = stamp
    return True


def public_checkpoint_projection(cp: VideoSupervisorCheckpoint | None) -> dict[str, Any] | None:
    if cp is None:
        return None
    from app import task_registry
    from app.video_control import control_snapshot
    conn = get_conn()
    task_running = task_registry.active("video_completion", cp.episode_id)
    run = conn.execute(
        "SELECT status, deadline_at, started_at, finished_at, updated_at FROM workflow_runs WHERE id=?",
        (cp.run_id,),
    ).fetchone() if cp.run_id else None
    active_media_jobs = int(conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE episode_id=? AND kind='video'
             AND status IN ('queued','running','waiting_provider','waiting_retry','waiting')""",
        (cp.episode_id,),
    ).fetchone()["c"])
    abandoned_provider_jobs = int(conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE episode_id=? AND kind='video' AND status='abandoned'""",
        (cp.episode_id,),
    ).fetchone()["c"])
    persisted_heartbeat = max(
        float(cp.last_heartbeat_at or 0),
        float(run["updated_at"] or 0) if run else 0,
    ) or None
    heartbeat_stale = bool(
        cp.phase not in TERMINAL_SUPERVISOR_PHASES
        and persisted_heartbeat
        and now() - persisted_heartbeat > SUPERVISOR_HEARTBEAT_STALE_S
    )
    return {
        "phase": cp.phase,
        "run_id": cp.run_id,
        "goal": cp.goal,
        "repair_epoch": cp.repair_epoch,
        "tick_no": cp.tick_no,
        "started_at": cp.started_at or (run["started_at"] if run else None),
        "deadline_at": cp.deadline_at or (run["deadline_at"] if run else None),
        "last_heartbeat_at": persisted_heartbeat,
        "dispatch_fenced_at": cp.dispatch_fenced_at,
        "closeout_started_at": cp.closeout_started_at,
        "finished_at": cp.finished_at or (run["finished_at"] if run else None),
        "terminal_reason": cp.terminal_reason,
        "quality_target_missed": cp.quality_target_missed,
        "missing_shots": cp.missing_shots,
        "closeout_adoptions": cp.closeout_adoptions,
        "grant_id": cp.grant_id,
        "storyboard_artifact_id": cp.storyboard_artifact_id,
        "budget": cp.budget,
        "coverage": cp.coverage,
        "shot_state": cp.shot_state,
        "last_plan": cp.last_plan,
        "outcome": cp.outcome,
        "pending_control": control_snapshot(cp.episode_id),
        "task_running": task_running,
        "running": task_running,
        "preserve_adopted": True,
        "run_status": run["status"] if run else None,
        "active_media_jobs": active_media_jobs,
        "abandoned_provider_jobs": abandoned_provider_jobs,
        "heartbeat_stale": heartbeat_stale,
        "closeout": {
            "started_at": cp.closeout_started_at,
            "adoptions": cp.closeout_adoptions,
            "missing_shots": cp.missing_shots,
            "quality_target_missed": cp.quality_target_missed,
        },
    }


def _human_adopted(conn, shot_id: str) -> bool:
    """是否存在人工采用 Gate。``gate_decisions`` 表无 payload_json 列，勿查询该列。"""
    row = conn.execute(
        """SELECT id FROM gate_decisions
           WHERE gate_key='video_adoption' AND decision IN ('approve','approve_with_risk')
             AND artifact_id IN (
               SELECT artifact_id FROM shot_versions WHERE shot_id=? AND artifact_id IS NOT NULL
             )
           LIMIT 1""",
        (shot_id,),
    ).fetchone()
    return bool(row)


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
        episode_art = conn.execute(
            "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
            (episode_storyboard_id,),
        ).fetchone()
        try:
            episode_parents = json.loads(
                episode_art["parent_artifact_ids_json"] or "[]"
            ) if episode_art else []
        except (TypeError, ValueError):
            episode_parents = []
        # Current storyboard aggregates directly parent their per-shot artifacts.
        # A shot artifact inside the approved aggregate is current, not stale.
        if shot_art not in episode_parents:
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
    valid_storyboard_parents = {episode_storyboard_id}
    if shot_art:
        valid_storyboard_parents.add(shot_art)
    return not any(parent in valid_storyboard_parents for parent in parents)


def _reconcile_terminal_continuity_blocks(
    episode_id: str,
    *,
    run_id: str | None = None,
) -> int:
    """把不可能再获得上游尾帧的等待任务转成可路由 Issue。

    queued + waiting_continuity 过去会永远被当成 active，Supervisor 因而永远
    不会进入 L3 降链。这里只在上游既无技术候选、也无活动任务时解除死锁。
    """
    from app.media_pipeline import stages as media_stages

    conn = get_conn()
    rows = conn.execute(
        """SELECT j.id, j.shot_id, j.version_id, j.after_shot_id, s.shot_no
           FROM jobs j JOIN shots s ON s.id=j.shot_id
           WHERE j.episode_id=? AND j.kind='video'
             AND s.adopted_version_id IS NULL
             AND j.status IN ('queued','waiting','waiting_retry')
             AND j.after_shot_id IS NOT NULL
             AND j.pipeline_stage=?
             AND (? IS NULL OR j.owner_run_id=?)""",
        (episode_id, media_stages.STAGE_WAITING_CONTINUITY, run_id, run_id),
    ).fetchall()
    changed = 0
    for row in rows:
        upstream_rows = conn.execute(
            """SELECT technical_validation_json FROM shot_versions
               WHERE shot_id=? AND status='succeeded'""",
            (row["after_shot_id"],),
        ).fetchall()
        upstream_candidate = False
        for candidate in upstream_rows:
            try:
                if json.loads(candidate["technical_validation_json"] or "{}").get("passed"):
                    upstream_candidate = True
                    break
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        upstream_active = conn.execute(
            """SELECT 1 FROM jobs
               WHERE shot_id=? AND kind='video'
                 AND status IN ('queued','running','waiting_provider','waiting_retry','waiting')
                 AND (? IS NULL OR owner_run_id=?)
               LIMIT 1""",
            (row["after_shot_id"], run_id, run_id),
        ).fetchone()
        if upstream_candidate or upstream_active:
            continue
        message = "上一镜无可用尾帧且已无活动任务；解除连续性等待，交由 Supervisor 降链修复"
        cursor = conn.execute(
            """UPDATE jobs
               SET status='waiting_human', pipeline_stage=?, stage_status='blocked',
                   reason_code='VIDEO_CHAIN_ANCHOR_BLOCKED', reason_text=?, error=?,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?, stage_updated_at=?
               WHERE id=? AND status IN ('queued','waiting','waiting_retry')""",
            (media_stages.STAGE_WAITING_HUMAN, message, message, now(), now(), row["id"]),
        )
        if cursor.rowcount != 1:
            continue
        if row["version_id"]:
            conn.execute(
                """UPDATE shot_versions SET status='waiting_human', error=?
                   WHERE id=? AND status IN ('queued','running','waiting_retry')""",
                (message, row["version_id"]),
            )
        conn.commit()
        persist_shot_issue(
            episode_id=episode_id,
            shot_id=row["shot_id"],
            shot_no=int(row["shot_no"]),
            issues=[Issue(
                code="VIDEO_CHAIN_ANCHOR_BLOCKED",
                severity=IssueSeverity.BLOCKER,
                subject=row["shot_id"],
                message=message,
                evidence={
                    "shot_no": int(row["shot_no"]),
                    "path": str(row["shot_no"]),
                    "rule_id": "chain_anchor",
                    "job_id": row["id"],
                },
                repair_hint="取消尾帧依赖并按独立首帧重建本镜",
            )],
            source="supervisor_continuity_reconcile",
            run_id=run_id,
        )
        changed += 1
    if changed:
        from app.observability.metrics import inc
        inc("video_continuity_anchor_blocked_total", value=changed, episode_id=episode_id)
    return changed


def _stop_supervised_video_jobs(episode_id: str, *, run_id: str | None, reason: str) -> list[dict[str, Any]]:
    """冻结本次补齐仍在活动/阻塞的媒体任务；重复调用安全。"""
    from app.orchestration import media_scheduler

    conn = get_conn()
    rows = conn.execute(
        """SELECT j.id FROM jobs j
           JOIN shots s ON s.id=j.shot_id
           WHERE j.episode_id=? AND j.kind='video'
             AND s.adopted_version_id IS NULL
             AND j.status IN (
               'queued','running','waiting_provider','waiting_retry','waiting',
               'waiting_human','paused_budget'
             )""",
        (episode_id,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            results.append(media_scheduler.request_cancel(row["id"], reason=reason))
        except Exception as exc:  # noqa: BLE001 — 逐任务 best effort，其余任务仍必须停止
            results.append({"job_id": row["id"], "cancelled": False, "error": str(exc)})
    media_scheduler.reconcile_cancelled_version_states(episode_id=episode_id)
    return results


def _release_episode_supervisor(episode_id: str, *, run_id: str | None) -> None:
    conn = get_conn()
    if run_id:
        conn.execute(
            """UPDATE episodes
               SET video_completion_mode='quick', active_video_run_id=NULL,
                   status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
               WHERE id=? AND (active_video_run_id=? OR active_video_run_id IS NULL)""",
            (episode_id, run_id),
        )
    else:
        conn.execute(
            """UPDATE episodes
               SET video_completion_mode='quick', active_video_run_id=NULL,
                   status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
               WHERE id=?""",
            (episode_id,),
        )
    conn.commit()


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
    supervisor_run_id = cp.run_id if cp is not None else None

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
                  AND (? IS NULL OR owner_run_id=?)
                ORDER BY created_at DESC""",
            (*shot_ids, *status_list, supervisor_run_id, supervisor_run_id),
        ).fetchall():
            if row["shot_id"] not in active_jobs:
                active_jobs[row["shot_id"]] = row["id"]

    cost_map: dict[str, float] = {}
    attempts_map: dict[str, int] = {}
    dispatch_map: dict[str, int] = {}
    best_map: dict[str, dict[str, Any]] = {}
    if shot_ids:
        placeholders = ",".join("?" * len(shot_ids))
        for row in conn.execute(
            f"""SELECT shot_id, id, qa_json, technical_validation_json, cost_cny,
                       provider_task_id, status, image_inputs, version_no
                FROM shot_versions
                WHERE shot_id IN ({placeholders}) AND status!='cleared'""",
            shot_ids,
        ).fetchall():
            sid = row["shot_id"]
            try:
                version_meta = json.loads(row["image_inputs"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                version_meta = {}
            is_delivery_fallback = bool(version_meta.get("delivery_fallback"))
            if is_delivery_fallback:
                # 清理前遗留的图片兜底不算视频版本、尝试次数或覆盖率。
                continue
            if (
                supervisor_run_id is None
                or version_meta.get("supervisor_run_id") == supervisor_run_id
            ):
                dispatch_map[sid] = dispatch_map.get(sid, 0) + 1
            cost_map[sid] = cost_map.get(sid, 0.0) + float(row["cost_cny"] or 0)
            if (
                row["provider_task_id"]
                or row["status"] in {"succeeded", "failed", "running", "queued"}
            ):
                # 产生过 provider 任务或进入执行的版本计为 paid attempt
                if row["provider_task_id"] or row["status"] == "succeeded":
                    paid_attempts = max(
                        1,
                        int(version_meta.get("provider_paid_attempts") or 0),
                    )
                    attempts_map[sid] = attempts_map.get(sid, 0) + paid_attempts
            if row["status"] != "succeeded":
                continue
            qa = json.loads(row["qa_json"] or "{}")
            technical = json.loads(row["technical_validation_json"] or "{}")
            if not technical.get("passed"):
                continue
            try:
                score = float(qa.get("overall")) if qa.get("overall") is not None else -1.0
            except (TypeError, ValueError):
                score = -1.0
            hard_failures = classify_video_hard_failures(qa, technical=technical)
            qa_recovered = bool(qa.get("qa_recovered") or qa.get("status") == "unverified")
            selection_score = video_candidate_selection_score(
                score, hard_failures, qa_recovered=qa_recovered,
            )
            rank = (not qa_recovered, selection_score, score, int(row["version_no"] or 0))
            cur = best_map.get(sid)
            if cur is None or rank >= cur["rank"]:
                best_map[sid] = {
                    "id": row["id"],
                    "score": score,
                    "rank": rank,
                    "qa": qa,
                    "technical": technical,
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
        adopted_version_id = row["adopted_version_id"]
        if adopted_version_id:
            adopted_row = conn.execute(
                """SELECT status,image_inputs,video_path,
                          technical_validation_json
                     FROM shot_versions WHERE id=?""",
                (adopted_version_id,),
            ).fetchone()
            if not _adopted_video_is_usable(adopted_row):
                adopted_version_id = None
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

        persisted = load_persisted_shot_issues(
            sid,
            run_id=supervisor_run_id,
        )
        last_codes = list(saved.get("last_issue_codes") or [])
        if persisted:
            last_codes = [i.code for i in persisted]

        # 若有失败 job 无成功版，补充 issue
        if grade == "C" and not best and not persisted:
            fail_job = conn.execute(
                """SELECT * FROM jobs WHERE shot_id=? AND kind='video'
                   AND status IN ('failed','paused_budget','waiting_human')
                   AND (? IS NULL OR owner_run_id=?)
                   ORDER BY created_at DESC LIMIT 1""",
                (sid, supervisor_run_id, supervisor_run_id),
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

        observed_attempts = int(attempts_map.get(sid, 0))
        try:
            checkpoint_attempts = int(saved.get("attempts_paid") or 0)
        except (TypeError, ValueError):
            checkpoint_attempts = 0

        entry = ShotCoverageEntry(
            shot_no=int(row["shot_no"]),
            shot_id=sid,
            grade=grade,  # type: ignore[arg-type]
            adopted_version_id=adopted_version_id,
            best_version_id=(best or {}).get("id"),
            best_qa_overall=graded["qa_overall"],
            qa_gain_last_2=gain,
            # Checkpoints remember policy history, but the durable version ledger is
            # authoritative for attempts completed after the previous checkpoint.
            # Never let a stale checkpoint move the counter backwards.
            attempts_paid=max(checkpoint_attempts, observed_attempts),
            attempts_dispatched=max(
                int(saved.get("attempts_dispatched") or 0),
                int(dispatch_map.get(sid, 0)),
            ),
            attempts_budgeted=int(saved.get("attempts_budgeted") or MIN_ATTEMPTS_PER_SHOT),
            no_charge_requeues=int(saved.get("no_charge_requeues") or 0),
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
            never_attempted=dispatch_map.get(sid, 0) == 0 and not saved.get("attempts_dispatched"),
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

    covered = sum(1 for entry in entries if entry.adopted_version_id)
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


def _rebuild_budgeted_coverage_ledger(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int,
    budget_cap_cny: float,
) -> CoverageLedger:
    """Build the read-heavy ledger outside the uvicorn event-loop thread."""
    ledger = rebuild_coverage_ledger(
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
    )
    for entry in ledger.entries:
        entry.attempts_budgeted = attempts_for(
            entry,
            ledger,
            budget_cap_cny=budget_cap_cny,
        )
    return ledger


async def _rebuild_budgeted_coverage_ledger_async(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int,
    budget_cap_cny: float,
) -> CoverageLedger:
    if not _supervisor_checks_can_use_worker_thread():
        return _rebuild_budgeted_coverage_ledger(
            episode_id,
            cp=cp,
            fallback_quota=fallback_quota,
            budget_cap_cny=budget_cap_cny,
        )
    return await asyncio.to_thread(
        _rebuild_budgeted_coverage_ledger,
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
        budget_cap_cny=budget_cap_cny,
    )


async def _rebuild_coverage_ledger_async(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int | None = None,
) -> CoverageLedger:
    if not _supervisor_checks_can_use_worker_thread():
        return rebuild_coverage_ledger(
            episode_id,
            cp=cp,
            fallback_quota=fallback_quota,
        )
    return await asyncio.to_thread(
        rebuild_coverage_ledger,
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
    )


def _has_dispatch_budget_capacity(
    episode_id: str,
    entry: ShotCoverageEntry,
    *,
    budget_cap_cny: float,
) -> bool:
    """Project budget capacity before running the expensive shot preflight.

    ``media_scheduler.reserve_budget`` remains the atomic authority. This
    projection reads the same durable costs and active reservations so the
    Supervisor observes in-flight jobs instead of compiling versions which can
    only enter ``paused_budget``.
    """
    from app.video_cost_model import initial_shot_generation_cost

    conn = get_conn()
    shot = conn.execute(
        "SELECT duration_s FROM shots WHERE id=? AND episode_id=?",
        (entry.shot_id, episode_id),
    ).fetchone()
    if shot is None:
        return False
    estimate = initial_shot_generation_cost(float(shot["duration_s"] or 5.0))
    spent = float(conn.execute(
        """SELECT COALESCE(SUM(v.cost_cny), 0) AS amount
             FROM shot_versions v JOIN shots s ON s.id=v.shot_id
            WHERE s.episode_id=? AND v.status='succeeded'""",
        (episode_id,),
    ).fetchone()["amount"] or 0)
    reserved = float(conn.execute(
        """SELECT COALESCE(SUM(amount_cny), 0) AS amount
             FROM budget_reservations
            WHERE scope_type='episode' AND scope_id=?
              AND status IN ('reserved','running')""",
        (episode_id,),
    ).fetchone()["amount"] or 0)
    return spent + reserved + float(estimate) <= float(budget_cap_cny) + 1e-9


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
            "adopted_version_id": e.adopted_version_id,
            "attempts_paid": e.attempts_paid,
            "attempts_dispatched": e.attempts_dispatched,
            "attempts_budgeted": e.attempts_budgeted,
            "no_charge_requeues": e.no_charge_requeues,
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
        "adopted": sum(1 for entry in ledger.entries if entry.adopted_version_id),
        "unadopted": sum(1 for entry in ledger.entries if not entry.adopted_version_id),
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
            scene_time=(r["scene_time"] if "scene_time" in r.keys() else "") or "",
            scene_setting=r["scene_setting"] or "",
            scene_name=(r["scene_name"] if "scene_name" in r.keys() else "") or "",
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
    cp: VideoSupervisorCheckpoint | None = None,
    plan: VideoRepairPlan | None = None,
    first: bool = False,
) -> bool:
    """入队；失败 Issue 化。返回是否产生新进展（非 reused）。"""
    from app import worker
    from app.compiler import CompileError

    # 付费派发的最后保险：补齐模式永远不得为已有采用版的镜头创建新任务。
    # entry 是循环开始时的快照；入队前必须重读数据库，封住用户刚刚采用候选的并发窗口。
    if entry.adopted_version_id:
        return False

    conn = get_conn()
    current_shot = conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id=? AND episode_id=?",
        (entry.shot_id, episode_id),
    ).fetchone()
    if not current_shot or current_shot["adopted_version_id"]:
        return False
    execution_rows = conn.execute(
        """SELECT j.status,j.provider_create_state,v.provider_task_id,v.image_inputs
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id=j.version_id
            WHERE j.shot_id=? AND j.episode_id=? AND j.kind='video'
              AND (v.status IS NULL OR v.status!='cleared')
            ORDER BY j.created_at DESC""",
        (entry.shot_id, episode_id),
    ).fetchall()
    for execution in execution_rows:
        try:
            execution_meta = json.loads(execution["image_inputs"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            execution_meta = {}
        if run_id and execution_meta.get("supervisor_run_id") != run_id:
            continue
        if execution["provider_create_state"] == "model_rejected":
            return False
        if (
            execution["status"] in ACTIVE_JOB_STATUSES
            and (
                execution["provider_task_id"]
                or execution["provider_create_state"] in {"submitting", "accepted"}
            )
        ):
            return False

    if run_id:
        ep = conn.execute(
            "SELECT active_video_run_id, video_completion_mode FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        latest = load_latest_checkpoint(episode_id)
        if (
            not ep
            or ep["video_completion_mode"] != "complete"
            or ep["active_video_run_id"] != run_id
            or (latest is not None and (
                latest.dispatch_fenced_at is not None
                or latest.phase in TERMINAL_SUPERVISOR_PHASES
            ))
        ):
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "VIDEO_DISPATCH_FENCED",
                    "warning",
                    f"第 {entry.shot_no} 镜派发被终态围栏拒绝",
                    payload={"shot_no": entry.shot_no, "phase": latest.phase if latest else None},
                )
            return False

    authority_checkpoint = cp or (load_latest_checkpoint(episode_id) if run_id else None)
    if authority_checkpoint is not None:
        # Last reversible boundary before worker.enqueue_shot can create paid
        # provider work.  Do not turn authority drift into a generic QA issue.
        _verify_supervisor_paid_authority(
            authority_checkpoint,
            stage="video_provider_enqueue",
        )

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
    kwargs["supervisor_run_id"] = run_id
    # Supervisor dispatches are positive production actions too.  Capture the
    # same immutable dependency token used by the review wall so a later
    # screenplay/storyboard/asset change fences already-running providers.
    if run_id:
        try:
            from app.api import _review_assert_shot_positive
            kwargs["dependency_snapshot"] = _review_assert_shot_positive(entry.shot_id)
        except Exception as exc:
            issues = issues_from_enqueue_error(
                exc, shot_id=entry.shot_id, shot_no=entry.shot_no,
            )
            persist_shot_issue(
                episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
                issues=issues, source="supervisor_dependency_fence", run_id=run_id,
            )
            entry.last_issue_codes = [i.code for i in issues]
            return False
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
        if authority_checkpoint is not None:
            _verify_supervisor_paid_authority(
                authority_checkpoint,
                stage="video_provider_enqueue_commit",
            )
        _debug_enqueue_started = time.monotonic()
        # #region debug-point B:enqueue-duration
        with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"B","location":"app/video_supervisor.py:_dispatch","msg":"[DEBUG] worker enqueue start","data":{"shot_no":entry.shot_no,"db_in_transaction":get_conn().in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
        # #endregion
        result = worker.enqueue_shot(entry.shot_id, **{
            k: v for k, v in kwargs.items() if k != "supervisor_meta"
        })
        # #region debug-point B:enqueue-duration
        with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"B","location":"app/video_supervisor.py:_dispatch","msg":"[DEBUG] worker enqueue end","data":{"shot_no":entry.shot_no,"elapsed_ms":round((time.monotonic()-_debug_enqueue_started)*1000,1),"db_in_transaction":get_conn().in_transaction,"reused":bool(result.get("reused"))},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
        # #endregion
        # 把 supervisor meta 写入新建 version
        if result.get("version_id") and kwargs.get("supervisor_meta"):
            _patch_version_supervisor_meta(result["version_id"], kwargs["supervisor_meta"])
    except GrantValidationError:
        raise
    except (CompileError, ValueError) as exc:
        issues = issues_from_enqueue_error(exc, shot_id=entry.shot_id, shot_no=entry.shot_no)
        persist_shot_issue(
            episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
            issues=issues, source="supervisor_enqueue", run_id=run_id,
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
            issues=issues, source="supervisor_enqueue", run_id=run_id,
        )
        entry.last_issue_codes = [i.code for i in issues]
        return False

    if result.get("paused_budget"):
        issues = issues_from_enqueue_error(
            ValueError("预算不足，任务暂停"), shot_id=entry.shot_id, shot_no=entry.shot_no,
        )
        persist_shot_issue(
            episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
            issues=issues, source="supervisor_budget", run_id=run_id,
        )
        entry.last_issue_codes = [i.code for i in issues]
        return False
    if result.get("reused"):
        return bool(result.get("resumed"))
    if degrade:
        entry.continuity_degraded = True
    if plan and plan.rebuild_reference:
        entry.rebuilt_reference = True
    return True


def _dispatch_with_heartbeat(
    entry: ShotCoverageEntry,
    *,
    episode_id: str,
    run_id: str | None,
    cp: VideoSupervisorCheckpoint,
    plan: VideoRepairPlan | None = None,
    first: bool = False,
) -> bool:
    """Keep the run live across synchronous request compilation and enqueue."""
    _debug_dispatch_started = time.monotonic()
    # #region debug-point A:dispatch-duration
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"A","location":"app/video_supervisor.py:_dispatch_with_heartbeat","msg":"[DEBUG] dispatch start","data":{"shot_no":entry.shot_no,"db_in_transaction":get_conn().in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    _refresh_supervisor_heartbeat(cp, run_id=run_id)
    try:
        return _dispatch(
            entry,
            episode_id=episode_id,
            run_id=run_id,
            cp=cp,
            plan=plan,
            first=first,
        )
    finally:
        _refresh_supervisor_heartbeat(cp, run_id=run_id)
        # #region debug-point A:dispatch-duration
        with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"A","location":"app/video_supervisor.py:_dispatch_with_heartbeat","msg":"[DEBUG] dispatch end","data":{"shot_no":entry.shot_no,"elapsed_ms":round((time.monotonic()-_debug_dispatch_started)*1000,1),"db_in_transaction":get_conn().in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
        # #endregion


async def _dispatch_with_heartbeat_async(
    entry: ShotCoverageEntry,
    *,
    episode_id: str,
    run_id: str | None,
    cp: VideoSupervisorCheckpoint,
    plan: VideoRepairPlan | None = None,
    first: bool = False,
    heartbeat_interval_s: float = DISPATCH_HEARTBEAT_INTERVAL_S,
) -> bool:
    """Run synchronous authority checks and enqueue work off the event loop."""
    if not _supervisor_checks_can_use_worker_thread():
        return _dispatch_with_heartbeat(
            entry,
            episode_id=episode_id,
            run_id=run_id,
            cp=cp,
            plan=plan,
            first=first,
        )

    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _dispatch_heartbeat(
            cp,
            run_id=run_id,
            stop=heartbeat_stop,
            interval_s=heartbeat_interval_s,
        ),
        name=f"video-dispatch-heartbeat:{episode_id}:{entry.shot_no}",
    )
    worker_task = asyncio.create_task(
        asyncio.to_thread(
            _dispatch_with_heartbeat,
            entry,
            episode_id=episode_id,
            run_id=run_id,
            cp=cp,
            plan=plan,
            first=first,
        ),
        name=f"video-dispatch-worker:{episode_id}:{entry.shot_no}",
    )
    cancelled = False
    try:
        while True:
            try:
                result = await asyncio.shield(worker_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                continue
        if cancelled:
            raise asyncio.CancelledError
        return result
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def _dispatch_heartbeat(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    stop: asyncio.Event,
    interval_s: float = DISPATCH_HEARTBEAT_INTERVAL_S,
) -> None:
    wait_s = max(
        0.01,
        min(float(interval_s), SUPERVISOR_HEARTBEAT_STALE_S / 3.0),
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait_s)
            return
        except asyncio.TimeoutError:
            pass
        try:
            refreshed = await asyncio.to_thread(
                _refresh_supervisor_heartbeat,
                cp,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient DB contention is retryable
            try:
                errors.log_error(
                    exc,
                    action="video_supervisor.dispatch_heartbeat",
                    context={"episode_id": cp.episode_id, "run_id": run_id},
                )
            except Exception:  # noqa: BLE001 - heartbeat retry must stay alive
                pass
            continue
        if not refreshed:
            return


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


def _collect_issues(
    entry: ShotCoverageEntry,
    *,
    run_id: str | None = None,
) -> list[Issue]:
    issues = load_persisted_shot_issues(entry.shot_id, run_id=run_id)
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
    if not issues:
        failed = conn.execute(
            """SELECT * FROM jobs
               WHERE shot_id=? AND kind='video'
                 AND status IN ('failed','paused_budget','waiting_human')
                 AND (? IS NULL OR owner_run_id=?)
               ORDER BY created_at DESC LIMIT 1""",
            (entry.shot_id, run_id, run_id),
        ).fetchone()
        if failed:
            failed_version = None
            if failed["version_id"]:
                failed_version = conn.execute(
                    "SELECT * FROM shot_versions WHERE id=?",
                    (failed["version_id"],),
                ).fetchone()
            issues = issues_from_job_failure(
                dict(failed),
                dict(failed_version) if failed_version else None,
                shot_id=entry.shot_id,
                shot_no=entry.shot_no,
            )
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
    shot = conn.execute(
        "SELECT shot_contract_json, last_frame_desc FROM shots WHERE id=?",
        (entry.shot_id,),
    ).fetchone()
    if shot:
        try:
            contract = json.loads(shot["shot_contract_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            contract = {}
        if isinstance(contract, dict):
            planned = contract.get("state_out")
        planned = planned or shot["last_frame_desc"]
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


def _repair_authority_ids(shots: list[Shot]) -> StoryboardRepairAffectedAuthority:
    action_ids: set[str] = set()
    event_ids: set[str] = set()
    phase_ids: set[str] = set()
    experience_ids: set[str] = set()
    for shot in shots:
        action_ids.update(
            value
            for value in [shot.primary_action_id, *(shot.supporting_action_ids or [])]
            if value
        )
        event_ids.update(value for value in (shot.event_ids or []) if value)
        phase_ids.update(value for value in (shot.action_phase_ids or []) if value)
        if shot.shot_contribution:
            experience_ids.update(
                value
                for value in shot.shot_contribution.experience_intent_ids
                if value
            )
    return StoryboardRepairAffectedAuthority(
        action_ids=sorted(action_ids),
        event_ids=sorted(event_ids),
        action_phase_ids=sorted(phase_ids),
        experience_intent_ids=sorted(experience_ids),
    )


def _candidate_storyboard_from_repair(
    board: Storyboard,
    *,
    shot_index: int,
    proposal: StoryboardRepairProposal,
) -> Storyboard:
    shots = [shot.model_copy(deep=True) for shot in board.shots]
    replacements = [shot.model_copy(deep=True) for shot in proposal.candidate_shots]
    shots[shot_index : shot_index + 1] = replacements
    # shot_no is a display/order projection. Structural insertion shifts later
    # numbers but never edits authored state, action or audience contracts.
    for index, shot in enumerate(shots, start=1):
        shot.shot_no = index
    return Storyboard(episode_no=board.episode_no, shots=shots)


def _validate_storyboard_repair_proposal(
    proposal: StoryboardRepairProposal,
    *,
    board: Storyboard,
    shot_index: int,
    database_shot_id: str,
    screenplay,
    bible,
    target_duration_s: int,
    episode_id: str,
) -> tuple[Storyboard | None, list[str]]:
    errors: list[str] = []
    original = board.shots[shot_index]
    original_authority_id = (
        str(original.shot_id or "").strip() or database_shot_id
    )
    if proposal.base_shot_id != database_shot_id:
        errors.append("base_shot_id 必须精确绑定当前数据库 Shot")
    expected_count = 1 if proposal.operation == "replace" else 2
    if len(proposal.candidate_shots) != expected_count:
        errors.append(f"{proposal.operation} 操作必须输出 {expected_count} 个完整 Shot")
    candidate_ids = [str(shot.shot_id or "").strip() for shot in proposal.candidate_shots]
    if candidate_ids and candidate_ids[0] != original_authority_id:
        errors.append("候选首镜必须保留被修复 Shot 的权威 ID")
    if any(not value for value in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids):
        errors.append("候选 Shot 必须具有非空且不重复的稳定 ID")
    first_no = int(original.shot_no)
    if [shot.shot_no for shot in proposal.candidate_shots] != list(
        range(first_no, first_no + expected_count)
    ):
        errors.append("候选 Shot 的 shot_no 必须从被修复镜头开始连续排列")
    proposed_duration = sum(int(shot.duration_s or 0) for shot in proposal.candidate_shots)
    if proposed_duration != proposal.expected_total_duration_s:
        errors.append("预计时长必须等于所有候选 Shot 的时长之和")

    expected_affected = _repair_authority_ids([original, *proposal.candidate_shots])
    for field in (
        "action_ids",
        "event_ids",
        "action_phase_ids",
        "experience_intent_ids",
    ):
        proposed = list(getattr(proposal.affected_authority, field))
        expected = list(getattr(expected_affected, field))
        if len(proposed) != len(set(proposed)) or set(proposed) != set(expected):
            errors.append(
                "affected_authority 必须完整列出原镜头与候选镜头触及的 "
                f"{field}"
            )

    narrative_plan = screenplay.narrative_plan if screenplay is not None else None
    if narrative_plan is not None:
        known_actions = {item.action_id for item in narrative_plan.atomic_actions}
        known_events = {item.event_id for item in narrative_plan.events}
        known_phases = {
            phase.phase_id
            for action in narrative_plan.atomic_actions
            for phase in action.temporal_phases
        }
        known_experiences = {
            item.experience_intent_id for item in narrative_plan.experience_intents
        }
        for values, known, label in (
            (proposal.affected_authority.action_ids, known_actions, "action"),
            (proposal.affected_authority.event_ids, known_events, "event"),
            (proposal.affected_authority.action_phase_ids, known_phases, "phase"),
            (
                proposal.affected_authority.experience_intent_ids,
                known_experiences,
                "experience",
            ),
        ):
            missing = sorted(set(values) - known)
            if missing:
                errors.append(f"affected_authority 含不存在的 {label} IDs: {missing}")

    candidate_board: Storyboard | None = None
    if not errors:
        candidate_board = _candidate_storyboard_from_repair(
            board,
            shot_index=shot_index,
            proposal=proposal,
        )
        from app.validators import (
            validate_storyboard,
            validate_storyboard_continuity_contract,
            validate_storyboard_preserves_key_content,
            validate_storyboard_soundtrack,
        )

        errors.extend(validate_storyboard(
            candidate_board,
            bible,
            target_duration_s,
            narrative_authority=narrative_plan is not None,
            narrative_plan=narrative_plan,
            screenplay=screenplay,
        ))
        errors.extend(validate_storyboard_continuity_contract(
            candidate_board,
            screenplay,
        ))
        if screenplay is not None:
            errors.extend(validate_storyboard_soundtrack(
                candidate_board,
                screenplay,
                target_duration_s,
            ))
            errors.extend(validate_storyboard_preserves_key_content(
                candidate_board,
                screenplay,
            ))
        if narrative_plan is not None:
            from app.narrative import validate_storyboard_narrative

            errors.extend(validate_storyboard_narrative(
                candidate_board,
                screenplay,
                complete=True,
                expected_scope_id=episode_id,
            ))
    return candidate_board, list(dict.fromkeys(errors))


async def _semantic_storyboard_repair_proposal(
    *,
    board: Storyboard,
    shot_index: int,
    database_shot_id: str,
    screenplay,
    bible,
    target_duration_s: int,
    episode_id: str,
    repair_plan: VideoRepairPlan | None,
    observed_issue_codes: list[str],
    authority_checkpoint: VideoSupervisorCheckpoint | None = None,
) -> tuple[StoryboardRepairProposal, Storyboard]:
    from app.harness import model_gateway

    current = board.shots[shot_index]
    prompt = {
        "task": (
            "As a continuity director, infer the semantic cause of this failed shot and "
            "propose either one complete replacement Shot or a two-Shot split. "
            "Do not edit any unrelated shot and do not select a repair from issue wording."
        ),
        "episode_id": episode_id,
        "database_shot_id": database_shot_id,
        "repair_evidence": {
            "structured_issue_codes": observed_issue_codes,
            "router_reason": repair_plan.reason if repair_plan else None,
            "router_issue_evidence": (
                repair_plan.model_dump(mode="json") if repair_plan else None
            ),
        },
        "current_shot_index": shot_index,
        "current_shot": current.model_dump(mode="json"),
        "previous_shot": (
            board.shots[shot_index - 1].model_dump(mode="json")
            if shot_index > 0 else None
        ),
        "next_shot": (
            board.shots[shot_index + 1].model_dump(mode="json")
            if shot_index + 1 < len(board.shots) else None
        ),
        "complete_storyboard": board.model_dump(mode="json"),
        "screenplay_authority": (
            screenplay.model_dump(mode="json") if screenplay is not None else None
        ),
        "bible": bible.model_dump(mode="json"),
        "target_duration_s": target_duration_s,
        "output_schema": StoryboardRepairProposal.model_json_schema(),
        "constraints": [
            "candidate_shots are complete Shot objects, not patches",
            "replace returns exactly one candidate; split returns exactly two",
            "the first candidate preserves the current authority shot_id",
            "affected_authority is derived from semantic graph relations",
            "preserve causal topology, action ownership, state hand-offs and audience processing",
        ],
    }
    prior = ""
    errors: list[str] = []
    for attempt in range(1, 3):
        if authority_checkpoint is not None:
            _verify_supervisor_paid_authority(
                authority_checkpoint,
                stage="semantic_storyboard_repair_model",
            )
        request = json.dumps(prompt, ensure_ascii=False)
        if errors:
            request += (
                "\nThe previous proposal failed deterministic validation. Return a full corrected JSON proposal.\n- "
                + "\n- ".join(errors[:20])
                + "\nPrevious proposal:\n"
                + prior[:16000]
            )
        prior = await model_gateway.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a senior film director repairing a storyboard from semantic "
                        "intent, narrative relations and audience comprehension. Output JSON only."
                    ),
                },
                {"role": "user", "content": request},
            ],
            temperature=0.2,
            max_tokens=16384,
            call_meta={
                "stage": "video_supervisor_storyboard_repair",
                "stage_key": "semantic_storyboard_repair_proposal",
                "call_role": "continuity_director",
                "episode_id": episode_id,
                "repair_round": attempt - 1,
                "contract_version": "storyboard-semantic-repair-proposal.v1",
            },
        )
        try:
            proposal = StoryboardRepairProposal.model_validate(extract_json(prior))
        except Exception as exc:  # noqa: BLE001 - untrusted model boundary
            errors = [f"JSON/Schema invalid: {exc}"]
            continue
        candidate, errors = _validate_storyboard_repair_proposal(
            proposal,
            board=board,
            shot_index=shot_index,
            database_shot_id=database_shot_id,
            screenplay=screenplay,
            bible=bible,
            target_duration_s=target_duration_s,
            episode_id=episode_id,
        )
        if not errors and candidate is not None:
            return proposal, candidate
    raise ValueError("; ".join(errors[:20]) or "semantic repair model returned no proposal")


async def _amend_storyboard(
    entry: ShotCoverageEntry,
    *,
    grant: VideoCompletionGrant,
    plan: VideoRepairPlan | None = None,
    run_id: str | None = None,
) -> bool:
    """L5 只创建分镜修改草稿，不改写已确认分镜。

    ``allow_storyboard_edit`` 授予的是“提议草稿”权限，不是绕过人工重新
    确认的权限。草稿产生后 Supervisor 转 WAITING_HUMAN，且视频流水线
    保持暂停；只有分镜台完成并发布新终态后才能重新授权。
    """
    if not grant.allow_storyboard_edit:
        return False
    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (entry.shot_id,)).fetchone()
    if not row:
        return False
    episode_id = str(row["episode_id"])
    try:
        authority_checkpoint = VideoSupervisorCheckpoint(
            episode_id=episode_id,
            grant_id=grant.grant_id,
            storyboard_artifact_id=grant.storyboard_artifact_id,
            episode_video_plan_id=grant.episode_video_plan_id,
            episode_video_plan_revision=grant.episode_video_plan_revision,
            video_plan_release_hash=grant.video_plan_release_hash,
            capability_snapshot_id=grant.capability_snapshot_id,
        )
        _verify_supervisor_paid_authority(
            authority_checkpoint,
            stage="semantic_storyboard_repair",
        )
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        project = conn.execute(
            "SELECT bible_json FROM projects WHERE id=?", (episode["project_id"],)
        ).fetchone()
        if not project or not str(project["bible_json"] or "").strip():
            raise ValueError("当前项目缺少可验证的 Bible，不能生成分镜修复候选")
        from app.schemas import Bible
        from app.production.screenplay_authority import resolve_downstream_screenplay
        from app.domain.storyboard_ops import _board_from_shot_rows

        bible = Bible.model_validate_json(project["bible_json"])
        try:
            screenplay = resolve_downstream_screenplay(
                episode_id,
                conn=conn,
            ).screenplay
        except ValueError:
            screenplay = None
        rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        board = _board_from_shot_rows(rows, int(episode["episode_no"] or 1))
        shot_index = next(
            index for index, item in enumerate(rows) if item["id"] == entry.shot_id
        )
        proposal, candidate_board = await _semantic_storyboard_repair_proposal(
            board=board,
            shot_index=shot_index,
            database_shot_id=entry.shot_id,
            screenplay=screenplay,
            bible=bible,
            target_duration_s=int(episode["target_duration_s"] or 0),
            episode_id=episode_id,
            repair_plan=plan,
            observed_issue_codes=list(
                (plan.issue_codes if plan else entry.last_issue_codes) or []
            ),
            authority_checkpoint=authority_checkpoint,
        )
    except GrantValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - model/validator failure pauses for human
        if run_id:
            evidence_repository.append_event(
                run_id,
                "VIDEO_STORYBOARD_PROPOSAL_REJECTED",
                "warning",
                "AI 分镜语义修复候选未通过，已转人工",
                payload={"shot_id": entry.shot_id, "error": str(exc)[:2000]},
            )
        return False
    try:
        art = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_repair_proposal",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T1",
            content={
                "proposed_by": "video_supervisor_semantic_director",
                "base_storyboard_artifact_id": grant.storyboard_artifact_id,
                "proposal": proposal.model_dump(mode="json"),
                "candidate_storyboard": candidate_board.model_dump(mode="json"),
                "requires_manual_confirmation": True,
            },
            parent_artifact_ids=[grant.storyboard_artifact_id] if grant.storyboard_artifact_id else [],
            contract_version="storyboard-semantic-repair-proposal.v1",
        ))
        conn.execute(
            "UPDATE episodes SET working_storyboard_artifact_id=? WHERE id=?",
            (art["id"], episode_id),
        )
        conn.commit()
        try:
            from app.video_control import request_control
            request_control(episode_id, "pause")
        except Exception:  # noqa: BLE001
            pass
        if run_id:
            evidence_repository.append_event(
                run_id, "VIDEO_STORYBOARD_DRAFT_CREATED", "warning",
                f"第 {entry.shot_no} 镜 L5 修改草稿待重新确认",
                payload={
                    "shot_id": entry.shot_id,
                    "artifact_id": art["id"],
                    "operation": proposal.operation,
                    "affected_authority": proposal.affected_authority.model_dump(
                        mode="json"
                    ),
                },
            )
        return True
    except Exception:  # noqa: BLE001
        return False


# 兼容旧名
async def _amend_storyboard_duration(
    entry: ShotCoverageEntry,
    *,
    grant: VideoCompletionGrant,
) -> bool:
    return await _amend_storyboard(entry, grant=grant)


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


def _adopt_ready_candidates(
    ledger: CoverageLedger,
    *,
    run_id: str | None,
) -> int:
    """正常运行期采用首个技术有效候选；QA 只影响风险展示。"""
    adopted = 0
    for entry in ledger.entries:
        if entry.adopted_version_id or not entry.best_version_id:
            continue
        result = select_best_video_candidate(entry.shot_id, force_best=False)
        if not result:
            continue
        entry.adopted_version_id = result.get("version_id")
        entry.fallback_reason = result.get("fallback_reason")
        adopted += 1
        if run_id:
            evidence_repository.append_event(
                run_id,
                "VIDEO_CANDIDATE_ADOPTED",
                "info",
                f"第 {entry.shot_no} 镜由 Supervisor 采用候选",
                payload={"shot_no": entry.shot_no, "version_id": entry.adopted_version_id},
            )
    return adopted


def _write_coverage_report(
    cp: VideoSupervisorCheckpoint,
    ledger: CoverageLedger,
    *,
    outcome: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = {
        "episode_id": cp.episode_id,
        "run_id": cp.run_id,
        "outcome": outcome,
        "terminal_reason": cp.terminal_reason,
        "started_at": cp.started_at,
        "deadline_at": cp.deadline_at,
        "finished_at": cp.finished_at,
        "grades": ledger.grades,
        "fallback_quota": ledger.fallback_quota,
        "quality_target_missed": cp.quality_target_missed,
        "missing_shots": cp.missing_shots,
        "closeout_adoptions": cp.closeout_adoptions,
        "cost_spent_cny": ledger.cost_spent,
        "budget_cap_cny": (cp.budget or {}).get("cap_cny"),
        "shots": [
            {
                "shot_no": e.shot_no,
                "shot_id": e.shot_id,
                "grade": e.grade,
                "adopted_version_id": e.adopted_version_id,
                "best_version_id": e.best_version_id,
                "qa_overall": e.best_qa_overall,
                "cost_spent_cny": e.cost_spent_cny,
                "repair_level": e.repair_level,
                "continuity_degraded": e.continuity_degraded,
                "fallback_reason": e.fallback_reason,
                "attempts_paid": e.attempts_paid,
                "attempts_budgeted": e.attempts_budgeted,
                "last_issue_codes": e.last_issue_codes,
            }
            for e in ledger.entries
        ],
        "last_plan": cp.last_plan,
        "repair_epoch": cp.repair_epoch,
        **(extra or {}),
    }
    # 以 run + outcome 为幂等键；旧报告不能吞掉新一轮的终态报告。
    rows = get_conn().execute(
        """SELECT content_json FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('validated','approved')
           ORDER BY created_at DESC LIMIT 20""",
        (REPORT_ARTIFACT_TYPE, cp.episode_id),
    ).fetchall()
    for row in rows:
        try:
            old = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if old.get("run_id") == cp.run_id and old.get("outcome") == outcome:
            return old
    art = evidence_repository.create_artifact(EvidenceArtifact(
        type=REPORT_ARTIFACT_TYPE,
        scope_type="episode",
        scope_id=cp.episode_id,
        status="validated",
        trust_level="T2",
        content=report,
        contract_version="video-coverage-1.1.0",
    ))
    evidence_repository.create_evaluation(
        art["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="video_coverage_report",
            evaluator_version="1.1.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"grades": ledger.grades, "outcome": outcome},
        ),
    )
    return report


def _deadline_closeout(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    reason: str = "VIDEO_WALL_CLOCK_EXCEEDED",
) -> VideoSupervisorCheckpoint:
    """不可逆、幂等的截止收口：停止派发，停止任务，采用每镜最佳技术可播候选。"""
    if cp.phase in TERMINAL_SUPERVISOR_PHASES:
        _release_episode_supervisor(cp.episode_id, run_id=run_id or cp.run_id)
        return cp
    cp.phase = "DEADLINE_CLOSING"
    cp.terminal_reason = reason
    cp.dispatch_fenced_at = cp.dispatch_fenced_at or now()
    cp.closeout_started_at = cp.closeout_started_at or now()
    save_checkpoint(cp, run_id=run_id)

    stopped = _stop_supervised_video_jobs(
        cp.episode_id,
        run_id=run_id or cp.run_id,
        reason=f"Supervisor 收口：{reason}",
    )
    ledger = rebuild_coverage_ledger(
        cp.episode_id,
        cp=cp,
        fallback_quota=int(cp.coverage.get("fallback_quota") or 0),
    )
    adopted_at_closeout: list[dict[str, Any]] = list(cp.closeout_adoptions)
    already_recorded = {str(item.get("shot_id")) for item in adopted_at_closeout}
    conn = get_conn()
    for entry in ledger.entries:
        if entry.adopted_version_id:
            row = conn.execute(
                "SELECT adoption_reason, qa_json FROM shot_versions WHERE id=?",
                (entry.adopted_version_id,),
            ).fetchone()
            if (
                entry.shot_id not in already_recorded
                and row
                and str(row["adoption_reason"] or "").startswith("截止收口由 Supervisor")
            ):
                try:
                    adopted_qa = json.loads(row["qa_json"] or "{}")
                    adopted_score = adopted_qa.get("overall")
                except (TypeError, ValueError, json.JSONDecodeError):
                    adopted_score = None
                adopted_at_closeout.append({
                    "shot_no": entry.shot_no,
                    "shot_id": entry.shot_id,
                    "version_id": entry.adopted_version_id,
                    "qa_overall": adopted_score,
                    "risk": row["adoption_reason"],
                })
                already_recorded.add(entry.shot_id)
            # adopted 是不可被补齐流程覆盖的用户结果；技术/QA 风险写报告，
            # 但截止收口也不得换版。
            continue
        result = select_best_video_candidate(entry.shot_id, force_best=True)
        if not result:
            continue
        version_id = result.get("version_id")
        reason_text = (
            f"截止收口由 Supervisor 强制采用：在技术可播候选中选择最佳版本；"
            f"QA 仅作为排序和风险标记。{result.get('reason') or ''}"
        )
        conn.execute(
            "UPDATE shot_versions SET adoption_reason=? WHERE id=?",
            (reason_text, version_id),
        )
        conn.commit()
        if entry.shot_id not in already_recorded:
            adopted_at_closeout.append({
                "shot_no": entry.shot_no,
                "shot_id": entry.shot_id,
                "version_id": version_id,
                "qa_overall": entry.best_qa_overall,
                "risk": result.get("fallback_reason") or result.get("reason"),
            })
            already_recorded.add(entry.shot_id)

    cp.closeout_adoptions = adopted_at_closeout
    ledger = rebuild_coverage_ledger(
        cp.episode_id,
        cp=cp,
        fallback_quota=int(cp.coverage.get("fallback_quota") or 0),
    )
    cp.missing_shots = [e.shot_no for e in ledger.entries if not e.adopted_version_id]
    cp.quality_target_missed = bool(
        cp.missing_shots or any(e.grade != "A" for e in ledger.entries)
    )
    cp.finished_at = now()
    cp.outcome = (
        "PARTIAL_NO_USABLE_CANDIDATE"
        if cp.missing_shots
        else "COMPLETED_DEADLINE_FALLBACK"
    )
    cp.phase = cp.outcome  # type: ignore[assignment]
    _merge_shot_state(cp, ledger)
    report = _write_coverage_report(
        cp,
        ledger,
        outcome=cp.outcome,
        extra={
            "stopped_jobs": stopped,
            "deterministic_fallbacks": {
                "disabled": True,
                "reason": "静态图片、轻运动卡和静音片段不具备视频采用资格",
            },
        },
    )
    if cp.grant_id:
        try:
            consume_grant(cp.grant_id)
        except Exception:  # noqa: BLE001
            pass
    save_checkpoint(cp, run_id=run_id)
    _release_episode_supervisor(cp.episode_id, run_id=run_id or cp.run_id)
    if run_id:
        evidence_repository.append_event(
            run_id,
            "VIDEO_DEADLINE_CLOSED",
            "warning" if cp.missing_shots else "info",
            f"截止收口完成；采用 {len(cp.closeout_adoptions)} 镜，缺失 {cp.missing_shots}",
            payload=report,
        )
    from app.observability.metrics import inc
    inc(
        "video_supervisor_deadline_fallback_adopted_total",
        value=len(cp.closeout_adoptions),
        episode_id=cp.episode_id,
        terminal_reason=reason,
    )
    inc(
        "video_supervisor_deadline_missing_shots_total",
        value=len(cp.missing_shots),
        episode_id=cp.episode_id,
        terminal_reason=reason,
    )
    inc(
        "video_supervisor_deadline_closeout_seconds",
        value=max(0, int((cp.finished_at or now()) - (cp.closeout_started_at or now()))),
        episode_id=cp.episode_id,
    )
    return cp


def _finalize_covered(
    cp: VideoSupervisorCheckpoint, ledger: CoverageLedger, *, run_id: str | None
) -> VideoSupervisorCheckpoint:
    cp.phase = "FINALIZING"
    save_checkpoint(cp, run_id=run_id)
    cp.phase = "SUCCEEDED_COVERED"
    cp.outcome = "SUCCEEDED_COVERED"
    cp.terminal_reason = "COVERAGE_TARGET_MET"
    cp.finished_at = now()
    cp.missing_shots = []
    cp.quality_target_missed = any(
        entry.grade != "A" or entry.video_stale or entry.chain_stale
        for entry in ledger.entries
    )
    _merge_shot_state(cp, ledger)
    report = _write_coverage_report(cp, ledger, outcome=cp.outcome)
    if cp.grant_id:
        try:
            consume_grant(cp.grant_id)
        except Exception:  # noqa: BLE001
            pass
    save_checkpoint(cp, run_id=run_id)
    _release_episode_supervisor(cp.episode_id, run_id=run_id or cp.run_id)
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


def _reference_asset_scan(episode_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the read-only episode asset scan using the persisted storyboard."""
    conn = get_conn()
    episode = conn.execute(
        "SELECT id, project_id, episode_no FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if not episode:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?", (episode["project_id"],),
    ).fetchone()
    if not project or not (project["bible_json"] or "").strip():
        return dict(episode), {"characters": [], "scenes": [], "blockers": []}
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    from app.domain.storyboard_ops import _board_from_shot_rows
    from app.multiview import scan_episode_reference_asset_gaps
    from app.production.screenplay_authority import resolve_downstream_screenplay
    from app.schemas import Bible

    board = _board_from_shot_rows(rows, int(episode["episode_no"]))
    bible = Bible.model_validate_json(project["bible_json"])
    screenplay = resolve_downstream_screenplay(episode_id, conn=conn).screenplay
    scan = scan_episode_reference_asset_gaps(
        project_id=episode["project_id"],
        episode_no=int(episode["episode_no"]),
        shots=[(row["id"], board.shots[index]) for index, row in enumerate(rows)],
        bible=bible,
        screenplay=screenplay,
    )
    return dict(episode), scan


async def _asset_prep_heartbeat(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    stop: asyncio.Event,
    interval_s: float = ASSET_PREP_HEARTBEAT_INTERVAL_S,
) -> None:
    """Keep long reference generation from looking like a dead supervisor.

    Character and scene packs can each spend several minutes in a provider call,
    and preparation may also wait behind the per-project lock.  Neither wait is a
    control-plane failure, so keep both the run row and checkpoint fresh until the
    preparation stage exits.  Ownership is checked before every write so an old
    task cannot revive its heartbeat after a newer run has taken over.
    """
    wait_s = max(0.01, min(float(interval_s), SUPERVISOR_HEARTBEAT_STALE_S / 3.0))
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait_s)
            return
        except asyncio.TimeoutError:
            pass
        if run_id:
            owner = get_conn().execute(
                "SELECT active_video_run_id FROM episodes WHERE id=?",
                (cp.episode_id,),
            ).fetchone()
            if not owner or owner["active_video_run_id"] != run_id:
                return
        cp.phase = "PREPARING_ASSETS"
        save_checkpoint(cp, run_id=run_id)


async def _prepare_episode_reference_assets(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    run_id: str | None,
) -> dict[str, Any]:
    """Prepare only missing Bible-managed assets before any video dispatch."""
    _verify_supervisor_paid_authority(cp, stage="reference_asset_scan")
    episode, initial = _reference_asset_scan(episode_id)
    if not initial["blockers"]:
        return initial

    cp.phase = "PREPARING_ASSETS"
    cp.outcome = None
    save_checkpoint(cp, run_id=run_id)
    if run_id:
        evidence_repository.append_event(
            run_id,
            "VIDEO_REFERENCE_ASSET_PREP_STARTED",
            "info",
            "正在补齐本集视频所需的人物与场景资产",
            payload={
                "characters": initial["characters"],
                "scenes": initial["scenes"],
            },
        )

    project_id = str(episode["project_id"])
    lock = _REFERENCE_ASSET_PREP_LOCKS.setdefault(project_id, asyncio.Lock())
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _asset_prep_heartbeat(cp, run_id=run_id, stop=heartbeat_stop),
        name=f"video-asset-prep-heartbeat:{episode_id}",
    )
    try:
        async with lock:
            _, current = _reference_asset_scan(episode_id)
            conn = get_conn()
            project = conn.execute(
                "SELECT bible_json FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            bible_payload = json.loads(project["bible_json"] or "{}") if project else {}
            visual_style = str(
                (bible_payload.get("world") or {}).get("visual_style_canonical") or ""
            )
            episode_no = int(episode["episode_no"])
            initial_characters: list[str] = []
            if current["characters"]:
                from app.multiview import complete_legacy_character_pack

                for name in current["characters"]:
                    _verify_supervisor_paid_authority(
                        cp,
                        stage="character_pack_completion",
                    )
                    pack = await complete_legacy_character_pack(
                        project_id, name, episode_no, visual_style,
                    )
                    if pack is None:
                        initial_characters.append(name)
            if initial_characters:
                from app.refs import generate_refs
                _verify_supervisor_paid_authority(
                    cp,
                    stage="character_reference_generation",
                )
                await generate_refs(
                    project_id,
                    only_characters=initial_characters,
                    resume=True,
                )
            # Portrait generation merges a newer Bible snapshot, so re-scan before
            # preparing scenes rather than reusing stale project JSON.
            _, current = _reference_asset_scan(episode_id)
            initial_scenes: list[str] = []
            if current["scenes"]:
                from app.multiview import complete_legacy_scene_pack

                for name in current["scenes"]:
                    _verify_supervisor_paid_authority(
                        cp,
                        stage="scene_pack_completion",
                    )
                    pack = await complete_legacy_scene_pack(
                        project_id, name, episode_no, visual_style,
                    )
                    if pack is None:
                        initial_scenes.append(name)
            if initial_scenes:
                from app.scenes import generate_scene_refs
                _verify_supervisor_paid_authority(
                    cp,
                    stage="scene_reference_generation",
                )
                await generate_scene_refs(
                    project_id,
                    only_scene=initial_scenes,
                    resume=True,
                )
            _, current = _reference_asset_scan(episode_id)
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

    if current["blockers"]:
        # 补齐动作已完成有界尝试；缺口转为输入风险，后续镜头使用已有锚点、
        # 关键帧或纯文本继续，不能把整集停在资产门禁。
        if run_id:
            evidence_repository.append_event(
                run_id,
                "VIDEO_REFERENCE_ASSET_PREP_FALLBACK",
                "warning",
                "参考资产补齐重试耗尽，继续使用当前可用产物",
                payload={"blockers": current["blockers"][:8]},
            )
    if run_id:
        evidence_repository.append_event(
            run_id,
            "VIDEO_REFERENCE_ASSET_PREP_COMPLETED",
            "info",
            "本集视频所需资产已就绪",
        )
    return current


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
            deadline_at=None,
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

    if cp.phase in TERMINAL_SUPERVISOR_PHASES:
        return cp

    initial_wall_cap = float(
        wall_clock_cap_s
        if wall_clock_cap_s is not None
        else (cp.budget.get("wall_clock_cap_s") or 4 * 3600)
    )
    cp.deadline_at = cp.deadline_at or ((cp.started_at or now()) + initial_wall_cap)
    if now() >= cp.deadline_at:
        return _deadline_closeout(cp, run_id=run_id, reason="VIDEO_WALL_CLOCK_EXCEEDED")

    grant: VideoCompletionGrant | None = None
    if cp.grant_id:
        try:
            grant = await asyncio.to_thread(
                validate_video_grant,
                cp.grant_id,
                episode_id=episode_id,
                storyboard_artifact_id=ep["storyboard_artifact_id"],
            )
            cp.budget["cap_cny"] = float(grant.budget_cap_cny)
            cp.coverage["fallback_quota"] = int(grant.max_fallback_shots)
            allow_fallback_adopt = grant.allow_fallback_adopt
            allow_storyboard_edit = grant.allow_storyboard_edit
            wall_clock_cap_s = float(grant.wall_clock_cap_s)
            cp.deadline_at = float(grant.deadline_at)
            cp.budget["wall_clock_cap_s"] = float(grant.wall_clock_cap_s)
        except GrantValidationError as exc:
            if cp.deadline_at and now() >= cp.deadline_at:
                return _deadline_closeout(cp, run_id=run_id, reason="VIDEO_WALL_CLOCK_EXCEEDED")
            cp.phase = "WAITING_AUTHORIZATION"
            cp.outcome = exc.code
            save_checkpoint(cp, run_id=run_id)
            return cp

    try:
        grant = await _ensure_supervisor_video_plan(cp)
        save_checkpoint(cp, run_id=run_id)
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

    try:
        await _prepare_episode_reference_assets(
            episode_id,
            cp=cp,
            run_id=run_id,
        )
    except GrantValidationError as exc:
        cp.phase = "WAITING_AUTHORIZATION"
        cp.outcome = exc.code
        save_checkpoint(cp, run_id=run_id)
        return cp
    except Exception as exc:  # noqa: BLE001 - 资产失败降级，不得中断整集覆盖
        cp.quality_target_missed = True
        save_checkpoint(cp, run_id=run_id)
        if run_id:
            evidence_repository.append_event(
                run_id,
                "VIDEO_REFERENCE_ASSET_PREP_FAILED",
                "warning",
                f"参考资产准备失败，继续使用当前可用资产生成真实模型视频：{str(exc)[:500]}",
            )

    wall_cap = float(wall_clock_cap_s if wall_clock_cap_s is not None else (cp.budget.get("wall_clock_cap_s") or 4 * 3600))
    if grant:
        wall_cap = float(grant.wall_clock_cap_s)
        cp.deadline_at = float(grant.deadline_at)
    else:
        cp.deadline_at = (cp.started_at or now()) + wall_cap

    while True:
        if cp.deadline_at and now() >= cp.deadline_at:
            return _deadline_closeout(cp, run_id=run_id, reason="VIDEO_WALL_CLOCK_EXCEEDED")
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

        # Budget top-ups remain visible, while every authored or plan drift is
        # re-evaluated instead of trusting a cached grant row.
        try:
            g = await _verify_supervisor_paid_authority_async(
                cp,
                stage="supervisor_tick",
            )
        except GrantValidationError as exc:
            cp.phase = (
                "CANCELLED" if exc.code == "GRANT_REVOKED"
                else "WAITING_AUTHORIZATION"
            )
            cp.outcome = exc.code
            save_checkpoint(cp, run_id=run_id)
            return cp
        if g:
            cp.budget["cap_cny"] = float(g.budget_cap_cny)
            wall_cap = float(g.wall_clock_cap_s)
            cp.deadline_at = float(g.deadline_at)
            cp.budget["wall_clock_cap_s"] = wall_cap
            allow_fallback_adopt = g.allow_fallback_adopt
            allow_storyboard_edit = g.allow_storyboard_edit
            grant = g

        cp.tick_no += 1
        cp.phase = "PLANNING_COVERAGE"
        _reconcile_terminal_continuity_blocks(episode_id, run_id=run_id)
        cap = float(cp.budget.get("cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY)
        fallback_quota = int(cp.coverage.get("fallback_quota") or 0)
        ledger = await _rebuild_budgeted_coverage_ledger_async(
            episode_id,
            cp=cp,
            fallback_quota=fallback_quota,
            budget_cap_cny=cap,
        )
        adopted_ready = (
            await asyncio.to_thread(
                _adopt_ready_candidates,
                ledger,
                run_id=run_id,
            )
            if _supervisor_checks_can_use_worker_thread()
            else _adopt_ready_candidates(ledger, run_id=run_id)
        )
        if adopted_ready:
            ledger = await _rebuild_budgeted_coverage_ledger_async(
                episode_id,
                cp=cp,
                fallback_quota=fallback_quota,
                budget_cap_cny=cap,
            )
        _merge_shot_state(cp, ledger)
        save_checkpoint(cp, run_id=run_id)

        if ledger.covered_within_quota():
            return _finalize_covered(cp, ledger, run_id=run_id)

        spent = float(ledger.cost_spent)
        if spent >= cap:
            return _deadline_closeout(
                cp, run_id=run_id, reason="VIDEO_BUDGET_EXHAUSTED_FALLBACK",
            )
        if cp.deadline_at and now() >= cp.deadline_at:
            return _deadline_closeout(cp, run_id=run_id, reason="VIDEO_WALL_CLOCK_EXCEEDED")
        if cp.repair_epoch > MAX_REPAIR_EPOCHS:
            return _deadline_closeout(cp, run_id=run_id, reason="REPAIR_EPOCHS_EXHAUSTED")

        if ledger.has_active_jobs() and not ledger.actionable():
            cp.phase = "OBSERVING"
            save_checkpoint(cp, run_id=run_id)
            await asyncio.sleep(cp.tick_interval_s)
            continue

        cp.phase = "EVALUATING"
        progressed = False
        budget_capacity_reached = False
        soft_cap = cap * FIRST_PASS_BUDGET_FRACTION
        per_shot_cap = (cap / max(1, ledger.shots_total)) * SHOT_BUDGET_MULTIPLIER

        # #region debug-point C:dispatch-batch
        with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"C","location":"app/video_supervisor.py:run_video_supervisor","msg":"[DEBUG] actionable batch","data":{"tick_no":cp.tick_no,"actionable_count":len(ledger.actionable()),"active_jobs":ledger.has_active_jobs(),"db_in_transaction":get_conn().in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
        # #endregion
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
                if not _has_dispatch_budget_capacity(
                    episode_id,
                    entry,
                    budget_cap_cny=cap,
                ):
                    budget_capacity_reached = True
                    break
                cp.phase = "DISPATCHING"
                try:
                    dispatched = await _dispatch_with_heartbeat_async(
                        entry,
                        episode_id=episode_id,
                        run_id=run_id,
                        cp=cp,
                        first=True,
                    )
                except GrantValidationError as exc:
                    cp.phase = "WAITING_AUTHORIZATION"
                    cp.outcome = exc.code
                    save_checkpoint(cp, run_id=run_id)
                    return cp
                if dispatched:
                    progressed = True
                # Narrative-authority request compilation is intentionally
                # thorough and synchronous. Yield after each dispatch so a
                # large first pass cannot starve HTTP and media workers.
                await asyncio.sleep(0)
                continue

            issues = _collect_issues(entry, run_id=run_id)
            model_rejected = any(
                not issue.repairable
                and (issue.evidence or {}).get("pause_state") == "PAUSED_EXTERNAL"
                for issue in issues
            )
            if model_rejected:
                entry.repair_level = "L6"
                entry.fallback_reason = "视频模型明确拒绝，已排除自动修复与付费重试"
                continue
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

            if max(entry.attempts_paid, entry.attempts_dispatched) >= entry.attempts_budgeted and plan.is_paid:
                continue

            if plan.strategy == "amend_storyboard":
                try:
                    amended = bool(
                        grant
                        and grant.allow_storyboard_edit
                        and await _amend_storyboard(
                            entry,
                            grant=grant,
                            plan=plan,
                            run_id=run_id,
                        )
                    )
                except GrantValidationError as exc:
                    cp.phase = "WAITING_AUTHORIZATION"
                    cp.outcome = exc.code
                    _merge_shot_state(cp, ledger)
                    save_checkpoint(cp, run_id=run_id)
                    return cp
                if amended:
                    cp.phase = "WAITING_HUMAN"
                    cp.outcome = "已创建分镜修改草稿；视频流水线已暂停，等待分镜台完整终态与人工重新确认"
                    _merge_shot_state(cp, ledger)
                    save_checkpoint(cp, run_id=run_id)
                    return cp
                if grant and grant.allow_storyboard_edit:
                    cp.phase = "WAITING_HUMAN"
                    cp.outcome = "AI 语义分镜修复候选未通过全链路验证，需要人工处理"
                    _merge_shot_state(cp, ledger)
                    save_checkpoint(cp, run_id=run_id)
                    return cp
                else:
                    cp.phase = "WAITING_AUTHORIZATION"
                    cp.outcome = "STORYBOARD_REPAIR_PROPOSAL_NOT_AUTHORIZED"
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
                       AND (? IS NULL OR owner_run_id=?)
                       ORDER BY created_at DESC LIMIT 1""",
                    (entry.shot_id, run_id, run_id),
                ).fetchone()
                if job and entry.no_charge_requeues < 2:
                    conn.execute(
                        "UPDATE jobs SET status='queued', retry_count=0, error=NULL, updated_at=? WHERE id=?",
                        (now(), job["id"]),
                    )
                    conn.commit()
                    entry.no_charge_requeues += 1
                    progressed = True
                    continue
                # 同一个失败任务最多免计费重排两次；之后创建新版本并受镜级派发上限约束。
                plan = plan.model_copy(update={
                    "level": "L1",
                    "strategy": "retake_directed",
                    "is_paid": True,
                    "reason": (plan.reason or "") + "；免计费重排已达上限，切换受控新版本",
                })
                if max(entry.attempts_paid, entry.attempts_dispatched) >= entry.attempts_budgeted:
                    continue
            cp.phase = "REPAIRING"
            if run_id:
                evidence_repository.append_event(
                    run_id, "VIDEO_REPAIR_PLAN_SELECTED", "info",
                    plan.reason, payload=cp.last_plan,
                )
            try:
                if plan.is_paid and not _has_dispatch_budget_capacity(
                    episode_id,
                    entry,
                    budget_cap_cny=cap,
                ):
                    budget_capacity_reached = True
                    break
                dispatched = await _dispatch_with_heartbeat_async(
                    entry,
                    episode_id=episode_id,
                    run_id=run_id,
                    cp=cp,
                    plan=plan,
                )
            except GrantValidationError as exc:
                cp.phase = "WAITING_AUTHORIZATION"
                cp.outcome = exc.code
                save_checkpoint(cp, run_id=run_id)
                return cp
            if dispatched:
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
            await asyncio.sleep(0)

        if budget_capacity_reached:
            observed = await _rebuild_coverage_ledger_async(
                episode_id,
                cp=cp,
                fallback_quota=int(cp.coverage.get("fallback_quota") or 0),
            )
            _merge_shot_state(cp, observed)
            if observed.has_active_jobs():
                cp.phase = "OBSERVING"
                cp.outcome = "VIDEO_BUDGET_CAPACITY_RESERVED_BY_ACTIVE_JOBS"
                save_checkpoint(cp, run_id=run_id)
                await asyncio.sleep(cp.tick_interval_s)
                continue
            cp.phase = "PAUSED_BUDGET"
            cp.outcome = "VIDEO_BUDGET_CAPACITY_REACHED"
            save_checkpoint(cp, run_id=run_id)
            return cp

        # 尝试预算耗尽后统一 best-effort 兜底；质量等级和旧 fallback quota
        # 只用于报告，不得让任何已有可播候选继续空置。
        if allow_fallback_adopt:
            # 刷新 ledger 状态到 cp
            _merge_shot_state(cp, ledger)
            ledger2 = await _rebuild_coverage_ledger_async(
                episode_id,
                cp=cp,
            )
            for entry in ledger2.exhausted_but_technically_ok():
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


def _mark_failed_closed(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    reason: str,
) -> VideoSupervisorCheckpoint:
    """连收口协议自身也失败时的最小安全终态。"""
    cp.phase = "FAILED_CLOSED"
    cp.outcome = "FAILED_CLOSED"
    cp.terminal_reason = reason
    cp.dispatch_fenced_at = cp.dispatch_fenced_at or now()
    cp.finished_at = now()
    cp.quality_target_missed = True
    try:
        _stop_supervised_video_jobs(cp.episode_id, run_id=run_id or cp.run_id, reason=reason)
    except Exception:  # noqa: BLE001
        pass
    try:
        save_checkpoint(cp, run_id=run_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        _release_episode_supervisor(cp.episode_id, run_id=run_id or cp.run_id)
    except Exception:  # noqa: BLE001
        # 最后一层仍尝试直接清掉假运行标记。
        conn = get_conn()
        conn.execute(
            """UPDATE episodes SET video_completion_mode='quick', active_video_run_id=NULL,
                      status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
               WHERE id=?""",
            (cp.episode_id,),
        )
        conn.commit()
    return cp


async def run_video_completion_resilient(
    episode_id: str,
    **kwargs: Any,
) -> VideoSupervisorCheckpoint:
    """控制面异常自动续跑；连续失败后仍执行候选收口并停止所有付费任务。"""
    run_id = kwargs.get("run_id")
    recoveries = 0
    while True:
        try:
            return await run_video_completion_supervisor(episode_id, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 控制面必须 fail closed，不能裸奔 Worker
            from app.observability.metrics import inc
            inc(
                "video_supervisor_failed_with_active_jobs_total",
                episode_id=episode_id,
                error_type=type(exc).__name__,
            )
            recoveries += 1
            cp = load_latest_checkpoint(episode_id)
            if run_id and (cp is None or cp.run_id != run_id):
                # A fresh run may fail before its first checkpoint is durable.
                # Never recover from an older execution epoch: its attempt
                # counters, budget and terminal fences belong to cleared work.
                cp = None
            cp = cp or VideoSupervisorCheckpoint(
                episode_id=episode_id,
                run_id=run_id,
                phase="RECOVERING_CONTROL_PLANE",
                started_at=now(),
            )
            cp.control_plane_recoveries = max(cp.control_plane_recoveries, recoveries)
            cp.phase = "RECOVERING_CONTROL_PLANE"
            cp.outcome = f"{type(exc).__name__}: {str(exc)[:500]}"
            if cp.deadline_at is None:
                wall_cap = float(kwargs.get("wall_clock_cap_s") or 4 * 3600)
                cp.deadline_at = (cp.started_at or now()) + wall_cap
            try:
                save_checkpoint(cp, run_id=run_id)
                if run_id:
                    evidence_repository.append_event(
                        run_id,
                        "VIDEO_CONTROL_PLANE_RECOVERING",
                        "error",
                        f"Supervisor 控制面异常，自动恢复 {recoveries}/{CONTROL_PLANE_MAX_RECOVERIES}",
                        payload={"error_type": type(exc).__name__, "message": str(exc)[:1000]},
                    )
            except Exception:  # noqa: BLE001 — 保留原异常，继续进入 fail-closed 路径
                pass
            if recoveries <= CONTROL_PLANE_MAX_RECOVERIES and now() < (cp.deadline_at or now()):
                kwargs["resume"] = True
                await asyncio.sleep(min(5.0, float(recoveries)))
                continue
            try:
                return _deadline_closeout(
                    cp,
                    run_id=run_id,
                    reason="CONTROL_PLANE_FAILURE",
                )
            except Exception as close_exc:  # noqa: BLE001
                return _mark_failed_closed(
                    cp,
                    run_id=run_id,
                    reason=f"CONTROL_PLANE_CLOSEOUT_FAILED: {type(close_exc).__name__}: {close_exc}",
                )


async def reconcile_stale_video_supervisors() -> int:
    """接管 heartbeat 超时但内存 task 仍占位的 Supervisor。"""
    from app import task_registry
    from app.errors import log_error
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.observability.metrics import inc

    conn = get_conn()
    rows = conn.execute(
        """SELECT e.id, e.project_id, e.storyboard_artifact_id, e.active_video_run_id,
                  r.updated_at AS run_updated_at
           FROM episodes e
           LEFT JOIN workflow_runs r ON r.id=e.active_video_run_id
           WHERE e.video_completion_mode='complete' AND e.active_video_run_id IS NOT NULL"""
    ).fetchall()
    recovered = 0

    async def reconcile_one(row) -> bool:
        episode_id = row["id"]
        cp = load_latest_checkpoint(episode_id)
        if cp is None or cp.phase in TERMINAL_SUPERVISOR_PHASES or cp.phase in {
            "PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_AUTHORIZATION", "WAITING_HUMAN",
        }:
            return False
        heartbeat = max(float(cp.last_heartbeat_at or 0), float(row["run_updated_at"] or 0))
        task_running = task_registry.active("video_completion", episode_id)
        # A checkpoint created before absolute deadlines existed is a legacy
        # incident.  Do not mutate it automatically: the repair-preview +
        # explicit confirmation path owns that migration.
        if not task_running and cp.deadline_at is None:
            return False
        if heartbeat and now() - heartbeat <= SUPERVISOR_HEARTBEAT_STALE_S:
            return False
        try:
            _verify_supervisor_paid_authority(cp, stage="watchdog_takeover")
        except GrantValidationError as exc:
            cp.phase = "WAITING_AUTHORIZATION"
            cp.outcome = exc.code
            save_checkpoint(cp, run_id=cp.run_id)
            return False
        current = conn.execute(
            """SELECT e.active_video_run_id, e.video_completion_mode,
                      r.updated_at AS run_updated_at
                 FROM episodes e
                 LEFT JOIN workflow_runs r ON r.id=e.active_video_run_id
                WHERE e.id=?""",
            (episode_id,),
        ).fetchone()
        latest_cp = load_latest_checkpoint(episode_id)
        current_heartbeat = max(
            float((latest_cp.last_heartbeat_at if latest_cp else 0) or 0),
            float((current["run_updated_at"] if current else 0) or 0),
        )
        if (
            current is None
            or current["video_completion_mode"] != "complete"
            or current["active_video_run_id"] != row["active_video_run_id"]
            or (
                current_heartbeat
                and now() - current_heartbeat <= SUPERVISOR_HEARTBEAT_STALE_S
            )
        ):
            return False
        task_running = task_registry.active("video_completion", episode_id)
        if task_running:
            await task_registry.cancel_and_wait("video_completion", episode_id)
        recorder = WorkflowRecorder.create(
            workflow_type="episode_video_completion",
            scope_type="episode",
            scope_id=episode_id,
            input_fingerprint=fingerprint(
                row["storyboard_artifact_id"], cp.grant_id, "watchdog_closeout",
            ),
            requested_by="system",
            trigger_type="watchdog",
            policy_snapshot={"supervisor": "video_completion", "watchdog_takeover": True},
            deadline_at=cp.deadline_at,
            parent_run_id=row["active_video_run_id"],
        )
        recorder.start()
        claimed = conn.execute(
            """UPDATE episodes SET active_video_run_id=?, status='generating'
               WHERE id=? AND active_video_run_id=?""",
            (recorder.run_id, episode_id, row["active_video_run_id"]),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            recorder.cancel("检测到更新的补齐运行，watchdog 放弃接管")
            return False
        conn.commit()
        cp.run_id = recorder.run_id
        cp.phase = "RECOVERING_CONTROL_PLANE"
        cp.control_plane_recoveries += 1
        try:
            result = _deadline_closeout(
                cp,
                run_id=recorder.run_id,
                reason="SUPERVISOR_HEARTBEAT_STALE",
            )
            recorder.partial(result.outcome or result.phase)
        except Exception as exc:  # noqa: BLE001
            _mark_failed_closed(
                cp,
                run_id=recorder.run_id,
                reason=f"WATCHDOG_CLOSEOUT_FAILED: {type(exc).__name__}: {exc}",
            )
            recorder.fail(exc)
        inc("video_supervisor_watchdog_takeover_total", episode_id=episode_id)
        return True

    for row in rows:
        episode_id = row["id"]
        try:
            if await reconcile_one(row):
                recovered += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            log_error(
                exc,
                action="video_supervisor.watchdog_episode",
                context={
                    "episode_id": episode_id,
                    "active_video_run_id": row["active_video_run_id"],
                },
                meta={"stage": "video_supervisor_watchdog", "isolation": "episode"},
            )
            inc(
                "video_supervisor_watchdog_episode_error_total",
                episode_id=episode_id,
                error_type=type(exc).__name__,
            )
    return recovered


async def video_supervisor_watchdog_loop(interval_s: float = 30.0) -> None:
    while True:
        try:
            # 轻量级业务巡检始终运行，不要求用户显式开启全片补齐授权。
            # 它只恢复已请求任务或降级孤儿连续性，不会自行创建新的付费范围。
            from app import worker
            worker.reconcile_stalled_video_jobs()
            await reconcile_stale_video_supervisors()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — watchdog 自身不得因单集坏数据退出
            from app.errors import log_error
            from app.observability.metrics import inc
            log_error(
                exc,
                action="video_supervisor.watchdog_loop",
                context={"interval_s": interval_s},
                meta={"stage": "video_supervisor_watchdog", "isolation": "loop"},
            )
            inc(
                "video_supervisor_watchdog_loop_error_total",
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(max(5.0, min(float(interval_s), 60.0)))


def preview_video_completion_repair(episode_id: str) -> dict[str, Any]:
    """只读预演遗留/崩溃 run 的收口结果，不创建校验、不改 adopted、不停 job。"""
    conn = get_conn()
    cp = load_latest_checkpoint(episode_id)
    ep = conn.execute(
        "SELECT active_video_run_id, video_completion_mode, status FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not ep:
        raise ValueError(f"剧集不存在：{episode_id}")
    shots = conn.execute(
        "SELECT id, shot_no, adopted_version_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    plan: list[dict[str, Any]] = []
    for shot in shots:
        candidates: list[dict[str, Any]] = []
        versions = conn.execute(
            """SELECT id, version_no, qa_json, technical_validation_json, adoption_reason
               FROM shot_versions WHERE shot_id=? AND status='succeeded' ORDER BY version_no""",
            (shot["id"],),
        ).fetchall()
        for version in versions:
            try:
                technical = json.loads(version["technical_validation_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                technical = {}
            if not technical.get("passed"):
                continue
            try:
                qa = json.loads(version["qa_json"] or "{}")
                score = float(qa.get("overall")) if qa.get("overall") is not None else -1.0
            except (TypeError, ValueError, json.JSONDecodeError):
                score = -1.0
            candidates.append({
                "version_id": version["id"],
                "version_no": int(version["version_no"]),
                "qa_overall": score,
            })
        candidates.sort(key=lambda item: (item["qa_overall"], item["version_no"]), reverse=True)
        adopted_valid = any(
            candidate["version_id"] == shot["adopted_version_id"] for candidate in candidates
        )
        if adopted_valid:
            action = "retain_adopted"
            selected = shot["adopted_version_id"]
        elif candidates:
            action = "adopt_best_technical_candidate"
            selected = candidates[0]["version_id"]
        else:
            action = "mark_missing"
            selected = None
        blocked_job = conn.execute(
            """SELECT id, after_shot_id FROM jobs
               WHERE shot_id=? AND kind='video'
                 AND status IN ('queued','running','waiting_provider','waiting_retry','waiting','waiting_human')
               ORDER BY created_at DESC LIMIT 1""",
            (shot["id"],),
        ).fetchone()
        plan.append({
            "shot_no": int(shot["shot_no"]),
            "shot_id": shot["id"],
            "action": action,
            "selected_version_id": selected,
            "candidates": candidates,
            "active_job_id": blocked_job["id"] if blocked_job else None,
            "blocked_by_shot_id": blocked_job["after_shot_id"] if blocked_job else None,
        })
    return {
        "dry_run": True,
        "episode_id": episode_id,
        "active_video_run_id": ep["active_video_run_id"],
        "video_completion_mode": ep["video_completion_mode"],
        "episode_status": ep["status"],
        "checkpoint_phase": cp.phase if cp else None,
        "checkpoint_started_at": cp.started_at if cp else None,
        "checkpoint_deadline_at": cp.deadline_at if cp else None,
        "would_adopt": [item for item in plan if item["action"] == "adopt_best_technical_candidate"],
        "would_retain": [item for item in plan if item["action"] == "retain_adopted"],
        "would_mark_missing": [item for item in plan if item["action"] == "mark_missing"],
        "shots": plan,
        "will_start_generation": False,
        "will_delete_media": False,
    }


def recover_video_completion_runs() -> int:
    """服务重启后恢复未完成的视频补齐 Supervisor。"""
    from app import task_registry
    from app.errors import log_error
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.observability.metrics import inc

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
        """SELECT id, project_id, status AS episode_status, active_video_run_id,
                  video_completion_mode, storyboard_artifact_id
           FROM episodes WHERE video_completion_mode='complete'"""
    ).fetchall()
    resumed = 0

    def recover_one(row) -> bool:
        episode_id = row["id"]
        if task_registry.active("video_completion", episode_id):
            return False
        cp = load_latest_checkpoint(episode_id)
        if cp is None:
            return False
        legacy_without_deadline = cp.deadline_at is None
        if legacy_without_deadline and cp.grant_id:
            prior_grant = get_video_grant(cp.grant_id)
            if prior_grant:
                cp.deadline_at = float(prior_grant.deadline_at)
        if cp.phase in TERMINAL_SUPERVISOR_PHASES or cp.phase in {"WAITING_AUTHORIZATION", "WAITING_HUMAN"}:
            return False
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
            return False
        deadline_due = bool(cp.deadline_at and now() >= cp.deadline_at)
        # 旧版本事故 run 没有持久化 deadline；只提供 dry-run，禁止启动时静默改用户现场数据。
        if legacy_without_deadline and deadline_due:
            return False
        if cp.grant_id and not deadline_due:
            try:
                _verify_supervisor_paid_authority(cp, stage="service_restart_resume")
            except GrantValidationError as exc:
                cp.phase = "WAITING_AUTHORIZATION"
                cp.outcome = exc.code
                save_checkpoint(cp)
                return False

        recorder = WorkflowRecorder.create(
            workflow_type="episode_video_completion",
            scope_type="episode",
            scope_id=episode_id,
            input_fingerprint=fingerprint(row["storyboard_artifact_id"], cp.grant_id),
            requested_by="system",
            trigger_type="resume",
            policy_snapshot={"supervisor": "video_completion", "resume": True},
            deadline_at=cp.deadline_at,
            parent_run_id=row["active_video_run_id"],
        )

        async def _task(eid=episode_id, rid=recorder.run_id, gid=cp.grant_id, rec=recorder):
            # Recovery tasks are spawned inside the FastAPI lifespan. Let the
            # lifespan publish startup completion before reconstructing large
            # immutable screenplay/storyboard authority models.
            await asyncio.sleep(1.0)
            rec.start()
            try:
                result = await run_video_completion_resilient(
                    eid, resume=True, grant_id=gid, run_id=rid,
                )
                if result.phase == "SUCCEEDED_COVERED":
                    rec.succeed(result.outcome or "SUCCEEDED_COVERED")
                elif result.phase in {"WAITING_AUTHORIZATION", "WAITING_HUMAN", "PAUSED_EXTERNAL", "PAUSED_BUDGET"}:
                    rec.partial(result.outcome or result.phase)
                elif result.phase == "CANCELLED":
                    rec.cancel()
                else:
                    rec.partial(result.phase)
            except asyncio.CancelledError:
                if task_registry.shutdown_in_progress():
                    rec.pause_external("服务重启，全片视频补齐等待自动恢复")
                else:
                    rec.cancel()
                raise
            except Exception as exc:
                rec.fail(exc)
                raise

        claimed = conn.execute(
            """UPDATE episodes SET active_video_run_id=?, status='generating'
               WHERE id=? AND active_video_run_id IS ?""",
            (recorder.run_id, episode_id, row["active_video_run_id"]),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            recorder.cancel("恢复启动权已变化，当前运行未启动")
            return False
        conn.commit()
        coro = _task()
        try:
            task_registry.spawn(
                "video_completion", episode_id, coro, project_id=row["project_id"],
            )
        except Exception as exc:
            coro.close()
            try:
                recorder.start()
                recorder.fail(exc)
            except Exception:  # noqa: BLE001
                pass
            conn.execute(
                """UPDATE episodes SET active_video_run_id=?, status=?
                   WHERE id=? AND active_video_run_id=?""",
                (
                    row["active_video_run_id"],
                    row["episode_status"],
                    episode_id,
                    recorder.run_id,
                ),
            )
            conn.commit()
            raise
        return True

    for row in rows:
        episode_id = row["id"]
        try:
            if recover_one(row):
                resumed += 1
        except Exception as exc:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            log_error(
                exc,
                action="video_supervisor.recover_episode",
                context={
                    "episode_id": episode_id,
                    "active_video_run_id": row["active_video_run_id"],
                },
                meta={"stage": "video_supervisor_recovery", "isolation": "episode"},
            )
            inc(
                "video_supervisor_recovery_episode_error_total",
                episode_id=episode_id,
                error_type=type(exc).__name__,
            )
    return resumed
