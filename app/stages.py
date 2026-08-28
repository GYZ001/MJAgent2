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
    resolution_declares_functional_identity,
)
from app.continuity import (adaptation_hook_errors)
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
from app.schemas import (AppearanceEvidence, Bible, Character, CharacterAffiliation, CharacterAlias,
                         CharacterRelation, Dialogue, EMOTIONS, EpisodeScreenplay,
                         Relationship, Scene, StoryboardOutline, World,
                         StoryboardOutlineShot, extract_json, normalize_screenplay_json_shape,
                         schema_errors, _repair_json_key_after_colon)
from app.validators import (ending_hook_is_grounded,
                            ending_hook_grounding_report,
                            key_line_catalog,
                            source_dialogue_fragments,
                            validate_bible, validate_screenplay,
                            validate_scene_bible)
from app.renderability import (
    DIALOGUE_CHAIN_TURNS_HARD_MAX,
    renderability_prompt_block,
)
from app.source_excerpt import (
    index_source_segments,
    render_indexed_source,
    structural_front_matter_ids,
)
from app.source_facts import (
    SOURCE_FACT_VERSION,
    source_facts,
    source_segment_facts,
)
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
    if stage_key.startswith("character_bible"):
        base_call_meta = _bible_short_json_call_meta(base_call_meta)
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
        if prefill and isinstance(obj, dict):
            obj.update(prefill)
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

BIBLE_HEAD_CHAPTERS = 20         # 首版人物谱发现窗口：只看前二十章；按一章一小请求并发
BIBLE_LOOKAHEAD_CHAPTERS = 0     # 发现窗口已扩到 60 章，不再额外扩大裁决卷宗范围
BIBLE_RECURRING_MIN_ONSTAGE_QUOTES = 2  # 至少两条经裁决闸核验的「本人在场」证据才算重要角色
# 且这些证据至少跨两个章节。只数条数分不出「跨章反复登场的人物」和「在某一章里连说
# 三句话的路人」：类别称谓（「绿袍男子」这种靠衣着指人、换个场合就指别人的说法）往往
# 在单章里就能凑满条数。人物谱的作用域是全书，进这份名单的判据也该是全书级的复现。
BIBLE_RECURRING_MIN_ONSTAGE_CHAPTERS = 2
# 全文统计通道门槛：命中量 + 章节覆盖率同时达标即可独立进入名单，不依赖模型裁决。
BIBLE_STATISTICAL_MIN_MENTIONS = 25
BIBLE_STATISTICAL_MIN_CHAPTER_RATIO = 0.15
# 真名替换门槛：真名必须至少和名单称呼一样常见，才有资格当人物谱主名。
# 旧值 0.2 会把「靠山老祖 1072 次 / 白主 344 次」改成主名「白主」，
# 更常用的原文称呼连 aliases 都进不去，检索直接落空。
BIBLE_FORMAL_NAME_MIN_RATIO = 1.0
BIBLE_MUST_COVER_MAX = 20        # 前 60 章重要角色容量；详情仍逐角色小请求生成
# 点名调用每个候选最多申报几条在场证据。判据只需要 BIBLE_RECURRING_MIN_ONSTAGE_QUOTES
# （=2）条核验通过的证据；这里留 1 条余量应付结构闸/裁决闸刷掉个别证据，不留更多——
# 多留的每一条对戏份多的主角都是纯浪费：一个出场上千次的主角，旧提示词「尽量都列出来」
# 会让模型老实列出十几条，既拉长点名调用本身的输出（更容易撞 max_tokens 截断，撞了就要
# 整次重试），又线性拉长下游裁决闸的调用条数。见 `_recurring_character_names` docstring。
BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE = 3
BIBLE_ROLL_CALL_CHUNK_CHAPTERS = 1
BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS = 8000
BIBLE_ROLL_CALL_CHUNK_MAX_TOKENS = 12000
BIBLE_ROLL_CALL_CONCURRENCY = 6
BIBLE_ROLL_CALL_MAX_ATTEMPTS = 3
BIBLE_ROLL_CALL_TIMEOUT_S = 300.0
BIBLE_SMALL_VERDICT_TIMEOUT_S = 120.0
BIBLE_FIRST_TOKEN_TIMEOUT_S = float(config.TIMEOUT_CHAT_FIRST_TOKEN_S)


def _corpus_scoped_chapter_threshold(threshold: int, available_chapters: int) -> int:
    """把「至少覆盖 N 章」的门槛压回语料实际有的章数之内。

    「跨 N 章复现」在只有 M < N 章的语料里不是更严格的标准，而是结构上永远
    判不过的判据——它挂在了语料被切成几章上，不挂在「这个人是不是反复登场」上。
    真实故障：《王六郎》全文 2944 字只切出 1 章，3 个候选、4 条在场证据全部通过
    结构闸与裁决闸，必收名单仍是空的，人物谱以「未产出任何经原文核验的角色候选」
    整体失败。章数够的语料上封顶不生效，跨章判据原样保留。
    """
    return max(1, min(int(threshold), max(1, int(available_chapters))))


def _bible_short_json_call_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """人物谱短 JSON 调用：关掉 thinking，并给 0 字节流式空等一个首字上限。

    调用方传入的 ``first_token_timeout_s`` 优先；详情比点名长，需要更宽的首字窗。
    """
    merged = {**meta, "disable_thinking": True}
    if "first_token_timeout_s" not in meta:
        merged["first_token_timeout_s"] = BIBLE_FIRST_TOKEN_TIMEOUT_S
    return merged

# 旁文本净化的三个闸（见 `_chapters_without_paratext` docstring 里的事故口径）：
BIBLE_PARATEXT_MARGIN_CHAPTERS = 3   # 净化只会让正文变短，头部因此可能多吃进几章
BIBLE_PARATEXT_CONCURRENCY = 8       # 净化各章互不依赖，chat 路径也没有全局信号量
# 旁文本只是可选净化，绝不能占据人物谱主链路两分钟。真实调用一般 2~5 秒；
# 首批并发在短预算内能完成多少就采用多少，其余原文直通并留给后续按章缓存。
BIBLE_PARATEXT_BUDGET_S = 15.0
BIBLE_PARATEXT_CHAPTER_TIMEOUT_S = 8.0


def _bible_source_plan(
    valid: list[dict], budget: int, head_chapters: int | None,
) -> list[tuple[int, int, bool]]:
    """规划人物谱源文本读哪几章、每章读多少字：`(章在 valid 里的下标, 截取字数, 是否节选)`。

    渲染（`_render_bible_source`）和「哪几章需要先净化旁文本」
    （`_bible_paratext_scope`）共用这一份规划：口径只有一处，不会漂移出
    「净化了 643 章、真正读的只有 33 章」那种落差。
    """
    plan: list[tuple[int, int, bool]] = []
    # 头部顺序铺设：用至多 70% 预算（其余留给后段抽样）。
    head_budget = int(budget * 0.7)
    if head_chapters:
        # 首版人物谱要求「完整读完前 N 章」。按比例切的头部会随章节长度漂移：
        # 长章小说可能读到第三、四章就把头部预算用光，主要配角整体缺席。
        head_budget = min(budget, max(head_budget, sum(
            len(ch["content"].strip()) for ch in valid[:head_chapters]
        )))
    used = 0
    head_count = 0
    for index, ch in enumerate(valid):
        remain = head_budget - used
        if remain <= 200:
            break
        take = min(len(ch["content"].strip()), remain)
        plan.append((index, take, False))
        used += take
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
                content = later[li]["content"].strip()
                take = min(len(content), _BIBLE_TAIL_SLICE_CHARS, remain_budget)
                plan.append((head_count + li, take, True))
                remain_budget -= take
    return plan


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

    blocks: list[str] = []
    for index, take, excerpt in _bible_source_plan(valid, budget, head_chapters):
        ch = valid[index]
        content = ch["content"].strip()
        clipped = content[:take]
        if excerpt:
            suffix = "……（节选开头，仅供识别后期登场角色）" if len(content) > take else ""
            blocks.append(f"【{_title(ch)}·节选】\n{clipped}{suffix}")
        else:
            suffix = "……（原文过长已截断）" if len(content) > take else ""
            blocks.append(f"【{_title(ch)}】\n{clipped}{suffix}")

    return "\n\n".join(blocks)


class _RosterOnstageEvidence(BaseModel):
    """一条「本人在场」证据申报：模型只负责申报，是否真的成立由后端结构闸 +
    独立裁决闸核验（见 `_recurring_character_names` docstring）。"""

    chapter_index: int = -1
    quote: str = ""


class _RosterCandidate(BaseModel):
    """人物点名候选；aliases/identity_evidence 让跨章归一可以晚于首次点名完成。

    personhood 是建卡资格，不是最终身份：person 可建卡，non_person 明确不建卡，
    uncertain 延迟绑定——先留着称呼，等真名/在场证据，而不是直接从名单抹掉。
    """

    primary_appellation: str = ""
    formal_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    identity_evidence: list[_RosterOnstageEvidence] = Field(default_factory=list)
    onstage_evidence: list[_RosterOnstageEvidence] = Field(default_factory=list)
    personhood: Literal["person", "non_person", "uncertain"] = "uncertain"
    # 称呼形态由资格裁决里的模型判定，程序不查词表。referential 这类只描述外形或
    # 身份的代称不能自己建卡，要先由身份归一裁决认领到某个人身上。
    name_form: Literal[
        "personal_name", "honorific", "referential", "uncertain",
    ] = "uncertain"


class _CharacterRollCall(BaseModel):
    """人物点名合同：候选 + 在场证据，不再只是名字字符串。"""

    candidates: list[_RosterCandidate] = Field(default_factory=list)


class _RosterIdentityResolution(BaseModel):
    """描述性称呼与候选实体的局部消歧结果。"""

    verdict: Literal["same", "different", "uncertain"] = "uncertain"
    canonical_appellation: str = ""
    supporting_chapter_index: int = -1


class _MentionedCharacterImportanceResolution(BaseModel):
    """仅被提及角色是否值得进入人物谱的证据裁决。"""

    verdict: Literal["retain", "drop", "uncertain"] = "uncertain"
    supporting_chapter_index: int = -1
    reason: str = ""


def _shared_appellations(candidates: list["_RosterCandidate"]) -> set[str]:
    """本次点名里被多个不同候选共用的称呼，不能当个体标识。

    判据从输入推导，不枚举「少年/胖子/弟子」这类词表——类别词是开放集合，
    穷举不完，而且同一个词在别的作品里可能就是某个人的固定绰号。一个称呼
    只要被两个以上候选各自申报，它在这本书里就分不出人，只能送模型消歧。
    """
    owners: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        owner = (candidate.primary_appellation or "").strip()
        if not owner:
            continue
        for name in _candidate_appellations(candidate):
            owners[name].add(owner)
    return {name for name, group in owners.items() if len(group) > 1}


def _is_composite_appellation(value: str, known_names: set[str]) -> bool:
    """带属格的组合指称（「X 的 Y」）指向的是关系，不是稳定人物身份。

    「的」是汉语属格标记，属于语法结构而不是某本书的词表；这里只判结构，
    是不是同一个人交给身份归一裁决。
    """
    text = (value or "").strip()
    if not text or "的" not in text:
        return False
    if any(name and name != text and name in text for name in known_names):
        return True
    return text.index("的") > 0 and text.index("的") < len(text) - 1


def _roster_label_needs_identity_resolution(
    candidate: "_RosterCandidate", known_names: set[str], ambiguous: set[str],
) -> bool:
    """要不要送身份归一：形态由模型裁决，剩下两条判据从本次点名数据推导。"""
    text = (candidate.formal_name or candidate.primary_appellation or "").strip()
    return (
        candidate.name_form == "referential"
        or text in ambiguous
        or _is_composite_appellation(text, known_names)
    )


def _roster_appellation_mentions(
    candidate: "_RosterCandidate", chapters_by_idx: dict[int, str],
) -> int:
    """这个候选的全部称呼在原文里的合计命中数。"""
    terms = _candidate_appellations(candidate)
    if not terms:
        return 0
    return sum(text.count(term) for text in chapters_by_idx.values() for term in terms)


def _roster_candidate_stands_alone(
    candidate: "_RosterCandidate",
    specific: list["_RosterCandidate"],
    chapters_by_idx: dict[int, str],
) -> bool:
    """归并不成立时，这个称呼是不是「比名单里最常见的那个还常见」。

    身份归一裁决回答的只是「这个称呼能不能并到名单里另一个实体身上」。它答不出来
    说明并不过去，不说明称呼背后没有人——把这两件事当成一件，会让「没有正式姓名、
    全书只以代称出现」的角色结构上必然出局：它因 name_form=referential 被送去归一，
    而归一的候选实体名单里恰恰没有它自己，于是必判 uncertain，随即被当泛称删除。

    真实故障（《王六郎》proj_177d147e16c7）：主角「许某」全篇提及 34 次、自报 3 条
    在场证据全部通过结构闸，被归一裁决拿去和「王六郎/异史氏」比对判 uncertain 后
    整个丢弃，必收名单只剩 1 人；人物谱里那张「许」卡是主生成模型事后自造的单字名，
    拿它做子串检索会命中「也许」「许多」「许姓」。

    判据取「不低于 specific 里的最大提及数」，而不是任何绝对次数门槛。绝对门槛在
    这里必然失效：类别称谓在长篇里比真配角出现得更多——《我欲封天》1616 章语料中
    「绿袍男子」498 次 / 覆盖 138 章、「精明男子」503 次，都远高于真角色「王有材」
    的 58 次，任何够低到能救《王六郎》许某（34 次）的门槛都会把它们一并放回来。
    相对位置才分得开：许某比名单里最常见的「王六郎」（25 次）还常见，只可能是被
    误删的主角；绿袍男子相对孟浩的 55137 次差三个数量级，仍是类别称谓。

    这条通道刻意保守——它只救「比主角还常见却被整个删掉」这一种极端情形。规模不
    及主角的第二主角救不回来，那是漏救；放错方向会让类别称谓涌进人物谱，代价大得多。
    比较取严格大于：平局在小样本里太廉价（两个各出现一次的称呼谁也不比谁常见），
    放行平局等于把这道闸开成常开。
    """
    if not specific:
        return False
    mentions = _roster_appellation_mentions(candidate, chapters_by_idx)
    if mentions <= 0:
        return False
    strongest = max(
        _roster_appellation_mentions(item, chapters_by_idx) for item in specific
    )
    return mentions > strongest


def _candidate_appellations(candidate: _RosterCandidate) -> set[str]:
    return {
        value.strip() for value in [
            candidate.primary_appellation, candidate.formal_name, *candidate.aliases,
        ] if value and value.strip()
    }


def _coerce_roster_chapter_index(value: Any) -> int:
    """程序拥有章号：模型常写「第1章」、[1]、['1']，不能因此把已判定的 person 打成 uncertain。"""
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value if value > 0 else -1
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else -1
    if isinstance(value, list) and value:
        return _coerce_roster_chapter_index(value[0])
    if isinstance(value, str):
        match = re.search(r"(\d+)", value.strip())
        if match:
            number = int(match.group(1))
            return number if number > 0 else -1
    return -1


def _normalize_roster_verdict_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """模型常把 verdict 写成 judge_result/判断结果；缺字段时不能默成 uncertain 再淘汰。"""
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    if "verdict" not in normalized:
        for key in ("judge_result", "result", "判断结果", "reveal_status"):
            if key in normalized:
                normalized["verdict"] = normalized[key]
                break
    if "supporting_chapter_index" in normalized:
        normalized["supporting_chapter_index"] = _coerce_roster_chapter_index(
            normalized["supporting_chapter_index"],
        )
    return normalized


def _require_explicit_verdict(payload: Any) -> Any:
    if isinstance(payload, dict) and "verdict" not in payload:
        raise ValueError("verdict is required")
    return payload


def _pin_roster_name_to_source(
    name: str, texts: list[str], *, fallback_texts: list[str] | None = None,
) -> str:
    """名称必须钉在原文上：逐字命中优先；仅当证据文本没有该写法时，才允许唯一的一字之差。"""
    text_name = (name or "").strip()
    if not text_name:
        return ""
    if any(text_name in (text or "") for text in texts):
        return text_name
    length = len(text_name)
    if length >= 2:
        found: list[str] = []
        for text in texts:
            body = text or ""
            for index in range(0, max(0, len(body) - length + 1)):
                span = body[index:index + length]
                if not all("\u4e00" <= char <= "\u9fff" for char in span):
                    continue
                if sum(left != right for left, right in zip(text_name, span, strict=True)) == 1:
                    found.append(span)
        unique = list(dict.fromkeys(found))
        if len(unique) == 1:
            return unique[0]
    if fallback_texts and any(text_name in (text or "") for text in fallback_texts):
        return text_name
    return ""


def _candidate_source_texts(
    candidate: _RosterCandidate, chapters_by_idx: dict[int, str],
) -> list[str]:
    texts: list[str] = []
    for evidence in [*candidate.onstage_evidence, *candidate.identity_evidence]:
        quote = (evidence.quote or "").strip()
        if quote:
            texts.append(quote)
        chapter_text = chapters_by_idx.get(evidence.chapter_index, "")
        if chapter_text:
            texts.append(chapter_text)
    return texts


def _pin_roster_candidates_to_source(
    candidates: list[_RosterCandidate],
    chapters_by_idx: dict[int, str],
) -> list[_RosterCandidate]:
    """程序拥有名称匹配：模型申报的称呼若钉不进原文，就不能建卡。"""
    pinned: list[_RosterCandidate] = []
    for candidate in candidates:
        texts = _candidate_source_texts(candidate, chapters_by_idx)
        chapter_texts = list(chapters_by_idx.values())
        primary = _pin_roster_name_to_source(
            candidate.primary_appellation, texts, fallback_texts=chapter_texts,
        )
        if not primary:
            continue
        formal = _pin_roster_name_to_source(
            candidate.formal_name, texts, fallback_texts=chapter_texts,
        )
        aliases = []
        for alias in candidate.aliases:
            pinned_alias = _pin_roster_name_to_source(
                alias, texts, fallback_texts=chapter_texts,
            )
            if pinned_alias and pinned_alias not in {primary, formal, *aliases}:
                aliases.append(pinned_alias)
        pinned.append(candidate.model_copy(update={
            "primary_appellation": primary,
            "formal_name": "" if not formal or formal == primary else formal,
            "aliases": aliases,
        }))
    return pinned


def _identity_merge_keys(candidate: _RosterCandidate) -> set[str]:
    """连通分量的候选键就是这个候选自己申报的全部称呼。

    这里不做「哪些词算类别词」的过滤——那需要词表，而词表是开放集合。
    合并的真正约束在下面：共享称呼必须是某一方的主称呼，且两人要在原文里
    共现得上；分不出人的称呼由后面的身份归一裁决交给模型判。
    """
    return {value for value in _candidate_appellations(candidate) if value}


def _merge_roll_call_candidates(
    chunk_results: list[list[_RosterCandidate]],
) -> list[_RosterCandidate]:
    """按同章明确身份链接做连通分量归并；规范名始终优先使用正式姓名。"""
    flattened = [candidate for group in chunk_results for candidate in group
                 if (candidate.primary_appellation or "").strip()]
    parent = list(range(len(flattened)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(flattened)):
        left_names = _identity_merge_keys(flattened[left])
        if not left_names:
            continue
        left_primary = (flattened[left].primary_appellation or "").strip()
        for right in range(left + 1, len(flattened)):
            right_names = _identity_merge_keys(flattened[right])
            shared = left_names & right_names
            if not shared:
                continue
            right_primary = (flattened[right].primary_appellation or "").strip()
            # 两人碰巧被写成同一个 formal_name（王有材/小胖子都「揭示」成李富贵）
            # 时，共享的只能是那个真名，不是任何一方的主称呼，不得合并。
            if shared & {left_primary, right_primary}:
                union(left, right)

    groups: dict[int, list[_RosterCandidate]] = {}
    for index, candidate in enumerate(flattened):
        groups.setdefault(find(index), []).append(candidate)

    merged: list[_RosterCandidate] = []
    for group in groups.values():
        formal_names = [item.formal_name.strip() for item in group if item.formal_name.strip()]
        primary = group[0].primary_appellation.strip()
        formal = formal_names[0] if formal_names else ""
        aliases = list(dict.fromkeys(
            value for item in group for value in _candidate_appellations(item)
            if value and value not in {formal, primary}
        ))
        if formal and primary != formal and primary not in aliases:
            aliases.insert(0, primary)
        evidence: list[_RosterOnstageEvidence] = []
        identity_evidence: list[_RosterOnstageEvidence] = []
        for item in group:
            evidence.extend(item.onstage_evidence)
            identity_evidence.extend(item.identity_evidence)
        deduped = list({(item.chapter_index, item.quote): item for item in evidence}.values())
        deduped_identity = list({
            (item.chapter_index, item.quote): item for item in identity_evidence
        }.values())
        personhoods = {item.personhood for item in group}
        if "person" in personhoods:
            personhood = "person"
        elif personhoods == {"non_person"}:
            personhood = "non_person"
        else:
            personhood = "uncertain"
        merged.append(_RosterCandidate(
            primary_appellation=primary,
            formal_name=formal,
            aliases=aliases,
            identity_evidence=deduped_identity[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE],
            onstage_evidence=deduped[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE],
            personhood=personhood,
        ))
    return merged


async def _resolve_generic_character_candidates(
    candidates: list[_RosterCandidate],
    chapters_by_idx: dict[int, str],
    *,
    project_id: str | None = None,
) -> list[_RosterCandidate]:
    """把描述性称呼裁决到已有实体；不确定时不再凭空创建新角色。"""
    known_names = {value for item in candidates for value in _candidate_appellations(item)}
    # 归并已经跑完，同一个人的多份点名结果已经合成一条。此刻还被多个候选共用的
    # 称呼，在这本书里就分不出人，必须交给模型消歧，不能各自建卡。
    ambiguous = _shared_appellations(candidates)
    specific = [
        candidate for candidate in candidates
        if not _roster_label_needs_identity_resolution(candidate, known_names, ambiguous)
    ]
    if not specific:
        return candidates
    kept: list[_RosterCandidate] = []
    jobs: list[tuple[_RosterCandidate, str, list[str]]] = []
    candidate_names = [
        {
            "canonical": item.formal_name or item.primary_appellation,
            "appellations": sorted(_candidate_appellations(item)),
        }
        for item in specific
    ]
    for candidate in candidates:
        label = (candidate.formal_name or candidate.primary_appellation).strip()
        if not _roster_label_needs_identity_resolution(candidate, known_names, ambiguous):
            kept.append(candidate)
            continue
        evidence_blocks: list[str] = []
        for evidence in candidate.onstage_evidence[:2]:
            chapter_text = chapters_by_idx.get(evidence.chapter_index, "")
            dossier = _roster_presence_dossier(
                evidence.chapter_index, chapter_text, evidence.quote,
            )
            if dossier:
                evidence_blocks.append(json.dumps(dossier, ensure_ascii=False))
        if not evidence_blocks:
            # 卷宗都建不出来就没法问归一，但「问不成」同样不是「这个人不存在」，
            # 判据与归一失败那条路径共用（见 `_roster_candidate_stands_alone`）。
            if _roster_candidate_stands_alone(candidate, specific, chapters_by_idx):
                kept.append(candidate)
            continue
        prompt = f"""任务：判断描述性称呼「{label}」是否是候选实体名单中的同一个人物。

候选实体：
{json.dumps(candidate_names, ensure_ascii=False)}

描述性称呼的原文卷宗：
{chr(10).join(evidence_blocks)}

硬规则：
1. 只有原文中的同场连续指代、动作连续、对话连续或明确命名句才能判 same。
2. 外貌相似、年龄相近、都在同一宗门、常识猜测都不能判 same。
3. canonical_appellation 只能逐字选择候选实体的 canonical 值。
4. 无法充分证明时 verdict=uncertain；描述性称呼不能因此创建独立人物谱角色。
"""
        jobs.append((candidate, prompt, evidence_blocks))

    async def _resolve_one(
        candidate: _RosterCandidate, prompt: str, evidence_blocks: list[str],
    ) -> tuple[_RosterCandidate, _RosterIdentityResolution] | None:
        label = (candidate.formal_name or candidate.primary_appellation).strip()
        try:
            resolution = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX},
                     {"role": "user", "content": prompt}],
                    model_type=_RosterIdentityResolution,
                    validate=None,
                    operation_id="character_identity_resolution:" + hashlib.sha256(
                        f"{label}:{chr(10).join(evidence_blocks)}".encode("utf-8")
                    ).hexdigest(),
                    temperature=0.0,
                    max_tokens=512,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "人物身份归一",
                        "stage_key": "character_identity_resolution",
                        "call_role": "stage_validate",
                        "character_name": label,
                        "project_id": project_id,
                    }),
                ),
                timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - 不确定时宁可不新建泛称角色
            return None
        if resolution.verdict != "same":
            return None
        return candidate, resolution

    resolved = await asyncio.gather(*(
        _resolve_one(candidate, prompt, evidence_blocks)
        for candidate, prompt, evidence_blocks in jobs
    ))
    # 合并回 specific 必须串行：两个泛称可能指向同一实体，并行写 aliases 会丢条目。
    for (asked, _prompt, _blocks), item in zip(jobs, resolved, strict=True):
        if item is None:
            # 归并不成立：并不到别人身上的称呼，只有在全书没有独立存在规模时才是
            # 泛称；够规模的放回普通候选，由三条准入通道决定去留。
            if _roster_candidate_stands_alone(asked, specific, chapters_by_idx):
                kept.append(asked)
            continue
        candidate, resolution = item
        target = next((
            entry for entry in specific
            if (entry.formal_name or entry.primary_appellation) == resolution.canonical_appellation
        ), None)
        if target is None:
            continue
        aliases = list(dict.fromkeys([
            *target.aliases, candidate.primary_appellation, candidate.formal_name,
            *candidate.aliases,
        ]))
        target.aliases = [
            value for value in aliases
            if value and value not in {target.primary_appellation, target.formal_name}
        ]
        target.onstage_evidence = list({
            (entry.chapter_index, entry.quote): entry
            for entry in [*target.onstage_evidence, *candidate.onstage_evidence]
        }.values())[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE]
        target.identity_evidence = list({
            (entry.chapter_index, entry.quote): entry
            for entry in [*target.identity_evidence, *candidate.onstage_evidence]
        }.values())[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE]
    return kept


class _RosterPersonhoodResolution(BaseModel):
    """点名候选是不是可定妆的人物。三态由问题结构决定，不枚举器物/野兽名单。"""

    verdict: Literal["person", "non_person", "uncertain"] = "uncertain"
    supporting_chapter_index: int = -1
    name_form: Literal[
        "personal_name", "honorific", "referential", "uncertain",
    ] = "uncertain"

    @model_validator(mode="before")
    @classmethod
    def _require_verdict(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = _normalize_roster_verdict_payload(value)
        return _require_explicit_verdict(value)


class _RosterTrueNameResolution(BaseModel):
    """发现窗口之后原文是否揭示了这个称呼对应的正式姓名。"""

    verdict: Literal["revealed", "unrevealed", "uncertain"] = "uncertain"
    true_name: str = ""
    supporting_chapter_index: int = -1
    supporting_quote: str = ""

    @model_validator(mode="before")
    @classmethod
    def _require_verdict(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = _normalize_roster_verdict_payload(value)
        return _require_explicit_verdict(value)


BIBLE_PERSONHOOD_DOSSIER_SEGMENTS = 12


def _roster_personhood_dossier(
    candidate: _RosterCandidate, chapters_by_idx: dict[int, str],
) -> list[dict[str, Any]]:
    """资格卷宗必须含候选称呼本身。真实故障：孟浩的在场引句扩成「孟兄」段，
    模型据此判 uncertain，主角从人物谱消失。

    段落跨整个窗口取样而不是取最靠前的几段：判「这是人还是器物」要看它在不同
    场合怎么被使用，只给开头连着的几段，模型看到的可能全是同一个场景。
    """
    blocks: list[dict[str, Any]] = []
    names = [value for value in _candidate_appellations(candidate) if value]
    if not names:
        return blocks

    def _add(item: dict[str, Any]) -> bool:
        text = item.get("text") or ""
        if not any(name in text for name in names):
            return False
        if item not in blocks:
            blocks.append(item)
        return len(blocks) >= BIBLE_PERSONHOOD_DOSSIER_SEGMENTS

    for evidence in candidate.onstage_evidence[:3]:
        chapter_text = chapters_by_idx.get(evidence.chapter_index, "")
        for item in _roster_presence_dossier(
            evidence.chapter_index, chapter_text, evidence.quote,
        ):
            if _add(item):
                return blocks
    for item in _spread_named_segments(
        names, chapters_by_idx, limit=BIBLE_PERSONHOOD_DOSSIER_SEGMENTS,
    ):
        if _add(item):
            return blocks
    return blocks


def _named_hit_chapters(names: list[str], chapters_by_idx: dict[int, str]) -> list[int]:
    anchors = [value.strip() for value in names if value and value.strip()]
    if not anchors:
        return []
    return [
        chapter_idx for chapter_idx in sorted(chapters_by_idx)
        if any(anchor in (chapters_by_idx.get(chapter_idx) or "") for anchor in anchors)
    ]


def _spread_named_segments(
    names: list[str], chapters_by_idx: dict[int, str], *, limit: int,
    segment_max_chars: int = 240, offset: int = 0,
) -> list[dict[str, Any]]:
    """检索含这些称呼的原文段，跨全部章节交错取样。

    程序只负责把上下文找齐给模型，不在这里做任何关于这个人的判断。命中章按
    固定步长挑选，offset 让相邻的几批取到互不重叠的章，多批合起来就能把
    跨度铺满——一个只在某一章交代的身份，靠单批均匀取样很容易正好被跳过。
    """
    anchors = [value.strip() for value in names if value and value.strip()]
    if not anchors or limit <= 0:
        return []
    hit_chapters = _named_hit_chapters(anchors, chapters_by_idx)
    if not hit_chapters:
        return []
    if len(hit_chapters) > limit:
        stride = max(1, math.ceil(len(hit_chapters) / limit))
        hit_chapters = hit_chapters[offset % stride::stride][:limit]
    elif offset:
        return []
    blocks: list[dict[str, Any]] = []
    for chapter_idx in hit_chapters:
        chapter_text = chapters_by_idx.get(chapter_idx) or ""
        for index, segment in enumerate(
            index_source_segments(chapter_text, max_chars=segment_max_chars)
        ):
            if not any(anchor in segment.text for anchor in anchors):
                continue
            blocks.append({
                "chapter_idx": chapter_idx,
                "segment_index": index + 1,
                "text": segment.text,
            })
            break
        if len(blocks) >= limit:
            break
    return blocks


async def _filter_non_person_roster_candidates(
    candidates: list[_RosterCandidate],
    chapters_by_idx: dict[int, str],
    *,
    project_id: str | None = None,
) -> list[_RosterCandidate]:
    """铜镜、没有自己姓名的野兽、一次性描述不是人物谱角色。

    建卡用延迟绑定：只有明确 non_person 才丢掉；uncertain 先留着，交给真名/在场闸。
    """

    async def _judge(candidate: _RosterCandidate) -> _RosterCandidate | None:
        label = (candidate.formal_name or candidate.primary_appellation).strip()
        dossier = _roster_personhood_dossier(candidate, chapters_by_idx)
        if not label:
            return None
        if not dossier:
            return candidate.model_copy(update={"personhood": "uncertain"})
        catalog = "\n\n".join(
            f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
            for item in dossier
        )
        valid_chapters = {int(item["chapter_idx"]) for item in dossier}
        names = sorted(_candidate_appellations(candidate))
        prompt = f"""任务：判断「{label}」是不是可单独指认、能画定妆照的人物。
同一人物的其它称呼：{json.dumps(names, ensure_ascii=False)}。这些称呼指向同一个人时，按一个人判断。

原文卷宗：
{catalog}

只根据卷宗原文判断，JSON 字段必须用 verdict：
- person：卷宗里这个称呼指一个能说话或行动的人物，有稳定身份，可以作为人物谱角色。
- non_person：这个称呼指器物、法宝、地点、组织、没有自己姓名的野兽，或无法对应到具体人名的一次性描述。
- uncertain：卷宗不够，还不能决定。证据不足时选 uncertain，不要猜。

同时用 name_form 说明「{label}」这个写法本身是哪一种称呼形态：
- personal_name：人物的姓名，包括姓名、单名、以及被当作固定名字使用的绰号。
- honorific：姓氏或关系加上称呼，例如某某师姐、某某爷，指人但不是姓名。
- referential：靠外形、衣着、年龄、身份或方位来指人的说法，换个场合就可能指别人。
- uncertain：卷宗不足以判断这个写法属于哪一种。

supporting_chapter_index 必须是卷宗里出现过的数字章号，例如 1，不要写「第1章」或数组。不得根据常识或作品知识补充。
"""
        try:
            resolution = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX},
                     {"role": "user", "content": prompt}],
                    model_type=_RosterPersonhoodResolution,
                    validate=None,
                    normalize_payload=_normalize_roster_verdict_payload,
                    operation_id="character_personhood:" + hashlib.sha256(
                        f"{label}:{catalog}".encode("utf-8")
                    ).hexdigest(),
                    temperature=0.0,
                    max_tokens=256,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "人物候选资格",
                        "stage_key": "character_personhood",
                        "call_role": "stage_validate",
                        "character_name": label,
                        "project_id": project_id,
                    }),
                ),
                timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - 不确定就延迟绑定，不从名单抹掉
            return candidate.model_copy(update={"personhood": "uncertain"})
        chapter_ok = resolution.supporting_chapter_index in valid_chapters
        name_in_dossier = any(
            any(name in (item.get("text") or "") for name in names)
            for item in dossier
        )
        evidence_ok = chapter_ok or name_in_dossier
        if resolution.verdict == "non_person" and evidence_ok:
            return None
        name_form = resolution.name_form if evidence_ok else "uncertain"
        if resolution.verdict == "person" and evidence_ok:
            return candidate.model_copy(update={
                "personhood": "person", "name_form": name_form,
            })
        return candidate.model_copy(update={
            "personhood": "uncertain", "name_form": name_form,
        })

    judged = await asyncio.gather(*(_judge(item) for item in candidates))
    return [item for item in judged if item is not None]


BIBLE_TRUE_NAME_DOSSIER_SEGMENTS = 12
BIBLE_TRUE_NAME_DOSSIER_BATCHES = 4


def _roster_true_name_dossier_batches(
    names: list[str], chapters_by_idx: dict[int, str],
    *, limit: int = BIBLE_TRUE_NAME_DOSSIER_SEGMENTS,
    batches: int = BIBLE_TRUE_NAME_DOSSIER_BATCHES,
) -> list[list[dict[str, Any]]]:
    """真名裁决的卷宗，切成几批互不重叠的取样。

    姓名往往只在一两章里交代过，一本上千章的书按单批均匀取样几乎必然跳过
    那一章（真实故障：「许师姐→许清」的揭示在第 37 章，八段取样落在 29 和 70
    之间）。分批交错让模型有机会读到跨度里的其它章，读到就停，不必跑满。
    """
    return [
        batch for offset in range(max(1, batches))
        if (batch := _spread_named_segments(
            names, chapters_by_idx, limit=limit, offset=offset,
        ))
    ]


async def _discover_roster_true_names(
    candidates: list[_RosterCandidate],
    chapters: list[dict],
    *,
    project_id: str | None = None,
) -> list[_RosterCandidate]:
    """由模型读原文卷宗决定每个称呼的正式姓名，程序只做检索与钉证。

    点名模型顺手填的 formal_name 也要在这里复核：它可能把身边的物件或半句话
    当成姓名（真实故障：「王腾飞」的真名被写成「这阵法」）。复核对不上就退回
    称呼，宁可没有真名，也不让一个不是名字的串当主名。

    真实事故：许清在第 34 章才以真名出现，前 20 章点名只收到「许师姐」，全文检索
    因此只数到两百次尊称，女主角被标成低频配角。
    """
    chapters_by_idx = _chapters_by_idx(chapters)

    async def _judge_batch(
        candidate: _RosterCandidate, anchors: list[str], dossier: list[dict[str, Any]],
    ) -> _RosterCandidate | None:
        """一批卷宗的裁决：钉证过了就返回带真名的候选，否则 None 让上层换下一批。"""
        appellation = (candidate.primary_appellation or "").strip()
        catalog = "\n\n".join(
            f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
            for item in dossier
        )
        valid_chapters = {int(item["chapter_idx"]) for item in dossier}
        prompt = f"""任务：判断后文是否揭示了「{appellation}」的正式姓名。

原文卷宗：
{catalog}

只根据卷宗原文判断，JSON 字段必须用 verdict：
- revealed：卷宗里出现了这个人的正式姓名，true_name 必须逐字抄自卷宗连续原文。
- unrevealed：卷宗没有揭示正式姓名。
- uncertain：证据不够，不要猜一个名字。

supporting_chapter_index 必须是卷宗里出现过的章号；supporting_quote 必须是该章原文逐字引句。
不得根据常识或作品知识补一个名字。
"""
        try:
            resolution = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX},
                     {"role": "user", "content": prompt}],
                    model_type=_RosterTrueNameResolution,
                    validate=None,
                    normalize_payload=_normalize_roster_verdict_payload,
                    operation_id="character_true_name:" + hashlib.sha256(
                        f"{appellation}:{catalog}".encode("utf-8")
                    ).hexdigest(),
                    temperature=0.0,
                    max_tokens=512,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "人物真名揭示",
                        "stage_key": "character_true_name",
                        "call_role": "stage_validate",
                        "character_name": appellation,
                        "project_id": project_id,
                    }),
                ),
                timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - 这批没跑成就换下一批，别把整个候选判死
            return None
        true_name = (resolution.true_name or "").strip()
        chapter_text = chapters_by_idx.get(resolution.supporting_chapter_index, "")
        quote = (resolution.supporting_quote or "").strip()
        occupied_elsewhere = {
            (item.primary_appellation or "").strip()
            for item in candidates
            if (item.primary_appellation or "").strip()
            and (item.primary_appellation or "").strip() != appellation
        }
        # 钉证的锚点是这个候选的任一已确认称呼，与检索卷宗时用的口径一致：
        # 卷宗段可能来自只写了绰号的那一章，硬要求主名逐字出现会把模型答对的
        # 真名判死（真实故障：「小胖子」的揭示章原文只写「胖子」）。
        if (
            resolution.verdict != "revealed"
            or not true_name
            or true_name in {*anchors, appellation}
            or true_name in occupied_elsewhere
            or resolution.supporting_chapter_index not in valid_chapters
            or true_name not in chapter_text
            or not any(anchor in chapter_text for anchor in anchors)
            or (quote and quote not in chapter_text)
            or (quote and true_name not in quote)
        ):
            return None
        aliases = list(dict.fromkeys([*candidate.aliases, appellation]))
        return candidate.model_copy(update={
            "formal_name": true_name,
            "aliases": [value for value in aliases if value and value != true_name],
            "personhood": "person",
        })

    async def _discover(candidate: _RosterCandidate) -> _RosterCandidate:
        appellation = (candidate.primary_appellation or "").strip()
        if not appellation:
            return candidate
        claimed = (candidate.formal_name or "").strip()
        # 点名申报过真名却一批都没复核过，说明那个串没被原文证明是这个人的姓名。
        unconfirmed = candidate.model_copy(update={"formal_name": ""}) if claimed else candidate
        anchors = [value for value in dict.fromkeys([appellation, *candidate.aliases]) if value]
        batches = _roster_true_name_dossier_batches(anchors, chapters_by_idx)
        for dossier in batches:
            resolved = await _judge_batch(candidate, anchors, dossier)
            if resolved is not None:
                return resolved
        return unconfirmed

    return list(await asyncio.gather(*(_discover(item) for item in candidates)))


def _resolve_conflicting_formal_names(
    candidates: list[_RosterCandidate],
) -> list[_RosterCandidate]:
    """同一个真名被两个候选同时申报时，至少有一个是错的。

    只有主名本身就是这个真名的候选能留住它；一个都没有就两边都清空，
    宁可退回称呼，也不要把两个人并成同一张卡。
    """
    by_formal: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        formal = (candidate.formal_name or "").strip()
        if formal:
            by_formal.setdefault(formal, []).append(index)
    drop: set[int] = set()
    for formal, indices in by_formal.items():
        if len(indices) < 2:
            continue
        keep = {
            index for index in indices
            if (candidates[index].primary_appellation or "").strip() == formal
        }
        drop.update(index for index in indices if index not in keep)
    resolved: list[_RosterCandidate] = []
    for index, candidate in enumerate(candidates):
        if index not in drop:
            resolved.append(candidate)
            continue
        resolved.append(candidate.model_copy(update={"formal_name": ""}))
    return resolved


class _BibleRollCallChunkFailed(StageError):
    """点名分块在退避重试后仍失败：宁可整体失败，也不允许无证据兜底生成人物谱。

    继承 StageError，让 classify() 走内容生成（GEN），而不是 RuntimeError 落到
    系统内部（SYS）。真实故障 ERR-20260827-a2f706：8/20 分块耗尽后界面写成
    「服务器内部错误」。
    """

    def __init__(self, message: str):
        super().__init__("人物点名", [message])


class _BibleSupplement(BaseModel):
    """补录合同：只为「必收名单里还缺的人」补出完整角色条目。"""

    characters: list[Character] = Field(default_factory=list)


def _pick_canonical_display_name(
    appellation: str, formal: str, chapters: list[dict],
) -> tuple[str, list[str]]:
    """选主名：真名与绰号都是同一角色的检索键，只决定「人物谱里显示哪个」。

    真名优先只在真名确实被原文常用、且能和名单称呼同窗共现时成立。真实故障：
    「小胖子」全书出现 152 次、「李富贵」只出现 1 次，机械套用真名优先会把主名
    改成正文里几乎不存在的写法；「靠山老祖 / 白主」则是反过来，0.2 比例把更常用
    的原文称呼挤出 aliases。
    """
    appellation = (appellation or "").strip()
    formal = (formal or "").strip()
    if not formal or formal == appellation:
        return appellation, []
    if _cooccurrence_quote(chapters, appellation, formal) is None:
        return appellation, []
    appellation_hits = sum((ch.get("content") or "").count(appellation) for ch in chapters)
    formal_hits = sum((ch.get("content") or "").count(formal) for ch in chapters)
    if formal_hits >= max(1, appellation_hits * BIBLE_FORMAL_NAME_MIN_RATIO):
        return formal, [appellation]
    return appellation, [formal]


def _cooccurrence_quote(
    chapters: list[dict], left: str, right: str,
) -> tuple[int, str] | None:
    """找一条同时含两个称呼、不超过 80 字的原文引句。"""
    if not left or not right or left == right:
        return None
    for chapter in chapters:
        text = chapter.get("content") or ""
        if left not in text or right not in text:
            continue
        try:
            idx = int(chapter.get("idx"))
        except (TypeError, ValueError):
            continue
        right_positions = [match.start() for match in re.finditer(re.escape(right), text)]
        for start in right_positions:
            window_start = max(0, start - 40)
            quote = text[window_start:window_start + 80]
            if left in quote and right in quote:
                return idx, quote.replace("\n", "")
    return None


def _attach_roster_source_appellations(
    character: Character, entry: _BibleRosterEntry, chapters: list[dict],
) -> None:
    """名单里已经程序绑定的称呼必须能检索到同一张卡，不能等详情模型再报一遍别名。

    真实故障：必收名单已是「李富贵（小胖子）」/ 绑定后的「许师姐→许清」，详情模型
    没把真名写进 aliases，核验闸再一丢，人物谱只剩绰号。
    """
    from app.portraits import IDENTITY_NAME_FORM_REFERENTIAL

    known = {character.name, *(item.text for item in character.aliases if item.text)}
    unverified = set(entry.unverified_appellations)
    for raw in entry.source_appellations:
        text = (raw or "").strip()
        if not text or text in known:
            continue
        # 这条免检通道成立的前提是「这个称呼是名单赖以成立的身份标识」：候选能
        # 进必收名单，靠的就是它，在场证据已经逐条过了结构闸、裁决闸和段号钉证。
        # 点名模型顺手申报的 aliases 没有这层保证，走到这里等于零核验入谱——它们
        # 只能走详情侧那条正规闸（_alias_declaration_verified + 别名裁决）。
        #
        # 真实故障 ERR-20260828-9fcabe（《罗刹海市》EP1）：点名把「大夫」报成主角
        # 马骥的别名，共现闸在「那些士绅大夫争着想开开眼界，便叫村民邀请马骥前去」
        # 这句里同时看到两个词就放行了——可这句话里大夫是发出邀请的人，马骥是被
        # 邀请的人，恰恰是两拨人。「大夫」就此成为马骥的登记称谓，进了
        # reserved_authority_labels；映射台随后正确地把本集朝堂上的众大夫判成
        # functional，撞上「不得冒用已登记身份称谓」，整集失败且重试必然复现。
        if text in unverified:
            continue
        # 词形闸不属于证据强弱问题：一个切碎的短语残片无论共现多少次都不指代任何
        # 人，登记它只会让下游的子串匹配到处误命中。
        if not _alias_text_is_independent_appellation(text):
            continue
        found = None
        for anchor in list(known):
            if not anchor:
                continue
            found = _cooccurrence_quote(chapters, anchor, text)
            if found is not None:
                break
        if found is None:
            continue
        chapter_idx, quote = found
        character.aliases.append(CharacterAlias(
            text=text,
            # 这条别名是程序按共现补回来的，没有模型标注过形态，就不替它下结论。
            name_kind=IDENTITY_NAME_FORM_REFERENTIAL,
            evidence_chapter_index=chapter_idx,
            evidence_quote=quote,
        ))
        known.add(text)


async def _recurring_character_names(
    chapters: list[dict], *, project_id: str | None = None,
) -> list[tuple[str, str, int, int, int, list[str]]]:
    """产出「必收角色名单」：先点名+自报在场证据，再用结构闸+独立裁决闸核验每条
    证据是不是真的证明本人在场，核验通过的证据条数（`verified_onstage_count`）
    才是判据——不再是"名字字符串在原文窗口里出现的次数"。

    根因：旧判据把字符串出现次数当"重不重要"的代理信号，两个方向都会失效。
    假阳性方向——王伯/周员外/靠山老祖的命中全部来自旁白交代身份或他人台词提及，
    本人从未真正在场，却因为次数够多进了必收名单。假阴性方向——这个信号只统计
    模型报出的候选名字本身的出现次数，原文如果通篇用绰号称呼一个人（本人几乎
    只以"小胖子"出现，正式姓名仅出现一两次）而此刻圣经正文还没生成、没有别名表
    能把绰号翻译回正式姓名，这个人就会被判定为不重要。

    新流程：
    1. 模型只在前 BIBLE_HEAD_CHAPTERS 章里点名，每个候选申报 primary_appellation
       （原文最常用写法，允许绰号）+ formal_name（原文已揭示的正式姓名，未揭示则
       空）+ onstage_evidence（能证明本人在场的原文引句列表）——绰号本身就能直接
       充当"必收货币"，不需要一张此刻还不存在的别名表做转译。
    2. 代码结构闸，逐条证据：G1 引句所在章节必须落在统计窗口（前 HEAD+LOOKAHEAD
       章）内；G2 引句必须逐字命中该章原文（允许模型自行加/脱一层引号的噪音）；
       G3 称呼（primary_appellation 或 formal_name 中非空的那个）必须是引句子串。
       任一不满足直接丢弃该条证据，不发起裁决调用。点名也允许申报虽未出场、但原文
       已明确赋予持续剧情作用的具名人物，后续由 mentioned_only 通道独立判断。
    3. 结构闸通过的证据才发起独立低温模型裁决（`_roster_presence_verdict_call`，
       与别名裁决闸同一分工范式：代码检索卷宗 → 模型独立裁决 → 代码结构性钉证）：
       只问这段原文里称呼所指的人物本人是不是真的在场（本人说话/动作/被叙述在场，
       而不是被谈论、被指涉、被交代来历的对象）；裁决通过（verdict=="onstage" 且
       段号钉证通过）才计入该候选的 `verified_onstage_count`。
    4. 按 `verified_onstage_count` 降序（同分按 primary_appellation 字典序打破
       平局）排序，取 >= BIBLE_RECURRING_MIN_ONSTAGE_QUOTES 的候选，最多保留
       BIBLE_MUST_COVER_MAX 个。

    点名调用失败时返回空名单，绝不阻断人物谱本身；结构闸/裁决闸任一步不通过，
    该条证据直接丢弃，不确定不登记（不会因为某一条证据没通过就拒绝整个候选，
    只是那一条不计数）。
    """
    valid = [ch for ch in chapters if (ch.get("content") or "").strip()]
    if not valid:
        return []
    head = valid[:BIBLE_HEAD_CHAPTERS]
    chunks = [
        head[index:index + BIBLE_ROLL_CALL_CHUNK_CHAPTERS]
        for index in range(0, len(head), BIBLE_ROLL_CALL_CHUNK_CHAPTERS)
    ]
    chapters_by_idx = _chapters_by_idx(valid)
    roll_call_sem = asyncio.Semaphore(BIBLE_ROLL_CALL_CONCURRENCY)

    async def _call_chunk(chunk: list[dict], chunk_index: int) -> list[_RosterCandidate]:
        chunk_text = _render_bible_source(
            chunk, budget=BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS,
            head_chapters=len(chunk),
        )
        if not chunk_text.strip():
            return []
        prompt = f"""任务：从下面的小说正文里找出【出场人物】，为每个人物申报能证明他本人真的出现在画面中的证据，不要只给名字。

要求：
1. primary_appellation：本章里称呼这个人物最常用、最稳定的一种写法，可以是正式姓名、外号、绰号、尊称或代称，必须逐字照抄。
2. formal_name：本章已经明确揭示的正式姓名；未揭示就填空字符串，禁止猜测。若“孙天地自称……”或“众人称小胖子李富贵”这种同一人物身份链接出现，必须填 formal_name。
3. aliases：本章明确指向同一人物的其它称呼；只有本章有明确身份链接才填，不能凭外貌相似猜。
4. identity_evidence：证明 formal_name/aliases 与 primary_appellation 是同一人的逐字引句，最多 2 条；引句需同时包含两种称呼或明确自称结构。
5. onstage_evidence：每人最多 {BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE} 条，格式为 chapter_index + quote；quote 必须是本块原文不超过约 80 字的逐字引句，并包含 primary_appellation、formal_name 或 alias。
6. 只有本人说话、行动或被直接叙述为在场才算 onstage_evidence；被谈论、回忆或背景介绍不算。
7. 但具名人物即使当前仅被提及，只要原文明示其建立宗门/制度、造成持续冲突、留下关键规则或后续行动目标，也可输出候选；onstage_evidence 填包含该剧情作用的逐字引句，后续程序会把它判为 mentioned_only，不得伪装成已出场。
8. 只申报可单独指认、能作为定妆对象的人物；同一个人在本块只输出一次。器物与法宝、没有自己姓名的野兽、用「某人的客人」这类描述指代且无法对应到具体人名的路人，都不是人物候选。
9. 本块最多输出 12 个候选，优先输出戏份最重的人物；引句只保留能证明在场的最小片段，不要复述剧情。

小说正文：
{chunk_text}

输出 JSON Schema：
{{"candidates": [{{"primary_appellation": str, "formal_name": str, "aliases": [str], "identity_evidence": [{{"chapter_index": int, "quote": str}}], "onstage_evidence": [{{"chapter_index": int, "quote": str}}]}}]}}"""
        if len(chunk_text) > BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS:
            raise ValueError("人物点名分块输入超过硬上限")
        last_error: Exception | None = None
        for attempt in range(1, BIBLE_ROLL_CALL_MAX_ATTEMPTS + 1):
            try:
                async with roll_call_sem:
                    raw = await asyncio.wait_for(
                        model_gateway.chat(
                            [{"role": "system", "content": SYSTEM_PREFIX},
                             {"role": "user", "content": prompt}],
                            temperature=0.2,
                            max_tokens=BIBLE_ROLL_CALL_CHUNK_MAX_TOKENS,
                            call_meta=_bible_short_json_call_meta({
                                "stage": "人物点名",
                                "stage_key": "character_roll_call",
                                "call_role": "stage_generate",
                                "call_role_label": "人物点名分块",
                                "expected_json": True,
                                "chunk_index": chunk_index,
                                "chunk_count": len(chunks),
                                "input_chars": len(chunk_text),
                                "attempt": attempt,
                            }),
                        ),
                        timeout=BIBLE_ROLL_CALL_TIMEOUT_S,
                    )
                return _CharacterRollCall.model_validate(extract_json(raw)).candidates
            except Exception as exc:  # noqa: BLE001 - 限流/超时退避重试，仍不阻断其它块
                last_error = exc
                if attempt < BIBLE_ROLL_CALL_MAX_ATTEMPTS:
                    await asyncio.sleep(min(20.0, 2.0 * (2 ** (attempt - 1))))
        log_provider_call(
            "character_roll_call", config.MODEL_TEXT, "FAILED", None, 0,
            meta={
                "chunk_index": chunk_index,
                "outcome": "roll_call_chunk_exhausted",
                "attempts": BIBLE_ROLL_CALL_MAX_ATTEMPTS,
                "error": str(last_error)[:300],
            },
        )
        raise _BibleRollCallChunkFailed(
            f"人物点名分块 {chunk_index} 连续 {BIBLE_ROLL_CALL_MAX_ATTEMPTS} 次失败：{last_error}"
        )

    chunk_results = await asyncio.gather(*(
        _call_chunk(chunk, index) for index, chunk in enumerate(chunks)
    ), return_exceptions=True)
    failed_chunks = [item for item in chunk_results if isinstance(item, BaseException)]
    if failed_chunks and len(failed_chunks) == len(chunk_results):
        raise _BibleRollCallChunkFailed(
            f"人物点名全部 {len(chunk_results)} 个分块均失败，拒绝在无原文证据下生成人物谱："
            f"{failed_chunks[0]}"
        )
    if len(failed_chunks) > max(1, len(chunk_results) // 3):
        raise _BibleRollCallChunkFailed(
            f"人物点名失败分块过多（{len(failed_chunks)}/{len(chunk_results)}），"
            f"名单不可信，拒绝继续生成：{failed_chunks[0]}"
        )
    candidates = _merge_roll_call_candidates([
        item for item in chunk_results if not isinstance(item, BaseException)
    ])
    candidates = _pin_roster_candidates_to_source(candidates, chapters_by_idx)
    # 资格裁决先跑：它顺带判出每个称呼是姓名、尊称还是代称，身份归一要靠这个
    # 结论决定谁该被消歧，程序不再用词表预判。
    candidates = await _filter_non_person_roster_candidates(
        candidates, chapters_by_idx, project_id=project_id,
    )
    candidates = await _resolve_generic_character_candidates(
        candidates, chapters_by_idx, project_id=project_id,
    )
    candidates = await _discover_roster_true_names(
        candidates, valid, project_id=project_id,
    )
    candidates = _resolve_conflicting_formal_names(candidates)
    candidates = _merge_roll_call_candidates([[item] for item in candidates])
    candidates = [
        item.model_copy(update={"personhood": "person"})
        if item.personhood != "non_person" and (item.formal_name or "").strip()
        else item
        for item in candidates
    ]

    # 结构闸 G1 用的窗口原文：前 HEAD 章 + 往后 LOOKAHEAD 章，按章节序号建索引
    # （复用 `_chapters_by_idx`，与别名核验同一个查找表构造方式）。
    window_chapters_by_idx = _chapters_by_idx(
        valid[:BIBLE_HEAD_CHAPTERS + BIBLE_LOOKAHEAD_CHAPTERS]
    )
    seen: set[str] = set()
    ambiguous_appellations = _shared_appellations(candidates)
    verified_counts: dict[str, int] = {}
    formal_names: dict[str, str] = {}
    aliases_by_appellation: dict[str, list[str]] = {}
    mention_counts: dict[str, int] = {}
    chapter_counts: dict[str, int] = {}
    personhood_by_appellation: dict[str, str] = {}
    evidence_total = 0
    structural_pass = 0
    # 结构闸（G1-G3）零模型调用、纯同步核对，先把候选证据筛成「值得送裁决闸」的
    # 卷宗清单；裁决闸才是本函数唯一的模型调用，放到下面统一并发发起。
    verdict_jobs: list[tuple[str, list[dict[str, Any]]]] = []
    for candidate in candidates:
        appellation = (candidate.primary_appellation or "").strip()
        formal = (candidate.formal_name or "").strip()
        if not appellation or appellation in seen:
            continue
        seen.add(appellation)
        formal_names[appellation] = formal
        aliases = list(dict.fromkeys(
            value for value in candidate.aliases
            if value and value not in {appellation, formal}
        ))
        if formal and formal != appellation and appellation not in aliases:
            aliases.insert(0, appellation)
        aliases_by_appellation[appellation] = aliases
        personhood_by_appellation[appellation] = candidate.personhood
        search_terms = {value for value in [appellation, formal, *aliases] if value}
        mention_counts[appellation] = sum(
            (chapter.get("content") or "").count(term)
            for chapter in valid for term in search_terms
        )
        chapter_counts[appellation] = sum(
            1 for chapter in valid
            if any(term in (chapter.get("content") or "") for term in search_terms)
        )
        verified_counts[appellation] = 0
        # 防御性兜底：即便模型没听提示词的话报多了，这里也只取前 N 条送进结构闸/
        # 裁决闸，保证下游裁决调用数量有上界，不随模型的自由发挥线性增长。
        evidence_list = candidate.onstage_evidence[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE]
        for evidence in evidence_list:
            evidence_total += 1
            quote = (evidence.quote or "").strip()
            if not quote:
                continue
            # G1：chapter_index 必须落在本轮统计窗口内，防止模型编造窗口外的章号。
            chapter_text = window_chapters_by_idx.get(evidence.chapter_index, "")
            if not chapter_text:
                continue
            # G2：quote 必须是该章原文的逐字子串（允许脱一层配对引号的噪音）。
            if not any(v in chapter_text for v in _quote_comparison_variants(quote)):
                continue
            # G3：任一已绑定称呼都必须能在引句里逐字找到——绰号和真名是同一人的 Mention。
            appellations = [value for value in (appellation, formal, *aliases) if value]
            if not any(value in quote for value in appellations):
                continue
            structural_pass += 1
            dossier = _roster_presence_dossier(evidence.chapter_index, chapter_text, quote)
            if not dossier:
                continue  # no_presence_dossier：不确定不登记，不是跳过检查
            verdict_jobs.append((appellation, dossier))

    # 裁决闸并发发起：每条证据一次独立模型调用，此前是嵌套 for/await 全程串行——
    # 一个出场上千次的主角单独就能把这里拖成几十次排队调用（真实故障：
    # run_8ebe1225aa69，18 条证据串行裁决耗时 91.6s，仍被 900s 总超时拦腰截断）。
    # 这里只是把发起方式从"一条条 await"改成"一起 gather"，真正的并发上限由
    # `model_gateway.chat`→`run_with_provider_call_slot` 那道进程级 `text_provider_calls`
    # 优先级闸门统一节流（见 app/generation_concurrency.py），不额外起一套并发框架。
    # 失败隔离：单条证据裁决失败/不通过只让这一个 job 判 0 票，不影响其它 job，
    # 语义与原来的 continue 完全一致（不确定不登记）；裁决闸本身的提示词/温度/
    # 候选集算法（`_roster_presence_verdict_call`）原样未动。
    verdict_pass = 0
    mentioned_counts: dict[str, int] = defaultdict(int)
    mentioned_dossiers: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # 每条通过裁决的在场证据落在哪一章：通道 A 判「复现」要用它，光数条数分不出
    # 「跨章反复登场的人物」和「在某一章里连说三句话的路人」。章号取被裁决钉住的
    # 那一段，不取整份卷宗——卷宗可能跨章检索，钉证段才是模型真正认定在场的那条。
    verified_chapters: dict[str, set[int]] = defaultdict(set)

    async def _judge_evidence(
        appellation: str, dossier: list[dict[str, Any]],
    ) -> tuple[str, int]:
        try:
            verdict = await _roster_presence_verdict_call(
                appellation=appellation, dossier=dossier, project_id=project_id,
            )
        except Exception as exc:  # noqa: BLE001 - 裁决失败按不确定处理：不确定不登记
            log_provider_call(
                "character_roster_presence_verdict", config.MODEL_TEXT,
                "FAILED", None, 0,
                meta={"appellation": appellation, "error": str(exc)[:300]},
            )
            return "uncertain", -1
        pinned = _alias_verdict_pin_segment(dossier, verdict.supporting_segment_index)
        if pinned is None:
            return "uncertain", -1
        return verdict.verdict, _coerce_roster_chapter_index(pinned.get("chapter_idx"))

    judged = await asyncio.gather(
        *(_judge_evidence(appellation, dossier) for appellation, dossier in verdict_jobs)
    )
    for (appellation, dossier), (verdict, chapter_idx) in zip(
        verdict_jobs, judged, strict=True,
    ):
        if verdict == "onstage":
            verdict_pass += 1
            verified_counts[appellation] += 1
            if chapter_idx > 0:
                verified_chapters[appellation].add(chapter_idx)
        elif verdict == "mentioned_only":
            mentioned_counts[appellation] += 1
            mentioned_dossiers[appellation].extend(dossier)

    mentioned_retain: set[str] = set()

    async def _judge_mentioned_importance(appellation: str) -> tuple[str, bool]:
        dossier = mentioned_dossiers.get(appellation, [])[:6]
        if not dossier or appellation in ambiguous_appellations:
            return appellation, False
        catalog = "\n\n".join(
            f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
            for item in dossier
        )
        prompt = f"""任务：判断仅被提及、尚未真实出场的具名人物「{appellation}」是否应作为未来重要角色保留在人物谱。

原文卷宗：
{catalog}

全文机械信号：称呼/别名命中 {mention_counts.get(appellation, 0)} 次，覆盖 {chapter_counts.get(appellation, 0)} 章。

只有原文明确显示其具备持续剧情作用时才 retain，例如：创建宗门或制度、造成当前核心冲突、留下仍在生效的规则/遗产、被明确设为后续行动目标。仅有家世介绍、欠债对象、路人背景、一次性比较或传闻，一律 drop；证据不足选 uncertain。不得根据常识或作品知识补充。
"""
        try:
            resolution = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX},
                     {"role": "user", "content": prompt}],
                    model_type=_MentionedCharacterImportanceResolution,
                    validate=None,
                    operation_id="mentioned_character_importance:" + hashlib.sha256(
                        f"{appellation}:{catalog}".encode("utf-8")
                    ).hexdigest(),
                    temperature=0.0,
                    max_tokens=384,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "未出场角色重要性裁决",
                        "stage_key": "mentioned_character_importance",
                        "call_role": "stage_validate",
                        "character_name": appellation,
                        "project_id": project_id,
                    }),
                ),
                timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - 不确定不登记
            return appellation, False
        valid_chapters = {int(item["chapter_idx"]) for item in dossier}
        return appellation, (
            resolution.verdict == "retain"
            and resolution.supporting_chapter_index in valid_chapters
        )

    mentioned_jobs = [
        _judge_mentioned_importance(appellation)
        for appellation, count in mentioned_counts.items()
        if count >= 1 and mention_counts.get(appellation, 0) >= 2
    ]
    if mentioned_jobs:
        for appellation, retain in await asyncio.gather(*mentioned_jobs):
            if retain:
                mentioned_retain.add(appellation)

    # 准入分三条独立通道，任一命中即可进入名单：
    # A. 在场证据通道：裁决闸核验通过 >= BIBLE_RECURRING_MIN_ONSTAGE_QUOTES 条，
    #    且这些证据跨到 >= BIBLE_RECURRING_MIN_ONSTAGE_CHAPTERS 个章节
    #    （章数不足的短篇按语料实际章数封顶，见 `_corpus_scoped_chapter_threshold`）；
    # B. 剧情权威通道：仅被提及，但原文赋予其持续剧情作用（mentioned_retain）；
    # C. 全文统计通道：全文命中与章节覆盖同时达标——主角/核心配角在原文里持续出现，
    #    这本身就是比"某一条引句能否通过单次模型裁决"更稳的重要性证据。
    #    真实故障：孟浩前 20 章提及 991 次、覆盖 20/20 章，却因 3 条引句裁决全判
    #    other 被整个淘汰，而只出现 1 次的「李富贵」反被当成主角，人物谱不可用。
    window_size = max(1, len(head))
    # 两条通道的章节门槛都按语料实际章数封顶（见 `_corpus_scoped_chapter_threshold`）：
    # 统计通道数的是全书命中章，用全书章数封顶；在场通道数的是窗口内被钉证的章，
    # 用窗口章数封顶。
    min_statistical_chapters = _corpus_scoped_chapter_threshold(
        max(2, round(window_size * BIBLE_STATISTICAL_MIN_CHAPTER_RATIO)), len(valid),
    )
    statistical_retain = {
        appellation
        for appellation in verified_counts
        # 是不是人由资格裁决说了算，程序不再拿施事动词表去猜。判不出来的候选
        # 走不了统计通道，正确做法是把卷宗做厚让模型判得出，不是绕过它。
        if personhood_by_appellation.get(appellation) == "person"
        and appellation not in ambiguous_appellations
        and mention_counts.get(appellation, 0) >= BIBLE_STATISTICAL_MIN_MENTIONS
        and chapter_counts.get(appellation, 0) >= min_statistical_chapters
    }
    # 通道 A 要的是「复现人物」（本函数名即 recurring），所以在场证据必须跨章：
    # 全部挤在同一章说明这个人在那一章之外没有存在感，而人物谱的作用域是全书。
    # 真实故障：「绿袍男子」——靠山宗那批绿袍修士的类别称谓，不是谁的专名——三条
    # 在场证据全在第 2 章，靠通道 A 建了正式角色卡；它随后被映射器裸命中，把整集
    # 映射卡死在「称谓未逐字出现在本集原文」的反幻觉闸上，且重试必然复现。
    # 漏判不是永久损失：真在某一章挑大梁的角色由分镜阶段的按集新角色发现补建卡。
    # 语料本身只有一章时「跨章」这个维度不存在，门槛退到 1 章，把关交给条数门槛
    # （BIBLE_RECURRING_MIN_ONSTAGE_QUOTES）和资格裁决；此时仍要求至少有一章被
    # 钉证，钉不住章号的证据照旧不计。
    min_onstage_chapters = _corpus_scoped_chapter_threshold(
        BIBLE_RECURRING_MIN_ONSTAGE_CHAPTERS, len(window_chapters_by_idx),
    )
    onstage_recurring = {
        appellation
        for appellation, count in verified_counts.items()
        if count >= BIBLE_RECURRING_MIN_ONSTAGE_QUOTES
        and len(verified_chapters.get(appellation, ())) >= min_onstage_chapters
    }
    ranked = [
        (
            appellation,
            formal_names.get(appellation, ""),
            count,
            mention_counts.get(appellation, 0),
            chapter_counts.get(appellation, 0),
            aliases_by_appellation.get(appellation, []),
        )
        for appellation, count in verified_counts.items()
        if appellation in onstage_recurring
        or appellation in mentioned_retain
        or appellation in statistical_retain
    ]
    # 排序主键换成"全文覆盖广度 + 命中量"，在场证据数退为次级信号：裁决闸对出场
    # 密集的主角反而更容易判 other（引句里叙述多、纯对话少），用它当主键会系统性
    # 地把主角排到配角后面。
    ranked.sort(key=lambda item: (
        0 if item[0] in mentioned_retain and item[2] == 0 else -1,
        -item[4], -item[3], -item[2], item[1] or item[0],
    ))
    result = ranked[:BIBLE_MUST_COVER_MAX]
    # 记账：供人工从数字上判断「这次点名是不是明显偏少/裁决通过率是不是异常低」，
    # 不是核验闸门本身。
    log_provider_call(
        "character_roll_call_coverage", config.MODEL_TEXT, "OK", None, 0,
        meta={
            "candidates": len(candidates),
            "evidence_total": evidence_total,
            "structural_gate_passed": structural_pass,
            "presence_verdict_passed": verdict_pass,
            "must_cover": len(result),
            "fulltext_mentions": sum(item[3] for item in result),
            "personhood_person": sum(
                1 for value in personhood_by_appellation.values() if value == "person"
            ),
            "personhood_deferred": sum(
                1 for value in personhood_by_appellation.values() if value == "uncertain"
            ),
            "true_names_bound": sum(
                1 for appellation, formal in formal_names.items()
                if formal and formal != appellation
            ),
        },
    )
    return result


def _bible_covers_name(bible: Bible, appellations: set[str]) -> bool:
    """必收名单条目是否已经在人物谱里覆盖。`appellations` 是调用方传入的待匹配称呼
    集合（如 `{primary_appellation, formal_name}`，已过滤空值）——传集合而不是单个
    字符串，是因为一个必收条目现在可能同时有原文常用称呼（可以是绰号）和正式姓名
    两种写法，任一种在人物谱里出现都算已覆盖。

    命中条件二选一：
    1. 待匹配称呼中任一项与角色 `character.name` 存在子串关系（原有行为不变，
       允许模型用更完整的正式姓名收录同一人）；
    2. 待匹配称呼中任一项与角色 `character.aliases[].text` **精确相等**（不用
       子串——别名本身已经是核验过的精确称谓，用子串关系反而可能对上不相关的短
       别名，比如单字"老"作为子串命中一堆无关别名；相等判断更安全，且 aliases.text
       本身就是逐字原文称谓，绰号能否被人物谱覆盖就看这一条）。

    `appellations` 为空集合时内层循环天然不执行、直接返回 False（未覆盖）——不是
    因为显式判断"集合为空就跳过检查"而短路，是 `any()`/for 循环对空可迭代对象的
    自然行为，不会误判为已覆盖。
    """
    for character in bible.characters:
        for appellation in appellations:
            if not appellation:
                continue
            if (
                appellation == character.name
                or appellation in character.name
                or character.name in appellation
            ):
                return True
            if any(appellation == alias.text for alias in character.aliases):
                return True
    return False


async def _supplement_bible_characters(bible: Bible, missing: list[tuple[str, str, int]],
                                       chapters_text: str, *,
                                       chapters_by_idx: dict[int, str],
                                       visual_style_prompt: str | None = None,
                                       project_id: str | None = None) -> list[str]:
    """为必收名单里仍然缺席的角色补一次条目；失败或不合格就放弃该角色。

    这一步刻意放在 AgentLoop 之外：人物谱缺角色是质量问题，不该把整个项目
    卡在 bible_status=warning 上（那会连带停掉定妆照与场景库）。

    `missing` 是 `_recurring_character_names` 产出的 (primary_appellation,
    formal_name, verified_onstage_count) 三元组：formal_name 非空时指示模型把它
    用作 character.name、并把 primary_appellation 登记为一条别名（绰号做正式姓名
    的补充记录，而不是丢弃）；formal_name 为空时直接用 primary_appellation 作
    character.name。补录角色新增的 aliases 同样只是模型申报，append 成功后必须
    过与主生成同一套核验（`_verify_character_aliases_for_subset`，只核验本次新增
    的角色，不对已核验过的角色重复发起模型调用）才会真正登记。

    chapters_by_idx：全书原文查找表（`_chapters_by_idx(chapters)`），用于核验模型
    随外观一并申报的 source_evidence（本函数没有 AgentLoop 重试，核验失败的证据
    条目直接从列表里剔除，不拒绝整个角色）与随 name 一并申报的 aliases（核验失败
    的别名条目同样直接剔除，不影响角色本身的补录）。
    """
    from app.refs import (
        PRODUCTION_APPEARANCE_MAX_CHARS,
        PRODUCTION_APPEARANCE_MIN_CHARS,
    )

    expected_names = {(formal_name or appellation) for appellation, formal_name, _ in missing}
    wanted_lines = [
        (
            f'{formal_name}（原文常用称呼"{appellation}"，已核验在场证据 {count} 条）'
            if formal_name else
            f"{appellation}（已核验在场证据 {count} 条）"
        )
        for appellation, formal_name, count in missing
    ]
    wanted = "、".join(wanted_lines)
    style = visual_style_prompt or bible.world.visual_style_canonical
    prompt = f"""任务：为下列【已确认重要但人物谱漏收】的角色补出角色条目，用于 AI 视频生成的一致性控制。

必须补录的角色（name 取值规则见要求 6；不得改写或合并）：
{wanted}

已收录角色（不要重复输出）：{'、'.join(c.name for c in bible.characters) or '无'}
全片统一画风（角色外观必须服从）：{style}

要求：
1. 只输出上面「必须补录」的角色，也不要多输出别人。唯一例外：其中某个名字如果其实不是人物（是宗门、地名或法宝名），跳过它，不要硬编成角色。
2. appearance_canonical 是固定外观锚点串：{PRODUCTION_APPEARANCE_MIN_CHARS}~{PRODUCTION_APPEARANCE_MAX_CHARS} 字，只写常规完整着装、中性站姿下
   可直接看见并能跨镜稳定复现的静态形态；不写性格、情绪、眼神行为，不得写裸体或私密身体
   部位。通用形态（性别年龄感/发型发色/服装款式颜色）原文没写时可按题材合理设定，不需要
   举证；是否再写 1 个标志性特征取决于原文对这个角色本人是否确有描写——有就写且逐字取用
   并在 source_evidence 里举证（evidence_chapter_index + 40 字以内的原文逐字短句，短句里
   要能直接读出是在写这个角色本人，不是同段落里的其他人），没有就不写，不必凑数。
3. role 取"主角|重要配角|反派"之一。
4. speech_style 15~30 字，描述句长习惯/口头禅/敬语习惯。
5. relationships.to 只能指向【已收录角色或本次补录角色】的 name；无法确定就留空数组。
6. name 取值：上面的写法括号里标了"原文常用称呼『XX』"的条目，character.name 用括号外给出的
   正式姓名，并把括号里那个原文常用称呼登记为一条 aliases（text=该称呼，name_kind 按语境判断
   取 personal_name/honorific/referential，evidence_chapter_index + evidence_quote 给一条
   能同时看到这个称呼与这个正式姓名的原文逐字引句，原样照抄不得改写；找不到这种共现就不要
   申报这条别名，不影响角色本身的补录）；没有标注原文常用称呼的条目，character.name 直接用
   给出的那个写法，不需要另外申报别名。

小说文本：
{chapters_text}

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}], "source_evidence": [{{"evidence_chapter_index": int, "evidence_quote": str}}], "aliases": [{{"text": str, "name_kind": "personal_name|honorific|referential", "evidence_chapter_index": int, "evidence_quote": str}}]}}]}}"""
    try:
        raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX},
             {"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=8192,
            call_meta=_bible_short_json_call_meta({
                "stage": "人物谱补录",
                "stage_key": "character_bible_supplement",
                "call_role": "stage_generate",
                "call_role_label": "人物谱补录",
                "expected_json": True,
            }),
        )
        drafted = _BibleSupplement.model_validate(extract_json(raw)).characters
    except Exception as exc:  # noqa: BLE001 - 补录失败保留已有人物谱，不阻断下游
        log_provider_call(
            "character_bible_supplement", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"error": str(exc)[:300]},
        )
        return []
    added: list[str] = []
    added_characters: list[Character] = []
    for character in drafted:
        name = (character.name or "").strip()
        if (
            name not in expected_names
            or _bible_covers_name(bible, {name})
            or not PRODUCTION_APPEARANCE_MIN_CHARS
            <= len(character.appearance_canonical)
            <= PRODUCTION_APPEARANCE_MAX_CHARS
        ):
            continue
        character.name = name
        character.ref_image_path = None
        character.portrait_prompt_override = None
        # 没有 AgentLoop 重试可用：核验失败的证据条目直接剔除（角色照常补录，只是
        # 这条特征失去了申报的举证），不因为一条证据不实就放弃整个角色补录。
        character.source_evidence = [
            evidence for evidence in character.source_evidence
            if _appearance_evidence_verified(
                chapters_by_idx, {character.name},
                evidence.evidence_chapter_index, evidence.evidence_quote,
            )
        ]
        bible.characters.append(character)
        added.append(name)
        added_characters.append(character)
    # 关系只能指向最终名单里的人，否则 validate_bible 会因「关系指向未知角色」退回。
    names = {c.name for c in bible.characters}
    for character in bible.characters:
        character.relationships = [
            relation for relation in character.relationships if relation.to in names
        ]
    # 补录角色声明的 aliases 同样只是申报，必须过与主生成同一套核验才能真正登记——
    # 只对本次新增的角色调用，避免对已核验过的角色重复发起模型调用（难点 C 第 4 点）。
    if added_characters:
        await _verify_character_aliases_for_subset(
            bible, added_characters, chapters_by_idx, project_id=project_id,
        )
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


def _alias_text_is_independent_appellation(text: str) -> bool:
    """别名是否是能独立指代一个人的称呼，而不是从更长短语里切出来的残片。

    结构性判据，不对任何具体称谓做特判：现代汉语里「的」只作结构助词，永远
    后接于修饰语，所以一个以「的」起头的字符串必然是从更长名词短语的中间切
    开的，它自己不指代任何人。判据只认这一个字——「地」「得」虽然也常作助词，
    却能合法起头（「地煞老祖」「得道真人」），纳进来会误伤真称呼。

    真实事故：模型从原文「杂役处的师兄」里截出「的师兄」登记成主角孟浩的别名，
    而那句话说的根本不是他。这类残片进人物谱之后，下游是按子串匹配用它的：
    app/production/prep_pack.py 的群演候选集（``form in source_text``）、认知卡
    的在场判定都会在任何含「……的师兄」字样的章节里命中，把无关角色拉进候选。
    """
    stripped = (text or "").strip()
    return bool(stripped) and stripped[0] != "的"


def _alias_declaration_verified(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    text: str,
    evidence_chapter_index: int,
    evidence_quote: str,
) -> bool:
    """别名申报的代码核验：结构性判据，不对任何具体称谓做特判（禁止黑白名单式修复）。

    四个条件必须同时成立，任一不满足就不登记（不确定不登记是安全默认）：
    1. text 本身是个能独立指代人的称呼，不是从更长短语里切出来的残片——见
       `_alias_text_is_independent_appellation`；
    2. evidence_quote 是 evidence_chapter_index 对应章节原文的逐字子串；
    3. text（申报的别名本身）是 evidence_quote 的子串——证据必须真的提到这个别名，
       不能是一句不相干的话；
    4. 该章节原文里还能找到 anchor_texts（角色规范名或已确认的其它别名）中的至少一项——
       证明这条别名与该角色存在共现依据，不是张冠李戴。

    条件 1、2 都按 `_quote_comparison_variants` 产出的候选引句形式判断（原始引句 /
    脱掉一层配对引号后的内文），同一候选形式需要同时满足两个条件才算命中，避免
    "脱引号让子串关系对不上"这种格式噪音误判为证据不足。
    """
    text = (text or "").strip()
    quote = (evidence_quote or "").strip()
    if not text or not quote:
        return False
    if not _alias_text_is_independent_appellation(text):
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


# ---------- 外观标志性特征证据核验（王有材事故修复，见 logs/appearance_provenance_plan.md）----------
#
# 根因：`appearance_canonical` 生成 prompt 曾同时放"必须包含 1 个标志性特征"的正向配额和
# "原著未描写处按题材合理补全"的兜底授权——对一个原文毫无外貌描写的角色，这个组合逼模型
# 编造，模型选择的解法是"就近取材"，把同场另一个角色的特征安到了这个角色头上（王有材↔
# 小胖子）。修复分两半：prompt 删掉配额（见 generate_bible/_supplement_bible_characters/
# assess_new_character 的规则 2 文案），并新增本节的结构性核验，逐字核对模型申报的证据。
#
# 40 字上限是关键判据：用真实回归数据实测，王有材事故里"把同场角色特征安到王有材头上"
# 唯一可用的原文句子，从"王有材"三字开头到能覆盖那条借来的特征（"较胖"）为止，最短连续
# 引句需要 44 字——40 字上限让"把别人的描写和这个人的名字圈进同一条引文"在物理上不可能。

APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS = 40


def _appearance_evidence_verified(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    evidence_chapter_index: int,
    evidence_quote: str,
) -> bool:
    """标志性特征证据核验：结构性判据，不做任何语义分类（禁止黑白名单式修复）。

    两个条件必须同时成立：
    1. evidence_quote 长度 <= APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS，且是
       evidence_chapter_index 对应章节原文的逐字连续子串（按 `_quote_comparison_variants`
       产出的候选引句形式判断，与别名核验同一套脱引号容错）；
    2. 角色规范名（或调用方传入的其它已确认锚点）出现在这条引句本身内部——不是出现在
       整章的其它位置。这是与 `_alias_declaration_verified` 条件 3（整章共现）的关键
       区别：外观证据要求"名字和描写在同一条不超过 40 字的短引句里"，因为整章共现挡不住
       "同一句里名字属于A、描写属于B"这种跨人借用（王有材事故的实际触发路径）。
    """
    quote = (evidence_quote or "").strip()
    if not quote or len(quote) > APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS:
        return False
    chapter_text = chapters_by_idx.get(evidence_chapter_index, "")
    if not chapter_text:
        return False
    for candidate in _quote_comparison_variants(quote):
        if candidate in chapter_text and any(
            anchor and anchor in candidate for anchor in anchor_texts
        ):
            return True
    return False


def _validate_appearance_evidence(bible: Bible, chapters_by_idx: dict[int, str]) -> list[str]:
    """遍历每个角色的 source_evidence，只对非空条目核验；核验失败才产生 error（驱动
    AgentLoop 的修复重试）。空 source_evidence 数组永远不产生 error——诚实的"这个角色
    没有可举证的标志性特征"是安全默认值，不是缺陷信号，不能让"老实说没有"比"编一个能蒙混
    过关的"更差（那会复刻本次事故的激励结构）。

    锚点只用角色规范名，不用 aliases：本函数在 AgentLoop 校验闭包里对候选 Bible 逐轮调用，
    此时 aliases 只是模型本轮申报、尚未经过 `_verify_character_aliases_in_place` 代码核验，
    用未核验的申报去解锁另一项核验会开一个"自证"漏洞——与 `_verify_character_aliases_in_place`
    自身"只用已验证别名扩大锚点集合"的既有规则一致（不确定不采信）。
    """
    errors: list[str] = []
    for i, character in enumerate(bible.characters):
        anchor_texts = {character.name}
        for j, evidence in enumerate(character.source_evidence):
            if _appearance_evidence_verified(
                chapters_by_idx, anchor_texts,
                evidence.evidence_chapter_index, evidence.evidence_quote,
            ):
                continue
            errors.append(
                f"characters[{i}]({character.name}).source_evidence[{j}] 未通过核验：第 "
                f"{evidence.evidence_chapter_index} 章原文里找不到一条不超过 "
                f"{APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS} 字、且与「{character.name}」同句"
                "出现的逐字引句；请换一条真实可查的原文短引句，或直接去掉这个特征，"
                "appearance_canonical 只写通用形态即可，不必凑数。"
            )
    return errors


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
#
# 取句策略修复（事故：双锚定闸上线后状态事实产出率跌到个位数）：`_alias_declaration_
# verified`/`_find_alias_bridge_chapter` 的共现闸按整章判断（text 与 anchor_texts 之一
# 同时出现在章节任意位置即算通过），但旧版 `_alias_bridge_quote` 取句时只看"分段里有
# 没有 text（对象）"，完全不看主体——真实 dry-run 复现："王腾飞→靠山宗""赵武刚→靠山
# 宗""上官修→靠山宗""韩宗→靠山宗" 四条不同主体的归属申报，桥接检索给出的是同一句
# 关于王腾飞的引文，因为那是全书第一个出现"靠山宗"的分段，与被测主体是谁无关——这句
# 引文随后必然被 `_status_fact_quote_dual_anchor_verified`（第四闸）拒绝，不是这些申报
# 真的都是假的，是取句本身没找对句子。反证：同一条"王腾飞→韩宗"在另一次运行里，恰好
# 检索到一句同时提到两人的原文，就正确通过——同一条事实选对句子就过、选错句子就拒，
# 证明第四闸本身没问题，问题在取句优先级。
#
# 修复：`_alias_bridge_dual_anchor_quote` 在桥接章内优先找"同时包含主体锚点与 text"的
# 分段，`_find_alias_bridge_chapter` 相应优先选"存在这种双锚定分段"的桥接章（找不到
# 双锚定分段/双锚定章时逐级回落到原有行为：分段回落到 `_alias_bridge_quote`——第一个
# 含 text 的分段；章节回落到"第一个满足共现闸的章节"——两者都与修复前逐字节一致，不
# 改变找不到双锚定时的输出）。这不是放宽共现闸：章节/分段是否"合格"仍然只看 text 是否
# 逐字出现（不接受主体/对象的其它别名形式顶替 text 本身），双锚定只是在已经合格的分段
# 里，多一层"优先选哪一个"的偏好，不会让原本拒绝的申报被放行，也不会让原本能找到桥接章
# 的申报反而找不到。别名回填与状态事实回填共用同一套函数（`_alias_bridge_quote`/
# `_find_alias_bridge_chapter`），别名场景里"主体"是角色、"对象"是别名文本，双锚定同样
# 成立——回填 dry-run 对照见变更记录，通过条数与逐条明细未受影响。

_ALIAS_BRIDGE_QUOTE_MAX_CHARS = 200  # 引句长度上限：够定位上下文，不整段搬运


def _alias_bridge_quote(chapter_text: str, text: str) -> str | None:
    """从桥接章原文里确定性截取包含 text 的引句：复用 `index_source_segments`
    做自然段/句级切分（与裁决庭卷宗检索同一工具，已处理引号跨段等边界情况），
    取第一个包含 text 的分段——分段本身就是原文的逐字切片，天然满足
    `_alias_declaration_verified` 条件 1（逐字子串）与条件 2（text 是引句子串）。
    调用方已确认 text 在 chapter_text 里，理论上必有命中；找不到时返回 None
    交由调用方兜底拒绝，不强行拼一句可能不含 text 的引句。

    不看主体——这是 `_find_alias_bridge_chapter` 优先找不到双锚定分段/双锚定章时的
    回落取句（见模块顶部"取句策略修复"说明），行为与该函数改名/加主体优先级之前
    完全一致，不单独调用时不受影响。"""
    for segment in index_source_segments(chapter_text, max_chars=_ALIAS_BRIDGE_QUOTE_MAX_CHARS):
        if text in segment.text:
            return segment.text
    return None


def _alias_bridge_dual_anchor_quote(
    chapter_text: str, text: str, subject_anchor_texts: set[str],
) -> str | None:
    """桥接章内优先检索"同时包含主体与对象"的分段（取句策略修复，见模块顶部"取句
    策略修复"说明）：按段号升序找第一个同时满足 text（对象：别名文本，或归属组织/
    关系对象的申报文本）与 subject_anchor_texts（主体：被测角色规范名或已确认别名）
    中至少一项的分段。全书该章内找不到这样的分段时返回 None，交由调用方回落到
    `_alias_bridge_quote`（不看主体，取第一个含 text 的分段——原有行为，不变）。

    不在这里扩大 text 的匹配范围：text 是否出现在某分段，仍然只按逐字子串判断
    （不接受主体/对象的其它别名形式顶替 text 本身）——这是"桥接章/桥接分段是否合格"
    的共现判据，本次不放宽；本函数只是在"已经含 text"的分段集合里，额外多看一眼这一段
    是否也含主体，改变的只是"多个合格分段里优先选哪一个"，不改变"什么算合格"。"""
    if not subject_anchor_texts:
        return None
    for segment in index_source_segments(chapter_text, max_chars=_ALIAS_BRIDGE_QUOTE_MAX_CHARS):
        if text in segment.text and any(
            anchor and anchor in segment.text for anchor in subject_anchor_texts
        ):
            return segment.text
    return None


def _find_alias_bridge_chapter(
    chapters_by_idx: dict[int, str], anchor_texts: set[str], text: str,
) -> tuple[int, str] | None:
    """桥接章确定性检索：扫描全部章节（不受 ALIAS_BACKFILL_SOURCE_BUDGET_CHARS 预算
    限制——那个预算只约束喂给模型的上下文长度，不该约束代码自己的确定性检索范围），
    按章节序号升序找同时包含 text（申报的别名文本/归属组织/关系对象）与 anchor_texts
    （角色规范名或已确认别名）中至少一项的章节——"是否合格"这条共现判据本身不变。

    取句优先级（两轮扫描，取句策略修复见模块顶部说明）：
    1. 第一轮只看"合格章节中，是否存在同时包含 text 与 anchor_texts 之一的分段"
       （`_alias_bridge_dual_anchor_quote`），按章节序号升序取第一个命中的——不是
       任选一个双锚定章，是"最早出现双锚定证据"，与既有"最早共现即已构成充分证据"
       的确定性选择原则一致；
    2. 全部合格章节都没有这样的分段时，回落到原有行为：按章节序号升序取第一个能
       用 `_alias_bridge_quote` 取到引句（第一个含 text 的分段，不看主体）的合格
       章节——与双锚定优先级引入之前逐字节一致。

    两轮都找不到 → 返回 None，调用方维持拒绝。"""
    qualifying = [
        (idx, chapters_by_idx[idx])
        for idx in sorted(chapters_by_idx)
        if text in chapters_by_idx[idx]
        and any(anchor and anchor in chapters_by_idx[idx] for anchor in anchor_texts)
    ]
    for idx, content in qualifying:
        dual_quote = _alias_bridge_dual_anchor_quote(content, text, anchor_texts)
        if dual_quote:
            return idx, dual_quote
    for idx, content in qualifying:
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
# 三层保底配额（移植自 app/production/prep_pack.py 已用两轮真实生产事故验证过的
# "按层保底配额、保底不受字数预算挤占"方案，见提交 0395a73「候选判别卷宗按层保底
# 配额，杜绝一侧饿死」与 1f15844「卷宗每候选保底不受字数挤占」；缺陷修复见下方
# `_alias_verdict_dossier` docstring"第二个真实回归"一节）：both/text_only/
# anchor_only 三层各自的保底名额，任一层都不能被其它层挤到 0。
#
# 取值 4：不是另起炉灶拍的新数字——prep_pack 那两次修复面对的是同一形状的问题，
# 且单卷宗最多收录条数上限恰好同为 12（`_PREP_PACK_FUNCTIONAL_CANDIDATE_
# DOSSIER_MAX_ENTRIES == _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES == 12`），两轮真实
# 生产事故验证后收敛到的保底值就是 4，这里直接复用同一常量值，不重新调参。3 层
# × 4 = 12，恰好等于 MAX_ENTRIES：三层证据都充足（各自 >= 4 条可用）时保底阶段
# 直接占满预算，谁都不会被挤到 0；某一层可用证据不足 4 条时（如 both，按下方
# docstring 所述通常很少），它的保底天然只取到自己实际拥有的条数，节省下的名额
# 通过下面的 flex 阶段（仍按 both -> text_only -> anchor_only 既有优先级）分给
# 证据更多的层，不需要另写"回收"逻辑。
_ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES = 4
# 保底段的单段截断上限（1f15844 同一根因：条数保底如果仍受字数预算约束，会被排
# 在它前面的长段落吃光字数额度，保底名额有位置却进不了卷宗——1f15844 提交信息
# 原话"保底的是'配额位置'，不是'配额一定进得去卷宗'"）。复用 prep_pack
# `_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS` 同一
# 取值 260：三层保底最多 12 段（3x4=12，即上面注释的最坏情形），12x260=3120，
# 仍明显小于 MAX_CHARS(6000)，保底阶段因此不需要跟 flex 阶段抢字数预算。
_ALIAS_VERDICT_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS = 260
_ALIAS_VERDICT_DOSSIER_TRUNCATION_MARK = "…"


def _alias_verdict_dossier_truncate_segment(text: str, anchor: str) -> str:
    """保底段确定性截断（移植自 prep_pack.py
    `_prep_pack_functional_candidate_truncate_segment`，逻辑不变，仅随宿主函数
    改名）：保底层的段落绝不因为字数超限被整条丢弃——某个层唯一/仅有的证据段
    如果恰好很长（大段环境描写、大段对话），必须截断而不是排除，模型才有机会
    看到它。

    `anchor` 是这段文本之所以入选保底层的具体触发词（调用方按层传入：both/
    text_only 传 `text` 本身——has_text 恒为真，必然能找到；anchor_only 传命中
    的那个具体 anchor 字面串），用来定位"核心句"：先用中文常见句子终止符
    （。！？换行）把 `text` 切成句子，取包含 `anchor` 的那一句；这句本身仍超过
    目标长度时，以 `anchor` 在句中的位置为中心继续裁剪，保证锚点词始终留在
    截断结果里（截掉的是锚点词两侧的上下文，不是锚点词本身）。裁剪掉的一侧加
    省略标记。`anchor` 为空或在 `text` 里根本找不到（防御性：调用方按约定只会
    传入确实命中该段的锚点词，但不假设这个约定一定成立）时退回"从头部截断到
    目标长度"这个更保守的兜底，不做任何"哪句更重要"的语义判断。不针对任何
    具体人名/称谓做特判——`anchor` 完全是调用方传入的字符串参数，本函数只做
    纯字符串定位与切片，是结构操作，不是语义理解。"""
    limit = _ALIAS_VERDICT_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS
    if len(text) <= limit:
        return text
    mark = _ALIAS_VERDICT_DOSSIER_TRUNCATION_MARK
    anchor_pos = text.find(anchor) if anchor else -1
    if anchor_pos < 0:
        return text[:limit].rstrip() + mark
    start, end = 0, len(text)
    for match in re.finditer(r"[。！？\n]", text):
        boundary = match.end()
        if boundary <= anchor_pos:
            start = boundary
        else:
            end = boundary
            break
    if end - start > limit:
        local_pos = anchor_pos - start
        half = max(0, (limit - len(anchor)) // 2)
        crop_start = start + max(0, local_pos - half)
        crop_end = min(end, crop_start + limit)
        crop_start = max(start, crop_end - limit)
        start, end = crop_start, crop_end
    core = text[start:end].strip()
    prefix = mark if start > 0 else ""
    suffix = mark if end < len(text) else ""
    return f"{prefix}{core}{suffix}"


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
    的大段背景描写，反而看不到"孟兄"那句台词紧邻的对话）。排序规则的锚点顺序不变：
    both 优先 → text_only 次之（这两类条数通常很少，一般不会触顶）→ anchor_only
    按"离最近的 both/text_only 段落有多远"升序排列，越靠近别名实际出现的位置越
    优先，距离相同按文档顺序（下标升序）确定性打破平局。

    第二个真实回归（"主角淹没预算"第四次复发——本项目此前已在 prep_pack.py 里
    修过三次同类问题：卷宗整体、候选判别 B 侧内部、B 侧稀缺槽位，见该文件
    0395a73/1f15844 两次提交的完整说明）：`text`（别名场景下的别名本身；状态
    事实场景下调用方传入的是归属对象/关系对象，见 `_status_fact_evidence_
    resolution`）若恰好是章内高频词（结构上与主角名同样"近乎每段都出现"，
    例如某个宗门名反复被提及），text_only 段落数量可能远超 both、也远超
    anchor_only。旧实现"both 全部收录 → text_only 全部收录 → anchor_only 补足
    剩余预算"里，"全部收录"没有上限——text_only 可以在任何 anchor_only 段落被
    考虑之前，独自把 MAX_ENTRIES/MAX_CHARS 吃光。anchor_only 段落正是"这些已
    确认称谓在这一章还出现在哪"的关键证据，一旦被整体挤出卷宗，模型只能在残缺
    材料上判断候选是否与别名指代同一人。

    修复：移植 prep_pack.py 的按层保底配额方案（0395a73）并叠加"保底不受字数
    预算挤占"（1f15844）——both/text_only/anchor_only 三层各自先分到
    `min(_ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES, 该层实际可用条数)` 的保底
    名额，谁都不能被其它层挤到 0；保底层的段落一律直接收录，不因为字数预算
    不够被跳过（1f15844 的核心教训：条数保底如果仍受字数预算约束，排在前面的
    长段落照样能把后面保底段的字数额度吃光，保底"有位置"不等于"进得去卷宗"），
    单段超过 `_ALIAS_VERDICT_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS` 时用
    `_alias_verdict_dossier_truncate_segment` 做确定性截断（保留锚点词所在的
    核心句 + 省略标记），不整段丢弃。三层保底收录完毕后，剩余的"flex"名额才按
    both -> text_only -> anchor_only 既有优先级顺序（anchor_only 仍按上面的
    邻近度排序）继续分配，这部分维持原有语义——只用字数预算约束、不截断（缺了
    不影响"每层至少有保底代表"这个硬要求）。两个既有上限常量（MAX_ENTRIES/
    MAX_CHARS）原样不变，只是同一份预算内部的分配规则更细颗粒度，不是靠放大
    上限绕过问题。

    条数与总字数超过上限后按上述顺序截断，不用随机采样，同一输入任何时候重跑
    都得到同一份卷宗。调用方已确认 `text` 在 `chapter_text` 里，理论上 both/
    text_only 至少有一条命中；真的一条都没有（分段边界极端情况）就返回空列表，
    交由调用方兜底拒绝。"""
    segments = index_source_segments(chapter_text)
    both_indexes: list[int] = []
    text_only_indexes: list[int] = []
    anchor_only_indexes: list[int] = []
    anchor_only_matched_anchor: dict[int, str] = {}
    for index, seg in enumerate(segments):
        has_text = text in seg.text
        matched_anchor = next(
            (anchor for anchor in anchor_texts if anchor and anchor in seg.text), None,
        )
        if has_text and matched_anchor is not None:
            both_indexes.append(index)
        elif has_text:
            text_only_indexes.append(index)
        elif matched_anchor is not None:
            anchor_only_indexes.append(index)
            anchor_only_matched_anchor[index] = matched_anchor
    if not both_indexes and not text_only_indexes:
        return []
    priority_indexes = both_indexes + text_only_indexes
    anchor_only_by_proximity = sorted(
        anchor_only_indexes,
        key=lambda index: (min(abs(index - anchor) for anchor in priority_indexes), index),
    )

    # 按层保底配额 + 保底段免字数预算挤占（移植自 prep_pack.py 0395a73/1f15844，
    # 见本函数 docstring"第二个真实回归"一节）：三层各自先分到不超过自身可用
    # 条数、也不超过 MIN_LAYER_ENTRIES 的保底名额；保底之后剩余的名额（flex）
    # 仍按既有优先级顺序竞争分配。
    reserve_both = min(_ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES, len(both_indexes))
    reserve_text_only = min(_ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES, len(text_only_indexes))
    reserve_anchor_only = min(
        _ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES, len(anchor_only_by_proximity),
    )
    guaranteed_both, overflow_both = both_indexes[:reserve_both], both_indexes[reserve_both:]
    guaranteed_text_only, overflow_text_only = (
        text_only_indexes[:reserve_text_only], text_only_indexes[reserve_text_only:]
    )
    guaranteed_anchor_only, overflow_anchor_only = (
        anchor_only_by_proximity[:reserve_anchor_only],
        anchor_only_by_proximity[reserve_anchor_only:],
    )

    selected: list[int] = []
    used_chars = 0
    resolved_text: dict[int, str] = {}
    # 保底层：both/text_only 以 `text` 本身为截断锚点（has_text 恒为真）；
    # anchor_only 以命中该段的具体 anchor 字面串为截断锚点。一律直接收录，
    # 不做字数预算判断——这正是 1f15844 相对 0395a73 的核心差异。
    for index in guaranteed_both + guaranteed_text_only:
        if len(selected) >= _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES:
            break
        piece = _alias_verdict_dossier_truncate_segment(segments[index].text, text)
        selected.append(index)
        used_chars += len(piece)
        resolved_text[index] = piece
    for index in guaranteed_anchor_only:
        if len(selected) >= _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES:
            break
        anchor_word = anchor_only_matched_anchor.get(index, "")
        piece = _alias_verdict_dossier_truncate_segment(segments[index].text, anchor_word)
        selected.append(index)
        used_chars += len(piece)
        resolved_text[index] = piece
    # flex 层：维持原有语义，按 both -> text_only -> anchor_only 优先级顺序，
    # 仍受字数预算约束（缺了不影响"每层至少有保底代表"这个硬要求）。
    for index in overflow_both + overflow_text_only + overflow_anchor_only:
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
            "text": resolved_text.get(index, segments[index].text),
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
        call_meta=_bible_short_json_call_meta({
            "stage": "别名回填桥接章裁决",
            "stage_key": "character_alias_backfill_verdict",
            "call_role": "stage_generate",
            "call_role_label": "别名桥接章裁决",
            "expected_json": True,
            "project_id": project_id,
            "alias": alias,
            "true_name": true_name,
            "candidates": candidates,
        }),
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


# ---------- A0a. 人物点名在场裁决闸（见 §3 步骤 3，与别名裁决闸同一分工范式：
# 代码检索卷宗 → 低温模型独立裁决 → 代码结构性钉证）----------
#
# 根因回顾（见 `_recurring_character_names` docstring）：旧判据把"名字在原文窗口里
# 出现的次数"当成"这个人是不是重要角色"的代理信号，王伯/周员外/靠山老祖三个反例
# 证明这个信号会整体失效——命中全部来自旁白交代身份或他人台词提及，本人从未
# 真正出现在画面里。结构闸（G1-G3，见 `_recurring_character_names`）只能核验
# "引句确实是原文逐字内容、称呼确实在引句里出现"，核验不了"这句话描述的是不是
# 这个人本人在场"——这是一道开放语义判断，必须像别名裁决闸一样交给独立的低温
# 模型调用，代码只做结构性钉证。


def _roster_presence_dossier(
    chapter_idx: int, chapter_text: str, quote: str,
) -> list[dict[str, Any]]:
    """在场裁决卷宗检索：quote 已经过结构闸核验为该章原文的逐字子串（含
    `_quote_comparison_variants` 允许的脱引号变体），这里用 `index_source_segments`
    定位 quote 落在哪个自然段，连同前后各 1 段一并收录，供裁决闸判断"在场 vs 仅被
    提及"时看到足够上下文——不像别名裁决闸那样需要覆盖全部候选人的按层保底配额
    （这里没有候选竞争，只有一条证据本身要不要被采信），一段 + 前后各 1 段的小卷宗
    足够。quote 跨越自然段边界、或分段规则下找不到任何包含 quote 的自然段（极端
    情况）时返回空列表，交由调用方按 `reason="no_presence_dossier"` 拒绝——不确定
    不登记的安全默认在这里同样成立，不是跳过检查。"""
    segments = index_source_segments(chapter_text)
    variants = _quote_comparison_variants(quote)
    hit_index = next(
        (i for i, seg in enumerate(segments) if any(v in seg.text for v in variants)),
        None,
    )
    if hit_index is None:
        return []
    lo = max(0, hit_index - 1)
    hi = min(len(segments) - 1, hit_index + 1)
    return [
        {"chapter_idx": chapter_idx, "segment_index": i + 1, "text": segments[i].text}
        for i in range(lo, hi + 1)
    ]


_ROSTER_PRESENCE_VERDICT_LABELS = ("onstage", "mentioned_only", "uncertain")


class _RosterPresenceVerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 三态是非题结构本身决定的封闭集合（在场/仅被提及/无法确定），不是从业务数据里
    # 枚举出的名单，schema 层面同样用 enum 收紧（见 `_roster_presence_verdict_call`）。
    verdict: str
    # 钉证判据与别名裁决闸同一范式：模型只需引用卷宗目录里某一条的段号，不要求逐字
    # 复述原文；schema 层面用 enum 收紧到本次卷宗实际收录的段号集合，代码层面复用
    # `_alias_verdict_pin_segment` 再做一次结构性核验。
    supporting_segment_index: int


async def _roster_presence_verdict_call(
    *, appellation: str, dossier: list[dict[str, Any]], project_id: str | None,
) -> _RosterPresenceVerdictResponse:
    """在场裁决：唯一一次独立模型调用，只给卷宗原文，问一道结构相同、措辞不同的
    是非题——不是"称谓 X 指代候选中的谁"（别名裁决闸的候选判别），而是"这段文字里，
    这个称呼指代的人物本人是不是真的置身其中"。这段"在场"语义（本人说话/动作/被
    叙述在场 vs 被提及/回忆/转述/背景交代）与 `app/production/prep_pack.py`
    `_extract_chunk` 的 `segment_indexes` 判据是同一条语义边界，此处只是移植到
    一个新的调用点。"""
    catalog = "\n\n".join(
        f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
        for item in dossier
    )
    segment_indexes = [item["segment_index"] for item in dossier]
    prompt = f"""下面是原著第 {dossier[0]['chapter_idx']} 章中包含称呼"{appellation}"的原文段落
（含前后语境，出现顺序不代表任何推断结论），每段前面标了段号：
{catalog}

任务：仅依据以上原文段落本身，判断称呼"{appellation}"所指代的人物本人，是不是真的出现
在画面中——本人在说话、在行动，或被旁白直接叙述为置身其中，才算在场；如果这段文字里
正在说话、正在行动、被叙述置身其中的是别人，"{appellation}"只是作为被谈论、被指涉、被
交代来历或状态的对象出现在别人的叙述或话语里，即使字面提到了这个称呼，也不算在场。
- verdict 三选一："onstage"（本人确实在场）/ "mentioned_only"（只是被提及、回忆、转述
  或背景交代，本人未真正置身其中）/ "uncertain"（原文本身不足以判断）；证据不够就选
  uncertain，不要为了给出结论而猜测。
- supporting_segment_index 必须填上面某一段落标注的段号（取值只能是 {segment_indexes}
  之一），选你得出这个结论最主要依据的那一段，不要凭空填一个没在目录里出现的段号。
只输出符合 Schema 的 JSON。"""
    operation_id = "character_roster_presence_verdict:" + hashlib.sha256(
        json.dumps(
            {
                "appellation": appellation,
                "dossier": [
                    (item["chapter_idx"], item["segment_index"]) for item in dossier
                ],
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    schema = _RosterPresenceVerdictResponse.model_json_schema()
    schema["properties"]["supporting_segment_index"]["enum"] = segment_indexes
    schema["properties"]["verdict"]["enum"] = list(_ROSTER_PRESENCE_VERDICT_LABELS)
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_RosterPresenceVerdictResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=300,
        # 低温：与 `_alias_verdict_call` 同一理由——这道闸的语义判断要稳定复现，
        # 同一份卷宗重跑不该一次判在场一次判不确定。
        temperature=0.0,
        format_retry_limit=1,
        semantic_retry_limit=1,
        output_schema=schema,
        call_meta=_bible_short_json_call_meta({
            "stage": "人物在场裁决",
            "stage_key": "character_roster_presence_verdict",
            "call_role": "stage_generate",
            "call_role_label": "人物在场裁决",
            "expected_json": True,
            "project_id": project_id,
            "appellation": appellation,
        }),
    )


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


async def _verify_character_aliases_for_subset(
    bible: Bible, characters: list[Character], chapters_by_idx: dict[int, str], *,
    project_id: str | None = None,
) -> dict[str, list[str]]:
    """`_verify_character_aliases_in_place` 的内层循环，抽成可传入显式 `characters`
    子集的辅助函数——供 `_verify_character_aliases_in_place`（传入 `bible.characters`
    全量，行为与抽取前完全一致）与 `_supplement_bible_characters`（补录 append 成功
    后只对本次新增角色调用，不对已核验过的角色重复发起模型调用）共用。

    候选面快照（`roster`，供裁决闸判别"这个称呼指代候选中的谁"）永远取自完整
    `bible.characters`，不受 `characters` 子集影响：核验范围可以只挑几个角色，
    但候选集必须是整本人物谱——否则会重演"真实误登记事故 2"同一形状的问题
    （裁决模型看不到正确候选，只能矮子里拔将军）。只处理 aliases 字段，绝不触碰
    角色的任何其它既有字段。"""
    roster = _alias_verdict_roster(bible)
    added: dict[str, list[str]] = {}

    async def _verify_one(character: Character) -> tuple[str, list[str]]:
        # 同一角色的别名必须串行：后一条要用前面已确认的 anchor_texts。
        # 不同角色互相独立，下面 gather 只并行角色，不并行同一角色内部。
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
        return character.name, added_texts

    for name, added_texts in await asyncio.gather(*(_verify_one(character) for character in characters)):
        if added_texts:
            added[name] = added_texts
    return added


async def _verify_character_aliases_in_place(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, list[str]]:
    """`generate_bible` 主链路核验：模型随人物谱正文一并申报的 aliases 同样只是申报，
    落库前必须过同一套代码核验（`_alias_evidence_resolution`，与回填函数共用；模型
    申报章节没通过共现闸时，退一步做全书桥接章检索，通过共现闸后还要再过一道桥接章
    原文独立裁决，见该函数 docstring）。只处理 aliases 字段，绝不触碰角色的任何其它
    既有字段。核验范围是 `bible.characters` 全量（内层循环见
    `_verify_character_aliases_for_subset`）。"""
    chapters_by_idx = _chapters_by_idx(chapters)
    return await _verify_character_aliases_for_subset(
        bible, bible.characters, chapters_by_idx, project_id=project_id,
    )


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
            call_meta=_bible_short_json_call_meta({
                "stage": "人物别名回填",
                "stage_key": "character_alias_backfill",
                "call_role": "stage_generate",
                "call_role_label": "别名回填",
                "expected_json": True,
                "project_id": project_id,
            }),
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
# 额外新增的核验环节：有效区间起止章（valid_from_chapter/valid_to_chapter）本身也应该
# 有证据支撑，不能让模型随口给一个区间——见 `_status_fact_interval_resolution`。但区间
# 边界与核心事实（角色+归属/关系对象+证据章+引句，已过声明核验/桥接检索+候选判别裁决）
# 是两个独立核验的东西：边界外推没有独立支撑时只回落该边界本身（标注为回落值），不
# 拒绝已核验的核心事实；边界与核心证据矛盾（如终点早于证据章）才整条拒绝。
#
# 另一处事故修复（引句双锚定，见 `_status_fact_quote_dual_anchor_verified`）：首批真实
# 回填产出 6 条，人工复核发现 3 条的 evidence_quote 里没有主体——章级共现闸（判据 3）
# 按整章判断，被登记引用的却只是章内一句，章级通过不等于这一句里真锚定了主体，其中
# 一条（关系事实"王腾飞→韩宗"，引句是韩宗对孟浩说话）是彻头彻尾的假事实，另两条结论
# 为真但引句不合格——不可核验的正确答案与错误答案是同一等级的东西，一律拒绝。这道闸
# 加在核心证据核验之后、候选判别裁决之前，不替换、不削弱既有三闸与候选判别，独立生效。

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
    申报的假设，调用方据此拒绝登记。

    真实事故（proj_3ac0b627fa46 全量回填 22 条申报 0 条通过，误诊为"区间核验过严"，
    追查后发现区间核验从未被触及——全部卡在这一步）：提问措辞早先写成"这段证据所描述
    的{{fact_noun}}（对象：'{{claim_text}}'）实际说的是候选中的哪一位本人"，对关系事实
    （`fact_noun`="人物关系"）是道错题——`claim_text` 此时是 `to`（关系对象，本身就是
    候选集里一个现成的、无歧义的人名），模型据此老老实实回答"'{{claim_text}}'这个名字
    指的就是候选里的{{claim_text}}本人"（如 claim_text="韩宗" → selected_candidate="韩宗"，
    provider_calls 10692/10693/10695 等历史记录可查），但调用方 `_status_fact_evidence_
    resolution` 比对的是 `selected_candidate != subject_name`（subject_name 是关系的
    发起方，如"孟浩"，结构上恒不等于 `to`）——问的是"claim_text 这个词指代谁"，答案
    自然是 claim_text 自己，比对目标却是 subject_name，二者结构性错位，导致人物关系
    100% 必然 candidate_mismatch，与证据是否真实成立无关。现改为明确要求模型回答"谁
    拥有/构成这层{{fact_noun}}"（fact-to-person，与本模块顶部设计注释"归属/关系要问的
    是'与某势力/某人存在这层关系的其实是候选中的谁'"一致），并把 `claim_text` 从候选
    列表里剔除（调用方负责，见 `_status_fact_evidence_resolution`）——它结构上不可能是
    正确答案，留在候选里只会引诱模型选择那个"显而易见"但错误的选项。"""
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

以上段落是"候选中某人与"{claim_text}"存在这层{fact_noun}"这一申报的证据来源。任务：仅
依据原文段落本身，判断真正与"{claim_text}"存在这层{fact_noun}的，实际上是候选中的哪一位
本人。
- selected_candidate 回答的是"拥有/构成这层{fact_noun}的那个人是谁"，不是"'{claim_text}'
  这个名字本身指代候选中的谁"——即使"{claim_text}"恰好也是一个现成的人名，也不能因为
  这一点就直接选它，必须依据原文证据确认候选中"与它存在这层{fact_noun}"的是谁；
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
    object_anchor_texts: set[str],
    resolved_chapter_index: int,
    declared_valid_from_chapter: int | None,
    declared_valid_to_chapter: int | None,
) -> tuple[int, bool, int | None, bool] | None:
    """有效区间起止章核验：不能让模型随口给一个区间（不确定不登记，安全默认同核心
    证据），但"区间边界没独立证据"与"核心事实没证据"是两件不同的事，不能绑在一起
    处理——见下方"拆分处置"说明（事故修复：状态事实回填 100% 拒绝）。

    - 起点/终点若未申报（None）：起点回退为核心证据所在的 `resolved_chapter_index`
      （它已经过声明核验或桥接检索，本身就是有证据支撑的章节）；终点回退为 None，
      代表"尚无证据表明已失效"，与 `character_portraits` 表 `ep_end IS NULL` 的既有
      查询惯例同构（设计文档 §3.2）。
    - 起点/终点若申报了与 `resolved_chapter_index` 不同的章节：该章节必须独立通过
      与核心证据完全同一条闸——`_status_fact_boundary_dual_anchor_verified`（复用
      `_status_fact_quote_dual_anchor_verified` 这条双锚定原语，按自然段而非整章
      判断）。

      缺陷修复：此前这里仍是"claim_text 与 anchor_texts 之一同时出现在章节任意
      位置"的章级共现判据，与十几行外的 `_status_fact_quote_dual_anchor_verified`
      自相矛盾——那条闸就是专门为了堵住章级共现"对主角近乎零过滤力"这一漏洞才加的
      （见其 docstring 引用的真实事故：王腾飞在第27章章级共现 5 次、共现闸判定
      通过，但被登记的引句其实是韩宗对孟浩说话，那句话里根本没有王腾飞）。边界
      判定如果继续用章级共现，同一个漏洞原样留在这里：主体若是主角（几乎每章
      无处不在），边界章又恰好在别处提到了归属对象/关系对象，`valid_from_is_
      fallback=False`（含义是"该边界经过独立核验"）就会被错误地记成 False——而
      该章其实从未在同一段文字里把这条边界与主体真正连接起来。现在边界判定与
      核心证据共用同一条双锚定原语，不再有这个不一致。
    - 边界与核心证据点矛盾（起点晚于 `resolved_chapter_index`、终点早于它）：这是
      自相矛盾——核心证据本身已经证明该事实在 `resolved_chapter_index` 这一章成立，
      不是"外推不足"，返回 None 交由调用方整体拒绝（§4 反例，不可放松）。

    拆分处置（不是放宽标准）：申报的边界与核心证据不矛盾、但也找不到独立双锚定
    支撑（既不等于 `resolved_chapter_index`，边界章又没有任何一段同时锚定主体
    与对象）——这种"未核验的外推"只丢弃该边界本身，不牵连已经过声明核验/候选
    判别的核心事实：对应边界回落为默认值（起点回落为 `resolved_chapter_index`、
    终点回落为 None），并把返回的第 2/4 位标记为 True，供调用方在
    `CharacterAffiliation.valid_from_is_fallback`/`valid_to_is_fallback` 如实标注——
    这是代码回落的默认值，不代表这就是模型申报并核验通过的原始边界。旧实现把这种
    "外推不成立"与"核心矛盾"一视同仁地整条拒绝，等于让未核验的边界外推否决了已核验
    的核心事实，用错了地方（真实项目 proj_3ac0b627fa46 dry-run 复现：22 条申报里这条
    规则从未被实际触发过——回填 100% 拒绝的真正原因在候选判别裁决环节，见
    `_status_fact_verdict_call` docstring；但这条规则本身仍是一处真实的过严设计，
    一旦上游问题修复、更多事实进入这一步，就会开始误杀，因此一并修正）。

    `object_anchor_texts` 与调用方 `_status_fact_evidence_resolution` 核验核心
    证据引句双锚定时用的是同一个集合（归属对象/关系对象的规范名∪已确认别名），
    不是把 `claim_text` 原样传入——边界章里对象出现的具体措辞未必与核心证据章
    完全一致（例如对象本身是某个已确认别名的角色，见该函数关于 `object_anchor_
    texts` 构造方式的说明），双锚定既然要求"同一条原语"，就应当连锚点集合本身
    也保持一致，不能只搬运判据形状、锚点集合却各用各的。
    """
    valid_from_chapter = resolved_chapter_index
    valid_from_is_fallback = False
    if declared_valid_from_chapter is not None and declared_valid_from_chapter != resolved_chapter_index:
        if declared_valid_from_chapter > resolved_chapter_index:
            return None  # 矛盾：起点不能晚于核心证据章，整条拒绝
        boundary_text = chapters_by_idx.get(declared_valid_from_chapter, "")
        if boundary_text and _status_fact_boundary_dual_anchor_verified(
            boundary_text, anchor_texts, object_anchor_texts,
        ):
            valid_from_chapter = declared_valid_from_chapter
        else:
            valid_from_is_fallback = True  # 外推无独立支撑：不采信，回落为核心证据章

    valid_to_chapter: int | None = None
    valid_to_is_fallback = False
    if declared_valid_to_chapter is not None:
        if declared_valid_to_chapter == resolved_chapter_index:
            valid_to_chapter = resolved_chapter_index
        elif declared_valid_to_chapter < resolved_chapter_index:
            return None  # 矛盾：终点不能早于核心证据章，整条拒绝
        else:
            boundary_text = chapters_by_idx.get(declared_valid_to_chapter, "")
            if boundary_text and _status_fact_boundary_dual_anchor_verified(
                boundary_text, anchor_texts, object_anchor_texts,
            ):
                valid_to_chapter = declared_valid_to_chapter
            else:
                valid_to_is_fallback = True  # 外推无独立支撑：不采信，回落为开放终点

    return valid_from_chapter, valid_from_is_fallback, valid_to_chapter, valid_to_is_fallback


def _status_fact_quote_dual_anchor_verified(
    quote: str,
    subject_anchor_texts: set[str],
    object_anchor_texts: set[str],
) -> bool:
    """状态事实引句双锚定核验（事故修复：真实人物谱回填出现的假事实——"王腾飞 同党/
    同门→韩宗"，引用的是"韩宗看都不看其他人一眼，望着孟浩，冷淡开口"这句，句中根本
    没有王腾飞，是韩宗对孟浩说话，与王腾飞无关；另两条"孟浩→靠山宗""许清→靠山宗"引句
    也分别缺主体、只剩三个字的组织名，同一漏洞的三种呈现）。

    根因：`_alias_declaration_verified`/`_find_alias_bridge_chapter` 的"共现"判据是
    按章节整体判断的（claim_text 与 anchor_texts 之一同时出现在该章原文任意位置即算
    通过），但被实际登记、供人工复核的 evidence_quote 只是章节内的一句/一段——章级
    共现通过不代表这一句里真的锚定了主体。没有主体锚点就无法区分"真但证据差"与"假"，
    两者外观完全一致（都是"claim_text 在引句里，主体不在"），所以一律拒绝，不区分
    对待——不可核验的正确答案与错误答案是同一等级的东西。

    条件（归属/关系两类结构相同，调用方按语义传入对应的 subject/object 锚点集合）：
    引句必须同时包含 subject_anchor_texts（主体角色的规范名或已确认别名）中至少一项，
    与 object_anchor_texts（归属对象 org 本身；或关系对象 to 的规范名/已确认别名）中
    至少一项——且必须在同一种引号候选形式（`_quote_comparison_variants`，处理全角/
    半角引号导致的假阴性）下同时命中，不能分别用不同形式各自命中一侧再拼凑。任一侧
    缺失整条拒绝，不尝试放宽或"修补"引句去凑双锚定（不确定不登记，安全默认）。

    这道闸加在核心证据核验（声明核验/桥接检索）之后、候选判别裁决之前——不满足直接
    拒绝，省一次候选判别模型调用；不替换、不削弱既有三闸（章级共现、逐字引句在原文、
    候选判别）与后续段号钉证，只是额外补上"引句本身双锚定"这一层，四闸独立生效。
    """
    quote = (quote or "").strip()
    if not quote:
        return False
    subject_forms = [s for s in subject_anchor_texts if s]
    object_forms = [o for o in object_anchor_texts if o]
    if not subject_forms or not object_forms:
        return False
    for candidate in _quote_comparison_variants(quote):
        if (
            any(s in candidate for s in subject_forms)
            and any(o in candidate for o in object_forms)
        ):
            return True
    return False


def _status_fact_boundary_dual_anchor_verified(
    chapter_text: str,
    subject_anchor_texts: set[str],
    object_anchor_texts: set[str],
) -> bool:
    """区间边界章的双锚定核验（缺陷修复，见 `_status_fact_interval_resolution`
    docstring"边界章级共现"一节）：与核心证据共用同一条原语
    `_status_fact_quote_dual_anchor_verified`，只是这里没有一句现成的
    evidence_quote 可以直接判断，需要先在边界章内部确定性检索出候选"quote"。

    做法：把边界章按 `index_source_segments`（与 `_alias_verdict_dossier` 同一
    分段工具、同一默认粒度，不另起一套分段规则）切成自然段，只要存在至少一段
    本身就同时双锚定通过（引号候选形式下同一形式内同时含主体锚点与对象锚点
    各至少一项），就认为这条边界有独立支撑，返回 True。

    这不是把"整章共现"换成"整章双锚定"（那仍然不够——主体在第一段、对象在
    最后一段，整章拼起来一样能双双命中，跟章级共现是同一个漏洞的另一种写法）：
    双锚定必须发生在同一自然段内，与核心证据的 evidence_quote 是"原文里的
    一句/一段"这一颗粒度完全对齐，不接受跨段拼凑。全书任何一段都不满足时
    返回 False，交由调用方按"拆分处置"回落为 fallback，不牵连核心事实。"""
    for segment in index_source_segments(chapter_text):
        if _status_fact_quote_dual_anchor_verified(
            segment.text, subject_anchor_texts, object_anchor_texts,
        ):
            return True
    return False


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
    "valid_from_chapter": int|None, "valid_from_is_fallback": bool, "valid_to_chapter":
    int|None, "valid_to_is_fallback": bool}`：`accepted=True` 时后面这些字段是应当登记
    的证据与区间——`valid_from_is_fallback`/`valid_to_is_fallback` 为 True 表示对应边界
    是代码回落的默认值（模型申报的边界未能独立核验、不予采信），不是模型申报并核验
    通过的原始边界（见 `_status_fact_interval_resolution` docstring"拆分处置"一节）；
    `accepted=False` 时 `reason` 是机器可读拒绝原因（`no_bridge_chapter`/
    `quote_missing_dual_anchor`/`no_verdict_candidates`/`no_verdict_dossier`/
    `verdict_call_failed`/`candidate_mismatch`/`candidate_uncertain`/
    `segment_not_pinned`/`interval_contradiction`——最后一项特指申报区间与核心证据点
    逻辑矛盾，不包含"边界外推缺乏独立支撑"这种情况，后者现在走拆分处置、不再整条
    拒绝；`quote_missing_dual_anchor` 见 `_status_fact_quote_dual_anchor_verified`
    docstring——章级共现通过不代表被登记的这一句引句里真的锚定了主体）。
    """
    empty = {
        "accepted": False, "chapter_idx": None, "quote": "", "reason": "",
        "valid_from_chapter": None, "valid_from_is_fallback": False,
        "valid_to_chapter": None, "valid_to_is_fallback": False,
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

    # 引句双锚定闸（事故修复，见 `_status_fact_quote_dual_anchor_verified` docstring）：
    # object 锚点复用 `roster`——claim_text 若恰好是某角色的规范名（关系事实的 to 必然
    # 是，因为调用方已核验 `to in known_names`），取其规范名+已确认别名全集；claim_text
    # 不是任何角色规范名时（归属事实的 org，自由文本，没有别名概念），`roster.get` 落空，
    # 回退为 {claim_text} 本身——两种情况用同一行代码表达，不需要按归属/关系分支特判。
    object_anchor_texts = set(roster.get(claim_text, [claim_text]) or [claim_text])
    if not _status_fact_quote_dual_anchor_verified(
        resolved_quote, set(anchor_texts), object_anchor_texts,
    ):
        return {**empty, "reason": "quote_missing_dual_anchor"}

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
    # claim_text 本身若恰好也在候选集里（关系事实的 to 就是这种情况——它本就是候选中
    # 已收录的另一个人），结构上永远不可能是"拥有这层事实的那个人"（`to == name` 早在
    # 调用方过滤掉了自关系）。留在候选列表里会诱导裁决模型把"claim_text 这个名字指代
    # 候选中的谁"（trivial，答案就是它自己）误当成本次要判别的问题，见
    # `_status_fact_verdict_call` docstring 对真实事故的说明。此处剔除，双重保险：
    # 提示词已经明确说明，这里再从 Schema enum 层面彻底堵死这个选项。
    verdict_candidates = [c for c in candidates if c != claim_text]
    try:
        response = await _status_fact_verdict_call(
            fact_noun=fact_noun, claim_text=claim_text, dossier=dossier,
            candidates=verdict_candidates, project_id=project_id,
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
        chapters_by_idx, anchor_texts, object_anchor_texts, resolved_chapter_index,
        declared_valid_from_chapter, declared_valid_to_chapter,
    )
    if interval is None:
        return {**empty, "reason": "interval_contradiction"}
    valid_from_chapter, valid_from_is_fallback, valid_to_chapter, valid_to_is_fallback = interval

    return {
        "accepted": True, "chapter_idx": resolved_chapter_index, "quote": resolved_quote,
        "reason": "", "valid_from_chapter": valid_from_chapter,
        "valid_from_is_fallback": valid_from_is_fallback,
        "valid_to_chapter": valid_to_chapter,
        "valid_to_is_fallback": valid_to_is_fallback,
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
            call_meta=_bible_short_json_call_meta({
                "stage": "人物状态事实回填",
                "stage_key": "character_status_fact_backfill",
                "call_role": "stage_generate",
                "call_role_label": "归属关系回填",
                "expected_json": True,
                "project_id": project_id,
            }),
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
                valid_from_is_fallback=resolved["valid_from_is_fallback"],
                valid_to_chapter=resolved["valid_to_chapter"],
                valid_to_is_fallback=resolved["valid_to_is_fallback"],
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
                valid_from_is_fallback=resolved["valid_from_is_fallback"],
                valid_to_chapter=resolved["valid_to_chapter"],
                valid_to_is_fallback=resolved["valid_to_is_fallback"],
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


def _bible_paratext_scope(valid: list[dict]) -> list[int]:
    """人物谱真正会读到的章（下标落在 `valid` 上）：头部 + 后段抽样 + 必收统计窗口。

    净化按章一次模型调用，范围必须跟「读了什么」对齐，否则代价随书长线性增长
    而收益为零。
    """
    plan = _bible_source_plan(valid, BIBLE_SOURCE_BUDGET_CHARS, BIBLE_HEAD_CHAPTERS)
    scope = {index for index, _, _ in plan}
    head_end = max((index for index, _, excerpt in plan if not excerpt), default=-1) + 1
    # 净化只会让正文变短，头部因此可能比按原文规划时多吃进几章；同时
    # `_recurring_character_names` 的逐字统计窗口是前 HEAD+LOOKAHEAD 章，
    # 必收名单正是被旁文本污染的那一环，这段必须整段净化。
    window = max(
        head_end + BIBLE_PARATEXT_MARGIN_CHAPTERS,
        BIBLE_HEAD_CHAPTERS + BIBLE_LOOKAHEAD_CHAPTERS,
    )
    scope |= set(range(min(window, len(valid))))
    return sorted(index for index in scope if 0 <= index < len(valid))


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

    **2026-08-25 事故（run_8388b4e31301）**：这里原本 `for ch in chapters` 串行
    净化**全书**，一章一次模型调用。643 章的项目在 15 分钟闸门内只跑完 126 次
    （其中 3 次读超时各 152s），人物谱本体一次调用都没轮上，整轮超时作废；
    而这本书人物谱真正读到的只有 33 章，610 次调用是纯浪费。因此这里定死三条：
    只净化 `_bible_paratext_scope` 圈出的章、并发跑、整段封顶
    `BIBLE_PARATEXT_BUDGET_S`。净化本来就是「判不出就退回原文」的净化步骤而不是
    闸门，超时未完成的章原样进入下游，绝不能再把人物谱拖死。

    **2026-08-27（paratext 按章一次、持久化，见
    logs/paratext_single_source_plan.md）**：净化结果现在读/写
    `chapters.paratext_json`，不再每次都直接问模型——首次跑某个项目仍要
    为 scope 内每章各发一次模型调用（跟改造前一样受
    `BIBLE_PARATEXT_BUDGET_S` 封顶），但算完就永久落库；同一项目重新谱写
    人物谱（打回重生、脚本重试）时，这些章大概率命中缓存，`chat 调用数`
    应趋近于零，这一步的墙钟耗时应趋近于"读库"而不是"等模型"。缺
    `id` 的章节（测试用的合成 dict）无法持久化，退化为每次都重算，行为
    与改造前完全一致，不影响正确性。
    """
    from app.source_paratext import chapter_paratext_offsets, remove_offsets

    positions = [i for i, ch in enumerate(chapters) if (ch.get("content") or "").strip()]
    valid = [chapters[i] for i in positions]
    if not valid:
        return chapters
    scope = _bible_paratext_scope(valid)
    conn = get_conn()
    limiter = asyncio.Semaphore(BIBLE_PARATEXT_CONCURRENCY)
    started = time.time()

    async def _clean(slot: int) -> tuple[int, str, bool]:
        chapter = valid[slot]
        async with limiter:
            try:
                regions, cache_hit = await asyncio.wait_for(
                    chapter_paratext_offsets(
                        conn, chapter,
                        operation_id=f"bible.paratext:{chapter.get('id') or positions[slot]}",
                    ),
                    timeout=BIBLE_PARATEXT_CHAPTER_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                return slot, chapter.get("content") or "", False
        stripped = remove_offsets(chapter.get("content") or "", regions)
        return slot, stripped, cache_hit

    tasks = [asyncio.create_task(_clean(slot)) for slot in scope]
    try:
        done, pending = await asyncio.wait(tasks, timeout=BIBLE_PARATEXT_BUDGET_S)
    except BaseException:  # 外层取消/关服：不留悬挂任务
        for task in tasks:
            task.cancel()
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    cleaned = list(chapters)
    changed = 0
    cache_hits = 0
    for task in done:
        if task.cancelled() or task.exception() is not None:
            continue
        slot, stripped, cache_hit = task.result()
        if cache_hit:
            cache_hits += 1
        original = valid[slot]
        if stripped != (original.get("content") or ""):
            cleaned[positions[slot]] = {**original, "content": stripped}
            changed += 1
    log_provider_call(
        "character_bible_paratext", config.MODEL_TEXT,
        "OK", None, int((time.time() - started) * 1000),
        meta={
            "chapters_total": len(valid),
            "chapters_in_scope": len(scope),
            "chapters_stripped": changed,
            "unfinished": len(pending),
            "budget_s": BIBLE_PARATEXT_BUDGET_S,
            # 命中持久化缓存 vs 真正发起模型调用的两类计数（见方案文档
            # "改动清单"一节）：重跑同一项目时前者应趋近 chapters_in_scope，
            # 后者应趋近 0——这两个数字是判断"120s 预算有没有真的降下来"
            # 的直接依据，不用再去 provider_calls 表里数。
            "cache_hits": cache_hits,
            "model_calls": len(done) - cache_hits,
            "degraded_to_original": len(pending),
            "outcome": "best_effort_bypass" if pending else "complete",
        },
    )
    return cleaned


BIBLE_DETAIL_EVIDENCE_MAX_CHARS = 12000
BIBLE_DETAIL_EVIDENCE_MAX_SEGMENTS = 12
BIBLE_ROSTER_INPUT_MAX_CHARS = 16000
BIBLE_DETAIL_TIMEOUT_S = 90.0
BIBLE_DETAIL_MAX_ATTEMPTS = 3
BIBLE_DETAIL_MAX_TOKENS = 4096
# 单角色详情比点名/裁决长，20s 首字会把已发出的成功流误杀；60s 仍切断 0 字节空等。
BIBLE_DETAIL_FIRST_TOKEN_TIMEOUT_S = 60.0

# appearance_canonical 的写作规则。提成常量是为了能被测试直接断言：这段文字
# 的产出会原封不动流进图像与视频模型（定妆照 prompt 由 app/portraits.py 拼接、
# 分镜 prompt 由 app/production/storyboard_pack.py 要求逐字沿用），所以这里
# 每一句都在约束"最终画到画面上的是什么"，不是普通的文风偏好。
#
# 这条规则原本要求模型在看不出性别时写"原文未点明性别"。意图是对的——不许按
# 名字或常识猜性别，猜出来的是编造。但产出位置错了：那句话是写给人看的元话语，
# 却被逐字拼进了图像 prompt。实测三个角色（靠山老祖/陈凡/何洛华）的定妆照
# prompt 因此变成「单角色全身定妆照：原文未点明性别，是靠山宗掌门……」，图像
# 模型只能把这七个字当成要画的内容。
#
# 所以保留"不许猜"的内核，只改"确实看不出时写什么"：不写元话语，改从看得见的
# 特征起笔，让这段描述在缺性别的情况下依然是一幅能照着画的画像。
BIBLE_APPEARANCE_FIELD_RULE = (
    "appearance_canonical 会被逐字送进图像与视频模型当作画面描述，所以整段"
    "从第一个字起就必须是画得出来的东西。关于原文本身的说明（原文有没有写、"
    "证据够不够、能不能确定）不属于画面，写进去图像模型只会把这几个字当成"
    "要画的内容。\n"
    "性别按证据包原文写：代词、身份称谓（师兄/师姐/公子/姑娘这类本身就分"
    "性别的称呼）、他人对话、外貌描写里凡是点明性别的地方都要照原文来。"
    "证据包里确实看不出性别时就不写性别，直接从看得见的特征起笔——年龄观感、"
    "体态、面部特征、气质、发型与随身物——让这段描述照样是一幅能照着画的"
    "画像。任何情况下都不要按名字或常识猜性别。"
)


class _BibleRosterEntry(BaseModel):
    name: str
    role: str
    source_appellations: list[str] = Field(default_factory=list)
    # source_appellations 里哪几项只是点名模型顺口报的别名，没有经过任何核验。
    # 其余各项（primary_appellation / formal_name / 被降级的那个显示名）是这个
    # 候选赖以进入必收名单的身份标识本身——在场证据逐条过了结构闸、独立裁决闸和
    # 段号钉证，名单成立就意味着它们成立。candidate.aliases 没有这层保证：点名
    # 提示词允许模型随手申报，代码一路没有核对过它们指的是不是同一个人。
    # 检索用途（证据包召回、详情提示词的"原文称呼"）照旧吃全集，宽一点无害；
    # 只有"登记进人物谱 aliases"这一步必须把两者分开，见
    # _attach_roster_source_appellations。
    unverified_appellations: list[str] = Field(default_factory=list)
    presence_status: Literal["onstage", "mentioned_only"] = "onstage"
    importance_score: float = 0.0
    importance_signals: list[str] = Field(default_factory=list)
    portrait_eligible: bool = True
    appearance_status: Literal["grounded", "insufficient_evidence", "deferred"] = "grounded"


class _BibleRosterDraft(BaseModel):
    characters: list[_BibleRosterEntry] = Field(default_factory=list)
    world: World


class _CharacterDetail(BaseModel):
    appearance_canonical: str
    period_costume_canonical: str = ""
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)
    aliases: list[CharacterAlias] = Field(default_factory=list)
    source_evidence: list[AppearanceEvidence] = Field(default_factory=list)


def _sanitize_character_detail_payload(payload: dict) -> dict:
    """丢掉缺证据锚点的别名/外观证据，不让整条角色详情校验失败。

    真实事故：孟浩详情三次都因 aliases[].evidence_chapter_index=null 整单作废，
    随后被 `_generate_character_detail_batch` 从名单静默删除，人物谱里没有主角。
    别名合同是「不确定不登记」，缺锚点应丢那一条，不是拒绝这个人。
    """
    data = dict(payload)
    aliases = data.get("aliases")
    if isinstance(aliases, list):
        data["aliases"] = [
            item for item in aliases
            if isinstance(item, dict)
            and item.get("evidence_chapter_index") is not None
            and str(item.get("text") or "").strip()
            and str(item.get("evidence_quote") or "").strip()
        ]
    evidence = data.get("source_evidence")
    if isinstance(evidence, list):
        data["source_evidence"] = [
            item for item in evidence
            if isinstance(item, dict)
            and item.get("evidence_chapter_index") is not None
            and str(item.get("evidence_quote") or "").strip()
        ]
    return data


_CHARACTER_DETAIL_STRING_FIELDS = (
    "appearance_canonical",
    "period_costume_canonical",
    "personality",
    "speech_style",
)


def _repair_character_detail_json(text: str) -> str:
    """Restore a dropped or split key after a completed string field.

    Production (provider_calls 14363 / 孟浩 attempt 2): appearance_canonical closed,
    next key opened, then the model wrote `: "身着...` with the field name missing.
    Insert the next `_CharacterDetail` string field. Key-after-colon splits are
    repaired first so a surviving identifier is not rewritten as a missing key.
    """
    repaired = _repair_json_key_after_colon(text)
    for index, field in enumerate(_CHARACTER_DETAIL_STRING_FIELDS[:-1]):
        nxt = _CHARACTER_DETAIL_STRING_FIELDS[index + 1]
        pattern = rf'("{re.escape(field)}"\s*:\s*"(?:\\.|[^"\\])*")\s*,\s*"\s*:\s*"'
        repaired = re.sub(pattern, rf'\1,\n    "{nxt}": "', repaired, count=1)
    return repaired


def _parse_character_detail_payload(raw: str) -> dict:
    """Extract and sanitize one character-detail object; repair known JSON splits."""
    try:
        payload = extract_json(raw)
    except ValueError:
        repaired = _repair_character_detail_json(raw)
        if repaired == raw:
            raise
        payload = extract_json(repaired)
    return _sanitize_character_detail_payload(payload)


def _character_stub_from_roster(entry: _BibleRosterEntry) -> Character:
    """名单已锁定时，详情失败也要留下这个人，外观留空待补，不编造长相。"""
    return Character(
        name=entry.name,
        role=entry.role,
        appearance_canonical="外观待补全，详情生成未通过校验，当前不自动定妆",
        personality="",
        speech_style="",
        relationships=[],
        aliases=[],
        source_evidence=[],
        presence_status=entry.presence_status,
        importance_score=entry.importance_score,
        importance_signals=entry.importance_signals,
        portrait_eligible=False,
        appearance_status="insufficient_evidence",
        period_costume_canonical="待详情通过后再依据年代与身份补全",
    )


def _character_detail_evidence_pack(
    chapters: list[dict], appellations: list[str], *, max_chars: int = BIBLE_DETAIL_EVIDENCE_MAX_CHARS,
) -> str:
    """给一个角色检索有界的原文卷宗：命中章跨全书取样，不是只取最靠前的几段。

    只取最前面的命中段，模型看到的会全是这个人早期的固定称呼；真名揭示、性别
    交代、外貌描写往往在后文，取不到就只能靠猜。这里只负责把上下文找齐，
    外貌和性别怎么写全部由模型依据这些原文决定。
    """
    anchors = [value.strip() for value in appellations if value and value.strip()]
    selected: list[str] = []
    if anchors:
        chapters_by_idx: dict[int, str] = {}
        for chapter in chapters:
            content = (chapter.get("content") or "").strip()
            if not content:
                continue
            try:
                chapters_by_idx[int(chapter.get("idx"))] = content
            except (TypeError, ValueError):
                continue
        for item in _spread_named_segments(
            anchors, chapters_by_idx,
            limit=BIBLE_DETAIL_EVIDENCE_MAX_SEGMENTS, segment_max_chars=800,
        ):
            block = f"【第{item['chapter_idx']}章·证据】\n{item['text'].strip()}"
            if sum(len(value) for value in selected) + len(block) > max_chars:
                break
            selected.append(block)
    if selected:
        return "\n\n".join(selected)
    # No lexical hit: bounded fallback only, never the 60K source corpus.
    for chapter in chapters[:3]:
        content = (chapter.get("content") or "").strip()
        if not content:
            continue
        block = f"【第{chapter.get('idx', '?')}章·有限背景】\n{content[:1200]}"
        if sum(len(item) for item in selected) + len(block) > max_chars:
            break
        selected.append(block)
    return "\n\n".join(selected)


def _normalize_must_cover_rows(
    rows: list[tuple],
) -> list[tuple[str, str, int, int, int, list[str]]]:
    """兼容旧三元组调用方；生产路径使用带全文统计与别名的六元组。"""
    normalized: list[tuple[str, str, int, int, int, list[str]]] = []
    for row in rows:
        if len(row) >= 6:
            appellation, formal, onstage, mentions, chapters, aliases = row[:6]
        else:
            appellation, formal, onstage = row[:3]
            mentions, chapters, aliases = onstage, 1, []
        normalized.append((
            str(appellation), str(formal), int(onstage), int(mentions), int(chapters),
            [str(alias) for alias in aliases if str(alias).strip()],
        ))
    return normalized


def _character_importance_metadata(
    onstage: int, mentions: int, chapters: int,
) -> tuple[float, list[str]]:
    """把独立 Harness 信号压成可解释分数；准入仍由证据门禁决定，不由分数单独决定。"""
    score = min(100.0, onstage * 22.0 + min(chapters, 12) * 4.0 + min(mentions, 30) * 0.8)
    signals = [f"verified_onstage:{onstage}", f"fulltext_mentions:{mentions}", f"chapter_coverage:{chapters}"]
    return round(score, 1), signals


def _normalize_roster_against_candidates(
    draft: _BibleRosterDraft,
    must_cover: list[tuple[str, str, int, int, int, list[str]]],
    chapters: list[dict] | None = None,
) -> _BibleRosterDraft:
    """代码拥有名单最终权：模型只分配 role，不得拆人、改名或漏人。"""
    if not must_cover:
        return draft
    model_entries = list(draft.characters)
    normalized: list[_BibleRosterEntry] = []
    for appellation, formal, onstage, mentions, chapters_hit, aliases in must_cover:
        if chapters:
            canonical, demoted = _pick_canonical_display_name(appellation, formal, chapters)
        else:
            canonical, demoted = (formal or appellation), ([appellation] if formal and formal != appellation else [])
        # 真名与绰号都留在检索键里：下游遇到任一称呼都要能映射回这一个角色的定妆图。
        source_names = [
            name for name in dict.fromkeys([appellation, formal, *demoted, *aliases])
            if name and name != canonical
        ]
        all_names = {canonical, appellation, formal, *aliases} - {""}
        matched = next((
            item for item in model_entries
            if item.name in all_names or bool(set(item.source_appellations) & all_names)
        ), None)
        score, signals = _character_importance_metadata(onstage, mentions, chapters_hit)
        # onstage=0 只说明"没有一条引句通过单次模型裁决"，不等于这个人没出场。
        # 全文命中量达标时按已出场处理：主角引句叙述密集，裁决闸判 other 的概率
        # 反而更高，若据此判成 mentioned_only，主角和高频配角会被踢出定妆。
        # 章节覆盖率只用于点名窗口准入，不得拿全书章数做分母——1616 章小说的
        # 0.15 会要求覆盖 242 章，王腾飞这种反派会被标成仅提及、不给定妆。
        statistically_present = mentions >= BIBLE_STATISTICAL_MIN_MENTIONS
        mentioned_only = onstage == 0 and not statistically_present
        if onstage == 0 and statistically_present:
            signals = signals + ["presence_by_fulltext_coverage"]
        normalized.append(_BibleRosterEntry(
            name=canonical,
            role=(matched.role if matched else ("关键伏笔角色" if mentioned_only else "重要配角")),
            source_appellations=source_names,
            unverified_appellations=[
                name for name in source_names
                if name in set(aliases) and name not in {appellation, formal, *demoted}
            ],
            presence_status="mentioned_only" if mentioned_only else "onstage",
            importance_score=score,
            importance_signals=signals + (["retained_by_plot_authority"] if mentioned_only else []),
            portrait_eligible=True,
            appearance_status="grounded",
        ))
    draft.characters = normalized
    _assign_protagonist_by_signals(draft)
    return draft


def _assign_protagonist_by_signals(draft: _BibleRosterDraft) -> None:
    """主角由统计信号确定性指派，不交给模型自由发挥。

    must_cover 已按「章节覆盖 → 全文命中」降序排好，排在最前且真实出场的角色就是
    全书出现最广、最密的人物。真实故障：模型把只出现 1 次的「李富贵」标成主角，
    而覆盖 20/20 章、提及 991 次的孟浩连名单都没进。这里在名单定稿后统一改写 role，
    保证有且只有一个主角，且主角必然是统计上最核心的那个已出场角色。
    """
    onstage = [item for item in draft.characters if item.presence_status == "onstage"]
    if not onstage:
        return
    protagonist = onstage[0]
    for item in draft.characters:
        if item is protagonist:
            item.role = "主角"
        elif item.role == "主角":
            item.role = "重要配角"


def _validate_bible_roster(draft: _BibleRosterDraft) -> list[str]:
    names = [(item.name or "").strip() for item in draft.characters]
    errors: list[str] = []
    if not 1 <= len(names) <= 20:
        errors.append(f"characters 数量 {len(names)}，要求 1~20 个")
    if any(not name for name in names):
        errors.append("characters.name 不能为空")
    if len(names) != len(set(names)):
        errors.append("characters.name 存在重复")
    if getattr(draft.world, "visual_style_canonical", None) is None:
        errors.append("world.visual_style_canonical 缺失")
    return errors


async def _generate_character_detail(
    entry: _BibleRosterEntry,
    *,
    roster_names: list[str],
    evidence_pack: str,
    style: str,
    era: str = "",
    chapters_by_idx: dict[int, str],
    project_id: str | None,
) -> Character | None:
    from app.refs import PRODUCTION_APPEARANCE_MAX_CHARS, PRODUCTION_APPEARANCE_MIN_CHARS

    base_pack = evidence_pack[:BIBLE_DETAIL_EVIDENCE_MAX_CHARS]
    last_error = ""
    for attempt in range(1, BIBLE_DETAIL_MAX_ATTEMPTS + 1):
        pack = base_pack if attempt == 1 else base_pack[: max(2000, len(base_pack) // 2)]
        prompt = f"""任务：只为一个已确认角色生成角色详情。角色名字与角色类型已经由上游锁定，不得更改。
目标角色：{entry.name}
角色类型：{entry.role}
原文称呼：{'、'.join(entry.source_appellations) or entry.name}
完整角色名单（relationships.to 只能从这里选择）：{'、'.join(roster_names)}
统一画风：{style}
世界年代/社会形态：{era or '原文未明确，必须从证据包的社会制度、材质和服装称谓保守判断'}

{BIBLE_APPEARANCE_FIELD_RULE}

要求：appearance_canonical {PRODUCTION_APPEARANCE_MIN_CHARS}~{PRODUCTION_APPEARANCE_MAX_CHARS} 字；period_costume_canonical 20~60 字，明确该年代、地域/宗门、身份层级下可用的服装形制、面料、鞋履、束发与禁用的现代/错代元素，并与原文直接服装描写一致；speech_style 15~30 字；只写该角色；不确定的关系、别名、标志性特征证据留空。source_evidence 引句必须不超过 40 字且逐字来自证据包。

该角色的小证据包（不是全书）：
{pack}

输出 JSON Schema：
{{"appearance_canonical": str, "period_costume_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}], "aliases": [{{"text": str, "name_kind": str, "evidence_chapter_index": int, "evidence_quote": str}}], "source_evidence": [{{"evidence_chapter_index": int, "evidence_quote": str}}]}}"""
        started = time.time()
        try:
            raw = await asyncio.wait_for(
                model_gateway.chat(
                    [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": prompt}],
                    temperature=0.35 if attempt == 1 else 0.15,
                    max_tokens=BIBLE_DETAIL_MAX_TOKENS,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "角色详情生成",
                        "stage_key": "character_bible_detail",
                        "call_role": "stage_generate" if attempt == 1 else "stage_repair",
                        "call_role_label": "单角色详情",
                        "expected_json": True,
                        "character_name": entry.name,
                        "attempt": attempt,
                        "input_chars": len(pack),
                        "project_id": project_id,
                        "first_token_timeout_s": BIBLE_DETAIL_FIRST_TOKEN_TIMEOUT_S,
                    }),
                ),
                timeout=BIBLE_DETAIL_TIMEOUT_S,
            )
            detail = _CharacterDetail.model_validate(
                _parse_character_detail_payload(raw)
            )
            character = Character(
                name=entry.name,
                role=entry.role,
                appearance_canonical=detail.appearance_canonical,
                personality=detail.personality,
                speech_style=detail.speech_style,
                relationships=detail.relationships,
                aliases=detail.aliases,
                source_evidence=detail.source_evidence,
                presence_status=entry.presence_status,
                importance_score=entry.importance_score,
                importance_signals=entry.importance_signals,
                portrait_eligible=True,
                appearance_status="grounded",
                period_costume_canonical=detail.period_costume_canonical,
            )
            if not PRODUCTION_APPEARANCE_MIN_CHARS <= len(character.appearance_canonical) <= PRODUCTION_APPEARANCE_MAX_CHARS:
                raise ValueError("appearance_canonical 长度越界")
            if not 20 <= len(character.period_costume_canonical) <= 60:
                raise ValueError("period_costume_canonical 长度越界")
            character.relationships = [item for item in character.relationships if item.to in roster_names]
            character.source_evidence = [
                item for item in character.source_evidence
                if _appearance_evidence_verified(
                    chapters_by_idx, {entry.name}, item.evidence_chapter_index, item.evidence_quote,
                )
            ]
            log_provider_call(
                "character_bible_detail", config.MODEL_TEXT, "OK", None,
                int((time.time() - started) * 1000),
                meta={"character_name": entry.name, "attempt": attempt, "input_chars": len(pack)},
            )
            return character
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - only this character retries
            last_error = str(exc)
            log_provider_call(
                "character_bible_detail", config.MODEL_TEXT,
                "TIMEOUT" if isinstance(exc, TimeoutError) else "FAILED", None,
                int((time.time() - started) * 1000),
                meta={"character_name": entry.name, "attempt": attempt, "input_chars": len(pack), "error": last_error[:300]},
            )
    return None


async def _generate_character_detail_batch(
    entries: list[_BibleRosterEntry], chapters: list[dict], *, style: str, era: str = "",
    chapters_by_idx: dict[int, str], project_id: str | None,
) -> list[Character]:
    roster_names = [entry.name for entry in entries]
    tasks = [asyncio.create_task(_generate_character_detail(
        entry,
        roster_names=roster_names,
        evidence_pack=_character_detail_evidence_pack(
            chapters, [entry.name, *entry.source_appellations]
        ),
        style=style,
        era=era,
        chapters_by_idx=chapters_by_idx,
        project_id=project_id,
    )) for entry in entries]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    characters: list[Character] = []
    for entry, result in zip(entries, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException) or result is None:
            # 名单已锁定。详情失败留下占位，禁止把主角/配角从人物谱抹掉。
            characters.append(_character_stub_from_roster(entry))
            continue
        characters.append(result)
    return characters


async def generate_bible(chapters: list[dict], feedback: str = "", previous_bible: dict | None = None,
                         project_id: str | None = None,
                         visual_style_prompt: str | None = None) -> Bible:
    """Generate a small roster first, then fan out bounded per-character requests."""
    chapters = await _chapters_without_paratext(chapters)
    chapters_by_idx = _chapters_by_idx(chapters)
    must_cover = _normalize_must_cover_rows(
        await _recurring_character_names(chapters, project_id=project_id)
    )
    if not must_cover:
        raise StageError(
            "角色圣经",
            ["人物点名未产出任何经原文核验的角色候选，拒绝在无证据情况下编造人物谱"],
            exit_reason="empty_verified_roster",
        )

    must_cover_lines = [
        (
            f"{formal or appellation}（原文称呼：{'、'.join(dict.fromkeys([appellation, *aliases]))}；"
            f"核验在场 {onstage_count} 次；全文命中 {mention_count} 次；覆盖 {chapter_count} 章）"
        )
        for appellation, formal, onstage_count, mention_count, chapter_count, aliases in must_cover
    ]
    previous_names = [
        item.get("name", "") for item in (previous_bible or {}).get("characters", [])
        if item.get("name")
    ]
    forced_style = visual_style_prompt or ""
    roster_context = "\n".join(f"- {line}" for line in must_cover_lines) or "- 暂无已核验候选"
    roster_prompt = f"""任务：根据已经完成代码归并和在场核验的候选摘要，只确定人物谱最终角色名单、角色类型和世界观；不要生成外观、性格、台词风格、关系或证据。

已核验候选摘要：
{roster_context}

规则：
1. 候选摘要来自前 20 章单章点名、身份归一、在场核验与全文检索；不得新增摘要中没有的人物，总数不超过 20。
2. 所有候选都必须收录；role 只负责区分主次，不得删除低频但已核验在场的候选。全文命中/覆盖章节用于判断重要程度，在场证据用于判断是否真实出场，二者不能互相替代。
3. name 必须使用括号外的正式姓名；若括号外仍是描述性称呼，说明全文尚未揭示真名，才可暂用该称呼。source_appellations 必须完整收录括号内原文称呼。
4. 同一候选行内的正式姓名、绰号、描述性称呼属于同一人物，严禁拆成多个角色。
5. 用户反馈：{feedback.strip() or '无'}。
6. 历史人物谱角色仅供返工对照，不得绕过候选摘要新增人物：{'、'.join(previous_names) or '无'}。
7. visual_style_canonical：{forced_style or '按古典修仙题材生成 25~40 字的统一 CG/动画/漫画/插画画风'}。
8. era 与 genre 只写简短题材标签；不得复述小说内容。

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "source_appellations": [str]}}], "world": {{"era": str, "genre": str, "visual_style_canonical": str}}}}"""
    roster_loop = AgentLoop(
        stage_key="character_bible_roster",
        contract_key="character_bible_roster",
        goal="确定人物谱角色名单与统一世界观",
        scope_type="project",
        scope_id=project_id or hashlib.sha256(roster_context.encode("utf-8")).hexdigest()[:16],
        artifact_type="character_bible_roster",
        policy=AgentLoopPolicy(
            max_iterations=2,
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=1,
            allow_warning_candidate=False,
            repair_all_blockers=True,
        ),
    )

    def validate_roster(candidate: _BibleRosterDraft) -> list[str]:
        _normalize_roster_against_candidates(candidate, must_cover, chapters)
        if visual_style_prompt:
            candidate.world.visual_style_canonical = visual_style_prompt
        return _validate_bible_roster(candidate)

    roster = await _run_with_agent_loop(
        "人物名单", "character_bible_roster", roster_prompt, _BibleRosterDraft,
        validate_roster, loop=roster_loop, temperature=0.3, max_tokens=4096,
        repair_user_prompt_limit=16000, repair_candidate_limit=5000,
    )
    _normalize_roster_against_candidates(roster, must_cover, chapters)
    if visual_style_prompt:
        roster.world.visual_style_canonical = visual_style_prompt
    style = roster.world.visual_style_canonical
    characters = await _generate_character_detail_batch(
        roster.characters,
        chapters,
        style=style,
        era=roster.world.era,
        chapters_by_idx=chapters_by_idx,
        project_id=project_id,
    )
    bible = Bible(world=roster.world, characters=characters)
    await _verify_character_aliases_in_place(bible, chapters, project_id=project_id)
    for character in bible.characters:
        entry = next((item for item in roster.characters if item.name == character.name), None)
        if entry is not None:
            _attach_roster_source_appellations(character, entry, chapters)

    missing = [
        item for item in must_cover
        if not _bible_covers_name(
            bible, {value for value in (item[0], item[1], *item[5]) if value}
        )
    ]
    if missing:
        # Reuse the same single-character primitive; no batch/full-source supplement request.
        existing_names = {character.name for character in bible.characters}
        entries = [
            _BibleRosterEntry(
                name=formal or appellation,
                role="重要配角",
                source_appellations=list(dict.fromkeys([appellation, *aliases])),
                # 跟主路径同一条线：appellation 是这个候选进名单的身份标识，
                # 点名申报的 aliases 没核验过，不能走免检通道入谱。
                unverified_appellations=[
                    name for name in dict.fromkeys(aliases) if name != appellation
                ],
            )
            for appellation, formal, _onstage, _mentions, _chapters, aliases in missing
            if (formal or appellation) not in existing_names
        ]
        supplemented = await _generate_character_detail_batch(
            entries,
            chapters,
            style=style,
            era=roster.world.era,
            chapters_by_idx=chapters_by_idx,
            project_id=project_id,
        )
        bible.characters.extend(supplemented)
        if supplemented:
            await _verify_character_aliases_for_subset(
                bible, supplemented, chapters_by_idx, project_id=project_id,
            )
            for character, entry in zip(supplemented, entries, strict=False):
                _attach_roster_source_appellations(character, entry, chapters)

    valid_names = {character.name for character in bible.characters}
    for character in bible.characters:
        character.relationships = [
            relation for relation in character.relationships if relation.to in valid_names
        ]
    errors = validate_bible(bible) + _validate_appearance_evidence(bible, chapters_by_idx)
    if errors:
        raise StageError("角色圣经", errors, exit_reason="local_detail_blockers")
    return bible


# ---------- A2. 场景圣经（场景图素材库的规范场景，跨集场景一致性核心） ----------

class _SceneBibleDraft(BaseModel):
    """场景圣经输出合同（仅生成期使用）：一组规范场景。"""

    scenes: list[Scene]


async def generate_scene_bible(chapters: list[dict], bible: Bible,
                               feedback: str = "", project_id: str | None = None) -> list[Scene]:
    """从原文提取「规范场景」清单，作为场景图素材库的底稿（与 generate_bible 同构）。
    每个场景给 name（稳定短标签）+ scene_canonical（固定场景锚点串，画风约束与人物锚点一致，
    按 bible.world.visual_style_canonical 是否为照片级真人摄影预设二选一：非摄影风格必须
    CG/动画/漫画类非真人风格，否则后续 Seedance/Seedream 易因疑似真人报错；摄影风格则相反，
    要求真实材质与摄影级细节）。"""
    from app.refs import SCENE_CANONICAL_MAX_CHARS, SCENE_CANONICAL_MIN_CHARS
    from app.visual_styles import is_photographic_style_prompt
    chapters_text = _render_bible_source(chapters)
    style = bible.world.visual_style_canonical
    genre = bible.world.genre or ""
    feedback_part = ""
    if feedback.strip():
        feedback_part = f"\n人工打回重生要求（最高优先级）：\n{feedback.strip()}\n"
    if is_photographic_style_prompt(style):
        scene_style_rule = (
            f'4. 【硬性约束】scene_canonical 必须贴合全片画风「{style}」，是照片级摄影质感的'
            "实景环境描述，允许并鼓励真实材质、自然光影与摄影级细节；场景本身仍是虚构地点，"
            "不指向可识别的真实地标、真实机构或真实商业品牌名称。"
        )
    else:
        scene_style_rule = (
            f'4. 【硬性约束】scene_canonical 必须贴合全片画风「{style}」，是 CG/动画/漫画/插画类的'
            '非真人渲染场景（写实质感氛围词可保留），严禁"真人实拍/实景照片/摄影棚实拍"这类描述'
            "（否则后续图像/视频接口会因疑似真人实景报错）。"
        )
    prompt = f"""任务：从小说文本中提取【规范场景清单】，用于后续 AI 视频生成的场景一致性控制（场景图素材库）。

全片画风（场景锚点必须与之一致）：{style}
题材：{genre or '（未标注）'}

要求：
1. 只收录【反复出现 / 有戏份 / 画面感强】的关键场景（如主角居所、宗门广场、夜晚密林、朝堂等），最多 12 个；一次性出现的过场地点不要收录。
2. name：稳定的场景短标签（4~10 字，如"宗门广场""破败客栈内"），后续所有分镜的场景都收敛到这些名字，便于跨集复用同一张场景图。name 之间不要语义重复。
3. scene_canonical 是该场景的"固定场景锚点串"：{SCENE_CANONICAL_MIN_CHARS}~{SCENE_CANONICAL_MAX_CHARS} 字（这是硬门禁，多一个字整份清单都会被拒收，写完请数一遍），必须包含 地点/室内外/典型光线时段/标志性陈设或建筑/整体氛围色调。只写视觉可见的环境信息，不写人物、不写剧情动作。原著未描写处按题材与画风合理补全并保持内部一致。
{scene_style_rule}
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


def _speech_budget_table_text(durations: list[int] | None = None) -> str:
    """口播预算换算表：`{duration}s≤{cap}字`，逐镜/大纲提示词共用同一数据源。

    唯一数据源是 ``config.max_spoken_chars_for_duration``；两处提示词都必须调用
    这个函数拼装文案，禁止各自手写字面量表，否则公式一改就会有一处提示词悄悄漂移
    成谎言（大纲/逐镜历史上就出现过这种分叉：大纲只给了 10s 天花板，逐镜给了全表）。
    """
    values = sorted(durations) if durations is not None else sorted(config.ALLOWED_DURATIONS)
    return "、".join(f"{value}s≤{config.max_spoken_chars_for_duration(value)}字" for value in values)


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


# ---------- C2. 单集分镜脚本（基于完整剧本拆分） ----------


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
    from app.multiview import watermark_qa_mode
    if watermark_qa_mode() == "ignore_unless_occluding":
        watermark_reported = result.get("watermark_detected") is True
        watermark_occluding = result.get("watermark_occluding")
        if watermark_reported and watermark_occluding is False:
            # Same contract as review_scene_image: only tell the deterministic
            # policy this provider mark is allowed when the configured
            # practical-quality mode says so, never unconditionally.
            result["non_occluding_provider_watermark"] = True
    from app.portrait_policy import normalize_portrait_seed_qa
    return normalize_portrait_seed_qa(result)


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
