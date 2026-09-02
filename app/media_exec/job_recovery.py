"""启动期与周期性的媒体作业恢复/对账（拆分自 ``run_job.py``）。

``recover_and_start`` 是进程启动时调用一次的入口：清空内存队列、拉起
worker 池与调度器（``.worker_lifecycle``/``.dispatch``，2026-08-30 从前者拆出
后者承接持久派发部分，见 ``.dispatch`` 模块 docstring）。``recover_media_jobs``/
``_recover_one_media_job`` 处理进程重启后遗留的 in-flight job；
``reconcile_stalled_video_jobs``/``_block_orphaned_continuity_job`` 是
``_stale_lease_sweeper``（``.worker_lifecycle``）周期性调用的对账，找出租约过
期、连续性锚点缺失或被单镜拖死的 job 并纠正。``recover_and_start`` 需要
``.worker_lifecycle`` 的两个名字（``_drain_memory_queue``/``ensure_workers``）
与 ``.dispatch`` 的两个名字（``_dispatch_due_jobs``/``_start_durable_dispatcher``），
而 ``.worker_lifecycle`` 自己的 ``_stale_lease_sweeper`` 又需要本文件的恢复函
数——真正的双向依赖，用惰性（函数体内部）导入在本文件这一侧打破，
``.worker_lifecycle`` 侧保持顶层导入（做法与 ``.enqueue``/``.run_job`` 的既有
惰性导入一致）；``.dispatch`` 与本文件之间没有反向依赖，不需要同样处理。
"""

from __future__ import annotations

import json

from app import config, errors
from app.db import get_conn, new_id, now, rows_to_dicts
from app.orchestration import media_scheduler

from .common import _poll_queue, _queue, _video_ready_queue
from .enqueue import (
    enqueue_shot,
    reconcile_episode_generation_status,
    recover_equivalent_stale_provider_jobs,
)
from .legacy_keyframes import decommission_legacy_keyframe_jobs


def recover_and_start(loop_concurrency: int | None = None) -> None:
    """启动时恢复队列（PRD §4.5 验收：中途杀进程重启后队列状态可恢复）。"""
    from app.media_pipeline.bootstrap import start_media_pipeline
    from .worker_lifecycle import _drain_memory_queue, ensure_workers
    from .dispatch import _dispatch_due_jobs, _start_durable_dispatcher

    start_media_pipeline()
    decommission_legacy_keyframe_jobs()
    # Reconcile expired durable leases, then rebuild scheduling exclusively from
    # DB state. Startup recovery may have pre-enqueued dozens of duplicate IDs;
    # discarding those in-memory copies is safe because jobs are durable.
    media_scheduler.recoverable_jobs()
    _drain_memory_queue(_queue)
    _drain_memory_queue(_video_ready_queue)
    _drain_memory_queue(_poll_queue)
    conn = get_conn()
    stale_provider_episode_ids = [
        row["episode_id"]
        for row in conn.execute(
            """SELECT DISTINCT episode_id FROM jobs
                WHERE kind='video' AND status='stale'
                  AND provider_non_cancellable=1
                  AND cancellation_requested=0 AND abandoned=0"""
        ).fetchall()
    ]
    for episode_id in stale_provider_episode_ids:
        recover_equivalent_stale_provider_jobs(episode_id)
    generating_episode_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM episodes WHERE status='generating'"
        ).fetchall()
    ]
    for episode_id in generating_episode_ids:
        reconcile_episode_generation_status(episode_id)
    # 启动时按通道分别取并发，不再用 max(submit, reference) 混成一个池
    n = loop_concurrency  # 若显式传入，仍作为参考图 worker 目标
    ensure_workers(n)
    _start_durable_dispatcher()
    _dispatch_due_jobs()


def _recover_one_media_job(
    conn, job_id: str, run_id: str | None, step_run_id: str | None, reason: str
) -> bool:
    """把一个卡住的媒体 job 复位给持久调度器：
    - accepted provider task 回到 waiting_provider，其他任务回到 queued；
      provider_task_id、轮询责任与持久化 retry 到期时间保留
    - Run 立即进入 WAITING_RETRY，监控页显示“恢复排队中”
    - 被中断的 Step 保持 FAILED 审计终态，并创建 iteration+1 的 READY attempt
    返回 True 表示实际复位过；False 表示 job 已不存在或被并发改动（调用方忽略）。"""
    cursor = conn.execute(
        "UPDATE jobs SET status=CASE WHEN provider_poll_required=1 "
        "THEN 'waiting_provider' ELSE 'queued' END, "
        "lease_owner=NULL, lease_expires_at=NULL, "
        "error=NULL, updated_at=? "
        "WHERE id=? AND status IN ('running','queued','waiting_provider') "
        "AND cancellation_requested=0 AND abandoned=0",
        (now(), job_id),
    )
    if cursor.rowcount != 1:
        return False
    try:
        from app.orchestration.state_machine import transition_run, transition_step

        run = conn.execute(
            "SELECT status FROM workflow_runs WHERE id=?", (run_id,)
        ).fetchone() if run_id else None
        if run and run["status"] in {"RUNNING", "PAUSED_EXTERNAL"}:
            transition_run(
                run_id, run["status"], "WAITING_RETRY", reason,
                failure_code=(
                    "SERVICE_RESTART" if run["status"] == "PAUSED_EXTERNAL" else "LEASE_EXPIRED"
                ),
                conn=conn,
            )
        old_step = conn.execute(
            "SELECT * FROM step_runs WHERE id=?", (step_run_id,)
        ).fetchone() if step_run_id else None
        if old_step:
            previous_status = old_step["status"]
            if previous_status == "RUNNING":
                transition_step(
                    step_run_id, "RUNNING", "FAILED", reason,
                    decision="retry", error_code="LEASE_EXPIRED", conn=conn,
                )
            if previous_status in {"RUNNING", "FAILED"}:
                iteration = conn.execute(
                    "SELECT COALESCE(MAX(iteration_no),0)+1 AS n FROM step_runs "
                    "WHERE run_id=? AND step_key=?",
                    (run_id, old_step["step_key"]),
                ).fetchone()["n"]
                new_step_id = new_id("step")
                conn.execute(
                    """INSERT INTO step_runs(
                           id, run_id, step_key, iteration_no, parent_step_run_id, status,
                           agent_name, contract_version, prompt_version, policy_version,
                           input_artifact_ids_json, context_manifest_json
                       ) VALUES(?,?,?,?,?,'PENDING',?,?,?,?,?,?)""",
                    (
                        new_step_id, run_id, old_step["step_key"], int(iteration), step_run_id,
                        old_step["agent_name"], old_step["contract_version"],
                        old_step["prompt_version"], old_step["policy_version"],
                        old_step["input_artifact_ids_json"] or "[]",
                        old_step["context_manifest_json"] or "{}",
                    ),
                )
                transition_step(new_step_id, "PENDING", "READY", reason, conn=conn)
                conn.execute(
                    "UPDATE jobs SET step_run_id=? WHERE id=?", (new_step_id, job_id)
                )
                conn.execute(
                    "INSERT INTO run_events(id, run_id, step_run_id, ts, event_type, severity, "
                    "message, payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_id("evt"), run_id, new_step_id, now(), "MEDIA_RECOVERY_QUEUED",
                        "warning", reason,
                        json.dumps(
                            {"job_id": job_id, "previous_step_run_id": step_run_id},
                            ensure_ascii=False,
                        ),
                    ),
                )
    except Exception:  # noqa: BLE001 legacy/minimal schemas still recover the durable job itself
        pass
    # The durable dispatcher will see this row within one second. Avoid directly
    # flooding the FIFO when startup/sweeper recovers an entire episode.
    return True


def recover_media_jobs() -> int:
    """启动时恢复因服务重启被中断的媒体任务。

    init_db() 在重启时把所有 status='RUNNING' 的 workflow_runs 标为 PAUSED_EXTERNAL +
    failure_code='SERVICE_RESTART'，同时把对应 step_runs 标 FAILED；但底层 jobs 表的
    lease（默认 180s）在重启那一刻往往还没过期，media_scheduler.recoverable_jobs()
    只扫 status='running' AND lease_expires_at<now 的 job，因此不会重新入队——
    结果就是用户看到的"任务卡在'服务重启，可从安全检查点恢复'"。

    本函数把这些 job 显式复位回 queued；数据库驱动的持久调度器会在下一轮重新
    发现它们。run 从 PAUSED_EXTERNAL 转回 WAITING_RETRY，旧 FAILED step 保留为
    审计历史，并创建 iteration+1 的 READY step 供 worker 接管。

    边界：不恢复 FAILED/CANCELLED（真正报错或人工取消）。历史上这里还排除
    PAUSED_BUDGET（预算不足，需显式 retry_paused 释放预算后重试）——成本预算
    拦截体系退场（2026-09-01）后，``reconcile_stalled_video_jobs`` 的周期扫描
    已经把 paused_budget 当可继续状态自动恢复，不再要求用户显式操作，本函数
    这里不需要重复处理。"""
    media_scheduler.reconcile_cancelled_version_states()
    decommission_legacy_keyframe_jobs()
    conn = get_conn()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id AS job_id, j.run_id, j.step_run_id
           FROM jobs j
           JOIN workflow_runs wr ON wr.id=j.run_id
           WHERE j.status IN ('running','queued','waiting_provider')
             AND wr.status='PAUSED_EXTERNAL'
             AND wr.failure_code='SERVICE_RESTART'
             AND j.cancellation_requested=0
             AND j.abandoned=0
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS: startup recovery
               -- scans every owner's media jobs for orphaned running
               -- generation tasks after a process reload/restart; excludes
               -- soft-deleted (recycle-bin) projects so their residual jobs
               -- are not re-armed and do not burn quota
                WHERE p.id=j.project_id AND p.deleted_at IS NOT NULL
             )""",
    ))
    resumed = 0
    for r in rows:
        if _recover_one_media_job(
            conn, r["job_id"], r["run_id"], r["step_run_id"], "服务重启后自动恢复任务"
        ):
            resumed += 1
    conn.commit()
    try:
        reconcile_stalled_video_jobs()
    except Exception as exc:  # noqa: BLE001 启动恢复各子域隔离，媒体 lease 恢复仍需成功
        errors.record_and_format(
            exc,
            action="startup_recovery.media_stalls",
            context={"resumed_media_jobs": resumed},
        )
    return resumed


def _block_orphaned_continuity_job(conn, row) -> bool:
    """Keep the planned dependency and surface repair instead of degrading."""
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.scheduler import continuity_anchor_ready
    from app.media_pipeline.stage_state import set_pipeline_stage

    after_shot_id = row["after_shot_id"]
    if not after_shot_id or not row["version_id"]:
        return False
    ready, reason = continuity_anchor_ready(conn, after_shot_id)
    if ready:
        return False
    # 上游还有活跃任务时继续等；只处理已明确需要人工或上游已不存在的孤儿链。
    if "人工" not in str(reason or "") and "不存在" not in str(reason or ""):
        return False

    version = conn.execute(
        "SELECT * FROM shot_versions WHERE id=?", (row["version_id"],)
    ).fetchone()
    if version is None:
        return False

    try:
        planned_meta = json.loads(version["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        planned_meta = {}
    shot_plan_id = str(planned_meta.get("shot_plan_id") or "")
    if shot_plan_id:
        message = (
            "计划依赖的上一镜采用视频或真实尾帧当前不可恢复；"
            "本镜保持原生成模式等待修复，系统未改用其他模式。"
        )
        conn.execute(
            """UPDATE jobs
                  SET status='waiting_human',error=?,
                      reason_code='VIDEO_PLAN_DEPENDENCY_REPAIR_REQUIRED',
                      reason_text=?,next_retry_at=NULL,video_slot_active=0,updated_at=?
                WHERE id=?""",
            (message, message, now(), row["id"]),
        )
        conn.execute(
            "UPDATE shot_versions SET status='waiting_human',error=?,video_slot_active=0 WHERE id=?",
            (message, row["version_id"]),
        )
        set_pipeline_stage(
            row["id"],
            media_stages.STAGE_WAITING_HUMAN,
            reason_code="VIDEO_PLAN_DEPENDENCY_REPAIR_REQUIRED",
            reason_text=message,
            conn=conn,
        )
        conn.commit()
        return True

    message = (
        "历史连续性任务缺少可恢复的上一镜尾帧；系统未改写提示词、"
        "未移除依赖，也未切换生成模式。请修复上游镜头后重新生成。"
    )
    conn.execute(
        """UPDATE jobs
              SET status='waiting_human',error=?,
                  reason_code='VIDEO_DEPENDENCY_REPAIR_REQUIRED',
                  reason_text=?,next_retry_at=NULL,video_slot_active=0,updated_at=?
            WHERE id=?""",
        (message, message, now(), row["id"]),
    )
    conn.execute(
        "UPDATE shot_versions SET status='waiting_human',error=?,video_slot_active=0 WHERE id=?",
        (message, row["version_id"]),
    )
    set_pipeline_stage(
        row["id"],
        media_stages.STAGE_WAITING_HUMAN,
        reason_code="VIDEO_DEPENDENCY_REPAIR_REQUIRED",
        reason_text=message,
        conn=conn,
    )
    conn.commit()
    return True


def _resume_budget_paused_episodes(conn, limit: int) -> int:
    """Auto-resume every ``paused_budget`` job (退场后不再是人工闸门，见调用方)。"""
    episode_ids = [
        str(row["id"])
        for row in conn.execute(
            """SELECT DISTINCT j.episode_id AS id FROM jobs j
                WHERE j.kind='video' AND j.status='paused_budget'
                  AND j.episode_id IS NOT NULL
                  AND NOT EXISTS ( -- ALL_OWNERS, excludes recycle-bin projects
                    SELECT 1 FROM projects p WHERE p.id=j.project_id AND p.deleted_at IS NOT NULL
                  )
                ORDER BY j.episode_id LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    ]
    if not episode_ids:
        return 0
    from .retry_scheduling import retry_paused
    resumed = 0
    for episode_id in episode_ids:
        try:
            resumed += retry_paused(episode_id)
        except Exception:
            continue
    return resumed


def reconcile_stalled_video_jobs(limit: int = 50) -> dict[str, int]:
    """周期修复没有 worker 能消费的业务级卡死状态。

    ``paused_budget`` 历史上是"预算不足，需要用户在页面上显式提额"的意图性
    人工闸门，不当成卡死状态自动恢复。成本预算拦截体系退场（2026-09-01）
    后，那个"用户必须显式提额"的前提本身已经不成立——``reserve_budget``/
    ``reserve_provider_video_budget`` 都不再据金额拒绝，旧状态行不会再有
    "预算不足"这个真实原因，继续要求人工点一下"恢复"才能重新排队，纯粹是
    为已废止概念服务的拦路石（见 CLAUDE.md「Retiring Features」）。这里改为
    与其它卡死状态一样，周期扫描自动把它们判定为可继续。
    """
    from app.observability.metrics import inc

    conn = get_conn()
    stamp = now()
    report = dict.fromkeys((
        "redundant_preflight_closed", "legacy_jobless_recovered",
        "legacy_preflight_reactivated", "preflight_retried",
        "continuity_degraded", "dependency_repair_required",
        "budget_resumed", "quarantine_released", "episodes_reconciled",
    ), 0)

    redundant = conn.execute(
        """UPDATE jobs
           SET status='cancelled', cancellation_requested=1,
               reason_code='SUPERSEDED_PREFLIGHT',
               reason_text='已有成功采用版，关闭并发产生的冗余校验任务',
               error='已有成功采用版，关闭并发产生的冗余校验任务',
               next_retry_at=NULL, stage_status='complete', updated_at=?
           WHERE kind='video' AND version_id IS NULL
             AND status IN ('waiting_retry','waiting_human')
             AND cancellation_requested=0 AND abandoned=0
             AND EXISTS (
               SELECT 1
               FROM shots s
               JOIN shot_versions v ON v.id=s.adopted_version_id
               WHERE s.id=jobs.shot_id AND v.status='succeeded'
             )""",
        (stamp,),
    ).rowcount
    # SQLite starts a write transaction even when UPDATE affects zero rows.
    # Release it before the read-heavy reconciliation passes below.
    conn.commit()
    if redundant:
        report["redundant_preflight_closed"] = int(redundant)

    # 兼容修复上线前的历史事故：当时 preflight 发生在 jobs INSERT 之前，
    # 因而只留下 issue artifact。仅恢复 24 小时内、整集仍处于 generating、
    # 且从未创建过版本或任务的明确 VIDEO_PREFLIGHT_BLOCKED 镜头。
    legacy_rows = rows_to_dicts(conn.execute(
        """SELECT a.scope_id AS shot_id, a.content_json
           FROM artifacts a
           JOIN shots s ON s.id=a.scope_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE a.type='video_shot_issue'
             AND a.scope_type='shot'
             AND a.status IN ('candidate','validated','approved')
             AND a.created_at>=?
             AND e.status='generating'
             AND NOT EXISTS (
               SELECT 1 FROM shot_versions v WHERE v.shot_id=s.id
             )
             AND NOT EXISTS (
               SELECT 1 FROM jobs j WHERE j.shot_id=s.id AND j.kind='video'
             )
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS: periodic startup
               -- sweep re-enqueues legacy jobless preflight-blocked shots
               -- across every owner; excludes soft-deleted (recycle-bin)
               -- projects so their shots are not re-enqueued and do not
               -- burn quota
                WHERE p.id=e.project_id AND p.deleted_at IS NOT NULL
             )
           ORDER BY a.created_at DESC LIMIT ?""",
        (stamp - 86400.0, max(1, int(limit))),
    ))
    seen_legacy: set[str] = set()
    for row in legacy_rows:
        shot_id = row["shot_id"]
        if shot_id in seen_legacy:
            continue
        seen_legacy.add(shot_id)
        try:
            payload = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        codes = {
            str(item.get("code") or "")
            for item in (payload.get("issues") or [])
            if isinstance(item, dict)
        }
        if "VIDEO_PREFLIGHT_BLOCKED" not in codes:
            continue
        try:
            enqueue_shot(shot_id)
        except Exception:
            # 新版 enqueue 已经把失败镜头纳入可见任务状态。
            pass
        if conn.execute(
            "SELECT 1 FROM jobs WHERE shot_id=? AND kind='video' LIMIT 1",
            (shot_id,),
        ).fetchone():
            report["legacy_jobless_recovered"] += 1

    preflight_rows = rows_to_dicts(conn.execute(
        """SELECT id, shot_id, status FROM jobs
           WHERE kind='video' AND version_id IS NULL
             AND status='waiting_retry'
             AND (next_retry_at IS NULL OR next_retry_at<=?)
             AND cancellation_requested=0 AND abandoned=0
             AND NOT EXISTS (
               SELECT 1 FROM projects p -- ALL_OWNERS: periodic sweep retries
               -- stalled video preflight jobs across every owner; excludes
               -- soft-deleted (recycle-bin) projects so their jobs are not
               -- retried and do not burn quota
                WHERE p.id=jobs.project_id AND p.deleted_at IS NOT NULL
             )
           ORDER BY updated_at LIMIT ?""",
        (stamp, max(1, int(limit))),
    ))
    for row in preflight_rows:
        try:
            result = enqueue_shot(row["shot_id"])
            if result.get("task_accepted") or result.get("reused"):
                report["preflight_retried"] += 1
                if row["status"] == "waiting_human":
                    report["legacy_preflight_reactivated"] += 1
        except Exception:
            # enqueue_shot 已持久化新的 retry / waiting_human 状态。
            continue

    cutoff = stamp - float(config.VIDEO_CONTINUITY_ORPHAN_TIMEOUT)
    continuity_rows = rows_to_dicts(conn.execute(
        """SELECT id, shot_id, version_id, episode_id, project_id, after_shot_id
           FROM jobs
           WHERE kind='video' AND version_id IS NOT NULL
             AND pipeline_stage IN ('waiting_continuity_anchor','waiting_dependency')
             AND status IN ('queued','waiting_retry','waiting_human')
             AND COALESCE(stage_started_at, updated_at, created_at)<=?
             AND cancellation_requested=0 AND abandoned=0
           ORDER BY COALESCE(stage_started_at, updated_at, created_at)
           LIMIT ?""",
        (cutoff, max(1, int(limit))),
    ))
    for row in continuity_rows:
        try:
            if _block_orphaned_continuity_job(conn, row):
                report["dependency_repair_required"] += 1
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    report["budget_resumed"] = _resume_budget_paused_episodes(conn, limit)
    from .quarantine_release import release_orphan_quarantined_versions
    report["quarantine_released"] = release_orphan_quarantined_versions(conn, limit)
    episode_rows = conn.execute(
        "SELECT id FROM episodes WHERE status='generating'"
    ).fetchall()
    for row in episode_rows:
        try:
            if reconcile_episode_generation_status(row["id"]):
                report["episodes_reconciled"] += 1
        except Exception:
            continue
    total = sum(report.values())
    if total:
        inc("video_stall_sweeper_repairs_total", value=total, **report)
    return report

__all__ = [name for name in globals() if not name.startswith("__")]
