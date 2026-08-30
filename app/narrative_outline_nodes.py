"""Per-event ShotTask-node synthesis (phase E) for
normalize_narrative_storyboard_outline: decide how many nodes (support /
dialogue / main) each event needs and build them from its model-authored
base shot(s).

Split out of narrative_outline.py -- see that function's docstring. This
was the outer function's largest loop body (nested one level inside "for
event in plan.events"); it is extracted verbatim (mechanically dedented)
with its two loop-level ``continue`` statements translated to ``return``
(there is no enclosing loop to continue once this is its own function) and
the ``already_projected`` branch's direct append into the caller's shared
``nodes`` list changed to build-and-return its own list -- same final
content, since the caller does ``nodes.extend(...)`` with the return value.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app import config


def _build_outline_nodes_for_event(
    event: Any,
    base_by_event: dict[str, Any],
    bases_by_event: dict[str, list[Any]],
    already_projected: bool,
    key_ids_by_event: dict[str, list[str]],
    key_line_meta: dict[str, tuple[str, str, str]],
    actions: dict[str, Any],
    deltas_by_event: dict[str, list[str]],
    delta_paths: dict[str, tuple[str, Any, str]],
    plan: Any,
    evidence_by_event: dict[str, list[Any]],
    character_state_ids_by_event: dict[str, list[str]],
) -> list[tuple[str, str, Any]]:
    """Build the (event_id, role, shot) nodes for one event; [] if the event has no owning base shot."""
    base = base_by_event.get(event.event_id)
    if base is None:
        return []
    event_bases = list(bases_by_event[event.event_id])
    if already_projected:
        already_projected_nodes = []
        for event_base in event_bases:
            contribution = event_base.shot_contribution
            role = (
                "support"
                if (
                    contribution is not None
                    and contribution.target_delta_ids
                    and not event_base.primary_action_id
                    and not event_base.key_line_ids
                )
                else "dialogue" if event_base.key_line_ids else "main"
            )
            already_projected_nodes.append((event.event_id, role, event_base))
        return already_projected_nodes
    event_key_ids = key_ids_by_event.get(event.event_id) or [
        key_id
        for event_base in event_bases
        for key_id in event_base.key_line_ids
        if key_id in key_line_meta
    ]
    existing_key_ids = {
        key_id
        for event_base in event_bases
        for key_id in event_base.key_line_ids
    }
    missing_key_ids = [
        key_id
        for key_id in event_key_ids
        if key_id not in existing_key_ids
    ]
    dialogue_groups: list[list[str]] = []
    current_group: list[str] = []
    current_chars = 0
    current_speaker = ""
    for key_id in missing_key_ids:
        speaker, line, _chain_id = key_line_meta[key_id]
        line_chars = len(
            "".join(
                character
                for character in line
                if character.isalnum()
            )
        )
        if (
            current_group
            and (
                speaker != current_speaker
                or current_chars + line_chars
                > config.MAX_SPOKEN_CHARS_PER_SHOT
            )
        ):
            dialogue_groups.append(current_group)
            current_group = []
            current_chars = 0
        current_group.append(key_id)
        current_chars += line_chars
        current_speaker = speaker
    if current_group:
        dialogue_groups.append(current_group)

    action_s = sum(
        max(0.0, phase.estimated_min_s)
        for action_id in event.action_ids
        for action in [actions.get(action_id)]
        if action is not None
        for phase in action.temporal_phases
    )
    processing_by_prior: defaultdict[str, float] = defaultdict(float)
    for delta_id in deltas_by_event[event.event_id]:
        prior_id, delta, _destination = delta_paths[delta_id]
        processing_by_prior[prior_id] += max(
            0.0,
            delta.required_processing_s,
        )
    processing_s = max(processing_by_prior.values(), default=0.0)
    reaction_s = (
        1.0 if character_state_ids_by_event[event.event_id] else 0.0
    )
    def _spoken_seconds(key_ids: list[str]) -> float:
        spoken_chars = sum(
            len(
                "".join(
                    character
                    for character in key_line_meta.get(
                        key_id,
                        ("", "", ""),
                    )[1]
                    if character.isalnum()
                )
            )
            for key_id in key_ids
        )
        return (
            spoken_chars
            * float(config.VIDEO_DURATION_MIN_S)
            / float(config.SPOKEN_CHARS_PER_5_SECONDS)
        )

    first_dialogue_s = 0.0
    if dialogue_groups:
        first_dialogue_s = _spoken_seconds(dialogue_groups[0])
    elif event_bases:
        first_dialogue_s = max(
            (_spoken_seconds(list(item.key_line_ids)) for item in event_bases),
            default=0.0,
        )
    needs_support = bool(
        processing_s > 0
        and (
            action_s + processing_s + reaction_s
            > config.VIDEO_DURATION_MAX_S
            or processing_s + first_dialogue_s
            > config.VIDEO_DURATION_MAX_S
        )
    )
    support_completes_event = bool(
        needs_support
        and not event.action_ids
        and not event_key_ids
        and not event.effects_add
        and not event.effects_remove
        and not character_state_ids_by_event[event.event_id]
    )
    event_nodes: list[tuple[str, str, Any]] = []
    if needs_support:
        support = base.model_copy(deep=True)
        support.primary_action_id = None
        support.supporting_action_ids = []
        support.action_phase_ids = []
        support.primary_action = (
            next(
                (
                    window.readability_reason
                    for window in plan.readability_windows
                    if set(window.target_delta_ids).intersection(
                        deltas_by_event[event.event_id]
                    )
                ),
                "",
            )
            or next(
                (
                    item.observable_claim
                    for item in evidence_by_event[event.event_id]
                ),
                support.beat,
            )
        )
        support.state_out = support.primary_action
        support.key_line_ids = []
        support.audio_cast = []
        event_nodes.append((event.event_id, "support", support))
    completion = "；".join(dict.fromkeys(
        str(action.completion_condition or "").strip()
        for action_id in event.action_ids
        for action in [actions.get(action_id)]
        if action is not None
        and str(action.completion_condition or "").strip()
    ))
    action_delivery_texts = {
        text
        for action_id in event.action_ids
        for action in [actions.get(action_id)]
        if action is not None
        for text in (
            str(action.semantic_intent or "").strip(),
            str(action.completion_condition or "").strip(),
        )
        if text
    }
    main_base_index = next(
        (
            index
            for index, event_base in enumerate(event_bases)
            if str(event_base.primary_action or "").strip()
            in action_delivery_texts
        ),
        None,
    )
    for base_index, event_base in enumerate(event_bases):
        if support_completes_event:
            continue
        spoken_s = _spoken_seconds(list(event_base.key_line_ids))
        if (
            event_base.key_line_ids
            and spoken_s + action_s + reaction_s
            > config.VIDEO_DURATION_MAX_S
        ):
            dialogue = event_base.model_copy(deep=True)
            dialogue.primary_action_id = None
            dialogue.supporting_action_ids = []
            dialogue.action_phase_ids = []
            spoken_text = "；".join(
                key_line_meta.get(key_id, ("", "", ""))[1]
                for key_id in dialogue.key_line_ids
                if key_line_meta.get(key_id, ("", "", ""))[1]
            )
            if spoken_text:
                dialogue.primary_action = spoken_text
                dialogue.beat = spoken_text
                dialogue.covers = spoken_text
                dialogue.state_out = spoken_text
            event_nodes.append((event.event_id, "dialogue", dialogue))

            main = event_base.model_copy(deep=True)
            main.key_line_ids = []
            main.audio_cast = []
            if completion:
                main.state_in = dialogue.state_out
                main.primary_action = completion
                main.state_out = completion
                main.beat = completion
                main.covers = completion
            event_nodes.append((event.event_id, "main", main))
            continue
        event_nodes.append((
            event.event_id,
            (
                "main"
                if base_index == main_base_index
                else "dialogue" if event_base.key_line_ids else "main"
            ),
            event_base,
        ))
    generated_dialogues: list[tuple[str, str, Any]] = []
    for group in dialogue_groups:
        dialogue = base.model_copy(deep=True)
        dialogue.primary_action_id = None
        dialogue.supporting_action_ids = []
        dialogue.action_phase_ids = []
        dialogue.key_line_ids = list(group)
        speakers = list(dict.fromkeys(
            key_line_meta[key_id][0]
            for key_id in group
            if key_line_meta[key_id][0]
        ))
        dialogue.audio_cast = speakers
        dialogue.characters_visible = speakers[:1]
        dialogue.primary_action = "；".join(
            key_line_meta[key_id][1]
            for key_id in group
        )
        dialogue.beat = dialogue.primary_action
        dialogue.covers = dialogue.primary_action
        generated_dialogues.append(
            (event.event_id, "dialogue", dialogue)
        )
    if generated_dialogues:
        insert_at = next(
            (
                index
                for index, (_event_id, role, _shot) in enumerate(
                    event_nodes
                )
                if role == "main"
            ),
            len(event_nodes),
        )
        event_nodes[insert_at:insert_at] = generated_dialogues
    if generated_dialogues and main_base_index is None and completion:
        directed_main = next(
            (
                event_shot
                for _event_id, role, event_shot in reversed(event_nodes)
                if role == "main"
            ),
            None,
        )
        if directed_main is not None:
            directed_main.state_in = ""
            directed_main.primary_action = completion
            directed_main.state_out = completion
            directed_main.beat = completion
            directed_main.covers = completion
    if (
        not support_completes_event
        and not any(
            role == "main"
            for _event_id, role, _shot in event_nodes
        )
    ):
        main = base.model_copy(deep=True)
        main.key_line_ids = []
        main.audio_cast = []
        main.primary_action = (
            completion
            or "人物闭口呈现本事件完成后的可见反应与状态结果"
        )
        main.state_out = main.primary_action
        main.beat = main.primary_action
        main.covers = main.primary_action
        event_nodes.append((event.event_id, "main", main))
    for _event_id, _role, event_shot in event_nodes:
        for field_name in (
            "scene_id",
            "scene_time",
            "scene_name",
            "scene_setting",
        ):
            if getattr(event_shot, field_name):
                continue
            setattr(event_shot, field_name, getattr(base, field_name))
    return event_nodes


def _build_outline_nodes(
    plan: Any,
    base_by_event: dict[str, Any],
    bases_by_event: dict[str, list[Any]],
    already_projected: bool,
    key_ids_by_event: dict[str, list[str]],
    key_line_meta: dict[str, tuple[str, str, str]],
    actions: dict[str, Any],
    deltas_by_event: dict[str, list[str]],
    delta_paths: dict[str, tuple[str, Any, str]],
    evidence_by_event: dict[str, list[Any]],
    character_state_ids_by_event: dict[str, list[str]],
) -> list[tuple[str, str, Any]]:
    """Build the full ordered node list across every event in the plan."""
    nodes: list[tuple[str, str, Any]] = []
    for event in plan.events:
        nodes.extend(_build_outline_nodes_for_event(
            event, base_by_event, bases_by_event, already_projected,
            key_ids_by_event, key_line_meta, actions, deltas_by_event,
            delta_paths, plan, evidence_by_event, character_state_ids_by_event,
        ))
    return nodes
