"""集级分镜 Supervisor AgentLoop。

以「整集 hard gate 通过并发布为待人工确认」为唯一成功条件；
业务校验失败进入 Repair Router，不得以 PARTIAL/scripted+error 结束。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app import config, errors
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact, Issue
from app.repair_router import (
    RepairPlan,
    bump_fingerprint_count,
    route_issues,
)
from app.schemas import (
    Bible,
    CognitiveBridgePlan,
    EpisodeScreenplay,
    Shot,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
    StoryboardScenePack,
    extract_json,
)
from app.stages import (
    StageError,
    StoryboardShotDraft,
    ensure_storyboard_scene_contexts,
    generate_storyboard_scene_pack,
    generate_storyboard_next_shot,
    generate_storyboard_outline,
    normalize_storyboard_direction_fields,
    normalize_storyboard_shot_candidate,
    storyboard_planning_bible,
    storyboard_shot_authority_context,
    validate_storyboard_visual_identity_contract,
)

SupervisorPhase = Literal[
    "CREATED",
    "PREFLIGHT",
    "PLANNING_OUTLINE",
    "VALIDATING_OUTLINE",
    "GENERATING_SHOTS",
    "VALIDATING_EPISODE",
    "REPAIRING",
    "SUCCEEDED",
    "PAUSED_EXTERNAL",
    "PAUSED_BUDGET",
    "WAITING_AUTHORIZATION",
    "WAITING_HUMAN",
    "CANCELLED",
]

CHECKPOINT_TYPE = "storyboard_supervisor_checkpoint"
STORYBOARD_REPAIR_SAFETY_LIMIT = 24
STORYBOARD_REPAIR_ACTIVATION_LIMIT = 6
STORYBOARD_REPAIR_MAX_FINGERPRINT_ATTEMPTS = 4
STORYBOARD_REPAIR_PLANNER_VERSION = "storyboard-repair-v2"


_PHASE_LABELS: dict[str, str] = {
    "CREATED": "已创建",
    "PREFLIGHT": "前置资产预检",
    "PLANNING_OUTLINE": "规划分镜大纲",
    "VALIDATING_OUTLINE": "校验分镜大纲",
    "GENERATING_SHOTS": "按场景批量生成",
    "VALIDATING_EPISODE": "整集校验",
    "REPAIRING": "定向修复",
    "SUCCEEDED": "已完成",
    "PAUSED_EXTERNAL": "外部服务暂停",
    "PAUSED_BUDGET": "预算暂停",
    "WAITING_AUTHORIZATION": "等待授权",
    "WAITING_HUMAN": "等待人工",
    "CANCELLED": "已取消",
}


def _phase_label(phase: str) -> str:
    return _PHASE_LABELS.get(phase, phase or "未知阶段")


class SupervisorCheckpoint(BaseModel):
    episode_id: str
    phase: SupervisorPhase = "CREATED"
    repair_epoch: int = 0
    planner_version: str = ""
    activation_no: int = 0
    activation_attempt_count: int = 0
    outline_artifact_id: str | None = None
    validated_shot_artifact_ids: list[str] = Field(default_factory=list)
    validated_prefix_end: int = 0
    next_shot_no: int = 1
    expected_total: int = 0
    coverage: dict[str, list[str]] = Field(default_factory=dict)
    pending_issue_ids: list[str] = Field(default_factory=list)
    issue_fingerprint_counts: dict[str, int] = Field(default_factory=dict)
    issue_strategy_history: dict[str, list[str]] = Field(default_factory=dict)
    input_versions: dict[str, str | None] = Field(default_factory=dict)
    last_repair: dict[str, Any] | None = None
    repair_candidate_shots: list[dict[str, Any]] = Field(default_factory=list)
    scene_pack_candidates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    legacy_repair_audit: dict[str, Any] = Field(default_factory=dict)
    outcome: str | None = None  # SUCCEEDED_READY_FOR_CONFIRM


def _storyboard_scene_pack_batches(
    outline: StoryboardOutline,
    *,
    max_shots: int | None = None,
) -> list[dict[str, Any]]:
    """Partition each scene into bounded, contiguous model outputs."""
    chunk_size = max(
        1,
        int(max_shots or config.STORYBOARD_SCENE_PACK_MAX_SHOTS),
    )
    batches: list[dict[str, Any]] = []
    for context in outline.scene_contexts:
        scene_briefs = [
            brief
            for brief in outline.shots
            if brief.scene_id == context.scene_id
        ]
        for offset in range(0, len(scene_briefs), chunk_size):
            chunk = scene_briefs[offset:offset + chunk_size]
            if not chunk:
                continue
            start = min(int(item.shot_no) for item in chunk)
            end = max(int(item.shot_no) for item in chunk)
            batches.append({
                "key": (
                    context.scene_id
                    if len(scene_briefs) <= chunk_size
                    else f"{context.scene_id}:{start}-{end}"
                ),
                "context": context,
                "shot_nos": {
                    int(item.shot_no)
                    for item in chunk
                },
                "start": start,
                "end": end,
            })
    return batches


def _migrate_checkpoint(cp: SupervisorCheckpoint) -> SupervisorCheckpoint:
    """Upgrade legacy runaway checkpoints without erasing their audit counters."""
    if cp.planner_version == STORYBOARD_REPAIR_PLANNER_VERSION:
        return cp
    cp.legacy_repair_audit = {
        **cp.legacy_repair_audit,
        "repair_epoch": int(cp.repair_epoch or 0),
        "issue_fingerprint_counts": dict(cp.issue_fingerprint_counts or {}),
        "issue_strategy_history": dict(cp.issue_strategy_history or {}),
        "migrated_from": cp.planner_version or "legacy",
    }
    # ``repair_epoch`` remains the lifetime audit count.  The corrupt legacy
    # per-fingerprint counters must not consume the first bounded v2 activation.
    cp.issue_fingerprint_counts = {}
    cp.issue_strategy_history = {}
    cp.activation_attempt_count = 0
    cp.repair_candidate_shots = []
    cp.planner_version = STORYBOARD_REPAIR_PLANNER_VERSION
    if cp.outcome == "PAUSED_REPAIR_SAFETY_LIMIT":
        cp.outcome = "WAITING_RETRY_LEGACY_MIGRATED"
    return cp


def _begin_repair_activation(cp: SupervisorCheckpoint) -> SupervisorCheckpoint:
    """Open a fresh bounded repair round without erasing lifetime audit data."""
    completed_activations = list(
        cp.legacy_repair_audit.get("completed_activation_histories") or []
    )
    if cp.issue_strategy_history:
        completed_activations.append({
            "activation_no": int(cp.activation_no or 0),
            "attempt_count": int(cp.activation_attempt_count or 0),
            "issue_strategy_history": dict(cp.issue_strategy_history),
        })
        cp.legacy_repair_audit = {
            **cp.legacy_repair_audit,
            "completed_activation_histories": completed_activations[-20:],
        }
    cp.activation_no = int(cp.activation_no or 0) + 1
    cp.activation_attempt_count = 0
    cp.issue_fingerprint_counts = {}
    # Strategy attempts are activation-local.  Reusing the prior activation's
    # map makes a user-authorized retry exhaust before its first provider call.
    cp.issue_strategy_history = {}
    cp.repair_candidate_shots = []
    if cp.last_repair:
        cp.last_repair = {**cp.last_repair, "status": "superseded_by_new_activation"}
    cp.outcome = None
    return cp


def prepare_published_storyboard_repair(
    episode_id: str,
    issue_messages: list[str],
) -> SupervisorCheckpoint:
    """Create an isolated repair window for a published, unconfirmed board."""
    conn = get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise StageError("分镜修复", ["剧集不存在"])
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if not rows:
        raise StageError("分镜修复", ["当前没有可建立修订候选的正式分镜"])
    from app.domain.storyboard_ops import _board_from_shot_rows

    board = _board_from_shot_rows(rows, int(episode["episode_no"] or 1))
    checkpoint = load_latest_checkpoint(episode_id) or SupervisorCheckpoint(
        episode_id=episode_id,
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        validated_prefix_end=len(board.shots),
        next_shot_no=len(board.shots) + 1,
        expected_total=len(board.shots),
        input_versions={
            "screenplay_artifact_id": episode["screenplay_artifact_id"],
        },
    )
    checkpoint = _begin_repair_activation(checkpoint)
    checkpoint.validated_prefix_end = len(board.shots)
    checkpoint.next_shot_no = len(board.shots) + 1
    checkpoint.expected_total = max(
        int(checkpoint.expected_total or 0),
        len(board.shots),
    )
    outline = None
    if episode["storyboard_outline_json"]:
        try:
            outline = StoryboardOutline.model_validate_json(
                episode["storyboard_outline_json"]
            )
        except (TypeError, ValueError):
            outline = None

    repair_screenplay = None
    repair_bible = None
    narrative_repair_active = None
    if outline is not None:
        from app.domain.common import _project_bible_or_placeholder
        from app.domain.video_ops import (
            evaluate_storyboard_for_confirmation,
        )
        from app.production.screenplay_authority import (
            resolve_downstream_screenplay,
        )

        repair_context = resolve_downstream_screenplay(
            episode_id,
            conn=conn,
        )
        repair_screenplay = repair_context.screenplay
        narrative_repair_active = (
            repair_context.narrative_authority_required
        )
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?",
            (episode["project_id"],),
        ).fetchone()
        repair_bible = _project_bible_or_placeholder(project)
        current_evaluation = evaluate_storyboard_for_confirmation(
            dict(episode),
            board,
            repair_screenplay,
            repair_bible,
            has_real_bible=bool(
                project and str(project["bible_json"] or "").strip()
            ),
            record_metrics=False,
            allow_evidence_refinalize=True,
        )
        # Preview payloads intentionally cap display detail. They prove the
        # user's intent and snapshot, but the server-side current evaluation
        # owns the complete repair target set.
        issue_messages = list(dict.fromkeys([
            *current_evaluation.errors,
            *(
                str(message)
                for message in issue_messages
                if str(message).strip()
            ),
        ]))

    explicit_targets = {
        int(match.group(1))
        for message in issue_messages
        for match in re.finditer(
            r"(?:shot_no\s*=\s*|第\s*|镜头\s*)(\d+)\s*镜?",
            str(message or ""),
            re.I,
        )
    }
    local_scope = len(explicit_targets) == 1
    plan = route_issues(
        issue_messages,
        validated_prefix_end=len(board.shots),
        semantic_diagnosis={
            "scope": "current_shot" if local_scope else "adjacent_window",
            "selected_strategy": "repair_current" if local_scope else "repair_window",
            "selection_reason": (
                "发布后门禁问题已定位到单个正式镜头"
                if local_scope else
                "发布后门禁问题需要在相邻正式镜头窗口中隔离修复"
            ),
            "execution_verified": True,
        },
    )
    checkpoint = _apply_repair(
        checkpoint,
        plan,
        conn,
        episode_id,
        list(board.shots),
        outline,
        repair_screenplay=repair_screenplay,
        narrative_repair_active=narrative_repair_active,
    )
    if not _repair_is_pending(checkpoint):
        raise StageError(
            "分镜修复",
            ["未能为发布后门禁问题建立隔离修订候选"],
        )
    candidate_outline = _repair_outline_for_checkpoint(
        checkpoint,
        outline,
    )
    if (
        candidate_outline is not None
        and repair_screenplay is not None
        and repair_bible is not None
        and repair_screenplay.narrative_plan is not None
    ):
        from app.narrative_outline import (
            normalize_narrative_storyboard_outline,
        )

        relation_repairs = normalize_narrative_storyboard_outline(
            candidate_outline,
            repair_screenplay,
            bible=repair_bible,
            preserve_shot_ids=True,
        )
        checkpoint.last_repair = {
            **(checkpoint.last_repair or {}),
            "candidate_outline": candidate_outline.model_dump(mode="json"),
            "relation_migration_count": len(relation_repairs),
        }
    save_checkpoint(checkpoint)
    return checkpoint


def _withdraw_legacy_failed_publication(
    conn,
    episode_id: str,
    episode,
    cp: SupervisorCheckpoint,
) -> bool:
    """Withdraw pointers created by the legacy failed-gate fallback.

    The immutable artifact and certificate remain as audit evidence, but they
    must not stay addressable as the episode's current published storyboard.
    """
    if cp.outcome != "SUCCEEDED_GATE_RETRY_EXHAUSTED_FALLBACK":
        return False
    artifact_id = (
        episode["published_storyboard_artifact_id"]
        or episode["storyboard_artifact_id"]
    )
    revision_id = episode["storyboard_production_revision_id"]
    reason = "旧版 Supervisor 在整集门禁失败时误发布，现已撤回并等待局部修复"
    conn.execute(
        """UPDATE episodes SET storyboard_artifact_id=NULL,
                  working_storyboard_artifact_id=NULL,
                  published_storyboard_artifact_id=NULL,
                  storyboard_completion_certificate_id=NULL,
                  storyboard_production_revision_id=NULL,
                  storyboard_warning=?
           WHERE id=?""",
        (reason, episode_id),
    )
    if revision_id:
        conn.execute(
            "UPDATE production_revisions SET status='superseded',published_artifact_id=NULL "
            "WHERE id=?",
            (revision_id,),
        )
    if artifact_id:
        conn.execute(
            "UPDATE artifacts SET status='stale',stale_reason=? WHERE id=? AND status!='rejected'",
            (reason, artifact_id),
        )
    conn.commit()
    if artifact_id:
        evidence_repository.invalidate_descendants(artifact_id, reason)
    return True


def _is_retryable_external_error(exc: BaseException) -> bool:
    """Recognize provider failures even after an orchestration layer wraps them."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if bool(getattr(current, "retryable", False)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _pause_for_external_error(
    cp: SupervisorCheckpoint,
    conn,
    episode_id: str,
    exc: BaseException,
    *,
    run_id: str | None,
    action: str,
) -> SupervisorCheckpoint:
    """Persist a safe resumable boundary for a temporary provider failure."""
    public = errors.record_and_format(
        exc,
        action=action,
        context={"episode_id": episode_id},
    )
    note = f"外部依赖暂不可用，已保留分镜检查点，可稍后继续任务：{public}"
    cp.phase = "PAUSED_EXTERNAL"
    cp.outcome = "PAUSED_PROVIDER_UNAVAILABLE"
    save_checkpoint(cp, run_id=run_id)
    conn.execute(
        "UPDATE episodes SET status='scripting', script_error=? WHERE id=?",
        (note[:800], episode_id),
    )
    conn.commit()
    if run_id:
        evidence_repository.append_event(
            run_id,
            "SUPERVISOR_PAUSED",
            "warning",
            "外部服务不可用，已在安全检查点暂停",
            payload={"error_type": type(exc).__name__, "retryable": True},
        )
    return cp


def _current_storyboard_projection_has_material(conn, episode_id: str, ep) -> bool:
    """Check current storyboard ownership without considering audit checkpoints."""
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
    ).fetchone()["c"])
    return bool(
        shot_count
        or ep["storyboard_outline_json"]
        or ep["storyboard_artifact_id"]
        or ep["working_storyboard_artifact_id"]
        or ep["published_storyboard_artifact_id"]
        or ep["storyboard_production_revision_id"]
        or ep["storyboard_completion_certificate_id"]
    )


def load_latest_checkpoint(episode_id: str) -> SupervisorCheckpoint | None:
    try:
        from app.production.revision import get_active_production_revision

        revision = get_active_production_revision(episode_id, "storyboard")
        raw_checkpoint = dict(revision.checkpoint_json or {}) if revision else {}
        raw_supervisor = raw_checkpoint.get("supervisor_checkpoint")
        if isinstance(raw_supervisor, dict) and raw_supervisor:
            return _migrate_checkpoint(SupervisorCheckpoint.model_validate(raw_supervisor))
    except (TypeError, ValueError):
        pass
    conn = get_conn()
    row = conn.execute(
        """SELECT id, content_json FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC LIMIT 1""",
        (CHECKPOINT_TYPE, episode_id),
    ).fetchone()
    if not row:
        return None
    try:
        raw = json.loads(row["content_json"] or "{}")
        return _migrate_checkpoint(SupervisorCheckpoint.model_validate(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def save_checkpoint(cp: SupervisorCheckpoint, *, run_id: str | None = None) -> str:
    cp = _migrate_checkpoint(cp)
    if run_id:
        fence_conn = get_conn()
        run_row = fence_conn.execute(
            "SELECT status FROM workflow_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        owner = fence_conn.execute(
            "SELECT active_storyboard_run_id FROM episodes WHERE id=?",
            (cp.episode_id,),
        ).fetchone()
        if (
            run_row
            and (
                run_row["status"] not in {"CREATED", "RUNNING"}
                or not owner
                or owner["active_storyboard_run_id"] != run_id
            )
        ):
            current = fence_conn.execute(
                """SELECT id FROM artifacts
                     WHERE type=? AND scope_type='episode' AND scope_id=?
                       AND status IN ('candidate','validated','approved')
                     ORDER BY created_at DESC,version DESC LIMIT 1""",
                (CHECKPOINT_TYPE, cp.episode_id),
            ).fetchone()
            return str(current["id"]) if current else ""
    payload = cp.model_dump(mode="json")
    payload_hash = evidence_repository.content_hash(payload)
    # The production revision owns the current mutable recovery point.  Audit
    # artifacts are appended only when material state changed.
    try:
        from app.production.revision import (
            get_active_production_revision,
            get_production_revision,
            save_checkpoint as save_revision_checkpoint,
        )

        revision = get_active_production_revision(cp.episode_id, "storyboard")
        if revision is None:
            pointer = get_conn().execute(
                "SELECT storyboard_production_revision_id FROM episodes WHERE id=?",
                (cp.episode_id,),
            ).fetchone()
            revision_id = pointer["storyboard_production_revision_id"] if pointer else None
            revision = get_production_revision(revision_id) if revision_id else None
        if revision:
            revision_checkpoint = dict(revision.checkpoint_json or {})
            revision_checkpoint["supervisor_checkpoint"] = payload
            revision_checkpoint["activation_no"] = cp.activation_no
            revision_checkpoint["activation_attempt_count"] = cp.activation_attempt_count
            revision_checkpoint["lifetime_repair_count"] = cp.repair_epoch
            revision_checkpoint["phase"] = cp.phase
            revision_checkpoint["yield_reason"] = cp.outcome
            save_revision_checkpoint(revision.id, revision_checkpoint)
    except Exception:  # noqa: BLE001 - legacy databases remain readable
        pass

    conn = get_conn()
    latest = conn.execute(
        """SELECT id,content_hash FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC LIMIT 1""",
        (CHECKPOINT_TYPE, cp.episode_id),
    ).fetchone()
    if latest and latest["content_hash"] == payload_hash:
        return str(latest["id"])
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type=CHECKPOINT_TYPE,
        scope_type="episode",
        scope_id=cp.episode_id,
        status="validated",
        trust_level="T2",
        content=payload,
        contract_version=get_contract("storyboard").version,
    ))
    evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="storyboard_supervisor",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"phase": cp.phase, "repair_epoch": cp.repair_epoch, "run_id": run_id},
        ),
    )
    if run_id:
        evidence_repository.append_event(
            run_id,
            "STORYBOARD_SUPERVISOR_CHECKPOINT",
            "info",
            f"检查点：{_phase_label(cp.phase)}（已通过 {cp.validated_prefix_end} 镜）",
            payload=cp.model_dump(mode="json"),
        )
    return artifact["id"]


def _blocker_messages(draft) -> list[str]:
    residual = list(getattr(draft, "residual_errors", []) or [])
    disposition = getattr(draft, "disposition", None)
    issues = getattr(draft, "residual_issues", None) or []
    structural = [
        i.get("message", "") for i in issues
        if isinstance(i, dict) and i.get("severity") == "blocker"
        and _is_structural_storyboard_issue(i.get("category"))
    ]
    if structural:
        return structural
    if disposition == "NEEDS_REPLAN":
        # Phase 3 QA score-only: quality/capacity replanning requests are
        # warnings for the report, not a reason to delete/split/replan shots.
        return []
    blockers = [
        i.get("message", "") for i in issues
        if isinstance(i, dict) and i.get("severity") == "blocker"
        and _is_structural_storyboard_issue(i.get("category"))
    ]
    if blockers:
        return blockers
    # warning-only：允许继续（非 blocker）
    if disposition == "WARNING" and residual:
        return []
    if disposition not in {"PASS", "WARNING", None}:
        return []
    return []


def _stop_after_exhausted_agent_loop(
    cp: SupervisorCheckpoint,
    exc: StageError,
    *,
    shot_no: int,
    run_id: str | None = None,
) -> SupervisorCheckpoint | None:
    """Promote a child loop's terminal budget signal to the supervisor."""
    if exc.exit_reason != "authority_blockers_exhausted":
        return None
    iterations = max(1, int(exc.iterations or 0))
    issue_messages = [
        str(issue.message).strip()
        for issue in exc.issues
        if str(issue.message).strip()
    ] or [
        str(message).strip()
        for message in exc.errors
        if str(message).strip()
    ]
    issue_codes = list(dict.fromkeys(
        str(issue.code).strip()
        for issue in exc.issues
        if str(issue.code).strip()
    ))
    cp.phase = "WAITING_HUMAN"
    cp.outcome = "REPAIR_FAILED_AGENT_LOOP_EXHAUSTED"
    cp.last_repair = {
        "level": "L5",
        "strategy": "waiting_human",
        "status": "paused",
        "reason": "child_agent_loop_authority_blockers_exhausted",
        "invalidation_frontier": shot_no,
        "touched_shot_nos": [shot_no],
        "issue_codes": issue_codes,
        "issue_messages": issue_messages,
        "iterations": iterations,
        "exit_reason": exc.exit_reason,
    }
    cp.repair_candidate_shots = []
    save_checkpoint(cp, run_id=run_id)
    if run_id:
        evidence_repository.append_event(
            run_id,
            "STORYBOARD_AGENT_LOOP_EXHAUSTED",
            "warning",
            f"第 {shot_no} 镜自动修复达到 {iterations} 轮上限，任务已停止",
            payload={
                "shot_no": shot_no,
                "iterations": iterations,
                "exit_reason": exc.exit_reason,
                "issue_codes": issue_codes,
            },
        )
    return cp


def _is_structural_storyboard_issue(category: Any = None) -> bool:
    """Use the serialized Issue category; legacy prose has no authority."""
    return str(category or "") == "structural"


def _storyboard_warning_requires_auto_repair(issue: Any) -> bool:
    """QA findings never authorize another model generation."""
    return False


def _storyboard_generation_is_complete(
    completed: list[Shot], planned_total: int, max_shots: int,
) -> bool:
    """Return whether generation may safely leave the per-shot loop.

    A persisted ``is_final`` flag is only authoritative after every planned
    outline beat has been generated.  Legacy interrupted repairs can leave an
    early shot marked final while the durable outline still contains later
    beats; treating that flag as terminal publishes an incomplete storyboard.
    """
    count = len(completed)
    if planned_total > 0 and count >= planned_total:
        return True
    if completed and completed[-1].is_final and planned_total <= 0:
        return True
    _ = max_shots
    return False


def _repair_candidate_made_progress(
    *,
    mode: str,
    candidate_passed: bool,
    before_messages: list[str],
    after_messages: list[str],
    window_start: int | None = None,
    window_end: int | None = None,
) -> bool:
    """Accept only monotonic partial repair so independent issues converge."""
    if candidate_passed or mode == "append":
        return True
    before = _repair_message_atoms(before_messages)
    after = _repair_message_atoms(after_messages)
    if not before:
        return False
    resolved = before - after
    introduced = after - before
    if not resolved:
        return False
    if not introduced:
        return True
    return False


def _repair_message_atoms(messages: list[str]) -> set[tuple[str, str]]:
    """Split combined gate messages into stable repair targets."""
    from app.evaluations.issues import issue_code

    atoms: set[tuple[str, str]] = set()
    for value in messages:
        message = str(value or "").strip()
        if not message:
            continue
        code = issue_code(message)
        target_ids = {
            match.upper()
            for match in re.findall(r"(?<![A-Z0-9])(?:KL|S|I)\d+(?![A-Z0-9])", message, re.I)
        }
        if target_ids:
            atoms.update((code, target_id) for target_id in target_ids)
            continue
        shot_nos = {
            match.group(1)
            for match in re.finditer(
                r"(?:shot_no\s*=\s*|第\s*|镜头\s*)(\d+)\s*镜?",
                message,
                re.I,
            )
        }
        if shot_nos:
            atoms.update((code, f"shot:{shot_no}") for shot_no in shot_nos)
            continue
        atoms.add((code, message))
    return atoms


def _deterministic_dialogue_framing_candidate(
    shot: Shot,
) -> Shot | None:
    """Return the smallest contract-preserving framing fix when it is unambiguous."""
    from app.continuity import dialogue_action_staging_kind

    candidate = Shot.model_validate(shot.model_dump(mode="json"))
    changed = False
    staging_kind = dialogue_action_staging_kind(candidate)
    if staging_kind == "spatial" and candidate.shot_size not in {"远景", "全景", "中景"}:
        candidate.shot_size = "中景"
        changed = True
    elif staging_kind == "prop" and candidate.shot_size == "特写":
        candidate.shot_size = "近景"
        changed = True
    if changed:
        tags = list(candidate.risk_tags or [])
        if "dialogue_action_staging" not in tags:
            tags.append("dialogue_action_staging")
        candidate.risk_tags = tags

    return candidate if changed else None


def _deterministic_ambient_audio_cast_candidate(
    shot: Shot,
) -> Shot | None:
    """Remove identity claims from a timeline that contains only ambient sound."""
    from app.spoken_contract import effective_spoken_segments

    if (
        not shot.audio_cast
        or effective_spoken_segments(shot)
        or not shot.audio_timeline
        or any(item.type != "ambient_sound" for item in shot.audio_timeline)
    ):
        return None
    candidate = Shot.model_validate(shot.model_dump(mode="json"))
    candidate.audio_cast = []
    return candidate


def _recover_truncated_outline_from_approved_artifact(
    conn,
    ep,
    cp: SupervisorCheckpoint,
    outline: StoryboardOutline | None,
) -> StoryboardOutline | None:
    """Recover a legacy-truncated mutable outline from its approved artifact.

    This is deliberately narrow: it only runs for a checkpoint that claims a
    completed publication while the official shot projection is still shorter
    than that checkpoint.  The recovered artifact must belong to the exact
    current screenplay version and contain more beats than the mutable JSON.
    User-approved structure edits therefore remain untouched.
    """
    current_count = len(outline.shots) if (outline and outline.shots) else 0
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (ep["id"],),
    ).fetchone()["c"])
    if not (
        cp.phase == "SUCCEEDED"
        and int(cp.expected_total or 0) > shot_count
        and int(cp.expected_total or 0) > current_count
        and ep["storyboard_artifact_id"]
        and ep["screenplay_artifact_id"]
    ):
        return outline

    rows = conn.execute(
        """SELECT content_json,parent_artifact_ids_json FROM artifacts
           WHERE type='storyboard_outline' AND scope_type='episode' AND scope_id=?
             AND status IN ('approved','validated')
           ORDER BY created_at DESC""",
        (ep["id"],),
    ).fetchall()
    for row in rows:
        try:
            parents = json.loads(row["parent_artifact_ids_json"] or "[]")
            candidate = StoryboardOutline.model_validate_json(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if ep["screenplay_artifact_id"] not in parents:
            continue
        if len(candidate.shots) <= max(current_count, shot_count):
            continue
        conn.execute(
            "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
            (candidate.model_dump_json(), ep["id"]),
        )
        conn.commit()
        cp.expected_total = len(candidate.shots)
        cp.phase = "VALIDATING_OUTLINE"
        cp.outcome = None
        return candidate
    return outline


def _recover_outline_from_current_artifact(
    conn,
    ep,
    cp: SupervisorCheckpoint,
) -> StoryboardOutline | None:
    """Reuse a parseable outline candidate when only score-only findings remain."""
    candidate_ids: list[str] = []
    if cp.outline_artifact_id:
        candidate_ids.append(str(cp.outline_artifact_id))
    rows = conn.execute(
        """SELECT id FROM artifacts
           WHERE type='storyboard_outline' AND scope_type='episode' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC""",
        (ep["id"],),
    ).fetchall()
    candidate_ids.extend(str(row["id"]) for row in rows)
    expected_parents = {
        str(value)
        for value in (
            cp.input_versions.get("screenplay_artifact_id"),
            cp.input_versions.get("bible_artifact_id"),
        )
        if value
    }
    from app.loops.base import is_structural_issue

    for artifact_id in dict.fromkeys(candidate_ids):
        artifact = evidence_repository.get_artifact(artifact_id)
        if (
            artifact is None
            or artifact.get("type") != "storyboard_outline"
            or artifact.get("scope_type") != "episode"
            or artifact.get("scope_id") != ep["id"]
            or artifact.get("status")
            in {"stale", "rejected", "superseded", "needs_revision"}
            or not expected_parents.issubset(
                set(artifact.get("parent_artifact_ids") or [])
            )
        ):
            continue
        try:
            outline = StoryboardOutline.model_validate(artifact.get("content") or {})
        except (TypeError, ValueError):
            continue
        if not outline.shots:
            continue
        evaluation_rows = conn.execute(
            """SELECT status,hard_gate_passed,evaluation_role,runtime_blocking,
                      issues_json
                 FROM evaluations WHERE artifact_id=?""",
            (artifact_id,),
        ).fetchall()
        if not evaluation_rows or any(
            not bool(row["hard_gate_passed"])
            or bool(row["runtime_blocking"])
            or row["status"] == "failed"
            for row in evaluation_rows
        ):
            continue
        requires_format_repair = False
        for row in evaluation_rows:
            try:
                raw_issues = json.loads(row["issues_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                requires_format_repair = True
                break
            for raw_issue in raw_issues:
                try:
                    issue = Issue.model_validate(raw_issue)
                except (TypeError, ValueError):
                    requires_format_repair = True
                    break
                if is_structural_issue(issue) or bool(
                    (issue.evidence or {}).get("requires_regeneration", False)
                ):
                    requires_format_repair = True
                    break
            if requires_format_repair:
                break
        if requires_format_repair:
            continue
        cp.outline_artifact_id = artifact_id
        cp.expected_total = len(outline.shots)
        cp.phase = "VALIDATING_OUTLINE"
        cp.outcome = None
        conn.execute(
            "UPDATE episodes SET storyboard_outline_json=?, storyboard_warning=NULL WHERE id=?",
            (outline.model_dump_json(), ep["id"]),
        )
        conn.commit()
        return outline
    return None


def _contiguous_shot_rows(rows) -> list:
    """Return only the validated 1..N prefix, stopping at the first shot_no gap."""
    prefix = []
    expected = 1
    for row in rows:
        shot_no = int(row["shot_no"])
        if shot_no != expected:
            break
        prefix.append(row)
        expected += 1
    return prefix


def _storyboard_hash(board: Storyboard) -> str:
    return evidence_repository.content_hash(board.model_dump(mode="json"))


def _restore_misplaced_shot_fields_from_provider(
    conn,
    *,
    episode_id: str,
    rows,
    board: Storyboard,
    outline: StoryboardOutline | None,
    run_id: str | None,
) -> bool:
    """Recover fields returned by a successful call but misplaced at JSON root."""
    outline_by_no = {
        int(brief.shot_no): brief
        for brief in (outline.shots if outline is not None else [])
    }
    changed_shots: list[dict[str, Any]] = []
    for index, (row, shot) in enumerate(zip(rows, board.shots)):
        missing = [
            field for field in (
                "first_frame_desc", "last_frame_desc", "source_excerpt",
            )
            if not str(getattr(shot, field, "") or "").strip()
        ]
        if not missing:
            continue

        artifact_ids = [str(row["storyboard_artifact_id"] or "")]
        visited: set[str] = set()
        recovered = False
        while artifact_ids and not recovered:
            artifact_id = artifact_ids.pop(0)
            if not artifact_id or artifact_id in visited:
                continue
            visited.add(artifact_id)
            artifact_row = conn.execute(
                """SELECT created_by_step_run_id,parent_artifact_ids_json
                     FROM artifacts WHERE id=?""",
                (artifact_id,),
            ).fetchone()
            if artifact_row is None:
                continue
            try:
                artifact_ids.extend(
                    str(item)
                    for item in json.loads(
                        artifact_row["parent_artifact_ids_json"] or "[]"
                    )
                    if item
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            step_run_id = str(artifact_row["created_by_step_run_id"] or "")
            if not step_run_id:
                continue
            call_rows = conn.execute(
                """SELECT id,response_json FROM provider_calls
                   WHERE step_run_id=? AND kind='chat'
                     AND status IN ('OK','SUCCESS','SUCCEEDED')
                     AND response_json IS NOT NULL
                   ORDER BY id DESC""",
                (step_run_id,),
            ).fetchall()
            for call_row in call_rows:
                try:
                    response = json.loads(call_row["response_json"] or "{}")
                    raw = str(
                        response["choices"][0]["message"]["content"]
                    )
                    candidate = extract_json(raw)
                except (
                    KeyError, IndexError, TypeError, ValueError,
                    json.JSONDecodeError,
                ):
                    continue
                brief = outline_by_no.get(int(shot.shot_no))
                normalized, normalizations = normalize_storyboard_shot_candidate(
                    candidate,
                    episode_no=board.episode_no,
                    shot_no=int(shot.shot_no),
                    outline_story_event_id=(
                        str(brief.story_event_id or "") if brief else ""
                    ),
                    outline_narrative_task=(
                        brief.model_dump(mode="json") if brief else None
                    ),
                    previous_scene_name=(
                        str(board.shots[index - 1].scene_name or "")
                        if index > 0 else ""
                    ),
                    previous_scene_time=(
                        str(board.shots[index - 1].scene_time or "")
                        if index > 0 else ""
                    ),
                )
                moved_fields = {
                    str(change.get("field") or "").removeprefix("shot.")
                    for change in normalizations
                    if change.get("reason") == "misplaced_root_field"
                }
                if not moved_fields.intersection(missing):
                    continue
                try:
                    draft = StoryboardShotDraft.model_validate(normalized)
                    from app.storyboard_workspace import (
                        align_generated_source_evidence,
                    )

                    draft.shot.source_excerpt, _binding = (
                        align_generated_source_evidence(
                            episode_id,
                            draft.shot.source_excerpt,
                        )
                    )
                except (TypeError, ValueError, HTTPException):
                    continue
                for field in moved_fields:
                    if field in Shot.model_fields:
                        setattr(shot, field, getattr(draft.shot, field))
                changed_shots.append({
                    "shot_no": int(shot.shot_no),
                    "fields": sorted(moved_fields),
                    "provider_call_id": int(call_row["id"]),
                })
                recovered = True

    if not changed_shots:
        return False
    for row, shot in zip(rows, board.shots):
        if any(
            item["shot_no"] == int(shot.shot_no)
            for item in changed_shots
        ):
            _write_shot_fields(
                conn,
                str(row["id"]),
                shot,
                row["storyboard_artifact_id"],
                narrative_authority=True,
            )
    conn.commit()
    from app.domain.storyboard_ops import _ensure_current_storyboard_shot_artifacts

    _ensure_current_storyboard_shot_artifacts(conn, episode_id, board)
    conn.commit()
    if run_id:
        evidence_repository.append_event(
            run_id,
            "STORYBOARD_PROVIDER_PROJECTION_RECOVERED",
            "warning",
            "已从成功供应商响应恢复错层的分镜必填字段",
            payload={"shots": changed_shots},
        )
    return True


def _repair_is_pending(cp: SupervisorCheckpoint) -> bool:
    repair = cp.last_repair or {}
    return repair.get("status") in {"candidate_pending", "candidate_generating"}


def _annotate_blind_review_repair(
    cp: SupervisorCheckpoint,
    errors: list[str],
) -> None:
    """Record review context without overwriting the repair lifecycle state."""
    repair = dict(cp.last_repair or {})
    paused = cp.phase in {
        "WAITING_HUMAN",
        "WAITING_AUTHORIZATION",
        "PAUSED_EXTERNAL",
    }
    repair["status"] = str(repair.get("status") or (
        "paused" if paused else "candidate_pending"
    ))
    repair["review_status"] = (
        "blind_review_failed_paused"
        if paused
        else "blind_review_repair_planned"
    )
    repair["blind_review_errors"] = list(errors)
    cp.last_repair = repair


def _repair_feedback_for_shot(messages: list[str], shot_no: int) -> list[str]:
    localized: list[str] = []
    for message in messages:
        targets = {
            int(value)
            for value in re.findall(r"(?:shot_no\s*=\s*|第\s*)(\d+)\s*镜?", message, re.I)
        }
        if not targets or shot_no in targets:
            localized.append(message)
    return localized


def _repair_outline_for_checkpoint(
    cp: SupervisorCheckpoint,
    fallback: StoryboardOutline | None,
) -> StoryboardOutline | None:
    raw = (cp.last_repair or {}).get("candidate_outline")
    if raw:
        try:
            return StoryboardOutline.model_validate(raw)
        except (TypeError, ValueError):
            pass
    return fallback


def _missing_spine_targets(plan: RepairPlan) -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for message in plan.issue_messages or []:
        for beat_id, who, does in re.findall(
            r"\b(S\d+)\s*/\s*([^:：；]+)\s*[:：]\s*([^；]+)",
            message,
            re.I,
        ):
            normalized = beat_id.upper()
            if normalized in seen:
                continue
            seen.add(normalized)
            targets.append((normalized, who.strip(), does.strip()))
    return targets


def _retarget_spine_repair_brief(
    brief: StoryboardOutlineShot,
    target: tuple[str, str, str] | None,
) -> None:
    if target is None:
        return
    beat_id, who, does = target
    brief.beat = f"{who}{does}"
    brief.covers = does
    brief.spine_beat_ids = [beat_id]
    brief.primary_action = does
    audio_cast = [str(name).strip() for name in (brief.audio_cast or []) if str(name).strip()]
    if brief.key_line_ids and who and who not in audio_cast:
        audio_cast.insert(0, who)
    brief.audio_cast = audio_cast


def _retarget_spine_repair_shot(
    shot: Shot,
    brief: StoryboardOutlineShot | None,
    target: tuple[str, str, str] | None,
) -> None:
    """Reapply the authorized spine contract after model paraphrasing."""
    if target is None:
        return
    beat_id, who, does = target
    shot.primary_action = does
    shot.spine_beat_ids = [beat_id]
    if brief is not None:
        for field_name in (
            "key_line_ids",
            "information_ids",
            "new_information_ids",
        ):
            required = list(getattr(brief, field_name, None) or [])
            if required:
                setattr(shot, field_name, required)
    audio_cast = [
        str(name).strip()
        for name in (shot.audio_cast or [])
        if str(name).strip()
    ]
    if shot.key_line_ids and who and who not in audio_cast:
        audio_cast.insert(0, who)
    shot.audio_cast = audio_cast


def _apply_semantic_outline_operations(
    outline: StoryboardOutline,
    raw_operations: list[dict[str, Any]],
) -> tuple[StoryboardOutline, list[dict[str, Any]]]:
    from app.narrative_repair import (
        SemanticOutlineOperation,
        apply_semantic_outline_operations,
    )

    operations = [
        SemanticOutlineOperation.model_validate(raw)
        for raw in raw_operations
    ]
    return apply_semantic_outline_operations(outline, operations)


def _outline_changed_window(
    before: StoryboardOutline,
    after: StoryboardOutline,
) -> tuple[int, int, int] | None:
    """Return 1-based (start, old_end, new_end) for the minimal changed span."""
    def _identity(shot: StoryboardOutlineShot) -> dict[str, Any]:
        payload = shot.model_dump(mode="json")
        payload.pop("shot_no", None)
        return payload

    old = [_identity(shot) for shot in before.shots]
    new = [_identity(shot) for shot in after.shots]
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    if prefix == len(old) == len(new):
        return None
    suffix = 0
    while (
        suffix < len(old) - prefix
        and suffix < len(new) - prefix
        and old[len(old) - suffix - 1] == new[len(new) - suffix - 1]
    ):
        suffix += 1
    return prefix + 1, len(old) - suffix, len(new) - suffix


def _repair_context_shots(conn, cp: SupervisorCheckpoint, episode_no: int) -> list[Shot]:
    """Return prefix + durable candidates while leaving the official rows untouched."""
    repair = cp.last_repair or {}
    start = max(1, int(repair.get("window_start") or cp.next_shot_no or 1))
    prefix_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? AND shot_no<? ORDER BY shot_no",
        (cp.episode_id, start),
    ).fetchall()
    prefix = list(_board_from_rows(prefix_rows, episode_no).shots) if prefix_rows else []
    candidates: list[Shot] = []
    for raw in cp.repair_candidate_shots:
        try:
            candidates.append(_shot_from_checkpoint(raw))
        except (TypeError, ValueError):
            continue
    return [*prefix, *candidates]


def _shot_checkpoint_payload(
    shot: Shot,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist runtime evidence lineage alongside the schema-bound shot."""
    payload = shot.model_dump(mode="json")
    artifact_id = (
        getattr(shot, "evidence_artifact_id", None)
        or (fallback or {}).get("_evidence_artifact_id")
    )
    if artifact_id:
        payload["_evidence_artifact_id"] = str(artifact_id)
    return payload


def _shot_from_checkpoint(raw: dict[str, Any]) -> Shot:
    shot = Shot.model_validate(raw)
    artifact_id = raw.get("_evidence_artifact_id")
    if artifact_id:
        object.__setattr__(shot, "evidence_artifact_id", str(artifact_id))
    return shot


def _board_from_rows(rows, episode_no: int) -> Storyboard:
    # Local import avoids the domain module's shared-namespace import cycle.
    from app.domain.storyboard_ops import _board_from_shot_rows

    return _board_from_shot_rows(rows, episode_no)


def _merge_repair_candidate(
    current: Storyboard,
    cp: SupervisorCheckpoint,
) -> Storyboard:
    repair = cp.last_repair or {}
    start = max(1, int(repair.get("window_start") or 1))
    mode = str(repair.get("mode") or "replace")
    current_shots = [
        Shot.model_validate(shot.model_dump(mode="json"))
        for shot in current.shots
    ]
    candidates = [_shot_from_checkpoint(raw) for raw in cp.repair_candidate_shots]
    if mode == "insert":
        merged = [*current_shots[: start - 1], *candidates, *current_shots[start - 1 :]]
        for shot_no, shot in enumerate(merged, start=1):
            shot.shot_no = shot_no
        return Storyboard(episode_no=current.episode_no, shots=merged)
    if mode == "structure":
        old_end = int(repair.get("structure_old_end") or (start - 1))
        merged = [
            *current_shots[: start - 1],
            *candidates,
            *current_shots[max(start - 1, old_end) :],
        ]
        for shot_no, shot in enumerate(merged, start=1):
            shot.shot_no = shot_no
        return Storyboard(episode_no=current.episode_no, shots=merged)
    current_by_no = {
        int(shot.shot_no): shot for shot in current_shots
    }
    for candidate in candidates:
        official = current_by_no.get(int(candidate.shot_no))
        if official is not None:
            candidate.shot_uid = official.shot_uid
    by_no = {int(shot.shot_no): shot for shot in candidates}
    merged = [by_no.get(int(shot.shot_no), shot) for shot in current_shots]
    # A failed initial/append generation has no official target row yet.
    existing_nos = {int(shot.shot_no) for shot in current_shots}
    merged.extend(shot for shot in candidates if int(shot.shot_no) not in existing_nos)
    merged.sort(key=lambda shot: int(shot.shot_no))
    return Storyboard(episode_no=current.episode_no, shots=merged)


def _validated_candidate_projection(
    current: Storyboard,
    evaluated: Storyboard,
    cp: SupervisorCheckpoint,
) -> Storyboard:
    """Overlay only the evaluated repair window onto the exact CAS baseline.

    Full-gate evaluation may derive render-time fields on every shot.  Those
    derived values are useful to validate the candidate but are outside a
    local repair's authorized write window.  Persisting or hashing them would
    either mutate unrelated shots or produce a projection mismatch.
    """
    repair = cp.last_repair or {}
    start = max(1, int(repair.get("window_start") or 1))
    raw_count = len(cp.repair_candidate_shots)
    mode = str(repair.get("mode") or "replace")
    if mode in {"insert", "structure"}:
        selected = [
            shot for shot in evaluated.shots
            if start <= int(shot.shot_no) < start + raw_count
        ]
    else:
        end = max(start, int(repair.get("window_end") or start))
        selected = [
            shot for shot in evaluated.shots
            if start <= int(shot.shot_no) <= end
        ]
    projected_cp = cp.model_copy(deep=True)
    projected_cp.repair_candidate_shots = [
        _shot_checkpoint_payload(
            shot,
            fallback=(
                cp.repair_candidate_shots[index]
                if index < len(cp.repair_candidate_shots)
                else None
            ),
        )
        for index, shot in enumerate(selected)
    ]
    return _merge_repair_candidate(current, projected_cp)


def _write_shot_fields(
    conn,
    row_id: str,
    shot: Shot,
    artifact_id: str | None,
    *,
    narrative_authority: bool = False,
) -> None:
    from app.continuity import shot_contract_dict
    from app.validators import normalize_action_desc

    if not narrative_authority:
        shot.action_desc = normalize_action_desc(shot.action_desc)
    conn.execute(
        """UPDATE shots SET duration_s=?,shot_size=?,camera_move=?,scene_time=?,scene_setting=?,scene_name=?,
                  characters=?,action_desc=?,first_frame_desc=?,last_frame_desc=?,source_excerpt=?,
                  narration=?,dialogues=?,transition=?,continuity_from_prev=?,shot_contract_json=?,
                  continuity_mode=?,observed_state_out=?,storyboard_artifact_id=? WHERE id=?""",
        (
            shot.duration_s, shot.shot_size, shot.camera_move, shot.scene_time, shot.scene_setting,
            shot.scene_name or None, json.dumps(shot.characters, ensure_ascii=False),
            shot.action_desc, shot.first_frame_desc, shot.last_frame_desc,
            shot.source_excerpt, shot.narration,
            json.dumps([item.model_dump() for item in shot.dialogues], ensure_ascii=False),
            shot.transition, int(shot.continuity_from_prev),
            json.dumps(shot_contract_dict(shot), ensure_ascii=False),
            shot.continuity_mode, shot.observed_state_out, artifact_id, row_id,
        ),
    )


def _ensure_storyboard_revision(
    episode_id: str,
    board: Storyboard,
    *,
    contract_version: str,
):
    from app.production.revision import (
        ensure_production_revision,
        mark_baseline_generated,
    )

    revision = ensure_production_revision(
        episode_id=episode_id,
        kind="storyboard",
        input_fingerprint=_storyboard_hash(board),
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
        resume=True,
    )
    if revision.working_artifact_id:
        return revision
    baseline = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard_document",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T2",
        content=board.model_dump(mode="json"),
        contract_version=contract_version,
    ))
    return mark_baseline_generated(
        revision.id,
        baseline_artifact_id=baseline["id"],
        working_artifact_id=baseline["id"],
    )


def _commit_repair_candidate(
    conn,
    cp: SupervisorCheckpoint,
    *,
    episode_id: str,
    screenplay,
    current_board: Storyboard,
    candidate_board: Storyboard,
    expected_screenplay_artifact_id: str | None,
    run_id: str | None,
) -> str:
    """CAS-commit a validated candidate and its official projection in one transaction."""
    from app.domain.storyboard_ops import (
        _assert_storyboard_write_authorized,
        _insert_storyboard_shot,
    )
    from app.production.revision import get_production_revision

    repair = cp.last_repair or {}
    candidate_outline = _repair_outline_for_checkpoint(cp, None)
    expected_board_hash = str(repair.get("base_hash") or "")
    current_hash = _storyboard_hash(current_board)
    if expected_board_hash and expected_board_hash != current_hash:
        raise RuntimeError("storyboard working projection CAS conflict")

    mode = str(repair.get("mode") or "replace")
    start = max(1, int(repair.get("window_start") or 1))
    end = max(start, int(repair.get("window_end") or start))
    raw_candidate_count = len(cp.repair_candidate_shots)
    candidate_end = (
        start + raw_candidate_count - 1
        if mode in {"insert", "structure"}
        else end
    )
    official_by_no = {int(shot.shot_no): shot for shot in current_board.shots}
    from app.storyboard_workspace import align_generated_source_evidence

    for shot in candidate_board.shots:
        shot_no = int(shot.shot_no)
        if start <= shot_no <= candidate_end:
            evidence_candidates: list[str] = []
            if mode not in {"insert", "structure"}:
                official = official_by_no.get(shot_no)
                if official is not None and official.source_excerpt.strip():
                    evidence_candidates.append(official.source_excerpt)
            evidence_candidates.extend([
                shot.source_excerpt,
                *[dialogue.line for dialogue in shot.dialogues],
                shot.narration,
                *[item.text for item in shot.audio_timeline],
            ])
            alignment_error: HTTPException | None = None
            for evidence in evidence_candidates:
                if not evidence.strip():
                    continue
                try:
                    shot.source_excerpt, _binding = align_generated_source_evidence(
                        episode_id, evidence,
                    )
                    break
                except HTTPException as exc:
                    alignment_error = exc
            else:
                raise alignment_error or HTTPException(
                    422, "自动修复候选缺少可证明的原文证据",
                )

    contract_version = get_contract("storyboard").version
    revision = _ensure_storyboard_revision(
        episode_id, current_board, contract_version=contract_version,
    )
    revision = get_production_revision(revision.id)
    assert revision is not None and revision.working_artifact_id
    parent_ids = [revision.working_artifact_id]
    candidate_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard_document",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T2",
        content=candidate_board.model_dump(mode="json"),
        parent_artifact_ids=parent_ids,
        contract_version=contract_version,
    ))

    # Artifact creation commits independently. Re-observe both chain head and
    # projection inside BEGIN IMMEDIATE before touching official rows.
    conn.execute("BEGIN IMMEDIATE")
    try:
        _assert_storyboard_write_authorized(
            conn, episode_id, expected_screenplay_artifact_id,
        )
        rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
        ).fetchall()
        observed = _board_from_rows(rows, current_board.episode_no)
        if expected_board_hash and _storyboard_hash(observed) != expected_board_hash:
            raise RuntimeError("storyboard projection changed during repair")
        chain = conn.execute(
            "SELECT working_artifact_id FROM production_revisions WHERE id=?",
            (revision.id,),
        ).fetchone()
        if not chain or chain["working_artifact_id"] != revision.working_artifact_id:
            raise RuntimeError("storyboard working artifact changed during repair")

        if mode in {"insert", "structure"}:
            candidate_shots = [
                shot for shot in candidate_board.shots
                if start <= int(shot.shot_no) < start + raw_candidate_count
            ]
        else:
            candidate_shots = [
                shot for shot in candidate_board.shots
                if start <= int(shot.shot_no) <= end
            ]
        if len(candidate_shots) != raw_candidate_count:
            raise RuntimeError("validated storyboard candidate window mismatch")
        if mode == "insert":
            _open_shot_gap(conn, episode_id, start)
            for shot in candidate_shots:
                shot_id = _insert_storyboard_shot(
                    conn, episode_id, screenplay, shot, expected_screenplay_artifact_id,
                )
                from app.storyboard_workspace import realign_generated_source_binding
                realign_generated_source_binding(
                    episode_id, shot_id, shot.source_excerpt, conn=conn, commit=False,
                )
        elif mode == "structure":
            old_end = int(repair.get("structure_old_end") or (start - 1))
            old_count = max(0, old_end - start + 1)
            if old_count:
                _delete_shot_window(conn, episode_id, start, old_end)
            delta = len(candidate_shots) - old_count
            suffix_rows = conn.execute(
                "SELECT id,shot_no FROM shots WHERE episode_id=? AND shot_no>? "
                + ("ORDER BY shot_no DESC" if delta > 0 else "ORDER BY shot_no ASC"),
                (episode_id, old_end),
            ).fetchall()
            if delta:
                for suffix_row in suffix_rows:
                    conn.execute(
                        "UPDATE shots SET shot_no=? WHERE id=?",
                        (int(suffix_row["shot_no"]) + delta, suffix_row["id"]),
                    )
            for shot in candidate_shots:
                shot_id = _insert_storyboard_shot(
                    conn, episode_id, screenplay, shot,
                    expected_screenplay_artifact_id,
                )
                from app.storyboard_workspace import realign_generated_source_binding

                realign_generated_source_binding(
                    episode_id, shot_id, shot.source_excerpt, conn=conn, commit=False,
                )
        else:
            from app import worker
            from app.storyboard_workspace import realign_generated_source_binding

            for shot in candidate_shots:
                row = conn.execute(
                    "SELECT id FROM shots WHERE episode_id=? AND shot_no=?",
                    (episode_id, shot.shot_no),
                ).fetchone()
                artifact_id = getattr(shot, "evidence_artifact_id", None)
                if row:
                    worker.clear_shot_artifacts(
                        row["id"], active_storyboard_run_id=run_id, commit=False,
                    )
                    _write_shot_fields(
                        conn,
                        row["id"],
                        shot,
                        artifact_id,
                        narrative_authority=screenplay.narrative_plan is not None,
                    )
                    realign_generated_source_binding(
                        episode_id, row["id"], shot.source_excerpt,
                        conn=conn, commit=False,
                    )
                else:
                    shot_id = _insert_storyboard_shot(
                        conn, episode_id, screenplay, shot,
                        expected_screenplay_artifact_id,
                    )
                    realign_generated_source_binding(
                        episode_id, shot_id, shot.source_excerpt,
                        conn=conn, commit=False,
                    )
        if candidate_outline is not None:
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                (candidate_outline.model_dump_json(), episode_id),
            )
        projected_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
        ).fetchall()
        projected_board = _board_from_rows(projected_rows, candidate_board.episode_no)
        if _storyboard_hash(projected_board) != _storyboard_hash(candidate_board):
            raise RuntimeError("storyboard candidate projection mismatch")
        conn.execute(
            "UPDATE production_revisions SET working_artifact_id=?,updated_at=? WHERE id=?",
            (candidate_artifact["id"], time.time(), revision.id),
        )
        conn.execute(
            "UPDATE episodes SET working_storyboard_artifact_id=? WHERE id=?",
            (candidate_artifact["id"], episode_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    patch_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard_patch",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T2",
        content={
            "semantic_attempt_id": repair.get("semantic_attempt_id"),
            "issue_fingerprint": repair.get("fingerprint"),
            "issue_messages": repair.get("issue_messages") or [],
            "mode": repair.get("mode"),
            "window": [repair.get("window_start"), repair.get("window_end")],
            "before_hash": current_hash,
            "after_hash": _storyboard_hash(candidate_board),
            "before_artifact_id": revision.working_artifact_id,
            "after_artifact_id": candidate_artifact["id"],
        },
        parent_artifact_ids=[revision.working_artifact_id, candidate_artifact["id"]],
        contract_version=contract_version,
    ))
    if run_id:
        evidence_repository.append_event(
            run_id, "STORYBOARD_PATCH_COMMITTED", "info",
            f"已原子提交第 {repair.get('window_start')}~{repair.get('window_end')} 镜修复",
            payload={
                "patch_artifact_id": patch_artifact["id"],
                "candidate_artifact_id": candidate_artifact["id"],
                "semantic_attempt_id": repair.get("semantic_attempt_id"),
            },
        )
    return str(candidate_artifact["id"])


def _apply_storyboard_planning_target(
    conn: Any,
    episode_id: str,
    episode_data: dict[str, Any],
    compact_target: int,
    *,
    narrative_authority: bool,
) -> None:
    """Use a derived storyboard duration without mutating screenplay authority."""
    if compact_target == episode_data.get("target_duration_s"):
        return
    episode_data["target_duration_s"] = compact_target
    if narrative_authority:
        return
    conn.execute(
        "UPDATE episodes SET target_duration_s=? WHERE id=?",
        (compact_target, episode_id),
    )
    conn.commit()


def _storyboard_bible_snapshot(project_row: Any, cp: SupervisorCheckpoint) -> Bible:
    """Resolve the immutable Bible artifact bound to this supervisor run."""
    from app.domain.common import _project_bible_or_placeholder

    current = _project_bible_or_placeholder(project_row)
    artifact_id = str(cp.input_versions.get("bible_artifact_id") or "").strip()
    if not artifact_id:
        return current
    artifact = evidence_repository.get_artifact(artifact_id)
    project_id = str(project_row["id"] or "") if project_row else ""
    if (
        artifact is None
        or artifact.get("type") != "character_bible"
        or artifact.get("scope_type") != "project"
        or str(artifact.get("scope_id") or "") != project_id
    ):
        return current
    try:
        return Bible.model_validate(artifact.get("content") or {})
    except (TypeError, ValueError):
        return current


async def run_storyboard_supervisor(
    episode_id: str,
    *,
    resume: bool = True,
    run_id: str | None = None,
    preflight_done: bool = False,
    new_activation: bool = False,
) -> SupervisorCheckpoint:
    """集级 Supervisor 主循环；任何入口都会在生成镜头前完成人物/场景资产预检。"""
    from app.domain.common import (
        _compact_episode_target,
        _episode_source_text,
        _storyboard_target_for_source,
    )
    from app.validators import (
        normalize_continuity,
        normalize_offbible_characters,
        normalize_transition_visuals,
        prefer_default_shot_durations,
        relieve_spoken_overflow,
        storyboard_shot_count_range,
        validate_storyboard_direction_contract,
        validate_storyboard_preserves_key_content,
        validate_storyboard,
    )
    from app.domain.storyboard_ops import (
        _board_from_shot_rows,
        _ensure_current_storyboard_shot_artifacts,
        _finalize_storyboard_evidence,
        _insert_storyboard_shot,
        _assert_storyboard_write_authorized,
        _persist_storyboard_character_policy_repairs,
        _reconcile_storyboard_scene_projection,
        _reconcile_storyboard_plan,
        _sync_storyboard_shot_timing,
    )
    from app.domain.video_ops import evaluate_storyboard_for_confirmation

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("分镜脚本", ["剧集不存在"])

    if not ep["screenplay_json"] or ep["screenplay_status"] != "ready":
        raise StageError("分镜脚本", ["请先生成并确认本集可拍剧本，再展开分镜"])
    from app.production.screenplay_authority import resolve_downstream_screenplay

    try:
        screenplay_context = resolve_downstream_screenplay(episode_id, conn=conn)
    except ValueError as exc:
        raise StageError("分镜脚本", [f"剧本权威链无效：{exc}"]) from exc
    screenplay = screenplay_context.screenplay
    narrative_authority = screenplay_context.narrative_authority_required
    if narrative_authority:
        from app.narrative import validate_storyboard_screenplay_authority

        upstream_runtime_errors = validate_storyboard_screenplay_authority(
            screenplay,
            expected_scope_id=episode_id,
        )
        if upstream_runtime_errors:
            raise StageError(
                "分镜上游权威",
                [
                    "已发布剧本含无法确定性投影的执行合同错误，"
                    "必须先修复剧本，不得调用分镜模型",
                    *upstream_runtime_errors,
                ],
            )
    resolved_screenplay_authority = None
    if narrative_authority:
        from app.production.screenplay_authority import (
            resolve_current_screenplay_authority,
        )

        resolved_screenplay_authority = resolve_current_screenplay_authority(
            episode_id,
            conn=conn,
            require_narrative=True,
        )
    published_storyboard_authority = False
    if (
        narrative_authority
        and ep["published_storyboard_artifact_id"]
        and ep["storyboard_completion_certificate_id"]
    ):
        try:
            from app.production.certificate import (
                verify_current_storyboard_completion_authority,
            )

            authority_rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                (episode_id,),
            ).fetchall()
            authority_board = _board_from_rows(
                authority_rows,
                int(ep["episode_no"] or 1),
            )
            verify_current_storyboard_completion_authority(
                episode=ep,
                current_storyboard_content=authority_board.model_dump(mode="json"),
            )
            published_storyboard_authority = True
        except Exception:
            # A stale certificate must be re-finalized from the exact current
            # projection; it is not a still-published authority that blocks
            # resume.  The full Supervisor gates run again before publication.
            published_storyboard_authority = False
    if published_storyboard_authority and resume:
        checkpoint_probe = load_latest_checkpoint(episode_id)
        if checkpoint_probe is None or not _repair_is_pending(checkpoint_probe):
            raise StageError(
                "分镜脚本",
                [
                    "已发布叙事分镜不能作为普通续跑工作区；"
                    "仅能恢复已隔离的语义修订候选"
                ],
            )

    async def _route_with_narrative_diagnosis(
        route_inputs,
        *,
        board: Storyboard,
        next_shot_no: int | None = None,
    ) -> RepairPlan:
        route_kwargs = {
            "validated_prefix_end": cp.validated_prefix_end,
            "next_shot_no": next_shot_no,
            "issue_fingerprint_counts": cp.issue_fingerprint_counts,
        }
        if screenplay.narrative_plan is None:
            return route_issues(route_inputs, **route_kwargs)
        from app.narrative_repair import route_narrative_issues
        return await route_narrative_issues(
            route_inputs,
            episode_id=episode_id,
            screenplay=screenplay,
            board=board,
            outline=outline,
            uncommitted_candidate=(
                next_shot_no is not None
                and next_shot_no == len(board.shots) + 1
            ),
            **route_kwargs,
        )

    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    cp = load_latest_checkpoint(episode_id) if resume else None
    if cp is None:
        cp = SupervisorCheckpoint(
            episode_id=episode_id,
            input_versions={
                "screenplay_artifact_id": ep["screenplay_artifact_id"],
                "bible_artifact_id": p["bible_artifact_id"] if p else None,
            },
            phase="PREFLIGHT",
            planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        )
    else:
        cp = _migrate_checkpoint(cp)
    bible = _storyboard_bible_snapshot(p, cp)
    has_real_bible = bool((p["bible_json"] or "").strip()) if p else False
    if not published_storyboard_authority:
        _reconcile_storyboard_scene_projection(conn, episode_id, bible)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    ep_data = dict(ep)
    ep_data["bible_artifact_id"] = cp.input_versions.get("bible_artifact_id")
    source_text = (
        resolved_screenplay_authority.source_text
        if resolved_screenplay_authority is not None
        else _episode_source_text(conn, ep)
    )

    if not preflight_done:
        # Direct/recovery callers may enter without the REST wrapper. Text
        # generation still proceeds from the published identity contract;
        # portrait/scene files are a video-stage dependency, not a text gate.
        if run_id:
            evidence_repository.append_event(
                run_id,
                "STORYBOARD_ASSET_WAIT_SKIPPED",
                "info",
                "未等待人物/场景图片落盘，分镜文本继续生成",
                payload={"episode_id": episode_id},
            )

    if new_activation:
        _withdraw_legacy_failed_publication(conn, episode_id, ep, cp)
        cp = _begin_repair_activation(cp)
    if run_id:
        evidence_repository.append_event(
            run_id, "STORYBOARD_SUPERVISOR_STARTED", "info",
            "分镜 Supervisor 已启动（断点续跑）" if resume else "分镜 Supervisor 已启动（全新运行）",
            payload={
                "episode_id": episode_id,
                "phase": cp.phase,
                "resume": resume,
            },
        )

    # 上游版本校验。发布新剧本会清空当前分镜投影，但历史 checkpoint 会作为审计
    # 证据保留；此时应按新剧本重新开始，而不是把旧 checkpoint 误判为可续跑任务。
    if (ep["screenplay_artifact_id"] or "") != (cp.input_versions.get("screenplay_artifact_id") or ""):
        if resume and not _current_storyboard_projection_has_material(conn, episode_id, ep):
            cp = SupervisorCheckpoint(
                episode_id=episode_id,
                input_versions={
                    "screenplay_artifact_id": ep["screenplay_artifact_id"],
                    "bible_artifact_id": p["bible_artifact_id"] if p else None,
                },
                phase="PREFLIGHT",
            )
            bible = _storyboard_bible_snapshot(p, cp)
            resume = False
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "STALE_STORYBOARD_CHECKPOINT_IGNORED",
                    "info",
                    "上游剧本已重新发布且当前分镜为空，已按新剧本重新开始",
                    payload={"episode_id": episode_id},
                )
        else:
            cp.phase = "WAITING_AUTHORIZATION"
            cp.outcome = "WAITING_AUTHORIZATION"
            save_checkpoint(cp, run_id=run_id)
            conn.execute(
                "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                ("上游剧本已变更，自动完成授权失效，请重新授权后继续", episode_id),
            )
            conn.commit()
            return cp

    if not resume:
        # 新 production revision 只初始化工作区；任何已有数据都只能续跑，不得全量清空。
        from app.production.revision import ensure_production_revision, get_active_production_revision
        from app.harness.contracts import get_contract

        existing_rev = get_active_production_revision(episode_id, "storyboard")
        if existing_rev and existing_rev.baseline_done and existing_rev.first_evaluation_done:
            resume = True
        else:
            try:
                contract_ver = get_contract("storyboard").version
            except Exception:  # noqa: BLE001
                contract_ver = "1"
            ensure_production_revision(
                episode_id=episode_id,
                kind="storyboard",
                contract_version=contract_ver,
                resume=False,
            )

    conn.execute(
        "UPDATE episodes SET status='scripting', script_error=NULL, storyboard_warning=NULL WHERE id=?",
        (episode_id,),
    )
    conn.commit()

    spine_n = len((screenplay.plot_spine.spine_beats if screenplay.plot_spine else None) or [])
    compact_target = _storyboard_target_for_source(
        ep_data.get("target_duration_s"), len(source_text), spine_beat_count=spine_n or None
    )
    _apply_storyboard_planning_target(
        conn,
        episode_id,
        ep_data,
        compact_target,
        narrative_authority=narrative_authority,
    )

    prev = conn.execute(
        "SELECT cliffhanger FROM episodes WHERE project_id=? AND episode_no=?",
        (ep["project_id"], ep["episode_no"] - 1),
    ).fetchone()

    outline: StoryboardOutline | None = None
    if resume and ep["storyboard_outline_json"]:
        try:
            outline = StoryboardOutline.model_validate_json(ep["storyboard_outline_json"])
        except (TypeError, ValueError):
            outline = None
    outline = _recover_truncated_outline_from_approved_artifact(
        conn, ep, cp, outline,
    )
    if outline is None:
        outline = _recover_outline_from_current_artifact(conn, ep, cp)
        if outline is not None:
            save_checkpoint(cp, run_id=run_id)
    if (
        outline is not None
        and narrative_authority
        and not published_storyboard_authority
    ):
        from app.narrative_outline import (
            normalize_narrative_storyboard_outline,
        )
        from app.validators import normalize_outline_dialogue_ownership

        authority_repairs = normalize_narrative_storyboard_outline(
            outline,
            screenplay,
            bible=bible,
            preserve_shot_ids=True,
        )
        dialogue_repairs = normalize_outline_dialogue_ownership(
            outline,
            screenplay,
        )
        authority_repairs.extend(
            normalize_narrative_storyboard_outline(
                outline,
                screenplay,
                bible=bible,
                preserve_shot_ids=True,
            )
        )
        pending_repair_dialogue_repairs: list[dict[str, Any]] = []
        pending_authority_repairs: list[dict[str, Any]] = []
        updated_pending_repair = False
        discarded_repair = None
        if _repair_is_pending(cp):
            pending_outline = _repair_outline_for_checkpoint(cp, outline)
            if pending_outline is not None and pending_outline is not outline:
                pending_authority_repairs = (
                    normalize_narrative_storyboard_outline(
                        pending_outline,
                        screenplay,
                        bible=bible,
                        preserve_shot_ids=True,
                    )
                )
                pending_repair_dialogue_repairs = (
                    normalize_outline_dialogue_ownership(
                        pending_outline,
                        screenplay,
                    )
                )
                if pending_repair_dialogue_repairs or pending_authority_repairs:
                    if "relation_migration_count" in (cp.last_repair or {}):
                        cp.last_repair = {
                            **(cp.last_repair or {}),
                            "candidate_outline": pending_outline.model_dump(
                                mode="json"
                            ),
                        }
                        updated_pending_repair = True
                        save_checkpoint(cp, run_id=run_id)
                    else:
                        discarded_repair = cp.last_repair
        if dialogue_repairs or authority_repairs:
            ensure_storyboard_scene_contexts(
                outline,
                screenplay,
                bible,
            )
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                (outline.model_dump_json(), episode_id),
            )
            conn.commit()
            ep_data["storyboard_outline_json"] = outline.model_dump_json()
        if discarded_repair is not None:
            cp.legacy_repair_audit = {
                **cp.legacy_repair_audit,
                "discarded_pre_migration_repair": discarded_repair,
            }
            cp.last_repair = None
            cp.repair_candidate_shots = []
            cp.phase = "GENERATING_SHOTS"
            cp.outcome = None
            save_checkpoint(cp, run_id=run_id)
        if (
            dialogue_repairs
            or updated_pending_repair
            or discarded_repair is not None
        ):
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "STORYBOARD_OUTLINE_DIALOGUE_OWNERSHIP_REPAIRED",
                    "info",
                    "已确定性修复历史大纲中的重复台词 owner 与对白残片",
                    payload={
                        "dialogue_repairs": dialogue_repairs,
                        "pending_repair_dialogue_repairs": (
                            pending_repair_dialogue_repairs
                        ),
                        "authority_repairs": authority_repairs,
                        "pending_authority_repairs": (
                            pending_authority_repairs
                        ),
                        "updated_pending_repair": updated_pending_repair,
                        "discarded_pending_repair": discarded_repair is not None,
                    },
                )

    def _normalize_completed_authority_board(
        board: Storyboard,
    ) -> list[dict[str, Any]]:
        repairs: list[dict[str, Any]] = []
        if not narrative_authority or outline is None:
            return repairs
        outline_by_no = {
            int(brief.shot_no): brief
            for brief in outline.shots
        }
        for index, shot in enumerate(board.shots):
            brief = outline_by_no.get(int(shot.shot_no))
            if brief is None:
                continue
            normalized, changes = normalize_storyboard_shot_candidate(
                {
                    "episode_no": ep_data["episode_no"],
                    "shot": shot.model_dump(mode="json"),
                },
                episode_no=ep_data["episode_no"],
                shot_no=int(shot.shot_no),
                **storyboard_shot_authority_context(
                    screenplay,
                    brief,
                    board.shots[index - 1] if index > 0 else None,
                    bible=bible,
                ),
            )
            normalized_shot = Shot.model_validate(normalized["shot"])
            if not changes:
                continue
            board.shots[index] = normalized_shot
            repairs.append({
                "shot_no": int(shot.shot_no),
                "fields": [change["field"] for change in changes],
            })
        return repairs

    # A code/policy update can make a previously pending repair obsolete. Before
    # resuming its provider call, re-evaluate a structurally complete official
    # board and keep it when the current gate already passes.
    if resume and _repair_is_pending(cp) and outline and outline.shots:
        current_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        current_prefix = _contiguous_shot_rows(current_rows)
        current_board = (
            _board_from_shot_rows(current_prefix, ep_data["episode_no"])
            if current_prefix else None
        )
        if (
            current_board is not None
            and len(current_board.shots) == len(outline.shots)
            and current_board.shots[-1].is_final
        ):
            _normalize_completed_authority_board(current_board)
            current_evaluation = evaluate_storyboard_for_confirmation(
                ep_data,
                current_board,
                screenplay,
                bible,
                has_real_bible=bool((p["bible_json"] or "").strip()) if p else False,
            )
            repair_warnings = [
                issue for issue in (current_evaluation.issues or [])
                if _storyboard_warning_requires_auto_repair(issue)
            ]
            if current_evaluation.passed and not repair_warnings:
                previous_repair = cp.last_repair or {}
                cp.last_repair = {
                    **previous_repair,
                    "status": "obsolete_current_gate_passed",
                    "remaining_issue_codes": [],
                }
                cp.repair_candidate_shots = []
                cp.phase = "VALIDATING_EPISODE"
                cp.outcome = None
                cp.expected_total = len(current_board.shots)
                cp.validated_prefix_end = len(current_board.shots)
                cp.next_shot_no = len(current_board.shots) + 1
                cp.validated_shot_artifact_ids = [
                    row["storyboard_artifact_id"]
                    for row in current_prefix
                    if row["storyboard_artifact_id"]
                ]
                save_checkpoint(cp, run_id=run_id)
                if run_id:
                    evidence_repository.append_event(
                        run_id,
                        "OBSOLETE_STORYBOARD_REPAIR_CLEARED",
                        "info",
                        "当前正式分镜已通过最新门禁，已丢弃过期修复候选",
                        payload={
                            "shot_count": len(current_board.shots),
                            "previous_fingerprint": previous_repair.get("fingerprint"),
                        },
                    )

    def _reload_completed(rows=None) -> list[Shot]:
        """Load only the contiguous prefix and keep checkpoint evidence aligned."""
        if rows is None:
            rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                (episode_id,),
            ).fetchall()
        prefix_rows = _contiguous_shot_rows(rows)
        shots = (
            list(_board_from_shot_rows(prefix_rows, ep_data["episode_no"]).shots)
            if prefix_rows
            else []
        )
        authority_board = Storyboard(
            episode_no=ep_data["episode_no"],
            shots=shots,
        )
        authority_repairs = _normalize_completed_authority_board(
            authority_board,
        )
        shots = list(authority_board.shots)
        if authority_repairs and not published_storyboard_authority:
            for repair in authority_repairs:
                index = int(repair["shot_no"]) - 1
                _write_shot_fields(
                    conn,
                    prefix_rows[index]["id"],
                    shots[index],
                    prefix_rows[index]["storyboard_artifact_id"],
                    narrative_authority=True,
                )
            conn.commit()
            prefix_rows = list(_ensure_current_storyboard_shot_artifacts(
                conn,
                episode_id,
                authority_board,
            ))
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "STORYBOARD_OUTLINE_AUTHORITY_REBOUND",
                    "info",
                    "已从批准大纲确定性恢复镜头叙事权威字段",
                    payload={
                        "episode_id": episode_id,
                        "repairs": authority_repairs,
                    },
                )
        if narrative_authority and shots:
            from app.identity_contracts import (
                canonicalize_storyboard_operational_identities,
            )

            identity_repairs = canonicalize_storyboard_operational_identities(
                Storyboard(
                    episode_no=ep_data["episode_no"],
                    shots=shots,
                ),
                bible,
                screenplay,
            )
            if identity_repairs and not published_storyboard_authority:
                outline_identity_repairs: list[dict[str, Any]] = []
                outline_by_no = {
                    int(brief.shot_no): brief
                    for brief in (outline.shots if outline is not None else [])
                }
                for shot in shots:
                    brief = outline_by_no.get(int(shot.shot_no))
                    if brief is None:
                        continue
                    changed_fields: list[str] = []
                    for field in ("characters_visible", "audio_cast"):
                        value = list(getattr(shot, field) or [])
                        if getattr(brief, field) == value:
                            continue
                        setattr(brief, field, value)
                        changed_fields.append(field)
                    if changed_fields:
                        outline_identity_repairs.append({
                            "shot_no": int(shot.shot_no),
                            "fields": changed_fields,
                        })
                for row, shot in zip(prefix_rows, shots):
                    _write_shot_fields(
                        conn,
                        str(row["id"]),
                        shot,
                        row["storyboard_artifact_id"],
                        narrative_authority=True,
                    )
                if outline_identity_repairs and outline is not None:
                    conn.execute(
                        "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                        (outline.model_dump_json(), episode_id),
                    )
                conn.commit()
                prefix_rows = list(_ensure_current_storyboard_shot_artifacts(
                    conn,
                    episode_id,
                    Storyboard(
                        episode_no=ep_data["episode_no"],
                        shots=shots,
                    ),
                ))
                if run_id:
                    evidence_repository.append_event(
                        run_id,
                        "STORYBOARD_OPERATIONAL_IDENTITIES_CANONICALIZED",
                        "info",
                        "已把分镜内部身份投影为人物与声音业务身份",
                        payload={
                            "repairs": identity_repairs,
                            "outline_repairs": outline_identity_repairs,
                        },
                    )
        cp.validated_prefix_end = len(shots)
        cp.next_shot_no = len(shots) + 1
        cp.validated_shot_artifact_ids = [
            row["storyboard_artifact_id"]
            for row in prefix_rows
            if row["storyboard_artifact_id"]
        ]
        return shots

    def _pause_with_unpublished_storyboard(
        candidate_shots: list[Shot], *, reason: str,
    ) -> SupervisorCheckpoint | None:
        """Keep working rows recoverable while making them explicitly unpublishable.

        This is a pause/checkpoint operation, never a quality fallback.  In
        particular, a narrative board loses any prior blind-review authority;
        neither the current-best candidate nor an older pass may be promoted
        after the current gate failed.
        """
        if not _run_has_write_ownership():
            return cp
        if not candidate_shots:
            return None
        shot_count = len(candidate_shots)
        message = (
            f"整集校验仍未通过；{shot_count} 个工作分镜仅作恢复检查点，"
            f"未发布且不可确认：{reason}"
        )
        if screenplay.narrative_plan is not None:
            from app.narrative_review import invalidate_episode_narrative_review

            invalidate_episode_narrative_review(
                conn,
                episode_id,
                "storyboard_gate_failed:" + reason[:300],
            )
            conn.execute(
                "UPDATE episodes SET status='scripted',script_error=?,storyboard_warning=?,"
                "narrative_status='needs_review',narrative_review_artifact_id=NULL WHERE id=?",
                (message[:800], message[:800], episode_id),
            )
        else:
            conn.execute(
                "UPDATE episodes SET status='scripted',script_error=?,storyboard_warning=? WHERE id=?",
                (message[:800], message[:800], episode_id),
            )
        conn.commit()
        cp.phase = "WAITING_HUMAN"
        cp.outcome = "WAITING_RETRY_GATE_REPAIR_EXHAUSTED"
        cp.validated_prefix_end = shot_count
        cp.next_shot_no = shot_count + 1
        cp.last_repair = {
            **(cp.last_repair or {}),
            "status": "unpublished_checkpoint_preserved",
            "reason": reason,
        }
        cp.repair_candidate_shots = []
        save_checkpoint(cp, run_id=run_id)
        if run_id:
            evidence_repository.append_event(
                run_id,
                "STORYBOARD_GATE_RETRY_EXHAUSTED",
                "warning",
                "分镜门禁重试耗尽，工作副本未发布并已禁止确认",
                payload={
                    "shot_count": shot_count,
                    "reason": reason,
                    "published": False,
                },
            )
        return cp

    existing_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    completed: list[Shot] = _reload_completed(existing_rows)
    if completed and _restore_misplaced_shot_fields_from_provider(
        conn,
        episode_id=episode_id,
        rows=existing_rows,
        board=Storyboard(
            episode_no=ep_data["episode_no"],
            shots=list(completed),
        ),
        outline=outline,
        run_id=run_id,
    ):
        completed = _reload_completed()
    if _repair_is_pending(cp):
        completed = _repair_context_shots(conn, cp, ep_data["episode_no"])
    if completed and not narrative_authority:
        recovered_board = Storyboard(episode_no=ep_data["episode_no"], shots=list(completed))
        character_changes = normalize_offbible_characters(recovered_board, bible)
        # 恢复路径面对的是已经落库的旧合同。规范化器已原子剥离可见角色、
        # 声轨和引用中的幽灵身份，并把台词文本保留为修复证据；在这里再抛错
        # 会让确定性修复永远无法落盘。新生成候选仍在下方严格拒绝未知身份。
        _persist_storyboard_character_policy_repairs(
            conn, episode_id, recovered_board, character_changes
        )
        completed = list(recovered_board.shots)

    _, max_shots = storyboard_shot_count_range(ep_data["target_duration_s"])
    planned_persisted = len(outline.shots) if (outline and outline.shots) else 0
    final_feedback: list[str] | None = None
    needs_outline = outline is None

    def _run_has_write_ownership() -> bool:
        if not run_id:
            return True
        run_row = conn.execute(
            "SELECT status FROM workflow_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            return True
        owner = conn.execute(
            "SELECT active_storyboard_run_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        return bool(
            run_row["status"] in {"CREATED", "RUNNING"}
            and owner
            and owner["active_storyboard_run_id"] == run_id
        )

    # Every activation is independently bounded. ``repair_epoch`` is lifetime
    # audit only and must never make a newly-authorized activation a no-op.
    while True:
        if not _run_has_write_ownership():
            return cp
        # 用户 pause / handoff：在安全边界生效
        from app.storyboard_control import consume_control
        ctrl = consume_control(episode_id)
        if ctrl == "pause":
            cp.phase = "PAUSED_EXTERNAL"
            cp.outcome = "PAUSED_BY_USER"
            save_checkpoint(cp, run_id=run_id)
            conn.execute(
                "UPDATE episodes SET status='scripting', script_error=? WHERE id=?",
                ("用户暂停：已保留已验证 checkpoint，可继续自动修复", episode_id),
            )
            conn.commit()
            if run_id:
                evidence_repository.append_event(
                    run_id, "SUPERVISOR_PAUSED", "info", "用户暂停",
                    payload={"phase": cp.phase, "prefix": cp.validated_prefix_end},
                )
            return cp
        if ctrl == "handoff":
            cp.phase = "WAITING_HUMAN"
            cp.outcome = "WAITING_HUMAN_HANDOFF"
            save_checkpoint(cp, run_id=run_id)
            conn.execute(
                "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                ("已转人工处理：自动修复已停止，已验证镜头与问题清单已保留", episode_id),
            )
            conn.commit()
            if run_id:
                evidence_repository.append_event(
                    run_id, "SUPERVISOR_HANDOFF", "info", "转人工处理",
                    payload={"phase": cp.phase, "last_repair": cp.last_repair},
                )
            return cp

        # ---- 大纲 ----
        if needs_outline or cp.phase in {"PLANNING_OUTLINE", "REPAIRING"} and outline is None:
            cp.phase = "PLANNING_OUTLINE"
            save_checkpoint(cp, run_id=run_id)
            try:
                outline = await generate_storyboard_outline(
                    ep_data, source_text, bible,
                    prev_ending=prev["cliffhanger"] if prev else "",
                    screenplay=screenplay,
                )
            except Exception as exc:  # noqa: BLE001
                if not _run_has_write_ownership():
                    return cp
                if _is_retryable_external_error(exc):
                    return _pause_for_external_error(
                        cp,
                        conn,
                        episode_id,
                        exc,
                        run_id=run_id,
                        action="storyboard_outline_provider",
                    )
                public = errors.record_and_format(
                    exc, action="storyboard_outline_degraded",
                    context={"episode_id": episode_id},
                )
                conn.execute(
                    "UPDATE episodes SET storyboard_warning=? WHERE id=?",
                    (f"分镜大纲失败：{public}", episode_id),
                )
                conn.commit()
                raise StageError("分镜大纲", [public]) from exc
            if not _run_has_write_ownership():
                return cp
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=?, storyboard_warning=NULL WHERE id=?",
                (outline.model_dump_json(), episode_id),
            )
            conn.commit()
            cp.outline_artifact_id = str(
                getattr(outline, "evidence_artifact_id", "") or ""
            ) or None
            cp.phase = "VALIDATING_OUTLINE"
            cp.expected_total = len(outline.shots)
            planned_persisted = len(outline.shots)
            needs_outline = False
            if run_id:
                evidence_repository.append_event(
                    run_id, "OUTLINE_VALIDATED", "info",
                    f"大纲通过，共 {len(outline.shots)} 镜",
                )
            save_checkpoint(cp, run_id=run_id)

        # ---- 按场景批量生成（新合同）----
        active_repair = cp.last_repair or {}
        repair_pending = _repair_is_pending(cp)
        pack_outline = (
            _repair_outline_for_checkpoint(cp, outline)
            if repair_pending
            else outline
        )
        scene_pack_failure: BaseException | None = None
        failed_scene_ids: set[str] = set()
        failed_batch_ends: list[int] = []
        scene_pack_fallback_end: int | None = None
        if (
            pack_outline is not None
            and not pack_outline.scene_contexts
            and not completed
        ):
            derived_contexts = ensure_storyboard_scene_contexts(
                pack_outline,
                screenplay,
                bible,
            )
            if derived_contexts:
                if not repair_pending:
                    conn.execute(
                        "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                        (pack_outline.model_dump_json(), episode_id),
                    )
                    conn.commit()
                else:
                    cp.last_repair = {
                        **active_repair,
                        "candidate_outline": pack_outline.model_dump(
                            mode="json"
                        ),
                    }
                    active_repair = cp.last_repair
                    save_checkpoint(cp, run_id=run_id)
                if run_id:
                    evidence_repository.append_event(
                        run_id,
                        "STORYBOARD_SCENE_BATCHES_DERIVED",
                        "info",
                        f"已由批准大纲确定性划分 {len(derived_contexts)} 个场景批次",
                        payload={"batches": derived_contexts},
                    )

        scene_pack_batches = (
            _storyboard_scene_pack_batches(pack_outline)
            if pack_outline is not None else []
        )
        full_repair_scene_pack = bool(
            repair_pending
            and pack_outline is not None
            and str(active_repair.get("mode") or "") == "replace"
            and int(active_repair.get("window_start") or 0) == 1
            and int(active_repair.get("window_end") or 0)
            == len(pack_outline.shots)
        )
        scene_pack_mode = bool(
            pack_outline is not None
            and pack_outline.scene_contexts
            and (not repair_pending or full_repair_scene_pack)
        )
        if scene_pack_mode:
            scene_ends = {
                int(batch["end"])
                for batch in scene_pack_batches
            }
            # Each bounded scene chunk is committed atomically. Checkpoints at
            # older, non-chunk boundaries use the per-shot compatibility path.
            scene_pack_mode = len(completed) == 0 or len(completed) in scene_ends
            if not scene_pack_mode:
                next_shot_no = len(completed) + 1
                scene_pack_fallback_end = next((
                    int(batch["end"])
                    for batch in scene_pack_batches
                    if int(batch["start"]) == next_shot_no
                ), None)

        if scene_pack_mode:
            # Only generate the contiguous frontier chunk.  The old implementation
            # scheduled every remaining scene and awaited one giant gather before
            # committing anything.  A 142-shot outline could therefore spend many
            # minutes creating candidate artifacts while the UI correctly saw zero
            # persisted shots; one bad scene also wasted every later scene call.
            # Each chunk is already bounded (normally <= 8 shots), so generate,
            # validate and commit it before advancing to the next chunk.
            frontier_batch = next((
                batch
                for batch in scene_pack_batches
                if int(batch["end"]) > len(completed)
            ), None)
            pending_batches = (
                [frontier_batch]
                if (
                    frontier_batch is not None
                    and str(frontier_batch["key"])
                    not in cp.scene_pack_candidates
                )
                else []
            )
            if pending_batches:
                batch = pending_batches[0]
                context = batch["context"]
                try:
                    result = await generate_storyboard_scene_pack(
                        ep_data,
                        source_text,
                        bible,
                        screenplay,
                        pack_outline,
                        batch["context"],
                        shot_nos=set(batch["shot_nos"]),
                    )
                except Exception as exc:  # noqa: BLE001 - isolate this chunk
                    result = exc
                if not _run_has_write_ownership():
                    return cp
                if isinstance(result, BaseException):
                    failed_scene_ids.add(context.scene_id)
                    failed_batch_ends.append(int(batch["end"]))
                    scene_pack_failure = result
                else:
                    cp.scene_pack_candidates[str(batch["key"])] = (
                        result.model_dump(mode="json")
                    )
                save_checkpoint(cp, run_id=run_id)

            from app.validators import (
                normalize_dialogue_focus_offscreen_mentions,
            )

            for batch in scene_pack_batches:
                context = batch["context"]
                batch_key = str(batch["key"])
                scene_start = int(batch["start"])
                scene_end = int(batch["end"])
                if scene_end <= len(completed):
                    cp.scene_pack_candidates.pop(batch_key, None)
                    continue
                if scene_start != len(completed) + 1:
                    break
                raw_pack = cp.scene_pack_candidates.get(batch_key)
                if raw_pack is None:
                    break
                pack = StoryboardScenePack.model_validate(raw_pack)
                candidate_board = Storyboard(
                    episode_no=ep_data["episode_no"],
                    shots=[*completed, *pack.shots],
                )
                normalize_continuity(candidate_board)
                if narrative_authority:
                    if not full_repair_scene_pack:
                        _normalize_completed_authority_board(candidate_board)
                    from app.identity_contracts import (
                        canonicalize_storyboard_operational_identities,
                    )
                    canonicalize_storyboard_operational_identities(
                        candidate_board,
                        bible,
                        screenplay,
                    )
                else:
                    character_changes = normalize_offbible_characters(
                        candidate_board,
                        bible,
                    )
                    stripped = sorted({
                        str(change.get("stripped") or "").strip()
                        for change in character_changes
                        if str(change.get("stripped") or "").strip()
                    })
                    if stripped:
                        failed_scene_ids.add(context.scene_id)
                        failed_batch_ends.append(scene_end)
                        scene_pack_failure = StageError(
                            "场景分镜人物合同",
                            [
                                "场景包残留未解析人物身份："
                                + "、".join(stripped)
                            ],
                        )
                        break
                    normalize_dialogue_focus_offscreen_mentions(
                        candidate_board,
                        bible,
                    )
                relieve_spoken_overflow(candidate_board)
                prefer_default_shot_durations(
                    candidate_board,
                    narrative_authority=narrative_authority,
                    narrative_plan=screenplay.narrative_plan,
                )
                if not narrative_authority:
                    normalize_transition_visuals(candidate_board)
                candidate_errors = (
                    validate_storyboard(
                        candidate_board,
                        storyboard_planning_bible(bible, pack_outline),
                        int(ep_data.get("target_duration_s") or 0),
                        narrative_authority=True,
                        narrative_plan=screenplay.narrative_plan,
                        screenplay=screenplay,
                    )
                    if narrative_authority
                    else []
                )
                if narrative_authority:
                    candidate_errors.extend(
                        validate_storyboard_visual_identity_contract(
                            candidate_board,
                            pack_outline,
                            storyboard_planning_bible(
                                bible,
                                pack_outline,
                            ),
                            screenplay,
                            episode_id=episode_id,
                        )
                    )
                if candidate_errors:
                    failed_scene_ids.add(context.scene_id)
                    failed_batch_ends.append(scene_end)
                    scene_pack_failure = StageError(
                        "场景分镜批量校验",
                        candidate_errors,
                    )
                    break
                expected_screenplay_artifact_id = cp.input_versions.get(
                    "screenplay_artifact_id"
                )
                if not _run_has_write_ownership():
                    return cp
                if full_repair_scene_pack:
                    cp.repair_candidate_shots = [
                        _shot_checkpoint_payload(shot)
                        for shot in candidate_board.shots
                    ]
                    completed = list(candidate_board.shots)
                    cp.scene_pack_candidates.pop(batch_key, None)
                    cp.phase = "GENERATING_SHOTS"
                    cp.expected_total = len(pack_outline.shots)
                    cp.next_shot_no = len(completed) + 1
                    cp.last_repair = {
                        **(cp.last_repair or {}),
                        "status": "candidate_generating",
                        "candidate_count": len(
                            cp.repair_candidate_shots
                        ),
                    }
                    save_checkpoint(cp, run_id=run_id)
                    if run_id:
                        evidence_repository.append_event(
                            run_id,
                            "SCENE_PACK_REPAIR_CANDIDATE_CHECKPOINTED",
                            "info",
                            (
                                f"{context.scene_id} 已保存第 "
                                f"{scene_start}~{scene_end} 镜隔离候选"
                            ),
                            payload={
                                "batch_key": batch_key,
                                "scene_id": context.scene_id,
                                "shot_start": scene_start,
                                "shot_end": scene_end,
                                "candidate_count": len(
                                    cp.repair_candidate_shots
                                ),
                            },
                        )
                    continue
                conn.execute("SAVEPOINT scene_pack_commit")
                try:
                    _sync_storyboard_shot_timing(
                        conn,
                        episode_id,
                        candidate_board,
                        expected_screenplay_artifact_id,
                    )
                    for shot in candidate_board.shots[len(completed):]:
                        _insert_storyboard_shot(
                            conn,
                            episode_id,
                            screenplay,
                            shot,
                            expected_screenplay_artifact_id,
                        )
                    conn.execute(
                        "UPDATE episodes SET status='scripting',script_error=NULL WHERE id=?",
                        (episode_id,),
                    )
                    conn.execute("RELEASE SAVEPOINT scene_pack_commit")
                except Exception:
                    conn.execute("ROLLBACK TO SAVEPOINT scene_pack_commit")
                    conn.execute("RELEASE SAVEPOINT scene_pack_commit")
                    raise
                conn.commit()
                completed = _reload_completed()
                cp.scene_pack_candidates.pop(batch_key, None)
                cp.phase = "GENERATING_SHOTS"
                cp.expected_total = len(outline.shots)
                cp.validated_prefix_end = len(completed)
                cp.next_shot_no = len(completed) + 1
                save_checkpoint(cp, run_id=run_id)
                if run_id:
                    evidence_repository.append_event(
                        run_id,
                        "SCENE_PACK_BATCH_COMMITTED",
                        "info",
                        f"{context.scene_id} 已原子提交第 {scene_start}~{scene_end} 镜",
                        payload={
                            "batch_key": batch_key,
                            "scene_id": context.scene_id,
                            "scene_no": context.scene_no,
                            "shot_start": scene_start,
                            "shot_end": scene_end,
                        },
                    )

            if scene_pack_failure is None and any(
                int(batch["end"]) > len(completed)
                for batch in scene_pack_batches
            ):
                # Commit progress is now visible to the UI.  Re-enter the outer
                # loop to generate the next bounded chunk instead of falling into
                # the legacy per-shot path for every remaining scene.
                cp.phase = "GENERATING_SHOTS"
                cp.expected_total = len(outline.shots)
                cp.validated_prefix_end = len(completed)
                cp.next_shot_no = len(completed) + 1
                save_checkpoint(cp, run_id=run_id)
                continue

            if scene_pack_failure is not None:
                if _is_retryable_external_error(scene_pack_failure):
                    return _pause_for_external_error(
                        cp,
                        conn,
                        episode_id,
                        scene_pack_failure,
                        run_id=run_id,
                        action="storyboard_scene_pack_provider",
                    )
                # A content/schema failure is local to the bounded chunk.
                # Preserve committed chunks and continue only the failed
                # window through the per-shot compatibility path.
                for batch in scene_pack_batches:
                    if batch["context"].scene_id in failed_scene_ids:
                        cp.scene_pack_candidates.pop(
                            str(batch["key"]),
                            None,
                        )
                scene_pack_fallback_end = (
                    min(failed_batch_ends)
                    if failed_batch_ends else None
                )
                cp.phase = "GENERATING_SHOTS"
                cp.outcome = None
                save_checkpoint(cp, run_id=run_id)
                conn.execute(
                    "UPDATE episodes SET status='scripting',script_error=? WHERE id=?",
                    (
                        (
                            "场景批量候选未通过，已自动切换逐镜兼容路径："
                            + str(scene_pack_failure)
                        )[:800],
                        episode_id,
                    ),
                )
                conn.commit()
                if run_id:
                    evidence_repository.append_event(
                        run_id,
                        "SCENE_PACK_FALLBACK_TO_SHOTS",
                        "warning",
                        "场景批量候选未通过，仅失败场景切换逐镜生成",
                        payload={
                            "error": str(scene_pack_failure)[:1000],
                            "failed_scene_ids": sorted(failed_scene_ids),
                            "fallback_end_shot_no": scene_pack_fallback_end,
                            "preserved_scene_ids": sorted(
                                cp.scene_pack_candidates
                            ),
                        },
                    )

        # ---- 逐镜兼容/修复路径 ----
        if _repair_is_pending(cp):
            completed = _repair_context_shots(conn, cp, ep_data["episode_no"])
        cp.phase = "GENERATING_SHOTS"
        cp.outcome = None
        # 恢复自 CANCELLED / WAITING_* 检查点时，先持久化新的运行相位，再进入可能
        # 耗时数分钟的模型调用；否则页面会在 Run 已运行时继续显示旧终态。
        save_checkpoint(cp, run_id=run_id)
        shot_loop_broke_for_repair = False
        while True:
            active_repair = cp.last_repair or {}
            repair_pending = _repair_is_pending(cp)
            generation_outline = (
                _repair_outline_for_checkpoint(cp, outline)
                if repair_pending else outline
            )
            if repair_pending and len(completed) >= int(active_repair.get("window_end") or 0):
                break
            if (
                scene_pack_fallback_end is not None
                and len(completed) >= scene_pack_fallback_end
            ):
                break
            planned_now = (
                len(generation_outline.shots)
                if (generation_outline and generation_outline.shots) else 0
            )
            if not repair_pending and _storyboard_generation_is_complete(
                completed, planned_now, max_shots,
            ):
                break
            if repair_pending and len(completed) >= max_shots:
                break

            # An interrupted legacy run may have marked the old tail final even
            # though the persisted outline still has work.  Clear it in memory;
            # it is written back only in the same transaction as the next
            # validated shot, so a failed provider call never mutates the
            # official projection.
            if (
                not repair_pending
                and completed
                and completed[-1].is_final
                and planned_now > len(completed)
            ):
                completed[-1].is_final = False

            shot_no = len(completed) + 1
            cp.next_shot_no = shot_no
            # 每镜开始前再检查一次控制请求
            from app.storyboard_control import peek_control
            if peek_control(episode_id):
                break
            try:
                generation_bible = (
                    storyboard_planning_bible(bible, generation_outline)
                    if generation_outline is not None
                    else bible
                )
                draft = await generate_storyboard_next_shot(
                    ep_data, source_text, generation_bible,
                    prev_ending=prev["cliffhanger"] if prev else "",
                    screenplay=screenplay,
                    completed_shots=completed,
                    final_feedback=final_feedback,
                    outline=generation_outline,
                    repair_feedback=_repair_feedback_for_shot(
                        list(active_repair.get("issue_messages") or []), shot_no,
                    ) if repair_pending else None,
                    semantic_attempt_id=active_repair.get("semantic_attempt_id") if repair_pending else None,
                )
            except StageError as exc:
                if not _run_has_write_ownership():
                    return cp
                stopped = _stop_after_exhausted_agent_loop(
                    cp,
                    exc,
                    shot_no=shot_no,
                    run_id=run_id,
                )
                if stopped is not None:
                    iterations = max(1, int(exc.iterations or 0))
                    message = (
                        f"第 {shot_no} 镜自动修复已达到 {iterations} 轮上限，"
                        f"任务已安全停止；已保留前 {len(completed)} 个通过镜头。"
                    )
                    conn.execute(
                        "UPDATE episodes SET status='scripted',script_error=? WHERE id=?",
                        (message[:800], episode_id),
                    )
                    conn.commit()
                    return stopped
                plan = await _route_with_narrative_diagnosis(
                    list(exc.errors) if hasattr(exc, "errors") else [str(exc)],
                    board=Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)),
                    next_shot_no=shot_no,
                )
                if not _run_has_write_ownership():
                    return cp
                cp = _apply_repair(
                    cp, plan, conn, episode_id, completed, outline,
                    repair_screenplay=screenplay,
                    narrative_repair_active=narrative_authority,
                )
                if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL"}:
                    if cp.outcome in {
                        "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
                        "WAITING_RETRY_ACTIVATION_BUDGET",
                    }:
                        fallback = _pause_with_unpublished_storyboard(
                            completed, reason=(cp.last_repair or {}).get("reason") or str(exc),
                        )
                        if fallback is not None:
                            return fallback
                    conn.execute(
                        "UPDATE episodes SET status='scripted',script_error=? WHERE id=?",
                        (((cp.last_repair or {}).get("reason") or plan.reason)[:800], episode_id),
                    )
                    conn.commit()
                    return cp
                completed = _repair_context_shots(conn, cp, ep_data["episode_no"])
                shot_loop_broke_for_repair = True
                break
            except Exception as exc:  # noqa: BLE001
                # Provider 类故障 → 可恢复暂停
                if _is_retryable_external_error(exc):
                    return _pause_for_external_error(
                        cp,
                        conn,
                        episode_id,
                        exc,
                        run_id=run_id,
                        action="storyboard_shot_provider",
                    )
                raise

            if not _run_has_write_ownership():
                return cp
            disposition = getattr(draft, "disposition", "PASS")
            blockers = _blocker_messages(draft)

            # NEEDS_REPLAN 或 blocker：不落主 shots
            if disposition == "NEEDS_REPLAN" or blockers:
                # 仍可把 candidate artifact 保留在 draft.evidence_artifact_id
                plan = await _route_with_narrative_diagnosis(
                    blockers or list(getattr(draft, "residual_errors", []) or []),
                    board=Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)),
                    next_shot_no=shot_no,
                )
                if not _run_has_write_ownership():
                    return cp
                if run_id:
                    evidence_repository.append_event(
                        run_id, "REPAIR_PLAN_SELECTED", "info",
                        f"已选择修复策略：{plan.strategy}，回退至第 {plan.invalidation_frontier} 镜",
                        payload=plan.model_dump(mode="json"),
                )
                cp = _apply_repair(
                    cp, plan, conn, episode_id, completed, outline,
                    repair_screenplay=screenplay,
                    narrative_repair_active=narrative_authority,
                )
                if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL"}:
                    reason = (cp.last_repair or {}).get("reason") or plan.reason
                    if cp.outcome in {
                        "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
                        "WAITING_RETRY_ACTIVATION_BUDGET",
                    }:
                        paused = _pause_with_unpublished_storyboard(
                            completed,
                            reason=reason,
                        )
                        if paused is not None:
                            return paused
                    save_checkpoint(cp, run_id=run_id)
                    conn.execute(
                        "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                        (str(reason)[:800], episode_id),
                    )
                    conn.commit()
                    return cp
                else:
                    completed = _repair_context_shots(conn, cp, ep_data["episode_no"])
                    shot_loop_broke_for_repair = True
                    break

            # PASS / warning-only → 落库 validated
            board = Storyboard(episode_no=ep_data["episode_no"], shots=[*completed, draft.shot])
            if not narrative_authority:
                normalize_continuity(board)
            else:
                from app.identity_contracts import (
                    canonicalize_storyboard_operational_identities,
                )

                canonicalize_storyboard_operational_identities(
                    board,
                    bible,
                    screenplay,
                )
            character_changes = (
                []
                if narrative_authority
                else normalize_offbible_characters(board, bible)
            )
            stripped = sorted({
                str(change.get("stripped") or "").strip()
                for change in character_changes
                if str(change.get("stripped") or "").strip()
            })
            if stripped:
                raise StageError("分镜人物合同", [
                    "分镜候选残留未解析人物身份："
                    + "、".join(stripped)
                    + "；未写入镜头，请严格使用发布剧本中的人物身份"
                ])
            if not narrative_authority:
                from app.validators import normalize_dialogue_focus_offscreen_mentions

                normalize_dialogue_focus_offscreen_mentions(board, bible)
                relieve_spoken_overflow(board)
                prefer_default_shot_durations(board)
                normalize_transition_visuals(board)
            if generation_outline is not None:
                normalize_storyboard_direction_fields(
                    board,
                    generation_outline,
                    screenplay,
                )
            expected_screenplay_artifact_id = cp.input_versions.get("screenplay_artifact_id")
            shot = board.shots[-1]
            shot.is_final = bool(draft.is_final)
            shot.prompt_contract_version = "renderability_v1"
            object.__setattr__(shot, "evidence_artifact_id", getattr(draft, "evidence_artifact_id", None))
            if repair_pending:
                target_data = active_repair.get("spine_target") or {}
                spine_target = (
                    (
                        str(target_data.get("beat_id") or ""),
                        str(target_data.get("who") or ""),
                        str(target_data.get("does") or ""),
                    )
                    if target_data.get("beat_id") and target_data.get("does")
                    else None
                )
                repair_brief = (
                    generation_outline.shots[shot_no - 1]
                    if (
                        generation_outline is not None
                        and 0 < shot_no <= len(generation_outline.shots)
                    )
                    else None
                )
                _retarget_spine_repair_shot(
                    shot,
                    repair_brief,
                    spine_target,
                )
                cp.repair_candidate_shots.append(_shot_checkpoint_payload(shot))
                cp.last_repair = {
                    **(cp.last_repair or {}),
                    "status": "candidate_generating",
                    "candidate_count": len(cp.repair_candidate_shots),
                }
                completed.append(shot)
                cp.next_shot_no = len(completed) + 1
                save_checkpoint(cp, run_id=run_id)
                continue

            _sync_storyboard_shot_timing(
                conn, episode_id, board, expected_screenplay_artifact_id
            )
            _insert_storyboard_shot(
                conn, episode_id, screenplay, shot, expected_screenplay_artifact_id
            )
            conn.execute(
                "UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,)
            )
            conn.commit()
            completed = _reload_completed()
            revision = (
                None
                if narrative_authority
                else _reconcile_storyboard_plan(
                    conn,
                    episode_id,
                    ep_data["episode_no"],
                    outline,
                    completed,
                    planned_persisted,
                )
            )
            if revision is not None:
                planned_persisted = revision[1]
                cp.expected_total = revision[1]
            if run_id:
                evidence_repository.append_event(
                    run_id, "SHOT_CHECKPOINT_VALIDATED", "info",
                    f"第 {shot.shot_no} 镜已通过",
                    payload={"shot_no": shot.shot_no},
                )
            save_checkpoint(cp, run_id=run_id)

            if draft.is_final:
                break
            final_feedback = (
                None
                if narrative_authority
                else validate_storyboard_preserves_key_content(
                    Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)),
                    screenplay,
                ) or None
            )

        if shot_loop_broke_for_repair:
            continue
        if (
            scene_pack_fallback_end is not None
            and len(completed) >= scene_pack_fallback_end
            and outline is not None
            and len(completed) < len(outline.shots)
        ):
            cp.phase = "GENERATING_SHOTS"
            cp.next_shot_no = len(completed) + 1
            save_checkpoint(cp, run_id=run_id)
            continue

        # A repair candidate is fully generated in memory/artifacts. Validate
        # the merged board before changing any official shot row.
        if _repair_is_pending(cp):
            official_rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
            ).fetchall()
            official_board = _board_from_rows(official_rows, ep_data["episode_no"])
            repair = cp.last_repair or {}
            if str(repair.get("base_hash") or "") != _storyboard_hash(official_board):
                cp.phase = "WAITING_HUMAN"
                cp.outcome = "WAITING_RETRY_CAS_CONFLICT"
                cp.last_repair = {**repair, "status": "paused", "reason": "working_projection_changed"}
                save_checkpoint(cp, run_id=run_id)
                conn.execute(
                    "UPDATE episodes SET status='scripted',script_error=? WHERE id=?",
                    ("分镜在自动修复期间已被其他操作修改，本次候选未覆盖现有内容", episode_id),
                )
                conn.commit()
                return cp
            candidate_board = _merge_repair_candidate(official_board, cp)
            mode = str(repair.get("mode") or "replace")
            committed_outline = _repair_outline_for_checkpoint(cp, outline)
            candidate_episode = dict(ep_data)
            if committed_outline is not None:
                candidate_episode["storyboard_outline_json"] = (
                    committed_outline.model_dump_json()
                )
            candidate_evaluation = evaluate_storyboard_for_confirmation(
                candidate_episode,
                candidate_board,
                screenplay,
                bible,
                has_real_bible=has_real_bible,
            )
            candidate_board = _validated_candidate_projection(
                official_board, candidate_evaluation.board, cp,
            )
            before_codes = set(repair.get("issue_codes") or [])
            repair_required_after = [
                str(getattr(issue, "message", "") or "")
                for issue in (candidate_evaluation.issues or [])
                if _storyboard_warning_requires_auto_repair(issue)
            ]
            after_messages = [*candidate_evaluation.errors, *repair_required_after]
            from app.evaluations.issues import issue_code as _issue_code
            after_codes = {
                *[getattr(issue, "code", "") for issue in (candidate_evaluation.issues or [])],
                *[_issue_code(message) for message in candidate_evaluation.errors],
            }
            after_codes.discard("")
            before_messages = list(repair.get("issue_messages") or [])
            improved = _repair_candidate_made_progress(
                mode=mode,
                candidate_passed=candidate_evaluation.passed,
                before_messages=before_messages,
                after_messages=after_messages,
                window_start=int(repair.get("window_start") or 1),
                window_end=int(repair.get("window_end") or 1),
            )
            no_op = _storyboard_hash(candidate_board) == _storyboard_hash(official_board)
            if no_op or not improved:
                if run_id:
                    evidence_repository.append_event(
                        run_id, "STORYBOARD_REPAIR_NO_PROGRESS", "warning",
                        "候选未改变内容或未解决目标问题，已拒绝覆盖正式分镜",
                        payload={"no_op": no_op, "before_codes": sorted(before_codes),
                                 "after_codes": sorted(after_codes)},
                    )
                retry_plan = await _route_with_narrative_diagnosis(
                    before_messages or after_messages or ["storyboard repair made no progress"],
                    board=official_board,
                )
                if not _run_has_write_ownership():
                    return cp
                cp = _apply_repair(
                    cp, retry_plan, conn, episode_id, list(official_board.shots), outline,
                    repair_screenplay=screenplay,
                    narrative_repair_active=narrative_authority,
                )
                if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL"}:
                    if cp.outcome in {
                        "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
                        "WAITING_RETRY_ACTIVATION_BUDGET",
                    }:
                        fallback = _pause_with_unpublished_storyboard(
                            list(official_board.shots), reason="repair_candidate_no_progress",
                        )
                        if fallback is not None:
                            return fallback
                    conn.execute(
                        "UPDATE episodes SET status='scripted',script_error=? WHERE id=?",
                        ("自动修复未产生质量改善，已安全停止且未修改现有分镜", episode_id),
                    )
                    conn.commit()
                    return cp
                completed = _repair_context_shots(conn, cp, ep_data["episode_no"])
                continue

            _commit_repair_candidate(
                conn, cp,
                episode_id=episode_id,
                screenplay=screenplay,
                current_board=official_board,
                candidate_board=candidate_board,
                expected_screenplay_artifact_id=cp.input_versions.get("screenplay_artifact_id"),
                run_id=run_id,
            )
            if committed_outline is not None:
                outline = committed_outline
                planned_persisted = len(outline.shots)
                cp.expected_total = planned_persisted
            cp.last_repair = {
                **repair,
                "status": "applied",
                "after_hash": _storyboard_hash(candidate_board),
                "remaining_issue_codes": sorted(after_codes),
            }
            cp.repair_candidate_shots = []
            completed = _reload_completed()
            cp.phase = "VALIDATING_EPISODE"
            cp.outcome = None
            save_checkpoint(cp, run_id=run_id)
            continue

        # 逐镜循环因控制请求 break：回主循环顶部消费
        from app.storyboard_control import peek_control as _peek
        if _peek(episode_id):
            continue

        # ---- 整集校验 ----
        cp.phase = "VALIDATING_EPISODE"
        full_board = Storyboard(episode_no=ep_data["episode_no"], shots=list(completed))
        planned_now = len(outline.shots) if (outline and outline.shots) else 0
        if (
            (planned_now > 0 and len(full_board.shots) != planned_now)
            or not full_board.shots
            or not full_board.shots[-1].is_final
        ):
            fallback = _pause_with_unpublished_storyboard(
                list(full_board.shots),
                reason=(
                    f"生成次数耗尽：已完成 {len(full_board.shots)}/{planned_now or '?'} 镜"
                ),
            )
            if fallback is not None:
                return fallback
            cp.phase = "WAITING_HUMAN"
            cp.outcome = "WAITING_RETRY_NO_STORYBOARD_ARTIFACT"
            save_checkpoint(cp, run_id=run_id)
            return cp
        direction_repairs = (
            normalize_storyboard_direction_fields(
                full_board,
                outline,
                screenplay,
            )
            if outline is not None
            else []
        )
        if direction_repairs:
            current_rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                (episode_id,),
            ).fetchall()
            for row, shot in zip(current_rows, full_board.shots):
                _write_shot_fields(
                    conn,
                    str(row["id"]),
                    shot,
                    row["storyboard_artifact_id"],
                    narrative_authority=narrative_authority,
                )
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                (outline.model_dump_json(), episode_id),
            )
            conn.commit()
            completed = list(full_board.shots)
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "STORYBOARD_DIRECTION_FIELDS_REBOUND",
                    "info",
                    "已从批准大纲确定性补齐分镜导演字段",
                    payload={"repairs": direction_repairs},
                )
        evaluation_bible = (
            storyboard_planning_bible(bible, outline)
            if outline is not None
            else bible
        )
        evaluation = evaluate_storyboard_for_confirmation(
            ep_data,
            full_board,
            screenplay,
            evaluation_bible,
            has_real_bible=has_real_bible,
        )
        repair_required_warnings = [
            issue for issue in (evaluation.issues or [])
            if _storyboard_warning_requires_auto_repair(issue)
        ]
        # The broad aesthetic QA remains score-only. Director-scene invariants
        # are structural: missing context, an empty-purpose shot, or an
        # unreadable action/emotion camera plan cannot be published.
        runtime_blocking_errors = [
            *evaluation.errors,
            *validate_storyboard_direction_contract(
                evaluation.board,
                outline,
            ),
            *(
                validate_storyboard_visual_identity_contract(
                    evaluation.board,
                    outline,
                    evaluation_bible,
                    screenplay,
                    episode_id=episode_id,
                )
                if outline is not None and narrative_authority
                else []
            ),
        ]
        if runtime_blocking_errors:
            repair_warning_messages = [
                str(getattr(issue, "message", "") or "")
                for issue in repair_required_warnings
                if str(getattr(issue, "message", "") or "").strip()
            ]
            repair_inputs = [
                *runtime_blocking_errors,
                *repair_warning_messages,
            ]
            if run_id:
                evidence_repository.append_event(
                    run_id, "EPISODE_VALIDATION_FAILED", "warning",
                    f"整集校验发现 {len(repair_inputs)} 项问题，进入定向修复",
                    payload={
                        "errors": evaluation.errors[:12],
                        "repair_required_warnings": [
                            getattr(issue, "message", str(issue))
                            for issue in repair_required_warnings[:12]
                        ],
                    },
                )
            plan = await _route_with_narrative_diagnosis(
                repair_inputs,
                board=evaluation.board,
            )
            if not _run_has_write_ownership():
                return cp
            cp = _apply_repair(
                cp, plan, conn, episode_id, completed, outline,
                repair_screenplay=screenplay,
                narrative_repair_active=narrative_authority,
            )
            if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL"}:
                if cp.outcome in {
                    "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
                    "WAITING_RETRY_ACTIVATION_BUDGET",
                }:
                    fallback = _pause_with_unpublished_storyboard(
                        list(evaluation.board.shots), reason="episode_validation_retry_exhausted",
                    )
                    if fallback is not None:
                        return fallback
                save_checkpoint(cp, run_id=run_id)
                conn.execute(
                    "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                    (("；".join(evaluation.errors[:5]))[:800], episode_id),
                )
                conn.commit()
                return cp
            completed = _repair_context_shots(conn, cp, ep_data["episode_no"])
            continue

        # ---- 通过：finalize 后统一等待人工确认 ----
        actual_total = sum(int(s.duration_s or 0) for s in completed)
        synced = (
            int(ep_data["target_duration_s"] or 0)
            if narrative_authority
            else (
                actual_total
                if outline is not None and outline.scene_contexts
                else _compact_episode_target(
                    actual_total or ep_data["target_duration_s"]
                )
            )
        )
        _assert_storyboard_write_authorized(
            conn, episode_id, cp.input_versions.get("screenplay_artifact_id")
        )
        if narrative_authority and not published_storyboard_authority:
            current_rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                (episode_id,),
            ).fetchall()
            if len(current_rows) != len(evaluation.board.shots):
                raise RuntimeError(
                    "分镜发布前投影对账失败：正式镜头数与确认候选不一致"
                )
            projection_repairs: list[dict[str, Any]] = []
            outline_projection_repairs: list[dict[str, Any]] = []
            outline_by_no = {
                int(brief.shot_no): brief
                for brief in (outline.shots if outline is not None else [])
            }
            for row, shot in zip(current_rows, evaluation.board.shots):
                current_shot = _board_from_shot_rows(
                    [row],
                    ep_data["episode_no"],
                ).shots[0]
                before = current_shot.model_dump(mode="json")
                after = shot.model_dump(mode="json")
                changed_fields = sorted(
                    field
                    for field in set(before) | set(after)
                    if before.get(field) != after.get(field)
                )
                brief = outline_by_no.get(int(shot.shot_no))
                if (
                    brief is not None
                    and brief.continuity_mode != shot.continuity_mode
                ):
                    brief.continuity_mode = shot.continuity_mode
                    outline_projection_repairs.append({
                        "shot_no": int(shot.shot_no),
                        "fields": ["continuity_mode"],
                    })
                if not changed_fields:
                    continue
                _write_shot_fields(
                    conn,
                    str(row["id"]),
                    shot,
                    row["storyboard_artifact_id"],
                    narrative_authority=True,
                )
                projection_repairs.append({
                    "shot_no": int(shot.shot_no),
                    "fields": changed_fields,
                })
            if outline_projection_repairs and outline is not None:
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                    (outline.model_dump_json(), episode_id),
                )
            conn.commit()
            rebound_rows = list(_ensure_current_storyboard_shot_artifacts(
                conn,
                episode_id,
                evaluation.board,
            ))
            conn.commit()
            cp.validated_shot_artifact_ids = [
                str(row["storyboard_artifact_id"])
                for row in rebound_rows
                if row["storyboard_artifact_id"]
            ]
            if (projection_repairs or outline_projection_repairs) and run_id:
                evidence_repository.append_event(
                    run_id,
                    "STORYBOARD_PREFINAL_PROJECTION_REBOUND",
                    "info",
                    "已在冷观众审读前对账正式镜头投影与逐镜证据",
                    payload={
                        "repairs": projection_repairs,
                        "outline_repairs": outline_projection_repairs,
                    },
                )
            save_checkpoint(cp, run_id=run_id)
        narrative_review_report = None
        narrative_review_artifact_ids: list[str] = []
        if narrative_authority:
            from app.narrative_review import (
                NarrativeReviewError,
                run_blind_audience_review,
                verify_persisted_narrative_review,
            )
            from app.schemas import NarrativeReviewReport

            checkpoint_review_ids = list(dict.fromkeys(
                str(item)
                for item in (
                    (cp.last_repair or {}).get("narrative_review_artifact_ids")
                    or []
                )
                if str(item)
            ))
            if checkpoint_review_ids:
                try:
                    report_artifacts = [
                        evidence_repository.get_artifact(artifact_id)
                        for artifact_id in checkpoint_review_ids
                    ]
                    usable_reports = [
                        artifact
                        for artifact in report_artifacts
                        if artifact is not None
                        and artifact.get("type") == "narrative_review_report"
                        and artifact.get("status")
                        not in {"stale", "rejected", "superseded", "needs_revision"}
                    ]
                    if len(usable_reports) != 1:
                        raise ValueError("检查点没有唯一可复用的冷观众审读报告")
                    candidate_report = NarrativeReviewReport.model_validate(
                        usable_reports[0].get("content") or {}
                    )
                    verified_report_id = verify_persisted_narrative_review(
                        episode_id=episode_id,
                        screenplay=screenplay,
                        board=evaluation.board,
                        report=candidate_report,
                        artifact_ids=checkpoint_review_ids,
                    )
                    if verified_report_id != usable_reports[0]["id"]:
                        raise ValueError("检查点冷观众审读报告指针漂移")
                    narrative_review_report = candidate_report
                    narrative_review_artifact_ids = checkpoint_review_ids
                    if run_id:
                        evidence_repository.append_event(
                            run_id,
                            "NARRATIVE_REVIEW_REUSED",
                            "info",
                            "分镜内容未变化，复用检查点中已通过的冷观众审读证据",
                            payload={"report_artifact_id": verified_report_id},
                        )
                except Exception:  # noqa: BLE001 - invalid reuse falls back to a fresh review
                    narrative_review_report = None
                    narrative_review_artifact_ids = []
            if narrative_review_report is None:
                current_report_rows = conn.execute(
                    """SELECT id FROM artifacts
                       WHERE type='narrative_review_report'
                         AND scope_type='episode' AND scope_id=?
                         AND status NOT IN (
                             'stale','rejected','superseded','needs_revision'
                         )
                       ORDER BY version DESC""",
                    (episode_id,),
                ).fetchall()
                for row in current_report_rows:
                    candidate_artifact = evidence_repository.get_artifact(
                        str(row["id"])
                    )
                    if candidate_artifact is None:
                        continue
                    candidate_ids = list(dict.fromkeys([
                        str(candidate_artifact["id"]),
                        *[
                            str(item)
                            for item in (
                                candidate_artifact.get("parent_artifact_ids")
                                or []
                            )
                            if str(item)
                        ],
                    ]))
                    try:
                        candidate_report = NarrativeReviewReport.model_validate(
                            candidate_artifact.get("content") or {}
                        )
                        verified_report_id = verify_persisted_narrative_review(
                            episode_id=episode_id,
                            screenplay=screenplay,
                            board=evaluation.board,
                            report=candidate_report,
                            artifact_ids=candidate_ids,
                        )
                    except Exception:  # noqa: BLE001 - try the next immutable report
                        continue
                    narrative_review_report = candidate_report
                    narrative_review_artifact_ids = candidate_ids
                    if run_id:
                        evidence_repository.append_event(
                            run_id,
                            "NARRATIVE_REVIEW_REUSED",
                            "info",
                            "分镜内容未变化，复用当前已通过的冷观众审读证据",
                            payload={"report_artifact_id": verified_report_id},
                        )
                    break
            if narrative_review_report is None:
                try:
                    (
                        _observations,
                        narrative_review_report,
                        narrative_review_artifact_ids,
                    ) = await run_blind_audience_review(
                        episode_id=episode_id,
                        screenplay=screenplay,
                        board=evaluation.board,
                        screenplay_artifact_id=ep["screenplay_artifact_id"],
                    )
                    if not _run_has_write_ownership():
                        return cp
                except NarrativeReviewError as exc:
                    if not _run_has_write_ownership():
                        return cp
                    review_evidence_codes = {
                        "REVIEW_INPUT_SHOT_EVIDENCE_DRIFT",
                        "REVIEW_INPUT_SHOT_EVIDENCE_MISSING",
                        "REVIEW_INPUT_SHOT_EVIDENCE_INVALID",
                    }
                    error_codes = {
                        error[1:error.index("]")]
                        for error in exc.errors
                        if error.startswith("[") and "]" in error
                    }
                    if (
                        error_codes
                        and error_codes.issubset(review_evidence_codes)
                    ):
                        rebound_rows = list(
                            _ensure_current_storyboard_shot_artifacts(
                                conn,
                                episode_id,
                                evaluation.board,
                            )
                        )
                        conn.commit()
                        cp.validated_shot_artifact_ids = [
                            str(row["storyboard_artifact_id"])
                            for row in rebound_rows
                            if row["storyboard_artifact_id"]
                        ]
                        cp.last_repair = {
                            **(cp.last_repair or {}),
                            "status": "review_input_evidence_rebound",
                            "issue_codes": sorted(error_codes),
                        }
                        save_checkpoint(cp, run_id=run_id)
                        if run_id:
                            evidence_repository.append_event(
                                run_id,
                                "NARRATIVE_REVIEW_INPUT_EVIDENCE_REBOUND",
                                "info",
                                "冷观众审读输入证据已确定性重签，未调用语义修复模型",
                                payload={
                                    "issue_codes": sorted(error_codes),
                                },
                            )
                        continue
                    plan = await _route_with_narrative_diagnosis(
                        exc.errors,
                        board=evaluation.board,
                    )
                    if not _run_has_write_ownership():
                        return cp
                    cp = _apply_repair(
                        cp, plan, conn, episode_id, list(evaluation.board.shots), outline,
                        repair_screenplay=screenplay,
                        narrative_repair_active=narrative_authority,
                    )
                    _annotate_blind_review_repair(cp, exc.errors)
                    save_checkpoint(cp, run_id=run_id)
                    if cp.phase in {
                        "WAITING_HUMAN",
                        "WAITING_AUTHORIZATION",
                        "PAUSED_EXTERNAL",
                    }:
                        conn.execute(
                            "UPDATE episodes SET status='scripted', narrative_status='needs_review', script_error=? WHERE id=?",
                            (
                                (
                                    "冷观众审读未通过："
                                    + "；".join(exc.errors[:5])
                                )[:800],
                                episode_id,
                            ),
                        )
                        conn.commit()
                        return cp
                    completed = _repair_context_shots(
                        conn, cp, ep_data["episode_no"],
                    )
                    continue
            try:
                from app.narrative_calibration import (
                    assert_report_meets_current_calibration,
                )

                assert_report_meets_current_calibration(
                    narrative_review_report,
                )
            except Exception as exc:  # noqa: BLE001 - explicit bootstrap gate
                if not _run_has_write_ownership():
                    return cp
                cp.phase = "WAITING_HUMAN"
                cp.outcome = "WAITING_HUMAN_CALIBRATION"
                cp.validated_prefix_end = len(evaluation.board.shots)
                cp.next_shot_no = len(evaluation.board.shots) + 1
                cp.last_repair = {
                    "status": "waiting_human_calibration",
                    "reason": str(exc),
                    "narrative_review_artifact_ids": (
                        narrative_review_artifact_ids
                    ),
                    "candidate_outline_published": False,
                }
                save_checkpoint(cp, run_id=run_id)
                conn.execute(
                    """UPDATE episodes
                          SET status='scripted',
                              narrative_status='needs_review',
                              narrative_review_artifact_id=NULL,
                              narrative_calibration_artifact_id=NULL,
                              script_error=?
                        WHERE id=?""",
                    (
                        (
                            "分镜结构与冷观众审读已完成，正在等待一次观看权威："
                            + str(exc)
                        )[:800],
                        episode_id,
                    ),
                )
                conn.commit()
                return cp
        if not _run_has_write_ownership():
            return cp
        if narrative_authority:
            _finalize_storyboard_evidence(
                episode_id,
                evaluation.board,
                narrative_review_report=narrative_review_report,
                narrative_review_artifact_ids=narrative_review_artifact_ids,
            )
        else:
            _finalize_storyboard_evidence(episode_id, evaluation.board)

        if narrative_authority:
            # Published narrative screenplay fields are immutable authority
            # inputs.  Storyboard completion may validate the duration but may
            # not silently rewrite the screenplay target to match its output.
            conn.execute(
                "UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?",
                (episode_id,),
            )
        else:
            conn.execute(
                "UPDATE episodes SET status='scripted', script_error=NULL, "
                "target_duration_s=? WHERE id=?",
                (synced, episode_id),
            )
        conn.commit()
        cp.phase = "SUCCEEDED"
        cp.outcome = "SUCCEEDED_READY_FOR_CONFIRM"
        save_checkpoint(cp, run_id=run_id)
        return cp


def _apply_repair(
    cp: SupervisorCheckpoint,
    plan: RepairPlan,
    conn,
    episode_id: str,
    completed: list[Shot],
    outline: StoryboardOutline | None,
    *,
    repair_screenplay: EpisodeScreenplay | None = None,
    narrative_repair_active: bool | None = None,
) -> SupervisorCheckpoint:
    """规划一个非破坏式候选窗口；此函数绝不删除正式 shots。"""
    from app.observability.metrics import inc
    from app.repair_router import normalize_strategy

    strategy = normalize_strategy(plan.strategy)
    if plan.pause_state:
        cp.phase = plan.pause_state  # type: ignore[assignment]
        cp.outcome = (
            "REPAIR_FAILED_STRATEGIES_EXHAUSTED"
            if plan.pause_state == "WAITING_HUMAN"
            else plan.reason
        )
        cp.last_repair = {
            **plan.model_dump(mode="json"),
            "strategy": strategy,
            "status": "paused",
        }
        cp.repair_candidate_shots = []
        save_checkpoint(cp)
        return cp

    history = list(cp.issue_strategy_history.get(plan.fingerprint, []))
    if cp.activation_attempt_count >= STORYBOARD_REPAIR_ACTIVATION_LIMIT:
        cp.phase = "WAITING_HUMAN"
        cp.outcome = "WAITING_RETRY_ACTIVATION_BUDGET"
        cp.last_repair = {
            **plan.model_dump(mode="json"),
            "strategy": strategy,
            "status": "paused",
            "reason": "activation_budget_exhausted",
        }
        cp.repair_candidate_shots = []
        save_checkpoint(cp)
        return cp
    if len(history) >= STORYBOARD_REPAIR_MAX_FINGERPRINT_ATTEMPTS:
        cp.phase = "WAITING_HUMAN"
        cp.outcome = "REPAIR_FAILED_STRATEGIES_EXHAUSTED"
        cp.last_repair = {
            **plan.model_dump(mode="json"),
            "strategy": strategy,
            "status": "paused",
            "reason": "fingerprint_strategy_budget_exhausted",
        }
        cp.repair_candidate_shots = []
        save_checkpoint(cp)
        return cp

    cp.phase = "REPAIRING"
    cp.activation_no = max(1, int(cp.activation_no or 0))
    cp.activation_attempt_count += 1
    cp.repair_epoch += 1
    cp.issue_fingerprint_counts = bump_fingerprint_count(
        cp.issue_fingerprint_counts, plan.fingerprint
    )
    frontier = max(1, int(plan.invalidation_frontier or 1))
    bridge: CognitiveBridgePlan | None = None
    selected_assessment: dict[str, Any] = {}
    if plan.semantic_diagnosis:
        diagnosis = plan.semantic_diagnosis
        assessments = list(diagnosis.get("candidate_assessments") or [])
        def _assessment_strategy(value: Any) -> str:
            return normalize_strategy(str(value or ""))

        selected_assessment = next((
            item for item in assessments
            if _assessment_strategy(item.get("strategy")) == strategy
        ), {})
        deletion_test_applicable = False
        for raw_operation in list(
            selected_assessment.get("outline_operations") or []
        ):
            try:
                from app.narrative_repair import SemanticOutlineOperation

                operation = SemanticOutlineOperation.model_validate(
                    raw_operation
                )
                if operation.executable_op() == "delete_outline_shot":
                    deletion_test_applicable = True
                    break
            except (TypeError, ValueError):
                # Invalid typed operations are rejected again at the executor
                # boundary. Until then, keep the bridge gate fail-closed.
                deletion_test_applicable = True
                break
        affected_ids = [
            shot.shot_id
            for shot in completed
            if shot.shot_no in set(plan.touched_shot_nos)
            and shot.shot_id
        ]
        assimilation_task_ids = list(diagnosis.get("assimilation_task_ids") or [])
        if assimilation_task_ids:
            bridge = CognitiveBridgePlan(
                bridge_plan_id=f"BP-{episode_id}-{cp.repair_epoch}",
                assimilation_task_ids=assimilation_task_ids,
                candidate_changes=assessments,
                expected_audience_delta={
                    "semantic_gap": diagnosis.get("semantic_gap") or "",
                    "affected_relation_ids": diagnosis.get("affected_relation_ids") or [],
                },
                affected_shot_ids=affected_ids,
                estimated_screen_time_delta=0.0,
                deletion_test_result={
                    "applicable": deletion_test_applicable,
                    "passed": (
                        bool(selected_assessment.get("passes_deletion_test"))
                        if deletion_test_applicable
                        else True
                    ),
                    "reason": (
                        "selected operation deletes an outline node"
                        if deletion_test_applicable
                        else "selected operation does not delete an outline node"
                    ),
                },
                marginal_gain_result={
                    "passed": bool(selected_assessment.get("passes_marginal_gain_test")),
                    "expected_gain": selected_assessment.get("expected_narrative_gain", 0.0),
                },
                selection_reason=str(diagnosis.get("selection_reason") or plan.reason),
            )
    effective_strategy = strategy
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    ep = conn.execute(
        "SELECT episode_no,target_duration_s,storyboard_outline_json "
        "FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    episode_no = int(ep["episode_no"] or 1) if ep else 1
    current_board = _board_from_rows(rows, episode_no)
    official_outline = None
    if ep and ep["storyboard_outline_json"]:
        try:
            official_outline = StoryboardOutline.model_validate_json(
                ep["storyboard_outline_json"]
            )
        except (TypeError, ValueError):
            official_outline = None
    candidate_outline = (
        (official_outline or outline).model_copy(deep=True)
        if (official_outline or outline) is not None else None
    )
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if episode_row is None:
        raise StageError("分镜修复", ["剧集不存在"])
    if repair_screenplay is None or narrative_repair_active is None:
        from app.production.screenplay_authority import resolve_downstream_screenplay

        try:
            repair_context = resolve_downstream_screenplay(episode_id, conn=conn)
        except ValueError as exc:
            raise StageError("分镜修复", [f"剧本权威链无效：{exc}"]) from exc
        repair_screenplay = repair_context.screenplay
        narrative_repair_active = repair_context.narrative_authority_required
    if candidate_outline is not None and bridge is not None:
        # Candidate isolation: a semantic bridge is only a proposed repair
        # until the merged storyboard passes the complete gate.  Keep it in
        # checkpoint-owned candidate_outline; _commit_repair_candidate writes
        # this outline together with the candidate shots in one transaction.
        candidate_outline.cognitive_bridge_plans = [
            *[
                item for item in candidate_outline.cognitive_bridge_plans
                if item.bridge_plan_id != bridge.bridge_plan_id
            ],
            bridge,
        ]
    semantic_outline_events: list[dict[str, Any]] = []
    semantic_changed_window: tuple[int, int, int] | None = None
    raw_outline_operations = list(
        selected_assessment.get("outline_operations") or []
    )
    if narrative_repair_active and (
        plan.needs_semantic_selection
        or (
            strategy not in {"normalize", "repair_current", "repair_window"}
            and not raw_outline_operations
        )
    ):
        # Narrative repairs may not drift into a legacy fixed strategy when the
        # AI intent is missing or needs a capability the current typed executor
        # cannot express.  Keep the official board untouched and require review.
        cp.phase = "WAITING_HUMAN"
        cp.outcome = "SEMANTIC_REPAIR_NOT_EXECUTABLE"
        cp.last_repair = {
            **plan.model_dump(mode="json"),
            "strategy": strategy,
            "status": "paused",
            "reason": "semantic repair lacks a verified executable operation",
            "candidate_outline_published": False,
        }
        cp.repair_candidate_shots = []
        save_checkpoint(cp)
        return cp
    if narrative_repair_active and raw_outline_operations:
        if candidate_outline is None:
            cp.phase = "WAITING_HUMAN"
            cp.outcome = "SEMANTIC_OUTLINE_BASE_MISSING"
            cp.last_repair = {
                **plan.model_dump(mode="json"),
                "strategy": strategy,
                "status": "paused",
                "reason": "semantic outline operations require a current outline",
            }
            cp.repair_candidate_shots = []
            save_checkpoint(cp)
            return cp
        before_semantic_outline = candidate_outline.model_copy(deep=True)
        try:
            candidate_outline, semantic_outline_events = (
                _apply_semantic_outline_operations(
                    candidate_outline,
                    raw_outline_operations,
                )
            )
            from app.narrative_repair import (
                reproject_semantic_outline_authority,
            )

            reproject_semantic_outline_authority(
                candidate_outline,
                repair_screenplay,
            )
            semantic_changed_window = _outline_changed_window(
                before_semantic_outline,
                candidate_outline,
            )
            from app.narrative import validate_storyboard_narrative

            semantic_outline_errors = validate_storyboard_narrative(
                board=None,
                screenplay=repair_screenplay,
                outline=candidate_outline,
                complete=True,
                expected_scope_id=episode_id,
            )
            if semantic_changed_window is None:
                semantic_outline_errors.append(
                    "[SEMANTIC_OUTLINE_NOOP] AI 大纲操作未产生结构或权威任务变化"
                )
            if semantic_outline_errors:
                raise ValueError("；".join(semantic_outline_errors[:8]))
        except Exception as exc:  # noqa: BLE001 - fail closed at candidate boundary
            cp.phase = "WAITING_HUMAN"
            cp.outcome = "SEMANTIC_OUTLINE_CANDIDATE_REJECTED"
            cp.last_repair = {
                **plan.model_dump(mode="json"),
                "strategy": strategy,
                "status": "paused",
                "reason": str(exc),
                "candidate_outline_published": False,
            }
            cp.repair_candidate_shots = []
            save_checkpoint(cp)
            return cp
        if bridge is not None:
            bridge.added_shot_ids = [
                str(event.get("after_shot_id") or "")
                for event in semantic_outline_events
                if event.get("op") == "insert_outline_shot"
                and event.get("after_shot_id")
            ]
            bridge.removed_shot_ids = [
                str(event.get("before_shot_id") or "")
                for event in semantic_outline_events
                if event.get("op") == "delete_outline_shot"
                and event.get("before_shot_id")
            ]
            bridge.estimated_screen_time_delta = float(
                sum(float(shot.duration_s or 0) for shot in candidate_outline.shots)
                - sum(float(shot.duration_s or 0) for shot in before_semantic_outline.shots)
            )
    # ``S* / who: does`` parsing is retained only for artifacts created before
    # the authority graph existed.  Narrative artifacts are targeted by the
    # AI-selected relation IDs carried in ``semantic_diagnosis`` and never by
    # issue-code or story-word matching.
    spine_targets = [] if narrative_repair_active else _missing_spine_targets(plan)
    active_spine_target: tuple[str, str, str] | None = None
    if spine_targets:
        target_by_id = {target[0]: target for target in spine_targets}
        bound = [
            (int(shot.shot_no), target_by_id[beat_id])
            for shot in current_board.shots
            for value in (shot.spine_beat_ids or [])
            if (beat_id := str(value).upper()) in target_by_id
        ]
        bound_shot_nos = [shot_no for shot_no, _target in bound]
        if bound_shot_nos:
            # Place the first local repair beside the earliest affected beat.
            # Subsequent full-gate passes will route any remaining beat IDs.
            frontier = min(bound_shot_nos)
            active_spine_target = next(
                target for shot_no, target in bound if shot_no == frontier
            )
        elif spine_targets:
            active_spine_target = spine_targets[0]
    max_no = max((int(shot.shot_no) for shot in current_board.shots), default=0)
    mode = "replace" if frontier <= max_no else "append"
    window_start = frontier
    window_end = frontier

    structure_old_end: int | None = None
    structure_new_end: int | None = None
    if semantic_changed_window is not None:
        window_start, structure_old_end, structure_new_end = semantic_changed_window
        window_end = structure_new_end
        frontier = window_start
        mode = "structure"
        inc(
            "storyboard_semantic_structure_candidate_total",
            episode_id=episode_id,
            strategy=strategy,
            operations=len(semantic_outline_events),
        )
    elif strategy in {"split_adjacent_shot", "split_shot"}:
        from app.validators import (
            split_outline_over_action_capacity,
            split_outline_over_key_line_capacity,
            storyboard_shot_count_range,
        )
        events: list[dict] = []
        if candidate_outline is not None and not narrative_repair_active:
            _, max_shots = storyboard_shot_count_range(
                episode_row["target_duration_s"] if episode_row else 50
            )
            # A semantic split candidate is tested against every applicable
            # structural capacity relation.  The issue label does not select a
            # transformer; each analyzer independently returns a change only
            # when its measured relation is actually over capacity.
            events.extend(split_outline_over_action_capacity(
                candidate_outline,
                max_shots=max_shots,
                shot_nos={frontier},
                force=True,
            ))
            if repair_screenplay is not None:
                events.extend(split_outline_over_key_line_capacity(
                    candidate_outline, repair_screenplay, max_shots=max_shots,
                ))
            if events:
                inc(
                    "storyboard_split_shot_total",
                    episode_id=episode_id,
                    shot_no=frontier,
                    shots_after=len(candidate_outline.shots),
                    strategy=strategy,
                )
        mode = "insert"
        window_start = min(max(1, frontier + 1), max_no + 1)
        window_end = window_start
        if not events:
            # 大纲无法再拆 → 插入明确节点，绝不整集重规划
            effective_strategy = "insert_shot"
            cp.last_repair = {
                **(cp.last_repair or {}),
                "strategy": "insert_shot",
                "reason": "split_noop_escalate_insert",
            }
            if candidate_outline is not None and frontier <= len(candidate_outline.shots):
                # 在 frontier 处复制相邻大纲节点作为插镜占位
                from copy import deepcopy
                src = candidate_outline.shots[
                    min(len(candidate_outline.shots), frontier) - 1
                ]
                extra = deepcopy(src)
                extra.shot_no = frontier
                _retarget_spine_repair_brief(extra, active_spine_target)
                # 重排后续编号由后续生成填充；这里扩展计划长度
                candidate_outline.shots.insert(frontier - 1, extra)
                for i, node in enumerate(candidate_outline.shots, start=1):
                    node.shot_no = i
            inc(
                "storyboard_insert_shot_total",
                episode_id=episode_id,
                shot_no=frontier,
                strategy="insert_shot",
            )
    elif strategy == "insert_shot":
        requested_frontier = frontier
        insert_at = min(max(1, requested_frontier + 1), max_no + 1)
        mode = "insert"
        window_start = insert_at
        window_end = insert_at
        if candidate_outline is not None:
            from copy import deepcopy
            if candidate_outline.shots:
                source_idx = min(
                    len(candidate_outline.shots), max(1, requested_frontier)
                ) - 1
                extra = deepcopy(candidate_outline.shots[source_idx])
                extra.shot_no = insert_at
                _retarget_spine_repair_brief(extra, active_spine_target)
                candidate_outline.shots.insert(insert_at - 1, extra)
                for i, node in enumerate(candidate_outline.shots, start=1):
                    node.shot_no = i
        inc("storyboard_insert_shot_total", episode_id=episode_id, shot_no=insert_at)
    elif strategy in {"repair_current", "normalize", "delete_shot"}:
        window_start = frontier
        window_end = frontier
    elif strategy in {"repair_window", "move_shot"}:
        touched = sorted({
            int(shot_no)
            for shot_no in plan.touched_shot_nos
            if 0 < int(shot_no) <= max_no
        })
        if len(touched) > 1:
            window_start = touched[0]
            window_end = touched[-1]
        else:
            window_start = frontier
            window_end = (
                min(max_no, frontier + 1)
                if max_no >= frontier
                else frontier
            )
    else:
        cp.phase = "WAITING_HUMAN"
        cp.outcome = "SEMANTIC_REPAIR_NOT_EXECUTABLE"
        cp.last_repair = {
            **plan.model_dump(mode="json"),
            "strategy": strategy,
            "status": "paused",
            "reason": "semantic strategy requires an unavailable executor",
            "candidate_outline_published": False,
        }
        cp.repair_candidate_shots = []
        save_checkpoint(cp)
        return cp

    if mode == "append":
        # A legacy destructive repair may have removed rows before the reported
        # target. Regenerate the missing contiguous prefix as part of the same
        # durable candidate, while keeping the target as the window end.
        window_start = min(window_start, max_no + 1)
        window_end = max(window_start, window_end)

    attempt_no = len(history) + 1
    semantic_source = (
        f"{episode_id}:{cp.activation_no}:{plan.fingerprint}:"
        f"{effective_strategy}:{window_start}:{window_end}:{attempt_no}"
    )
    semantic_attempt_id = "sbatt_" + hashlib.sha256(
        semantic_source.encode("utf-8")
    ).hexdigest()[:24]
    history.append(f"{effective_strategy}:{semantic_attempt_id}")
    cp.issue_strategy_history = {
        **cp.issue_strategy_history,
        plan.fingerprint: history,
    }
    cp.last_repair = {
        **plan.model_dump(mode="json"),
        "strategy": effective_strategy,
        "status": "candidate_pending",
        "mode": mode,
        "window_start": window_start,
        "window_end": window_end,
        "base_hash": _storyboard_hash(current_board),
        "semantic_attempt_id": semantic_attempt_id,
        "attempt_no": attempt_no,
        "spine_target": (
            {
                "beat_id": active_spine_target[0],
                "who": active_spine_target[1],
                "does": active_spine_target[2],
            }
            if active_spine_target is not None else None
        ),
        "candidate_outline": (
            candidate_outline.model_dump(mode="json")
            if candidate_outline is not None else None
        ),
        "candidate_expected_total": (
            len(candidate_outline.shots) if candidate_outline is not None else 0
        ),
        "semantic_outline_events": semantic_outline_events,
        "structure_old_end": structure_old_end,
        "structure_new_end": structure_new_end,
    }
    cp.repair_candidate_shots = []
    if mode == "replace" and window_start == window_end:
        target = next(
            (shot for shot in current_board.shots if int(shot.shot_no) == window_start),
            None,
        )
        deterministic = None
        deterministic_kind = ""
        if (
            target is not None
            and not narrative_repair_active
        ):
            deterministic = _deterministic_dialogue_framing_candidate(target)
            deterministic_kind = "dialogue_action_framing"
        if deterministic is None and target is not None:
            deterministic = _deterministic_ambient_audio_cast_candidate(target)
            deterministic_kind = "ambient_sound_identity_cleanup"
        if deterministic is not None:
            if (
                deterministic_kind == "ambient_sound_identity_cleanup"
                and candidate_outline is not None
            ):
                brief = next((
                    item for item in candidate_outline.shots
                    if int(item.shot_no) == window_start
                ), None)
                if brief is not None:
                    brief.audio_cast = list(deterministic.audio_cast or [])
                    cp.last_repair = {
                        **cp.last_repair,
                        "candidate_outline": candidate_outline.model_dump(mode="json"),
                    }
            cp.repair_candidate_shots = [_shot_checkpoint_payload(deterministic)]
            cp.last_repair = {
                **cp.last_repair,
                "status": "candidate_generating",
                "candidate_count": 1,
                "deterministic_repair": deterministic_kind,
            }
    cp.validated_prefix_end = max(0, window_start - 1)
    cp.next_shot_no = window_start
    cp.validated_shot_artifact_ids = cp.validated_shot_artifact_ids[: max(0, window_start - 1)]
    conn.commit()
    save_checkpoint(cp)
    try:
        from app.observability.tracing import current_trace
        rid = current_trace().run_id
        if rid:
            evidence_repository.append_event(
                rid, "STORYBOARD_REPAIR_CANDIDATE_PLANNED", "info",
                f"已创建非破坏修复窗口 {window_start}~{window_end}",
                payload=cp.last_repair or {},
            )
    except Exception:  # noqa: BLE001
        pass
    return cp


def _delete_shot_window(conn, episode_id: str, start_no: int, end_no: int) -> int:
    """只删除 [start_no, end_no] 闭区间内的镜头，保留前后无关镜头。"""
    start_no = max(1, int(start_no))
    end_no = max(start_no, int(end_no))
    rows = conn.execute(
        "SELECT id, shot_no FROM shots WHERE episode_id=? AND shot_no>=? AND shot_no<=? ORDER BY shot_no",
        (episode_id, start_no, end_no),
    ).fetchall()
    if not rows:
        return 0
    from app import worker
    try:
        from app.observability.tracing import current_trace
        active_storyboard_run_id = current_trace().run_id
    except Exception:  # noqa: BLE001
        active_storyboard_run_id = None
    for row in rows:
        worker.clear_shot_artifacts(
            row["id"],
            active_storyboard_run_id=active_storyboard_run_id,
            commit=False,
        )
        conn.execute("DELETE FROM shots WHERE id=?", (row["id"],))
    return len(rows)


def _open_shot_gap(conn, episode_id: str, insert_at: int) -> int:
    """Shift a suffix right by one, preserving IDs/assets and leaving one gap."""
    insert_at = max(1, int(insert_at))
    rows = conn.execute(
        "SELECT id, shot_no FROM shots WHERE episode_id=? AND shot_no>=? "
        "ORDER BY shot_no DESC",
        (episode_id, insert_at),
    ).fetchall()
    # Individual descending updates avoid transient UNIQUE(episode_id, shot_no)
    # collisions while retaining every suffix row in place.
    for row in rows:
        conn.execute(
            "UPDATE shots SET shot_no=? WHERE id=?",
            (int(row["shot_no"]) + 1, row["id"]),
        )
    return len(rows)
