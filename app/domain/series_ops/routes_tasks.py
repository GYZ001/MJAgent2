"""连播任务台任务类路由：列表/切分预览/生成/删除/详情/入队/取消/队列暂停继续。

薄封装：鉴权/所有权由挂载路由的 ``app.domain.common.router``
（``dependencies=_PROJECT_OWNER_DEPS``，见 ``app/main.py``）统一处理；这里只做
``ui_route`` 前置（让能力总线接管命令语义）与响应整形。

路由注册顺序有讲究：Starlette 按注册顺序匹配路径，``GET .../series-tasks/plan``
必须定义在 ``GET .../series-tasks/{task_id}`` 之前，否则会被后者的路径参数
吞掉（``plan`` 会被当成 ``task_id="plan"``）。
"""
from __future__ import annotations

from fastapi import Body

from app.db import get_conn
from app.domain.common import _as_body_dict, _project_or_404, router

from . import queue, tasks


@router.get("/projects/{project_id}/series-tasks")
def list_series_tasks(project_id: str, offset: int = 0, limit: int = 50):
    _project_or_404(project_id)
    return tasks.list_tasks(get_conn(), project_id, offset, limit)


@router.get("/projects/{project_id}/series-tasks/plan")
def plan_series_tasks(project_id: str, group_size: int = tasks.DEFAULT_GROUP_SIZE):
    _project_or_404(project_id)
    return tasks.plan_groups(get_conn(), project_id, group_size)


@router.post("/projects/{project_id}/series-tasks")
async def generate_series_tasks(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route

    payload = {"project_id": project_id, **_as_body_dict(body)}
    routed = await ui_route("series.tasks_generate", payload)
    if routed is not None:
        return routed
    _project_or_404(project_id)
    return tasks.generate_tasks(get_conn(), project_id, _as_body_dict(body))


@router.delete("/projects/{project_id}/series-tasks/{task_id}")
async def delete_series_task(project_id: str, task_id: str):
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("series.task_delete", {"project_id": project_id, "task_id": task_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    return tasks.delete_task(get_conn(), project_id, task_id)


@router.get("/projects/{project_id}/series-tasks/{task_id}")
def get_series_task(project_id: str, task_id: str):
    _project_or_404(project_id)
    return tasks.task_detail(get_conn(), project_id, task_id)


@router.post("/projects/{project_id}/series-tasks/enqueue")
async def enqueue_series_tasks(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route

    payload = {"project_id": project_id, **_as_body_dict(body)}
    routed = await ui_route("series.tasks_enqueue", payload)
    if routed is not None:
        return routed
    _project_or_404(project_id)
    parsed = _as_body_dict(body)
    return await queue.enqueue(project_id, list(parsed.get("task_ids") or []), bool(parsed.get("force")))


@router.post("/projects/{project_id}/series-tasks/cancel")
async def cancel_series_tasks(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route

    payload = {"project_id": project_id, **_as_body_dict(body)}
    routed = await ui_route("series.tasks_cancel", payload)
    if routed is not None:
        return routed
    _project_or_404(project_id)
    parsed = _as_body_dict(body)
    return await queue.cancel(project_id, list(parsed.get("task_ids") or []))


@router.post("/projects/{project_id}/series-tasks/queue/pause")
async def pause_series_queue(project_id: str):
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("series.queue_pause", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    return await queue.pause(project_id)


@router.post("/projects/{project_id}/series-tasks/queue/resume")
async def resume_series_queue(project_id: str):
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("series.queue_resume", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    return await queue.resume(project_id)
