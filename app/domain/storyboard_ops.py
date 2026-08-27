from __future__ import annotations

import asyncio
import re
import sqlite3

from app.harness.types import Issue, IssueSeverity

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

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


def _sync_storyboard_shot_timing(
    conn,
    episode_id: str,
    board: Storyboard,
    expected_screenplay_artifact_id: str | None = None,
) -> None:
    _assert_storyboard_write_authorized(conn, episode_id, expected_screenplay_artifact_id)
    for shot in board.shots:
        conn.execute(
            "UPDATE shots SET duration_s=?, transition=?, continuity_from_prev=?, last_frame_desc=?, shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE episode_id=? AND shot_no=?",
            (shot.duration_s, shot.transition, int(shot.continuity_from_prev), shot.last_frame_desc,
             _shot_contract_json(shot), shot.continuity_mode, shot.observed_state_out,
             episode_id, shot.shot_no),
        )


def _sync_storyboard_scene_bindings(conn, episode_id: str, board: Storyboard) -> int:
    """回写分离后的时间、规范场景图身份及兼容显示文案。

    模糊/旧式输入只允许出现在校验入口；一旦命中，正式投影必须固化为
    ``scene_name`` 规范名，以保证后续选图一一对应。
    """
    rows = conn.execute(
        "SELECT id,shot_no,scene_time,scene_setting,scene_name FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    by_shot_no = {int(row["shot_no"]): row for row in rows}
    changed = 0
    for shot in board.shots:
        row = by_shot_no.get(int(shot.shot_no))
        if row is None:
            continue
        current = (
            str(row["scene_time"] or "").strip(),
            str(row["scene_setting"] or "").strip(),
            str(row["scene_name"] or "").strip(),
        )
        resolved_time = str(shot.scene_time or "").strip()
        resolved = str(shot.scene_name or "").strip()
        resolved_setting = str(shot.scene_setting or "").strip()
        if current == (resolved_time, resolved_setting, resolved):
            continue
        conn.execute(
            "UPDATE shots SET scene_time=?,scene_setting=?,scene_name=? WHERE id=?",
            (resolved_time, resolved_setting, resolved or None, row["id"]),
        )
        changed += 1
    return changed


def _reconcile_storyboard_scene_projection(conn, episode_id: str, bible: Bible) -> dict[str, int]:
    """幂等对账正式镜头与分镜大纲的场景投影。

    场景归一是确定性派生过程，不应依赖「整集所有门禁通过」才落库。
    否则只要台词、连续性等任一无关问题存在，已经判定正确的 scene_name
    仍会长期停留在内存副本，导致页面、选图和暂停检查点持续读到旧绑定。
    """
    from types import SimpleNamespace
    from app.validators import canonicalize_storyboard_scene

    scenes = getattr(bible, "scenes", None) or []
    if not scenes:
        return {"shots": 0, "outline_shots": 0}

    outline_changes = 0
    outline_by_no: dict[int, StoryboardOutlineShot] = {}
    episode = conn.execute(
        "SELECT storyboard_outline_json FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    raw_outline = episode["storyboard_outline_json"] if episode else None
    if raw_outline:
        try:
            outline = StoryboardOutline.model_validate_json(raw_outline)
        except (TypeError, ValueError):
            outline = None
        if outline is not None:
            for brief in outline.shots:
                before = (brief.scene_time, brief.scene_setting, brief.scene_name)
                if not canonicalize_storyboard_scene(brief, bible):
                    continue
                outline_by_no[int(brief.shot_no)] = brief
                if (brief.scene_time, brief.scene_setting, brief.scene_name) != before:
                    outline_changes += 1
            if outline_changes:
                from app.storyboard_authority import (
                    persist_storyboard_outline_projection,
                )

                persist_storyboard_outline_projection(
                    episode_id,
                    outline,
                    conn=conn,
                )

    shot_changes = 0
    rows = conn.execute(
        "SELECT id,shot_no,scene_time,scene_setting,scene_name FROM shots WHERE episode_id=?",
        (episode_id,),
    ).fetchall()
    for row in rows:
        brief = outline_by_no.get(int(row["shot_no"]))
        target = SimpleNamespace(
            scene_time=str(
                (brief.scene_time if brief is not None else row["scene_time"])
                or ""
            ),
            scene_setting=str(
                (brief.scene_setting if brief is not None else row["scene_setting"])
                or ""
            ),
            scene_name=str(
                (brief.scene_name if brief is not None else row["scene_name"])
                or ""
            ),
        )
        before = (
            str(row["scene_time"] or ""),
            str(row["scene_setting"] or ""),
            str(row["scene_name"] or ""),
        )
        if not canonicalize_storyboard_scene(target, bible):
            continue
        after = (target.scene_time, target.scene_setting, target.scene_name)
        if after == before:
            continue
        conn.execute(
            "UPDATE shots SET scene_time=?,scene_setting=?,scene_name=? WHERE id=?",
            (*after, row["id"]),
        )
        shot_changes += 1

    if shot_changes or outline_changes:
        conn.commit()
    return {"shots": shot_changes, "outline_shots": outline_changes}


def _storyboard_has_material(episode_id: str, ep: dict | None = None) -> bool:
    """Return whether the current episode projection still owns storyboard work."""
    episode = dict(ep) if ep is not None else dict(_episode_or_404(episode_id))
    shot_count = int(get_conn().execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
    ).fetchone()["c"])
    return bool(
        shot_count
        or episode.get("storyboard_outline_json")
        or episode.get("storyboard_artifact_id")
        or episode.get("working_storyboard_artifact_id")
        or episode.get("published_storyboard_artifact_id")
        or episode.get("storyboard_production_revision_id")
        or episode.get("storyboard_completion_certificate_id")
    )


def _storyboard_checkpoint_matches_screenplay(cp, ep: dict) -> bool:
    """Only checkpoints bound to the current screenplay and Bible may resume."""
    if cp is None:
        return False
    bound = str(cp.input_versions.get("screenplay_artifact_id") or "")
    current = str(ep.get("screenplay_artifact_id") or "")
    bound_bible = str(cp.input_versions.get("bible_artifact_id") or "")
    current_bible = str(ep.get("bible_artifact_id") or "")
    if not current_bible and ep.get("project_id"):
        project = get_conn().execute(
            "SELECT bible_artifact_id FROM projects WHERE id=?",
            (ep["project_id"],),
        ).fetchone()
        current_bible = str(project["bible_artifact_id"] or "") if project else ""
    return bool(
        bound
        and current
        and bound == current
        and bound_bible
        and current_bible
        and bound_bible == current_bible
    )


def _storyboard_has_persisted_work(episode_id: str, ep: dict | None = None) -> bool:
    """Whether this episode already has storyboard work that must be resumed or cleared.

    Starting a task and continuing a task are deliberately separate user actions.  A
    fresh start must never silently replace shots or adopt a checkpoint left by an
    earlier run.
    """
    from app.storyboard_supervisor import load_latest_checkpoint

    episode = ep or dict(_episode_or_404(episode_id))
    if _storyboard_has_material(episode_id, episode):
        return True
    checkpoint = load_latest_checkpoint(episode_id)
    # Publishing a new screenplay clears the current storyboard projection but
    # intentionally keeps historical checkpoint artifacts for audit.  Such a
    # checkpoint is not resumable work for the new screenplay.
    return _storyboard_checkpoint_matches_screenplay(checkpoint, episode)


def _storyboard_resume_decision(episode_id: str, ep: dict | None = None) -> dict:
    """Project the one authoritative decision for resuming a storyboard.

    ``is_final`` only says that the current tail closes the episode.  It does
    not say that the whole-board confirmation gates passed.  A completed tail
    with unresolved hard gates must reopen the Supervisor's non-destructive
    repair loop, while a genuinely confirmable board must still reject blind
    append attempts.
    """
    episode = dict(ep) if ep is not None else dict(_episode_or_404(episode_id))
    published_release_bound = bool(
        episode.get("published_storyboard_artifact_id")
        and episode.get("storyboard_completion_certificate_id")
        and episode.get("storyboard_production_revision_id")
    )
    if (
        published_release_bound
        and episode.get("status") in {"confirmed", "generating", "done", "mixed"}
    ):
        return {
            "allowed": False,
            "resume_mode": None,
            "blocking_reason": (
                "当前分镜已有已确认发布基线，不能原地续跑；"
                "修订必须在隔离候选中完成并重新发布"
            ),
            "storyboard_status": None,
        }
    row = get_conn().execute(
        "SELECT shot_no,shot_contract_json FROM shots WHERE episode_id=? "
        "ORDER BY shot_no DESC LIMIT 1",
        (episode_id,),
    ).fetchone()
    tail_is_final = False
    if row and row["shot_contract_json"]:
        try:
            tail_is_final = bool(json.loads(row["shot_contract_json"] or "{}").get("is_final"))
        except (TypeError, ValueError, json.JSONDecodeError):
            tail_is_final = False

    # An unfinished tail is ordinary checkpoint continuation.
    if not tail_is_final:
        return {
            "allowed": True,
            "resume_mode": "continue_generation",
            "blocking_reason": None,
            "storyboard_status": None,
        }

    # Use the same atomic projection that drives the primary UI action.  This
    # prevents preflight from promising a repair that the POST endpoint later
    # mistakes for an attempt to append after an is_final tail.
    from app import api as api_facade

    status = api_facade.episode_detail(episode_id, view="board")["storyboard_status"]
    allowed = status.get("recommended_action") == "resume_storyboard"
    if allowed:
        return {
            "allowed": True,
            "resume_mode": status.get("resume_mode") or "continue_generation",
            "blocking_reason": None,
            "storyboard_status": status,
        }
    if (
        status.get("recommended_action") == "confirm_storyboard"
        and not (
            episode.get("storyboard_artifact_id")
            and episode.get("storyboard_completion_certificate_id")
        )
    ):
        return {
            "allowed": True,
            "resume_mode": "finalize_evidence",
            "blocking_reason": None,
            "storyboard_status": status,
        }

    # Preserve recovery from a stale projection whose durable Run has already
    # ended.  A live task is handled by the caller's deduplication guard; a
    # confirmed/done episode must never enter this compatibility path.
    if (
        episode.get("status") in {"scripting", "script_failed"}
        and not _storyboard_generation_is_live(episode)
    ):
        return {
            "allowed": True,
            "resume_mode": "continue_generation",
            "blocking_reason": None,
            "storyboard_status": status,
        }

    if status.get("recommended_action") == "confirm_storyboard":
        reason = "当前分镜已完整收束且确认门禁已通过，请直接确认分镜，无需继续生成"
    elif status.get("recommended_action") == "go_review_wall":
        reason = "当前分镜已经确认，不能再续跑；如需调整请先创建新的制作修订"
    else:
        reason = status.get("write_block_reason") or "当前收尾镜后没有可恢复的生成或修复任务"
    return {
        "allowed": False,
        "resume_mode": None,
        "blocking_reason": reason,
        "storyboard_status": status,
    }


def _storyboard_start_preflight_payload(episode_id: str) -> dict:
    from app.storyboard_supervisor import load_latest_checkpoint
    from app.storyboard_workspace import episode_fingerprint

    ep = _episode_or_404(episode_id)
    if not _screenplay_ready(ep):
        rebuild_block = _screenplay_rebuild_block(get_conn(), ep)
        if rebuild_block is not None:
            raise HTTPException(409, rebuild_block)
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    conn = get_conn()
    shots = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
    ).fetchone()["c"])
    action = "resume" if _storyboard_has_persisted_work(episode_id, dict(ep)) else "create"
    cp = load_latest_checkpoint(episode_id) if action == "resume" else None
    resume_decision = (
        _storyboard_resume_decision(episode_id, dict(ep))
        if action == "resume"
        else {
            "allowed": True,
            "resume_mode": "create",
            "blocking_reason": None,
            "storyboard_status": None,
        }
    )
    current_status = resume_decision.get("storyboard_status") or {}
    current_gate_issues = list(current_status.get("hard_gate_issues") or [])
    planned = int(cp.expected_total or 0) if cp else 0
    if not planned and ep["storyboard_outline_json"]:
        try:
            planned = len(json.loads(ep["storyboard_outline_json"] or "{}").get("shots") or [])
        except (TypeError, ValueError, json.JSONDecodeError):
            planned = 0
    kept = (
        min(shots, max(0, int(cp.validated_prefix_end or 0)))
        if action == "resume" and cp else (shots if action == "resume" else 0)
    )
    resume_from = kept + 1 if action == "resume" else 1
    remaining = max(0, planned - kept) if planned else None
    strategies_exhausted = bool(
        cp and cp.outcome in {
            "REPAIR_FAILED_STRATEGIES_EXHAUSTED",
            "SUCCEEDED_GATE_RETRY_EXHAUSTED_FALLBACK",
            "WAITING_RETRY_GATE_REPAIR_EXHAUSTED",
        }
    )
    latest_run = conn.execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    provider_stats = {"external_calls": 0, "cache_reuses": 0}
    if latest_run:
        row = conn.execute(
            """SELECT
                   SUM(CASE WHEN kind='chat' AND status IN ('OK','SUCCESS','SUCCEEDED') THEN 1 ELSE 0 END) AS external_calls,
                   SUM(CASE WHEN kind='provider_cache_hit' AND status='REUSED' THEN 1 ELSE 0 END) AS cache_reuses
               FROM provider_calls WHERE run_id=?""",
            (latest_run["id"],),
        ).fetchone()
        provider_stats = {
            "external_calls": int(row["external_calls"] or 0) if row else 0,
            "cache_reuses": int(row["cache_reuses"] or 0) if row else 0,
        }
    return {
        "episode_id": episode_id,
        "action": action,
        "resume_mode": resume_decision["resume_mode"],
        "screenplay_artifact_id": ep["screenplay_artifact_id"],
        "storyboard_artifact_id": ep["storyboard_artifact_id"],
        "checkpoint": {
            "available": bool(cp),
            "phase": cp.phase if cp else None,
            "resume_from_shot": resume_from,
        },
        "kept_validated_shots": kept,
        "planned_shots": planned or None,
        "remaining_shots": remaining,
        "can_start": bool(resume_decision["allowed"]),
        "blocking_reason": resume_decision["blocking_reason"],
        "current_gate_issue_count": len(current_gate_issues),
        "current_gate_issues": current_gate_issues[:12],
        "gate_retry_exhausted": strategies_exhausted,
        "warning": (
            "上一轮修复预算已用尽；继续后将开启新的有界修复轮次，现有分镜在候选通过前保持不变"
            if strategies_exhausted else None
        ),
        "repair": {
            "lifetime_repair_count": int(cp.repair_epoch or 0) if cp else 0,
            "activation_no": int(cp.activation_no or 0) if cp else 0,
            "activation_attempt_count": int(cp.activation_attempt_count or 0) if cp else 0,
            "max_attempts_per_activation": 6,
            "candidate_preserves_official_shots": True,
            "last_issue_messages": (
                current_gate_issues[:12]
                or (list((cp.last_repair or {}).get("issue_messages") or [])[:12] if cp else [])
            ),
            **provider_stats,
        },
        "impact": (
            "保留现有镜头，重新执行整集门禁并仅在候选通过后替换问题镜"
            if resume_decision["resume_mode"] == "repair_existing"
            else "保留全部已通过镜头，仅续做冷观众审读和发布证据签发"
            if resume_decision["resume_mode"] == "finalize_evidence"
            else "保留已通过逐镜校验的镜头，并从下一镜继续"
            if action == "resume"
            else "从空白开始生成本集分镜"
        ),
        "estimated_wait_minutes": [max(1, (remaining or planned or 1)), max(2, (remaining or planned or 1) * 3)],
        "estimated_cost_cny": None,
        "estimate_note": "文本生成费用按实际调用结算；不会自动提交付费视频生成",
        "baseline_fingerprint": episode_fingerprint(episode_id),
    }


@router.post("/episodes/{episode_id}/storyboard/preflight")
def storyboard_start_preflight(episode_id: str, body: dict | None = Body(None)):
    from app.storyboard_workspace import create_preview

    _as_body_dict(body)
    payload = _storyboard_start_preflight_payload(episode_id)
    return create_preview(f"start:{payload['action']}", episode_id, payload)


def _persist_storyboard_character_policy_repairs(
    conn, episode_id: str, board: Storyboard, changes: list[dict]
) -> list[str]:
    """Persist deterministic repairs as derived T1 candidates, preserving lineage.

    The character-policy evaluation only proves this normalization, not every storyboard
    gate, so the derived artifact must not be committed as T2 on its own.
    """
    material = [change for change in changes if change.get("mutated")]
    if not material:
        return []
    contract_version = get_contract("storyboard").version
    artifact_ids: list[str] = []
    by_shot = {shot.shot_no: shot for shot in board.shots}
    for shot_no in dict.fromkeys(int(change["shot_no"]) for change in material):
        row = conn.execute(
            "SELECT id, storyboard_artifact_id FROM shots WHERE episode_id=? AND shot_no=?",
            (episode_id, shot_no),
        ).fetchone()
        shot = by_shot.get(shot_no)
        if row is None or shot is None:
            continue
        shot_changes = [change for change in material if int(change["shot_no"]) == shot_no]
        previous_artifact_id = row["storyboard_artifact_id"]
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_shot",
            scope_type="storyboard_checkpoint",
            scope_id=f"{episode_id}:{shot_no}",
            status="candidate",
            trust_level="T1",
            content=shot.model_dump(mode="json"),
            parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
            contract_version=contract_version,
        ))
        evidence_repository.create_evaluation(
            artifact["id"],
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="storyboard_character_policy",
                evaluator_version=contract_version,
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={
                    "policy": "functional_extra_v1",
                    "scope": "character_policy_only",
                    "changes": shot_changes,
                },
            ),
        )
        if previous_artifact_id:
            evidence_repository.invalidate_descendants(
                previous_artifact_id,
                f"镜头角色合同已由 {artifact['id']} 修订",
                exclude_ids={str(artifact["id"])},
            )
        has_runtime_derivatives = conn.execute(
            """SELECT EXISTS(SELECT 1 FROM shot_versions WHERE shot_id=?)
                      OR EXISTS(SELECT 1 FROM shot_scenes WHERE shot_id=?) AS present""",
            (row["id"], row["id"]),
        ).fetchone()["present"]
        if has_runtime_derivatives:
            worker.clear_shot_artifacts(row["id"])
        conn.execute(
            """UPDATE shots SET characters=?, action_desc=?, first_frame_desc=?,
               last_frame_desc=?, narration=?, dialogues=?, shot_contract_json=?,
               continuity_mode=?, observed_state_out=?, storyboard_artifact_id=? WHERE id=?""",
            (
                json.dumps(shot.characters, ensure_ascii=False),
                shot.action_desc,
                shot.first_frame_desc,
                shot.last_frame_desc,
                shot.narration,
                json.dumps([dialogue.model_dump() for dialogue in shot.dialogues], ensure_ascii=False),
                _shot_contract_json(shot),
                shot.continuity_mode,
                shot.observed_state_out,
                artifact["id"],
                row["id"],
            ),
        )
        artifact_ids.append(str(artifact["id"]))
        log_provider_call(
            "storyboard_character_policy",
            config.MODEL_TEXT,
            "CHARACTER_POLICY_REPAIRED",
            None,
            0,
            meta={
                "episode_id": episode_id,
                "shot_no": shot_no,
                "contract_version": contract_version,
                "artifact_id": artifact["id"],
                "changes": shot_changes,
            },
        )
    conn.commit()
    return artifact_ids


def _ensure_current_storyboard_shot_artifacts(
    conn,
    episode_id: str,
    board: Storyboard,
    *,
    commit: bool = True,
):
    """Bind every current shot to immutable evidence for its current number and content."""
    rows = conn.execute(
        "SELECT id,shot_no,source_excerpt,storyboard_artifact_id FROM shots "
        "WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if len(rows) != len(board.shots):
        raise RuntimeError(
            f"分镜证据对账失败：投影 {len(rows)} 镜，待发布内容 {len(board.shots)} 镜"
        )
    contract_version = get_contract("storyboard").version
    for row, shot in zip(rows, board.shots):
        if int(row["shot_no"]) != int(shot.shot_no):
            raise RuntimeError("分镜证据对账失败：镜头顺序与待发布内容不一致")
        content = shot.model_dump(mode="json")
        current_id = row["storyboard_artifact_id"]
        if _storyboard_shot_artifact_matches(
            conn, episode_id, shot, current_id,
        ):
            continue

        evaluation = Evaluation(
            evaluator_type="deterministic",
            evaluator_name="storyboard_projection_rebind",
            evaluator_version=contract_version,
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={
                "episode_id": episode_id,
                "shot_no": int(shot.shot_no),
                "previous_artifact_id": current_id,
                "reason": "current shot projection and immutable evidence were realigned",
            },
        )
        artifact_input = EvidenceArtifact(
            type="storyboard_shot",
            scope_type="storyboard_checkpoint",
            scope_id=f"{episode_id}:{shot.shot_no}",
            status="candidate",
            trust_level="T1",
            content=content,
            parent_artifact_ids=[str(current_id)] if current_id else [],
            contract_version=contract_version,
        )
        if commit:
            artifact = evidence_repository.create_artifact(artifact_input)
            artifact = evidence_repository.commit_artifact(
                None, artifact["id"], [evaluation],
            )
        else:
            artifact = evidence_repository.create_and_commit_artifact_in_transaction(
                conn,
                artifact_input,
                [evaluation],
            )
        conn.execute(
            "UPDATE shots SET storyboard_artifact_id=? WHERE id=?",
            (artifact["id"], row["id"]),
        )
        if commit:
            conn.commit()

    return conn.execute(
        "SELECT id,shot_no,source_excerpt,storyboard_artifact_id FROM shots "
        "WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()


def _storyboard_shot_artifact_matches(
    conn,
    episode_id: str,
    shot: Shot,
    artifact_id: str | None,
) -> bool:
    if not artifact_id:
        return False
    current = conn.execute(
        """SELECT type,scope_type,scope_id,status,content_hash
             FROM artifacts WHERE id=?""",
        (artifact_id,),
    ).fetchone()
    return bool(
        current
        and current["type"] == "storyboard_shot"
        and current["scope_type"] == "storyboard_checkpoint"
        and current["scope_id"] == f"{episode_id}:{shot.shot_no}"
        and current["status"] == "approved"
        and current["content_hash"] == evidence_repository.content_hash(
            shot.model_dump(mode="json")
        )
    )


def _storyboard_shot_evidence_requires_rebind(
    conn,
    episode_id: str,
    board: Storyboard,
) -> bool:
    rows = conn.execute(
        "SELECT shot_no,storyboard_artifact_id FROM shots "
        "WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if len(rows) != len(board.shots):
        return True
    return any(
        int(row["shot_no"]) != int(shot.shot_no)
        or not _storyboard_shot_artifact_matches(
            conn, episode_id, shot, row["storyboard_artifact_id"],
        )
        for row, shot in zip(rows, board.shots)
    )


def _storyboard_publication_evidence_state(
    episode: dict,
    board: Storyboard,
) -> tuple[bool, bool]:
    """Return ``(current, refinalize_only)`` for the bound release evidence.

    A calibration-authority update can stale an otherwise byte-identical
    Storyboard Artifact.  That is an evidence-lineage change, not a request to
    repair or regenerate shots.  Only exact authority-projection equality may
    take this refinalization path; real projection drift remains a hard gate.
    """
    artifact_id = str(episode.get("storyboard_artifact_id") or "")
    certificate_id = str(
        episode.get("storyboard_completion_certificate_id") or ""
    )
    revision_id = str(episode.get("storyboard_production_revision_id") or "")
    if not artifact_id or not certificate_id or not revision_id:
        return False, False
    artifact = evidence_repository.get_artifact(artifact_id)
    if (
        artifact is None
        or artifact.get("scope_type") != "episode"
        or artifact.get("scope_id") != episode.get("id")
    ):
        # Preserve the evaluator's typed hard-gate explanation for malformed
        # legacy fixtures and genuinely missing evidence.
        return True, False
    try:
        from app.narrative import storyboard_authority_projection

        projection_matches = storyboard_authority_projection(
            artifact.get("content") or {}
        ) == storyboard_authority_projection(board.model_dump(mode="json"))
    except Exception:  # noqa: BLE001 - malformed authority remains a hard gate
        return True, False
    if not projection_matches:
        return True, False
    try:
        # 叙事权威凭证校验（verify_current_storyboard_completion_authority）只
        # 对「当前仍要求叙事权威」的分集有意义——它自己会在
        # narrative_authority_required=False 时主动抛错（"当前剧集不使用叙事
        # 权威凭证"），这不是证据变质，是分类判据本身已经变了（典型场景：
        # 该集分镜/剧本已迁移到 prep_pack 6.0.0+ 合同，contract 设计上就不产出
        # narrative_plan，见 108e2c1 对 resolve_downstream_screenplay 的说明）。
        # 上面的 projection_matches 已经证明正文投影逐字一致；如果这里不预先
        # 判断分类，就会把"这项校验天然不适用"误判成"证据异常，禁止原地
        # 续跑"——一个内容完全没问题的已确认分集会被卡在一句用户既看不懂、
        # 也无处可核实的报错前，真实回归 ep_3d523ff4d0a4（EP1）复现。
        from app.production.screenplay_authority import (
            resolve_downstream_screenplay,
        )

        screenplay_context = resolve_downstream_screenplay(
            str(episode.get("id") or ""),
        )
        if not screenplay_context.narrative_authority_required:
            return True, False
        from app.production.certificate import (
            verify_current_storyboard_completion_authority,
        )

        verify_current_storyboard_completion_authority(
            episode=episode,
            current_storyboard_content=board.model_dump(mode="json"),
        )
    except Exception:  # noqa: BLE001 - exact content may safely reissue lineage
        return False, True
    return True, False


def _finalize_storyboard_evidence(
    episode_id: str,
    board: Storyboard,
) -> str:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    planned_total = 0
    try:
        planned_total = len(
            json.loads(ep["storyboard_outline_json"] or "{}").get("shots") or []
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        planned_total = 0
    findings: list[str] = []
    if not board.shots:
        raise RuntimeError("没有任何分镜产物可发布")
    if planned_total and len(board.shots) != planned_total:
        raise RuntimeError(f"分镜数量与计划不同：已完成 {len(board.shots)}/{planned_total} 镜")
    if not board.shots[-1].is_final:
        raise RuntimeError("最终镜未标记收束，禁止发布未结束的分镜")
    screenplay = None
    narrative_authority = False
    if ep["screenplay_json"]:
        from app.production.screenplay_authority import resolve_downstream_screenplay

        try:
            screenplay_context = resolve_downstream_screenplay(
                episode_id,
                conn=conn,
            )
        except Exception as exc:
            raise RuntimeError(f"分镜发布前剧本权威链无效：{exc}") from exc
        screenplay = screenplay_context.screenplay
        narrative_authority = screenplay_context.narrative_authority_required
    if narrative_authority:
        from app.narrative import (
            validate_storyboard_narrative,
        )

        narrative_errors = validate_storyboard_narrative(
            board,
            screenplay,
            outline=(
                StoryboardOutline.model_validate_json(ep["storyboard_outline_json"])
                if ep["storyboard_outline_json"]
                else None
            ),
            complete=True,
            expected_scope_id=episode_id,
        )
        if narrative_errors:
            raise RuntimeError(
                "分镜叙事硬门禁未通过：" + "；".join(narrative_errors[:8])
            )
        # 冷观众审读（narrative_review）与一次观看校准（narrative_calibration）
        # 已整体下线（用户拍板）：这里曾经的强制要求在删除前就已经是一个必死
        # 分支——本函数在活代码里的两个调用方（app.production.storyboard_pack.
        # run_storyboard_pack_generation、app.domain.video_ops._confirm_storyboard_
        # impl）都只按位置参数传 (episode_id, board)，从未提供过
        # narrative_review_report，所以 narrative_authority=True 时这里过去
        # 100% 抛 RuntimeError（分镜冷观众审读报告缺失，禁止发布）。删除这段
        # 不会让任何当前可达路径从"会拒绝"变成"会放行"——上面的
        # validate_storyboard_narrative 结构化叙事硬门禁（108e2c1 修的那道）
        # 原样保留，narrative_authority 的分类判据本身（screenplay_context.
        # narrative_authority_required）也原样保留，没有被本次改动放宽。
    if _sync_storyboard_scene_bindings(conn, episode_id, board):
        # 这是由当前门禁确定的派生外键修复，即使后续证据发布失败也应保留，
        # 避免下次重试继续读取已经证伪的历史场景绑定。
        conn.commit()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    shot_rows = _ensure_current_storyboard_shot_artifacts(
        conn, episode_id, board,
    )
    from app.storyboard_workspace import verify_or_bind_existing_excerpt
    for row in shot_rows:
        try:
            verify_or_bind_existing_excerpt(
                episode_id, row["id"], row["source_excerpt"] or "",
            )
        except Exception as exc:  # noqa: BLE001 - evidence finding is score-only at publish
            findings.append(f"镜头来源证据未绑定：{exc}")
    if narrative_authority and findings:
        raise RuntimeError(
            "分镜来源证据硬门禁未通过：" + "；".join(findings[:8])
        )
    shot_parent_ids: list[str] = []
    for row in shot_rows:
        artifact_id = row["storyboard_artifact_id"]
        if not artifact_id:
            continue
        shot_parent_ids.append(str(artifact_id))
        artifact_row = conn.execute(
            "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()
        if artifact_row:
            try:
                shot_parent_ids.extend(
                    str(item)
                    for item in json.loads(
                        artifact_row["parent_artifact_ids_json"] or "[]"
                    )
                    if item
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    parents = list(dict.fromkeys(
        str(artifact_id)
        for artifact_id in (
            project["bible_artifact_id"],
            ep["screenplay_artifact_id"],
            *shot_parent_ids,
        )
        if artifact_id
    ))
    contract_version = get_contract("storyboard").version
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T2",
        content=board.model_dump(mode="json"),
        parent_artifact_ids=parents,
        contract_version=contract_version,
    ))
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="storyboard_full_gate",
        evaluator_version=contract_version,
        status="warning" if findings else "passed",
        hard_gate_passed=not findings,
        evaluation_role="runtime_gate" if narrative_authority else "score_only",
        score_status="scored",
        runtime_blocking=narrative_authority,
        retry_eligible=False,
        score=max(0, 100 - 10 * len(findings)),
        issues=[Issue(
            code="STORYBOARD_GATE_EXHAUSTED_WARNING",
            severity=IssueSeverity.WARNING,
            subject=episode_id,
            message=message,
        ) for message in findings],
        evidence={
            "shot_count": len(board.shots),
            "duration_range_s": [config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S],
            "duration_decided_by": "model",
            "checkpoint_artifact_ids": parents,
            "gate_retry_exhausted": bool(findings),
            "findings": findings,
        },
    )
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [evaluation])
    from app.production.publish import publish_storyboard
    from app.production.revision import (
        bind_unpublished_revision_metadata,
        ensure_production_revision,
        mark_baseline_generated,
        update_working_artifact,
    )

    revision = ensure_production_revision(
        episode_id=episode_id,
        kind="storyboard",
        input_fingerprint=evidence_repository.content_hash(board.model_dump(mode="json")),
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
        resume=True,
    )
    if revision.working_artifact_id:
        update_working_artifact(revision.id, artifact["id"])
    else:
        revision = mark_baseline_generated(
            revision.id,
            baseline_artifact_id=artifact["id"],
            working_artifact_id=artifact["id"],
        )
    revision = bind_unpublished_revision_metadata(
        revision.id,
        input_fingerprint=(
            revision.input_fingerprint
            or evidence_repository.content_hash(board.model_dump(mode="json"))
        ),
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
    )
    eval_rows = conn.execute(
        "SELECT id FROM evaluations WHERE artifact_id=? ORDER BY created_at",
        (artifact["id"],),
    ).fetchall()
    publish_storyboard(
        episode_id=episode_id,
        revision_id=revision.id,
        artifact_id=artifact["id"],
        artifact_hash=artifact.get("content_hash") or evidence_repository.content_hash(
            board.model_dump(mode="json")
        ),
        evaluation_ids=[str(row["id"]) for row in eval_rows],
        shots_payload=[shot.model_dump(mode="json") for shot in board.shots],
        outline_json=ep["storyboard_outline_json"],
        input_fingerprint=revision.input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=revision.qa_profile_version or "storyboard-full-gate-2",
    )
    conn.commit()
    return str(artifact["id"])


def _soft_gap_continue_residual(residual: list[str]) -> bool:
    """是否仅为「暂不能收尾 / 继续补镜」软缺口（可清 is_final 续跑的那一类）。"""
    return (
        len(residual) == 1
        and "暂不能收尾" in residual[0]
        and "继续补镜" in residual[0]
    )


def _can_continue_for_soft_gap(
    *,
    is_final: bool,
    completed_count: int,
    planned_count: int,
    max_shots: int,
    residual: list[str],
) -> bool:
    """软缺口是否允许再开下一镜。

    有大纲时：只有计划里还剩未执行节拍才允许续跑（covers 语义拆分胀长后 planned_count 会变大）。
    计划已跑完、或已到技术硬上限时，禁止再发明大纲外幻觉镜。
    """
    if not is_final:
        return False
    if completed_count >= max_shots:
        return False
    # planned_count>0：大纲驱动；已达当前计划长度则禁止计划外补镜。
    if planned_count > 0 and completed_count >= planned_count:
        return False
    return _soft_gap_continue_residual(residual)


def _reconcile_storyboard_plan(conn, episode_id: str, episode_no: int,
                              outline: StoryboardOutline | None, completed: list[Shot],
                              persisted_total: int) -> tuple[int, int, str] | None:
    """让落库大纲成为唯一事实源，消除"规划十几镜却分镜24"的困惑。

    逐镜阶段若模型判断单镜超过合法时长上限（config.VIDEO_DURATION_MAX_S）仍演不完而继续拆镜、镜头数超出计划长度，
    内存 outline 会领先于
    落库的 storyboard_outline_json，导致前端 storyboard_planned_shots 显示陈旧的初始估算。

    本函数在每提交一镜后把当前计划追平实际镜头数并回写 DB，使规划数随逐镜细化实时自更新、
    单调不减且始终 ≥ 已通过镜头数。返回 (from_total, to_total, reason) 供事件记录；无变化返回 None。
    """
    if outline is None:
        return None
    appended = False
    # 模型拆镜超出计划、但未触发 covers 自动拆分：补占位节拍，让计划长度追平实际。
    if len(outline.shots) < len(completed):
        appended = True
        for shot in completed[len(outline.shots):]:
            raw = (shot.action_desc or shot.narration or "").strip()
            beat = "".join(raw.split())[:60] or "逐镜细化新增镜头"
            outline.shots.append(StoryboardOutlineShot(
                shot_no=len(outline.shots) + 1,
                scene_time=shot.scene_time or "",
                scene_name=shot.scene_name or "",
                scene_setting=shot.scene_setting or "",
                beat=beat,
                covers="",
                duration_s=int(shot.duration_s or 0) or None,
            ))
    to_total = len(outline.shots)
    if to_total == persisted_total:
        return None
    from app.storyboard_authority import persist_storyboard_outline_projection

    persist_storyboard_outline_projection(
        episode_id,
        outline,
        conn=conn,
    )
    reason = "shot_overflow" if appended else "covers_split"
    log_provider_call(
        "storyboard_plan_revised", config.MODEL_TEXT, "PLAN_REVISED", None, 0,
        meta={"episode_id": episode_id, "episode_no": episode_no, "stage": "分镜脚本",
              "from": persisted_total, "to": to_total,
              "actual_shots": len(completed), "reason": reason})
    return (persisted_total, to_total, reason)


async def _prepare_storyboard_assets_background(episode_id: str) -> None:
    """Fill portrait/scene assets without blocking screenplay-to-storyboard text work."""
    from app.observability.tracing import detached_trace

    # asyncio tasks copy ContextVars when spawned. Asset discovery is an
    # independent lifecycle, so do not attribute its later provider calls to
    # the storyboard text step that happened to schedule it.
    with detached_trace():
        await _prepare_storyboard_assets_background_detached(episode_id)


async def _prepare_storyboard_assets_background_detached(episode_id: str) -> None:
    conn = get_conn()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if not ep or not ep["screenplay_json"]:
        return
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
    ).fetchone()
    bible = _project_bible_or_placeholder(project)
    screenplay = _load_screenplay(ep)
    if screenplay is None:
        # _load_screenplay() deliberately returns None for an
        # episode_prep_pack projection (screenplay contract 6.0.0+) --
        # callers built for the legacy EpisodeScreenplay shape must not get
        # a silently-empty object (see its docstring in app.domain.common).
        # That guard must not become "skip asset prep for every prep_pack
        # episode": the storyboard stage still needs portrait/scene assets
        # resolved before it can run, so project the prep_pack payload here
        # instead of reusing _load_screenplay's legacy-only return value.
        prep_pack_payload = episode_prep_pack_payload(ep)
        if prep_pack_payload is None:
            return
        from app.production.screenplay_authority import (
            project_prep_pack_to_screenplay,
        )

        screenplay = project_prep_pack_to_screenplay(prep_pack_payload)
    try:
        from app.portraits import ensure_cards_for_screenplay

        portrait_result = await ensure_cards_for_screenplay(
            ep["project_id"],
            ep["episode_no"],
            screenplay,
            bible,
        )
        if portrait_result.get("blocking_errors"):
            raise StageError(
                "人物资产准备",
                list(portrait_result["blocking_errors"]),
            )
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
        ).fetchone()
        bible = _project_bible_or_placeholder(project)

        from app.scenes import ensure_scenes_for_storyboard

        scene_result = await ensure_scenes_for_storyboard(
            ep["project_id"],
            ep["episode_no"],
            screenplay,
            bible,
        )
        if scene_result.get("blocking_errors"):
            raise StageError(
                "场景资产准备",
                list(scene_result["blocking_errors"]),
            )
        conn.execute(
            "UPDATE episodes SET storyboard_warning=NULL WHERE id=? "
            "AND storyboard_warning LIKE '资产异步准备:%'",
            (episode_id,),
        )
        conn.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - text work remains independently recoverable
        public = errors.record_and_format(
            exc,
            action="storyboard_assets_background",
            context={
                "episode_id": episode_id,
                "project_id": ep["project_id"],
            },
        )
        conn.execute(
            "UPDATE episodes SET storyboard_warning=? WHERE id=?",
            (
                (
                    "资产异步准备: 人物或场景参考资产尚未完整就绪；"
                    "分镜文本不受影响，视频提交前会继续补齐。"
                    + public
                )[:800],
                episode_id,
            ),
        )
        conn.commit()


async def _storyboard_task(
    episode_id: str,
    *,
    resume: bool = True,
    run_id: str | None = None,
    new_activation: bool = False,
):
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()

    def _preflight_event(event_type: str, message: str, *, payload: dict | None = None,
                         severity: str = "info") -> None:
        if not run_id:
            return
        from app.evidence import repository as _evidence_repository
        _evidence_repository.append_event(
            run_id, event_type, severity, message,
            payload={"episode_id": episode_id, "episode_no": ep["episode_no"], **(payload or {})},
        )

    try:
        if ep["screenplay_status"] != "ready" or not ep["screenplay_json"]:
            raise StageError("分镜脚本", ["请先生成并确认本集可拍剧本，再展开分镜"])
        from app.production.screenplay_authority import resolve_downstream_screenplay

        try:
            screenplay_context = resolve_downstream_screenplay(
                episode_id,
                conn=conn,
            )
        except Exception as exc:
            raise StageError("分镜脚本", [f"已发布剧本权威链无效：{exc}"]) from exc
        screenplay = screenplay_context.screenplay
        narrative_authority = screenplay_context.narrative_authority_required
        published_storyboard_baseline = False
        if (
            narrative_authority
            and ep["published_storyboard_artifact_id"]
            and ep["storyboard_completion_certificate_id"]
        ):
            try:
                from app.production.certificate import verify_completion_certificate

                baseline_certificate = verify_completion_certificate(
                    str(ep["storyboard_completion_certificate_id"]),
                    expected_kind="storyboard",
                    expected_scope_id=episode_id,
                    expected_artifact_id=str(
                        ep["published_storyboard_artifact_id"]
                    ),
                    expected_production_revision_id=str(
                        ep["storyboard_production_revision_id"] or ""
                    ),
                    allow_consumed=True,
                    allow_stale_artifact_for_revision=True,
                )
                published_storyboard_baseline = bool(
                    baseline_certificate.consumed_at is not None
                )
            except Exception:
                published_storyboard_baseline = False
        if resume and published_storyboard_baseline:
            rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                (episode_id,),
            ).fetchall()
            board = _board_from_shot_rows(rows, ep["episode_no"])
            from app.production.certificate import (
                verify_completion_certificate,
            )
            from app.narrative import storyboard_authority_projection

            published_artifact = evidence_repository.get_artifact(
                str(ep["published_storyboard_artifact_id"])
            )
            if published_artifact is None:
                raise StageError("分镜脚本", ["当前发布分镜 Artifact 已缺失"])
            published_board = Storyboard.model_validate(
                published_artifact.get("content") or {}
            )
            verify_completion_certificate(
                str(ep["storyboard_completion_certificate_id"]),
                expected_kind="storyboard",
                expected_scope_id=episode_id,
                expected_artifact_id=str(ep["published_storyboard_artifact_id"]),
                expected_production_revision_id=str(
                    ep["storyboard_production_revision_id"] or ""
                ),
                allow_consumed=True,
                allow_stale_artifact_for_revision=True,
            )
            projection_restored = bool(
                storyboard_authority_projection(board)
                != storyboard_authority_projection(published_board)
            )
            if projection_restored:
                if ep["status"] in {"confirmed", "generating", "done", "mixed"}:
                    raise StageError(
                        "分镜脚本",
                        ["已确认分镜投影与证书漂移，禁止自动覆盖，请先停止下游"],
                    )
                if len(rows) != len(published_board.shots):
                    raise StageError(
                        "分镜脚本",
                        ["当前 shots 行数与已签证 Storyboard Artifact 不一致"],
                    )
                from app.storyboard_supervisor import _write_shot_fields

                conn.execute("BEGIN IMMEDIATE")
                try:
                    for row, shot in zip(rows, published_board.shots):
                        _write_shot_fields(
                            conn,
                            str(row["id"]),
                            shot,
                            row["storyboard_artifact_id"],
                            narrative_authority=True,
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                rows = conn.execute(
                    "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                    (episode_id,),
                ).fetchall()
                board = _board_from_shot_rows(rows, ep["episode_no"])
                _preflight_event(
                    "STORYBOARD_PUBLISHED_PROJECTION_RESTORED",
                    "已从签证 Artifact 恢复 mutable shots 正式投影",
                    payload={
                        "artifact_id": ep["published_storyboard_artifact_id"],
                        "shot_count": len(rows),
                    },
                    severity="warning",
                )

            # The immutable baseline was verified above with its exact
            # artifact, revision and evaluation set. A stale baseline may
            # seed an isolated revision, but it no longer authorizes
            # downstream work; the replacement publish issues a new
            # completion certificate.
            p = conn.execute(
                "SELECT * FROM projects WHERE id=?",
                (ep["project_id"],),
            ).fetchone()
            bible = _project_bible_or_placeholder(p)
            from app.identity_contracts import (
                canonicalize_storyboard_operational_identities,
            )

            identity_repairs = canonicalize_storyboard_operational_identities(
                board,
                bible,
                screenplay,
            )
            if not identity_repairs:
                from app.storyboard_supervisor import (
                    _repair_is_pending,
                    load_latest_checkpoint,
                    run_storyboard_supervisor,
                )

                repair_checkpoint = load_latest_checkpoint(episode_id)
                if (
                    repair_checkpoint is not None
                    and _repair_is_pending(repair_checkpoint)
                    and ep["status"] in {"scripted", "scripting"}
                ):
                    _preflight_event(
                        "STORYBOARD_PUBLISHED_REPAIR_CANDIDATE_STARTED",
                        "已基于发布分镜建立隔离修订候选，正式投影保持不变",
                        payload={
                            "artifact_id": ep["published_storyboard_artifact_id"],
                            "window_start": (
                                repair_checkpoint.last_repair or {}
                            ).get("window_start"),
                            "window_end": (
                                repair_checkpoint.last_repair or {}
                            ).get("window_end"),
                        },
                    )
                    return await run_storyboard_supervisor(
                        episode_id,
                        resume=True,
                        run_id=run_id,
                        preflight_done=True,
                        new_activation=False,
                    )
            if not identity_repairs and projection_restored:
                conn.execute(
                    "UPDATE episodes SET status='scripted',script_error=NULL,"
                    "storyboard_warning=NULL WHERE id=?",
                    (episode_id,),
                )
                conn.commit()
                from app.storyboard_supervisor import load_latest_checkpoint

                return load_latest_checkpoint(episode_id)
            if not identity_repairs or ep["status"] in {
                "confirmed", "generating", "done", "mixed",
            }:
                raise StageError(
                    "分镜脚本",
                    [
                        "当前叙事分镜已原子发布，不能在正式 shots 上原地续跑；"
                        "请创建语义修订候选并重新发布"
                    ],
                )
            old_artifact_id = str(ep["published_storyboard_artifact_id"])
            old_revision_id = str(ep["storyboard_production_revision_id"] or "")
            stamp = now()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if old_revision_id:
                    conn.execute(
                        """UPDATE production_revisions
                              SET status='superseded',updated_at=?
                            WHERE id=? AND status='published'
                              AND published_artifact_id=?""",
                        (stamp, old_revision_id, old_artifact_id),
                    )
                conn.execute(
                    """UPDATE artifacts
                          SET status='superseded',
                              stale_reason='deterministic_identity_projection_rebind'
                        WHERE id=? AND status IN ('validated','approved')""",
                    (old_artifact_id,),
                )
                episode_update = conn.execute(
                    """UPDATE episodes
                          SET storyboard_artifact_id=NULL,
                              working_storyboard_artifact_id=NULL,
                              published_storyboard_artifact_id=NULL,
                              storyboard_completion_certificate_id=NULL,
                              storyboard_production_revision_id=NULL
                        WHERE id=? AND status IN ('scripted','scripting')
                          AND published_storyboard_artifact_id=?
                          AND storyboard_completion_certificate_id=?""",
                    (
                        episode_id,
                        old_artifact_id,
                        ep["storyboard_completion_certificate_id"],
                    ),
                )
                if episode_update.rowcount != 1:
                    raise RuntimeError("分镜身份修订撤下旧发布指针发生并发冲突")
                from app.storyboard_supervisor import _write_shot_fields

                for row, shot in zip(rows, board.shots):
                    _write_shot_fields(
                        conn,
                        str(row["id"]),
                        shot,
                        None,
                        narrative_authority=True,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            _ensure_current_storyboard_shot_artifacts(
                conn,
                episode_id,
                board,
            )
            conn.commit()
            _preflight_event(
                "STORYBOARD_IDENTITY_PROJECTION_REVISION_CREATED",
                "已撤下未确认旧版并创建确定性身份修订工作投影",
                payload={
                    "old_artifact_id": old_artifact_id,
                    "old_revision_id": old_revision_id,
                    "repairs": identity_repairs,
                },
            )
            ep = conn.execute(
                "SELECT * FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            published_storyboard_baseline = False
        conn.execute("UPDATE episodes SET status='scripting', script_error=NULL, storyboard_warning=NULL WHERE id=?", (episode_id,))
        conn.commit()
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        # Text identities were hard-gated during screenplay production. Image
        # packages now start immediately but no longer serialize storyboard text.
        if not task_registry.active("storyboard_assets", episode_id):
            task_registry.spawn(
                "storyboard_assets",
                episode_id,
                _prepare_storyboard_assets_background(episode_id),
                project_id=ep["project_id"],
            )
        _preflight_event(
            "STORYBOARD_ASSETS_SCHEDULED",
            "人物与场景资产已并行准备；分镜文本立即继续",
            payload={"blocking": False},
        )

        # 恢复旧 checkpoint 时先把模型产生的引号漂移/拼接式证据收敛为授权原文中的
        # 连续片段。严格匹配不足的内容保持未解决，仍由确认门禁拦截。
        if resume and not published_storyboard_baseline:
            from app.storyboard_workspace import repair_generated_source_bindings

            evidence_repair = repair_generated_source_bindings(episode_id)
            if evidence_repair["bound"]:
                _preflight_event(
                    "STORYBOARD_SOURCE_EVIDENCE_REALIGNED",
                    f"续跑前修复源文引用：已绑定 {evidence_repair.get('bound', 0)} 条证据",
                    payload=evidence_repair,
                )
                log_provider_call(
                    "storyboard_source_evidence_repair",
                    config.MODEL_TEXT,
                    "SOURCE_EVIDENCE_REALIGNED",
                    None,
                    0,
                    meta={"episode_id": episode_id, **evidence_repair},
                )

        _preflight_event(
            "STORYBOARD_PREFLIGHT_FINISHED",
            "剧本身份合同已就绪，资产异步准备，交由分镜 Supervisor 展开生成",
        )

        # 集级 Supervisor：大纲 → 逐镜 → 整集校验 → 修复，完成后等待人工确认。
        from app.storyboard_supervisor import run_storyboard_supervisor
        return await run_storyboard_supervisor(
            episode_id,
            resume=resume,
            run_id=run_id,
            preflight_done=True,
            new_activation=new_activation,
        )
    except (StageError, Exception) as exc:  # noqa: BLE001
        # 回滚这次失败尝试自己遗留的未提交写入，必须在任何其他 conn.execute 之前做，
        # 包括紧接着的 errors.log_error() 调用——app.db.insert_error_log 自己也在
        # 这同一个 task 缓存连接（app.db.get_conn() 按 asyncio.current_task() 缓存，
        # 同一个 task 内所有 get_conn() 调用拿到同一个连接对象）上落一条 error_logs
        # 行并 conn.commit()，如果先调用 log_error 再回滚，回滚已经来不及——error_logs
        # 那次 commit 会把此刻这个连接上任何未提交的挂起写入一起提交掉，回滚这一步就
        # 成了马后炮。app.production.storyboard_pack.persist_storyboard_pack 先 DELETE
        # 本集旧 shots（ON DELETE CASCADE 一并删掉 shot_versions——已经生成好的真实
        # 视频记录）再逐段 INSERT 新 shots，整段过程故意不提交，只在函数末尾成功写完
        # 全部段落后 commit 一次，靠"从不中途提交"做到"要么整批换成新的、要么旧的
        # 原封不动"。这里如果不在最前面先回滚，随后不管是 log_error 的隐式 commit
        # 还是下面给 episodes 表写状态后的显式 conn.commit()，都会把这个还没走到
        # persist_storyboard_pack 自己那次 commit 的半成品事务提交下去：旧集已经被
        # 删，新集只写了一部分（甚至一行都没来得及写），这一集就空了——这正是"重新
        # 生成"中途失败导致已产出真实视频被连带清空的根因。回滚只丢弃这次失败尝试
        # 自己产生的未提交写入；本函数在调用 run_storyboard_supervisor 之前的每一步
        # 都已经在各自的检查点上 conn.commit() 过（例如上面的 status='scripting'
        # 那次），回滚不会波及那些已经落盘的状态，也不影响下面重新读到的 saved 计数
        # ——修复后 saved 永远只反映"最后一次真正提交成功"的那份数据。
        if conn.in_transaction:
            conn.rollback()
        rec = errors.log_error(exc, action="storyboard_generate", context={"episode_id": episode_id})
        saved = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
        if run_id:
            run_row = conn.execute(
                "SELECT status FROM workflow_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            owner = conn.execute(
                "SELECT active_storyboard_run_id FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            if (
                run_row
                and (
                    run_row["status"] not in {"CREATED", "RUNNING"}
                    or not owner
                    or owner["active_storyboard_run_id"] != run_id
                )
            ):
                return
        # Supervisor 已把 WAITING_* 写为 scripted+script_error；此处只处理未捕获异常。
        # ``scripting+script_error`` 仍可能只是更早的场景包降级提示，不能据此吞掉
        # 当前异常，否则 Step 会被误记为 SUCCEEDED，Run 却在外层被判为 FAILED，
        # 同时真正异常也会被旧提示遮蔽。
        ep_now = conn.execute("SELECT status, script_error FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if ep_now and ep_now["status"] in {"scripted", "confirmed"} and ep_now["script_error"]:
            return
        if saved:
            try:
                planned = len(
                    json.loads(
                        conn.execute(
                            "SELECT storyboard_outline_json FROM episodes WHERE id=?",
                            (episode_id,),
                        ).fetchone()["storyboard_outline_json"] or "{}"
                    ).get("shots") or []
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                planned = 0
            if planned and saved >= planned:
                note = (
                    f"分镜 {saved}/{planned} 镜已完成，但发布证据校验失败："
                    f"{rec.message}（{rec.code} · {rec.error_id}）"
                )
            else:
                # 修复回滚之后，这里的 saved/rows 一定是"最后一次真正提交成功"的
                # 那份数据，不会再是这次失败尝试留下的半成品——分镜台 2.0.0
                # （episode_prep_pack）路径的持久化本身就是单事务、一次性全写，
                # 已经没有"逐镜追加、可从中间补写"的旧管线语义了（那套按镜头
                # 逐个提交的修复状态机已随 event_chain 驱动的旧分镜管线一起下线，
                # 见 app/storyboard_supervisor.py run_storyboard_supervisor 的
                # 说明）。这条提示不再暗示"接着上次断点补写"，只如实说明当前
                # 保留的是上一次成功持久化的版本，未被本轮失败改动。
                note = (
                    f"分镜生成失败，数据库中的 {saved} 个镜头是上一次成功持久化的"
                    "版本，未被本轮失败改动；可重新生成分镜。"
                    f"本轮失败原因：{rec.message}"
                    f"（{rec.code} · {rec.error_id}）"
                )
            conn.execute("UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                         (note[:800], episode_id))
        else:
            conn.execute("UPDATE episodes SET status='script_failed', script_error=? WHERE id=?",
                         (rec.public, episode_id))
        conn.commit()
        # Persist the recoverable episode projection, then preserve the
        # exception boundary so WorkflowRecorder marks both the step and run
        # as failed. Returning here would falsely record STEP_SUCCEEDED.
        raise


def recover_storyboard_tasks() -> int:
    """恢复被服务重启中断的分镜任务，不接管用户主动暂停的 Run。"""
    from app.generation_concurrency import PRIORITY_RECOVERY

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM episodes "
        "WHERE status='scripting' AND screenplay_status='ready' AND screenplay_json IS NOT NULL"
    ).fetchall()
    resumed = 0
    for row in rows:
        episode_id = row["id"]
        if task_registry.active("storyboard", episode_id):
            continue
        latest = conn.execute(
            """SELECT id,status,failure_code FROM workflow_runs
               WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
               ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        if latest:
            if latest["status"] in {"CREATED", "RUNNING"}:
                # A durable run may belong to another live service instance.
                continue
            if latest["status"] != "PAUSED_EXTERNAL" or latest["failure_code"] != "SERVICE_RESTART":
                # PARTIAL / WAITING_HUMAN / user_pause are explicit manual resume points.
                continue
            parent = latest
        else:
            # Legacy databases may have only the projection state and no run ledger.
            parent = None
        recorder = None
        try:
            if row["storyboard_outline_json"]:
                from app.production.screenplay_authority import (
                    resolve_downstream_screenplay,
                )
                from app.storyboard_authority import (
                    resolve_storyboard_outline_authority,
                )

                screenplay_context = resolve_downstream_screenplay(
                    episode_id,
                    conn=conn,
                )
                if screenplay_context.narrative_authority_required:
                    resolve_storyboard_outline_authority(
                        episode_id,
                        conn=conn,
                    )
            recorder = _new_storyboard_recorder(
                episode_id, resume=True,
                requested_by="system", trigger_type="resume",
                parent_run_id=parent["id"] if parent else None,
            )
            installed = conn.execute(
                "UPDATE episodes SET active_storyboard_run_id=? "
                "WHERE id=? AND status='scripting' AND active_storyboard_run_id IS ?",
                (recorder.run_id, episode_id, row["active_storyboard_run_id"]),
            )
            if installed.rowcount != 1:
                conn.rollback()
                recorder.cancel("分镜恢复启动权已变化，当前运行未启动", conn=None)
                continue
            conn.commit()
            task_registry.spawn(
                "storyboard", episode_id,
                _storyboard_guarded_recorded(
                    episode_id,
                    recorder,
                    resume=True,
                    new_activation=False,
                    priority=PRIORITY_RECOVERY,
                ),
                project_id=row["project_id"],
            )
            resumed += 1
        except Exception as exc:  # noqa: BLE001 - one bad episode must not block startup
            public = errors.record_and_format(
                exc,
                action="storyboard_recovery_spawn",
                context={"episode_id": episode_id, "previous_run_id": row["active_storyboard_run_id"]},
            )
            from app.storyboard_supervisor import load_latest_checkpoint
            checkpoint = load_latest_checkpoint(episode_id)
            shot_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
            ).fetchone()["c"])
            recoverable = bool(shot_count or (checkpoint and checkpoint.validated_prefix_end > 0))
            conn.execute(
                "UPDATE episodes SET status=?, script_error=?, active_storyboard_run_id=NULL WHERE id=?",
                (
                    "script_failed" if recoverable else "planned",
                    (
                        f"服务重启后的分镜恢复未能启动；"
                        f"{'已通过镜头和恢复点均已保留，可点击继续分镜' if recoverable else '剧本已保留，可重新生成分镜'}。"
                        f"{public}"
                    ),
                    episode_id,
                ),
            )
            conn.commit()
            if recorder is not None:
                try:
                    recorder.cancel("分镜恢复任务未能启动，已回滚到可重试状态", conn=None)
                except Exception:  # noqa: BLE001
                    pass
    return resumed


def _shot_video_is_stale(conn, shot_row, episode_storyboard_id: str | None) -> bool:
    """分镜 Artifact 不一致，或采用版冻结的人物/场景版本已落后于本集最新，均判 stale。"""
    try:
        adopted = shot_row["adopted_version_id"]
    except (KeyError, IndexError, TypeError):
        adopted = None
    if not adopted:
        return False
    try:
        shot_art = shot_row["storyboard_artifact_id"]
    except (KeyError, IndexError, TypeError):
        shot_art = None
    if episode_storyboard_id and shot_art and shot_art != episode_storyboard_id:
        episode_art = conn.execute(
            "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
            (episode_storyboard_id,),
        ).fetchone()
        try:
            episode_parents = json.loads(
                episode_art["parent_artifact_ids_json"] or "[]"
            ) if episode_art else []
        except (TypeError, ValueError):
            episode_parents = []
        if shot_art not in episode_parents:
            return True
    ver = conn.execute(
        "SELECT artifact_id, image_inputs FROM shot_versions WHERE id=?", (adopted,)
    ).fetchone()
    if not ver or not ver["artifact_id"]:
        # 无 artifact 时仍可检查资产版本 stale
        if ver and _shot_adopted_assets_stale(conn, shot_row, ver):
            return True
        return False
    art = conn.execute(
        "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
        (ver["artifact_id"],),
    ).fetchone()
    if art:
        try:
            parents = json.loads(art["parent_artifact_ids_json"] or "[]")
        except (TypeError, ValueError):
            parents = []
        if episode_storyboard_id and parents:
            valid_storyboard_parents = {episode_storyboard_id}
            if shot_art:
                valid_storyboard_parents.add(shot_art)
            if not any(parent in valid_storyboard_parents for parent in parents):
                return True
    return _shot_adopted_assets_stale(conn, shot_row, ver)


def _shot_adopted_assets_stale(conn, shot_row, version_row) -> bool:
    """采用版 reference_manifest 中的人物/场景 revision 是否仍是本集当前生效版本。"""
    try:
        from app.multiview import (
            character_multiview_enabled, scene_multiview_enabled,
            manifest_asset_revision_ids, manifest_asset_view_fingerprints,
            portrait_row_for_episode, scene_row_for_episode,
        )
    except Exception:  # noqa: BLE001
        return False
    if not character_multiview_enabled() and not scene_multiview_enabled():
        return False
    meta = {}
    try:
        meta = json.loads(version_row["image_inputs"] or "{}") if version_row["image_inputs"] else {}
    except (TypeError, ValueError, KeyError):
        meta = {}
    manifest = meta.get("reference_manifest") if isinstance(meta, dict) else None
    if not isinstance(manifest, dict):
        # 回退：从首张带 dependency_manifest 的参考图读取
        for ref in (meta.get("reference_images") or []) if isinstance(meta, dict) else []:
            if isinstance(ref, dict) and isinstance(ref.get("dependency_manifest"), dict):
                manifest = ref["dependency_manifest"]
                break
    if not isinstance(manifest, dict):
        return False
    frozen_ids = manifest_asset_revision_ids(manifest)
    if not frozen_ids:
        return False
    try:
        episode_id = shot_row["episode_id"]
    except (KeyError, IndexError, TypeError):
        return False
    ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return False
    project_id = ep["project_id"]
    episode_no = ep["episode_no"]
    for key, frozen_rev in frozen_ids.items():
        if key.startswith("character:"):
            name = key.split(":", 1)[1]
            row = portrait_row_for_episode(project_id, name, episode_no)
            current_id = row["id"] if row else None
            if current_id != frozen_rev:
                return True
        elif key.startswith("scene:"):
            name = key.split(":", 1)[1]
            row = scene_row_for_episode(project_id, name, episode_no)
            current_id = row["id"] if row else None
            if current_id != frozen_rev:
                return True
    frozen_views = manifest_asset_view_fingerprints(manifest)
    for (kind, name, role), frozen_fp in frozen_views.items():
        if kind == "character":
            parent = portrait_row_for_episode(project_id, name, episode_no)
            table = "character_portrait_views"
            parent_column = "portrait_id"
        else:
            parent = scene_row_for_episode(project_id, name, episode_no)
            table = "scene_reference_views"
            parent_column = "scene_reference_id"
        if not parent:
            return True
        current = conn.execute(
            f"SELECT input_fingerprint FROM {table} "
            f"WHERE {parent_column}=? AND view_role=? AND status='ready'",
            (parent["id"], role),
        ).fetchone()
        current_fp = current["input_fingerprint"] if current else None
        if current_fp != frozen_fp:
            return True
    return False


def _new_storyboard_recorder(
    episode_id: str,
    *,
    resume: bool = False,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    checkpoints = rows_to_dicts(conn.execute(
        "SELECT shot_no, storyboard_artifact_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall())
    project = conn.execute(
        "SELECT bible_artifact_id FROM projects WHERE id=?",
        (ep["project_id"],),
    ).fetchone()
    bible_artifact_id = _storyboard_bound_bible_artifact_id(
        episode_id,
        ep,
        project["bible_artifact_id"] if project else None,
        resume=resume,
    )
    contract = get_contract("storyboard")
    return WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            ep["screenplay_artifact_id"],
            bible_artifact_id,
            ep["storyboard_outline_json"],
            checkpoints,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "supervisor": True,
            "checkpoint": "supervisor_and_per_shot",
            "max_iterations_per_shot": contract.max_iterations,
            "max_inner_iterations": 4,
            "blocker_warning_candidate_allowed": False,
            "provider_retry": {
                "max_retries_per_call": config.TEXT_PROVIDER_MAX_RETRIES,
                "base_delay_s": config.TEXT_PROVIDER_RETRY_BASE_DELAY,
                "strategy": "bounded_exponential_backoff_same_request",
            },
        },
        config_snapshot={"storyboard_shot_max_tokens": config.STORYBOARD_SHOT_MAX_TOKENS},
        parent_run_id=parent_run_id,
    )


def _storyboard_bound_bible_artifact_id(
    episode_id: str,
    episode_row,
    current_artifact_id: str | None,
    *,
    resume: bool,
) -> str | None:
    """Use the checkpoint Bible on resume when its screenplay still matches."""
    if not resume:
        return current_artifact_id
    from app.storyboard_supervisor import load_latest_checkpoint

    cp = load_latest_checkpoint(episode_id)
    if cp is None:
        return current_artifact_id
    bound_screenplay = str(
        cp.input_versions.get("screenplay_artifact_id") or ""
    )
    current_screenplay = str(episode_row["screenplay_artifact_id"] or "")
    if bound_screenplay and bound_screenplay != current_screenplay:
        return current_artifact_id
    return cp.input_versions.get("bible_artifact_id") or current_artifact_id


async def _recorded_storyboard_task(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    resume: bool,
    new_activation: bool = False,
) -> None:
    recorder.start()
    try:
        conn = get_conn()
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        # 旧的最小化测试 schema 可能没有 board_text_provider 列（真实数据库总有，见
        # app/db.py 迁移）；SELECT * 拿满全部列，缺列时下面按 None 处理，不炸查询。
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
        ).fetchone()
        bible_artifact_id = _storyboard_bound_bible_artifact_id(
            episode_id,
            ep,
            project["bible_artifact_id"] if project else None,
            resume=resume,
        )
        input_ids = [
            artifact_id for artifact_id in (
                bible_artifact_id,
                ep["screenplay_artifact_id"],
            ) if artifact_id
        ]
        context = ContextPack(goal="集级 Supervisor：生成整集分镜直至通过并等待人工确认")
        if ep["screenplay_json"]:
            context.add_text(
                "screenplay", ep["screenplay_json"],
                source_artifact_id=ep["screenplay_artifact_id"], limit=24000,
            )
        from app import model_registry
        from app.harness.text_provider_scope import stage_text_provider

        resolved_text_provider = model_registry.resolve_stage_text_provider(
            dict(project).get("board_text_provider") if project else None
        )
        with stage_text_provider(resolved_text_provider):
            _step_id, supervisor_result = await recorder.step(
                "storyboard",
                lambda: _storyboard_task_with_sqlite_lock_retry(
                    episode_id,
                    resume=resume,
                    run_id=getattr(recorder, "run_id", None),
                    new_activation=new_activation,
                ),
                contract_key="storyboard",
                agent_name="storyboard_supervisor",
                input_artifact_ids=input_ids,
                context_manifest=context.manifest(),
            )
        result = conn.execute(
            "SELECT status, script_error, storyboard_artifact_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        phase = str(getattr(supervisor_result, "phase", "") or "")
        outcome = str(getattr(supervisor_result, "outcome", "") or "")
        if result and result["status"] == "confirmed":
            recorder.succeed("分镜已确认（尚未产生视频费用）", conn=None)
        elif phase == "SUCCEEDED" and outcome == "SUCCEEDED_READY_FOR_CONFIRM":
            recorder.succeed("分镜已完成，等待人工确认", conn=None)
        elif phase == "PAUSED_EXTERNAL":
            from app.orchestration.state_machine import transition_run
            transition_run(
                recorder.run_id, "RUNNING", "PAUSED_EXTERNAL",
                (result["script_error"] if result else None) or outcome or "Supervisor 已暂停",
                failure_code=(
                    "PROVIDER_UNAVAILABLE"
                    if outcome == "PAUSED_PROVIDER_UNAVAILABLE"
                    else "USER_PAUSE"
                ), conn=None,
            )
        elif phase == "WAITING_AUTHORIZATION":
            from app.orchestration.state_machine import transition_run
            transition_run(
                recorder.run_id, "RUNNING", "WAITING_AUTHORIZATION",
                (result["script_error"] if result else None) or outcome,
                failure_code="WAITING_AUTHORIZATION", conn=None,
            )
        elif phase == "WAITING_HUMAN":
            from app.orchestration.state_machine import transition_run
            wait_state = (
                "WAITING_RETRY"
                if outcome in {
                    "WAITING_RETRY_ACTIVATION_BUDGET",
                    "WAITING_RETRY_CAS_CONFLICT",
                    "WAITING_RETRY_GATE_REPAIR_EXHAUSTED",
                    "WAITING_RETRY_STORYBOARD_INCOMPLETE",
                }
                else "WAITING_HUMAN"
            )
            transition_run(
                recorder.run_id, "RUNNING", wait_state,
                (result["script_error"] if result else None) or outcome or "Supervisor 等待处理",
                failure_code=outcome or wait_state, conn=None,
            )
        elif (
            supervisor_result is None
            and result
            and result["status"] == "scripted"
            and result["storyboard_artifact_id"]
            and not result["script_error"]
        ):
            recorder.succeed("分镜已完成，等待人工确认", conn=None)
        else:
            message = (
                str(result["script_error"] or "分镜 Supervisor 未进入可恢复终态")
                if result else "分镜生成失败"
            )
            # Run 终态与页面投影必须在同一收尾路径收敛。否则 finally 只清活动
            # 指针而遗留 status=scripting，分镜台会在任务中心已经 FAILED 后仍显示运行。
            conn.execute(
                "UPDATE episodes SET status='script_failed',script_error=? "
                "WHERE id=? AND active_storyboard_run_id=?",
                (message[:800], episode_id, recorder.run_id),
            )
            conn.commit()
            recorder.fail(RuntimeError(message), conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，分镜运行等待自动续做", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:
        recorder.fail(exc, conn=None)
        raise
    finally:
        # The workflow run remains available for audit/resume lineage, but it must
        # stop acting as a write lock once this coroutine has ended. The guarded
        # comparison avoids clearing a newer run that may have started meanwhile.
        try:
            cleanup_conn = get_conn()
            cleanup_conn.execute(
                "UPDATE episodes SET active_storyboard_run_id=NULL "
                "WHERE id=? AND active_storyboard_run_id=?",
                (episode_id, recorder.run_id),
            )
            cleanup_conn.commit()
        except Exception:  # noqa: BLE001
            pass


_STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S = (0.25, 1.0, 2.0)


def _is_transient_sqlite_lock(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code is None:
        return False
    return (int(error_code) & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


async def _storyboard_task_with_sqlite_lock_retry(
    episode_id: str,
    *,
    resume: bool,
    run_id: str | None,
    new_activation: bool,
):
    """Resume from the durable checkpoint after a transient SQLite writer lock."""
    for attempt in range(len(_STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S) + 1):
        try:
            return await _storyboard_task(
                episode_id,
                resume=bool(resume or attempt),
                run_id=run_id,
                new_activation=bool(new_activation and attempt == 0),
            )
        except sqlite3.OperationalError as exc:
            if (
                not _is_transient_sqlite_lock(exc)
                or attempt >= len(_STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S)
            ):
                raise
            get_conn().rollback()
            delay_s = _STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S[attempt]
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "STORYBOARD_SQLITE_LOCK_RETRY",
                    "warning",
                    "SQLite 写锁冲突，已回滚未完成事务并从安全检查点重试",
                    payload={
                        "attempt": attempt + 1,
                        "delay_s": delay_s,
                        "episode_id": episode_id,
                    },
                )
            await asyncio.sleep(delay_s)

    raise RuntimeError("unreachable")


def _storyboard_generation_is_live(ep: dict) -> bool:
    """判断活动指针是否真的对应本进程/存储中的活跃分镜任务。

    ``episodes.status='scripting'`` 是 UI 投影，不是可靠的任务存活证明。旧 Run 已经
    PARTIAL/CANCELLED/FAILED 时若仍按该字段去重，继续按钮只会返回旧 run_id，页面进入
    “正在生成”但后台没有任务。进程内注册表优先；跨重启仅 CREATED/RUNNING Run 算活跃。
    """
    if task_registry.active("storyboard", ep["id"]):
        return True
    try:
        run_id = ep["active_storyboard_run_id"]
    except (KeyError, IndexError, TypeError):
        run_id = None
    if not run_id:
        return False
    if str(run_id).startswith("starting:"):
        return True
    from app.evidence import repository
    run = repository.get_run(run_id)
    return bool(run and run.get("status") in {"CREATED", "RUNNING"})


@router.post("/episodes/{episode_id}/storyboard")
async def start_storyboard(episode_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route
    body_was_explicit = isinstance(body, dict)
    body = _as_body_dict(body)
    ep = _episode_or_404(episode_id)
    resume_existing = _storyboard_has_persisted_work(episode_id, dict(ep))
    payload = {"episode_id": episode_id, **body}
    routed = await ui_route("storyboard.generate", payload)
    if routed is not None:
        return routed
    if resume_existing:
        return await resume_storyboard(episode_id, body if body_was_explicit else None)
    if body_was_explicit:
        from app.storyboard_workspace import require_preview
        require_preview(body.get("preflight_token"), "start:create", episode_id, consume=True)
    _require_harness_engine(ep["project_id"])
    if ep["screenplay_publish_fence"]:
        raise HTTPException(409, "剧本正在安全发布，暂不能启动新分镜任务")
    if _storyboard_generation_is_live(ep):
        return {
            "status": "scripting",
            "run_id": ep["active_storyboard_run_id"],
            "deduplicated": True,
        }
    if not _screenplay_ready(ep):
        rebuild_block = _screenplay_rebuild_block(get_conn(), ep)
        if rebuild_block is not None:
            raise HTTPException(409, rebuild_block)
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    conn = get_conn()
    previous = {
        "status": ep["status"],
        "script_error": ep["script_error"],
        "active_storyboard_run_id": ep["active_storyboard_run_id"],
    }
    start_claim = f"starting:{int(now())}:{new_id('storyboard')}"
    cursor = conn.execute(
        """UPDATE episodes
              SET status='scripting', script_error=NULL, active_storyboard_run_id=?
            WHERE id=? AND screenplay_publish_fence=0
              AND active_storyboard_run_id IS ?""",
        (start_claim, episode_id, previous["active_storyboard_run_id"]),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise HTTPException(409, "分镜状态已被其他请求抢占，请刷新后查看当前任务")
    conn.commit()
    recorder = None
    coro = None
    try:
        recorder = _new_storyboard_recorder(episode_id)
        owned = conn.execute(
            "UPDATE episodes SET active_storyboard_run_id=? "
            "WHERE id=? AND active_storyboard_run_id=?",
            (recorder.run_id, episode_id, start_claim),
        )
        if owned.rowcount != 1:
            conn.rollback()
            raise RuntimeError("分镜启动所有权已变化")
        conn.commit()
        coro = _storyboard_guarded_recorded(
            episode_id,
            recorder,
            resume=False,
            new_activation=False,
            priority=0,
        )
        task_registry.spawn(
            "storyboard", episode_id, coro, project_id=ep["project_id"],
        )
    except Exception as exc:
        if coro is not None:
            coro.close()
        current_owner = recorder.run_id if recorder is not None else start_claim
        conn.execute(
            """UPDATE episodes
                  SET status=?, script_error=?, active_storyboard_run_id=?
                WHERE id=? AND active_storyboard_run_id=?""",
            (
                previous["status"],
                previous["script_error"],
                previous["active_storyboard_run_id"],
                episode_id,
                current_owner,
            ),
        )
        conn.commit()
        if recorder is not None:
            try:
                recorder.cancel("分镜任务未能启动，剧集状态已回滚", conn=None)
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "STORYBOARD_START_SPAWN_FAILED",
            "message": "分镜任务未能启动，剧本和原状态已保留，请重试",
            "recovery_action": "重新点击生成分镜；尚未开始逐镜生成",
            "episode_id": episode_id,
            "rolled_back": True,
            "recoverable": True,
        }) from exc
    return {
        "status": "scripting",
        "run_id": recorder.run_id,
        "action": "create",
        "resource_uri": f"manju://runs/{recorder.run_id}",
    }


async def resume_storyboard(episode_id: str, body: dict | None = Body(None)):
    """内部从 Supervisor Checkpoint / 已验证前缀恢复；对外统一走 POST /storyboard。"""
    body_was_explicit = isinstance(body, dict)
    body = _as_body_dict(body)
    preview_payload: dict = {}
    if body_was_explicit:
        from app.storyboard_workspace import require_preview
        preview_payload = require_preview(
            body.get("preflight_token"),
            "start:resume",
            episode_id,
            consume=True,
        )
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["screenplay_publish_fence"]:
        raise HTTPException(409, "剧本正在安全发布，暂不能继续分镜")
    if _storyboard_generation_is_live(ep):
        return {
            "status": "scripting",
            "run_id": ep["active_storyboard_run_id"],
            "deduplicated": True,
        }
    if not _screenplay_ready(ep):
        rebuild_block = _screenplay_rebuild_block(get_conn(), ep)
        if rebuild_block is not None:
            raise HTTPException(409, rebuild_block)
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    conn = get_conn()
    saved = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"]
    from app.storyboard_supervisor import load_latest_checkpoint
    cp = load_latest_checkpoint(episode_id)
    if (
        cp is not None
        and not _storyboard_checkpoint_matches_screenplay(cp, dict(ep))
        and not _storyboard_has_material(episode_id, dict(ep))
    ):
        # The old checkpoint is historical evidence for a screenplay version
        # whose downstream projection has already been cleared.  It cannot be
        # resumed as shot N+1 of the new screenplay.
        cp = None
    if not saved and cp is None:
        raise HTTPException(409, "当前没有可恢复的 Supervisor / 逐镜 checkpoint，请重新生成分镜")
    resume_decision = _storyboard_resume_decision(episode_id, dict(ep))
    if not resume_decision["allowed"]:
        raise HTTPException(409, resume_decision["blocking_reason"])
    prepared_published_repair = False
    if preview_payload.get("resume_mode") == "repair_existing":
        from app.storyboard_supervisor import prepare_published_storyboard_repair

        prepare_published_storyboard_repair(
            episode_id,
            [
                str(message)
                for message in preview_payload.get("current_gate_issues") or []
                if str(message).strip()
            ],
        )
        prepared_published_repair = True
    parent = conn.execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    previous = {
        "status": ep["status"],
        "script_error": ep["script_error"],
        "active_storyboard_run_id": ep["active_storyboard_run_id"],
    }
    start_claim = f"starting:{int(now())}:{new_id('storyboard')}"
    cursor = conn.execute(
        """UPDATE episodes
              SET status='scripting', script_error=NULL, active_storyboard_run_id=?
            WHERE id=? AND screenplay_publish_fence=0
              AND active_storyboard_run_id IS ?""",
        (start_claim, episode_id, previous["active_storyboard_run_id"]),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise HTTPException(409, "分镜状态已被其他请求抢占，请刷新后查看当前任务")
    conn.commit()
    recorder = None
    coro = None
    try:
        recorder = _new_storyboard_recorder(
            episode_id,
            resume=True,
            trigger_type="resume",
            parent_run_id=parent["id"] if parent else None,
        )
        # 任务注册前持久化指针，避免 Run 已启动但页面无法轮询或控制。
        owned = conn.execute(
            "UPDATE episodes SET active_storyboard_run_id=? "
            "WHERE id=? AND active_storyboard_run_id=?",
            (recorder.run_id, episode_id, start_claim),
        )
        if owned.rowcount != 1:
            conn.rollback()
            raise RuntimeError("分镜续跑所有权已变化")
        conn.commit()
        coro = _storyboard_guarded_recorded(
            episode_id,
            recorder,
            resume=True,
            new_activation=not prepared_published_repair,
            priority=0,
        )
        task_registry.spawn(
            "storyboard", episode_id, coro, project_id=ep["project_id"],
        )
    except Exception as exc:
        if coro is not None:
            coro.close()
        conn.execute(
            """UPDATE episodes
               SET active_storyboard_run_id=?, status=?, script_error=?
               WHERE id=? AND active_storyboard_run_id IS ?""",
            (
                previous["active_storyboard_run_id"],
                previous["status"],
                previous["script_error"],
                episode_id,
                recorder.run_id if recorder is not None else start_claim,
            ),
        )
        conn.commit()
        if recorder is not None:
            try:
                recorder.cancel("分镜继续任务未能启动，状态已回滚", conn=None)
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "STORYBOARD_RESUME_SPAWN_FAILED",
            "message": "分镜继续任务未能启动，已回滚到可重试状态",
            "recovery_action": "请稍后重试；已通过镜头和 checkpoint 均已保留",
            "episode_id": episode_id,
            "run_id": recorder.run_id if recorder is not None else None,
            "rolled_back": True,
            "recoverable": True,
        }) from exc
    checkpoint_saved = int(cp.validated_prefix_end or 0) if cp else 0
    resumed_from_shot = checkpoint_saved if cp else int(saved)
    checkpoint_next = int(cp.next_shot_no or 0) if cp else 0
    return {
        "status": "scripting",
        "run_id": recorder.run_id,
        "action": "resume",
        "resumed_from_shot": resumed_from_shot,
        "next_shot_no": checkpoint_next or resumed_from_shot + 1,
        "checkpoint_only": bool(not saved and cp is not None),
    }


@router.post("/projects/{project_id}/storyboard-all")
async def start_storyboard_all(project_id: str):
    """为本项目所有【待分镜】(planned) 剧集批量生成分镜，限并发逐集触发。
    必须是 async def：sync 路由跑在无事件循环的线程池里，asyncio.create_task 会抛
    'no running event loop'，导致状态已置为 scripting 但任务从未启动（前端显示分镜中、模型却收不到请求）。
    同时回收状态卡在 scripting 但无在跑任务的孤儿集，便于一键修复。"""
    from app.generation_concurrency import PRIORITY_BATCH
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("storyboard.generate_batch", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, status, script_error, screenplay_status, screenplay_json, screenplay_publish_fence, "
        "active_storyboard_run_id "
        "FROM episodes WHERE project_id=? AND status IN ('planned','scripting','script_failed') ORDER BY episode_no",
        (project_id,)).fetchall()
    # 待分镜的；以及卡在“分镜中”却没有在跑任务的孤儿（需重新触发）
    candidates = [
        r for r in rows
        if r["screenplay_status"] == "ready" and r["screenplay_json"]
        and not r["screenplay_publish_fence"]
        and not _storyboard_generation_is_live(dict(r))
    ]
    if not candidates:
        raise HTTPException(409, "没有可展开分镜的剧集（需先生成剧本，且状态为待分镜/分镜失败/卡住的分镜中）")
    run_ids: list[str] = []
    failed_to_start: list[dict] = []
    for candidate in candidates:
        eid = candidate["id"]
        recorder = None
        try:
            recorder = _new_storyboard_recorder(eid, trigger_type="batch")
        except Exception as exc:
            public = errors.record_and_format(
                exc, action="storyboard_batch_recorder",
                context={"project_id": project_id, "episode_id": eid},
            )
            failed_to_start.append({"episode_id": eid, "error": public, "retryable": True})
            continue
        installed = conn.execute(
            """UPDATE episodes
               SET status='scripting', script_error=NULL, active_storyboard_run_id=?
               WHERE id=? AND status=? AND active_storyboard_run_id IS ?
                 AND screenplay_publish_fence=0
                 AND NOT EXISTS (
                     SELECT 1 FROM workflow_runs AS wr
                     WHERE wr.id=episodes.active_storyboard_run_id
                       AND wr.status IN ('CREATED','RUNNING')
                 )""",
            (
                recorder.run_id,
                eid,
                candidate["status"],
                candidate["active_storyboard_run_id"],
            ),
        )
        if installed.rowcount != 1:
            conn.rollback()
            recorder.cancel("批量分镜启动权已变化，当前运行未启动", conn=None)
            failed_to_start.append({
                "episode_id": eid,
                "error": "剧集状态刚刚发生变化，本次未接管",
                "retryable": True,
            })
            continue
        conn.commit()
        coro = _storyboard_guarded_recorded(
            eid,
            recorder,
            resume=True,
            new_activation=True,
            priority=PRIORITY_BATCH,
        )
        try:
            task_registry.spawn(
                "storyboard", eid, coro, project_id=project_id,
            )
        except Exception as exc:
            coro.close()
            rollback_status = (
                "script_failed" if candidate["status"] == "scripting" else candidate["status"]
            )
            rollback_error = (
                "检测到上次分镜任务已中断；本次批量任务也未能启动，可继续重试"
                if candidate["status"] == "scripting"
                else candidate["script_error"]
            )
            conn.execute(
                """UPDATE episodes
                   SET active_storyboard_run_id=NULL, status=?, script_error=?
                   WHERE id=? AND active_storyboard_run_id=?""",
                (rollback_status, rollback_error, eid, recorder.run_id),
            )
            conn.commit()
            recorder.cancel("批量分镜任务未能启动，状态已回滚", conn=None)
            public = errors.record_and_format(
                exc, action="storyboard_batch_spawn",
                context={"project_id": project_id, "episode_id": eid},
            )
            failed_to_start.append({"episode_id": eid, "error": public, "retryable": True})
            continue
        run_ids.append(recorder.run_id)
    if not run_ids:
        raise HTTPException(503, {
            "code": "STORYBOARD_BATCH_START_FAILED",
            "message": "批量分镜任务均未能启动，各集剧本和恢复点已保留，可直接重试",
            "failed_to_start": failed_to_start,
        })
    return {
        "started": len(run_ids),
        "run_ids": run_ids,
        "failed_to_start": failed_to_start,
        "retryable_failures": len(failed_to_start),
    }


async def _storyboard_guarded_recorded(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    resume: bool,
    new_activation: bool,
    priority: int,
) -> None:
    from app.generation_concurrency import run_with_generation_slot

    await run_with_generation_slot(
        "storyboard",
        lambda: _recorded_storyboard_task(
            episode_id,
            recorder,
            resume=resume,
            new_activation=new_activation,
        ),
        priority=priority,
    )


@router.post("/episodes/{episode_id}/storyboard/cancel")
async def cancel_storyboard(episode_id: str, body: dict | None = Body(None)):
    """立即暂停分镜任务，保留工作镜头和安全检查点以便继续或清空。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("storyboard.cancel", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    if ep["status"] != "scripting":
        return {
            "status": ep["status"],
            "deduplicated": True,
            "message": "任务已自然结束或此前已停止；当前状态保持不变",
        }
    await task_registry.cancel_and_wait("storyboard", episode_id)
    await task_registry.cancel_and_wait("storyboard_assets", episode_id)
    from app.storyboard_workspace import finalize_storyboard_cancellation
    return finalize_storyboard_cancellation(
        episode_id,
        run_id=ep["active_storyboard_run_id"],
        message="已从分镜台暂停生成",
        paused=True,
    )


def _assert_storyboard_clear_not_running(episode_id: str, ep: dict) -> None:
    """Clearing is a stopped-state action; never use it as an implicit pause."""
    from app.storyboard_supervisor import load_latest_checkpoint

    active_run_id = ep.get("active_storyboard_run_id")
    active_run = (
        get_conn().execute(
            "SELECT status FROM workflow_runs WHERE id=?", (active_run_id,),
        ).fetchone()
        if active_run_id else None
    )
    checkpoint = load_latest_checkpoint(episode_id)
    stopped_phases = {
        "PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_HUMAN",
        "WAITING_AUTHORIZATION", "CANCELLED", "SUCCEEDED",
    }
    task_is_live = task_registry.active("storyboard", episode_id)
    run_is_live = bool(active_run and active_run["status"] in {"CREATED", "RUNNING"})
    episode_is_live = bool(
        ep.get("status") == "scripting"
        and (checkpoint is None or checkpoint.phase not in stopped_phases)
    )
    if task_is_live or run_is_live or episode_is_live:
        raise HTTPException(409, "分镜任务仍在运行，请先暂停任务，再清空分镜")


@router.post("/episodes/{episode_id}/storyboard/clear-preview")
def preview_storyboard_clear(episode_id: str):
    """Return the complete impact of resetting an episode's storyboard workspace."""
    from app.storyboard_workspace import create_preview, episode_fingerprint

    ep = _episode_or_404(episode_id)
    _assert_storyboard_clear_not_running(episode_id, dict(ep))
    # A task can stop before its first shot with either a zero-prefix checkpoint
    # or only a preflight failure projection. Both must remain clearable;
    # otherwise Resume and Clear reject the same empty workspace.
    stopped_preflight_failure = bool(
        ep["status"] == "script_failed" and str(ep["script_error"] or "").strip()
    )
    if (
        not _storyboard_has_persisted_work(episode_id, dict(ep))
        and not stopped_preflight_failure
    ):
        raise HTTPException(409, "当前没有可清空的分镜数据")
    conn = get_conn()
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
    ).fetchone()["c"])
    video_version_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
        (episode_id,),
    ).fetchone()["c"])
    reference_asset_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM reference_assets a
           JOIN reference_sets r ON r.id=a.reference_set_id
           JOIN shots s ON s.id=r.shot_id
           WHERE s.episode_id=? AND a.deleted=0""",
        (episode_id,),
    ).fetchone()["c"])
    workflow_run_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM workflow_runs
           WHERE workflow_type IN ('storyboard','video_completion')
             AND scope_type='episode' AND scope_id=?""",
        (episode_id,),
    ).fetchone()["c"])
    delivery_package_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM delivery_packages WHERE episode_id=?",
        (episode_id,),
    ).fetchone()["c"])
    payload = {
        "shot_count": shot_count,
        "video_version_count": video_version_count,
        "reference_asset_count": reference_asset_count,
        "workflow_run_count": workflow_run_count,
        "delivery_package_count": delivery_package_count,
        "active_task_will_stop": bool(
            ep["active_storyboard_run_id"] or ep["active_video_run_id"]
        ),
        "screenplay_preserved": True,
        "irreversible": True,
    }
    return create_preview(
        "storyboard_clear",
        episode_id,
        payload,
        baseline_fingerprint=episode_fingerprint(episode_id),
    )


@router.post("/episodes/{episode_id}/storyboard/clear")
async def apply_storyboard_clear(episode_id: str, body: dict):
    """Clear all storyboard/downstream state after an explicit current preview."""
    from app.storyboard_workspace import require_preview

    ep = _episode_or_404(episode_id)
    _assert_storyboard_clear_not_running(episode_id, dict(ep))
    require_preview(body.get("preview_token"), "storyboard_clear", episode_id)
    return await clear_storyboard_projection(episode_id)


def _episode_target_video_model(ep) -> str:
    """归一化本集绑定的视频供应商 key；历史脏值/空值回落到 provider 默认。"""
    from app import video_providers

    raw = str(ep["target_video_model"] or "").strip()
    return raw if raw in video_providers.registered_providers() else "hiagent"


def _require_video_clear_write_scope(project_id: str) -> None:
    """切换视频模型的破坏性分支（清空本集视频产物）闸门。

    与 Command Bus 的 ``video.clear_episode_videos``（``risk=R3_DESTRUCTIVE``，
    ``scopes={"manju:project-write"}``）同档：不可逆清空要求写权限，review/
    readonly 角色不具备。scope 必须按本集所属 workspace 取（``Principal.
    scopes_for``），不能用 ``principal.all_scopes``——后者是该用户所有 workspace
    的并集，在别处有写权限不代表在这里有。``principal is None`` 视为未挂会话
    闸门的内部调用，沿用 ``app/authz/resolve.py::require_workspace_access`` 与
    ``app/capabilities/bus.py::_authorize`` 的既有约定，直接放行。
    """
    from app.auth.principal import get_current_principal
    from app.authz.resolve import _workspace_of_project

    principal = get_current_principal()
    if principal is None:
        return
    resolution = _workspace_of_project(get_conn(), project_id)
    workspace_id = resolution.value if resolution.kind == "workspace" else None
    if "manju:project-write" not in principal.scopes_for(workspace_id):
        raise HTTPException(403, "清空本集视频产物需要 manju:project-write 权限")


@router.post("/episodes/{episode_id}/video-model")
async def set_episode_video_model(episode_id: str, body: dict | None = None):
    """分镜台人工切换本集绑定的视频生成模型；与生成台强绑定，不做静默转换。

    两个供应商的提示词方言互不兼容（Seedance 自由中文散文 vs MiniMax H3 结构化
    英文字段+双语台词块），留着旧方言已生成的产物就是脏数据。本集已有视频生成
    产物时必须显式带 ``confirm_clear_prompts=true`` 二次确认才会执行，执行时
    连带清空这些产物（复用 ``videos/clear`` 同一套清空机制，保留参考图），这条
    清空分支要求 ``manju:project-write``（见 ``_require_video_clear_write_scope``），
    与 ``video.clear_episode_videos`` 同档；没有产物的普通切换不受此限。写法
    与权限约定参照本文件的 ``storyboard/clear-preview``/``storyboard/clear``：
    分镜台本机人工入口，不向 Agent/MCP 开放。
    """
    from app import video_providers

    body = body or {}
    target = str(body.get("target_video_model") or "").strip()
    options = video_providers.registered_providers()
    if target not in options:
        raise HTTPException(
            422,
            f"未知视频模型：{target or '(空)'}；可选：{'、'.join(sorted(options))}",
        )
    ep = _episode_or_404(episode_id)
    current = _episode_target_video_model(ep)
    if target == current:
        return {
            "episode_id": episode_id, "target_video_model": current,
            "changed": False, "cleared_videos": 0,
        }
    conn = get_conn()
    prompt_artifact_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
        (episode_id,),
    ).fetchone()["c"])
    if prompt_artifact_count and not bool(body.get("confirm_clear_prompts")):
        raise HTTPException(409, {
            "code": "VIDEO_MODEL_SWITCH_REQUIRES_CONFIRMATION",
            "message": (
                f"本集已有 {prompt_artifact_count} 条视频生成产物（提示词方言绑定于 "
                f"{current}），切换到 {target} 会清空这些产物；两套模型提示词语法不兼容，"
                "不能混用。请带 confirm_clear_prompts=true 二次确认后再切换。"
            ),
            "prompt_artifact_count": prompt_artifact_count,
            "current_target_video_model": current,
            "requested_target_video_model": target,
        })
    cleared_videos = 0
    if prompt_artifact_count:
        _require_video_clear_write_scope(ep["project_id"])
        snapshot = _review_upstream_snapshot(episode_id)
        if snapshot["active_upstream_runs"]:
            raise HTTPException(409, {
                "code": "UPSTREAM_RUN_ACTIVE",
                "message": "编剧或分镜任务仍在写入，不能切换视频模型",
                "active_runs": snapshot["active_upstream_runs"],
            })
        _require_provider_clearance(conn, episode_id=episode_id)
        await reset_video_completion_state(episode_id, reason="VIDEO_MODEL_SWITCH")
        worker.pause_episode_video_tasks(episode_id)
        try:
            clear_result = worker.clear_episode_video_assets(episode_id)
        except ValueError as exc:
            raise HTTPException(409, getattr(exc, "detail", str(exc))) from exc
        cleared_videos = int(clear_result.get("videos") or 0)
    conn = get_conn()
    conn.execute(
        "UPDATE episodes SET target_video_model=? WHERE id=?",
        (target, episode_id),
    )
    conn.commit()
    _review_write_audit(
        "episode.video_model_switch", "episode", episode_id,
        old_state={"target_video_model": current},
        new_state={"target_video_model": target, "cleared_videos": cleared_videos},
    )
    return {
        "episode_id": episode_id, "target_video_model": target,
        "changed": True, "cleared_videos": cleared_videos,
    }


async def clear_storyboard_projection(episode_id: str) -> dict:
    """Fast product reset: clear current production state while retaining audit history."""
    cancelled_tasks = 0
    for kind in ("storyboard", "video_completion"):
        try:
            cancelled_tasks += int(await asyncio.wait_for(
                task_registry.cancel_and_wait(kind, episode_id), timeout=10,
            ))
        except TimeoutError as exc:
            raise HTTPException(
                409,
                "相关生成任务未能在 10 秒内安全停止；未开始清空，请稍后重试",
            ) from exc

    def _reset_projection() -> dict:
        import shutil

        from app.completion_grant import ProviderTasksNotTerminalError

        ep = _episode_or_404(episode_id)
        if ep["screenplay_publish_fence"]:
            raise HTTPException(409, "剧本正在发布，请完成后再清空分镜")
        conn = get_conn()
        claimed = conn.execute(
            "UPDATE episodes SET screenplay_publish_fence=1 "
            "WHERE id=? AND screenplay_publish_fence=0",
            (episode_id,),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            raise HTTPException(409, "分镜状态刚刚发生变化，请稍后重试")
        conn.commit()

        package_paths: list[str] = []
        try:
            shot_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
            ).fetchone()["c"])
            media_versions = int(conn.execute(
                """SELECT COUNT(*) AS c FROM shot_versions v
                   JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
                (episode_id,),
            ).fetchone()["c"])
            run_rows = conn.execute(
                """SELECT id,workflow_type,status FROM workflow_runs
                   WHERE workflow_type IN ('storyboard','video_completion')
                     AND scope_type='episode' AND scope_id=?""",
                (episode_id,),
            ).fetchall()
            package_paths = [
                str(row["package_path"])
                for row in conn.execute(
                    "SELECT package_path FROM delivery_packages WHERE episode_id=?",
                    (episode_id,),
                ).fetchall()
                if row["package_path"]
            ]

            worker.delete_episode_shots(episode_id)
            conn = get_conn()
            conn.execute("BEGIN IMMEDIATE")
            stamp = now()
            active_run_ids = [
                str(row["id"])
                for row in run_rows
                if row["status"] in evidence_repository.ACTIVE_RUN_STATUSES
            ]
            if active_run_ids:
                marks = ",".join("?" for _ in active_run_ids)
                conn.execute(
                    f"""UPDATE step_runs SET status='CANCELLED', finished_at=COALESCE(finished_at,?),
                           exit_reason=COALESCE(exit_reason,'CLEARED_BY_USER')
                       WHERE run_id IN ({marks})
                         AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')""",
                    (stamp, *active_run_ids),
                )
                conn.execute(
                    f"""UPDATE provider_calls SET status='CANCELLED',
                           error=COALESCE(error,'CLEARED_BY_USER')
                       WHERE run_id IN ({marks}) AND status='RUNNING'""",
                    active_run_ids,
                )
                conn.execute(
                    f"""UPDATE workflow_runs SET status='CANCELLED',
                           failure_code='CLEARED_BY_USER',
                           failure_message='用户清空分镜工作区',
                           finished_at=COALESCE(finished_at,?), updated_at=?
                       WHERE id IN ({marks})""",
                    (stamp, stamp, *active_run_ids),
                )

            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shot_audio'"
            ).fetchone():
                conn.execute("DELETE FROM shot_audio WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM storyboard_action_previews WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM storyboard_edit_sessions WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM storyboard_workspace_state WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM delivery_packages WHERE episode_id=?", (episode_id,))
            conn.execute(
                "DELETE FROM completion_grants WHERE episode_id=? AND kind='storyboard'",
                (episode_id,),
            )
            conn.execute(
                "DELETE FROM production_grants WHERE episode_id=? AND kind='storyboard'",
                (episode_id,),
            )
            conn.execute(
                """UPDATE production_revisions SET status='superseded', updated_at=?
                   WHERE episode_id=? AND kind='storyboard' AND status='active'""",
                (stamp, episode_id),
            )
            conn.execute(
                """UPDATE artifacts SET status='rejected',
                       stale_reason=COALESCE(stale_reason,'用户已清空分镜工作区')
                   WHERE type IN ('storyboard_supervisor_checkpoint','storyboard_outline')
                     AND scope_type='episode' AND scope_id=?
                     AND status IN ('candidate','validated','approved')""",
                (episode_id,),
            )
            conn.execute(
                """UPDATE episodes SET
                       storyboard_outline_json=NULL,
                       storyboard_artifact_id=NULL,
                       storyboard_warning=NULL,
                       active_storyboard_run_id=NULL,
                       working_storyboard_artifact_id=NULL,
                       published_storyboard_artifact_id=NULL,
                       storyboard_production_revision_id=NULL,
                       storyboard_completion_certificate_id=NULL,
                       active_video_run_id=NULL,
                       video_control_json=NULL,
                       delivery_artifact_id=NULL,
                       delivery_status='not_ready',
                       status='planned',
                       script_error=NULL,
                       screenplay_publish_fence=0
                   WHERE id=?""",
                (episode_id,),
            )
            from app.storyboard_authority import (
                clear_storyboard_outline_authority,
            )

            clear_storyboard_outline_authority(
                episode_id,
                conn=conn,
            )
            if "storyboard_control_json" in {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(episodes)").fetchall()
            }:
                conn.execute(
                    "UPDATE episodes SET storyboard_control_json=NULL WHERE id=?",
                    (episode_id,),
                )
            conn.commit()

            projects_root = config.PROJECTS_DIR.resolve()
            removed_files = 0
            for raw_path in dict.fromkeys(package_paths):
                candidate = Path(raw_path)
                try:
                    resolved = candidate.resolve()
                    if resolved == projects_root or projects_root not in resolved.parents:
                        continue
                    if resolved.is_dir():
                        shutil.rmtree(resolved, ignore_errors=True)
                        removed_files += 1
                    elif resolved.exists():
                        resolved.unlink()
                        removed_files += 1
                except OSError:
                    continue
            storyboard_runs = sum(
                1 for row in run_rows if row["workflow_type"] == "storyboard"
            )
            return {
                "cleared": True,
                "episode_id": episode_id,
                "shots_deleted": shot_count,
                "media_versions_deleted": media_versions,
                "storyboard_runs_preserved": storyboard_runs,
                "downstream_runs_preserved": len(run_rows) - storyboard_runs,
                "files_deleted": removed_files,
                "cancelled_tasks": cancelled_tasks,
                "screenplay_preserved": True,
                "audit_history_preserved": True,
            }
        except ProviderTasksNotTerminalError as exc:
            conn = get_conn()
            if conn.in_transaction:
                conn.rollback()
            raise HTTPException(409, exc.detail) from exc
        except Exception:
            conn = get_conn()
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn = get_conn()
            conn.execute(
                "UPDATE episodes SET screenplay_publish_fence=0 WHERE id=?",
                (episode_id,),
            )
            conn.commit()

    return await asyncio.to_thread(_reset_projection)


async def clear_storyboard(episode_id: str):
    """清理整集分镜痕迹；产品入口必须先通过 ``storyboard_clear`` 影响预览。

    The screenplay is intentionally retained.  Unlike cancellation, clearing also
    removes checkpoints, workflow/provider cache rows, active revisions and all
    shot-derived media so the next start is observably and behaviorally clean.
    """
    from app.completion_grant import ProviderTasksNotTerminalError

    ep = _episode_or_404(episode_id)
    if ep["screenplay_publish_fence"]:
        raise HTTPException(409, "剧本正在发布，请完成后再清空分镜")

    conn = get_conn()
    claimed = conn.execute(
        "UPDATE episodes SET screenplay_publish_fence=1 "
        "WHERE id=? AND screenplay_publish_fence=0",
        (episode_id,),
    )
    if claimed.rowcount != 1:
        conn.rollback()
        raise HTTPException(409, "分镜状态刚刚发生变化，请稍后重试")
    conn.commit()

    cancelled_tasks = 0
    package_paths: list[str] = []
    artifact_paths: list[str] = []
    try:
        for kind in ("storyboard", "video_completion"):
            cancelled_tasks += int(await task_registry.cancel_and_wait(kind, episode_id))

        conn = get_conn()
        shot_ids = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM shots WHERE episode_id=?", (episode_id,),
            ).fetchall()
        ]
        shot_count = len(shot_ids)
        media_versions = int(conn.execute(
            """SELECT COUNT(*) AS c FROM shot_versions v
               JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
            (episode_id,),
        ).fetchone()["c"])
        package_paths = [
            str(row["package_path"])
            for row in conn.execute(
                "SELECT package_path FROM delivery_packages WHERE episode_id=?",
                (episode_id,),
            ).fetchall()
            if row["package_path"]
        ]

        run_rows = conn.execute(
            """SELECT id,workflow_type FROM workflow_runs
               WHERE workflow_type IN ('storyboard','video_completion')
                 AND scope_type='episode' AND scope_id=?""",
            (episode_id,),
        ).fetchall()
        run_ids = [str(row["id"]) for row in run_rows]
        storyboard_run_count = sum(
            1 for row in run_rows if row["workflow_type"] == "storyboard"
        )
        step_ids: list[str] = []
        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            step_ids = [
                str(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM step_runs WHERE run_id IN ({marks})", run_ids,
                ).fetchall()
            ]

        certificate_artifact_ids = [
            str(row["artifact_id"])
            for row in conn.execute(
                "SELECT artifact_id FROM completion_certificates "
                "WHERE kind='storyboard' AND scope_id=?",
                (episode_id,),
            ).fetchall()
        ]
        artifact_where = [
            "(scope_type='episode' AND scope_id=? AND type IN "
            "('storyboard','storyboard_outline','storyboard_supervisor_checkpoint',"
            "'video_supervisor_checkpoint','video_coverage_report'))",
            "(scope_type='storyboard_checkpoint' AND scope_id LIKE ?)",
        ]
        artifact_params: list[object] = [episode_id, f"{episode_id}:%"]
        if shot_ids:
            marks = ",".join("?" for _ in shot_ids)
            artifact_where.append(f"(scope_type='shot' AND scope_id IN ({marks}))")
            artifact_params.extend(shot_ids)
        if step_ids:
            marks = ",".join("?" for _ in step_ids)
            artifact_where.append(f"created_by_step_run_id IN ({marks})")
            artifact_params.extend(step_ids)
        artifact_rows = conn.execute(
            "SELECT id,file_path FROM artifacts WHERE " + " OR ".join(artifact_where),
            artifact_params,
        ).fetchall()
        artifact_ids = list(dict.fromkeys(
            [str(row["id"]) for row in artifact_rows] + certificate_artifact_ids
        ))
        artifact_paths = [str(row["file_path"]) for row in artifact_rows if row["file_path"]]

        # This removes shots, references, media jobs and final video files first.
        worker.delete_episode_shots(episode_id)
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shot_audio'"
        ).fetchone():
            conn.execute("DELETE FROM shot_audio WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM storyboard_action_previews WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM storyboard_edit_sessions WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM storyboard_workspace_state WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM delivery_packages WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM customer_feedback WHERE episode_id=?", (episode_id,))
        conn.execute(
            "DELETE FROM completion_grants WHERE episode_id=? AND kind='storyboard'",
            (episode_id,),
        )
        conn.execute(
            "DELETE FROM production_grants WHERE episode_id=? AND kind='storyboard'",
            (episode_id,),
        )
        conn.execute(
            "DELETE FROM completion_certificates WHERE kind='storyboard' AND scope_id=?",
            (episode_id,),
        )
        conn.execute(
            "DELETE FROM production_revisions WHERE episode_id=? AND kind='storyboard'",
            (episode_id,),
        )
        conn.execute(
            "DELETE FROM review_action_audit WHERE scope_type='episode' AND scope_id=?",
            (episode_id,),
        )
        if shot_ids:
            marks = ",".join("?" for _ in shot_ids)
            conn.execute(
                f"DELETE FROM review_action_audit WHERE scope_type='shot' AND scope_id IN ({marks})",
                shot_ids,
            )

        conn.execute(
            """UPDATE episodes SET
                   storyboard_outline_json=NULL,
                   storyboard_artifact_id=NULL,
                   storyboard_warning=NULL,
                   active_storyboard_run_id=NULL,
                   working_storyboard_artifact_id=NULL,
                   published_storyboard_artifact_id=NULL,
                   storyboard_production_revision_id=NULL,
                   storyboard_completion_certificate_id=NULL,
                   active_video_run_id=NULL,
                   video_control_json=NULL,
                   delivery_artifact_id=NULL,
                   delivery_status='not_ready',
                   status='planned',
                   script_error=NULL,
                   screenplay_publish_fence=0
               WHERE id=?""",
            (episode_id,),
        )
        from app.storyboard_authority import (
            clear_storyboard_outline_authority,
        )

        clear_storyboard_outline_authority(
            episode_id,
            conn=conn,
        )
        if "storyboard_control_json" in {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(episodes)").fetchall()
        }:
            conn.execute(
                "UPDATE episodes SET storyboard_control_json=NULL WHERE id=?",
                (episode_id,),
            )

        if artifact_ids:
            marks = ",".join("?" for _ in artifact_ids)
            conn.execute(f"DELETE FROM gate_decisions WHERE artifact_id IN ({marks})", artifact_ids)
            conn.execute(f"DELETE FROM evaluations WHERE artifact_id IN ({marks})", artifact_ids)
            conn.execute(
                f"UPDATE artifacts SET superseded_by_artifact_id=NULL "
                f"WHERE superseded_by_artifact_id IN ({marks})",
                artifact_ids,
            )
            conn.execute(f"DELETE FROM artifacts WHERE id IN ({marks})", artifact_ids)

        if run_ids:
            run_marks = ",".join("?" for _ in run_ids)
            conn.execute(f"DELETE FROM gate_decisions WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(f"DELETE FROM provider_calls WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(f"DELETE FROM run_events WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(f"UPDATE agent_tool_calls SET run_id=NULL WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(
                f"UPDATE customer_feedback SET revision_run_id=NULL "
                f"WHERE revision_run_id IN ({run_marks})",
                run_ids,
            )
            if step_ids:
                step_marks = ",".join("?" for _ in step_ids)
                conn.execute(
                    f"UPDATE artifacts SET created_by_step_run_id=NULL "
                    f"WHERE created_by_step_run_id IN ({step_marks})",
                    step_ids,
                )
                conn.execute(
                    f"UPDATE evaluations SET step_run_id=NULL WHERE step_run_id IN ({step_marks})",
                    step_ids,
                )
                conn.execute(
                    f"UPDATE step_runs SET parent_step_run_id=NULL "
                    f"WHERE parent_step_run_id IN ({step_marks})",
                    step_ids,
                )
            conn.execute(f"DELETE FROM step_runs WHERE run_id IN ({run_marks})", run_ids)
            conn.execute(
                f"UPDATE workflow_runs SET parent_run_id=NULL WHERE parent_run_id IN ({run_marks})",
                run_ids,
            )
            conn.execute(f"DELETE FROM workflow_runs WHERE id IN ({run_marks})", run_ids)
        conn.commit()

        # Delete packaged/file artifacts only when they are inside this workspace.
        import shutil

        projects_root = config.PROJECTS_DIR.resolve()
        removed_files = 0
        for raw_path in dict.fromkeys(package_paths + artifact_paths):
            candidate = Path(raw_path)
            try:
                resolved = candidate.resolve()
                if resolved == projects_root or projects_root not in resolved.parents:
                    continue
                if resolved.is_dir():
                    shutil.rmtree(resolved, ignore_errors=True)
                    removed_files += 1
                elif resolved.exists():
                    resolved.unlink()
                    removed_files += 1
            except OSError:
                continue
        return {
            "cleared": True,
            "episode_id": episode_id,
            "shots_deleted": shot_count,
            "media_versions_deleted": media_versions,
            "storyboard_runs_deleted": storyboard_run_count,
            "downstream_runs_deleted": len(run_ids) - storyboard_run_count,
            "storyboard_artifacts_deleted": len(artifact_ids),
            "files_deleted": removed_files,
            "cancelled_tasks": cancelled_tasks,
            "screenplay_preserved": True,
        }
    except ProviderTasksNotTerminalError as exc:
        conn = get_conn()
        if conn.in_transaction:
            conn.rollback()
        raise HTTPException(409, exc.detail) from exc
    except Exception:
        conn = get_conn()
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn = get_conn()
        conn.execute(
            "UPDATE episodes SET screenplay_publish_fence=0 WHERE id=?",
            (episode_id,),
        )
        conn.commit()


def _storyboard_issue_targets_shot(message: str, index: int, shot_no: int) -> bool:
    """精确定位镜头诊断，避免 shot_no=1 误匹配 shot_no=10～19。"""
    if f"shots[{index}](shot_no={shot_no})" in message:
        return True
    return bool(
        re.search(rf"(?<!\d)shot_no\s*=\s*{shot_no}(?!\d)", message)
        or re.search(rf"第\s*{shot_no}\s*镜", message)
    )


def _storyboard_status_snapshot(
    ep: dict,
    shots: list[dict],
    supervisor: dict | None,
    screenplay: EpisodeScreenplay | None = None,
    screenplay_rebuild_error: Exception | None = None,
) -> dict:
    """返回供所有分镜台区域共同消费的 v1 原子状态投影。"""
    from app.storyboard_workspace import episode_fingerprint, monotonic_snapshot_version

    screenplay_ready = bool(
        screenplay_rebuild_error is None
        and ep.get("screenplay_status") == "ready"
        and ep.get("screenplay_artifact_id")
    )
    shot_count = len(shots)
    outline_count = 0
    try:
        outline_count = len(json.loads(ep.get("storyboard_outline_json") or "{}").get("shots") or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        outline_count = 0
    # 结构编辑会撤销整集分镜产物，并把新顺序写回大纲。此时旧 supervisor
    # checkpoint 只描述上一次生成，不能继续覆盖用户刚批准的新计划镜数。
    structural_draft = ep.get("storyboard_artifact_id") is None and outline_count > 0
    planned = int(
        (outline_count if structural_draft else 0)
        or (supervisor or {}).get("expected_total")
        or ep.get("storyboard_planned_shots")
        or outline_count
        or shot_count
        or 0
    )
    # ``validated_prefix_end`` is a safe-resume boundary, not the number of rows
    # currently visible in the draft.  In particular, an explicit zero must not
    # fall back to ``shot_count`` or the UI would claim that every draft shot is
    # safe after a repair invalidated the whole prefix.
    passed = (
        min(shot_count, max(0, int(supervisor.get("validated_prefix_end") or 0)))
        if supervisor is not None
        else shot_count
    )
    final_valid = bool(shots and shots[-1].get("is_final"))
    phase = str((supervisor or {}).get("phase") or "")
    active_run_live = _storyboard_generation_is_live(ep)
    # ``episodes.status`` 是业务投影，不是任务存活证明。Run 已 FAILED/CANCELLED
    # 或活动指针已清理后，即使旧 checkpoint 仍停在 GENERATING_SHOTS，也绝不能
    # 继续向前端报告 running。
    running = ep.get("status") == "scripting" and active_run_live
    incomplete_terminal_checkpoint = bool(
        phase == "SUCCEEDED"
        and (
            shot_count != planned
            or passed != shot_count
            or not final_valid
        )
    )
    paused = not active_run_live and (
        phase in {
            "PAUSED_EXTERNAL", "PAUSED_BUDGET",
            "WAITING_HUMAN", "WAITING_AUTHORIZATION",
        }
        or incomplete_terminal_checkpoint
    )
    confirmed = ep.get("status") in {"confirmed", "generating", "done"}
    if confirmed:
        passed = shot_count
    resume_from = max(
        1,
        int((supervisor or {}).get("next_shot_no") or (passed + 1)),
    )
    complete_structure = bool(
        ep.get("status") in {"scripted", "confirmed", "generating", "done"}
        and shot_count > 0
        and planned == shot_count
        and passed == shot_count
        and final_valid
    )
    terminal_structure = bool(
        complete_structure
        and (
            not supervisor
            or phase == "SUCCEEDED"
        )
    )
    repair = (supervisor or {}).get("last_repair") or {}
    repair_touched = {
        int(value) for value in (repair.get("touched_shot_nos") or [])
        if str(value).isdigit()
    }
    raw_repair_errors = [
        str(message) for message in (repair.get("issue_messages") or [])
        if str(message).strip()
    ]
    # Only typed repair records are current authority. Historical records that
    # contain prose messages without issue codes predate the structural gates
    # and must be re-evaluated instead of interpreted through a word blacklist.
    repair_issue_codes = [
        str(code) for code in (repair.get("issue_codes") or [])
        if str(code).strip()
    ]
    active_repair_errors = (
        []
        if phase == "SUCCEEDED" or not repair_issue_codes
        else raw_repair_errors
    )
    obsolete_policy_repair = bool(
        raw_repair_errors and not repair_issue_codes
    )
    if (
        not active_repair_errors
        and paused
        and ep.get("script_error")
        and not obsolete_policy_repair
    ):
        active_repair_errors = [
            value.strip() for value in str(ep.get("script_error") or "").split("；") if value.strip()
        ]
    gate_errors: list[str] = list(dict.fromkeys(active_repair_errors))
    score_warnings: list[str] = []
    gate_system_error: str | None = None
    published_release_bound = bool(
        ep.get("storyboard_artifact_id")
        and ep.get("storyboard_completion_certificate_id")
        and ep.get("storyboard_production_revision_id")
    )
    publication_evidence_ready = published_release_bound
    evidence_refinalize_only = False
    if complete_structure:
        try:
            try:
                current_gate_evaluator = evaluate_storyboard_for_confirmation
            except NameError:
                # 直接导入 domain 模块时没有 app.api 的共享命名空间注入。
                from app.domain.video_ops import (
                    evaluate_storyboard_for_confirmation as current_gate_evaluator,
                )
            board = Storyboard(
                episode_no=int(ep["episode_no"]),
                shots=[Shot.model_validate(shot) for shot in shots],
            )
            (
                publication_evidence_ready,
                evidence_refinalize_only,
            ) = _storyboard_publication_evidence_state(ep, board)
            project = get_conn().execute(
                "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
            ).fetchone()
            bible = _project_bible_or_placeholder(project)
            evaluation = current_gate_evaluator(
                ep,
                board,
                screenplay,
                bible,
                has_real_bible=bool((project["bible_json"] or "").strip()) if project else False,
                record_metrics=False,
                allow_evidence_refinalize=evidence_refinalize_only,
            )
            # 完整镜头投影的当前同源评估才是门禁真值。
            # 暂停 checkpoint 仅是当时的恢复点，不能让已被确定性
            # 对账修复的旧问题永久覆盖当前数据。
            gate_errors = list(dict.fromkeys(evaluation.errors))
            score_warnings.extend(evaluation.warnings)
            terminal_structure = complete_structure
            if not gate_errors and (raw_repair_errors or ep.get("script_error")):
                obsolete_policy_repair = True
            # 历史 source_excerpt 能否逐字回绑只属于来源审计，不是用户可修复的
            # 分镜结构错误。发布证据仍会记录该 finding，但不能让状态快照误报
            # 一个没有镜号、没有修复入口的整集门禁。
        except Exception as exc:  # noqa: BLE001
            gate_system_error = (
                f"确认门禁执行失败（{type(exc).__name__}）：{exc}"
            )
    for index, shot in enumerate(shots):
        shot_no = int(shot.get("shot_no") or index + 1)
        localized = [
            message for message in gate_errors
            if _storyboard_issue_targets_shot(message, index, shot_no)
        ]
        if shot_no in repair_touched:
            localized.extend(active_repair_errors)
            localized = list(dict.fromkeys(localized))
        # Score-only：质量 warning 仍挂到镜头供 UI 展示，但不进入确认硬门禁。
        localized_scores = [
            message for message in score_warnings
            if _storyboard_issue_targets_shot(message, index, shot_no)
        ]
        if localized_scores:
            shot["qa_warnings"] = localized_scores
        if localized:
            shot["preflight_errors"] = localized
    full_terminal = bool(terminal_structure and not gate_errors)
    repairing_existing = bool(
        final_valid
        and gate_errors
        and (planned <= 0 or shot_count >= planned)
    )
    invalid = bool(
        (running and confirmed)
        or (full_terminal and running)
        or (confirmed and not shots)
    )
    if gate_system_error:
        state, headline, action = (
            "syncing",
            "确认门禁服务异常，暂不可执行写操作",
            "refresh_status",
        )
    elif invalid:
        state, headline, action = "syncing", "状态同步中，暂不可执行高影响操作", "refresh_status"
    elif not screenplay_ready:
        state, headline, action = (
            "no_screenplay",
            "当前剧本需要按新合同重建后才能生成分镜"
            if screenplay_rebuild_error is not None
            else "尚无可用于分镜的剧本",
            "go_screenplay",
        )
    elif running:
        state, headline, action = "running", f"分镜任务进行中，当前处理第 {resume_from} 镜", "view_progress"
    elif confirmed and published_release_bound:
        state = "confirmed"
        headline = (
            "已确认正式版存在证据异常，禁止原地续跑"
            if gate_errors or not publication_evidence_ready
            else "当前分镜已确认"
        )
        action = "go_review_wall"
    elif full_terminal and published_release_bound and storyboard_pack_prompts_complete(
        get_conn(), ep["id"],
    ):
        # 分镜台 2.0.0（app.production.storyboard_pack）路径：发布证据在生成
        # 完成时已自动落盘（published_release_bound），本集视频提示词也已
        # 全部生成（storyboard_pack_prompts_complete）。旧版需要用户额外点一
        # 次"完成发布证据/确认视频提示词"才能把 episodes.status 推到
        # confirmed 的仪式，在这条管线上不做任何这里还没做过的额外校验
        # （见 app.domain.review_wall._review_upstream_snapshot 同一处改动的
        # 注释）——产物齐了就直接可进生成台，不再停下来等一次点击。
        state = "confirmed"
        headline = f"{shot_count}/{planned} 段视频提示词已全部生成，可进入生成台"
        action = "go_review_wall"
    elif paused and not terminal_structure:
        state, headline, action = (
            "paused",
            "整集修复已暂停，可继续修复现有问题镜"
            if repairing_existing
            else f"局部修复已暂停，将从第 {resume_from} 镜继续",
            "resume_storyboard",
        )
    elif terminal_structure and gate_errors:
        state, headline, action = "failed", f"还有 {len(gate_errors)} 个确认门禁问题，可继续修改", "resume_storyboard"
    elif ep.get("status") == "script_failed" or (ep.get("script_error") and not full_terminal):
        state, headline, action = "failed", f"生成停在第 {max(1, passed + 1)} 镜，可继续处理", "resume_storyboard"
    elif confirmed and not publication_evidence_ready:
        state, headline, action = (
            "paused",
            f"{shot_count}/{planned} 镜已通过，待更新发布证据",
            "resume_storyboard",
        )
    elif not shots:
        state, headline, action = "empty", "剧本已就绪，尚未生成分镜", "generate_storyboard"
    elif full_terminal and not publication_evidence_ready:
        state, headline, action = (
            "paused",
            f"{shot_count}/{planned} 镜已通过，待完成发布证据",
            "resume_storyboard",
        )
    elif full_terminal:
        state, headline, action = "ready_to_confirm", f"{shot_count}/{planned} 镜已通过，等待确认", "confirm_storyboard"
    else:
        state, headline, action = "syncing", "分镜尚未达到完整终态", "refresh_status"
    fingerprint = episode_fingerprint(ep["id"])
    feature_flags = {
        "safe_readonly": str(get_setting("storyboard_workspace_safe_readonly") or "false").lower() == "true",
        "structure_edit": str(get_setting("storyboard_structure_edit_enabled") or "true").lower() == "true",
        "source_rebind": str(get_setting("storyboard_source_rebind_enabled") or "true").lower() == "true",
    }
    if feature_flags["safe_readonly"]:
        state = "syncing"
        headline = "分镜台处于安全只读模式，可继续审阅"
        action = "refresh_status"
    resume_mode = None
    if action == "resume_storyboard":
        resume_mode = (
            "finalize_evidence"
            if full_terminal and not publication_evidence_ready
            else "repair_existing"
            if repairing_existing
            else "continue_generation"
        )
    return {
        "contract_version": "storyboard-workspace.v1",
        "snapshot_version": monotonic_snapshot_version(ep["id"], fingerprint),
        "state_fingerprint": fingerprint,
        "state": state,
        "headline": headline,
        "screenplay_available": screenplay_ready,
        "task_phase": phase or None,
        "planned_shots": planned,
        "produced_shots": shot_count,
        "validated_shots": passed,
        # Explicit semantic aliases for new clients.  Keep the v1 fields above
        # for compatibility, but do not force UI copy to guess their meaning.
        "draft_shots": shot_count,
        "safe_checkpoint_shots": passed,
        "pending_revalidation_shots": max(0, shot_count - passed),
        "resume_from_shot": resume_from,
        "resume_mode": resume_mode,
        "final_shot_valid": final_valid,
        "hard_gates_passed": bool(not gate_errors and (full_terminal or confirmed)),
        "hard_gate_issue_count": len(gate_errors),
        "hard_gate_issues": gate_errors[:30],
        "system_error": gate_system_error,
        "feature_flags": feature_flags,
        "confirmed": confirmed,
        "editable": bool(
            screenplay_ready
            and not running
            and not invalid
            and not gate_system_error
            and not feature_flags["safe_readonly"]
        ),
        "confirmable": bool(
            full_terminal
            and publication_evidence_ready
            and not feature_flags["safe_readonly"]
        ),
        "recommended_action": action,
        "write_block_reason": (
            "分镜正在生成或修复，请先暂停" if running
            else gate_system_error
            if gate_system_error
            else "状态组合不安全，请刷新" if invalid or state == "syncing"
            else None
        ),
        "_obsolete_policy_repair": obsolete_policy_repair,
    }


@router.get("/episodes/{episode_id}/storyboard/status")
def storyboard_status(episode_id: str):
    detail = episode_detail(episode_id, view="board")
    return detail["storyboard_status"]


@router.get("/episodes/{episode_id}/storyboard/source")
def storyboard_authorized_source(episode_id: str):
    from app.storyboard_workspace import chapter_sources
    _episode_or_404(episode_id)
    enabled = str(get_setting("storyboard_source_rebind_enabled") or "true").lower() == "true"
    return {
        "episode_id": episode_id,
        "enabled": enabled,
        "chapters": chapter_sources(episode_id) if enabled else [],
        "disabled_reason": None if enabled else "原文重绑定正在灰度回滚；现有证据仍可只读审阅",
    }


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


async def _plan_one_shot(shot_row, *, conn=None, force: bool = False) -> dict:
    """Compatibility entry: resolve one shot from the authoritative episode plan."""
    from app.video_plan import generate_episode_plan

    try:
        episode_id = shot_row["episode_id"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("单镜模式计划缺少 episode_id，不能绕过整集规划") from exc
    plan = await generate_episode_plan(episode_id, force=force, conn=conn)
    item = next((candidate for candidate in plan.shots if candidate.shot_id == shot_row["id"]), None)
    if item is None:
        raise ValueError("整集视频模式计划未覆盖当前镜头")
    return item.model_dump(mode="json")


async def _ensure_shot_mode_plan(conn, shot_id: str, *, force: bool = False) -> None:
    """Ensure the shot is projected from a valid versioned episode plan."""
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        return
    plan_dict = await _plan_one_shot(shot_row, conn=conn, force=force)
    conn.execute("UPDATE shots SET mode_plan=? WHERE id=?",
                 (json.dumps(plan_dict, ensure_ascii=False), shot_id))
    conn.commit()


_MAX_PUBLIC_IMAGE_INPUT_CHARS = 1_000_000


def _public_shot_versions(conn, shot_id: str, *, include_inputs: bool) -> list[dict]:
    if include_inputs:
        rows = conn.execute(
            """SELECT id, shot_id, version_no, prompt_text, status, error,
                      video_path, qa_json, cost_cny, latency_s, artifact_id,
                      adoption_reason, playback_rate, technical_validation_json, created_at,
                      provider_task_id,
                      (SELECT job.attempt_started_at FROM jobs AS job
                        WHERE job.version_id=shot_versions.id
                          AND job.attempt_started_at IS NOT NULL
                          AND job.status NOT IN ('succeeded','failed','cancelled')
                        ORDER BY job.attempt_started_at DESC LIMIT 1) AS running_since,
                      CASE WHEN status='rejected_static_fallback'
                           THEN 1 ELSE 0 END AS delivery_fallback,
                      CASE WHEN length(image_inputs) <= ? THEN image_inputs END AS image_inputs,
                      CASE WHEN length(image_inputs) > ? THEN 1 ELSE 0 END AS image_inputs_omitted
               FROM shot_versions
               WHERE shot_id=? AND status!='cleared'
               ORDER BY version_no DESC""",
            (_MAX_PUBLIC_IMAGE_INPUT_CHARS, _MAX_PUBLIC_IMAGE_INPUT_CHARS, shot_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, shot_id, version_no, '' AS prompt_text, status, error,
                      video_path, qa_json, cost_cny, latency_s, artifact_id,
                      adoption_reason, playback_rate, technical_validation_json, created_at,
                      provider_task_id,
                      (SELECT job.attempt_started_at FROM jobs AS job
                        WHERE job.version_id=shot_versions.id
                          AND job.attempt_started_at IS NOT NULL
                          AND job.status NOT IN ('succeeded','failed','cancelled')
                        ORDER BY job.attempt_started_at DESC LIMIT 1) AS running_since,
                      CASE WHEN status='rejected_static_fallback'
                           THEN 1 ELSE 0 END AS delivery_fallback,
                      NULL AS image_inputs
               FROM shot_versions
               WHERE shot_id=? AND status!='cleared'
               ORDER BY version_no DESC""",
            (shot_id,),
        ).fetchall()
    versions = [
        version for version in rows_to_dicts(rows)
        if not bool(version.pop("delivery_fallback", 0))
    ]
    reference_lineage: dict[str, list[str]] = {}
    if include_inputs:
        for version in versions:
            raw_meta = json.loads(version.get("image_inputs") or "{}")
            for raw_ref in raw_meta.get("reference_images") or []:
                ref_id = raw_ref.get("id") if isinstance(raw_ref, dict) else None
                if ref_id:
                    reference_lineage.setdefault(str(ref_id), []).append(str(version["id"]))
    for version in versions:
        version["qa"] = json.loads(version["qa_json"]) if version["qa_json"] else None
        version.pop("qa_json", None)
        meta = json.loads(version.get("image_inputs") or "{}") if include_inputs else {}
        inputs_omitted = bool(version.pop("image_inputs_omitted", 0))
        boundary_contract = (
            meta.get("boundary_pair_qa")
            if isinstance(meta.get("boundary_pair_qa"), dict)
            else {}
        )
        upstream_video_url = None
        upstream_video_revision = str(
            meta.get("upstream_adopted_video_revision") or ""
        )
        if upstream_video_revision:
            upstream_video = conn.execute(
                """SELECT video_path FROM shot_versions
                   WHERE id=? AND status='succeeded'""",
                (upstream_video_revision,),
            ).fetchone()
            if upstream_video:
                upstream_video_url = _media_url(upstream_video["video_path"])
        refs = [
            _public_reference_image(ref)
            for ref in (meta.get("reference_images") or [])
            if isinstance(ref, dict)
        ]
        for ref in refs:
            ref["referenced_by_version_ids"] = reference_lineage.get(str(ref.get("id")), [])
        version["image_inputs"] = {
            "first_frame_used": bool(meta.get("first_frame_used")),
            "first_frame_src": meta.get("first_frame_src"),
            "first_frame_source": boundary_contract.get("first_frame_source"),
            "first_frame_scene_id": meta.get("first_frame_scene_id"),
            "first_frame_image_url": _media_url(meta.get("first_frame_path")),
            "last_frame_used": bool(meta.get("last_frame_used")),
            "last_frame_src": meta.get("last_frame_src"),
            "last_frame_source": boundary_contract.get("last_frame_source"),
            "last_frame_scene_id": meta.get("last_frame_scene_id"),
            "last_frame_image_url": _media_url(meta.get("last_frame_path")),
            "video_input_url": upstream_video_url or meta.get("video_input_url"),
            "video_input_source_revision_id": upstream_video_revision or None,
            "mode": meta.get("mode"),
            "mode_decision": meta.get("mode_decision"),
            "planned_mode": meta.get("planned_mode"),
            "actual_mode": meta.get("actual_mode"),
            "video_input_intent": meta.get("video_input_intent"),
            "ai_video_prompt_contract_version": meta.get(
                "ai_video_prompt_contract_version"
            ),
            "ai_video_prompt_generated_at": meta.get(
                "ai_video_prompt_generated_at"
            ),
            "required_reference_characters": list(
                meta.get("required_reference_characters") or []
            ),
            "required_interaction_reference_characters": list(
                meta.get("required_interaction_reference_characters") or []
            ),
            "reference_image_used": bool(meta.get("reference_image_used")),
            "reference_images": refs,
            "reference_failure_logs": [
                _public_failure_log(item)
                for item in (meta.get("reference_failure_logs") or [])
                if isinstance(item, dict)
            ],
            "fallback_reason": meta.get("fallback_reason"),
            "retry_reason": meta.get("retry_reason"),
            "omitted_for_size": inputs_omitted,
        }
        if version.get("video_path"):
            version["video_url"] = build_media_url(version["video_path"])
    return versions


@router.get("/episodes/{episode_id}")
def episode_detail(episode_id: str, view: str | None = None):
    """Return episode data shaped for the requesting workspace.

    The legacy/default response remains complete for MCP and API consumers.
    UI workspaces opt into a narrow view so screenplay, storyboard, and cinema
    pages never touch historical media JSON.
    """
    if view not in (None, "script", "board", "wall", "cinema"):
        raise HTTPException(400, f"未知分集视图：{view}")
    if view in (None, "board"):
        from app.storyboard_workspace import reconcile_cancelled_storyboard_run
        reconcile_cancelled_storyboard_run(episode_id)
    with evidence_repository.artifact_read_scope():
        return _episode_detail_projection(episode_id, view)


def _episode_detail_projection(episode_id: str, view: str | None) -> dict:
    """Read-only projection body; the caller owns reconciliation and scoping."""
    full = view is None
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
    screenplay_rebuild_error = None
    try:
        script = _load_screenplay(ep) if full or view in ("script", "board") else None
    except errors.ArtifactNeedsRebuildError as exc:
        if view not in {"script", "board"}:
            raise
        screenplay_rebuild_error = exc
        script = None
    screenplay_payload = (
        script.model_dump() if script and (full or view in ("script", "board")) else None
    )
    if view == "script":
        # 剧本台不读也不改叙事蓝图；不下发它，写回时由服务端从权威补齐。
        screenplay_payload = screenplay_workspace_projection(screenplay_payload)
        ep["screenplay_withheld_fields"] = list(SCREENPLAY_WORKSPACE_WITHHELD_FIELDS)
    ep["screenplay"] = screenplay_payload
    # episode_prep_pack（screenplay 契约 6.0.0+，见 docs/TRANSFORM_FREEZE_PLAN.md）
    # 是与 EpisodeScreenplay 完全不同的形状；script 为 None 且原始 JSON 命中新形状时
    # 走这个专用投影字段，而不是让前端从 ep["screenplay"]=null 里读不到任何内容。
    ep["prep_pack"] = (
        episode_prep_pack_payload(ep)
        if script is None and (full or view in ("script", "board"))
        else None
    )
    ep["scene_options"] = []
    if full or view in ("board", "wall"):
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
        ).fetchone()
        project_bible = _project_bible_or_placeholder(project)
        if full or view == "board":
            ep["scene_options"] = [
                scene.name for scene in (project_bible.scenes or []) if (scene.name or "").strip()
            ]
    if full or view == "script":
        from app.domain.screenplay_ops import (
            _screenplay_authority_state,
        )
    ep.pop("screenplay_required_dialogues", None)
    ep.pop("screenplay_required_dialogue_occurrences", None)
    artifact_id = ep.get("screenplay_artifact_id")
    artifact = (
        evidence_repository.get_artifact(artifact_id)
        if artifact_id and (full or view == "script") else None
    )
    if artifact:
        artifact.pop("content_json", None)
        artifact.pop("content", None)
        artifact["evaluations"] = evidence_repository.get_evaluations(artifact_id)
        if (
            screenplay_rebuild_error is not None
            and artifact.get("id") == screenplay_rebuild_error.artifact_id
        ):
            artifact["stale_code"] = screenplay_rebuild_error.code
    ep["screenplay_evidence"] = artifact
    if full or view == "script":
        from app.production.revision import screenplay_production_state
        ep["screenplay_production"] = screenplay_production_state(episode_id)
    else:
        ep["screenplay_production"] = None
    storyboard_artifact_id = ep.get("storyboard_artifact_id")
    storyboard_artifact = (
        evidence_repository.get_artifact(storyboard_artifact_id)
        if storyboard_artifact_id and (full or view == "board") else None
    )
    if storyboard_artifact:
        storyboard_artifact.pop("content_json", None)
        storyboard_artifact.pop("content", None)
        storyboard_artifact["evaluations"] = evidence_repository.get_evaluations(
            storyboard_artifact_id
        )
    ep["storyboard_evidence"] = storyboard_artifact
    # 页面投影不回传整份 screenplay_json，但 screenplay_state 的权威判定必须看得见它：
    # 先 pop 再判定会让 _screenplay_ready 因「没有页面投影」直接 fail-closed，于是
    # 同一时刻 GET /episodes/{id}?view=script 报 qa_certificate_invalid，
    # 而 GET /episodes/{id}/screenplay/status 报 ready —— 两个端点对同一集给出
    # 互相矛盾的权威状态（剧本台恰好把两者合并展示）。
    screenplay_projection_json = ep.pop("screenplay_json", None)
    # 分镜大纲（先规划后逐镜填充）：透出给前端做 已通过 k / 计划 N 镜 的进度展示
    outline = None
    outline_json_for_gate = (
        ep.get("storyboard_outline_json")
        if full or view == "board"
        else None
    )
    if full or view == "board":
        try:
            outline = json.loads(outline_json_for_gate or "null")
        except (TypeError, ValueError):
            outline = None
    ep.pop("storyboard_outline_json", None)
    ep["storyboard_outline"] = outline
    ep["storyboard_planned_shots"] = len(outline["shots"]) if outline and outline.get("shots") else None
    # Supervisor 运行面板数据（PRD §14.2）
    if full or view == "board":
        from app.storyboard_supervisor import load_latest_checkpoint
        from app.storyboard_control import control_snapshot

        cp = load_latest_checkpoint(episode_id)
        stale_checkpoint_ignored = False
        if (
            cp is not None
            and not _storyboard_checkpoint_matches_screenplay(cp, ep)
            and not (_storyboard_has_material(episode_id, ep) or outline)
        ):
            stale_checkpoint_ignored = True
            cp = None
        if (
            stale_checkpoint_ignored
            and ep.get("script_error")
            == "上游剧本已变更，自动完成授权失效，请重新授权后继续"
        ):
            # The database keeps old checkpoint artifacts as audit evidence after
            # a screenplay publish clears its downstream projection.  Do not leak
            # that historical pause/error into the new screenplay's board view.
            ep["script_error"] = None
        ep["supervisor"] = None
        if cp is not None:
            repair = cp.last_repair or {}
            ep["supervisor"] = {
                "phase": cp.phase,
                "repair_epoch": cp.repair_epoch,
                "lifetime_repair_count": cp.repair_epoch,
                "activation_no": cp.activation_no,
                "activation_attempt_count": cp.activation_attempt_count,
                "activation_attempt_limit": 6,
                "validated_prefix_end": cp.validated_prefix_end,
                "next_shot_no": cp.next_shot_no,
                "expected_total": cp.expected_total or ep["storyboard_planned_shots"] or 0,
                "outcome": cp.outcome,
                "last_repair": repair,
                "strategy": repair.get("strategy"),
                "frontier": repair.get("invalidation_frontier"),
                "issue_codes": repair.get("issue_codes") or [],
                "pending_control": control_snapshot(episode_id),
            }
        try:
            ep["active_storyboard_run_id"] = ep.get("active_storyboard_run_id")
        except Exception:  # noqa: BLE001
            ep["active_storyboard_run_id"] = None
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"])
    ep["shot_count"] = shot_count
    if full or view == "script":
        ep["screenplay_state"] = _screenplay_authority_state(
            {**ep, "screenplay_json": screenplay_projection_json},
            shot_count=shot_count,
            production=ep.get("screenplay_production"),
            rebuild_error=screenplay_rebuild_error,
        )
    else:
        ep["screenplay_state"] = None
    if view in ("script", "cinema"):
        ep["shots"] = []
        ep["pipeline_summary"] = None
        return ep

    shot_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    # 冷观众审读驱动的叙事指标面板（NarrativeReadinessPanel）已随观众深读/
    # 校准校验功能一起下线（用户拍板）：这里曾经的 compute_narrative_metrics
    # 调用只在 script.narrative_plan is not None 时才执行，对 prep_pack
    # （契约 6.0.0+）分集永远是 None，本就是死分支，不留兼容。
    ep["narrative_metrics"] = None
    # 预估只按模型选择的实际分镜时长累计；单集不设总时长产品上限。
    ep["cost_cny"] = worker.episode_cost(episode_id)
    ep["cost_limit_cny"] = float(get_setting("episode_cost_limit_cny") or 100)
    shots = rows_to_dicts(shot_rows)
    version_counts = {}
    if view == "board" and shots:
        count_rows = conn.execute(
            """SELECT v.shot_id, COUNT(*) AS version_count
               FROM shot_versions v
               JOIN shots s ON s.id=v.shot_id
               WHERE s.episode_id=? GROUP BY v.shot_id""",
            (episode_id,),
        ).fetchall()
        version_counts = {row["shot_id"]: int(row["version_count"]) for row in count_rows}
    pipeline_statuses = {}
    pipeline_summary = None
    if full or view == "wall":
        try:
            from app.media_pipeline.status import episode_pipeline_statuses
            pipeline_statuses, pipeline_summary = episode_pipeline_statuses(episode_id, conn=conn)
        except Exception:  # noqa: BLE001
            pipeline_statuses, pipeline_summary = {}, None
    for s in shots:
        s["characters"] = json.loads(s["characters"] or "[]")
        s["dialogues"] = json.loads(s["dialogues"] or "[]")
        _apply_contract_to_public_shot(s)
        from app.continuity import information_items_for_shot
        s["new_information_items"] = information_items_for_shot(s, script)
        from app.video_cost_model import initial_shot_generation_cost

        s["est_cost_cny"] = initial_shot_generation_cost(s["duration_s"])
        if s.get("storyboard_artifact_id") and (full or view == "board"):
            shot_artifact = evidence_repository.get_artifact(s["storyboard_artifact_id"])
            if shot_artifact:
                shot_artifact.pop("content_json", None)
                shot_artifact.pop("content", None)
                shot_artifact["evaluations"] = evidence_repository.get_evaluations(
                    s["storyboard_artifact_id"]
                )
            s["storyboard_evidence"] = shot_artifact
        else:
            s["storyboard_evidence"] = None
        if full or view == "board":
            from app.storyboard_workspace import source_binding_for_shot
            s["source_binding"] = source_binding_for_shot(s["id"])
        # mode_plan 存的是 JSON 文本，解析成对象供前端只读展示模型决策
        try:
            s["mode_plan"] = json.loads(s["mode_plan"]) if s.get("mode_plan") else None
        except (TypeError, ValueError):
            s["mode_plan"] = None
        # 新链路只使用参考图；旧关键帧字段仅保留在数据库中做历史兼容，不再对外暴露或参与状态判断。
        for legacy_key in (
            "approved_scene_id", "approved_head_scene_id", "approved_tail_scene_id", "scene_status",
        ):
            s.pop(legacy_key, None)
        s["video_stale"] = _shot_video_is_stale(conn, s, ep.get("storyboard_artifact_id"))
        if view == "board":
            s["version_count"] = version_counts.get(s["id"], 0)
            s["versions"] = []
            s["pipeline"] = None
            continue

        s["versions"] = _public_shot_versions(conn, s["id"], include_inputs=full)
        if s.get("adopted_version_id") and not any(
            version["id"] == s["adopted_version_id"] for version in s["versions"]
        ):
            s["delivery_fallback_active"] = True
            s["adopted_version_id"] = None
        s["pipeline"] = pipeline_statuses.get(s["id"])
        s["video_status"] = (
            s["pipeline"].get("video_status") if s["pipeline"] else None
        )
        # 透出 grade / fallback，供生成台 A/B 分色
        try:
            from app.evidence.media import grade_shot_video
            graded = grade_shot_video(s["id"])
            s["video_grade"] = graded.get("grade")
            s["fallback_reason"] = graded.get("fallback_reason")
            s["continuity_degraded"] = bool(graded.get("continuity_degraded"))
        except Exception:  # noqa: BLE001
            s["video_grade"] = None
            s["fallback_reason"] = None
            s["continuity_degraded"] = False
    ep["shots"] = shots
    if full or view == "board":
        status_episode = {
            **ep,
            # The public response omits the raw JSON, but the shared full gate
            # must still receive the approved outline readability windows.
            "storyboard_outline_json": outline_json_for_gate,
        }
        ep["storyboard_status"] = _storyboard_status_snapshot(
            status_episode,
            shots,
            ep.get("supervisor"),
            script,
            screenplay_rebuild_error,
        )
        if ep["storyboard_status"].pop("_obsolete_policy_repair", False):
            ep["script_error"] = None
        # 任务计时以服务端 run 为准：localStorage 起点在运行中刷新后会永久搁浅，
        # 下一个任务复用旧起点会显示出「已等待 1244 分」这类虚高时长。
        # 不走 active_storyboard_run_id：该指针在任务结束时被清空，取最近一次 run
        # 才能在完成后继续显示「本次耗时」。
        ep["storyboard_status"].update(
            {
                f"task_{key}": value
                for key, value in evidence_repository.latest_run_timing(
                    workflow_type="storyboard",
                    scope_type="episode",
                    scope_id=episode_id,
                    conn=conn,
                ).items()
            }
        )
        # 逐镜耗时（累计全部重试迭代），按 shot_no 归集。
        ep["shot_timings"] = evidence_repository.storyboard_shot_timings(
            episode_id=episode_id,
            conn=conn,
        )
    ep["pipeline_summary"] = pipeline_summary
    # 视频补齐 Supervisor 面板（生成台）
    if full or view == "wall":
        # 整集视频生成的总计时；单条视频的耗时随 version 一起下发。
        ep["video_task_timing"] = evidence_repository.latest_run_timing(
            workflow_type="episode_video_completion",
            scope_type="episode",
            scope_id=episode_id,
            conn=conn,
        )
        try:
            from app.completion_grant import (
                episode_video_completion_budget_requirement,
            )
            ep["video_budget"] = episode_video_completion_budget_requirement(
                episode_id,
                conn=conn,
            )
        except Exception:  # noqa: BLE001
            ep["video_budget"] = None
        try:
            from app.video_supervisor import load_latest_checkpoint, public_checkpoint_projection
            vcp = load_latest_checkpoint(episode_id)
            ep["video_supervisor"] = public_checkpoint_projection(vcp)
            try:
                ep["active_video_run_id"] = ep.get("active_video_run_id")
                ep["video_completion_mode"] = ep.get("video_completion_mode") or "quick"
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            ep["video_supervisor"] = None
    return ep


@router.get("/shots/{shot_id}/review")
def shot_review_detail(shot_id: str):
    """Load the expensive review gallery for one selected shot only."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "镜头不存在")
    shot = dict(row)
    shot["characters"] = json.loads(shot["characters"] or "[]")
    shot["dialogues"] = json.loads(shot["dialogues"] or "[]")
    _apply_contract_to_public_shot(shot)
    from app.continuity import information_items_for_shot
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (shot["episode_id"],)
    ).fetchone()
    screenplay = None
    if episode_row is not None:
        from app.production.screenplay_authority import (
            episode_requires_immutable_screenplay_authority,
            resolve_downstream_screenplay,
        )

        try:
            screenplay = resolve_downstream_screenplay(
                str(episode_row["id"]), conn=conn,
            ).screenplay
        except ValueError as exc:
            if episode_requires_immutable_screenplay_authority(
                episode_row, conn=conn,
            ):
                published_id = str(
                    episode_row["published_screenplay_artifact_id"] or ""
                )
                projected_id = str(episode_row["screenplay_artifact_id"] or "")
                if not published_id or published_id != projected_id:
                    raise HTTPException(
                        409, f"当前剧本权威链无法验证，不能展示评审详情：{exc}",
                    ) from exc
                try:
                    from app.production.patch import load_screenplay_from_artifact

                    screenplay = load_screenplay_from_artifact(published_id)
                except Exception as artifact_exc:
                    raise HTTPException(
                        409,
                        "当前剧本权威链无法验证，且已发布剧本 Artifact 不可读取："
                        f"{artifact_exc}",
                    ) from artifact_exc
            # Explicit plan-null legacy rows keep their historical review
            # behavior; they do not have an immutable authority contract.
            else:
                screenplay = _load_screenplay(dict(episode_row))
    shot["new_information_items"] = information_items_for_shot(shot, screenplay)
    from app.video_cost_model import initial_shot_generation_cost

    shot["est_cost_cny"] = initial_shot_generation_cost(shot["duration_s"])
    shot["video_stale"] = _shot_video_is_stale(
        conn, shot, episode_row["storyboard_artifact_id"] if episode_row else None
    )
    for legacy_key in (
        "approved_scene_id", "approved_head_scene_id", "approved_tail_scene_id", "scene_status",
    ):
        shot.pop(legacy_key, None)
    try:
        shot["mode_plan"] = json.loads(shot["mode_plan"]) if shot.get("mode_plan") else None
    except (TypeError, ValueError):
        shot["mode_plan"] = None
    shot["storyboard_evidence"] = None
    shot["versions"] = _public_shot_versions(conn, shot_id, include_inputs=True)
    if shot.get("adopted_version_id") and not any(
        version["id"] == shot["adopted_version_id"] for version in shot["versions"]
    ):
        shot["delivery_fallback_active"] = True
        shot["adopted_version_id"] = None
    try:
        from app.media_pipeline.status import shot_pipeline_status
        shot["pipeline"] = shot_pipeline_status(shot_id, conn=conn)
    except Exception:  # noqa: BLE001
        shot["pipeline"] = None
    shot["video_status"] = (
        shot["pipeline"].get("video_status") if shot["pipeline"] else None
    )
    try:
        from app.evidence.media import grade_shot_video
        graded = grade_shot_video(shot_id)
        shot["video_grade"] = graded.get("grade")
        shot["fallback_reason"] = graded.get("fallback_reason")
        shot["continuity_degraded"] = bool(graded.get("continuity_degraded"))
    except Exception:  # noqa: BLE001
        shot["video_grade"] = None
        shot["fallback_reason"] = None
        shot["continuity_degraded"] = False
    return shot


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


def _storyboard_residual_hint(residual: list[str]) -> str:
    """Return an actionable repair hint for the current validation failures."""
    text = "；".join(residual)
    hints: list[str] = []
    if "口播上限" in text or "念不完" in text:
        hints.append("请在本镜台词区精简文案，或使用“在当前镜后新增”分担台词")
    if "角色圣经中不存在" in text or "既不在角色圣经" in text or "圣经角色为" in text:
        hints.append("请在本镜“画面角色”选择器中改选人物谱已有角色")
    if "未落实本镜大纲 covers" in text or "只停留在大纲" in text:
        hints.append("请在本镜“画面与动作”或“台词”中写出该剧情事实")
    if not hints:
        hints.append("请定位问题镜继续修改；如需自动处理，可在任务详情中选择继续生成或转人工")
    return "；".join(hints)


def _storyboard_loop_exit_text(exit_reason: str) -> str:
    """Translate the actual AgentLoop exit reason without misreporting exhaustion."""
    return {
        "max_iterations": "已达到重试上限",
        "no_quality_gain": "连续修复无质量提升，修复循环已停止",
        "stalled": "连续输出相同问题，修复循环已停止",
    }.get(exit_reason, "修复循环未通过")


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

__all__ = [name for name in globals() if not name.startswith("__")]
