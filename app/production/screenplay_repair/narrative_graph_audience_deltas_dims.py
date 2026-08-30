"""Attention/affective/active-question delta-dimension reconciliation and
no-change-delta pruning for narrative_graph_audience_deltas.py's per-path
reconciliation (P14 of _normalize_screenplay_narrative_graph).

Split out of narrative_graph_normalize.py -- see that file's module
docstring, and narrative_graph_audience_deltas.py's docstring for why this
sibling file exists (keeping both files under the 400-line file-shape
target).
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .narrative_graph_audience_context import _unique_delta_id


def _reconcile_path_attention_delta(
    deltas: list[dict[str, Any]],
    state_in: dict[str, Any],
    state_out: dict[str, Any],
    deadline_event_id: str,
    window_id: Any,
    used_delta_ids: set[str],
    path: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Synthesize or fill the attention-dimension delta if attention residue/working memory changed."""

    def unique_delta_id(path_id: str, suffix: str) -> str:
        return _unique_delta_id(used_delta_ids, path_id, suffix)

    attention_changed = (
        state_in.get("attention_residue_ids")
        != state_out.get("attention_residue_ids")
        or state_in.get("working_memory")
        != state_out.get("working_memory")
    )
    attention_delta = next((
        delta
        for delta in deltas
        if str(delta.get("dimension") or "") == "attention"
    ), None)
    if attention_changed:
        if attention_delta is None:
            attention_delta = {
                "target_delta_id": unique_delta_id(
                    str(path.get("audience_path_id") or "path"),
                    "attention",
                ),
                "dimension": "attention",
                "proposition_ids": sorted({
                    str(item.get("proposition_id") or "")
                    for item in (
                        state_out.get("working_memory") or []
                    )
                    if (
                        isinstance(item, dict)
                        and str(
                            item.get("proposition_id") or ""
                        ).strip()
                    )
                }),
                "description": "绑定注意残留与工作记忆变化",
                "target_confidence": None,
                "required_processing_s": 0.5,
                "deadline_event_id": deadline_event_id,
                "primary_delivery_window_id": window_id,
                "custom_dimension": None,
            }
            deltas.append(attention_delta)
            changes.append({
                "kind": "audience_attention_delta",
                "id": attention_delta["target_delta_id"],
            })
        attention_delta["from_state"] = {
            "attention_residue_ids": list(
                state_in.get("attention_residue_ids") or []
            ),
            "working_memory": deepcopy(
                state_in.get("working_memory") or []
            ),
        }
        attention_delta["to_state"] = {
            "attention_residue_ids": list(
                state_out.get("attention_residue_ids") or []
            ),
            "working_memory": deepcopy(
                state_out.get("working_memory") or []
            ),
        }


def _reconcile_path_affective_delta(
    deltas: list[dict[str, Any]],
    state_in: dict[str, Any],
    state_out: dict[str, Any],
    deadline_event_id: str,
    window_id: Any,
    used_delta_ids: set[str],
    path: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Synthesize or fill the affective-dimension delta if affective_state changed."""

    def unique_delta_id(path_id: str, suffix: str) -> str:
        return _unique_delta_id(used_delta_ids, path_id, suffix)

    if state_in.get("affective_state") != state_out.get(
        "affective_state"
    ):
        affective_delta = next((
            delta
            for delta in deltas
            if str(delta.get("dimension") or "") == "affective"
        ), None)
        if affective_delta is None:
            affective_delta = {
                "target_delta_id": unique_delta_id(
                    str(path.get("audience_path_id") or "path"),
                    "affective",
                ),
                "dimension": "affective",
                "proposition_ids": [],
                "description": "绑定观众入场与目标出场的情绪状态变化",
                "target_confidence": None,
                "required_processing_s": 0.0,
                "deadline_event_id": deadline_event_id,
                "primary_delivery_window_id": window_id,
                "custom_dimension": None,
            }
            deltas.append(affective_delta)
            changes.append({
                "kind": "audience_affective_delta",
                "id": affective_delta["target_delta_id"],
            })
        affective_delta["from_state"] = {
            "affective_state": dict(
                state_in.get("affective_state") or {}
            ),
        }
        affective_delta["to_state"] = {
            "affective_state": dict(
                state_out.get("affective_state") or {}
            ),
        }
        if not affective_delta.get("deadline_event_id"):
            affective_delta["deadline_event_id"] = deadline_event_id
        if (
            not affective_delta.get("primary_delivery_window_id")
            and window_id
        ):
            affective_delta["primary_delivery_window_id"] = window_id
        changes.append({
            "kind": "audience_affective_delta_state",
            "id": affective_delta["target_delta_id"],
        })


def _reconcile_path_question_delta(
    deltas: list[dict[str, Any]],
    state_in: dict[str, Any],
    state_out: dict[str, Any],
    deadline_event_id: str,
    window_id: Any,
    used_delta_ids: set[str],
    path: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Synthesize or fill the active_question_ids delta if it changed."""

    def unique_delta_id(path_id: str, suffix: str) -> str:
        return _unique_delta_id(used_delta_ids, path_id, suffix)

    if (
        state_in.get("active_question_ids")
        != state_out.get("active_question_ids")
    ):
        question_delta = next((
            delta
            for delta in deltas
            if (
                str(delta.get("dimension") or "") == "other"
                and str(delta.get("custom_dimension") or "")
                == "active_question_ids"
            )
        ), None)
        if question_delta is None:
            question_delta = {
                "target_delta_id": unique_delta_id(
                    str(path.get("audience_path_id") or "path"),
                    "questions",
                ),
                "dimension": "other",
                "proposition_ids": [],
                "description": "绑定观众主动问题集合变化",
                "target_confidence": None,
                "required_processing_s": 0.0,
                "deadline_event_id": deadline_event_id,
                "primary_delivery_window_id": window_id,
                "custom_dimension": "active_question_ids",
            }
            deltas.append(question_delta)
            changes.append({
                "kind": "audience_question_delta",
                "id": question_delta["target_delta_id"],
            })
        question_delta["from_state"] = {
            "active_question_ids": list(
                state_in.get("active_question_ids") or []
            ),
        }
        question_delta["to_state"] = {
            "active_question_ids": list(
                state_out.get("active_question_ids") or []
            ),
        }


def _prune_no_change_path_deltas(
    deltas: list[dict[str, Any]],
    state_in: dict[str, Any],
    state_out: dict[str, Any],
    path: dict[str, Any],
    removed_delta_ids: set[str],
    changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop deltas whose from_state/to_state (or belief stance/confidence) show no real change; track their ids as removed."""
    retained_deltas = []
    for delta in deltas:
        semantic_no_change = (
            delta.get("from_state") == delta.get("to_state")
        )
        if str(delta.get("dimension") or "") == "belief":
            proposition_ids = {
                str(item)
                for item in (
                    delta.get("proposition_ids") or []
                )
            }
            before_beliefs = {
                str(item.get("proposition_id") or ""): (
                    item.get("stance"),
                    item.get("confidence"),
                )
                for item in (state_in.get("beliefs") or [])
                if (
                    isinstance(item, dict)
                    and str(
                        item.get("proposition_id") or ""
                    ) in proposition_ids
                )
            }
            after_beliefs = {
                str(item.get("proposition_id") or ""): (
                    item.get("stance"),
                    item.get("confidence"),
                )
                for item in (state_out.get("beliefs") or [])
                if (
                    isinstance(item, dict)
                    and str(
                        item.get("proposition_id") or ""
                    ) in proposition_ids
                )
            }
            semantic_no_change = (
                bool(proposition_ids)
                and all(
                    before_beliefs.get(proposition_id)
                    == after_beliefs.get(proposition_id)
                    for proposition_id in proposition_ids
                )
            )
        if not semantic_no_change:
            retained_deltas.append(delta)
            continue
        delta_id = str(
            delta.get("target_delta_id") or ""
        ).strip()
        if delta_id:
            removed_delta_ids.add(delta_id)
        changes.append({
            "kind": "no_change_target_delta_removed",
            "id": delta_id,
            "path_id": path.get("audience_path_id"),
        })
    return retained_deltas

