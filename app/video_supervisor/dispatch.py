"""逐镜派发：入队前置校验、心跳包裹与失败退款处理。"""
from __future__ import annotations

import asyncio
import json

from typing import Any

from app.completion_grant import GrantValidationError
from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.media_pipeline.stages import ACTIVE_JOB_STATUSES
from app.schemas import Shot
from app.video_issues import issues_from_enqueue_error, persist_shot_issue
from app.video_repair_router import VideoRepairPlan, bump_fingerprint_count

from .authority import _supervisor_checks_can_use_worker_thread, _verify_supervisor_paid_authority
from .checkpoint import _refresh_supervisor_heartbeat, load_latest_checkpoint
from .constants import DISPATCH_HEARTBEAT_INTERVAL_S, SUPERVISOR_HEARTBEAT_STALE_S, TERMINAL_SUPERVISOR_PHASES
from .models import ShotCoverageEntry, VideoSupervisorCheckpoint



def _after_shot_id(episode_id: str, shot_no: int, *, degrade: bool = False) -> str | None:
    if degrade or shot_no <= 1:
        return None
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? AND shot_no IN (?, ?) ORDER BY shot_no",
        (episode_id, shot_no - 1, shot_no),
    ).fetchall()
    if len(rows) < 2:
        return None
    prev_row, cur_row = rows[0], rows[1]

    def to_model(r):
        return Shot(
            shot_no=r["shot_no"], duration_s=r["duration_s"] or 5,
            shot_size=r["shot_size"] or "中景", camera_move=r["camera_move"] or "固定",
            scene_time=(r["scene_time"] if "scene_time" in r.keys() else "") or "",
            scene_setting=r["scene_setting"] or "",
            scene_name=(r["scene_name"] if "scene_name" in r.keys() else "") or "",
            characters=json.loads(r["characters"] or "[]"),
            action_desc=r["action_desc"] or "",
            continuity_from_prev=bool(r["continuity_from_prev"]),
        )

    if uses_previous_tail_frame(derive_continuity_mode(to_model(cur_row), to_model(prev_row))):
        return prev_row["id"]
    return None


def _dispatch(
    entry: ShotCoverageEntry,
    *,
    episode_id: str,
    run_id: str | None,
    cp: VideoSupervisorCheckpoint | None = None,
    plan: VideoRepairPlan | None = None,
    first: bool = False,
) -> bool:
    """入队；失败 Issue 化。返回是否产生新进展（非 reused）。"""
    from app import worker
    from app.compiler import CompileError

    # 付费派发的最后保险：补齐模式永远不得为已有采用版的镜头创建新任务。
    # entry 是循环开始时的快照；入队前必须重读数据库，封住用户刚刚采用候选的并发窗口。
    if entry.adopted_version_id:
        return False

    conn = get_conn()
    current_shot = conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id=? AND episode_id=?",
        (entry.shot_id, episode_id),
    ).fetchone()
    if not current_shot or current_shot["adopted_version_id"]:
        return False
    execution_rows = conn.execute(
        """SELECT j.status,j.provider_create_state,v.provider_task_id,v.image_inputs
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id=j.version_id
            WHERE j.shot_id=? AND j.episode_id=? AND j.kind='video'
              AND (v.status IS NULL OR v.status!='cleared')
            ORDER BY j.created_at DESC""",
        (entry.shot_id, episode_id),
    ).fetchall()
    for execution in execution_rows:
        # model_rejected 是跨轮次长期有效的终态标记：一旦供应商明确拒绝过这个
        # 镜头，任何 run（包括之后新开的 run）都不得再对它派发付费任务。这条
        # 判断必须挂在“这件事本身成没成”上，不能被下面按 run_id 过滤的
        # continue 挡住——否则新 run 会看不到旧 run 判定的终态，盲目重投。
        if execution["provider_create_state"] == "model_rejected":
            return False
        try:
            execution_meta = json.loads(execution["image_inputs"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            execution_meta = {}
        if run_id and execution_meta.get("supervisor_run_id") != run_id:
            continue
        if (
            execution["status"] in ACTIVE_JOB_STATUSES
            and (
                execution["provider_task_id"]
                or execution["provider_create_state"] in {"submitting", "accepted"}
            )
        ):
            return False

    if run_id:
        ep = conn.execute(
            "SELECT active_video_run_id, video_completion_mode FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        latest = load_latest_checkpoint(episode_id)
        if (
            not ep
            or ep["video_completion_mode"] != "complete"
            or ep["active_video_run_id"] != run_id
            or (latest is not None and (
                latest.dispatch_fenced_at is not None
                or latest.phase in TERMINAL_SUPERVISOR_PHASES
            ))
        ):
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "VIDEO_DISPATCH_FENCED",
                    "warning",
                    f"第 {entry.shot_no} 镜派发被终态围栏拒绝",
                    payload={"shot_no": entry.shot_no, "phase": latest.phase if latest else None},
                )
            return False

    authority_checkpoint = cp or (load_latest_checkpoint(episode_id) if run_id else None)
    if authority_checkpoint is not None:
        # Last reversible boundary before worker.enqueue_shot can create paid
        # provider work.  Do not turn authority drift into a generic QA issue.
        _verify_supervisor_paid_authority(
            authority_checkpoint,
            stage="video_provider_enqueue",
        )

    kwargs: dict[str, Any] = {}
    degrade = bool(plan and plan.degrade_chain)
    if not first:
        kwargs["reroll"] = True
    if plan:
        if plan.extra_negative:
            kwargs["extra_negative"] = plan.extra_negative
        if plan.critique:
            kwargs["critique"] = plan.critique
        if plan.prompt_aggressive:
            # 软化：由 enqueue 侧 prompt_override 处理；此处标记进 image_inputs via worker 扩展
            kwargs["prompt_override"] = None  # 让 compile 走默认，aggressive 在 meta
    after = _after_shot_id(episode_id, entry.shot_no, degrade=degrade)
    kwargs["after_shot_id"] = after
    kwargs["auto_retake_count"] = entry.attempts_paid
    kwargs["supervisor_run_id"] = run_id
    # Supervisor dispatches are positive production actions too.  Capture the
    # same immutable dependency token used by the review wall so a later
    # screenplay/storyboard/asset change fences already-running providers.
    if run_id:
        try:
            from app.api import _review_assert_shot_positive
            kwargs["dependency_snapshot"] = _review_assert_shot_positive(entry.shot_id)
        except Exception as exc:
            issues = issues_from_enqueue_error(
                exc, shot_id=entry.shot_id, shot_no=entry.shot_no,
            )
            persist_shot_issue(
                episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
                issues=issues, source="supervisor_dependency_fence", run_id=run_id,
            )
            entry.last_issue_codes = [i.code for i in issues]
            return False
    kwargs["supervisor_meta"] = {
        "supervisor_run_id": run_id,
        "supervisor_repair_level": (plan.level if plan else entry.repair_level),
        "supervisor_strategy": (plan.strategy if plan else "first_attempt"),
        "supervisor_issue_codes": (plan.issue_codes if plan else entry.last_issue_codes),
        "continuity_degraded": degrade or entry.continuity_degraded,
        "rebuild_reference": bool(plan and plan.rebuild_reference),
    }

    try:
        # rebuild_reference：清参考图目录标记，让 enqueue 重建
        if plan and plan.rebuild_reference:
            conn = get_conn()
            conn.execute(
                """UPDATE shot_versions SET image_inputs=json_set(
                     COALESCE(image_inputs, '{}'), '$.reference_images', json('[]'),
                     '$.force_rebuild_reference', 1)
                   WHERE shot_id=? AND status='succeeded'""",
                (entry.shot_id,),
            )
            # sqlite json_set 可能不可用：容错
            try:
                conn.commit()
            except Exception:  # noqa: BLE001
                conn.rollback()
        if authority_checkpoint is not None:
            _verify_supervisor_paid_authority(
                authority_checkpoint,
                stage="video_provider_enqueue_commit",
            )
        result = worker.enqueue_shot(entry.shot_id, **{
            k: v for k, v in kwargs.items() if k != "supervisor_meta"
        })
        # 把 supervisor meta 写入新建 version
        if result.get("version_id") and kwargs.get("supervisor_meta"):
            _patch_version_supervisor_meta(result["version_id"], kwargs["supervisor_meta"])
    except GrantValidationError:
        raise
    except (CompileError, ValueError) as exc:
        issues = issues_from_enqueue_error(exc, shot_id=entry.shot_id, shot_no=entry.shot_no)
        persist_shot_issue(
            episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
            issues=issues, source="supervisor_enqueue", run_id=run_id,
        )
        entry.last_issue_codes = [i.code for i in issues]
        for issue in issues:
            entry.issue_fingerprint_counts = bump_fingerprint_count(
                entry.issue_fingerprint_counts, issue.fingerprint
            )
        return False
    except Exception as exc:  # noqa: BLE001
        issues = issues_from_enqueue_error(exc, shot_id=entry.shot_id, shot_no=entry.shot_no)
        persist_shot_issue(
            episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
            issues=issues, source="supervisor_enqueue", run_id=run_id,
        )
        entry.last_issue_codes = [i.code for i in issues]
        return False

    if result.get("paused_budget"):
        issues = issues_from_enqueue_error(
            ValueError("预算不足，任务暂停"), shot_id=entry.shot_id, shot_no=entry.shot_no,
        )
        persist_shot_issue(
            episode_id=episode_id, shot_id=entry.shot_id, shot_no=entry.shot_no,
            issues=issues, source="supervisor_budget", run_id=run_id,
        )
        entry.last_issue_codes = [i.code for i in issues]
        return False
    if result.get("reused"):
        return bool(result.get("resumed"))
    if degrade:
        entry.continuity_degraded = True
    if plan and plan.rebuild_reference:
        entry.rebuilt_reference = True
    return True


def _dispatch_with_heartbeat(
    entry: ShotCoverageEntry,
    *,
    episode_id: str,
    run_id: str | None,
    cp: VideoSupervisorCheckpoint,
    plan: VideoRepairPlan | None = None,
    first: bool = False,
) -> bool:
    """Keep the run live across synchronous request compilation and enqueue."""
    _refresh_supervisor_heartbeat(cp, run_id=run_id)
    try:
        return _dispatch(
            entry,
            episode_id=episode_id,
            run_id=run_id,
            cp=cp,
            plan=plan,
            first=first,
        )
    finally:
        _refresh_supervisor_heartbeat(cp, run_id=run_id)


async def _dispatch_with_heartbeat_async(
    entry: ShotCoverageEntry,
    *,
    episode_id: str,
    run_id: str | None,
    cp: VideoSupervisorCheckpoint,
    plan: VideoRepairPlan | None = None,
    first: bool = False,
    heartbeat_interval_s: float = DISPATCH_HEARTBEAT_INTERVAL_S,
) -> bool:
    """Run synchronous authority checks and enqueue work off the event loop."""
    if not _supervisor_checks_can_use_worker_thread():
        return _dispatch_with_heartbeat(
            entry,
            episode_id=episode_id,
            run_id=run_id,
            cp=cp,
            plan=plan,
            first=first,
        )

    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _dispatch_heartbeat(
            cp,
            run_id=run_id,
            stop=heartbeat_stop,
            interval_s=heartbeat_interval_s,
        ),
        name=f"video-dispatch-heartbeat:{episode_id}:{entry.shot_no}",
    )
    worker_task = asyncio.create_task(
        asyncio.to_thread(
            _dispatch_with_heartbeat,
            entry,
            episode_id=episode_id,
            run_id=run_id,
            cp=cp,
            plan=plan,
            first=first,
        ),
        name=f"video-dispatch-worker:{episode_id}:{entry.shot_no}",
    )
    cancelled = False
    try:
        while True:
            try:
                result = await asyncio.shield(worker_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                continue
        if cancelled:
            raise asyncio.CancelledError
        return result
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def _dispatch_heartbeat(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    stop: asyncio.Event,
    interval_s: float = DISPATCH_HEARTBEAT_INTERVAL_S,
) -> None:
    wait_s = max(
        0.01,
        min(float(interval_s), SUPERVISOR_HEARTBEAT_STALE_S / 3.0),
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait_s)
            return
        except asyncio.TimeoutError:
            pass
        try:
            refreshed = await asyncio.to_thread(
                _refresh_supervisor_heartbeat,
                cp,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient DB contention is retryable
            try:
                from app import errors
                errors.log_error(
                    exc,
                    action="video_supervisor.dispatch_heartbeat",
                    context={"episode_id": cp.episode_id, "run_id": run_id},
                )
            except Exception:  # noqa: BLE001 - heartbeat retry must stay alive
                pass
            continue
        if not refreshed:
            return


def _patch_version_supervisor_meta(version_id: str, meta: dict[str, Any]) -> None:
    conn = get_conn()
    row = conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?", (version_id,)
    ).fetchone()
    if not row:
        return
    try:
        data = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update(meta)
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id=?",
        (json.dumps(data, ensure_ascii=False), version_id),
    )
    conn.commit()


def _requeue_no_charge_job(
    conn,
    *,
    shot_id: str,
    run_id: str | None,
) -> str | None:
    """Requeue a retryable failure without crossing an intentional pause gate."""
    job = conn.execute(
        """SELECT id FROM jobs WHERE shot_id=? AND kind='video'
           AND status IN ('failed','waiting_retry')
           AND (? IS NULL OR owner_run_id=?)
           ORDER BY created_at DESC LIMIT 1""",
        (shot_id, run_id, run_id),
    ).fetchone()
    if not job:
        return None
    changed = conn.execute(
        """UPDATE jobs
              SET status='queued', retry_count=0, error=NULL, updated_at=?
            WHERE id=? AND status IN ('failed','waiting_retry')""",
        (now(), job["id"]),
    )
    conn.commit()
    return str(job["id"]) if changed.rowcount == 1 else None


def _budget_paused_job_id(
    conn,
    *,
    shot_id: str,
) -> str | None:
    row = conn.execute(
        """SELECT id FROM jobs WHERE shot_id=? AND kind='video'
           AND status='paused_budget'
           AND cancellation_requested=0 AND abandoned=0
           ORDER BY created_at DESC LIMIT 1""",
        (shot_id,),
    ).fetchone()
    return str(row["id"]) if row else None
