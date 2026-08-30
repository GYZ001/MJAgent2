"""Identity/legacy-relation projection context for
normalize_narrative_storyboard_outline.

Split out of narrative_outline.py -- see that function's docstring and the
module docstring of narrative_outline.py for the overall split map.
``_identity_is_visual_capable``/``_visible_display_names`` were originally
nested closures over ``identity_contracts``; they are promoted to top-level
functions taking it as an explicit parameter because both are called again
much later, inside the per-shot projection phase in
narrative_outline_project.py -- a plain closure can't cross that file
boundary, and CLAUDE.md's guidance is to prefer explicit parameters over
reaching for a closure once a value must outlive its original scope anyway.
"""
from __future__ import annotations

from typing import Any

from app.identity_contracts import (
    identity_ids_in_authority_text,
    storyboard_action_relation_ids,
)
from app.schemas import EpisodeScreenplay


def _identity_is_visual_capable(identity_contracts: dict[str, Any], identity_id: str) -> bool:
    """True unless the identity's visual_policy is offscreen_only."""
    contract = identity_contracts.get(identity_id)
    return contract is None or contract.visual_policy != "offscreen_only"


def _visible_display_names(identity_contracts: dict[str, Any], identity_ids: set[str]) -> list[str]:
    """Display names of the given identity ids that are visually capable, in stable order."""
    return list(dict.fromkeys(
        contract.display_name
        for identity_id, contract in identity_contracts.items()
        if identity_id in identity_ids
        and _identity_is_visual_capable(identity_contracts, identity_id)
    ))


def _prepare_outline_identity_context(
    screenplay: EpisodeScreenplay,
    plan: Any,
    bible: Any,
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any],
    dict[str, set[str]], dict[str, set[str]], list[dict[str, Any]],
    dict[str, str],
]:
    """Build event/action/identity indices and project legacy (pre-onscreen_entity_ids) events' visible-identity relations.

    Returns (events, actions, identity_contracts, legacy_events,
    legacy_event_text_identity_ids, legacy_event_relation_ids,
    legacy_action_relation_changes, compiler_context_identity_names).
    """
    events = {item.event_id: item for item in plan.events}
    actions = {item.action_id: item for item in plan.atomic_actions}
    if bible is not None:
        from app.identity_contracts import narrative_identity_resolver

        identity_contracts = {
            identity.identity_id: identity
            for identity in narrative_identity_resolver(
                bible,
                screenplay,
            ).identities
        }
    else:
        identity_contracts = {
            identity.identity_id: identity
            for identity in plan.identity_contracts
        }
    legacy_events = {
        str(item.event_id or "").strip(): item
        for item in screenplay.events or []
        if str(item.event_id or "").strip()
    }

    def _legacy_visual_identity_ids(event_id: str) -> set[str]:
        legacy = legacy_events.get(event_id)
        if legacy is None:
            return set()
        relation_ids = identity_ids_in_authority_text(
            screenplay,
            "\n".join((
                str(legacy.trigger or ""),
                str(legacy.visible_change or ""),
                str(legacy.state_out or ""),
            )),
            bible=bible,
            strip_dialogue=True,
        )
        return {
            identity_id for identity_id in relation_ids
            if _identity_is_visual_capable(identity_contracts, identity_id)
        }

    legacy_event_text_identity_ids: dict[str, set[str]] = {}
    legacy_event_relation_ids: dict[str, set[str]] = {}
    legacy_action_relation_changes: list[dict[str, Any]] = []
    for event_id, event in events.items():
        if event.onscreen_entity_ids:
            continue
        text_identity_ids = _legacy_visual_identity_ids(event_id)
        relation_ids = set(text_identity_ids)
        for action_id in event.action_ids:
            action = actions.get(action_id)
            if action is None:
                continue
            text_identity_ids.update(
                identity_ids_in_authority_text(
                    screenplay,
                    "\n".join((
                        str(action.semantic_intent or ""),
                        str(action.completion_condition or ""),
                        *(
                            str(phase.start_condition or "")
                            for phase in action.temporal_phases
                        ),
                        *(
                            str(phase.end_condition or "")
                            for phase in action.temporal_phases
                        ),
                    )),
                    bible=bible,
                    strip_dialogue=True,
                )
            )
            relation_ids.update(text_identity_ids)
            projected_actor_ids, projected_target_ids = (
                storyboard_action_relation_ids(
                    screenplay,
                    event_id,
                    action,
                    bible=bible,
                )
            )
            relation_ids.update(projected_actor_ids)
            relation_ids.update(projected_target_ids)
            if projected_actor_ids != action.actor_ids:
                legacy_action_relation_changes.append({
                    "field": f"narrative_plan.atomic_actions.{action_id}.actor_ids",
                    "from": list(action.actor_ids),
                    "to": projected_actor_ids,
                    "reason": "legacy_action_typed_relation_projection",
                })
            if projected_target_ids != action.target_ids:
                legacy_action_relation_changes.append({
                    "field": f"narrative_plan.atomic_actions.{action_id}.target_ids",
                    "from": list(action.target_ids),
                    "to": projected_target_ids,
                    "reason": "legacy_action_typed_relation_projection",
                })
        legacy_event_text_identity_ids[event_id] = set(text_identity_ids)
        legacy_event_relation_ids[event_id] = relation_ids

    compiler_context_identity_names = {
        identity.identity_id: identity.display_name
        for identity in plan.identity_contracts
        if identity.kind == "source_backed_scene_context_actor"
    }
    return (
        events, actions, identity_contracts, legacy_events,
        legacy_event_text_identity_ids, legacy_event_relation_ids,
        legacy_action_relation_changes, compiler_context_identity_names,
    )
