"""分镜续跑判据：检查点匹配、已有产出判定、续跑决策与生成前置检查 payload。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 mutation_primitives 与 task_run。
"""
from __future__ import annotations

import json

from app.db import get_conn
from app.domain.common import (
    _as_body_dict,
    _episode_or_404,
    _screenplay_ready,
    router,
)
from fastapi import (
    Body,
    HTTPException,
)

from .mutation_primitives import _screenplay_rebuild_block
from .task_run import _storyboard_generation_is_live


def _storyboard_has_material(episode_id: str, ep: dict | None = None) -> bool:
    """Return whether the current episode projection still owns storyboard work."""
    episode = dict(ep) if ep is not None else dict(_episode_or_404(episode_id))
    shot_count = int(get_conn().execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
    ).fetchone()["c"])
    return bool(
        shot_count
        or episode.get("storyboard_outline_json")
        or episode.get("storyboard_artifact_id")
        or episode.get("working_storyboard_artifact_id")
        or episode.get("published_storyboard_artifact_id")
        or episode.get("storyboard_production_revision_id")
        or episode.get("storyboard_completion_certificate_id")
    )

def _storyboard_checkpoint_matches_screenplay(cp, ep: dict) -> bool:
    """Only checkpoints bound to the current screenplay and Bible may resume."""
    if cp is None:
        return False
    bound = str(cp.input_versions.get("screenplay_artifact_id") or "")
    current = str(ep.get("screenplay_artifact_id") or "")
    bound_bible = str(cp.input_versions.get("bible_artifact_id") or "")
    current_bible = str(ep.get("bible_artifact_id") or "")
    if not current_bible and ep.get("project_id"):
        project = get_conn().execute(
            "SELECT bible_artifact_id FROM projects WHERE id=?",
            (ep["project_id"],),
        ).fetchone()
        current_bible = str(project["bible_artifact_id"] or "") if project else ""
    return bool(
        bound
        and current
        and bound == current
        and bound_bible
        and current_bible
        and bound_bible == current_bible
    )

def _storyboard_has_persisted_work(episode_id: str, ep: dict | None = None) -> bool:
    """Whether this episode already has storyboard work that must be resumed or cleared.

    Starting a task and continuing a task are deliberately separate user actions.  A
    fresh start must never silently replace shots or adopt a checkpoint left by an
    earlier run.
    """
    from app.storyboard_supervisor import load_latest_checkpoint

    episode = ep or dict(_episode_or_404(episode_id))
    if _storyboard_has_material(episode_id, episode):
        return True
    checkpoint = load_latest_checkpoint(episode_id)
    # Publishing a new screenplay clears the current storyboard projection but
    # intentionally keeps historical checkpoint artifacts for audit.  Such a
    # checkpoint is not resumable work for the new screenplay.
    return _storyboard_checkpoint_matches_screenplay(checkpoint, episode)

def _storyboard_resume_decision(episode_id: str, ep: dict | None = None) -> dict:
    """Project the one authoritative decision for resuming a storyboard.

    ``is_final`` only says that the current tail closes the episode.  It does
    not say that the whole-board confirmation gates passed.  A completed tail
    with unresolved hard gates must reopen the Supervisor's non-destructive
    repair loop, while a genuinely confirmable board must still reject blind
    append attempts.
    """
    episode = dict(ep) if ep is not None else dict(_episode_or_404(episode_id))
    published_release_bound = bool(
        episode.get("published_storyboard_artifact_id")
        and episode.get("storyboard_completion_certificate_id")
        and episode.get("storyboard_production_revision_id")
    )
    if (
        published_release_bound
        and episode.get("status") in {"confirmed", "generating", "done", "mixed"}
    ):
        return {
            "allowed": False,
            "resume_mode": None,
            "blocking_reason": (
                "当前分镜已有已确认发布基线，不能原地续跑；"
                "修订必须在隔离候选中完成并重新发布"
            ),
            "storyboard_status": None,
        }
    row = get_conn().execute(
        "SELECT shot_no,shot_contract_json FROM shots WHERE episode_id=? "
        "ORDER BY shot_no DESC LIMIT 1",
        (episode_id,),
    ).fetchone()
    tail_is_final = False
    if row and row["shot_contract_json"]:
        try:
            tail_is_final = bool(json.loads(row["shot_contract_json"] or "{}").get("is_final"))
        except (TypeError, ValueError, json.JSONDecodeError):
            tail_is_final = False

    # An unfinished tail is ordinary checkpoint continuation.
    if not tail_is_final:
        return {
            "allowed": True,
            "resume_mode": "continue_generation",
            "blocking_reason": None,
            "storyboard_status": None,
        }

    # Use the same atomic projection that drives the primary UI action.  This
    # prevents preflight from promising a repair that the POST endpoint later
    # mistakes for an attempt to append after an is_final tail.
    from app import api as api_facade

    status = api_facade.episode_detail(episode_id, view="board")["storyboard_status"]
    allowed = status.get("recommended_action") == "resume_storyboard"
    if allowed:
        return {
            "allowed": True,
            "resume_mode": status.get("resume_mode") or "continue_generation",
            "blocking_reason": None,
            "storyboard_status": status,
        }
    if (
        status.get("recommended_action") == "confirm_storyboard"
        and not (
            episode.get("storyboard_artifact_id")
            and episode.get("storyboard_completion_certificate_id")
        )
    ):
        return {
            "allowed": True,
            "resume_mode": "finalize_evidence",
            "blocking_reason": None,
            "storyboard_status": status,
        }

    # Preserve recovery from a stale projection whose durable Run has already
    # ended.  A live task is handled by the caller's deduplication guard; a
    # confirmed/done episode must never enter this compatibility path.
    if (
        episode.get("status") in {"scripting", "script_failed"}
        and not _storyboard_generation_is_live(episode)
    ):
        return {
            "allowed": True,
            "resume_mode": "continue_generation",
            "blocking_reason": None,
            "storyboard_status": status,
        }

    if status.get("recommended_action") == "confirm_storyboard":
        reason = "当前分镜已完整收束且确认门禁已通过，请直接确认分镜，无需继续生成"
    elif status.get("recommended_action") == "go_review_wall":
        reason = "当前分镜已经确认，不能再续跑；如需调整请先创建新的制作修订"
    else:
        reason = status.get("write_block_reason") or "当前收尾镜后没有可恢复的生成或修复任务"
    return {
        "allowed": False,
        "resume_mode": None,
        "blocking_reason": reason,
        "storyboard_status": status,
    }

def _storyboard_start_preflight_payload(episode_id: str) -> dict:
    from app.storyboard_supervisor import load_latest_checkpoint
    from app.storyboard_workspace import episode_fingerprint

    ep = _episode_or_404(episode_id)
    if not _screenplay_ready(ep):
        rebuild_block = _screenplay_rebuild_block(get_conn(), ep)
        if rebuild_block is not None:
            raise HTTPException(409, rebuild_block)
        raise HTTPException(409, "请先在映射台生成本集可拍剧本")
    conn = get_conn()
    shots = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
    ).fetchone()["c"])
    action = "resume" if _storyboard_has_persisted_work(episode_id, dict(ep)) else "create"
    cp = load_latest_checkpoint(episode_id) if action == "resume" else None
    resume_decision = (
        _storyboard_resume_decision(episode_id, dict(ep))
        if action == "resume"
        else {
            "allowed": True,
            "resume_mode": "create",
            "blocking_reason": None,
            "storyboard_status": None,
        }
    )
    current_status = resume_decision.get("storyboard_status") or {}
    current_gate_issues = list(current_status.get("hard_gate_issues") or [])
    planned = int(cp.expected_total or 0) if cp else 0
    if not planned and ep["storyboard_outline_json"]:
        try:
            planned = len(json.loads(ep["storyboard_outline_json"] or "{}").get("shots") or [])
        except (TypeError, ValueError, json.JSONDecodeError):
            planned = 0
    kept = (
        min(shots, max(0, int(cp.validated_prefix_end or 0)))
        if action == "resume" and cp else (shots if action == "resume" else 0)
    )
    resume_from = kept + 1 if action == "resume" else 1
    remaining = max(0, planned - kept) if planned else None
    strategies_exhausted = bool(
        cp and cp.outcome in {
            "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
            "SUCCEEDED_GATE_RETRY_EXHAUSTED_FALLBACK",
            "WAITING_RETRY_GATE_REPAIR_EXHAUSTED",
        }
    )
    latest_run = conn.execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    provider_stats = {"external_calls": 0, "cache_reuses": 0}
    if latest_run:
        row = conn.execute(
            """SELECT
                   SUM(CASE WHEN kind='chat' AND status IN ('OK','SUCCESS','SUCCEEDED') THEN 1 ELSE 0 END) AS external_calls,
                   SUM(CASE WHEN kind='provider_cache_hit' AND status='REUSED' THEN 1 ELSE 0 END) AS cache_reuses
               FROM provider_calls WHERE run_id=?""",
            (latest_run["id"],),
        ).fetchone()
        provider_stats = {
            "external_calls": int(row["external_calls"] or 0) if row else 0,
            "cache_reuses": int(row["cache_reuses"] or 0) if row else 0,
        }
    return {
        "episode_id": episode_id,
        "action": action,
        "resume_mode": resume_decision["resume_mode"],
        "screenplay_artifact_id": ep["screenplay_artifact_id"],
        "storyboard_artifact_id": ep["storyboard_artifact_id"],
        "checkpoint": {
            "available": bool(cp),
            "phase": cp.phase if cp else None,
            "resume_from_shot": resume_from,
        },
        "kept_validated_shots": kept,
        "planned_shots": planned or None,
        "remaining_shots": remaining,
        "can_start": bool(resume_decision["allowed"]),
        "blocking_reason": resume_decision["blocking_reason"],
        "current_gate_issue_count": len(current_gate_issues),
        "current_gate_issues": current_gate_issues[:12],
        "gate_retry_exhausted": strategies_exhausted,
        "warning": (
            "上一轮修复预算已用尽；继续后将开启新的有界修复轮次，现有分镜在候选通过前保持不变"
            if strategies_exhausted else None
        ),
        "repair": {
            "lifetime_repair_count": int(cp.repair_epoch or 0) if cp else 0,
            "activation_no": int(cp.activation_no or 0) if cp else 0,
            "activation_attempt_count": int(cp.activation_attempt_count or 0) if cp else 0,
            "max_attempts_per_activation": 6,
            "candidate_preserves_official_shots": True,
            "last_issue_messages": (
                current_gate_issues[:12]
                or (list((cp.last_repair or {}).get("issue_messages") or [])[:12] if cp else [])
            ),
            **provider_stats,
        },
        "impact": (
            "保留现有镜头，重新执行整集门禁并仅在候选通过后替换问题镜"
            if resume_decision["resume_mode"] == "repair_existing"
            else "保留全部已通过镜头，仅续做冷观众审读和发布证据签发"
            if resume_decision["resume_mode"] == "finalize_evidence"
            else "保留已通过逐镜校验的镜头，并从下一镜继续"
            if action == "resume"
            else "从空白开始生成本集分镜"
        ),
        "estimated_wait_minutes": [max(1, (remaining or planned or 1)), max(2, (remaining or planned or 1) * 3)],
        "estimate_note": "分镜文本生成后不会自动提交视频生成，需人工确认",
        "baseline_fingerprint": episode_fingerprint(episode_id),
    }

@router.post("/episodes/{episode_id}/storyboard/preflight")
def storyboard_start_preflight(episode_id: str, body: dict | None = Body(None)):
    from app.storyboard_workspace import create_preview

    _as_body_dict(body)
    payload = _storyboard_start_preflight_payload(episode_id)
    return create_preview(f"start:{payload['action']}", episode_id, payload)
