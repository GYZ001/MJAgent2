"""The top-level NarrativeBlueprint structural validator: validate_narrative_blueprint."""
from __future__ import annotations

import re
from collections import defaultdict

from app.source_excerpt import index_source_segments, structural_front_matter_ids

from .constants import BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
from .models_core import (
    BlueprintSourceOccurrenceError,
    BlueprintSourceOwnershipError,
    BlueprintStateChange,
    NarrativeBlueprint,
    blueprint_source_occurrence_issues,
)
from .scene_plans import derive_blueprint_scene_plans, validate_blueprint_scene_partition
from .state_subject_issues import blueprint_state_subject_issues
from .voice_identity_issues import blueprint_voice_identity_issues


def validate_narrative_blueprint(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[str]:
    errors: list[str] = []
    segments = index_source_segments(source_text)
    source_order = {
        segment.segment_id: index
        for index, segment in enumerate(segments)
    }
    expected_source_ids = {
        segment.segment_id for segment in segments
    } - structural_front_matter_ids(segments)

    if not blueprint.nodes:
        return ["[BLUEPRINT_EMPTY] 叙事蓝图没有任何时间线节点"]

    errors.extend(
        (
            f"[BLUEPRINT_{issue.code.upper()}] "
            f"{'、'.join(issue.node_keys)} "
            f"{'、'.join(issue.source_segment_ids)}：{issue.message}；"
            f"必须：{issue.required_resolution}"
        )
        for issue in blueprint_voice_identity_issues(
            blueprint,
            source_text,
        )
    )
    errors.extend(
        (
            f"[BLUEPRINT_{issue.code.upper()}] "
            f"{'、'.join(issue.node_keys)} "
            f"{'、'.join(issue.source_segment_ids)}：{issue.message}；"
            f"必须：{issue.required_resolution}"
        )
        for issue in blueprint_state_subject_issues(
            blueprint,
            source_text,
        )
    )

    node_keys = [node.key for node in blueprint.nodes]
    if len(node_keys) != len(set(node_keys)):
        errors.append("[BLUEPRINT_NODE_KEY_DUPLICATE] 时间线节点 key 重复")

    unknown_source_ids = {
        source_id
        for node in blueprint.nodes
        for source_id in node.source_segment_ids
        if source_id not in source_order
    }
    if unknown_source_ids:
        errors.append(
            "[BLUEPRINT_SOURCE_UNKNOWN] 节点引用未知来源段："
            + "、".join(sorted(unknown_source_ids)[:20])
        )

    errors.extend(
        issue.error
        for issue in blueprint_source_occurrence_issues(blueprint.nodes)
    )

    owned_source_ids = {
        source_id
        for node in blueprint.nodes
        for source_id in node.source_segment_ids
    }
    missing_source_ids = expected_source_ids - owned_source_ids
    if missing_source_ids:
        errors.append(
            "[BLUEPRINT_SOURCE_MISSING] 时间线漏掉原文段："
            + "、".join(sorted(missing_source_ids)[:20])
        )

    first_owner_positions: dict[str, int] = {}
    for node_position, node in enumerate(blueprint.nodes):
        if not node.source_segment_ids:
            errors.append(
                f"[BLUEPRINT_NODE_UNGROUNDED] {node.key} 没有来源段"
            )
            continue
        if (
            len(node.source_segment_ids)
            > BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
        ):
            errors.append(
                f"[BLUEPRINT_NODE_OVERBROAD] {node.key} 合并了"
                f"{len(node.source_segment_ids)} 个来源段"
            )
        positions = [
            source_order[source_id]
            for source_id in node.source_segment_ids
            if source_id in source_order
        ]
        if positions != sorted(set(positions)):
            errors.append(
                f"[BLUEPRINT_SOURCE_ORDER] {node.key} 来源顺序错误或重复"
            )
        if positions and positions[-1] - positions[0] + 1 != len(positions):
            errors.append(
                f"[BLUEPRINT_SOURCE_DISCONTIGUOUS] {node.key} 合并非连续来源"
            )
        for source_id in node.source_segment_ids:
            first_owner_positions.setdefault(source_id, node_position)
        if not node.temporal_domain_key.strip() or not node.time_label.strip():
            errors.append(
                f"[BLUEPRINT_TIME_MISSING] {node.key} 缺少时间域或时间标签"
            )
        if not node.location_key.strip() or not node.location_label.strip():
            errors.append(
                f"[BLUEPRINT_LOCATION_MISSING] {node.key} 缺少单一地点"
            )
        elif re.search(
            r"[、+/]|内外",
            node.location_label,
        ):
            errors.append(
                f"[BLUEPRINT_LOCATION_COMPOSITE] {node.key} 把多个空间"
                f"合并为一个节点：{node.location_label}"
            )
        if (
            node.adaptation_kind == "logic_bridge"
            and len(node.bridge_rationale.strip()) < 8
        ):
            errors.append(
                f"[BLUEPRINT_BRIDGE_RATIONALE_MISSING] {node.key} 的"
                "逻辑补桥没有说明必要性和不改变原文结果的依据"
            )
        expected_semantics = (
            ("causal", "standalone")
            if node.narrative_layer == "story"
            else ("connective", "exclude_from_spine")
        )
        if (
            node.event_priority,
            node.render_policy,
        ) != expected_semantics:
            errors.append(
                f"[BLUEPRINT_NODE_SEMANTICS_INVALID] {node.key} 的"
                "叙事层、事件优先级与渲染策略不一致"
            )

    expected_positions = [
        first_owner_positions[source_id]
        for source_id in sorted(
            expected_source_ids,
            key=lambda source_id: source_order[source_id],
        )
        if source_id in first_owner_positions
    ]
    if expected_positions != sorted(expected_positions):
        ordered_source_ids = [
            source_id
            for source_id in sorted(
                expected_source_ids,
                key=lambda source_id: source_order[source_id],
            )
            if source_id in first_owner_positions
        ]
        inversion = next(
            (
                (previous_source_id, source_id)
                for previous_source_id, source_id in zip(
                    ordered_source_ids,
                    ordered_source_ids[1:],
                )
                if (
                    first_owner_positions[source_id]
                    < first_owner_positions[previous_source_id]
                )
            ),
            None,
        )
        owner_node_keys = {
            source_id: blueprint.nodes[position].key
            for source_id, position in first_owner_positions.items()
        }
        detail = (
            f"：{inversion[0]}@{owner_node_keys[inversion[0]]} 与 "
            f"{inversion[1]}@{owner_node_keys[inversion[1]]}"
            if inversion else ""
        )
        errors.append(
            "[BLUEPRINT_FIRST_CONSUMPTION_ORDER] 来源首次消费顺序违背原文"
            + detail
        )

    first = blueprint.nodes[0]
    if first.time_relation != "episode_start":
        errors.append(
            "[BLUEPRINT_EPISODE_START] 首节点必须标记 episode_start"
        )

    flashback_active = False
    known_node_keys: set[str] = set()
    active_state_facts: defaultdict[str, set[str]] = defaultdict(set)
    facts: dict[str, BlueprintStateChange] = {}
    participant_locations: dict[str, str] = {}
    constrained_since: dict[str, int] = {}
    constraint_facts: dict[str, str] = {}
    release_nodes: defaultdict[str, set[str]] = defaultdict(set)
    for index, node in enumerate(blueprint.nodes):
        previous = blueprint.nodes[index - 1] if index else None
        if previous is not None:
            time_changed = (
                node.temporal_domain_key != previous.temporal_domain_key
            )
            location_changed = node.location_key != previous.location_key
            if (
                (time_changed or location_changed)
                and not node.transition_cue.strip()
            ):
                errors.append(
                    f"[BLUEPRINT_TRANSITION_CUE_MISSING] {node.key} "
                    "发生时空变化但没有可见/可听转场依据"
                )
            if time_changed and node.time_relation in {
                "continuous", "flashback_continue",
            }:
                errors.append(
                    f"[BLUEPRINT_TIME_RELATION_INVALID] {node.key} "
                    "时间域变化却标记为连续"
                )

        if node.time_relation == "flashback_enter":
            if flashback_active:
                errors.append(
                    f"[BLUEPRINT_FLASHBACK_NESTED] {node.key} 重复进入回忆"
                )
            flashback_active = True
        elif node.time_relation == "flashback_continue" and not flashback_active:
            errors.append(
                f"[BLUEPRINT_FLASHBACK_ORPHAN] {node.key} 未进入回忆却延续回忆"
            )
        elif node.time_relation == "flashback_exit":
            if not flashback_active:
                errors.append(
                    f"[BLUEPRINT_FLASHBACK_EXIT_ORPHAN] {node.key} "
                    "没有可退出的回忆"
                )
            flashback_active = False

        for participant in node.participants:
            previous_location = participant_locations.get(participant)
            if (
                previous_location
                and previous_location != node.location_key
                and not node.transition_cue.strip()
            ):
                errors.append(
                    f"[BLUEPRINT_CHARACTER_TELEPORT] {node.key} 中 "
                    f"{participant} 从 {previous_location} 无衔接到 "
                    f"{node.location_key}"
                )
            participant_locations[participant] = node.location_key

        participant_keys = set(node.participants)
        evidence_keys = {
            evidence.identity_key for evidence in node.participant_evidence
            if evidence.identity_key
        } | {
            identity_key
            for assignment in node.state_subject_assignments
            for identity_key in assignment.identity_keys
        }
        for evidence in node.participant_evidence:
            unknown_evidence_sources = (
                set(evidence.source_segment_ids) - set(node.source_segment_ids)
            )
            if unknown_evidence_sources:
                errors.append(
                    f"[BLUEPRINT_PARTICIPANT_EVIDENCE_OUT_OF_SCOPE] {node.key} "
                    f"{evidence.identity_key} 引用非 owned SRC："
                    + "、".join(sorted(unknown_evidence_sources))
                )
            if evidence.identity_key not in participant_keys:
                errors.append(
                    f"[BLUEPRINT_PARTICIPANT_EVIDENCE_ORPHAN] {node.key} "
                    f"{evidence.identity_key} 未列入 participants"
                )
        # Presence of the list itself is not authority.  A node with declared
        # participants and an empty evidence list used to bypass this gate,
        # even though the equivalent non-empty/partial list was rejected.
        missing_evidence = participant_keys - evidence_keys
        if missing_evidence:
            errors.append(
                f"[BLUEPRINT_PARTICIPANT_EVIDENCE_MISSING] {node.key} 缺少"
                "参与者来源证据：" + "、".join(sorted(missing_evidence))
            )

        for requirement in node.state_requirements:
            if requirement.assumed_prior:
                active_state_facts[requirement.state_key].add(
                    f"assumed:{node.key}:{requirement.state_key}"
                )
                continue
            fact = facts.get(requirement.required_fact_key)
            if fact is None:
                errors.append(
                    f"[BLUEPRINT_STATE_UNESTABLISHED] {node.key} 依赖未建立状态 "
                    f"{requirement.state_key}；required_fact_key="
                    f"{requirement.required_fact_key or '（空）'}"
                )
            elif fact.state_key != requirement.state_key:
                errors.append(
                    f"[BLUEPRINT_STATE_KEY_MISMATCH] {node.key} 引用事实 "
                    f"{fact.fact_key}，但 state_key 不一致"
                )
            elif (
                fact.fact_key
                not in active_state_facts[requirement.state_key]
            ):
                errors.append(
                    f"[BLUEPRINT_STATE_SUPERSEDED] {node.key} 依赖的事实 "
                    f"{fact.fact_key} 已被后续状态替代"
                )
        for change in node.state_changes:
            if change.fact_key in facts:
                errors.append(
                    f"[BLUEPRINT_FACT_KEY_DUPLICATE] {change.fact_key} 重复"
                )
                continue
            for superseded_key in change.supersedes_fact_keys:
                superseded = facts.get(superseded_key)
                is_active = (
                    superseded is not None
                    and superseded_key
                    in active_state_facts[superseded.state_key]
                )
                is_explicit_constraint_release = (
                    is_active
                    and bool(node.released_constraints_for)
                    and superseded_key in constraint_facts.values()
                )
                if not is_active or (
                    superseded.state_key != change.state_key
                    and not is_explicit_constraint_release
                ):
                    errors.append(
                        f"[BLUEPRINT_STATE_SUPERSEDE_INVALID] {node.key} "
                        f"不能替代事实 {superseded_key}"
                    )
                    continue
                active_state_facts[superseded.state_key].discard(
                    superseded_key
                )
            facts[change.fact_key] = change
            active_state_facts[change.state_key].add(change.fact_key)

        for release_key in node.released_constraints_for:
            actor_key = release_key
            if release_key not in constraint_facts:
                actor_key = next(
                    (
                        actor
                        for actor, fact_key in constraint_facts.items()
                        if fact_key == release_key
                    ),
                    release_key,
                )
            constraint_fact_key = constraint_facts.get(actor_key)
            fact_released = any(
                constraint_fact_key in change.supersedes_fact_keys
                for change in node.state_changes
            )
            if not constraint_fact_key or not fact_released:
                errors.append(
                    f"[BLUEPRINT_AGENCY_RELEASE_UNGROUNDED] {node.key} "
                    f"声称解除 {actor_key} 的约束，但没有替代有效约束事实"
                )
                continue
            constrained_since.pop(actor_key, None)
            constraint_facts.pop(actor_key, None)
            release_nodes[actor_key].add(node.key)

        decision = node.decision
        if decision is not None:
            if decision.actor_key not in set(node.participants):
                errors.append(
                    f"[BLUEPRINT_DECISION_ACTOR_NOT_PARTICIPANT] {node.key} "
                    f"的 decision actor {decision.actor_key} 不在 participants"
                )
            if (
                node.participant_evidence
                and decision.actor_key not in {
                    evidence.identity_key
                    for evidence in node.participant_evidence
                }
            ):
                errors.append(
                    f"[BLUEPRINT_DECISION_ACTOR_EVIDENCE_MISSING] {node.key} "
                    f"的 decision actor {decision.actor_key} 没有 participant evidence"
                )
            unknown_setup = (
                set(decision.setup_node_keys)
                - known_node_keys
                - {node.key}
            )
            if unknown_setup:
                errors.append(
                    f"[BLUEPRINT_MOTIVATION_FUTURE] {node.key} 的动机依据"
                    "尚未发生："
                    + "、".join(sorted(unknown_setup))
                )
            if (
                decision.impact == "major"
                and not decision.setup_node_keys
            ):
                errors.append(
                    f"[BLUEPRINT_MOTIVATION_MISSING] {node.key} 的重大决定"
                    "没有前置压力、欲望或认知依据"
                )
            constrained_at = constrained_since.get(decision.actor_key)
            if (
                decision.agency_mode == "voluntary"
                and constrained_at is not None
            ):
                errors.append(
                    f"[BLUEPRINT_AGENCY_RELEASE_MISSING] {node.key} 将"
                    f"{decision.actor_key} 从被迫/无行为能力改为自主，"
                    "但中间没有约束解除节点"
                )
            elif decision.agency_mode in {
                "coerced", "incapacitated",
            }:
                constrained_since[decision.actor_key] = index
                constraint_fact = facts.get(
                    decision.constraint_fact_key,
                )
                if (
                    constraint_fact is None
                    or decision.constraint_fact_key
                    not in active_state_facts[
                        constraint_fact.state_key
                    ]
                ):
                    errors.append(
                        f"[BLUEPRINT_AGENCY_CONSTRAINT_FACT_MISSING] "
                        f"{node.key} 标记为 {decision.agency_mode}，但没有"
                        "建立有效 constraint_fact_key"
                    )
                else:
                    constraint_facts[decision.actor_key] = (
                        decision.constraint_fact_key
                    )
            unknown_release_keys = (
                set(decision.constraint_release_node_keys)
                - release_nodes[decision.actor_key]
            )
            if unknown_release_keys:
                errors.append(
                    f"[BLUEPRINT_AGENCY_RELEASE_REFERENCE_INVALID] {node.key} "
                    "引用的约束解除节点无效："
                    + "、".join(sorted(unknown_release_keys))
                )
        known_node_keys.add(node.key)

    if flashback_active:
        errors.append("[BLUEPRINT_FLASHBACK_UNCLOSED] 回忆时间域没有返回现在")

    try:
        plans = derive_blueprint_scene_plans(blueprint)
    except (
        BlueprintSourceOccurrenceError,
        BlueprintSourceOwnershipError,
    ) as exc:
        errors.extend(exc.errors)
    else:
        errors.extend(validate_blueprint_scene_partition(blueprint, plans))
    return list(dict.fromkeys(errors))
