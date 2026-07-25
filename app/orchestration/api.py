from __future__ import annotations

import asyncio
import json
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
    if run["status"] not in repository.ACTIVE_RUN_STATUSES:
        raise HTTPException(409, "运行已结束，不能取消")
    if run["workflow_type"] == "screenplay":
        cancelled = await task_registry.cancel_and_wait("screenplay", run["scope_id"])
        if not cancelled:
            WorkflowRecorder(run_id).cancel("已取消暂停中的剧本运行")
            cancelled = True
        return {"cancelled": cancelled, "run": repository.get_run(run_id)}
    if run["workflow_type"] in {"storyboard", "character_bible"}:
        kind = "storyboard" if run["workflow_type"] == "storyboard" else "bible"
        cancelled = await task_registry.cancel_and_wait(kind, run["scope_id"])
        if not cancelled:
            WorkflowRecorder(run_id).cancel("已取消暂停中的运行")
            cancelled = True
        return {"cancelled": cancelled, "run": repository.get_run(run_id)}
    if run["workflow_type"] == "episode_video_completion":
        from app.completion_grant import revoke_grant
        from app.video_supervisor import load_latest_checkpoint, save_checkpoint

        cancelled = await task_registry.cancel_and_wait("video_completion", run["scope_id"])
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
        if not cancelled:
            WorkflowRecorder(run_id).cancel("已取消视频补齐")
            cancelled = True
        try:
            get_conn().execute(
                "UPDATE episodes SET video_completion_mode='quick' WHERE id=?",
                (run["scope_id"],),
            )
            get_conn().commit()
        except Exception:  # noqa: BLE001
            pass
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
        "SELECT id, project_id FROM episodes WHERE id=?", (run["scope_id"],)
    ).fetchone()
    if not episode:
        raise HTTPException(404, "剧集不存在")
    if task_registry.active("screenplay", episode["id"]):
        raise HTTPException(409, "该剧集已有剧本任务在运行")
    stamp = domain_api.now()
    get_conn().execute(
        "UPDATE episodes SET screenplay_status='running', screenplay_error=NULL, "
        "screenplay_started_at=?, screenplay_updated_at=? WHERE id=?",
        (stamp, stamp, episode["id"]),
    )
    get_conn().commit()
    recorder = domain_api._new_screenplay_recorder(
        episode["id"],
        requested_by="api",
        trigger_type=trigger_type,
        parent_run_id=run_id,
    )
    task_registry.spawn(
        "screenplay",
        episode["id"],
        domain_api._recorded_screenplay_task(episode["id"], recorder),
        project_id=episode["project_id"],
    )
    return repository.get_run(recorder.run_id)


def _restart_storyboard_run(run_id: str, trigger_type: str):
    run = repository.get_run(run_id)
    from app import api as domain_api
    from app.storyboard_supervisor import load_latest_checkpoint

    episode = get_conn().execute(
        "SELECT * FROM episodes WHERE id=?", (run["scope_id"],)
    ).fetchone()
    if not episode:
        raise HTTPException(404, "剧集不存在")
    if task_registry.active("storyboard", episode["id"]):
        raise HTTPException(409, "该剧集已有分镜任务在运行")
    try:
        completion_mode = episode["storyboard_completion_mode"] or "ready_for_manual_confirm"
    except (KeyError, IndexError, TypeError):
        completion_mode = "ready_for_manual_confirm"
    cp = load_latest_checkpoint(episode["id"])
    grant_id = cp.completion_grant_id if cp else None
    get_conn().execute(
        "UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode["id"],)
    )
    get_conn().commit()
    recorder = domain_api._new_storyboard_recorder(
        episode["id"],
        requested_by="api",
        trigger_type=trigger_type,
        parent_run_id=run_id,
        completion_mode=completion_mode,
    )
    try:
        get_conn().execute(
            "UPDATE episodes SET active_storyboard_run_id=? WHERE id=?",
            (recorder.run_id, episode["id"]),
        )
        get_conn().commit()
    except Exception:  # noqa: BLE001
        pass
    task_registry.spawn(
        "storyboard", episode["id"],
        domain_api._recorded_storyboard_task(
            episode["id"], recorder, resume=True,
            completion_mode=completion_mode,
            completion_grant_id=grant_id,
        ),
        project_id=episode["project_id"],
    )
    return repository.get_run(recorder.run_id)


def _restart_bible_run(run_id: str, trigger_type: str):
    run = repository.get_run(run_id)
    from app import api as domain_api

    project = get_conn().execute(
        "SELECT id, bible_feedback FROM projects WHERE id=?", (run["scope_id"],)
    ).fetchone()
    if not project:
        raise HTTPException(404, "项目不存在")
    if task_registry.active("bible", project["id"]):
        raise HTTPException(409, "该项目已有人物谱任务在运行")
    get_conn().execute(
        "UPDATE projects SET bible_status='running', bible_error=NULL WHERE id=?", (project["id"],)
    )
    get_conn().commit()
    recorder = domain_api._new_bible_recorder(
        project["id"], requested_by="api", trigger_type=trigger_type, parent_run_id=run_id
    )
    task_registry.spawn(
        "bible", project["id"],
        domain_api._recorded_bible_task(
            project["id"], project["bible_feedback"] or "", recorder, trigger_full_refs=True
        ),
        project_id=project["id"],
    )
    return repository.get_run(recorder.run_id)


def _restart_run(run_id: str, trigger_type: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["workflow_type"] == "screenplay":
        return _restart_screenplay_run(run_id, trigger_type)
    if run["workflow_type"] == "storyboard":
        return _restart_storyboard_run(run_id, trigger_type)
    if run["workflow_type"] == "character_bible":
        return _restart_bible_run(run_id, trigger_type)
    raise HTTPException(400, "当前工作流尚未接入恢复适配器")


@router.post("/runs/{run_id}/resume")
async def resume_run_route(run_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("run.control", {"run_id": run_id, "action": "resume"}, initiator="ui")
    return respond_ui(result)


async def resume_run(run_id: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["status"] not in {
        "PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_RETRY",
        "WAITING_HUMAN", "WAITING_AUTHORIZATION",
    }:
        raise HTTPException(409, "当前状态不能恢复")
    return _restart_run(run_id, "resume")


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
async def retry_run_route(run_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("run.control", {"run_id": run_id, "action": "retry"}, initiator="ui")
    return respond_ui(result)


async def retry_run(run_id: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["status"] not in {"FAILED", "PARTIAL", "CANCELLED"}:
        raise HTTPException(409, "只有失败、部分完成或已取消的运行可以受控重试")
    return _restart_run(run_id, "retry")


@router.get("/projects/{project_id}/storyboard-metrics")
def project_storyboard_metrics(project_id: str):
    """批量分镜并发与 Supervisor 指标（EpisodesPage 运行条）。"""
    conn = get_conn()
    project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, "项目不存在")
    episodes = conn.execute(
        "SELECT id, episode_no, title, status, script_error, active_storyboard_run_id, "
        "storyboard_completion_mode FROM episodes WHERE project_id=? ORDER BY episode_no",
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
        mode = None
        try:
            mode = ep["storyboard_completion_mode"]
        except (KeyError, IndexError, TypeError):
            mode = None
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
            "completion_mode": mode,
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
    routed = await ui_route("delivery.create_package", {"episode_id": episode_id})
    if routed is not None:
        return routed
    from app.delivery import build_delivery_package, validate_package_id

    payload = dict(body or {})
    # package_id 必须服务端可控：忽略客户端自带路径穿越载荷，仅允许恢复场景沿用已校验 id。
    raw_package_id = payload.get("package_id")
    if raw_package_id:
        try:
            payload["package_id"] = validate_package_id(str(raw_package_id))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        payload["package_id"] = new_id("delivery")
    payload.setdefault("operation_started_at", now())
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
            )

        _, result = await recorder.step(
            "build_delivery_snapshot", operation,
            agent_name="delivery_loop",
            context_manifest={"immutable_snapshot": True},
        )
        recorder.succeed("交付快照已生成")
        return {**result, "run_id": recorder.run_id}
    except KeyError as exc:
        recorder.fail(exc)
        raise HTTPException(404, "剧集不存在") from exc
    except ValueError as exc:
        recorder.fail(exc)
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        recorder.fail(exc)
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
    row = get_conn().execute(
        "SELECT package_path FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "交付包不存在")
    path = Path(row["package_path"]).resolve()
    target = (path / filename).resolve()
    if path not in target.parents or not target.is_file():
        raise HTTPException(404, "交付文件不存在")
    return target


@router.get("/delivery/packages/{package_id}/report")
def download_delivery_report(package_id: str):
    path = _delivery_file(package_id, "quality-report.html")
    return FileResponse(path, media_type="text/html", filename=f"{package_id}-quality-report.html")


@router.get("/delivery/packages/{package_id}/archive")
def download_delivery_archive(package_id: str):
    row = get_conn().execute(
        "SELECT package_path FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "交付包不存在")
    archive = Path(str(row["package_path"]) + ".zip").resolve()
    if not archive.is_file():
        raise HTTPException(404, "交付压缩包不存在")
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
        },
    )
    if routed is not None:
        return routed
    from app.delivery import approve_delivery, validate_package_id

    if not get_conn().execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
        raise HTTPException(404, "剧集不存在")
    payload = dict(body)
    # 批准产出的新快照 id 一律服务端生成，禁止客户端注入路径。
    payload["approved_package_id"] = new_id("delivery")
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
    from app.delivery import build_delivery_package

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
            )

        await recorder.step(
            "build_delivery_snapshot", operation,
            agent_name="delivery_loop",
            context_manifest={"immutable_snapshot": True, "recovered": True},
        )
        recorder.succeed("交付快照已从服务重启中恢复")
    except asyncio.CancelledError:
        recorder.cancel("交付快照恢复已取消")
        raise
    except Exception as exc:  # noqa: BLE001 recovery failure must remain visible
        recorder.fail(exc)


async def _resume_delivery_approval(
    episode_id: str, payload: dict, recorder: WorkflowRecorder
) -> None:
    from app.delivery import approve_delivery

    recorder.start()
    try:
        async def operation():
            existing = get_conn().execute(
                "SELECT id FROM delivery_packages WHERE id=? AND status='approved'",
                (payload["approved_package_id"],),
            ).fetchone()
            if existing:
                return {"package_id": existing["id"], "already_committed": True}
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
