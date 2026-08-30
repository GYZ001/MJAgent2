"""分镜（Storyboard）镜头生成、编辑、结构调整与清空的路由与业务逻辑。

``app/domain/storyboard_ops.py``（5,487 行 / 98 个顶层定义，本仓最大的单文件）拆成本包，按关注点分 20 个子模块：镜头写操作共享原语（mutation_primitives，含 _board_from_shot_rows）、场景绑定投影（scene_projection）、镜头/素材陈旧性判据（staleness）、分镜清空实际执行（clear_apply）、视频模型人工切换（video_model）、角色策略修复落库（character_policy_repair）、证据核验+终态收口+软缺口续跑+计划自更新（evidence）、生成任务体主循环（task_body，单函数 426 行不拆）、任务体录制器+sqlite 锁重试+守卫包装（task_run）、续跑判据（resume_state）、开机孤儿任务恢复（task_recovery）、生成发起/续跑/批量（start_resume）、清空预览（clear_preview）、整体状态快照（status_snapshot，单函数 334 行不拆）、单镜头编辑会话（shot_edit_session）、结构调整事务（structure）、镜头版本列表投影（public_shot_versions）、整集详情投影（episode_detail，单函数 323 行不拆）、单镜头编辑落地（edit_shot，单函数 419 行不拆）、台词契约审计与冲突裁决（spoken_contract）。移动未重写，逻辑/签名/格式不变；四个单函数子模块之所以不进一步拆分，是因为各自是其所在状态机判据/事务边界的唯一权威执行顺序，拆分会打散提前返回与写入的耦合。

``storyboard_ops`` <-> ``video_ops``、``storyboard_ops`` <-> ``review_wall`` 在整个``app.domain`` 包级别是真实双向依赖（见 ``app/domain/__init__.py`` 模块 docstring）：``video_model`` 子模块里 ``set_episode_video_model`` 对 ``app.domain.video_ops``/``app.domain.review_wall`` 的引用、``status_snapshot`` 对 ``app.domain.video_ops`` 的引用、``episode_detail`` 对 ``app.domain.screenplay_ops`` 的引用，都是原文件里就已经存在的函数内延迟导入，移动时原样保留（不是本次新增）。本包内部的调用图本身是一个DAG（用 Tarjan 算法核验过，不存在非平凡强连通分量），11 个子模块之间的每一条依赖边都能用一个固定的拓扑序（如上列出的 order）满足，不需要包内延迟导入。

本文件是稳定入口：所有既有 ``from app.domain.storyboard_ops import X`` / ``app.domain.storyboard_ops.X`` 调用点必须原样可用——每个符号（含每个子模块自己``import`` 进来的名字，不只是它原生定义的）用 ``name as name`` 显式再导出（PEP 484 显式重导出写法，与 ``app/validators/__init__.py``、``app/domain/bible_ops/__init__.py`` 同一先例），而不是 ``from .x import *``（``app/FILE_CONVENTIONS.toml`` 的 ``star_import`` 闸门禁止后者）。新增分镜相关逻辑请加进对应关注点的子模块，不要加回本文件。
"""
from __future__ import annotations

from .mutation_primitives import (
    EpisodeScreenplay as EpisodeScreenplay,
    HTTPException as HTTPException,
    Shot as Shot,
    Storyboard as Storyboard,
    _NARRATIVE_PRESENTATION_EDIT_FIELDS as _NARRATIVE_PRESENTATION_EDIT_FIELDS,
    _apply_contract_to_public_shot as _apply_contract_to_public_shot,
    _assert_storyboard_write_authorized as _assert_storyboard_write_authorized,
    _board_from_shot_rows as _board_from_shot_rows,
    _insert_storyboard_shot as _insert_storyboard_shot,
    _narrative_semantic_edit_fields as _narrative_semantic_edit_fields,
    _raise_narrative_semantic_mutation_required as _raise_narrative_semantic_mutation_required,
    _resolve_storyboard_mutation_screenplay as _resolve_storyboard_mutation_screenplay,
    _screenplay_rebuild_block as _screenplay_rebuild_block,
    _shot_contract_json as _shot_contract_json,
    annotations as annotations,
    errors as errors,
    json as json,
    new_id as new_id,
    normalize_action_desc as normalize_action_desc,
)

from .scene_projection import (
    Bible as Bible,
    StoryboardOutline as StoryboardOutline,
    StoryboardOutlineShot as StoryboardOutlineShot,
    _reconcile_storyboard_scene_projection as _reconcile_storyboard_scene_projection,
    _sync_storyboard_scene_bindings as _sync_storyboard_scene_bindings,
)

from .staleness import (
    _shot_adopted_assets_stale as _shot_adopted_assets_stale,
    _shot_video_is_stale as _shot_video_is_stale,
)

from .clear_apply import (
    Path as Path,
    _episode_or_404 as _episode_or_404,
    asyncio as asyncio,
    clear_storyboard as clear_storyboard,
    clear_storyboard_projection as clear_storyboard_projection,
    config as config,
    evidence_repository as evidence_repository,
    get_conn as get_conn,
    now as now,
    task_registry as task_registry,
    worker as worker,
)

from .video_model import (
    _episode_target_video_model as _episode_target_video_model,
    _require_video_clear_write_scope as _require_video_clear_write_scope,
    router as router,
    set_episode_video_model as set_episode_video_model,
)

from .character_policy_repair import (
    Evaluation as Evaluation,
    EvidenceArtifact as EvidenceArtifact,
    _persist_storyboard_character_policy_repairs as _persist_storyboard_character_policy_repairs,
    get_contract as get_contract,
    log_provider_call as log_provider_call,
)

from .evidence import (
    Issue as Issue,
    IssueSeverity as IssueSeverity,
    _can_continue_for_soft_gap as _can_continue_for_soft_gap,
    _ensure_current_storyboard_shot_artifacts as _ensure_current_storyboard_shot_artifacts,
    _finalize_storyboard_evidence as _finalize_storyboard_evidence,
    _reconcile_storyboard_plan as _reconcile_storyboard_plan,
    _soft_gap_continue_residual as _soft_gap_continue_residual,
    _storyboard_publication_evidence_state as _storyboard_publication_evidence_state,
    _storyboard_shot_artifact_matches as _storyboard_shot_artifact_matches,
    _storyboard_shot_evidence_requires_rebind as _storyboard_shot_evidence_requires_rebind,
)

from .task_body import (
    StageError as StageError,
    _load_screenplay as _load_screenplay,
    _prepare_storyboard_assets_background as _prepare_storyboard_assets_background,
    _prepare_storyboard_assets_background_detached as _prepare_storyboard_assets_background_detached,
    _project_bible_or_placeholder as _project_bible_or_placeholder,
    _storyboard_task as _storyboard_task,
    episode_prep_pack_payload as episode_prep_pack_payload,
)

from .task_run import (
    ContextPack as ContextPack,
    WorkflowRecorder as WorkflowRecorder,
    _STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S as _STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S,
    _is_transient_sqlite_lock as _is_transient_sqlite_lock,
    _new_storyboard_recorder as _new_storyboard_recorder,
    _recorded_storyboard_task as _recorded_storyboard_task,
    _storyboard_bound_bible_artifact_id as _storyboard_bound_bible_artifact_id,
    _storyboard_generation_is_live as _storyboard_generation_is_live,
    _storyboard_guarded_recorded as _storyboard_guarded_recorded,
    _storyboard_task_with_sqlite_lock_retry as _storyboard_task_with_sqlite_lock_retry,
    fingerprint as fingerprint,
    rows_to_dicts as rows_to_dicts,
    sqlite3 as sqlite3,
)

from .resume_state import (
    Body as Body,
    _as_body_dict as _as_body_dict,
    _screenplay_ready as _screenplay_ready,
    _storyboard_checkpoint_matches_screenplay as _storyboard_checkpoint_matches_screenplay,
    _storyboard_has_material as _storyboard_has_material,
    _storyboard_has_persisted_work as _storyboard_has_persisted_work,
    _storyboard_resume_decision as _storyboard_resume_decision,
    _storyboard_start_preflight_payload as _storyboard_start_preflight_payload,
    storyboard_start_preflight as storyboard_start_preflight,
)

from .task_recovery import (
    recover_storyboard_tasks as recover_storyboard_tasks,
)

from .start_resume import (
    _project_or_404 as _project_or_404,
    _require_harness_engine as _require_harness_engine,
    cancel_storyboard as cancel_storyboard,
    resume_storyboard as resume_storyboard,
    start_storyboard as start_storyboard,
    start_storyboard_all as start_storyboard_all,
)

from .clear_preview import (
    _assert_storyboard_clear_not_running as _assert_storyboard_clear_not_running,
    apply_storyboard_clear as apply_storyboard_clear,
    preview_storyboard_clear as preview_storyboard_clear,
)

from .status_snapshot import (
    _storyboard_issue_targets_shot as _storyboard_issue_targets_shot,
    _storyboard_status_snapshot as _storyboard_status_snapshot,
    get_setting as get_setting,
    re as re,
    storyboard_authorized_source as storyboard_authorized_source,
    storyboard_pack_prompts_complete as storyboard_pack_prompts_complete,
)

from .shot_edit_session import (
    _public_shot_editable_value as _public_shot_editable_value,
    discard_shot_edit_draft as discard_shot_edit_draft,
    list_shot_edit_drafts as list_shot_edit_drafts,
    preview_shot_edit_impact as preview_shot_edit_impact,
    shot_cost_cny as shot_cost_cny,
    start_shot_edit_session as start_shot_edit_session,
)

from .structure import (
    _apply_storyboard_structure_transaction as _apply_storyboard_structure_transaction,
    _set_row_final_contract as _set_row_final_contract,
    _structure_operation_plan as _structure_operation_plan,
    apply_storyboard_structure as apply_storyboard_structure,
    preview_storyboard_structure as preview_storyboard_structure,
)

from .public_shot_versions import (
    _MAX_PUBLIC_IMAGE_INPUT_CHARS as _MAX_PUBLIC_IMAGE_INPUT_CHARS,
    _media_url as _media_url,
    _public_failure_log as _public_failure_log,
    _public_reference_image as _public_reference_image,
    _public_shot_versions as _public_shot_versions,
    build_media_url as build_media_url,
)

from .episode_detail import (
    SCREENPLAY_WORKSPACE_WITHHELD_FIELDS as SCREENPLAY_WORKSPACE_WITHHELD_FIELDS,
    _episode_detail_projection as _episode_detail_projection,
    episode_detail as episode_detail,
    screenplay_workspace_projection as screenplay_workspace_projection,
    shot_review_detail as shot_review_detail,
    storyboard_status as storyboard_status,
)

from .edit_shot import (
    clip_duration_value as clip_duration_value,
    edit_shot as edit_shot,
    schema_errors as schema_errors,
)

from .spoken_contract import (
    audit_episode_spoken_contract as audit_episode_spoken_contract,
    migrate_episode_shot_ids as migrate_episode_shot_ids,
    preview_spoken_conflict as preview_spoken_conflict,
    resolve_spoken_conflict as resolve_spoken_conflict,
)
