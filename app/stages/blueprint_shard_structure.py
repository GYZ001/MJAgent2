"""叙事蓝图分片——分片边界、去重折叠与叶子计划缓存结构。"""
from __future__ import annotations

from collections import defaultdict
import json
from typing import Any


from app.narrative_blueprint import (
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD,
    BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD,
    NarrativeBlueprintShard,
    blueprint_source_occurrence_issues,
)
from app.source_facts import (
    SOURCE_FACT_VERSION,
    source_segment_facts,
)

from .common import StageError
from .constants import BLUEPRINT_SHARD_MAX_TOKENS, BLUEPRINT_SHARD_MIN_TOKENS


def _blueprint_shard_boundary_context(
    nodes: list[Any],
) -> dict[str, Any]:
    active_facts: dict[str, dict[str, Any]] = {}
    participant_locations: dict[str, str] = {}
    story_nodes = [
        node for node in nodes
        if node.narrative_layer == "story"
    ]
    for node in story_nodes:
        for change in node.state_changes:
            for fact_key in change.supersedes_fact_keys:
                active_facts.pop(fact_key, None)
            active_facts[change.fact_key] = {
                "fact_key": change.fact_key,
                "state_key": change.state_key,
                "value": change.value,
                "established_by": node.key,
            }
        for participant in node.participants:
            participant_locations[participant] = node.location_key
    return {
        "recent_nodes": [
            {
                "key": node.key,
                "summary": node.summary,
                "temporal_domain_key": node.temporal_domain_key,
                "time_label": node.time_label,
                "location_key": node.location_key,
                "location_label": node.location_label,
                "participants": node.participants,
            }
            for node in story_nodes[-6:]
        ],
        "active_state_facts": list(active_facts.values())[-40:],
        "participant_locations": participant_locations,
    }


def _namespace_blueprint_shard(
    shard: NarrativeBlueprintShard,
) -> None:
    prefix = f"S{shard.shard_index:03d}-"
    node_key_map = {
        node.key: f"{prefix}{node.key}"
        for node in shard.nodes
        if not node.key.startswith(prefix)
    }
    fact_key_map = {
        change.fact_key: f"{prefix}{change.fact_key}"
        for node in shard.nodes
        for change in node.state_changes
        if not change.fact_key.startswith(prefix)
    }
    for node in shard.nodes:
        node.key = node_key_map.get(node.key, node.key)
        for requirement in node.state_requirements:
            requirement.required_fact_key = fact_key_map.get(
                requirement.required_fact_key,
                requirement.required_fact_key,
            )
        for change in node.state_changes:
            change.fact_key = fact_key_map.get(
                change.fact_key,
                change.fact_key,
            )
            change.supersedes_fact_keys = [
                fact_key_map.get(fact_key, fact_key)
                for fact_key in change.supersedes_fact_keys
            ]
        if node.decision is not None:
            node.decision.setup_node_keys = [
                node_key_map.get(node_key, node_key)
                for node_key in node.decision.setup_node_keys
            ]
            node.decision.constraint_release_node_keys = [
                node_key_map.get(node_key, node_key)
                for node_key in node.decision.constraint_release_node_keys
            ]
            node.decision.constraint_fact_key = fact_key_map.get(
                node.decision.constraint_fact_key,
                node.decision.constraint_fact_key,
            )


def _blueprint_node_has_operational_authority(node: Any) -> bool:
    return bool(
        node.participants
        or node.participant_evidence
        or node.source_unit_deliveries
        or node.state_subject_assignments
        or node.environment_source_unit_keys
        or node.state_requirements
        or node.state_changes
        or node.released_constraints_for
        or node.decision is not None
        or node.exit_state.strip()
    )


def _collapse_nonoperational_duplicate_source_nodes(
    shard: NarrativeBlueprintShard,
) -> None:
    """Remove only authority-free nodes whose SRCs have one other owner."""
    owners_by_source: defaultdict[str, list[int]] = defaultdict(list)
    for index, node in enumerate(shard.nodes):
        for source_id in dict.fromkeys(node.source_segment_ids):
            owners_by_source[source_id].append(index)

    removable_indexes = {
        index
        for index, node in enumerate(shard.nodes)
        if (
            node.source_segment_ids
            and not _blueprint_node_has_operational_authority(node)
            and all(
                len({
                    owner_index
                    for owner_index in owners_by_source[source_id]
                    if owner_index != index
                }) == 1
                for source_id in node.source_segment_ids
            )
        )
    }
    # Two authority-free duplicate nodes do not establish which one is
    # redundant. Keep both so the source occurrence validator fails closed.
    removable_indexes = {
        index
        for index in removable_indexes
        if all(
            next(
                owner_index
                for owner_index in owners_by_source[source_id]
                if owner_index != index
            ) not in removable_indexes
            for source_id in shard.nodes[index].source_segment_ids
        )
    }
    shard.nodes = [
        node
        for index, node in enumerate(shard.nodes)
        if index not in removable_indexes
    ]


def _remove_duplicate_repair_orphan_nodes(
    shard: NarrativeBlueprintShard,
    *,
    attempt: int,
    previous_candidate: dict[str, Any] | None,
    previous_validation_errors: list[str],
) -> None:
    """Remove only nodes orphaned while repairing typed duplicate ownership."""

    if (
        attempt <= 1
        or previous_candidate is None
    ):
        return
    previous = NarrativeBlueprintShard.model_validate(previous_candidate)
    reported_errors = set(previous_validation_errors)
    duplicate_issues = [
        issue
        for issue in blueprint_source_occurrence_issues(
            previous.nodes,
            prefix="BLUEPRINT_SHARD",
        )
        if issue.error in reported_errors
    ]
    duplicate_sources_by_node: defaultdict[str, set[str]] = defaultdict(set)
    for issue in duplicate_issues:
        for node_key in issue.node_keys:
            duplicate_sources_by_node[node_key].add(
                issue.source_segment_id
            )
    previous_nodes_by_key: defaultdict[str, list[Any]] = defaultdict(list)
    for node in previous.nodes:
        previous_nodes_by_key[node.key].append(node)
    current_owners: defaultdict[str, list[str]] = defaultdict(list)
    for node in shard.nodes:
        for source_id in node.source_segment_ids:
            current_owners[source_id].append(node.key)

    def removable(node: Any) -> bool:
        if (
            node.source_segment_ids
            or _blueprint_node_has_operational_authority(node)
        ):
            return False
        previous_matches = previous_nodes_by_key.get(node.key, [])
        if len(previous_matches) != 1:
            return False
        lost_sources = previous_matches[0].source_segment_ids
        return bool(lost_sources) and all(
            source_id in duplicate_sources_by_node[node.key]
            and len(current_owners[source_id]) == 1
            and current_owners[source_id][0] != node.key
            for source_id in lost_sources
        )

    shard.nodes = [
        node
        for node in shard.nodes
        if not removable(node)
    ]


def _normalize_blueprint_shard_structure(
    shard: NarrativeBlueprintShard,
    *,
    boundary_context: dict[str, Any],
    attempt: int = 1,
    previous_candidate: dict[str, Any] | None = None,
    previous_validation_errors: list[str] | None = None,
) -> None:
    _collapse_nonoperational_duplicate_source_nodes(shard)
    _remove_duplicate_repair_orphan_nodes(
        shard,
        attempt=attempt,
        previous_candidate=previous_candidate,
        previous_validation_errors=previous_validation_errors or [],
    )
    fact_state_keys = {
        str(fact.get("fact_key") or ""): str(
            fact.get("state_key") or ""
        )
        for fact in boundary_context.get("active_state_facts", [])
    }
    previous = None
    for node in shard.nodes:
        if previous is not None:
            changed_domain = (
                node.temporal_domain_key
                != previous.temporal_domain_key
            )
            changed_location = node.location_key != previous.location_key
            if changed_domain and node.time_relation == "continuous":
                node.time_relation = "jump"
            if (
                (changed_domain or changed_location)
                and not node.transition_cue.strip()
            ):
                node.transition_cue = (
                    node.opening_image.strip()
                    or f"从{previous.location_label}转至{node.location_label}"
                )
        if (
            node.decision is not None
            and node.decision.impact == "major"
            and not node.decision.pressure.strip()
        ):
            node.decision.pressure = node.action_logic
        if (
            node.decision is not None
            and node.decision.impact == "major"
            and not node.decision.setup_node_keys
            and node.decision.pressure.strip()
            and node.decision.desire.strip()
        ):
            node.decision.setup_node_keys = [node.key]
        for change in node.state_changes:
            change.supersedes_fact_keys = [
                fact_key
                for fact_key in change.supersedes_fact_keys
                if (
                    fact_key not in fact_state_keys
                    or fact_state_keys[fact_key] == change.state_key
                    or node.released_constraints_for
                )
            ]
            fact_state_keys[change.fact_key] = change.state_key
        previous = node
    if (
        shard.shard_index == 1
        and not boundary_context.get("active_state_facts")
    ):
        for node in shard.nodes:
            for requirement in node.state_requirements:
                if not requirement.required_fact_key.strip():
                    requirement.assumed_prior = True


def _blueprint_segment_output_weight(segment: Any) -> int:
    """Estimate typed output pressure without asking the model to plan itself."""

    facts = source_segment_facts(segment.segment_id, segment.text)
    return max(1, len(facts))


def _partition_blueprint_segments(segments: list[Any]) -> list[list[Any]]:
    """Create stable sequential shards bounded by SRC and source-fact pressure."""

    shards: list[list[Any]] = []
    current: list[Any] = []
    current_weight = 0
    for segment in segments:
        weight = _blueprint_segment_output_weight(segment)
        if current and (
            len(current) >= BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD
            or current_weight + weight
            > BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD
        ):
            shards.append(current)
            current = []
            current_weight = 0
        current.append(segment)
        current_weight += weight
    if current:
        shards.append(current)
    return shards


def _split_blueprint_segments(segments: list[Any]) -> list[list[Any]]:
    """Split one failed shard at the deterministic nearest weight midpoint."""

    if len(segments) < 2:
        return [segments]
    weights = [_blueprint_segment_output_weight(segment) for segment in segments]
    total = sum(weights)
    prefix = 0
    best_index = 1
    best_distance: int | None = None
    for index, weight in enumerate(weights[:-1], start=1):
        prefix += weight
        distance = abs(total - 2 * prefix)
        if best_distance is None or distance < best_distance:
            best_index = index
            best_distance = distance
    return [segments[:best_index], segments[best_index:]]


def _blueprint_leaf_plan_from_cache(
    segments: list[Any],
    cached_rows: list[Any],
    *,
    source_corpus_hash: str | None = None,
) -> tuple[list[list[Any]], list[int], dict[int, tuple[Any, NarrativeBlueprintShard]]]:
    """Rebuild one exact source cover before any paid parent request.

    Current-policy validated leaves are durable split-manifest entries.  They
    may cover a prefix plus later gaps after a failed activation.  Non-identical
    overlapping leaves are ambiguous authority and therefore fail closed;
    uncovered ranges are partitioned deterministically without first paying for
    a parent range that already contains reusable children.
    """
    source_ids = [str(segment.segment_id) for segment in segments]
    source_positions = {
        source_id: index for index, source_id in enumerate(source_ids)
    }
    interval_rows: dict[
        tuple[int, int], tuple[Any, NarrativeBlueprintShard, int]
    ] = {}
    for row in cached_rows:
        try:
            snapshot = json.loads(row["model_snapshot_json"] or "{}")
            if (
                snapshot.get("source_fact_version") != SOURCE_FACT_VERSION
                or snapshot.get("shard_policy_version")
                != BLUEPRINT_SHARD_POLICY_VERSION
                or snapshot.get("local_authority_validator_version")
                != BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
                or snapshot.get("split_manifest_version")
                != BLUEPRINT_SPLIT_MANIFEST_VERSION
            ):
                continue
            if source_corpus_hash is not None and snapshot.get(
                "source_corpus_hash"
            ) != source_corpus_hash:
                continue
            raw_content = json.loads(row["content_json"] or "{}")
            from app.evidence import repository as evidence_repository

            if str(row["content_hash"] or "") != evidence_repository.content_hash(
                raw_content
            ):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_HASH] validated leaf 内容哈希漂移"],
                )
            shard = NarrativeBlueprintShard.model_validate(raw_content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        owned = list(shard.source_segment_ids)
        if not owned or any(source_id not in source_positions for source_id in owned):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_SPLIT_MANIFEST_SOURCE_ESCAPE] 缓存 leaf 引用当前来源外 SRC"],
            )
        positions = [source_positions[source_id] for source_id in owned]
        if positions != list(range(positions[0], positions[-1] + 1)):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_SPLIT_MANIFEST_SOURCE_GAP] 缓存 leaf 来源不连续或乱序"],
            )
        interval = (positions[0], positions[-1] + 1)
        prior = interval_rows.get(interval)
        if prior is not None:
            if prior[1].model_dump(mode="json") != shard.model_dump(mode="json"):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_DUPLICATE_CONFLICT] 同区间存在不同 validated leaf"],
                )
            continue
        interval_rows[interval] = (
            row,
            shard,
            int(snapshot.get("split_depth") or 0),
        )
    ordered = sorted(interval_rows)
    for left, right in zip(ordered, ordered[1:]):
        if right[0] < left[1]:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_SPLIT_MANIFEST_OVERLAP] validated leaf 区间重叠"],
            )

    planned: list[list[Any]] = []
    depths: list[int] = []
    cached_by_plan_index: dict[int, tuple[Any, NarrativeBlueprintShard]] = {}
    cursor = 0
    for start, end in ordered:
        if cursor < start:
            for gap in _partition_blueprint_segments(segments[cursor:start]):
                planned.append(gap)
                depths.append(0)
        row, shard, depth = interval_rows[(start, end)]
        planned.append(segments[start:end])
        depths.append(depth)
        cached_by_plan_index[len(planned)] = (row, shard)
        cursor = end
    if cursor < len(segments):
        for gap in _partition_blueprint_segments(segments[cursor:]):
            planned.append(gap)
            depths.append(0)
    flattened = [segment.segment_id for group in planned for segment in group]
    if flattened != source_ids:
        raise StageError(
            "剧本时空因果蓝图分片",
            ["[BLUEPRINT_SPLIT_MANIFEST_COVERAGE] leaf/gap 计划未精确覆盖当前来源"],
        )
    for plan_index, (_row, shard) in cached_by_plan_index.items():
        if shard.shard_index != plan_index:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_SPLIT_MANIFEST_INDEX] 缓存 leaf 序号与精确覆盖顺序不一致"],
            )
    return planned, depths, cached_by_plan_index


def _blueprint_shard_token_budget(segments: list[Any]) -> int:
    weight = sum(_blueprint_segment_output_weight(segment) for segment in segments)
    estimated = BLUEPRINT_SHARD_MIN_TOKENS + weight * 512
    return min(
        BLUEPRINT_SHARD_MAX_TOKENS,
        max(BLUEPRINT_SHARD_MIN_TOKENS, estimated),
    )
