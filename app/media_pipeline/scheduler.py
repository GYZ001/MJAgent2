"""公平调度：优先级 + 老化 + 配额 + 连续镜依赖。"""
from __future__ import annotations

import json

from app.db import get_conn
from app.media_pipeline import stages as S
from app.media_pipeline.retry_policy import (
    episode_inflight_cap,
    first_pass_retake_slot_fraction,
    project_inflight_cap,
)


def continuity_anchor_ready(conn, after_shot_id: str | None) -> tuple[bool, str | None]:
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


def episode_first_pass_incomplete(episode_id: str) -> bool:
    """是否仍有镜头没有任何成功候选视频。"""
    conn = get_conn()
    row = conn.execute(
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
        # 重抽最多占 25% 在途槽
        inflight = count_inflight_videos(episode_id=episode_id)
        retake_cap = max(1, int(episode_inflight_cap() * first_pass_retake_slot_fraction()))
        # 统计本集自动重抽在途
        conn = get_conn()
        retake_inflight = 0
        rows = conn.execute(
            """SELECT v.image_inputs FROM jobs j
               JOIN shot_versions v ON v.id=j.version_id
               WHERE j.episode_id=? AND j.kind='video'
                 AND j.status IN ('running','waiting_provider','queued')
                 AND j.cancellation_requested=0 AND j.abandoned=0""",
            (episode_id,),
        ).fetchall()
        for r in rows:
            meta = json.loads(r["image_inputs"] or "{}")
            if int(meta.get("auto_retake_count") or 0) > 0:
                retake_inflight += 1
        if retake_inflight >= retake_cap:
            return False, "首轮未覆盖完成，自动重抽槽位已满"
        # 额外：若在途已接近满且重抽会挤占，也拒绝
        if inflight >= global_cap:
            return False, "上游槽位已满，优先首轮覆盖"
    return True, None
