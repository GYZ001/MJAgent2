"""道具库（世界书物件库）只读列表与重生成参考图端点。

道具库业务逻辑本身在 ``app.props``（判据/模型评估/出图/落库全部在那一层，见其
包 docstring）；本文件只做路由与 404/409 转译，与 ``portrait_candidates.py``/
``manual_scene.py`` 的既有分工一致——路由层不重复实现任何判据。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.db import get_conn
from app.domain.common import _media_url, _project_or_404, router
from app.props import props_for_project, regenerate_prop_reference


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
