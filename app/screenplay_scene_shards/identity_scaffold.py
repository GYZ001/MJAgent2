"""Identity-scaffold hashing and unit-slot compilation: the content hashes that
pin a scene-shard generation call to its exact input contract, the repair
schema builder, and ``_compile_unit_identity_scaffold`` which derives the
per-unit actor/target/speaker/state-subject scaffold the model must honor.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

from app.schemas import ActionAgency
from app.screenplay_ir import IRActionParticipantDelivery
from copy import deepcopy
from typing import Any

from .common import _hash
from .constants import (
    SCREENPLAY_SCENE_CREATIVE_VERSION,
    SCREENPLAY_SCENE_INPUT_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
    SCREENPLAY_SHARD_PLAN_VERSION,
    _ATTRIBUTED_TEXT_DELIVERY_MODES,
)
from .models import (
    ScreenplaySceneCompiledUnitSlot,
    ScreenplaySceneInputContract,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardPlan,
    ScreenplaySceneUnitSlotPlan,
)


def _contract_identity_scaffold_hash(
    contract: ScreenplaySceneInputContract,
) -> str:
    return _hash({
        "contract_version": SCREENPLAY_SCENE_INPUT_VERSION,
        "scene_plan_key": contract.scene_plan_key,
        "source_segment_ids": contract.source_segment_ids,
        "source_semantics": {
            source_id: semantics.model_dump(mode="json")
            for source_id, semantics in contract.source_semantics.items()
        },
        "participant_bindings": [
            binding.model_dump(mode="json")
            for binding in contract.participant_bindings
        ],
        "action_evidence": [
            evidence.model_dump(mode="json")
            for evidence in contract.action_evidence
        ],
        "unit_slots": [
            slot.model_dump(mode="json")
            for slot in contract.unit_slots
        ],
    })


def screenplay_scene_identity_scaffold_hash(
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> str:
    return _hash({
        "contract_version": SCREENPLAY_SCENE_INPUT_VERSION,
        "scenes": [
            {
                "scene_plan_key": contract.scene_plan_key,
                "identity_scaffold_hash": (
                    contract.identity_scaffold_hash
                    or _contract_identity_scaffold_hash(contract)
                ),
            }
            for contract in scene_input_contracts
        ],
    })


def screenplay_scene_generation_scaffold_hash(
    plan: ScreenplaySceneShardPlan,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> str:
    return _hash({
        "shard_contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
        "plan_contract_version": SCREENPLAY_SHARD_PLAN_VERSION,
        "input_contract_version": SCREENPLAY_SCENE_INPUT_VERSION,
        "creative_contract_version": SCREENPLAY_SCENE_CREATIVE_VERSION,
        "shard_id": plan.shard_id,
        "scene_plan_keys": plan.scene_plan_keys,
        "source_segment_ids": plan.source_segment_ids,
        "source_scene_owners": plan.source_scene_owners,
        "unit_slots": [
            slot.model_dump(mode="json")
            for slot in plan.unit_slots
        ],
        "scene_contracts": [
            {
                "scene_plan_key": contract.scene_plan_key,
                "identity_scaffold_hash": (
                    contract.identity_scaffold_hash
                    or _contract_identity_scaffold_hash(contract)
                ),
                "unit_slots": [
                    slot.model_dump(mode="json")
                    for slot in contract.unit_slots
                ],
            }
            for contract in scene_input_contracts
        ],
    })


def build_screenplay_scene_shard_repair_schema(
    shard: ScreenplaySceneShardIR | None = None,
    *,
    plan: ScreenplaySceneShardPlan,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> dict[str, Any]:
    """Build one closed slot-content schema for initial and repair attempts."""
    del shard
    schema = deepcopy(ScreenplaySceneShardCreativeIR.model_json_schema())
    slot_schemas: dict[str, dict[str, Any]] = {}
    for slot in plan.unit_slots:
        constraints: dict[str, Any] = {"type": "object"}
        if slot.kind == "dialogue":
            constraints["properties"] = {
                "text": {"const": slot.source_text},
            }
        slot_schemas[slot.unit_key] = {
            "allOf": [
                {"$ref": "#/$defs/ScreenplaySceneShardCreativeUnit"},
                constraints,
            ],
        }
    slot_keys = [slot.unit_key for slot in plan.unit_slots]
    schema["properties"]["slots"] = {
        "type": "object",
        "properties": slot_schemas,
        "required": slot_keys,
        "additionalProperties": False,
        "minProperties": len(slot_keys),
        "maxProperties": len(slot_keys),
    }
    schema["x-schema-purpose"] = (
        "creative-content-for-deterministic-generation-slots"
    )
    schema["x-identity-scaffold-hash"] = (
        screenplay_scene_identity_scaffold_hash(scene_input_contracts)
    )
    schema["x-generation-scaffold-hash"] = (
        screenplay_scene_generation_scaffold_hash(
            plan,
            scene_input_contracts,
        )
    )
    return schema


def _ordered_unique(values: list[str]) -> list[str]:
    return [
        value
        for value in dict.fromkeys(
            str(item or "").strip() for item in values
        )
        if value
    ]


def _compile_unit_identity_scaffold(
    slot: ScreenplaySceneUnitSlotPlan,
    *,
    contract: ScreenplaySceneInputContract,
) -> tuple[ScreenplaySceneCompiledUnitSlot, list[str]]:
    errors: list[str] = []
    source_ids = _ordered_unique(slot.source_segment_ids)
    source_set = set(source_ids)
    participant_usages: dict[str, set[str]] = {}
    participant_channels: dict[str, list[str]] = {}
    scene_visible_keys: set[str] = set()
    voice_claims: list[str] = []
    decision_actor_keys: list[str] = []
    state_subject_claims: list[str] = []
    joint_state_subject_claims: list[list[str]] = []
    exact_decision_actor_keys: list[str] = []
    environment_only = False
    source_text_by_id = {
        segment.source_segment_id: segment.text
        for segment in contract.source_segments
    }

    for action in contract.action_evidence:
        if not source_set.intersection(action.source_segment_ids):
            continue
        if slot.source_unit_key in action.environment_source_unit_keys:
            environment_only = True
        for assignment in action.state_subject_assignments:
            if assignment.source_unit_key == slot.source_unit_key:
                joint_state_subject_claims.append(
                    _ordered_unique(assignment.identity_keys)
                )
        for participant in action.participants:
            if not source_set.intersection(
                participant.source_segment_ids
            ):
                continue
            if participant.usage == "visible":
                scene_visible_keys.add(participant.identity_key)
            if (
                participant.usage == "voice"
                and not participant.source_unit_keys
                and slot.kind == "dialogue"
            ):
                errors.append(
                    f"{slot.unit_key} voice identity evidence "
                    "缺少精确 source_unit_keys"
                )
                continue
            if (
                participant.usage == "state_subject"
                and not participant.source_unit_keys
            ):
                errors.append(
                    f"{slot.unit_key} state_subject identity evidence "
                    "缺少精确 source_unit_keys"
                )
                continue
            if (
                participant.source_unit_keys
                and slot.source_unit_key
                not in participant.source_unit_keys
            ):
                continue
            participant_usages.setdefault(
                participant.identity_key,
                set(),
            ).add(participant.usage)
            if participant.usage == "voice":
                voice_claims.append(participant.identity_key)
            if (
                participant.usage == "state_subject"
                and participant.identity_key not in state_subject_claims
            ):
                state_subject_claims.append(participant.identity_key)
            channels = participant_channels.setdefault(
                participant.identity_key,
                [],
            )
            for channel in participant.perception_channels:
                if channel not in channels:
                    channels.append(channel)
        if (
            action.decision_actor_key
            and action.decision_actor_key in participant_usages
            and action.decision_actor_key not in decision_actor_keys
        ):
            decision_actor_keys.append(action.decision_actor_key)
            if any(
                participant.identity_key == action.decision_actor_key
                and slot.source_unit_key in participant.source_unit_keys
                for participant in action.participants
            ):
                exact_decision_actor_keys.append(action.decision_actor_key)

    visible_keys = [
        identity_key
        for identity_key, usages in participant_usages.items()
        if "visible" in usages
    ]
    voice_keys = [
        identity_key
        for identity_key, usages in participant_usages.items()
        if "voice" in usages
    ]
    speaker_key: str | None = None
    actor_keys: list[str] = []
    target_keys: list[str] = []

    if slot.kind == "dialogue":
        if len(voice_claims) == 1 and voice_claims[0]:
            speaker_key = voice_claims[0]
        elif len(voice_claims) > 1:
            errors.append(
                f"{slot.unit_key} dialogue source unit 含多个 voice "
                "speaker evidence"
            )
        else:
            errors.append(
                f"{slot.unit_key} dialogue 缺少唯一 speaker "
                "voice identity evidence"
            )
        if speaker_key:
            actor_keys = [speaker_key]
            if decision_actor_keys == [speaker_key]:
                target_keys = [
                    key for key in visible_keys if key != speaker_key
                ]
    else:
        if len(decision_actor_keys) > 1:
            errors.append(
                f"{slot.unit_key} 来源含多个 decision actor，"
                "必须在 Blueprint 中拆分来源动作"
            )
        elif decision_actor_keys:
            actor_keys = list(decision_actor_keys)
            target_keys = [
                key for key in visible_keys
                if key not in decision_actor_keys
            ]
        source_has_dialogue_slot = slot.kind == "dialogue"
        if voice_keys and not source_has_dialogue_slot:
            if len(voice_keys) == 1:
                speaker_key = voice_keys[0]
            else:
                errors.append(
                    f"{slot.unit_key} 无对白结构的来源含多个 voice identity，"
                    "必须在 Blueprint 中拆分来源"
                )

    typed_actor_claims = _ordered_unique(exact_decision_actor_keys)
    unspoken_owner_key = (
        slot.content_owner_key
        if (
            slot.delivery_mode == "unspoken_reference"
            and slot.content_owner_key
        )
        else ""
    )
    if (
        unspoken_owner_key
        and unspoken_owner_key in scene_visible_keys
        and unspoken_owner_key not in visible_keys
    ):
        visible_keys.append(unspoken_owner_key)
    if unspoken_owner_key:
        if joint_state_subject_claims:
            if (
                len(joint_state_subject_claims) != 1
                or joint_state_subject_claims[0]
                != [unspoken_owner_key]
            ):
                errors.append(
                    f"{slot.unit_key} unspoken content owner 与 joint "
                    "state_subject 冲突"
                )
        elif state_subject_claims:
            if set(state_subject_claims) != {unspoken_owner_key}:
                errors.append(
                    f"{slot.unit_key} unspoken content owner 与 single "
                    "state_subject 冲突"
                )
        else:
            state_subject_claims.append(unspoken_owner_key)
    state_subject_keys: list[str] = []
    state_subject_key = ""
    if len(joint_state_subject_claims) > 1:
        errors.append(
            f"{slot.unit_key} 存在多个 joint state_subject assignment"
        )
    elif joint_state_subject_claims and state_subject_claims:
        errors.append(
            f"{slot.unit_key} 同时声明 single 与 joint state_subject"
        )
    elif joint_state_subject_claims:
        state_subject_keys = joint_state_subject_claims[0]
    elif len(state_subject_claims) > 1:
        errors.append(
            f"{slot.unit_key} 存在多个 state_subject identity evidence："
            f"{state_subject_claims}"
        )
    elif state_subject_claims:
        state_subject_keys = [state_subject_claims[0]]
    elif slot.kind == "dialogue" and speaker_key:
        state_subject_keys = [speaker_key]
    elif len(typed_actor_claims) == 1:
        state_subject_keys = [typed_actor_claims[0]]
    elif len(typed_actor_claims) > 1:
        errors.append(
            f"{slot.unit_key} 存在多个 exact-unit typed actor "
            f"{typed_actor_claims}，必须由 Blueprint "
            "usage=state_subject 唯一冻结"
        )
    if len(state_subject_keys) == 1:
        state_subject_key = state_subject_keys[0]
    if (
        slot.kind == "action"
        and state_subject_keys
    ):
        actor_keys = _ordered_unique([*actor_keys, *state_subject_keys])
    if environment_only and (
        state_subject_keys
        or state_subject_claims
        or joint_state_subject_claims
        or typed_actor_claims
        or (slot.kind == "dialogue" and speaker_key)
    ):
        errors.append(
            f"{slot.unit_key} 同时声明人物主体与 environment_only"
        )
    elif (
        not environment_only
        and not state_subject_keys
        and not (
            slot.delivery_mode in _ATTRIBUTED_TEXT_DELIVERY_MODES
            and slot.content_owner_key
        )
    ):
        errors.append(
            f"{slot.unit_key} 缺少 single/joint state_subject 结构证据，"
            "且未显式声明 environment_only"
        )
    relation_keys = _ordered_unique([
        *actor_keys,
        *target_keys,
        *([speaker_key] if speaker_key else []),
    ])
    participant_deliveries: list[IRActionParticipantDelivery] = []
    observable_basis = slot.source_text.strip() or " ".join(
        source_text_by_id.get(source_id, "")
        for source_id in source_ids
    ).strip()
    for participant_key in relation_keys:
        if participant_key in visible_keys:
            continue
        channels = participant_channels.get(participant_key, [])
        if not channels:
            errors.append(
                f"{slot.unit_key} 画外参与者 "
                f"{participant_key} 缺少确定性可感知通道"
            )
            continue
        participant_deliveries.append(IRActionParticipantDelivery(
            participant_key=participant_key,
            observable_claim=(
                f"{participant_key} 通过 {','.join(channels)} 交付来源 "
                f"{','.join(source_ids)}：{observable_basis[:120]}"
            ),
            audible="audible" in channels,
            visible_effect="visible_effect" in channels,
            visible_reaction="visible_reaction" in channels,
        ))

    return ScreenplaySceneCompiledUnitSlot(
        **slot.model_dump(
            mode="python",
            exclude={
                "state_subject_key",
                "state_subject_keys",
                "environment_only",
            },
        ),
        actor_keys=actor_keys,
        target_keys=target_keys,
        onscreen_entity_keys=visible_keys,
        participant_deliveries=participant_deliveries,
        speaker_key=speaker_key,
        state_subject_key=state_subject_key,
        state_subject_keys=state_subject_keys,
        environment_only=environment_only,
        action_agency=ActionAgency(
            kind="character" if relation_keys else "unattributed",
            identity_bearing=bool(relation_keys),
            source_segment_ids=source_ids,
        ),
    ), errors


def _structural_slot(
    slot: ScreenplaySceneCompiledUnitSlot,
) -> ScreenplaySceneUnitSlotPlan:
    compiler_owned = {
        "state_subject_key",
        "state_subject_keys",
        "environment_only",
    }
    return ScreenplaySceneUnitSlotPlan.model_validate(
        slot.model_dump(
            mode="python",
            include=(
                set(ScreenplaySceneUnitSlotPlan.model_fields)
                - compiler_owned
            ),
        )
    )
