"""视频补齐授权（VideoCompletionGrant）。

用户选择「补齐到全片可用」时签发；分镜始终由人工确认，不存在自动确认授权。
token 只存哈希，不存明文。
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db import get_conn, new_id, now
from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
    provider_task_clearance_snapshot,
)

GRANT_TTL_S = 6 * 3600  # 6 小时
VIDEO_PERMISSION = "video.complete_episode"
DEFAULT_VIDEO_BUDGET_CAP_CNY = 150.0
DEFAULT_VIDEO_WALL_CLOCK_CAP_S = 4 * 3600
DEFAULT_FALLBACK_QUOTA_FRACTION = 0.2
# 快速生成（video.generate_episode / video.generate_shot）曾经把 cap 精确设成
# 首轮预估，零余量——任何一镜只要真需要第二次付费尝试就必然打穿上限，只能
# 人工加额。2026-08-25 用 data/manju.db 里 shot_versions 的版本数分布反推：
# 已产出真实版本的 24 个镜头中 66.7% 只需 1 个版本、29.2% 需要 2 个、4.2%
# （EP1 第 1 镜）需要 3 个——单镜最多观测到 2 次重投，EP1 全集口径达到过
# 15 版/8 镜=1.88 倍。按“每镜最多可承受 2 次重投”取整数倍数，对首轮预估
# 整体乘 3，覆盖已观测到的最坏单镜情形，即使全集所有镜头都撞到这个上限也
# 仍有余量。
VIDEO_BUDGET_RETRY_MARGIN_MULTIPLIER = 3.0
# 用户拍板的单集硬上限（2026-08-25，「留余量吧，一集 500 块钱以内」）。现有
# 单价 ¥12/镜、库内最大分集 15 镜（ep_a0e90058f83c）时，3 倍余量后 ¥540 会
# 被这道保险丝截到 ¥500——截断后的有效倍数（¥500/¥180≈2.78）仍高于历史
# 最坏重投比例（EP1 1.88 倍），只是把无限风险换成有限风险，正常分集规模
# 下不会触发。
EPISODE_VIDEO_BUDGET_HARD_CAP_CNY = 500.0

_PROVIDER_CLAIM_LEDGER_COLUMNS = {
    "operation_id",
    "project_id",
    "episode_id",
    "shot_id",
    "job_id",
    "version_id",
    "origin_episode_id",
    "origin_shot_id",
    "origin_job_id",
    "origin_version_id",
    "amount_cny",
    "status",
    "liability_source",
    "created_at",
    "updated_at",
    "accepted_at",
    "settled_at",
    "released_at",
    "liability_closed_at",
    "closure_reason",
}


class VideoCompletionGrant(BaseModel):
    grant_id: str
    episode_id: str
    project_id: str
    storyboard_artifact_id: str
    release_qualification_hash: str = ""
    release_qualification: dict[str, Any] = Field(default_factory=dict)
    episode_video_plan_id: str | None = None
    episode_video_plan_revision: int | None = None
    video_plan_release_hash: str | None = None
    capability_snapshot_id: str | None = None
    permission: Literal["video.complete_episode"] = VIDEO_PERMISSION
    kind: Literal["video"] = "video"
    budget_cap_cny: float = DEFAULT_VIDEO_BUDGET_CAP_CNY
    wall_clock_cap_s: float = DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    deadline_at: float
    allow_fallback_adopt: bool = True
    max_fallback_shots: int = 0
    allow_storyboard_edit: bool = False
    issued_by: str = "user"
    issued_at: float
    expires_at: float
    consumed_at: float | None = None
    revoked_at: float | None = None


class VideoBudgetAuthorizationError(RuntimeError):
    """A payable provider video call would exceed the user-approved cap."""


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


def ensure_video_budget_authority_tables(conn=None) -> None:
    db = conn or get_conn()
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


async def reconcile_provider_tasks_for_clear(
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    shot_ids: list[str] | tuple[str, ...] = (),
    version_ids: list[str] | tuple[str, ...] = (),
    conn=None,
    terminal_observations: dict[str, dict[str, Any]] | None = None,
    evidence_source: str | None = None,
) -> dict[str, Any]:
    """Settle provider tasks that are already remotely terminal before a clear.

    This path only polls durable task handles. It never downloads media, adopts
    a result, or submits a new provider task. It only ever moves a claim to
    ``settled``/``closed_liability`` once the provider itself has confirmed a
    terminal outcome (``succeeded``/``failed``) — an unresolved or still-running
    provider task is left untouched and remains a real blocker.

    Scope works exactly like :func:`provider_task_clearance_snapshot`: pass
    ``project_id`` for whole-project cleanup or ``episode_id``/``shot_ids``/
    ``version_ids`` for a narrower, user-triggered reconciliation (e.g. the
    storyboard board's "清空视频提示词" recovery action).
    """
    from app import hiagent
    from app.hiagent import ProviderError

    db = conn or get_conn()
    initial = provider_task_clearance_snapshot(
        project_id=project_id,
        episode_id=episode_id,
        shot_ids=shot_ids,
        version_ids=version_ids,
        conn=db,
    )
    reconciled: list[str] = []
    seen: set[tuple[str, str]] = set()
    for blocker in initial["blockers"]:
        job_id = str(blocker.get("job_id") or "")
        operation_id = str(blocker.get("provider_operation_id") or "")
        if not job_id or not operation_id or (job_id, operation_id) in seen:
            continue
        seen.add((job_id, operation_id))
        task_id = str(blocker.get("provider_task_id") or "").strip()
        if not task_id:
            calls = db.execute(
                """SELECT response_json FROM provider_calls
                    WHERE kind='video_create' AND status='OK' AND operation_id=?
                      AND response_json IS NOT NULL
                    ORDER BY id DESC""",
                (operation_id,),
            ).fetchall()
            for call in calls:
                try:
                    payload = json.loads(call["response_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    task_id = str(payload.get("id") or "").strip()
                if task_id:
                    break
        if not task_id:
            continue
        result = (terminal_observations or {}).get(task_id)
        if result is None:
            try:
                result = await hiagent.poll_video_task(
                    task_id,
                    call_meta={
                        "project_id": project_id,
                        "episode_id": episode_id,
                        "job_id": job_id,
                        "operation_id": operation_id,
                        "purpose": evidence_source or "provider_tasks_clear_reconcile",
                    },
                )
            except ProviderError:
                continue
        status = str((result or {}).get("status") or "").strip().lower()
        if status not in {"succeeded", "failed"}:
            continue
        stamp = now()
        amount_cny = max(0.0, float(blocker.get("amount_cny") or 0))
        # 通用措辞：这条路径既服务整项目删除也服务分集清空（storyboard/videos
        # clear 撞上 PROVIDER_TASKS_NOT_TERMINAL 后的恢复入口），不能预设是
        # 哪一种触发场景；具体触发点交给下面的 evidence_source 后缀记录。
        terminal_message = (
            "已核对供应商任务成功终态；费用已结算，结果保持隔离且不可采用"
            if status == "succeeded"
            else "已核对供应商任务失败终态；费用责任已结算"
        )
        if evidence_source:
            terminal_message += f"；核对证据={evidence_source}"
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """UPDATE provider_video_budget_claims
                      SET status='settled',updated_at=?,settled_at=?
                    WHERE operation_id=? AND job_id=?
                      AND status NOT IN ('released','settled','closed_liability')""",
                (stamp, stamp, operation_id, job_id),
            )
            db.execute(
                """UPDATE jobs
                      SET status=?,error=?,provider_create_state='accepted',
                          provider_non_cancellable=1,provider_poll_required=0,
                          provider_result_adoptable=0,video_slot_active=0,
                          cancellation_requested=0,abandoned=0,reserved_cost_cny=0,
                          lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,
                          updated_at=?
                    WHERE id=? AND provider_operation_id=?""",
                (
                    "succeeded" if status == "succeeded" else "failed",
                    terminal_message,
                    stamp,
                    job_id,
                    operation_id,
                ),
            )
            version_id = str(blocker.get("version_id") or "")
            if version_id:
                db.execute(
                    """UPDATE shot_versions
                          SET provider_task_id=?,status=?,error=?,cost_cny=?,
                              video_slot_active=0
                        WHERE id=?""",
                    (
                        task_id,
                        "quarantined" if status == "succeeded" else "failed",
                        terminal_message,
                        amount_cny,
                        version_id,
                    ),
                )
            db.execute(
                """UPDATE budget_reservations
                      SET status='settled',settled_at=?,actual_cost_cny=?
                    WHERE job_id=? AND status IN ('reserved','running')""",
                (stamp, amount_cny, job_id),
            )
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        reconciled.append(job_id)
    return {
        "reconciled_job_ids": reconciled,
        "clearance": provider_task_clearance_snapshot(
            project_id=project_id,
            episode_id=episode_id,
            shot_ids=shot_ids,
            version_ids=version_ids,
            conn=db,
        ),
    }


async def reconcile_project_provider_tasks_for_clear(
    project_id: str,
    *,
    conn=None,
    terminal_observations: dict[str, dict[str, Any]] | None = None,
    evidence_source: str | None = None,
) -> dict[str, Any]:
    """Back-compat wrapper: project-wide reconciliation before project deletion.

    Kept as a thin alias so ``app/domain/projects.py`` does not need to change;
    see :func:`reconcile_provider_tasks_for_clear` for the general (project or
    episode/shot/version scoped) implementation.
    """
    return await reconcile_provider_tasks_for_clear(
        project_id=project_id,
        conn=conn,
        terminal_observations=terminal_observations,
        evidence_source=evidence_source,
    )


def close_superseded_unclaimed_video_jobs(
    episode_id: str, *, conn=None,
) -> list[str]:
    """Close video jobs that never reached the provider and are now moot.

    A job qualifies only when *every* one of these holds — each condition is
    local, already-durable evidence, not a guess:

    - it never became payable (``provider_create_state='not_started'``,
      ``provider_non_cancellable=0``, no ``provider_task_id`` on its version,
      and no ``provider_calls`` row proves a create request ever went out);
    - it carries no live budget claim (no ``provider_video_budget_claims`` row
      other than ``released``/absent);
    - its shot has since adopted a *different* version that already succeeded.

    Under those conditions the job cannot represent any billing liability —
    there is nothing to reconcile with the provider, because the provider was
    never asked. Closing it mirrors the existing stale-supersession pattern in
    ``app/video_plan.py::reconcile_adopted_revision`` (jobs.status='stale',
    cancellation_requested=1, abandoned=1), just generalized from
    ``status='paused'`` to any non-terminal status so a ``paused_budget``
    orphan left behind by budget contention does not sit stuck forever once
    its shot has moved on. This never touches a job that still has any
    provider footprint or any non-released claim.
    """
    db = conn or get_conn()
    ensure_video_budget_authority_tables(db)
    rows = db.execute(
        """SELECT j.id AS job_id, j.version_id
             FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
             JOIN shots s ON s.id=j.shot_id
             JOIN shot_versions av ON av.id=s.adopted_version_id
            WHERE j.episode_id=? AND j.kind='video'
              AND j.status NOT IN ('succeeded','failed','cancelled','abandoned')
              AND j.cancellation_requested=0 AND j.abandoned=0
              AND j.provider_non_cancellable=0
              AND COALESCE(j.provider_create_state,'') IN ('','not_started')
              AND (v.provider_task_id IS NULL OR v.provider_task_id='')
              AND NOT EXISTS(
                  SELECT 1 FROM provider_calls pc
                   WHERE pc.kind='video_create' AND pc.status='OK'
                     AND pc.operation_id=j.provider_operation_id
              )
              AND NOT EXISTS(
                  SELECT 1 FROM provider_video_budget_claims c
                   WHERE c.job_id=j.id AND c.status!='released'
              )
              AND s.adopted_version_id IS NOT NULL
              AND s.adopted_version_id!=j.version_id
              AND av.status='succeeded'""",
        (episode_id,),
    ).fetchall()
    job_ids = [str(row["job_id"]) for row in rows]
    if not job_ids:
        return []
    stamp = now()
    message = "所属镜头已采用其他成功版本，本任务从未提交给供应商，已作为过时任务收口"
    placeholders = ",".join("?" for _ in job_ids)
    db.execute(
        f"""UPDATE jobs
               SET status='stale',cancellation_requested=1,abandoned=1,
                   error=?,reserved_cost_cny=0,lease_owner=NULL,
                   lease_expires_at=NULL,next_retry_at=NULL,updated_at=?
             WHERE id IN ({placeholders})""",
        (message, stamp, *job_ids),
    )
    db.execute(
        """UPDATE budget_reservations
              SET status='released',settled_at=?,actual_cost_cny=0
            WHERE job_id IN ({}) AND status IN ('reserved','running')""".format(
            placeholders
        ),
        (stamp, *job_ids),
    )
    version_ids = [str(row["version_id"]) for row in rows if row["version_id"]]
    if version_ids:
        vplaceholders = ",".join("?" for _ in version_ids)
        db.execute(
            f"""UPDATE shot_versions SET status='stale',error=?
                 WHERE id IN ({vplaceholders})""",
            (message, *version_ids),
        )
    db.commit()
    return job_ids


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


def migrate_legacy_video_liabilities(conn=None) -> int:
    """Move unowned legacy version costs into the project claim ledger once."""
    db = conn or get_conn()
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


def _episode_video_budget_floor(episode_id: str, *, conn) -> tuple[float, float]:
    """已承诺的下限：返回 (baseline_cny, floor_cny)。floor 是既有 cap 与
    （baseline+在途认领）取大者；没有 authority 行时回退到历史遗留
    liability。cap 永远不能低于 floor，否则会撕毁已经产生的付款责任。"""
    current = conn.execute(
        "SELECT baseline_cny,cap_cny FROM episode_video_budget_authorities WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if current:
        baseline = float(current["baseline_cny"] or 0)
        claimed = float(conn.execute(
            """SELECT COALESCE(SUM(amount_cny),0) AS amount
                 FROM provider_video_budget_claims
                WHERE episode_id=? AND status!='released'""",
            (episode_id,),
        ).fetchone()["amount"] or 0)
        return baseline, max(float(current["cap_cny"] or 0), baseline + claimed)
    baseline = _historical_video_liability(episode_id, conn=conn)
    return baseline, baseline


def _apply_video_budget_retry_margin(
    floor_cny: float,
    amount_cny: float,
    *,
    multiplier: float = VIDEO_BUDGET_RETRY_MARGIN_MULTIPLIER,
    hard_cap_cny: float = EPISODE_VIDEO_BUDGET_HARD_CAP_CNY,
) -> float:
    """把新批准的首轮金额按重试余量倍数计入 cap，再夹到单集硬上限——但
    永远不低于已经承诺的下限，硬上限只截「新增余量」，不撕毁既有责任。"""
    raw = floor_cny + max(0.0, float(amount_cny)) * multiplier
    return max(floor_cny, min(raw, hard_cap_cny))


def preview_episode_video_budget_authorization_cap(
    episode_id: str, amount_cny: float, *, conn=None,
) -> float:
    """只读预览：`authorize_episode_video_budget_increment` 对同样的
    episode_id/amount 会产出的 cap，供审批卡在真正下单前展示「授权上限」。
    必须与真实授权函数用同一套推导逻辑，不能各算各的、界面和执行对不上。
    """
    db = conn or get_conn()
    ensure_video_budget_authority_tables(db)
    _, floor = _episode_video_budget_floor(episode_id, conn=db)
    return round(_apply_video_budget_retry_margin(floor, amount_cny), 6)


def authorize_episode_video_budget_increment(
    episode_id: str,
    increment_cny: float,
    *,
    source: str,
    operation_id: str | None = None,
    request_fingerprint: str | None = None,
    conn=None,
) -> float:
    """Add one explicitly approved payable-video amount to the episode cap."""
    amount = float(increment_cny)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("视频授权额度必须是非负有限数")
    db = conn or get_conn()
    ensure_video_budget_authority_tables(db)
    db.execute(
        """CREATE TABLE IF NOT EXISTS video_budget_authorization_receipts(
               operation_id TEXT PRIMARY KEY,
               episode_id TEXT NOT NULL,
               request_fingerprint TEXT NOT NULL,
               increment_cny REAL NOT NULL,
               cap_after_cny REAL NOT NULL,
               source TEXT NOT NULL,
               created_at REAL NOT NULL
           )"""
    )
    if bool(operation_id) != bool(request_fingerprint):
        raise ValueError("预算授权 receipt 必须同时提供 operation_id 与 request_fingerprint")
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        if operation_id:
            receipt = db.execute(
                """SELECT episode_id,request_fingerprint,increment_cny,cap_after_cny
                     FROM video_budget_authorization_receipts WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()
            if receipt:
                if (
                    str(receipt["episode_id"]) != episode_id
                    or str(receipt["request_fingerprint"]) != request_fingerprint
                    or abs(float(receipt["increment_cny"]) - amount) > 1e-9
                ):
                    raise ValueError("预算授权 idempotency_key 已绑定不同请求")
                if owns_transaction:
                    db.commit()
                return round(float(receipt["cap_after_cny"]), 6)
        stamp = now()
        baseline, floor = _episode_video_budget_floor(episode_id, conn=db)
        cap = _apply_video_budget_retry_margin(floor, amount)
        db.execute(
            """INSERT INTO episode_video_budget_authorities(
                   episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(episode_id) DO UPDATE SET
                   cap_cny=excluded.cap_cny,source=excluded.source,
                   authorized_at=excluded.authorized_at,updated_at=excluded.updated_at""",
            (episode_id, baseline, cap, source, stamp, stamp),
        )
        if operation_id:
            db.execute(
                """INSERT INTO video_budget_authorization_receipts(
                       operation_id,episode_id,request_fingerprint,increment_cny,
                       cap_after_cny,source,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    operation_id,
                    episode_id,
                    request_fingerprint,
                    amount,
                    cap,
                    source,
                    stamp,
                ),
            )
        if owns_transaction:
            db.commit()
        return round(cap, 6)
    except Exception:
        if owns_transaction:
            db.rollback()
        raise


def authorize_episode_video_budget_absolute(
    episode_id: str,
    cap_cny: float,
    *,
    source: str,
    conn=None,
) -> float:
    """Persist an absolute completion-run cap without forgetting sunk liability."""
    requested = float(cap_cny)
    if not math.isfinite(requested) or requested < 0:
        raise ValueError("视频授权上限必须是非负有限数")
    db = conn or get_conn()
    ensure_video_budget_authority_tables(db)
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        current = db.execute(
            "SELECT baseline_cny,cap_cny FROM episode_video_budget_authorities WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        baseline = (
            float(current["baseline_cny"] or 0)
            if current else _historical_video_liability(episode_id, conn=db)
        )
        cap = max(
            requested,
            float(current["cap_cny"] or 0) if current else 0.0,
        )
        stamp = now()
        db.execute(
            """INSERT INTO episode_video_budget_authorities(
                   episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(episode_id) DO UPDATE SET
                   cap_cny=excluded.cap_cny,source=excluded.source,
                   authorized_at=excluded.authorized_at,updated_at=excluded.updated_at""",
            (episode_id, baseline, cap, source, stamp, stamp),
        )
        if owns_transaction:
            db.commit()
        return round(cap, 6)
    except Exception:
        if owns_transaction:
            db.rollback()
        raise


def episode_video_budget_snapshot(episode_id: str, *, conn=None) -> dict[str, float] | None:
    db = conn or get_conn()
    ensure_video_budget_authority_tables(db)
    row = db.execute(
        "SELECT baseline_cny,cap_cny FROM episode_video_budget_authorities WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if row is None:
        return None
    claimed = float(db.execute(
        """SELECT COALESCE(SUM(amount_cny),0) AS amount
             FROM provider_video_budget_claims
            WHERE episode_id=? AND status!='released'""",
        (episode_id,),
    ).fetchone()["amount"] or 0)
    baseline = float(row["baseline_cny"] or 0)
    cap = float(row["cap_cny"] or 0)
    return {
        "baseline_cny": baseline,
        "claimed_cny": claimed,
        "used_cny": round(baseline + claimed, 6),
        "cap_cny": cap,
        "remaining_cny": round(max(0.0, cap - baseline - claimed), 6),
    }


def project_video_budget_snapshot(project_id: str, *, conn=None) -> dict[str, float]:
    """Aggregate durable provider liability across every episode in a project.

    Claim release is the accounting boundary. Job and version outcomes only
    describe execution; they cannot return capacity after a provider call may
    have incurred a charge. Episodes without an authority row use the legacy
    liability estimator until their first grant freezes that amount as baseline.
    """
    db = conn or get_conn()
    ensure_video_budget_authority_tables(db)
    episodes = db.execute(
        """SELECT e.id,a.baseline_cny
             FROM episodes e
             LEFT JOIN episode_video_budget_authorities a ON a.episode_id=e.id
            WHERE e.project_id=?""",
        (project_id,),
    ).fetchall()
    baseline = 0.0
    legacy = 0.0
    for row in episodes:
        if row["baseline_cny"] is None:
            legacy += _historical_video_liability(str(row["id"]), conn=db)
        else:
            baseline += float(row["baseline_cny"] or 0)
    claimed = float(db.execute(
        """SELECT COALESCE(SUM(c.amount_cny),0) AS amount
             FROM provider_video_budget_claims c
            WHERE c.project_id=? AND c.status!='released'""",
        (project_id,),
    ).fetchone()["amount"] or 0)
    used = baseline + legacy + claimed
    return {
        "baseline_cny": round(baseline, 6),
        "legacy_cny": round(legacy, 6),
        "claimed_cny": round(claimed, 6),
        "used_cny": round(used, 6),
    }


def episode_video_completion_budget_requirement(
    episode_id: str,
    *,
    conn=None,
) -> dict[str, float | int]:
    """Return the absolute provider cap needed for one claim per current shot."""
    from app.video_cost_model import initial_shot_generation_cost

    db = conn or get_conn()
    ensure_video_budget_authority_tables(db)
    release_row = db.execute(
        "SELECT published_storyboard_artifact_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    cost_basis = None
    if release_row and release_row["published_storyboard_artifact_id"]:
        from app.video_plan import authoritative_storyboard_plan_cost

        cost_basis = authoritative_storyboard_plan_cost(
            episode_id,
            conn=db,
        )
    snapshot = episode_video_budget_snapshot(episode_id, conn=db)
    used = float((snapshot or {}).get("used_cny") or 0)
    claimed_shot_ids = {
        str(row["shot_id"])
        for row in db.execute(
            """SELECT DISTINCT v.shot_id
                 FROM provider_video_budget_claims c
                 JOIN shot_versions v ON v.id=c.version_id
                 JOIN shots s ON s.id=v.shot_id
                WHERE c.episode_id=? AND c.status!='released'
                  AND s.episode_id=?""",
            (episode_id, episode_id),
        ).fetchall()
    }
    remaining = 0.0
    projected_first_pass = 0.0
    total_shots = 0
    for row in db.execute(
        "SELECT id,duration_s FROM shots WHERE episode_id=?",
        (episode_id,),
    ).fetchall():
        total_shots += 1
        shot_cost = initial_shot_generation_cost(
            float(row["duration_s"] or 0)
        )
        projected_first_pass += shot_cost
        if str(row["id"]) in claimed_shot_ids:
            continue
        remaining += shot_cost
    if cost_basis is not None and (
        total_shots != int(cost_basis["shot_count"])
        or abs(
            projected_first_pass
            - float(cost_basis["estimated_cost_cny"])
        ) > 1e-9
    ):
        raise ValueError(
            "视频成本投影与当前 outline/release authority 不一致"
        )
    return {
        "used_cny": round(used, 6),
        "claimed_current_shots": len(claimed_shot_ids),
        "shots_total": total_shots,
        "unclaimed_first_pass_cny": round(remaining, 6),
        "required_completion_cap_cny": round(used + remaining, 6),
    }


def reserve_provider_video_budget(
    *,
    episode_id: str,
    job_id: str,
    version_id: str,
    operation_id: str,
    amount_cny: float,
    conn=None,
) -> bool:
    """Atomically claim one provider create cost.

    A payable create without episode authority is rejected. When a caller
    supplies an active transaction, the claim participates in that transaction.
    """
    amount = max(0.0, float(amount_cny))
    db = conn or get_conn()
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
    conn=None,
) -> bool:
    """Close recovery for an accepted operation without releasing its budget.

    This terminal is used only after an explicit decision to abandon recovery
    and create a new provider operation. The conservative claim amount remains
    project-used because the old provider charge cannot be disproved.
    """
    closure_reason = str(reason or "").strip()
    if not closure_reason:
        raise ValueError("provider claim liability closure requires a reason")
    db = conn or get_conn()
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


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_completion_grants_table(conn=None) -> None:
    db = conn or get_conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS completion_grants (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            screenplay_artifact_id TEXT NOT NULL,
            bible_artifact_id TEXT,
            permission TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            issued_by TEXT NOT NULL,
            issued_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL,
            revoked_at REAL,
            impact_snapshot_json TEXT
        )"""
    )
    for stmt in (
        "ALTER TABLE completion_grants ADD COLUMN kind TEXT NOT NULL DEFAULT 'video'",
        "ALTER TABLE completion_grants ADD COLUMN storyboard_artifact_id TEXT",
        "ALTER TABLE completion_grants ADD COLUMN budget_cap_cny REAL",
        "ALTER TABLE completion_grants ADD COLUMN wall_clock_cap_s REAL",
        "ALTER TABLE completion_grants ADD COLUMN deadline_at REAL",
        "ALTER TABLE completion_grants ADD COLUMN allow_fallback_adopt INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE completion_grants ADD COLUMN max_fallback_shots INTEGER",
        "ALTER TABLE completion_grants ADD COLUMN allow_storyboard_edit INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE completion_grants ADD COLUMN release_qualification_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE completion_grants ADD COLUMN release_qualification_hash TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE completion_grants ADD COLUMN episode_video_plan_id TEXT",
        "ALTER TABLE completion_grants ADD COLUMN episode_video_plan_revision INTEGER",
        "ALTER TABLE completion_grants ADD COLUMN video_plan_release_hash TEXT",
        "ALTER TABLE completion_grants ADD COLUMN capability_snapshot_id TEXT",
    ):
        try:
            db.execute(stmt)
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    db.execute(
        "DELETE FROM completion_grants WHERE kind='storyboard' OR permission='storyboard.generate_and_confirm'"
    )
    db.commit()


def default_max_fallback_shots(shots_total: int) -> int:
    return max(1, int(math.ceil(max(0, shots_total) * DEFAULT_FALLBACK_QUOTA_FRACTION)))


RELEASE_QUALIFICATION_VERSION = "video-completion-release-qualification.v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _content_fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _legacy_screenplay_projection_material(
    episode: Any,
    *,
    mode: str,
    projection_hash: str,
) -> dict[str, Any]:
    """Content-address every legacy fact without pretending it is certified."""
    from app.evidence import repository as evidence_repository

    keys = set(episode.keys())
    artifact_id = str(
        (episode["published_screenplay_artifact_id"] if "published_screenplay_artifact_id" in keys else "")
        or (episode["screenplay_artifact_id"] if "screenplay_artifact_id" in keys else "")
        or ""
    )
    binding: dict[str, Any] = {
        "artifact_id": artifact_id,
        "compatibility": "legacy_projection_only",
    }
    artifact = evidence_repository.get_artifact(artifact_id) if artifact_id else None
    if artifact is not None:
        try:
            current_hash = evidence_repository.content_hash(
                artifact.get("content"), artifact.get("file_path")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("legacy screenplay artifact cannot be content-addressed") from exc
        if not artifact.get("content_hash") or artifact.get("content_hash") != current_hash:
            raise ValueError("legacy screenplay artifact content hash drifted")
        binding.update({
            "content_hash": current_hash,
            "type": artifact.get("type"),
            "scope_type": artifact.get("scope_type"),
            "scope_id": artifact.get("scope_id"),
            "status": artifact.get("status"),
            "contract_version": artifact.get("contract_version"),
        })
    return {
        "mode": mode,
        "immutable_authority_required": False,
        "narrative_authority_required": False,
        "projection_hash": projection_hash,
        "artifact": binding,
    }


def _screenplay_release_material(episode_id: str, *, conn) -> dict[str, Any]:
    """Resolve screenplay authority without allowing a mutable downgrade.

    Historical projection-only episodes remain readable through an explicit
    compatibility contract.  The first durable production revision,
    certificate, or narrative review makes the immutable resolver mandatory.
    """
    from app.production.screenplay_authority import (
        episode_requires_immutable_screenplay_authority,
        resolve_current_screenplay_authority,
        resolve_downstream_screenplay,
    )

    episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    immutable_required = episode_requires_immutable_screenplay_authority(
        episode,
        conn=conn,
    )
    try:
        context = resolve_downstream_screenplay(episode_id, conn=conn)
    except ValueError:
        if immutable_required:
            raise
        raw = ""
        try:
            raw = str(episode["screenplay_json"] or "")
        except (KeyError, IndexError):
            pass
        return _legacy_screenplay_projection_material(
            episode,
            mode="legacy_plan_null_projection_absent" if not raw else "legacy_plan_null",
            projection_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
    if not context.immutable_authority_required:
        return _legacy_screenplay_projection_material(
            episode,
            mode="legacy_plan_null",
            projection_hash=_content_fingerprint(
                context.screenplay.model_dump(mode="json")
            ),
        )
    resolved = resolve_current_screenplay_authority(
        episode_id,
        conn=conn,
        require_narrative=context.narrative_authority_required,
    )
    revision_id = ""
    try:
        revision_id = str(episode["screenplay_production_revision_id"] or "")
    except (KeyError, IndexError):
        pass
    return {
        "mode": "immutable_narrative" if context.narrative_authority_required else "immutable",
        "immutable_authority_required": True,
        "narrative_authority_required": context.narrative_authority_required,
        "published_screenplay_artifact_id": resolved.artifact_id,
        "published_screenplay_artifact_hash": resolved.artifact_hash,
        "screenplay_completion_certificate_id": resolved.certificate_id,
        "screenplay_production_revision_id": revision_id,
        "screenplay_input_fingerprint": resolved.input_fingerprint,
    }


def _storyboard_release_material(
    episode_id: str,
    *,
    conn,
    legacy_plan_null: bool,
) -> dict[str, Any]:
    from app.evidence import repository as evidence_repository
    from app.video_plan import (
        canonical_shot_contract_fingerprint,
        current_storyboard_release_manifest,
    )

    try:
        manifest = current_storyboard_release_manifest(episode_id, conn=conn)
    except (TypeError, ValueError):
        if not legacy_plan_null:
            raise
        # Explicit historical compatibility: some pre-contract rows contain
        # nullable camera/scene fields and cannot instantiate today's Shot
        # model. Bind every persisted column instead of weakening validation
        # for modern releases.
        episode = conn.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        legacy_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no,id",
            (episode_id,),
        ).fetchall()
        artifact_id = str(
            (
                episode["published_storyboard_artifact_id"]
                if "published_storyboard_artifact_id" in episode.keys()
                else None
            )
            or episode["storyboard_artifact_id"]
            or ""
        )
        if not artifact_id:
            raise ValueError("legacy storyboard projection has no release pointer")
        raw_projection_hash = _content_fingerprint(
            [dict(row) for row in legacy_rows]
        )
        manifest = {
            "published_storyboard_artifact_id": artifact_id,
            "published_storyboard_artifact_hash": raw_projection_hash,
            "completion_certificate_id": str(
                episode["storyboard_completion_certificate_id"] or ""
            ) if "storyboard_completion_certificate_id" in episode.keys() else "",
            "narrative_review_artifact_id": str(
                episode["narrative_review_artifact_id"] or ""
            ) if "narrative_review_artifact_id" in episode.keys() else "",
        }
        manifest["release_qualification_hash"] = _content_fingerprint({
            "manifest_version": "storyboard-release-manifest.legacy-plan-null.v1",
            "episode_id": episode_id,
            **manifest,
        })
    artifact_id = manifest["published_storyboard_artifact_id"]
    artifact = evidence_repository.get_artifact(artifact_id)
    if artifact is None:
        artifact_binding: dict[str, Any] = {
            "compatibility": "legacy_projection_pointer",
            "artifact_id": artifact_id,
            "content_hash": manifest["published_storyboard_artifact_hash"],
        }
    else:
        try:
            current_hash = evidence_repository.content_hash(
                artifact.get("content"), artifact.get("file_path")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("current storyboard artifact cannot be content-addressed") from exc
        if (
            not artifact.get("content_hash")
            or artifact.get("content_hash") != current_hash
            or current_hash != manifest["published_storyboard_artifact_hash"]
        ):
            raise ValueError("current storyboard artifact content hash drifted")
        artifact_binding = {
            "artifact_id": artifact_id,
            "content_hash": current_hash,
            "type": artifact.get("type"),
            "scope_type": artifact.get("scope_type"),
            "scope_id": artifact.get("scope_id"),
            "status": artifact.get("status"),
            "contract_version": artifact.get("contract_version"),
        }
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no, id",
        (episode_id,),
    ).fetchall()
    shot_projection: list[dict[str, Any]] = []
    for row in rows:
        try:
            contract_hash = canonical_shot_contract_fingerprint(row)
            projection_mode = "canonical_shot_contract"
        except (TypeError, ValueError):
            if not legacy_plan_null:
                raise
            contract_hash = _content_fingerprint(dict(row))
            projection_mode = "legacy_complete_database_row"
        shot_projection.append({
            "database_shot_id": str(row["id"]),
            "shot_uid": str(row["shot_uid"] or "") if "shot_uid" in row.keys() else "",
            "shot_no": int(row["shot_no"]),
            "projection_mode": projection_mode,
            "canonical_contract_hash": contract_hash,
        })
    return {
        **manifest,
        "artifact": artifact_binding,
        "shots_authority_projection_hash": _content_fingerprint(shot_projection),
        "shots_authority_projection": shot_projection,
    }


def _narrative_review_material(
    episode_id: str,
    *,
    screenplay_material: dict[str, Any],
) -> dict[str, Any]:
    """Keep the legacy qualification slot explicitly score-only.

    Storyboard release authority is already verified by
    ``_storyboard_release_material``. Optional audience scoring is not an
    authored input and must not invalidate a paid-work grant when it changes.
    """
    return {
        "required": False,
        "verified": True,
        "evaluation_role": "score_only",
        "episode_id": episode_id,
        "narrative_project": bool(
            screenplay_material.get("narrative_authority_required")
        ),
    }


def _generation_plan_material(
    episode_id: str,
    *,
    conn,
    applicable: bool | None,
) -> dict[str, Any]:
    from app.video_plan import (
        capability_snapshot_by_id,
        load_latest_plan,
        shot_video_execution_contract_fingerprint,
        video_plan_provider_selection_is_current,
        verify_episode_plan_is_current,
    )

    if applicable is False:
        return {"applicable": False, "compatibility": "plan_pending_at_grant_issue"}
    plan = load_latest_plan(episode_id, conn=conn)
    if plan is None:
        if applicable:
            raise ValueError("current episode video generation plan is missing")
        return {"applicable": False, "compatibility": "plan_pending_at_grant_issue"}
    if plan.status != "valid" or not verify_episode_plan_is_current(
        plan,
        conn=conn,
        mark_stale=False,
    ) or not video_plan_provider_selection_is_current(plan, conn=conn):
        if applicable is None:
            return {
                "applicable": False,
                "compatibility": "plan_pending_at_grant_issue",
            }
        raise ValueError("current episode video generation plan is not valid")
    snapshot = capability_snapshot_by_id(plan.capability_snapshot_id, conn=conn)
    if snapshot is None:
        raise ValueError("video generation plan capability snapshot is missing")
    return {
        "applicable": True,
        "episode_video_plan_id": plan.episode_video_plan_id,
        "plan_revision": int(plan.plan_revision),
        "source_storyboard_revision_id": plan.source_storyboard_revision_id,
        "release_qualification_hash": plan.release_qualification_hash,
        "capability_snapshot_id": plan.capability_snapshot_id,
        "capability_snapshot_hash": _content_fingerprint(
            snapshot.model_dump(mode="json")
        ),
        "planner_provider": plan.planner_provider,
        "planner_model": plan.planner_model,
        "planner_prompt_fingerprint": plan.planner_prompt_fingerprint,
        "authoritative_shot_count": len(plan.shots),
        "authoritative_estimated_cost_cny": round(
            sum(float(shot.estimated_cost or 0) for shot in plan.shots),
            6,
        ),
        "shot_execution_contracts": [
            {
                "shot_id": shot.shot_id,
                "shot_plan_id": shot.shot_plan_id,
                "contract_hash": shot_video_execution_contract_fingerprint(shot),
            }
            for shot in plan.shots
        ],
    }


def current_video_completion_qualification(
    episode_id: str,
    *,
    generation_plan_applicable: bool | None = None,
    conn=None,
) -> tuple[dict[str, Any], str]:
    """Build the complete release contract rechecked before every paid stage."""
    db = conn or get_conn()
    episode = db.execute(
        "SELECT id,project_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    screenplay = _screenplay_release_material(episode_id, conn=db)
    storyboard = _storyboard_release_material(
        episode_id,
        conn=db,
        legacy_plan_null=str(screenplay.get("mode") or "").startswith("legacy_plan_null"),
    )
    material = {
        "qualification_version": RELEASE_QUALIFICATION_VERSION,
        "episode_id": episode_id,
        "project_id": str(episode["project_id"]),
        "screenplay_authority": screenplay,
        "storyboard_authority": storyboard,
        "narrative_review_authority": _narrative_review_material(
            episode_id,
            screenplay_material=screenplay,
        ),
        "generation_plan": _generation_plan_material(
            episode_id,
            conn=db,
            applicable=generation_plan_applicable,
        ),
    }
    return material, _content_fingerprint(material)


def _video_budget_authority_operation_id(
    *,
    event_type: str,
    scope_id: str,
    idempotency_key: str | None,
    fallback_id: str,
) -> str:
    normalized = str(idempotency_key or "").strip()
    if not normalized:
        return fallback_id
    material = _canonical_json({
        "event_type": event_type,
        "scope_id": scope_id,
        "idempotency_key": normalized,
    })
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"video-budget-authority:{digest}"


def _idempotent_video_grant(
    conn,
    *,
    operation_id: str,
    request_fingerprint: str,
) -> VideoCompletionGrant | None:
    event = conn.execute(
        """SELECT grant_id,request_fingerprint
             FROM video_budget_authority_ledger
            WHERE operation_id=?""",
        (operation_id,),
    ).fetchone()
    if event is None:
        return None
    if str(event["request_fingerprint"]) != request_fingerprint:
        raise GrantValidationError(
            "GRANT_IDEMPOTENCY_CONFLICT",
            "同一幂等键已用于不同的视频预算授权请求",
        )
    row = conn.execute(
        "SELECT * FROM completion_grants WHERE id=?",
        (event["grant_id"],),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "视频预算授权审计事件引用的 completion grant 不存在"
        )
    return _row_to_video_grant(row)


def _record_video_budget_authority_event(
    conn,
    *,
    operation_id: str,
    request_fingerprint: str,
    event_type: str,
    grant_id: str,
    episode_id: str,
    project_id: str,
    requested_add_cny: float,
    prior_grant_cap_cny: float | None,
    grant_cap_cny: float,
    prior_authority_cap_cny: float | None,
    authority_cap_cny: float,
    prior_wall_clock_cap_s: float | None,
    wall_clock_cap_s: float,
    created_at: float,
) -> None:
    conn.execute(
        """INSERT INTO video_budget_authority_ledger(
               id,operation_id,request_fingerprint,event_type,grant_id,
               episode_id,project_id,requested_add_cny,
               prior_grant_cap_cny,grant_cap_cny,
               prior_authority_cap_cny,authority_cap_cny,
               prior_wall_clock_cap_s,wall_clock_cap_s,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("video_budget_authority"),
            operation_id,
            request_fingerprint,
            event_type,
            grant_id,
            episode_id,
            project_id,
            requested_add_cny,
            prior_grant_cap_cny,
            grant_cap_cny,
            prior_authority_cap_cny,
            authority_cap_cny,
            prior_wall_clock_cap_s,
            wall_clock_cap_s,
            created_at,
        ),
    )


def issue_video_completion_grant(
    *,
    episode_id: str,
    project_id: str,
    storyboard_artifact_id: str,
    budget_cap_cny: float | None = None,
    wall_clock_cap_s: float | None = None,
    allow_fallback_adopt: bool = True,
    max_fallback_shots: int | None = None,
    allow_storyboard_edit: bool = False,
    shots_total: int = 0,
    issued_by: str = "user",
    ttl_s: int = GRANT_TTL_S,
    impact_snapshot: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[VideoCompletionGrant, str]:
    """Atomically issue one completion grant and its payable budget authority."""
    ensure_completion_grants_table()
    conn = get_conn()
    ensure_video_budget_authority_tables(conn)
    requested_budget = (
        float(budget_cap_cny) if budget_cap_cny is not None else None
    )
    requested_wall = (
        float(wall_clock_cap_s)
        if wall_clock_cap_s is not None
        else DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    )
    requested_quota = (
        int(max_fallback_shots)
        if max_fallback_shots is not None
        else default_max_fallback_shots(shots_total)
    )
    request_fingerprint = _content_fingerprint({
        "episode_id": episode_id,
        "project_id": project_id,
        "storyboard_artifact_id": storyboard_artifact_id or "",
        "budget_cap_cny": requested_budget,
        "wall_clock_cap_s": requested_wall,
        "allow_fallback_adopt": bool(allow_fallback_adopt),
        "max_fallback_shots": requested_quota,
        "allow_storyboard_edit": bool(allow_storyboard_edit),
        "shots_total": int(shots_total),
        "issued_by": issued_by,
        "ttl_s": int(ttl_s),
        "impact_snapshot": impact_snapshot or {},
    })
    grant_id = new_id("grant")
    token = secrets.token_urlsafe(24)
    operation_id = _video_budget_authority_operation_id(
        event_type="grant_issued",
        scope_id=episode_id,
        idempotency_key=idempotency_key,
        fallback_id=f"completion-grant-issue:{grant_id}",
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = _idempotent_video_grant(
            conn,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            conn.commit()
            return replay, ""
        episode = conn.execute(
            "SELECT project_id FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if episode is None:
            raise GrantValidationError(
                "GRANT_SCOPE_MISSING",
                "视频补齐授权的分集不存在",
            )
        if str(episode["project_id"]) != str(project_id):
            raise GrantValidationError(
                "GRANT_PROJECT_MISMATCH",
                "视频补齐授权的项目与分集不匹配",
            )
        qualification, qualification_hash = current_video_completion_qualification(
            episode_id,
            conn=conn,
        )
        bound_storyboard_id = str(
            qualification["storyboard_authority"][
                "published_storyboard_artifact_id"
            ]
        )
        if (storyboard_artifact_id or "") != bound_storyboard_id:
            raise GrantValidationError(
                "UPSTREAM_VERSION_CHANGED",
                "请求授权的分镜 Artifact 不是当前发布版",
            )
        generation_plan = qualification["generation_plan"]
        issued_at = now()
        budget_requirement = episode_video_completion_budget_requirement(
            episode_id,
            conn=conn,
        )
        required_cap = float(
            budget_requirement["required_completion_cap_cny"] or 0
        )
        cap = float(
            requested_budget
            if requested_budget is not None
            else max(1.0, required_cap)
        )
        if cap + 1e-9 < required_cap:
            raise GrantValidationError(
                "VIDEO_BUDGET_BELOW_AUTHORITY_PLAN",
                "视频授权低于当前权威镜头计划的一次完整生成成本："
                f"requested={cap:g}, required={required_cap:g}",
            )
        wall = requested_wall
        if not math.isfinite(cap) or not 1 <= cap <= 100000:
            raise GrantValidationError(
                "INVALID_BUDGET",
                "视频补齐预算必须是 1–100000 的有限数",
            )
        if not math.isfinite(wall) or not 60 <= wall <= 604800:
            raise GrantValidationError(
                "INVALID_WALL_CLOCK",
                "视频补齐时长墙必须是 60–604800 秒的有限数",
            )
        deadline_at = issued_at + wall
        expires_at = issued_at + max(60, int(ttl_s), int(wall) + 3600)
        prior_authority = conn.execute(
            """SELECT cap_cny FROM episode_video_budget_authorities
               WHERE episode_id=?""",
            (episode_id,),
        ).fetchone()
        prior_authority_cap = (
            float(prior_authority["cap_cny"])
            if prior_authority is not None
            else None
        )
        conn.execute(
            """INSERT INTO completion_grants(
                id, episode_id, project_id, screenplay_artifact_id, bible_artifact_id,
                permission, token_hash, issued_by, issued_at, expires_at, consumed_at, revoked_at,
                impact_snapshot_json, kind, storyboard_artifact_id, budget_cap_cny, wall_clock_cap_s, deadline_at,
                allow_fallback_adopt, max_fallback_shots, allow_storyboard_edit,
                release_qualification_json, release_qualification_hash,
                episode_video_plan_id, episode_video_plan_revision,
                video_plan_release_hash, capability_snapshot_id
            ) VALUES(?,?,?,?,NULL,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                grant_id, episode_id, project_id, "",
                VIDEO_PERMISSION, _hash_token(token), issued_by, issued_at,
                expires_at, json.dumps(impact_snapshot or {}, ensure_ascii=False),
                "video", storyboard_artifact_id or "", cap, wall, deadline_at,
                1 if allow_fallback_adopt else 0, requested_quota,
                1 if allow_storyboard_edit else 0,
                _canonical_json(qualification), qualification_hash,
                generation_plan.get("episode_video_plan_id"),
                generation_plan.get("plan_revision"),
                generation_plan.get("release_qualification_hash"),
                generation_plan.get("capability_snapshot_id"),
            ),
        )
        authority_cap = authorize_episode_video_budget_absolute(
            episode_id,
            cap,
            source=f"completion_grant:{grant_id}",
            conn=conn,
        )
        _record_video_budget_authority_event(
            conn,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
            event_type="grant_issued",
            grant_id=grant_id,
            episode_id=episode_id,
            project_id=project_id,
            requested_add_cny=cap,
            prior_grant_cap_cny=None,
            grant_cap_cny=cap,
            prior_authority_cap_cny=prior_authority_cap,
            authority_cap_cny=authority_cap,
            prior_wall_clock_cap_s=None,
            wall_clock_cap_s=wall,
            created_at=issued_at,
        )
        grant = VideoCompletionGrant(
            grant_id=grant_id,
            episode_id=episode_id,
            project_id=project_id,
            storyboard_artifact_id=storyboard_artifact_id or "",
            release_qualification_hash=qualification_hash,
            release_qualification=qualification,
            episode_video_plan_id=generation_plan.get("episode_video_plan_id"),
            episode_video_plan_revision=generation_plan.get("plan_revision"),
            video_plan_release_hash=generation_plan.get(
                "release_qualification_hash"
            ),
            capability_snapshot_id=generation_plan.get(
                "capability_snapshot_id"
            ),
            budget_cap_cny=cap,
            wall_clock_cap_s=wall,
            deadline_at=deadline_at,
            allow_fallback_adopt=allow_fallback_adopt,
            max_fallback_shots=requested_quota,
            allow_storyboard_edit=allow_storyboard_edit,
            issued_by=issued_by,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        conn.commit()
        return grant, token
    except ValueError as exc:
        if conn.in_transaction:
            conn.rollback()
        if isinstance(exc, GrantValidationError):
            raise
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_INVALID",
            str(exc),
        ) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _row_to_video_grant(row) -> VideoCompletionGrant:
    def _col(name, default=None):
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return default

    try:
        release_qualification = json.loads(
            _col("release_qualification_json", "{}") or "{}"
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        release_qualification = {}
    return VideoCompletionGrant(
        grant_id=row["id"],
        episode_id=row["episode_id"],
        project_id=row["project_id"],
        storyboard_artifact_id=_col("storyboard_artifact_id") or "",
        release_qualification_hash=_col("release_qualification_hash") or "",
        release_qualification=release_qualification,
        episode_video_plan_id=_col("episode_video_plan_id") or None,
        episode_video_plan_revision=(
            int(_col("episode_video_plan_revision"))
            if _col("episode_video_plan_revision") is not None
            else None
        ),
        video_plan_release_hash=_col("video_plan_release_hash") or None,
        capability_snapshot_id=_col("capability_snapshot_id") or None,
        budget_cap_cny=float(_col("budget_cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY),
        wall_clock_cap_s=float(_col("wall_clock_cap_s") or DEFAULT_VIDEO_WALL_CLOCK_CAP_S),
        deadline_at=float(
            _col("deadline_at")
            or (float(row["issued_at"]) + float(_col("wall_clock_cap_s") or DEFAULT_VIDEO_WALL_CLOCK_CAP_S))
        ),
        allow_fallback_adopt=bool(int(_col("allow_fallback_adopt", 1) or 0)),
        max_fallback_shots=int(_col("max_fallback_shots") or 0),
        allow_storyboard_edit=bool(int(_col("allow_storyboard_edit", 0) or 0)),
        issued_by=row["issued_by"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        revoked_at=row["revoked_at"],
    )


def get_video_grant(grant_id: str) -> VideoCompletionGrant | None:
    ensure_completion_grants_table()
    row = get_conn().execute(
        "SELECT * FROM completion_grants WHERE id=?", (grant_id,)
    ).fetchone()
    if not row:
        return None
    try:
        kind = row["kind"]
    except (KeyError, IndexError, TypeError):
        kind = None
    if kind != "video" and row["permission"] != VIDEO_PERMISSION:
        return None
    return _row_to_video_grant(row)


class GrantValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_video_grant(
    grant_id: str,
    *,
    episode_id: str,
    storyboard_artifact_id: str | None,
) -> VideoCompletionGrant:
    """视频补齐前校验授权。"""
    grant = get_video_grant(grant_id)
    if grant is None:
        raise GrantValidationError("GRANT_NOT_FOUND", "视频补齐授权不存在")
    if grant.episode_id != episode_id:
        raise GrantValidationError("GRANT_EPISODE_MISMATCH", "授权不属于本集")
    if grant.revoked_at is not None:
        raise GrantValidationError("GRANT_REVOKED", "视频补齐授权已撤销")
    if grant.consumed_at is not None:
        raise GrantValidationError("GRANT_CONSUMED", "视频补齐授权已使用")
    if now() > grant.expires_at:
        raise GrantValidationError("GRANT_EXPIRED", "视频补齐授权已过期")
    if (storyboard_artifact_id or "") != (grant.storyboard_artifact_id or ""):
        raise GrantValidationError(
            "UPSTREAM_VERSION_CHANGED",
            "分镜 Artifact 已变更，视频补齐授权失效",
        )
    stored = grant.release_qualification
    stored_hash = grant.release_qualification_hash
    if not stored or not stored_hash or _content_fingerprint(stored) != stored_hash:
        raise GrantValidationError(
            "GRANT_RELEASE_QUALIFICATION_MISSING",
            "视频补齐授权缺少可重算的发布资格绑定",
        )
    plan_binding = dict(stored.get("generation_plan") or {})
    plan_applicable = bool(plan_binding.get("applicable"))
    try:
        current, current_hash = current_video_completion_qualification(
            episode_id,
            generation_plan_applicable=plan_applicable,
        )
    except ValueError as exc:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_INVALID",
            f"当前发布资格无法验证：{exc}",
        ) from exc
    if current_hash != stored_hash or current != stored:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_CHANGED",
            "剧本、分镜、审读、凭证、Shot 投影或视频计划已变更，请重新授权",
        )
    if plan_applicable and (
        grant.episode_video_plan_id != plan_binding.get("episode_video_plan_id")
        or grant.episode_video_plan_revision != plan_binding.get("plan_revision")
        or grant.video_plan_release_hash != plan_binding.get("release_qualification_hash")
        or grant.capability_snapshot_id != plan_binding.get("capability_snapshot_id")
    ):
        raise GrantValidationError(
            "GRANT_PLAN_BINDING_CORRUPT",
            "授权的视频计划绑定与内容指纹不一致",
        )
    return grant


def bind_video_grant_generation_plan(
    grant_id: str,
    *,
    episode_id: str,
    storyboard_artifact_id: str | None,
) -> VideoCompletionGrant:
    """Atomically bind the first valid plan before any paid media preparation.

    A grant may be issued before the asynchronous planner has run.  Its release
    facts are already immutable at that point; this operation may only replace
    the explicit ``plan_pending_at_grant_issue`` slot while every other release
    fact is byte-for-byte unchanged.
    """
    grant = validate_video_grant(
        grant_id,
        episode_id=episode_id,
        storyboard_artifact_id=storyboard_artifact_id,
    )
    old = grant.release_qualification
    old_plan = dict(old.get("generation_plan") or {})
    if old_plan.get("applicable"):
        return grant
    try:
        current, current_hash = current_video_completion_qualification(
            episode_id,
            generation_plan_applicable=True,
        )
    except ValueError as exc:
        raise GrantValidationError("VIDEO_PLAN_INVALID", str(exc)) from exc
    old_release = {key: value for key, value in old.items() if key != "generation_plan"}
    current_release = {
        key: value for key, value in current.items() if key != "generation_plan"
    }
    if old_release != current_release:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_CHANGED",
            "视频计划产生前发布资格已变更，不得继续绑定",
        )
    plan = current["generation_plan"]
    conn = get_conn()
    updated = conn.execute(
        """UPDATE completion_grants
              SET release_qualification_json=?,release_qualification_hash=?,
                  episode_video_plan_id=?,episode_video_plan_revision=?,
                  video_plan_release_hash=?,capability_snapshot_id=?
            WHERE id=? AND release_qualification_hash=?""",
        (
            _canonical_json(current),
            current_hash,
            plan["episode_video_plan_id"],
            plan["plan_revision"],
            plan["release_qualification_hash"],
            plan["capability_snapshot_id"],
            grant_id,
            grant.release_qualification_hash,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise GrantValidationError(
            "GRANT_CONCURRENTLY_CHANGED",
            "视频补齐授权在绑定计划时已被并发修改",
        )
    conn.commit()
    return validate_video_grant(
        grant_id,
        episode_id=episode_id,
        storyboard_artifact_id=storyboard_artifact_id,
    )


def bump_video_grant_budget(
    grant_id: str,
    *,
    add_cny: float,
    add_wall_s: float = 0,
    idempotency_key: str | None = None,
) -> VideoCompletionGrant:
    """Atomically top up a grant, its budget authority and audit ledger."""
    ensure_completion_grants_table()
    add_cny = float(add_cny)
    add_wall_s = float(add_wall_s)
    if not math.isfinite(add_cny) or add_cny < 0 or add_cny > 100000:
        raise GrantValidationError("INVALID_BUDGET", "追加预算必须是 0–100000 的有限数")
    if not math.isfinite(add_wall_s) or add_wall_s < 0 or add_wall_s > 604800:
        raise GrantValidationError("INVALID_WALL_CLOCK", "追加时长必须是 0–604800 秒的有限数")
    if add_cny == 0 and add_wall_s == 0:
        raise GrantValidationError("EMPTY_TOPUP", "追加预算和时长不能同时为 0")
    request_fingerprint = _content_fingerprint({
        "grant_id": grant_id,
        "add_cny": add_cny,
        "add_wall_s": add_wall_s,
    })
    operation_id = _video_budget_authority_operation_id(
        event_type="grant_topped_up",
        scope_id=grant_id,
        idempotency_key=idempotency_key,
        fallback_id=f"completion-grant-topup:{new_id('topup')}",
    )
    conn = get_conn()
    ensure_video_budget_authority_tables(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = _idempotent_video_grant(
            conn,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            conn.commit()
            return replay
        row = conn.execute(
            "SELECT * FROM completion_grants WHERE id=?",
            (grant_id,),
        ).fetchone()
        if row is None or (
            row["kind"] != "video" and row["permission"] != VIDEO_PERMISSION
        ):
            raise GrantValidationError(
                "GRANT_NOT_FOUND",
                "视频补齐授权不存在",
            )
        grant = _row_to_video_grant(row)
        if grant.revoked_at is not None:
            raise GrantValidationError(
                "GRANT_REVOKED",
                "视频补齐授权已撤销",
            )
        new_cap = float(grant.budget_cap_cny) + add_cny
        new_wall = float(grant.wall_clock_cap_s) + add_wall_s
        if new_cap > 100000 or new_wall > 604800:
            raise GrantValidationError(
                "GRANT_LIMIT_EXCEEDED",
                "追加后授权超过最大上限",
            )
        stamp = now()
        new_deadline = float(grant.issued_at) + new_wall
        new_expires = max(float(grant.expires_at), stamp + GRANT_TTL_S)
        prior_authority = conn.execute(
            """SELECT cap_cny FROM episode_video_budget_authorities
               WHERE episode_id=?""",
            (grant.episode_id,),
        ).fetchone()
        prior_authority_cap = (
            float(prior_authority["cap_cny"])
            if prior_authority is not None
            else None
        )
        updated = conn.execute(
            """UPDATE completion_grants
                  SET budget_cap_cny=?,wall_clock_cap_s=?,deadline_at=?,
                      expires_at=?,consumed_at=NULL
                WHERE id=? AND budget_cap_cny=? AND wall_clock_cap_s=?
                  AND deadline_at=? AND expires_at=? AND revoked_at IS NULL""",
            (
                new_cap,
                new_wall,
                new_deadline,
                new_expires,
                grant_id,
                grant.budget_cap_cny,
                grant.wall_clock_cap_s,
                grant.deadline_at,
                grant.expires_at,
            ),
        )
        if updated.rowcount != 1:
            raise GrantValidationError(
                "GRANT_CONCURRENTLY_CHANGED",
                "视频补齐授权在追加预算时已被并发修改",
            )
        authority_cap = authorize_episode_video_budget_absolute(
            grant.episode_id,
            new_cap,
            source=f"completion_grant_topup:{grant_id}",
            conn=conn,
        )
        _record_video_budget_authority_event(
            conn,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
            event_type="grant_topped_up",
            grant_id=grant_id,
            episode_id=grant.episode_id,
            project_id=grant.project_id,
            requested_add_cny=add_cny,
            prior_grant_cap_cny=grant.budget_cap_cny,
            grant_cap_cny=new_cap,
            prior_authority_cap_cny=prior_authority_cap,
            authority_cap_cny=authority_cap,
            prior_wall_clock_cap_s=grant.wall_clock_cap_s,
            wall_clock_cap_s=new_wall,
            created_at=stamp,
        )
        stored = conn.execute(
            "SELECT * FROM completion_grants WHERE id=?",
            (grant_id,),
        ).fetchone()
        if stored is None:
            raise RuntimeError("追加后 completion grant 丢失")
        result = _row_to_video_grant(stored)
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def consume_grant(grant_id: str) -> None:
    ensure_completion_grants_table()
    conn = get_conn()
    conn.execute(
        "UPDATE completion_grants SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
        (now(), grant_id),
    )
    conn.commit()


def revoke_grant(grant_id: str) -> None:
    ensure_completion_grants_table()
    conn = get_conn()
    conn.execute(
        "UPDATE completion_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (now(), grant_id),
    )
    conn.commit()


def revoke_active_video_grants_for_episode(episode_id: str) -> int:
    ensure_completion_grants_table()
    conn = get_conn()
    cur = conn.execute(
        """UPDATE completion_grants SET revoked_at=?
           WHERE episode_id=? AND kind='video' AND revoked_at IS NULL AND consumed_at IS NULL""",
        (now(), episode_id),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def active_video_grant_budget_cap(episode_id: str) -> float | None:
    """若本集有未撤销的视频 grant，返回其 budget_cap_cny，供 enqueue 优先读取。"""
    ensure_completion_grants_table()
    row = get_conn().execute(
        """SELECT budget_cap_cny FROM completion_grants
           WHERE episode_id=? AND kind='video' AND revoked_at IS NULL
             AND (consumed_at IS NULL OR consumed_at=0)
             AND expires_at > ?
           ORDER BY issued_at DESC LIMIT 1""",
        (episode_id, now()),
    ).fetchone()
    if not row:
        return None
    try:
        return float(row["budget_cap_cny"]) if row["budget_cap_cny"] is not None else None
    except (TypeError, ValueError, KeyError):
        return None


# app.db.init_db() no longer imports this module directly (P0-3 dependency
# inversion, docs/coupling_review_2026-08-29.md 第2步) — it looks these up by
# name through app.db_schema instead. Registered at import time so the name
# is resolvable as soon as something has imported this module once.
from app.db_schema import register_table as _register_table  # noqa: E402

_register_table("video_budget_authority_tables", ensure_video_budget_authority_tables)
_register_table("completion_grants_table", ensure_completion_grants_table)
_register_table("legacy_video_liabilities_migration", migrate_legacy_video_liabilities)
