"""分镜确认落地（人工确认后把分镜状态收敛为已确认）。

从 app/domain/video_ops.py 按原样搬移；依赖 confirmation_eval 与 confirmation_gate。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    new_id,
    now,
)
from app.domain.common import (
    _as_body_dict,
    _compact_episode_target,
    _episode_or_404,
    _project_bible_or_placeholder,
    router,
)
from app.domain.storyboard_ops import (
    _board_from_shot_rows,
    _finalize_storyboard_evidence,
)
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation
from fastapi import (
    Body,
    HTTPException,
)

from .confirmation_eval import (
    _storyboard_confirmation_progress,
    evaluate_storyboard_for_confirmation,
)
from .confirmation_gate import (
    _has_current_storyboard_completion_certificate,
    _restore_unconfirmed_storyboard_projection,
)


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
        assert_storyboard_source_bindings_complete,
        require_preview,
        verify_or_bind_existing_excerpt,
    )

    already = _episode_or_404(episode_id)
    try:
        assert_storyboard_source_bindings_complete(
            episode_id,
            conn=get_conn(),
        )
    except ValueError as exc:
        raise ValueError(f"分镜确认被拒绝：{exc}") from exc
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
    outline_authority = None
    if narrative_authority:
        from app.storyboard_authority import (
            resolve_storyboard_outline_authority,
        )

        outline_authority = resolve_storyboard_outline_authority(
            episode_id,
            conn=conn,
        )
    compact_target = (
        outline_authority.authoritative_duration_s
        if outline_authority is not None
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
    if outline_authority is not None:
        from app.storyboard_authority import (
            assert_storyboard_matches_outline_authority,
        )

        assert_storyboard_matches_outline_authority(
            outline_authority,
            board,
        )
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
