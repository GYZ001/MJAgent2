from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

from app import config, task_registry
from app.db import get_conn, new_id, now
from app.evidence import repository
from app.orchestration.engine import WorkflowRecorder, fingerprint


router = APIRouter(prefix="/api")


@router.get("/runs")
def list_runs(
    active: bool | None = Query(default=None),
    project_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    return repository.list_runs(active=active, project_id=project_id, limit=limit)


RUN_ACTIONABLE_STATUSES = {
    "CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN", "WAITING_AUTHORIZATION",
    "PAUSED_BUDGET", "PAUSED_EXTERNAL", "FAILED", "PARTIAL",
}


@router.get("/runs/query")
def query_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str = "",
    status: str | None = None,
    project_id: str | None = None,
    workflow: str | None = None,
    episode_no: int | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    include_history: bool = False,
    sort: str = "desc",
):
    """监制房全量 Run 查询；旧 ``/runs`` 数组契约继续供其他页面兼容使用。"""
    query_started = time.perf_counter()
    conn = get_conn()
    rows = [dict(row) for row in conn.execute(
        """SELECT wr.*,
                  CASE wr.scope_type
                    WHEN 'project' THEN wr.scope_id
                    WHEN 'episode' THEN scope_episode.project_id
                    WHEN 'shot' THEN shot_episode.project_id
                  END AS project_id,
                  COALESCE(project_scope.name, episode_project.name, shot_project.name) AS project_name,
                  COALESCE(scope_episode.id, shot_episode.id) AS episode_id,
                  COALESCE(scope_episode.episode_no, shot_episode.episode_no) AS episode_no,
                  COALESCE(scope_episode.title, shot_episode.title) AS episode_title,
                  scope_shot.id AS shot_id, scope_shot.shot_no AS shot_no
           FROM workflow_runs wr
           LEFT JOIN projects project_scope ON wr.scope_type='project' AND project_scope.id=wr.scope_id
           LEFT JOIN episodes scope_episode ON wr.scope_type='episode' AND scope_episode.id=wr.scope_id
           LEFT JOIN projects episode_project ON episode_project.id=scope_episode.project_id
           LEFT JOIN shots scope_shot ON wr.scope_type='shot' AND scope_shot.id=wr.scope_id
           LEFT JOIN episodes shot_episode ON shot_episode.id=scope_shot.episode_id
           LEFT JOIN projects shot_project ON shot_project.id=shot_episode.project_id"""
    ).fetchall()]
    allowed_statuses = {item.strip() for item in (status or "").split(",") if item.strip()}
    keyword = search.strip().lower()
    filtered = []
    for row in rows:
        if not include_history and not allowed_statuses and row.get("status") not in RUN_ACTIONABLE_STATUSES:
            continue
        if allowed_statuses and row.get("status") not in allowed_statuses:
            continue
        if project_id and row.get("project_id") != project_id:
            continue
        if workflow and row.get("workflow_type") != workflow:
            continue
        if episode_no is not None and row.get("episode_no") != episode_no:
            continue
        updated_at = float(row.get("updated_at") or 0)
        if from_ts is not None and updated_at < from_ts:
            continue
        if to_ts is not None and updated_at > to_ts:
            continue
        if keyword:
            haystack = " ".join(str(row.get(key) or "") for key in (
                "id", "workflow_type", "scope_type", "scope_id", "project_name",
                "episode_title", "current_step_key", "failure_code", "failure_message",
            )).lower()
            if keyword not in haystack:
                continue
        filtered.append(row)
    filtered.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=sort.lower() != "asc")
    total = len(filtered)
    start = (page - 1) * page_size
    return {
        "items": filtered[start:start + page_size], "total": total,
        "page": page, "page_size": page_size,
        "page_count": max(1, (total + page_size - 1) // page_size),
        "server_time": now(),
        "query_ms": round((time.perf_counter() - query_started) * 1000, 2),
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    run["steps"] = repository.get_steps(run_id)
    return run


@router.get("/runs/{run_id}/steps")
def get_steps(run_id: str):
    if not repository.get_run(run_id):
        raise HTTPException(404, "运行不存在")
    return repository.get_steps(run_id)


@router.get("/runs/{run_id}/events")
def get_events(
    run_id: str,
    after: float | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=1000),
):
    if not repository.get_run(run_id):
        raise HTTPException(404, "运行不存在")
    return repository.get_events(run_id, after=after, limit=limit)


@router.post("/runs/{run_id}/cancel")
async def cancel_run_route(run_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("run.control", {"run_id": run_id, "action": "cancel"}, initiator="ui")
    return respond_ui(result)


async def cancel_run(run_id: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    # 分镜 Run 可能从监制房直接取消；必须和分镜台按集取消收口到同一业务终态。
    # 已是 CANCELLED 的 Run 也允许幂等补偿，以修复旧版本留下的 scripting 锁。
    if run["workflow_type"] == "storyboard":
        from app.storyboard_workspace import finalize_storyboard_cancellation

        cancelled = run["status"] == "CANCELLED"
        if run["status"] in repository.ACTIVE_RUN_STATUSES:
            cancelled = await task_registry.cancel_and_wait("storyboard", run["scope_id"])
            if not cancelled:
                WorkflowRecorder(run_id).cancel("已取消分镜运行")
                cancelled = True
        elif not cancelled:
            raise HTTPException(409, "运行已结束，不能取消")
        finalized = finalize_storyboard_cancellation(
            run["scope_id"], run_id=run_id, message="已取消分镜运行"
        )
        return {"cancelled": cancelled, "finalized": finalized, "run": repository.get_run(run_id)}
    # 视频补齐：即使 Run 已因崩溃结束，也要允许取消并复位集状态，否则生成台会死锁
    if run["workflow_type"] == "episode_video_completion":
        from app.completion_grant import revoke_grant
        from app.video_supervisor import load_latest_checkpoint, save_checkpoint

        cancelled = False
        if run["status"] in repository.ACTIVE_RUN_STATUSES:
            cancelled = await task_registry.cancel_and_wait("video_completion", run["scope_id"])
            if not cancelled:
                WorkflowRecorder(run_id).cancel("已取消视频补齐")
                cancelled = True
        else:
            cancelled = True
        cp = load_latest_checkpoint(run["scope_id"])
        if cp:
            if cp.grant_id:
                try:
                    revoke_grant(cp.grant_id)
                except Exception:  # noqa: BLE001
                    pass
            cp.phase = "CANCELLED"
            cp.outcome = "CANCELLED"
            save_checkpoint(cp, run_id=run_id)
        try:
            get_conn().execute(
                """UPDATE episodes
                   SET video_completion_mode='quick',
                       active_video_run_id=NULL,
                       video_control_json=NULL,
                       status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
                   WHERE id=?""",
                (run["scope_id"],),
            )
            get_conn().commit()
        except Exception:  # noqa: BLE001
            pass
        return {"cancelled": cancelled, "run": repository.get_run(run_id)}
    if run["workflow_type"] == "project_video_completion_queue":
        from app import api as domain_api

        domain_api.clear_project_video_queue_pause(run["scope_id"])
        cancelled = await task_registry.cancel_and_wait(
            "video_completion_project", run["scope_id"],
        )
        current = repository.get_run(run_id)
        if not cancelled and current and current["status"] in repository.ACTIVE_RUN_STATUSES:
            WorkflowRecorder(run_id).cancel("项目补齐剩余队列已取消")
            cancelled = True
        return {
            "cancelled": cancelled,
            "current_episode_continues": True,
            "message": "已停止尚未启动的后续分集；当前单集补齐继续执行",
            "run": repository.get_run(run_id),
        }
    if run["status"] not in repository.ACTIVE_RUN_STATUSES:
        raise HTTPException(409, "运行已结束，不能取消")
    if run["workflow_type"] == "screenplay":
        cancelled = await task_registry.cancel_and_wait("screenplay", run["scope_id"])
        if not cancelled:
            WorkflowRecorder(run_id).cancel("已取消暂停中的剧本运行")
            cancelled = True
        return {"cancelled": cancelled, "run": repository.get_run(run_id)}
    if run["workflow_type"] == "character_bible":
        cancelled = await task_registry.cancel_and_wait("bible", run["scope_id"])
        if not cancelled:
            WorkflowRecorder(run_id).cancel("已取消暂停中的运行")
            cancelled = True
        return {"cancelled": cancelled, "run": repository.get_run(run_id)}
    task = task_registry.get("run", run_id)
    if task and not task.done():
        await task_registry.cancel_and_wait("run", run_id)
        return {"cancelled": True, "run": repository.get_run(run_id)}
    raise HTTPException(409, "运行没有可取消的进程内任务")


def _restart_screenplay_run(run_id: str, trigger_type: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["workflow_type"] != "screenplay" or run["scope_type"] != "episode":
        raise HTTPException(400, "不是可恢复的剧本运行")
    from app import api as domain_api

    episode = get_conn().execute(
        "SELECT * FROM episodes WHERE id=?", (run["scope_id"],)
    ).fetchone()
    if not episode:
        raise HTTPException(404, "剧集不存在")
    if episode["active_screenplay_run_id"] != run_id:
        raise HTTPException(409, {
            "code": "SCREENPLAY_RUN_NO_LONGER_OWNS_EPISODE",
            "message": "该历史运行已不再绑定当前剧本，不能恢复或重试",
            "action": "open_screenplay",
        })
    if task_registry.active("screenplay", episode["id"]):
        raise HTTPException(409, "该剧集已有剧本任务在运行")
    try:
        from app.production.revision import (
            resolve_screenplay_resume_eligibility,
        )

        eligibility = resolve_screenplay_resume_eligibility(
            episode["id"],
            conn=get_conn(),
        )
        if not eligibility.resumable:
            raise HTTPException(409, {
                "code": "SCREENPLAY_RUN_NOT_RESUMABLE",
                "message": eligibility.reason,
                "action": "open_screenplay",
            })
        is_repair = eligibility.mode == "finalize"
        recorder = domain_api._new_screenplay_recorder(
            episode["id"],
            requested_by="api",
            trigger_type=trigger_type,
            parent_run_id=run_id,
        )
        domain_api._spawn_screenplay_activation(
            episode["id"],
            recorder,
            project_id=episode["project_id"],
            status="repairing" if is_repair else "running",
            message=(
                "从任务中心继续局部修复：工作副本和检查点均已保留"
                if is_repair
                else f"从任务中心{eligibility.label}：恢复决策已锁定"
            ),
            expected_active_run_id=run_id,
            resume_eligibility=eligibility,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, {
            "code": "RUN_RESUME_START_FAILED",
            "message": "剧本恢复任务未能启动，原状态已保留，可稍后重试",
            "action": "retry_resume",
        }) from exc
    return repository.get_run(recorder.run_id)


def _restart_storyboard_run(run_id: str, trigger_type: str):
    run = repository.get_run(run_id)
    from app import api as domain_api

    episode = get_conn().execute(
        "SELECT * FROM episodes WHERE id=?", (run["scope_id"],)
    ).fetchone()
    if not episode:
        raise HTTPException(404, "剧集不存在")
    if task_registry.active("storyboard", episode["id"]):
        raise HTTPException(409, "该剧集已有分镜任务在运行")
    recorder = None
    try:
        recorder = domain_api._new_storyboard_recorder(
            episode["id"],
            requested_by="api",
            trigger_type=trigger_type,
            parent_run_id=run_id,
        )
        get_conn().execute(
            "UPDATE episodes SET status='scripting',script_error=NULL,"
            "active_storyboard_run_id=? WHERE id=?",
            (recorder.run_id, episode["id"]),
        )
        get_conn().commit()
        task_registry.spawn(
            "storyboard", episode["id"],
            domain_api._recorded_storyboard_task(
                episode["id"], recorder, resume=True, new_activation=True,
            ),
            project_id=episode["project_id"],
        )
    except Exception as exc:
        get_conn().execute(
            "UPDATE episodes SET status=?, script_error=?, active_storyboard_run_id=? WHERE id=?",
            (
                episode["status"],
                episode["script_error"],
                episode["active_storyboard_run_id"],
                episode["id"],
            ),
        )
        get_conn().commit()
        if recorder is not None:
            try:
                recorder.cancel("恢复任务启动失败，资源状态已回滚")
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "RUN_RESUME_START_FAILED",
            "message": "分镜恢复任务未能启动，原状态已保留，可稍后重试",
            "action": "retry_resume",
        }) from exc
    return repository.get_run(recorder.run_id)


def _restart_bible_run(run_id: str, trigger_type: str):
    run = repository.get_run(run_id)
    from app import api as domain_api

    project = get_conn().execute(
        "SELECT id, bible_feedback, bible_status, bible_error FROM projects WHERE id=?",
        (run["scope_id"],),
    ).fetchone()
    if not project:
        raise HTTPException(404, "项目不存在")
    if task_registry.active("bible", project["id"]):
        raise HTTPException(409, "该项目已有人物谱任务在运行")
    recorder = None
    try:
        recorder = domain_api._new_bible_recorder(
            project["id"], requested_by="api", trigger_type=trigger_type, parent_run_id=run_id
        )
        get_conn().execute(
            "UPDATE projects SET bible_status='running', bible_error=NULL WHERE id=?",
            (project["id"],),
        )
        get_conn().commit()
        task_registry.spawn(
            "bible", project["id"],
            domain_api._recorded_bible_task(
                project["id"], project["bible_feedback"] or "", recorder, trigger_full_refs=True
            ),
            project_id=project["id"],
        )
    except Exception as exc:
        get_conn().execute(
            "UPDATE projects SET bible_status=?, bible_error=? WHERE id=?",
            (project["bible_status"], project["bible_error"], project["id"]),
        )
        get_conn().commit()
        if recorder is not None:
            try:
                recorder.cancel("恢复任务启动失败，资源状态已回滚")
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "RUN_RESUME_START_FAILED",
            "message": "人物谱恢复任务未能启动，原状态已保留，可稍后重试",
            "action": "retry_resume",
        }) from exc
    return repository.get_run(recorder.run_id)


async def _restart_video_completion_run(run_id: str, trigger_type: str):
    run = repository.get_run(run_id)
    from app import api as domain_api
    from app.completion_grant import GrantValidationError, validate_video_grant
    from app.video_supervisor import load_latest_checkpoint

    if run.get("recovered_by_run_id"):
        raise HTTPException(409, {
            "code": "RUN_ALREADY_RECOVERED",
            "message": "该运行已创建续跑任务，请查看最新运行",
            "recovered_by_run_id": run["recovered_by_run_id"],
            "action": "open_recovered_run",
        })
    if trigger_type == "retry":
        raise HTTPException(409, {
            "code": "VIDEO_COMPLETION_RETRY_REQUIRES_NEW_AUTHORIZATION",
            "message": "已结束的补齐运行不能沿用旧授权续跑，请重新授权后发起全片补齐",
            "action": "start_fresh",
        })
    episode = get_conn().execute(
        "SELECT id, storyboard_artifact_id FROM episodes WHERE id=?",
        (run["scope_id"],),
    ).fetchone()
    if not episode:
        raise HTTPException(404, "剧集不存在")
    if task_registry.active("video_completion", episode["id"]):
        raise HTTPException(409, "该剧集已有全片补齐任务在运行")
    cp = load_latest_checkpoint(episode["id"])
    if not cp or not cp.grant_id:
        raise HTTPException(409, {
            "code": "VIDEO_COMPLETION_RESUME_CONTEXT_MISSING",
            "message": "未找到可续跑的补齐检查点或授权，请重新发起全片补齐",
            "action": "start_fresh",
        })
    checkpoint_run_id = getattr(cp, "run_id", None)
    if checkpoint_run_id and checkpoint_run_id != run_id:
        raise HTTPException(409, {
            "code": "VIDEO_COMPLETION_CHECKPOINT_MOVED",
            "message": "该运行已有更新的续跑检查点，请查看最新运行",
            "checkpoint_run_id": checkpoint_run_id,
            "action": "open_latest_run",
        })
    try:
        validate_video_grant(
            cp.grant_id,
            episode_id=episode["id"],
            storyboard_artifact_id=episode["storyboard_artifact_id"],
        )
    except GrantValidationError as exc:
        raise HTTPException(409, {
            "code": exc.code,
            "message": str(exc),
            "action": "renew_authorization",
            "completion_grant_id": cp.grant_id,
        }) from exc
    result = await domain_api._complete_episode_core(
        episode["id"],
        {"mode": "resume", "completion_grant_id": cp.grant_id},
        parent_run_id=run_id,
        trigger_type=trigger_type,
    )
    return repository.get_run(result["run_id"])


def _restart_media_run(
    run_id: str,
    trigger_type: str,
    *,
    allow_new_submission: bool = False,
):
    run = repository.get_run(run_id)
    rows = get_conn().execute(
        "SELECT * FROM jobs WHERE run_id=? ORDER BY updated_at DESC",
        (run_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, {
            "code": "MEDIA_RUN_JOB_MISSING",
            "message": "运行没有可恢复的媒体任务记录，请从源页面重新发起",
            "action": "open_source",
        })
    job = dict(rows[0])
    if job["status"] in {"queued", "running", "waiting_provider"}:
        return {
            "run": run,
            "job": job,
            "idempotent": True,
            "message": "媒体任务已在持久队列中，无需重复恢复",
        }
    if trigger_type != "resume":
        raise HTTPException(409, {
            "code": "MEDIA_RUN_RETRY_REQUIRES_NEW_VERSION",
            "message": "已失败或取消的视频运行不能原地重试，请从生成台创建新视频版本",
            "action": "create_new_version",
            "job_id": job["id"],
        })
    from app.system_api import retry_job

    result = retry_job(
        job["id"],
        {"allow_new_submission": bool(allow_new_submission)},
    )
    return {
        "run": repository.get_run(run_id),
        "job": result["job"],
        "retryability": result["retryability"],
        "accepted": True,
    }


def _restart_project_video_queue_run(run_id: str, trigger_type: str):
    from app import api as domain_api

    run = repository.get_run(run_id)
    if run.get("recovered_by_run_id"):
        raise HTTPException(409, {
            "code": "RUN_ALREADY_RECOVERED",
            "message": "该项目队列已创建续跑任务，请查看最新运行",
            "recovered_by_run_id": run["recovered_by_run_id"],
            "action": "open_recovered_run",
        })
    if task_registry.active("video_completion_project", run["scope_id"]):
        raise HTTPException(409, "该项目已有补齐队列在运行")
    try:
        snapshot = run.get("config_snapshot") or json.loads(
            run.get("config_snapshot_json") or "{}"
        )
        state = snapshot["queue_state"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(409, {
            "code": "PROJECT_VIDEO_QUEUE_CONTEXT_MISSING",
            "message": "项目补齐队列缺少恢复参数，请重新提交项目补齐",
            "action": "start_fresh",
        }) from exc
    state = json.loads(json.dumps(state, ensure_ascii=False))
    if trigger_type == "retry":
        for item in state.get("plan") or []:
            if item.get("status") in {
                "partial", "failed", "cancelled",
                "failed_to_schedule", "skipped_budget",
            }:
                item["status"] = "queued"
                for field in (
                    "run_id",
                    "completion_grant_id",
                    "child_run_status",
                    "child_failure_code",
                    "child_message",
                    "error",
                ):
                    item.pop(field, None)
    recorder = WorkflowRecorder.create(
        workflow_type="project_video_completion_queue",
        scope_type="project",
        scope_id=run["scope_id"],
        input_fingerprint=run["input_fingerprint"],
        requested_by="user",
        trigger_type=trigger_type,
        policy_snapshot=run.get("policy_snapshot") or {},
        config_snapshot={"queue_state": state},
        budget_limit_cny=run.get("budget_limit_cny"),
        parent_run_id=run_id,
    )
    domain_api.clear_project_video_queue_pause(run["scope_id"])
    coro = domain_api._run_project_video_completion_queue(
        run["scope_id"], state, recorder,
    )
    try:
        task_registry.spawn(
            "video_completion_project",
            run["scope_id"],
            coro,
            project_id=run["scope_id"],
        )
    except Exception as exc:
        coro.close()
        try:
            recorder.cancel("项目补齐队列恢复任务未能启动")
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(503, {
            "code": "PROJECT_VIDEO_QUEUE_RESUME_FAILED",
            "message": "项目补齐队列未能恢复，持久计划仍保留，可稍后重试",
            "action": "retry_resume",
        }) from exc
    return repository.get_run(recorder.run_id)


async def _restart_run(
    run_id: str,
    trigger_type: str,
    *,
    allow_new_submission: bool = False,
):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["workflow_type"] == "screenplay":
        return _restart_screenplay_run(run_id, trigger_type)
    if run["workflow_type"] == "storyboard":
        return _restart_storyboard_run(run_id, trigger_type)
    if run["workflow_type"] == "character_bible":
        return _restart_bible_run(run_id, trigger_type)
    if run["workflow_type"] == "episode_video_completion":
        return await _restart_video_completion_run(run_id, trigger_type)
    if run["workflow_type"] == "project_video_completion_queue":
        return _restart_project_video_queue_run(run_id, trigger_type)
    if run["workflow_type"] in {"video_generation", "scene_generation"}:
        return _restart_media_run(
            run_id,
            trigger_type,
            allow_new_submission=allow_new_submission,
        )
    raise HTTPException(400, "当前工作流尚未接入恢复适配器")


@router.post("/runs/{run_id}/resume")
async def resume_run_route(run_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch(
        "run.control",
        {
            "run_id": run_id,
            "action": "resume",
            "allow_new_submission": bool((body or {}).get("allow_new_submission")),
        },
        initiator="ui",
    )
    return respond_ui(result)


async def resume_run(run_id: str, *, allow_new_submission: bool = False):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["status"] not in {
        "PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_RETRY",
        "WAITING_HUMAN", "WAITING_AUTHORIZATION",
    }:
        raise HTTPException(409, "当前状态不能恢复")
    return await _restart_run(
        run_id,
        "resume",
        allow_new_submission=allow_new_submission,
    )


@router.post("/runs/{run_id}/pause")
async def pause_run_route(run_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("run.control", {"run_id": run_id, "action": "pause"}, initiator="ui")
    return respond_ui(result)


async def pause_run(run_id: str):
    """协作式暂停：当前安全步骤完成后进入 PAUSED_EXTERNAL。"""
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["status"] not in repository.ACTIVE_RUN_STATUSES:
        if run["workflow_type"] == "episode_video_completion":
            raise HTTPException(409, "补齐运行已结束。请点「取消」复位面板，或清空本集后重试")
        raise HTTPException(409, "运行已结束，不能暂停")
    if run["workflow_type"] == "storyboard":
        from app.storyboard_control import request_control
        request_control(run["scope_id"], "pause")
        repository.append_event(
            run_id, "SUPERVISOR_PAUSE_REQUESTED", "info", "已请求暂停（当前安全步骤后生效）",
            payload={"episode_id": run["scope_id"]},
        )
        return {"paused_requested": True, "run": repository.get_run(run_id)}
    if run["workflow_type"] == "episode_video_completion":
        from app.video_control import request_control
        request_control(run["scope_id"], "pause")
        repository.append_event(
            run_id, "VIDEO_SUPERVISOR_PAUSED", "info", "已请求暂停视频补齐",
            payload={"episode_id": run["scope_id"]},
        )
        return {"paused_requested": True, "run": repository.get_run(run_id)}
    if run["workflow_type"] == "project_video_completion_queue":
        from app import api as domain_api

        domain_api.request_project_video_queue_pause(run["scope_id"])
        stopped = await task_registry.cancel_and_wait(
            "video_completion_project", run["scope_id"],
        )
        if not stopped and run["status"] == "RUNNING":
            WorkflowRecorder(run_id).pause_external("用户暂停，项目补齐剩余队列已保留")
            get_conn().execute(
                "UPDATE workflow_runs SET failure_code='USER_PAUSED' WHERE id=?",
                (run_id,),
            )
            get_conn().commit()
        return {
            "paused": True,
            "current_episode_continues": True,
            "run": repository.get_run(run_id),
        }
    raise HTTPException(400, "当前工作流不支持暂停")


@router.post("/runs/{run_id}/handoff")
async def handoff_run_route(run_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("run.control", {"run_id": run_id, "action": "handoff"}, initiator="ui")
    return respond_ui(result)


async def handoff_run(run_id: str):
    """停止自动修复，转人工；保留已验证 checkpoint 与问题清单。"""
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["workflow_type"] == "episode_video_completion":
        from app.video_control import request_control
        from app.video_supervisor import load_latest_checkpoint, save_checkpoint
        from app.orchestration.state_machine import transition_run

        request_control(run["scope_id"], "handoff")
        repository.append_event(
            run_id, "VIDEO_SUPERVISOR_HANDOFF", "info", "已请求转人工",
            payload={"episode_id": run["scope_id"]},
        )
        if run["status"] in {"PAUSED_EXTERNAL", "WAITING_RETRY", "WAITING_HUMAN"} or not task_registry.active(
            "video_completion", run["scope_id"]
        ):
            cp = load_latest_checkpoint(run["scope_id"])
            if cp:
                cp.phase = "WAITING_HUMAN"
                save_checkpoint(cp, run_id=run_id)
            try:
                if run["status"] == "RUNNING":
                    transition_run(run_id, "RUNNING", "WAITING_HUMAN", "user_handoff")
                elif run["status"] in {"PAUSED_EXTERNAL", "WAITING_RETRY"}:
                    transition_run(run_id, run["status"], "WAITING_HUMAN", "user_handoff")
            except Exception:  # noqa: BLE001
                pass
        return {"handoff_requested": True, "run": repository.get_run(run_id)}
    if run["workflow_type"] != "storyboard":
        raise HTTPException(400, "当前仅分镜/视频 Supervisor 支持转人工")
    from app.storyboard_control import request_control

    request_control(run["scope_id"], "handoff")
    repository.append_event(
        run_id, "SUPERVISOR_HANDOFF_REQUESTED", "info", "已请求转人工",
        payload={"episode_id": run["scope_id"]},
    )
    # 若任务已不在跑（已在 WAITING_*），直接标记
    if run["status"] in {"PAUSED_EXTERNAL", "WAITING_RETRY", "WAITING_HUMAN"} or not task_registry.active(
        "storyboard", run["scope_id"]
    ):
        from app.storyboard_supervisor import load_latest_checkpoint, save_checkpoint
        from app.orchestration.state_machine import transition_run

        cp = load_latest_checkpoint(run["scope_id"])
        if cp:
            cp.phase = "WAITING_HUMAN"
            save_checkpoint(cp, run_id=run_id)
        try:
            if run["status"] == "RUNNING":
                transition_run(run_id, "RUNNING", "WAITING_HUMAN", "user_handoff")
            elif run["status"] in {"PAUSED_EXTERNAL", "WAITING_RETRY"}:
                transition_run(run_id, run["status"], "WAITING_HUMAN", "user_handoff")
        except Exception:  # noqa: BLE001
            pass
        get_conn().execute(
            "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
            ("已转人工处理：自动修复已停止，已验证镜头与问题清单已保留", run["scope_id"]),
        )
        get_conn().commit()
    return {"handoff_requested": True, "run": repository.get_run(run_id)}


@router.post("/runs/{run_id}/retry")
async def retry_run_route(run_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch(
        "run.control",
        {
            "run_id": run_id,
            "action": "retry",
            "allow_new_submission": bool((body or {}).get("allow_new_submission")),
        },
        initiator="ui",
    )
    return respond_ui(result)


async def retry_run(run_id: str, *, allow_new_submission: bool = False):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["status"] not in {"FAILED", "PARTIAL", "CANCELLED"}:
        raise HTTPException(409, "只有失败、部分完成或已取消的运行可以受控重试")
    return await _restart_run(
        run_id,
        "retry",
        allow_new_submission=allow_new_submission,
    )


@router.get("/projects/{project_id}/storyboard-metrics")
def project_storyboard_metrics(project_id: str):
    """批量分镜并发与 Supervisor 指标（EpisodesPage 运行条）。"""
    conn = get_conn()
    project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, "项目不存在")
    episodes = conn.execute(
        "SELECT id, episode_no, title, status, script_error, active_storyboard_run_id, "
        "screenplay_status FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,),
    ).fetchall()
    from app.storyboard_supervisor import load_latest_checkpoint

    # storyboard run 的 scope_type=episode，不能用 list_runs(project_id=…)（那只匹配 project scope）
    active_run_ids: set[str] = set()
    for ep in episodes:
        rid = ep["active_storyboard_run_id"]
        if rid:
            run = repository.get_run(rid)
            if run and run.get("status") in repository.ACTIVE_RUN_STATUSES:
                active_run_ids.add(rid)
    rows = []
    phase_counts: dict[str, int] = {}
    for ep in episodes:
        cp = load_latest_checkpoint(ep["id"])
        phase = cp.phase if cp else None
        tracked = ep["status"] == "scripting" or (
            phase and phase not in {"SUCCEEDED", "CANCELLED", "CREATED"}
        )
        if not tracked:
            continue
        if phase:
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        rows.append({
            "episode_id": ep["id"],
            "episode_no": ep["episode_no"],
            "title": ep["title"],
            "status": ep["status"],
            "phase": phase,
            "repair_epoch": cp.repair_epoch if cp else 0,
            "validated_prefix_end": cp.validated_prefix_end if cp else 0,
            "expected_total": cp.expected_total if cp else 0,
            "run_id": ep["active_storyboard_run_id"] or None,
        })
    return {
        "project_id": project_id,
        "active_storyboard_runs": len(active_run_ids),
        "scripting_episodes": sum(1 for e in episodes if e["status"] == "scripting"),
        "phase_counts": phase_counts,
        "waiting_human": phase_counts.get("WAITING_HUMAN", 0),
        "paused": (
            phase_counts.get("PAUSED_EXTERNAL", 0)
            + phase_counts.get("PAUSED_BUDGET", 0)
        ),
        "waiting_authorization": phase_counts.get("WAITING_AUTHORIZATION", 0),
        "repairing": phase_counts.get("REPAIRING", 0),
        "episodes": rows,
    }


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str):
    artifact = repository.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "证据产物不存在")
    return {**artifact, "evaluations": repository.get_evaluations(artifact_id)}


@router.get("/artifacts/{artifact_id}/evals")
def get_artifact_evaluations(artifact_id: str):
    if not repository.get_artifact(artifact_id):
        raise HTTPException(404, "证据产物不存在")
    return repository.get_evaluations(artifact_id)


@router.get("/artifacts/{artifact_id}/lineage")
def get_artifact_lineage(artifact_id: str):
    if not repository.get_artifact(artifact_id):
        raise HTTPException(404, "证据产物不存在")
    return repository.get_lineage(artifact_id)


@router.get("/gates")
def list_pending_gates(
    project_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    return repository.pending_human_gates(project_id=project_id, limit=limit)


@router.post("/gates/{artifact_id}/decision")
def decide_gate(artifact_id: str, body: dict = Body(...)):
    """统一门禁决策入口；以 artifact version + 幂等键保护并发和重复提交。"""
    from app.harness.types import Evaluation
    from app.monitoring import audit

    decision = str(body.get("decision") or "").strip()
    reason = str(body.get("reason") or "").strip()
    decided_by = str(body.get("decided_by") or "monitor_user").strip()
    expected_version = body.get("expected_version")
    idem = str(body.get("idempotency_key") or "").strip()
    if decision not in {"approve", "reject", "approve_with_risk"}:
        raise HTTPException(422, "门禁决定只允许批准、带风险批准或打回")
    if not reason:
        raise HTTPException(422, "请填写处理意见")
    conn = get_conn()
    artifact = repository.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "门禁产物不存在")
    if expected_version is not None and int(expected_version) != int(artifact["version"]):
        raise HTTPException(409, detail={
            "code": "GATE_VERSION_CONFLICT", "message": "证据版本已变化，请刷新后重试",
            "current_version": artifact["version"],
        })
    existing = conn.execute(
        "SELECT * FROM gate_decisions WHERE artifact_id=? ORDER BY created_at DESC LIMIT 1",
        (artifact_id,),
    ).fetchone()
    if existing:
        existing_dict = dict(existing)
        if existing_dict["decision"] == decision:
            return {"ok": True, "idempotent": True, "decision": existing_dict}
        raise HTTPException(409, detail={
            "code": "GATE_ALREADY_DECIDED", "message": "该门禁已由其他人处理",
            "current_decision": existing_dict["decision"],
        })
    domain_publish_labels = {
        "episode_screenplay": "剧本台",
        "storyboard": "分镜台",
    }
    domain_publish_label = domain_publish_labels.get(str(artifact["type"]))
    if domain_publish_label:
        raise HTTPException(409, detail={
            "code": "DOMAIN_PUBLISH_FLOW_REQUIRED",
            "message": (
                f"{artifact['type']} 由 {domain_publish_label} 的正式发布事务管理；"
                f"请回到 {domain_publish_label} 使用页面按钮处理，通用门禁不会直接改写业务状态"
            ),
            "artifact_type": artifact["type"],
            "workspace": domain_publish_label,
        })
    gate_key = {
        "character_bible": "character_bible", "episode_screenplay": "screenplay",
        "storyboard": "storyboard", "delivery_package": "delivery",
    }.get(artifact["type"], artifact["type"])
    step = conn.execute(
        "SELECT run_id FROM step_runs WHERE id=?", (artifact.get("created_by_step_run_id"),)
    ).fetchone() if artifact.get("created_by_step_run_id") else None
    run_id = step["run_id"] if step else None
    if artifact["type"] == "delivery_package":
        package = conn.execute(
            "SELECT id,episode_id FROM delivery_packages WHERE artifact_id=? AND status='waiting_human' ORDER BY created_at DESC LIMIT 1",
            (artifact_id,),
        ).fetchone()
        if not package:
            raise HTTPException(409, "交付包已不在待审核状态")
        from app.delivery import approve_delivery
        try:
            result = approve_delivery(
                package["episode_id"], decided_by=decided_by, decision=decision,
                reason=reason, accepted_risk=body.get("accepted_risk"), package_id=package["id"],
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        audit("gate_decision", "artifact", artifact_id, decision, {"idempotency_key": idem})
        return {"ok": True, "idempotent": False, "decision": decision, "result": result}
    if decision in {"approve", "approve_with_risk"}:
        repository.commit_artifact(None, artifact_id, [Evaluation(
            evaluator_type="human", evaluator_name=decided_by, evaluator_version="monitor-1.0",
            status="warning" if decision == "approve_with_risk" else "passed",
            hard_gate_passed=True, score=100,
            evidence={"decision": decision, "reason": reason, "idempotency_key": idem},
        )])
    else:
        conn.execute("UPDATE artifacts SET status='rejected' WHERE id=?", (artifact_id,))
    conn.execute(
        """INSERT INTO gate_decisions(id,artifact_id,run_id,gate_key,decision,decided_by,reason,accepted_risk,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (new_id("gate"), artifact_id, run_id, gate_key, decision, decided_by, reason,
         body.get("accepted_risk"), now()),
    )
    if artifact["scope_type"] == "project" and artifact["type"] == "character_bible":
        if decision.startswith("approve"):
            conn.execute(
                "UPDATE projects SET bible_json=?,bible_artifact_id=?,bible_status='ready',bible_error=NULL WHERE id=?",
                (json.dumps(artifact.get("content") or {}, ensure_ascii=False), artifact_id, artifact["scope_id"]),
            )
        else:
            conn.execute("UPDATE projects SET bible_status='failed',bible_error=? WHERE id=?", (reason, artifact["scope_id"]))
    conn.commit()
    if run_id:
        repository.append_event(run_id, "HUMAN_GATE_DECIDED", "info", f"人工门禁已{decision}", payload={
            "artifact_id": artifact_id, "decision": decision, "decided_by": decided_by,
        })
    audit("gate_decision", "artifact", artifact_id, decision, {"idempotency_key": idem})
    return {"ok": True, "idempotent": False, "decision": decision, "artifact_id": artifact_id, "run_id": run_id}


@router.get("/episodes/{episode_id}/delivery/readiness")
def get_delivery_readiness(episode_id: str):
    from app.delivery import delivery_readiness

    try:
        return delivery_readiness(episode_id)
    except KeyError as exc:
        raise HTTPException(404, "剧集不存在") from exc


@router.post("/episodes/{episode_id}/delivery/package")
async def create_delivery_package(episode_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route

    payload = dict(body) if isinstance(body, dict) else {}
    routed = await ui_route("delivery.create_package", {
        "episode_id": episode_id,
        "idempotency_key": payload.get("idempotency_key"),
        "request_id": payload.get("request_id"),
    })
    if routed is not None:
        return routed
    from app.delivery import build_delivery_package, validate_package_id

    # package_id 必须服务端可控：忽略客户端自带路径穿越载荷，仅允许恢复场景沿用已校验 id。
    raw_package_id = payload.get("package_id")
    if raw_package_id:
        try:
            payload["package_id"] = validate_package_id(str(raw_package_id))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not idempotency_key:
            raise HTTPException(422, "生成交付包必须提供稳定的 idempotency_key")
        digest = hashlib.sha256(
            f"{episode_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        payload["package_id"] = f"delivery_{digest}"
    operation_request_fingerprint = fingerprint(
        episode_id,
        {
            key: payload[key]
            for key in sorted(payload)
            if key not in {"request_id", "operation_started_at", "package_id"}
        },
    )
    from app.delivery import (
        claim_delivery_package_operation,
        finish_delivery_package_operation,
    )

    operation_lease_owner, recovered_result = claim_delivery_package_operation(
        package_id=payload["package_id"],
        episode_id=episode_id,
        request_fingerprint=operation_request_fingerprint,
    )
    if recovered_result is not None:
        return recovered_result
    assert operation_lease_owner is not None
    payload.setdefault("operation_started_at", now())
    def mark_operation_failed(exc: Exception) -> None:
        try:
            finish_delivery_package_operation(
                package_id=payload["package_id"],
                request_fingerprint=operation_request_fingerprint,
                lease_owner=operation_lease_owner,
                result={"error": str(exc)},
                succeeded=False,
            )
        except ValueError:
            pass
    if not get_conn().execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
        raise HTTPException(404, "剧集不存在")
    recorder = WorkflowRecorder.create(
        workflow_type="delivery_package",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(episode_id, payload),
        requested_by=str(payload.get("decided_by") or "user"),
        trigger_type="manual",
        policy_snapshot={
            "shot_duration_range_s": [config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S],
            "shot_duration_decided_by": "model",
            "immutable_snapshot": True,
        },
        config_snapshot={"recovery_payload": payload},
    )
    recorder.start()
    try:
        async def operation():
            return await asyncio.to_thread(
                build_delivery_package,
                episode_id,
                package_id=payload["package_id"],
                decided_by=payload.get("decided_by"),
                decision=payload.get("decision"),
                reason=str(payload.get("reason") or ""),
                accepted_risk=payload.get("accepted_risk"),
                operation_started_at=payload["operation_started_at"],
                operation_request_fingerprint=operation_request_fingerprint,
                operation_lease_owner=operation_lease_owner,
            )

        _, result = await recorder.step(
            "build_delivery_snapshot", operation,
            agent_name="delivery_loop",
            context_manifest={"immutable_snapshot": True},
        )
        recorder.succeed("交付快照已生成")
        response = {**result, "run_id": recorder.run_id}
        finish_delivery_package_operation(
            package_id=payload["package_id"],
            request_fingerprint=operation_request_fingerprint,
            lease_owner=operation_lease_owner,
            result=response,
            succeeded=True,
        )
        return response
    except KeyError as exc:
        recorder.fail(exc)
        mark_operation_failed(exc)
        raise HTTPException(404, "剧集不存在") from exc
    except ValueError as exc:
        recorder.fail(exc)
        mark_operation_failed(exc)
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        recorder.fail(exc)
        mark_operation_failed(exc)
        raise


@router.get("/episodes/{episode_id}/delivery/packages")
def list_delivery_packages(episode_id: str):
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
        raise HTTPException(404, "剧集不存在")
    return [dict(row) for row in conn.execute(
        "SELECT * FROM delivery_packages WHERE episode_id=? ORDER BY created_at DESC", (episode_id,)
    ).fetchall()]


def _delivery_file(package_id: str, filename: str) -> Path:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "交付包不存在")
    current = conn.execute(
        "SELECT delivery_artifact_id FROM episodes WHERE id=?",
        (row["episode_id"],),
    ).fetchone()
    if (
        row["status"] not in {"waiting_human", "approved"}
        or current is None
        or current["delivery_artifact_id"] != row["artifact_id"]
    ):
        raise HTTPException(409, "交付包已不是当前可下载权威")
    path = Path(row["package_path"]).resolve()
    target = (path / filename).resolve()
    if path not in target.parents or not target.is_file():
        raise HTTPException(404, "交付文件不存在")
    try:
        manifest = json.loads(row["manifest_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(409, "交付 manifest 已损坏") from exc
    item = next(
        (entry for entry in manifest.get("files") or [] if entry.get("path") == filename),
        None,
    )
    from app.delivery import _sha256

    if (
        item is None
        or _sha256(target) != str(item.get("sha256") or "")
        or target.stat().st_size != int(item.get("size_bytes") or -1)
    ):
        raise HTTPException(409, "交付文件与已审核 manifest 不一致")
    return target


@router.get("/delivery/packages/{package_id}/report")
def download_delivery_report(package_id: str):
    path = _delivery_file(package_id, "quality-report.html")
    return FileResponse(path, media_type="text/html", filename=f"{package_id}-quality-report.html")


@router.get("/delivery/packages/{package_id}/archive")
def download_delivery_archive(package_id: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "交付包不存在")
    current = conn.execute(
        "SELECT delivery_artifact_id FROM episodes WHERE id=?",
        (row["episode_id"],),
    ).fetchone()
    if (
        row["status"] not in {"waiting_human", "approved"}
        or current is None
        or current["delivery_artifact_id"] != row["artifact_id"]
    ):
        raise HTTPException(409, "交付包已不是当前可下载权威")
    archive = Path(str(row["package_path"]) + ".zip").resolve()
    if not archive.is_file():
        raise HTTPException(404, "交付压缩包不存在")
    from app.delivery import _archive_matches_directory, _sha256

    package_path = Path(str(row["package_path"])).resolve()
    if not _archive_matches_directory(archive, package_path):
        raise HTTPException(409, "交付压缩包与已审核目录不一致")
    evaluation = conn.execute(
        """SELECT evidence_json FROM evaluations
           WHERE artifact_id=? AND evaluator_type='human'
             AND hard_gate_passed=1
           ORDER BY created_at DESC LIMIT 1""",
        (row["artifact_id"],),
    ).fetchone()
    if evaluation is not None:
        try:
            evidence = json.loads(evaluation["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        if _sha256(archive) != str(evidence.get("approved_archive_sha256") or ""):
            raise HTTPException(409, "交付压缩包与已批准证据不一致")
    return FileResponse(archive, media_type="application/zip", filename=f"{package_id}.zip")


@router.post("/episodes/{episode_id}/delivery/approve")
async def decide_delivery(episode_id: str, body: dict = Body(...)):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "delivery.review",
        {
            "episode_id": episode_id,
            "package_id": body.get("package_id"),
            "decision": body.get("decision"),
            "reason": body.get("reason"),
            "accepted_risk": body.get("accepted_risk"),
            "idempotency_key": body.get("idempotency_key"),
            "request_id": body.get("request_id"),
        },
    )
    if routed is not None:
        return routed
    from app.delivery import approve_delivery, validate_package_id

    if not get_conn().execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
        raise HTTPException(404, "剧集不存在")
    payload = dict(body)
    # 批准产出的新快照 id 一律服务端生成，禁止客户端注入路径。
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise HTTPException(422, "交付审批必须提供稳定的 idempotency_key")
    approval_digest = hashlib.sha256(
        f"{episode_id}\0delivery.review\0{idempotency_key}".encode("utf-8")
    ).hexdigest()[:24]
    payload["approved_package_id"] = f"delivery_{approval_digest}"
    if payload.get("package_id"):
        try:
            payload["package_id"] = validate_package_id(str(payload["package_id"]))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    payload.setdefault("operation_started_at", now())
    recorder = WorkflowRecorder.create(
        workflow_type="delivery_approval",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(episode_id, payload),
        requested_by=str(payload.get("decided_by") or "user"),
        trigger_type="human_gate",
        policy_snapshot={"immutable_snapshot": True},
        config_snapshot={"recovery_payload": payload},
    )
    recorder.start()
    try:
        async def operation():
            return await asyncio.to_thread(
                approve_delivery,
                episode_id,
                decided_by=str(payload.get("decided_by") or "user"),
                decision=str(payload.get("decision") or ""),
                reason=str(payload.get("reason") or ""),
                accepted_risk=payload.get("accepted_risk"),
                approved_package_id=payload["approved_package_id"],
                operation_started_at=payload["operation_started_at"],
                package_id=payload.get("package_id"),
            )

        _, result = await recorder.step(
            "apply_delivery_gate", operation,
            agent_name="delivery_loop",
            context_manifest={"decision": payload.get("decision"), "immutable_snapshot": True},
        )
        recorder.succeed("交付门禁已处理")
        return {**result, "run_id": recorder.run_id}
    except ValueError as exc:
        recorder.fail(exc)
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        recorder.fail(exc)
        raise


async def _resume_delivery_package(
    episode_id: str, payload: dict, recorder: WorkflowRecorder
) -> None:
    from app.delivery import (
        build_delivery_package,
        claim_delivery_package_operation,
        finish_delivery_package_operation,
    )

    operation_request_fingerprint = fingerprint(
        episode_id,
        {
            key: payload[key]
            for key in sorted(payload)
            if key not in {"request_id", "operation_started_at", "package_id"}
        },
    )
    operation_owner, recovered_result = claim_delivery_package_operation(
        package_id=payload["package_id"],
        episode_id=episode_id,
        request_fingerprint=operation_request_fingerprint,
        allow_interrupted_takeover=True,
    )
    if recovered_result is not None:
        recorder.start()
        recorder.succeed("交付快照已按 durable receipt 恢复")
        return
    assert operation_owner is not None

    recorder.start()
    try:
        async def operation():
            return await asyncio.to_thread(
                build_delivery_package,
                episode_id,
                package_id=payload["package_id"],
                decided_by=payload.get("decided_by"),
                decision=payload.get("decision"),
                reason=str(payload.get("reason") or ""),
                accepted_risk=payload.get("accepted_risk"),
                operation_started_at=payload["operation_started_at"],
                operation_request_fingerprint=operation_request_fingerprint,
                operation_lease_owner=operation_owner,
            )

        _, result = await recorder.step(
            "build_delivery_snapshot", operation,
            agent_name="delivery_loop",
            context_manifest={"immutable_snapshot": True, "recovered": True},
        )
        recorder.succeed("交付快照已从服务重启中恢复")
        finish_delivery_package_operation(
            package_id=payload["package_id"],
            request_fingerprint=operation_request_fingerprint,
            lease_owner=operation_owner,
            result={**result, "run_id": recorder.run_id},
            succeeded=True,
        )
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，交付快照等待自动恢复")
        else:
            recorder.cancel("交付快照恢复已取消")
        raise
    except Exception as exc:  # noqa: BLE001 recovery failure must remain visible
        recorder.fail(exc)
        try:
            finish_delivery_package_operation(
                package_id=payload["package_id"],
                request_fingerprint=operation_request_fingerprint,
                lease_owner=operation_owner,
                result={"error": str(exc)},
                succeeded=False,
            )
        except ValueError:
            pass


async def _resume_delivery_approval(
    episode_id: str, payload: dict, recorder: WorkflowRecorder
) -> None:
    from app.delivery import approve_delivery

    recorder.start()
    try:
        async def operation():
            return await asyncio.to_thread(
                approve_delivery,
                episode_id,
                decided_by=str(payload.get("decided_by") or "user"),
                decision=str(payload.get("decision") or ""),
                reason=str(payload.get("reason") or ""),
                accepted_risk=payload.get("accepted_risk"),
                approved_package_id=payload["approved_package_id"],
                operation_started_at=payload["operation_started_at"],
                package_id=payload.get("package_id"),
                allow_interrupted_takeover=True,
            )

        await recorder.step(
            "apply_delivery_gate", operation,
            agent_name="delivery_loop",
            context_manifest={
                "decision": payload.get("decision"),
                "immutable_snapshot": True,
                "recovered": True,
            },
        )
        recorder.succeed("交付门禁已从服务重启中恢复")
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，交付门禁等待自动恢复")
        else:
            recorder.cancel("交付门禁恢复已取消")
        raise
    except Exception as exc:  # noqa: BLE001 recovery failure must remain visible
        recorder.fail(exc)


def recover_delivery_tasks() -> int:
    """Resume file-building HTTP tasks whose client connection died on restart."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT wr.*, e.project_id
           FROM workflow_runs wr
           JOIN episodes e ON wr.scope_type='episode' AND e.id=wr.scope_id
           WHERE wr.workflow_type IN ('delivery_package','delivery_approval')
             AND wr.status='PAUSED_EXTERNAL'
             AND wr.failure_code='SERVICE_RESTART'
             AND wr.recovered_by_run_id IS NULL
           ORDER BY wr.updated_at"""
    ).fetchall()
    resumed = 0
    for row in rows:
        try:
            snapshot = json.loads(row["config_snapshot_json"] or "{}")
            payload = snapshot["recovery_payload"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            conn.execute(
                "UPDATE workflow_runs SET failure_message=? WHERE id=?",
                ("旧任务缺少可重放参数，需人工重新发起", row["id"]),
            )
            conn.commit()
            continue
        recorder = None
        operation = None
        try:
            recorder = WorkflowRecorder.create(
                workflow_type=row["workflow_type"],
                scope_type="episode",
                scope_id=row["scope_id"],
                input_fingerprint=row["input_fingerprint"],
                requested_by="system",
                trigger_type="resume",
                policy_snapshot=json.loads(row["policy_snapshot_json"] or "{}"),
                config_snapshot={"recovery_payload": payload},
                parent_run_id=row["id"],
            )
            operation = (
                _resume_delivery_package(row["scope_id"], payload, recorder)
                if row["workflow_type"] == "delivery_package"
                else _resume_delivery_approval(row["scope_id"], payload, recorder)
            )
            task_registry.spawn(
                "run", recorder.run_id, operation, project_id=row["project_id"]
            )
            resumed += 1
        except Exception as exc:  # one broken recovery must not block later packages
            if operation is not None:
                operation.close()
            if recorder is not None:
                try:
                    recorder.cancel("交付恢复任务未能启动")
                except Exception:  # noqa: BLE001
                    pass
            get_conn().execute(
                "UPDATE workflow_runs SET failure_message=? WHERE id=?",
                (
                    f"交付恢复任务未能启动：{type(exc).__name__}；文件与原运行记录已保留，可重新发起",
                    row["id"],
                ),
            )
            get_conn().commit()
            continue
    return resumed


@router.post("/episodes/{episode_id}/customer-feedback")
async def create_customer_feedback(episode_id: str, body: dict = Body(...)):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "delivery.submit_feedback",
        {
            "episode_id": episode_id,
            "package_id": body.get("package_id"),
            "feedback": body.get("message") or body.get("feedback") or "",
            "request_revision": bool(body.get("request_revision", True)),
        },
    )
    if routed is not None:
        return routed
    from app.delivery import add_customer_feedback

    message = str(body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "反馈内容不能为空")
    try:
        return add_customer_feedback(
            episode_id,
            message=message,
            created_by=str(body.get("created_by") or "customer"),
            issue_code=body.get("issue_code"),
            rating=body.get("rating"),
            request_revision=bool(body.get("request_revision")),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/benchmarks")
async def run_benchmark(body: dict = Body(...)):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("system.run_benchmark", {"payload": body})
    if routed is not None:
        return routed
    from app.benchmarks import project_quality_metrics, record_benchmark

    project_id = body.get("project_id")
    if project_id and not get_conn().execute(
        "SELECT 1 FROM projects WHERE id=?", (project_id,)
    ).fetchone():
        raise HTTPException(404, "项目不存在")
    candidate_samples = body.get("candidate_samples")
    if candidate_samples is None:
        if not project_id:
            raise HTTPException(400, "需要 project_id 或 candidate_samples")
        candidate_samples = [project_quality_metrics(project_id)]
    baseline_samples = body.get("baseline_samples") or []
    if not baseline_samples:
        raise HTTPException(400, "必须提供旧链路 baseline_samples，禁止无基线宣称质量提升")
    is_real_project = bool(body.get("is_real_project"))
    attested_by = str(body.get("attested_by") or "").strip() or None
    if is_real_project and (not project_id or not attested_by):
        raise HTTPException(400, "真实项目基准必须关联项目并填写 attested_by")
    return record_benchmark(
        project_id=project_id,
        baseline_label=str(body.get("baseline_label") or "legacy"),
        candidate_label=str(body.get("candidate_label") or "harness"),
        baseline_samples=baseline_samples,
        candidate_samples=candidate_samples,
        thresholds=body.get("thresholds"),
        is_real_project=is_real_project,
        attested_by=attested_by,
        attestation_note=str(body.get("attestation_note") or "").strip() or None,
    )


@router.get("/benchmarks/release-gate")
def get_release_gate():
    from app.benchmarks import release_gate_status

    return release_gate_status()


@router.put("/projects/{project_id}/engine")
async def set_project_engine(project_id: str, body: dict = Body(...)):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "system.set_engine",
        {"project_id": project_id, "enabled": bool(body.get("enabled"))},
    )
    if routed is not None:
        return routed
    enabled = bool(body.get("enabled"))
    conn = get_conn()
    cursor = conn.execute(
        "UPDATE projects SET harness_engine_enabled=? WHERE id=?", (int(enabled), project_id)
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise HTTPException(404, "项目不存在")
    return {"project_id": project_id, "harness_engine_enabled": enabled}


def cancel_media_job(job_id: str) -> dict:
    """取消媒体 Job 的领域逻辑，供 REST 路由与 ``job.cancel`` Command Handler 共用。"""
    from app.orchestration.media_scheduler import request_cancel

    try:
        return request_cancel(job_id)
    except KeyError as exc:
        raise HTTPException(404, "媒体任务不存在") from exc


@router.post("/jobs/{job_id}/cancel")
async def cancel_media_job_route(job_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("job.cancel", {"job_id": job_id}, initiator="ui")
    return respond_ui(result)
