"""分镜清空的实际执行（投影级与整集级）。

从 app/domain/storyboard_ops.py 按原样搬移；被 clear_preview 依赖。
"""
from __future__ import annotations

import asyncio

from app import (
    config,
    task_registry,
    worker,
)
from app.db import (
    get_conn,
    now,
)
from app.domain.common import _episode_or_404
from app.evidence import repository as evidence_repository
from fastapi import HTTPException
from pathlib import Path


async def clear_storyboard_projection(episode_id: str) -> dict:
    """Fast product reset: clear current production state while retaining audit history."""
    cancelled_tasks = 0
    for kind in ("storyboard", "video_completion"):
        try:
            cancelled_tasks += int(await asyncio.wait_for(
                task_registry.cancel_and_wait(kind, episode_id), timeout=10,
            ))
        except TimeoutError as exc:
            raise HTTPException(
                409,
                "相关生成任务未能在 10 秒内安全停止；未开始清空，请稍后重试",
            ) from exc

    def _reset_projection() -> dict:
        import shutil

        from app.completion_grant import ProviderTasksNotTerminalError

        ep = _episode_or_404(episode_id)
        if ep["screenplay_publish_fence"]:
            raise HTTPException(409, "剧本正在发布，请完成后再清空分镜")
        conn = get_conn()
        claimed = conn.execute(
            "UPDATE episodes SET screenplay_publish_fence=1 "
            "WHERE id=? AND screenplay_publish_fence=0",
            (episode_id,),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            raise HTTPException(409, "分镜状态刚刚发生变化，请稍后重试")
        conn.commit()

        package_paths: list[str] = []
        try:
            shot_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
            ).fetchone()["c"])
            media_versions = int(conn.execute(
                """SELECT COUNT(*) AS c FROM shot_versions v
                   JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
                (episode_id,),
            ).fetchone()["c"])
            run_rows = conn.execute(
                """SELECT id,workflow_type,status FROM workflow_runs
                   WHERE workflow_type IN ('storyboard','video_completion')
                     AND scope_type='episode' AND scope_id=?""",
                (episode_id,),
            ).fetchall()
            package_paths = [
                str(row["package_path"])
                for row in conn.execute(
                    "SELECT package_path FROM delivery_packages WHERE episode_id=?",
                    (episode_id,),
                ).fetchall()
                if row["package_path"]
            ]

            worker.delete_episode_shots(episode_id)
            conn = get_conn()
            conn.execute("BEGIN IMMEDIATE")
            stamp = now()
            active_run_ids = [
                str(row["id"])
                for row in run_rows
                if row["status"] in evidence_repository.ACTIVE_RUN_STATUSES
            ]
            if active_run_ids:
                marks = ",".join("?" for _ in active_run_ids)
                conn.execute(
                    f"""UPDATE step_runs SET status='CANCELLED', finished_at=COALESCE(finished_at,?),
                           exit_reason=COALESCE(exit_reason,'CLEARED_BY_USER')
                       WHERE run_id IN ({marks})
                         AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')""",
                    (stamp, *active_run_ids),
                )
                conn.execute(
                    f"""UPDATE provider_calls SET status='CANCELLED',
                           error=COALESCE(error,'CLEARED_BY_USER')
                       WHERE run_id IN ({marks}) AND status='RUNNING'""",
                    active_run_ids,
                )
                conn.execute(
                    f"""UPDATE workflow_runs SET status='CANCELLED',
                           failure_code='CLEARED_BY_USER',
                           failure_message='用户清空分镜工作区',
                           finished_at=COALESCE(finished_at,?), updated_at=?
                       WHERE id IN ({marks})""",
                    (stamp, stamp, *active_run_ids),
                )

            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shot_audio'"
            ).fetchone():
                conn.execute("DELETE FROM shot_audio WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM storyboard_action_previews WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM storyboard_edit_sessions WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM storyboard_workspace_state WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM delivery_packages WHERE episode_id=?", (episode_id,))
            conn.execute(
                "DELETE FROM completion_grants WHERE episode_id=? AND kind='storyboard'",
                (episode_id,),
            )
            conn.execute(
                "DELETE FROM production_grants WHERE episode_id=? AND kind='storyboard'",
                (episode_id,),
            )
            conn.execute(
                """UPDATE production_revisions SET status='superseded', updated_at=?
                   WHERE episode_id=? AND kind='storyboard' AND status='active'""",
                (stamp, episode_id),
            )
            conn.execute(
                """UPDATE artifacts SET status='rejected',
                       stale_reason=COALESCE(stale_reason,'用户已清空分镜工作区')
                   WHERE type IN ('storyboard_supervisor_checkpoint','storyboard_outline')
                     AND scope_type='episode' AND scope_id=?
                     AND status IN ('candidate','validated','approved')""",
                (episode_id,),
            )
            conn.execute(
                """UPDATE episodes SET
                       storyboard_outline_json=NULL,
                       storyboard_artifact_id=NULL,
                       storyboard_warning=NULL,
                       active_storyboard_run_id=NULL,
                       working_storyboard_artifact_id=NULL,
                       published_storyboard_artifact_id=NULL,
                       storyboard_production_revision_id=NULL,
                       storyboard_completion_certificate_id=NULL,
                       active_video_run_id=NULL,
                       video_control_json=NULL,
                       delivery_artifact_id=NULL,
                       delivery_status='not_ready',
                       status='planned',
                       script_error=NULL,
                       screenplay_publish_fence=0
                   WHERE id=?""",
                (episode_id,),
            )
            from app.storyboard_authority import (
                clear_storyboard_outline_authority,
            )

            clear_storyboard_outline_authority(
                episode_id,
                conn=conn,
            )
            if "storyboard_control_json" in {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(episodes)").fetchall()
            }:
                conn.execute(
                    "UPDATE episodes SET storyboard_control_json=NULL WHERE id=?",
                    (episode_id,),
                )
            conn.commit()

            projects_root = config.PROJECTS_DIR.resolve()
            removed_files = 0
            for raw_path in dict.fromkeys(package_paths):
                candidate = Path(raw_path)
                try:
                    resolved = candidate.resolve()
                    if resolved == projects_root or projects_root not in resolved.parents:
                        continue
                    if resolved.is_dir():
                        shutil.rmtree(resolved, ignore_errors=True)
                        removed_files += 1
                    elif resolved.exists():
                        resolved.unlink()
                        removed_files += 1
                except OSError:
                    continue
            storyboard_runs = sum(
                1 for row in run_rows if row["workflow_type"] == "storyboard"
            )
            return {
                "cleared": True,
                "episode_id": episode_id,
                "shots_deleted": shot_count,
                "media_versions_deleted": media_versions,
                "storyboard_runs_preserved": storyboard_runs,
                "downstream_runs_preserved": len(run_rows) - storyboard_runs,
                "files_deleted": removed_files,
                "cancelled_tasks": cancelled_tasks,
                "screenplay_preserved": True,
                "audit_history_preserved": True,
            }
        except ProviderTasksNotTerminalError as exc:
            conn = get_conn()
            if conn.in_transaction:
                conn.rollback()
            raise HTTPException(409, exc.detail) from exc
        except Exception:
            conn = get_conn()
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn = get_conn()
            conn.execute(
                "UPDATE episodes SET screenplay_publish_fence=0 WHERE id=?",
                (episode_id,),
            )
            conn.commit()

    return await asyncio.to_thread(_reset_projection)

async def clear_storyboard(episode_id: str):
    """清理整集分镜痕迹；产品入口必须先通过 ``storyboard_clear`` 影响预览。

    The screenplay is intentionally retained.  Unlike cancellation, clearing also
    removes checkpoints, workflow/provider cache rows, active revisions and all
    shot-derived media so the next start is observably and behaviorally clean.
    """
    from app.completion_grant import ProviderTasksNotTerminalError

    ep = _episode_or_404(episode_id)
    if ep["screenplay_publish_fence"]:
        raise HTTPException(409, "剧本正在发布，请完成后再清空分镜")

    conn = get_conn()
    claimed = conn.execute(
        "UPDATE episodes SET screenplay_publish_fence=1 "
        "WHERE id=? AND screenplay_publish_fence=0",
        (episode_id,),
    )
    if claimed.rowcount != 1:
        conn.rollback()
        raise HTTPException(409, "分镜状态刚刚发生变化，请稍后重试")
    conn.commit()

    cancelled_tasks = 0
    package_paths: list[str] = []
    artifact_paths: list[str] = []
    try:
        for kind in ("storyboard", "video_completion"):
            cancelled_tasks += int(await task_registry.cancel_and_wait(kind, episode_id))

        conn = get_conn()
        shot_ids = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM shots WHERE episode_id=?", (episode_id,),
            ).fetchall()
        ]
        shot_count = len(shot_ids)
        media_versions = int(conn.execute(
            """SELECT COUNT(*) AS c FROM shot_versions v
               JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
            (episode_id,),
        ).fetchone()["c"])
        package_paths = [
            str(row["package_path"])
            for row in conn.execute(
                "SELECT package_path FROM delivery_packages WHERE episode_id=?",
                (episode_id,),
            ).fetchall()
            if row["package_path"]
        ]

        run_rows = conn.execute(
            """SELECT id,workflow_type FROM workflow_runs
               WHERE workflow_type IN ('storyboard','video_completion')
                 AND scope_type='episode' AND scope_id=?""",
            (episode_id,),
        ).fetchall()
        run_ids = [str(row["id"]) for row in run_rows]
        storyboard_run_count = sum(
            1 for row in run_rows if row["workflow_type"] == "storyboard"
        )
        step_ids: list[str] = []
        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            step_ids = [
                str(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM step_runs WHERE run_id IN ({marks})", run_ids,
                ).fetchall()
            ]

        certificate_artifact_ids = [
            str(row["artifact_id"])
            for row in conn.execute(
                "SELECT artifact_id FROM completion_certificates "
                "WHERE kind='storyboard' AND scope_id=?",
                (episode_id,),
            ).fetchall()
        ]
        artifact_where = [
            "(scope_type='episode' AND scope_id=? AND type IN "
            "('storyboard','storyboard_outline','storyboard_supervisor_checkpoint',"
            "'video_supervisor_checkpoint','video_coverage_report'))",
            "(scope_type='storyboard_checkpoint' AND scope_id LIKE ?)",
        ]
        artifact_params: list[object] = [episode_id, f"{episode_id}:%"]
        if shot_ids:
            marks = ",".join("?" for _ in shot_ids)
            artifact_where.append(f"(scope_type='shot' AND scope_id IN ({marks}))")
            artifact_params.extend(shot_ids)
        if step_ids:
            marks = ",".join("?" for _ in step_ids)
            artifact_where.append(f"created_by_step_run_id IN ({marks})")
            artifact_params.extend(step_ids)
        artifact_rows = conn.execute(
            "SELECT id,file_path FROM artifacts WHERE " + " OR ".join(artifact_where),
            artifact_params,
        ).fetchall()
        artifact_ids = list(dict.fromkeys(
            [str(row["id"]) for row in artifact_rows] + certificate_artifact_ids
        ))
        artifact_paths = [str(row["file_path"]) for row in artifact_rows if row["file_path"]]

        # This removes shots, references, media jobs and final video files first.
        worker.delete_episode_shots(episode_id)
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shot_audio'"
        ).fetchone():
            conn.execute("DELETE FROM shot_audio WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM storyboard_action_previews WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM storyboard_edit_sessions WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM storyboard_workspace_state WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM delivery_packages WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM customer_feedback WHERE episode_id=?", (episode_id,))
        conn.execute(
            "DELETE FROM completion_grants WHERE episode_id=? AND kind='storyboard'",
            (episode_id,),
        )
        conn.execute(
            "DELETE FROM production_grants WHERE episode_id=? AND kind='storyboard'",
            (episode_id,),
        )
        conn.execute(
            "DELETE FROM completion_certificates WHERE kind='storyboard' AND scope_id=?",
            (episode_id,),
        )
        conn.execute(
            "DELETE FROM production_revisions WHERE episode_id=? AND kind='storyboard'",
            (episode_id,),
        )
        conn.execute(
            "DELETE FROM review_action_audit WHERE scope_type='episode' AND scope_id=?",
            (episode_id,),
        )
        if shot_ids:
            marks = ",".join("?" for _ in shot_ids)
            conn.execute(
                f"DELETE FROM review_action_audit WHERE scope_type='shot' AND scope_id IN ({marks})",
                shot_ids,
            )

        conn.execute(
            """UPDATE episodes SET
                   storyboard_outline_json=NULL,
                   storyboard_artifact_id=NULL,
                   storyboard_warning=NULL,
                   active_storyboard_run_id=NULL,
                   working_storyboard_artifact_id=NULL,
                   published_storyboard_artifact_id=NULL,
                   storyboard_production_revision_id=NULL,
                   storyboard_completion_certificate_id=NULL,
                   active_video_run_id=NULL,
                   video_control_json=NULL,
                   delivery_artifact_id=NULL,
                   delivery_status='not_ready',
                   status='planned',
                   script_error=NULL,
                   screenplay_publish_fence=0
               WHERE id=?""",
            (episode_id,),
        )
        from app.storyboard_authority import (
            clear_storyboard_outline_authority,
        )

        clear_storyboard_outline_authority(
            episode_id,
            conn=conn,
        )
        if "storyboard_control_json" in {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(episodes)").fetchall()
        }:
            conn.execute(
                "UPDATE episodes SET storyboard_control_json=NULL WHERE id=?",
                (episode_id,),
            )

        if artifact_ids:
            marks = ",".join("?" for _ in artifact_ids)
            conn.execute(f"DELETE FROM gate_decisions WHERE artifact_id IN ({marks})", artifact_ids)
            conn.execute(f"DELETE FROM evaluations WHERE artifact_id IN ({marks})", artifact_ids)
            conn.execute(
                f"UPDATE artifacts SET superseded_by_artifact_id=NULL "
                f"WHERE superseded_by_artifact_id IN ({marks})",
                artifact_ids,
            )
            conn.execute(f"DELETE FROM artifacts WHERE id IN ({marks})", artifact_ids)

        if run_ids:
            run_marks = ",".join("?" for _ in run_ids)
            conn.execute(f"DELETE FROM gate_decisions WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(f"DELETE FROM provider_calls WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(f"DELETE FROM run_events WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(f"UPDATE agent_tool_calls SET run_id=NULL WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(
                f"UPDATE customer_feedback SET revision_run_id=NULL "
                f"WHERE revision_run_id IN ({run_marks})",
                run_ids,
            )
            if step_ids:
                step_marks = ",".join("?" for _ in step_ids)
                conn.execute(
                    f"UPDATE artifacts SET created_by_step_run_id=NULL "
                    f"WHERE created_by_step_run_id IN ({step_marks})",
                    step_ids,
                )
                conn.execute(
                    f"UPDATE evaluations SET step_run_id=NULL WHERE step_run_id IN ({step_marks})",
                    step_ids,
                )
                conn.execute(
                    f"UPDATE step_runs SET parent_step_run_id=NULL "
                    f"WHERE parent_step_run_id IN ({step_marks})",
                    step_ids,
                )
            conn.execute(f"DELETE FROM step_runs WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(
                f"UPDATE workflow_runs SET parent_run_id=NULL WHERE parent_run_id IN ({run_marks})",
                run_ids,
            )
            conn.execute(f"DELETE FROM workflow_runs WHERE id IN ({run_marks})", run_ids)
        conn.commit()

        # Delete packaged/file artifacts only when they are inside this workspace.
        import shutil

        projects_root = config.PROJECTS_DIR.resolve()
        removed_files = 0
        for raw_path in dict.fromkeys(package_paths + artifact_paths):
            candidate = Path(raw_path)
            try:
                resolved = candidate.resolve()
                if resolved == projects_root or projects_root not in resolved.parents:
                    continue
                if resolved.is_dir():
                    shutil.rmtree(resolved, ignore_errors=True)
                    removed_files += 1
                elif resolved.exists():
                    resolved.unlink()
                    removed_files += 1
            except OSError:
                continue
        return {
            "cleared": True,
            "episode_id": episode_id,
            "shots_deleted": shot_count,
            "media_versions_deleted": media_versions,
            "storyboard_runs_deleted": storyboard_run_count,
            "downstream_runs_deleted": len(run_ids) - storyboard_run_count,
            "storyboard_artifacts_deleted": len(artifact_ids),
            "files_deleted": removed_files,
            "cancelled_tasks": cancelled_tasks,
            "screenplay_preserved": True,
        }
    except ProviderTasksNotTerminalError as exc:
        conn = get_conn()
        if conn.in_transaction:
            conn.rollback()
        raise HTTPException(409, exc.detail) from exc
    except Exception:
        conn = get_conn()
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn = get_conn()
        conn.execute(
            "UPDATE episodes SET screenplay_publish_fence=0 WHERE id=?",
            (episode_id,),
        )
        conn.commit()
