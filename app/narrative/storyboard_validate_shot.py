"""Per-shot validation orchestrator for ``validate_storyboard_narrative``.

Calls each phase in the same order the pre-split single function ran them
(see ``storyboard_validate.py``'s module docstring for the full phase map
and which sibling module implements each one). Computing the
visible/offscreen entity sets is cheap and self-contained (only reads
``shot``), so it stays inline here rather than becoming its own phase
function.
"""
from __future__ import annotations

from typing import Any

from .primitives import _norm
from .storyboard_validate_context import _ShotLoopContext, _ShotLoopState
from .storyboard_validate_shot_bindings import (
    _validate_shot_bindings,
    _validate_shot_bound_actions,
    _validate_shot_participant_deliveries,
)
from .storyboard_validate_shot_boundary import (
    _finalize_shot_bookkeeping,
    _validate_shot_boundary_handoff,
    _validate_shot_completed_ledger,
)
from .storyboard_validate_shot_capacity import (
    _validate_shot_capacity_basic,
    _validate_shot_capacity_dimensions,
)
from .storyboard_validate_shot_contribution import (
    _validate_shot_audience_grounding,
    _validate_shot_audience_paths,
    _validate_shot_contribution,
)
from .storyboard_validate_shot_state import (
    _shot_minimum_action_seconds,
    _validate_shot_event_replay,
    _validate_shot_state_transition,
)


def _validate_shot(
    position: int,
    shot: Any,
    items: list[Any],
    errors: list[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
) -> None:
    """Validate one shot against the narrative graph, in original phase order."""
    (
        shot_id, shot_no, label, event_ids, scene_id,
        primary_action_id, supporting, bound_action_ids, phase_ids,
    ) = _validate_shot_bindings(position, shot, errors, ctx, state)

    visible_or_audible_entities, offscreen_actors, offscreen_targets = _compute_shot_visibility(shot)
    shot_delivery_by_key = _validate_shot_participant_deliveries(
        shot, label, bound_action_ids, offscreen_actors, offscreen_targets, ctx, errors,
    )
    _validate_shot_bound_actions(
        position, label, event_ids, bound_action_ids, phase_ids, supporting,
        visible_or_audible_entities, offscreen_actors, offscreen_targets,
        shot_delivery_by_key, ctx, state, errors,
    )

    planned_in, delta_add, delta_remove, planned_out = _validate_shot_state_transition(
        shot, label, ctx, state, errors,
    )
    event_entry_states, event_effect_fact_ids = _validate_shot_event_replay(
        position, label, event_ids, planned_in, planned_out, ctx, state, errors,
    )
    minimum_action_s = _shot_minimum_action_seconds(
        label, bound_action_ids, phase_ids, planned_in, event_entry_states, ctx, errors,
    )

    contribution = _validate_shot_contribution(
        position, label, shot_id, scene_id, event_ids, bound_action_ids,
        delta_add, delta_remove, event_effect_fact_ids, shot, ctx, state, errors,
    )
    current_paths = _validate_shot_audience_paths(shot, label, ctx, state, errors)
    boundary = _validate_shot_audience_grounding(
        shot, label, contribution, current_paths, ctx, errors,
    )

    components, duration_s, capacity_label = _validate_shot_capacity_basic(
        shot, label, shot_no, minimum_action_s, bound_action_ids, ctx, errors,
    )
    if components is not None:
        _validate_shot_capacity_dimensions(
            shot, capacity_label, duration_s, components, contribution, boundary, ctx, errors,
        )

    _validate_shot_boundary_handoff(
        position, label, shot_id, primary_action_id, boundary, items,
        current_paths, ctx, state, errors,
    )
    _validate_shot_completed_ledger(shot, label, bound_action_ids, phase_ids, ctx, state, errors)
    _finalize_shot_bookkeeping(
        shot, label, position, primary_action_id, phase_ids, bound_action_ids,
        boundary, ctx, state, errors,
    )
    state.previous_paths = current_paths


def _compute_shot_visibility(shot: Any) -> tuple[set[str], set[str], set[str]]:
    """Compute the shot's visible/audible entities and its declared offscreen actors/targets.

    Returns ``(visible_or_audible_entities, offscreen_actors, offscreen_targets)``.
    """
    visible_or_audible_entities = {
        _norm(value)
        for value in (
            *(getattr(shot, "visible_entity_ids", []) or []),
            *(getattr(shot, "characters_visible", []) or []),
            *(getattr(shot, "characters", []) or []),
            *(getattr(shot, "audio_cast", []) or []),
            *(
                getattr(dialogue, "speaker", "")
                for dialogue in (getattr(shot, "dialogues", []) or [])
            ),
        )
        if _norm(value)
    }
    offscreen_actors = {
        _norm(value)
        for value in (getattr(shot, "offscreen_action_actor_ids", []) or [])
        if _norm(value)
    }
    offscreen_targets = {
        _norm(value)
        for value in (getattr(shot, "offscreen_action_target_ids", []) or [])
        if _norm(value)
    }
    return visible_or_audible_entities, offscreen_actors, offscreen_targets
