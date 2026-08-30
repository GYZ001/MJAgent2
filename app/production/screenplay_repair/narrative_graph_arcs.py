"""Core id-lookup construction, arc-contract, state-fact, evidence-perceiver,
belief-stance and critical-proposition repair phases of
_normalize_screenplay_narrative_graph.

Split out of narrative_graph_normalize.py -- see that file's module
docstring.
"""
from __future__ import annotations

from typing import Any


def _build_core_lookup_dicts(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Index propositions, evidence and events by their id fields.

    Returns (propositions_by_id, evidence_by_id, events_by_id) -- the three
    lookup dicts every later phase of narrative-graph normalization reads
    from (and, for events_by_id/audience state building, mutates through).
    """
    propositions_by_id = {
        str(item.get("proposition_id") or ""): item
        for item in (data.get("propositions") or [])
        if (
            isinstance(item, dict)
            and str(item.get("proposition_id") or "").strip()
        )
    }
    evidence_by_id = {
        str(item.get("evidence_id") or ""): item
        for item in (data.get("evidence") or [])
        if (
            isinstance(item, dict)
            and str(item.get("evidence_id") or "").strip()
        )
    }
    events_by_id = {
        str(item.get("event_id") or ""): item
        for item in (data.get("events") or [])
        if (
            isinstance(item, dict)
            and str(item.get("event_id") or "").strip()
        )
    }
    return propositions_by_id, evidence_by_id, events_by_id


def _normalize_arc_contract_promises(
    data: dict[str, Any],
    propositions_by_id: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Reproject orphan arc promises onto their setup propositions, link payoff contracts, and drop unsupported promises."""
    payoff_contracts_by_id = {
        str(item.get("setup_payoff_id") or ""): item
        for item in (data.get("setup_payoff_contracts") or [])
        if (
            isinstance(item, dict)
            and str(item.get("setup_payoff_id") or "").strip()
        )
    }
    for arc in data.get("arc_contracts") or []:
        if not isinstance(arc, dict):
            continue
        promises = [
            str(item)
            for item in (arc.get("promise_proposition_ids") or [])
            if str(item or "").strip()
        ]
        referenced_payoffs = [
            payoff_contracts_by_id[payoff_id]
            for payoff_id in (arc.get("payoff_contract_ids") or [])
            if payoff_id in payoff_contracts_by_id
        ]
        setup_promises = {
            str(proposition_id)
            for payoff in referenced_payoffs
            for proposition_id in (payoff.get("setup_proposition_ids") or [])
            if str(proposition_id or "") in propositions_by_id
        }
        orphan_promises = [
            proposition_id
            for proposition_id in promises
            if proposition_id not in setup_promises
        ]
        replacements: dict[str, str] = {}
        for proposition_id in orphan_promises:
            matching_payoffs = [
                payoff
                for payoff in referenced_payoffs
                if proposition_id in (
                    payoff.get("intended_inference_ids") or []
                )
            ]
            if len(matching_payoffs) != 1:
                break
            setup_ids = list(dict.fromkeys(
                str(item)
                for item in (
                    matching_payoffs[0].get("setup_proposition_ids") or []
                )
                if str(item or "") in propositions_by_id
            ))
            if len(setup_ids) != 1:
                break
            replacements[proposition_id] = setup_ids[0]
        else:
            if replacements:
                normalized_promises = list(dict.fromkeys(
                    replacements.get(proposition_id, proposition_id)
                    for proposition_id in promises
                ))
                changes.append({
                    "kind": "arc_promise_setup_projection",
                    "id": arc.get("arc_id"),
                    "from": promises,
                    "to": normalized_promises,
                    "inference_to_setup": replacements,
                })
                arc["promise_proposition_ids"] = normalized_promises

        current_promises = [
            str(item)
            for item in (arc.get("promise_proposition_ids") or [])
            if str(item or "").strip()
        ]
        current_payoff_ids = [
            str(item)
            for item in (arc.get("payoff_contract_ids") or [])
            if str(item or "").strip()
        ]
        current_setup_promises = {
            str(proposition_id)
            for payoff_id in current_payoff_ids
            if payoff_id in payoff_contracts_by_id
            for proposition_id in (
                payoff_contracts_by_id[payoff_id].get(
                    "setup_proposition_ids"
                ) or []
            )
        }
        newly_linked_payoffs: list[str] = []
        for proposition_id in current_promises:
            if proposition_id in current_setup_promises:
                continue
            candidates = [
                payoff_id
                for payoff_id, payoff in payoff_contracts_by_id.items()
                if proposition_id in (
                    payoff.get("setup_proposition_ids") or []
                )
            ]
            if len(candidates) == 1 and candidates[0] not in current_payoff_ids:
                newly_linked_payoffs.append(candidates[0])
                current_payoff_ids.append(candidates[0])
                current_setup_promises.add(proposition_id)
        if newly_linked_payoffs:
            changes.append({
                "kind": "arc_payoff_contract_link",
                "id": arc.get("arc_id"),
                "added": newly_linked_payoffs,
            })
            arc["payoff_contract_ids"] = current_payoff_ids

        unsupported_promises = [
            proposition_id
            for proposition_id in current_promises
            if proposition_id not in current_setup_promises
        ]
        if unsupported_promises and arc.get("core_question_ids"):
            supported_promises = [
                proposition_id
                for proposition_id in current_promises
                if proposition_id not in unsupported_promises
            ]
            changes.append({
                "kind": "arc_unsupported_promise_removed",
                "id": arc.get("arc_id"),
                "from": current_promises,
                "to": supported_promises,
                "unsupported": unsupported_promises,
            })
            arc["promise_proposition_ids"] = supported_promises



def _synthesize_missing_effect_facts(
    data: dict[str, Any],
    events_by_id: dict[str, Any],
    actions_by_id: dict[str, Any],
    evidence_by_id: dict[str, Any],
    propositions_by_id: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Synthesize a state_facts entry for any event effects_add fact-id that has no matching fact yet."""
    existing_fact_ids = {
        str(item.get("fact_id") or "")
        for item in (data.get("state_facts") or [])
        if isinstance(item, dict) and str(item.get("fact_id") or "").strip()
    }
    missing_effect_ids = {
        str(fact_id)
        for event in events_by_id.values()
        for fact_id in (event.get("effects_add") or [])
        if str(fact_id or "").strip() not in existing_fact_ids
    }
    for missing_fact_id in sorted(missing_effect_ids):
        producer_events = [
            event
            for event in events_by_id.values()
            if missing_fact_id in (event.get("effects_add") or [])
        ]
        producer_actions = [
            action
            for action in actions_by_id.values()
            if missing_fact_id in (action.get("effects_add") or [])
        ]
        if len(producer_events) != 1 or len(producer_actions) > 1:
            continue
        event = producer_events[0]
        event_id = str(event.get("event_id") or "")
        supported = {
            str(proposition_id)
            for evidence in evidence_by_id.values()
            if (
                isinstance(evidence.get("anchor"), dict)
                and evidence["anchor"].get("type") == "event"
                and str(evidence["anchor"].get("id") or "") == event_id
            )
            for proposition_id in (
                evidence.get("supports_proposition_ids") or []
            )
            if str(proposition_id or "") in propositions_by_id
        }
        candidates = [
            proposition_id
            for proposition_id in (event.get("proposition_ids") or [])
            if proposition_id in supported
        ]
        if len(candidates) != 1:
            continue
        proposition_id = candidates[0]
        proposition = propositions_by_id[proposition_id]
        action = producer_actions[0] if producer_actions else None
        actors = list((action or {}).get("actor_ids") or [])
        if len(actors) != 1:
            continue
        event_position = list(events_by_id).index(event_id) + 1
        fact = {
            "fact_id": missing_fact_id,
            "proposition_id": proposition_id,
            "subject_id": actors[0],
            "predicate_id": str(
                proposition.get("semantic_identity_key")
                or f"state-after-{event_id}"
            ),
            "value": {
                "kind": "text",
                "data": str(proposition.get("canonical_statement") or ""),
            },
            "time_scope": f"main@{event_position}",
            "visibility": "visible",
            "provenance": "screenplay",
            "confidence": 1.0,
        }
        data.setdefault("state_facts", []).append(fact)
        existing_fact_ids.add(missing_fact_id)
        changes.append({
            "kind": "missing_effect_fact",
            "id": missing_fact_id,
            "event_id": event_id,
            "proposition_id": proposition_id,
            "subject_id": actors[0],
        })


def _widen_evidence_perceivers(
    data: dict[str, Any],
    evidence_by_id: dict[str, Any],
    events_by_id: dict[str, Any],
    actions_by_id: dict[str, Any],
    propositions_by_id: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Add a character to evidence.perceivable_by when their belief cites evidence they should plausibly perceive."""
    proposition_entities = {
        proposition_id: {
            str(entity_id)
            for entity_id in (item.get("entity_ids") or [])
            if str(entity_id or "").strip()
        }
        for proposition_id, item in propositions_by_id.items()
    }
    for belief in data.get("character_beliefs") or []:
        if not isinstance(belief, dict):
            continue
        character_id = str(belief.get("character_id") or "").strip()
        if not character_id:
            continue
        for evidence_id in belief.get("perceived_evidence_ids") or []:
            evidence = evidence_by_id.get(str(evidence_id))
            if evidence is None:
                continue
            perceivable = list(evidence.get("perceivable_by") or [])
            if character_id in perceivable:
                continue
            supported_entities = {
                entity_id
                for proposition_id in (
                    evidence.get("supports_proposition_ids") or []
                )
                for entity_id in proposition_entities.get(
                    str(proposition_id),
                    set(),
                )
            }
            anchor = evidence.get("anchor") or {}
            event = events_by_id.get(str(anchor.get("id") or ""))
            event_entities = {
                entity_id
                for action_id in (
                    (event or {}).get("action_ids") or []
                )
                for entity_id in (
                    *((actions_by_id.get(str(action_id)) or {}).get(
                        "actor_ids",
                    ) or []),
                    *((actions_by_id.get(str(action_id)) or {}).get(
                        "target_ids",
                    ) or []),
                )
            }
            if character_id not in supported_entities | event_entities:
                continue
            evidence["perceivable_by"] = [
                *perceivable,
                character_id,
            ]
            changes.append({
                "kind": "evidence_perceiver",
                "id": evidence_id,
                "character_id": character_id,
            })


def _normalize_belief_stance_aliases(
    data: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Canonicalize character/audience belief stance synonyms (e.g. 'confirmed' -> 'believed')."""
    stance_aliases = {
        "accepted": "believed",
        "committed": "believed",
        "confirmed": "believed",
        "disbelieved": "rejected",
        "known": "believed",
        "uncertain": "suspected",
    }
    for collection, id_field in (
        ("character_beliefs", "character_belief_id"),
        ("audience_states", "audience_state_id"),
    ):
        for snapshot in data.get(collection) or []:
            if not isinstance(snapshot, dict):
                continue
            for belief in snapshot.get("beliefs") or []:
                if not isinstance(belief, dict):
                    continue
                stance = str(belief.get("stance") or "").strip()
                normalized = stance_aliases.get(stance, stance)
                if normalized == stance:
                    continue
                changes.append({
                    "kind": "belief_stance",
                    "id": (
                        f"{snapshot.get(id_field)}/"
                        f"{belief.get('proposition_id')}"
                    ),
                    "from": stance,
                    "to": normalized,
                })
                belief["stance"] = normalized


def _attach_missing_critical_propositions(
    data: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Attach propositions from causally-critical events to the nearest attention delta if untracked."""
    events = [
        event for event in (data.get("events") or [])
        if isinstance(event, dict)
    ]
    causal_parent_ids = {
        str(parent_id)
        for event in events
        for parent_id in (event.get("causal_parent_ids") or [])
        if str(parent_id or "").strip()
    }
    critical_event_ids = {
        str(event.get("event_id") or "")
        for event in events
        if (
            event.get("downstream_dependency_event_ids")
            or str(event.get("event_id") or "") in causal_parent_ids
        )
    }
    intended = {
        str(proposition_id)
        for intent in (data.get("experience_intents") or [])
        if isinstance(intent, dict)
        for path in (intent.get("audience_paths") or [])
        if isinstance(path, dict)
        for delta in (path.get("target_deltas") or [])
        if isinstance(delta, dict)
        for proposition_id in (delta.get("proposition_ids") or [])
        if str(proposition_id or "").strip()
    }
    withheld = {
        str(item.get("proposition_id") or "")
        for intent in (data.get("experience_intents") or [])
        if isinstance(intent, dict)
        for item in (intent.get("withheld_propositions") or [])
        if isinstance(item, dict) and str(item.get("proposition_id") or "").strip()
    }
    missing_critical = {
        str(proposition_id)
        for event in events
        if str(event.get("event_id") or "") in critical_event_ids
        for proposition_id in (event.get("proposition_ids") or [])
        if str(proposition_id or "").strip() not in intended | withheld
    }
    if missing_critical:
        for intent in data.get("experience_intents") or []:
            if not isinstance(intent, dict):
                continue
            for path in intent.get("audience_paths") or []:
                if not isinstance(path, dict):
                    continue
                attention_deltas = [
                    delta
                    for delta in (path.get("target_deltas") or [])
                    if isinstance(delta, dict)
                    and str(delta.get("dimension") or "") == "attention"
                ]
                if len(attention_deltas) != 1:
                    continue
                delta = attention_deltas[0]
                existing = [
                    str(item)
                    for item in (delta.get("proposition_ids") or [])
                    if str(item or "").strip()
                ]
                normalized = list(dict.fromkeys([
                    *existing,
                    *sorted(missing_critical),
                ]))
                if normalized == existing:
                    continue
                changes.append({
                    "kind": "critical_proposition_intent",
                    "id": delta.get("target_delta_id"),
                    "from": existing,
                    "to": normalized,
                })
                delta["proposition_ids"] = normalized

