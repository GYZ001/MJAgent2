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

from pydantic import BaseModel, Field

from app import errors
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.repair_router import (
    RepairPlan,
    bump_fingerprint_count,
    route_issues,
)
from app.renderability import SHOT_SOFT_MAX
from app.schemas import Shot, Storyboard, StoryboardOutline
from app.stages import StageError, generate_storyboard_next_shot, generate_storyboard_outline

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
    legacy_repair_audit: dict[str, Any] = Field(default_factory=dict)
    outcome: str | None = None  # SUCCEEDED_READY_FOR_CONFIRM


def _migrate_checkpoint(cp: SupervisorCheckpoint) -> SupervisorCheckpoint:
    """Upgrade legacy runaway checkpoints without erasing their audit counters."""
    if cp.planner_version == STORYBOARD_REPAIR_PLANNER_VERSION:
        return cp
    cp.legacy_repair_audit = {
        **cp.legacy_repair_audit,
        "repair_epoch": int(cp.repair_epoch or 0),
        "issue_fingerprint_counts": dict(cp.issue_fingerprint_counts or {}),
        "migrated_from": cp.planner_version or "legacy",
    }
    # ``repair_epoch`` remains the lifetime audit count.  The corrupt legacy
    # per-fingerprint counters must not consume the first bounded v2 activation.
    cp.issue_fingerprint_counts = {}
    cp.activation_attempt_count = 0
    cp.repair_candidate_shots = []
    cp.planner_version = STORYBOARD_REPAIR_PLANNER_VERSION
    if cp.outcome == "PAUSED_REPAIR_SAFETY_LIMIT":
        cp.outcome = "WAITING_RETRY_LEGACY_MIGRATED"
    return cp


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
            "PAUSED_EXTERNAL",
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
            f"Supervisor checkpoint phase={cp.phase} prefix={cp.validated_prefix_end}",
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
        and _is_structural_storyboard_issue(i.get("code"), i.get("message", ""))
    ]
    if structural:
        return structural
    if disposition == "NEEDS_REPLAN":
        # Phase 3 QA score-only: quality/capacity replanning requests are
        # warnings for the report, not a reason to delete/split/replan shots.
        return [m for m in residual if _is_structural_storyboard_issue(None, m)]
    blockers = [
        i.get("message", "") for i in issues
        if isinstance(i, dict) and i.get("severity") == "blocker"
        and _is_structural_storyboard_issue(i.get("code"), i.get("message", ""))
    ]
    if blockers:
        return blockers
    # warning-only：允许继续（非 blocker）
    if disposition == "WARNING" and residual:
        return [m for m in residual if _is_structural_storyboard_issue(None, m)]
    if disposition not in {"PASS", "WARNING", None}:
        return [m for m in residual if _is_structural_storyboard_issue(None, m)]
    return []


def _is_structural_storyboard_issue(code: Any = None, message: Any = "") -> bool:
    text = f"{code or ''} {message or ''}".lower()
    structural_tokens = (
        "schema",
        "json",
        "field",
        "字段",
        "必填",
        "missing required",
        "source_binding",
        "原文证据",
        "版本已变化",
        "upstream_version_changed",
        "artifact",
        "id_invalid",
        "invalid_id",
        "dialogue_framing_invalid",
        "多个画内说话人",
        "单人对白",
        "只保留说话人",
        "正反打",
    )
    return any(token in text for token in structural_tokens)


def _is_quality_only_repair_plan(plan: RepairPlan) -> bool:
    quality_codes = {
        "SPOKEN_CAPACITY_EXCEEDED",
        "SPOKEN_CONTRACT_CONFLICT",
        "SHOT_OUTLINE_COVERAGE",
        "STATE_CHAIN_INVALID",
        "KEY_LINE_MISSING",
        "SPINE_MISSING",
        "KEY_CONTENT_MISSING",
        "DROP_LIST_REINTRODUCED",
        "PLAN_EXHAUSTED_NOT_FINAL",
    }
    codes = set(plan.issue_codes or [])
    return bool(codes) and codes.issubset(quality_codes)


def _storyboard_warning_requires_auto_repair(issue: Any) -> bool:
    """Warnings that are deterministic delivery defects, not taste scores."""
    code = str(getattr(issue, "code", "") or "")
    message = str(getattr(issue, "message", "") or "")
    return bool(
        code == "OVERDETAIL"
        or "连续 3 个镜头景别" in message
        or "连续三个镜头景别" in message
    )


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
    return count >= max_shots


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


def _repair_is_pending(cp: SupervisorCheckpoint) -> bool:
    repair = cp.last_repair or {}
    return repair.get("status") in {"candidate_pending", "candidate_generating"}


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
            candidates.append(Shot.model_validate(raw))
        except (TypeError, ValueError):
            continue
    return [*prefix, *candidates]


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
    candidates = [Shot.model_validate(raw) for raw in cp.repair_candidate_shots]
    if mode == "insert":
        merged = [*current_shots[: start - 1], *candidates, *current_shots[start - 1 :]]
        for shot_no, shot in enumerate(merged, start=1):
            shot.shot_no = shot_no
        return Storyboard(episode_no=current.episode_no, shots=merged)
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
    if str(repair.get("mode") or "replace") == "insert":
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
        shot.model_dump(mode="json") for shot in selected
    ]
    return _merge_repair_candidate(current, projected_cp)


def _write_shot_fields(conn, row_id: str, shot: Shot, artifact_id: str | None) -> None:
    from app.continuity import shot_contract_dict
    from app.validators import normalize_action_desc

    shot.action_desc = normalize_action_desc(shot.action_desc)
    conn.execute(
        """UPDATE shots SET duration_s=?,shot_size=?,camera_move=?,scene_setting=?,scene_name=?,
                  characters=?,action_desc=?,first_frame_desc=?,last_frame_desc=?,source_excerpt=?,
                  narration=?,dialogues=?,transition=?,continuity_from_prev=?,shot_contract_json=?,
                  continuity_mode=?,observed_state_out=?,storyboard_artifact_id=? WHERE id=?""",
        (
            shot.duration_s, shot.shot_size, shot.camera_move, shot.scene_setting,
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
    expected_board_hash = str(repair.get("base_hash") or "")
    current_hash = _storyboard_hash(current_board)
    if expected_board_hash and expected_board_hash != current_hash:
        raise RuntimeError("storyboard working projection CAS conflict")

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

        mode = str(repair.get("mode") or "replace")
        start = max(1, int(repair.get("window_start") or 1))
        raw_candidate_count = len(cp.repair_candidate_shots)
        if mode == "insert":
            candidate_shots = [
                shot for shot in candidate_board.shots
                if start <= int(shot.shot_no) < start + raw_candidate_count
            ]
        else:
            end = max(start, int(repair.get("window_end") or start))
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
                    _write_shot_fields(conn, row["id"], shot, artifact_id)
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


async def run_storyboard_supervisor(
    episode_id: str,
    *,
    resume: bool = True,
    run_id: str | None = None,
    preflight_done: bool = False,
    new_activation: bool = False,
) -> SupervisorCheckpoint:
    """集级 Supervisor 主循环。调用前应已完成人物/场景预检（或设 preflight_done=False 由本函数跳过）。"""
    from app.domain.common import (
        _compact_episode_target,
        _episode_source_text,
        _load_screenplay,
        _project_bible_or_placeholder,
        _storyboard_target_for_source,
    )
    from app.validators import (
        normalize_continuity,
        normalize_offbible_characters,
        normalize_transition_visuals,
        prefer_default_shot_durations,
        relieve_spoken_overflow,
        storyboard_shot_count_range,
        validate_storyboard_preserves_key_content,
    )
    from app.domain.storyboard_ops import (
        _board_from_shot_rows,
        _finalize_storyboard_evidence,
        _insert_storyboard_shot,
        _shot_contract_json,
        _assert_storyboard_write_authorized,
        _persist_storyboard_character_policy_repairs,
        _reconcile_storyboard_plan,
        _sync_storyboard_shot_timing,
    )
    from app.domain.video_ops import evaluate_storyboard_for_confirmation

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("分镜脚本", ["剧集不存在"])

    screenplay = _load_screenplay(ep)
    if screenplay is None or ep["screenplay_status"] != "ready":
        raise StageError("分镜脚本", ["请先生成并确认本集可拍剧本，再展开分镜"])

    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = _project_bible_or_placeholder(p)
    ep_data = dict(ep)
    source_text = _episode_source_text(conn, ep)

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
    if new_activation:
        cp.activation_no = int(cp.activation_no or 0) + 1
        cp.activation_attempt_count = 0
        cp.issue_fingerprint_counts = {}
        cp.repair_candidate_shots = []
        if cp.last_repair:
            cp.last_repair = {**cp.last_repair, "status": "superseded_by_new_activation"}
        cp.outcome = None
    if run_id:
        evidence_repository.append_event(
            run_id, "STORYBOARD_SUPERVISOR_STARTED", "info",
            f"Supervisor 启动 resume={resume}",
            payload={
                "episode_id": episode_id,
                "phase": cp.phase,
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
    if compact_target != ep_data.get("target_duration_s"):
        conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
        conn.commit()
        ep_data["target_duration_s"] = compact_target

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
        cp.validated_prefix_end = len(shots)
        cp.next_shot_no = len(shots) + 1
        cp.validated_shot_artifact_ids = [
            row["storyboard_artifact_id"]
            for row in prefix_rows
            if row["storyboard_artifact_id"]
        ]
        return shots

    def _publish_best_effort_storyboard(
        candidate_shots: list[Shot], *, reason: str,
    ) -> SupervisorCheckpoint | None:
        """Publish the current non-empty board after bounded gate repair is exhausted."""
        if not candidate_shots:
            return None
        board = Storyboard(episode_no=ep_data["episode_no"], shots=list(candidate_shots))
        normalize_continuity(board)
        relieve_spoken_overflow(board)
        prefer_default_shot_durations(board)
        normalize_transition_visuals(board)
        board.shots[-1].is_final = True
        rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
        ).fetchall()
        for row, shot in zip(rows, board.shots):
            conn.execute(
                "UPDATE shots SET continuity_from_prev=?,transition=?,duration_s=?,shot_size=?,"
                "camera_move=?,last_frame_desc=?,shot_contract_json=?,continuity_mode=?,"
                "observed_state_out=? WHERE id=?",
                (
                    int(shot.continuity_from_prev), shot.transition, shot.duration_s,
                    shot.shot_size, shot.camera_move, shot.last_frame_desc,
                    _shot_contract_json(shot), shot.continuity_mode, shot.observed_state_out,
                    row["id"],
                ),
            )
        conn.commit()
        _finalize_storyboard_evidence(episode_id, board)
        actual_total = sum(int(shot.duration_s or 0) for shot in board.shots)
        conn.execute(
            "UPDATE episodes SET status='scripted',script_error=NULL,storyboard_warning=?,"
            "target_duration_s=? WHERE id=?",
            (
                ("门禁修复次数耗尽，已发布当前最佳分镜：" + reason)[:800],
                _compact_episode_target(actual_total or ep_data["target_duration_s"]),
                episode_id,
            ),
        )
        conn.commit()
        cp.phase = "SUCCEEDED"
        cp.outcome = "SUCCEEDED_GATE_RETRY_EXHAUSTED_FALLBACK"
        cp.validated_prefix_end = len(board.shots)
        cp.next_shot_no = len(board.shots) + 1
        cp.last_repair = {
            **(cp.last_repair or {}),
            "status": "fallback_published",
            "reason": reason,
        }
        save_checkpoint(cp, run_id=run_id)
        if run_id:
            evidence_repository.append_event(
                run_id,
                "STORYBOARD_GATE_RETRY_EXHAUSTED_FALLBACK",
                "warning",
                "分镜门禁重试耗尽，已发布当前最佳产物",
                payload={"shot_count": len(board.shots), "reason": reason},
            )
        return cp

    existing_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    completed: list[Shot] = _reload_completed(existing_rows)
    if _repair_is_pending(cp):
        completed = _repair_context_shots(conn, cp, ep_data["episode_no"])
    if completed:
        recovered_board = Storyboard(episode_no=ep_data["episode_no"], shots=list(completed))
        character_changes = normalize_offbible_characters(recovered_board, bible)
        _persist_storyboard_character_policy_repairs(
            conn, episode_id, recovered_board, character_changes
        )
        completed = list(recovered_board.shots)

    _, max_shots = storyboard_shot_count_range(ep_data["target_duration_s"])
    planned_persisted = len(outline.shots) if (outline and outline.shots) else 0
    final_feedback: list[str] | None = None
    needs_outline = outline is None

    # Every activation is independently bounded. ``repair_epoch`` is lifetime
    # audit only and must never make a newly-authorized activation a no-op.
    while True:
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
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=?, storyboard_warning=NULL WHERE id=?",
                (outline.model_dump_json(), episode_id),
            )
            conn.commit()
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

        # ---- 逐镜 ----
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
            if repair_pending and len(completed) >= int(active_repair.get("window_end") or 0):
                break
            planned_now = len(outline.shots) if (outline and outline.shots) else 0
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
                draft = await generate_storyboard_next_shot(
                    ep_data, source_text, bible,
                    prev_ending=prev["cliffhanger"] if prev else "",
                    screenplay=screenplay,
                    completed_shots=completed,
                    final_feedback=final_feedback,
                    outline=outline,
                    repair_feedback=_repair_feedback_for_shot(
                        list(active_repair.get("issue_messages") or []), shot_no,
                    ) if repair_pending else None,
                    semantic_attempt_id=active_repair.get("semantic_attempt_id") if repair_pending else None,
                )
            except StageError as exc:
                plan = route_issues(
                    list(exc.errors) if hasattr(exc, "errors") else [str(exc)],
                    validated_prefix_end=cp.validated_prefix_end,
                    next_shot_no=shot_no,
                    issue_fingerprint_counts=cp.issue_fingerprint_counts,
                )
                cp = _apply_repair(cp, plan, conn, episode_id, completed, outline)
                if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL"}:
                    if cp.outcome in {
                        "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
                        "WAITING_RETRY_ACTIVATION_BUDGET",
                    }:
                        fallback = _publish_best_effort_storyboard(
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
                msg = str(exc)
                if _is_retryable_external_error(exc) or any(
                    k in msg.lower() for k in ("timeout", "unavailable", "429", "503", "连接")
                ):
                    return _pause_for_external_error(
                        cp,
                        conn,
                        episode_id,
                        exc,
                        run_id=run_id,
                        action="storyboard_shot_provider",
                    )
                raise

            disposition = getattr(draft, "disposition", "PASS")
            blockers = _blocker_messages(draft)

            # NEEDS_REPLAN 或 blocker：不落主 shots
            if disposition == "NEEDS_REPLAN" or blockers:
                # 仍可把 candidate artifact 保留在 draft.evidence_artifact_id
                plan = route_issues(
                    blockers or list(getattr(draft, "residual_errors", []) or []),
                    validated_prefix_end=cp.validated_prefix_end,
                    next_shot_no=shot_no,
                    issue_fingerprint_counts=cp.issue_fingerprint_counts,
                )
                if run_id:
                    evidence_repository.append_event(
                        run_id, "REPAIR_PLAN_SELECTED", "info",
                        f"{plan.strategy} frontier={plan.invalidation_frontier}",
                        payload=plan.model_dump(mode="json"),
                )
                cp = _apply_repair(cp, plan, conn, episode_id, completed, outline)
                if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL"}:
                    if cp.outcome in {
                        "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
                        "WAITING_RETRY_ACTIVATION_BUDGET",
                    }:
                        cp.phase = "GENERATING_SHOTS"
                        cp.outcome = None
                        cp.last_repair = {
                            **(cp.last_repair or {}),
                            "status": "gate_retry_exhausted_accept_candidate",
                            "reason": (cp.last_repair or {}).get("reason") or plan.reason,
                        }
                        save_checkpoint(cp, run_id=run_id)
                    else:
                        save_checkpoint(cp, run_id=run_id)
                        conn.execute(
                            "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                            (((cp.last_repair or {}).get("reason") or plan.reason)[:800], episode_id),
                        )
                        conn.commit()
                        return cp
                else:
                    completed = _repair_context_shots(conn, cp, ep_data["episode_no"])
                    shot_loop_broke_for_repair = True
                    break

            # PASS / warning-only → 落库 validated
            board = Storyboard(episode_no=ep_data["episode_no"], shots=[*completed, draft.shot])
            normalize_continuity(board)
            for c in normalize_offbible_characters(board, bible):
                pass  # 已归一
            relieve_spoken_overflow(board)
            prefer_default_shot_durations(board)
            normalize_transition_visuals(board)
            expected_screenplay_artifact_id = cp.input_versions.get("screenplay_artifact_id")
            shot = board.shots[-1]
            shot.is_final = bool(draft.is_final)
            shot.prompt_contract_version = "renderability_v1"
            object.__setattr__(shot, "evidence_artifact_id", getattr(draft, "evidence_artifact_id", None))
            if repair_pending:
                cp.repair_candidate_shots.append(shot.model_dump(mode="json"))
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
            revision = _reconcile_storyboard_plan(
                conn, episode_id, ep_data["episode_no"], outline, completed, planned_persisted
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
            if len(completed) >= SHOT_SOFT_MAX:
                final_feedback = None
            else:
                final_feedback = validate_storyboard_preserves_key_content(
                    Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)),
                    screenplay,
                ) or None

        if shot_loop_broke_for_repair:
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
            p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
            bible = _project_bible_or_placeholder(p)
            has_real_bible = bool((p["bible_json"] or "").strip()) if p else False
            candidate_evaluation = evaluate_storyboard_for_confirmation(
                ep_data, candidate_board, screenplay, bible,
                has_real_bible=has_real_bible,
            )
            candidate_board = _validated_candidate_projection(
                official_board, candidate_evaluation.board, cp,
            )
            before_codes = set(repair.get("issue_codes") or [])
            after_messages = [*candidate_evaluation.errors, *candidate_evaluation.warnings]
            from app.evaluations.issues import issue_code as _issue_code
            after_codes = {
                *[getattr(issue, "code", "") for issue in (candidate_evaluation.issues or [])],
                *[_issue_code(message) for message in candidate_evaluation.errors],
            }
            after_codes.discard("")
            before_messages = list(repair.get("issue_messages") or [])
            remaining_target_codes = before_codes.intersection(after_codes)
            resolved_target_codes = before_codes - after_codes
            improved = bool(
                mode == "append"
                or candidate_evaluation.passed and (
                    not remaining_target_codes
                    or bool(resolved_target_codes)
                    or len(after_messages) < max(1, len(before_messages))
                )
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
                retry_plan = route_issues(
                    before_messages or after_messages or ["storyboard repair made no progress"],
                    validated_prefix_end=len(official_board.shots),
                    issue_fingerprint_counts=cp.issue_fingerprint_counts,
                )
                cp = _apply_repair(
                    cp, retry_plan, conn, episode_id, list(official_board.shots), outline,
                )
                if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL"}:
                    if cp.outcome in {
                        "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
                        "WAITING_RETRY_ACTIVATION_BUDGET",
                    }:
                        fallback = _publish_best_effort_storyboard(
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
            fallback = _publish_best_effort_storyboard(
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
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        has_real_bible = bool((p["bible_json"] or "").strip()) if p else False
        evaluation = evaluate_storyboard_for_confirmation(
            ep_data, full_board, screenplay, bible, has_real_bible=has_real_bible,
        )
        repair_required_warnings = [
            issue for issue in (evaluation.issues or [])
            if _storyboard_warning_requires_auto_repair(issue)
        ]
        if not evaluation.passed or repair_required_warnings:
            repair_inputs = evaluation.errors or repair_required_warnings
            if run_id:
                evidence_repository.append_event(
                    run_id, "EPISODE_VALIDATION_FAILED", "warning",
                    f"{len(repair_inputs)} issues",
                    payload={
                        "errors": evaluation.errors[:12],
                        "repair_required_warnings": [
                            getattr(issue, "message", str(issue))
                            for issue in repair_required_warnings[:12]
                        ],
                    },
                )
            plan = route_issues(
                repair_inputs,
                validated_prefix_end=cp.validated_prefix_end,
                issue_fingerprint_counts=cp.issue_fingerprint_counts,
            )
            cp = _apply_repair(cp, plan, conn, episode_id, completed, outline)
            if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL"}:
                if cp.outcome in {
                    "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
                    "WAITING_RETRY_ACTIVATION_BUDGET",
                }:
                    fallback = _publish_best_effort_storyboard(
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
        synced = _compact_episode_target(actual_total or ep_data["target_duration_s"])
        _assert_storyboard_write_authorized(
            conn, episode_id, cp.input_versions.get("screenplay_artifact_id")
        )
        _finalize_storyboard_evidence(episode_id, evaluation.board)

        conn.execute(
            "UPDATE episodes SET status='scripted', script_error=NULL, target_duration_s=? WHERE id=?",
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
    effective_strategy = strategy
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    ep = conn.execute("SELECT episode_no,target_duration_s FROM episodes WHERE id=?", (episode_id,)).fetchone()
    episode_no = int(ep["episode_no"] or 1) if ep else 1
    current_board = _board_from_rows(rows, episode_no)
    max_no = max((int(shot.shot_no) for shot in current_board.shots), default=0)
    mode = "replace" if frontier <= max_no else "append"
    window_start = frontier
    window_end = frontier

    if strategy in {"split_adjacent_shot", "split_shot"}:
        from app.validators import (
            split_outline_over_action_capacity,
            split_outline_over_key_line_capacity,
            storyboard_shot_count_range,
        )
        from app.domain.common import _load_screenplay

        episode_row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        screenplay = _load_screenplay(episode_row) if episode_row else None
        events: list[dict] = []
        if outline is not None:
            _, max_shots = storyboard_shot_count_range(
                episode_row["target_duration_s"] if episode_row else 50
            )
            if "ACTION_CAPACITY_EXCEEDED" in plan.issue_codes:
                events.extend(split_outline_over_action_capacity(
                    outline,
                    max_shots=max_shots,
                    shot_nos={frontier},
                    force=True,
                ))
            if screenplay is not None and "SPOKEN_CAPACITY_EXCEEDED" in plan.issue_codes:
                events.extend(split_outline_over_key_line_capacity(
                    outline, screenplay, max_shots=max_shots,
                ))
            if events:
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                    (outline.model_dump_json(), episode_id),
                )
                cp.expected_total = len(outline.shots)
                inc(
                    "storyboard_split_shot_total",
                    episode_id=episode_id,
                    shot_no=frontier,
                    shots_after=len(outline.shots),
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
            if outline is not None and frontier <= len(outline.shots):
                # 在 frontier 处复制相邻大纲节点作为插镜占位
                from copy import deepcopy
                src = outline.shots[min(len(outline.shots), frontier) - 1]
                extra = deepcopy(src)
                extra.shot_no = frontier
                # 重排后续编号由后续生成填充；这里扩展计划长度
                outline.shots.insert(frontier - 1, extra)
                for i, node in enumerate(outline.shots, start=1):
                    node.shot_no = i
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                    (outline.model_dump_json(), episode_id),
                )
                cp.expected_total = len(outline.shots)
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
        if outline is not None:
            from copy import deepcopy
            if outline.shots:
                source_idx = min(len(outline.shots), max(1, requested_frontier)) - 1
                extra = deepcopy(outline.shots[source_idx])
                extra.shot_no = insert_at
                outline.shots.insert(insert_at - 1, extra)
                for i, node in enumerate(outline.shots, start=1):
                    node.shot_no = i
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                    (outline.model_dump_json(), episode_id),
                )
                cp.expected_total = len(outline.shots)
        inc("storyboard_insert_shot_total", episode_id=episode_id, shot_no=insert_at)
    elif strategy in {"repair_current", "normalize", "delete_shot"}:
        window_start = frontier
        window_end = frontier
    elif strategy in {"repair_window", "move_shot"}:
        window_start = frontier
        window_end = min(max_no, frontier + 1) if max_no >= frontier else frontier
    else:
        effective_strategy = "repair_current"
        window_start = frontier
        window_end = frontier

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
    }
    cp.repair_candidate_shots = []
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
