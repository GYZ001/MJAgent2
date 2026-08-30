"""Shot-numbering and per-position projection-state setup (phases F/G/H)
for normalize_narrative_storyboard_outline, run once the full node list is
known and before the per-node projection loop.

Split out of narrative_outline.py -- see that function's docstring.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.identity_contracts import storyboard_action_relation_ids


@dataclass
class _OutlineProjectionState:
    """Mutable running state threaded through the per-node projection loop
    in narrative_outline_project.py -- positions/ownership are read-only
    after this is built; current_facts/current_audience_state/
    completed_actions/completed_phases/normalized_shots/changes are updated
    by each projected node in sequence (loop-carried simulation state, same
    as the pre-split source's plain locals)."""

    positions_by_event: dict[str, list[int]]
    first_position: dict[str, int]
    last_position: dict[str, int]
    delta_owner_position: dict[str, int]
    task_owner_position: dict[int, list[str]]
    current_facts: set[str]
    current_audience_state: dict[str, str]
    completed_actions: set[str]
    completed_phases: set[str]
    all_event_ids: list[str]
    redundant_context_ids_by_event: dict[str, set[str]]
    normalized_shots: list[Any]
    changes: list[dict[str, Any]]


def _assign_outline_shot_numbers(
    nodes: list[tuple[str, str, Any]],
    preserve_shot_ids: bool,
) -> None:
    """Number each node's shot sequentially, minting shot_id unless preserving existing ids."""
    for position, (_event_id, _role, shot) in enumerate(nodes, start=1):
        shot.shot_no = position
        if not preserve_shot_ids:
            shot.shot_id = f"SH{position:03d}"


def _build_outline_projection_state(
    nodes: list[tuple[str, str, Any]],
    deltas_by_event: dict[str, list[str]],
    delta_paths: dict[str, tuple[str, Any, str]],
    paths: dict[str, Any],
    plan: Any,
    events: dict[str, Any],
    actions: dict[str, Any],
    screenplay: Any,
    bible: Any,
    compiler_context_identity_names: dict[str, str],
    action_delivery_changes: list[dict[str, Any]],
    legacy_action_relation_changes: list[dict[str, Any]],
) -> _OutlineProjectionState:
    """Index node positions/delta-ownership/task-ownership and initialize the fact/audience-state simulation."""
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
    redundant_context_ids_by_event: dict[str, set[str]] = {}
    for event_id, event in events.items():
        redundant_ids: set[str] = set()
        for action_id in event.action_ids:
            action = actions.get(action_id)
            if action is None:
                continue
            actor_ids = set(
                storyboard_action_relation_ids(
                    screenplay,
                    event_id,
                    action,
                    bible=bible,
                )[0]
            )
            if actor_ids - set(compiler_context_identity_names):
                redundant_ids.update(
                    actor_ids & set(compiler_context_identity_names)
                )
        redundant_context_ids_by_event[event_id] = redundant_ids
    normalized_shots = []
    changes: list[dict[str, Any]] = [
        *action_delivery_changes,
        *legacy_action_relation_changes,
    ]
    return _OutlineProjectionState(
        positions_by_event=positions_by_event,
        first_position=first_position,
        last_position=last_position,
        delta_owner_position=delta_owner_position,
        task_owner_position=task_owner_position,
        current_facts=current_facts,
        current_audience_state=current_audience_state,
        completed_actions=completed_actions,
        completed_phases=completed_phases,
        all_event_ids=all_event_ids,
        redundant_context_ids_by_event=redundant_context_ids_by_event,
        normalized_shots=normalized_shots,
        changes=changes,
    )
