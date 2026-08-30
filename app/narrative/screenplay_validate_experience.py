"""Audience-prior, audience-state and experience-intent (outer) validation
phases of validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
The per-audience-path body of the experience-intent phase (the original
source's innermost, by far largest loop) lives in
screenplay_validate_experience_paths.py, called once per (intent, path)
pair from _validate_experience_intents below.
"""
from __future__ import annotations

from typing import Any

from .primitives import _anchor_ref_errors, _norm, _require_refs
from .screenplay_validate_experience_paths import _validate_experience_intent_path


def _validate_audience_priors(index: Any, plan: Any, errors: list[str]) -> set[str]:
    """Check each audience prior's scope and known/unknown proposition consistency; return the prior-id set."""
    prior_ids = set(index.priors)
    for prior_id, prior in index.priors.items():
        if prior.scope_id != plan.scope_id:
            errors.append(
                f"[AUDIENCE_PRIOR_SCOPE_MISMATCH] {prior_id}.scope_id={prior.scope_id} "
                f"不属于当前叙事作用域 {plan.scope_id}"
            )
        _require_refs([*prior.assumed_known_proposition_ids, *prior.assumed_unknown_proposition_ids], index.propositions, errors, prior_id)
        overlap = set(prior.assumed_known_proposition_ids).intersection(prior.assumed_unknown_proposition_ids)
        if overlap:
            errors.append(f"[AUDIENCE_PRIOR_CONFLICT] {prior_id} 同时假定知道和不知道 {sorted(overlap)}")
        if not _norm(prior.audience_description):
            errors.append(f"[AUDIENCE_PRIOR_DESCRIPTION_MISSING] {prior_id} 缺少一次观看前提")
    return prior_ids


def _validate_audience_states(index: Any, event_order: dict[str, int], errors: list[str]) -> None:
    """Check each audience state's anchor, refs, belief validity and no-future-evidence constraint."""
    for state_id, state in index.audience_states.items():
        _anchor_ref_errors(
            state.anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{state_id}.anchor",
        )
        _require_refs([state.audience_prior_id], index.priors, errors, state_id)
        _require_refs(state.active_question_ids, index.questions, errors, f"{state_id}.active_question_ids")
        for belief in state.beliefs:
            _require_refs([belief.proposition_id], index.propositions, errors, state_id)
            _require_refs(belief.evidence_ids, index.evidence, errors, state_id)
            if belief.stance not in {"believed", "suspected", "rejected", "unknown"}:
                errors.append(f"[AUDIENCE_BELIEF_STANCE_INVALID] {state_id}/{belief.proposition_id} stance 非法")
            if not 0 <= belief.confidence <= 1:
                errors.append(f"[CONFIDENCE_RANGE] {state_id}/{belief.proposition_id}.confidence 必须在 0..1")
            for evidence_id in belief.evidence_ids:
                evidence = index.evidence.get(evidence_id)
                if evidence and "audience" not in evidence.perceivable_by:
                    errors.append(f"[AUDIENCE_EVIDENCE_NOT_PERCEIVABLE] {state_id} 引用了观众不可感知的 {evidence_id}")
                if (
                    evidence
                    and state.anchor.type == "event"
                    and evidence.anchor.type == "event"
                    and event_order.get(evidence.anchor.id, -1) > event_order.get(state.anchor.id, -1)
                ):
                    errors.append(f"[AUDIENCE_EVIDENCE_FROM_FUTURE] {state_id} 依据未来事件证据 {evidence_id}")
        for memory in state.working_memory:
            if not isinstance(memory, dict):
                errors.append(f"[AUDIENCE_MEMORY_INVALID] {state_id}.working_memory 必须是结构化条目")
                continue
            proposition_id = _norm(memory.get("proposition_id"))
            _require_refs([proposition_id], index.propositions, errors, f"{state_id}.working_memory")
            retention = memory.get("retention_confidence")
            if not isinstance(retention, (int, float)) or not 0 <= float(retention) <= 1:
                errors.append(f"[AUDIENCE_MEMORY_CONFIDENCE_INVALID] {state_id}/{proposition_id} 保留置信度必须在 0..1")



def _validate_experience_intents(
    index: Any,
    plan: Any,
    declared_entity_ids: set[str],
    prior_ids: set[str],
    event_order: dict[str, int],
    errors: list[str],
) -> None:
    """Check each experience intent's scope/refs/prior-path coverage, then each of its audience paths."""
    target_delta_ids: set[str] = set()
    for intent_id, intent in index.intents.items():
        allowed_intent_scopes = {plan.scope_id, *index.scenes, *index.arcs}
        if intent.scope_id not in allowed_intent_scopes:
            errors.append(
                f"[EXPERIENCE_INTENT_SCOPE_MISMATCH] {intent_id}.scope_id={intent.scope_id} "
                "未绑定当前集、场景或段落合同"
            )
        _require_refs(intent.anchor_event_ids, index.events, errors, intent_id)
        _require_refs(
            intent.attention_target_ids,
            declared_entity_ids | set(index.propositions),
            errors,
            f"{intent_id}.attention_target_ids",
        )
        path_prior_ids = {path.audience_prior_id for path in intent.audience_paths}
        if len(path_prior_ids) != len(intent.audience_paths):
            errors.append(f"[AUDIENCE_PATH_PRIOR_DUPLICATE] {intent_id} 为同一观众先验声明了多条未分期路径")
        missing_priors = prior_ids - path_prior_ids
        if missing_priors:
            errors.append(f"[AUDIENCE_PATH_MISSING] {intent_id} 缺少观众先验路径 {sorted(missing_priors)}")
        for withheld in intent.withheld_propositions:
            _require_refs([withheld.proposition_id], index.propositions, errors, intent_id)
            if not _norm(withheld.reason) or not (withheld.future_disclosure_anchor or withheld.carried_question_id):
                errors.append(f"[WITHHELD_WITHOUT_CONTRACT] {intent_id}/{withheld.proposition_id} 缺少隐藏理由及未来锚点/延续问题")
            if withheld.future_disclosure_anchor:
                _anchor_ref_errors(
                    withheld.future_disclosure_anchor,
                    events=index.events,
                    scenes=index.scenes,
                    errors=errors,
                    subject=f"{intent_id}/{withheld.proposition_id}.future_disclosure_anchor",
                )
            if withheld.carried_question_id:
                _require_refs([withheld.carried_question_id], index.questions, errors, intent_id)
        withheld_by_proposition = {
            item.proposition_id: item for item in intent.withheld_propositions
        }
        for path in intent.audience_paths:

            _validate_experience_intent_path(
                index, event_order, path, withheld_by_proposition,
                target_delta_ids, errors,
            )
