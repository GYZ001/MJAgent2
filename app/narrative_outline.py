"""Deterministic ShotTask projection from an approved narrative graph."""
from __future__ import annotations

from collections import defaultdict
from math import ceil
from typing import Any

from app import config
from app.narrative import _target_state_fragment_matches
from app.schemas import (
    AudienceStatePathRef,
    EpisodeScreenplay,
    NarrativeBoundaryContract,
    ShotCapacityBudget,
    ShotContribution,
    StoryboardOutline,
)


def normalize_split_action_owner_completions(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[dict[str, Any]]:
    """Repair terminal action shots created after an event's dialogue splits."""
    plan = screenplay.narrative_plan
    if plan is None:
        return []

    dialogue_event_ids = {
        event_id
        for shot in outline.shots
        if shot.key_line_ids
        for event_id in (
            list(shot.event_ids)
            or ([shot.story_event_id] if shot.story_event_id else [])
        )
    }
    actions = {
        action.action_id: action
        for action in plan.atomic_actions
    }
    changes: list[dict[str, Any]] = []
    for shot in outline.shots:
        event_ids = (
            list(shot.event_ids)
            or ([shot.story_event_id] if shot.story_event_id else [])
        )
        if (
            not shot.primary_action_id
            or shot.key_line_ids
            or shot.audio_cast
            or not dialogue_event_ids.intersection(event_ids)
        ):
            continue
        action_ids = [
            shot.primary_action_id,
            *shot.supporting_action_ids,
        ]
        completion_parts = [
            str(action.completion_condition or "").strip()
            for action_id in action_ids
            for action in [actions.get(action_id)]
            if action is not None
            and str(action.completion_condition or "").strip()
        ]
        completion = "；".join(dict.fromkeys(completion_parts))
        if not completion:
            continue
        for field, value in (
            ("state_in", ""),
            ("primary_action", completion),
            ("state_out", completion),
            ("beat", completion),
            ("covers", completion),
        ):
            current = getattr(shot, field)
            if current == value:
                continue
            setattr(shot, field, value)
            changes.append({
                "shot_no": shot.shot_no,
                "field": field,
                "from": current,
                "to": value,
                "reason": "split_event_action_completion",
            })
    return changes


def normalize_narrative_storyboard_outline(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[dict[str, Any]]:
    """Project graph-owned fields while preserving model-authored directing text.

    The model chooses the visible beat, scene presentation and legacy delivery
    fields. Event state replay, action ownership, audience staging, cumulative
    ledgers, capacity and boundaries are deterministic consequences of the
    published narrative plan.
    """
    plan = screenplay.narrative_plan
    if plan is None or not outline.shots or not plan.events:
        return []

    events = {item.event_id: item for item in plan.events}
    actions = {item.action_id: item for item in plan.atomic_actions}
    event_order = {
        item.event_id: position
        for position, item in enumerate(plan.events)
    }
    key_line_meta: dict[str, tuple[str, str, str]] = {}
    chain_key_ids: dict[str, list[str]] = {}
    key_number = 1
    for chain in screenplay.dialogue_chains:
        ids: list[str] = []
        for turn in chain.turns:
            key_id = f"KL{key_number:02d}"
            key_line_meta[key_id] = (
                turn.speaker,
                turn.line,
                chain.chain_id,
            )
            ids.append(key_id)
            key_number += 1
        chain_key_ids[chain.chain_id] = ids
    base_by_event: dict[str, Any] = {}
    for shot in outline.shots:
        event_ids = list(shot.event_ids or [])
        if not event_ids and shot.story_event_id:
            event_ids = [shot.story_event_id]
        for event_id in event_ids:
            if event_id in events:
                base_by_event.setdefault(event_id, shot)
    required_events = [
        item.event_id
        for item in plan.events
        if item.must_keep and item.delivery_policy == "deliver"
    ]
    if any(event_id not in base_by_event for event_id in required_events):
        return []

    key_event: dict[str, str] = {}
    for event_id, base in base_by_event.items():
        for key_id in base.key_line_ids:
            if key_id in key_line_meta:
                key_event[key_id] = event_id
    for key_ids in chain_key_ids.values():
        known_events = {
            key_event[key_id]
            for key_id in key_ids
            if key_id in key_event
        }
        if len(known_events) == 1:
            event_id = next(iter(known_events))
            for key_id in key_ids:
                key_event.setdefault(key_id, event_id)
    chain_events: dict[str, str] = {}
    for chain_id, key_ids in chain_key_ids.items():
        known_events = {
            key_event[key_id]
            for key_id in key_ids
            if key_id in key_event
        }
        if len(known_events) == 1:
            chain_events[chain_id] = next(iter(known_events))

    def _bigrams(value: str) -> set[str]:
        compact = "".join(
            character
            for character in value
            if character.isalnum()
        )
        return {
            compact[index:index + 2]
            for index in range(max(0, len(compact) - 1))
        }

    proposition_text = {
        item.proposition_id: item.canonical_statement
        for item in plan.propositions
    }
    event_semantic_text = {
        event.event_id: "".join([
            *[
                evidence.observable_claim
                for evidence in plan.evidence
                if (
                    evidence.anchor.type == "event"
                    and evidence.anchor.id == event.event_id
                )
            ],
            *[
                proposition_text.get(proposition_id, "")
                for proposition_id in event.proposition_ids
            ],
        ])
        for event in plan.events
    }
    ordered_chain_ids = [
        chain.chain_id
        for chain in screenplay.dialogue_chains
    ]
    for chain_index, chain_id in enumerate(ordered_chain_ids):
        if chain_id in chain_events:
            continue
        key_ids = chain_key_ids.get(chain_id) or []
        chain_text = "".join(
            key_line_meta[key_id][1]
            for key_id in key_ids
        )
        chain_bigrams = _bigrams(chain_text)
        previous_event = next(
            (
                chain_events[candidate]
                for candidate in reversed(
                    ordered_chain_ids[:chain_index]
                )
                if candidate in chain_events
            ),
            "",
        )
        next_event = next(
            (
                chain_events[candidate]
                for candidate in ordered_chain_ids[chain_index + 1:]
                if candidate in chain_events
            ),
            "",
        )
        interval_event_ids = [
            event_id
            for event_id in event_semantic_text
            if (
                (
                    not previous_event
                    or event_order[event_id] > event_order[previous_event]
                )
                and (
                    not next_event
                    or event_order[event_id] < event_order[next_event]
                )
            )
        ]
        semantic_candidates = (
            interval_event_ids
            if interval_event_ids
            else list(event_semantic_text)
        )
        scored = sorted(
            (
                (
                    len(chain_bigrams & _bigrams(event_text))
                    / max(1, len(chain_bigrams)),
                    event_order.get(event_id, -1),
                    event_id,
                )
                for event_id, event_text in event_semantic_text.items()
                if event_id in semantic_candidates
            ),
            reverse=True,
        )
        selected_event = (
            scored[0][2]
            if scored and scored[0][0] > 0
            else ""
        )
        if not selected_event:
            selected_event = next_event
        if not selected_event:
            selected_event = previous_event
        if selected_event:
            chain_events[chain_id] = selected_event
            for key_id in key_ids:
                key_event.setdefault(key_id, selected_event)
    key_ids_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for key_id in key_line_meta:
        event_id = key_event.get(key_id)
        if event_id:
            key_ids_by_event[event_id].append(key_id)

    paths: dict[str, Any] = {}
    delta_paths: dict[str, tuple[str, Any, str]] = {}
    delta_destinations: dict[str, str] = {}
    for intent in plan.experience_intents:
        for path in intent.audience_paths:
            paths[path.audience_prior_id] = path
            ordered = sorted(
                path.target_deltas,
                key=lambda item: (
                    event_order.get(item.deadline_event_id, len(event_order)),
                    item.target_delta_id,
                ),
            )
            prior_states = [
                state
                for state in plan.audience_states
                if state.audience_prior_id == path.audience_prior_id
            ]
            deadline_groups: list[list[Any]] = []
            for delta in ordered:
                if (
                    not deadline_groups
                    or deadline_groups[-1][0].deadline_event_id
                    != delta.deadline_event_id
                ):
                    deadline_groups.append([])
                deadline_groups[-1].append(delta)
            current_state_id = path.audience_state_in_id
            for group_index, group in enumerate(deadline_groups):
                destination = path.audience_state_out_target_id
                if group_index + 1 < len(deadline_groups):
                    next_group = deadline_groups[group_index + 1]
                    candidates = [
                        state.audience_state_id
                        for state in prior_states
                        if (
                            state.audience_state_id
                            not in {
                                path.audience_state_in_id,
                                path.audience_state_out_target_id,
                            }
                            and all(
                                _target_state_fragment_matches(
                                    delta,
                                    delta.to_state,
                                    state,
                                )
                                for delta in group
                            )
                            and all(
                                _target_state_fragment_matches(
                                    next_delta,
                                    next_delta.from_state,
                                    state,
                                )
                                for next_delta in next_group
                            )
                        )
                    ]
                    # Some authority graphs intentionally expose only the
                    # initial and final audience snapshots. In that case the
                    # delta ledger still records this deadline's contribution,
                    # while the coarse snapshot ID remains unchanged until a
                    # declared later state is reached.
                    destination = (
                        candidates[0]
                        if len(candidates) == 1
                        else current_state_id
                    )
                for delta in group:
                    delta_paths[delta.target_delta_id] = (
                        path.audience_prior_id,
                        delta,
                        destination,
                    )
                    delta_destinations[delta.target_delta_id] = destination
                current_state_id = destination

    deltas_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for delta_id, (_prior_id, delta, _destination) in delta_paths.items():
        deltas_by_event[delta.deadline_event_id].append(delta_id)

    evidence_by_event: defaultdict[str, list[Any]] = defaultdict(list)
    for evidence in plan.evidence:
        if evidence.anchor.type == "event":
            evidence_by_event[evidence.anchor.id].append(evidence)
    character_state_ids_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for state in [*plan.character_states, *plan.character_beliefs]:
        if state.anchor.type != "event":
            continue
        state_id = (
            getattr(state, "character_state_id", None)
            or getattr(state, "character_belief_id", None)
        )
        if state_id:
            character_state_ids_by_event[state.anchor.id].append(state_id)

    window_seconds_by_event: defaultdict[str, float] = defaultdict(float)
    for window in plan.readability_windows:
        for event_id in window.event_ids:
            window_seconds_by_event[event_id] = max(
                window_seconds_by_event[event_id],
                float(window.scheduled_processing_s or 0),
            )

    nodes: list[tuple[str, str, Any]] = []
    for event in plan.events:
        base = base_by_event.get(event.event_id)
        if base is None:
            continue
        event_key_ids = key_ids_by_event.get(event.event_id) or [
            key_id
            for key_id in base.key_line_ids
            if key_id in key_line_meta
        ]
        dialogue_groups: list[list[str]] = []
        current_group: list[str] = []
        current_chars = 0
        current_speaker = ""
        for key_id in event_key_ids:
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
        first_dialogue_s = 0.0
        if dialogue_groups:
            first_dialogue_chars = sum(
                len(
                    "".join(
                        character
                        for character in key_line_meta[key_id][1]
                        if character.isalnum()
                    )
                )
                for key_id in dialogue_groups[0]
            )
            first_dialogue_s = (
                first_dialogue_chars
                * float(config.VIDEO_DURATION_MIN_S)
                / float(config.SPOKEN_CHARS_PER_5_SECONDS)
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
            nodes.append((event.event_id, "support", support))
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
            nodes.append((event.event_id, "dialogue", dialogue))
        main = base.model_copy(deep=True)
        if dialogue_groups:
            main.key_line_ids = []
            main.audio_cast = []
            if not event.action_ids:
                main.primary_action = "人物闭口呈现本事件完成后的可见反应与状态结果"
                main.beat = main.primary_action
                main.covers = main.primary_action
        nodes.append((event.event_id, "main", main))

    for position, (_event_id, _role, shot) in enumerate(nodes, start=1):
        shot.shot_no = position
        shot.shot_id = f"SH{position:03d}"

    positions_by_event: defaultdict[str, list[int]] = defaultdict(list)
    for position, (event_id, _role, _shot) in enumerate(nodes):
        positions_by_event[event_id].append(position)
    first_position = {
        event_id: positions[0]
        for event_id, positions in positions_by_event.items()
    }
    last_position = {
        event_id: positions[-1]
        for event_id, positions in positions_by_event.items()
    }

    delta_owner_position: dict[str, int] = {}
    for event_id, delta_ids in deltas_by_event.items():
        positions = positions_by_event.get(event_id) or []
        if not positions:
            continue
        support_position = next(
            (
                position
                for position in positions
                if nodes[position][1] == "support"
            ),
            positions[0],
        )
        for delta_id in delta_ids:
            delta_owner_position[delta_id] = support_position

    task_owner_position: defaultdict[int, list[str]] = defaultdict(list)
    if nodes:
        for task in plan.assimilation_tasks:
            task_owner_position[0].append(task.assimilation_task_id)

    current_facts = set(plan.initial_state_fact_ids)
    current_audience_state = {
        prior_id: path.audience_state_in_id
        for prior_id, path in paths.items()
    }
    completed_actions: set[str] = set()
    completed_phases: set[str] = set()
    all_event_ids = list(events)
    normalized_shots = []
    changes: list[dict[str, Any]] = []

    for position, (event_id, role, shot) in enumerate(nodes):
        event = events[event_id]
        is_last_occurrence = position == last_position[event_id]
        is_support = role == "support"
        is_dialogue = role == "dialogue"
        action_ids = list(event.action_ids) if is_last_occurrence else []
        primary_action_id = action_ids[0] if action_ids else None
        supporting_action_ids = action_ids[1:]
        phase_ids = [
            phase.phase_id
            for action_id in action_ids
            for action in [actions.get(action_id)]
            if action is not None
            for phase in action.temporal_phases
        ]

        visible_ids = {
            entity_id
            for evidence in evidence_by_event[event_id]
            for entity_id in evidence.perceivable_by
            if entity_id != "audience"
        }
        for action_id in action_ids:
            action = actions.get(action_id)
            if action is not None:
                visible_ids.update(action.actor_ids)
                visible_ids.update(action.target_ids)

        shot.event_ids = [event_id]
        shot.story_event_id = event_id
        shot.primary_action_id = primary_action_id
        shot.supporting_action_ids = supporting_action_ids
        shot.action_phase_ids = phase_ids
        shot.visible_entity_ids = sorted(visible_ids)
        shot.offscreen_action_actor_ids = []
        shot.offscreen_action_target_ids = []

        shot.planned_state_in_fact_ids = sorted(current_facts)
        declared_add_ids = set(event.effects_add) if is_last_occurrence else set()
        declared_remove_ids = set(event.effects_remove) if is_last_occurrence else set()
        remove_ids = declared_remove_ids & current_facts
        add_ids = declared_add_ids - current_facts - remove_ids
        shot.planned_delta_add_fact_ids = sorted(add_ids)
        shot.planned_delta_remove_fact_ids = sorted(remove_ids)
        current_facts = (current_facts - remove_ids) | add_ids
        shot.planned_state_out_fact_ids = sorted(current_facts)
        shot.completed_before_action_ids = sorted(completed_actions)
        shot.completed_before_action_phase_ids = sorted(completed_phases)
        shot.reserved_future_event_ids = [
            candidate
            for candidate in all_event_ids
            if first_position.get(candidate, len(nodes)) > position
        ]

        path_inputs = dict(current_audience_state)
        owned_delta_ids = [
            delta_id
            for delta_id, owner_position in delta_owner_position.items()
            if owner_position == position
        ]
        for delta_id in sorted(
            owned_delta_ids,
            key=lambda item: (
                event_order.get(delta_paths[item][1].deadline_event_id, 0),
                item,
            ),
        ):
            prior_id, _delta, destination = delta_paths[delta_id]
            current_audience_state[prior_id] = destination
        shot.audience_state_paths = [
            AudienceStatePathRef.model_validate({
                "audience_prior_id": prior_id,
                "audience_state_in_id": path_inputs[prior_id],
                "audience_state_out_target_id": current_audience_state[prior_id],
            })
            for prior_id in sorted(paths)
        ]

        event_evidence_ids = [
            item.evidence_id
            for item in evidence_by_event[event_id]
        ]
        evidence_ids = (
            event_evidence_ids
            if (
                is_support
                or is_dialogue
                or not any(
                    item_role == "support"
                    for item_event, item_role, _item_shot in nodes
                    if item_event == event_id
                )
                or bool(action_ids)
            )
            else []
        )
        character_state_ids = (
            list(character_state_ids_by_event[event_id])
            if is_last_occurrence
            else []
        )
        audience_delta_ids = [
            current_audience_state[prior_id]
            for prior_id in sorted(paths)
            if path_inputs[prior_id] != current_audience_state[prior_id]
        ]
        shot.shot_contribution = ShotContribution.model_validate({
            "shot_contribution_id": f"SCONTRIB-{shot.shot_id}",
            "experience_intent_ids": [
                item.experience_intent_id
                for item in plan.experience_intents
            ],
            "target_delta_ids": owned_delta_ids,
            "assimilation_task_ids": task_owner_position.get(position, []),
            "evidence_ids": evidence_ids,
            "story_delta_fact_ids": sorted(add_ids | remove_ids),
            "character_state_delta_ids": character_state_ids,
            "audience_state_delta_ids": audience_delta_ids,
            "affective_delta": {},
            "spatial_temporal_delta": {},
            "dramatic_pressure_delta": 0.0,
        })

        action_s = sum(
            max(0.0, phase.estimated_min_s)
            for action_id in action_ids
            for action in [actions.get(action_id)]
            if action is not None
            for phase in action.temporal_phases
        )
        processing_by_prior: defaultdict[str, float] = defaultdict(float)
        for delta_id in owned_delta_ids:
            prior_id, delta, _destination = delta_paths[delta_id]
            processing_by_prior[prior_id] += max(
                0.0,
                delta.required_processing_s,
            )
        inference_s = max(processing_by_prior.values(), default=0.0)
        reaction_s = 1.0 if character_state_ids else 0.0
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
            for key_id in shot.key_line_ids
        )
        spoken_s = (
            spoken_chars
            * float(config.VIDEO_DURATION_MIN_S)
            / float(config.SPOKEN_CHARS_PER_5_SECONDS)
        )
        shot.capacity_budget = ShotCapacityBudget.model_validate({
            "action_phase_s": action_s,
            "spoken_and_text_s": spoken_s,
            "attention_switch_s": 0.0,
            "inference_processing_s": inference_s,
            "reaction_registration_s": reaction_s,
            "spatial_reorientation_s": 0.0,
            "entry_exit_settle_s": 0.0,
            "other_s": 0.0,
            "other_reason": None,
        })
        event_window_s = window_seconds_by_event[event_id]
        minimum_duration = max(
            5,
            int(ceil(action_s + spoken_s + inference_s + reaction_s)),
            int(ceil(event_window_s)) if not is_support else 0,
        )
        shot.duration_s = min(
            config.VIDEO_DURATION_MAX_S,
            max(config.VIDEO_DURATION_MIN_S, minimum_duration),
        )

        if position == 0:
            shot.narrative_boundary_from_previous = None
        else:
            previous_shot = normalized_shots[-1]
            shot.narrative_boundary_from_previous = (
                NarrativeBoundaryContract.model_validate({
                "boundary_id": (
                    f"NB-{previous_shot.shot_id}-{shot.shot_id}"
                ),
                "previous_shot_id": previous_shot.shot_id,
                "next_shot_id": shot.shot_id,
                "narrative_relation": "相邻镜头按事件因果与状态链继续",
                "required_state_invariants": list(
                    shot.planned_state_in_fact_ids
                ),
                "allowed_state_deltas": [],
                "state_delta_transitions": [],
                "forbidden_replay_action_ids": sorted(completed_actions),
                "handoff_action_phase_id": None,
                "spatial_orientation_contract": {},
                "temporal_orientation_contract": {},
                "audience_state_handoffs": [
                    {
                        "audience_prior_id": prior_id,
                        "previous_state_out_id": path_inputs[prior_id],
                        "next_state_in_id": path_inputs[prior_id],
                    }
                    for prior_id in sorted(paths)
                ],
                "affective_handoff": {},
                "cut_motivation": (
                    "前一镜任务完成，切换到下一事件的可感知交付"
                ),
                })
            )

        completed_phases.update(phase_ids)
        completed_actions.update(action_ids)
        normalized_shots.append(shot)
        changes.append({
            "shot_no": shot.shot_no,
            "shot_id": shot.shot_id,
            "event_id": event_id,
            "role": role,
        })

    windows = []
    for source_window in plan.readability_windows:
        window = source_window.model_copy(deep=True)
        shot_ids: list[str] = []
        for event_id in window.event_ids:
            shot_ids.extend(
                normalized_shots[position].shot_id
                for position in positions_by_event.get(event_id, [])
            )
        for delta_id in window.target_delta_ids:
            owner_position = delta_owner_position.get(delta_id)
            if owner_position is not None:
                shot_ids.append(normalized_shots[owner_position].shot_id)
        window.shot_ids = list(dict.fromkeys(shot_ids))
        linked_duration = sum(
            float(shot.duration_s or 0)
            for shot in normalized_shots
            if shot.shot_id in window.shot_ids
        )
        window.planned_available_s = min(
            linked_duration,
            max(
                float(window.scheduled_processing_s or 0),
                min(linked_duration, float(window.planned_available_s or 0)),
            ),
        )
        windows.append(window)

    for shot in normalized_shots:
        shot.readability_window_ids = [
            window.readability_window_id
            for window in windows
            if shot.shot_id in window.shot_ids
        ]

    normalized = StoryboardOutline.model_validate({
        "episode_no": outline.episode_no,
        "shots": [
            shot.model_dump(mode="json")
            for shot in normalized_shots
        ],
        "readability_windows": [
            window.model_dump(mode="json")
            for window in windows
        ],
        "cognitive_bridge_plans": [],
    })
    outline.shots = normalized.shots
    outline.readability_windows = normalized.readability_windows
    outline.cognitive_bridge_plans = []
    changes.extend(
        normalize_split_action_owner_completions(
            outline,
            screenplay,
        )
    )
    return changes
