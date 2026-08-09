from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

try:
    _board_from_shot_rows
except NameError:  # pragma: no cover - direct module import
    from app.domain.storyboard_ops import (
        _board_from_shot_rows,
        _finalize_storyboard_evidence,
    )

def _shot_contract_json(shot: Shot) -> str:
    from app.continuity import shot_contract_dict
    return json.dumps(shot_contract_dict(shot), ensure_ascii=False)


def _uses_previous_tail_frame_for_model(shot: Shot, prev: Shot | None = None) -> bool:
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    return uses_previous_tail_frame(derive_continuity_mode(shot, prev))


class ConfirmationEvaluation:
    """只读确认评估结果；不写数据库。"""

    __slots__ = (
        "passed", "errors", "warnings", "issues", "board", "compact_target",
        "estimated_cost_cny",
    )

    def __init__(
        self,
        *,
        passed: bool,
        errors: list[str],
        warnings: list[str],
        issues: list,
        board: Storyboard,
        compact_target: int,
        estimated_cost_cny: float,
    ):
        self.passed = passed
        self.errors = errors
        self.warnings = list(warnings or [])
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
) -> bool:
    """只允许人工确认已经停止写入且通过 Supervisor 门禁的完整分镜。"""
    if shot_count <= 0 or shot_count != planned_shots or not final_shot_valid:
        return False
    if episode["status"] == "confirmed":
        return not bool(episode["script_error"])
    if episode["status"] != "scripted" or episode["script_error"]:
        return False
    if checkpoint is not None:
        phase = str(getattr(checkpoint, "phase", "") or "")
        validated = int(getattr(checkpoint, "validated_prefix_end", 0) or 0)
        expected = int(getattr(checkpoint, "expected_total", 0) or planned_shots)
        return bool(
            phase == "SUCCEEDED"
            and validated == shot_count
            and expected == shot_count
        )
    # 兼容人工编辑后没有 Supervisor checkpoint 的既有分镜；完整门禁仍会在下方重算。
    return True


def _storyboard_confirmation_progress(episode, rows) -> dict:
    """Return the current terminal-state facts used by preview and submit."""
    from app.storyboard_supervisor import load_latest_checkpoint

    checkpoint = load_latest_checkpoint(episode["id"])
    outline_count = 0
    try:
        outline_count = len(
            json.loads(episode["storyboard_outline_json"] or "{}").get("shots") or []
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        outline_count = 0
    structural_draft = episode["storyboard_artifact_id"] is None and outline_count > 0
    planned = int(
        (outline_count if structural_draft else 0)
        or (checkpoint.expected_total if checkpoint else 0)
        or outline_count
        or len(rows)
    )
    board = _board_from_shot_rows(rows, episode["episode_no"])
    final_valid = bool(board.shots and board.shots[-1].is_final)
    return {
        "checkpoint": checkpoint,
        "planned_shots": planned,
        "board": board,
        "final_shot_valid": final_valid,
        "terminal": _is_storyboard_terminal_for_confirmation(
            episode,
            checkpoint,
            shot_count=len(rows),
            planned_shots=planned,
            final_shot_valid=final_valid,
        ),
    }


def _storyboard_structural_errors(storyboard: Storyboard) -> list[str]:
    errors: list[str] = []
    shots = list(storyboard.shots or [])
    if not shots:
        return ["本集还没有分镜"]
    seen: set[int] = set()
    for index, shot in enumerate(shots, start=1):
        shot_no = int(shot.shot_no or 0)
        if shot_no <= 0:
            errors.append(f"第 {index} 个镜头缺少有效 shot_no")
        elif shot_no in seen:
            errors.append(f"shot_no={shot_no} 重复")
        else:
            seen.add(shot_no)
        if shot_no and shot_no != index:
            errors.append(f"shot_no={shot_no} 与顺序 {index} 不一致")
        required = {
            "shot_size": shot.shot_size,
            "camera_move": shot.camera_move,
            "scene_name": shot.scene_name or shot.scene_setting,
            "action_desc": shot.action_desc,
            "first_frame_desc": shot.first_frame_desc,
            "last_frame_desc": shot.last_frame_desc,
            "source_excerpt": shot.source_excerpt,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            errors.append(f"第 {shot_no or index} 镜缺少必填字段：{', '.join(missing)}")
        if int(shot.duration_s or 0) <= 0:
            errors.append(f"第 {shot_no or index} 镜缺少有效 duration_s")
    return errors


def _storyboard_operational_projection_errors(
    storyboard: Storyboard,
    screenplay: EpisodeScreenplay,
) -> list[str]:
    """Validate legacy delivery IDs and adjacent-scene routing as hard structure."""
    from app.scene_contract import scene_name_of, scene_time_of
    from app.validators import _scene_time_changed

    legacy_event_ids = {
        str(event.event_id or "").strip()
        for event in (screenplay.events or [])
        if str(event.event_id or "").strip()
    }
    errors: list[str] = []
    for index, shot in enumerate(storyboard.shots):
        event_id = str(shot.story_event_id or "").strip()
        if event_id and event_id not in legacy_event_ids:
            errors.append(
                "[STORYBOARD_OPERATIONAL_EVENT_ID_INVALID] "
                f"第 {shot.shot_no} 镜 story_event_id=「{event_id}」"
                "未映射到 screenplay.events 的唯一事件 ID"
            )
        if index == 0:
            continue
        previous = storyboard.shots[index - 1]
        same_scene = (
            scene_name_of(previous) == scene_name_of(shot)
            and not _scene_time_changed(
                scene_time_of(previous),
                scene_time_of(shot),
            )
        )
        mode = str(shot.continuity_mode or "").strip()
        if same_scene and mode == "scene_change":
            errors.append(
                "[STORYBOARD_OPERATIONAL_CONTINUITY_INVALID] "
                f"第 {shot.shot_no} 镜与上一镜同场同时却使用 scene_change"
            )
        elif not same_scene and mode != "scene_change":
            errors.append(
                "[STORYBOARD_OPERATIONAL_CONTINUITY_INVALID] "
                f"第 {shot.shot_no} 镜已跨场或跨时却使用 {mode or '空模式'}"
            )
        if same_scene and mode != "scene_change" and shot.transition != "硬切":
            errors.append(
                "[STORYBOARD_OPERATIONAL_TRANSITION_INVALID] "
                f"第 {shot.shot_no} 镜同场切换必须使用硬切"
            )
    return errors


def evaluate_storyboard_for_confirmation(
    episode,
    storyboard: Storyboard,
    screenplay: EpisodeScreenplay | None,
    bible: Bible,
    *,
    has_real_bible: bool = True,
    target_duration_s: int | None = None,
    record_metrics: bool = True,
    allow_evidence_refinalize: bool = False,
) -> ConfirmationEvaluation:
    """与 confirm_episode_core 同源的只读确认评估（不写库）。

    Supervisor 与确认门必须共用此函数，避免「Supervisor 认为通过、确认门又用另一套规则失败」。
    """
    from app.evaluations.issues import issues_from_messages
    from app.harness.types import IssueSeverity
    from app.continuity import dialogue_framing_errors
    from app.validators import (
        prefer_default_shot_durations,
        score_storyboard_direction_readability,
        validate_storyboard_direction_contract,
        validate_storyboard_screenplay_scene_alignment,
    )

    # Evaluation is a read-only gate.  A shallow list copy still shares every
    # Shot instance with the caller, so normalizers could mutate the CAS
    # baseline while validating a repair candidate and create a false conflict.
    board = Storyboard.model_validate(storyboard.model_dump(mode="json"))
    narrative_plan = screenplay.narrative_plan if screenplay is not None else None
    character_changes = (
        [] if narrative_plan is not None else normalize_offbible_characters(board, bible)
    )
    if narrative_plan is None:
        normalize_continuity(board)
    prefer_default_shot_durations(
        board,
        narrative_authority=narrative_plan is not None,
        narrative_plan=narrative_plan,
    )
    normalize_transition_visuals(board)
    compact_target = _compact_episode_target(
        target_duration_s if target_duration_s is not None else episode["target_duration_s"]
    )
    actual_total = sum(int(s.duration_s or 0) for s in board.shots)
    compact_target = _compact_episode_target(actual_total or compact_target)

    structural_errors = _storyboard_structural_errors(board)
    outline = None
    if screenplay is not None and screenplay.narrative_plan is not None:
        structural_errors.extend(
            _storyboard_operational_projection_errors(
                board,
                screenplay,
            )
        )
        from app.identity_contracts import (
            IdentityContractError,
            storyboard_visual_identity_relation,
        )
        task_visible_ids: dict[int, list[str]] = {}
        try:
            from app.schemas import StoryboardOutline

            outline = StoryboardOutline.model_validate_json(
                episode["storyboard_outline_json"] or "{}"
            )
            task_visible_ids = {
                int(brief.shot_no): list(brief.visible_entity_ids)
                for brief in outline.shots
            }
        except (KeyError, IndexError, TypeError, ValueError):
            task_visible_ids = {}

        try:
            for shot in board.shots:
                relation = storyboard_visual_identity_relation(
                    shot,
                    task_visible_ids.get(
                        int(shot.shot_no),
                        list(shot.visible_entity_ids or []),
                    ),
                    bible,
                    screenplay,
                )
                unexpected = list(relation["unexpected_display_names"])
                binding_mismatches = list(
                    relation["identity_binding_mismatches"]
                )
                unresolved_tokens = [
                    *relation["unresolved_visible_tokens"],
                    *relation["unresolved_visible_entity_ids"],
                ]
                if unexpected or binding_mismatches or unresolved_tokens:
                    detail = (
                        f"可见身份 {unexpected} 不属于本镜叙事任务"
                        if unexpected
                        else (
                            "characters_visible 与 visible_entity_ids "
                            "未按同一身份、同一顺序绑定"
                        )
                    )
                    if unresolved_tokens:
                        detail += (
                            f"，包含未登记 token "
                            f"{list(dict.fromkeys(unresolved_tokens))}"
                        )
                    structural_errors.append(
                        "[SHOT_VISIBLE_IDENTITY_NOT_GROUNDED] "
                        f"第 {shot.shot_no} 镜{detail}"
                    )
        except IdentityContractError:
            # The existing narrative identity validator reports the complete
            # malformed-contract diagnostic.  Do not duplicate partial text.
            pass
        try:
            active_storyboard_run_id = episode["active_storyboard_run_id"]
            storyboard_artifact_id = episode["storyboard_artifact_id"]
            completion_certificate_id = episode[
                "storyboard_completion_certificate_id"
            ]
        except (KeyError, IndexError, TypeError):
            active_storyboard_run_id = getattr(
                episode,
                "active_storyboard_run_id",
                None,
            )
            storyboard_artifact_id = getattr(
                episode,
                "storyboard_artifact_id",
                None,
            )
            completion_certificate_id = getattr(
                episode,
                "storyboard_completion_certificate_id",
                None,
            )
        if (
            not active_storyboard_run_id
            and storyboard_artifact_id
            and completion_certificate_id
            and not allow_evidence_refinalize
        ):
            from app.production.certificate import (
                verify_current_storyboard_completion_authority,
            )

            try:
                verify_current_storyboard_completion_authority(
                    episode=episode,
                    current_storyboard_content=board.model_dump(mode="json"),
                )
            except ValueError as exc:
                structural_errors.append(
                    "[STORYBOARD_AUTHORITY_PROJECTION_DRIFT] "
                    f"当前正式镜头投影与已发布 Artifact/完成凭证不一致：{exc}"
                )
    stripped = sorted({
        str(change.get("stripped") or "").strip()
        for change in character_changes
        if str(change.get("stripped") or "").strip()
    })
    if stripped:
        structural_errors.append(
            "分镜残留未在剧本阶段解析的人物身份："
            + "、".join(stripped)
            + "；禁止确认和视频生产"
        )
    score_warnings: list[str] = []
    if screenplay is not None:
        structural_errors.extend(
            validate_storyboard_screenplay_scene_alignment(board, screenplay, bible)
        )
        if screenplay.narrative_plan is not None:
            from app.narrative import validate_storyboard_narrative

            try:
                raw_outline = episode["storyboard_outline_json"]
            except (KeyError, IndexError, TypeError):
                raw_outline = getattr(
                    episode,
                    "storyboard_outline_json",
                    None,
                )
            if raw_outline:
                try:
                    outline = StoryboardOutline.model_validate_json(
                        raw_outline
                    )
                except (TypeError, ValueError):
                    structural_errors.append(
                        "[STORYBOARD_OUTLINE_INVALID] 当前分镜大纲无法解析"
                    )
            score_warnings.extend(validate_storyboard_narrative(
                board,
                screenplay,
                outline=outline,
                complete=True,
                expected_scope_id=str(episode["id"]),
            ))
    if outline is not None:
        structural_errors.extend(
            validate_storyboard_direction_contract(board, outline)
        )
        score_warnings.extend(
            score_storyboard_direction_readability(board, outline)
        )
    score_warnings.extend(validate_storyboard(
        board,
        bible,
        compact_target,
        narrative_authority=narrative_plan is not None,
        narrative_plan=narrative_plan,
        screenplay=screenplay,
    ))
    dialogue_findings = [
        message
        for shot in board.shots
        for message in dialogue_framing_errors(
            shot,
            narrative_authority=narrative_plan is not None,
        )
    ]
    score_warnings.extend(dialogue_findings)
    if screenplay is not None:
        score_warnings.extend(validate_storyboard_soundtrack(board, screenplay, compact_target))
        score_warnings.extend(validate_storyboard_preserves_key_content(board, screenplay))
    if has_real_bible and not structural_errors:
        try:
            for s in board.shots:
                compile_prompt(
                    s.model_copy(deep=True),
                    bible,
                    screenplay=screenplay,
                )
        except Exception as exc:  # noqa: BLE001
            structural_errors.append(f"Prompt 编译失败：{exc}")
    try:
        ep_id = episode["id"]
    except Exception:  # noqa: BLE001
        ep_id = getattr(episode, "id", "") or ""
    _ = record_metrics
    issues = issues_from_messages(
        score_warnings,
        subject=f"episode:{ep_id}",
        severity=IssueSeverity.WARNING,
    )
    est = sum(shot_cost_cny(s.duration_s) for s in board.shots)
    return ConfirmationEvaluation(
        passed=not structural_errors,
        errors=structural_errors,
        warnings=score_warnings,
        issues=issues,
        board=board,
        compact_target=compact_target,
        estimated_cost_cny=round(est, 2),
    )


def _has_current_storyboard_completion_certificate(conn, episode) -> bool:
    data = dict(episode)
    certificate_id = data.get("storyboard_completion_certificate_id")
    artifact_id = data.get("storyboard_artifact_id")
    revision_id = data.get("storyboard_production_revision_id")
    if not certificate_id or not artifact_id or not revision_id:
        return False
    try:
        from app.production.screenplay_authority import resolve_downstream_screenplay

        screenplay_context = resolve_downstream_screenplay(
            str(data.get("id") or ""),
            conn=conn,
        )
        has_narrative_plan = screenplay_context.narrative_authority_required
    except Exception:  # noqa: BLE001 - immutable authority drift fails closed
        return False
    if has_narrative_plan:
        try:
            from app.production.certificate import (
                verify_current_storyboard_completion_authority,
            )

            shot_rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                (data.get("id"),),
            ).fetchall()
            current_board = _board_from_shot_rows(
                shot_rows,
                int(data.get("episode_no") or 1),
            )
            verify_current_storyboard_completion_authority(
                episode=episode,
                current_storyboard_content=current_board.model_dump(mode="json"),
            )
            return True
        except Exception:  # noqa: BLE001 - paid authority fast path fails closed
            return False

    # Explicit plan-null compatibility: retain the pre-narrative certificate
    # shape without imposing the new evaluator contract on legacy projects.
    try:
        row = conn.execute(
            """SELECT c.kind,c.scope_id,c.artifact_id,c.artifact_hash,c.blockers,
                      c.must_fix_issues,c.production_revision_id,c.consumed_at,
                      a.content_hash AS current_artifact_hash,
                      a.status AS current_artifact_status
                 FROM completion_certificates c
                 JOIN artifacts a ON a.id=c.artifact_id
                WHERE c.id=?""",
            (certificate_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001 - legacy databases use live diagnostics
        return False
    return bool(
        row
        and row["kind"] == "storyboard"
        and row["scope_id"] == data.get("id")
        and row["artifact_id"] == artifact_id
        and row["production_revision_id"] == revision_id
        and row["artifact_hash"] == row["current_artifact_hash"]
        and row["current_artifact_status"]
        not in {"stale", "rejected", "superseded", "needs_revision"}
        and int(row["blockers"] or 0) == 0
        and int(row["must_fix_issues"] or 0) == 0
        and row["consumed_at"] is not None
    )


def _restore_unconfirmed_storyboard_projection(
    conn,
    episode,
) -> Storyboard:
    """Restore mutable shots from the exact consumed release Artifact."""
    data = dict(episode)
    if data.get("status") != "scripted":
        raise ValueError("只有等待人工确认的分镜允许从发布 Artifact 恢复投影")
    artifact_id = str(data.get("storyboard_artifact_id") or "")
    certificate_id = str(data.get("storyboard_completion_certificate_id") or "")
    revision_id = str(data.get("storyboard_production_revision_id") or "")
    from app.production.certificate import verify_completion_certificate

    verify_completion_certificate(
        certificate_id,
        expected_kind="storyboard",
        expected_scope_id=str(data.get("id") or ""),
        expected_artifact_id=artifact_id,
        expected_production_revision_id=revision_id,
        allow_consumed=True,
    )
    artifact = evidence_repository.get_artifact(artifact_id)
    if artifact is None:
        raise ValueError("已签证 Storyboard Artifact 不存在")
    board = Storyboard.model_validate(artifact.get("content") or {})
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (data.get("id"),),
    ).fetchall()
    if len(rows) != len(board.shots):
        raise ValueError("当前 shots 行数与已签证 Storyboard Artifact 不一致")
    from app.storyboard_supervisor import _write_shot_fields

    conn.execute("BEGIN IMMEDIATE")
    try:
        for row, shot in zip(rows, board.shots):
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
    return board


def _assert_storyboard_generation_gate(episode_id: str) -> None:
    """Authorize paid work from a current certificate, never a live score.

    For explicit plan-null projects the historical live hard-gate fallback is
    retained.  Once a narrative plan exists, live evaluation is diagnostic
    only and cannot mint authority in place of immutable release evidence.
    """
    conn = get_conn()
    episode = _episode_or_404(episode_id)
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "本集尚无分镜")
    if _has_current_storyboard_completion_certificate(conn, episode):
        return
    screenplay = None
    screenplay_error = None
    narrative_authority = False
    try:
        from app.production.screenplay_authority import resolve_downstream_screenplay

        screenplay_context = resolve_downstream_screenplay(episode_id, conn=conn)
        screenplay = screenplay_context.screenplay
        narrative_authority = screenplay_context.narrative_authority_required
    except Exception as exc:  # noqa: BLE001 - malformed authority fails closed
        screenplay_error = exc
        from app.production.screenplay_authority import (
            episode_requires_immutable_screenplay_authority,
        )

        narrative_authority = episode_requires_immutable_screenplay_authority(
            episode, conn=conn,
        )
    narrative_authority = bool(
        narrative_authority
        or dict(episode).get("narrative_review_artifact_id")
        or dict(episode).get("narrative_status") == "ready"
    )
    if not narrative_authority and dict(episode).get("storyboard_completion_certificate_id"):
        try:
            from app.production.certificate import (
                completion_certificate_has_narrative_evidence,
            )

            narrative_authority = completion_certificate_has_narrative_evidence(
                dict(episode).get("storyboard_completion_certificate_id")
            )
        except Exception:  # noqa: BLE001 - live diagnostics below still fail malformed rows
            pass
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],),
    ).fetchone()
    try:
        if screenplay_error is not None:
            raise screenplay_error
        evaluation = evaluate_storyboard_for_confirmation(
            episode,
            _board_from_shot_rows(rows, episode["episode_no"]),
            screenplay,
            _project_bible_or_placeholder(project),
            has_real_bible=bool((project["bible_json"] or "").strip()) if project else False,
            record_metrics=False,
        )
        hard_errors = list(dict.fromkeys(evaluation.errors))
    except Exception as exc:  # legacy malformed rows must fail closed, not return HTTP 500
        hard_errors = [f"分镜结构无法通过确认评估：{exc}"]
    if narrative_authority:
        hard_errors.insert(
            0,
            "[NARRATIVE_CERTIFICATE_REQUIRED] 当前叙事分镜缺少或失去与发布 "
            "Artifact 精确绑定的完成凭证；实时评估仅用于诊断，不能授权付费生成",
        )
    hard_errors = list(dict.fromkeys(hard_errors))
    if hard_errors:
        raise HTTPException(409, {
            "code": "STORYBOARD_CONFIRMATION_REQUIRED",
            "message": f"当前分镜仍有 {len(hard_errors)} 个确认门禁问题，尚不能启动付费视频",
            "errors": hard_errors[:30],
            "recovery_action": "返回分镜台继续修复；全部硬门禁通过并重新确认后再生成视频",
            "episode_id": episode_id,
        })


def create_storyboard_confirmation_preview(episode_id: str) -> dict:
    """计算并签发人工确认快照。"""
    from app.storyboard_workspace import create_preview

    ep = _episode_or_404(episode_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "本集还没有分镜")
    progress = _storyboard_confirmation_progress(ep, rows)
    planned = progress["planned_shots"]
    board = progress["board"]
    final_valid = progress["final_shot_valid"]
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = _project_bible_or_placeholder(project)
    from app.production.screenplay_authority import resolve_downstream_screenplay

    try:
        screenplay = resolve_downstream_screenplay(episode_id, conn=conn).screenplay
    except ValueError as exc:
        # Preview is an authorization input.  A modern episode with a broken
        # immutable chain must not produce a new confirmation token from a
        # mutable page copy.
        raise HTTPException(409, f"当前剧本权威链无法验证：{exc}") from exc
    evaluation = evaluate_storyboard_for_confirmation(
        ep, board, screenplay, bible,
        has_real_bible=bool((project["bible_json"] or "").strip()) if project else False,
    )
    hard_errors = list(evaluation.errors)
    if not progress["terminal"]:
        hard_errors.insert(
            0,
            f"分镜尚未达到完整终态：已完成 {len(rows)}/{planned} 镜，最终镜{'有效' if final_valid else '缺失'}",
        )
    hard_errors = list(dict.fromkeys(hard_errors))
    warnings = list(dict.fromkeys(evaluation.warnings))
    payload = {
        "contract_version": "storyboard-confirm.v3",
        "episode_id": episode_id,
        "storyboard_artifact_id": ep["storyboard_artifact_id"],
        "shot_count": len(rows),
        "planned_shots": planned,
        "total_duration_s": sum(int(shot.duration_s or 0) for shot in evaluation.board.shots),
        "final_shot_valid": final_valid,
        "hard_gates": {
            "passed": not hard_errors,
            "errors": hard_errors,
            "retry_exhausted_fallback": False,
            "findings": [],
        },
        "warnings": warnings,
        "score_only": {
            "evaluation_role": "score_only",
            "runtime_blocking": False,
            "issue_count": len(evaluation.issues),
        },
        "estimated_video_cost_cny": {
            "min": evaluation.estimated_cost_cny,
            "max": evaluation.estimated_cost_cny,
            "note": "按当前服务端费率估算；确认不会自动提交付费视频",
        },
        "unlocks": [] if hard_errors else ["生成台", "付费视频生成入口"],
        "recovery_action": (
            "返回分镜台继续修复；全部硬门禁通过后再确认"
            if hard_errors else None
        ),
    }
    if hard_errors:
        try:
            from app.observability.metrics import inc
            inc("storyboard_confirm_preview_total", episode_id=episode_id, passed=False)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(409, detail=payload)
    preview_payload = create_preview("confirm", episode_id, payload)
    try:
        from app.observability.metrics import inc
        inc("storyboard_confirm_preview_total", episode_id=episode_id, passed=True)
    except Exception:  # noqa: BLE001
        pass
    return preview_payload


@router.post("/episodes/{episode_id}/confirm-preview")
def confirm_episode_preview(episode_id: str):
    return create_storyboard_confirmation_preview(episode_id)


def _converge_confirmed_storyboard_state(
    episode_id: str,
    *,
    active_storyboard_run_id: str | None,
) -> None:
    """把已确认的业务结果投影到 Supervisor/运行指针/授权状态。

    该收口同时用于首次确认与幂等重试，因此能自愈旧版留下的「已确认但
    active_storyboard_run_id 仍存在」状态。
    """
    conn = get_conn()
    # episode.active_* 只是快速指针，生成资格为了 fail-closed 还会反查
    # durable workflow_runs。服务重启恢复发生竞态时，可能留下一条没有
    # active 指针的 PAUSED_EXTERNAL 孤儿；若确认时只清指针，生成台会
    # 同时看到“已确认”与“分镜仍在运行”。人工确认是这些上游运行的
    # 最终终点，必须把未恢复的持久运行、步骤和供应商调用一并收口。
    active_statuses = tuple(sorted(evidence_repository.ACTIVE_RUN_STATUSES))
    marks = ",".join("?" for _ in active_statuses)
    stale_runs = conn.execute(
        f"""SELECT id FROM workflow_runs
              WHERE scope_type='episode' AND scope_id=?
                AND workflow_type IN ('screenplay','storyboard')
                AND status IN ({marks})
                AND recovered_by_run_id IS NULL""",
        (episode_id, *active_statuses),
    ).fetchall()
    stale_run_ids = [str(row["id"]) for row in stale_runs]
    if stale_run_ids:
        run_marks = ",".join("?" for _ in stale_run_ids)
        stamp = now()
        conn.execute(
            f"""UPDATE step_runs SET status='CANCELLED',
                       finished_at=COALESCE(finished_at,?),
                       exit_reason=COALESCE(exit_reason,'SUPERSEDED_BY_STORYBOARD_CONFIRMATION')
                   WHERE run_id IN ({run_marks})
                     AND status NOT IN ('SUCCEEDED','WARNING','FAILED','CANCELLED','SKIPPED')""",
            (stamp, *stale_run_ids),
        )
        conn.execute(
            f"""UPDATE provider_calls SET status='CANCELLED',
                       error=COALESCE(error,'SUPERSEDED_BY_STORYBOARD_CONFIRMATION')
                   WHERE run_id IN ({run_marks}) AND status='RUNNING'""",
            stale_run_ids,
        )
        conn.execute(
            f"""UPDATE workflow_runs SET status='CANCELLED',
                       failure_code='SUPERSEDED_BY_STORYBOARD_CONFIRMATION',
                       failure_message='分镜已经人工确认，旧上游运行已收口',
                       finished_at=COALESCE(finished_at,?), updated_at=?
                   WHERE id IN ({run_marks})""",
            (stamp, stamp, *stale_run_ids),
        )
    conn.execute(
        "UPDATE episodes SET script_error=NULL, active_storyboard_run_id=NULL WHERE id=?",
        (episode_id,),
    )
    conn.commit()
    try:
        from app.storyboard_supervisor import (
            SupervisorCheckpoint,
            load_latest_checkpoint,
            save_checkpoint,
        )

        checkpoint = load_latest_checkpoint(episode_id)
        if checkpoint is None:
            shot_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?",
                (episode_id,),
            ).fetchone()["c"])
            checkpoint = SupervisorCheckpoint(
                episode_id=episode_id,
                phase="SUCCEEDED",
                outcome="SUCCEEDED_READY_FOR_CONFIRM",
                validated_prefix_end=shot_count,
                next_shot_no=shot_count + 1,
                expected_total=shot_count,
            )
        elif (
            checkpoint.phase != "SUCCEEDED"
            or checkpoint.outcome != "SUCCEEDED_READY_FOR_CONFIRM"
        ):
            checkpoint.phase = "SUCCEEDED"
            checkpoint.outcome = "SUCCEEDED_READY_FOR_CONFIRM"
        # Confirmation is the terminal authority. The preceding run has
        # already been cancelled above, so a run-ownership fence would reject
        # this durable terminal projection.
        save_checkpoint(checkpoint)
    except Exception:  # noqa: BLE001 -- 业务确认已完成，辅助投影可在下次重试自愈
        pass
    try:
        from app.completion_grant import revoke_active_video_grants_for_episode

        revoke_active_video_grants_for_episode(episode_id)
    except Exception:  # noqa: BLE001 -- 不回滚已通过门禁的确认
        pass


def confirm_episode_core(
    episode_id: str,
    *,
    decided_by: str = "user",
    reason: str | None = None,
    preview_token: str | None = None,
) -> dict:
    already = _episode_or_404(episode_id)
    if already["status"] == "confirmed":
        return _confirm_episode_core_impl(
            episode_id,
            decided_by=decided_by,
            reason=reason,
            preview_token=preview_token,
            preview_validated=True,
        )
    from app.storyboard_workspace import require_preview

    conn = get_conn()
    claim = f"confirming:{int(now())}:{new_id('storyboard')}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        require_preview(
            preview_token,
            "confirm",
            episode_id,
            consume=True,
        )
        claimed = conn.execute(
            """UPDATE episodes SET active_storyboard_run_id=?
                 WHERE id=? AND active_storyboard_run_id IS ?""",
            (claim, episode_id, already["active_storyboard_run_id"]),
        )
        if claimed.rowcount != 1:
            raise HTTPException(409, "确认状态已被其他请求抢占，请刷新后重新预览")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    try:
        return _confirm_episode_core_impl(
            episode_id,
            decided_by=decided_by,
            reason=reason,
            preview_token=preview_token,
            preview_validated=True,
        )
    finally:
        cleanup = get_conn()
        cleanup.execute(
            "UPDATE episodes SET active_storyboard_run_id=NULL "
            "WHERE id=? AND active_storyboard_run_id=?",
            (episode_id, claim),
        )
        cleanup.commit()


def _confirm_episode_core_impl(
    episode_id: str,
    *,
    decided_by: str = "user",
    reason: str | None = None,
    preview_token: str | None = None,
    preview_validated: bool = False,
) -> dict:
    """人工确认门：只有整集硬门禁通过的分镜才能 confirmed。
    失败抛 ValueError（消息面向 UI）；供路由与 Supervisor 复用。
    """
    from app.storyboard_workspace import (
        require_preview,
        verify_or_bind_existing_excerpt,
    )

    already = _episode_or_404(episode_id)
    if already["status"] == "confirmed":
        if not already["screenplay_json"]:
            from app.production.screenplay_authority import (
                episode_requires_immutable_screenplay_authority,
            )

            if episode_requires_immutable_screenplay_authority(
                already,
                conn=get_conn(),
            ):
                raise ValueError(
                    "已确认剧本权威链的页面投影缺失，"
                    "不能降级后幂等重申确认"
                )
            has_narrative_plan = False
        else:
            try:
                from app.production.screenplay_authority import (
                    resolve_downstream_screenplay,
                )

                screenplay_context = resolve_downstream_screenplay(
                    episode_id,
                    conn=get_conn(),
                )
                has_narrative_plan = screenplay_context.narrative_authority_required
            except Exception as exc:  # noqa: BLE001 - confirmed authority is fail closed
                raise ValueError(
                    f"已确认剧本权威链已漂移，不能幂等重申确认：{exc}"
                ) from exc
        if (
            has_narrative_plan
            and not _has_current_storyboard_completion_certificate(get_conn(), already)
        ):
            raise ValueError(
                "已确认分镜的完成凭证或正式镜头投影已失效，"
                "不能幂等重申确认；请返回分镜台重新发布"
            )
        _converge_confirmed_storyboard_state(
            episode_id,
            active_storyboard_run_id=already["active_storyboard_run_id"],
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
    if not preview_validated:
        require_preview(preview_token, "confirm", episode_id, consume=True)
    ep = _episode_or_404(episode_id)
    conn = get_conn()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    has_real_bible = bool((p["bible_json"] or "").strip())
    bible = _project_bible_or_placeholder(p)
    from app.production.screenplay_authority import resolve_downstream_screenplay

    screenplay_context = resolve_downstream_screenplay(episode_id, conn=conn)
    screenplay = screenplay_context.screenplay
    narrative_authority = screenplay_context.narrative_authority_required
    compact_target = (
        int(ep["target_duration_s"] or 0)
        if narrative_authority
        else _compact_episode_target(ep["target_duration_s"])
    )
    shots_rows = conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    if not shots_rows:
        raise ValueError("本集还没有分镜脚本")
    progress = _storyboard_confirmation_progress(ep, shots_rows)
    if not progress["terminal"]:
        raise ValueError(
            "分镜尚未达到完整终态："
            f"已完成 {len(shots_rows)}/{progress['planned_shots']} 镜，"
            f"最终镜{'有效' if progress['final_shot_valid'] else '缺失'}"
        )
    verified_legacy_bindings: list[tuple[str, dict]] = []
    if not narrative_authority:
        for row in shots_rows:
            try:
                binding = verify_or_bind_existing_excerpt(
                    episode_id,
                    row["id"],
                    row["source_excerpt"] or "",
                    persist_legacy=False,
                )
                verified_legacy_bindings.append((str(row["id"]), binding))
            except HTTPException:
                pass
    # 来源回绑是后台审计 finding：可在 evidence evaluation 中追踪，但不属于
    # 用户可操作的质量建议，也不应污染确认后的 storyboard_warning。
    board = _board_from_shot_rows(shots_rows, ep["episode_no"])
    shots = board.shots
    if narrative_authority:
        from app.production.certificate import (
            verify_current_storyboard_completion_authority,
        )

        try:
            verify_current_storyboard_completion_authority(
                episode=ep,
                current_storyboard_content=board.model_dump(mode="json"),
            )
        except ValueError as exc:
            if "shots 投影" not in str(exc):
                raise
            board = _restore_unconfirmed_storyboard_projection(conn, ep)
            shots_rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                (episode_id,),
            ).fetchall()
            board = _board_from_shot_rows(shots_rows, ep["episode_no"])
            shots = board.shots
            verify_current_storyboard_completion_authority(
                episode=ep,
                current_storyboard_content=board.model_dump(mode="json"),
            )
    if shots and not shots[-1].is_final and not narrative_authority:
        shots[-1].is_final = True
    character_changes = []
    stripped = sorted({
        str(change.get("stripped") or "").strip()
        for change in character_changes
        if str(change.get("stripped") or "").strip()
    })
    if stripped:
        raise ValueError(
            "分镜残留未在剧本阶段解析的人物身份："
            + "、".join(stripped)
            + "；已停止确认，未删除人物或台词"
        )
    character_artifact_ids: list[str] = []
    normalized_fields_changed = False
    original_board_payload = board.model_dump(mode="json")
    evaluation = evaluate_storyboard_for_confirmation(
        ep, board, screenplay, bible,
        has_real_bible=has_real_bible,
        target_duration_s=compact_target,
    )
    confirmation_errors = list(dict.fromkeys(evaluation.errors))
    if confirmation_errors:
        raise ValueError(
            f"分镜确认门禁未通过（{len(confirmation_errors)} 项）："
            + "；".join(confirmation_errors[:5])
        )
    confirmation_warnings = list(dict.fromkeys(evaluation.warnings))
    board = evaluation.board
    normalized_fields_changed = board.model_dump(mode="json") != original_board_payload
    compact_target = evaluation.compact_target
    est = evaluation.estimated_cost_cny
    shots = board.shots

    # 人工镜头编辑会产生新的 shot artifact，但不会变更旧的
    # episode.storyboard_artifact_id。仅看“整集指针是否存在”会把旧整集
    # 快照再次发布，导致页面是新台词、证据链却仍指向被删台词的旧版。
    # 因此确认时必须比较“当前整板内容 hash”，任一镜人工修订都必须
    # 先派生新的整集证据，再通过人工确认门。
    storyboard_artifact_id = ep["storyboard_artifact_id"]
    content_hash = None
    current_board_payload = board.model_dump(mode="json")
    if narrative_authority:
        from app.narrative import storyboard_authority_projection

        current_board_payload = storyboard_authority_projection(board)
    current_board_hash = evidence_repository.content_hash(current_board_payload)
    stored_board_hash = None
    stored_board_payload = None
    if storyboard_artifact_id:
        stored = conn.execute(
            "SELECT content_hash,content_json FROM artifacts WHERE id=?",
            (storyboard_artifact_id,),
        ).fetchone()
        stored_board_hash = stored["content_hash"] if stored else None
        if stored and stored["content_json"]:
            try:
                stored_board_payload = json.loads(stored["content_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                stored_board_payload = None
    if narrative_authority and stored_board_payload is not None:
        stored_board_hash = evidence_repository.content_hash(
            storyboard_authority_projection(stored_board_payload)
        )
    storyboard_snapshot_changed = stored_board_hash != current_board_hash
    from app.domain.storyboard_ops import _storyboard_shot_evidence_requires_rebind
    storyboard_shot_evidence_changed = bool(
        not narrative_authority
        and _storyboard_shot_evidence_requires_rebind(conn, episode_id, board)
    )
    if narrative_authority and (
        not storyboard_artifact_id or storyboard_snapshot_changed
    ):
        raise ValueError(
            "当前叙事分镜不再等同已发布 Artifact，"
            "请生成修订候选并重新发布"
        )
    if (
        not narrative_authority
        and (
            character_artifact_ids
            or normalized_fields_changed
            or not storyboard_artifact_id
            or storyboard_snapshot_changed
            or storyboard_shot_evidence_changed
        )
    ):
        storyboard_artifact_id = _finalize_storyboard_evidence(episode_id, board)
    if not narrative_authority and compact_target != int(ep["target_duration_s"] or 0):
        conn.execute(
            "UPDATE episodes SET target_duration_s=? WHERE id=?",
            (compact_target, episode_id),
        )
    if storyboard_artifact_id:
        art = conn.execute(
            "SELECT content_hash FROM artifacts WHERE id=?", (storyboard_artifact_id,)
        ).fetchone()
        content_hash = art["content_hash"] if art else None

    if ep["status"] == "confirmed":
        if storyboard_artifact_id and content_hash:
            existing_gate = conn.execute(
                "SELECT id FROM gate_decisions WHERE artifact_id=? AND gate_key='storyboard' AND decision IN ('approve','approve_with_risk')",
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
        "SELECT id FROM gate_decisions WHERE artifact_id=? AND gate_key='storyboard' AND decision IN ('approve','approve_with_risk')",
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
                new_id("gate"), storyboard_artifact_id, "storyboard",
                "approve", decided_by,
                reason or (
                    "门禁重试耗尽后确认当前产物"
                    if confirmation_warnings else "确认当前分镜产物"
                ),
                now(),
            ),
        )
    active_storyboard_run_id = ep["active_storyboard_run_id"]
    conn.execute(
        "UPDATE episodes SET status='confirmed', script_error=NULL, storyboard_warning=?, "
        "active_storyboard_run_id=NULL WHERE id=?",
        (
            ("门禁重试耗尽后采用当前产物：" + "；".join(confirmation_warnings[:5]))[:800]
            if confirmation_warnings else None,
            episode_id,
        ),
    )
    conn.commit()
    if verified_legacy_bindings:
        from app.storyboard_workspace import persist_source_binding
        for shot_id, binding in verified_legacy_bindings:
            persist_source_binding(shot_id, binding)
    # 手动确认是 Supervisor 「已就绪待确认」状态的真正终点。
    _converge_confirmed_storyboard_state(
        episode_id,
        active_storyboard_run_id=active_storyboard_run_id,
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
        {
            "episode_id": episode_id,
            "preview_token": body.get("preview_token"),
            "reason": body.get("reason"),
        },
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
    worker.pause_episode_video_tasks(episode_id)
    try:
        result = worker.clear_episode_artifacts(episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _review_write_audit("artifacts.clear_episode", "episode", episode_id, new_state=result)
    return result


@router.post("/episodes/{episode_id}/videos/clear")
async def clear_episode_videos(episode_id: str):
    """Clear all shot videos in the episode while preserving reference images."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_episode_videos", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    snapshot = _review_upstream_snapshot(episode_id)
    if snapshot["active_upstream_runs"]:
        raise HTTPException(409, {
            "code": "UPSTREAM_RUN_ACTIVE",
            "message": "编剧或分镜任务仍在写入，不能清空视频",
            "active_runs": snapshot["active_upstream_runs"],
        })
    await reset_video_completion_state(episode_id, reason="VIDEOS_CLEARED")
    worker.pause_episode_video_tasks(episode_id)
    try:
        result = worker.clear_episode_video_assets(episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _review_write_audit("artifacts.clear_episode_videos", "episode", episode_id, new_state=result)
    return result


async def reset_video_completion_state(episode_id: str, *, reason: str = "CANCELLED") -> dict:
    """停止全片补齐 Supervisor，并把集级补齐状态复位，避免生成台死锁。"""
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
    worker.stop_shot_video_tasks(shot_id)
    try:
        result = worker.clear_shot_artifacts(shot_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _review_write_audit("artifacts.clear_shot", "shot", shot_id, new_state=result)
    return result


def _shot_clear_context(shot_id: str):
    conn = get_conn()
    shot = conn.execute("SELECT id, episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    snapshot = _review_upstream_snapshot(shot["episode_id"])
    if snapshot["active_upstream_runs"]:
        raise HTTPException(409, {
            "code": "UPSTREAM_RUN_ACTIVE",
            "message": "编剧或分镜任务仍在写入，不能清空资产",
            "active_runs": snapshot["active_upstream_runs"],
        })
    return conn, shot


@router.post("/shots/{shot_id}/references/clear")
async def clear_shot_references(shot_id: str):
    """Clear this shot's generated images without touching its videos."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_shot_references", {"shot_id": shot_id})
    if routed is not None:
        return routed
    conn, shot = _shot_clear_context(shot_id)
    active = conn.execute(
        """SELECT COUNT(*) AS c FROM jobs WHERE shot_id=? AND kind='video'
           AND status IN ('queued','running','waiting_provider','waiting_retry','paused')""",
        (shot_id,),
    ).fetchone()["c"]
    if active:
        raise HTTPException(409, "本镜仍有生成任务，请先停止整集任务再清空参考图")
    try:
        result = worker.clear_shot_reference_assets(shot_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _review_write_audit("artifacts.clear_shot_references", "shot", shot_id, new_state=result)
    return result


@router.post("/shots/{shot_id}/videos/clear")
async def clear_shot_videos(shot_id: str):
    """Clear this shot's videos while preserving its reference images."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_shot_videos", {"shot_id": shot_id})
    if routed is not None:
        return routed
    _, shot = _shot_clear_context(shot_id)
    worker.stop_shot_video_tasks(shot_id)
    try:
        result = worker.clear_shot_video_assets(shot_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _review_write_audit("artifacts.clear_shot_videos", "shot", shot_id, new_state=result)
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
        if use and str(target.get("type") or "") == "plot_key_frame":
            # 只有用户明确恢复/保留关键帧才允许跨 prompt 合同复用；
            # 编辑场景图/人物图不应让旧关键帧永久绕过升级。
            meta["reference_gallery_contract_override"] = True
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


# ----- 版本化三模式视频计划与能力 -----


@router.post("/episodes/{episode_id}/video-generation-plan")
async def create_episode_video_generation_plan(
    episode_id: str,
    body: dict | None = None,
):
    _episode_or_404(episode_id)
    _assert_storyboard_generation_gate(episode_id)
    from app.video_plan import VideoPlanValidationError, generate_episode_plan

    try:
        plan = await generate_episode_plan(
            episode_id, force=bool((body or {}).get("force")),
        )
    except VideoPlanValidationError as exc:
        raise HTTPException(409, {
            "status": "BLOCKED_UPSTREAM_CONTRACT",
            "blockers": exc.issues,
        }) from exc
    return plan.model_dump(mode="json")


@router.get("/episodes/{episode_id}/video-generation-plan")
def get_episode_video_generation_plan(episode_id: str):
    _episode_or_404(episode_id)
    from app.video_plan import load_latest_plan

    plan = load_latest_plan(episode_id)
    if not plan:
        return None
    return plan.model_dump(mode="json")


@router.post("/episodes/{episode_id}/video-generation-plan/validate")
def validate_episode_video_generation_plan(episode_id: str):
    _episode_or_404(episode_id)
    from app.video_plan import (
        VideoPlanValidationError,
        capability_snapshot_by_id,
        current_storyboard_release_manifest,
        load_latest_plan,
        validate_episode_plan,
    )

    conn = get_conn()
    plan = load_latest_plan(episode_id, conn=conn)
    if not plan:
        raise HTTPException(404, "本集尚未生成视频模式计划")
    snapshot = capability_snapshot_by_id(plan.capability_snapshot_id, conn=conn)
    if not snapshot:
        raise HTTPException(409, "计划引用的供应商能力快照不存在")
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    try:
        release_manifest = current_storyboard_release_manifest(
            episode_id,
            conn=conn,
        )
        validate_episode_plan(
            plan,
            list(rows),
            snapshot,
            release_manifest=release_manifest,
        )
    except (ValueError, VideoPlanValidationError) as exc:
        blockers = (
            exc.issues
            if isinstance(exc, VideoPlanValidationError)
            else [{"code": "STORYBOARD_RELEASE_AUTHORITY_STALE", "message": str(exc)}]
        )
        raise HTTPException(409, {"valid": False, "blockers": blockers}) from exc
    return {"valid": True, "plan": plan.model_dump(mode="json")}


@router.post("/episodes/{episode_id}/video-generation-plan/reconcile")
def reconcile_episode_video_generation_plan(
    episode_id: str,
    body: dict | None = None,
):
    _episode_or_404(episode_id)
    from app.video_plan import reconcile_adopted_revision

    conn = get_conn()
    payload = body or {}
    shot_id = payload.get("shot_id")
    version_id = payload.get("adopted_version_id")
    if shot_id:
        row = conn.execute(
            "SELECT adopted_version_id FROM shots WHERE id=? AND episode_id=?",
            (shot_id, episode_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "镜头不存在")
        adopted = version_id or row["adopted_version_id"]
        if not adopted:
            raise HTTPException(409, "该镜头尚未采用视频")
        result = reconcile_adopted_revision(shot_id, adopted, conn=conn)
        conn.commit()
        return result
    results = []
    for row in conn.execute(
        """SELECT id,adopted_version_id FROM shots
           WHERE episode_id=? AND adopted_version_id IS NOT NULL ORDER BY shot_no""",
        (episode_id,),
    ).fetchall():
        results.append(reconcile_adopted_revision(
            row["id"], row["adopted_version_id"], conn=conn,
        ))
    conn.commit()
    return {
        "episode_id": episode_id,
        "bound": sum(item["bound"] for item in results),
        "stale_shot_ids": sorted({
            shot for item in results for shot in item["stale_shot_ids"]
        }),
    }


@router.post("/episodes/{episode_id}/video-generation-plan/override")
def override_episode_video_generation_plan(
    episode_id: str,
    body: dict | None = None,
):
    _episode_or_404(episode_id)
    from app.video_plan import (
        PlanAssetRequirement,
        VideoGenerationMode,
        VideoInputIntent,
        VideoPlanValidationError,
        capability_snapshot_by_id,
        current_storyboard_release_manifest,
        load_latest_plan,
        publish_plan,
        validate_episode_plan,
    )

    payload = body or {}
    if not payload.get("reason"):
        raise HTTPException(422, "人工覆盖必须填写原因")
    conn = get_conn()
    current = load_latest_plan(episode_id, conn=conn)
    if not current:
        raise HTTPException(404, "本集尚未生成视频模式计划")
    target = next(
        (item for item in current.shots if item.shot_id == payload.get("shot_id")),
        None,
    )
    if not target:
        raise HTTPException(404, "待覆盖镜头不属于当前计划")
    try:
        override_mode = VideoGenerationMode(payload.get("mode"))
        override_intent = (
            VideoInputIntent(payload["video_input_intent"])
            if payload.get("video_input_intent") else None
        )
        override_assets = [
            PlanAssetRequirement.model_validate(asset)
            for asset in (payload.get("required_assets") or [])
        ]
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"人工覆盖模式或素材合同无效：{exc}") from exc
    next_revision = int(conn.execute(
        "SELECT COALESCE(MAX(plan_revision),0)+1 n FROM episode_video_generation_plans WHERE episode_id=?",
        (episode_id,),
    ).fetchone()["n"])
    replacement = current.model_copy(deep=True)
    replacement.episode_video_plan_id = new_id("evp")
    replacement.plan_revision = next_revision
    replacement.status = "draft"
    replacement.created_at = now()
    for item in replacement.shots:
        item.shot_plan_id = new_id("svp")
        item.episode_video_plan_id = replacement.episode_video_plan_id
        item.plan_revision = next_revision
        if item.shot_id != target.shot_id:
            continue
        item.mode = override_mode
        item.planned_mode = item.mode
        item.video_input_intent = override_intent
        item.depends_on_shot_id = payload.get("depends_on_shot_id")
        item.required_assets = override_assets
        item.reason_codes = [
            *item.reason_codes,
            "MANUAL_OPERATION_OVERRIDE",
        ]
        item.input_revision_fingerprints["manual_override_reason"] = _review_sha(
            payload["reason"]
        )
    snapshot = capability_snapshot_by_id(
        replacement.capability_snapshot_id, conn=conn,
    )
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    try:
        if not snapshot:
            raise VideoPlanValidationError([{"code": "CAPABILITY_SNAPSHOT_MISSING"}])
        release_manifest = current_storyboard_release_manifest(
            episode_id, conn=conn,
        )
        validate_episode_plan(
            replacement,
            list(rows),
            snapshot,
            release_manifest=release_manifest,
        )
    except (ValueError, VideoPlanValidationError) as exc:
        issues = exc.issues if isinstance(exc, VideoPlanValidationError) else [{"code": str(exc)}]
        raise HTTPException(409, {"valid": False, "blockers": issues}) from exc
    publish_plan(replacement, conn=conn)
    conn.commit()
    return replacement.model_dump(mode="json")


@router.post(
    "/episodes/{episode_id}/video-generation-plan/{plan_id}/execute"
)
async def execute_episode_video_generation_plan(
    episode_id: str,
    plan_id: str,
    body: dict | None = None,
):
    from app.video_plan import load_latest_plan

    plan = load_latest_plan(episode_id)
    if not plan or plan.episode_video_plan_id != plan_id or plan.status != "valid":
        raise HTTPException(409, "只能执行当前有效的视频模式计划")
    from app.capabilities.dispatch import dispatch, respond_ui
    payload = body or {}
    result = await dispatch(
        "video.generate_episode",
        {
            "episode_id": episode_id,
            "idempotency_key": payload.get("idempotency_key"),
            "request_id": payload.get("request_id"),
            "approval_token": payload.get("approval_token"),
        },
        initiator="ui",
    )
    return respond_ui(result)


@router.get("/video-capabilities/{provider}/{model:path}")
def get_video_capabilities(provider: str, model: str):
    from app.video_plan import current_capability_snapshot

    return current_capability_snapshot(
        provider=provider, model=model,
    ).model_dump(mode="json")


@router.post("/video-capabilities/{provider}/{model:path}/probe")
async def probe_video_capability(
    provider: str,
    model: str,
    body: dict | None = None,
):
    from app import hiagent
    from app.video_plan import (
        ProviderVideoCapabilitySnapshot,
        current_capability_snapshot,
        save_capability_snapshot,
    )

    payload = body or {}
    if payload.get("confirm") is not True:
        raise HTTPException(409, "能力探针会创建真实付费任务，必须显式提交 confirm=true")
    capability = str(payload.get("capability") or "")
    base = current_capability_snapshot(provider=provider, model=model)
    image_urls: list[tuple[str, str]] = []
    video_urls: list[tuple[str, str]] = []
    if capability == "reference_image":
        image_urls = [(str(payload.get("reference_image_url") or ""), "reference_image")]
    elif capability == "first_last_pair":
        image_urls = [
            (str(payload.get("first_frame_url") or ""), "first_frame"),
            (str(payload.get("last_frame_url") or ""), "last_frame"),
        ]
    elif capability in {"reference_video", "true_video_continuation"}:
        video_urls = [(str(payload.get("reference_video_url") or ""), "reference_video")]
    else:
        raise HTTPException(422, "未知能力探针类型")
    if any(not url.strip() for url, _role in [*image_urls, *video_urls]):
        raise HTTPException(422, "能力探针缺少对应的受控输入素材 URL")
    task_id = None
    result = None
    provider_error = None
    try:
        task_id = await hiagent.create_video_task(
            str(payload.get("prompt") or "受控能力探针：保持输入主体、场景与画面风格。"),
            image_urls=image_urls,
            video_urls=video_urls,
            return_last_frame=bool(payload.get("return_last_frame")),
            call_meta={"stage": "provider_video_capability_probe", "capability": capability},
        )
        deadline = time.time() + float(payload.get("timeout_s") or 1800)
        while time.time() < deadline:
            result = await hiagent.poll_video_task(
                task_id,
                call_meta={"stage": "provider_video_capability_probe", "capability": capability},
            )
            if result["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(float(payload.get("poll_interval_s") or 10))
    except Exception as exc:
        provider_error = exc
    technical_success = bool(
        not provider_error and result and result.get("status") == "succeeded"
    )
    failure_reason = (
        f"{type(provider_error).__name__}:{provider_error}"
        if provider_error else (result or {}).get("error") or "timeout"
    )
    values = base.model_dump(mode="json")
    values.update({
        "id": new_id("cap"),
        "provider": provider,
        "model": model,
        "probe_time": now(),
        "probe_task_id": task_id,
        "probe_result": (
            "succeeded" if technical_success else f"failed:{failure_reason}"
        ),
        "technical_success": technical_success,
    })
    if payload.get("return_last_frame"):
        values["supports_return_last_frame"] = bool(
            technical_success and (result or {}).get("last_frame_url")
        )
    if capability == "reference_image":
        values["supports_reference_image"] = technical_success
    elif capability == "first_last_pair":
        values["supports_first_frame"] = technical_success
        values["supports_last_frame"] = technical_success
        values["supports_first_last_pair"] = technical_success
    elif capability == "reference_video":
        values["supports_reference_video"] = technical_success
    else:
        semantic_success = bool(
            technical_success
            and payload.get("semantic_regression_passed") is True
            and int(payload.get("semantic_sample_count") or 0) >= 20
        )
        values["supports_true_video_continuation"] = semantic_success
        values["semantic_continuation_success"] = semantic_success
    snapshot = ProviderVideoCapabilitySnapshot.model_validate(values)
    save_capability_snapshot(snapshot)
    if provider_error:
        raise HTTPException(
            409,
            {
                "message": f"能力探针失败：{provider_error}",
                "capability_snapshot_id": snapshot.id,
            },
        ) from provider_error
    return snapshot.model_dump(mode="json")


@router.post("/provider-media-publications")
async def create_provider_media_publication(body: dict | None = None):
    from app.video_plan import ProviderMediaPublicationService

    payload = body or {}
    try:
        return await ProviderMediaPublicationService().publish(
            source_revision_id=str(payload.get("source_revision_id") or ""),
            source_url=payload.get("source_url"),
            local_path=payload.get("local_path"),
            expires_at=payload.get("expires_at"),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/jobs/{job_id}/video-mode-audit")
def get_job_video_mode_audit(job_id: str):
    from app.video_plan import mode_audit_for_job

    audit = mode_audit_for_job(job_id)
    if not audit:
        raise HTTPException(404, "视频任务不存在")
    return audit


# ----- 视频生成（三模式 AI 计划） -----

def _shot_by_no(episode_id: str, shot_no: int):
    return get_conn().execute(
        "SELECT id FROM shots WHERE episode_id=? AND shot_no=?", (episode_id, shot_no)).fetchone()


@router.post("/episodes/{episode_id}/generate")
async def generate_episode(episode_id: str, body: dict | None = None):
    """先生成并校验整集三模式计划，再按素材依赖 DAG 安全入队。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.generate_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    return await _generate_episode_core(episode_id, body or {})


def _adopt_reused_completed_version(
    conn,
    *,
    shot_id: str,
    version_id: str,
) -> bool:
    """Adopt an idempotently reused version only when it is deliverable."""
    version = conn.execute(
        """SELECT status,video_path,technical_validation_json
             FROM shot_versions
            WHERE id=? AND shot_id=?""",
        (version_id, shot_id),
    ).fetchone()
    if (
        not version
        or version["status"] != "succeeded"
        or not version["video_path"]
        or not Path(str(version["video_path"])).is_file()
    ):
        return False
    try:
        technical = json.loads(version["technical_validation_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if technical and not technical.get("passed"):
        return False
    updated = conn.execute(
        """UPDATE shots SET adopted_version_id=?
            WHERE id=? AND (adopted_version_id IS NULL OR adopted_version_id='')""",
        (version_id, shot_id),
    )
    return updated.rowcount == 1


async def _generate_episode_core(episode_id: str, body: dict) -> dict:
    """Create/reuse jobs for an episode; ``only_incomplete`` powers Continue."""
    ep = _episode_or_404(episode_id)
    qualification = _review_assert_positive_action(
        episode_id, body.get("qualification_version"),
    )
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise HTTPException(409, "分镜脚本未确认（先在工作台点击确认分镜）")
    _assert_storyboard_generation_gate(episode_id)
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
    from app.video_plan import (
        VideoPlanValidationError,
        generate_episode_plan,
        load_latest_plan,
        verify_episode_plan_is_current,
    )
    try:
        plan = load_latest_plan(episode_id, conn=conn)
        requested_plan_id = body.get("plan_id")
        if requested_plan_id:
            if not plan or plan.episode_video_plan_id != requested_plan_id:
                raise HTTPException(409, "请求执行的计划不是当前有效 revision")
            if not verify_episode_plan_is_current(plan, conn=conn):
                raise HTTPException(409, "请求执行的计划已不符合当前生成台输入策略，请重新生成计划")
        else:
            plan = await generate_episode_plan(
                episode_id, force=bool(body.get("force_replan")), conn=conn,
            )
    except VideoPlanValidationError as exc:
        raise HTTPException(409, {
            "status": "BLOCKED_UPSTREAM_CONTRACT",
            "blockers": exc.issues,
        }) from exc
    if not plan or plan.status != "valid":
        raise HTTPException(409, "视频模式计划尚未通过确定性校验")
    plan_by_shot = {item.shot_id: item for item in plan.shots}
    board = _board_from_shot_rows(shots_rows, ep["episode_no"])
    shots = []
    previous_shot = None
    previous_shot_row = None
    for idx, row in enumerate(shots_rows):
        current = board.shots[idx]
        shots.append({
            "row": dict(row),
            "shot": current,
            "prev": previous_shot,
            "prev_row": dict(previous_shot_row) if previous_shot_row is not None else None,
        })
        previous_shot = current
        previous_shot_row = row
    from_no = body.get("from_shot_no")
    if from_no is not None:
        try:
            from_no = int(from_no)
        except (TypeError, ValueError):
            pass
    if from_no:
        selected = []
        for s in shots:
            if s["row"]["shot_no"] == from_no:
                selected_ids = {s["row"]["id"]}
                changed = True
                while changed:
                    changed = False
                    for item in plan.shots:
                        if (
                            item.depends_on_shot_id in selected_ids
                            and item.shot_id not in selected_ids
                        ):
                            selected_ids.add(item.shot_id)
                            changed = True
                selected = [
                    item for item in shots if item["row"]["id"] in selected_ids
                ]
                break
        if not selected:
            raise HTTPException(404, f"未找到镜 {from_no}")
    else:
        selected = shots
    completed_count = 0
    if body.get("only_incomplete"):
        completed_ids = {
            row["id"] for row in conn.execute(
                """SELECT s.id FROM shots s
                   WHERE s.episode_id=? AND (
                       s.adopted_version_id IS NOT NULL OR EXISTS(
                           SELECT 1 FROM shot_versions v
                           WHERE v.shot_id=s.id AND v.status='succeeded'
                             AND v.video_path IS NOT NULL AND v.video_path!=''
                       )
                   )""",
                (episode_id,),
            ).fetchall()
        }
        completed_count = sum(1 for item in selected if item["row"]["id"] in completed_ids)
        selected = [item for item in selected if item["row"]["id"] not in completed_ids]
    # Quick generation must not create one doomed paid-version record per shot.
    # The completion supervisor owns the self-healing asset preparation path.
    from app.multiview import scan_episode_reference_asset_gaps
    from app.domain.common import _project_bible_or_placeholder
    from app.production.screenplay_authority import resolve_downstream_screenplay
    from app.schemas import EpisodeScreenplay

    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
    ).fetchone()
    bible = _project_bible_or_placeholder(project)
    projection = EpisodeScreenplay.model_validate_json(ep["screenplay_json"])
    try:
        screenplay = resolve_downstream_screenplay(episode_id, conn=conn).screenplay
    except ValueError as exc:
        if projection.narrative_plan is not None:
            raise HTTPException(
                409, f"当前叙事剧本权威链无法验证：{exc}",
            ) from exc
        screenplay = projection
    asset_gaps = scan_episode_reference_asset_gaps(
        project_id=ep["project_id"],
        episode_no=int(ep["episode_no"]),
        shots=[(item["row"]["id"], item["shot"]) for item in selected],
        bible=bible,
        screenplay=screenplay,
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
    results = []
    for s in selected:
        shot_plan = plan_by_shot.get(s["row"]["id"])
        if not shot_plan:
            results.append({
                "shot_id": s["row"]["id"],
                "error": "当前计划未覆盖该镜头，已阻止入队",
                "issue_codes": ["VIDEO_PLAN_SHOT_MISSING"],
            })
            continue
        after = shot_plan.depends_on_shot_id
        try:
            r = worker.enqueue_shot(
                s["row"]["id"], after_shot_id=after,
                dependency_snapshot=qualification,
            )
            # enqueue_shot also reports active/paused same-key jobs as reused.
            # Only an already deliverable completed version may be adopted here.
            if r.get("reused") and r.get("version_id"):
                _adopt_reused_completed_version(
                    conn,
                    shot_id=s["row"]["id"],
                    version_id=r["version_id"],
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
    return {
        "episode_video_plan_id": plan.episode_video_plan_id,
        "plan_revision": plan.plan_revision,
        "mode_distribution": {
            mode: sum(1 for item in plan.shots if item.mode.value == mode)
            for mode in (
                "REFERENCE_IMAGE_MODE",
                "FIRST_FRAME_MODE",
                "FIRST_LAST_FRAME_MODE",
                "VIDEO_INPUT_MODE",
            )
        },
        "critical_path_latency_ms": plan.critical_path_latency_ms,
        "estimated_cost": plan.estimated_cost,
        "enqueued": results,
        "skipped_completed": completed_count,
        "selected_shots": len(selected),
    }


async def _generate_shot_core(shot_id: str, body: dict) -> dict:
    """单镜生成视频的领域逻辑，供 REST 路由与 ``video.generate_shot`` Command Handler 共用。"""
    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise HTTPException(404, "镜头不存在")
    _assert_storyboard_generation_gate(shot_row["episode_id"])
    qualification = _review_assert_shot_positive(
        shot_id, body.get("qualification_version"),
    )
    # 按视频质检问题重生：取「当前采用版 / 最新成功版」的问题清单（必要时现场跑质检），
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
                """SELECT id FROM shot_versions
                   WHERE shot_id=? AND status='succeeded'
                     AND NOT (
                       json_valid(image_inputs)
                       AND COALESCE(json_extract(image_inputs,'$.delivery_fallback'),0)=1
                     )
                   ORDER BY version_no DESC LIMIT 1""",
                (shot_id,)).fetchone()
        if ref:
            critique = await worker.critique_version(ref["id"])
            critique_sources.append({"source": "video_qa", "version_id": ref["id"]})
    from app.video_plan import (
        VideoPlanValidationError,
        create_local_replan_revision,
        generate_episode_plan,
    )
    try:
        plan = await generate_episode_plan(shot_row["episode_id"], conn=conn)
        if (
            body.get("reroll")
            or body.get("with_critique")
            or body.get("prompt_override")
        ):
            replan_reason = (
                "critique_guided_redo"
                if body.get("with_critique")
                else (
                    "prompt_override_redo"
                    if body.get("prompt_override")
                    else "single_shot_reroll"
                )
            )
            plan = create_local_replan_revision(
                shot_id,
                reason=replan_reason,
                conn=conn,
            )
    except VideoPlanValidationError as exc:
        raise HTTPException(409, {
            "status": "BLOCKED_UPSTREAM_CONTRACT",
            "blockers": exc.issues,
        }) from exc
    shot_plan = next(
        (item for item in plan.shots if item.shot_id == shot_id),
        None,
    )
    if not shot_plan:
        raise HTTPException(409, "当前有效视频模式计划未覆盖该镜头")
    after = shot_plan.depends_on_shot_id
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
            "with_critique": bool(body.get("with_critique")),
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
    from app.video_playback import normalize_playback_rate

    version_id = body.get("version_id")
    try:
        playback_rate = normalize_playback_rate(body.get("playback_rate"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
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
        media_evidence.persist_candidate_observed_state_out(
            version_id,
            str(observed_state_out),
        )
    reason = str(body.get("reason") or "").strip()
    if len(reason) < 4:
        raise HTTPException(422, "请填写有效的采用理由（至少 4 个字，说明质量、成本或版本比较）")
    evidence_repository.commit_artifact(
        None,
        artifact["id"],
        [Evaluation(
            evaluator_type="human", evaluator_name=str(body.get("decided_by") or "user"),
            evaluator_version="1.0.0", status="passed", hard_gate_passed=True,
            score=100, evidence={
                "decision": "adopt", "reason": reason, "playback_rate": playback_rate,
            },
        )],
    )
    shot = conn.execute("SELECT episode_id, adopted_version_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    previous_rate = float(v["playback_rate"] or 1.0)
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.execute(
        "UPDATE shot_versions SET adoption_reason=?, playback_rate=? WHERE id=?",
        (reason, playback_rate, version_id),
    )
    conn.execute(
        """INSERT INTO gate_decisions(
               id, artifact_id, gate_key, decision, decided_by, reason, created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            new_id("gate"), artifact["id"], "video_adoption", "approve",
            str(body.get("decided_by") or "user"), reason, now(),
        ),
    )
    from app.video_plan import reconcile_adopted_revision
    reconcile_result = reconcile_adopted_revision(
        shot_id, version_id, conn=conn,
    )
    conn.commit()
    _review_write_audit(
        "video_version.adopt", "shot", shot_id, target_version=version_id,
        old_state={
            "adopted_version_id": shot["adopted_version_id"] if shot else None,
            "playback_rate": previous_rate,
        },
        new_state={"adopted_version_id": version_id, "playback_rate": playback_rate}, reason=reason,
        idempotency_key=body.get("idempotency_key"), request_id=body.get("request_id"),
    )
    if shot and (
        shot["adopted_version_id"] != version_id
        or abs(previous_rate - playback_rate) > 0.0001
    ):
        worker.invalidate_episode_final(shot["episode_id"])
    return {
        "adopted": version_id,
        "artifact_id": artifact["id"],
        "reason": reason,
        "playback_rate": playback_rate,
        "video_plan_reconcile": reconcile_result,
    }


@router.post("/shots/{shot_id}/adopt")
async def adopt_version(shot_id: str, body: dict):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch(
        "video.adopt_version",
        {
            "shot_id": shot_id, "version_id": body.get("version_id"), "reason": body.get("reason"),
            "playback_rate": body.get("playback_rate", 1.0),
            "qualification_version": body.get("qualification_version"),
            "idempotency_key": body.get("idempotency_key"), "request_id": body.get("request_id"),
        },
        initiator="ui",
    )
    return respond_ui(result)


def _cancel_shot_adoption_core(shot_id: str) -> dict:
    """保留真实模型候选，只取消本镜采纳关系；后续合成不得使用图片代替。"""
    conn = get_conn()
    shot = conn.execute(
        "SELECT id,episode_id,adopted_version_id FROM shots WHERE id=?", (shot_id,),
    ).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    previous = shot["adopted_version_id"]
    if not previous:
        return {"shot_id": shot_id, "previous_adopted_version_id": None, "adopted_version_id": None}
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (shot_id,))
    from app.video_plan import reconcile_adopted_revision
    reconcile_result = reconcile_adopted_revision(
        shot_id, "__unadopted__", conn=conn,
    )
    conn.commit()
    worker.invalidate_episode_final(shot["episode_id"])
    _review_write_audit(
        "video_version.cancel_adoption",
        "shot",
        shot_id,
        target_version=previous,
        old_state={"adopted_version_id": previous},
        new_state={"adopted_version_id": None},
        reason="用户取消采纳；保留真实模型候选，成片禁止使用图片或静音片段代替",
    )
    return {
        "shot_id": shot_id,
        "previous_adopted_version_id": previous,
        "adopted_version_id": None,
        "video_plan_reconcile": reconcile_result,
    }


@router.post("/shots/{shot_id}/adoption/cancel")
async def cancel_shot_adoption(shot_id: str):
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("video.cancel_adoption", {"shot_id": shot_id})
    if routed is not None:
        return routed
    return _cancel_shot_adoption_core(shot_id)


@router.post("/episodes/{episode_id}/resume")
async def resume_episode(episode_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.resume_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    reset_result = None
    if (ep["video_completion_mode"] or "quick") == "complete":
        reset_result = await reset_video_completion_state(
            episode_id,
            reason="CONTINUED_AS_QUICK",
        )
    try:
        resumed = worker.resume_episode_video_tasks(episode_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if resumed.get("requires_provider_confirmation"):
        raise HTTPException(409, {
            "code": "PROVIDER_HANDLE_UNCONFIRMED",
            "message": "部分暂停任务可能已被供应商接单，系统未自动重复提交，以避免重复扣费",
            "recovery_action": resumed.get("recovery_action"),
            "unresolved_provider_jobs": resumed.get("unresolved_provider_jobs") or [],
            "episode_id": episode_id,
            "recoverable": True,
        })
    budget_resumed = worker.retry_paused(episode_id)
    generated = await _generate_episode_core(episode_id, {"only_incomplete": True})
    if (
        int(resumed.get("resumed_jobs") or 0) == 0
        and int(budget_resumed or 0) == 0
        and int(generated.get("selected_shots") or 0) == 0
        and not generated.get("enqueued")
    ):
        if reset_result is not None:
            return {
                **resumed,
                "budget_resumed_jobs": 0,
                "enqueued": [],
                "skipped_completed": int(generated.get("skipped_completed") or 0),
                "selected_shots": 0,
                "state_changed": True,
                "video_completion_mode": "quick",
                "supervisor_stopped": True,
                "cancelled_task": bool(reset_result.get("cancelled_task")),
                "message": "已停止全片补齐并切回快速模式；当前没有其他待继续任务",
            }
        raise HTTPException(409, {
            "code": "VIDEO_RESUME_EMPTY",
            "message": "当前没有可继续的视频任务",
            "recovery_action": "如需重新生成，请在生成台选择具体镜头或整集重新生成",
            "episode_id": episode_id,
            "recoverable": True,
            "state": {
                "resumed_jobs": 0,
                "budget_resumed_jobs": 0,
                "selected_shots": 0,
                "skipped_completed": int(generated.get("skipped_completed") or 0),
            },
        })
    return {
        **resumed,
        "budget_resumed_jobs": budget_resumed,
        "enqueued": generated["enqueued"],
        "skipped_completed": generated["skipped_completed"],
        "selected_shots": generated["selected_shots"],
        "state_changed": reset_result is not None,
        "video_completion_mode": "quick",
        "supervisor_stopped": reset_result is not None,
    }


@router.post("/episodes/{episode_id}/video/stop")
async def stop_episode_video(episode_id: str):
    """Pause the whole episode's video work; a later Continue can resume it."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.stop_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    if (ep["video_completion_mode"] or "quick") == "complete":
        await reset_video_completion_state(episode_id, reason="STOPPED")
    try:
        result = worker.pause_episode_video_tasks(episode_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    _review_write_audit("video.pause_episode", "episode", episode_id, new_state=result)
    return result


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
    from app.observability.tracing import bind_trace
    from app.video_supervisor import run_video_completion_resilient
    recorder.start()
    try:
        with bind_trace(recorder.run_id, None):
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
        if result.phase in {"SUCCEEDED_COVERED", "COMPLETED_DEADLINE_FALLBACK"}:
            recorder.succeed(result.outcome or "SUCCEEDED_COVERED")
        elif result.phase == "CANCELLED":
            recorder.cancel()
        else:
            coverage = result.coverage or {}
            completed_shots = int(coverage.get("adopted") or 0)
            total_shots = int(coverage.get("total") or 0)
            if result.finished_at is not None and total_shots > 0 and completed_shots == 0:
                recorder.fail_result(
                    result.outcome or result.phase,
                    failure_code="NO_COMPLETED_OUTPUT",
                )
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
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，全片视频补齐等待自动恢复")
        else:
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


async def _complete_episode_core(
    episode_id: str,
    body: dict,
    *,
    parent_run_id: str | None = None,
    trigger_type: str = "manual",
) -> dict:
    from app.completion_grant import (
        DEFAULT_VIDEO_BUDGET_CAP_CNY,
        DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
        GrantValidationError,
        default_max_fallback_shots,
        issue_video_completion_grant,
        bump_video_grant_budget,
        revoke_grant,
        validate_video_grant,
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
    _assert_storyboard_generation_gate(episode_id)
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
    if mode == "resume" and not grant_id:
        raise HTTPException(422, {
            "code": "VIDEO_COMPLETION_GRANT_REQUIRED",
            "message": "继续补齐必须携带原补齐授权；如需重新开始，请选择 fresh 模式",
            "action": "start_fresh",
        })

    # resume + 追加预算
    add_budget = _review_validate_authorization_number(
        body.get("add_budget_cny"), field="add_budget_cny", minimum=1, maximum=100000,
    )
    add_wall = _review_validate_authorization_number(
        body.get("add_wall_clock_s"), field="add_wall_clock_s", minimum=60, maximum=604800,
    )
    if (add_budget is not None or add_wall is not None) and not (mode == "resume" and grant_id):
        raise HTTPException(422, "追加授权只能用于带 completion_grant_id 的 resume 模式")
    existing = None
    if mode == "resume" and grant_id:
        try:
            existing = validate_video_grant(
                grant_id,
                episode_id=episode_id,
                storyboard_artifact_id=ep["storyboard_artifact_id"],
            )
            if add_budget is not None or add_wall is not None:
                existing = bump_video_grant_budget(
                    grant_id,
                    add_cny=float(add_budget or 0),
                    add_wall_s=float(add_wall or 0),
                )
        except GrantValidationError as exc:
            raise HTTPException(409, {
                "code": exc.code,
                "message": str(exc),
                "action": "renew_authorization",
                "completion_grant_id": grant_id,
            }) from exc

    issued_new_grant = False
    if mode == "fresh":
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
        issued_new_grant = True
        grant_id = grant.grant_id
        budget_cap = grant.budget_cap_cny
        wall_cap = grant.wall_clock_cap_s
        max_fallback = grant.max_fallback_shots
    else:
        if existing:
            budget_cap = existing.budget_cap_cny
            wall_cap = existing.wall_clock_cap_s
            max_fallback = existing.max_fallback_shots
            allow_fallback = existing.allow_fallback_adopt
            allow_edit = existing.allow_storyboard_edit

    active_run_states = {
        "CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN",
        "WAITING_AUTHORIZATION", "PAUSED_BUDGET", "PAUSED_EXTERNAL",
    }
    start_claim = f"starting:{int(now())}:{new_id('video_completion')}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        owner_row = conn.execute(
            "SELECT active_video_run_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        previous_active_run_id = owner_row["active_video_run_id"] if owner_row else None
        active_status = None
        previous_claim_live = False
        if str(previous_active_run_id or "").startswith("starting:"):
            try:
                claim_started_at = float(str(previous_active_run_id).split(":", 2)[1])
                previous_claim_live = now() - claim_started_at <= 60
            except (TypeError, ValueError, IndexError):
                previous_claim_live = False
        elif previous_active_run_id:
            active_row = conn.execute(
                "SELECT status FROM workflow_runs WHERE id=?",
                (previous_active_run_id,),
            ).fetchone()
            active_status = active_row["status"] if active_row else None
        if (
            previous_claim_live
            or active_status in active_run_states
        ):
            raise HTTPException(409, {
                "code": "VIDEO_COMPLETION_ALREADY_ACTIVE",
                "message": "全片补齐任务已在启动或运行，请勿重复提交",
                "active_run_id": previous_active_run_id,
                "action": "view_progress",
            })
        claimed = conn.execute(
            """UPDATE episodes SET active_video_run_id=?
               WHERE id=? AND active_video_run_id IS ?""",
            (start_claim, episode_id, previous_active_run_id),
        )
        if claimed.rowcount != 1:
            raise HTTPException(409, {
                "code": "VIDEO_COMPLETION_START_CONFLICT",
                "message": "本集补齐状态刚刚发生变化，请刷新后重试",
                "action": "refresh",
            })
        conn.commit()
    except Exception:
        conn.rollback()
        if issued_new_grant and grant_id:
            revoke_grant(grant_id)
        raise

    cap = float(budget_cap if budget_cap is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY)
    resolved_wall_cap = float(
        wall_cap if wall_cap is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    )
    try:
        recorder = WorkflowRecorder.create(
            workflow_type="episode_video_completion",
            scope_type="episode",
            scope_id=episode_id,
            input_fingerprint=fingerprint(
                ep["storyboard_artifact_id"], grant_id, mode,
            ),
            requested_by="user",
            trigger_type=trigger_type,
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
            parent_run_id=parent_run_id,
        )
    except Exception:
        conn.execute(
            """UPDATE episodes SET active_video_run_id=?
               WHERE id=? AND active_video_run_id=?""",
            (previous_active_run_id, episode_id, start_claim),
        )
        conn.commit()
        if issued_new_grant and grant_id:
            revoke_grant(grant_id)
        raise
    installed = conn.execute(
        """UPDATE episodes
           SET video_completion_mode='complete',
               status='generating',
               active_video_run_id=?
           WHERE id=? AND active_video_run_id=?""",
        (recorder.run_id, episode_id, start_claim),
    )
    if installed.rowcount != 1:
        conn.rollback()
        recorder.cancel("补齐启动权已变化，当前运行未启动")
        if issued_new_grant and grant_id:
            revoke_grant(grant_id)
        raise HTTPException(409, {
            "code": "VIDEO_COMPLETION_START_CONFLICT",
            "message": "本集补齐状态刚刚发生变化，请刷新后重试",
            "action": "refresh",
        })
    conn.commit()

    completion_coro = _recorded_video_completion_task(
        episode_id, recorder,
        resume=(mode == "resume"),
        grant_id=grant_id,
        budget_cap_cny=cap,
        wall_clock_cap_s=float(wall_cap) if wall_cap is not None else None,
        allow_fallback_adopt=bool(allow_fallback),
        max_fallback_shots=int(max_fallback) if max_fallback is not None else None,
        allow_storyboard_edit=allow_edit,
    )
    try:
        task_registry.spawn(
            "video_completion", episode_id, completion_coro,
            project_id=ep["project_id"],
        )
    except Exception as exc:
        completion_coro.close()
        try:
            recorder.start()
            recorder.fail(exc)
        except Exception as record_exc:  # noqa: BLE001
            errors.log_error(
                record_exc,
                action="video_completion_start_record_failed",
                context={"episode_id": episode_id, "run_id": recorder.run_id},
            )
        try:
            previous_mode = ep["video_completion_mode"] or "quick"
        except (KeyError, IndexError):
            previous_mode = "quick"
        conn.execute(
            """UPDATE episodes
               SET video_completion_mode=?,
                   status=?,
                   active_video_run_id=NULL
               WHERE id=? AND active_video_run_id=?""",
            (previous_mode, ep["status"], episode_id, recorder.run_id),
        )
        conn.commit()
        raise HTTPException(503, {
            "code": "VIDEO_COMPLETION_START_FAILED",
            "message": "全片补齐任务未能启动，尚未产生生成任务，可安全重试",
            "retryable": True,
            "completion_grant_id": grant_id,
            "run_id": recorder.run_id,
        }) from exc
    return {
        "status": "accepted",
        "run_id": recorder.run_id,
        "goal": "complete_episode_video",
        "completion_grant_id": grant_id,
        "resource_uri": f"manju://runs/{recorder.run_id}",
        "poll_url": f"/api/episodes/{episode_id}/video-completion",
        "message": "全片补齐任务已启动，可在生成台查看进度",
    }


def _video_completion_user_contract(
    episode_id: str,
    cp: Any,
    projection: dict[str, Any],
    *,
    running: bool,
) -> dict[str, Any]:
    phase = str(getattr(cp, "phase", "") or projection.get("phase") or "")
    checkpoint_run_id = getattr(cp, "run_id", None) or projection.get("run_id")
    run_id = (
        projection.get("active_video_run_id") or checkpoint_run_id
        if running else checkpoint_run_id
    )
    grant_id = getattr(cp, "grant_id", None) or projection.get("grant_id")
    base = f"/api/episodes/{episode_id}/video-completion"

    def action(action_id, label, method, endpoint, confirm=False):
        return {
            "id": action_id,
            "label": label,
            "method": method,
            "endpoint": endpoint,
            "requires_confirm": confirm,
        }

    def running_contract():
        actions = [action("view_progress", "查看进度", "GET", base)]
        if run_id:
            actions.append(action("pause", "暂停", "POST", f"/api/runs/{run_id}/pause", True))
        return {
            "user_state": "running",
            "message": "正在补齐全片视频，已完成内容会持续保留",
            "next_actions": actions,
        }

    active_run_id = projection.get("active_video_run_id")
    if running and (cp is None or (active_run_id and active_run_id != checkpoint_run_id)):
        return running_contract()
    active_run_status = str(projection.get("active_run_status") or "")
    if (
        not running
        and active_run_id
        and active_run_id != checkpoint_run_id
        and (
            str(active_run_id).startswith("starting:")
            or active_run_status in {
                "CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN",
                "WAITING_AUTHORIZATION", "PAUSED_BUDGET", "PAUSED_EXTERNAL",
            }
        )
    ):
        actions = [action("repair_preview", "查看恢复状态", "GET", f"{base}/repair-preview")]
        if not str(active_run_id).startswith("starting:"):
            actions.insert(0, action("open_run", "查看运行", "GET", f"/api/runs/{active_run_id}"))
        return {
            "user_state": "recovering",
            "message": "检测到未完成的补齐运行，系统正在恢复持久化进度",
            "next_actions": actions,
        }
    if cp is None:
        return {
            "user_state": "not_started",
            "message": "尚未开始全片视频补齐",
            "next_actions": [
                action("start_completion", "开始全片补齐", "POST", base, True),
            ],
        }
    if phase == "SUCCEEDED_COVERED":
        return {
            "user_state": "completed",
            "message": "全片视频已补齐",
            "next_actions": [
                action("view_results", "查看成片", "GET", f"/api/episodes/{episode_id}"),
            ],
        }
    if phase == "COMPLETED_DEADLINE_FALLBACK":
        return {
            "user_state": "completed",
            "message": "已按截止时间完成交差，部分镜头可能使用保底版本",
            "next_actions": [
                action("view_results", "查看结果", "GET", f"/api/episodes/{episode_id}"),
            ],
        }
    if phase == "PARTIAL_NO_USABLE_CANDIDATE":
        missing = len(projection.get("missing_shots") or [])
        suffix = f"，仍有 {missing} 个镜头未能生成技术可播版" if missing else ""
        return {
            "user_state": "failed",
            "message": f"确定性缺镜兜底遇到技术故障{suffix}，已保留所有现有结果",
            "next_actions": [
                action("repair_preview", "查看修复预演", "GET", f"{base}/repair-preview"),
                action("start_completion", "重新授权并补齐", "POST", base, True),
            ],
        }
    if phase == "FAILED_CLOSED":
        return {
            "user_state": "failed",
            "message": "全片补齐已安全停止，现有采用版不会丢失",
            "next_actions": [
                action("repair_preview", "查看修复预演", "GET", f"{base}/repair-preview"),
                action("start_completion", "重新授权并补齐", "POST", base, True),
            ],
        }
    if phase == "CANCELLED":
        return {
            "user_state": "cancelled",
            "message": "全片补齐已取消，已完成内容仍然保留",
            "next_actions": [
                action("start_completion", "重新开始", "POST", base, True),
            ],
        }
    if running:
        return running_contract()
    if phase in {"WAITING_AUTHORIZATION", "PAUSED_BUDGET"}:
        return {
            "user_state": "waiting_authorization",
            "message": "任务已暂停，需要追加授权或预算后继续",
            "next_actions": [{
                **action("authorize_continue", "追加授权并继续", "POST", base, True),
                "required_fields": ["add_budget_cny", "add_wall_clock_s"],
                "required_rule": "至少填写一项",
                "request_body": {
                    "mode": "resume",
                    "completion_grant_id": grant_id,
                },
            }, action("start_completion", "重新授权并开始", "POST", base, True)],
        }
    if phase in {"WAITING_HUMAN", "PAUSED_EXTERNAL", "WAITING_RETRY"}:
        actions = [action("repair_preview", "查看恢复预演", "GET", f"{base}/repair-preview")]
        if run_id:
            actions.insert(0, action("resume", "继续补齐", "POST", f"/api/runs/{run_id}/resume"))
        return {
            "user_state": "waiting_human",
            "message": "任务已暂停，检查评审意见或恢复条件后可继续",
            "next_actions": actions,
        }
    return {
        "user_state": "interrupted",
        "message": "补齐任务当前未运行，请先查看恢复预演再继续",
        "next_actions": [
            action("repair_preview", "查看恢复预演", "GET", f"{base}/repair-preview"),
            action("start_completion", "重新授权并补齐", "POST", base, True),
        ],
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
    active_run = conn.execute(
        "SELECT status FROM workflow_runs WHERE id=?",
        (proj["active_video_run_id"],),
    ).fetchone() if proj["active_video_run_id"] else None
    proj["active_run_status"] = active_run["status"] if active_run else None
    running = task_registry.active("video_completion", episode_id)
    proj["running"] = running
    proj.update(_video_completion_user_contract(
        episode_id, cp, proj, running=running,
    ))
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


def _persist_project_video_queue(run_id: str, state: dict) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE workflow_runs SET config_snapshot_json=?,updated_at=? WHERE id=?",
        (json.dumps({"queue_state": state}, ensure_ascii=False), now(), run_id),
    )
    conn.commit()


_project_video_queue_pause_requests: set[str] = set()
_PROJECT_VIDEO_CHILD_WAIT_STATUSES = {
    "CREATED",
    "RUNNING",
    "WAITING_RETRY",
    "WAITING_HUMAN",
    "WAITING_AUTHORIZATION",
    "PAUSED_BUDGET",
    "PAUSED_EXTERNAL",
}
_PROJECT_VIDEO_ITEM_SUCCESS_STATUSES = {
    "success",
    "finished",  # Compatibility with queue snapshots persisted before status propagation.
    "already_covered",
}


def request_project_video_queue_pause(project_id: str) -> None:
    _project_video_queue_pause_requests.add(project_id)


def clear_project_video_queue_pause(project_id: str) -> None:
    _project_video_queue_pause_requests.discard(project_id)


def _authoritative_project_video_child_run(run_id: str | None) -> dict | None:
    """Follow recovery links and return the latest persisted child attempt."""
    if not run_id:
        return None
    conn = get_conn()
    current_id = run_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        row = conn.execute(
            """SELECT id,status,failure_code,failure_message,recovered_by_run_id
               FROM workflow_runs WHERE id=?""",
            (current_id,),
        ).fetchone()
        if not row:
            return None
        current = dict(row)
        recovered_by = current.get("recovered_by_run_id")
        if not recovered_by:
            return current
        current_id = recovered_by
    return None


def _propagate_project_video_child_status(item: dict) -> None:
    child = _authoritative_project_video_child_run(item.get("run_id"))
    if child is None:
        item["status"] = "failed"
        item["error"] = "单集补齐运行记录缺失，无法确认完成状态"
        return

    child_status = str(child["status"] or "").upper()
    item["run_id"] = child["id"]
    item["child_run_status"] = child_status
    if child.get("failure_code"):
        item["child_failure_code"] = child["failure_code"]
    if child.get("failure_message"):
        item["child_message"] = child["failure_message"]

    if child_status == "SUCCEEDED":
        item["status"] = "success"
    elif child_status == "PARTIAL":
        item["status"] = "partial"
    elif child_status == "FAILED":
        item["status"] = "failed"
    elif child_status == "CANCELLED":
        item["status"] = "cancelled"
    elif child_status in _PROJECT_VIDEO_CHILD_WAIT_STATUSES:
        item["status"] = "waiting"
    else:
        item["status"] = "failed"
        item["error"] = f"单集补齐返回未知运行状态：{child_status or 'EMPTY'}"

    if item["status"] == "failed" and child.get("failure_message"):
        item["error"] = str(child["failure_message"])[:500]


def _finish_project_video_completion_queue(plan: list[dict], recorder) -> None:
    from app.evidence import repository as evidence_repository
    from app.orchestration.state_machine import transition_run

    statuses = [str(item.get("status") or "") for item in plan]
    waiting_items = [item for item in plan if item.get("status") == "waiting"]
    if waiting_items:
        waiting_statuses = [
            str(item.get("child_run_status") or "")
            for item in waiting_items
        ]
        target = next(
            (
                status for status in waiting_statuses
                if status in _PROJECT_VIDEO_CHILD_WAIT_STATUSES
                and status not in {"CREATED", "RUNNING"}
            ),
            "WAITING_HUMAN",
        )
        source = next(
            (
                item for item in waiting_items
                if item.get("child_run_status") == target
            ),
            waiting_items[0],
        )
        message = f"项目补齐队列有 {len(waiting_items)} 集等待继续处理"
        transition_run(
            recorder.run_id,
            "RUNNING",
            target,
            message,
            failure_code=source.get("child_failure_code"),
        )
        evidence_repository.append_event(
            recorder.run_id,
            "PROJECT_VIDEO_QUEUE_WAITING",
            "warning",
            message,
            payload={"waiting": len(waiting_items), "status": target},
        )
        return

    unsuccessful = [
        status for status in statuses
        if status not in _PROJECT_VIDEO_ITEM_SUCCESS_STATUSES
    ]
    if not unsuccessful:
        recorder.succeed("项目补齐队列已全部处理")
        return
    if all(status == "cancelled" for status in unsuccessful) and all(
        status == "cancelled" for status in statuses
    ):
        recorder.cancel("项目补齐队列中的单集任务均已取消")
        return
    if statuses and all(status == "failed" for status in statuses):
        recorder.fail_result(
            f"项目补齐队列失败，{len(statuses)} 集均未完成",
            failure_code="PROJECT_VIDEO_CHILD_FAILED",
        )
        return
    recorder.partial(
        f"项目补齐队列已结束，{len(unsuccessful)} 集未成功完成"
    )


async def _run_project_video_completion_queue(
    project_id: str,
    state: dict,
    recorder,
) -> None:
    import asyncio

    plan = state.get("plan") or []
    recorder.start()
    _persist_project_video_queue(recorder.run_id, state)
    try:
        for item in plan:
            item_status = item.get("status")
            if item_status not in {
                "queued", "started", "waiting", "already_running",
                "failed_to_schedule",
            }:
                continue
            episode_id = item["episode_id"]
            if item_status == "already_running" and not item.get("run_id"):
                active_run = get_conn().execute(
                    "SELECT active_video_run_id FROM episodes WHERE id=?",
                    (episode_id,),
                ).fetchone()
                if active_run:
                    item["run_id"] = active_run["active_video_run_id"]
            # A recovered per-episode Supervisor always owns the episode first.
            while any(
                task_registry.active("video_completion", candidate["episode_id"])
                for candidate in plan
                if candidate.get("episode_id")
            ):
                await asyncio.sleep(5)
            if item_status in {"started", "waiting", "already_running"}:
                _propagate_project_video_child_status(item)
                _persist_project_video_queue(recorder.run_id, state)
                continue
            try:
                from app.video_supervisor import rebuild_coverage_ledger

                ledger = rebuild_coverage_ledger(episode_id)
                if ledger.covered_within_quota():
                    item["status"] = "success"
                    _persist_project_video_queue(recorder.run_id, state)
                    continue
            except Exception:  # noqa: BLE001
                pass
            room_now = max(
                0.0,
                float(state["global_budget_cap_cny"]) - _project_video_spent(project_id),
            )
            if room_now < 5:
                item["status"] = "skipped_budget"
                item["allocated_cny"] = 0
                _persist_project_video_queue(recorder.run_id, state)
                continue
            item["allocated_cny"] = min(float(item["allocated_cny"]), room_now)
            try:
                result = await _complete_episode_core(episode_id, {
                    "mode": "fresh",
                    "budget_cap_cny": item["allocated_cny"],
                    "wall_clock_cap_s": state["wall_clock_cap_s"],
                    "allow_fallback_adopt": state["allow_fallback_adopt"],
                    "allow_storyboard_edit": state["allow_storyboard_edit"],
                })
                item["status"] = "started"
                item["run_id"] = result.get("run_id")
                item["completion_grant_id"] = result.get("completion_grant_id")
                _persist_project_video_queue(recorder.run_id, state)
                while task_registry.active("video_completion", episode_id):
                    await asyncio.sleep(8)
                _propagate_project_video_child_status(item)
            except Exception as exc:  # noqa: BLE001
                item["status"] = "failed"
                item["error"] = str(exc)[:500]
            _persist_project_video_queue(recorder.run_id, state)
        _finish_project_video_completion_queue(plan, recorder)
    except asyncio.CancelledError:
        _persist_project_video_queue(recorder.run_id, state)
        pause_requested = project_id in _project_video_queue_pause_requests
        _project_video_queue_pause_requests.discard(project_id)
        if task_registry.shutdown_in_progress() or pause_requested:
            recorder.pause_external(
                "用户暂停，项目补齐剩余队列已保留"
                if pause_requested else "服务重启，项目补齐剩余队列等待自动恢复"
            )
            if pause_requested:
                conn = get_conn()
                conn.execute(
                    "UPDATE workflow_runs SET failure_code='USER_PAUSED' WHERE id=?",
                    (recorder.run_id,),
                )
                conn.commit()
        else:
            recorder.cancel("项目补齐队列已取消")
        raise
    except Exception as exc:
        _persist_project_video_queue(recorder.run_id, state)
        recorder.fail(exc)
        raise


def recover_project_video_completion_queues() -> int:
    from app.orchestration.engine import WorkflowRecorder

    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM workflow_runs
           WHERE workflow_type='project_video_completion_queue'
             AND status='PAUSED_EXTERNAL' AND failure_code='SERVICE_RESTART'
             AND recovered_by_run_id IS NULL ORDER BY updated_at"""
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["scope_id"]
        if task_registry.active("video_completion_project", project_id):
            continue
        recorder = None
        coro = None
        try:
            snapshot = json.loads(row["config_snapshot_json"] or "{}")
            state = snapshot["queue_state"]
            if not isinstance(state, dict) or not isinstance(state.get("plan"), list):
                raise ValueError("项目补齐恢复参数不完整")
            recorder = WorkflowRecorder.create(
                workflow_type="project_video_completion_queue",
                scope_type="project",
                scope_id=project_id,
                input_fingerprint=row["input_fingerprint"],
                requested_by="system",
                trigger_type="resume",
                policy_snapshot=json.loads(row["policy_snapshot_json"] or "{}"),
                config_snapshot={"queue_state": state},
                budget_limit_cny=row["budget_limit_cny"],
                parent_run_id=row["id"],
            )
            coro = _run_project_video_completion_queue(project_id, state, recorder)
            task_registry.spawn(
                "video_completion_project",
                project_id,
                coro,
                project_id=project_id,
            )
            resumed += 1
        except Exception as exc:  # noqa: BLE001
            if coro is not None:
                coro.close()
            if recorder is not None:
                try:
                    recorder.cancel("项目补齐队列恢复任务未能启动")
                except Exception:  # noqa: BLE001
                    pass
            errors.record_and_format(
                exc,
                action="project_video_completion_recovery",
                context={"project_id": project_id, "run_id": row["id"]},
            )
            conn.execute(
                """UPDATE workflow_runs
                   SET status='FAILED',failure_code='RECOVERY_START_FAILED',
                       failure_message='项目补齐队列恢复任务未能启动，可重新提交',updated_at=?
                   WHERE id=? AND status='PAUSED_EXTERNAL'""",
                (now(), row["id"]),
            )
            conn.commit()
            continue
    return resumed


async def _complete_project_videos_core(project_id: str, body: dict) -> dict:
    """全局预算编排：按 episode_no 顺序分配 per-episode cap，串行启动未覆盖集。"""
    from app.orchestration.engine import WorkflowRecorder, fingerprint

    conn = get_conn()
    project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, "项目不存在")
    active_queue = conn.execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='project_video_completion_queue'
             AND scope_type='project' AND scope_id=?
             AND recovered_by_run_id IS NULL
             AND status IN (
               'CREATED','RUNNING','WAITING_RETRY','WAITING_HUMAN',
               'WAITING_AUTHORIZATION','PAUSED_BUDGET','PAUSED_EXTERNAL'
             )
           ORDER BY updated_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    if task_registry.active("video_completion_project", project_id) or active_queue:
        raise HTTPException(409, {
            "code": "PROJECT_VIDEO_COMPLETION_ALREADY_ACTIVE",
            "message": "项目补齐队列已在运行或等待恢复，请查看现有进度",
            "run_id": active_queue["id"] if active_queue else None,
            "action": "view_progress",
        })

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
    rest: list[dict] = []
    project_queue_run_id = None

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
            queue_state = {
                "global_budget_cap_cny": global_cap,
                "per_episode_cap_cny": per_cap,
                "wall_clock_cap_s": wall_cap,
                "allow_fallback_adopt": allow_fallback,
                "allow_storyboard_edit": allow_edit,
                "plan": plan,
            }
            recorder = None
            chain_coro = None
            try:
                recorder = WorkflowRecorder.create(
                    workflow_type="project_video_completion_queue",
                    scope_type="project",
                    scope_id=project_id,
                    input_fingerprint=fingerprint(project_id, queue_state),
                    requested_by="user",
                    trigger_type="manual",
                    policy_snapshot={
                        "serial": True,
                        "global_budget_cap_cny": global_cap,
                        "per_episode_cap_cny": per_cap,
                    },
                    config_snapshot={"queue_state": queue_state},
                    budget_limit_cny=global_cap,
                )
                project_queue_run_id = recorder.run_id
                chain_coro = _run_project_video_completion_queue(
                    project_id, queue_state, recorder,
                )
                task_registry.spawn(
                    "video_completion_project", project_id, chain_coro, project_id=project_id,
                )
            except Exception as exc:
                if chain_coro is not None:
                    chain_coro.close()
                if recorder is not None:
                    try:
                        recorder.cancel("项目补齐队列未能启动")
                    except Exception:  # noqa: BLE001
                        pass
                for item in rest:
                    item["status"] = "failed_to_schedule"
                    item["error"] = "项目级排队任务未能启动，可重新提交项目补齐；已启动集不受影响"
                errors.record_and_format(
                    exc,
                    action="video_completion_project_spawn",
                    context={
                        "project_id": project_id,
                        "already_started_episode_ids": [item["episode_id"] for item in started],
                        "pending_episode_ids": [item["episode_id"] for item in rest],
                    },
                )

    return {
        "status": "accepted",
        "project_id": project_id,
        "global_budget_cap_cny": global_cap,
        "project_spent_cny": project_spent,
        "remaining_cny": remaining_global,
        "plan": plan,
        "started": started,
        "project_queue_active": bool(rest) and all(
            item.get("status") != "failed_to_schedule" for item in rest
        ),
        "project_queue_run_id": project_queue_run_id,
        "project_queue_poll_url": (
            f"/api/runs/{project_queue_run_id}" if project_queue_run_id else None
        ),
        "retryable_schedule_failures": [
            item["episode_id"] for item in plan if item.get("status") == "failed_to_schedule"
        ],
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
        return await asyncio.to_thread(worker.concatenate_episode, episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"ffmpeg 合成失败：{exc}")


@router.get("/episodes/{episode_id}/stale-assets-preview")
def stale_assets_preview(episode_id: str):
    """生成台：资产/分镜 stale 影响预览（只读）。"""
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    from app.domain.storyboard_ops import _shot_video_is_stale, _shot_adopted_assets_stale
    from app.video_cost_model import initial_shot_generation_cost
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
            "estimated_cost_cny": initial_shot_generation_cost(
                float(shot.get("duration_s") or 0)
            ),
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
