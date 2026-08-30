"""Derives blueprint voice/identity issues from effective source-unit delivery decisions."""
from __future__ import annotations

from collections import defaultdict

from app.source_facts import SourceFact, source_facts

from .constants import AUDIBLE_SOURCE_DELIVERY_MODES
from .models_core import NarrativeBlueprint, NarrativeNode, NarrativeParticipantEvidence, NarrativeSourceUnitDelivery
from .models_patch import BlueprintSemanticIssue


def effective_source_unit_deliveries(
    node: NarrativeNode,
) -> list[NarrativeSourceUnitDelivery]:
    """Return explicit delivery decisions plus exact legacy voice bindings."""
    deliveries = [
        item.model_copy(deep=True)
        for item in node.source_unit_deliveries
    ]
    explicit_keys = {
        item.source_unit_key for item in deliveries
    }
    for evidence in node.participant_evidence:
        if evidence.usage != "voice":
            continue
        for key in evidence.source_unit_keys:
            if key in explicit_keys:
                continue
            deliveries.append(NarrativeSourceUnitDelivery(
                source_unit_key=key,
                mode="spoken_dialogue",
                content_owner_key=evidence.identity_key,
                performer_key=evidence.identity_key,
            ))
    return deliveries


def blueprint_voice_identity_issues(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[BlueprintSemanticIssue]:
    """Validate quoted-unit delivery and its exact performer identity."""
    facts = source_facts(source_text)
    facts_by_key = {fact.source_unit_key: fact for fact in facts}
    quoted_by_source: defaultdict[str, list[SourceFact]] = defaultdict(list)
    for fact in facts:
        if fact.projection == "quoted":
            quoted_by_source[fact.source_segment_id].append(fact)

    issues: list[BlueprintSemanticIssue] = []
    for node in blueprint.nodes:
        if node.source_semantics().projection_policy != "picture":
            if node.environment_source_unit_keys:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_environment_non_picture",
                    node_keys=[node.key],
                    source_segment_ids=list(node.source_segment_ids),
                    source_unit_keys=list(
                        node.environment_source_unit_keys
                    ),
                    message=(
                        "paratext/audit-only node 不得携带 "
                        "environment_source_unit_keys"
                    ),
                    required_resolution=(
                        "移除非画面节点的 environment 主体标记；"
                        "其 source ownership 与顺序保持不变"
                    ),
                ))
            continue
        owned_sources = set(node.source_segment_ids)
        owned_quotes = [
            fact
            for source_id in node.source_segment_ids
            for fact in quoted_by_source.get(source_id, [])
        ]
        deliveries: defaultdict[
            str,
            list[NarrativeSourceUnitDelivery],
        ] = defaultdict(list)
        for delivery in effective_source_unit_deliveries(node):
            fact = facts_by_key.get(delivery.source_unit_key)
            if (
                fact is None
                or fact.projection != "quoted"
                or fact.source_segment_id not in owned_sources
            ):
                issues.append(BlueprintSemanticIssue(
                    code="source_delivery_conflict",
                    node_keys=[node.key],
                    source_segment_ids=list(node.source_segment_ids),
                    source_unit_keys=[delivery.source_unit_key],
                    message=(
                        f"{delivery.source_unit_key} 不是本节点拥有的 "
                        "quoted source unit"
                    ),
                    required_resolution=(
                        "仅为本节点实际拥有的 quoted source unit "
                        "声明交付方式"
                    ),
                ))
                continue
            deliveries[delivery.source_unit_key].append(delivery)

        claims: defaultdict[
            str,
            list[NarrativeParticipantEvidence],
        ] = defaultdict(list)
        for evidence in node.participant_evidence:
            if evidence.usage != "voice":
                continue
            effective_source_ids = list(
                evidence.source_segment_ids or node.source_segment_ids
            )
            effective_source_set = set(effective_source_ids)
            invalid_keys = [
                key
                for key in evidence.source_unit_keys
                if (
                    key not in facts_by_key
                    or facts_by_key[key].projection != "quoted"
                    or facts_by_key[key].source_segment_id
                    not in owned_sources
                    or facts_by_key[key].source_segment_id
                    not in effective_source_set
                )
            ]
            if invalid_keys:
                issues.append(BlueprintSemanticIssue(
                    code="voice_identity_conflict",
                    node_keys=[node.key],
                    source_segment_ids=list(
                        evidence.source_segment_ids
                    ),
                    source_unit_keys=list(invalid_keys),
                    message=(
                        f"{evidence.identity_key} 的 voice evidence 引用了"
                        "非本节点 quoted source unit："
                        + "、".join(invalid_keys)
                    ),
                    required_resolution=(
                        "保留节点、来源 ownership 与语义，只把 voice "
                        "evidence 绑定到本节点实际拥有的 dialogue unit"
                    ),
                ))
                continue
            segment_scoped_non_dialogue_voice = (
                bool(effective_source_ids)
                and effective_source_set.issubset(owned_sources)
                and not any(
                    quoted_by_source.get(source_id, [])
                    for source_id in effective_source_ids
                )
            )
            if (
                not evidence.source_unit_keys
                and segment_scoped_non_dialogue_voice
            ):
                # A segment-scoped offscreen voice can be valid evidence for
                # an audible action even when the source has no dialogue unit.
                continue
            target_keys = evidence.source_unit_keys
            if not target_keys:
                issues.append(BlueprintSemanticIssue(
                    code="voice_identity_conflict",
                    node_keys=[node.key],
                    source_segment_ids=list(evidence.source_segment_ids),
                    message=(
                        f"{evidence.identity_key} 的 voice evidence 缺少 "
                        "source_unit_keys，没有绑定本节点 "
                        "quoted source unit"
                    ),
                    required_resolution=(
                        "保留节点、来源 ownership 与语义，为该 voice evidence "
                        "填写精确 source_unit_keys"
                    ),
                ))
                continue
            for key in dict.fromkeys(target_keys):
                claims[key].append(evidence)

        participant_keys = set(node.participants)
        for fact in owned_quotes:
            unit_deliveries = deliveries.get(fact.source_unit_key, [])
            if not unit_deliveries:
                issues.append(BlueprintSemanticIssue(
                    code="source_delivery_missing",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=(
                        f"{fact.source_unit_key} 缺少 quoted source unit "
                        "交付决策"
                    ),
                    required_resolution=(
                        "根据来源语义显式选择声音、书面文字、声音效果或"
                        "非口播引用；引号本身不得自动等同对白"
                    ),
                ))
                continue
            if len(unit_deliveries) != 1:
                issues.append(BlueprintSemanticIssue(
                    code="source_delivery_conflict",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=(
                        f"{fact.source_unit_key} 同时声明多个交付决策"
                    ),
                    required_resolution=(
                        "每个 quoted source unit 只保留一个交付决策"
                    ),
                ))
                continue
            delivery = unit_deliveries[0]
            referenced_delivery_identities = {
                delivery.performer_key
            } if delivery.performer_key else set()
            unknown_delivery_identities = (
                referenced_delivery_identities - participant_keys
            )
            if unknown_delivery_identities:
                issues.append(BlueprintSemanticIssue(
                    code="source_delivery_identity_conflict",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    source_unit_keys=[fact.source_unit_key],
                    message=(
                        f"{fact.source_unit_key} 的表演身份未列入 "
                        f"participants：{sorted(unknown_delivery_identities)}"
                    ),
                    required_resolution=(
                        "声音 delivery 的 performer_key 必须精确引用"
                        "本节点参与者；content_owner_key 可以是文字或物件归属"
                    ),
                ))

            unit_claims = claims.get(fact.source_unit_key, [])
            identities = {
                evidence.identity_key
                for evidence in unit_claims
                if evidence.identity_key
            }
            if delivery.mode not in AUDIBLE_SOURCE_DELIVERY_MODES:
                if unit_claims:
                    issues.append(BlueprintSemanticIssue(
                        code="voice_identity_conflict",
                        node_keys=[node.key],
                        source_segment_ids=[fact.source_segment_id],
                        source_unit_keys=[fact.source_unit_key],
                        message=(
                            f"{fact.source_unit_key} 的 delivery mode="
                            f"{delivery.mode}，不得声明 voice performer"
                        ),
                        required_resolution=(
                            "移除非声音交付上的 voice evidence，保留内容归属"
                        ),
                    ))
                continue
            if not unit_claims:
                code = "voice_identity_missing"
                message = (
                    f"{fact.source_unit_key} 缺少结构化 voice performer identity evidence"
                )
            elif len(unit_claims) > 1 and len(identities) > 1:
                code = "voice_identity_ambiguous"
                message = (
                    f"{fact.source_unit_key} 同时声明多个 voice identity："
                    + "、".join(sorted(identities))
                )
            elif len(unit_claims) != 1 or len(identities) != 1:
                code = "voice_identity_conflict"
                message = (
                    f"{fact.source_unit_key} 必须恰有一个非空 voice identity evidence"
                )
            elif next(iter(identities)) != delivery.performer_key:
                code = "voice_identity_conflict"
                message = (
                    f"{fact.source_unit_key} 的 voice identity 与 "
                    "performer_key 不一致"
                )
            else:
                continue
            issues.append(BlueprintSemanticIssue(
                code=code,
                node_keys=[node.key],
                source_segment_ids=[fact.source_segment_id],
                source_unit_keys=[fact.source_unit_key],
                message=message,
                required_resolution=(
                    "保持完整 node、source ownership、来源顺序和语义三元不变；"
                    "仅为声音交付的 quoted source unit 提供恰一个 voice evidence，"
                    "identity_key 使用人物 registry 的 typed reference"
                ),
            ))
    return issues
