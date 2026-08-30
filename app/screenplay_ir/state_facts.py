"""Compiler phase: compiles state facts, atomic actions, character states/beliefs and the information ledger."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.schemas import InformationItem, StoryEvent

from .identity_resolver import _IRIdentityResolver
from .models_event import IRActionPhase, ScreenplayGenerationIR
from .prompt_context import _state_fact_ids


def _ir_compile_state_facts_and_actions(
    value: ScreenplayGenerationIR,
    identity_resolver: _IRIdentityResolver,
    episode: dict[str, Any],
    episode_no: int,
    event_ids: dict[str, str],
    event_adapted_prop_id: dict[str, str],
    event_state_subject_ids: dict[str, list[str]],
    effective_render_policy: dict[str, str],
    first_adapted_prop_id: str,
    compiler_audit: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[StoryEvent],
    list[InformationItem],
    dict[str, str],
    dict[str, str],
    "defaultdict[str, list[str]]",
]:
    state_facts: list[dict[str, Any]] = []
    narrative_events: list[dict[str, Any]] = []
    atomic_actions: list[dict[str, Any]] = []
    narrative_evidence: list[dict[str, Any]] = []
    character_states: list[dict[str, Any]] = []
    character_beliefs: list[dict[str, Any]] = []
    legacy_events: list[StoryEvent] = []
    information_ledger: list[InformationItem] = []
    event_evidence_ids: dict[str, str] = {}
    event_action_ids: dict[str, str] = {}
    event_character_state_ids: defaultdict[str, list[str]] = defaultdict(list)
    initial_subjects = event_state_subject_ids[value.events[0].key]
    previous_fact_ids = _state_fact_ids(0, len(initial_subjects))
    state_facts.extend({
        "fact_id": fact_id,
        "proposition_id": first_adapted_prop_id,
        "subject_id": subject_id,
        "predicate_id": "episode_state",
        "value": {"kind": "text", "data": value.events[0].precondition_state},
        "time_scope": "main@0",
        "visibility": "visible",
        "provenance": "screenplay",
        "confidence": 1.0,
    } for fact_id, subject_id in zip(previous_fact_ids, initial_subjects))

    for position, event in enumerate(value.events, start=1):
        event_id = event_ids[event.key]
        action_id = f"A-{position}"
        evidence_id = f"EV-{position}"
        subject_ids = event_state_subject_ids[event.key]
        current_fact_ids = _state_fact_ids(position, len(subject_ids))
        pre_prop_id = (
            first_adapted_prop_id
            if position == 1
            else event_adapted_prop_id[value.events[position - 2].key]
        )
        adapted_prop_id = event_adapted_prop_id[event.key]
        actor_ids = [
            identity_resolver.id(token) for token in event.actor_keys
            if str(token).strip() != "audience"
        ]
        target_ids = [
            identity_resolver.id(token) for token in event.target_keys
            if str(token).strip() != "audience"
        ]
        onscreen_entity_ids = [
            identity_resolver.id(token) for token in event.onscreen_entity_keys
            if str(token).strip() != "audience"
        ]
        participant_delivery_rows: list[dict[str, Any]] = []
        participant_evidence_rows: list[dict[str, Any]] = []
        for delivery_position, delivery in enumerate(
            event.participant_deliveries,
            start=1,
        ):
            participant_id = identity_resolver.id(delivery.participant_key)
            participant_evidence_id = (
                f"{evidence_id}-PD{delivery_position}"
            )
            participant_delivery_rows.append({
                "action_id": action_id,
                "participant_id": participant_id,
                "evidence_ids": [participant_evidence_id],
                "audible": delivery.audible,
                "visible_effect": delivery.visible_effect,
                "visible_reaction": delivery.visible_reaction,
            })
            participant_evidence_rows.append({
                "evidence_id": participant_evidence_id,
                "anchor": {"type": "event", "id": event_id},
                "observable_claim": delivery.observable_claim,
                "perceivable_by": ["audience"],
                "supports_proposition_ids": [adapted_prop_id],
                "planned_salience": event.salience,
                "planned_duration_s": event.readability_s,
                "competing_attention_ids": [],
            })
        # State ownership follows the same complete authority relation used by
        # the event's propositions. Falling back only to the first actor (or
        # the episode's initial subject) made target/observer/speaker/scene
        # participants disappear and produced undeclared pseudo identities.
        state_facts.extend({
            "fact_id": fact_id,
            "proposition_id": adapted_prop_id,
            "subject_id": subject_id,
            "predicate_id": "episode_state",
            "value": {"kind": "text", "data": event.resulting_state},
            "time_scope": f"main@{position}",
            "visibility": "visible",
            "provenance": "screenplay",
            "confidence": 1.0,
        } for fact_id, subject_id in zip(current_fact_ids, subject_ids))

        phases = list(event.action_phases) or [
            IRActionPhase(
                start_condition=event.precondition_state,
                end_condition=event.completion_condition,
                estimated_min_s=1.0,
            )
        ]
        phase_rows = [
            {
                "phase_id": f"{action_id}/P{phase_index}",
                "start_condition": phase.start_condition,
                "end_condition": phase.end_condition,
                "estimated_min_s": phase.estimated_min_s,
            }
            for phase_index, phase in enumerate(phases, start=1)
        ]
        atomic_actions.append({
            "action_id": action_id,
            "actor_ids": actor_ids,
            "target_ids": target_ids,
            "action_agency": {
                "kind": event.action_agency.kind,
                "identity_bearing": bool(actor_ids or target_ids),
                "source_segment_ids": list(event.source_segment_ids),
            },
            "text_provenance": {
                "kind": event.text_provenance.kind,
                "identity_keys": [
                    identity_resolver.id(token)
                    for token in event.text_provenance.identity_keys
                ],
                "content_owner_keys": [
                    identity_resolver.id(token)
                    for token in event.text_provenance.content_owner_keys
                ],
                "source_segment_ids": list(
                    event.text_provenance.source_segment_ids
                ),
            },
            "dialogue_text": event.dialogue_text,
            "required_text": event.required_text,
            "prop_text": event.prop_text,
            "on_screen_text": event.on_screen_text,
            "participant_deliveries": participant_delivery_rows,
            "semantic_intent": event.action_intent,
            "precondition_fact_ids": list(previous_fact_ids),
            "effects_add": list(current_fact_ids),
            "effects_remove": list(previous_fact_ids),
            "completion_condition": event.completion_condition,
            "decision_requirement": (
                "applies" if event.decision_required and actor_ids
                else "not_applicable"
            ),
            "decision_not_applicable_reason": (
                None
                if event.decision_required and actor_ids
                else (
                    event.decision_reason
                    or "该事件由环境变化或非自主作用触发，不需要人物选择链"
                )
            ),
            "temporal_phases": phase_rows,
            "splittable_boundaries": [
                phase_rows[index]["phase_id"]
                for index, phase in enumerate(phases)
                if phase.splittable_after
            ],
        })
        event_action_ids[event.key] = action_id

        perceivable = list(dict.fromkeys([
            *(
                identity_resolver.id(token)
                for token in event.perceivable_by
                if str(token).strip() != "audience"
            ),
            *actor_ids,
            "audience",
        ]))
        narrative_evidence.append({
            "evidence_id": evidence_id,
            "anchor": {"type": "event", "id": event_id},
            "observable_claim": event.observable_claim,
            "perceivable_by": perceivable,
            "supports_proposition_ids": [adapted_prop_id],
            "planned_salience": event.salience,
            "planned_duration_s": event.readability_s,
            "competing_attention_ids": [],
        })
        narrative_evidence.extend(participant_evidence_rows)
        event_evidence_ids[event.key] = evidence_id

        parents = [
            event_ids[key] for key in event.causal_parent_keys
            if key in event_ids
        ]
        if position > 1:
            previous_event_id = event_ids[value.events[position - 2].key]
            if previous_event_id not in parents:
                parents.append(previous_event_id)
        downstream = (
            [event_ids[value.events[position].key]]
            if position < len(value.events)
            else []
        )
        narrative_events.append({
            "event_id": event_id,
            "proposition_ids": list(dict.fromkeys([
                pre_prop_id, adapted_prop_id,
            ])),
            "causal_parent_ids": parents,
            "precondition_fact_ids": list(previous_fact_ids),
            "action_ids": [action_id],
            "onscreen_entity_ids": onscreen_entity_ids,
            "effects_add": list(current_fact_ids),
            "effects_remove": list(previous_fact_ids),
            "character_goal_effects": [],
            "downstream_dependency_event_ids": downstream,
            "salience": event.salience,
            "irreversibility": event.irreversibility,
            "must_keep": event.must_keep,
            "narrative_layer": event.narrative_layer,
            "event_priority": event.event_priority,
            "render_policy": effective_render_policy[event.key],
            "delivery_scope_id": str(episode.get("id") or f"episode-{episode_no}"),
            "delivery_policy": "deliver",
            "primary_delivery_window_id": f"RW-{position}",
        })
        previous_fact_ids = current_fact_ids
        legacy_events.append(StoryEvent(
            event_id=event_id,
            source_span=",".join(event.source_segment_ids),
            source_fact=event.source_statement,
            state_in=event.precondition_state,
            trigger=event.action_intent,
            visible_change=event.observable_claim,
            state_out=event.resulting_state,
            must_keep=event.must_keep,
            narrative_layer=event.narrative_layer,
            event_priority=event.event_priority,
            render_policy=effective_render_policy[event.key],
            adaptation_addition=event.adaptation_relation == "invent",
            adaptation_reason=event.adaptation_reason,
            approved=event.adaptation_relation != "invent",
        ))

        info_values = event.information or [event.observable_claim]
        for content in info_values:
            information_ledger.append(InformationItem(
                info_id=f"I{len(information_ledger) + 1}",
                event_id=event_id,
                content=content,
                delivery_owner="visual_action",
                status="unassigned",
            ))

        for actor_position, actor_id in enumerate(actor_ids, start=1):
            state_id = f"CDS-{position}-{actor_position}"
            belief_id = f"CB-{position}-{actor_position}"
            event_character_state_ids[event.key].append(state_id)
            character_states.append({
                "character_state_id": state_id,
                "character_id": actor_id,
                "anchor": {"type": "event", "id": event_id},
                "goal_proposition_ids": [adapted_prop_id],
                "stakes_proposition_ids": [adapted_prop_id],
                "relationship_state": {},
                "emotion": {
                    "label": event.character_emotion or "受当前事件影响",
                    "intensity": max(0.1, event.salience),
                    "observable_evidence": [evidence_id],
                },
                "pressure": event.salience,
                "tactic": event.character_tactic or event.action_intent,
            })
            if event.decision_required:
                character_beliefs.append({
                    "character_belief_id": belief_id,
                    "character_id": actor_id,
                    "anchor": {"type": "event", "id": event_id},
                    "perceived_evidence_ids": [evidence_id],
                    "beliefs": [{
                        "proposition_id": adapted_prop_id,
                        "stance": "believed",
                        "confidence": max(0.6, event.salience),
                        "evidence_ids": [evidence_id],
                    }],
                    "misbelief_proposition_ids": [],
                    "decision_proposition_ids": [adapted_prop_id],
                    "decision_basis_ids": [evidence_id],
                    "decision_action_ids": [action_id],
                })
    return (
        state_facts,
        narrative_events,
        atomic_actions,
        narrative_evidence,
        character_states,
        character_beliefs,
        legacy_events,
        information_ledger,
        event_evidence_ids,
        event_action_ids,
        event_character_state_ids,
    )
