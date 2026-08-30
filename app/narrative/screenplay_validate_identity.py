"""Declared-entity-id, identity, proposition and adaptation-decision
validation phases of validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
"""
from __future__ import annotations

from typing import Any

from app.schemas import is_system_environment_entity_id

from .primitives import _norm, _require_refs


def _build_declared_entity_ids(index: Any) -> set[str]:
    """Collect every entity_id declared by any proposition -- the identity graph's vocabulary."""
    declared_entity_ids = {
        _norm(entity_id)
        for proposition in index.propositions.values()
        for entity_id in proposition.entity_ids
        if _norm(entity_id)
    }
    return declared_entity_ids


def _validate_reserved_environment_entities(
    declared_entity_ids: set[str],
    environment_entity_id: str,
    errors: list[str],
) -> None:
    """Flag declared entity ids that collide with another scope's reserved system-environment entity."""
    reserved_environment_ids = {
        entity_id
        for entity_id in declared_entity_ids
        if is_system_environment_entity_id(entity_id)
    }
    foreign_environment_ids = reserved_environment_ids - {environment_entity_id}
    if foreign_environment_ids:
        errors.append(
            "[SYSTEM_NARRATIVE_ENTITY_SCOPE_MISMATCH] 命题身份图包含其他作用域的"
            f"系统环境实体 {sorted(foreign_environment_ids)}"
        )


def _validate_identities(
    index: Any,
    declared_entity_ids: set[str],
    errors: list[str],
) -> None:
    """Check each identity's reserved-token usage, display-name uniqueness and evidence traceability."""
    identity_display_names: dict[str, str] = {}
    for identity_id, identity in index.identities.items():
        reserved_identity_tokens = {
            token
            for token in [
                identity_id,
                identity.display_name,
                *identity.voice_ids,
            ]
            if is_system_environment_entity_id(token)
        }
        if reserved_identity_tokens:
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {identity_id} 把系统环境实体 "
                f"{sorted(reserved_identity_tokens)} 注册为人物/声音身份"
            )
        display_name = _norm(identity.display_name)
        if display_name in identity_display_names:
            errors.append(
                f"[IDENTITY_DISPLAY_NAME_AMBIGUOUS] display_name={display_name} 同时指向 "
                f"{identity_display_names[display_name]} 与 {identity_id}"
            )
        else:
            identity_display_names[display_name] = identity_id
        evidence = identity.evidence
        _require_refs(
            evidence.source_evidence_ids,
            index.source_evidence,
            errors,
            f"{identity_id}.evidence.source_evidence_ids",
        )
        _require_refs(
            evidence.proposition_ids,
            index.propositions,
            errors,
            f"{identity_id}.evidence.proposition_ids",
        )
        _require_refs(
            evidence.adaptation_decision_ids,
            index.decisions,
            errors,
            f"{identity_id}.evidence.adaptation_decision_ids",
        )
        if not any((
            evidence.source_evidence_ids,
            evidence.proposition_ids,
            evidence.adaptation_decision_ids,
        )):
            errors.append(
                f"[IDENTITY_EVIDENCE_MISSING] {identity_id} 缺少可追溯的 evidence ID"
            )
        if not _norm(evidence.rationale):
            errors.append(
                f"[IDENTITY_RATIONALE_MISSING] {identity_id} 缺少身份意图判定 rationale"
            )
        linked_graph_ids = {identity_id, *identity.voice_ids}
        if (
            not linked_graph_ids.intersection(declared_entity_ids)
            and not evidence.proposition_ids
            and not identity.voice_ids
        ):
            errors.append(
                f"[IDENTITY_GRAPH_LINK_MISSING] {identity_id} 既未进入命题身份图，"
                "也未通过 evidence.proposition_ids/voice_ids 绑定叙事用途"
            )


def _validate_propositions(
    index: Any,
    adapted_ids: set[str],
    errors: list[str],
) -> None:
    """Check proposition semantic-identity/statement uniqueness, domain validity and source grounding."""
    proposition_identity_owners: dict[tuple[str, str], str] = {}
    proposition_statement_owners: dict[tuple[str, str], str] = {}
    for proposition_id, proposition in index.propositions.items():
        semantic_identity_key = _norm(proposition.semantic_identity_key)
        if not semantic_identity_key:
            errors.append(
                f"[PROPOSITION_SEMANTIC_IDENTITY_MISSING] "
                f"{proposition_id}.semantic_identity_key 不能为空"
            )
        if not _norm(proposition.canonical_statement):
            errors.append(f"[PROPOSITION_STATEMENT_MISSING] {proposition_id}.canonical_statement 不能为空")
        if proposition.narrative_domain not in {"source_canon", "adapted_story"}:
            errors.append(f"[PROPOSITION_DOMAIN_INVALID] {proposition_id}.narrative_domain 非法")
        if semantic_identity_key and proposition.narrative_domain in {
            "source_canon", "adapted_story",
        }:
            identity = (proposition.narrative_domain, semantic_identity_key)
            previous = proposition_identity_owners.get(identity)
            if previous:
                errors.append(
                    f"[PROPOSITION_SEMANTIC_IDENTITY_DUPLICATE] 同一叙事域内 "
                    f"{previous}/{proposition_id} 共用了语义身份 {semantic_identity_key}；"
                    "同一命题只能保留一个 proposition_id"
                )
            else:
                proposition_identity_owners[identity] = proposition_id
            statement_identity = (
                proposition.narrative_domain,
                _norm(proposition.canonical_statement).casefold(),
            )
            previous_statement = proposition_statement_owners.get(statement_identity)
            if previous_statement:
                errors.append(
                    f"[PROPOSITION_CANONICAL_DUPLICATE] 同一叙事域内 "
                    f"{previous_statement}/{proposition_id} 的 canonical_statement 完全相同；"
                    "不得用不同 semantic_identity_key 绕过命题归一"
                )
            else:
                proposition_statement_owners[statement_identity] = proposition_id
        if proposition.domain_truth_status not in {"true", "false", "undetermined", "not_applicable"}:
            errors.append(f"[PROPOSITION_TRUTH_STATUS_INVALID] {proposition_id}.domain_truth_status 非法")
        _require_refs(proposition.direct_source_evidence_ids, index.source_evidence, errors, proposition_id)
        if proposition.narrative_domain == "source_canon" and not proposition.direct_source_evidence_ids:
            errors.append(f"[SOURCE_PROPOSITION_UNGROUNDED] {proposition_id} 缺少直接来源证据")
        if proposition.narrative_domain == "adapted_story":
            adapted_ids.add(proposition_id)
            if proposition.direct_source_evidence_ids:
                errors.append(
                    f"[ADAPTED_PROPOSITION_DIRECT_SOURCE] {proposition_id} 属于 adapted_story，"
                    "不得把原文证据直接当作改写后真值的证明"
                )
        normalized_entities = [_norm(value) for value in proposition.entity_ids]
        if any(not value for value in normalized_entities) or len(set(normalized_entities)) != len(normalized_entities):
            errors.append(f"[PROPOSITION_ENTITY_ID_INVALID] {proposition_id}.entity_ids 含空值或重复身份")


def _validate_adaptation_decisions(
    index: Any,
    adapted_ids: set[str],
    errors: list[str],
) -> None:
    """Check each AdaptationDecision's proposition-domain pairing and relation, then flag undeclared adapted propositions."""
    decided_adapted: set[str] = set()
    adaptation_relations = {
        "preserve", "condense", "split", "combine", "transform", "omit", "invent", "other",
    }
    for decision_id, decision in index.decisions.items():
        _require_refs(decision.source_proposition_ids, index.propositions, errors, decision_id)
        _require_refs(decision.adapted_proposition_ids, index.propositions, errors, decision_id)
        for proposition_id in decision.source_proposition_ids:
            proposition = index.propositions.get(proposition_id)
            if proposition and proposition.narrative_domain != "source_canon":
                errors.append(f"[ADAPTATION_SOURCE_DOMAIN] {decision_id} 的来源 {proposition_id} 不是 source_canon")
        for proposition_id in decision.adapted_proposition_ids:
            proposition = index.propositions.get(proposition_id)
            if proposition and proposition.narrative_domain != "adapted_story":
                errors.append(f"[ADAPTATION_TARGET_DOMAIN] {decision_id} 的结果 {proposition_id} 不是 adapted_story")
            decided_adapted.add(proposition_id)
        if decision.relation == "other" and not _norm(decision.custom_relation):
            errors.append(f"[ADAPTATION_CUSTOM_RELATION_MISSING] {decision_id} relation=other 时必须说明语义关系")
        if decision.relation not in adaptation_relations:
            errors.append(f"[ADAPTATION_RELATION_INVALID] {decision_id}.relation 非法；未预设关系必须用 other + custom_relation")
        if not _norm(decision.creative_reason):
            errors.append(f"[ADAPTATION_REASON_MISSING] {decision_id} 缺少改编理由")
        _require_refs(
            decision.protected_causal_effect_ids,
            set(index.propositions) | set(index.events),
            errors,
            f"{decision_id}.protected_causal_effect_ids",
        )
        _require_refs(decision.affected_event_ids, index.events, errors, f"{decision_id}.affected_event_ids")
    for proposition_id in sorted(adapted_ids - decided_adapted):
        errors.append(f"[ADAPTED_PROPOSITION_UNDECLARED] {proposition_id} 没有 AdaptationDecision")

