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


@router.post("/runs")
async def create_run(body: dict = Body(...)):
    workflow_type = str(body.get("workflow_type") or "").strip()
    scope_type = str(body.get("scope_type") or "project").strip()
    scope_id = str(body.get("scope_id") or "").strip()
    if workflow_type != "auto_project" or scope_type != "project" or not scope_id:
        raise HTTPException(400, "Phase 1 仅支持 workflow_type=auto_project 的项目级运行")
    if not get_conn().execute("SELECT 1 FROM projects WHERE id=?", (scope_id,)).fetchone():
        raise HTTPException(404, "项目不存在")
    from app import auto

    if auto.is_running(scope_id):
        raise HTTPException(409, "该项目已有自动流水线在运行")
    run_id = auto.start(scope_id, export_dir=body.get("export_dir"), requested_by="api")
    return repository.get_run(run_id)


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
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch("run.control", {"run_id": run_id, "action": "cancel"}, initiator="ui")
    raise_if_failed(result)
    return result_http_payload(result)


async def cancel_run(run_id: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["status"] not in repository.ACTIVE_RUN_STATUSES:
        raise HTTPException(409, "运行已结束，不能取消")
    if run["workflow_type"] == "auto_project":
        from app import auto

        current = auto.status(run["scope_id"])
        if current["running"] and current["run_id"] != run_id:
            raise HTTPException(409, "该项目已有另一条运行，不能用旧 Run 取消它")
        if current["running"]:
            cancelled = await auto.cancel(run["scope_id"])
        else:
            WorkflowRecorder(run_id).cancel("已取消暂停中的运行")
            cancelled = True
        return {"cancelled": cancelled, "run": repository.get_run(run_id)}
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
    task = task_registry.get("run", run_id)
    if task and not task.done():
        await task_registry.cancel_and_wait("run", run_id)
        return {"cancelled": True, "run": repository.get_run(run_id)}
    raise HTTPException(409, "运行没有可取消的进程内任务")


def _restart_auto_run(run_id: str, trigger_type: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["workflow_type"] != "auto_project" or run["scope_type"] != "project":
        raise HTTPException(400, "Phase 1 仅支持恢复项目自动流水线")
    from app import auto

    if auto.is_running(run["scope_id"]):
        raise HTTPException(409, "该项目已有自动流水线在运行")
    new_run_id = auto.start(
        run["scope_id"],
        requested_by="api",
        trigger_type=trigger_type,
        parent_run_id=run_id,
    )
    return repository.get_run(new_run_id)


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

    episode = get_conn().execute(
        "SELECT id, project_id FROM episodes WHERE id=?", (run["scope_id"],)
    ).fetchone()
    if not episode:
        raise HTTPException(404, "剧集不存在")
    if task_registry.active("storyboard", episode["id"]):
        raise HTTPException(409, "该剧集已有分镜任务在运行")
    get_conn().execute(
        "UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode["id"],)
    )
    get_conn().commit()
    recorder = domain_api._new_storyboard_recorder(
        episode["id"], requested_by="api", trigger_type=trigger_type, parent_run_id=run_id
    )
    task_registry.spawn(
        "storyboard", episode["id"],
        domain_api._recorded_storyboard_task(episode["id"], recorder, resume=True),
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
    if run["workflow_type"] == "auto_project":
        return _restart_auto_run(run_id, trigger_type)
    if run["workflow_type"] == "screenplay":
        return _restart_screenplay_run(run_id, trigger_type)
    if run["workflow_type"] == "storyboard":
        return _restart_storyboard_run(run_id, trigger_type)
    if run["workflow_type"] == "character_bible":
        return _restart_bible_run(run_id, trigger_type)
    raise HTTPException(400, "当前工作流尚未接入恢复适配器")


@router.post("/runs/{run_id}/resume")
async def resume_run_route(run_id: str):
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch("run.control", {"run_id": run_id, "action": "resume"}, initiator="ui")
    raise_if_failed(result)
    return result_http_payload(result)


async def resume_run(run_id: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["status"] not in {"PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_RETRY", "WAITING_HUMAN"}:
        raise HTTPException(409, "当前状态不能恢复")
    return _restart_run(run_id, "resume")


@router.post("/runs/{run_id}/retry")
async def retry_run_route(run_id: str):
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch("run.control", {"run_id": run_id, "action": "retry"}, initiator="ui")
    raise_if_failed(result)
    return result_http_payload(result)


async def retry_run(run_id: str):
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    if run["status"] not in {"FAILED", "PARTIAL", "CANCELLED"}:
        raise HTTPException(409, "只有失败、部分完成或已取消的运行可以受控重试")
    return _restart_run(run_id, "retry")


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
    from app.delivery import build_delivery_package

    payload = dict(body or {})
    payload.setdefault("package_id", new_id("delivery"))
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
    from app.delivery import approve_delivery

    if not get_conn().execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
        raise HTTPException(404, "剧集不存在")
    payload = dict(body)
    payload.setdefault("approved_package_id", new_id("delivery"))
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
def create_customer_feedback(episode_id: str, body: dict = Body(...)):
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
def run_benchmark(body: dict = Body(...)):
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
def set_project_engine(project_id: str, body: dict = Body(...)):
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
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch("job.cancel", {"job_id": job_id}, initiator="ui")
    raise_if_failed(result)
    return result_http_payload(result)
