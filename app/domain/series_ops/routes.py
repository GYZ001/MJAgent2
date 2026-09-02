"""连播台四条 REST 路由：启动、查询、暂停、继续。

薄封装：鉴权/所有权由挂载路由的 ``app.domain.common.router``
（``dependencies=_PROJECT_OWNER_DEPS``，见 ``app/main.py``）统一处理；这里
只做 ``ui_route`` 前置（让能力总线接管命令语义）与响应整形，业务判断都在
``orchestrator``/``state``/``merge`` 里。
"""
from __future__ import annotations

from fastapi import Body

from app.db import get_conn
from app.domain.common import _as_body_dict, _project_or_404, router

from . import merge, orchestrator, state


@router.post("/projects/{project_id}/series-film")
async def start_series_film(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route

    payload = {"project_id": project_id, **_as_body_dict(body)}
    routed = await ui_route("series.film_start", payload)
    if routed is not None:
        return routed
    _project_or_404(project_id)
    return await orchestrator.start_series_film_core(project_id, _as_body_dict(body))


@router.get("/projects/{project_id}/series-film")
def get_series_film(project_id: str):
    _project_or_404(project_id)
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM workflow_runs
           WHERE workflow_type=? AND scope_type='project' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (state.WORKFLOW_TYPE, project_id),
    ).fetchone()
    run = state.project_run_view(dict(row)) if row else None
    film = None
    if run is not None and run.get("episode_from") and run.get("episode_to"):
        film = merge.film_for_range(project_id, run["episode_from"], run["episode_to"])
    if film is None:
        film = merge.latest_film(project_id)
    episodes = conn.execute(
        "SELECT id, episode_no, title FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,),
    ).fetchall()
    return {
        "run": run,
        "film": film,
        "episodes_available": [
            {"episode_id": r["id"], "episode_no": r["episode_no"], "title": r["title"]}
            for r in episodes
        ],
    }


@router.post("/projects/{project_id}/series-film/pause")
async def pause_series_film(project_id: str):
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("series.film_pause", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    return await orchestrator.pause_series_film_core(project_id)


@router.post("/projects/{project_id}/series-film/resume")
async def resume_series_film(project_id: str):
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("series.film_resume", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    return await orchestrator.resume_series_film_core(project_id)
