"""Unified startup reconciliation for every durable background workflow."""
from __future__ import annotations

import fcntl
from pathlib import Path
import time
from typing import Any

from app.config import DATA_DIR


_last_report: dict[str, Any] = {}
_runtime_lock_handle = None


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
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + max(float(wait_timeout_s), 0.0)
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
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
        recover_scene_review_tasks,
        recover_scene_ref_tasks,
        recover_scene_view_redo_tasks,
        recover_screenplay_tasks,
        recover_storyboard_tasks,
    )
    from app.planning import recover_plan_tasks
    from app.orchestration.api import recover_delivery_tasks

    report: dict[str, Any] = {
        "media": worker.recover_media_jobs(),
        "abandoned_partial_files_removed": cleanup_abandoned_parts(PROJECTS_DIR),
    }
    worker.recover_and_start()
    worker.start_stale_lease_sweeper()

    report["character_bible"] = recover_bible_tasks()
    report["character_references"] = recover_character_ref_tasks()
    report["portrait_view_redo"] = recover_portrait_view_redo_tasks()
    report["scene_references"] = recover_scene_ref_tasks()
    scene_view_redo_resumed = recover_scene_view_redo_tasks()
    if scene_view_redo_resumed:
        report["scene_view_redo"] = scene_view_redo_resumed
    scene_review_resumed = recover_scene_review_tasks()
    if scene_review_resumed:
        report["scene_history_review"] = scene_review_resumed
    report["episode_mapping"] = recover_plan_tasks()
    report["screenplay"] = recover_screenplay_tasks()
    report["storyboard"] = recover_storyboard_tasks()
    try:
        from app.video_supervisor import recover_video_completion_runs
        report["video_completion"] = recover_video_completion_runs()
    except Exception as exc:  # noqa: BLE001
        report["video_completion"] = {"error": str(exc)}
    report["delivery"] = recover_delivery_tasks()

    global _last_report
    _last_report = report
    return dict(report)


def last_report() -> dict[str, Any]:
    return dict(_last_report)
