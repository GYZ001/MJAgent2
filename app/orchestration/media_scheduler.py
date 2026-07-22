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
    amount = max(0.0, float(amount_cny))
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT status FROM budget_reservations WHERE job_id=?", (job_id,)
        ).fetchone()
        if existing and existing["status"] in ACTIVE_RESERVATIONS:
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


def claim_job(job_id: str, owner: str, *, lease_seconds: float = 120.0) -> Claim | None:
    """CAS claim a due queued job, or reclaim an expired lease."""
    db = get_conn()
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
               (status='queued' AND (next_retry_at IS NULL OR next_retry_at<=?)) OR
               (status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?))
           )""",
        (owner, expires, stamp, stamp, job_id, stamp, stamp),
    )
    if cursor.rowcount != 1:
        db.rollback()
        return None
    db.execute(
        "UPDATE budget_reservations SET status='running' WHERE job_id=? AND status='reserved'",
        (job_id,),
    )
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


def request_cancel(job_id: str) -> dict[str, object]:
    """Cancel locally; paid provider work may continue and is marked abandoned on completion."""
    db = get_conn()
    row = db.execute(
        "SELECT status, provider_non_cancellable, version_id, run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row:
        raise KeyError(job_id)
    non_cancellable = bool(row["provider_non_cancellable"])
    # A crashed process may already have recovered a provider-backed job from
    # running to queued. provider_non_cancellable remains the durable truth that
    # paid upstream work can still complete, so cancellation must stay abandoned.
    status = "abandoned" if non_cancellable else "cancelled"
    db.execute(
        """UPDATE jobs SET cancellation_requested=1, abandoned=?, status=?,
                  lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?""",
        (int(status == "abandoned"), status, now(), job_id),
    )
    if row["version_id"]:
        db.execute(
            "UPDATE shot_versions SET status=? WHERE id=? AND status IN ('queued','running')",
            (status, row["version_id"]),
        )
    db.commit()
    settle_budget(job_id, 0.0, success=False)
    from app.orchestration.media_runs import mark_media_job_state
    mark_media_job_state(row["run_id"], row["step_run_id"], status, "用户取消媒体任务")
    return {"job_id": job_id, "status": status, "provider_may_continue": non_cancellable}


def recoverable_jobs() -> list[tuple[str, float]]:
    """Return every recoverable job and its remaining delay.

    Including future retry timestamps is essential: after a process restart no
    in-memory timer exists to enqueue those jobs later.
    """
    db = get_conn()
    stamp = now()
    db.execute(
        """UPDATE jobs SET status='queued', lease_owner=NULL, lease_expires_at=NULL,
                  updated_at=? WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?)
                  AND cancellation_requested=0 AND abandoned=0""",
        (stamp, stamp),
    )
    rows = db.execute(
        """SELECT id, next_retry_at FROM jobs
           WHERE status='queued' AND cancellation_requested=0 AND abandoned=0
           ORDER BY created_at""",
    ).fetchall()
    db.commit()
    return [
        (str(row["id"]), max(0.0, float(row["next_retry_at"] or stamp) - stamp))
        for row in rows
    ]
