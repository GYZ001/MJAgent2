"""场景提示词与场景锚点的人工编辑入口。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _project_or_404,
    router,
)
from app.refs import (
    SCENE_CANONICAL_MAX_CHARS,
    SCENE_CANONICAL_MIN_CHARS,
)
from app.schemas import (
    Bible,
    schema_errors,
)
from fastapi import HTTPException

from .primitives import (
    _parse_json_value,
    _scene_canonical_length_ok,
)
from .scene_assets import _scene_current_row


@router.put("/projects/{project_id}/scenes/{scene_name}/prompt")
async def edit_scene_prompt(project_id: str, scene_name: str, body: dict):
    """更新单个场景的场景图生成词。传空字符串/null 恢复为默认合成描述。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "scene.update_prompt",
        {"project_id": project_id, "scene_name": scene_name, "prompt": (body.get("scene_prompt") or "")},
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    prompt_text = (body.get("scene_prompt") or "").strip()
    if prompt_text and not 10 <= len(prompt_text) <= 400:
        raise HTTPException(422, f"场景图描述长度 {len(prompt_text)} 字，要求 10~400 字（留空则恢复默认）")
    bible = json.loads(p["bible_json"])
    target = next((s for s in bible.get("scenes", []) if s.get("name") == scene_name), None)
    if target is None:
        raise HTTPException(404, f"场景不存在：{scene_name}")
    target["scene_prompt_override"] = prompt_text or None
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(bible, ensure_ascii=False), project_id))
    conn.commit()
    return {"saved": True, "reset_to_default": not prompt_text}

@router.put("/projects/{project_id}/scenes/{scene_name}")
async def edit_scene_anchor(project_id: str, scene_name: str, body: dict):
    """结构化保存场景锚点；只改文字并标记待重绘，不产生图片费用。"""
    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    expected = body.get("expected_version")
    if expected is None or int(expected) != int(p.get("bible_version") or 0):
        raise HTTPException(409, detail={
            "code": "BIBLE_VERSION_CONFLICT", "message": "场景锚点已被其他操作更新，请刷新后重试",
            "current_version": int(p.get("bible_version") or 0),
        })
    bible = json.loads(p["bible_json"])
    target = next((scene for scene in bible.get("scenes", []) if scene.get("name") == scene_name), None)
    if target is None:
        raise HTTPException(404, f"场景不存在：{scene_name}")
    canonical = str(body.get("scene_canonical") or "").strip()
    if not _scene_canonical_length_ok(canonical):
        raise HTTPException(
            422,
            f"完整场景锚点要求 {SCENE_CANONICAL_MIN_CHARS}~{SCENE_CANONICAL_MAX_CHARS} 字",
        )
    location = str(body.get("location_kind") or target.get("location_kind") or "").strip()
    if location and location not in {"室内", "室外", "其他"}:
        raise HTTPException(422, "location_kind 须为室内/室外/其他")
    target.update({
        "scene_canonical": canonical, "location_kind": location,
        "space": str(body.get("space") or "").strip(),
        "time_of_day": str(body.get("time_of_day") or "").strip(),
        "lighting": str(body.get("lighting") or "").strip(),
        "landmarks": [str(item).strip() for item in (body.get("landmarks") or []) if str(item).strip()],
    })
    instance, validation_errors = schema_errors(Bible, bible)
    if validation_errors:
        raise HTTPException(422, "；".join(validation_errors))
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=?,bible_version=bible_version+1 WHERE id=?",
        (instance.model_dump_json(), project_id),
    )
    current = _scene_current_row(conn, project_id, scene_name)
    if current:
        change = _parse_json_value(current["change_json"] if "change_json" in current.keys() else None, {}) or {}
        change.update({"description_changed": True, "pending_redraw": True, "changed_at": now()})
        conn.execute("UPDATE scene_references SET change_json=? WHERE id=?",
                     (json.dumps(change, ensure_ascii=False), current["id"]))
    conn.commit()
    return {"saved": True, "bible_version": int(p.get("bible_version") or 0) + 1, "pending_redraw": True, "generated": False}
