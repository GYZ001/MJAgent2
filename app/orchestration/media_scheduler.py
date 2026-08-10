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
    limit_cny: float,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Atomically reserve episode budget before a payable job can run."""
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
        spent = db.execute(
            """SELECT COALESCE(SUM(v.cost_cny), 0) AS amount
               FROM shot_versions v JOIN shots s ON s.id=v.shot_id
               WHERE s.episode_id=? AND v.status='succeeded'""",
            (episode_id,),
        ).fetchone()["amount"]
        reserved = db.execute(
            """SELECT COALESCE(SUM(amount_cny), 0) AS amount
               FROM budget_reservations
               WHERE scope_type='episode' AND scope_id=? AND status IN ('reserved','running')
                 AND job_id!=?""",
            (episode_id, job_id),
        ).fetchone()["amount"]
        if float(spent) + float(reserved) + amount > float(limit_cny) + 1e-9:
            db.execute(
                "UPDATE jobs SET status='paused_budget', reserved_cost_cny=0, updated_at=? WHERE id=?",
                (now(), job_id),
            )
            if owns_transaction:
                db.commit()
            return False
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


def extend_budget_reservation(
    job_id: str,
    episode_id: str,
    additional_cny: float,
    limit_cny: float,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Atomically enlarge one active reservation before an intentional paid resubmit."""
    db = conn or get_conn()
    additional = max(0.0, float(additional_cny))
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            """SELECT amount_cny, status FROM budget_reservations
               WHERE job_id=?""",
            (job_id,),
        ).fetchone()
        current = (
            float(existing["amount_cny"] or 0)
            if existing and existing["status"] in ACTIVE_RESERVATIONS
            else 0.0
        )
        spent = db.execute(
            """SELECT COALESCE(SUM(v.cost_cny), 0) AS amount
               FROM shot_versions v JOIN shots s ON s.id=v.shot_id
               WHERE s.episode_id=? AND v.status='succeeded'""",
            (episode_id,),
        ).fetchone()["amount"]
        reserved = db.execute(
            """SELECT COALESCE(SUM(amount_cny), 0) AS amount
               FROM budget_reservations
               WHERE scope_type='episode' AND scope_id=?
                 AND status IN ('reserved','running') AND job_id!=?""",
            (episode_id, job_id),
        ).fetchone()["amount"]
        target = current + additional
        if float(spent) + float(reserved) + target > float(limit_cny) + 1e-9:
            db.rollback()
            return False
        if existing:
            db.execute(
                """UPDATE budget_reservations
                   SET amount_cny=?, status='reserved', settled_at=NULL,
                       actual_cost_cny=NULL
                   WHERE job_id=?""",
                (target, job_id),
            )
        else:
            db.execute(
                """INSERT INTO budget_reservations(
                       id, job_id, scope_type, scope_id, amount_cny, status, created_at
                   ) VALUES(?,?, 'episode', ?, ?, 'reserved', ?)""",
                (new_id("budget"), job_id, episode_id, target, now()),
            )
        db.execute(
            "UPDATE jobs SET reserved_cost_cny=?, updated_at=? WHERE id=?",
            (target, now(), job_id),
        )
        db.commit()
        return True
    except Exception:
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
                  SET status=?, error=?
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


def release_lease(job_id: str, owner: str, status: str, error: str | None = None) -> bool:
    db = get_conn()
    cursor = db.execute(
        """UPDATE jobs SET status=?, error=?, lease_owner=NULL, lease_expires_at=NULL,
                  updated_at=? WHERE id=? AND lease_owner=?""",
        (status, error, now(), job_id, owner),
    )
    db.commit()
    return cursor.rowcount == 1


def schedule_retry(job_id: str, message: str, *, max_retries: int, base_delay: float) -> float | None:
    """Persist retry count and due time. Returns due timestamp, or None when exhausted."""
    db = get_conn()
    row = db.execute("SELECT retry_count FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    attempt = int(row["retry_count"] or 0) + 1
    if attempt > max(0, int(max_retries)):
        return None
    due = now() + max(0.0, float(base_delay)) * (2 ** (attempt - 1))
    db.execute(
        """UPDATE jobs SET status='queued', error=?, retry_count=?, next_retry_at=?,
                  lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?""",
        (message, attempt, due, now(), job_id),
    )
    db.execute(
        "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
        (job_id,),
    )
    db.commit()
    return due


def request_cancel(job_id: str, *, reason: str = "用户已停止视频任务") -> dict[str, object]:
    """Cancel locally; paid provider work may continue and is marked abandoned on completion."""
    db = get_conn()
    row = db.execute(
        """SELECT status, provider_non_cancellable, version_id, run_id,
                  step_run_id, episode_id
             FROM jobs WHERE id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        raise KeyError(job_id)
    cancellable = {
        "queued", "running", "paused_budget", "waiting_provider", "waiting_retry",
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
    # A crashed process may already have recovered a provider-backed job from
    # running to queued. provider_non_cancellable remains the durable truth that
    # paid upstream work can still complete, so cancellation must stay abandoned.
    status = "abandoned" if non_cancellable else "cancelled"
    cursor = db.execute(
        """UPDATE jobs SET cancellation_requested=1, abandoned=?, status=?, error=?,
                  lease_owner=NULL, lease_expires_at=NULL, next_retry_at=NULL, updated_at=?
           WHERE id=? AND status IN (
               'queued','running','paused_budget','waiting_provider','waiting_retry','waiting','waiting_human'
           )""",
        (int(status == "abandoned"), status, reason, now(), job_id),
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
            "UPDATE shot_versions SET status=?, error=? WHERE id=? AND status IN ('queued','running','paused_budget')",
            (status, reason, row["version_id"]),
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
    else:
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
    }


def recoverable_jobs() -> list[tuple[str, float]]:
    """Return every recoverable job and its remaining delay.

    Including future retry timestamps is essential: after a process restart no
    in-memory timer exists to enqueue those jobs later.
    waiting_provider 任务同样需要恢复轮询，否则重启后上游视频无人收尾。
    """
    db = get_conn()
    stamp = now()
    # 过期 lease 的 running：若上游已接单，回到 waiting_provider；否则回 queued
    expired = db.execute(
        """SELECT id, provider_non_cancellable, provider_create_state, version_id FROM jobs
           WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?)
             AND cancellation_requested=0 AND abandoned=0""",
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
           ORDER BY created_at""",
    ).fetchall()
    db.commit()
    return [
        (str(row["id"]), max(0.0, float(row["next_retry_at"] or stamp) - stamp))
        for row in rows
    ]
