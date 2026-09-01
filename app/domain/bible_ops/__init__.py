"""人物谱（Bible）与角色/场景引用图生成的路由与业务逻辑。

``app/domain/bible_ops.py``（4,386 行 / 123 个顶层定义）拆成本包，按关注点分 15 个子模块：共享原语（primitives）、场景素材缺口判据（scene_assets）、人物谱改动影响预检（precheck）、角色引用图生成任务（refs_generation）与场景引用图/场景圣经准备任务（scene_bible_prep）、人物谱生成任务体（task_run）与开机孤儿任务恢复（task_recovery，依赖 task_run 故排其后）、单场景引用图生命周期（scene_refs）、整体画风切换与草稿存取（style_and_drafts）、人物谱/角色卡编辑（edit）、立绘候选查询与采纳（portrait_candidates）、变更后自动改配裁决（auto_change）、场景提示词/锚点编辑（scene_edit）、角色/场景单视图重绘（view_redo）、用户提名建卡（nominate，2026-08-31 新增：用户看到角色没被自动选上时的手动入口，复用既有建卡判据，不新写一套）、共用上传校验（manual_upload，2026-08-31 新增）、角色手动新增/替换定妆照（manual_character，2026-08-31 新增：图像描述与图片完全由用户提供，不走模型；去重仍过 resolve_card_owner，替换复用 portrait_candidates 的 adopt/rollback 语义）、场景手动新增/替换场景图（manual_scene，2026-08-31 新增：与 manual_character 同一用户拍板的场景侧镜像）。移动未重写，逻辑/签名/格式不变。

本文件是稳定入口：所有既有 ``from app.domain.bible_ops import X`` / ``app.domain.bible_ops.X`` 调用点必须原样可用——每个符号（含每个子模块自己``import`` 进来的名字，不只是它原生定义的）用 ``name as name`` 显式再导出（PEP 484显式重导出写法，与 ``app/validators/__init__.py``、``app/narrative/__init__.py``同一先例），而不是 ``from .x import *``（``app/FILE_CONVENTIONS.toml`` 的``star_import`` 闸门禁止后者）。新增人物谱相关逻辑请加进对应关注点的子模块，不要加回本文件。
"""
from __future__ import annotations

from .primitives import (
    DEFAULT_VISUAL_STYLE_NAME as DEFAULT_VISUAL_STYLE_NAME,
    HTTPException as HTTPException,
    SCENE_CANONICAL_MAX_CHARS as SCENE_CANONICAL_MAX_CHARS,
    SCENE_CANONICAL_MIN_CHARS as SCENE_CANONICAL_MIN_CHARS,
    _SCENE_CANONICAL_LENGTH_MESSAGE as _SCENE_CANONICAL_LENGTH_MESSAGE,
    _consume_payment_quote as _consume_payment_quote,
    _decode_refs_target as _decode_refs_target,
    _ensure_character_payment_quotes as _ensure_character_payment_quotes,
    _issue_payment_quote as _issue_payment_quote,
    _normalize_character_selection as _normalize_character_selection,
    _normalize_visual_style_name as _normalize_visual_style_name,
    _parse_json_value as _parse_json_value,
    _payment_confirm_required as _payment_confirm_required,
    _project_columns as _project_columns,
    _quote_stale as _quote_stale,
    _refs_target_payload as _refs_target_payload,
    _scene_canonical_length_ok as _scene_canonical_length_ok,
    _supports_bible_style_name as _supports_bible_style_name,
    _validate_payment_quote as _validate_payment_quote,
    _visual_style_prompt_or_default as _visual_style_prompt_or_default,
    annotations as annotations,
    default_visual_style_prompt as default_visual_style_prompt,
    get_conn as get_conn,
    json as json,
    new_id as new_id,
    now as now,
    visual_style_prompt as visual_style_prompt,
)

from .scene_assets import (
    _normalize_scene_selection as _normalize_scene_selection,
    _project_or_404 as _project_or_404,
    _scene_asset_state as _scene_asset_state,
    _scene_current_row as _scene_current_row,
    _scene_required_roles as _scene_required_roles,
    compute_scene_cost_precheck as compute_scene_cost_precheck,
    fingerprint as fingerprint,
    rows_to_dicts as rows_to_dicts,
    scan_scene_asset_gaps as scan_scene_asset_gaps,
)

from .precheck import (
    Bible as Bible,
    Path as Path,
    _artifact_type_counts as _artifact_type_counts,
    _bible_conflict_detail as _bible_conflict_detail,
    _classify_bible_changes as _classify_bible_changes,
    _compute_bible_generate_precheck as _compute_bible_generate_precheck,
    _parse_bible_write_body as _parse_bible_write_body,
    _purge_for_style_change as _purge_for_style_change,
    _purge_removed_character_portraits as _purge_removed_character_portraits,
    bible_generate_precheck as bible_generate_precheck,
    bible_impact_preview as bible_impact_preview,
    bible_visual_styles as bible_visual_styles,
    character_is_portrait_eligible as character_is_portrait_eligible,
    compute_bible_impact_preview as compute_bible_impact_preview,
    compute_refs_cost_precheck as compute_refs_cost_precheck,
    evidence_repository as evidence_repository,
    refs_cost_precheck as refs_cost_precheck,
    router as router,
    schema_errors as schema_errors,
    visual_style_options as visual_style_options,
    worker as worker,
)

from .refs_generation import (
    WorkflowRecorder as WorkflowRecorder,
    _active_refs_run as _active_refs_run,
    _new_refs_recorder as _new_refs_recorder,
    _refs_generation_busy as _refs_generation_busy,
    _refs_task as _refs_task,
    _refs_task_active as _refs_task_active,
    _start_refs_generation as _start_refs_generation,
    asyncio as asyncio,
    cancel_refs as cancel_refs,
    errors as errors,
    start_refs as start_refs,
    task_registry as task_registry,
)

from .scene_bible_prep import (
    _decode_scene_target as _decode_scene_target,
    _scene_assets_task_active as _scene_assets_task_active,
    _scene_bible_task as _scene_bible_task,
    _scene_refs_task as _scene_refs_task,
    _scene_refs_task_active as _scene_refs_task_active,
    _start_scene_bible_preparation as _start_scene_bible_preparation,
    _start_scene_refs_generation as _start_scene_refs_generation,
)

from .task_run import (
    BIBLE_INTERRUPTED_ERROR as BIBLE_INTERRUPTED_ERROR,
    BIBLE_TASK_TIMEOUT_S as BIBLE_TASK_TIMEOUT_S,
    Body as Body,
    ContextPack as ContextPack,
    StageError as StageError,
    _as_body_dict as _as_body_dict,
    _bible_task as _bible_task,
    _bible_task_active as _bible_task_active,
    _cancel_bible_core as _cancel_bible_core,
    _new_bible_recorder as _new_bible_recorder,
    _recorded_bible_task as _recorded_bible_task,
    _require_harness_engine as _require_harness_engine,
    _start_bible_core as _start_bible_core,
    cancel_bible as cancel_bible,
    generate_bible as generate_bible,
    start_bible as start_bible,
)

from .task_recovery import (
    recover_bible_tasks as recover_bible_tasks,
    recover_character_ref_tasks as recover_character_ref_tasks,
    recover_scene_ref_tasks as recover_scene_ref_tasks,
)

from .scene_refs import (
    _scene_refs_progress_payload as _scene_refs_progress_payload,
    cancel_scene_refs as cancel_scene_refs,
    config as config,
    preview_scene_bible as preview_scene_bible,
    scene_bible_precheck as scene_bible_precheck,
    scene_refs_gaps as scene_refs_gaps,
    scene_refs_precheck as scene_refs_precheck,
    scene_refs_progress as scene_refs_progress,
    start_scene_bible as start_scene_bible,
    start_scene_refs as start_scene_refs,
    validate_scene_bible as validate_scene_bible,
)

from .style_and_drafts import (
    DEFAULT_VISUAL_STYLE_NAME as DEFAULT_VISUAL_STYLE_NAME,
    _compute_style_regen_quote as _compute_style_regen_quote,
    bible_visual_styles_unscoped as bible_visual_styles_unscoped,
    get_bible_draft as get_bible_draft,
    refs_gaps as refs_gaps,
    refs_progress as refs_progress,
    save_bible_draft as save_bible_draft,
    set_bible_visual_style as set_bible_visual_style,
    visual_style_options as visual_style_options,
)

from .edit import (
    Evaluation as Evaluation,
    EvidenceArtifact as EvidenceArtifact,
    _commit_bible_revision as _commit_bible_revision,
    edit_bible as edit_bible,
    edit_character as edit_character,
    edit_portrait_prompt as edit_portrait_prompt,
)

from .nominate import (
    nominate_character as nominate_character,
)

from .portrait_candidates import (
    _adopt_portrait_by_id as _adopt_portrait_by_id,
    _media_url as _media_url,
    _portrait_artifact_candidate_payload as _portrait_artifact_candidate_payload,
    _portrait_candidate_payload as _portrait_candidate_payload,
    _portrait_gate_lists as _portrait_gate_lists,
    _portrait_views_for as _portrait_views_for,
    _set_current_portrait as _set_current_portrait,
    adopt_portrait_candidate as adopt_portrait_candidate,
    list_portrait_candidates as list_portrait_candidates,
    rollback_portrait_candidate as rollback_portrait_candidate,
)

from .auto_change import (
    _auto_change_character_card as _auto_change_character_card,
    _auto_change_payload as _auto_change_payload,
    _auto_change_portrait_id as _auto_change_portrait_id,
    decide_auto_change as decide_auto_change,
    list_auto_changes as list_auto_changes,
)

from .scene_edit import (
    edit_scene_anchor as edit_scene_anchor,
    edit_scene_prompt as edit_scene_prompt,
)

# get_setting 同理从真源（app.db）导出：2026-09-01 成本预算体系退役时，
# refs_generation.py 里那行 get_setting 导入变成未使用被删掉，直接打断了这条
# 再导出链，整个工作区 import app.main 失败——与下面 current_actor_name 是同一个坑。
from app.db import get_setting as get_setting

# current_actor_name 从真源导出，不再借道 view_redo 转手：候选采纳路由退场后
# view_redo 自己不再用它，靠"子模块碰巧还 import 着"来支撑包属性，下一个人删掉
# 那行未使用的 import 就会连带打断这条再导出链（本次已实测踩中）。
from app.auth.principal import current_actor_name as current_actor_name

from .view_redo import (
    _run_portrait_view_redo as _run_portrait_view_redo,
    _run_scene_view_redo as _run_scene_view_redo,
    _start_portrait_view_redo as _start_portrait_view_redo,
    _start_scene_view_redo as _start_scene_view_redo,
    cancel_scene_view_regeneration as cancel_scene_view_regeneration,
    recover_portrait_view_redo_tasks as recover_portrait_view_redo_tasks,
    recover_scene_view_redo_tasks as recover_scene_view_redo_tasks,
    regenerate_character_view_route as regenerate_character_view_route,
    regenerate_scene_view_route as regenerate_scene_view_route,
    rollback_scene_reference as rollback_scene_reference,
)

from .manual_character import (
    add_manual_character as add_manual_character,
    replace_character_portrait_image as replace_character_portrait_image,
)

from .manual_scene import (
    add_manual_scene as add_manual_scene,
    replace_scene_image as replace_scene_image,
    rollback_manual_scene_image as rollback_manual_scene_image,
)
