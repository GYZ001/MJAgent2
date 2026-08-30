"""Per-shot mode-attempt bookkeeping: read/record a shot's execution plan and
its actual-mode history, and audit one job's mode lineage.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale). Two
originally non-adjacent groups in the pre-split source
(``get_shot_plan``/``record_mode_attempt``/``active_plan_is_current`` and,
far below it, ``mode_audit_for_job``) share this one concern and are moved
into the same file.
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, new_id, now

from .models import ShotVideoGenerationPlan, VideoGenerationMode
from .publish import _shot_plan_from_row, load_latest_plan
from .release_manifest import shot_video_execution_contract_fingerprint
from .staleness import verify_episode_plan_is_current


def get_shot_plan(shot_id: str, *, conn=None) -> ShotVideoGenerationPlan | None:
    db = conn or get_conn()
    shot = db.execute(
        "SELECT episode_id FROM shots WHERE id=?",
        (shot_id,),
    ).fetchone()
    if not shot:
        return None
    plan = load_latest_plan(str(shot["episode_id"]), conn=db)
    if plan is None or not verify_episode_plan_is_current(plan, conn=db):
        return None
    return next((item for item in plan.shots if item.shot_id == shot_id), None)


def record_mode_attempt(
    *,
    version_id: str,
    shot_plan: ShotVideoGenerationPlan,
    actual_mode: VideoGenerationMode,
    status: str,
    provider_task_id: str | None = None,
    error: str | None = None,
    conn=None,
) -> str:
    db = conn or get_conn()
    running = db.execute(
        """SELECT id FROM video_generation_attempts
           WHERE version_id=? AND status='provider_running'
             AND COALESCE(provider_task_id,'')=COALESCE(?,'')
           ORDER BY attempt_no DESC LIMIT 1""",
        (version_id, provider_task_id),
    ).fetchone()
    terminal_running = (
        db.execute(
            """SELECT id FROM video_generation_attempts
               WHERE version_id=? AND status='provider_running'
               ORDER BY attempt_no DESC LIMIT 1""",
            (version_id,),
        ).fetchone()
        if status != "provider_running" else None
    )
    existing = running or terminal_running
    attempt_id = existing["id"] if existing else new_id("vattempt")
    if existing:
        db.execute(
            """UPDATE video_generation_attempts SET actual_mode=?,status=?,
                      provider_task_id=?,error=?,updated_at=? WHERE id=?""",
            (actual_mode.value, status, provider_task_id, error, now(), attempt_id),
        )
    else:
        attempt_no = int(db.execute(
            "SELECT COALESCE(MAX(attempt_no),0)+1 n FROM video_generation_attempts WHERE version_id=?",
            (version_id,),
        ).fetchone()["n"])
        db.execute(
            """INSERT INTO video_generation_attempts(
                   id,shot_plan_id,version_id,attempt_no,planned_mode,actual_mode,
                   video_input_intent,status,provider_task_id,error,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                attempt_id, shot_plan.shot_plan_id, version_id, attempt_no,
                shot_plan.mode.value, actual_mode.value,
                shot_plan.video_input_intent.value if shot_plan.video_input_intent else None,
                status, provider_task_id, error, now(), now(),
            ),
        )
    db.execute(
        """UPDATE shot_video_generation_plans
              SET actual_mode=?,status=?,updated_at=? WHERE id=?""",
        (actual_mode.value, status, now(), shot_plan.shot_plan_id),
    )
    if conn is None:
        db.commit()
    return attempt_id


def active_plan_is_current(shot_plan_id: str, *, conn=None) -> bool:
    db = conn or get_conn()
    row = db.execute(
        """SELECT sp.*,ep.status AS episode_status,ep.episode_id,
                  ep.plan_revision,ep.source_storyboard_revision_id
           FROM shot_video_generation_plans sp
           JOIN episode_video_generation_plans ep ON ep.id=sp.episode_video_plan_id
           WHERE sp.id=?""",
        (shot_plan_id,),
    ).fetchone()
    if (
        not row
        or row["status"] in {"stale", "superseded"}
    ):
        return False
    plan = load_latest_plan(str(row["episode_id"]), conn=db)
    if plan is None or not verify_episode_plan_is_current(plan, conn=db):
        return False
    current = next(
        (item for item in plan.shots if item.shot_id == row["shot_id"]),
        None,
    )
    if current is None:
        return False
    if row["episode_status"] == "valid":
        return current.shot_plan_id == shot_plan_id
    previous = _shot_plan_from_row(row, row)
    return (
        shot_video_execution_contract_fingerprint(previous)
        == shot_video_execution_contract_fingerprint(current)
    )


def mode_audit_for_job(job_id: str, *, conn=None) -> dict[str, Any] | None:
    db = conn or get_conn()
    row = db.execute(
        """SELECT j.id AS job_id,j.status AS job_status,j.reason_code,j.reason_text,
                  v.id AS version_id,v.provider_task_id,v.image_inputs,
                  sp.*,ep.plan_revision,ep.source_storyboard_revision_id,
                  ep.capability_snapshot_id AS episode_capability_snapshot_id
           FROM jobs j
           LEFT JOIN shot_versions v ON v.id=j.version_id
           LEFT JOIN shot_video_generation_plans sp
             ON sp.id=CASE WHEN json_valid(v.image_inputs)
                           THEN json_extract(v.image_inputs,'$.shot_plan_id') END
           LEFT JOIN episode_video_generation_plans ep ON ep.id=sp.episode_video_plan_id
           WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    try:
        meta = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    return {
        "job_id": row["job_id"],
        "job_status": row["job_status"],
        "version_id": row["version_id"],
        "provider_task_id": row["provider_task_id"],
        "shot_plan_id": meta.get("shot_plan_id"),
        "plan_revision": row["plan_revision"],
        "source_storyboard_revision_id": row["source_storyboard_revision_id"],
        "capability_snapshot_id": row["episode_capability_snapshot_id"],
        "planned_mode": row["planned_mode"] or meta.get("planned_mode") or meta.get("mode"),
        "actual_mode": row["actual_mode"] or meta.get("actual_mode"),
        "video_input_intent": row["video_input_intent"] or meta.get("video_input_intent"),
        "depends_on_shot_id": row["depends_on_shot_id"] or meta.get("after_shot_id"),
        "status": row["status"] or meta.get("plan_status"),
        "degraded_from_mode": row["degraded_from_mode"],
        "degraded_to_mode": row["degraded_to_mode"],
        "degraded_reason": row["degraded_reason"],
        "input_fingerprints": (
            json.loads(row["input_fingerprints_json"] or "{}")
            if row["input_fingerprints_json"] else meta.get("input_revision_fingerprints") or {}
        ),
        "reason_code": row["reason_code"],
        "reason_text": row["reason_text"],
        "stale": bool(meta.get("stale")),
        "stale_reason": meta.get("stale_reason"),
    }
