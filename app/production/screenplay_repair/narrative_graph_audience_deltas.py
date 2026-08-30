"""Per-audience-path target-delta reconciliation phase (P14) of
_normalize_screenplay_narrative_graph: outer loop, per-path setup/dispatch,
and the belief-dimension reconciliation (the largest of the four
dimensions).

Split out of narrative_graph_normalize.py -- see that file's module
docstring. The original phase was one loop over (intent, path) pairs with a
~400-line body; the body is split here by the four delta "dimensions" it
reconciles (belief / attention / affective / active-question) plus the
shared setup and the final no-change-delta pruning, since those are the
real internal seams (each dimension's block was already delimited by blank
lines in the source and reads/writes disjoint delta entries). The other
three dimensions and the pruning step live in
narrative_graph_audience_deltas_dims.py, to keep both files under the
400-line file-shape target.
"""
from __future__ import annotations

from typing import Any

from .narrative_graph_audience_context import (
    _AudienceExperienceContext,
    _unique_delta_id,
)
from .narrative_graph_audience_deltas_dims import (
    _prune_no_change_path_deltas,
    _reconcile_path_affective_delta,
    _reconcile_path_attention_delta,
    _reconcile_path_question_delta,
)


def _reconcile_audience_path_deltas(
    ctx: _AudienceExperienceContext,
    changes: list[dict[str, Any]],
) -> None:
    """Reconcile target_deltas for every audience_path of every experience_intent."""
    for intent in ctx.intent_items:
        if not isinstance(intent, dict):
            continue
        for path in intent.get("audience_paths") or []:
            if not isinstance(path, dict):
                continue
            _reconcile_single_audience_path_deltas(intent, path, ctx, changes)


def _reconcile_single_audience_path_deltas(
    intent: dict[str, Any],
    path: dict[str, Any],
    ctx: _AudienceExperienceContext,
    changes: list[dict[str, Any]],
) -> None:
    """Resolve one audience_path's in/out states, then reconcile each delta dimension for it."""
    audience_states_by_id = ctx.audience_states_by_id
    audience_priors_by_id = ctx.audience_priors_by_id
    evidence_for_proposition = ctx.evidence_for_proposition
    used_delta_ids = ctx.used_delta_ids
    removed_delta_ids = ctx.removed_delta_ids

    state_in = audience_states_by_id.get(
        str(path.get("audience_state_in_id") or "")
    )
    state_out = audience_states_by_id.get(
        str(path.get("audience_state_out_target_id") or "")
    )
    if state_in is None or state_out is None:
        return
    deltas = [
        item
        for item in (path.get("target_deltas") or [])
        if isinstance(item, dict)
    ]
    path["target_deltas"] = deltas
    template = deltas[0] if deltas else {}
    deadline_event_id = str(
        template.get("deadline_event_id")
        or (intent.get("anchor_event_ids") or [""])[-1]
    )
    window_id = template.get("primary_delivery_window_id")

    incoming_beliefs = {
        str(item.get("proposition_id") or ""): item
        for item in (state_in.get("beliefs") or [])
        if isinstance(item, dict)
    }
    prior = audience_priors_by_id.get(
        str(path.get("audience_prior_id") or "")
    ) or {}
    assumed_unknown = {
        str(item)
        for item in (
            prior.get("assumed_unknown_proposition_ids") or []
        )
    }
    outgoing_beliefs = {
        str(item.get("proposition_id") or ""): item
        for item in (state_out.get("beliefs") or [])
        if isinstance(item, dict)
    }

    _reconcile_path_belief_deltas(
        deltas, state_in, state_out, prior, assumed_unknown,
        incoming_beliefs, outgoing_beliefs, evidence_for_proposition,
        deadline_event_id, window_id, used_delta_ids, path, changes,
    )
    _reconcile_path_attention_delta(
        deltas, state_in, state_out, deadline_event_id, window_id,
        used_delta_ids, path, changes,
    )
    _reconcile_path_affective_delta(
        deltas, state_in, state_out, deadline_event_id, window_id,
        used_delta_ids, path, changes,
    )
    _reconcile_path_question_delta(
        deltas, state_in, state_out, deadline_event_id, window_id,
        used_delta_ids, path, changes,
    )
    path["target_deltas"] = _prune_no_change_path_deltas(
        deltas, state_in, state_out, path, removed_delta_ids, changes,
    )


def _reconcile_path_belief_deltas(
    deltas: list[dict[str, Any]],
    state_in: dict[str, Any],
    state_out: dict[str, Any],
    prior: dict[str, Any],
    assumed_unknown: set[str],
    incoming_beliefs: dict[str, Any],
    outgoing_beliefs: dict[str, Any],
    evidence_for_proposition: dict[str, list[str]],
    deadline_event_id: str,
    window_id: Any,
    used_delta_ids: set[str],
    path: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Fill belief-dimension deltas' from/to state snapshots and synthesize a delta for any unaccounted belief change."""

    def unique_delta_id(path_id: str, suffix: str) -> str:
        return _unique_delta_id(used_delta_ids, path_id, suffix)

    for delta in deltas:
        if str(delta.get("dimension") or "") != "belief":
            continue
        for proposition_id in delta.get("proposition_ids") or []:
            proposition_id = str(proposition_id)
            if (
                proposition_id in incoming_beliefs
                or proposition_id not in assumed_unknown
            ):
                continue
            unknown_belief = {
                "proposition_id": proposition_id,
                "stance": "unknown",
                "confidence": 0.0,
                "evidence_ids": [],
            }
            state_in.setdefault("beliefs", []).append(
                unknown_belief
            )
            incoming_beliefs[proposition_id] = unknown_belief
            changes.append({
                "kind": "audience_prior_unknown_belief",
                "id": state_in.get("audience_state_id"),
                "proposition_id": proposition_id,
            })
        for proposition_id in delta.get("proposition_ids") or []:
            proposition_id = str(proposition_id)
            if proposition_id in outgoing_beliefs:
                continue
            target_confidence = float(
                delta.get("target_confidence")
                if delta.get("target_confidence") is not None
                else 1.0
            )
            outgoing_beliefs[proposition_id] = {
                "proposition_id": proposition_id,
                "stance": "believed",
                "confidence": target_confidence,
                "evidence_ids": evidence_for_proposition.get(
                    proposition_id,
                    [],
                ),
            }
            state_out.setdefault("beliefs", []).append(
                outgoing_beliefs[proposition_id]
            )
            changes.append({
                "kind": "audience_target_belief",
                "id": state_out.get("audience_state_id"),
                "proposition_id": proposition_id,
            })
        proposition_ids = [
            str(item)
            for item in (delta.get("proposition_ids") or [])
        ]
        delta["from_state"] = {
            "beliefs": [
                item
                for item in (state_in.get("beliefs") or [])
                if (
                    isinstance(item, dict)
                    and str(item.get("proposition_id") or "")
                    in proposition_ids
                )
            ],
        }
        delta["to_state"] = {
            "beliefs": [
                item
                for item in (state_out.get("beliefs") or [])
                if (
                    isinstance(item, dict)
                    and str(item.get("proposition_id") or "")
                    in proposition_ids
                )
            ],
        }

    changed_belief_ids = {
        proposition_id
        for proposition_id in (
            set(incoming_beliefs) | set(outgoing_beliefs)
        )
        if incoming_beliefs.get(proposition_id)
        != outgoing_beliefs.get(proposition_id)
    }
    covered_belief_ids = {
        str(proposition_id)
        for delta in deltas
        if str(delta.get("dimension") or "") == "belief"
        for proposition_id in (
            delta.get("proposition_ids") or []
        )
    }
    missing_belief_ids = sorted(
        changed_belief_ids - covered_belief_ids
    )
    if missing_belief_ids:
        delta = {
            "target_delta_id": unique_delta_id(
                str(path.get("audience_path_id") or "path"),
                "belief",
            ),
            "dimension": "belief",
            "proposition_ids": missing_belief_ids,
            "description": "绑定该观众路径中实际发生的信念变化",
            "from_state": {
                "beliefs": [
                    item
                    for item in (state_in.get("beliefs") or [])
                    if (
                        isinstance(item, dict)
                        and str(item.get("proposition_id") or "")
                        in missing_belief_ids
                    )
                ],
            },
            "to_state": {
                "beliefs": [
                    item
                    for item in (state_out.get("beliefs") or [])
                    if (
                        isinstance(item, dict)
                        and str(item.get("proposition_id") or "")
                        in missing_belief_ids
                    )
                ],
            },
            "target_confidence": None,
            "required_processing_s": 0.5,
            "deadline_event_id": deadline_event_id,
            "primary_delivery_window_id": window_id,
            "custom_dimension": None,
        }
        deltas.append(delta)
        changes.append({
            "kind": "audience_belief_delta",
            "id": delta["target_delta_id"],
            "proposition_ids": missing_belief_ids,
        })

