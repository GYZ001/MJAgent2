"""定妆照肤色局部色块核验：只读检查入口，供人物谱前端渲染不合格人物卡上的
告警与「重新生成」提示。

L5（``app.domain`` 前缀覆盖）：只做请求解析/404 映射，业务逻辑在
``app.portraits.portrait_skin_blush_audit``（L4）。GET 路由：判定调用是
多模态模型调用（非视频生成），不计费（CLAUDE.md「文本免费但视频有额度」），
不消耗任何配额、不改变任何业务状态（命中缓存时甚至不发真实请求），只读检查，
不需要 ``ui_route``/能力分类，与 ``voice_routes.list_character_voices``
同一种"GET 式只读路由不经过 Command Bus"的写法；缓存写入是诊断性的，不是
本端点对外承诺的业务效果。
"""
from __future__ import annotations

from app.db import get_conn
from app.domain.common import _project_or_404, router
from app.portraits.portrait_skin_blush_audit import audit_project_portraits


@router.get("/projects/{project_id}/portraits/skin-blush-audit")
async def run_portrait_skin_blush_audit(project_id: str, character_name: str | None = None):
    """非写实画风项目返回空列表；写实画风项目逐个当前角色定妆照跑核验（命中
    缓存不重复真实调用模型）。``character_name`` 非空时只核验该角色。"""
    project = _project_or_404(project_id)
    only_character = (character_name or "").strip() or None
    results = await audit_project_portraits(get_conn(), project_id, project, only_character=only_character)
    return {"project_id": project_id, "results": results}
