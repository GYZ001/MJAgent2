"""Unified startup reconciliation for every durable background workflow."""
from __future__ import annotations

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


def last_report() -> dict[str, Any]:
    return dict(_last_report)
