"""剧本（Screenplay）生成、编辑、批量发起与恢复的路由与业务逻辑。

``app/domain/screenplay_ops.py``（3,885 行 / 69 个顶层定义）拆成本包，按关注点分 14 个子模块：发布状态快照/内容差异/卡司影响/权威字段（status_snapshot）、prep-pack 轻量状态查询（lightweight_status，依赖 status_snapshot）、运行态判定/所有者断言/命令总线重试授权/目标时长（run_control）、生成任务体/录制器/context pack（task_body）、批次台账刷新与「录制+守卫」包装（guarded，依赖 task_body，供 activation/batch/task_recovery 复用）、孤儿收据清理与实际发起生成（activation，依赖 guarded）、剧本编辑与影响预览（edit）、生成前置检查（preflight，依赖 activation）、草稿存取（draft，依赖 edit）、生成发起与续跑（start_resume）、定向修复（repair）、删除（delete）、整项目批量发起/取消（batch）、开机孤儿任务恢复（task_recovery）。移动未重写，逻辑/签名/格式不变；子模块之间原本共享同一文件命名空间形成的真实调用环（activation 的 _spawn_screenplay_activation 经由 guarded 间接调用批次台账，batch 的 start_screenplay_all 又直接调用 activation）通过把 guarded 单独拆成一个只向前依赖（task_body、run_control）的文件破环，不是靠延迟导入。

本文件是稳定入口：所有既有 ``from app.domain.screenplay_ops import X`` / ``app.domain.screenplay_ops.X`` 调用点必须原样可用——每个符号（含每个子模块自己``import`` 进来的名字，不只是它原生定义的）用 ``name as name`` 显式再导出（PEP 484 显式重导出写法，与 ``app/validators/__init__.py``、``app/narrative/__init__.py``、``app/domain/bible_ops/__init__.py`` 同一先例），而不是 ``from .x import *``（``app/FILE_CONVENTIONS.toml`` 的 ``star_import`` 闸门禁止后者）。新增剧本相关逻辑请加进对应关注点的子模块，不要加回本文件。
"""
from __future__ import annotations

from .status_snapshot import (
    EpisodeScreenplay as EpisodeScreenplay,
    _SCREENPLAY_IR_WORKING_TYPES as _SCREENPLAY_IR_WORKING_TYPES,
    _SCREENPLAY_REBUILD_ERROR_UNSET as _SCREENPLAY_REBUILD_ERROR_UNSET,
    _clear_unpublished_screenplay_ir as _clear_unpublished_screenplay_ir,
    _project_bible_or_placeholder as _project_bible_or_placeholder,
    _published_screenplay_revalidation_eligibility as _published_screenplay_revalidation_eligibility,
    _screenplay_authority_state as _screenplay_authority_state,
    _screenplay_cast_impact as _screenplay_cast_impact,
    _screenplay_content_payload as _screenplay_content_payload,
    _screenplay_field_diff as _screenplay_field_diff,
    _screenplay_production_state as _screenplay_production_state,
    _screenplay_ready as _screenplay_ready,
    _screenplay_rebuild_state as _screenplay_rebuild_state,
    _screenplay_status_snapshot as _screenplay_status_snapshot,
    annotations as annotations,
    evidence_repository as evidence_repository,
    get_conn as get_conn,
    json as json,
    task_registry as task_registry,
)

from .lightweight_status import (
    _episode_or_404 as _episode_or_404,
    router as router,
    screenplay_lightweight_status as screenplay_lightweight_status,
)

from .prep_pack_stage_snapshot import (
    Any as Any,
    _PREP_PACK_STAGE_STEP_KEYS as _PREP_PACK_STAGE_STEP_KEYS,
    _prep_pack_stage_snapshot as _prep_pack_stage_snapshot,
)

from .run_control import (
    Body as Body,
    HTTPException as HTTPException,
    StateConflict as StateConflict,
    WorkflowRecorder as WorkflowRecorder,
    _SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL as _SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL,
    _as_body_dict as _as_body_dict,
    _assert_screenplay_run_owner as _assert_screenplay_run_owner,
    _cancel_persisted_screenplay_run as _cancel_persisted_screenplay_run,
    _enter_screenplay_command_bus_retry_approval as _enter_screenplay_command_bus_retry_approval,
    _exit_screenplay_command_bus_retry_approval as _exit_screenplay_command_bus_retry_approval,
    _project_screenplay_runtime_failure as _project_screenplay_runtime_failure,
    _retry_authority as _retry_authority,
    _screenplay_fallback_status as _screenplay_fallback_status,
    _screenplay_task_active as _screenplay_task_active,
    config as config,
    now as now,
    update_episode_target_duration as update_episode_target_duration,
)

from .task_body import (
    ContextPack as ContextPack,
    SCREENPLAY_SOURCE_BUDGET_CHARS as SCREENPLAY_SOURCE_BUDGET_CHARS,
    StageError as StageError,
    _episode_source_text as _episode_source_text,
    _new_screenplay_recorder as _new_screenplay_recorder,
    _recorded_screenplay_task as _recorded_screenplay_task,
    _screenplay_character_discovery as _screenplay_character_discovery,
    _screenplay_context_pack as _screenplay_context_pack,
    _screenplay_task as _screenplay_task,
    asyncio as asyncio,
    errors as errors,
    fingerprint as fingerprint,
    get_contract as get_contract,
    get_setting as get_setting,
)

from .guarded import (
    _refresh_screenplay_batch_run as _refresh_screenplay_batch_run,
    _screenplay_guarded as _screenplay_guarded,
)

from .activation import (
    _abandon_orphaned_blueprint_receipts as _abandon_orphaned_blueprint_receipts,
    _screenplay_blueprint_budget_projection as _screenplay_blueprint_budget_projection,
    _spawn_screenplay_activation as _spawn_screenplay_activation,
)

from .edit import (
    EvidenceArtifact as EvidenceArtifact,
    SCREENPLAY_WORKSPACE_WITHHELD_FIELDS as SCREENPLAY_WORKSPACE_WITHHELD_FIELDS,
    _load_screenplay as _load_screenplay,
    _prepare_screenplay_for_storage as _prepare_screenplay_for_storage,
    _screenplay_payload_with_authority_fields as _screenplay_payload_with_authority_fields,
    edit_screenplay as edit_screenplay,
    merge_withheld_screenplay_fields as merge_withheld_screenplay_fields,
    preview_screenplay_edit_impact as preview_screenplay_edit_impact,
    schema_errors as schema_errors,
)

from .preflight import (
    BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD as BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD,
    _screenplay_generation_preflight as _screenplay_generation_preflight,
    math as math,
    screenplay_generation_preflight as screenplay_generation_preflight,
)

from .draft import (
    delete_screenplay_draft as delete_screenplay_draft,
    get_screenplay_draft as get_screenplay_draft,
    new_id as new_id,
    save_screenplay_draft as save_screenplay_draft,
)

from .start_resume import (
    _prepare_published_screenplay_revalidation as _prepare_published_screenplay_revalidation,
    _require_harness_engine as _require_harness_engine,
    resume_screenplay as resume_screenplay,
    start_screenplay as start_screenplay,
)

from .repair import (
    repair_screenplay_draft as repair_screenplay_draft,
)

from .delete import (
    delete_screenplay as delete_screenplay,
    worker as worker,
)

from .batch import (
    _project_or_404 as _project_or_404,
    cancel_screenplay as cancel_screenplay,
    cancel_screenplay_all as cancel_screenplay_all,
    start_screenplay_all as start_screenplay_all,
)

from .task_recovery import (
    recover_screenplay_tasks as recover_screenplay_tasks,
)
