"""Compiler phase: assembles events from scene units, derives events when the model supplied none, and isolates paratext-only events."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app.schemas import ActionAgency, TextProvenance
from app.source_excerpt import structural_front_matter_ids

from .constants import ScreenplayIRFidelityError
from .contract_validation import _structural_context_authority_id, screenplay_beat_fields_repeat
from .models_core import IRActionParticipantDelivery, IRCoverageGroup, IRIdentity, IRScene, IRSceneUnit
from .models_event import IREvent, IRMetadata, ScreenplayGenerationIR
from .unit_ownership import _ir_normalize_strict_unit_ownership
from .unit_typed_validation import (
    _ir_build_identity_alias_index,
    _ir_compute_assigned_source_indices,
    _ir_validate_typed_scene_units,
)


def _ir_assemble_events_from_units(
    value: ScreenplayGenerationIR,
    episode: dict[str, Any],
    compiler_audit: list[dict[str, Any]],
    flat_units: list[tuple[IRScene, IRSceneUnit]],
    assigned_indices: list[int],
    segments_list: list[Any],
    format_version: str,
    strict_unit_ownership: bool,
    typed_visual_unit_contract: bool,
    identity_aliases: dict[str, set[str]],
    identity_display: dict[str, str],
    normalized_event_keys: dict[tuple[str, int], str],
    explicit_actor_keys_by_event: "defaultdict[tuple[str, str], list[str]]",
    explicit_target_keys_by_event: "defaultdict[tuple[str, str], list[str]]",
    onscreen_keys_by_event: "defaultdict[tuple[str, str], list[str]]",
) -> None:
    def merge_participant_deliveries(
        existing: list[IRActionParticipantDelivery],
        additions: list[IRActionParticipantDelivery],
    ) -> list[IRActionParticipantDelivery]:
        merged = [item.model_copy(deep=True) for item in existing]
        by_participant = {
            item.participant_key: item
            for item in merged
        }
        for addition in additions:
            current = by_participant.get(addition.participant_key)
            if current is None:
                current = addition.model_copy(deep=True)
                merged.append(current)
                by_participant[current.participant_key] = current
                continue
            current.audible = current.audible or addition.audible
            current.visible_effect = (
                current.visible_effect or addition.visible_effect
            )
            current.visible_reaction = (
                current.visible_reaction or addition.visible_reaction
            )
            current.observable_claim = "；".join(dict.fromkeys(filter(
                None,
                [
                    current.observable_claim.strip(),
                    addition.observable_claim.strip(),
                ],
            )))
        return merged

    derived_by_key: dict[str, IREvent] = {}
    for unit_index, ((scene, unit), segment_index) in enumerate(
        zip(flat_units, assigned_indices, strict=True),
        start=1,
    ):
        event_key = normalized_event_keys[(scene.key, unit_index)]
        actor_keys = list(
            explicit_actor_keys_by_event[(scene.key, event_key)]
        )
        target_keys = list(
            explicit_target_keys_by_event[(scene.key, event_key)]
        )
        onscreen_entity_keys = list(
            onscreen_keys_by_event[(scene.key, event_key)]
        )
        contextual_actor = False
        if (
            not actor_keys
            and unit.kind == "action"
            and not typed_visual_unit_contract
        ):
            contextual_key = f"context_actor_{scene.key}"
            contextual_actor = True
            actor_keys = [contextual_key]
            if contextual_key not in identity_aliases:
                display = (
                    f"{scene.scene_heading}中的未具名参与者"
                )
                value.identities.append(IRIdentity(
                    key=contextual_key,
                    authority_id=_structural_context_authority_id(
                        episode,
                        contextual_key,
                    ),
                    display_name=display,
                    kind="source_backed_scene_context_actor",
                    visual_policy="collective",
                    visual_canonical=(
                        f"仅按本场动作「{unit.text[:40]}」表现的未具名参与者"
                    ),
                    asset_requirement="optional",
                    voice_canonical="符合本场来源动作的环境或群体声线",
                    role_type="functional_character",
                    rationale=(
                        "动作单元未指向已登记人物，按当前场次和来源动作建立"
                        "局部 contextual actor，不跨场复用"
                    ),
                ))
                identity_aliases[contextual_key] = {
                    contextual_key,
                    display,
                }
                identity_display[contextual_key] = display
            if not onscreen_entity_keys:
                onscreen_entity_keys = [contextual_key]
        source_ids = (
            list(dict.fromkeys(unit.source_segment_ids))
            if strict_unit_ownership
            else [segments_list[segment_index].segment_id]
        )
        if unit.kind == "dialogue":
            speaker = identity_display.get(
                unit.speaker_key or "",
                unit.speaker_key or "当前说话人",
            )
            adapted_statement = f"{speaker}说出对白「{unit.text.strip()}」"
        else:
            adapted_statement = unit.text.strip()
            if len(re.sub(r"\s+", "", adapted_statement)) < 8:
                adapted_statement = (
                    f"{scene.summary or scene.story_function}中的动作："
                    f"{adapted_statement}"
                )
        existing = derived_by_key.get(event_key)
        if existing is not None:
            semantic_contract = (
                unit.narrative_layer,
                unit.event_priority,
                unit.render_policy,
            )
            if semantic_contract != (
                existing.narrative_layer,
                existing.event_priority,
                existing.render_policy,
            ):
                raise ValueError(
                    "同一 event_key 的语义优先级合同不一致："
                    f"{event_key}"
                )
            if (
                existing.state_subject_key != unit.state_subject_key
                or existing.state_subject_keys != unit.state_subject_keys
                or existing.environment_only != unit.environment_only
            ):
                raise ScreenplayIRFidelityError(
                    f"IR {format_version} {scene.key}.{event_key} 同一事件"
                    "包含不一致的 state subject/environment 声明"
                )
            existing.source_segment_ids = list(dict.fromkeys([
                *existing.source_segment_ids,
                *source_ids,
            ]))
            existing.actor_keys = list(dict.fromkeys([
                *existing.actor_keys,
                *actor_keys,
            ]))
            existing.target_keys = list(dict.fromkeys([
                *existing.target_keys,
                *target_keys,
            ]))
            existing.onscreen_entity_keys = list(dict.fromkeys([
                *existing.onscreen_entity_keys,
                *onscreen_entity_keys,
            ]))
            existing.participant_deliveries = merge_participant_deliveries(
                existing.participant_deliveries,
                unit.participant_deliveries,
            )
            agency_kinds = list(dict.fromkeys([
                existing.action_agency.kind,
                unit.action_agency.kind,
            ]))
            existing.action_agency = ActionAgency(
                kind=(
                    agency_kinds[0]
                    if len(agency_kinds) == 1
                    else "mixed"
                ),
                identity_bearing=bool(
                    existing.actor_keys or existing.target_keys
                ),
                source_segment_ids=list(existing.source_segment_ids),
            )
            existing.text_provenance = TextProvenance(
                kind=existing.text_provenance.kind,
                identity_keys=(
                    []
                    if existing.text_provenance.kind in (
                        "required_text",
                        "prop_text",
                        "on_screen_text",
                    )
                    else list(dict.fromkeys([
                        *existing.actor_keys,
                        *existing.target_keys,
                    ]))
                ),
                content_owner_keys=list(dict.fromkeys([
                    *existing.text_provenance.content_owner_keys,
                    *unit.text_provenance.content_owner_keys,
                ])),
                source_segment_ids=list(existing.source_segment_ids),
            )
            if not screenplay_beat_fields_repeat(
                existing.adapted_statement,
                adapted_statement,
            ):
                existing.adapted_statement = (
                    f"{existing.adapted_statement}；{adapted_statement}"
                )
            if unit.resulting_state.strip():
                existing.resulting_state = unit.resulting_state.strip()
            continue
        derived_by_key[event_key] = IREvent(
            key=event_key,
            scene_key=scene.key,
            narrative_layer=unit.narrative_layer,
            event_priority=unit.event_priority,
            render_policy=unit.render_policy,
            source_segment_ids=source_ids,
            adapted_statement=adapted_statement,
            resulting_state=unit.resulting_state.strip(),
            actor_keys=actor_keys,
            target_keys=target_keys,
            onscreen_entity_keys=onscreen_entity_keys,
            participant_deliveries=[
                delivery.model_copy(deep=True)
                for delivery in unit.participant_deliveries
            ],
            state_subject_key=unit.state_subject_key,
            state_subject_keys=list(unit.state_subject_keys),
            environment_only=unit.environment_only,
            action_agency=ActionAgency(
                kind=unit.action_agency.kind,
                identity_bearing=bool(actor_keys or target_keys),
                source_segment_ids=source_ids,
            ),
            text_provenance=TextProvenance(
                kind=unit.text_provenance.kind,
                identity_keys=(
                    []
                    if unit.text_provenance.kind in (
                        "required_text",
                        "prop_text",
                        "on_screen_text",
                    )
                    else list(dict.fromkeys([
                        *actor_keys,
                        *target_keys,
                    ]))
                ),
                content_owner_keys=list(
                    unit.text_provenance.content_owner_keys
                ),
                source_segment_ids=source_ids,
            ),
            dialogue_text=(
                unit.text.strip()
                if unit.kind == "dialogue"
                else ""
            ),
            required_text=unit.required_text,
            prop_text=unit.prop_text,
            on_screen_text=unit.on_screen_text,
            character_emotion="",
            decision_required=bool(actor_keys) and not contextual_actor,
            decision_reason=(
                "来源动作没有人物 actor，不建立人物自主决策链"
                if not actor_keys else ""
            ),
            must_keep=True,
        )
    value.events = list(derived_by_key.values())
    compiler_audit.append({
        "path": "events",
        "operation": "derive_from_scene_units",
        "count": len(value.events),
        "source_segment_count": len(segments_list),
        "reason": "scene_units_are_the_authored_playback_timeline",
    })


def _ir_derive_events_from_scene_units(
    value: ScreenplayGenerationIR,
    episode: dict[str, Any],
    source_text: str,
    compiler_audit: list[dict[str, Any]],
    segments: dict[str, Any],
    segments_list: list[Any],
    audit_only_source_ids: set[str],
    format_version: str,
    strict_unit_ownership: bool,
    typed_visual_unit_contract: bool,
) -> dict[str, str]:
    """Verbatim orchestration of the original `if not value.events:` block
    (2,609-3,730 lines in the pre-refactor source): derive one IREvent per
    scene unit, only run when the model did not author events directly.
    Returns identity_display, the sole piece of state this phase produces
    that a later phase (compact event-field derivation) still needs.
    """
    flat_units = [
        (scene, unit)
        for scene in value.scenes
        for unit in scene.units
    ]
    if not flat_units:
        raise ValueError("IR scenes.units 不能为空")
    front_matter_ids = (
        structural_front_matter_ids(segments_list)
        if strict_unit_ownership else set()
    )
    dramatic_segments = [
        segment for segment in segments_list
        if (
            segment.segment_id not in front_matter_ids
            and segment.segment_id not in audit_only_source_ids
        )
    ]
    expected_source_ids = {
        segment.segment_id for segment in dramatic_segments
    }
    all_source_ids = {
        segment.segment_id for segment in segments_list
    }
    if strict_unit_ownership:
        flat_units = _ir_normalize_strict_unit_ownership(
            value, compiler_audit, segments, segments_list,
            dramatic_segments, expected_source_ids, all_source_ids,
        )
    assigned_indices = _ir_compute_assigned_source_indices(
        value, flat_units, segments_list, source_text, strict_unit_ownership,
    )
    (
        identity_aliases,
        identity_display,
        event_scene_owners,
        normalized_event_keys,
        explicit_actor_keys_by_event,
        explicit_target_keys_by_event,
        onscreen_keys_by_event,
    ) = _ir_build_identity_alias_index(value, episode)
    _ir_validate_typed_scene_units(
        flat_units, format_version, typed_visual_unit_contract,
        identity_aliases, normalized_event_keys, event_scene_owners,
        explicit_actor_keys_by_event, explicit_target_keys_by_event,
        onscreen_keys_by_event,
    )
    _ir_assemble_events_from_units(
        value, episode, compiler_audit, flat_units, assigned_indices,
        segments_list, format_version, strict_unit_ownership,
        typed_visual_unit_contract, identity_aliases, identity_display,
        normalized_event_keys, explicit_actor_keys_by_event,
        explicit_target_keys_by_event, onscreen_keys_by_event,
    )
    return identity_display


def _ir_isolate_paratext_events(
    value: ScreenplayGenerationIR,
    metadata: IRMetadata,
    compiler_audit: list[dict[str, Any]],
) -> None:
    if not value.events:
        raise ValueError("IR events 不能为空")

    excluded_event_keys: set[str] = set()
    excluded_source_ids: list[str] = []
    for event in value.events:
        if event.narrative_layer == "paratext":
            if event.render_policy != "exclude_from_spine":
                raise ValueError(
                    f"paratext 事件 {event.key} 必须 exclude_from_spine"
                )
            excluded_event_keys.add(event.key)
            excluded_source_ids.extend(event.source_segment_ids)
        elif event.render_policy == "exclude_from_spine":
            raise ValueError(
                f"story 事件 {event.key} 不得 exclude_from_spine"
            )
    if excluded_event_keys:
        excluded_ids = list(dict.fromkeys(excluded_source_ids))
        covered_as_audit = {
            source_id
            for group in value.coverage
            if group.disposition == "audit_only"
            for source_id in group.source_segment_ids
        }
        missing_audit_ids = [
            source_id
            for source_id in excluded_ids
            if source_id not in covered_as_audit
        ]
        if missing_audit_ids:
            value.coverage.append(IRCoverageGroup(
                source_segment_ids=missing_audit_ids,
                disposition="audit_only",
                projection_policy="audit_only",
                reason=(
                    "来源内容属于非剧情旁文本，保留来源审计，"
                    "不进入成片叙事 spine"
                ),
            ))
        value.events = [
            event for event in value.events
            if event.key not in excluded_event_keys
        ]
        value.beats = [
            beat for beat in value.beats
            if not set(beat.source_segment_ids).issubset(excluded_ids)
        ]
        retained_scenes: list[IRScene] = []
        for scene in value.scenes:
            scene.units = [
                unit for unit in scene.units
                if unit.event_key not in excluded_event_keys
            ]
            if scene.units:
                retained_scenes.append(scene)
        value.scenes = retained_scenes
        if not value.events or not value.scenes:
            raise ValueError("非剧情旁文本隔离后没有可成片剧情事件")
        metadata.must_keep_ending = (
            value.events[-1].resulting_state
            or value.events[-1].completion_condition
            or metadata.must_keep_ending
        )
        compiler_audit.append({
            "path": "events",
            "operation": "exclude_paratext_from_picture_spine",
            "event_keys": sorted(excluded_event_keys),
            "source_segment_ids": excluded_ids,
        })
