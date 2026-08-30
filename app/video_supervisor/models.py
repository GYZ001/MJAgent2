"""video_supervisor 的 Pydantic 数据模型：覆盖台账条目、checkpoint、分镜修复提案。"""
from __future__ import annotations

import json

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas import Shot
from app.video_repair_router import RepairLevel

from .constants import MIN_ATTEMPTS_PER_SHOT, SUPERVISOR_TICK_INTERVAL_S, SupervisorPhase



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
    depends_on_shot_id: str | None = None
    dependency_ready: bool = True
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
            if e.depends_on_shot_id and not e.dependency_ready:
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
