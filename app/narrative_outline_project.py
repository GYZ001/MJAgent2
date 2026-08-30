"""Per-node shot projection (phase I) for
normalize_narrative_storyboard_outline: fill visible-identity, planned
state-fact deltas, audience-state-path handoffs, shot_contribution,
capacity_budget, duration_s and narrative_boundary_from_previous for one
(position, event_id, role, shot) node.

Split out of narrative_outline.py -- see that function's docstring. This
was the outer function's second-largest loop body (nested one level inside
"for position, (event_id, role, shot) in enumerate(nodes)"); extracted
verbatim (mechanically dedented). ``_visual_capable``/``_display_names``
calls became explicit-param calls to the top-level functions promoted in
narrative_outline_identity.py (see that file's docstring for why).

One genuine semantic note, not a behavior change: the source rebinds
``current_facts`` to a new set each iteration
(``current_facts = (current_facts - remove_ids) | add_ids``) rather than
mutating it in place. A plain local alias of ``state.current_facts`` would
only rebind the local name, silently orphaning the caller's copy -- exactly
the ``global``-across-modules trap CLAUDE.md warns about, one level down
(a rebind inside a helper losing the caller's view, not a module-global
issue). The fix is the explicit ``state.current_facts = current_facts``
write-back at the end, so the next call (next position) observes the same
value the monolithic function's next loop iteration would have.
"""
from __future__ import annotations

from collections import defaultdict
from math import ceil
from typing import Any

from app import config
from app.identity_contracts import storyboard_action_relation_ids
from app.schemas import (
    AudienceStatePathRef,
    NarrativeBoundaryContract,
    ShotCapacityBudget,
    ShotContribution,
)

from .narrative_outline_identity import _identity_is_visual_capable, _visible_display_names
from .narrative_outline_state import _OutlineProjectionState


def _project_outline_shot(
    position: int,
    event_id: str,
    role: str,
    shot: Any,
    nodes: list[tuple[str, str, Any]],
    event_order: dict[str, int],
    state: _OutlineProjectionState,
    events: dict[str, Any],
    actions: dict[str, Any],
    plan: Any,
    screenplay: Any,
    bible: Any,
    identity_contracts: dict[str, Any],
    compiler_context_identity_names: dict[str, str],
    legacy_event_relation_ids: dict[str, set[str]],
    legacy_event_text_identity_ids: dict[str, set[str]],
    key_line_meta: dict[str, tuple[str, str, str]],
    deltas_by_event: dict[str, list[str]],
    delta_paths: dict[str, tuple[str, Any, str]],
    evidence_by_event: dict[str, list[Any]],
    character_state_ids_by_event: dict[str, list[str]],
    paths: dict[str, Any],
    window_seconds_by_event: dict[str, float],
) -> None:
    """Fill one node's shot fields in place, appending it (and its change log entries) to state."""
    first_position = state.first_position
    last_position = state.last_position
    delta_owner_position = state.delta_owner_position
    task_owner_position = state.task_owner_position
    current_facts = state.current_facts
    current_audience_state = state.current_audience_state
    completed_actions = state.completed_actions
    completed_phases = state.completed_phases
    all_event_ids = state.all_event_ids
    redundant_context_ids_by_event = state.redundant_context_ids_by_event
    normalized_shots = state.normalized_shots
    changes = state.changes

    event = events[event_id]
    is_last_occurrence = position == last_position[event_id]
    is_support = role == "support"
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
    bound_actor_ids: set[str] = set()
    bound_target_ids: set[str] = set()
    bound_participant_deliveries: list[Any] = []
    for action_id in action_ids:
        action = actions.get(action_id)
        if action is None:
            continue
        actor_ids, target_ids = storyboard_action_relation_ids(
            screenplay,
            event_id,
            action,
            bible=bible,
        )
        bound_actor_ids.update(actor_ids)
        bound_target_ids.update(target_ids)
        bound_participant_deliveries.extend(
            delivery
            for delivery in action.participant_deliveries
            if (
                delivery.action_id == action_id
                and delivery.participant_id in {
                    *actor_ids,
                    *target_ids,
                }
                and delivery.is_perceivable
                and delivery.evidence_ids
            )
        )

    visible_ids = {
        entity_id
        for entity_id in event.onscreen_entity_ids
        if entity_id != "audience" and _identity_is_visual_capable(identity_contracts, entity_id)
    }
    # Old published narrative plans predate onscreen_entity_ids.  Their
    # migration is relation-based: action ownership plus exact identity
    # occurrences in visual state text.  Evidence perceivers and scene cast
    # are intentionally excluded because neither denotes shot presence.
    if not event.onscreen_entity_ids:
        relation_ids = legacy_event_relation_ids.get(event_id) or set()
        if relation_ids:
            visible_ids.update(
                identity_id
                for identity_id in relation_ids
                if _identity_is_visual_capable(identity_contracts, identity_id)
            )
        else:
            # Some historical physical actions use only pronouns in their
            # prose.  With no exact relation evidence at all, preserve the
            # already-typed actor/target ownership rather than guessing.
            for event_action_id in event.action_ids:
                event_action = actions.get(event_action_id)
                if event_action is None:
                    continue
                effective_actor_ids, effective_target_ids = (
                    storyboard_action_relation_ids(
                        screenplay,
                        event_id,
                        event_action,
                        bible=bible,
                    )
                )
                visible_ids.update(
                    identity_id
                    for identity_id in (
                        *effective_actor_ids,
                        *effective_target_ids,
                    )
                    if _identity_is_visual_capable(identity_contracts, identity_id)
                )
        if not legacy_event_text_identity_ids.get(event_id):
            # Some pre-v1.5 events describe participants only through
            # pronouns or counts ("两人"). When no exact identity surface
            # exists, retain only old roster entries that still resolve
            # exactly through the current typed registry. They form a
            # permitted candidate relation; the directing model still
            # chooses the actual visible subset.
            current_names = {
                str(name or "").strip()
                for name in (shot.characters_visible or [])
                if str(name or "").strip()
            }
            visible_ids.update(
                identity_id
                for identity_id, contract in identity_contracts.items()
                if (
                    _identity_is_visual_capable(identity_contracts, identity_id)
                    and str(contract.display_name or "").strip()
                    in current_names
                )
            )
    redundant_context_ids = redundant_context_ids_by_event[event_id]
    visible_ids.difference_update(redundant_context_ids)
    allowed_names = _visible_display_names(identity_contracts, visible_ids)
    # This is the event's permitted composition relation, not the final
    # shot cast. Historical outlines often copied a whole scene roster or
    # retained a wrong first-mentioned actor here. The directing layer
    # chooses the actual visible subset later and records the three visual
    # fields together.
    projected_names = allowed_names
    if projected_names != list(shot.characters_visible or []):
        changes.append({
            "shot_no": shot.shot_no,
            "field": "characters_visible",
            "from": list(shot.characters_visible or []),
            "to": projected_names,
            "reason": "event_onscreen_identity_authority",
        })
        shot.characters_visible = projected_names
    unexpected_visual_names = [
        contract.display_name
        for identity_id, contract in identity_contracts.items()
        if identity_id not in visible_ids
        and contract.visual_policy != "offscreen_only"
        and any(
            contract.display_name in str(value or "")
            for value in (
                shot.primary_action,
                shot.beat,
                shot.covers,
                shot.state_out,
            )
        )
    ]
    if unexpected_visual_names and (
        role == "reaction" or shot.continuity_mode == "reaction_cut"
    ):
        reaction = (
            f"{projected_names[0]}闭口呈现当前事件完成后的状态变化"
            if projected_names
            else "当前画面以原有可见状态承接下一动作"
        )
        for field_name in (
            "primary_action", "beat", "covers", "state_out",
        ):
            before = str(getattr(shot, field_name) or "")
            if before == reaction:
                continue
            setattr(shot, field_name, reaction)
            changes.append({
                "shot_no": shot.shot_no,
                "field": field_name,
                "from": before,
                "to": reaction,
                "reason": "reaction_visual_identity_authority",
            })
    if redundant_context_ids:
        redundant_names = {
            compiler_context_identity_names[identity_id]
            for identity_id in redundant_context_ids
        }
        shot.characters_visible = [
            name
            for name in shot.characters_visible
            if name not in redundant_names
        ]

    shot.event_ids = [event_id]
    shot.story_event_id = event_id
    shot.primary_action_id = primary_action_id
    shot.supporting_action_ids = supporting_action_ids
    shot.action_phase_ids = phase_ids
    shot.visible_entity_ids = sorted(visible_ids)
    contracted_offscreen_ids = {
        delivery.participant_id
        for delivery in bound_participant_deliveries
        if delivery.participant_id not in visible_ids
    }
    shot.offscreen_action_actor_ids = sorted(
        (bound_actor_ids - visible_ids) & contracted_offscreen_ids
    )
    shot.offscreen_action_target_ids = sorted(
        (bound_target_ids - visible_ids) & contracted_offscreen_ids
    )
    delivered_offscreen_ids = {
        *shot.offscreen_action_actor_ids,
        *shot.offscreen_action_target_ids,
    }
    shot.action_participant_deliveries = [
        delivery.model_copy(deep=True)
        for delivery in bound_participant_deliveries
        if delivery.participant_id in delivered_offscreen_ids
    ]

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
    evidence_ids = event_evidence_ids
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

    state.current_facts = current_facts


def _project_outline_shots(
    nodes: list[tuple[str, str, Any]],
    event_order: dict[str, int],
    state: _OutlineProjectionState,
    events: dict[str, Any],
    actions: dict[str, Any],
    plan: Any,
    screenplay: Any,
    bible: Any,
    identity_contracts: dict[str, Any],
    compiler_context_identity_names: dict[str, str],
    legacy_event_relation_ids: dict[str, set[str]],
    legacy_event_text_identity_ids: dict[str, set[str]],
    key_line_meta: dict[str, tuple[str, str, str]],
    deltas_by_event: dict[str, list[str]],
    delta_paths: dict[str, tuple[str, Any, str]],
    evidence_by_event: dict[str, list[Any]],
    character_state_ids_by_event: dict[str, list[str]],
    paths: dict[str, Any],
    window_seconds_by_event: dict[str, float],
) -> None:
    """Project every node into its final shot, in order (state carries forward across nodes)."""
    for position, (event_id, role, shot) in enumerate(nodes):
        _project_outline_shot(
            position, event_id, role, shot, nodes, event_order, state,
            events, actions, plan, screenplay, bible, identity_contracts,
            compiler_context_identity_names, legacy_event_relation_ids,
            legacy_event_text_identity_ids, key_line_meta, deltas_by_event,
            delta_paths, evidence_by_event, character_state_ids_by_event,
            paths, window_seconds_by_event,
        )
