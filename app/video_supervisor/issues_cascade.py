"""镜头 Issue 收集与降级/回退级联传播。"""
from __future__ import annotations

import json

from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.evidence.media import select_best_video_candidate
from app.harness.types import Issue
from app.video_issues import issues_from_job_failure, issues_from_qa, load_persisted_shot_issues
from app.video_repair_router import MAX_CHAIN_CASCADE_DEPTH, should_cascade, state_drift_significant

from .models import CoverageLedger, ShotCoverageEntry, VideoSupervisorCheckpoint



def _collect_issues(
    entry: ShotCoverageEntry,
    *,
    run_id: str | None = None,
) -> list[Issue]:
    issues = load_persisted_shot_issues(entry.shot_id, run_id=run_id)
    conn = get_conn()
    if entry.best_version_id:
        row = conn.execute(
            "SELECT * FROM shot_versions WHERE id=?", (entry.best_version_id,)
        ).fetchone()
        if row:
            qa = json.loads(row["qa_json"] or "{}")
            technical = json.loads(row["technical_validation_json"] or "{}")
            issues.extend(issues_from_qa(
                qa, technical, shot_id=entry.shot_id,
                version_id=row["id"], shot_no=entry.shot_no,
            ))
    if not issues:
        # 终态失败（供应商已明确拒绝：provider_create_state='model_rejected'
        # 或 disposition='external_terminal'）不受 owner_run_id 过滤——这件事
        # 本身已经成了定论，不因为哪个 run 发现的而失效。其余失败（例如尚未
        # 升级的 manual_review 瞬时错误）继续按 run_id 过滤：手动重开一个新
        # run 仍然可以甩掉旧 run 留下的瞬时错误，这是 load_persisted_shot_issues
        # docstring 里写明的原设计意图，不在本次改动范围内。
        # ORDER BY 里让终态失败优先于时间新旧：终态一旦成立就必须是权威结论，
        # 不能被同一镜头之后又出现的非终态记录（理论上不该发生，但不能靠“不
        # 会发生”兜底）用时间戳顺序悄悄盖过。
        failed = conn.execute(
            """SELECT * FROM jobs
               WHERE shot_id=? AND kind='video'
                 AND status IN ('failed','waiting_human')
                 AND (
                     provider_create_state='model_rejected'
                     OR provider_failure_disposition='external_terminal'
                     OR (? IS NULL OR owner_run_id=?)
                 )
               ORDER BY
                 CASE WHEN provider_create_state='model_rejected'
                           OR provider_failure_disposition='external_terminal'
                      THEN 0 ELSE 1 END,
                 created_at DESC
               LIMIT 1""",
            (entry.shot_id, run_id, run_id),
        ).fetchone()
        if failed:
            failed_version = None
            if failed["version_id"]:
                failed_version = conn.execute(
                    "SELECT * FROM shot_versions WHERE id=?",
                    (failed["version_id"],),
                ).fetchone()
            issues = issues_from_job_failure(
                dict(failed),
                dict(failed_version) if failed_version else None,
                shot_id=entry.shot_id,
                shot_no=entry.shot_no,
            )
    if not issues and entry.last_issue_codes:
        issues = [
            Issue(
                code=code,
                severity="blocker",  # type: ignore[arg-type]
                subject=entry.shot_id,
                message=code,
                evidence={"shot_no": entry.shot_no, "path": str(entry.shot_no), "rule_id": code},
            )
            for code in entry.last_issue_codes
        ]
    return issues


def _apply_cascade(entry: ShotCoverageEntry, ledger: CoverageLedger, cp: VideoSupervisorCheckpoint) -> list[int]:
    """标记下游 chain_stale；超深度则 degrade。"""
    cascaded: list[int] = []
    observed = None
    if entry.best_version_id:
        conn = get_conn()
        row = conn.execute(
            "SELECT qa_json FROM shot_versions WHERE id=?", (entry.best_version_id,)
        ).fetchone()
        if row:
            qa = json.loads(row["qa_json"] or "{}")
            observed = qa.get("observed_state_out")
    planned = None
    conn = get_conn()
    shot = conn.execute(
        "SELECT shot_contract_json, last_frame_desc FROM shots WHERE id=?",
        (entry.shot_id,),
    ).fetchone()
    if shot:
        try:
            contract = json.loads(shot["shot_contract_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            contract = {}
        if isinstance(contract, dict):
            planned = contract.get("state_out")
        planned = planned or shot["last_frame_desc"]
    drift = state_drift_significant(planned, observed)

    for other in ledger.entries:
        if other.shot_no <= entry.shot_no:
            continue
        if other.chain_head_shot_no != entry.chain_head_shot_no:
            continue
        if other.chain_position - entry.chain_position > MAX_CHAIN_CASCADE_DEPTH:
            # 超深度：降链而非重烧
            other.continuity_degraded = True
            cp.shot_state.setdefault(str(other.shot_no), {})["continuity_degraded"] = True
            continue
        if should_cascade(entry, other, state_drift=drift):
            other.chain_stale = True
            cascaded.append(other.shot_no)
    return cascaded


def _adopt_fallback(entry: ShotCoverageEntry, *, episode_id: str, run_id: str | None) -> bool:
    result = select_best_video_candidate(entry.shot_id)
    if not result:
        return False
    entry.grade = result.get("grade") or "B"  # type: ignore[assignment]
    if entry.grade == "A":
        entry.grade = "B"
    entry.fallback_reason = result.get("fallback_reason") or result.get("reason")
    entry.adopted_version_id = result.get("version_id")
    if run_id:
        evidence_repository.append_event(
            run_id, "VIDEO_FALLBACK_ADOPTED", "warning",
            f"第 {entry.shot_no} 镜 B 级兜底采纳",
            payload={
                "shot_no": entry.shot_no,
                "version_id": entry.adopted_version_id,
                "fallback_reason": entry.fallback_reason,
            },
        )
    return True
