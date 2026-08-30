"""Normalizes source order and validates/applies a NarrativeBlueprintPatch's node replacements."""
from __future__ import annotations

from app.source_excerpt import index_source_segments

from .models_core import NarrativeBlueprint, NarrativeNode
from .models_patch import NarrativeBlueprintPatch, NarrativeNodeReplacement
from .scene_plans import derive_blueprint_scene_plans
from .semantic_review_schema import normalize_blueprint_fact_versions


def normalize_blueprint_source_order(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> int:
    """Restore authoritative source order after independent node replacements."""
    source_order = {
        segment.segment_id: index
        for index, segment in enumerate(index_source_segments(source_text))
    }
    ranked_nodes: list[tuple[int, int, NarrativeNode]] = []
    for original_index, node in enumerate(blueprint.nodes):
        positions = [
            source_order[source_id]
            for source_id in node.source_segment_ids
            if source_id in source_order
        ]
        if not positions or len(positions) != len(node.source_segment_ids):
            return 0
        ranked_nodes.append((min(positions), original_index, node))
    ordered_nodes = [
        node
        for _source_position, _original_index, node
        in sorted(ranked_nodes)
    ]
    moved = sum(
        node is not blueprint.nodes[index]
        for index, node in enumerate(ordered_nodes)
    )
    if moved:
        blueprint.nodes = ordered_nodes
    return moved


def validate_narrative_blueprint_patch_projection(
    patch: NarrativeBlueprintPatch,
    blueprint: NarrativeBlueprint,
) -> list[str]:
    """Keep repair inside the canonical timeline and source authority."""
    node_map = {node.key: node for node in blueprint.nodes}
    errors: list[str] = []
    replacement_keys = [
        replacement.node_key for replacement in patch.replacements
    ]
    if len(replacement_keys) != len(set(replacement_keys)):
        errors.append(
            "[BLUEPRINT_PATCH_NODE_DUPLICATE] "
            "同一 canonical node 不得重复替换"
        )
    if patch.delete_node_keys:
        errors.append(
            "[BLUEPRINT_PATCH_TIMELINE_DELETE] "
            "repair 不得删除 canonical timeline node"
        )
    for replacement in patch.replacements:
        original = node_map.get(replacement.node_key)
        if original is None:
            errors.append(
                "[BLUEPRINT_PATCH_NODE_UNKNOWN] "
                f"{replacement.node_key} 不在修复窗口"
            )
            continue
        if len(replacement.nodes) != 1:
            errors.append(
                "[BLUEPRINT_PATCH_TIMELINE_CARDINALITY] "
                f"{replacement.node_key} 必须一对一替换"
            )
            continue
        for node in replacement.nodes:
            if node.key != original.key:
                errors.append(
                    "[BLUEPRINT_PATCH_NODE_IDENTITY_CHANGE] "
                    f"{replacement.node_key} 不得改写 canonical key"
                )
            if node.source_segment_ids != original.source_segment_ids:
                errors.append(
                    "[BLUEPRINT_PATCH_SOURCE_OWNERSHIP_CHANGE] "
                    f"{replacement.node_key} 必须保持完整有序来源 ownership"
                )
            expected_semantics = (
                original.narrative_layer,
                original.event_priority,
                original.render_policy,
            )
            actual_semantics = (
                node.narrative_layer,
                node.event_priority,
                node.render_policy,
            )
            if actual_semantics != expected_semantics:
                errors.append(
                    "[BLUEPRINT_PATCH_SOURCE_SEMANTICS_CHANGE] "
                    f"{replacement.node_key} 必须保持来源语义三元"
                )
    return list(dict.fromkeys(errors))


def apply_narrative_blueprint_patch(
    blueprint: NarrativeBlueprint,
    patch: NarrativeBlueprintPatch,
    *,
    allow_source_expansion: bool = False,
    source_text: str | None = None,
) -> int:
    canonical_contract = [
        (
            node.key,
            tuple(node.source_segment_ids),
            node.narrative_layer,
            node.event_priority,
            node.render_policy,
        )
        for node in blueprint.nodes
    ]
    projection_errors = validate_narrative_blueprint_patch_projection(
        patch,
        blueprint,
    )
    if projection_errors:
        raise ValueError("；".join(projection_errors))
    original_keys = {node.key for node in blueprint.nodes}
    normalized_replacements: list[NarrativeNodeReplacement] = []
    replacement_by_target: dict[str, NarrativeNodeReplacement] = {}
    for replacement in patch.replacements:
        target_key = replacement.node_key
        if target_key not in original_keys:
            replacement_sources = {
                source_id
                for node in replacement.nodes
                for source_id in node.source_segment_ids
            }
            scored_targets: list[tuple[float, str]] = []
            for node in blueprint.nodes:
                original_sources = set(node.source_segment_ids)
                overlap = replacement_sources.intersection(
                    original_sources
                )
                union = replacement_sources.union(original_sources)
                if overlap and union:
                    scored_targets.append((
                        len(overlap) / len(union),
                        node.key,
                    ))
            best_score = max(
                (score for score, _key in scored_targets),
                default=0.0,
            )
            best_keys = [
                key
                for score, key in scored_targets
                if score == best_score and score >= 0.5
            ]
            if len(best_keys) != 1:
                raise ValueError(
                    "蓝图局部修复引用未知节点且无法按来源唯一重绑定："
                    f"{target_key}"
                )
            target_key = best_keys[0]
        existing = replacement_by_target.get(target_key)
        if existing is not None:
            existing.nodes.extend(replacement.nodes)
            continue
        replacement.node_key = target_key
        replacement_by_target[target_key] = replacement
        normalized_replacements.append(replacement)
    patch.replacements = normalized_replacements
    replacements = {
        replacement.node_key: replacement
        for replacement in patch.replacements
    }
    # Replacing a node already removes the original. Models occasionally also
    # list that key under delete_node_keys; replacement is the more specific
    # instruction and must win or the repaired source span disappears.
    delete_node_keys = set(patch.delete_node_keys) - set(replacements)
    delete_node_keys.intersection_update(original_keys)
    reserved_fact_keys = {
        change.fact_key
        for node in blueprint.nodes
        if (
            node.key not in replacements
            and node.key not in delete_node_keys
        )
        for change in node.state_changes
    }
    facts_by_key = {
        change.fact_key: change
        for node in blueprint.nodes
        for change in node.state_changes
    }
    removed_fact_keys = {
        change.fact_key
        for node in blueprint.nodes
        if node.key in replacements or node.key in delete_node_keys
        for change in node.state_changes
    }
    constraint_actor_by_fact = {
        node.decision.constraint_fact_key: node.decision.actor_key
        for node in blueprint.nodes
        if (
            node.decision is not None
            and node.decision.constraint_fact_key
        )
    }
    fact_key_renames: dict[str, str] = {}
    for replacement in patch.replacements:
        for replacement_node in replacement.nodes:
            if not replacement_node.transition_cue.strip():
                replacement_node.transition_cue = (
                    replacement_node.opening_image.strip()
                    or replacement_node.action_logic.strip()
                )
            for change_index, change in enumerate(
                replacement_node.state_changes,
                start=1,
            ):
                explicit_releases = set(
                    replacement_node.released_constraints_for
                )
                change.supersedes_fact_keys = [
                    fact_key
                    for fact_key in change.supersedes_fact_keys
                    if (
                        fact_key not in removed_fact_keys
                        and (
                            fact_key not in facts_by_key
                            or facts_by_key[fact_key].state_key
                            == change.state_key
                            or fact_key in explicit_releases
                            or constraint_actor_by_fact.get(fact_key)
                            in explicit_releases
                        )
                    )
                ]
                if change.fact_key in reserved_fact_keys:
                    original_key = change.fact_key
                    new_key = (
                        f"repair-{replacement_node.key}-{change_index}"
                    )
                    while new_key in reserved_fact_keys:
                        new_key += "x"
                    fact_key_renames[original_key] = new_key
                    change.fact_key = new_key
                reserved_fact_keys.add(change.fact_key)
    if fact_key_renames:
        for replacement in patch.replacements:
            for node in replacement.nodes:
                for requirement in node.state_requirements:
                    requirement.required_fact_key = fact_key_renames.get(
                        requirement.required_fact_key,
                        requirement.required_fact_key,
                    )
                for change in node.state_changes:
                    change.supersedes_fact_keys = [
                        fact_key_renames.get(fact_key, fact_key)
                        for fact_key in change.supersedes_fact_keys
                    ]
                node.released_constraints_for = [
                    fact_key_renames.get(value, value)
                    for value in node.released_constraints_for
                ]
                if node.decision is not None:
                    node.decision.constraint_fact_key = (
                        fact_key_renames.get(
                            node.decision.constraint_fact_key,
                            node.decision.constraint_fact_key,
                        )
                    )
    changed = 0
    rebuilt_nodes: list[NarrativeNode] = []
    existing_keys = {
        node.key
        for node in blueprint.nodes
        if (
            node.key not in replacements
            and node.key not in delete_node_keys
        )
    }
    for node in blueprint.nodes:
        if node.key in delete_node_keys:
            changed += 1
            continue
        replacement = replacements.get(node.key)
        if replacement is None:
            rebuilt_nodes.append(node)
            continue
        replacement_source_ids = {
            source_id
            for replacement_node in replacement.nodes
            for source_id in replacement_node.source_segment_ids
        }
        if (
            not replacement_source_ids
            or (
                not allow_source_expansion
                and not replacement_source_ids.issubset(
                    set(node.source_segment_ids),
                )
            )
        ):
            rebuilt_nodes.append(node)
            continue
        for replacement_node in replacement.nodes:
            if replacement_node.key in existing_keys:
                raise ValueError(
                    f"蓝图局部修复产生重复节点 key："
                    f"{replacement_node.key}"
                )
            existing_keys.add(replacement_node.key)
            rebuilt_nodes.append(replacement_node)
        changed += 1
    blueprint.nodes = rebuilt_nodes
    replacement_key_map = {
        old_key: replacement.nodes[0].key
        for old_key, replacement in replacements.items()
        if replacement.nodes and replacement.nodes[0].key != old_key
    }
    if replacement_key_map:
        for rebuilt_node in blueprint.nodes:
            if rebuilt_node.decision is None:
                continue
            rebuilt_node.decision.setup_node_keys = [
                replacement_key_map.get(node_key, node_key)
                for node_key in rebuilt_node.decision.setup_node_keys
            ]
            rebuilt_node.decision.constraint_release_node_keys = [
                replacement_key_map.get(node_key, node_key)
                for node_key
                in rebuilt_node.decision.constraint_release_node_keys
            ]
    normalize_blueprint_fact_versions(blueprint)
    repaired_contract = [
        (
            node.key,
            tuple(node.source_segment_ids),
            node.narrative_layer,
            node.event_priority,
            node.render_policy,
        )
        for node in blueprint.nodes
    ]
    if repaired_contract != canonical_contract:
        raise ValueError(
            "[BLUEPRINT_PATCH_CANONICAL_TIMELINE_CHANGE] repair 前后 "
            "timeline key、顺序、source ownership 与语义三元必须完全一致"
        )
    derive_blueprint_scene_plans(blueprint)
    return changed
