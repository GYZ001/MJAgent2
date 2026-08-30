"""Event, causal-DAG, causal-retention and state-fact-availability
validation phases of validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
event_order/parents/action_event_owner/fact_producer are built here and
returned for reuse by the many later validation phases (actions,
structural-equivalence audit, character belief/decision-chain, experience
intents, payoffs, arcs) that order themselves by event position or need to
know which event/action produced a given state fact.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.schemas import is_system_environment_entity_id

from .primitives import _cycle_nodes, _norm, _require_refs


def _validate_events(
    index: Any,
    plan: Any,
    declared_entity_ids: set[str],
    errors: list[str],
) -> tuple[dict[str, int], dict[str, list[str]], dict[str, str], dict[str, str]]:
    """Check each event's refs, onscreen-policy, scope, effect/proposition coverage and ordering; index event_order/parents/action_event_owner/fact_producer."""
    action_event_owner: dict[str, str] = {}
    fact_producer: dict[str, str] = {}
    offscreen_only_identity_ids = {
        contract.identity_id
        for contract in plan.identity_contracts
        if contract.visual_policy == "offscreen_only"
    }
    event_order = {event_id: position for position, event_id in enumerate(index.events)}
    parents: dict[str, list[str]] = {}

    for event_id, event in index.events.items():
        parents[event_id] = list(event.causal_parent_ids)
        _require_refs(event.proposition_ids, index.propositions, errors, event_id)
        _require_refs(event.causal_parent_ids, index.events, errors, event_id)
        _require_refs(event.precondition_fact_ids, index.facts, errors, event_id)
        _require_refs(event.action_ids, index.actions, errors, event_id)
        undeclared_onscreen = {
            entity_id
            for entity_id in event.onscreen_entity_ids
            if entity_id not in declared_entity_ids
        }
        if undeclared_onscreen:
            errors.append(
                f"[NARRATIVE_ENTITY_UNDECLARED] {event_id}.onscreen_entity_ids "
                f"含未声明身份 {sorted(undeclared_onscreen)}"
            )
        environment_onscreen = {
            entity_id
            for entity_id in event.onscreen_entity_ids
            if is_system_environment_entity_id(entity_id)
        }
        if environment_onscreen:
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {event_id}.onscreen_entity_ids "
                f"把系统环境实体当作可见人物 {sorted(environment_onscreen)}"
            )
        invalid_onscreen = (
            set(event.onscreen_entity_ids) & offscreen_only_identity_ids
        )
        if invalid_onscreen:
            errors.append(
                f"[EVENT_ONSCREEN_POLICY_INVALID] {event_id}.onscreen_entity_ids "
                f"含仅允许画外的身份 {sorted(invalid_onscreen)}"
            )
        _require_refs(
            event.downstream_dependency_event_ids,
            index.events,
            errors,
            f"{event_id}.downstream_dependency_event_ids",
        )
        _require_refs([*event.effects_add, *event.effects_remove], index.facts, errors, event_id)
        if event.delivery_scope_id != plan.scope_id:
            errors.append(
                f"[EVENT_SCOPE_MISMATCH] {event_id}.delivery_scope_id={event.delivery_scope_id} "
                f"不属于当前叙事作用域 {plan.scope_id}"
            )
        if set(event.effects_add).intersection(event.effects_remove):
            errors.append(f"[EVENT_EFFECT_CONFLICT] {event_id} 同时增加和删除同一状态事实")
        fact_proposition_ids = {
            index.facts[fact_id].proposition_id
            for fact_id in (
                *event.precondition_fact_ids,
                *event.effects_add,
                *event.effects_remove,
            )
            if fact_id in index.facts
        }
        if not fact_proposition_ids.issubset(event.proposition_ids):
            errors.append(
                f"[EVENT_FACT_PROPOSITION_MISMATCH] {event_id}.proposition_ids 未覆盖其前置/效果事实的命题 "
                f"{sorted(fact_proposition_ids - set(event.proposition_ids))}"
            )
        if not event.effects_add and not event.effects_remove and not event.proposition_ids:
            errors.append(f"[EVENT_NO_DELTA] {event_id} 没有事实、命题或认知变化")
        for parent_id in event.causal_parent_ids:
            if parent_id in event_order and event_order[parent_id] >= event_order[event_id]:
                errors.append(f"[EVENT_CAUSAL_ORDER] {event_id} 的原因 {parent_id} 未先于结果出现")
        for downstream_id in event.downstream_dependency_event_ids:
            if downstream_id in event_order and event_order[downstream_id] <= event_order[event_id]:
                errors.append(f"[EVENT_DOWNSTREAM_ORDER] {event_id} 的下游 {downstream_id} 没有位于其后")
        for action_id in event.action_ids:
            previous = action_event_owner.get(action_id)
            if previous and previous != event_id:
                errors.append(f"[ACTION_EVENT_OWNER_DUPLICATE] {action_id} 同时被 {previous}/{event_id} 作为事件主动作")
            action_event_owner[action_id] = event_id
        for fact_id in event.effects_add:
            previous = fact_producer.get(fact_id)
            if previous and previous != event_id:
                errors.append(f"[FACT_PRODUCER_DUPLICATE] {fact_id} 被 {previous}/{event_id} 重复创建")
            fact_producer[fact_id] = event_id
        if event.delivery_policy not in {"deliver", "withhold", "carry"}:
            errors.append(f"[EVENT_DELIVERY_POLICY_INVALID] {event_id}.delivery_policy 非法")
        if event.delivery_policy == "deliver" and event.must_keep:
            window_id = _norm(event.primary_delivery_window_id)
            window = index.windows.get(window_id)
            if not window_id:
                errors.append(f"[EVENT_PRIMARY_WINDOW_MISSING] {event_id} 是本作用域必交付事件但没有主要窗口")
            elif window is None:
                errors.append(f"[NARRATIVE_REF_MISSING] {event_id}.primary_delivery_window_id 引用了不存在的 {window_id}")
            elif event_id not in window.event_ids:
                errors.append(f"[EVENT_PRIMARY_WINDOW_MISMATCH] {window_id} 没有声明主要交付事件 {event_id}")
        if not 0 <= event.salience <= 1 or not 0 <= event.irreversibility <= 1:
            errors.append(f"[EVENT_IMPORTANCE_RANGE] {event_id}.salience/irreversibility 必须在 0..1")

    return event_order, parents, action_event_owner, fact_producer


def _validate_event_dag_acyclic(parents: dict[str, list[str]], errors: list[str]) -> None:
    """Check the event causal-parent graph has no cycle."""
    cycle = _cycle_nodes(parents)
    if cycle:
        errors.append("[EVENT_DAG_CYCLE] 事件因果图存在环：" + " -> ".join(cycle))


def _validate_causal_event_retention(
    index: Any,
    event_order: dict[str, int],
    fact_producer: dict[str, str],
    errors: list[str],
) -> None:
    """Check causally-required/preserved events keep must_keep, and no event depends on a not-yet-produced fact."""
    causal_parent_ids = {
        parent_id for event in index.events.values() for parent_id in event.causal_parent_ids
    }
    consumed_fact_ids = {
        fact_id for event in index.events.values() for fact_id in event.precondition_fact_ids
    }
    decisions_by_event: defaultdict[str, list[Any]] = defaultdict(list)
    for decision in index.decisions.values():
        for event_id in decision.affected_event_ids:
            decisions_by_event[event_id].append(decision)
    for event_id, event in index.events.items():
        causally_required = bool(
            event.downstream_dependency_event_ids
            or event_id in causal_parent_ids
            or set(event.effects_add).intersection(consumed_fact_ids)
        )
        if causally_required and not event.must_keep:
            errors.append(
                f"[CAUSAL_EVENT_MUST_KEEP_DOWNGRADED] {event_id} 仍是后续事件的因果前置，"
                "不得在未重写因果图前改为 must_keep=false"
            )
        if not event.must_keep:
            preserve_decisions = [
                decision for decision in decisions_by_event.get(event_id, [])
                if decision.relation in {"preserve", "split"}
            ]
            if preserve_decisions:
                errors.append(
                    f"[PRESERVED_EVENT_MUST_KEEP_DOWNGRADED] {event_id} 由保留/拆分决策产生，"
                    "不得不经新的省略/变换决策直接改为 must_keep=false"
                )
    for event_id, event in index.events.items():
        for fact_id in event.precondition_fact_ids:
            producer = fact_producer.get(fact_id)
            if producer and event_order.get(producer, -1) >= event_order[event_id]:
                errors.append(f"[EVENT_PRECONDITION_FROM_FUTURE] {event_id} 依赖由未来事件 {producer} 才产生的 {fact_id}")


def _validate_state_fact_availability(
    plan: Any,
    index: Any,
    fact_producer: dict[str, str],
    errors: list[str],
) -> None:
    """Check every state fact is either an audited initial fact or has a unique producer, then simulate the fact timeline."""
    initial_facts = set(plan.initial_state_fact_ids)
    produced_facts = {
        *fact_producer,
        *(
            fact_id
            for action in index.actions.values()
            for fact_id in action.effects_add
        ),
    }
    if initial_facts & produced_facts:
        errors.append(
            f"[INITIAL_FACT_HAS_PRODUCER] 初始事实不得同时由本作用域事件产生："
            f"{sorted(initial_facts & produced_facts)}"
        )
    unintroduced_facts = set(index.facts) - initial_facts - produced_facts
    if unintroduced_facts:
        errors.append(
            f"[STATE_FACT_INTRODUCTION_MISSING] 以下事实既非显式初始态也无唯一产生事件："
            f"{sorted(unintroduced_facts)}"
        )

    # Simulate only from the explicitly audited initial facts.  Every later
    # transition must consume an available precondition and cannot silently
    # recreate/remove a fact.
    active_facts = set(initial_facts)
    for event_id, event in index.events.items():
        missing_preconditions = set(event.precondition_fact_ids) - active_facts
        if missing_preconditions:
            errors.append(
                f"[EVENT_PRECONDITION_UNAVAILABLE] {event_id} 的前置事实尚未成立："
                f"{sorted(missing_preconditions)}"
            )
        missing_removals = set(event.effects_remove) - active_facts
        if missing_removals:
            errors.append(
                f"[STATE_REGRESSION] {event_id} 试图移除当前并未成立的事实："
                f"{sorted(missing_removals)}"
            )
        repeated_adds = set(event.effects_add) & active_facts
        if repeated_adds:
            errors.append(
                f"[STATE_REPLAY_WITHOUT_DELTA] {event_id} 再次建立已成立事实："
                f"{sorted(repeated_adds)}"
            )
        active_facts.difference_update(event.effects_remove)
        active_facts.update(event.effects_add)

