"""Supervisor checkpoint 的持久化、心跳刷新与对外投影。"""
from __future__ import annotations

import asyncio
import json

from typing import Any

from app.db import get_conn, new_id, now
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact

from .authority import _supervisor_checks_can_use_worker_thread
from .constants import (
    CHECKPOINT_ARTIFACT_TYPE,
    SUPERVISOR_HEARTBEAT_STALE_S,
    TERMINAL_SUPERVISOR_PHASES,
    _CHECKPOINT_WRITE_SEMAPHORE,
)
from .models import VideoSupervisorCheckpoint



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


def _persist_checkpoint_transaction(
    conn: Any,
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
) -> str:
    rid = run_id or cp.run_id
    if rid:
        conn.execute(
            """UPDATE workflow_runs
               SET updated_at=?, deadline_at=COALESCE(deadline_at, ?)
               WHERE id=? AND status IN (
                   'CREATED','RUNNING','WAITING_RETRY','WAITING_HUMAN',
                   'WAITING_AUTHORIZATION','PAUSED_BUDGET','PAUSED_EXTERNAL'
               )""",
            (cp.last_heartbeat_at, cp.deadline_at, rid),
        )

    existing = conn.execute(
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
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type=CHECKPOINT_ARTIFACT_TYPE,
            scope_type="episode",
            scope_id=cp.episode_id,
            status="validated",
            trust_level="T2",
            content=cp.model_dump(mode="json"),
            contract_version="video-supervisor-1.0.0",
        ),
        conn=conn,
        commit=False,
    )
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="video_supervisor",
        evaluator_version="1.0.0",
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={"phase": cp.phase, "repair_epoch": cp.repair_epoch, "run_id": rid},
    )
    conn.execute(
        """INSERT INTO evaluations(
               id, artifact_id, step_run_id, evaluator_type, evaluator_name,
               evaluator_version, status, hard_gate_passed, evaluation_role,
               score_status, runtime_blocking, retry_eligible, score,
               dimension_scores_json, issues_json, evidence_json, raw_result_ref,
               confidence, recovered, created_at
           ) VALUES(?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("eval"),
            artifact["id"],
            evaluation.evaluator_type,
            evaluation.evaluator_name,
            evaluation.evaluator_version,
            evaluation.status,
            int(evaluation.hard_gate_passed),
            evaluation.evaluation_role,
            evaluation.score_status,
            int(evaluation.runtime_blocking),
            int(evaluation.retry_eligible),
            evaluation.score,
            json.dumps(evaluation.dimension_scores, ensure_ascii=False),
            json.dumps(
                [issue.model_dump(mode="json") for issue in evaluation.issues],
                ensure_ascii=False,
            ),
            json.dumps(evaluation.evidence, ensure_ascii=False),
            evaluation.raw_result_ref,
            evaluation.confidence,
            int(evaluation.recovered),
            now(),
        ),
    )
    if rid:
        conn.execute(
            """INSERT INTO run_events(
                   id, run_id, step_run_id, ts, event_type, severity, message,
                   payload_json, trace_id
               ) VALUES(?,?,NULL,?,?,?,?,?,NULL)""",
            (
                new_id("evt"),
                rid,
                now(),
                "VIDEO_SUPERVISOR_CHECKPOINT",
                "info",
                f"Video supervisor checkpoint phase={cp.phase} epoch={cp.repair_epoch}",
                json.dumps(
                    {
                        "phase": cp.phase,
                        "coverage": cp.coverage,
                        "tick_no": cp.tick_no,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    return artifact["id"]


def save_checkpoint(cp: VideoSupervisorCheckpoint, *, run_id: str | None = None) -> str:
    """Persist one complete checkpoint atomically on the caller's DB connection."""
    cp.last_heartbeat_at = now()
    conn = get_conn()
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        artifact_id = _persist_checkpoint_transaction(conn, cp, run_id=run_id)
        conn.commit()
        return artifact_id
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


async def _save_checkpoint_async(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None = None,
) -> str:
    """Serialize complete checkpoint writes and keep file-backed SQLite off-loop."""
    cp.last_heartbeat_at = now()
    snapshot = cp.model_copy(deep=True)
    return await _run_checkpoint_write(save_checkpoint, snapshot, run_id=run_id)


async def _run_checkpoint_write(operation: Any, *args: Any, **kwargs: Any) -> Any:
    """Hold the bounded writer slot until an off-loop transaction really exits."""
    async with _CHECKPOINT_WRITE_SEMAPHORE:
        if not _supervisor_checks_can_use_worker_thread():
            return operation(*args, **kwargs)
        worker_task = asyncio.create_task(
            asyncio.to_thread(operation, *args, **kwargs),
        )
        cancelled = False
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
