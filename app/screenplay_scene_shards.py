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
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.character_policy import functional_extra_anchor
from app.db import get_conn, get_setting
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.identity_authority import identity_authority_registry
from app.narrative_blueprint import (
    BlueprintSceneDerivation,
    BlueprintScenePlan,
    NarrativeBlueprint,
    derive_blueprint_scene_plans,
)
from app.observability.tracing import current_trace
from app.renderability import SCENE_STORY_FUNCTION_MIN_CHARS
from app.schemas import Bible
from app.screenplay_ir import (
    IRActionParticipantDelivery,
    IRExperience,
    IRIdentity,
    IRMetadata,
    IRScene,
    IRSceneUnit,
    ScreenplayGenerationIR,
    IR_VERSION,
)
from app.source_excerpt import index_source_segments, structural_front_matter_ids


SCREENPLAY_ENVELOPE_VERSION = "screenplay-envelope.v1"
SCREENPLAY_SCENE_SHARD_VERSION = "screenplay-scene-shard.v4"
SCREENPLAY_SHARD_PLAN_VERSION = "screenplay-scene-shard-plan.v2"
SCREENPLAY_SCENE_INPUT_VERSION = "screenplay-scene-input.v4"
SCREENPLAY_MERGED_IR_VERSION = "screenplay-generation-ir-merged.v3"
SCREENPLAY_SCENE_SHARD_MIN_OUTPUT_TOKENS = 4096
SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS = 16384
SCREENPLAY_SCENE_SHARD_SCENE_RESERVE_TOKENS = 512
SCREENPLAY_SCENE_SHARD_UNIT_RESERVE_TOKENS = 128
SCREENPLAY_SCENE_SHARD_REASONING_RESERVE_PERCENT = 20


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
    contract_version: Literal["screenplay-scene-shard-plan.v2"] = SCREENPLAY_SHARD_PLAN_VERSION
    shard_id: str
    scene_plan_keys: list[str]
    source_segment_ids: list[str]
    source_scene_owners: dict[str, str]
    derived_relations: list[BlueprintSceneDerivation] = Field(
        default_factory=list,
    )
    source_ownership_hash: str
    estimated_units: int = Field(ge=1)
    estimated_output_chars: int = Field(ge=1)
    boundary_state_in: dict[str, Any] = Field(default_factory=dict)
    boundary_state_out: dict[str, Any] = Field(default_factory=dict)
    source_hash: str
    boundary_hash: str
    blueprint_hash: str
    identity_registry_hash: str


class ScreenplaySceneSourceSegment(BaseModel):
    source_segment_id: str
    text: str


class ScreenplaySceneParticipantBinding(BaseModel):
    blueprint_key: str
    identity_key: str


class ScreenplayActionParticipantDeliveryContract(BaseModel):
    contract_version: Literal["screenplay-generation-ir.v2"] = IR_VERSION
    evidence_schema: dict[str, Any] = Field(
        default_factory=IRActionParticipantDelivery.model_json_schema,
    )
    unit_field_required: Literal[True] = True
    offscreen_relation_requires_evidence: Literal[True] = True
    observable_claim_required: Literal[True] = True
    perceivable_channel_required: Literal[True] = True


class ScreenplaySceneInputContract(BaseModel):
    contract_version: Literal["screenplay-scene-input.v4"] = (
        SCREENPLAY_SCENE_INPUT_VERSION
    )
    scene_plan_key: str
    node_keys: list[str]
    source_segment_ids: list[str]
    source_segments: list[ScreenplaySceneSourceSegment]
    participant_bindings: list[ScreenplaySceneParticipantBinding]
    source_scene_owners: dict[str, str]
    derived_relations: list[BlueprintSceneDerivation] = Field(
        default_factory=list,
    )
    action_participant_delivery_contract: (
        ScreenplayActionParticipantDeliveryContract
    ) = Field(default_factory=ScreenplayActionParticipantDeliveryContract)
    source_ownership_hash: str


class UnresolvedParticipant(BaseModel):
    source_label: str
    source_segment_ids: list[str] = Field(default_factory=list)
    scene_key: str = ""
    usage: str = "visible"
    reason: str = ""


class ScreenplaySceneShardUnit(IRSceneUnit):
    participant_deliveries: list[IRActionParticipantDelivery]


class ScreenplaySceneShardScene(IRScene):
    units: list[ScreenplaySceneShardUnit] = Field(default_factory=list)


class ScreenplaySceneShardIR(BaseModel):
    contract_version: Literal["screenplay-scene-shard.v4"] = SCREENPLAY_SCENE_SHARD_VERSION
    episode_no: int
    shard_id: str
    scene_plan_keys: list[str]
    scenes: list[ScreenplaySceneShardScene]
    consumed_source_ids: list[str] = Field(default_factory=list)
    unresolved_participants: list[UnresolvedParticipant] = Field(default_factory=list)
    source_hash: str = ""
    boundary_hash: str = ""
    blueprint_hash: str = ""
    identity_registry_hash: str = ""
    source_ownership_hash: str = ""


_PARTICIPANT_PERCEPTION_CHANNELS = (
    "audible",
    "visible_effect",
    "visible_reaction",
)


def _unit_relation_participant_keys(
    unit: ScreenplaySceneShardUnit,
) -> list[str]:
    values = [*unit.actor_keys, *unit.target_keys]
    if unit.kind == "dialogue" and unit.speaker_key:
        values.append(unit.speaker_key)
    return [
        key
        for key in dict.fromkeys(
            str(value or "").strip() for value in values
        )
        if key
    ]


def _unit_delivery_evidence_channels(
    unit: ScreenplaySceneShardUnit,
    participant_key: str,
) -> list[str]:
    channels: list[str] = []
    for delivery in unit.participant_deliveries:
        if delivery.participant_key.strip() != participant_key:
            continue
        for channel in _PARTICIPANT_PERCEPTION_CHANNELS:
            if getattr(delivery, channel) and channel not in channels:
                channels.append(channel)
    return channels


def _scene_canonical_identity_keys(
    contract: ScreenplaySceneInputContract,
) -> list[str]:
    return [
        key
        for key in dict.fromkeys(
            binding.identity_key.strip()
            for binding in contract.participant_bindings
        )
        if key
    ]


def _canonical_identity_array_schema(
    canonical_keys: list[str],
    *,
    values: list[str] | None = None,
) -> dict[str, Any]:
    if not canonical_keys:
        return {
            "type": "array",
            "items": False,
            "minItems": 0,
            "maxItems": 0,
        }
    schema: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "string",
            "enum": list(canonical_keys),
        },
        "uniqueItems": True,
        "minItems": 0,
        "maxItems": len(canonical_keys),
    }
    if values is None:
        return schema
    candidate_keys = [
        key
        for key in dict.fromkeys(
            str(value or "").strip() for value in values
        )
        if key
    ]
    canonical_set = set(canonical_keys)
    bound_candidate_keys = [
        key for key in candidate_keys if key in canonical_set
    ]
    schema["minItems"] = len(bound_candidate_keys)
    schema["maxItems"] = len(bound_candidate_keys)
    if bound_candidate_keys:
        schema["allOf"] = [
            {
                "contains": {"const": key},
                "minContains": 1,
                "maxContains": 1,
            }
            for key in bound_candidate_keys
        ]
    return schema


def _canonical_speaker_schema(
    canonical_keys: list[str],
    *,
    value: str | None | object,
) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    canonical_schema = {
        "type": "string",
        "enum": list(canonical_keys),
    }
    if value is _UNBOUND_SCHEMA_VALUE:
        return {
            "anyOf": [
                canonical_schema,
                {"type": "null"},
            ],
        }
    if str(value).strip() in canonical_keys:
        canonical_schema["enum"] = [str(value).strip()]
    return canonical_schema


_UNBOUND_SCHEMA_VALUE = object()


def _initial_participant_delivery_schema(
    canonical_keys: list[str],
) -> dict[str, Any]:
    if not canonical_keys:
        return {
            "type": "array",
            "items": False,
            "minItems": 0,
            "maxItems": 0,
        }
    return {
        "type": "array",
        "items": {
            "allOf": [
                {"$ref": "#/$defs/IRActionParticipantDelivery"},
                {
                    "type": "object",
                    "properties": {
                        "participant_key": {
                            "type": "string",
                            "enum": list(canonical_keys),
                        },
                    },
                    "required": ["participant_key"],
                },
            ],
        },
        "minItems": 0,
        "maxItems": len(canonical_keys),
    }


def _participant_delivery_repair_schema(
    participant_key: str,
    perception_channels: list[str],
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "participant_key": {
            "type": "string",
            "enum": [participant_key],
        },
        "observable_claim": {"type": "string", "minLength": 1},
    }
    properties.update({
        channel: {"const": channel in perception_channels}
        for channel in _PARTICIPANT_PERCEPTION_CHANNELS
    })
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": [
            "participant_key",
            "observable_claim",
            *_PARTICIPANT_PERCEPTION_CHANNELS,
        ],
        "additionalProperties": False,
        "x-perception-channels": list(perception_channels),
    }
    if not perception_channels:
        # Keep the required participant visible while forbidding invented
        # channels. The semantic gap must be repaired in the unit binding.
        schema["not"] = {}
        schema["x-evidence-gap"] = True
    return schema


def build_screenplay_scene_shard_repair_schema(
    shard: ScreenplaySceneShardIR | None = None,
    *,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> dict[str, Any]:
    """Build one contract-bound schema for initial and semantic attempts."""
    schema = deepcopy(ScreenplaySceneShardIR.model_json_schema())
    scene_schemas: list[dict[str, Any]] = []
    unit_contracts: list[dict[str, Any]] = []
    candidate_scenes = {
        scene.key: scene
        for scene in (shard.scenes if shard is not None else [])
    }

    for contract in scene_input_contracts:
        scene_key = contract.scene_plan_key
        canonical_keys = _scene_canonical_identity_keys(contract)
        canonical_set = set(canonical_keys)
        initial_unit_constraint = {
            "type": "object",
            "properties": {
                "actor_keys": _canonical_identity_array_schema(
                    canonical_keys,
                ),
                "target_keys": _canonical_identity_array_schema(
                    canonical_keys,
                ),
                "onscreen_entity_keys": _canonical_identity_array_schema(
                    canonical_keys,
                ),
                "speaker_key": _canonical_speaker_schema(
                    canonical_keys,
                    value=_UNBOUND_SCHEMA_VALUE,
                ),
                "participant_deliveries": (
                    _initial_participant_delivery_schema(canonical_keys)
                ),
            },
            "required": [
                "actor_keys",
                "target_keys",
                "onscreen_entity_keys",
                "speaker_key",
                "participant_deliveries",
            ],
        }
        candidate_scene = candidate_scenes.get(scene_key)
        units_schema: dict[str, Any] = {
            "type": "array",
            "items": {
                "allOf": [
                    {"$ref": "#/$defs/ScreenplaySceneShardUnit"},
                    initial_unit_constraint,
                ],
            },
        }
        if candidate_scene is not None:
            unit_schemas: list[dict[str, Any]] = []
            for unit_index, unit in enumerate(candidate_scene.units):
                relation_keys = [
                    key
                    for key in _unit_relation_participant_keys(unit)
                    if key in canonical_set
                ]
                onscreen_keys = [
                    key
                    for key in dict.fromkeys(
                        str(value or "").strip()
                        for value in unit.onscreen_entity_keys
                    )
                    if key in canonical_set
                ]
                onscreen_set = set(onscreen_keys)
                offscreen_keys = [
                    key for key in relation_keys if key not in onscreen_set
                ]
                required_deliveries: list[dict[str, Any]] = []
                item_schemas: list[dict[str, Any]] = []
                evidence_gaps: list[str] = []
                for participant_key in offscreen_keys:
                    channels = _unit_delivery_evidence_channels(
                        unit,
                        participant_key,
                    )
                    required_deliveries.append({
                        "participant_key": participant_key,
                        "perception_channels": channels,
                    })
                    item_schemas.append(
                        _participant_delivery_repair_schema(
                            participant_key,
                            channels,
                        )
                    )
                    if not channels:
                        evidence_gaps.append(participant_key)

                unit_constraint = deepcopy(initial_unit_constraint)
                unit_constraint["properties"].update({
                    "event_key": {"const": unit.event_key},
                    "source_segment_ids": {
                        "const": list(unit.source_segment_ids),
                    },
                    "actor_keys": _canonical_identity_array_schema(
                        canonical_keys,
                        values=unit.actor_keys,
                    ),
                    "target_keys": _canonical_identity_array_schema(
                        canonical_keys,
                        values=unit.target_keys,
                    ),
                    "onscreen_entity_keys": _canonical_identity_array_schema(
                        canonical_keys,
                        values=unit.onscreen_entity_keys,
                    ),
                    "speaker_key": _canonical_speaker_schema(
                        canonical_keys,
                        value=unit.speaker_key,
                    ),
                    "participant_deliveries": {
                        "type": "array",
                        "prefixItems": item_schemas,
                        "items": False,
                        "minItems": len(offscreen_keys),
                        "maxItems": len(offscreen_keys),
                    },
                })
                unit_constraint["required"] = [
                    *unit_constraint["required"],
                    "event_key",
                    "source_segment_ids",
                ]
                unit_schemas.append({
                    "allOf": [
                        {"$ref": "#/$defs/ScreenplaySceneShardUnit"},
                        unit_constraint,
                    ],
                })
                unit_contracts.append({
                    "scene_key": scene_key,
                    "unit_index": unit_index,
                    "event_key": unit.event_key,
                    "canonical_identity_keys": canonical_keys,
                    "relation_participant_keys": relation_keys,
                    "onscreen_entity_keys": onscreen_keys,
                    "required_deliveries": required_deliveries,
                    "evidence_gaps": evidence_gaps,
                })
            units_schema = {
                "type": "array",
                "prefixItems": unit_schemas,
                "items": False,
                "minItems": len(unit_schemas),
                "maxItems": len(unit_schemas),
            }

        scene_schemas.append({
            "allOf": [
                {"$ref": "#/$defs/ScreenplaySceneShardScene"},
                {
                    "type": "object",
                    "properties": {
                        "key": {"const": scene_key},
                        "units": units_schema,
                    },
                    "required": ["key", "units"],
                },
            ],
        })

    schema["properties"]["scenes"] = {
        "type": "array",
        "prefixItems": scene_schemas,
        "items": False,
        "minItems": len(scene_schemas),
        "maxItems": len(scene_schemas),
    }
    schema["x-schema-purpose"] = "scene-contract-bound"
    schema["x-scene-identity-contracts"] = [
        {
            "scene_key": contract.scene_plan_key,
            "canonical_identity_keys": (
                _scene_canonical_identity_keys(contract)
            ),
        }
        for contract in scene_input_contracts
    ]
    schema["x-unit-delivery-contracts"] = unit_contracts
    return schema


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


def _source_ownership_hash(blueprint: NarrativeBlueprint) -> str:
    return _hash({
        "source_scene_owners": blueprint.source_scene_owners,
        "scene_derivations": [
            relation.model_dump(mode="json")
            for relation in blueprint.scene_derivations
        ],
    })


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


def _identity_aliases(
    identity_registry: list[dict[str, Any]],
    *,
    identity_keys: set[str] | None = None,
) -> dict[str, str]:
    aliases = {key: key for key in (identity_keys or set())}
    for item in identity_registry:
        identity_key = str(item.get("identity_key") or "").strip()
        if not identity_key:
            continue
        for value in (
            identity_key,
            item.get("authority_id"),
            item.get("canonical_name"),
            *(item.get("source_labels") or []),
        ):
            label = str(value or "").strip()
            if label:
                aliases[label] = identity_key
    return aliases


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


def _screenplay_scene_shard_required_tokens(
    *,
    estimated_output_chars: int,
    estimated_units: int,
    scene_count: int,
) -> int:
    content_tokens = math.ceil(max(1, estimated_output_chars) / 1.5)
    structural_reserve = (
        max(1, scene_count) * SCREENPLAY_SCENE_SHARD_SCENE_RESERVE_TOKENS
        + max(1, estimated_units) * SCREENPLAY_SCENE_SHARD_UNIT_RESERVE_TOKENS
    )
    subtotal = content_tokens + structural_reserve
    return math.ceil(
        subtotal
        * (100 + SCREENPLAY_SCENE_SHARD_REASONING_RESERVE_PERCENT)
        / 100
    )


def screenplay_scene_shard_token_budget(
    plan: ScreenplaySceneShardPlan,
) -> int:
    """Return a bounded output budget derived from the shard structure."""
    required = _screenplay_scene_shard_required_tokens(
        estimated_output_chars=plan.estimated_output_chars,
        estimated_units=plan.estimated_units,
        scene_count=len(plan.scene_plan_keys),
    )
    return max(
        SCREENPLAY_SCENE_SHARD_MIN_OUTPUT_TOKENS,
        min(SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS, required),
    )


def _screenplay_scene_shard_budget_meta(
    plan: ScreenplaySceneShardPlan,
) -> dict[str, int | bool]:
    content_tokens = math.ceil(plan.estimated_output_chars / 1.5)
    structural_reserve = (
        len(plan.scene_plan_keys)
        * SCREENPLAY_SCENE_SHARD_SCENE_RESERVE_TOKENS
        + plan.estimated_units
        * SCREENPLAY_SCENE_SHARD_UNIT_RESERVE_TOKENS
    )
    required = _screenplay_scene_shard_required_tokens(
        estimated_output_chars=plan.estimated_output_chars,
        estimated_units=plan.estimated_units,
        scene_count=len(plan.scene_plan_keys),
    )
    return {
        "estimated_output_chars": plan.estimated_output_chars,
        "estimated_units": plan.estimated_units,
        "estimated_content_tokens": content_tokens,
        "structural_reserve_tokens": structural_reserve,
        "reasoning_reserve_tokens": (
            required - content_tokens - structural_reserve
        ),
        "required_output_tokens": required,
        "output_budget_tokens": screenplay_scene_shard_token_budget(plan),
        "output_budget_limited": (
            required > SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS
        ),
    }


def build_screenplay_scene_shard_plans(
    blueprint: NarrativeBlueprint,
    *,
    source_text: str,
    identity_registry_hash: str,
    max_units: int | None = None,
    max_output_chars: int | None = None,
) -> list[ScreenplaySceneShardPlan]:
    """Deterministically group consecutive Blueprint-owned scene plans."""
    derive_blueprint_scene_plans(blueprint)
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
    source_ownership_hash = _source_ownership_hash(blueprint)
    groups: list[list[BlueprintScenePlan]] = []
    current: list[BlueprintScenePlan] = []
    current_units = 0
    current_chars = 0
    current_domain = ""
    for plan in blueprint.scene_plans:
        units, output_chars = _scene_estimate(plan, source_by_id)
        candidate_required_tokens = _screenplay_scene_shard_required_tokens(
            estimated_output_chars=current_chars + output_chars,
            estimated_units=current_units + units,
            scene_count=len(current) + 1,
        )
        would_overflow = bool(current) and (
            current_units + units > max_units
            or current_chars + output_chars > max_output_chars
            or candidate_required_tokens
            > SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS
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
        group_scene_keys = [plan.key for plan in group]
        source_ids = [
            source_id
            for source_id, owner_scene_key
            in blueprint.source_scene_owners.items()
            if owner_scene_key in group_scene_keys
        ]
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
            scene_plan_keys=group_scene_keys,
            source_segment_ids=source_ids,
            source_scene_owners=dict(blueprint.source_scene_owners),
            derived_relations=[
                relation.model_copy(deep=True)
                for relation in blueprint.scene_derivations
                if relation.target_scene_plan_key in group_scene_keys
            ],
            source_ownership_hash=source_ownership_hash,
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


def build_screenplay_scene_input_contracts(
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: list[BlueprintScenePlan],
    source_by_id: dict[str, str],
    identity_registry: list[dict[str, Any]],
) -> list[ScreenplaySceneInputContract]:
    """Bind scene-owned source text and Blueprint participants before writing."""
    errors: list[str] = []
    scene_keys = [scene_plan.key for scene_plan in scene_plans]
    if scene_keys != plan.scene_plan_keys:
        errors.append(
            "逐场输入合同与 shard plan 场次不一致："
            f"expected={plan.scene_plan_keys}, actual={scene_keys}"
        )

    projected_source_ids = [
        source_id
        for source_id, owner_scene_key in plan.source_scene_owners.items()
        if owner_scene_key in scene_keys
    ]
    if projected_source_ids != plan.source_segment_ids:
        errors.append(
            "逐场输入合同的唯一 SRC 投影与 shard plan 不一致："
            f"expected={plan.source_segment_ids}, actual={projected_source_ids}"
        )

    aliases = _identity_aliases(identity_registry)
    contracts: list[ScreenplaySceneInputContract] = []
    for scene_plan in scene_plans:
        owned_source_ids = [
            source_id
            for source_id, owner_scene_key
            in plan.source_scene_owners.items()
            if owner_scene_key == scene_plan.key
        ]
        if scene_plan.source_segment_ids != owned_source_ids:
            conflicting_source_ids = [
                source_id
                for source_id in scene_plan.source_segment_ids
                if plan.source_scene_owners.get(source_id)
                != scene_plan.key
            ]
            if conflicting_source_ids:
                errors.extend(
                    f"{source_id} 唯一归属 "
                    f"{plan.source_scene_owners.get(source_id) or '未定义'}，"
                    f"不得由 {scene_plan.key} 消费"
                    for source_id in conflicting_source_ids
                )
            else:
                errors.append(
                    f"{scene_plan.key} source_segment_ids 与唯一 owner 投影不一致"
                )
        missing_source_ids = [
            source_id
            for source_id in owned_source_ids
            if source_id not in source_by_id
        ]
        if missing_source_ids:
            errors.append(
                f"{scene_plan.key} 输入合同缺少来源正文："
                + ",".join(missing_source_ids)
            )
        unresolved_participants = [
            participant
            for participant in scene_plan.participant_keys
            if participant not in aliases
        ]
        if unresolved_participants:
            errors.append(
                f"{scene_plan.key} Blueprint participant 未冻结："
                + ",".join(unresolved_participants)
            )
        contracts.append(ScreenplaySceneInputContract(
            scene_plan_key=scene_plan.key,
            node_keys=list(scene_plan.node_keys),
            source_segment_ids=owned_source_ids,
            source_segments=[
                ScreenplaySceneSourceSegment(
                    source_segment_id=source_id,
                    text=source_by_id[source_id],
                )
                for source_id in owned_source_ids
                if source_id in source_by_id
            ],
            participant_bindings=[
                ScreenplaySceneParticipantBinding(
                    blueprint_key=participant,
                    identity_key=aliases.get(participant, ""),
                )
                for participant in scene_plan.participant_keys
            ],
            source_scene_owners=dict(plan.source_scene_owners),
            derived_relations=[
                relation.model_copy(deep=True)
                for relation in plan.derived_relations
                if relation.target_scene_plan_key == scene_plan.key
            ],
            action_participant_delivery_contract=(
                ScreenplayActionParticipantDeliveryContract()
            ),
            source_ownership_hash=plan.source_ownership_hash,
        ))
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)
    return contracts


def build_screenplay_scene_input_contract_set(
    *,
    plans: list[ScreenplaySceneShardPlan],
    blueprint: NarrativeBlueprint,
    source_text: str,
    identity_registry: list[dict[str, Any]],
) -> dict[str, list[ScreenplaySceneInputContract]]:
    """Build the scene-owned contract once for generation, retry, and merge."""
    expected_ownership_hash = _source_ownership_hash(blueprint)
    scene_plan_map = {scene_plan.key: scene_plan for scene_plan in blueprint.scene_plans}
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    contracts: dict[str, list[ScreenplaySceneInputContract]] = {}
    for plan in plans:
        if (
            plan.source_scene_owners != blueprint.source_scene_owners
            or plan.source_ownership_hash != expected_ownership_hash
        ):
            raise ScreenplaySceneShardError(
                plan.shard_id,
                ["shard plan 的 source owner 合同与 Blueprint 不一致"],
            )
        missing_scene_keys = [
            scene_key for scene_key in plan.scene_plan_keys
            if scene_key not in scene_plan_map
        ]
        if missing_scene_keys:
            raise ScreenplaySceneShardError(
                plan.shard_id,
                ["逐场输入合同缺少 Blueprint scene plan：" + ",".join(missing_scene_keys)],
            )
        contracts[plan.shard_id] = build_screenplay_scene_input_contracts(
            plan=plan,
            scene_plans=[
                scene_plan_map[scene_key] for scene_key in plan.scene_plan_keys
            ],
            source_by_id=source_by_id,
            identity_registry=identity_registry,
        )
    return contracts


def _validate_scene_input_contracts(
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_keys: set[str],
) -> tuple[dict[str, ScreenplaySceneInputContract], list[str]]:
    errors: list[str] = []
    expected_plan_source_ids = [
        source_id
        for source_id, owner_scene_key in plan.source_scene_owners.items()
        if owner_scene_key in plan.scene_plan_keys
    ]
    if plan.source_segment_ids != expected_plan_source_ids:
        errors.append(
            "shard plan source_segment_ids 与唯一 owner 投影不一致"
        )
    invalid_relations = [
        relation.relation_key
        for relation in plan.derived_relations
        if (
            relation.target_scene_plan_key not in plan.scene_plan_keys
            or relation.source_scene_plan_key
            == relation.target_scene_plan_key
        )
    ]
    if invalid_relations:
        errors.append(
            "shard plan 含无效跨场派生关系："
            + ",".join(invalid_relations)
        )
    actual_scene_keys = [
        contract.scene_plan_key for contract in scene_input_contracts
    ]
    if actual_scene_keys != plan.scene_plan_keys:
        errors.append(
            "逐场参与者合同与 shard plan 不一致："
            f"expected={plan.scene_plan_keys}, actual={actual_scene_keys}"
        )
    contracts_by_scene: dict[str, ScreenplaySceneInputContract] = {}
    for contract in scene_input_contracts:
        if contract.scene_plan_key in contracts_by_scene:
            errors.append(
                "逐场参与者合同 scene_plan_key 必须唯一："
                + contract.scene_plan_key
            )
            continue
        contracts_by_scene[contract.scene_plan_key] = contract
    for scene_key in plan.scene_plan_keys:
        expected_scene = scene_plans.get(scene_key)
        contract = contracts_by_scene.get(scene_key)
        if expected_scene is None:
            errors.append(f"逐场参与者合同引用未知 scene：{scene_key}")
            continue
        if contract is None:
            errors.append(f"{scene_key} 缺少逐场参与者合同")
            continue
        if contract.node_keys != expected_scene.node_keys:
            errors.append(f"{scene_key} 逐场参与者合同 node_keys 与 Blueprint 不一致")
        expected_source_ids = [
            source_id
            for source_id, owner_scene_key
            in plan.source_scene_owners.items()
            if owner_scene_key == scene_key
        ]
        if expected_scene.source_segment_ids != expected_source_ids:
            conflicting_source_ids = [
                source_id
                for source_id in expected_scene.source_segment_ids
                if plan.source_scene_owners.get(source_id) != scene_key
            ]
            if conflicting_source_ids:
                errors.extend(
                    f"{source_id} 唯一归属 "
                    f"{plan.source_scene_owners.get(source_id) or '未定义'}，"
                    f"不得由 {scene_key} 消费"
                    for source_id in conflicting_source_ids
                )
            else:
                errors.append(
                    f"{scene_key} Blueprint source_segment_ids "
                    "与唯一 owner 投影不一致"
                )
        if contract.source_segment_ids != expected_source_ids:
            errors.append(
                f"{scene_key} 逐场参与者合同 source_segment_ids "
                "与唯一 owner 投影不一致"
            )
        contract_source_ids = [
            segment.source_segment_id
            for segment in contract.source_segments
        ]
        if contract_source_ids != expected_source_ids:
            errors.append(
                f"{scene_key} 逐场来源正文与唯一 owner 投影不一致"
            )
        if contract.source_scene_owners != plan.source_scene_owners:
            errors.append(
                f"{scene_key} 逐场 source owner 合同与 shard plan 不一致"
            )
        if contract.source_ownership_hash != plan.source_ownership_hash:
            errors.append(
                f"{scene_key} source_ownership_hash 与 shard plan 不一致"
            )
        expected_relations = [
            relation.model_dump(mode="json")
            for relation in plan.derived_relations
            if relation.target_scene_plan_key == scene_key
        ]
        actual_relations = [
            relation.model_dump(mode="json")
            for relation in contract.derived_relations
        ]
        if actual_relations != expected_relations:
            errors.append(
                f"{scene_key} 跨场派生关系与 shard plan 不一致"
            )
        expected_delivery_contract = (
            ScreenplayActionParticipantDeliveryContract()
        )
        if (
            contract.action_participant_delivery_contract
            != expected_delivery_contract
        ):
            errors.append(
                f"{scene_key} action participant delivery 合同与 "
                f"{IR_VERSION} 不一致"
            )
        expected_blueprint_keys = list(expected_scene.participant_keys)
        actual_blueprint_keys = [
            binding.blueprint_key for binding in contract.participant_bindings
        ]
        if actual_blueprint_keys != expected_blueprint_keys:
            errors.append(
                f"{scene_key} 逐场参与者合同 participant_bindings 与 Blueprint 不一致"
            )
        invalid_bindings = [
            binding.identity_key
            for binding in contract.participant_bindings
            if (
                not binding.identity_key
                or binding.identity_key not in identity_keys
            )
        ]
        if invalid_bindings:
            errors.append(
                f"{scene_key} 逐场参与者合同含未冻结 identity_key："
                + ",".join(invalid_bindings)
            )
    return contracts_by_scene, errors


_GENERIC_STORY_FUNCTION_LABELS = {
    "setup",
    "development",
    "complication",
    "turn",
    "climax",
    "resolution",
}


def normalize_screenplay_scene_shard_payload(
    payload: dict[str, Any],
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    blueprint: NarrativeBlueprint,
) -> dict[str, Any]:
    """Fill program-owned fields before Pydantic rejects a usable candidate."""
    normalized = deepcopy(payload)
    normalized.update({
        "episode_no": episode_no,
        "shard_id": plan.shard_id,
        "scene_plan_keys": list(plan.scene_plan_keys),
        "source_hash": plan.source_hash,
        "boundary_hash": plan.boundary_hash,
        "blueprint_hash": plan.blueprint_hash,
        "identity_registry_hash": plan.identity_registry_hash,
        "source_ownership_hash": plan.source_ownership_hash,
    })
    node_map = {node.key: node for node in blueprint.nodes}
    for scene in normalized.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        expected = scene_plans.get(str(scene.get("key") or ""))
        if expected is None:
            continue
        current = str(scene.get("story_function") or "").strip()
        if (
            len(current) >= SCENE_STORY_FUNCTION_MIN_CHARS
            and current.lower() not in _GENERIC_STORY_FUNCTION_LABELS
        ):
            continue
        summaries = [
            str(node_map[node_key].summary or node_map[node_key].action_logic)
            for node_key in expected.node_keys
            if node_key in node_map
            and str(
                node_map[node_key].summary
                or node_map[node_key].action_logic
            ).strip()
        ]
        if summaries:
            scene["story_function"] = "推进本场事件：" + "；".join(summaries)
    return normalized


def normalize_screenplay_scene_shard(
    shard: ScreenplaySceneShardIR,
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardIR:
    """Normalize program-owned fields without changing authored scene prose."""
    shard.episode_no = episode_no
    shard.shard_id = plan.shard_id
    shard.scene_plan_keys = list(plan.scene_plan_keys)
    shard.source_hash = plan.source_hash
    shard.boundary_hash = plan.boundary_hash
    shard.blueprint_hash = plan.blueprint_hash
    shard.identity_registry_hash = plan.identity_registry_hash
    shard.source_ownership_hash = plan.source_ownership_hash

    contracts_by_scene = {
        contract.scene_plan_key: contract
        for contract in scene_input_contracts
    }
    for scene in shard.scenes:
        contract = contracts_by_scene.get(scene.key)
        aliases = {
            value: binding.identity_key
            for binding in (
                contract.participant_bindings if contract is not None else []
            )
            for value in (binding.blueprint_key, binding.identity_key)
            if value
        }

        def normalize_refs(values: list[str]) -> list[str]:
            normalized: list[str] = []
            for value in values:
                label = str(value or "").strip()
                resolved = aliases.get(label, label)
                if resolved and resolved not in normalized:
                    normalized.append(resolved)
            return normalized

        scene.character_keys = normalize_refs(scene.character_keys)
        for unit in scene.units:
            if unit.kind == "dialogue" and unit.source_text.strip():
                unit.text = unit.source_text.strip()
            unit.actor_keys = normalize_refs(unit.actor_keys)
            unit.onscreen_entity_keys = normalize_refs(
                unit.onscreen_entity_keys
            )
            if unit.speaker_key:
                unit.speaker_key = aliases.get(
                    unit.speaker_key,
                    unit.speaker_key,
                )
            unit.target_keys = normalize_refs(unit.target_keys)
            for delivery in unit.participant_deliveries:
                delivery.participant_key = aliases.get(
                    delivery.participant_key,
                    delivery.participant_key,
                )
    consumed_owner: dict[str, str] = {}
    consumed_source_ids: list[str] = []
    for scene in shard.scenes:
        for unit in scene.units:
            for source_id in unit.source_segment_ids:
                previous_scene = consumed_owner.get(source_id)
                if previous_scene is None:
                    consumed_owner[source_id] = scene.key
                    consumed_source_ids.append(source_id)
    shard.consumed_source_ids = consumed_source_ids
    return shard


def validate_screenplay_scene_shard(
    shard: ScreenplaySceneShardIR,
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_keys: set[str],
    front_matter_ids: set[str] | None = None,
) -> list[str]:
    front_matter_ids = front_matter_ids or set()
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
    contracts_by_scene, contract_errors = _validate_scene_input_contracts(
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=scene_input_contracts,
        identity_keys=identity_keys,
    )
    errors.extend(contract_errors)
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
        contract = contracts_by_scene.get(scene.key)
        delivery_contract = (
            contract.action_participant_delivery_contract
            if contract is not None
            else ScreenplayActionParticipantDeliveryContract()
        )
        allowed_identity_keys = {
            binding.identity_key
            for binding in (
                contract.participant_bindings if contract is not None else []
            )
            if binding.identity_key
        }
        unowned_scene_characters = sorted(
            set(scene.character_keys) - allowed_identity_keys
        )
        if unowned_scene_characters:
            errors.append(
                f"{scene.key}.character_keys 违反逐场参与者合同："
                f"{unowned_scene_characters}"
            )
        for unit_index, unit in enumerate(scene.units):
            if not unit.source_segment_ids:
                errors.append(
                    f"{scene.key}.units[{unit_index}] "
                    "必须声明 source_segment_ids"
                )
            for source_id in unit.source_segment_ids:
                planned_owner = plan.source_scene_owners.get(source_id)
                if planned_owner != scene.key:
                    errors.append(
                        f"{scene.key}.units[{unit_index}] 来源唯一归属冲突："
                        f"{source_id} owner={planned_owner or '未定义'}，"
                        f"consumer={scene.key}"
                    )
                elif source_id not in actual_consumed:
                    actual_consumed.append(source_id)
            if (
                unit.speaker_key
                and unit.speaker_key not in allowed_identity_keys
            ):
                errors.append(
                    f"{scene.key}.units[{unit_index}] speaker_key "
                    "违反逐场参与者合同："
                    f"{unit.speaker_key}"
                )
            unowned_onscreen = sorted(
                set(unit.onscreen_entity_keys) - allowed_identity_keys
            )
            if unowned_onscreen:
                errors.append(
                    f"{scene.key}.units[{unit_index}] onscreen_entity_keys "
                    f"违反逐场参与者合同：{unowned_onscreen}"
                )
            unowned_action_relations = sorted(
                set([*unit.actor_keys, *unit.target_keys])
                - allowed_identity_keys
            )
            if unowned_action_relations:
                errors.append(
                    f"{scene.key}.units[{unit_index}] actor/target "
                    f"违反逐场参与者合同：{unowned_action_relations}"
                )
            relation_keys = {
                *unit.actor_keys,
                *unit.target_keys,
                *(
                    [unit.speaker_key]
                    if unit.kind == "dialogue" and unit.speaker_key
                    else []
                ),
            }
            if (
                delivery_contract.unit_field_required
                and "participant_deliveries" not in unit.model_fields_set
            ):
                errors.append(
                    f"{scene.key}.units[{unit_index}] 必须显式声明 "
                    "participant_deliveries"
                )
            delivery_keys: set[str] = set()
            for delivery in unit.participant_deliveries:
                participant_key = delivery.participant_key.strip()
                if participant_key in delivery_keys:
                    errors.append(
                        f"{scene.key}.units[{unit_index}] 对 "
                        f"{participant_key} 重复声明参与者交付"
                    )
                    continue
                delivery_keys.add(participant_key)
                if participant_key not in allowed_identity_keys:
                    errors.append(
                        f"{scene.key}.units[{unit_index}].participant_deliveries "
                        f"违反逐场参与者合同：{participant_key}"
                    )
                if participant_key not in relation_keys:
                    errors.append(
                        f"{scene.key}.units[{unit_index}] 的参与者交付 "
                        f"{participant_key} 不属于 actor/target/speaker"
                    )
                if participant_key in unit.onscreen_entity_keys:
                    errors.append(
                        f"{scene.key}.units[{unit_index}] 的参与者交付 "
                        f"{participant_key} 已在画面中"
                    )
                missing_claim = (
                    delivery_contract.observable_claim_required
                    and not delivery.observable_claim.strip()
                )
                missing_channel = (
                    delivery_contract.perceivable_channel_required
                    and not delivery.is_perceivable
                )
                if missing_claim or missing_channel:
                    errors.append(
                        f"{scene.key}.units[{unit_index}] 的参与者交付 "
                        f"{participant_key} 缺少结构化可感知证据"
                    )
            missing_deliveries = set()
            if delivery_contract.offscreen_relation_requires_evidence:
                missing_deliveries = (
                    relation_keys
                    - set(unit.onscreen_entity_keys)
                    - delivery_keys
                )
            if missing_deliveries:
                errors.append(
                    f"{scene.key}.units[{unit_index}] 未入画 actor/target/speaker "
                    "缺少 participant_deliveries："
                    f"{sorted(missing_deliveries)}"
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
            source_id
            for source_id, owner_scene_key
            in plan.source_scene_owners.items()
            if owner_scene_key == scene.key
            if source_id not in front_matter_ids
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
        ("source_ownership_hash", plan.source_ownership_hash),
    ):
        actual = str(getattr(shard, field) or "")
        if actual != expected:
            errors.append(f"{field} 不匹配")
    return errors


def _recover_scene_shard_from_provider_calls(
    *,
    operation_id: str,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    blueprint: NarrativeBlueprint,
    identity_keys: set[str],
    front_matter_ids: set[str],
) -> tuple[ScreenplaySceneShardIR, dict[str, Any]] | None:
    """Revalidate a complete prior response before issuing another paid call."""
    rows = get_conn().execute(
        """SELECT id,response_json
             FROM provider_calls
            WHERE operation_id=? AND status='OK' AND response_json IS NOT NULL
            ORDER BY id DESC LIMIT 10""",
        (operation_id,),
    ).fetchall()
    for row in rows:
        try:
            envelope = json.loads(row["response_json"])
            raw = str(envelope["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for payload in model_gateway._json_candidates(raw):
            try:
                normalized_payload = normalize_screenplay_scene_shard_payload(
                    payload,
                    episode_no=episode_no,
                    plan=plan,
                    scene_plans=scene_plans,
                    blueprint=blueprint,
                )
                shard = ScreenplaySceneShardIR.model_validate(
                    normalized_payload
                )
            except (TypeError, ValueError, ValidationError):
                continue
            normalize_screenplay_scene_shard(
                shard,
                episode_no=episode_no,
                plan=plan,
                scene_plans=scene_plans,
                scene_input_contracts=scene_input_contracts,
            )
            errors = validate_screenplay_scene_shard(
                shard,
                plan=plan,
                scene_plans=scene_plans,
                scene_input_contracts=scene_input_contracts,
                identity_keys=identity_keys,
                front_matter_ids=front_matter_ids,
            )
            if not errors:
                return shard, {
                    "outcome": "validated_provider_recovery",
                    "provider_call_id": int(row["id"]),
                    "local_recovery": True,
                    "validation_errors": [],
                }
    return None


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
    scene_input_contracts: dict[
        str, list[ScreenplaySceneInputContract]
    ],
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> ScreenplayGenerationIR:
    errors: list[str] = []
    expected_ownership_hash = _source_ownership_hash(blueprint)
    by_id = {shard.shard_id: shard for shard in shards}
    if len(by_id) != len(shards):
        errors.append("shard_id 必须全局唯一")
    if set(by_id) != {plan.shard_id for plan in plans}:
        errors.append("validated shards 与 shard plan 集合不一致")
    expected_blueprint_hash = blueprint_content_hash(blueprint)
    if envelope.blueprint_hash != expected_blueprint_hash:
        errors.append("Envelope blueprint_hash 不匹配")
    for plan in plans:
        if plan.blueprint_hash != expected_blueprint_hash:
            errors.append(f"{plan.shard_id} blueprint_hash 不匹配")
        if plan.source_scene_owners != blueprint.source_scene_owners:
            errors.append(f"{plan.shard_id} source owner 合同与 Blueprint 不一致")
        if plan.source_ownership_hash != expected_ownership_hash:
            errors.append(f"{plan.shard_id} source_ownership_hash 不匹配")
    expected_scenes = [plan.key for plan in blueprint.scene_plans]
    merged_scenes: list[IRScene] = []
    consumed: list[str] = []
    scene_plan_map = {plan.key: plan for plan in blueprint.scene_plans}
    identity_keys = {identity.key for identity in identities}
    segments = index_source_segments(source_text)
    front_matter_ids = structural_front_matter_ids(segments)
    for plan_index, plan in enumerate(plans):
        shard = by_id.get(plan.shard_id)
        if shard is None:
            continue
        errors.extend(validate_screenplay_scene_shard(
            shard,
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=scene_input_contracts.get(
                plan.shard_id, []
            ),
            identity_keys=identity_keys,
            front_matter_ids=front_matter_ids,
        ))
        if plan_index and plan.boundary_state_in != plans[plan_index - 1].boundary_state_out:
            errors.append(f"{plan.shard_id} boundary state 与前一 shard 不闭合")
        merged_scenes.extend(_namespace_shard_scene_keys(shard))
        consumed.extend(shard.consumed_source_ids)
    if [scene.key for scene in merged_scenes] != expected_scenes:
        errors.append("合并后 scene 顺序与 Blueprint 不一致")
    required_ids = [
        segment.segment_id for segment in segments
        if segment.segment_id not in front_matter_ids
    ]
    missing = [source_id for source_id in required_ids if source_id not in consumed]
    if missing:
        errors.append("合并 IR 未覆盖非标题 SRC：" + ",".join(missing))
    source_order = {source_id: index for index, source_id in enumerate(required_ids)}
    first_owned = []
    already: set[str] = set()
    actual_source_owners: dict[str, str] = {}
    for scene in merged_scenes:
        for unit in scene.units:
            for source_id in unit.source_segment_ids:
                expected_owner = blueprint.source_scene_owners.get(source_id)
                if expected_owner != scene.key:
                    errors.append(
                        f"{source_id} 唯一归属 "
                        f"{expected_owner or '未定义'}，"
                        f"不得由 {scene.key} 消费"
                    )
                previous_owner = actual_source_owners.get(source_id)
                if previous_owner is None:
                    actual_source_owners[source_id] = scene.key
                elif previous_owner != scene.key:
                    errors.append(
                        f"{source_id} 被 {previous_owner} 与 "
                        f"{scene.key} 跨场重复消费"
                    )
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
        source_scene_owners=dict(blueprint.source_scene_owners),
        scene_derivations=[
            relation.model_dump(mode="json")
            for relation in blueprint.scene_derivations
        ],
        source_ownership_hash=expected_ownership_hash,
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
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
    output_schema: dict[str, Any],
) -> str:
    plan_payload = plan.model_dump(mode="json")
    plan_payload["source_scene_owners"] = {
        source_id: plan.source_scene_owners[source_id]
        for source_id in plan.source_segment_ids
    }
    contract_payloads: list[dict[str, Any]] = []
    bound_identity_keys: list[str] = []
    for contract in scene_input_contracts:
        payload = contract.model_dump(mode="json")
        payload["source_scene_owners"] = {
            source_id: contract.source_scene_owners[source_id]
            for source_id in contract.source_segment_ids
        }
        contract_payloads.append(payload)
        bound_identity_keys.extend(
            binding.identity_key for binding in contract.participant_bindings
        )
    registry_by_key = {
        str(item.get("identity_key") or ""): item
        for item in identity_registry
    }
    projected_identity_registry = [
        registry_by_key[identity_key]
        for identity_key in dict.fromkeys(bound_identity_keys)
        if identity_key in registry_by_key
    ]
    return (
        "任务：只写指定 Blueprint 场次的紧凑语义 IR。场 key、heading、顺序和来源所有权"
        "均由程序拥有，不得改名、跨场挪 SRC 或输出整集 metadata/experience/events/beats/coverage。"
        "逐场输入合同已把每段来源正文和参与者冻结身份绑定到唯一 scene_plan_key；"
        "跨场状态、前置决定与转场信息只能读取 derived_relations，绝不能再次消费其来源场 SRC；"
        "每个 unit.source_segment_ids 必须非空且属于当前 scene 的 source_segment_ids，"
        "参与者字段必须使用当前 scene participant_bindings 中的 identity_key。"
        "每个非标题来源必须由至少一个 action/dialogue unit 消费。dialogue.text 与 "
        "dialogue.source_text 必须填写同一段逐字原文对白，不得把剧情摘要、转述或扩写写进 "
        "text；表演和动作另写 action unit。speaker_key 只能逐字引用冻结 identity_key。"
        "冻结表已完成蓝图参与者收口：actor_keys、target_keys、onscreen_entity_keys 和 "
        "speaker_key 只能使用冻结表中的 identity_key，禁止生成 unresolved_* 占位 ID。"
        "确实无法绑定时只能记录到 unresolved_participants，且该候选会被退回上游身份收口，"
        "不得把未冻结值同时塞入任何关系字段。每个 unit 的 "
        "actor_keys/target_keys 只能填写当前 unit 的实际动作执行者与受作用对象；"
        "onscreen_entity_keys 只能填写这一动作或话轮当下实际在画面中的冻结 identity_key；"
        "被台词提到、仅能听见或只感知事件的身份不得因此进入该列表。复杂动作可在同一 local event_key"
        "下写有序 units。每个 unit 必须显式输出 participant_deliveries，确无画外关系时输出空数组。"
        "actor/target/speaker 未进入 onscreen_entity_keys 时，必须在同一 unit 的 "
        "participant_deliveries 中填写 participant_key、observable_claim，并按实际证据设置 "
        "audible、visible_effect、visible_reaction 至少一项；不得只把身份塞入画外分区，"
        "也不得为通过校验编造可听、可见影响或可见反应。\n"
        "输出根结构硬合同：根对象必须是完整 ScreenplaySceneShardIR，第一层必须包含 "
        "contract_version、episode_no、shard_id、scene_plan_keys、scenes、"
        "consumed_source_ids、unresolved_participants、source_hash、boundary_hash、"
        "blueprint_hash、identity_registry_hash、source_ownership_hash。"
        "绝不能把单个 scene、unit、数组或解释文字"
        "作为根输出。scenes 必须按 scene_plan_keys 恰好各输出一次；consumed_source_ids "
        "必须等于 units 实际首次消费的 SRC 顺序并集。\nShard plan：\n"
        + json.dumps(
            plan_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\nBlueprint scene plans：\n"
        + json.dumps(
            [value.model_dump(mode="json") for value in blueprint_scene_plans],
            ensure_ascii=False, separators=(",", ":"),
        )
        + "\n相关 Blueprint nodes：\n"
        + json.dumps(blueprint_nodes, ensure_ascii=False, separators=(",", ":"))
        + "\n冻结 identity registry：\n"
        + json.dumps(
            projected_identity_registry,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n逐场输入合同（来源正文不得跨 scene_plan_key 使用）：\n"
        + json.dumps(
            contract_payloads,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n只输出 Schema 对象：\n"
        + json.dumps(output_schema, ensure_ascii=False)
        + f"\n固定字段：episode_no={episode_no}, shard_id={plan.shard_id},"
        f" source_hash={plan.source_hash}, boundary_hash={plan.boundary_hash},"
        f" blueprint_hash={plan.blueprint_hash},"
        f" identity_registry_hash={plan.identity_registry_hash},"
        f" source_ownership_hash={plan.source_ownership_hash}"
    )


async def generate_screenplay_scene_shards(
    *,
    episode: dict[str, Any],
    source_text: str,
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict[str, Any]],
    identities: list[IRIdentity],
    plans: list[ScreenplaySceneShardPlan],
    scene_input_contracts: dict[
        str, list[ScreenplaySceneInputContract]
    ],
    blueprint_artifact_id: str | None = None,
    identity_artifact_id: str | None = None,
    progress: Callable[[list[dict[str, Any]]], Any] | None = None,
) -> tuple[list[ScreenplaySceneShardIR], list[str], list[dict[str, Any]]]:
    """Generate/reuse independent shards with a per-episode concurrency cap."""
    episode_id = str(episode.get("id") or f"episode-{episode['episode_no']}")
    scene_plan_map = {plan.key: plan for plan in blueprint.scene_plans}
    source_segments = index_source_segments(source_text)
    front_matter_ids = structural_front_matter_ids(source_segments)
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
            "source_ownership_hash": plan.source_ownership_hash,
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
                    "source_ownership_hash",
                )
            ) and content.get("contract_version") == SCREENPLAY_SCENE_SHARD_VERSION,
        )
        if cached:
            try:
                shard = ScreenplaySceneShardIR.model_validate(cached["content"])
            except ValidationError:
                shard = None
            if shard is not None:
                normalize_screenplay_scene_shard(
                    shard,
                    episode_no=int(episode["episode_no"]),
                    plan=plan,
                    scene_plans=scene_plan_map,
                    scene_input_contracts=scene_input_contracts.get(
                        plan.shard_id, []
                    ),
                )
                errors = validate_screenplay_scene_shard(
                    shard,
                    plan=plan,
                    scene_plans=scene_plan_map,
                    scene_input_contracts=scene_input_contracts.get(
                        plan.shard_id, []
                    ),
                    identity_keys=identity_keys,
                    front_matter_ids=front_matter_ids,
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
        plan_scene_input_contracts = scene_input_contracts.get(
            plan.shard_id, []
        )
        repair_contracts: list[dict[str, Any]] = []
        for contract in plan_scene_input_contracts:
            payload = contract.model_dump(mode="json")
            payload["source_scene_owners"] = {
                source_id: contract.source_scene_owners[source_id]
                for source_id in contract.source_segment_ids
            }
            repair_contracts.append(payload)
        output_schema = build_screenplay_scene_shard_repair_schema(
            scene_input_contracts=plan_scene_input_contracts,
        )
        prompt = _scene_shard_prompt(
            episode_no=int(episode["episode_no"]),
            plan=plan,
            blueprint_scene_plans=selected_scene_plans,
            blueprint_nodes=[
                node.model_dump(mode="json")
                for node in blueprint.nodes if node.key in selected_node_keys
            ],
            scene_input_contracts=plan_scene_input_contracts,
            identity_registry=identity_registry,
            output_schema=output_schema,
        )
        operation_id = (
            f"screenplay.scene-shard:{SCREENPLAY_SCENE_SHARD_VERSION}:"
            f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
            f"{episode_id}:{plan.shard_id}:{plan.source_hash}:"
            f"{plan.boundary_hash}:{plan.blueprint_hash}:"
            f"{plan.identity_registry_hash}:{plan.source_ownership_hash}"
        )

        def normalize_payload(value: dict[str, Any]) -> dict[str, Any]:
            return normalize_screenplay_scene_shard_payload(
                value,
                episode_no=int(episode["episode_no"]),
                plan=plan,
                scene_plans=scene_plan_map,
                blueprint=blueprint,
            )

        def validate_shard(value: ScreenplaySceneShardIR) -> list[str]:
            normalize_screenplay_scene_shard(
                value,
                episode_no=int(episode["episode_no"]),
                plan=plan,
                scene_plans=scene_plan_map,
                scene_input_contracts=plan_scene_input_contracts,
            )
            return validate_screenplay_scene_shard(
                value,
                plan=plan,
                scene_plans=scene_plan_map,
                scene_input_contracts=plan_scene_input_contracts,
                identity_keys=identity_keys,
                front_matter_ids=front_matter_ids,
            )

        def repair_schema(
            value: ScreenplaySceneShardIR,
        ) -> dict[str, Any]:
            return build_screenplay_scene_shard_repair_schema(
                value,
                scene_input_contracts=plan_scene_input_contracts,
            )

        attempts: list[dict[str, Any]] = []
        recovered = _recover_scene_shard_from_provider_calls(
            operation_id=operation_id,
            episode_no=int(episode["episode_no"]),
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=plan_scene_input_contracts,
            blueprint=blueprint,
            identity_keys=identity_keys,
            front_matter_ids=front_matter_ids,
        )
        if recovered is not None:
            shard, recovery_attempt = recovered
            attempts.append(recovery_attempt)
        else:
            async with semaphore:
                checkpoint_rows[plan.shard_id].update({
                    "status": "running", "attempt": 1,
                })
                emit_progress()
                budget_meta = _screenplay_scene_shard_budget_meta(plan)
                shard = await model_gateway.chat_structured(
                    [{"role": "user", "content": prompt}],
                    model_type=ScreenplaySceneShardIR,
                    validate=validate_shard,
                    operation_id=operation_id,
                    max_tokens=screenplay_scene_shard_token_budget(plan),
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
                        **budget_meta,
                    },
                    repair_context=json.dumps({
                        "root_contract": {
                            "contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
                            "episode_no": int(episode["episode_no"]),
                            "shard_id": plan.shard_id,
                            "scene_plan_keys": list(plan.scene_plan_keys),
                            "required_root_fields": [
                                "contract_version", "episode_no", "shard_id",
                                "scene_plan_keys", "scenes",
                                "consumed_source_ids",
                                "unresolved_participants", "source_hash",
                                "boundary_hash", "blueprint_hash",
                                "identity_registry_hash",
                                "source_ownership_hash",
                            ],
                            "root_must_not_be": "single_scene_or_unit",
                        },
                        "scene_input_contracts": repair_contracts,
                        "action_participant_delivery_contract": (
                            ScreenplayActionParticipantDeliveryContract()
                            .model_dump(mode="json")
                        ),
                        "final_gate_contract": [
                            "each unit must declare non-empty source_segment_ids",
                            "each source_id must resolve to exactly one scene owner",
                            "each unit source_segment_ids must match that owner",
                            "cross-scene context must use derived_relations without consuming source again",
                            "all non-title scene-owned SRC must be consumed",
                            "all identity relations must use the current scene participant_bindings",
                            "every offscreen actor/target/speaker must have structured participant_deliveries evidence",
                            "dialogue text must equal exact source_text",
                            "unresolved placeholders are forbidden in relation fields",
                        ],
                    }, ensure_ascii=False, separators=(",", ":")),
                    output_schema=output_schema,
                    repair_schema=repair_schema,
                    normalize_payload=normalize_payload,
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
