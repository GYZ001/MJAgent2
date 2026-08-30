from __future__ import annotations

from app.db import get_conn, now
from app.orchestration import media_scheduler


def decommission_legacy_keyframe_jobs() -> int:
    """取消升级前遗留的关键帧任务并清掉镜头的假运行状态。

    关键帧候选不再属于视频生成链路。已完成的历史图片暂不删除，便于审计；
    但 queued/running/paused_budget 任务必须停止，且不能被启动恢复重新入队。
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT id FROM jobs
           WHERE kind='scene' AND status IN ('queued','running','paused_budget')"""
    ).fetchall()
    for row in rows:
        try:
            media_scheduler.request_cancel(row["id"])
        except Exception:  # noqa: BLE001 兼容旧库里缺少编排审计记录的任务
            conn.execute(
                """UPDATE jobs SET status='cancelled', cancellation_requested=1,
                          lease_owner=NULL, lease_expires_at=NULL, reserved_cost_cny=0,
                          error=?, updated_at=? WHERE id=?""",
                ("关键帧功能已下线；请从参考图视频入口重新生成", now(), row["id"]),
            )
            conn.execute(
                """UPDATE budget_reservations SET status='released', settled_at=?,
                          actual_cost_cny=0 WHERE job_id=?""",
                (now(), row["id"]),
            )
    conn.execute("UPDATE shots SET scene_status='none' WHERE scene_status!='none'")
    conn.commit()
    return len(rows)

__all__ = [name for name in globals() if not name.startswith("__")]
