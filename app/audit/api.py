"""操作审计 REST 接口（``/api/system/audit/*``），系统管理员专属。

L5（见 app/LAYERS.toml "app.audit.api" = 5，覆盖 "app.audit" = 2 前缀）：只有
本文件允许 import app.auth.deps（挂 APIRouter，只被 app.main 引用，同
``app.auth.admin_api`` 的角色）。只服务查询，不做任何写入——写入全部走
``app.audit.recorder``/``store`` 的记录钩子，与本路由完全解耦。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.audit import queries
from app.auth.deps import require_system_admin

router = APIRouter(
    prefix="/api/system/audit", tags=["audit"],
    dependencies=[Depends(require_system_admin)],
)


@router.get("/events")
def list_events(
    since: float | None = None, until: float | None = None, user_id: str | None = None,
    event: str | None = None, outcome: str | None = None, source: str | None = None,
    project_id: str | None = None, q: str | None = None, limit: int = 50,
    cursor: str | None = None,
):
    return queries.list_events(
        since=since, until=until, user_id=user_id, event=event, outcome=outcome,
        source=source, project_id=project_id, q=q, limit=limit, cursor=cursor,
    )


@router.get("/events/{event_id}")
def get_event(event_id: str):
    item = queries.get_event(event_id)
    if item is None:
        raise HTTPException(404, "审计记录不存在")
    return item


@router.get("/facets")
def facets(since: float | None = None, until: float | None = None):
    return queries.facets(since=since, until=until)
