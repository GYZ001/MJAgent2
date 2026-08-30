"""Unified startup reconciliation for every durable background workflow."""
from __future__ import annotations

import asyncio

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt
from pathlib import Path
import time
from typing import Any

from app.config import DATA_DIR


_last_report: dict[str, Any] = {}
_runtime_lock_handle = None


def _try_lock(handle) -> None:
    handle.seek(0)
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise BlockingIOError from exc


def _unlock(handle) -> None:
    handle.seek(0)
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def acquire_runtime_recovery_lock(
    path: Path | None = None,
    *,
    wait_timeout_s: float = 0.0,
) -> bool:
    """为共用同一 SQLite 的服务实例选出唯一恢复协调者。

    锁在整个 lifespan 内持有；进程崩溃时操作系统会立即释放。
    旁路端口/健康检查实例可以启动，但不得把主实例的活跃任务当成重启遗留任务。
    """
    global _runtime_lock_handle
    if _runtime_lock_handle is not None:
        return True
    lock_path = path or (DATA_DIR / ".runtime-recovery.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    deadline = time.monotonic() + max(float(wait_timeout_s), 0.0)
    while True:
        try:
            _try_lock(handle)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                return False
            # uvicorn --reload 会先启新 worker、再等旧 worker 退出。
            # 给旧 worker 一个有界的释放窗口，避免新 worker 永久变成被动实例。
            time.sleep(0.05)
    _runtime_lock_handle = handle
    return True


def release_runtime_recovery_lock() -> None:
    global _runtime_lock_handle
    handle = _runtime_lock_handle
    _runtime_lock_handle = None
    if handle is None:
        return
    try:
        _unlock(handle)
    finally:
        handle.close()


def record_passive_instance() -> None:
    global _last_report
    _last_report = {
        "skipped": True,
        "reason": "已有共享数据库的活跃服务实例；本实例未执行启动恢复",
    }


async def recover_all() -> dict[str, Any]:
    """Reconcile persisted state before accepting traffic.

    Each durable workflow is reconciled from its persisted state before the
    service begins accepting traffic.
    """
    from app import worker
    from app.atomic_io import cleanup_abandoned_parts
    from app.config import PROJECTS_DIR
    from app.api import (
        recover_bible_tasks,
        recover_character_ref_tasks,
        recover_portrait_view_redo_tasks,
        recover_project_video_completion_queues,
        recover_scene_ref_tasks,
        recover_scene_view_redo_tasks,
        recover_screenplay_tasks,
        recover_storyboard_tasks,
    )
    from app.planning import recover_plan_tasks
    from app.orchestration.api import recover_delivery_tasks

    started_at = time.time()
    started_clock = time.monotonic()
    report: dict[str, Any] = {}

    def run_step(key: str, operation, *, record_empty: bool = True):
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001
            from app.errors import log_error
            rec = log_error(
                exc,
                action=f"startup_recovery.{key}",
                context={"step": key},
                meta={"stage": "startup_recovery", "isolation": "step"},
            )
            report[key] = {
                "error": str(exc),
                "error_id": rec.error_id,
                "exc_type": type(exc).__name__,
            }
            return None
        if record_empty or result:
            report[key] = result
        return result

    from app import db
    run_step(
        "startup_business_status_repair",
        lambda: _repair_startup_business_status(db.get_conn()),
    )
    run_step("media", worker.recover_media_jobs)
    from app.artifacts import flush_pending_media_cleanup
    run_step("media_cleanup_outbox", flush_pending_media_cleanup)
    run_step(
        "abandoned_partial_files_removed",
        lambda: cleanup_abandoned_parts(PROJECTS_DIR),
    )
    from app.rejected_media import purge_rejected_media
    run_step("rejected_media_purged", purge_rejected_media)
    run_step("media_dispatcher", worker.recover_and_start, record_empty=False)
    run_step("stale_lease_sweeper", worker.start_stale_lease_sweeper, record_empty=False)

    run_step("character_bible", recover_bible_tasks)
    run_step("character_references", recover_character_ref_tasks)
    run_step("portrait_view_redo", recover_portrait_view_redo_tasks)
    run_step("scene_references", recover_scene_ref_tasks)
    scene_view_redo_resumed = run_step(
        "scene_view_redo", recover_scene_view_redo_tasks, record_empty=False,
    )
    if scene_view_redo_resumed:
        report["scene_view_redo"] = scene_view_redo_resumed
    run_step("episode_mapping", recover_plan_tasks)
    run_step("screenplay", recover_screenplay_tasks)
    run_step("storyboard", recover_storyboard_tasks)

    def recover_video_completion():
        from app.video_supervisor import recover_video_completion_runs
        return recover_video_completion_runs()

    run_step("video_completion", recover_video_completion)
    run_step("project_video_completion", recover_project_video_completion_queues)
    run_step("delivery", recover_delivery_tasks)
    finished_at = time.time()
    report["recovery_meta"] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": round((time.monotonic() - started_clock) * 1000),
        "failed_steps": [
            key for key, value in report.items()
            if isinstance(value, dict) and value.get("error_id")
        ],
    }

    global _last_report
    _last_report = report
    return dict(report)


def _repair_legacy_screenplay_warning_status(conn) -> int:
    """剧本 warning 终态已移除：有工作副本的继续 Repair，其余旧候选明确标为失败。

    搬自 ``app.db.init_db()``（P0-3 依赖反转，见
    ``docs/coupling_review_2026-08-29.md`` 第2步）：这是业务状态重写，不是
    schema 初始化，不该挂在纯 schema 初始化路径（CLI、测试的 ``init_db()``）
    上顺带执行——只有真正的启动恢复（本函数的调用方 ``recover_all()``，
    ``recovery_owner`` 独占）才该碰业务状态。事务边界由调用方
    ``_repair_startup_business_status`` 统一提交/回滚，本函数不自行 commit。
    """
    cur = conn.execute(
        """UPDATE episodes
              SET screenplay_status=CASE
                    WHEN COALESCE(working_screenplay_artifact_id, '') != '' THEN 'repairing'
                    ELSE 'failed'
                  END,
                  screenplay_error=COALESCE(
                    screenplay_error,
                    '旧版 warning 候选未取得 QA 通过凭证，请继续修复或重新生成'
                  ),
                  screenplay_snapshot_version=screenplay_snapshot_version+1
            WHERE screenplay_status='warning'"""
    )
    return int(cur.rowcount or 0)


def _repair_misclassified_scene_refs_status(conn) -> int:
    """旧版把"候选已生成但缺新版 QA 证据"误归类为 ProviderError，并在项目页显示为
    大模型故障。保留历史 run 原貌供审计，只修正项目当前态与面向用户的恢复指引。

    搬自 ``app.db.init_db()``，理由与 ``_repair_legacy_screenplay_warning_status``
    相同：业务状态重写，只属于启动恢复，不属于 schema 初始化。事务边界同上，
    由调用方统一提交/回滚。
    """
    cur = conn.execute(
        """UPDATE projects
           SET scene_refs_status='warning',
               scene_refs_error=(
                 SELECT '历史任务已更正分类：图片候选已生成，但缺少新版 QA 证据。'
                        || '请进入候选页重新验 QA，或对未验证候选执行人工复核；系统不会继续重复出图。原始诊断：'
                        || substr(r.failure_message, 1, 700)
                   FROM workflow_runs r
                  WHERE r.workflow_type='scene_references'
                    AND r.scope_type='project'
                    AND r.scope_id=projects.id
                  ORDER BY r.updated_at DESC LIMIT 1
               )
         WHERE scene_refs_status='failed'
           AND EXISTS (
             SELECT 1 FROM workflow_runs r
              WHERE r.workflow_type='scene_references'
                AND r.scope_type='project'
                AND r.scope_id=projects.id
                AND r.id=(
                  SELECT latest.id FROM workflow_runs latest
                   WHERE latest.workflow_type='scene_references'
                     AND latest.scope_type='project'
                     AND latest.scope_id=projects.id
                   ORDER BY latest.updated_at DESC LIMIT 1
                )
                AND r.failure_message LIKE '%候选缺少可用的新版%'
           )"""
    )
    return int(cur.rowcount or 0)


def _repair_startup_business_status(conn) -> dict[str, int]:
    """跑完两条搬自 ``app.db.init_db()`` 的业务状态修复，独占提交这次改动。

    ``conn`` 是调用方（``recover_all()``，经由 ``app.db.get_conn()``）持有的
    连接；本函数是这次改动的事务边界所有者：成功统一 commit，任何一步失败
    先 rollback 再上抛——rollback 必须是异常处理器的第一条语句，排在
    ``run_step`` 后续可能触发的 ``log_error`` 落库之前，避免把这两条 UPDATE
    的半改状态和 error_logs 的写入混进同一次隐式提交。
    """
    try:
        screenplay_rewritten = _repair_legacy_screenplay_warning_status(conn)
        scene_refs_rewritten = _repair_misclassified_scene_refs_status(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "screenplay_warning_rewritten": screenplay_rewritten,
        "scene_refs_misclassification_rewritten": scene_refs_rewritten,
    }


def last_report() -> dict[str, Any]:
    return dict(_last_report)


async def project_recycle_bin_sweep_loop(interval_s: float = 300.0) -> None:
    """周期性彻底清理已过 24 小时保留期的软删除项目。

    与 ``app.video_supervisor.watchdog.video_supervisor_watchdog_loop`` 同一种
    调度形态（``task_registry.spawn`` 起一个常驻协程，自身 ``while True`` +
    ``asyncio.sleep``），刻意不引入 APScheduler 之类的新依赖——这是本仓已有的
    唯一周期任务机制，见 app/main.py::lifespan。判据挂在 ``deleted_at`` 时间戳
    上（见 app.domain.projects.sweep_expired_deleted_projects），不挂在这个循环
    的运行时长上：无论后端重启多少次、这个循环停跑了多久，只要时间戳过期，
    下一次巡检就会清理；单个项目清理失败（例如供应商任务未到终态）不影响
    其余到期项目，也不会让循环本身退出。
    """
    from app.domain.projects import sweep_expired_deleted_projects
    while True:
        try:
            await sweep_expired_deleted_projects()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 巡检循环自身不得因单个项目坏数据退出
            from app.errors import log_error
            log_error(
                exc,
                action="project_recycle_bin_sweep_loop",
                context={"interval_s": interval_s},
                meta={"stage": "recycle_bin_sweep", "isolation": "loop"},
            )
        await asyncio.sleep(max(60.0, min(float(interval_s), 900.0)))


async def account_recycle_bin_sweep_loop(interval_s: float = 300.0) -> None:
    """周期性彻底清理已过 30 天保留期的软删除账号（管理员删账号路径）。

    与 ``project_recycle_bin_sweep_loop`` 同一种调度形态与同一条判据风格——挂
    ``users.deleted_at`` 时间戳，不挂内存计时器；见
    ``app.domain.account_deletion.sweep_expired_deleted_accounts``。单个账号
    清理失败（例如级联项目里有供应商任务未到终态）不影响其余到期账号，也不会
    让循环本身退出。
    """
    from app.domain.account_deletion import sweep_expired_deleted_accounts
    while True:
        try:
            await sweep_expired_deleted_accounts()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 巡检循环自身不得因单个账号坏数据退出
            from app.errors import log_error
            log_error(
                exc,
                action="account_recycle_bin_sweep_loop",
                context={"interval_s": interval_s},
                meta={"stage": "account_recycle_bin_sweep", "isolation": "loop"},
            )
        await asyncio.sleep(max(60.0, min(float(interval_s), 900.0)))


async def monitor_audit_flush_loop(interval_s: float = 60.0) -> None:
    """定期把 ``monitor_audit`` 本地缓冲（写锁竞争导致的失败兜底）补写回库。

    与其余巡检循环同一种调度形态。``app.monitor_audit_buffer.flush()`` 是同步
    的文件锁 + SQLite 写，丢进线程池执行，避免阻塞事件循环；单轮失败（例如
    这一刻写锁仍被别的事务占着）不影响下一轮，也不会让循环本身退出——见
    ``app.monitor_audit_buffer`` 模块文档。
    """
    from app.monitor_audit_buffer import flush as flush_pending_audit
    while True:
        try:
            await asyncio.to_thread(flush_pending_audit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 巡检循环自身不得因单轮补写失败退出
            from app.errors import log_error
            log_error(
                exc,
                action="monitor_audit_flush_loop",
                context={"interval_s": interval_s},
                meta={"stage": "monitor_audit_flush", "isolation": "loop"},
            )
        await asyncio.sleep(max(60.0, min(float(interval_s), 900.0)))
