"""清空/删除前的供应商任务对账。

只在「删项目 / 清空整集」路径上被调用：核对供应商任务的真实终态，把零产出的
失败结算为 0（2026-08-30，见 2969902），有产出的按金额结算。
"""
from __future__ import annotations

import json
from typing import Any


from app.db import now
from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
    provider_task_clearance_snapshot,
)
from app.completion_grant.ledger import ensure_video_budget_authority_tables


def _claimed_amount_cny(db, operation_id: str) -> float:
    """真源查询：``provider_video_budget_claims.amount_cny``（主键 operation_id）。

    结算写进 shot_versions.cost_cny/budget_reservations.actual_cost_cny 两张
    纯审计台账，不再随 provider_task_clearance 的 blocker 展示结构传递（那个
    结构面向 UI，已退场金额展示）。
    """
    row = db.execute(
        "SELECT amount_cny FROM provider_video_budget_claims WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    return max(0.0, float((row["amount_cny"] if row else 0) or 0))


async def reconcile_provider_tasks_for_clear(
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    shot_ids: list[str] | tuple[str, ...] = (),
    version_ids: list[str] | tuple[str, ...] = (),
    conn,
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

    db = conn
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
        amount_cny = _claimed_amount_cny(db, operation_id)
        # 通用措辞：这条路径既服务整项目删除也服务分集清空（storyboard/videos
        # clear 撞上 PROVIDER_TASKS_NOT_TERMINAL 后的恢复入口），不能预设是
        # 哪一种触发场景；具体触发点交给下面的 evidence_source 后缀记录。
        terminal_message = (
            "已核对供应商任务成功终态；账目已结清，结果保持隔离且不可采用"
            if status == "succeeded"
            else "已核对供应商任务失败终态；账目已结清"
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
                        amount_cny if status == "succeeded" else 0.0,  # failed=零产出不计费
                        version_id,
                    ),
                )
            db.execute(
                """UPDATE budget_reservations
                      SET status='settled',settled_at=?,actual_cost_cny=?
                    WHERE job_id=? AND status IN ('reserved','running')""",
                (stamp, amount_cny if status == "succeeded" else 0.0, job_id),
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
    conn,
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
    episode_id: str, *, conn,
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
    ``status='paused'`` to any non-terminal status so a historical
    ``paused_budget`` orphan (money no longer gates dispatch, see
    CLAUDE.md「Retiring Features」) does not sit stuck forever once its shot
    has moved on. This never touches a job that still has any provider
    footprint or any non-released claim.
    """
    db = conn
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
