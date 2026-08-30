"""Critical-proposition-intent, assimilation-task, readability-window,
target-delta-window and setup/payoff validation phases of
validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .primitives import _norm, _require_refs


def _validate_critical_proposition_intent_coverage(index: Any, errors: list[str]) -> None:
    """Flag propositions from causally-critical events with no experience-intent or withheld-proposition contract."""
    child_ids = {
        parent_id for event in index.events.values() for parent_id in event.causal_parent_ids
    }
    critical_events = {
        event_id for event_id, event in index.events.items()
        if event.downstream_dependency_event_ids or event_id in child_ids
    }
    intended_propositions = {
        proposition_id
        for intent in index.intents.values()
        for path in intent.audience_paths
        for delta in path.target_deltas
        for proposition_id in delta.proposition_ids
    }
    withheld_propositions = {
        withheld.proposition_id
        for intent in index.intents.values()
        for withheld in intent.withheld_propositions
    }

    for event_id in sorted(critical_events):
        for proposition_id in index.events[event_id].proposition_ids:
            if proposition_id not in intended_propositions | withheld_propositions:
                errors.append(
                    f"[CRITICAL_PROPOSITION_INTENT_MISSING] {event_id}/{proposition_id} "
                    "被后续剧情依赖却没有逐先验体验意图或有意隐藏合同"
                )


def _validate_assimilation_tasks(index: Any, errors: list[str]) -> None:
    """Check each assimilation task's refs, target/path/intent binding and status."""
    for task_id, task in index.tasks.items():
        _require_refs([task.experience_intent_id], index.intents, errors, task_id)
        _require_refs([task.audience_path_id], index.paths, errors, task_id)
        _require_refs([task.target_delta_id], index.deltas, errors, task_id)
        _require_refs(task.required_prior_proposition_ids, index.propositions, errors, task_id)
        _require_refs(task.downstream_dependency_event_ids, index.events, errors, task_id)
        path = index.paths.get(task.audience_path_id)
        if path and task.target_delta_id not in {d.target_delta_id for d in path.target_deltas}:
            errors.append(f"[ASSIMILATION_TARGET_MISMATCH] {task_id} 的 target_delta 不属于其 audience_path")
        intent = index.intents.get(task.experience_intent_id)
        if intent and task.audience_path_id not in {item.audience_path_id for item in intent.audience_paths}:
            errors.append(f"[ASSIMILATION_INTENT_PATH_MISMATCH] {task_id} 的 audience_path 不属于其 ExperienceIntent")
        if not _norm(task.satisfaction_criteria):
            errors.append(f"[ASSIMILATION_CRITERIA_MISSING] {task_id} 缺少可由盲审验证的标准")
        if task.status not in {"open", "planned", "satisfied", "needs_review"}:
            errors.append(f"[ASSIMILATION_STATUS_INVALID] {task_id}.status 非法")
        if task.status == "needs_review":
            errors.append(f"[ASSIMILATION_NEEDS_REVIEW] {task_id} 仍不确定，不能标记叙事就绪")



def _validate_readability_windows(index: Any, errors: list[str]) -> None:
    """Check each readability window's refs, timing validity and processing-budget adequacy."""
    delta_prior = {
        delta.target_delta_id: path.audience_prior_id
        for intent in index.intents.values()
        for path in intent.audience_paths
        for delta in path.target_deltas
    }
    for window_id, window in index.windows.items():
        _require_refs(window.event_ids, index.events, errors, window_id)
        _require_refs(window.proposition_ids, index.propositions, errors, window_id)
        _require_refs(window.target_delta_ids, index.deltas, errors, window_id)
        _require_refs(window.evidence_ids, index.evidence, errors, window_id)
        if window.scheduled_processing_s < 0 or window.planned_available_s < 0:
            errors.append(f"[READABILITY_TIME_INVALID] {window_id} 的处理时间不能为负")
        if window.status == "satisfied" and window.planned_available_s < window.scheduled_processing_s:
            errors.append(f"[READABILITY_CAPACITY_EXCEEDED] {window_id} 可用时间不足却标记 satisfied")
        if window.status not in {"planned", "satisfied", "needs_replan"}:
            errors.append(f"[READABILITY_STATUS_INVALID] {window_id}.status 非法")
        if not _norm(window.readability_reason):
            errors.append(f"[READABILITY_REASON_MISSING] {window_id} 没有说明为何需要独立注意窗口")
        required_by_prior: defaultdict[str, float] = defaultdict(float)
        for delta_id in window.target_delta_ids:
            delta = index.deltas.get(delta_id)
            if delta:
                required_by_prior[delta_prior.get(delta_id, "unknown")] += max(
                    0.0, delta.required_processing_s,
                )
        required_processing = max(required_by_prior.values(), default=0.0)
        if window.scheduled_processing_s < required_processing:
            errors.append(
                f"[READABILITY_SCHEDULE_UNDERALLOCATED] {window_id} 分配 "
                f"{window.scheduled_processing_s}s，小于低分位路径所需 {required_processing}s"
            )



def _validate_target_delta_primary_windows(index: Any, errors: list[str]) -> None:
    """Check each target delta has a unique, valid, mutually-consistent primary delivery window."""
    for delta_id, delta in index.deltas.items():
        window_id = _norm(delta.primary_delivery_window_id)
        window = index.windows.get(window_id)
        if not window_id:
            errors.append(f"[TARGET_PRIMARY_WINDOW_MISSING] {delta_id} 没有唯一主要交付窗口")
        elif window is None:
            errors.append(f"[NARRATIVE_REF_MISSING] {delta_id}.primary_delivery_window_id 引用了不存在的 {window_id}")
        elif delta_id not in window.target_delta_ids:
            errors.append(f"[TARGET_PRIMARY_WINDOW_MISMATCH] {window_id} 没有声明目标变化 {delta_id}")


def _validate_setup_payoff_contracts(
    index: Any,
    prior_ids: set[str],
    event_order: dict[str, int],
    errors: list[str],
) -> None:
    """Check each setup/payoff contract's refs, ordering, status and per-prior recall-task coverage."""
    for payoff_id, payoff in index.payoffs.items():
        _require_refs([*payoff.setup_proposition_ids, *payoff.intended_inference_ids], index.propositions, errors, payoff_id)
        _require_refs([*payoff.setup_event_ids, *payoff.payoff_event_ids], index.events, errors, payoff_id)
        _require_refs([payoff.retention_deadline_event_id], index.events, errors, f"{payoff_id}.retention_deadline_event_id")
        if payoff.status == "paid_off" and not payoff.payoff_event_ids:
            errors.append(f"[PAYOFF_EVENT_MISSING] {payoff_id} 已兑现但没有兑现事件")
        if payoff.status not in {"open", "preserved", "paid_off", "intentionally_carried"}:
            errors.append(f"[PAYOFF_STATUS_INVALID] {payoff_id}.status 非法")
        if not 0 <= payoff.minimum_retention_confidence <= 1:
            errors.append(f"[PAYOFF_RETENTION_RANGE] {payoff_id}.minimum_retention_confidence 必须在 0..1")
        setup_positions = [event_order[item] for item in payoff.setup_event_ids if item in event_order]
        payoff_positions = [event_order[item] for item in payoff.payoff_event_ids if item in event_order]
        if setup_positions and payoff_positions and max(setup_positions) >= min(payoff_positions):
            errors.append(f"[SETUP_PAYOFF_ORDER_INVALID] {payoff_id} 的铺垫没有先于兑现")
        deadline_position = event_order.get(payoff.retention_deadline_event_id)
        if deadline_position is not None and setup_positions and deadline_position < max(setup_positions):
            errors.append(f"[SETUP_RETENTION_DEADLINE_INVALID] {payoff_id} 的记忆截止点早于铺垫")
        low_memory_by_prior: dict[str, set[str]] = {}
        if deadline_position is not None:
            for prior_id in prior_ids:
                eligible_states = [
                    (event_order[state.anchor.id], state_position, state)
                    for state_position, state in enumerate(index.audience_states.values())
                    if state.audience_prior_id == prior_id
                    and state.anchor.type == "event"
                    and state.anchor.id in event_order
                    and event_order[state.anchor.id] <= deadline_position
                ]
                latest_state = max(eligible_states, default=None)
                memory = {
                    _norm(item.get("proposition_id")): float(item.get("retention_confidence"))
                    for state in ([latest_state[2]] if latest_state else [])
                    for item in state.working_memory
                    if isinstance(item, dict)
                    and _norm(item.get("proposition_id"))
                    and isinstance(item.get("retention_confidence"), (int, float))
                }
                low = {
                    proposition_id
                    for proposition_id in payoff.setup_proposition_ids
                    if memory.get(proposition_id, 0.0) < payoff.minimum_retention_confidence
                }
                if low:
                    low_memory_by_prior[prior_id] = low
        recall_required = bool(low_memory_by_prior)
        if payoff.recall_needed is None:
            errors.append(f"[SETUP_RECALL_DECISION_MISSING] {payoff_id}.recall_needed 必须由逐先验工作记忆推导")
        elif payoff.recall_needed != recall_required:
            errors.append(
                f"[SETUP_RECALL_DECISION_MISMATCH] {payoff_id}.recall_needed={payoff.recall_needed} "
                f"与低分位记忆结果 {recall_required} 不一致"
            )
        if recall_required:
            for prior_id, low_propositions in low_memory_by_prior.items():
                matching_tasks = [
                    task
                    for task in index.tasks.values()
                    if index.paths.get(task.audience_path_id)
                    and index.paths[task.audience_path_id].audience_prior_id == prior_id
                    and low_propositions.issubset(task.required_prior_proposition_ids)
                    and (
                        payoff.retention_deadline_event_id in task.downstream_dependency_event_ids
                        or set(payoff.payoff_event_ids).intersection(task.downstream_dependency_event_ids)
                    )
                ]
                if not matching_tasks:
                    errors.append(
                        f"[SETUP_RECALL_TASK_MISSING] {payoff_id}/{prior_id} 在使用前已遗忘 "
                        f"{sorted(low_propositions)}，但没有逐路径认知唤回任务"
                    )

