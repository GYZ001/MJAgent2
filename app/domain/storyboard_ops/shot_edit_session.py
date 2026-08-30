"""单镜头编辑会话：起草、编辑影响预览、草稿列举与丢弃。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 mutation_primitives。
"""
from __future__ import annotations

import json

from app.compiler import shot_cost_cny
from app.db import get_conn
from app.domain.common import router
from app.evidence import repository as evidence_repository
from fastapi import HTTPException

from .mutation_primitives import (
    _narrative_semantic_edit_fields,
    _raise_narrative_semantic_mutation_required,
    _resolve_storyboard_mutation_screenplay,
)


@router.post("/shots/{shot_id}/edit-session")
def start_shot_edit_session(shot_id: str):
    from app.storyboard_workspace import create_edit_session
    return create_edit_session(shot_id)

def _public_shot_editable_value(shot: dict, key: str):
    value = shot.get(key)
    if key in {"characters", "dialogues"} and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value

@router.post("/shots/{shot_id}/impact-preview")
def preview_shot_edit_impact(shot_id: str, body: dict):
    from app.storyboard_workspace import (
        create_preview, episode_fingerprint, require_edit_session, validate_source_binding,
    )

    session = require_edit_session(body.get("edit_session_token"), shot_id)
    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise HTTPException(404, "镜头不存在")
    shot = dict(shot_row)
    screenplay_context = _resolve_storyboard_mutation_screenplay(
        conn, str(shot["episode_id"]),
    )
    changes = dict(body.get("changes") or {})
    forbidden = {"id", "episode_id", "shot_no", "storyboard_artifact_id"}.intersection(changes)
    if forbidden:
        raise HTTPException(422, f"不可直接修改字段：{'、'.join(sorted(forbidden))}")
    if "source_excerpt" in changes:
        raise HTTPException(422, "原文证据不可自由输入，请从本集授权原文框选")
    if "source_binding" in changes:
        excerpt, normalized_binding = validate_source_binding(shot["episode_id"], changes["source_binding"])
        changes["source_binding"] = normalized_binding
        changes["source_excerpt"] = excerpt
    changed_fields = [
        key for key, value in changes.items()
        if key != "source_binding" and _public_shot_editable_value(shot, key) != value
    ]
    if screenplay_context.narrative_authority_required:
        semantic_changes = _narrative_semantic_edit_fields(changed_fields)
        if semantic_changes:
            _raise_narrative_semantic_mutation_required(
                operation="shot_edit",
                fields=semantic_changes,
            )
    if not changed_fields:
        try:
            from app.observability.metrics import inc
            inc("storyboard_save_noop_total", episode_id=shot["episode_id"], shot_id=shot_id)
        except Exception:  # noqa: BLE001
            pass
        return {
            "unchanged": True,
            "changed_fields": [],
            "message": "结构化内容没有变化，不会创建新版本或失效下游",
        }
    version_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shot_versions WHERE shot_id=?", (shot_id,),
    ).fetchone()["c"])
    scene_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shot_scenes WHERE shot_id=?", (shot_id,),
    ).fetchone()["c"])
    descendants: list[dict] = []
    if shot.get("storyboard_artifact_id"):
        descendants = evidence_repository.get_lineage(shot["storyboard_artifact_id"]).get("descendants") or []
    payload = {
        "unchanged": False,
        "changed_fields": changed_fields,
        "normalized_changes": changes,
        "baseline_artifact_id": session.get("baseline_artifact_id"),
        "baseline_content_hash": session["baseline_content_hash"],
        "requires_reconfirm": True,
        "paid_media_invalidated": bool(version_count or scene_count),
        "stale_descendant_ids": [str(item["id"]) for item in descendants if item.get("status") != "stale"],
        "stale_count": len(descendants),
        "by_artifact_type": {
            "参考图": scene_count,
            "视频版本": version_count,
            "证据链": len(descendants),
        },
        "revalidation_shots": sorted({max(1, int(shot["shot_no"]) - 1), int(shot["shot_no"]), int(shot["shot_no"]) + 1}),
        "rebuild": {
            "image_count": scene_count,
            "unit_price_cny": 0,
            "estimated_cost_cny": round(shot_cost_cny(int(changes.get("duration_s") or shot["duration_s"])), 2) if version_count else 0,
            "max_retry_budget_cny": round(shot_cost_cny(int(changes.get("duration_s") or shot["duration_s"])) * 2, 2) if version_count else 0,
            "note": "视频重生成费用按届时服务端费率重新报价",
        },
    }
    return create_preview(
        "shot_edit", shot["episode_id"], payload,
        shot_id=shot_id, baseline_fingerprint=episode_fingerprint(shot["episode_id"]),
    )

@router.get("/shots/{shot_id}/drafts")
def list_shot_edit_drafts(shot_id: str):
    shot = get_conn().execute("SELECT episode_id,shot_no FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    rows = get_conn().execute(
        """SELECT id,version,status,content_json,parent_artifact_ids_json,created_at
           FROM artifacts WHERE type='storyboard_shot' AND scope_type='storyboard_checkpoint'
             AND scope_id=? AND status='needs_revision' ORDER BY created_at DESC""",
        (f"{shot['episode_id']}:{shot['shot_no']}",),
    ).fetchall()
    items = []
    for row in rows:
        evaluations = evidence_repository.get_evaluations(row["id"])
        issues = []
        for evaluation in evaluations:
            evidence = evaluation.get("evidence") or {}
            issues.extend(evidence.get("issues") or [])
        items.append({
            "id": row["id"], "version": row["version"], "status": row["status"],
            "content": json.loads(row["content_json"] or "{}"),
            "baseline_artifact_ids": json.loads(row["parent_artifact_ids_json"] or "[]"),
            "issues": issues, "created_at": row["created_at"],
        })
    return {"items": items}

@router.delete("/shots/{shot_id}/drafts/{draft_id}")
def discard_shot_edit_draft(shot_id: str, draft_id: str):
    conn = get_conn()
    shot = conn.execute("SELECT episode_id,shot_no FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    scope = f"{shot['episode_id']}:{shot['shot_no']}"
    row = conn.execute(
        "SELECT id FROM artifacts WHERE id=? AND scope_id=? AND status='needs_revision'",
        (draft_id, scope),
    ).fetchone()
    if not row:
        raise HTTPException(404, "失败草稿不存在")
    conn.execute("UPDATE artifacts SET status='rejected' WHERE id=?", (draft_id,))
    conn.commit()
    return {"discarded": True, "published_unchanged": True}
