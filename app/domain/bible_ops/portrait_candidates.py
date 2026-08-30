"""角色立绘候选图的查询、采纳与回滚，及其内部通用 payload 组装。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    new_id,
    now,
    rows_to_dicts,
)
from app.domain.common import (
    _media_url,
    _project_or_404,
    router,
)
from fastapi import HTTPException
from pathlib import Path

from .primitives import _parse_json_value


def _portrait_views_for(conn, portrait_id: str) -> list[dict]:
    try:
        rows = rows_to_dicts(conn.execute(
            "SELECT * FROM character_portrait_views WHERE portrait_id=? "
            "ORDER BY view_role, selected DESC, (status='ready') DESC, created_at DESC",
            (portrait_id,),
        ).fetchall())
    except Exception:  # noqa: BLE001
        return []
    views: list[dict] = []
    seen_roles: set[str] = set()
    for row in rows:
        view_role = str(row.get("view_role") or "")
        if view_role in seen_roles:
            continue
        seen_roles.add(view_role)
        qa = _parse_json_value(row.get("qa_json"), {})
        views.append({
            "id": row.get("id"),
            "view_role": row.get("view_role"),
            "framing": row.get("framing"),
            "status": row.get("status"),
            "selected": bool(row.get("selected", 1)),
            "image_url": _media_url(row.get("image_path")),
            "qa": qa,
            "qa_overall": qa.get("overall") if isinstance(qa, dict) else None,
        })
    return views

def _portrait_candidate_payload(row, views: list[dict] | None = None) -> dict:
    group_qa = _parse_json_value(row["group_qa_json"] if "group_qa_json" in row.keys() else None, {})
    change = _parse_json_value(row["change_json"] if "change_json" in row.keys() else None, {})
    view_items = views if views is not None else _portrait_views_for(get_conn(), row["id"])
    return {
        "id": row["id"],
        "portrait_id": row["id"],
        "project_id": row["project_id"],
        "character_name": row["character_name"],
        "ep_start": row["ep_start"],
        "ep_end": row["ep_end"],
        "historical": int(row["ep_start"] or 0) <= 0 or row["ep_end"] is not None,
        "is_current": row["ep_end"] is None,
        "appearance": row["appearance"],
        "prompt": row["prompt"],
        "base_portrait_id": row["base_portrait_id"],
        "bible_version": row["bible_version"],
        "artifact_id": row["artifact_id"] if "artifact_id" in row.keys() else None,
        "pack_status": row["pack_status"] if "pack_status" in row.keys() else None,
        "group_qa": group_qa,
        "change": change,
        "image_url": _media_url(row["image_path"]),
        "views": view_items,
        "created_at": row["created_at"],
    }

def _portrait_artifact_candidate_payload(conn, row) -> dict:
    """Expose generated front-image candidates that failed before a pack existed.

    These artifacts are deliberately not adoptable as production portraits: they
    have not completed the three required views.  They remain visible so a user
    can inspect the actual image and QA evidence instead of seeing a misleading
    provider failure with an empty candidate list.
    """
    content = _parse_json_value(row["content_json"] if "content_json" in row.keys() else None, {})
    if not isinstance(content, dict):
        content = {}
    try:
        evaluations = conn.execute(
            "SELECT * FROM evaluations WHERE artifact_id=? ORDER BY created_at DESC",
            (row["id"],),
        ).fetchall()
    except Exception:  # noqa: BLE001 - compatibility with historical/minimal schemas
        evaluations = []

    model_eval = next((item for item in evaluations if item["evaluator_type"] == "model"), None)
    file_eval = next((item for item in evaluations if item["evaluator_type"] == "file"), None)
    evidence = _parse_json_value(
        model_eval["evidence_json"] if model_eval and "evidence_json" in model_eval.keys() else None,
        {},
    )
    qa = dict(evidence.get("qa") or {}) if isinstance(evidence, dict) else {}
    raw_issues = _parse_json_value(
        model_eval["issues_json"] if model_eval and "issues_json" in model_eval.keys() else None,
        [],
    )
    hard: list[str] = []
    warnings: list[str] = []
    for issue in raw_issues if isinstance(raw_issues, list) else []:
        if isinstance(issue, dict):
            message = str(issue.get("message") or issue.get("code") or "").strip()
            severity = str(issue.get("severity") or "").lower()
            if not message:
                continue
            if severity in {"blocker", "critical", "error"}:
                hard.append(message)
            else:
                warnings.append(message)
        elif str(issue).strip():
            warnings.append(str(issue).strip())
    if model_eval is not None:
        if model_eval["score"] is not None and not isinstance(qa.get("overall"), (int, float)):
            qa["overall"] = float(model_eval["score"]) / 100.0
        failed = not bool(model_eval["hard_gate_passed"])
        qa["status"] = "failed" if failed else str(model_eval["status"] or "unverified")
        if failed and not hard:
            hard.append("人物一致性 QA 未通过")
    elif file_eval is not None and not bool(file_eval["hard_gate_passed"]):
        qa["status"] = "failed"
        hard.append("图片技术校验未通过")
    else:
        qa.setdefault("status", "unverified")
    qa["hard_failures"] = list(dict.fromkeys([*(qa.get("hard_failures") or []), *hard]))
    qa["issues"] = list(dict.fromkeys([*(qa.get("issues") or []), *warnings]))

    return {
        "id": row["id"],
        "artifact_id": row["id"],
        "project_id": str(row["scope_id"] or "").split(":", 1)[0],
        "character_name": content.get("character_name"),
        "candidate_kind": "single_image",
        "attempt": content.get("attempt"),
        "status": "failed" if qa.get("status") == "failed" else "unverified",
        "pack_status": "not_built",
        "group_qa": qa,
        "qa": qa,
        "image_url": _media_url(row["file_path"] if "file_path" in row.keys() else None),
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
        "adoptable": False,
        "blocked_reason": (
            "该图只完成了正面单图阶段，尚未形成正面、3/4 面、侧面三视角包；"
            "可用于人工复核和重新生成，但不能直接标记为生产可用定妆包。"
        ),
    }

def _portrait_gate_lists(row, views: list[dict]) -> tuple[list[str], list[str]]:
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    group_qa = _parse_json_value(row["group_qa_json"] if "group_qa_json" in row.keys() else None, {})
    hard: list[str] = []
    soft: list[str] = []
    if isinstance(group_qa, dict):
        hard.extend(str(x) for x in (group_qa.get("hard_failures") or []) if str(x).strip())
        soft.extend(str(x) for x in (group_qa.get("issues") or []) if str(x).strip())
        if group_qa.get("status") and group_qa.get("status") != "ready":
            hard.append(f"group_qa_status={group_qa.get('status')}")
        hard.extend(str(x) for x in (group_qa.get("failed_views") or []) if str(x).strip())
        for view in group_qa.get("views") or []:
            if not isinstance(view, dict):
                continue
            hard.extend(str(x) for x in (view.get("hard_failures") or []) if str(x).strip())
            soft.extend(str(x) for x in (view.get("issues") or []) if str(x).strip())
    for view in views:
        qa = view.get("qa") if isinstance(view.get("qa"), dict) else {}
        hard.extend(str(x) for x in (qa.get("hard_failures") or []) if str(x).strip())
        soft.extend(str(x) for x in (qa.get("issues") or []) if str(x).strip())
        if view.get("status") != "ready" or not (view.get("image_path") or view.get("image_url")):
            hard.append(f"{view.get('view_role')}:status={view.get('status') or 'missing'}")
    ready_roles = {
        view.get("view_role") for view in views
        if view.get("view_role") in CHARACTER_REQUIRED_VIEWS
        and view.get("status") == "ready" and (view.get("image_path") or view.get("image_url"))
    }
    for missing_role in CHARACTER_REQUIRED_VIEWS:
        if missing_role not in ready_roles:
            hard.append(f"missing_required_view={missing_role}")
    pack_status = row["pack_status"] if "pack_status" in row.keys() else None
    if pack_status and pack_status != "ready":
        hard.append(f"pack_status={pack_status}")
    return list(dict.fromkeys(hard)), list(dict.fromkeys(soft))

def _set_current_portrait(
    conn,
    project_id: str,
    character_name: str,
    row,
    *,
    reason: str,
    decision: str,
) -> dict:
    stamp = now()
    target_start = int(row["ep_start"] or 1)
    adopted_start = target_start
    if target_start <= 0:
        # 初始包历史版本使用负数槽位避开 (project, character, ep_start)
        # 唯一约束；回滚时先将当前 ep=1 版本移入新历史槽，再恢复目标。
        minimum = conn.execute(
            "SELECT MIN(ep_start) AS value FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=0",
            (project_id, character_name),
        ).fetchone()
        history_start = int(minimum["value"] if minimum and minimum["value"] is not None else 0) - 1
        conn.execute(
            "UPDATE character_portraits SET ep_start=?, ep_end=0 "
            "WHERE project_id=? AND character_name=? AND id<>? AND ep_end IS NULL",
            (history_start, project_id, character_name, row["id"]),
        )
        adopted_start = 1
    else:
        ep_end = max(target_start - 1, 0)
        conn.execute(
            "UPDATE character_portraits SET ep_end=? "
            "WHERE project_id=? AND character_name=? AND id<>? AND ep_end IS NULL",
            (ep_end, project_id, character_name, row["id"]),
        )
    change = _parse_json_value(row["change_json"] if "change_json" in row.keys() else None, {})
    if not isinstance(change, dict):
        change = {}
    change.update({
        "review_status": decision,
        "adoption_reason": reason,
        "decided_at": stamp,
    })
    conn.execute(
        "UPDATE character_portraits SET ep_start=?, ep_end=NULL, change_json=? WHERE id=?",
        (adopted_start, json.dumps(change, ensure_ascii=False), row["id"]),
    )
    prow = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if prow and prow["bible_json"]:
        bible = json.loads(prow["bible_json"])
        for character in bible.get("characters", []):
            if character.get("name") == character_name:
                character["ref_image_path"] = row["image_path"]
                break
        conn.execute(
            "UPDATE projects SET bible_json=? WHERE id=?",
            (json.dumps(bible, ensure_ascii=False), project_id),
        )
    artifact_id = row["artifact_id"] if "artifact_id" in row.keys() else None
    if artifact_id:
        conn.execute(
            "INSERT INTO gate_decisions(id, artifact_id, gate_key, decision, decided_by, reason, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (new_id("gate"), artifact_id, "portrait_adoption", decision, "bible_editor", reason, stamp),
        )
    conn.commit()
    return {"portrait_id": row["id"], "character_name": character_name, "ep_start": adopted_start}

def _adopt_portrait_by_id(
    project_id: str,
    character_name: str,
    portrait_id: str,
    *,
    reason: str,
    bypass_soft: bool = False,
    decision: str = "approve",
) -> dict:
    _project_or_404(project_id)
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=?",
        (portrait_id, project_id, character_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "造型版本不存在")
    image_path = str(row["image_path"] or "").strip()
    if not image_path or not Path(image_path).is_file():
        raise HTTPException(409, {
            "code": "PORTRAIT_FILE_UNAVAILABLE",
            "message": "候选定妆主图文件不可用",
        })
    views = _portrait_views_for(conn, portrait_id)
    hard, soft = _portrait_gate_lists(row, views)
    del bypass_soft
    quality_warnings = list(dict.fromkeys([*hard, *soft]))
    result = _set_current_portrait(
        conn, project_id, character_name, row, reason=reason, decision=decision,
    )
    return {
        **result,
        "soft_warnings": quality_warnings,
        "gate_retry_exhausted": bool(hard),
        "candidate": _portrait_candidate_payload(row, views),
    }

@router.get("/projects/{project_id}/characters/{character_name}/portrait-candidates")
async def list_portrait_candidates(project_id: str, character_name: str):
    """列出角色定妆候选与历史包。"""
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "ORDER BY ep_start DESC, created_at DESC",
        (project_id, character_name),
    ).fetchall()
    portrait_items = [_portrait_candidate_payload(row) for row in rows]
    attached_artifact_ids = {
        str(row["artifact_id"]) for row in rows
        if "artifact_id" in row.keys() and row["artifact_id"]
    }
    try:
        artifact_rows = conn.execute(
            "SELECT * FROM artifacts WHERE type='character_portrait' "
            "AND scope_type='reference_asset' AND scope_id=? "
            "ORDER BY created_at DESC LIMIT 30",
            (f"{project_id}:{character_name}:1",),
        ).fetchall()
    except Exception:  # noqa: BLE001 - old databases may not have evidence tables
        artifact_rows = []
    raw_candidates = [
        _portrait_artifact_candidate_payload(conn, row)
        for row in artifact_rows if str(row["id"]) not in attached_artifact_ids
    ]
    return {
        "project_id": project_id,
        "character_name": character_name,
        "items": [*portrait_items, *raw_candidates],
    }

@router.post("/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/adopt")
async def adopt_portrait_candidate(
    project_id: str, character_name: str, portrait_id: str, body: dict | None = None,
):
    payload = body or {}
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(422, "采纳候选必须填写 reason")
    result = _adopt_portrait_by_id(
        project_id,
        character_name,
        portrait_id,
        reason=reason,
        bypass_soft=payload.get("bypass_soft") is True,
        decision="approve",
    )
    return {"adopted": True, **result}

@router.post("/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/rollback")
async def rollback_portrait_candidate(
    project_id: str, character_name: str, portrait_id: str, body: dict | None = None,
):
    payload = body or {}
    reason = str(payload.get("reason") or "回滚到上一可用定妆包").strip()
    _project_or_404(project_id)
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=?",
        (portrait_id, project_id, character_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "造型版本不存在")
    target = None
    if row["base_portrait_id"]:
        target = conn.execute(
            "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=? "
            "AND (pack_status IS NULL OR pack_status='ready')",
            (row["base_portrait_id"], project_id, character_name),
        ).fetchone()
    if target is None:
        target = conn.execute(
            "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? AND id<>? "
            "AND (pack_status IS NULL OR pack_status='ready') "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id, character_name, portrait_id),
        ).fetchone()
    if target is None:
        raise HTTPException(409, "没有可回滚的 ready 定妆包")
    result = _adopt_portrait_by_id(
        project_id,
        character_name,
        target["id"],
        reason=reason,
        bypass_soft=True,
        decision="rollback",
    )
    return {"rolled_back": True, "from_portrait_id": portrait_id, **result}
