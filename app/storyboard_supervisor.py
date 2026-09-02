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

from app.db import (
    get_conn,
)
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact, Issue
from app.repair_router import (
    RepairPlan,
    bump_fingerprint_count,
    route_issues,
)
from app.schemas import (
    CognitiveBridgePlan,
    EpisodeScreenplay,
    Shot,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
)
from app.storyboard_authority import (
    StoryboardOutlineAuthority,
    StoryboardOutlineAuthorityError,
    persist_storyboard_outline_projection,
    resolve_storyboard_outline_authority,
)
from app.stages import (
    StageError,
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
    "WAITING_AUTHORIZATION",
    "WAITING_HUMAN",
    "CANCELLED",
]

CHECKPOINT_TYPE = "storyboard_supervisor_checkpoint"
OUTLINE_ARTIFACT_INPUT_VERSION = "storyboard_outline_artifact_id"
OUTLINE_REVISION_INPUT_VERSION = "storyboard_outline_revision"
OUTLINE_FINGERPRINT_INPUT_VERSION = "storyboard_outline_fingerprint"
OUTLINE_PROMPT_INPUT_VERSION = "storyboard_outline_prompt_version"
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
    from app.domain.storyboard_ops.mutation_primitives import _board_from_shot_rows

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
        from app.visual_styles import _project_bible_or_placeholder
        from app.domain.video_ops.confirmation_eval import (
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




def _revision_checkpoint_row(conn, episode_id: str):
    row = conn.execute(
        """SELECT id,status,checkpoint_json
             FROM production_revisions
            WHERE episode_id=? AND kind='storyboard' AND status='active'
            ORDER BY updated_at DESC,id DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    if row is not None:
        return row
    pointer = conn.execute(
        "SELECT storyboard_production_revision_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    revision_id = pointer["storyboard_production_revision_id"] if pointer else None
    if not revision_id:
        return None
    return conn.execute(
        """SELECT id,status,checkpoint_json
             FROM production_revisions
            WHERE id=? AND kind='storyboard'
              AND status IN ('active','published')""",
        (revision_id,),
    ).fetchone()


def _checkpoint_from_payload(raw: Any) -> SupervisorCheckpoint | None:
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return _migrate_checkpoint(SupervisorCheckpoint.model_validate(raw))
    except (TypeError, ValueError):
        return None


def _bind_checkpoint_to_outline_authority(
    cp: SupervisorCheckpoint,
    authority: StoryboardOutlineAuthority,
) -> SupervisorCheckpoint:
    old_artifact_id = str(
        cp.outline_artifact_id
        or cp.input_versions.get(OUTLINE_ARTIFACT_INPUT_VERSION)
        or ""
    )
    authority_changed = bool(
        old_artifact_id
        and old_artifact_id != authority.artifact_id
    )
    if authority_changed:
        cp.legacy_repair_audit = {
            **cp.legacy_repair_audit,
            "discarded_stale_outline_authority": {
                "artifact_id": old_artifact_id,
                "replacement_artifact_id": authority.artifact_id,
                "last_repair": cp.last_repair,
            },
        }
        cp.last_repair = None
        cp.repair_candidate_shots = []
        cp.scene_pack_candidates = {}
        if cp.phase == "REPAIRING":
            cp.phase = "VALIDATING_OUTLINE"
        cp.outcome = None
    cp.outline_artifact_id = authority.artifact_id
    cp.expected_total = len(authority.outline.shots)
    cp.input_versions = {
        **cp.input_versions,
        OUTLINE_ARTIFACT_INPUT_VERSION: authority.artifact_id,
        OUTLINE_REVISION_INPUT_VERSION: str(authority.revision),
        OUTLINE_FINGERPRINT_INPUT_VERSION: authority.fingerprint,
        OUTLINE_PROMPT_INPUT_VERSION: authority.prompt_version,
    }
    return cp


def _bind_checkpoint_to_current_outline(
    conn,
    cp: SupervisorCheckpoint,
) -> SupervisorCheckpoint:
    row = conn.execute(
        """SELECT storyboard_outline_json,storyboard_outline_revision,
                  storyboard_outline_fingerprint,storyboard_outline_artifact_id,
                  target_duration_authority
             FROM episodes WHERE id=?""",
        (cp.episode_id,),
    ).fetchone()
    if (
        row is None
        or not row["storyboard_outline_json"]
        or int(row["storyboard_outline_revision"] or 0) <= 0
        or not row["storyboard_outline_fingerprint"]
        or not row["storyboard_outline_artifact_id"]
    ):
        return cp
    try:
        authority = resolve_storyboard_outline_authority(
            cp.episode_id,
            conn=conn,
        )
    except StoryboardOutlineAuthorityError:
        return cp
    return _bind_checkpoint_to_outline_authority(cp, authority)


def _outline_authority_cas(
    cp: SupervisorCheckpoint,
) -> dict[str, Any]:
    artifact_id = (
        cp.outline_artifact_id
        or cp.input_versions.get(OUTLINE_ARTIFACT_INPUT_VERSION)
    )
    raw_revision = cp.input_versions.get(OUTLINE_REVISION_INPUT_VERSION)
    revision = None
    if raw_revision not in {None, ""}:
        try:
            revision = int(raw_revision)
        except (TypeError, ValueError):
            revision = None
    fingerprint = cp.input_versions.get(
        OUTLINE_FINGERPRINT_INPUT_VERSION
    )
    return {
        "expected_outline_artifact_id": artifact_id,
        "expected_outline_revision": revision,
        "expected_outline_fingerprint": fingerprint,
    }




def _checkpoint_payload_for_revision(
    row,
    cp: SupervisorCheckpoint,
) -> dict[str, Any]:
    try:
        payload = json.loads(row["checkpoint_json"] or "{}") if row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["supervisor_checkpoint"] = cp.model_dump(mode="json")
    payload["activation_no"] = cp.activation_no
    payload["activation_attempt_count"] = cp.activation_attempt_count
    payload["lifetime_repair_count"] = cp.repair_epoch
    payload["phase"] = cp.phase
    payload["yield_reason"] = cp.outcome
    return payload


def _persist_checkpoint_payload(
    conn,
    cp: SupervisorCheckpoint,
    *,
    run_id: str | None = None,
) -> str:
    payload = cp.model_dump(mode="json")
    payload_hash = evidence_repository.content_hash(payload)
    revision = _revision_checkpoint_row(conn, cp.episode_id)
    if revision is not None:
        revision_payload = _checkpoint_payload_for_revision(revision, cp)
        cursor = conn.execute(
            """UPDATE production_revisions
                  SET checkpoint_json=?,updated_at=?
                WHERE id=? AND status IN ('active','published')
                  AND checkpoint_json=?""",
            (
                json.dumps(revision_payload, ensure_ascii=False),
                time.time(),
                revision["id"],
                revision["checkpoint_json"],
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "storyboard supervisor checkpoint revision CAS 冲突"
            )

    latest = conn.execute(
        """SELECT id,content_hash FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC,version DESC LIMIT 1""",
        (CHECKPOINT_TYPE, cp.episode_id),
    ).fetchone()
    if latest and latest["content_hash"] == payload_hash:
        return str(latest["id"])
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type=CHECKPOINT_TYPE,
            scope_type="episode",
            scope_id=cp.episode_id,
            status="validated",
            trust_level="T2",
            content=payload,
            contract_version=get_contract("storyboard").version,
        ),
        conn=conn,
        commit=False,
    )
    evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="storyboard_supervisor",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={
                "phase": cp.phase,
                "repair_epoch": cp.repair_epoch,
                "run_id": run_id,
            },
        ),
        conn=conn,
        commit=False,
    )
    return str(artifact["id"])


def _persist_outline_authority_checkpoint(
    conn,
    authority: StoryboardOutlineAuthority,
) -> None:
    revision = _revision_checkpoint_row(conn, authority.episode_id)
    checkpoint = None
    if revision is not None:
        try:
            revision_payload = json.loads(
                revision["checkpoint_json"] or "{}"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            revision_payload = {}
        checkpoint = _checkpoint_from_payload(
            revision_payload.get("supervisor_checkpoint")
            if isinstance(revision_payload, dict)
            else None
        )
    if checkpoint is None:
        row = conn.execute(
            """SELECT content_json FROM artifacts
               WHERE type=? AND scope_type='episode' AND scope_id=?
                 AND status IN ('candidate','validated','approved')
               ORDER BY created_at DESC,version DESC LIMIT 1""",
            (CHECKPOINT_TYPE, authority.episode_id),
        ).fetchone()
        if row is not None:
            try:
                checkpoint = _checkpoint_from_payload(
                    json.loads(row["content_json"] or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                checkpoint = None
    if checkpoint is None:
        return
    _bind_checkpoint_to_outline_authority(checkpoint, authority)
    _persist_checkpoint_payload(conn, checkpoint)


def load_latest_checkpoint(episode_id: str) -> SupervisorCheckpoint | None:
    conn = get_conn()
    revision = _revision_checkpoint_row(conn, episode_id)
    if revision is not None:
        try:
            raw_checkpoint = json.loads(revision["checkpoint_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_checkpoint = {}
        checkpoint = _checkpoint_from_payload(
            raw_checkpoint.get("supervisor_checkpoint")
            if isinstance(raw_checkpoint, dict)
            else None
        )
        if checkpoint is not None:
            return _bind_checkpoint_to_current_outline(conn, checkpoint)
    row = conn.execute(
        """SELECT id, content_json FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC,version DESC LIMIT 1""",
        (CHECKPOINT_TYPE, episode_id),
    ).fetchone()
    if not row:
        return None
    try:
        raw = json.loads(row["content_json"] or "{}")
        checkpoint = _checkpoint_from_payload(raw)
        return (
            _bind_checkpoint_to_current_outline(conn, checkpoint)
            if checkpoint is not None
            else None
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def save_checkpoint(
    cp: SupervisorCheckpoint,
    *,
    run_id: str | None = None,
    conn=None,
    commit: bool = True,
) -> str:
    cp = _migrate_checkpoint(cp)
    db = conn or get_conn()
    if run_id:
        run_row = db.execute(
            "SELECT status FROM workflow_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        owner = db.execute(
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
            current = db.execute(
                """SELECT id FROM artifacts
                     WHERE type=? AND scope_type='episode' AND scope_id=?
                       AND status IN ('candidate','validated','approved')
                     ORDER BY created_at DESC,version DESC LIMIT 1""",
                (CHECKPOINT_TYPE, cp.episode_id),
            ).fetchone()
            return str(current["id"]) if current else ""
    started_transaction = not db.in_transaction
    if started_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        _bind_checkpoint_to_current_outline(db, cp)
        artifact_id = _persist_checkpoint_payload(
            db,
            cp,
            run_id=run_id,
        )
        if commit:
            db.commit()
    except BaseException:
        if started_transaction or commit:
            db.rollback()
        raise
    if run_id and commit:
        evidence_repository.append_event(
            run_id,
            "STORYBOARD_SUPERVISOR_CHECKPOINT",
            "info",
            f"检查点：{_phase_label(cp.phase)}（已通过 {cp.validated_prefix_end} 镜）",
            payload=cp.model_dump(mode="json"),
        )
    return artifact_id




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
        authority = persist_storyboard_outline_projection(
            str(ep["id"]),
            outline,
            artifact_id=artifact_id,
            conn=conn,
            **_outline_authority_cas(cp),
        )
        if authority is not None:
            _bind_checkpoint_to_outline_authority(cp, authority)
        else:
            cp.outline_artifact_id = artifact_id
        cp.expected_total = len(outline.shots)
        cp.phase = "VALIDATING_OUTLINE"
        cp.outcome = None
        conn.execute(
            "UPDATE episodes SET storyboard_warning=NULL WHERE id=?",
            (ep["id"],),
        )
        conn.commit()
        return outline
    return None




def _storyboard_hash(board: Storyboard) -> str:
    return evidence_repository.content_hash(board.model_dump(mode="json"))




def _repair_is_pending(cp: SupervisorCheckpoint) -> bool:
    repair = cp.last_repair or {}
    return repair.get("status") in {"candidate_pending", "candidate_generating"}




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




def _board_from_rows(rows, episode_no: int) -> Storyboard:
    # Local import avoids the domain module's shared-namespace import cycle.
    from app.domain.storyboard_ops.mutation_primitives import _board_from_shot_rows

    return _board_from_shot_rows(rows, episode_no)




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




async def run_storyboard_supervisor(
    episode_id: str,
    *,
    resume: bool = True,
    run_id: str | None = None,
    preflight_done: bool = False,
    new_activation: bool = False,
) -> SupervisorCheckpoint:
    """集级 Supervisor 主循环；任何入口都会在生成镜头前完成人物/场景资产预检。"""

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("分镜脚本", ["剧集不存在"])

    if not ep["screenplay_json"] or ep["screenplay_status"] != "ready":
        raise StageError("分镜脚本", ["请先生成并确认本集可拍剧本，再展开分镜"])

    # episode_prep_pack (screenplay contract 2.0.0, commit 48e01ff) episodes
    # branch to a structurally different, much simpler generation path here
    # -- before any of the legacy narrative_plan/outline/repair machinery
    # below runs -- because that machinery is built around
    # screenplay.narrative_plan / screenplay.events, which prep_pack payloads
    # never have (the mapping stage stopped producing an event list by
    # design; see docs/STORYBOARD_PROMPT_IR_DESIGN.md). Routing here instead
    # of trying to patch each of the ~50 narrative-contract fields the legacy
    # path assumes is what actually eliminates the silent-empty-projection
    # failure mode (app.production.screenplay_authority
    # .project_prep_pack_to_screenplay used to be reached from here with an
    # always-empty event_chain): prep_pack episodes now simply never execute
    # that code, instead of executing it against data it was never designed
    # to consume.
    from app.production.screenplay_authority import is_prep_pack_payload

    raw_screenplay_payload = json.loads(ep["screenplay_json"])
    if is_prep_pack_payload(raw_screenplay_payload):
        from app.production.storyboard_pack import run_storyboard_pack_generation

        return await run_storyboard_pack_generation(
            episode_id, ep=ep, conn=conn, payload=raw_screenplay_payload, resume=resume,
        )

    # The heavy narrative_plan/event_chain-driven generation+repair pipeline
    # that used to run here (outline compile -> scene-pack generation ->
    # per-shot repair/gate/finalize loop, ~2300 lines) was deleted along with
    # its only callers, app.stages.generate_storyboard_outline /
    # generate_storyboard_scene_pack / generate_storyboard_next_shot (see
    # commit history: prep_pack 2.0.0 / storyboard 2.0.0 stopped producing
    # narrative_plan-bearing screenplays; every screenplay_json the mapping
    # stage writes is an episode_prep_pack payload, which the branch above
    # already routes to run_storyboard_pack_generation). A screenplay_json
    # that reaches this point without "prep_pack_version" is not a supported
    # input any more -- fail loudly instead of running retired machinery
    # against data it was never built to receive.
    raise StageError(
        "分镜脚本",
        ["当前剧本不是 episode_prep_pack 格式，旧的叙事权威分镜管线已下线；请重新生成剧本"],
    )


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


