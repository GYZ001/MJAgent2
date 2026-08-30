"""Post-loop passes for ``validate_storyboard_narrative``: whole-episode checks
that need every shot's contribution already recorded in ``_ShotLoopState``.

Each function here is one independent check over the finished per-shot
ledgers (see ``storyboard_validate.py``'s module docstring for the full phase
map), called once in the original sequence -- unlike the per-shot phases,
none of these read or write each other's locals except ``first_event_position``
(built once by ``_validate_event_causal_order`` and passed to the two passes
that need it) and ``windows`` (built by ``_validate_storyboard_readability_
windows`` and passed to ``_validate_primary_window_delivery``). Moved
verbatim out of the pre-split single function -- only the wrapping into
named phase functions is new.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .primitives import _contribution_nonempty, _norm, _require_refs
from .storyboard_validate_context import _ShotLoopContext, _ShotLoopState


def _validate_event_causal_order(
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> dict[str, tuple[int, int]]:
    """Validate causal-parent ordering and must-keep delivery for every event.

    Returns ``first_event_position`` (``event_id -> earliest (shot_position,
    event_index)``), reused by the intended-ambiguity and delivery-
    completeness passes.
    """
    first_event_position = {
        event_id: min(occurrences)
        for event_id, occurrences in state.event_occurrences.items()
        if occurrences
    }
    for event_id, event in ctx.index.events.items():
        event_position = first_event_position.get(event_id)
        for parent_id in event.causal_parent_ids:
            parent_position = first_event_position.get(parent_id)
            if event_position is not None and parent_position is not None and parent_position >= event_position:
                errors.append(f"[STORYBOARD_EVENT_ORDER_INVALID] {event_id} 没有排在原因 {parent_id} 之后")
        if ctx.complete and event.delivery_policy == "deliver" and event.must_keep and event_position is None:
            errors.append(f"[MUST_KEEP_EVENT_UNDELIVERED] {event_id} 是本作用域必交付事件但未进入分镜")
    return first_event_position


def _validate_action_phase_delivery_order(
    items: list[Any],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate a multi-phase action's phases were each delivered once, in order.

    A multi-shot action is one ordered execution, not several shots that each
    restage the whole gesture. Phase identity and order are structural, so
    this remains genre- and wording-independent.
    """
    if not ctx.complete:
        return
    for action_id, action in ctx.index.actions.items():
        if action_id not in ctx.action_event_owner:
            continue
        expected_phase_ids = [phase.phase_id for phase in action.temporal_phases]
        deliveries = sorted(state.phase_deliveries.get(action_id, []))
        delivered_phase_ids = [phase_id for _, _, phase_id in deliveries]
        if expected_phase_ids:
            if delivered_phase_ids != expected_phase_ids:
                errors.append(
                    f"[ACTION_PHASE_DELIVERY_MISMATCH] {action_id} 阶段必须按定义顺序各交付一次："
                    f"expected={expected_phase_ids} actual={delivered_phase_ids}"
                )
            phase_counts: defaultdict[str, int] = defaultdict(int)
            for phase_id in delivered_phase_ids:
                phase_counts[phase_id] += 1
            duplicates = sorted(
                phase_id for phase_id, count in phase_counts.items() if count > 1
            )
            if duplicates:
                errors.append(
                    f"[ACTION_PHASE_OWNER_DUPLICATE] {action_id} 重复交付阶段 {duplicates}"
                )
            if deliveries:
                first_position = deliveries[0][0]
                first_label = _norm(getattr(items[first_position], "shot_id", "")) or (
                    f"shot_no={getattr(items[first_position], 'shot_no', first_position + 1)}"
                )
                if state.action_owners.get(action_id) != first_label:
                    errors.append(
                        f"[ACTION_PRIMARY_PHASE_OWNER_MISMATCH] {action_id} 的主要动作所有者"
                        "必须是执行首阶段的镜头"
                    )
        elif action_id not in state.action_owners:
            errors.append(f"[PHASELESS_ACTION_OWNER_MISSING] {action_id} 没有唯一主要执行镜头")
        positions = state.action_delivery_positions.get(action_id, [])
        if positions and positions != sorted(positions):
            errors.append(f"[ACTION_DELIVERY_ORDER_INVALID] {action_id} 的镜头交付顺序非单调")


def _build_ambiguity_lookups(
    items: list[Any], ctx: _ShotLoopContext,
) -> tuple[dict[str, Any], dict[str, int], dict[str, int]]:
    """Build the withheld-proposition, shot-position and first-scene-position lookups.

    Returns ``(withheld_contracts, shot_position_by_id, first_scene_position)``.
    """
    withheld_contracts = {
        withheld.proposition_id: withheld
        for intent in ctx.plan.experience_intents
        for withheld in intent.withheld_propositions
    }
    shot_position_by_id = {
        _norm(getattr(shot, "shot_id", "")): position
        for position, shot in enumerate(items)
        if _norm(getattr(shot, "shot_id", ""))
    }
    first_scene_position: dict[str, int] = {}
    for position, shot in enumerate(items):
        scene_id = _norm(getattr(shot, "scene_id", ""))
        if scene_id:
            first_scene_position.setdefault(scene_id, position)
    return withheld_contracts, shot_position_by_id, first_scene_position


def _validate_intended_ambiguity(
    items: list[Any],
    first_event_position: dict[str, tuple[int, int]],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Validate a withheld proposition is never disclosed before its declared anchor."""
    withheld_contracts, shot_position_by_id, first_scene_position = _build_ambiguity_lookups(items, ctx)
    for position, shot in enumerate(items):
        contribution = getattr(shot, "shot_contribution", None)
        if not contribution:
            continue
        for evidence_id in contribution.evidence_ids:
            evidence = ctx.index.evidence.get(evidence_id)
            if evidence is None or "audience" not in evidence.perceivable_by:
                continue
            _validate_evidence_disclosure_timing(
                shot, position, evidence, first_event_position, first_scene_position,
                shot_position_by_id, withheld_contracts, errors,
            )


def _validate_evidence_disclosure_timing(
    shot: Any,
    position: int,
    evidence: Any,
    first_event_position: dict[str, tuple[int, int]],
    first_scene_position: dict[str, int],
    shot_position_by_id: dict[str, int],
    withheld_contracts: dict[str, Any],
    errors: list[str],
) -> None:
    """Validate every proposition this evidence supports has reached its disclosure anchor."""
    for proposition_id in evidence.supports_proposition_ids:
        withheld = withheld_contracts.get(proposition_id)
        if withheld is None:
            continue
        disclosure = withheld.future_disclosure_anchor
        disclosure_reached = False
        if disclosure is not None and disclosure.type == "event":
            disclosure_position = first_event_position.get(disclosure.id)
            disclosure_reached = (
                disclosure_position is not None
                and (position, 0) >= disclosure_position
            )
        elif disclosure is not None and disclosure.type == "scene":
            disclosure_position = first_scene_position.get(disclosure.id)
            disclosure_reached = (
                disclosure_position is not None and position >= disclosure_position
            )
        elif disclosure is not None and disclosure.type == "shot":
            disclosure_position = shot_position_by_id.get(disclosure.id)
            disclosure_reached = (
                disclosure_position is not None and position >= disclosure_position
            )
        if not disclosure_reached:
            errors.append(
                f"[INTENDED_AMBIGUITY_BROKEN] shot_id={getattr(shot, 'shot_id', '')} "
                f"在约定锚点前交付了有意隐藏命题 {proposition_id}"
            )


def _validate_delivery_completeness(
    first_event_position: dict[str, tuple[int, int]],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate every target-delta/action/assimilation-task got a delivering shot before its deadline."""
    if not ctx.complete:
        return
    for delta_id in ctx.index.deltas:
        owners = state.delta_owners.get(delta_id, [])
        if len(owners) == 0:
            errors.append(f"[TARGET_DELTA_UNDELIVERED] {delta_id} 没有主要交付镜头")
        elif len(owners) > 1:
            errors.append(f"[TARGET_DELTA_OWNER_DUPLICATE] {delta_id} 在 {owners} 被重复主要交付")
        owner_positions = state.delta_owner_positions.get(delta_id, [])
        delta = ctx.index.deltas[delta_id]
        deadline_position = first_event_position.get(delta.deadline_event_id)
        if owner_positions and deadline_position is not None and (owner_positions[0], 0) > deadline_position:
            errors.append(f"[TARGET_DELTA_AFTER_DEADLINE] {delta_id} 在截止事件 {delta.deadline_event_id} 之后才交付")
    for action_id, action in ctx.index.actions.items():
        event_uses = any(action_id in event.action_ids for event in ctx.index.events.values())
        if event_uses and action_id not in state.action_owners:
            errors.append(f"[ACTION_UNFILMED] {action_id} 属于叙事事件但没有主要执行镜头")
    for task_id, task in ctx.index.tasks.items():
        owners = state.task_owners.get(task_id, [])
        if not owners:
            errors.append(f"[ASSIMILATION_TASK_UNDELIVERED] {task_id} 没有镜头证据贡献")
            continue
        if len(owners) > 1:
            errors.append(f"[ASSIMILATION_TASK_OWNER_DUPLICATE] {task_id} 被多个镜头重复主要承担")
        delta = ctx.index.deltas.get(task.target_delta_id)
        deadline_ids = list(task.downstream_dependency_event_ids)
        if delta:
            deadline_ids.append(delta.deadline_event_id)
        deadline_positions = [
            first_event_position[event_id]
            for event_id in deadline_ids
            if event_id in first_event_position
        ]
        if deadline_positions and (owners[0], 0) > min(deadline_positions):
            errors.append(f"[ASSIMILATION_TASK_AFTER_DEADLINE] {task_id} 在下游使用后才完成")


def _validate_storyboard_readability_windows(
    items: list[Any],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> list[Any]:
    """Validate readability windows' refs, capacity and shot backrefs.

    Returns ``windows``, reused by ``_validate_primary_window_delivery``.
    """
    windows = list(ctx.outline.readability_windows if ctx.outline and ctx.outline.readability_windows else ctx.plan.readability_windows)
    window_ids = {window.readability_window_id for window in windows}
    for window in windows:
        _require_refs(window.target_delta_ids, ctx.index.deltas, errors, window.readability_window_id)
        if ctx.complete:
            _require_refs(window.shot_ids, set(state.shot_ids), errors, window.readability_window_id)
            if (window.event_ids or window.target_delta_ids) and not window.shot_ids:
                errors.append(f"[READABILITY_WINDOW_UNASSIGNED] {window.readability_window_id} 没有绑定实际镜头")
        if window.planned_available_s < window.scheduled_processing_s:
            errors.append(
                f"[READABILITY_CAPACITY_EXCEEDED] {window.readability_window_id} 计划可用 "
                f"{window.planned_available_s}s，小于分配处理时间 {window.scheduled_processing_s}s"
            )
        linked_duration = sum(
            float(getattr(state.shot_ids.get(shot_id), "duration_s", 0) or 0)
            for shot_id in window.shot_ids
            if shot_id in state.shot_ids
        )
        if ctx.complete and linked_duration and window.planned_available_s > linked_duration:
            errors.append(
                f"[READABILITY_WINDOW_DURATION_EXCEEDED] {window.readability_window_id} 的有效可读时间 "
                "大于所绑定镜头总时长"
            )
        for shot_id in window.shot_ids:
            shot = state.shot_ids.get(shot_id)
            if shot and window.readability_window_id not in (
                getattr(shot, "readability_window_ids", []) or []
            ):
                errors.append(f"[READABILITY_WINDOW_BACKREF_MISSING] {shot_id} 没有回引 {window.readability_window_id}")
    for shot in items:
        for window_id in getattr(shot, "readability_window_ids", []) or []:
            if window_id not in window_ids:
                errors.append(f"[READABILITY_WINDOW_MISSING] {getattr(shot, 'shot_id', '')} 引用了不存在的 {window_id}")
    return windows


def _validate_primary_window_delivery(
    windows: list[Any],
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate must-keep events and target deltas land in their declared primary window."""
    windows_by_id = {window.readability_window_id: window for window in windows}
    if not ctx.complete:
        return
    for event_id, event in ctx.index.events.items():
        if event.delivery_policy != "deliver" or not event.must_keep:
            continue
        window = windows_by_id.get(_norm(event.primary_delivery_window_id))
        if window and not any(
            shot_id in state.shot_ids and event_id in (getattr(state.shot_ids[shot_id], "event_ids", []) or [])
            for shot_id in window.shot_ids
        ):
            errors.append(f"[EVENT_PRIMARY_WINDOW_UNDELIVERED] {event_id} 没有在其主要窗口内出现")
    for delta_id, delta in ctx.index.deltas.items():
        window = windows_by_id.get(_norm(delta.primary_delivery_window_id))
        owners = state.delta_owners.get(delta_id, [])
        if window and owners and owners[0] not in window.shot_ids:
            errors.append(f"[TARGET_PRIMARY_WINDOW_OWNER_MISMATCH] {delta_id} 的主要交付镜头不在 {window.readability_window_id}")


def _validate_cognitive_bridges(
    ctx: _ShotLoopContext,
    state: _ShotLoopState,
    errors: list[str],
) -> None:
    """Validate candidate outline cognitive-bridge plans against the deletion/marginal-gain tests."""
    bridge_ids: set[str] = set()
    for bridge in (ctx.outline.cognitive_bridge_plans if ctx.outline else []):
        bridge_id = _norm(bridge.bridge_plan_id)
        if not bridge_id or bridge_id in bridge_ids:
            errors.append(f"[COGNITIVE_BRIDGE_ID_INVALID] 认知桥 ID 为空或重复：{bridge_id or '<empty>'}")
        bridge_ids.add(bridge_id)
        _require_refs(bridge.assimilation_task_ids, ctx.index.tasks, errors, bridge_id)
        _require_refs(bridge.affected_shot_ids, set(state.shot_ids), errors, bridge_id)
        _require_refs(bridge.added_shot_ids, set(state.shot_ids), errors, bridge_id)
        if set(bridge.removed_shot_ids).intersection(state.shot_ids):
            errors.append(f"[COGNITIVE_BRIDGE_REMOVAL_STILL_PRESENT] {bridge_id} 声明删除的镜头仍在候选大纲")
        if not bridge.assimilation_task_ids:
            errors.append(f"[COGNITIVE_BRIDGE_TASK_MISSING] {bridge_id} 没有绑定需要修复的认知任务")
        if not bridge.candidate_changes or not bridge.expected_audience_delta:
            errors.append(f"[COGNITIVE_BRIDGE_HYPOTHESIS_MISSING] {bridge_id} 缺少候选改动或预期观众状态增量")
        if not _norm(bridge.selection_reason):
            errors.append(f"[COGNITIVE_BRIDGE_SELECTION_REASON_MISSING] {bridge_id} 缺少选择依据")
        deletion = bridge.deletion_test_result
        marginal = bridge.marginal_gain_result
        if deletion.get("passed") is not True or deletion.get("deletion_is_lossless") is True:
            errors.append(f"[COGNITIVE_BRIDGE_DELETION_TEST_FAILED] {bridge_id} 删除测试未证明该镜头/改动必要")
        gain = marginal.get("expected_gain")
        if (
            marginal.get("passed") is not True
            or not isinstance(gain, (int, float))
            or float(gain) <= 0
        ):
            errors.append(f"[COGNITIVE_BRIDGE_MARGINAL_GAIN_FAILED] {bridge_id} 边际增益测试未证明正向叙事收益")
        added_contributions = [
            getattr(state.shot_ids[shot_id], "shot_contribution", None)
            for shot_id in bridge.added_shot_ids
            if shot_id in state.shot_ids
        ]
        if bridge.added_shot_ids and not all(
            contribution
            and set(contribution.assimilation_task_ids).intersection(bridge.assimilation_task_ids)
            and _contribution_nonempty(contribution)
            for contribution in added_contributions
        ):
            errors.append(f"[COGNITIVE_BRIDGE_ADDED_SHOT_UNGROUNDED] {bridge_id} 新增镜头未直接承担所绑定认知任务")
