"""媒体执行共享导言与队列状态。

后续 media_exec 切片通过 ``exec`` 注入同一命名空间，因此这里的 import 看似未使用，
实际供 enqueue/run_job/concat 等切片复用。勿用 ruff 自动删 import。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app import config, errors, hiagent, video_modes
from app.atomic_io import atomic_copy, atomic_write_bytes
from app.artifacts import (_adopted_video_paths, _invalidate_final_video,
                           clear_episode_artifacts, clear_episode_video_assets,
                           clear_shot_artifacts, clear_shot_reference_assets,
                           clear_shot_video_assets,
                           delete_episode_shots, delete_project_episodes,
                           delete_video_version, invalidate_episode_final,
                           invalidate_shot_video_derivatives,
                           purge_character_video_artifacts,
                           purge_project_video_artifacts, purge_shot_videos)
from app.compiler import ensure_source_excerpt_in_prompt, idem_key as make_idem_key, shot_cost_cny
from app.completion_grant import VideoBudgetAuthorizationError
from app.db import (
    get_conn,
    get_setting,
    log_provider_call,
    new_id,
    now,
    rows_to_dicts,
    run_write_transaction,
)
from app.hiagent import ProviderError
from app.evidence import media as media_evidence
from app.media_urls import build_media_url
from app.orchestration import media_scheduler
from app.orchestration.media_runs import ensure_media_trace, mark_media_job_state

__all__ = [
    "build_media_url",
    "clear_episode_artifacts", "clear_episode_video_assets", "clear_shot_artifacts",
    "clear_shot_reference_assets", "clear_shot_video_assets", "delete_episode_shots",
    "delete_project_episodes", "delete_video_version", "invalidate_episode_final",
    "invalidate_shot_video_derivatives", "purge_character_video_artifacts",
    "purge_project_video_artifacts", "purge_shot_videos",
]

_queue: asyncio.Queue[str] = asyncio.Queue()  # 兼容别名 → 参考图通道
_reference_queue: asyncio.Queue[str] = _queue
_video_ready_queue: asyncio.Queue[str] = asyncio.Queue()
_poll_queue: asyncio.Queue[str] = asyncio.Queue()
_workers: list[asyncio.Task] = []
_reference_workers: list[asyncio.Task] = _workers  # 同列表，命名清晰
_video_ready_workers: list[asyncio.Task] = []
_poll_workers: list[asyncio.Task] = []
_worker_retire_events: dict[asyncio.Task, asyncio.Event] = {}
_worker_target = 0
_reference_worker_target = 0
_video_ready_worker_target = 0
_poll_worker_target = 0
_dispatcher_task: asyncio.Task | None = None
# 延迟重排任务的强引用，避免被 GC 回收（asyncio 不持有后台任务的引用）。
_retry_tasks: set[asyncio.Task] = set()

_DISPATCH_INTERVAL_SECONDS = 1.0
_DISPATCH_BACKLOG_PER_WORKER = 2


class LeaseLost(RuntimeError):
    """The current process was fenced by recovery or another worker claim."""


def episode_video_budget_limit(episode_id: str) -> float:
    """Resolve the user-approved episode cap before the static safety default."""
    static_limit = float(get_setting("episode_cost_limit_cny") or 100)
    authority_limit: float | None = None
    try:
        from app.completion_grant import episode_video_budget_snapshot

        authority = episode_video_budget_snapshot(episode_id)
        if authority is not None:
            authority_limit = float(authority["cap_cny"])
    except Exception:  # noqa: BLE001 - legacy databases retain static cap
        pass
    try:
        from app.completion_grant import active_video_grant_budget_cap

        grant_cap = active_video_grant_budget_cap(episode_id)
        if grant_cap is not None:
            return float(grant_cap)
    except Exception:  # noqa: BLE001
        pass
    return authority_limit if authority_limit is not None else static_limit

__all__ = [name for name in globals() if not name.startswith("__")]
