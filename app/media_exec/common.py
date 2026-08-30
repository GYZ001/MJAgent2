"""媒体执行共享队列状态与围栏基类（真包拆分后的公共底座）。

``app/media_exec`` 原来把本文件与 enqueue/legacy_keyframes/run_job/concat 四个
切片 ``exec`` 进同一个共享命名空间（见 ``app/worker.py`` 与
``app/media_exec/__init__.py`` 的历史版本），任何切片对 ``_queue``/``_workers``
等名字的引用都天然解析到这里。拆成真包后，每个子模块必须显式 ``from .common
import name`` 才能拿到这些名字——``_queue``/``_reference_queue``/
``_video_ready_queue``/``_poll_queue``/``_workers``/``_reference_workers``/
``_video_ready_workers``/``_poll_workers``/``_worker_retire_events``/
``_retry_tasks`` 只在这里各自成立，其余子模块导入的都是同一个可变对象的引用
（列表/字典/集合/队列全靠原地修改，不做重新赋值，因此跨模块 import 之后仍是
同一份）。

``_worker_target``/``_reference_worker_target``/``_video_ready_worker_target``/
``_poll_worker_target``/``_dispatcher_task`` **不**放在这里：它们只被
``app/media_exec/worker_lifecycle.py`` 里用 ``global`` 语句重新赋值
（``ensure_workers()``/``stop()``），而 Python 的 ``global`` 只能重绑定函数所在
模块自己的命名空间——放在 common.py 会导致 ``worker_lifecycle.py`` 的
``global`` 语句创建出它自己的一份从未同步回 common.py 的整数副本。真正的所
有权在 ``worker_lifecycle.py`` 顶部，与 ``_sweeper_task`` 放在一起（
``run_job.py`` 顶层从那里再导入这五个名字只是转手，不改变所有权）。
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
from app.observability.tracing import set_worker_trace

_queue: asyncio.Queue[str] = asyncio.Queue()  # 兼容别名 → 参考图通道
_reference_queue: asyncio.Queue[str] = _queue
_video_ready_queue: asyncio.Queue[str] = asyncio.Queue()
_poll_queue: asyncio.Queue[str] = asyncio.Queue()
_workers: list[asyncio.Task] = []
_reference_workers: list[asyncio.Task] = _workers  # 同列表，命名清晰
_video_ready_workers: list[asyncio.Task] = []
_poll_workers: list[asyncio.Task] = []
_worker_retire_events: dict[asyncio.Task, asyncio.Event] = {}
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
