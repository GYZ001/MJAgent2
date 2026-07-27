from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

try:
    _board_from_shot_rows
except NameError:  # pragma: no cover - direct module import
    from app.domain.storyboard_ops import _board_from_shot_rows

def _shot_contract_json(shot: Shot) -> str:
    from app.continuity import shot_contract_dict
    return json.dumps(shot_contract_dict(shot), ensure_ascii=False)


def _uses_previous_tail_frame_for_model(shot: Shot, prev: Shot | None = None) -> bool:
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    return uses_previous_tail_frame(derive_continuity_mode(shot, prev))


class ConfirmationEvaluation:
    """只读确认评估结果；不写数据库。"""

    __slots__ = ("passed", "errors", "issues", "board", "compact_target", "estimated_cost_cny")

    def __init__(
        self,
        *,
        passed: bool,
        errors: list[str],
        issues: list,
        board: Storyboard,
        compact_target: int,
        estimated_cost_cny: float,
    ):
        self.passed = passed
        self.errors = errors
        self.issues = issues
        self.board = board
        self.compact_target = compact_target
        self.estimated_cost_cny = estimated_cost_cny


def _is_storyboard_terminal_for_confirmation(
    episode,
    checkpoint,
    *,
    shot_count: int,
    planned_shots: int,
    final_shot_valid: bool,
    automated: bool,
) -> bool:
    """Allow the supervisor's internal confirmation phase without weakening manual confirmation."""
    if shot_count <= 0 or shot_count != planned_shots or not final_shot_valid:
        return False
    if checkpoint is not None:
        phase = str(getattr(checkpoint, "phase", "") or "")
        validated = int(getattr(checkpoint, "validated_prefix_end", 0) or 0)
        expected = int(getattr(checkpoint, "expected_total", 0) or planned_shots)
        checkpoint_complete = bool(
            phase in {"PREPARING_CONFIRM", "CONFIRMING", "SUCCEEDED"}
            and validated == shot_count
            and expected == shot_count
        )
        if automated and checkpoint_complete:
            return True
        # A stopped internal confirmation may be completed manually once the episode
        # is no longer being written. The full confirmation evaluation still runs below.
        if episode["status"] == "scripted" and checkpoint_complete:
            return True
    return bool(
        episode["status"] == "scripted"
        and not episode["script_error"]
        and checkpoint is None
    )


def evaluate_storyboard_for_confirmation(
    episode,
    storyboard: Storyboard,
    screenplay: EpisodeScreenplay | None,
    bible: Bible,
    *,
    has_real_bible: bool = True,
    target_duration_s: int | None = None,
    record_metrics: bool = True,
) -> ConfirmationEvaluation:
    """与 confirm_episode_core 同源的只读确认评估（不写库）。

    Supervisor 与确认门必须共用此函数，避免「Supervisor 认为通过、确认门又用另一套规则失败」。
    """
    from app.evaluations.issues import issues_from_messages
    from app.validators import prefer_default_shot_durations

    board = Storyboard(episode_no=storyboard.episode_no, shots=list(storyboard.shots))
    normalize_offbible_characters(board, bible)
    normalize_continuity(board)
    prefer_default_shot_durations(board)
    normalize_transition_visuals(board)
    compact_target = _compact_episode_target(
        target_duration_s if target_duration_s is not None else episode["target_duration_s"]
    )
    actual_total = sum(int(s.duration_s or 0) for s in board.shots)
    compact_target = _compact_episode_target(actual_total or compact_target)

    errors = validate_storyboard(board, bible, compact_target)
    if screenplay is not None:
        errors.extend(validate_storyboard_soundtrack(board, screenplay, compact_target))
        errors.extend(validate_storyboard_preserves_key_content(board, screenplay))
    if has_real_bible and not errors:
        try:
            for s in board.shots:
                compile_prompt(s, bible)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Prompt 编译失败：{exc}")
    try:
        ep_id = episode["id"]
    except Exception:  # noqa: BLE001
        ep_id = getattr(episode, "id", "") or ""
    # VAL-422 可观测性：确认门才首次发现的容量/口播冲突（理想应为 0）。
    if record_metrics:
        try:
            from app.observability.metrics import inc
            for err in errors:
                if "口播上限" in err or "台词纯文字" in err:
                    inc("confirm_first_seen_capacity_error_total", episode_id=ep_id)
                if "分叉" in err or "SPOKEN_CONTRACT" in err or "口播合同" in err:
                    inc("confirm_first_seen_spoken_conflict_total", episode_id=ep_id)
        except Exception:  # noqa: BLE001
            pass
    issues = issues_from_messages(errors, subject=f"episode:{ep_id}")
    est = sum(shot_cost_cny(s.duration_s) for s in board.shots)
    return ConfirmationEvaluation(
        passed=not errors,
        errors=errors,
        issues=issues,
        board=board,
        compact_target=compact_target,
        estimated_cost_cny=round(est, 2),
    )


def create_storyboard_confirmation_preview(episode_id: str, *, automated: bool = False) -> dict:
    """计算并签发确认快照；提交与自动确认均消费同一契约。"""
    from app.storyboard_supervisor import load_latest_checkpoint
    from app.storyboard_workspace import create_preview, verify_or_bind_existing_excerpt

    ep = _episode_or_404(episode_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "本集还没有分镜")
    cp = load_latest_checkpoint(episode_id)
    outline_count = 0
    try:
        outline_count = len(json.loads(ep["storyboard_outline_json"] or "{}").get("shots") or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        outline_count = 0
    # 结构调整会撤销整集 artifact 并重写大纲；此时旧 checkpoint 仅是历史生成记录，
    # 确认门必须以用户刚批准的新结构为准。
    structural_draft = ep["storyboard_artifact_id"] is None and outline_count > 0
    planned = int(
        (outline_count if structural_draft else 0)
        or (cp.expected_total if cp else 0)
        or outline_count
        or len(rows)
    )
    board = _board_from_shot_rows(rows, ep["episode_no"])
    final_valid = bool(board.shots and board.shots[-1].is_final)
    terminal = _is_storyboard_terminal_for_confirmation(
        ep,
        cp,
        shot_count=len(rows),
        planned_shots=planned,
        final_shot_valid=final_valid,
        automated=automated,
    )
    evidence_errors: list[str] = []
    for row in rows:
        try:
            verify_or_bind_existing_excerpt(
                episode_id, row["id"], row["source_excerpt"] or "",
            )
        except HTTPException as exc:
            detail = exc.detail
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            evidence_errors.append(f"第 {row['shot_no']} 镜：{message}")
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = _project_bible_or_placeholder(project)
    screenplay = _load_screenplay(ep)
    evaluation = evaluate_storyboard_for_confirmation(
        ep, board, screenplay, bible,
        has_real_bible=bool((project["bible_json"] or "").strip()) if project else False,
    )
    hard_errors = list(evaluation.errors) + evidence_errors
    if not terminal:
        hard_errors.insert(
            0,
            f"分镜尚未达到完整终态：已完成 {len(rows)}/{planned} 镜，最终镜{'有效' if final_valid else '缺失'}",
        )
    warnings: list[str] = []
    if any(int(shot.duration_s or 0) > 5 for shot in evaluation.board.shots):
        warnings.append("存在超过 5 秒的镜头，已纳入全量 AI/确定性门禁")
    payload = {
        "contract_version": "storyboard-confirm.v1",
        "episode_id": episode_id,
        "storyboard_artifact_id": ep["storyboard_artifact_id"],
        "shot_count": len(rows),
        "planned_shots": planned,
        "total_duration_s": sum(int(shot.duration_s or 0) for shot in evaluation.board.shots),
        "final_shot_valid": final_valid,
        "hard_gates": {
            "passed": terminal and evaluation.passed and not evidence_errors,
            "errors": hard_errors,
        },
        "warnings": warnings,
        "estimated_video_cost_cny": {
            "min": evaluation.estimated_cost_cny,
            "max": evaluation.estimated_cost_cny,
            "note": "按当前服务端费率估算；确认不会自动提交付费视频",
        },
        "unlocks": ["评审墙", "付费视频生成入口"],
    }
    if not payload["hard_gates"]["passed"]:
        try:
            from app.observability.metrics import inc
            inc("storyboard_confirm_preview_total", episode_id=episode_id, passed=False)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(409, {
            "code": "STORYBOARD_NOT_CONFIRMABLE",
            "message": "分镜尚未通过确认门禁",
            **payload,
        })
    # 硬门禁错误比人工警告更可操作：若两者同时存在，必须先返回完整的
    # STORYBOARD_NOT_CONFIRMABLE 证据，不能被「转人工」的泛化警告遮蔽。
    if automated and warnings:
        raise HTTPException(409, "自动确认遇到需要人工判断的警告，请转人工确认")
    try:
        from app.observability.metrics import inc
        inc("storyboard_confirm_preview_total", episode_id=episode_id, passed=True)
    except Exception:  # noqa: BLE001
        pass
    return create_preview("confirm", episode_id, payload)


@router.post("/episodes/{episode_id}/confirm-preview")
def confirm_episode_preview(episode_id: str):
    return create_storyboard_confirmation_preview(episode_id)


def _converge_confirmed_storyboard_state(
    episode_id: str,
    *,
    active_storyboard_run_id: str | None,
    decided_by: str,
) -> None:
    """把已确认的业务结果投影到 Supervisor/运行指针/授权状态。

    该收口同时用于首次确认与幂等重试，因此能自愈旧版留下的「已确认但
    active_storyboard_run_id 仍存在」状态。
    """
    conn = get_conn()
    conn.execute(
        "UPDATE episodes SET script_error=NULL, active_storyboard_run_id=NULL WHERE id=?",
        (episode_id,),
    )
    conn.commit()
    try:
        from app.storyboard_supervisor import load_latest_checkpoint, save_checkpoint

        checkpoint = load_latest_checkpoint(episode_id)
        if checkpoint is not None and (
            checkpoint.phase != "SUCCEEDED" or checkpoint.outcome != "SUCCEEDED_CONFIRMED"
        ):
            checkpoint.phase = "SUCCEEDED"
            checkpoint.outcome = "SUCCEEDED_CONFIRMED"
            save_checkpoint(checkpoint, run_id=active_storyboard_run_id)
    except Exception:  # noqa: BLE001 -- 业务确认已完成，辅助投影可在下次重试自愈
        pass
    if decided_by != "supervisor":
        try:
            from app.completion_grant import revoke_active_grants_for_episode

            revoke_active_grants_for_episode(episode_id)
        except Exception:  # noqa: BLE001 -- 不回滚已通过门禁的确认
            pass


def confirm_episode_core(
    episode_id: str,
    *,
    decided_by: str = "user",
    reason: str | None = None,
    preview_token: str | None = None,
) -> dict:
    """人工/自动确认门：全量业务校验通过才进入 confirmed。
    失败抛 ValueError（消息面向 UI）；供路由与 Supervisor 复用。
    """
    from app.storyboard_workspace import consume_preview, require_preview

    already = _episode_or_404(episode_id)
    if already["status"] == "confirmed":
        _converge_confirmed_storyboard_state(
            episode_id,
            active_storyboard_run_id=already["active_storyboard_run_id"],
            decided_by=decided_by,
        )
        shots = get_conn().execute(
            "SELECT duration_s FROM shots WHERE episode_id=?", (episode_id,),
        ).fetchall()
        return {
            "confirmed": True,
            "idempotent": True,
            "shot_count": len(shots),
            "total_duration_s": sum(int(row["duration_s"] or 0) for row in shots),
        }
    preview = require_preview(preview_token, "confirm", episode_id)
    if not (preview.get("hard_gates") or {}).get("passed"):
        raise ValueError("确认预览未通过完整性与业务门禁")
    ep = _episode_or_404(episode_id)
    conn = get_conn()
    compact_target = _compact_episode_target(ep["target_duration_s"])
    if compact_target != ep["target_duration_s"]:
        conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
        conn.commit()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    has_real_bible = bool((p["bible_json"] or "").strip())
    bible = _project_bible_or_placeholder(p)
    shots_rows = conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    if not shots_rows:
        raise ValueError("本集还没有分镜脚本")
    board = _board_from_shot_rows(shots_rows, ep["episode_no"])
    shots = board.shots
    character_changes = normalize_offbible_characters(board, bible)
    character_artifact_ids = _persist_storyboard_character_policy_repairs(
        conn, episode_id, board, character_changes
    )
    before = [
        (
            s.continuity_from_prev, s.transition, s.duration_s, s.shot_size, s.camera_move,
            s.continuity_mode, s.observed_state_out,
            (r["shot_contract_json"] if "shot_contract_json" in r.keys() else "") or "",
        )
        for r, s in zip(shots_rows, shots)
    ]
    normalize_continuity(board)
    from app.validators import prefer_default_shot_durations
    prefer_default_shot_durations(board)
    normalize_transition_visuals(board)
    actual_total = sum(int(s.duration_s or 0) for s in shots)
    synced_target = _compact_episode_target(actual_total or compact_target)
    if synced_target != compact_target:
        compact_target = synced_target
        conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
    normalized_fields_changed = False
    for r, s, (old_cont, old_trans, old_dur, old_size, old_move, old_mode, old_observed, old_contract) in zip(shots_rows, shots, before):
        if (old_cont != s.continuity_from_prev or old_trans != s.transition or old_dur != s.duration_s
                or old_size != s.shot_size or old_move != s.camera_move
                or old_mode != s.continuity_mode or old_observed != s.observed_state_out
                or old_contract != _shot_contract_json(s)
                or (r["last_frame_desc"] or "") != s.last_frame_desc):
            normalized_fields_changed = True
            conn.execute(
                "UPDATE shots SET continuity_from_prev=?, transition=?, duration_s=?, shot_size=?, camera_move=?, last_frame_desc=?, shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE id=?",
                (int(s.continuity_from_prev), s.transition, s.duration_s, s.shot_size, s.camera_move,
                 s.last_frame_desc, _shot_contract_json(s), s.continuity_mode, s.observed_state_out, r["id"]))
    conn.commit()

    screenplay = _load_screenplay(ep)
    # 重新从当前（可能已归一）镜头构建 board，再跑同源只读评估。
    board = _board_from_shot_rows(
        conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall(),
        ep["episode_no"],
    )
    evaluation = evaluate_storyboard_for_confirmation(
        ep, board, screenplay, bible,
        has_real_bible=has_real_bible,
        target_duration_s=compact_target,
    )
    if not evaluation.passed:
        raise ValueError(json.dumps(evaluation.errors, ensure_ascii=False))
    board = evaluation.board
    compact_target = evaluation.compact_target
    est = evaluation.estimated_cost_cny
    shots = board.shots

    # 幂等：已 confirmed 且 artifact hash 相同 → 直接成功；hash 不同则拒绝覆盖。
    storyboard_artifact_id = ep["storyboard_artifact_id"]
    content_hash = None
    if character_artifact_ids or normalized_fields_changed or not storyboard_artifact_id:
        storyboard_artifact_id = _finalize_storyboard_evidence(episode_id, board)
    if storyboard_artifact_id:
        art = conn.execute(
            "SELECT content_hash FROM artifacts WHERE id=?", (storyboard_artifact_id,)
        ).fetchone()
        content_hash = art["content_hash"] if art else None

    if ep["status"] == "confirmed":
        if storyboard_artifact_id and content_hash:
            existing_gate = conn.execute(
                "SELECT id FROM gate_decisions WHERE artifact_id=? AND gate_key='storyboard' AND decision='approve'",
                (storyboard_artifact_id,),
            ).fetchone()
            if existing_gate:
                return {
                    "confirmed": True,
                    "idempotent": True,
                    "estimated_cost_cny": est,
                    "shot_count": len(shots),
                    "total_duration_s": sum(s.duration_s for s in shots),
                    "target_duration_s": compact_target,
                }
        raise ValueError(
            "本集已确认但分镜内容已变化；禁止覆盖已确认分镜，请先撤销确认或新建修订"
        )

    idempotency_key = f"{episode_id}:{content_hash or storyboard_artifact_id or 'none'}"
    existing = conn.execute(
        "SELECT id FROM gate_decisions WHERE artifact_id=? AND gate_key='storyboard' AND decision='approve'",
        (storyboard_artifact_id,),
    ).fetchone() if storyboard_artifact_id else None
    if storyboard_artifact_id and not existing:
        human_eval = Evaluation(
            evaluator_type="human",
            evaluator_name="storyboard_reviewer",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={
                "decision": "approve",
                "shot_count": len(shots),
                "decided_by": decided_by,
                "idempotency_key": idempotency_key,
            },
        )
        evidence_repository.commit_artifact(None, storyboard_artifact_id, [human_eval])
        conn.execute(
            """INSERT INTO gate_decisions(
                   id, artifact_id, gate_key, decision, decided_by, reason, created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                new_id("gate"), storyboard_artifact_id, "storyboard", "approve", decided_by,
                reason or "分镜全量确定性校验通过并确认", now(),
            ),
        )
    active_storyboard_run_id = ep["active_storyboard_run_id"]
    conn.execute(
        "UPDATE episodes SET status='confirmed', script_error=NULL, "
        "active_storyboard_run_id=NULL WHERE id=?",
        (episode_id,),
    )
    conn.commit()
    consume_preview(str(preview_token))
    # 手动确认是 Supervisor 「已就绪待确认」状态的真正终点。
    _converge_confirmed_storyboard_state(
        episode_id,
        active_storyboard_run_id=active_storyboard_run_id,
        decided_by=decided_by,
    )
    try:
        from app.observability.metrics import inc
        inc("storyboard_confirm_submit_total", episode_id=episode_id, passed=True, decided_by=decided_by)
    except Exception:  # noqa: BLE001
        pass
    return {
        "confirmed": True,
        "estimated_cost_cny": est,
        "shot_count": len(shots),
        "total_duration_s": sum(s.duration_s for s in shots),
        "target_duration_s": compact_target,
        "idempotency_key": idempotency_key,
    }


@router.post("/episodes/{episode_id}/confirm")
async def confirm_episode(episode_id: str, body: dict | None = Body(None)):
    """运行确定性确认门；校验通过后把剧集推进到 confirmed。"""
    from app.capabilities.dispatch import dispatch, respond_ui

    body = _as_body_dict(body)
    result = await dispatch(
        "storyboard.confirm",
        {"episode_id": episode_id, "preview_token": body.get("preview_token")},
        initiator="ui",
    )
    return respond_ui(result)


@router.post("/episodes/{episode_id}/clear-artifacts")
async def clear_episode_artifacts(episode_id: str):
    """清空整集所有镜头的参考图、视频与模型分析，并回退到「已确认」。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    snapshot = _review_upstream_snapshot(episode_id)
    if snapshot["active_upstream_runs"]:
        raise HTTPException(409, {
            "code": "UPSTREAM_RUN_ACTIVE",
            "message": "编剧或分镜任务仍在写入，不能普通清空；请先停止上游任务",
            "active_runs": snapshot["active_upstream_runs"],
        })
    await reset_video_completion_state(episode_id, reason="CLEARED")
    try:
        result = worker.clear_episode_artifacts(episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _review_write_audit("artifacts.clear_episode", "episode", episode_id, new_state=result)
    return result


async def reset_video_completion_state(episode_id: str, *, reason: str = "CANCELLED") -> dict:
    """停止全片补齐 Supervisor，并把集级补齐状态复位，避免评审墙死锁。"""
    from app import task_registry
    from app.completion_grant import revoke_grant
    from app.video_control import request_control
    from app.video_supervisor import load_latest_checkpoint, save_checkpoint

    _ensure_video_episode_columns()
    cancelled = await task_registry.cancel_and_wait("video_completion", episode_id)
    try:
        request_control(episode_id, "clear")
    except Exception:  # noqa: BLE001
        pass
    cp = load_latest_checkpoint(episode_id)
    if cp:
        if cp.grant_id:
            try:
                revoke_grant(cp.grant_id)
            except Exception:  # noqa: BLE001
                pass
        if cp.phase not in {"SUCCEEDED_COVERED", "CANCELLED"}:
            cp.phase = "CANCELLED"
            cp.outcome = reason
            save_checkpoint(cp, run_id=cp.run_id)
    conn = get_conn()
    conn.execute(
        """UPDATE episodes
           SET video_completion_mode='quick',
               active_video_run_id=NULL,
               video_control_json=NULL,
               status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
           WHERE id=?""",
        (episode_id,),
    )
    conn.commit()
    return {"episode_id": episode_id, "cancelled_task": bool(cancelled), "reason": reason}


@router.post("/episodes/{episode_id}/video-completion/reset")
async def reset_video_completion(episode_id: str):
    """强制结束补齐 Supervisor 并复位面板状态（不清空已有视频文件）。"""
    _episode_or_404(episode_id)
    return await reset_video_completion_state(episode_id, reason="RESET")


@router.post("/shots/{shot_id}/clear-artifacts")
async def clear_shot_artifacts(shot_id: str):
    """清空单个镜头的参考图、视频与模型分析。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_shot", {"shot_id": shot_id})
    if routed is not None:
        return routed
    conn = get_conn()
    shot = conn.execute("SELECT id, episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    snapshot = _review_upstream_snapshot(shot["episode_id"])
    if snapshot["active_upstream_runs"]:
        raise HTTPException(409, {
            "code": "UPSTREAM_RUN_ACTIVE",
            "message": "编剧或分镜任务仍在写入，不能普通清空",
            "active_runs": snapshot["active_upstream_runs"],
        })
    try:
        result = worker.clear_shot_artifacts(shot_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _review_write_audit("artifacts.clear_shot", "shot", shot_id, new_state=result)
    return result


@router.delete("/versions/{version_id}")
async def delete_version(version_id: str):
    """删除一个已生成的视频版本（含文件）。若是采用版则清空采用、使本集成品失效。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.delete_version", {"version_id": version_id})
    if routed is not None:
        return routed
    conn = get_conn()
    v = conn.execute(
        """SELECT v.id, v.shot_id, s.adopted_version_id,
                  EXISTS(SELECT 1 FROM jobs j WHERE j.version_id=v.id AND j.status IN ('queued','running','waiting_provider')) AS active_job
             FROM shot_versions v JOIN shots s ON s.id=v.shot_id WHERE v.id=?""",
        (version_id,),
    ).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    if v["adopted_version_id"] == version_id:
        raise HTTPException(409, "当前采用版受保护，请先采用其他版本")
    if v["active_job"]:
        raise HTTPException(409, "该版本仍在生成且被任务依赖，请先停止任务")
    shot_id = worker.delete_video_version(version_id)
    _review_write_audit("video_version.delete", "version", version_id, old_state=dict(v))
    return {"deleted": version_id, "shot_id": shot_id}


def _set_reference_image_used(
    version_id: str, ref_id: str, *, use: bool, override_reason: str | None = None,
) -> dict:
    """素材画廊里把某张参考图标记为「废弃」或「恢复使用」。
    废弃后该图不再喂给视频模型（见 video_modes.build_seedance_image_inputs），仅留作展示。"""
    conn = get_conn()
    v = conn.execute("SELECT image_inputs FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    meta = json.loads(v["image_inputs"] or "{}")
    refs = meta.get("reference_images") or []
    target = next((r for r in refs if r.get("id") == ref_id), None)
    if target is None:
        raise HTTPException(404, "参考图不存在")
    if use:
        _review_assert_reference_restore(version_id, ref_id)
    if use and target.get("rejectReason") and not (override_reason or "").strip():
        raise HTTPException(400, "恢复质检淘汰的参考图必须填写覆盖理由")
    changed = target.get("deleted") != (not use) or target.get("selectedForSeedance") != use
    target["deleted"] = not use
    target["selectedForSeedance"] = use
    if use and (override_reason or "").strip():
        target["restoreOverrideReason"] = override_reason.strip()
        target["restoredAt"] = now()
        changed = True
    meta["reference_images"] = refs
    if changed:
        meta["reference_gallery_revision"] = now()
        meta["reference_gallery_edited"] = True
    conn.execute("UPDATE shot_versions SET image_inputs=? WHERE id=?",
                 (json.dumps(meta, ensure_ascii=False), version_id))
    conn.commit()
    _review_write_audit(
        "reference.restore" if use else "reference.discard",
        "version", version_id, target_version=str(meta.get("reference_gallery_revision") or ""),
        old_state={"ref_id": ref_id, "deleted": not target.get("deleted")},
        new_state={"ref_id": ref_id, "deleted": not use}, reason=override_reason,
    )
    return {
        "version_id": version_id,
        "ref_id": ref_id,
        "deleted": not use,
        "override_reason": (override_reason or "").strip() or None,
    }


@router.delete("/versions/{version_id}/reference-images/{ref_id}")
async def discard_reference_image(version_id: str, ref_id: str):
    """废弃一张参考图：移入废弃画廊，且后续调用视频模型时不再使用它。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "reference.review",
        {"version_id": version_id, "ref_id": ref_id, "action": "discard"},
    )
    if routed is not None:
        return routed
    return _set_reference_image_used(version_id, ref_id, use=False)


@router.post("/versions/{version_id}/reference-images/{ref_id}/restore")
async def restore_reference_image(version_id: str, ref_id: str, body: dict | None = Body(None)):
    """把废弃画廊里的参考图恢复为可用（重新计入喂给视频模型的参考图）。
    若该图曾被 QA 淘汰，body.override_reason 必填，写入审计字段。"""
    from app.capabilities.dispatch import ui_route
    body = _as_body_dict(body)
    routed = await ui_route(
        "reference.review",
        {
            "version_id": version_id,
            "ref_id": ref_id,
            "action": "restore",
            "override_reason": body.get("override_reason"),
        },
    )
    if routed is not None:
        return routed
    return _set_reference_image_used(
        version_id, ref_id, use=True, override_reason=body.get("override_reason"),
    )


# ----- 视频生成（固定参考图模式） -----

def _shot_by_no(episode_id: str, shot_no: int):
    return get_conn().execute(
        "SELECT id FROM shots WHERE episode_id=? AND shot_no=?", (episode_id, shot_no)).fetchone()


@router.post("/episodes/{episode_id}/generate")
async def generate_episode(episode_id: str, body: dict | None = None):
    """批量生成整集视频（固定参考图模式）：每个视频任务内部生成/复用参考图并提交 Seedance。
    body.from_shot_no：只从该镜起、沿其连续段往后重生（中途改动后用）。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.generate_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    qualification = _review_assert_positive_action(
        episode_id, (body or {}).get("qualification_version"),
    )
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise HTTPException(409, "分镜脚本未确认（先在工作台点击确认分镜）")
    # Supervisor 运行期间拒绝快速模式，避免重复付费
    try:
        mode = ep["video_completion_mode"]
    except (KeyError, IndexError, TypeError):
        mode = None
    if mode == "complete" and task_registry.active("video_completion", episode_id):
        raise HTTPException(409, "全片补齐 Supervisor 运行中，请使用补齐模式或等待完成")
    conn = get_conn()
    shots_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,)).fetchall()
    board = _board_from_shot_rows(shots_rows, ep["episode_no"])
    shots = [
        {"row": dict(row), "shot": board.shots[idx], "prev": board.shots[idx - 1] if idx > 0 else None}
        for idx, row in enumerate(shots_rows)
    ]
    from_no = (body or {}).get("from_shot_no")
    if from_no is not None:
        try:
            from_no = int(from_no)
        except (TypeError, ValueError):
            pass
    if from_no:
        selected = []
        for i, s in enumerate(shots):
            if s["row"]["shot_no"] == from_no:
                selected = [s]
                for nxt in shots[i + 1:]:
                    if _uses_previous_tail_frame_for_model(nxt["shot"], nxt["prev"]):
                        selected.append(nxt)
                    else:
                        break
                break
        if not selected:
            raise HTTPException(404, f"未找到镜 {from_no}")
    else:
        selected = shots
    # Quick generation must not create one doomed paid-version record per shot.
    # The completion supervisor owns the self-healing asset preparation path.
    from app.multiview import scan_episode_reference_asset_gaps
    asset_gaps = scan_episode_reference_asset_gaps(
        project_id=ep["project_id"],
        episode_no=int(ep["episode_no"]),
        shots=[(item["row"]["id"], item["shot"]) for item in selected],
    )
    if asset_gaps["blockers"]:
        names = [
            *(f"人物「{name}」" for name in asset_gaps["characters"]),
            *(f"场景「{name}」" for name in asset_gaps["scenes"]),
        ]
        summary = "、".join(names) or "本集生产资产"
        raise HTTPException(
            409,
            f"{summary}尚未就绪。为避免整集批量失败，请使用“补齐到全片可用”，系统会先补齐资产再生成视频。",
        )
    # 不再预先清空 adopted_version_id：新版本成功并通过技术门禁后由
    # select_best_video_candidate 比较切换；任务失败时保留原可交付采用结果。
    # 固定参考图模式：批量生成前确保每个选中镜都有固定参考图计划。
    for s in selected:
        await _ensure_shot_mode_plan(conn, s["row"]["id"])
    results = []
    for s in selected:
        after = None
        if _uses_previous_tail_frame_for_model(s["shot"], s["prev"]) and s["row"]["shot_no"] > 1:
            pr = _shot_by_no(episode_id, s["row"]["shot_no"] - 1)
            after = pr["id"] if pr else None
        try:
            r = worker.enqueue_shot(
                s["row"]["id"], after_shot_id=after,
                dependency_snapshot=qualification,
            )
            # 幂等命中（已有相同成片）：若当前无采用版，把复用版标为采用
            if r.get("reused") and r.get("version_id"):
                row = conn.execute(
                    "SELECT adopted_version_id FROM shots WHERE id=?", (s["row"]["id"],)
                ).fetchone()
                if not row or not row["adopted_version_id"]:
                    conn.execute(
                        "UPDATE shots SET adopted_version_id=? WHERE id=?",
                        (r["version_id"], s["row"]["id"]),
                    )
            results.append({"shot_id": s["row"]["id"], **r})
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(exc, action="enqueue_shot",
                                              context={"shot_id": s["row"]["id"], "episode_id": episode_id})
            issue_codes: list[str] = []
            try:
                from app.video_issues import issues_from_enqueue_error, persist_shot_issue
                issues = issues_from_enqueue_error(
                    exc, shot_id=s["row"]["id"], shot_no=s["row"]["shot_no"],
                )
                issue_codes = [i.code for i in issues]
                persist_shot_issue(
                    episode_id=episode_id,
                    shot_id=s["row"]["id"],
                    shot_no=s["row"]["shot_no"],
                    issues=issues,
                    source="generate_episode_enqueue",
                )
            except Exception:  # noqa: BLE001
                pass
            results.append({
                "shot_id": s["row"]["id"],
                "error": public,
                "issue_codes": issue_codes,
            })
    conn.commit()
    return {"enqueued": results}


async def _generate_shot_core(shot_id: str, body: dict) -> dict:
    """单镜生成视频的领域逻辑，供 REST 路由与 ``video.generate_shot`` Command Handler 共用。"""
    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise HTTPException(404, "镜头不存在")
    qualification = _review_assert_shot_positive(
        shot_id, body.get("qualification_version"),
    )
    # 带 AI 评语重生：取「当前采用版 / 最新成功版」的问题清单（必要时现场跑评审），
    # 作为本次必须改正项写入 prompt，避免模型再犯同样的错。
    critique: list[str] | None = None
    critique_sources: list[dict] = []
    if body.get("with_critique"):
        ref = None
        if shot_row["adopted_version_id"]:
            ref = conn.execute("SELECT id FROM shot_versions WHERE id=? AND status='succeeded'",
                               (shot_row["adopted_version_id"],)).fetchone()
        if not ref:
            ref = conn.execute(
                "SELECT id FROM shot_versions WHERE shot_id=? AND status='succeeded' ORDER BY version_no DESC LIMIT 1",
                (shot_id,)).fetchone()
        if ref:
            critique = await worker.critique_version(ref["id"])
            critique_sources.append({"source": "video_qa", "version_id": ref["id"]})
    review_item_ids = [str(item) for item in (body.get("review_item_ids") or []) if str(item).strip()]
    if review_item_ids:
        _ensure_review_wall_tables(conn)
        marks = ",".join("?" for _ in review_item_ids)
        rows = conn.execute(
            f"""SELECT id, comment, severity, anchor_json FROM shot_review_items
                  WHERE shot_id=? AND id IN ({marks}) AND status IN ('open','in_progress')""",
            (shot_id, *review_item_ids),
        ).fetchall()
        if len(rows) != len(set(review_item_ids)):
            raise HTTPException(409, "所选评审项已变化、关闭或不属于当前镜头，请刷新向导")
        critique = list(critique or [])
        for item in rows:
            critique.append(f"[{item['severity']}] {item['comment']}")
            critique_sources.append({
                "source": "review_item", "review_item_id": item["id"],
                "severity": item["severity"], "anchor": _review_json(item["anchor_json"], {}),
            })
    # 固定参考图模式：生成前确保已有固定参考图计划。
    await _ensure_shot_mode_plan(conn, shot_id)
    # 同场景接上镜时，参考图模式可复用上一镜可用素材作为参考。
    after = None
    prev_row = None
    prev_shot = None
    if shot_row["shot_no"] > 1:
        prev_row = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
            (shot_row["episode_id"], shot_row["shot_no"] - 1),
        ).fetchone()
    if prev_row:
        models = _board_from_shot_rows([prev_row, shot_row], 0).shots
        prev_shot, shot_model = models[0], models[1]
    else:
        shot_model = _board_from_shot_rows([shot_row], 0).shots[0]
    if _uses_previous_tail_frame_for_model(shot_model, prev_shot) and shot_row["shot_no"] > 1:
        pr = _shot_by_no(shot_row["episode_id"], shot_row["shot_no"] - 1)
        after = pr["id"] if pr else None
    try:
        return worker.enqueue_shot(
            shot_id,
            prompt_override=body.get("prompt_override"),
            extra_negative=body.get("extra_negative"),
            reroll=bool(body.get("reroll")) or bool(body.get("with_critique")),
            critique=critique, after_shot_id=after,
            dependency_snapshot=qualification,
            critique_sources=critique_sources)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/shots/{shot_id}/generate")
async def generate_shot(shot_id: str, body: dict | None = None):
    from app.capabilities.dispatch import dispatch, respond_ui

    body = body or {}
    result = await dispatch(
        "video.generate_shot",
        {
            "shot_id": shot_id,
            "prompt_override": body.get("prompt_override"),
            "reroll": bool(body.get("reroll")),
            "critique": body.get("critique") or ("with_critique" if body.get("with_critique") else None),
            "review_item_ids": body.get("review_item_ids") or [],
            "qualification_version": body.get("qualification_version"),
            "idempotency_key": body.get("idempotency_key"),
            "request_id": body.get("request_id"),
        },
        initiator="ui",
    )
    return respond_ui(result)


@router.post("/shots/{shot_id}/video/stop")
async def stop_shot_video(shot_id: str):
    """立即停止本镜全部排队中或运行中的视频任务；重复调用安全。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.stop_shot", {"shot_id": shot_id})
    if routed is not None:
        return routed
    try:
        return worker.stop_shot_video_tasks(shot_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _adopt_version_core(shot_id: str, body: dict) -> dict:
    """人工采用视频版本的领域逻辑，供 REST 路由与 ``video.adopt_version`` Command Handler 共用。"""
    version_id = body.get("version_id")
    _review_assert_shot_positive(shot_id, body.get("qualification_version"))
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=? AND shot_id=?", (version_id, shot_id)).fetchone()
    if not v or v["status"] != "succeeded":
        raise HTTPException(409, "该版本不存在或未成功")
    from app.evidence import media as media_evidence

    try:
        artifact = media_evidence.record_video_candidate(version_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"候选证据创建失败：{exc}") from exc
    technical = json.loads(v["technical_validation_json"] or "{}")
    if not technical:
        refreshed = conn.execute(
            "SELECT technical_validation_json FROM shot_versions WHERE id=?", (version_id,)
        ).fetchone()
        technical = json.loads(refreshed["technical_validation_json"] or "{}")
    if not technical.get("passed"):
        raise HTTPException(409, "视频技术门禁未通过，不能人工采用")
    qa = json.loads(v["qa_json"] or "{}")
    observed_state_out = qa.get("observed_state_out")
    if observed_state_out:
        media_evidence.merge_observed_state_out_into_shot_contract(shot_id, str(observed_state_out))
    reason = str(body.get("reason") or "").strip()
    if len(reason) < 4 or reason in {"人工横向比较后采用", "默认", "同意"}:
        raise HTTPException(422, "请填写有效的采用理由（至少 4 个字，说明质量、成本或版本比较）")
    evidence_repository.commit_artifact(
        None,
        artifact["id"],
        [Evaluation(
            evaluator_type="human", evaluator_name=str(body.get("decided_by") or "user"),
            evaluator_version="1.0.0", status="passed", hard_gate_passed=True,
            score=100, evidence={"decision": "adopt", "reason": reason},
        )],
    )
    shot = conn.execute("SELECT episode_id, adopted_version_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.execute("UPDATE shot_versions SET adoption_reason=? WHERE id=?", (reason, version_id))
    conn.execute(
        """INSERT INTO gate_decisions(
               id, artifact_id, gate_key, decision, decided_by, reason, created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            new_id("gate"), artifact["id"], "video_adoption", "approve",
            str(body.get("decided_by") or "user"), reason, now(),
        ),
    )
    conn.commit()
    _review_write_audit(
        "video_version.adopt", "shot", shot_id, target_version=version_id,
        old_state={"adopted_version_id": shot["adopted_version_id"] if shot else None},
        new_state={"adopted_version_id": version_id}, reason=reason,
        idempotency_key=body.get("idempotency_key"), request_id=body.get("request_id"),
    )
    if shot and shot["adopted_version_id"] != version_id:
        worker.invalidate_episode_final(shot["episode_id"])
    return {"adopted": version_id, "artifact_id": artifact["id"], "reason": reason}


@router.post("/shots/{shot_id}/adopt")
async def adopt_version(shot_id: str, body: dict):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch(
        "video.adopt_version",
        {
            "shot_id": shot_id, "version_id": body.get("version_id"), "reason": body.get("reason"),
            "qualification_version": body.get("qualification_version"),
            "idempotency_key": body.get("idempotency_key"), "request_id": body.get("request_id"),
        },
        initiator="ui",
    )
    return respond_ui(result)


@router.post("/episodes/{episode_id}/resume")
async def resume_episode(episode_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.resume_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    return {"resumed_jobs": worker.retry_paused(episode_id)}


def _ensure_video_episode_columns(conn=None) -> None:
    db = conn or get_conn()
    for stmt in (
        "ALTER TABLE episodes ADD COLUMN active_video_run_id TEXT",
        "ALTER TABLE episodes ADD COLUMN video_completion_mode TEXT NOT NULL DEFAULT 'quick'",
        "ALTER TABLE episodes ADD COLUMN video_control_json TEXT",
    ):
        try:
            db.execute(stmt)
            db.commit()
        except Exception:  # noqa: BLE001
            pass


async def _recorded_video_completion_task(
    episode_id: str,
    recorder,
    *,
    resume: bool,
    grant_id: str | None,
    budget_cap_cny: float | None = None,
    wall_clock_cap_s: float | None = None,
    allow_fallback_adopt: bool = True,
    max_fallback_shots: int | None = None,
    allow_storyboard_edit: bool = False,
):
    import asyncio
    from app.video_supervisor import run_video_completion_resilient
    recorder.start()
    try:
        result = await run_video_completion_resilient(
            episode_id,
            resume=resume,
            grant_id=grant_id,
            run_id=recorder.run_id,
            budget_cap_cny=budget_cap_cny,
            wall_clock_cap_s=wall_clock_cap_s,
            allow_fallback_adopt=allow_fallback_adopt,
            max_fallback_shots=max_fallback_shots,
            allow_storyboard_edit=allow_storyboard_edit,
        )
        if result.phase == "SUCCEEDED_COVERED":
            recorder.succeed(result.outcome or "SUCCEEDED_COVERED")
        elif result.phase == "CANCELLED":
            recorder.cancel()
        else:
            recorder.partial(result.outcome or result.phase)
        if result.phase in {
            "SUCCEEDED_COVERED", "COMPLETED_DEADLINE_FALLBACK",
            "PARTIAL_NO_USABLE_CANDIDATE", "FAILED_CLOSED", "CANCELLED",
        }:
            from app.media_exec.enqueue import reconcile_episode_generation_status
            reconcile_episode_generation_status(episode_id)
        return result
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:
        recorder.fail(exc)
        raise


@router.post("/episodes/{episode_id}/video-completion")
async def complete_episode(episode_id: str, body: dict | None = None):
    """启动集级视频补齐 Supervisor（补齐到全片可用）。"""
    from app.capabilities.dispatch import ui_route
    payload = {"episode_id": episode_id, **(body or {})}
    routed = await ui_route("video.complete_episode", payload)
    if routed is not None:
        return routed
    return await _complete_episode_core(episode_id, body or {})


async def _complete_episode_core(episode_id: str, body: dict) -> dict:
    from app.completion_grant import (
        DEFAULT_VIDEO_BUDGET_CAP_CNY,
        DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
        default_max_fallback_shots,
        issue_video_completion_grant,
        bump_video_grant_budget,
        get_video_grant,
    )
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.video_supervisor import (
        FIRST_PASS_BUDGET_FRACTION,
        MAX_ATTEMPTS_PER_SHOT,
        MAX_CHAIN_CASCADE_DEPTH,
        MAX_REPAIR_EPOCHS,
        MIN_ATTEMPTS_PER_SHOT,
    )

    ep = _episode_or_404(episode_id)
    _review_assert_positive_action(episode_id, body.get("qualification_version"))
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise HTTPException(409, "分镜脚本未确认（先在工作台点击确认分镜）")
    _ensure_video_episode_columns()
    mode = body.get("mode") or "fresh"
    if mode not in {"fresh", "resume"}:
        raise HTTPException(422, "mode 只能是 fresh 或 resume")

    if task_registry.active("video_completion", episode_id):
        raise HTTPException(409, "全片补齐 Supervisor 已在运行")

    conn = get_conn()
    shots_total = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"]
    if int(shots_total or 0) <= 0:
        raise HTTPException(409, "本集尚无分镜")

    budget_cap = _review_validate_authorization_number(
        body.get("budget_cap_cny"), field="budget_cap_cny", minimum=1, maximum=100000,
    )
    wall_cap = _review_validate_authorization_number(
        body.get("wall_clock_cap_s"), field="wall_clock_cap_s", minimum=60, maximum=604800,
    )
    allow_fallback = body.get("allow_fallback_adopt", True)
    max_fallback = body.get("max_fallback_shots")
    allow_edit = bool(body.get("allow_storyboard_edit", False))
    grant_id = body.get("completion_grant_id")

    # resume + 追加预算
    add_budget = _review_validate_authorization_number(
        body.get("add_budget_cny"), field="add_budget_cny", minimum=1, maximum=100000,
    )
    add_wall = _review_validate_authorization_number(
        body.get("add_wall_clock_s"), field="add_wall_clock_s", minimum=60, maximum=604800,
    )
    if (add_budget is not None or add_wall is not None) and not (mode == "resume" and grant_id):
        raise HTTPException(422, "追加授权只能用于带 completion_grant_id 的 resume 模式")
    if mode == "resume" and grant_id and (add_budget is not None or add_wall is not None):
        bump_video_grant_budget(
            grant_id,
            add_cny=float(add_budget or 0),
            add_wall_s=float(add_wall or 0),
        )

    if mode == "fresh" or not grant_id:
        grant, _token = issue_video_completion_grant(
            episode_id=episode_id,
            project_id=ep["project_id"],
            storyboard_artifact_id=ep["storyboard_artifact_id"] or "",
            budget_cap_cny=float(budget_cap) if budget_cap is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY,
            wall_clock_cap_s=float(wall_cap) if wall_cap is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
            allow_fallback_adopt=bool(allow_fallback),
            max_fallback_shots=(
                int(max_fallback) if max_fallback is not None
                else default_max_fallback_shots(int(shots_total))
            ),
            allow_storyboard_edit=allow_edit,
            shots_total=int(shots_total),
            impact_snapshot={
                "mode": "complete_episode_video",
                "auto_concatenate": False,
                "auto_delivery": False,
            },
        )
        grant_id = grant.grant_id
        budget_cap = grant.budget_cap_cny
        wall_cap = grant.wall_clock_cap_s
        max_fallback = grant.max_fallback_shots
    else:
        existing = get_video_grant(grant_id)
        if existing:
            budget_cap = existing.budget_cap_cny
            wall_cap = existing.wall_clock_cap_s
            max_fallback = existing.max_fallback_shots
            allow_fallback = existing.allow_fallback_adopt
            allow_edit = existing.allow_storyboard_edit

    conn.execute(
        """UPDATE episodes
           SET video_completion_mode='complete',
               status='generating',
               active_video_run_id=NULL
           WHERE id=?""",
        (episode_id,),
    )
    conn.commit()

    cap = float(budget_cap if budget_cap is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY)
    resolved_wall_cap = float(
        wall_cap if wall_cap is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    )
    recorder = WorkflowRecorder.create(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            ep["storyboard_artifact_id"], grant_id, mode,
        ),
        requested_by="user",
        trigger_type="manual",
        budget_limit_cny=cap,
        deadline_at=now() + resolved_wall_cap,
        policy_snapshot={
            "supervisor": "video_completion",
            "budget_cap_cny": cap,
            "wall_clock_cap_s": resolved_wall_cap,
            "first_pass_budget_fraction": FIRST_PASS_BUDGET_FRACTION,
            "min_attempts_per_shot": MIN_ATTEMPTS_PER_SHOT,
            "max_attempts_per_shot": MAX_ATTEMPTS_PER_SHOT,
            "max_repair_epochs": MAX_REPAIR_EPOCHS,
            "max_chain_cascade_depth": MAX_CHAIN_CASCADE_DEPTH,
            "allow_fallback_adopt": bool(allow_fallback),
            "max_fallback_shots": int(max_fallback or 0),
            "allow_storyboard_edit": allow_edit,
        },
    )
    conn.execute(
        "UPDATE episodes SET active_video_run_id=? WHERE id=?",
        (recorder.run_id, episode_id),
    )
    conn.commit()

    task_registry.spawn(
        "video_completion", episode_id,
        _recorded_video_completion_task(
            episode_id, recorder,
            resume=(mode == "resume"),
            grant_id=grant_id,
            budget_cap_cny=cap,
            wall_clock_cap_s=float(wall_cap) if wall_cap is not None else None,
            allow_fallback_adopt=bool(allow_fallback),
            max_fallback_shots=int(max_fallback) if max_fallback is not None else None,
            allow_storyboard_edit=allow_edit,
        ),
        project_id=ep["project_id"],
    )
    return {
        "status": "accepted",
        "run_id": recorder.run_id,
        "goal": "complete_episode_video",
        "completion_grant_id": grant_id,
        "resource_uri": f"manju://runs/{recorder.run_id}",
    }


@router.get("/episodes/{episode_id}/video-completion")
def get_video_completion(episode_id: str):
    """只读：最新 checkpoint 公开投影 + 覆盖台账。"""
    _episode_or_404(episode_id)
    _ensure_video_episode_columns()
    from app.video_supervisor import (
        load_latest_checkpoint,
        public_checkpoint_projection,
        rebuild_coverage_ledger,
    )
    from app.video_cost_model import predict_episode_completion_cost
    cp = load_latest_checkpoint(episode_id)
    try:
        ledger = rebuild_coverage_ledger(episode_id, cp=cp)
        proj = public_checkpoint_projection(cp) or {}
        proj["ledger"] = {
            "shots_total": ledger.shots_total,
            "grades": ledger.grades,
            "coverage_rate": ledger.coverage_rate,
            "fallback_quota": ledger.fallback_quota,
            "cost_spent": ledger.cost_spent,
            "entries": [e.model_dump(mode="json") for e in ledger.entries],
        }
        adopted_count = sum(1 for entry in ledger.entries if entry.adopted_version_id)
        proj["coverage"] = {
            **(proj.get("coverage") or {}),
            "A": ledger.grades.get("A", 0),
            "B": ledger.grades.get("B", 0),
            "C": ledger.grades.get("C", 0),
            "total": ledger.shots_total,
            "adopted": adopted_count,
            "unadopted": max(0, ledger.shots_total - adopted_count),
            "coverage_rate": ledger.coverage_rate,
            "fallback_quota": ledger.fallback_quota,
        }
        try:
            uncovered_ids = [e.shot_id for e in ledger.entries if not e.adopted_version_id]
            proj["cost_forecast"] = predict_episode_completion_cost(
                episode_id, uncovered_shot_ids=uncovered_ids,
            )
        except Exception:  # noqa: BLE001
            proj["cost_forecast"] = None
    except Exception as exc:  # noqa: BLE001 — 台账失败时仍返回 checkpoint，避免面板整页 500
        proj = public_checkpoint_projection(cp) or {}
        proj["ledger"] = {"shots_total": 0, "grades": {}, "coverage_rate": 0.0, "entries": []}
        proj["cost_forecast"] = None
        proj["ledger_error"] = str(exc)
    conn = get_conn()
    ep = conn.execute(
        "SELECT active_video_run_id, video_completion_mode FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    try:
        proj["active_video_run_id"] = ep["active_video_run_id"] if ep else None
        proj["video_completion_mode"] = ep["video_completion_mode"] if ep else "quick"
    except (KeyError, IndexError, TypeError):
        proj["active_video_run_id"] = None
        proj["video_completion_mode"] = "quick"
    proj["running"] = task_registry.active("video_completion", episode_id)
    return proj


@router.get("/episodes/{episode_id}/video-completion/repair-preview")
def preview_video_completion_repair_route(episode_id: str):
    """只读：预演遗留 Supervisor 的收口动作。"""
    _episode_or_404(episode_id)
    from app.video_supervisor import preview_video_completion_repair
    return preview_video_completion_repair(episode_id)


@router.post("/episodes/{episode_id}/video-completion/repair")
def repair_video_completion_route(episode_id: str, body: dict | None = None):
    """显式确认后收口遗留 run；不会启动任何新视频生成。"""
    from app.completion_grant import get_video_grant
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.video_supervisor import (
        VideoSupervisorCheckpoint,
        _deadline_closeout,
        _mark_failed_closed,
        load_latest_checkpoint,
        preview_video_completion_repair,
        public_checkpoint_projection,
    )

    ep = _episode_or_404(episode_id)
    if not body or body.get("confirm") is not True:
        raise HTTPException(409, "必须先查看 repair-preview，并显式提交 confirm=true")
    if task_registry.active("video_completion", episode_id):
        raise HTTPException(409, "Supervisor 仍在真实运行，不能执行遗留收口")
    preview = preview_video_completion_repair(episode_id)
    cp = load_latest_checkpoint(episode_id) or VideoSupervisorCheckpoint(
        episode_id=episode_id,
        started_at=now(),
    )
    if cp.deadline_at is None and cp.grant_id:
        grant = get_video_grant(cp.grant_id)
        if grant:
            cp.deadline_at = float(grant.deadline_at)
    parent_run_id = ep["active_video_run_id"]
    recorder = WorkflowRecorder.create(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            ep["storyboard_artifact_id"], cp.grant_id, "confirmed_legacy_closeout",
        ),
        requested_by="user",
        trigger_type="repair",
        policy_snapshot={"supervisor": "video_completion", "confirmed_legacy_closeout": True},
        deadline_at=cp.deadline_at or now(),
        parent_run_id=parent_run_id,
    )
    recorder.start()
    conn = get_conn()
    conn.execute(
        """UPDATE episodes
           SET active_video_run_id=?, video_completion_mode='complete', status='generating'
           WHERE id=?""",
        (recorder.run_id, episode_id),
    )
    conn.commit()
    cp.run_id = recorder.run_id
    try:
        result = _deadline_closeout(
            cp,
            run_id=recorder.run_id,
            reason="CONFIRMED_LEGACY_INCIDENT_CLOSEOUT",
        )
        recorder.partial(result.outcome or result.phase)
    except Exception as exc:  # noqa: BLE001
        _mark_failed_closed(
            cp,
            run_id=recorder.run_id,
            reason=f"CONFIRMED_REPAIR_FAILED: {type(exc).__name__}: {exc}",
        )
        recorder.fail(exc)
        raise HTTPException(500, f"遗留 run 收口失败：{exc}") from exc
    return {
        "status": "closed",
        "run_id": recorder.run_id,
        "preview": preview,
        "result": public_checkpoint_projection(result),
    }


@router.post("/projects/{project_id}/video-completion")
async def complete_project_videos(project_id: str, body: dict | None = None):
    """跨集批量补齐：在全局预算内按集顺序启动 Supervisor。"""
    from app.capabilities.dispatch import ui_route
    payload = {"project_id": project_id, **(body or {})}
    routed = await ui_route("video.complete_project", payload)
    if routed is not None:
        return routed
    return await _complete_project_videos_core(project_id, body or {})


async def _complete_project_videos_core(project_id: str, body: dict) -> dict:
    """全局预算编排：按 episode_no 顺序分配 per-episode cap，串行启动未覆盖集。"""
    import asyncio
    conn = get_conn()
    project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, "项目不存在")

    global_cap = float(_review_validate_authorization_number(
        body.get("global_budget_cap_cny", 500), field="global_budget_cap_cny", minimum=1, maximum=1000000, allow_none=False,
    ))
    per_cap = float(_review_validate_authorization_number(
        body.get("per_episode_cap_cny", 150), field="per_episode_cap_cny", minimum=1, maximum=100000, allow_none=False,
    ))
    wall_cap = float(_review_validate_authorization_number(
        body.get("wall_clock_cap_s", 4 * 3600), field="wall_clock_cap_s", minimum=60, maximum=604800, allow_none=False,
    ))
    allow_fallback = bool(body.get("allow_fallback_adopt", True))
    allow_edit = bool(body.get("allow_storyboard_edit", False))
    episode_ids = body.get("episode_ids")

    rows = conn.execute(
        """SELECT id, episode_no, status, storyboard_artifact_id FROM episodes
           WHERE project_id=? ORDER BY episode_no""",
        (project_id,),
    ).fetchall()
    if episode_ids:
        wanted = set(episode_ids)
        rows = [r for r in rows if r["id"] in wanted]
    eligible = [
        r for r in rows
        if r["status"] in {"confirmed", "generating", "done"}
    ]
    if not eligible:
        raise HTTPException(409, "没有可补齐的已确认剧集")

    spent_row = conn.execute(
        """SELECT COALESCE(SUM(v.cost_cny),0) AS c
           FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE e.project_id=? AND v.status='succeeded'""",
        (project_id,),
    ).fetchone()
    project_spent = float(spent_row["c"] if spent_row else 0)
    remaining_global = max(0.0, global_cap - project_spent)

    plan = []
    allocated = 0.0
    from app.video_supervisor import rebuild_coverage_ledger
    for r in eligible:
        if task_registry.active("video_completion", r["id"]):
            plan.append({
                "episode_id": r["id"], "episode_no": r["episode_no"],
                "status": "already_running", "allocated_cny": 0,
            })
            continue
        try:
            ledger = rebuild_coverage_ledger(r["id"])
            if ledger.covered_within_quota():
                plan.append({
                    "episode_id": r["id"], "episode_no": r["episode_no"],
                    "status": "already_covered", "allocated_cny": 0,
                })
                continue
        except Exception:  # noqa: BLE001
            pass
        room = remaining_global - allocated
        if room < 5:
            plan.append({
                "episode_id": r["id"], "episode_no": r["episode_no"],
                "status": "skipped_budget", "allocated_cny": 0,
            })
            continue
        ep_cap = min(per_cap, room)
        plan.append({
            "episode_id": r["id"], "episode_no": r["episode_no"],
            "status": "queued", "allocated_cny": ep_cap,
        })
        allocated += ep_cap

    started = []
    queue = [p for p in plan if p["status"] == "queued"]

    async def _run_one(item: dict) -> dict:
        room_now = max(0.0, global_cap - _project_video_spent(project_id))
        if room_now < 5:
            item["status"] = "skipped_budget"
            return item
        item["allocated_cny"] = min(float(item["allocated_cny"]), room_now)
        result = await _complete_episode_core(item["episode_id"], {
            "mode": "fresh",
            "budget_cap_cny": item["allocated_cny"],
            "wall_clock_cap_s": wall_cap,
            "allow_fallback_adopt": allow_fallback,
            "allow_storyboard_edit": allow_edit,
        })
        item["status"] = "started"
        item["run_id"] = result.get("run_id")
        item["completion_grant_id"] = result.get("completion_grant_id")
        return item

    if queue:
        first = await _run_one(queue[0])
        started.append(first)
        rest = queue[1:]
        if rest:
            async def _chain(items=rest):
                for item in items:
                    # 等待项目内任意集级 supervisor 空闲
                    while any(
                        task_registry.active("video_completion", p["episode_id"])
                        for p in plan
                        if p.get("episode_id") and p.get("status") in {"queued", "started", "already_running"}
                    ):
                        await asyncio.sleep(5)
                    try:
                        await _run_one(item)
                    except Exception as exc:  # noqa: BLE001
                        item["status"] = "failed"
                        item["error"] = str(exc)[:200]
                    while task_registry.active("video_completion", item["episode_id"]):
                        await asyncio.sleep(8)

            task_registry.spawn(
                "video_completion_project", project_id, _chain(), project_id=project_id,
            )

    return {
        "status": "accepted",
        "project_id": project_id,
        "global_budget_cap_cny": global_cap,
        "project_spent_cny": project_spent,
        "remaining_cny": remaining_global,
        "plan": plan,
        "started": started,
    }


def _project_video_spent(project_id: str) -> float:
    row = get_conn().execute(
        """SELECT COALESCE(SUM(v.cost_cny),0) AS c
           FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE e.project_id=? AND v.status='succeeded'""",
        (project_id,),
    ).fetchone()
    return float(row["c"] if row else 0)


# ---------- 成片台：预览 / 拼接 / 导出 ----------

@router.get("/episodes/{episode_id}/mix-status")
def mix_status(episode_id: str):
    """按镜号顺序返回每镜成片 URL、整体进度、已合成成品（若有）。"""
    _episode_or_404(episode_id)
    return worker.episode_mix_status(episode_id)


@router.post("/episodes/{episode_id}/concatenate")
async def concatenate(episode_id: str):
    """把本集所有已采用的视频片段按镜号顺序拼接成一个 MP4。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("delivery.concatenate", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    try:
        return worker.concatenate_episode(episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"ffmpeg 合成失败：{exc}")


@router.get("/episodes/{episode_id}/stale-assets-preview")
def stale_assets_preview(episode_id: str):
    """评审墙：资产/分镜 stale 影响预览（只读）。"""
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    from app.domain.storyboard_ops import _shot_video_is_stale, _shot_adopted_assets_stale
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    items = []
    for row in rows:
        shot = dict(row)
        stale = _shot_video_is_stale(conn, shot, ep.get("storyboard_artifact_id"))
        if not stale:
            continue
        reasons = []
        try:
            if ep.get("storyboard_artifact_id") and shot.get("storyboard_artifact_id") and (
                shot["storyboard_artifact_id"] != ep["storyboard_artifact_id"]
            ):
                reasons.append("storyboard_artifact")
        except (KeyError, TypeError):
            pass
        adopted = shot.get("adopted_version_id")
        if adopted:
            ver = conn.execute(
                "SELECT artifact_id, image_inputs FROM shot_versions WHERE id=?", (adopted,)
            ).fetchone()
            if ver and _shot_adopted_assets_stale(conn, shot, ver):
                reasons.append("asset_revision")
        if not reasons:
            reasons.append("parent_artifact")
        reason_labels = {
            "storyboard_artifact": "已确认分镜版本已变更",
            "asset_revision": "人物或场景资产版本已变更",
            "parent_artifact": "上游证据链已变更",
        }
        items.append({
            "shot_id": shot["id"],
            "shot_no": shot["shot_no"],
            "adopted_version_id": adopted,
            "reasons": reasons,
            "reason_labels": [reason_labels.get(reason, "未知陈旧原因") for reason in reasons],
            "storyboard_artifact_id": shot.get("storyboard_artifact_id"),
            "current_storyboard_artifact_id": ep.get("storyboard_artifact_id"),
            "estimated_cost_cny": shot_cost_cny(float(shot.get("duration_s") or 0)),
            "hint": "参考资产或分镜已更新，本镜采用版可能使用旧证据链",
        })
    qualification = _review_upstream_snapshot(episode_id)
    for item in items:
        asset_inputs = [
            asset for asset in qualification["assets"].get("inputs", [])
            if asset.get("shot_id") == item["shot_id"]
        ]
        item["asset_qualification"] = asset_inputs
        item["asset_soft_warnings"] = [
            warning for warning in qualification["assets"].get("soft_warnings", [])
            if warning.get("shot_id") == item["shot_id"]
        ]
        item["rule_versions"] = sorted({
            str(asset.get("rule_version")) for asset in asset_inputs if asset.get("rule_version")
        })
    preview_version = _review_sha({
        "episode_id": episode_id,
        "qualification_version": qualification["qualification_version"],
        "shots": [(item["shot_id"], item["adopted_version_id"], item["reasons"]) for item in items],
    })[:32]
    return {
        "episode_id": episode_id,
        "stale_count": len(items),
        "shots": items,
        "estimated_cost_cny": round(sum(item["estimated_cost_cny"] for item in items), 2),
        "qualification": qualification,
        "preview_version": preview_version,
        "repair_action": "POST /api/episodes/{id}/repair-stale-assets with confirm=true",
    }


@router.post("/episodes/{episode_id}/repair-stale-assets")
async def repair_stale_assets(episode_id: str, body: dict | None = None):
    """批量修复 stale 镜头：对指定/全部 stale 镜强制重抽新视频版本（保留旧采用版直至新版成功）。"""
    body = body or {}
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "video.repair_stale_assets",
        {
            "episode_id": episode_id,
            "shot_ids": body.get("shot_ids") or [],
            "confirm": body.get("confirm") is True,
            "preview_version": body.get("preview_version"),
            "qualification_version": body.get("qualification_version"),
            "idempotency_key": body.get("idempotency_key"),
        },
    )
    if routed is not None:
        return routed
    if body.get("confirm") is not True:
        raise HTTPException(409, "必须先查看 stale-assets-preview，并显式提交 confirm=true")
    preview = stale_assets_preview(episode_id)
    _review_assert_positive_action(episode_id, body.get("qualification_version"))
    if body.get("preview_version") and body.get("preview_version") != preview["preview_version"]:
        raise HTTPException(409, {
            "code": "STALE_PREVIEW_EXPIRED",
            "message": "陈旧资产范围或依赖已变化，请重新预演",
            "preview": preview,
        })
    wanted = set(body.get("shot_ids") or [])
    targets = [
        item for item in preview["shots"]
        if not wanted or item["shot_id"] in wanted
    ]
    if not targets:
        return {"queued": 0, "shot_ids": [], "message": "没有需要修复的 stale 镜头"}
    queued = []
    errors = []
    for item in targets:
        try:
            result = await _generate_shot_core(item["shot_id"], {
                "reroll": True,
                "qualification_version": preview["qualification"]["qualification_version"],
                "idempotency_key": f"{body.get('idempotency_key') or preview['preview_version']}:{item['shot_id']}",
            })
            queued.append({"shot_id": item["shot_id"], "shot_no": item["shot_no"], "result": result})
        except Exception as exc:  # noqa: BLE001
            errors.append({"shot_id": item["shot_id"], "shot_no": item["shot_no"], "error": str(exc)})
    return {
        "queued": len(queued),
        "shot_ids": [q["shot_id"] for q in queued],
        "errors": errors,
        "message": f"已为 {len(queued)} 个 stale 镜头提交重生",
        "preview_version": preview["preview_version"],
    }


__all__ = [name for name in globals() if not name.startswith("__")]
