"""Per-pass read-only lookups and cross-shot accumulators for
``validate_storyboard_narrative``'s per-shot loop (see ``storyboard_validate.py``'s
module docstring for the full phase map this package's files implement).

``_ShotLoopContext`` groups values computed once before the loop and never
mutated during it. ``_ShotLoopState`` groups the accumulators every shot
iteration reads and/or updates -- keeping the two separate makes the
read-only/mutable distinction visible at every phase-function call site
instead of buried in one shared blob. Moved verbatim (as expressions) out of
the pre-split ``validate_storyboard_narrative``'s setup code; only the
wrapping into a builder function and two dataclasses is new.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.schemas import EpisodeScreenplay, NarrativeContinuityPlan, StoryboardOutline

from .plan_index import NarrativeIndex
from .primitives import _norm


@dataclass
class _ShotLoopContext:
    """Read-only lookups built once from the narrative plan/index/screenplay."""

    index: NarrativeIndex
    plan: NarrativeContinuityPlan
    screenplay: EpisodeScreenplay
    outline: StoryboardOutline | None
    complete: bool
    operational_scene_ids: set[str]
    phase_owner: dict[str, Any]
    action_event_owner: dict[str, str]
    action_relations: dict[str, tuple[list[str], list[str]]]
    delta_paths: dict[str, tuple[str, Any, str]]
    prior_ids: set[str]


@dataclass
class _ShotLoopState:
    """Cross-iteration accumulators mutated as shots are validated in order."""

    shot_ids: dict[str, Any] = field(default_factory=dict)
    action_owners: dict[str, str] = field(default_factory=dict)
    delta_owners: defaultdict = field(default_factory=lambda: defaultdict(list))
    delta_owner_positions: defaultdict = field(default_factory=lambda: defaultdict(list))
    task_owners: defaultdict = field(default_factory=lambda: defaultdict(list))
    event_occurrences: defaultdict = field(default_factory=lambda: defaultdict(list))
    contribution_ids: set[str] = field(default_factory=set)
    phase_deliveries: defaultdict = field(default_factory=lambda: defaultdict(list))
    action_delivery_positions: defaultdict = field(default_factory=lambda: defaultdict(list))
    contribution_character_owners: dict[str, str] = field(default_factory=dict)
    contribution_audience_owners: dict[str, str] = field(default_factory=dict)
    previous_paths: dict[str, Any] = field(default_factory=dict)
    previous_state_out: set[str] | None = None
    completed_actions: set[str] = field(default_factory=set)
    completed_phases: set[str] = field(default_factory=set)
    previous_shot_phase_ids: list[str] = field(default_factory=list)


def _build_loop_context(
    screenplay: EpisodeScreenplay,
    plan: NarrativeContinuityPlan,
    index: NarrativeIndex,
    outline: StoryboardOutline | None,
    complete: bool,
    items: list[Any],
) -> tuple[_ShotLoopContext, _ShotLoopState]:
    """Build the read-only per-pass context and a fresh mutable loop state."""
    state = _ShotLoopState()
    _precompute_event_occurrences(items, index, state)
    phase_owner, action_event_owner, action_relations, delta_paths = _build_loop_lookups(screenplay, plan, index)
    ctx = _ShotLoopContext(
        index=index,
        plan=plan,
        screenplay=screenplay,
        outline=outline,
        complete=complete,
        operational_scene_ids=_compute_operational_scene_ids(screenplay, outline),
        phase_owner=phase_owner,
        action_event_owner=action_event_owner,
        action_relations=action_relations,
        delta_paths=delta_paths,
        prior_ids=set(index.priors),
    )
    return ctx, state


def _compute_operational_scene_ids(
    screenplay: EpisodeScreenplay, outline: StoryboardOutline | None,
) -> set[str]:
    """Compute the SC-numbered scene IDs plus any outline-declared scene contexts."""
    operational_scene_ids = {
        f"SC{int(scene.scene_no):02d}"
        for scene in screenplay.scene_outline
        if int(scene.scene_no or 0) > 0
    }
    if outline is not None:
        operational_scene_ids.update(
            _norm(context.scene_id)
            for context in outline.scene_contexts
            if _norm(context.scene_id)
        )
    return operational_scene_ids


def _precompute_event_occurrences(items: list[Any], index: NarrativeIndex, state: _ShotLoopState) -> None:
    """Record each event's ``(shot_position, event_index)`` occurrences across all shots."""
    for item_position, item in enumerate(items):
        item_event_ids = list(getattr(item, "event_ids", []) or [])
        if not item_event_ids and _norm(getattr(item, "story_event_id", "")):
            item_event_ids = [_norm(getattr(item, "story_event_id", ""))]
        for event_index, event_id in enumerate(item_event_ids):
            if event_id in index.events:
                state.event_occurrences[event_id].append((item_position, event_index))


def _build_loop_lookups(
    screenplay: EpisodeScreenplay, plan: NarrativeContinuityPlan, index: NarrativeIndex,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, Any]]:
    """Build the phase-owner/action-event-owner/action-relations/delta-paths lookups.

    Returns ``(phase_owner, action_event_owner, action_relations, delta_paths)``.
    """
    phase_owner = {
        phase.phase_id: action
        for action in index.actions.values()
        for phase in action.temporal_phases
    }
    action_event_owner = {
        action_id: event_id
        for event_id, event in index.events.items()
        for action_id in event.action_ids
    }
    from app.identity_contracts import storyboard_action_relation_ids

    action_relations = {
        action_id: storyboard_action_relation_ids(
            screenplay,
            action_event_owner.get(action_id, ""),
            action,
        )
        for action_id, action in index.actions.items()
    }
    delta_paths = {
        delta.target_delta_id: (
            path.audience_prior_id,
            delta,
            path.audience_state_out_target_id,
        )
        for intent in plan.experience_intents
        for path in intent.audience_paths
        for delta in path.target_deltas
    }
    return phase_owner, action_event_owner, action_relations, delta_paths
