"""Key-line-to-event resolution, audience-path/target-delta indexing and
per-event lookup-table construction for
normalize_narrative_storyboard_outline.

Split out of narrative_outline.py -- see that function's docstring.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.narrative import _target_state_fragment_matches
from app.schemas import EpisodeScreenplay


def _resolve_outline_key_event_mapping(
    screenplay: EpisodeScreenplay,
    plan: Any,
    event_order: dict[str, int],
    bases_by_event: dict[str, list[Any]],
    key_line_meta: dict[str, tuple[str, str, str]],
    chain_key_ids: dict[str, list[str]],
) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Resolve which event each key_line/dialogue_chain belongs to (from existing shot ownership, then bigram-similarity fallback).

    Returns (key_event, chain_events, key_ids_by_event).
    """
    key_event: dict[str, str] = {}
    for event_id, bases in bases_by_event.items():
        for base in bases:
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
    return key_event, chain_events, key_ids_by_event


def _build_outline_audience_path_index(
    plan: Any,
    event_order: dict[str, int],
) -> tuple[dict[str, Any], dict[str, tuple[str, Any, str]], dict[str, str]]:
    """Resolve each audience path's per-deadline-group destination state.

    Returns (paths, delta_paths, delta_destinations).
    """
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
    return paths, delta_paths, delta_destinations


def _build_outline_event_lookup_indices(
    plan: Any,
    delta_paths: dict[str, tuple[str, Any, str]],
) -> tuple[
    dict[str, list[str]], dict[str, list[Any]], dict[str, list[str]], dict[str, float],
]:
    """Index target-deltas, evidence, character-state ids and readability-window seconds by event.

    Returns (deltas_by_event, evidence_by_event, character_state_ids_by_event,
    window_seconds_by_event).
    """
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
    return (
        deltas_by_event, evidence_by_event, character_state_ids_by_event,
        window_seconds_by_event,
    )
