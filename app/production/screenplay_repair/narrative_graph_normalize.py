"""_normalize_screenplay_narrative_graph: repairs exact source offsets and
unambiguous event-ID/punctuation drift across an entire narrative_plan graph
in one deterministic pass.

Split out of app/production/screenplay_repair.py. This file holds exactly one
top-level function (~1,620 lines) -- it is kept as one function verbatim
(moved, not rewritten) rather than refactored into smaller pieces, so this
file exceeds the usual 600-line/200-function-line file-shape targets; see the
package's split report for why further splitting was out of scope here.
"""
from __future__ import annotations

import re
from app.schemas import EpisodeScreenplay
from copy import deepcopy
from typing import Any

from .dialogue_chain_repair import _normalize_dialogue_chain_continuity
from .dialogue_source_alignment import (
    _normalize_dialogue_lines_to_source,
    _source_evidence_span,
)


def _normalize_screenplay_narrative_graph(
    script: EpisodeScreenplay,
    *,
    authorized_source_chapters: dict[str, str] | None,
) -> list[dict[str, Any]]:
    """Repair exact source offsets and unambiguous event-ID punctuation drift."""
    plan = script.narrative_plan
    if plan is None:
        return []
    data = plan.model_dump(mode="json")
    changes: list[dict[str, Any]] = []
    from app.validators import normalize_screenplay_dialogue_chains

    before_dialogue_chains = [
        chain.model_dump(mode="json")
        for chain in (script.dialogue_chains or [])
    ]
    before_full_script_text = script.full_script_text
    normalize_screenplay_dialogue_chains(script)
    after_dialogue_chains = [
        chain.model_dump(mode="json")
        for chain in (script.dialogue_chains or [])
    ]
    if (
        after_dialogue_chains != before_dialogue_chains
        or script.full_script_text != before_full_script_text
    ):
        changes.append({
            "kind": "dialogue_chain_normalization",
            "before_chain_count": len(before_dialogue_chains),
            "after_chain_count": len(after_dialogue_chains),
            "full_script_text_changed": (
                script.full_script_text != before_full_script_text
            ),
        })

    for index, chain in enumerate(script.dialogue_chains or []):
        topic = (chain.topic or "").strip()
        if len(topic) >= 4 or not chain.turns:
            continue
        speakers = list(dict.fromkeys(
            (turn.speaker or "").strip()
            for turn in chain.turns
            if (turn.speaker or "").strip()
        ))
        subject = (chain.turns[0].line or "").strip()[:16].strip("，。！？ ")
        normalized_topic = (
            f"{'与'.join(speakers[:2]) or '角色'}围绕"
            f"{subject or '当前事件'}的对话"
        )
        changes.append({
            "kind": "dialogue_topic",
            "id": chain.chain_id or f"dialogue-chain-{index}",
            "from": topic,
            "to": normalized_topic,
        })
        chain.topic = normalized_topic

    raw_chapters = (
        authorized_source_chapters
        if isinstance(authorized_source_chapters, dict)
        else {}
    )
    chapters = {
        str(chapter_id): str(text)
        for chapter_id, text in raw_chapters.items()
        if str(chapter_id).strip() and str(text)
    }
    dialogue_source = "\n".join(dict.fromkeys(chapters.values()))
    dialogue_changes = _normalize_dialogue_lines_to_source(
        script,
        dialogue_source,
    )
    changes.extend(dialogue_changes)
    continuity_changes = _normalize_dialogue_chain_continuity(
        script,
        dialogue_source,
    )
    changes.extend(continuity_changes)
    if dialogue_changes or continuity_changes:
        normalize_screenplay_dialogue_chains(script)
    source_contexts: dict[str, list[str]] = {}
    for proposition in data.get("propositions") or []:
        if not isinstance(proposition, dict):
            continue
        statement = str(proposition.get("canonical_statement") or "").strip()
        if not statement:
            continue
        for evidence_id in proposition.get("direct_source_evidence_ids") or []:
            source_contexts.setdefault(str(evidence_id), []).append(statement)
    for index, evidence in enumerate(data.get("source_evidence") or []):
        if not isinstance(evidence, dict):
            continue
        span = evidence.get("source_span")
        excerpt = str(evidence.get("verbatim_excerpt") or "")
        if not isinstance(span, dict) or not excerpt:
            continue
        evidence_id = evidence.get("source_evidence_id") or f"source-{index}"
        context = " ".join(source_contexts.get(str(evidence_id), []))
        chapter_id = str(span.get("chapter_id") or "")
        chapter = chapters.get(chapter_id)
        resolved = (
            _source_evidence_span(chapter, excerpt, context=context)
            if chapter is not None
            else None
        )
        if chapter is None:
            candidates = (
                [(candidate_id, None) for candidate_id in chapters]
                if len(chapters) == 1
                else [
                    (candidate_id, candidate)
                    for candidate_id, candidate_text in chapters.items()
                    if (
                        candidate := _source_evidence_span(
                            candidate_text,
                            excerpt,
                            context=context,
                        )
                    ) is not None
                ]
            )
            if len(candidates) != 1:
                continue
            chapter_id, resolved = candidates[0]
            chapter = chapters[chapter_id]
            if resolved is None:
                resolved = _source_evidence_span(
                    chapter,
                    excerpt,
                    context=context,
                )
            changes.append({
                "kind": "source_chapter",
                "id": evidence_id,
                "from": span.get("chapter_id"),
                "to": chapter_id,
            })
            span["chapter_id"] = chapter_id
        if resolved is None:
            continue
        start, end, expanded_excerpt = resolved
        if expanded_excerpt is not None and expanded_excerpt != excerpt:
            changes.append({
                "kind": "source_excerpt_expanded",
                "id": evidence_id,
                "from_chars": len(excerpt),
                "to_chars": len(expanded_excerpt),
            })
            evidence["verbatim_excerpt"] = expanded_excerpt
        if span.get("start") != start or span.get("end") != end:
            changes.append({
                "kind": "source_span",
                "id": evidence_id,
                "from": {"start": span.get("start"), "end": span.get("end")},
                "to": {"start": start, "end": end},
            })
            span["start"] = start
            span["end"] = end

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
    actions_by_id = {
        str(item.get("action_id") or "").strip(): item
        for item in (data.get("atomic_actions") or [])
        if (
            isinstance(item, dict)
            and str(item.get("action_id") or "").strip()
        )
    }
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

    def unique_delta_id(path_id: str, suffix: str) -> str:
        base = f"{path_id}-{suffix}"
        value = base
        counter = 2
        while value in used_delta_ids:
            value = f"{base}-{counter}"
            counter += 1
        used_delta_ids.add(value)
        return value

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

    for intent in intent_items:
        if not isinstance(intent, dict):
            continue
        for path in intent.get("audience_paths") or []:
            if not isinstance(path, dict):
                continue
            state_in = audience_states_by_id.get(
                str(path.get("audience_state_in_id") or "")
            )
            state_out = audience_states_by_id.get(
                str(path.get("audience_state_out_target_id") or "")
            )
            if state_in is None or state_out is None:
                continue
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
            path["target_deltas"] = retained_deltas

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

    if changes:
        script.narrative_plan = type(plan).model_validate(data)
    return changes


