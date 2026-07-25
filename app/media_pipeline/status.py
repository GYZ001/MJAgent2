"""镜头级流水线状态：供 API / 前端展示真实阶段与 ETA。"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, now
from app.media_pipeline import stages as S

# 粗粒度历史默认（秒）；后续可用真实 P50 替换
_STAGE_P50 = {
    S.STAGE_REFERENCE: 90.0,
    S.STAGE_VIDEO_SUBMIT: 2.0,
    S.STAGE_VIDEO_POLL: 180.0,
    S.STAGE_DOWNLOAD: 15.0,
    S.STAGE_QA: 40.0,
}


def _job_stage(job, version) -> tuple[str, str]:
    """返回 (pipeline_status, current_stage)。"""
    status = job["status"]
    meta = json.loads((version["image_inputs"] if version else None) or "{}")
    has_refs = bool(meta.get("reference_images") or meta.get("reference_set_id"))
    has_task = bool(version and version["provider_task_id"])

    if status == S.WAITING_PROVIDER:
        return "waiting_provider", S.STAGE_VIDEO_POLL
    if status == "paused_budget":
        return S.WAITING_BUDGET, S.STAGE_VIDEO_SUBMIT
    if status in ("cancelled", "abandoned"):
        return status, S.STAGE_VIDEO_SUBMIT
    if status == "failed":
        return "failed", S.STAGE_VIDEO_SUBMIT
    if status == "succeeded":
        return "succeeded", S.STAGE_ADOPT
    if status == "running":
        if not has_refs and not has_task:
            return "running", S.STAGE_REFERENCE
        if not has_task:
            return "running", S.STAGE_VIDEO_SUBMIT
        # 已有 task_id 却仍 running：收尾（下载/QA）
        return "running", S.STAGE_DOWNLOAD
    if status == "queued":
        if has_task:
            return "queued", S.STAGE_DOWNLOAD  # 轮询完成待下载，或待再次 poll
        if has_refs:
            return "queued", S.STAGE_VIDEO_SUBMIT
        return "queued", S.STAGE_REFERENCE
    return status or "unknown", S.STAGE_VIDEO_SUBMIT


def _status_from_rows(shot, *, candidate_count: int, retake_count: int, job,
                      provider_task_id: str | None, queue_position: int | None,
                      reference_progress: dict[str, int] | None, db) -> dict[str, Any]:
    """Build UI status without loading shot_versions.image_inputs.

    Historical image_inputs rows may contain hundreds of megabytes of embedded
    data URLs. Pipeline status only needs scalar version/job/reference facts, so
    reading and decoding that JSON here is both unnecessary and dangerously
    expensive.
    """
    blocked_reason = None
    provider_elapsed_s = None
    estimated_start_at = None
    estimated_finish_at = None
    pipeline_status = "idle"
    current_stage = None

    if job:
        status = job["status"]
        if status == S.WAITING_PROVIDER:
            pipeline_status, current_stage = "waiting_provider", S.STAGE_VIDEO_POLL
        elif status == "paused_budget":
            pipeline_status, current_stage = S.WAITING_BUDGET, S.STAGE_VIDEO_SUBMIT
        elif status == "running":
            pipeline_status = "running"
            current_stage = S.STAGE_DOWNLOAD if provider_task_id else S.STAGE_REFERENCE
        elif status == "queued":
            pipeline_status = "queued"
            current_stage = S.STAGE_DOWNLOAD if provider_task_id else S.STAGE_REFERENCE
        else:
            pipeline_status, current_stage = status or "unknown", S.STAGE_VIDEO_SUBMIT
        if job["provider_submitted_at"]:
            provider_elapsed_s = max(0.0, now() - float(job["provider_submitted_at"]))
        if job["error"] and "等待" in (job["error"] or ""):
            blocked_reason = job["error"]
        p50 = _STAGE_P50.get(current_stage or "", 60.0)
        if job["status"] == "queued" and job["next_retry_at"]:
            estimated_start_at = float(job["next_retry_at"])
        elif job["status"] == "queued":
            estimated_start_at = now() + max(0, (queue_position or 1) - 1) * 30.0
        else:
            estimated_start_at = now()
        remaining = p50
        if current_stage == S.STAGE_VIDEO_POLL and provider_elapsed_s is not None:
            remaining = max(30.0, p50 - provider_elapsed_s)
        estimated_finish_at = (estimated_start_at or now()) + remaining

        # 连续镜阻塞探测
        if job["after_shot_id"] and job["status"] == "queued" and not provider_task_id:
            from app.media_pipeline.scheduler import continuity_anchor_ready
            ready, reason = continuity_anchor_ready(db, job["after_shot_id"])
            if not ready:
                pipeline_status = S.BLOCKED if "人工" not in (reason or "") else S.WAITING_HUMAN
                blocked_reason = reason
                current_stage = S.STAGE_REFERENCE
    elif candidate_count > 0 and not shot["adopted_version_id"]:
        pipeline_status = S.WAITING_HUMAN
        current_stage = S.STAGE_ADOPT
        blocked_reason = "已有候选，等待人工采用"
    elif shot["adopted_version_id"]:
        pipeline_status = "adopted"
        current_stage = S.STAGE_ADOPT

    label = None
    if current_stage == S.STAGE_VIDEO_POLL and provider_elapsed_s is not None:
        m, s = divmod(int(provider_elapsed_s), 60)
        label = f"Seedance 已提交，生成 {m}分{s:02d}秒"
    elif current_stage == S.STAGE_REFERENCE and reference_progress:
        label = f"准备参考图 {reference_progress['done']}/{reference_progress['total']}"
    elif pipeline_status == "queued" and queue_position:
        label = f"排队中（第 {queue_position} 位）"
    elif current_stage:
        label = S.PIPELINE_STAGE_LABELS.get(current_stage, current_stage)
    if blocked_reason and pipeline_status in (S.BLOCKED, S.WAITING_HUMAN):
        label = blocked_reason

    return {
        "pipeline_status": pipeline_status,
        "current_stage": current_stage,
        "stage_label": label,
        "queue_position": queue_position,
        "provider_elapsed_s": provider_elapsed_s,
        "reference_progress": reference_progress,
        "candidate_count": candidate_count,
        "retake_count": retake_count,
        "blocked_reason": blocked_reason,
        "estimated_start_at": estimated_start_at,
        "estimated_finish_at": estimated_finish_at,
    }


def episode_pipeline_statuses(episode_id: str, *, conn=None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load all shot statuses for an episode with a fixed number of light queries."""
    db = conn or get_conn()
    shots = db.execute(
        "SELECT id, adopted_version_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if not shots:
        return {}, {
            "shots_total": 0, "adopted": 0, "with_candidate": 0,
            "upstream_generating": 0, "preparing_references": 0,
            "queued": 0, "waiting_human": 0,
        }

    version_rows = db.execute(
        """SELECT v.shot_id,
                  SUM(CASE WHEN v.status='succeeded' AND v.video_path IS NOT NULL
                           AND v.video_path!='' THEN 1 ELSE 0 END) AS candidate_count,
                  MAX(v.version_no) AS latest_version_no
           FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id
           WHERE s.episode_id=?
           GROUP BY v.shot_id""",
        (episode_id,),
    ).fetchall()
    version_stats = {row["shot_id"]: row for row in version_rows}

    job_rows = db.execute(
        """SELECT j.*, v.provider_task_id
           FROM jobs j
           LEFT JOIN shot_versions v ON v.id=j.version_id
           WHERE j.episode_id=? AND j.kind='video'
             AND j.status IN ('queued','running','waiting_provider','paused_budget','waiting_retry')
             AND j.cancellation_requested=0 AND j.abandoned=0
           ORDER BY j.created_at DESC""",
        (episode_id,),
    ).fetchall()
    jobs_by_shot = {}
    for row in job_rows:
        jobs_by_shot.setdefault(row["shot_id"], row)

    queued_rows = db.execute(
        """SELECT id FROM jobs
           WHERE kind='video' AND status='queued'
             AND cancellation_requested=0 AND abandoned=0
             AND (next_retry_at IS NULL OR next_retry_at<=?)
           ORDER BY created_at""",
        (now(),),
    ).fetchall()
    queue_positions = {row["id"]: index + 1 for index, row in enumerate(queued_rows)}

    reference_rows = db.execute(
        """SELECT rs.shot_id,
                  SUM(CASE WHEN ra.deleted=0 THEN 1 ELSE 0 END) AS total,
                  SUM(CASE WHEN ra.deleted=0 AND ra.selected=1 THEN 1 ELSE 0 END) AS selected
           FROM reference_sets rs
           JOIN shots s ON s.id=rs.shot_id
           LEFT JOIN reference_assets ra ON ra.reference_set_id=rs.id
           WHERE s.episode_id=? AND rs.id IN (
             SELECT newest.id FROM reference_sets newest
             WHERE newest.revision=(
               SELECT MAX(candidate.revision) FROM reference_sets candidate
               WHERE candidate.shot_id=newest.shot_id
             )
           )
           GROUP BY rs.shot_id""",
        (episode_id,),
    ).fetchall()
    reference_stats = {row["shot_id"]: row for row in reference_rows}

    statuses: dict[str, dict[str, Any]] = {}
    adopted = 0
    with_candidate = 0
    queued = 0
    waiting_human = 0
    for s in shots:
        version = version_stats.get(s["id"])
        candidate_count = int(version["candidate_count"] or 0) if version else 0
        # The exact legacy auto-retake counter lives inside huge JSON. Version
        # attempts are a safe lightweight approximation for display purposes.
        retake_count = max(0, int(version["latest_version_no"] or 1) - 1) if version else 0
        job = jobs_by_shot.get(s["id"])
        refs = reference_stats.get(s["id"])
        reference_progress = None
        if refs and int(refs["total"] or 0) > 0:
            reference_progress = {
                "done": int(refs["selected"] or 0),
                "total": int(refs["total"] or 0),
            }
        st = _status_from_rows(
            s,
            candidate_count=candidate_count,
            retake_count=retake_count,
            job=job,
            provider_task_id=job["provider_task_id"] if job else None,
            queue_position=queue_positions.get(job["id"]) if job else None,
            reference_progress=reference_progress,
            db=db,
        )
        statuses[s["id"]] = st
        if s["adopted_version_id"]:
            adopted += 1
        if st.get("candidate_count", 0) > 0 or s["adopted_version_id"]:
            with_candidate += 1
        ps = st.get("pipeline_status")
        if ps == "queued":
            queued += 1
        elif ps in (S.WAITING_HUMAN, S.BLOCKED):
            waiting_human += 1

    upstream = int(db.execute(
        """SELECT COUNT(*) c FROM jobs
           WHERE episode_id=? AND kind='video'
             AND status='waiting_provider'
             AND cancellation_requested=0 AND abandoned=0""",
        (episode_id,),
    ).fetchone()["c"])
    preparing_refs = int(db.execute(
        """SELECT COUNT(*) c FROM jobs j
           JOIN shot_versions v ON v.id=j.version_id
           WHERE j.episode_id=? AND j.kind='video' AND j.status='running'
             AND (v.provider_task_id IS NULL OR v.provider_task_id='')
             AND j.cancellation_requested=0 AND j.abandoned=0""",
        (episode_id,),
    ).fetchone()["c"])
    summary = {
        "shots_total": len(shots),
        "adopted": adopted,
        "with_candidate": with_candidate,
        "upstream_generating": upstream,
        "preparing_references": preparing_refs,
        "queued": queued,
        "waiting_human": waiting_human,
    }
    return statuses, summary


def shot_pipeline_status(shot_id: str, *, conn=None) -> dict[str, Any]:
    db = conn or get_conn()
    row = db.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        return {}
    statuses, _ = episode_pipeline_statuses(row["episode_id"], conn=db)
    return statuses.get(shot_id, {})


def episode_pipeline_summary(episode_id: str, *, conn=None) -> dict[str, Any]:
    _, summary = episode_pipeline_statuses(episode_id, conn=conn)
    return summary
