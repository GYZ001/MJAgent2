"""Prompt-context construction for the semantic (LLM-driven) patch planner:
the issue-target excerpt window and the full narrative-patch prompt context
builder. Split into its own file (rather than living next to
_llm_field_patch/_llm_field_patch_once) to break a two-file import cycle --
both the planner entry point and its single-call implementation need these
builders, and they need nothing back from either.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import json
import re
from app.harness.types import Issue
from typing import Any


_ISSUE_TARGET_INDEX_RE = re.compile(r"([a-z_][a-z0-9_]*)\[(\d+)\]")
# 键是**校验器真实写进 message 的下标标签名**，值是该标签在 payload 里的路径。
# 两者并不总是同名：`EpisodeScreenplay.events` 在 `ScreenplayDocument` 里叫
# `story_events`，而门禁写的是 `events[i]`（app/validators.py 的 `tag`）。
# 按 payload 字段名建键会让整段切片对该字段静默失效——第一版就是这么错的。
# `tests/test_repair_context_blind_spots.py` 用真实校验器输出反查这张表，
# 出现新的下标标签而这里没登记就会红。
_ISSUE_TARGET_CONTAINERS: dict[str, tuple[str, ...]] = {
    "spine_beats": ("plot_spine", "spine_beats"),
    "source_coverage": ("source_coverage",),
    "events": ("story_events",),
    "information_ledger": ("information_ledger",),
}
_ISSUE_TARGET_WINDOW = 1


def _issue_target_excerpt(
    payload: dict[str, Any],
    issue: Issue,
) -> dict[str, Any]:
    """Show the repairer the exact document slice its issue points at."""
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    haystack = " ".join(
        str(value or "")
        for value in (
            evidence.get("path"),
            issue.message,
            issue.subject,
            issue.repair_hint,
        )
    )
    excerpt: dict[str, Any] = {}
    for match in _ISSUE_TARGET_INDEX_RE.finditer(haystack):
        name, raw_index = match.group(1), match.group(2)
        route = _ISSUE_TARGET_CONTAINERS.get(name)
        if route is None or name in excerpt:
            continue
        container: Any = payload
        for key in route:
            container = (container or {}).get(key) if isinstance(container, dict) else None
        if not isinstance(container, list) or not container:
            continue
        index = int(raw_index)
        low = max(0, index - _ISSUE_TARGET_WINDOW)
        high = min(len(container), index + _ISSUE_TARGET_WINDOW + 1)
        excerpt[name] = {
            "path": ".".join(route),
            "window_start_index": low,
            "items": container[low:high],
        }
    return excerpt


def _narrative_patch_prompt_context(
    document,
    issue: Issue,
    source_text: str,
) -> tuple[dict[str, Any], str]:
    """Project one issue-local graph slice instead of the full document."""
    payload = document.model_dump(mode="json")
    plan = payload.get("narrative_plan") or {}
    issue_payload = issue.model_dump(mode="json")
    issue_blob = json.dumps(issue_payload, ensure_ascii=False)

    def strings(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, list):
            return set().union(*(strings(item) for item in value), set())
        if isinstance(value, dict):
            return set().union(*(strings(item) for item in value.values()), set())
        return set()

    graph_nodes: list[tuple[str, str, dict, set[str]]] = []
    all_ids: set[str] = set()
    for collection, values in plan.items():
        if not isinstance(values, list):
            continue
        singular = collection[:-1] if collection.endswith("s") else collection
        for item in values:
            if not isinstance(item, dict):
                continue
            id_fields = [
                (key, str(value))
                for key, value in item.items()
                if key.endswith("_id") and isinstance(value, str) and value
            ]
            if not id_fields:
                continue
            primary = next(
                (
                    value
                    for key, value in id_fields
                    if key == f"{singular}_id"
                ),
                id_fields[0][1],
            )
            all_ids.add(primary)
            graph_nodes.append((collection, primary, item, set()))

    enriched_nodes: list[tuple[str, str, dict, set[str]]] = []
    for collection, primary, item, _refs in graph_nodes:
        refs = strings(item) & all_ids
        refs.discard(primary)
        enriched_nodes.append((collection, primary, item, refs))
    graph_nodes = enriched_nodes

    selected_ids = {
        identity
        for identity in all_ids
        if identity in issue_blob
    }
    ordered_selected: list[str] = [
        primary
        for _collection, primary, _item, _refs in graph_nodes
        if primary in selected_ids
    ]
    frontier = set(selected_ids)
    for _depth in range(2):
        if not frontier or len(ordered_selected) >= 24:
            break
        discovered: list[str] = []
        for _collection, primary, _item, refs in graph_nodes:
            if primary in selected_ids:
                continue
            if refs & frontier or (
                primary in set().union(*(
                    refs
                    for _c, selected, _i, refs in graph_nodes
                    if selected in frontier
                ), set())
            ):
                discovered.append(primary)
        for primary in discovered:
            if primary not in selected_ids:
                selected_ids.add(primary)
                ordered_selected.append(primary)
                if len(ordered_selected) >= 24:
                    break
        frontier = set(discovered)

    if not ordered_selected:
        related_values = strings(issue_payload)
        for _collection, primary, item, _refs in graph_nodes:
            if strings(item) & related_values:
                selected_ids.add(primary)
                ordered_selected.append(primary)
                if len(ordered_selected) >= 8:
                    break

    selected_order = {
        primary: index for index, primary in enumerate(ordered_selected)
    }
    scoped_plan: dict[str, Any] = {
        key: value
        for key, value in plan.items()
        if not isinstance(value, list)
    }
    graph_index: dict[str, list[str]] = {}
    for collection, primary, item, _refs in graph_nodes:
        graph_index.setdefault(collection, []).append(primary)
        if primary not in selected_ids:
            continue
        scoped_plan.setdefault(collection, []).append(item)
    for values in scoped_plan.values():
        if isinstance(values, list):
            values.sort(
                key=lambda item: selected_order.get(
                    next(
                        (
                            str(value)
                            for key, value in item.items()
                            if key.endswith("_id")
                            and isinstance(value, str)
                        ),
                        "",
                    ),
                    len(selected_order),
                ),
            )

    source_excerpts: list[str] = []
    for item in scoped_plan.get("source_evidence") or []:
        excerpt = str(item.get("verbatim_excerpt") or "").strip()
        if excerpt and excerpt in source_text:
            source_excerpts.append(excerpt)
    if not source_excerpts and len(source_text) <= 20_000:
        source_excerpt = source_text
    else:
        source_excerpt = "\n\n".join(dict.fromkeys(source_excerpts))
    source_excerpt = source_excerpt[:20_000]

    context = {
        "screenplay_metadata": payload.get("screenplay_metadata"),
        "scene_blocks": payload.get("scene_blocks"),
        "dialogue_chains": payload.get("dialogue_chains"),
        "voice_bible": payload.get("voice_bible"),
        "narrative_plan": scoped_plan,
        "narrative_graph_id_index": graph_index,
        "scope_note": (
            "narrative_plan 仅含当前问题的双向两跳依赖闭包；"
            "narrative_graph_id_index 是全图稳定 ID 索引。"
        ),
    }
    target_excerpt = _issue_target_excerpt(payload, issue)
    if target_excerpt:
        context["issue_target_excerpt"] = target_excerpt
        context["scope_note"] += (
            "issue_target_excerpt 是本次问题直接指向的文档切片"
            "（含前后各一条相邻项，window_start_index 为窗口首项的真实下标），"
            "修改这些字段时以它为准。"
        )
    return context, source_excerpt


