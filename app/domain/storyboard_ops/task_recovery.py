"""启动时恢复孤儿分镜生成任务。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 task_run。
"""
from __future__ import annotations

from app import (
    errors,
    task_registry,
)
from app.db import get_conn

from .task_run import (
    _new_storyboard_recorder,
    _storyboard_guarded_recorded,
)


def recover_storyboard_tasks() -> int:
    """恢复被服务重启中断的分镜任务，不接管用户主动暂停的 Run。"""
    from app.generation_concurrency import PRIORITY_RECOVERY

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM episodes "
        "WHERE status='scripting' AND screenplay_status='ready' "
        "AND screenplay_json IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM projects p -- ALL_OWNERS: startup recovery scans "
        "every owner's episodes for orphaned running storyboard tasks "
        "after a process reload/restart; excludes soft-deleted "
        "(recycle-bin) projects so their residual tasks are not resumed\n"
        "WHERE p.id=episodes.project_id AND p.deleted_at IS NOT NULL"
        ")"
    ).fetchall()
    resumed = 0
    for row in rows:
        episode_id = row["id"]
        if task_registry.active("storyboard", episode_id):
            continue
        latest = conn.execute(
            """SELECT id,status,failure_code FROM workflow_runs
               WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
               ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        if latest:
            if latest["status"] in {"CREATED", "RUNNING"}:
                # A durable run may belong to another live service instance.
                continue
            if latest["status"] != "PAUSED_EXTERNAL" or latest["failure_code"] != "SERVICE_RESTART":
                # PARTIAL / WAITING_HUMAN / user_pause are explicit manual resume points.
                continue
            parent = latest
        else:
            # Legacy databases may have only the projection state and no run ledger.
            parent = None
        recorder = None
        try:
            if row["storyboard_outline_json"]:
                from app.production.screenplay_authority import (
                    resolve_downstream_screenplay,
                )
                from app.storyboard_authority import (
                    resolve_storyboard_outline_authority,
                )

                screenplay_context = resolve_downstream_screenplay(
                    episode_id,
                    conn=conn,
                )
                if screenplay_context.narrative_authority_required:
                    resolve_storyboard_outline_authority(
                        episode_id,
                        conn=conn,
                    )
            recorder = _new_storyboard_recorder(
                episode_id, resume=True,
                requested_by="system", trigger_type="resume",
                parent_run_id=parent["id"] if parent else None,
            )
            installed = conn.execute(
                "UPDATE episodes SET active_storyboard_run_id=? "
                "WHERE id=? AND status='scripting' AND active_storyboard_run_id IS ?",
                (recorder.run_id, episode_id, row["active_storyboard_run_id"]),
            )
            if installed.rowcount != 1:
                conn.rollback()
                recorder.cancel("分镜恢复启动权已变化，当前运行未启动", conn=None)
                continue
            conn.commit()
            task_registry.spawn(
                "storyboard", episode_id,
                _storyboard_guarded_recorded(
                    episode_id,
                    recorder,
                    resume=True,
                    new_activation=False,
                    priority=PRIORITY_RECOVERY,
                ),
                project_id=row["project_id"],
            )
            resumed += 1
        except Exception as exc:  # noqa: BLE001 - one bad episode must not block startup
            public = errors.record_and_format(
                exc,
                action="storyboard_recovery_spawn",
                context={"episode_id": episode_id, "previous_run_id": row["active_storyboard_run_id"]},
            )
            from app.storyboard_supervisor import load_latest_checkpoint
            checkpoint = load_latest_checkpoint(episode_id)
            shot_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
            ).fetchone()["c"])
            recoverable = bool(shot_count or (checkpoint and checkpoint.validated_prefix_end > 0))
            conn.execute(
                "UPDATE episodes SET status=?, script_error=?, active_storyboard_run_id=NULL WHERE id=?",
                (
                    "script_failed" if recoverable else "planned",
                    (
                        f"服务重启后的分镜恢复未能启动；"
                        f"{'已通过镜头和恢复点均已保留，可点击继续分镜' if recoverable else '剧本已保留，可重新生成分镜'}。"
                        f"{public}"
                    ),
                    episode_id,
                ),
            )
            conn.commit()
            if recorder is not None:
                try:
                    recorder.cancel("分镜恢复任务未能启动，已回滚到可重试状态", conn=None)
                except Exception:  # noqa: BLE001
                    pass
    return resumed
