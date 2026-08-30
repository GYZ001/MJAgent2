"""Compiles a provider-authored creative draft into the structural scene-shard
IR: text-provenance classification and
``compile_screenplay_scene_shard_draft``, which merges compiled unit slots
with the creative units the provider returned.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

from app.narrative_blueprint import BlueprintScenePlan
from app.schemas import (
    ActionAgency,
    TextProvenance,
)
from copy import deepcopy

from .identity_registry import ScreenplaySceneShardError
from .identity_scaffold import (
    _ordered_unique,
    _structural_slot,
    screenplay_scene_generation_scaffold_hash,
    screenplay_scene_identity_scaffold_hash,
)
from .models import (
    ScreenplaySceneCompiledUnitSlot,
    ScreenplaySceneInputContract,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardCreativeUnit,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardPlan,
    ScreenplaySceneShardScene,
    ScreenplaySceneShardUnit,
)


def _compile_text_provenance(
    *,
    creative_unit: ScreenplaySceneShardCreativeUnit,
    compiled_slot: ScreenplaySceneCompiledUnitSlot,
) -> tuple[TextProvenance, str]:
    if creative_unit.required_text.strip():
        provenance_kind = "required_text"
    elif creative_unit.prop_text.strip():
        provenance_kind = "prop_text"
    elif creative_unit.on_screen_text.strip():
        provenance_kind = "on_screen_text"
    elif compiled_slot.kind == "dialogue":
        provenance_kind = "dialogue"
    else:
        provenance_kind = "creative_action"
    relation_identity_keys = _ordered_unique([
        *compiled_slot.actor_keys,
        *compiled_slot.target_keys,
        *(
            [compiled_slot.speaker_key]
            if compiled_slot.speaker_key
            else []
        ),
    ])
    provenance = TextProvenance(
        kind=provenance_kind,
        identity_keys=(
            []
            if provenance_kind in (
                "required_text", "prop_text", "on_screen_text",
            )
            else relation_identity_keys
        ),
        content_owner_keys=(
            [compiled_slot.content_owner_key]
            if compiled_slot.content_owner_key
            else []
        ),
        source_segment_ids=list(compiled_slot.source_segment_ids),
    )
    agency_kind = (
        compiled_slot.action_agency.kind
        if relation_identity_keys
        else provenance_kind
        if provenance_kind in (
            "required_text", "prop_text", "on_screen_text",
        )
        else "unattributed"
    )
    return provenance, agency_kind


def compile_screenplay_scene_shard_draft(
    draft: ScreenplaySceneShardCreativeIR,
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardIR:
    """Join creative slot content to the immutable generation scaffold."""
    errors: list[str] = []
    expected_slot_keys = [
        slot.unit_key for slot in plan.unit_slots
    ]
    actual_slot_keys = set(draft.slots)
    missing_slot_keys = [
        unit_key
        for unit_key in expected_slot_keys
        if unit_key not in actual_slot_keys
    ]
    extra_slot_keys = sorted(
        actual_slot_keys - set(expected_slot_keys)
    )
    if missing_slot_keys:
        errors.append(
            "[GENERATION_CONTRACT] 缺失 slot："
            + ",".join(missing_slot_keys)
        )
    if extra_slot_keys:
        errors.append(
            "[GENERATION_CONTRACT] 多余 slot："
            + ",".join(extra_slot_keys)
        )
    for planned_slot in plan.unit_slots:
        creative_unit = draft.slots.get(planned_slot.unit_key)
        if (
            creative_unit is not None
            and planned_slot.kind == "dialogue"
            and creative_unit.text.strip() != planned_slot.source_text.strip()
        ):
            errors.append(
                f"{planned_slot.unit_key} dialogue.text 必须等于 "
                "scaffold source_text"
            )

    contracts_by_scene = {
        contract.scene_plan_key: contract
        for contract in scene_input_contracts
    }
    compiled_slots_by_key: dict[
        str, ScreenplaySceneCompiledUnitSlot
    ] = {}
    plan_slots_by_key = {
        slot.unit_key: slot for slot in plan.unit_slots
    }
    for contract in scene_input_contracts:
        for compiled_slot in contract.unit_slots:
            if compiled_slot.unit_key in compiled_slots_by_key:
                errors.append(
                    "[GENERATION_CONTRACT] unit_key 重复："
                    + compiled_slot.unit_key
                )
                continue
            compiled_slots_by_key[compiled_slot.unit_key] = compiled_slot
            planned_slot = plan_slots_by_key.get(compiled_slot.unit_key)
            if planned_slot is None:
                errors.append(
                    "[GENERATION_CONTRACT] 输入合同含未计划 slot："
                    + compiled_slot.unit_key
                )
            elif _structural_slot(compiled_slot) != planned_slot:
                errors.append(
                    "[GENERATION_CONTRACT] slot 结构漂移："
                    + compiled_slot.unit_key
                )
    missing_compiled_slots = [
        unit_key
        for unit_key in expected_slot_keys
        if unit_key not in compiled_slots_by_key
    ]
    if missing_compiled_slots:
        errors.append(
            "[GENERATION_CONTRACT] 输入合同缺失 slot："
            + ",".join(missing_compiled_slots)
        )
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)

    scenes: list[ScreenplaySceneShardScene] = []
    consumed_source_ids: list[str] = []
    for scene_key in plan.scene_plan_keys:
        scene_plan = scene_plans.get(scene_key)
        contract = contracts_by_scene.get(scene_key)
        if scene_plan is None or contract is None:
            errors.append(f"{scene_key} 缺少 scene plan 或 identity scaffold")
            continue
        units: list[ScreenplaySceneShardUnit] = []
        character_keys: list[str] = []
        for planned_slot in (
            slot for slot in plan.unit_slots
            if slot.scene_key == scene_key
        ):
            compiled_slot = compiled_slots_by_key[planned_slot.unit_key]
            creative_unit = draft.slots[planned_slot.unit_key]
            if planned_slot.delivery_mode == "written_text":
                exact_text = planned_slot.source_text.strip().strip(
                    "“”「」『』\"'"
                )
                creative_unit = creative_unit.model_copy(update={
                    "required_text": exact_text,
                    "prop_text": "",
                    "on_screen_text": "",
                })
            text = creative_unit.text.strip()
            text_provenance, agency_kind = _compile_text_provenance(
                creative_unit=creative_unit,
                compiled_slot=compiled_slot,
            )
            unit = ScreenplaySceneShardUnit(
                unit_key=planned_slot.unit_key,
                kind=planned_slot.kind,
                text=text,
                event_key=planned_slot.event_key,
                narrative_layer=planned_slot.narrative_layer,
                event_priority=planned_slot.event_priority,
                render_policy=planned_slot.render_policy,
                source_segment_ids=list(
                    planned_slot.source_segment_ids
                ),
                actor_keys=list(compiled_slot.actor_keys),
                target_keys=list(compiled_slot.target_keys),
                onscreen_entity_keys=list(
                    compiled_slot.onscreen_entity_keys
                ),
                participant_deliveries=[
                    delivery.model_copy(deep=True)
                    for delivery in compiled_slot.participant_deliveries
                ],
                action_agency=ActionAgency(
                    kind=agency_kind,
                    identity_bearing=(
                        compiled_slot.action_agency.identity_bearing
                    ),
                    source_segment_ids=list(
                        compiled_slot.action_agency.source_segment_ids
                    ),
                ),
                text_provenance=text_provenance,
                required_text=creative_unit.required_text,
                prop_text=creative_unit.prop_text,
                on_screen_text=creative_unit.on_screen_text,
                resulting_state=creative_unit.resulting_state,
                speaker_key=compiled_slot.speaker_key,
                state_subject_key=compiled_slot.state_subject_key,
                state_subject_keys=list(compiled_slot.state_subject_keys),
                environment_only=compiled_slot.environment_only,
                function=creative_unit.function,
                source_text=planned_slot.source_text,
                chain_key="",
                performance=creative_unit.performance,
            )
            units.append(unit)
            for identity_key in [
                *unit.actor_keys,
                *unit.target_keys,
                *unit.onscreen_entity_keys,
                *([unit.speaker_key] if unit.speaker_key else []),
                *[
                    delivery.participant_key
                    for delivery in unit.participant_deliveries
                ],
            ]:
                if identity_key not in character_keys:
                    character_keys.append(identity_key)
            for source_id in unit.source_segment_ids:
                if source_id not in consumed_source_ids:
                    consumed_source_ids.append(source_id)
        context_requirements = _ordered_unique([
            relation.summary
            for relation in contract.derived_relations
            if relation.summary.strip()
        ])
        opening = scene_plan.opening_image.strip()
        exit_state = scene_plan.exit_state.strip()
        summary = "；".join(
            value for value in (opening, exit_state) if value
        ) or scene_plan.scene_heading
        story_function = "推进本场事件并完成状态变化：" + summary
        scenes.append(ScreenplaySceneShardScene(
            key=scene_key,
            scene_heading=scene_plan.scene_heading,
            story_function=story_function,
            character_keys=character_keys,
            summary=summary,
            conflict="",
            turn=exit_state,
            source_basis=",".join(scene_plan.source_segment_ids),
            previous_scene_exit_state=scene_plan.previous_scene_exit_state,
            opening_image=scene_plan.opening_image,
            agency_contracts=deepcopy(scene_plan.agency_contracts),
            entry_state=scene_plan.previous_scene_exit_state,
            exit_state=scene_plan.exit_state,
            context_requirements=context_requirements,
            units=units,
        ))
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)
    return ScreenplaySceneShardIR(
        episode_no=episode_no,
        shard_id=plan.shard_id,
        scene_plan_keys=list(plan.scene_plan_keys),
        scenes=scenes,
        consumed_source_ids=consumed_source_ids,
        unresolved_participants=[],
        source_hash=plan.source_hash,
        boundary_hash=plan.boundary_hash,
        blueprint_hash=plan.blueprint_hash,
        identity_registry_hash=plan.identity_registry_hash,
        source_ownership_hash=plan.source_ownership_hash,
        identity_scaffold_hash=screenplay_scene_identity_scaffold_hash(
            scene_input_contracts
        ),
        generation_scaffold_hash=(
            screenplay_scene_generation_scaffold_hash(
                plan,
                scene_input_contracts,
            )
        ),
    )
