"""Validates and applies the blueprint's scene contract: validate_and_apply_blueprint_scene_contract."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .models_core import NarrativeBlueprint
from .scene_plans import derive_blueprint_scene_plans


def validate_and_apply_blueprint_scene_contract(
    candidate: Any,
    blueprint: NarrativeBlueprint,
    *,
    allow_prefix: bool = False,
) -> list[str]:
    """Validate authored IR scenes and apply program-owned headings/order."""
    errors: list[str] = []
    entity_keys = list(dict.fromkeys(
        participant
        for node in blueprint.nodes
        for participant in node.participants
        if participant
    ))
    entity_components: defaultdict[str, list[str]] = defaultdict(list)
    for entity_key in entity_keys:
        for component in entity_key.split("_"):
            if len(component) >= 2:
                entity_components[component].append(entity_key)
    for identity in list(getattr(candidate, "identities", []) or []):
        identity_key = str(getattr(identity, "key", "") or "")
        if (
            identity_key.startswith("context_actor_")
            or getattr(identity, "role_type", "")
            == "source_backed_scene_context_actor"
        ):
            continue
        current_display_name = str(
            getattr(identity, "display_name", "") or ""
        )
        if any(
            current_display_name == entity_key.replace("_", "")
            or current_display_name in entity_key.split("_")
            for entity_key in entity_keys
        ):
            continue
        identity_tokens = " ".join([
            identity_key,
            current_display_name,
        ])
        full_matches = [
            entity_key
            for entity_key in entity_keys
            if entity_key.replace("_", "") in identity_tokens
        ]
        component_matches = [
            (component, keys[0])
            for component, keys in entity_components.items()
            if len(keys) == 1 and component in identity_tokens
        ]
        candidate_names = {
            entity_key.replace("_", "")
            for entity_key in full_matches
        } | {
            component
            for component, _entity_key in component_matches
        }
        if len(candidate_names) == 1:
            identity.display_name = next(iter(candidate_names))
    plans = derive_blueprint_scene_plans(blueprint)
    scenes = list(getattr(candidate, "scenes", []) or [])
    if len(scenes) > len(plans):
        errors.append(
            "[BLUEPRINT_SCENE_COUNT_OVERFLOW] 剧本场次数超过程序蓝图："
            f"{len(scenes)}>{len(plans)}"
        )
        return errors
    if not allow_prefix and len(scenes) != len(plans):
        errors.append(
            "[BLUEPRINT_SCENE_PREFIX_INCOMPLETE] 剧本没有完成全部蓝图场次："
            f"{len(scenes)}/{len(plans)}"
        )

    if hasattr(candidate, "source_scene_owners"):
        candidate.source_scene_owners = dict(
            blueprint.source_scene_owners
        )
    if hasattr(candidate, "scene_derivations"):
        candidate.scene_derivations = [
            relation.model_dump(mode="json")
            for relation in blueprint.scene_derivations
        ]

    actual_source_scenes: defaultdict[str, list[str]] = defaultdict(list)
    for scene_index, scene in enumerate(scenes):
        if scene_index >= len(plans):
            continue
        scene_key = plans[scene_index].key
        for unit in (getattr(scene, "units", []) or []):
            for source_id in (
                getattr(unit, "source_segment_ids", []) or []
            ):
                if scene_key not in actual_source_scenes[source_id]:
                    actual_source_scenes[source_id].append(scene_key)
    for source_id, scene_keys in actual_source_scenes.items():
        if len(scene_keys) > 1:
            errors.append(
                "[BLUEPRINT_SOURCE_REUSED_ACROSS_SCENES] "
                f"{source_id} 同时被 " + "、".join(scene_keys) + " 消费"
            )
    if errors:
        return errors

    source_order = {
        source_id: index
        for index, source_id in enumerate(
            blueprint.source_scene_owners
        )
    }
    allowed_by_plan = [
        set(plan.source_segment_ids) for plan in plans
    ]
    reassigned_units: list[list[Any]] = [
        [] for _scene in scenes
    ]
    for scene_index, scene in enumerate(scenes):
        for unit in (getattr(scene, "units", []) or []):
            unit_source_ids = set(
                getattr(unit, "source_segment_ids", []) or []
            )
            candidate_indexes = [
                plan_index
                for plan_index, allowed_source_ids
                in enumerate(allowed_by_plan[:len(scenes)])
                if unit_source_ids.issubset(allowed_source_ids)
            ]
            if (
                not candidate_indexes
                and getattr(unit, "kind", "") == "action"
                and unit_source_ids
            ):
                source_groups: list[tuple[int, list[str]]] = []
                for source_id in (
                    getattr(unit, "source_segment_ids", []) or []
                ):
                    owning_indexes = [
                        plan_index
                        for plan_index, allowed_source_ids
                        in enumerate(allowed_by_plan[:len(scenes)])
                        if source_id in allowed_source_ids
                    ]
                    owner_index = min(
                        owning_indexes,
                        key=lambda index: abs(index - scene_index),
                        default=scene_index,
                    )
                    if (
                        not source_groups
                        or source_groups[-1][0] != owner_index
                    ):
                        source_groups.append((owner_index, [source_id]))
                    else:
                        source_groups[-1][1].append(source_id)
                clauses = [
                    clause.strip()
                    for clause in re.findall(
                        r"[^，。！？；]+[，。！？；]?",
                        str(getattr(unit, "text", "")),
                    )
                    if clause.strip()
                ]
                if (
                    len(source_groups) > 1
                    and len(clauses) >= len(source_groups)
                ):
                    clause_start = 0
                    total_sources = sum(
                        len(source_ids)
                        for _index, source_ids in source_groups
                    )
                    consumed_sources = 0
                    for part_index, (
                        owner_index,
                        source_ids,
                    ) in enumerate(source_groups, start=1):
                        consumed_sources += len(source_ids)
                        clause_end = (
                            len(clauses)
                            if part_index == len(source_groups)
                            else max(
                                clause_start + 1,
                                round(
                                    len(clauses)
                                    * consumed_sources
                                    / max(total_sources, 1)
                                ),
                            )
                        )
                        split_unit = unit.model_copy(deep=True)
                        split_unit.event_key = (
                            f"{unit.event_key}-bp-part-{part_index}"
                        )
                        split_unit.text = "".join(
                            clauses[clause_start:clause_end]
                        )
                        split_unit.source_segment_ids = source_ids
                        reassigned_units[owner_index].append(split_unit)
                        clause_start = clause_end
                    continue
            target_index = (
                scene_index
                if scene_index in candidate_indexes
                else min(
                    candidate_indexes,
                    key=lambda index: abs(index - scene_index),
                    default=scene_index,
                )
            )
            reassigned_units[target_index].append(unit)
    for scene_index, scene in enumerate(scenes):
        scene.units = sorted(
            reassigned_units[scene_index],
            key=lambda unit: min(
                (
                    source_order.get(source_id, len(source_order))
                    for source_id in (
                        getattr(unit, "source_segment_ids", []) or []
                    )
                ),
                default=len(source_order),
            ),
        )

    ordered_scenes = []
    for scene, plan in zip(scenes, plans):
        allowed_source_ids = set(plan.source_segment_ids)
        invalid_units = [
            str(getattr(unit, "event_key", ""))
            for unit in (getattr(scene, "units", []) or [])
            if not set(getattr(unit, "source_segment_ids", []) or []).issubset(
                allowed_source_ids,
            )
        ]
        if invalid_units:
            errors.append(
                f"[BLUEPRINT_SCENE_SOURCE_ESCAPE] {plan.key} 的 units 引用了"
                "其他时空节点来源："
                + "、".join(invalid_units[:10])
            )
        scene.key = plan.key
        scene.scene_heading = plan.scene_heading
        if hasattr(scene, "previous_scene_exit_state"):
            scene.previous_scene_exit_state = (
                plan.previous_scene_exit_state
            )
        if hasattr(scene, "opening_image"):
            scene.opening_image = plan.opening_image
        if hasattr(scene, "entry_state"):
            scene.entry_state = plan.opening_image
        if hasattr(scene, "exit_state"):
            scene.exit_state = plan.exit_state
        if hasattr(scene, "agency_contracts"):
            scene.agency_contracts = plan.agency_contracts
        ordered_scenes.append(scene)
    candidate.scenes = ordered_scenes
    return errors
