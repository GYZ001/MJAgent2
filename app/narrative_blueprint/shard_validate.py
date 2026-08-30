"""The structural shard validator: validate_narrative_blueprint_shard."""
from __future__ import annotations

import re
from typing import Any

from .constants import BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
from .models_core import (
    BlueprintSourceOccurrenceError,
    BlueprintSourceOwnershipError,
    NarrativeBlueprint,
    NarrativeBlueprintShard,
    blueprint_source_occurrence_issues,
)
from .models_patch import render_blueprint_shard_semantic_issue
from .scene_plans import derive_blueprint_scene_plans
from .state_subject_issues import blueprint_state_subject_issues
from .voice_identity_issues import blueprint_voice_identity_issues


def validate_narrative_blueprint_shard(
    shard: NarrativeBlueprintShard,
    *,
    expected_episode_no: int,
    expected_shard_index: int,
    expected_source_segment_ids: list[str],
    optional_source_segment_ids: set[str] | None = None,
    boundary_state_facts: list[dict[str, Any]] | None = None,
    source_text: str | None = None,
) -> list[str]:
    errors: list[str] = []
    expected = list(expected_source_segment_ids)
    expected_set = set(expected)
    optional = set(optional_source_segment_ids or ())
    if shard.episode_no != expected_episode_no:
        errors.append("[BLUEPRINT_SHARD_EPISODE] episode_no 不匹配")
    if shard.shard_index != expected_shard_index:
        errors.append("[BLUEPRINT_SHARD_INDEX] shard_index 不匹配")
    if shard.source_segment_ids != expected:
        errors.append("[BLUEPRINT_SHARD_SOURCE_CONTRACT] 分片来源清单不匹配")
    owned = [
        source_id
        for node in shard.nodes
        for source_id in node.source_segment_ids
    ]
    errors.extend(
        issue.error
        for issue in blueprint_source_occurrence_issues(
            shard.nodes,
            prefix="BLUEPRINT_SHARD",
        )
    )
    escaped = set(owned) - expected_set
    if escaped:
        errors.append(
            "[BLUEPRINT_SHARD_SOURCE_ESCAPE] 节点引用分片外来源："
            + "、".join(sorted(escaped))
        )
    missing = expected_set - set(owned) - optional
    if missing:
        errors.append(
            "[BLUEPRINT_SHARD_SOURCE_MISSING] 分片漏掉来源："
            + "、".join(sorted(missing))
        )
    source_positions = {
        source_id: position
        for position, source_id in enumerate(expected)
    }
    active_facts = {
        str(fact.get("fact_key") or ""): str(
            fact.get("state_key") or ""
        )
        for fact in (boundary_state_facts or [])
        if str(fact.get("fact_key") or "")
    }
    prior_position = -1
    for node_index, node in enumerate(shard.nodes):
        previous = shard.nodes[node_index - 1] if node_index else None
        if not node.source_segment_ids:
            errors.append(
                f"[BLUEPRINT_SHARD_NODE_UNGROUNDED] {node.key} 没有来源段"
            )
            continue
        if (
            len(node.source_segment_ids)
            > BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
        ):
            errors.append(
                f"[BLUEPRINT_SHARD_NODE_SIZE] {node.key} 合并了"
                f"{len(node.source_segment_ids)} 个来源段，最多允许 "
                f"{BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE} 个；"
                "source-fact unit 数量不计入 node size"
            )
            continue
        positions = [
            source_positions[source_id]
            for source_id in node.source_segment_ids
            if source_id in source_positions
        ]
        if positions and positions != list(
            range(min(positions), max(positions) + 1)
        ):
            errors.append(
                f"[BLUEPRINT_SHARD_SOURCE_GAP] {node.key} 来源不连续"
            )
        if positions and min(positions) < prior_position:
            errors.append(
                f"[BLUEPRINT_SHARD_SOURCE_ORDER] {node.key} 来源顺序倒退"
            )
        if positions:
            prior_position = min(positions)
        if re.search(r"[、+/]|内外", node.location_label):
            errors.append(
                f"[BLUEPRINT_SHARD_LOCATION_COMPOSITE] {node.key} "
                f"包含多个主要地点：{node.location_label}"
            )
        if previous is not None:
            changed_domain = (
                node.temporal_domain_key
                != previous.temporal_domain_key
            )
            changed_location = node.location_key != previous.location_key
            if changed_domain and node.time_relation == "continuous":
                errors.append(
                    f"[BLUEPRINT_SHARD_TIME_RELATION] {node.key} "
                    "时间域变化却标记 continuous"
                )
            if (
                (changed_domain or changed_location)
                and not node.transition_cue.strip()
            ):
                errors.append(
                    f"[BLUEPRINT_SHARD_TRANSITION_REQUIRED] {node.key} "
                    "时空变化缺少可见/可听转场"
                )
        if (
            node.decision is not None
            and node.decision.impact == "major"
            and (
                not node.decision.setup_node_keys
                or not node.decision.pressure.strip()
                or not node.decision.desire.strip()
            )
        ):
            errors.append(
                f"[BLUEPRINT_SHARD_MOTIVATION_REQUIRED] {node.key} "
                "重大决定缺少前置节点、压力或欲望"
            )
        for requirement in node.state_requirements:
            if (
                not requirement.assumed_prior
                and not requirement.required_fact_key.strip()
            ):
                errors.append(
                    f"[BLUEPRINT_SHARD_FACT_REQUIRED] {node.key} "
                    f"状态 {requirement.state_key} 缺少 fact_key"
                )
            elif (
                not requirement.assumed_prior
                and requirement.required_fact_key not in active_facts
            ):
                errors.append(
                    f"[BLUEPRINT_SHARD_FACT_UNKNOWN] {node.key} "
                    f"引用未建立事实 {requirement.required_fact_key}"
                )
        for change in node.state_changes:
            for superseded_key in change.supersedes_fact_keys:
                if superseded_key not in active_facts:
                    errors.append(
                        f"[BLUEPRINT_SHARD_SUPERSEDE_UNKNOWN] {node.key} "
                        f"不能替代未建立事实 {superseded_key}"
                    )
                elif (
                    active_facts[superseded_key] != change.state_key
                    and not node.released_constraints_for
                ):
                    errors.append(
                        f"[BLUEPRINT_SHARD_SUPERSEDE_STATE] {node.key} "
                        f"替代事实 {superseded_key} 的 state_key 不一致"
                    )
                active_facts.pop(superseded_key, None)
            active_facts[change.fact_key] = change.state_key
    node_keys = [node.key for node in shard.nodes]
    if len(node_keys) != len(set(node_keys)):
        errors.append("[BLUEPRINT_SHARD_NODE_DUPLICATE] 节点 key 重复")
    for node in shard.nodes:
        participant_keys = set(node.participants)
        evidence_keys = {
            evidence.identity_key
            for evidence in node.participant_evidence
            if evidence.identity_key
        } | {
            identity_key
            for assignment in node.state_subject_assignments
            for identity_key in assignment.identity_keys
        }
        for evidence in node.participant_evidence:
            escaped_sources = (
                set(evidence.source_segment_ids)
                - set(node.source_segment_ids)
            )
            if escaped_sources:
                errors.append(
                    f"[BLUEPRINT_SHARD_PARTICIPANT_EVIDENCE_OUT_OF_SCOPE] "
                    f"{node.key} identity_key={evidence.identity_key} "
                    "引用非 owned SRC："
                    + "、".join(sorted(escaped_sources))
                )
        orphan_evidence = evidence_keys - participant_keys
        if orphan_evidence:
            errors.append(
                f"[BLUEPRINT_SHARD_PARTICIPANT_EVIDENCE_ORPHAN] "
                f"{node.key} evidence identity 未列入 participants："
                + "、".join(sorted(orphan_evidence))
            )
        missing_evidence = participant_keys - evidence_keys
        if missing_evidence:
            errors.append(
                f"[BLUEPRINT_SHARD_PARTICIPANT_EVIDENCE_MISSING] "
                f"{node.key} participants 缺少同 identity_key 的来源证据"
                "或 exact-unit joint assignment："
                + "、".join(sorted(missing_evidence))
                + "；保留有来源角色并补 participant_evidence，"
                "不得删除角色或改用默认身份"
            )
    if source_text is not None:
        local_blueprint = NarrativeBlueprint(
            episode_no=shard.episode_no,
            nodes=shard.nodes,
        )
        for issue in (
            blueprint_voice_identity_issues(local_blueprint, source_text)
            + blueprint_state_subject_issues(local_blueprint, source_text)
        ):
            errors.append(render_blueprint_shard_semantic_issue(issue))
    try:
        derive_blueprint_scene_plans(NarrativeBlueprint(
            episode_no=shard.episode_no,
            nodes=shard.nodes,
        ))
    except (
        BlueprintSourceOccurrenceError,
        BlueprintSourceOwnershipError,
    ) as exc:
        errors.extend(
            error.replace(
                "[BLUEPRINT_SOURCE_OWNER_CONFLICT]",
                "[BLUEPRINT_SHARD_SOURCE_OWNER_CONFLICT]",
            )
            for error in exc.errors
        )
    return errors
