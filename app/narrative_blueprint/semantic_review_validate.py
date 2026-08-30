"""Validates a semantic review against the blueprint: agency continuity, environment-subject contract errors, resolved-issue and dialogue-authority checks, and voice-issue filtering."""
from __future__ import annotations

from collections import defaultdict

from app.source_excerpt import index_source_segments
from app.source_facts import SourceFact, source_facts

from .constants import _CANONICAL_SOURCE_UNIT_REFERENCE_RE
from .models_core import NarrativeBlueprint
from .models_patch import BlueprintSemanticIssue, BlueprintSemanticReview
from .state_subject_issues import blueprint_state_subject_issues
from .state_subject_perception import _node_state_subject_repairable_identities
from .voice_identity_issues import blueprint_voice_identity_issues


def normalize_blueprint_agency_continuity(
    blueprint: NarrativeBlueprint,
) -> int:
    """Project unresolved coercion forward until its fact is explicitly released."""
    active_constraints: dict[str, tuple[str, str]] = {}
    release_nodes: defaultdict[str, set[str]] = defaultdict(set)
    changes = 0
    for node in blueprint.nodes:
        decision = node.decision
        if (
            decision is not None
            and decision.agency_mode in {"coerced", "incapacitated"}
            and decision.constraint_fact_key
        ):
            constraint_fact_key = decision.constraint_fact_key
            for state_change in node.state_changes:
                filtered = [
                    fact_key
                    for fact_key in state_change.supersedes_fact_keys
                    if fact_key != constraint_fact_key
                ]
                if filtered != state_change.supersedes_fact_keys:
                    state_change.supersedes_fact_keys = filtered
                    changes += 1
            filtered_releases = [
                value
                for value in node.released_constraints_for
                if value not in {
                    decision.actor_key,
                    constraint_fact_key,
                }
            ]
            if filtered_releases != node.released_constraints_for:
                node.released_constraints_for = filtered_releases
                changes += 1
        released_values = set(node.released_constraints_for)
        for actor_key, (fact_key, _mode) in list(
            active_constraints.items()
        ):
            fact_released = any(
                fact_key in change.supersedes_fact_keys
                for change in node.state_changes
            )
            if (
                fact_released
                and (
                    actor_key in released_values
                    or fact_key in released_values
                )
            ):
                active_constraints.pop(actor_key, None)
                release_nodes[actor_key].add(node.key)

        if decision is None:
            continue
        valid_release_keys = [
            key
            for key in decision.constraint_release_node_keys
            if key in release_nodes[decision.actor_key]
        ]
        if valid_release_keys != decision.constraint_release_node_keys:
            decision.constraint_release_node_keys = valid_release_keys
            changes += 1
        active = active_constraints.get(decision.actor_key)
        if active is not None and decision.agency_mode == "voluntary":
            constraint_fact_key, agency_mode = active
            decision.agency_mode = agency_mode
            decision.constraint_fact_key = constraint_fact_key
            decision.narrative_attribution = "external_coercion"
            decision.agency_change_reason = (
                "程序继承尚未解除的约束事实，禁止提前恢复自主"
            )
            changes += 1
        if (
            decision.agency_mode in {"coerced", "incapacitated"}
            and decision.constraint_fact_key
        ):
            active_constraints[decision.actor_key] = (
                decision.constraint_fact_key,
                decision.agency_mode,
            )
    return changes


def _blueprint_environment_subject_issue_contract_errors(
    issue: BlueprintSemanticIssue,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[str]:
    """Validate exact scope without making a semantic subject judgment."""
    if issue.code != "state_subject_environment_misclassified":
        return []

    errors: list[str] = []
    declared_unit_keys = set(issue.source_unit_keys)
    text_unit_keys = list(dict.fromkeys(
        _CANONICAL_SOURCE_UNIT_REFERENCE_RE.findall(issue.message)
        + _CANONICAL_SOURCE_UNIT_REFERENCE_RE.findall(
            issue.required_resolution
        )
    ))
    undeclared_text_unit_keys = [
        unit_key
        for unit_key in text_unit_keys
        if unit_key not in declared_unit_keys
    ]
    if undeclared_text_unit_keys:
        errors.append(
            "state_subject_environment_misclassified 的 "
            "message/required_resolution 引用了 source_unit_keys 未声明的 "
            "canonical exact unit："
            + "、".join(undeclared_text_unit_keys)
        )
    if len(issue.node_keys) != 1:
        errors.append(
            "state_subject_environment_misclassified 必须恰好引用一个节点"
        )
        return errors
    node = next(
        (item for item in blueprint.nodes if item.key == issue.node_keys[0]),
        None,
    )
    if node is None:
        errors.append(
            "state_subject_environment_misclassified 必须引用现有节点"
        )
        return errors

    unit_keys = list(issue.source_unit_keys)
    if (
        not unit_keys
        or any(not unit_key.strip() for unit_key in unit_keys)
        or len(unit_keys) != len(set(unit_keys))
    ):
        errors.append(
            "state_subject_environment_misclassified 必须引用非空且唯一的 "
            "exact action source_unit_keys"
        )
        return errors

    facts_by_key = {
        fact.source_unit_key: fact
        for fact in source_facts(source_text)
    }
    target_facts: list[SourceFact] = []
    for unit_key in unit_keys:
        fact = facts_by_key.get(unit_key)
        if fact is None or fact.projection != "action":
            errors.append(
                "state_subject_environment_misclassified 只能引用 canonical "
                f"action source unit：{unit_key}"
            )
            continue
        target_facts.append(fact)
        if fact.source_segment_id not in node.source_segment_ids:
            errors.append(
                f"{unit_key} 不属于节点 {node.key} 拥有的 SRC"
            )
        if unit_key not in node.environment_source_unit_keys:
            errors.append(
                f"{unit_key} 当前不是节点 {node.key} 的 environment ownership"
            )
        if unit_key in node.state_subject_adjudicated_unit_keys:
            errors.append(f"{unit_key} 已完成 state subject adjudication")
        if not _node_state_subject_repairable_identities(
            node,
            source_unit_key=unit_key,
        ):
            errors.append(
                f"{unit_key} 没有 existing participant visible/voice authority"
            )

    if len(target_facts) == len(unit_keys):
        expected_source_ids = list(dict.fromkeys(
            fact.source_segment_id for fact in target_facts
        ))
        if issue.source_segment_ids != expected_source_ids:
            errors.append(
                "state_subject_environment_misclassified 的 "
                "source_segment_ids 必须与 exact units 精确匹配"
            )
    return errors


def validate_blueprint_semantic_review(
    review: BlueprintSemanticReview,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[str]:
    errors: list[str] = []
    node_keys = {node.key for node in blueprint.nodes}
    source_ids = {
        segment.segment_id
        for segment in index_source_segments(source_text)
    }
    source_unit_keys = {
        fact.source_unit_key
        for fact in source_facts(source_text)
    }
    for index, issue in enumerate(review.issues, start=1):
        if not issue.node_keys:
            errors.append(
                f"[BLUEPRINT_REVIEW_NODE_REQUIRED] issue {index} 没有节点"
            )
        unknown_nodes = set(issue.node_keys) - node_keys
        if unknown_nodes:
            errors.append(
                f"[BLUEPRINT_REVIEW_NODE_UNKNOWN] issue {index} 引用未知节点："
                + "、".join(sorted(unknown_nodes))
            )
        unknown_sources = set(issue.source_segment_ids) - source_ids
        if unknown_sources:
            errors.append(
                f"[BLUEPRINT_REVIEW_SOURCE_UNKNOWN] issue {index} "
                "引用未知来源："
                + "、".join(sorted(unknown_sources))
            )
        unknown_units = set(issue.source_unit_keys) - source_unit_keys
        if unknown_units:
            errors.append(
                f"[BLUEPRINT_REVIEW_SOURCE_UNIT_UNKNOWN] issue {index} "
                "引用未知 exact unit："
                + "、".join(sorted(unknown_units))
            )
        errors.extend(
            "[BLUEPRINT_REVIEW_STATE_SUBJECT_ENVIRONMENT_CONTRACT] "
            f"issue {index}：{error}"
            for error in _blueprint_environment_subject_issue_contract_errors(
                issue,
                blueprint,
                source_text,
            )
        )
    return errors


def blueprint_semantic_issue_is_resolved(
    issue: BlueprintSemanticIssue,
    blueprint: NarrativeBlueprint,
) -> bool:
    """Recognize a structurally completed setup bridge despite stale review text."""
    if issue.code != "setup_missing" or not issue.node_keys:
        return False
    nodes = {node.key: node for node in blueprint.nodes}
    targets = [nodes.get(key) for key in issue.node_keys]
    if any(node is None for node in targets):
        return False
    issue_sources = set(issue.source_segment_ids)
    return all(
        node is not None
        and node.adaptation_kind == "logic_bridge"
        and bool(node.bridge_rationale.strip())
        and bool(
            node.transition_cue.strip()
            or node.opening_image.strip()
        )
        and (
            not issue_sources
            or issue_sources.issubset(node.source_segment_ids)
        )
        for node in targets
    )


def blueprint_semantic_voice_issue_has_dialogue_authority(
    issue: BlueprintSemanticIssue,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> bool:
    """Require exact deterministic support for contract-shaped findings."""
    if issue.code == "state_subject_environment_misclassified":
        return blueprint_environment_subject_issue_has_exact_authority(
            issue,
            blueprint,
            source_text,
        )
    if issue.code.startswith(("voice_identity_", "source_delivery_")):
        candidate_issues = blueprint_voice_identity_issues(
            blueprint,
            source_text,
        )
    elif issue.code.startswith("state_subject_"):
        candidate_issues = blueprint_state_subject_issues(
            blueprint,
            source_text,
        )
    else:
        return True
    deterministic_issues = [
        deterministic_issue
        for deterministic_issue in candidate_issues
        if deterministic_issue.code == issue.code
    ]
    issue_node_keys = set(issue.node_keys)
    supported_node_keys = {
        node_key
        for deterministic_issue in deterministic_issues
        for node_key in deterministic_issue.node_keys
    }
    if (
        not issue_node_keys
        or not issue_node_keys.issubset(supported_node_keys)
    ):
        return False
    relevant_deterministic_issues = [
        deterministic_issue
        for deterministic_issue in deterministic_issues
        if (
            issue_node_keys.intersection(deterministic_issue.node_keys)
            and set(issue.source_segment_ids).intersection(
                deterministic_issue.source_segment_ids
            )
        )
    ]
    supported_source_ids = {
        source_id
        for deterministic_issue in relevant_deterministic_issues
        for source_id in deterministic_issue.source_segment_ids
    }
    if not (
        issue.source_segment_ids
        and set(issue.source_segment_ids).issubset(supported_source_ids)
    ):
        return False
    if not issue.source_unit_keys:
        return True
    supported_source_unit_keys = {
        source_unit_key
        for deterministic_issue in relevant_deterministic_issues
        for source_unit_key in deterministic_issue.source_unit_keys
    }
    return bool(supported_source_unit_keys) and set(
        issue.source_unit_keys
    ).issubset(supported_source_unit_keys)


def blueprint_environment_subject_issue_has_exact_authority(
    issue: BlueprintSemanticIssue,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> bool:
    """Bind a semantic environment finding to unresolved exact units.

    This check proves scope only. It deliberately does not infer a subject
    from punctuation, text, visibility, or the participant roster; two
    independent semantic reviewers remain responsible for the classification.
    """
    return (
        issue.code == "state_subject_environment_misclassified"
        and not _blueprint_environment_subject_issue_contract_errors(
            issue,
            blueprint,
            source_text,
        )
    )


def filter_blueprint_semantic_review_voice_issues(
    review: BlueprintSemanticReview,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> int:
    """Drop unsupported delivery/subject guesses before reviewer consensus."""
    retained = [
        issue
        for issue in review.issues
        if blueprint_semantic_voice_issue_has_dialogue_authority(
            issue,
            blueprint,
            source_text,
        )
    ]
    removed = len(review.issues) - len(retained)
    review.issues = retained
    return removed
