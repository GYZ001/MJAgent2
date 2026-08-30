"""分镜结构调整（拆镜/合镜/重排）的预览与落库事务。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 evidence 与 mutation_primitives。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    get_setting,
)
from app.domain.common import router
from app.schemas import (
    StoryboardOutline,
    StoryboardOutlineShot,
)
from fastapi import HTTPException

from .evidence import _ensure_current_storyboard_shot_artifacts
from .mutation_primitives import (
    _board_from_shot_rows,
    _insert_storyboard_shot,
    _raise_narrative_semantic_mutation_required,
    _resolve_storyboard_mutation_screenplay,
)


def _structure_operation_plan(episode_id: str, body: dict) -> dict:
    if str(get_setting("storyboard_structure_edit_enabled") or "true").lower() != "true":
        raise HTTPException(409, "镜头结构调整当前未开放；可修改现有问题镜或继续 Agent 修复")
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise HTTPException(404, "剧集不存在")
    from app.storyboard_workspace import assert_storyboard_mutation_allowed

    assert_storyboard_mutation_allowed(conn, episode_id)
    screenplay_context = _resolve_storyboard_mutation_screenplay(conn, episode_id)
    if screenplay_context.narrative_authority_required:
        _raise_narrative_semantic_mutation_required(
            operation=str(body.get("operation") or "structure_edit"),
        )
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "当前没有可调整的镜头")
    operation = str(body.get("operation") or "")
    if operation not in {"add_after", "duplicate_after", "delete", "move"}:
        raise HTTPException(422, "结构操作必须是新增、复制、删除或移动")
    shot_id = str(body.get("shot_id") or "")
    index = next((idx for idx, row in enumerate(rows) if row["id"] == shot_id), -1)
    if index < 0:
        raise HTTPException(404, "目标镜头不存在")
    target_index = int(body.get("target_index", index))
    target_index = max(0, min(len(rows) - 1, target_index))
    contract = {}
    try:
        contract = json.loads(rows[index]["shot_contract_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        contract = {}
    if operation == "delete" and len(rows) == 1:
        raise HTTPException(409, "不能删除全剧唯一镜头")
    if operation == "delete" and contract.get("is_final") and not body.get("new_final_shot_id"):
        raise HTTPException(409, "删除收尾镜前必须指定新的收尾镜")
    new_count = len(rows) + (1 if operation in {"add_after", "duplicate_after"} else -1 if operation == "delete" else 0)
    old_order = [row["id"] for row in rows]
    preview_order = list(old_order)
    placeholder = "new-shot" if operation == "add_after" else "copy-shot"
    if operation in {"add_after", "duplicate_after"}:
        preview_order.insert(index + 1, placeholder)
    elif operation == "delete":
        preview_order.pop(index)
    else:
        moved = preview_order.pop(index)
        preview_order.insert(target_index, moved)
    affected_nos = sorted({
        max(1, index), index + 1, min(max(1, new_count), index + 2),
        target_index + 1,
    })
    version_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v JOIN shots s ON s.id=v.shot_id
           WHERE s.episode_id=?""", (episode_id,),
    ).fetchone()["c"])
    return {
        "operation": operation,
        "shot_id": shot_id,
        "target_index": target_index,
        "new_final_shot_id": body.get("new_final_shot_id"),
        "before_count": len(rows),
        "after_count": new_count,
        "before_order": old_order,
        "after_order": preview_order,
        "renumbered_shots": sum(1 for i, value in enumerate(preview_order) if i >= len(old_order) or value != old_order[i]),
        "revalidation_shots": affected_nos,
        "requires_reconfirm": True,
        "paid_media_invalidated": version_count > 0,
        "stale_count": version_count,
        "by_artifact_type": {"视频版本": version_count},
        "final_shot_impact": "将重新指定收尾镜" if operation == "delete" and contract.get("is_final") else "收尾镜保持唯一",
    }

@router.post("/episodes/{episode_id}/storyboard/structure-preview")
def preview_storyboard_structure(episode_id: str, body: dict):
    from app.storyboard_workspace import create_preview, episode_fingerprint
    payload = _structure_operation_plan(episode_id, body)
    return create_preview(
        "structure", episode_id, payload,
        shot_id=payload["shot_id"], baseline_fingerprint=episode_fingerprint(episode_id),
    )

def _set_row_final_contract(conn, shot_id: str, final: bool) -> None:
    row = conn.execute("SELECT shot_contract_json FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        return
    try:
        contract = json.loads(row["shot_contract_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        contract = {}
    contract["is_final"] = bool(final)
    conn.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id=?",
        (json.dumps(contract, ensure_ascii=False), shot_id),
    )

@router.post("/episodes/{episode_id}/storyboard/structure")
def apply_storyboard_structure(episode_id: str, body: dict):
    from app.completion_grant import ProviderTasksNotTerminalError

    conn = get_conn()
    try:
        return _apply_storyboard_structure_transaction(episode_id, body)
    except ProviderTasksNotTerminalError as exc:
        if conn.in_transaction:
            conn.rollback()
        raise HTTPException(409, exc.detail) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

def _apply_storyboard_structure_transaction(episode_id: str, body: dict):
    from app.artifacts import (
        flush_media_cleanup_outbox,
        stage_shot_artifact_cleanup,
    )
    from app.storyboard_workspace import (
        assert_storyboard_mutation_allowed,
        persist_source_binding,
        require_preview,
        source_binding_for_shot,
    )

    preview = require_preview(
        body.get("preview_token"), "structure", episode_id,
        shot_id=str(body.get("shot_id") or ""),
    )
    expected = {
        "operation": body.get("operation"),
        "shot_id": body.get("shot_id"),
        "target_index": int(body.get("target_index", preview.get("target_index", 0))),
        "new_final_shot_id": body.get("new_final_shot_id"),
    }
    for key, value in expected.items():
        if value != preview.get(key):
            raise HTTPException(409, "结构操作与已批准预览不一致，请重新预览")
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    assert_storyboard_mutation_allowed(conn, episode_id)
    require_preview(
        body.get("preview_token"),
        "structure",
        episode_id,
        shot_id=str(body.get("shot_id") or ""),
        consume=True,
    )
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    cleanup_outbox_ids: list[str] = []
    invalidated = 0
    for row in rows:
        cleanup = stage_shot_artifact_cleanup(conn, str(row["id"]))
        invalidated += int(cleanup.get("videos", 0)) + int(cleanup.get("references", 0))
        if cleanup.get("outbox_id"):
            cleanup_outbox_ids.append(str(cleanup["outbox_id"]))
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    screenplay_context = _resolve_storyboard_mutation_screenplay(conn, episode_id)
    screenplay = screenplay_context.screenplay
    if screenplay_context.narrative_authority_required:
        _raise_narrative_semantic_mutation_required(
            operation=str(body.get("operation") or "structure_edit"),
        )
    previous_outline_by_id: dict[str, StoryboardOutlineShot] = {}
    try:
        previous_outline = StoryboardOutline.model_validate_json(ep["storyboard_outline_json"] or "{}")
        previous_outline_by_id = {
            row["id"]: previous_outline.shots[int(row["shot_no"]) - 1]
            for row in rows
            if 0 < int(row["shot_no"]) <= len(previous_outline.shots)
        }
    except (TypeError, ValueError, IndexError):
        previous_outline_by_id = {}
    by_id = {row["id"]: row for row in rows}
    target = by_id.get(str(body.get("shot_id")))
    if not target:
        raise HTTPException(404, "目标镜头不存在")
    operation = str(body["operation"])
    ordered_ids = [row["id"] for row in rows]
    created_id = None
    deleted_id = None
    if operation in {"add_after", "duplicate_after"}:
        source_model = _board_from_shot_rows([target], 1).shots[0].model_copy(deep=True)
        source_model.shot_no = int(target["shot_no"]) + 1
        source_model.is_final = False
        if operation == "add_after":
            source_model.dialogues = []
            source_model.audio_timeline = []
            source_model.primary_action = ""
            source_model.action_desc = "请补充本镜画面动作"
        conn.execute("UPDATE shots SET shot_no=-shot_no WHERE episode_id=?", (episode_id,))
        created_id = _insert_storyboard_shot(conn, episode_id, screenplay, source_model)
        insert_at = ordered_ids.index(target["id"]) + 1
        ordered_ids.insert(insert_at, created_id)
        binding = source_binding_for_shot(target["id"])
        if binding:
            persist_source_binding(
                created_id,
                binding,
                conn=conn,
                commit=False,
            )
    elif operation == "delete":
        deleted_id = target["id"]
        ordered_ids.remove(deleted_id)
        conn.execute("DELETE FROM shots WHERE id=?", (deleted_id,))
        conn.execute("UPDATE shots SET shot_no=-shot_no WHERE episode_id=?", (episode_id,))
    else:
        ordered_ids.remove(target["id"])
        ordered_ids.insert(int(preview["target_index"]), target["id"])
        conn.execute("UPDATE shots SET shot_no=-shot_no WHERE episode_id=?", (episode_id,))
    for index, item_id in enumerate(ordered_ids, start=1):
        conn.execute("UPDATE shots SET shot_no=? WHERE id=?", (index, item_id))
    final_id = body.get("new_final_shot_id")
    if final_id:
        if final_id not in ordered_ids:
            raise HTTPException(422, "指定的新收尾镜不存在")
        for item_id in ordered_ids:
            _set_row_final_contract(conn, item_id, item_id == final_id)
    else:
        finals = []
        for item_id in ordered_ids:
            row = conn.execute("SELECT shot_contract_json FROM shots WHERE id=?", (item_id,)).fetchone()
            try:
                if json.loads(row["shot_contract_json"] or "{}").get("is_final"):
                    finals.append(item_id)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if len(finals) != 1:
            for item_id in ordered_ids:
                _set_row_final_contract(conn, item_id, item_id == ordered_ids[-1])
    # 结构操作本身就是对“计划镜头序列”的修改。把新的连续顺序同步回唯一计划源，
    # 否则旧 checkpoint 的 expected_total 会令新增/删除后的工作区永远无法进入完整终态。
    current_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    outline_shots: list[StoryboardOutlineShot] = []
    for row in current_rows:
        model = _board_from_shot_rows([row], int(ep["episode_no"])).shots[0]
        prior = previous_outline_by_id.get(row["id"])
        if row["id"] == created_id and operation == "duplicate_after":
            prior = previous_outline_by_id.get(target["id"])
        brief = prior.model_copy(deep=True) if prior else StoryboardOutlineShot(shot_no=int(row["shot_no"]))
        brief.shot_no = int(row["shot_no"])
        brief.scene_time = model.scene_time or ""
        brief.scene_name = model.scene_name or ""
        brief.scene_setting = model.scene_setting or ""
        brief.beat = (model.action_desc or model.primary_action or "请补充本镜画面动作").strip()
        brief.covers = model.source_excerpt or ""
        brief.primary_action = model.primary_action or ""
        brief.emotion_beat = model.emotion_beat or ""
        brief.state_in = model.state_in or ""
        brief.state_out = model.state_out or ""
        brief.continuity_mode = model.continuity_mode or ""
        brief.duration_s = int(model.duration_s or 0) or None
        brief.characters_visible = list(model.characters_visible or [])
        brief.audio_cast = list(model.audio_cast or [])
        if row["id"] == created_id and operation == "add_after":
            brief.story_event_id = ""
            brief.spine_beat_ids = []
            brief.key_line_ids = []
            brief.information_ids = []
            brief.new_information_ids = []
        outline_shots.append(brief)
    updated_outline = StoryboardOutline(episode_no=int(ep["episode_no"]), shots=outline_shots)
    from app.storyboard_authority import persist_storyboard_outline_projection

    persist_storyboard_outline_projection(
        episode_id,
        updated_outline,
        conn=conn,
        commit=False,
    )
    conn.execute(
        """UPDATE episodes
              SET status='scripted', script_error=NULL,
                  storyboard_artifact_id=NULL
            WHERE id=?""",
        (episode_id,),
    )
    _ensure_current_storyboard_shot_artifacts(
        conn,
        episode_id,
        _board_from_shot_rows(current_rows, int(ep["episode_no"])),
        commit=False,
    )
    conn.commit()
    for outbox_id in cleanup_outbox_ids:
        flush_media_cleanup_outbox(outbox_id)
    return {
        "ok": True,
        "operation": operation,
        "created_shot_id": created_id,
        "deleted_shot_id": deleted_id,
        "shot_count": len(ordered_ids),
        "invalidated": invalidated,
        "requires_reconfirm": True,
        "revalidation_shots": preview.get("revalidation_shots") or [],
    }
