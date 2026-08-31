"""供应商预算认领台账（provider_video_budget_claims）与历史负债迁移。

建表、列形态探测、旧数据归属推断与迁移。上层的预算授权、认领、对账都建立在
这张台账上。
"""
from __future__ import annotations

import json


from app.db import now
from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
)
from app.completion_grant.models import _PROVIDER_CLAIM_LEDGER_COLUMNS

def _create_provider_claim_ledger_table(db, table_name: str) -> None:
    if table_name not in {
        "provider_video_budget_claims",
        "provider_video_budget_claims_v2",
    }:
        raise ValueError("invalid provider claim ledger table name")
    db.execute(
        f"""CREATE TABLE {table_name} (
               operation_id TEXT PRIMARY KEY,
               project_id TEXT NOT NULL,
               episode_id TEXT,
               shot_id TEXT,
               job_id TEXT,
               version_id TEXT,
               origin_episode_id TEXT NOT NULL,
               origin_shot_id TEXT,
               origin_job_id TEXT NOT NULL,
               origin_version_id TEXT NOT NULL,
               amount_cny REAL NOT NULL,
               status TEXT NOT NULL,
               liability_source TEXT NOT NULL DEFAULT 'provider_operation',
               created_at REAL NOT NULL,
               updated_at REAL NOT NULL,
               accepted_at REAL,
               settled_at REAL,
               released_at REAL,
               liability_closed_at REAL,
               closure_reason TEXT,
               FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
               FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE SET NULL,
               FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE SET NULL,
               FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL,
               FOREIGN KEY(version_id) REFERENCES shot_versions(id) ON DELETE SET NULL
           )"""
    )


def _provider_claim_ledger_is_current(db) -> bool:
    columns = {
        str(row["name"])
        for row in db.execute(
            "PRAGMA table_info(provider_video_budget_claims)"
        ).fetchall()
    }
    if not _PROVIDER_CLAIM_LEDGER_COLUMNS.issubset(columns):
        return False
    foreign_keys = {
        (str(row["from"]), str(row["table"])): str(row["on_delete"]).upper()
        for row in db.execute(
            "PRAGMA foreign_key_list(provider_video_budget_claims)"
        ).fetchall()
    }
    return foreign_keys == {
        ("project_id", "projects"): "CASCADE",
        ("episode_id", "episodes"): "SET NULL",
        ("shot_id", "shots"): "SET NULL",
        ("job_id", "jobs"): "SET NULL",
        ("version_id", "shot_versions"): "SET NULL",
    }


def _legacy_claim_owner(db, row) -> dict[str, str | None]:
    keys = set(row.keys())
    origin_job_id = str(
        row["origin_job_id"]
        if "origin_job_id" in keys
        else row["job_id"]
    )
    origin_version_id = str(
        row["origin_version_id"]
        if "origin_version_id" in keys
        else row["version_id"]
    )
    job = db.execute(
        "SELECT id,project_id,episode_id,shot_id,version_id FROM jobs WHERE id=?",
        (row["job_id"] if row["job_id"] else origin_job_id,),
    ).fetchone()
    version = db.execute(
        "SELECT id,shot_id FROM shot_versions WHERE id=?",
        (row["version_id"] if row["version_id"] else origin_version_id,),
    ).fetchone()
    shot_ids = {
        str(value)
        for value in (
            job["shot_id"] if job else None,
            version["shot_id"] if version else None,
            row["origin_shot_id"] if "origin_shot_id" in keys else None,
        )
        if value
    }
    if len(shot_ids) > 1:
        raise RuntimeError(
            f"provider claim {row['operation_id']} has inconsistent shot ownership"
        )
    origin_shot_id = next(iter(shot_ids), None)
    shot = (
        db.execute(
            "SELECT id,episode_id FROM shots WHERE id=?",
            (origin_shot_id,),
        ).fetchone()
        if origin_shot_id
        else None
    )
    episode_ids = {
        str(value)
        for value in (
            row["episode_id"],
            job["episode_id"] if job else None,
            shot["episode_id"] if shot else None,
            row["origin_episode_id"] if "origin_episode_id" in keys else None,
        )
        if value
    }
    if len(episode_ids) != 1:
        raise RuntimeError(
            f"provider claim {row['operation_id']} has unresolved episode ownership"
        )
    origin_episode_id = next(iter(episode_ids))
    episode = db.execute(
        "SELECT id,project_id FROM episodes WHERE id=?",
        (origin_episode_id,),
    ).fetchone()
    project_ids = {
        str(value)
        for value in (
            episode["project_id"] if episode else None,
            job["project_id"] if job else None,
            row["project_id"] if "project_id" in keys else None,
        )
        if value
    }
    if len(project_ids) != 1:
        raise RuntimeError(
            f"provider claim {row['operation_id']} has unresolved project ownership"
        )
    project_id = next(iter(project_ids))
    if not db.execute(
        "SELECT 1 FROM projects WHERE id=?",
        (project_id,),
    ).fetchone():
        raise RuntimeError(
            f"provider claim {row['operation_id']} project owner is missing"
        )
    if (
        job
        and job["version_id"]
        and str(job["version_id"]) != origin_version_id
    ):
        raise RuntimeError(
            f"provider claim {row['operation_id']} has inconsistent version ownership"
        )
    return {
        "project_id": project_id,
        "episode_id": origin_episode_id if episode else None,
        "shot_id": origin_shot_id if shot else None,
        "job_id": str(job["id"]) if job else None,
        "version_id": str(version["id"]) if version else None,
        "origin_episode_id": origin_episode_id,
        "origin_shot_id": origin_shot_id,
        "origin_job_id": origin_job_id,
        "origin_version_id": origin_version_id,
    }


def _migrate_provider_claim_ledger(db) -> None:
    legacy_rows = db.execute(
        "SELECT * FROM provider_video_budget_claims ORDER BY created_at,operation_id"
    ).fetchall()
    migrated = [
        (row, _legacy_claim_owner(db, row))
        for row in legacy_rows
    ]
    db.execute("DROP TABLE IF EXISTS provider_video_budget_claims_v2")
    _create_provider_claim_ledger_table(db, "provider_video_budget_claims_v2")
    for row, owner in migrated:
        status = str(row["status"])
        accepted_at = (
            float(row["updated_at"])
            if status in {"accepted", "settled"}
            else None
        )
        settled_at = float(row["updated_at"]) if status == "settled" else None
        released_at = float(row["updated_at"]) if status == "released" else None
        liability_closed_at = (
            row["liability_closed_at"]
            if "liability_closed_at" in row.keys()
            else (
                float(row["updated_at"])
                if status == "closed_liability"
                else None
            )
        )
        closure_reason = (
            row["closure_reason"]
            if "closure_reason" in row.keys()
            else None
        )
        liability_source = (
            row["liability_source"]
            if "liability_source" in row.keys()
            else "provider_operation"
        )
        db.execute(
            """INSERT INTO provider_video_budget_claims_v2(
                   operation_id,project_id,episode_id,shot_id,job_id,version_id,
                   origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
                   amount_cny,status,liability_source,created_at,updated_at,
                   accepted_at,settled_at,released_at,
                   liability_closed_at,closure_reason
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["operation_id"],
                owner["project_id"],
                owner["episode_id"],
                owner["shot_id"],
                owner["job_id"],
                owner["version_id"],
                owner["origin_episode_id"],
                owner["origin_shot_id"],
                owner["origin_job_id"],
                owner["origin_version_id"],
                row["amount_cny"],
                status,
                liability_source,
                row["created_at"],
                row["updated_at"],
                accepted_at,
                settled_at,
                released_at,
                liability_closed_at,
                closure_reason,
            ),
        )
    db.execute("DROP INDEX IF EXISTS idx_provider_video_budget_episode")
    db.execute("DROP INDEX IF EXISTS idx_provider_video_budget_project")
    db.execute("DROP INDEX IF EXISTS idx_provider_video_budget_shot")
    db.execute("DROP TABLE provider_video_budget_claims")
    db.execute(
        "ALTER TABLE provider_video_budget_claims_v2 "
        "RENAME TO provider_video_budget_claims"
    )
    migrated_count = int(db.execute(
        "SELECT COUNT(*) FROM provider_video_budget_claims"
    ).fetchone()[0])
    if migrated_count != len(legacy_rows):
        raise RuntimeError("provider claim ledger migration lost rows")


def ensure_video_budget_authority_tables(conn) -> None:
    db = conn
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            """CREATE TABLE IF NOT EXISTS episode_video_budget_authorities (
                   episode_id TEXT PRIMARY KEY,
                   baseline_cny REAL NOT NULL DEFAULT 0,
                   cap_cny REAL NOT NULL,
                   source TEXT NOT NULL,
                   authorized_at REAL NOT NULL,
                   updated_at REAL NOT NULL,
                   FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
               )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS video_budget_authority_ledger (
                   id TEXT PRIMARY KEY,
                   operation_id TEXT NOT NULL UNIQUE,
                   request_fingerprint TEXT NOT NULL,
                   event_type TEXT NOT NULL,
                   grant_id TEXT NOT NULL,
                   episode_id TEXT NOT NULL,
                   project_id TEXT NOT NULL,
                   requested_add_cny REAL NOT NULL,
                   prior_grant_cap_cny REAL,
                   grant_cap_cny REAL NOT NULL,
                   prior_authority_cap_cny REAL,
                   authority_cap_cny REAL NOT NULL,
                   prior_wall_clock_cap_s REAL,
                   wall_clock_cap_s REAL NOT NULL,
                   created_at REAL NOT NULL,
                   FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
                   FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
               )"""
        )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_video_budget_authority_grant
                   ON video_budget_authority_ledger(grant_id,created_at)"""
        )
        claims_exist = bool(db.execute(
            """SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='provider_video_budget_claims'"""
        ).fetchone())
        if not claims_exist:
            _create_provider_claim_ledger_table(
                db,
                "provider_video_budget_claims",
            )
        elif not _provider_claim_ledger_is_current(db):
            _migrate_provider_claim_ledger(db)
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_provider_video_budget_episode
                   ON provider_video_budget_claims(episode_id,status)"""
        )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_provider_video_budget_project
                   ON provider_video_budget_claims(project_id,status)"""
        )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_provider_video_budget_shot
                   ON provider_video_budget_claims(shot_id,status)"""
        )
        if owns_transaction:
            db.commit()
    except Exception:
        if owns_transaction:
            db.rollback()
        raise
def _unowned_historical_video_liabilities(episode_id: str, *, conn) -> list:
    """Return legacy version liabilities not already owned by a durable claim."""
    return conn.execute(
        """SELECT e.project_id,s.id AS shot_id,s.duration_s,
                  v.id AS version_id,v.cost_cny,v.provider_task_id,
                  v.image_inputs,v.created_at
             FROM shot_versions v
             JOIN shots s ON s.id=v.shot_id
             JOIN episodes e ON e.id=s.episode_id
            WHERE s.episode_id=?
              AND (
                  COALESCE(v.cost_cny,0)>0
                  OR (v.provider_task_id IS NOT NULL AND v.provider_task_id!='')
              )
              AND NOT EXISTS (
                  SELECT 1
                    FROM provider_video_budget_claims c
                   WHERE c.origin_version_id=v.id OR c.version_id=v.id
              )
            ORDER BY v.created_at,v.id""",
        (episode_id,),
    ).fetchall()


def _legacy_video_liability_amount(row) -> float:
    from app.compiler import shot_cost_cny

    cost = float(row["cost_cny"] or 0)
    if cost > 0:
        return round(cost, 6)
    try:
        meta = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    attempts = max(1, int(meta.get("provider_paid_attempts") or 0))
    return round(
        shot_cost_cny(int(row["duration_s"] or 0)) * attempts,
        6,
    )


def _historical_video_liability(episode_id: str, *, conn) -> float:
    """Estimate only legacy liability that has no authority or claim owner."""
    rows = _unowned_historical_video_liabilities(episode_id, conn=conn)
    total = sum(_legacy_video_liability_amount(row) for row in rows)
    return round(total, 6)


def migrate_legacy_video_liabilities(conn) -> int:
    """Move unowned legacy version costs into the project claim ledger once."""
    db = conn
    ensure_video_budget_authority_tables(db)
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        episode_ids = [
            str(row["id"])
            for row in db.execute(
                """SELECT e.id
                     FROM episodes e
                     LEFT JOIN episode_video_budget_authorities a
                       ON a.episode_id=e.id
                    WHERE a.episode_id IS NULL
                    ORDER BY e.created_at,e.id"""
            ).fetchall()
        ]
        stamp = now()
        migrated = 0
        for episode_id in episode_ids:
            for row in _unowned_historical_video_liabilities(
                episode_id,
                conn=db,
            ):
                version_id = str(row["version_id"])
                operation_id = f"legacy-video-liability:{version_id}"
                collision = db.execute(
                    """SELECT origin_version_id
                         FROM provider_video_budget_claims
                        WHERE operation_id=?""",
                    (operation_id,),
                ).fetchone()
                if collision is not None:
                    raise RuntimeError(
                        "legacy video liability operation ownership collision: "
                        f"{operation_id}"
                    )
                created_at = (
                    float(row["created_at"])
                    if row["created_at"] is not None
                    else stamp
                )
                db.execute(
                    """INSERT INTO provider_video_budget_claims(
                           operation_id,project_id,episode_id,shot_id,
                           job_id,version_id,origin_episode_id,origin_shot_id,
                           origin_job_id,origin_version_id,amount_cny,status,
                           liability_source,created_at,updated_at,
                           accepted_at,settled_at
                       ) VALUES(?,?,?,?,NULL,?,?,?,?,?,?,'settled',
                                'legacy_version_migration',?,?,?,?)""",
                    (
                        operation_id,
                        row["project_id"],
                        episode_id,
                        row["shot_id"],
                        version_id,
                        episode_id,
                        row["shot_id"],
                        f"legacy-version:{version_id}",
                        version_id,
                        _legacy_video_liability_amount(row),
                        created_at,
                        stamp,
                        created_at,
                        stamp,
                    ),
                )
                migrated += 1
        if owns_transaction:
            db.commit()
        return migrated
    except Exception:
        if owns_transaction:
            db.rollback()
        raise
