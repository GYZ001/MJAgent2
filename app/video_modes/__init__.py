"""分镜视频生成的参考图/关键帧输入组装（视频生成模式判定与参考资产装配）。

原 app/video_modes.py（4,081 行）按关注点拆分为本包下的多个模块：模式判定与设置读取
（mode_selection.py）、人物/场景图库资产查找（asset_lookup.py）、叙事关键帧契约
（keyframe_contract.py）、参考图 prompt 组装（reference_prompt.py）、单张参考图生成调用
（reference_generate.py）、图库素材整体装配与对外入口 build_reference_assets
（reference_assemble.py）、跨镜连续性尾帧装配（continuity_tail.py）、Seedance 供应商输入
打包（seedance_pack.py）。

旧版逐镜生成流程（``_build_generated_reference_assets_legacy``，曾拆成
reference_generate_legacy*.py 共 9 个文件）已于 2026-08-30 整体删除：2026-08-09
（commit da02e67）起 `build_reference_assets` 已经不再调用它，唯一的存活入口
`_build_library_reference_assets` 只读人物谱/场景库现有图片，不再触发新生成；
它自那之后零测试覆盖、零生产调用方，是纯粹的死代码。

本文件是唯一的稳定入口：全仓所有 `from app.video_modes import X` /
`import app.video_modes` / `video_modes.X` 使用方式必须不经改动继续可用——下面按**真源**
显式再导出每一个符号：包内定义的符号从定义它的子模块导出一次；来自其它模块的符号
（`app.schemas`、`app.db`、`app.hiagent`、`app.video_plan` 等）从其真正的定义模块直接
导出，不借道某个碰巧 import 了它的子模块转手（用 `name as name` 的 PEP 484 显式重导出
写法，不使用 `from .x import *`，见 app/FILE_CONVENTIONS.toml 的 star_import 闸门）。
`config`/`hiagent` 是 `app.config`/`app.hiagent` 两个共享单例模块对象本身，
`tests/test_video_modes_monkeypatch_guard.py` 显式把 `video_modes.config.X` /
`video_modes.hiagent.X` 排除在“包拆分打桩陷阱”之外，因此仍从真源 `app` 导出。
stdlib/typing（`Any`、`Callable`、`Path`、`annotations`、`asdict`、`base64`、
`dataclass`、`field`、`hashlib`、`json`、`re`、`shutil`、`subprocess`）不再作为包属性
导出——纯子模块实现细节导入，全仓 grep 确认没有 `video_modes.<名字>` 读取或打桩依赖。
新增视频模式逻辑请加进对应关注点的子模块，不要加回本文件。
"""
from __future__ import annotations

from app import (
    config as config,
    hiagent as hiagent,
)

from app.atomic_io import (
    atomic_write_bytes as atomic_write_bytes,
)
from app.db import (
    get_setting as get_setting,
    new_id as new_id,
)
from app.hiagent import (
    ProviderError as ProviderError,
)
from app.refs import (
    visual_style_lock as visual_style_lock,
)
from app.schemas import (
    Bible as Bible,
    EpisodeScreenplay as EpisodeScreenplay,
    Shot as Shot,
)
from app.video_plan import (
    VideoGenerationMode as VideoGenerationModeEnum,  # noqa: F401 -- renamed re-export
    VideoInputIntent as VideoInputIntent,
)

from .asset_lookup import (
    _asset_from_path as _asset_from_path,
    _safe_ref_name as _safe_ref_name,
    character_reference_assets as character_reference_assets,
    reference_image_path as reference_image_path,
    scene_reference_assets as scene_reference_assets,
)
from .continuity_tail import (
    _apply_redundancy_penalties as _apply_redundancy_penalties,
    _finalize_reference_selection as _finalize_reference_selection,
    assemble_continuity_tail as assemble_continuity_tail,
)
from .keyframe_contract import (
    _keyframe_character_anchors as _keyframe_character_anchors,
    _keyframe_contract as _keyframe_contract,
    _keyframe_contract_instructions as _keyframe_contract_instructions,
    _keyframe_text_instruction as _keyframe_text_instruction,
    is_narrative_keyframe_slot as is_narrative_keyframe_slot,
    keyframe_contract_fingerprint as keyframe_contract_fingerprint,
)
from .mode_selection import (
    FIRST_FRAME_MODE as FIRST_FRAME_MODE,
    FIRST_LAST_FRAME_MODE as FIRST_LAST_FRAME_MODE,
    KEYFRAME_PROMPT_CONTRACT_VERSION as KEYFRAME_PROMPT_CONTRACT_VERSION,
    KEYFRAME_STRUCTURAL_FALLBACK_MODE as KEYFRAME_STRUCTURAL_FALLBACK_MODE,
    REFERENCE_IMAGE_MODE as REFERENCE_IMAGE_MODE,
    REFERENCE_IMAGE_TYPES as REFERENCE_IMAGE_TYPES,
    REFERENCE_INPUT_POLICY_VERSION as REFERENCE_INPUT_POLICY_VERSION,
    ReferenceImageAsset as ReferenceImageAsset,
    ReferenceImagePlan as ReferenceImagePlan,
    ShotVideoModeDecision as ShotVideoModeDecision,
    ShotVideoModeSelector as ShotVideoModeSelector,
    VIDEO_INPUT_MODE as VIDEO_INPUT_MODE,
    VideoGenerationMode as VideoGenerationMode,
    _DEFAULT_KEYFRAME_CANDIDATE_COUNT as _DEFAULT_KEYFRAME_CANDIDATE_COUNT,
    _KEYFRAME_LLM_PROMPT_MAX_CHARS as _KEYFRAME_LLM_PROMPT_MAX_CHARS,
    _MAX_REDUNDANCY_PENALTY as _MAX_REDUNDANCY_PENALTY,
    _MAX_TIMELINE_KEYFRAMES as _MAX_TIMELINE_KEYFRAMES,
    _MULTI_KEYFRAME_INVARIANCE_NOTE as _MULTI_KEYFRAME_INVARIANCE_NOTE,
    _SHORT_SHOT_MAX_SECONDS as _SHORT_SHOT_MAX_SECONDS,
    _dedupe_str as _dedupe_str,
    _parse_ref_prompts as _parse_ref_prompts,
    _reference_runtime_blocking as _reference_runtime_blocking,
    _screenplay_call_kwargs as _screenplay_call_kwargs,
    batch_prompt_enabled as batch_prompt_enabled,
    bool_setting as bool_setting,
    decision_to_dict as decision_to_dict,
    default_reference_decision as default_reference_decision,
    dict_to_decision as dict_to_decision,
    estimated_keyframe_generation_count as estimated_keyframe_generation_count,
    float_setting as float_setting,
    int_setting as int_setting,
    keyframe_candidate_count as keyframe_candidate_count,
    max_character_reference_images as max_character_reference_images,
    max_reference_images as max_reference_images,
    min_generated_references as min_generated_references,
    reference_prompt_async as reference_prompt_async,
    role_adaptive_enabled as role_adaptive_enabled,
    supporting_keyframe_candidate_count as supporting_keyframe_candidate_count,
)
from .reference_assemble import (
    _build_library_reference_assets as _build_library_reference_assets,
    _enforce_reference_consistency as _enforce_reference_consistency,
    build_reference_assets as build_reference_assets,
)
from .reference_generate import (
    _SEED_USAGE_NOTE as _SEED_USAGE_NOTE,
    _extract_last_frame as _extract_last_frame,
    _generate_image_with_seed_fallback as _generate_image_with_seed_fallback,
    _generate_one_reference as _generate_one_reference,
    _portrait_seed_inputs as _portrait_seed_inputs,
    previous_tail_reference_asset as previous_tail_reference_asset,
    previous_tail_source_contract as previous_tail_source_contract,
)
from .reference_prompt import (
    _photographic_medium_instruction as _photographic_medium_instruction,
    _seeded_structured_endpoint as _seeded_structured_endpoint,
    reference_gallery_matches_keyframe_contract as reference_gallery_matches_keyframe_contract,
    reference_gallery_matches_library_policy as reference_gallery_matches_library_policy,
    reference_generation_prompt as reference_generation_prompt,
)
from .seedance_pack import (
    REFERENCE_PROMPT_NOTE_MARKER as REFERENCE_PROMPT_NOTE_MARKER,
    REFERENCE_SINGLE_INSTANCE_NOTE as REFERENCE_SINGLE_INSTANCE_NOTE,
    _CONTINUITY_FRAME_LABELS as _CONTINUITY_FRAME_LABELS,
    _dedupe_assets as _dedupe_assets,
    _reference_identity_names as _reference_identity_names,
    _reference_input_label as _reference_input_label,
    append_reference_prompt_notes as append_reference_prompt_notes,
    append_reference_prompt_notes_from_dicts as append_reference_prompt_notes_from_dicts,
    build_seedance_image_inputs as build_seedance_image_inputs,
    build_seedance_video_inputs as build_seedance_video_inputs,
    dedupe_reference_dicts as dedupe_reference_dicts,
    pack_reference_images_for_seedance as pack_reference_images_for_seedance,
)

