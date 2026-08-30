"""Builds and cross-validates the per-scene input contracts a shard generation
call is grounded on: participant/action/state-subject evidence assembly
(``build_screenplay_scene_input_contracts``) and their structural validation
against the shard plan.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

from app.narrative_blueprint import (
    BlueprintScenePlan,
    NarrativeBlueprint,
    NarrativeNode,
)
from app.screenplay_ir import IR_VERSION
from app.source_excerpt import index_source_segments
from typing import Any

from .identity_registry import (
    ScreenplaySceneShardError,
    _identity_aliases,
    _source_ownership_hash,
)
from .identity_scaffold import (
    _compile_unit_identity_scaffold,
    _contract_identity_scaffold_hash,
    _structural_slot,
)
from .models import (
    ScreenplayActionParticipantDeliveryContract,
    ScreenplaySceneActionEvidence,
    ScreenplaySceneActionParticipantEvidence,
    ScreenplaySceneInputContract,
    ScreenplaySceneParticipantBinding,
    ScreenplaySceneShardPlan,
    ScreenplaySceneSourceSegment,
    ScreenplaySceneStateSubjectAssignment,
)


def build_screenplay_scene_input_contracts(
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: list[BlueprintScenePlan],
    source_by_id: dict[str, str],
    identity_registry: list[dict[str, Any]],
    blueprint_nodes: list[NarrativeNode] | None = None,
) -> list[ScreenplaySceneInputContract]:
    """Bind source text and canonical action evidence before scene writing."""
    errors: list[str] = []
    scene_keys = [scene_plan.key for scene_plan in scene_plans]
    if scene_keys != plan.scene_plan_keys:
        errors.append(
            "逐场输入合同与 shard plan 场次不一致："
            f"expected={plan.scene_plan_keys}, actual={scene_keys}"
        )

    projected_source_ids = [
        source_id
        for source_id, owner_scene_key in plan.source_scene_owners.items()
        if owner_scene_key in scene_keys
    ]
    if projected_source_ids != plan.source_segment_ids:
        errors.append(
            "逐场输入合同的唯一 SRC 投影与 shard plan 不一致："
            f"expected={plan.source_segment_ids}, actual={projected_source_ids}"
        )

    aliases = _identity_aliases(identity_registry)
    nodes_by_key = {
        node.key: node for node in (blueprint_nodes or [])
    }
    contracts: list[ScreenplaySceneInputContract] = []
    for scene_plan in scene_plans:
        owned_source_ids = [
            source_id
            for source_id, owner_scene_key
            in plan.source_scene_owners.items()
            if owner_scene_key == scene_plan.key
        ]
        if scene_plan.source_segment_ids != owned_source_ids:
            conflicting_source_ids = [
                source_id
                for source_id in scene_plan.source_segment_ids
                if plan.source_scene_owners.get(source_id)
                != scene_plan.key
            ]
            if conflicting_source_ids:
                errors.extend(
                    f"{source_id} 唯一归属 "
                    f"{plan.source_scene_owners.get(source_id) or '未定义'}，"
                    f"不得由 {scene_plan.key} 消费"
                    for source_id in conflicting_source_ids
                )
            else:
                errors.append(
                    f"{scene_plan.key} source_segment_ids 与唯一 owner 投影不一致"
                )
        missing_source_ids = [
            source_id
            for source_id in owned_source_ids
            if source_id not in source_by_id
        ]
        if missing_source_ids:
            errors.append(
                f"{scene_plan.key} 输入合同缺少来源正文："
                + ",".join(missing_source_ids)
            )
        unresolved_participants = [
            participant
            for participant in scene_plan.participant_keys
            if participant not in aliases
        ]
        if unresolved_participants:
            errors.append(
                f"{scene_plan.key} Blueprint participant 未冻结："
                + ",".join(unresolved_participants)
            )
        action_evidence: list[ScreenplaySceneActionEvidence] = []
        for node_key in scene_plan.node_keys:
            node = nodes_by_key.get(node_key)
            if node is None:
                if blueprint_nodes is not None:
                    errors.append(
                        f"{scene_plan.key} identity scaffold 缺少 Blueprint "
                        f"node：{node_key}"
                    )
                continue
            participants: list[
                ScreenplaySceneActionParticipantEvidence
            ] = []
            for evidence in node.participant_evidence:
                if evidence.usage == "mentioned":
                    # Content ownership is preserved in the Blueprint delivery
                    # contract, but it is not an executable scene participant.
                    continue
                identity_key = aliases.get(evidence.identity_key, "")
                if not identity_key:
                    errors.append(
                        f"{scene_plan.key} action evidence identity 未冻结："
                        f"{evidence.identity_key}"
                    )
                    continue
                evidence_source_ids = list(
                    evidence.source_segment_ids
                    or node.source_segment_ids
                )
                escaped_source_ids = (
                    set(evidence_source_ids) - set(owned_source_ids)
                )
                if escaped_source_ids:
                    errors.append(
                        f"{scene_plan.key} action evidence 引用非本场来源："
                        f"{sorted(escaped_source_ids)}"
                    )
                    continue
                participants.append(
                    ScreenplaySceneActionParticipantEvidence(
                        identity_key=identity_key,
                        source_segment_ids=evidence_source_ids,
                        source_unit_keys=list(evidence.source_unit_keys),
                        usage=evidence.usage,
                        perception_channels=(
                            ["audible"]
                            if evidence.usage == "voice"
                            else []
                        ),
                    )
                )
            decision_actor_key = None
            if node.decision is not None:
                decision_actor_key = aliases.get(
                    node.decision.actor_key,
                    "",
                )
                if not decision_actor_key:
                    errors.append(
                        f"{scene_plan.key} decision actor 未冻结："
                        f"{node.decision.actor_key}"
                    )
            state_subject_assignments: list[
                ScreenplaySceneStateSubjectAssignment
            ] = []
            for assignment in node.state_subject_assignments:
                unresolved_assignment_identities = [
                    identity_key
                    for identity_key in assignment.identity_keys
                    if identity_key not in aliases
                ]
                if unresolved_assignment_identities:
                    errors.append(
                        f"{scene_plan.key} joint state subject identity 未冻结："
                        + ",".join(unresolved_assignment_identities)
                    )
                    continue
                state_subject_assignments.append(
                    ScreenplaySceneStateSubjectAssignment(
                        source_unit_key=assignment.source_unit_key,
                        mode=assignment.mode,
                        identity_keys=[
                            aliases[identity_key]
                            for identity_key in assignment.identity_keys
                        ],
                    )
                )
            action_evidence.append(ScreenplaySceneActionEvidence(
                node_key=node.key,
                source_segment_ids=list(node.source_segment_ids),
                participants=participants,
                state_subject_assignments=state_subject_assignments,
                decision_actor_key=decision_actor_key or None,
                environment_source_unit_keys=list(
                    node.environment_source_unit_keys
                ),
            ))

        contract = ScreenplaySceneInputContract(
            scene_plan_key=scene_plan.key,
            node_keys=list(scene_plan.node_keys),
            source_segment_ids=owned_source_ids,
            source_semantics={
                source_id: scene_plan.source_semantics[source_id]
                for source_id in owned_source_ids
            },
            source_segments=[
                ScreenplaySceneSourceSegment(
                    source_segment_id=source_id,
                    text=source_by_id[source_id],
                )
                for source_id in owned_source_ids
                if source_id in source_by_id
            ],
            participant_bindings=[
                ScreenplaySceneParticipantBinding(
                    blueprint_key=participant,
                    identity_key=aliases.get(participant, ""),
                )
                for participant in scene_plan.participant_keys
            ],
            source_scene_owners=dict(plan.source_scene_owners),
            derived_relations=[
                relation.model_copy(deep=True)
                for relation in plan.derived_relations
                if relation.target_scene_plan_key == scene_plan.key
            ],
            action_participant_delivery_contract=(
                ScreenplayActionParticipantDeliveryContract()
            ),
            action_evidence=action_evidence,
            unit_slots=[],
            source_ownership_hash=plan.source_ownership_hash,
        )
        scene_slots = [
            slot
            for slot in plan.unit_slots
            if slot.scene_key == scene_plan.key
        ]
        if not scene_slots:
            errors.append(
                f"{scene_plan.key} generation scaffold 缺少 unit slot"
            )
        for slot in scene_slots:
            invalid_slot_sources = [
                source_id
                for source_id in slot.source_segment_ids
                if (
                    source_id not in owned_source_ids
                    or plan.source_scene_owners.get(source_id)
                    != scene_plan.key
                )
            ]
            if invalid_slot_sources:
                errors.append(
                    f"{slot.unit_key} source owner 不匹配："
                    + ",".join(invalid_slot_sources)
                )
                continue
            compiled_slot, slot_errors = (
                _compile_unit_identity_scaffold(
                    slot,
                    contract=contract,
                )
            )
            errors.extend(slot_errors)
            contract.unit_slots.append(compiled_slot)
        contract.identity_scaffold_hash = (
            _contract_identity_scaffold_hash(contract)
        )
        contracts.append(contract)
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)
    return contracts


def build_screenplay_scene_input_contract_set(
    *,
    plans: list[ScreenplaySceneShardPlan],
    blueprint: NarrativeBlueprint,
    source_text: str,
    identity_registry: list[dict[str, Any]],
) -> dict[str, list[ScreenplaySceneInputContract]]:
    """Build the scene-owned contract once for generation, retry, and merge."""
    expected_ownership_hash = _source_ownership_hash(blueprint)
    scene_plan_map = {scene_plan.key: scene_plan for scene_plan in blueprint.scene_plans}
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    contracts: dict[str, list[ScreenplaySceneInputContract]] = {}
    for plan in plans:
        if (
            plan.source_scene_owners != blueprint.source_scene_owners
            or plan.source_ownership_hash != expected_ownership_hash
        ):
            raise ScreenplaySceneShardError(
                plan.shard_id,
                ["shard plan 的 source owner 合同与 Blueprint 不一致"],
            )
        missing_scene_keys = [
            scene_key for scene_key in plan.scene_plan_keys
            if scene_key not in scene_plan_map
        ]
        if missing_scene_keys:
            raise ScreenplaySceneShardError(
                plan.shard_id,
                ["逐场输入合同缺少 Blueprint scene plan：" + ",".join(missing_scene_keys)],
            )
        contracts[plan.shard_id] = build_screenplay_scene_input_contracts(
            plan=plan,
            scene_plans=[
                scene_plan_map[scene_key] for scene_key in plan.scene_plan_keys
            ],
            source_by_id=source_by_id,
            identity_registry=identity_registry,
            blueprint_nodes=list(blueprint.nodes),
        )
    return contracts


def _validate_scene_input_contracts(
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_keys: set[str],
) -> tuple[dict[str, ScreenplaySceneInputContract], list[str]]:
    errors: list[str] = []
    expected_plan_source_ids = [
        source_id
        for source_id, owner_scene_key in plan.source_scene_owners.items()
        if owner_scene_key in plan.scene_plan_keys
    ]
    if plan.source_segment_ids != expected_plan_source_ids:
        errors.append(
            "shard plan source_segment_ids 与唯一 owner 投影不一致"
        )
    unit_keys = [slot.unit_key for slot in plan.unit_slots]
    event_keys = [slot.event_key for slot in plan.unit_slots]
    if len(set(unit_keys)) != len(unit_keys):
        errors.append("shard plan unit_key 必须唯一")
    if len(set(event_keys)) != len(event_keys):
        errors.append("shard plan event_key 必须唯一")
    if [slot.unit_order for slot in plan.unit_slots] != list(
        range(1, len(plan.unit_slots) + 1)
    ):
        errors.append("shard plan unit_order 必须连续且按播放顺序递增")
    invalid_slot_owners = [
        slot.unit_key
        for slot in plan.unit_slots
        if (
            slot.scene_key not in plan.scene_plan_keys
            or any(
                plan.source_scene_owners.get(source_id)
                != slot.scene_key
                for source_id in slot.source_segment_ids
            )
        )
    ]
    if invalid_slot_owners:
        errors.append(
            "shard plan slot 来源归属不匹配："
            + ",".join(invalid_slot_owners)
        )
    invalid_relations = [
        relation.relation_key
        for relation in plan.derived_relations
        if (
            relation.target_scene_plan_key not in plan.scene_plan_keys
            or relation.source_scene_plan_key
            == relation.target_scene_plan_key
        )
    ]
    if invalid_relations:
        errors.append(
            "shard plan 含无效跨场派生关系："
            + ",".join(invalid_relations)
        )
    actual_scene_keys = [
        contract.scene_plan_key for contract in scene_input_contracts
    ]
    if actual_scene_keys != plan.scene_plan_keys:
        errors.append(
            "逐场参与者合同与 shard plan 不一致："
            f"expected={plan.scene_plan_keys}, actual={actual_scene_keys}"
        )
    contracts_by_scene: dict[str, ScreenplaySceneInputContract] = {}
    for contract in scene_input_contracts:
        if contract.scene_plan_key in contracts_by_scene:
            errors.append(
                "逐场参与者合同 scene_plan_key 必须唯一："
                + contract.scene_plan_key
            )
            continue
        contracts_by_scene[contract.scene_plan_key] = contract
    for scene_key in plan.scene_plan_keys:
        expected_scene = scene_plans.get(scene_key)
        contract = contracts_by_scene.get(scene_key)
        if expected_scene is None:
            errors.append(f"逐场参与者合同引用未知 scene：{scene_key}")
            continue
        if contract is None:
            errors.append(f"{scene_key} 缺少逐场参与者合同")
            continue
        if contract.node_keys != expected_scene.node_keys:
            errors.append(f"{scene_key} 逐场参与者合同 node_keys 与 Blueprint 不一致")
        expected_source_ids = [
            source_id
            for source_id, owner_scene_key
            in plan.source_scene_owners.items()
            if owner_scene_key == scene_key
        ]
        if expected_scene.source_segment_ids != expected_source_ids:
            conflicting_source_ids = [
                source_id
                for source_id in expected_scene.source_segment_ids
                if plan.source_scene_owners.get(source_id) != scene_key
            ]
            if conflicting_source_ids:
                errors.extend(
                    f"{source_id} 唯一归属 "
                    f"{plan.source_scene_owners.get(source_id) or '未定义'}，"
                    f"不得由 {scene_key} 消费"
                    for source_id in conflicting_source_ids
                )
            else:
                errors.append(
                    f"{scene_key} Blueprint source_segment_ids "
                    "与唯一 owner 投影不一致"
                )
        if contract.source_segment_ids != expected_source_ids:
            errors.append(
                f"{scene_key} 逐场参与者合同 source_segment_ids "
                "与唯一 owner 投影不一致"
            )
        expected_source_semantics = {
            source_id: expected_scene.source_semantics[source_id]
            for source_id in expected_source_ids
        }
        if contract.source_semantics != expected_source_semantics:
            errors.append(
                f"{scene_key} 逐场来源语义与 Blueprint 不一致"
            )
        contract_source_ids = [
            segment.source_segment_id
            for segment in contract.source_segments
        ]
        if contract_source_ids != expected_source_ids:
            errors.append(
                f"{scene_key} 逐场来源正文与唯一 owner 投影不一致"
            )
        if contract.source_scene_owners != plan.source_scene_owners:
            errors.append(
                f"{scene_key} 逐场 source owner 合同与 shard plan 不一致"
            )
        if contract.source_ownership_hash != plan.source_ownership_hash:
            errors.append(
                f"{scene_key} source_ownership_hash 与 shard plan 不一致"
            )
        expected_relations = [
            relation.model_dump(mode="json")
            for relation in plan.derived_relations
            if relation.target_scene_plan_key == scene_key
        ]
        actual_relations = [
            relation.model_dump(mode="json")
            for relation in contract.derived_relations
        ]
        if actual_relations != expected_relations:
            errors.append(
                f"{scene_key} 跨场派生关系与 shard plan 不一致"
            )
        expected_delivery_contract = (
            ScreenplayActionParticipantDeliveryContract()
        )
        if (
            contract.action_participant_delivery_contract
            != expected_delivery_contract
        ):
            errors.append(
                f"{scene_key} action participant delivery 合同与 "
                f"{IR_VERSION} 不一致"
            )
        expected_unit_slots = [
            slot for slot in plan.unit_slots
            if slot.scene_key == scene_key
        ]
        actual_structural_slots = [
            _structural_slot(slot)
            for slot in contract.unit_slots
        ]
        if actual_structural_slots != expected_unit_slots:
            errors.append(
                f"{scene_key} unit slot 与 shard plan 不一致"
            )
        for actual_slot in contract.unit_slots:
            planned_slot = next(
                (
                    slot for slot in expected_unit_slots
                    if slot.unit_key == actual_slot.unit_key
                ),
                None,
            )
            if planned_slot is None:
                continue
            expected_slot, slot_errors = (
                _compile_unit_identity_scaffold(
                    planned_slot,
                    contract=contract,
                )
            )
            errors.extend(slot_errors)
            if actual_slot != expected_slot:
                errors.append(
                    f"{actual_slot.unit_key} identity scaffold drift"
                )
        expected_scaffold_hash = _contract_identity_scaffold_hash(
            contract
        )
        if contract.identity_scaffold_hash != expected_scaffold_hash:
            errors.append(
                f"{scene_key} identity_scaffold_hash 与 unit scaffold "
                "不一致"
            )
        if contract.action_evidence:
            invalid_evidence_identities = [
                participant.identity_key
                for action in contract.action_evidence
                for participant in action.participants
                if participant.identity_key not in identity_keys
            ]
            if invalid_evidence_identities:
                errors.append(
                    f"{scene_key} action evidence 含未冻结 identity_key："
                    + ",".join(invalid_evidence_identities)
                )
            escaped_evidence_sources = sorted({
                source_id
                for action in contract.action_evidence
                for source_id in action.source_segment_ids
                if source_id not in expected_source_ids
            })
            if escaped_evidence_sources:
                errors.append(
                    f"{scene_key} action evidence 引用非本场来源："
                    f"{escaped_evidence_sources}"
                )
        expected_blueprint_keys = list(expected_scene.participant_keys)
        actual_blueprint_keys = [
            binding.blueprint_key for binding in contract.participant_bindings
        ]
        if actual_blueprint_keys != expected_blueprint_keys:
            errors.append(
                f"{scene_key} 逐场参与者合同 participant_bindings 与 Blueprint 不一致"
            )
        invalid_bindings = [
            binding.identity_key
            for binding in contract.participant_bindings
            if (
                not binding.identity_key
                or binding.identity_key not in identity_keys
            )
        ]
        if invalid_bindings:
            errors.append(
                f"{scene_key} 逐场参与者合同含未冻结 identity_key："
                + ",".join(invalid_bindings)
            )
    return contracts_by_scene, errors


_GENERIC_STORY_FUNCTION_LABELS = {
    "setup",
    "development",
    "complication",
    "turn",
    "climax",
    "resolution",
}
