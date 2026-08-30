"""Action, action/event-consistency and structural-equivalence-audit
validation phases of validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.schemas import is_system_environment_entity_id

from .primitives import _norm, _require_refs


def _validate_actions(index: Any, declared_entity_ids: set[str], errors: list[str]) -> None:
    """Check each action's refs, semantics, participants, decision requirement and temporal phases."""
    for action_id, action in index.actions.items():
        _require_refs(action.precondition_fact_ids, index.facts, errors, action_id)
        _require_refs([*action.effects_add, *action.effects_remove], index.facts, errors, action_id)
        if not _norm(action.semantic_intent) or not _norm(action.completion_condition):
            errors.append(f"[ACTION_SEMANTICS_MISSING] {action_id} 缺少语义意图或可观察完成条件")
        if set(action.effects_add).intersection(action.effects_remove):
            errors.append(f"[ACTION_EFFECT_CONFLICT] {action_id} 同时增加和删除同一状态事实")
        if (
            not action.actor_ids
            and not action.target_ids
            and action.action_agency.identity_bearing
        ):
            errors.append(f"[ACTION_PARTICIPANT_MISSING] {action_id} 没有主体或作用目标")
        undeclared_participants = (
            set(action.actor_ids) | set(action.target_ids)
        ) - declared_entity_ids
        if undeclared_participants:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {action_id} 含未声明动作参与者 {sorted(undeclared_participants)}")
        environment_participants = {
            entity_id
            for entity_id in [*action.actor_ids, *action.target_ids]
            if is_system_environment_entity_id(entity_id)
        }
        if environment_participants:
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {action_id} 把系统环境实体"
                f"当作动作人物 {sorted(environment_participants)}"
            )
        if action.decision_requirement not in {"applies", "not_applicable"}:
            errors.append(f"[ACTION_DECISION_REQUIREMENT_INVALID] {action_id}.decision_requirement 非法")
        if (
            action.decision_requirement == "not_applicable"
            and not _norm(action.decision_not_applicable_reason)
        ):
            errors.append(f"[ACTION_DECISION_ALTERNATIVE_MISSING] {action_id} 不需要人物决策链时必须说明因果依据")
        phase_ids: set[str] = set()
        for phase in action.temporal_phases:
            if not _norm(phase.phase_id) or phase.phase_id in phase_ids:
                errors.append(f"[ACTION_PHASE_ID_INVALID] {action_id} 的阶段 ID 为空或重复")
            phase_ids.add(phase.phase_id)
            if phase.estimated_min_s < 0:
                errors.append(f"[ACTION_PHASE_DURATION_INVALID] {phase.phase_id}.estimated_min_s 不能为负")
            if not _norm(phase.start_condition) or not _norm(phase.end_condition):
                errors.append(f"[ACTION_PHASE_BOUNDARY_MISSING] {phase.phase_id} 缺少开始或结束条件")
        for boundary_id in action.splittable_boundaries:
            if boundary_id not in phase_ids:
                errors.append(f"[ACTION_SPLIT_BOUNDARY_MISSING] {action_id} 引用了不存在阶段 {boundary_id}")



def _validate_action_event_effect_consistency(index: Any, errors: list[str]) -> None:
    """Check every event fully carries the preconditions/effects of its bound actions."""
    for event_id, event in index.events.items():
        bound_actions = [
            index.actions[action_id]
            for action_id in event.action_ids
            if action_id in index.actions
        ]
        action_adds = {
            fact_id
            for action in bound_actions
            for fact_id in action.effects_add
        }
        action_removes = {
            fact_id
            for action in bound_actions
            for fact_id in action.effects_remove
        }
        for action_id in event.action_ids:
            action = index.actions.get(action_id)
            if action is None:
                continue
            external_preconditions = (
                set(action.precondition_fact_ids) - action_adds
            )
            net_adds = set(action.effects_add) - action_removes
            net_removes = set(action.effects_remove) - action_adds
            if not external_preconditions.issubset(event.precondition_fact_ids):
                errors.append(f"[ACTION_EVENT_PRECONDITION_MISMATCH] {event_id} 未承接 {action_id} 的全部前置事实")
            if not net_adds.issubset(event.effects_add):
                errors.append(f"[ACTION_EVENT_EFFECT_MISMATCH] {event_id} 未承接 {action_id} 的新增事实")
            if not net_removes.issubset(event.effects_remove):
                errors.append(f"[ACTION_EVENT_EFFECT_MISMATCH] {event_id} 未承接 {action_id} 的移除事实")


def _validate_action_structural_equivalence_audit(
    index: Any,
    action_event_owner: dict[str, str],
    errors: list[str],
) -> None:
    """Flag structurally-identical action pairs missing a semantic-equivalence audit, and check each audit's causal grounding."""
    structurally_equivalent_pairs: set[frozenset[str]] = set()
    action_signatures: defaultdict[tuple[Any, ...], list[str]] = defaultdict(list)
    for action_id, action in index.actions.items():
        signature = (
            tuple(sorted(action.actor_ids)),
            tuple(sorted(action.target_ids)),
            tuple(sorted(action.precondition_fact_ids)),
            tuple(sorted(action.effects_add)),
            tuple(sorted(action.effects_remove)),
            _norm(action.completion_condition),
        )
        for previous_action_id in action_signatures[signature]:
            structurally_equivalent_pairs.add(
                frozenset((previous_action_id, action_id)),
            )
        action_signatures[signature].append(action_id)

    def _event_depends_on(descendant_id: str, ancestor_id: str) -> bool:
        pending = list(index.events.get(descendant_id).causal_parent_ids) if descendant_id in index.events else []
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == ancestor_id:
                return True
            if current in visited or current not in index.events:
                continue
            visited.add(current)
            pending.extend(index.events[current].causal_parent_ids)
        return False

    audited_pairs: set[frozenset[str]] = set()
    for audit_id, audit in index.action_audits.items():
        action_pair = [_norm(value) for value in audit.action_ids]
        _require_refs(action_pair, index.actions, errors, audit_id)
        if len(action_pair) != 2 or len(set(action_pair)) != 2:
            errors.append(f"[ACTION_SEMANTIC_AUDIT_PAIR_INVALID] {audit_id} 必须比较两个不同动作")
            continue
        pair_key = frozenset(action_pair)
        if pair_key in audited_pairs:
            errors.append(f"[ACTION_SEMANTIC_AUDIT_DUPLICATE] {audit_id} 重复审计动作对 {sorted(pair_key)}")
        audited_pairs.add(pair_key)
        _require_refs(audit.added_target_delta_ids, index.deltas, errors, audit_id)
        _require_refs(audit.added_character_state_ids, index.character_states, errors, audit_id)
        _require_refs(audit.added_evidence_ids, index.evidence, errors, audit_id)
        _require_refs(audit.causal_basis_event_ids, index.events, errors, audit_id)
        if audit.decision not in {"pass", "reject", "needs_review"}:
            errors.append(f"[ACTION_SEMANTIC_AUDIT_DECISION_INVALID] {audit_id}.decision 非法")
        if not _norm(audit.reason):
            errors.append(f"[ACTION_SEMANTIC_AUDIT_REASON_MISSING] {audit_id} 缺少开放语义比较理由")
        if audit.decision != "pass":
            errors.append(f"[ACTION_SEMANTIC_AUDIT_UNRESOLVED] {audit_id} 尚未通过语义重复审计")
        if pair_key in structurally_equivalent_pairs and not audit.semantically_equivalent:
            errors.append(f"[ACTION_STRUCTURAL_EQUIVALENCE_DENIED] {audit_id} 不得否认参与者、前置、效果和完成条件均相同的动作对")
        if not audit.semantically_equivalent:
            if audit.functional_repeat is True:
                errors.append(f"[ACTION_REPEAT_RELATION_CONFLICT] {audit_id} 声明语义不等价却又标记为功能性重复")
            continue
        if audit.functional_repeat is not True:
            errors.append(f"[ACTION_REDUNDANT_REPEAT] {audit_id} 语义等价动作没有证明新的叙事功能")
            continue
        base_action_id, repeat_action_id = action_pair
        base_event_id = action_event_owner.get(base_action_id)
        repeat_event_id = action_event_owner.get(repeat_action_id)
        if (
            not base_event_id
            or not repeat_event_id
            or not _event_depends_on(repeat_event_id, base_event_id)
            or not {base_event_id, repeat_event_id}.issubset(audit.causal_basis_event_ids)
        ):
            errors.append(f"[ACTION_FUNCTIONAL_REPEAT_CAUSAL_GAP] {audit_id} 后一动作未结构化地依赖前一动作")
        repeat_event = index.events.get(repeat_event_id or "")
        repeat_propositions = set(repeat_event.proposition_ids if repeat_event else [])
        grounded_target = any(
            set(index.deltas[delta_id].proposition_ids).intersection(repeat_propositions)
            for delta_id in audit.added_target_delta_ids
            if delta_id in index.deltas
        )
        grounded_character = any(
            index.character_states[state_id].anchor.type == "event"
            and index.character_states[state_id].anchor.id == repeat_event_id
            for state_id in audit.added_character_state_ids
            if state_id in index.character_states
        )
        grounded_evidence = any(
            index.evidence[evidence_id].anchor.type == "event"
            and index.evidence[evidence_id].anchor.id == repeat_event_id
            for evidence_id in audit.added_evidence_ids
            if evidence_id in index.evidence
        )
        if not any((grounded_target, grounded_character, grounded_evidence)):
            errors.append(f"[ACTION_FUNCTIONAL_REPEAT_DELTA_MISSING] {audit_id} 没有绑定后一事件真实产生的观众、人物或证据增量")
    missing_action_audits = structurally_equivalent_pairs - audited_pairs
    for pair in sorted((sorted(value) for value in missing_action_audits)):
        errors.append(f"[ACTION_SEMANTIC_AUDIT_MISSING] 结构高度等价的不同动作 ID 必须进入 AI 语义审计：{pair}")

