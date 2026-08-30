"""单镜头编辑落地（唯一大函数，见模块内注释）。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 mutation_primitives。单个函数 419 行，是镜头编辑的唯一权威校验+落库
顺序，拆分会打散前置校验与写入的耦合，不做拆分（移动未拆分）。
"""
from __future__ import annotations

import json

from app.compiler import clip_duration_value
from app.db import get_conn
from app.domain.common import (
    _project_bible_or_placeholder,
    router,
)
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import (
    Evaluation,
    EvidenceArtifact,
)
from app.schemas import (
    Shot,
    Storyboard,
    StoryboardOutline,
    schema_errors,
)
from app.validators import normalize_action_desc
from fastapi import HTTPException

from .mutation_primitives import (
    _apply_contract_to_public_shot,
    _board_from_shot_rows,
    _narrative_semantic_edit_fields,
    _raise_narrative_semantic_mutation_required,
    _resolve_storyboard_mutation_screenplay,
    _shot_contract_json,
)


@router.put("/shots/{shot_id}")
async def edit_shot(shot_id: str, body: dict):
    from app.capabilities.dispatch import ui_route
    expected_version = body.get("expected_version")
    meta_keys = {
        "expected_version", "edit_session_token", "preview_token",
        "baseline_content_hash", "change_source", "source_binding",
    }
    patch = {k: v for k, v in body.items() if k not in meta_keys}
    routed = await ui_route(
        "shot.update",
        {
            "shot_id": shot_id, "patch": patch, "expected_version": expected_version,
            "edit_session_token": body.get("edit_session_token"),
            "preview_token": body.get("preview_token"),
            "baseline_content_hash": body.get("baseline_content_hash"),
            "change_source": body.get("change_source") or "standard_edit",
            "source_binding": body.get("source_binding"),
        },
    )
    if routed is not None:
        return routed
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    _resolve_storyboard_mutation_screenplay(conn, str(shot["episode_id"]))
    from app.storyboard_workspace import (
        persist_source_binding, require_edit_session,
        require_preview, validate_source_binding,
    )
    session = require_edit_session(body.get("edit_session_token"), shot_id)
    preview = require_preview(
        body.get("preview_token"), "shot_edit", shot["episode_id"], shot_id=shot_id,
    )
    if body.get("baseline_content_hash") != session["baseline_content_hash"]:
        raise HTTPException(409, "保存基线与进入编辑时不一致，请重新对比最新版")
    approved_changes = dict(preview.get("normalized_changes") or {})
    submitted_changes = dict(patch)
    source_binding = body.get("source_binding")
    normalized_source_binding = None
    if source_binding is not None:
        excerpt, normalized_source_binding = validate_source_binding(shot["episode_id"], source_binding)
        submitted_changes["source_excerpt"] = excerpt
    if submitted_changes != {k: v for k, v in approved_changes.items() if k != "source_binding"}:
        raise HTTPException(409, "保存内容与已批准的影响预览不一致，请重新预览")
    body = {
        **submitted_changes,
        "expected_version": expected_version,
        "edit_session_token": body.get("edit_session_token"),
        "preview_token": body.get("preview_token"),
        "baseline_content_hash": body.get("baseline_content_hash"),
        "change_source": body.get("change_source") or "standard_edit",
    }
    current_version = shot["storyboard_artifact_id"] or ""
    if expected_version is not None and str(expected_version) != str(current_version):
        raise HTTPException(
            409,
            f"镜头版本冲突：当前版本 {current_version or '空'}，请求基于 {expected_version}，请刷新后重试",
        )
    if not approved_changes:
        return {"ok": True, "unchanged": True, "artifact_id": current_version, "impact": {"stale_count": 0}}
    merged = dict(shot)
    merged["characters"] = json.loads(merged["characters"] or "[]")
    merged["dialogues"] = json.loads(merged["dialogues"] or "[]")
    merged["continuity_from_prev"] = bool(merged["continuity_from_prev"])
    _apply_contract_to_public_shot(merged)
    editable_keys = (
        "duration_s", "shot_size", "camera_move", "scene_time", "scene_name", "scene_setting", "characters",
        "action_desc", "first_frame_desc", "last_frame_desc", "source_excerpt", "narration",
        "dialogues", "transition", "continuity_from_prev",
        "story_event_id", "purpose", "spine_beat_ids", "key_line_ids", "information_ids",
        "new_information_ids", "reinforcement_info_ids", "spoken_contract_status",
        "state_in", "primary_action", "emotion_beat", "state_out", "observed_state_out",
        "continuity_mode", "characters_visible", "audio_cast", "audio_timeline",
        "required_text", "continuity_state_in", "continuity_state_out",
        "reference_roles", "do_not_repeat", "risk_tags",
        "prompt_contract_version", "legacy_unvalidated", "camera_angle",
        "spatial_anchor", "is_final", "context_requirement_ids",
        "resulting_change", "readability_focus", "camera_motivation",
        "repeat_of_shot_id", "repeat_gain",
    )
    for key in editable_keys:
        if key in body:
            merged[key] = body[key]
    # 时长 clamp 到产品侧合法区间；缺省/非法时回退默认时长。
    merged["duration_s"] = clip_duration_value(merged.get("duration_s"))
    if "duration_s" in submitted_changes:
        from app.renderability import (
            DURATION_REVIEW_RISK_TAG,
            HUMAN_DURATION_REVIEW_TAG,
            PREFERRED_SHOT_DURATION_S,
        )

        duration_tags = [
            tag for tag in (merged.get("risk_tags") or [])
            if tag not in {DURATION_REVIEW_RISK_TAG, HUMAN_DURATION_REVIEW_TAG}
        ]
        if int(merged["duration_s"]) > PREFERRED_SHOT_DURATION_S:
            duration_tags.append(HUMAN_DURATION_REVIEW_TAG)
        merged["risk_tags"] = duration_tags
    instance, errors = schema_errors(
        Shot,
        {key: merged[key] for key in Shot.model_fields if key in merged},
    )
    if errors:
        raise HTTPException(422, "；".join(errors))
    # 产品禁止旁白：保存时强制清空 narration，并从 timeline 剥离 narration 轨。
    instance.narration = ""
    if instance.audio_timeline:
        instance.audio_timeline = [item for item in instance.audio_timeline if item.type != "narration"]
    # VAL-422：人工编辑必须重新通过确定性业务校验；「人改过」≠ hard gate 通过。
    from app.continuity import (
        action_capacity_errors, speech_capacity_errors, spoken_contract_coherence_errors, shot_id_space_errors,
        state_chain_errors,
    )
    from app.spoken_contract import (
        RULE_SPOKEN_CAPACITY,
        synchronize_spoken_contract,
        spoken_text_of,
    )
    from app.validators import (
        normalize_offbible_characters,
        validate_storyboard_shot_covers_outline,
        validate_storyboard_preserves_key_content,
        key_line_delivery_errors,
    )
    episode_id = shot["episode_id"]
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    screenplay_context = _resolve_storyboard_mutation_screenplay(conn, episode_id)
    screenplay = screenplay_context.screenplay
    changed_fields = {key for key in submitted_changes if key != "source_binding"}
    narrative_authority = screenplay_context.narrative_authority_required
    if not narrative_authority:
        instance.action_desc = normalize_action_desc(instance.action_desc)
    if narrative_authority:
        semantic_changes = _narrative_semantic_edit_fields(changed_fields)
        if semantic_changes:
            _raise_narrative_semantic_mutation_required(
                operation="shot_edit",
                fields=semantic_changes,
            )
    # 人工保存与确认门共用同一角色合同：临时描述角色开口时，
    # 在派生 timeline 之前就补齐可见名单，避免台词被错降级为画外音，
    # 更避免“保存后存在、确认后消失”。
    project_bible = None
    if ep is not None:
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
        ).fetchone()
        project_bible = _project_bible_or_placeholder(project)
        character_changes = (
            []
            if narrative_authority
            else normalize_offbible_characters(
                Storyboard(episode_no=ep["episode_no"], shots=[instance]),
                project_bible,
            )
        )
        stripped = sorted({
            str(change.get("stripped") or "").strip()
            for change in character_changes
            if str(change.get("stripped") or "").strip()
        })
        if stripped:
            raise HTTPException(422, {
                "code": "storyboard_character_identity_unresolved",
                "message": "分镜不允许新增未解析的人物称谓，本次未保存、未删除台词",
                "characters": stripped,
                "action": "请先在剧本阶段完成未来 10 章身份消歧",
            })
        from app.validators import canonicalize_storyboard_scene
        if getattr(project_bible, "scenes", None) and not canonicalize_storyboard_scene(
            instance,
            project_bible,
            prefer_explicit=bool({"scene_time", "scene_name"} & set(submitted_changes)),
        ):
            raise HTTPException(
                422,
                "场景标签无法唯一匹配场景图；请输入更接近的场景名，或直接选择库内规范名",
            )
    sync = synchronize_spoken_contract(
        instance,
        changed_fields={k for k in ("dialogues", "audio_timeline") if k in changed_fields},
    )
    # 容量只走 speech_capacity_errors，避免与 sync 内 capacity_issue 重复报告。
    business_errors: list[str] = [
        issue.message for issue in sync.issues
        if issue.severity == "blocker" and issue.rule_id != RULE_SPOKEN_CAPACITY
    ]
    business_errors.extend(action_capacity_errors(
        instance,
        narrative_authority=narrative_authority,
        narrative_plan=(screenplay.narrative_plan if screenplay is not None else None),
    ))
    business_errors.extend(speech_capacity_errors(instance))
    business_errors.extend(spoken_contract_coherence_errors(instance))
    business_errors.extend(shot_id_space_errors(instance))
    business_errors.extend(key_line_delivery_errors(instance, screenplay))

    outline = None
    if ep is not None and ep["storyboard_outline_json"]:
        try:
            outline = StoryboardOutline.model_validate_json(ep["storyboard_outline_json"])
        except Exception:  # noqa: BLE001
            outline = None
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    board = _board_from_shot_rows(rows, ep["episode_no"] if ep else 1)
    # 用编辑后的镜头替换同号位，再跑相邻状态链 / 大纲 covers / 收束整集校验。
    replaced = False
    for idx, existing in enumerate(board.shots):
        if existing.shot_no == instance.shot_no:
            board.shots[idx] = instance
            replaced = True
            break
    if not replaced:
        board.shots.append(instance)
        board.shots.sort(key=lambda s: s.shot_no)

    if not narrative_authority:
        # 手工编辑也必须服从与自动分镜相同的媒体输入合同。尤其同场景镜头
        # 的起点只投影上一镜结束状态，不能通过直接请求重新引入一张独立首帧图。
        from app.continuity import normalize_board_continuity

        normalize_board_continuity(board)
        instance = next(
            item for item in board.shots
            if item.shot_no == instance.shot_no
        )

    if not narrative_authority and outline and outline.shots:
        brief = next((s for s in outline.shots if s.shot_no == instance.shot_no), None)
        if brief is not None and (brief.covers or "").strip():
            prior_text = "".join(
                (s.action_desc or "") + spoken_text_of(s)
                for s in board.shots if s.shot_no < instance.shot_no
            )
            later = "；".join(
                (s.covers or "") for s in outline.shots if s.shot_no > instance.shot_no
            )
            business_errors.extend(validate_storyboard_shot_covers_outline(
                instance, brief.covers, instance.shot_no,
                prior_text=prior_text, later_planned_covers=later,
                narrative_authority=False,
            ))

    # 相邻窗口状态链：只保留「本镜」相关诊断，避免旧邻镜缺字段误伤本次保存。
    neighbor_nos = {instance.shot_no - 1, instance.shot_no, instance.shot_no + 1}
    neighbor_board = Storyboard(
        episode_no=board.episode_no,
        shots=[s for s in board.shots if s.shot_no in neighbor_nos],
    )
    if neighbor_board.shots and (
        (instance.state_in or "").strip() or (instance.state_out or "").strip()
    ):
        tag = f"shot_no={instance.shot_no}"
        business_errors.extend(
            err
            for err in state_chain_errors(
                neighbor_board,
                narrative_authority=narrative_authority,
            )
            if tag in err
        )

    is_final_edit = bool(instance.is_final) or (
        outline is not None and outline.shots
        and instance.shot_no >= len(outline.shots)
    )
    if is_final_edit and screenplay is not None:
        business_errors.extend(validate_storyboard_preserves_key_content(board, screenplay))
    if narrative_authority and screenplay is not None:
        from app.narrative import validate_storyboard_narrative

        business_errors.extend(validate_storyboard_narrative(
            board,
            screenplay,
            outline=outline,
            complete=True,
            expected_scope_id=episode_id,
        ))

    # 去重：同一文案只报一次
    deduped: list[str] = []
    seen_err: set[str] = set()
    for msg in business_errors:
        if msg in seen_err:
            continue
        seen_err.add(msg)
        deduped.append(msg)
    if narrative_authority and deduped:
        raise HTTPException(422, {
            "code": "narrative_candidate_rejected",
            "message": "编辑候选未通过整集叙事不变量，本次未保存",
            "errors": deduped[:20],
        })
    # 正式镜头、证据、下游失效索引和编辑会话必须在同一事务收口。
    previous_artifact_id = shot["storyboard_artifact_id"]
    contract_version = get_contract("storyboard").version
    from app.artifacts import (
        flush_media_cleanup_outbox,
        stage_shot_artifact_cleanup,
    )

    conn.execute("BEGIN IMMEDIATE")
    cleanup_outbox_id = None
    try:
        session = require_edit_session(body.get("edit_session_token"), shot_id)
        require_preview(
            body.get("preview_token"),
            "shot_edit",
            episode_id,
            shot_id=shot_id,
            consume=True,
        )
        if body.get("baseline_content_hash") != session["baseline_content_hash"]:
            raise HTTPException(409, "保存基线已变化，请重新对比最新版")
        conn.execute(
            "UPDATE shots SET duration_s=?, shot_size=?, camera_move=?, scene_time=?, scene_setting=?, scene_name=?, characters=?, action_desc=?, first_frame_desc=?, last_frame_desc=?, source_excerpt=?, narration=?, dialogues=?, transition=?, continuity_from_prev=?, shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE id=?",
            (instance.duration_s, instance.shot_size, instance.camera_move, instance.scene_time,
             instance.scene_setting, instance.scene_name or None,
             json.dumps(instance.characters, ensure_ascii=False), instance.action_desc, instance.first_frame_desc, instance.last_frame_desc,
             instance.source_excerpt, instance.narration,
             json.dumps([d.model_dump() for d in instance.dialogues], ensure_ascii=False),
             instance.transition, int(instance.continuity_from_prev), _shot_contract_json(instance),
             instance.continuity_mode, instance.observed_state_out, shot_id))
        if normalized_source_binding is not None:
            persist_source_binding(
                shot_id,
                normalized_source_binding,
                conn=conn,
                commit=False,
            )
        manual_artifact = evidence_repository.create_and_commit_artifact_in_transaction(
            conn,
            EvidenceArtifact(
                type="storyboard_shot",
                scope_type="storyboard_checkpoint",
                scope_id=f"{episode_id}:{shot['shot_no']}",
                status="validated",
                trust_level="T2",
                content=instance.model_dump(mode="json"),
                parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
                contract_version=contract_version,
            ),
            [
                Evaluation(
                    evaluator_type="human",
                    evaluator_name="storyboard_editor",
                    evaluator_version="1.0.0",
                    status="passed",
                    hard_gate_passed=False,
                    score=100,
                    evidence={"decision": "authored_or_reviewed", "shot_id": shot_id},
                ),
                Evaluation(
                    evaluator_type="deterministic",
                    evaluator_name="storyboard_shot_business_gate",
                    evaluator_version=contract_version,
                    status="warning" if deduped else "passed",
                    hard_gate_passed=not bool(deduped),
                    evaluation_role="score_only",
                    runtime_blocking=False,
                    retry_eligible=False,
                    score=0 if deduped else 100,
                    evidence={
                        "shot_id": shot_id,
                        "spoken_contract_status": instance.spoken_contract_status,
                        "gate_retry_exhausted": bool(deduped),
                        "warnings": deduped[:12],
                    },
                ),
            ],
        )
        conn.execute(
            "UPDATE shots SET storyboard_artifact_id=? WHERE id=?",
            (manual_artifact["id"], shot_id),
        )
        invalidated = stage_shot_artifact_cleanup(conn, shot_id)
        cleanup_outbox_id = invalidated.get("outbox_id")
        conn.execute(
            "UPDATE episodes SET status='scripted', storyboard_warning=NULL WHERE id=?",
            (episode_id,),
        )
        conn.execute(
            "UPDATE storyboard_edit_sessions SET status='saved' WHERE token=?",
            (body.get("edit_session_token"),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if cleanup_outbox_id:
        flush_media_cleanup_outbox(str(cleanup_outbox_id))
    try:
        from app.observability.metrics import inc
        inc(
            "storyboard_save_result_total", episode_id=episode_id, shot_id=shot_id,
            noop=False, validation="warning" if deduped else "passed",
            source=body.get("change_source") or "standard_edit",
        )
    except Exception:  # noqa: BLE001
        pass
    impact = evidence_repository.get_lineage(previous_artifact_id or manual_artifact["id"])
    return {
        "ok": True,
        "invalidated": invalidated,
        "artifact_id": manual_artifact["id"],
        "qa_warnings": deduped,
        "gate_retry_exhausted": bool(deduped),
        "impact": {
            "stale_descendant_ids": [
                item["id"] for item in impact["descendants"] if item["status"] == "stale"
            ],
            "requires_reconfirm": True,
            "paid_media_invalidated": bool(invalidated),
        },
    }
