"""Builds the creative-generation prompt for one scene shard, including the
semantic-authority payload that pins each unit's identity/perception
constraints for the provider.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import json
from app.narrative_blueprint import BlueprintScenePlan
from app.source_facts import source_segment_facts
from typing import Any

from .identity_scaffold import screenplay_scene_generation_scaffold_hash
from .models import (
    ScreenplaySceneInputContract,
    ScreenplaySceneShardPlan,
)


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
    exact_slot_authority, identity_labels = (
        _scene_shard_semantic_authority_payload(
            scene_input_contracts=scene_input_contracts,
            identity_registry=identity_registry,
        )
    )
    generation_scaffold_hash = (
        screenplay_scene_generation_scaffold_hash(
            plan,
            scene_input_contracts,
        )
    )
    return (
        "任务：只填写程序预声明 generation slot 的动作、对白和表演内容。"
        "scene_key、unit_key、event_key、kind、source_segment_ids、播放顺序、"
        "来源归属以及 actor/target/speaker/onscreen/participant_deliveries "
        "均已由 Blueprint、shard plan 和 compiler 锁定，模型无权输出或修改。"
        "根对象只能包含 contract_version 与 slots；slots 必须是对象，属性名必须"
        "与 Shard plan 的 unit_key 集合完全相等。每个 slot 只能填写 text、"
        "performance、resulting_state、function、required_text、prop_text、"
        "on_screen_text。required_text、prop_text、on_screen_text 只填写需要"
        "准确出现在画面中的文字内容，每个 slot 最多使用一种；对白必须只写入"
        "dialogue slot 的 text，不得把口播放入 required_text。action_agency、"
        "source_surface 与 delivery_mode 已由来源交付合同锁定；"
        "delivery_mode=written_text 时，compiler 会把 source_text 确定性写入"
        "required_text，模型不得改写原文或把内容作者伪装成发声者。"
        "agency_kind、text_provenance、identity_keys 均由 compiler 根据 generation "
        "scaffold 关系、文字结构字段与 source IDs 生成，模型输出这些字段属于"
        "additionalProperties 越权并直接失败。文字中出现人物姓名不会创建人物关系。"
        "不得用数组位置匹配，不得增加、"
        "删除、重命名或重排结构主键。dialogue slot 的 text 已由 Schema 固定为"
        "来源原文。任何缺失 slot、多余 slot 或越权字段都会明确作为 "
        "generation_contract 失败，不会静默改写。逐 slot exact authority 是"
        "唯一创作权限：action slot 即使 source_text 为空也不授权自由改写，"
        "必须只展开自身 source_fact.text；每个 slot 只能改写自身 source_fact，"
        "不得借用相邻 unit 的事实。cross-slot 内容必须归因到最早越界 slot，"
        "不得提前或重复承载其他 slot 的来源事实。"
        "逐 slot exact authority 中 environment_only=true 的 slot 只描写环境、"
        "空间、氛围与无主体的客观现象，text/performance/resulting_state 不得写入"
        "任何人物的思考、发问、反应、动作或情绪，也不得把环境拟人化或让环境替人物"
        "行动；此类 slot 没有 state_subject，任何人物内容都会作为 "
        "environment_personification 失败。\nShard plan：\n"
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
        + "\n逐 slot exact authority：\n"
        + json.dumps(
            exact_slot_authority,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\nexact authority 身份标签：\n"
        + json.dumps(
            identity_labels,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n只输出 Schema 对象：\n"
        + json.dumps(output_schema, ensure_ascii=False)
        + f"\n程序固定上下文（禁止输出）：episode_no={episode_no}, "
        f"shard_id={plan.shard_id}, generation_scaffold_hash="
        f"{generation_scaffold_hash}"
    )


def _scene_shard_semantic_authority_payload(
    *,
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    source_facts_by_key = {
        fact.source_unit_key: fact
        for contract in scene_input_contracts
        for segment in contract.source_segments
        for fact in source_segment_facts(
            segment.source_segment_id,
            segment.text,
        )
    }
    authority_slots = {
        slot.unit_key: {
            "kind": slot.kind,
            "source_unit_key": slot.source_unit_key,
            "source_text": slot.source_text,
            "source_fact": (
                source_facts_by_key[slot.source_unit_key].model_dump(
                    mode="json"
                )
                if slot.source_unit_key in source_facts_by_key
                else None
            ),
            "state_subject_key": slot.state_subject_key,
            "state_subject_keys": slot.state_subject_keys,
            "environment_only": slot.environment_only,
            "actor_keys": slot.actor_keys,
            "target_keys": slot.target_keys,
            "speaker_key": slot.speaker_key,
            "onscreen_entity_keys": slot.onscreen_entity_keys,
        }
        for contract in scene_input_contracts
        for slot in contract.unit_slots
    }
    allowed_identity_keys = {
        value
        for slot in authority_slots.values()
        for field in (
            "actor_keys", "target_keys", "onscreen_entity_keys",
        )
        for value in slot[field]
    } | {
        str(slot.get("speaker_key") or "")
        for slot in authority_slots.values()
        if str(slot.get("speaker_key") or "")
    } | {
        str(slot.get("state_subject_key") or "")
        for slot in authority_slots.values()
        if str(slot.get("state_subject_key") or "")
    } | {
        str(identity_key)
        for slot in authority_slots.values()
        for identity_key in slot.get("state_subject_keys") or []
    }
    identity_labels = {
        str(item.get("identity_key") or ""): {
            "canonical_name": str(item.get("canonical_name") or ""),
            "source_labels": list(item.get("source_labels") or []),
            "authority_id": str(item.get("authority_id") or ""),
        }
        for item in identity_registry
        if str(item.get("identity_key") or "") in allowed_identity_keys
    }
    return authority_slots, identity_labels
