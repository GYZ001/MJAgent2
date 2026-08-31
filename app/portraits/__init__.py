"""人物定妆照（跨集一致性增强，PRD §5.4 第 2 层的时间维扩展）。

定妆照按"适用集区间"分段存于 character_portraits（ep_start/ep_end，ep_end=NULL 表示开区间=当前最新版）。
两条反应式产生路径都按集触发、不做全量轮询。新角色发现挂在【剧本阶段】并在正式剧本校验前完成，
分镜阶段保留幂等兜底；已有角色外观漂移仍在分镜展开前处理：
  ① 新角色发现：剧本里出现、人物谱里没有、戏份够的角色 → 建卡 + 定妆，适用集从首次出场那集起开放。
  ② 已有角色按集漂移：剧本里出现、本集之前已有定妆照的角色 → 用【本集源文】判断外观相比当前锚点
     是否明显变化：
       - 变化不大 → 沿用当前定妆照（开区间自然向后覆盖），不重绘、不花钱；
       - 变化很大 → 关闭当前定妆照右区间（= 本集-1），以当前定妆照为底【图生图】重绘新定妆照
         （左区间=本集、右区间开放），并把 bible 该角色锚点同步成最新（供人物谱 UI 展示）。

生成台/关键帧出图时按集号选用覆盖该集的定妆照与外观锚点：图走 portrait_for_episode，文字锚点走
bible_for_episode（把 bible 换成"本集视图"），二者同段同源（见 app.refs / app.video_modes / app.worker）。

原 app/portraits.py（10,821 行）按关注点拆分为本包下的 24 个模块：常量与身份词法
（constants.py / _identity_tokens.py）、DB 探测（_db_probe.py）、新角色发现主链路
（discovery*.py）、当代身份证据与合并（evidence_*.py）、身份候选 schema
（identity_schemas.py / identity_response_projection.py）、剧本身份归并落库
（resolution_*.py）、结构化身份覆盖度（structural_coverage*.py）、人物卡
（cards*.py）、定妆照漂移与 I/O（portrait_drift.py / portrait_io.py）。

本文件是唯一的稳定入口：全仓所有 `from app.portraits import X` / `import
app.portraits` / `portraits.X` 使用方式必须不经改动继续可用——下面按来源模块显式
再导出每一个符号（禁止 `from .x import *`，见 app/FILE_CONVENTIONS.toml 的
star_import 闸门）。新增人物谱/定妆照逻辑请加进对应关注点的子模块，不要加回本文件。

~170 个既有测试用 `monkeypatch.setattr(app.portraits, "name", stub)` 打桩——这个包
拆分前是单文件，所有跨 chunk 调用共享同一个模块命名空间，打包级别的补丁天然打到每
一处。拆成真包后，每个子模块对它导入的名字持有自己的独立拷贝，只打包级别的
re-export 属性不会到达真正调用该名字的子模块（补丁看似生效、实则没有拦到任何调
用）。修复方式与 `app/stages/__init__.py`、`app/validators/__init__.py` 等历史拆包
一致：`tests/conftest.py` 的 `patch_portraits_everywhere(monkeypatch, name, value)`
遍历 `app.portraits` 的每个子模块、在真正绑定该名字的地方打桩，配套的
`tests/test_portraits_monkeypatch_guard.py` 用 AST 扫描全部测试文件，裸形态的
`monkeypatch.setattr(portraits, ...)` / 字符串形式 `"app.portraits.name"` 会被判红。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import config, hiagent, textmatch
from app.atomic_io import atomic_write_bytes
from app.character_policy import resolution_declares_functional_identity
from app.db import get_conn, get_setting, new_id, now, set_setting
from app.evidence import repository as evidence_repository
from app.evidence.media import record_reference_asset
from app.errors import ContentGenerationError, code_ref
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.identity_authority import (
    IdentityAuthorityConflictError,
    identity_authority_registry,
    identity_resolution_is_authoritative,
    normalize_character_resolution,
    normalize_character_resolutions,
)
from app.orchestration.state_machine import StateConflict
from app.ingest import chapter_is_stub, chapter_titles_match
from app.refs import (
    PRODUCTION_APPEARANCE_MAX_CHARS,
    PRODUCTION_APPEARANCE_MIN_CHARS,
    _safe_name,
    portrait_prompt,
    production_appearance_anchor,
)
from app.schemas import Bible, Character, EpisodeScreenplay, extract_json
from app.source_excerpt import (
    SourceSegment,
    align_source_excerpt,
    index_source_segments,
)

from ._db_probe import (
    _has_column,
    _has_table,
)
from ._identity_tokens import (
    _IDENTITY_DISAMBIGUATING_ORDINALS,
    _IDENTITY_LIST_SEPARATOR_PATTERN,
    _identity_disambiguating_suffix,
    _identity_list_tokens,
    _identity_source_label_has_list_separator,
    _project_identity_token,
    _visual_entity_id_for_resolution_safe,
)
from .cards import (
    CHARACTER_CARD_ROLES,
    CHARACTER_SUBJECT_PERSON,
    _candidate_requires_identity_card,
    assess_new_character,
    bible_with_pending_characters_for_text,
    bible_with_provisional_characters,
    ensure_character_card,
)
from .cards_ensure import ensure_cards_for_text
from .constants import (
    APPEARANCE_MAX,
    APPEARANCE_MIN,
    AUTOMATIC_IDENTITY_DECISION_PROVENANCE,
    CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET,
    CAST_DISCOVERY_SOURCE_BUDGET,
    CHARACTER_CARD_MAX_TOKENS,
    CURRENT_IDENTITY_DECISION_VERSION,
    CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION,
    CURRENT_IDENTITY_LITERAL_PROVENANCE,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    DURABLE_IDENTITY_DECISION_PROVENANCE,
    FRAGMENT_BUDGET,
    FRAGMENT_WINDOW,
    FUTURE_IDENTITY_DECISION_VERSION,
    IDENTITY_ADJUDICATION_SOURCE_PROVENANCE,
    IDENTITY_DISCOVERY_CONTRACT_VERSION,
    IDENTITY_NAME_FORMS,
    IDENTITY_NAME_FORM_HONORIFIC,
    IDENTITY_NAME_FORM_PERSONAL,
    IDENTITY_NAME_FORM_REFERENTIAL,
    IDENTITY_NAME_FORM_RULE,
    IDENTITY_REQUEST_MAX_TOKENS,
    IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH,
    REISSUE_KNOWN_RESOLUTION_KIND,
    STAGED_INITIAL_EP_START,
    STRUCTURAL_IDENTITY_COVERAGE_VERSION,
)
from .current_ref import current_portrait_ref, portrait_for_episode
from .discovery import discover_character_candidates
from .discovery_fragments import (
    CHARACTER_IMPORTANCE_FORWARD_CHAPTERS,
    DISCOVERY_REJUDGE_WINDOW,
    IDENTITY_DISCOVERY_FORWARD_CHAPTERS,
    _IDENTITY_CARRIER_ANNOTATION_RE,
    _aligned_identity_source_label,
    _bible_lock,
    _bible_locks,
    _bible_locks_guard,
    _card_lock,
    _card_locks,
    _card_locks_guard,
    _discovery_skip_key,
    _distributed_identity_fragments,
    _draft_identity_projection,
    _forward_fragments,
    _future_chapter_context,
    _future_identity_context,
    _identity_carrier_annotation_base,
    _name_in_bible,
    _non_character_skip_key,
)
from .discovery_legacy import (
    _current_identity_projection_errors,
    _discover_character_candidates_legacy,
    _record_current_identity_absorbed_visual_merges,
    _record_visual_entity_merge,
)
from .discovery_resample import (
    IDENTITY_RESAMPLE_FORMAT_REMINDER,
    IDENTITY_RESAMPLE_TEMPERATURE_BUMP,
    IDENTITY_RESAMPLE_TEMPERATURE_CAP,
    IDENTITY_UNUSABLE_RESPONSE_RESAMPLES,
    _bounded_owned_identity_evidence,
    _canonical_named_authority_id,
    _identity_operation_retry_epoch,
    _identity_structured_with_resample,
    _named_candidate_materialization_compatible,
    extract_character_fragments,
    screen_appearance_changes,
    screenplay_identity_scope_fingerprint,
)
from .evidence_catalog import (
    _current_identity_evidence_batches,
    _current_identity_evidence_catalog_hash,
    _current_identity_evidence_payload,
    _current_identity_evidence_receipt_is_valid,
    _current_identity_evidence_records,
    _current_identity_known_decision_catalog,
    _current_identity_prior_decision_catalog,
    _seal_current_identity_evidence,
)
from .evidence_merge import (
    _CURRENT_IDENTITY_DECISION_CAP_FLOOR,
    _CURRENT_IDENTITY_DECISION_CAP_PER_REF,
    _CURRENT_KNOWN_BACKEND_OWNED_ECHO_KEYS,
    _CurrentIdentitySchemaViolation,
    _current_identity_decision_cap,
    _current_identity_declared_signature,
    _current_identity_disambiguation_key,
    _current_identity_durable_signature,
    _current_identity_is_schema_violation,
    _current_identity_receipt_sort_key,
    _current_identity_reconcile_as_single,
    _current_identity_semantic_signature,
    _identity_form_functional_key,
    _merge_current_identity_occurrences,
    _normalize_current_identity_payload,
    _resolved_evidence_ref,
)
from .evidence_receipt import (
    _attach_candidate_source_evidence,
    _validate_current_identity_receipt_bundle,
    extract_current_identity_candidates,
)
from .future_identity_resolution import resolve_future_identity_candidates
from .identity_response_projection import _project_current_identity_response
from .identity_schemas import (
    CurrentFunctionalIdentityDecision,
    CurrentIdentityCandidateResponse,
    CurrentKnownIdentityDecision,
    CurrentNewNamedIdentityDecision,
    FutureIdentityCandidateResponse,
    StructuralIdentityCoverageResponse,
    _IDENTITY_COVERAGE_STRICT_PROVIDER_SCHEMA_KEYWORDS,
    _current_identity_schema,
    _future_identity_schema,
    _identity_coverage_strict_provider_schema,
    _identity_strict_provider_schema,
    _identity_strict_response_format,
    _structural_identity_coverage_response_format,
    _structural_identity_coverage_schema,
)
from .portrait_drift import (
    _backfill_matching_future_portrait,
    _episode_source_text,
    _refresh_portrait_on_drift,
    ensure_cards_for_screenplay,
    reconcile_bible_display_appearances,
)
from .portrait_io import (
    _append_character_to_bible,
    _generate_discovered_character_portrait,
    _generate_fresh_portrait,
    _new_portrait_path,
    _open_portrait,
    _portrait_dir,
    _redraw_portrait,
    _review_portrait_asset,
    _save_image_item,
    _update_bible_appearance,
    appearance_for_episode,
    bible_for_episode,
    portrait_views_for_episode,
    promote_staged_initial_portrait,
    redraw_prompt,
    register_initial_portrait,
    stage_initial_portrait,
)
from .resolution_apply_labels import (
    _identity_value_contains,
    _merge_duplicate_narrative_identity_contracts,
    _replace_identity_list_label,
    _replace_identity_value,
    _replace_narrative_plan_identity,
    _replace_resolved_label,
    _replace_screenplay_body_label,
    _restore_non_dialogue_prefix,
)
from .resolution_apply_screenplay import (
    apply_screenplay_character_resolutions,
    normalize_screenplay_identity_annotations,
    normalize_screenplay_offscreen_visual_identities,
    normalize_screenplay_voice_ids,
)
from .resolution_errors import (
    screenplay_character_resolution_errors,
    screenplay_unknown_identity_errors,
)
from .resolution_store import (
    load_screenplay_character_resolutions,
    load_screenplay_character_resolutions_for_source,
    merge_screenplay_character_resolutions,
    persist_screenplay_character_resolutions,
    screenplay_character_resolutions_for_source,
)
from .structural_coverage import (
    _STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION,
    _STRUCTURAL_IDENTITY_RECEIPT_VERSION,
    _identity_adjudication_receipt_is_valid,
    _identity_resolution,
    _project_bible_character_names,
    _structural_identity_candidate_semantic_hash,
    _structural_identity_candidate_semantic_rows,
    _structural_identity_catalog_input_hash,
    _structural_identity_catalog_receipt_is_valid,
    _structural_identity_required_bible_names,
    _structural_identity_resolution_receipt,
    _structural_identity_resolution_receipt_is_valid,
    screenplay_identity_resolution_is_current_for_scope,
    screenplay_identity_resolution_is_current_for_source,
    structural_identity_resolution_is_current,
)
from .structural_coverage_audit import audit_identity_coverage_from_structural_evidence
from .structural_coverage_ensure import ensure_structural_identity_coverage

__all__ = [
    "APPEARANCE_MAX",
    "APPEARANCE_MIN",
    "AUTOMATIC_IDENTITY_DECISION_PROVENANCE",
    "Any",
    "BaseModel",
    "Bible",
    "CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET",
    "CAST_DISCOVERY_SOURCE_BUDGET",
    "CHARACTER_CARD_MAX_TOKENS",
    "CHARACTER_CARD_ROLES",
    "CHARACTER_IMPORTANCE_FORWARD_CHAPTERS",
    "CHARACTER_SUBJECT_PERSON",
    "CURRENT_IDENTITY_DECISION_VERSION",
    "CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION",
    "CURRENT_IDENTITY_LITERAL_PROVENANCE",
    "CURRENT_IDENTITY_SYNTHETIC_PROVENANCE",
    "Callable",
    "Character",
    "ConfigDict",
    "ContentGenerationError",
    "CurrentFunctionalIdentityDecision",
    "CurrentIdentityCandidateResponse",
    "CurrentKnownIdentityDecision",
    "CurrentNewNamedIdentityDecision",
    "DISCOVERY_REJUDGE_WINDOW",
    "DURABLE_IDENTITY_DECISION_PROVENANCE",
    "EpisodeScreenplay",
    "EvidenceArtifact",
    "FRAGMENT_BUDGET",
    "FRAGMENT_WINDOW",
    "FUTURE_IDENTITY_DECISION_VERSION",
    "Field",
    "FutureIdentityCandidateResponse",
    "IDENTITY_ADJUDICATION_SOURCE_PROVENANCE",
    "IDENTITY_DISCOVERY_CONTRACT_VERSION",
    "IDENTITY_DISCOVERY_FORWARD_CHAPTERS",
    "IDENTITY_NAME_FORMS",
    "IDENTITY_NAME_FORM_HONORIFIC",
    "IDENTITY_NAME_FORM_PERSONAL",
    "IDENTITY_NAME_FORM_REFERENTIAL",
    "IDENTITY_NAME_FORM_RULE",
    "IDENTITY_REQUEST_MAX_TOKENS",
    "IDENTITY_RESAMPLE_FORMAT_REMINDER",
    "IDENTITY_RESAMPLE_TEMPERATURE_BUMP",
    "IDENTITY_RESAMPLE_TEMPERATURE_CAP",
    "IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH",
    "IDENTITY_UNUSABLE_RESPONSE_RESAMPLES",
    "IdentityAuthorityConflictError",
    "Literal",
    "NoReturn",
    "PRODUCTION_APPEARANCE_MAX_CHARS",
    "PRODUCTION_APPEARANCE_MIN_CHARS",
    "Path",
    "REISSUE_KNOWN_RESOLUTION_KIND",
    "STAGED_INITIAL_EP_START",
    "STRUCTURAL_IDENTITY_COVERAGE_VERSION",
    "SourceSegment",
    "StateConflict",
    "StructuralIdentityCoverageResponse",
    "ValidationError",
    "_CURRENT_IDENTITY_DECISION_CAP_FLOOR",
    "_CURRENT_IDENTITY_DECISION_CAP_PER_REF",
    "_CURRENT_KNOWN_BACKEND_OWNED_ECHO_KEYS",
    "_CurrentIdentitySchemaViolation",
    "_IDENTITY_CARRIER_ANNOTATION_RE",
    "_IDENTITY_COVERAGE_STRICT_PROVIDER_SCHEMA_KEYWORDS",
    "_IDENTITY_DISAMBIGUATING_ORDINALS",
    "_IDENTITY_LIST_SEPARATOR_PATTERN",
    "_STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION",
    "_STRUCTURAL_IDENTITY_RECEIPT_VERSION",
    "_aligned_identity_source_label",
    "_append_character_to_bible",
    "_attach_candidate_source_evidence",
    "_backfill_matching_future_portrait",
    "_bible_lock",
    "_bible_locks",
    "_bible_locks_guard",
    "_bounded_owned_identity_evidence",
    "_candidate_requires_identity_card",
    "_canonical_named_authority_id",
    "_card_lock",
    "_card_locks",
    "_card_locks_guard",
    "_current_identity_decision_cap",
    "_current_identity_declared_signature",
    "_current_identity_disambiguation_key",
    "_current_identity_durable_signature",
    "_current_identity_evidence_batches",
    "_current_identity_evidence_catalog_hash",
    "_current_identity_evidence_payload",
    "_current_identity_evidence_receipt_is_valid",
    "_current_identity_evidence_records",
    "_current_identity_is_schema_violation",
    "_current_identity_known_decision_catalog",
    "_current_identity_prior_decision_catalog",
    "_current_identity_projection_errors",
    "_current_identity_receipt_sort_key",
    "_current_identity_reconcile_as_single",
    "_current_identity_schema",
    "_current_identity_semantic_signature",
    "_discover_character_candidates_legacy",
    "_discovery_skip_key",
    "_distributed_identity_fragments",
    "_draft_identity_projection",
    "_episode_source_text",
    "_forward_fragments",
    "_future_chapter_context",
    "_future_identity_context",
    "_future_identity_schema",
    "_generate_discovered_character_portrait",
    "_generate_fresh_portrait",
    "_has_column",
    "_has_table",
    "_identity_adjudication_receipt_is_valid",
    "_identity_carrier_annotation_base",
    "_identity_coverage_strict_provider_schema",
    "_identity_disambiguating_suffix",
    "_identity_form_functional_key",
    "_identity_list_tokens",
    "_identity_operation_retry_epoch",
    "_identity_resolution",
    "_identity_source_label_has_list_separator",
    "_identity_strict_provider_schema",
    "_identity_strict_response_format",
    "_identity_structured_with_resample",
    "_identity_value_contains",
    "_merge_current_identity_occurrences",
    "_merge_duplicate_narrative_identity_contracts",
    "_name_in_bible",
    "_named_candidate_materialization_compatible",
    "_new_portrait_path",
    "_non_character_skip_key",
    "_normalize_current_identity_payload",
    "_open_portrait",
    "_portrait_dir",
    "_project_bible_character_names",
    "_project_current_identity_response",
    "_project_identity_token",
    "_record_current_identity_absorbed_visual_merges",
    "_record_visual_entity_merge",
    "_redraw_portrait",
    "_refresh_portrait_on_drift",
    "_replace_identity_list_label",
    "_replace_identity_value",
    "_replace_narrative_plan_identity",
    "_replace_resolved_label",
    "_replace_screenplay_body_label",
    "_resolved_evidence_ref",
    "_restore_non_dialogue_prefix",
    "_review_portrait_asset",
    "_safe_name",
    "_save_image_item",
    "_seal_current_identity_evidence",
    "_structural_identity_candidate_semantic_hash",
    "_structural_identity_candidate_semantic_rows",
    "_structural_identity_catalog_input_hash",
    "_structural_identity_catalog_receipt_is_valid",
    "_structural_identity_coverage_response_format",
    "_structural_identity_coverage_schema",
    "_structural_identity_required_bible_names",
    "_structural_identity_resolution_receipt",
    "_structural_identity_resolution_receipt_is_valid",
    "_update_bible_appearance",
    "_validate_current_identity_receipt_bundle",
    "_visual_entity_id_for_resolution_safe",
    "align_source_excerpt",
    "appearance_for_episode",
    "apply_screenplay_character_resolutions",
    "assess_new_character",
    "asyncio",
    "atomic_write_bytes",
    "audit_identity_coverage_from_structural_evidence",
    "base64",
    "bible_for_episode",
    "bible_with_pending_characters_for_text",
    "bible_with_provisional_characters",
    "chapter_is_stub",
    "chapter_titles_match",
    "code_ref",
    "config",
    "current_portrait_ref",
    "discover_character_candidates",
    "ensure_cards_for_screenplay",
    "ensure_cards_for_text",
    "ensure_character_card",
    "ensure_structural_identity_coverage",
    "evidence_repository",
    "extract_character_fragments",
    "extract_current_identity_candidates",
    "extract_json",
    "field_validator",
    "get_conn",
    "get_setting",
    "hiagent",
    "identity_authority_registry",
    "identity_resolution_is_authoritative",
    "index_source_segments",
    "json",
    "load_screenplay_character_resolutions",
    "load_screenplay_character_resolutions_for_source",
    "merge_screenplay_character_resolutions",
    "model_gateway",
    "new_id",
    "normalize_character_resolution",
    "normalize_character_resolutions",
    "normalize_screenplay_identity_annotations",
    "normalize_screenplay_offscreen_visual_identities",
    "normalize_screenplay_voice_ids",
    "now",
    "persist_screenplay_character_resolutions",
    "portrait_for_episode",
    "portrait_prompt",
    "portrait_views_for_episode",
    "production_appearance_anchor",
    "promote_staged_initial_portrait",
    "re",
    "reconcile_bible_display_appearances",
    "record_reference_asset",
    "redraw_prompt",
    "register_initial_portrait",
    "resolution_declares_functional_identity",
    "resolve_future_identity_candidates",
    "screen_appearance_changes",
    "screenplay_character_resolution_errors",
    "screenplay_character_resolutions_for_source",
    "screenplay_identity_resolution_is_current_for_scope",
    "screenplay_identity_resolution_is_current_for_source",
    "screenplay_identity_scope_fingerprint",
    "screenplay_unknown_identity_errors",
    "set_setting",
    "sqlite3",
    "stage_initial_portrait",
    "structural_identity_resolution_is_current",
    "textmatch",
]
