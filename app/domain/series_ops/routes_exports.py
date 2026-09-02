"""连播任务台导出类路由：打包导出选中任务、列出最近的导出包。"""
from __future__ import annotations

from fastapi import Body

from app.domain.common import _as_body_dict, _project_or_404, router

from . import exports


@router.post("/projects/{project_id}/series-exports")
async def create_series_export(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route

    payload = {"project_id": project_id, **_as_body_dict(body)}
    routed = await ui_route("series.export_create", payload)
    if routed is not None:
        return routed
    _project_or_404(project_id)
    parsed = _as_body_dict(body)
    return exports.create_export(project_id, list(parsed.get("task_ids") or []))


@router.get("/projects/{project_id}/series-exports")
def list_series_exports(project_id: str):
    _project_or_404(project_id)
    return {"exports": exports.list_exports(project_id)}
