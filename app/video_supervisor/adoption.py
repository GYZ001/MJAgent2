"""就绪候选自动采用与覆盖报告落盘。"""
from __future__ import annotations

import asyncio
import json

from typing import Any

from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.evidence.media import select_best_video_candidate
from app.harness.types import Evaluation, EvidenceArtifact

from .authority import _supervisor_checks_can_use_worker_thread
from .budget import _merge_shot_state, _rebuild_coverage_ledger_async
from .checkpoint import _save_checkpoint_async
from .constants import REPORT_ARTIFACT_TYPE
from .models import CoverageLedger, VideoSupervisorCheckpoint



def _adopt_ready_candidates(
    ledger: CoverageLedger,
    *,
    run_id: str | None,
) -> int:
    """正常运行期采用首个技术有效候选；QA 只影响风险展示。

    每处理完一个镜头就立即 ``conn.commit()``，不攒到循环末尾再提交一次。
    ``reconcile_adopted_revision`` 是显式 ``conn=`` 调用，按契约故意不提
    交（见 app/video_plan.py 尾部 ``if conn is None: db.commit()``），历史
    上靠"调用方之后紧接着的 save_checkpoint 恰好在同一根线程上顺带把它提
    交掉"补上——那只是 threading.local 连接缓存在同步顺序调用下的副作用，
    不是契约保证；并发补齐把 asyncio.to_thread 派到另一根线程时，
    save_checkpoint 会拿到全新连接（``in_transaction=False``），只提交它
    自己那次写入，这里的写入就悬空，或被未来某次不相关提交顺走。
    按镜头提交而不是整批提交一次，是因为这个函数本来就不是"全部采用成
    功或都不生效"的单一事务：``select_best_video_candidate`` 对每个镜头
    的采用（shots.adopted_version_id / shot_versions.adoption_reason /
    delivery_packages 失效）早已经自成一次独立提交，在 ``reconcile_
    adopted_revision`` 被调用之前就已经落盘。如果改成攒到循环末尾统一提
    交一次，一旦后面某个镜头的 ``reconcile_adopted_revision`` 抛出异常，
    外层 ``run_video_completion_resilient`` 的回滚会把本次循环里此前已经
    成功处理、且其采用早已独立落盘的镜头的依赖图同步（video_plan_
    dependencies / shot_video_generation_plans / jobs 的 stale 标记）一并
    卷入回滚，造成"已采用但依赖图未同步"的新不一致——比现在的悬空提交更
    糟。按镜头提交把每次失败的影响面收窄到当前镜头自己，不新增跨镜头的
    原子性假设。
    """
    adopted = 0
    from app.video_plan import reconcile_adopted_revision
    conn = get_conn()

    for entry in ledger.entries:
        if entry.adopted_version_id:
            reconcile_adopted_revision(
                entry.shot_id,
                entry.adopted_version_id,
                conn=conn,
            )
            conn.commit()
            continue
        if not entry.best_version_id:
            continue
        result = select_best_video_candidate(entry.shot_id, force_best=False)
        if not result:
            continue
        entry.adopted_version_id = result.get("version_id")
        if not entry.adopted_version_id:
            continue
        reconcile_adopted_revision(
            entry.shot_id,
            entry.adopted_version_id,
            conn=conn,
        )
        conn.commit()
        entry.fallback_reason = result.get("fallback_reason")
        adopted += 1
        if run_id:
            evidence_repository.append_event(
                run_id,
                "VIDEO_CANDIDATE_ADOPTED",
                "info",
                f"第 {entry.shot_no} 镜由 Supervisor 采用候选",
                payload={"shot_no": entry.shot_no, "version_id": entry.adopted_version_id},
            )
    return adopted


def _has_unadopted_ready_candidate(episode_id: str) -> bool:
    row = get_conn().execute(
        """SELECT 1
             FROM shots s
             JOIN shot_versions v ON v.shot_id=s.id
            WHERE s.episode_id=? AND s.adopted_version_id IS NULL
              AND v.status='succeeded'
              AND v.video_path IS NOT NULL AND v.video_path!=''
              AND json_valid(v.technical_validation_json)
              AND json_extract(v.technical_validation_json,'$.passed')=1
            LIMIT 1""",
        (episode_id,),
    ).fetchone()
    return row is not None


async def _adopt_ready_candidates_incrementally(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    fallback_quota: int,
    run_id: str | None,
) -> int:
    threaded = _supervisor_checks_can_use_worker_thread()
    has_ready = (
        await asyncio.to_thread(_has_unadopted_ready_candidate, episode_id)
        if threaded
        else _has_unadopted_ready_candidate(episode_id)
    )
    if not has_ready:
        return 0
    observed = await _rebuild_coverage_ledger_async(
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
    )
    adopted = (
        await asyncio.to_thread(
            _adopt_ready_candidates,
            observed,
            run_id=run_id,
        )
        if threaded
        else _adopt_ready_candidates(observed, run_id=run_id)
    )
    if not adopted:
        return 0
    refreshed = await _rebuild_coverage_ledger_async(
        episode_id,
        cp=cp,
        fallback_quota=fallback_quota,
    )
    _merge_shot_state(cp, refreshed)
    await _save_checkpoint_async(cp, run_id=run_id)
    return adopted


def _write_coverage_report(
    cp: VideoSupervisorCheckpoint,
    ledger: CoverageLedger,
    *,
    outcome: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = {
        "episode_id": cp.episode_id,
        "run_id": cp.run_id,
        "outcome": outcome,
        "terminal_reason": cp.terminal_reason,
        "started_at": cp.started_at,
        "deadline_at": cp.deadline_at,
        "finished_at": cp.finished_at,
        "grades": ledger.grades,
        "fallback_quota": ledger.fallback_quota,
        "quality_target_missed": cp.quality_target_missed,
        "missing_shots": cp.missing_shots,
        "closeout_adoptions": cp.closeout_adoptions,
        "cost_spent_cny": ledger.cost_spent,
        "budget_cap_cny": (cp.budget or {}).get("cap_cny"),
        "shots": [
            {
                "shot_no": e.shot_no,
                "shot_id": e.shot_id,
                "grade": e.grade,
                "adopted_version_id": e.adopted_version_id,
                "best_version_id": e.best_version_id,
                "qa_overall": e.best_qa_overall,
                "cost_spent_cny": e.cost_spent_cny,
                "repair_level": e.repair_level,
                "continuity_degraded": e.continuity_degraded,
                "fallback_reason": e.fallback_reason,
                "attempts_paid": e.attempts_paid,
                "attempts_budgeted": e.attempts_budgeted,
                "last_issue_codes": e.last_issue_codes,
            }
            for e in ledger.entries
        ],
        "last_plan": cp.last_plan,
        "repair_epoch": cp.repair_epoch,
        **(extra or {}),
    }
    # 以 run + outcome 为幂等键；旧报告不能吞掉新一轮的终态报告。
    rows = get_conn().execute(
        """SELECT content_json FROM artifacts
           WHERE type=? AND scope_type='episode' AND scope_id=?
             AND status IN ('validated','approved')
           ORDER BY created_at DESC LIMIT 20""",
        (REPORT_ARTIFACT_TYPE, cp.episode_id),
    ).fetchall()
    for row in rows:
        try:
            old = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if old.get("run_id") == cp.run_id and old.get("outcome") == outcome:
            return old
    art = evidence_repository.create_artifact(EvidenceArtifact(
        type=REPORT_ARTIFACT_TYPE,
        scope_type="episode",
        scope_id=cp.episode_id,
        status="validated",
        trust_level="T2",
        content=report,
        contract_version="video-coverage-1.1.0",
    ))
    evidence_repository.create_evaluation(
        art["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="video_coverage_report",
            evaluator_version="1.1.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"grades": ledger.grades, "outcome": outcome},
        ),
    )
    return report
