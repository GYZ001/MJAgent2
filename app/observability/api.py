"""Strictly project-scoped observability routes.

The legacy system/run endpoints remain available for compatibility, but the project
workspace UI only consumes this router.  Every detail and mutation resolves the
object back to one project before returning data or dispatching an action.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.db import get_conn
from app.evidence import repository
from app.orchestration import api as orchestration_api
from app import system_api


router = APIRouter(prefix="/api")


def _project(project_id: str) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT id,name,created_at FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "项目不存在")
    return dict(row)


def _single_project(candidates: list[str | None]) -> str | None:
    values = {str(value) for value in candidates if value}
    return next(iter(values)) if len(values) == 1 else None


def _scope_project(scope_type: str | None, scope_id: str | None) -> str | None:
    if not scope_type or not scope_id:
        return None
    conn = get_conn()
    if scope_type == "project":
        row = conn.execute("SELECT id FROM projects WHERE id=?", (scope_id,)).fetchone()
        return str(row["id"]) if row else None
    if scope_type == "episode":
        row = conn.execute("SELECT project_id FROM episodes WHERE id=?", (scope_id,)).fetchone()
        return str(row["project_id"]) if row else None
    if scope_type == "shot":
        row = conn.execute(
            """SELECT e.project_id FROM shots s JOIN episodes e ON e.id=s.episode_id
               WHERE s.id=?""", (scope_id,),
        ).fetchone()
        return str(row["project_id"]) if row else None
    return None


def _run_project(run_id: str) -> str | None:
    run = repository.get_run(run_id)
    if not run:
        return None
    return _scope_project(run.get("scope_type"), run.get("scope_id"))


def _run_context(run: dict[str, Any]) -> dict[str, Any]:
    project_id = _scope_project(run.get("scope_type"), run.get("scope_id"))
    project = _project(project_id) if project_id else None
    context: dict[str, Any] = {
        "project_id": project_id,
        "project_name": project.get("name") if project else None,
    }
    conn = get_conn()
    if run.get("scope_type") == "episode":
        row = conn.execute(
            "SELECT id AS episode_id,episode_no,title AS episode_title FROM episodes WHERE id=?",
            (run.get("scope_id"),),
        ).fetchone()
        if row:
            context.update(dict(row))
    elif run.get("scope_type") == "shot":
        row = conn.execute(
            """SELECT s.id AS shot_id,s.shot_no,e.id AS episode_id,e.episode_no,
                      e.title AS episode_title FROM shots s JOIN episodes e ON e.id=s.episode_id
               WHERE s.id=?""", (run.get("scope_id"),),
        ).fetchone()
        if row:
            context.update(dict(row))
    return context


def _artifact_project(artifact_id: str) -> str | None:
    artifact = repository.get_artifact(artifact_id)
    if not artifact:
        return None
    candidates = [_scope_project(artifact.get("scope_type"), artifact.get("scope_id"))]
    step_id = artifact.get("created_by_step_run_id")
    if step_id:
        row = get_conn().execute("SELECT run_id FROM step_runs WHERE id=?", (step_id,)).fetchone()
        if row:
            candidates.append(_run_project(str(row["run_id"])))
    return _single_project(candidates)


def _job_summary(job_id: str, source: str = "auto") -> dict[str, Any] | None:
    if source == "auto":
        return next(
            (row for row in system_api.jobs_overview(include_all=True)["recent"]
             if str(row.get("id")) == job_id),
            None,
        )
    if source == "run":
        run = repository.get_run(job_id)
        return {**run, "id": job_id, "source": "run", "run_id": job_id} if run else None
    if source == "screenplay" or job_id.startswith("screenplay_"):
        episode_id = job_id.removeprefix("screenplay_")
        row = get_conn().execute(
            "SELECT id,project_id FROM episodes WHERE id=?", (episode_id,),
        ).fetchone()
        return {"id": job_id, "source": "screenplay", **dict(row)} if row else None
    row = get_conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return {**dict(row), "source": "job"} if row else None


def _job_project(job_id: str, source: str = "auto") -> str | None:
    summary = _job_summary(job_id, source)
    if not summary:
        return None
    return system_api._job_project_id(summary)


def _call_row(call_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    return dict(row) if row else None


def _call_project(call_id: int) -> str | None:
    row = _call_row(call_id)
    if not row:
        return None
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    return system_api._call_project_id(row, meta)


def _assert_scope(project_id: str, actual: str | None, label: str = "观测对象") -> None:
    _project(project_id)
    # 使用同一个 404，避免通过详情或动作接口探测其他项目的对象是否存在。
    if not actual or actual != project_id:
        raise HTTPException(404, f"{label}不存在")


def _scope(payload: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "scope": {"type": "project", "project_id": project["id"], "project_name": project["name"]}}


@router.get("/projects/{project_id}/observability/runs")
def scoped_runs(
    project_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    search: str = "", status: str | None = None, workflow: str | None = None,
    episode_no: int | None = None, from_ts: float | None = None, to_ts: float | None = None,
    include_history: bool = False, sort: str = "desc",
):
    project = _project(project_id)
    return _scope(orchestration_api.query_runs(
        page, page_size, search, status, project_id, workflow, episode_no,
        from_ts, to_ts, include_history, sort,
    ), project)


@router.get("/projects/{project_id}/observability/runs/{run_id}")
def scoped_run(project_id: str, run_id: str):
    _assert_scope(project_id, _run_project(run_id), "运行")
    run = orchestration_api.get_run(run_id)
    return {**run, **_run_context(run)}


@router.get("/projects/{project_id}/observability/runs/{run_id}/steps")
def scoped_run_steps(project_id: str, run_id: str):
    _assert_scope(project_id, _run_project(run_id), "运行")
    return orchestration_api.get_steps(run_id)


@router.get("/projects/{project_id}/observability/runs/{run_id}/events")
def scoped_run_events(project_id: str, run_id: str, after: float | None = None, limit: int = Query(500, ge=1, le=1000)):
    _assert_scope(project_id, _run_project(run_id), "运行")
    return orchestration_api.get_events(run_id, after=after, limit=limit)


@router.post("/projects/{project_id}/observability/runs/{run_id}/{action}")
async def scoped_run_action(project_id: str, run_id: str, action: str, body: dict | None = Body(None)):
    _assert_scope(project_id, _run_project(run_id), "运行")
    if action == "cancel":
        return await orchestration_api.cancel_run_route(run_id)
    if action == "resume":
        return await orchestration_api.resume_run_route(run_id, body)
    if action == "retry":
        return await orchestration_api.retry_run_route(run_id, body)
    raise HTTPException(404, "运行动作不存在")


@router.get("/projects/{project_id}/observability/jobs")
def scoped_jobs(
    project_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    search: str = "", status: str | None = None, workflow: str | None = None,
    from_ts: float | None = None, to_ts: float | None = None, sort: str = "desc",
):
    project = _project(project_id)
    payload = system_api.query_jobs(
        page, page_size, search, status, project_id, workflow, from_ts, to_ts, sort,
    )
    # Keep the lightweight summary contract used by the existing UI while the
    # canonical field remains ``items`` for paginated consumers.
    payload["recent"] = payload["items"]
    return _scope(payload, project)


@router.get("/projects/{project_id}/observability/jobs/{job_id}")
def scoped_job(project_id: str, job_id: str, source: str = "auto"):
    _assert_scope(project_id, _job_project(job_id, source), "任务")
    return system_api.job_detail(job_id, source)


@router.post("/projects/{project_id}/observability/jobs/{job_id}/{action}")
async def scoped_job_action(project_id: str, job_id: str, action: str, source: str = "auto", body: dict | None = Body(None)):
    summary = _job_summary(job_id, source)
    _assert_scope(project_id, _job_project(job_id, source), "任务")
    effective_source = str((summary or {}).get("source") or source)
    run_id = str((summary or {}).get("run_id") or job_id)
    if effective_source == "run":
        return await scoped_run_action(project_id, run_id, action, body)
    if effective_source == "job" and action == "cancel":
        return await orchestration_api.cancel_media_job_route(job_id)
    if effective_source == "job" and action in {"retry", "resume"}:
        return system_api.retry_job(job_id, body)
    raise HTTPException(409, "当前任务不支持该操作")


@router.get("/projects/{project_id}/observability/calls")
def scoped_calls(
    project_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    search: str = "", status: str | None = None, category: str | None = None,
    function: str | None = None, model: str | None = None, from_ts: float | None = None,
    to_ts: float | None = None, sort: str = "desc", ids: str | None = None,
):
    project = _project(project_id)
    payload = system_api.query_calls(
        page, page_size, search, status, category, project_id, function, model,
        from_ts, to_ts, sort, ids,
    )
    for item in payload["items"]:
        item["context"] = {
            **(item.get("context") or {}),
            "project_id": project_id,
            "project_name": project["name"],
        }
    for aggregate in payload["aggregates"]:
        aggregate["project_id"] = project_id
        aggregate["project_name"] = project["name"]
    return _scope(payload, project)


@router.get("/projects/{project_id}/observability/calls/{call_id}")
def scoped_call(project_id: str, call_id: int):
    _assert_scope(project_id, _call_project(call_id), "调用记录")
    return system_api.call_detail(call_id)


@router.get("/projects/{project_id}/observability/calls/{call_id}/download", response_class=PlainTextResponse)
def scoped_call_download(project_id: str, call_id: int):
    _assert_scope(project_id, _call_project(call_id), "调用记录")
    return system_api.download_call_detail(call_id)


@router.get("/projects/{project_id}/observability/gates")
def scoped_gates(project_id: str, limit: int = Query(100, ge=1, le=500)):
    _project(project_id)
    return orchestration_api.list_pending_gates(project_id=project_id, limit=limit)


@router.post("/projects/{project_id}/observability/gates/{artifact_id}/decision")
def scoped_gate_decision(project_id: str, artifact_id: str, body: dict = Body(...)):
    _assert_scope(project_id, _artifact_project(artifact_id), "门禁产物")
    return orchestration_api.decide_gate(artifact_id, body)


@router.get("/projects/{project_id}/observability/artifacts/{artifact_id}")
def scoped_artifact(project_id: str, artifact_id: str):
    _assert_scope(project_id, _artifact_project(artifact_id), "证据产物")
    return orchestration_api.get_artifact(artifact_id)


@router.get("/projects/{project_id}/observability/artifacts/{artifact_id}/{part}")
def scoped_artifact_part(project_id: str, artifact_id: str, part: str):
    _assert_scope(project_id, _artifact_project(artifact_id), "证据产物")
    if part == "evals":
        return orchestration_api.get_artifact_evaluations(artifact_id)
    if part == "lineage":
        return orchestration_api.get_artifact_lineage(artifact_id)
    raise HTTPException(404, "证据视图不存在")


@router.get("/observability/resolve")
def resolve_legacy_observability(
    run_id: str | None = None, job_id: str | None = None, call_id: int | None = None,
    source: str = "auto",
):
    provided = sum(value is not None for value in (run_id, job_id, call_id))
    if provided != 1:
        raise HTTPException(422, "必须且只能提供 run_id、job_id、call_id 之一")
    if run_id:
        project_id, section, object_id = _run_project(run_id), "runs", run_id
    elif job_id:
        project_id, section, object_id = _job_project(job_id, source), "jobs", job_id
    else:
        project_id, section, object_id = _call_project(int(call_id)), "calls", str(call_id)
    if not project_id:
        raise HTTPException(404, "观测对象未关联有效项目")
    _project(project_id)
    return {"project_id": project_id, "section": section, "object_id": object_id}


@router.get("/system/overview")
def system_overview():
    """System-wide aggregate only: no raw run, job, or call records are returned."""
    conn = get_conn()
    projects = [dict(row) for row in conn.execute(
        "SELECT id,name,created_at FROM projects ORDER BY created_at DESC"
    ).fetchall()]
    jobs = system_api.jobs_overview(include_all=True)["recent"]
    by_project: dict[str, Counter[str]] = {item["id"]: Counter() for item in projects}
    unattributed_jobs = 0
    for row in jobs:
        pid = row.get("project_id")
        if pid in by_project:
            by_project[pid][str(row.get("status") or "unknown")] += 1
        else:
            unattributed_jobs += 1
    call_rows = [dict(row) for row in conn.execute(
        "SELECT id,meta,run_id,step_run_id FROM provider_calls ORDER BY id DESC"
    ).fetchall()]
    scope_maps = system_api._project_scope_maps()
    call_counts: Counter[str] = Counter()
    unattributed_calls = 0
    for row in call_rows:
        try:
            meta = json.loads(row.get("meta") or "{}")
        except (TypeError, json.JSONDecodeError):
            meta = {}
        pid = system_api._call_project_id(row, meta if isinstance(meta, dict) else {}, scope_maps)
        if pid in by_project:
            call_counts[pid] += 1
        else:
            unattributed_calls += 1
    return {
        "projects": [
            {**project, "job_counts": dict(by_project[project["id"]]), "call_count": call_counts[project["id"]]}
            for project in projects
        ],
        "totals": {
            "projects": len(projects), "jobs": len(jobs), "calls": len(call_rows),
            "unattributed_jobs": unattributed_jobs, "unattributed_calls": unattributed_calls,
        },
        "server_time": time.time(),
    }
