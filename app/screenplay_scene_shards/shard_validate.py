"""Normalizes and validates a provider-authored scene shard against its input
contracts and identity scaffold: payload normalization plus
``validate_screenplay_scene_shard``, the structural gate a shard must pass
before it can be merged.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

from app.narrative_blueprint import (
    BlueprintScenePlan,
    NarrativeBlueprint,
)
from app.renderability import SCENE_STORY_FUNCTION_MIN_CHARS
from app.screenplay_ir import screenplay_beat_fields_repeat
from copy import deepcopy
from typing import Any

from .identity_registry import ScreenplaySceneShardError
from .identity_scaffold import (
    _ordered_unique,
    screenplay_scene_generation_scaffold_hash,
    screenplay_scene_identity_scaffold_hash,
)
from .input_contracts import _validate_scene_input_contracts
from .models import (
    ScreenplaySceneInputContract,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardPlan,
)


def normalize_screenplay_scene_shard_payload(
    payload: dict[str, Any],
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    blueprint: NarrativeBlueprint,
) -> dict[str, Any]:
    """Return an unchanged copy; structural drift must fail explicitly."""
    del episode_no, plan, scene_plans, blueprint
    return deepcopy(payload)


def normalize_screenplay_scene_creative_payload(
    payload: dict[str, Any],
    *,
    scene_plans: dict[str, BlueprintScenePlan],
    blueprint: NarrativeBlueprint,
) -> dict[str, Any]:
    """Return an unchanged copy; slot/schema violations are not repaired."""
    del scene_plans, blueprint
    return deepcopy(payload)


def normalize_screenplay_scene_shard(
    shard: ScreenplaySceneShardIR,
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardIR:
    """Validate a compiled shard without changing provider-authored content."""
    if shard.episode_no != episode_no:
        raise ScreenplaySceneShardError(
            plan.shard_id,
            ["episode_no 与 generation scaffold 不一致"],
        )
    identity_keys = {
        binding.identity_key
        for contract in scene_input_contracts
        for binding in contract.participant_bindings
        if binding.identity_key
    }
    errors = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=scene_input_contracts,
        identity_keys=identity_keys,
    )
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)
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
    del front_matter_ids
    errors: list[str] = []
    if shard.episode_no < 1:
        errors.append("episode_no 必须为正整数")
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
    expected_identity_hash = screenplay_scene_identity_scaffold_hash(
        scene_input_contracts
    )
    if shard.identity_scaffold_hash != expected_identity_hash:
        errors.append("identity_scaffold_hash 不匹配")
    expected_generation_hash = (
        screenplay_scene_generation_scaffold_hash(
            plan,
            scene_input_contracts,
        )
    )
    if shard.generation_scaffold_hash != expected_generation_hash:
        errors.append("generation_scaffold_hash 不匹配")
    contracts_by_scene, contract_errors = _validate_scene_input_contracts(
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=scene_input_contracts,
        identity_keys=identity_keys,
    )
    errors.extend(contract_errors)
    actual_consumed: list[str] = []
    actual_unit_keys = [
        unit.unit_key
        for scene in shard.scenes
        for unit in scene.units
    ]
    duplicate_unit_keys = [
        unit_key
        for unit_key in dict.fromkeys(actual_unit_keys)
        if actual_unit_keys.count(unit_key) > 1
    ]
    if duplicate_unit_keys:
        errors.append(
            "编译结果 unit_key 重复："
            + ",".join(duplicate_unit_keys)
        )
    expected_unit_keys = [
        slot.unit_key for slot in plan.unit_slots
    ]
    if actual_unit_keys != expected_unit_keys:
        errors.append(
            "编译结果 unit_key 顺序/归属与 shard plan 不一致："
            f"expected={expected_unit_keys}, actual={actual_unit_keys}"
        )
    compiled_slots_by_key = {
        slot.unit_key: slot
        for contract in scene_input_contracts
        for slot in contract.unit_slots
    }
    units_by_key = {
        unit.unit_key: (scene.key, unit)
        for scene in shard.scenes
        for unit in scene.units
        if unit.unit_key
    }
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
        if contract is None:
            continue
        expected_scene_unit_keys = [
            slot.unit_key
            for slot in plan.unit_slots
            if slot.scene_key == scene.key
        ]
        if [unit.unit_key for unit in scene.units] != expected_scene_unit_keys:
            errors.append(
                f"{scene.key} unit slot 顺序与 plan 不一致"
            )
        expected_character_keys = _ordered_unique([
            identity_key
            for slot in contract.unit_slots
            for identity_key in [
                *slot.actor_keys,
                *slot.target_keys,
                *slot.onscreen_entity_keys,
                *([slot.speaker_key] if slot.speaker_key else []),
                *[
                    delivery.participant_key
                    for delivery in slot.participant_deliveries
                ],
            ]
        ])
        if scene.character_keys != expected_character_keys:
            errors.append(
                f"{scene.key}.character_keys 与 compiled slot 不一致"
            )
    for planned_slot in plan.unit_slots:
        actual_pair = units_by_key.get(planned_slot.unit_key)
        compiled_slot = compiled_slots_by_key.get(
            planned_slot.unit_key
        )
        if actual_pair is None:
            errors.append(
                f"缺失 compiled slot：{planned_slot.unit_key}"
            )
            continue
        if compiled_slot is None:
            errors.append(
                f"输入合同缺失 compiled slot：{planned_slot.unit_key}"
            )
            continue
        actual_scene_key, unit = actual_pair
        if actual_scene_key != planned_slot.scene_key:
            errors.append(
                f"{planned_slot.unit_key} scene_key 漂移："
                f"{actual_scene_key}"
            )
        structural_actual = {
            "unit_key": unit.unit_key,
            "event_key": unit.event_key,
            "kind": unit.kind,
            "narrative_layer": unit.narrative_layer,
            "event_priority": unit.event_priority,
            "render_policy": unit.render_policy,
            "source_segment_ids": list(unit.source_segment_ids),
            "source_text": unit.source_text,
            "chain_key": unit.chain_key,
        }
        structural_expected = {
            "unit_key": planned_slot.unit_key,
            "event_key": planned_slot.event_key,
            "kind": planned_slot.kind,
            "narrative_layer": planned_slot.narrative_layer,
            "event_priority": planned_slot.event_priority,
            "render_policy": planned_slot.render_policy,
            "source_segment_ids": list(
                planned_slot.source_segment_ids
            ),
            "source_text": planned_slot.source_text,
            "chain_key": "",
        }
        if structural_actual != structural_expected:
            errors.append(
                f"{planned_slot.unit_key} 结构字段漂移，禁止改写 "
                "event/scene/source/order/owner"
            )
        identity_actual = {
            "actor_keys": list(unit.actor_keys),
            "target_keys": list(unit.target_keys),
            "speaker_key": unit.speaker_key,
            "onscreen_entity_keys": list(
                unit.onscreen_entity_keys
            ),
            "participant_deliveries": [
                delivery.model_dump(mode="json")
                for delivery in unit.participant_deliveries
            ],
        }
        identity_expected = {
            "actor_keys": list(compiled_slot.actor_keys),
            "target_keys": list(compiled_slot.target_keys),
            "speaker_key": compiled_slot.speaker_key,
            "onscreen_entity_keys": list(
                compiled_slot.onscreen_entity_keys
            ),
            "participant_deliveries": [
                delivery.model_dump(mode="json")
                for delivery in compiled_slot.participant_deliveries
            ],
        }
        if identity_actual != identity_expected:
            errors.append(
                f"{planned_slot.unit_key} identity scaffold drift"
            )
        if (
            unit.kind == "dialogue"
            and unit.text.strip() != planned_slot.source_text.strip()
        ):
            errors.append(
                f"{planned_slot.unit_key} dialogue.text 必须等于 "
                "scaffold source_text"
            )
        # 同一条「动作与结果不得雷同」的判据，下游 plot_spine 硬门禁
        # （SPINE_ACTION_TURN_DUPLICATE）已经在用，但那里**没有任何修复策略**：
        # 编译器把 text→does、resulting_state→turn 逐字带下去，门禁一命中，
        # 规划器立刻记 exhausted，整集停在 WAITING_HUMAN，补丁数为 0
        # （生产 EP1：145 个单元里 4 个把 resulting_state 原样写成了 text）。
        # 判据本身是确定性的（condense 后完全相同，或双向 0.9 的最长公共段
        # 与 bigram 覆盖），所以把它提到**这一层**——这里有 3 轮定向语义修复，
        # 模型能拿到具体 unit 和明确诉求，是同一条规则唯一能被便宜修好的位置。
        # 域必须与下游门禁**完全一致**，不能更宽：paratext / exclude_from_spine
        # 事件在 `finalize_screenplay_ir` 里被整体剔出 events / beats / units
        # （app/screenplay_ir.py 的「非剧情旁文本隔离」），根本走不到
        # validate_plot_spine。若在这一层顺手把它们也判掉，就等于凭空发明了一条
        # 下游从不存在的约束，去卡下游刻意排除的内容。
        if (
            unit.kind != "dialogue"
            and unit.narrative_layer != "paratext"
            and unit.render_policy != "exclude_from_spine"
            and screenplay_beat_fields_repeat(unit.text, unit.resulting_state)
        ):
            errors.append(
                f"{planned_slot.unit_key} resulting_state 与 text 语义重复；"
                "text 写可见/可听动作，resulting_state 必须写该动作完成后"
                "新成立的人物、信息、关系或局势状态"
            )
        for source_id in unit.source_segment_ids:
            planned_owner = plan.source_scene_owners.get(source_id)
            if planned_owner != planned_slot.scene_key:
                errors.append(
                    f"{planned_slot.unit_key} 来源唯一归属冲突："
                    f"{source_id} owner={planned_owner or '未定义'}"
                )
            elif source_id not in actual_consumed:
                actual_consumed.append(source_id)
    missing_sources = [
        source_id
        for source_id in plan.source_segment_ids
        if source_id not in actual_consumed
    ]
    if missing_sources:
        errors.append(
            "编译结果未覆盖 plan 来源：" + ",".join(missing_sources)
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
