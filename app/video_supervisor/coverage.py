"""覆盖台账重建：逐镜分级、连续性链与陈旧性判定。"""
from __future__ import annotations

import json
import math

from typing import Any

from app.continuity import classify_video_hard_failures
from app.db import get_conn
from app.evidence.media import grade_shot_video, video_candidate_selection_score
from app.media_pipeline.stages import ACTIVE_JOB_STATUSES
from app.schemas import Shot
from app.video_issues import issues_from_job_failure, load_persisted_shot_issues

from .constants import MIN_ATTEMPTS_PER_SHOT
from .models import CoverageLedger, ShotCoverageEntry, VideoSupervisorCheckpoint, _adopted_video_is_usable



def _human_adopted(conn, shot_id: str) -> bool:
    """是否存在人工采用 Gate。``gate_decisions`` 表无 payload_json 列，勿查询该列。"""
    row = conn.execute(
        """SELECT id FROM gate_decisions
           WHERE gate_key='video_adoption' AND decision IN ('approve','approve_with_risk')
             AND artifact_id IN (
               SELECT artifact_id FROM shot_versions WHERE shot_id=? AND artifact_id IS NOT NULL
             )
           LIMIT 1""",
        (shot_id,),
    ).fetchone()
    return bool(row)


def _compute_chains(shot_rows: list[Any]) -> dict[str, tuple[int, int, int]]:
    """shot_id → (chain_head_no, chain_position, chain_len)。"""
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    from app.schemas import Shot

    def to_model(row) -> Shot:
        return Shot(
            shot_no=row["shot_no"],
            duration_s=row["duration_s"] or 5,
            shot_size=row["shot_size"] or "中景",
            camera_move=row["camera_move"] or "固定",
            scene_setting=row["scene_setting"] or "",
            characters=json.loads(row["characters"] or "[]"),
            action_desc=row["action_desc"] or "",
            continuity_from_prev=bool(row["continuity_from_prev"]),
            continuity_mode=(row["continuity_mode"] if "continuity_mode" in row.keys() else None),
        )

    models = [to_model(r) for r in shot_rows]
    uses_tail = []
    for i, m in enumerate(models):
        prev = models[i - 1] if i > 0 else None
        mode = derive_continuity_mode(m, prev)
        uses_tail.append(uses_previous_tail_frame(mode) and i > 0)

    # 分段
    result: dict[str, tuple[int, int, int]] = {}
    i = 0
    n = len(shot_rows)
    while i < n:
        head = i
        j = i + 1
        while j < n and uses_tail[j]:
            j += 1
        length = j - head
        for k in range(head, j):
            result[shot_rows[k]["id"]] = (
                int(shot_rows[head]["shot_no"]),
                k - head,
                length,
            )
        i = j
    return result


def _video_stale_for_shot(conn, shot_row, episode_storyboard_id: str | None) -> bool:
    """分镜变更后旧视频失效。"""
    adopted = shot_row["adopted_version_id"]
    if not adopted:
        return False
    # 镜级 storyboard artifact 与 episode 当前不一致
    shot_art = None
    try:
        shot_art = shot_row["storyboard_artifact_id"]
    except (KeyError, IndexError, TypeError):
        shot_art = None
    if episode_storyboard_id and shot_art and shot_art != episode_storyboard_id:
        episode_art = conn.execute(
            "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
            (episode_storyboard_id,),
        ).fetchone()
        try:
            episode_parents = json.loads(
                episode_art["parent_artifact_ids_json"] or "[]"
            ) if episode_art else []
        except (TypeError, ValueError):
            episode_parents = []
        # Current storyboard aggregates directly parent their per-shot artifacts.
        # A shot artifact inside the approved aggregate is current, not stale.
        if shot_art not in episode_parents:
            return True
    ver = conn.execute(
        "SELECT artifact_id FROM shot_versions WHERE id=?", (adopted,)
    ).fetchone()
    if not ver or not ver["artifact_id"]:
        return False
    art = conn.execute(
        "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
        (ver["artifact_id"],),
    ).fetchone()
    if not art:
        return False
    try:
        parents = json.loads(art["parent_artifact_ids_json"] or "[]")
    except (TypeError, ValueError):
        parents = []
    if not episode_storyboard_id:
        return False
    if not parents:
        return False
    valid_storyboard_parents = {episode_storyboard_id}
    if shot_art:
        valid_storyboard_parents.add(shot_art)
    return not any(parent in valid_storyboard_parents for parent in parents)



def _latest_video_jobs(
    conn: Any, shot_ids: list[str], supervisor_run_id: str | None,
) -> tuple[dict[str, str], set[str]]:
    """每镜的在途 job，以及「最新一个 job 以供应商真实拒绝告终」的镜头。
    两者都不按 supervisor 轮次过滤：占槽（uq_versions_active_video_shot）是镜头级
    排他的，上一轮/服务重启前遗留的在途 job 一样占着槽，这一轮再派只会撞唯一索引
    （2026-09-05 我欲封天第 2 集：「视频输入校验未通过：UNIQUE constraint failed:
    shot_versions.shot_id」转人工）；拒绝的是这段内容本身，换一轮也不会变。
    只要镜头后来又派了新 job（任何状态），它就是最新的，旧拒绝不再成立。
    """
    _ = supervisor_run_id
    active_jobs: dict[str, str] = {}
    rejected: set[str] = set()
    if not shot_ids:
        return active_jobs, rejected
    placeholders = ",".join("?" * len(shot_ids))
    status_list = tuple(ACTIVE_JOB_STATUSES)
    status_ph = ",".join("?" * len(status_list))
    for row in conn.execute(
        f"""SELECT id, shot_id FROM jobs
            WHERE shot_id IN ({placeholders}) AND kind='video'
              AND status IN ({status_ph})
            ORDER BY created_at DESC""",
        (*shot_ids, *status_list),
    ).fetchall():
        if row["shot_id"] not in active_jobs:
            active_jobs[row["shot_id"]] = row["id"]
    seen: set[str] = set()
    for row in conn.execute(
        f"""SELECT shot_id, status, provider_create_state FROM jobs
            WHERE shot_id IN ({placeholders}) AND kind='video'
            ORDER BY created_at DESC, rowid DESC""",
        tuple(shot_ids),
    ).fetchall():
        sid = row["shot_id"]
        if sid in seen:
            continue
        seen.add(sid)
        if row["status"] == "failed" and row["provider_create_state"] == "model_rejected":
            rejected.add(sid)
    return active_jobs, rejected


def rebuild_coverage_ledger(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint | None = None,
    fallback_quota: int | None = None,
) -> CoverageLedger:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    shot_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    shot_no_by_id = {str(row["id"]): int(row["shot_no"]) for row in shot_rows}
    dependency_map: dict[str, tuple[str | None, bool]] = {}
    plan_row = conn.execute(
        """SELECT id FROM episode_video_generation_plans
           WHERE episode_id=? AND status='valid'
           ORDER BY plan_revision DESC, created_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    if plan_row:
        for dep in conn.execute(
            """SELECT p.shot_id,p.depends_on_shot_id,
                      d.upstream_adopted_version_id
                 FROM shot_video_generation_plans p
                 LEFT JOIN video_plan_dependencies d
                   ON d.shot_plan_id=p.id
                WHERE p.episode_video_plan_id=?""",
            (plan_row["id"],),
        ).fetchall():
            depends_on = str(dep["depends_on_shot_id"] or "") or None
            dependency_map[str(dep["shot_id"])] = (
                depends_on,
                not depends_on or bool(dep["upstream_adopted_version_id"]),
            )
    chains = _compute_chains(shot_rows)
    ep_sb = ep["storyboard_artifact_id"] if ep else None
    supervisor_run_id = cp.run_id if cp is not None else None

    # 批量读 jobs / versions / costs
    shot_ids = [r["id"] for r in shot_rows]
    active_jobs, rejected_shots = _latest_video_jobs(conn, shot_ids, supervisor_run_id)

    cost_map: dict[str, float] = {}
    attempts_map: dict[str, int] = {}
    dispatch_map: dict[str, int] = {}
    best_map: dict[str, dict[str, Any]] = {}
    if shot_ids:
        placeholders = ",".join("?" * len(shot_ids))
        for row in conn.execute(
            f"""SELECT shot_id, id, qa_json, technical_validation_json, cost_cny,
                       provider_task_id, status, image_inputs, version_no
                FROM shot_versions
                WHERE shot_id IN ({placeholders}) AND status!='cleared'""",
            shot_ids,
        ).fetchall():
            sid = row["shot_id"]
            try:
                version_meta = json.loads(row["image_inputs"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                version_meta = {}
            is_delivery_fallback = bool(version_meta.get("delivery_fallback"))
            if is_delivery_fallback:
                # 清理前遗留的图片兜底不算视频版本、尝试次数或覆盖率。
                continue
            if (
                supervisor_run_id is None
                or version_meta.get("supervisor_run_id") == supervisor_run_id
            ):
                dispatch_map[sid] = dispatch_map.get(sid, 0) + 1
            cost_map[sid] = cost_map.get(sid, 0.0) + float(row["cost_cny"] or 0)
            if (
                row["provider_task_id"]
                or row["status"] in {"succeeded", "failed", "running", "queued"}
            ):
                # 产生过 provider 任务或进入执行的版本计为 paid attempt
                if row["provider_task_id"] or row["status"] == "succeeded":
                    paid_attempts = max(
                        1,
                        int(version_meta.get("provider_paid_attempts") or 0),
                    )
                    attempts_map[sid] = attempts_map.get(sid, 0) + paid_attempts
            if row["status"] != "succeeded":
                continue
            qa = json.loads(row["qa_json"] or "{}")
            technical = json.loads(row["technical_validation_json"] or "{}")
            if not technical.get("passed"):
                continue
            try:
                score = float(qa.get("overall")) if qa.get("overall") is not None else -1.0
            except (TypeError, ValueError):
                score = -1.0
            hard_failures = classify_video_hard_failures(qa, technical=technical)
            qa_recovered = bool(qa.get("qa_recovered") or qa.get("status") == "unverified")
            selection_score = video_candidate_selection_score(
                score, hard_failures, qa_recovered=qa_recovered,
            )
            rank = (not qa_recovered, selection_score, score, int(row["version_no"] or 0))
            cur = best_map.get(sid)
            if cur is None or rank >= cur["rank"]:
                best_map[sid] = {
                    "id": row["id"],
                    "score": score,
                    "rank": rank,
                    "qa": qa,
                    "technical": technical,
                    "image_inputs": row["image_inputs"],
                }

    quota = fallback_quota
    if quota is None and cp and cp.coverage:
        quota = int(cp.coverage.get("fallback_quota") or 0)
    if quota is None:
        quota = max(1, int(math.ceil(len(shot_rows) * 0.2)))

    entries: list[ShotCoverageEntry] = []
    grades = {"A": 0, "B": 0, "C": 0}
    total_cost = 0.0
    prev_state = (cp.shot_state if cp else {}) or {}

    for row in shot_rows:
        sid = row["id"]
        saved = prev_state.get(str(row["shot_no"])) or prev_state.get(sid) or {}
        chain_head, chain_pos, chain_len = chains.get(sid, (row["shot_no"], 0, 1))
        best = best_map.get(sid)
        adopted_version_id = row["adopted_version_id"]
        if adopted_version_id:
            adopted_row = conn.execute(
                """SELECT status,image_inputs,video_path,
                          technical_validation_json
                     FROM shot_versions WHERE id=?""",
                (adopted_version_id,),
            ).fetchone()
            if not _adopted_video_is_usable(adopted_row):
                adopted_version_id = None
        graded = grade_shot_video(
            sid,
            technical=(best or {}).get("technical"),
            qa=(best or {}).get("qa"),
            version_row={
                "id": (best or {}).get("id"),
                "image_inputs": (best or {}).get("image_inputs"),
                "technical_validation_json": json.dumps((best or {}).get("technical") or {}),
                "qa_json": json.dumps((best or {}).get("qa") or {}),
            } if best else None,
            continuity_degraded=bool(saved.get("continuity_degraded")),
        )
        grade = graded["grade"]
        stale = _video_stale_for_shot(conn, row, ep_sb)
        if stale and grade in {"A", "B"}:
            grade = "C"
        grades[grade] = grades.get(grade, 0) + 1
        cost = float(cost_map.get(sid, 0.0))
        total_cost += cost
        qa_history = list(saved.get("qa_history") or [])
        if graded["qa_overall"] is not None:
            if not qa_history or qa_history[-1] != graded["qa_overall"]:
                qa_history = (qa_history + [float(graded["qa_overall"])])[-8:]
        gain = None
        if len(qa_history) >= 2:
            gain = qa_history[-1] - qa_history[-2]

        persisted = load_persisted_shot_issues(
            sid,
            run_id=supervisor_run_id,
        )
        last_codes = list(saved.get("last_issue_codes") or [])
        if persisted:
            last_codes = [i.code for i in persisted]

        # 若有失败 job 无成功版，补充 issue
        if grade == "C" and not best and not persisted:
            fail_job = conn.execute(
                """SELECT * FROM jobs WHERE shot_id=? AND kind='video'
                   AND status IN ('failed','waiting_human')
                   AND (? IS NULL OR owner_run_id=?)
                   ORDER BY created_at DESC LIMIT 1""",
                (sid, supervisor_run_id, supervisor_run_id),
            ).fetchone()
            if fail_job:
                fail_ver = None
                if fail_job["version_id"]:
                    fail_ver = conn.execute(
                        "SELECT * FROM shot_versions WHERE id=?", (fail_job["version_id"],)
                    ).fetchone()
                job_issues = issues_from_job_failure(
                    dict(fail_job), dict(fail_ver) if fail_ver else None,
                    shot_id=sid, shot_no=row["shot_no"],
                )
                if job_issues:
                    last_codes = [i.code for i in job_issues]

        observed_attempts = int(attempts_map.get(sid, 0))
        depends_on_shot_id, dependency_ready = dependency_map.get(
            str(sid),
            (None, True),
        )
        try:
            checkpoint_attempts = int(saved.get("attempts_paid") or 0)
        except (TypeError, ValueError):
            checkpoint_attempts = 0

        entry = ShotCoverageEntry(
            shot_no=int(row["shot_no"]),
            shot_id=sid,
            grade=grade,  # type: ignore[arg-type]
            adopted_version_id=adopted_version_id,
            best_version_id=(best or {}).get("id"),
            best_qa_overall=graded["qa_overall"],
            qa_gain_last_2=gain,
            # Checkpoints remember policy history, but the durable version ledger is
            # authoritative for attempts completed after the previous checkpoint.
            # Never let a stale checkpoint move the counter backwards.
            attempts_paid=max(checkpoint_attempts, observed_attempts),
            attempts_dispatched=max(
                int(saved.get("attempts_dispatched") or 0),
                int(dispatch_map.get(sid, 0)),
            ),
            attempts_budgeted=int(saved.get("attempts_budgeted") or MIN_ATTEMPTS_PER_SHOT),
            no_charge_requeues=int(saved.get("no_charge_requeues") or 0),
            cost_spent_cny=cost,
            last_issue_codes=last_codes,
            issue_fingerprint_counts=dict(saved.get("issue_fingerprint_counts") or {}),
            repair_level=saved.get("repair_level") or "L0",
            chain_head_shot_no=chain_head,
            chain_position=chain_pos,
            chain_len=chain_len,
            blocked_by_shot_no=(
                shot_no_by_id.get(depends_on_shot_id)
                if depends_on_shot_id and not dependency_ready
                else None
            ),
            depends_on_shot_id=depends_on_shot_id,
            dependency_ready=dependency_ready,
            chain_stale=bool(saved.get("chain_stale")),
            active_job_id=active_jobs.get(sid),
            provider_rejected=sid in rejected_shots,
            human_adopted=_human_adopted(conn, sid),
            continuity_degraded=bool(saved.get("continuity_degraded") or graded.get("continuity_degraded")),
            never_attempted=dispatch_map.get(sid, 0) == 0 and not saved.get("attempts_dispatched"),
            qa_history=qa_history,
            rebuilt_reference=bool(saved.get("rebuilt_reference")),
            fatal_repeat_count=int(saved.get("fatal_repeat_count") or 0),
            fallback_reason=graded.get("fallback_reason") or saved.get("fallback_reason"),
            video_stale=stale,
        )
        # blocked_by
        if entry.active_job_id:
            job = conn.execute(
                "SELECT after_shot_id, pipeline_stage FROM jobs WHERE id=?",
                (entry.active_job_id,),
            ).fetchone()
            if job and job["after_shot_id"] and (job["pipeline_stage"] or "").endswith("waiting_continuity"):
                prev = conn.execute(
                    "SELECT shot_no FROM shots WHERE id=?", (job["after_shot_id"],)
                ).fetchone()
                if prev:
                    entry.blocked_by_shot_no = int(prev["shot_no"])
        entries.append(entry)

    covered = sum(1 for entry in entries if entry.adopted_version_id)
    total = len(entries)
    return CoverageLedger(
        episode_id=episode_id,
        shots_total=total,
        grades=grades,
        coverage_rate=(covered / total) if total else 0.0,
        fallback_quota=int(quota),
        entries=entries,
        cost_spent=total_cost,
    )
