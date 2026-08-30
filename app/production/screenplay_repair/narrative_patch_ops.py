"""Low-level narrative-graph document-patch mechanics: locating/collecting
narrative nodes, applying a single document patch operation, and validating
candidate patch operations are executable against the current graph.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import re
from app.harness.types import Issue
from app.production.patch import PatchOperation
from typing import Any


def _find_narrative_node(value: Any, node_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if any(
            key.endswith("_id") and str(candidate or "") == node_id
            for key, candidate in value.items()
        ):
            return value
        for child in value.values():
            found = _find_narrative_node(child, node_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_narrative_node(child, node_id)
            if found is not None:
                return found
    return None


def _narrative_collection_for_node(
    plan_data: dict[str, Any],
    node_id: str,
) -> str | None:
    matches = [
        collection
        for collection, nodes in plan_data.items()
        if (
            isinstance(nodes, list)
            and _find_narrative_node(nodes, node_id) is not None
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _narrative_collection_for_new_node(
    plan_data: dict[str, Any],
    node_id: str,
    value: dict[str, Any],
) -> str | None:
    identity_fields = {
        key
        for key, candidate in value.items()
        if (
            key.endswith("_id")
            and str(candidate or "").strip() == node_id
        )
    }
    if not identity_fields:
        return None
    matches: list[str] = []
    for collection, nodes in plan_data.items():
        if not isinstance(nodes, list):
            continue
        if any(
            isinstance(node, dict)
            and bool(identity_fields & set(node))
            for node in nodes
        ):
            matches.append(collection)
    return matches[0] if len(matches) == 1 else None


def _try_document_patch_operation(
    operation: PatchOperation,
    document: Any,
    plan_data: dict[str, Any],
) -> tuple[PatchOperation, Any] | None:
    """Resolve and probe a direct document field before graph ID inference."""
    if operation.op != "replace_field":
        return None
    target = dict(operation.target or {})
    collection = re.split(
        r"[.\[]+",
        str(target.get("collection") or "").strip(),
        maxsplit=1,
    )[0]
    if (
        str(target.get("kind") or "").strip() == "narrative_node"
        or (collection and isinstance(plan_data.get(collection), list))
    ):
        return None

    from app.production.patch import apply_patch_operation_to_document
    from app.production.screenplay_document import resolve_field_patch_target

    candidate = operation.model_copy(deep=True)
    candidate.target = resolve_field_patch_target(
        document,
        path=candidate.path,
        target=target,
    )
    try:
        updated, _ = apply_patch_operation_to_document(document, candidate)
    except Exception:  # noqa: BLE001 - probe untrusted candidate in isolation
        return None
    return candidate, updated


def _candidate_targets_narrative_graph(
    candidate: dict[str, Any],
    plan_data: dict[str, Any],
    *,
    document: Any | None = None,
) -> bool:
    operations = candidate.get("operations")
    if not isinstance(operations, list):
        return False
    for raw in operations:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_patch_operation_payload(raw)
        try:
            operation = PatchOperation.model_validate(normalized)
        except Exception:  # noqa: BLE001 - model output is untrusted
            continue
        if (
            document is not None
            and _try_document_patch_operation(operation, document, plan_data)
            is not None
        ):
            continue
        target = normalized.get("target") or {}
        node_id = str(target.get("id") or "").strip()
        collection = re.split(
            r"[.\[]+",
            str(target.get("collection") or "").strip(),
            maxsplit=1,
        )[0]
        if collection and isinstance(plan_data.get(collection), list):
            return True
        if node_id and _narrative_collection_for_node(plan_data, node_id):
            return True
        value = normalized.get("value")
        if (
            normalized.get("op") in {"create_node", "insert_node"}
            and node_id
            and isinstance(value, dict)
            and _narrative_collection_for_new_node(
                plan_data,
                node_id,
                value,
            )
        ):
            return True
    return False


def _normalize_top_level_narrative_parent(
    target: dict[str, Any],
    *,
    collection: str,
    plan_data: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(target)
    parent_id = str(normalized.get("parent_id") or "").strip()
    parent_field = str(normalized.get("parent_field") or "").strip()
    if (
        collection
        and parent_field == collection
        and parent_id in {
            "narrative_plan",
            str(plan_data.get("scope_id") or ""),
        }
    ):
        normalized.pop("parent_id", None)
        normalized.pop("parent_field", None)
    return normalized


def _expand_single_action_event_closure(
    operations: list[PatchOperation],
    plan_data: dict[str, Any],
) -> list[PatchOperation]:
    """Keep a one-action event's fact transition fields structurally aligned."""
    expanded = [operation.model_copy(deep=True) for operation in operations]
    existing = {
        (
            str((operation.target or {}).get("id") or ""),
            re.split(r"[./]+", operation.path.strip("/"))[-1],
        )
        for operation in expanded
    }
    events = plan_data.get("events")
    actions = plan_data.get("atomic_actions")
    if not isinstance(events, list) or not isinstance(actions, list):
        return expanded

    for operation in list(expanded):
        if operation.op != "replace_field":
            continue
        target = operation.target or {}
        node_id = str(target.get("id") or "").strip()
        collection = str(target.get("collection") or "").strip()
        if not collection and node_id:
            collection = _narrative_collection_for_node(plan_data, node_id) or ""
        field = re.split(r"[./]+", operation.path.strip("/"))[-1]
        if collection != "events" or field not in {
            "precondition_fact_ids", "effects_add", "effects_remove",
        }:
            continue
        event = _find_narrative_node(events, node_id)
        if event is None:
            continue
        action_ids = [
            str(action_id)
            for action_id in (event.get("action_ids") or [])
            if str(action_id).strip()
        ]
        if len(action_ids) != 1:
            continue
        action = _find_narrative_node(actions, action_ids[0])
        old_value = event.get(field)
        if (
            action is None
            or not isinstance(old_value, list)
            or not isinstance(operation.value, list)
            or action.get(field) != old_value
            or (action_ids[0], field) in existing
        ):
            continue
        expanded.append(PatchOperation(
            op="replace_field",
            path=field,
            value=list(operation.value),
            target={
                "kind": "narrative_node",
                "collection": "atomic_actions",
                "id": action_ids[0],
                "derived_from_event_id": node_id,
            },
        ))
        existing.add((action_ids[0], field))
    return expanded


def _resolve_narrative_patch_owner(
    nodes: list[Any],
    *,
    node_id: str,
    patch_field: str,
    issue: Issue,
) -> tuple[dict[str, Any], str] | None:
    """Resolve a wrongly targeted ancestor only when schema evidence is unique."""
    ancestor = _find_narrative_node(nodes, node_id)
    if ancestor is None:
        return None
    if patch_field in ancestor:
        return ancestor, node_id

    owners: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if patch_field in value:
                owners.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(ancestor)
    evidence = issue.evidence or {}
    issue_locator = " ".join([
        issue.message or "",
        str(evidence.get("path") or ""),
        *[str(value) for value in evidence.get("related_node_ids") or []],
    ])
    mentioned: list[tuple[dict[str, Any], str]] = []
    for owner in owners:
        for key, candidate in owner.items():
            candidate_id = str(candidate or "").strip()
            if (
                key.endswith("_id")
                and candidate_id
                and candidate_id in issue_locator
            ):
                mentioned.append((owner, candidate_id))

    unique_mentions = {
        (id(owner), candidate_id): (owner, candidate_id)
        for owner, candidate_id in mentioned
    }
    if len(unique_mentions) == 1:
        return next(iter(unique_mentions.values()))
    if len(owners) == 1:
        identity_values = [
            str(candidate or "").strip()
            for key, candidate in owners[0].items()
            if key.endswith("_id") and str(candidate or "").strip()
        ]
        if len(identity_values) == 1:
            return owners[0], identity_values[0]
    return None


def _normalize_patch_operation_payload(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    target = dict(item.get("target") or {})
    has_field_path = bool(str(item.get("path") or "").strip())
    structural_op = str(item.get("op") or "") in {
        "create_node", "insert_node", "delete_node", "move_node",
    }
    if has_field_path and not structural_op:
        normalized["op"] = "replace_field"
    elif structural_op:
        normalized["path"] = ""
    for key in ("parent_id", "parent_field", "to_index"):
        if key in item and key not in target:
            target[key] = item[key]
    if (
        not has_field_path
        and target.get("parent_id")
        and not target.get("parent_field")
    ):
        target.pop("parent_id", None)
    normalized["target"] = target
    return normalized


def _candidate_is_executable(
    candidate: dict[str, Any],
    document: Any,
) -> bool:
    """Probe a candidate with the production executor on an isolated document."""
    from app.production.patch import (
        PatchOperation,
        apply_patch_operation_to_document,
    )

    operations = candidate.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 3:
        return False
    working = document
    try:
        for raw in operations:
            if not isinstance(raw, dict):
                return False
            operation = PatchOperation.model_validate(
                _normalize_patch_operation_payload(raw),
            )
            target = dict(operation.target or {})
            plan = getattr(working, "narrative_plan", None)
            plan_data = (
                plan.model_dump(mode="json")
                if plan is not None
                else {}
            )
            direct_patch = _try_document_patch_operation(
                operation,
                working,
                plan_data,
            )
            if direct_patch is not None:
                operation, working = direct_patch
                continue
            collection = re.split(
                r"[.\[]+",
                str(target.get("collection") or "").strip(),
                maxsplit=1,
            )[0]
            node_id = str(target.get("id") or "").strip()
            if not collection and node_id:
                collection = (
                    _narrative_collection_for_node(plan_data, node_id)
                    or ""
                )
            if (
                not collection
                and operation.op in {"create_node", "insert_node"}
                and node_id
                and isinstance(operation.value, dict)
            ):
                collection = (
                    _narrative_collection_for_new_node(
                        plan_data,
                        node_id,
                        operation.value,
                    )
                    or ""
                )
            if isinstance(plan_data.get(collection), list):
                target = _normalize_top_level_narrative_parent(
                    target,
                    collection=collection,
                    plan_data=plan_data,
                )
                target = {
                    **target,
                    "kind": "narrative_node",
                    "collection": collection,
                }
            operation.target = target
            working, _ = apply_patch_operation_to_document(working, operation)
    except Exception:  # noqa: BLE001 - probing untrusted model output
        return False
    return True


def _resolve_dialogue_chain_turn_target(
    document,
    *,
    target: dict[str, Any],
    patch_field: str,
) -> dict[str, Any] | None:
    turn_id = str(target.get("turn_id") or target.get("id") or "").strip()
    chain_id = str(target.get("chain_id") or "").strip()
    turn_index = target.get("turn_index")
    match = re.fullmatch(r"(.+)-T(\d+)", turn_id, re.I)
    if not chain_id and match:
        chain_id = match.group(1)
    if turn_index is None and match:
        turn_index = int(match.group(2)) - 1
    if not chain_id or turn_index is None:
        return None
    try:
        turn_index = int(turn_index)
    except (TypeError, ValueError):
        return None
    chain = next(
        (
            item for item in document.dialogue_chains
            if (item.chain_id or "").strip() == chain_id
        ),
        None,
    )
    if (
        chain is None
        or not 0 <= turn_index < len(chain.turns or [])
        or patch_field not in type(chain.turns[turn_index]).model_fields
    ):
        return None
    return {
        **target,
        "id": turn_id or f"{chain_id}-T{turn_index + 1}",
        "turn_id": turn_id or f"{chain_id}-T{turn_index + 1}",
        "chain_id": chain_id,
        "turn_index": turn_index,
    }


