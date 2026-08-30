"""Character-belief, character-dramatic-state and character-decision-chain
validation phases of validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.schemas import is_system_environment_entity_id

from .primitives import _anchor_ref_errors, _norm, _require_refs


def _validate_character_beliefs(
    index: Any,
    declared_entity_ids: set[str],
    event_order: dict[str, int],
    errors: list[str],
) -> None:
    """Check each character-belief snapshot's anchor, perception grounding and decision-binding completeness."""
    for belief_id, snapshot in index.character_beliefs.items():
        if snapshot.character_id not in declared_entity_ids:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {belief_id}.character_id={snapshot.character_id} 未声明")
        if is_system_environment_entity_id(snapshot.character_id):
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {belief_id}.character_id "
                "不得使用系统环境实体"
            )
        _anchor_ref_errors(
            snapshot.anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{belief_id}.anchor",
        )
        _require_refs(snapshot.perceived_evidence_ids, index.evidence, errors, belief_id)
        perceived = set(snapshot.perceived_evidence_ids)
        for evidence_id in perceived:
            evidence = index.evidence.get(evidence_id)
            if evidence and snapshot.character_id not in evidence.perceivable_by:
                errors.append(
                    f"[CHARACTER_EVIDENCE_NOT_PERCEIVABLE] {belief_id} 让 {snapshot.character_id} "
                    f"依据其不可感知的 {evidence_id} 更新信念"
                )
        for belief in snapshot.beliefs:
            _require_refs([belief.proposition_id], index.propositions, errors, belief_id)
            _require_refs(belief.evidence_ids, index.evidence, errors, belief_id)
            if belief.stance not in {"believed", "suspected", "rejected", "unknown"}:
                errors.append(f"[CHARACTER_BELIEF_STANCE_INVALID] {belief_id}/{belief.proposition_id} stance 非法")
            if not 0 <= belief.confidence <= 1:
                errors.append(f"[CONFIDENCE_RANGE] {belief_id}/{belief.proposition_id}.confidence 必须在 0..1")
            if belief.stance != "unknown" and not set(belief.evidence_ids).issubset(perceived):
                errors.append(f"[CHARACTER_BELIEF_WITHOUT_EVIDENCE] {belief_id} 的已知信念没有进入感知证据集合")
        _require_refs(snapshot.misbelief_proposition_ids, index.propositions, errors, f"{belief_id}.misbelief_proposition_ids")
        _require_refs(snapshot.decision_proposition_ids, index.propositions, errors, f"{belief_id}.decision_proposition_ids")
        _require_refs(snapshot.decision_action_ids, index.actions, errors, f"{belief_id}.decision_action_ids")
        allowed_basis = set(index.propositions) | set(index.evidence)
        _require_refs(snapshot.decision_basis_ids, allowed_basis, errors, f"{belief_id}.decision_basis_ids")
        if any((snapshot.decision_proposition_ids, snapshot.decision_basis_ids, snapshot.decision_action_ids)) and not all((
            snapshot.decision_proposition_ids,
            snapshot.decision_basis_ids,
            snapshot.decision_action_ids,
        )):
            errors.append(f"[CHARACTER_DECISION_BINDING_INCOMPLETE] {belief_id} 的决策必须同时绑定动作、决策命题和已获得依据")
        if snapshot.decision_action_ids and snapshot.anchor.type != "event":
            errors.append(
                f"[CHARACTER_DECISION_ANCHOR_UNORDERED] {belief_id} 授权动作却使用不可在剧本事件图排序的 "
                f"{snapshot.anchor.type} 锚点"
            )
        held_propositions = {
            belief.proposition_id for belief in snapshot.beliefs if belief.stance != "unknown"
        }
        for basis_id in snapshot.decision_basis_ids:
            if basis_id in index.evidence and basis_id not in perceived:
                errors.append(f"[CHARACTER_DECISION_UNPERCEIVED_BASIS] {belief_id} 的决定依据 {basis_id} 未被角色感知")
            if basis_id in index.propositions and basis_id not in held_propositions:
                errors.append(f"[CHARACTER_DECISION_UNHELD_BASIS] {belief_id} 的决定依据 {basis_id} 未形成角色信念")
        if snapshot.anchor.type == "event" and snapshot.anchor.id in event_order:
            belief_position = event_order[snapshot.anchor.id]
            for evidence_id in perceived:
                evidence = index.evidence.get(evidence_id)
                if (
                    evidence
                    and evidence.anchor.type == "event"
                    and evidence.anchor.id in event_order
                    and event_order[evidence.anchor.id] > belief_position
                ):
                    errors.append(
                        f"[CHARACTER_EVIDENCE_FROM_FUTURE] {belief_id} 依据未来事件证据 {evidence_id}"
                    )



def _validate_character_dramatic_states(
    index: Any,
    declared_entity_ids: set[str],
    errors: list[str],
) -> None:
    """Check each character dramatic-state's declaration, anchor and non-emptiness."""
    for state_id, state in index.character_states.items():
        if state.character_id not in declared_entity_ids:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {state_id}.character_id={state.character_id} 未声明")
        if is_system_environment_entity_id(state.character_id):
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {state_id}.character_id "
                "不得使用系统环境实体"
            )
        _anchor_ref_errors(
            state.anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{state_id}.anchor",
        )
        _require_refs([*state.goal_proposition_ids, *state.stakes_proposition_ids], index.propositions, errors, state_id)
        if not 0 <= state.pressure <= 1:
            errors.append(f"[PRESSURE_RANGE] {state_id}.pressure 必须在 0..1")
        if not any((
            state.goal_proposition_ids,
            state.stakes_proposition_ids,
            state.relationship_state,
            state.emotion,
            _norm(state.tactic),
        )):
            errors.append(f"[CHARACTER_DRAMATIC_STATE_EMPTY] {state_id} 没有目标、代价、关系、情绪或策略贡献")


def _validate_character_decision_chains(
    index: Any,
    event_order: dict[str, int],
    errors: list[str],
) -> None:
    """Check every action requiring a decision has a preceding belief/dramatic-state chain for its actor."""
    beliefs_by_character: defaultdict[str, list[Any]] = defaultdict(list)
    states_by_character: defaultdict[str, list[Any]] = defaultdict(list)
    for snapshot in index.character_beliefs.values():
        beliefs_by_character[snapshot.character_id].append(snapshot)
    for state in index.character_states.values():
        states_by_character[state.character_id].append(state)

    for event_id, event in index.events.items():
        event_position = event_order[event_id]
        for action_id in event.action_ids:
            action = index.actions.get(action_id)
            if action is None:
                continue
            for actor_id in action.actor_ids:
                eligible_beliefs = [
                    item for item in beliefs_by_character.get(actor_id, [])
                    if item.anchor.type == "event"
                    and event_order.get(item.anchor.id, len(event_order)) <= event_position
                    and action_id in item.decision_action_ids
                ]
                if action.decision_requirement == "applies" and not any(
                    item.decision_proposition_ids and item.decision_basis_ids
                    for item in eligible_beliefs
                ):
                    errors.append(
                        f"[CHARACTER_DECISION_CHAIN_MISSING] {event_id}/{action_id} 的执行者 "
                        f"{actor_id} 缺少感知→判断→选择依据"
                    )
                eligible_states = [
                    item for item in states_by_character.get(actor_id, [])
                    if item.anchor.type == "event"
                    and event_order.get(item.anchor.id, len(event_order)) <= event_position
                ]
                if action.decision_requirement == "applies" and not eligible_states:
                    errors.append(
                        f"[CHARACTER_DRAMATIC_STATE_MISSING] {event_id}/{action_id} 的执行者 "
                        f"{actor_id} 缺少目标/情绪/关系状态"
                    )

