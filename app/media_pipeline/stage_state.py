"""权威 pipeline_stage 读写：jobs 表为执行状态真相。"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, now
from app.media_pipeline import stages as S


def _row_has_column(conn, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def set_pipeline_stage(
    job_id: str,
    stage: str,
    *,
    pipeline_status: str | None = None,
    reason_code: str | None = None,
    reason_text: str | None = None,
    scheduler_lane: str | None = None,
    priority_class: str | None = None,
    stage_progress: dict[str, Any] | None = None,
    ready_at: float | None = None,
    conn=None,
) -> None:
    """在同一事务语义下更新 jobs 的阶段字段；缺列时静默跳过（旧库迁移前）。"""
    db = conn or get_conn()
    if not _row_has_column(db, "jobs", "pipeline_stage"):
        return
    stamp = now()
    progress_json = json.dumps(stage_progress, ensure_ascii=False) if stage_progress is not None else None
    # state_revision 单调递增
    row = db.execute(
        "SELECT state_revision, pipeline_stage FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row:
        return
    revision = int(row["state_revision"] or 0) + 1
    same_stage = row["pipeline_stage"] == stage
    sets = [
        "pipeline_stage=?",
        "stage_updated_at=?",
        "state_revision=?",
        "updated_at=?",
    ]
    args: list[Any] = [stage, stamp, revision, stamp]
    if not same_stage:
        sets.append("stage_started_at=?")
        args.append(stamp)
        sets.append("stage_status=?")
        args.append("active")
    if pipeline_status is not None and _row_has_column(db, "jobs", "pipeline_status"):
        # pipeline_status 列可选；宏观状态仍以 jobs.status 为主
        pass
    if reason_code is not None:
        sets.append("reason_code=?")
        args.append(reason_code)
    if reason_text is not None:
        sets.append("reason_text=?")
        args.append(reason_text)
    if scheduler_lane is not None:
        sets.append("scheduler_lane=?")
        args.append(scheduler_lane)
    if priority_class is not None:
        sets.append("priority_class=?")
        args.append(priority_class)
    if progress_json is not None:
        sets.append("stage_progress_json=?")
        args.append(progress_json)
    if ready_at is not None:
        sets.append("ready_at=?")
        args.append(ready_at)
    elif stage == S.STAGE_VIDEO_READY:
        sets.append("ready_at=COALESCE(ready_at, ?)")
        args.append(stamp)
    args.append(job_id)
    db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)
    if conn is None:
        db.commit()


def read_job_pipeline(job_row) -> dict[str, Any]:
    """从 job 行提取持久化阶段投影（缺列时返回空）。"""
    if job_row is None:
        return {}
    keys = set(job_row.keys()) if hasattr(job_row, "keys") else set()
    def _g(name: str, default=None):
        if name not in keys:
            return default
        return job_row[name]
    progress = None
    raw = _g("stage_progress_json")
    if raw:
        try:
            progress = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            progress = None
    stage = _g("pipeline_stage")
    if stage in S.LEGACY_STAGE_MAP:
        stage = S.LEGACY_STAGE_MAP[stage]
    return {
        "pipeline_stage": stage,
        "stage_status": _g("stage_status"),
        "stage_started_at": _g("stage_started_at"),
        "stage_updated_at": _g("stage_updated_at"),
        "stage_progress": progress,
        "reason_code": _g("reason_code"),
        "reason_text": _g("reason_text"),
        "scheduler_lane": _g("scheduler_lane"),
        "priority_class": _g("priority_class"),
        "ready_at": _g("ready_at"),
        "state_revision": int(_g("state_revision") or 0),
    }


def stage_label(stage: str | None, *, progress: dict | None = None, reason_text: str | None = None) -> str:
    if reason_text and stage in (
        S.STAGE_PREFLIGHT_RETRY, S.STAGE_PREFLIGHT_BLOCKED,
        S.STAGE_WAITING_CONTINUITY, S.STAGE_WAITING_VIDEO_SLOT,
        S.STAGE_WAITING_HUMAN, S.STAGE_FAILED,
    ):
        return reason_text
    label = S.PIPELINE_STAGE_LABELS.get(stage or "", stage or "未知阶段")
    if stage == S.STAGE_REFERENCE_GENERATE and progress:
        cur = progress.get("current")
        total = progress.get("total")
        if cur is not None and total:
            return f"生成参考图 {cur}/{total}"
    if stage == S.STAGE_REFERENCE_QA and progress:
        cur = progress.get("current")
        total = progress.get("total")
        if cur is not None and total:
            return f"参考图质检 {cur}/{total}"
    if stage == S.STAGE_REFERENCE_PROMPT and progress:
        total = progress.get("total") or 4
        return f"编写 {total} 张参考图提示词"
    if stage == S.STAGE_AUTO_RETAKE and progress:
        attempt = progress.get("attempt")
        limit = progress.get("attempt_limit")
        if attempt is not None and limit is not None:
            return f"首轮 QA 未通过，自动重抽 {attempt}/{limit}"
    if stage and stage not in S.PIPELINE_STAGE_LABELS and stage not in S.PIPELINE_STAGES:
        return f"未知阶段（{stage}）"
    return label or "未知阶段"
