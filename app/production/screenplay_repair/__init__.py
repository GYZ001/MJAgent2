"""剧本 Production Repair Agent：Baseline 一次生成后只做局部 Patch。

原 app/production/screenplay_repair.py（5,409 行）按关注点拆分为本包下的多个
模块：门禁常量/异常（gates）、确定性 patch 策略与spine-beat定位（patch_planning）、
QA 校验入口（qa）、单条 issue 的顶层修复编排（repair_loop）、对白与原文的证据
对齐（dialogue_source_alignment）、叙事图归一化（narrative_graph_normalize，
单一巨型函数，见该文件 docstring）、checkpoint/恢复（checkpoint_recovery）、
resume working 的重验证（revalidate_resume）、issue 选择与信号判定
（issue_selection）、对白链修复（dialogue_chain_repair）、叙事图 patch 底层
操作（narrative_patch_ops）、候选 patch 预检（preflight）、LLM 语义 patch 的
prompt 上下文构建（llm_patch_prompt）与规划+单次调用执行（llm_field_patch）。

本文件是唯一的稳定入口：全仓所有 `from app.production.screenplay_repair import X`
/ `import app.production.screenplay_repair` / `screenplay_repair.X` 使用方式
必须不经改动继续可用——下面按来源模块显式再导出每一个符号（禁止
`from .x import *`，见 app/FILE_CONVENTIONS.toml 的 star_import 闸门）。新增
修复逻辑请加进对应关注点的子模块，不要加回本文件。

拆包陷阱（monkeypatch）：`monkeypatch.setattr(screenplay_repair, "name", stub)`
只改包级重导出，不影响子模块内部已绑定的同名引用——子模块互相调用走的是各自
`from .x import name` 落下的本地绑定，不经过包属性查找。真实测试打桩必须用
tests/conftest.py 的 patch_screenplay_repair_everywhere()（同构于
patch_stages_everywhere()，见 app/stages/__init__.py 与
tests/test_stages_monkeypatch_guard.py 的先例）。
"""
from __future__ import annotations

from .checkpoint_recovery import (
    EvidenceArtifact,
    _activation_retry_grant_id,
    _artifact_descends_from,
    _checkpoint_after_baseline_generation,
    _complete_screenplay_from_working_artifact,
    _reusable_recovery_document,
    _reusable_recovery_evaluation,
    _screenplay_recovery_hard_issues,
    ensure_source_characters_incremental,
    evidence_repository,
    get_conn,
    get_production_revision,
    load_screenplay_from_artifact,
    mark_first_evaluation,
    now,
    publish_screenplay,
    rebind_input_fingerprint,
    save_checkpoint,
    screenplay_artifact_payload,
    update_working_artifact,
)
from .dialogue_chain_repair import (
    DIALOGUE_CHAIN_TURNS_HARD_MAX,
    _dialogue_chain_replacement_is_local,
    _normalize_character_decision_basis,
    _normalize_dialogue_chain_continuity,
    _normalize_dialogue_source_references,
    _source_references_are_grounded,
    _unique_source_dialogue,
)
from .dialogue_source_alignment import (
    SequenceMatcher,
    _best_source_evidence_for_turn,
    _dialogue_turn_at,
    _normalize_dialogue_lines_to_source,
    _source_evidence_score,
    _source_evidence_span,
    _source_sentence_candidates,
    config,
)
from .gates import (
    Any,
    Issue,
    MAX_REPAIR_ACTIVATION_PASSES,
    MAX_REPAIR_ACTIVATION_PATCHES,
    MAX_STRATEGY_ATTEMPTS_PER_ISSUE,
    NARRATIVE_PATCH_PLANNER_MAX_OUTPUT_TOKENS,
    SCREENPLAY_REPAIR_PLANNER_VERSION,
    ScreenplayIdentityGateError,
    ScreenplayNarrativeGateError,
    StateConflict,
    _DIALOGUE_SOURCE_MISMATCH_RE,
    _SCENE_NUMBER_RE,
    _SOURCE_EVIDENCE_STOP_CHARS,
    _SOURCE_SENTENCE_RE,
    _SOURCE_SPAN_EXACT_MISMATCH_RE,
    _eval_id_from_create,
    _gate_failure_message,
    _persist_screenplay_duration_expansion,
    annotations,
    json,
    non_waivable_screenplay_issues,
    re,
    screenplay_identity_gate_issues,
)
from .issue_selection import (
    Counter,
    _choose_issue,
    _identity_contract_repair_policy,
    _introduced_issue_messages,
    _issue_acceptance_test,
    _target_issue_signature_still_open,
)
from .llm_field_patch import (
    NARRATIVE_CONTRACT_VERSION,
    _llm_field_patch,
    _llm_field_patch_once,
)
from .llm_patch_prompt import (
    _ISSUE_TARGET_CONTAINERS,
    _ISSUE_TARGET_INDEX_RE,
    _ISSUE_TARGET_WINDOW,
    _issue_target_excerpt,
    _narrative_patch_prompt_context,
)
from .narrative_graph_audience_context import deepcopy
from .narrative_graph_normalize import _normalize_screenplay_narrative_graph
from .narrative_patch_ops import (
    _candidate_is_executable,
    _candidate_targets_narrative_graph,
    _expand_single_action_event_closure,
    _find_narrative_node,
    _narrative_collection_for_new_node,
    _narrative_collection_for_node,
    _normalize_patch_operation_payload,
    _normalize_top_level_narrative_parent,
    _resolve_dialogue_chain_turn_target,
    _resolve_narrative_patch_owner,
    _try_document_patch_operation,
)
from .patch_planning import (
    EpisodeScreenplay,
    PatchOperation,
    _best_scene_for_spine_beat,
    _patch_strategy_key,
    _plan_source_span_patch,
    _source_evidence_contexts,
    _source_span_issue_evidence_id,
    _strategy_was_tried,
    textmatch,
)
from .preflight import _preflight_document_candidate
from .qa import (
    Bible,
    Evaluation,
    blocker_count,
    enrich_issues,
    get_setting,
    hashlib,
    identity_resolution_is_authoritative,
    issues_from_validator_messages,
    must_fix_count,
    resolution_declares_functional_identity,
    run_screenplay_qa,
    structured_issue,
)
from .repair_loop import (
    _derive_scene_story_function,
    _heuristic_fill_dramatic_field,
    _opening_anchor_from_issue,
    _plan_screenplay_repair_operations,
    _scene_from_issue,
    plan_screenplay_patch,
)
from .revalidate_resume import (
    _revalidate_or_rebuild_resume_working,
    recover_screenplay_working_authority,
)
