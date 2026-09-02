from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.db import get_conn, new_id, now


ACTIVE_RESERVATIONS = {"reserved", "running"}


@dataclass(frozen=True, slots=True)
class Claim:
    recovered: bool = False


def reserve_budget(
    job_id: str,
    episode_id: str,
    amount_cny: float,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Record one payable job's reservation against the episode ledger.

    金额已不构成生成拦截（会员分档时长制）。此前保留过一个从不比较的
    ``limit_cny`` 形参只是为了不必改动全部调用点签名；超限即拦截并把 job
    打成 ``paused_budget`` 的那条分支已删除（见 CLAUDE.md「Retiring
    Features」）。``limit_cny`` 本身已随「往上游收一步」的第二轮退场
    (2026-09-02) 一并删除——它是本函数体内唯一从不被读取的形参，删掉的同时
    连带删了它唯一的上游来源 ``episode_video_budget_limit()``（曾恒返回
    ``math.inf``，经 ``ensure_media_trace()`` 写进
    ``workflow_runs.budget_limit_cny`` 后把 ``GET /api/system/jobs`` 的
    ``json.dumps`` 炸成 500）。"""
    db = conn or get_conn()
    owns_transaction = not db.in_transaction
    amount = max(0.0, float(amount_cny))
    try:
        if owns_transaction:
            db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT status FROM budget_reservations WHERE job_id=?", (job_id,)
        ).fetchone()
        if existing and existing["status"] in ACTIVE_RESERVATIONS:
            if owns_transaction:
                db.commit()
            return True
        db.execute(
            """INSERT INTO budget_reservations(
                   id, job_id, scope_type, scope_id, amount_cny, status, created_at
               ) VALUES(?,?, 'episode', ?, ?, 'reserved', ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   amount_cny=excluded.amount_cny, status='reserved', settled_at=NULL,
                   actual_cost_cny=NULL""",
            (new_id("budget"), job_id, episode_id, amount, now()),
        )
        db.execute(
            "UPDATE jobs SET reserved_cost_cny=?, updated_at=? WHERE id=?",
            (amount, now(), job_id),
        )
        if owns_transaction:
            db.commit()
        return True
    except Exception:
        if owns_transaction:
            db.rollback()
        raise


def settle_budget(job_id: str, actual_cost_cny: float, *, success: bool) -> None:
    db = get_conn()
    status = "settled" if success else "released"
    db.execute(
        "UPDATE budget_reservations SET status=?, settled_at=?, actual_cost_cny=? WHERE job_id=?",
        (status, now(), max(0.0, float(actual_cost_cny)) if success else 0.0, job_id),
    )
    db.execute("UPDATE jobs SET reserved_cost_cny=0 WHERE id=?", (job_id,))
    db.commit()


def reconcile_cancelled_version_states(
    episode_id: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Project durable job cancellation onto its version after late worker writes."""
    db = conn or get_conn()
    rows = db.execute(
        """SELECT j.version_id, j.abandoned, j.error AS job_error,
                  v.status AS version_status, v.error AS version_error
             FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
            WHERE j.cancellation_requested=1
              AND (? IS NULL OR j.episode_id=?)""",
        (episode_id, episode_id),
    ).fetchall()
    changed = 0
    for row in rows:
        target_status = "abandoned" if bool(row["abandoned"]) else "cancelled"
        target_error = row["job_error"] or row["version_error"]
        if (
            row["version_status"] == target_status
            and row["version_error"] == target_error
        ):
            continue
        cursor = db.execute(
            """UPDATE shot_versions
                  SET status=?, error=?, video_slot_active=0
                WHERE id=?
                  AND EXISTS (
                      SELECT 1 FROM jobs j
                       WHERE j.version_id=shot_versions.id
                         AND j.cancellation_requested=1
                  )""",
            (target_status, target_error, row["version_id"]),
        )
        changed += int(cursor.rowcount)
    if changed:
        db.commit()
    return changed


def claim_job(
    job_id: str,
    owner: str,
    *,
    lease_seconds: float = 120.0,
    conn: sqlite3.Connection | None = None,
    commit: bool = True,
) -> Claim | None:
    """CAS claim a due queued / waiting_provider job, or reclaim an expired lease."""
    db = conn or get_conn()
    stamp = now()
    expires = stamp + max(5.0, float(lease_seconds))
    row = db.execute("SELECT status, lease_expires_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    recovered = row["status"] == "running"
    cursor = db.execute(
        """UPDATE jobs SET status='running', lease_owner=?, lease_expires_at=?,
                  attempt_started_at=COALESCE(attempt_started_at, ?), updated_at=?
           WHERE id=? AND cancellation_requested=0 AND abandoned=0 AND (
               (status IN ('queued','waiting_provider') AND (next_retry_at IS NULL OR next_retry_at<=?)) OR
               (status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?))
           )""",
        (owner, expires, stamp, stamp, job_id, stamp, stamp),
    )
    if cursor.rowcount != 1:
        if commit:
            db.rollback()
        return None
    db.execute(
        "UPDATE budget_reservations SET status='running' WHERE job_id=? AND status='reserved'",
        (job_id,),
    )
    if commit:
        db.commit()
    return Claim(recovered=recovered)


def renew_lease(job_id: str, owner: str, *, lease_seconds: float = 120.0) -> bool:
    db = get_conn()
    cursor = db.execute(
        """UPDATE jobs SET lease_expires_at=?, updated_at=?
           WHERE id=? AND status='running' AND lease_owner=? AND cancellation_requested=0""",
        (now() + max(5.0, float(lease_seconds)), now(), job_id, owner),
    )
    db.commit()
    return cursor.rowcount == 1


def request_cancel(job_id: str, *, reason: str = "用户已停止视频任务") -> dict[str, object]:
    """Release local generation authority without losing accepted provider work."""
    db = get_conn()
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    row = db.execute(
        """SELECT j.status,j.provider_non_cancellable,j.provider_create_state,
                  j.version_id,j.run_id,j.step_run_id,j.episode_id,
                  j.provider_operation_id,
                  v.provider_task_id
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id=j.version_id
            WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        raise KeyError(job_id)
    cancellable = {
        "queued", "running", "waiting_provider", "waiting_retry",
        "waiting", "waiting_human",
    }
    if row["status"] not in cancellable:
        return {
            "job_id": job_id,
            "status": row["status"],
            "provider_may_continue": bool(row["provider_non_cancellable"]),
            "cancelled": False,
        }
    non_cancellable = bool(row["provider_non_cancellable"])
    provider_accepted = bool(
        row["provider_create_state"] == "accepted"
        and (row["provider_task_id"] or non_cancellable)
    )
    if provider_accepted:
        message = (
            f"{reason}；供应商已接单，继续轮询原任务，迟到结果只进入隔离审计"
        )
        cursor = db.execute(
            """UPDATE jobs
                  SET status='waiting_provider',error=?,
                      video_slot_active=0,provider_poll_required=1,
                      provider_result_adoptable=0,
                      cancellation_requested=0,abandoned=0,
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=?,
                      updated_at=?
                WHERE id=? AND status IN (
                    'queued','running','waiting_provider',
                    'waiting_retry','waiting','waiting_human'
                )""",
            (message, now(), now(), job_id),
        )
        if cursor.rowcount != 1:
            db.rollback()
            current = db.execute(
                "SELECT status,provider_non_cancellable FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if current is None:
                raise KeyError(job_id)
            return {
                "job_id": job_id,
                "status": current["status"],
                "provider_may_continue": bool(
                    current["provider_non_cancellable"]
                ),
                "cancelled": False,
            }
        if row["version_id"]:
            db.execute(
                """UPDATE shot_versions
                      SET status='waiting_provider',error=?,
                          video_slot_active=0
                    WHERE id=?""",
                (message, row["version_id"]),
            )
        db.execute(
            """UPDATE budget_reservations
                  SET status='reserved',settled_at=NULL,actual_cost_cny=NULL
                WHERE job_id=?""",
            (job_id,),
        )
        db.commit()
        from app.orchestration.media_runs import mark_media_job_state

        mark_media_job_state(
            row["run_id"],
            row["step_run_id"],
            "waiting_provider",
            message,
        )
        return {
            "job_id": job_id,
            "status": "waiting_provider",
            "provider_may_continue": True,
            "cancelled": True,
            "result_isolated": True,
        }
    # A crashed process may already have recovered a provider-backed job from
    # running to queued. provider_non_cancellable remains the durable truth that
    # paid upstream work can still complete, so cancellation must stay abandoned.
    status = "abandoned" if non_cancellable else "cancelled"
    pre_transport_cancel = bool(
        not non_cancellable
        and not row["provider_task_id"]
        and row["provider_create_state"] in {"not_started", "submitting"}
    )
    cursor = db.execute(
        """UPDATE jobs SET cancellation_requested=1, abandoned=?, status=?, error=?,
                  video_slot_active=0,lease_owner=NULL,lease_expires_at=NULL,
                  next_retry_at=NULL,
                  provider_create_state=CASE WHEN ? THEN 'not_started'
                                             ELSE provider_create_state END,
                  provider_non_cancellable=CASE WHEN ? THEN 0
                                                ELSE provider_non_cancellable END,
                  updated_at=?
           WHERE id=? AND status IN (
               'queued','running','waiting_provider','waiting_retry','waiting','waiting_human'
           )""",
        (
            int(status == "abandoned"),
            status,
            reason,
            int(pre_transport_cancel),
            int(pre_transport_cancel),
            now(),
            job_id,
        ),
    )
    if cursor.rowcount != 1:
        db.rollback()
        current = db.execute(
            "SELECT status, provider_non_cancellable FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not current:
            raise KeyError(job_id)
        return {
            "job_id": job_id,
            "status": current["status"],
            "provider_may_continue": bool(current["provider_non_cancellable"]),
            "cancelled": False,
        }
    if row["version_id"]:
        db.execute(
            """UPDATE shot_versions
                  SET status=?,error=?,video_slot_active=0
                WHERE id=? AND status IN ('queued','running')""",
            (status, reason, row["version_id"]),
        )
    provider_claim_released = False
    if pre_transport_cancel and row["provider_operation_id"]:
        stamp = now()
        released = db.execute(
            """UPDATE provider_video_budget_claims
                  SET status='released',updated_at=?,released_at=?
                WHERE operation_id=? AND job_id=? AND status='reserved'
                  AND accepted_at IS NULL""",
            (
                stamp,
                stamp,
                row["provider_operation_id"],
                job_id,
            ),
        )
        provider_claim_released = released.rowcount > 0
        db.execute(
            """UPDATE budget_reservations
                  SET status='released',settled_at=?,actual_cost_cny=0
                WHERE job_id=? AND status IN ('reserved','running')""",
            (stamp, job_id),
        )
        db.execute(
            "UPDATE jobs SET reserved_cost_cny=0 WHERE id=?",
            (job_id,),
        )
    db.commit()
    # 上游已接单：预算从 reserved/running 转为 committed 口径——保留 settled 审计，
    # 金额记为预估（不可真正取消上游时不能直接释放为 0 假装没花钱）。
    if non_cancellable:
        reserved = db.execute(
            "SELECT amount_cny FROM budget_reservations WHERE job_id=?", (job_id,)
        ).fetchone()
        estimate = float(reserved["amount_cny"]) if reserved else 0.0
        db.execute(
            "UPDATE budget_reservations SET status='settled', settled_at=?, actual_cost_cny=? WHERE job_id=?",
            (now(), estimate, job_id),
        )
        db.execute("UPDATE jobs SET reserved_cost_cny=0 WHERE id=?", (job_id,))
        db.commit()
    elif not pre_transport_cancel:
        settle_budget(job_id, 0.0, success=False)
    reconcile_cancelled_version_states(
        episode_id=row["episode_id"],
        conn=db,
    )
    from app.orchestration.media_runs import mark_media_job_state
    mark_media_job_state(row["run_id"], row["step_run_id"], status, reason)
    return {
        "job_id": job_id,
        "status": status,
        "provider_may_continue": non_cancellable,
        "cancelled": True,
        "provider_claim_released": provider_claim_released,
    }


def recoverable_jobs() -> list[tuple[str, float]]:
    """Return every recoverable job and its remaining delay.

    Including future retry timestamps is essential: after a process restart no
    in-memory timer exists to enqueue those jobs later.
    waiting_provider 任务同样需要恢复轮询，否则重启后上游视频无人收尾。
    """
    db = get_conn()
    stamp = now()
    # 过期 lease 的 running：若上游已接单，回到 waiting_provider；否则回 queued。
    # 回收站项目的残留 job 保持排除在外：不把它复位成可调度状态，它就永远
    # 停在 running，不会被下面的派发资格扫描或 worker_lifecycle 的调度器重新
    # 捞起——24 小时（或账号级联 30 天）后回收站清理会连这一行一并处理掉。
    expired = db.execute(
        """SELECT id, provider_non_cancellable, provider_create_state, version_id FROM jobs
           WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?)
             AND cancellation_requested=0 AND abandoned=0
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS: startup recovery scans
               -- every owner's stale-leased media jobs after a process
               -- reload/restart; excludes soft-deleted (recycle-bin)
               -- projects so their residual jobs are not re-armed and do
               -- not burn quota
                WHERE p.id=jobs.project_id AND p.deleted_at IS NOT NULL
             )""",
        (stamp,),
    ).fetchall()
    for row in expired:
        has_provider = False
        if row["version_id"]:
            v = db.execute(
                "SELECT provider_task_id FROM shot_versions WHERE id=?", (row["version_id"],)
            ).fetchone()
            has_provider = bool(v and v["provider_task_id"])
        new_status = (
            "waiting_provider"
            if has_provider or (
                bool(row["provider_non_cancellable"]) and row["provider_create_state"] == "accepted"
            )
            else "queued"
        )
        db.execute(
            """UPDATE jobs SET status=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND status='running'""",
            (new_status, stamp, row["id"]),
        )
    rows = db.execute(
        """SELECT id, next_retry_at FROM jobs
           WHERE status IN ('queued','waiting_provider') AND cancellation_requested=0 AND abandoned=0
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS: startup recovery's
               -- actual dispatch-eligibility scan, run for every owner;
               -- excludes soft-deleted (recycle-bin) projects so their
               -- residual jobs are not reported as recoverable and do not
               -- burn quota
                WHERE p.id=jobs.project_id AND p.deleted_at IS NOT NULL
             )
           ORDER BY created_at""",
    ).fetchall()
    db.commit()
    return [
        (str(row["id"]), max(0.0, float(row["next_retry_at"] or stamp) - stamp))
        for row in rows
    ]
