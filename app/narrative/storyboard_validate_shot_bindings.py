"""Per-shot identity/reference and action-binding validation.

One slice of ``validate_storyboard_narrative``'s per-shot loop (see
``storyboard_validate.py``'s module docstring for the full phase map):
stable-ID/event/scene references, primary/supporting action bindings and
their phase-id deliveries (``_validate_shot_bindings``); structured
action/participant delivery contracts (``_validate_shot_participant_deliveries``);
and the per-bound-action event/phase/actor/target delivery checks that read
those deliveries back (``_validate_shot_bound_actions``). Moved verbatim out
of the pre-split single function -- only the wrapping into named phase
functions is new.
"""
from __future__ import annotations

from typing import Any

from .primitives import _norm, _require_refs
from .storyboard_validate_context import _ShotLoopContext, _ShotLoopState


def _validate_shot_bindings(
    position: int,
    shot: Any,
    errors: list[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
) -> tuple[str, int, str, list[str], str, str | None, list[str], list[str], list[str]]:
    """Validate shot_id/event/scene refs and action/phase bindings.

    Returns ``(shot_id, shot_no, label, event_ids, scene_id,
    primary_action_id, supporting, bound_action_ids, phase_ids)`` for later
    phases in this shot's validation.
    """
    shot_id, shot_no, label, event_ids, scene_id = _validate_shot_identity_refs(
        position, shot, errors, ctx, state,
    )
    primary_action_id, supporting, bound_action_ids = _validate_action_bindings(
        shot, label, errors, ctx, state,
    )
    phase_ids = _validate_phase_bindings(
        position, shot, label, bound_action_ids, supporting, errors, ctx, state,
    )
    return shot_id, shot_no, label, event_ids, scene_id, primary_action_id, supporting, bound_action_ids, phase_ids


def _validate_shot_identity_refs(
    position: int,
    shot: Any,
    errors: list[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
) -> tuple[str, int, str, list[str], str]:
    """Validate the shot's stable ID, event refs and scene ref.

    Returns ``(shot_id, shot_no, label, event_ids, scene_id)``.
    """
    shot_id = _norm(getattr(shot, "shot_id", ""))
    shot_no = int(getattr(shot, "shot_no", position + 1) or position + 1)
    label = shot_id or f"shot_no={shot_no}"
    if not shot_id:
        errors.append(f"[SHOT_ID_MISSING] {label} 缺少稳定 shot_id")
    elif shot_id in state.shot_ids:
        errors.append(f"[SHOT_ID_DUPLICATE] shot_id 重复：{shot_id}")
    else:
        state.shot_ids[shot_id] = shot

    event_ids = list(getattr(shot, "event_ids", []) or [])
    if not event_ids and _norm(getattr(shot, "story_event_id", "")):
        event_ids = [_norm(getattr(shot, "story_event_id", ""))]
    _require_refs(event_ids, ctx.index.events, errors, label)
    scene_id = _norm(getattr(shot, "scene_id", ""))
    if ctx.index.scenes or ctx.operational_scene_ids:
        if not scene_id:
            errors.append(f"[SHOT_SCENE_ID_MISSING] {label} 缺少 SceneDramaticContract 引用")
        elif scene_id not in ctx.operational_scene_ids:
            _require_refs([scene_id], ctx.index.scenes, errors, label)
    return shot_id, shot_no, label, event_ids, scene_id


def _validate_action_bindings(
    shot: Any,
    label: str,
    errors: list[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
) -> tuple[str | None, list[str], list[str]]:
    """Validate the primary/supporting action refs and primary-action ownership.

    Returns ``(primary_action_id, supporting, bound_action_ids)``.
    """
    primary_action_id = _norm(getattr(shot, "primary_action_id", None)) or None
    supporting = [
        _norm(value) for value in (getattr(shot, "supporting_action_ids", []) or [])
    ]
    if len(set(supporting)) != len(supporting) or (
        primary_action_id is not None and primary_action_id in supporting
    ):
        errors.append(f"[SHOT_ACTION_BINDING_DUPLICATE] {label} 的主/辅动作引用重复")
    bound_action_ids = [
        action_id for action_id in [primary_action_id, *supporting] if action_id
    ]
    if primary_action_id:
        _require_refs([primary_action_id], ctx.index.actions, errors, label)
        previous = state.action_owners.get(primary_action_id)
        if previous:
            errors.append(f"[ACTION_PRIMARY_OWNER_DUPLICATE] {primary_action_id} 在 {previous}/{label} 重复作为主要动作")
        state.action_owners[primary_action_id] = label
    _require_refs(supporting, ctx.index.actions, errors, label)
    return primary_action_id, supporting, bound_action_ids


def _validate_phase_bindings(
    position: int,
    shot: Any,
    label: str,
    bound_action_ids: list[str],
    supporting: list[str],
    errors: list[str],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
) -> list[str]:
    """Validate action-phase IDs, record their deliveries, and start supporting actions.

    Returns ``phase_ids``.
    """
    phase_ids = [
        _norm(value) for value in (getattr(shot, "action_phase_ids", []) or [])
    ]
    if any(not phase_id for phase_id in phase_ids) or len(set(phase_ids)) != len(phase_ids):
        errors.append(f"[SHOT_ACTION_PHASE_ID_INVALID] {label} 含空或重复动作阶段")
    _require_refs(phase_ids, ctx.phase_owner, errors, f"{label}.action_phase_ids")
    for phase_index, phase_id in enumerate(phase_ids):
        action = ctx.phase_owner.get(phase_id)
        if action and action.action_id not in bound_action_ids:
            errors.append(
                f"[SHOT_ACTION_PHASE_OWNER_MISMATCH] {label}/{phase_id} 不属于本镜绑定动作"
            )
        if action:
            state.phase_deliveries[action.action_id].append((position, phase_index, phase_id))
    for action_id in supporting:
        action = ctx.index.actions.get(action_id)
        action_phase_ids = [
            phase.phase_id
            for phase in (action.temporal_phases if action else [])
        ]
        if not action_phase_ids or action_phase_ids[0] not in phase_ids:
            continue
        previous = state.action_owners.get(action_id)
        if previous and previous != label:
            errors.append(
                f"[ACTION_PRIMARY_OWNER_DUPLICATE] {action_id} 在 "
                f"{previous}/{label} 重复开始执行"
            )
        state.action_owners[action_id] = label
    return phase_ids


def _validate_shot_participant_deliveries(
    shot: Any,
    label: str,
    bound_action_ids: list[str],
    offscreen_actors: set[str],
    offscreen_targets: set[str],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> dict[tuple[str, str], Any]:
    """Validate ``action_participant_deliveries`` against bound actions and offscreen sets.

    Returns the per-shot ``(action_id, participant_id) -> delivery`` map, reused
    by ``_validate_shot_bound_actions`` to check every offscreen participant has
    a matching structured delivery.
    """
    shot_delivery_by_key: dict[tuple[str, str], Any] = {}
    contribution_evidence_ids = {
        _norm(evidence_id)
        for evidence_id in (
            getattr(
                getattr(shot, "shot_contribution", None),
                "evidence_ids",
                [],
            )
            or []
        )
        if _norm(evidence_id)
    }
    for delivery in (
        getattr(shot, "action_participant_deliveries", []) or []
    ):
        _validate_one_participant_delivery(
            delivery, label, bound_action_ids, offscreen_actors, offscreen_targets,
            contribution_evidence_ids, shot_delivery_by_key, ctx, errors,
        )
    return shot_delivery_by_key


def _validate_one_participant_delivery(
    delivery: Any,
    label: str,
    bound_action_ids: list[str],
    offscreen_actors: set[str],
    offscreen_targets: set[str],
    contribution_evidence_ids: set[str],
    shot_delivery_by_key: dict[tuple[str, str], Any],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Validate one structured delivery's action/participant binding, then its authority/evidence."""
    delivery_key = (
        _norm(delivery.action_id),
        _norm(delivery.participant_id),
    )
    if delivery_key in shot_delivery_by_key:
        errors.append(
            f"[SHOT_ACTION_PARTICIPANT_DELIVERY_DUPLICATE] {label}/"
            f"{delivery_key[0]}/{delivery_key[1]}"
        )
        return
    shot_delivery_by_key[delivery_key] = delivery
    action = ctx.index.actions.get(delivery_key[0])
    if delivery_key[0] not in bound_action_ids or action is None:
        errors.append(
            f"[SHOT_ACTION_PARTICIPANT_DELIVERY_ACTION_INVALID] {label}/"
            f"{delivery_key[0]} 不属于本镜绑定动作"
        )
        return
    effective_actor_ids, effective_target_ids = ctx.action_relations.get(
        action.action_id,
        (list(action.actor_ids), list(action.target_ids)),
    )
    if delivery_key[1] not in {
        *effective_actor_ids,
        *effective_target_ids,
    }:
        errors.append(
            f"[SHOT_ACTION_PARTICIPANT_DELIVERY_PARTICIPANT_INVALID] "
            f"{label}/{delivery_key[0]}/{delivery_key[1]} "
            "不是该动作的 actor/target"
        )
        return
    _validate_participant_delivery_authority(
        delivery, delivery_key, action, label, offscreen_actors, offscreen_targets,
        contribution_evidence_ids, errors,
    )


def _validate_participant_delivery_authority(
    delivery: Any,
    delivery_key: tuple[str, str],
    action: Any,
    label: str,
    offscreen_actors: set[str],
    offscreen_targets: set[str],
    contribution_evidence_ids: set[str],
    errors: list[str],
) -> None:
    """Validate the delivery matches the action's authoritative copy, is offscreen, and its evidence."""
    authoritative = next(
        (
            item
            for item in action.participant_deliveries
            if (
                _norm(item.action_id),
                _norm(item.participant_id),
            ) == delivery_key
        ),
        None,
    )
    if (
        authoritative is None
        or authoritative.model_dump(mode="json")
        != delivery.model_dump(mode="json")
    ):
        errors.append(
            f"[SHOT_ACTION_PARTICIPANT_DELIVERY_AUTHORITY_MISMATCH] "
            f"{label}/{delivery_key[0]}/{delivery_key[1]}"
        )
    if delivery_key[1] not in offscreen_actors | offscreen_targets:
        errors.append(
            f"[SHOT_ACTION_PARTICIPANT_DELIVERY_NOT_OFFSCREEN] "
            f"{label}/{delivery_key[0]}/{delivery_key[1]}"
        )
    missing_evidence = {
        _norm(evidence_id)
        for evidence_id in delivery.evidence_ids
        if _norm(evidence_id)
    } - contribution_evidence_ids
    if missing_evidence:
        errors.append(
            f"[SHOT_ACTION_PARTICIPANT_DELIVERY_EVIDENCE_UNCLAIMED] "
            f"{label}/{delivery_key[0]}/{delivery_key[1]} 的证据 "
            f"{sorted(missing_evidence)} 未进入 shot_contribution"
        )


def _validate_shot_bound_actions(
    position: int,
    label: str,
    event_ids: list[str],
    bound_action_ids: list[str],
    phase_ids: list[str],
    supporting: list[str],
    visible_or_audible_entities: set[str],
    offscreen_actors: set[str],
    offscreen_targets: set[str],
    shot_delivery_by_key: dict[tuple[str, str], Any],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate offscreen actor/target validity and per-bound-action delivery."""
    _validate_offscreen_participants_bound(label, bound_action_ids, offscreen_actors, offscreen_targets, ctx, errors)
    for action_id in bound_action_ids:
        _validate_one_bound_action(
            action_id, position, label, event_ids, phase_ids, supporting,
            visible_or_audible_entities, offscreen_actors, offscreen_targets,
            shot_delivery_by_key, ctx, state, errors,
        )


def _validate_offscreen_participants_bound(
    label: str,
    bound_action_ids: list[str],
    offscreen_actors: set[str],
    offscreen_targets: set[str],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Validate every declared offscreen actor/target belongs to a bound action."""
    bound_actor_ids = {
        actor_id
        for action_id in bound_action_ids
        for actor_id in ctx.action_relations.get(action_id, ([], []))[0]
    }
    bound_target_ids = {
        target_id
        for action_id in bound_action_ids
        for target_id in ctx.action_relations.get(action_id, ([], []))[1]
    }
    invalid_offscreen_actors = offscreen_actors - bound_actor_ids
    if invalid_offscreen_actors:
        errors.append(
            f"[SHOT_OFFSCREEN_ACTOR_INVALID] {label} 画外执行者不属于本镜绑定动作："
            f"{sorted(invalid_offscreen_actors)}"
        )
    invalid_offscreen_targets = offscreen_targets - bound_target_ids
    if invalid_offscreen_targets:
        errors.append(
            f"[SHOT_OFFSCREEN_TARGET_INVALID] {label} 画外作用对象不属于本镜绑定动作："
            f"{sorted(invalid_offscreen_targets)}"
        )


def _validate_one_bound_action(
    action_id: str,
    position: int,
    label: str,
    event_ids: list[str],
    phase_ids: list[str],
    supporting: list[str],
    visible_or_audible_entities: set[str],
    offscreen_actors: set[str],
    offscreen_targets: set[str],
    shot_delivery_by_key: dict[tuple[str, str], Any],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate one bound action's event/phase ownership, then its actor/target delivery."""
    state.action_delivery_positions[action_id].append(position)
    owner_event_id = ctx.action_event_owner.get(action_id)
    if owner_event_id is None or owner_event_id not in event_ids:
        errors.append(
            f"[SHOT_ACTION_EVENT_MISMATCH] {label}/{action_id} 没有绑定该动作的权威事件"
        )
    action = ctx.index.actions.get(action_id)
    if action is None:
        return
    action_phase_set = {phase.phase_id for phase in action.temporal_phases}
    delivered_for_action = [
        phase_id for phase_id in phase_ids if phase_id in action_phase_set
    ]
    if action.temporal_phases and not delivered_for_action:
        errors.append(f"[SHOT_ACTION_PHASE_MISSING] {label}/{action_id} 没有声明本镜负责的阶段")
    if not action.temporal_phases and action_id in supporting:
        errors.append(
            f"[PHASELESS_SUPPORTING_ACTION_INVALID] {label}/{action_id} 没有可拆阶段，不得作为辅动作提前/重演"
        )
    effective_actor_ids, effective_target_ids = ctx.action_relations.get(
        action_id,
        (list(action.actor_ids), list(action.target_ids)),
    )
    missing_actors = (
        set(effective_actor_ids)
        - visible_or_audible_entities
        - offscreen_actors
    )
    if missing_actors:
        errors.append(
            f"[SHOT_ACTION_ACTOR_UNDELIVERED] {label}/{action_id} 的执行者既未可见/可听也未显式画外交付："
            f"{sorted(missing_actors)}"
        )
    missing_targets = (
        set(effective_target_ids)
        - visible_or_audible_entities
        - offscreen_targets
    )
    if missing_targets:
        errors.append(
            f"[SHOT_ACTION_TARGET_UNDELIVERED] {label}/{action_id} 的作用对象既未可见/可听"
            f"也未显式画外交付：{sorted(missing_targets)}"
        )
    _validate_offscreen_participant_deliveries(
        action_id, label, effective_actor_ids, effective_target_ids,
        offscreen_actors, offscreen_targets, shot_delivery_by_key, errors,
    )


def _validate_offscreen_participant_deliveries(
    action_id: str,
    label: str,
    effective_actor_ids: list[str],
    effective_target_ids: list[str],
    offscreen_actors: set[str],
    offscreen_targets: set[str],
    shot_delivery_by_key: dict[tuple[str, str], Any],
    errors: list[str],
) -> None:
    """Validate every offscreen actor/target of this action has a structured delivery."""
    for participant_id in sorted(
        (set(effective_actor_ids) & offscreen_actors)
        | (set(effective_target_ids) & offscreen_targets)
    ):
        if (action_id, participant_id) not in shot_delivery_by_key:
            errors.append(
                f"[SHOT_ACTION_PARTICIPANT_DELIVERY_MISSING] "
                f"{label}/{action_id}/{participant_id} 声明画外交付"
                "却没有结构化可感知证据合同"
            )
