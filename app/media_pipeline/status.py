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


def shot_pipeline_status(shot_id: str, *, conn=None) -> dict[str, Any]:
    db = conn or get_conn()
    shot = db.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return {}

    versions = db.execute(
        "SELECT * FROM shot_versions WHERE shot_id=? ORDER BY version_no DESC",
        (shot_id,),
    ).fetchall()
    candidate_count = sum(
        1 for v in versions if v["status"] == "succeeded" and v["video_path"]
    )
    retake_count = 0
    for v in versions:
        meta = json.loads(v["image_inputs"] or "{}")
        retake_count = max(retake_count, int(meta.get("auto_retake_count") or 0))

    job = db.execute(
        """SELECT * FROM jobs
           WHERE shot_id=? AND kind='video'
             AND status IN ('queued','running','waiting_provider','paused_budget','waiting_retry')
             AND cancellation_requested=0 AND abandoned=0
           ORDER BY created_at DESC LIMIT 1""",
        (shot_id,),
    ).fetchone()

    blocked_reason = None
    queue_position = None
    provider_elapsed_s = None
    reference_progress = None
    estimated_start_at = None
    estimated_finish_at = None
    pipeline_status = "idle"
    current_stage = None

    if job:
        version = db.execute(
            "SELECT * FROM shot_versions WHERE id=?", (job["version_id"],)
        ).fetchone()
        pipeline_status, current_stage = _job_stage(job, version)
        if job["status"] == "queued":
            earlier = db.execute(
                """SELECT COUNT(*) c FROM jobs
                   WHERE kind='video' AND status='queued'
                     AND cancellation_requested=0 AND abandoned=0
                     AND (next_retry_at IS NULL OR next_retry_at<=?)
                     AND created_at < ?""",
                (now(), job["created_at"]),
            ).fetchone()["c"]
            queue_position = int(earlier) + 1
        if job["provider_submitted_at"]:
            provider_elapsed_s = max(0.0, now() - float(job["provider_submitted_at"]))
        if job["error"] and "等待" in (job["error"] or ""):
            blocked_reason = job["error"]
        # 参考图进度：已选中数 / 生成数
        if version:
            meta = json.loads(version["image_inputs"] or "{}")
            refs = [r for r in (meta.get("reference_images") or []) if not r.get("deleted")]
            selected = [r for r in refs if r.get("selectedForSeedance", True)]
            if refs:
                reference_progress = {"done": len(selected), "total": max(len(refs), 1)}
        # ETA
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
        if job["after_shot_id"] and job["status"] == "queued" and not (
            version and version["provider_task_id"]
        ):
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


def episode_pipeline_summary(episode_id: str, *, conn=None) -> dict[str, Any]:
    db = conn or get_conn()
    shots = db.execute(
        "SELECT id, adopted_version_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    adopted = 0
    with_candidate = 0
    upstream = 0
    preparing_refs = 0
    queued = 0
    waiting_human = 0
    for s in shots:
        st = shot_pipeline_status(s["id"], conn=db)
        if s["adopted_version_id"]:
            adopted += 1
        if st.get("candidate_count", 0) > 0 or s["adopted_version_id"]:
            with_candidate += 1
        ps = st.get("pipeline_status")
        stage = st.get("current_stage")
        if ps == S.WAITING_PROVIDER:
            upstream += 1
        elif ps == "running" and stage == S.STAGE_REFERENCE:
            preparing_refs += 1
        elif ps == "queued":
            queued += 1
        elif ps in (S.WAITING_HUMAN, S.BLOCKED):
            waiting_human += 1
    # 再补上游：running 且已提交
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
    return {
        "shots_total": len(shots),
        "adopted": adopted,
        "with_candidate": with_candidate,
        "upstream_generating": upstream,
        "preparing_references": preparing_refs,
        "queued": queued,
        "waiting_human": waiting_human,
    }
