"""截止时间收口与全片覆盖终态收敛。"""
from __future__ import annotations

import json

from typing import Any

from app.completion_grant import consume_grant
from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.evidence.media import select_best_video_candidate

from .adoption import _write_coverage_report
from .budget import _merge_shot_state
from .checkpoint import _run_checkpoint_write, save_checkpoint
from .constants import TERMINAL_SUPERVISOR_PHASES
from .coverage import rebuild_coverage_ledger
from .job_control import _release_episode_supervisor, _stop_supervised_video_jobs
from .models import CoverageLedger, VideoSupervisorCheckpoint



def _deadline_closeout(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    reason: str = "VIDEO_WALL_CLOCK_EXCEEDED",
) -> VideoSupervisorCheckpoint:
    """不可逆、幂等的截止收口：停止派发，停止任务，采用每镜最佳技术可播候选。"""
    if cp.phase in TERMINAL_SUPERVISOR_PHASES:
        _release_episode_supervisor(cp.episode_id, run_id=run_id or cp.run_id)
        return cp
    cp.phase = "DEADLINE_CLOSING"
    cp.terminal_reason = reason
    cp.dispatch_fenced_at = cp.dispatch_fenced_at or now()
    cp.closeout_started_at = cp.closeout_started_at or now()
    save_checkpoint(cp, run_id=run_id)

    stopped = _stop_supervised_video_jobs(
        cp.episode_id,
        run_id=run_id or cp.run_id,
        reason=f"Supervisor 收口：{reason}",
    )
    ledger = rebuild_coverage_ledger(
        cp.episode_id,
        cp=cp,
        fallback_quota=int(cp.coverage.get("fallback_quota") or 0),
    )
    adopted_at_closeout: list[dict[str, Any]] = list(cp.closeout_adoptions)
    already_recorded = {str(item.get("shot_id")) for item in adopted_at_closeout}
    conn = get_conn()
    for entry in ledger.entries:
        if entry.adopted_version_id:
            row = conn.execute(
                "SELECT adoption_reason, qa_json FROM shot_versions WHERE id=?",
                (entry.adopted_version_id,),
            ).fetchone()
            if (
                entry.shot_id not in already_recorded
                and row
                and str(row["adoption_reason"] or "").startswith("截止收口由 Supervisor")
            ):
                try:
                    adopted_qa = json.loads(row["qa_json"] or "{}")
                    adopted_score = adopted_qa.get("overall")
                except (TypeError, ValueError, json.JSONDecodeError):
                    adopted_score = None
                adopted_at_closeout.append({
                    "shot_no": entry.shot_no,
                    "shot_id": entry.shot_id,
                    "version_id": entry.adopted_version_id,
                    "qa_overall": adopted_score,
                    "risk": row["adoption_reason"],
                })
                already_recorded.add(entry.shot_id)
            # adopted 是不可被补齐流程覆盖的用户结果；技术/QA 风险写报告，
            # 但截止收口也不得换版。
            continue
        result = select_best_video_candidate(entry.shot_id)
        if not result:
            continue
        version_id = result.get("version_id")
        reason_text = (
            f"截止收口由 Supervisor 强制采用：在技术可播候选中选择最佳版本；"
            f"QA 仅作为排序和风险标记。{result.get('reason') or ''}"
        )
        conn.execute(
            "UPDATE shot_versions SET adoption_reason=? WHERE id=?",
            (reason_text, version_id),
        )
        conn.commit()
        if entry.shot_id not in already_recorded:
            adopted_at_closeout.append({
                "shot_no": entry.shot_no,
                "shot_id": entry.shot_id,
                "version_id": version_id,
                "qa_overall": entry.best_qa_overall,
                "risk": result.get("fallback_reason") or result.get("reason"),
            })
            already_recorded.add(entry.shot_id)

    cp.closeout_adoptions = adopted_at_closeout
    ledger = rebuild_coverage_ledger(
        cp.episode_id,
        cp=cp,
        fallback_quota=int(cp.coverage.get("fallback_quota") or 0),
    )
    cp.missing_shots = [e.shot_no for e in ledger.entries if not e.adopted_version_id]
    cp.quality_target_missed = bool(
        cp.missing_shots or any(e.grade != "A" for e in ledger.entries)
    )
    cp.finished_at = now()
    cp.outcome = (
        "PARTIAL_NO_USABLE_CANDIDATE"
        if cp.missing_shots
        else "COMPLETED_DEADLINE_FALLBACK"
    )
    cp.phase = cp.outcome  # type: ignore[assignment]
    _merge_shot_state(cp, ledger)
    report = _write_coverage_report(
        cp,
        ledger,
        outcome=cp.outcome,
        extra={
            "stopped_jobs": stopped,
            "deterministic_fallbacks": {
                "disabled": True,
                "reason": "静态图片、轻运动卡和静音片段不具备视频采用资格",
            },
        },
    )
    if cp.grant_id:
        try:
            consume_grant(cp.grant_id)
        except Exception:  # noqa: BLE001
            pass
    save_checkpoint(cp, run_id=run_id)
    _release_episode_supervisor(cp.episode_id, run_id=run_id or cp.run_id)
    if run_id:
        evidence_repository.append_event(
            run_id,
            "VIDEO_DEADLINE_CLOSED",
            "warning" if cp.missing_shots else "info",
            f"截止收口完成；采用 {len(cp.closeout_adoptions)} 镜，缺失 {cp.missing_shots}",
            payload=report,
        )
    from app.observability.metrics import inc
    inc(
        "video_supervisor_deadline_fallback_adopted_total",
        value=len(cp.closeout_adoptions),
        episode_id=cp.episode_id,
        terminal_reason=reason,
    )
    inc(
        "video_supervisor_deadline_missing_shots_total",
        value=len(cp.missing_shots),
        episode_id=cp.episode_id,
        terminal_reason=reason,
    )
    inc(
        "video_supervisor_deadline_closeout_seconds",
        value=max(0, int((cp.finished_at or now()) - (cp.closeout_started_at or now()))),
        episode_id=cp.episode_id,
    )
    return cp


def _finalize_covered(
    cp: VideoSupervisorCheckpoint, ledger: CoverageLedger, *, run_id: str | None
) -> VideoSupervisorCheckpoint:
    cp.phase = "FINALIZING"
    save_checkpoint(cp, run_id=run_id)
    cp.phase = "SUCCEEDED_COVERED"
    cp.outcome = "SUCCEEDED_COVERED"
    cp.terminal_reason = "COVERAGE_TARGET_MET"
    cp.finished_at = now()
    cp.missing_shots = []
    cp.quality_target_missed = any(
        entry.grade != "A" or entry.video_stale or entry.chain_stale
        for entry in ledger.entries
    )
    _merge_shot_state(cp, ledger)
    report = _write_coverage_report(cp, ledger, outcome=cp.outcome)
    if cp.grant_id:
        try:
            consume_grant(cp.grant_id)
        except Exception:  # noqa: BLE001
            pass
    save_checkpoint(cp, run_id=run_id)
    _release_episode_supervisor(cp.episode_id, run_id=run_id or cp.run_id)
    if run_id:
        evidence_repository.append_event(
            run_id, "VIDEO_COVERAGE_COMPLETED", "info",
            f"全片覆盖完成 A={ledger.grades.get('A')} B={ledger.grades.get('B')}",
            payload=report,
        )
    return cp


async def _deadline_closeout_async(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    reason: str,
) -> VideoSupervisorCheckpoint:
    return await _run_checkpoint_write(
        _deadline_closeout,
        cp,
        run_id=run_id,
        reason=reason,
    )


async def _finalize_covered_async(
    cp: VideoSupervisorCheckpoint,
    ledger: CoverageLedger,
    *,
    run_id: str | None,
) -> VideoSupervisorCheckpoint:
    return await _run_checkpoint_write(
        _finalize_covered,
        cp,
        ledger,
        run_id=run_id,
    )
