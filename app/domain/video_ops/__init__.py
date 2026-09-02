"""视频（Video）生成、确认、完成态与整项目补齐队列的路由与业务逻辑。

``app/domain/video_ops.py``（4,984 行 / 100 个顶层定义）拆成本包，按关注点分 16 个子模块：分镜确认资格评估（confirmation_eval，单函数 evaluate_storyboard_for_confirmation 264 行不拆）、参考图丢弃/恢复（reference_images）、供应商能力探测（capability）、镜头版本采纳（adopt）、整项目补齐队列共享原语（project_queue_core，含 ``_project_video_queue_pause_requests`` 模块级单例）、确认预览与生成闸门（confirmation_gate）、生成计划创建/校验/执行（plan）、整集/单镜头生成发起（generate，单函数 _generate_episode_core 351 行不拆）、成片合成任务体（completion_core，单函数 _complete_episode_core 440 行不拆）、产物清空（clear）、续跑/停止（resume_episode）、分镜确认落地（confirm_episode）、完成态用户契约投影（completion_contract）、补齐队列单次运行与开机恢复（project_queue_run）、整项目批量补齐主入口（project_queue_complete，单函数 _complete_project_videos_core 493 行不拆）、混音/拼接/陈旧素材（misc）。移动未重写，逻辑/签名/格式不变；四个单函数子模块不进一步拆分的原因同上（各自是其状态机判据/资源收口的唯一权威执行顺序）。

``video_ops`` <-> ``storyboard_ops``、``video_ops`` <-> ``review_wall`` 在整个``app.domain`` 包级别是真实双向依赖（见 ``app/domain/__init__.py`` 模块 docstring）：本包对 ``app.domain.storyboard_ops`` 的引用（2 个名字：``_board_from_shot_rows``/``_finalize_storyboard_evidence``）与对 ``app.domain.review_wall`` 的引用（7 个名字）都在 ``generate.py``/``project_queue_complete.py`` 等子模块的模块级 import 中，原样保留（不是本次新增，跨包引用不受本包内部拆分影响）。账号即项目空间落地后 ``_require_video_clear_write_scope``（团队角色写权限闸门）已随角色模型一并退场，见 ``app/domain/video_ops/clear.py``。本包内部的调用图本身是一个 DAG（用 Tarjan 算法核验过，不存在非平凡强连通分量）。

本文件是稳定入口：所有既有 ``from app.domain.video_ops import X`` / ``app.domain.video_ops.X`` 调用点必须原样可用——每个符号（含每个子模块自己``import`` 进来的名字，不只是它原生定义的）用 ``name as name`` 显式再导出（PEP 484 显式重导出写法，与 ``app/domain/bible_ops/__init__.py``、``app/domain/storyboard_ops/__init__.py`` 同一先例），而不是 ``from .x import *``（``app/FILE_CONVENTIONS.toml`` 的 ``star_import`` 闸门禁止后者）。新增视频相关逻辑请加进对应关注点的子模块，不要加回本文件。
"""
from __future__ import annotations

# shot_cost_cny 曾经过 .confirmation_eval 转手再导出；成本预算拦截体系退场
# 后 confirmation_eval.py 不再需要它（estimated_cost_cny 字段已随之删除），
# 但这里仍是既有对外 API 面的一部分，改成直接从权威定义处导入——见
# CLAUDE.md「再导出门面...写 from x import y as y，不要借道某个碰巧 import
# 了它的子模块转手」。
from app.compiler import shot_cost_cny as shot_cost_cny

from .confirmation_eval import (
    Bible as Bible,
    ConfirmationEvaluation as ConfirmationEvaluation,
    EpisodeScreenplay as EpisodeScreenplay,
    Shot as Shot,
    Storyboard as Storyboard,
    _board_from_shot_rows as _board_from_shot_rows,
    _compact_episode_target as _compact_episode_target,
    _evaluate_storyboard_pack_for_confirmation as _evaluate_storyboard_pack_for_confirmation,
    _is_storyboard_terminal_for_confirmation as _is_storyboard_terminal_for_confirmation,
    _shot_contract_json as _shot_contract_json,
    _storyboard_confirmation_progress as _storyboard_confirmation_progress,
    _storyboard_operational_projection_errors as _storyboard_operational_projection_errors,
    _storyboard_structural_errors as _storyboard_structural_errors,
    annotations as annotations,
    compile_prompt as compile_prompt,
    evaluate_storyboard_for_confirmation as evaluate_storyboard_for_confirmation,
    json as json,
    normalize_continuity as normalize_continuity,
    normalize_offbible_characters as normalize_offbible_characters,
    normalize_transition_visuals as normalize_transition_visuals,
    validate_storyboard as validate_storyboard,
    validate_storyboard_preserves_key_content as validate_storyboard_preserves_key_content,
    validate_storyboard_soundtrack as validate_storyboard_soundtrack,
)

from .reference_images import (
    Body as Body,
    HTTPException as HTTPException,
    _as_body_dict as _as_body_dict,
    _review_assert_reference_restore as _review_assert_reference_restore,
    _review_write_audit as _review_write_audit,
    _set_reference_image_used as _set_reference_image_used,
    discard_reference_image as discard_reference_image,
    get_conn as get_conn,
    now as now,
    restore_reference_image as restore_reference_image,
    router as router,
)

from .capability import (
    asyncio as asyncio,
    create_provider_media_publication as create_provider_media_publication,
    get_job_video_mode_audit as get_job_video_mode_audit,
    get_video_capabilities as get_video_capabilities,
    new_id as new_id,
    probe_video_capability as probe_video_capability,
    time as time,
)

from .adopt import (
    Evaluation as Evaluation,
    _adopt_version_core as _adopt_version_core,
    _cancel_shot_adoption_core as _cancel_shot_adoption_core,
    _review_assert_shot_positive as _review_assert_shot_positive,
    adopt_version as adopt_version,
    cancel_shot_adoption as cancel_shot_adoption,
    current_actor_name as current_actor_name,
    evidence_repository as evidence_repository,
    worker as worker,
)

from .project_queue_core import (
    _PROJECT_VIDEO_CHILD_WAIT_STATUSES as _PROJECT_VIDEO_CHILD_WAIT_STATUSES,
    _PROJECT_VIDEO_ITEM_SUCCESS_STATUSES as _PROJECT_VIDEO_ITEM_SUCCESS_STATUSES,
    _authoritative_project_video_child_run as _authoritative_project_video_child_run,
    _finish_project_video_completion_queue as _finish_project_video_completion_queue,
    _persist_project_video_queue as _persist_project_video_queue,
    _project_video_queue_pause_requests as _project_video_queue_pause_requests,
    _propagate_project_video_child_status as _propagate_project_video_child_status,
    clear_project_video_queue_pause as clear_project_video_queue_pause,
    request_project_video_queue_pause as request_project_video_queue_pause,
)

from .confirmation_gate import (
    _assert_storyboard_generation_gate as _assert_storyboard_generation_gate,
    _episode_or_404 as _episode_or_404,
    _has_current_storyboard_completion_certificate as _has_current_storyboard_completion_certificate,
    _project_bible_or_placeholder as _project_bible_or_placeholder,
    _restore_unconfirmed_storyboard_projection as _restore_unconfirmed_storyboard_projection,
    confirm_episode_preview as confirm_episode_preview,
    create_storyboard_confirmation_preview as create_storyboard_confirmation_preview,
)

from .plan import (
    _review_sha as _review_sha,
    create_episode_video_generation_plan as create_episode_video_generation_plan,
    execute_episode_video_generation_plan as execute_episode_video_generation_plan,
    get_episode_video_generation_plan as get_episode_video_generation_plan,
    override_episode_video_generation_plan as override_episode_video_generation_plan,
    reconcile_episode_video_generation_plan as reconcile_episode_video_generation_plan,
    validate_episode_video_generation_plan as validate_episode_video_generation_plan,
)

from .generate import (
    Path as Path,
    _adopt_reused_completed_version as _adopt_reused_completed_version,
    _generate_episode_core as _generate_episode_core,
    _generate_shot_core as _generate_shot_core,
    _review_assert_positive_action as _review_assert_positive_action,
    _shot_by_no as _shot_by_no,
    errors as errors,
    generate_episode as generate_episode,
    generate_shot as generate_shot,
    stop_shot_video as stop_shot_video,
    task_registry as task_registry,
)

from .completion_core import (
    _complete_episode_core as _complete_episode_core,
    _ensure_video_episode_columns as _ensure_video_episode_columns,
    _recorded_video_completion_task as _recorded_video_completion_task,
    _review_validate_authorization_number as _review_validate_authorization_number,
    complete_episode as complete_episode,
)

from .clear import (
    _require_provider_clearance as _require_provider_clearance,
    _review_upstream_snapshot as _review_upstream_snapshot,
    _shot_clear_context as _shot_clear_context,
    clear_episode_artifacts as clear_episode_artifacts,
    clear_episode_videos as clear_episode_videos,
    clear_shot_artifacts as clear_shot_artifacts,
    clear_shot_references as clear_shot_references,
    clear_shot_videos as clear_shot_videos,
    delete_version as delete_version,
    reconcile_episode_provider_tasks as reconcile_episode_provider_tasks,
    reset_video_completion as reset_video_completion,
    reset_video_completion_state as reset_video_completion_state,
)

from .resume_episode import (
    resume_episode as resume_episode,
    stop_episode_video as stop_episode_video,
)

from .confirm_episode import (
    _confirm_episode_core_impl as _confirm_episode_core_impl,
    _converge_confirmed_storyboard_state as _converge_confirmed_storyboard_state,
    _finalize_storyboard_evidence as _finalize_storyboard_evidence,
    confirm_episode as confirm_episode,
    confirm_episode_core as confirm_episode_core,
)

from .completion_contract import (
    Any as Any,
    _resume_prepared_complete_episode_operation as _resume_prepared_complete_episode_operation,
    _video_completion_user_contract as _video_completion_user_contract,
    get_video_completion as get_video_completion,
    preview_video_completion_repair_route as preview_video_completion_repair_route,
    repair_video_completion_route as repair_video_completion_route,
)

from .project_queue_run import (
    _run_project_video_completion_queue as _run_project_video_completion_queue,
    recover_project_video_completion_queues as recover_project_video_completion_queues,
)

from .project_queue_complete import (
    _complete_project_videos_core as _complete_project_videos_core,
    complete_project_videos as complete_project_videos,
)

from .misc import (
    concatenate as concatenate,
    mix_status as mix_status,
    repair_stale_assets as repair_stale_assets,
    stale_assets_preview as stale_assets_preview,
)
