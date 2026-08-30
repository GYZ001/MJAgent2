"""Event-id canonicalization and event/action fact-reference derivation
phases of _normalize_screenplay_narrative_graph.

Split out of narrative_graph_normalize.py -- see that file's module
docstring.
"""
from __future__ import annotations

import re
from typing import Any


def _normalize_event_id_references(
    data: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Canonicalize event-id references (typos/casing/punctuation drift) across the whole plan tree."""
    event_ids = {
        str(event.get("event_id") or "").strip()
        for event in (data.get("events") or [])
        if isinstance(event, dict) and str(event.get("event_id") or "").strip()
    }
    event_aliases: dict[str, list[str]] = {}
    for event_id in event_ids:
        alias = re.sub(r"[^a-z0-9]+", "", event_id.casefold())
        if alias:
            event_aliases.setdefault(alias, []).append(event_id)

    def canonical_event_id(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or raw in event_ids:
            return raw
        alias = re.sub(r"[^a-z0-9]+", "", raw.casefold())
        matches = event_aliases.get(alias) or []
        return matches[0] if len(matches) == 1 else raw

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            is_event_anchor = str(value.get("type") or "").strip() == "event"
            for key, child in list(value.items()):
                child_path = f"{path}.{key}" if path else key
                if key == "id" and is_event_anchor:
                    normalized = canonical_event_id(child)
                    if normalized != child:
                        changes.append({
                            "kind": "event_ref",
                            "path": child_path,
                            "from": child,
                            "to": normalized,
                        })
                        value[key] = normalized
                elif key.endswith("event_id"):
                    normalized = canonical_event_id(child)
                    if normalized != child:
                        changes.append({
                            "kind": "event_ref",
                            "path": child_path,
                            "from": child,
                            "to": normalized,
                        })
                        value[key] = normalized
                elif key.endswith("event_ids") or key == "causal_parent_ids":
                    if isinstance(child, list):
                        normalized_values = [
                            canonical_event_id(item) for item in child
                        ]
                        if normalized_values != child:
                            changes.append({
                                "kind": "event_refs",
                                "path": child_path,
                                "from": child,
                                "to": normalized_values,
                            })
                            value[key] = normalized_values
                else:
                    walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(data)


def _build_actions_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index atomic_actions by action_id."""
    actions_by_id = {
        str(item.get("action_id") or "").strip(): item
        for item in (data.get("atomic_actions") or [])
        if (
            isinstance(item, dict)
            and str(item.get("action_id") or "").strip()
        )
    }
    return actions_by_id


def _derive_event_action_fact_refs(
    data: dict[str, Any],
    actions_by_id: dict[str, dict[str, Any]],
    changes: list[dict[str, Any]],
) -> None:
    """Derive each event's precondition/effect fact refs and proposition_ids from its bound actions."""
    fact_propositions = {
        str(fact.get("fact_id") or "").strip(): str(
            fact.get("proposition_id") or ""
        ).strip()
        for fact in (data.get("state_facts") or [])
        if (
            isinstance(fact, dict)
            and str(fact.get("fact_id") or "").strip()
            and str(fact.get("proposition_id") or "").strip()
        )
    }
    for event in data.get("events") or []:
        if not isinstance(event, dict):
            continue
        bound_actions = [
            actions_by_id[action_id]
            for action_id in (event.get("action_ids") or [])
            if action_id in actions_by_id
        ]
        action_preconditions = {
            str(fact_id)
            for action in bound_actions
            for fact_id in (action.get("precondition_fact_ids") or [])
            if str(fact_id or "").strip()
        }
        action_adds = {
            str(fact_id)
            for action in bound_actions
            for fact_id in (action.get("effects_add") or [])
            if str(fact_id or "").strip()
        }
        action_removes = {
            str(fact_id)
            for action in bound_actions
            for fact_id in (action.get("effects_remove") or [])
            if str(fact_id or "").strip()
        }
        touched_action_facts = (
            action_preconditions | action_adds | action_removes
        )
        derived_action_facts = {
            "precondition_fact_ids": action_preconditions - action_adds,
            "effects_add": action_adds - action_removes,
            "effects_remove": action_removes - action_adds,
        }
        for field, derived_facts in derived_action_facts.items():
            existing_facts = list(event.get(field) or [])
            preserved_event_facts = [
                fact_id
                for fact_id in existing_facts
                if str(fact_id) not in touched_action_facts
            ]
            normalized_facts = list(dict.fromkeys([
                *preserved_event_facts,
                *sorted(derived_facts),
            ]))
            if normalized_facts != existing_facts:
                changes.append({
                    "kind": "event_action_fact_refs",
                    "id": event.get("event_id"),
                    "field": field,
                    "from": existing_facts,
                    "to": normalized_facts,
                })
                event[field] = normalized_facts
        existing = list(event.get("proposition_ids") or [])
        required = [
            fact_propositions[fact_id]
            for fact_id in (
                *(event.get("precondition_fact_ids") or []),
                *(event.get("effects_add") or []),
                *(event.get("effects_remove") or []),
            )
            if fact_id in fact_propositions
        ]
        normalized = list(dict.fromkeys([*existing, *required]))
        if normalized != existing:
            changes.append({
                "kind": "event_proposition_refs",
                "id": event.get("event_id"),
                "from": existing,
                "to": normalized,
            })
            event["proposition_ids"] = normalized

