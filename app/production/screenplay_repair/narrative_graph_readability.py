"""Removed-delta cleanup (P15), readability-window budget normalization
(P16) and identity-contract evidence-ref normalization (P17) phases of
_normalize_screenplay_narrative_graph.

Split out of narrative_graph_normalize.py -- see that file's module
docstring.
"""
from __future__ import annotations

from typing import Any


def _prune_removed_delta_references(
    data: dict[str, Any],
    removed_delta_ids: set[str],
    changes: list[dict[str, Any]],
) -> None:
    """Drop references to pruned target_delta_ids from readability_windows and assimilation_tasks."""
    if removed_delta_ids:
        for window in data.get("readability_windows") or []:
            if not isinstance(window, dict):
                continue
            existing = list(window.get("target_delta_ids") or [])
            normalized = [
                delta_id
                for delta_id in existing
                if str(delta_id) not in removed_delta_ids
            ]
            if normalized != existing:
                window["target_delta_ids"] = normalized
                changes.append({
                    "kind": "removed_delta_window_refs",
                    "id": window.get("readability_window_id"),
                    "from": existing,
                    "to": normalized,
                })
        existing_tasks = list(data.get("assimilation_tasks") or [])
        normalized_tasks = [
            task
            for task in existing_tasks
            if (
                not isinstance(task, dict)
                or str(task.get("target_delta_id") or "")
                not in removed_delta_ids
            )
        ]
        if normalized_tasks != existing_tasks:
            data["assimilation_tasks"] = normalized_tasks
            changes.append({
                "kind": "removed_delta_assimilation_tasks",
                "removed_target_delta_ids": sorted(removed_delta_ids),
            })


def _normalize_readability_windows(
    data: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Link each target_delta into its readability_window and pad the window's processing budget to cover it."""
    delta_requirements: dict[str, tuple[str, float]] = {}
    delta_windows: dict[str, str] = {}
    for intent in data.get("experience_intents") or []:
        if not isinstance(intent, dict):
            continue
        for path in intent.get("audience_paths") or []:
            if not isinstance(path, dict):
                continue
            prior_id = str(path.get("audience_prior_id") or "unknown")
            for delta in path.get("target_deltas") or []:
                if not isinstance(delta, dict):
                    continue
                delta_id = str(delta.get("target_delta_id") or "").strip()
                if not delta_id:
                    continue
                try:
                    required = max(
                        0.0, float(delta.get("required_processing_s") or 0),
                    )
                except (TypeError, ValueError):
                    required = 0.0
                delta_requirements[delta_id] = (prior_id, required)
                window_id = str(
                    delta.get("primary_delivery_window_id") or ""
                ).strip()
                if window_id:
                    delta_windows[delta_id] = window_id
    windows_by_id = {
        str(window.get("readability_window_id") or ""): window
        for window in (data.get("readability_windows") or [])
        if (
            isinstance(window, dict)
            and str(window.get("readability_window_id") or "").strip()
        )
    }
    for delta_id, window_id in delta_windows.items():
        window = windows_by_id.get(window_id)
        if window is None:
            continue
        existing_ids = list(window.get("target_delta_ids") or [])
        if delta_id in existing_ids:
            continue
        normalized_ids = [*existing_ids, delta_id]
        changes.append({
            "kind": "readability_target_delta_ref",
            "id": window_id,
            "from": existing_ids,
            "to": normalized_ids,
        })
        window["target_delta_ids"] = normalized_ids
    for window in data.get("readability_windows") or []:
        if not isinstance(window, dict):
            continue
        required_by_prior: dict[str, float] = {}
        for delta_id in window.get("target_delta_ids") or []:
            prior_id, required = delta_requirements.get(
                str(delta_id), ("unknown", 0.0),
            )
            required_by_prior[prior_id] = (
                required_by_prior.get(prior_id, 0.0) + required
            )
        required_processing = max(required_by_prior.values(), default=0.0)
        try:
            scheduled = float(window.get("scheduled_processing_s") or 0)
        except (TypeError, ValueError):
            scheduled = 0.0
        try:
            available = float(window.get("planned_available_s") or 0)
        except (TypeError, ValueError):
            available = 0.0
        normalized_scheduled = max(scheduled, required_processing)
        normalized_available = max(available, normalized_scheduled)
        if (
            normalized_scheduled != scheduled
            or normalized_available != available
        ):
            changes.append({
                "kind": "readability_budget",
                "id": window.get("readability_window_id"),
                "from": {
                    "scheduled_processing_s": scheduled,
                    "planned_available_s": available,
                },
                "to": {
                    "scheduled_processing_s": normalized_scheduled,
                    "planned_available_s": normalized_available,
                },
            })
            window["scheduled_processing_s"] = normalized_scheduled
            window["planned_available_s"] = normalized_available


def _normalize_identity_contract_evidence_refs(
    data: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Drop identity_contract evidence refs that point at ids no longer present in the plan."""
    valid_identity_refs = {
        "source_evidence_ids": {
            str(item.get("source_evidence_id") or "")
            for item in (data.get("source_evidence") or [])
            if isinstance(item, dict)
        },
        "proposition_ids": {
            str(item.get("proposition_id") or "")
            for item in (data.get("propositions") or [])
            if isinstance(item, dict)
        },
        "adaptation_decision_ids": {
            str(item.get("adaptation_decision_id") or "")
            for item in (data.get("adaptation_decisions") or [])
            if isinstance(item, dict)
        },
    }
    for contract in data.get("identity_contracts") or []:
        if not isinstance(contract, dict):
            continue
        evidence = contract.get("evidence")
        if not isinstance(evidence, dict):
            continue
        normalized_fields = {
            field: [
                value
                for value in (evidence.get(field) or [])
                if str(value or "") in valid_ids
            ]
            for field, valid_ids in valid_identity_refs.items()
        }
        if not any(normalized_fields.values()):
            continue
        for field, normalized_values in normalized_fields.items():
            existing_values = list(evidence.get(field) or [])
            if normalized_values == existing_values:
                continue
            changes.append({
                "kind": "identity_evidence_refs",
                "id": contract.get("identity_id"),
                "field": field,
                "from": existing_values,
                "to": normalized_values,
            })
            evidence[field] = normalized_values

