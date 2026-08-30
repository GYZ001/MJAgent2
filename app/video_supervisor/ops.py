"""对外运维入口：修复预览与启动期批量恢复。"""
from __future__ import annotations

import asyncio
import json

from typing import Any

from app.completion_grant import GrantValidationError, get_video_grant
from app.db import get_conn, now

from .authority import _record_grant_validation_failure, _verify_supervisor_paid_authority
from .checkpoint import load_latest_checkpoint, save_checkpoint
from .constants import TERMINAL_SUPERVISOR_PHASES
from .resilience import run_video_completion_resilient



def preview_video_completion_repair(episode_id: str) -> dict[str, Any]:
    """只读预演遗留/崩溃 run 的收口结果，不创建校验、不改 adopted、不停 job。"""
    conn = get_conn()
    cp = load_latest_checkpoint(episode_id)
    ep = conn.execute(
        "SELECT active_video_run_id, video_completion_mode, status FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not ep:
        raise ValueError(f"剧集不存在：{episode_id}")
    shots = conn.execute(
        "SELECT id, shot_no, adopted_version_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    plan: list[dict[str, Any]] = []
    for shot in shots:
        candidates: list[dict[str, Any]] = []
        versions = conn.execute(
            """SELECT id, version_no, qa_json, technical_validation_json, adoption_reason
               FROM shot_versions WHERE shot_id=? AND status='succeeded' ORDER BY version_no""",
            (shot["id"],),
        ).fetchall()
        for version in versions:
            try:
                technical = json.loads(version["technical_validation_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                technical = {}
            if not technical.get("passed"):
                continue
            try:
                qa = json.loads(version["qa_json"] or "{}")
                score = float(qa.get("overall")) if qa.get("overall") is not None else -1.0
            except (TypeError, ValueError, json.JSONDecodeError):
                score = -1.0
            candidates.append({
                "version_id": version["id"],
                "version_no": int(version["version_no"]),
                "qa_overall": score,
            })
        candidates.sort(key=lambda item: (item["qa_overall"], item["version_no"]), reverse=True)
        adopted_valid = any(
            candidate["version_id"] == shot["adopted_version_id"] for candidate in candidates
        )
        if adopted_valid:
            action = "retain_adopted"
            selected = shot["adopted_version_id"]
        elif candidates:
            action = "adopt_best_technical_candidate"
            selected = candidates[0]["version_id"]
        else:
            action = "mark_missing"
            selected = None
        blocked_job = conn.execute(
            """SELECT id, after_shot_id FROM jobs
               WHERE shot_id=? AND kind='video'
                 AND status IN ('queued','running','waiting_provider','waiting_retry','waiting','waiting_human')
               ORDER BY created_at DESC LIMIT 1""",
            (shot["id"],),
        ).fetchone()
        plan.append({
            "shot_no": int(shot["shot_no"]),
            "shot_id": shot["id"],
            "action": action,
            "selected_version_id": selected,
            "candidates": candidates,
            "active_job_id": blocked_job["id"] if blocked_job else None,
            "blocked_by_shot_id": blocked_job["after_shot_id"] if blocked_job else None,
        })
    return {
        "dry_run": True,
        "episode_id": episode_id,
        "active_video_run_id": ep["active_video_run_id"],
        "video_completion_mode": ep["video_completion_mode"],
        "episode_status": ep["status"],
        "checkpoint_phase": cp.phase if cp else None,
        "checkpoint_started_at": cp.started_at if cp else None,
        "checkpoint_deadline_at": cp.deadline_at if cp else None,
        "would_adopt": [item for item in plan if item["action"] == "adopt_best_technical_candidate"],
        "would_retain": [item for item in plan if item["action"] == "retain_adopted"],
        "would_mark_missing": [item for item in plan if item["action"] == "mark_missing"],
        "shots": plan,
        "will_start_generation": False,
        "will_delete_media": False,
    }


def recover_video_completion_runs() -> int:
    """服务重启后恢复未完成的视频补齐 Supervisor。"""
    from app import task_registry
    from app.errors import log_error
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.observability.metrics import inc

    conn = get_conn()
    # 确保列存在
    for stmt in (
        "ALTER TABLE episodes ADD COLUMN active_video_run_id TEXT",
        "ALTER TABLE episodes ADD COLUMN video_completion_mode TEXT NOT NULL DEFAULT 'quick'",
    ):
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception:  # noqa: BLE001
            pass

    rows = conn.execute(
        """SELECT id, project_id, status AS episode_status, active_video_run_id,
                  video_completion_mode, storyboard_artifact_id
           FROM episodes WHERE video_completion_mode='complete'"""
    ).fetchall()
    resumed = 0

    def recover_one(row) -> bool:
        episode_id = row["id"]
        if task_registry.active("video_completion", episode_id):
            return False
        cp = load_latest_checkpoint(episode_id)
        if cp is None:
            return False
        legacy_without_deadline = cp.deadline_at is None
        if legacy_without_deadline and cp.grant_id:
            prior_grant = get_video_grant(cp.grant_id)
            if prior_grant:
                cp.deadline_at = float(prior_grant.deadline_at)
        if cp.phase in TERMINAL_SUPERVISOR_PHASES or cp.phase in {"WAITING_AUTHORIZATION", "WAITING_HUMAN"}:
            return False
        # 用户取消的 run 不恢复
        cancelled = conn.execute(
            """SELECT id FROM workflow_runs
               WHERE workflow_type='episode_video_completion' AND scope_type='episode'
                 AND scope_id=? AND status='CANCELLED'
               ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        latest = conn.execute(
            """SELECT status FROM workflow_runs
               WHERE workflow_type='episode_video_completion' AND scope_type='episode'
                 AND scope_id=? ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        if cancelled and latest and latest["status"] == "CANCELLED":
            return False
        deadline_due = bool(cp.deadline_at and now() >= cp.deadline_at)
        # 旧版本事故 run 没有持久化 deadline；只提供 dry-run，禁止启动时静默改用户现场数据。
        if legacy_without_deadline and deadline_due:
            return False
        if cp.grant_id and not deadline_due:
            try:
                _verify_supervisor_paid_authority(cp, stage="service_restart_resume")
            except GrantValidationError as exc:
                cp.phase = "WAITING_AUTHORIZATION"
                cp.outcome = exc.code
                save_checkpoint(cp)
                _record_grant_validation_failure(
                    cp, exc, run_id=cp.run_id, stage="service_restart_resume",
                )
                return False

        recorder = WorkflowRecorder.create(
            workflow_type="episode_video_completion",
            scope_type="episode",
            scope_id=episode_id,
            input_fingerprint=fingerprint(row["storyboard_artifact_id"], cp.grant_id),
            requested_by="system",
            trigger_type="resume",
            policy_snapshot={"supervisor": "video_completion", "resume": True},
            deadline_at=cp.deadline_at,
            parent_run_id=row["active_video_run_id"],
        )

        async def _task(eid=episode_id, rid=recorder.run_id, gid=cp.grant_id, rec=recorder):
            # Recovery tasks are spawned inside the FastAPI lifespan. Let the
            # lifespan publish startup completion before reconstructing large
            # immutable screenplay/storyboard authority models.
            await asyncio.sleep(1.0)
            rec.start()
            try:
                result = await run_video_completion_resilient(
                    eid, resume=True, grant_id=gid, run_id=rid,
                )
                if result.phase == "SUCCEEDED_COVERED":
                    rec.succeed(result.outcome or "SUCCEEDED_COVERED", conn=None)
                elif result.phase in {"WAITING_AUTHORIZATION", "WAITING_HUMAN", "PAUSED_EXTERNAL", "PAUSED_BUDGET"}:
                    rec.partial(result.outcome or result.phase, conn=None)
                elif result.phase == "CANCELLED":
                    rec.cancel(conn=None)
                else:
                    rec.partial(result.phase, conn=None)
            except asyncio.CancelledError:
                if task_registry.shutdown_in_progress():
                    rec.pause_external("服务重启，全片视频补齐等待自动恢复", conn=None)
                else:
                    rec.cancel(conn=None)
                raise
            except Exception as exc:
                rec.fail(exc, conn=None)
                raise

        claimed = conn.execute(
            """UPDATE episodes SET active_video_run_id=?, status='generating'
               WHERE id=? AND active_video_run_id IS ?""",
            (recorder.run_id, episode_id, row["active_video_run_id"]),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            recorder.cancel("恢复启动权已变化，当前运行未启动", conn=None)
            return False
        conn.commit()
        coro = _task()
        try:
            task_registry.spawn(
                "video_completion", episode_id, coro, project_id=row["project_id"],
            )
        except Exception as exc:
            coro.close()
            try:
                recorder.start()
                recorder.fail(exc, conn=None)
            except Exception:  # noqa: BLE001
                pass
            conn.execute(
                """UPDATE episodes SET active_video_run_id=?, status=?
                   WHERE id=? AND active_video_run_id=?""",
                (
                    row["active_video_run_id"],
                    row["episode_status"],
                    episode_id,
                    recorder.run_id,
                ),
            )
            conn.commit()
            raise
        return True

    for row in rows:
        episode_id = row["id"]
        try:
            if recover_one(row):
                resumed += 1
        except Exception as exc:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            log_error(
                exc,
                action="video_supervisor.recover_episode",
                context={
                    "episode_id": episode_id,
                    "active_video_run_id": row["active_video_run_id"],
                },
                meta={"stage": "video_supervisor_recovery", "isolation": "episode"},
            )
            inc(
                "video_supervisor_recovery_episode_error_total",
                episode_id=episode_id,
                error_type=type(exc).__name__,
            )
    return resumed
