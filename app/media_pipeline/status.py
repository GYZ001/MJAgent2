"""镜头级流水线状态：优先读取 jobs 持久化阶段，前端不再二次猜测。"""
from __future__ import annotations

from typing import Any

from app.db import get_conn, now
from app.media_pipeline import stages as S
from app.media_pipeline.stage_state import read_job_pipeline, stage_label


VIDEO_STATUS_PENDING_GENERATION = "pending_generation"
VIDEO_STATUS_GENERATING = "generating"
VIDEO_STATUS_PENDING_ADOPTION = "pending_adoption"
VIDEO_STATUS_ADOPTED = "adopted"
VIDEO_STATUS_GENERATION_FAILED = "generation_failed"

VIDEO_STATUS_LABELS = {
    VIDEO_STATUS_PENDING_GENERATION: "待生成",
    VIDEO_STATUS_GENERATING: "生成中",
    VIDEO_STATUS_PENDING_ADOPTION: "待采纳",
    VIDEO_STATUS_ADOPTED: "已采纳",
    VIDEO_STATUS_GENERATION_FAILED: "生成失败",
}


def _video_status(*, has_adopted: bool, has_active_job: bool,
                  candidate_count: int, latest_version_status: str | None) -> str:
    """Project persisted video state to the only five statuses shown on the review wall.

    The persisted adoption pointer is deliberately terminal for display purposes. Playback
    health is reported separately; neither a stale flag nor a concurrent retake may make a
    shot selected by the user/Supervisor look incomplete again.
    """
    if has_adopted:
        return VIDEO_STATUS_ADOPTED
    if has_active_job:
        return VIDEO_STATUS_GENERATING
    if candidate_count > 0:
        return VIDEO_STATUS_PENDING_ADOPTION
    if latest_version_status == "failed":
        return VIDEO_STATUS_GENERATION_FAILED
    return VIDEO_STATUS_PENDING_GENERATION


def _macro_status_from_job(job) -> str:
    status = job["status"]
    if status == "paused_budget":
        return S.WAITING_BUDGET
    if status in ("cancelled", "abandoned", "failed", "succeeded"):
        return status
    if status == S.WAITING_PROVIDER:
        return "waiting"
    if status == "running":
        return "running"
    if status == S.WAITING_RETRY:
        return "waiting"
    if status == S.WAITING_HUMAN:
        return S.WAITING_HUMAN
    if status == "queued":
        persisted = read_job_pipeline(job)
        stage = persisted.get("pipeline_stage")
        if stage in (
            S.STAGE_WAITING_CONTINUITY, S.STAGE_WAITING_DEPENDENCY,
            S.STAGE_WAITING_VIDEO_SLOT, S.STAGE_WAITING_HUMAN,
        ):
            return "waiting"
        return "queued"
    return status or "unknown"


def _status_from_rows(shot, *, candidate_count: int, retake_count: int,
                      latest_version_status: str | None, job,
                      provider_task_id: str | None, queue_position: int | None,
                      reference_progress: dict[str, int] | None, db) -> dict[str, Any]:
    """Build UI status from persisted job stage; fallback only when stage missing."""
    blocked_reason = None
    provider_elapsed_s = None
    pipeline_status = "idle"
    current_stage = None
    reason_code = None
    reason_text = None
    scheduler_lane = None
    stage_progress = None
    stage_started_at = None
    state_revision = 0
    attempt = 1
    attempt_limit = 3
    next_stage = None
    blocked_by_shot_id = None

    if job:
        persisted = read_job_pipeline(job)
        current_stage = persisted.get("pipeline_stage")
        reason_code = persisted.get("reason_code")
        reason_text = persisted.get("reason_text") or job["error"]
        scheduler_lane = persisted.get("scheduler_lane")
        stage_progress = persisted.get("stage_progress")
        stage_started_at = persisted.get("stage_started_at")
        state_revision = persisted.get("state_revision") or 0
        pipeline_status = _macro_status_from_job(job)

        # 缺持久化阶段时才回退推断（兼容旧任务）
        if not current_stage:
            status = job["status"]
            if status == S.WAITING_PROVIDER or provider_task_id:
                current_stage = S.STAGE_VIDEO_GENERATING
            elif status == "paused_budget":
                current_stage = S.STAGE_PAUSED_BUDGET
            elif status == S.WAITING_RETRY and not job["version_id"]:
                current_stage = S.STAGE_PREFLIGHT_RETRY
            elif status == S.WAITING_HUMAN and not job["version_id"]:
                current_stage = S.STAGE_PREFLIGHT_BLOCKED
            elif status == "running":
                current_stage = S.STAGE_VIDEO_DOWNLOADING if provider_task_id else S.STAGE_REFERENCE_GENERATE
            elif status == "queued":
                current_stage = S.STAGE_VIDEO_DOWNLOADING if provider_task_id else S.STAGE_JOB_QUEUED
            elif status == "failed":
                current_stage = S.STAGE_FAILED
            elif status == "succeeded":
                current_stage = S.STAGE_CANDIDATE_READY
            else:
                current_stage = S.STAGE_JOB_QUEUED

        # 有 task id 时禁止显示准备参考图
        if provider_task_id and current_stage in (
            S.STAGE_REFERENCE_PROMPT, S.STAGE_REFERENCE_GENERATE, S.STAGE_REFERENCE_QA,
            S.STAGE_REFERENCE_CONSISTENCY, S.STAGE_REFERENCE, S.STAGE_JOB_QUEUED,
            S.STAGE_WAITING_CONTINUITY, S.STAGE_WAITING_DEPENDENCY,
            S.STAGE_VIDEO_READY, S.STAGE_WAITING_VIDEO_SLOT,
        ):
            current_stage = S.STAGE_VIDEO_GENERATING if job["status"] == S.WAITING_PROVIDER else S.STAGE_VIDEO_DOWNLOADING

        if job["provider_submitted_at"]:
            provider_elapsed_s = max(0.0, now() - float(job["provider_submitted_at"]))

        if job["after_shot_id"] and current_stage in (
            S.STAGE_WAITING_CONTINUITY, S.STAGE_WAITING_DEPENDENCY,
        ):
            blocked_by_shot_id = job["after_shot_id"]
            from app.media_pipeline.scheduler import continuity_anchor_ready
            ready, reason = continuity_anchor_ready(
                db,
                job["after_shot_id"],
                require_adopted=current_stage == S.STAGE_WAITING_DEPENDENCY,
            )
            if not ready:
                pipeline_status = "waiting"
                blocked_reason = reason_text or reason
                if "人工" in (reason or ""):
                    pipeline_status = S.WAITING_HUMAN
                    current_stage = S.STAGE_WAITING_HUMAN

        if current_stage == S.STAGE_WAITING_HUMAN or pipeline_status == S.WAITING_HUMAN:
            pipeline_status = S.WAITING_HUMAN
        if reason_text and current_stage in (
            S.STAGE_WAITING_CONTINUITY,
            S.STAGE_WAITING_DEPENDENCY,
            S.STAGE_WAITING_VIDEO_SLOT,
        ):
            blocked_reason = reason_text

        # next_stage 粗映射
        _NEXT = {
            S.STAGE_PREFLIGHT_VALIDATING: S.STAGE_JOB_QUEUED,
            S.STAGE_PREFLIGHT_RETRY: S.STAGE_PREFLIGHT_VALIDATING,
            S.STAGE_JOB_QUEUED: S.STAGE_REFERENCE_PROMPT,
            S.STAGE_REFERENCE_PROMPT: S.STAGE_REFERENCE_GENERATE,
            S.STAGE_REFERENCE_GENERATE: S.STAGE_REFERENCE_QA,
            S.STAGE_REFERENCE_QA: S.STAGE_REFERENCE_CONSISTENCY,
            S.STAGE_REFERENCE_CONSISTENCY: S.STAGE_VIDEO_READY,
            S.STAGE_WAITING_CONTINUITY: S.STAGE_CONTINUITY_ASSEMBLING,
            S.STAGE_WAITING_DEPENDENCY: S.STAGE_VIDEO_READY,
            S.STAGE_CONTINUITY_ASSEMBLING: S.STAGE_VIDEO_READY,
            S.STAGE_VIDEO_READY: S.STAGE_VIDEO_SUBMITTING,
            S.STAGE_WAITING_VIDEO_SLOT: S.STAGE_VIDEO_SUBMITTING,
            S.STAGE_VIDEO_SUBMITTING: S.STAGE_VIDEO_GENERATING,
            S.STAGE_VIDEO_GENERATING: S.STAGE_VIDEO_DOWNLOADING,
            S.STAGE_VIDEO_DOWNLOADING: S.STAGE_VIDEO_TECHNICAL,
            S.STAGE_VIDEO_TECHNICAL: S.STAGE_VIDEO_QA,
            S.STAGE_VIDEO_QA: S.STAGE_CANDIDATE_READY,
        }
        next_stage = _NEXT.get(current_stage)

        try:
            # 轻量：不读巨型 image_inputs；retake_count 已由调用方传入
            attempt = max(1, int(retake_count) + 1)
            attempt_limit = 3
        except (TypeError, ValueError):
            pass

    elif candidate_count > 0 and not shot["adopted_version_id"]:
        pipeline_status = S.WAITING_HUMAN
        current_stage = S.STAGE_CANDIDATE_READY
        blocked_reason = "已有候选，等待人工采用"
    elif shot["adopted_version_id"]:
        pipeline_status = "adopted"
        current_stage = S.STAGE_ADOPTED

    # 参考图进度：优先持久化 stage_progress，否则用 reference_assets 统计
    if not stage_progress and reference_progress and current_stage in (
        S.STAGE_REFERENCE_GENERATE, S.STAGE_REFERENCE_QA, S.STAGE_REFERENCE,
    ):
        stage_progress = {
            "current": reference_progress["done"],
            "total": reference_progress["total"],
            "unit": "reference_slots",
        }

    label = stage_label(current_stage, progress=stage_progress, reason_text=blocked_reason or reason_text)
    if current_stage == S.STAGE_VIDEO_GENERATING and provider_elapsed_s is not None:
        display_elapsed_s = provider_elapsed_s
        if (stage_progress or {}).get("provider_phase") == "generating":
            try:
                generation_started_at = float(
                    stage_progress.get("provider_generation_started_at") or 0
                )
            except (TypeError, ValueError):
                generation_started_at = 0
            if generation_started_at > 0:
                display_elapsed_s = max(0.0, now() - generation_started_at)
        m, s = divmod(int(display_elapsed_s), 60)
        elapsed_kind = (
            "生成"
            if (stage_progress or {}).get("provider_phase") == "generating"
            else "已接单"
        )
        if stage_progress:
            label = f"{label} · {elapsed_kind} {m:02d}:{s:02d}"
        else:
            label = f"视频模型已接单 · {elapsed_kind} {m:02d}:{s:02d}"
    elif pipeline_status == "queued" and queue_position and current_stage == S.STAGE_WAITING_VIDEO_SLOT:
        label = f"视频输入已就绪，等待上游槽位（第 {queue_position} 位）"
    elif pipeline_status == "queued" and queue_position and not reason_text:
        label = f"排队中（第 {queue_position} 位）"

    stage_elapsed_s = None
    if stage_started_at:
        stage_elapsed_s = max(0.0, now() - float(stage_started_at))

    blocked_without_candidate = bool(job) and (
        job["status"] == S.WAITING_HUMAN
        or current_stage in (S.STAGE_PREFLIGHT_BLOCKED, S.STAGE_WAITING_HUMAN)
    )
    video_status = _video_status(
        has_adopted=bool(shot["adopted_version_id"]),
        has_active_job=(
            job is not None
            and job["status"] not in (S.PAUSED, S.WAITING_HUMAN)
        ),
        candidate_count=candidate_count,
        latest_version_status="failed" if blocked_without_candidate else latest_version_status,
    )

    return {
        # 这组字段用来明确回答「任务是否已下发」。只有持久化 job
        # 存在才算系统已受理；provider_submitted 只在上游返回 task id 后为真。
        # 前端不再根据「生成中」文案反推下发状态。
        "task_accepted": job is not None,
        "task_id": job["id"] if job else None,
        "task_created_at": job["created_at"] if job else None,
        "task_updated_at": job["updated_at"] if job else None,
        "next_retry_at": job["next_retry_at"] if job else None,
        "retry_count": int(job["retry_count"] or 0) if job else 0,
        "provider_submitted": bool(provider_task_id),
        "video_status": video_status,
        "video_status_label": VIDEO_STATUS_LABELS[video_status],
        "pipeline_status": pipeline_status,
        "pipeline_stage": current_stage,
        "current_stage": current_stage,  # 兼容旧字段
        "stage_label": label,
        "stage_progress": stage_progress,
        "queue_position": queue_position,
        "provider_elapsed_s": provider_elapsed_s,
        "stage_elapsed_s": stage_elapsed_s,
        "stage_started_at": stage_started_at,
        "reference_progress": reference_progress,
        "candidate_count": candidate_count,
        "retake_count": retake_count,
        "attempt": attempt,
        "attempt_limit": attempt_limit,
        "blocked_reason": blocked_reason,
        "reason_code": reason_code,
        "reason_text": reason_text or blocked_reason,
        "scheduler_lane": scheduler_lane,
        "blocked_by_shot_id": blocked_by_shot_id,
        "next_stage": next_stage,
        "state_revision": state_revision,
        # 不再输出看似精确的 ETA
        "estimated_start_at": None,
        "estimated_finish_at": None,
    }


def episode_pipeline_statuses(episode_id: str, *, conn=None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load all shot statuses for an episode with a fixed number of light queries."""
    db = conn or get_conn()
    shots = db.execute(
        """SELECT s.id, s.adopted_version_id
           FROM shots s
           WHERE s.episode_id=?
           ORDER BY s.shot_no""",
        (episode_id,),
    ).fetchall()
    if not shots:
        return {}, {
            "shots_total": 0, "adopted": 0, "with_candidate": 0,
            "upstream_generating": 0, "preparing_references": 0,
            "video_ready": 0, "waiting_continuity": 0, "video_qa": 0,
            "queued": 0, "waiting_human": 0, "failed": 0,
            "paused": 0,
            "video_status_counts": {
                status: 0 for status in VIDEO_STATUS_LABELS
            },
        }

    version_rows = db.execute(
        """SELECT v.shot_id,
                  SUM(CASE WHEN v.status='succeeded' AND v.video_path IS NOT NULL
                           AND v.video_path!='' THEN 1 ELSE 0 END) AS candidate_count,
                  MAX(v.version_no) AS latest_version_no,
                  (
                    SELECT latest.status
                    FROM shot_versions latest
                    WHERE latest.shot_id=v.shot_id
                    ORDER BY latest.version_no DESC
                    LIMIT 1
                  ) AS latest_version_status
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
             AND j.status IN (
               'queued','running','waiting_provider','paused_budget',
               'waiting_retry','waiting_human','paused'
             )
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
    preparing_refs = 0
    video_ready = 0
    waiting_continuity = 0
    video_qa = 0
    failed = 0
    paused = 0
    upstream = 0
    video_status_counts = {
        status: 0 for status in VIDEO_STATUS_LABELS
    }

    for s in shots:
        version = version_stats.get(s["id"])
        candidate_count = int(version["candidate_count"] or 0) if version else 0
        retake_count = max(0, int(version["latest_version_no"] or 1) - 1) if version else 0
        latest_version_status = version["latest_version_status"] if version else None
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
            latest_version_status=latest_version_status,
            job=job,
            provider_task_id=job["provider_task_id"] if job else None,
            queue_position=queue_positions.get(job["id"]) if job else None,
            reference_progress=reference_progress,
            db=db,
        )
        statuses[s["id"]] = st
        video_status_counts[st["video_status"]] += 1
        if st["video_status"] == VIDEO_STATUS_ADOPTED:
            adopted += 1
        if st.get("candidate_count", 0) > 0:
            with_candidate += 1
        ps = st.get("pipeline_status")
        stage = st.get("pipeline_stage") or st.get("current_stage")
        if ps == "queued":
            queued += 1
        elif ps == S.PAUSED:
            paused += 1
        elif ps in (S.WAITING_HUMAN, S.BLOCKED):
            waiting_human += 1
        if stage in (S.STAGE_REFERENCE_PROMPT, S.STAGE_REFERENCE_GENERATE, S.STAGE_REFERENCE_QA,
                     S.STAGE_REFERENCE_CONSISTENCY, S.STAGE_REFERENCE):
            preparing_refs += 1
        if stage in (S.STAGE_VIDEO_READY, S.STAGE_WAITING_VIDEO_SLOT):
            video_ready += 1
        if stage in (S.STAGE_WAITING_CONTINUITY, S.STAGE_WAITING_DEPENDENCY):
            waiting_continuity += 1
        if stage in (S.STAGE_VIDEO_QA, S.STAGE_VIDEO_TECHNICAL):
            video_qa += 1
        if stage == S.STAGE_VIDEO_GENERATING or ps == "waiting_provider":
            upstream += 1
        if stage == S.STAGE_FAILED or ps == "failed":
            failed += 1

    summary = {
        "shots_total": len(shots),
        "adopted": adopted,
        "with_candidate": with_candidate,
        "upstream_generating": upstream,
        "preparing_references": preparing_refs,
        "video_ready": video_ready,
        "waiting_continuity": waiting_continuity,
        "video_qa": video_qa,
        "queued": queued,
        "waiting_human": waiting_human,
        "failed": failed,
        "paused": paused,
        "video_status_counts": video_status_counts,
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
