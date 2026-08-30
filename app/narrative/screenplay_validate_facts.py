"""State-fact, dramatic-question and evidence validation phases of
validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
"""
from __future__ import annotations

from typing import Any

from app.schemas import is_system_environment_entity_id

from .primitives import _anchor_ref_errors, _norm, _require_refs


def _validate_state_facts(
    index: Any,
    declared_entity_ids: set[str],
    environment_entity_id: str,
    errors: list[str],
) -> None:
    """Check each state fact's proposition ref, subject declaration/scope and basic field validity."""
    for fact_id, fact in index.facts.items():
        _require_refs([fact.proposition_id], index.propositions, errors, fact_id)
        if _norm(fact.subject_id) not in declared_entity_ids:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {fact_id}.subject_id={fact.subject_id} 未在命题身份图中声明")
        if is_system_environment_entity_id(fact.subject_id):
            if fact.subject_id != environment_entity_id:
                errors.append(
                    f"[SYSTEM_NARRATIVE_ENTITY_SCOPE_MISMATCH] {fact_id}.subject_id="
                    f"{fact.subject_id} 不属于当前作用域"
                )
            proposition = index.propositions.get(fact.proposition_id)
            if (
                proposition is not None
                and fact.subject_id not in proposition.entity_ids
            ):
                errors.append(
                    f"[SYSTEM_NARRATIVE_ENTITY_PROPOSITION_MISSING] {fact_id} 的"
                    f"系统环境主体未由命题 {fact.proposition_id}.entity_ids 声明"
                )
        if not _norm(fact.predicate_id):
            errors.append(f"[STATE_PREDICATE_MISSING] {fact_id}.predicate_id 不能为空")
        if fact.provenance not in {"source", "screenplay", "storyboard"}:
            errors.append(f"[STATE_PROVENANCE_INVALID] {fact_id}.provenance 非法")
        if not _norm(fact.time_scope):
            errors.append(f"[STATE_TIME_SCOPE_MISSING] {fact_id}.time_scope 不能为空")
        if not 0 <= fact.confidence <= 1:
            errors.append(f"[CONFIDENCE_RANGE] {fact_id}.confidence 必须在 0..1")


def _validate_dramatic_questions(index: Any, errors: list[str]) -> None:
    """Check each dramatic question's text, target propositions, anchors and status/resolution consistency."""
    for question_id, question in index.questions.items():
        if not _norm(question.question_text):
            errors.append(f"[DRAMATIC_QUESTION_TEXT_MISSING] {question_id}.question_text 不能为空")
        _require_refs(question.target_proposition_ids, index.propositions, errors, question_id)
        _anchor_ref_errors(
            question.open_anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{question_id}.open_anchor",
        )
        if question.resolution_anchor is not None:
            _anchor_ref_errors(
                question.resolution_anchor,
                events=index.events,
                scenes=index.scenes,
                errors=errors,
                subject=f"{question_id}.resolution_anchor",
            )
        if question.status not in {"open", "resolved", "carried"}:
            errors.append(f"[DRAMATIC_QUESTION_STATUS_INVALID] {question_id}.status 非法")
        if question.status == "resolved" and question.resolution_anchor is None:
            errors.append(f"[DRAMATIC_QUESTION_RESOLUTION_MISSING] {question_id} 已 resolved 但没有 resolution_anchor")


def _validate_evidence(index: Any, declared_entity_ids: set[str], errors: list[str]) -> None:
    """Check each evidence item's proposition refs, anchor, perceiver declarations and field validity."""
    event_or_scene_ids = set(index.events) | set(index.scenes)

    for evidence_id, evidence in index.evidence.items():
        _require_refs(evidence.supports_proposition_ids, index.propositions, errors, evidence_id)
        if evidence.anchor.type in {"event", "scene"}:
            _require_refs([evidence.anchor.id], event_or_scene_ids, errors, f"{evidence_id}.anchor")
        if not evidence.perceivable_by:
            errors.append(f"[EVIDENCE_AUDIENCE_MISSING] {evidence_id}.perceivable_by 不能为空")
        undeclared_perceivers = {
            entity_id
            for entity_id in evidence.perceivable_by
            if entity_id != "audience" and entity_id not in declared_entity_ids
        }
        if undeclared_perceivers:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {evidence_id}.perceivable_by 含未声明身份 {sorted(undeclared_perceivers)}")
        environment_perceivers = {
            entity_id
            for entity_id in evidence.perceivable_by
            if is_system_environment_entity_id(entity_id)
        }
        if environment_perceivers:
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {evidence_id}.perceivable_by "
                f"把系统环境实体当作感知者 {sorted(environment_perceivers)}"
            )
        if not _norm(evidence.observable_claim):
            errors.append(f"[EVIDENCE_CLAIM_MISSING] {evidence_id}.observable_claim 不能为空")
        _anchor_ref_errors(
            evidence.anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{evidence_id}.anchor",
        )
        if not 0 <= evidence.planned_salience <= 1:
            errors.append(f"[SALIENCE_RANGE] {evidence_id}.planned_salience 必须在 0..1")
        if evidence.planned_duration_s is not None and evidence.planned_duration_s < 0:
            errors.append(f"[EVIDENCE_DURATION_INVALID] {evidence_id}.planned_duration_s 不能为负")

