"""公平调度：优先级 + 老化 + 配额 + 连续镜依赖 + QPSP 水位/cohort。"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, now
from app.media_pipeline import stages as S
from app.media_pipeline.retry_policy import (
    episode_inflight_cap,
    first_pass_retake_slot_fraction,
    prepared_reference_backlog,
    project_inflight_cap,
    reference_shot_cohort_limit,
    scheduler_policy,
    video_ready_high_watermark,
    video_ready_low_watermark,
)


def continuity_anchor_ready(
    conn,
    after_shot_id: str | None,
    *,
    require_adopted: bool = False,
) -> tuple[bool, str | None]:
    """连续镜是否已有可用尾帧锚点。

    返回 (ready, blocked_reason)。
    - 有 adopted 且技术合格 → ready
    - 有 succeeded 技术合格候选（provisional）→ ready
    - 有活跃任务 → 等待
    - 已失败且无候选 → waiting_human
    """
    if not after_shot_id:
        return True, None
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (after_shot_id,)).fetchone()
    if not shot:
        return False, "上一镜不存在"

    def _tech_ok(version_id: str) -> bool:
        v = conn.execute(
            "SELECT status, video_path, technical_validation_json FROM shot_versions WHERE id=?",
            (version_id,),
        ).fetchone()
        if not v or v["status"] != "succeeded" or not v["video_path"]:
            return False
        tech = json.loads(v["technical_validation_json"] or "{}")
        # 无技术校验记录时视为可暂用（兼容旧数据）
        return bool(tech.get("passed", True))

    if shot["adopted_version_id"] and _tech_ok(shot["adopted_version_id"]):
        return True, None
    if require_adopted:
        active = conn.execute(
            """SELECT COUNT(*) c FROM jobs
               WHERE shot_id=? AND kind='video'
                 AND status IN ('queued','running','waiting_provider','waiting_retry')
                 AND cancellation_requested=0 AND abandoned=0""",
            (after_shot_id,),
        ).fetchone()["c"]
        if active:
            return False, "等待上一镜生成并采用"
        return False, "上一镜尚未采用可用视频，待人工处理"

    candidates = conn.execute(
        """SELECT id FROM shot_versions
           WHERE shot_id=? AND status='succeeded' AND video_path IS NOT NULL
           ORDER BY version_no DESC""",
        (after_shot_id,),
    ).fetchall()
    for c in candidates:
        if _tech_ok(c["id"]):
            return True, None  # provisional best candidate

    active = conn.execute(
        """SELECT COUNT(*) c FROM jobs
           WHERE shot_id=? AND kind='video'
             AND status IN ('queued','running','waiting_provider','waiting_retry')
             AND cancellation_requested=0 AND abandoned=0""",
        (after_shot_id,),
    ).fetchone()["c"]
    if active:
        return False, "等待上一镜生成完成"

    if candidates:
        return False, "上一镜候选未通过技术校验，待人工处理"
    return False, "上一镜尚无可用尾帧，待人工处理"


def count_inflight_videos(*, episode_id: str | None = None, project_id: str | None = None) -> int:
    conn = get_conn()
    if episode_id:
        return int(conn.execute(
            """SELECT COUNT(*) c FROM jobs
               WHERE episode_id=? AND kind='video'
                 AND status IN ('running','waiting_provider')
                 AND provider_non_cancellable=1
                 AND cancellation_requested=0 AND abandoned=0""",
            (episode_id,),
        ).fetchone()["c"])
    if project_id:
        return int(conn.execute(
            """SELECT COUNT(*) c FROM jobs
               WHERE project_id=? AND kind='video'
                 AND status IN ('running','waiting_provider')
                 AND provider_non_cancellable=1
                 AND cancellation_requested=0 AND abandoned=0""",
            (project_id,),
        ).fetchone()["c"])
    return int(conn.execute(
        """SELECT COUNT(*) c FROM jobs
           WHERE kind='video'
             AND status IN ('running','waiting_provider')
             AND provider_non_cancellable=1
             AND cancellation_requested=0 AND abandoned=0""",
    ).fetchone()["c"])


def _parse_meta(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _is_auto_retake_meta(meta: dict[str, Any]) -> bool:
    return int(meta.get("auto_retake_count") or 0) > 0


def count_inflight_auto_retakes(episode_id: str, *, conn=None) -> int:
    """本集真正占用上游视频槽的自动重抽数（与 count_inflight_videos 同口径）。

    不含 queued / waiting_video_slot：本地排队不算在途，避免互相自锁。
    """
    db = conn or get_conn()
    rows = db.execute(
        """SELECT v.image_inputs FROM jobs j
           JOIN shot_versions v ON v.id=j.version_id
           WHERE j.episode_id=? AND j.kind='video'
             AND j.status IN ('running','waiting_provider')
             AND j.provider_non_cancellable=1
             AND j.cancellation_requested=0 AND j.abandoned=0""",
        (episode_id,),
    ).fetchall()
    count = 0
    for row in rows:
        if _is_auto_retake_meta(_parse_meta(row["image_inputs"])):
            count += 1
    return count


def episode_first_pass_incomplete(episode_id: str, *, conn=None) -> bool:
    """是否仍有镜头没有任何成功候选视频。"""
    db = conn or get_conn()
    row = db.execute(
        """SELECT COUNT(*) c FROM shots s
           WHERE s.episode_id=?
             AND NOT EXISTS (
               SELECT 1 FROM shot_versions v
               WHERE v.shot_id=s.id AND v.status='succeeded' AND v.video_path IS NOT NULL
             )""",
        (episode_id,),
    ).fetchone()
    return int(row["c"] or 0) > 0


def can_admit_video_submit(
    *,
    episode_id: str,
    project_id: str,
    is_auto_retake: bool,
) -> tuple[bool, str | None]:
    """视频提交准入：全局/项目/剧集配额 + 首轮优先时重抽限额。"""
    from app.media_pipeline.concurrency import channel_limit

    global_cap = channel_limit(S.RESOURCE_VIDEO_INFLIGHT)
    if count_inflight_videos() >= global_cap:
        return False, "全局上游视频槽位已满"
    if count_inflight_videos(project_id=project_id) >= project_inflight_cap():
        return False, "项目上游视频槽位已满"
    if count_inflight_videos(episode_id=episode_id) >= episode_inflight_cap():
        return False, "本集上游视频槽位已满"

    if is_auto_retake and episode_first_pass_incomplete(episode_id):
        # 重抽最多占上游在途槽的 25%；只计真正已交上游的重抽
        inflight = count_inflight_videos(episode_id=episode_id)
        retake_cap = max(1, int(episode_inflight_cap() * first_pass_retake_slot_fraction()))
        retake_inflight = count_inflight_auto_retakes(episode_id)
        if retake_inflight >= retake_cap:
            return False, "首轮未覆盖完成，自动重抽槽位已满"
        # 额外：若在途已接近满且重抽会挤占，也拒绝
        if inflight >= global_cap:
            return False, "上游槽位已满，优先首轮覆盖"
    return True, None


def claim_video_submit_slot(
    *,
    job_id: str,
    lease_owner: str,
    episode_id: str,
    project_id: str,
    version_id: str,
    operation_id: str,
    amount_cny: float,
    is_auto_retake: bool,
    conn=None,
) -> tuple[bool, str | None]:
    """Atomically claim inflight capacity and the payable provider budget.

    The advisory scheduler check deliberately remains cheap.  This is the
    authoritative submit-side fence: SQLite's write lock serializes concurrent
    workers before any of them can make a provider create call.
    """
    from app.completion_grant import reserve_provider_video_budget
    from app.media_pipeline.concurrency import channel_limit

    db = conn or get_conn()
    if db.in_transaction:
        raise RuntimeError("video inflight claim requires a clean transaction boundary")

    def count(where: str = "", args: tuple[Any, ...] = ()) -> int:
        return int(db.execute(
            """SELECT COUNT(*) AS c FROM jobs
                 WHERE kind='video'
                   AND status IN ('running','waiting_provider')
                   AND provider_non_cancellable=1
                   AND cancellation_requested=0 AND abandoned=0"""
            + where,
            args,
        ).fetchone()["c"])

    try:
        db.execute("BEGIN IMMEDIATE")
        owned = db.execute(
            """SELECT provider_non_cancellable,provider_create_state
                 FROM jobs
                WHERE id=? AND episode_id=? AND project_id=? AND version_id=?
                  AND kind='video' AND status='running' AND lease_owner=?
                  AND cancellation_requested=0 AND abandoned=0""",
            (job_id, episode_id, project_id, version_id, lease_owner),
        ).fetchone()
        if owned is None:
            raise ValueError("视频提交槽位 claim 已失去 job lease 或 scope")
        if bool(owned["provider_non_cancellable"]):
            db.commit()
            return True, None

        limits = (
            (count(), channel_limit(S.RESOURCE_VIDEO_INFLIGHT), "全局上游视频槽位已满"),
            (count(" AND project_id=?", (project_id,)), project_inflight_cap(), "项目上游视频槽位已满"),
            (count(" AND episode_id=?", (episode_id,)), episode_inflight_cap(), "本集上游视频槽位已满"),
        )
        for current, cap, reason in limits:
            if current >= cap:
                db.rollback()
                return False, reason

        if is_auto_retake and episode_first_pass_incomplete(episode_id, conn=db):
            retake_cap = max(
                1,
                int(episode_inflight_cap() * first_pass_retake_slot_fraction()),
            )
            if count_inflight_auto_retakes(episode_id, conn=db) >= retake_cap:
                db.rollback()
                return False, "首轮未覆盖完成，自动重抽槽位已满"

        if reserve_provider_video_budget(
            episode_id=episode_id,
            job_id=job_id,
            version_id=version_id,
            operation_id=operation_id,
            amount_cny=amount_cny,
            conn=db,
        ) is not True:
            db.rollback()
            return False, "VIDEO_BUDGET_NOT_AUTHORIZED"
        marked = db.execute(
            """UPDATE jobs
                  SET provider_non_cancellable=1,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                  AND provider_create_state='submitting'
                  AND provider_non_cancellable=0
                  AND cancellation_requested=0 AND abandoned=0""",
            (now(), job_id, lease_owner),
        )
        if marked.rowcount != 1:
            raise ValueError("视频提交槽位 claim 发生 lease/CAS 冲突")
        db.commit()
        return True, None
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def is_true_video_ready(meta: dict[str, Any], *, continuity_ok: bool) -> bool:
    """真正可提交 Seedance 的就绪条件（PRD §4.1）。"""
    if meta.get("video_input_manifest_frozen"):
        return continuity_ok and bool(meta.get("reference_images"))
    # 兼容旧路径：完整参考图完成 + 非连续或连续锚点已就绪
    if not meta.get("reference_images"):
        return False
    if meta.get("reference_generation_complete") is False:
        return False
    if meta.get("reference_static_ready") and not meta.get("reference_generation_complete"):
        # 仅静态就绪、等尾帧 → 未真正 VIDEO_READY
        return False
    return continuity_ok


def count_true_video_ready_not_submitted(
    *,
    episode_id: str | None = None,
    conn=None,
    exclude_auto_retakes: bool = False,
) -> int:
    """统计已 VIDEO_READY 但尚未获得 provider_task_id 的镜头数。"""
    db = conn or get_conn()
    sql = """SELECT j.id, j.after_shot_id, v.image_inputs, v.provider_task_id, j.pipeline_stage
             FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
             WHERE j.kind='video'
               AND j.status IN ('queued','running')
               AND j.cancellation_requested=0 AND j.abandoned=0
               AND (v.provider_task_id IS NULL OR v.provider_task_id='')"""
    args: list[Any] = []
    if episode_id:
        sql += " AND j.episode_id=?"
        args.append(episode_id)
    rows = db.execute(sql, args).fetchall()
    count = 0
    cache: dict[str, bool] = {}
    for row in rows:
        meta = _parse_meta(row["image_inputs"])
        if exclude_auto_retakes and _is_auto_retake_meta(meta):
            continue
        if row["pipeline_stage"] == S.STAGE_VIDEO_READY:
            count += 1
            continue
        after = row["after_shot_id"]
        if after:
            if after not in cache:
                cache[after] = continuity_anchor_ready(db, after)[0]
            ok = cache[after]
        else:
            ok = True
        if is_true_video_ready(meta, continuity_ok=ok):
            count += 1
    return count


def count_active_reference_cohorts(*, episode_id: str | None = None, conn=None) -> int:
    """正在占用参考图/图片通道的镜头数（running 且无 provider_task_id）。"""
    db = conn or get_conn()
    sql = """SELECT COUNT(*) c FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
             WHERE j.kind='video' AND j.status='running'
               AND (v.provider_task_id IS NULL OR v.provider_task_id='')
               AND j.cancellation_requested=0 AND j.abandoned=0
               AND COALESCE(j.pipeline_stage,'') NOT IN (?, ?, ?, ?, ?, ?)"""
    args: list[Any] = [
        S.STAGE_VIDEO_READY,
        S.STAGE_WAITING_VIDEO_SLOT,
        S.STAGE_VIDEO_SUBMITTING,
        S.STAGE_VIDEO_GENERATING,
        S.STAGE_VIDEO_DOWNLOADING,
        S.STAGE_VIDEO_QA,
    ]
    if episode_id:
        sql += " AND j.episode_id=?"
        args.append(episode_id)
    return int(db.execute(sql, args).fetchone()["c"])


def continuity_chain_remaining(conn, episode_id: str, shot_id: str) -> int:
    """当前镜头之后还有多少依赖其尾帧的连续镜（简化：按 after_shot_id 链长度）。"""
    remaining = 0
    current = shot_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        row = conn.execute(
            """SELECT shot_id FROM jobs
               WHERE episode_id=? AND kind='video' AND after_shot_id=?
                 AND cancellation_requested=0 AND abandoned=0
               ORDER BY created_at DESC LIMIT 1""",
            (episode_id, current),
        ).fetchone()
        if not row:
            # 也查 shots 表的连续性字段
            nxt = conn.execute(
                """SELECT id FROM shots
                   WHERE episode_id=? AND continuity_from_prev=1
                     AND shot_no=(SELECT shot_no+1 FROM shots WHERE id=?)""",
                (episode_id, current),
            ).fetchone()
            if not nxt:
                break
            current = nxt["id"]
            remaining += 1
            continue
        current = row["shot_id"]
        remaining += 1
    return remaining


def job_scheduler_score(
    *,
    first_pass: bool,
    continuity_remaining: int,
    completed_slots: int,
    wait_age_minutes: float,
    auto_retake: bool,
) -> float:
    """同队列内排序分（PRD §4.3）；不取代硬优先级。"""
    return (
        1000.0 * (1 if first_pass else 0)
        + 100.0 * continuity_remaining
        + 20.0 * completed_slots
        + min(wait_age_minutes, 30.0)
        - 300.0 * (1 if auto_retake else 0)
    )


def should_start_more_reference_work(*, episode_id: str | None = None, conn=None) -> tuple[bool, int]:
    """是否允许启动新的普通参考图镜头；返回 (allow, demand_slots)。"""
    db = conn or get_conn()
    if scheduler_policy() == "legacy":
        return True, prepared_reference_backlog()
    # 首轮未覆盖时，排队/卡住的自动重抽不计入水位，避免压制未覆盖镜开工
    exclude_retakes = bool(episode_id) and episode_first_pass_incomplete(episode_id, conn=db)
    ready = count_true_video_ready_not_submitted(
        episode_id=episode_id, conn=db, exclude_auto_retakes=exclude_retakes,
    )
    high = video_ready_high_watermark()
    low = video_ready_low_watermark()
    if ready >= high:
        return False, 0
    # 视频槽未满时尽量补到高水位；已满时只在低于低水位时补
    from app.media_pipeline.concurrency import channel_limit
    inflight = count_inflight_videos(episode_id=episode_id) if episode_id else count_inflight_videos()
    cap = episode_inflight_cap() if episode_id else channel_limit(S.RESOURCE_VIDEO_INFLIGHT)
    slots_full = inflight >= cap
    if slots_full and ready >= low:
        # 保持缓冲，不扩张
        demand = max(0, high - ready) if ready < low else 0
        return demand > 0, demand
    demand = max(0, high - ready)
    active = count_active_reference_cohorts(episode_id=episode_id, conn=db)
    cohort_cap = reference_shot_cohort_limit()
    room = max(0, cohort_cap - active)
    return room > 0 and demand > 0, min(room, demand)


def classify_scheduler_lane(
    *,
    refs_ready: bool,
    continuity_ok: bool,
    is_retake: bool,
    static_ready_waiting: bool,
    critical_path: bool,
) -> str:
    if refs_ready and continuity_ok:
        return S.LANE_VIDEO_READY if not is_retake else S.LANE_RETAKE
    if is_retake:
        return S.LANE_RETAKE
    if critical_path or static_ready_waiting:
        return S.LANE_REFERENCE_CRITICAL
    return S.LANE_REFERENCE_NORMAL


__all__ = [
    "continuity_anchor_ready",
    "count_inflight_videos",
    "count_inflight_auto_retakes",
    "episode_first_pass_incomplete",
    "can_admit_video_submit",
    "is_true_video_ready",
    "count_true_video_ready_not_submitted",
    "count_active_reference_cohorts",
    "continuity_chain_remaining",
    "job_scheduler_score",
    "should_start_more_reference_work",
    "classify_scheduler_lane",
    "scheduler_policy",
    "video_ready_high_watermark",
    "video_ready_low_watermark",
    "reference_shot_cohort_limit",
    "prepared_reference_backlog",
    "now",
]
