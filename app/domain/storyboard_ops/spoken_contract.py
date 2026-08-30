"""台词契约审计、镜头 ID 迁移与台词冲突的预览/裁决。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 edit_shot/evidence/mutation_primitives/shot_edit_session。
"""
from __future__ import annotations

import json

from app.db import get_conn
from app.domain.common import (
    _as_body_dict,
    _episode_or_404,
    router,
)
from app.schemas import Shot
from fastapi import (
    Body,
    HTTPException,
)

from .edit_shot import edit_shot
from .evidence import _ensure_current_storyboard_shot_artifacts
from .mutation_primitives import (
    _apply_contract_to_public_shot,
    _board_from_shot_rows,
    _raise_narrative_semantic_mutation_required,
    _resolve_storyboard_mutation_screenplay,
)
from .shot_edit_session import preview_shot_edit_impact


@router.get("/episodes/{episode_id}/spoken-contract/audit")
def audit_episode_spoken_contract(episode_id: str):
    """只读审计本集口播合同（PRD §6.1）：不写库。"""
    _episode_or_404(episode_id)
    from app.spoken_contract import audit_legacy_spoken_contract, validate_spoken_contract
    from app.observability.metrics import inc

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    board = _board_from_shot_rows(rows, 1)
    results = []
    conflict_count = 0
    for shot in board.shots:
        status = audit_legacy_spoken_contract(shot)
        issues = [i.model_dump(mode="json") for i in validate_spoken_contract(shot)]
        if status == "conflict":
            conflict_count += 1
            inc("spoken_contract_conflict_total", episode_id=episode_id, shot_no=shot.shot_no, source="audit")
        results.append({
            "shot_no": shot.shot_no,
            "spoken_contract_status": status,
            "legacy_unvalidated": bool(shot.legacy_unvalidated),
            "issues": issues,
            "repair_options": [
                "rebuild_timeline_from_dialogues",
                "rebuild_dialogues_from_timeline",
            ] if status == "conflict" else [],
        })
    return {
        "episode_id": episode_id,
        "conflict_count": conflict_count,
        "shots": results,
    }

@router.post("/episodes/{episode_id}/migrate-shot-ids")
def migrate_episode_shot_ids(episode_id: str, body: dict | None = Body(None)):
    """把误写入 story_event_id 的 S* 迁移到 spine_beat_ids（PRD §6.2）。"""
    _episode_or_404(episode_id)
    dry_run = bool(_as_body_dict(body).get("dry_run", False))
    from app.continuity import migrate_shot_id_spaces, shot_contract_dict

    conn = get_conn()
    screenplay_context = _resolve_storyboard_mutation_screenplay(conn, episode_id)
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    board = _board_from_shot_rows(rows, 1)
    by_no = {int(r["shot_no"]): r for r in rows}
    changed = []
    changed_shots: list[Shot] = []
    for shot in board.shots:
        actions = migrate_shot_id_spaces(shot)
        if not actions:
            continue
        changed.append({"shot_no": shot.shot_no, "actions": actions})
        changed_shots.append(shot)
    if changed and screenplay_context.narrative_authority_required and not dry_run:
        _raise_narrative_semantic_mutation_required(operation="shot_id_migration")
    if not dry_run:
        for shot in changed_shots:
            row = by_no.get(shot.shot_no)
            if row is None:
                continue
            conn.execute(
                "UPDATE shots SET shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE id=?",
                (
                    json.dumps(shot_contract_dict(shot), ensure_ascii=False),
                    shot.continuity_mode,
                    shot.observed_state_out,
                    row["id"],
                ),
            )
    if not dry_run and changed:
        conn.commit()
        current_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        _ensure_current_storyboard_shot_artifacts(
            conn,
            episode_id,
            _board_from_shot_rows(current_rows, 1),
        )
    return {"episode_id": episode_id, "dry_run": dry_run, "changed": changed}

@router.post("/shots/{shot_id}/resolve-spoken-conflict")
async def resolve_spoken_conflict(shot_id: str, body: dict):
    """人工选择口播基准并同步（PRD §6.1 / §7.2）。

    choice:
      - rebuild_timeline_from_dialogues
      - rebuild_dialogues_from_timeline
    若镜头已有付费视频，必须 set invalidate_media=true，否则 409。
    """
    choice = (body or {}).get("choice") or ""
    invalidate_media = bool((body or {}).get("invalidate_media", False))
    if choice not in {"rebuild_timeline_from_dialogues", "rebuild_dialogues_from_timeline"}:
        raise HTTPException(422, "choice 必须是 rebuild_timeline_from_dialogues 或 rebuild_dialogues_from_timeline")
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    has_paid = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM shot_versions WHERE shot_id=? AND status IN ('done','generating','pending')) AS present",
        (shot_id,),
    ).fetchone()["present"]
    if has_paid and not invalidate_media:
        raise HTTPException(
            409,
            "本镜已有付费视频产物；请确认 invalidate_media=true 使旧视频失效后再改口播基准",
        )
    # 复用 edit_shot：只改一侧字段，触发 synchronize_spoken_contract 定向重建。
    patch: dict = {
        "expected_version": shot["storyboard_artifact_id"],
        "edit_session_token": body.get("edit_session_token"),
        "baseline_content_hash": body.get("baseline_content_hash"),
        "preview_token": body.get("preview_token"),
        "change_source": "spoken_conflict_resolution",
    }
    if choice == "rebuild_timeline_from_dialogues":
        patch["dialogues"] = json.loads(shot["dialogues"] or "[]")
    else:
        from app.continuity import apply_shot_contract
        temp = Shot(
            shot_no=shot["shot_no"], duration_s=shot["duration_s"], shot_size=shot["shot_size"],
            camera_move=shot["camera_move"],
            scene_time=(shot["scene_time"] if "scene_time" in shot.keys() else "") or "",
            scene_setting=shot["scene_setting"],
            scene_name=(shot["scene_name"] if "scene_name" in shot.keys() else "") or "",
            characters=json.loads(shot["characters"] or "[]"),
            action_desc=shot["action_desc"], first_frame_desc=shot["first_frame_desc"] or "",
            last_frame_desc=shot["last_frame_desc"] or "", source_excerpt=shot["source_excerpt"] or "",
            narration=shot["narration"], dialogues=json.loads(shot["dialogues"] or "[]"),
            transition=shot["transition"] or "硬切",
            continuity_from_prev=bool(shot["continuity_from_prev"]),
        )
        apply_shot_contract(temp, shot["shot_contract_json"] if "shot_contract_json" in shot.keys() else None)
        patch["audio_timeline"] = [item.model_dump(mode="json") for item in (temp.audio_timeline or [])]
    patch["spoken_contract_status"] = "coherent"
    result = await edit_shot(shot_id, patch)
    from app.observability.metrics import inc
    inc(
        "spoken_contract_conflict_total",
        episode_id=shot["episode_id"],
        shot_no=shot["shot_no"],
        source="resolved",
        choice=choice,
    )
    return {"ok": True, "choice": choice, **(result if isinstance(result, dict) else {})}

@router.post("/shots/{shot_id}/spoken-conflict-preview")
def preview_spoken_conflict(shot_id: str, body: dict):
    from app.storyboard_workspace import create_edit_session

    choice = (body or {}).get("choice") or ""
    if choice not in {"rebuild_timeline_from_dialogues", "rebuild_dialogues_from_timeline"}:
        raise HTTPException(422, "请选择以台词或时间轴为准")
    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "镜头不存在")
    public = dict(row)
    public["characters"] = json.loads(public["characters"] or "[]")
    public["dialogues"] = json.loads(public["dialogues"] or "[]")
    _apply_contract_to_public_shot(public)
    changes = (
        {"dialogues": public["dialogues"], "spoken_contract_status": "coherent"}
        if choice == "rebuild_timeline_from_dialogues"
        else {"audio_timeline": public.get("audio_timeline") or [], "spoken_contract_status": "coherent"}
    )
    session = create_edit_session(shot_id)
    impact = preview_shot_edit_impact(shot_id, {
        "edit_session_token": session["edit_session_token"],
        "changes": changes,
    })
    if impact.get("unchanged"):
        # 即使其中一侧结构相同，也需要明确变更来源来完成冲突状态同步。
        raise HTTPException(409, "所选口播基准没有可重建内容，请选择另一侧或继续编辑")
    return {**impact, **session, "choice": choice}
