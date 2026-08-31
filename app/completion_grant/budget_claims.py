"""供应商预算认领与负债关闭：把一次真实的供应商调用记进台账并占用额度。
"""
from __future__ import annotations



from app.db import now
from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
)
from app.completion_grant.ledger import ensure_video_budget_authority_tables

def reserve_provider_video_budget(
    *,
    episode_id: str,
    job_id: str,
    version_id: str,
    operation_id: str,
    amount_cny: float,
    conn,
) -> bool:
    """Atomically claim one provider create cost.

    A payable create without episode authority is rejected. When a caller
    supplies an active transaction, the claim participates in that transaction.
    """
    amount = max(0.0, float(amount_cny))
    db = conn
    if db.in_transaction:
        tables = {
            str(row["name"])
            for row in db.execute(
                """SELECT name FROM sqlite_master
                    WHERE type='table' AND name IN (
                        'episode_video_budget_authorities',
                        'provider_video_budget_claims'
                    )"""
            ).fetchall()
        }
        if tables != {
            "episode_video_budget_authorities",
            "provider_video_budget_claims",
        }:
            return False
    else:
        ensure_video_budget_authority_tables(db)
    owns_transaction = not db.in_transaction
    try:
        if owns_transaction:
            db.execute("BEGIN IMMEDIATE")
        authority = db.execute(
            "SELECT baseline_cny,cap_cny FROM episode_video_budget_authorities WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        if authority is None:
            if owns_transaction:
                db.rollback()
            return False
        scope = db.execute(
            """SELECT e.project_id,s.episode_id,s.id AS shot_id
                 FROM jobs j
                 JOIN shot_versions v ON v.id=?
                 JOIN shots s ON s.id=v.shot_id
                 JOIN episodes e ON e.id=s.episode_id
                WHERE j.id=? AND j.version_id=v.id AND j.shot_id=s.id
                  AND j.episode_id=s.episode_id AND s.episode_id=?
                  AND (j.project_id IS NULL OR j.project_id=e.project_id)""",
            (version_id, job_id, episode_id),
        ).fetchone()
        if scope is None:
            raise ValueError(
                "provider budget claim ownership does not align with "
                "job/version/shot/episode"
            )
        existing = db.execute(
            """SELECT project_id,origin_episode_id,origin_shot_id,
                      origin_job_id,origin_version_id,amount_cny,status
                 FROM provider_video_budget_claims WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        if existing:
            existing_owner = (
                str(existing["project_id"]),
                str(existing["origin_episode_id"]),
                str(existing["origin_shot_id"]),
                str(existing["origin_job_id"]),
                str(existing["origin_version_id"]),
            )
            requested_owner = (
                str(scope["project_id"]),
                episode_id,
                str(scope["shot_id"]),
                job_id,
                version_id,
            )
            if (
                existing_owner != requested_owner
                or abs(float(existing["amount_cny"]) - amount) > 1e-9
            ):
                raise ValueError(
                    "provider operation is already owned by a different "
                    "budget claim"
                )
            if existing["status"] != "released":
                if owns_transaction:
                    db.commit()
                return True
        claimed = float(db.execute(
            """SELECT COALESCE(SUM(amount_cny),0) AS amount
                 FROM provider_video_budget_claims
                WHERE episode_id=? AND status!='released'""",
            (episode_id,),
        ).fetchone()["amount"] or 0)
        used = float(authority["baseline_cny"] or 0) + claimed
        cap = float(authority["cap_cny"] or 0)
        if used + amount > cap + 1e-9:
            if owns_transaction:
                db.rollback()
            return False
        stamp = now()
        db.execute(
            """INSERT INTO provider_video_budget_claims(
                   operation_id,project_id,episode_id,shot_id,job_id,version_id,
                   origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
                   amount_cny,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)
               ON CONFLICT(operation_id) DO UPDATE SET
                   project_id=excluded.project_id,
                   episode_id=excluded.episode_id,
                   shot_id=excluded.shot_id,
                   job_id=excluded.job_id,
                   version_id=excluded.version_id,
                   origin_episode_id=excluded.origin_episode_id,
                   origin_shot_id=excluded.origin_shot_id,
                   origin_job_id=excluded.origin_job_id,
                   origin_version_id=excluded.origin_version_id,
                   amount_cny=excluded.amount_cny,
                   status='reserved',
                   accepted_at=NULL,
                   settled_at=NULL,
                   released_at=NULL,
                   liability_closed_at=NULL,
                   closure_reason=NULL,
                   updated_at=excluded.updated_at""",
            (
                operation_id,
                scope["project_id"],
                episode_id,
                scope["shot_id"],
                job_id,
                version_id,
                episode_id,
                scope["shot_id"],
                job_id,
                version_id,
                amount,
                stamp,
                stamp,
            ),
        )
        if owns_transaction:
            db.commit()
        return True
    except Exception:
        if owns_transaction:
            db.rollback()
        raise


def close_provider_video_budget_claim_liability(
    operation_id: str,
    *,
    job_id: str,
    reason: str,
    conn,
) -> bool:
    """Close recovery for an accepted operation without releasing its budget.

    This terminal is used only after an explicit decision to abandon recovery
    and create a new provider operation. The conservative claim amount remains
    project-used because the old provider charge cannot be disproved.
    """
    closure_reason = str(reason or "").strip()
    if not closure_reason:
        raise ValueError("provider claim liability closure requires a reason")
    db = conn
    ensure_video_budget_authority_tables(db)
    existing = db.execute(
        """SELECT status FROM provider_video_budget_claims
            WHERE operation_id=? AND job_id=?""",
        (operation_id, job_id),
    ).fetchone()
    if existing is None:
        return False
    if existing["status"] == "released":
        raise ValueError(
            "released provider claim cannot become a chargeable liability"
        )
    if existing["status"] in {"settled", "closed_liability"}:
        return True
    stamp = now()
    cursor = db.execute(
        """UPDATE provider_video_budget_claims
              SET status='closed_liability',updated_at=?,
                  liability_closed_at=?,closure_reason=?
            WHERE operation_id=? AND job_id=?
              AND status!='released' AND status!='settled'
              AND status!='closed_liability'""",
        (stamp, stamp, closure_reason, operation_id, job_id),
    )
    if conn is None:
        db.commit()
    return cursor.rowcount == 1
