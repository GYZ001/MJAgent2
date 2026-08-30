"""启动时恢复孤儿剧本生成任务。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 task_body/activation/guarded，因此排在最后。
"""
from __future__ import annotations

import json

from app import (
    errors,
    task_registry,
)
from app.db import (
    get_conn,
    now,
)
from app.domain.common import _screenplay_ready
from app.orchestration.engine import WorkflowRecorder
from app.orchestration.state_machine import StateConflict

from .activation import _spawn_screenplay_activation
from .guarded import _screenplay_guarded
from .task_body import _new_screenplay_recorder


def recover_screenplay_tasks() -> int:
    """Resume only work that was actually interrupted by a service restart."""
    from app.errors import ArtifactNeedsRebuildError
    from app.generation_concurrency import PRIORITY_RECOVERY
    from app.production.patch import load_screenplay_from_artifact
    from app.production.screenplay_authority import (
        resolve_current_screenplay_authority,
    )

    conn = get_conn()
    published_rows = conn.execute(
        """SELECT *
             FROM episodes
            WHERE screenplay_artifact_id IS NOT NULL
              AND (
                    screenplay_status='ready'
                    OR (
                        screenplay_status='failed'
                        AND active_screenplay_run_id IS NULL
                    )
                  )"""
    ).fetchall()
    for published in published_rows:
        # episode_prep_pack (screenplay contract 6.0.0+, see
        # app.production.prep_pack) has no narrative_plan and is not the
        # legacy EpisodeScreenplay shape load_screenplay_from_artifact
        # validates. Without this guard, every restart's startup sweep would
        # call into that legacy validator for a freshly-published prep_pack
        # artifact, which is "historically bound" (it has a completion
        # certificate and episode pointers) but has no narrative_plan --
        # _assert_screenplay_artifact_contract raises ArtifactNeedsRebuildError
        # for exactly that combination, flipping a fully valid 'ready'
        # episode to 'failed' on every process restart. Caught via a real
        # EP1 run going from 'ready' to 'failed' immediately after an
        # unrelated backend restart. prep_pack's own completion certificate +
        # coverage ledger already guarantee correctness at publish time and
        # its content is immutable afterward, so this legacy re-validation
        # has nothing meaningful to do for it -- treat as already valid.
        raw_screenplay_json = published["screenplay_json"] if "screenplay_json" in published.keys() else None
        if raw_screenplay_json:
            try:
                parsed_screenplay_json = json.loads(raw_screenplay_json)
            except (TypeError, ValueError):
                parsed_screenplay_json = None
            if (
                isinstance(parsed_screenplay_json, dict)
                and "prep_pack_version" in parsed_screenplay_json
            ):
                continue
        published_artifact_id = str(
            published["screenplay_artifact_id"] or ""
        )
        try:
            if published_artifact_id:
                load_screenplay_from_artifact(published_artifact_id)
            has_immutable_authority = bool(
                published["screenplay_completion_certificate_id"]
                or published["screenplay_production_revision_id"]
            )
            if has_immutable_authority:
                resolve_current_screenplay_authority(
                    str(published["id"]),
                    conn=conn,
                )
            if published["screenplay_status"] == "ready":
                valid = _screenplay_ready(dict(published))
            elif not has_immutable_authority:
                resolve_current_screenplay_authority(
                    str(published["id"]),
                    conn=conn,
                )
                valid = True
            else:
                valid = True
        except ArtifactNeedsRebuildError as exc:
            conn.execute(
                "UPDATE episodes SET screenplay_status='failed',"
                "screenplay_error=?,active_screenplay_run_id=NULL,"
                "screenplay_updated_at=? WHERE id=? "
                "AND screenplay_artifact_id=?",
                (
                    str(exc),
                    now(),
                    published["id"],
                    published_artifact_id,
                ),
            )
            continue
        except Exception:
            valid = False
        if valid:
            if published["screenplay_status"] == "failed":
                conn.execute(
                    "UPDATE episodes SET screenplay_status='ready',"
                    "screenplay_error=NULL,screenplay_updated_at=? "
                    "WHERE id=? AND screenplay_status='failed' "
                    "AND active_screenplay_run_id IS NULL "
                    "AND screenplay_artifact_id=?",
                    (
                        now(),
                        published["id"],
                        published["screenplay_artifact_id"],
                    ),
                )
            continue
        if published["screenplay_status"] != "ready":
            continue
        conn.execute(
            "UPDATE episodes SET screenplay_status='failed',screenplay_error=?,"
            "active_screenplay_run_id=NULL,screenplay_updated_at=? "
            "WHERE id=? AND screenplay_status='ready' "
            "AND screenplay_artifact_id=?",
            (
                "现有完成凭证未通过当前生产门禁；旧剧本与证据已保留，"
                "请重新发起剧本生成",
                now(),
                published["id"],
                published["screenplay_artifact_id"],
            ),
        )
    conn.commit()
    rows = conn.execute(
        """SELECT e.*
             FROM episodes e
            WHERE NOT EXISTS (
                    SELECT 1 FROM projects p -- ALL_OWNERS: startup recovery
                    -- scans every owner's episodes for orphaned running
                    -- screenplay-generation tasks after a process
                    -- reload/restart; excludes soft-deleted (recycle-bin)
                    -- projects so their residual tasks are not resumed and
                    -- do not burn quota
                     WHERE p.id=e.project_id AND p.deleted_at IS NOT NULL
                  )
              AND (
                    (
                        e.screenplay_status IN ('queued','running')
                        AND COALESCE(e.screenplay_error, '') NOT LIKE 'CANCELLING:%'
                        AND NOT EXISTS(
                            SELECT 1 FROM workflow_runs cancelled
                             WHERE cancelled.id=e.active_screenplay_run_id
                               AND cancelled.status IN ('CANCELLED','CANCELLING')
                        )
                      )
                   OR (
                        e.screenplay_status='repairing'
                        AND EXISTS(
                            SELECT 1 FROM workflow_runs wr
                             WHERE wr.id=e.active_screenplay_run_id
                               AND wr.workflow_type='screenplay'
                               AND wr.status='PAUSED_EXTERNAL'
                               AND wr.recovered_by_run_id IS NULL
                        )
                   )
              )"""
    ).fetchall()
    resumed = 0
    for row in rows:
        episode_id = row["id"]
        # Startup recovery deliberately ignores a persisted PAUSED_EXTERNAL
        # owner: there cannot yet be a local worker, and this loop is the code
        # responsible for replacing that interrupted run.
        if task_registry.active("screenplay", episode_id):
            continue
        orphan_run = conn.execute(
            "SELECT status FROM workflow_runs WHERE id=?",
            (row["active_screenplay_run_id"],),
        ).fetchone()
        if orphan_run and orphan_run["status"] == "CREATED":
            try:
                WorkflowRecorder(row["active_screenplay_run_id"]).cancel(
                    "服务重启前尚在排队，已由恢复运行接管", conn=None
                )
            except StateConflict:
                pass
        from app.production.revision import (
            resolve_screenplay_resume_eligibility,
        )

        eligibility = resolve_screenplay_resume_eligibility(
            episode_id,
            conn=conn,
        )
        if not eligibility.resumable:
            conn.execute(
                "UPDATE episodes SET screenplay_status='repairing',screenplay_error=?,"
                "active_screenplay_run_id=NULL,screenplay_updated_at=? "
                "WHERE id=? AND active_screenplay_run_id=?",
                (
                    eligibility.reason,
                    now(),
                    episode_id,
                    row["active_screenplay_run_id"],
                ),
            )
            conn.commit()
            continue
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='screenplay' "
            "AND scope_type='episode' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        batch_parent = conn.execute(
            "SELECT parent.id,parent.status FROM workflow_runs child "
            "JOIN workflow_runs parent ON parent.id=child.parent_run_id "
            "WHERE child.id=? AND parent.workflow_type='screenplay_batch' "
            "AND parent.status IN ('RUNNING','PAUSED_EXTERNAL')",
            (row["active_screenplay_run_id"],),
        ).fetchone()
        batch_run_id = batch_parent["id"] if batch_parent else None
        if batch_parent and batch_parent["status"] == "PAUSED_EXTERNAL":
            try:
                WorkflowRecorder(batch_run_id).start()
            except StateConflict:
                pass
        recorder = None
        try:
            recorder = _new_screenplay_recorder(
                episode_id,
                requested_by="recovery",
                trigger_type="resume",
                parent_run_id=batch_run_id or (parent["id"] if parent else None),
            )
            _spawn_screenplay_activation(
                episode_id,
                recorder,
                project_id=row["project_id"],
                status="queued",
                message=f"{eligibility.label}已排队，等待文本生成槽位",
                preserve_started_at=True,
                expected_active_run_id=row["active_screenplay_run_id"],
                resume_eligibility=eligibility,
                task_factory=lambda episode_id=episode_id, recorder=recorder, batch_run_id=batch_run_id: _screenplay_guarded(
                    episode_id,
                    recorder,
                    priority=PRIORITY_RECOVERY,
                    batch_run_id=batch_run_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - recover remaining episodes independently
            public = errors.record_and_format(
                exc,
                action="screenplay_recovery_spawn",
                context={"episode_id": episode_id, "previous_run_id": row["active_screenplay_run_id"]},
            )
            retry_status = "repairing" if row["screenplay_status"] == "repairing" else "failed"
            retry_hint = (
                "工作副本已保留，请点击「继续剧本流程」"
                if retry_status == "repairing"
                else "原文与约束已保留，请重新发起首版剧本"
            )
            conn.execute(
                "UPDATE episodes SET screenplay_status=?, screenplay_error=?, "
                "active_screenplay_run_id=NULL, screenplay_updated_at=? "
                "WHERE id=? AND active_screenplay_run_id=?",
                (
                    retry_status,
                    f"服务重启后的自动恢复未能启动；{retry_hint}。{public}",
                    now(),
                    episode_id,
                    row["active_screenplay_run_id"],
                ),
            )
            conn.commit()
            continue
        resumed += 1
    return resumed
