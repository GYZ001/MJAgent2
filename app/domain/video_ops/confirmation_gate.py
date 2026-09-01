"""分镜确认预览与生成前置闸门（发布证书判定、未确认投影回滚）。

从 app/domain/video_ops.py 按原样搬移；依赖 confirmation_eval。
"""
from __future__ import annotations

from app.db import get_conn
from .source_coverage import storyboard_source_coverage_gap
from app.domain.common import (
    _episode_or_404,
    _project_bible_or_placeholder,
    router,
)
from app.domain.storyboard_ops import _board_from_shot_rows
from app.evidence import repository as evidence_repository
from app.schemas import Storyboard
from fastapi import HTTPException

from .confirmation_eval import (
    _storyboard_confirmation_progress,
    evaluate_storyboard_for_confirmation,
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


def _assert_episode_reference_assets_ready(conn, episode, rows) -> None:
    """本集用到的定妆照/场景图必须都已就绪，否则不得发起付费视频。

    判据复用 ``scan_episode_reference_asset_gaps``——整集入口用的就是它，不另写
    第二套"有没有图"的判断（本仓库已因两套判据分叉吃过亏）。
    """
    from app.multiview import scan_episode_reference_asset_gaps

    from app.domain.storyboard_ops import _board_from_shot_rows

    board = _board_from_shot_rows(rows, episode["episode_no"])
    gaps = scan_episode_reference_asset_gaps(
        project_id=episode["project_id"],
        episode_no=int(episode["episode_no"]),
        shots=list(zip([row["id"] for row in rows], board.shots, strict=False)),
        conn=conn,
    )
    if not gaps["blockers"]:
        return
    names = "、".join([
        *(f"人物「{name}」" for name in gaps["characters"]),
        *(f"场景「{name}」" for name in gaps["scenes"]),
    ]) or "本集生产资产"
    raise HTTPException(409, {
        "code": "REFERENCE_ASSETS_NOT_READY",
        "message": f"{names}的参考图还没生成好，现在发起视频会拿不到素材。"
                   "图片正在后台生成，稍等片刻再试；也可以在人物谱/场景库手动上传。",
        "recovery_action": "等待后台出图完成，或在人物谱/场景库手动补图",
        "episode_id": episode["id"],
    })


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
    from app.storyboard_workspace import (
        assert_storyboard_source_bindings_complete,
    )

    try:
        assert_storyboard_source_bindings_complete(
            episode_id,
            conn=conn,
        )
    except ValueError as exc:
        raise HTTPException(409, {
            "code": "STORYBOARD_SOURCE_BINDING_REQUIRED",
            "message": str(exc),
            "recovery_action": "返回分镜台补全原文绑定后重新发布",
            "episode_id": episode_id,
        }) from exc
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
    # 原文覆盖是独立于"这批镜头自身完不完整"的另一个问题，判据见 source_coverage：
    # 计划镜数本身就是 1 时 1/1 也判终态通过，而那一镜可能只绑了开头几百字。
    if (coverage_gap := storyboard_source_coverage_gap(conn, episode_id)) is not None:
        hard_errors.insert(0, coverage_gap)
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


def _assert_shot_generation_gate(episode_id: str) -> None:
    """单镜生成专用闸门：公共闸门 + 本集参考图必须已就绪。

    出图从映射台解耦到后台之后，发起付费视频时定妆照/场景图可能还没跑完，而
    单镜入口此前只查镜头存在、分镜确认与预算，缺图照样能发出付费调用。

    为什么不把这条加进公共的 _assert_storyboard_generation_gate：它被四个付费
    入口共用，其中"补齐到全片可用"本身就会先补素材再生成——那正是整集入口
    缺图时 409 消息里指的出路，把校验加进公共部分会把出路一起堵死。
    """
    _assert_storyboard_generation_gate(episode_id)
    conn = get_conn()
    episode = _episode_or_404(episode_id)
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    _assert_episode_reference_assets_ready(conn, episode, rows)
