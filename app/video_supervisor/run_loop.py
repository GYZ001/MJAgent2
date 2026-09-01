"""Supervisor 主 tick 循环 run_video_completion_supervisor。"""
from __future__ import annotations

import asyncio

from app.completion_grant import (
    DEFAULT_VIDEO_BUDGET_CAP_CNY,
    GrantValidationError,
    VideoCompletionGrant,
    VideoPlanGenerationError,
    validate_video_grant,
)
from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.video_control import consume_control
from app.video_issues import is_fatal
from app.video_repair_router import bump_fingerprint_count, route as route_video_repair

from .adoption import _adopt_ready_candidates, _adopt_ready_candidates_incrementally
from .assets import _prepare_episode_reference_assets
from .authority import (
    _ensure_supervisor_video_plan,
    _record_grant_validation_failure,
    _supervisor_checks_can_use_worker_thread,
    _verify_supervisor_paid_authority_async,
)
from .budget import (
    _budget_view,
    _has_dispatch_budget_capacity,
    _merge_shot_state,
    _rebuild_budgeted_coverage_ledger_async,
    _rebuild_coverage_ledger_async,
)
from .checkpoint import _save_checkpoint_async, load_latest_checkpoint
from .closeout import _deadline_closeout_async, _finalize_covered_async
from .constants import (
    FIRST_PASS_BUDGET_FRACTION,
    MAX_REPAIR_EPOCHS,
    SHOT_BUDGET_MULTIPLIER,
    SUPERVISOR_TICK_INTERVAL_S,
    SUPERVISOR_TICK_MAX_INTERVAL_S,
    TERMINAL_SUPERVISOR_PHASES,
)
from .dispatch import _budget_paused_job_id, _dispatch_with_heartbeat_async, _requeue_no_charge_job
from .issues_cascade import _adopt_fallback, _apply_cascade, _collect_issues
from .job_control import _reconcile_terminal_continuity_blocks
from .models import VideoSupervisorCheckpoint
from .storyboard_repair import _amend_storyboard, _try_auto_crop


async def _resolve_grant_failure(
    cp: VideoSupervisorCheckpoint,
    exc: GrantValidationError | VideoPlanGenerationError,
    *,
    run_id: str | None,
    stage: str,
) -> VideoSupervisorCheckpoint:
    """一个判据，供每一处付费边界的 except 共用：这次失败该不该让用户去追加
    授权？

    ``VideoPlanGenerationError`` 是独立的异常类型，天生不会被任何裸
    ``except GrantValidationError`` 接住——调用方必须显式把它加进 except 元组
    才能碰到这里；一旦碰到，一律落 FAILED_CLOSED：模型计划 JSON 畸形，追加多
    少预算或时长都解决不了（ERR-20260831-dd05c7）。``GRANT_REVOKED`` 是唯一
    「用户已经自己取消」的 GrantValidationError code，落 CANCELLED；其余才是
    真正的授权缺口，落 WAITING_AUTHORIZATION。
    """
    if isinstance(exc, VideoPlanGenerationError):
        cp.phase = "FAILED_CLOSED"
    elif exc.code == "GRANT_REVOKED":
        cp.phase = "CANCELLED"
    else:
        cp.phase = "WAITING_AUTHORIZATION"
    cp.outcome = exc.code
    await _save_checkpoint_async(cp, run_id=run_id)
    _record_grant_validation_failure(cp, exc, run_id=run_id, stage=stage)
    return cp


async def run_video_completion_supervisor(
    episode_id: str,
    *,
    resume: bool = True,
    grant_id: str | None = None,
    run_id: str | None = None,
    budget_cap_cny: float | None = None,
    wall_clock_cap_s: float | None = None,
    allow_fallback_adopt: bool = True,
    max_fallback_shots: int | None = None,
    allow_storyboard_edit: bool = False,
) -> VideoSupervisorCheckpoint:
    """主循环：周期性 reconciler。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"剧集不存在：{episode_id}")

    cp = load_latest_checkpoint(episode_id) if resume else None
    if cp is None:
        shots_total = conn.execute(
            "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
        ).fetchone()["c"]
        from app.completion_grant import default_max_fallback_shots
        quota = max_fallback_shots if max_fallback_shots is not None else default_max_fallback_shots(int(shots_total or 0))
        cap = float(budget_cap_cny if budget_cap_cny is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY)
        cp = VideoSupervisorCheckpoint(
            episode_id=episode_id,
            run_id=run_id,
            phase="PREFLIGHT",
            started_at=now(),
            deadline_at=None,
            grant_id=grant_id,
            storyboard_artifact_id=ep["storyboard_artifact_id"],
            budget={
                "cap_cny": cap,
                "spent_cny": 0.0,
                "first_pass_soft_cap_cny": cap * FIRST_PASS_BUDGET_FRACTION,
                "per_shot_cap_cny": (cap / max(1, int(shots_total or 1))) * SHOT_BUDGET_MULTIPLIER,
            },
            coverage={"fallback_quota": quota, "A": 0, "B": 0, "C": int(shots_total or 0), "total": int(shots_total or 0)},
        )
    else:
        if run_id:
            cp.run_id = run_id
        if grant_id:
            cp.grant_id = grant_id
        if budget_cap_cny is not None:
            cp.budget["cap_cny"] = float(budget_cap_cny)

    if cp.phase in TERMINAL_SUPERVISOR_PHASES:
        return cp

    initial_wall_cap = float(
        wall_clock_cap_s
        if wall_clock_cap_s is not None
        else (cp.budget.get("wall_clock_cap_s") or 4 * 3600)
    )
    cp.deadline_at = cp.deadline_at or ((cp.started_at or now()) + initial_wall_cap)
    if now() >= cp.deadline_at:
        return await _deadline_closeout_async(
            cp, run_id=run_id, reason="VIDEO_WALL_CLOCK_EXCEEDED",
        )

    grant: VideoCompletionGrant | None = None
    if cp.grant_id:
        try:
            grant = await asyncio.to_thread(
                validate_video_grant,
                cp.grant_id,
                episode_id=episode_id,
                storyboard_artifact_id=ep["storyboard_artifact_id"],
            )
            cp.budget["cap_cny"] = float(grant.budget_cap_cny)
            cp.coverage["fallback_quota"] = int(grant.max_fallback_shots)
            allow_fallback_adopt = grant.allow_fallback_adopt
            allow_storyboard_edit = grant.allow_storyboard_edit
            wall_clock_cap_s = float(grant.wall_clock_cap_s)
            cp.deadline_at = float(grant.deadline_at)
            cp.budget["wall_clock_cap_s"] = float(grant.wall_clock_cap_s)
        except GrantValidationError as exc:
            if cp.deadline_at and now() >= cp.deadline_at:
                return await _deadline_closeout_async(
                    cp, run_id=run_id, reason="VIDEO_WALL_CLOCK_EXCEEDED",
                )
            return await _resolve_grant_failure(
                cp, exc, run_id=run_id, stage="grant_validate_preflight",
            )

    try:
        grant = await _ensure_supervisor_video_plan(cp)
        await _save_checkpoint_async(cp, run_id=run_id)
    except (VideoPlanGenerationError, GrantValidationError) as exc:
        return await _resolve_grant_failure(
            cp, exc, run_id=run_id, stage="ensure_video_plan",
        )

    if run_id:
        evidence_repository.append_event(
            run_id, "VIDEO_SUPERVISOR_STARTED", "info",
            "视频补齐 Supervisor 启动",
            payload={"resume": resume, "grant_id": cp.grant_id},
        )

    try:
        await _prepare_episode_reference_assets(
            episode_id,
            cp=cp,
            run_id=run_id,
        )
    except GrantValidationError as exc:
        return await _resolve_grant_failure(
            cp, exc, run_id=run_id, stage="reference_asset_prep",
        )
    except Exception as exc:  # noqa: BLE001 - 资产失败降级，不得中断整集覆盖
        cp.quality_target_missed = True
        await _save_checkpoint_async(cp, run_id=run_id)
        if run_id:
            evidence_repository.append_event(
                run_id,
                "VIDEO_REFERENCE_ASSET_PREP_FAILED",
                "warning",
                f"参考资产准备失败，继续使用当前可用资产生成真实模型视频：{str(exc)[:500]}",
            )

    wall_cap = float(wall_clock_cap_s if wall_clock_cap_s is not None else (cp.budget.get("wall_clock_cap_s") or 4 * 3600))
    if grant:
        wall_cap = float(grant.wall_clock_cap_s)
        cp.deadline_at = float(grant.deadline_at)
    else:
        cp.deadline_at = (cp.started_at or now()) + wall_cap

    while True:
        if cp.deadline_at and now() >= cp.deadline_at:
            return await _deadline_closeout_async(
                cp, run_id=run_id, reason="VIDEO_WALL_CLOCK_EXCEEDED",
            )
        action = consume_control(episode_id)
        if action == "pause":
            cp.phase = "PAUSED_EXTERNAL"
            await _save_checkpoint_async(cp, run_id=run_id)
            if run_id:
                evidence_repository.append_event(
                    run_id, "VIDEO_SUPERVISOR_PAUSED", "info", "用户暂停",
                )
            return cp
        if action == "handoff":
            cp.phase = "WAITING_HUMAN"
            await _save_checkpoint_async(cp, run_id=run_id)
            if run_id:
                evidence_repository.append_event(
                    run_id, "VIDEO_SUPERVISOR_HANDOFF", "warning", "转交人工",
                )
            return cp
        if action == "retry_now":
            cp.tick_interval_s = SUPERVISOR_TICK_INTERVAL_S
            cp.idle_ticks = 0

        # Budget top-ups remain visible, while every authored or plan drift is
        # re-evaluated instead of trusting a cached grant row.
        try:
            g = await _verify_supervisor_paid_authority_async(
                cp,
                stage="supervisor_tick",
            )
        except GrantValidationError as exc:
            return await _resolve_grant_failure(
                cp, exc, run_id=run_id, stage="supervisor_tick",
            )
        if g:
            cp.budget["cap_cny"] = float(g.budget_cap_cny)
            wall_cap = float(g.wall_clock_cap_s)
            cp.deadline_at = float(g.deadline_at)
            cp.budget["wall_clock_cap_s"] = wall_cap
            allow_fallback_adopt = g.allow_fallback_adopt
            allow_storyboard_edit = g.allow_storyboard_edit
            grant = g

        cp.tick_no += 1
        cp.phase = "PLANNING_COVERAGE"
        _reconcile_terminal_continuity_blocks(episode_id, run_id=run_id)
        cap = float(cp.budget.get("cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY)
        fallback_quota = int(cp.coverage.get("fallback_quota") or 0)
        ledger = await _rebuild_budgeted_coverage_ledger_async(
            episode_id,
            cp=cp,
            fallback_quota=fallback_quota,
            budget_cap_cny=cap,
        )
        adopted_ready = (
            await asyncio.to_thread(
                _adopt_ready_candidates,
                ledger,
                run_id=run_id,
            )
            if _supervisor_checks_can_use_worker_thread()
            else _adopt_ready_candidates(ledger, run_id=run_id)
        )
        if adopted_ready:
            ledger = await _rebuild_budgeted_coverage_ledger_async(
                episode_id,
                cp=cp,
                fallback_quota=fallback_quota,
                budget_cap_cny=cap,
            )
        _merge_shot_state(cp, ledger)
        await _save_checkpoint_async(cp, run_id=run_id)

        if ledger.covered_within_quota():
            return await _finalize_covered_async(cp, ledger, run_id=run_id)

        spent = float(ledger.cost_spent)
        if spent >= cap:
            return await _deadline_closeout_async(
                cp, run_id=run_id, reason="VIDEO_BUDGET_EXHAUSTED_FALLBACK",
            )
        if cp.deadline_at and now() >= cp.deadline_at:
            return await _deadline_closeout_async(
                cp, run_id=run_id, reason="VIDEO_WALL_CLOCK_EXCEEDED",
            )
        if cp.repair_epoch > MAX_REPAIR_EPOCHS:
            return await _deadline_closeout_async(
                cp, run_id=run_id, reason="REPAIR_EPOCHS_EXHAUSTED",
            )

        if ledger.has_active_jobs() and not ledger.actionable():
            cp.phase = "OBSERVING"
            await _save_checkpoint_async(cp, run_id=run_id)
            await asyncio.sleep(cp.tick_interval_s)
            continue

        cp.phase = "EVALUATING"
        progressed = False
        budget_capacity_reached = False
        soft_cap = cap * FIRST_PASS_BUDGET_FRACTION
        per_shot_cap = (cap / max(1, ledger.shots_total)) * SHOT_BUDGET_MULTIPLIER

        for entry in ledger.actionable():
            budget_paused_job_id = _budget_paused_job_id(
                conn,
                shot_id=entry.shot_id,
            )
            if budget_paused_job_id:
                cp.phase = "WAITING_AUTHORIZATION"
                cp.outcome = "VIDEO_BUDGET_PAUSED"
                cp.last_plan = {
                    "shot_no": entry.shot_no,
                    "level": "L6",
                    "strategy": "handoff_human",
                    "reason": "预算暂停仅允许通过页面显式继续",
                    "issue_codes": ["VIDEO_BUDGET_PAUSED"],
                    "job_id": budget_paused_job_id,
                }
                _merge_shot_state(cp, ledger)
                await _save_checkpoint_async(cp, run_id=run_id)
                return cp

            # 单镜上限
            if entry.cost_spent_cny >= per_shot_cap and entry.grade == "C":
                if allow_fallback_adopt and entry.best_version_id:
                    if _adopt_fallback(entry, episode_id=episode_id, run_id=run_id):
                        progressed = True
                continue

            if entry.never_attempted:
                # 首轮软预算
                if not cp.first_pass_done and spent >= soft_cap:
                    continue
                if not _has_dispatch_budget_capacity(
                    episode_id,
                    entry,
                    budget_cap_cny=cap,
                ):
                    budget_capacity_reached = True
                    break
                cp.phase = "DISPATCHING"
                try:
                    dispatched = await _dispatch_with_heartbeat_async(
                        entry,
                        episode_id=episode_id,
                        run_id=run_id,
                        cp=cp,
                        first=True,
                    )
                except GrantValidationError as exc:
                    return await _resolve_grant_failure(
                        cp, exc, run_id=run_id, stage="dispatch_first_pass",
                    )
                if dispatched:
                    progressed = True
                # Narrative-authority request compilation is intentionally
                # thorough and synchronous. Yield after each dispatch so a
                # large first pass cannot starve HTTP and media workers.
                await asyncio.sleep(0)
                await _adopt_ready_candidates_incrementally(
                    episode_id,
                    cp=cp,
                    fallback_quota=fallback_quota,
                    run_id=run_id,
                )
                continue

            issues = _collect_issues(entry, run_id=run_id)
            model_rejected = any(
                not issue.repairable
                and (issue.evidence or {}).get("pause_state") == "PAUSED_EXTERNAL"
                for issue in issues
            )
            if model_rejected:
                entry.repair_level = "L6"
                entry.fallback_reason = "视频模型明确拒绝，已排除自动修复与付费重试"
                continue
            # 更新 fatal 计数
            if any(is_fatal(i) for i in issues):
                entry.fatal_repeat_count += 1

            plan = route_video_repair(
                issues,
                entry=entry,
                budget=_budget_view(cp, ledger),
                fingerprint_counts=entry.issue_fingerprint_counts,
                current_level=entry.repair_level,  # type: ignore[arg-type]
                allow_storyboard_edit=allow_storyboard_edit,
                qa_history=entry.qa_history,
                rebuilt_reference=entry.rebuilt_reference,
                fatal_repeat_count=entry.fatal_repeat_count,
            )
            entry.repair_level = plan.level
            cp.last_plan = {
                "shot_no": entry.shot_no,
                "level": plan.level,
                "strategy": plan.strategy,
                "reason": plan.reason,
                "issue_codes": plan.issue_codes,
            }
            if plan.fingerprint:
                entry.issue_fingerprint_counts = bump_fingerprint_count(
                    entry.issue_fingerprint_counts, plan.fingerprint
                )
            if plan.pause_state:
                cp.phase = plan.pause_state  # type: ignore[assignment]
                cp.outcome = plan.reason
                _merge_shot_state(cp, ledger)
                await _save_checkpoint_async(cp, run_id=run_id)
                if run_id:
                    evidence_repository.append_event(
                        run_id, "VIDEO_REPAIR_PLAN_SELECTED", "warning",
                        plan.reason, payload=cp.last_plan,
                    )
                return cp

            if plan.strategy == "handoff_human":
                continue

            if max(entry.attempts_paid, entry.attempts_dispatched) >= entry.attempts_budgeted and plan.is_paid:
                continue

            if plan.strategy == "amend_storyboard":
                try:
                    amended = bool(
                        grant
                        and grant.allow_storyboard_edit
                        and await _amend_storyboard(
                            entry,
                            grant=grant,
                            plan=plan,
                            run_id=run_id,
                        )
                    )
                except GrantValidationError as exc:
                    _merge_shot_state(cp, ledger)
                    return await _resolve_grant_failure(
                        cp, exc, run_id=run_id, stage="amend_storyboard",
                    )
                if amended:
                    cp.phase = "WAITING_HUMAN"
                    cp.outcome = "已创建分镜修改草稿；视频流水线已暂停，等待分镜台完整终态与人工重新确认"
                    _merge_shot_state(cp, ledger)
                    await _save_checkpoint_async(cp, run_id=run_id)
                    return cp
                if grant and grant.allow_storyboard_edit:
                    cp.phase = "WAITING_HUMAN"
                    cp.outcome = "AI 语义分镜修复候选未通过全链路验证，需要人工处理"
                    _merge_shot_state(cp, ledger)
                    await _save_checkpoint_async(cp, run_id=run_id)
                    return cp
                else:
                    cp.phase = "WAITING_AUTHORIZATION"
                    cp.outcome = "STORYBOARD_REPAIR_PROPOSAL_NOT_AUTHORIZED"
                    await _save_checkpoint_async(cp, run_id=run_id)
                    return cp
                continue

            if plan.strategy == "auto_crop":
                if _try_auto_crop(entry, run_id=run_id):
                    progressed = True
                    continue
                # 裁切失败则降为定向重抽
                plan = plan.model_copy(update={
                    "strategy": "retake_directed",
                    "is_paid": True,
                    "reason": (plan.reason or "") + "；自动裁切失败，改定向重抽",
                })

            if plan.strategy == "requeue_no_charge":
                # 预算暂停是页面确认门禁，不能作为普通失败免计费重排。
                requeued_job_id = (
                    _requeue_no_charge_job(
                        conn,
                        shot_id=entry.shot_id,
                        run_id=run_id,
                    )
                    if entry.no_charge_requeues < 2
                    else None
                )
                if requeued_job_id:
                    entry.no_charge_requeues += 1
                    progressed = True
                    continue
                # 同一个失败任务最多免计费重排两次；之后创建新版本并受镜级派发上限约束。
                plan = plan.model_copy(update={
                    "level": "L1",
                    "strategy": "retake_directed",
                    "is_paid": True,
                    "reason": (plan.reason or "") + "；免计费重排已达上限，切换受控新版本",
                })
                if max(entry.attempts_paid, entry.attempts_dispatched) >= entry.attempts_budgeted:
                    continue
            cp.phase = "REPAIRING"
            if run_id:
                evidence_repository.append_event(
                    run_id, "VIDEO_REPAIR_PLAN_SELECTED", "info",
                    plan.reason, payload=cp.last_plan,
                )
            try:
                if plan.is_paid and not _has_dispatch_budget_capacity(
                    episode_id,
                    entry,
                    budget_cap_cny=cap,
                ):
                    budget_capacity_reached = True
                    break
                dispatched = await _dispatch_with_heartbeat_async(
                    entry,
                    episode_id=episode_id,
                    run_id=run_id,
                    cp=cp,
                    plan=plan,
                )
            except GrantValidationError as exc:
                return await _resolve_grant_failure(
                    cp, exc, run_id=run_id, stage="dispatch_repair",
                )
            if dispatched:
                progressed = True
                cascaded = _apply_cascade(entry, ledger, cp)
                if cascaded and run_id:
                    evidence_repository.append_event(
                        run_id, "VIDEO_CHAIN_INVALIDATED", "info",
                        f"第 {entry.shot_no} 镜级联 {cascaded}",
                        payload={"shot_no": entry.shot_no, "cascade": cascaded},
                    )
                if plan.degrade_chain and run_id:
                    evidence_repository.append_event(
                        run_id, "VIDEO_CHAIN_DEGRADED", "warning",
                        f"第 {entry.shot_no} 镜降链",
                        payload={"shot_no": entry.shot_no},
                    )
            await asyncio.sleep(0)
            await _adopt_ready_candidates_incrementally(
                episode_id,
                cp=cp,
                fallback_quota=fallback_quota,
                run_id=run_id,
            )

        if budget_capacity_reached:
            observed = await _rebuild_coverage_ledger_async(
                episode_id,
                cp=cp,
                fallback_quota=int(cp.coverage.get("fallback_quota") or 0),
            )
            _merge_shot_state(cp, observed)
            if observed.has_active_jobs():
                cp.phase = "OBSERVING"
                cp.outcome = "VIDEO_BUDGET_CAPACITY_RESERVED_BY_ACTIVE_JOBS"
                await _save_checkpoint_async(cp, run_id=run_id)
                await asyncio.sleep(cp.tick_interval_s)
                continue
            cp.phase = "PAUSED_BUDGET"
            cp.outcome = "VIDEO_BUDGET_CAPACITY_REACHED"
            await _save_checkpoint_async(cp, run_id=run_id)
            return cp

        # 尝试预算耗尽后统一 best-effort 兜底；质量等级和旧 fallback quota
        # 只用于报告，不得让任何已有可播候选继续空置。
        if allow_fallback_adopt:
            # 刷新 ledger 状态到 cp
            _merge_shot_state(cp, ledger)
            ledger2 = await _rebuild_coverage_ledger_async(
                episode_id,
                cp=cp,
            )
            for entry in ledger2.exhausted_but_technically_ok():
                if _adopt_fallback(entry, episode_id=episode_id, run_id=run_id):
                    progressed = True

        # 首轮：若所有 never_attempted 都处理过或软预算触顶
        if not any(e.never_attempted for e in ledger.entries):
            cp.first_pass_done = True

        _merge_shot_state(cp, ledger)
        if not progressed and not ledger.has_active_jobs():
            cp.repair_epoch += 1
            cp.idle_ticks += 1
            cp.tick_interval_s = min(
                SUPERVISOR_TICK_MAX_INTERVAL_S,
                cp.tick_interval_s * 1.5,
            )
        else:
            if progressed:
                cp.idle_ticks = 0
                cp.tick_interval_s = SUPERVISOR_TICK_INTERVAL_S
        await _save_checkpoint_async(cp, run_id=run_id)
        await asyncio.sleep(cp.tick_interval_s)
