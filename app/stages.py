"""LLM 流水线阶段：摘要 / 角色圣经 / 剧集规划 / 可拍剧本 / 分镜脚本。
每阶段 = prompt + Schema 校验 + 业务校验 + 修复回路（默认重试到 max_repair_attempts 次，失败抛 StageError——禁止兜底）。
校验类失败一律让模型继续修复；只有模型真正不可用（鉴权失败/参数 400/网关持续故障，
即 hiagent.ProviderError 透传）才立刻失败——重试同一 prompt 对这类错误无意义。
提示词正文与 docs/PROMPT_SPEC.md 保持同步，改动需先跑金样回归。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
import re
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import config, hiagent, textmatch
from app.character_policy import (
    functional_extra_policy_text,
    resolution_declares_functional_identity,
)
from app.continuity import (adaptation_hook_errors, ensure_audio_timeline,
                            information_ledger_errors, ledger_context_for_shot,
                            sync_shot_continuity_fields)
from app.db import get_conn, get_setting, log_provider_call
from app.evaluations.issues import issues_from_messages
from app.errors import ArtifactNeedsRebuildError, ContentGenerationError
from app.harness import model_gateway
from app.harness.types import Issue, IssueSeverity
from app.loops import AgentLoop, AgentLoopFailure, AgentLoopPolicy
from app.narrative_blueprint import (
    AUDIBLE_SOURCE_DELIVERY_MODES,
    BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE,
    BLUEPRINT_PROMPT_VERSION,
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD,
    BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD,
    BLUEPRINT_VERSION,
    BlueprintSourceOccurrenceError,
    BlueprintSourceOwnershipError,
    BlueprintSemanticReview,
    BlueprintStateSubjectOwnershipPatch,
    NarrativeBlueprint,
    NarrativeBlueprintPatch,
    NarrativeBlueprintShard,
    apply_blueprint_state_subject_ownership_patch,
    apply_narrative_blueprint_patch,
    blueprint_authority_validator_fingerprint,
    blueprint_patch_schema,
    blueprint_shard_candidate_hash,
    blueprint_source_occurrence_issues,
    blueprint_shard_provider_schema,
    blueprint_semantic_issue_is_resolved,
    blueprint_semantic_voice_issue_has_dialogue_authority,
    blueprint_state_subject_issues,
    blueprint_state_subject_ownership_patch_schema,
    filter_blueprint_semantic_review_voice_issues,
    blueprint_prompt_contract,
    blueprint_semantic_review_schema,
    derive_blueprint_scene_plans,
    normalize_blueprint_agency_continuity,
    normalize_blueprint_fact_versions,
    normalize_blueprint_provider_payload,
    normalize_blueprint_requirement_state_keys,
    normalize_blueprint_raw_json,
    normalize_blueprint_semantic_review_payload,
    normalize_blueprint_state_subject_evidence_projection,
    normalize_blueprint_state_subject_perception,
    recover_complete_blueprint_prefix,
    render_blueprint_shard_semantic_issue,
    validate_and_apply_blueprint_scene_contract,
    validate_blueprint_semantic_review,
    validate_blueprint_scene_partition,
    validate_narrative_blueprint,
    validate_narrative_blueprint_patch_projection,
    validate_narrative_blueprint_shard,
)
from app.schemas import (Bible, CAMERA_MOVES, Character, CharacterAffiliation, CharacterAlias,
                         CharacterRelation, Dialogue, EMOTIONS, EpisodeScreenplay,
                         NARRATIVE_CONTRACT_VERSION, RequiredOnScreenText,
                         SHOT_SIZES, Scene, Shot, Storyboard,
                         StoryboardContextRequirement, StoryboardOutline,
                         StoryboardOutlineShot, StoryboardSceneContext, StoryboardScenePack,
                         TRANSITIONS,
                         extract_json, normalize_screenplay_json_shape,
                         schema_errors)
from app.validators import (SOURCE_EXCERPT_MIN_CHARS,
                            canonicalize_storyboard_scene,
                            defer_establishing_covers,
                            ending_hook_is_grounded,
                            ending_hook_grounding_report,
                            _condense,
                            _scene_time_changed,
                            normalize_action_desc, normalize_continuity,
                            normalize_dialogue_focus_offscreen_mentions,
                            normalize_offbible_characters, normalize_transition_visuals,
                            narrative_outline_action_capacity_errors,
                            key_line_catalog,
                            match_scene_name,
                            prefer_default_shot_durations,
                            relieve_spoken_overflow,
                            source_dialogue_fragments,
                            storyboard_shot_count_range,
                            validate_bible, validate_screenplay,
                            validate_scene_bible,
                            validate_storyboard,
                            resolve_screenplay_scene_names,
                            validate_storyboard_shot_scene_alignment,
                            validate_storyboard_shot_covers_outline,
                            validate_storyboard_outline,
                            validate_storyboard_direction_contract,
                            validate_storyboard_preserves_key_content,
                            validate_storyboard_soundtrack)
from app.renderability import (
    ACTION_DESC_HARD_MIN,
    ACTION_DESC_TARGET_MAX,
    ACTION_DESC_TARGET_MIN,
    DIALOGUE_CHAIN_TURNS_HARD_MAX,
    PREFERRED_SHOT_DURATION_S,
    SHOT_HARD_MAX,
    renderability_prompt_block,
)
from app.source_excerpt import (
    AlignedExcerpt,
    align_source_excerpt,
    index_source_segments,
    render_indexed_source,
    structural_front_matter_ids,
)
from app.source_facts import (
    SOURCE_FACT_VERSION,
    source_facts,
    source_segment_facts,
)
from app.spoken_contract import onscreen_text_for_capacity
from app.screenplay_ir import (
    IR_COMPILER_VERSION,
    IR_LOCAL_SOURCE_WINDOW,
    IR_MAX_SOURCE_SEGMENTS_PER_UNIT,
    IR_MIN_ADAPTED_SOURCE_RATIO,
    IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO,
    IRScene,
    IRSceneUnit,
    IR_VERSION,
    ScreenplayGenerationIR,
    ScreenplayIRFidelityError,
    ScreenplayIRIdentityConflictError,
    compile_screenplay_ir,
    normalize_screenplay_ir_payload,
    recover_complete_screenplay_ir_prefix,
    scene_heading_has_multiple_locations,
    screenplay_ir_bible_context,
    screenplay_ir_missing_event_semantic_paths,
    screenplay_ir_missing_participant_delivery_paths,
    screenplay_ir_prompt_contract,
    screenplay_ir_source_audit_contract_errors,
)
from app.identity_authority import (
    identity_resolution_is_authoritative,
    model_identity_authority_prompt_rule,
)

SYSTEM_PREFIX = (
    "你是专业的竖屏漫剧（动态漫画短剧）编剧与分镜师。\n"
    "你的观众看的是 AI 生成视频，不是摄影机实拍；请为模型能力写作，不为文学完整度炫技。\n"
    "输出规则：只输出一个 JSON 对象，无 Markdown 围栏，无解释文字；字符串内部的英文双引号必须写成 JSON 转义形式。\n"
    "所有内容使用简体中文。"
)

SCREENPLAY_BASELINE_PROMPT_VERSION = "screenplay-compact-ir-5.5.1"
SCREENPLAY_BLUEPRINT_PROMPT_VERSION = BLUEPRINT_PROMPT_VERSION
BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION = "blueprint-semantic-review.v5"
# IR shape drift is normalized locally. A second AgentLoop iteration would
# resend the entire chapter and candidate for a few field-level corrections,
# erasing the latency/token savings of the compact contract.
SCREENPLAY_STRUCTURAL_BOOTSTRAP_ITERATIONS = 1
SCREENPLAY_IR_MIN_TOKENS = 20480
SCREENPLAY_IR_MAX_TOKENS = 36864

BLUEPRINT_SHARD_MIN_TOKENS = 6144
BLUEPRINT_SHARD_MAX_TOKENS = 16384
BLUEPRINT_SHARD_MAX_ATTEMPTS = 3
# A transport stall authors nothing, so it is not a semantic attempt and gets
# its own bounded budget.  Production: shard 13 of ep_a0e90058f83c spent
# attempts 1 and 2 on invalid candidates, then attempt 3 stalled at 0 received
# characters after 182.8s -- the episode died on a call that never delivered a
# single byte, with no candidate to show for it.
BLUEPRINT_SHARD_MAX_STALL_RETRIES = 2
BLUEPRINT_REVIEW_FORMAT_RETRY_LIMIT = 1
# A full (non-targeted) review of a converged blueprint can carry a dozen+
# must-fix issues; 8192 output tokens is exactly the truncation cliff observed
# in production (finish_reason=length -> OUTPUT_TRUNCATED, replayed forever).
# The text model supports up to 32768 output tokens.
BLUEPRINT_REVIEW_MAX_TOKENS = 16384
# Extra attempts for a single independent semantic reviewer when the provider
# never received the request (delivery_state == not_sent, replay_safe). A
# transient not-sent failure of one reviewer must not discard the whole
# multi-round blueprint generation; genuinely-unknown outcomes still fail closed.
BLUEPRINT_REVIEW_PROVIDER_RETRY_LIMIT = 1
# Consensus needs two independent samples.  When exactly one reviewer never
# delivered an opinion at all, draw ONE more sample under this number instead of
# discarding a validated blueprint that cost ~30 minutes to build.  It is a new
# deterministic operation, never a replay of the unresolved call, and it is
# bounded to one per review round.
BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE = 3
# Runaway-generation breakers.  These three values are *floors*: a blueprint is
# produced leaf-by-leaf, so the honest cost of one activation scales with the
# planned leaf count, not with a constant.  ``_BlueprintGenerationBudget``
# raises all three from the deterministic leaf plan (see ``adopt_shard_plan``)
# so the breakers only ever fire on genuine runaway, never on the nominal path
# of a long episode.
BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS = 32
BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS = 131072
BLUEPRINT_GENERATION_MAX_WALL_SECONDS = 1800.0
# 与身份合同同源的标定：会先思考再作答的模型，其 reasoning token 与正文共用
# completion 预算，所以"够写下补丁"并不等于"够跑完这次调用"。
IR_FIDELITY_PATCH_MAX_TOKENS = 16384
# Per planned leaf: one full-shard call plus at most one typed ownership
# repair call (``BLUEPRINT_SHARD_MAX_ATTEMPTS`` allows a third attempt, which
# the shared headroom below absorbs together with dynamic splits and the
# patch/review stages).
BLUEPRINT_LEAF_PROVIDER_CALLS = 2
BLUEPRINT_LEAF_CALL_HEADROOM = 8
BLUEPRINT_GENERATION_MAX_SPLIT_DEPTH = 4
# 场次语义门禁耗尽修复轮次后仍未收口，往往不是文案问题，而是**蓝图把这个 source unit
# 分错了类**（例如把一句人物内在特质标成纯环境）：环境 slot 不许写人物内容，源文却整句
# 都是人物，两条路都通不过 —— 合同可证明无解，而场次层唯一的补救手段（重写文案）
# 修不好一个分类错误。生产 EP2 的 SS002 因此累计打了 254 次 provider 调用、
# 整片重写 8 次，每一轮双审共识都给出完全相同的判定。
#
# 这一层的正确动作是把证据交回**拥有该决定的那一层**：带着「哪些 unit 下游无解、
# 审查员原话是什么」重建一次蓝图。它不是盲目重摇——反馈会进入分片的 source payload，
# 既改变 source_hash（使已缓存分片不被复用），也作为显式约束进入提示词。
# 严格限一次：真正无解的输入不会因为多试几次而变得有解。
SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT = 1


def screenplay_ir_token_budget(source_text: str) -> int:
    """Bound output by source complexity without reserving 36K for short chapters."""
    source_segments = len(index_source_segments(source_text))
    estimated = 8192 + source_segments * 48
    return min(
        SCREENPLAY_IR_MAX_TOKENS,
        max(SCREENPLAY_IR_MIN_TOKENS, estimated),
    )


def screenplay_ir_fidelity_budget(source_text: str) -> dict[str, Any]:
    segments = index_source_segments(source_text)
    front_matter_ids = structural_front_matter_ids(segments)
    dramatic = [
        segment for segment in segments
        if segment.segment_id not in front_matter_ids
    ]
    source_chars = sum(
        len(textmatch.condense(segment.text))
        for segment in dramatic
    )
    windows: list[dict[str, Any]] = []
    for start in range(0, len(dramatic), IR_LOCAL_SOURCE_WINDOW):
        window = dramatic[start:start + IR_LOCAL_SOURCE_WINDOW]
        chars = sum(
            len(textmatch.condense(segment.text))
            for segment in window
        )
        windows.append({
            "first_source_id": window[0].segment_id,
            "last_source_id": window[-1].segment_id,
            "source_chars": chars,
            "minimum_adapted_chars": math.ceil(
                chars * IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO
            ),
        })
    return {
        "front_matter_ids": sorted(front_matter_ids),
        "dramatic_source_chars": source_chars,
        "minimum_adapted_chars": math.ceil(
            source_chars * IR_MIN_ADAPTED_SOURCE_RATIO
        ),
        "windows": windows,
    }


def _narrative_blueprint_content_hash(
    blueprint: NarrativeBlueprint | None,
) -> str:
    if blueprint is None:
        return ""
    return hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _current_blueprint_authority_snapshot(
    source_text: str,
    *,
    generation_mode: str,
    generation_budget: Any | None = None,
    shard_count: int | None = None,
) -> dict[str, Any]:
    """One versioned authority binding for every final Blueprint producer."""
    validator_material = {
        "contract_version": BLUEPRINT_VERSION,
        "prompt_version": SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "source_fact_version": SOURCE_FACT_VERSION,
        "shard_policy_version": BLUEPRINT_SHARD_POLICY_VERSION,
        "local_authority_validator_version": (
            BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
        ),
        "split_manifest_version": BLUEPRINT_SPLIT_MANIFEST_VERSION,
    }
    snapshot: dict[str, Any] = {
        "generation_mode": generation_mode,
        **validator_material,
        "source_corpus_hash": hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest(),
        "validator_fingerprint": (
            blueprint_authority_validator_fingerprint()
        ),
    }
    if generation_mode == "semantic_reviewed":
        snapshot["review_policy_version"] = (
            BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
        )
    if shard_count is not None:
        snapshot["shard_count"] = int(shard_count)
    if generation_budget is not None:
        snapshot.update({
            "provider_call_count": generation_budget.provider_calls,
            "requested_output_tokens": (
                generation_budget.requested_output_tokens
            ),
            "actual_output_tokens": generation_budget.actual_output_tokens,
            "unknown_output_tokens": generation_budget.unknown_output_tokens,
            "charged_output_tokens": generation_budget.charged_output_tokens,
            "active_reserved_output_tokens": (
                generation_budget.reserved_output_tokens
            ),
        })
    return snapshot


def _blueprint_authority_snapshot_is_current(
    snapshot: dict[str, Any],
    source_text: str,
) -> bool:
    expected = _current_blueprint_authority_snapshot(
        source_text,
        generation_mode=str(snapshot.get("generation_mode") or "authority"),
    )
    authority_keys = [
        "contract_version",
        "prompt_version",
        "source_fact_version",
        "shard_policy_version",
        "local_authority_validator_version",
        "split_manifest_version",
        "source_corpus_hash",
        "validator_fingerprint",
    ]
    if str(snapshot.get("generation_mode") or "") == "semantic_reviewed":
        authority_keys.append("review_policy_version")
    return all(
        snapshot.get(key) == expected.get(key)
        for key in authority_keys
    )


def _select_current_blueprint_artifact(
    rows: list[Any],
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> tuple[str | None, str | None]:
    """Prefer a current wrapper; retain an old same-hash id only as lineage."""
    expected_hash = _narrative_blueprint_content_hash(blueprint)
    legacy_same_hash_id: str | None = None
    for row in rows:
        try:
            raw_content = json.loads(row["content_json"] or "{}")
            if not _artifact_json_content_is_sealed(row, raw_content):
                continue
            row_blueprint = NarrativeBlueprint.model_validate(raw_content)
            if _narrative_blueprint_content_hash(row_blueprint) != expected_hash:
                continue
            artifact_id = str(row["id"])
            if legacy_same_hash_id is None:
                legacy_same_hash_id = artifact_id
            snapshot = json.loads(row["model_snapshot_json"] or "{}")
            if (
                str(row["contract_version"] or "") == BLUEPRINT_VERSION
                and str(row["prompt_version"] or "")
                == SCREENPLAY_BLUEPRINT_PROMPT_VERSION
                and _blueprint_authority_snapshot_is_current(
                    snapshot,
                    source_text,
                )
            ):
                return artifact_id, legacy_same_hash_id
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None, legacy_same_hash_id


def _artifact_json_content_is_sealed(row: Any, content: object) -> bool:
    """Verify a DB artifact wrapper before any cache/recovery projection."""
    from app.evidence import repository as evidence_repository

    try:
        stored_hash = str(row["content_hash"] or "")
    except (KeyError, IndexError, TypeError):
        return False
    return bool(
        stored_hash
        and stored_hash == evidence_repository.content_hash(content)
    )


def _screenplay_ir_blueprint_snapshot_matches(
    model_snapshot: dict[str, Any],
    expected_blueprint_hash: str,
) -> bool:
    recorded_hash = str(model_snapshot.get("blueprint_hash") or "")
    return bool(
        not expected_blueprint_hash
        or not recorded_hash
        or recorded_hash == expected_blueprint_hash
    )


def _recover_screenplay_ir_candidate(
    episode_id: str,
    *,
    blueprint_hash: str = "",
    expected_source_audit_annotations: list[object] | None = None,
) -> tuple[ScreenplayGenerationIR, str] | None:
    """Load the latest IR produced for the same authority input."""
    from app.evidence import repository as evidence_repository
    from app.observability.tracing import current_trace

    trace = current_trace()
    if not trace.run_id:
        return None
    conn = get_conn()
    current_run = conn.execute(
        "SELECT input_fingerprint FROM workflow_runs WHERE id=?",
        (trace.run_id,),
    ).fetchone()
    if current_run is None:
        return None
    input_fingerprint = str(current_run["input_fingerprint"] or "")
    lineage_rows = conn.execute(
        """WITH RECURSIVE lineage(id, parent_run_id) AS (
               SELECT id,parent_run_id
                 FROM workflow_runs
                WHERE id=?
               UNION ALL
               SELECT wr.id,wr.parent_run_id
                 FROM workflow_runs wr
                 JOIN lineage ON wr.id=lineage.parent_run_id
           )
           SELECT id FROM lineage""",
        (trace.run_id,),
    ).fetchall()
    lineage_run_ids = [str(row["id"]) for row in lineage_rows]
    if not lineage_run_ids:
        return None
    lineage_marks = ",".join("?" for _ in lineage_run_ids)
    rows = conn.execute(
        f"""SELECT a.id,a.type,a.content_json,a.content_hash,a.contract_version,
                  a.prompt_version,
                  a.model_snapshot_json,
                  wr.input_fingerprint AS artifact_input_fingerprint
             FROM artifacts a
             JOIN step_runs sr ON sr.id=a.created_by_step_run_id
             JOIN workflow_runs wr ON wr.id=sr.run_id
            WHERE a.scope_type='episode' AND a.scope_id=?
              AND a.contract_version LIKE 'screenplay-generation-ir.v%'
              AND a.status!='stale'
              AND a.type IN (
                    'screenplay_generation_ir',
                    'screenplay_generation_ir_raw',
                    'episode_screenplay'
              )
              AND wr.input_fingerprint=?
              AND wr.id IN ({lineage_marks})
            ORDER BY CASE
                         WHEN a.prompt_version=? AND a.contract_version=?
                         THEN 0
                         WHEN a.contract_version=? THEN 1
                         ELSE 2
                     END,
                     a.created_at DESC
            LIMIT 20""",
        (
            episode_id,
            input_fingerprint,
            *lineage_run_ids,
            SCREENPLAY_BASELINE_PROMPT_VERSION,
            IR_VERSION,
            IR_VERSION,
        ),
    ).fetchall()
    for row in rows:
        try:
            model_snapshot = json.loads(
                row["model_snapshot_json"] or "{}"
            )
            if not _screenplay_ir_blueprint_snapshot_matches(
                model_snapshot,
                blueprint_hash,
            ):
                continue
            content = json.loads(row["content_json"] or "{}")
            if (
                not str(row["content_hash"] or "")
                or str(row["content_hash"])
                != evidence_repository.content_hash(content)
            ):
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(row["id"]),
                    artifact_type=str(row["type"]),
                    reason="IR Artifact 内容与存储指纹漂移",
                )
            raw = content.get("raw_output") if isinstance(content, dict) else None
            if isinstance(raw, str):
                try:
                    payload = extract_json(
                        raw,
                        repair_unescaped_inner_quotes=True,
                    )
                except ValueError:
                    payload = recover_complete_screenplay_ir_prefix(raw)
            else:
                payload = content
            if not isinstance(payload, dict):
                continue
            missing_paths = [
                *screenplay_ir_missing_participant_delivery_paths(payload),
                *screenplay_ir_missing_event_semantic_paths(payload),
            ]
            audit_errors = screenplay_ir_source_audit_contract_errors(
                payload,
                expected_source_audit_annotations=(
                    expected_source_audit_annotations
                ),
            )
            if missing_paths or audit_errors:
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(row["id"]),
                    artifact_type=str(row["type"]),
                    reason=(
                        "缺少当前合同要求的显式字段 "
                        + "、".join(missing_paths[:10])
                        + (
                            "；" + "；".join(audit_errors[:10])
                            if audit_errors else ""
                        )
                    ),
                )
            artifact_contract = str(row["contract_version"] or "")
            payload_contract = str(payload.get("format_version") or "")
            if artifact_contract == IR_VERSION and payload_contract != IR_VERSION:
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(row["id"]),
                    artifact_type=str(row["type"]),
                    reason=(
                        f"Artifact 合同为 {IR_VERSION}，"
                        f"内容合同为 {payload_contract or 'missing'}"
                    ),
                )
            if artifact_contract != IR_VERSION:
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(row["id"]),
                    artifact_type=str(row["type"]),
                    reason=(
                        f"Artifact 合同 {artifact_contract or 'missing'} "
                        f"与当前 {IR_VERSION} 不一致，需要重建"
                    ),
                )
            payload, _changes = normalize_screenplay_ir_payload(payload)
            candidate = ScreenplayGenerationIR.model_validate(payload)
        except ArtifactNeedsRebuildError as exc:
            conn.execute(
                "UPDATE artifacts SET status='stale',stale_reason=? "
                "WHERE id=? AND status!='rejected'",
                (str(exc), row["id"]),
            )
            conn.commit()
            continue
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        return candidate, str(row["id"])
    return None


class StageError(Exception):
    """阶段失败：errors 面向 UI 展示（PRD 原则 P2：失败要响）。"""

    def __init__(
        self,
        stage: str,
        errors: list[str],
        *,
        exit_reason: str | None = None,
        iterations: int | None = None,
        issues: list[Issue] | None = None,
    ):
        self.stage = stage
        self.errors = errors
        self.exit_reason = exit_reason
        self.iterations = iterations
        self.issues = list(issues or [])
        super().__init__(f"[{stage}] " + "；".join(errors[:5]))


class _IRFidelityInsertion(BaseModel):
    scene_key: str
    insert_after_event_key: str | None = None
    units: list[IRSceneUnit] = Field(default_factory=list)


class _IRFidelityPatch(BaseModel):
    insertions: list[_IRFidelityInsertion] = Field(default_factory=list)
    new_scenes: list[IRScene] = Field(default_factory=list)


class _IRScenePartition(BaseModel):
    key: str
    scene_heading: str
    story_function: str
    summary: str
    conflict: str
    turn: str
    unit_indexes: list[int]


class _IRSceneReplacement(BaseModel):
    scene_key: str
    scenes: list[_IRScenePartition]


class _IRScenePartitionPlan(BaseModel):
    replacements: list[_IRSceneReplacement]


async def _repartition_multilocation_ir_scenes(
    candidate: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    source_text: str,
    parent_artifact_id: str | None,
) -> ScreenplayGenerationIR:
    problematic = [
        scene for scene in candidate.scenes
        if scene_heading_has_multiple_locations(scene.scene_heading)
    ]
    if not problematic:
        return candidate

    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    source_segments = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    context = []
    for scene in problematic:
        context.append({
            "scene_key": scene.key,
            "scene_heading": scene.scene_heading,
            "story_function": scene.story_function,
            "summary": scene.summary,
            "conflict": scene.conflict,
            "turn": scene.turn,
            "units": [
                {
                    "unit_index": index,
                    "kind": unit.kind,
                    "text": unit.text,
                    "source_segment_ids": unit.source_segment_ids,
                    "source_text": [
                        source_segments[source_id]
                        for source_id in unit.source_segment_ids
                        if source_id in source_segments
                    ],
                }
                for index, unit in enumerate(scene.units)
            ],
        })
    prompt = (
        "任务：把包含多个不连续地点的 IR 场次重新分成连续时空场次。"
        "只能重新分组已有 unit_index，禁止改写、删除、复制或新增 unit。"
        "每个原场的所有索引必须恰好使用一次，分组必须保持原顺序且每组索引连续。"
        "每个新 scene_heading 只能包含一个主要地点，地点栏禁止使用「、」「+」"
        "或逗号连接多个地点；同一建筑内发生明确房间切换也应分场。"
        "时间、地点、人物目标或连续动作发生切换时建立新场。\n\n"
        "待重分场数据："
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        + "\n\n只输出 JSON："
        '{"replacements":[{"scene_key":"sc1","scenes":['
        '{"key":"sc1a","scene_heading":"【场1】日 / 单一地点",'
        '"story_function":"","summary":"","conflict":"","turn":"",'
        '"unit_indexes":[0,1]}]}]}'
    )
    raw = await model_gateway.chat(
        [
            {"role": "system", "content": SYSTEM_PREFIX},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=12288,
        call_meta={
            "stage": "剧本连续时空重分场",
            "stage_key": "screenplay_ir_scene_partition",
            "call_role": "stage_repair",
            "call_role_label": "连续时空重分场",
            "episode_id": str(episode.get("id") or ""),
            "generation_contract": IR_VERSION,
            "compiler_version": IR_COMPILER_VERSION,
            "expected_json": True,
            "reuse_successful_operation": True,
        },
    )
    plan = _IRScenePartitionPlan.model_validate(extract_json(raw))
    replacements = {item.scene_key: item for item in plan.replacements}
    rebuilt: list[IRScene] = []
    used_keys = {
        scene.key for scene in candidate.scenes
        if scene.key not in replacements
    }
    for scene in candidate.scenes:
        if scene not in problematic:
            rebuilt.append(scene)
            continue
        replacement = replacements.get(scene.key)
        if replacement is None or len(replacement.scenes) < 2:
            raise ValueError(f"IR 场次重分组缺少有效替代：{scene.key}")
        ordered_partitions = sorted(
            replacement.scenes,
            key=lambda partition: min(
                partition.unit_indexes,
                default=len(scene.units),
            ),
        )
        starts = [
            min(partition.unit_indexes, default=len(scene.units))
            for partition in ordered_partitions
        ]
        if (
            not starts
            or starts[0] != 0
            or len(set(starts)) != len(starts)
            or any(
                start < 0 or start >= len(scene.units)
                for start in starts
            )
        ):
            raise ValueError(
                f"IR 场次重分组缺少有效连续边界：{scene.key}"
            )
        for partition_index, partition in enumerate(ordered_partitions):
            unit_start = starts[partition_index]
            unit_end = (
                starts[partition_index + 1]
                if partition_index + 1 < len(starts)
                else len(scene.units)
            )
            partition.unit_indexes = list(range(unit_start, unit_end))
            if (
                partition.key in used_keys
                or scene_heading_has_multiple_locations(
                    partition.scene_heading
                )
            ):
                raise ValueError(
                    f"IR 场次重分组的新 key/heading 非法：{partition.key}"
                )
            used_keys.add(partition.key)
            rebuilt.append(IRScene(
                key=partition.key,
                scene_heading=partition.scene_heading,
                story_function=partition.story_function,
                summary=partition.summary,
                conflict=partition.conflict,
                turn=partition.turn,
                units=[
                    scene.units[index]
                    for index in partition.unit_indexes
                ],
            ))
    candidate.scenes = rebuilt
    candidate.events = []
    candidate.beats = []
    candidate.coverage = []

    trace = current_trace()
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir_scene_partition_raw",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="candidate",
            trust_level="T0",
            content={"raw_output": raw},
            parent_artifact_ids=(
                [parent_artifact_id] if parent_artifact_id else []
            ),
            contract_version=IR_VERSION,
            prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    completed_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="candidate",
            trust_level="T1",
            content=candidate.model_dump(mode="json"),
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version=IR_VERSION,
            prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
            model_snapshot={
                "compiler_version": IR_COMPILER_VERSION,
                "scene_partition_count": len(problematic),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    object.__setattr__(
        candidate,
        "evidence_artifact_id",
        completed_artifact["id"],
    )
    return candidate


def _ir_fidelity_patch_context(
    candidate: ScreenplayGenerationIR,
    source_text: str,
) -> dict[str, Any]:
    segments = index_source_segments(source_text)
    front_matter_ids = structural_front_matter_ids(segments)
    dramatic = [
        segment for segment in segments
        if segment.segment_id not in front_matter_ids
    ]
    flat_units = [
        (scene, unit)
        for scene in candidate.scenes
        for unit in scene.units
    ]
    owned = {
        source_id
        for _scene, unit in flat_units
        for source_id in unit.source_segment_ids
    }
    missing = [
        segment.segment_id
        for segment in dramatic
        if segment.segment_id not in owned
    ]
    windows: list[dict[str, Any]] = []
    for start in range(0, len(dramatic), IR_LOCAL_SOURCE_WINDOW):
        window = dramatic[start:start + IR_LOCAL_SOURCE_WINDOW]
        window_ids = {segment.segment_id for segment in window}
        source_chars = sum(
            len(textmatch.condense(segment.text))
            for segment in window
        )
        existing_units = [
            {
                "scene_key": scene.key,
                "event_key": unit.event_key,
                "kind": unit.kind,
                "text": unit.text,
                "source_segment_ids": unit.source_segment_ids,
                "speaker_key": unit.speaker_key,
            }
            for scene, unit in flat_units
            if window_ids.intersection(unit.source_segment_ids)
        ]
        adapted_chars = sum(
            len(textmatch.condense(str(item["text"])))
            for item in existing_units
        )
        target_chars = math.ceil(
            source_chars * IR_MIN_ADAPTED_SOURCE_RATIO
        )
        missing_here = [
            source_id for source_id in missing if source_id in window_ids
        ]
        if adapted_chars >= target_chars and not missing_here:
            continue
        windows.append({
            "source_range": (
                f"{window[0].segment_id}-{window[-1].segment_id}"
            ),
            "source_chars": source_chars,
            "existing_adapted_chars": adapted_chars,
            "minimum_final_adapted_chars": target_chars,
            "minimum_additional_chars": max(
                0, target_chars - adapted_chars,
            ),
            "missing_source_ids": missing_here,
            "source_segments": [
                {
                    "source_segment_id": segment.segment_id,
                    "text": segment.text,
                }
                for segment in window
            ],
            "existing_units": existing_units,
        })
    return {
        "missing_source_ids": missing,
        "identities": [
            {
                "key": identity.key,
                "display_name": identity.display_name,
                "authority_id": identity.authority_id,
                "voice_canonical": identity.voice_canonical,
            }
            for identity in candidate.identities
        ],
        "scenes": [
            {
                "key": scene.key,
                "scene_heading": scene.scene_heading,
                "summary": scene.summary,
            }
            for scene in candidate.scenes
        ],
        "windows_requiring_expansion": windows,
    }


def _select_fidelity_blueprint_plans(
    context: dict[str, Any],
    plans: list[Any],
    *,
    candidate_scene_count: int,
) -> tuple[list[Any], list[Any], list[Any], set[str]]:
    remaining_plans = plans[candidate_scene_count:]
    repair_source_ids = set(context["missing_source_ids"])
    repair_source_ids.update(
        str(segment["source_segment_id"])
        for window in context["windows_requiring_expansion"]
        for segment in window["source_segments"]
    )
    internal_plans = [
        plan
        for plan in plans[:candidate_scene_count]
        if repair_source_ids.intersection(plan.source_segment_ids)
    ]
    selected_plans = [] if internal_plans else remaining_plans[:6]
    return (
        remaining_plans,
        internal_plans,
        selected_plans,
        repair_source_ids,
    )


def _merge_ir_fidelity_patch(
    candidate: ScreenplayGenerationIR,
    patch: _IRFidelityPatch,
    source_text: str,
    *,
    round_no: int,
) -> int:
    segments = index_source_segments(source_text)
    source_order = {
        segment.segment_id: index
        for index, segment in enumerate(segments)
    }
    scenes = {scene.key: scene for scene in candidate.scenes}
    existing_units = [
        (scene, unit)
        for scene in candidate.scenes
        for unit in scene.units
    ]
    occupied_keys = {
        unit.event_key for _scene, unit in existing_units
    }
    inserted = 0
    existing_scene_keys = set(scenes)
    for new_scene in patch.new_scenes:
        if new_scene.key in existing_scene_keys or not new_scene.units:
            continue
        valid_units: list[IRSceneUnit] = []
        for patch_unit in new_scene.units:
            source_ids = list(patch_unit.source_segment_ids)
            if (
                not source_ids
                or any(source_id not in source_order for source_id in source_ids)
            ):
                continue
            if len(source_ids) != len(set(source_ids)):
                raise ValueError(
                    "保真补写 unit.source_segment_ids 不得重复"
                )
            if candidate.source_scene_owners:
                owner_scene_keys = {
                    candidate.source_scene_owners.get(source_id)
                    for source_id in source_ids
                }
                if owner_scene_keys != {new_scene.key}:
                    raise ValueError(
                        "保真补写 new_scene 违反 source 唯一归属："
                        f"{source_ids} -> {sorted(str(value) for value in owner_scene_keys)}，"
                        f"target={new_scene.key}"
                    )
            inserted += 1
            event_key = f"fidelity-r{round_no}-{inserted}"
            while event_key in occupied_keys:
                inserted += 1
                event_key = f"fidelity-r{round_no}-{inserted}"
            occupied_keys.add(event_key)
            patch_unit.event_key = event_key
            patch_unit.source_segment_ids = source_ids
            valid_units.append(patch_unit)
        if not valid_units:
            continue
        new_scene.units = valid_units
        candidate.scenes.append(new_scene)
        scenes[new_scene.key] = new_scene
        existing_scene_keys.add(new_scene.key)
        existing_units.extend(
            (new_scene, unit) for unit in valid_units
        )
    for insertion in patch.insertions:
        for patch_unit in insertion.units:
            source_ids = list(patch_unit.source_segment_ids)
            if (
                not source_ids
                or any(source_id not in source_order for source_id in source_ids)
            ):
                continue
            if len(source_ids) != len(set(source_ids)):
                raise ValueError(
                    "保真补写 unit.source_segment_ids 不得重复"
                )
            target_scene = None
            if candidate.source_scene_owners:
                owner_scene_keys = {
                    candidate.source_scene_owners.get(source_id)
                    for source_id in source_ids
                }
                if len(owner_scene_keys) != 1 or None in owner_scene_keys:
                    raise ValueError(
                        "保真补写 unit 跨越多个 source owner："
                        f"{source_ids} -> {sorted(str(value) for value in owner_scene_keys)}"
                    )
                owner_scene_key = next(iter(owner_scene_keys))
                target_scene = scenes.get(owner_scene_key)
                if target_scene is None:
                    raise ValueError(
                        f"保真补写缺少 source owner scene：{owner_scene_key}"
                    )
            else:
                target_index = min(
                    source_order[source_id] for source_id in source_ids
                )
                nearest_scene = min(
                    (
                        (
                            min(
                                abs(source_order[source_id] - target_index)
                                for source_id in unit.source_segment_ids
                                if source_id in source_order
                            ),
                            scene,
                        )
                        for scene, unit in existing_units
                        if any(
                            source_id in source_order
                            for source_id in unit.source_segment_ids
                        )
                    ),
                    key=lambda item: item[0],
                    default=(0, scenes.get(insertion.scene_key)),
                )[1]
                target_scene = (
                    nearest_scene or scenes.get(insertion.scene_key)
                )
            if target_scene is None:
                continue
            inserted += 1
            event_key = f"fidelity-r{round_no}-{inserted}"
            while event_key in occupied_keys:
                inserted += 1
                event_key = f"fidelity-r{round_no}-{inserted}"
            occupied_keys.add(event_key)
            patch_unit.event_key = event_key
            patch_unit.source_segment_ids = source_ids
            target_scene.units.append(patch_unit)
            existing_units.append((target_scene, patch_unit))

    for scene in candidate.scenes:
        scene.units.sort(
            key=lambda unit: min(
                (
                    source_order[source_id]
                    for source_id in unit.source_segment_ids
                    if source_id in source_order
                ),
                default=len(segments),
            )
        )
    candidate.events = []
    candidate.beats = []
    candidate.coverage = []
    return inserted


async def _complete_screenplay_ir_fidelity(
    candidate: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    parent_artifact_id: str | None,
    narrative_blueprint: NarrativeBlueprint | None = None,
) -> ScreenplayGenerationIR:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace
    from app.identity_adjudication import adjudicate_screenplay_ir_identities

    candidate = await adjudicate_screenplay_ir_identities(
        candidate,
        episode=episode,
        source_text=source_text,
        bible=bible,
    )

    patched = False
    consecutive_empty_patches = 0
    fidelity_error: ValueError | None = None
    initial_gap_context = _ir_fidelity_patch_context(candidate, source_text)
    missing_scene_plan_count = (
        max(
            0,
            len(derive_blueprint_scene_plans(narrative_blueprint))
            - len(candidate.scenes),
        )
        if narrative_blueprint is not None else 0
    )
    configured_max_rounds = max(
        1, min(8, int(get_setting("screenplay_fidelity_max_rounds") or 8))
    )
    max_rounds = min(
        configured_max_rounds,
        max(
            2,
            len(initial_gap_context["windows_requiring_expansion"])
            + missing_scene_plan_count,
        ),
    )
    for round_no in range(1, max_rounds + 1):
        try:
            compile_screenplay_ir(
                candidate.model_copy(deep=True),
                episode=episode,
                source_text=source_text,
                bible=bible,
            )
            if patched:
                trace = current_trace()
                completed_artifact = evidence_repository.create_artifact(
                    EvidenceArtifact(
                        type="screenplay_generation_ir",
                        scope_type="episode",
                        scope_id=str(episode.get("id") or ""),
                        status="candidate",
                        trust_level="T1",
                        content=candidate.model_dump(mode="json"),
                        parent_artifact_ids=(
                            [parent_artifact_id]
                            if parent_artifact_id else []
                        ),
                        contract_version=IR_VERSION,
                        prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
                        model_snapshot={
                            "compiler_version": IR_COMPILER_VERSION,
                            "fidelity_completion_rounds": round_no - 1,
                            "blueprint_hash": (
                                _narrative_blueprint_content_hash(
                                    narrative_blueprint
                                )
                            ),
                        },
                    ),
                    step_run_id=trace.step_run_id,
                )
                object.__setattr__(
                    candidate,
                    "evidence_artifact_id",
                    completed_artifact["id"],
                )
            return candidate
        except ValueError as exc:
            if not isinstance(exc, ScreenplayIRFidelityError):
                raise
            fidelity_error = exc

        context = _ir_fidelity_patch_context(candidate, source_text)
        if narrative_blueprint is not None:
            plans = derive_blueprint_scene_plans(narrative_blueprint)
            (
                _remaining_plans,
                internal_plans,
                selected_plans,
                _repair_source_ids,
            ) = _select_fidelity_blueprint_plans(
                context,
                plans,
                candidate_scene_count=len(candidate.scenes),
            )
            original_windows = context["windows_requiring_expansion"]
            original_missing_source_ids = context["missing_source_ids"]

            def project_windows(
                allowed_source_ids: set[str],
            ) -> list[dict[str, Any]]:
                projected: list[dict[str, Any]] = []
                for window in original_windows:
                    source_segments = [
                        segment
                        for segment in window["source_segments"]
                        if segment["source_segment_id"]
                        in allowed_source_ids
                    ]
                    if not source_segments:
                        continue
                    source_ids = {
                        segment["source_segment_id"]
                        for segment in source_segments
                    }
                    existing_units = [
                        unit
                        for unit in window["existing_units"]
                        if source_ids.intersection(
                            unit["source_segment_ids"]
                        )
                    ]
                    source_chars = sum(
                        len(textmatch.condense(segment["text"]))
                        for segment in source_segments
                    )
                    adapted_chars = sum(
                        len(textmatch.condense(unit["text"]))
                        for unit in existing_units
                    )
                    target_chars = math.ceil(
                        source_chars * IR_MIN_ADAPTED_SOURCE_RATIO
                    )
                    missing_source_ids = [
                        source_id
                        for source_id in window["missing_source_ids"]
                        if source_id in allowed_source_ids
                    ]
                    if (
                        adapted_chars >= target_chars
                        and not missing_source_ids
                    ):
                        continue
                    projected.append({
                        **window,
                        "source_range": (
                            f"{source_segments[0]['source_segment_id']}-"
                            f"{source_segments[-1]['source_segment_id']}"
                        ),
                        "source_chars": source_chars,
                        "existing_adapted_chars": adapted_chars,
                        "minimum_final_adapted_chars": target_chars,
                        "minimum_additional_chars": max(
                            0, target_chars - adapted_chars,
                        ),
                        "missing_source_ids": missing_source_ids,
                        "source_segments": source_segments,
                        "existing_units": existing_units,
                    })
                return projected

            allowed_source_ids = {
                source_id
                for plan in (
                    internal_plans[:6]
                    if internal_plans
                    else selected_plans
                )
                for source_id in plan.source_segment_ids
            }
            selected_windows = project_windows(allowed_source_ids)
            if not selected_windows and internal_plans and _remaining_plans:
                # A batch that happens to contain no gap is not a failure --
                # the gap simply lives in a later batch.  Only inspecting the
                # first six remaining plans made that an episode-ending
                # ValueError whenever the missing source sat further along
                # (production EP2 died at IR_MERGE this way).  Walk the
                # remaining plans batch by batch until one actually has work.
                for start in range(0, len(_remaining_plans), 6):
                    batch = _remaining_plans[start:start + 6]
                    if not batch:
                        break
                    batch_source_ids = {
                        source_id
                        for plan in batch
                        for source_id in plan.source_segment_ids
                    }
                    batch_windows = project_windows(batch_source_ids)
                    if batch_windows:
                        selected_plans = batch
                        allowed_source_ids = batch_source_ids
                        selected_windows = batch_windows
                        break
            context["required_remaining_scene_plans"] = [
                plan.model_dump(mode="json")
                for plan in selected_plans
            ]
            context["missing_source_ids"] = [
                source_id
                for source_id in original_missing_source_ids
                if source_id in allowed_source_ids
            ]
            context["windows_requiring_expansion"] = selected_windows
        else:
            context["windows_requiring_expansion"] = context[
                "windows_requiring_expansion"
            ][:2]
        windows = context["windows_requiring_expansion"]
        if not windows:
            # 编译器报了保真缺口，窗口投影器却找不到任何可补窗口——两者对
            # "还缺什么"的判断不一致。裸抛 "没有可处理的缺口窗口" 会把编译器
            # 的诊断整个吞掉，让这一类失败无从下手；把它原样带出来。
            raise ValueError(
                "IR 保真补写没有可处理的缺口窗口；编译器报告的缺口："
                + str(fidelity_error)[:400]
            )
        if candidate.source_scene_owners:
            context["source_scene_owners"] = dict(
                candidate.source_scene_owners
            )
            context["scene_derivations"] = list(
                candidate.scene_derivations
            )
        prompt = (
            "任务：只补写现有剧本 IR 中缺失或过度压缩的剧情单元，不重写整集。\n"
            f"这是第 {round_no} 轮局部补写；只要上下文仍列出缺口，禁止返回空数组。\n"
            "每个窗口都给出了原文、已有 units 和最低补写字符数。新增 units 必须把"
            "遗漏的动作、人物反应、对白关系、因果桥梁和场景转换真正写进 text；"
            "禁止重复已有内容凑字数。\n"
            "source_segment_ids 只能引用对应窗口内 SRC，必须按原文顺序且连续；"
            "若上下文含 source_scene_owners，每个 SRC 只能写入其 owner scene；"
            "跨场信息只能读取 scene_derivations，不得重复消费来源场 SRC。"
            "dialogue.source_text 必须逐字来自声明的 SRC，并使用 identities 中已有"
            " speaker_key。scene_key 从现有 scenes 选择。每个 insertion 的 units 按"
            "播放顺序输出，event_key 可使用任意临时唯一值，后端会重编号。"
            "若缺失 SRC 是现有正文之后的连续尾段，必须通过 new_scenes 续写必要的新场次，"
            "不得把不同时空强塞进最后一个旧场；非尾段缺口才使用 insertions。\n\n"
            "若上下文包含 required_remaining_scene_plans，new_scenes 必须逐项使用其"
            " key、scene_heading、顺序和 source_segment_ids 分配；禁止合并、跳过或"
            "自行改名蓝图场次。每个新 scene 的 units 只能引用该 plan 允许的 SRC。\n\n"
            "保真缺口上下文：\n"
            + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
            + "\n\n只输出 JSON："
            '{"new_scenes":[{"key":"sc_next",'
            '"scene_heading":"【场】日 / 地点","story_function":"",'
            '"summary":"","conflict":"","turn":"","units":['
            '{"kind":"action","text":"尾段可拍动作",'
            '"event_key":"tail1","source_segment_ids":["SRC0100"]}]}],'
            '"insertions":[{"scene_key":"sc1",'
            '"insert_after_event_key":"ev1","units":['
            '{"kind":"action","text":"新增可拍动作",'
            '"event_key":"patch1","source_segment_ids":["SRC0003"]},'
            '{"kind":"dialogue","text":"改编台词","event_key":"patch2",'
            '"source_segment_ids":["SRC0003"],"speaker_key":"person_a",'
            '"function":"statement","source_text":"原文逐字话语",'
            '"chain_key":"dc_patch"}]}]}'
        )
        structured_patch = await model_gateway.chat_structured(
            [
                {"role": "system", "content": SYSTEM_PREFIX},
                {"role": "user", "content": prompt},
            ],
            model_type=_IRFidelityPatch,
            validate=None,
            operation_id=(
                f"screenplay.ir-fidelity:{IR_VERSION}:"
                f"{episode.get('id') or episode.get('episode_no')}:"
                f"{round_no}:"
                + hashlib.sha256(
                    json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
            ),
            temperature=0.3,
            # 推理模型的 reasoning token 计入 completion_tokens，固定 8192 会在
            # 写出补丁之前就被思考耗尽（生产上 EP3 拿到 finish_reason=length /
            # completion_tokens=8193）。预算只是上限，真实成本按实际用量结算。
            max_tokens=IR_FIDELITY_PATCH_MAX_TOKENS,
            format_retry_limit=int(
                get_setting("screenplay_format_retry_limit") or 1
            ),
            semantic_retry_limit=0,
            call_meta={
                "stage": "剧本来源保真局部补写",
                "stage_key": "screenplay_ir_fidelity_patch",
                "call_role": "stage_repair",
                "call_role_label": "局部剧情补写",
                "repair_round": round_no,
                "episode_id": str(episode.get("id") or ""),
                "generation_contract": IR_VERSION,
                "compiler_version": IR_COMPILER_VERSION,
                "expected_json": True,
                "reuse_successful_operation": True,
            },
            repair_context=json.dumps(
                context, ensure_ascii=False, separators=(",", ":")
            ),
        )
        raw = structured_patch.model_dump_json()
        trace = current_trace()
        raw_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_generation_ir_fidelity_patch_raw",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="candidate",
                trust_level="T0",
                content={"raw_output": raw, "round": round_no},
                parent_artifact_ids=(
                    [parent_artifact_id] if parent_artifact_id else []
                ),
                contract_version=IR_VERSION,
                prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
            ),
            step_run_id=trace.step_run_id,
        )
        payload = structured_patch.model_dump(mode="json")
        for new_scene in payload.get("new_scenes", []):
            if not isinstance(new_scene, dict):
                continue
            new_scene.setdefault(
                "story_function",
                str(new_scene.get("summary") or "推进本场剧情"),
            )
        patch = _IRFidelityPatch.model_validate(payload)
        inserted = _merge_ir_fidelity_patch(
            candidate,
            patch,
            source_text,
            round_no=round_no,
        )
        if not inserted:
            consecutive_empty_patches += 1
            if consecutive_empty_patches >= 2:
                raise ValueError(
                    "IR 保真补写连续两轮未返回任何可合并 unit"
                )
            continue
        consecutive_empty_patches = 0
        patched = True
        normalized_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_generation_ir_fidelity_patch",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="validated",
                trust_level="T1",
                content=patch.model_dump(mode="json"),
                parent_artifact_ids=[raw_artifact["id"]],
                contract_version=IR_VERSION,
                prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
                model_snapshot={
                    "inserted_units": inserted,
                    "resolved_source_ids": sorted(set(
                        source_id
                        for insertion in patch.insertions
                        for unit in insertion.units
                        for source_id in unit.source_segment_ids
                    ).union(
                        source_id
                        for scene in patch.new_scenes
                        for unit in scene.units
                        for source_id in unit.source_segment_ids
                    )),
                    "missing_source_ids_before": context["missing_source_ids"],
                },
            ),
            step_run_id=trace.step_run_id,
        )
        parent_artifact_id = normalized_artifact["id"]

    compile_screenplay_ir(
        candidate.model_copy(deep=True),
        episode=episode,
        source_text=source_text,
        bible=bible,
    )
    trace = current_trace()
    completed_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="candidate",
            trust_level="T1",
            content=candidate.model_dump(mode="json"),
            parent_artifact_ids=(
                [parent_artifact_id] if parent_artifact_id else []
            ),
            contract_version=IR_VERSION,
            prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
            model_snapshot={
                "compiler_version": IR_COMPILER_VERSION,
                "fidelity_completion_rounds": max_rounds,
                "blueprint_hash": _narrative_blueprint_content_hash(
                    narrative_blueprint
                ),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    object.__setattr__(
        candidate,
        "evidence_artifact_id",
        completed_artifact["id"],
    )
    return candidate


class StoryboardShotDraft(BaseModel):
    """逐镜头分镜输出合同：每次只让模型生成一个镜头，降低格式和内容同时失控的概率。"""

    episode_no: int
    shot: Shot
    is_final: bool = False

    @model_validator(mode="after")
    def require_production_frames(self) -> "StoryboardShotDraft":
        """Reject a draft that cannot be handed to keyframe/video production."""
        missing = [
            field
            for field in ("first_frame_desc", "last_frame_desc")
            if not str(getattr(self.shot, field, "") or "").strip()
        ]
        if missing:
            raise ValueError(
                "shot 缺少分镜生产必填字段：" + ", ".join(missing)
            )
        return self


class DirectedPhysicalInteraction(BaseModel):
    kind: Literal[
        "none",
        "person_person_contact",
        "person_object_contact",
        "spatial_interaction",
    ] = "none"
    participants: list[str] = Field(default_factory=list)
    phase: Literal["none", "approach", "established", "separated"] = "none"
    contact_point: str = ""
    contact_point_visible: bool = False


class DirectedSceneShotDraft(BaseModel):
    """Only fields that still require directing judgement.

    Outline-owned IDs, timing, cast, dialogue, evidence and continuity are
    hydrated by the server. Optional legacy fields remain readable so an
    interrupted pre-upgrade scene candidate can still resume.
    """

    shot_no: int
    shot_size: str
    camera_angle: str
    camera_move: str
    camera_motivation: str
    action_desc: str
    first_frame_desc: str
    last_frame_desc: str
    spatial_anchor: str = ""
    physical_interaction: DirectedPhysicalInteraction = Field(
        default_factory=DirectedPhysicalInteraction
    )
    dialogue_emotions: dict[str, str] = Field(default_factory=dict)
    required_text: RequiredOnScreenText | None = None
    source_excerpt: str = ""
    purpose: str = ""
    context_requirement_ids: list[str] = Field(default_factory=list)
    resulting_change: str = ""
    readability_focus: str = ""
    duration_s: int | None = None
    characters: list[str] = Field(default_factory=list)
    dialogues: list[Dialogue] = Field(default_factory=list)
    transition: str = ""
    repeat_of_shot_id: str | None = None
    repeat_gain: str = ""


class DirectedScenePackDraft(BaseModel):
    episode_no: int
    scene_id: str
    shots: list[DirectedSceneShotDraft]


_SHOT_NULLABLE_TEXT_FIELDS = frozenset({
    "story_event_id", "purpose", "state_in", "primary_action", "emotion_beat",
    "state_out", "observed_state_out", "continuity_mode", "prompt_contract_version",
    "camera_angle", "spatial_anchor", "scene_name", "first_frame_desc",
    "last_frame_desc", "source_excerpt",
})
_SHOT_NULLABLE_LIST_FIELDS = frozenset({
    "dialogues", "new_information_ids", "reinforcement_info_ids", "characters_visible",
    "audio_cast", "audio_timeline", "reference_roles", "do_not_repeat", "risk_tags",
    "event_ids", "supporting_action_ids", "action_phase_ids", "visible_entity_ids",
    "offscreen_action_actor_ids", "offscreen_action_target_ids", "audience_state_paths",
    "action_participant_deliveries",
    "planned_state_in_fact_ids",
    "planned_delta_add_fact_ids", "planned_delta_remove_fact_ids",
    "planned_state_out_fact_ids", "completed_before_action_ids",
    "completed_before_action_phase_ids", "reserved_future_event_ids",
    "readability_window_ids",
})
_STORYBOARD_NARRATIVE_AUTHORITY_FIELDS = (
    "shot_id", "scene_id", "event_ids", "spine_beat_ids", "key_line_ids",
    "primary_action_id",
    "supporting_action_ids", "action_phase_ids", "visible_entity_ids",
    "characters_visible",
    "offscreen_action_actor_ids", "offscreen_action_target_ids",
    "action_participant_deliveries",
    "shot_contribution", "audience_state_paths",
    "planned_state_in_fact_ids", "planned_delta_add_fact_ids",
    "planned_delta_remove_fact_ids", "planned_state_out_fact_ids",
    "completed_before_action_ids", "completed_before_action_phase_ids",
    "reserved_future_event_ids", "readability_window_ids",
    "narrative_boundary_from_previous",
)
_STORYBOARD_VISUAL_STAGING_FIELDS = frozenset({
    "visible_entity_ids",
    "characters_visible",
    "offscreen_action_actor_ids",
    "offscreen_action_target_ids",
})


def _bound_action_relation_ids(
    screenplay: EpisodeScreenplay,
    brief: StoryboardOutlineShot | None,
    *,
    bible: Bible | None = None,
) -> tuple[list[str], list[str]]:
    """Return graph-owned actor/target relations for one shot task."""
    if brief is None or screenplay.narrative_plan is None:
        return [], []
    actions = {
        action.action_id: action
        for action in screenplay.narrative_plan.atomic_actions
    }
    action_event_owner = {
        action_id: event.event_id
        for event in screenplay.narrative_plan.events
        for action_id in event.action_ids
    }
    action_ids = [
        *([brief.primary_action_id] if brief.primary_action_id else []),
        *(brief.supporting_action_ids or []),
    ]
    from app.identity_contracts import storyboard_action_relation_ids

    relations = [
        storyboard_action_relation_ids(
            screenplay,
            action_event_owner.get(action_id, ""),
            action,
            bible=bible,
        )
        for action_id in action_ids
        for action in [actions.get(action_id)]
        if action is not None
    ]
    actor_ids = list(dict.fromkeys(
        actor_id for actors, _targets in relations for actor_id in actors
    ))
    target_ids = list(dict.fromkeys(
        target_id for _actors, targets in relations for target_id in targets
    ))
    return actor_ids, target_ids


def _resolve_legacy_story_event_id(
    outline_event_id: str,
    legacy_event_ids: list[str] | tuple[str, ...],
) -> str:
    """Join graph and legacy event IDs only when the normalized match is unique."""
    requested = str(outline_event_id or "").strip()
    available = list(dict.fromkeys(
        str(event_id or "").strip()
        for event_id in legacy_event_ids
        if str(event_id or "").strip()
    ))
    if not requested:
        return ""
    if requested in available:
        return requested

    alias = "".join(ch.casefold() for ch in requested if ch.isalnum())
    if not alias:
        return ""
    matches = [
        event_id
        for event_id in available
        if "".join(ch.casefold() for ch in event_id if ch.isalnum()) == alias
    ]
    return matches[0] if len(matches) == 1 else ""


def storyboard_shot_authority_context(
    screenplay: EpisodeScreenplay,
    brief: StoryboardOutlineShot | None,
    previous_shot: Shot | None = None,
    *,
    bible: Bible | None = None,
) -> dict[str, Any]:
    """Build the single authority context used by generation and rebound."""
    narrative_task = (
        brief.model_dump(mode="json")
        if brief is not None
        else None
    )
    if narrative_task is not None:
        actor_ids, target_ids = _bound_action_relation_ids(
            screenplay,
            brief,
            bible=bible,
        )
        narrative_task["_bound_action_actor_ids"] = actor_ids
        narrative_task["_bound_action_target_ids"] = target_ids
        if bible is not None and screenplay.narrative_plan is not None:
            from app.identity_contracts import (
                IdentityContractError,
                narrative_identity_resolver,
            )

            resolver = narrative_identity_resolver(bible, screenplay)
            visual_identities: list[dict[str, str]] = []
            for token in brief.visible_entity_ids:
                try:
                    identity = resolver.resolve(token, usage="visual")
                except IdentityContractError:
                    continue
                if any(
                    item["identity_id"] == identity.identity_id
                    for item in visual_identities
                ):
                    continue
                visual_identities.append({
                    "identity_id": identity.identity_id,
                    "display_name": identity.display_name,
                })
            narrative_task["_visual_identities"] = visual_identities
    return {
        "outline_story_event_id": (
            str(brief.story_event_id or "")
            if brief is not None
            else ""
        ),
        "legacy_story_event_ids": [
            str(event.event_id or "").strip()
            for event in (screenplay.events or [])
            if str(event.event_id or "").strip()
        ],
        "outline_narrative_task": narrative_task,
        "previous_scene_name": (
            str(previous_shot.scene_name or "")
            if previous_shot is not None
            else ""
        ),
        "previous_scene_time": (
            str(previous_shot.scene_time or "")
            if previous_shot is not None
            else ""
        ),
        "previous_last_frame_desc": (
            str(previous_shot.last_frame_desc or "")
            if previous_shot is not None
            else ""
        ),
    }


def normalize_storyboard_shot_candidate(
    obj: dict[str, Any],
    *,
    episode_no: int,
    shot_no: int,
    outline_story_event_id: str = "",
    legacy_story_event_ids: list[str] | tuple[str, ...] | None = None,
    outline_narrative_task: dict[str, Any] | None = None,
    previous_scene_name: str = "",
    previous_scene_time: str = "",
    previous_last_frame_desc: str = "",
    preserve_director_camera: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Losslessly normalize common LLM serialization mistakes at the boundary.

    The persisted ``Shot`` contract stays strict.  Only unambiguous null-to-
    empty conversions and server-owned identity fields are repaired here;
    objects and other incompatible values are deliberately left for schema
    validation instead of being hidden behind broad ``str(value)`` coercion.
    """
    normalized = dict(obj)
    changes: list[dict[str, Any]] = []

    if normalized.get("episode_no") != episode_no:
        changes.append({
            "field": "episode_no", "from": normalized.get("episode_no"),
            "to": episode_no, "reason": "server_authoritative",
        })
        normalized["episode_no"] = episode_no

    raw_shot = normalized.get("shot")
    if not isinstance(raw_shot, dict):
        return normalized, changes
    shot = dict(raw_shot)
    normalized["shot"] = shot

    # A common near-valid response closes ``shot`` one brace too early and
    # continues its remaining fields at the draft root. ``raw_decode`` can
    # still recover that root object, but Pydantic would otherwise ignore the
    # misplaced fields. Move only declared Shot fields and never overwrite a
    # non-empty value already inside ``shot``.
    for field in Shot.model_fields:
        if field == "is_final" or field not in normalized:
            continue
        root_value = normalized.pop(field)
        current_value = shot.get(field)
        if current_value not in (None, "", [], {}):
            continue
        shot[field] = root_value
        changes.append({
            "field": f"shot.{field}",
            "from": current_value,
            "to": root_value,
            "reason": "misplaced_root_field",
        })

    if shot.get("shot_no") != shot_no:
        changes.append({
            "field": "shot.shot_no", "from": shot.get("shot_no"),
            "to": shot_no, "reason": "server_authoritative",
        })
        shot["shot_no"] = shot_no

    for field in _SHOT_NULLABLE_TEXT_FIELDS:
        if field in shot and shot[field] is None:
            shot[field] = ""
            changes.append({
                "field": f"shot.{field}", "from_type": "null", "to": "",
                "reason": "nullable_text_normalization",
            })
    for field in _SHOT_NULLABLE_LIST_FIELDS:
        if field in shot and shot[field] is None:
            shot[field] = []
            changes.append({
                "field": f"shot.{field}", "from_type": "null", "to": [],
                "reason": "nullable_list_normalization",
            })

    # Narrative graph IDs and the legacy event ledger can use different
    # separators. Only a unique normalized join may enter the legacy field.
    authoritative_story_event_id = outline_story_event_id
    if legacy_story_event_ids is not None:
        authoritative_story_event_id = _resolve_legacy_story_event_id(
            outline_story_event_id,
            legacy_story_event_ids,
        )
    if (
        outline_story_event_id
        and shot.get("story_event_id") != authoritative_story_event_id
    ):
        changes.append({
            "field": "shot.story_event_id", "from": shot.get("story_event_id"),
            "to": authoritative_story_event_id,
            "reason": (
                "outline_legacy_event_authority"
                if legacy_story_event_ids is not None
                else "outline_authoritative"
            ),
        })
        shot["story_event_id"] = authoritative_story_event_id

    # Narrative allocation is authored and validated at outline time.  The
    # shot model may realize it visually, but must not be asked to retype or
    # reinterpret these server-owned IDs, ledgers, budgets, and boundaries.
    if isinstance(outline_narrative_task, dict):
        for field in _STORYBOARD_NARRATIVE_AUTHORITY_FIELDS:
            if field in _STORYBOARD_VISUAL_STAGING_FIELDS:
                continue
            if field not in outline_narrative_task:
                continue
            planned = outline_narrative_task[field]
            if shot.get(field) == planned:
                continue
            changes.append({
                "field": f"shot.{field}",
                "from": shot.get(field),
                "to": planned,
                "reason": "outline_narrative_authority",
            })
            shot[field] = deepcopy(planned)

        def _tokens(value: object) -> list[str]:
            if not isinstance(value, list):
                return []
            return list(dict.fromkeys(
                token
                for item in value
                if (token := str(item or "").strip())
            ))

        planned_entity_ids = _tokens(
            outline_narrative_task.get("visible_entity_ids")
        )
        planned_names = _tokens(
            outline_narrative_task.get("characters_visible")
        )
        candidate_entity_ids = _tokens(shot.get("visible_entity_ids"))
        candidate_characters = _tokens(shot.get("characters"))
        candidate_visible_names = _tokens(shot.get("characters_visible"))
        visual_identity_rows = [
            item
            for item in outline_narrative_task.get("_visual_identities", [])
            if isinstance(item, dict)
        ]
        identity_id_by_display_name = {
            str(item.get("display_name") or "").strip():
                str(item.get("identity_id") or "").strip()
            for item in visual_identity_rows
            if (
                str(item.get("display_name") or "").strip()
                and str(item.get("identity_id") or "").strip()
            )
        }
        mapped_candidate_entity_ids = [
            identity_id_by_display_name[name]
            for name in candidate_characters
            if name in identity_id_by_display_name
        ]
        if (
            candidate_characters
            and len(mapped_candidate_entity_ids) == len(candidate_characters)
        ):
            candidate_entity_ids = list(dict.fromkeys(
                mapped_candidate_entity_ids
            ))
        bound_actor_ids = set(_tokens(
            outline_narrative_task.get("_bound_action_actor_ids")
        ))
        bound_target_ids = set(_tokens(
            outline_narrative_task.get("_bound_action_target_ids")
        ))
        has_relation_authority = (
            "_bound_action_actor_ids" in outline_narrative_task
            and "_bound_action_target_ids" in outline_narrative_task
        )
        declared_offscreen = set(_tokens([
            *_tokens(shot.get("offscreen_action_actor_ids")),
            *_tokens(shot.get("offscreen_action_target_ids")),
        ]))
        contracted_offscreen = {
            str(item.get("participant_id") or "").strip()
            for item in (
                outline_narrative_task.get("action_participant_deliveries")
                or []
            )
            if (
                isinstance(item, dict)
                and str(item.get("participant_id") or "").strip()
                and str(item.get("action_id") or "").strip() in {
                    str(outline_narrative_task.get("primary_action_id") or "").strip(),
                    *(
                        str(action_id or "").strip()
                        for action_id in (
                            outline_narrative_task.get("supporting_action_ids")
                            or []
                        )
                    ),
                }
                and any((
                    bool(item.get("audible")),
                    bool(item.get("visible_effect")),
                    bool(item.get("visible_reaction")),
                ))
                and any(
                    str(evidence_id or "").strip()
                    for evidence_id in (item.get("evidence_ids") or [])
                )
            )
        }
        participant_ids = bound_actor_ids | bound_target_ids
        visual_partition_is_valid = bool(
            has_relation_authority
            and candidate_entity_ids
            and candidate_characters
            and candidate_visible_names
            and set(candidate_entity_ids).issubset(planned_entity_ids)
            and (
                not planned_names
                or set(candidate_characters).issubset(planned_names)
            )
            and set(candidate_characters) == set(candidate_visible_names)
            and len(candidate_entity_ids) == len(candidate_characters)
            and declared_offscreen.issubset(participant_ids)
            and declared_offscreen.issubset(contracted_offscreen)
            and (
                participant_ids - set(candidate_entity_ids)
            ).issubset(contracted_offscreen)
        )
        if visual_partition_is_valid:
            canonical_offscreen_actors = [
                identity_id
                for identity_id in _tokens(
                    outline_narrative_task.get("_bound_action_actor_ids")
                )
                if identity_id not in candidate_entity_ids
                and identity_id in contracted_offscreen
            ]
            canonical_offscreen_targets = [
                identity_id
                for identity_id in _tokens(
                    outline_narrative_task.get("_bound_action_target_ids")
                )
                if identity_id not in candidate_entity_ids
                and identity_id in contracted_offscreen
            ]
            for field, value in (
                ("visible_entity_ids", candidate_entity_ids),
                ("characters", candidate_characters),
                ("characters_visible", candidate_visible_names),
                ("offscreen_action_actor_ids", canonical_offscreen_actors),
                ("offscreen_action_target_ids", canonical_offscreen_targets),
            ):
                if shot.get(field) == value:
                    continue
                changes.append({
                    "field": f"shot.{field}",
                    "from": shot.get(field),
                    "to": value,
                    "reason": "structured_visual_staging_partition",
                })
                shot[field] = value
        else:
            for field in (
                "visible_entity_ids",
                "offscreen_action_actor_ids",
                "offscreen_action_target_ids",
            ):
                if field not in outline_narrative_task:
                    continue
                planned = _tokens(outline_narrative_task.get(field))
                if shot.get(field) == planned:
                    continue
                changes.append({
                    "field": f"shot.{field}",
                    "from": shot.get(field),
                    "to": planned,
                    "reason": "outline_narrative_authority",
                })
                shot[field] = planned
            if planned_names:
                for field in ("characters", "characters_visible"):
                    if shot.get(field) == planned_names:
                        continue
                    changes.append({
                        "field": f"shot.{field}",
                        "from": shot.get(field),
                        "to": planned_names,
                        "reason": "outline_visual_identity_authority",
                    })
                    shot[field] = deepcopy(planned_names)

        # Continuity state keeps look/outfit facts for identities that have
        # left the frame.  Those facts must remain available, but a stale
        # ``visibility=visible`` makes the prompt compiler reintroduce the
        # identity as a character reference after the shot roster has already
        # been projected to its task-owned subset.
        selected_visual_tokens = {
            *_tokens(shot.get("visible_entity_ids")),
            *_tokens(shot.get("characters")),
            *_tokens(shot.get("characters_visible")),
        }
        for item in visual_identity_rows:
            identity_id = str(item.get("identity_id") or "").strip()
            display_name = str(item.get("display_name") or "").strip()
            if (
                identity_id in selected_visual_tokens
                or display_name in selected_visual_tokens
            ):
                selected_visual_tokens.update({
                    token
                    for token in (identity_id, display_name)
                    if token
                })
        hidden_visibilities = {
            "hidden", "offscreen", "not_visible", "画外", "不可见",
        }
        for field in ("continuity_state_in", "continuity_state_out"):
            state = shot.get(field)
            if not isinstance(state, dict):
                continue
            character_states = state.get("characters")
            if not isinstance(character_states, dict):
                continue
            normalized_state = deepcopy(state)
            normalized_characters = normalized_state["characters"]
            hidden_identities: list[str] = []
            for token, character_state in normalized_characters.items():
                if (
                    str(token or "").strip() in selected_visual_tokens
                    or not isinstance(character_state, dict)
                    or str(character_state.get("visibility") or "")
                    .strip().lower() in hidden_visibilities
                ):
                    continue
                character_state["visibility"] = "hidden"
                hidden_identities.append(str(token))
            if not hidden_identities:
                continue
            shot[field] = normalized_state
            changes.append({
                "field": f"shot.{field}.characters",
                "hidden_identities": hidden_identities,
                "reason": "structured_visual_state_partition",
            })

        for field in ("scene_name", "scene_time"):
            planned = str(outline_narrative_task.get(field) or "")
            if planned and shot.get(field) != planned:
                changes.append({
                    "field": f"shot.{field}",
                    "from": shot.get(field),
                    "to": planned,
                    "reason": "outline_scene_authority",
                })
                shot[field] = planned
        planned_scene_name = str(
            outline_narrative_task.get("scene_name") or ""
        )
        planned_scene_time = str(
            outline_narrative_task.get("scene_time") or ""
        )
        planned_continuity = str(
            outline_narrative_task.get("continuity_mode") or ""
        )
        expected_continuity = planned_continuity
        if previous_scene_name or previous_scene_time:
            changed_scene = (
                planned_scene_name != previous_scene_name
                or _scene_time_changed(
                    previous_scene_time,
                    planned_scene_time,
                )
            )
            if changed_scene:
                expected_continuity = "scene_change"
            elif expected_continuity == "scene_change":
                expected_continuity = "same_scene_cut"
        if (
            expected_continuity
            and shot.get("continuity_mode") != expected_continuity
        ):
            changes.append({
                "field": "shot.continuity_mode",
                "from": shot.get("continuity_mode"),
                "to": expected_continuity,
                "reason": "derived_scene_continuity",
            })
            shot["continuity_mode"] = expected_continuity
        if (
            expected_continuity == "scene_change"
            and shot.get("transition") in {None, "", "硬切"}
        ):
            changes.append({
                "field": "shot.transition",
                "from": shot.get("transition"),
                "to": "叠化",
                "reason": "derived_scene_transition",
            })
            shot["transition"] = "叠化"
        elif (
            expected_continuity
            and expected_continuity != "scene_change"
            and shot.get("transition") != "硬切"
        ):
            changes.append({
                "field": "shot.transition",
                "from": shot.get("transition"),
                "to": "硬切",
                "reason": "derived_scene_transition",
            })
            shot["transition"] = "硬切"
        inherits_video_tail = bool(
            expected_continuity
            and expected_continuity != "scene_change"
            and (
                previous_scene_name
                or previous_scene_time
                or previous_last_frame_desc
            )
        )
        if shot.get("continuity_from_prev") != inherits_video_tail:
            changes.append({
                "field": "shot.continuity_from_prev",
                "from": shot.get("continuity_from_prev"),
                "to": inherits_video_tail,
                "reason": "derived_video_tail_input",
            })
            shot["continuity_from_prev"] = inherits_video_tail
        previous_tail_desc = str(previous_last_frame_desc or "").strip()
        if (
            inherits_video_tail
            and previous_tail_desc
            and shot.get("first_frame_desc") != previous_tail_desc
        ):
            changes.append({
                "field": "shot.first_frame_desc",
                "from": shot.get("first_frame_desc"),
                "to": previous_tail_desc,
                "reason": "previous_video_tail_first_frame",
            })
            shot["first_frame_desc"] = previous_tail_desc
            if shot.get("state_in") != previous_tail_desc:
                changes.append({
                    "field": "shot.state_in",
                    "from": shot.get("state_in"),
                    "to": previous_tail_desc,
                    "reason": "previous_video_tail_state_in",
                })
                shot["state_in"] = previous_tail_desc

        planned_audio_cast = outline_narrative_task.get("audio_cast")
        if isinstance(planned_audio_cast, list):
            has_spoken_audio = bool(shot.get("dialogues")) or any(
                isinstance(item, dict)
                and item.get("type") in {
                    "spoken_dialogue",
                    "offscreen_voice",
                }
                and str(item.get("text") or "").strip()
                for item in (shot.get("audio_timeline") or [])
            )
            if (
                not outline_narrative_task.get("key_line_ids")
                and not has_spoken_audio
            ):
                planned_audio_cast = []
            if shot.get("audio_cast") != planned_audio_cast:
                changes.append({
                    "field": "shot.audio_cast",
                    "from": shot.get("audio_cast"),
                    "to": planned_audio_cast,
                    "reason": (
                        "silent_outline_audio_cast_cleared"
                        if not planned_audio_cast
                        else "outline_audio_authority"
                    ),
                })
                shot["audio_cast"] = deepcopy(planned_audio_cast)
            if (
                not planned_audio_cast
                and not outline_narrative_task.get("key_line_ids")
            ):
                removed_spoken = bool(
                    shot.get("dialogues")
                    or shot.get("narration")
                    or any(
                        isinstance(item, dict)
                        and item.get("type") in {
                            "spoken_dialogue",
                            "offscreen_voice",
                        }
                        for item in (shot.get("audio_timeline") or [])
                    )
                )
                shot["dialogues"] = []
                shot["narration"] = ""
                shot["audio_timeline"] = [
                    item
                    for item in (shot.get("audio_timeline") or [])
                    if not (
                        isinstance(item, dict)
                        and item.get("type") in {
                            "spoken_dialogue",
                            "offscreen_voice",
                        }
                    )
                ]
                if removed_spoken:
                    changes.append({
                        "field": "shot.spoken_contract",
                        "reason": "unassigned_spoken_content_removed",
                    })

        planned_budget = outline_narrative_task.get("capacity_budget")
        if isinstance(planned_budget, dict):
            def _content_chars(text: str) -> int:
                return len(re.sub(r"[\W_]+", "", text, flags=re.UNICODE))

            normalized_timeline = deepcopy(
                shot.get("audio_timeline") or []
            )
            spoken_cursor = 0.0
            timeline_timing_changed = False
            for item in normalized_timeline:
                if (
                    not isinstance(item, dict)
                    or item.get("type")
                    not in {"spoken_dialogue", "offscreen_voice"}
                ):
                    continue
                duration_s = (
                    _content_chars(str(item.get("text") or ""))
                    * float(config.VIDEO_DURATION_MIN_S)
                    / float(config.SPOKEN_CHARS_PER_5_SECONDS)
                )
                start_s = spoken_cursor
                end_s = spoken_cursor + duration_s
                if (
                    float(item.get("start_s") or 0) != start_s
                    or float(item.get("end_s") or 0) != end_s
                ):
                    item["start_s"] = start_s
                    item["end_s"] = end_s
                    timeline_timing_changed = True
                spoken_cursor = end_s
            if timeline_timing_changed:
                changes.append({
                    "field": "shot.audio_timeline",
                    "reason": "derived_spoken_timing",
                })
                shot["audio_timeline"] = normalized_timeline
            primary_onscreen_speaker = next(
                (
                    str(item.get("speaker_id") or "").strip()
                    for item in normalized_timeline
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "spoken_dialogue"
                        and str(item.get("speaker_id") or "").strip()
                    )
                ),
                "",
            )
            dialogue_framing_changed = False
            if primary_onscreen_speaker:
                for item in normalized_timeline:
                    if (
                        not isinstance(item, dict)
                        or item.get("type") != "spoken_dialogue"
                        or str(item.get("speaker_id") or "").strip()
                        in {"", primary_onscreen_speaker}
                    ):
                        continue
                    item["type"] = "offscreen_voice"
                    item["lip_sync"] = False
                    dialogue_framing_changed = True
            if dialogue_framing_changed:
                changes.append({
                    "field": "shot.audio_timeline",
                    "reason": "single_onscreen_speaker",
                })
                shot["audio_timeline"] = normalized_timeline
            spoken_segments = [
                item
                for item in normalized_timeline
                if (
                    isinstance(item, dict)
                    and item.get("type") in {
                        "spoken_dialogue",
                        "offscreen_voice",
                    }
                    and str(item.get("speaker_id") or "").strip()
                    and str(item.get("text") or "").strip()
                )
            ]
            if spoken_segments:
                normalized_dialogues = [
                    {
                        "speaker": str(item["speaker_id"]).strip(),
                        "line": str(item["text"]).strip(),
                        "emotion": str(
                            item.get("emotion") or "平静"
                        ),
                        "delivery": str(item["type"]),
                    }
                    for item in spoken_segments
                ]
                if shot.get("dialogues") != normalized_dialogues:
                    changes.append({
                        "field": "shot.dialogues",
                        "from": shot.get("dialogues"),
                        "to": normalized_dialogues,
                        "reason": "timeline_spoken_authority",
                    })
                    shot["dialogues"] = normalized_dialogues
            onscreen_speaker_ids = [
                str(item.get("speaker_id") or "").strip()
                for item in normalized_timeline
                if (
                    isinstance(item, dict)
                    and item.get("type") == "spoken_dialogue"
                    and str(item.get("speaker_id") or "").strip()
                )
            ]
            visible_characters = list(
                shot.get("characters_visible") or []
            )
            unique_onscreen_speakers = list(dict.fromkeys(
                onscreen_speaker_ids
            ))
            # Lip sync proves that a speaker must be visible; it does not prove
            # that every other action participant should disappear.  The
            # narrative task's actor/target partition owns the composition, so
            # audio normalization may add a declared on-screen speaker but must
            # never collapse an already valid multi-person staging to a solo.
            if has_relation_authority:
                declared_characters = set(_tokens(shot.get("characters")))
                normalized_visible = list(dict.fromkeys([
                    *visible_characters,
                    *(
                        speaker_id
                        for speaker_id in unique_onscreen_speakers
                        if speaker_id in declared_characters
                    ),
                ]))
            else:
                normalized_visible = (
                    unique_onscreen_speakers
                    if len(unique_onscreen_speakers) == 1
                    else list(dict.fromkeys([
                        *visible_characters,
                        *unique_onscreen_speakers,
                    ]))
                )
            if normalized_visible != visible_characters:
                changes.append({
                    "field": "shot.characters_visible",
                    "from": visible_characters,
                    "to": normalized_visible,
                    "reason": "lip_sync_speaker_visible",
                })
                shot["characters_visible"] = normalized_visible
            if has_relation_authority and len(unique_onscreen_speakers) == 1:
                try:
                    from app.continuity import dialogue_focus_subject

                    focus = dialogue_focus_subject(
                        Shot.model_validate(shot),
                        narrative_authority=True,
                    )
                except (TypeError, ValueError):
                    focus = None
                focus_identity_id = (
                    identity_id_by_display_name.get(focus or "")
                    if focus
                    else None
                )
                if focus and focus_identity_id:
                    canonical_offscreen_actors = [
                        identity_id
                        for identity_id in _tokens(
                            outline_narrative_task.get(
                                "_bound_action_actor_ids"
                            )
                        )
                        if identity_id != focus_identity_id
                    ]
                    canonical_offscreen_targets = [
                        identity_id
                        for identity_id in _tokens(
                            outline_narrative_task.get(
                                "_bound_action_target_ids"
                            )
                        )
                        if identity_id != focus_identity_id
                    ]
                    for field, value in (
                        ("characters", [focus]),
                        ("characters_visible", [focus]),
                        ("visible_entity_ids", [focus_identity_id]),
                        (
                            "offscreen_action_actor_ids",
                            canonical_offscreen_actors,
                        ),
                        (
                            "offscreen_action_target_ids",
                            canonical_offscreen_targets,
                        ),
                    ):
                        if shot.get(field) == value:
                            continue
                        changes.append({
                            "field": f"shot.{field}",
                            "from": shot.get(field),
                            "to": value,
                            "reason": "typed_dialogue_focus_projection",
                        })
                        shot[field] = value
            if unique_onscreen_speakers:
                # Only an explicit outline camera choice is authoritative.
                # A single spoken voice can accompany a full-body action or a
                # multi-person interaction, so it cannot generically imply a
                # close-up or a static camera.
                planned_shot_size = str(
                    ""
                    if preserve_director_camera
                    else outline_narrative_task.get("camera_size") or ""
                )
                if preserve_director_camera:
                    target_shot_size = shot.get("shot_size")
                elif planned_shot_size in SHOT_SIZES:
                    target_shot_size = planned_shot_size
                elif not has_relation_authority:
                    target_shot_size = "近景"
                else:
                    target_shot_size = shot.get("shot_size")
                if shot.get("shot_size") != target_shot_size:
                    changes.append({
                        "field": "shot.shot_size",
                        "from": shot.get("shot_size"),
                        "to": target_shot_size,
                        "reason": "outline_camera_authority",
                    })
                    shot["shot_size"] = target_shot_size
                planned_camera_move = str(
                    ""
                    if preserve_director_camera
                    else outline_narrative_task.get("camera_movement") or ""
                )
                target_camera_move = (
                    shot.get("camera_move")
                    if preserve_director_camera
                    else (
                        planned_camera_move
                        if planned_camera_move in CAMERA_MOVES
                        else (
                            shot.get("camera_move")
                            if has_relation_authority
                            else (
                                shot.get("camera_move")
                                if shot.get("camera_move")
                                in {"固定", "推近"}
                                else "固定"
                            )
                        )
                    )
                )
                if shot.get("camera_move") != target_camera_move:
                    changes.append({
                        "field": "shot.camera_move",
                        "from": shot.get("camera_move"),
                        "to": target_camera_move,
                        "reason": "outline_camera_authority",
                    })
                    shot["camera_move"] = target_camera_move

            dialogue_text = "".join(
                str(item.get("line") or "")
                for item in (shot.get("dialogues") or [])
                if isinstance(item, dict)
            )
            narration_text = str(shot.get("narration") or "")
            timeline = [
                item
                for item in (shot.get("audio_timeline") or [])
                if (
                    isinstance(item, dict)
                    and item.get("type") in {
                        "spoken_dialogue",
                        "offscreen_voice",
                    }
                )
            ]
            timeline_text = "".join(
                str(item.get("text") or "")
                for item in timeline
            )
            required_text = shot.get("required_text")
            onscreen_text = onscreen_text_for_capacity(required_text)

            linguistic_chars = max(
                _content_chars(dialogue_text + narration_text),
                _content_chars(timeline_text),
            ) + _content_chars(onscreen_text)
            text_min_s = (
                linguistic_chars
                * float(config.VIDEO_DURATION_MIN_S)
                / float(config.SPOKEN_CHARS_PER_5_SECONDS)
            )
            timeline_min_s = max(
                (
                    float(item.get("end_s") or 0)
                    for item in timeline
                ),
                default=0.0,
            )
            spoken_min_s = max(text_min_s, timeline_min_s)
            budget = deepcopy(planned_budget)
            budget["spoken_and_text_s"] = max(
                float(budget.get("spoken_and_text_s") or 0),
                spoken_min_s,
            )
            if shot.get("capacity_budget") != budget:
                changes.append({
                    "field": "shot.capacity_budget",
                    "from": shot.get("capacity_budget"),
                    "to": budget,
                    "reason": "derived_spoken_capacity",
                })
                shot["capacity_budget"] = budget
            required_duration = int(math.ceil(sum(
                float(budget.get(field) or 0)
                for field in (
                    "action_phase_s",
                    "spoken_and_text_s",
                    "attention_switch_s",
                    "inference_processing_s",
                    "reaction_registration_s",
                    "spatial_reorientation_s",
                    "entry_exit_settle_s",
                    "other_s",
                )
            )))
            current_duration = int(
                shot.get("duration_s")
                or config.VIDEO_DURATION_MIN_S
            )
            if (
                current_duration < required_duration
                <= config.VIDEO_DURATION_MAX_S
            ):
                changes.append({
                    "field": "shot.duration_s",
                    "from": current_duration,
                    "to": required_duration,
                    "reason": "derived_joint_capacity",
                })
                shot["duration_s"] = required_duration

    return normalized, changes


def normalize_storyboard_outline_candidate(
    obj: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize unambiguous outline serialization without weakening its schema."""
    normalized = dict(obj)
    changes: list[dict[str, Any]] = []
    raw_shots = normalized.get("shots")
    if not isinstance(raw_shots, list):
        return normalized, changes
    shots: list[Any] = []
    for index, raw_shot in enumerate(raw_shots):
        if not isinstance(raw_shot, dict):
            shots.append(raw_shot)
            continue
        shot = dict(raw_shot)
        for field_name, field_info in StoryboardOutlineShot.model_fields.items():
            if field_name not in shot or shot[field_name] is not None:
                continue
            default = field_info.get_default(call_default_factory=True)
            if not isinstance(default, (str, list, dict)):
                continue
            shot[field_name] = deepcopy(default)
            changes.append({
                "field": f"shots.{index}.{field_name}",
                "from": None,
                "to": deepcopy(default),
                "reason": "nullable_outline_field_normalization",
            })
        covers = shot.get("covers")
        if isinstance(covers, list) and all(
            isinstance(item, str) for item in covers
        ):
            merged = "；".join(
                item.strip() for item in covers if item.strip()
            )
            shot["covers"] = merged
            changes.append({
                "field": f"shots.{index}.covers",
                "from": covers,
                "to": merged,
                "reason": "join_string_list",
            })
        shots.append(shot)
    normalized["shots"] = shots
    return normalized, changes


def _render_error_history(
    error_history: list[list[str]],
    *,
    latest_keep: int = 12,
) -> str:
    """渲染历次输出的问题记录（让模型看到自己反复犯的错）。
    与上一轮完全相同的轮次折叠成一行，避免把同样的错误抄 7 遍、把 prompt 撑爆。"""
    blocks: list[str] = []
    for i, errs in enumerate(error_history):
        if i > 0 and errs == error_history[i - 1]:
            blocks.append(f"【第 {i + 1} 次输出】问题与上一次完全相同（未改进）")
            continue
        if i == len(error_history) - 1:
            keep = latest_keep
        elif i == len(error_history) - 2:
            keep = min(12, latest_keep)
        else:
            keep = 5
        lines = [f"- {e}" for e in errs[:keep]]
        if len(errs) > keep:
            lines.append(f"- ……（另有 {len(errs) - keep} 条同轮问题从略）")
        blocks.append(f"【第 {i + 1} 次输出的问题】\n" + "\n".join(lines))
    return "\n".join(blocks)


def _preserve_omitted_storyboard_repair_fields(
    previous_raw: str,
    repair_raw: str,
) -> tuple[str, list[str]]:
    """Apply a full-object repair as a patch for fields the model omitted."""
    try:
        previous = extract_json(previous_raw)
        repaired = extract_json(repair_raw)
    except ValueError:
        return repair_raw, []
    if not isinstance(previous, dict) or not isinstance(repaired, dict):
        return repair_raw, []
    previous_shot = previous.get("shot")
    repaired_shot = repaired.get("shot")
    if not isinstance(previous_shot, dict) or not isinstance(repaired_shot, dict):
        return repair_raw, []

    merged = deepcopy(repaired)
    merged_shot = merged["shot"]
    preserved: list[str] = []
    for key, value in previous_shot.items():
        if key not in merged_shot:
            merged_shot[key] = deepcopy(value)
            preserved.append(f"shot.{key}")
    for key in ("episode_no", "is_final"):
        if key not in merged and key in previous:
            merged[key] = deepcopy(previous[key])
            preserved.append(key)
    if not preserved:
        return repair_raw, []
    return (
        json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
        preserved,
    )


async def _run_with_agent_loop(
    stage: str,
    stage_key: str,
    user_prompt: str,
    model_cls: type[BaseModel],
    business_validate: Callable[[BaseModel], list[str | Issue]],
    *,
    loop: AgentLoop,
    temperature: float = 0.7,
    max_tokens: int = 8192,
    repair_user_prompt_limit: int | None = 3000,
    repair_candidate_limit: int | None = 6000,
    repair_context: str | None = None,
    repair_output_contract: str | None = None,
    prefill: dict | None = None,
    storyboard_candidate_context: dict[str, Any] | None = None,
    semantic_attempt_id: str | None = None,
) -> BaseModel:
    """Phase 2 loop adapter: structured issues, bounded repair and persisted iterations."""
    base_call_meta = {
        "stage": stage,
        "stage_key": stage_key,
        "initiator_label": stage,
        "initiator_scope": "agent_loop",
        "contract_version": loop.contract.version,
        "expected_json": True,
        **(
            {
                "generation_contract": IR_VERSION,
                "published_output_contract": "EpisodeScreenplay@4.0.0",
                "deterministic_compiler": "app.screenplay_ir.compile_screenplay_ir",
                "compiler_version": IR_COMPILER_VERSION,
                "prompt_version": loop.prompt_version,
            }
            if model_cls is ScreenplayGenerationIR
            else {}
        ),
    }
    iteration_state = {"number": 0}

    async def producer(
        iteration_no: int,
        previous_raw: str | None,
        latest_issues,
        issue_history,
    ) -> str:
        iteration_state["number"] = iteration_no
        semantic_call_meta: dict[str, Any] = {}
        if semantic_attempt_id:
            digest = hashlib.sha256(
                f"{semantic_attempt_id}:inner:{iteration_no}".encode("utf-8")
            ).hexdigest()[:32]
            semantic_call_meta = {
                "semantic_attempt_id": semantic_attempt_id,
                "operation_id": f"op_sem_{digest}",
            }
        if iteration_no == 1:
            return await model_gateway.chat(
                [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": user_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                call_meta={
                    **base_call_meta,
                    "call_role": "stage_generate",
                    "call_role_label": "主生成",
                    "repair_round": 0,
                    **semantic_call_meta,
                    # 若相同幂等 operation 已从供应商成功返回、但本地状态机在落库前
                    # 发生恢复竞态，直接复用已记录响应，禁止再次付费生成。
                    "reuse_successful_operation": True,
                },
            )
        error_history = [[issue.message for issue in issues] for issues in issue_history]
        repair_index = iteration_no - 1
        repair_temp = 0.2 if repair_index < 3 else min(0.2 + 0.15 * (repair_index - 2), 0.8)
        emphasis = (
            ""
            if repair_index < 3
            else (
                f"\n\n【第 {repair_index} 次修复】历史问题已多次未解决。"
                "必须逐条定向修改，且不得引入新的合同违规。"
            )
        )
        if repair_context is not None:
            original_task = repair_context
        else:
            original_task = (
                user_prompt
                if repair_user_prompt_limit is None
                else user_prompt[:repair_user_prompt_limit]
            )
        previous_candidate = previous_raw or ""
        if repair_candidate_limit is not None:
            previous_candidate = previous_candidate[:repair_candidate_limit]
        repair_prompt = (
            "你此前的输出未通过校验。以下问题均为结构化硬门禁，不是泛泛建议：\n"
            + _render_error_history(
                error_history,
                latest_keep=48 if loop.policy.repair_all_blockers else 12,
            )
            + emphasis
            + "\n\n只修复上述问题，然后重新输出完整 JSON（不要解释，不要 Markdown）。"
            + "\n\n原任务要求：\n"
            + original_task
            + "\n\n最近一次候选：\n"
            + previous_candidate
            + (
                "\n\n本轮输出合同（最高优先级）：\n" + repair_output_contract
                if repair_output_contract
                else ""
            )
        )
        repaired_raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": repair_prompt}],
            temperature=repair_temp,
            max_tokens=max_tokens,
            call_meta={
                **base_call_meta,
                "call_role": "stage_repair",
                "call_role_label": "定向修复",
                "repair_round": repair_index,
                **semantic_call_meta,
                "latest_issue_codes": [issue.code for issue in latest_issues[:10]],
                "latest_errors": [issue.message for issue in latest_issues[:10]],
            },
        )
        if model_cls is StoryboardShotDraft and previous_raw:
            repaired_raw, preserved_fields = (
                _preserve_omitted_storyboard_repair_fields(
                    previous_raw,
                    repaired_raw,
                )
            )
            if preserved_fields:
                log_provider_call(
                    "storyboard_repair_field_preservation",
                    config.MODEL_TEXT,
                    "MERGED",
                    None,
                    0,
                    meta={
                        "stage": stage,
                        "iteration_no": iteration_no,
                        "preserved_fields": preserved_fields,
                        "reason": "repair_response_omitted_previous_field",
                    },
                )
        return repaired_raw

    def evaluator(raw: str):
        if model_cls is NarrativeBlueprint:
            raw = normalize_blueprint_raw_json(raw)
        try:
            obj = extract_json(
                raw,
                repair_unescaped_inner_quotes=model_cls in {
                    EpisodeScreenplay,
                    NarrativeBlueprint,
                },
                repair_singleton_string_object_fields=(
                    ("attention_memory_assumptions",)
                    if model_cls is EpisodeScreenplay
                    else ()
                ),
            )
        except ValueError as exc:
            obj = (
                recover_complete_screenplay_ir_prefix(raw)
                if model_cls is ScreenplayGenerationIR
                else (
                    recover_complete_blueprint_prefix(raw)
                    if model_cls is NarrativeBlueprint
                    else None
                )
            )
            if obj is None:
                messages = [str(exc)]
                return None, issues_from_messages(
                    messages,
                    subject=f"{loop.scope_type}:{loop.scope_id}",
                    category="structural",
                )
        if model_cls is ScreenplayGenerationIR and isinstance(obj, dict):
            obj, normalizations = normalize_screenplay_ir_payload(obj)
            if normalizations:
                log_provider_call(
                    "screenplay_ir_candidate_normalization",
                    config.MODEL_TEXT,
                    "NORMALIZED",
                    None,
                    0,
                    meta={
                        "episode_id": loop.scope_id,
                        "stage": stage,
                        "generation_contract": IR_VERSION,
                        "compiler_version": IR_COMPILER_VERSION,
                        "changes": normalizations,
                    },
                )
        if model_cls is EpisodeScreenplay:
            obj, normalized_paths = normalize_screenplay_json_shape(obj)
            if normalized_paths:
                log_provider_call(
                    "screenplay_candidate_normalization", config.MODEL_TEXT,
                    "NORMALIZED", None, 0,
                    meta={
                        "episode_id": loop.scope_id,
                        "stage": stage,
                        "change": "normalize_screenplay_candidate_shape",
                        "normalized_paths": normalized_paths,
                    },
                )
        if model_cls is StoryboardOutline and isinstance(obj, dict):
            obj, normalizations = normalize_storyboard_outline_candidate(obj)
            if normalizations:
                log_provider_call(
                    "storyboard_outline_candidate_normalization",
                    config.MODEL_TEXT,
                    "NORMALIZED",
                    None,
                    0,
                    meta={
                        "episode_id": loop.scope_id,
                        "stage": stage,
                        "changes": normalizations,
                    },
                )
        if model_cls is StoryboardShotDraft and storyboard_candidate_context is not None:
            obj, normalizations = normalize_storyboard_shot_candidate(
                obj,
                episode_no=int(storyboard_candidate_context["episode_no"]),
                shot_no=int(storyboard_candidate_context["shot_no"]),
                outline_story_event_id=str(
                    storyboard_candidate_context.get("outline_story_event_id") or ""
                ),
                legacy_story_event_ids=storyboard_candidate_context.get(
                    "legacy_story_event_ids"
                ),
                outline_narrative_task=storyboard_candidate_context.get(
                    "outline_narrative_task"
                ),
                previous_scene_name=str(
                    storyboard_candidate_context.get(
                        "previous_scene_name"
                    ) or ""
                ),
                previous_scene_time=str(
                    storyboard_candidate_context.get(
                        "previous_scene_time"
                    ) or ""
                ),
                previous_last_frame_desc=str(
                    storyboard_candidate_context.get(
                        "previous_last_frame_desc"
                    ) or ""
                ),
                preserve_director_camera=bool(
                    storyboard_candidate_context.get(
                        "preserve_director_camera"
                    )
                ) or iteration_state["number"] > 1,
            )
            if normalizations:
                log_provider_call(
                    "storyboard_candidate_normalization", config.MODEL_TEXT,
                    "NORMALIZED", None, 0,
                    meta={**storyboard_candidate_context, "changes": normalizations},
                )
        if prefill and isinstance(obj, dict):
            obj.update(prefill)
        if (
            model_cls is StoryboardShotDraft
            and isinstance(obj, dict)
            and "shot" not in obj
            and isinstance(obj.get("shots"), list)
        ):
            messages = [
                "字段 shot：逐镜合同只允许单数 shot 对象，禁止 shots 数组；"
                f"当前一次输出了 {len(obj['shots'])} 个镜头。只保留当前镜，后续内容留给下一轮生成"
            ]
            instance = None
        else:
            instance, messages = schema_errors(model_cls, obj)
        if instance is not None and model_cls is Bible:
            from app.refs import production_appearance_anchor

            normalizations = []
            for character in instance.characters:
                original = character.appearance_canonical
                normalized = production_appearance_anchor(original)
                if normalized != original:
                    character.appearance_canonical = normalized
                    normalizations.append({
                        "character": character.name,
                        "from": original,
                        "to": normalized,
                    })
            if normalizations:
                log_provider_call(
                    "character_bible_candidate_normalization",
                    config.MODEL_TEXT,
                    "NORMALIZED",
                    None,
                    0,
                    meta={
                        "project_id": loop.scope_id,
                        "stage": stage,
                        "changes": normalizations,
                    },
                )
        if instance is not None:
            messages = business_validate(instance)
        typed_issues = [item for item in messages if isinstance(item, Issue)]
        prose_messages = [str(item) for item in messages if not isinstance(item, Issue)]
        return (
            instance,
            [
                *issues_from_messages(
                    prose_messages,
                    subject=f"{loop.scope_type}:{loop.scope_id}",
                ),
                *typed_issues,
            ],
        )

    try:
        result = await loop.run(producer, evaluator)
    except AgentLoopFailure as exc:
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "LOOP_FAILED", None, 0,
            meta={
                "stage": stage,
                "iterations": exc.iterations,
                "exit_reason": exc.exit_reason,
                "issue_codes": [issue.code for issue in exc.issues[:10]],
            },
        )
        raise StageError(
            stage,
            [issue.message for issue in exc.issues]
            + [f"Agent Loop 退出：{exc.exit_reason}（{exc.iterations} 轮）"],
            exit_reason=exc.exit_reason,
            iterations=exc.iterations,
            issues=exc.issues,
        ) from exc
    if result.status == "warning":
        object.__setattr__(result.value, "residual_errors", [issue.message for issue in result.issues])
        object.__setattr__(
            result.value, "residual_issues",
            [issue.model_dump(mode="json") for issue in result.issues],
        )
        object.__setattr__(result.value, "disposition", "WARNING")
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "WARNING_CANDIDATE", None, 0,
            meta={
                "stage": stage,
                "iterations": result.iterations,
                "exit_reason": result.exit_reason,
                "artifact_id": result.artifact_id,
                "issue_codes": [issue.code for issue in result.issues[:10]],
            },
        )
    elif result.status == "baseline":
        object.__setattr__(result.value, "residual_errors", [issue.message for issue in result.issues])
        object.__setattr__(
            result.value, "residual_issues",
            [issue.model_dump(mode="json") for issue in result.issues],
        )
        object.__setattr__(result.value, "disposition", "BASELINE")
        object.__setattr__(result.value, "evidence_artifact_id", result.artifact_id)
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "BASELINE_HANDOFF", None, 0,
            meta={
                "stage": stage,
                "iterations": result.iterations,
                "exit_reason": result.exit_reason,
                "artifact_id": result.artifact_id,
                "issue_codes": [issue.code for issue in result.issues[:10]],
                "call_role": "local_patch",
            },
        )
    elif result.status == "needs_replan":
        object.__setattr__(result.value, "residual_errors", [issue.message for issue in result.issues])
        object.__setattr__(
            result.value, "residual_issues",
            [issue.model_dump(mode="json") for issue in result.issues],
        )
        object.__setattr__(result.value, "disposition", "NEEDS_REPLAN")
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "NEEDS_REPLAN", None, 0,
            meta={
                "stage": stage,
                "iterations": result.iterations,
                "exit_reason": result.exit_reason,
                "artifact_id": result.artifact_id,
                "issue_codes": [issue.code for issue in result.issues[:10]],
            },
        )
    else:
        object.__setattr__(result.value, "disposition", "PASS")
    object.__setattr__(result.value, "loop_exit_reason", result.exit_reason)
    object.__setattr__(result.value, "evidence_artifact_id", result.artifact_id)
    return result.value


# ---------- A. 角色圣经 ----------

BIBLE_SOURCE_BUDGET_CHARS = 60000


_BIBLE_TAIL_SAMPLE_MAX = 12      # 后段最多抽样多少章（取其开头，角色多在章首登场）
_BIBLE_TAIL_SLICE_CHARS = 1500   # 每个抽样章节注入的开头字数

BIBLE_HEAD_CHAPTERS = 10         # 首版人物谱必须完整通读的章节数
BIBLE_LOOKAHEAD_CHAPTERS = 10    # 判定「这个角色重不重要」时再往后看的章节数
BIBLE_RECURRING_MIN_HITS = 3     # 在通读窗口内逐字出现多少次才算重要角色
BIBLE_MUST_COVER_MAX = 12        # 必收名单上限，避免把生成预算耗在龙套身上


def _render_bible_source(chapters: list[dict], budget: int = BIBLE_SOURCE_BUDGET_CHARS,
                         *, head_chapters: int | None = None) -> str:
    """为角色圣经渲染源文本：先顺序铺头部（主角通常在前期出场），再在剩余预算里
    跨越全书【抽样后段章节的开头】，让后期才登场的重要角色（如中后段反派）也能进圣经——
    否则分镜阶段引用这些角色会因"不在圣经"而反复返工或被迫漏掉。
    """
    valid = [ch for ch in chapters if (ch.get("content") or "").strip()]
    if not valid:
        return ""

    def _title(ch: dict) -> str:
        return ch.get("title") or f"第{ch.get('idx', '?')}章"

    # 头部顺序铺设：用至多 70% 预算（其余留给后段抽样）。
    head_budget = int(budget * 0.7)
    if head_chapters:
        # 首版人物谱要求「完整读完前 N 章」。按比例切的头部会随章节长度漂移：
        # 长章小说可能读到第三、四章就把头部预算用光，主要配角整体缺席。
        head_budget = min(budget, max(head_budget, sum(
            len(ch["content"].strip()) for ch in valid[:head_chapters]
        )))
    blocks: list[str] = []
    used = 0
    head_count = 0
    for ch in valid:
        remain = head_budget - used
        if remain <= 200:
            break
        content = ch["content"].strip()
        clipped = content[:remain]
        suffix = "……（原文过长已截断）" if len(content) > remain else ""
        blocks.append(f"【{_title(ch)}】\n{clipped}{suffix}")
        used += len(clipped)
        head_count += 1

    # 后段抽样：在头部未覆盖的章节里均匀取样，注入每章开头若干字，覆盖后期登场人物。
    later = valid[head_count:]
    remain_budget = budget - used
    if later and remain_budget > 200:
        sample_n = min(len(later), _BIBLE_TAIL_SAMPLE_MAX, max(1, remain_budget // _BIBLE_TAIL_SLICE_CHARS))
        if sample_n > 0:
            step = len(later) / sample_n
            picked_idx = sorted({min(len(later) - 1, int(i * step)) for i in range(sample_n)})
            for li in picked_idx:
                if remain_budget <= 200:
                    break
                ch = later[li]
                slice_chars = min(_BIBLE_TAIL_SLICE_CHARS, remain_budget)
                content = ch["content"].strip()
                clipped = content[:slice_chars]
                suffix = "……（节选开头，仅供识别后期登场角色）" if len(content) > slice_chars else ""
                blocks.append(f"【{_title(ch)}·节选】\n{clipped}{suffix}")
                remain_budget -= len(clipped)

    return "\n\n".join(blocks)


class _CharacterRollCall(BaseModel):
    """人物点名合同：只要名字，不要描写（描写留给正式的人物谱调用）。"""

    names: list[str] = Field(default_factory=list)


class _BibleSupplement(BaseModel):
    """补录合同：只为「必收名单里还缺的人」补出完整角色条目。"""

    characters: list[Character] = Field(default_factory=list)


async def _recurring_character_names(chapters: list[dict]) -> list[tuple[str, int]]:
    """产出「必收角色名单」：先点名，再按原文逐字出现次数判定谁重要。

    只靠一次生成调用时，模型往往只写开头几段里的人：它既没有全局出现次数，
    也没有动力把后面才反复登场的角色补上。这里把判断拆成两步——
    模型只负责在前 BIBLE_HEAD_CHAPTERS 章里点出候选名字（它擅长这件事），
    后端再在「前 N 章 + 往后 BIBLE_LOOKAHEAD_CHAPTERS 章」里逐字统计出现次数
    （这件事必须精确，不能交给模型），出现多次的才进必收名单。
    统计窗口失败时返回空名单，绝不阻断人物谱本身。
    """
    valid = [ch for ch in chapters if (ch.get("content") or "").strip()]
    if not valid:
        return []
    head = valid[:BIBLE_HEAD_CHAPTERS]
    head_text = _render_bible_source(head, head_chapters=BIBLE_HEAD_CHAPTERS)
    if not head_text.strip():
        return []
    prompt = f"""任务：从下面的小说正文里点出【出场人物的名字】，只点名，不要写任何描写。

要求：
1. 只输出原文中稳定出现的人名或固定称谓（如"孟浩""王有材""许师姐"）。
2. 不要输出"少年""女子""老者""两人"这类泛称，也不要输出宗门名、地名、法宝名。
3. 同一个人只输出一次，用原文里最常见的那个写法，不要把同一人的多个称呼都列出来。
4. 名字必须逐字出现在正文中，不得改写、简称或补全。
5. 宁多勿漏：戏份很少的人也可以点出来，后端会按出现次数再筛一遍。

小说正文：
{head_text}

输出 JSON Schema：
{{"names": [str]}}"""
    try:
        raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX},
             {"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=2048,
            call_meta={
                "stage": "人物点名",
                "stage_key": "character_roll_call",
                "call_role": "stage_generate",
                "call_role_label": "人物点名",
                "expected_json": True,
            },
        )
        names = _CharacterRollCall.model_validate(extract_json(raw)).names
    except Exception as exc:  # noqa: BLE001 - 点名只是覆盖度提示，失败不阻断人物谱
        log_provider_call(
            "character_roll_call", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"error": str(exc)[:300]},
        )
        return []
    head_raw = "\n".join(ch["content"] for ch in head)
    window_raw = "\n".join(
        ch["content"]
        for ch in valid[:BIBLE_HEAD_CHAPTERS + BIBLE_LOOKAHEAD_CHAPTERS]
    )
    ranked: list[tuple[str, int]] = []
    seen: set[str] = set()
    for value in names:
        name = str(value or "").strip()
        # 必须逐字出现在通读章节里：模型改写或补全出来的名字不进必收名单。
        if not 2 <= len(name) <= 8 or name in seen or name not in head_raw:
            continue
        seen.add(name)
        hits = window_raw.count(name)
        if hits >= BIBLE_RECURRING_MIN_HITS:
            ranked.append((name, hits))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:BIBLE_MUST_COVER_MAX]


def _bible_covers_name(bible: Bible, name: str) -> bool:
    """必收名字是否已经在人物谱里（允许模型用更完整的正式姓名收录同一人）。"""
    return any(
        character.name == name
        or (name and character.name and (name in character.name or character.name in name))
        for character in bible.characters
    )


async def _supplement_bible_characters(bible: Bible, missing: list[tuple[str, int]],
                                       chapters_text: str, *,
                                       visual_style_prompt: str | None = None) -> list[str]:
    """为必收名单里仍然缺席的角色补一次条目；失败或不合格就放弃该角色。

    这一步刻意放在 AgentLoop 之外：人物谱缺角色是质量问题，不该把整个项目
    卡在 bible_status=warning 上（那会连带停掉定妆照与场景库）。
    """
    from app.refs import (
        PRODUCTION_APPEARANCE_MAX_CHARS,
        PRODUCTION_APPEARANCE_MIN_CHARS,
    )

    wanted = "、".join(f"{name}（原文出现 {hits} 次）" for name, hits in missing)
    style = visual_style_prompt or bible.world.visual_style_canonical
    prompt = f"""任务：为下列【已确认重要但人物谱漏收】的角色补出角色条目，用于 AI 视频生成的一致性控制。

必须补录的角色（name 必须逐字照抄，不得改写或合并）：
{wanted}

已收录角色（不要重复输出）：{'、'.join(c.name for c in bible.characters) or '无'}
全片统一画风（角色外观必须服从）：{style}

要求：
1. 只输出上面「必须补录」的角色，也不要多输出别人。唯一例外：其中某个名字如果其实不是人物（是宗门、地名或法宝名），跳过它，不要硬编成角色。
2. appearance_canonical 是固定外观锚点串：{PRODUCTION_APPEARANCE_MIN_CHARS}~{PRODUCTION_APPEARANCE_MAX_CHARS} 字，必须包含 性别年龄感/发型发色/服装款式与颜色/1 个标志性特征。只写常规完整着装、中性站姿下可直接看见并能跨镜稳定复现的静态形态；不写性格、情绪、眼神行为，不得写裸体或私密身体部位。原著未描写处按题材合理补全并保持内部一致。
3. role 取"主角|重要配角|反派"之一。
4. speech_style 15~30 字，描述句长习惯/口头禅/敬语习惯。
5. relationships.to 只能指向【已收录角色或本次补录角色】的 name；无法确定就留空数组。

小说文本：
{chapters_text}

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}]}}]}}"""
    try:
        raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX},
             {"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=8192,
            call_meta={
                "stage": "人物谱补录",
                "stage_key": "character_bible_supplement",
                "call_role": "stage_generate",
                "call_role_label": "人物谱补录",
                "expected_json": True,
            },
        )
        drafted = _BibleSupplement.model_validate(extract_json(raw)).characters
    except Exception as exc:  # noqa: BLE001 - 补录失败保留已有人物谱，不阻断下游
        log_provider_call(
            "character_bible_supplement", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"error": str(exc)[:300]},
        )
        return []
    wanted_names = {name for name, _ in missing}
    added: list[str] = []
    for character in drafted:
        name = (character.name or "").strip()
        if (
            name not in wanted_names
            or _bible_covers_name(bible, name)
            or not PRODUCTION_APPEARANCE_MIN_CHARS
            <= len(character.appearance_canonical)
            <= PRODUCTION_APPEARANCE_MAX_CHARS
        ):
            continue
        character.name = name
        character.ref_image_path = None
        character.portrait_prompt_override = None
        bible.characters.append(character)
        added.append(name)
    # 关系只能指向最终名单里的人，否则 validate_bible 会因「关系指向未知角色」退回。
    names = {c.name for c in bible.characters}
    for character in bible.characters:
        character.relationships = [
            relation for relation in character.relationships if relation.to in names
        ]
    return added


# ---------- A1. 人物别名回填（层一，见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1） ----------

ALIAS_BACKFILL_SOURCE_BUDGET_CHARS = 150000  # 一次性全书扫描，预算高于常规人物谱生成的 60000


def _chapters_by_idx(chapters: list[dict]) -> dict[int, str]:
    """按章节序号建立原文查找表，供别名证据的逐字核验使用（未经预算截断的完整正文）。"""
    result: dict[int, str] = {}
    for chapter in chapters:
        content = (chapter.get("content") or "").strip()
        if not content:
            continue
        try:
            idx = int(chapter.get("idx"))
        except (TypeError, ValueError):
            continue
        result[idx] = content
    return result


# 逐字比对时可选脱掉的成对引号：ASCII 直引号与全角引号都要覆盖，因为模型申报
# evidence_quote 时有时会给原本没有引号包裹（或已用另一种引号包裹）的原文自行加上
# 一层引号，导致本来逐字正确的引句因为多出的引号字符核验不过。只有首尾字符恰好配对
# 才脱这一层，不配对的原样保留——不能把原文本身的一部分误当引号脱掉。
_PAIRED_QUOTE_MARKS = (('"', '"'), ('“', '”'), ("'", "'"), ('‘', '’'))


def _quote_comparison_variants(quote: str) -> list[str]:
    """逐字比对用的候选引句形式：原始引句本身，以及（若首尾恰好是成对的引号字符）
    脱掉这对引号后的内文。只脱这一侧（模型申报的引句），原文一侧不做任何改写；
    两种形式中任一比对命中即算通过。"""
    variants = [quote]
    if len(quote) >= 2:
        for open_mark, close_mark in _PAIRED_QUOTE_MARKS:
            if quote[0] == open_mark and quote[-1] == close_mark:
                inner = quote[1:-1]
                if inner:
                    variants.append(inner)
                break
    return variants


def _alias_declaration_verified(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    text: str,
    evidence_chapter_index: int,
    evidence_quote: str,
) -> bool:
    """别名申报的代码核验：结构性判据，不对任何具体称谓做特判（禁止黑白名单式修复）。

    三个条件必须同时成立，任一不满足就不登记（不确定不登记是安全默认）：
    1. evidence_quote 是 evidence_chapter_index 对应章节原文的逐字子串；
    2. text（申报的别名本身）是 evidence_quote 的子串——证据必须真的提到这个别名，
       不能是一句不相干的话；
    3. 该章节原文里还能找到 anchor_texts（角色规范名或已确认的其它别名）中的至少一项——
       证明这条别名与该角色存在共现依据，不是张冠李戴。

    条件 1、2 都按 `_quote_comparison_variants` 产出的候选引句形式判断（原始引句 /
    脱掉一层配对引号后的内文），同一候选形式需要同时满足两个条件才算命中，避免
    "脱引号让子串关系对不上"这种格式噪音误判为证据不足。
    """
    text = (text or "").strip()
    quote = (evidence_quote or "").strip()
    if not text or not quote:
        return False
    chapter_text = chapters_by_idx.get(evidence_chapter_index, "")
    if not chapter_text:
        return False
    if not any(anchor and anchor in chapter_text for anchor in anchor_texts):
        return False
    return any(
        text in candidate and candidate in chapter_text
        for candidate in _quote_comparison_variants(quote)
    )


# ---------- A1a. 桥接章确定性检索（分工修复：模型申报语义，代码检索证据所在地） ----------
#
# 真实回归发现的分工错误：模型申报「李富贵→小胖子」「许清→许师姐」这两条语义假设
# 完全正确，但 evidence_chapter_index 报错了章——它引用的章节里没有角色正式姓名，
# 共现闸（_alias_declaration_verified 条件 3）必然拒绝。根因是让模型去做"记住桥接章
# 在哪"这件事：这是确定性检索，代码扫全部章节又快又准，模型单次扫 15 万字反而漏。
# 参照 app/production/prep_pack.py 的裁决庭范式（_prep_pack_true_name_dossier /
# _prep_pack_true_name_verdict / _prep_pack_pin_dossier_quote：代码检索卷宗 → 模型
# 裁决 → 代码钉证），但这里比裁决庭少一步：裁决庭要解决的是"称谓 X 是否等于人名 Y"
# 这个开放语义判断，需要模型独立裁决；这里模型在申报 character_name+text 这对时，
# 已经做完了"这是不是同一个人/这是不是这个人的别名"的语义判断——剩下的只是纯字符串
# 问题："全书哪一章能同时证明这对申报"，不需要再发一次模型调用去问一个已经有答案的
# 语义问题，代码直接检索、钉证即可（见 _find_alias_bridge_chapter / _alias_bridge_quote）。
# 找不到桥接章 → 维持拒绝（不确定不登记，安全默认不因为多了这条兜底而放松）。

_ALIAS_BRIDGE_QUOTE_MAX_CHARS = 200  # 引句长度上限：够定位上下文，不整段搬运


def _alias_bridge_quote(chapter_text: str, text: str) -> str | None:
    """从桥接章原文里确定性截取包含 text 的引句：复用 `index_source_segments`
    做自然段/句级切分（与裁决庭卷宗检索同一工具，已处理引号跨段等边界情况），
    取第一个包含 text 的分段——分段本身就是原文的逐字切片，天然满足
    `_alias_declaration_verified` 条件 1（逐字子串）与条件 2（text 是引句子串）。
    调用方已确认 text 在 chapter_text 里，理论上必有命中；找不到时返回 None
    交由调用方兜底拒绝，不强行拼一句可能不含 text 的引句。"""
    for segment in index_source_segments(chapter_text, max_chars=_ALIAS_BRIDGE_QUOTE_MAX_CHARS):
        if text in segment.text:
            return segment.text
    return None


def _find_alias_bridge_chapter(
    chapters_by_idx: dict[int, str], anchor_texts: set[str], text: str,
) -> tuple[int, str] | None:
    """桥接章确定性检索：扫描全部章节（不受 ALIAS_BACKFILL_SOURCE_BUDGET_CHARS 预算
    限制——那个预算只约束喂给模型的上下文长度，不该约束代码自己的确定性检索范围），
    按章节序号升序找第一个同时包含 text（申报的别名文本）与 anchor_texts（角色规范名
    或已确认别名）中至少一项的章节。"升序取第一个命中"是多桥接章命中时的确定性选择
    规则：同一输入任何时候重跑结果一致，可复现、可审计（与裁决庭卷宗采样同一原则）；
    选最早出现的桥接章而非任意一个，也与"最早共现即已构成充分证据"的直觉一致。
    全书都没有这样的章节 → 返回 None，调用方维持拒绝。"""
    for idx in sorted(chapters_by_idx):
        content = chapters_by_idx[idx]
        if text not in content:
            continue
        if not any(anchor and anchor in content for anchor in anchor_texts):
            continue
        quote = _alias_bridge_quote(content, text)
        if quote:
            return idx, quote
    return None


# ---------- B. 章级认知卡（确定性组装，零模型调用，见 docs/CHARACTER_COGNITION_LAYER_DESIGN.md
# §4.2） ----------
#
# 三类事实的时间语义完全不同（设计文档 §3），认知卡把这条语义结构化摆出来，供下面 A1b
# 裁决闸（`_alias_verdict_call`）当候选判别的背景参考。本身不发起任何模型调用、不做
# 任何语义判断，只是纯字符串/区间运算：
# - 身份事实（`Character.aliases`）恒真，不受章节号 N 影响，只用于"在场判定"（角色
#   规范名或已确认别名逐字命中本章原文即算在场，与 `_alias_verdict_candidates`
#   同一判据，零语义、不针对具体称谓特判）；
# - 状态事实（`Character.affiliations`/`relations`）带有效区间，按"截至第 N 章"过滤
#   （`valid_from_chapter <= N` 且 `valid_to_chapter` 为空或 `>= N`），避免拿后期状态
#   描写当下（§3.2），区间重叠时同一归属对象/关系对象只取最近生效的一条，与
#   `character_portraits` 表 `ORDER BY ep_start DESC LIMIT 1` 的既有惯例同构
#   （`app/portraits.py` `portrait_for_episode`）；
# - 前瞻信号（`forward_appearance_hits`）复用既有 `CHARACTER_IMPORTANCE_FORWARD_
#   CHAPTERS` 前瞻窗口常量（`app/portraits.py`），统计方式与 `_recurring_character_
#   names` 的 `window_raw.count(name)` 同一惯例（§3.3），不新造常量、不重新发明统计
#   口径。本次只组装该字段供未来消费点使用（§9 P1 第 7 项——未具名角色建卡触发点，
#   本次不实现触发逻辑本身）；当前唯一接入的裁决闸注入（下方 A1b）不读取这个字段，
#   只用 affiliations_as_of/relations_as_of。
#
# 同一 (bible 快照, chapter_idx, forward_window_chapters) 输入任何时候重建结果逐字节
# 相同（§11 判据 2）：不依赖模型调用，`bible.characters`/`character.affiliations`/
# `character.relations` 都是列表，遍历顺序本身就是确定的。

CHAPTER_COGNITION_CARD_MAX_CHARACTERS = 8  # 单卡最多收录角色数：裁决闸候选集通常就是该
    # 章出场的人物谱角色，规模个位数（`_alias_verdict_candidates`）；8 留出冗余，同时
    # 防止人物扎堆的章节把提示词拖长——超限时按调用方给定范围内 bible.characters 的
    # 原始顺序截断，不做二次排序，保证可复现。
CHAPTER_COGNITION_FACTS_MAX_PER_KIND = 3  # 每个角色的 affiliations_as_of / relations_as_of
    # 各自最多展示的条数：每条状态事实入库前都要单独过一遍候选判别裁决（§4.1），门槛
    # 高，正常不会在单角色名下堆积大量条目；3 条留有冗余又不至于让单个角色的背景摘要
    # 过长——超限按角色 affiliations/relations 的原始登记顺序（区间去重取最新一条后）
    # 截断，不做二次排序。
CHAPTER_COGNITION_SUMMARY_MAX_CHARS = 60  # 单条归属/关系摘要（org/to + relation_kind
    # 拼装后）最长字符数：org/relation_kind 是模型自由文本，理论上无长度上限，必须有
    # 硬顶防止个别异常长文本拖长提示词；60 字足够容纳"血妖宗（效忠），第X章证据"这类
    # 正常长度还留有余量。


class ChapterCognitionEntry(BaseModel):
    """认知卡单个角色条目（§4.2）：全部字段均为确定性拼装的只读展示值，不是新的存储
    字段——真实数据仍在 `Character.aliases`/`affiliations`/`relations`，这里只是按
    章节号 N 过滤/统计后的摘要视图。"""

    name: str                                              # 人物谱规范名
    matched_surface_forms: list[str] = Field(default_factory=list)  # 命中的称谓：规范名
    # 或已确认别名，逐字子串命中，零语义、不针对具体称谓特判（复用
    # `_alias_verdict_candidates` 的判据模式）
    affiliations_as_of: list[str] = Field(default_factory=list)  # 截至本章生效的归属摘要
    # （org + relation_kind 拼装只读字符串，供提示词展示，不是新的存储字段）
    relations_as_of: list[str] = Field(default_factory=list)  # 截至本章生效的关系摘要，
    # 拼装方式同上
    forward_appearance_hits: int = 0  # 前瞻窗口内该角色规范名/别名的逐字出现次数


class ChapterCognitionCard(BaseModel):
    """章级认知卡（§4.2）：同一 (bible 快照, chapter_idx, forward_window_chapters) 输入
    任何时候重建结果逐字节相同（§11 判据 2，机械回归测试）。"""

    chapter_idx: int                            # 本卡对应的原著章节序号（进度锚点）
    forward_window_chapters: int                # 本次使用的前瞻窗口大小 K，记账供审计复现
    present_characters: list[ChapterCognitionEntry] = Field(default_factory=list)


def _status_facts_as_of_chapter(
    entries: list[Any], chapter_idx: int, *, group_key: Callable[[Any], str],
) -> list[Any]:
    """状态事实"截至第 N 章"区间过滤 + 同对象最新一条优先（§4.2 point 2，与
    `character_portraits` 表 `ORDER BY ep_start DESC LIMIT 1` 的既有惯例同构，见
    `app/portraits.py` `portrait_for_episode`）：先筛出 `valid_from_chapter <=
    chapter_idx` 且（`valid_to_chapter` 为空或 `>= chapter_idx`）的条目；`group_key`
    相同的多条（同一归属对象 org、或同一关系对象 to）若区间重叠都满足，只保留
    `valid_from_chapter` 最大（最近生效）的一条——同一角色可以同时对不同 org/to 各有
    一条独立生效的事实，只有指向同一对象的多条才互相竞争。返回顺序按 `group_key`
    首次出现顺序（即 `entries` 原始顺序）排列，保证同一输入任何时候重建结果逐字节
    相同。"""
    valid = [
        item for item in entries
        if item.valid_from_chapter <= chapter_idx
        and (item.valid_to_chapter is None or item.valid_to_chapter >= chapter_idx)
    ]
    best: dict[str, Any] = {}
    order: list[str] = []
    for item in valid:
        key = group_key(item)
        if key not in best:
            order.append(key)
            best[key] = item
        elif item.valid_from_chapter > best[key].valid_from_chapter:
            best[key] = item
    return [best[key] for key in order]


def _cognition_affiliation_summary(item: CharacterAffiliation) -> str:
    """归属摘要拼装：纯字符串运算，不做任何语义判断。"""
    label = item.org + (f"（{item.relation_kind}）" if item.relation_kind else "")
    text = f"{label}，第{item.evidence_chapter_index}章证据"
    return text[:CHAPTER_COGNITION_SUMMARY_MAX_CHARS]


def _cognition_relation_summary(item: CharacterRelation) -> str:
    """关系摘要拼装：与 `_cognition_affiliation_summary` 同构，`org` 换成 `to`。"""
    label = item.to + (f"（{item.relation_kind}）" if item.relation_kind else "")
    text = f"{label}，第{item.evidence_chapter_index}章证据"
    return text[:CHAPTER_COGNITION_SUMMARY_MAX_CHARS]


def build_chapter_cognition_card(
    bible: Bible,
    chapters_by_idx: dict[int, str],
    chapter_idx: int,
    *,
    character_names: list[str] | None = None,
    forward_window_chapters: int | None = None,
) -> ChapterCognitionCard:
    """章级认知卡组装（§4.2）：代码零语义，纯字符串/区间运算，不发起模型调用。
    `character_names` 是需要纳入的角色范围，由调用方给定（本文件唯一调用点
    `_alias_evidence_resolution` 传入的是裁决闸已经结构性算出的候选集
    `_alias_verdict_candidates`）；缺省（`None`）时对 `bible.characters` 全量扫描
    （§4.2 point 1 "遍历 bible.characters"）。`Character.affiliations`/`relations`
    当前项目尚未真实回填过（均为空列表）时，本函数优雅退化为只含
    `matched_surface_forms`（无归属/关系摘要）的条目，不报错、不拒绝工作——见 §12
    "回滚"对这一退化路径的要求。同一 `(bible 快照, chapter_idx, forward_window_
    chapters)` 输入任何时候重建结果逐字节相同（§11 判据 2）。"""
    if forward_window_chapters is None:
        from app.portraits import CHARACTER_IMPORTANCE_FORWARD_CHAPTERS
        forward_window_chapters = CHARACTER_IMPORTANCE_FORWARD_CHAPTERS

    chapter_text = chapters_by_idx.get(chapter_idx, "")
    forward_text = "\n".join(
        chapters_by_idx[idx]
        for idx in range(chapter_idx + 1, chapter_idx + forward_window_chapters + 1)
        if idx in chapters_by_idx
    )
    wanted = set(character_names) if character_names is not None else None

    present: list[tuple[Character, list[str], list[str]]] = []
    for character in bible.characters:
        if wanted is not None and character.name not in wanted:
            continue
        surface_forms = [character.name, *(a.text for a in character.aliases if a.text)]
        matched = [form for form in surface_forms if form and form in chapter_text]
        if matched:  # 在场判定：规范名或已确认别名逐字命中本章原文（§4.2 point 1）
            present.append((character, surface_forms, matched))

    entries: list[ChapterCognitionEntry] = []
    # 确定性截断：按调用方给定范围内 bible.characters 的原始顺序取前
    # CHAPTER_COGNITION_CARD_MAX_CHARACTERS 个在场角色，不做二次排序。
    for character, surface_forms, matched in present[:CHAPTER_COGNITION_CARD_MAX_CHARACTERS]:
        affiliations_as_of = [
            _cognition_affiliation_summary(item)
            for item in _status_facts_as_of_chapter(
                character.affiliations, chapter_idx, group_key=lambda a: a.org,
            )
        ][:CHAPTER_COGNITION_FACTS_MAX_PER_KIND]
        relations_as_of = [
            _cognition_relation_summary(item)
            for item in _status_facts_as_of_chapter(
                character.relations, chapter_idx, group_key=lambda r: r.to,
            )
        ][:CHAPTER_COGNITION_FACTS_MAX_PER_KIND]
        forward_hits = (
            sum(forward_text.count(form) for form in surface_forms if form)
            if forward_text else 0
        )
        entries.append(ChapterCognitionEntry(
            name=character.name,
            matched_surface_forms=matched,
            affiliations_as_of=affiliations_as_of,
            relations_as_of=relations_as_of,
            forward_appearance_hits=forward_hits,
        ))
    return ChapterCognitionCard(
        chapter_idx=chapter_idx,
        forward_window_chapters=forward_window_chapters,
        present_characters=entries,
    )


def _cognition_status_lines(card: ChapterCognitionCard | None) -> list[str]:
    """把认知卡中"有归属或关系摘要"的角色条目渲染成提示词"候选人已知状态"文本块的
    逐行文本（§4.3）：只展示状态事实（affiliations_as_of/relations_as_of），不展示
    `forward_appearance_hits`——前瞻信号服务重要性判断（§3.3），与判别式提问无关，
    §9 P1 第 7 项才会消费它，本函数不注入到裁决闸提示词里。角色没有任何状态事实
    摘要时不出现在结果里；`card` 为 `None`，或全部在场角色都没有状态事实摘要（当前
    真实状态：`backfill_character_status_facts` 尚未真实跑过，`affiliations`/
    `relations` 均为空）时返回空列表，供调用方据此把整段"候选人已知状态"文本块省略，
    不留空标题、不留占位噪声。"""
    if card is None:
        return []
    lines: list[str] = []
    for entry in card.present_characters:
        facts: list[str] = []
        if entry.affiliations_as_of:
            facts.append("归属 " + "、".join(entry.affiliations_as_of))
        if entry.relations_as_of:
            facts.append("关系 " + "、".join(entry.relations_as_of))
        if facts:
            lines.append(f"- {entry.name}：" + "；".join(facts))
    return lines


# ---------- A1b. 裁决闸：桥接章原文独立裁决（补上"同章共现"证明不了"指同一人"的漏洞） ----------
#
# 真实误登记事故：全书别名回填写库后核验发现「孟浩←虎爷爷」（第 3 章）——第 3 章原文
# 里「虎爷爷」明确是欺负孟浩的另一个魁梧大汉，根本不是孟浩本人。根因：`_alias_
# declaration_verified`/`_find_alias_bridge_chapter`（条件 3）只证明"别名文本与角色
# 规范名在同一章出现"，证明不了"这个别名指的就是这个角色"——指代关系是模型在没看到
# 桥接章原文的情况下凭全书记忆断言的（它把这个称谓和另一段记忆搞混了）。且主角类角色
# 几乎每章都出场，共现闸对这类角色的过滤力接近零：随便一个同章出现的称谓，不管是不是
# 主角本人，都能通过共现闸。
#
# 修复：回到项目既有的裁决庭范式（`app/production/prep_pack.py` 的
# `_prep_pack_true_name_dossier` / `_prep_pack_true_name_verdict` /
# `_prep_pack_pin_dossier_quote`：代码检索卷宗 → 独立模型裁决 → 代码钉证），在代码
# 定位到桥接章（或模型自己申报的章节已经通过共现闸）之后，带着该章的真实原文段落
# 再做一次独立裁决调用，问模型"依据这些原文，称谓 X 是否指代角色 Y 本人"，三态回答
# same/different/uncertain，uncertain 与 different 一律拒绝登记（不确定不登记，安全
# 默认）。
#
# 钉证方式：引用卷宗段号，不要求模型逐字复述原文。最初的实现要求模型的
# supporting_quote 逐字（经引号规范化后）命中卷宗某条，线上复核暴露这个钉证方式本身
# 不可靠——"李富贵←小胖子"（第 10 章）与"上官修←上官师叔"两条本该通过的正确别名，
# 分别被 quote_not_pinned 误杀、以及同一输入两次复核给出不同结果，根因是模型转录
# 原文时会跨段拼接、加省略号、微调标点，这些噪音跟"证据是否成立"无关，却被当成了
# 拒绝理由。改为让模型在 verdict 之外只需引用卷宗目录里某一条的段号
# （supporting_segment_index），JSON Schema 用 enum 把候选值限定为本次卷宗实际收录的
# 段号集合（参照 `app/portraits.py` `_current_identity_schema()` 给 `evidence_ref`
# 注入 enum 的写法），钉证退化为一次整数是否落在集合内的结构性判断——模型选中的段落
# 本身就是代码检索出的真实原文，无从编造，也不存在转录误差。supporting_quote 保留为
# 可选的观测字段（写进裁决通过日志，便于人工复核），不再是钉证硬闸的一部分。
#
# 卷宗构造是确定性的：只从已经定位到的那一章取证据（不是整章、也不是全书），取该章
# 里包含别名文本 `text` 和/或角色规范名 `true_name` 的自然段（与
# `_prep_pack_true_name_dossier` 同一检索原则，缩小到已定位的单章）——两者共现的
# 段落、以及只含 `text` 的段落必须收录；只含 `true_name` 的段落按"离最近的别名相关
# 段落有多近"补足剩余预算（不是按章节开头起的文档顺序），因为桥接章里真正点明
# `true_name` 身份的那段，很多时候并不挨着 `text` 出现，而 `true_name` 若是主角，
# 几乎每段都会出现——按文档顺序截断会被开头大段无关独白占满预算，把真正有用的
# `true_name` 段落挤出去（真实回归：project proj_3ac0b627fa46 第 1 章"孟兄"只出现
# 一次，"孟浩"贯穿全章出现三十余次，见 `_alias_verdict_dossier` 的完整说明）。条数
# 与总字数超过上限时按"两者共现段落 / 只含别名段落全部优先、只含真名段落按接近别名
# 段落的程度补足预算"的确定性规则截断——不用随机采样，同一输入任何时候重跑都得到
# 同一份卷宗。

_ALIAS_VERDICT_DOSSIER_MAX_ENTRIES = 12  # 单条别名裁决卷宗最多收录的段落数
_ALIAS_VERDICT_DOSSIER_MAX_CHARS = 6000  # 单条别名裁决卷宗最多收录的总字符数


def _alias_verdict_dossier(
    chapter_idx: int, chapter_text: str, text: str, anchor_texts: set[str],
) -> list[dict[str, Any]]:
    """裁决卷宗检索：零语义，纯字符串包含判断，只从已定位到的这一章本身取证，不整章
    塞给模型。把该章按自然段切分（`index_source_segments`）后分三类：
    - both：同段同时含别名 `text` 与 `anchor_texts`（角色规范名、或本角色已确认的
      其它别名）中至少一项——最直接的证据；
    - text_only：只含 `text`——必须收录，模型至少要看到别名本身被怎么用的；
    - anchor_only：只含 `anchor_texts` 中至少一项、不含 `text`——用来在别名段落之外
      补充"这些已确认的称谓在这一章还出现在哪"，帮模型判断两者是否指同一人。

    为什么要搜整个 `anchor_texts` 而不是只搜角色规范名：真实回归——李富贵的别名
    "胖爷"在桥接章里的连接证据是"（胖爷）……此刻小胖子正蹲在那里"，这段原文压根
    没提"李富贵"三个字，"小胖子"（李富贵已确认的另一条别名）才是真正的桥梁。只搜
    角色规范名会把这段关键证据漏掉，模型看不到任何连接就只能回答 uncertain——与
    `_alias_declaration_verified` 条件 3 的共现闸本就允许"该章节找到角色规范名或
    已确认的其它别名"任一项是同一个道理，裁决闸的证据检索范围不能比共现闸更窄。

    真实回归暴露的另一个坑：`anchor_texts` 里若含主角规范名，几乎每段都会出现，
    如果只按"从章节开头数第几段"这种文档顺序截断，预算会被开头大段无关的独白占满，
    反而把本该收录的 `text` 段落、以及紧挨着 `text` 段落的关键 anchor_only 段落
    挤出预算之外（project proj_3ac0b627fa46 第 1 章："孟兄"只出现一次，"孟浩"出现
    三十余次贯穿全章——若不做优先级区分，裁决闸看到的会是章节开头孟浩独自坐在山顶
    的大段背景描写，反而看不到"孟兄"那句台词紧邻的对话）。

    因此排序规则是确定性的三层优先级：both 全部收录 → text_only 全部收录（这两类
    条数通常很少，一般不会触顶）→ anchor_only 按"离最近的 both/text_only 段落有多
    远"升序补足剩余预算，越靠近别名实际出现的位置越优先，距离相同按文档顺序（下标
    升序）确定性打破平局——这样即使预算有限，模型看到的也是"别名出现处附近这些
    已确认称谓怎么被提及"，而不是章节任意位置的无关段落。条数与总字数超过上限后
    按上述顺序截断，不用随机采样，同一输入任何时候重跑都得到同一份卷宗。调用方
    已确认 `text` 在 `chapter_text` 里，理论上 both/text_only 至少有一条命中；真的
    一条都没有（分段边界极端情况）就返回空列表，交由调用方兜底拒绝。"""
    segments = index_source_segments(chapter_text)
    both_indexes: list[int] = []
    text_only_indexes: list[int] = []
    anchor_only_indexes: list[int] = []
    for index, seg in enumerate(segments):
        has_text = text in seg.text
        has_anchor = any(
            anchor and anchor in seg.text for anchor in anchor_texts
        )
        if has_text and has_anchor:
            both_indexes.append(index)
        elif has_text:
            text_only_indexes.append(index)
        elif has_anchor:
            anchor_only_indexes.append(index)
    if not both_indexes and not text_only_indexes:
        return []
    priority_indexes = both_indexes + text_only_indexes
    anchor_only_by_proximity = sorted(
        anchor_only_indexes,
        key=lambda index: (min(abs(index - anchor) for anchor in priority_indexes), index),
    )
    ordered_candidates = priority_indexes + anchor_only_by_proximity
    selected: list[int] = []
    used_chars = 0
    for index in ordered_candidates:
        if len(selected) >= _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES:
            break
        seg_text = segments[index].text
        if selected and used_chars + len(seg_text) > _ALIAS_VERDICT_DOSSIER_MAX_CHARS:
            continue
        selected.append(index)
        used_chars += len(seg_text)
    selected.sort()
    return [
        {
            "chapter_idx": chapter_idx, "segment_index": index + 1,
            "text": segments[index].text,
        }
        for index in selected
    ]


# 真实误登记事故 2：「王腾飞←王师弟」（第 189 章）——同一人工抽查发现的另一条误登记，
# 裁决闸两次都放行。原文里"看在王师弟的份上"这句话是血妖宗的李诗琪替另一个血妖宗
# 弟子王有材求情（王有材当章已经站到了孟浩一边），"王师弟"指王有材；王腾飞是同章
# 与孟浩敌对、正瞪着孟浩的另一个人，二者只是同姓。该章"王腾飞"出现 6 次、"王师弟"
# 只出现 1 次且恰好挨着王腾飞的戏份，卷宗按"离别名最近"的规则把王腾飞相关段落选进
# 去；根因不在卷宗检索，而在提问方式——"称谓 X 是否指代人名 Y 本人"是一道是非题，
# 模型看到卷宗里反复出现的是王腾飞，天然倾向对"是不是王腾飞"点头，这是确认偏误，
# 跟王腾飞与王有材同姓与否无关（换成任何两个同章出场、其中一个反复出现的角色都会
# 触发同样的偏误）。
#
# 修复：把裁决从"确认单一假设"改造成"从候选集中判别"。`selected_candidate` 取值
# 收紧为该章节里结构性命中的全部人物谱角色（角色规范名或其已确认别名的逐字子串命中，
# 见 `_alias_verdict_candidates`，零语义、不针对任何具体人名/姓氏特判）外加一个
# 显式的"都不是/无法确定"选项，schema 用 enum 同时限定候选集（与段号 enum 同一套
# 写法）。只有选中的候选恰好是本次申报的 `true_name` 才登记；选了候选集里的其他人、
# 选了"都不是/无法确定"、或候选集本身为空（不应该发生，防御性分支），一律拒绝。
# 这样"王师弟"这条会强迫模型在孟浩、王有材、李诗琪、王腾飞之间明确选一个并说出
# 理由，而不是回答一道"是不是"的确认题。


_ALIAS_VERDICT_NO_MATCH_LABEL = "都不是/无法确定"


def _alias_verdict_candidates(chapter_text: str, roster: dict[str, list[str]]) -> list[str]:
    """该章出现的全部人物谱候选人：结构判据，角色规范名或其任一已登记别名在章节原文
    里逐字子串命中即算该角色在这一章"出场"，零语义、不针对任何具体人名/姓氏做特判
    （见本节"真实误登记事故 2"）。`roster` 是调用方在本轮核验开始前对 `bible.characters`
    取的一次性快照（规范名 -> [规范名, 已登记别名...]），同一批核验内所有裁决调用
    共用同一份快照，不随本轮核验进度中途变化——避免同一批别名因处理顺序不同算出
    不同候选集，保证结构判据可复现。返回值按 `roster` 的登记顺序（即人物谱原始顺序）
    去重后的规范名列表；一个角色只要任一称谓命中就只计入一次，不按命中次数排序。"""
    return [
        name for name, surface_forms in roster.items()
        if any(form and form in chapter_text for form in surface_forms)
    ]


class _AliasVerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 候选判别（见本节"真实误登记事故 2"）：不再回答"是不是 true_name"的是非题，
    # 而是在候选集（该章出场的全部人物谱角色 + "都不是/无法确定"）里选一个。
    # schema 层面用 enum 收紧到 `_alias_verdict_call` 构造的候选集（与段号 enum
    # 同一写法），代码层面只有选中值恰好等于 true_name 才登记。
    selected_candidate: str
    # 钉证判据（见本节顶部大注释）：模型只需引用卷宗目录里某一条的段号，不再要求
    # 逐字复述原文。schema 层面用 enum 把候选值限定为本次卷宗实际收录的段号集合
    # （见 `_alias_verdict_call` 里对 `output_schema` 的 enum 注入），代码层面
    # `_alias_verdict_pin_segment` 再做一次结构性核验——两层防线都不依赖模型转录
    # 原文是否精确。
    supporting_segment_index: int
    # 可选的观测字段：模型仍可以给出一句引文供人工复核参考，但不再逐字比对、也不
    # 作为通过与否的判据（真实回归：this 字段之前叫钉证硬闸，"李富贵←小胖子"
    # "上官修←上官师叔"两条正确别名分别被误杀 / 同一输入两次结果不一致，根因是
    # 逐字复述本身脆弱，见本节顶部大注释）。
    supporting_quote: str = ""


async def _alias_verdict_call(
    *, alias: str, true_name: str, dossier: list[dict[str, Any]],
    candidates: list[str], project_id: str | None,
    cognition_card: ChapterCognitionCard | None = None,
) -> _AliasVerdictResponse:
    """裁决：唯一一次独立模型调用，只给卷宗原文与候选人名单，不点名"你猜是不是
    true_name"——把"这称谓到底指代候选里的哪一位"这个判别完全交给模型自己独立
    做出，答案落在候选集之外（含"都不是/无法确定"）一律视为没有确认申报的假设，
    与 `_prep_pack_true_name_verdict` 同一范式（先给独立卷宗，再让模型做判断，
    不预设结论）。`candidates` 由调用方 `_alias_verdict_candidates` 结构性算出，
    保证包含 `true_name` 本人（该章一定命中，见 `_alias_evidence_resolution` 对
    候选集为空的防御性拒绝分支的说明）。

    `cognition_card`（可选，见 docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.3）：认知层
    章级认知卡，附带每个候选"截至本章"的归属/关系背景摘要。这是 §1.3 指出的缺口的
    直接修复——裁决闸原先只能看"这一章本身"的原文，现在额外看到候选人跨章建立的
    状态事实。注入的文本块与卷宗原文段落明确分区、分别标注，并显式声明"判定仍须
    基于原文段落，认知卡只能辅助区分候选，不得仅凭认知卡下结论"（§4.3 防幻觉纪律：
    属性错了比没有更糟），不放松段号钉证、候选 enum、"都不是/无法确定"即拒绝等既有
    闸门。`cognition_card` 为 `None`，或其中没有任何候选带归属/关系摘要（当前真实
    状态：状态事实回填尚未真实跑过，`affiliations`/`relations` 均为空）时，
    `_cognition_status_lines` 返回空列表，下面拼出的 `cognition_section` 为空字符串，
    prompt 与本次改造前逐字一致——不留空标题、不留占位噪声。"""
    catalog = "\n\n".join(
        f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
        for item in dossier
    )
    segment_indexes = [item["segment_index"] for item in dossier]
    candidate_options = [*candidates, _ALIAS_VERDICT_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    cognition_lines = _cognition_status_lines(cognition_card)
    cognition_section = ""
    if cognition_lines:
        cognition_section = (
            "候选人已知状态（认知卡背景参考，来自结构化的归属/关系历史证据，不是本次"
            "卷宗原文）：\n" + "\n".join(cognition_lines) + "\n"
            "以上认知卡只用于辅助区分候选身份，本身不构成判定依据；下面才是本次判定"
            "必须依据的卷宗原文段落，若认知卡与原文段落冲突、或认知卡未提及，一律以"
            "原文段落为准，不得仅凭以上认知卡下结论。\n\n"
        )
    prompt = f"""{cognition_section}下面是原著第 {dossier[0]['chapter_idx']} 章中包含称谓"{alias}"的原文段落
（含前后语境，出现顺序不代表任何推断结论），每段前面标了段号：
{catalog}

该章出场的人物谱角色候选（判别范围仅限这些人，不要引入候选之外的人）：
{candidate_list}

任务：仅依据以上原文段落本身，判断称谓"{alias}"最可能指代上面候选中的哪一位本人。
- selected_candidate 必须从候选列表中选一个精确姓名，或者在证据不足以确定具体是谁时
  选"{_ALIAS_VERDICT_NO_MATCH_LABEL}"；不要因为某个候选在段落里出现次数多就倾向选他，
  只依据原文是否真的能确定"{alias}"说的就是他本人；
- supporting_segment_index 必须填上面某一段落标注的段号（取值只能是 {segment_indexes}
  之一），选你得出这个结论最主要依据的那一段，不要凭空填一个没在目录里出现的段号；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，不要求逐字
  精确，留空也可以。
只输出符合 Schema 的 JSON。"""
    operation_id = "character_alias_backfill_verdict:" + hashlib.sha256(
        json.dumps(
            {
                "alias": alias, "true_name": true_name, "candidates": candidates,
                "dossier": [
                    (item["chapter_idx"], item["segment_index"]) for item in dossier
                ],
                "cognition": cognition_lines,
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    schema = _AliasVerdictResponse.model_json_schema()
    # 参照 app/portraits.py `_current_identity_schema()` 给 evidence_ref 注入 enum
    # 的写法：把候选段号、候选人名单都收紧到本次实际可用的集合，模型在协议层面就
    # 选不出卷宗外的段号或候选集之外的人；真正生效的核验仍在
    # `_alias_verdict_pin_segment` 与 `_alias_evidence_resolution` 里做代码侧结构
    # 校验（provider 对 enum 的遵守不是可证明保证，见这两处调用点的说明）。
    schema["properties"]["supporting_segment_index"]["enum"] = segment_indexes
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_AliasVerdictResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=500,
        # 低温：这道闸的语义判断要稳定——同一份卷宗重跑不该一次选中一次不确定。
        # 真实回归：0.2 时同一批别名重跑三次，多条会在 same/uncertain 之间摇摆；
        # 降到 0 后结论稳定下来（钉证已改为选段号/选候选人，不再依赖模型逐字复述
        # 原文，温度对钉证成功率不再有直接影响，但仍保留低温以稳定判别结论本身）。
        temperature=0.0,
        format_retry_limit=1,
        semantic_retry_limit=1,
        output_schema=schema,
        call_meta={
            "stage": "别名回填桥接章裁决",
            "stage_key": "character_alias_backfill_verdict",
            "call_role": "stage_generate",
            "call_role_label": "别名桥接章裁决",
            "expected_json": True,
            "project_id": project_id,
            "alias": alias,
            "true_name": true_name,
            "candidates": candidates,
        },
    )


def _alias_verdict_pin_segment(
    dossier: list[dict[str, Any]], segment_index: Any,
) -> dict[str, Any] | None:
    """钉证：结构性校验，不再要求模型逐字复述原文（原 `_alias_verdict_pin_quote` 的
    做法，见本节顶部大注释）。模型只需要在响应里选一个段号，这里核对该段号是否落在
    本次卷宗实际收录的段号集合内——命中即视为钉证通过，因为卷宗内容本身就是代码
    检索出的真实原文，模型选中某一条不存在"编造"或"转录出错"的空间，只可能选中
    （合法）或选错/瞎编（非法）。非法输入（不是整数、或不在集合内）一律返回 None，
    交由调用方按无效裁决拒绝——不确定不登记的安全默认在这里同样成立：宁可拒绝一个
    只是格式不对的合法裁决，也不放宽到"看起来像是"就算数。命中返回该条卷宗记录
    （自带 chapter_idx，供调用方记账）。"""
    try:
        target = int(segment_index)
    except (TypeError, ValueError):
        return None
    for item in dossier:
        if item["segment_index"] == target:
            return item
    return None


def _alias_verdict_roster(bible: Bible) -> dict[str, list[str]]:
    """裁决候选面快照：规范名 -> [规范名, 已登记别名...]，取 `bible.characters` 当前
    状态的一次性快照。调用方（`_verify_character_aliases_in_place` /
    `backfill_character_aliases` / `reverify_character_aliases`）各自在本轮核验开始前
    构造一次，循环内对同一个 bible 的所有裁决调用共用同一份，不随本轮核验进度中途
    变化——结构判据要求同一输入任何时候重跑结果一致，如果候选面随每条别名的核验
    结果实时增减，同一批别名先后处理顺序不同会算出不同的候选集，不可复现。"""
    return {c.name: [c.name, *(a.text for a in c.aliases)] for c in bible.characters}


async def _alias_evidence_resolution(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    text: str,
    true_name: str,
    evidence_chapter_index: int,
    evidence_quote: str,
    *,
    roster: dict[str, list[str]],
    project_id: str | None = None,
    bible: Bible | None = None,
) -> dict[str, Any]:
    """别名证据判定的统一入口，供三处调用方（`_verify_character_aliases_in_place` /
    `backfill_character_aliases` / `reverify_character_aliases`）共用。分两段：

    第一段（既有逻辑不变）：模型申报的章节直接核验通过，就采信模型申报的
    (evidence_chapter_index, evidence_quote)；不通过时不直接拒绝——模型定位错了章节
    不代表申报的语义假设本身是错的，退一步交给 `_find_alias_bridge_chapter` 在全书
    范围内确定性检索桥接章。两条路径都没有可核验的证据 → 拒绝
    （reason="no_bridge_chapter"）。

    第二段（裁决闸，见本节顶部大注释）：无论证据来自哪条路径，"该章节同时出现别名与
    角色规范名（或已确认别名）"只证明"同章共现"，证明不了"指代同一人"——必须让模型
    看着这一章的真实原文，从该章出场的全部人物谱候选（`roster` 经
    `_alias_verdict_candidates` 结构性算出，见"真实误登记事故 2"）里判别称谓 text
    最可能指代谁；裁决结果必须恰好选中 true_name 本人，且支撑段号钉证在卷宗段号
    集合内，才算真正核验通过。选中候选集里的其他人（reason="candidate_mismatch"）、
    选"都不是/无法确定"（reason="candidate_uncertain"）、候选集为空（防御性分支，
    reason="no_verdict_candidates"，正常不应触发——true_name 或其已登记别名命中
    该章是本函数走到这一步的前提，必然会被 `_alias_verdict_candidates` 收进候选集，
    见该函数 docstring）、裁决调用失败（reason="verdict_call_failed"）、段号钉证
    失败（reason="segment_not_pinned"），一律拒绝（不确定不登记）。

    返回统一结构 {"accepted": bool, "chapter_idx": int|None, "quote": str,
    "reason": str}：accepted=True 时 chapter_idx/quote 是应当登记的证据（来自第一段，
    与裁决闸引用的卷宗原文无关——裁决闸只是额外必须通过的门槛，不改写已核验证据的
    内容）；accepted=False 时 reason 是机器可读的拒绝原因，供调用方记账与复核报告。

    `bible`（可选，见 docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.3）：调用方若提供
    完整 `Bible`，会额外组装一张章级认知卡（`build_chapter_cognition_card`，候选范围
    限定为本次裁决闸算出的 `candidates`）注入 `_alias_verdict_call` 的提示词，帮助
    模型看到候选人跨章建立的归属/关系背景，不改变裁决规则本身。缺省 `None`（例如
    测试直接构造裁决场景、不需要认知卡）时行为与认知卡引入前完全一致。"""
    empty = {"accepted": False, "chapter_idx": None, "quote": "", "reason": ""}
    if _alias_declaration_verified(
        chapters_by_idx, anchor_texts, text, evidence_chapter_index, evidence_quote,
    ):
        resolved_chapter_index, resolved_quote = evidence_chapter_index, evidence_quote
    else:
        bridge = _find_alias_bridge_chapter(chapters_by_idx, anchor_texts, text)
        if bridge is None:
            return {**empty, "reason": "no_bridge_chapter"}
        resolved_chapter_index, resolved_quote = bridge

    chapter_text = chapters_by_idx.get(resolved_chapter_index, "")
    candidates = _alias_verdict_candidates(chapter_text, roster)
    if not candidates:
        return {**empty, "reason": "no_verdict_candidates"}
    # 卷宗证据锚点必须覆盖全部候选人，不能只锚定被测的这一位（真实误登记事故 2、
    # 见本节顶部大注释）：只把候选名单摆给模型看，卷宗本身若只收录"text 与被测
    # true_name 共现"的段落，模型就永远看不到"另一个候选人在这章别的地方被点名"
    # 这条关键证据——第 189 章"王有材默默站起身站在孟浩身后"这段原文既不含
    # "王师弟"也不含被测的"王腾飞"，只按 anchor_texts={"王腾飞"} 检索会把它漏掉，
    # 模型只能看着反复出现的"王腾飞"就近作答，重演确认偏误。把全部候选人（结构性
    # 算出，来自 `_alias_verdict_candidates`）的规范名与已登记别名一并纳入锚点，
    # dossier 的 anchor_only 类别就能把"王有材"那一段也按接近别名段落的程度收录
    # 进来，模型才有机会看到真正指向正确候选的证据，而不只是被反复出现的名字带偏。
    dossier_anchor_texts = set(anchor_texts) | {
        form for name in candidates for form in roster.get(name, [])
    }
    dossier = _alias_verdict_dossier(
        resolved_chapter_index, chapter_text, text, dossier_anchor_texts,
    )
    if not dossier:
        return {**empty, "reason": "no_verdict_dossier"}
    cognition_card = (
        build_chapter_cognition_card(
            bible, chapters_by_idx, resolved_chapter_index, character_names=candidates,
        )
        if bible is not None else None
    )
    try:
        response = await _alias_verdict_call(
            alias=text, true_name=true_name, dossier=dossier,
            candidates=candidates, project_id=project_id,
            cognition_card=cognition_card,
        )
    except Exception as exc:  # noqa: BLE001 - 裁决调用失败按不确定处理：不确定不登记
        log_provider_call(
            "character_alias_backfill_verdict", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"alias": text, "true_name": true_name, "error": str(exc)[:300]},
        )
        return {**empty, "reason": "verdict_call_failed"}
    if response.selected_candidate != true_name:
        reason = (
            "candidate_uncertain"
            if response.selected_candidate == _ALIAS_VERDICT_NO_MATCH_LABEL
            else "candidate_mismatch"
        )
        return {**empty, "reason": reason}
    if _alias_verdict_pin_segment(dossier, response.supporting_segment_index) is None:
        return {**empty, "reason": "segment_not_pinned"}
    return {
        "accepted": True, "chapter_idx": resolved_chapter_index,
        "quote": resolved_quote, "reason": "",
    }


async def _verify_character_aliases_in_place(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, list[str]]:
    """`generate_bible` 主链路核验：模型随人物谱正文一并申报的 aliases 同样只是申报，
    落库前必须过同一套代码核验（`_alias_evidence_resolution`，与回填函数共用；模型
    申报章节没通过共现闸时，退一步做全书桥接章检索，通过共现闸后还要再过一道桥接章
    原文独立裁决，见该函数 docstring）。只处理 aliases 字段，绝不触碰角色的任何其它
    既有字段。"""
    chapters_by_idx = _chapters_by_idx(chapters)
    roster = _alias_verdict_roster(bible)
    added: dict[str, list[str]] = {}
    for character in bible.characters:
        anchor_texts = {character.name}
        verified: list[CharacterAlias] = []
        added_texts: list[str] = []
        for item in character.aliases:
            text = (item.text or "").strip()
            if not text or text == character.name or text in anchor_texts:
                continue
            resolved = await _alias_evidence_resolution(
                chapters_by_idx, anchor_texts, text, character.name,
                item.evidence_chapter_index, item.evidence_quote,
                roster=roster, project_id=project_id, bible=bible,
            )
            if resolved["accepted"]:
                verified.append(CharacterAlias(
                    text=text, name_kind=item.name_kind,
                    evidence_chapter_index=resolved["chapter_idx"],
                    evidence_quote=resolved["quote"],
                ))
                anchor_texts.add(text)
                added_texts.append(text)
        character.aliases = verified
        if added_texts:
            added[character.name] = added_texts
    return added


def _render_alias_backfill_source(
    chapters: list[dict], budget: int = ALIAS_BACKFILL_SOURCE_BUDGET_CHARS,
) -> str:
    """为别名回填渲染全书原文：块头强制显示原文章节序号（idx），不像 `_render_bible_source`
    那样优先用章节标题——回填要求模型精确报出 `evidence_chapter_index`，标题文本
    （可能是任意小说章节名）无法保证与 idx 对应，块头必须显式给出数字。"""
    valid = [ch for ch in chapters if (ch.get("content") or "").strip()]
    blocks: list[str] = []
    used = 0
    for chapter in valid:
        remain = budget - used
        if remain <= 200:
            break
        content = chapter["content"].strip()
        clipped = content[:remain]
        suffix = "……（原文过长已截断）" if len(content) > remain else ""
        blocks.append(f"【第 {chapter.get('idx', '?')} 章】\n{clipped}{suffix}")
        used += len(clipped)
    return "\n\n".join(blocks)


class _AliasBackfillDeclaration(BaseModel):
    """别名回填申报合同：模型只申报，是否登记由后端核验决定。"""

    character_name: str = ""
    text: str = ""
    name_kind: str = ""
    evidence_chapter_index: int = -1
    evidence_quote: str = ""


class _AliasBackfillDraft(BaseModel):
    aliases: list[_AliasBackfillDeclaration] = Field(default_factory=list)


async def backfill_character_aliases(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, list[str]]:
    """窄口径别名回填（层一，用于当前项目一次性回填历史人物谱）：全书上下文，只产出并
    核验 `Character.aliases`，绝不改写人物谱任何其它既有字段（name/role/appearance_canonical/
    personality/speech_style/relationships/ref_image_path/portrait_prompt_override 全部
    原样保留，本函数不读写它们）。

    调用方式：协调层在部署窗口拿到已定稿的 `bible`（`Bible` 实例）与该项目全书 `chapters`
    （`list[dict]`，需含 `idx`/`content` 字段，与 `generate_bible` 输入同构）后直接：

        added = await backfill_character_aliases(bible, chapters, project_id=project_id)

    函数原地把核验通过的别名追加进对应 `Character.aliases`（幂等：已存在的别名文本、或与
    `character.name` 相同的文本不会重复追加，可安全重跑）；调用方随后自行把更新后的
    `bible` 序列化落库（本函数不做任何数据库读写——app/db.py 由其它 agent 并行改动，
    不在本函数职责范围内）。

    返回值 `{character_name: [本次新增别名文本, ...]}`，供调用方记账/日志展示；返回空 dict
    不代表失败（可能全书确实没有可核验的别名，也可能模型调用失败——两者都已通过
    `log_provider_call` 记录，失败时 status="FAILED"，全书无可核验别名时 status="EMPTY"）。

    核验规则见 `_alias_evidence_resolution`：模型只负责申报语义假设（character_name+text），
    代码逐字核验证据；模型申报的章节没通过共现闸时，代码在全书范围内确定性检索桥接章
    （`_find_alias_bridge_chapter`）作为兜底，找不到才真正拒绝——不确定不登记。
    禁止任何具体称谓的硬编码——判据只看结构（逐字子串命中 + 章节内共现），不针对
    "许师姐""小胖子"等具体词做特判分支。
    """
    chapters_by_idx = _chapters_by_idx(chapters)
    source = _render_alias_backfill_source(chapters)
    if not source.strip() or not bible.characters:
        return {}
    verdict_roster = _alias_verdict_roster(bible)
    roster_text = "、".join(
        c.name + (f"（已登记别名：{'、'.join(a.text for a in c.aliases)}）" if c.aliases else "")
        for c in bible.characters
    )
    prompt = f"""任务：通读下面的全书正文，为【已收录角色】找出他们在原文中出现过的其它称谓
（外号、尊称、代称、未揭晓真名前的描述性代称等），逐条给出可核验的证据。

已收录角色（只为这些人申报别名，不要发明角色列表之外的人）：
{roster_text}

要求：
1. 每条别名给五个字段：character_name（必须逐字等于上面角色列表中的某个名字）、
   text（该别名在原文中的逐字写法）、name_kind（personal_name=真名/honorific=尊称/
   referential=代称，按原文语境判断该称谓的性质）、evidence_chapter_index（该别名出现的
   证据所在章节序号，取该章节【第 N 章】块头里的数字 N——注意这不是该别名第一次出现的
   章节，而是该别名与角色正式姓名（或本角色另一条已确认别名）同时出现的那一章；很多别名
   （尤其是真名揭晓前的描述性代称）最早出现时全书还没交代过角色真名，那一章通不过共现
   核验，要在全书范围内找到两者共现的章节再申报——一旦登记成功，该别名会覆盖它在全书的
   所有出现，不局限于你引用的这一章）、evidence_quote（该共现章节原文中的逐字引句，必须
   原样照抄，一个字都不能改，也不要自己在引句前后加引号包裹——原文本来有没有引号就照抄
   有没有，不要额外添加；且这句引文所在章节里必须能同时找到该角色的正式姓名或本角色的
   另一条别名——如果找不到这种共现，说明这条证据站不住，不要申报）。
2. 不确定就不要申报：证据不足、记不清原文原句、全书都找不到别名与正式姓名共现、或章节
   序号可能有误的情况，宁可漏报，绝不能编造或近似改写引句——后端会逐字核对，改写过的
   引句或自行添加的引号包裹都无法通过、白白浪费申报。
3. 同一个别名同一个角色只申报一次；角色的正式姓名本身不算别名，不要重复申报。
4. 只申报别名本身，不要输出角色的外观、性格、关系等其它信息——这些字段本次不会被采用。

全书正文（部分较长章节可能已截断，仅代表你能看到的范围，不代表原文实际只有这些）：
{source}

输出 JSON Schema：
{{"aliases": [{{"character_name": str, "text": str, "name_kind": "personal_name|honorific|referential", "evidence_chapter_index": int, "evidence_quote": str}}]}}"""
    try:
        raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX},
             {"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=8192,
            call_meta={
                "stage": "人物别名回填",
                "stage_key": "character_alias_backfill",
                "call_role": "stage_generate",
                "call_role_label": "别名回填",
                "expected_json": True,
                "project_id": project_id,
            },
        )
        declared = _AliasBackfillDraft.model_validate(extract_json(raw)).aliases
    except Exception as exc:  # noqa: BLE001 - 回填失败保留已有人物谱，不阻断调用方
        log_provider_call(
            "character_alias_backfill", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"error": str(exc)[:300]},
        )
        return {}

    by_name = {c.name: c for c in bible.characters}
    grouped: dict[str, list[_AliasBackfillDeclaration]] = defaultdict(list)
    for item in declared:
        name = (item.character_name or "").strip()
        if name in by_name:
            grouped[name].append(item)

    added: dict[str, list[str]] = {}
    for name, items in grouped.items():
        character = by_name[name]
        anchor_texts = {character.name, *(a.text for a in character.aliases)}
        added_texts: list[str] = []
        for item in items:
            text = (item.text or "").strip()
            if not text or text in anchor_texts:
                continue
            resolved = await _alias_evidence_resolution(
                chapters_by_idx, anchor_texts, text, character.name,
                item.evidence_chapter_index, item.evidence_quote,
                roster=verdict_roster, project_id=project_id, bible=bible,
            )
            if resolved["accepted"]:
                character.aliases.append(CharacterAlias(
                    text=text, name_kind=item.name_kind,
                    evidence_chapter_index=resolved["chapter_idx"],
                    evidence_quote=resolved["quote"],
                ))
                anchor_texts.add(text)
                added_texts.append(text)
        if added_texts:
            added[name] = added_texts

    log_provider_call(
        "character_alias_backfill", config.MODEL_TEXT,
        "OK" if added else "EMPTY", None, 0,
        meta={
            "declared": len(declared),
            "verified": sum(len(v) for v in added.values()),
            "characters_touched": list(added.keys()),
        },
    )
    return added


async def reverify_character_aliases(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """层一别名复核：对 bible 中已登记的全部 `Character.aliases` 逐条重跑
    `_alias_evidence_resolution`（含裁决闸，见该函数与上方"A1b. 裁决闸"注释），不通过
    的从该角色的 aliases 中移除。用于清理裁决闸补上之前落库的历史别名，也可作为未来
    任何别名批次的通用复核工具重复调用（幂等：已经通过新闸的别名重跑仍然通过，不会
    被误删）。

    背景（真实事故）：裁决闸补上之前落库的别名只过了"同章共现"这一道更弱的核验，
    证明不了"指代同一人"——已发生的误登记是模型在没看到桥接章原文的情况下凭全书
    记忆断言的语义假设，实际那一章里该称谓明确是另一个人；共现闸对几乎每章都出场的
    角色近乎零过滤力。本函数让所有既有别名重新过一遍现在的完整核验链，是清理这类
    历史误登记的通用工具，不针对任何具体人名做特判。

    调用方式：

        report = await reverify_character_aliases(bible, chapters, project_id=project_id)

    与 `backfill_character_aliases` 共享同一套核验入口、同一个"不确定不登记"默认——
    唯一区别是候选来源：这里不发起新的模型申报，直接把 `character.aliases` 里已有的
    (text, evidence_chapter_index, evidence_quote) 当作待复核的申报重新核验一遍。每个
    角色内部按既有别名的列表顺序增量建立 anchor_texts（与 backfill 的建表方式一致）：
    前面的别名先通过复核才会被后面同角色的别名当作共现锚点，避免"用一条本身尚未证实
    的别名去证明另一条别名"的循环依赖。

    返回 `{character_name: [{"text":, "kept": bool, "reason": str}, ...]}`，逐条给出
    复核结论与拒绝原因（`kept=True` 时 `reason==""`），供调用方生成复核报告；只原地
    改写 `Character.aliases`，不触碰角色的任何其它既有字段。角色本来就没有别名的
    不出现在返回结果里。"""
    chapters_by_idx = _chapters_by_idx(chapters)
    roster = _alias_verdict_roster(bible)
    report: dict[str, list[dict[str, Any]]] = {}
    for character in bible.characters:
        if not character.aliases:
            continue
        anchor_texts = {character.name}
        kept: list[CharacterAlias] = []
        entries: list[dict[str, Any]] = []
        for item in character.aliases:
            text = (item.text or "").strip()
            resolved = await _alias_evidence_resolution(
                chapters_by_idx, anchor_texts, text, character.name,
                item.evidence_chapter_index, item.evidence_quote,
                roster=roster, project_id=project_id, bible=bible,
            )
            if resolved["accepted"]:
                kept.append(CharacterAlias(
                    text=text, name_kind=item.name_kind,
                    evidence_chapter_index=resolved["chapter_idx"],
                    evidence_quote=resolved["quote"],
                ))
                anchor_texts.add(text)
                entries.append({"text": text, "kept": True, "reason": ""})
            else:
                entries.append({
                    "text": text, "kept": False, "reason": resolved["reason"],
                })
        character.aliases = kept
        report[character.name] = entries
    return report


# ---------- A2. 状态事实回填（认知层，见 docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.1） ----------
#
# 状态事实（Character.affiliations 阵营归属 / Character.relations 对人关系）与层一别名的
# 核心区别：别名恒真（一次核验永久生效），状态事实带"有效区间"，需要"截至第 N 章"的区间
# 语义（设计文档 §3.2）。核验管线完全复用层一已经用三条真实事故验证过的机制——不重新
# 实现语义判断（禁止黑白名单式修复，任何具体人名/势力名/称谓都不得硬编码特判）：
# - 核心证据（申报角色与归属/关系对象是否真的在该章共现、引句是否逐字命中）复用
#   `_alias_declaration_verified`（判据模式不变：把"申报的别名文本"换成"归属组织名/
#   关系对象人名"，把"角色规范名或已确认别名"作为共现锚点，两者结构完全一致）；
# - 模型申报章节没通过共现闸时，复用 `_find_alias_bridge_chapter` 在全书范围内确定性
#   检索桥接章（不受 ALIAS_BACKFILL_SOURCE_BUDGET_CHARS 预算限制，见该函数 docstring）；
# - "同章共现≠指代同一人"的裁决闸同样成立：主角在一章出现几十次，跟任何词都共现，
#   共现只是必要条件——复用 `_alias_verdict_candidates`（该章出场的全部人物谱角色，
#   零语义结构扫描）与 `_alias_verdict_dossier`（卷宗覆盖全部候选证据，不止被测对象
#   周围，见该函数 docstring 对"孟兄/孟浩"真实回归的说明）、`_alias_verdict_pin_segment`
#   （段号钉证，不比对模型转录引句）。唯一新增的是 `_status_fact_verdict_call`：
#   `_alias_verdict_call` 的提示词是"称谓 X 指代候选中的谁"这句话术专用于别名场景
#   （term-to-person），归属/关系要问的是"这段证据里，与某势力/某人存在这层关系的其实
#   是候选中的谁"（fact-to-person）——语义不同不能共用同一句提示词，但候选判别机制
#   本身（枚举收紧候选/段号、"都不是/无法确定"选项、拒绝是非题式确认偏误）完全照搬，
#   不是另起炉灶。
#
# 额外新增的核验环节：有效区间起止章（valid_from_chapter/valid_to_chapter）本身也必须
# 有证据支撑，不能让模型随口给一个区间——见 `_status_fact_interval_resolution`。

_STATUS_FACT_VERDICT_STAGE_KEY = "character_status_fact_backfill_verdict"


async def _status_fact_verdict_call(
    *, fact_noun: str, claim_text: str, dossier: list[dict[str, Any]],
    candidates: list[str], project_id: str | None,
) -> _AliasVerdictResponse:
    """状态事实（归属/关系）候选判别裁决：与 `_alias_verdict_call` 同一范式（代码检索
    卷宗 → 模型在候选集中独立判别 → 段号钉证），复用其响应结构（`_AliasVerdictResponse`，
    字段本身零语义，候选/段号/引句三项对别名与状态事实同样适用，不需要另造一个响应类）、
    候选与卷宗来源（`_alias_verdict_candidates`/`_alias_verdict_dossier`，调用方负责传入）
    与钉证核验（`_alias_verdict_pin_segment`，调用方负责调用）——本函数只负责提问措辞与
    发起这一次独立模型调用，不重复实现候选判别的机制本身。

    `fact_noun` 是自然语言里的关系性质描述（"势力归属"/"人物关系"），`claim_text` 是被
    判别的归属对象（org）或关系对象（to）文本。返回值语义与 `_alias_verdict_call` 完全
    一致：`selected_candidate` 命中候选集之外（含"都不是/无法确定"）一律视为没有确认
    申报的假设，调用方据此拒绝登记。"""
    catalog = "\n\n".join(
        f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
        for item in dossier
    )
    segment_indexes = [item["segment_index"] for item in dossier]
    candidate_options = [*candidates, _ALIAS_VERDICT_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    prompt = f"""下面是原著第 {dossier[0]['chapter_idx']} 章中与"{claim_text}"相关的原文段落
（含前后语境，出现顺序不代表任何推断结论），每段前面标了段号：
{catalog}

该章出场的人物谱角色候选（判别范围仅限这些人，不要引入候选之外的人）：
{candidate_list}

任务：仅依据以上原文段落本身，判断这段证据所描述的{fact_noun}（对象："{claim_text}"）
实际说的是上面候选中的哪一位本人。
- selected_candidate 必须从候选列表中选一个精确姓名，或者在证据不足以确定具体是谁时
  选"{_ALIAS_VERDICT_NO_MATCH_LABEL}"；不要因为某个候选在段落里出现次数多就倾向选他，
  只依据原文是否真的能确定这段{fact_noun}说的就是他本人；
- supporting_segment_index 必须填上面某一段落标注的段号（取值只能是 {segment_indexes}
  之一），选你得出这个结论最主要依据的那一段，不要凭空填一个没在目录里出现的段号；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，不要求逐字
  精确，留空也可以。
只输出符合 Schema 的 JSON。"""
    operation_id = _STATUS_FACT_VERDICT_STAGE_KEY + ":" + hashlib.sha256(
        json.dumps(
            {
                "fact_noun": fact_noun, "claim_text": claim_text, "candidates": candidates,
                "dossier": [
                    (item["chapter_idx"], item["segment_index"]) for item in dossier
                ],
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    schema = _AliasVerdictResponse.model_json_schema()
    schema["properties"]["supporting_segment_index"]["enum"] = segment_indexes
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_AliasVerdictResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=500,
        temperature=0.0,  # 与 _alias_verdict_call 同一理由：判别结论要稳定复现
        format_retry_limit=1,
        semantic_retry_limit=1,
        output_schema=schema,
        call_meta={
            "stage": "状态事实回填裁决",
            "stage_key": _STATUS_FACT_VERDICT_STAGE_KEY,
            "call_role": "stage_generate",
            "call_role_label": "状态事实裁决",
            "expected_json": True,
            "project_id": project_id,
            "fact_noun": fact_noun,
            "claim_text": claim_text,
            "candidates": candidates,
        },
    )


def _status_fact_interval_resolution(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    claim_text: str,
    resolved_chapter_index: int,
    declared_valid_from_chapter: int | None,
    declared_valid_to_chapter: int | None,
) -> tuple[int, int | None] | None:
    """有效区间起止章核验：不能让模型随口给一个区间（不确定不登记，安全默认同核心
    证据）。

    - 起点/终点若未申报（None）：起点回退为核心证据所在的 `resolved_chapter_index`
      （它已经过声明核验或桥接检索，本身就是有证据支撑的章节）；终点回退为 None，
      代表"尚无证据表明已失效"，与 `character_portraits` 表 `ep_end IS NULL` 的既有
      查询惯例同构（设计文档 §3.2）。
    - 起点/终点若申报了与 `resolved_chapter_index` 不同的章节：该章节必须独立满足
      "claim_text 与 anchor_texts 至少一项同时出现"这一结构性共现判据——与
      `_find_alias_bridge_chapter` 判断某章是否构成桥接章的判据完全同构，只是这里
      章节号已经由模型给出，不需要再检索一次，只核验这一章是否真的存在支撑证据。
    - 边界不能与核心证据点矛盾：核心证据本身已经证明该事实在 `resolved_chapter_index`
      这一章成立，起点不能晚于它、终点不能早于它。

    任一边界核验不过就返回 None，交由调用方整体拒绝——不做"部分采信、自动收窄区间"
    这种模型没有明确申报过的推测行为，属性错了比没有更糟（设计文档 §4.3）。
    """
    valid_from_chapter = resolved_chapter_index
    if declared_valid_from_chapter is not None and declared_valid_from_chapter != resolved_chapter_index:
        if declared_valid_from_chapter > resolved_chapter_index:
            return None
        boundary_text = chapters_by_idx.get(declared_valid_from_chapter, "")
        if not boundary_text or claim_text not in boundary_text:
            return None
        if not any(anchor and anchor in boundary_text for anchor in anchor_texts):
            return None
        valid_from_chapter = declared_valid_from_chapter

    valid_to_chapter: int | None = None
    if declared_valid_to_chapter is not None:
        if declared_valid_to_chapter == resolved_chapter_index:
            valid_to_chapter = resolved_chapter_index
        else:
            if declared_valid_to_chapter < resolved_chapter_index:
                return None
            boundary_text = chapters_by_idx.get(declared_valid_to_chapter, "")
            if not boundary_text or claim_text not in boundary_text:
                return None
            if not any(anchor and anchor in boundary_text for anchor in anchor_texts):
                return None
            valid_to_chapter = declared_valid_to_chapter

    return valid_from_chapter, valid_to_chapter


async def _status_fact_evidence_resolution(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    claim_text: str,
    subject_name: str,
    evidence_chapter_index: int,
    evidence_quote: str,
    declared_valid_from_chapter: int | None,
    declared_valid_to_chapter: int | None,
    *,
    fact_noun: str,
    roster: dict[str, list[str]],
    project_id: str | None = None,
) -> dict[str, Any]:
    """状态事实证据判定的统一入口，与 `_alias_evidence_resolution` 同一流程骨架（该函数
    docstring 有完整的两段式说明，这里不重复）：核心证据核验（声明核验 → 桥接检索兜底）
    → 候选判别裁决（候选覆盖该章全部人物谱角色，卷宗证据覆盖全部候选而不止被测对象）
    → 有效区间核验（`_status_fact_interval_resolution`，别名机制没有这一步，因为别名
    恒真、不需要区间）。`claim_text` 是归属对象（org）或关系对象（to）的逐字文本，
    `subject_name` 是被测角色的规范名（裁决闸要求 `selected_candidate` 恰好等于它）。

    返回结构 `{"accepted": bool, "chapter_idx": int|None, "quote": str, "reason": str,
    "valid_from_chapter": int|None, "valid_to_chapter": int|None}`：`accepted=True` 时
    后四个字段是应当登记的证据与区间；`accepted=False` 时 `reason` 是机器可读拒绝原因
    （`no_bridge_chapter`/`no_verdict_candidates`/`no_verdict_dossier`/
    `verdict_call_failed`/`candidate_mismatch`/`candidate_uncertain`/
    `segment_not_pinned`/`interval_unverified`）。
    """
    empty = {
        "accepted": False, "chapter_idx": None, "quote": "", "reason": "",
        "valid_from_chapter": None, "valid_to_chapter": None,
    }
    if _alias_declaration_verified(
        chapters_by_idx, anchor_texts, claim_text, evidence_chapter_index, evidence_quote,
    ):
        resolved_chapter_index, resolved_quote = evidence_chapter_index, evidence_quote
    else:
        bridge = _find_alias_bridge_chapter(chapters_by_idx, anchor_texts, claim_text)
        if bridge is None:
            return {**empty, "reason": "no_bridge_chapter"}
        resolved_chapter_index, resolved_quote = bridge

    chapter_text = chapters_by_idx.get(resolved_chapter_index, "")
    candidates = _alias_verdict_candidates(chapter_text, roster)
    if subject_name not in candidates:
        # 防御性分支：subject_name 对应的 anchor_texts 已经通过声明核验/桥接检索命中
        # 该章，理论上必然被 `_alias_verdict_candidates` 收进候选集（正常不应触发）。
        return {**empty, "reason": "no_verdict_candidates"}
    # 卷宗证据锚点必须覆盖全部候选人，不能只锚定被测对象一方（与 `_alias_evidence_resolution`
    # 同一理由，见该函数关于"真实误登记事故 2"的说明——只给卷宗看被测对象周围的证据，
    # 候选判别题就会名存实亡）。
    dossier_anchor_texts = set(anchor_texts) | {
        form for name in candidates for form in roster.get(name, [])
    }
    dossier = _alias_verdict_dossier(
        resolved_chapter_index, chapter_text, claim_text, dossier_anchor_texts,
    )
    if not dossier:
        return {**empty, "reason": "no_verdict_dossier"}
    try:
        response = await _status_fact_verdict_call(
            fact_noun=fact_noun, claim_text=claim_text, dossier=dossier,
            candidates=candidates, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001 - 裁决调用失败按不确定处理：不确定不登记
        log_provider_call(
            _STATUS_FACT_VERDICT_STAGE_KEY, config.MODEL_TEXT, "FAILED", None, 0,
            meta={
                "claim_text": claim_text, "subject_name": subject_name,
                "error": str(exc)[:300],
            },
        )
        return {**empty, "reason": "verdict_call_failed"}
    if response.selected_candidate != subject_name:
        reason = (
            "candidate_uncertain"
            if response.selected_candidate == _ALIAS_VERDICT_NO_MATCH_LABEL
            else "candidate_mismatch"
        )
        return {**empty, "reason": reason}
    if _alias_verdict_pin_segment(dossier, response.supporting_segment_index) is None:
        return {**empty, "reason": "segment_not_pinned"}

    interval = _status_fact_interval_resolution(
        chapters_by_idx, anchor_texts, claim_text, resolved_chapter_index,
        declared_valid_from_chapter, declared_valid_to_chapter,
    )
    if interval is None:
        return {**empty, "reason": "interval_unverified"}
    valid_from_chapter, valid_to_chapter = interval

    return {
        "accepted": True, "chapter_idx": resolved_chapter_index, "quote": resolved_quote,
        "reason": "", "valid_from_chapter": valid_from_chapter,
        "valid_to_chapter": valid_to_chapter,
    }


class _StatusFactAffiliationDeclaration(BaseModel):
    """归属回填申报合同：模型只申报，是否登记由后端核验决定（与
    `_AliasBackfillDeclaration` 同一纪律）。"""

    character_name: str = ""
    org: str = ""
    relation_kind: str = ""
    evidence_chapter_index: int = -1
    evidence_quote: str = ""
    valid_from_chapter: int | None = None    # 不申报=交给代码回退为证据所在章
    valid_to_chapter: int | None = None      # 不申报=尚无证据表明已失效


class _StatusFactRelationDeclaration(BaseModel):
    """关系回填申报合同：结构与 `_StatusFactAffiliationDeclaration` 同构，唯一差别是
    `org`（归属对象，自由文本）换成 `to`（关系对象，必须是人物谱里已有的另一个人）。"""

    character_name: str = ""
    to: str = ""
    relation_kind: str = ""
    evidence_chapter_index: int = -1
    evidence_quote: str = ""
    valid_from_chapter: int | None = None
    valid_to_chapter: int | None = None


class _StatusFactBackfillDraft(BaseModel):
    affiliations: list[_StatusFactAffiliationDeclaration] = Field(default_factory=list)
    relations: list[_StatusFactRelationDeclaration] = Field(default_factory=list)


def _status_fact_roster_hint(character: Character) -> str:
    """为回填提示词的已收录角色清单附上该角色已登记的归属/关系摘要（帮助模型不重复
    申报、也不与已知事实矛盾），纯字符串拼装、无模型调用。"""
    parts: list[str] = []
    if character.affiliations:
        parts.append("归属：" + "、".join(a.org for a in character.affiliations))
    if character.relations:
        parts.append("关系：" + "、".join(
            f"{r.to}({r.relation_kind})" for r in character.relations
        ))
    return "；".join(parts)


async def backfill_character_status_facts(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, dict[str, list[str]]]:
    """窄口径状态事实回填（认知层，用于当前项目一次性回填历史人物谱）：全书上下文，
    只产出并核验 `Character.affiliations`/`Character.relations`，绝不改写人物谱任何
    其它既有字段（包括层一的 `aliases`——与 `backfill_character_aliases` 互不干扰，
    两个函数各自只读写自己负责的字段）。

    调用方式：协调层在部署窗口拿到已定稿的 `bible`（`Bible` 实例，建议已经跑过
    `backfill_character_aliases` 回填过别名——状态事实的共现锚点会用到角色已确认的
    别名，先跑别名回填能提高召回率，但不是强制前置条件，`bible` 只有规范名也能跑）与
    该项目全书 `chapters`（`list[dict]`，需含 `idx`/`content` 字段，与
    `backfill_character_aliases`/`generate_bible` 输入同构）后直接：

        added = await backfill_character_status_facts(bible, chapters, project_id=project_id)

    函数原地把核验通过的归属/关系追加进对应 `Character.affiliations`/`Character.relations`
    （幂等：同一角色已登记过的 org / (to, relation_kind) 组合不会重复追加，可安全重跑）；
    调用方随后自行把更新后的 `bible` 序列化落库（本函数不做任何数据库读写）。

    返回值 `{"affiliations": {character_name: [本次新增归属org, ...]}, "relations":
    {character_name: [本次新增关系对象to, ...]}}`，供调用方记账/日志展示；两个子 dict
    都为空不代表失败（可能全书确实没有可核验的状态事实，也可能模型调用失败——两者都已
    通过 `log_provider_call` 记录，失败时 status="FAILED"，全书无可核验状态事实时
    status="EMPTY"）。

    核验规则见 `_status_fact_evidence_resolution`：模型只负责申报语义假设
    （character_name + org/to + 可选的有效区间），代码逐字核验证据、候选判别裁决、
    区间是否有证据支撑；任一环节不过 → 不登记（不确定不登记，安全默认，绝不放松）。
    禁止任何具体人名/势力名/称谓的硬编码——判据只看结构（逐字子串命中 + 章节内共现 +
    候选判别 + 区间证据），不针对具体词做特判分支。
    """
    chapters_by_idx = _chapters_by_idx(chapters)
    source = _render_alias_backfill_source(chapters)
    empty_result: dict[str, dict[str, list[str]]] = {"affiliations": {}, "relations": {}}
    if not source.strip() or not bible.characters:
        return empty_result
    roster = _alias_verdict_roster(bible)
    roster_text = "、".join(
        c.name + (f"（已登记{hint}）" if (hint := _status_fact_roster_hint(c)) else "")
        for c in bible.characters
    )
    prompt = f"""任务：通读下面的全书正文，为【已收录角色】找出他们在原文中有明确证据支撑的
势力/宗门归属，以及与其它已收录角色之间有明确证据支撑的人物关系（如同门、师徒、敌对、
盟友等），逐条给出可核验的证据。

已收录角色（只为这些人申报归属/关系，不要发明角色列表之外的人；关系的对象也必须是下面
列表里的另一个人，不能是角色本人，也不能是列表外的人）：
{roster_text}

归属（affiliations）每条给七个字段：
1. character_name（必须逐字等于上面角色列表中的某个名字）；
2. org（该角色所属的宗门/阵营/势力名，逐字照抄原文写法）；
3. relation_kind（该角色与该势力的关系性质，自由描述，如"成员""效忠""敌对"等，不强制
   使用固定词表）；
4. evidence_chapter_index（该角色姓名或已确认别名与该势力名同时出现、且原文明确交代
   归属关系的那一章的章节序号，取该章节【第 N 章】块头里的数字 N——只是同章出现不算，
   原文必须真的能看出这层归属关系）；
5. evidence_quote（该章节原文中的逐字引句，必须原样照抄，一个字都不能改，也不要自己
   在引句前后加引号包裹）；
6. valid_from_chapter（可选）：该归属从哪一章开始生效——不确定就不要填这个字段，后端
   会用 evidence_chapter_index 作为默认起点；只有原文明确交代了这层归属并非从头就有
   （比如后来才拜入门下）时才需要申报，且必须是能找到相应原文依据的章节，编造的起点
   会导致整条归属都不被采信；
7. valid_to_chapter（可选）：该归属到哪一章为止——不确定/仍在持续就不要填这个字段，
   后端默认视为尚未失效；只有原文明确交代了归属结束（叛出师门、转投他派等）时才需要
   申报，同样必须有原文依据支撑。

关系（relations）每条给七个字段，结构与归属完全相同，唯一差别：把 org 换成 to（关系
对象，必须是【已收录角色】列表中的另一个名字）。

不确定就不要申报：证据不足、记不清原文原句、原文没有明确交代归属/关系（只是同章出现
不算），宁可漏报，绝不能编造或近似改写引句——后端会逐字核对，改写过的引句或自行添加
的引号包裹都无法通过、白白浪费申报。只申报归属/关系，不要输出角色的外观、性格等其它
信息——这些字段本次不会被采用。

全书正文（部分较长章节可能已截断，仅代表你能看到的范围，不代表原文实际只有这些）：
{source}

输出 JSON Schema：
{{"affiliations": [{{"character_name": str, "org": str, "relation_kind": str, "evidence_chapter_index": int, "evidence_quote": str, "valid_from_chapter": int, "valid_to_chapter": int}}], "relations": [{{"character_name": str, "to": str, "relation_kind": str, "evidence_chapter_index": int, "evidence_quote": str, "valid_from_chapter": int, "valid_to_chapter": int}}]}}"""
    try:
        raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX},
             {"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=8192,
            call_meta={
                "stage": "人物状态事实回填",
                "stage_key": "character_status_fact_backfill",
                "call_role": "stage_generate",
                "call_role_label": "归属关系回填",
                "expected_json": True,
                "project_id": project_id,
            },
        )
        declared = _StatusFactBackfillDraft.model_validate(extract_json(raw))
    except Exception as exc:  # noqa: BLE001 - 回填失败保留已有人物谱，不阻断调用方
        log_provider_call(
            "character_status_fact_backfill", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"error": str(exc)[:300]},
        )
        return empty_result

    by_name = {c.name: c for c in bible.characters}
    known_names = set(by_name.keys())

    added_affiliations: dict[str, list[str]] = {}
    for item in declared.affiliations:
        name = (item.character_name or "").strip()
        character = by_name.get(name)
        org = (item.org or "").strip()
        if character is None or not org:
            continue
        if org in {a.org for a in character.affiliations}:
            continue  # 幂等：已登记过的归属不重复追加，可安全重跑
        anchor_texts = {character.name, *(a.text for a in character.aliases)}
        resolved = await _status_fact_evidence_resolution(
            chapters_by_idx, anchor_texts, org, character.name,
            item.evidence_chapter_index, item.evidence_quote,
            item.valid_from_chapter, item.valid_to_chapter,
            fact_noun="势力归属", roster=roster, project_id=project_id,
        )
        if resolved["accepted"]:
            character.affiliations.append(CharacterAffiliation(
                org=org, relation_kind=item.relation_kind,
                evidence_chapter_index=resolved["chapter_idx"],
                evidence_quote=resolved["quote"],
                valid_from_chapter=resolved["valid_from_chapter"],
                valid_to_chapter=resolved["valid_to_chapter"],
            ))
            added_affiliations.setdefault(name, []).append(org)

    added_relations: dict[str, list[str]] = {}
    for item in declared.relations:
        name = (item.character_name or "").strip()
        character = by_name.get(name)
        to = (item.to or "").strip()
        if character is None or not to or to == name or to not in known_names:
            continue  # 关系对象必须是人物谱里已有的另一个人（不能是角色本人或未知的人）
        if (to, item.relation_kind) in {(r.to, r.relation_kind) for r in character.relations}:
            continue  # 幂等：同一对象+同一关系性质已登记过的不重复追加
        anchor_texts = {character.name, *(a.text for a in character.aliases)}
        resolved = await _status_fact_evidence_resolution(
            chapters_by_idx, anchor_texts, to, character.name,
            item.evidence_chapter_index, item.evidence_quote,
            item.valid_from_chapter, item.valid_to_chapter,
            fact_noun="人物关系", roster=roster, project_id=project_id,
        )
        if resolved["accepted"]:
            character.relations.append(CharacterRelation(
                to=to, relation_kind=item.relation_kind,
                evidence_chapter_index=resolved["chapter_idx"],
                evidence_quote=resolved["quote"],
                valid_from_chapter=resolved["valid_from_chapter"],
                valid_to_chapter=resolved["valid_to_chapter"],
            ))
            added_relations.setdefault(name, []).append(to)

    log_provider_call(
        "character_status_fact_backfill", config.MODEL_TEXT,
        "OK" if (added_affiliations or added_relations) else "EMPTY", None, 0,
        meta={
            "declared_affiliations": len(declared.affiliations),
            "declared_relations": len(declared.relations),
            "verified_affiliations": sum(len(v) for v in added_affiliations.values()),
            "verified_relations": sum(len(v) for v in added_relations.values()),
        },
    )
    return {"affiliations": added_affiliations, "relations": added_relations}


async def _chapters_without_paratext(chapters: list[dict]) -> list[dict]:
    """把作者的话等旁文本从章节正文里剔掉，再交给人物谱这条链路。

    生产缺陷 R9：网文章节正文里直接粘着作者的话（求票、感谢读者、活动公告）。
    `_recurring_character_names` 按**原文逐字出现次数**产出「必收名单」，
    而提示词明令「名单里的每个名字…不得改写、合并或省略」——于是作者笔名
    在统计窗口里出现 27 次排第 4（高于真配角王有材 17 次），进入必收名单，
    **模型是被程序命令**建出那张人物卡的。它照办的同时把关系写成
    「创作者，在故事外注视并推动主角命运」，等于自己标注了疑虑。

    所以这不是模型幻觉，是程序把旁文本当成了正文来统计。判据与叙事蓝图
    共用一份（`app/source_paratext.PARATEXT_RULE`）。

    净化失败一律退回原文：人物谱不能因为这一步判不出来就产不出来。
    """
    from app.source_paratext import strip_paratext

    cleaned: list[dict] = []
    for index, chapter in enumerate(chapters):
        content = (chapter.get("content") or "")
        if not content.strip():
            cleaned.append(chapter)
            continue
        stripped = await strip_paratext(
            content, operation_id=f"bible.paratext:{chapter.get('id') or index}"
        )
        cleaned.append(
            chapter if stripped == content else {**chapter, "content": stripped}
        )
    return cleaned


async def generate_bible(chapters: list[dict], feedback: str = "", previous_bible: dict | None = None,
                         project_id: str | None = None,
                         visual_style_prompt: str | None = None) -> Bible:
    # 必须在渲染源文本**和**统计必收名单之前净化，两者都以正文为准。
    chapters = await _chapters_without_paratext(chapters)
    chapters_text = _render_bible_source(chapters, head_chapters=BIBLE_HEAD_CHAPTERS)
    must_cover = await _recurring_character_names(chapters)
    must_cover_part = ""
    if must_cover:
        must_cover_part = f"""
【必收角色名单】（后端已在前 {BIBLE_HEAD_CHAPTERS} 章及其后 {BIBLE_LOOKAHEAD_CHAPTERS} 章原文里逐字统计出现次数；次数多＝戏份重，必须全部收录）：
{'、'.join(f'{name}（{hits} 次）' for name, hits in must_cover)}

执行方式：
- 名单里的每个名字都要单独输出一个角色条目，name 逐字照抄名单写法，不得改写、合并或省略。
- 名单之外的重要角色仍然可以补充；总数以覆盖名单为准，不要为了写得短就删掉名单里的人。
- 唯一例外：名单里某个名字如果其实不是人物（是宗门、地名或法宝名），跳过它，不要硬编成角色。
"""
    previous_part = ""
    if previous_bible:
        names = "、".join(
            c.get("name", "") for c in previous_bible.get("characters", []) if c.get("name")
        )
        style = (previous_bible.get("world") or {}).get("visual_style_canonical", "")
        previous_part = f"\n当前人物谱摘要（用于对照返工，不可直接照抄错误）：\n已收录角色：{names or '无'}\n当前画风：{style or '无'}\n"
    feedback_part = ""
    if feedback.strip():
        feedback_part = f"""
人工打回重生要求（最高优先级）：
{feedback.strip()}

执行方式：
- 如果用户点名遗漏人物，必须回到原文中查找并收录；受角色总数上限影响时，删除更边缘的角色也要保留用户点名人物。
- 如果用户指出身份、关系、外观或称谓错误，必须按要求修正，并保持后续 relationships 一致。
- 不要把同一人物的外号、尊称、简称拆成多个角色；统一为原文最稳定的正式姓名。
"""
    visual_style_part = ""
    if visual_style_prompt:
        visual_style_part = f"""
统一画风（最高优先级，必须逐字写入 world.visual_style_canonical）：
{visual_style_prompt}

执行方式：
- 不得改写、扩写、缩写或替换该统一画风提示词。
- 角色外观、场景、定妆照和后续视频提示词都必须服从该统一画风。
"""
    prompt = f"""任务：从小说文本中提取角色圣经与世界观，用于后续 AI 视频生成的一致性控制。

要求：
1. 收录所有出场 2 次以上或明显重要的角色；【必收角色名单】里的人一个都不能少，其余按重要性补足，总数不超过 20 个。
2. appearance_canonical 是该角色的"固定外观锚点串"：40~60 字，必须包含 性别年龄感/发型发色/服装款式与颜色/1 个标志性特征。只写常规完整着装、中性站姿下可直接看见并能跨镜稳定复现的静态形态：五官、发型、体型、外层服装、可见配饰或面部标记。不写性格、欲望、气质、眼神行为、对他人的注视方式，不得写裸体、内衣、私密身体部位或必须暴露身体才能看见的特征。原著未描写的部分，按题材合理补全并保持内部一致。
3. visual_style_canonical：25~40 字的全局画风串，包含 美术风格/光线/色调，适配竖屏漫剧，必须依据本书题材定制。【硬性约束】必须是 CG/动画/漫画/插画类的非真人风格（如 3D 渲染、3D 写实 CG、2D 动画、动态漫画、厚涂插画、国漫风等，写实质感/照片级/胶片颗粒等氛围词可以保留），但严禁"真人实拍/真人出镜/实拍摄影"这类真人风格描述（否则后续 Seedance 视频接口会因疑似真人而报错 InputImageSensitiveContentDetected）。核心是画面为 CG/动画渲染而非真人拍摄。
4. speech_style 用于后续台词写作：句长习惯/口头禅/敬语习惯等，15~30 字。
5. name 必须互不重复且取原文最稳定的正式姓名；同一人物在原文中出现的其它别名/外号/尊称/
   代称（包括真名揭晓前的描述性代称，如"银色长袍女子"）不要拆成多个角色，而是逐条列入
   该角色的 aliases：每条给 text（原文逐字写法）、name_kind（personal_name=真名/
   honorific=尊称/referential=代称，按语境判断）、evidence_chapter_index（该别名出现的
   证据所在章节序号，取原文分块【】块头里的数字；如果看到的分块标题不是数字、无法确定
   准确章节序号，就不要申报这条别名——注意这个序号不是该别名第一次出现的章节，而是该
   别名与角色正式姓名（或本角色另一条已确认别名）同时出现的那一章；很多描述性代称最早
   出现时全书还没交代真名，那一章通不过共现核验，要在全书范围内找到两者共现的章节再
   申报，一旦登记成功该别名会覆盖它在全书的所有出现）、evidence_quote（该共现章节原文中
   的逐字引句，必须原样照抄，一个字都不能改，也不要自己在引句前后加引号包裹——原文本来
   有没有引号就照抄有没有，不要额外添加；且这句引文所在章节里必须能同时找到该角色的
   正式姓名或本角色的另一条别名——找不到这种共现就不要申报，后端会逐字核对，不满足条件
   的申报一律不会登记）。
   不确定就不要申报别名——宁可漏报，绝不能编造或近似改写引句。
6. relationships 只描述【已收录角色之间】的关系：relationships.to 必须逐字等于本次 characters 里某个角色的 name，不要指向未收录的人物（否则代码校验会因「关系指向未知角色」退回重写）。与圈外人物的关系请省略，或并入 personality 文字描述。

小说文本：
{chapters_text}{previous_part}{feedback_part}{visual_style_part}{must_cover_part}

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}], "aliases": [{{"text": str, "name_kind": "personal_name|honorific|referential", "evidence_chapter_index": int, "evidence_quote": str}}]}}], "world": {{"era": str, "genre": str, "visual_style_canonical": str}}}}"""
    loop = AgentLoop(
        stage_key="character_bible",
        contract_key="character_bible",
        goal="从原文章节生成来源可追溯、视觉锚点完整的人物圣经",
        scope_type="project",
        scope_id=project_id or hashlib.sha256(chapters_text.encode("utf-8")).hexdigest()[:16],
        artifact_type="character_bible",
        policy=AgentLoopPolicy(
            max_iterations=min(max(int(get_setting("max_repair_attempts") or 4), 1), 4),
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=False,
            repair_all_blockers=True,
        ),
    )

    def validate_authoritative_bible(candidate: Bible) -> list[str]:
        # The forced project style is authority, not a post-processing display
        # override. Apply it before AgentLoop records the accepted Artifact.
        if visual_style_prompt:
            candidate.world.visual_style_canonical = visual_style_prompt
        return validate_bible(candidate)

    bible = await _run_with_agent_loop(
        "角色圣经", "character_bible", prompt, Bible, validate_authoritative_bible,
        loop=loop, temperature=0.5, max_tokens=16384,
        # 默认的 3000 字任务摘要会把小说正文整段截掉：一旦首轮候选未过校验，
        # 后续每一轮"定向修复"都只看得到规则和上一版候选，模型只能照抄开头
        # 那几段里的人，人物谱越修越少。人物谱的任务本体就是原文，不能截。
        repair_user_prompt_limit=None,
    )
    if visual_style_prompt:
        bible.world.visual_style_canonical = visual_style_prompt
    # 规则 5 要求模型随人物谱一并申报别名，但那只是申报——落库前必须过与回填函数
    # 同一套代码核验（逐字命中 + 章节内共现 + 桥接章原文独立裁决），不满足就丢弃，
    # 不阻断人物谱本身。
    await _verify_character_aliases_in_place(bible, chapters, project_id=project_id)
    missing = [item for item in must_cover if not _bible_covers_name(bible, item[0])]
    if missing:
        added = await _supplement_bible_characters(
            bible, missing, chapters_text, visual_style_prompt=visual_style_prompt,
        )
        log_provider_call(
            "character_bible_coverage", config.MODEL_TEXT,
            "OK" if len(added) == len(missing) else "PARTIAL", None, 0,
            meta={
                "must_cover": [name for name, _ in must_cover],
                "missing": [name for name, _ in missing],
                "supplemented": added,
            },
        )
    return bible


# ---------- A2. 场景圣经（场景图素材库的规范场景，跨集场景一致性核心） ----------

class _SceneBibleDraft(BaseModel):
    """场景圣经输出合同（仅生成期使用）：一组规范场景。"""

    scenes: list[Scene]


async def generate_scene_bible(chapters: list[dict], bible: Bible,
                               feedback: str = "", project_id: str | None = None) -> list[Scene]:
    """从原文提取「规范场景」清单，作为场景图素材库的底稿（与 generate_bible 同构）。
    每个场景给 name（稳定短标签）+ scene_canonical（固定场景锚点串，画风约束与人物锚点一致：
    必须 CG/动画/漫画类非真人风格，否则后续 Seedance/Seedream 易因疑似真人报错）。"""
    chapters_text = _render_bible_source(chapters)
    style = bible.world.visual_style_canonical
    genre = bible.world.genre or ""
    feedback_part = ""
    if feedback.strip():
        feedback_part = f"\n人工打回重生要求（最高优先级）：\n{feedback.strip()}\n"
    prompt = f"""任务：从小说文本中提取【规范场景清单】，用于后续 AI 视频生成的场景一致性控制（场景图素材库）。

全片画风（场景锚点必须与之一致）：{style}
题材：{genre or '（未标注）'}

要求：
1. 只收录【反复出现 / 有戏份 / 画面感强】的关键场景（如主角居所、宗门广场、夜晚密林、朝堂等），最多 12 个；一次性出现的过场地点不要收录。
2. name：稳定的场景短标签（4~10 字，如"宗门广场""破败客栈内"），后续所有分镜的场景都收敛到这些名字，便于跨集复用同一张场景图。name 之间不要语义重复。
3. scene_canonical 是该场景的"固定场景锚点串"：30~60 字，必须包含 地点/室内外/典型光线时段/标志性陈设或建筑/整体氛围色调。只写视觉可见的环境信息，不写人物、不写剧情动作。原著未描写处按题材与画风合理补全并保持内部一致。
4. 【硬性约束】scene_canonical 必须贴合全片画风「{style}」，是 CG/动画/漫画/插画类的非真人渲染场景（写实质感氛围词可保留），严禁"真人实拍/实景照片/摄影棚实拍"这类描述（否则后续图像/视频接口会因疑似真人实景报错）。
5. location_kind 取"室内/室外/其他"之一。

小说文本：
{chapters_text}{feedback_part}

输出 JSON Schema：
{{"scenes": [{{"name": str, "scene_canonical": str, "location_kind": "室内|室外|其他"}}]}}"""
    loop = AgentLoop(
        stage_key="scene_bible",
        contract_key="scene_bible",
        goal="从原文章节提取跨集复用、来源可追溯的规范场景",
        scope_type="project",
        scope_id=project_id or hashlib.sha256((chapters_text + style).encode("utf-8")).hexdigest()[:16],
        artifact_type="scene_bible",
        policy=AgentLoopPolicy(
            max_iterations=min(max(int(get_setting("max_repair_attempts") or 4), 1), 4),
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=False,
        ),
    )
    draft = await _run_with_agent_loop(
        "场景圣经", "scene_bible", prompt, _SceneBibleDraft,
        lambda d: validate_scene_bible(d.scenes), loop=loop, temperature=0.5,
        # 与人物谱同因：修复轮不能把小说正文截掉，否则只会反复重排开头几个场景。
        repair_user_prompt_limit=None,
    )
    return list(draft.scenes)


# 模型以为看到了全部，把后半章静默丢掉。改为命名常量 + 截断标记，让模型知道"后文还有，按依据补全"。
SCREENPLAY_SOURCE_BUDGET_CHARS = 120000


_SOURCE_QUOTED_DIALOGUE_RE = re.compile(r"[“「『][^”」』\n]{2,240}[”」』]")
_SOURCE_SPEAKER_DIALOGUE_RE = re.compile(
    r"(?m)^[^\n：:]{1,20}[：:][^\n]{2,300}$"
)


def _source_dialogue_evidence(text: str, limit: int) -> str:
    """Select exact dialogue evidence from a source section without inventing prose.

    Long chapters used to be truncated at the head, which made dialogue in the
    middle or climax literally unavailable to generation and repair.  Preserve
    dialogue-shaped source fragments in addition to stable head/tail context.
    """
    if limit <= 0 or not text:
        return ""
    fragments: list[str] = []
    seen: set[str] = set()
    for pattern in (_SOURCE_SPEAKER_DIALOGUE_RE, _SOURCE_QUOTED_DIALOGUE_RE):
        for match in pattern.finditer(text):
            fragment = match.group(0).strip()
            condensed = re.sub(r"\s+", "", fragment)
            if not condensed or condensed in seen:
                continue
            seen.add(condensed)
            fragments.append(fragment)
    selected: list[str] = []
    used = 0
    for fragment in fragments:
        cost = len(fragment) + (1 if selected else 0)
        if used + cost > limit:
            continue
        selected.append(fragment)
        used += cost
    return "\n".join(selected)


def _render_screenplay_source(source_text: str, budget: int = SCREENPLAY_SOURCE_BUDGET_CHARS) -> str:
    text = source_text or ""
    if len(text) <= budget:
        return text
    marker_a = "\n\n……（中段叙事已按上下文预算压缩；以下保留中段原文对白证据）……\n"
    marker_b = "\n\n……（继续保留本章结尾原文，结尾事件与台词不得遗漏）……\n"
    payload_budget = max(0, budget - len(marker_a) - len(marker_b))
    head_budget = int(payload_budget * 0.35)
    tail_budget = int(payload_budget * 0.35)
    dialogue_budget = payload_budget - head_budget - tail_budget
    middle_end = max(head_budget, len(text) - tail_budget)
    middle = text[head_budget:middle_end]
    dialogue_evidence = _source_dialogue_evidence(middle, dialogue_budget)
    # If the middle contains little dialogue, use the remaining allowance for
    # contiguous context immediately after the head instead of wasting budget.
    unused = max(0, dialogue_budget - len(dialogue_evidence))
    head = text[:head_budget + unused]
    tail = text[-tail_budget:] if tail_budget else ""
    return head + marker_a + dialogue_evidence + marker_b + tail


def _character_resolution_prompt_block(episode: dict) -> str:
    """把剧本预检的姓名消歧结果转成生产硬合同。"""
    from app.identity_authority import normalize_character_resolutions

    rows = [
        item
        for item in normalize_character_resolutions(
            episode.get("character_resolutions") or [],
        )
        if identity_resolution_is_authoritative(item)
    ]
    if not rows:
        return (
            "【角色身份预解析】本集没有额外称谓决议；人物谱角色的 "
            "authority_id 使用 bible:<人物谱准确姓名>。"
            + model_identity_authority_prompt_rule()
        )
    return (
        "【角色身份预解析·剧本发布硬门禁】\n"
        + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        + "\n人物谱准确姓名使用 bible:<姓名>。"
        + model_identity_authority_prompt_rule()
        + "除 source_text 等逐字原文证据外，所有展示姓名必须使用对应 canonical_name。"
        + "后续章节只用于确认身份，严禁在本集泄露其剧情。"
    )


def _narrative_plan_schema_example(
    scope_id: str,
    *,
    source_chapter_ids: list[str] | None = None,
) -> str:
    """Render the complete narrative-plan JSON surface without story templates.

    Values are ID/relationship examples only.  The surrounding prompt explicitly
    forbids copying audience categories or content from this example; every
    semantic value must be inferred from the current source and adaptation.
    """
    example_chapter_id = (
        source_chapter_ids[0] if source_chapter_ids else "current-source-chapter"
    )
    example = {
        "contract_version": NARRATIVE_CONTRACT_VERSION,
        "scope_id": scope_id,
        "source_evidence": [{
            "source_evidence_id": "SE-1",
            "source_span": {
                "chapter_id": example_chapter_id,
                "start": 0,
                "end": 12,
            },
            "verbatim_excerpt": "从本集授权原文逐字摘录",
            "confidence": 1.0,
        }],
        "identity_contracts": [
            {
                "identity_id": "character-id",
                "display_name": "当前来源与戏剧职责定义的显示名",
                "kind": "由来源证据与本集语义推导的开放身份性质",
                "visual_policy": "contextual",
                "visual_canonical": "足以跨镜识别当前身份的中性视觉锚点",
                "asset_requirement": "optional",
                "voice_ids": ["voice-id"],
                "evidence": {
                    "source_evidence_ids": ["SE-1"],
                    "proposition_ids": ["P-SOURCE-1", "P-ADAPTED-1"],
                    "adaptation_decision_ids": ["AD-1"],
                    "rationale": "为什么该身份及其视觉、资产、声音策略对本集是必要且充分的",
                },
            },
            {
                "identity_id": "entity-id",
                "display_name": "当前叙事中被动作或状态引用的实体名",
                "kind": "由当前命题与作用关系推导的开放实体性质",
                "visual_policy": "contextual",
                "visual_canonical": "足以识别当前实体与其状态变化的视觉锚点",
                "asset_requirement": "optional",
                "voice_ids": [],
                "evidence": {
                    "source_evidence_ids": ["SE-1"],
                    "proposition_ids": ["P-SOURCE-1", "P-ADAPTED-1"],
                    "adaptation_decision_ids": ["AD-1"],
                    "rationale": "该实体被本集命题、事实或动作实际引用的证据理由",
                },
            },
        ],
        "propositions": [
            {
                "proposition_id": "P-SOURCE-1",
                "semantic_identity_key": "当前项目内该原文命题的语义身份键",
                "canonical_statement": "不可再拆的原文命题",
                "narrative_domain": "source_canon",
                "entity_ids": ["entity-id", "character-id"],
                "direct_source_evidence_ids": ["SE-1"],
                "domain_truth_status": "true",
            },
            {
                "proposition_id": "P-ADAPTED-1",
                "semantic_identity_key": "当前项目内该改编命题的语义身份键",
                "canonical_statement": "改编后的不可再拆命题",
                "narrative_domain": "adapted_story",
                "entity_ids": ["entity-id", "character-id"],
                "direct_source_evidence_ids": [],
                "domain_truth_status": "true",
            },
        ],
        "adaptation_decisions": [{
            "adaptation_decision_id": "AD-1",
            "source_proposition_ids": ["P-SOURCE-1"],
            "adapted_proposition_ids": ["P-ADAPTED-1"],
            "relation": "preserve",
            "custom_relation": None,
            "creative_reason": "本集改编理由",
            "protected_causal_effect_ids": ["P-ADAPTED-1"],
            "affected_event_ids": ["E-1", "E-2"],
            "uncertainty": None,
        }],
        "state_facts": [
            {
                "fact_id": "F-1",
                "proposition_id": "P-ADAPTED-1",
                "subject_id": "entity-id",
                "predicate_id": "project-semantic-predicate-id",
                "value": {"kind": "text", "data": "事件前状态"},
                "time_scope": "main@1",
                "visibility": "visible",
                "provenance": "screenplay",
                "confidence": 1.0,
            },
            {
                "fact_id": "F-2",
                "proposition_id": "P-ADAPTED-1",
                "subject_id": "entity-id",
                "predicate_id": "project-semantic-predicate-id",
                "value": {"kind": "text", "data": "原因事件完成后、结果行动前的状态"},
                "time_scope": "main@2",
                "visibility": "visible",
                "provenance": "screenplay",
                "confidence": 1.0,
            },
            {
                "fact_id": "F-3",
                "proposition_id": "P-ADAPTED-1",
                "subject_id": "entity-id",
                "predicate_id": "project-semantic-predicate-id",
                "value": {"kind": "text", "data": "结果行动完成后的状态"},
                "time_scope": "main@3",
                "visibility": "visible",
                "provenance": "screenplay",
                "confidence": 1.0,
            },
        ],
        "initial_state_fact_ids": ["F-1"],
        "evidence": [
            {
                "evidence_id": "EV-1",
                "anchor": {"type": "event", "id": "E-1"},
                "observable_claim": "执行者与观众在原因事件当下实际可感知的内容",
                "perceivable_by": ["character-id", "audience"],
                "supports_proposition_ids": ["P-ADAPTED-1"],
                "planned_salience": 0.8,
                "planned_duration_s": 1.5,
                "competing_attention_ids": [],
            },
            {
                "evidence_id": "EV-2",
                "anchor": {"type": "event", "id": "E-2"},
                "observable_claim": "观察者可核对结果行动已完成",
                "perceivable_by": ["character-id", "audience"],
                "supports_proposition_ids": ["P-ADAPTED-1"],
                "planned_salience": 0.7,
                "planned_duration_s": 0.5,
                "competing_attention_ids": [],
            },
        ],
        "dramatic_questions": [{
            "dramatic_question_id": "DQ-1",
            "question_text": "观众此时应追问的问题",
            "target_proposition_ids": ["P-ADAPTED-1"],
            "open_anchor": {"type": "event", "id": "E-1"},
            "intended_resolution_scope_id": scope_id,
            "desired_state_while_open": "unknown",
            "resolution_anchor": None,
            "status": "open",
        }],
        "events": [
            {
                "event_id": "E-1",
                "proposition_ids": ["P-ADAPTED-1"],
                "causal_parent_ids": [],
                "precondition_fact_ids": ["F-1"],
                "action_ids": [],
                "effects_add": ["F-2"],
                "effects_remove": ["F-1"],
                "character_goal_effects": [],
                "downstream_dependency_event_ids": ["E-2"],
                "salience": 0.8,
                "irreversibility": 0.5,
                "must_keep": True,
                "delivery_scope_id": scope_id,
                "delivery_policy": "deliver",
                "primary_delivery_window_id": "RW-1",
            },
            {
                "event_id": "E-2",
                "proposition_ids": ["P-ADAPTED-1"],
                "causal_parent_ids": ["E-1"],
                "precondition_fact_ids": ["F-2"],
                "action_ids": ["A-1"],
                "effects_add": ["F-3"],
                "effects_remove": ["F-2"],
                "character_goal_effects": [],
                "downstream_dependency_event_ids": [],
                "salience": 0.8,
                "irreversibility": 0.6,
                "must_keep": True,
                "delivery_scope_id": scope_id,
                "delivery_policy": "deliver",
                "primary_delivery_window_id": "RW-2",
            },
        ],
        "atomic_actions": [{
            "action_id": "A-1",
            "actor_ids": ["character-id"],
            "target_ids": ["entity-id"],
            "participant_deliveries": [],
            "semantic_intent": "该动作在故事中完成什么",
            "precondition_fact_ids": ["F-2"],
            "effects_add": ["F-3"],
            "effects_remove": ["F-2"],
            "completion_condition": "观察者可验证的完成条件",
            "decision_requirement": "applies",
            "decision_not_applicable_reason": None,
            "temporal_phases": [{
                "phase_id": "A-1/P1",
                "start_condition": "开始条件",
                "end_condition": "结束条件",
                "estimated_min_s": 1.0,
            }],
            "splittable_boundaries": ["A-1/P1"],
        }],
        "action_relation_audits": [],
        "character_states": [{
            "character_state_id": "CDS-1",
            "character_id": "character-id",
            "anchor": {"type": "event", "id": "E-1"},
            "goal_proposition_ids": ["P-ADAPTED-1"],
            "stakes_proposition_ids": [],
            "relationship_state": {},
            "emotion": {"label": "自由语义", "intensity": 0.5, "observable_evidence": ["EV-1"]},
            "pressure": 0.5,
            "tactic": "当前手段",
        }],
        "character_beliefs": [{
            "character_belief_id": "CB-1",
            "character_id": "character-id",
            "anchor": {"type": "event", "id": "E-1"},
            "perceived_evidence_ids": ["EV-1"],
            "beliefs": [{
                "proposition_id": "P-ADAPTED-1",
                "stance": "suspected",
                "confidence": 0.6,
                "evidence_ids": ["EV-1"],
            }],
            "misbelief_proposition_ids": [],
            "decision_proposition_ids": ["P-ADAPTED-1"],
            "decision_basis_ids": ["EV-1"],
            "decision_action_ids": ["A-1"],
        }],
        "audience_priors": [
            {
                "audience_prior_id": "AP-1",
                "scope_id": scope_id,
                "audience_description": "由当前项目语义推导的一次观看先验 A",
                "assumed_known_proposition_ids": [],
                "assumed_unknown_proposition_ids": ["P-ADAPTED-1"],
                "familiarity_assumptions": [],
                "language_and_context_assumptions": [],
                "attention_memory_assumptions": {},
                "calibration_source": "needs_review",
            },
            {
                "audience_prior_id": "AP-2",
                "scope_id": scope_id,
                "audience_description": "与 A 具有不同已知命题或记忆条件的当前项目先验 B",
                "assumed_known_proposition_ids": ["P-ADAPTED-1"],
                "assumed_unknown_proposition_ids": [],
                "familiarity_assumptions": [],
                "language_and_context_assumptions": [],
                "attention_memory_assumptions": {},
                "calibration_source": "needs_review",
            },
        ],
        "audience_states": [
            {
                "audience_state_id": "AS-AP1-IN",
                "audience_prior_id": "AP-1",
                "anchor": {"type": "event", "id": "E-1"},
                "beliefs": [{
                    "proposition_id": "P-ADAPTED-1",
                    "stance": "unknown",
                    "confidence": 0.0,
                    "evidence_ids": [],
                }],
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": [{"proposition_id": "P-ADAPTED-1", "retention_confidence": 0.7}],
                "attention_residue_ids": [],
                "affective_state": {},
            },
            {
                "audience_state_id": "AS-AP1-OUT",
                "audience_prior_id": "AP-1",
                "anchor": {"type": "event", "id": "E-1"},
                "beliefs": [{
                    "proposition_id": "P-ADAPTED-1",
                    "stance": "suspected",
                    "confidence": 0.6,
                    "evidence_ids": ["EV-1"],
                }],
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": [{"proposition_id": "P-ADAPTED-1", "retention_confidence": 0.7}],
                "attention_residue_ids": [],
                "affective_state": {},
            },
            {
                "audience_state_id": "AS-AP2-IN",
                "audience_prior_id": "AP-2",
                "anchor": {"type": "event", "id": "E-1"},
                "beliefs": [],
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": [{"proposition_id": "P-ADAPTED-1", "retention_confidence": 0.7}],
                "attention_residue_ids": [],
                "affective_state": {},
            },
            {
                "audience_state_id": "AS-AP2-OUT",
                "audience_prior_id": "AP-2",
                "anchor": {"type": "event", "id": "E-1"},
                "beliefs": [],
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": [{"proposition_id": "P-ADAPTED-1", "retention_confidence": 0.7}],
                "attention_residue_ids": ["DQ-1"],
                "affective_state": {},
            },
        ],
        "experience_intents": [{
            "experience_intent_id": "XI-1",
            "scope_id": scope_id,
            "anchor_event_ids": ["E-1"],
            "director_objective": "这一段希望观众经历的状态变化",
            "attention_target_ids": ["P-ADAPTED-1"],
            "audience_paths": [
                {
                    "audience_path_id": "XP-AP1-1",
                    "audience_prior_id": "AP-1",
                    "audience_state_in_id": "AS-AP1-IN",
                    "audience_state_out_target_id": "AS-AP1-OUT",
                    "target_deltas": [{
                        "target_delta_id": "XD-AP1-1",
                        "dimension": "belief",
                        "proposition_ids": ["P-ADAPTED-1"],
                        "description": "该先验观众需要发生的状态差",
                        "from_state": {"stance": "unknown", "confidence": 0.0},
                        "to_state": {"stance": "suspected", "confidence": 0.6},
                        "target_confidence": 0.6,
                        "required_processing_s": 1.0,
                        "deadline_event_id": "E-2",
                        "primary_delivery_window_id": "RW-1",
                        "custom_dimension": None,
                    }],
                },
                {
                    "audience_path_id": "XP-AP2-1",
                    "audience_prior_id": "AP-2",
                    "audience_state_in_id": "AS-AP2-IN",
                    "audience_state_out_target_id": "AS-AP2-OUT",
                    "target_deltas": [{
                        "target_delta_id": "XD-AP2-1",
                        "dimension": "attention",
                        "proposition_ids": ["P-ADAPTED-1"],
                        "description": "该先验观众需要把注意集中到仍未解决的问题",
                        "from_state": {"attention_residue_ids": []},
                        "to_state": {"attention_residue_ids": ["DQ-1"]},
                        "target_confidence": None,
                        "required_processing_s": 0.5,
                        "deadline_event_id": "E-2",
                        "primary_delivery_window_id": "RW-1",
                        "custom_dimension": None,
                    }],
                },
            ],
            "withheld_propositions": [],
            "forbidden_misconceptions": [],
        }],
        "assimilation_tasks": [{
            "assimilation_task_id": "AT-1",
            "experience_intent_id": "XI-1",
            "audience_path_id": "XP-AP1-1",
            "target_delta_id": "XD-AP1-1",
            "required_prior_proposition_ids": [],
            "downstream_dependency_event_ids": ["E-2"],
            "satisfaction_criteria": "可由冷观众观察验证的达成条件",
            "status": "planned",
        }],
        "readability_windows": [
            {
                "readability_window_id": "RW-1",
                "event_ids": ["E-1"],
                "proposition_ids": ["P-ADAPTED-1"],
                "target_delta_ids": ["XD-AP1-1", "XD-AP2-1"],
                "shot_ids": [],
                "attention_target_ids": ["P-ADAPTED-1"],
                "evidence_ids": ["EV-1"],
                "scheduled_processing_s": 1.0,
                "planned_available_s": 1.0,
                "competing_attention_ids": [],
                "readability_reason": "在下游事件使用前交付证据并留出逐先验处理时间",
                "status": "planned",
            },
            {
                "readability_window_id": "RW-2",
                "event_ids": ["E-2"],
                "proposition_ids": ["P-ADAPTED-1"],
                "target_delta_ids": [],
                "shot_ids": [],
                "attention_target_ids": ["P-ADAPTED-1"],
                "evidence_ids": ["EV-2"],
                "scheduled_processing_s": 0.5,
                "planned_available_s": 0.5,
                "competing_attention_ids": [],
                "readability_reason": "让行动完成条件在切离前可观察",
                "status": "planned",
            },
        ],
        "setup_payoff_contracts": [{
            "setup_payoff_id": "SP-1",
            "setup_proposition_ids": ["P-ADAPTED-1"],
            "setup_event_ids": ["E-1"],
            "payoff_event_ids": ["E-2"],
            "intended_inference_ids": ["P-ADAPTED-1"],
            "retention_deadline_event_id": "E-2",
            "minimum_retention_confidence": 0.5,
            "recall_needed": False,
            "status": "paid_off",
        }],
        "scene_contracts": [{
            "scene_id": "SC01",
            "applicability": "applies",
            "not_applicable_reason": None,
            "alternative_dramatic_function": None,
            "scene_question_id": "DQ-1",
            "point_of_view_character_id": "character-id",
            "audience_state_paths": [
                {"audience_prior_id": "AP-1", "audience_state_in_id": "AS-AP1-IN", "audience_state_out_target_id": "AS-AP1-OUT"},
                {"audience_prior_id": "AP-2", "audience_state_in_id": "AS-AP2-IN", "audience_state_out_target_id": "AS-AP2-OUT"},
            ],
            "character_state_in_ids": ["CDS-1"],
            "goal_proposition_ids": ["P-ADAPTED-1"],
            "obstacle_proposition_ids": ["P-ADAPTED-1"],
            "stakes_proposition_ids": ["P-ADAPTED-1"],
            "pressure_curve": [{"anchor": {"type": "event", "id": "E-1"}, "value": 0.5}],
            "turn_event_ids": ["E-2"],
            "value_polarity_in": "入场价值",
            "value_polarity_out": "离场价值",
            "relationship_deltas": [],
            "character_state_out_ids": ["CDS-1"],
            "scene_button": "场景结束时交给下一场的决定、问题或冲击",
        }],
        "arc_contracts": [{
            "arc_id": "ARC-EPISODE",
            "scope": "episode",
            "applicability": "applies",
            "not_applicable_reason": None,
            "alternative_dramatic_function": None,
            "core_question_ids": ["DQ-1"],
            "promise_proposition_ids": ["P-ADAPTED-1"],
            "escalation_event_ids": ["E-1"],
            "climax_event_ids": ["E-2"],
            "payoff_contract_ids": ["SP-1"],
            "pressure_curve": [{"anchor": {"type": "event", "id": "E-1"}, "value": 0.5}],
            "information_density_curve": [{"anchor": {"type": "event", "id": "E-1"}, "value": 0.5}],
            "processing_beats": [{"anchor": {"type": "event", "id": "E-1"}, "purpose": "消化、停顿或转向"}],
            "ending_hook_question_ids": [],
            "resolved_question_ids": [],
            "carried_question_ids": ["DQ-1"],
        }],
    }
    return json.dumps(example, ensure_ascii=False, separators=(",", ":"))


def _narrative_plan_prompt_block(scope_id: str) -> str:
    return (
        "【全链路叙事连续性合同·剧本发布硬门禁】\n"
        "顶层必须输出 narrative_plan，并且它是下游分镜与冷观众审读的唯一叙事权威：\n"
        "1. 先从授权原文逐字抽取 SourceEvidence，再建 source_canon 命题；"
        "SourceSpan.start/end 必须精确切出 verbatim_excerpt，不得只让摘录在原文某处出现；"
        "SourceSpan.chapter_id 只能使用本次输入明确列出的授权章节 ID；"
        "改编命题必须属于 adapted_story，通过 AdaptationDecision 连接，"
        "实质改写后不得继承原文 direct_source_evidence_ids。命题 entity_ids 合并构成本作用域身份图，"
        "fact.subject_id、action actor/target、人物状态与可感知者都必须引用其中身份。\n"
        "系统保留的 environment:<episode-scope> 仅由 deterministic compiler 为真正无人物的环境状态建立，"
        "模型不得输出或把它当作 identity_contract、声音、可见人物、动作参与者或 POV。\n"
        "1a. 每条命题必须填写 semantic_identity_key，键由当前项目语义归一产生，不来自全局词表。"
        "同一 narrative_domain 中语义等价的陈述必须共用同一键，并最终只保留一个 proposition_id；"
        "不得通过同义改写、语序变化或更换 ID 重复创建同一命题。来源域与改编域可以分别拥有自己的键，"
        "但必须通过 AdaptationDecision 连接。\n"
        "1b. identity_contracts 是非角色圣经身份的唯一权威注册表。identity_id 供命题、事实、"
        "动作、状态与信念精确引用，display_name 供场次、剧本正文与对白精确引用；kind 是基于当前"
        "来源和戏剧职责的开放语义，不得使用姓名、称谓或题材白名单判定。无论具名新角色、"
        "一次性功能身份、群体身份或画外说话人，都必须由当前语义意图选择 visual_policy："
        "canonical 表示需持久定妆且 asset_requirement=required；contextual 表示仅在当前上下文保持识别；"
        "collective 用群体构成锚点而非伪造单一人物；offscreen_only 表示纯画外且必须 "
        "asset_requirement=forbidden。除 offscreen_only 外 visual_canonical 必填，canonical 必须 required。"
        "voice_ids 必须精确回指 voice_bible.speaker_id；Bible 已有角色的 speaker_id 必须直接使用"
        "人物谱准确姓名，禁止另造 V-MH 一类声音别名；非 Bible 身份才通过 identity_contract.voice_ids 连接。"
        "evidence 必须以 source_evidence_ids、proposition_ids、"
        "adaptation_decision_ids 和 rationale 说明身份决策。除纯旁白可仅由 voice_bible.role_type=narrator 表达外，"
        "任何未在角色圣经中的可见身份或说话人，以及任何进入 identity/entity、scene characters、dialogue speaker"
        "或 information speaker 的身份，都必须先有完整合同；未声明身份不得进入剧本或分镜。\n"
        "2. events 必须按因果拓扑顺序排列：每个 causal_parent_id 位于当前事件之前，"
        "每个 downstream_dependency_event_id 位于其后，全图无环。只有 initial_state_fact_ids 中显式列出的 StateFact "
        "可作为初始成立集逐事件重放；其他事实必须有唯一 effects_add 生产者。precondition 和 effects_remove 必须当下成立，"
        "effects_add 必须当下未成立；不得从未来取前置事实、重复建立已成立事实或无效移除。\n"
        "3. AtomicAction 必须有执行者或作用对象、语义意图、可观察完成条件与唯一阶段 ID；"
        "effects_add/effects_remove 不得重叠。事件引用某动作时，事件的 precondition_fact_ids、effects_add、"
        "effects_remove 必须分别完整覆盖该动作的同名集合，不得只写动作摘要却丢掉状态效果。"
        "当 action actor/target 不在 owner event.onscreen_entity_ids 中时，必须在该动作的 "
        "participant_deliveries 中按 action_id+participant_id 绑定专属 NarrativeEvidence.evidence_id，"
        "并以 audible、visible_effect、visible_reaction 布尔字段至少声明一种观众可感知交付；"
        "证据必须锚定 owner event 且 perceivable_by 含 audience。"
        "对所有不同 ID 但主体、目标、前置、效果、完成条件高度等价或语义同义的动作，"
        "必须输出 ActionSemanticRelationAudit；功能性重复只有在后一事件因果依赖前一事件，且绑定"
        "新 target_delta、人物状态或可感知证据时才能保留；不得用动作词表判断。\n"
        "4. NarrativeEvidence 只写锚点当下真正可感知的证据。每个执行状态变化动作的 actor，"
        "在该事件或更早锚点必须同时有 CharacterDramaticState 与 CharacterBeliefSnapshot；"
        "decision_action_ids、decision_proposition_ids 与 decision_basis_ids 必须三者成对非空，并精确授权当前动作；"
        "决策依据只能是角色已感知的证据"
        "或已持有的命题，不得使用角色不可感知/未来证据。\n"
        "5. audience_priors 至少 2 条，必须根据当前项目的已知命题、熟悉度和记忆条件动态推导；"
        "禁止固定人群名单、题材白名单或复制下方示例描述。\n"
        "6. 每个 ExperienceIntent 对每个 audience_prior 恰好一条 audience_path；每条路径有独立入场状态、"
        "结构上真正发生变化的目标状态与 target_deltas。每个 delta 的 dimension 必须在对应 AudienceState "
        "字段中真正改变，from_state 不得等于 to_state；必须填 required_processing_s、"
        "deadline_event_id 和唯一 primary_delivery_window_id，不得用平均观众替代逐先验计算。\n"
        "7. 只在现有证据无法推出目标状态时创建 AssimilationTask；"
        "新事物是否需要桥接由先验差、推断路径、下游依赖、注意竞争和记忆衰减推导，"
        "不按内容类别打补丁。任务必须精确引用 intent/path/delta，并声明可盲审的达成标准及下游依赖；"
        "分镜中只能有一个 shot_contribution 作为任务主交付，且必须在 delta 截止事件与所有下游依赖事件中"
        "最早一个的所在镜或之前完成。\n"
        "8. 为必交付事件与目标变化创建 ReadabilityWindow；剧本阶段 shot_ids 可为空。"
        "每个窗口先按 audience_prior 分组求和其 target_deltas.required_processing_s，"
        "scheduled_processing_s 必须不小于各先验分组和的最大值，且 planned_available_s 不小于 scheduled_processing_s。"
        "deliver+must_keep 事件和每个 target_delta 都必须反向引用唯一主窗口，窗口 event_ids/target_delta_ids "
        "也必须回引它们。\n"
        "9. 同时输出 SceneDramaticContract、NarrativeArcContract 和 SetupPayoffContract；"
        "SceneDramaticContract 必须与最终 scene_outline 一一对应，数量完全相同，"
        "按 scene_no 使用 SC01、SC02……连续 ID；即使两个场次使用同一物理地点，"
        "只要中间发生过时间、地点、目标或连续动作切换，就必须保留为两个独立场次合同。"
        "非传统段落可设 not_applicable，但必须给出替代戏剧功能，不得强套模板。"
        "每个 arc.promise_proposition_ids 必须来自该 arc.payoff_contract_ids 所引用合同的 "
        "setup_proposition_ids；payoff_event_ids 表示兑现事件，intended_inference_ids 表示兑现后推论，"
        "SetupPayoffContract 不存在 payoff_proposition_ids 字段，不得把末端推论误写成 arc promise。\n"
        "10. 所有 ID 必须唯一且引用存在；AI 不确定时使用 uncertainty/needs_review，"
        "禁止用剧情关键词、动作词表、集数特判或内容类别到修复方案的固定映射。\n"
        "narrative_plan 的完整 JSON 字段与关系结构见文末输出 Schema；"
        "其中示例值只说明引用关系，严禁复制为本集语义。"
    )


async def _repair_narrative_blueprint(
    blueprint: NarrativeBlueprint,
    *,
    episode: dict[str, Any],
    source_text: str,
    additional_errors: list[str] | None = None,
    generation_budget: _BlueprintGenerationBudget | None = None,
) -> NarrativeBlueprint:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    parent_artifact_ids: list[str] = []
    pending_external_errors = list(additional_errors or [])
    undelivered_patch_errors: list[str] = []

    def normalize_and_validate() -> list[str]:
        normalize_blueprint_state_subject_perception(blueprint)
        normalize_blueprint_requirement_state_keys(blueprint)
        return validate_narrative_blueprint(blueprint, source_text)

    for round_no in range(1, 7):
        normalize_blueprint_agency_continuity(blueprint)
        errors = normalize_and_validate() + pending_external_errors
        if not errors:
            trace = current_trace()
            evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint",
                    scope_type="episode",
                    scope_id=str(episode.get("id") or ""),
                    status="validated",
                    trust_level="T1",
                    content=blueprint.model_dump(mode="json"),
                    parent_artifact_ids=parent_artifact_ids,
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    model_snapshot=_current_blueprint_authority_snapshot(
                        source_text,
                        generation_mode="semantic_repair",
                        generation_budget=generation_budget,
                    ),
                ),
                step_run_id=trace.step_run_id,
            )
            return blueprint
        error_text = "\n".join(errors)
        mentioned_keys = {
            node.key
            for node in blueprint.nodes
            if node.key and node.key in error_text
        }
        selected_indexes = {
            neighbor
            for index, node in enumerate(blueprint.nodes)
            if node.key in mentioned_keys
            for neighbor in range(
                max(0, index - 2),
                min(len(blueprint.nodes), index + 3),
            )
        }
        mentioned_source_ids = set(re.findall(
            r"\bSRC\d+\b",
            "\n".join(errors),
        ))
        source_segments = index_source_segments(source_text)
        source_order = {
            segment.segment_id: index
            for index, segment in enumerate(source_segments)
        }
        for source_id in mentioned_source_ids:
            target_position = source_order.get(source_id)
            if target_position is None:
                continue
            nearest_index = min(
                range(len(blueprint.nodes)),
                key=lambda index: min(
                    (
                        abs(
                            source_order[owned_source_id]
                            - target_position
                        )
                        for owned_source_id
                        in blueprint.nodes[index].source_segment_ids
                        if owned_source_id in source_order
                    ),
                    default=len(source_segments),
                ),
            )
            selected_indexes.update(range(
                max(0, nearest_index - 2),
                min(len(blueprint.nodes), nearest_index + 3),
            ))
        open_flashback_depth = 0
        for node in blueprint.nodes:
            if node.time_relation == "flashback_enter":
                open_flashback_depth += 1
            elif node.time_relation == "flashback_exit":
                open_flashback_depth = max(0, open_flashback_depth - 1)
        if not selected_indexes and open_flashback_depth and blueprint.nodes:
            flashback_enter_indexes = [
                index
                for index, node in enumerate(blueprint.nodes)
                if node.time_relation == "flashback_enter"
            ]
            if flashback_enter_indexes:
                enter_index = flashback_enter_indexes[-1]
                selected_indexes.update(range(
                    max(0, enter_index - 1),
                    min(len(blueprint.nodes), enter_index + 2),
                ))
            selected_indexes.update(range(
                max(0, len(blueprint.nodes) - 3),
                len(blueprint.nodes),
            ))
        if not selected_indexes:
            raise ValueError(
                "蓝图错误无法映射到可局部替换的时间线节点："
                + "；".join(errors[:10])
            )
        selected_nodes = [
            blueprint.nodes[index].model_dump(mode="json")
            for index in sorted(selected_indexes)
        ]
        selected_node_keys = [
            str(node["key"]) for node in selected_nodes
        ]
        selected_source_ids = {
            str(source_id)
            for node in selected_nodes
            for source_id in node["source_segment_ids"]
        }
        selected_source_facts = [
            fact.model_dump(mode="json")
            for fact in source_facts(source_text)
            if fact.source_segment_id in selected_source_ids
        ]
        projection_contract = {
            node.key: node.source_semantics().projection_policy
            for node in blueprint.nodes
            if node.key in set(selected_node_keys)
        }
        patch_schema = blueprint_patch_schema(
            blueprint,
            selected_node_keys,
        )
        node_index = [
            {
                "key": node.key,
                "summary": node.summary,
                "time": node.time_label,
                "location": node.location_label,
            }
            for node in blueprint.nodes
        ]
        repair_prompt = (
            "只局部修复叙事蓝图的硬门禁问题，禁止重写整份蓝图。"
            "replacements 中只输出需要修改的完整 node，并使用 node 字段。"
            "每个 replacement 必须与原节点一对一，完整保持 canonical key、"
            "source_segment_ids 的集合与顺序，以及"
            "narrative_layer/event_priority/render_policy 语义三元。"
            "禁止拆分、合并、新增、删除或重排 timeline node；"
            "delete_node_keys 必须为空。"
            "允许修正时间关系、转场、状态事实引用、决定、行为自主性和"
            " released_constraints_for、participant_evidence。每个 replacement node "
            "必须显式保留或修正"
            "除上述 canonical authority 字段外的创作与分场字段。"
            "每个 replacement 必须保持修复前 projection_policy；audit_only "
            "节点与来源只能保留在来源审计，不得改成 story 或放入 scene。"
            "paratext/audit_only replacement 必须把 participants、"
            "participant_evidence、state_subject_assignments、"
            "environment_source_unit_keys、"
            "source_unit_deliveries、state_requirements、state_changes、"
            "released_constraints_for 全部保持为空列表，decision=null，"
            "exit_state=空字符串；标题卡可见文字只写入 summary/"
            "action_logic/opening_image，不得伪造 written_text delivery。"
            "不得修改未列出的节点。原文没有的同谋、"
            "关系、满房、行程或人物动机默认禁止；若为修复原文自身的明确逻辑矛盾"
            "确有必要，必须设 adaptation_kind=logic_bridge，并用 bridge_rationale"
            "说明为何不改变核心事件与结果。已有住宿、车辆、关系等持久事实必须"
            "继续有效，禁止用锁门、系统错误、被占用等新理由让它失效；若剧情需要"
            "临时空间，应在更早的相关节点建立属于其他人物的独立资源，再说明角色"
            "为何临时使用该资源。被迫决定必须建立"
            " constraint_fact_key；只有后续节点用 supersedes_fact_keys 终止"
            "该约束事实后，才能写 released_constraints_for。快感或停止反抗"
            "不能解除威胁。若 setup_missing 涉及原文明确写出的既有关系或人物，"
            "禁止删除、弱化该来源事实；必须在当前节点或更早节点增加可见/可听的"
            "身份与关系建立内容，同节点先建立再引用也有效。\n\n"
            "若硬门禁是 source_delivery 或 voice_identity 问题，只能在保持完整"
            "node、source_segment_ids 顺序和来源语义的前提下，修正"
            "source_unit_deliveries 与 participant_evidence：仅story/picture节点的"
            " projection=quoted "
            "单元必须先根据来源语义明确选择 spoken_dialogue、offscreen_voice、"
            "written_text、sound_effect 或 unspoken_reference。只有声音交付才允许"
            "恰有一个 usage=voice，并用 source_unit_keys 精确引用且与 performer_key "
            "一致；引号只是句法边界，不得自动视为对白。story/picture的"
            " projection=action 正文以及节点的"
            "summary/action_logic 即使写有‘旁白’或‘介绍’，也不得被提升为 dialogue "
            "或伪造 voice evidence；不得删除、拆分、合并或重排节点来规避。\n\n"
            "每个story/picture节点中 projection=action 的 prose source unit 必须三选一："
            "人物思考、反应、发问或动作使用一条 usage=state_subject "
            "participant_evidence，并用 source_unit_keys 精确绑定该 unit；"
            "结构标点切分后仍不可拆的共同动作使用一条 mode=joint 的"
            "state_subject_assignments，identity_keys列出全部共同主体；"
            "真正无人物状态所有者的环境变化才把该 unit 写入 "
            "environment_source_unit_keys。visible 、场次 roster、文本姓名和"
            "content_owner 均不能推断状态主体。repair 只允许补此证据，"
            "不得改 timeline/node/source ownership/audit_only 语义。"
            "paratext/audit_only的quoted/action unit均不适用这两条规则。\n\n"
            "硬门禁：\n"
            + "\n".join(f"- {error}" for error in errors)
            + "\n\n相关节点及前后文：\n"
            + json.dumps(
                selected_nodes,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n门禁提到的缺失来源原文：\n"
            + json.dumps(
                [
                    {
                        "source_segment_id": segment.segment_id,
                        "text": segment.text,
                    }
                    for segment in source_segments
                    if segment.segment_id in mentioned_source_ids
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n相关来源的机器结构化 units（唯一 voice 判定权威）：\n"
            + json.dumps(
                selected_source_facts,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n全篇节点索引（仅用于引用）：\n"
            + json.dumps(
                node_index,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n输出 Schema：\n"
            + json.dumps(
                patch_schema,
                ensure_ascii=False,
            )
        )
        trace = current_trace()
        raw_artifact_ids: list[str] = []

        def record_patch_attempt(attempt: dict[str, Any]) -> None:
            raw_artifact = evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint_patch_raw",
                    scope_type="episode",
                    scope_id=str(episode.get("id") or ""),
                    status="candidate",
                    trust_level="T0",
                    content={
                        "raw_output": str(attempt.get("raw_response") or ""),
                        "round": round_no,
                        "outcome": str(attempt.get("outcome") or ""),
                        "format_attempt": int(attempt.get("format_attempt") or 0),
                        "semantic_attempt": int(
                            attempt.get("semantic_attempt") or 0
                        ),
                    },
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                ),
                step_run_id=trace.step_run_id,
            )
            raw_artifact_ids.append(str(raw_artifact["id"]))

        repair_input_hash = hashlib.sha256(
            json.dumps(
                {
                    "blueprint_hash": _narrative_blueprint_content_hash(blueprint),
                    "errors": errors,
                    "selected_node_keys": [
                        node.get("key") for node in selected_nodes
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        requested_max_tokens = 16384
        patch_messages = [
            {"role": "system", "content": SYSTEM_PREFIX},
            {"role": "user", "content": repair_prompt},
        ]
        operation_id, effective_max_tokens = (
            _blueprint_structured_operation_id(
                operation_kind="patch",
                episode_id=str(episode.get("id") or ""),
                semantic_input_hash=repair_input_hash,
                ordinal=str(round_no),
                messages=patch_messages,
                output_schema=patch_schema,
                requested_max_tokens=requested_max_tokens,
                temperature=0.1,
            )
        )
        reservation_id: int | None = None
        remaining_seconds: float | None = None
        legacy_retry_call_id: int | None = None
        if generation_budget is not None:
            legacy_retry_call_id = generation_budget.explicit_retry_call_id(
                "screenplay_blueprint_patch"
            )
            reservation_id = generation_budget.claim(
                max_tokens=effective_max_tokens,
                requested_max_tokens=requested_max_tokens,
                operation_id=operation_id,
            )
            remaining_seconds = generation_budget.remaining_seconds()
        structured_call = model_gateway.chat_structured(
            patch_messages,
            model_type=NarrativeBlueprintPatch,
            validate=lambda value: (
                validate_narrative_blueprint_patch_projection(
                    value,
                    blueprint,
                )
            ),
            operation_id=operation_id,
            temperature=0.1,
            max_tokens=requested_max_tokens,
            # A Blueprint patch participates in the same global call/token
            # budget as its leaves.  An implicit format retry would be a
            # second paid request without a second reservation.
            format_retry_limit=(
                0
                if generation_budget is not None
                else int(get_setting("screenplay_format_retry_limit") or 1)
            ),
            semantic_retry_limit=0,
            call_meta={
                "stage": "剧本蓝图局部语义修复",
                "stage_key": "screenplay_blueprint_patch",
                "call_role": "stage_repair",
                "call_role_label": "蓝图局部语义修复",
                "repair_round": round_no,
                "supersedes_provider_call_id": legacy_retry_call_id,
                "episode_id": str(episode.get("id") or ""),
                "production_grant_id": (
                    generation_budget.retry_grant_id
                    if generation_budget is not None else ""
                ),
                "contract_version": BLUEPRINT_VERSION,
                "expected_json": True,
                "reuse_successful_operation": True,
                "require_cached_successful_operation": bool(
                    generation_budget is not None
                    and operation_id
                    in generation_budget._durable_successful_operations
                ),
                "disable_reasoning_fallback": True,
                "disable_provider_retries": True,
                "disable_provider_candidate_fallback": True,
            },
            repair_context=json.dumps(
                {
                    "replaceable_node_keys": selected_node_keys,
                    "projection_contract": projection_contract,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            output_schema=patch_schema,
            on_attempt=record_patch_attempt,
            usage_callback=(
                None
                if reservation_id is None
                else lambda usage_event: generation_budget.record_usage(
                    reservation_id,
                    usage_event,
                )
            ),
        )
        try:
            patch = (
                await structured_call
                if remaining_seconds is None
                else await asyncio.wait_for(
                    structured_call,
                    timeout=max(0.001, remaining_seconds),
                )
            )
        except hiagent.ProviderError as exc:
            if reservation_id is not None:
                generation_budget.settle(
                    reservation_id,
                    unreported_outcome=(
                        "not_sent"
                        if exc.delivery_state == "not_sent"
                        and exc.replay_safe
                        else "unknown"
                    ),
                )
            raise
        except model_gateway.StructuredFormatError as exc:
            if reservation_id is not None:
                generation_budget.settle(reservation_id)
            # A patch that decoded but failed the schema is an answer the
            # provider actually authored: it keeps the strict one-call rule and
            # must never be re-rolled until it happens to pass.  A response that
            # never decoded into a JSON object at all -- in production the keys
            # degenerated into runs of tabs and spaces -- carries no repair to
            # preserve and is simply a round that was never delivered.  This
            # loop already owns a bounded budget of rounds, each with its own
            # reservation and operation id, so spending the next one is the
            # in-contract answer; aborting the episode on round 1 threw the
            # remaining budgeted rounds away.
            if not getattr(exc, "unparseable", False):
                raise
            undelivered_patch_errors.append(str(exc))
            continue
        except BaseException:
            if reservation_id is not None:
                generation_budget.settle(reservation_id)
            raise
        else:
            if reservation_id is not None:
                generation_budget.settle(reservation_id)
        changed = apply_narrative_blueprint_patch(
            blueprint,
            patch,
            allow_source_expansion=False,
            source_text=source_text,
        )
        if not changed:
            raise ValueError("蓝图局部修复没有替换任何节点")
        pending_external_errors = []
        normalized_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint_patch",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="validated",
                trust_level="T1",
                content=patch.model_dump(mode="json"),
                parent_artifact_ids=raw_artifact_ids[-1:],
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot={"replaced_nodes": changed},
            ),
            step_run_id=trace.step_run_id,
        )
        parent_artifact_ids = [normalized_artifact["id"]]
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="candidate",
                trust_level="T1",
                content=blueprint.model_dump(mode="json"),
                parent_artifact_ids=parent_artifact_ids,
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot={
                    "semantic_patch_round": round_no,
                    "remaining_issue_count": len(normalize_and_validate()),
                },
            ),
            step_run_id=trace.step_run_id,
        )

    normalize_blueprint_agency_continuity(blueprint)
    errors = normalize_and_validate()
    if errors:
        raise ContentGenerationError(
            "蓝图局部语义修复六轮后仍未通过："
            + "；".join(errors[:10])
            + (
                f"；其中 {len(undelivered_patch_errors)} 轮未收到可解析的修复响应，"
                f"最近一次：{undelivered_patch_errors[-1]}"
                if undelivered_patch_errors
                else ""
            )
        )
    trace = current_trace()
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="validated",
            trust_level="T1",
            content=blueprint.model_dump(mode="json"),
            parent_artifact_ids=parent_artifact_ids,
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot=_current_blueprint_authority_snapshot(
                source_text,
                generation_mode="semantic_repair",
                generation_budget=generation_budget,
            ),
        ),
        step_run_id=trace.step_run_id,
    )
    return blueprint


def _blueprint_exact_ownership_claims(
    blueprint: NarrativeBlueprint,
    target_unit_keys: list[str],
) -> dict[str, dict[str, Any]]:
    """Project only the ownership fields protected by exact-unit repair."""
    return {
        unit_key: {
            "single": [
                {
                    "node_key": node.key,
                    "identity_key": evidence.identity_key,
                }
                for node in blueprint.nodes
                for evidence in node.participant_evidence
                if (
                    evidence.usage == "state_subject"
                    and unit_key in evidence.source_unit_keys
                )
            ],
            "joint": [
                {
                    "node_key": node.key,
                    "identity_keys": list(assignment.identity_keys),
                }
                for node in blueprint.nodes
                for assignment in node.state_subject_assignments
                if assignment.source_unit_key == unit_key
            ],
            "environment_node_keys": [
                node.key
                for node in blueprint.nodes
                if unit_key in node.environment_source_unit_keys
            ],
            "adjudicated_node_keys": [
                node.key
                for node in blueprint.nodes
                if unit_key in node.state_subject_adjudicated_unit_keys
            ],
        }
        for unit_key in target_unit_keys
    }


async def _repair_reviewed_blueprint_state_subject_ownership(
    blueprint: NarrativeBlueprint,
    *,
    issues: list[Any],
    episode: dict[str, Any],
    source_text: str,
    generation_budget: _BlueprintGenerationBudget | None = None,
) -> tuple[NarrativeBlueprint, str]:
    """Adjudicate consensus environment findings through one exact-only call."""
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    target_unit_keys = list(dict.fromkeys(
        unit_key
        for issue in issues
        for unit_key in issue.source_unit_keys
    ))
    if not target_unit_keys:
        raise ValueError("environment ownership consensus 缺少 exact unit keys")

    patch_schema = blueprint_state_subject_ownership_patch_schema(
        blueprint,
        target_unit_keys,
        source_text,
    )
    facts = source_facts(source_text)
    facts_by_source: defaultdict[str, list[Any]] = defaultdict(list)
    for fact in facts:
        facts_by_source[fact.source_segment_id].append(fact)
    facts_by_key = {fact.source_unit_key: fact for fact in facts}
    nodes_by_source = {
        source_id: node
        for node in blueprint.nodes
        for source_id in node.source_segment_ids
    }
    source_context: dict[str, Any] = {}
    allowed_identities: dict[str, list[str]] = {}
    node_context: dict[str, Any] = {}
    for unit_key in target_unit_keys:
        fact = facts_by_key[unit_key]
        source_group = facts_by_source[fact.source_segment_id]
        fact_index = next(
            index
            for index, candidate in enumerate(source_group)
            if candidate.source_unit_key == unit_key
        )
        owner = nodes_by_source[fact.source_segment_id]
        source_context[unit_key] = {
            "source_fact": fact.model_dump(mode="json"),
            "adjacent_source_units": [
                candidate.model_dump(mode="json")
                for candidate in source_group[
                    max(0, fact_index - 1):fact_index
                ] + source_group[fact_index + 1:fact_index + 2]
            ],
        }
        allowed_identities[unit_key] = [
            identity_key
            for identity_key in owner.participants
            if any(
                evidence.identity_key == identity_key
                and evidence.usage in {"visible", "voice"}
                and fact.source_segment_id in evidence.source_segment_ids
                and (
                    not evidence.source_unit_keys
                    or unit_key in evidence.source_unit_keys
                )
                for evidence in owner.participant_evidence
            )
        ]
        node_context[owner.key] = owner.model_dump(mode="json")

    compact = lambda value: json.dumps(  # noqa: E731
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    repair_prompt = (
        "仅输出 exact-unit state-subject ownership patch JSON，不得输出或改写"
        "完整 Blueprint。repairs 必须恰好覆盖 schema 要求的全部 source unit key。"
        "对每个 target 只依据 source_fact、相邻 source units 与 owning node 的完整"
        "语义，独立选择 single、joint 或 environment；不得按文本关键词、姓名、"
        "内容类别或固定列表判断。single 必须是唯一人物主体，joint 只用于语义上"
        "不可拆的共同主体，environment 只用于确实没有人物状态主体的环境变化。"
        "identity_keys 只能取对应 allowed_identities。除这些 exact target 的"
        "single/joint/environment ownership 外不得修改任何字段。本调用不重试。\n"
        f"base_candidate_hash={patch_schema['properties']['base_candidate_hash']['const']}\n"
        f"review_consensus={compact([issue.model_dump(mode='json') for issue in issues])}\n"
        f"target_source_context={compact(source_context)}\n"
        f"current_ownership={compact(_blueprint_exact_ownership_claims(blueprint, target_unit_keys))}\n"
        f"allowed_identities={compact(allowed_identities)}\n"
        f"owning_nodes={compact(node_context)}\n"
        f"schema={compact(patch_schema)}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PREFIX},
        {"role": "user", "content": repair_prompt},
    ]
    semantic_input_hash = hashlib.sha256(
        json.dumps(
            {
                "blueprint_hash": _narrative_blueprint_content_hash(blueprint),
                "target_unit_keys": target_unit_keys,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    requested_max_tokens = 8192
    operation_id, effective_max_tokens = _blueprint_structured_operation_id(
        operation_kind="review_ownership_patch",
        episode_id=str(episode.get("id") or ""),
        semantic_input_hash=semantic_input_hash,
        ordinal="exact",
        messages=messages,
        output_schema=patch_schema,
        requested_max_tokens=requested_max_tokens,
        temperature=0.1,
    )

    def validate_patch(
        patch: BlueprintStateSubjectOwnershipPatch,
    ) -> list[str]:
        try:
            apply_blueprint_state_subject_ownership_patch(
                blueprint,
                patch,
                target_unit_keys=target_unit_keys,
                source_text=source_text,
            )
        except (TypeError, ValueError) as exc:
            return [str(exc)]
        return []

    reservation_id: int | None = None
    remaining_seconds: float | None = None
    legacy_retry_call_id: int | None = None
    if generation_budget is not None:
        legacy_retry_call_id = generation_budget.explicit_retry_call_id(
            "screenplay_blueprint_patch"
        )
        reservation_id = generation_budget.claim(
            max_tokens=effective_max_tokens,
            requested_max_tokens=requested_max_tokens,
            operation_id=operation_id,
        )
        remaining_seconds = generation_budget.remaining_seconds()

    patch_call = model_gateway.chat_structured(
        messages,
        model_type=BlueprintStateSubjectOwnershipPatch,
        validate=validate_patch,
        operation_id=operation_id,
        temperature=0.1,
        max_tokens=requested_max_tokens,
        format_retry_limit=0,
        semantic_retry_limit=0,
        call_meta={
            "stage": "剧本蓝图精确主体归属裁决",
            "stage_key": "screenplay_blueprint_patch",
            "call_role": "stage_repair",
            "call_role_label": "蓝图精确主体归属裁决",
            "supersedes_provider_call_id": legacy_retry_call_id,
            "episode_id": str(episode.get("id") or ""),
            "production_grant_id": (
                generation_budget.retry_grant_id
                if generation_budget is not None else ""
            ),
            "contract_version": BLUEPRINT_VERSION,
            "expected_json": True,
            "repair_mode": "exact_state_subject_ownership",
            "reuse_successful_operation": True,
            "require_cached_successful_operation": bool(
                generation_budget is not None
                and operation_id
                in generation_budget._durable_successful_operations
            ),
            "disable_reasoning_fallback": True,
            "disable_provider_retries": True,
            "disable_provider_candidate_fallback": True,
        },
        repair_context=compact({
            "target_source_unit_keys": target_unit_keys,
            "allowed_identities": allowed_identities,
        }),
        output_schema=patch_schema,
        usage_callback=(
            None
            if reservation_id is None
            else lambda usage_event: generation_budget.record_usage(
                reservation_id,
                usage_event,
            )
        ),
    )
    try:
        patch = (
            await patch_call
            if remaining_seconds is None
            else await asyncio.wait_for(
                patch_call,
                timeout=max(0.001, remaining_seconds),
            )
        )
    except hiagent.ProviderError as exc:
        if reservation_id is not None:
            generation_budget.settle(
                reservation_id,
                unreported_outcome=(
                    "not_sent"
                    if exc.delivery_state == "not_sent" and exc.replay_safe
                    else "unknown"
                ),
            )
        raise
    except BaseException:
        if reservation_id is not None:
            generation_budget.settle(reservation_id)
        raise
    else:
        if reservation_id is not None:
            generation_budget.settle(reservation_id)

    repaired = apply_blueprint_state_subject_ownership_patch(
        blueprint,
        patch,
        target_unit_keys=target_unit_keys,
        source_text=source_text,
    )
    if not isinstance(repaired, NarrativeBlueprint):
        repaired = NarrativeBlueprint.model_validate(
            repaired.model_dump(mode="json")
        )
    trace = current_trace()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint_ownership_patch",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="validated",
            trust_level="T1",
            content={
                "target_source_unit_keys": target_unit_keys,
                "patch": patch.model_dump(mode="json"),
            },
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot={
                "review_policy_version": (
                    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                ),
                "authority_fingerprint": (
                    blueprint_authority_validator_fingerprint()
                ),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    return repaired, str(artifact["id"])


def _blueprint_semantic_issue_exact_scope(
    issue: Any,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return the exact scope used to bind a review to local authority."""
    return (
        str(issue.code),
        tuple(sorted(str(key) for key in issue.node_keys)),
        tuple(sorted(str(key) for key in issue.source_segment_ids)),
        tuple(sorted(str(key) for key in issue.source_unit_keys)),
    )


def _blueprint_semantic_issue_has_deterministic_authority(
    issue: Any,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> bool:
    """Whether a typed one-sided finding has deterministic local authority.

    The shared validator accepts a reviewer sub-scope when every referenced
    node/source is covered by a server-derived delivery or state-subject issue.
    Its default ``True`` for ordinary craft findings is deliberately fenced
    out here. Environment misclassification is also excluded: that check only
    proves exact-unit scope, not the semantic identity of the state subject.
    """
    code = str(issue.code)
    if (
        code == "state_subject_environment_misclassified"
        or not code.startswith((
            "voice_identity_",
            "source_delivery_",
            "state_subject_",
        ))
    ):
        return False
    return blueprint_semantic_voice_issue_has_dialogue_authority(
        issue,
        blueprint,
        source_text,
    )


async def _semantic_review_narrative_blueprint(
    blueprint: NarrativeBlueprint,
    *,
    episode: dict[str, Any],
    source_text: str,
    generation_budget: _BlueprintGenerationBudget | None = None,
) -> NarrativeBlueprint:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    def persist_reviewed_authority(
        *,
        parent_artifact_ids: list[str] | None = None,
    ) -> None:
        """Persist reviewed authority, then terminalize old unknown retries.

        The artifact commit deliberately happens first.  A crash between the
        two writes leaves the historical provider outcome unresolved (safe);
        the inverse state -- resolving without durable reviewed authority --
        is impossible.
        """
        episode_id = str(episode.get("id") or "")
        trace = current_trace()
        run_id = str(trace.run_id or "")
        if not episode_id or not run_id:
            return
        content = blueprint.model_dump(mode="json")
        content_digest = evidence_repository.content_hash(content)
        source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        existing = get_conn().execute(
            """SELECT id FROM artifacts
                 WHERE type='screenplay_narrative_blueprint'
                   AND scope_type='episode' AND scope_id=?
                   AND status='validated' AND content_hash=?
                   AND contract_version=? AND prompt_version=?
                   AND json_extract(
                       model_snapshot_json,'$.generation_mode'
                   )='semantic_reviewed'
                   AND json_extract(
                       model_snapshot_json,'$.source_corpus_hash'
                   )=?
                   AND json_extract(
                       model_snapshot_json,'$.review_policy_version'
                   )=?
                 ORDER BY created_at DESC LIMIT 1""",
            (
                episode_id,
                content_digest,
                BLUEPRINT_VERSION,
                SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                source_digest,
                BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
            ),
        ).fetchone()
        if existing is None:
            evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint",
                    scope_type="episode",
                    scope_id=episode_id,
                    status="validated",
                    trust_level="T1",
                    content=content,
                    parent_artifact_ids=list(parent_artifact_ids or []),
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    model_snapshot=_current_blueprint_authority_snapshot(
                        source_text,
                        generation_mode="semantic_reviewed",
                        generation_budget=generation_budget,
                    ),
                ),
                step_run_id=trace.step_run_id,
            )

        # Historical unknown provider outcomes are resolved only after this
        # reviewed artifact has been selected as current authority and written
        # into the active revision checkpoint by the downstream boundary.

    initial_blueprint_hash = hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    review_source_corpus_hash = hashlib.sha256(
        source_text.encode("utf-8")
    ).hexdigest()
    review_input_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "episode_id": str(episode.get("id") or ""),
                "blueprint_hash": initial_blueprint_hash,
                "source_corpus_hash": review_source_corpus_hash,
                "review_policy_version": BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
                "authority_fingerprint": blueprint_authority_validator_fingerprint(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cached_rows = get_conn().execute(
        """SELECT id,content_json,content_hash,model_snapshot_json
             FROM artifacts
            WHERE scope_type='episode' AND scope_id=?
              AND type='screenplay_narrative_blueprint_review_consensus'
              AND status='validated'
              AND contract_version=? AND prompt_version=?
            ORDER BY created_at DESC LIMIT 20""",
        (
            str(episode.get("id") or ""),
            BLUEPRINT_VERSION,
            SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        ),
    ).fetchall()
    for row in cached_rows:
        try:
            cached = json.loads(row["content_json"] or "{}")
            if not _artifact_json_content_is_sealed(row, cached):
                continue
            cached_snapshot = json.loads(
                row["model_snapshot_json"] or "{}"
            )
            cached_authoritative_issue_count = int(
                cached.get("authoritative_issue_count")
            )
            cached_residual_issue_count = int(
                cached.get("non_authoritative_residual_issue_count")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        cached_outcome = str(cached.get("review_outcome") or "")
        reusable_no_authority_outcome = bool(
            (
                cached_outcome == "clean"
                and cached_residual_issue_count == 0
            )
            or (
                cached_outcome
                == "non_authoritative_one_sided_residual"
                and cached.get("review_mode") == "full"
                and cached_residual_issue_count > 0
            )
        )
        if (
            cached.get("blueprint_hash") == initial_blueprint_hash
            and not cached.get("consensus_issue_keys")
            and not cached.get("deterministic_authority_issue_keys")
            and cached_authoritative_issue_count == 0
            and reusable_no_authority_outcome
            and cached_snapshot.get("review_policy_version")
            == BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
            and cached_snapshot.get("authority_fingerprint")
            == blueprint_authority_validator_fingerprint()
            and cached_snapshot.get("source_corpus_hash")
            == review_source_corpus_hash
            and cached_snapshot.get("review_input_fingerprint")
            == review_input_fingerprint
        ):
            persist_reviewed_authority(parent_artifact_ids=[str(row["id"])])
            return blueprint

    targeted_review = str(
        get_setting("screenplay_targeted_blueprint_review_enabled") or "true"
    ).strip().lower() not in {"0", "false", "off", "no"}

    def review_projection() -> tuple[dict[str, Any], str, list[str]]:
        nodes = blueprint.nodes
        risky: set[int] = set()
        for index, node in enumerate(nodes):
            previous = nodes[index - 1] if index else None
            if (
                node.time_relation not in {"episode_start", "continuous"}
                or (previous is not None and (
                    node.temporal_domain_key != previous.temporal_domain_key
                    or node.location_key != previous.location_key
                ))
                or (node.decision is not None and node.decision.impact == "major")
                or bool(node.released_constraints_for)
                or bool(node.state_requirements)
                or bool(node.environment_source_unit_keys)
                or node.dramatic_load >= 3
            ):
                risky.add(index)
        if not risky and nodes:
            risky.update({0, len(nodes) - 1})
        selected = {
            neighbor
            for index in risky
            for neighbor in range(max(0, index - 1), min(len(nodes), index + 2))
        }
        selected_nodes = [nodes[index] for index in sorted(selected)]
        selected_keys = {node.key for node in selected_nodes}
        source_ids = list(dict.fromkeys(
            source_id
            for node in selected_nodes
            for source_id in node.source_segment_ids
        ))
        indexed = {
            segment.segment_id: segment.text
            for segment in index_source_segments(source_text)
        }
        projected = {
            "format_version": blueprint.format_version,
            "episode_no": blueprint.episode_no,
            "nodes": [node.model_dump(mode="json") for node in selected_nodes],
            "scene_plans": [
                plan.model_dump(mode="json")
                for plan in blueprint.scene_plans
                if selected_keys.intersection(plan.node_keys)
            ],
            "review_scope": {
                "risk_node_keys": [nodes[index].key for index in sorted(risky)],
                "included_neighbor_node_keys": [node.key for node in selected_nodes],
                "total_blueprint_nodes": len(nodes),
            },
        }
        source_projection = "\n".join(
            f"[{source_id}] {indexed[source_id]}"
            for source_id in source_ids
            if source_id in indexed
        )
        return projected, source_projection, [node.key for node in selected_nodes]

    for review_round in range(1, 5):
        current_blueprint_hash = hashlib.sha256(
            json.dumps(
                blueprint.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        projected_blueprint, projected_source, projected_node_keys = (
            review_projection()
            if targeted_review
            else (
                blueprint.model_dump(mode="json"),
                _render_screenplay_source(render_indexed_source(source_text)),
                [node.key for node in blueprint.nodes],
            )
        )
        node_reference_contract = {
            "contract_version": "blueprint-semantic-node-reference.v1",
            "canonical_nodes": [
                {
                    "ordinal": ordinal,
                    "identity": node_key,
                }
                for ordinal, node_key in enumerate(
                    projected_node_keys,
                    start=1,
                )
            ],
        }
        projected_source_ids = list(dict.fromkeys(
            source_id
            for node in blueprint.nodes
            if node.key in set(projected_node_keys)
            for source_id in node.source_segment_ids
        ))
        projected_source_facts = [
            fact
            for fact in source_facts(source_text)
            if fact.source_segment_id in set(projected_source_ids)
        ]
        projected_source_unit_keys = [
            fact.source_unit_key for fact in projected_source_facts
        ]
        source_reference_contract = {
            "contract_version": "blueprint-semantic-source-reference.v1",
            "canonical_source_segment_ids": projected_source_ids,
            "canonical_source_unit_keys": projected_source_unit_keys,
            "structured_source_units": [
                fact.model_dump(mode="json")
                for fact in projected_source_facts
            ],
        }
        review_schema = blueprint_semantic_review_schema(
            projected_node_keys,
            projected_source_ids,
            projected_source_unit_keys,
        )
        prompt = (
            "你是漫剧叙事蓝图的独立语义审稿人。只找会导致观众理解错误、"
            "人物瞬移、状态矛盾、因果跳跃或动机突变的可证实问题；不改稿，"
            "不评价题材或人物道德，不因为个人偏好要求美化原文。\n"
            "逐项检查：\n"
            "1. 回忆进入/退出、次日/当晚/数日后是否可识别，时间标签是否互相冲突；\n"
            "2. 人物、车辆、司机、行李、房间和关键物品的位置与行动是否闭环；\n"
            "3. 已建立的住宿、关系、知情状态等是否被后文无理由推翻，是否为了推进"
            "剧情临时发明满房、同谋、开放关系等便利条件；\n"
            "4. 重大决定是否有此前可见的压力、欲望和认知依据；\n"
            "5. 威胁、武器、醉酒或失去行动能力是否被错误改写为自主选择，约束解除"
            "是否真实发生；\n"
            "6. 后文引用的视觉事实是否此前真正给观众看见。\n"
            "7. 每个节点的三元叙事语义是否与来源职责一致：story 必须是可表演、"
            "可形成画面状态变化的故事语义；paratext 必须只做来源审计并使用"
            " connective+exclude_from_spine。不得按 SRC 编号、章节位置、人物是否"
            "为空或文本关键词判断，只能依据该段在叙事中的语义职责。\n"
            "8. 每个 projection=picture 的 quoted source unit 是否恰有一个"
            " source_unit_delivery；只有 spoken_dialogue/offscreen_voice 才能有"
            " usage=voice participant evidence，并通过 source_unit_keys 精确绑定"
            "且与 performer_key 一致；"
            "missing、多个 identity 或重复/冲突 claim 必须分别输出"
            " voice_identity_missing、voice_identity_ambiguous、"
            "voice_identity_conflict，不得拖到 SceneInput。quoted source unit 只以"
            "本轮来源合同 structured_source_units 中 projection=quoted 的机器事实"
            "为准；书页、信件、回忆引语、声音效果等非口播内容必须使用对应非声音"
            "delivery mode，不能为其伪造 speaker。story/picture中 projection=action "
            "的正文及 Blueprint 的 summary/"
            "action_logic 即使出现‘旁白’‘介绍’等自然语言，也不需要 voice，禁止"
            "将其提升为 dialogue 或要求伪造旁白 identity。\n"
            "9. 每个story/picture节点中 projection=action 的 prose source unit 必须拥有唯一"
            " exact-unit usage=state_subject evidence，或在 "
            "environment_source_unit_keys 中显式标记为纯环境。visible、"
            "scene roster、content_owner 不是主体证据；缺失、多主体或"
            "人物主体与环境标记冲突必须作为 must_fix 报告。"
            "若且仅若当前 environment_source_unit_keys 中的 action unit 在本轮"
            "完整语义中实际是人物的思考、反应、发问或动作，必须只输出"
            " code=state_subject_environment_misclassified；每条 issue 恰好引用"
            "一个 owning node，并在 source_unit_keys 中精确列出该 issue 涉及的"
            "全部 canonical exact units，在 source_segment_ids 中列出这些 units"
            "精确对应的 SRC。不得为真正的环境变化输出该 code，不得用文本关键词、"
            "姓名或内容列表判断。"
            "paratext/audit_only的quoted/action unit不适用delivery或state-subject要求，"
            "其所有剧情合同字段必须为空。\n"
            "连续剧可继承前序集已经建立的人物和关系；原文在当前节点明确揭示的"
            "既有关系，只要该节点先以可见/可听内容建立再引用，也不属于"
            " setup_missing。不得要求删除原文明确写出的关系来修复 setup。\n"
            "required_resolution 不得把无来源的便利设定伪装为原文事实；若只能通过"
            "改编补桥修复，必须明确要求 adaptation_kind=logic_bridge 及审计理由。"
            "每个问题必须引用本轮节点引用合同中的 canonical identity；node_keys"
            " 每项可直接使用 identity，或使用结构化 {\"ordinal\":正整数} /"
            " {\"identity\":\"canonical identity\"}。ordinal 从 1 开始，严格对应"
            " canonical_nodes 顺序。禁止根据文本相似度推断、拼接或改写 identity。"
            "发现确定问题后必须保留完整 issue；修正引用时不得删除该 issue。"
            "有直接原文依据时附 source_segment_ids。只输出 must_fix=true 的确定"
            "问题，禁止泛泛建议。"
            "\n\n本轮节点引用合同：\n"
            + json.dumps(
                node_reference_contract,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n本轮来源引用合同：\n"
            + json.dumps(
                source_reference_contract,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n蓝图：\n"
            + json.dumps(
                projected_blueprint,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n带稳定 ID 的原文：\n"
            + projected_source
            + "\n\n输出 Schema：\n"
            + json.dumps(
                review_schema,
                ensure_ascii=False,
            )
        )
        trace = current_trace()
        reviews: list[BlueprintSemanticReview] = []
        review_artifact_ids: list[str] = []
        dropped_voice_issue_counts: dict[int, int] = {}
        async def run_reviewer(sample_no: int) -> BlueprintSemanticReview:
            last_validated_review: BlueprintSemanticReview | None = None
            validated_drop_count = 0

            def validate_review(candidate_review: BlueprintSemanticReview) -> list[str]:
                nonlocal last_validated_review, validated_drop_count
                dropped = filter_blueprint_semantic_review_voice_issues(
                    candidate_review,
                    blueprint,
                    source_text,
                )
                if candidate_review is last_validated_review:
                    validated_drop_count += dropped
                else:
                    last_validated_review = candidate_review
                    validated_drop_count = dropped
                errors = validate_blueprint_semantic_review(
                    candidate_review,
                    blueprint,
                    source_text,
                )
                if targeted_review:
                    allowed = set(projected_node_keys)
                    errors.extend(
                        f"风险审稿引用了范围外节点：{node_key}"
                        for issue in candidate_review.issues
                        for node_key in issue.node_keys
                        if node_key not in allowed
                    )
                return errors

            review_messages = [
                {"role": "system", "content": SYSTEM_PREFIX},
                {
                    "role": "user",
                    "content": f"{prompt}\n独立审稿样本编号：{sample_no}",
                },
            ]
            operation_id, effective_max_tokens = (
                _blueprint_structured_operation_id(
                    operation_kind="review",
                    episode_id=str(episode.get("id") or ""),
                    semantic_input_hash=current_blueprint_hash,
                    ordinal=(
                        f"{review_round}:{sample_no}:"
                        f"{'targeted' if targeted_review else 'full'}"
                    ),
                    messages=review_messages,
                    output_schema=review_schema,
                    requested_max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
                    temperature=0.1,
                )
            )
            format_retry_limit = BLUEPRINT_REVIEW_FORMAT_RETRY_LIMIT
            durable_base_replay = bool(
                generation_budget is not None
                and operation_id
                in generation_budget._durable_successful_operations
            )
            reservation_operation_id = operation_id
            if durable_base_replay and format_retry_limit > 0:
                reservation_operation_id = (
                    _blueprint_format_repair_reservation_operation_id(
                        operation_id
                    )
                )
            reservation_id: int | None = None
            remaining_seconds: float | None = None
            legacy_retry_call_id: int | None = None
            if generation_budget is not None:
                legacy_retry_call_id = (
                    generation_budget.explicit_retry_call_id(
                        "screenplay_blueprint_review"
                    )
                )
                reservation_id = generation_budget.claim(
                    max_tokens=effective_max_tokens,
                    requested_max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
                    operation_id=reservation_operation_id,
                )
                remaining_seconds = generation_budget.remaining_seconds()
            review_call = model_gateway.chat_structured(
                review_messages,
                model_type=BlueprintSemanticReview,
                validate=validate_review,
                operation_id=operation_id,
                temperature=0.1,
                max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
                format_retry_limit=format_retry_limit,
                semantic_retry_limit=0,
                call_meta={
                    "stage": "剧本蓝图语义审稿",
                    "stage_key": "screenplay_blueprint_review",
                    "call_role": "stage_critic",
                    "call_role_label": "蓝图独立语义审稿",
                    "review_round": review_round,
                    "review_sample": sample_no,
                    "supersedes_provider_call_id": legacy_retry_call_id,
                    "episode_id": str(episode.get("id") or ""),
                    "production_grant_id": (
                        generation_budget.retry_grant_id
                        if generation_budget is not None else ""
                    ),
                    "contract_version": BLUEPRINT_VERSION,
                    "substage": "risk_nodes" if targeted_review else "full",
                    "source_count": len(projected_source.splitlines()),
                    "reuse_successful_operation": True,
                    "require_cached_successful_operation": (
                        durable_base_replay and format_retry_limit <= 0
                    ),
                    "disable_reasoning_fallback": True,
                    "disable_provider_retries": True,
                    "disable_provider_candidate_fallback": True,
                },
                repair_context=json.dumps(
                    {
                        "node_reference_contract": node_reference_contract,
                        "source_reference_contract": (
                            source_reference_contract
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                output_schema=review_schema,
                normalize_payload=lambda payload: (
                    normalize_blueprint_semantic_review_payload(
                        payload,
                        projected_node_keys,
                    )
                ),
                usage_callback=(
                    None
                    if reservation_id is None
                    else lambda usage_event: generation_budget.record_usage(
                        reservation_id,
                        usage_event,
                    )
                ),
            )
            try:
                review = (
                    await review_call
                    if remaining_seconds is None
                    else await asyncio.wait_for(
                        review_call,
                        timeout=max(0.001, remaining_seconds),
                    )
                )
            except hiagent.ProviderError as exc:
                if reservation_id is not None:
                    generation_budget.settle(
                        reservation_id,
                        unreported_outcome=(
                            "not_sent"
                            if exc.delivery_state == "not_sent"
                            and exc.replay_safe
                            else "unknown"
                        ),
                    )
                raise
            except BaseException:
                if reservation_id is not None:
                    generation_budget.settle(reservation_id)
                raise
            else:
                if reservation_id is not None:
                    generation_budget.settle(reservation_id)
            # The real gateway invokes validate_review, but test/replay
            # adapters are allowed to return a typed cached value directly.
            # Reapply the deterministic authority filter at the boundary so an
            # unsupported delivery/state guess can never reach consensus.  If
            # the same object was already filtered by the callback, retain its
            # prior count instead of counting the boundary no-op twice.
            boundary_dropped = filter_blueprint_semantic_review_voice_issues(
                review,
                blueprint,
                source_text,
            )
            dropped_voice_issue_counts[sample_no] = (
                validated_drop_count + boundary_dropped
                if review is last_validated_review
                else boundary_dropped
            )
            review.issues = [
                issue
                for issue in review.issues
                if not blueprint_semantic_issue_is_resolved(
                    issue,
                    blueprint,
                )
            ]
            return review

        async def run_reviewer_resilient(
            sample_no: int,
        ) -> BlueprintSemanticReview:
            # Retry a single reviewer ONLY when the provider never received the
            # request (not_sent + replay_safe): that cannot double-charge or
            # leave unknown liability, and re-uses the same deterministic
            # operation_id. Timeouts / mid-stream cuts (unknown outcome) are not
            # ProviderError-not_sent, so they still propagate and fail closed.
            attempts = BLUEPRINT_REVIEW_PROVIDER_RETRY_LIMIT + 1
            for attempt in range(1, attempts + 1):
                try:
                    return await run_reviewer(sample_no)
                except hiagent.ProviderError as exc:
                    replay_safe = bool(
                        getattr(exc, "delivery_state", None) == "not_sent"
                        and getattr(exc, "replay_safe", False)
                    )
                    if not replay_safe or attempt >= attempts:
                        raise
                    if trace.run_id:
                        evidence_repository.append_event(
                            trace.run_id,
                            "BLUEPRINT_REVIEWER_RETRY",
                            "info",
                            "独立审稿样本未送达，按 replay-safe 重试同一确定性 operation",
                            step_run_id=trace.step_run_id,
                            trace_id=trace.trace_id,
                            payload={
                                "review_round": review_round,
                                "review_sample": sample_no,
                                "attempt": attempt,
                            },
                        )
            raise AssertionError("unreachable reviewer retry exhaustion")

        def record_review(sample_no: int, result: Any) -> bool:
            if isinstance(result, BaseException):
                evidence_repository.append_event(
                    trace.run_id,
                    "BLUEPRINT_REVIEWER_UNAVAILABLE",
                    "warning",
                    "蓝图独立审稿样本不可用，已按 operational fail-closed 处理",
                    step_run_id=trace.step_run_id,
                    trace_id=trace.trace_id,
                    payload={
                        "review_round": review_round,
                        "review_sample": sample_no,
                        "error_type": type(result).__name__,
                    },
                ) if trace.run_id else None
                return False
            review = result
            reviews.append(review)
            artifact = evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint_review",
                    scope_type="episode",
                    scope_id=str(episode.get("id") or ""),
                    status="candidate",
                    trust_level="T1",
                    content=review.model_dump(mode="json"),
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    model_snapshot={
                        "review_round": review_round,
                        "review_sample": sample_no,
                        "dropped_unsupported_voice_issue_count": (
                            dropped_voice_issue_counts.get(sample_no, 0)
                        ),
                    },
                ),
                step_run_id=trace.step_run_id,
            )
            review_artifact_ids.append(artifact["id"])
            return True

        results = await asyncio.gather(
            run_reviewer_resilient(1),
            run_reviewer_resilient(2),
            return_exceptions=True,
        )
        for failure in results:
            # A generation breaker (call/token/wall budget) is not a reviewer
            # being unavailable.  Letting gather() swallow it would resurface it
            # as "审稿人不足两份" and send the operator after the wrong thing.
            if isinstance(failure, StageError):
                raise failure
        outcomes = list(enumerate(results, start=1))
        for sample_no, result in outcomes:
            record_review(sample_no, result)

        undelivered = [
            result
            for _sample_no, result in outcomes
            if isinstance(result, BaseException)
            and _blueprint_review_sample_is_undelivered(result)
        ]
        if len(reviews) == 1 and len(undelivered) == 1:
            # Exactly one reviewer never delivered an opinion, so consensus is
            # one clean sample short rather than compromised.  Draw that one
            # sample again as a NEW deterministic operation (sample no 3), which
            # is not a replay of the unresolved call and cannot double-charge
            # it.  Bounded to a single supplementary sample per round, and the
            # call still goes through generation_budget.claim() plus the
            # activation's remaining wall clock, so it cannot outrun any
            # breaker.  Discarding a whole validated blueprint costs ~30
            # minutes; one more review sample costs ~45s.
            if trace.run_id:
                evidence_repository.append_event(
                    trace.run_id,
                    "BLUEPRINT_REVIEWER_SUPPLEMENTED",
                    "info",
                    "一名独立审稿样本未送达，补采一个新样本而非作废整份蓝图",
                    step_run_id=trace.step_run_id,
                    trace_id=trace.trace_id,
                    payload={
                        "review_round": review_round,
                        "review_sample": BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                        "undelivered_error_type": type(
                            undelivered[0]
                        ).__name__,
                    },
                )
            try:
                supplementary = await run_reviewer_resilient(
                    BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                )
            except StageError:
                raise
            except BaseException as exc:  # noqa: BLE001 - fail closed below
                record_review(BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE, exc)
            else:
                record_review(
                    BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                    supplementary,
                )

        if len(reviews) < 2:
            evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint_review_consensus",
                    scope_type="episode",
                    scope_id=str(episode.get("id") or ""),
                    status="needs_revision",
                    trust_level="T1",
                    content={
                        "review_round": review_round,
                        "blueprint_hash": current_blueprint_hash,
                        "consensus_issue_keys": [],
                        "non_consensus_issue_count": sum(
                            len(review.issues) for review in reviews
                        ),
                        "valid_review_sample_count": len(reviews),
                        "unavailable_review_sample_count": 2 - len(reviews),
                        "dropped_unsupported_voice_issue_count": sum(
                            dropped_voice_issue_counts.values()
                        ),
                    },
                    parent_artifact_ids=review_artifact_ids,
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    model_snapshot={
                        "review_policy_version": (
                            BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                        ),
                        "authority_fingerprint": (
                            blueprint_authority_validator_fingerprint()
                        ),
                        "source_corpus_hash": review_source_corpus_hash,
                        "review_input_fingerprint": review_input_fingerprint,
                    },
                ),
                step_run_id=trace.step_run_id,
            )
            raise ContentGenerationError(
                "蓝图语义审稿人不足两份，已停止而非静默视为无问题"
            )

        issue_maps = [
            {
                (
                    issue.code,
                    tuple(sorted(issue.node_keys)),
                    tuple(sorted(issue.source_unit_keys)),
                ): issue
                for issue in review.issues
                if issue.must_fix
            }
            for review in reviews
        ]
        consensus_keys = set(issue_maps[0]).intersection(issue_maps[1])
        consensus_issues = [
            issue_maps[0][key] for key in sorted(consensus_keys)
        ]
        non_consensus_issue_count = (
            sum(len(issue_map) for issue_map in issue_maps)
            - 2 * len(consensus_keys)
        )
        deterministic_authority_issues = sorted(
            (
                issue
                for issue_map in issue_maps
                for issue_key, issue in issue_map.items()
                if (
                    issue_key not in consensus_keys
                    and _blueprint_semantic_issue_has_deterministic_authority(
                        issue,
                        blueprint,
                        source_text,
                    )
                )
            ),
            key=_blueprint_semantic_issue_exact_scope,
        )
        authoritative_issues = (
            consensus_issues + deterministic_authority_issues
        )
        non_authoritative_residual_issue_count = (
            non_consensus_issue_count
            - len(deterministic_authority_issues)
        )
        reviews_are_clean = not issue_maps[0] and not issue_maps[1]
        needs_full_fallback = bool(
            targeted_review
            and not authoritative_issues
            and non_authoritative_residual_issue_count
        )
        full_review_has_non_authoritative_residual = bool(
            not targeted_review
            and not authoritative_issues
            and non_authoritative_residual_issue_count
        )
        consensus_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint_review_consensus",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status=(
                    "needs_revision"
                    if needs_full_fallback or authoritative_issues
                    else "validated"
                ),
                trust_level="T1",
                content={
                    "review_round": review_round,
                    "blueprint_hash": current_blueprint_hash,
                    "consensus_issue_keys": [
                        {
                            "code": code,
                            "node_keys": list(node_keys),
                            "source_unit_keys": list(source_unit_keys),
                        }
                        for code, node_keys, source_unit_keys
                        in sorted(consensus_keys)
                    ],
                    "deterministic_authority_issue_keys": [
                        {
                            "code": issue.code,
                            "node_keys": sorted(issue.node_keys),
                            "source_segment_ids": sorted(
                                issue.source_segment_ids
                            ),
                            "source_unit_keys": sorted(
                                issue.source_unit_keys
                            ),
                        }
                        for issue in deterministic_authority_issues
                    ],
                    "authoritative_issue_count": len(
                        authoritative_issues
                    ),
                    "non_consensus_issue_count": non_consensus_issue_count,
                    "non_authoritative_residual_issue_count": (
                        non_authoritative_residual_issue_count
                    ),
                    "dropped_unsupported_voice_issue_count": sum(
                        dropped_voice_issue_counts.values()
                    ),
                    "review_mode": "targeted" if targeted_review else "full",
                    "review_outcome": (
                        "full_fallback_required"
                        if needs_full_fallback else
                        "consensus_issues"
                        if consensus_keys else
                        "deterministic_authority_issues"
                        if deterministic_authority_issues else
                        "non_authoritative_one_sided_residual"
                        if full_review_has_non_authoritative_residual else
                        "clean"
                    ),
                },
                parent_artifact_ids=review_artifact_ids,
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot={
                    "review_policy_version": (
                        BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                    ),
                    "authority_fingerprint": (
                        blueprint_authority_validator_fingerprint()
                    ),
                    "source_corpus_hash": review_source_corpus_hash,
                    "review_input_fingerprint": review_input_fingerprint,
                },
            ),
            step_run_id=trace.step_run_id,
        )
        if needs_full_fallback:
            # A targeted one-sided result cannot establish clean authority.
            # The next bounded round switches to the complete Blueprint; no
            # patch is attempted from non-consensus findings.
            if review_round >= 4:
                raise ContentGenerationError(
                    "蓝图定向语义复审仍有单侧必须修复问题，已按非 clean 停止"
                )
            targeted_review = False
            continue
        if reviews_are_clean:
            persist_reviewed_authority(
                parent_artifact_ids=[str(consensus_artifact["id"])],
            )
            return blueprint
        if full_review_has_non_authoritative_residual:
            persist_reviewed_authority(
                parent_artifact_ids=[str(consensus_artifact["id"])],
            )
            return blueprint
        if not authoritative_issues:
            raise ContentGenerationError(
                "蓝图双审存在未解决问题，但没有可安全修复的权威问题"
            )
        if review_round >= 4:
            gate_label = (
                "语义共识"
                if consensus_issues
                else "确定性权威"
            )
            raise ContentGenerationError(
                f"蓝图{gate_label}复审仍有必须修复问题："
                + "；".join(
                    issue.message for issue in authoritative_issues[:10]
                )
            )
        semantic_errors = [
            (
                f"[BLUEPRINT_SEMANTIC_{issue.code.upper()}] "
                f"{'、'.join(issue.node_keys)} "
                f"{'、'.join(issue.source_segment_ids)} "
                f"{'、'.join(issue.source_unit_keys)}："
                f"{issue.message}；必须：{issue.required_resolution}"
            )
            for issue in authoritative_issues
        ]
        ownership_issues = [
            issue
            for issue in authoritative_issues
            if issue.code == "state_subject_environment_misclassified"
        ]
        mixed_issues = [
            issue
            for issue in authoritative_issues
            if issue.code != "state_subject_environment_misclassified"
        ]
        ownership_artifact_ids: list[str] = []
        if mixed_issues:
            protected_unit_keys = list(dict.fromkeys(
                unit_key
                for issue in ownership_issues
                for unit_key in issue.source_unit_keys
            ))
            protected_claims = _blueprint_exact_ownership_claims(
                blueprint,
                protected_unit_keys,
            )
            blueprint = await _repair_narrative_blueprint(
                blueprint,
                episode=episode,
                source_text=source_text,
                additional_errors=[
                    error
                    for error, issue in zip(
                        semantic_errors,
                        authoritative_issues,
                    )
                    if issue.code
                    != "state_subject_environment_misclassified"
                ],
                generation_budget=generation_budget,
            )
            if protected_claims != _blueprint_exact_ownership_claims(
                blueprint,
                protected_unit_keys,
            ):
                raise ContentGenerationError(
                    "蓝图普通节点修复越权改写 exact-unit ownership"
                )
        if ownership_issues:
            blueprint, ownership_artifact_id = (
                await _repair_reviewed_blueprint_state_subject_ownership(
                    blueprint,
                    issues=ownership_issues,
                    episode=episode,
                    source_text=source_text,
                    generation_budget=generation_budget,
                )
            )
            ownership_artifact_ids.append(ownership_artifact_id)
            targeted_review = False
        elif non_consensus_issue_count:
            targeted_review = False
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint_review_repair_link",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="validated",
                trust_level="T1",
                content={
                    "review_artifact_ids": review_artifact_ids,
                    "repaired_issue_count": len(authoritative_issues),
                    "consensus_repaired_issue_count": len(
                        consensus_issues
                    ),
                    "deterministic_authority_repaired_issue_count": len(
                        deterministic_authority_issues
                    ),
                    "ownership_repaired_issue_count": len(ownership_issues),
                    "mixed_repaired_issue_count": len(mixed_issues),
                    "ownership_source_unit_keys": list(dict.fromkeys(
                        unit_key
                        for issue in ownership_issues
                        for unit_key in issue.source_unit_keys
                    )),
                },
                parent_artifact_ids=(
                    review_artifact_ids + ownership_artifact_ids
                ),
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            ),
            step_run_id=trace.step_run_id,
        )
    return blueprint


def _blueprint_review_sample_is_undelivered(exc: BaseException) -> bool:
    """Whether a reviewer failed without ever authoring a review opinion.

    Only these are worth drawing again.  A transport failure (timeout, cut
    stream) and a body that never decoded into JSON both mean the reviewer
    never said anything, so a fresh sample restores the missing opinion without
    overruling one.

    Deliberately excluded:

    * ``StructuredSemanticError`` -- the reviewer *did* author an opinion and it
      failed the review contract.  Re-drawing until some sample passes is
      exactly the coached-compliance failure the strict contracts forbid.
    * ``StructuredFormatError`` with ``unparseable=False`` -- a decoded but
      off-schema answer is likewise authored, and the gateway already spent its
      one bounded format repair on it.
    * ``StructuredProviderRejection`` -- an explicit refusal envelope is
      normally persistent; another sample just burns wall clock.
    * ``StageError`` -- generation breakers must surface, not be re-drawn.
    """
    if isinstance(exc, StageError):
        return False
    if isinstance(exc, hiagent.ProviderError):
        return True
    if isinstance(exc, model_gateway.StructuredProviderRejection):
        return False
    if isinstance(exc, model_gateway.StructuredFormatError):
        return bool(getattr(exc, "unparseable", False))
    return False


def _blueprint_shard_boundary_context(
    nodes: list[Any],
) -> dict[str, Any]:
    active_facts: dict[str, dict[str, Any]] = {}
    participant_locations: dict[str, str] = {}
    story_nodes = [
        node for node in nodes
        if node.narrative_layer == "story"
    ]
    for node in story_nodes:
        for change in node.state_changes:
            for fact_key in change.supersedes_fact_keys:
                active_facts.pop(fact_key, None)
            active_facts[change.fact_key] = {
                "fact_key": change.fact_key,
                "state_key": change.state_key,
                "value": change.value,
                "established_by": node.key,
            }
        for participant in node.participants:
            participant_locations[participant] = node.location_key
    return {
        "recent_nodes": [
            {
                "key": node.key,
                "summary": node.summary,
                "temporal_domain_key": node.temporal_domain_key,
                "time_label": node.time_label,
                "location_key": node.location_key,
                "location_label": node.location_label,
                "participants": node.participants,
            }
            for node in story_nodes[-6:]
        ],
        "active_state_facts": list(active_facts.values())[-40:],
        "participant_locations": participant_locations,
    }


def _namespace_blueprint_shard(
    shard: NarrativeBlueprintShard,
) -> None:
    prefix = f"S{shard.shard_index:03d}-"
    node_key_map = {
        node.key: f"{prefix}{node.key}"
        for node in shard.nodes
        if not node.key.startswith(prefix)
    }
    fact_key_map = {
        change.fact_key: f"{prefix}{change.fact_key}"
        for node in shard.nodes
        for change in node.state_changes
        if not change.fact_key.startswith(prefix)
    }
    for node in shard.nodes:
        node.key = node_key_map.get(node.key, node.key)
        for requirement in node.state_requirements:
            requirement.required_fact_key = fact_key_map.get(
                requirement.required_fact_key,
                requirement.required_fact_key,
            )
        for change in node.state_changes:
            change.fact_key = fact_key_map.get(
                change.fact_key,
                change.fact_key,
            )
            change.supersedes_fact_keys = [
                fact_key_map.get(fact_key, fact_key)
                for fact_key in change.supersedes_fact_keys
            ]
        if node.decision is not None:
            node.decision.setup_node_keys = [
                node_key_map.get(node_key, node_key)
                for node_key in node.decision.setup_node_keys
            ]
            node.decision.constraint_release_node_keys = [
                node_key_map.get(node_key, node_key)
                for node_key in node.decision.constraint_release_node_keys
            ]
            node.decision.constraint_fact_key = fact_key_map.get(
                node.decision.constraint_fact_key,
                node.decision.constraint_fact_key,
            )


def _blueprint_node_has_operational_authority(node: Any) -> bool:
    return bool(
        node.participants
        or node.participant_evidence
        or node.source_unit_deliveries
        or node.state_subject_assignments
        or node.environment_source_unit_keys
        or node.state_requirements
        or node.state_changes
        or node.released_constraints_for
        or node.decision is not None
        or node.exit_state.strip()
    )


def _collapse_nonoperational_duplicate_source_nodes(
    shard: NarrativeBlueprintShard,
) -> None:
    """Remove only authority-free nodes whose SRCs have one other owner."""
    owners_by_source: defaultdict[str, list[int]] = defaultdict(list)
    for index, node in enumerate(shard.nodes):
        for source_id in dict.fromkeys(node.source_segment_ids):
            owners_by_source[source_id].append(index)

    removable_indexes = {
        index
        for index, node in enumerate(shard.nodes)
        if (
            node.source_segment_ids
            and not _blueprint_node_has_operational_authority(node)
            and all(
                len({
                    owner_index
                    for owner_index in owners_by_source[source_id]
                    if owner_index != index
                }) == 1
                for source_id in node.source_segment_ids
            )
        )
    }
    # Two authority-free duplicate nodes do not establish which one is
    # redundant. Keep both so the source occurrence validator fails closed.
    removable_indexes = {
        index
        for index in removable_indexes
        if all(
            next(
                owner_index
                for owner_index in owners_by_source[source_id]
                if owner_index != index
            ) not in removable_indexes
            for source_id in shard.nodes[index].source_segment_ids
        )
    }
    shard.nodes = [
        node
        for index, node in enumerate(shard.nodes)
        if index not in removable_indexes
    ]


def _remove_duplicate_repair_orphan_nodes(
    shard: NarrativeBlueprintShard,
    *,
    attempt: int,
    previous_candidate: dict[str, Any] | None,
    previous_validation_errors: list[str],
) -> None:
    """Remove only nodes orphaned while repairing typed duplicate ownership."""

    if (
        attempt <= 1
        or previous_candidate is None
    ):
        return
    previous = NarrativeBlueprintShard.model_validate(previous_candidate)
    reported_errors = set(previous_validation_errors)
    duplicate_issues = [
        issue
        for issue in blueprint_source_occurrence_issues(
            previous.nodes,
            prefix="BLUEPRINT_SHARD",
        )
        if issue.error in reported_errors
    ]
    duplicate_sources_by_node: defaultdict[str, set[str]] = defaultdict(set)
    for issue in duplicate_issues:
        for node_key in issue.node_keys:
            duplicate_sources_by_node[node_key].add(
                issue.source_segment_id
            )
    previous_nodes_by_key: defaultdict[str, list[Any]] = defaultdict(list)
    for node in previous.nodes:
        previous_nodes_by_key[node.key].append(node)
    current_owners: defaultdict[str, list[str]] = defaultdict(list)
    for node in shard.nodes:
        for source_id in node.source_segment_ids:
            current_owners[source_id].append(node.key)

    def removable(node: Any) -> bool:
        if (
            node.source_segment_ids
            or _blueprint_node_has_operational_authority(node)
        ):
            return False
        previous_matches = previous_nodes_by_key.get(node.key, [])
        if len(previous_matches) != 1:
            return False
        lost_sources = previous_matches[0].source_segment_ids
        return bool(lost_sources) and all(
            source_id in duplicate_sources_by_node[node.key]
            and len(current_owners[source_id]) == 1
            and current_owners[source_id][0] != node.key
            for source_id in lost_sources
        )

    shard.nodes = [
        node
        for node in shard.nodes
        if not removable(node)
    ]


def _normalize_blueprint_shard_structure(
    shard: NarrativeBlueprintShard,
    *,
    boundary_context: dict[str, Any],
    attempt: int = 1,
    previous_candidate: dict[str, Any] | None = None,
    previous_validation_errors: list[str] | None = None,
) -> None:
    _collapse_nonoperational_duplicate_source_nodes(shard)
    _remove_duplicate_repair_orphan_nodes(
        shard,
        attempt=attempt,
        previous_candidate=previous_candidate,
        previous_validation_errors=previous_validation_errors or [],
    )
    fact_state_keys = {
        str(fact.get("fact_key") or ""): str(
            fact.get("state_key") or ""
        )
        for fact in boundary_context.get("active_state_facts", [])
    }
    previous = None
    for node in shard.nodes:
        if previous is not None:
            changed_domain = (
                node.temporal_domain_key
                != previous.temporal_domain_key
            )
            changed_location = node.location_key != previous.location_key
            if changed_domain and node.time_relation == "continuous":
                node.time_relation = "jump"
            if (
                (changed_domain or changed_location)
                and not node.transition_cue.strip()
            ):
                node.transition_cue = (
                    node.opening_image.strip()
                    or f"从{previous.location_label}转至{node.location_label}"
                )
        if (
            node.decision is not None
            and node.decision.impact == "major"
            and not node.decision.pressure.strip()
        ):
            node.decision.pressure = node.action_logic
        if (
            node.decision is not None
            and node.decision.impact == "major"
            and not node.decision.setup_node_keys
            and node.decision.pressure.strip()
            and node.decision.desire.strip()
        ):
            node.decision.setup_node_keys = [node.key]
        for change in node.state_changes:
            change.supersedes_fact_keys = [
                fact_key
                for fact_key in change.supersedes_fact_keys
                if (
                    fact_key not in fact_state_keys
                    or fact_state_keys[fact_key] == change.state_key
                    or node.released_constraints_for
                )
            ]
            fact_state_keys[change.fact_key] = change.state_key
        previous = node
    if (
        shard.shard_index == 1
        and not boundary_context.get("active_state_facts")
    ):
        for node in shard.nodes:
            for requirement in node.state_requirements:
                if not requirement.required_fact_key.strip():
                    requirement.assumed_prior = True


def _blueprint_segment_output_weight(segment: Any) -> int:
    """Estimate typed output pressure without asking the model to plan itself."""

    facts = source_segment_facts(segment.segment_id, segment.text)
    return max(1, len(facts))


def _partition_blueprint_segments(segments: list[Any]) -> list[list[Any]]:
    """Create stable sequential shards bounded by SRC and source-fact pressure."""

    shards: list[list[Any]] = []
    current: list[Any] = []
    current_weight = 0
    for segment in segments:
        weight = _blueprint_segment_output_weight(segment)
        if current and (
            len(current) >= BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD
            or current_weight + weight
            > BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD
        ):
            shards.append(current)
            current = []
            current_weight = 0
        current.append(segment)
        current_weight += weight
    if current:
        shards.append(current)
    return shards


def _split_blueprint_segments(segments: list[Any]) -> list[list[Any]]:
    """Split one failed shard at the deterministic nearest weight midpoint."""

    if len(segments) < 2:
        return [segments]
    weights = [_blueprint_segment_output_weight(segment) for segment in segments]
    total = sum(weights)
    prefix = 0
    best_index = 1
    best_distance: int | None = None
    for index, weight in enumerate(weights[:-1], start=1):
        prefix += weight
        distance = abs(total - 2 * prefix)
        if best_distance is None or distance < best_distance:
            best_index = index
            best_distance = distance
    return [segments[:best_index], segments[best_index:]]


def _blueprint_leaf_plan_from_cache(
    segments: list[Any],
    cached_rows: list[Any],
    *,
    source_corpus_hash: str | None = None,
) -> tuple[list[list[Any]], list[int], dict[int, tuple[Any, NarrativeBlueprintShard]]]:
    """Rebuild one exact source cover before any paid parent request.

    Current-policy validated leaves are durable split-manifest entries.  They
    may cover a prefix plus later gaps after a failed activation.  Non-identical
    overlapping leaves are ambiguous authority and therefore fail closed;
    uncovered ranges are partitioned deterministically without first paying for
    a parent range that already contains reusable children.
    """
    source_ids = [str(segment.segment_id) for segment in segments]
    source_positions = {
        source_id: index for index, source_id in enumerate(source_ids)
    }
    interval_rows: dict[
        tuple[int, int], tuple[Any, NarrativeBlueprintShard, int]
    ] = {}
    for row in cached_rows:
        try:
            snapshot = json.loads(row["model_snapshot_json"] or "{}")
            if (
                snapshot.get("source_fact_version") != SOURCE_FACT_VERSION
                or snapshot.get("shard_policy_version")
                != BLUEPRINT_SHARD_POLICY_VERSION
                or snapshot.get("local_authority_validator_version")
                != BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
                or snapshot.get("split_manifest_version")
                != BLUEPRINT_SPLIT_MANIFEST_VERSION
            ):
                continue
            if source_corpus_hash is not None and snapshot.get(
                "source_corpus_hash"
            ) != source_corpus_hash:
                continue
            raw_content = json.loads(row["content_json"] or "{}")
            from app.evidence import repository as evidence_repository

            if str(row["content_hash"] or "") != evidence_repository.content_hash(
                raw_content
            ):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_HASH] validated leaf 内容哈希漂移"],
                )
            shard = NarrativeBlueprintShard.model_validate(raw_content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        owned = list(shard.source_segment_ids)
        if not owned or any(source_id not in source_positions for source_id in owned):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_SPLIT_MANIFEST_SOURCE_ESCAPE] 缓存 leaf 引用当前来源外 SRC"],
            )
        positions = [source_positions[source_id] for source_id in owned]
        if positions != list(range(positions[0], positions[-1] + 1)):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_SPLIT_MANIFEST_SOURCE_GAP] 缓存 leaf 来源不连续或乱序"],
            )
        interval = (positions[0], positions[-1] + 1)
        prior = interval_rows.get(interval)
        if prior is not None:
            if prior[1].model_dump(mode="json") != shard.model_dump(mode="json"):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_DUPLICATE_CONFLICT] 同区间存在不同 validated leaf"],
                )
            continue
        interval_rows[interval] = (
            row,
            shard,
            int(snapshot.get("split_depth") or 0),
        )
    ordered = sorted(interval_rows)
    for left, right in zip(ordered, ordered[1:]):
        if right[0] < left[1]:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_SPLIT_MANIFEST_OVERLAP] validated leaf 区间重叠"],
            )

    planned: list[list[Any]] = []
    depths: list[int] = []
    cached_by_plan_index: dict[int, tuple[Any, NarrativeBlueprintShard]] = {}
    cursor = 0
    for start, end in ordered:
        if cursor < start:
            for gap in _partition_blueprint_segments(segments[cursor:start]):
                planned.append(gap)
                depths.append(0)
        row, shard, depth = interval_rows[(start, end)]
        planned.append(segments[start:end])
        depths.append(depth)
        cached_by_plan_index[len(planned)] = (row, shard)
        cursor = end
    if cursor < len(segments):
        for gap in _partition_blueprint_segments(segments[cursor:]):
            planned.append(gap)
            depths.append(0)
    flattened = [segment.segment_id for group in planned for segment in group]
    if flattened != source_ids:
        raise StageError(
            "剧本时空因果蓝图分片",
            ["[BLUEPRINT_SPLIT_MANIFEST_COVERAGE] leaf/gap 计划未精确覆盖当前来源"],
        )
    for plan_index, (_row, shard) in cached_by_plan_index.items():
        if shard.shard_index != plan_index:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_SPLIT_MANIFEST_INDEX] 缓存 leaf 序号与精确覆盖顺序不一致"],
            )
    return planned, depths, cached_by_plan_index


def _blueprint_shard_token_budget(segments: list[Any]) -> int:
    weight = sum(_blueprint_segment_output_weight(segment) for segment in segments)
    estimated = BLUEPRINT_SHARD_MIN_TOKENS + weight * 512
    return min(
        BLUEPRINT_SHARD_MAX_TOKENS,
        max(BLUEPRINT_SHARD_MIN_TOKENS, estimated),
    )


_BLUEPRINT_SOURCE_UNIT_KEY_PATTERN = re.compile(r"\bSRC\d+:unit:\d+\b")


def _freeze_unreported_voice_pairs(
    candidate_payload: dict[str, Any],
    *,
    previous_candidate: dict[str, Any],
    validation_errors: list[str],
) -> dict[str, Any]:
    """Restore only unchanged, valid audible pairs omitted by a retry."""

    candidate = deepcopy(candidate_payload)
    mutable_unit_keys = {
        unit_key
        for error in validation_errors
        for unit_key in _BLUEPRINT_SOURCE_UNIT_KEY_PATTERN.findall(error)
    }

    def node_index(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        indexed: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        nodes = payload.get("nodes")
        if not isinstance(nodes, list):
            return indexed
        for node in nodes:
            if not isinstance(node, dict):
                continue
            key = node.get("key")
            if isinstance(key, str):
                indexed[key].append(node)
        return indexed

    previous_nodes = node_index(previous_candidate)
    candidate_nodes = node_index(candidate)
    for node_key, previous_matches in previous_nodes.items():
        candidate_matches = candidate_nodes.get(node_key, [])
        if len(previous_matches) != 1 or len(candidate_matches) != 1:
            continue
        previous_node = previous_matches[0]
        candidate_node = candidate_matches[0]
        previous_source_ids = previous_node.get("source_segment_ids")
        candidate_source_ids = candidate_node.get("source_segment_ids")
        if (
            not isinstance(previous_source_ids, list)
            or not isinstance(candidate_source_ids, list)
            or candidate_source_ids != previous_source_ids
        ):
            continue

        previous_deliveries = previous_node.get(
            "source_unit_deliveries",
            [],
        )
        previous_evidence = previous_node.get("participant_evidence", [])
        candidate_deliveries = candidate_node.get(
            "source_unit_deliveries",
            [],
        )
        candidate_evidence = candidate_node.get("participant_evidence", [])
        if not all(
            isinstance(value, list)
            for value in (
                previous_deliveries,
                previous_evidence,
                candidate_deliveries,
                candidate_evidence,
            )
        ):
            continue

        def deliveries_by_unit(
            rows: list[Any],
        ) -> defaultdict[str, list[dict[str, Any]]]:
            indexed: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                unit_key = row.get("source_unit_key")
                if isinstance(unit_key, str):
                    indexed[unit_key].append(row)
            return indexed

        def voice_claims_by_unit(
            rows: list[Any],
        ) -> defaultdict[str, list[dict[str, Any]]]:
            indexed: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                if not isinstance(row, dict) or row.get("usage") != "voice":
                    continue
                unit_keys = row.get("source_unit_keys")
                if not isinstance(unit_keys, list):
                    continue
                for unit_key in unit_keys:
                    if isinstance(unit_key, str):
                        indexed[unit_key].append(row)
            return indexed

        previous_delivery_by_unit = deliveries_by_unit(previous_deliveries)
        previous_claims_by_unit = voice_claims_by_unit(previous_evidence)
        candidate_delivery_by_unit = deliveries_by_unit(candidate_deliveries)
        candidate_claims_by_unit = voice_claims_by_unit(candidate_evidence)
        for unit_key, unit_deliveries in previous_delivery_by_unit.items():
            if (
                unit_key in mutable_unit_keys
                or _BLUEPRINT_SOURCE_UNIT_KEY_PATTERN.fullmatch(unit_key) is None
                or len(unit_deliveries) != 1
            ):
                continue
            previous_delivery = unit_deliveries[0]
            performer_key = previous_delivery.get("performer_key")
            if (
                previous_delivery.get("mode")
                not in AUDIBLE_SOURCE_DELIVERY_MODES
                or not isinstance(performer_key, str)
                or not performer_key.strip()
            ):
                continue
            previous_claims = previous_claims_by_unit.get(unit_key, [])
            if len(previous_claims) != 1:
                continue
            previous_claim = previous_claims[0]
            unit_source_id = unit_key.split(":unit:", 1)[0]
            evidence_source_ids = previous_claim.get("source_segment_ids")
            if (
                previous_claim.get("identity_key") != performer_key
                or not isinstance(evidence_source_ids, list)
                or unit_source_id not in evidence_source_ids
            ):
                continue

            unit_candidate_deliveries = candidate_delivery_by_unit.get(
                unit_key,
                [],
            )
            unit_candidate_claims = candidate_claims_by_unit.get(unit_key, [])
            if unit_candidate_claims or len(unit_candidate_deliveries) > 1:
                continue
            restored_claim = deepcopy(previous_claim)
            restored_claim["source_unit_keys"] = [unit_key]
            if unit_candidate_deliveries:
                if unit_candidate_deliveries[0] != previous_delivery:
                    continue
                candidate_evidence.append(restored_claim)
                continue
            candidate_deliveries.append(deepcopy(previous_delivery))
            candidate_evidence.append(restored_claim)
    return candidate


def _freeze_unreported_state_subject_ownership(
    candidate: NarrativeBlueprintShard,
    *,
    previous_candidate: dict[str, Any],
    validation_errors: list[str],
) -> None:
    """Keep retry ownership changes local to units named by validation."""

    previous = NarrativeBlueprintShard.model_validate(previous_candidate)
    mutable_unit_keys = {
        unit_key
        for error in validation_errors
        for unit_key in _BLUEPRINT_SOURCE_UNIT_KEY_PATTERN.findall(error)
    }
    candidate_nodes = {node.key: node for node in candidate.nodes}
    previous_nodes = {node.key: node for node in previous.nodes}

    def ownership_keys(node: Any) -> set[str]:
        return {
            unit_key
            for evidence in node.participant_evidence
            if evidence.usage == "state_subject"
            for unit_key in evidence.source_unit_keys
        } | {
            assignment.source_unit_key
            for assignment in node.state_subject_assignments
        } | set(node.environment_source_unit_keys)

    previous_owner_by_unit = {
        unit_key: node.key
        for node in previous.nodes
        for unit_key in ownership_keys(node)
    }
    candidate_owned_keys = {
        unit_key
        for node in candidate.nodes
        for unit_key in ownership_keys(node)
    }
    frozen_unit_keys = {
        unit_key
        for unit_key in candidate_owned_keys | set(previous_owner_by_unit)
        if (
            unit_key not in mutable_unit_keys
            and (
                unit_key not in previous_owner_by_unit
                or previous_owner_by_unit[unit_key] in candidate_nodes
            )
        )
    }

    for node in candidate.nodes:
        retained_evidence = []
        for evidence in node.participant_evidence:
            if evidence.usage != "state_subject":
                retained_evidence.append(evidence)
                continue
            retained_keys = [
                unit_key
                for unit_key in evidence.source_unit_keys
                if unit_key not in frozen_unit_keys
            ]
            if retained_keys:
                evidence.source_unit_keys = retained_keys
                retained_evidence.append(evidence)
        node.participant_evidence = retained_evidence
        node.state_subject_assignments = [
            assignment
            for assignment in node.state_subject_assignments
            if assignment.source_unit_key not in frozen_unit_keys
        ]
        node.environment_source_unit_keys = [
            unit_key
            for unit_key in node.environment_source_unit_keys
            if unit_key not in frozen_unit_keys
        ]

    for node_key, previous_node in previous_nodes.items():
        node = candidate_nodes.get(node_key)
        if node is None:
            continue
        for evidence in previous_node.participant_evidence:
            if evidence.usage != "state_subject":
                continue
            retained_keys = [
                unit_key
                for unit_key in evidence.source_unit_keys
                if unit_key in frozen_unit_keys
            ]
            if retained_keys:
                restored = deepcopy(evidence)
                restored.source_unit_keys = retained_keys
                node.participant_evidence.append(restored)
        node.state_subject_assignments.extend(
            deepcopy(assignment)
            for assignment in previous_node.state_subject_assignments
            if assignment.source_unit_key in frozen_unit_keys
        )
        node.environment_source_unit_keys.extend(
            unit_key
            for unit_key in previous_node.environment_source_unit_keys
            if unit_key in frozen_unit_keys
        )

    for node in candidate.nodes:
        node.participants = list(dict.fromkeys(
            [
                evidence.identity_key
                for evidence in node.participant_evidence
                if evidence.identity_key.strip()
            ]
            + [
                identity_key
                for assignment in node.state_subject_assignments
                for identity_key in assignment.identity_keys
                if identity_key.strip()
            ]
        ))


def _blueprint_state_subject_repair_target_keys(
    issues: list[Any],
) -> list[str]:
    return list(dict.fromkeys(
        unit_key
        for issue in issues
        for unit_key in issue.source_unit_keys
    ))


def _blueprint_state_subject_repair_issues(
    candidate: NarrativeBlueprintShard,
    *,
    validation_errors: list[str],
    source_text: str,
) -> list[Any] | None:
    """Select repair-only mode only when typed issues equal all shard errors."""
    issues = blueprint_state_subject_issues(
        NarrativeBlueprint(
            episode_no=candidate.episode_no,
            nodes=candidate.nodes,
        ),
        source_text,
    )
    if (
        not issues
        or any(not issue.source_unit_keys for issue in issues)
        or validation_errors != [
            render_blueprint_shard_semantic_issue(issue)
            for issue in issues
        ]
    ):
        return None
    target_unit_keys = _blueprint_state_subject_repair_target_keys(issues)
    try:
        blueprint_state_subject_ownership_patch_schema(
            candidate,
            target_unit_keys,
            source_text,
        )
    except (TypeError, ValueError):
        return None
    return issues


def _blueprint_state_subject_repair_prompt(
    *,
    previous_candidate: dict[str, Any],
    issues: list[Any],
    source_payload: list[dict[str, Any]],
    source_text: str,
) -> str:
    """Render the bounded attempt-2 ownership map contract."""
    candidate = NarrativeBlueprintShard.model_validate(previous_candidate)
    target_unit_keys = _blueprint_state_subject_repair_target_keys(issues)
    patch_schema = blueprint_state_subject_ownership_patch_schema(
        candidate,
        target_unit_keys,
        source_text,
    )
    facts_by_source = {
        str(source.get("source_segment_id") or ""): list(
            source.get("source_facts") or []
        )
        for source in source_payload
    }
    facts_by_key = {
        str(fact.get("source_unit_key") or ""): fact
        for facts in facts_by_source.values()
        for fact in facts
    }
    target_source_context: dict[str, Any] = {}
    current_claims: dict[str, Any] = {}
    allowed_identities: dict[str, list[str]] = {}
    for unit_key in target_unit_keys:
        fact = facts_by_key[unit_key]
        source_id = str(fact.get("source_segment_id") or "")
        source_facts_for_unit = facts_by_source[source_id]
        fact_index = next(
            index
            for index, value in enumerate(source_facts_for_unit)
            if value.get("source_unit_key") == unit_key
        )
        target_source_context[unit_key] = {
            "source_fact": fact,
            "adjacent_units": source_facts_for_unit[
                max(0, fact_index - 1):fact_index
            ] + source_facts_for_unit[
                fact_index + 1:fact_index + 2
            ],
        }
        current_claims[unit_key] = {
            "single": [
                evidence.identity_key
                for node in candidate.nodes
                for evidence in node.participant_evidence
                if (
                    evidence.usage == "state_subject"
                    and unit_key in evidence.source_unit_keys
                )
            ],
            "joint": [
                list(assignment.identity_keys)
                for node in candidate.nodes
                for assignment in node.state_subject_assignments
                if assignment.source_unit_key == unit_key
            ],
            "environment": any(
                unit_key in node.environment_source_unit_keys
                for node in candidate.nodes
            ),
        }
        owner = next(
            node
            for node in candidate.nodes
            if source_id in node.source_segment_ids
        )
        allowed_identities[unit_key] = list(owner.participants)

    compact = lambda value: json.dumps(  # noqa: E731
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "仅输出state-subject ownership repair JSON，不得输出完整shard。"
        "repairs必须逐项覆盖schema中repairs.required的全部properties，"
        "不得漏项或增加target。single表示唯一人物主体；joint只用于结构上"
        "不可拆的共同动作；environment只用于无人物状态所有者的环境变化。"
        "identity_keys只能使用对应target的allowed_identities；"
        "不要输出source ids，服务端会从source facts派生。\n"
        f"base_candidate_hash={blueprint_shard_candidate_hash(candidate)}\n"
        f"target_source_facts={compact(target_source_context)}\n"
        f"current_claims={compact(current_claims)}\n"
        f"allowed_identities={compact(allowed_identities)}\n"
        f"schema={compact(patch_schema)}"
    )


def _blueprint_shard_prompt(
    *,
    episode_no: int,
    shard_index: int,
    shard_count: int,
    errors: list[str],
    bible_context: dict[str, Any],
    boundary: dict[str, Any],
    source_payload: list[dict[str, Any]],
    previous_candidate: dict[str, Any] | None = None,
) -> str:
    """Render the complete Blueprint contract without prose duplication."""

    rules = (
        "仅处理target_sources；每个SRC必须整体且只归一个节点，严禁把同一SRC按"
        "source unit、动作、对白、时空或容量拆给多个节点。节点只能在SRC边界拆分，"
        "按源顺序且最多"
        f"{BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE}个连续SRC。每节点把所拥有SRC内的"
        "连续动作压缩为一个核心因果进程和一个因果/情绪转折；仅story节点填exit_state。"
        "首分片首节点time_relation=episode_start；其余严格延续boundary_context。"
        "复用有效fact_key、人物位置、时间域和稳定character_key；本分片新key保持唯一。"
        "participants去重后的identity集合必须与participant_evidence中非空"
        "identity_key集合及exact-unit joint assignment identity_keys的并集完全相等；"
        "每个identity至少有一条来源证据，每条证据的source_unit_keys必须非空且"
        "只引用本节点owned SRC的unit。"
        "participant_evidence一律不要输出source_segment_ids：后端会从"
        "source_unit_keys确定性派生，多写只会浪费输出并引入不一致。"
        "usage=visible只写「在本节点画面中出现、但不是任何action unit的"
        "state_subject、也没有voice」的人物；已经写了state_subject或voice的人物"
        "不要再为同一unit补visible，后端会确定性派生其可感知证据。"
        "修复缺证据时必须保留"
        "原文已有角色并补同identity_key的participant_evidence，禁止删除角色、"
        "合并多个身份或改用默认身份。"
        "每节点显式narrative_layer/event_priority/render_policy。故事画面用"
        "story+causal+standalone；旁文本用paratext+connective+exclude_from_spine。"
        "paratext只保留summary/action_logic/opening_image等文字展示；participants、"
        "participant_evidence、state_subject_assignments、"
        "environment_source_unit_keys、source_unit_deliveries、"
        "state_requirements、state_changes、released_constraints_for必须全为[]，"
        "decision必须null，exit_state必须空字符串。"
        "不得按SRC编号、位置、空人物或词表猜分类。仅story/picture节点的quoted单元逐一给"
        "source_unit_deliveries；quoted_span不等于开口。每条spoken_dialogue/"
        "offscreen_voice delivery除performer_key外，还必须在participant_evidence"
        "追加一条独立对象：identity_key与performer_key相同、usage=\"voice\"、"
        "source_unit_keys只含该delivery的source_unit_key。"
        "performer_key不能替代这条typed voice evidence；每个声音"
        "unit必须恰有一条；written_text、sound_effect、unspoken_reference等非声音"
        "delivery在同一source_unit_key上不得有usage=voice。content_owner可以是"
        "文字、物件或概念的归属，不要求列入participants，也绝不等于performer。"
        "仅story/picture的action单元"
        "不写delivery，但必须精确三选一：单主体动作/思考/反应/发问写唯一"
        "usage=state_subject；结构切分后仍不可拆的共同动作写唯一mode=joint的"
        "state_subject_assignments并列出全部identity_keys；纯环境写"
        "environment_source_unit_keys。主体是执行动作或经历状态变化者，不得把"
        "动作目标、被观察者、同场者或unit中出现的所有姓名默认加入主体；"
        "非人物力量或环境状态作用于人物时归environment，不把受影响人物写成joint。"
        "source-fact unit可能是逗号切开的句法片段，必须结合相邻unit判断共享谓语；"
        "visible、roster和content_owner绝非主体默认值。paratext/audit_only无论原文unit是"
        "quoted还是action，都不适用delivery/state-subject规则。"
        "retry必须以previous_candidate为基线，仅修改validation_errors明确"
        "报错的source_unit_key及对应字段。state_subject_ambiguous中，可拆动作"
        "只保留唯一single state_subject；结构切分后仍不可拆的共同动作必须移除"
        "该unit全部single claims，再建立唯一mode=joint且identity_keys列出全部"
        "有来源共同主体、至少2个。未报错unit的single/joint/environment ownership"
        "必须逐项保持不变，禁止把正确single改成单元素joint。"
        "修复SRC重复owner时必须省略失去来源的冗余node，禁止输出"
        "source_segment_ids=[]的无来源node。"
        "若地点变化发生在两个SRC之间，才可在该SRC边界拆节点；若同一SRC内部跨越"
        "多个主要地点，仍必须整段只归一个节点，location_key/location_label只填写"
        "该SRC核心因果进程实际发生的一个主要地点，移动过程写入transition_cue和"
        "action_logic，禁止复合地点、禁止拆SRC。"
        "state_requirements每条的required_fact_key必须指向一个已建立事实："
        "要么是本分片更早节点state_changes里出现过的fact_key，要么是"
        "boundary_context.active_state_facts里已带入的fact_key；"
        "严禁引用任何没有在这两处建立过的fact_key（会触发"
        "BLUEPRINT_SHARD_FACT_UNKNOWN）。若某状态确实在本集之前就已成立、"
        "本分片无对应state_changes来源，则该requirement必须写assumed_prior=true"
        "并留空required_fact_key，绝不可凭空编造fact_key。supersedes_fact_keys"
        "同理只能指向已建立事实。"
        "所有描述字段简洁，不复述原文或Schema。"
    )
    if any(
        item.get("downstream_semantic_conflicts")
        for item in source_payload
    ):
        rules += (
            "本分片的 target_sources 中带 downstream_semantic_conflicts 的 unit，"
            "上一版蓝图给它的归类在下游场次写作里被两位独立校对员一致判定为无法成立："
            "环境 slot 不得写人物内容，而这些 unit 的源文本身就在讲人物，"
            "或者归属主体与源文语义不符。必须为这些 unit 重新给出成立的归属："
            "若源文讲的是某个人物的状态、反应、动作或内在特质，就把它写进该人物的 "
            "state_subject 证据，不要放进 environment_source_unit_keys；"
            "若它确实只是环境、空间或物件的客观现象，才保留为环境。"
            "不得为了绕过冲突而删除、合并或改写这些 unit 的来源归属之外的结构。"
        )
    compact = lambda value: json.dumps(  # noqa: E731 - local canonical renderer
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"生成第{episode_no}集Blueprint分片{shard_index}/{shard_count}。{rules}\n"
        f"validation_errors={compact(errors)}\n"
        f"characters={compact(bible_context)}\n"
        f"boundary_context={compact(boundary)}\n"
        f"previous_candidate={compact(previous_candidate)}\n"
        f"target_sources={compact(source_payload)}\n"
        "schema="
        + compact(blueprint_shard_provider_schema(source_payload))
    )


def _blueprint_provider_operation_id(
    *,
    episode_id: str,
    shard_index: int,
    attempt: int,
    split_depth: int,
    source_hash: str,
    boundary_hash: str,
    prompt: str,
    provider: str,
    model: str,
    max_tokens: int,
    effective_max_tokens: int,
    temperature: float,
    provider_semantic_settings: dict[str, Any],
    stall_epoch: int = 0,
) -> str:
    material = {
        "contract_version": BLUEPRINT_VERSION,
        "prompt_version": SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "shard_policy_version": BLUEPRINT_SHARD_POLICY_VERSION,
        "episode_id": episode_id,
        "shard_index": shard_index,
        "attempt": attempt,
        "split_depth": split_depth,
        "source_hash": source_hash,
        "boundary_hash": boundary_hash,
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "provider_semantic_settings": provider_semantic_settings,
        "requested_max_tokens": max_tokens,
        "effective_max_tokens": effective_max_tokens,
        "system_prompt_hash": hashlib.sha256(
            SYSTEM_PREFIX.encode("utf-8")
        ).hexdigest(),
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    if stall_epoch:
        # Only a retry after a stall carries this key, so the operation ids of
        # every ordinary attempt stay exactly what they were.
        material["stall_epoch"] = int(stall_epoch)
    return "blueprint_" + hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:32]


def _blueprint_structured_operation_id(
    *,
    operation_kind: str,
    episode_id: str,
    semantic_input_hash: str,
    ordinal: str,
    messages: list[dict[str, str]],
    output_schema: dict[str, Any],
    requested_max_tokens: int,
    temperature: float,
) -> tuple[str, int]:
    """Fingerprint an exact paid structured request, independent of run id.

    A process/run boundary is not a new semantic operation.  Provider/model
    routing and every setting that changes the outbound payload are included,
    so a legitimate configuration change becomes a new operation instead of
    either reusing or being blocked by an old cached response.
    """
    provider, model, effective_max_tokens = hiagent.text_request_token_limits(
        requested_max_tokens=requested_max_tokens,
    )
    material = {
        "operation_kind": operation_kind,
        "episode_id": episode_id,
        "contract_version": BLUEPRINT_VERSION,
        "prompt_version": SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "semantic_input_hash": semantic_input_hash,
        "ordinal": ordinal,
        "provider": provider,
        "model": model,
        "requested_max_tokens": requested_max_tokens,
        "effective_max_tokens": effective_max_tokens,
        "temperature": temperature,
        "provider_semantic_settings": (
            hiagent.text_request_semantic_settings(provider)
        ),
        "messages": messages,
        "output_schema": output_schema,
    }
    digest = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"screenplay.blueprint.{operation_kind}:{digest}", effective_max_tokens


def _blueprint_format_repair_reservation_operation_id(
    base_operation_id: str,
) -> str:
    return (
        f"{base_operation_id}:reservation:"
        f"{BLUEPRINT_VERSION}:structured-format-repair"
    )


# Deleting the screenplay is the terminal disposition of everything that
# production spent: the same command supersedes the active revision, so a
# retry grant -- which may only bind to an active revision -- can never be
# issued for a receipt that outlives it.  Marking the abandoned calls keeps
# their cost auditable while closing the liability the deleted product owned.
BLUEPRINT_CALL_ABANDONED_BY_DELETE = "ABANDONED_BY_SCREENPLAY_DELETE"


class _BlueprintGenerationBudget:
    """Reserve call exposure, then settle against provider-reported usage.

    A requested ``max_tokens`` value is only an upper bound for one active
    call.  Charging every historical request at that upper bound rejects a
    later retry even when earlier calls used a small fraction of their
    reservation.  Unknown outcomes remain conservatively charged at the full
    reservation so the cost cap never fails open.
    """

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.provider_calls = 0
        self.requested_output_tokens = 0
        self.actual_output_tokens = 0
        self.unknown_output_tokens = 0
        self._next_reservation_id = 1
        self._reservations: dict[int, dict[str, Any]] = {}
        self._durable_successful_operations: set[str] = set()
        self._durable_unknown_operations: dict[str, str] = {}
        self._durable_unknown_stage_calls: dict[str, tuple[int, str]] = {}
        self._durable_unknown_receipts: list[dict[str, Any]] = []
        self._explicit_retry_authorized = False
        self.retry_grant_id = ""
        self.planned_leaf_count = 0

    def adopt_shard_plan(self, planned_leaf_count: int) -> None:
        """Raise the runaway breakers to the size of the deterministic plan.

        The leaf plan is derived locally from the frozen source cover (and from
        already-validated cached leaves), never from model output, so it cannot
        be inflated by a provider response.  It only ever grows -- a dynamic
        split adds leaves mid-run -- and the caps grow with it, so a cap can
        never shrink below exposure that was already admitted.
        """
        count = max(0, int(planned_leaf_count))
        if count > self.planned_leaf_count:
            self.planned_leaf_count = count

    @property
    def max_provider_calls(self) -> int:
        if not self.planned_leaf_count:
            return BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS
        return max(
            BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS,
            self.planned_leaf_count * BLUEPRINT_LEAF_PROVIDER_CALLS
            + BLUEPRINT_LEAF_CALL_HEADROOM,
        )

    @property
    def plan_scale(self) -> int:
        """How many floor-sized activations the leaf plan justifies.

        Token and wall-clock exposure track the admissible call count, so all
        three breakers keep the calibration they were chosen with instead of
        one of them firing first purely because the episode is long.
        """
        floor = max(1, BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS)
        return max(1, -(-self.max_provider_calls // floor))

    @property
    def max_output_tokens(self) -> int:
        return BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS * self.plan_scale

    @property
    def max_wall_seconds(self) -> float:
        return BLUEPRINT_GENERATION_MAX_WALL_SECONDS * self.plan_scale

    def remaining_seconds(self) -> float:
        """Wall clock left for one provider call in this activation."""
        return self.max_wall_seconds - (
            time.monotonic() - self.started_at
        )

    @classmethod
    def from_durable_calls(
        cls,
        *,
        run_id: str | None,
        started_at_epoch: float | None = None,
        episode_id: str = "",
        input_fingerprint: str = "",
        retry_grant_id: str = "",
        include_resolved_by_call_id: int | None = None,
    ) -> "_BlueprintGenerationBudget":
        budget = cls()
        if started_at_epoch is not None:
            elapsed = max(0.0, time.time() - float(started_at_epoch))
            budget.started_at = time.monotonic() - elapsed
        budget.retry_grant_id = str(retry_grant_id or "")
        if not run_id and not (episode_id and input_fingerprint):
            return budget
        query = (
            "SELECT pc.id,pc.response_json,pc.meta,pc.status,"
            "pc.recovery_disposition,pc.operation_id,pc.ts,"
            "pc.superseded_by_call_id,pc.run_id,pc.production_grant_id,"
            "pc.request_hash "
            "FROM provider_calls pc "
            "LEFT JOIN workflow_runs wr ON wr.id=pc.run_id "
            "WHERE pc.kind='chat' "
            "AND json_extract(meta,'$.stage_key') IN "
            "('screenplay_blueprint_shard','screenplay_blueprint_patch',"
            "'screenplay_blueprint_review') "
            "AND pc.kind != 'provider_cache_hit'"
        )
        params: tuple[Any, ...]
        if episode_id and input_fingerprint:
            query += (
                " AND ("
                "      (wr.scope_type='episode' AND wr.scope_id=?)"
                "      OR json_extract(pc.meta,'$.episode_id')=?"
                " )"
                " AND wr.input_fingerprint=?"
            )
            params = (episode_id, episode_id, input_fingerprint)
        else:
            query += " AND pc.run_id=?"
            params = (run_id,)
        query += " ORDER BY pc.id"
        rows = get_conn().execute(query, params).fetchall()
        latest_operation_status: dict[str, tuple[str, str]] = {}
        latest_stage_status: dict[str, tuple[int, str, str]] = {}
        for row in rows:
            try:
                meta = json.loads(row["meta"] or "{}")
                response = json.loads(row["response_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                meta, response = {}, {}
            try:
                stored_operation_id = row["operation_id"]
            except (IndexError, KeyError):
                stored_operation_id = None
            try:
                durable_call_at = float(row["ts"])
            except (IndexError, KeyError, TypeError, ValueError):
                durable_call_at = 0.0
            try:
                durable_run_id = str(row["run_id"] or "")
            except (IndexError, KeyError):
                durable_run_id = str(run_id or "")
            is_current_activation = bool(
                run_id and durable_run_id == str(run_id)
            )
            try:
                durable_grant_id = str(row["production_grant_id"] or "")
            except (IndexError, KeyError):
                durable_grant_id = ""
            durable_grant_id = durable_grant_id or str(
                meta.get("production_grant_id") or ""
            )
            try:
                superseded_by_call_id = row["superseded_by_call_id"]
            except (KeyError, IndexError):
                superseded_by_call_id = None
            resolved_by_expected = bool(
                include_resolved_by_call_id
                and int(superseded_by_call_id or 0)
                == int(include_resolved_by_call_id)
            )
            try:
                durable_disposition = str(row["recovery_disposition"] or "")
            except (KeyError, IndexError):
                durable_disposition = ""
            abandoned_by_delete = (
                durable_disposition == BLUEPRINT_CALL_ABANDONED_BY_DELETE
            )
            unresolved_liability = (
                not superseded_by_call_id or resolved_by_expected
            ) and not abandoned_by_delete
            if (
                not durable_grant_id
                and episode_id
                and durable_call_at > 0
                and (not superseded_by_call_id or resolved_by_expected)
            ):
                # One narrow migration bridge for pre-column calls: a run's
                # BASELINE_GENERATION_STARTED event names the exact revision.
                # Bind only if that revision had exactly one grant at call
                # time; ambiguous grant histories remain unresolved/fail-safe.
                try:
                    legacy_grants = get_conn().execute(
                        """SELECT g.id
                             FROM run_events e
                             JOIN production_grants g
                               ON g.production_revision_id=json_extract(
                                  e.payload_json,'$.revision_id')
                            WHERE e.run_id=?
                              AND e.event_type='BASELINE_GENERATION_STARTED'
                              AND g.episode_id=? AND g.issued_at<=?
                            ORDER BY g.issued_at""",
                        (durable_run_id, episode_id, durable_call_at),
                    ).fetchall()
                    if len(legacy_grants) == 1:
                        durable_grant_id = str(legacy_grants[0]["id"] or "")
                except Exception:  # noqa: BLE001 - legacy/mocked schemas
                    durable_grant_id = ""
            operation_id = str(
                stored_operation_id or meta.get("operation_id") or ""
            ).strip()
            requested = max(1, int(meta.get("requested_max_tokens") or 1))
            effective = max(
                1,
                int(meta.get("effective_max_tokens") or requested),
            )
            status = str(row["status"] or "").upper()
            stage_key = str(meta.get("stage_key") or "")
            if (
                stage_key
                and status in {"INTERRUPTED", "RUNNING"}
                and unresolved_liability
            ):
                try:
                    durable_call_id = int(row["id"])
                except (KeyError, TypeError, ValueError):
                    durable_call_id = 0
                latest_stage_status[stage_key] = (
                    durable_call_id,
                    status,
                    durable_grant_id,
                )
                try:
                    durable_request_hash = str(row["request_hash"] or "")
                except (IndexError, KeyError):
                    durable_request_hash = ""
                budget._durable_unknown_receipts.append({
                    "call_id": durable_call_id,
                    "stage_key": stage_key,
                    "operation_id": operation_id,
                    "request_hash": durable_request_hash,
                    "effective_max_tokens": effective,
                    "prior_grant_id": durable_grant_id,
                })
            if operation_id and not abandoned_by_delete:
                # A settled-abandoned call is not "the previous attempt of this
                # semantic operation" either: leaving it here made ``claim``
                # demand a Production Grant for an operation whose liability
                # had already been closed, so a cached shard replay after a
                # delete was blocked by a call nobody can authorize any more.
                latest_operation_status[operation_id] = (
                    status,
                    durable_grant_id,
                )
            if status not in {"OK", "SUCCESS", "SUCCEEDED"}:
                disposition = str(row["recovery_disposition"] or "").lower()
                delivery_state = str(meta.get("delivery_state") or "").lower()
                if (
                    disposition not in {"not_sent", "definitely_not_sent"}
                    and delivery_state not in {"not_sent", "definitely_not_sent"}
                    and unresolved_liability
                ):
                    # A fresh retry activation inherits unresolved paid/unknown
                    # liability, but not the elapsed wall clock of a dead
                    # activation.  Current-activation calls and unresolved
                    # historical calls both consume its call/token caps.
                    budget.requested_output_tokens += requested
                    budget.provider_calls += 1
                    budget.unknown_output_tokens += effective
                continue
            # Historical successful operations remain available for strict
            # cache replay, but their already-settled cost/call count does not
            # consume the new logical retry activation's execution epoch.
            if not is_current_activation and episode_id and input_fingerprint:
                continue
            budget.requested_output_tokens += requested
            budget.provider_calls += 1
            usage = response.get("usage") if isinstance(response, dict) else None
            actual = (
                usage.get("completion_tokens")
                if isinstance(usage, dict)
                else None
            )
            if isinstance(actual, int) and actual >= 0:
                budget.actual_output_tokens += actual
            else:
                budget.unknown_output_tokens += effective
        for operation_id, (status, prior_grant_id) in latest_operation_status.items():
            if status in {"OK", "SUCCESS", "SUCCEEDED"}:
                budget._durable_successful_operations.add(operation_id)
            elif status in {"INTERRUPTED", "RUNNING"}:
                budget._durable_unknown_operations[operation_id] = prior_grant_id
        for stage_key, (call_id, _status, prior_grant_id) in latest_stage_status.items():
            if call_id:
                budget._durable_unknown_stage_calls[stage_key] = (
                    call_id,
                    prior_grant_id,
                )
        return budget

    @property
    def requires_fresh_retry_grant(self) -> bool:
        """Whether unresolved provider outcomes require a new user grant."""
        if not self._durable_unknown_stage_calls:
            return False
        if self._explicit_retry_authorized and self.retry_grant_id:
            return False
        return True

    def authorize_unknown_retry(self, grant_id: str) -> None:
        """Bind the approval-minted grant to this exact projected receipt set."""
        if not grant_id or not self._durable_unknown_stage_calls:
            raise ValueError("unknown retry authorization requires receipts and grant")
        self.retry_grant_id = grant_id
        self._explicit_retry_authorized = True

    @property
    def unknown_receipts(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._durable_unknown_receipts]

    def assert_activation_admissible(
        self,
        *,
        minimum_output_tokens: int = BLUEPRINT_SHARD_MIN_TOKENS,
    ) -> None:
        """Read-only admission fence used before any character/provider call."""
        if self.requires_fresh_retry_grant:
            raise StageError(
                "剧本时空因果蓝图分片",
                [
                    "[BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] "
                    "上次供应商结果未知；必须先签发新的 Production Grant"
                ],
            )
        elapsed = time.monotonic() - self.started_at
        if elapsed >= self.max_wall_seconds:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_TIME_BUDGET] 超过当前激活时间上限"],
            )
        if self.provider_calls >= self.max_provider_calls:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_CALL_BUDGET] 超过全局调用上限"],
            )
        if (
            self.charged_output_tokens + int(minimum_output_tokens)
            > self.max_output_tokens
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_TOKEN_BUDGET] 剩余输出预算不足一次安全分片"],
            )

    def explicit_retry_call_id(self, stage_key: str) -> int | None:
        prior = self._durable_unknown_stage_calls.get(stage_key)
        if prior is None:
            return None
        call_id, prior_grant_id = prior
        # Gate on the SAME authorization state that ``claim`` checks
        # (``_explicit_retry_authorized``), not merely on ``retry_grant_id``
        # being present and distinct. ``retry_grant_id`` is populated from the
        # config snapshot in ``from_durable_calls`` even when Site B
        # authorization failed, so the weaker guard could hand back a prior
        # interrupted call id that ``claim`` would then refuse — the two gates
        # must not disagree.
        if not (
            self._explicit_retry_authorized
            and self.retry_grant_id
            and self.retry_grant_id != prior_grant_id
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                [
                    "[BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] "
                    "上次供应商结果未知；缺少新的 Production Grant"
                ],
            )
        return call_id

    @property
    def reserved_output_tokens(self) -> int:
        return sum(
            int(item["max_tokens"])
            for item in self._reservations.values()
        )

    @property
    def charged_output_tokens(self) -> int:
        return self.actual_output_tokens + self.unknown_output_tokens

    def claim(
        self,
        *,
        max_tokens: int,
        requested_max_tokens: int | None = None,
        operation_id: str = "",
    ) -> int:
        durable_replay = bool(
            operation_id
            and operation_id in self._durable_successful_operations
        )
        prior_unknown_grant = self._durable_unknown_operations.get(operation_id)
        if prior_unknown_grant is not None and not durable_replay:
            if not self._explicit_retry_authorized:
                raise StageError(
                    "剧本时空因果蓝图分片",
                    [
                        "[BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] "
                        "上次供应商结果未知；必须由新的 Production Grant "
                        "显式授权同一语义 operation 的下一 attempt"
                    ],
                )
        elapsed = time.monotonic() - self.started_at
        if (
            not durable_replay
            and elapsed >= self.max_wall_seconds
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_TIME_BUDGET] 超过全局时间上限"],
            )
        if (
            not durable_replay
            and self.provider_calls >= self.max_provider_calls
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_CALL_BUDGET] 超过全局调用上限"],
            )
        if (
            not durable_replay
            and (
                self.charged_output_tokens
                + self.reserved_output_tokens
                + max_tokens
                > self.max_output_tokens
            )
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_TOKEN_BUDGET] 超过全局输出 token 上限"],
            )
        reservation_id = self._next_reservation_id
        self._next_reservation_id += 1
        if not durable_replay:
            self.provider_calls += 1
            self.requested_output_tokens += int(
                requested_max_tokens or max_tokens
            )
        self._reservations[reservation_id] = {
            "max_tokens": int(max_tokens),
            "requested_max_tokens": int(
                requested_max_tokens or max_tokens
            ),
            "actual_tokens": 0,
            "fresh_responses": 0,
            "unknown_responses": 0,
            "reused_responses": 0,
            "durable_replay": durable_replay,
        }
        return reservation_id

    def record_usage(
        self,
        reservation_id: int,
        usage_event: dict[str, Any],
    ) -> None:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return
        if usage_event.get("reused") is True:
            reservation["reused_responses"] += 1
            return
        reservation["fresh_responses"] += 1
        completion_tokens = usage_event.get("completion_tokens")
        if isinstance(completion_tokens, int) and completion_tokens >= 0:
            reservation["actual_tokens"] += completion_tokens
        else:
            reservation["unknown_responses"] += 1

    def settle(
        self,
        reservation_id: int,
        *,
        unreported_outcome: str = "unknown",
    ) -> dict[str, Any]:
        reservation = self._reservations.pop(reservation_id, None)
        if reservation is None:
            raise RuntimeError("蓝图输出 token 预留已结算或不存在")
        effective = int(reservation["max_tokens"])
        requested = int(reservation["requested_max_tokens"])
        durable_replay = bool(reservation["durable_replay"])
        fresh_responses = int(reservation["fresh_responses"])
        unknown_responses = int(reservation["unknown_responses"])
        actual = int(reservation["actual_tokens"])
        if fresh_responses == 0 and int(reservation["reused_responses"]) > 0:
            charged = 0
            actual_value: int | None = 0
        elif fresh_responses == 0 and unreported_outcome == "not_sent":
            charged = 0
            actual_value = 0
        elif unknown_responses == 0:
            if fresh_responses > 0:
                charged = actual
                actual_value = actual
                self.actual_output_tokens += actual
            else:
                charged = effective
                actual_value = None
                self.unknown_output_tokens += effective
        else:
            actual_value = None
            charged = actual + effective * unknown_responses
            self.actual_output_tokens += actual
            self.unknown_output_tokens += effective * unknown_responses
        return {
            "requested_max_tokens": requested,
            "effective_max_tokens": effective,
            "actual_completion_tokens": actual_value,
            "usage_reported": fresh_responses > 0 and unknown_responses == 0,
            "fresh_responses": fresh_responses,
            "reused_responses": int(reservation["reused_responses"]),
            "unknown_responses": unknown_responses,
            "durable_replay": durable_replay,
            "charged_output_tokens": charged,
            "global_charged_output_tokens": self.charged_output_tokens,
        }


def blueprint_retry_receipts_hash(receipts: list[dict[str, Any]]) -> str:
    """Canonical authority binding for one explicit unknown-retry grant."""
    raw = json.dumps(
        receipts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _blueprint_generation_budget_for_trace(
    trace: Any,
    *,
    episode_id: str = "",
) -> _BlueprintGenerationBudget:
    run_id = getattr(trace, "run_id", None)
    run_started_at: float | None = None
    input_fingerprint = ""
    retry_grant_id = ""
    retry_receipts_hash = ""
    if run_id:
        run_row = get_conn().execute(
            "SELECT started_at,input_fingerprint,config_snapshot_json "
            "FROM workflow_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run_row is not None:
            run_started_at = run_row["started_at"]
            input_fingerprint = str(run_row["input_fingerprint"] or "")
            try:
                config_snapshot = json.loads(
                    run_row["config_snapshot_json"] or "{}"
                )
                if episode_id and not str(
                    config_snapshot.get(
                        "blueprint_budget_lineage_fingerprint"
                    ) or ""
                ):
                    raise StageError(
                        "剧本时空因果蓝图分片",
                        [
                            "[BLUEPRINT_BUDGET_SNAPSHOT_INVALID] "
                            "运行缺少冻结的蓝图预算 lineage"
                        ],
                    )
                retry_grant_id = str(
                    config_snapshot.get("blueprint_retry_grant_id") or ""
                )
                retry_receipts_hash = str(
                    config_snapshot.get("blueprint_retry_receipts_hash") or ""
                )
                input_fingerprint = str(
                    config_snapshot.get(
                        "blueprint_budget_lineage_fingerprint",
                        input_fingerprint,
                    )
                    or input_fingerprint
                )
            except StageError:
                raise
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                if episode_id:
                    raise StageError(
                        "剧本时空因果蓝图分片",
                        [
                            "[BLUEPRINT_BUDGET_SNAPSHOT_INVALID] "
                            "运行蓝图预算 snapshot 损坏"
                        ],
                    ) from exc
    if episode_id and not retry_grant_id:
        try:
            grant_row = get_conn().execute(
                """SELECT r.grant_id
                     FROM production_revisions r
                     JOIN production_grants g ON g.id=r.grant_id
                    WHERE r.episode_id=? AND r.kind='screenplay'
                      AND r.status='active'
                      AND g.revoked_at IS NULL AND g.expires_at>?
                    ORDER BY r.updated_at DESC LIMIT 1""",
                (episode_id, time.time()),
            ).fetchone()
            if grant_row is not None:
                retry_grant_id = str(grant_row["grant_id"] or "")
        except Exception:  # noqa: BLE001 - isolated legacy test schemas
            retry_grant_id = ""
    budget = _BlueprintGenerationBudget.from_durable_calls(
        run_id=run_id,
        started_at_epoch=run_started_at,
        episode_id=episode_id,
        input_fingerprint=input_fingerprint,
        retry_grant_id=retry_grant_id,
    )
    if retry_grant_id and budget.unknown_receipts:
        try:
            # Authorize on the authority facts frozen atomically at activation,
            # NOT on whether the grant's original revision is still the head.
            # ``_spawn_screenplay_activation`` mints this ``user_retry_approval``
            # grant, consumes it, and freezes ``blueprint_retry_grant_id`` /
            # ``blueprint_retry_receipts_hash`` into the run snapshot in one
            # transaction. The baseline task legitimately supersedes that
            # revision (unstable ``input_fingerprint``), which is orthogonal to
            # authority. Requiring ``r.status='active'`` here deadlocked every
            # retry (BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED). Run-scope, the
            # covered receipt set and the input lineage are each frozen and
            # re-verified below; revocation/TTL and single-use (``consumed_at``)
            # stay enforced, so dropping the revision-head join is safe.
            grant_row = get_conn().execute(
                """SELECT g.issued_by,g.input_artifact_hash
                     FROM production_grants g
                    WHERE g.id=? AND g.episode_id=? AND g.kind='screenplay'
                      AND g.issued_by='user_retry_approval'
                      AND g.consumed_at IS NOT NULL
                      AND g.revoked_at IS NULL AND g.expires_at>?""",
                (retry_grant_id, episode_id, time.time()),
            ).fetchone()
            if (
                grant_row is not None
                and str(grant_row["issued_by"] or "") == "user_retry_approval"
                and str(grant_row["input_artifact_hash"] or "")
                == blueprint_retry_receipts_hash(budget.unknown_receipts)
                and (
                    not retry_receipts_hash
                    or retry_receipts_hash
                    == blueprint_retry_receipts_hash(budget.unknown_receipts)
                )
            ):
                budget.authorize_unknown_retry(retry_grant_id)
        except Exception:  # noqa: BLE001 - isolated legacy schemas
            pass
    return budget


def _append_blueprint_semantic_rebuild_event(
    *,
    episode_id: str,
    shard_id: str,
    unresolved: dict[str, list[str]],
    rebuild_no: int,
) -> None:
    """Leave durable evidence that a blueprint was rebuilt for a dead end."""
    from app.evidence import repository as evidence_repository
    from app.observability.tracing import current_trace

    try:
        run_id = current_trace().run_id
    except Exception:  # noqa: BLE001 - evidence must never break generation
        run_id = None
    if not run_id:
        return
    try:
        evidence_repository.append_event(
            run_id,
            "BLUEPRINT_SEMANTIC_REBUILD",
            "warn",
            (
                f"场次语义门禁在 {shard_id} 未收口，按下游证据重建叙事蓝图"
                f"（第 {rebuild_no} 次，上限 "
                f"{SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT} 次）"
            ),
            payload={
                "episode_id": episode_id,
                "shard_id": shard_id,
                "unresolved_semantic_units": unresolved,
                "rebuild_no": rebuild_no,
            },
        )
    except Exception:  # noqa: BLE001 - evidence must never break generation
        return


def _cached_leaf_superseded_by_feedback(
    *,
    cached_source_hash: str,
    source_hash: str,
    source_payload: list[dict[str, Any]],
) -> bool:
    """Whether a cached leaf was built before this activation changed its input.

    A semantic rebuild deliberately injects ``downstream_semantic_conflicts``
    into the affected shard's source payload, which changes ``source_hash`` on
    purpose.  The previous leaf is then simply *not applicable* -- it is not
    authority drift.  Treating it as drift makes the rebuild die in
    ``BLUEPRINT_SPLIT_MANIFEST_AUTHORITY``, i.e. exactly inside the scenario the
    rebuild exists to rescue.  Shards without injected evidence keep the
    original strict drift check unchanged.
    """
    if cached_source_hash == source_hash:
        return False
    return any(
        item.get("downstream_semantic_conflicts")
        for item in source_payload
    )


def _blueprint_shard_source_entry(
    segment: Any,
    semantic_feedback: dict[str, list[str]] | None,
) -> dict[str, Any]:
    """One shard source entry, carrying any downstream semantic dead-end.

    The feedback rides inside the source payload on purpose: that payload is
    hashed into ``source_hash``, so a rebuild neither reuses the cached leaf nor
    replays the same provider operation, and it is serialised into the prompt,
    so the model sees exactly which unit could not be rendered and why.
    """
    entry: dict[str, Any] = {
        "source_segment_id": segment.segment_id,
        "text": segment.text,
        "source_facts": [
            fact.model_dump(mode="json")
            for fact in source_segment_facts(
                segment.segment_id,
                segment.text,
            )
        ],
    }
    prefix = f"{segment.segment_id}:"
    conflicts = {
        unit_key: list(messages)
        for unit_key, messages in (semantic_feedback or {}).items()
        if unit_key.startswith(prefix)
    }
    if conflicts:
        entry["downstream_semantic_conflicts"] = conflicts
    return entry


async def _generate_sharded_narrative_blueprint(
    episode: dict[str, Any],
    source_text: str,
    bible_context: dict[str, Any],
    *,
    generation_budget: _BlueprintGenerationBudget | None = None,
    semantic_feedback: dict[str, list[str]] | None = None,
) -> NarrativeBlueprint:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    segments = index_source_segments(source_text)
    source_corpus_hash = hashlib.sha256(
        source_text.encode("utf-8")
    ).hexdigest()
    cached_rows = get_conn().execute(
        """SELECT id,content_json,content_hash,model_snapshot_json
             FROM artifacts
            WHERE scope_type='episode' AND scope_id=?
              AND type='screenplay_narrative_blueprint_shard'
              AND contract_version=?
              AND prompt_version=? AND status='validated'
            ORDER BY created_at DESC LIMIT 200""",
        (
            str(episode.get("id") or ""),
            BLUEPRINT_VERSION,
            SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        ),
    ).fetchall()
    (
        segment_shards,
        shard_split_depths,
        cached_by_plan_index,
    ) = _blueprint_leaf_plan_from_cache(
        segments,
        list(cached_rows),
        source_corpus_hash=source_corpus_hash,
    )
    if generation_budget is None:
        generation_budget = _blueprint_generation_budget_for_trace(
            current_trace(),
        )
    generation_budget.adopt_shard_plan(len(segment_shards))
    optional_ids = structural_front_matter_ids(segments)
    merged_nodes: list[Any] = []
    shard_index = 1
    while shard_index <= len(segment_shards):
        generation_budget.adopt_shard_plan(len(segment_shards))
        shard_segments = segment_shards[shard_index - 1]
        split_depth = shard_split_depths[shard_index - 1]
        source_ids = [segment.segment_id for segment in shard_segments]
        source_payload = [
            _blueprint_shard_source_entry(segment, semantic_feedback)
            for segment in shard_segments
        ]
        boundary = _blueprint_shard_boundary_context(merged_nodes)
        source_hash = hashlib.sha256(
            json.dumps(
                source_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        boundary_hash = hashlib.sha256(
            json.dumps(
                boundary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        shard: NarrativeBlueprintShard | None = None
        planned_cached = cached_by_plan_index.get(shard_index)
        if planned_cached is not None and _cached_leaf_superseded_by_feedback(
            cached_source_hash=planned_cached[1].source_hash,
            source_hash=source_hash,
            source_payload=source_payload,
        ):
            planned_cached = None
        if planned_cached is not None:
            cached_row, cached = planned_cached
            try:
                cached_snapshot = json.loads(
                    cached_row["model_snapshot_json"] or "{}"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_SNAPSHOT] leaf snapshot 损坏"],
                )
            cached_errors = validate_narrative_blueprint_shard(
                cached,
                expected_episode_no=int(episode["episode_no"]),
                expected_shard_index=shard_index,
                expected_source_segment_ids=source_ids,
                optional_source_segment_ids=optional_ids,
                boundary_state_facts=boundary["active_state_facts"],
                source_text=source_text,
            )
            if not (
                cached_snapshot.get("source_fact_version")
                == SOURCE_FACT_VERSION
                and cached_snapshot.get("shard_policy_version")
                == BLUEPRINT_SHARD_POLICY_VERSION
                and cached_snapshot.get("local_authority_validator_version")
                == BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
                and cached.source_hash == source_hash
                and cached.boundary_hash == boundary_hash
            ):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_AUTHORITY] leaf source/boundary/policy 漂移"],
                )
            if cached_errors:
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_VALIDATION] " + cached_errors[0]],
                )
            shard = cached
            log_provider_call(
                "screenplay_blueprint_shard_local_recompile",
                config.MODEL_TEXT,
                "REUSED",
                None,
                0,
                meta={
                    "episode_id": str(episode.get("id") or ""),
                    "shard_index": shard_index,
                    "artifact_id": str(cached_row["id"]),
                    "split_manifest_version": BLUEPRINT_SPLIT_MANIFEST_VERSION,
                },
            )
        if shard is None:
            errors: list[str] = []
            previous_candidate: dict[str, Any] | None = None
            ownership_repair_issues: list[Any] | None = None
            split_for_truncation = False
            token_budget = _blueprint_shard_token_budget(shard_segments)
            stall_epoch = 0
            attempt = 0
            previous_attempt_repaired_ownership = False
            while attempt < BLUEPRINT_SHARD_MAX_ATTEMPTS:
                attempt += 1
                # The bounded ownership-map mode belongs to the attempt after
                # a repairable candidate actually appeared -- not to attempt 2
                # whatever happened before it.  In production shard 21 of
                # ep_3b07c59c0856 lost attempt 1 to malformed JSON, so the one
                # repair slot was spent before any candidate existed; attempt 2
                # then produced exactly the 15 ownership issues this mode
                # repairs and had to be re-rolled blind instead.  A repair that
                # did not resolve its own issues falls back to a full shard so
                # the remaining budget is never spent re-sending the same map.
                repair_only = bool(
                    previous_candidate is not None
                    and ownership_repair_issues is not None
                    and not previous_attempt_repaired_ownership
                )
                if repair_only:
                    assert previous_candidate is not None
                    assert ownership_repair_issues is not None
                    prompt = _blueprint_state_subject_repair_prompt(
                        previous_candidate=previous_candidate,
                        issues=ownership_repair_issues,
                        source_payload=source_payload,
                        source_text=source_text,
                    )
                else:
                    prompt = _blueprint_shard_prompt(
                        episode_no=int(episode["episode_no"]),
                        shard_index=shard_index,
                        shard_count=len(segment_shards),
                        errors=errors,
                        bible_context=bible_context,
                        boundary=boundary,
                        source_payload=source_payload,
                        previous_candidate=previous_candidate,
                    )
                provider, model, effective_max_tokens = (
                    hiagent.text_request_token_limits(
                        requested_max_tokens=token_budget,
                    )
                )
                operation_id = _blueprint_provider_operation_id(
                    episode_id=str(episode.get("id") or ""),
                    shard_index=shard_index,
                    attempt=attempt,
                    stall_epoch=stall_epoch,
                    split_depth=split_depth,
                    source_hash=source_hash,
                    boundary_hash=boundary_hash,
                    prompt=prompt,
                    provider=provider,
                    model=model,
                    max_tokens=token_budget,
                    effective_max_tokens=effective_max_tokens,
                    temperature=0.15,
                    provider_semantic_settings=(
                        hiagent.text_request_semantic_settings(provider)
                    ),
                )
                reservation_id = generation_budget.claim(
                    max_tokens=effective_max_tokens,
                    requested_max_tokens=token_budget,
                    operation_id=operation_id,
                )
                durable_replay = bool(
                    operation_id
                    in generation_budget._durable_successful_operations
                )
                remaining_seconds = (
                    generation_budget.max_wall_seconds
                    if durable_replay
                    else generation_budget.remaining_seconds()
                )
                settlement: dict[str, Any] | None = None
                try:
                    raw = await asyncio.wait_for(
                        model_gateway.chat(
                            [
                                {"role": "system", "content": SYSTEM_PREFIX},
                                {"role": "user", "content": prompt},
                            ],
                            temperature=0.15,
                            max_tokens=token_budget,
                            call_meta={
                                "stage": "剧本时空因果蓝图分片",
                                "stage_key": "screenplay_blueprint_shard",
                                "episode_id": str(episode.get("id") or ""),
                                "shard_index": shard_index,
                                "shard_count": len(segment_shards),
                                "attempt": attempt,
                                "response_mode": (
                                    "ownership_repair"
                                    if repair_only
                                    else "full_shard"
                                ),
                                "split_depth": split_depth,
                                "source_count": len(source_ids),
                                "source_fact_count": sum(
                                    len(item["source_facts"])
                                    for item in source_payload
                                ),
                                "expected_json": True,
                                "operation_id": operation_id,
                                "production_grant_id": (
                                    generation_budget.retry_grant_id
                                ),
                                "reuse_successful_operation": True,
                                "require_cached_successful_operation": (
                                    durable_replay
                                ),
                                "contract_version": BLUEPRINT_VERSION,
                                "disable_reasoning_fallback": True,
                                "disable_provider_retries": True,
                                "disable_provider_candidate_fallback": True,
                            },
                            usage_callback=lambda usage_event: (
                                generation_budget.record_usage(
                                    reservation_id,
                                    usage_event,
                                )
                            ),
                        ),
                        timeout=max(0.001, remaining_seconds),
                    )
                except hiagent.ProviderError as exc:
                    settlement = generation_budget.settle(
                        reservation_id,
                        unreported_outcome=(
                            "not_sent"
                            if exc.delivery_state == "not_sent"
                            and exc.replay_safe
                            else "unknown"
                        ),
                    )
                    if (
                        exc.failure_kind
                        == hiagent.ProviderFailureKind.OUTPUT_TRUNCATED.value
                        and len(shard_segments) > 1
                        and split_depth < BLUEPRINT_GENERATION_MAX_SPLIT_DEPTH
                    ):
                        split_for_truncation = True
                        break
                    if (
                        stall_epoch < BLUEPRINT_SHARD_MAX_STALL_RETRIES
                        and hiagent.provider_answer_undelivered(exc)
                    ):
                        # An answer the provider never delivered -- a stall
                        # before the first character, or a stream cut before its
                        # own ``[DONE]`` -- is not a completed generation:
                        # nothing was authored, so there is no candidate to
                        # preserve and nothing to re-roll.  It replays the same
                        # semantic attempt out of its own bounded budget instead
                        # of consuming one -- a stall landing on the last attempt
                        # used to kill the episode having delivered zero
                        # characters.  The fresh stall epoch gives the retry a
                        # distinct operation id, so the budget does not treat it
                        # as replaying the same unknown outcome.
                        stall_epoch += 1
                        attempt -= 1
                        continue
                    raise
                except asyncio.TimeoutError as exc:
                    settlement = generation_budget.settle(reservation_id)
                    raise StageError(
                        "剧本时空因果蓝图分片",
                        ["[BLUEPRINT_GENERATION_TIME_BUDGET] provider调用超过全局时间上限"],
                    ) from exc
                except BaseException:
                    generation_budget.settle(reservation_id)
                    raise
                else:
                    settlement = generation_budget.settle(reservation_id)
                assert settlement is not None
                trace = current_trace()
                evidence_repository.create_artifact(
                    EvidenceArtifact(
                        type="screenplay_narrative_blueprint_shard_raw",
                        scope_type="episode",
                        scope_id=str(episode.get("id") or ""),
                        status="candidate",
                        trust_level="T0",
                        content={
                            "raw_output": raw,
                            "shard_index": shard_index,
                            "attempt": attempt,
                            "response_mode": (
                                "ownership_repair"
                                if repair_only
                                else "full_shard"
                            ),
                            "source_hash": source_hash,
                            "boundary_hash": boundary_hash,
                            "source_fact_version": SOURCE_FACT_VERSION,
                            "shard_policy_version": (
                                BLUEPRINT_SHARD_POLICY_VERSION
                            ),
                            "split_manifest_version": (
                                BLUEPRINT_SPLIT_MANIFEST_VERSION
                            ),
                            "split_depth": split_depth,
                            "requested_max_tokens": token_budget,
                            "token_settlement": settlement,
                        },
                        contract_version=BLUEPRINT_VERSION,
                        prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    ),
                    step_run_id=trace.step_run_id,
                )
                try:
                    provider_payload = extract_json(
                        raw,
                        repair_unescaped_inner_quotes=True,
                    )
                    if repair_only:
                        assert previous_candidate is not None
                        assert ownership_repair_issues is not None
                        patch = (
                            BlueprintStateSubjectOwnershipPatch.model_validate(
                                provider_payload
                            )
                        )
                        candidate = (
                            apply_blueprint_state_subject_ownership_patch(
                                previous_candidate,
                                patch,
                                target_unit_keys=(
                                    _blueprint_state_subject_repair_target_keys(
                                        ownership_repair_issues
                                    )
                                ),
                                source_text=source_text,
                            )
                        )
                    else:
                        normalized_payload = (
                            normalize_blueprint_provider_payload(
                                provider_payload,
                            )
                        )
                        if previous_candidate and isinstance(
                            normalized_payload,
                            dict,
                        ):
                            normalized_payload = _freeze_unreported_voice_pairs(
                                normalized_payload,
                                previous_candidate=previous_candidate,
                                validation_errors=errors,
                            )
                            normalized_payload = (
                                normalize_blueprint_provider_payload(
                                    normalized_payload,
                                )
                            )
                        candidate = NarrativeBlueprintShard.model_validate(
                            normalized_payload,
                        )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    errors = [f"[BLUEPRINT_SHARD_JSON] {exc}"]
                    continue
                if not repair_only:
                    candidate.source_hash = source_hash
                    candidate.boundary_hash = boundary_hash
                    candidate.source_segment_ids = source_ids
                    _normalize_blueprint_shard_structure(
                        candidate,
                        boundary_context=boundary,
                        attempt=attempt,
                        previous_candidate=previous_candidate,
                        previous_validation_errors=errors,
                    )
                    _namespace_blueprint_shard(candidate)
                    if previous_candidate is not None:
                        _freeze_unreported_state_subject_ownership(
                            candidate,
                            previous_candidate=previous_candidate,
                            validation_errors=errors,
                        )
                # Returns a count of removed keys; the in-place normalization
                # on ``candidate`` is the intended effect.
                _removed_state_subject_keys = (
                    normalize_blueprint_state_subject_evidence_projection(
                        candidate,
                        source_text,
                    )
                )
                # A unit that already resolves to exactly one state subject
                # implies its own perception evidence; the merged-blueprint
                # repair loop has always settled that locally.  Running the
                # same normalization per shard keeps the contract identical and
                # stops every shard from spending a whole ownership-repair
                # round trip restating an owner it had already chosen.
                normalize_blueprint_state_subject_perception(candidate)
                previous_candidate = candidate.model_dump(mode="json")
                errors = validate_narrative_blueprint_shard(
                    candidate,
                    expected_episode_no=int(episode["episode_no"]),
                    expected_shard_index=shard_index,
                    expected_source_segment_ids=source_ids,
                    optional_source_segment_ids=optional_ids,
                    boundary_state_facts=boundary[
                        "active_state_facts"
                    ],
                    source_text=source_text,
                )
                if not errors:
                    shard = candidate
                    evidence_repository.create_artifact(
                        EvidenceArtifact(
                            type="screenplay_narrative_blueprint_shard",
                            scope_type="episode",
                            scope_id=str(episode.get("id") or ""),
                            status="validated",
                            trust_level="T1",
                            content=shard.model_dump(mode="json"),
                            contract_version=BLUEPRINT_VERSION,
                            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                            model_snapshot={
                                "shard_index": shard_index,
                                "source_fact_version": SOURCE_FACT_VERSION,
                                "source_count": len(source_ids),
                                "source_fact_count": sum(
                                    len(item["source_facts"])
                                    for item in source_payload
                                ),
                                "shard_policy_version": (
                                    BLUEPRINT_SHARD_POLICY_VERSION
                                ),
                                "local_authority_validator_version": (
                                    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
                                ),
                                "split_manifest_version": (
                                    BLUEPRINT_SPLIT_MANIFEST_VERSION
                                ),
                                "source_corpus_hash": source_corpus_hash,
                                "split_depth": split_depth,
                                "requested_max_tokens": token_budget,
                                "actual_completion_tokens": settlement[
                                    "actual_completion_tokens"
                                ],
                                "usage_reported": settlement[
                                    "usage_reported"
                                ],
                                "charged_output_tokens": settlement[
                                    "charged_output_tokens"
                                ],
                                "global_charged_output_tokens": settlement[
                                    "global_charged_output_tokens"
                                ],
                            },
                        ),
                        step_run_id=trace.step_run_id,
                    )
                    break
                previous_attempt_repaired_ownership = repair_only
                ownership_repair_issues = (
                    _blueprint_state_subject_repair_issues(
                        candidate,
                        validation_errors=errors,
                        source_text=source_text,
                    )
                )
            if split_for_truncation:
                split = _split_blueprint_segments(shard_segments)
                segment_shards[shard_index - 1:shard_index] = split
                shard_split_depths[shard_index - 1:shard_index] = [
                    split_depth + 1,
                    split_depth + 1,
                ]
                cached_by_plan_index = {
                    (index + 1 if index > shard_index else index): value
                    for index, value in cached_by_plan_index.items()
                    if index != shard_index
                }
                continue
            if shard is None:
                if (
                    len(shard_segments) > 1
                    and split_depth < BLUEPRINT_GENERATION_MAX_SPLIT_DEPTH
                ):
                    split = _split_blueprint_segments(shard_segments)
                    segment_shards[shard_index - 1:shard_index] = split
                    shard_split_depths[shard_index - 1:shard_index] = [
                        split_depth + 1,
                        split_depth + 1,
                    ]
                    cached_by_plan_index = {
                        (index + 1 if index > shard_index else index): value
                        for index, value in cached_by_plan_index.items()
                        if index != shard_index
                    }
                    continue
                raise StageError(
                    f"剧本时空因果蓝图分片 {shard_index}",
                    errors[:10],
                )
        merged_nodes.extend(shard.nodes)
        shard_index += 1
    blueprint = NarrativeBlueprint(
        episode_no=int(episode["episode_no"]),
        nodes=merged_nodes,
    )
    normalize_blueprint_fact_versions(blueprint)
    normalize_blueprint_requirement_state_keys(blueprint)
    errors = validate_narrative_blueprint(blueprint, source_text)
    if errors:
        blueprint = await _repair_narrative_blueprint(
            blueprint,
            episode=episode,
            source_text=source_text,
            generation_budget=generation_budget,
        )
    derive_blueprint_scene_plans(blueprint)
    trace = current_trace()
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="validated",
            trust_level="T1",
            content=blueprint.model_dump(mode="json"),
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot=_current_blueprint_authority_snapshot(
                source_text,
                generation_mode="source_shards",
                generation_budget=generation_budget,
                shard_count=len(segment_shards),
            ),
        ),
        step_run_id=trace.step_run_id,
    )
    return blueprint


async def _generate_screenplay_narrative_blueprint(
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    *,
    semantic_feedback: dict[str, list[str]] | None = None,
) -> NarrativeBlueprint:
    from app.observability.tracing import current_trace

    trace = current_trace()
    generation_budget = _blueprint_generation_budget_for_trace(
        trace,
        episode_id=str(episode.get("id") or ""),
    )
    current_run = get_conn().execute(
        "SELECT input_fingerprint FROM workflow_runs WHERE id=?",
        (trace.run_id,),
    ).fetchone()
    # 带着下游死结重建时绝不能复用上一版蓝图：那份蓝图正是死结的来源。
    if semantic_feedback:
        current_run = None
    if current_run is not None:
        rows = get_conn().execute(
            """SELECT a.content_json,a.content_hash
                 FROM artifacts a
                 JOIN step_runs sr ON sr.id=a.created_by_step_run_id
                 JOIN workflow_runs wr ON wr.id=sr.run_id
                WHERE a.scope_type='episode' AND a.scope_id=?
                  AND a.type='screenplay_narrative_blueprint'
                  AND a.status='validated'
                  AND a.contract_version=?
                  AND a.prompt_version=?
                  AND wr.input_fingerprint=?
                ORDER BY a.created_at DESC LIMIT 10""",
            (
                str(episode.get("id") or ""),
                BLUEPRINT_VERSION,
                SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                str(current_run["input_fingerprint"] or ""),
            ),
        ).fetchall()
        for row in rows:
            try:
                content = json.loads(row["content_json"] or "{}")
                if not _artifact_json_content_is_sealed(row, content):
                    continue
                raw = (
                    content.get("raw_output")
                    if isinstance(content, dict)
                    else None
                )
                if isinstance(raw, str):
                    try:
                        payload = extract_json(
                            normalize_blueprint_raw_json(raw),
                            repair_unescaped_inner_quotes=True,
                        )
                    except ValueError:
                        payload = recover_complete_blueprint_prefix(raw)
                else:
                    payload = content
                recovered = NarrativeBlueprint.model_validate(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            normalize_blueprint_fact_versions(recovered)
            normalize_blueprint_requirement_state_keys(recovered)
            log_provider_call(
                "screenplay_blueprint_local_recompile",
                config.MODEL_TEXT,
                "REUSED",
                None,
                0,
                meta={
                    "episode_id": str(episode.get("id") or ""),
                    "contract_version": BLUEPRINT_VERSION,
                    "prompt_version": SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                },
            )
            if validate_narrative_blueprint(recovered, source_text):
                recovered = await _repair_narrative_blueprint(
                    recovered,
                    episode=episode,
                    source_text=source_text,
                    generation_budget=generation_budget,
                )
            derive_blueprint_scene_plans(recovered)
            return await _semantic_review_narrative_blueprint(
                recovered,
                episode=episode,
                source_text=source_text,
                generation_budget=generation_budget,
            )

    source_with_ids = render_indexed_source(source_text)
    bible_context = screenplay_ir_bible_context(
        bible,
        source_text=source_text,
        episode_no=int(episode["episode_no"]),
        character_resolutions=list(
            episode.get("character_resolutions") or []
        ),
    )
    candidate = await _generate_sharded_narrative_blueprint(
        episode,
        source_text,
        bible_context,
        generation_budget=generation_budget,
        semantic_feedback=semantic_feedback,
    )
    return await _semantic_review_narrative_blueprint(
        candidate,
        episode=episode,
        source_text=source_text,
        generation_budget=generation_budget,
    )

    prompt = f"""任务：先为第 {episode['episode_no']} 集建立写作前叙事蓝图。

这一步不写剧本台词和场景正文，只识别原文中不可机械判断的时间、空间、行动因果、
人物位置、持久状态和重大决定依据。后端会依据节点的时间域与单一地点确定性分场，
再让剧本阶段严格消费分场结果。

硬规则：
1. 按原文顺序覆盖每个非标题 SRC。单节点最多绑定
   {BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE} 个连续 SRC，不得用大节点掩盖事件。
1a. 每个节点必须显式输出 narrative_layer/event_priority/render_policy。
   可表演且形成画面状态变化的故事语义只能使用 story+causal+standalone；
   仅保留完整来源审计、不进入成片的旁文本只能使用
   paratext+connective+exclude_from_spine。不得按 SRC 编号、章节位置、
   characters 是否为空或文本关键词分类。
   paratext 节点的 participants、participant_evidence、
   environment_source_unit_keys、source_unit_deliveries、state_requirements、
   state_changes、released_constraints_for 必须为空列表，decision=null，
   exit_state=空字符串。标题可见文字只放 summary/action_logic/opening_image，
   不得生成 written_text delivery。
2. temporal_domain_key 表示同一连续时间域；回忆必须明确 flashback_enter、
   flashback_continue、flashback_exit。次日、当晚、数日后和蒙太奇必须使用正确
   time_relation，并提供观众可见/可听的 transition_cue。
3. 每节点只有一个主要 location_key/location_label。人物改变地点时，transition_cue
   必须说明走路、乘车、下车、进入房间、字幕或匹配剪辑，禁止瞬移。
   location_label 禁止使用「/」「、」「+」「内外」合并大堂/房间、里间/外间、
   车站/车厢等多个空间。只有地点变化发生在两个 SRC 之间时才能在该 SRC 边界拆节点；
   同一 SRC 内跨越多个地点时仍保持一个节点，只填写核心因果进程的一个主要地点，
   移动写入 transition_cue/action_logic，禁止复合地点和拆 SRC。
   同一 SRC 只能归属一个程序分场；其他场需要该信息时，
   必须通过 state_requirements、decision.setup_node_keys 或 transition_cue 建立
   显式派生关系，不得重复消费原 SRC。
4. 对后文会复用的持久事实建立 state_changes/state_requirements，包括但不限于：
   车辆所有者与司机、人物所在位置、住宿分配、房间结构、关键物品、掩护动作、
   谁知道什么。每个 state_change 必须建立本集唯一且递增的 fact_key（F001...）；
   后续 requirement 必须用 required_fact_key 精确引用此前仍有效的事实，禁止重新用
   自由文本描述一个“差不多”的状态。只有人物谱或前集已明确建立、但本集原文没有
   建立节点的事实才可设 assumed_prior=true，并写清审计依据；不得把为了推动剧情
   临时发明的同谋、开放关系、满房等设定标成 assumed_prior。事实默认并存；司机、
   住宿分配、人物位置等互斥事实发生替换时，必须在新事实的 supersedes_fact_keys
   中明确列出被替代事实。
5. major decision 必须通过 setup_node_keys 引用此前已经发生的压力、欲望、认知或关系
   节点，并写清 pressure/desire。禁止“受一次刺激立即性格突变”。
6. agency_mode 必须区分 voluntary、reluctant、coerced、incapacitated、unclear。
   武器、威胁或失去行为能力不能同时写成自主选择；若自主性后来变化，必须另建节点，
   并提供明确 agency_change_reason 和可见心理过程。coerced/incapacitated 决定必须
   用 constraint_fact_key 引用本节点建立且仍有效的约束事实。从该状态恢复为
   voluntary 时，constraint_release_node_keys 必须引用发生在两次决定之间、真正解除
   武器/威胁/无行为能力约束的节点；该节点还必须把角色 key 写入
   released_constraints_for，并用 state_change.supersedes_fact_keys 终止原约束事实。
   产生快感、停止反抗或自我说服不等于约束解除。
7. scene_boundary_before 只标记创作上必须切场的额外边界。时间域、地点、回忆进出变化
   后端本身就会自动切场。scene_plans 留空，禁止由模型决定场次编号和标题。
8. summary/action_logic 必须交代“为何发生、如何到达、动作完成后改变了什么”，
   不能只罗列事件。不得为修补逻辑发明违背原文的事实；仅当原文本身存在明确矛盾、
   且不补桥就无法成片时，才可使用 adaptation_kind=logic_bridge，并在
   bridge_rationale 中说明必要性及如何保持核心事件/结果；普通视觉过桥使用
   transition_cue，不能冒充剧情事实。
9. 每个 projection=action 的 prose source unit 必须有 typed 状态归属。
   单主体思考、反应、发问或动作在 participant_evidence 中填一条
   usage=state_subject，并用 source_unit_keys 精确绑定该 unit；结构标点切分后
   仍不可拆的共同动作填一条 mode=joint 的 state_subject_assignments，
   identity_keys 列出全部共同主体；真正无人物
   状态所有者的环境单元才写入 environment_source_unit_keys。visible、
   scene roster、content_owner 或文本姓名均不能作为主体推断。
10. participants 中每个 identity 都必须至少有一条 identity_key 完全相同的
    participant_evidence，且 source_segment_ids 非空并只引用本节点 owned SRC；
    不得仅列 roster，不得默认角色。

本集概要：{episode.get('synopsis') or '（无）'}
人物与场景上下文：
{json.dumps(bible_context, ensure_ascii=False, separators=(",", ":"))}

带稳定段 ID 的授权原文：
{_render_screenplay_source(source_with_ids)}

只输出 JSON，不要解释：
程序所有权摘要：
{json.dumps(blueprint_prompt_contract(), ensure_ascii=False)}
完整输出 Schema：
{json.dumps(NarrativeBlueprint.model_json_schema(), ensure_ascii=False)}
"""
    loop = AgentLoop(
        stage_key="screenplay_blueprint",
        contract_key="screenplay",
        goal=f"建立第 {episode['episode_no']} 集叙事时空与因果蓝图",
        scope_type="episode",
        scope_id=str(
            episode.get("id") or f"episode-{episode['episode_no']}"
        ),
        artifact_type="screenplay_narrative_blueprint",
        prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        policy=AgentLoopPolicy(
            max_iterations=1,
            stall_rounds=2,
            min_quality_gain=0.01,
            no_gain_rounds=2,
            allow_warning_candidate=False,
            repair_all_blockers=True,
        ),
    )
    try:
        candidate = await _run_with_agent_loop(
            "剧本时空因果蓝图",
            "screenplay_blueprint",
            prompt,
            NarrativeBlueprint,
            lambda value: validate_narrative_blueprint(
                value,
                source_text,
            ),
            loop=loop,
            temperature=0.2,
            max_tokens=20480,
            repair_user_prompt_limit=None,
            repair_candidate_limit=None,
            prefill={
                "format_version": BLUEPRINT_VERSION,
                "episode_no": episode["episode_no"],
            },
        )
    except AgentLoopFailure:
        from app.observability.tracing import current_trace

        trace = current_trace()
        row = get_conn().execute(
            """SELECT a.content_json,a.content_hash
                 FROM artifacts a
                 JOIN step_runs sr ON sr.id=a.created_by_step_run_id
                WHERE a.scope_type='episode' AND a.scope_id=?
                  AND a.type='screenplay_narrative_blueprint'
                  AND a.prompt_version=?
                  AND sr.run_id=?
                ORDER BY a.created_at DESC LIMIT 1""",
            (
                str(episode.get("id") or ""),
                SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                trace.run_id,
            ),
        ).fetchone()
        if row is None:
            raise
        fallback_content = json.loads(row["content_json"] or "{}")
        if not _artifact_json_content_is_sealed(row, fallback_content):
            raise
        candidate = NarrativeBlueprint.model_validate(fallback_content)
        candidate = await _repair_narrative_blueprint(
            candidate,
            episode=episode,
            source_text=source_text,
        )
    derive_blueprint_scene_plans(candidate)
    return await _semantic_review_narrative_blueprint(
        candidate,
        episode=episode,
        source_text=source_text,
    )


def _save_screenplay_generation_checkpoint(
    episode_id: str,
    phase: str,
    **values: Any,
) -> None:
    """Persist resumable pre-Document state without changing baseline_done."""
    from app.production.revision import (
        get_active_production_revision,
        save_checkpoint,
    )

    revision = get_active_production_revision(episode_id, "screenplay")
    if revision is None or revision.baseline_done:
        return
    checkpoint = dict(revision.checkpoint_json or {})
    save_checkpoint(revision.id, {
        **checkpoint,
        "phase": phase,
        **values,
    })


def _commit_blueprint_authority_checkpoint(
    *,
    episode_id: str,
    blueprint_artifact_id: str,
    blueprint_hash: str,
    source_text: str,
) -> None:
    """Atomically checkpoint current Blueprint authority and resolve its receipts."""
    from app.observability.tracing import current_trace

    trace = current_trace()
    run_id = str(trace.run_id or "")
    if not run_id:
        _save_screenplay_generation_checkpoint(
            episode_id,
            "IDENTITY_FREEZE",
            blueprint_artifact_id=blueprint_artifact_id,
            blueprint_hash=blueprint_hash,
            yield_reason=None,
        )
        return
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if owner is None or str(owner["active_screenplay_run_id"] or "") != run_id:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_OWNER_DRIFT] 当前运行已失去剧集写权"],
            )
        revision = conn.execute(
            """SELECT id,checkpoint_json,grant_id FROM production_revisions
                 WHERE episode_id=? AND kind='screenplay' AND status='active'
                 ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        if revision is None:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_REVISION_MISSING] 缺少active revision"],
            )
        artifact = conn.execute(
            """SELECT content_json,content_hash,model_snapshot_json FROM artifacts
                 WHERE id=? AND type='screenplay_narrative_blueprint'
                   AND scope_type='episode' AND scope_id=?
                   AND status='validated' AND contract_version=?
                   AND prompt_version=?""",
            (
                blueprint_artifact_id,
                episode_id,
                BLUEPRINT_VERSION,
                SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            ),
        ).fetchone()
        if artifact is None:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_ARTIFACT_INVALID] current Blueprint artifact失效"],
            )
        snapshot = json.loads(artifact["model_snapshot_json"] or "{}")
        artifact_content = json.loads(artifact["content_json"] or "{}")
        if not _artifact_json_content_is_sealed(artifact, artifact_content):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_ARTIFACT_HASH] current Blueprint 内容指纹漂移"],
            )
        artifact_blueprint = NarrativeBlueprint.model_validate(artifact_content)
        artifact_blueprint_hash = _narrative_blueprint_content_hash(
            artifact_blueprint
        )
        artifact_errors = validate_narrative_blueprint(
            artifact_blueprint,
            source_text,
        )
        try:
            artifact_plans = derive_blueprint_scene_plans(artifact_blueprint)
            artifact_errors.extend(
                validate_blueprint_scene_partition(
                    artifact_blueprint,
                    artifact_plans,
                )
            )
        except (
            BlueprintSourceOccurrenceError,
            BlueprintSourceOwnershipError,
            ValueError,
        ) as exc:
            artifact_errors.extend(
                getattr(exc, "errors", None) or [str(exc)]
            )
        if (
            artifact_blueprint_hash != blueprint_hash
            or not _blueprint_authority_snapshot_is_current(
                snapshot,
                source_text,
            )
            or artifact_errors
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                [
                    "[BLUEPRINT_AUTHORITY_SNAPSHOT_DRIFT] "
                    "Blueprint authority版本或语义漂移"
                ] + artifact_errors[:10],
            )
        checkpoint = json.loads(revision["checkpoint_json"] or "{}")
        checkpoint.update({
            "phase": "IDENTITY_FREEZE",
            "blueprint_artifact_id": blueprint_artifact_id,
            "blueprint_hash": blueprint_hash,
            "yield_reason": None,
        })
        changed = conn.execute(
            "UPDATE production_revisions SET checkpoint_json=?,updated_at=? "
            "WHERE id=? AND status='active' AND grant_id IS ?",
            (
                json.dumps(checkpoint, ensure_ascii=False),
                time.time(),
                str(revision["id"]),
                revision["grant_id"],
            ),
        )
        if changed.rowcount != 1:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_CHECKPOINT_CAS] revision authority漂移"],
            )

        run = conn.execute(
            "SELECT input_fingerprint,config_snapshot_json FROM workflow_runs "
            "WHERE id=? AND scope_type='episode' AND scope_id=?",
            (run_id, episode_id),
        ).fetchone()
        config_snapshot = json.loads(run["config_snapshot_json"] or "{}")
        receipts = config_snapshot.get("blueprint_retry_receipts") or []
        pinned_hash = str(
            config_snapshot.get("blueprint_retry_receipts_hash") or ""
        )
        grant_id = str(config_snapshot.get("blueprint_retry_grant_id") or "")
        if not receipts and not pinned_hash and not grant_id:
            conn.commit()
            return
        if (
            not isinstance(receipts, list)
            or not receipts
            or blueprint_retry_receipts_hash(receipts) != pinned_hash
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_RECEIPTS_DRIFT] retry receipts snapshot漂移"],
            )
        if str(revision["grant_id"] or "") != grant_id:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_GRANT_DRIFT] retry grant authority漂移"],
            )
        grant = conn.execute(
            """SELECT 1 FROM production_grants
                WHERE id=? AND episode_id=? AND kind='screenplay'
                  AND production_revision_id=?
                  AND issued_by='user_retry_approval'
                  AND input_artifact_hash=? AND consumed_at IS NOT NULL
                  AND revoked_at IS NULL AND expires_at>?""",
            (
                grant_id,
                episode_id,
                str(revision["id"]),
                pinned_hash,
                time.time(),
            ),
        ).fetchone()
        if grant is None:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_GRANT_INVALID] pinned retry grant失效"],
            )
        exact_ids = [int(item.get("call_id") or 0) for item in receipts]
        if any(call_id <= 0 for call_id in exact_ids) or len(exact_ids) != len(set(exact_ids)):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_RECEIPTS_INVALID] retry call IDs无效"],
            )
        artifact_hash = str(artifact["content_hash"] or "")
        operation_id = "blueprint-resolution:" + hashlib.sha256(
            f"{blueprint_artifact_id}:{artifact_hash}:{run_id}:{grant_id}:{pinned_hash}".encode()
        ).hexdigest()
        resolution = conn.execute(
            "SELECT id FROM provider_calls WHERE operation_id=? "
            "AND kind='blueprint_authority_resolution'",
            (operation_id,),
        ).fetchone()
        resolution_id = int(resolution["id"]) if resolution is not None else 0
        # Reconstruct the authority receipts with the same durable resolver
        # used by preflight/runtime.  Old provider rows may have lossy meta,
        # NULL request hashes, and no durable grant column; the resolver's
        # narrow BASELINE-event bridge is the only authority allowed to infer
        # those legacy fields.  On crash replay, include receipts already
        # terminalized by this exact deterministic resolution.
        durable_budget = _BlueprintGenerationBudget.from_durable_calls(
            run_id=run_id,
            episode_id=episode_id,
            input_fingerprint=str(run["input_fingerprint"] or ""),
            retry_grant_id=grant_id,
            include_resolved_by_call_id=(resolution_id or None),
        )
        canonical_receipts = durable_budget.unknown_receipts
        if (
            canonical_receipts != receipts
            or blueprint_retry_receipts_hash(canonical_receipts) != pinned_hash
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_RECEIPTS_DRIFT] durable retry receipts漂移"],
            )
        placeholders = ",".join("?" for _ in exact_ids)
        rows = conn.execute(
            f"""SELECT pc.id,pc.status,pc.superseded_by_call_id,
                       pc.recovery_disposition,pc.operation_id,
                       pc.request_hash,pc.production_grant_id,pc.meta,
                       wr.input_fingerprint
                  FROM provider_calls pc
                  JOIN workflow_runs wr ON wr.id=pc.run_id
                 WHERE pc.id IN ({placeholders})
                   AND wr.scope_type='episode' AND wr.scope_id=?""",
            (*exact_ids, episode_id),
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        for receipt in receipts:
            call_id = int(receipt["call_id"])
            row = by_id.get(call_id)
            meta = json.loads(row["meta"] or "{}") if row is not None else {}
            already_exact = bool(
                resolution_id
                and row is not None
                and int(row["superseded_by_call_id"] or 0) == resolution_id
                and str(row["recovery_disposition"] or "")
                == "SUPERSEDED_BY_VALIDATED_BLUEPRINT_REBUILD"
            )
            if (
                row is None
                or str(row["status"] or "") not in {"INTERRUPTED", "RUNNING"}
                or (
                    row["superseded_by_call_id"] is not None
                    and not already_exact
                )
                or str(row["input_fingerprint"] or "")
                != str(run["input_fingerprint"] or "")
                or str(meta.get("stage_key") or "")
                != str(receipt.get("stage_key") or "")
                or str(row["operation_id"] or "")
                != str(receipt.get("operation_id") or "")
                or str(row["request_hash"] or "")
                != str(receipt.get("request_hash") or "")
            ):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    [f"[BLUEPRINT_RESOLUTION_RECEIPT_CAS] call {call_id} authority漂移"],
                )
        if resolution is None:
            cursor = conn.execute(
                """INSERT INTO provider_calls(
                       ts,kind,model,status,latency_ms,contract_version,
                       production_grant_id,response_json,meta,run_id,step_run_id,
                       operation_id,attempt_no,recovery_disposition
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), "blueprint_authority_resolution",
                    "deterministic", "OK", 0, BLUEPRINT_VERSION, grant_id,
                    json.dumps({
                        "artifact_id": blueprint_artifact_id,
                        "artifact_hash": artifact_hash,
                        "receipts_hash": pinned_hash,
                    }, sort_keys=True, separators=(",", ":")),
                    json.dumps({
                        "stage_key": "screenplay_blueprint_resolution",
                        "episode_id": episode_id,
                    }, sort_keys=True, separators=(",", ":")),
                    run_id, trace.step_run_id, operation_id, 1,
                    "VALIDATED_BLUEPRINT_AUTHORITY",
                ),
            )
            resolution_id = int(cursor.lastrowid)
        else:
            resolution_row = conn.execute(
                """SELECT status,run_id,production_grant_id,response_json,
                          contract_version,recovery_disposition
                     FROM provider_calls WHERE id=?""",
                (resolution_id,),
            ).fetchone()
            resolution_response: dict[str, Any] = {}
            if resolution_row is not None:
                try:
                    resolution_response = json.loads(
                        resolution_row["response_json"] or "{}"
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            if (
                resolution_row is None
                or str(resolution_row["status"] or "") != "OK"
                or str(resolution_row["run_id"] or "") != run_id
                or str(resolution_row["production_grant_id"] or "")
                != grant_id
                or str(resolution_row["contract_version"] or "")
                != BLUEPRINT_VERSION
                or str(resolution_row["recovery_disposition"] or "")
                != "VALIDATED_BLUEPRINT_AUTHORITY"
                or str(resolution_response.get("artifact_id") or "")
                != blueprint_artifact_id
                or str(resolution_response.get("artifact_hash") or "")
                != artifact_hash
                or str(resolution_response.get("receipts_hash") or "")
                != pinned_hash
            ):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_RESOLUTION_RECEIPT_INVALID] resolution receipt漂移"],
                )
        for call_id in exact_ids:
            cursor = conn.execute(
                "UPDATE provider_calls SET superseded_by_call_id=?,"
                "recovery_disposition='SUPERSEDED_BY_VALIDATED_BLUEPRINT_REBUILD' "
                "WHERE id=? AND status IN ('INTERRUPTED','RUNNING') "
                "AND superseded_by_call_id IS NULL",
                (resolution_id, call_id),
            )
            if cursor.rowcount == 0:
                exact = conn.execute(
                    "SELECT 1 FROM provider_calls WHERE id=? "
                    "AND superseded_by_call_id=? "
                    "AND recovery_disposition="
                    "'SUPERSEDED_BY_VALIDATED_BLUEPRINT_REBUILD'",
                    (call_id, resolution_id),
                ).fetchone()
                if exact is None:
                    raise StageError(
                        "剧本时空因果蓝图分片",
                        ["[BLUEPRINT_RESOLUTION_PARTIAL] retry receipts未精确终结"],
                    )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


async def _run_screenplay_workflow_step(
    step_key: str,
    operation: Callable[[], Any],
    *,
    agent_name: str,
    context_manifest: dict[str, Any] | None = None,
) -> Any:
    """Expose each pre-document generation phase as a persisted workflow step."""
    from app.observability.tracing import current_trace

    trace = current_trace()
    if not trace.run_id:
        return await operation()
    from app.orchestration.engine import WorkflowRecorder

    _step_id, result = await WorkflowRecorder(trace.run_id).step(
        step_key,
        operation,
        agent_name=agent_name,
        context_manifest=context_manifest,
    )
    return result


def _clear_ungrounded_ending_hook(
    script: EpisodeScreenplay,
    *,
    episode_id: str,
    source: str,
) -> None:
    """ending_hook 溯源判定失败时清空，并把判定证据写成可查的观测事件。

    背景：这条清空动作以前是完全静默的（app/stages.py 两处、
    app/production/publish.py 一处直接 `script.ending_hook = ""`，不留任何
    痕迹）——数据上无法区分"原文真的没钩子（合法留空）"和"被误杀"。EP4 的
    269 条原子事件把结尾拆得极细，导致旧的单事件判据误杀了一条模型正确、
    忠实原文的钩子；这次要不是人工追问 EP4 为什么慢，根本发现不了。QA
    校验（validate_screenplay_narrative 里的 ending_hook 分支）只在
    "非空但过短"时报错，对"被清空为空"完全无感，指望不上它兜底。

    这里复用 ending_hook_grounding_report 而不是 ending_hook_is_grounded：
    两层判据的实测覆盖率数值、最佳匹配 event id/窗口，都要落进 provider_calls
    观测记录，供事后查证具体清空原因，而不只是一个 bool。
    """
    hook_text = (script.ending_hook or "").strip()
    if not hook_text:
        return
    report = ending_hook_grounding_report(
        script.ending_hook, script.full_script_text, events=script.events,
    )
    if report["grounded"]:
        return
    script.ending_hook = ""
    log_provider_call(
        "ending_hook_grounding_rejected",
        config.MODEL_TEXT,
        "REJECTED",
        None,
        0,
        meta={
            "episode_id": episode_id,
            "source": source,
            "hook_text": report["hook_text"],
            "tier": report["tier"],
            "layer1_coverage": report["layer1_coverage"],
            "best_event_id": report["best_event_id"],
            "best_event_coverage": report["best_event_coverage"],
            "window": report["window"],
        },
    )


async def _generate_screenplay_scene_sharded_baseline(
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    *,
    narrative_blueprint: NarrativeBlueprint,
) -> EpisodeScreenplay:
    """Generate resumable Envelope + Blueprint-owned scene shards, then compile."""
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace
    from app.screenplay_scene_shards import (
        SCREENPLAY_SHARD_PLAN_VERSION,
        blueprint_content_hash,
        blueprint_referenced_content_owners,
        build_frozen_identity_registry,
        build_screenplay_scene_input_contract_set,
        build_screenplay_scene_shard_plans,
        generate_screenplay_envelope,
        generate_screenplay_scene_shards,
        merge_screenplay_scene_shards,
        persist_identity_registry,
        persist_merged_ir,
        shard_progress,
    )

    # Rebuild the complete ownership contract at the orchestration boundary.
    # Resumed artifacts must not retain a pre-v3 scene projection.
    derive_blueprint_scene_plans(narrative_blueprint)

    # The old fixed third identity scan is replaced by a typed audit after the
    # Blueprint exists.  Only participant references not already covered by the
    # frozen authority projection are sent, together with their owned SRC.
    from app.identity_authority import identity_authority_registry
    from app.portraits import (
        ensure_structural_identity_coverage,
        screenplay_character_resolutions_for_source,
    )
    from app.orchestration.state_machine import StateConflict

    episode["character_resolutions"] = (
        screenplay_character_resolutions_for_source(
            list(episode.get("character_resolutions") or []),
            episode_no=int(episode.get("episode_no") or 0),
            source_text=source_text,
        )
    )
    authorities = identity_authority_registry(
        bible,
        list(episode.get("character_resolutions") or []),
    )
    known_identity_labels = {
        str(value).strip()
        for authority in authorities
        for value in (
            authority.get("canonical_name"),
            *(authority.get("source_labels") or []),
            authority.get("authority_id"),
        )
        if str(value or "").strip()
    }
    bible_identity_names = {
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
    }
    structural_identity_evidence: list[dict[str, Any]] = []
    for node in narrative_blueprint.nodes:
        if node.narrative_layer == "paratext":
            continue
        evidence_by_key = {
            item.identity_key: item
            for item in node.participant_evidence
        }
        for participant in node.participants:
            evidence = evidence_by_key.get(participant)
            usage = evidence.usage if evidence else "visible"
            if participant in known_identity_labels:
                matching_authorities = [
                    authority
                    for authority in authorities
                    if participant in {
                        str(authority.get("canonical_name") or "").strip(),
                        str(authority.get("authority_id") or "").strip(),
                        *(
                            str(value or "").strip()
                            for value in authority.get("source_labels") or []
                        ),
                    }
                ]
                nonmaterializable_named = any(
                    str(authority.get("identity_kind") or "").strip()
                    != "functional"
                    and not (
                        str(authority.get("canonical_name") or "").strip()
                        in bible_identity_names
                        and str(authority.get("authority_id") or "").strip()
                        == "bible:"
                        + str(authority.get("canonical_name") or "").strip()
                    )
                    for authority in matching_authorities
                )
                if (
                    usage in {"visible", "voice", "state_subject"}
                    and nonmaterializable_named
                ):
                    raise ContentGenerationError(
                        "Blueprint 可见人物只有不可物化的引用身份："
                        f"{participant} ({usage})"
                    )
                continue
            structural_identity_evidence.append({
                "identity_key": participant,
                "source_label": participant,
                "source_segment_ids": (
                    list(evidence.source_segment_ids)
                    if evidence else list(node.source_segment_ids)
                ),
                "usage": usage,
                "node_key": node.key,
            })
    if structural_identity_evidence and episode.get("project_id") and episode.get("id"):
        coverage_trace = current_trace()
        from app.production.revision import get_active_production_revision

        coverage_revision = get_active_production_revision(
            str(episode["id"]), "screenplay"
        )

        def assert_coverage_owner() -> None:
            owner_row = get_conn().execute(
                "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
                (str(episode["id"]),),
            ).fetchone()
            actual_owner = str(
                owner_row["active_screenplay_run_id"] or ""
            ) if owner_row else "missing"
            if actual_owner != coverage_trace.run_id:
                raise StateConflict(
                    "screenplay_resolution_owner",
                    str(episode["id"]),
                    {coverage_trace.run_id},
                    actual_owner,
                )

        coverage = await ensure_structural_identity_coverage(
            str(episode["project_id"]),
            str(episode["id"]),
            int(episode["episode_no"]),
            source_text,
            bible,
            structural_identity_evidence,
            write_guard=assert_coverage_owner,
            expected_active_run_id=coverage_trace.run_id,
            expected_revision_id=(
                coverage_revision.id if coverage_revision is not None else None
            ),
        )
        if coverage.get("errors"):
            raise ValueError(
                "蓝图人物权威收口失败："
                + "；".join(str(value) for value in coverage["errors"][:10])
            )
        if "resolutions" in coverage:
            episode["character_resolutions"] = list(
                coverage.get("resolutions") or []
            )
        if coverage.get("added"):
            project_row = get_conn().execute(
                "SELECT bible_json FROM projects WHERE id=?",
                (str(episode["project_id"]),),
            ).fetchone()
            if project_row and project_row["bible_json"]:
                bible = Bible.model_validate(json.loads(project_row["bible_json"]))

    episode_id = str(
        episode.get("id") or f"episode-{episode['episode_no']}"
    )
    blueprint_hash = blueprint_content_hash(narrative_blueprint)
    trace = current_trace()
    blueprint_row = get_conn().execute(
        """SELECT id,content_json,content_hash,model_snapshot_json,contract_version,
                  prompt_version
             FROM artifacts
             WHERE scope_type='episode' AND scope_id=?
               AND type='screenplay_narrative_blueprint'
               AND status='validated'
             ORDER BY created_at DESC LIMIT 20""",
        (episode_id,),
    ).fetchall()
    blueprint_artifact_id, legacy_same_hash_artifact_id = (
        _select_current_blueprint_artifact(
            list(blueprint_row),
            narrative_blueprint,
            source_text,
        )
    )
    if blueprint_artifact_id is None:
        artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content=narrative_blueprint.model_dump(mode="json"),
                parent_artifact_ids=(
                    [legacy_same_hash_artifact_id]
                    if legacy_same_hash_artifact_id else []
                ),
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot=_current_blueprint_authority_snapshot(
                    source_text,
                    generation_mode="current_authority_wrapper",
                ),
            ),
            step_run_id=trace.step_run_id,
        )
        blueprint_artifact_id = str(artifact["id"])
    _commit_blueprint_authority_checkpoint(
        episode_id=episode_id,
        blueprint_artifact_id=blueprint_artifact_id,
        blueprint_hash=blueprint_hash,
        source_text=source_text,
    )

    async def freeze_identity() -> tuple[list[Any], list[dict[str, Any]], str, str]:
        current_resolutions = screenplay_character_resolutions_for_source(
            list(episode.get("character_resolutions") or []),
            episode_no=int(episode.get("episode_no") or 0),
            source_text=source_text,
        )
        episode["character_resolutions"] = current_resolutions
        identities_value, registry_value, registry_hash_value = (
            build_frozen_identity_registry(
                bible,
                current_resolutions,
                referenced_content_owners=(
                    blueprint_referenced_content_owners(narrative_blueprint)
                ),
            )
        )
        from app.production.revision import get_active_production_revision

        active_revision = get_active_production_revision(
            episode_id,
            "screenplay",
        )
        reused_identity = (
            (active_revision.checkpoint_json or {})
            .get("reused_inputs", {})
            .get("identity_registry", {})
            if active_revision is not None
            else {}
        )
        reused_identity_id = str(
            reused_identity.get("artifact_id") or ""
        )
        reused_identity_hash = str(
            reused_identity.get("identity_registry_hash") or ""
        )
        identity_parents = [blueprint_artifact_id]
        if (
            reused_identity_id
            and reused_identity_hash == registry_hash_value
        ):
            identity_parents.append(reused_identity_id)
        artifact_id_value = persist_identity_registry(
            episode_id=episode_id,
            identity_registry=registry_value,
            identity_registry_hash=registry_hash_value,
            parent_artifact_ids=identity_parents,
        )
        return (
            identities_value,
            registry_value,
            registry_hash_value,
            artifact_id_value,
        )

    (
        identities,
        identity_registry,
        identity_registry_hash,
        identity_artifact_id,
    ) = await _run_screenplay_workflow_step(
        "screenplay_identity_freeze",
        freeze_identity,
        agent_name="screenplay_identity_freeze",
        context_manifest={"episode_id": episode_id},
    )
    plans = build_screenplay_scene_shard_plans(
        narrative_blueprint,
        source_text=source_text,
        identity_registry_hash=identity_registry_hash,
        identity_registry=identity_registry,
    )
    scene_input_contracts = build_screenplay_scene_input_contract_set(
        plans=plans,
        blueprint=narrative_blueprint,
        source_text=source_text,
        identity_registry=identity_registry,
    )
    plan_payload = {
        "contract_version": SCREENPLAY_SHARD_PLAN_VERSION,
        "blueprint_hash": blueprint_hash,
        "identity_registry_hash": identity_registry_hash,
        "plans": [plan.model_dump(mode="json") for plan in plans],
    }
    shard_plan_hash = evidence_repository.content_hash(plan_payload)
    plan_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_scene_shard_plan",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content={**plan_payload, "shard_plan_hash": shard_plan_hash},
            parent_artifact_ids=[blueprint_artifact_id, identity_artifact_id],
            contract_version=SCREENPLAY_SHARD_PLAN_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    _save_screenplay_generation_checkpoint(
        episode_id,
        "ENVELOPE_GENERATION",
        identity_artifact_id=identity_artifact_id,
        identity_registry_hash=identity_registry_hash,
        shard_plan_artifact_id=plan_artifact["id"],
        shard_plan_hash=shard_plan_hash,
        shards=[
            {
                "shard_id": plan.shard_id,
                "status": "pending",
                "attempt": 0,
                "source_hash": plan.source_hash,
                "boundary_hash": plan.boundary_hash,
            }
            for plan in plans
        ],
        yield_reason=None,
    )

    def update_shard_progress(rows: list[dict[str, Any]]) -> None:
        _save_screenplay_generation_checkpoint(
            episode_id,
            "SCENE_SHARD_GENERATION",
            shards=rows,
            shard_progress=shard_progress(rows),
            yield_reason=None,
        )

    envelope_result, shard_result = await asyncio.gather(
        _run_screenplay_workflow_step(
            "screenplay_envelope",
            lambda: generate_screenplay_envelope(
                episode=episode,
                blueprint=narrative_blueprint,
                identity_registry=identity_registry,
                identity_registry_hash=identity_registry_hash,
                blueprint_artifact_id=blueprint_artifact_id,
                identity_artifact_id=identity_artifact_id,
                source_text=source_text,
            ),
            agent_name="screenplay_envelope",
            context_manifest={"episode_id": episode_id},
        ),
        _run_screenplay_workflow_step(
            "screenplay_scene_shards",
            lambda: generate_screenplay_scene_shards(
                episode=episode,
                source_text=source_text,
                blueprint=narrative_blueprint,
                identity_registry=identity_registry,
                identities=identities,
                plans=plans,
                scene_input_contracts=scene_input_contracts,
                blueprint_artifact_id=blueprint_artifact_id,
                identity_artifact_id=identity_artifact_id,
                progress=update_shard_progress,
            ),
            agent_name="screenplay_scene_shards",
            context_manifest={
                "episode_id": episode_id,
                "shard_count": len(plans),
            },
        ),
    )
    envelope, envelope_artifact_id = envelope_result
    shards, shard_artifact_ids, checkpoint_rows = shard_result
    _save_screenplay_generation_checkpoint(
        episode_id,
        "IR_MERGE",
        envelope_artifact_id=envelope_artifact_id,
        shards=checkpoint_rows,
        shard_progress=shard_progress(checkpoint_rows),
        yield_reason=None,
    )
    async def merge_ir() -> tuple[Any, str]:
        merged_value = merge_screenplay_scene_shards(
            envelope=envelope,
            identities=identities,
            plans=plans,
            shards=shards,
            scene_input_contracts=scene_input_contracts,
            blueprint=narrative_blueprint,
            source_text=source_text,
        )
        artifact_id_value = persist_merged_ir(
            episode_id=episode_id,
            ir=merged_value,
            parent_artifact_ids=[
                blueprint_artifact_id,
                identity_artifact_id,
                envelope_artifact_id,
                *shard_artifact_ids,
            ],
            blueprint_hash=blueprint_hash,
            identity_registry_hash=identity_registry_hash,
        )
        return merged_value, artifact_id_value

    merged_ir, merged_artifact_id = await _run_screenplay_workflow_step(
        "screenplay_merge",
        merge_ir,
        agent_name="screenplay_merge",
        context_manifest={
            "episode_id": episode_id,
            "shard_count": len(shards),
        },
    )
    _save_screenplay_generation_checkpoint(
        episode_id,
        "IR_MERGE",
        merged_ir_artifact_id=merged_artifact_id,
        yield_reason=None,
    )
    completed_ir = await _complete_screenplay_ir_fidelity(
        merged_ir,
        episode=episode,
        source_text=source_text,
        bible=bible,
        parent_artifact_id=merged_artifact_id,
        narrative_blueprint=narrative_blueprint,
    )
    blueprint_errors = validate_and_apply_blueprint_scene_contract(
        completed_ir,
        narrative_blueprint,
    )
    if blueprint_errors:
        raise ValueError("；".join(blueprint_errors))
    compiler_audit: list[dict[str, Any]] = []
    script = compile_screenplay_ir(
        completed_ir,
        episode=episode,
        source_text=source_text,
        bible=bible,
        audit=compiler_audit,
    )
    object.__setattr__(
        script,
        "_source_ir_artifact_id",
        getattr(completed_ir, "evidence_artifact_id", merged_artifact_id),
    )
    # 溯源校验：ending_hook 必须能在本集正文中找到对应内容支持，否则视为编造并清空。
    # 不再依据 episode.cliffhanger 是否预设来决定是否允许写 ending_hook——
    # 判据从"字段是否预设"换成"内容是否真的来自本集正文"。此处 script.events
    # 已由 compile_screenplay_ir 编译完成，传入后额外要求结构化事件溯源。清空
    # 动作本身必须可观测（见 _clear_ungrounded_ending_hook docstring）。
    _clear_ungrounded_ending_hook(
        script,
        episode_id=str(episode.get("id") or ""),
        source="screenplay_scene_sharded_baseline",
    )
    return script


async def generate_screenplay(episode: dict, source_text: str, bible: Bible,
                              prev_ending: str = "") -> EpisodeScreenplay:
    """小说 -> 完整剧本。

    新格式不在剧本台阶段强制拆成拍卡，而是先生成一份可读、可审、可拆镜的生产级剧本稿；
    拆镜与执行字段延后到分镜阶段。先显式锁定"本集必保留关键台词/关键剧情点"，
    再写正文，从机制上阻止重要台词与剧情在压缩中被丢弃。
    """
    from app.portraits import screenplay_character_resolutions_for_source

    episode["character_resolutions"] = (
        screenplay_character_resolutions_for_source(
            list(episode.get("character_resolutions") or []),
            episode_no=int(episode.get("episode_no") or 0),
            source_text=source_text,
        )
    )
    scene_shards_enabled = str(
        get_setting("screenplay_scene_shards_enabled") or "true"
    ).strip().lower() not in {"0", "false", "off", "no"}
    semantic_feedback: dict[str, list[str]] = {}
    for rebuild_no in range(SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT + 1):
        narrative_blueprint = await _run_screenplay_workflow_step(
            "screenplay_blueprint",
            lambda: _generate_screenplay_narrative_blueprint(
                episode,
                source_text,
                bible,
                semantic_feedback=dict(semantic_feedback),
            ),
            agent_name="screenplay_blueprint",
            context_manifest={
                "episode_id": str(episode.get("id") or ""),
                "source_chars": len(source_text),
                "semantic_rebuild_no": rebuild_no,
            },
        )
        if not scene_shards_enabled:
            break
        from app.screenplay_scene_shards import ScreenplaySceneShardError

        try:
            return await _generate_screenplay_scene_sharded_baseline(
                episode,
                source_text,
                bible,
                narrative_blueprint=narrative_blueprint,
            )
        except ScreenplaySceneShardError as exc:
            unresolved = dict(
                getattr(exc, "unresolved_semantic_units", {}) or {}
            )
            # 只有「语义门禁耗尽修复轮次仍未收口」才带 unresolved：其余分片失败
            # （schema、归属、预算）不属于蓝图分类问题，照常向上抛。
            if not unresolved or rebuild_no >= (
                SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT
            ):
                raise
            semantic_feedback = unresolved
            _append_blueprint_semantic_rebuild_event(
                episode_id=str(episode.get("id") or ""),
                shard_id=str(getattr(exc, "shard_id", "") or ""),
                unresolved=unresolved,
                rebuild_no=rebuild_no + 1,
            )
    blueprint_json = json.dumps(
        narrative_blueprint.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    screenplay_bible_json = json.dumps(
        screenplay_ir_bible_context(
            bible,
            source_text=source_text,
            episode_no=int(episode["episode_no"]),
            character_resolutions=list(episode.get("character_resolutions") or []),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    character_resolution_block = _character_resolution_prompt_block(episode)
    source_with_ids = render_indexed_source(source_text)
    fidelity_budget = screenplay_ir_fidelity_budget(source_text)
    fidelity_budget_json = json.dumps(
        fidelity_budget,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    episode_hook = (episode.get("hook") or "").strip()
    episode_cliffhanger = (episode.get("cliffhanger") or "").strip()
    no_episode_hook = not episode_hook and not episode_cliffhanger
    source_dialogues = source_dialogue_fragments(source_text)
    opening_dialogue_block = (
        "【首条改编对白来源锚点·硬门禁】\n"
        "- D001 是本集实际采用的第一条对白，不是整章最早出现的引号片段。\n"
        "第一条 dialogue unit 的 source_text 必须逐字引用本集原文中语义支持该改编台词的"
        "真实话语，text 只能做口语压缩，二者不得表达不同内容。"
        "不得把拟声、环境声或已被改编方案舍弃场景中的无关话语强绑为 D001。"
        "即使说话人不需跨集定妆，只要该话语被本集采用并负责触发后续反应，也必须按完整因果链保留。"
        if source_dialogues
        else "【首条改编对白来源锚点】本集原文未检测到显式对白，禁止凭空发明对白。"
    )
    screenplay_hook_rule = (
        f"剧本开头必须承接上一集真实结尾：{episode_hook}，用本集原文真实内容尽快呼应/推进，"
        f"不得无视，也不得凭空续写 {episode_hook} 之外的新剧情。"
        if episode_hook
        else "剧本开头按原文真实开场推进。"
    )
    screenplay_ending_rule = (
        "本集结尾按原文真实收束；若本集原文结尾处存在真实悬念/转折/未完成动作，"
        "据实呈现为 ending_hook；若原文本集确已完结、无遗留悬念，ending_hook 留空——"
        "不得为了留钩在原文没有悬念处编造下一集事件。"
    )
    authorized_source_chapters = episode.get("authorized_source_chapters")
    source_chapter_ids = (
        [str(value) for value in authorized_source_chapters]
        if isinstance(authorized_source_chapters, dict)
        else [
            str(value)
            for value in (episode.get("source_chapters") or [])
            if str(value).strip()
        ]
    )
    source_chapter_contract = json.dumps(
        [{"chapter_id": value} for value in source_chapter_ids],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》生成完整生产剧本的紧凑语义 IR。

后端会把 IR 确定性编译成现有 EpisodeScreenplay、narrative_plan、来源台账和全部分镜引用。
你只负责不可机械推导的创作语义；禁止输出最终 S/E/F/A/SC/P/SE/RW/AP/AS/XI/XD 等编号，
也不要重复输出 key_lines、full_script_text、scene_outline 或任何反向引用。
后端将 scenes.units 确定性生成 `"dialogue_chains"`；`key_lines` 由后端按 dialogue_chains.turns 确定性回填。
每组 dialogue_chain 最多 {DIALOGUE_CHAIN_TURNS_HARD_MAX} 个连续话轮，超过时按自然语义转折拆分 chain_key。
后端编译后的最终顶层必须输出 narrative_plan；IR 只写其不可机械推导的关系语义。
最终来源证据允许的章节句柄：{source_chapter_contract}

核心目标不是压缩剧情。当前 {episode['target_duration_s']} 秒只是最低节奏参考，
最终时长由完整剧情、对白容量、主线节拍和场次建立成本自动扩展，不设上限。
不得删除有效事件、对白、人物反应、因果桥梁和场景上下文；只有能指出重复来源的内容才允许 duplicate。

{renderability_prompt_block()}

【IR 编写规则】
1. scenes.units 是唯一的模型创作时间线，按原文与因果顺序覆盖全部有效剧情单元。
   后端从 units 确定性生成 events、plot_spine/beats；禁止输出 events 或 beats。
2. 下方每个 SRC* 是不可跳过的细粒度原文段。每个 action/dialogue unit 必须填写
   source_segment_ids，且只能填写该 unit 真正改编进正文的 SRC；同一 unit 可合并连续 SRC，
   同一 SRC 也可由多个 unit 展开。所有 SRC 必须至少被一个正文 unit 消费，首次消费顺序
   必须与原文一致。不得把未改编内容仅登记为 context/coverage 来制造“已覆盖”；
   coverage 留空，由后端从 units 确定性生成 deliver/merge。单个 unit 最多合并
   {IR_MAX_SOURCE_SEGMENTS_PER_UNIT} 个连续 SRC，禁止一次挂载大量 ID 掩盖删戏。
3. scenes 按真实时空与连续动作分场。每场只写 heading、戏剧功能、摘要、冲突、转折及
   units；人物、来源依据、入场/出场状态由后端从 events 投影。units 是最终台本的严格播放顺序：
   action 表示动作段，dialogue 表示角色实际开口；禁止把场次摘要冒充 units。
   每个 scene_heading 只能写一个主要地点，禁止用「、」「+」或逗号把家、学校、
   车辆、车站、宾馆等不连续空间合成一场；时间、地点、人物目标或连续动作切换必须新建场次。
4. 每个 unit 必须提供一个按播放顺序递增且在本集唯一的 event_key，作为程序生成 event
   的稳定句柄，并按原文顺序填写 source_segment_ids。resulting_state 必须写该 unit
   完成后新成立的人物、信息、道具、关系或局势状态，禁止复述 text；does→turn 必须形成
   可核对的状态变化。每个 dialogue 必须引用 identity.key，
   chain_key 保持问答链，source_text 必须逐字连续存在于本集原文且语义支持改编台词。
   dialogue 的 source_text 必须位于该 unit 声明的 source_segment_ids 内。
   原文没有显式对白时禁止发明角色对白。
5. 不要输出 events。event 的来源段、参与身份、前后状态、动作意图、完成条件、
   action_phases、observable_claim、因果边、证据、信息台账、显著度与阅读时间，
   均由后端根据 units 顺序、resulting_state、speaker_key、原文锚点和动作文本确定性生成。
6. 复杂动作的自然阶段直接写成同一 event_key 下有序的多个 action unit；
   后端据此建立 action_phases 和可跨镜边界。
7. identities 覆盖所有可见身份和说话人。{model_identity_authority_prompt_rule()}
   人物谱角色 display_name 必须逐字使用人物谱姓名；
   其他身份只要原文有明确称谓，就必须把原文逐字称谓写入 source_names，display_name 使用
   source_names[0]，key 作为全篇稳定实体 ID；不得把原文可区分实体改写成路人编号、
   英文占位符或其他临时名字。原文确实未命名的群众才用地点+职责构成稳定
   key/display_name。视觉/资产/声音策略由本集来源和戏剧职责决定，
   禁止按姓名或题材白名单猜测。
8. 不要输出 audience_priors。后端按项目上下文确定性建立两类一次观看先验；
   experience 只写本集希望观众完成的整体理解变化、盲审标准和处理时间。
9. metadata 必须包含完整戏剧问题、目标、阻力、代价、情绪曲线、结局和四段结构。
10. {screenplay_hook_rule} {screenplay_ending_rule}
11. 完整剧情优先于压缩：所有 units 的改编净文本总量不得低于原文净文本的
    {IR_MIN_ADAPTED_SOURCE_RATIO:.0%}；每连续 {IR_LOCAL_SOURCE_WINDOW} 个 SRC 的局部改编量
    不得低于该窗口原文净文本的 {IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO:.0%}。
    这是防止删戏的最低线，不是扩写目标。必须保留有效动作、人物反应、对白关系、
    因果桥梁、入场出场与场景转换，不能用重复描述凑字数。

本集程序计算的保真预算（字符数均为去空白后的净文本，必须逐窗口满足）：
{fidelity_budget_json}
front_matter_ids 是程序确认的章节标题，不需要改编进正文；其余 SRC 不得遗漏。

本集规划：
- 概要：{episode.get('synopsis') or '（无）'}
- hook：{episode_hook or '（空）'}
- cliffhanger：{episode_cliffhanger or '（空）'}
- 上一集结尾：{prev_ending or '（本集为第一集）'}

【程序已验证的叙事蓝图·场次权威】
{blueprint_json}
必须严格按 scene_plans 的 key、顺序、单一时间域和单一地点输出 scenes。
每场 units 只能消费该 scene_plan.source_segment_ids，不得跨场挪用来源。
节点的 transition_cue、state_requirements/state_changes、decision 动机、agency_mode
与 narrative_attribution 都是正文必须可见的因果合同；禁止只把它们写在摘要里。
生理反应、停止反抗或事后互动不能反向改写事件发生时的 agency_mode；同一节点中
external_coercion 不得同时归因为角色自愿选择或道德转变。自主性真正改变时必须使用
蓝图中已拆开的后续节点和约束解除事实。

{opening_dialogue_block}

本集相关人物与场景圣经（已按当前来源证据动态投影）：
{screenplay_bible_json}

{character_resolution_block}

带稳定段 ID 的本集原文：
授权章节 ID：{source_chapter_ids or ['（未提供）']}
{_render_screenplay_source(source_with_ids)}

只输出以下紧凑 JSON 合同，不要解释、不要 Markdown：
{screenplay_ir_prompt_contract()}"""
    # Production Repair：完整生成只允许一次；QA 后禁止“重新输出完整 JSON”。
    return await generate_screenplay_baseline(
        episode, source_text, bible, prev_ending=prev_ending, _prompt=prompt,
        _no_episode_hook=no_episode_hook,
        _narrative_blueprint=narrative_blueprint,
    )


async def generate_screenplay_baseline(
    episode: dict,
    source_text: str,
    bible: Bible,
    prev_ending: str = "",
    *,
    _prompt: str | None = None,
    _no_episode_hook: bool | None = None,
    _narrative_blueprint: NarrativeBlueprint | None = None,
) -> EpisodeScreenplay:
    """仅一次有效 Baseline 生成。无论 QA 是否通过都返回可解析候选，交由局部 Patch。"""
    if _prompt is None:
        # 复用 generate_screenplay 的 prompt 构建：直接再调一次会递归，故要求调用方传入
        # 或走 generate_screenplay 包装。此处保留独立入口供 Production Repair 使用。
        return await generate_screenplay(episode, source_text, bible, prev_ending=prev_ending)

    # Compact IR only permits one full model response. Shape drift is repaired
    # before Pydantic validation; semantic fields are expanded by the local
    # compiler. Never resend the complete source and candidate as a repair.
    structural_bootstrap_iterations = SCREENPLAY_STRUCTURAL_BOOTSTRAP_ITERATIONS
    loop = AgentLoop(
        stage_key="screenplay",
        contract_key="screenplay",
        goal=f"生成第 {episode['episode_no']} 集剧本 Baseline（仅一次完整生成）",
        scope_type="episode",
        scope_id=str(episode.get("id") or f"episode-{episode['episode_no']}"),
        artifact_type="screenplay_generation_ir",
        prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
        policy=AgentLoopPolicy(
            max_iterations=structural_bootstrap_iterations,
            stall_rounds=min(2, structural_bootstrap_iterations),
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=False,
            baseline_only=True,
            repair_all_blockers=True,
        ),
    )

    def _check_screenplay(s: EpisodeScreenplay) -> list[str | Issue]:
        # 身份消歧是 Baseline 构建规范化，必须在首次 QA 之前落实，
        # 避免为“绿袍男子→路人甲”浪费一轮模型修复。
        from app.portraits import (
            apply_screenplay_character_resolutions,
            normalize_screenplay_identity_annotations,
            screenplay_character_resolution_errors,
        )
        resolutions = list(episode.get("character_resolutions") or [])
        functional_identity_names = {
            str(item.get("canonical_name") or "").strip()
            for item in resolutions
            if (
                isinstance(item, dict)
                and identity_resolution_is_authoritative(item)
                and resolution_declares_functional_identity(item)
                and str(item.get("canonical_name") or "").strip()
            )
        }
        apply_screenplay_character_resolutions(
            s,
            resolutions,
        )
        normalize_screenplay_identity_annotations(s, bible)
        errors = validate_screenplay(
            s, bible, max(1, episode["target_duration_s"] // config.VIDEO_DURATION_MIN_S),
            episode_no=episode["episode_no"], source_text=source_text,
            require_dialogue_chains=True,
            # Narrative source spans are chapter-local. The generic validator
            # only receives the concatenated episode source and therefore
            # cannot validate those offsets without producing false
            # SOURCE_SPAN_EXACT_MISMATCH findings.
            validate_narrative=False,
            require_source_coverage=True,
            functional_identity_names=functional_identity_names,
            episode=episode,
        )
        if s.narrative_plan is None:
            errors.append(Issue(
                code="NARRATIVE_PLAN_REQUIRED",
                severity=IssueSeverity.BLOCKER,
                category="structural",
                subject="screenplay",
                message=(
                    "新生成剧本必须包含 narrative_plan；legacy 兼容仅允许读取"
                    "历史已发布产物，不得用于新生产"
                ),
                evidence={"path": "/narrative_plan", "rule_id": "required"},
                repairable=True,
            ))
        else:
            from app.narrative import validate_screenplay_narrative

            authorized_source_chapters = (
                episode.get("authorized_source_chapters")
                if isinstance(
                    episode.get("authorized_source_chapters"), dict,
                )
                else None
            )
            raw_source_chapters = episode.get("source_chapters") or []
            if isinstance(raw_source_chapters, str):
                try:
                    raw_source_chapters = json.loads(raw_source_chapters)
                except (TypeError, ValueError, json.JSONDecodeError):
                    raw_source_chapters = []
            errors.extend(validate_screenplay_narrative(
                s,
                require=True,
                source_text=source_text,
                expected_scope_id=str(
                    episode.get("id")
                    or f"episode-{episode['episode_no']}"
                ),
                authorized_source_chapter_ids=(
                    [
                        str(value)
                        for value in raw_source_chapters
                        if str(value).strip()
                    ]
                    if authorized_source_chapters is None
                    else None
                ),
                authorized_source_chapters=authorized_source_chapters,
            ))
        errors.extend(screenplay_character_resolution_errors(
            s,
            resolutions,
        ))
        errors.extend(adaptation_hook_errors(s, episode))
        # 判据是内容是否有本集正文支持，不是 episode.hook/cliffhanger 字段是否预设为空。
        ending = (s.ending_hook or "").strip()
        if ending and not ending_hook_is_grounded(ending, s.full_script_text, events=s.events):
            errors.append(
                "ending_hook 与本集正文几乎不重合，判定为编造下一集钩子：ending_hook 必须"
                "来自本集原文真实存在的悬念/转折/未完成动作，原文确已完结则留空")
        deduped_errors: list[str | Issue] = []
        seen_errors: set[str] = set()
        for error in errors:
            identity = (
                f"issue:{error.fingerprint}"
                if isinstance(error, Issue)
                else f"message:{error}"
            )
            if identity in seen_errors:
                continue
            seen_errors.add(identity)
            deduped_errors.append(error)
        return deduped_errors

    def _compile_candidate(
        candidate: ScreenplayGenerationIR | EpisodeScreenplay,
    ) -> EpisodeScreenplay:
        if isinstance(candidate, EpisodeScreenplay):
            return candidate
        compiler_audit: list[dict[str, Any]] = []
        return compile_screenplay_ir(
            candidate,
            episode=episode,
            source_text=source_text,
            bible=bible,
            audit=compiler_audit,
        )

    def _check_ir(
        candidate: ScreenplayGenerationIR | EpisodeScreenplay,
    ) -> list[str | Issue]:
        if (
            isinstance(candidate, ScreenplayGenerationIR)
            and _narrative_blueprint is not None
        ):
            blueprint_errors = (
                validate_and_apply_blueprint_scene_contract(
                    candidate,
                    _narrative_blueprint,
                    allow_prefix=True,
                )
            )
            if blueprint_errors:
                return blueprint_errors
        try:
            return _check_screenplay(_compile_candidate(candidate))
        except ScreenplayIRIdentityConflictError:
            # Exact binding cannot decide this semantic identity.  Preserve the
            # otherwise usable baseline and run the bounded AI adjudicator
            # before the first fidelity compile instead of regenerating it.
            return []
        except (TypeError, ValueError) as exc:
            if (
                isinstance(candidate, ScreenplayGenerationIR)
                and isinstance(exc, ScreenplayIRFidelityError)
            ):
                # The baseline is structurally usable. A bounded source-local
                # expansion runs after AgentLoop instead of resending the
                # entire chapter and candidate.
                return []
            return [Issue(
                code="SCREENPLAY_IR_COMPILE_FAILED",
                severity=IssueSeverity.BLOCKER,
                category="structural",
                subject="screenplay",
                message=str(exc),
                evidence={"path": "$", "rule_id": "ir_compile"},
                repairable=True,
            )]

    episode_id = str(
        episode.get("id") or f"episode-{episode['episode_no']}"
    )
    recovered = _recover_screenplay_ir_candidate(
        episode_id,
        blueprint_hash=_narrative_blueprint_content_hash(
            _narrative_blueprint
        ),
        expected_source_audit_annotations=(
            list(_narrative_blueprint.source_audit_annotations)
            if _narrative_blueprint is not None
            else None
        ),
    )
    if recovered is not None:
        recovered_candidate, recovered_artifact_id = recovered
        try:
            if _narrative_blueprint is None:
                recovered_candidate = await _repartition_multilocation_ir_scenes(
                    recovered_candidate,
                    episode=episode,
                    source_text=source_text,
                    parent_artifact_id=recovered_artifact_id,
                )
            else:
                blueprint_errors = (
                    validate_and_apply_blueprint_scene_contract(
                        recovered_candidate,
                        _narrative_blueprint,
                        allow_prefix=True,
                    )
                )
                if blueprint_errors:
                    raise ValueError("；".join(blueprint_errors))
            recovered_candidate = await _complete_screenplay_ir_fidelity(
                recovered_candidate,
                episode=episode,
                source_text=source_text,
                bible=bible,
                parent_artifact_id=getattr(
                    recovered_candidate,
                    "evidence_artifact_id",
                    recovered_artifact_id,
                ),
                narrative_blueprint=_narrative_blueprint,
            )
            if _narrative_blueprint is not None:
                blueprint_errors = (
                    validate_and_apply_blueprint_scene_contract(
                        recovered_candidate,
                        _narrative_blueprint,
                    )
                )
                if blueprint_errors:
                    raise ValueError("；".join(blueprint_errors))
            recovered_script = _compile_candidate(recovered_candidate)
            recovered_errors = _check_screenplay(recovered_script)
        except (TypeError, ValueError):
            recovered_errors = ["recovered_ir_compile_failed"]
        else:
            object.__setattr__(
                recovered_script,
                "_source_ir_artifact_id",
                recovered_artifact_id,
            )
            log_provider_call(
                "screenplay_ir_local_recompile",
                config.MODEL_TEXT,
                "REUSED",
                None,
                0,
                meta={
                    "episode_id": episode_id,
                    "artifact_id": recovered_artifact_id,
                    "generation_contract": IR_VERSION,
                    "compiler_version": IR_COMPILER_VERSION,
                    "prompt_version": SCREENPLAY_BASELINE_PROMPT_VERSION,
                    "qa_handoff_issue_count": len(recovered_errors),
                    "reason": (
                        "production_repair_handles_compiled_candidate_issues"
                    ),
                },
            )
            return recovered_script

    candidate = await _run_with_agent_loop(
        "剧本首次整版 Baseline", "screenplay", _prompt,
        ScreenplayGenerationIR,
        _check_ir,
        loop=loop, temperature=0.7,
        max_tokens=screenplay_ir_token_budget(source_text),
        repair_user_prompt_limit=None,
        repair_candidate_limit=None,
        prefill={
            "format_version": IR_VERSION,
            "episode_no": episode["episode_no"],
        },
    )
    if isinstance(candidate, ScreenplayGenerationIR):
        if _narrative_blueprint is None:
            candidate = await _repartition_multilocation_ir_scenes(
                candidate,
                episode=episode,
                source_text=source_text,
                parent_artifact_id=getattr(
                    candidate, "evidence_artifact_id", None,
                ),
            )
        else:
            blueprint_errors = validate_and_apply_blueprint_scene_contract(
                candidate,
                _narrative_blueprint,
                allow_prefix=True,
            )
            if blueprint_errors:
                raise ValueError("；".join(blueprint_errors))
        candidate = await _complete_screenplay_ir_fidelity(
            candidate,
            episode=episode,
            source_text=source_text,
            bible=bible,
            parent_artifact_id=getattr(candidate, "evidence_artifact_id", None),
            narrative_blueprint=_narrative_blueprint,
        )
        if _narrative_blueprint is not None:
            blueprint_errors = validate_and_apply_blueprint_scene_contract(
                candidate,
                _narrative_blueprint,
            )
            if blueprint_errors:
                raise ValueError("；".join(blueprint_errors))
    script = _compile_candidate(candidate)
    object.__setattr__(
        script,
        "_source_ir_artifact_id",
        getattr(candidate, "evidence_artifact_id", None),
    )
    # 溯源校验：与 Scene-Shard 路径一致，只有 ending_hook 在本集正文找不到对应内容
    # 支持时才判定为编造并清空；不再仅凭 episode.hook/cliffhanger 是否预设决定。
    # 清空动作本身必须可观测（见 _clear_ungrounded_ending_hook docstring）。
    _clear_ungrounded_ending_hook(
        script,
        episode_id=str(episode.get("id") or ""),
        source="screenplay_baseline_legacy",
    )
    return script


def _compact_narrative_plan_context(screenplay: EpisodeScreenplay) -> str:
    """Project the authority graph into a compact, ID-preserving storyboard context.

    The storyboard does not need verbatim source spans or calibration prose on
    every call, but it must see every ownership/state/deadline relation.  This
    projection deliberately keeps semantic IDs and graph edges instead of
    summarizing them into lossy natural language.
    """
    plan = screenplay.narrative_plan
    if plan is None:
        return "【叙事连续性权威图】缺失；新生产不得规划分镜。\n"

    payload = {
        "contract_version": plan.contract_version,
        "scope_id": plan.scope_id,
        "initial_state_fact_ids": list(plan.initial_state_fact_ids),
        "source_evidence": [
            {
                "source_evidence_id": item.source_evidence_id,
                "source_span": item.source_span.model_dump(mode="json"),
                "verbatim_excerpt": item.verbatim_excerpt[:120],
                "confidence": item.confidence,
            }
            for item in plan.source_evidence
        ],
        "propositions": [
            {
                "proposition_id": item.proposition_id,
                "canonical_statement": item.canonical_statement,
                "narrative_domain": item.narrative_domain,
                "entity_ids": list(item.entity_ids),
                "direct_source_evidence_ids": item.direct_source_evidence_ids,
                "domain_truth_status": item.domain_truth_status,
            }
            for item in plan.propositions
        ],
        "adaptation_decisions": [
            item.model_dump(mode="json") for item in plan.adaptation_decisions
        ],
        "state_facts": [item.model_dump(mode="json") for item in plan.state_facts],
        "evidence": [item.model_dump(mode="json") for item in plan.evidence],
        "dramatic_questions": [
            item.model_dump(mode="json") for item in plan.dramatic_questions
        ],
        "events": [item.model_dump(mode="json") for item in plan.events],
        "atomic_actions": [
            item.model_dump(mode="json") for item in plan.atomic_actions
        ],
        "action_relation_audits": [
            item.model_dump(mode="json") for item in plan.action_relation_audits
        ],
        "character_states": [
            item.model_dump(mode="json") for item in plan.character_states
        ],
        "character_beliefs": [
            item.model_dump(mode="json") for item in plan.character_beliefs
        ],
        "audience_priors": [
            item.model_dump(mode="json") for item in plan.audience_priors
        ],
        "audience_states": [
            item.model_dump(mode="json") for item in plan.audience_states
        ],
        "experience_intents": [
            item.model_dump(mode="json") for item in plan.experience_intents
        ],
        "assimilation_tasks": [
            item.model_dump(mode="json") for item in plan.assimilation_tasks
        ],
        "readability_windows": [
            item.model_dump(mode="json") for item in plan.readability_windows
        ],
        "setup_payoff_contracts": [
            item.model_dump(mode="json") for item in plan.setup_payoff_contracts
        ],
        "scene_contracts": [
            item.model_dump(mode="json") for item in plan.scene_contracts
        ],
        "arc_contracts": [
            item.model_dump(mode="json") for item in plan.arc_contracts
        ],
    }
    return (
        "【叙事连续性权威图·分镜只能引用不得改写】\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )


def _shot_narrative_plan_context(
    screenplay: EpisodeScreenplay,
    brief: StoryboardOutlineShot | None,
) -> str:
    """Render the approved per-shot graph projection without replaying the episode graph."""
    plan = screenplay.narrative_plan
    if plan is None:
        return "【本镜叙事权威图投影】缺失；新生产不得生成分镜。\n"
    if brief is None:
        return _compact_narrative_plan_context(screenplay)
    payload = {
        "contract_version": plan.contract_version,
        "scope_id": plan.scope_id,
        "approved_shot_task": brief.model_dump(mode="json"),
    }
    return (
        "【本镜叙事权威图投影·已由完整权威图编译并通过确定性校验】\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )


def _storyboard_narrative_contract_block(*, include_outline_windows: bool) -> str:
    """Return the ID-level narrative task contract shared by both shot stages.

    This is deliberately relation-driven: it never guesses a genre, audience
    category, transition keyword, or a canned "bridge shot" recipe.  The model
    must bind every value to the screenplay's authoritative narrative graph.
    """
    shot_schema = {
        "shot_id": "SH001（本集唯一、稳定）",
        "scene_id": "引用 narrative_plan.scene_contracts[].scene_id",
        "event_ids": ["引用 narrative_plan.events[].event_id"],
        "primary_action_id": "引用 narrative_plan.atomic_actions[].action_id；无主动作的功能镜可为 null",
        "supporting_action_ids": [],
        "action_phase_ids": ["仅引用本镜实际执行的 temporal phase_id；跨镜动作不得重复分配阶段"],
        "visible_entity_ids": ["本镜实际可见且由权威图定义的 actor/target/entity ID"],
        "offscreen_action_actor_ids": ["仅填写本镜绑定动作中明确在画外执行的 actor_id"],
        "offscreen_action_target_ids": ["仅填写本镜绑定动作中明确在画外承受作用的 target_id"],
        "action_participant_deliveries": [{
            "action_id": "引用本镜绑定 action_id",
            "participant_id": "仅引用对应 action 的画外 actor/target ID",
            "evidence_ids": ["引用本镜 shot_contribution.evidence_ids 中的可感知证据"],
            "audible": False,
            "visible_effect": True,
            "visible_reaction": False,
        }],
        "capacity_budget": {
            "action_phase_s": 0.0,
            "spoken_and_text_s": 0.0,
            "attention_switch_s": 0.0,
            "inference_processing_s": 0.0,
            "reaction_registration_s": 0.0,
            "spatial_reorientation_s": 0.0,
            "entry_exit_settle_s": 0.0,
            "other_s": 0.0,
            "other_reason": None,
        },
        "shot_contribution": {
            "shot_contribution_id": "SCONTRIB-SH001（本集唯一）",
            "experience_intent_ids": ["引用 narrative_plan.experience_intents[].experience_intent_id"],
            "target_delta_ids": ["仅在本镜是该目标差的唯一主交付镜时引用 target_delta_id"],
            "assimilation_task_ids": ["引用本镜实际承担的 assimilation_task_id"],
            "evidence_ids": ["引用本镜让角色/观众实际可感知的 evidence_id"],
            "story_delta_fact_ids": ["引用本镜真正产生的 fact_id"],
            "character_state_delta_ids": ["引用本镜真正改变的 character_state_id/character_belief_id"],
            "audience_state_delta_ids": ["引用本镜实际产生的 audience_state_id"],
            "affective_delta": {},
            "spatial_temporal_delta": {},
            "dramatic_pressure_delta": 0.0,
        },
        "audience_state_paths": [{
            "audience_prior_id": "AP-ID（每个 audience_prior 均必须一条）",
            "audience_state_in_id": "引用该先验的入口 audience_state_id",
            "audience_state_out_target_id": "引用该先验的出口 audience_state_id",
        }],
        "planned_state_in_fact_ids": ["本镜开始已成立的 fact_id"],
        "planned_delta_add_fact_ids": ["本镜新建立的 fact_id"],
        "planned_delta_remove_fact_ids": ["本镜作废的 fact_id"],
        "planned_state_out_fact_ids": ["本镜结束仍成立的 fact_id"],
        "completed_before_action_ids": ["在本镜之前已完成、不得重演的 action_id"],
        "completed_before_action_phase_ids": ["在本镜之前已完成、不得重演的 phase_id"],
        "reserved_future_event_ids": ["明确留给未来镜头、本镜不得泄露的 event_id"],
        "readability_window_ids": ["本镜为之提供证据/注意/处理时间的 readability_window_id"],
        "narrative_boundary_from_previous": None,
    }
    later_boundary_schema = {
        "boundary_id": "NB-SH001-SH002（本集唯一）",
        "previous_shot_id": "紧邻上一镜 shot_id",
        "next_shot_id": "本镜 shot_id",
        "narrative_relation": "两镜的真实叙事关系，由上下文推导",
        "required_state_invariants": ["同时存在于上镜出态和本镜入态、切换后必须保持的 fact_id"],
        "allowed_state_deltas": ["上镜出态与本镜入态对称差中、该边界明确允许的 fact_id"],
        "state_delta_transitions": [{
            "transition_id": "本集唯一的结构关系 ID",
            "basis_type": "timeline_change|viewpoint_visibility_change|spatial_reorientation|action_phase_handoff|other",
            "source_fact_id": "变化前 fact_id 或 null",
            "target_fact_id": "变化后 fact_id 或 null",
            "basis_action_phase_id": "仅 action_phase_handoff 时引用 phase_id，否则 null",
            "custom_basis": "仅 other 时说明开放关系，否则 null",
            "reason": "该结构关系为何允许此状态变化",
        }],
        "forbidden_replay_action_ids": ["上文已完成、本镜不得重演的 action_id"],
        "handoff_action_phase_id": "若为同一动作跨镜，引用合法 phase_id；否则 null",
        "spatial_orientation_contract": {},
        "temporal_orientation_contract": {},
        "audience_state_handoffs": [{
            "audience_prior_id": "AP-ID（所有先验各一条）",
            "previous_state_out_id": "必须等于上镜该先验 audience_state_out_target_id",
            "next_state_in_id": "必须等于本镜该先验 audience_state_in_id",
        }],
        "affective_handoff": {},
        "cut_motivation": "为何此刻切换观众注意，必须非空",
    }
    root_schema = ""
    if include_outline_windows:
        root_schema = (
            "\n大纲根对象还必须输出 `readability_windows`：从 narrative_plan.readability_windows "
            "按 ID 复制语义关系，为每个窗口填入实际承担的 shot_ids，并按实际分镜时长计算 "
            "planned_available_s。窗口列出的每个 shot_id 必须在该镜 readability_window_ids 回引，"
            "镜头引用的每个窗口也必须在窗口 shot_ids 反向引用；planned_available_s 不得大于"
            "绑定镜头总时长，且不得小于 scheduled_processing_s。不得无依据标记 satisfied。根字段结构：\n"
            + json.dumps({
                "readability_windows": [{
                    "readability_window_id": "RW-ID",
                    "event_ids": ["E-ID"],
                    "proposition_ids": ["P-ID"],
                    "target_delta_ids": ["XD-ID"],
                    "shot_ids": ["SH001"],
                    "attention_target_ids": ["P-ID"],
                    "evidence_ids": ["EV-ID"],
                    "scheduled_processing_s": 1.0,
                    "planned_available_s": 1.0,
                    "competing_attention_ids": [],
                    "readability_reason": "下游依赖与可读性原因",
                    "status": "planned",
                }],
                "cognitive_bridge_plans": [],
            }, ensure_ascii=False, separators=(",", ":"))
        )
    return (
        "【叙事任务与镜间交接·必须合并进每个 shot 的 JSON 合同】\n"
        "1. 所有 ID 只能引用上方 narrative_plan 中实际存在的对象；不得自创语义、类型白名单或固定补镜模板。\n"
        "2. shot_id 必须本集唯一且在大纲→逐镜→审核中保持不变；event_ids/scene_id 必须绑定权威图。\n"
        "3. event_ids 在全集的首次出现必须保持 narrative_plan.events 的因果拓扑顺序；"
        "deliver+must_keep 事件必须在其 primary_delivery_window_id 绑定的实际镜头中出现。\n"
        "4. primary_action_id 可为 null：建立、反应、证据、吸收/处理镜只要有真实功能就合法；"
        "但 shot_contribution 必填且至少一个贡献维度非空/非零，不允许纯填充镜。\n"
        "5. 每个被事件引用的 action_id 恰好一个 primary owner；有 temporal_phases 时，owner 必须是交付首阶段的镜头。"
        "每镜 action_phase_ids 只列本镜真实执行阶段，同一动作可跨相邻镜按定义顺序各交付一次；起始阶段镜承接 precondition，"
        "结束阶段镜承接全部 effects。无阶段动作只能作为一个不可拆的 primary action，不能作为 supporting action。\n"
        "5a. capacity_budget 必填：action_phase_s 不得低于本镜 action_phase_ids 的 estimated_min_s 总和；"
        "全部开放观看任务预算之和不得超过 duration_s。只有 AtomicAction.splittable_boundaries 声明的边界才可跨镜拆分；"
        "无合法边界时由 AI 上溯重构动作与镜头任务，禁止按文本切词拆镜。completed_before_action_ids 与 "
        "completed_before_action_phase_ids 必须精确继承前序实际完成账本，并阻止重演。\n"
        "5b. 动作 actor 必须出现在 visible_entity_ids/characters/characters_visible/audio_cast 中，"
        "或作为本动作 actor 明确列入 offscreen_action_actor_ids。动作 target 也必须可见/可听，"
        "或明确列入 offscreen_action_target_ids；任一画外参与者都必须在 "
        "action_participant_deliveries 中按 action_id+participant_id 绑定 evidence_ids，"
        "并结构化声明 audible、visible_effect、visible_reaction 至少一项。证据必须进入本镜 "
        "shot_contribution 且对 audience 可感知，不得用固定身份名单或动作词表猜测。\n"
        "6. 状态方程是精确集合等式：planned_state_out_fact_ids = "
        "(planned_state_in_fact_ids - planned_delta_remove_fact_ids) ∪ planned_delta_add_fact_ids。"
        "remove 必须是 in 的子集，add/remove 不得重叠；镜内事件的全部 precondition 必须在 in，"
        "全部 effects 必须在对应 add/remove，shot_contribution.story_delta_fact_ids 必须属于本镜 add∪remove。\n"
        "7. audience_state_paths 必须覆盖 narrative_plan 的每个 audience_prior；同一先验在本镜的入口状态必须"
        "精确等于上镜出口状态。不得用‘平均观众’覆盖多先验差异。\n"
        "8. 每个 target_delta_id 恰好由一镜作为主交付，主交付镜必须位于 deadline_event_id 所在镜或之前，"
        "且必须属于 delta.primary_delivery_window_id 的 shot_ids。每个 assimilation_task_id 也恰好由一镜主承担，"
        "并在其 target delta 截止事件与 downstream_dependency_event_ids 中最早事件的所在镜或之前完成。\n"
        "9. readability_window_ids 只分给真正提供证据、注意聚焦或观众处理时间的镜头；"
        "镜头与窗口的 shot_ids 必须双向互指，不得只写一边。reserved_future_event_ids 不得在当前镜泄露。\n"
        "10. 第一镜 narrative_boundary_from_previous 必须为 null；第二镜起必填边界对象，精确连接真实相邻 shot_id。"
        "默认本镜 in 等于上镜 out；若存在跨边界变化，两者对称差必须全部由 allowed_state_deltas 明确授权，"
        "required_state_invariants 必须同时存在于上镜 out 与本镜 in。边界还必须对每个先验精确交接"
        "previous_state_out_id→next_state_in_id，并解释此刻为何切换注意。\n"
        "每个 shot 除原有字段外必须合并以下结构（字符串是类型/引用说明，不得原样抄成内容）：\n"
        + json.dumps(shot_schema, ensure_ascii=False, separators=(",", ":"))
        + "\n第二镜起 narrative_boundary_from_previous 对象结构示例：\n"
        + json.dumps(later_boundary_schema, ensure_ascii=False, separators=(",", ":"))
        + root_schema
        + "\n"
    )


def _storyboard_key_content_block(
    screenplay: EpisodeScreenplay,
    *,
    brief: StoryboardOutlineShot | None = None,
) -> str:
    """把剧本台主线合同渲染成分镜 prompt 区块（spine + key_lines/points + drop_list）。"""
    key_lines = [ln.strip() for ln in (screenplay.key_lines or []) if ln and ln.strip()]
    key_points = [pt.strip() for pt in (screenplay.key_plot_points or []) if pt and pt.strip()]
    contract = [
        f"- 本集戏剧问题：{screenplay.dramatic_question}" if screenplay.dramatic_question else "",
        f"- 主角目标：{screenplay.protagonist_goal}" if screenplay.protagonist_goal else "",
        f"- 阻力：{screenplay.obstacle}" if screenplay.obstacle else "",
        f"- 失败代价：{screenplay.stakes}" if screenplay.stakes else "",
    ]
    contract_text = "\n".join(c for c in contract if c)
    from app.validators import key_line_catalog
    catalog = key_line_catalog(screenplay)
    if catalog:
        catalog_items = list(catalog.items())
        if brief is not None:
            current_key_line_ids = set(brief.key_line_ids or [])
            catalog_items = [
                (key_line_id, text)
                for key_line_id, text in catalog_items
                if key_line_id in current_key_line_ids
            ]
        lines_text = "\n".join(
            f"- {key_line_id}｜{text}"
            for key_line_id, text in catalog_items
        ) or "（本镜未分配关键台词）"
    else:
        lines_text = "\n".join(f"- {ln}" for ln in key_lines) or "（剧本未单列，请从完整剧本文本中提取主线对白）"
    points_text = (
        f"- {brief.covers or brief.beat}"
        if brief is not None else
        ("\n".join(f"- {pt}" for pt in key_points)
         or "（剧本未单列，请从完整剧本文本中提取主线剧情）")
    )
    blocks: list[str] = (
        [
            "【叙事连续性】本镜任务已由完整权威图编译；"
            "执行下方本镜大纲任务，不得重新分配 ID 或剧情归属。",
            "",
        ]
        if brief is not None else
        [_compact_narrative_plan_context(screenplay), ""]
    )
    spine = screenplay.plot_spine
    if spine:
        beats = list(spine.spine_beats or [])
        if brief is not None:
            current_spine_ids = set(brief.spine_beat_ids or [])
            beats = [beat for beat in beats if beat.beat_id in current_spine_ids]
        beat_lines = "\n".join(
            f"- {b.beat_id}｜{b.who}｜{b.does}→{b.turn}"
            + ("" if b.must_keep else "（可删过渡）")
            for b in beats
        ) or "（无）"
        drops = (
            "（完整大纲编译阶段已执行，本镜不得拍摄大纲任务之外的内容）"
            if brief is not None else
            ("\n".join(f"- {d}" for d in (spine.drop_list or []) if d) or "（无）")
        )
        blocks.extend([
            "【主线骨架 plot_spine】（必须覆盖 must_keep 节拍；drop_list 禁止拍摄）：",
            f"- premise：{spine.episode_premise or screenplay.episode_premise or '（无）'}",
            f"- must_keep_ending：{spine.must_keep_ending or '（无）'}",
            "spine_beats：",
            beat_lines,
            "drop_list（禁止拍）：",
            drops,
            "",
        ])
    if contract_text:
        blocks.extend(["【单集戏剧契约】（指导取舍：服务它们的内容优先保留）：", contract_text, ""])
    blocks.extend([
        "【本集主线对白链】（KL 顺序就是剧情顺序；每条必须写进某镜有效口播并填 key_line_ids。回答/安慰/反驳必须与其触发台词放在同镜或相邻镜，禁止孤立摘句；代码校验存在性与顺序）：",
        lines_text,
        "",
        "【本集主线剧情点】（每条必须在某镜的 action_desc 或有效口播中体现，代码逐条校验）：",
        points_text,
    ])
    return "\n".join(blocks) + "\n"


def _scene_library_block(bible: Bible, screenplay: EpisodeScreenplay | None = None) -> str:
    """注入可用场景图清单：scene_name 选图，scene_time 独立表达时间。"""
    scenes = list(getattr(bible, "scenes", None) or [])
    if screenplay is not None and screenplay.scene_outline:
        relevant = set(resolve_screenplay_scene_names(screenplay, bible))
        scenes = [scene for scene in scenes if scene.name in relevant]
    if not scenes:
        return ""
    rows = "\n".join(
        f"- {sc.name}：{sc.scene_canonical}" for sc in scenes if getattr(sc, "name", ""))
    names = "、".join(sc.name for sc in scenes if getattr(sc, "name", ""))
    return (
        "【本集已就绪场景图】（scene_name 与下列场景图一一对应）：\n"
        f"{rows}\n"
        f"硬性要求：scene_name 必须直接填上列规范名之一（{names}）；"
        "scene_time 单独填早/中/晚/黄昏/具体时刻，不得把时间混入 scene_name。"
        "禁止借用本集剧本之外的相似场景，也禁止自创场景；本集新地点已经由系统在分镜前自动建库并完成场景图。\n"
    )


def _render_completed_shots_context(shots: list[Shot]) -> str:
    if not shots:
        return "（尚无已通过镜头，本次是第 1 镜）"
    rows: list[dict] = []
    for index, shot in enumerate(shots):
        # 已完成镜头只提供“承接/防重复”的状态摘要，避免把完整动作历史再次喂给模型重演。
        state_out = (
            (getattr(shot, "observed_state_out", "") or "").strip()
            or (getattr(shot, "state_out", "") or "").strip()
            or (shot.last_frame_desc or "").strip()
        )
        narration = (shot.narration or "").strip()
        dialogue_text = "｜".join(d.line for d in shot.dialogues if (d.line or "").strip())
        rows.append({
            "shot_no": shot.shot_no,
            "duration_s": shot.duration_s,
            "scene_time": shot.scene_time,
            "scene_name": shot.scene_name,
            "scene_setting": shot.scene_setting,
            "characters_visible": shot.characters_visible or shot.characters,
            "continuity_mode": shot.continuity_mode,
            "承接状态": state_out[:160],
            "delivered_info_ids": list(shot.new_information_ids or []),
            "soundtrack_brief": (narration + ("｜" if narration and dialogue_text else "") + dialogue_text)[:120],
            "transition": shot.transition,
        })
    return json.dumps(rows, ensure_ascii=False, indent=2)


def _relevant_text_windows(text: str, hints: list[str], *, max_chars: int) -> str:
    """从长文中抽取当前镜相关窗口，用于逐镜生成/修复。

    完整原文已在剧本和分镜大纲阶段消化；逐镜阶段只需要能逐字摘录当前任务的
    局部上下文。命中多处时去重后合并，未命中时保留首尾，避免因节选失败丢掉尾钩。
    """
    source = (text or "").strip()
    if len(source) <= max_chars:
        return source
    keywords: list[str] = []
    for hint in hints:
        for atom in re.split(r"[\s，。！？；：、|｜/]+", hint or ""):
            atom = atom.strip()
            if not (2 <= len(atom) <= 24):
                continue
            candidates = [atom]
            # 大纲常是原文改写（如“谷言拿起钥匙” vs “谷言终于拿起钥匙”）；
            # 长句精确命中失败时，用较长连续子串定位，不做昂贵的语义检索。
            if len(atom) >= 6:
                for width in range(min(8, len(atom) - 1), 3, -1):
                    candidates.extend(atom[i:i + width] for i in range(0, len(atom) - width + 1))
            for candidate in candidates:
                if candidate not in keywords:
                    keywords.append(candidate)
    half = 700
    spans: list[tuple[int, int]] = []
    for keyword in keywords[:16]:
        start_at = 0
        while len(spans) < 6:
            pos = source.find(keyword, start_at)
            if pos < 0:
                break
            spans.append((max(0, pos - half), min(len(source), pos + len(keyword) + half)))
            start_at = pos + len(keyword)
        if len(spans) >= 6:
            break
    if not spans:
        side = max_chars // 2
        return source[:side] + "\n……（中间无关段落已省略）……\n" + source[-side:]
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 120:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    chunks: list[str] = []
    used = 0
    for start, end in merged:
        chunk = source[start:end]
        remain = max_chars - used
        if remain <= 0:
            break
        chunks.append(chunk[:remain])
        used += min(len(chunk), remain)
    return "\n……（无关段落已省略）……\n".join(chunks)


def _storyboard_progress_block(completed_shots: list[Shot]) -> str:
    used = sum(shot.duration_s for shot in completed_shots)
    return (
        f"\n【分镜进度】已通过 {len(completed_shots)} 镜、累计 {used}s；"
        "镜头数由完整剧情与上下文交付决定，不设数量上限。\n"
        f"- 本镜 duration_s **默认 {PREFERRED_SHOT_DURATION_S}s**；仅当口播或连续动作确实放不下才取 "
        f"{PREFERRED_SHOT_DURATION_S + 1}~{config.VIDEO_DURATION_MAX_S}s，且须接受后续 AI 时长审核。\n"
        "- 继续按剧本主线推进，覆盖 must_keep spine 与主线台词后即可设置 is_final=true。\n"
    )


def _filter_partial_storyboard_errors(
    errors: list[str],
    *,
    current_index: int,
    current_shot_no: int | None = None,
) -> list[str]:
    """逐镜头 QA 只拦当前镜头与前后承接问题；整集数量/全量声轨/关键内容在最后统一兜底。"""
    filtered: list[str] = []
    for error in errors:
        shot_refs = [int(m.group(1)) for m in re.finditer(r"shots\[(\d+)\]", error)]
        shot_no_refs = [
            int(m.group(1))
            for m in re.finditer(r"shot_no\s*=\s*(\d+)", error)
        ]
        # 当前镜修复不了已落库镜头的字段（典型：上一镜 last_frame_desc 没写出换场视觉），
        # 不把这类错误喂回当前镜，避免模型原地修 8 次。
        if shot_refs and max(shot_refs) < current_index:
            continue
        if (
            current_shot_no is not None
            and shot_no_refs
            and max(shot_no_refs) < current_shot_no
        ):
            continue
        filtered.append(error)
    return filtered


def _normalized_candidate_board(episode_no: int, completed_shots: list[Shot], shot: Shot,
                                bible: Bible | None = None,
                                target_duration_s: int | None = None,
                                *,
                                narrative_authority: bool = False,
                                narrative_plan: Any | None = None,
                                screenplay: EpisodeScreenplay | None = None,
                                ) -> tuple[Storyboard, list[str]]:
    raw_board = Storyboard(episode_no=episode_no, shots=[*completed_shots, shot])
    # 在副本上做合同规范化。未知人物不能靠代码静默删掉；它必须作为
    # 当前候选的模型修复反馈返回，原始台词和角色字段保持完整。
    board = Storyboard.model_validate(raw_board.model_dump(mode="json"))
    if not narrative_authority:
        normalize_continuity(board)
    # The legacy normalizer classifies identities with a fixed functional-role
    # vocabulary and may destructively strip an authority-graph entity.  New
    # ShotTasks are validated against narrative character/action references in
    # validate_storyboard; do not mutate them here.
    character_changes = (
        [] if narrative_authority else normalize_offbible_characters(board, bible)
    )
    stripped = sorted({
        str(change.get("stripped") or "").strip()
        for change in character_changes
        if str(change.get("stripped") or "").strip()
    })
    if stripped:
        return raw_board, [
            "分镜候选使用了未在剧本阶段解析的人物身份："
            + "、".join(stripped)
            + "；不得删除人物或台词，请严格改用已发布剧本中的人物谱正名/路人编号"
        ]
    if not narrative_authority:
        normalize_dialogue_focus_offscreen_mentions(board, bible)
    elif bible is not None and screenplay is not None:
        from app.identity_contracts import (
            IdentityContractError,
            canonicalize_storyboard_operational_identities,
        )

        try:
            canonicalize_storyboard_operational_identities(
                board,
                bible,
                screenplay,
            )
        except IdentityContractError as exc:
            return raw_board, [f"分镜身份权威合同无法解析：{exc}"]
    # ② 产品禁止旁白/内心OS：确定性清空 narration 与 timeline narration 轨。
    relieve_spoken_overflow(board)
    prefer_default_shot_durations(
        board,
        narrative_authority=narrative_authority,
        narrative_plan=narrative_plan,
    )
    normalize_transition_visuals(board)
    if not narrative_authority:
        for s in board.shots:
            s.action_desc = normalize_action_desc(s.action_desc)
    return board, []


def _project_shot_scene_from_outline(
    shot: Shot,
    brief: StoryboardOutlineShot | None,
    bible: Bible,
) -> bool:
    """Project the approved scene task onto a generated shot.

    Scene identity is planning authority, not creative model output.  The
    per-shot fallback therefore follows the same projection contract as a
    directed scene pack, including after a validator-normalized candidate is
    returned by the agent loop.
    """
    if brief is not None:
        if not any(str(value or "").strip() for value in (
            brief.scene_time,
            brief.scene_setting,
            brief.scene_name,
        )):
            return True
        shot.scene_time = str(brief.scene_time or "").strip()
        shot.scene_setting = str(brief.scene_setting or "").strip()
        shot.scene_name = str(brief.scene_name or "").strip()
    return bool(canonicalize_storyboard_scene(shot, bible))


def _expand_short_storyboard_source_excerpt(
    aligned: AlignedExcerpt,
    source_text: str,
) -> AlignedExcerpt:
    """Expand a proven short quote with contiguous authorized line context.

    One-character dialogue such as ``说！`` can be the exact delivery owned by
    a shot, but the audit contract intentionally requires a longer excerpt so
    reviewers can identify its source unambiguously.  The expansion is purely
    positional inside the authorized episode source; it does not rewrite text
    or classify dialogue content.
    """
    if len(_condense(aligned.excerpt)) >= SOURCE_EXCERPT_MIN_CHARS:
        return aligned
    source = source_text or ""
    if not source:
        return aligned

    start = max(0, int(aligned.start_offset))
    end = min(len(source), int(aligned.end_offset))
    line_start = source.rfind("\n", 0, start) + 1
    next_break = source.find("\n", end)
    line_end = len(source) if next_break < 0 else next_break
    start, end = line_start, line_end

    while len(_condense(source[start:end])) < SOURCE_EXCERPT_MIN_CHARS:
        next_start = end + 1 if end < len(source) else len(source)
        next_end = source.find("\n", next_start)
        if next_start < len(source):
            end = len(source) if next_end < 0 else next_end
            continue
        previous_break = source.rfind("\n", 0, max(0, start - 1))
        if start > 0:
            start = previous_break + 1
            continue
        break

    raw = source[start:end]
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw) - len(raw.rstrip())
    start += leading
    if trailing:
        end -= trailing
    excerpt = source[start:end]
    if len(_condense(excerpt)) < SOURCE_EXCERPT_MIN_CHARS:
        return aligned
    return AlignedExcerpt(
        excerpt=excerpt,
        start_offset=start,
        end_offset=end,
        match_chars=len(_condense(excerpt)),
        exact=True,
    )


def align_storyboard_source_evidence(
    shot: Shot,
    source_text: str,
    *,
    screenplay: EpisodeScreenplay | None = None,
) -> AlignedExcerpt | None:
    """Return the first delivery field backed by authorized contiguous source."""
    candidates = [
        shot.source_excerpt,
        *[dialogue.line for dialogue in shot.dialogues],
        shot.narration,
        *[item.text for item in shot.audio_timeline],
    ]
    aligned = next((
        aligned
        for candidate in candidates
        if str(candidate or "").strip()
        for aligned in [align_source_excerpt(
            str(candidate),
            source_text,
            min_match_chars=SOURCE_EXCERPT_MIN_CHARS,
        )]
        if aligned is not None
    ), None)
    if aligned is not None or screenplay is None:
        return aligned

    primary_event_id = str(shot.story_event_id or "").strip()
    event_ids = list(dict.fromkeys([
        *([primary_event_id] if primary_event_id else []),
        *[
            str(event_id).strip()
            for event_id in (shot.event_ids or [])
            if str(event_id).strip()
        ],
    ]))
    event_source_spans = {
        re.sub(
            r"[^A-Za-z0-9]",
            "",
            str(event.event_id or ""),
        ).lower(): str(event.source_span or "").strip()
        for event in (screenplay.events or [])
        if (
            str(event.event_id or "").strip()
            and str(event.source_span or "").strip()
        )
    }
    source_segments = {
        segment.segment_id: segment
        for segment in index_source_segments(source_text)
    }
    for event_id in event_ids:
        event_key = re.sub(
            r"[^A-Za-z0-9]",
            "",
            event_id,
        ).lower()
        source_span = event_source_spans.get(event_key, "")
        for segment_id in re.findall(r"SRC\d+", source_span.upper()):
            segment = source_segments.get(segment_id)
            if segment is None:
                continue
            semantic_text = "".join([
                str(shot.primary_action or ""),
                str(shot.action_desc or ""),
                *[
                    str(dialogue.line or "")
                    for dialogue in shot.dialogues
                ],
            ])
            semantic_compact = _condense(semantic_text)
            semantic_bigrams = {
                semantic_compact[index:index + 2]
                for index in range(max(0, len(semantic_compact) - 1))
            }
            sentences = [
                value.strip()
                for value in re.findall(
                    r"[^。！？\n]+[。！？]?",
                    segment.text,
                )
                if len(_condense(value)) >= SOURCE_EXCERPT_MIN_CHARS
            ]
            excerpt = max(
                sentences or [segment.text],
                key=lambda value: (
                    len({
                        _condense(value)[index:index + 2]
                        for index in range(
                            max(0, len(_condense(value)) - 1)
                        )
                    } & semantic_bigrams),
                    -len(value),
                ),
            )
            local_offset = segment.text.find(excerpt)
            start_offset = (
                segment.start_offset + max(0, local_offset)
            )
            return _expand_short_storyboard_source_excerpt(AlignedExcerpt(
                excerpt=excerpt,
                start_offset=start_offset,
                end_offset=start_offset + len(excerpt),
                match_chars=len(_condense(excerpt)),
                exact=True,
            ), source_text)
    authoritative_matches = [
        match
        for event_id in event_ids
        for event_key in [
            re.sub(r"[^A-Za-z0-9]", "", event_id).lower()
        ]
        if event_key in event_source_spans
        for match in [
            align_source_excerpt(
                event_source_spans[event_key],
                source_text,
                min_match_chars=SOURCE_EXCERPT_MIN_CHARS,
            )
        ]
        if match is not None
    ]
    unique_matches = {
        (match.start_offset, match.end_offset, match.excerpt): match
        for match in authoritative_matches
    }
    return next(iter(unique_matches.values())) if len(unique_matches) == 1 else None


def _validate_storyboard_shot_draft(draft: StoryboardShotDraft, *, episode: dict, bible: Bible,
                                    screenplay: EpisodeScreenplay, completed_shots: list[Shot],
                                    shot_no: int, allow_finish: bool, must_finish: bool,
                                    narrative_authority: bool | None = None,
                                    outline: StoryboardOutline | None = None,
                                    outline_covers: str = "", later_planned_covers: str = "",
                                    outline_scene_name: str = "",
                                    outline_narrative_task: StoryboardOutlineShot | None = None,
                                    source_text: str = "") -> list[str | Issue]:
    errors: list[str] = []
    structural_issues: list[Issue] = []
    expected_narrative_authority = screenplay.narrative_plan is not None
    if narrative_authority is None:
        narrative_authority = expected_narrative_authority
    elif narrative_authority != expected_narrative_authority:
        errors.append(
            "[NARRATIVE_AUTHORITY_MODE_MISMATCH] 逐镜校验模式必须与 "
            "screenplay.narrative_plan 的存在性一致"
        )
    if draft.episode_no != episode["episode_no"]:
        errors.append(f"episode_no={draft.episode_no}，必须等于 {episode['episode_no']}")
    if draft.shot.shot_no != shot_no:
        errors.append(f"shot.shot_no={draft.shot.shot_no}，当前只允许输出第 {shot_no} 镜")
    for field in ("first_frame_desc", "last_frame_desc"):
        if not str(getattr(draft.shot, field, "") or "").strip():
            errors.append(
                f"[REQUIRED_FIELD_MISSING] shot.{field} 是分镜生产必填字段"
            )
    if draft.is_final and not allow_finish:
        errors.append(f"当前第 {shot_no} 镜还不能作为最后一镜；本集至少需要更多镜头承接完整剧情")
    if must_finish and not draft.is_final:
        errors.append(
            f"当前已到本集收束位（大纲末镜/技术硬上限），第 {shot_no} 镜必须收束到尾钩并设置 is_final=true"
        )

    if outline_narrative_task is not None and not _project_shot_scene_from_outline(
        draft.shot,
        outline_narrative_task,
        bible,
    ):
        errors.append(
            f"[STORYBOARD_SCENE_AUTHORITY_UNRESOLVED] 第 {shot_no} 镜的批准大纲场景"
            "无法投影到本集场景库"
        )

    aligned_excerpt = align_storyboard_source_evidence(
        draft.shot,
        source_text,
        screenplay=screenplay,
    )
    if aligned_excerpt is None:
        if not str(draft.shot.source_excerpt or "").strip():
            errors.append(
                "[REQUIRED_FIELD_MISSING] shot.source_excerpt 是分镜生产必填字段"
            )
        else:
            errors.append(
                "shot.source_excerpt 无法在本集授权原文中找到足够强的连续依据；"
                "请从‘本镜可逐字摘录原文’中复制一段连续原文"
            )
    else:
        # Evidence is an audit field, not creative prose.  Canonicalize harmless
        # drift or another source-backed delivery field before saving the artifact.
        draft.shot.source_excerpt = aligned_excerpt.excerpt

    # 相邻镜允许共享同一主线段落的 source_excerpt（Renderability：不再用「必须推进原文」逼碎镜）。

    target = episode["target_duration_s"]
    board, identity_errors = _normalized_candidate_board(
        episode["episode_no"], completed_shots, draft.shot, bible, target,
        narrative_authority=narrative_authority,
        narrative_plan=screenplay.narrative_plan,
        screenplay=screenplay,
    )
    errors.extend(identity_errors)
    current = board.shots[-1]
    structural_issues.extend(_storyboard_shot_visual_identity_issues(
        current,
        outline_narrative_task,
        bible,
        screenplay,
        episode_id=str(episode.get("id") or episode["episode_no"]),
    ))
    # A per-shot checkpoint is only safe when the exact object can already be
    # compiled for the downstream image/video prompt.  Keep this as a typed
    # structural issue so AgentLoop repairs it now instead of accepting a
    # score-only candidate and discovering the same contract break at episode
    # publication time.
    try:
        from app.compiler import CompileError, compile_prompt

        compile_prompt(
            current.model_copy(deep=True),
            bible,
            screenplay=screenplay,
        )
    except CompileError as exc:
        structural_issues.append(Issue(
            code="SHOT_PROMPT_COMPILE_FAILED",
            severity=IssueSeverity.BLOCKER,
            category="structural",
            subject=f"storyboard_checkpoint:{episode.get('id') or episode['episode_no']}:{shot_no}",
            message=f"第 {shot_no} 镜下游 Prompt 合同不可编译：{exc}",
            evidence={
                "path": f"shots[{max(0, shot_no - 1)}]",
                "rule_id": "downstream_prompt_compile",
            },
            repair_hint="同步本镜可见身份、声轨身份与已声明的叙事身份合同",
            repairable=True,
        ))
    partial_errors = (
        validate_storyboard(
            board,
            bible,
            target,
            narrative_authority=narrative_authority,
            narrative_plan=screenplay.narrative_plan,
            screenplay=screenplay,
        )
        + information_ledger_errors(board, screenplay)
    )
    errors.extend(_filter_partial_storyboard_errors(
        partial_errors,
        current_index=len(completed_shots),
        current_shot_no=shot_no,
    ))
    errors.extend(validate_storyboard_shot_scene_alignment(
        current,
        screenplay,
        bible,
        expected_scene_name=outline_scene_name,
    ))
    # 向前承接：复合 covers 里已在前序镜头落实的事实不再算本镜漏戏（呼应大纲"可拆到相邻多镜"）。
    from app.spoken_contract import spoken_text_of as _spoken_text_of
    prior_text = "".join(
        (s.action_desc or "") + _spoken_text_of(s)
        for s in board.shots[:-1]
    )
    if outline_narrative_task is None:
        errors.extend(validate_storyboard_shot_covers_outline(
            current, outline_covers, shot_no,
            prior_text=prior_text, later_planned_covers=later_planned_covers,
        ))

    # The outline owns narrative allocation.  Compare the complete structural
    # task instead of re-inferring it from prose, while leaving legacy outlines
    # (whose narrative fields are all empty) on their existing compatibility path.
    if outline_narrative_task is not None:
        narrative_fields = tuple(
            field
            for field in _STORYBOARD_NARRATIVE_AUTHORITY_FIELDS
            if field not in _STORYBOARD_VISUAL_STAGING_FIELDS
        )
        planned = outline_narrative_task.model_dump(mode="json", include=set(narrative_fields))
        if any(value not in (None, "", [], {}) for value in planned.values()):
            actual = current.model_dump(mode="json", include=set(narrative_fields))
            for field in narrative_fields:
                if actual.get(field) != planned.get(field):
                    errors.append(
                        f"[OUTLINE_NARRATIVE_TASK_DRIFT] shot_no={shot_no}.{field} "
                        "必须原样承接分镜大纲的权威叙事任务"
                    )

    # Validate the full accepted prefix against the same narrative authority
    # graph.  Prefix mode checks references, ownership collisions, replay and
    # hand-offs without demanding delivery of future events; the closing shot
    # upgrades to the complete-episode gate.
    from app.narrative import validate_storyboard_narrative
    errors.extend(validate_storyboard_narrative(
        board,
        screenplay,
        outline=outline,
        complete=bool(draft.is_final or must_finish),
        expected_scope_id=str(
            episode.get("id") or f"episode-{episode['episode_no']}"
        ),
    ))

    if not (draft.is_final or must_finish):
        return [*list(dict.fromkeys(errors)), *structural_issues]

    # 收尾镜才跑整集兜底校验。必保留台词/剧情点、声轨这类"靠后续镜头分担"的缺口，
    # 在自愿收尾时不硬塞进单镜（那会让修复回路卡死），而是要求改判 is_final=false 继续补镜；
    # 撞到大纲末镜 / 技术硬上限（must_finish）时再无合法后续镜可分担——只硬拦主线缺口，
    # 禁止再用氛围声轨逼出计划外幻觉镜。
    episode_errors = (
        validate_storyboard_soundtrack(board, screenplay, target)
        + validate_storyboard_preserves_key_content(board, screenplay)
    )
    if episode_errors:
        hard = [
            e for e in episode_errors
            if ("must_keep" in e) or ("主线台词" in e) or ("主线剧情点" in e) or ("主线节拍" in e)
        ]
        if must_finish:
            errors.extend(hard)
        else:
            errors.append(
                f"本集整集必保留内容/声轨尚未达标，第 {shot_no} 镜暂不能收尾："
                "请将 is_final 设为 false 继续补镜，在后续镜头补齐——"
                + "；".join(episode_errors[:6]))
    return [*list(dict.fromkeys(errors)), *structural_issues]


def _storyboard_shot_visual_identity_issues(
    shot: Shot,
    task: StoryboardOutlineShot | None,
    bible: Bible,
    screenplay: EpisodeScreenplay,
    *,
    episode_id: str,
) -> list[Issue]:
    """Keep rendered identities inside the current narrative task relation.

    The episode identity contract answers whether an identity exists.  The
    outline task answers whether that identity belongs in this exact shot.
    These are different joins: accepting any episode-level identity here can
    silently replace a source-backed anonymous group with an unrelated named
    person.  Free-form background action remains prose; only persistent visual
    identities must be owned by ``task.visible_entity_ids``.
    """
    if task is None or screenplay.narrative_plan is None:
        return []

    from app.identity_contracts import (
        IdentityContractError,
        narrative_identity_resolver,
        storyboard_visual_identity_relation,
    )

    try:
        narrative_identity_resolver(bible, screenplay)
    except IdentityContractError:
        # The downstream compiler reports a malformed episode identity
        # contract with its complete diagnostic; do not duplicate it here.
        return []

    relation = storyboard_visual_identity_relation(
        shot,
        list(task.visible_entity_ids),
        bible,
        screenplay,
    )
    unexpected_ids = list(relation["unexpected_identity_ids"])
    unexpected_names = list(relation["unexpected_display_names"])
    binding_mismatches = list(relation["identity_binding_mismatches"])
    unresolved_tokens = [
        *relation["unresolved_visible_tokens"],
        *relation["unresolved_visible_entity_ids"],
    ]
    if not unexpected_ids and not binding_mismatches and not unresolved_tokens:
        return []

    task_identity_ids = set(relation["allowed_identity_ids"])
    task_names = list(relation["allowed_display_names"])
    task_audio_names = list(dict.fromkeys(
        str(value or "").strip()
        for value in (task.audio_cast or [])
        if str(value or "").strip()
    ))
    authority_conflicts: list[dict[str, str]] = []
    required_text = shot.required_text
    if (
        required_text is not None
        and str(required_text.strategy or "").strip() == "embedded_prop"
        and str(required_text.surface or "").strip()
    ):
        try:
            text_surface_identity = narrative_identity_resolver(
                bible,
                screenplay,
            ).resolve(required_text.surface, usage="visual")
        except IdentityContractError:
            text_surface_identity = None
        if (
            text_surface_identity is not None
            and text_surface_identity.identity_id in unexpected_ids
        ):
            authority_conflicts.append({
                "preserve_path": "required_text",
                "remove_identity_id": text_surface_identity.identity_id,
                "reason": "文字承载面要求该身份可见",
            })
    audio_repair = (
        f"；本镜声轨身份 {task_audio_names} 可继续保留在 audio_cast、dialogues "
        "与 audio_timeline，但未同时属于 visible_entity_ids 的说话者必须使用 "
        "offscreen_voice、lip_sync=false，且不得出现在动作或首尾帧画面中"
        if task_audio_names else ""
    )
    if binding_mismatches or unresolved_tokens:
        problem = (
            f"第 {shot.shot_no} 镜的 characters_visible 与 visible_entity_ids "
            "未按同一身份、同一顺序绑定"
        )
        if unresolved_tokens:
            problem += f"，且包含未登记 token {list(dict.fromkeys(unresolved_tokens))}"
    else:
        problem = (
            f"第 {shot.shot_no} 镜的持久可见身份 {unexpected_names} "
            "不属于本镜叙事任务"
        )
    return [Issue(
        code="SHOT_VISIBLE_IDENTITY_NOT_GROUNDED",
        severity=IssueSeverity.BLOCKER,
        category="structural",
        subject=f"storyboard_checkpoint:{episode_id}:{shot.shot_no}",
        message=(
            f"{problem}；本镜 visible_entity_ids 仅绑定 {task_names}"
        ),
        evidence={
            "path": f"shots[{max(0, shot.shot_no - 1)}]",
            "rule_id": "shot_visible_identity_relation",
            "task_identity_ids": sorted(task_identity_ids),
            "unexpected_identity_ids": unexpected_ids,
            "identity_binding_mismatches": binding_mismatches,
            "unresolved_visible_tokens": list(dict.fromkeys(unresolved_tokens)),
            "identity_relation_sources": relation["identity_relation_sources"],
            "text_provenance": relation["text_provenance"],
            "authority_conflicts": authority_conflicts,
        },
        repair_hint=(
            f"从当前 typed identity registry 原子重建 characters_visible 与"
            f"visible_entity_ids，再将 characters 与角色 reference_roles 精确收敛到"
            f"本镜任务对应的 {task_names}；required_text 与道具 text_state "
            f"保持独立文字 provenance，不得按其中姓名改写可见身份{audio_repair}"
        ),
        repairable=not authority_conflicts,
    )]


def validate_storyboard_visual_identity_contract(
    board: Storyboard,
    outline: StoryboardOutline,
    bible: Bible,
    screenplay: EpisodeScreenplay,
    *,
    episode_id: str,
) -> list[str]:
    """Apply the same shot/task visual relation gate to any board path."""
    briefs = {
        int(brief.shot_no): brief
        for brief in outline.shots
    }
    issues = [
        issue
        for shot in board.shots
        for issue in _storyboard_shot_visual_identity_issues(
            shot,
            briefs.get(int(shot.shot_no)),
            bible,
            screenplay,
            episode_id=episode_id,
        )
    ]
    return list(dict.fromkeys(issue.message for issue in issues))


async def _generate_episode_director_outline(
    episode: dict,
    source_text: str,
    bible: Bible,
    prev_ending: str,
    screenplay: EpisodeScreenplay,
) -> StoryboardOutline:
    """Plan the whole episode once; detailed shots are generated per scene."""
    key_content_block = _storyboard_key_content_block(screenplay)
    scene_library_block = _scene_library_block(bible, screenplay)
    scene_block = "\n".join(
        (
            f"场{scene.scene_no}｜{scene.scene_heading}｜功能：{scene.story_function}｜"
            f"人物：{'、'.join(scene.characters)}｜冲突：{scene.conflict or '无'}｜"
            f"入场：{scene.entry_state}｜离场：{scene.exit_state}｜"
            f"需建立：{'；'.join(scene.context_requirements)}｜摘要：{scene.summary}"
        )
        for scene in screenplay.scene_outline
    )
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》制定整集导演规划。

这是全局导演层，不是详细画面生成。镜头数量不设软上限或硬上限；由完整剧情交付、场景上下文、
动作可读性、情绪可读性和必要转场共同决定。50 镜或更多都允许，但每一镜必须有不可替代的作用。

【每场上下文合同】
1. scene_contexts 必须逐场覆盖剧本 scene_outline，scene_id 使用 SC001、SC002 连续编号。
2. 每场填写 entry_state、exit_state、transition_from_previous、spatial_axis。
3. context_requirements 把剧本的上下文要求改成稳定 ID（如 CTX-SC001-01）。
4. 新时空通常在前 1~3 镜建立时间、地点、空间轴线、人物位置和关键道具；若上一场已经通过
   动作、声音、视线或道具连续承接，可以只做必要的空间重定位，禁止机械重复远景。
5. required_before_shot_no 指向首次依赖该上下文的镜号，交付镜必须更早或同镜完成。

【每镜作用】
- purpose：明确本镜为什么存在，是推进事件、建立上下文、动作阶段、人物反应、情绪转折、
  证据揭示、空间重定位还是因果转场；不要只写“推进剧情”。
- resulting_change：写本镜结束后剧情、人物、情绪、空间或观众理解的实际变化。
- 每镜必须交付 spine_beat_ids、key_line_ids、information_ids 或 context_requirement_ids 中至少一类。
- 相同剧情与相同结果不得重复；有意重复必须填写 repeat_of_shot_id 和 repeat_gain，
  说明新增视角、反应、验证或兑现价值。

【摄影三元组】
- 每镜都规划 camera_size、camera_angle、camera_movement、camera_motivation。
- context：优先让空间、轴线和人物关系可读。
- action：动作段至少安排一镜中景/全景/远景配合跟随或横摇，完整看清动作路径、主体和作用对象。
- emotion：关键情绪转折至少安排一镜近景/特写配合固定或推近，让面部与姿态可读。
- dialogue：依据人物位置使用单人近景、双人镜、过肩或正反打，保持视线和轴线。
- evidence：给关键物件、结果或文字独立注意时间。
- transition：运动只服务动作、声音、视线、道具或因果承接，不能掩盖换人换装换景。

【拆镜原则】
- 单镜只演一个连续主动作或一个明确观看任务。
- 动作阶段、说话人变化、注意目标变化、空间关系变化、结果反应需要时均可拆镜。
- 不得为控制总时长合并不同动作，不得为了形式变化制造无作用空镜。
- duration_s 取 5~10 秒整数；总时长由所有有效镜头求和，不反向裁剪剧情。

完整剧本：
{screenplay.full_script_text}

场次合同：
{scene_block}

{key_content_block}
{scene_library_block}

上一集结尾：{prev_ending or '（本集为第一集）'}
本集结尾：{screenplay.ending_hook}

输出 JSON：
{{
  "episode_no": {episode['episode_no']},
  "scene_contexts": [{{
    "scene_id": "SC001", "scene_no": 1, "scene_name": str, "scene_time": str,
    "entry_state": str, "exit_state": str, "transition_from_previous": str,
    "spatial_axis": str,
    "context_requirements": [{{
      "requirement_id": "CTX-SC001-01", "description": str,
      "required_before_shot_no": int
    }}]
  }}],
  "shots": [{{
    "shot_no": 1, "shot_id": "SH0001", "scene_id": "SC001",
    "scene_time": str, "scene_name": str, "beat": str, "covers": str,
    "purpose": str, "context_requirement_ids": ["CTX-SC001-01"],
    "resulting_change": str,
    "readability_focus": "context|action|emotion|dialogue|evidence|transition",
    "camera_size": "远景|全景|中景|近景|特写",
    "camera_angle": str,
    "camera_movement": "固定|推近|拉远|横摇|跟随",
    "camera_motivation": str,
    "state_in": str, "primary_action": str, "state_out": str,
    "continuity_mode": "action_continuation|same_scene_cut|reaction_cut|reverse_angle|insert_detail|scene_change",
    "story_event_id": "E1", "spine_beat_ids": ["S01"],
    "key_line_ids": [], "information_ids": [], "duration_s": 5,
    "characters_visible": [], "audio_cast": [],
    "repeat_of_shot_id": null, "repeat_gain": ""
  }}]
}}"""

    def _validate(outline: StoryboardOutline) -> list[str]:
        errors = validate_storyboard_outline(
            outline,
            screenplay,
            int(episode.get("target_duration_s") or 0),
            bible=None,
        )
        contexts = outline.scene_contexts or []
        expected_scene_nos = list(range(1, len(screenplay.scene_outline) + 1))
        actual_scene_nos = [context.scene_no for context in contexts]
        if actual_scene_nos != expected_scene_nos:
            errors.append(
                f"scene_contexts.scene_no 必须为 {expected_scene_nos}，当前 {actual_scene_nos}"
            )
        context_ids: set[str] = set()
        scene_ids: set[str] = set()
        for context in contexts:
            if not context.scene_id.strip() or context.scene_id in scene_ids:
                errors.append(f"scene_contexts 含空或重复 scene_id：{context.scene_id!r}")
            scene_ids.add(context.scene_id)
            if len(context.entry_state.strip()) < 6 or len(context.exit_state.strip()) < 6:
                errors.append(f"{context.scene_id} 缺少可执行的 entry_state/exit_state")
            if not context.context_requirements:
                errors.append(f"{context.scene_id} 没有 context_requirements")
            for requirement in context.context_requirements:
                if (
                    not requirement.requirement_id.strip()
                    or requirement.requirement_id in context_ids
                ):
                    errors.append(
                        f"上下文 requirement_id 为空或重复：{requirement.requirement_id!r}"
                    )
                context_ids.add(requirement.requirement_id)
        delivered_context_ids: set[str] = set()
        planned_spine_ids: set[str] = set()
        valid_focuses = {
            "context", "action", "emotion", "dialogue", "evidence", "transition",
        }
        for shot in outline.shots:
            if shot.scene_id not in scene_ids:
                errors.append(f"大纲第 {shot.shot_no} 镜 scene_id={shot.scene_id} 不存在")
            if len(shot.purpose.strip()) < 6:
                errors.append(f"大纲第 {shot.shot_no} 镜 purpose 过短")
            if len(shot.resulting_change.strip()) < 4:
                errors.append(f"大纲第 {shot.shot_no} 镜 resulting_change 过短")
            if shot.readability_focus not in valid_focuses:
                errors.append(
                    f"大纲第 {shot.shot_no} 镜 readability_focus 非法：{shot.readability_focus!r}"
                )
            if shot.camera_size not in SHOT_SIZES:
                errors.append(f"大纲第 {shot.shot_no} 镜 camera_size 非法")
            if not shot.camera_angle.strip():
                errors.append(f"大纲第 {shot.shot_no} 镜 camera_angle 为空")
            if shot.camera_movement not in CAMERA_MOVES:
                errors.append(f"大纲第 {shot.shot_no} 镜 camera_movement 非法")
            if len(shot.camera_motivation.strip()) < 6:
                errors.append(f"大纲第 {shot.shot_no} 镜 camera_motivation 过短")
            unknown_context = set(shot.context_requirement_ids) - context_ids
            if unknown_context:
                errors.append(
                    f"大纲第 {shot.shot_no} 镜引用未知上下文：{sorted(unknown_context)}"
                )
            delivered_context_ids.update(shot.context_requirement_ids)
            planned_spine_ids.update(
                str(value).strip().upper() for value in shot.spine_beat_ids
            )
            if not (
                shot.spine_beat_ids
                or shot.key_line_ids
                or shot.information_ids
                or shot.context_requirement_ids
            ):
                errors.append(
                    f"大纲第 {shot.shot_no} 镜没有任何剧情或上下文交付项"
                )
        missing_context = sorted(context_ids - delivered_context_ids)
        if missing_context:
            errors.append(f"导演规划未安排上下文要求：{missing_context}")
        required_spine = {
            str(beat.beat_id).strip().upper()
            for beat in (
                screenplay.plot_spine.spine_beats
                if screenplay.plot_spine else []
            )
            if beat.must_keep and str(beat.beat_id).strip()
        }
        missing_spine = sorted(required_spine - planned_spine_ids)
        if missing_spine:
            errors.append(f"导演规划未安排 must_keep 主线节拍：{missing_spine}")
        return list(dict.fromkeys(errors))

    loop = AgentLoop(
        stage_key="storyboard_outline",
        contract_key="storyboard",
        goal=f"规划第 {episode['episode_no']} 集完整导演镜头与场景上下文",
        scope_type="episode",
        scope_id=str(episode.get("id") or f"episode-{episode['episode_no']}"),
        artifact_type="storyboard_outline",
        policy=AgentLoopPolicy(
            max_iterations=2,
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=False,
            repair_all_blockers=True,
        ),
    )
    outline = await _run_with_agent_loop(
        "整集导演规划",
        "storyboard_outline",
        prompt,
        StoryboardOutline,
        _validate,
        loop=loop,
        temperature=0.6,
        max_tokens=config.STORYBOARD_OUTLINE_MAX_TOKENS,
        repair_user_prompt_limit=None,
        repair_candidate_limit=None,
        prefill={"episode_no": episode["episode_no"]},
    )
    return outline


async def generate_storyboard_outline(episode: dict, source_text: str, bible: Bible,
                                      prev_ending: str, screenplay: EpisodeScreenplay) -> StoryboardOutline:
    """Compile narrative outlines locally; keep the model path for legacy scripts.

    The published narrative graph already owns global event/state relations.
    Requiring a model to restate the complete graph as one JSON object is both
    redundant and unbounded, so only later bounded scene packs use the model.
    """
    if not (screenplay.full_script_text or "").strip():
        raise StageError("分镜大纲", ["请先生成完整剧本，再规划分镜大纲"])
    if screenplay.narrative_plan is not None:
        from app.narrative_priority import (
            authoritative_outline_duration_s,
            compile_authoritative_delivery_outline,
        )
        from app.evidence import repository as evidence_repository
        from app.harness.contracts import get_contract
        from app.harness.types import EvidenceArtifact
        from app.narrative import validate_storyboard_narrative
        from app.narrative_outline import (
            narrative_outline_action_delivery_errors,
        )
        from app.validators import (
            outline_key_line_capacity_errors,
            outline_key_line_speaker_errors,
            outline_scene_coverage_errors,
        )

        screenplay, outline, compile_audit = (
            compile_authoritative_delivery_outline(
            screenplay,
            bible=bible,
        )
        )
        authoritative_duration_s = authoritative_outline_duration_s(outline)
        episode["target_duration_s"] = authoritative_duration_s
        ensure_storyboard_scene_contexts(outline, screenplay, bible)

        errors = [
            *narrative_outline_action_delivery_errors(
                outline,
                screenplay,
            ),
            *outline_key_line_capacity_errors(outline, screenplay),
            *outline_key_line_speaker_errors(outline, screenplay),
            *outline_scene_coverage_errors(outline, screenplay, bible),
            *narrative_outline_action_capacity_errors(
                outline,
                screenplay.narrative_plan,
            ),
            *validate_storyboard_narrative(
                board=None,
                screenplay=screenplay,
                outline=outline,
                complete=True,
                expected_scope_id=str(
                    episode.get("id")
                    or f"episode-{episode['episode_no']}"
                ),
            ),
        ]
        if errors:
            raise StageError(
                "分镜大纲确定性编译",
                list(dict.fromkeys(errors)),
            )

        parent_ids = [
            str(value)
            for value in (
                episode.get("screenplay_artifact_id"),
                episode.get("bible_artifact_id"),
            )
            if str(value or "").strip()
        ]
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_outline",
            scope_type="episode",
            scope_id=str(
                episode.get("id")
                or f"episode-{episode['episode_no']}"
            ),
            status="validated",
            trust_level="T2",
            content=outline.model_dump(mode="json"),
            parent_artifact_ids=parent_ids,
            contract_version=get_contract("storyboard").version,
            prompt_version="storyboard-outline-compiler-1.0.0",
            model_snapshot={
                **dict(getattr(outline, "_compile_audit", {}) or {}),
                **compile_audit,
                "final_shot_count": len(outline.shots),
                "authoritative_duration_s": authoritative_duration_s,
                "scene_batch_count": len(outline.scene_contexts),
            },
        ))
        object.__setattr__(
            outline,
            "evidence_artifact_id",
            artifact["id"],
        )
        log_provider_call(
            "storyboard_outline_local_compile",
            config.MODEL_TEXT,
            "COMPILED",
            None,
            0,
            meta={
                "episode_id": episode.get("id"),
                "episode_no": episode.get("episode_no"),
                "input_event_count": len(
                    screenplay.narrative_plan.events
                ),
                "final_shot_count": len(outline.shots),
                "scene_batch_count": len(outline.scene_contexts),
                "model_calls": 0,
                "artifact_id": artifact["id"],
            },
        )
        return outline
    if screenplay.narrative_plan is None:
        return await _generate_episode_director_outline(
            episode,
            source_text,
            bible,
            prev_ending,
            screenplay,
        )
    target = episode["target_duration_s"]
    min_shots, max_shots = storyboard_shot_count_range(target)
    key_content_block = _storyboard_key_content_block(screenplay)
    narrative_shot_contract = _storyboard_narrative_contract_block(
        include_outline_windows=True,
    )
    scene_library_block = _scene_library_block(bible, screenplay)
    is_first = int(episode.get("episode_no") or 0) == 1
    episode_hook = (episode.get("hook") or "").strip()
    episode_cliffhanger = (episode.get("cliffhanger") or "").strip()
    narrative_authority = screenplay.narrative_plan is not None
    if narrative_authority:
        first_rule = (
            "第 1 镜必须按 narrative_plan 中的首个可交付事件、观众先验与 "
            "readability window 定义开场；是否需要建场/引导镜由 assimilation task 判断，"
            "不得因集数或固定套路强行添加。"
        )
        rhythm_rule = (
            "镜头功能必须覆盖 narrative_plan.scene_contracts/arc_contracts 中实际声明为 "
            "applies 的压力、转折、兑现与处理节拍；not_applicable 的戏剧功能不得强套。"
        )
        director_staging_rule = (
            "调度必须执行 ShotTask.action_phase_ids 分配阶段的 actor/target、起止条件与"
            "completion_condition；对白、空间移动、作用对象变化或反应是否同镜，"
            "由动作前置/效果、阶段可观察性、观众注意任务和状态边界决定，"
            "不得套用预设动作词、道具类型或互动类型清单。"
        )
        outline_action_capacity_rule = (
            "动作容量只按 ShotTask.action_phase_ids 实际分配的 AtomicAction temporal phases "
            "的 estimated_min_s 总和计算，不设与语义无关的阶段个数阈值；"
            "阶段最短时间总和不得超过镜长。"
            "capacity_budget.action_phase_s 不得低于阶段最短时间，全部观看任务预算总和不得超过镜长。"
            "超限时由 AI 保持 precondition/effects/completion、阶段顺序与状态方程，"
            "只在 splittable_boundaries 声明的阶段边界重新分配相邻 ShotTask；"
            "无合法边界时上溯重构动作与任务，"
            "beat/covers/primary_action 的用词和同义改写不参与计数。"
        )
    else:
        first_rule = ("【本集是第一集】第 1 镜是全片开场建场镜：先交代世界观/主角处境/核心设定，再带出本集 hook。"
                      if is_first else (
                          f"第 1 镜必须承接上一集真实结尾：{episode_hook}，用本集原文真实内容尽快呼应/推进，"
                          f"不得无视，也不得凭空续写 {episode_hook} 之外的新剧情。"
                          if episode_hook else
                          "第 1 镜按剧本真实开场自然进入。"
                      ))
        rhythm_rule = "N 条镜头必须覆盖开端→发展→冲突→高潮→收束，篇幅按主线状态变化分配。"
        director_staging_rule = (
            "只用大形体动作。对白严格按话轮拆镜；若对白同时承担走位、离场或道具操作，"
            "必须规划为中景/全景动作对白镜；必须看见接触点的双人肢体互动才允许双人同框。"
        )
        outline_action_capacity_rule = (
            "动作容量与视频生成门禁一致：5~6s 最多 2 个顺序动作节拍，"
            "7~10s 最多 3 个；primary_action、beat、covers 任一字段超限都要拆镜。"
        )
    ending_rule = (
        f"最后一镜落到本集真实尾钩：{episode_cliffhanger}，用本集已有内容据实呈现，"
        "不得凭空发明该尾钩之外的下一集事件。"
        if episode_cliffhanger else
        "最后一镜按剧本/原文真实结束状态收束；本集确已完结、无遗留悬念时不得发明下一集钩子。"
    )
    scene_block = (chr(10).join(
        f"场{sc.scene_no}｜{sc.scene_heading}｜功能：{sc.story_function}｜摘要：{sc.summary}｜"
        f"冲突：{sc.conflict or '（无）'}｜转折：{sc.turn or '（无）'}"
        for sc in screenplay.scene_outline) if screenplay.scene_outline else "（未提供场次结构）")
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》规划【分镜大纲】。

你现在做的是全局节奏规划：把下方【完整剧本 / plot_spine】铺成有序的 N 条镜头。
镜头数由完整覆盖主线和场景上下文决定，不设数量上限；禁止无作用重复镜头。
must_keep spine 只是最低覆盖线，不是内容白名单；除 drop_list 明确授权删除外，
完整剧本每个场次、动作结果、必要反应、出入场和因果承接都必须落镜。
不写景别/运镜/首尾帧/台词原文。

{renderability_prompt_block()}

最重要的目标是节奏：后续会严格按这份大纲逐镜填充，所以——
- 每一条镜头都必须包含 state_in、state_out 和非空 shot_contribution。只要为当前叙事意图交付真实的证据、观众认知、情绪、时空定向或压力差，建立镜/反应镜/吸收镜的 primary_action_id 可为 null；但禁止两条镜头重复交付同一 action/target_delta，也禁止纯填充。
- scene_outline 的每一场必须按原顺序至少分配一镜；同一地点后来再次出现仍是新的场次，不能与前一次合并。场内出现新的连续动作、作用对象、说话人、可见结果或反应时继续拆镜。
- 地点、时间或叙事视角发生较大跳跃时，从观众连续观看角度决定是否增加建立场、人物到达、环境/道具承接或动作衔接镜；不得从“家里”无承接硬切到“学校”后直接进入核心对白。
- {rhythm_rule}
- {ending_rule}
- 禁止按文本长度机械拆分；每次拆镜必须对应新的动作、话轮或信息节拍。

【导演调度总则】{director_staging_rule}

完整剧本：
标题：{screenplay.title}
一句话梗概：{screenplay.logline}
场次结构：
{scene_block}

完整剧本文本：
{screenplay.full_script_text}

情绪曲线：{screenplay.emotional_curve}
结尾钩子：{screenplay.ending_hook}

{key_content_block}
{scene_library_block}
{narrative_shot_contract}
硬性约束：
1. 镜头数由完整覆盖剧本决定且不设上限；20、40、60 镜都合法，禁止为贴合目标时长主动省略剧情。shot_no 从 1 连续递增。大纲 duration_s **默认 5**，仅必要时取 6~10；每镜必须有独立作用。
2. 每条保留 beat/covers 兼容旧流程，同时必须填写上方叙事任务合同的 shot_id、scene_id、event_ids、primary_action_id、supporting_action_ids、action_phase_ids、visible_entity_ids、offscreen_action_actor_ids、offscreen_action_target_ids、action_participant_deliveries、capacity_budget、shot_contribution、逐先验 audience_state_paths、事实状态差、动作/阶段完成账本、readability_window_ids 与 boundary。state_in/primary_action/state_out、continuity_mode、story_event_id、spine_beat_ids、key_line_ids、new_information_ids、duration_s、characters_visible、audio_cast 继续保留。beat 只作为一句话摘要，不得替代结构化任务。
3. 相邻两镜 state_out -> state_in 与每个观众先验的 audience_state_out_target_id -> audience_state_in_id 都必须精确承接。primary_action_id 非空时必须唯一归属且不同于 completed_before_action_ids；为 null 时仍必须用 shot_contribution 证明新的叙事功能。
4. 上方主线台词/剧情点/spine 必须分配到 covers，并填写 key_line_ids（KL01..）与 spine_beat_ids（S01..）；drop_list 禁止分配。new_information_ids 只能引用 screenplay.information_ledger 已有 info_id。同一镜必保留口播字数不得超过该镜 duration_s 容量（10s≤{config.MAX_SPOKEN_CHARS_PER_SHOT}字），超限必须拆到相邻镜。
4b. 同一镜 key_line_ids 只能属于同一说话人；说话人变化就是切镜点。按“甲单人近景说完 → 乙单人反打回应”拆成相邻镜，禁止把问答双方和围观人群同时塞进一个对白镜头。
4c. 每条 must_keep spine 的 who 必须在分配该 S* 的某一镜中成为可见动作主体，does 必须真正拍出。旁观者的宣告、转述或评论不能替代事件主体完成原子动作；是否拆成“动作完成 → 结果/反应”由 action temporal phases、镜头时长与 readability budget 共同判定。
4d. {outline_action_capacity_rule}
5. covers 只写本镜必须拍出/说出的具体事实（可见动作、可听台词、可感知反应、可核对信息点）；禁止用纯抽象导演意图代替可观测证据；意图写入 shot_contribution/affective_delta，可拍事实写入 beat/primary_action/state_out。
6. {first_rule}
7. scene_time 与 scene_name 分开填：scene_time 直接引用本场 scene_contract 的开放文本时间或相对阶段；scene_name 必须是场景库规范名，同一物理场景不因人物走到门口/桌边而改名。scene_id 直接引用 scene_contract.scene_id，禁止整集复用同一个 scene_id。
8. beat 必须写清人物或作用对象跨越可见性/空间边界的原因、起始条件与完成条件。
9. continuity_mode 必须从 action_continuation / same_scene_cut / reaction_cut / reverse_angle / insert_detail / scene_change 中选择；只有 action_continuation 表示承接上一镜同一动作尾状态，其他同场景切换不得冒充动作连续。
10. story_event_id 只写剧本事件 E*；主线节拍写 spine_beat_ids（S*）；禁止把 S* 写入 story_event_id。
11. event_ids 是 narrative_plan.events 的权威引用；若同一事件也存在旧 events[] 台账，story_event_id 必须与 event_ids 中对应的主事件同义并优先复用同一 ID。第一镜 boundary 必须为 null，后续每镜的 boundary 必须连接真实相邻 shot_id 并阻止动作重演。

本集目标时长参考值 {target}s（不是上限；完整覆盖需要时允许明显超过）。
上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON（不要解释、不要 Markdown）：
{{"episode_no": {episode['episode_no']}, "shots": [{{"shot_no": int, "scene_time": "直接引用 scene_contract 的开放文本", "scene_name": "上方场景库规范名", "beat": "兼容字段：本镜推进的剧情一句话", "covers": "本镜任务的读者可读摘要；结构化交付以稳定 ID 与 shot_contribution 为准", "state_in": "本镜开始时人物/道具/信息状态", "primary_action": "本镜唯一主动作/主交付", "state_out": "本镜结束时的新状态", "continuity_mode": "action_continuation|same_scene_cut|reaction_cut|reverse_angle|insert_detail|scene_change", "story_event_id": "对应 screenplay.events[].event_id（E*）或空", "spine_beat_ids": ["S01"], "key_line_ids": ["KL01"], "new_information_ids": ["本镜首次交付的信息ID，可空"], "duration_s": 5, "characters_visible": ["本镜画面可见角色"], "audio_cast": ["本镜发声角色/功能性声音，可空"]}}]}}"""
    log_provider_call(
        "storyboard_outline_prompt", config.MODEL_TEXT, "PROMPT_READY", None, 0,
        meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
              "target_duration_s": target, "shot_range": [min_shots, max_shots],
              "prompt_chars": len(prompt), "contract_version": "renderability_v1"})
    def _check(o: StoryboardOutline) -> list[str]:
        if screenplay.narrative_plan is not None:
            # New artifacts are governed by the authority graph.  Keep this
            # preflight structural and relation-based: legacy covers wording,
            # episode-number heuristics and deterministic content transformers
            # must not rewrite an AI-authored narrative allocation.
            from app.narrative_outline import (
                narrative_outline_action_delivery_errors,
                normalize_narrative_storyboard_outline,
            )

            projection_changes = normalize_narrative_storyboard_outline(
                o,
                screenplay,
                bible=bible,
            )
            if projection_changes:
                log_provider_call(
                    "storyboard_outline_authority_projection",
                    config.MODEL_TEXT,
                    "NORMALIZED",
                    None,
                    0,
                    meta={
                        "episode_id": episode.get("id"),
                        "episode_no": episode.get("episode_no"),
                        "changes": projection_changes,
                    },
                )
            from app.validators import (
                assign_outline_delivery_ids,
                normalize_outline_spoken_durations,
                normalize_outline_dialogue_ownership,
                outline_scene_coverage_errors,
                outline_key_line_capacity_errors,
                outline_key_line_speaker_errors,
                split_outline_on_speaker_changes,
                split_outline_over_action_capacity,
                split_outline_over_key_line_capacity,
            )

            assign_outline_delivery_ids(o, screenplay)
            split_events = [
                *split_outline_over_action_capacity(
                    o,
                    max_shots=max_shots,
                ),
                *split_outline_on_speaker_changes(
                    o,
                    screenplay,
                    max_shots=max_shots,
                ),
                *split_outline_over_key_line_capacity(
                    o,
                    screenplay,
                    max_shots=max_shots,
                ),
                *normalize_outline_dialogue_ownership(
                    o,
                    screenplay,
                ),
            ]
            if split_events:
                projection_changes = (
                    normalize_narrative_storyboard_outline(
                        o,
                        screenplay,
                        bible=bible,
                    )
                )
                projection_changes.extend(
                    normalize_outline_dialogue_ownership(
                        o,
                        screenplay,
                    )
                )
                log_provider_call(
                    "storyboard_outline_semantic_split",
                    config.MODEL_TEXT,
                    "NORMALIZED",
                    None,
                    0,
                    meta={
                        "episode_id": episode.get("id"),
                        "episode_no": episode.get("episode_no"),
                        "splits": split_events,
                        "projection_changes": projection_changes,
                    },
                )
            o.scene_contexts = []
            ensure_storyboard_scene_contexts(
                o,
                screenplay,
                bible,
            )
            for change in normalize_outline_spoken_durations(o, screenplay):
                log_provider_call(
                    "storyboard_outline_spoken_duration",
                    config.MODEL_TEXT,
                    "DURATION_NORMALIZED",
                    None,
                    0,
                    meta={
                        "episode_id": episode.get("id"),
                        "episode_no": episode.get("episode_no"),
                        **change,
                    },
                )
            narrative_errors: list[str] = []
            if not o.shots:
                narrative_errors.append("[OUTLINE_EMPTY] 分镜大纲不能为空")
            if SHOT_HARD_MAX is not None and len(o.shots) > SHOT_HARD_MAX:
                narrative_errors.append(
                    f"[OUTLINE_HARD_LIMIT] 分镜数 {len(o.shots)} 超过技术上限 {SHOT_HARD_MAX}"
                )
            actual_order = [shot.shot_no for shot in o.shots]
            expected_order = list(range(1, len(o.shots) + 1))
            if actual_order != expected_order:
                narrative_errors.append(
                    f"[OUTLINE_ORDER_INVALID] shot_no 必须为 {expected_order}，当前 {actual_order}"
                )
            for shot in o.shots:
                if not (
                    config.VIDEO_DURATION_MIN_S
                    <= int(shot.duration_s or 0)
                    <= config.VIDEO_DURATION_MAX_S
                ):
                    narrative_errors.append(
                        f"[OUTLINE_DURATION_INVALID] shot_id={shot.shot_id or shot.shot_no} "
                        f"duration_s 必须在 {config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S}"
                    )
            narrative_errors.extend(
                narrative_outline_action_delivery_errors(
                    o,
                    screenplay,
                )
            )
            narrative_errors.extend(
                outline_key_line_capacity_errors(o, screenplay)
            )
            narrative_errors.extend(
                outline_key_line_speaker_errors(o, screenplay)
            )
            narrative_errors.extend(
                outline_scene_coverage_errors(
                    o,
                    screenplay,
                    bible,
                )
            )
            narrative_errors.extend(narrative_outline_action_capacity_errors(
                o,
                screenplay.narrative_plan,
            ))
            from app.narrative import validate_storyboard_narrative

            narrative_errors.extend(validate_storyboard_narrative(
                board=None,
                screenplay=screenplay,
                outline=o,
                complete=True,
                expected_scope_id=str(
                    episode.get("id") or f"episode-{episode['episode_no']}"
                ),
            ))
            return list(dict.fromkeys(narrative_errors))
        # VAL-422：回填 KL*/S*，并在超容时确定性拆镜，再跑大纲校验。
        from app.validators import (
            assign_outline_delivery_ids,
            split_outline_over_action_capacity,
            split_outline_on_speaker_changes,
            split_outline_over_key_line_capacity,
        )
        for c in assign_outline_delivery_ids(o, screenplay):
            log_provider_call(
                "storyboard_outline_assign_ids", config.MODEL_TEXT, "DELIVERY_IDS_ASSIGNED", None, 0,
                meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                      "stage": "分镜大纲", **c},
            )
        for ev in split_outline_over_action_capacity(o, max_shots=max_shots):
            log_provider_call(
                "storyboard_outline_action_split", config.MODEL_TEXT, "ACTION_CAPACITY_SPLIT", None, 0,
                meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                      "stage": "分镜大纲", **ev},
            )
        for ev in split_outline_on_speaker_changes(
            o, screenplay, max_shots=max_shots,
        ):
            log_provider_call(
                "storyboard_outline_speaker_split", config.MODEL_TEXT, "DIALOGUE_TURN_SPLIT", None, 0,
                meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                      "stage": "分镜大纲", **ev},
            )
        for ev in split_outline_over_key_line_capacity(o, screenplay, max_shots=max_shots):
            log_provider_call(
                "storyboard_outline_capacity_split", config.MODEL_TEXT, "KEY_LINE_CAPACITY_SPLIT", None, 0,
                meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                      "stage": "分镜大纲", **ev},
            )
        errors = validate_storyboard_outline(o, screenplay, target, bible=bible)
        from app.narrative import validate_storyboard_narrative
        errors.extend(validate_storyboard_narrative(
            board=None,
            screenplay=screenplay,
            outline=o,
            complete=True,
            expected_scope_id=str(
                episode.get("id") or f"episode-{episode['episode_no']}"
            ),
        ))
        return list(dict.fromkeys(errors))

    outline_loop = AgentLoop(
        stage_key="storyboard_outline",
        contract_key="storyboard",
        goal=f"规划第 {episode['episode_no']} 集完整逐镜节奏与必保留内容分配（仅一次 Baseline）",
        scope_type="episode",
        scope_id=str(episode.get("id") or f"episode-{episode['episode_no']}"),
        artifact_type="storyboard_outline",
        policy=AgentLoopPolicy(
            max_iterations=4,
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=False,
            repair_all_blockers=True,
            baseline_only=False,
        ),
    )
    outline = await _run_with_agent_loop(
        "分镜大纲", "storyboard_outline", prompt, StoryboardOutline, _check,
        loop=outline_loop, temperature=0.6,
        max_tokens=config.STORYBOARD_OUTLINE_MAX_TOKENS,
        repair_user_prompt_limit=None,
        repair_candidate_limit=6000,
        prefill={"episode_no": episode["episode_no"]},
    )
    if screenplay.narrative_plan is not None:
        from app.narrative_outline import (
            narrative_outline_action_delivery_errors,
        )
        from app.validators import (
            normalize_outline_dialogue_ownership,
            normalize_outline_spoken_durations,
            outline_key_line_capacity_errors,
            outline_key_line_speaker_errors,
        )

        normalize_outline_dialogue_ownership(outline, screenplay)
        normalize_outline_spoken_durations(outline, screenplay)
        final_errors = [
            *(
                f"[OUTLINE_DURATION_INVALID] shot_id={shot.shot_id or shot.shot_no} "
                f"duration_s 必须在 {config.VIDEO_DURATION_MIN_S}~"
                f"{config.VIDEO_DURATION_MAX_S}"
                for shot in outline.shots
                if not (
                    config.VIDEO_DURATION_MIN_S
                    <= int(shot.duration_s or 0)
                    <= config.VIDEO_DURATION_MAX_S
                )
            ),
            *narrative_outline_action_delivery_errors(
                outline,
                screenplay,
            ),
            *outline_key_line_capacity_errors(outline, screenplay),
            *outline_key_line_speaker_errors(outline, screenplay),
        ]
        if final_errors:
            raise StageError(
                "分镜大纲硬合同",
                list(dict.fromkeys(final_errors)),
            )
        ensure_storyboard_scene_contexts(outline, screenplay, bible)
        return outline
    # 减重试 #2：第一集第 1 镜是强制建场镜，把派给它的判决/反转类 covers 顺延合并到第 2 镜，
    # 避免逐镜阶段"照建场写→漏 covers / 硬塞判决→引入圣经外角色"的连环重试。
    # 在校验通过后做确定性顺延，不扰动大纲修复回路。
    for c in defer_establishing_covers(outline, int(episode.get("episode_no") or 0)):
        log_provider_call(
            "storyboard_outline_defer_covers", config.MODEL_TEXT, "COVERS_DEFERRED", None, 0,
            meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                  "stage": "分镜大纲", **c})
    # 顺延 covers 后再次做容量拆镜，避免建场镜顺延把多条 KL 堆回同一镜。
    from app.validators import (
        assign_outline_delivery_ids,
        split_outline_over_action_capacity,
        split_outline_on_speaker_changes,
        split_outline_over_key_line_capacity,
    )
    assign_outline_delivery_ids(outline, screenplay)
    for ev in split_outline_over_action_capacity(outline, max_shots=max_shots):
        log_provider_call(
            "storyboard_outline_action_split", config.MODEL_TEXT, "ACTION_CAPACITY_SPLIT", None, 0,
            meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                  "stage": "分镜大纲", "phase": "post_defer", **ev},
        )
    for ev in split_outline_on_speaker_changes(
        outline, screenplay, max_shots=max_shots,
    ):
        log_provider_call(
            "storyboard_outline_speaker_split", config.MODEL_TEXT, "DIALOGUE_TURN_SPLIT", None, 0,
            meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                  "stage": "分镜大纲", "phase": "post_defer", **ev},
        )
    for ev in split_outline_over_key_line_capacity(outline, screenplay, max_shots=max_shots):
        log_provider_call(
            "storyboard_outline_capacity_split", config.MODEL_TEXT, "KEY_LINE_CAPACITY_SPLIT", None, 0,
            meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                  "stage": "分镜大纲", "phase": "post_defer", **ev},
        )
    # Post-processing is allowed to change allocation only when the resulting
    # artifact still satisfies the same authority graph.  Never publish a
    # deterministically split/deferred outline whose IDs or ownership drifted.
    from app.narrative import validate_storyboard_narrative
    post_narrative_errors = validate_storyboard_narrative(
        board=None,
        screenplay=screenplay,
        outline=outline,
        complete=True,
        expected_scope_id=str(
            episode.get("id") or f"episode-{episode['episode_no']}"
        ),
    )
    if post_narrative_errors:
        raise StageError("分镜大纲叙事合同", post_narrative_errors)
    ensure_storyboard_scene_contexts(outline, screenplay, bible)
    return outline


def _render_storyboard_outline(
    outline: StoryboardOutline | None,
    current_shot_no: int,
    valid_info_ids: set[str] | None = None,
) -> str:
    """Render a bounded local window plus the ID-only global sequence."""
    if not outline or not outline.shots:
        return ""
    total = len(outline.shots)
    rows = []
    neighborhood = {
        shot_no for shot_no in range(current_shot_no - 1, current_shot_no + 2)
        if 1 <= shot_no <= total
    }
    for s in outline.shots:
        if s.shot_no not in neighborhood:
            continue
        scene_label = s.scene_name or s.scene_setting
        scene = f"｜{s.scene_time or '时间未定'}｜{scene_label}" if scene_label else ""
        covers = f"｜落实：{s.covers}" if (s.covers or "").strip() else ""
        state = ""
        if (s.state_in or s.primary_action or s.state_out):
            state = f"｜状态：{s.state_in or '未填'} -> {s.primary_action or s.beat} -> {s.state_out or '未填'}"
        event = f"｜event:{s.story_event_id}" if (s.story_event_id or "").strip() else ""
        info_ids = [
            info_id for info_id in s.new_information_ids
            if valid_info_ids is None or info_id in valid_info_ids
        ]
        info = f"｜info:{','.join(info_ids)}" if info_ids else ""
        narrative_bits = [
            f"shot_id:{s.shot_id}" if s.shot_id else "",
            f"scene_id:{s.scene_id}" if s.scene_id else "",
            f"events:{','.join(s.event_ids)}" if s.event_ids else "",
            f"action:{s.primary_action_id}" if s.primary_action_id else "action:null",
            (
                f"contribution:{s.shot_contribution.shot_contribution_id}"
                if s.shot_contribution else "contribution:缺失"
            ),
            (
                "target_deltas:" + ",".join(s.shot_contribution.target_delta_ids)
                if s.shot_contribution and s.shot_contribution.target_delta_ids else ""
            ),
            f"windows:{','.join(s.readability_window_ids)}" if s.readability_window_ids else "",
            (
                f"boundary:{s.narrative_boundary_from_previous.boundary_id}"
                if s.narrative_boundary_from_previous else "boundary:null"
            ),
        ]
        narrative = "｜叙事任务：" + " / ".join(
            item for item in narrative_bits if item
        )
        mark = "  ← 本镜" if s.shot_no == current_shot_no else ""
        rows.append(
            f"第{s.shot_no}/{total}镜{scene}：{s.beat}{state}{event}{info}"
            f"{narrative}{covers}{mark}"
        )
    sequence = " ".join(
        f"{s.shot_no}:{s.shot_id or '-'}:{','.join(s.event_ids) or s.story_event_id or '-'}"
        for s in outline.shots
    )
    return (
        "本集分镜大纲（仅展开相邻任务；本镜只落实标注「← 本镜」的一条）：\n"
        + "\n".join(rows)
        + "\n全局顺序索引（镜号:shot_id:event_ids，仅用于确认位置，不得据此扩写）：\n"
        + sequence
    )


def _outline_brief(outline: StoryboardOutline | None, shot_no: int):
    if outline and 1 <= shot_no <= len(outline.shots):
        return outline.shots[shot_no - 1]
    return None


def _screenplay_scene_parts(screenplay: EpisodeScreenplay, scene_no: int) -> tuple[str, str]:
    scene = next(
        (
            item
            for item in screenplay.scene_outline
            if int(item.scene_no or 0) == scene_no
        ),
        None,
    )
    if scene is None:
        return "", ""
    heading = re.sub(
        r"^\s*【?场\s*\d+】?\s*",
        "",
        str(scene.scene_heading or "").strip(),
    )
    parts = re.split(r"\s*[/／]\s*", heading, maxsplit=1)
    if len(parts) == 2:
        # ``scene_heading`` owns an explicit ``scene_time / scene_name``
        # positional contract.  Do not reinterpret either side through a
        # language whitelist: relative time labels and new locales are open
        # vocabulary by design.
        return parts[0].strip(), parts[1].strip()
    return "", heading


def ensure_storyboard_scene_contexts(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    bible: Bible | None = None,
) -> list[dict[str, Any]]:
    """Build batch boundaries from an approved outline without another model call."""
    if not outline.shots:
        return []

    script_scenes = sorted(
        list(screenplay.scene_outline or []),
        key=lambda item: int(item.scene_no or 0),
    )
    changes: list[dict[str, Any]] = []
    narrative_scene_numbers = {
        str(contract.scene_id or "").strip(): index
        for index, contract in enumerate(
            list(
                screenplay.narrative_plan.scene_contracts
                if screenplay.narrative_plan is not None
                else []
            ),
            start=1,
        )
        if str(contract.scene_id or "").strip()
    }
    preserves_narrative_scene_ids = bool(narrative_scene_numbers) and all(
        str(brief.scene_id or "").strip() in narrative_scene_numbers
        for brief in outline.shots
    )

    if (
        bible is not None
        and preserves_narrative_scene_ids
        and len(script_scenes) == len(narrative_scene_numbers)
    ):
        from app.scene_contract import compose_scene_setting

        canonical_by_scene_id: dict[str, str] = {}
        for scene_id, scene_no in narrative_scene_numbers.items():
            script_scene = script_scenes[scene_no - 1]
            canonical = match_scene_name(
                script_scene.scene_heading,
                list(getattr(bible, "scenes", None) or []),
                allow_fuzzy=False,
            )
            if canonical:
                canonical_by_scene_id[scene_id] = canonical
        for brief in outline.shots:
            scene_id = str(brief.scene_id or "").strip()
            canonical = canonical_by_scene_id.get(scene_id)
            if not canonical or brief.scene_name == canonical:
                continue
            previous = str(brief.scene_name or brief.scene_setting or "")
            brief.scene_name = canonical
            brief.scene_setting = compose_scene_setting(
                str(brief.scene_time or ""),
                canonical,
                fallback=str(brief.scene_setting or ""),
            )
            changes.append({
                "scene_id": scene_id,
                "shot_no": int(brief.shot_no),
                "field": "scene_name",
                "from": previous,
                "to": canonical,
                "reason": "screenplay_scene_alias_authority",
            })
        for context in outline.scene_contexts:
            canonical = canonical_by_scene_id.get(
                str(context.scene_id or "").strip()
            )
            if canonical:
                context.scene_name = canonical

    if outline.scene_contexts:
        return changes

    scene_id_bindings: defaultdict[str, set[tuple[str, str]]] = (
        defaultdict(set)
    )
    for brief in outline.shots:
        scene_id = str(brief.scene_id or "").strip()
        if scene_id:
            scene_id_bindings[scene_id].add((
                str(brief.scene_name or brief.scene_setting).strip(),
                str(brief.scene_time or "").strip(),
            ))

    # Narrative scene IDs are immutable graph references.  A physical scene may
    # legitimately change its open-vocabulary time/name wording inside one
    # dramatic scene contract; splitting on that wording and minting SC15+
    # leaves storyboard shots pointing outside narrative_plan.scene_contracts.
    # Legacy/model-authored outlines do not have this authority relation and
    # keep the historical physical-run partitioning below.
    runs: list[tuple[str, list[StoryboardOutlineShot]]] = []
    for brief in outline.shots:
        scene_name = str(brief.scene_name or brief.scene_setting).strip()
        scene_time = str(brief.scene_time or "").strip()
        scene_id = str(brief.scene_id or "").strip()
        scene_key = (
            f"id:{scene_id}"
            if (
                scene_id
                and (
                    preserves_narrative_scene_ids
                    or len(scene_id_bindings[scene_id]) == 1
                )
            )
            else f"scene:{scene_name}:{scene_time}"
        )
        if runs and runs[-1][0] == scene_key:
            runs[-1][1].append(brief)
        else:
            runs.append((scene_key, [brief]))

    contexts: list[StoryboardSceneContext] = []
    script_cursor = 0
    for run_index, (_scene_key, briefs) in enumerate(runs, start=1):
        first = briefs[0]
        last = briefs[-1]
        script_scene = None
        if script_cursor < len(script_scenes):
            candidate = script_scenes[script_cursor]
            _candidate_time, candidate_name = _screenplay_scene_parts(
                screenplay,
                int(candidate.scene_no or 0),
            )
            candidate_canonical = (
                match_scene_name(
                    candidate.scene_heading,
                    list(getattr(bible, "scenes", None) or []),
                    allow_fuzzy=False,
                )
                if bible is not None
                else None
            )
            run_name = str(
                first.scene_name or first.scene_setting or ""
            ).strip()
            run_token = re.sub(r"[\W_]+", "", run_name).casefold()
            candidate_token = re.sub(
                r"[\W_]+",
                "",
                str(candidate_canonical or candidate_name),
            ).casefold()
            if (
                run_token
                and candidate_token
                and (
                    run_token == candidate_token
                    or run_token in candidate_token
                    or candidate_token in run_token
                )
            ):
                script_scene = candidate
                script_cursor += 1
        script_scene_no = int(
            getattr(script_scene, "scene_no", run_index) or run_index
        )
        scene_id = (
            str(first.scene_id or "").strip()
            if preserves_narrative_scene_ids
            else f"SC{run_index:02d}"
        )
        context_scene_no = narrative_scene_numbers.get(scene_id, run_index)
        heading_time, heading_name = _screenplay_scene_parts(
            screenplay,
            script_scene_no,
        )
        scene_time = str(first.scene_time or heading_time).strip()
        scene_name = str(
            first.scene_name
            or first.scene_setting
            or heading_name
        ).strip()
        requirements: list[StoryboardContextRequirement] = []
        requirement_ids: list[str] = []
        for requirement_index, description in enumerate(
            list(getattr(script_scene, "context_requirements", None) or []),
            start=1,
        ):
            text = str(description or "").strip()
            if not text:
                continue
            requirement_id = f"CTX-{scene_id}-{requirement_index:02d}"
            requirement_ids.append(requirement_id)
            requirements.append(StoryboardContextRequirement(
                requirement_id=requirement_id,
                description=text,
                required_before_shot_no=int(first.shot_no),
            ))
        if requirement_ids and not first.context_requirement_ids:
            first.context_requirement_ids = list(requirement_ids)

        for brief in briefs:
            brief.scene_id = scene_id
            if not brief.scene_time:
                brief.scene_time = scene_time
            if not brief.scene_name:
                brief.scene_name = scene_name
        context = StoryboardSceneContext(
            scene_id=scene_id,
            scene_no=context_scene_no,
            scene_name=scene_name,
            scene_time=scene_time,
            entry_state=(
                str(first.state_in or "").strip()
                or str(getattr(script_scene, "entry_state", "") or "").strip()
                or "本场开始状态由首镜 state_in 承接"
            ),
            exit_state=(
                str(last.state_out or "").strip()
                or str(getattr(script_scene, "exit_state", "") or "").strip()
                or "本场结束状态由末镜 state_out 交付"
            ),
            transition_from_previous=(
                "首场直接建立"
                if run_index == 1
                else str(first.continuity_mode or "scene_change")
            ),
            spatial_axis=scene_id,
            context_requirements=requirements,
        )
        contexts.append(context)
        changes.append({
            "scene_id": scene_id,
            "shot_start": int(first.shot_no),
            "shot_end": int(last.shot_no),
            "requirement_ids": requirement_ids,
        })
    outline.scene_contexts = contexts
    return changes


def _parse_key_line(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    for separator in ("：", ":"):
        speaker, found, line = text.partition(separator)
        if found and speaker.strip() and line.strip():
            return speaker.strip(), line.strip()
    return "", text


def _scene_pack_dialogues(
    brief: StoryboardOutlineShot,
    screenplay: EpisodeScreenplay,
    emotions: dict[str, str],
    *,
    bible: Bible,
) -> list[Dialogue]:
    catalog = key_line_catalog(screenplay)
    visible_tokens = (
        list(brief.characters_visible)
        if brief.characters_visible
        else list(brief.visible_entity_ids)
    )
    visible_names = set(_canonical_scene_pack_names(
        visible_tokens,
        bible=bible,
        screenplay=screenplay,
        usage="visual",
    ))
    dialogues: list[Dialogue] = []
    for raw_id in brief.key_line_ids:
        key_id = str(raw_id or "").strip().upper()
        speaker, line = _parse_key_line(catalog.get(key_id, ""))
        if not speaker or not line:
            continue
        canonical_speakers = _canonical_scene_pack_names(
            [speaker],
            bible=bible,
            screenplay=screenplay,
            usage="voice",
        )
        canonical_speaker = canonical_speakers[0] if canonical_speakers else speaker
        emotion = str(
            emotions.get(key_id)
            or emotions.get(line)
            or "平静"
        ).strip()
        dialogues.append(Dialogue(
            speaker=canonical_speaker,
            line=line,
            emotion=emotion if emotion in EMOTIONS else "平静",
            delivery=(
                "spoken_dialogue"
                if canonical_speaker in visible_names
                else "offscreen_voice"
            ),
        ))
    return dialogues


def _canonical_scene_pack_names(
    values: list[str],
    *,
    bible: Bible,
    screenplay: EpisodeScreenplay,
    usage: str,
) -> list[str]:
    if screenplay.narrative_plan is None:
        return list(dict.fromkeys(
            str(value or "").strip() for value in values if str(value or "").strip()
        ))
    from app.identity_contracts import (
        IdentityContractError,
        narrative_identity_resolver,
    )

    resolver = narrative_identity_resolver(bible, screenplay)
    resolved: list[str] = []
    for value in values:
        token = str(value or "").strip()
        if not token:
            continue
        try:
            identity = resolver.resolve(token, usage=usage)
        except IdentityContractError:
            continue
        if identity.display_name not in resolved:
            resolved.append(identity.display_name)
    return resolved


def _scene_pack_characters(
    brief: StoryboardOutlineShot,
    dialogues: list[Dialogue],
    *,
    bible: Bible,
    screenplay: EpisodeScreenplay,
    fallback: list[str],
) -> list[str]:
    if screenplay.narrative_plan is not None:
        permitted = _canonical_scene_pack_names(
            list(brief.visible_entity_ids),
            bible=bible,
            screenplay=screenplay,
            usage="visual",
        )
        planned = _canonical_scene_pack_names(
            list(brief.characters_visible),
            bible=bible,
            screenplay=screenplay,
            usage="visual",
        )
        authorized = [
            name for name in planned if name in set(permitted)
        ] or permitted
        requested = _canonical_scene_pack_names(
            [
                *fallback,
                *[
                    dialogue.speaker
                    for dialogue in dialogues
                    if dialogue.delivery == "spoken_dialogue"
                ],
            ],
            bible=bible,
            screenplay=screenplay,
            usage="visual",
        )
        if requested and set(requested).issubset(authorized):
            return requested
        # The outline owns the permitted identity relation.  An empty or
        # out-of-relation directing choice falls back to that relation and is
        # then rejected by the ordinary render-capacity gate when necessary.
        return authorized
    visual_candidates = (
        list(brief.characters_visible)
        if brief.characters_visible
        else list(brief.visible_entity_ids)
    )
    return _canonical_scene_pack_names(
        [
            *visual_candidates,
            *fallback,
            *[
                dialogue.speaker
                for dialogue in dialogues
                if dialogue.delivery == "spoken_dialogue"
            ],
        ],
        bible=bible,
        screenplay=screenplay,
        usage="visual",
    )


def _scene_pack_visual_partition(
    characters: list[str],
    brief: StoryboardOutlineShot,
    screenplay: EpisodeScreenplay,
    bible: Bible,
) -> tuple[list[str], list[str], list[str]]:
    """Project a model-chosen focal cast onto graph actor/target relations."""
    if screenplay.narrative_plan is None:
        return list(brief.visible_entity_ids), [], []
    from app.identity_contracts import (
        IdentityContractError,
        narrative_identity_resolver,
    )

    resolver = narrative_identity_resolver(bible, screenplay)
    permitted_ids = set(brief.visible_entity_ids)
    visible_ids: list[str] = []
    for character in characters:
        try:
            identity = resolver.resolve(character, usage="visual")
        except IdentityContractError:
            continue
        if (
            identity.identity_id in permitted_ids
            and identity.identity_id not in visible_ids
        ):
            visible_ids.append(identity.identity_id)
    actor_ids, target_ids = _bound_action_relation_ids(
        screenplay,
        brief,
        bible=bible,
    )
    bound_action_ids = {
        *([brief.primary_action_id] if brief.primary_action_id else []),
        *brief.supporting_action_ids,
    }
    contracted_offscreen_ids = {
        delivery.participant_id
        for delivery in brief.action_participant_deliveries
        if (
            delivery.action_id in bound_action_ids
            and delivery.is_perceivable
            and delivery.evidence_ids
        )
    }
    return (
        visible_ids,
        [
            identity_id
            for identity_id in actor_ids
            if (
                identity_id not in visible_ids
                and identity_id in contracted_offscreen_ids
            )
        ],
        [
            identity_id
            for identity_id in target_ids
            if (
                identity_id not in visible_ids
                and identity_id in contracted_offscreen_ids
            )
        ],
    )


def _scene_pack_source_excerpt(
    brief: StoryboardOutlineShot,
    screenplay: EpisodeScreenplay,
    source_text: str,
    *,
    fallback: str,
) -> str:
    candidates: list[str] = []
    catalog = key_line_catalog(screenplay)
    key_lines = {
        _parse_key_line(catalog.get(str(key_id).strip().upper(), ""))[1]
        for key_id in brief.key_line_ids
    }
    for chain in screenplay.dialogue_chains:
        for turn in chain.turns:
            if str(turn.line or "").strip() in key_lines:
                candidates.append(str(turn.source_text or ""))

    normalized_event_ids = {
        re.sub(r"[^A-Za-z0-9]", "", str(value or "")).lower()
        for value in [brief.story_event_id, *brief.event_ids]
        if str(value or "").strip()
    }
    for event in screenplay.events:
        event_key = re.sub(
            r"[^A-Za-z0-9]",
            "",
            str(event.event_id or ""),
        ).lower()
        if event_key in normalized_event_ids:
            candidates.append(str(event.source_span or ""))

    plan = screenplay.narrative_plan
    if plan is not None:
        event_by_id = {item.event_id: item for item in plan.events}
        evidence_by_id = {
            item.source_evidence_id: item.verbatim_excerpt
            for item in plan.source_evidence
        }
        proposition_by_id = {
            item.proposition_id: item for item in plan.propositions
        }
        for event_id in brief.event_ids:
            event = event_by_id.get(event_id)
            if event is None:
                continue
            for proposition_id in event.proposition_ids:
                proposition = proposition_by_id.get(proposition_id)
                if proposition is None:
                    continue
                candidates.extend(
                    evidence_by_id.get(evidence_id, "")
                    for evidence_id in proposition.direct_source_evidence_ids
                )
            for decision in plan.adaptation_decisions:
                if event_id not in decision.affected_event_ids:
                    continue
                for proposition_id in decision.source_proposition_ids:
                    proposition = proposition_by_id.get(proposition_id)
                    if proposition is None:
                        continue
                    candidates.extend(
                        evidence_by_id.get(evidence_id, "")
                        for evidence_id in proposition.direct_source_evidence_ids
                    )

    source_segments = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    spine_by_id = {
        str(item.beat_id or "").strip().upper(): item
        for item in (
            screenplay.plot_spine.spine_beats
            if screenplay.plot_spine else []
        )
    }
    for beat_id in brief.spine_beat_ids:
        beat = spine_by_id.get(str(beat_id or "").strip().upper())
        if beat is None:
            continue
        candidates.extend(
            source_segments.get(segment_id, "")
            for segment_id in beat.source_segment_ids
        )
    candidates.extend([
        fallback,
        brief.covers,
        brief.primary_action,
        brief.beat,
    ])
    for candidate in candidates:
        aligned = align_source_excerpt(
            str(candidate or ""),
            source_text,
            min_match_chars=SOURCE_EXCERPT_MIN_CHARS,
        )
        if aligned is not None:
            return aligned.excerpt
    return ""


def _scene_pack_task_fields(
    brief: StoryboardOutlineShot,
    screenplay: EpisodeScreenplay,
) -> tuple[str, str, str]:
    valid_focuses = {
        "context", "action", "emotion", "dialogue", "evidence", "transition",
    }
    focus = str(brief.readability_focus or "").strip()
    if focus not in valid_focuses:
        contribution = brief.shot_contribution
        if brief.key_line_ids:
            focus = "dialogue"
        elif brief.primary_action_id or brief.action_phase_ids:
            focus = "action"
        elif contribution and contribution.character_state_delta_ids:
            focus = "emotion"
        elif contribution and contribution.assimilation_task_ids:
            focus = "context"
        else:
            focus = "evidence"
    contribution = brief.shot_contribution
    if brief.key_line_ids:
        catalog = key_line_catalog(screenplay)
        lines = [
            _parse_key_line(
                catalog.get(str(key_id or "").strip().upper(), "")
            )[1]
            for key_id in brief.key_line_ids
        ]
        delivered = "；".join(line for line in lines if line)
        derived_purpose = (
            "交付剧本关键台词 " + "、".join(brief.key_line_ids)
        )
        derived_change = (
            f"观众听清「{delivered}」"
            if delivered
            else "观众听清关键台词 " + "、".join(brief.key_line_ids)
        )
    elif contribution and contribution.character_state_delta_ids:
        derived_purpose = (
            "呈现角色状态变化 "
            + "、".join(contribution.character_state_delta_ids)
        )
        derived_change = brief.state_out or derived_purpose
    elif contribution and contribution.assimilation_task_ids:
        derived_purpose = (
            "建立观看上下文 "
            + "、".join(contribution.assimilation_task_ids)
        )
        derived_change = brief.state_out or derived_purpose
    else:
        derived_purpose = brief.beat or brief.primary_action or "落实当前镜头的叙事任务"
        derived_change = brief.state_out or brief.covers or brief.beat or "当前镜头任务完成"
    purpose = str(brief.purpose or derived_purpose).strip()
    resulting_change = str(brief.resulting_change or derived_change).strip()
    return purpose, resulting_change, focus


def _normalize_scene_pack_camera(shot: Shot) -> None:
    """Fill invalid camera fields without replacing valid director choices.

    The scene-pack model has the complete shot-local action and composition.
    Focus metadata is useful as a fallback, but it is too coarse to prove that
    an already valid middle shot, tracking move, or other realization is
    wrong.  Semantic disagreements remain evaluator/repair work.
    """
    from app.continuity import dialogue_action_staging_kind

    size_is_valid = shot.shot_size in SHOT_SIZES
    move_is_valid = shot.camera_move in CAMERA_MOVES
    if size_is_valid and move_is_valid:
        return

    focus = str(shot.readability_focus or "")
    if focus == "action":
        if not size_is_valid:
            shot.shot_size = "中景"
        if not move_is_valid:
            shot.camera_move = "跟随"
    elif focus == "emotion":
        if not size_is_valid:
            shot.shot_size = "近景"
        if not move_is_valid:
            shot.camera_move = "固定"
    elif focus == "dialogue":
        staging = dialogue_action_staging_kind(
            shot,
            narrative_authority=bool(shot.event_ids),
        )
        if staging == "spatial":
            if not size_is_valid:
                shot.shot_size = "中景"
            if not move_is_valid:
                shot.camera_move = "跟随"
        else:
            if not size_is_valid:
                shot.shot_size = "近景"
            if not move_is_valid:
                shot.camera_move = "固定"
    else:
        if not size_is_valid:
            shot.shot_size = "中景"
        if not move_is_valid:
            shot.camera_move = "固定"


def normalize_storyboard_direction_fields(
    board: Storyboard,
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[dict[str, Any]]:
    """Fill deterministic director metadata from the approved shot tasks."""
    briefs = {
        int(brief.shot_no): brief
        for brief in outline.shots
    }
    changes: list[dict[str, Any]] = []
    for shot in board.shots:
        brief = briefs.get(int(shot.shot_no))
        if brief is None:
            continue
        before = {
            "action_desc": shot.action_desc,
            "purpose": shot.purpose,
            "resulting_change": shot.resulting_change,
            "readability_focus": shot.readability_focus,
            "camera_angle": shot.camera_angle,
            "camera_motivation": shot.camera_motivation,
            "context_requirement_ids": list(shot.context_requirement_ids or []),
            "shot_size": shot.shot_size,
            "camera_move": shot.camera_move,
        }
        purpose, resulting_change, readability_focus = _scene_pack_task_fields(
            brief,
            screenplay,
        )
        if not str(shot.purpose or "").strip():
            shot.purpose = purpose
        if not str(shot.resulting_change or "").strip():
            shot.resulting_change = resulting_change
        if not str(shot.readability_focus or "").strip():
            shot.readability_focus = readability_focus
        if shot.dialogues and shot.readability_focus != "dialogue":
            shot.readability_focus = "dialogue"
        if not shot.context_requirement_ids and brief.context_requirement_ids:
            shot.context_requirement_ids = list(brief.context_requirement_ids)
            if not brief.readability_focus:
                shot.readability_focus = "context"
        if not str(shot.camera_angle or "").strip():
            shot.camera_angle = str(brief.camera_angle or "平视").strip()
        if len(str(shot.action_desc or "").strip()) < ACTION_DESC_HARD_MIN:
            planned_action = str(brief.primary_action or "").strip()
            if planned_action and len(planned_action) < ACTION_DESC_HARD_MIN:
                planned_action = (
                    planned_action.rstrip("。")
                    + "，并保持原位让动作结果清楚可见"
                )
            if len(planned_action) >= ACTION_DESC_HARD_MIN:
                shot.action_desc = planned_action
        _normalize_scene_pack_camera(shot)
        if not str(shot.camera_motivation or "").strip():
            shot.camera_motivation = str(
                brief.camera_motivation
                or (
                    f"以{shot.shot_size}{shot.camera_angle}配合{shot.camera_move}，"
                    f"清晰呈现{shot.purpose}"
                )
            ).strip()
        outline_fields = {
            "purpose": shot.purpose,
            "resulting_change": shot.resulting_change,
            "readability_focus": shot.readability_focus,
            "camera_size": shot.shot_size,
            "camera_angle": shot.camera_angle,
            "camera_movement": shot.camera_move,
            "camera_motivation": shot.camera_motivation,
        }
        outline_changed_fields: list[str] = []
        for field, value in outline_fields.items():
            if getattr(brief, field) == value:
                continue
            setattr(brief, field, value)
            outline_changed_fields.append(f"outline.{field}")
        after = {
            "action_desc": shot.action_desc,
            "purpose": shot.purpose,
            "resulting_change": shot.resulting_change,
            "readability_focus": shot.readability_focus,
            "camera_angle": shot.camera_angle,
            "camera_motivation": shot.camera_motivation,
            "context_requirement_ids": list(shot.context_requirement_ids or []),
            "shot_size": shot.shot_size,
            "camera_move": shot.camera_move,
        }
        changed_fields = [
            field
            for field in before
            if before[field] != after[field]
        ]
        changed_fields.extend(outline_changed_fields)
        if changed_fields:
            changes.append({
                "shot_no": int(shot.shot_no),
                "fields": changed_fields,
            })
    return changes


def _directed_interaction_risk_tags(
    interaction: DirectedPhysicalInteraction,
) -> list[str]:
    if interaction.kind == "none":
        return []
    tags = ["dialogue_action_staging"]
    if interaction.kind == "person_person_contact":
        tags.append("dialogue_two_shot_required")
    if interaction.phase != "none":
        tags.append(f"contact_phase:{interaction.phase}")
    return tags


def _hydrate_directed_scene_pack(
    draft: DirectedScenePackDraft,
    *,
    outline: StoryboardOutline,
    source_text: str,
    screenplay: EpisodeScreenplay,
    bible: Bible,
) -> StoryboardScenePack:
    briefs = {
        int(item.shot_no): item
        for item in outline.shots
        if item.scene_id == draft.scene_id
    }
    shots: list[Shot] = []
    last_global_shot_no = len(outline.shots)
    for item in draft.shots:
        brief = briefs[int(item.shot_no)]
        purpose, resulting_change, readability_focus = _scene_pack_task_fields(
            brief,
            screenplay,
        )
        dialogues = _scene_pack_dialogues(
            brief,
            screenplay,
            item.dialogue_emotions,
            bible=bible,
        )
        if not dialogues and item.dialogues:
            dialogues = list(item.dialogues)
        characters = _scene_pack_characters(
            brief,
            dialogues,
            bible=bible,
            screenplay=screenplay,
            fallback=list(item.characters),
        )
        source_excerpt = _scene_pack_source_excerpt(
            brief,
            screenplay,
            source_text,
            fallback=item.source_excerpt,
        )
        duration_s = int(
            brief.duration_s
            or item.duration_s
            or config.DEFAULT_VIDEO_DURATION_S
        )
        # The outline camera is a coarse planning suggestion.  The directed
        # scene pack is the later, shot-local realization that has the actual
        # action path and composition in view, so a valid pack decision must
        # not be overwritten by an earlier generic close-up/static default.
        shot_size = (
            item.shot_size
            if item.shot_size in SHOT_SIZES
            else brief.camera_size
        )
        camera_angle = str(
            item.camera_angle or brief.camera_angle
        ).strip()
        camera_move = (
            item.camera_move
            if item.camera_move in CAMERA_MOVES
            else brief.camera_movement
        )
        camera_motivation = str(
            item.camera_motivation or brief.camera_motivation
        ).strip()
        continuity_mode = str(brief.continuity_mode or "").strip()
        transition = (
            "叠化"
            if continuity_mode == "scene_change"
            else "硬切"
        )
        story_event_id = _resolve_legacy_story_event_id(
            brief.story_event_id,
            [
                str(event.event_id or "")
                for event in screenplay.events
                if str(event.event_id or "").strip()
            ],
        )
        shot = Shot(
            shot_no=int(item.shot_no),
            shot_id=brief.shot_id or f"SH{int(item.shot_no):04d}",
            scene_id=brief.scene_id,
            duration_s=duration_s,
            shot_size=shot_size,
            camera_angle=camera_angle,
            camera_move=camera_move,
            camera_motivation=camera_motivation,
            scene_time=brief.scene_time,
            scene_name=brief.scene_name,
            scene_setting=brief.scene_setting,
            characters=characters,
            characters_visible=list(characters),
            action_desc=item.action_desc,
            first_frame_desc=item.first_frame_desc,
            last_frame_desc=item.last_frame_desc,
            source_excerpt=source_excerpt,
            narration="",
            dialogues=dialogues,
            transition=transition,
            story_event_id=story_event_id,
            purpose=purpose,
            spine_beat_ids=list(brief.spine_beat_ids),
            key_line_ids=list(brief.key_line_ids),
            information_ids=list(brief.information_ids),
            new_information_ids=list(brief.information_ids),
            state_in=brief.state_in,
            primary_action=brief.primary_action,
            emotion_beat=brief.emotion_beat,
            state_out=brief.state_out,
            continuity_mode=continuity_mode,
            audio_cast=_canonical_scene_pack_names(
                [*(brief.audio_cast or []), *[
                    dialogue.speaker for dialogue in dialogues
                ]],
                bible=bible,
                screenplay=screenplay,
                usage="voice",
            ),
            risk_tags=_directed_interaction_risk_tags(
                item.physical_interaction
            ),
            required_text=item.required_text,
            spatial_anchor=item.spatial_anchor,
            is_final=int(item.shot_no) == last_global_shot_no,
            context_requirement_ids=list(brief.context_requirement_ids),
            resulting_change=resulting_change,
            readability_focus=readability_focus,
            repeat_of_shot_id=brief.repeat_of_shot_id,
            repeat_gain=brief.repeat_gain,
            prompt_contract_version="director_scene_pack_v2",
        )
        for field in _STORYBOARD_NARRATIVE_AUTHORITY_FIELDS:
            if field in _STORYBOARD_VISUAL_STAGING_FIELDS:
                continue
            if not hasattr(brief, field):
                continue
            setattr(shot, field, deepcopy(getattr(brief, field)))
        (
            shot.visible_entity_ids,
            shot.offscreen_action_actor_ids,
            shot.offscreen_action_target_ids,
        ) = _scene_pack_visual_partition(
            characters,
            brief,
            screenplay,
            bible,
        )
        shot.capacity_budget = deepcopy(brief.capacity_budget)
        if len(_condense(shot.source_excerpt)) < SOURCE_EXCERPT_MIN_CHARS:
            aligned = align_storyboard_source_evidence(
                shot,
                source_text,
                screenplay=screenplay,
            )
            if aligned is not None:
                shot.source_excerpt = aligned.excerpt
        _normalize_scene_pack_camera(shot)
        ensure_audio_timeline(shot, screenplay.voice_bible)
        normalized, _changes = normalize_storyboard_shot_candidate(
            {
                "episode_no": draft.episode_no,
                "shot": shot.model_dump(mode="json"),
            },
            episode_no=draft.episode_no,
            shot_no=int(shot.shot_no),
            preserve_director_camera=True,
            **storyboard_shot_authority_context(
                screenplay,
                brief,
                shots[-1] if shots else None,
                bible=bible,
            ),
        )
        shot = Shot.model_validate(normalized["shot"])
        shots.append(shot)
    normalized_board = Storyboard(
        episode_no=draft.episode_no,
        shots=shots,
    )
    # Duration is graph/program authority in a directed scene pack, so a model
    # repair cannot change it.  Apply the shared deterministic duration policy
    # before validating the pack: shots that genuinely need extra speech or
    # viewing time keep 6~10s with the normal review tag, while a stale 10s
    # allocation for one 5s action is compressed instead of wasting every
    # AgentLoop iteration on an immutable field.
    prefer_default_shot_durations(
        normalized_board,
        narrative_authority=screenplay.narrative_plan is not None,
        narrative_plan=screenplay.narrative_plan,
    )
    normalize_continuity(normalized_board)
    return StoryboardScenePack(
        episode_no=draft.episode_no,
        scene_id=draft.scene_id,
        shots=normalized_board.shots,
    )


def storyboard_planning_bible(
    bible: Bible,
    outline: StoryboardOutline,
) -> Bible:
    """Use the episode's approved scene plan while async assets catch up."""
    planning_bible = bible.model_copy(deep=True)
    known_names = {scene.name for scene in planning_bible.scenes}
    for planned_scene in outline.scene_contexts:
        scene_name = str(planned_scene.scene_name or "").strip()
        if not scene_name or scene_name in known_names:
            continue
        planning_bible.scenes.append(Scene(
            name=scene_name,
            scene_canonical=(
                f"{scene_name}，{planned_scene.scene_time or '本场时间'}，"
                "空间结构、人物站位与光线服从本场剧本上下文，保持统一画风"
            ),
            first_episode=outline.episode_no,
        ))
        known_names.add(scene_name)
    return planning_bible


def _scene_pack_chunk_validation_outline(
    outline: StoryboardOutline,
    scene_context: StoryboardSceneContext,
    briefs: list[StoryboardOutlineShot],
) -> StoryboardOutline:
    """Project whole-scene obligations onto the bounded chunk that owns them.

    Large scenes are generated in several model calls.  Context requirements
    are graph deliveries owned by specific outline shots, normally the first
    shot of the scene.  Revalidating every later chunk against the complete
    scene requirement list makes an already-delivered obligation impossible
    to satisfy and wastes repair calls.  Keep only requirements explicitly
    assigned to this chunk's briefs; whole-outline validation remains the
    authority that proves every scene requirement has an owner.
    """
    chunk_requirement_ids = {
        str(requirement_id or "").strip()
        for brief in briefs
        for requirement_id in brief.context_requirement_ids
        if str(requirement_id or "").strip()
    }
    chunk_context = scene_context.model_copy(update={
        "context_requirements": [
            requirement
            for requirement in scene_context.context_requirements
            if requirement.requirement_id in chunk_requirement_ids
        ],
    })
    return StoryboardOutline(
        episode_no=outline.episode_no,
        shots=briefs,
        scene_contexts=[chunk_context],
    )


async def generate_storyboard_scene_pack(
    episode: dict,
    source_text: str,
    bible: Bible,
    screenplay: EpisodeScreenplay,
    outline: StoryboardOutline,
    scene_context: StoryboardSceneContext,
    *,
    shot_nos: set[int] | None = None,
) -> StoryboardScenePack:
    """Generate one bounded scene chunk; independent chunks may run in parallel."""
    briefs = [
        item for item in outline.shots
        if (
            item.scene_id == scene_context.scene_id
            and (
                shot_nos is None
                or int(item.shot_no) in shot_nos
            )
        )
    ]
    if not briefs:
        raise StageError(
            "场景分镜",
            [f"{scene_context.scene_id} 没有导演规划镜头"],
        )
    planning_bible = storyboard_planning_bible(bible, outline)
    shot_nos = [int(item.shot_no) for item in briefs]
    hints = [
        scene_context.scene_name,
        scene_context.scene_time,
        *[item.beat for item in briefs],
        *[item.covers for item in briefs],
    ]
    screenplay_window = _relevant_text_windows(
        screenplay.full_script_text,
        hints,
        max_chars=8000,
    )
    source_window = _relevant_text_windows(
        source_text,
        hints,
        max_chars=6000,
    )
    creative_briefs = [
        {
            "shot_no": brief.shot_no,
            "shot_id": brief.shot_id,
            "beat": brief.beat,
            "covers": brief.covers,
            "state_in": brief.state_in,
            "primary_action": brief.primary_action,
            "state_out": brief.state_out,
            "characters_visible": _scene_pack_characters(
                brief,
                [],
                bible=planning_bible,
                screenplay=screenplay,
                fallback=list(brief.characters_visible),
            ),
            "visible_entity_ids": brief.visible_entity_ids,
            "action_phase_ids": brief.action_phase_ids,
            "bound_action_actor_ids": _bound_action_relation_ids(
                screenplay,
                brief,
                bible=planning_bible,
            )[0],
            "bound_action_target_ids": _bound_action_relation_ids(
                screenplay,
                brief,
                bible=planning_bible,
            )[1],
            "key_line_ids": brief.key_line_ids,
            "speech_allowed": bool(brief.key_line_ids),
            "program_dialogue_count": len(brief.key_line_ids),
            "duration_s": brief.duration_s,
            "camera_preset": {
                "shot_size": brief.camera_size,
                "camera_angle": brief.camera_angle,
                "camera_move": brief.camera_movement,
                "camera_motivation": brief.camera_motivation,
            },
        }
        for brief in briefs
    ]
    prompt = f"""任务：按导演规划生成 {scene_context.scene_id} 的有界场景分镜块。

本场镜号必须精确为 {shot_nos}，数量不得增删；导演规划已经决定了完整剧情覆盖和拆镜边界。
你只负责每镜的画面动作、生成起点描述、结束状态目标、摄影表达和空间构图。
镜号、叙事 ID、场景、台词、时长、来源证据、连续性边界、信息台账和是否末镜均由程序装配。
characters 是本层唯一需要输出的身份调度字段：它只能从对应任务的 characters_visible 中选择
本镜实际进入构图的焦点身份；不得补入集合外身份。未进入构图的动作参与者由程序按
atomic action 的 actor/target 关系登记为画外身份，不得为了同场在场关系强塞群戏。

场景上下文：
{json.dumps(scene_context.model_dump(mode='json'), ensure_ascii=False, indent=2)}

本场创作任务：
{json.dumps(creative_briefs, ensure_ascii=False, indent=2)}

相关剧本：
{screenplay_window}

本场可引用原文：
{source_window}

人物谱与统一画风：
{planning_bible.model_dump_json()}

导演规则：
1. 每镜只完成规划中的一个连续动作或观看任务，不得改写任务与结果方向。
   action_desc 必须控制在 {ACTION_DESC_TARGET_MIN}~{ACTION_DESC_TARGET_MAX} 字，
   只保留主体、单一动作路径和可见结果。
2. 每镜必须输出景别 shot_size、角度 camera_angle、运动 camera_move，并用 camera_motivation
   解释三者如何服务上下文、动作、情绪、对白、证据或转场。
3. 动作任务用中景/全景/远景配合跟随或横摇，
   完整展示动作路径、主体和作用对象。
4. 情绪任务用近景/特写配合固定或推近，
   让情绪变化可读，不依赖微表情堆砌。
5. 本场第一镜的 first_frame_desc 只按人物谱和场景库建立起点，不生成任何额外剧情图；
   本场后续镜的 first_frame_desc 必须逐字复制上一镜 last_frame_desc，因为实际生成只会使用
   上一条采用视频的真实尾帧。last_frame_desc 只描述本镜视频结束状态目标，不代表生成静态尾帧图。
6. 相邻镜承接人物位置、视线、道具和动作结果；人物不得凭空出现、消失或换装。
7. dialogue_emotions 只按 key_line_id 填情绪；台词文本和说话人由程序从剧本原样注入。
   speech_allowed=false 的镜头必须全程闭口，只写无声动作或反应，禁止出现“开口、说话、嘴唇张开”等口播动作。
8. required_text 仅在本镜确实需要画面精确文字时填写，否则为 null。
9. source_excerpt 通常留空，由程序按 event/spine/台词证据回绑；只有任务证据不足时才逐字复制原文。
10. physical_interaction 必须由你根据本镜 actor/target、temporal phase、首尾姿态和实际画面语义判断，
    不得按动作关键词套模板。双人身体接触使用 person_person_contact，participants 必须同时保留在
    characters，phase 写 approach/established/separated，contact_point 写清双方哪个部位接触，
    contact_point_visible 必须为 true；只有确实无互动时才写 none。

输出 JSON：
{{
  "episode_no": {episode['episode_no']},
  "scene_id": "{scene_context.scene_id}",
  "shots": [{{
    "shot_no": {shot_nos[0]},
    "shot_size": "远景|全景|中景|近景|特写",
    "camera_angle": str,
    "camera_move": "固定|推近|拉远|横摇|跟随",
    "camera_motivation": str,
    "characters": ["从本镜任务允许身份中选择的实际构图角色"],
    "action_desc": str,
    "first_frame_desc": str,
    "last_frame_desc": str,
    "spatial_anchor": str,
    "physical_interaction": {{
      "kind": "none|person_person_contact|person_object_contact|spatial_interaction",
      "participants": ["本镜实际互动且进入构图的角色名"],
      "phase": "none|approach|established|separated",
      "contact_point": str,
      "contact_point_visible": bool
    }},
    "dialogue_emotions": {{"KL01": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定"}},
    "required_text": null,
    "source_excerpt": ""
  }}]
}}"""

    def _validate(draft: DirectedScenePackDraft) -> list[str]:
        errors: list[str] = []
        if draft.episode_no != episode["episode_no"]:
            errors.append(
                f"episode_no={draft.episode_no}，必须等于 {episode['episode_no']}"
            )
        if draft.scene_id != scene_context.scene_id:
            errors.append(
                f"scene_id={draft.scene_id}，必须等于 {scene_context.scene_id}"
            )
        actual_nos = [int(item.shot_no) for item in draft.shots]
        if actual_nos != shot_nos:
            errors.append(f"场景镜号必须精确为 {shot_nos}，当前 {actual_nos}")
            return errors
        for item in draft.shots:
            interaction = item.physical_interaction
            participants = list(dict.fromkeys(interaction.participants))
            if participants != interaction.participants:
                errors.append(
                    f"shot_no={item.shot_no}.physical_interaction.participants 不得重复"
                )
            undeclared = [
                name for name in participants
                if name not in item.characters
            ]
            if undeclared:
                errors.append(
                    f"shot_no={item.shot_no}.physical_interaction 含未入画参与者 "
                    + "、".join(undeclared)
                )
            if interaction.kind == "person_person_contact":
                if len(participants) < 2:
                    errors.append(
                        f"shot_no={item.shot_no} 双人接触必须声明至少两名参与者"
                    )
                if (
                    interaction.phase == "none"
                    or not interaction.contact_point.strip()
                    or not interaction.contact_point_visible
                ):
                    errors.append(
                        f"shot_no={item.shot_no} 双人接触必须给出阶段和画面可见接触点"
                    )
            elif interaction.kind == "none" and (
                participants
                or interaction.phase != "none"
                or interaction.contact_point.strip()
                or interaction.contact_point_visible
            ):
                errors.append(
                    f"shot_no={item.shot_no} 无互动时不得填写参与者、阶段或接触点"
                )
        try:
            pack = _hydrate_directed_scene_pack(
                draft,
                outline=outline,
                source_text=source_text,
                screenplay=screenplay,
                bible=planning_bible,
            )
        except (TypeError, ValueError) as exc:
            return [f"场景包程序化装配失败：{exc}"]
        for shot in pack.shots:
            tag = f"shot_no={shot.shot_no}"
            if shot.duration_s not in config.ALLOWED_DURATIONS:
                errors.append(f"{tag}.duration_s 必须为 5~10 秒整数")
            if shot.shot_size not in SHOT_SIZES:
                errors.append(f"{tag}.shot_size 非法")
            if shot.camera_move not in CAMERA_MOVES:
                errors.append(f"{tag}.camera_move 非法")
            if not shot.camera_angle.strip():
                errors.append(f"{tag}.camera_angle 为空")
            if len(shot.camera_motivation.strip()) < 6:
                errors.append(f"{tag}.camera_motivation 过短")
            if len(shot.action_desc.strip()) < ACTION_DESC_HARD_MIN:
                errors.append(f"{tag}.action_desc 过短")
            if len(shot.source_excerpt.strip()) < SOURCE_EXCERPT_MIN_CHARS:
                errors.append(f"{tag}.source_excerpt 无法从权威来源确定性回绑")
        if not errors:
            temporary = Storyboard(
                episode_no=episode["episode_no"],
                shots=[
                    shot.model_copy(update={"shot_no": index})
                    for index, shot in enumerate(pack.shots, start=1)
                ],
            )
            errors.extend(validate_storyboard(
                temporary,
                planning_bible,
                int(episode.get("target_duration_s") or 0),
                narrative_authority=screenplay.narrative_plan is not None,
                narrative_plan=screenplay.narrative_plan,
                screenplay=screenplay,
            ))
            errors.extend(validate_storyboard_direction_contract(
                Storyboard(
                    episode_no=episode["episode_no"],
                    shots=pack.shots,
                ),
                _scene_pack_chunk_validation_outline(
                    outline,
                    scene_context,
                    briefs,
                ),
            ))
            errors.extend(validate_storyboard_visual_identity_contract(
                Storyboard(
                    episode_no=episode["episode_no"],
                    shots=pack.shots,
                ),
                outline,
                planning_bible,
                screenplay,
                episode_id=str(
                    episode.get("id") or episode["episode_no"]
                ),
            ))
        return list(dict.fromkeys(errors))

    loop = AgentLoop(
        stage_key=f"storyboard_scene_{scene_context.scene_no}",
        contract_key="storyboard",
        goal=f"生成 {scene_context.scene_id} 完整场景分镜包",
        scope_type="storyboard_scene",
        scope_id=f"{episode.get('id') or episode['episode_no']}:{scene_context.scene_id}",
        artifact_type="storyboard_scene_pack",
        policy=AgentLoopPolicy(
            max_iterations=3,
            stall_rounds=3,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=False,
            repair_all_blockers=True,
        ),
    )
    draft = await _run_with_agent_loop(
        "场景分镜",
        "storyboard_scene_pack",
        prompt,
        DirectedScenePackDraft,
        _validate,
        loop=loop,
        temperature=0.7,
        max_tokens=config.STORYBOARD_OUTLINE_MAX_TOKENS,
        repair_user_prompt_limit=None,
        repair_candidate_limit=None,
        prefill={
            "episode_no": episode["episode_no"],
            "scene_id": scene_context.scene_id,
        },
    )
    pack = _hydrate_directed_scene_pack(
        draft,
        outline=outline,
        source_text=source_text,
        screenplay=screenplay,
        bible=planning_bible,
    )
    artifact_id = getattr(draft, "evidence_artifact_id", None)
    for shot in pack.shots:
        object.__setattr__(shot, "evidence_artifact_id", artifact_id)
    return pack


def _split_text_by_content_budget(text: str, budget: int) -> list[str]:
    """按 ``_condense`` 的计数口径切文本，并保留原有字符顺序。"""
    if budget <= 0:
        raise ValueError("budget must be positive")
    chunks: list[str] = []
    buffer: list[str] = []
    content_chars = 0
    for char in text.strip():
        char_cost = len(_condense(char))
        if buffer and char_cost and content_chars + char_cost > budget:
            chunk = "".join(buffer).strip()
            if chunk:
                chunks.append(chunk)
            buffer = []
            content_chars = 0
        buffer.append(char)
        content_chars += char_cost
    chunk = "".join(buffer).strip()
    if chunk:
        chunks.append(chunk)
    return chunks


def _split_atoms_to_content_budget(atoms: list[str], budget: int) -> list[str]:
    """尽量沿句读贪心分组；单个长句仍超限时再确定性硬切。"""
    pieces = [
        piece
        for atom in atoms
        for piece in _split_text_by_content_budget(atom, budget)
    ]
    groups: list[list[str]] = []
    current: list[str] = []
    for piece in pieces:
        candidate = "；".join([*current, piece])
        if current and len(_condense(candidate)) > budget:
            groups.append(current)
            current = [piece]
        else:
            current.append(piece)
    if current:
        groups.append(current)

    # 贪心装箱可能留下极短尾段（真实镜12曾得到 26/4）。在不越预算且不改变
    # 原顺序的前提下，把边界处的完整语义原子后移，使相邻镜头更均衡、更可拍。
    changed = True
    while changed:
        changed = False
        for index in range(len(groups) - 1):
            left, right = groups[index], groups[index + 1]
            if len(left) <= 1:
                continue
            before = abs(
                len(_condense("；".join(left))) - len(_condense("；".join(right)))
            )
            shifted_left = left[:-1]
            shifted_right = [left[-1], *right]
            right_cost = len(_condense("；".join(shifted_right)))
            after = abs(
                len(_condense("；".join(shifted_left))) - right_cost
            )
            if right_cost <= budget and after < before:
                groups[index] = shifted_left
                groups[index + 1] = shifted_right
                changed = True
    return ["；".join(group) for group in groups]


async def generate_storyboard_next_shot(episode: dict, source_text: str, bible: Bible,
                                        prev_ending: str, screenplay: EpisodeScreenplay,
                                        completed_shots: list[Shot],
                                        final_feedback: list[str] | None = None,
                                        outline: StoryboardOutline | None = None,
                                        repair_feedback: list[str] | None = None,
                                        semantic_attempt_id: str | None = None) -> StoryboardShotDraft:
    """基于已通过镜头生成下一个镜头；业务校验通过才返回，调用方可立即落库给前端增量展示。"""
    if not (screenplay.full_script_text or "").strip():
        raise StageError("分镜脚本", ["旧版拍卡剧本已下线，请先重新生成完整剧本，再进入分镜台"])

    speech_styles = "；".join(f"{c.name}：{c.speech_style}" for c in bible.characters if c.speech_style)
    durations = sorted(config.ALLOWED_DURATIONS)
    narrative_authority = screenplay.narrative_plan is not None
    extra_policy = (
        "本镜任何身份必须引用人物谱或叙事权威图/voice_bible 中由当前来源与戏剧职责定义的唯一身份；"
        "不得从固定功能角色名单选择或根据外表描述自创身份"
        if narrative_authority
        else functional_extra_policy_text()
    )
    output_contract = _storyboard_output_contract(
        episode,
        bible,
        durations,
        speech_styles,
        narrative_authority=narrative_authority,
    )
    preflight_contract = _storyboard_preflight_contract(
        episode,
        narrative_authority=narrative_authority,
    )
    transition_options = "|".join(sorted(TRANSITIONS))
    key_content_block = _storyboard_key_content_block(screenplay)
    narrative_shot_contract = _storyboard_narrative_contract_block(
        include_outline_windows=False,
    )
    scene_library_block = _scene_library_block(bible, screenplay)
    min_shots, max_shots = storyboard_shot_count_range(episode["target_duration_s"])
    shot_no = len(completed_shots) + 1
    # VAL-422：关键台词字数超单镜容量时，在逐镜生成前拆镜并重排 shot_no。
    if outline is not None and not narrative_authority:
        from app.validators import (
            split_outline_over_action_capacity,
            split_outline_on_speaker_changes,
            split_outline_over_key_line_capacity,
        )
        for ev in split_outline_over_action_capacity(
            outline, max_shots=max_shots, shot_nos={shot_no},
        ):
            log_provider_call(
                "storyboard_outline_action_split", config.MODEL_TEXT, "ACTION_CAPACITY_SPLIT", None, 0,
                meta={"episode_id": episode.get("id"), "shot_no": shot_no,
                      "phase": "pre_shot", **ev},
            )
        for ev in split_outline_on_speaker_changes(
            outline, screenplay, max_shots=max_shots,
        ):
            log_provider_call(
                "storyboard_outline_speaker_split", config.MODEL_TEXT, "DIALOGUE_TURN_SPLIT", None, 0,
                meta={"episode_id": episode.get("id"), "shot_no": shot_no, "phase": "pre_shot", **ev},
            )
        for ev in split_outline_over_key_line_capacity(outline, screenplay, max_shots=max_shots):
            log_provider_call(
                "storyboard_outline_capacity_split", config.MODEL_TEXT, "KEY_LINE_CAPACITY_SPLIT", None, 0,
                meta={"episode_id": episode.get("id"), "shot_no": shot_no, "phase": "pre_shot", **ev},
            )
    if outline is not None and narrative_authority:
        from app.narrative_outline import (
            normalize_split_action_owner_completions,
        )

        completion_changes = normalize_split_action_owner_completions(
            outline,
            screenplay,
        )
        if completion_changes:
            log_provider_call(
                "storyboard_outline_action_completion_projection",
                config.MODEL_TEXT,
                "NORMALIZED",
                None,
                0,
                meta={
                    "episode_id": episode.get("id"),
                    "shot_no": shot_no,
                    "phase": "pre_shot",
                    "changes": completion_changes,
                },
            )
    # 有大纲时由计划的镜头数决定收尾时机（执行完整份大纲，避免提前收尾把后段剧情挤掉）；
    # 无大纲时回退到基础镜头数下限。
    expected_total = len(outline.shots) if (outline and outline.shots) else min_shots
    allow_finish = shot_no >= max(min_shots if not (outline and outline.shots) else expected_total, 1)
    # P0：到达当前大纲末镜（或技术硬上限）必须收束。禁止「计划已跑完仍 is_final=false /
    # 继续补镜」发明大纲外幻觉镜头（生产事故：12 镜通过后冒出无剧情的第 13 镜）。
    must_finish = bool(
        (outline and outline.shots and shot_no >= expected_total)
        or shot_no >= max_shots
    )
    episode_hook = (episode.get("hook") or "").strip()
    episode_cliffhanger = (episode.get("cliffhanger") or "").strip()
    final_shot_rule = (
        f"如果 is_final=true，本镜必须落到本集真实尾钩：{episode_cliffhanger}，用本集已有内容据实呈现，"
        "并且整集必保留关键台词/剧情点都已经在已通过镜头或本镜中体现。"
        if episode_cliffhanger else
        "如果 is_final=true，本镜只收束到剧本/原文已有的真实结束状态，不得发明原文没有的下一集钩子。"
    )
    first_shot_entry_rule = (
        f"第 1 镜必须承接上一集真实结尾：{episode_hook}，用本集原文真实内容尽快呼应/推进，"
        f"不得无视，也不得凭空续写 {episode_hook} 之外的新剧情。"
        if episode_hook else
        "第 1 镜按剧本真实开场自然进入。"
    )
    if narrative_authority:
        first_shot_entry_rule = (
            "严格执行大纲首镜的 event/action/contribution/audience path 与 readability window；"
            "是否需要建场或引导镜已由 assimilation task 与处理预算决定，不得按集数强加。"
        )
        shot_action_capacity_rule = (
            "本镜动作容量只按大纲传入的 action_phase_ids 实际分配阶段及其 "
            "estimated_min_s 计算，不设固定阶段个数阈值；阶段最短时间总和不得超过 duration_s。"
            "capacity_budget.action_phase_s 必须覆盖阶段最短时间，全部观看任务预算总和不得超过 duration_s。"
            "跨镜时只在 splittable_boundaries 声明边界分配阶段；无合法边界则上溯重构任务。"
            "action_desc 只能将本镜已分配阶段可拍化，不得增加新前置、效果或完成条件；"
            "用词、同义改写与题材动作名称均不参与计数。"
        )
        shot_staging_rule = (
            "人物或作用对象的可见性/空间状态变化必须由大纲的阶段起止条件与"
            "planned_state_in/out 解释；不得用固定入画动作词或题材道具模板补写。"
        )
        shot_dialogue_staging_rule = (
            "有台词时，根据本镜 temporal phase、actor/target 同框可观察性与"
            "shot_contribution 的注意任务决定景别、可见角色与是否切分话轮；"
            "不以预设走位、道具操作或肢体互动词表触发。"
        )
    else:
        shot_action_capacity_rule = (
            "5~6s 的 action_desc/primary_action 最多 2 个顺序动作节拍，"
            "7~10s 最多 3 个；入画、转身、穿行、停下、道具操作、结果显现与开口按顺序计数。"
        )
        shot_staging_rule = (
            "characters 里新增人物必须写清如何进入画面；减少人物必须写清离开、遮挡、画外或换场原因。"
        )
        shot_dialogue_staging_rule = (
            "纯台词/表情交付用说话人单人近景/特写；台词同时有走位、离场或道具操作时用中景/全景，"
            "并写 dialogue_action_staging；必须看清接触点的双人互动写 dialogue_two_shot_required。"
        )
    budget_block = _storyboard_progress_block(completed_shots)
    brief = _outline_brief(outline, shot_no)
    key_content_block = _storyboard_key_content_block(screenplay, brief=brief)
    valid_info_ids = {item.info_id for item in screenplay.information_ledger or []}
    outline_block = _render_storyboard_outline(outline, shot_no, valid_info_ids)
    current_info_ids = [
        info_id for info_id in (brief.new_information_ids or [])
        if info_id in valid_info_ids
    ] if brief is not None else []
    ledger_context = ledger_context_for_shot(screenplay, completed_shots, current_info_ids)
    ledger_prompt_context = ledger_context
    if narrative_authority:
        ledger_prompt_context = {
            "delivered_ids": list(ledger_context.get("delivered_ids") or [])[-5:],
            "current_ids": list(ledger_context.get("current_ids") or []),
            "pending_count": len(ledger_context.get("pending_ids") or []),
            "delivered_items": list(ledger_context.get("delivered_items") or [])[-5:],
            "current_items": list(ledger_context.get("current_items") or []),
            "do_not_repeat": list(ledger_context.get("do_not_repeat") or [])[-5:],
        }
    ledger_block = json.dumps(ledger_prompt_context, ensure_ascii=False, indent=2)
    brief_block = ""
    if brief is not None:
        brief_block = (
            f"\n【本镜大纲任务】（第 {shot_no}/{expected_total} 镜，必须落实这一条、不要停留在前面已覆盖的剧情）：\n"
            f"- 推进：{brief.beat}\n"
            + (f"- 状态链：{brief.state_in or '（未填）'} -> {brief.primary_action or brief.beat} -> {brief.state_out or '（未填）'}\n")
            + (f"- continuity_mode：{brief.continuity_mode or '（按规则选择）'}\n")
            + (f"- story_event_id：{brief.story_event_id}\n" if (brief.story_event_id or '').strip() else "")
            + (f"- spine_beat_ids：{', '.join(brief.spine_beat_ids)}\n" if brief.spine_beat_ids else "")
            + (f"- key_line_ids：{', '.join(brief.key_line_ids)}\n" if brief.key_line_ids else "")
            + (f"- 本镜新交付信息ID：{', '.join(current_info_ids)}\n" if current_info_ids else "")
            + (f"- 建议时长：{brief.duration_s}s\n" if brief.duration_s else "")
            + (f"- 本镜允许进入构图的身份：{', '.join(brief.characters_visible)}\n" if brief.characters_visible else "")
            + (f"- 声音演员/声源：{', '.join(brief.audio_cast)}\n" if brief.audio_cast else "")
            + (f"- 落实关键内容：{brief.covers}\n"
               "  这些内容必须明确写进本镜 action_desc 或有效口播（dialogues/audio_timeline）；"
               "只出现在 covers/source_excerpt 里不算完成。\n" if (brief.covers or '').strip() else "")
            + (f"- 计划时间：{brief.scene_time}\n" if (brief.scene_time or '').strip() else "")
            + (f"- 计划场景图：{brief.scene_name or brief.scene_setting}\n"
               if (brief.scene_name or brief.scene_setting or '').strip() else "")
            + (
                "- 叙事任务合同（从大纲原样承接 ID/归属/边界，禁止重新发明）：\n"
                + json.dumps({
                    key: value
                    for key, value in brief.model_dump(mode="json").items()
                    if key in {
                        "shot_id", "scene_id", "event_ids", "primary_action_id",
                        "supporting_action_ids", "action_phase_ids",
                        "visible_entity_ids", "offscreen_action_actor_ids",
                        "offscreen_action_target_ids",
                        "capacity_budget", "shot_contribution",
                        "audience_state_paths", "planned_state_in_fact_ids",
                        "planned_delta_add_fact_ids", "planned_delta_remove_fact_ids",
                        "planned_state_out_fact_ids", "completed_before_action_ids",
                        "completed_before_action_phase_ids",
                        "reserved_future_event_ids", "readability_window_ids",
                        "narrative_boundary_from_previous",
                    }
                }, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
        )
    context_hints = [
        brief.beat if brief is not None else "",
        brief.covers if brief is not None else "",
        episode.get("hook") or "",
        episode.get("cliffhanger") or "",
        *[c.name for c in bible.characters],
    ]
    screenplay_window = _relevant_text_windows(
        screenplay.full_script_text, context_hints, max_chars=3000)
    source_window = _relevant_text_windows(source_text, context_hints, max_chars=2200)
    feedback_block = ""
    if final_feedback:
        # ④ 软剧情点保护：临近收尾时把"未落实必保留内容"升级为最高优先级，并明确剧情点可用
        # action_desc 直接拍出来（不必逐字台词），避免无台词但具有因果或关系增量的剧情被略过。
        head = ("\n【收尾前必须补齐·最高优先级】本集即将收尾，但以下剧本必保留内容仍未落实；"
                "请【本镜或紧接的下一镜】优先把它们拍出来/念出来，未全部落实前不得设 is_final=true：\n"
                if allow_finish else
                "\n【本集仍有未落实的必保留内容】（整集校验：以下内容尚未出现在已通过镜头中）：\n")
        tail = ("\n落实方式：关键台词写进 dialogues（人物开口）；"
                "其余主线事实写进 action_desc 的可见动作，"
                "并说明该动作实际改变的事件、关系或观众状态；只要画面或声轨可感知即算落实，"
                "不要在压缩里整段略过。\n")
        feedback_block = head + "\n".join(f"- {e}" for e in final_feedback[:10]) + tail
    repair_feedback_block = ""
    if repair_feedback:
        repair_feedback_block = (
            "\n【本轮局部修复指令·必须逐条解决】\n"
            "以下是确定性校验器对当前工作副本的真实报错。"
            "只修正与本镜相关的问题，保留大纲事件、原文证据与已正确字段：\n"
            + "\n".join(f"- {message}" for message in repair_feedback[:12])
            + "\n不得原样返回上一个候选；若报错涉及相邻镜头，必须同时参照上一镜的景别与尾状态。\n"
        )

    character_state_block = (
        "\n".join(screenplay.character_state_changes)
        if not narrative_authority else
        (
            f"本镜入态：{brief.state_in or '（未填）'}\n"
            f"本镜动作：{brief.primary_action or brief.beat}\n"
            f"本镜出态：{brief.state_out or '（未填）'}"
            if brief is not None else "（按本镜大纲任务承接）"
        )
    )

    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》按顺序生成【第 {shot_no} 镜】。

你现在处于“逐镜头分镜台”：每次只输出一个镜头。前面已经 QA 通过的镜头不可重写，只能把它们作为上下文，继续往后承接剧情。

已确认完整剧本：
标题：{screenplay.title}
一句话梗概：{screenplay.logline}
剧本格式说明：{screenplay.script_format_note or '场次化台本稿'}
场次结构：
{chr(10).join(
    f"场{scene.scene_no}｜{scene.scene_heading}｜功能：{scene.story_function}｜人物：{'、'.join(scene.characters)}｜摘要：{scene.summary}｜冲突：{scene.conflict or '（无）'}｜转折：{scene.turn or '（无）'}"
    for scene in screenplay.scene_outline
) if screenplay.scene_outline else '（未提供场次结构）'}

与本镜任务相关的剧本节选（仅用于理解本镜任务；其余剧情由场次结构、分镜大纲和台账 ID 承接）：
{screenplay_window}

人物状态变化：
{character_state_block or '（无单列项）'}

情绪曲线：
{screenplay.emotional_curve}

结尾钩子：
{screenplay.ending_hook}

原文依据：
{screenplay.source_basis}

{key_content_block}
{scene_library_block}
{narrative_shot_contract}
辅助结构：
- 开端：{screenplay.opening or '（未单列）'}
- 发展：{screenplay.development or '（未单列）'}
- 冲突：{screenplay.conflict or '（未单列）'}
- 高潮：{screenplay.climax or '（未单列）'}
- 改编方向：{screenplay.adaptation_direction or '（未单列）'}

{outline_block}

已通过镜头（必须作为上下文承接，不得改写）：
{_render_completed_shots_context(completed_shots[-1:])}

信息台账上下文（info_id 仅用于内部引用；创作与防重复必须理解其中的中文 content）：
{ledger_block}
{brief_block}{budget_block}{feedback_block}{repair_feedback_block}
当前镜头约束：
1. 只输出第 {shot_no} 镜，shot.shot_no 必须等于 {shot_no}。
2. 本集镜头数不设上限；当前按大纲推进到第 {shot_no}/{expected_total} 镜。本镜必须落实大纲第 {shot_no} 条并产生独立作用，不得停留、复述或发明大纲外内容。{"只有剧情已完整落到尾钩时才可设置 is_final=true，否则必须继续生成" if allow_finish else "剧情尚未铺到计划收尾，is_final 必须为 false"}。duration_s 默认 {PREFERRED_SHOT_DURATION_S}。
2b. 动作容量必须与视频生成门禁一致：{shot_action_capacity_rule}
2c. shot_id、scene_id、event_ids、primary_action_id、supporting_action_ids、action_phase_ids、action_participant_deliveries、capacity_budget、shot_contribution、audience_state_paths、事实状态差、completed_before_action_ids、completed_before_action_phase_ids、reserved_future_event_ids、readability_window_ids 和 narrative_boundary_from_previous 必须从本镜大纲任务原样承接。visible_entity_ids 是本镜实际进入构图的身份，必须是大纲同字段的子集；characters/characters_visible 必须与该子集一一对应。绑定动作中未进入构图的 actor/target 只有在 action_participant_deliveries 已按 action_id+participant_id 绑定本镜可感知 evidence_ids 时，才可分别填入 offscreen_action_actor_ids/offscreen_action_target_ids；可见与有证据的画外分区必须完整覆盖绑定动作参与者。不得改写动作归属，也不得把无关的同场旁观者强塞进画面。
2d. primary_action_id 可为 null，但 shot_contribution 必须非空；支撑/反应/建立/吸收镜必须明确交付证据、观众状态差、情绪、时空定向或戏剧压力中至少一项，不得借 null 产生无功能空镜。
3. 从第 2 镜开始，必须明确承接上一镜的 state_out/observed_state_out；不要重演上一镜完整 action_desc。同场景时，first_frame_desc 与 state_in 必须逐字继承上一镜 last_frame_desc，因为实际输入是上一条采用视频的真实尾帧；换场时才按人物谱和场景库建立新的起点。反应切、正反打等剪辑语义不改变这条输入规则。
3b. audience_state_paths 必须逐一覆盖 narrative_plan 中的全部 audience_prior；从第 2 镜起，每个先验的本镜 audience_state_in_id 必须精确等于上镜 audience_state_out_target_id。边界合同也必须记录同样的逐先验 handoff。
3c. 第 1 镜 narrative_boundary_from_previous 必须为 null；从第 2 镜起必须连接上一镜与本镜真实 shot_id，列出不变量、允许差量、已完成禁止重演的 action_id，并给出非空 cut_motivation。
4. {final_shot_rule}
5. 如果 is_final=false，本镜结尾要留下清楚的动作/情绪/信息状态，供下一镜继续。
6. 人物与作用对象的连续调度：{shot_staging_rule}
6b. 若本镜有 spoken_dialogue：{shot_dialogue_staging_rule}
7. continuity_mode 必须从 action_continuation / same_scene_cut / reaction_cut / reverse_angle / insert_detail / scene_change 中选择；只在同一人物同一动作跨镜延续时使用 action_continuation，普通同场景切换用 same_scene_cut / reaction_cut / reverse_angle / insert_detail，跨时空用 scene_change。
8. new_information_ids 只能从 current_ids / pending_ids 中选择本镜首次交付的信息，禁止自创英文 snake_case ID；若两栏均为空则输出空数组。do_not_repeat 只能填写 do_not_repeat 栏给出的中文剧情内容，不得填写裸 ID；已交付且不允许强化的信息不得重复讲。
9. 功能性路人合同：{extra_policy}。
10. continuity_state_in/out 是跨镜唯一结构化状态快照。未被本镜 primary_action 明确改变的场景 revision/axis/光线、人物 look/outfit revision、道具 revision/owner/location/form 必须从上一镜原样继承；不确定的字段用空字符串，不得自创 ID。
11. 有精确中文时 required_text.strategy 默认 deterministic_insert，只安排唯一 delivery_owner_shot_no；除非文字必须跟随运动道具，否则禁止选 embedded_prop。其他镜头只保持道具形状/色块，不重复要求模型拼字。

拆分原则：
1. 按完整剧本的因果链继续往后拆，不能跳过中间关键事件，也不能重写已通过镜头已经覆盖的内容。
2. 每条 shot 都要推进剧情，且承接上一条的动作、情绪或信息状态。
3. scene_time 只写时间，scene_name 只写场景库规范名；characters 只写实际出现在画面中、且入画原因已经交代清楚的角色；对白镜头不要把“同场在场人物”误当成“当前画面可见人物”全部塞入。
4. 优先用真实台词+画面动作表达信息；narration 必须为空；禁止内心OS/旁白，无法开口的信息用姿态与表情大方向表达。
5. 每条 shot 都必须能追溯到完整剧本与原文依据，不要空泛扩写。
6. 第 1 镜处理：{first_shot_entry_rule if narrative_authority else ('【本集是第一集】第 1 镜是全片开场建场镜，主任务是交代故事背景（世界观/主角处境/核心设定）为全片铺底，再自然带出本集 hook。' if int(episode.get('episode_no') or 0) == 1 else first_shot_entry_rule)}
7. 最后 1 镜规则：{final_shot_rule}

{output_contract}

{preflight_contract}

本镜相关改编源文本节选（source_excerpt 必须从这里逐字摘录；它是上游改编证据和审计字段，不得写进后续 Seedance 画面提示词，也不得把原文散文当成可直接渲染内容）：
{source_window}

角色圣经：{bible.model_dump_json()}
上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "is_final": bool, "shot": {{"shot_no": {shot_no}, "duration_s": int, "shot_size": "远景|全景|中景|近景|特写", "camera_move": "固定|推近|拉远|横摇|跟随", "scene_time": "直接引用本场 scene_contract 的开放文本", "scene_name": "上方场景库规范名", "characters": ["画面中实际可见且受人物谱或叙事权威图定义的身份"], "characters_visible": ["本镜画面实际可见的已定义身份"], "action_desc": str, "state_in": "同场景逐字继承上一镜 last_frame_desc；换场写新起点", "primary_action": "本镜权威任务的可拍表达", "state_out": "本镜结束后交给下一镜的精确状态", "continuity_mode": "action_continuation|same_scene_cut|reaction_cut|reverse_angle|insert_detail|scene_change", "continuity_state_in": {{"scene": {{"scene_revision_id": str, "time_of_day": str, "lighting_state": str, "axis_id": str, "landmarks": {{"landmark": "screen_side"}}}}, "characters": {{"权威身份ID": {{"look_revision_id": str, "outfit_revision_id": str, "visibility": str, "screen_side": str, "pose": str, "facing": str, "gaze_target": str, "left_hand": str, "right_hand": str}}}}, "props": {{"实体ID": {{"canonical_name": str, "revision_id": str, "owner": str, "location": str, "form": str, "visibility": "required|optional|hidden", "text_state": str, "required": bool}}}}}}, "continuity_state_out": "与 continuity_state_in 同结构，只改写本镜任务真正改变的字段", "story_event_id": "对应 screenplay.events[].event_id（E*）；没有对应事件时必须输出空字符串，禁止输出 null，禁止写 S*", "spine_beat_ids": ["本镜落地的主线节拍 S*，可空"], "key_line_ids": ["本镜说出的关键台词 KL*，可空"], "new_information_ids": ["仅填写 information_ledger 中已有的内部编号"], "do_not_repeat": ["只能填写已交付信息的语义内容，禁止裸 ID"], "risk_tags": ["根据当前 ShotTask 实际导演风险填写"], "audio_cast": ["本镜受权威图/voice_bible 定义的发声身份"], "audio_timeline": [{{"start_s": float, "end_s": float, "type": "spoken_dialogue|offscreen_voice|ambient_sound", "speaker_id": "引用 voice_bible.speaker_id 或 null", "text": str, "lip_sync": bool, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定", "voice_canonical": str}}], "required_text": {{"surface": "当前事件实际定义的可读承载实体", "exact_text": "需要画面准确出现的文字；无则为空", "strategy": "deterministic_insert|audio_only|embedded_prop|none", "delivery_owner_shot_no": int, "appear_start_s": 0.0, "stable_until_s": null, "style": "", "allow_other_text": false, "max_other_text": 0, "font_role": "classical_serif", "reading_priority": "plot_critical"}}, "spatial_anchor": "continuity_state 中未被本镜动作改变的固定环境实体方位", "first_frame_desc": "同场景逐字复制上一镜 last_frame_desc；换场只按人物谱和场景库描述生成起点", "last_frame_desc": "本镜视频结束状态目标，不代表另行生成静态尾帧图", "source_excerpt": "对应本镜头的授权来源逐字摘录，至少 {SOURCE_EXCERPT_MIN_CHARS} 字，仅作审计证据", "narration": "", "dialogues": [{{"speaker": "必须引用本镜 characters/audio_cast 中已定义的身份", "line": str, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定", "delivery": "spoken_dialogue|offscreen_voice"}}], "transition": "{transition_options}"}}}}"""
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    log_provider_call(
        "storyboard_shot_prompt", config.MODEL_TEXT, "PROMPT_READY", None, 0,
        meta={
            "episode_id": episode.get("id"),
            "episode_no": episode.get("episode_no"),
            "shot_no": shot_no,
            "completed_shots": len(completed_shots),
            "expected_total": expected_total,
            "has_outline_brief": brief is not None,
            "source_chapters": episode.get("source_chapters"),
            "source_chars": len(source_text),
            "prompt_chars": len(prompt),
            "source_hash": source_hash,
            "prompt_hash": prompt_hash,
            "contract_version": "renderability_v1",
        })
    repair_output_contract = f"""只输出一个 JSON 根对象，根字段为 episode_no、is_final、shot。
shot 必须是单数对象，shot.shot_no 必须等于 {shot_no}；禁止输出 shots 数组，禁止附带下一镜。
shot.story_event_id 必须是 JSON 字符串；没有对应事件时输出 ""，禁止输出 null。
source_excerpt 内的双引号必须按 JSON 规范转义，或改用中文引号，不能破坏根对象语法。
动作容量必须通过与视频生成相同的硬门禁：{shot_action_capacity_rule}
如果当前内容仍超过最长 {config.VIDEO_DURATION_MAX_S}s 的容量，只压缩本镜到大纲已分配的内容；后续节拍由系统在下一轮逐镜生成。
修复时仍必须完整输出 shot 的叙事任务字段，不得因只修一项错误而丢失 ID、贡献、逐先验路径或镜间边界。
{narrative_shot_contract}"""
    repair_context = f"""当前仅修复第 {shot_no} 镜，不得输出其他镜头。
本镜大纲：{brief.beat if brief is not None else '（按完整大纲继续推进）'}
本镜必落内容：{brief.covers if brief is not None else '（无单列项）'}
角色圣经成员：{'/'.join(c.name for c in bible.characters)}
功能性路人：{extra_policy}。
合法时长：{config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S}s 整数，由模型按动作与口播选择最短可用时长；
口播上限随时长变化（5s={config.max_spoken_chars_for_duration(5)}字，10s={config.max_spoken_chars_for_duration(10)}字）。
动作上限：{shot_action_capacity_rule}
合法景别：{'|'.join(sorted(SHOT_SIZES))}；合法运镜：{'|'.join(sorted(CAMERA_MOVES))}；合法转场：{transition_options}。
上一镜详细承接：{_render_completed_shots_context(completed_shots[-1:])}
本镜大纲叙事任务（必须原样承接）：
{json.dumps(brief.model_dump(mode="json") if brief is not None else {}, ensure_ascii=False, separators=(",", ":"))}
本镜叙事权威图投影：
{_shot_narrative_plan_context(screenplay, brief)}
本镜相关剧本：
{screenplay_window}
本镜可逐字摘录原文：
{source_window}
修复时必须保留最近输出中已经正确的字段，只修正错误清单点名的问题。"""
    shot_loop = AgentLoop(
        stage_key=f"storyboard_shot_{shot_no}",
        contract_key="storyboard",
        goal=f"生成第 {shot_no} 镜并通过逐镜合同，保留已通过 checkpoint",
        scope_type="storyboard_checkpoint",
        scope_id=f"{episode.get('id') or episode['episode_no']}:{shot_no}",
        artifact_type="storyboard_shot",
        policy=AgentLoopPolicy(
            max_iterations=4,
            stall_rounds=4,
            min_quality_gain=0.03,
            no_gain_rounds=4,
            allow_warning_candidate=False,
            repair_all_blockers=True,
            commit_accepted_artifact=semantic_attempt_id is None,
        ),
    )
    draft = await _run_with_agent_loop(
        "分镜脚本", "storyboard", prompt, StoryboardShotDraft,
        lambda d: _validate_storyboard_shot_draft(
            d,
            episode=episode,
            bible=bible,
            screenplay=screenplay,
            completed_shots=completed_shots,
            shot_no=shot_no,
            allow_finish=allow_finish,
            must_finish=must_finish,
            narrative_authority=narrative_authority,
            outline=outline,
            outline_covers=(brief.covers if brief is not None else ""),
            outline_scene_name=((brief.scene_name or brief.scene_setting) if brief is not None else ""),
            outline_narrative_task=brief,
            # 向后承接：大纲排给后续镜头的事实留给后面拍，本镜不因此报漏戏。
            later_planned_covers="".join(
                (s.covers or "") for s in (outline.shots[shot_no:] if (outline and outline.shots) else [])
            ),
            source_text=source_text,
        ),
        loop=shot_loop,
        temperature=0.7,
        max_tokens=config.STORYBOARD_SHOT_MAX_TOKENS,
        repair_context=repair_context,
        repair_output_contract=repair_output_contract,
        prefill={"episode_no": episode["episode_no"]},
        storyboard_candidate_context={
            "episode_id": episode.get("id"),
            "episode_no": episode["episode_no"],
            "shot_no": shot_no,
            "preserve_director_camera": bool(repair_feedback),
            **storyboard_shot_authority_context(
                screenplay,
                brief if narrative_authority else None,
                completed_shots[-1] if completed_shots else None,
                bible=bible,
            ),
        },
        semantic_attempt_id=semantic_attempt_id,
    )
    if brief is not None and not _project_shot_scene_from_outline(
        draft.shot,
        brief,
        bible,
    ):
        raise StageError(
            "分镜场景权威投影",
            [f"第 {shot_no} 镜的批准大纲场景无法投影到本集场景库"],
        )
    if not narrative_authority:
        sync_shot_continuity_fields(draft.shot, completed_shots[-1] if completed_shots else None)
    ensure_audio_timeline(draft.shot, screenplay.voice_bible)
    # 大纲已分配的结构化 ID：模型漏填时从 brief 回填，保证主线台账可跨镜聚合。
    if brief is not None:
        if not draft.shot.spine_beat_ids and brief.spine_beat_ids:
            draft.shot.spine_beat_ids = list(brief.spine_beat_ids)
        if not draft.shot.key_line_ids and brief.key_line_ids:
            draft.shot.key_line_ids = list(brief.key_line_ids)
        if not draft.shot.information_ids and current_info_ids:
            # 大纲可能来自旧版本并携带 INFO_03 等非权威 ID。提示和校验前已经按
            # screenplay.information_ledger 过滤，这里必须复用同一白名单结果，不能
            # 在校验通过后重新注入未经校验的 brief.information_ids。
            draft.shot.information_ids = list(current_info_ids)
            draft.shot.new_information_ids = list(current_info_ids)
    # 防重复约束是给后续创作/视频模型理解的中文语义，不持久化裸内部 ID。
    draft.shot.do_not_repeat = list(ledger_context.get("do_not_repeat") or [])
    normalized_board, identity_errors = _normalized_candidate_board(
        episode["episode_no"], completed_shots, draft.shot, bible,
        episode["target_duration_s"],
        narrative_authority=narrative_authority,
        narrative_plan=screenplay.narrative_plan,
        screenplay=screenplay,
    )
    if identity_errors:
        raise StageError("分镜人物合同", identity_errors)
    draft.shot = normalized_board.shots[-1]
    missing_frames = [
        field
        for field in ("first_frame_desc", "last_frame_desc")
        if not str(getattr(draft.shot, field, "") or "").strip()
    ]
    if missing_frames:
        raise StageError(
            "分镜生产字段合同",
            [
                f"第 {shot_no} 镜缺少分镜生产必填字段："
                + ", ".join(missing_frames)
            ],
        )
    return draft


# ---------- C2. 单集分镜脚本（基于完整剧本拆分） ----------

def _first_shot_rule(
    episode: dict,
    *,
    narrative_authority: bool = False,
) -> str:
    """第 1 镜的写作要求：常规集=直接进 hook；但【第一集第一镜】是全片开场，主要职责是交代故事背景
    （世界观/主角处境/基本设定），为后续剧情铺底，而不是急着推进情节或抛冲突。"""
    hook = (episode.get("hook") or "").strip()
    cliffhanger = (episode.get("cliffhanger") or "").strip()
    ending = (
        f"最后 1 个镜头必须落到本集真实尾钩：{cliffhanger}，用本集已有内容据实呈现"
        if cliffhanger else
        "最后 1 个镜头只收束到剧本/原文已有状态，不得发明下一集钩子"
    )
    if narrative_authority:
        return (
            "23. 第 1 镜严格执行叙事权威合同中的首个合法事件、入场状态、"
            "AudiencePriorContract 与 AssimilationTask；是否需要建场、铺垫、揭示或直接入戏，"
            "由当前观众的认知缺口、事件前置和独立可读窗口决定，不得按集数或题材套用开场模板。\n"
            "    已由入场先验或上游镜头交付的信息不得重演；未具备的关键前置必须在其截止事件前以可感知证据交付。\n"
            f"    {ending}"
        )
    if int(episode.get("episode_no") or 0) == 1:
        return (
            f"23. 【第一集第一镜=全片开场建场镜，特殊规则，优先级最高】这一镜的主要任务是【交代故事背景】，"
            f"不是推进剧情、不是抛冲突反转：用【画面 + 必要真实台词】把【世界观/时代设定/主角是谁、身处什么处境、基本关系或核心设定】"
            f"讲清楚，让没看过原著的观众迅速进入这个故事。\n"
            f"    - action_desc 写一个能代表本片世界观/主角日常处境的【建立性画面】（establishing shot），"
            f"人物动作克制、信息靠画面与必要对白承载；禁止旁白/内心OS；不要在第一镜就让主角做剧烈动作或触发核心冲突。\n"
            f"    - narration 必须为空字符串；shot_size 优先用远景/全景做开场建场，先把环境和主角位置交代清楚。\n"
            "    - 开场需要更长铺陈时，可为单一连续建场动作选择 5~10 秒；超过 10 秒或进入新节拍时拆成相邻建场镜，逐步完成环境建立与人物入场，"
            f"所以 action_desc/首尾帧请按\"远景缓慢推近、镜头从环境推向主角\"来写：首帧是交代环境的大远景，"
            f"尾帧镜头推近到主角、但仍是同一机位的连续推进，人物动作保持克制连贯。\n"
            + (
                f"    - 仍要承接上一集真实结尾：{hook}，以\"先立背景、再据实呼应\"的方式呈现，"
                f"不要为了呼应牺牲掉背景交代，也不得凭空续写 {hook} 之外的新剧情。\n"
                if hook else
                "    - 本集开场无需承接钩子：开场只按原文和剧本真实情境建立世界与主角处境。\n"
            )
            + f"    {ending}")
    opening = (
        f"第 1 个镜头必须承接上一集真实结尾：{hook}，用本集原文真实内容尽快呼应/推进，"
        f"不得无视，也不得凭空续写 {hook} 之外的新剧情"
        if hook else
        "第 1 个镜头按剧本真实开场自然进入"
    )
    return f"23. {opening}\n    {ending}"


def _storyboard_output_contract(
    episode: dict,
    bible: Bible,
    durations: list[int],
    speech_styles: str,
    *,
    narrative_authority: bool = False,
) -> str:
    target = episode["target_duration_s"]
    min_shots, max_shots = storyboard_shot_count_range(target)
    character_names = "、".join(c.name for c in bible.characters) or "（角色圣经为空）"
    duration_options = "/".join(str(value) for value in durations)
    speech_budgets = "、".join(
        f"{value}s≤{config.max_spoken_chars_for_duration(value)}字" for value in durations
    )
    if narrative_authority:
        extra_policy = (
            "非人物谱身份必须由叙事权威图的 actor/character 引用与 "
            "voice_bible 中带来源证据的戏剧职责定义，不得从固定功能角色名单选择"
        )
        action_capacity_contract = (
            "6a. 【硬性·动作容量】只按 ShotTask.action_phase_ids 实际引用的 "
            "AtomicAction.temporal_phases 与 estimated_min_s 计算，capacity_budget.action_phase_s "
            "必须覆盖阶段最短时间、全部观看任务预算总和不得超过 duration_s。若超限，必须保持 "
            "precondition/effects/completion、阶段顺序与状态方程，由 AI 仅在 splittable_boundaries "
            "声明的边界重新分配 ShotTask；没有合法边界则上溯重构。任何动作用词或同义改写都不参与容量判定。"
        )
        staging_contract = (
            "8c. 【导演调度】画面角色、作用对象和注意目标必须服从 ShotTask 的 "
            "actor/target、temporal phase 起止条件与 shot_contribution；跨越可见性或空间边界时，"
            "以 planned_state_in/out 和 continuity boundary 完整解释，不按动作词表套模板。\n"
            "8c-1. 【对白构图】单人、多人、动作对白与话轮切分由当前阶段的可观察性、"
            "actor/target 同框必要性、口播容量与观众注意任务决定；不得依赖预设互动/道具词组触发。"
        )
        reference_contract = (
            "8c-2. 【固定参照连续性】spatial_anchor 引用本镜 continuity_state 中实际声明且"
            "不随主动作改变的环境实体；首尾帧只能发生 allowed state delta，不得增删、复制或换位未改变实体。"
        )
        contact_contract = (
            "8d. 【作用关系可观察性】当 AtomicAction 需要 actor/target 发生空间作用时，"
            "机位、首尾帧与动作描述必须让影响可观察，并与 completion_condition 可核对；不套接触动作词模板。"
        )
        physical_contract = (
            "25. 表演必须符合 AtomicAction 的起止条件、角色/对象能力与世界物理合同；"
            "若原方案不可拍，由 AI 保留语义意图与效果后重写可观察实现，不使用固定手势清单。"
        )
    else:
        extra_policy = functional_extra_policy_text()
        action_capacity_contract = (
            "6a. 【硬性·动作容量】与视频生成前门禁使用同一阈值：5~6s 最多 2 个顺序动作节拍，"
            "7~10s 最多 3 个；超限时在大纲阶段拆成前后相邻两镜。"
        )
        staging_contract = (
            "8c. 【导演调度】无对白的建立镜/动作镜只写实际进入构图的人物；人物进出画需交代大动作。\n"
            "8c-1. 【对白构图偏好】纯台词/表情交付优先使用说话人单人近景或特写；"
            "对白同时承担空间调度时必须完整拍出动作。"
        )
        reference_contract = (
            "8c-2. 【固定参照连续性】若场景圣经或本镜存在不随主动作移动的环境参照物，"
            "spatial_anchor 必须写清其方位，首尾帧保持形态、数量和位置。"
        )
        contact_contract = (
            "8d. 【接触侧面】接触类动作的首尾帧与动作描述按侧面机位书写，写清接触点与相对方位。"
        )
        physical_contract = "25. 动作符合物理；复杂手势改写成更稳的简单动作。"
    return f"""硬性输出规范（以下规则由代码校验，违反会被退回重写；请首轮直接满足）：
1. episode_no 必须等于 {episode['episode_no']}；shots 按剧情顺序排列，shot_no 必须从 1 开始连续递增，不能跳号、重复或乱序。
2. 整集镜头数由完整覆盖 must_keep spine、上下文与主线台词决定，不设数量上限；重复由每镜作用门禁拦截。
3. 复杂动作可拆，但优先删减超纲细节而非无限拆镜；禁止为碎镜而合并删主线。
4. duration_s **默认 {PREFERRED_SHOT_DURATION_S}s**；只能取整数 {duration_options} 秒。
   - 【选择原则】绝大多数镜用 {PREFERRED_SHOT_DURATION_S}s。仅当口播超过 {PREFERRED_SHOT_DURATION_S}s 预算、或同一连续动作确需铺陈时，才取 6~10s；超过 5s 的镜会进入 AI 时长审核。禁止无内容拉长。
   - 【硬性·音画同步】口播预算随 duration_s 增长：{speech_budgets}。选择时长后，动作与口播必须都能在该时长内自然完成。
   - 【硬性·拆镜边界】不同时间、地点、主动作必须拆镜；同 spine 事件通常 1~2 镜封顶。
5. 关键：每条 shot 只表现【一个】连贯主动作（大形体可读）。严禁出现"切到/切至/镜头切/镜头转向/闪回/回忆画面/分屏/下一个镜头/→"。禁止微表情/衣角/眼泪/指节等超纲词。
6. 单镜要像一个真实可拍的连续动作：主体、目标、起始状态和完成条件一致；若时空、主动作或注意任务已发生边界变化，必须在自然阶段边界分镜。
{action_capacity_contract}
7. 声轨纪律（重要）：分镜只保留【真实台词】（dialogues）；禁止旁白、内心OS、画外解说。人群/气氛声写进 action_desc。不能把有对白的剧本压成纯画面卡；是否开口由本镜信息交付与口播容量决定。同一镜最多一个 spoken_dialogue 说话人；问答双方必须按话轮拆成相邻正反打。
8. action_desc 目标 {ACTION_DESC_TARGET_MIN}~{ACTION_DESC_TARGET_MAX} 字：写清主体姓名与这一个大形体主动作；不要罗列多个镜头，不要写运镜术语。
8b. 【关键·生成起点与结束目标】每条 shot 必须给出 first_frame_desc 与 last_frame_desc：
   - 换场首镜的 first_frame_desc 只按人物谱和场景库建立画面，不得要求生成任何额外剧情图。
   - 同场景后续镜的 first_frame_desc 必须逐字复制上一镜 last_frame_desc；实际生成只使用上一条采用视频的真实尾帧作为本镜唯一首帧。
   - last_frame_desc 只描述本镜视频结束状态目标，不生成静态尾帧参考图；约 20~40 字，不要写超纲微细节、字幕、运镜。
{staging_contract}
{reference_contract}
{contact_contract}
8e. 【同框身高】多人物同框默认同身高、眼线齐平；仅当剧情明确需要身高差时才在 action_desc/首尾帧写明（如「高他一头」「孩童仰视」），否则不要写一高一低。
9. source_excerpt 必填：至少 {SOURCE_EXCERPT_MIN_CHARS} 字，可与相邻镜共享同一主线段落；仅作审计，不得进入 Seedance。
10. 字数只校验必要下限；优先保证主线可看，不要为凑数字堆细节。
11. 信息密度靠"一个清晰动作 + 必要台词"；禁止呆立、氛围空镜、重复上一镜。
12. 【硬性·禁旁白】narration 必须为空字符串 ""；禁止内心OS/画外解说/旁白员。无法开口的信息改用画面姿态表达。
12a. 【口播优先】单镜台词纯文字（不计标点）受第 4 条约束（最长 10s 也不得超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字）。环境群像声优先写进 action_desc。
12b. 【声轨时序】同一说话人的连续短句可按剧情顺序放在同镜；说话人一变化就必须切到下一镜，勿内容重复撞车。
13. 角色名必须准确：characters 不能为空；有姓名角色只能使用角色圣经准确姓名：{character_names}。{extra_policy}。
14. action_desc 必须显式写出本镜头主要角色的准确姓名。
15. dialogues 只写人物实际开口台词，dialogues[*].speaker 必须在本镜头 characters 中；同镜所有 spoken_dialogue 必须属于同一 speaker。
16. 【单镜】台词纯文字口播必须满足第 4 条上限。emotion 只能取：{'|'.join(sorted(EMOTIONS))}。说话风格：{speech_styles or '（无额外说话风格）'}。
17. scene_time 独立写时间段或具体时刻；scene_name 只写场景库规范名，与场景图一一对应。
18. shot_size 只能取：{'|'.join(sorted(SHOT_SIZES))}；camera_move 只能取：{'|'.join(sorted(CAMERA_MOVES))}；transition 只能取：{'|'.join(sorted(TRANSITIONS))}。
19. 同一 scene_name 的镜头尽量连续排列；时间变化只改 scene_time，不得改写 scene_name 来伪造新场景。
20. shot_size 由当前动作、人物调度和情绪表达决定；剧情需要时允许连续镜头使用相同景别，禁止仅为形式变化牺牲可拍性。
21. 相邻镜头用 continuity_mode 表达剪辑语义；action_continuation 仅用于同一人物同一动作跨镜延续，但所有同场景模式都继承上一条采用视频的真实尾帧。
22. 转场设计：同场景连续镜只能用"硬切"；换场不得硬切。
{_first_shot_rule(episode, narrative_authority=narrative_authority)}
24. 特效服从剧情，日常对话写实克制。
{physical_contract}"""


def _storyboard_preflight_contract(
    episode: dict,
    *,
    narrative_authority: bool = False,
) -> str:
    target = episode["target_duration_s"]
    min_shots, max_shots = storyboard_shot_count_range(target)
    if narrative_authority:
        return f"""首轮输出前必须逐镜预检（叙事权威路径）：
1. 镜头数由完整交付因果图、target delta 与 assimilation task 决定，不设数量上限。
2. 动作容量只读取 ShotTask.action_phase_ids 实际分配的 AtomicAction phases；不设固定阶段个数阈值，capacity_budget.action_phase_s 不得低于这些阶段的 estimated_min_s 总和、全部观看任务预算总和不得超过 duration_s。动作用词、同义词与题材词不参与判定。
3. 动作首阶段镜的 precondition_fact_ids 必须在 planned_state_in；末阶段镜的 effects_add/remove 必须在本镜状态差；completion_condition 必须在末阶段镜的 last_frame/observed_state_out 可核对。中间阶段不得冒充完整动作结果。
4. 若超容，由 AI 在 AtomicAction.splittable_boundaries 声明的边界提出新的相邻 ShotTask 阶段分配；必须保持事件拓扑、状态方程、action owner、阶段顺序、观众路径、deadline 与可读窗口。无合法边界时上溯重构动作与任务，禁止文本分隔器自动拆镜。
5. 第一镜 boundary 必须为 null；后续镜头必须精确连接相邻 shot_id，传递状态不变量、允许差量、已完成动作与每个 audience prior 的状态。
6. 画面角色、声源与参考身份只能引用叙事图/voice_bible 中由当前来源和戏剧职责定义的身份，不得从固定功能角色名单选择。
7. 换场首镜只以人物谱和场景库建立生成起点；同场景镜头的 first_frame_desc 必须逐字继承上一镜 last_frame_desc，对应上一条采用视频的真实尾帧。last_frame_desc 只定义结束状态目标，不生成静态尾帧图。
8. 景别、可见角色、作用对象与对白切分必须让当前 temporal phase、evidence 和注意任务可观察；不依赖预设互动/道具词组触发。
9. 声轨按本镜信息交付与口播容量决定；同镜对白语义、dialogues 和 audio_timeline 必须一致。
10. source_excerpt 必须是当前授权来源的可追溯证据，不得进入视频提示词。
11. 在输出前对本镜执行状态方程、阶段时间、动作防重演、观众状态交接与窗口截止时间的联合校验。"""
    return f"""首轮输出前必须逐镜预检（这些就是代码校验器的具体判定条件，不要等返工）：
1. 镜头数由完整覆盖剧情与上下文决定，不设数量上限；每条 duration_s **默认 {PREFERRED_SHOT_DURATION_S}s**，仅当口播或连续动作需要时取到 {config.VIDEO_DURATION_MAX_S}s。动作容量与视频门禁一致：5~6s≤2 个顺序节拍，7~10s≤3 个；超限在自然动作边界拆镜，每镜必须有独立作用。
2. 第 1 镜 continuity_mode 不得为 action_continuation；第 2 镜开始逐条和上一镜比较 state_out、scene_name、scene_time 与角色可见状态。
3. 如果本镜 scene_name 与 scene_time 均与上一镜相同：
   - continuity_mode 必须是 same_scene_cut / reaction_cut / reverse_angle / insert_detail / action_continuation 之一；
   - 只有同一人物同一动作跨镜延续时才能使用 action_continuation，且 state_in 必须承接上一镜 state_out/observed_state_out；
   - 无论采用上述哪种同场景模式，first_frame_desc 与 state_in 都必须逐字继承上一镜 last_frame_desc；
   - transition 必须为"硬切"；
   - characters 至少保留上一镜的 1 个核心人物；
   - action_desc/state_in 必须承接上一镜结尾的人物位置、道具/屏幕内容、动作或情绪，不能重新介绍场景或重复上一镜发现；
   - 如果本镜 characters 比上一镜多了某个角色，必须写清他/她“走进、上前、转身露出、从人群中出来、被带入”等入画过程；如果少了某个角色，必须写清他/她退开、离开、被遮挡、留在画外或换到另一人反应，禁止凭空出现/消失。
4. 如果本镜 scene_name 或 scene_time 与上一镜不同：
   - continuity_mode 必须为 scene_change；
   - transition 必须选择明确的换场方式，绝不能用"硬切"；普通时空跳转优先"淡出淡入"，情绪/回忆延续优先"声音延续+叠化"，悬疑冲击用"闪黑/闪白"，动作追逐用"甩镜/遮挡转场"，有构图呼应时用"匹配剪辑"；
   - scene_contract、state_in 与 boundary 必须显式引用换场后的时间、地点和状态来源；自然语言用词不参与通过判定；
   - 转场由最终编辑执行；上一镜 last_frame_desc 必须保留稳定、干净的动作结果，本镜 first_frame_desc 是新时间/新地点的建立画面；两侧都不预烧叠化/闪黑/闪白效果；
   - 如果只是同一段连续动作里从房间走到门口/楼道/桌边/窗前，不要改 scene_name，把移动写进 action_desc。
5. scene_name 是稳定的场景图身份，必须沿用库内规范名；scene_time 是独立时间维度。不要把楼道外/桌前/门口等镜头内容改写成新的 scene_name。
6. characters 只写本镜头实际可见/在场且已交代入画原因的人；允许第 13 条定义的功能性路人，具体姓名仍必须来自角色圣经。屏幕发信人、纸条落款、新闻里提到的人、AI 软件名不算 characters。
7. 每条 action_desc 必须显式落实本镜 primary_action_id/action_phase_ids，并让 characters 中承担动作的权威身份可见；是否超容只看 capacity_budget 与动作阶段时长，不扫描文案词汇。
8. 每条 shot 的 source_excerpt 必填（≥{SOURCE_EXCERPT_MIN_CHARS} 字），可与相邻镜共享主线段落；仅作审计，不得进入 Seedance。
9. 声轨预检：若完整剧本对应段落有“角色名：台词”且本镜负责交付该信息，必须写 dialogues；内心独白禁止写进 narration（narration 必须为空），改用画面姿态表达；人群嘲讽/恭维写进 action_desc。是否发声服从本镜信息交付与口播容量，禁止为比例凑对白。
9b. 对白构图预检：统计 spoken_dialogue 的唯一说话人。超过 1 人必须按话轮拆镜。正好 1 人且只有台词/表情交付时，characters/characters_visible 只含说话人，shot_size=近景或特写，camera_move=固定或推近；若台词同时包含走位、离场或剧情道具操作，改用中景/全景并写 dialogue_action_staging，完整保留动作路径；双人接触动作写 dialogue_two_shot_required。
10. first_frame_desc 表示实际生成起点：同场景逐字继承上一镜结束目标，换场只按人物谱与场景库建立；last_frame_desc 表示本镜结束目标，不是待生成的静态图片。
10b. spatial_anchor 必须写清当前构图内固定地标/大型道具的位置；同一视频从真实输入首帧到结束状态中，同一石碑、门、桌台或屏幕不得消失、复制、变形或换位。
11. 人物调度预检：逐条核对上一镜 last_frame_desc、本镜 first_frame_desc、characters、action_desc。任何角色的入画、出画、开口、转身、靠近、退后都必须有可见动作链；如果一句话解释不清，就拆成相邻两镜，不要让视频模型自行脑补。

常见错误 → 正确写法（以下角色A/场景A仅为占位示例，请替换成本集真实角色与场景）：
- 错：上一镜 scene_name="场景A"，本镜改成"场景A楼道外"。对：若角色A只是从房内走到门口，scene_name 仍写库内"场景A"，scene_time 独立保持，移动写进 action_desc。
- 错：纸条上出现一个落款名就把 characters 写成 ["该落款名"]。对：如果画面只拍到角色A和纸条，characters 写 ["角色A"]，纸条文字放 action_desc。
- 错：下一镜重新说"场景A昏暗、桌上有电脑"。对：下一镜直接从上一镜结尾继续，写"角色A仍盯着刚弹出的新闻推送，手指停在屏幕上，随后抬头望向门口，最后攥紧纸页。"。"""


def _score_or_none(value) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    if 1 < score <= 100:
        score /= 100
    return max(0.0, min(1.0, score))


def _extract_score_from_text(raw: str, key: str) -> float | None:
    key_pat = re.escape(key)
    number = r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))"
    patterns = (
        rf'["`]?{key_pat}["`]?\s*[:：]\s*{number}',
        rf'\b{key_pat}\b[\s\S]{{0,240}}?(?:score|评分|分数)\s*[:：]?\s*{number}',
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            score = _score_or_none(match.group(1))
            if score is not None:
                return score
    return None


def _normalize_issues(value, fallback: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str) and value.strip():
        items = [value.strip()]
    else:
        items = []
    if not items and fallback:
        items = fallback
    return items[:8]


def _bool_or_default(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_qa_object(obj: dict, score_keys: list[str], *, raw: str = "",
                         defaults: dict[str, float] | None = None,
                         recovered: bool = False) -> dict:
    defaults = defaults or {}
    # Keep the evaluator's observation fields in addition to the normalized
    # scores.  Scene/portrait policies rely on facts such as person_count and
    # watermark_detected; dropping them here turns a complete response into an
    # artificial "unverified" result.
    out: dict[str, object] = dict(obj)
    known_scores: list[float] = []
    incomplete = False
    for key in score_keys:
        score = _score_or_none(obj.get(key))
        if score is None:
            score = defaults.get(key)
        if score is None and raw:
            score = _extract_score_from_text(raw, key)
        if score is None:
            incomplete = True
            score = 0.0
        out[key] = score
        known_scores.append(score)
    overall = _score_or_none(obj.get("overall"))
    if overall is None:
        overall = defaults.get("overall")
    if overall is None:
        overall = round(sum(known_scores) / len(known_scores), 3) if known_scores else 0.0
    out["overall"] = max(0.0, min(1.0, overall))
    fallback_issues = (
        ["VLM返回非标准结构，未获得可验证的结构化诊断"]
        if recovered or incomplete else []
    )
    out["issues"] = _normalize_issues(obj.get("issues"), fallback_issues)
    out["observed_state_out"] = str(obj.get("observed_state_out") or "").strip()
    for key in ("no_story_repeat", "no_future_leak", "no_character_duplicate", "whole_clip_usable"):
        out[key] = _bool_or_default(obj.get(key), True)
    contract_facts = [
        f"{key}_below_contract"
        for key in score_keys
        if float(out.get(key) or 0.0) < 0.45
    ]
    contract_facts.extend(
        f"{key}_failed"
        for key in (
            "no_story_repeat",
            "no_future_leak",
            "no_character_duplicate",
        )
        if out.get(key) is False
    )
    out["contract_facts"] = list(dict.fromkeys(contract_facts))
    out["runtime_blocking"] = out.get("whole_clip_usable") is False
    out["blocking_facts"] = (
        ["whole_clip_contract_failed"] if out["runtime_blocking"] else []
    )
    # 供自动重抽判断“这是资产质量失败，还是 QA 响应格式失败”。
    # 非标准输出可以展示恢复分，但不应据此花钱重生视频。
    out["qa_recovered"] = recovered or incomplete
    return out


def _parse_qa_result(raw: str, score_keys: list[str], *,
                     defaults: dict[str, float] | None = None) -> dict:
    try:
        obj = extract_json(raw)
        return _normalize_qa_object(obj, score_keys, raw=raw, defaults=defaults)
    except ValueError:
        recovered = {key: _extract_score_from_text(raw, key) for key in score_keys}
        recovered = {key: value for key, value in recovered.items() if value is not None}
        return _normalize_qa_object(recovered, score_keys, raw=raw, defaults=defaults, recovered=True)


# ---------- E. VLM 质检 ----------

async def review_scene_image(image_b64: str, frame_desc: str, scene_setting: str,
                             character_anchors: list[str], prev_image_b64: str | None = None,
                             kind: str = "tail", initiator_label: str = "关键帧评审",
                             environment_only: bool = False) -> dict:
    """场景关键帧评审 agent：只对照【本帧自己的画面描述】（首图描述 / 尾图描述）检查该单张静止帧，
    不要拿整段动作或后续画面来要求它。返回 {expectation_match, continuity, clean_frame, overall, issues}。"""
    if environment_only:
        anchors = "（纯环境定场图：明确要求画面中无人；没有人物是正确结果，不得因此扣分）"
        subject_note = (
            "\n本次审核对象是跨镜头复用的纯环境定场图，不是剧情关键帧。"
            "画面必须无人；不要要求角色、人物动作、姿态、表情或互动。"
        )
        expectation_focus = "场景名称、空间类型、建筑结构、陈设、光照、画风与构图是否对得上"
        main_rule = (
            "- 这是纯环境图：画面无人是合格要求，绝不能因没有人物、角色锚点、动作或互动而扣分。\n"
            "- expectation_match 的主项是场景语义与环境细节；场景名称和预期描述中的明确要求都要核对。\n"
            "- space_type_matches 只判断室内外和地点大类；layout_detail_matches 单独判断布局、陈设与结构细节。\n"
            "- material_contract_matches 单独判断画面材质是否满足预期描述。三个字段必须根据各自语义独立填写。"
        )
    else:
        anchors = "\n".join(character_anchors) or "（缺少角色锚点）"
        subject_note = ""
        expectation_focus = (
            "人物姿态/表情/手部与道具的接触状态、人物的身体与视线朝向、"
            "人物与对象（道具或另一人）之间的空间互动关系，以及角色外观、场景是否对得上"
        )
        main_rule = (
            "- expectation_match 是本次评审的【主项】。若预期画面里人物在与某对象/另一人互动"
            "（触碰/按压/拿取/递出/挥击/指向/注视/搀扶等），而画面中人物只是正面端站、"
            "主体姿态、朝向或 actor/target 作用关系与当前 AtomicAction 的"
            "起止条件不符时，expectation_match 必须 ≤0.4。"
        )
    from app.multiview import watermark_qa_mode
    ignore_non_occluding_watermark = watermark_qa_mode() == "ignore_unless_occluding"
    watermark_note = (
        "供应商自动添加且位于角落、不遮挡场景主体的水印/Logo 不扣分，也不要写入 issues；"
        "只有遮挡关键场景结构时才扣分并说明遮挡位置。"
        if ignore_non_occluding_watermark
        else "画面不得包含任何位置、透明度的文字、水印或 Logo；检出即为入库硬失败。"
    )
    frame_name = "首图（本镜动作开始前的静止画面）" if kind == "head" else "尾图（本镜动作完成后的静止画面）"
    cont = ("\n本关键帧需与第2张参考图在画风、人物形象、光影上自然连贯（第2张可能是本镜首图或上一镜尾图）。"
            if prev_image_b64 else "\n本关键帧是新场景起点，无需对比上一镜。")
    expectation = f"""你是漫剧场景关键帧评审 agent。下面给出本镜{frame_name}{('（第1张）以及参考图（第2张，仅作连贯性对比）' if prev_image_b64 else '')}，对照下面这【单张静止帧】的预期检查，输出 JSON。

重要：只审这一张静止帧是否符合它自己的画面描述；不要因为它没有表现整段动作的过程或后续/结尾画面而扣分（动作的展开由视频负责，关键帧只是这一刻的定格）。但【这一刻的人物姿态、朝向与互动】必须与描述一致——定格不等于可以摆拍。{subject_note}

本帧预期画面：{frame_desc}
预期场景：{scene_setting}
预期角色外观：
{anchors}{cont}

检查项（各 0~1 评分）：
1. expectation_match  画面是否符合【本帧预期画面】，重点核对：{expectation_focus}
2. continuity         与参考图的画风、人物形象、光影是否连贯（无参考图则给 1）
3. clean_frame        无多余文字/多余人物/肢体畸形/五官崩坏。{watermark_note}

纯环境资产还必须输出可机器判断的观察事实；不能确认时使用 null，禁止猜测：
- person_count: 识别到的人物数量（int 或 null）
- watermark_detected: 是否检出任意水印/Logo（bool 或 null）
- watermark_occluding: 水印/Logo 是否遮挡主体或关键场景结构（bool 或 null）
- forbidden_text_detected: 是否检出不属于场景合理陈设的字幕、角标、随机字形或叠字（bool 或 null）
- forbidden_text_is_provider_mark: 检出的多余文字是否全部来自同一供应商标识（bool 或 null）
- space_type_matches: 室内外及空间类型是否符合预期（bool 或 null）
- layout_detail_matches: 布局、陈设与结构细节是否符合预期（bool 或 null）
- material_contract_matches: 材质是否符合预期描述（bool 或 null）

评分硬规则（务必遵守）：
{main_rule}
- overall 不得高于 expectation_match：动作/朝向/互动不对就是不合格，画面再干净、画风再连贯也不能给高 overall。
- issues 里必须逐条点明当前主体、作用对象、空间关系、起止条件或完成条件的具体不符之处，供下一版定向改正。

只输出 JSON：{{"expectation_match": float, "continuity": float, "clean_frame": float, "overall": float, "person_count": int|null, "watermark_detected": bool|null, "watermark_occluding": bool|null, "forbidden_text_detected": bool|null, "forbidden_text_is_provider_mark": bool|null, "space_type_matches": bool|null, "layout_detail_matches": bool|null, "material_contract_matches": bool|null, "issues": [str], "uncertainties": [str]}}"""
    frames = [image_b64] + ([prev_image_b64] if prev_image_b64 else [])
    raw = await hiagent.vlm_check(
        frames, expectation,
        call_meta={
            "initiator_label": initiator_label,
            "frame_kind": kind,
            "scene_setting": scene_setting,
            "has_prev_frame": bool(prev_image_b64),
        })
    defaults = {"continuity": 1.0} if not prev_image_b64 else None
    result = _parse_qa_result(raw, ["expectation_match", "continuity", "clean_frame"], defaults=defaults)
    if ignore_non_occluding_watermark:
        watermark_reported = result.get("watermark_detected") is True
        watermark_occluding = result.get("watermark_occluding")
        if watermark_reported and watermark_occluding is False:
            # Preserve the observed fact for audit, while explicitly telling
            # the deterministic policy that this provider mark is allowed by
            # the configured practical-quality mode.
            result["non_occluding_provider_watermark"] = True
            warnings = [str(item) for item in (result.get("warnings") or []) if str(item).strip()]
            warnings.append("检测到不遮挡主体的供应商角落标识，按实用质量模式不阻断采用")
            result["warnings"] = list(dict.fromkeys(warnings))
    # 动作/朝向/互动是关键帧的主项：把 overall 夹到不超过 expectation_match，避免"画面干净但动作不对"
    # 被 continuity/clean_frame 拉高均值而蒙混过审（VLM 即便没遵守上面的硬规则，这里也强制生效）。
    em = _score_or_none(result.get("expectation_match"))
    if em is not None:
        result["overall"] = round(min(float(result["overall"]), em), 3)
    if environment_only:
        from app.scene_policy import normalize_scene_image_qa
        result = normalize_scene_image_qa(result, environment_only=True)
    return result


async def review_portrait_image(image_b64: str, appearance_anchor: str) -> dict:
    """Review a character identity sheet without hard-gating acting direction.

    Portrait anchors can describe spirits, creatures, floating bodies, props, or
    acting direction. Only unmistakable identity/technical defects are hard
    gates; subjective styling differences remain review notes.
    """
    expectation = f"""你是漫剧角色定妆照评审 agent。请对照角色锚点检查这张单角色全身设定图，输出 JSON。

角色锚点：{appearance_anchor}

检查项（各 0~1 评分）：
1. identity_match      评估整体角色辨识度：性别、核心五官、主体形态与关键身份特征；年龄观感、服装款式、发型细节和装饰差异只作为参考
2. presentation_match  只评表演呈现：表情、眼神、笑容、气质、普通站姿/身姿是否符合锚点
3. clean_frame         单人物、主体完整、无遮挡主体的文字/水印/Logo、无肢体畸形/五官崩坏

评分硬规则（务必遵守）：
- 只有明确生成成其他角色、性别错误、核心身份/物种错误、非实体形态完全丢失、关键身份道具完全缺失、多人、明显畸形、严重裁切、遮挡主体的文字/水印属于硬门禁；不符时写入 hard_failures。
- 视觉年龄偏成熟或偏年轻、服装颜色/款式不同、发型细节或发饰不同、增加花瓣/莲花/普通光效等审美装饰，只写入 soft_warnings；不得写入 hard_failures，也不得因此把 identity_match 压到 0.60 以下。
- 角色锚点是创作方向，不是逐字验收清单。画面整体好看、人物清晰且核心身份成立时，应优先判为可用，不要因未逐项复刻文字细节而拒绝。
- 仅截掉脚尖、鞋尖、衣摆或发梢属于轻微裁切；供应商自动添加且位于角落、不遮挡人物的水印/Logo 属于轻微瑕疵。两者只写入 soft_warnings，不得写入 hard_failures。
- 表情、眼神、爱慕/妩媚/虚荣/戏谑等气质以及普通展示站姿只写入 soft_warnings，绝不能写入 hard_failures，也不能降低 identity_match。
- 定妆图允许中性表情和中性展示姿态；presentation_match 低可以有警告，但不能阻止后续生成 3/4 面和侧面。
- overall 只按 identity_match 与 clean_frame 的较低值给分，不计 presentation_match。
- 锚点没有要求的火焰、斗气光环或其他主体属于硬失败。

还要输出观察事实：person_count（人物数或 null）、watermark_detected、watermark_occluding（水印是否遮挡人物主体）、forbidden_text_detected、forbidden_text_is_provider_mark（禁止文字是否仅来自该非遮挡供应商角标）、full_body_visible、crop_severity（none/minor/major）、anatomy_valid（不能确认用 null）、stable_identity_matches（稳定身份特征是否匹配；不能确认用 null）、presentation_only_difference（差异是否仅为表情/姿态展示；不能确认用 null）。

只输出 JSON：{{"identity_match": float, "presentation_match": float, "clean_frame": float, "overall": float, "person_count": int|null, "watermark_detected": bool|null, "watermark_occluding": bool|null, "forbidden_text_detected": bool|null, "forbidden_text_is_provider_mark": bool|null, "full_body_visible": bool|null, "crop_severity": "none|minor|major", "anatomy_valid": bool|null, "stable_identity_matches": bool|null, "presentation_only_difference": bool|null, "soft_warnings": [str], "hard_failures": [str], "issues": [str]}}"""
    raw = await hiagent.vlm_check(
        [image_b64],
        expectation,
        call_meta={
            "initiator_label": "角色定妆照评审",
            "asset_kind": "portrait",
            "has_prev_frame": False,
        },
    )
    result = _parse_qa_result(
        raw,
        ["identity_match", "presentation_match", "clean_frame"],
    )
    try:
        raw_result = extract_json(raw)
    except Exception:  # noqa: BLE001 - _parse_qa_result already records recovery
        raw_result = {}
    for key in (
        "person_count", "watermark_detected", "watermark_occluding",
        "forbidden_text_detected", "forbidden_text_is_provider_mark",
        "full_body_visible", "crop_severity", "anatomy_valid",
        "stable_identity_matches", "presentation_only_difference",
        "soft_warnings", "hard_failures",
    ):
        if key in raw_result:
            result[key] = raw_result[key]
    from app.portrait_policy import normalize_portrait_seed_qa
    return normalize_portrait_seed_qa(result)


async def qa_shot(frames_b64: list[str], action_desc: str, scene_setting: str,
                  character_anchors: list[str], state_in: str = "", state_out: str = "",
                  required_dialogue: str = "", required_text: str = "",
                  structured_state: str = "", tracked_props: bool = False,
                  tracked_axis: bool = False,
                  *, duration_s: int | None = None,
                  duration_needs_review: bool = False,
                  visual_anchors: list[dict] | None = None,
                  image_manifest: list[dict] | None = None) -> dict:
    from app.multiview import watermark_qa_mode, video_visual_anchor_qa_enabled, build_image_manifest

    anchors = "\n".join(character_anchors) or "（缺少角色锚点，应回到分镜补角色）"
    duration_block = ""
    if duration_needs_review or (duration_s is not None and int(duration_s) > PREFERRED_SHOT_DURATION_S):
        duration_block = f"""
额外时长审核（本镜标称 {duration_s or '?'}s，超过默认 {PREFERRED_SHOT_DURATION_S}s）：
- duration_justified：若画面动作与口播显然在 {PREFERRED_SHOT_DURATION_S}s 内就能完成，必须为 false，并在 issues 写明「时长过长，建议改回 {PREFERRED_SHOT_DURATION_S}s」；
- 仅当连续动作/口播确实需要更长窗口时才为 true。
"""
    all_frames = list(frames_b64)
    manifest_entries: list[dict] = [{"role": "video_frame", "entity": f"frame_{i+1}"} for i in range(len(frames_b64))]
    if video_visual_anchor_qa_enabled() and visual_anchors:
        from pathlib import Path as _Path
        for anchor in visual_anchors:
            path_s = anchor.get("image_path") or anchor.get("path")
            if not path_s or not _Path(path_s).exists():
                continue
            try:
                all_frames.append(hiagent.encode_image_file(path_s))
            except OSError:
                continue
            if anchor.get("type") in {"plot_key_frame"} or anchor.get("role") == "keyframe":
                role = "candidate_keyframe"
            elif anchor.get("entity_type") == "character" or anchor.get("type") == "character":
                role = "character_anchor"
            elif anchor.get("entity_type") == "scene" or anchor.get("type") == "scene":
                role = "scene_anchor"
            elif anchor.get("type") == "previous_shot_frame":
                role = "continuity_anchor"
            else:
                role = "visual_anchor"
            manifest_entries.append({
                "role": role,
                "entity": anchor.get("entity_name") or anchor.get("name"),
                "view": anchor.get("view_role"),
            })
    effective_manifest = image_manifest or build_image_manifest(manifest_entries)
    wm_mode = watermark_qa_mode()
    wm_rule = (
        "小水印/Logo 不单独作为主扣分；仅遮挡脸/发型/衣服/手部动作或关键标志物时在对应主项扣分并标记 subject_occlusion"
        if wm_mode == "ignore_unless_occluding"
        else "无文字/水印/多余人物/肢体畸形"
    )
    expectation = f"""你是 AI 视频质检员。对照预期检查这些画面（视频抽帧 + 可选关键帧/人物/场景真值图），输出 JSON。

图片顺序清单（image_manifest）：{json.dumps(effective_manifest, ensure_ascii=False)}

预期画面：{action_desc}
预期起始状态：{state_in or '（未单列；按预期画面开头判断）'}
预期结束状态：{state_out or '（未单列；按预期画面结果判断）'}
预期场景：{scene_setting}
预期对白/声轨：{required_dialogue or '（无指定；若画面无法判断则 dialogue_match 给 1）'}
预期画面文字：{required_text or '（无指定；若无文字要求则 text_match 给 1）'}
结构化连续性合同：{structured_state or '（未提供；相关项给 1）'}
预期角色外观：
{anchors}
{duration_block}
检查项（各 0~1 评分）：
1. character_match  角色外观与人物真值/锚点相符；检查镜头内是否换脸/换发型/换装/体型跳变
2. action_match     核心动作是否真正出现
3. body_proportion  头身比与肢体完整性
4. outfit_match / hair_match / face_identity / scene_match  与真值一致；脸不可见时 face_identity 可为 null
5. clean_frame      {wm_rule}
6. start_state_match / end_state_match / dialogue_match / text_match  同前
7. prop_identity_match / prop_state_match / object_count_match  跟踪道具的版本外形、owner/location/form 及数量是否一致
8. camera_axis_match  若合同提供 axis_id，人物左右关系和视线轴不得无理由翻转
{"9. duration_justified  超过默认时长是否必要" if duration_block else ""}

硬规则：
- 只根据可见证据评分；action/character/outfit/hair 为主项；干净度不能抬高错人/错动作总分。
- 画内人物是否缺失必须以“画内角色合同”为准；明确标为画外的叙事关系人物不得按角色缺失、互动缺失或状态缺失扣分。
- overall 不得高于 character_match、action_match、start_state_match、end_state_match、dialogue_match、text_match，以及合同启用的道具/轴线主项。
- 缺必需分数时不要伪造满分。
- 对每个字段按 Schema 返回原始分数或布尔值，不输出自定义失败码；issues 只作审计说明。
{"- duration_justified=false 时 overall≤0.55。" if duration_block else ""}

只输出 JSON：{{"character_match": float, "action_match": float, "body_proportion": float, "outfit_match": float, "hair_match": float, "face_identity": float|null, "scene_match": float, "clean_frame": float, "start_state_match": float, "end_state_match": float, "dialogue_match": float, "text_match": float, "prop_identity_match": float, "prop_state_match": float, "object_count_match": float, "camera_axis_match": float, "no_story_repeat": bool, "no_future_leak": bool, "no_character_duplicate": bool, "whole_clip_usable": bool, "observed_state_out": str, "overall": float, "issues": [str]{', "duration_justified": bool' if duration_block else ''}}}"""
    try:
        raw = await hiagent.vlm_check(
            all_frames, expectation,
            call_meta={"initiator_label": "视频自动质检", "scene_setting": scene_setting,
                       "anchor_count": max(0, len(all_frames) - len(frames_b64))})
        defaults: dict[str, float] = {}
        if not state_in:
            defaults["start_state_match"] = 1.0
        if not state_out:
            defaults["end_state_match"] = 1.0
        if not required_dialogue:
            defaults["dialogue_match"] = 1.0
        if not required_text:
            defaults["text_match"] = 1.0
        if not tracked_props:
            defaults.update({
                "prop_identity_match": 1.0,
                "prop_state_match": 1.0,
                "object_count_match": 1.0,
            })
        if not tracked_axis:
            defaults["camera_axis_match"] = 1.0
        result = _parse_qa_result(
            raw,
            [
                "character_match", "action_match", "body_proportion", "outfit_match", "hair_match",
                "scene_match", "clean_frame",
                "start_state_match", "end_state_match", "dialogue_match", "text_match",
                "prop_identity_match", "prop_state_match", "object_count_match",
                "camera_axis_match",
            ],
            defaults=defaults,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "unverified",
            "overall": None,
            "issues": [f"视频 QA 未完成：{type(exc).__name__}: {exc}"],
            "qa_recovered": True,
            "image_manifest": effective_manifest,
            "contract_facts": [],
            "blocking_facts": [],
            "runtime_blocking": False,
        }
    try:
        raw_obj = extract_json(raw)
        face = raw_obj.get("face_identity")
        if face is None or (isinstance(face, str) and str(face).upper() in {"N/A", "NA"}):
            result["face_identity"] = None
        else:
            result["face_identity"] = max(0.0, min(1.0, float(face)))
    except Exception:  # noqa: BLE001
        result["face_identity"] = None
    caps = [
        _score_or_none(result.get(key))
        for key in ("character_match", "action_match", "start_state_match",
                    "end_state_match", "dialogue_match", "text_match")
    ]
    if tracked_props:
        caps.extend(_score_or_none(result.get(key)) for key in (
            "prop_identity_match", "prop_state_match", "object_count_match",
        ))
    if tracked_axis:
        caps.append(_score_or_none(result.get("camera_axis_match")))
    caps = [score for score in caps if score is not None]
    if caps:
        result["overall"] = round(min(float(result["overall"]), *caps), 3)
    if duration_block and result.get("duration_justified") is False:
        issues = list(result.get("issues") or [])
        if not any("时长" in str(x) for x in issues):
            issues.append(f"时长过长，建议改回 {PREFERRED_SHOT_DURATION_S}s")
        result["issues"] = issues
        result["overall"] = round(min(float(result.get("overall") or 1), 0.55), 3)
    result["image_manifest"] = effective_manifest
    result["status"] = "unverified" if result.get("qa_recovered") else "scored"
    return result


def evaluate_video_mode_qa(
    *,
    meta: dict,
    qa: dict,
    technical: dict,
) -> dict:
    """Build mode-specific evidence without conflating technical and semantic success."""
    from app.video_plan import VideoInputIntent

    mode = str(meta.get("actual_mode") or meta.get("mode") or "")
    technical_success = bool(technical.get("passed"))
    result: dict = {
        "planned_mode": meta.get("planned_mode") or mode,
        "actual_mode": mode,
        "technical_success": technical_success,
        "semantic_success": None,
        "input_roles_valid": False,
        "issues": [],
    }
    if mode == "REFERENCE_IMAGE_MODE":
        result["input_roles_valid"] = bool(
            not meta.get("first_frame_used")
            and not meta.get("last_frame_used")
            and not meta.get("reference_video_used")
        )
        result["semantic_success"] = (
            bool(qa.get("overall") is not None and float(qa["overall"]) >= 0.6)
            if qa.get("status") != "unverified" else None
        )
    elif mode == "FIRST_LAST_FRAME_MODE":
        result["input_roles_valid"] = bool(
            meta.get("first_frame_used")
            and meta.get("last_frame_used")
            and not meta.get("reference_image_used")
            and not meta.get("reference_video_used")
        )
        result["boundary_start_match"] = qa.get("start_state_match")
        result["boundary_end_match"] = qa.get("end_state_match")
        if qa.get("status") != "unverified":
            try:
                result["semantic_success"] = bool(
                    float(qa.get("start_state_match")) >= 0.6
                    and float(qa.get("end_state_match")) >= 0.6
                )
            except (TypeError, ValueError):
                result["semantic_success"] = None
    elif mode == "FIRST_FRAME_MODE":
        result["input_roles_valid"] = bool(
            meta.get("first_frame_used")
            and not meta.get("last_frame_used")
            and not meta.get("reference_image_used")
            and not meta.get("reference_video_used")
        )
        result["boundary_start_match"] = qa.get("start_state_match")
        if qa.get("status") != "unverified" and qa.get("start_state_match") is not None:
            try:
                result["semantic_success"] = bool(
                    float(qa.get("start_state_match")) >= 0.6
                )
            except (TypeError, ValueError):
                result["semantic_success"] = None
    elif mode == "VIDEO_INPUT_MODE":
        result["input_roles_valid"] = bool(
            meta.get("reference_video_used")
            and not meta.get("reference_image_used")
            and not meta.get("first_frame_used")
            and not meta.get("last_frame_used")
        )
        intent = str(meta.get("video_input_intent") or "")
        result["video_input_intent"] = intent
        result["provider_read_video"] = technical_success
        if intent == VideoInputIntent.CONTINUE_PREVIOUS_TAKE.value:
            # A normal VLM content score cannot certify trajectory continuation.
            result["semantic_success"] = None
            result["issues"].append("真续写需通过独立多样本边界语义回归")
        elif qa.get("status") != "unverified" and qa.get("overall") is not None:
            result["semantic_success"] = bool(float(qa["overall"]) >= 0.6)
    else:
        result["issues"].append("未知 actual_mode")
    if not result["input_roles_valid"]:
        result["issues"].append("供应商输入角色与计划模式不一致")
    return result
