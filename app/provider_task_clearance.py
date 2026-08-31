"""供应商付费任务的终态清空判据（provider task clearance）。

从 ``app/completion_grant.py`` 按原样搬移出来的一小簇符号：
``ProviderTasksNotTerminalError``/``_provider_task_clearance_evaluation``/
``provider_task_clearance_snapshot``/``assert_provider_tasks_clearable``/
``prepare_provider_tasks_for_clear``。这五个符号只对
``jobs``/``provider_video_budget_claims``/``provider_calls``/``shot_versions``
四张表做纯 SQL 查询和状态流转，模块级只依赖 ``app.db``——不依赖
``completion_grant.py`` 其余函数（预算授权、剧本/分镜发布材料聚合）需要的
``app.production.screenplay_authority``/``app.video_plan``/``app.hiagent``/
``app.evidence``。那些函数把整个 ``completion_grant.py`` 钉在 L4，但
``app.artifacts``（L2）只调用这条链路（``prepare_provider_tasks_for_clear``，
经由 ``artifacts.py`` 两处清空流程），把它独立成本文件后 ``artifacts`` 不再需要
越级依赖 L4——见 ``docs/layer_violations_plan_2026-08-30.md`` 组 5。

``completion_grant.py`` 从本文件重新导入这五个符号并保持原样对外可见（所有既有
``from app.completion_grant import ProviderTasksNotTerminalError`` 等调用点不用
改），``artifacts.py`` 的两处调用改成直接从本文件导入。

2026-08-30：``provider_task_clearance_snapshot``/``prepare_provider_tasks_for_
clear`` 各自延迟 import 同层的 ``app.provider_task_zero_cost``（供应商已终态
拒绝、且确凿零扣费的一类，``provably_unsubmitted_cancelled``/
``external_terminal`` 都不覆盖），只读重新分类 blocker、必要时结算——延迟导入
不是为了避环（两个模块互不反向依赖），只是不想改「模块级只依赖 app.db」这句
话本身。
"""
from __future__ import annotations

from typing import Any

from app.db import now
from app.provider_task_scope import build_clearance_scope


class ProviderTasksNotTerminalError(ValueError):
    """Destructive cleanup would erase recovery or billing authority."""

    def __init__(self, clearance: dict[str, Any]):
        self.detail = {
            "code": "PROVIDER_TASKS_NOT_TERMINAL",
            "message": (
                "供应商付费任务尚未终态，未清空任何资源；"
                "请先按恢复状态继续轮询或核对供应商创建结果"
            ),
            **clearance,
        }
        super().__init__(self.detail["message"])


# 清空判据链上的 conn 一律必传，不给默认值。这条链是「删项目 / 清空整集 /
# 清空单镜」的准入闸门：调用方漏传时若回退到自己开的连接，读到的是另一个
# 事务里看不见的状态，safe_to_clear 会与事务内的事实不一致，而且不报错。
# 必传之后，漏传在调用那一刻就是 TypeError。（CLAUDE.md「Ownership Must Be
# Explicit」：可选参数是缺陷的温床。）
def _provider_task_clearance_evaluation(
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    shot_ids: list[str] | tuple[str, ...] = (),
    version_ids: list[str] | tuple[str, ...] = (),
    conn,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    db = conn
    scope = build_clearance_scope(
        db,
        project_id=project_id,
        episode_id=episode_id,
        shot_ids=shot_ids,
        version_ids=version_ids,
    )
    job_columns = scope.job_columns
    job_scope_clauses = scope.job_scope_clauses
    job_scope_params = scope.job_scope_params
    claim_scope_clauses = scope.claim_scope_clauses
    claim_scope_params = scope.claim_scope_params

    claims_available = bool(db.execute(
        """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='provider_video_budget_claims'"""
    ).fetchone())
    provider_calls_available = bool(db.execute(
        """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='provider_calls'"""
    ).fetchone())
    create_call_succeeded_sql = (
        """EXISTS(
               SELECT 1 FROM provider_calls pc
                WHERE pc.kind='video_create' AND pc.status='OK'
                  AND pc.operation_id={operation}
           )"""
        if provider_calls_available
        else "0"
    )
    rows = []
    if claims_available:
        rows.extend(db.execute(
            f"""SELECT COALESCE(j.id,c.origin_job_id) AS job_id,
                       COALESCE(j.version_id,c.version_id,c.origin_version_id)
                           AS version_id,
                       COALESCE(j.project_id,c.project_id) AS project_id,
                       COALESCE(j.episode_id,c.episode_id,c.origin_episode_id)
                           AS episode_id,
                       COALESCE(j.shot_id,c.shot_id,c.origin_shot_id) AS shot_id,
                       j.id AS live_job_id,j.status AS job_status,
                       j.cancellation_requested,j.abandoned,
                       j.provider_non_cancellable,j.provider_operation_id,
                       j.provider_create_state,j.provider_failure_disposition,
                       v.provider_task_id,v.status AS version_status,
                       v.video_path,v.cost_cny,
                       c.status AS claim_status,c.amount_cny AS claim_amount,
                       c.operation_id AS claim_operation_id,c.accepted_at,
                       {create_call_succeeded_sql.format(operation="c.operation_id")}
                           AS create_call_succeeded
                  FROM provider_video_budget_claims c
                  LEFT JOIN jobs j ON j.id=c.job_id
                  LEFT JOIN shot_versions v ON v.id=c.version_id
                 WHERE {" OR ".join(claim_scope_clauses)}
                 ORDER BY c.created_at,c.operation_id""",
            claim_scope_params,
        ).fetchall())
    missing_claim_clause = (
        """AND NOT EXISTS (
               SELECT 1 FROM provider_video_budget_claims c
                WHERE c.job_id=j.id
           )"""
        if claims_available
        else ""
    )
    job_project_select = "j.project_id" if "project_id" in job_columns else "NULL"
    job_episode_select = "j.episode_id" if "episode_id" in job_columns else "NULL"
    rows.extend(db.execute(
        f"""SELECT j.id AS job_id,j.version_id,
                   {job_project_select} AS project_id,
                   {job_episode_select} AS episode_id,j.shot_id,
                   j.id AS live_job_id,
                   j.status AS job_status,
                   j.cancellation_requested,j.abandoned,
                   j.provider_non_cancellable,j.provider_operation_id,
                   j.provider_create_state,j.provider_failure_disposition,
                   v.provider_task_id,v.status AS version_status,
                   v.video_path,v.cost_cny,
                   NULL AS claim_status,NULL AS claim_amount,
                   j.provider_operation_id AS claim_operation_id,
                   NULL AS accepted_at,
                   {create_call_succeeded_sql.format(operation="j.provider_operation_id")}
                       AS create_call_succeeded
              FROM jobs j
              LEFT JOIN shot_versions v ON v.id=j.version_id
             WHERE ({" OR ".join(f"({clause})" for clause in job_scope_clauses)})
               {missing_claim_clause}
             ORDER BY j.created_at,j.id""",
        job_scope_params,
    ).fetchall())

    blockers: list[dict[str, Any]] = []
    releasable_operation_ids: list[str] = []
    settle_operation_ids: list[str] = []
    close_liability_operation_ids: list[str] = []
    for row in rows:
        create_state = str(row["provider_create_state"] or "").strip().lower()
        claim_status = (
            str(row["claim_status"]).strip().lower()
            if row["claim_status"] is not None
            else None
        )
        provider_task_id = str(row["provider_task_id"] or "").strip() or None
        operation_id = str(row["claim_operation_id"] or "").strip() or None
        current_operation_id = (
            str(row["provider_operation_id"] or "").strip() or None
        )
        claim_is_current = (
            claim_status is None
            or (
                row["live_job_id"] is not None
                and operation_id == current_operation_id
            )
        )
        provider_task_for_recovery = (
            provider_task_id if claim_is_current else None
        )
        provably_unsubmitted_cancelled = bool(
            claim_is_current
            and claim_status == "reserved"
            and operation_id
            and row["accepted_at"] is None
            and not provider_task_id
            and not row["provider_non_cancellable"]
            and create_state in {"", "not_started", "submitting"}
            and str(row["job_status"] or "").strip().lower() == "cancelled"
            and row["cancellation_requested"]
            and not row["abandoned"]
            and not row["create_call_succeeded"]
        )
        if provably_unsubmitted_cancelled:
            releasable_operation_ids.append(operation_id)
            continue
        failure_disposition = str(
            row["provider_failure_disposition"] or ""
        ).strip().lower()
        result_checkpointed = (
            claim_is_current
            and
            str(row["version_status"] or "").strip().lower() == "succeeded"
            and bool(
                str(row["video_path"] or "").strip()
                or float(row["cost_cny"] or 0) > 0
            )
        )
        if claim_is_current:
            provider_may_exist = bool(
                provider_task_id
                or row["provider_non_cancellable"]
                or create_state not in {"", "not_started"}
                or (
                    claim_status is not None
                    and claim_status not in {"reserved", "released", "settled"}
                )
            )
        else:
            provider_may_exist = claim_status not in {
                "released",
                "settled",
                "closed_liability",
            }
        terminal_evidence = bool(
            result_checkpointed
            or claim_status in {"settled", "closed_liability"}
            or (
                claim_status == "released"
                and (not claim_is_current or not provider_may_exist)
            )
            or (
                claim_is_current
                and failure_disposition == "external_terminal"
            )
        )
        if not provider_may_exist:
            if claim_status == "reserved" and operation_id:
                releasable_operation_ids.append(operation_id)
            continue
        if terminal_evidence:
            if (
                result_checkpointed
                and operation_id
                and claim_status not in {
                    "settled",
                    "closed_liability",
                    "released",
                }
            ):
                settle_operation_ids.append(operation_id)
            elif (
                claim_is_current
                and failure_disposition == "external_terminal"
                and operation_id
                and claim_status not in {
                    "settled",
                    "closed_liability",
                    "released",
                }
            ):
                close_liability_operation_ids.append(operation_id)
            continue

        locally_recoverable_poll = bool(
            provider_task_for_recovery
            and failure_disposition != "manual_review"
            and not row["cancellation_requested"]
            and not row["abandoned"]
        )
        blockers.append({
            "project_id": (
                str(row["project_id"]) if row["project_id"] is not None else None
            ),
            "episode_id": (
                str(row["episode_id"]) if row["episode_id"] is not None else None
            ),
            "shot_id": (
                str(row["shot_id"]) if row["shot_id"] is not None else None
            ),
            "job_id": str(row["job_id"]),
            "version_id": (
                str(row["version_id"]) if row["version_id"] is not None else None
            ),
            "provider_operation_id": operation_id,
            "provider_task_id": provider_task_for_recovery,
            "job_status": str(row["job_status"] or ""),
            "provider_create_state": (
                create_state if claim_is_current and create_state else "unknown"
            ),
            "claim_status": claim_status,
            "amount_cny": float(row["claim_amount"] or 0),
            "recovery_status": (
                "waiting_provider" if locally_recoverable_poll else "waiting_human"
            ),
            "recovery_action": (
                "review_provider_failure"
                if failure_disposition == "manual_review"
                else (
                    "continue_provider_poll"
                    if locally_recoverable_poll
                    else (
                        "restore_provider_poll"
                        if provider_task_for_recovery
                        else "reconcile_provider_create"
                    )
                )
            ),
        })
    return (
        {
            "safe_to_clear": not blockers,
            "resume_supported": bool(blockers),
            "blockers": blockers,
        },
        {
            "release": list(dict.fromkeys(releasable_operation_ids)),
            "settle": list(dict.fromkeys(settle_operation_ids)),
            "close_liability": list(
                dict.fromkeys(close_liability_operation_ids)
            ),
        },
    )


def provider_task_clearance_snapshot(
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    shot_ids: list[str] | tuple[str, ...] = (),
    version_ids: list[str] | tuple[str, ...] = (),
    conn,
) -> dict[str, Any]:
    """Return whether destructive cleanup can preserve provider authority.

    Scope comes from the project-owned claim ledger and live resources, never
    from job kind. A provider-backed operation without durable terminal
    evidence blocks cleanup so its task handle and billing authority survive.
    """
    from app.provider_task_zero_cost import apply_zero_cost_terminal_release

    db = conn
    clearance, terminal_actions = _provider_task_clearance_evaluation(
        project_id=project_id,
        episode_id=episode_id,
        shot_ids=shot_ids,
        version_ids=version_ids,
        conn=db,
    )
    apply_zero_cost_terminal_release(db, clearance, terminal_actions)
    return clearance


def assert_provider_tasks_clearable(
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    shot_ids: list[str] | tuple[str, ...] = (),
    version_ids: list[str] | tuple[str, ...] = (),
    conn,
) -> dict[str, Any]:
    clearance = provider_task_clearance_snapshot(
        project_id=project_id,
        episode_id=episode_id,
        shot_ids=shot_ids,
        version_ids=version_ids,
        conn=conn,
    )
    if not clearance["safe_to_clear"]:
        raise ProviderTasksNotTerminalError(clearance)
    return clearance


def prepare_provider_tasks_for_clear(
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    shot_ids: list[str] | tuple[str, ...] = (),
    version_ids: list[str] | tuple[str, ...] = (),
    conn,
) -> dict[str, Any]:
    """Fence provider risk and explicitly release unsubmitted reservations."""
    from app.provider_task_zero_cost import (
        _release_zero_cost_terminal_jobs_in_transaction,
        apply_zero_cost_terminal_release,
    )

    db = conn
    clearance, terminal_actions = _provider_task_clearance_evaluation(
        project_id=project_id,
        episode_id=episode_id,
        shot_ids=shot_ids,
        version_ids=version_ids,
        conn=db,
    )
    apply_zero_cost_terminal_release(db, clearance, terminal_actions)
    if not clearance["safe_to_clear"]:
        raise ProviderTasksNotTerminalError(clearance)
    zero_cost_release = terminal_actions.get("zero_cost_release") or []
    if zero_cost_release:
        _release_zero_cost_terminal_jobs_in_transaction(db, zero_cost_release)
    releasable = terminal_actions["release"]
    if releasable:
        marks = ",".join("?" for _ in releasable)
        stamp = now()
        db.execute(
            f"""UPDATE provider_video_budget_claims
                   SET status='released',updated_at=?,released_at=?
                 WHERE operation_id IN ({marks}) AND status='reserved'""",
            (stamp, stamp, *releasable),
        )
    settle = terminal_actions["settle"]
    if settle:
        marks = ",".join("?" for _ in settle)
        stamp = now()
        db.execute(
            f"""UPDATE provider_video_budget_claims
                   SET status='settled',updated_at=?,settled_at=?
                 WHERE operation_id IN ({marks})
                   AND status!='released' AND status!='closed_liability'""",
            (stamp, stamp, *settle),
        )
    close_liability = terminal_actions["close_liability"]
    if close_liability:
        marks = ",".join("?" for _ in close_liability)
        stamp = now()
        db.execute(
            f"""UPDATE provider_video_budget_claims
                   SET status='closed_liability',updated_at=?,
                       liability_closed_at=?,
                       closure_reason='provider_external_terminal'
                 WHERE operation_id IN ({marks})
                   AND status!='released' AND status!='settled'
                   AND status!='closed_liability'""",
            (stamp, stamp, *close_liability),
        )
    return clearance
