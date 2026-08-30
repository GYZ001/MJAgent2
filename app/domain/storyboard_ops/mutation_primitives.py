"""分镜镜头写操作的共享原语：契约 JSON、叙事语义编辑字段判定、写权限断言、插入镜头、镜头列表投影。

从 app/domain/storyboard_ops.py 按原样搬移；被本包几乎所有其它子模块依赖，是本包唯一没有反向依赖的基础层之一。
"""
from __future__ import annotations

import json

from app import errors
from app.db import new_id
from app.schemas import (
    EpisodeScreenplay,
    Shot,
    Storyboard,
)
from app.validators import normalize_action_desc
from fastapi import HTTPException


def _shot_contract_json(shot: Shot) -> str:
    from app.continuity import shot_contract_dict
    return json.dumps(shot_contract_dict(shot), ensure_ascii=False)

_NARRATIVE_PRESENTATION_EDIT_FIELDS = frozenset({
    "duration_s", "shot_size", "camera_move", "scene_time", "scene_name",
    "scene_setting", "characters", "action_desc", "first_frame_desc",
    "last_frame_desc", "source_excerpt", "dialogues", "audio_timeline",
    "transition", "camera_angle", "spatial_anchor", "risk_tags",
    "context_requirement_ids", "resulting_change", "readability_focus",
    "camera_motivation", "repeat_of_shot_id", "repeat_gain",
})

def _resolve_storyboard_mutation_screenplay(conn, episode_id: str):
    """Resolve the single screenplay authority used by every manual mutation."""
    from app.errors import ArtifactNeedsRebuildError
    from app.production.screenplay_authority import resolve_downstream_screenplay

    try:
        return resolve_downstream_screenplay(episode_id, conn=conn)
    except ArtifactNeedsRebuildError as exc:
        raise HTTPException(409, {
            "code": "ARTIFACT_NEEDS_REBUILD",
            "message": str(exc),
            "action": "请先重建并重新发布剧本，再继续分镜",
        }) from exc
    except Exception as exc:  # noqa: BLE001 - mutation boundary must fail closed
        raise HTTPException(409, {
            "code": "storyboard_screenplay_authority_invalid",
            "message": f"分镜写入所依赖的已发布剧本权威链无效：{exc}",
            "action": "请先恢复剧本 Artifact、页面投影、完成凭证与发布 revision 的一致性",
        }) from exc

def _screenplay_rebuild_block(conn, ep) -> dict | None:
    from app.production.screenplay_authority import (
        published_stale_screenplay_rebuild_error,
    )

    rebuild_error = published_stale_screenplay_rebuild_error(ep, conn=conn)
    if rebuild_error is None:
        return None
    return {
        "code": rebuild_error.code,
        "message": str(rebuild_error),
        "action": "请先重建并重新发布剧本，再继续分镜",
    }

def _narrative_semantic_edit_fields(changed_fields) -> list[str]:
    return sorted(set(changed_fields) - _NARRATIVE_PRESENTATION_EDIT_FIELDS)

def _raise_narrative_semantic_mutation_required(
    *,
    operation: str,
    fields: list[str] | None = None,
) -> None:
    detail = {
        "code": "narrative_semantic_repair_required",
        "message": "叙事权威分镜不允许原地改写结构或语义合同",
        "operation": operation,
        "action": "请创建语义修复 candidate，依次完成全板叙事验证、冷观众盲审和原子发布",
    }
    if fields:
        detail["fields"] = fields
    raise HTTPException(409, detail)

def _apply_contract_to_public_shot(target: dict) -> None:
    from app.continuity import apply_shot_contract, spoken_chars_from_shot
    from app.spoken_contract import max_speech_chars
    shot = Shot(
        shot_no=target["shot_no"],
        shot_uid=target.get("shot_uid") or "",
        duration_s=target["duration_s"],
        shot_size=target["shot_size"],
        camera_move=target["camera_move"],
        scene_time=target.get("scene_time") or "",
        scene_setting=target["scene_setting"],
        scene_name=target.get("scene_name") or "",
        characters=target.get("characters") or [],
        action_desc=target["action_desc"],
        first_frame_desc=target.get("first_frame_desc") or "",
        last_frame_desc=target.get("last_frame_desc") or "",
        source_excerpt=target.get("source_excerpt") or "",
        narration=target.get("narration"),
        dialogues=target.get("dialogues") or [],
        transition=target.get("transition") or "硬切",
        continuity_from_prev=bool(target.get("continuity_from_prev")),
        continuity_mode=target.get("continuity_mode") or "",
        observed_state_out=target.get("observed_state_out") or "",
    )
    apply_shot_contract(shot, target.get("shot_contract_json"))
    # Persist/project the canonical Shot model as a whole.  Enumerating a
    # hand-maintained subset silently erased every new authority field whenever
    # a user edited an unrelated presentation property.
    for key, value in shot.model_dump(mode="json").items():
        target[key] = value
    target["spoken_content_chars"] = spoken_chars_from_shot(shot)
    target["spoken_limit"] = max_speech_chars(int(target.get("duration_s") or shot.duration_s))
    target["has_legacy_narration"] = bool((target.get("narration") or "").strip())

def _assert_storyboard_write_authorized(
    conn, episode_id: str, expected_screenplay_artifact_id: str | None
) -> None:
    row = conn.execute(
        "SELECT screenplay_publish_fence, screenplay_artifact_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("分镜写入被拒绝：剧集已不存在")
    current = row["screenplay_artifact_id"] or ""
    expected = expected_screenplay_artifact_id or ""
    if row["screenplay_publish_fence"] or (expected and expected != current):
        # 回滚必须在 errors.log_error() 之前——同一原因见
        # _storyboard_task 顶层 except 分支上方的大注释：app.db.insert_error_log
        # 在这同一个 task 缓存连接上落一条 error_logs 行并 conn.commit()，谁先
        # 调用谁就先把此刻挂起的写入定型。这个守卫函数不止在事务开局被调用——
        # _insert_storyboard_shot 会在 _apply_storyboard_structure_transaction
        # 已经显式 BEGIN IMMEDIATE、且已执行过
        # ``UPDATE shots SET shot_no=-shot_no WHERE episode_id=?``（结构编辑的
        # 中间步骤：先把全部 shot_no 取负腾位置，稍后再重新编号）之后才调用它。
        # 如果这里先 log_error 后置守卫失败即抛，实现在 apply_storyboard_structure
        # 路由处的 ``except Exception: if conn.in_transaction: conn.rollback()``
        # 保护网会晚一步——commit 已经在这条 log_error 里发生，事务已经不在途，
        # 外层的 rollback 变成空操作，shot_no 全部取负这个半成品状态就被当成正常
        # 结果提交进库，后续重新编号/收尾镜设置永远不会补上。回滚只丢弃这次被拒
        # 写入自己遗留的挂起状态，不影响调用方更早已经各自 commit 过的检查点。
        if conn.in_transaction:
            conn.rollback()
        errors.log_error(
            None,
            action="storyboard_stale_run_write_rejected",
            context={
                "episode_id": episode_id,
                "expected_screenplay_artifact_id": expected,
                "current_screenplay_artifact_id": current,
                "publish_fence": bool(row["screenplay_publish_fence"]),
            },
            message="被替代的分镜运行尝试写入，已拒绝",
        )
        raise RuntimeError("分镜写入被拒绝：上游剧本版本或发布栅栏已变化")

def _insert_storyboard_shot(
    conn,
    episode_id: str,
    screenplay: EpisodeScreenplay,
    shot: Shot,
    expected_screenplay_artifact_id: str | None = None,
) -> str:
    _assert_storyboard_write_authorized(conn, episode_id, expected_screenplay_artifact_id)
    shot_id = new_id("shot")
    shot_uid = new_id("shotuid")
    shot.shot_uid = shot_uid
    if screenplay.narrative_plan is None:
        shot.action_desc = normalize_action_desc(shot.action_desc)
    from app.renderability import HUMAN_DURATION_REVIEW_TAG
    shot.risk_tags = [
        tag for tag in (shot.risk_tags or [])
        if tag != HUMAN_DURATION_REVIEW_TAG
    ]
    conn.execute(
        "INSERT INTO shots(id, shot_uid, episode_id, script_id, shot_no, duration_s, shot_size, camera_move, scene_time, scene_setting, scene_name, characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, transition, continuity_from_prev, shot_contract_json, continuity_mode, observed_state_out, storyboard_artifact_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (shot_id, shot_uid, episode_id, screenplay.id, shot.shot_no, shot.duration_s, shot.shot_size, shot.camera_move,
         shot.scene_time, shot.scene_setting, shot.scene_name or None,
         json.dumps(shot.characters, ensure_ascii=False), shot.action_desc,
         shot.first_frame_desc, shot.last_frame_desc, shot.source_excerpt, shot.narration,
         json.dumps([d.model_dump() for d in shot.dialogues], ensure_ascii=False),
         shot.transition, int(shot.continuity_from_prev), _shot_contract_json(shot),
         shot.continuity_mode, shot.observed_state_out,
         getattr(shot, "evidence_artifact_id", None)))
    return shot_id

def _board_from_shot_rows(rows, episode_no: int) -> Storyboard:
    """Restore a Storyboard from persisted shot rows for confirmation and validation."""
    from app.continuity import apply_shot_contract
    shots = []
    for r in rows:
        shot = Shot(
            shot_no=r["shot_no"],
            shot_uid=(r["shot_uid"] if "shot_uid" in r.keys() else "") or "",
            duration_s=r["duration_s"], shot_size=r["shot_size"], camera_move=r["camera_move"],
            scene_time=(r["scene_time"] if "scene_time" in r.keys() else "") or "",
            scene_setting=r["scene_setting"], scene_name=(r["scene_name"] if "scene_name" in r.keys() else "") or "",
            characters=json.loads(r["characters"] or "[]"),
            action_desc=r["action_desc"], first_frame_desc=r["first_frame_desc"] or "", last_frame_desc=r["last_frame_desc"] or "",
            source_excerpt=r["source_excerpt"] or "",
            narration=r["narration"], dialogues=json.loads(r["dialogues"] or "[]"),
            transition=r["transition"] or "硬切", continuity_from_prev=bool(r["continuity_from_prev"]),
            continuity_mode=(r["continuity_mode"] if "continuity_mode" in r.keys() else "") or "",
            observed_state_out=(r["observed_state_out"] if "observed_state_out" in r.keys() else "") or "",
        )
        if "shot_contract_json" in r.keys() and r["shot_contract_json"]:
            apply_shot_contract(shot, r["shot_contract_json"])
        shots.append(shot)
    return Storyboard(episode_no=episode_no, shots=shots)
