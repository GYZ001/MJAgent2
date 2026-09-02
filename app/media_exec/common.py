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
import math
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
    """兼容旧调用签名的哨兵值——金额不再构成生成拦截。

    本产品现行计费是会员分档时长制（HiAgent 自有服务，模型/视频调用不按金额
    计费）。历史实现会依次尝试 grant 固化 cap／权威快照 cap／
    ``episode_cost_limit_cny`` 设置旋钮取一个"预算上限"，喂给
    ``app.orchestration.media_scheduler.reserve_budget`` 去比较拦截——那次
    拦截曾在旋钮已调到 1000 的情况下仍因 grant 固化 cap 更早生效而拦住用户
    的整集生成（EP2 事故）。``reserve_budget``／
    ``completion_grant.reserve_provider_video_budget`` 均已删除金额比较分支，
    不再读取这个返回值做拦截判断；本函数仅为了不必改动全部调用点签名而保留，
    返回值不再具备任何业务含义。见 CLAUDE.md「Retiring Features」与本次
    「成本预算拦截体系退场」。
    """
    del episode_id
    return math.inf
