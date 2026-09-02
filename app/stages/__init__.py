"""LLM 流水线阶段包：摘要 / 角色圣经 / 剧集规划 / 可拍剧本 / 分镜脚本。

原 app/stages.py（12,142 行 / 174 个顶层定义）按关注点拆分为本包下的多个模块：
剧本 IR 保真（ir_*）、叙事蓝图分片与重试预算（blueprint_*）、人物谱生成与补充
（bible_*）、别名取证（identity_evidence.py / alias_*.py）、状态事实回填
（status_facts_*.py）、章节认知卡（cognition.py），外加两个跨关注点共用的
基础设施模块（common.py / constants.py / bible_shared.py / screenplay_source.py）。
旧点名与身份归并管线（roster_*.py 共 7 个模块）与旁文本净化（bible_paratext.py）
已于 2026-09-01 整体退场：generate_bible 不再点名角色，这条管线除测试外零生产
调用方，7+1 个模块彼此连通、构成一条完整链路，随本轮一并删除。

本文件是唯一的稳定入口：全仓所有 `from app.stages import X` / `import app.stages` /
`stages.X` 使用方式必须不经改动继续可用——下面按来源模块显式再导出每一个符号
（禁止 `from .x import *`，见 app/FILE_CONVENTIONS.toml 的 star_import 闸门）。
新增阶段逻辑请加进对应关注点的子模块，不要加回本文件。
"""
from __future__ import annotations

from app.source_excerpt import index_source_segments

from .alias_backfill import (
    ALIAS_BACKFILL_SOURCE_BUDGET_CHARS,
    Character,
    CharacterAlias,
    Field,
    _ALIAS_VERDICT_NO_MATCH_LABEL,
    _AliasBackfillDeclaration,
    _AliasBackfillDraft,
    _alias_declaration_verified,
    _alias_evidence_resolution,
    _alias_verdict_call,
    _alias_verdict_candidates,
    _alias_verdict_dossier,
    _alias_verdict_pin_segment,
    _alias_verdict_roster,
    _chapters_by_idx,
    _find_alias_bridge_chapter,
    _render_alias_backfill_source,
    _verify_character_aliases_for_subset,
    _verify_character_aliases_in_place,
    asyncio,
    backfill_character_aliases,
    build_chapter_cognition_card,
    defaultdict,
    reverify_character_aliases,
)
from .alias_verdict import (
    ChapterCognitionCard,
    _ALIAS_VERDICT_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS,
    _ALIAS_VERDICT_DOSSIER_MAX_CHARS,
    _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES,
    _ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES,
    _ALIAS_VERDICT_DOSSIER_TRUNCATION_MARK,
    _AliasExclusivityVerdictResponse,
    _AliasVerdictResponse,
    _alias_verdict_dossier_truncate_segment,
    _cognition_status_lines,
    re,
)
from .bible_generate import (
    AgentLoopPolicy,
    Scene,
    _BibleRosterEntry,
    _CharacterDetail,
    _SceneBibleDraft,
    _appearance_evidence_verified,
    _render_bible_source,
    _sanitize_character_detail_payload,
    _validate_appearance_evidence,
    generate_bible,
    generate_scene_bible,
    get_setting,
    time,
    validate_bible,
    validate_scene_bible,
)
from .bible_models import (
    AppearanceEvidence,
    Literal,
    Relationship,
    World,
)
from .bible_shared import (
    BIBLE_FIRST_TOKEN_TIMEOUT_S,
    _BIBLE_TAIL_SAMPLE_MAX,
    _BIBLE_TAIL_SLICE_CHARS,
)
from .blueprint_budget import BLUEPRINT_CALL_ABANDONED_BY_DELETE, _BlueprintGenerationBudget
from .blueprint_budget_trace import (
    _blueprint_generation_budget_for_trace,
    _blueprint_shard_source_entry,
    _cached_leaf_superseded_by_feedback,
    blueprint_retry_receipts_hash,
    source_segment_facts,
)
from .blueprint_checkpoint import (
    BLUEPRINT_VERSION,
    BlueprintSourceOccurrenceError,
    BlueprintSourceOwnershipError,
    _artifact_json_content_is_sealed,
    _blueprint_authority_snapshot_is_current,
    _clear_ungrounded_ending_hook,
    _commit_blueprint_authority_checkpoint,
    _narrative_blueprint_content_hash,
    _run_screenplay_workflow_step,
    _save_screenplay_generation_checkpoint,
    derive_blueprint_scene_plans,
    ending_hook_grounding_report,
    validate_blueprint_scene_partition,
    validate_narrative_blueprint,
)
from .blueprint_freeze import (
    AUDIBLE_SOURCE_DELIVERY_MODES,
    NarrativeBlueprintShard,
    _BLUEPRINT_SOURCE_UNIT_KEY_PATTERN,
    _freeze_unreported_state_subject_ownership,
    _freeze_unreported_voice_pairs,
)
from .blueprint_generate_entry import (
    BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE,
    _generate_screenplay_narrative_blueprint,
    _generate_sharded_narrative_blueprint,
    _render_screenplay_source,
    _repair_narrative_blueprint,
    _semantic_review_narrative_blueprint,
    blueprint_prompt_contract,
    normalize_blueprint_fact_versions,
    normalize_blueprint_requirement_state_keys,
    render_indexed_source,
    screenplay_ir_bible_context,
)
from .blueprint_generate_sharded import (
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BlueprintStateSubjectOwnershipPatch,
    SOURCE_FACT_VERSION,
    _blueprint_leaf_plan_from_cache,
    _blueprint_provider_operation_id,
    _blueprint_shard_boundary_context,
    _blueprint_shard_prompt,
    _blueprint_shard_token_budget,
    _blueprint_state_subject_repair_issues,
    _blueprint_state_subject_repair_prompt,
    _blueprint_state_subject_repair_target_keys,
    _current_blueprint_authority_snapshot,
    _namespace_blueprint_shard,
    _normalize_blueprint_shard_structure,
    _split_blueprint_segments,
    apply_blueprint_state_subject_ownership_patch,
    hiagent,
    normalize_blueprint_provider_payload,
    normalize_blueprint_state_subject_evidence_projection,
    normalize_blueprint_state_subject_perception,
    structural_front_matter_ids,
    validate_narrative_blueprint_shard,
)
from .blueprint_ownership_repair import (
    _blueprint_exact_ownership_claims,
    _blueprint_structured_operation_id,
    _repair_reviewed_blueprint_state_subject_ownership,
    blueprint_authority_validator_fingerprint,
    blueprint_state_subject_ownership_patch_schema,
    source_facts,
)
from .blueprint_prompt import (
    _blueprint_format_repair_reservation_operation_id,
    blueprint_shard_candidate_hash,
    blueprint_shard_provider_schema,
    blueprint_state_subject_issues,
    render_blueprint_shard_semantic_issue,
)
from .blueprint_repair import (
    ContentGenerationError,
    NarrativeBlueprintPatch,
    apply_narrative_blueprint_patch,
    blueprint_patch_schema,
    normalize_blueprint_agency_continuity,
    validate_narrative_blueprint_patch_projection,
)
from .blueprint_semantic_review import (
    BlueprintSemanticReview,
    _blueprint_review_sample_is_undelivered,
    _blueprint_semantic_issue_exact_scope,
    _blueprint_semantic_issue_has_deterministic_authority,
    blueprint_semantic_issue_is_resolved,
    blueprint_semantic_review_schema,
    blueprint_semantic_voice_issue_has_dialogue_authority,
    filter_blueprint_semantic_review_voice_issues,
    normalize_blueprint_semantic_review_payload,
    validate_blueprint_semantic_review,
)
from .blueprint_shard_structure import (
    BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD,
    BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD,
    _blueprint_node_has_operational_authority,
    _blueprint_segment_output_weight,
    _collapse_nonoperational_duplicate_source_nodes,
    _partition_blueprint_segments,
    _remove_duplicate_repair_orphan_nodes,
    blueprint_source_occurrence_issues,
)
from .cognition import (
    CHAPTER_COGNITION_CARD_MAX_CHARACTERS,
    CHAPTER_COGNITION_FACTS_MAX_PER_KIND,
    CHAPTER_COGNITION_SUMMARY_MAX_CHARS,
    ChapterCognitionEntry,
    CharacterAffiliation,
    CharacterRelation,
    _cognition_affiliation_summary,
    _cognition_relation_summary,
    _status_facts_as_of_chapter,
)
from .common import (
    AgentLoop,
    AgentLoopFailure,
    Any,
    BIBLE_FORMAL_NAME_MIN_RATIO,
    BIBLE_HEAD_CHAPTERS,
    BIBLE_LOOKAHEAD_CHAPTERS,
    BIBLE_MUST_COVER_MAX,
    BIBLE_RECURRING_MIN_ONSTAGE_CHAPTERS,
    BIBLE_RECURRING_MIN_ONSTAGE_QUOTES,
    BIBLE_ROLL_CALL_CHUNK_CHAPTERS,
    BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS,
    BIBLE_ROLL_CALL_CHUNK_MAX_TOKENS,
    BIBLE_ROLL_CALL_CONCURRENCY,
    BIBLE_ROLL_CALL_MAX_ATTEMPTS,
    BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE,
    BIBLE_ROLL_CALL_TIMEOUT_S,
    BIBLE_SMALL_VERDICT_TIMEOUT_S,
    BIBLE_STATISTICAL_MIN_CHAPTER_RATIO,
    BIBLE_STATISTICAL_MIN_MENTIONS,
    BaseModel,
    Bible,
    Callable,
    EpisodeScreenplay,
    IR_COMPILER_VERSION,
    IR_VERSION,
    Issue,
    NarrativeBlueprint,
    ScreenplayGenerationIR,
    StageError,
    StoryboardOutline,
    StoryboardOutlineShot,
    _bible_short_json_call_meta,
    _render_error_history,
    _run_with_agent_loop,
    adaptation_hook_errors,
    config,
    deepcopy,
    extract_json,
    hashlib,
    issues_from_messages,
    log_provider_call,
    model_gateway,
    normalize_blueprint_raw_json,
    normalize_screenplay_ir_payload,
    normalize_screenplay_json_shape,
    normalize_storyboard_outline_candidate,
    recover_complete_blueprint_prefix,
    recover_complete_screenplay_ir_prefix,
    schema_errors,
)
from .constants import (
    BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS,
    BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS,
    BLUEPRINT_GENERATION_MAX_SPLIT_DEPTH,
    BLUEPRINT_GENERATION_MAX_WALL_SECONDS,
    BLUEPRINT_LEAF_CALL_HEADROOM,
    BLUEPRINT_LEAF_PROVIDER_CALLS,
    BLUEPRINT_PROMPT_VERSION,
    BLUEPRINT_REVIEW_FORMAT_RETRY_LIMIT,
    BLUEPRINT_REVIEW_MAX_TOKENS,
    BLUEPRINT_REVIEW_PROVIDER_RETRY_LIMIT,
    BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
    BLUEPRINT_SHARD_MAX_ATTEMPTS,
    BLUEPRINT_SHARD_MAX_STALL_RETRIES,
    BLUEPRINT_SHARD_MAX_TOKENS,
    BLUEPRINT_SHARD_MIN_TOKENS,
    IR_FIDELITY_PATCH_MAX_TOKENS,
    SCREENPLAY_BASELINE_PROMPT_VERSION,
    SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
    SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT,
    SCREENPLAY_IR_MAX_TOKENS,
    SCREENPLAY_IR_MIN_TOKENS,
    SCREENPLAY_STRUCTURAL_BOOTSTRAP_ITERATIONS,
    SYSTEM_PREFIX,
    annotations,
)
from .identity_evidence import (
    APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS,
    _ALIAS_BRIDGE_QUOTE_MAX_CHARS,
    _PAIRED_QUOTE_MARKS,
    _alias_bridge_dual_anchor_quote,
    _alias_bridge_quote,
    _alias_text_is_independent_appellation,
    _quote_comparison_variants,
)
from .ir_snapshot import (
    _select_current_blueprint_artifact,
    screenplay_ir_token_budget,
)
from .screenplay_source import (
    SCREENPLAY_SOURCE_BUDGET_CHARS,
    _SOURCE_QUOTED_DIALOGUE_RE,
    _SOURCE_SPEAKER_DIALOGUE_RE,
    _character_resolution_prompt_block,
    _source_dialogue_evidence,
    identity_resolution_is_authoritative,
    model_identity_authority_prompt_rule,
)
from .status_facts_backfill import (
    _STATUS_FACT_VERDICT_STAGE_KEY,
    _StatusFactAffiliationDeclaration,
    _StatusFactBackfillDraft,
    _StatusFactRelationDeclaration,
    _status_fact_evidence_resolution,
    _status_fact_interval_resolution,
    _status_fact_quote_dual_anchor_verified,
    _status_fact_roster_hint,
    _status_fact_verdict_call,
    backfill_character_status_facts,
)
from .status_facts_verdict import _status_fact_boundary_dual_anchor_verified
