"""集级视频补齐 Supervisor（Episode Video Completion Supervisor）。

协调者（reconciler）：维护覆盖台账、Issue 化失败、经 Repair Router 重新入队，
不接管 _run_job / media_pipeline 调度器。

原 app/video_supervisor.py（4,487 行）按关注点拆分为本包下的多个模块：授权与视频计划
绑定（authority.py）、checkpoint 持久化与心跳（checkpoint.py）、覆盖台账重建与陈旧性
判定（coverage.py）、attempts 预算换算（budget.py）、连续性死锁解除与任务冻结/释放
（job_control.py）、逐镜派发（dispatch.py）、Issue 收集与降级级联（issues_cascade.py）、
分镜语义修复提案（storyboard_repair.py）、候选自动采用（adoption.py）、截止收口
（closeout.py）、参考素材预热（assets.py）、主 tick 循环（run_loop.py）、失败安全终态
与韧性外壳（resilience.py）、看门狗巡检（watchdog.py）、运维预览与恢复入口（ops.py），
外加常量/阶段标签（constants.py）与 Pydantic 数据模型（models.py）。

本文件是唯一的稳定入口：全仓所有 `from app.video_supervisor import X` /
`import app.video_supervisor` / `video_supervisor.X` 使用方式必须不经改动继续可用——
下面按**真源**显式再导出每一个符号（用 `name as name` 的 PEP 484 显式重导出写法，
不使用 `from .x import *`，见 app/FILE_CONVENTIONS.toml 的 star_import 闸门）：包内
定义的符号从定义它的子模块导出一次；来自其它包的符号从其真正的定义模块直接导出，
不借道某个碰巧 import 了它的子模块转手。stdlib/typing/`__future__`（`Any`、
`annotations`、`json`、`math`、`threading`、`Literal`、`pydantic.BaseModel`/
`Field`、`pathlib.Path`）不作为包属性导出——它们是子模块的实现细节导入，不是本包对外
API，且 `tests/conftest.py::patch_video_supervisor_everywhere` 打桩走的是子模块直接
`setattr`，不依赖包属性。`asyncio` 例外保留：它是共享单例模块本身（不是各子模块各自
持有的值副本），`tests/test_video_supervisor_integration.py` 的
`monkeypatch.setattr(supervisor.asyncio, "sleep", ...)` 直接改这个共享对象的属性，
删掉包级 `asyncio` 会让这一处 `AttributeError`（2026-09-01 实测踩中）。
新增补齐逻辑请加进对应关注点的子模块，不要加回本文件。
"""
from __future__ import annotations

import asyncio as asyncio

from app.completion_grant import (
    GrantValidationError as GrantValidationError,
    VideoCompletionGrant as VideoCompletionGrant,
    bind_video_grant_generation_plan as bind_video_grant_generation_plan,
    consume_grant as consume_grant,
    get_video_grant as get_video_grant,
    validate_video_grant as validate_video_grant,
)
from app.continuity import classify_video_hard_failures as classify_video_hard_failures
from app.db import (
    get_conn as get_conn,
    new_id as new_id,
    now as now,
)
from app.evidence import repository as evidence_repository  # noqa: F401 -- renamed re-export, ruff only special-cases `x as x`
from app.evidence.media import (
    grade_shot_video as grade_shot_video,
    select_best_video_candidate as select_best_video_candidate,
    video_candidate_selection_score as video_candidate_selection_score,
)
from app.harness.types import (
    Evaluation as Evaluation,
    EvidenceArtifact as EvidenceArtifact,
    Issue as Issue,
    IssueSeverity as IssueSeverity,
)
from app.media_pipeline.stages import ACTIVE_JOB_STATUSES as ACTIVE_JOB_STATUSES
from app.schemas import (
    Shot as Shot,
    Storyboard as Storyboard,
)
from app.video_control import consume_control as consume_control
from app.video_issues import (
    is_fatal as is_fatal,
    issues_from_enqueue_error as issues_from_enqueue_error,
    issues_from_job_failure as issues_from_job_failure,
    issues_from_qa as issues_from_qa,
    load_persisted_shot_issues as load_persisted_shot_issues,
    persist_shot_issue as persist_shot_issue,
)
from app.video_repair_router import (
    MAX_CHAIN_CASCADE_DEPTH as MAX_CHAIN_CASCADE_DEPTH,
    RepairLevel as RepairLevel,
    VideoRepairPlan as VideoRepairPlan,
    bump_fingerprint_count as bump_fingerprint_count,
    route as route_video_repair,  # noqa: F401 -- renamed re-export, ruff only special-cases `x as x`
    should_cascade as should_cascade,
    state_drift_significant as state_drift_significant,
)

from .adoption import (
    _adopt_ready_candidates as _adopt_ready_candidates,
    _adopt_ready_candidates_incrementally as _adopt_ready_candidates_incrementally,
    _has_unadopted_ready_candidate as _has_unadopted_ready_candidate,
    _write_coverage_report as _write_coverage_report,
)
from .assets import (
    _asset_prep_heartbeat as _asset_prep_heartbeat,
    _prepare_episode_reference_assets as _prepare_episode_reference_assets,
    _reference_asset_scan as _reference_asset_scan,
)
from .authority import (
    _ensure_supervisor_video_plan as _ensure_supervisor_video_plan,
    _record_grant_validation_failure as _record_grant_validation_failure,
    _supervisor_checks_can_use_worker_thread as _supervisor_checks_can_use_worker_thread,
    _verify_episode_plan_current_async as _verify_episode_plan_current_async,
    _verify_supervisor_paid_authority as _verify_supervisor_paid_authority,
    _verify_supervisor_paid_authority_async as _verify_supervisor_paid_authority_async,
)
from .budget import (
    _merge_shot_state as _merge_shot_state,
    _rebuild_budgeted_coverage_ledger as _rebuild_budgeted_coverage_ledger,
    _rebuild_budgeted_coverage_ledger_async as _rebuild_budgeted_coverage_ledger_async,
    _rebuild_coverage_ledger_async as _rebuild_coverage_ledger_async,
    attempts_for as attempts_for,
)
from .checkpoint import (
    _persist_checkpoint_transaction as _persist_checkpoint_transaction,
    _refresh_supervisor_heartbeat as _refresh_supervisor_heartbeat,
    _run_checkpoint_write as _run_checkpoint_write,
    _save_checkpoint_async as _save_checkpoint_async,
    load_latest_checkpoint as load_latest_checkpoint,
    public_checkpoint_projection as public_checkpoint_projection,
    save_checkpoint as save_checkpoint,
)
from .closeout import (
    _deadline_closeout as _deadline_closeout,
    _deadline_closeout_async as _deadline_closeout_async,
    _finalize_covered as _finalize_covered,
    _finalize_covered_async as _finalize_covered_async,
)
from .constants import (
    ASSET_PREP_HEARTBEAT_INTERVAL_S as ASSET_PREP_HEARTBEAT_INTERVAL_S,
    CHECKPOINT_ARTIFACT_TYPE as CHECKPOINT_ARTIFACT_TYPE,
    CONTROL_PLANE_MAX_RECOVERIES as CONTROL_PLANE_MAX_RECOVERIES,
    DISPATCH_HEARTBEAT_INTERVAL_S as DISPATCH_HEARTBEAT_INTERVAL_S,
    LIFECYCLE_HEARTBEAT_INTERVAL_S as LIFECYCLE_HEARTBEAT_INTERVAL_S,
    MAX_ATTEMPTS_PER_SHOT as MAX_ATTEMPTS_PER_SHOT,
    MAX_REPAIR_EPOCHS as MAX_REPAIR_EPOCHS,
    MIN_ATTEMPTS_PER_SHOT as MIN_ATTEMPTS_PER_SHOT,
    REPORT_ARTIFACT_TYPE as REPORT_ARTIFACT_TYPE,
    SUPERVISOR_HEARTBEAT_STALE_S as SUPERVISOR_HEARTBEAT_STALE_S,
    SUPERVISOR_TICK_INTERVAL_S as SUPERVISOR_TICK_INTERVAL_S,
    SUPERVISOR_TICK_MAX_INTERVAL_S as SUPERVISOR_TICK_MAX_INTERVAL_S,
    SupervisorPhase as SupervisorPhase,
    TERMINAL_SUPERVISOR_PHASES as TERMINAL_SUPERVISOR_PHASES,
    VIDEO_COMPLETION_BUSINESS_STAGES as VIDEO_COMPLETION_BUSINESS_STAGES,
    _CHECKPOINT_WRITE_SEMAPHORE as _CHECKPOINT_WRITE_SEMAPHORE,
    _PHASE_LABELS as _PHASE_LABELS,
    _REFERENCE_ASSET_PREP_LOCKS as _REFERENCE_ASSET_PREP_LOCKS,
    phase_label as phase_label,
)
from .coverage import (
    _compute_chains as _compute_chains,
    _human_adopted as _human_adopted,
    _video_stale_for_shot as _video_stale_for_shot,
    rebuild_coverage_ledger as rebuild_coverage_ledger,
)
from .dispatch import (
    _after_shot_id as _after_shot_id,
    _dispatch as _dispatch,
    _dispatch_heartbeat as _dispatch_heartbeat,
    _dispatch_with_heartbeat as _dispatch_with_heartbeat,
    _dispatch_with_heartbeat_async as _dispatch_with_heartbeat_async,
    _patch_version_supervisor_meta as _patch_version_supervisor_meta,
    _requeue_no_charge_job as _requeue_no_charge_job,
)
from .issues_cascade import (
    _adopt_fallback as _adopt_fallback,
    _apply_cascade as _apply_cascade,
    _collect_issues as _collect_issues,
)
from .job_control import (
    _reconcile_terminal_continuity_blocks as _reconcile_terminal_continuity_blocks,
    _release_episode_supervisor as _release_episode_supervisor,
    _stop_supervised_video_jobs as _stop_supervised_video_jobs,
)
from .models import (
    CoverageLedger as CoverageLedger,
    ShotCoverageEntry as ShotCoverageEntry,
    StoryboardRepairAffectedAuthority as StoryboardRepairAffectedAuthority,
    StoryboardRepairProposal as StoryboardRepairProposal,
    VideoSupervisorCheckpoint as VideoSupervisorCheckpoint,
    _adopted_video_is_usable as _adopted_video_is_usable,
)
from .ops import (
    preview_video_completion_repair as preview_video_completion_repair,
    recover_video_completion_runs as recover_video_completion_runs,
)
from .resilience import (
    _mark_failed_closed as _mark_failed_closed,
    _mark_failed_closed_async as _mark_failed_closed_async,
    _run_video_completion_resilient_loop as _run_video_completion_resilient_loop,
    _supervisor_lifecycle_heartbeat_worker as _supervisor_lifecycle_heartbeat_worker,
    run_video_completion_resilient as run_video_completion_resilient,
)
from .run_loop import run_video_completion_supervisor as run_video_completion_supervisor
from .storyboard_repair import (
    _amend_storyboard as _amend_storyboard,
    _candidate_storyboard_from_repair as _candidate_storyboard_from_repair,
    _repair_authority_ids as _repair_authority_ids,
    _semantic_storyboard_repair_proposal as _semantic_storyboard_repair_proposal,
    _try_auto_crop as _try_auto_crop,
    _validate_storyboard_repair_proposal as _validate_storyboard_repair_proposal,
)
from .watchdog import (
    reconcile_stale_video_supervisors as reconcile_stale_video_supervisors,
    video_supervisor_watchdog_loop as video_supervisor_watchdog_loop,
)
