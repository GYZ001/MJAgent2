"""Derives scene plans from a validated blueprint and validates the resulting scene partition."""
from __future__ import annotations

from collections import defaultdict

from .models_core import (
    BlueprintSceneDerivation,
    BlueprintScenePlan,
    BlueprintSourceAuditAnnotation,
    BlueprintSourceOccurrenceError,
    BlueprintSourceOwnershipError,
    BlueprintSourceSemantics,
    NarrativeBlueprint,
    NarrativeNode,
)


def derive_blueprint_scene_plans(
    blueprint: NarrativeBlueprint,
) -> list[BlueprintScenePlan]:
    def operational_participants(node: NarrativeNode) -> list[str]:
        if not node.participant_evidence and not node.state_subject_assignments:
            return [
                participant
                for participant in node.participants
                if participant
            ]
        return list(dict.fromkeys([
            evidence.identity_key
            for evidence in node.participant_evidence
            if (
                evidence.identity_key
                and evidence.usage
                in {"visible", "voice", "state_subject"}
            )
        ] + [
            identity_key
            for assignment in node.state_subject_assignments
            for identity_key in assignment.identity_keys
            if identity_key
        ]))

    occurrence_nodes: defaultdict[str, list[str]] = defaultdict(list)
    occurrence_partitions: defaultdict[str, set[str]] = defaultdict(set)
    for node in blueprint.nodes:
        projection_policy = node.source_semantics().projection_policy
        for source_id in node.source_segment_ids:
            occurrence_nodes[source_id].append(node.key)
            occurrence_partitions[source_id].add(projection_policy)
    partition_conflicts = {
        source_id
        for source_id, partitions in occurrence_partitions.items()
        if len(partitions) > 1
    }
    if partition_conflicts:
        raise BlueprintSourceOccurrenceError(
            {
                source_id: occurrence_nodes[source_id]
                for source_id in partition_conflicts
            },
            partition_conflicts=partition_conflicts,
        )

    source_semantics: dict[str, BlueprintSourceSemantics] = {}
    for node in blueprint.nodes:
        semantics = node.source_semantics()
        for source_id in node.source_segment_ids:
            existing = source_semantics.get(source_id)
            if existing is not None and existing != semantics:
                raise ValueError(
                    "[BLUEPRINT_SOURCE_SEMANTIC_CONFLICT] "
                    f"{source_id} 被赋予互相冲突的叙事语义"
                )
            source_semantics[source_id] = semantics

    picture_nodes = [
        node
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "picture"
    ]
    groups: list[list[NarrativeNode]] = []
    for node in picture_nodes:
        previous = groups[-1][-1] if groups else None
        current_group = groups[-1] if groups else []
        starts_scene = (
            previous is None
            or node.scene_boundary_before
            or node.temporal_domain_key != previous.temporal_domain_key
            or node.location_key != previous.location_key
            or node.time_relation in {
                "elapsed",
                "jump",
                "flashback_enter",
                "flashback_exit",
                "montage",
            }
            or sum(item.dramatic_load for item in current_group)
            + node.dramatic_load > 3
            or len({
                source_id
                for item in current_group
                for source_id in item.source_segment_ids
            } | set(node.source_segment_ids)) > 8
        )
        if starts_scene:
            groups.append([node])
        else:
            groups[-1].append(node)

    plans: list[BlueprintScenePlan] = []
    for index, nodes in enumerate(groups, start=1):
        first = nodes[0]
        previous_exit_state = (
            groups[index - 2][-1].exit_state
            or groups[index - 2][-1].summary
            if index > 1
            else ""
        )
        plans.append(BlueprintScenePlan(
            key=f"bp-sc{index:03d}",
            node_keys=[node.key for node in nodes],
            source_segment_ids=[],
            source_semantics={},
            temporal_domain_key=first.temporal_domain_key,
            time_label=first.time_label,
            location_key=first.location_key,
            location_label=first.location_label,
            transition_cue=first.transition_cue,
            previous_scene_exit_state=previous_exit_state,
            opening_image=(
                first.opening_image
                or first.transition_cue
                or first.summary
            ),
            exit_state=nodes[-1].exit_state or nodes[-1].summary,
            dramatic_load=sum(node.dramatic_load for node in nodes),
            agency_contracts=[
                {
                    "node_key": node.key,
                    "actor_key": node.decision.actor_key,
                    "agency_mode": node.decision.agency_mode,
                    "narrative_attribution": (
                        node.decision.narrative_attribution
                    ),
                    "constraint_fact_key": (
                        node.decision.constraint_fact_key
                    ),
                }
                for node in nodes
                if node.decision is not None
            ],
            participant_keys=list(dict.fromkeys(
                participant
                for node in nodes
                for participant in operational_participants(node)
            )),
            scene_heading=(
                f"【场{index}】{first.time_label} / {first.location_label}"
            ),
        ))

    node_scene_owners: dict[str, str] = {}
    source_scene_owners: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    for plan, nodes in zip(plans, groups, strict=True):
        for node in nodes:
            node_scene_owners[node.key] = plan.key
            for source_id in node.source_segment_ids:
                current_owner = source_scene_owners.get(source_id)
                if current_owner is None:
                    source_scene_owners[source_id] = plan.key
                    continue
                if current_owner == plan.key:
                    continue
                scene_keys = conflicts.setdefault(
                    source_id,
                    [current_owner],
                )
                if plan.key not in scene_keys:
                    scene_keys.append(plan.key)
    if conflicts:
        raise BlueprintSourceOwnershipError(conflicts)

    occurrence_owners: defaultdict[str, list[str]] = defaultdict(list)
    for node in blueprint.nodes:
        for source_id in node.source_segment_ids:
            occurrence_owners[source_id].append(node.key)
    occurrence_duplicates = {
        source_id: node_keys
        for source_id, node_keys in occurrence_owners.items()
        if len(node_keys) > 1
    }
    if occurrence_duplicates:
        raise BlueprintSourceOccurrenceError(occurrence_duplicates)

    for plan in plans:
        plan.source_segment_ids = [
            source_id
            for source_id, owner_scene_key in source_scene_owners.items()
            if owner_scene_key == plan.key
        ]
        plan.source_semantics = {
            source_id: source_semantics[source_id]
            for source_id in plan.source_segment_ids
        }

    node_map = {node.key: node for node in blueprint.nodes}
    derivations: list[BlueprintSceneDerivation] = []

    def append_derivation(
        relation_type: str,
        source_node_key: str,
        target_node_key: str,
        *,
        reference_key: str = "",
        summary: str = "",
    ) -> None:
        source_scene_key = node_scene_owners.get(source_node_key)
        target_scene_key = node_scene_owners.get(target_node_key)
        if (
            not source_scene_key
            or not target_scene_key
            or source_scene_key == target_scene_key
        ):
            return
        derivations.append(BlueprintSceneDerivation(
            relation_key=f"BD{len(derivations) + 1:04d}",
            relation_type=relation_type,
            source_scene_plan_key=source_scene_key,
            target_scene_plan_key=target_scene_key,
            source_node_key=source_node_key,
            target_node_key=target_node_key,
            reference_key=reference_key,
            summary=summary,
        ))

    for previous_nodes, current_nodes in zip(groups, groups[1:]):
        append_derivation(
            "scene_transition",
            previous_nodes[-1].key,
            current_nodes[0].key,
            summary=(
                current_nodes[0].transition_cue
                or current_nodes[0].opening_image
                or current_nodes[0].summary
            ),
        )

    fact_owner_nodes = {
        change.fact_key: node.key
        for node in blueprint.nodes
        for change in node.state_changes
        if change.fact_key
    }
    for target_node in blueprint.nodes:
        for requirement in target_node.state_requirements:
            source_node_key = fact_owner_nodes.get(
                requirement.required_fact_key,
            )
            if source_node_key:
                append_derivation(
                    "state_requirement",
                    source_node_key,
                    target_node.key,
                    reference_key=requirement.required_fact_key,
                    summary=requirement.reason,
                )
        if target_node.decision is None:
            continue
        for source_node_key in target_node.decision.setup_node_keys:
            if source_node_key in node_map:
                append_derivation(
                    "decision_setup",
                    source_node_key,
                    target_node.key,
                    summary=target_node.decision.pressure,
                )
        for source_node_key in (
            target_node.decision.constraint_release_node_keys
        ):
            if source_node_key in node_map:
                append_derivation(
                    "constraint_release",
                    source_node_key,
                    target_node.key,
                    reference_key=target_node.decision.constraint_fact_key,
                    summary=target_node.decision.agency_change_reason,
                )

    blueprint.scene_plans = plans
    blueprint.source_scene_owners = source_scene_owners
    blueprint.source_semantics = source_semantics
    blueprint.source_audit_annotations = [
        BlueprintSourceAuditAnnotation(
            node_key=node.key,
            source_segment_ids=list(node.source_segment_ids),
        )
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "audit_only"
    ]
    blueprint.scene_derivations = derivations
    return plans


def validate_blueprint_scene_partition(
    blueprint: NarrativeBlueprint,
    plans: list[BlueprintScenePlan] | None = None,
) -> list[str]:
    """Validate the exact ordered picture-node partition."""
    current_plans = blueprint.scene_plans if plans is None else plans
    picture_node_keys = [
        node.key
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "picture"
    ]
    audit_node_keys = {
        node.key
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "audit_only"
    }
    planned_node_keys = [
        node_key
        for plan in current_plans
        for node_key in plan.node_keys
    ]
    errors: list[str] = []
    leaked_audit_keys = [
        node_key
        for node_key in planned_node_keys
        if node_key in audit_node_keys
    ]
    if leaked_audit_keys:
        errors.append(
            "[BLUEPRINT_AUDIT_NODE_IN_SCENE] audit_only 节点不得进入 scene plan："
            + "、".join(leaked_audit_keys)
        )
    if planned_node_keys != picture_node_keys:
        errors.append(
            "[BLUEPRINT_SCENE_PARTITION_INVALID] picture 节点必须被 scene plans "
            "精确覆盖并保持相对顺序，禁止重复或遗漏"
        )

    audit_source_ids = [
        source_id
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "audit_only"
        for source_id in node.source_segment_ids
    ]
    annotated_source_ids = [
        source_id
        for annotation in blueprint.source_audit_annotations
        for source_id in annotation.source_segment_ids
    ]
    annotated_node_keys = [
        annotation.node_key
        for annotation in blueprint.source_audit_annotations
    ]
    expected_audit_node_keys = [
        node.key
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "audit_only"
    ]
    if (
        annotated_source_ids != audit_source_ids
        or annotated_node_keys != expected_audit_node_keys
    ):
        errors.append(
            "[BLUEPRINT_AUDIT_COVERAGE_INVALID] source_audit_annotations "
            "必须精确覆盖 audit_only timeline nodes 与来源"
        )
    picture_source_ids = {
        source_id
        for plan in current_plans
        for source_id in plan.source_segment_ids
    }
    leaked_audit_sources = [
        source_id
        for source_id in audit_source_ids
        if source_id in picture_source_ids
    ]
    if leaked_audit_sources:
        errors.append(
            "[BLUEPRINT_AUDIT_SOURCE_IN_SCENE] audit_only 来源不得进入 scene plan："
            + "、".join(leaked_audit_sources)
        )
    return list(dict.fromkeys(errors))
