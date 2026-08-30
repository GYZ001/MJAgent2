"""Normalizes state-subject evidence projection and derives blueprint state-subject issues."""
from __future__ import annotations

from collections import defaultdict

from app.source_facts import source_facts

from .models_core import (
    NarrativeBlueprint,
    NarrativeBlueprintShard,
    NarrativeParticipantEvidence,
    NarrativeStateSubjectAssignment,
)
from .models_patch import BlueprintSemanticIssue
from .state_subject_perception import _node_identity_has_perception_evidence


def normalize_blueprint_state_subject_evidence_projection(
    candidate: NarrativeBlueprint | NarrativeBlueprintShard,
    source_text: str,
) -> int:
    """Remove non-action keys from provider-authored state-subject rows."""
    action_unit_keys = {
        fact.source_unit_key
        for fact in source_facts(source_text)
        if fact.projection == "action"
    }
    removed = 0
    for node in candidate.nodes:
        retained_evidence: list[NarrativeParticipantEvidence] = []
        for evidence in node.participant_evidence:
            if evidence.usage != "state_subject":
                retained_evidence.append(evidence)
                continue
            retained_keys = [
                unit_key
                for unit_key in evidence.source_unit_keys
                if unit_key in action_unit_keys
            ]
            removed += len(evidence.source_unit_keys) - len(retained_keys)
            if not retained_keys:
                continue
            if retained_keys == evidence.source_unit_keys:
                retained_evidence.append(evidence)
                continue
            retained = evidence.model_copy(deep=True)
            retained.source_unit_keys = retained_keys
            retained_evidence.append(retained)
        node.participant_evidence = retained_evidence
    return removed


def blueprint_state_subject_issues(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[BlueprintSemanticIssue]:
    """Require one typed owner for every prose unit before scene generation."""
    facts = source_facts(source_text)
    facts_by_key = {fact.source_unit_key: fact for fact in facts}
    issues: list[BlueprintSemanticIssue] = []
    for node in blueprint.nodes:
        if node.source_semantics().projection_policy != "picture":
            continue
        owned_sources = set(node.source_segment_ids)
        action_facts = [
            fact for fact in facts
            if (
                fact.projection == "action"
                and fact.source_segment_id in owned_sources
            )
        ]
        environment_keys = list(node.environment_source_unit_keys)
        if len(environment_keys) != len(set(environment_keys)):
            duplicate_environment_keys = list(dict.fromkeys(
                key for key in environment_keys
                if environment_keys.count(key) > 1
            ))
            issues.append(BlueprintSemanticIssue(
                code="state_subject_environment_duplicate",
                node_keys=[node.key],
                source_segment_ids=list(node.source_segment_ids),
                source_unit_keys=duplicate_environment_keys,
                message="environment_source_unit_keys 含重复 source unit",
                required_resolution="每个环境 source unit 只能显式声明一次",
            ))
        invalid_environment_keys = [
            key for key in environment_keys
            if (
                key not in facts_by_key
                or facts_by_key[key].projection != "action"
                or facts_by_key[key].source_segment_id not in owned_sources
            )
        ]
        if invalid_environment_keys:
            issues.append(BlueprintSemanticIssue(
                code="state_subject_environment_invalid",
                node_keys=[node.key],
                source_segment_ids=list(node.source_segment_ids),
                source_unit_keys=list(invalid_environment_keys),
                message=(
                    "environment_source_unit_keys 引用非本节点 prose unit："
                    + "、".join(invalid_environment_keys)
                ),
                required_resolution="只标记本节点拥有的 prose/action source unit",
            ))

        claims_by_unit: defaultdict[
            str, list[NarrativeParticipantEvidence]
        ] = defaultdict(list)
        for evidence in node.participant_evidence:
            if evidence.usage != "state_subject":
                continue
            if not evidence.source_unit_keys:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_unit_missing",
                    node_keys=[node.key],
                    source_segment_ids=list(evidence.source_segment_ids),
                    source_unit_keys=[],
                    message=(
                        f"{evidence.identity_key} 的 state_subject evidence "
                        "缺少精确 source_unit_keys"
                    ),
                    required_resolution=(
                        "把状态主体绑定到本节点具体 prose source unit"
                    ),
                ))
                continue
            for key in evidence.source_unit_keys:
                fact = facts_by_key.get(key)
                if (
                    fact is None
                    or fact.projection != "action"
                    or fact.source_segment_id not in owned_sources
                    or fact.source_segment_id not in evidence.source_segment_ids
                    or bool(
                        set(evidence.source_segment_ids) - owned_sources
                    )
                ):
                    issues.append(BlueprintSemanticIssue(
                        code="state_subject_unit_invalid",
                        node_keys=[node.key],
                        source_segment_ids=list(evidence.source_segment_ids),
                        source_unit_keys=[key],
                        message=(
                            f"{evidence.identity_key} 的 state_subject "
                            f"引用非本节点 prose unit {key}"
                        ),
                        required_resolution=(
                            "只绑定本节点拥有的 prose/action source unit"
                        ),
                    ))
                    continue
                claims_by_unit[key].append(evidence)

        assignments_by_unit: defaultdict[
            str, list[NarrativeStateSubjectAssignment]
        ] = defaultdict(list)
        for assignment in node.state_subject_assignments:
            fact = facts_by_key.get(assignment.source_unit_key)
            invalid_identities = (
                set(assignment.identity_keys) - set(node.participants)
            )
            if (
                fact is None
                or fact.projection != "action"
                or fact.source_segment_id not in owned_sources
                or invalid_identities
            ):
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_assignment_invalid",
                    node_keys=[node.key],
                    source_segment_ids=list(node.source_segment_ids),
                    source_unit_keys=[assignment.source_unit_key],
                    message=(
                        f"{assignment.source_unit_key} 的 joint state subject "
                        "引用非本节点 action unit 或非 participants identity"
                    ),
                    required_resolution=(
                        "joint assignment 只绑定本节点 action unit，"
                        "identity_keys 必须是有来源证据的 participants"
                    ),
                ))
                continue
            assignments_by_unit[assignment.source_unit_key].append(assignment)

        for fact in action_facts:
            claims = claims_by_unit.get(fact.source_unit_key, [])
            assignments = assignments_by_unit.get(fact.source_unit_key, [])
            explicit = list(dict.fromkeys(
                evidence.identity_key
                for evidence in claims
                if evidence.usage == "state_subject"
            ))
            environment = fact.source_unit_key in environment_keys
            if len(assignments) > 1:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_assignment_ambiguous",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=(
                        f"{fact.source_unit_key} 存在多个 joint state subject "
                        "assignment"
                    ),
                    required_resolution="每个共同动作 unit 只能有一条 joint assignment",
                ))
            elif environment and (explicit or assignments):
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_environment_conflict",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=(
                        f"{fact.source_unit_key} 同时声明人物主体与 environment"
                    ),
                    required_resolution="人物主体和纯环境标记必须二选一",
                ))
            elif assignments and claims:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_assignment_conflict",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=(
                        f"{fact.source_unit_key} 同时声明 single 与 joint "
                        "state subject"
                    ),
                    required_resolution=(
                        "可拆单主体动作使用唯一 state_subject；"
                        "结构上不可拆的共同动作仅使用 joint assignment"
                    ),
                ))
            elif len(claims) > 1:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_ambiguous",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=(
                        f"{fact.source_unit_key} 存在多个候选状态主体："
                        + "、".join(
                            evidence.identity_key for evidence in claims
                        )
                    ),
                    required_resolution=(
                        "仅修此报错 unit：可拆动作保留唯一 "
                        "usage=state_subject evidence；结构切分后仍不可拆的"
                        "共同动作移除该 unit 的全部 single state_subject claims，"
                        "建立唯一 mode=joint assignment，identity_keys 列出全部"
                        "有来源共同主体且至少 2 个；其他 unit ownership 不得变化"
                    ),
                ))
            elif not explicit and not assignments and not environment:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_missing",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=f"{fact.source_unit_key} 缺少结构化状态主体",
                    required_resolution=(
                        "人物思考/动作/反应填唯一 state_subject evidence；"
                        "结构上不可拆的共同动作填唯一 joint assignment；"
                        "真正无人物的环境单元填 "
                        "environment_source_unit_keys"
                    ),
                ))
            subject_identities: list[str] = []
            if not environment:
                if len(claims) == 1 and not assignments:
                    subject_identities = [claims[0].identity_key]
                elif len(assignments) == 1 and not claims:
                    subject_identities = list(assignments[0].identity_keys)
            for identity_key in subject_identities:
                if _node_identity_has_perception_evidence(
                    node,
                    identity_key=identity_key,
                    source_unit_key=fact.source_unit_key,
                ):
                    continue
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_perception_missing",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=(
                        f"{fact.source_unit_key} 的人物主体 {identity_key} "
                        "缺少适用的 visible/voice evidence"
                    ),
                    required_resolution=(
                        "补充确定性可感知的 visible/voice evidence；"
                        "若该 unit 实际无人物主体则改为 environment"
                    ),
                ))
    return issues
