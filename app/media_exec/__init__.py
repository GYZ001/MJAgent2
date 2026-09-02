"""Media execution package -- real re-export surface (post exec()-facade split).

拆包前，``app/worker.py`` 把本包五个切片（``common``/``enqueue``/``legacy_keyframes``/
``run_job``/``concat``）依次 ``exec()`` 进自己的 ``globals()``，本包的
``__init__.py`` 也把它们 ``exec()`` 进*另一份*共享命名空间——两份执行各自产生
一整套独立的类/函数/可变注册表副本。``LeaseLost``/``VideoPlanStaleFence``/
``ReviewDependencyFence``/``VideoInputRepairRequired``/``ProviderCreateUnresolved``/
``VideoInflightAdmissionDeferred`` 从 ``app.media_exec`` 副本抛出时，不是
``app.worker`` 副本类的实例，``except worker.LeaseLost`` 抓不住，一路穿透到顶层
被当成未知故障；``monkeypatch.setattr(worker, "f", stub)`` 也只改了两份里的一份，
另一份该调的地方照样跑真代码。

现在是真包：五个切片是各自独立的模块，彼此需要的名字用显式 ``from .sibling
import name`` 互相导入（方法与 ``app/portraits`` 拆包一致，见该包
``__init__.py`` 的模块 docstring）。``app/worker.py`` 不再是
``sys.modules`` 级别的整体别名，而是显式再导出本包的公开名字（含下划线私有名，
保持 ``worker.xxx`` 全仓调用点不变）。

**唯一的例外**：``_worker_target``/``_reference_worker_target``/
``_video_ready_worker_target``/``_poll_worker_target``/``_dispatcher_task``
不在 ``.common``，而在 ``.run_job.worker_lifecycle``（``run_job.py`` 2026-08-30
进一步拆成的 14 个子模块之一，见该文件模块 docstring 的完整拆分说明）——它们
只被 ``worker_lifecycle.py`` 自己的 ``ensure_workers()``（四个 worker-target 计
数）/``stop()``（含 ``_dispatcher_task``）用 ``global`` 语句重新赋值，Python 的
``global`` 只能重绑定函数所在模块自己的命名空间，放在 common.py 会让这些写入
创建出一份 common.py 永远看不到的私有副本。``run_job.py`` 顶层
``from .worker_lifecycle import`` 这五个名字只是转手再导出（供本文件
``from .run_job import _worker_target, ...`` 与 ``app/worker.py`` 继续用同一份
85 名字清单，不用改一行），真正的定义处/写者/读者仍然同在
``worker_lifecycle.py``——2026-08-30 从 ``worker_lifecycle.py`` 进一步拆出的
``.dispatch``（承接 ``_enqueue_for_current_status``/``_queue_job``/两个
``_dispatch_due_jobs*``/``_durable_dispatcher``/``_start_durable_dispatcher``）
只读 ``worker_lifecycle._xxx``、只在 ``_start_durable_dispatcher()`` 里写
``worker_lifecycle._dispatcher_task = ...``，全部走限定属性访问而非
``global``，物理声明处不受影响，见 ``.dispatch`` 模块 docstring。``_queue``/
``_reference_queue``/
``_video_ready_queue``/``_poll_queue``/``_workers``/``_reference_workers``/
``_video_ready_workers``/``_poll_workers``/``_worker_retire_events``/
``_retry_tasks`` 没有这个问题——它们全程只被原地修改（``.append``/``.clear``/
``.add``/``.put``/``[key]=``），从未被重新赋值，所以哪个模块 ``from .common
import`` 到的都是同一个可变对象，直接满足「只剩一份」。

``enqueue.py`` 与 ``run_job.py`` 内部的 ``.dispatch``（经两跳转手：
``.dispatch`` 顶层导入 ``worker_lifecycle``，``worker_lifecycle`` 顶层导入
``job_recovery``，``job_recovery`` 顶层导入 ``enqueue`` 的两个名字）互相需要
对方的名字（``.dispatch`` 定义 ``_enqueue_for_current_status``；``enqueue`` 里
4 处调用它）。这是唯一一对真正的双向依赖，用惰性（函数内局部）import 打破：
``enqueue.py`` 把它对 ``_enqueue_for_current_status`` 的引用推迟到调用处，不
在模块顶层做，避免两个模块互相在对方未加载完时抢一个还不存在的名字
（``run_job.py`` 自己拆出的 14 个子模块之间还有第二对这样的双向依赖——
``worker_lifecycle.py``/``job_recovery.py`` 与 ``run_job.py``/``worker_loop.py``
各一对——同样各用一次函数内局部 import 打破，见各自文件的模块 docstring）。

~218 处既有测试用 ``monkeypatch.setattr(worker, "name", stub)`` 打桩——拆包前
``app.worker`` 与 ``app.media_exec`` 共享同一份命名空间，打包级别的补丁天然打到
每一处调用。拆成真包后，每个子模块对它导入的名字持有自己的独立拷贝，只打
``worker``/``app.media_exec`` 包级别的 re-export 属性不会到达真正调用该名字
的子模块。修复方式与 ``app/portraits``、``app/stages`` 等历史拆包一致：
``tests/conftest.py`` 的 ``patch_worker_everywhere(monkeypatch, name, value)``
遍历 ``app.worker``、``app.media_exec`` 包本身与它的每个子模块、在真正绑定该
名字的地方打桩，配套的 ``tests/test_worker_monkeypatch_guard.py`` 用 AST 扫描
全部测试文件，裸形态的 ``monkeypatch.setattr(worker, ...)`` 会被判红。
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
from app.atomic_io import (
    atomic_copy,
    atomic_write_bytes,
)
from app.artifacts import (
    _adopted_video_paths,
    _invalidate_final_video,
    clear_episode_artifacts,
    clear_episode_video_assets,
    clear_shot_artifacts,
    clear_shot_reference_assets,
    clear_shot_video_assets,
    delete_episode_shots,
    delete_project_episodes,
    delete_video_version,
    invalidate_episode_final,
    invalidate_shot_video_derivatives,
    purge_character_video_artifacts,
    purge_project_video_artifacts,
    purge_shot_videos,
)
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

from .common import (
    LeaseLost,
    _DISPATCH_BACKLOG_PER_WORKER,
    _DISPATCH_INTERVAL_SECONDS,
    _poll_queue,
    _poll_workers,
    _queue,
    _reference_queue,
    _reference_workers,
    _retry_tasks,
    _video_ready_queue,
    _video_ready_workers,
    _worker_retire_events,
    _workers,
    episode_video_budget_limit,
)

from .enqueue import (
    _assert_enqueue_storyboard_authority,
    _begin_video_preflight_job,
    _close_reused_preflight_job,
    _decision_from_mode_plan,
    _enqueue_shot_impl,
    _load_reference_gallery,
    _load_shot_model,
    _mark_video_preflight_failure,
    _outgoing_transition_context,
    _preflight_failure_is_retryable,
    _recover_paused_provider_handle,
    _reference_fingerprint_item,
    _reference_gallery_fingerprint,
    _resume_reused_paused_job,
    _reused_reason_for_status,
    _row_value,
    _transition_value,
    _usable_reference_dicts,
    _video_path,
    enqueue_shot,
    pause_episode_video_tasks,
    reconcile_episode_generation_status,
    recover_equivalent_stale_provider_jobs,
    resume_episode_video_tasks,
    stop_shot_video_tasks,
)

from .run_job import (
    ProviderCreateUnresolved,
    ReviewDependencyFence,
    VideoInflightAdmissionDeferred,
    VideoInputRepairRequired,
    VideoPlanStaleFence,
    _ContinuityWait,
    _SWEEPER_INTERVAL_SECONDS,
    _assert_current_storyboard_completion_authority,
    _assert_job_lease,
    _assert_provider_create_resolved,
    _assert_review_dependency_fence,
    _assert_review_dependency_fence_async,
    _assert_video_provider_submission_authority,
    _assert_video_provider_submission_authority_async,
    _authority_checks_can_use_worker_thread,
    _auto_retake,
    _await_with_job_lease_heartbeat,
    _block_orphaned_continuity_job,
    _claim_job_without_blocking_loop,
    _commit_provider_acceptance,
    _commit_provider_acceptance_in_transaction,
    _commit_provider_create_unresolved,
    _commit_provider_terminal_failure,
    _commit_provider_terminal_failure_in_transaction,
    _commit_video_result_checkpoint,
    _commit_video_result_checkpoint_in_transaction,
    _completed_reference_slots,
    _connection_for_heartbeat_operation,
    _defer_provider_poll,
    _dispatch_due_jobs,
    _dispatch_due_jobs_legacy,
    _dispatch_due_jobs_stage_aware,
    _dispatcher_task,
    _drain_memory_queue,
    _durable_dispatcher,
    _enqueue_for_current_status,
    _ensure_ai_video_prompt,
    _image_dimensions,
    _load_boundary_asset,
    _narrative_keyframe_candidate_progress,
    _normalize_boundary_pair,
    _paid_video_attempt_count,
    _persist_boundary_asset,
    _poll_worker_target,
    _prepare_first_frame_mode_inputs,
    _prepare_first_last_mode_inputs,
    _prepare_planned_mode_inputs,
    _prepare_reference_mode_inputs,
    _prepare_video_input_mode,
    _prior_task_poll_failure_messages,
    _provider_create_outcome_unknown,
    _provider_submitted_at,
    _provider_wait_policy,
    _queue_job,
    _recover_one_media_job,
    _recover_paid_video_task,
    _reference_gallery_ready,
    _reference_worker_target,
    _release_interrupted_worker_job,
    _release_pre_call_video_claim,
    _requeue_after,
    _resolve_current_execution_plan,
    _run_in_memory_write_transaction,
    _run_job,
    _schedule_job_retry,
    _set_job,
    _set_version,
    _stale_lease_sweeper,
    _start_durable_dispatcher,
    _sweeper_task,
    _video_image_inputs_from_meta,
    _video_mode_input_roles_valid,
    _video_model_rejection_guidance,
    _video_ready_worker_target,
    _wait_for_worker_job,
    _worker_loop,
    _worker_target,
    ensure_workers,
    reconcile_stalled_video_jobs,
    recover_and_start,
    recover_media_jobs,
    start_stale_lease_sweeper,
    stop,
)

from .concat import (
    ConcatOperationConflict,
    ConcatOperationInProgress,
    _ACTIVE_VIDEO_JOB_STATUSES,
    _CONCAT_COMMAND,
    _CONCAT_DURATION_TOLERANCE_MIN_S,
    _CONCAT_DURATION_TOLERANCE_RATIO,
    _CONCAT_OPERATION_LEASE_S,
    _CONCAT_PROBE_TIMEOUT_S,
    _active_generation_shot_nos,
    _assert_concat_sources_current,
    _auto_adopt_playable_candidates_before_mix,
    _concat_operation_key,
    _concat_promotion_checkpoint,
    _content_versioned_final_url,
    _edit_report_path,
    _ensure_concat_operation_receipts,
    _existing_final_url,
    _final_edit_decision,
    _final_edit_mode,
    _final_video_is_stale,
    _final_video_path,
    _is_delivery_fallback,
    _load_completed_concat_result,
    _media_sha256,
    _playable_model_candidate,
    _probe_concat_media,
    _publish_concat_output,
    _read_edit_report,
    _resume_concat_promotion,
    _shot_has_valid_adopted_video,
    _validate_concat_output,
    _versioned_final_url,
    claim_concat_operation,
    concatenate_episode,
    episode_mix_status,
    release_concat_operation,
)

__all__ = [
    "Any",
    "ConcatOperationConflict",
    "ConcatOperationInProgress",
    "LeaseLost",
    "Path",
    "ProviderCreateUnresolved",
    "ProviderError",
    "ReviewDependencyFence",
    "VideoBudgetAuthorizationError",
    "VideoInflightAdmissionDeferred",
    "VideoInputRepairRequired",
    "VideoPlanStaleFence",
    "_ACTIVE_VIDEO_JOB_STATUSES",
    "_CONCAT_COMMAND",
    "_CONCAT_DURATION_TOLERANCE_MIN_S",
    "_CONCAT_DURATION_TOLERANCE_RATIO",
    "_CONCAT_OPERATION_LEASE_S",
    "_CONCAT_PROBE_TIMEOUT_S",
    "_ContinuityWait",
    "_DISPATCH_BACKLOG_PER_WORKER",
    "_DISPATCH_INTERVAL_SECONDS",
    "_SWEEPER_INTERVAL_SECONDS",
    "_active_generation_shot_nos",
    "_adopted_video_paths",
    "_assert_concat_sources_current",
    "_assert_current_storyboard_completion_authority",
    "_assert_enqueue_storyboard_authority",
    "_assert_job_lease",
    "_assert_provider_create_resolved",
    "_assert_review_dependency_fence",
    "_assert_review_dependency_fence_async",
    "_assert_video_provider_submission_authority",
    "_assert_video_provider_submission_authority_async",
    "_authority_checks_can_use_worker_thread",
    "_auto_adopt_playable_candidates_before_mix",
    "_auto_retake",
    "_await_with_job_lease_heartbeat",
    "_begin_video_preflight_job",
    "_block_orphaned_continuity_job",
    "_claim_job_without_blocking_loop",
    "_close_reused_preflight_job",
    "_commit_provider_acceptance",
    "_commit_provider_acceptance_in_transaction",
    "_commit_provider_create_unresolved",
    "_commit_provider_terminal_failure",
    "_commit_provider_terminal_failure_in_transaction",
    "_commit_video_result_checkpoint",
    "_commit_video_result_checkpoint_in_transaction",
    "_completed_reference_slots",
    "_concat_operation_key",
    "_concat_promotion_checkpoint",
    "_connection_for_heartbeat_operation",
    "_content_versioned_final_url",
    "_decision_from_mode_plan",
    "_defer_provider_poll",
    "_dispatch_due_jobs",
    "_dispatch_due_jobs_legacy",
    "_dispatch_due_jobs_stage_aware",
    "_dispatcher_task",
    "_drain_memory_queue",
    "_durable_dispatcher",
    "_edit_report_path",
    "_enqueue_for_current_status",
    "_enqueue_shot_impl",
    "_ensure_ai_video_prompt",
    "_ensure_concat_operation_receipts",
    "_existing_final_url",
    "_final_edit_decision",
    "_final_edit_mode",
    "_final_video_is_stale",
    "_final_video_path",
    "_image_dimensions",
    "_invalidate_final_video",
    "_is_delivery_fallback",
    "_load_boundary_asset",
    "_load_completed_concat_result",
    "_load_reference_gallery",
    "_load_shot_model",
    "_mark_video_preflight_failure",
    "_media_sha256",
    "_narrative_keyframe_candidate_progress",
    "_normalize_boundary_pair",
    "_outgoing_transition_context",
    "_paid_video_attempt_count",
    "_persist_boundary_asset",
    "_playable_model_candidate",
    "_poll_queue",
    "_poll_worker_target",
    "_poll_workers",
    "_preflight_failure_is_retryable",
    "_prepare_first_frame_mode_inputs",
    "_prepare_first_last_mode_inputs",
    "_prepare_planned_mode_inputs",
    "_prepare_reference_mode_inputs",
    "_prepare_video_input_mode",
    "_prior_task_poll_failure_messages",
    "_probe_concat_media",
    "_provider_create_outcome_unknown",
    "_provider_submitted_at",
    "_provider_wait_policy",
    "_publish_concat_output",
    "_queue",
    "_queue_job",
    "_read_edit_report",
    "_recover_one_media_job",
    "_recover_paid_video_task",
    "_recover_paused_provider_handle",
    "_reference_fingerprint_item",
    "_reference_gallery_fingerprint",
    "_reference_gallery_ready",
    "_reference_queue",
    "_reference_worker_target",
    "_reference_workers",
    "_release_interrupted_worker_job",
    "_release_pre_call_video_claim",
    "_requeue_after",
    "_resolve_current_execution_plan",
    "_resume_concat_promotion",
    "_resume_reused_paused_job",
    "_retry_tasks",
    "_reused_reason_for_status",
    "_row_value",
    "_run_in_memory_write_transaction",
    "_run_job",
    "_schedule_job_retry",
    "_set_job",
    "_set_version",
    "_shot_has_valid_adopted_video",
    "_stale_lease_sweeper",
    "_start_durable_dispatcher",
    "_sweeper_task",
    "_transition_value",
    "_usable_reference_dicts",
    "_validate_concat_output",
    "_versioned_final_url",
    "_video_image_inputs_from_meta",
    "_video_mode_input_roles_valid",
    "_video_model_rejection_guidance",
    "_video_path",
    "_video_ready_queue",
    "_video_ready_worker_target",
    "_video_ready_workers",
    "_wait_for_worker_job",
    "_worker_loop",
    "_worker_retire_events",
    "_worker_target",
    "_workers",
    "asyncio",
    "atomic_copy",
    "atomic_write_bytes",
    "build_media_url",
    "claim_concat_operation",
    "clear_episode_artifacts",
    "clear_episode_video_assets",
    "clear_shot_artifacts",
    "clear_shot_reference_assets",
    "clear_shot_video_assets",
    "common",
    "concat",
    "concatenate_episode",
    "config",
    "delete_episode_shots",
    "delete_project_episodes",
    "delete_video_version",
    "enqueue",
    "enqueue_shot",
    "ensure_media_trace",
    "ensure_source_excerpt_in_prompt",
    "ensure_workers",
    "episode_mix_status",
    "episode_video_budget_limit",
    "errors",
    "get_conn",
    "get_setting",
    "hashlib",
    "hiagent",
    "invalidate_episode_final",
    "invalidate_shot_video_derivatives",
    "json",
    "log_provider_call",
    "make_idem_key",
    "mark_media_job_state",
    "math",
    "media_evidence",
    "media_scheduler",
    "new_id",
    "now",
    "pause_episode_video_tasks",
    "purge_character_video_artifacts",
    "purge_project_video_artifacts",
    "purge_shot_videos",
    "reconcile_episode_generation_status",
    "reconcile_stalled_video_jobs",
    "recover_and_start",
    "recover_equivalent_stale_provider_jobs",
    "recover_media_jobs",
    "release_concat_operation",
    "resume_episode_video_tasks",
    "rows_to_dicts",
    "run_job",
    "run_write_transaction",
    "set_worker_trace",
    "shot_cost_cny",
    "shutil",
    "start_stale_lease_sweeper",
    "stop",
    "stop_shot_video_tasks",
    "subprocess",
    "tempfile",
    "time",
    "video_modes",
]
