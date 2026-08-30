"""连续性死锁解除与本次补齐在途任务的冻结/释放。"""
from __future__ import annotations

import json

from typing import Any

from app.db import get_conn, now
from app.harness.types import Issue, IssueSeverity
from app.video_issues import persist_shot_issue



def _reconcile_terminal_continuity_blocks(
    episode_id: str,
    *,
    run_id: str | None = None,
) -> int:
    """把不可能再获得上游尾帧的等待任务转成可路由 Issue。

    queued + waiting_continuity 过去会永远被当成 active，Supervisor 因而永远
    不会进入 L3 降链。这里只在上游既无技术候选、也无活动任务时解除死锁。
    """
    from app.media_pipeline import stages as media_stages

    conn = get_conn()
    rows = conn.execute(
        """SELECT j.id, j.shot_id, j.version_id, j.after_shot_id, s.shot_no
           FROM jobs j JOIN shots s ON s.id=j.shot_id
           WHERE j.episode_id=? AND j.kind='video'
             AND s.adopted_version_id IS NULL
             AND j.status IN ('queued','waiting','waiting_retry')
             AND j.after_shot_id IS NOT NULL
             AND j.pipeline_stage=?
             AND (? IS NULL OR j.owner_run_id=?)""",
        (episode_id, media_stages.STAGE_WAITING_CONTINUITY, run_id, run_id),
    ).fetchall()
    changed = 0
    for row in rows:
        upstream_rows = conn.execute(
            """SELECT technical_validation_json FROM shot_versions
               WHERE shot_id=? AND status='succeeded'""",
            (row["after_shot_id"],),
        ).fetchall()
        upstream_candidate = False
        for candidate in upstream_rows:
            try:
                if json.loads(candidate["technical_validation_json"] or "{}").get("passed"):
                    upstream_candidate = True
                    break
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        upstream_active = conn.execute(
            """SELECT 1 FROM jobs
               WHERE shot_id=? AND kind='video'
                 AND status IN ('queued','running','waiting_provider','waiting_retry','waiting')
                 AND (? IS NULL OR owner_run_id=?)
               LIMIT 1""",
            (row["after_shot_id"], run_id, run_id),
        ).fetchone()
        if upstream_candidate or upstream_active:
            continue
        message = "上一镜无可用尾帧且已无活动任务；解除连续性等待，交由 Supervisor 降链修复"
        cursor = conn.execute(
            """UPDATE jobs
               SET status='waiting_human', pipeline_stage=?, stage_status='blocked',
                   reason_code='VIDEO_CHAIN_ANCHOR_BLOCKED', reason_text=?, error=?,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?, stage_updated_at=?
               WHERE id=? AND status IN ('queued','waiting','waiting_retry')""",
            (media_stages.STAGE_WAITING_HUMAN, message, message, now(), now(), row["id"]),
        )
        if cursor.rowcount != 1:
            continue
        if row["version_id"]:
            conn.execute(
                """UPDATE shot_versions SET status='waiting_human', error=?
                   WHERE id=? AND status IN ('queued','running','waiting_retry')""",
                (message, row["version_id"]),
            )
        conn.commit()
        persist_shot_issue(
            episode_id=episode_id,
            shot_id=row["shot_id"],
            shot_no=int(row["shot_no"]),
            issues=[Issue(
                code="VIDEO_CHAIN_ANCHOR_BLOCKED",
                severity=IssueSeverity.BLOCKER,
                subject=row["shot_id"],
                message=message,
                evidence={
                    "shot_no": int(row["shot_no"]),
                    "path": str(row["shot_no"]),
                    "rule_id": "chain_anchor",
                    "job_id": row["id"],
                },
                repair_hint="取消尾帧依赖并按独立首帧重建本镜",
            )],
            source="supervisor_continuity_reconcile",
            run_id=run_id,
        )
        changed += 1
    if changed:
        from app.observability.metrics import inc
        inc("video_continuity_anchor_blocked_total", value=changed, episode_id=episode_id)
    return changed


def _stop_supervised_video_jobs(episode_id: str, *, run_id: str | None, reason: str) -> list[dict[str, Any]]:
    """冻结本次补齐仍在活动/阻塞的媒体任务；重复调用安全。"""
    from app.orchestration import media_scheduler

    conn = get_conn()
    rows = conn.execute(
        """SELECT j.id FROM jobs j
           JOIN shots s ON s.id=j.shot_id
           WHERE j.episode_id=? AND j.kind='video'
             AND s.adopted_version_id IS NULL
             AND j.status IN (
               'queued','running','waiting_provider','waiting_retry','waiting',
               'waiting_human','paused_budget'
             )""",
        (episode_id,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            results.append(media_scheduler.request_cancel(row["id"], reason=reason))
        except Exception as exc:  # noqa: BLE001 — 逐任务 best effort，其余任务仍必须停止
            results.append({"job_id": row["id"], "cancelled": False, "error": str(exc)})
    media_scheduler.reconcile_cancelled_version_states(episode_id=episode_id)
    return results


def _release_episode_supervisor(episode_id: str, *, run_id: str | None) -> None:
    conn = get_conn()
    if run_id:
        conn.execute(
            """UPDATE episodes
               SET video_completion_mode='quick', active_video_run_id=NULL,
                   status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
               WHERE id=? AND (active_video_run_id=? OR active_video_run_id IS NULL)""",
            (episode_id, run_id),
        )
    else:
        conn.execute(
            """UPDATE episodes
               SET video_completion_mode='quick', active_video_run_id=NULL,
                   status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
               WHERE id=?""",
            (episode_id,),
        )
    conn.commit()
