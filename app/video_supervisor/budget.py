"""attempts 配额换算与 shot_state/coverage 视图向 checkpoint 合并。

金额不再构成生成拦截（会员分档时长制，非按金额计费）：``attempts_for`` 只按
``MIN_ATTEMPTS_PER_SHOT``/``MAX_ATTEMPTS_PER_SHOT`` 加链首/C 级加成计算配额，
不再换算成本。``_has_dispatch_budget_capacity``/``_budget_view`` 两个金额判断
与视图已删除——见 CLAUDE.md「Retiring Features」与本次「成本预算拦截体系
退场」。
"""
from __future__ import annotations

import asyncio

from typing import Any

from .authority import _supervisor_checks_can_use_worker_thread
from .constants import MAX_ATTEMPTS_PER_SHOT, MIN_ATTEMPTS_PER_SHOT
from .coverage import rebuild_coverage_ledger
from .models import CoverageLedger, ShotCoverageEntry, VideoSupervisorCheckpoint



def attempts_for(entry: ShotCoverageEntry, ledger: CoverageLedger) -> int:
    base = MIN_ATTEMPTS_PER_SHOT
    if entry.chain_position == 0 and entry.chain_len > 1:
        base += 1
    if entry.grade == "C":
        base += 1
    return max(MIN_ATTEMPTS_PER_SHOT, min(MAX_ATTEMPTS_PER_SHOT, base))


def _rebuild_budgeted_coverage_ledger(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int,
) -> CoverageLedger:
    """Build the read-heavy ledger outside the uvicorn event-loop thread."""
    ledger = rebuild_coverage_ledger(
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
    )
    for entry in ledger.entries:
        entry.attempts_budgeted = attempts_for(entry, ledger)
    return ledger


async def _rebuild_budgeted_coverage_ledger_async(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int,
) -> CoverageLedger:
    if not _supervisor_checks_can_use_worker_thread():
        return _rebuild_budgeted_coverage_ledger(
            episode_id,
            cp=cp,
            fallback_quota=fallback_quota,
        )
    return await asyncio.to_thread(
        _rebuild_budgeted_coverage_ledger,
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
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
