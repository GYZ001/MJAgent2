"""Typed pre-document screenplay envelope and scene-writing shards.

These artifacts are resumable generation evidence only.  They never become a
working/published screenplay pointer; the public authority remains the compiled
``ScreenplayDocument`` created by Production Repair.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.character_policy import functional_extra_anchor
from app.db import get_conn, get_setting
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.identity_authority import identity_authority_registry
from app.narrative_blueprint import BlueprintScenePlan, NarrativeBlueprint
from app.observability.tracing import current_trace
from app.renderability import SCENE_STORY_FUNCTION_MIN_CHARS
from app.schemas import Bible
from app.screenplay_ir import (
    IRExperience,
    IRIdentity,
    IRMetadata,
    IRScene,
    ScreenplayGenerationIR,
    IR_VERSION,
)
from app.source_excerpt import index_source_segments, structural_front_matter_ids


SCREENPLAY_ENVELOPE_VERSION = "screenplay-envelope.v1"
SCREENPLAY_SCENE_SHARD_VERSION = "screenplay-scene-shard.v2"
SCREENPLAY_SHARD_PLAN_VERSION = "screenplay-scene-shard-plan.v1"
SCREENPLAY_MERGED_IR_VERSION = "screenplay-generation-ir-merged.v1"


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _setting_int(key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(get_setting(key) or default)))
    except (TypeError, ValueError):
        return default


class ScreenplayEnvelopeMetadata(BaseModel):
    title: str = ""
    logline: str = ""
    dramatic_question: str = ""
    protagonist_goal: str = ""
    obstacle: str = ""
    stakes: str = ""
    emotional_curve: str = ""
    ending_hook: str = ""
    source_basis: str = ""
    adaptation_direction: str = ""
    opening: str = ""
    development: str = ""
    conflict: str = ""
    climax: str = ""
    episode_premise: str = ""
    approved_adaptations: list[str] = Field(default_factory=list)
    forbidden_additions: list[str] = Field(default_factory=list)
    script_format_note: str = "场次化台本稿"
    must_keep_ending: str = ""
    drop_list: list[str] = Field(default_factory=list)

    def to_ir(self) -> IRMetadata:
        return IRMetadata.model_validate(self.model_dump(mode="json"))


class ScreenplayEnvelopeExperience(BaseModel):
    director_objective: str = ""
    satisfaction_criteria: str = ""
    required_processing_s: float = Field(default=1.0, ge=0)
    forbidden_misconceptions: list[str] = Field(default_factory=list)

    def to_ir(self) -> IRExperience:
        return IRExperience.model_validate(self.model_dump(mode="json"))


class ScreenplayEnvelopeIR(BaseModel):
    contract_version: Literal["screenplay-envelope.v1"] = SCREENPLAY_ENVELOPE_VERSION
    episode_no: int
    metadata: ScreenplayEnvelopeMetadata
    experience: ScreenplayEnvelopeExperience
    blueprint_hash: str
    identity_registry_hash: str

class ScreenplaySceneShardPlan(BaseModel):
    contract_version: Literal["screenplay-scene-shard-plan.v1"] = SCREENPLAY_SHARD_PLAN_VERSION
    shard_id: str
    scene_plan_keys: list[str]
    source_segment_ids: list[str]
    estimated_units: int = Field(ge=1)
    estimated_output_chars: int = Field(ge=1)
    boundary_state_in: dict[str, Any] = Field(default_factory=dict)
    boundary_state_out: dict[str, Any] = Field(default_factory=dict)
    source_hash: str
    boundary_hash: str
    blueprint_hash: str
    identity_registry_hash: str


class UnresolvedParticipant(BaseModel):
    source_label: str
    source_segment_ids: list[str] = Field(default_factory=list)
    scene_key: str = ""
    usage: str = "visible"
    reason: str = ""


class ScreenplaySceneShardIR(BaseModel):
    contract_version: Literal["screenplay-scene-shard.v2"] = SCREENPLAY_SCENE_SHARD_VERSION
    episode_no: int
    shard_id: str
    scene_plan_keys: list[str]
    scenes: list[IRScene]
    consumed_source_ids: list[str] = Field(default_factory=list)
    unresolved_participants: list[UnresolvedParticipant] = Field(default_factory=list)
    source_hash: str = ""
    boundary_hash: str = ""
    blueprint_hash: str = ""
    identity_registry_hash: str = ""

class ScreenplaySceneShardError(ValueError):
    def __init__(self, shard_id: str, errors: list[str]):
        self.shard_id = shard_id
        self.errors = list(errors)
        super().__init__(f"{shard_id}: " + "；".join(errors[:10]))


class ScreenplaySceneMergeError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("；".join(errors[:20]))


class ScreenplaySceneShardOwnershipLost(RuntimeError):
    """A provider response returned after another run acquired the episode."""


def _assert_episode_owner(episode_id: str) -> None:
    trace = current_trace()
    if not trace.run_id:
        return
    row = get_conn().execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not row or row["active_screenplay_run_id"] != trace.run_id:
        raise ScreenplaySceneShardOwnershipLost(
            "场次分片返回时剧集 owner 已变化，旧 worker 不得持久化结果"
        )


def blueprint_content_hash(blueprint: NarrativeBlueprint) -> str:
    return _hash(blueprint.model_dump(mode="json"))


def build_frozen_identity_registry(
    bible: Bible,
    resolutions: list[dict[str, Any]] | None,
) -> tuple[list[IRIdentity], list[dict[str, Any]], str]:
    """Project durable authorities into stable IR identity keys."""
    authorities = identity_authority_registry(bible, resolutions)
    identities: list[IRIdentity] = []
    projected: list[dict[str, Any]] = []
    for authority in sorted(
        authorities,
        key=lambda item: str(item.get("authority_id") or ""),
    ):
        authority_id = str(authority.get("authority_id") or "").strip()
        if not authority_id:
            continue
        canonical_name = str(
            authority.get("canonical_name") or authority_id
        ).strip()
        source_names = list(dict.fromkeys(
            [canonical_name]
            + [
                str(value).strip()
                for value in authority.get("source_labels") or []
                if str(value).strip()
            ]
        ))
        digest = hashlib.sha256(authority_id.encode("utf-8")).hexdigest()[:12]
        identity_key = f"person_{digest}"
        named = str(authority.get("identity_kind") or "") == "named"
        identity = IRIdentity(
            key=identity_key,
            display_name=canonical_name,
            authority_id=authority_id,
            source_names=source_names,
            kind="named_character" if named else "functional_character",
            visual_policy="canonical" if named else "contextual",
            visual_canonical=(
                ""
                if named
                else functional_extra_anchor(
                    canonical_name,
                    declared_functional_names={canonical_name},
                )
            ),
            asset_requirement="required" if named else "optional",
            voice_canonical="",
            role_type="named_character" if named else "functional_character",
            rationale="来自冻结的人物谱/本集身份决议",
        )
        identities.append(identity)
        projected.append({
            **authority,
            "identity_key": identity_key,
            "source_instance_key": str(
                authority.get("source_instance_key")
                or authority.get("identity_group")
                or authority_id
            ),
        })
    # Narration is a compiler-owned voice identity and does not require model
    # adjudication.  Scene shards may use it only for source-backed narration.
    identities.append(IRIdentity(
        key="narrator",
        display_name="旁白",
        authority_id="narrator:narrator",
        source_names=[],
        kind="narrator",
        visual_policy="offscreen_only",
        asset_requirement="forbidden",
        role_type="narrator",
        rationale="后端拥有的纯旁白身份",
    ))
    projected.append({
        "authority_id": "narrator:narrator",
        "identity_key": "narrator",
        "canonical_name": "旁白",
        "identity_kind": "narrator",
        "source_labels": [],
        "source_instance_key": "narrator:narrator",
    })
    registry_hash = _hash(projected)
    return identities, projected, registry_hash


def _scene_estimate(
    scene_plan: BlueprintScenePlan,
    source_by_id: dict[str, str],
) -> tuple[int, int]:
    source_chars = sum(
        len(re.sub(r"\s+", "", source_by_id.get(source_id, "")))
        for source_id in scene_plan.source_segment_ids
    )
    units = max(
        2,
        len(scene_plan.source_segment_ids),
        math.ceil(source_chars / 90) + max(0, scene_plan.dramatic_load - 1),
    )
    output_chars = max(1200, units * 460 + source_chars * 2)
    return units, output_chars


def build_screenplay_scene_shard_plans(
    blueprint: NarrativeBlueprint,
    *,
    source_text: str,
    identity_registry_hash: str,
    max_units: int | None = None,
    max_output_chars: int | None = None,
) -> list[ScreenplaySceneShardPlan]:
    """Deterministically group consecutive Blueprint-owned scene plans."""
    max_units = max_units or _setting_int(
        "screenplay_scene_shard_max_units", 24, minimum=8, maximum=64
    )
    max_output_chars = max_output_chars or _setting_int(
        "screenplay_scene_shard_max_output_chars", 12000,
        minimum=3000, maximum=30000,
    )
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    blueprint_hash = blueprint_content_hash(blueprint)
    groups: list[list[BlueprintScenePlan]] = []
    current: list[BlueprintScenePlan] = []
    current_units = 0
    current_chars = 0
    current_domain = ""
    for plan in blueprint.scene_plans:
        units, output_chars = _scene_estimate(plan, source_by_id)
        would_overflow = bool(current) and (
            current_units + units > max_units
            or current_chars + output_chars > max_output_chars
        )
        # A temporal-domain change is a natural retry boundary.  Never combine
        # a later domain back into an earlier shard.
        domain_break = bool(current) and plan.temporal_domain_key != current_domain
        if would_overflow or domain_break:
            groups.append(current)
            current = []
            current_units = 0
            current_chars = 0
        current.append(plan)
        current_units += units
        current_chars += output_chars
        current_domain = plan.temporal_domain_key
    if current:
        groups.append(current)

    plans: list[ScreenplaySceneShardPlan] = []
    previous_boundary: dict[str, Any] = {}
    for index, group in enumerate(groups, start=1):
        source_ids = list(dict.fromkeys(
            source_id for plan in group for source_id in plan.source_segment_ids
        ))
        estimated = [_scene_estimate(plan, source_by_id) for plan in group]
        boundary_in = dict(previous_boundary)
        boundary_out = {
            "scene_key": group[-1].key,
            "temporal_domain_key": group[-1].temporal_domain_key,
            "location_key": group[-1].location_key,
            "exit_state": group[-1].exit_state,
        }
        source_hash = _hash({
            source_id: source_by_id.get(source_id, "") for source_id in source_ids
        })
        boundary_hash = _hash({"in": boundary_in, "out": boundary_out})
        plans.append(ScreenplaySceneShardPlan(
            shard_id=f"SS{index:03d}",
            scene_plan_keys=[plan.key for plan in group],
            source_segment_ids=source_ids,
            estimated_units=sum(value[0] for value in estimated),
            estimated_output_chars=sum(value[1] for value in estimated),
            boundary_state_in=boundary_in,
            boundary_state_out=boundary_out,
            source_hash=source_hash,
            boundary_hash=boundary_hash,
            blueprint_hash=blueprint_hash,
            identity_registry_hash=identity_registry_hash,
        ))
        previous_boundary = boundary_out
    return plans


def normalize_screenplay_scene_shard(
    shard: ScreenplaySceneShardIR,
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    identity_registry: list[dict[str, Any]],
    identity_keys: set[str],
) -> ScreenplaySceneShardIR:
    """Normalize program-owned fields without changing authored scene prose."""
    shard.episode_no = episode_no
    shard.shard_id = plan.shard_id
    shard.scene_plan_keys = list(plan.scene_plan_keys)
    shard.source_hash = plan.source_hash
    shard.boundary_hash = plan.boundary_hash
    shard.blueprint_hash = plan.blueprint_hash
    shard.identity_registry_hash = plan.identity_registry_hash

    aliases = {key: key for key in identity_keys}
    for item in identity_registry:
        identity_key = str(item.get("identity_key") or "").strip()
        if not identity_key:
            continue
        for value in (
            identity_key,
            item.get("canonical_name"),
            *(item.get("source_labels") or []),
        ):
            label = str(value or "").strip()
            if label:
                aliases[label] = identity_key

    def normalize_refs(values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            label = str(value or "").strip()
            resolved = aliases.get(label, label)
            if resolved and resolved not in normalized:
                normalized.append(resolved)
        return normalized

    for scene in shard.scenes:
        expected_scene = scene_plans.get(scene.key)
        participant_aliases = set(
            expected_scene.participant_keys if expected_scene else []
        )
        scene.character_keys = normalize_refs(scene.character_keys)
        for unit in scene.units:
            unit.actor_keys = normalize_refs(unit.actor_keys)
            unit.onscreen_entity_keys = normalize_refs(
                unit.onscreen_entity_keys
            )
            if unit.speaker_key:
                unit.speaker_key = aliases.get(
                    unit.speaker_key,
                    unit.speaker_key,
                )
            targets: list[str] = []
            for value in unit.target_keys:
                label = str(value or "").strip()
                resolved = aliases.get(label)
                if resolved:
                    if resolved not in targets:
                        targets.append(resolved)
                    continue
                if label in participant_aliases and label not in targets:
                    # A Blueprint participant missing from the frozen registry
                    # must remain visible to the hard validator.
                    targets.append(label)
                # Environment targets such as trees remain in the action prose;
                # target_keys is reserved for identity relations.
            unit.target_keys = targets
    return shard


def validate_screenplay_scene_shard(
    shard: ScreenplaySceneShardIR,
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    identity_keys: set[str],
) -> list[str]:
    errors: list[str] = []
    if shard.shard_id != plan.shard_id:
        errors.append(f"shard_id 应为 {plan.shard_id}")
    if shard.scene_plan_keys != plan.scene_plan_keys:
        errors.append("scene_plan_keys 与计划不一致")
    actual_scene_keys = [scene.key for scene in shard.scenes]
    if actual_scene_keys != plan.scene_plan_keys:
        errors.append(
            "scenes 必须按计划恰好输出一次："
            f"expected={plan.scene_plan_keys}, actual={actual_scene_keys}"
        )
    if shard.unresolved_participants:
        errors.append(
            "存在未冻结参与者："
            + "、".join(item.source_label for item in shard.unresolved_participants)
        )
    actual_consumed: list[str] = []
    event_owner: dict[str, str] = {}
    chain_owner: dict[str, str] = {}
    for scene in shard.scenes:
        expected_scene = scene_plans.get(scene.key)
        if expected_scene is None:
            errors.append(f"未知 scene key：{scene.key}")
            continue
        if scene.scene_heading != expected_scene.scene_heading:
            errors.append(f"{scene.key} scene_heading 必须由 Blueprint 精确拥有")
        if len(scene.story_function.strip()) < SCENE_STORY_FUNCTION_MIN_CHARS:
            errors.append(
                f"{scene.key}.story_function 必须完整说明本场戏剧功能，"
                f"至少 {SCENE_STORY_FUNCTION_MIN_CHARS} 个字符"
            )
        allowed = set(expected_scene.source_segment_ids)
        for unit_index, unit in enumerate(scene.units):
            for source_id in unit.source_segment_ids:
                if source_id not in allowed:
                    errors.append(
                        f"{scene.key}.units[{unit_index}] 来源越界：{source_id}"
                    )
                elif source_id not in actual_consumed:
                    actual_consumed.append(source_id)
            if unit.speaker_key and unit.speaker_key not in identity_keys:
                errors.append(
                    f"{scene.key}.units[{unit_index}] speaker_key 未冻结："
                    f"{unit.speaker_key}"
                )
            unbound_onscreen = sorted(
                set(unit.onscreen_entity_keys) - identity_keys
            )
            if unbound_onscreen:
                errors.append(
                    f"{scene.key}.units[{unit_index}] onscreen_entity_keys 未冻结："
                    f"{unbound_onscreen}"
                )
            unbound_action_relations = sorted(
                set([*unit.actor_keys, *unit.target_keys]) - identity_keys
            )
            if unbound_action_relations:
                errors.append(
                    f"{scene.key}.units[{unit_index}] actor/target 未冻结："
                    f"{unbound_action_relations}"
                )
            if "onscreen_entity_keys" not in unit.model_fields_set:
                errors.append(
                    f"{scene.key}.units[{unit_index}] 必须显式声明 onscreen_entity_keys"
                )
            if (
                unit.kind == "action"
                and "actor_keys" not in unit.model_fields_set
            ):
                errors.append(
                    f"{scene.key}.units[{unit_index}] 动作单元必须显式声明 actor_keys"
                )
            if unit.event_key:
                # Reuse inside a scene denotes phases of one event; reuse across
                # scenes is not allowed before global namespacing.
                previous_owner = event_owner.setdefault(unit.event_key, scene.key)
                if previous_owner != scene.key:
                    errors.append(f"跨场 event_key 重复：{unit.event_key}")
            if unit.chain_key:
                previous_owner = chain_owner.setdefault(unit.chain_key, scene.key)
                if previous_owner != scene.key:
                    errors.append(f"跨场 chain_key 重复：{unit.chain_key}")
        missing_for_scene = [
            source_id for source_id in expected_scene.source_segment_ids
            if source_id not in {
                source_id for unit in scene.units
                for source_id in unit.source_segment_ids
            }
        ]
        if missing_for_scene:
            errors.append(
                f"{scene.key} 未消费来源：{','.join(missing_for_scene)}"
            )
    if shard.consumed_source_ids != actual_consumed:
        errors.append("consumed_source_ids 必须按首次消费顺序等于 units 的实际来源并集")
    for field, expected in (
        ("source_hash", plan.source_hash),
        ("boundary_hash", plan.boundary_hash),
        ("blueprint_hash", plan.blueprint_hash),
        ("identity_registry_hash", plan.identity_registry_hash),
    ):
        actual = str(getattr(shard, field) or "")
        if actual != expected:
            errors.append(f"{field} 不匹配")
    return errors


def _namespace_shard_scene_keys(
    shard: ScreenplaySceneShardIR,
) -> list[IRScene]:
    scenes: list[IRScene] = []
    for scene in shard.scenes:
        event_map: dict[str, str] = {}
        chain_map: dict[str, str] = {}
        data = scene.model_dump(mode="json")
        scene_namespace = re.sub(r"[^A-Za-z0-9_]+", "_", scene.key).strip("_").lower()
        for unit in data.get("units") or []:
            event_key = str(unit.get("event_key") or "event")
            unit["event_key"] = event_map.setdefault(
                event_key,
                f"{shard.shard_id.lower()}_{scene_namespace}_{event_key}",
            )
            chain_key = str(unit.get("chain_key") or "")
            if chain_key:
                unit["chain_key"] = chain_map.setdefault(
                    chain_key,
                    f"{shard.shard_id.lower()}_{scene_namespace}_{chain_key}",
                )
        scenes.append(IRScene.model_validate(data))
    return scenes


def merge_screenplay_scene_shards(
    *,
    envelope: ScreenplayEnvelopeIR,
    identities: list[IRIdentity],
    plans: list[ScreenplaySceneShardPlan],
    shards: list[ScreenplaySceneShardIR],
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> ScreenplayGenerationIR:
    errors: list[str] = []
    by_id = {shard.shard_id: shard for shard in shards}
    if len(by_id) != len(shards):
        errors.append("shard_id 必须全局唯一")
    if set(by_id) != {plan.shard_id for plan in plans}:
        errors.append("validated shards 与 shard plan 集合不一致")
    expected_blueprint_hash = blueprint_content_hash(blueprint)
    if envelope.blueprint_hash != expected_blueprint_hash:
        errors.append("Envelope blueprint_hash 不匹配")
    expected_scenes = [plan.key for plan in blueprint.scene_plans]
    merged_scenes: list[IRScene] = []
    consumed: list[str] = []
    scene_plan_map = {plan.key: plan for plan in blueprint.scene_plans}
    identity_keys = {identity.key for identity in identities}
    for plan_index, plan in enumerate(plans):
        shard = by_id.get(plan.shard_id)
        if shard is None:
            continue
        errors.extend(validate_screenplay_scene_shard(
            shard,
            plan=plan,
            scene_plans=scene_plan_map,
            identity_keys=identity_keys,
        ))
        if plan_index and plan.boundary_state_in != plans[plan_index - 1].boundary_state_out:
            errors.append(f"{plan.shard_id} boundary state 与前一 shard 不闭合")
        merged_scenes.extend(_namespace_shard_scene_keys(shard))
        consumed.extend(shard.consumed_source_ids)
    if [scene.key for scene in merged_scenes] != expected_scenes:
        errors.append("合并后 scene 顺序与 Blueprint 不一致")
    segments = index_source_segments(source_text)
    required_ids = [
        segment.segment_id for segment in segments
        if segment.segment_id not in structural_front_matter_ids(segments)
    ]
    missing = [source_id for source_id in required_ids if source_id not in consumed]
    if missing:
        errors.append("合并 IR 未覆盖非标题 SRC：" + ",".join(missing))
    source_order = {source_id: index for index, source_id in enumerate(required_ids)}
    first_owned = []
    already: set[str] = set()
    for scene in merged_scenes:
        for unit in scene.units:
            for source_id in unit.source_segment_ids:
                if source_id in source_order and source_id not in already:
                    already.add(source_id)
                    first_owned.append(source_order[source_id])
    if first_owned != sorted(first_owned):
        errors.append("来源首次所有权顺序不单调")
    if errors:
        raise ScreenplaySceneMergeError(errors)
    return ScreenplayGenerationIR(
        format_version=IR_VERSION,
        episode_no=envelope.episode_no,
        metadata=envelope.metadata.to_ir(),
        identities=identities,
        scenes=merged_scenes,
        experience=envelope.experience.to_ir(),
    )


def _latest_validated_artifact(
    *,
    episode_id: str,
    artifact_type: str,
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    rows = get_conn().execute(
        """SELECT id,content_json,content_hash,parent_artifact_ids_json,
                  contract_version,prompt_version,model_snapshot_json
             FROM artifacts
            WHERE scope_type='episode' AND scope_id=? AND type=?
              AND status='validated'
            ORDER BY created_at DESC LIMIT 100""",
        (episode_id, artifact_type),
    ).fetchall()
    for row in rows:
        try:
            content = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if predicate(content):
            return {**dict(row), "content": content}
    return None


async def generate_screenplay_envelope(
    *,
    episode: dict[str, Any],
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict[str, Any]],
    identity_registry_hash: str,
    blueprint_artifact_id: str | None = None,
    identity_artifact_id: str | None = None,
) -> tuple[ScreenplayEnvelopeIR, str]:
    episode_id = str(episode.get("id") or f"episode-{episode['episode_no']}")
    _assert_episode_owner(episode_id)
    blueprint_hash = blueprint_content_hash(blueprint)
    cached = _latest_validated_artifact(
        episode_id=episode_id,
        artifact_type="screenplay_envelope",
        predicate=lambda content: (
            content.get("blueprint_hash") == blueprint_hash
            and content.get("identity_registry_hash") == identity_registry_hash
            and content.get("contract_version") == SCREENPLAY_ENVELOPE_VERSION
        ),
    )
    if cached:
        return ScreenplayEnvelopeIR.model_validate(cached["content"]), str(cached["id"])
    node_summary = [
        {
            "key": node.key,
            "summary": node.summary,
            "time_relation": node.time_relation,
            "location": node.location_label,
            "participants": node.participants,
            "scene_role": node.scene_role,
            "dramatic_load": node.dramatic_load,
            "action_logic": node.action_logic,
            "decision": node.decision.model_dump(mode="json") if node.decision else None,
            "agency": (
                node.decision.narrative_attribution if node.decision else None
            ),
        }
        for node in blueprint.nodes
    ]
    prompt = (
        "任务：根据已验证叙事蓝图生成整集全局 Screenplay Envelope。"
        "这里只决定 metadata 与 experience，不写 scenes，不需要也不得索要完整原文。"
        "不得在 approved_adaptations 中伪造来源事实。\n集信息：\n"
        + json.dumps({
            key: episode.get(key)
            for key in (
                "episode_no", "title", "synopsis", "hook", "cliffhanger",
            )
        }, ensure_ascii=False, separators=(",", ":"))
        + "\n蓝图全局摘要：\n"
        + json.dumps(node_summary, ensure_ascii=False, separators=(",", ":"))
        + "\n冻结身份摘要：\n"
        + json.dumps(identity_registry, ensure_ascii=False, separators=(",", ":"))
        + "\n只输出 Schema 对象：\n"
        + json.dumps(ScreenplayEnvelopeIR.model_json_schema(), ensure_ascii=False)
        + f"\n固定字段：contract_version={SCREENPLAY_ENVELOPE_VERSION},"
        f" episode_no={episode['episode_no']}, blueprint_hash={blueprint_hash},"
        f" identity_registry_hash={identity_registry_hash}"
    )

    def validate_envelope(value: ScreenplayEnvelopeIR) -> list[str]:
        errors: list[str] = []
        if value.episode_no != int(episode["episode_no"]):
            errors.append("episode_no 不匹配")
        if value.blueprint_hash != blueprint_hash:
            errors.append("blueprint_hash 不匹配")
        if value.identity_registry_hash != identity_registry_hash:
            errors.append("identity_registry_hash 不匹配")
        expected_ending = str(episode.get("cliffhanger") or "").strip()
        if not expected_ending and value.metadata.ending_hook.strip():
            errors.append("本集无 cliffhanger，ending_hook 必须为空")
        return errors

    attempts: list[dict[str, Any]] = []
    envelope = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=ScreenplayEnvelopeIR,
        validate=validate_envelope,
        operation_id=(
            f"screenplay.envelope:{SCREENPLAY_ENVELOPE_VERSION}:"
            f"{episode_id}:{blueprint_hash}:{identity_registry_hash}"
        ),
        max_tokens=6144,
        temperature=0.2,
        format_retry_limit=_setting_int(
            "screenplay_format_retry_limit", 1, minimum=0, maximum=3
        ),
        semantic_retry_limit=_setting_int(
            "screenplay_semantic_retry_limit", 1, minimum=0, maximum=3
        ),
        call_meta={
            "stage": "剧本全局包络",
            "stage_key": "screenplay_envelope",
            "substage": "envelope",
            "episode_id": episode_id,
            "input_chars": len(prompt),
            "source_count": 0,
        },
        on_attempt=attempts.append,
    )
    _assert_episode_owner(episode_id)
    trace = current_trace()
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_envelope_raw",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T0",
            content={
                "operation_id": (
                    f"screenplay.envelope:{blueprint_hash}:{identity_registry_hash}"
                ),
                "attempts": attempts,
            },
            parent_artifact_ids=[
                value for value in (blueprint_artifact_id, identity_artifact_id) if value
            ],
            contract_version=SCREENPLAY_ENVELOPE_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_envelope",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content=envelope.model_dump(mode="json"),
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version=SCREENPLAY_ENVELOPE_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    return envelope, str(artifact["id"])


def _scene_shard_prompt(
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    blueprint_scene_plans: list[BlueprintScenePlan],
    blueprint_nodes: list[dict[str, Any]],
    source_by_id: dict[str, str],
    identity_registry: list[dict[str, Any]],
) -> str:
    source_projection = {
        source_id: source_by_id[source_id]
        for source_id in plan.source_segment_ids
        if source_id in source_by_id
    }
    return (
        "任务：只写指定 Blueprint 场次的紧凑语义 IR。场 key、heading、顺序和来源所有权"
        "均由程序拥有，不得改名、跨场挪 SRC 或输出整集 metadata/experience/events/beats/coverage。"
        "每个非标题来源必须由至少一个 action/dialogue unit 消费；dialogue.source_text 必须"
        "逐字来自其声明 SRC。speaker_key 只能逐字引用冻结 identity_key。发现无法绑定的"
        "参与者时写 unresolved_participants，绝不自行创建 ID。每个 unit 的 "
        "actor_keys/target_keys 只能填写当前 unit 的实际动作执行者与受作用对象；"
        "onscreen_entity_keys 只能填写这一动作或话轮当下实际在画面中的冻结 identity_key；"
        "被台词提到、仅能听见或只感知事件的身份不得因此进入该列表。复杂动作可在同一 local event_key"
        "下写有序 units。\nShard plan：\n"
        + plan.model_dump_json()
        + "\nBlueprint scene plans：\n"
        + json.dumps(
            [value.model_dump(mode="json") for value in blueprint_scene_plans],
            ensure_ascii=False, separators=(",", ":"),
        )
        + "\n相关 Blueprint nodes：\n"
        + json.dumps(blueprint_nodes, ensure_ascii=False, separators=(",", ":"))
        + "\n冻结 identity registry：\n"
        + json.dumps(identity_registry, ensure_ascii=False, separators=(",", ":"))
        + "\n本 shard owned SRC：\n"
        + json.dumps(source_projection, ensure_ascii=False, separators=(",", ":"))
        + "\n只输出 Schema 对象：\n"
        + json.dumps(ScreenplaySceneShardIR.model_json_schema(), ensure_ascii=False)
        + f"\n固定字段：episode_no={episode_no}, shard_id={plan.shard_id},"
        f" source_hash={plan.source_hash}, boundary_hash={plan.boundary_hash},"
        f" blueprint_hash={plan.blueprint_hash},"
        f" identity_registry_hash={plan.identity_registry_hash}"
    )


async def generate_screenplay_scene_shards(
    *,
    episode: dict[str, Any],
    source_text: str,
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict[str, Any]],
    identities: list[IRIdentity],
    plans: list[ScreenplaySceneShardPlan],
    blueprint_artifact_id: str | None = None,
    identity_artifact_id: str | None = None,
    progress: Callable[[list[dict[str, Any]]], Any] | None = None,
) -> tuple[list[ScreenplaySceneShardIR], list[str], list[dict[str, Any]]]:
    """Generate/reuse independent shards with a per-episode concurrency cap."""
    episode_id = str(episode.get("id") or f"episode-{episode['episode_no']}")
    scene_plan_map = {plan.key: plan for plan in blueprint.scene_plans}
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    identity_keys = {identity.key for identity in identities}
    parallelism = _setting_int(
        "screenplay_scene_shard_parallelism", 2, minimum=1, maximum=2
    )
    semaphore = asyncio.Semaphore(parallelism)
    checkpoint_rows: dict[str, dict[str, Any]] = {
        plan.shard_id: {
            "shard_id": plan.shard_id,
            "status": "pending",
            "attempt": 0,
            "source_hash": plan.source_hash,
            "boundary_hash": plan.boundary_hash,
        }
        for plan in plans
    }

    def emit_progress() -> None:
        if progress is not None:
            progress([checkpoint_rows[plan.shard_id] for plan in plans])

    async def generate_one(
        plan: ScreenplaySceneShardPlan,
    ) -> tuple[ScreenplaySceneShardIR, str]:
        _assert_episode_owner(episode_id)
        cached = _latest_validated_artifact(
            episode_id=episode_id,
            artifact_type="screenplay_scene_shard",
            predicate=lambda content: all(
                content.get(field) == getattr(plan, field)
                for field in (
                    "shard_id", "source_hash", "boundary_hash",
                    "blueprint_hash", "identity_registry_hash",
                )
            ) and content.get("contract_version") == SCREENPLAY_SCENE_SHARD_VERSION,
        )
        if cached:
            try:
                shard = ScreenplaySceneShardIR.model_validate(cached["content"])
            except ValidationError:
                shard = None
            if shard is not None:
                errors = validate_screenplay_scene_shard(
                    shard,
                    plan=plan,
                    scene_plans=scene_plan_map,
                    identity_keys=identity_keys,
                )
                if not errors:
                    checkpoint_rows[plan.shard_id].update({
                        "status": "validated",
                        "attempt": 0,
                        "normalized_artifact_id": str(cached["id"]),
                        "reused": True,
                    })
                    emit_progress()
                    return shard, str(cached["id"])
        selected_scene_plans = [scene_plan_map[key] for key in plan.scene_plan_keys]
        selected_node_keys = {
            node_key for scene_plan in selected_scene_plans
            for node_key in scene_plan.node_keys
        }
        prompt = _scene_shard_prompt(
            episode_no=int(episode["episode_no"]),
            plan=plan,
            blueprint_scene_plans=selected_scene_plans,
            blueprint_nodes=[
                node.model_dump(mode="json")
                for node in blueprint.nodes if node.key in selected_node_keys
            ],
            source_by_id=source_by_id,
            identity_registry=identity_registry,
        )

        def validate_shard(value: ScreenplaySceneShardIR) -> list[str]:
            normalize_screenplay_scene_shard(
                value,
                episode_no=int(episode["episode_no"]),
                plan=plan,
                scene_plans=scene_plan_map,
                identity_registry=identity_registry,
                identity_keys=identity_keys,
            )
            return validate_screenplay_scene_shard(
                value,
                plan=plan,
                scene_plans=scene_plan_map,
                identity_keys=identity_keys,
            )

        async with semaphore:
            checkpoint_rows[plan.shard_id].update({
                "status": "running", "attempt": 1,
            })
            emit_progress()
            attempts: list[dict[str, Any]] = []
            shard = await model_gateway.chat_structured(
                [{"role": "user", "content": prompt}],
                model_type=ScreenplaySceneShardIR,
                validate=validate_shard,
                operation_id=(
                    f"screenplay.scene-shard:{SCREENPLAY_SCENE_SHARD_VERSION}:"
                    f"{episode_id}:{plan.shard_id}:{plan.source_hash}:"
                    f"{plan.boundary_hash}:{plan.blueprint_hash}:"
                    f"{plan.identity_registry_hash}"
                ),
                max_tokens=max(
                    4096,
                    min(16384, math.ceil(plan.estimated_output_chars / 1.5)),
                ),
                temperature=0.4,
                format_retry_limit=_setting_int(
                    "screenplay_format_retry_limit", 1, minimum=0, maximum=3
                ),
                semantic_retry_limit=_setting_int(
                    "screenplay_semantic_retry_limit", 1, minimum=0, maximum=3
                ),
                call_meta={
                    "stage": "剧本场次分片",
                    "stage_key": "screenplay_scene_shards",
                    "substage": "scene_writing",
                    "shard_id": plan.shard_id,
                    "shard_count": len(plans),
                    "episode_id": episode_id,
                    "source_count": len(plan.source_segment_ids),
                    "scene_count": len(plan.scene_plan_keys),
                    "input_chars": len(prompt),
                },
                repair_context=json.dumps({
                    "owned_source": {
                        source_id: source_by_id.get(source_id, "")
                        for source_id in plan.source_segment_ids
                    },
                    "allowed_identities": [
                        {
                            "identity_key": item.get("identity_key"),
                            "canonical_name": item.get("canonical_name"),
                            "source_labels": item.get("source_labels") or [],
                        }
                        for item in identity_registry
                    ],
                }, ensure_ascii=False, separators=(",", ":")),
                on_attempt=attempts.append,
            )
        _assert_episode_owner(episode_id)
        trace = current_trace()
        parents = [
            value for value in (blueprint_artifact_id, identity_artifact_id) if value
        ]
        raw_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_scene_shard_raw",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T0",
                content={
                    "shard_id": plan.shard_id,
                    "operation_id": (
                        f"screenplay.scene-shard:{plan.source_hash}:{plan.boundary_hash}"
                    ),
                    "attempts": attempts,
                },
                parent_artifact_ids=parents,
                contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
            ),
            step_run_id=trace.step_run_id,
        )
        artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_scene_shard",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content=shard.model_dump(mode="json"),
                parent_artifact_ids=[raw_artifact["id"]],
                contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
                model_snapshot={
                    "shard_id": plan.shard_id,
                    "scene_count": len(shard.scenes),
                    "unit_count": sum(len(scene.units) for scene in shard.scenes),
                },
            ),
            step_run_id=trace.step_run_id,
        )
        checkpoint_rows[plan.shard_id].update({
            "status": "validated",
            "raw_artifact_id": raw_artifact["id"],
            "normalized_artifact_id": artifact["id"],
            "reused": False,
        })
        emit_progress()
        return shard, str(artifact["id"])

    results = await asyncio.gather(
        *(generate_one(plan) for plan in plans),
        return_exceptions=True,
    )
    shards: list[ScreenplaySceneShardIR] = []
    artifact_ids: list[str] = []
    failures: list[BaseException] = []
    for plan, result in zip(plans, results, strict=True):
        if isinstance(result, BaseException):
            checkpoint_rows[plan.shard_id]["status"] = "failed"
            checkpoint_rows[plan.shard_id]["error_type"] = type(result).__name__
            failures.append(result)
            continue
        shard, artifact_id = result
        shards.append(shard)
        artifact_ids.append(artifact_id)
    emit_progress()
    if failures:
        first = failures[0]
        raise ScreenplaySceneShardError(
            next(
                plan.shard_id for plan in plans
                if checkpoint_rows[plan.shard_id]["status"] == "failed"
            ),
            [str(first)],
        ) from first
    return shards, artifact_ids, [checkpoint_rows[plan.shard_id] for plan in plans]


def persist_identity_registry(
    *,
    episode_id: str,
    identity_registry: list[dict[str, Any]],
    identity_registry_hash: str,
    parent_artifact_ids: list[str] | None = None,
) -> str:
    _assert_episode_owner(episode_id)
    cached = _latest_validated_artifact(
        episode_id=episode_id,
        artifact_type="screenplay_identity_registry",
        predicate=lambda content: (
            content.get("identity_registry_hash") == identity_registry_hash
        ),
    )
    if cached:
        return str(cached["id"])
    trace = current_trace()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_registry",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": "screenplay-identity-registry.v1",
                "identity_registry_hash": identity_registry_hash,
                "identities": identity_registry,
            },
            parent_artifact_ids=list(parent_artifact_ids or []),
            contract_version="screenplay-identity-registry.v1",
        ),
        step_run_id=trace.step_run_id,
    )
    return str(artifact["id"])


def persist_merged_ir(
    *,
    episode_id: str,
    ir: ScreenplayGenerationIR,
    parent_artifact_ids: list[str],
    blueprint_hash: str,
    identity_registry_hash: str,
) -> str:
    _assert_episode_owner(episode_id)
    trace = current_trace()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir_merged",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content=ir.model_dump(mode="json"),
            parent_artifact_ids=list(dict.fromkeys(parent_artifact_ids)),
            contract_version=SCREENPLAY_MERGED_IR_VERSION,
            model_snapshot={
                "generation_contract": IR_VERSION,
                "blueprint_hash": blueprint_hash,
                "identity_registry_hash": identity_registry_hash,
                "scene_count": len(ir.scenes),
                "unit_count": sum(len(scene.units) for scene in ir.scenes),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    object.__setattr__(ir, "evidence_artifact_id", artifact["id"])
    return str(artifact["id"])


def shard_progress(rows: list[dict[str, Any]] | None) -> dict[str, int]:
    values = list(rows or [])
    return {
        "total": len(values),
        "validated": sum(item.get("status") == "validated" for item in values),
        "running": sum(item.get("status") == "running" for item in values),
        "failed": sum(item.get("status") == "failed" for item in values),
    }
