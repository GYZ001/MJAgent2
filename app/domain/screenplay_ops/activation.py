"""剧本生成的孤儿收据清理、预算投影与实际发起生成（激活）主逻辑。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 guarded 与 status_snapshot，因此排在 guarded 之后。
"""
from __future__ import annotations

import json

from app import (
    screenplay_retry_authority as _retry_authority,
    task_registry,
)
from app.db import (
    get_conn,
    now,
)
from app.domain.common import _episode_source_text
from app.orchestration.engine import (
    WorkflowRecorder,
    fingerprint,
)
from app.orchestration.state_machine import StateConflict
from typing import Any

from .guarded import _screenplay_guarded
from .status_snapshot import _clear_unpublished_screenplay_ir


def _abandon_orphaned_blueprint_receipts(episode_id: str, *, conn) -> int:
    """Close unknown blueprint outcomes whose production no longer exists.

    The scope is exactly what the budget still counts as an unresolved receipt
    -- an unsuperseded ``INTERRUPTED``/``RUNNING`` call -- and not merely calls
    with no disposition yet: an unknown outcome is normally already stamped
    ``REQUIRES_EXPLICIT_RETRY``, which is precisely the state that needs
    closing.  Anything genuinely resolved carries a superseding call id and is
    excluded by that guard.  The rows, their cost and their responses stay
    untouched as audit evidence; only the open liability is settled.
    """
    from app.stages import BLUEPRINT_CALL_ABANDONED_BY_DELETE

    cursor = conn.execute(
        """UPDATE provider_calls SET recovery_disposition=?
            WHERE status IN ('INTERRUPTED','RUNNING')
              AND superseded_by_call_id IS NULL
              AND COALESCE(recovery_disposition,'') <> ?
              AND json_extract(meta,'$.stage_key') IN (
                  'screenplay_blueprint_shard',
                  'screenplay_blueprint_patch',
                  'screenplay_blueprint_review'
              )
              AND (
                  json_extract(meta,'$.episode_id')=?
                  OR run_id IN (
                      SELECT id FROM workflow_runs
                       WHERE scope_type='episode' AND scope_id=?
                  )
              )""",
        (
            BLUEPRINT_CALL_ABANDONED_BY_DELETE,
            BLUEPRINT_CALL_ABANDONED_BY_DELETE,
            episode_id,
            episode_id,
        ),
    )
    return int(cursor.rowcount or 0)

def _screenplay_blueprint_budget_projection(
    episode_id: str,
    *,
    run_id: str | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    """Read-only budget/grant projection shared by preflight and activation."""
    from app.source_excerpt import index_source_segments
    from app.stages import (
        BLUEPRINT_SHARD_MIN_TOKENS,
        _BlueprintGenerationBudget,
        _partition_blueprint_segments,
        blueprint_retry_receipts_hash,
    )

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if ep is None:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT bible_version FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    source_text = _episode_source_text(conn, ep)
    input_fp = fingerprint(
        episode_id,
        ep["source_chapters"],
        source_text,
        project["bible_version"] if project else 0,
    )
    revision_row = conn.execute(
        """SELECT id,grant_id FROM production_revisions
             WHERE episode_id=? AND kind='screenplay' AND status='active'
             ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    revision = dict(revision_row) if revision_row is not None else None
    current_grant_id = str(revision.get("grant_id") or "") if revision else ""
    budget = _BlueprintGenerationBudget.from_durable_calls(
        run_id=run_id,
        started_at_epoch=started_at,
        episode_id=episode_id,
        input_fingerprint=input_fp,
        retry_grant_id=current_grant_id,
    )
    if current_grant_id and budget.unknown_receipts:
        grant_row = conn.execute(
            """SELECT issued_by,input_artifact_hash,consumed_at
                 FROM production_grants
                WHERE id=? AND episode_id=? AND kind='screenplay'
                  AND production_revision_id=?
                  AND revoked_at IS NULL AND expires_at>?
                  AND consumed_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM workflow_runs wr
                       WHERE json_extract(
                           wr.config_snapshot_json,
                           '$.blueprint_retry_grant_id'
                       )=production_grants.id
                  )""",
            (
                current_grant_id,
                episode_id,
                str(revision["id"]),
                now(),
            ),
        ).fetchone()
        if (
            grant_row is not None
            and str(grant_row["issued_by"] or "") == "user_retry_approval"
            and str(grant_row["input_artifact_hash"] or "")
            == blueprint_retry_receipts_hash(budget.unknown_receipts)
        ):
            budget.authorize_unknown_retry(current_grant_id)
    # Size the runaway breakers from the same deterministic partition the
    # stage will plan from.  Cached leaves and dynamic splits can only add
    # leaves, so this uncached count is a lower bound: the fence here is never
    # more permissive than the runtime budget, and never rejects an episode
    # purely for being long.
    budget.adopt_shard_plan(
        len(_partition_blueprint_segments(index_source_segments(source_text)))
    )
    token_admissible = (
        budget.charged_output_tokens + BLUEPRINT_SHARD_MIN_TOKENS
        <= budget.max_output_tokens
    )
    call_admissible = budget.provider_calls < budget.max_provider_calls
    return {
        "budget": budget,
        "input_fingerprint": input_fp,
        "revision": revision,
        "current_grant_id": current_grant_id,
        "requires_fresh_retry_grant": budget.requires_fresh_retry_grant,
        "unknown_receipts": budget.unknown_receipts,
        "provider_calls": budget.provider_calls,
        "charged_output_tokens": budget.charged_output_tokens,
        "unknown_output_tokens": budget.unknown_output_tokens,
        "token_admissible": token_admissible,
        "call_admissible": call_admissible,
        "admissible_after_approval": token_admissible and call_admissible,
    }

def _spawn_screenplay_activation(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    project_id: str,
    status: str,
    message: str | None,
    preserve_started_at: bool = False,
    task_factory=None,
    expected_active_run_id: str | None = None,
    clear_unpublished_ir: bool = False,
    resume_eligibility=None,
    authorize_blueprint_retry: bool = False,
    expected_blueprint_unknown_receipts: list[dict[str, Any]] | None = None,
):
    """Atomically claim one episode before registering its in-process task."""
    conn = get_conn()
    previous: dict | None = None
    registered_task = None
    prepared_revision = None
    activation_retry_grant_id = ""
    activation_retry_receipts_hash = ""
    activation_retry_revision_id = ""
    try:
        conn.execute("BEGIN IMMEDIATE")
        activation_stamp = now()
        retry_approval_evidence = (
            _retry_authority.consume_screenplay_command_bus_retry_approval()
        )
        previous_row = conn.execute(
            "SELECT screenplay_status, screenplay_error, screenplay_started_at, "
            "screenplay_updated_at, active_screenplay_run_id, "
            "screenplay_publish_fence, "
            "screenplay_character_resolutions, screenplay_required_dialogues, "
            "screenplay_required_dialogue_occurrences "
            "FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if not previous_row:
            raise ValueError(f"episode not found: {episode_id}")
        previous = dict(previous_row)
        if previous["screenplay_publish_fence"]:
            raise StateConflict(
                "screenplay_publish_fence",
                episode_id,
                {"0"},
                str(previous["screenplay_publish_fence"]),
            )
        previous_run_id = str(
            previous["active_screenplay_run_id"] or ""
        )
        if expected_active_run_id is not None:
            if previous_run_id != str(expected_active_run_id or ""):
                raise StateConflict(
                    "screenplay_owner",
                    episode_id,
                    {str(expected_active_run_id or "")},
                    previous_run_id,
                )
        elif (
            previous["screenplay_status"] in {
                "queued", "running", "repairing",
            }
            and previous_run_id
        ):
            owner = conn.execute(
                "SELECT status FROM workflow_runs WHERE id=?",
                (previous_run_id,),
            ).fetchone()
            if owner and owner["status"] not in {
                "FAILED", "CANCELLED", "SUCCEEDED", "PARTIAL",
            }:
                raise StateConflict(
                    "screenplay_owner",
                    episode_id,
                    {""},
                    previous_run_id,
                )
        if resume_eligibility is not None:
            from app.production.revision import (
                rebase_screenplay_revision_for_resume,
                resolve_screenplay_resume_eligibility,
            )

            current_eligibility = resolve_screenplay_resume_eligibility(
                episode_id,
                conn=conn,
            )
            if (
                current_eligibility.mode != resume_eligibility.mode
                or current_eligibility.revision_action
                != resume_eligibility.revision_action
                or current_eligibility.revision_id
                != resume_eligibility.revision_id
                or current_eligibility.working_artifact_id
                != resume_eligibility.working_artifact_id
            ):
                raise StateConflict(
                    "screenplay_resume_eligibility",
                    episode_id,
                    {resume_eligibility.mode},
                    current_eligibility.mode,
                )
            if current_eligibility.revision_action == "rebase":
                prepared_revision = rebase_screenplay_revision_for_resume(
                    current_eligibility,
                    conn=conn,
                )
        if clear_unpublished_ir:
            _clear_unpublished_screenplay_ir(
                episode_id,
                conn=conn,
                commit=False,
            )
            conn.execute(
                "UPDATE episodes SET screenplay_character_resolutions='[]', "
                "screenplay_required_dialogues='[]', "
                "screenplay_required_dialogue_occurrences='[]' "
                "WHERE id=?",
                (episode_id,),
            )
        budget_projection = _screenplay_blueprint_budget_projection(
            episode_id,
            run_id=recorder.run_id,
            started_at=activation_stamp,
        )
        budget = budget_projection["budget"]
        if (
            clear_unpublished_ir
            and budget_projection["requires_fresh_retry_grant"]
            and budget_projection["revision"] is None
        ):
            # A fresh Baseline with no active revision has no production for
            # these receipts to belong to: the run that made them was
            # discarded and the revision a retry grant must bind to went with
            # it.  Every gate below would then be unsatisfiable -- there is no
            # grant that can be issued and no approval that can stand in for
            # one -- so the episode would be unstartable for good.  Settle the
            # orphaned liability exactly as a delete does and let this
            # activation proceed on its own new revision and budget.  A resume
            # never takes this path: it really does re-enter the production
            # those receipts were spent on, and still fails closed.
            if _abandon_orphaned_blueprint_receipts(episode_id, conn=conn):
                budget_projection = _screenplay_blueprint_budget_projection(
                    episode_id,
                    run_id=recorder.run_id,
                    started_at=activation_stamp,
                )
                budget = budget_projection["budget"]
        if budget_projection["requires_fresh_retry_grant"]:
            trusted_retry_approval = bool(
                authorize_blueprint_retry
                and retry_approval_evidence
            )
            from app.stages import blueprint_retry_receipts_hash

            current_receipts_hash = blueprint_retry_receipts_hash(
                budget_projection["unknown_receipts"]
            )

            if (
                trusted_retry_approval
                and str(retry_approval_evidence.get("receipts_hash") or "")
                != current_receipts_hash
            ):
                raise StateConflict(
                    "blueprint_unknown_retry_approval",
                    episode_id,
                    {
                        str(
                            retry_approval_evidence.get("receipts_hash")
                            or ""
                        )
                    },
                    current_receipts_hash,
                )
            if not trusted_retry_approval:
                budget.assert_activation_admissible()
            expected_receipts = expected_blueprint_unknown_receipts or []
            if expected_receipts != budget_projection["unknown_receipts"]:
                raise StateConflict(
                    "blueprint_unknown_retry_receipts",
                    episode_id,
                    {fingerprint(expected_receipts)},
                    fingerprint(budget_projection["unknown_receipts"]),
                )
            revision = budget_projection["revision"]
            if revision is None:
                raise RuntimeError(
                    "BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED: 缺少可绑定的 active revision"
                )
            from app.production.grant import issue_production_grant
            grant, _token = issue_production_grant(
                episode_id=episode_id,
                project_id=project_id,
                production_revision_id=str(revision["id"]),
                kind="screenplay",
                input_artifact_hash=blueprint_retry_receipts_hash(
                    budget_projection["unknown_receipts"]
                ),
                issued_by="user_retry_approval",
                conn=conn,
                commit=False,
            )
            budget.authorize_unknown_retry(grant.grant_id)
            activation_retry_grant_id = grant.grant_id
            activation_retry_receipts_hash = current_receipts_hash
            activation_retry_revision_id = str(revision["id"])
            run_row = conn.execute(
                "SELECT config_snapshot_json FROM workflow_runs WHERE id=?",
                (recorder.run_id,),
            ).fetchone()
            config_snapshot = json.loads(
                run_row["config_snapshot_json"] or "{}"
            ) if run_row is not None else {}
            config_snapshot.update({
                "blueprint_retry_grant_id": grant.grant_id,
                "blueprint_retry_receipts_hash": (
                    blueprint_retry_receipts_hash(
                        budget_projection["unknown_receipts"]
                    )
                ),
                "blueprint_retry_receipts": list(
                    budget_projection["unknown_receipts"]
                ),
            })
            conn.execute(
                "UPDATE workflow_runs SET config_snapshot_json=?,updated_at=? "
                "WHERE id=?",
                (
                    json.dumps(config_snapshot, ensure_ascii=False),
                    activation_stamp,
                    recorder.run_id,
                ),
            )
        elif budget.unknown_receipts and budget.retry_grant_id:
            # A legacy unconsumed exact grant may authorize one activation.
            # It is consumed below only after the task registry accepts the
            # worker, in the same transaction as the run snapshot and owner.
            activation_retry_grant_id = budget.retry_grant_id
            from app.stages import blueprint_retry_receipts_hash

            activation_retry_receipts_hash = blueprint_retry_receipts_hash(
                budget.unknown_receipts
            )
            revision = budget_projection["revision"]
            activation_retry_revision_id = (
                str(revision["id"]) if revision is not None else ""
            )
            run_row = conn.execute(
                "SELECT config_snapshot_json FROM workflow_runs WHERE id=?",
                (recorder.run_id,),
            ).fetchone()
            config_snapshot = json.loads(
                run_row["config_snapshot_json"] or "{}"
            ) if run_row is not None else {}
            config_snapshot.update({
                "blueprint_retry_grant_id": activation_retry_grant_id,
                "blueprint_retry_receipts_hash": (
                    activation_retry_receipts_hash
                ),
                "blueprint_retry_receipts": list(budget.unknown_receipts),
            })
            conn.execute(
                "UPDATE workflow_runs SET config_snapshot_json=?,updated_at=? "
                "WHERE id=?",
                (
                    json.dumps(config_snapshot, ensure_ascii=False),
                    activation_stamp,
                    recorder.run_id,
                ),
            )
        budget.assert_activation_admissible()
        stamp = activation_stamp
        started_at = (
            previous["screenplay_started_at"]
            if preserve_started_at else stamp
        )
        if started_at is None:
            started_at = stamp
        cursor = conn.execute(
            "UPDATE episodes SET screenplay_status=?, screenplay_error=?, "
            "screenplay_started_at=?, screenplay_updated_at=?, "
            "active_screenplay_run_id=? "
            "WHERE id=? AND COALESCE(active_screenplay_run_id, '')=?",
            (
                status,
                message,
                started_at,
                stamp,
                recorder.run_id,
                episode_id,
                previous_run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise StateConflict(
                "screenplay_owner",
                episode_id,
                {previous_run_id},
                "changed_during_claim",
            )
        task_coro = (
            task_factory()
            if task_factory is not None
            else _screenplay_guarded(episode_id, recorder)
        )
        registered_task = task_registry.spawn(
            "screenplay",
            episode_id,
            task_coro,
            project_id=project_id,
        )
        if activation_retry_grant_id:
            consumed = conn.execute(
                "UPDATE production_grants SET consumed_at=? "
                "WHERE id=? AND episode_id=? AND project_id=? "
                "AND production_revision_id=? AND kind='screenplay' "
                "AND issued_by='user_retry_approval' "
                "AND input_artifact_hash=? "
                "AND consumed_at IS NULL AND revoked_at IS NULL "
                "AND expires_at>? AND EXISTS ("
                " SELECT 1 FROM production_revisions r "
                "  WHERE r.id=production_grants.production_revision_id "
                "    AND r.episode_id=production_grants.episode_id "
                "    AND r.kind='screenplay' AND r.status='active' "
                "    AND r.grant_id=production_grants.id"
                ")",
                (
                    activation_stamp,
                    activation_retry_grant_id,
                    episode_id,
                    project_id,
                    activation_retry_revision_id,
                    activation_retry_receipts_hash,
                    activation_stamp,
                ),
            )
            if consumed.rowcount != 1:
                raise StateConflict(
                    "blueprint_retry_grant_consumption",
                    episode_id,
                    {activation_retry_grant_id},
                    "already_consumed_or_inactive",
                )
        # ``spawn`` only schedules the coroutine; it cannot run until this
        # synchronous function yields back to the event loop.  Commit after the
        # registry accepts it so a registration failure rolls back the owner
        # claim, identity columns and deleted retry-only IR in one transaction.
        conn.commit()
        return prepared_revision
    except BaseException:
        if registered_task is not None:
            task_registry.cancel("screenplay", episode_id)
        if conn.in_transaction:
            conn.rollback()
        try:
            recorder.cancel("任务未能启动，剧集状态已回滚", conn=None)
        except Exception:  # noqa: BLE001 - rollback must not be hidden by run bookkeeping
            pass
        raise
