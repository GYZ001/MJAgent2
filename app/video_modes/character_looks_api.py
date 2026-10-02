"""人物造型照的查询/补齐 REST 入口（人物造型照 P0 功能，2026-10-02）。

挂 ``APIRouter``、只被 ``app.main`` 引用——与 ``app.orgs.api``/``app.audit.api``
同一种「路由→域」入口角色，同理判 L5（覆盖 ``app.video_modes`` = 4 前缀，见
``app/LAYERS.toml``）。鉴权沿用 ``app.main`` 挂载点统一套的 ``_PROJECT_OWNER_DEPS``
（按路径参数解析 episode_id 所属项目），本文件不重复实现项目归属判断。

两条路由：
- ``POST /api/episodes/{episode_id}/character-looks``：202，受理后台补齐（不等
  生成完成就返回——单张图生成是分钟级供应商调用，同步等待会拖垮请求超时）。
- ``GET /api/episodes/{episode_id}/character-looks``：只读状态，直接复用
  ``scan_episode_character_look_needs``（与 ensure 内部同一份判据，不会漂移）。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from app.db import get_conn
from app.video_modes.character_look_views import scan_episode_character_look_needs
from app.video_modes.character_looks_ensure import (
    launch_background_ensure,
    load_target_shot_rows,
    summarize_look_items,
)

router = APIRouter(prefix="/api")


def _episode_or_404(episode_id: str) -> tuple[str, int]:
    row = get_conn().execute(
        "SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "剧集不存在")
    return str(row["project_id"]), int(row["episode_no"])


@router.post("/episodes/{episode_id}/character-looks", status_code=202)
async def start_character_looks(episode_id: str, body: dict | None = Body(None)):
    """补齐本集缺失/失败的人物造型照；``shot_ids`` 可选，省略则补齐整集。后台
    任务入口与生成入口闸门（``pending_character_looks_gate``）共用
    ``launch_background_ensure``，见该函数文档。"""
    project_id, _ = _episode_or_404(episode_id)
    payload = body if isinstance(body, dict) else {}
    raw_shot_ids = payload.get("shot_ids")
    shot_ids = [str(x) for x in raw_shot_ids] if isinstance(raw_shot_ids, list) and raw_shot_ids else None
    launch_background_ensure(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids)
    return {"status": "accepted"}


@router.get("/episodes/{episode_id}/character-looks")
def get_character_looks(episode_id: str):
    """每段每人物需要的造型照及当前状态（ready/running/failed/missing），供
    分镜台渲染「补齐造型照」按钮与计数。只读，不触发生成。"""
    from app.schemas import Bible  # 函数内导入：路由层按需加载模型 schema，避免模块顶层拉长 import 链

    project_id, episode_no = _episode_or_404(episode_id)
    conn = get_conn()
    project_row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project_row or not (project_row["bible_json"] or "").strip():
        return {"items": [], "summary": summarize_look_items([])}
    bible = Bible.model_validate_json(project_row["bible_json"])
    rows = load_target_shot_rows(conn, episode_id, None)
    items = scan_episode_character_look_needs(
        conn=conn, bible=bible, project_id=project_id, episode_no=episode_no, shot_rows=rows,
    )
    return {"items": items, "summary": summarize_look_items(items)}
