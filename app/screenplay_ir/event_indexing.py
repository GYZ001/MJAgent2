"""Compiler phase: indexes scenes/events/identities, derives missing event fields, and derives beats and audience priors."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app.schemas import Bible

from .contract_validation import screenplay_beat_fields_repeat
from .identity_authorities import _apply_authoritative_ir_identity_resolutions
from .models_core import IRBeat, IRIdentity, IRScene, IRSceneUnit
from .models_event import IRActionPhase, IRAudiencePrior, IREvent, IRMetadata, ScreenplayGenerationIR
from .prompt_context import _unique_by_key


def _ir_index_scenes_events_identities(
    value: ScreenplayGenerationIR,
    episode: dict[str, Any],
    bible: Bible,
    source_text: str,
    compiler_audit: list[dict[str, Any]],
) -> tuple[
    dict[str, IRScene],
    dict[str, IREvent],
    dict[str, IRIdentity],
    "defaultdict[str, list[IRSceneUnit]]",
    "defaultdict[str, list[str]]",
]:
    scene_by_key = _unique_by_key(value.scenes, "scenes")
    event_by_key = _unique_by_key(value.events, "events")
    identity_by_key = _unique_by_key(value.identities, "identities")
    for identity in identity_by_key.values():
        if not identity.source_names:
            source_name_match = re.search(
                r"原文(?:中的|中|称谓为?|称为)\s*[「“\"]?"
                r"([^，。；」”\"]+)",
                identity.rationale,
            )
            if source_name_match is not None:
                source_name = source_name_match.group(1).strip()
                if source_name in source_text:
                    identity.source_names = [source_name]
        source_names = list(dict.fromkeys(
            str(name).strip()
            for name in identity.source_names
            if str(name).strip() and str(name).strip() in source_text
        ))
        identity.source_names = source_names
        if source_names and identity.display_name != source_names[0]:
            compiler_audit.append({
                "path": f"identities.{identity.key}.display_name",
                "operation": "bind_source_canonical_name",
                "from": identity.display_name,
                "to": source_names[0],
                "reason": "identity_source_name_is_directly_authorized",
            })
            identity.display_name = source_names[0]
    _apply_authoritative_ir_identity_resolutions(
        value,
        episode=episode,
        bible=bible,
        audit=compiler_audit,
    )
    identity_by_key = _unique_by_key(value.identities, "identities")
    units_by_event: defaultdict[str, list[IRSceneUnit]] = defaultdict(list)
    for scene in value.scenes:
        for unit in scene.units:
            units_by_event[unit.event_key].append(unit)
    event_keys_by_scene: defaultdict[str, list[str]] = defaultdict(list)
    for event in value.events:
        event_keys_by_scene[event.scene_key].append(event.key)
    return (
        scene_by_key, event_by_key, identity_by_key,
        units_by_event, event_keys_by_scene,
    )


def _ir_derive_missing_event_fields(
    value: ScreenplayGenerationIR,
    scene_by_key: dict[str, IRScene],
    event_keys_by_scene: "defaultdict[str, list[str]]",
    units_by_event: "defaultdict[str, list[IRSceneUnit]]",
    identity_display: dict[str, str],
    metadata: IRMetadata,
    compiler_audit: list[dict[str, Any]],
) -> bool:
    for index, event in enumerate(value.events):
        previous = value.events[index - 1] if index else None
        next_event = (
            value.events[index + 1]
            if index + 1 < len(value.events) else None
        )
        event_units = list(units_by_event.get(event.key, []))
        action_units = [
            unit.text.strip()
            for unit in event_units
            if unit.kind == "action" and unit.text.strip()
        ]
        derived_fields: list[str] = []
        if not event.adapted_statement.strip():
            event.adapted_statement = (
                "；".join(action_units)
                or event.resulting_state
                or event.action_intent
                or event.completion_condition
            )
            derived_fields.append("adapted_statement")
        if not event.precondition_state.strip():
            event.precondition_state = (
                previous.resulting_state
                if previous and previous.resulting_state.strip()
                else metadata.opening
                or "本集首个事件发生前的来源状态"
            )
            derived_fields.append("precondition_state")
        if not event.action_intent.strip():
            event.action_intent = event.adapted_statement
            derived_fields.append("action_intent")
        if not event.completion_condition.strip():
            event.completion_condition = (
                action_units[-1]
                if action_units else event.adapted_statement
            )
            derived_fields.append("completion_condition")
        if not event.resulting_state.strip():
            scene = scene_by_key[event.scene_key]
            scene_event_keys = event_keys_by_scene[event.scene_key]
            is_scene_terminal = (
                bool(scene_event_keys)
                and scene_event_keys[-1] == event.key
            )
            scene_outcome = next(
                (
                    candidate.strip()
                    for candidate in (
                        scene.exit_state,
                        scene.turn,
                    )
                    if (
                        candidate.strip()
                        and not screenplay_beat_fields_repeat(
                            event.action_intent,
                            candidate,
                        )
                    )
                ),
                "",
            )
            dialogue_units = [
                unit
                for unit in event_units
                if unit.kind == "dialogue"
            ]
            if is_scene_terminal and scene_outcome:
                event.resulting_state = scene_outcome
            elif dialogue_units:
                last_dialogue = dialogue_units[-1]
                speaker = identity_display.get(
                    last_dialogue.speaker_key or "",
                    last_dialogue.speaker_key or "当前说话人",
                )
                event.resulting_state = {
                    "question": (
                        f"{speaker}提出的问题成为下一话轮必须回应的焦点"
                    ),
                    "response": (
                        f"{speaker}完成回应，前一问题获得明确答复"
                    ),
                    "decision": (
                        f"{speaker}作出明确决定，后续行动条件已经成立"
                    ),
                    "announcement": (
                        f"{speaker}公布的信息成为在场者已知事实"
                    ),
                    "trigger": (
                        f"{speaker}的发言触发后续人物或局势反应"
                    ),
                    "statement": (
                        f"{speaker}表达的信息进入当前场景的共同认知"
                    ),
                }.get(
                    last_dialogue.function,
                    f"{speaker}完成本话轮信息交付",
                )
            elif (
                next_event is not None
                and next_event.scene_key == event.scene_key
            ):
                next_statement = (
                    next_event.action_intent
                    or next_event.adapted_statement
                    or next_event.completion_condition
                ).strip()
                event.resulting_state = (
                    "当前动作完成，局势推进到下一事件"
                    + (
                        f"「{next_statement[:80]}」发生前"
                        if next_statement else "的前置状态"
                    )
                )
            else:
                event.resulting_state = (
                    scene_outcome
                    or f"当前动作完成，本场「{scene.story_function}」进入下一阶段"
                )
            derived_fields.append("resulting_state")
        if not event.observable_claim.strip():
            event.observable_claim = (
                "；".join(action_units)
                or event.completion_condition
            )
            derived_fields.append("observable_claim")
        if not event.action_phases:
            phase_texts = action_units or [event.completion_condition]
            event.action_phases = [
                IRActionPhase(
                    start_condition=(
                        event.precondition_state
                        if phase_index == 0
                        else phase_texts[phase_index - 1]
                    ),
                    end_condition=phase_text,
                    estimated_min_s=1.0,
                    splittable_after=phase_index < len(phase_texts) - 1,
                )
                for phase_index, phase_text in enumerate(phase_texts)
            ]
            derived_fields.append("action_phases")
        if derived_fields:
            compiler_audit.append({
                "path": f"events.{event.key}",
                "operation": "derive_compact_event_fields",
                "fields": derived_fields,
                "reason": "ordered_events_and_scene_units_are_authoritative",
            })
        if screenplay_beat_fields_repeat(
            event.action_intent,
            event.resulting_state,
        ):
            # 内容质量判断归质量闸门：validators 有一条同源规则
            # [SPINE_ACTION_TURN_DUPLICATE]，它带着"turn 必须写该动作完成后新
            # 成立的状态"这样可操作的诉求，而且身处修复循环之内。编译器再复制
            # 一份致命拷贝，只会把可修复的问题变成崩溃（生产上 EP3 卡死在
            # IR_MERGE），既不增加安全性，也让修复模型看不到该改什么。
            # 只留审计痕迹，判定权交给闸门。
            compiler_audit.append({
                "path": f"events.{event.key}",
                "operation": "flag_action_outcome_duplicate",
                "reason": "quality_gate_owns_spine_action_turn_duplicate",
            })
    beats_were_derived = not value.beats
    return beats_were_derived


def _ir_derive_beats_and_priors(
    value: ScreenplayGenerationIR,
    identity_by_key: dict[str, IRIdentity],
    beats_were_derived: bool,
    compiler_audit: list[dict[str, Any]],
) -> tuple[dict[str, IRBeat], dict[str, IRAudiencePrior]]:
    if beats_were_derived:
        value.beats = [
            IRBeat(
                key=f"derived-beat-{index}",
                who="、".join(
                    identity_by_key[token].display_name
                    if token in identity_by_key else token
                    for token in event.actor_keys
                    if str(token).strip() != "audience"
                ) or "当前事件主体",
                does=(
                    event.action_intent
                    or event.observable_claim
                    or event.completion_condition
                ),
                turn=event.resulting_state,
                purpose=(
                    event.adapted_statement
                    or event.observable_claim
                    or event.completion_condition
                ),
                source_segment_ids=list(event.source_segment_ids),
                must_keep=event.must_keep,
            )
            for index, event in enumerate(value.events, start=1)
        ]
        compiler_audit.append({
            "path": "beats",
            "operation": "derive",
            "count": len(value.beats),
            "reason": "events_are_the_single_semantic_authority",
        })
    repeated_beats = [
        beat.key
        for beat in value.beats
        if screenplay_beat_fields_repeat(beat.does, beat.turn)
    ]
    if repeated_beats:
        # 同上：这条规则的权威在质量闸门，它给的是可操作的改写要求。
        # 节拍本身就是从事件派生的，所以这里和上面报的是同一个内容缺陷。
        compiler_audit.append({
            "path": "beats",
            "operation": "flag_action_outcome_duplicate",
            "beats": repeated_beats[:20],
            "reason": "quality_gate_owns_spine_action_turn_duplicate",
        })
    beat_by_key = _unique_by_key(value.beats, "beats")
    prior_values = list(value.audience_priors)
    if len(prior_values) < 2:
        prior_values = [
            *prior_values,
            *[
                IRAudiencePrior(
                    key=f"derived_prior_{index}",
                    description=description,
                    target_stance="suspected" if index == 2 else "believed",
                    target_confidence=0.65 if index == 2 else 0.8,
                )
                for index, description in (
                    (1, "不了解本集背景、仅凭当前画面进入的一次观看者"),
                    (2, "记得项目基础人物关系、但不知道本集结果的一次观看者"),
                )
                if len(prior_values) < index
            ],
        ]
        compiler_audit.append({
            "path": "audience_priors",
            "operation": "derive",
            "count": len(prior_values),
            "reason": "project_level_once_viewing_priors",
        })
    prior_by_key = _unique_by_key(prior_values, "audience_priors")
    return beat_by_key, prior_by_key
