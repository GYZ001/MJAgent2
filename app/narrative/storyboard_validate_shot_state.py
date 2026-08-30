"""Per-shot state-fact delta, cross-shot state boundary, and in-shot event
replay validation.

One slice of ``validate_storyboard_narrative``'s per-shot loop (see
``storyboard_validate.py``'s module docstring for the full phase map):
``planned_state_in/out``/delta consistency (``_validate_shot_state_
transition``) plus the state-boundary justification and structured
transition checks against the *previous* shot's exit state
(``_validate_state_boundary_from_previous``, ``_validate_boundary_
transition`` and the five basis-specific validators it dispatches to --
each ``basis_type`` is a genuinely distinct relation, not an arbitrary
split); replaying this shot's own event preconditions/effects against
``planned_state_in/out`` (``_validate_shot_event_replay``); and the minimum
action-phase seconds this shot must budget, cross-checked against
event-ordered action preconditions (``_shot_minimum_action_seconds``).
Moved verbatim out of the pre-split single function -- only the wrapping
into named phase functions is new.

``_validate_shot_state_transition`` ends by writing ``planned_out`` back to
``state.previous_state_out`` -- this is a rebinding (the next shot's exit
state), not a mutation of the object read at the top of the function, so it
must be an explicit ``state.previous_state_out = ...`` assignment rather than
an in-place update.
"""
from __future__ import annotations

from typing import Any

from .primitives import _norm, _require_refs
from .storyboard_validate_context import _ShotLoopContext, _ShotLoopState


def _validate_shot_state_transition(
    shot: Any,
    label: str,
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Validate this shot's state-fact deltas and its boundary from the previous shot.

    Returns ``(planned_in, delta_add, delta_remove, planned_out)``. Writes
    ``planned_out`` to ``state.previous_state_out`` for the next shot.
    """
    planned_in = set(getattr(shot, "planned_state_in_fact_ids", []) or [])
    delta_add = set(getattr(shot, "planned_delta_add_fact_ids", []) or [])
    delta_remove = set(getattr(shot, "planned_delta_remove_fact_ids", []) or [])
    planned_out = set(getattr(shot, "planned_state_out_fact_ids", []) or [])
    _require_refs(planned_in | delta_add | delta_remove | planned_out, ctx.index.facts, errors, label)
    if delta_add & delta_remove:
        errors.append(f"[SHOT_STATE_DELTA_CONFLICT] {label} 同时增加和移除 {sorted(delta_add & delta_remove)}")
    if delta_remove - planned_in:
        errors.append(f"[SHOT_STATE_REGRESSION] {label} 移除未在入口成立的事实 {sorted(delta_remove - planned_in)}")
    expected_out = (planned_in - delta_remove) | delta_add
    if expected_out != planned_out:
        errors.append(
            f"[SHOT_STATE_OUT_MISMATCH] {label} 的 planned_state_out 不是 "
            "planned_state_in - remove + add"
        )
    if state.previous_state_out is not None:
        _validate_state_boundary_from_previous(shot, label, planned_in, ctx, state, errors)
    state.previous_state_out = planned_out
    return planned_in, delta_add, delta_remove, planned_out


def _validate_state_boundary_from_previous(
    shot: Any,
    label: str,
    planned_in: set[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate the state-boundary justification/handoff/invariants from the previous shot."""
    boundary = getattr(shot, "narrative_boundary_from_previous", None)
    allowed = set(boundary.allowed_state_deltas) if boundary else set()
    cross_boundary_delta = state.previous_state_out.symmetric_difference(planned_in)
    transitions = list(boundary.state_delta_transitions) if boundary else []
    justified = {
        fact_id
        for transition in transitions
        for fact_id in (transition.source_fact_id, transition.target_fact_id)
        if fact_id
    }
    if boundary and allowed != justified:
        errors.append(
            f"[BOUNDARY_STATE_JUSTIFICATION_MISMATCH] {label} allowed_state_deltas "
            "必须精确等于结构化转换中的来源/目标事实"
        )
    if cross_boundary_delta != allowed:
        errors.append(
            f"[SHOT_STATE_HANDOFF_BROKEN] {label} 与上一镜状态差不等于边界可验证转换："
            f"actual={sorted(cross_boundary_delta)} allowed={sorted(allowed)}"
        )
    if not boundary:
        return
    required = set(boundary.required_state_invariants)
    if not required.issubset(state.previous_state_out & planned_in):
        errors.append(f"[BOUNDARY_STATE_INVARIANT_BROKEN] {label} 未保持边界要求的世界状态")
    transition_ids: set[str] = set()
    for transition in transitions:
        _validate_boundary_transition(
            transition, transition_ids, label, boundary, planned_in, ctx, state, errors,
        )


def _validate_boundary_transition(
    transition: Any,
    transition_ids: set[str],
    label: str,
    boundary: Any,
    planned_in: set[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate one structured state transition's ID/refs/pair/reason, then its basis relation."""
    transition_id = _norm(transition.transition_id)
    if not transition_id or transition_id in transition_ids:
        errors.append(f"[BOUNDARY_TRANSITION_ID_INVALID] {label} 的转换 ID 为空或重复")
    transition_ids.add(transition_id)
    source_id = _norm(transition.source_fact_id)
    target_id = _norm(transition.target_fact_id)
    _require_refs([source_id, target_id], ctx.index.facts, errors, transition_id or label)
    if not source_id or not target_id or source_id == target_id:
        errors.append(f"[BOUNDARY_TRANSITION_PAIR_INVALID] {transition_id or label} 必须连接两个不同的状态事实")
        return
    if source_id not in state.previous_state_out or source_id in planned_in:
        errors.append(f"[BOUNDARY_TRANSITION_SOURCE_MISMATCH] {transition_id} 来源事实不是上镜离开态")
    if target_id not in planned_in or target_id in state.previous_state_out:
        errors.append(f"[BOUNDARY_TRANSITION_TARGET_MISMATCH] {transition_id} 目标事实不是本镜入场态")
    if not _norm(transition.reason):
        errors.append(f"[BOUNDARY_TRANSITION_REASON_MISSING] {transition_id} 缺少可审计转换理由")
    source_fact = ctx.index.facts.get(source_id)
    target_fact = ctx.index.facts.get(target_id)
    if source_fact is None or target_fact is None:
        return
    same_semantic_slot = (
        source_fact.proposition_id == target_fact.proposition_id
        and source_fact.subject_id == target_fact.subject_id
        and source_fact.predicate_id == target_fact.predicate_id
    )
    _validate_boundary_transition_basis(
        transition, transition_id, boundary, source_id, target_id,
        source_fact, target_fact, same_semantic_slot, ctx, errors,
    )


def _validate_boundary_transition_basis(
    transition: Any,
    transition_id: str,
    boundary: Any,
    source_id: str,
    target_id: str,
    source_fact: Any,
    target_fact: Any,
    same_semantic_slot: bool,
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Dispatch to the validator for this transition's declared structural basis."""
    basis = transition.basis_type
    if basis == "timeline_change":
        _validate_timeline_change_basis(transition_id, boundary, source_fact, target_fact, same_semantic_slot, errors)
    elif basis == "spatial_reorientation":
        _validate_spatial_reorientation_basis(transition_id, boundary, source_id, target_id, source_fact, target_fact, same_semantic_slot, errors)
    elif basis == "viewpoint_visibility_change":
        _validate_viewpoint_visibility_basis(transition_id, boundary, source_id, target_id, source_fact, target_fact, same_semantic_slot, errors)
    elif basis == "action_phase_handoff":
        _validate_action_phase_handoff_basis(transition, transition_id, boundary, source_id, target_id, ctx, errors)
    elif basis == "other":
        if not _norm(transition.custom_basis):
            errors.append(f"[BOUNDARY_CUSTOM_BASIS_MISSING] {transition_id} 未说明开放语义关系")
        errors.append(f"[BOUNDARY_TRANSITION_NEEDS_REVIEW] {transition_id} 的未预设边界关系需要人工复核")
    else:
        errors.append(f"[BOUNDARY_TRANSITION_BASIS_INVALID] {transition_id} 的结构依据非法；未预设关系必须用 other")


def _validate_timeline_change_basis(
    transition_id: str, boundary: Any, source_fact: Any, target_fact: Any, same_semantic_slot: bool, errors: list[str],
) -> None:
    temporal = boundary.temporal_orientation_contract
    if (
        not same_semantic_slot
        or source_fact.time_scope == target_fact.time_scope
        or temporal.get("from_time_scope") != source_fact.time_scope
        or temporal.get("to_time_scope") != target_fact.time_scope
        or not _norm(temporal.get("orientation_reason"))
    ):
        errors.append(f"[BOUNDARY_TIMELINE_RELATION_INVALID] {transition_id} 未绑定真实时域变化")


def _validate_spatial_reorientation_basis(
    transition_id: str, boundary: Any, source_id: str, target_id: str,
    source_fact: Any, target_fact: Any, same_semantic_slot: bool, errors: list[str],
) -> None:
    spatial = boundary.spatial_orientation_contract
    if (
        not same_semantic_slot
        or source_fact.time_scope != target_fact.time_scope
        or source_fact.value.kind != "spatial"
        or target_fact.value.kind != "spatial"
        or source_fact.value.data == target_fact.value.data
        or spatial.get("source_fact_id") != source_id
        or spatial.get("target_fact_id") != target_id
        or not _norm(spatial.get("orientation_reason"))
    ):
        errors.append(f"[BOUNDARY_SPATIAL_RELATION_INVALID] {transition_id} 未绑定真实空间重定向")


def _validate_viewpoint_visibility_basis(
    transition_id: str, boundary: Any, source_id: str, target_id: str,
    source_fact: Any, target_fact: Any, same_semantic_slot: bool, errors: list[str],
) -> None:
    spatial = boundary.spatial_orientation_contract
    if (
        not same_semantic_slot
        or source_fact.time_scope != target_fact.time_scope
        or source_fact.value != target_fact.value
        or source_fact.visibility == target_fact.visibility
        or spatial.get("source_fact_id") != source_id
        or spatial.get("target_fact_id") != target_id
        or not _norm(spatial.get("orientation_reason"))
    ):
        errors.append(f"[BOUNDARY_VIEWPOINT_RELATION_INVALID] {transition_id} 未绑定真实视点可见性变化")


def _validate_action_phase_handoff_basis(
    transition: Any, transition_id: str, boundary: Any, source_id: str, target_id: str,
    ctx: _ShotLoopContext, errors: list[str],
) -> None:
    phase_id = _norm(transition.basis_action_phase_id)
    action = ctx.phase_owner.get(phase_id)
    action_facts: set[str] = set()
    if action:
        action_facts.update(action.precondition_fact_ids)
        action_facts.update(action.effects_add)
        action_facts.update(action.effects_remove)
    if (
        not phase_id
        or phase_id != _norm(boundary.handoff_action_phase_id)
        or action is None
        or not {source_id, target_id}.issubset(action_facts)
    ):
        errors.append(f"[BOUNDARY_ACTION_PHASE_RELATION_INVALID] {transition_id} 未绑定真实动作阶段")


def _validate_shot_event_replay(
    position: int,
    label: str,
    event_ids: list[str],
    planned_in: set[str],
    planned_out: set[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> tuple[dict[str, set[str]], set[str]]:
    """Replay this shot's events' preconditions/effects against its planned state.

    Returns ``(event_entry_states, event_effect_fact_ids)``.
    """
    running_event_state = set(planned_in)
    event_entry_states: dict[str, set[str]] = {}
    event_effect_fact_ids: set[str] = set()
    for event_id in event_ids:
        event = ctx.index.events.get(event_id)
        occurrences = state.event_occurrences.get(event_id, [])
        if event is None or not occurrences:
            continue
        starts_here = position == occurrences[0][0]
        completes_here = ctx.complete and position == occurrences[-1][0]
        if starts_here:
            event_entry_states[event_id] = set(running_event_state)
            missing = (
                set(event.precondition_fact_ids)
                - running_event_state
            )
            if missing:
                errors.append(
                    f"[SHOT_EVENT_PRECONDITION_MISSING] {label}/"
                    f"{event_id} 镜内顺序缺少前置事实 {sorted(missing)}"
                )
        if completes_here:
            event_effect_fact_ids.update(event.effects_add)
            event_effect_fact_ids.update(event.effects_remove)
            running_event_state.difference_update(event.effects_remove)
            running_event_state.update(event.effects_add)
    if ctx.complete and running_event_state != planned_out:
        errors.append(
            f"[SHOT_EVENT_EFFECT_MISSING] {label} 的镜内事件顺序重放"
            "结果不等于 planned_state_out"
        )
    return event_entry_states, event_effect_fact_ids


def _shot_minimum_action_seconds(
    label: str,
    bound_action_ids: list[str],
    phase_ids: list[str],
    planned_in: set[str],
    event_entry_states: dict[str, set[str]],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> float:
    """Sum the minimum action-phase seconds bound to this shot.

    Also cross-checks that each starting action's preconditions are satisfied
    by the event-ordered entry state (falling back to ``planned_in`` for
    actions with no owning event).
    """
    minimum_action_s = 0.0
    for action_id in bound_action_ids:
        action = ctx.index.actions.get(action_id)
        if action is None:
            continue
        action_phase_ids = [phase.phase_id for phase in action.temporal_phases]
        delivered_for_action = [
            phase_id for phase_id in phase_ids if phase_id in action_phase_ids
        ]
        starts_action = (
            not action_phase_ids or action_phase_ids[0] in delivered_for_action
        )
        owner_event_id = ctx.action_event_owner.get(action_id, "")
        action_entry_state = event_entry_states.get(
            owner_event_id,
            planned_in,
        )
        if starts_action and not set(
            action.precondition_fact_ids
        ).issubset(action_entry_state):
            errors.append(
                f"[SHOT_ACTION_PRECONDITION_MISSING] {label} 未按镜内"
                f"事件顺序满足 {action_id} 的前置事实"
            )
        minimum_action_s += sum(
            max(0.0, phase.estimated_min_s)
            for phase in action.temporal_phases
            if phase.phase_id in delivered_for_action
        )
    return minimum_action_s
