"""Per-shot cross-shot boundary-handoff, completed-action ledger and bookkeeping.

One slice of ``validate_storyboard_narrative``'s per-shot loop (see
``storyboard_validate.py``'s module docstring for the full phase map): the
full boundary contract to the *previous* shot -- adjacency, refs, forbidden
replay, cut motivation and per-prior audience handoffs
(``_validate_shot_boundary_handoff``); the completed-action/phase ledger,
which must be an exact snapshot of prior actual results, not a permissive
list (``_validate_shot_completed_ledger``); and the expected action-phase
handoff plus this shot's own contribution to that ledger for the *next*
shot (``_finalize_shot_bookkeeping``). Moved verbatim out of the pre-split
single function -- only the wrapping into named phase functions is new.

``_finalize_shot_bookkeeping`` ends by writing ``phase_ids`` to
``state.previous_shot_phase_ids`` -- a rebinding (this shot's phases become
the *next* shot's "previous"), not a mutation of what was read at the top of
the function, so it is an explicit ``state.previous_shot_phase_ids = ...``
assignment.
"""
from __future__ import annotations

from typing import Any

from .primitives import _norm, _require_refs
from .storyboard_validate_context import _ShotLoopContext, _ShotLoopState


def _validate_shot_boundary_handoff(
    position: int,
    label: str,
    shot_id: str,
    primary_action_id: str | None,
    boundary: Any,
    items: list[Any],
    current_paths: dict[str, Any],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate the narrative boundary contract from the previous shot to this one."""
    if position == 0 and boundary is not None:
        errors.append(f"[BOUNDARY_ON_FIRST_SHOT] {label} 是首镜却声明了前向边界")
    if position > 0:
        previous_shot = items[position - 1]
        previous_id = _norm(getattr(previous_shot, "shot_id", ""))
        if boundary is None:
            errors.append(f"[NARRATIVE_BOUNDARY_MISSING] {previous_id or position} -> {label} 缺少叙事边界合同")
        else:
            if boundary.previous_shot_id != previous_id or boundary.next_shot_id != shot_id:
                errors.append(f"[NARRATIVE_BOUNDARY_ID_MISMATCH] {label} 的边界没有连接实际相邻镜头")
            _require_refs(boundary.required_state_invariants, ctx.index.facts, errors, label)
            _require_refs(boundary.allowed_state_deltas, ctx.index.facts, errors, label)
            _require_refs(boundary.forbidden_replay_action_ids, ctx.index.actions, errors, label)
            if boundary.handoff_action_phase_id:
                known_phase_ids = {
                    phase.phase_id
                    for action_item in ctx.index.actions.values()
                    for phase in action_item.temporal_phases
                }
                _require_refs([boundary.handoff_action_phase_id], known_phase_ids, errors, label)
            if primary_action_id and primary_action_id in boundary.forbidden_replay_action_ids:
                errors.append(f"[FORBIDDEN_ACTION_REPLAY] {label} 重演了边界已声明完成的动作 {primary_action_id}")
            if not _norm(boundary.cut_motivation):
                errors.append(f"[CUT_MOTIVATION_MISSING] {label} 的边界没有解释为何此时切换注意")
            handoffs = {
                _norm(item.get("audience_prior_id")): item
                for item in boundary.audience_state_handoffs
                if isinstance(item, dict)
            }
            if set(handoffs) != ctx.prior_ids:
                errors.append(f"[BOUNDARY_AUDIENCE_HANDOFF_MISSING] {label} 没有逐先验状态交接")
            for prior_id, item in handoffs.items():
                previous_path = state.previous_paths.get(prior_id)
                current_path = current_paths.get(prior_id)
                if not previous_path or not current_path:
                    continue
                previous_ref = _norm(
                    item.get("previous_state_out_id")
                    or item.get("audience_state_out_id")
                )
                next_ref = _norm(
                    item.get("next_state_in_id")
                    or item.get("audience_state_in_id")
                )
                if (
                    previous_ref != previous_path.audience_state_out_target_id
                    or next_ref != current_path.audience_state_in_id
                ):
                    errors.append(f"[BOUNDARY_AUDIENCE_HANDOFF_MISMATCH] {label}/{prior_id} 与镜头状态路径不一致")


def _validate_shot_completed_ledger(
    shot: Any,
    label: str,
    bound_action_ids: list[str],
    phase_ids: list[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate the completed-action/phase ledger is an exact snapshot before this shot.

    This closes both hidden replay (omitted completed IDs) and premature
    completion (invented IDs) without classifying action text.
    """
    completed_before = {
        _norm(value)
        for value in (getattr(shot, "completed_before_action_ids", []) or [])
        if _norm(value)
    }
    completed_phases_before = {
        _norm(value)
        for value in (
            getattr(shot, "completed_before_action_phase_ids", []) or []
        )
        if _norm(value)
    }
    _require_refs(completed_before, ctx.index.actions, errors, label)
    _require_refs(completed_phases_before, ctx.phase_owner, errors, label)
    if completed_before != state.completed_actions:
        errors.append(
            f"[COMPLETED_ACTION_LEDGER_MISMATCH] {label} 完成动作账本必须等于前序实际结果："
            f"declared={sorted(completed_before)} actual={sorted(state.completed_actions)}"
        )
    if completed_phases_before != state.completed_phases:
        errors.append(
            f"[COMPLETED_PHASE_LEDGER_MISMATCH] {label} 完成阶段账本必须等于前序实际结果："
            f"declared={sorted(completed_phases_before)} actual={sorted(state.completed_phases)}"
        )
    replayed_actions = state.completed_actions.intersection(bound_action_ids)
    if replayed_actions:
        errors.append(
            f"[COMPLETED_ACTION_REPLAY] {label} 再次绑定了已完成动作 "
            f"{sorted(replayed_actions)}"
        )
    replayed_phases = state.completed_phases.intersection(phase_ids)
    if replayed_phases:
        errors.append(
            f"[COMPLETED_ACTION_PHASE_REPLAY] {label} 再次执行了已完成阶段 "
            f"{sorted(replayed_phases)}"
        )


def _finalize_shot_bookkeeping(
    shot: Any,
    label: str,
    position: int,
    primary_action_id: str | None,
    phase_ids: list[str],
    bound_action_ids: list[str],
    boundary: Any,
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate the expected action-phase handoff, then record this shot's own ledger contribution.

    Updates ``state.completed_phases``/``state.completed_actions`` in place
    and rebinds ``state.previous_shot_phase_ids`` for the next shot.
    """
    # A boundary handoff names the first phase genuinely continued from
    # the immediately preceding shot.  It must be absent for unrelated
    # cuts, so a model cannot use a decorative ID to excuse discontinuity.
    expected_handoff_phase_id: str | None = None
    if state.previous_shot_phase_ids and phase_ids:
        previous_action_ids = {
            ctx.phase_owner[phase_id].action_id
            for phase_id in state.previous_shot_phase_ids
            if phase_id in ctx.phase_owner
        }
        for phase_id in phase_ids:
            action = ctx.phase_owner.get(phase_id)
            if action and action.action_id in previous_action_ids:
                expected_handoff_phase_id = phase_id
                break
    declared_handoff = _norm(boundary.handoff_action_phase_id) if boundary else ""
    if declared_handoff != _norm(expected_handoff_phase_id):
        errors.append(
            f"[BOUNDARY_ACTION_PHASE_HANDOFF_MISMATCH] {label} 阶段交接必须精确指向相邻镜头续接阶段："
            f"declared={declared_handoff or None} expected={expected_handoff_phase_id}"
        )
    if boundary and set(boundary.forbidden_replay_action_ids) != state.completed_actions:
        errors.append(
            f"[BOUNDARY_REPLAY_LEDGER_MISMATCH] {label} 边界禁止重演集必须等于已完成动作集"
        )

    state.completed_phases.update(phase_ids)
    for action_id in bound_action_ids:
        action = ctx.index.actions.get(action_id)
        if action is None:
            continue
        required_phase_ids = {phase.phase_id for phase in action.temporal_phases}
        if (
            (not required_phase_ids and action_id == primary_action_id)
            or (required_phase_ids and required_phase_ids.issubset(state.completed_phases))
        ):
            state.completed_actions.add(action_id)
    state.previous_shot_phase_ids = phase_ids
    reserved = list(getattr(shot, "reserved_future_event_ids", []) or [])
    _require_refs(reserved, ctx.index.events, errors, label)
    for event_id in reserved:
        occurrences = state.event_occurrences.get(event_id, [])
        if any(item_position <= position for item_position, _ in occurrences):
            errors.append(f"[RESERVED_EVENT_ALREADY_DELIVERED] {label} 把已出现事件 {event_id} 声明为未来保留")
    _require_refs(getattr(shot, "readability_window_ids", []) or [], ctx.index.windows, errors, label)
