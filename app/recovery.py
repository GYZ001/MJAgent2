"""Unified startup reconciliation for every durable background workflow."""
from __future__ import annotations

import asyncio
from typing import Any


_last_report: dict[str, Any] = {}


async def recover_all() -> dict[str, Any]:
    """Reconcile persisted state before accepting traffic.

    Parent project DAGs are resumed before standalone child stages.  Child
    adapters skip scopes owned by an active auto pipeline, preventing a restart
    from spawning the same work both inline and independently.
    """
    from app import auto, worker
    from app.atomic_io import cleanup_abandoned_parts
    from app.config import PROJECTS_DIR
    from app.api import (
        recover_bible_tasks,
        recover_character_ref_tasks,
        recover_scene_ref_tasks,
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

    report["auto_project"] = auto.recover_auto_tasks()
    # Let newly spawned parent tasks register aliases before child adapters scan.
    await asyncio.sleep(0)
    report["character_bible"] = recover_bible_tasks()
    report["character_references"] = recover_character_ref_tasks()
    report["scene_references"] = recover_scene_ref_tasks()
    report["episode_mapping"] = recover_plan_tasks()
    report["screenplay"] = recover_screenplay_tasks()
    report["storyboard"] = recover_storyboard_tasks()
    report["delivery"] = recover_delivery_tasks()

    global _last_report
    _last_report = report
    return dict(report)


def last_report() -> dict[str, Any]:
    return dict(_last_report)
