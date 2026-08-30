"""Compiler phase: validates and derives event relations/order, builds the identity resolver, and indexes scene units to collect identities."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app import textmatch
from app.schemas import Bible

from .identity_resolver import _IRIdentityResolver
from .models_core import IRIdentity, IRScene, IRSceneUnit
from .models_event import IREvent, ScreenplayGenerationIR


def _ir_validate_and_derive_event_relations(
    value: ScreenplayGenerationIR,
    scene_by_key: dict[str, IRScene],
    identity_by_key: dict[str, IRIdentity],
    event_by_key: dict[str, IREvent],
    expected_segment_ids: set[str],
    typed_visual_unit_contract: bool,
    compiler_audit: list[dict[str, Any]],
) -> dict[str, int]:
    event_order = {event.key: index for index, event in enumerate(value.events)}
    for event in value.events:
        if event.scene_key not in scene_by_key:
            raise ValueError(f"event {event.key} 引用了不存在的 scene {event.scene_key}")
        referenced_identity_keys = {
            *event.actor_keys,
            *event.target_keys,
            *event.onscreen_entity_keys,
            *event.text_provenance.content_owner_keys,
            *(
                delivery.participant_key
                for delivery in event.participant_deliveries
            ),
            *(
                key
                for key in event.perceivable_by
                if key != "audience"
            ),
        }
        unknown_identity_keys = referenced_identity_keys - set(identity_by_key)
        if unknown_identity_keys:
            raise ValueError(
                f"event {event.key} 引用了不存在的 identity："
                f"{sorted(unknown_identity_keys)}"
            )
        invalid_onscreen_keys = [
            key
            for key in event.onscreen_entity_keys
            if identity_by_key[key].visual_policy == "offscreen_only"
        ]
        if invalid_onscreen_keys:
            raise ValueError(
                f"event {event.key}.onscreen_entity_keys 含仅允许画外的身份："
                f"{invalid_onscreen_keys}"
            )
        relation_keys = {*event.actor_keys, *event.target_keys}
        delivery_keys: set[str] = set()
        for delivery in event.participant_deliveries:
            participant_key = delivery.participant_key.strip()
            if participant_key in delivery_keys:
                raise ValueError(
                    f"event {event.key} 对 {participant_key} 重复声明参与者交付"
                )
            delivery_keys.add(participant_key)
            if participant_key not in relation_keys:
                raise ValueError(
                    f"event {event.key} 的参与者交付 {participant_key} "
                    "不属于 actor/target"
                )
            if participant_key in event.onscreen_entity_keys:
                raise ValueError(
                    f"event {event.key} 的参与者交付 {participant_key} 已入画"
                )
            if not delivery.observable_claim.strip() or not delivery.is_perceivable:
                raise ValueError(
                    f"event {event.key} 的参与者交付 {participant_key} "
                    "缺少结构化可感知证据"
                )
        if (
            typed_visual_unit_contract
            and "onscreen_entity_keys" in event.model_fields_set
        ):
            offscreen_relation_keys = (
                relation_keys - set(event.onscreen_entity_keys)
            )
        else:
            offscreen_relation_keys = {
                key
                for key in relation_keys
                if identity_by_key[key].visual_policy == "offscreen_only"
            }
        missing_deliveries = offscreen_relation_keys - delivery_keys
        if missing_deliveries:
            raise ValueError(
                f"event {event.key} 未入画 actor/target 缺少结构化参与者交付："
                f"{sorted(missing_deliveries)}"
            )
        unknown_sources = set(event.source_segment_ids) - expected_segment_ids
        if unknown_sources:
            raise ValueError(f"event {event.key} 来源段不存在：{sorted(unknown_sources)}")
        unknown_parents = set(event.causal_parent_keys) - set(event_by_key)
        if unknown_parents:
            raise ValueError(f"event {event.key} 原因事件不存在：{sorted(unknown_parents)}")
        future_parents = [
            key for key in event.causal_parent_keys
            if event_order[key] >= event_order[event.key]
        ]
        if future_parents:
            raise ValueError(
                f"event {event.key} 引用了未先发生的原因事件：{future_parents}"
            )
        derived_fields: list[str] = []
        if not event.adapted_statement.strip():
            event.adapted_statement = (
                event.observable_claim
                or event.completion_condition
                or event.resulting_state
                or event.action_intent
            )
            derived_fields.append("adapted_statement")
        if not event.observable_claim.strip():
            event.observable_claim = (
                event.completion_condition
                or event.resulting_state
                or event.action_intent
            )
            derived_fields.append("observable_claim")
        if not event.adaptation_reason.strip():
            event.adaptation_reason = (
                "按当前事件的来源段、动作意图和完成条件确定性建立改编关系"
            )
            derived_fields.append("adaptation_reason")
        if not event.perceivable_by:
            event.perceivable_by = list(dict.fromkeys([
                *event.actor_keys,
                *event.target_keys,
                "audience",
            ]))
            derived_fields.append("perceivable_by")
        if not event.onscreen_entity_keys:
            event.onscreen_entity_keys = list(dict.fromkeys(
                key
                for key in [*event.actor_keys, *event.target_keys]
                if identity_by_key[key].visual_policy != "offscreen_only"
            ))
            derived_fields.append("onscreen_entity_keys")
        if derived_fields:
            compiler_audit.append({
                "path": f"events.{event.key}",
                "operation": "derive_fields",
                "fields": derived_fields,
                "reason": "deterministic_event_projection",
            })
    return event_order


def _ir_build_identity_resolver(
    identity_by_key: dict[str, IRIdentity],
    bible: Bible,
    episode: dict[str, Any],
    compiler_audit: list[dict[str, Any]],
) -> tuple[_IRIdentityResolver, dict[str, Any]]:
    bible_by_name = {item.name: item for item in bible.characters}
    identity_resolver = _IRIdentityResolver(
        identity_by_key=identity_by_key,
        bible_by_name=bible_by_name,
        episode=episode,
        compiler_audit=compiler_audit,
    )
    return identity_resolver, bible_by_name


def _ir_index_scene_units_and_collect_identities(
    value: ScreenplayGenerationIR,
    identity_resolver: _IRIdentityResolver,
    identity_by_key: dict[str, IRIdentity],
    event_by_key: dict[str, IREvent],
    event_order: dict[str, int],
    compiler_audit: list[dict[str, Any]],
) -> tuple[list[str], "defaultdict[str, list[str]]"]:
    def resolve_unit_event_key(scene: IRScene, unit: IRSceneUnit) -> str:
        if unit.event_key in event_by_key:
            return unit.event_key
        candidates = [
            event for event in value.events
            if event.scene_key == scene.key
        ]
        if not candidates:
            raise ValueError(
                f"scene {scene.key} unit 引用了不存在的 event "
                f"{unit.event_key}，且本场没有可归属事件"
            )
        unit_number = int(
            re.sub(r"\D", "", str(unit.event_key or "")) or 0
        )

        def rank(event: IREvent) -> tuple[float, float, int]:
            semantic_text = " ".join(filter(None, (
                event.source_statement,
                event.adapted_statement,
                event.action_intent,
                event.completion_condition,
                event.observable_claim,
            )))
            similarity = max(
                textmatch.longest_run_ratio(unit.text, semantic_text),
                textmatch.bigram_coverage(unit.text, semantic_text),
            )
            candidate_number = int(
                re.sub(r"\D", "", str(event.key or "")) or 0
            )
            return similarity, -abs(unit_number - candidate_number), -event_order[event.key]

        selected = max(candidates, key=rank)
        compiler_audit.append({
            "path": f"scenes.{scene.key}.units.event_key",
            "operation": "repair_reference",
            "from": unit.event_key,
            "to": selected.key,
            "reason": "same_scene_semantic_and_ordinal_match",
        })
        unit.event_key = selected.key
        return selected.key

    used_identity_keys: set[str] = set()
    event_speaker_keys: defaultdict[str, list[str]] = defaultdict(list)
    for scene in value.scenes:
        used_identity_keys.update(identity_resolver.key(token) for token in scene.character_keys)
        for unit in scene.units:
            resolve_unit_event_key(scene, unit)
            used_identity_keys.update(
                identity_resolver.key(token)
                for token in unit.text_provenance.content_owner_keys
            )
            if unit.kind == "dialogue":
                if not unit.speaker_key:
                    raise ValueError(f"scene {scene.key} 对白缺少 speaker_key")
                speaker_key = identity_resolver.key(unit.speaker_key)
                used_identity_keys.add(speaker_key)
                if speaker_key not in event_speaker_keys[unit.event_key]:
                    event_speaker_keys[unit.event_key].append(speaker_key)
    for event in value.events:
        used_identity_keys.update(
            identity_resolver.key(token)
            for token in event.actor_keys
            if str(token).strip() != "audience"
        )
        used_identity_keys.update(
            identity_resolver.key(token)
            for token in event.target_keys
            if str(token).strip() != "audience"
        )
        used_identity_keys.update(
            identity_resolver.key(token)
            for token in event.onscreen_entity_keys
            if str(token).strip() != "audience"
        )
        used_identity_keys.update(
            identity_resolver.key(token)
            for token in event.perceivable_by
            if str(token).strip() != "audience"
        )
        used_identity_keys.update(
            identity_resolver.key(token)
            for token in event.text_provenance.content_owner_keys
        )
    ordered_used_keys = [
        key for key in identity_by_key if key in used_identity_keys
    ]
    return ordered_used_keys, event_speaker_keys
