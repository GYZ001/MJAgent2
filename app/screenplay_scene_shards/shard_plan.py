"""Builds scene-shard plans from a narrative blueprint: per-scene unit-slot
assembly, output-token estimation, and
``build_screenplay_scene_shard_plans`` which partitions a blueprint's scenes
into shard-sized generation units.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import math
import re
from app.narrative_blueprint import (
    BlueprintScenePlan,
    NarrativeBlueprint,
    derive_blueprint_scene_plans,
    effective_source_unit_deliveries,
)
from app.source_excerpt import index_source_segments
from app.source_facts import source_segment_facts
from typing import Any

from .common import (
    _hash,
    _setting_int,
)
from .constants import (
    SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS,
    SCREENPLAY_SCENE_SHARD_MIN_OUTPUT_TOKENS,
    SCREENPLAY_SCENE_SHARD_REASONING_RESERVE_PERCENT,
    SCREENPLAY_SCENE_SHARD_SCENE_RESERVE_TOKENS,
    SCREENPLAY_SCENE_SHARD_UNIT_RESERVE_TOKENS,
)
from .identity_registry import (
    _identity_aliases,
    _source_ownership_hash,
    blueprint_content_hash,
)
from .models import (
    ScreenplaySceneShardPlan,
    ScreenplaySceneUnitSlotPlan,
)


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


def _build_group_unit_slots(
    group: list[BlueprintScenePlan],
    *,
    source_by_id: dict[str, str],
    scene_order_by_key: dict[str, int],
    delivery_by_unit: dict[str, Any] | None = None,
) -> list[ScreenplaySceneUnitSlotPlan]:
    legacy_delivery_fallback = delivery_by_unit is None
    delivery_by_unit = delivery_by_unit or {}
    slots: list[ScreenplaySceneUnitSlotPlan] = []
    unit_order = 0
    for scene_plan in group:
        scene_unit_order = 0
        for source_id in scene_plan.source_segment_ids:
            semantics = scene_plan.source_semantics.get(source_id)
            if semantics is None:
                raise ValueError(
                    f"{scene_plan.key} 缺少 {source_id} 的显式来源语义"
                )
            if semantics.projection_policy != "picture":
                raise ValueError(
                    f"{scene_plan.key} 不得为 {source_id} 的 "
                    f"{semantics.projection_policy} 投影生成创作 slot"
                )
            for fact in source_segment_facts(
                source_id,
                source_by_id.get(source_id, ""),
            ):
                if fact.projection == "paratext":
                    continue
                unit_order += 1
                scene_unit_order += 1
                source_part_order = fact.unit_order
                source_part = fact.text
                delivery = (
                    delivery_by_unit.get(fact.source_unit_key)
                    if fact.projection == "quoted"
                    else None
                )
                if (
                    fact.projection == "quoted"
                    and delivery is None
                    and not legacy_delivery_fallback
                ):
                    raise ValueError(
                        f"{fact.source_unit_key} 缺少 quoted source delivery"
                    )
                delivery_mode = (
                    delivery.mode
                    if delivery is not None
                    else (
                        "spoken_dialogue"
                        if fact.projection == "quoted"
                        else "action"
                    )
                )
                kind = (
                    "dialogue"
                    if delivery_mode in {
                        "spoken_dialogue",
                        "offscreen_voice",
                    }
                    else "action"
                )
                key_base = (
                    f"{scene_plan.key}:{source_id}:"
                    f"{source_part_order:03d}"
                )
                slots.append(ScreenplaySceneUnitSlotPlan(
                    unit_key=f"{key_base}:unit",
                    event_key=f"{key_base}:event",
                    scene_key=scene_plan.key,
                    scene_order=scene_order_by_key[scene_plan.key],
                    unit_order=unit_order,
                    scene_unit_order=scene_unit_order,
                    kind=kind,
                    narrative_layer=semantics.narrative_layer,
                    event_priority=semantics.event_priority,
                    render_policy=semantics.render_policy,
                    source_segment_ids=[source_id],
                    source_unit_key=fact.source_unit_key,
                    source_text=(
                        source_part
                        if fact.projection == "quoted"
                        else ""
                    ),
                    source_surface=fact.surface_form,
                    delivery_mode=delivery_mode,
                    content_owner_key=(
                        delivery.content_owner_key
                        if delivery is not None else ""
                    ),
                    performer_key=(
                        delivery.performer_key
                        if delivery is not None else ""
                    ),
                ))
    return slots


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
    identity_registry: list[dict[str, Any]] | None = None,
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
    identity_aliases = (
        _identity_aliases(identity_registry)
        if identity_registry is not None
        else {}
    )
    delivery_by_unit: dict[str, Any] = {}
    for node in blueprint.nodes:
        if node.source_semantics().projection_policy != "picture":
            continue
        for delivery in effective_source_unit_deliveries(node):
            if delivery.source_unit_key in delivery_by_unit:
                raise ValueError(
                    f"{delivery.source_unit_key} 含多个 quoted source delivery"
                )
            content_owner_key = delivery.content_owner_key.strip()
            performer_key = delivery.performer_key.strip()
            if content_owner_key and identity_registry is not None:
                frozen_owner = identity_aliases.get(content_owner_key, "")
                if not frozen_owner:
                    raise ValueError(
                        f"{delivery.source_unit_key} content owner 未冻结："
                        f"{content_owner_key}"
                    )
                content_owner_key = frozen_owner
            if performer_key and identity_registry is not None:
                frozen_performer = identity_aliases.get(performer_key, "")
                if not frozen_performer:
                    raise ValueError(
                        f"{delivery.source_unit_key} performer 未冻结："
                        f"{performer_key}"
                    )
                performer_key = frozen_performer
            delivery_by_unit[delivery.source_unit_key] = (
                delivery.model_copy(update={
                    "content_owner_key": content_owner_key,
                    "performer_key": performer_key,
                })
            )
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
    scene_order_by_key = {
        scene_plan.key: scene_order
        for scene_order, scene_plan in enumerate(
            blueprint.scene_plans,
            start=1,
        )
    }
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
        unit_slots = _build_group_unit_slots(
            group,
            source_by_id=source_by_id,
            scene_order_by_key=scene_order_by_key,
            delivery_by_unit=delivery_by_unit,
        )
        plans.append(ScreenplaySceneShardPlan(
            shard_id=f"SS{index:03d}",
            scene_plan_keys=group_scene_keys,
            source_segment_ids=source_ids,
            source_scene_owners=dict(blueprint.source_scene_owners),
            unit_slots=unit_slots,
            derived_relations=[
                relation.model_copy(deep=True)
                for relation in blueprint.scene_derivations
                if relation.target_scene_plan_key in group_scene_keys
            ],
            source_ownership_hash=source_ownership_hash,
            estimated_units=len(unit_slots),
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
