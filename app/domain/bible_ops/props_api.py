"""道具库（世界书物件库）只读列表与重生成参考图端点。

道具库业务逻辑本身在 ``app.props``（判据/模型评估/出图/落库全部在那一层，见其
包 docstring）；本文件只做路由与 404/409 转译，与 ``portrait_candidates.py``/
``manual_scene.py`` 的既有分工一致——路由层不重复实现任何判据。
"""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.db import get_conn
from app.domain.common import _media_url, _project_or_404, router
from app.props import props_for_project, regenerate_prop_reference
from app.props.card_audit import audits_for_project, launch_background_audit_for_project
from app.props.card_audit_doubts import confirm_doubt, keep_doubt


class PropAuditDoubtBody(BaseModel):
    """确认删除/保留一条道具卡复核存疑的请求体——卡名放请求体而不是路径，
    避免道具名里的特殊字符（空格、斜杠等）在 URL 路径段里被误解析。"""
    prop_name: str = Field(min_length=1)
    doubt_key: str = Field(min_length=1)


@router.get("/projects/{project_id}/props")
async def list_props(project_id: str):
    """道具库列表：name/appearance/aliases/image_path/status。"""
    _project_or_404(project_id)
    conn = get_conn()
    items = [
        {**item, "image_url": _media_url(item.get("image_path"))}
        for item in props_for_project(conn, project_id)
    ]
    return {"project_id": project_id, "items": items}


@router.post("/projects/{project_id}/props/{name}/regenerate")
async def regenerate_prop(project_id: str, name: str):
    """重新生成某道具的参考图；道具不在世界书里时返回 409（与
    ``bible_generate_precheck`` 等未走命令总线的路由同一约定：``ValueError``
    在这里必须显式转译，不走命令总线不会自动转 409）。"""
    _project_or_404(project_id)
    try:
        result = await regenerate_prop_reference(project_id, name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**result, "image_url": _media_url(result.get("image_path"))}


@router.post("/projects/{project_id}/props/audit")
async def audit_props(project_id: str):
    """道具库手动入口（触发点③，2026-10-03）：整项目全部道具卡按现行规则
    后台复核，立即返回受理结果——不走命令总线的路由须显式把 ``ValueError``
    转 409（CLAUDE.md 同条约定，与 ``regenerate_prop`` 一致）。"""
    _project_or_404(project_id)
    try:
        prop_names = launch_background_audit_for_project(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"project_id": project_id, "accepted": prop_names}


@router.get("/projects/{project_id}/props/audit")
async def list_prop_audits(project_id: str):
    """道具库手动入口的结果查询：每张卡最近一轮复核记录（含 running/failed，
    以及待人工确认的 doubts）。"""
    _project_or_404(project_id)
    items = audits_for_project(get_conn(), project_id)
    return {"project_id": project_id, "items": items}


@router.post("/projects/{project_id}/props/audit/confirm")
async def confirm_prop_audit_doubt(project_id: str, body: PropAuditDoubtBody):
    """人工确认删除一条复核存疑——按卡名 + 存疑原文逐字定位，走与自动删除
    同一条核验 + 重出图 + 原子回滚路径（见 ``app.props.card_audit_doubts``
    模块 docstring）。``ValueError``（项目/道具/存疑不存在、重出图失败）
    显式转 409，同 ``regenerate_prop``。"""
    _project_or_404(project_id)
    try:
        return await confirm_doubt(project_id, body.prop_name, body.doubt_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/projects/{project_id}/props/audit/keep")
async def keep_prop_audit_doubt(project_id: str, body: PropAuditDoubtBody):
    """人工确认保留一条复核存疑——记录决定后同一规则版本内不再重复呈现。"""
    _project_or_404(project_id)
    try:
        return await keep_doubt(project_id, body.prop_name, body.doubt_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
