"""attempts 预算换算、容量投影与 shot_state/预算视图向 checkpoint 合并。"""
from __future__ import annotations

import asyncio

from typing import Any

from app.completion_grant import DEFAULT_VIDEO_BUDGET_CAP_CNY
from app.db import get_conn

from .authority import _supervisor_checks_can_use_worker_thread
from .constants import (
    COST_PER_SECOND_CNY,
    FIRST_PASS_BUDGET_FRACTION,
    MAX_ATTEMPTS_PER_SHOT,
    MIN_ATTEMPTS_PER_SHOT,
    SHOT_BUDGET_MULTIPLIER,
)
from .coverage import rebuild_coverage_ledger
from .models import CoverageLedger, ShotCoverageEntry, VideoSupervisorCheckpoint



def attempts_for(entry: ShotCoverageEntry, ledger: CoverageLedger, *, budget_cap_cny: float) -> int:
    duration = 5.0
    conn = get_conn()
    row = conn.execute(
        "SELECT duration_s, episode_id FROM shots WHERE id=?", (entry.shot_id,)
    ).fetchone()
    episode_id = None
    if row:
        duration = float(row["duration_s"] or 5)
        episode_id = row["episode_id"]
    try:
        from app.video_cost_model import predict_shot_completion_cost
        pred = predict_shot_completion_cost(
            duration, episode_id=episode_id, grade=entry.grade,
        )
        est = float(pred["unit_cny"])
    except Exception:  # noqa: BLE001
        est = duration * COST_PER_SECOND_CNY + 1.0
    remaining = max(0.0, budget_cap_cny - ledger.cost_spent)
    uncovered = max(1, ledger.count_uncovered())
    affordable = int(remaining / (uncovered * max(est, 0.5))) if est > 0 else 0
    base = MIN_ATTEMPTS_PER_SHOT
    if entry.chain_position == 0 and entry.chain_len > 1:
        base += 1
    if entry.grade == "C":
        base += 1
    return max(MIN_ATTEMPTS_PER_SHOT, min(MAX_ATTEMPTS_PER_SHOT, min(base + affordable, MAX_ATTEMPTS_PER_SHOT)))


def _rebuild_budgeted_coverage_ledger(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int,
    budget_cap_cny: float,
) -> CoverageLedger:
    """Build the read-heavy ledger outside the uvicorn event-loop thread."""
    ledger = rebuild_coverage_ledger(
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
    )
    for entry in ledger.entries:
        entry.attempts_budgeted = attempts_for(
            entry,
            ledger,
            budget_cap_cny=budget_cap_cny,
        )
    return ledger


async def _rebuild_budgeted_coverage_ledger_async(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int,
    budget_cap_cny: float,
) -> CoverageLedger:
    if not _supervisor_checks_can_use_worker_thread():
        return _rebuild_budgeted_coverage_ledger(
            episode_id,
            cp=cp,
            fallback_quota=fallback_quota,
            budget_cap_cny=budget_cap_cny,
        )
    return await asyncio.to_thread(
        _rebuild_budgeted_coverage_ledger,
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
        budget_cap_cny=budget_cap_cny,
    )


async def _rebuild_coverage_ledger_async(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int | None = None,
) -> CoverageLedger:
    if not _supervisor_checks_can_use_worker_thread():
        return rebuild_coverage_ledger(
            episode_id,
            cp=cp,
            fallback_quota=fallback_quota,
        )
    return await asyncio.to_thread(
        rebuild_coverage_ledger,
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
    )


def _has_dispatch_budget_capacity(
    episode_id: str,
    entry: ShotCoverageEntry,
    *,
    budget_cap_cny: float,
) -> bool:
    """Project budget capacity before running the expensive shot preflight.

    ``media_scheduler.reserve_budget`` remains the atomic authority. This
    projection reads the same durable costs and active reservations so the
    Supervisor observes in-flight jobs instead of compiling versions which can
    only enter ``paused_budget``.
    """
    from app.video_cost_model import initial_shot_generation_cost

    conn = get_conn()
    shot = conn.execute(
        "SELECT duration_s FROM shots WHERE id=? AND episode_id=?",
        (entry.shot_id, episode_id),
    ).fetchone()
    if shot is None:
        return False
    estimate = initial_shot_generation_cost(float(shot["duration_s"] or 5.0))
    spent = float(conn.execute(
        """SELECT COALESCE(SUM(v.cost_cny), 0) AS amount
             FROM shot_versions v JOIN shots s ON s.id=v.shot_id
            WHERE s.episode_id=? AND v.status='succeeded'""",
        (episode_id,),
    ).fetchone()["amount"] or 0)
    reserved = float(conn.execute(
        """SELECT COALESCE(SUM(amount_cny), 0) AS amount
             FROM budget_reservations
            WHERE scope_type='episode' AND scope_id=?
              AND status IN ('reserved','running')""",
        (episode_id,),
    ).fetchone()["amount"] or 0)
    return spent + reserved + float(estimate) <= float(budget_cap_cny) + 1e-9


def _budget_view(cp: VideoSupervisorCheckpoint, ledger: CoverageLedger) -> dict[str, float]:
    cap = float((cp.budget or {}).get("cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY)
    return {
        "cap_cny": cap,
        "spent_cny": float(ledger.cost_spent),
        "first_pass_soft_cap_cny": cap * FIRST_PASS_BUDGET_FRACTION,
        "per_shot_cap_cny": (cap / max(1, ledger.shots_total)) * SHOT_BUDGET_MULTIPLIER,
        "remaining_cny": max(0.0, cap - ledger.cost_spent),
    }


def _merge_shot_state(cp: VideoSupervisorCheckpoint, ledger: CoverageLedger) -> None:
    state: dict[str, dict[str, Any]] = {}
    for e in ledger.entries:
        state[str(e.shot_no)] = {
            "grade": e.grade,
            "shot_id": e.shot_id,
            "adopted_version_id": e.adopted_version_id,
            "attempts_paid": e.attempts_paid,
            "attempts_dispatched": e.attempts_dispatched,
            "attempts_budgeted": e.attempts_budgeted,
            "no_charge_requeues": e.no_charge_requeues,
            "repair_level": e.repair_level,
            "issue_fingerprint_counts": e.issue_fingerprint_counts,
            "qa_history": e.qa_history,
            "continuity_degraded": e.continuity_degraded,
            "chain_stale": e.chain_stale,
            "rebuilt_reference": e.rebuilt_reference,
            "fatal_repeat_count": e.fatal_repeat_count,
            "last_issue_codes": e.last_issue_codes,
            "fallback_reason": e.fallback_reason,
            "video_stale": e.video_stale,
        }
    cp.shot_state = state
    cp.coverage = {
        "A": ledger.grades.get("A", 0),
        "B": ledger.grades.get("B", 0),
        "C": ledger.grades.get("C", 0),
        "total": ledger.shots_total,
        "adopted": sum(1 for entry in ledger.entries if entry.adopted_version_id),
        "unadopted": sum(1 for entry in ledger.entries if not entry.adopted_version_id),
        "fallback_quota": ledger.fallback_quota,
        "coverage_rate": ledger.coverage_rate,
    }
    cp.budget = _budget_view(cp, ledger)
