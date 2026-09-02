"""人物谱变更后自动改配（角色卡字段/立绘）候选的枚举与人工裁决。

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
from app.schemas import (
    Bible,
    schema_errors,
)
from fastapi import HTTPException

from .edit import _commit_bible_revision
from .portrait_candidates import _set_current_portrait
from .primitives import _parse_json_value
from .scene_assets import _scene_current_row


def _auto_change_payload(item: dict) -> dict:
    payload = item.get("payload")
    return payload if isinstance(payload, dict) else {}

def _auto_change_character_card(item: dict) -> dict | None:
    payload = _auto_change_payload(item)
    candidates = [
        payload.get("character"),
        payload.get("character_card"),
        item.get("character_card"),
    ]
    if isinstance(item.get("character"), dict):
        candidates.append(item.get("character"))
    if item.get("appearance_canonical") or payload.get("appearance_canonical"):
        candidates.append({**payload, **item})
    for card in candidates:
        if not isinstance(card, dict):
            continue
        name = (
            card.get("name")
            or payload.get("character_name")
            or item.get("character_name")
            or (item.get("character") if isinstance(item.get("character"), str) else None)
        )
        if not name:
            continue
        merged = dict(card)
        merged["name"] = name
        merged.setdefault("role", payload.get("role") or item.get("role") or "重要配角")
        merged.setdefault("appearance_canonical", payload.get("appearance_canonical") or item.get("appearance_canonical") or "")
        merged.setdefault("personality", payload.get("personality") or item.get("personality") or "")
        merged.setdefault("speech_style", payload.get("speech_style") or item.get("speech_style") or "")
        merged.setdefault("relationships", payload.get("relationships") or item.get("relationships") or [])
        return merged
    return None

def _auto_change_portrait_id(change_id: str, item: dict | None = None) -> str | None:
    if change_id.startswith("portrait:"):
        return change_id.split(":", 1)[1]
    payload = _auto_change_payload(item or {})
    return (
        payload.get("portrait_id")
        or payload.get("previous_portrait_id")
        or payload.get("base_portrait_id")
        or (item or {}).get("portrait_id")
        or (item or {}).get("previous_portrait_id")
        or (item or {}).get("base_portrait_id")
    )

@router.get("/projects/{project_id}/auto-changes")
async def list_auto_changes(project_id: str):
    """自动变更/待审队列（人物发现与漂移记录）。"""
    _project_or_404(project_id)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
    except Exception:
        return {"items": []}
    items = []
    if row and row["bible_auto_changes_json"]:
        try:
            items = json.loads(row["bible_auto_changes_json"]) or []
        except (TypeError, ValueError, json.JSONDecodeError):
            items = []
    # 同时从定妆 change_json 汇总漂移记录
    portraits = conn.execute(
        """SELECT id, character_name, ep_start, change_json, pack_status, created_at
           FROM character_portraits WHERE project_id=? AND change_json IS NOT NULL
           ORDER BY created_at DESC LIMIT 50""",
        (project_id,),
    ).fetchall()
    for r in portraits:
        try:
            change = json.loads(r["change_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            change = {}
        items.append({
            "id": f"portrait:{r['id']}",
            "kind": "appearance_drift",
            "status": change.get("review_status") or "recorded",
            "character": r["character_name"],
            "ep_start": r["ep_start"],
            "reason": change.get("reason"),
            "change_dimensions": change.get("change_dimensions") or [],
            "persistence": change.get("persistence"),
            "pack_status": r["pack_status"],
            "created_at": r["created_at"],
            "source": "portrait_change",
        })
    return {"items": items}

@router.post("/projects/{project_id}/auto-changes/{change_id}/decide")
async def decide_auto_change(project_id: str, change_id: str, body: dict | None = None):
    """批准/拒绝/回滚自动变更记录。"""
    project = _project_or_404(project_id)
    payload = body or {}
    decision = payload.get("decision") or "approve"
    if decision not in {"approve", "reject", "rollback", "merge"}:
        raise HTTPException(422, "decision 须为 approve/reject/rollback/merge")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        items = json.loads(row["bible_auto_changes_json"] or "[]") if row and row["bible_auto_changes_json"] else []
    except Exception:
        conn.execute("ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT")
        items = []
    found = False
    matched_item = None
    for item in items:
        if item.get("id") == change_id:
            item["status"] = decision
            item["decided_at"] = now()
            item["decision_reason"] = payload.get("reason") or ""
            if decision == "merge":
                item["merge_into_character"] = payload.get("merge_into_character")
                item["merge_into_scene"] = payload.get("merge_into_scene")
            if payload.get("ep_start") is not None:
                try:
                    item["ep_start"] = max(1, int(payload["ep_start"]))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(422, "ep_start 必须是正整数") from exc
            found = True
            matched_item = item
            break
    action_result: dict = {}
    if change_id.startswith("portrait:"):
        portrait_id = change_id.split(":", 1)[1]
        prow = conn.execute(
            "SELECT change_json, pack_status FROM character_portraits WHERE id=? AND project_id=?",
            (portrait_id, project_id),
        ).fetchone()
        if prow:
            try:
                change = json.loads(prow["change_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                change = {}
            change["review_status"] = decision
            change["decision_reason"] = payload.get("reason") or ""
            change["decided_at"] = now()
            if decision == "approve":
                change["review_status"] = "approved"
            conn.execute(
                "UPDATE character_portraits SET change_json=? WHERE id=?",
                (json.dumps(change, ensure_ascii=False), portrait_id),
            )
            if decision == "reject" and prow["pack_status"] != "ready":
                conn.execute(
                    "UPDATE character_portraits SET pack_status='rejected' WHERE id=?",
                    (portrait_id,),
                )
            found = True
            action_result["portrait_id"] = portrait_id
    if matched_item and decision == "approve":
        kind = matched_item.get("kind")
        if kind in {"new_character", "character_discovery", "new_bible_character"}:
            card = _auto_change_character_card(matched_item)
            if card:
                bible = json.loads(project["bible_json"] or '{"characters":[],"world":{"visual_style_canonical":""}}')
                if not any(c.get("name") == card.get("name") for c in bible.get("characters", [])):
                    bible.setdefault("characters", []).append(card)
                    instance, errors = schema_errors(Bible, bible)
                    if errors:
                        raise HTTPException(422, "；".join(errors))
                    revision = _commit_bible_revision(
                        project_id, project, instance, reason=f"批准新增角色：{card.get('name')}"
                    )
                    action_result.update({
                        "bible_version": revision["bible_version"],
                        "artifact_id": revision["artifact_id"],
                        "added_character": card.get("name"),
                    })
        elif kind == "appearance_drift":
            portrait_id = _auto_change_portrait_id(change_id, matched_item)
            if portrait_id:
                prow = conn.execute(
                    "SELECT change_json FROM character_portraits WHERE id=? AND project_id=?",
                    (portrait_id, project_id),
                ).fetchone()
                if prow:
                    change = _parse_json_value(prow["change_json"], {})
                    if not isinstance(change, dict):
                        change = {}
                    change["review_status"] = "approved"
                    change["decision_reason"] = payload.get("reason") or ""
                    change["decided_at"] = now()
                    conn.execute(
                        "UPDATE character_portraits SET change_json=? WHERE id=?",
                        (json.dumps(change, ensure_ascii=False), portrait_id),
                    )
                    found = True
                    action_result["portrait_id"] = portrait_id
        elif kind == "scene_discovery":
            scene = _auto_change_payload(matched_item).get("scene")
            if isinstance(scene, dict) and scene.get("name"):
                bible = json.loads(project["bible_json"] or '{}')
                if not any(item.get("name") == scene["name"] for item in bible.get("scenes", [])):
                    bible.setdefault("scenes", []).append(scene)
                    instance, validation_errors = schema_errors(Bible, bible)
                    if validation_errors:
                        raise HTTPException(422, "；".join(validation_errors))
                    conn.execute(
                        "UPDATE projects SET bible_json=?,bible_version=bible_version+1 WHERE id=?",
                        (instance.model_dump_json(), project_id),
                    )
                    action_result.update({
                        "added_scene": scene["name"], "requires_payment_confirmation": True,
                        "message": "场景锚点已批准入库；出图仍需在场景库完成生成范围确认",
                    })
        elif kind == "scene_state_change":
            scene_name = str(matched_item.get("scene") or "").strip()
            change_payload = _auto_change_payload(matched_item)
            new_canonical = str(change_payload.get("new_scene_canonical") or "").strip()
            ep_start = max(1, int(matched_item.get("ep_start") or 1))
            bible = json.loads(project["bible_json"] or '{}')
            target_scene = next(
                (item for item in bible.get("scenes", []) if item.get("name") == scene_name), None,
            )
            if not target_scene or not new_canonical:
                raise HTTPException(422, "场景状态变化缺少目标场景或新锚点")
            target_scene["pending_state_canonical"] = new_canonical
            target_scene["pending_state_ep_start"] = ep_start
            instance, validation_errors = schema_errors(Bible, bible)
            if validation_errors:
                raise HTTPException(422, "；".join(validation_errors))
            conn.execute(
                "UPDATE projects SET bible_json=?,bible_version=bible_version+1 WHERE id=?",
                (instance.model_dump_json(), project_id),
            )
            current_ref = _scene_current_row(conn, project_id, scene_name)
            if current_ref and "change_json" in current_ref.keys():
                ref_change = _parse_json_value(current_ref["change_json"], {}) or {}
                ref_change.update({
                    "pending_redraw": True, "pending_state_canonical": new_canonical,
                    "pending_state_ep_start": ep_start, "approved_change_id": change_id,
                    "approved_at": now(),
                })
                conn.execute(
                    "UPDATE scene_references SET change_json=? WHERE id=?",
                    (json.dumps(ref_change, ensure_ascii=False), current_ref["id"]),
                )
            action_result.update({
                "approved_scene_change": scene_name,
                "requires_payment_confirmation": True,
                "pending_state_ep_start": ep_start,
                "message": "状态变化锚点已保存为待重绘版本；仍需在场景库完成生成范围确认",
            })
    elif matched_item and decision == "rollback":
        portrait_id = _auto_change_portrait_id(change_id, matched_item)
        if portrait_id:
            row = conn.execute(
                "SELECT * FROM character_portraits WHERE id=? AND project_id=?",
                (portrait_id, project_id),
            ).fetchone()
            if row:
                target_id = (
                    _auto_change_payload(matched_item).get("previous_portrait_id")
                    or _auto_change_payload(matched_item).get("base_portrait_id")
                    or matched_item.get("previous_portrait_id")
                    or matched_item.get("base_portrait_id")
                    or row["base_portrait_id"]
                )
                target = None
                if target_id:
                    target = conn.execute(
                        "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=?",
                        (target_id, project_id, row["character_name"]),
                    ).fetchone()
                if target is None:
                    target = conn.execute(
                        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? AND id<>? "
                        "AND (pack_status IS NULL OR pack_status='ready') ORDER BY created_at DESC LIMIT 1",
                        (project_id, row["character_name"], portrait_id),
                    ).fetchone()
                if target:
                    action_result.update(_set_current_portrait(
                        conn,
                        project_id,
                        row["character_name"],
                        target,
                        reason=payload.get("reason") or "自动变更回滚",
                        decision="rollback",
                    ))
                    found = True
    elif matched_item and decision == "merge":
        is_scene_change = str(matched_item.get("kind") or "").startswith("scene_")
        target_name = payload.get("merge_into_scene") if is_scene_change else payload.get("merge_into_character")
        if not target_name:
            raise HTTPException(422, "merge 需要明确合并目标")
        bible = json.loads(project["bible_json"] or '{"characters":[]}')
        collection = bible.get("scenes", []) if is_scene_change else bible.get("characters", [])
        if not any(c.get("name") == target_name for c in collection):
            raise HTTPException(422, f"合并目标不存在：{target_name}")
        matched_item["decision_reason"] = (
            (payload.get("reason") or "").strip()
            or f"合并到已有{'场景' if is_scene_change else '角色'}：{target_name}"
        )
        action_result["merge_into_scene" if is_scene_change else "merge_into_character"] = target_name
    if not found:
        raise HTTPException(404, "自动变更记录不存在")
    conn.execute(
        "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
        (json.dumps(items, ensure_ascii=False), project_id),
    )
    conn.commit()
    return {"ok": True, "change_id": change_id, "decision": decision, **action_result}
