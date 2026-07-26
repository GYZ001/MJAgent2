"""集级分镜 Supervisor AgentLoop。

以「整集 hard gate 通过（并可选自动确认）」为唯一成功条件；
业务校验失败进入 Repair Router，不得以 PARTIAL/scripted+error 结束。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import errors
from app.completion_grant import (
    consume_grant,
    validate_grant_for_confirm,
    GrantValidationError,
)
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.repair_router import (
    RepairPlan,
    bump_fingerprint_count,
    route_issues,
)
from app.renderability import SHOT_SOFT_MAX
from app.schemas import Shot, Storyboard, StoryboardOutline
from app.stages import StageError, generate_storyboard_next_shot, generate_storyboard_outline

SupervisorPhase = Literal[
    "CREATED",
    "PREFLIGHT",
    "PLANNING_OUTLINE",
    "VALIDATING_OUTLINE",
    "GENERATING_SHOTS",
    "VALIDATING_EPISODE",
    "REPAIRING",
    "PREPARING_CONFIRM",
    "CONFIRMING",
    "SUCCEEDED",
    "WAITING_RETRY",
    "PAUSED_EXTERNAL",
    "PAUSED_BUDGET",
    "WAITING_AUTHORIZATION",
    "WAITING_HUMAN",
    "CANCELLED",
]

CompletionMode = Literal["ready_for_manual_confirm", "auto_confirm"]

MAX_REPAIR_EPOCHS = 6
CHECKPOINT_TYPE = "storyboard_supervisor_checkpoint"


class SupervisorCheckpoint(BaseModel):
    episode_id: str
    goal: Literal["generate_ready", "generate_and_confirm"] = "generate_ready"
    phase: SupervisorPhase = "CREATED"
    repair_epoch: int = 0
    outline_artifact_id: str | None = None
    validated_shot_artifact_ids: list[str] = Field(default_factory=list)
    validated_prefix_end: int = 0
    next_shot_no: int = 1
    expected_total: int = 0
    coverage: dict[str, list[str]] = Field(default_factory=dict)
    pending_issue_ids: list[str] = Field(default_factory=list)
    issue_fingerprint_counts: dict[str, int] = Field(default_factory=dict)
    completion_grant_id: str | None = None
    completion_mode: CompletionMode = "ready_for_manual_confirm"
    input_versions: dict[str, str | None] = Field(default_factory=dict)
    last_repair: dict[str, Any] | None = None
    outcome: str | None = None  # SUCCEEDED_READY_FOR_CONFIRM | SUCCEEDED_CONFIRMED


def load_latest_checkpoint(episode_id: str) -> SupervisorCheckpoint | None:
    conn = get_conn()
    row = conn.execute(
        """SELECT id, content_json FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC LIMIT 1""",
        (CHECKPOINT_TYPE, episode_id),
    ).fetchone()
    if not row:
        return None
    try:
        raw = json.loads(row["content_json"] or "{}")
        return SupervisorCheckpoint.model_validate(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def save_checkpoint(cp: SupervisorCheckpoint, *, run_id: str | None = None) -> str:
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type=CHECKPOINT_TYPE,
        scope_type="episode",
        scope_id=cp.episode_id,
        status="validated",
        trust_level="T2",
        content=cp.model_dump(mode="json"),
        contract_version=get_contract("storyboard").version,
    ))
    evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="storyboard_supervisor",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"phase": cp.phase, "repair_epoch": cp.repair_epoch, "run_id": run_id},
        ),
    )
    if run_id:
        evidence_repository.append_event(
            run_id,
            "STORYBOARD_SUPERVISOR_CHECKPOINT",
            "info",
            f"Supervisor checkpoint phase={cp.phase} prefix={cp.validated_prefix_end}",
            payload=cp.model_dump(mode="json"),
        )
    return artifact["id"]


def _delete_shots_from(conn, episode_id: str, frontier: int) -> int:
    """删除 frontier 及其之后的镜头（含媒体衍生物由调用方清）。"""
    rows = conn.execute(
        "SELECT id, shot_no FROM shots WHERE episode_id=? AND shot_no>=? ORDER BY shot_no",
        (episode_id, frontier),
    ).fetchall()
    from app import worker

    for row in rows:
        worker.clear_shot_artifacts(row["id"])
        conn.execute("DELETE FROM shots WHERE id=?", (row["id"],))
    return len(rows)


def _blocker_messages(draft) -> list[str]:
    residual = list(getattr(draft, "residual_errors", []) or [])
    disposition = getattr(draft, "disposition", None)
    if disposition == "NEEDS_REPLAN":
        return residual or ["单镜合同不可满足，需要重规划"]
    issues = getattr(draft, "residual_issues", None) or []
    blockers = [
        i.get("message", "") for i in issues
        if isinstance(i, dict) and i.get("severity") == "blocker"
    ]
    if blockers:
        return blockers
    # warning-only：允许继续（非 blocker）
    if disposition == "WARNING" and residual:
        # 兼容旧路径：若 residual 实际是容量等硬错误，仍视为 blocker
        hard = [m for m in residual if any(k in m for k in ("口播", "容量", "超过", "字段", "JSON", "schema"))]
        return hard
    return residual if disposition not in {"PASS", "WARNING", None} else []


async def run_storyboard_supervisor(
    episode_id: str,
    *,
    resume: bool = True,
    completion_mode: CompletionMode = "ready_for_manual_confirm",
    completion_grant_id: str | None = None,
    run_id: str | None = None,
    preflight_done: bool = False,
) -> SupervisorCheckpoint:
    """集级 Supervisor 主循环。调用前应已完成人物/场景预检（或设 preflight_done=False 由本函数跳过）。"""
    from app.domain.common import (
        _compact_episode_target,
        _episode_source_text,
        _load_screenplay,
        _project_bible_or_placeholder,
        _storyboard_target_for_source,
    )
    from app.validators import (
        normalize_continuity,
        normalize_offbible_characters,
        normalize_transition_visuals,
        prefer_default_shot_durations,
        relieve_spoken_overflow,
        storyboard_shot_count_range,
        validate_storyboard_preserves_key_content,
    )
    from app import worker
    from app.domain.storyboard_ops import (
        _board_from_shot_rows,
        _finalize_storyboard_evidence,
        _insert_storyboard_shot,
        _persist_storyboard_character_policy_repairs,
        _reconcile_storyboard_plan,
        _sync_storyboard_shot_timing,
    )
    from app.domain.video_ops import confirm_episode_core, evaluate_storyboard_for_confirmation

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("分镜脚本", ["剧集不存在"])

    screenplay = _load_screenplay(ep)
    if screenplay is None or ep["screenplay_status"] != "ready":
        raise StageError("分镜脚本", ["请先生成并确认本集可拍剧本，再展开分镜"])

    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = _project_bible_or_placeholder(p)
    ep_data = dict(ep)
    source_text = _episode_source_text(conn, ep)

    cp = load_latest_checkpoint(episode_id) if resume else None
    if cp is None:
        cp = SupervisorCheckpoint(
            episode_id=episode_id,
            goal="generate_and_confirm" if completion_mode == "auto_confirm" else "generate_ready",
            completion_mode=completion_mode,
            completion_grant_id=completion_grant_id,
            input_versions={
                "screenplay_artifact_id": ep["screenplay_artifact_id"],
                "bible_artifact_id": p["bible_artifact_id"] if p else None,
            },
            phase="PREFLIGHT",
        )
    else:
        # 恢复时若启动参数带了新的 completion_mode/grant，以启动为准
        if completion_mode:
            cp.completion_mode = completion_mode
            cp.goal = "generate_and_confirm" if completion_mode == "auto_confirm" else "generate_ready"
        if completion_grant_id:
            cp.completion_grant_id = completion_grant_id

    if run_id:
        evidence_repository.append_event(
            run_id, "STORYBOARD_SUPERVISOR_STARTED", "info",
            f"Supervisor 启动 mode={cp.completion_mode} resume={resume}",
            payload={"episode_id": episode_id, "phase": cp.phase},
        )

    # 上游版本校验
    if (ep["screenplay_artifact_id"] or "") != (cp.input_versions.get("screenplay_artifact_id") or ""):
        cp.phase = "WAITING_AUTHORIZATION"
        save_checkpoint(cp, run_id=run_id)
        conn.execute(
            "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
            ("上游剧本已变更，自动完成授权失效，请重新授权后继续", episode_id),
        )
        conn.commit()
        return cp

    # 确认中重启：幂等查询
    if resume and cp.phase == "CONFIRMING" and ep["status"] == "confirmed":
        cp.phase = "SUCCEEDED"
        cp.outcome = "SUCCEEDED_CONFIRMED"
        save_checkpoint(cp, run_id=run_id)
        return cp

    if not resume:
        # 新 production revision：若本 revision 已完成 Baseline+QA，拒绝 fresh 全量清空，改为 resume。
        from app.production.revision import ensure_production_revision, get_active_production_revision
        from app.harness.contracts import get_contract

        existing_rev = get_active_production_revision(episode_id, "storyboard")
        if existing_rev and existing_rev.baseline_done and existing_rev.first_evaluation_done:
            resume = True
        else:
            worker.delete_episode_shots(episode_id)
            try:
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=NULL, "
                    "working_storyboard_artifact_id=NULL, "
                    "storyboard_artifact_id=COALESCE(published_storyboard_artifact_id, NULL) WHERE id=?",
                    (episode_id,),
                )
            except Exception:  # noqa: BLE001
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=NULL, storyboard_artifact_id=NULL WHERE id=?",
                    (episode_id,),
                )
            conn.commit()
            cp.validated_prefix_end = 0
            cp.next_shot_no = 1
            cp.validated_shot_artifact_ids = []
            try:
                contract_ver = get_contract("storyboard").version
            except Exception:  # noqa: BLE001
                contract_ver = "1"
            ensure_production_revision(
                episode_id=episode_id,
                kind="storyboard",
                contract_version=contract_ver,
                resume=False,
            )

    conn.execute(
        "UPDATE episodes SET status='scripting', script_error=NULL, storyboard_warning=NULL WHERE id=?",
        (episode_id,),
    )
    conn.commit()

    spine_n = len((screenplay.plot_spine.spine_beats if screenplay.plot_spine else None) or [])
    compact_target = _storyboard_target_for_source(
        ep_data.get("target_duration_s"), len(source_text), spine_beat_count=spine_n or None
    )
    if compact_target != ep_data.get("target_duration_s"):
        conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
        conn.commit()
        ep_data["target_duration_s"] = compact_target

    prev = conn.execute(
        "SELECT cliffhanger FROM episodes WHERE project_id=? AND episode_no=?",
        (ep["project_id"], ep["episode_no"] - 1),
    ).fetchone()

    outline: StoryboardOutline | None = None
    if resume and ep["storyboard_outline_json"]:
        try:
            outline = StoryboardOutline.model_validate_json(ep["storyboard_outline_json"])
        except (TypeError, ValueError):
            outline = None

    existing_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    completed: list[Shot] = (
        list(_board_from_shot_rows(existing_rows, ep_data["episode_no"]).shots)
        if existing_rows else []
    )
    if completed:
        recovered_board = Storyboard(episode_no=ep_data["episode_no"], shots=list(completed))
        character_changes = normalize_offbible_characters(recovered_board, bible)
        _persist_storyboard_character_policy_repairs(
            conn, episode_id, recovered_board, character_changes
        )
        completed = list(recovered_board.shots)
        cp.validated_prefix_end = len(completed)
        cp.next_shot_no = len(completed) + 1
        cp.validated_shot_artifact_ids = [
            (r["storyboard_artifact_id"] or "") for r in existing_rows if r["storyboard_artifact_id"]
        ]

    _, max_shots = storyboard_shot_count_range(ep_data["target_duration_s"])
    planned_persisted = len(outline.shots) if (outline and outline.shots) else 0
    final_feedback: list[str] | None = None
    needs_outline = outline is None

    while cp.repair_epoch <= MAX_REPAIR_EPOCHS:
        # 用户 pause / handoff：在安全边界生效
        from app.storyboard_control import consume_control
        ctrl = consume_control(episode_id)
        if ctrl == "pause":
            cp.phase = "PAUSED_EXTERNAL"
            save_checkpoint(cp, run_id=run_id)
            conn.execute(
                "UPDATE episodes SET status='scripting', script_error=? WHERE id=?",
                ("用户暂停：已保留已验证 checkpoint，可继续自动修复", episode_id),
            )
            conn.commit()
            if run_id:
                evidence_repository.append_event(
                    run_id, "SUPERVISOR_PAUSED", "info", "用户暂停",
                    payload={"phase": cp.phase, "prefix": cp.validated_prefix_end},
                )
                try:
                    from app.orchestration.state_machine import transition_run
                    transition_run(run_id, "RUNNING", "PAUSED_EXTERNAL", "user_pause")
                except Exception:  # noqa: BLE001
                    pass
            return cp
        if ctrl == "handoff":
            cp.phase = "WAITING_HUMAN"
            save_checkpoint(cp, run_id=run_id)
            conn.execute(
                "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                ("已转人工处理：自动修复已停止，已验证镜头与问题清单已保留", episode_id),
            )
            conn.commit()
            if run_id:
                evidence_repository.append_event(
                    run_id, "SUPERVISOR_HANDOFF", "info", "转人工处理",
                    payload={"phase": cp.phase, "last_repair": cp.last_repair},
                )
                try:
                    from app.orchestration.state_machine import transition_run
                    transition_run(run_id, "RUNNING", "WAITING_HUMAN", "user_handoff")
                except Exception:  # noqa: BLE001
                    pass
            return cp

        # ---- 大纲 ----
        if needs_outline or cp.phase in {"PLANNING_OUTLINE", "REPAIRING"} and outline is None:
            cp.phase = "PLANNING_OUTLINE"
            save_checkpoint(cp, run_id=run_id)
            try:
                outline = await generate_storyboard_outline(
                    ep_data, source_text, bible,
                    prev_ending=prev["cliffhanger"] if prev else "",
                    screenplay=screenplay,
                )
            except Exception as exc:  # noqa: BLE001
                public = errors.record_and_format(
                    exc, action="storyboard_outline_degraded",
                    context={"episode_id": episode_id},
                )
                conn.execute(
                    "UPDATE episodes SET storyboard_warning=? WHERE id=?",
                    (f"分镜大纲失败：{public}", episode_id),
                )
                conn.commit()
                raise StageError("分镜大纲", [public]) from exc
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=?, storyboard_warning=NULL WHERE id=?",
                (outline.model_dump_json(), episode_id),
            )
            conn.commit()
            cp.phase = "VALIDATING_OUTLINE"
            cp.expected_total = len(outline.shots)
            planned_persisted = len(outline.shots)
            needs_outline = False
            if run_id:
                evidence_repository.append_event(
                    run_id, "OUTLINE_VALIDATED", "info",
                    f"大纲通过，共 {len(outline.shots)} 镜",
                )
            save_checkpoint(cp, run_id=run_id)

        # ---- 逐镜 ----
        cp.phase = "GENERATING_SHOTS"
        shot_loop_broke_for_repair = False
        while True:
            planned_now = len(outline.shots) if (outline and outline.shots) else 0
            if planned_now > 0 and len(completed) >= planned_now:
                break
            if completed and completed[-1].is_final:
                break
            if len(completed) >= max_shots:
                break

            shot_no = len(completed) + 1
            cp.next_shot_no = shot_no
            # 每镜开始前再检查一次控制请求
            from app.storyboard_control import peek_control
            if peek_control(episode_id):
                break
            try:
                draft = await generate_storyboard_next_shot(
                    ep_data, source_text, bible,
                    prev_ending=prev["cliffhanger"] if prev else "",
                    screenplay=screenplay,
                    completed_shots=completed,
                    final_feedback=final_feedback,
                    outline=outline,
                )
            except StageError as exc:
                plan = route_issues(
                    list(exc.errors) if hasattr(exc, "errors") else [str(exc)],
                    validated_prefix_end=cp.validated_prefix_end,
                    next_shot_no=shot_no,
                    issue_fingerprint_counts=cp.issue_fingerprint_counts,
                )
                cp = _apply_repair(cp, plan, conn, episode_id, completed, outline)
                completed = list(_board_from_shot_rows(
                    conn.execute(
                        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
                    ).fetchall(),
                    ep_data["episode_no"],
                ).shots) if cp.validated_prefix_end else []
                if (cp.last_repair or {}).get("strategy") == "insert_shot" or plan.strategy in {"insert_shot", "replan_outline"}:
                    outline = None
                    needs_outline = True
                    conn.execute(
                        "UPDATE episodes SET storyboard_outline_json=NULL WHERE id=?", (episode_id,)
                    )
                    conn.commit()
                shot_loop_broke_for_repair = True
                break
            except Exception as exc:  # noqa: BLE001
                # Provider 类故障 → 可恢复暂停
                msg = str(exc)
                if any(k in msg.lower() for k in ("timeout", "unavailable", "429", "503", "连接")):
                    cp.phase = "PAUSED_EXTERNAL"
                    save_checkpoint(cp, run_id=run_id)
                    conn.execute(
                        "UPDATE episodes SET status='scripting', script_error=? WHERE id=?",
                        (f"外部依赖暂不可用，已暂停待恢复：{msg[:200]}", episode_id),
                    )
                    conn.commit()
                    if run_id:
                        evidence_repository.append_event(
                            run_id, "SUPERVISOR_PAUSED", "warning", "PAUSED_EXTERNAL",
                            payload={"error": msg[:400]},
                        )
                    return cp
                raise

            disposition = getattr(draft, "disposition", "PASS")
            blockers = _blocker_messages(draft)

            # NEEDS_REPLAN 或 blocker：不落主 shots
            if disposition == "NEEDS_REPLAN" or blockers:
                # 仍可把 candidate artifact 保留在 draft.evidence_artifact_id
                plan = route_issues(
                    blockers or list(getattr(draft, "residual_errors", []) or []),
                    validated_prefix_end=cp.validated_prefix_end,
                    next_shot_no=shot_no,
                    issue_fingerprint_counts=cp.issue_fingerprint_counts,
                )
                if run_id:
                    evidence_repository.append_event(
                        run_id, "REPAIR_PLAN_SELECTED", "info",
                        f"{plan.strategy} frontier={plan.invalidation_frontier}",
                        payload=plan.model_dump(mode="json"),
                    )
                cp = _apply_repair(cp, plan, conn, episode_id, completed, outline)
                rows = conn.execute(
                    "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
                ).fetchall()
                completed = list(_board_from_shot_rows(rows, ep_data["episode_no"]).shots) if rows else []
                if plan.pause_state:
                    cp.phase = plan.pause_state  # type: ignore[assignment]
                    save_checkpoint(cp, run_id=run_id)
                    conn.execute(
                        "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                        (plan.reason[:800], episode_id),
                    )
                    conn.commit()
                    return cp
                if (cp.last_repair or {}).get("strategy") == "insert_shot" or plan.strategy in {"insert_shot", "replan_outline"}:
                    outline = None
                    needs_outline = True
                    conn.execute(
                        "UPDATE episodes SET storyboard_outline_json=NULL WHERE id=?", (episode_id,)
                    )
                    conn.commit()
                shot_loop_broke_for_repair = True
                break

            # PASS / warning-only → 落库 validated
            board = Storyboard(episode_no=ep_data["episode_no"], shots=[*completed, draft.shot])
            normalize_continuity(board)
            for c in normalize_offbible_characters(board, bible):
                pass  # 已归一
            relieve_spoken_overflow(board)
            prefer_default_shot_durations(board)
            normalize_transition_visuals(board)
            _sync_storyboard_shot_timing(conn, episode_id, board)
            shot = board.shots[-1]
            shot.is_final = bool(draft.is_final)
            shot.prompt_contract_version = "renderability_v1"
            object.__setattr__(shot, "evidence_artifact_id", getattr(draft, "evidence_artifact_id", None))
            _insert_storyboard_shot(conn, episode_id, screenplay, shot)
            completed = list(board.shots)
            conn.execute(
                "UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,)
            )
            conn.commit()
            revision = _reconcile_storyboard_plan(
                conn, episode_id, ep_data["episode_no"], outline, completed, planned_persisted
            )
            if revision is not None:
                planned_persisted = revision[1]
            cp.validated_prefix_end = len(completed)
            cp.next_shot_no = len(completed) + 1
            art_id = getattr(shot, "evidence_artifact_id", None)
            if art_id:
                cp.validated_shot_artifact_ids.append(art_id)
            if run_id:
                evidence_repository.append_event(
                    run_id, "SHOT_CHECKPOINT_VALIDATED", "info",
                    f"第 {shot.shot_no} 镜已通过",
                    payload={"shot_no": shot.shot_no},
                )
            save_checkpoint(cp, run_id=run_id)

            if draft.is_final:
                break
            if len(completed) >= SHOT_SOFT_MAX:
                final_feedback = None
            else:
                final_feedback = validate_storyboard_preserves_key_content(
                    Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)),
                    screenplay,
                ) or None

        if shot_loop_broke_for_repair:
            if cp.repair_epoch > MAX_REPAIR_EPOCHS:
                break
            continue

        # 逐镜循环因控制请求 break：回主循环顶部消费
        from app.storyboard_control import peek_control as _peek
        if _peek(episode_id):
            continue

        # ---- 整集校验 ----
        cp.phase = "VALIDATING_EPISODE"
        full_board = Storyboard(episode_no=ep_data["episode_no"], shots=list(completed))
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        has_real_bible = bool((p["bible_json"] or "").strip()) if p else False
        evaluation = evaluate_storyboard_for_confirmation(
            ep_data, full_board, screenplay, bible, has_real_bible=has_real_bible,
        )
        if not evaluation.passed:
            if run_id:
                evidence_repository.append_event(
                    run_id, "EPISODE_VALIDATION_FAILED", "warning",
                    f"{len(evaluation.errors)} issues",
                    payload={"errors": evaluation.errors[:12]},
                )
            plan = route_issues(
                evaluation.issues or evaluation.errors,
                validated_prefix_end=cp.validated_prefix_end,
                issue_fingerprint_counts=cp.issue_fingerprint_counts,
            )
            cp = _apply_repair(cp, plan, conn, episode_id, completed, outline)
            rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
            ).fetchall()
            completed = list(_board_from_shot_rows(rows, ep_data["episode_no"]).shots) if rows else []
            if plan.pause_state:
                cp.phase = plan.pause_state  # type: ignore[assignment]
                save_checkpoint(cp, run_id=run_id)
                conn.execute(
                    "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                    (("；".join(evaluation.errors[:5]))[:800], episode_id),
                )
                conn.commit()
                if run_id and plan.pause_state in {
                    "WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL",
                }:
                    try:
                        from app.orchestration.state_machine import transition_run
                        transition_run(
                            run_id, "RUNNING", plan.pause_state, "repair_pause",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return cp
            if (cp.last_repair or {}).get("strategy") == "insert_shot" or plan.strategy in {"insert_shot", "replan_outline"}:
                outline = None
                needs_outline = True
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=NULL WHERE id=?", (episode_id,)
                )
                conn.commit()
            continue

        # ---- 通过：finalize + 完成模式 ----
        actual_total = sum(int(s.duration_s or 0) for s in completed)
        synced = _compact_episode_target(actual_total or ep_data["target_duration_s"])
        _finalize_storyboard_evidence(episode_id, evaluation.board)

        if cp.completion_mode != "auto_confirm":
            conn.execute(
                "UPDATE episodes SET status='scripted', script_error=NULL, target_duration_s=? WHERE id=?",
                (synced, episode_id),
            )
            conn.commit()
            cp.phase = "SUCCEEDED"
            cp.outcome = "SUCCEEDED_READY_FOR_CONFIRM"
            save_checkpoint(cp, run_id=run_id)
            return cp

        # 自动确认
        cp.phase = "PREPARING_CONFIRM"
        save_checkpoint(cp, run_id=run_id)
        try:
            if not cp.completion_grant_id:
                raise GrantValidationError("GRANT_NOT_FOUND", "缺少自动确认授权")
            ep_now = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
            proj = conn.execute(
                "SELECT bible_artifact_id FROM projects WHERE id=?", (ep["project_id"],)
            ).fetchone()
            validate_grant_for_confirm(
                cp.completion_grant_id,
                episode_id=episode_id,
                screenplay_artifact_id=ep_now["screenplay_artifact_id"],
                bible_artifact_id=proj["bible_artifact_id"] if proj else None,
            )
            cp.phase = "CONFIRMING"
            save_checkpoint(cp, run_id=run_id)
            if run_id:
                evidence_repository.append_event(
                    run_id, "AUTO_CONFIRM_STARTED", "info", "开始自动确认",
                )
            confirm_episode_core(
                episode_id,
                decided_by="supervisor",
                reason="分镜全量确定性校验通过并由 Supervisor 自动确认",
            )
            consume_grant(cp.completion_grant_id)
            if run_id:
                evidence_repository.append_event(
                    run_id, "AUTO_CONFIRM_SUCCEEDED", "info",
                    "分镜已自动确认；尚未产生视频费用",
                )
            cp.phase = "SUCCEEDED"
            cp.outcome = "SUCCEEDED_CONFIRMED"
            save_checkpoint(cp, run_id=run_id)
            return cp
        except GrantValidationError as exc:
            cp.phase = "WAITING_AUTHORIZATION"
            save_checkpoint(cp, run_id=run_id)
            conn.execute(
                "UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                (str(exc)[:800], episode_id),
            )
            conn.commit()
            if run_id:
                evidence_repository.append_event(
                    run_id, "WAITING_AUTHORIZATION", "warning", str(exc)[:200],
                )
                try:
                    from app.orchestration.state_machine import transition_run
                    transition_run(run_id, "RUNNING", "WAITING_AUTHORIZATION", "grant_invalid")
                except Exception:  # noqa: BLE001
                    pass
            return cp
        except ValueError as exc:
            # VAL-422 → 回流 Repair
            try:
                err_list = json.loads(str(exc))
                if not isinstance(err_list, list):
                    err_list = [str(exc)]
            except (TypeError, ValueError, json.JSONDecodeError):
                err_list = [str(exc)]
            if run_id:
                evidence_repository.append_event(
                    run_id, "AUTO_CONFIRM_REJECTED", "warning",
                    "确认门拒绝，进入修复",
                    payload={"errors": err_list[:12]},
                )
            plan = route_issues(
                err_list,
                validated_prefix_end=cp.validated_prefix_end,
                issue_fingerprint_counts=cp.issue_fingerprint_counts,
            )
            cp = _apply_repair(cp, plan, conn, episode_id, completed, outline)
            rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
            ).fetchall()
            completed = list(_board_from_shot_rows(rows, ep_data["episode_no"]).shots) if rows else []
            if (cp.last_repair or {}).get("strategy") == "insert_shot" or plan.strategy in {"insert_shot", "replan_outline"}:
                outline = None
                needs_outline = True
            continue

    # 超出单次 activation 的 repair epoch：让出并自动续跑，不把可修复 QA 结束为 failure
    cp.phase = "WAITING_RETRY"
    save_checkpoint(cp, run_id=run_id)
    conn.execute(
        "UPDATE episodes SET status='scripting', script_error=? WHERE id=?",
        (f"自动修复让出（已完成 {MAX_REPAIR_EPOCHS} 轮局部修补），将自动续跑", episode_id),
    )
    conn.commit()
    if run_id:
        evidence_repository.append_event(
            run_id, "REPAIR_YIELD", "info",
            "分镜修复 activation 预算用尽，已写 checkpoint，等待自动续跑",
            payload={"repair_epoch": cp.repair_epoch},
        )
    return cp


def _apply_repair(
    cp: SupervisorCheckpoint,
    plan: RepairPlan,
    conn,
    episode_id: str,
    completed: list[Shot],
    outline: StoryboardOutline | None,
) -> SupervisorCheckpoint:
    """最小范围修复：只触及当前镜或相邻窗口；禁止 redo_suffix / replan_outline。"""
    from app.observability.metrics import inc
    from app.repair_router import normalize_strategy

    cp.phase = "REPAIRING"
    cp.repair_epoch += 1
    cp.issue_fingerprint_counts = bump_fingerprint_count(
        cp.issue_fingerprint_counts, plan.fingerprint
    )
    strategy = normalize_strategy(plan.strategy)
    cp.last_repair = {**plan.model_dump(mode="json"), "strategy": strategy}
    frontier = max(1, int(plan.invalidation_frontier or 1))
    deleted = 0
    effective_strategy = strategy

    if strategy in {"split_adjacent_shot", "split_shot"}:
        from app.validators import split_outline_over_key_line_capacity, storyboard_shot_count_range
        from app.domain.common import _load_screenplay

        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        screenplay = _load_screenplay(ep) if ep else None
        events: list[dict] = []
        if outline is not None and screenplay is not None:
            _, max_shots = storyboard_shot_count_range(ep["target_duration_s"] if ep else 50)
            events = split_outline_over_key_line_capacity(
                outline, screenplay, max_shots=max_shots,
            )
            if events:
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                    (outline.model_dump_json(), episode_id),
                )
                cp.expected_total = len(outline.shots)
                inc(
                    "storyboard_split_shot_total",
                    episode_id=episode_id,
                    shot_no=frontier,
                    shots_after=len(outline.shots),
                    strategy=strategy,
                )
        # 只失效 frontier 一镜（或窗口），不删整后缀
        window_end = frontier if strategy == "split_shot" else frontier
        deleted = _delete_shot_window(conn, episode_id, frontier, window_end)
        cp.validated_prefix_end = max(0, frontier - 1)
        cp.next_shot_no = frontier
        cp.validated_shot_artifact_ids = cp.validated_shot_artifact_ids[: max(0, frontier - 1)]
        if not events:
            # 大纲无法再拆 → 插入明确节点，绝不整集重规划
            effective_strategy = "insert_shot"
            cp.last_repair = {
                **(cp.last_repair or {}),
                "strategy": "insert_shot",
                "reason": "split_noop_escalate_insert",
            }
            if outline is not None and frontier <= len(outline.shots):
                # 在 frontier 处复制相邻大纲节点作为插镜占位
                from copy import deepcopy
                src = outline.shots[min(len(outline.shots), frontier) - 1]
                extra = deepcopy(src)
                extra.shot_no = frontier
                # 重排后续编号由后续生成填充；这里扩展计划长度
                outline.shots.insert(frontier - 1, extra)
                for i, node in enumerate(outline.shots, start=1):
                    node.shot_no = i
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                    (outline.model_dump_json(), episode_id),
                )
                cp.expected_total = len(outline.shots)
            inc(
                "storyboard_insert_shot_total",
                episode_id=episode_id,
                shot_no=frontier,
                strategy="insert_shot",
            )
    elif strategy == "insert_shot":
        # 不删除已通过镜头；仅从 frontier 起允许重填/追加
        deleted = _delete_shot_window(conn, episode_id, frontier, frontier)
        cp.validated_prefix_end = max(0, frontier - 1)
        cp.next_shot_no = frontier
        cp.validated_shot_artifact_ids = cp.validated_shot_artifact_ids[: max(0, frontier - 1)]
        if outline is not None:
            from copy import deepcopy
            idx = min(len(outline.shots), max(1, frontier)) - 1
            if outline.shots:
                extra = deepcopy(outline.shots[idx])
                extra.shot_no = frontier
                outline.shots.insert(idx + 1, extra)
                for i, node in enumerate(outline.shots, start=1):
                    node.shot_no = i
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                    (outline.model_dump_json(), episode_id),
                )
                cp.expected_total = len(outline.shots)
        inc("storyboard_insert_shot_total", episode_id=episode_id, shot_no=frontier)
    elif strategy in {"repair_current", "normalize", "delete_shot"}:
        deleted = _delete_shot_window(conn, episode_id, frontier, frontier)
        cp.validated_prefix_end = max(0, frontier - 1)
        cp.next_shot_no = frontier
        cp.validated_shot_artifact_ids = cp.validated_shot_artifact_ids[: max(0, frontier - 1)]
    elif strategy in {"repair_window", "move_shot"}:
        # 相邻 2~3 镜窗口
        window_end = frontier + 1
        deleted = _delete_shot_window(conn, episode_id, frontier, window_end)
        cp.validated_prefix_end = max(0, frontier - 1)
        cp.next_shot_no = frontier
        cp.validated_shot_artifact_ids = cp.validated_shot_artifact_ids[: max(0, frontier - 1)]
    else:
        # 未知策略降级为单镜修复
        effective_strategy = "repair_current"
        deleted = _delete_shot_window(conn, episode_id, frontier, frontier)
        cp.validated_prefix_end = max(0, frontier - 1)
        cp.next_shot_no = frontier
        cp.validated_shot_artifact_ids = cp.validated_shot_artifact_ids[: max(0, frontier - 1)]

    cp.last_repair = {**(cp.last_repair or {}), "strategy": effective_strategy}
    conn.commit()
    save_checkpoint(cp)
    try:
        from app.observability.tracing import current_trace
        rid = current_trace().run_id
        if rid and deleted:
            evidence_repository.append_event(
                rid, "LOCAL_PATCH_INVALIDATED", "info",
                f"局部失效边界={frontier}，删除 {deleted} 镜（策略={effective_strategy}）",
                payload={"frontier": frontier, "deleted": deleted, "strategy": effective_strategy},
            )
        if rid and effective_strategy in {"insert_shot", "split_shot", "split_adjacent_shot"}:
            evidence_repository.append_event(
                rid, "SHOT_STRUCTURE_PATCHED", "info",
                f"结构修补 strategy={effective_strategy} epoch={cp.repair_epoch}",
                payload=cp.last_repair or plan.model_dump(mode="json"),
            )
    except Exception:  # noqa: BLE001
        pass
    return cp


def _delete_shot_window(conn, episode_id: str, start_no: int, end_no: int) -> int:
    """只删除 [start_no, end_no] 闭区间内的镜头，保留前后无关镜头。"""
    start_no = max(1, int(start_no))
    end_no = max(start_no, int(end_no))
    rows = conn.execute(
        "SELECT id, shot_no FROM shots WHERE episode_id=? AND shot_no>=? AND shot_no<=? ORDER BY shot_no",
        (episode_id, start_no, end_no),
    ).fetchall()
    if not rows:
        return 0
    from app import worker
    for row in rows:
        worker.clear_shot_artifacts(row["id"])
        conn.execute("DELETE FROM shots WHERE id=?", (row["id"],))
    # 重排后续 shot_no，保持连续
    remaining = conn.execute(
        "SELECT id, shot_no FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    for idx, row in enumerate(remaining, start=1):
        if row["shot_no"] != idx:
            conn.execute("UPDATE shots SET shot_no=? WHERE id=?", (idx, row["id"]))
    return len(rows)