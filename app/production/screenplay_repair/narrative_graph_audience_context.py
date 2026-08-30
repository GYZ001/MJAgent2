"""Shared mutable state for the coarse audience-experience-path synthesis
phases of _normalize_screenplay_narrative_graph (P12/P13), plus those two
phases themselves.

Split out of narrative_graph_normalize.py -- see that file's module
docstring. ``_AudienceExperienceContext`` bundles the id-lookup dicts and
running sets that P12 (coarse per-prior audience paths), P13 (coarse
per-scene audience paths) and, downstream, the delta-reconciliation phase in
narrative_graph_audience_deltas.py all read and mutate in place -- passing
the same dict/set/list objects through every phase reproduces the original
single-function's shared-local-variable semantics exactly (mutations made in
one phase are visible to the next because they're the same objects, not
copies).
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass
class _AudienceExperienceContext:
    """Mutable lookup/tracking state threaded through P12, P13 and P14."""

    audience_states_by_id: dict[str, Any]
    audience_priors_by_id: dict[str, Any]
    evidence_for_proposition: dict[str, list[str]]
    used_delta_ids: set[str]
    removed_delta_ids: set[str]
    intent_items: list[dict[str, Any]]
    prior_ids: list[str]
    states_by_prior: dict[str, list[str]]


def _unique_delta_id(used_delta_ids: set[str], path_id: str, suffix: str) -> str:
    """Mint a target_delta_id unused so far, mutating used_delta_ids to reserve it."""
    base = f"{path_id}-{suffix}"
    value = base
    counter = 2
    while value in used_delta_ids:
        value = f"{base}-{counter}"
        counter += 1
    used_delta_ids.add(value)
    return value


def _build_audience_experience_context(
    data: dict[str, Any],
    evidence_by_id: dict[str, Any],
    propositions_by_id: dict[str, Any],
) -> _AudienceExperienceContext:
    """Build the shared lookup/tracking state for P12-P14 from the current plan data."""
    audience_states_by_id = {
        str(item.get("audience_state_id") or ""): item
        for item in (data.get("audience_states") or [])
        if (
            isinstance(item, dict)
            and str(item.get("audience_state_id") or "").strip()
        )
    }
    audience_priors_by_id = {
        str(item.get("audience_prior_id") or ""): item
        for item in (data.get("audience_priors") or [])
        if (
            isinstance(item, dict)
            and str(item.get("audience_prior_id") or "").strip()
        )
    }
    evidence_for_proposition = {
        proposition_id: [
            evidence_id
            for evidence_id, evidence in evidence_by_id.items()
            if proposition_id in (
                evidence.get("supports_proposition_ids") or []
            )
        ]
        for proposition_id in propositions_by_id
    }
    used_delta_ids = {
        str(delta.get("target_delta_id") or "")
        for intent in (data.get("experience_intents") or [])
        if isinstance(intent, dict)
        for path in (intent.get("audience_paths") or [])
        if isinstance(path, dict)
        for delta in (path.get("target_deltas") or [])
        if (
            isinstance(delta, dict)
            and str(delta.get("target_delta_id") or "").strip()
        )
    }
    removed_delta_ids: set[str] = set()
    intent_items = [
        intent
        for intent in (data.get("experience_intents") or [])
        if isinstance(intent, dict)
    ]
    prior_ids = [
        str(prior.get("audience_prior_id") or "").strip()
        for prior in (data.get("audience_priors") or [])
        if (
            isinstance(prior, dict)
            and str(prior.get("audience_prior_id") or "").strip()
        )
    ]
    states_by_prior: dict[str, list[str]] = {}
    for state_id, state in audience_states_by_id.items():
        prior_id = str(state.get("audience_prior_id") or "").strip()
        if prior_id:
            states_by_prior.setdefault(prior_id, []).append(state_id)
    return _AudienceExperienceContext(
        audience_states_by_id=audience_states_by_id,
        audience_priors_by_id=audience_priors_by_id,
        evidence_for_proposition=evidence_for_proposition,
        used_delta_ids=used_delta_ids,
        removed_delta_ids=removed_delta_ids,
        intent_items=intent_items,
        prior_ids=prior_ids,
        states_by_prior=states_by_prior,
    )


def _synthesize_coarse_audience_paths_by_prior(
    data: dict[str, Any],
    ctx: _AudienceExperienceContext,
    events_by_id: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Synthesize a coarse audience_path for any (intent, prior) pair missing one, from the nearest known state."""
    audience_states_by_id = ctx.audience_states_by_id
    states_by_prior = ctx.states_by_prior
    intent_items = ctx.intent_items
    prior_ids = ctx.prior_ids
    used_delta_ids = ctx.used_delta_ids

    def unique_delta_id(path_id: str, suffix: str) -> str:
        return _unique_delta_id(used_delta_ids, path_id, suffix)

    used_path_ids = {
        str(path.get("audience_path_id") or "").strip()
        for intent in intent_items
        for path in (intent.get("audience_paths") or [])
        if (
            isinstance(path, dict)
            and str(path.get("audience_path_id") or "").strip()
        )
    }
    current_state_by_prior: dict[str, str] = {}
    for intent_index, intent in enumerate(intent_items, start=1):
        paths = [
            path
            for path in (intent.get("audience_paths") or [])
            if isinstance(path, dict)
        ]
        intent["audience_paths"] = paths
        paths_by_prior = {
            str(path.get("audience_prior_id") or "").strip(): path
            for path in paths
            if str(path.get("audience_prior_id") or "").strip()
        }
        for prior_id in prior_ids:
            if prior_id in paths_by_prior:
                continue
            state_id = current_state_by_prior.get(prior_id)
            if not state_id:
                state_id = next(
                    (
                        str(path.get("audience_state_in_id") or "")
                        for later_intent in intent_items[intent_index:]
                        for path in (
                            later_intent.get("audience_paths") or []
                        )
                        if (
                            isinstance(path, dict)
                            and str(
                                path.get("audience_prior_id") or ""
                            ).strip() == prior_id
                            and str(
                                path.get("audience_state_in_id") or ""
                            ).strip()
                        )
                    ),
                    "",
                )
            if not state_id:
                state_id = next(
                    iter(states_by_prior.get(prior_id) or []),
                    "",
                )
            if not state_id:
                continue
            base_path_id = (
                f"XP-{prior_id}-{intent.get('experience_intent_id')}"
            )
            path_id = base_path_id
            suffix = 2
            while path_id in used_path_ids:
                path_id = f"{base_path_id}-{suffix}"
                suffix += 1
            used_path_ids.add(path_id)
            target_state_id = state_id
            target_deltas: list[dict[str, Any]] = []
            attention_targets = [
                str(item)
                for item in (
                    intent.get("attention_target_ids") or []
                )
                if str(item or "").strip()
            ]
            source_state = audience_states_by_id.get(state_id)
            anchor_event_id = str(
                (intent.get("anchor_event_ids") or [""])[-1]
            )
            if attention_targets and source_state is not None:
                base_state_id = (
                    f"AS-{prior_id}-"
                    f"{intent.get('experience_intent_id')}-COARSE"
                )
                target_state_id = base_state_id
                state_suffix = 2
                while target_state_id in audience_states_by_id:
                    target_state_id = (
                        f"{base_state_id}-{state_suffix}"
                    )
                    state_suffix += 1
                target_state = deepcopy(source_state)
                target_state["audience_state_id"] = target_state_id
                target_state["anchor"] = {
                    "type": "event",
                    "id": anchor_event_id,
                }
                before_attention = list(
                    source_state.get("attention_residue_ids") or []
                )
                after_attention = list(dict.fromkeys([
                    *before_attention,
                    *attention_targets,
                ]))
                before_memory = deepcopy(
                    source_state.get("working_memory") or []
                )
                after_memory = deepcopy(before_memory)
                if after_attention == before_attention:
                    remembered = {
                        str(item.get("proposition_id") or "")
                        for item in after_memory
                        if isinstance(item, dict)
                    }
                    for proposition_id in attention_targets:
                        if proposition_id in remembered:
                            continue
                        after_memory.append({
                            "proposition_id": proposition_id,
                            "retention_confidence": 0.7,
                        })
                target_state["attention_residue_ids"] = after_attention
                target_state["working_memory"] = after_memory
                data.setdefault("audience_states", []).append(
                    target_state
                )
                audience_states_by_id[target_state_id] = target_state
                states_by_prior.setdefault(prior_id, []).append(
                    target_state_id
                )
                delta_id = unique_delta_id(path_id, "attention")
                event = events_by_id.get(anchor_event_id) or {}
                target_deltas.append({
                    "target_delta_id": delta_id,
                    "dimension": "attention",
                    "proposition_ids": attention_targets,
                    "description": (
                        "为缺失先验路径登记当前意图的注意目标"
                    ),
                    "from_state": {
                        "attention_residue_ids": before_attention,
                        "working_memory": before_memory,
                    },
                    "to_state": {
                        "attention_residue_ids": after_attention,
                        "working_memory": after_memory,
                    },
                    "target_confidence": None,
                    "required_processing_s": 0.5,
                    "deadline_event_id": anchor_event_id,
                    "primary_delivery_window_id": event.get(
                        "primary_delivery_window_id"
                    ),
                    "custom_dimension": None,
                })
            path = {
                "audience_path_id": path_id,
                "audience_prior_id": prior_id,
                "audience_state_in_id": state_id,
                "audience_state_out_target_id": target_state_id,
                "target_deltas": target_deltas,
            }
            paths.append(path)
            paths_by_prior[prior_id] = path
            changes.append({
                "kind": "coarse_audience_path",
                "id": path_id,
                "experience_intent_id": intent.get(
                    "experience_intent_id"
                ),
                "audience_prior_id": prior_id,
                "state_in_id": state_id,
                "state_out_id": target_state_id,
            })
        for prior_id, path in paths_by_prior.items():
            state_id = str(
                path.get("audience_state_out_target_id") or ""
            ).strip()
            if state_id:
                current_state_by_prior[prior_id] = state_id


def _synthesize_coarse_scene_audience_paths(
    data: dict[str, Any],
    ctx: _AudienceExperienceContext,
    changes: list[dict[str, Any]],
) -> None:
    """Synthesize a coarse audience_state_path on each scene_contract for any prior missing one."""
    intent_items = ctx.intent_items
    prior_ids = ctx.prior_ids
    states_by_prior = ctx.states_by_prior

    intent_paths_by_event_prior: dict[
        tuple[str, str],
        tuple[str, str],
    ] = {}
    for intent in intent_items:
        anchor_event_ids = [
            str(value or "").strip()
            for value in (intent.get("anchor_event_ids") or [])
            if str(value or "").strip()
        ]
        for path in intent.get("audience_paths") or []:
            if not isinstance(path, dict):
                continue
            prior_id = str(path.get("audience_prior_id") or "").strip()
            state_in_id = str(path.get("audience_state_in_id") or "").strip()
            state_out_id = str(
                path.get("audience_state_out_target_id") or ""
            ).strip()
            if not prior_id or not state_in_id or not state_out_id:
                continue
            for event_id in anchor_event_ids:
                intent_paths_by_event_prior[(event_id, prior_id)] = (
                    state_in_id,
                    state_out_id,
                )

    for scene in data.get("scene_contracts") or []:
        if not isinstance(scene, dict):
            continue
        paths = [
            path
            for path in (scene.get("audience_state_paths") or [])
            if isinstance(path, dict)
        ]
        scene["audience_state_paths"] = paths
        existing_priors = {
            str(path.get("audience_prior_id") or "").strip()
            for path in paths
        }
        scene_event_ids = [
            str(value or "").strip()
            for value in (scene.get("turn_event_ids") or [])
            if str(value or "").strip()
        ]
        for prior_id in prior_ids:
            if prior_id in existing_priors:
                continue
            scene_transitions = [
                intent_paths_by_event_prior[(event_id, prior_id)]
                for event_id in scene_event_ids
                if (event_id, prior_id) in intent_paths_by_event_prior
            ]
            if scene_transitions:
                state_in_id = scene_transitions[0][0]
                state_out_id = scene_transitions[-1][1]
            else:
                # Without an event-local transition, only the earliest known
                # state is temporally safe. The episode-final state may contain
                # facts learned in later scenes.
                state_in_id = next(
                    iter(states_by_prior.get(prior_id) or []),
                    "",
                )
                state_out_id = state_in_id
            if not state_in_id or not state_out_id:
                continue
            paths.append({
                "audience_prior_id": prior_id,
                "audience_state_in_id": state_in_id,
                "audience_state_out_target_id": state_out_id,
            })
            changes.append({
                "kind": "coarse_scene_audience_path",
                "id": scene.get("scene_id"),
                "audience_prior_id": prior_id,
                "state_in_id": state_in_id,
                "state_out_id": state_out_id,
            })

