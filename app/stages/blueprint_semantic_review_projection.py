"""叙事蓝图语义双审——定向复审的风险节点投影。

从 ``blueprint_semantic_review.py`` 拆出：原 ``_semantic_review_narrative_
blueprint`` 内联的 ``review_projection()`` 闭包，只读 ``blueprint``/
``source_text``（不涉及跨轮次的可变状态），提升为顶层函数后按显式参数传入即可，
没有闭包捕获陷阱。
"""
from __future__ import annotations

from typing import Any

from app.narrative_blueprint import NarrativeBlueprint
from app.source_excerpt import index_source_segments


def _blueprint_semantic_review_projection(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> tuple[dict[str, Any], str, list[str]]:
    """定向复审时只投影风险节点及其邻居，而不是整份蓝图。"""
    nodes = blueprint.nodes
    risky: set[int] = set()
    for index, node in enumerate(nodes):
        previous = nodes[index - 1] if index else None
        if (
            node.time_relation not in {"episode_start", "continuous"}
            or (previous is not None and (
                node.temporal_domain_key != previous.temporal_domain_key
                or node.location_key != previous.location_key
            ))
            or (node.decision is not None and node.decision.impact == "major")
            or bool(node.released_constraints_for)
            or bool(node.state_requirements)
            or bool(node.environment_source_unit_keys)
            or node.dramatic_load >= 3
        ):
            risky.add(index)
    if not risky and nodes:
        risky.update({0, len(nodes) - 1})
    selected = {
        neighbor
        for index in risky
        for neighbor in range(max(0, index - 1), min(len(nodes), index + 2))
    }
    selected_nodes = [nodes[index] for index in sorted(selected)]
    selected_keys = {node.key for node in selected_nodes}
    source_ids = list(dict.fromkeys(
        source_id
        for node in selected_nodes
        for source_id in node.source_segment_ids
    ))
    indexed = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    projected = {
        "format_version": blueprint.format_version,
        "episode_no": blueprint.episode_no,
        "nodes": [node.model_dump(mode="json") for node in selected_nodes],
        "scene_plans": [
            plan.model_dump(mode="json")
            for plan in blueprint.scene_plans
            if selected_keys.intersection(plan.node_keys)
        ],
        "review_scope": {
            "risk_node_keys": [nodes[index].key for index in sorted(risky)],
            "included_neighbor_node_keys": [node.key for node in selected_nodes],
            "total_blueprint_nodes": len(nodes),
        },
    }
    source_projection = "\n".join(
        f"[{source_id}] {indexed[source_id]}"
        for source_id in source_ids
        if source_id in indexed
    )
    return projected, source_projection, [node.key for node in selected_nodes]
