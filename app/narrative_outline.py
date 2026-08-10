"""Deterministic ShotTask projection from an approved narrative graph."""
from __future__ import annotations

from collections import defaultdict
from math import ceil
import re
from typing import Any

from app import config
from app.identity_contracts import (
    identity_ids_in_authority_text,
    storyboard_action_relation_ids,
)
from app.narrative import (
    _target_state_fragment_matches,
    action_participant_delivery_errors,
)
from app.schemas import (
    AudienceStatePathRef,
    Bible,
    EpisodeScreenplay,
    NarrativeBoundaryContract,
    ShotCapacityBudget,
    ShotContribution,
    StoryboardOutline,
    StoryboardOutlineShot,
)


def _relation_text(value: object) -> str:
    """Normalize text only for exact graph-relation joins."""
    return "".join(
        character.casefold()
        for character in str(value or "")
        if character.isalnum()
    )


def _narrative_key_line_catalog(
    screenplay: EpisodeScreenplay,
) -> dict[str, tuple[str, str, str]]:
    """Return stable key-line IDs with their canonical speaker and line."""
    catalog: dict[str, tuple[str, str, str]] = {}
    for position, raw_line in enumerate(screenplay.key_lines or [], start=1):
        text = str(raw_line or "").strip()
        speaker, separator, spoken = text.partition("：")
        if not separator:
            speaker, separator, spoken = text.partition(":")
        if (
            separator
            and speaker.strip()
            and spoken.strip()
            and speaker.strip() != "旁白"
        ):
            catalog[f"KL{position:02d}"] = (
                speaker.strip(),
                spoken.strip(),
                text,
            )
    return catalog


def _action_key_line_ids(
    action_ids: list[str],
    actions: dict[str, Any],
    catalog: dict[str, tuple[str, str, str]],
) -> list[str]:
    """Join dialogue to actions by exact speaker and spoken-text relations."""
    action_parts = [
        text
        for action_id in action_ids
        for action in [actions.get(action_id)]
        if action is not None
        for text in (
            str(action.semantic_intent or "").strip(),
            str(action.completion_condition or "").strip(),
        )
        if text
    ]
    action_text = _relation_text("；".join(action_parts))
    if not action_text:
        return []
    quoted_lines = {
        _relation_text(match)
        for part in action_parts
        for match in re.findall(r"[「“『\"]([^」”』\"]+)[」”』\"]", part)
        if _relation_text(match)
    }
    catalog_entries = [
        (key_id, _relation_text(speaker), _relation_text(spoken))
        for key_id, (speaker, spoken, _canonical) in catalog.items()
        if _relation_text(speaker) and _relation_text(spoken)
    ]
    matched_modes: dict[str, str] = {}
    for quoted_line in quoted_lines:
        exact_ids = [
            key_id
            for key_id, speaker_relation, spoken_relation in catalog_entries
            if speaker_relation in action_text and spoken_relation == quoted_line
        ]
        if exact_ids:
            matched_modes.update((key_id, "exact") for key_id in exact_ids)
            continue
        # A long canonical turn may be split into adjacent key lines. Accept
        # fragments only when the complete contiguous sequence rebuilds it.
        for start, (_key_id, speaker_relation, _spoken_relation) in enumerate(
            catalog_entries
        ):
            if speaker_relation not in action_text:
                continue
            combined = ""
            fragment_ids: list[str] = []
            for key_id, candidate_speaker, spoken_relation in catalog_entries[start:]:
                if candidate_speaker != speaker_relation:
                    break
                combined += spoken_relation
                if not quoted_line.startswith(combined):
                    break
                fragment_ids.append(key_id)
                if combined == quoted_line:
                    if len(fragment_ids) >= 2:
                        for fragment_id in fragment_ids:
                            matched_modes.setdefault(fragment_id, "contiguous_fragments")
                    break
    return [
        key_id
        for key_id, _speaker, _spoken in catalog_entries
        if key_id in matched_modes
    ]


def reconcile_narrative_outline_action_deliveries(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    *,
    infer_unprojected_event_actions: bool = False,
) -> list[dict[str, Any]]:
    """Keep event dialogue IDs bound to the actions that explicitly own them.

    Plot-spine delivery IDs are a compatibility projection and can drift from
    the newer narrative graph.  When an atomic action explicitly contains both
    a canonical speaker and canonical spoken line, that exact relation is the
    authority.  Dialogue-only capacity splits retain ownership; all other
    stale IDs are removed from the event before duration and identity fields
    are derived.
    """
    plan = screenplay.narrative_plan
    catalog = _narrative_key_line_catalog(screenplay)
    if plan is None or not outline.shots or not catalog:
        return []
    actions = {
        action.action_id: action
        for action in plan.atomic_actions
    }
    event_actions = {
        event.event_id: list(event.action_ids)
        for event in plan.events
    }

    def shot_event_ids(shot: StoryboardOutlineShot) -> list[str]:
        return list(dict.fromkeys(
            event_id
            for event_id in [
                *(str(value or "").strip() for value in shot.event_ids),
                str(shot.story_event_id or "").strip(),
            ]
            if event_id
        ))

    shots_by_event: defaultdict[str, list[StoryboardOutlineShot]] = defaultdict(list)
    for shot in outline.shots:
        for event_id in shot_event_ids(shot):
            shots_by_event[event_id].append(shot)

    authoritative_ids_by_event = {
        event_id: _action_key_line_ids(action_ids, actions, catalog)
        for event_id, action_ids in event_actions.items()
    }
    authoritative_events_by_key: defaultdict[str, set[str]] = defaultdict(set)
    for event_id, key_ids in authoritative_ids_by_event.items():
        for key_id in key_ids:
            authoritative_events_by_key[key_id].add(event_id)
    exclusive_authority = {
        key_id: next(iter(event_ids))
        for key_id, event_ids in authoritative_events_by_key.items()
        if len(event_ids) == 1
    }

    changes: list[dict[str, Any]] = []
    for event_id, event_shots in shots_by_event.items():
        authoritative_ids = authoritative_ids_by_event.get(event_id, [])
        authoritative_set = set(authoritative_ids)

        explicit_action_ids = {
            id(shot): list(dict.fromkeys(filter(None, [
                shot.primary_action_id,
                *shot.supporting_action_ids,
            ])))
            for shot in event_shots
        }
        dialogue_owners: set[str] = set()
        for shot in event_shots:
            is_unprojected_action_owner = bool(
                infer_unprojected_event_actions
                and not explicit_action_ids[id(shot)]
                and not str(shot.shot_id or "").strip()
                and shot.shot_contribution is None
                and event_actions.get(event_id)
            )
            if explicit_action_ids[id(shot)] or is_unprojected_action_owner:
                continue
            dialogue_owners.update(
                str(key_id or "").strip().upper()
                for key_id in shot.key_line_ids
                if str(key_id or "").strip().upper() in authoritative_set
            )

        assigned_action_ids: set[str] = set(dialogue_owners)
        for shot in event_shots:
            action_ids = explicit_action_ids[id(shot)]
            if (
                not action_ids
                and infer_unprojected_event_actions
                and not str(shot.shot_id or "").strip()
                and shot.shot_contribution is None
            ):
                action_ids = event_actions.get(event_id, [])
            matched_ids = _action_key_line_ids(action_ids, actions, catalog)
            if action_ids and authoritative_set:
                desired_ids = [
                    key_id
                    for key_id in matched_ids
                    if key_id not in assigned_action_ids
                ]
                assigned_action_ids.update(desired_ids)
            else:
                desired_ids = [
                    str(key_id or "").strip().upper()
                    for key_id in shot.key_line_ids
                    if (
                        exclusive_authority.get(
                            str(key_id or "").strip().upper(),
                            event_id,
                        ) == event_id
                    )
                ]
            current_ids = [
                str(key_id or "").strip().upper()
                for key_id in shot.key_line_ids
                if str(key_id or "").strip()
            ]
            if current_ids == desired_ids:
                continue
            shot.key_line_ids = desired_ids
            shot.audio_cast = list(dict.fromkeys(
                catalog[key_id][0]
                for key_id in desired_ids
            ))
            changes.append({
                "shot_no": shot.shot_no,
                "event_id": event_id,
                "field": "key_line_ids",
                "from": current_ids,
                "to": desired_ids,
                "reason": "atomic_action_dialogue_relation",
            })
    return changes


def narrative_outline_action_delivery_errors(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[str]:
    """Validate exact action-to-dialogue ownership after all outline rewrites."""
    plan = screenplay.narrative_plan
    catalog = _narrative_key_line_catalog(screenplay)
    if plan is None or not outline.shots or not catalog:
        return []
    actions = {action.action_id: action for action in plan.atomic_actions}
    expected_by_event = {
        event.event_id: _action_key_line_ids(
            list(event.action_ids),
            actions,
            catalog,
        )
        for event in plan.events
    }
    events_by_key: defaultdict[str, set[str]] = defaultdict(set)
    for event_id, key_ids in expected_by_event.items():
        for key_id in key_ids:
            events_by_key[key_id].add(event_id)
    errors: list[str] = []
    for event in plan.events:
        expected = expected_by_event[event.event_id]
        if not expected:
            continue
        event_shots = [
            shot
            for shot in outline.shots
            if event.event_id in {
                *(str(value or "").strip() for value in shot.event_ids),
                str(shot.story_event_id or "").strip(),
            }
        ]
        actual = list(dict.fromkeys(
            str(key_id or "").strip().upper()
            for shot in event_shots
            for key_id in shot.key_line_ids
            if str(key_id or "").strip()
        ))
        missing = [key_id for key_id in expected if key_id not in actual]
        misplaced = [
            key_id
            for key_id in actual
            if (
                len(events_by_key.get(key_id, set())) == 1
                and event.event_id not in events_by_key[key_id]
            )
        ]
        if missing or misplaced:
            errors.append(
                "[OUTLINE_ACTION_DIALOGUE_RELATION_MISMATCH] "
                f"事件 {event.event_id} 的原子动作明确绑定台词 {expected}，"
                f"当前镜头交付为 {actual}，缺失 {missing}，错属 {misplaced}；"
                "请按 action_id 的说话人和原句关系重投影"
            )
    return errors


def normalize_split_action_owner_completions(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[dict[str, Any]]:
    """Fill missing terminal-action directing prose without overwriting it.

    Action/state IDs are graph authority, while ``beat``/``covers`` and the
    visible action prose are director-owned presentation.  Replacing an
    already directed post-dialogue result with the action's completion text
    can turn a reaction/result shot into a verbatim duplicate of the dialogue
    shot.  That duplicate cannot be repaired by the later scene-pack model,
    because the scene-pack contract correctly treats outline prose as fixed.
    """
    plan = screenplay.narrative_plan
    if plan is None:
        return []

    dialogue_event_ids = {
        event_id
        for shot in outline.shots
        if shot.key_line_ids
        for event_id in (
            list(shot.event_ids)
            or ([shot.story_event_id] if shot.story_event_id else [])
        )
    }
    actions = {
        action.action_id: action
        for action in plan.atomic_actions
    }
    changes: list[dict[str, Any]] = []
    for shot in outline.shots:
        event_ids = (
            list(shot.event_ids)
            or ([shot.story_event_id] if shot.story_event_id else [])
        )
        if (
            not shot.primary_action_id
            or shot.key_line_ids
            or shot.audio_cast
            or not dialogue_event_ids.intersection(event_ids)
        ):
            continue
        action_ids = [
            shot.primary_action_id,
            *shot.supporting_action_ids,
        ]
        completion_parts = [
            str(action.completion_condition or "").strip()
            for action_id in action_ids
            for action in [actions.get(action_id)]
            if action is not None
            and str(action.completion_condition or "").strip()
        ]
        completion = "；".join(dict.fromkeys(completion_parts))
        if not completion:
            continue
        for field, value in (
            ("state_in", ""),
            ("primary_action", completion),
            ("state_out", completion),
            ("beat", completion),
            ("covers", completion),
        ):
            current = getattr(shot, field)
            if str(current or "").strip() or current == value:
                continue
            setattr(shot, field, value)
            changes.append({
                "shot_no": shot.shot_no,
                "field": field,
                "from": current,
                "to": value,
                "reason": "split_event_action_completion",
            })
    return changes


def normalize_narrative_storyboard_outline(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    *,
    bible: Bible | None = None,
    preserve_shot_ids: bool = False,
) -> list[dict[str, Any]]:
    """Project graph-owned fields while preserving model-authored directing text.

    The model chooses the visible beat, scene presentation and legacy delivery
    fields. Event state replay, action ownership, audience staging, cumulative
    ledgers, capacity and boundaries are deterministic consequences of the
    published narrative plan.
    """
    plan = screenplay.narrative_plan
    if plan is None or not outline.shots or not plan.events:
        return []

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

    def _visual_capable(identity_id: str) -> bool:
        contract = identity_contracts.get(identity_id)
        return contract is None or contract.visual_policy != "offscreen_only"

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
            if _visual_capable(identity_id)
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

    def _display_names(identity_ids: set[str]) -> list[str]:
        return list(dict.fromkeys(
            contract.display_name
            for identity_id, contract in identity_contracts.items()
            if identity_id in identity_ids
            and _visual_capable(identity_id)
        ))
    compiler_context_identity_names = {
        identity.identity_id: identity.display_name
        for identity in plan.identity_contracts
        if identity.kind == "source_backed_scene_context_actor"
    }
    event_aliases: defaultdict[str, list[str]] = defaultdict(list)
    for event_id in events:
        alias = "".join(
            character.casefold()
            for character in event_id
            if character.isalnum()
        )
        if alias:
            event_aliases[alias].append(event_id)

    def canonical_event_id(value: str) -> str:
        raw = str(value or "").strip()
        if raw in events:
            return raw
        alias = "".join(
            character.casefold()
            for character in raw
            if character.isalnum()
        )
        matches = event_aliases.get(alias) or []
        return matches[0] if len(matches) == 1 else raw

    for shot in outline.shots:
        shot.event_ids = list(dict.fromkeys(
            canonical_event_id(event_id)
            for event_id in (shot.event_ids or [])
            if str(event_id or "").strip()
        ))
        if shot.story_event_id:
            shot.story_event_id = canonical_event_id(
                shot.story_event_id
            )
    event_order = {
        item.event_id: position
        for position, item in enumerate(plan.events)
    }
    catalog_by_turn: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for key_position, line in enumerate(screenplay.key_lines or [], start=1):
        text = str(line or "").strip()
        speaker, separator, spoken = text.partition("：")
        if not separator:
            speaker, separator, spoken = text.partition(":")
        if (
            not separator
            or not speaker.strip()
            or not spoken.strip()
            or speaker.strip() == "旁白"
        ):
            continue
        catalog_by_turn[(
            "".join(character for character in speaker if character.isalnum()),
            "".join(character for character in spoken if character.isalnum()),
        )].append(f"KL{key_position:02d}")

    key_line_meta: dict[str, tuple[str, str, str]] = {}
    chain_key_ids: dict[str, list[str]] = {}
    fallback_key_number = 1
    use_legacy_chain_catalog = not bool(catalog_by_turn)
    for chain in screenplay.dialogue_chains:
        ids: list[str] = []
        for turn in chain.turns:
            identity = (
                "".join(
                    character
                    for character in str(turn.speaker or "")
                    if character.isalnum()
                ),
                "".join(
                    character
                    for character in str(turn.line or "")
                    if character.isalnum()
                ),
            )
            candidates = catalog_by_turn.get(identity) or []
            if candidates:
                key_id = candidates.pop(0)
            elif use_legacy_chain_catalog and str(turn.speaker or "").strip() != "旁白":
                key_id = f"KL{fallback_key_number:02d}"
                fallback_key_number += 1
            else:
                continue
            key_line_meta[key_id] = (
                turn.speaker,
                turn.line,
                chain.chain_id,
            )
            ids.append(key_id)
        chain_key_ids[chain.chain_id] = ids
    base_by_event: dict[str, Any] = {}
    bases_by_event: defaultdict[str, list[Any]] = defaultdict(list)
    for shot in outline.shots:
        event_ids = list(shot.event_ids or [])
        if not event_ids and shot.story_event_id:
            event_ids = [shot.story_event_id]
        owned_event_ids = sorted(
            {
                event_id
                for event_id in event_ids
                if event_id in events
            },
            key=lambda event_id: event_order[event_id],
        )
        if owned_event_ids:
            event_id = owned_event_ids[0]
            base_by_event.setdefault(event_id, shot)
            bases_by_event[event_id].append(shot)
    already_projected = any(
        str(shot.shot_id or "").strip()
        and shot.shot_contribution is not None
        for shot in outline.shots
    )
    action_delivery_changes = reconcile_narrative_outline_action_deliveries(
        outline,
        screenplay,
        infer_unprojected_event_actions=not already_projected,
    )
    required_events = [
        item.event_id
        for item in plan.events
        if item.must_keep and item.delivery_policy == "deliver"
    ]
    if any(event_id not in base_by_event for event_id in required_events):
        return []

    key_event: dict[str, str] = {}
    for event_id, bases in bases_by_event.items():
        for base in bases:
            for key_id in base.key_line_ids:
                if key_id in key_line_meta:
                    key_event[key_id] = event_id
    for key_ids in chain_key_ids.values():
        known_events = {
            key_event[key_id]
            for key_id in key_ids
            if key_id in key_event
        }
        if len(known_events) == 1:
            event_id = next(iter(known_events))
            for key_id in key_ids:
                key_event.setdefault(key_id, event_id)
    chain_events: dict[str, str] = {}
    for chain_id, key_ids in chain_key_ids.items():
        known_events = {
            key_event[key_id]
            for key_id in key_ids
            if key_id in key_event
        }
        if len(known_events) == 1:
            chain_events[chain_id] = next(iter(known_events))

    def _bigrams(value: str) -> set[str]:
        compact = "".join(
            character
            for character in value
            if character.isalnum()
        )
        return {
            compact[index:index + 2]
            for index in range(max(0, len(compact) - 1))
        }

    proposition_text = {
        item.proposition_id: item.canonical_statement
        for item in plan.propositions
    }
    event_semantic_text = {
        event.event_id: "".join([
            *[
                evidence.observable_claim
                for evidence in plan.evidence
                if (
                    evidence.anchor.type == "event"
                    and evidence.anchor.id == event.event_id
                )
            ],
            *[
                proposition_text.get(proposition_id, "")
                for proposition_id in event.proposition_ids
            ],
        ])
        for event in plan.events
    }
    ordered_chain_ids = [
        chain.chain_id
        for chain in screenplay.dialogue_chains
    ]
    for chain_index, chain_id in enumerate(ordered_chain_ids):
        if chain_id in chain_events:
            continue
        key_ids = chain_key_ids.get(chain_id) or []
        chain_text = "".join(
            key_line_meta[key_id][1]
            for key_id in key_ids
        )
        chain_bigrams = _bigrams(chain_text)
        previous_event = next(
            (
                chain_events[candidate]
                for candidate in reversed(
                    ordered_chain_ids[:chain_index]
                )
                if candidate in chain_events
            ),
            "",
        )
        next_event = next(
            (
                chain_events[candidate]
                for candidate in ordered_chain_ids[chain_index + 1:]
                if candidate in chain_events
            ),
            "",
        )
        interval_event_ids = [
            event_id
            for event_id in event_semantic_text
            if (
                (
                    not previous_event
                    or event_order[event_id] > event_order[previous_event]
                )
                and (
                    not next_event
                    or event_order[event_id] < event_order[next_event]
                )
            )
        ]
        semantic_candidates = (
            interval_event_ids
            if interval_event_ids
            else list(event_semantic_text)
        )
        scored = sorted(
            (
                (
                    len(chain_bigrams & _bigrams(event_text))
                    / max(1, len(chain_bigrams)),
                    event_order.get(event_id, -1),
                    event_id,
                )
                for event_id, event_text in event_semantic_text.items()
                if event_id in semantic_candidates
            ),
            reverse=True,
        )
        selected_event = (
            scored[0][2]
            if scored and scored[0][0] > 0
            else ""
        )
        if not selected_event:
            selected_event = next_event
        if not selected_event:
            selected_event = previous_event
        if selected_event:
            chain_events[chain_id] = selected_event
            for key_id in key_ids:
                key_event.setdefault(key_id, selected_event)
    key_ids_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for key_id in key_line_meta:
        event_id = key_event.get(key_id)
        if event_id:
            key_ids_by_event[event_id].append(key_id)

    paths: dict[str, Any] = {}
    delta_paths: dict[str, tuple[str, Any, str]] = {}
    delta_destinations: dict[str, str] = {}
    for intent in plan.experience_intents:
        for path in intent.audience_paths:
            paths[path.audience_prior_id] = path
            ordered = sorted(
                path.target_deltas,
                key=lambda item: (
                    event_order.get(item.deadline_event_id, len(event_order)),
                    item.target_delta_id,
                ),
            )
            prior_states = [
                state
                for state in plan.audience_states
                if state.audience_prior_id == path.audience_prior_id
            ]
            deadline_groups: list[list[Any]] = []
            for delta in ordered:
                if (
                    not deadline_groups
                    or deadline_groups[-1][0].deadline_event_id
                    != delta.deadline_event_id
                ):
                    deadline_groups.append([])
                deadline_groups[-1].append(delta)
            current_state_id = path.audience_state_in_id
            for group_index, group in enumerate(deadline_groups):
                destination = path.audience_state_out_target_id
                if group_index + 1 < len(deadline_groups):
                    next_group = deadline_groups[group_index + 1]
                    candidates = [
                        state.audience_state_id
                        for state in prior_states
                        if (
                            state.audience_state_id
                            not in {
                                path.audience_state_in_id,
                                path.audience_state_out_target_id,
                            }
                            and all(
                                _target_state_fragment_matches(
                                    delta,
                                    delta.to_state,
                                    state,
                                )
                                for delta in group
                            )
                            and all(
                                _target_state_fragment_matches(
                                    next_delta,
                                    next_delta.from_state,
                                    state,
                                )
                                for next_delta in next_group
                            )
                        )
                    ]
                    # Some authority graphs intentionally expose only the
                    # initial and final audience snapshots. In that case the
                    # delta ledger still records this deadline's contribution,
                    # while the coarse snapshot ID remains unchanged until a
                    # declared later state is reached.
                    destination = (
                        candidates[0]
                        if len(candidates) == 1
                        else current_state_id
                    )
                for delta in group:
                    delta_paths[delta.target_delta_id] = (
                        path.audience_prior_id,
                        delta,
                        destination,
                    )
                    delta_destinations[delta.target_delta_id] = destination
                current_state_id = destination

    deltas_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for delta_id, (_prior_id, delta, _destination) in delta_paths.items():
        deltas_by_event[delta.deadline_event_id].append(delta_id)

    evidence_by_event: defaultdict[str, list[Any]] = defaultdict(list)
    for evidence in plan.evidence:
        if evidence.anchor.type == "event":
            evidence_by_event[evidence.anchor.id].append(evidence)
    character_state_ids_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for state in [*plan.character_states, *plan.character_beliefs]:
        if state.anchor.type != "event":
            continue
        state_id = (
            getattr(state, "character_state_id", None)
            or getattr(state, "character_belief_id", None)
        )
        if state_id:
            character_state_ids_by_event[state.anchor.id].append(state_id)

    window_seconds_by_event: defaultdict[str, float] = defaultdict(float)
    for window in plan.readability_windows:
        for event_id in window.event_ids:
            window_seconds_by_event[event_id] = max(
                window_seconds_by_event[event_id],
                float(window.scheduled_processing_s or 0),
            )

    nodes: list[tuple[str, str, Any]] = []
    for event in plan.events:
        base = base_by_event.get(event.event_id)
        if base is None:
            continue
        event_bases = list(bases_by_event[event.event_id])
        if already_projected:
            for event_base in event_bases:
                contribution = event_base.shot_contribution
                role = (
                    "support"
                    if (
                        contribution is not None
                        and contribution.target_delta_ids
                        and not event_base.primary_action_id
                        and not event_base.key_line_ids
                    )
                    else "dialogue" if event_base.key_line_ids else "main"
                )
                nodes.append((event.event_id, role, event_base))
            continue
        event_key_ids = key_ids_by_event.get(event.event_id) or [
            key_id
            for event_base in event_bases
            for key_id in event_base.key_line_ids
            if key_id in key_line_meta
        ]
        existing_key_ids = {
            key_id
            for event_base in event_bases
            for key_id in event_base.key_line_ids
        }
        missing_key_ids = [
            key_id
            for key_id in event_key_ids
            if key_id not in existing_key_ids
        ]
        dialogue_groups: list[list[str]] = []
        current_group: list[str] = []
        current_chars = 0
        current_speaker = ""
        for key_id in missing_key_ids:
            speaker, line, _chain_id = key_line_meta[key_id]
            line_chars = len(
                "".join(
                    character
                    for character in line
                    if character.isalnum()
                )
            )
            if (
                current_group
                and (
                    speaker != current_speaker
                    or current_chars + line_chars
                    > config.MAX_SPOKEN_CHARS_PER_SHOT
                )
            ):
                dialogue_groups.append(current_group)
                current_group = []
                current_chars = 0
            current_group.append(key_id)
            current_chars += line_chars
            current_speaker = speaker
        if current_group:
            dialogue_groups.append(current_group)

        action_s = sum(
            max(0.0, phase.estimated_min_s)
            for action_id in event.action_ids
            for action in [actions.get(action_id)]
            if action is not None
            for phase in action.temporal_phases
        )
        processing_by_prior: defaultdict[str, float] = defaultdict(float)
        for delta_id in deltas_by_event[event.event_id]:
            prior_id, delta, _destination = delta_paths[delta_id]
            processing_by_prior[prior_id] += max(
                0.0,
                delta.required_processing_s,
            )
        processing_s = max(processing_by_prior.values(), default=0.0)
        reaction_s = (
            1.0 if character_state_ids_by_event[event.event_id] else 0.0
        )
        def _spoken_seconds(key_ids: list[str]) -> float:
            spoken_chars = sum(
                len(
                    "".join(
                        character
                        for character in key_line_meta.get(
                            key_id,
                            ("", "", ""),
                        )[1]
                        if character.isalnum()
                    )
                )
                for key_id in key_ids
            )
            return (
                spoken_chars
                * float(config.VIDEO_DURATION_MIN_S)
                / float(config.SPOKEN_CHARS_PER_5_SECONDS)
            )

        first_dialogue_s = 0.0
        if dialogue_groups:
            first_dialogue_s = _spoken_seconds(dialogue_groups[0])
        elif event_bases:
            first_dialogue_s = max(
                (_spoken_seconds(list(item.key_line_ids)) for item in event_bases),
                default=0.0,
            )
        needs_support = bool(
            processing_s > 0
            and (
                action_s + processing_s + reaction_s
                > config.VIDEO_DURATION_MAX_S
                or processing_s + first_dialogue_s
                > config.VIDEO_DURATION_MAX_S
            )
        )
        support_completes_event = bool(
            needs_support
            and not event.action_ids
            and not event_key_ids
            and not event.effects_add
            and not event.effects_remove
            and not character_state_ids_by_event[event.event_id]
        )
        event_nodes: list[tuple[str, str, Any]] = []
        if needs_support:
            support = base.model_copy(deep=True)
            support.primary_action_id = None
            support.supporting_action_ids = []
            support.action_phase_ids = []
            support.primary_action = (
                next(
                    (
                        window.readability_reason
                        for window in plan.readability_windows
                        if set(window.target_delta_ids).intersection(
                            deltas_by_event[event.event_id]
                        )
                    ),
                    "",
                )
                or next(
                    (
                        item.observable_claim
                        for item in evidence_by_event[event.event_id]
                    ),
                    support.beat,
                )
            )
            support.state_out = support.primary_action
            support.key_line_ids = []
            support.audio_cast = []
            event_nodes.append((event.event_id, "support", support))
        completion = "；".join(dict.fromkeys(
            str(action.completion_condition or "").strip()
            for action_id in event.action_ids
            for action in [actions.get(action_id)]
            if action is not None
            and str(action.completion_condition or "").strip()
        ))
        action_delivery_texts = {
            text
            for action_id in event.action_ids
            for action in [actions.get(action_id)]
            if action is not None
            for text in (
                str(action.semantic_intent or "").strip(),
                str(action.completion_condition or "").strip(),
            )
            if text
        }
        main_base_index = next(
            (
                index
                for index, event_base in enumerate(event_bases)
                if str(event_base.primary_action or "").strip()
                in action_delivery_texts
            ),
            None,
        )
        for base_index, event_base in enumerate(event_bases):
            if support_completes_event:
                continue
            spoken_s = _spoken_seconds(list(event_base.key_line_ids))
            if (
                event_base.key_line_ids
                and spoken_s + action_s + reaction_s
                > config.VIDEO_DURATION_MAX_S
            ):
                dialogue = event_base.model_copy(deep=True)
                dialogue.primary_action_id = None
                dialogue.supporting_action_ids = []
                dialogue.action_phase_ids = []
                spoken_text = "；".join(
                    key_line_meta.get(key_id, ("", "", ""))[1]
                    for key_id in dialogue.key_line_ids
                    if key_line_meta.get(key_id, ("", "", ""))[1]
                )
                if spoken_text:
                    dialogue.primary_action = spoken_text
                    dialogue.beat = spoken_text
                    dialogue.covers = spoken_text
                    dialogue.state_out = spoken_text
                event_nodes.append((event.event_id, "dialogue", dialogue))

                main = event_base.model_copy(deep=True)
                main.key_line_ids = []
                main.audio_cast = []
                if completion:
                    main.state_in = dialogue.state_out
                    main.primary_action = completion
                    main.state_out = completion
                    main.beat = completion
                    main.covers = completion
                event_nodes.append((event.event_id, "main", main))
                continue
            event_nodes.append((
                event.event_id,
                (
                    "main"
                    if base_index == main_base_index
                    else "dialogue" if event_base.key_line_ids else "main"
                ),
                event_base,
            ))
        generated_dialogues: list[tuple[str, str, Any]] = []
        for group in dialogue_groups:
            dialogue = base.model_copy(deep=True)
            dialogue.primary_action_id = None
            dialogue.supporting_action_ids = []
            dialogue.action_phase_ids = []
            dialogue.key_line_ids = list(group)
            speakers = list(dict.fromkeys(
                key_line_meta[key_id][0]
                for key_id in group
                if key_line_meta[key_id][0]
            ))
            dialogue.audio_cast = speakers
            dialogue.characters_visible = speakers[:1]
            dialogue.primary_action = "；".join(
                key_line_meta[key_id][1]
                for key_id in group
            )
            dialogue.beat = dialogue.primary_action
            dialogue.covers = dialogue.primary_action
            generated_dialogues.append(
                (event.event_id, "dialogue", dialogue)
            )
        if generated_dialogues:
            insert_at = next(
                (
                    index
                    for index, (_event_id, role, _shot) in enumerate(
                        event_nodes
                    )
                    if role == "main"
                ),
                len(event_nodes),
            )
            event_nodes[insert_at:insert_at] = generated_dialogues
        if generated_dialogues and main_base_index is None and completion:
            directed_main = next(
                (
                    event_shot
                    for _event_id, role, event_shot in reversed(event_nodes)
                    if role == "main"
                ),
                None,
            )
            if directed_main is not None:
                directed_main.state_in = ""
                directed_main.primary_action = completion
                directed_main.state_out = completion
                directed_main.beat = completion
                directed_main.covers = completion
        if (
            not support_completes_event
            and not any(
                role == "main"
                for _event_id, role, _shot in event_nodes
            )
        ):
            main = base.model_copy(deep=True)
            main.key_line_ids = []
            main.audio_cast = []
            main.primary_action = (
                completion
                or "人物闭口呈现本事件完成后的可见反应与状态结果"
            )
            main.state_out = main.primary_action
            main.beat = main.primary_action
            main.covers = main.primary_action
            event_nodes.append((event.event_id, "main", main))
        for _event_id, _role, event_shot in event_nodes:
            for field_name in (
                "scene_id",
                "scene_time",
                "scene_name",
                "scene_setting",
            ):
                if getattr(event_shot, field_name):
                    continue
                setattr(event_shot, field_name, getattr(base, field_name))
        nodes.extend(event_nodes)

    for position, (_event_id, _role, shot) in enumerate(nodes, start=1):
        shot.shot_no = position
        if not preserve_shot_ids:
            shot.shot_id = f"SH{position:03d}"

    positions_by_event: defaultdict[str, list[int]] = defaultdict(list)
    for position, (event_id, _role, _shot) in enumerate(nodes):
        positions_by_event[event_id].append(position)
    first_position = {
        event_id: positions[0]
        for event_id, positions in positions_by_event.items()
    }
    last_position = {
        event_id: positions[-1]
        for event_id, positions in positions_by_event.items()
    }

    delta_owner_position: dict[str, int] = {}
    for event_id, delta_ids in deltas_by_event.items():
        positions = positions_by_event.get(event_id) or []
        if not positions:
            continue
        support_position = next(
            (
                position
                for position in positions
                if nodes[position][1] == "support"
            ),
            positions[0],
        )
        for delta_id in delta_ids:
            delta_owner_position[delta_id] = support_position
    task_owner_position: defaultdict[int, list[str]] = defaultdict(list)
    if nodes:
        for task in plan.assimilation_tasks:
            task_owner_position[0].append(task.assimilation_task_id)

    current_facts = set(plan.initial_state_fact_ids)
    current_audience_state = {
        prior_id: path.audience_state_in_id
        for prior_id, path in paths.items()
    }
    completed_actions: set[str] = set()
    completed_phases: set[str] = set()
    all_event_ids = list(events)
    redundant_context_ids_by_event: dict[str, set[str]] = {}
    for event_id, event in events.items():
        redundant_ids: set[str] = set()
        for action_id in event.action_ids:
            action = actions.get(action_id)
            if action is None:
                continue
            actor_ids = set(
                storyboard_action_relation_ids(
                    screenplay,
                    event_id,
                    action,
                    bible=bible,
                )[0]
            )
            if actor_ids - set(compiler_context_identity_names):
                redundant_ids.update(
                    actor_ids & set(compiler_context_identity_names)
                )
        redundant_context_ids_by_event[event_id] = redundant_ids
    normalized_shots = []
    changes: list[dict[str, Any]] = [
        *action_delivery_changes,
        *legacy_action_relation_changes,
    ]

    for position, (event_id, role, shot) in enumerate(nodes):
        event = events[event_id]
        is_last_occurrence = position == last_position[event_id]
        is_support = role == "support"
        action_ids = list(event.action_ids) if is_last_occurrence else []
        primary_action_id = action_ids[0] if action_ids else None
        supporting_action_ids = action_ids[1:]
        phase_ids = [
            phase.phase_id
            for action_id in action_ids
            for action in [actions.get(action_id)]
            if action is not None
            for phase in action.temporal_phases
        ]
        bound_actor_ids: set[str] = set()
        bound_target_ids: set[str] = set()
        bound_participant_deliveries: list[Any] = []
        for action_id in action_ids:
            action = actions.get(action_id)
            if action is None:
                continue
            actor_ids, target_ids = storyboard_action_relation_ids(
                screenplay,
                event_id,
                action,
                bible=bible,
            )
            bound_actor_ids.update(actor_ids)
            bound_target_ids.update(target_ids)
            bound_participant_deliveries.extend(
                delivery
                for delivery in action.participant_deliveries
                if (
                    delivery.action_id == action_id
                    and delivery.participant_id in {
                        *actor_ids,
                        *target_ids,
                    }
                    and delivery.is_perceivable
                    and delivery.evidence_ids
                )
            )

        visible_ids = {
            entity_id
            for entity_id in event.onscreen_entity_ids
            if entity_id != "audience" and _visual_capable(entity_id)
        }
        # Old published narrative plans predate onscreen_entity_ids.  Their
        # migration is relation-based: action ownership plus exact identity
        # occurrences in visual state text.  Evidence perceivers and scene cast
        # are intentionally excluded because neither denotes shot presence.
        if not event.onscreen_entity_ids:
            relation_ids = legacy_event_relation_ids.get(event_id) or set()
            if relation_ids:
                visible_ids.update(
                    identity_id
                    for identity_id in relation_ids
                    if _visual_capable(identity_id)
                )
            else:
                # Some historical physical actions use only pronouns in their
                # prose.  With no exact relation evidence at all, preserve the
                # already-typed actor/target ownership rather than guessing.
                for event_action_id in event.action_ids:
                    event_action = actions.get(event_action_id)
                    if event_action is None:
                        continue
                    effective_actor_ids, effective_target_ids = (
                        storyboard_action_relation_ids(
                            screenplay,
                            event_id,
                            event_action,
                            bible=bible,
                        )
                    )
                    visible_ids.update(
                        identity_id
                        for identity_id in (
                            *effective_actor_ids,
                            *effective_target_ids,
                        )
                        if _visual_capable(identity_id)
                    )
            if not legacy_event_text_identity_ids.get(event_id):
                # Some pre-v1.5 events describe participants only through
                # pronouns or counts ("两人"). When no exact identity surface
                # exists, retain only old roster entries that still resolve
                # exactly through the current typed registry. They form a
                # permitted candidate relation; the directing model still
                # chooses the actual visible subset.
                current_names = {
                    str(name or "").strip()
                    for name in (shot.characters_visible or [])
                    if str(name or "").strip()
                }
                visible_ids.update(
                    identity_id
                    for identity_id, contract in identity_contracts.items()
                    if (
                        _visual_capable(identity_id)
                        and str(contract.display_name or "").strip()
                        in current_names
                    )
                )
        redundant_context_ids = redundant_context_ids_by_event[event_id]
        visible_ids.difference_update(redundant_context_ids)
        allowed_names = _display_names(visible_ids)
        # This is the event's permitted composition relation, not the final
        # shot cast. Historical outlines often copied a whole scene roster or
        # retained a wrong first-mentioned actor here. The directing layer
        # chooses the actual visible subset later and records the three visual
        # fields together.
        projected_names = allowed_names
        if projected_names != list(shot.characters_visible or []):
            changes.append({
                "shot_no": shot.shot_no,
                "field": "characters_visible",
                "from": list(shot.characters_visible or []),
                "to": projected_names,
                "reason": "event_onscreen_identity_authority",
            })
            shot.characters_visible = projected_names
        unexpected_visual_names = [
            contract.display_name
            for identity_id, contract in identity_contracts.items()
            if identity_id not in visible_ids
            and contract.visual_policy != "offscreen_only"
            and any(
                contract.display_name in str(value or "")
                for value in (
                    shot.primary_action,
                    shot.beat,
                    shot.covers,
                    shot.state_out,
                )
            )
        ]
        if unexpected_visual_names and (
            role == "reaction" or shot.continuity_mode == "reaction_cut"
        ):
            reaction = (
                f"{projected_names[0]}闭口呈现当前事件完成后的状态变化"
                if projected_names
                else "当前画面以原有可见状态承接下一动作"
            )
            for field_name in (
                "primary_action", "beat", "covers", "state_out",
            ):
                before = str(getattr(shot, field_name) or "")
                if before == reaction:
                    continue
                setattr(shot, field_name, reaction)
                changes.append({
                    "shot_no": shot.shot_no,
                    "field": field_name,
                    "from": before,
                    "to": reaction,
                    "reason": "reaction_visual_identity_authority",
                })
        if redundant_context_ids:
            redundant_names = {
                compiler_context_identity_names[identity_id]
                for identity_id in redundant_context_ids
            }
            shot.characters_visible = [
                name
                for name in shot.characters_visible
                if name not in redundant_names
            ]

        shot.event_ids = [event_id]
        shot.story_event_id = event_id
        shot.primary_action_id = primary_action_id
        shot.supporting_action_ids = supporting_action_ids
        shot.action_phase_ids = phase_ids
        shot.visible_entity_ids = sorted(visible_ids)
        contracted_offscreen_ids = {
            delivery.participant_id
            for delivery in bound_participant_deliveries
            if delivery.participant_id not in visible_ids
        }
        shot.offscreen_action_actor_ids = sorted(
            (bound_actor_ids - visible_ids) & contracted_offscreen_ids
        )
        shot.offscreen_action_target_ids = sorted(
            (bound_target_ids - visible_ids) & contracted_offscreen_ids
        )
        delivered_offscreen_ids = {
            *shot.offscreen_action_actor_ids,
            *shot.offscreen_action_target_ids,
        }
        shot.action_participant_deliveries = [
            delivery.model_copy(deep=True)
            for delivery in bound_participant_deliveries
            if delivery.participant_id in delivered_offscreen_ids
        ]

        shot.planned_state_in_fact_ids = sorted(current_facts)
        declared_add_ids = set(event.effects_add) if is_last_occurrence else set()
        declared_remove_ids = set(event.effects_remove) if is_last_occurrence else set()
        remove_ids = declared_remove_ids & current_facts
        add_ids = declared_add_ids - current_facts - remove_ids
        shot.planned_delta_add_fact_ids = sorted(add_ids)
        shot.planned_delta_remove_fact_ids = sorted(remove_ids)
        current_facts = (current_facts - remove_ids) | add_ids
        shot.planned_state_out_fact_ids = sorted(current_facts)
        shot.completed_before_action_ids = sorted(completed_actions)
        shot.completed_before_action_phase_ids = sorted(completed_phases)
        shot.reserved_future_event_ids = [
            candidate
            for candidate in all_event_ids
            if first_position.get(candidate, len(nodes)) > position
        ]

        path_inputs = dict(current_audience_state)
        owned_delta_ids = [
            delta_id
            for delta_id, owner_position in delta_owner_position.items()
            if owner_position == position
        ]
        for delta_id in sorted(
            owned_delta_ids,
            key=lambda item: (
                event_order.get(delta_paths[item][1].deadline_event_id, 0),
                item,
            ),
        ):
            prior_id, _delta, destination = delta_paths[delta_id]
            current_audience_state[prior_id] = destination
        shot.audience_state_paths = [
            AudienceStatePathRef.model_validate({
                "audience_prior_id": prior_id,
                "audience_state_in_id": path_inputs[prior_id],
                "audience_state_out_target_id": current_audience_state[prior_id],
            })
            for prior_id in sorted(paths)
        ]

        event_evidence_ids = [
            item.evidence_id
            for item in evidence_by_event[event_id]
        ]
        evidence_ids = event_evidence_ids
        character_state_ids = (
            list(character_state_ids_by_event[event_id])
            if is_last_occurrence
            else []
        )
        audience_delta_ids = [
            current_audience_state[prior_id]
            for prior_id in sorted(paths)
            if path_inputs[prior_id] != current_audience_state[prior_id]
        ]
        shot.shot_contribution = ShotContribution.model_validate({
            "shot_contribution_id": f"SCONTRIB-{shot.shot_id}",
            "experience_intent_ids": [
                item.experience_intent_id
                for item in plan.experience_intents
            ],
            "target_delta_ids": owned_delta_ids,
            "assimilation_task_ids": task_owner_position.get(position, []),
            "evidence_ids": evidence_ids,
            "story_delta_fact_ids": sorted(add_ids | remove_ids),
            "character_state_delta_ids": character_state_ids,
            "audience_state_delta_ids": audience_delta_ids,
            "affective_delta": {},
            "spatial_temporal_delta": {},
            "dramatic_pressure_delta": 0.0,
        })

        action_s = sum(
            max(0.0, phase.estimated_min_s)
            for action_id in action_ids
            for action in [actions.get(action_id)]
            if action is not None
            for phase in action.temporal_phases
        )
        processing_by_prior: defaultdict[str, float] = defaultdict(float)
        for delta_id in owned_delta_ids:
            prior_id, delta, _destination = delta_paths[delta_id]
            processing_by_prior[prior_id] += max(
                0.0,
                delta.required_processing_s,
            )
        inference_s = max(processing_by_prior.values(), default=0.0)
        reaction_s = 1.0 if character_state_ids else 0.0
        spoken_chars = sum(
            len(
                "".join(
                    character
                    for character in key_line_meta.get(
                        key_id,
                        ("", "", ""),
                    )[1]
                    if character.isalnum()
                )
            )
            for key_id in shot.key_line_ids
        )
        spoken_s = (
            spoken_chars
            * float(config.VIDEO_DURATION_MIN_S)
            / float(config.SPOKEN_CHARS_PER_5_SECONDS)
        )
        shot.capacity_budget = ShotCapacityBudget.model_validate({
            "action_phase_s": action_s,
            "spoken_and_text_s": spoken_s,
            "attention_switch_s": 0.0,
            "inference_processing_s": inference_s,
            "reaction_registration_s": reaction_s,
            "spatial_reorientation_s": 0.0,
            "entry_exit_settle_s": 0.0,
            "other_s": 0.0,
            "other_reason": None,
        })
        event_window_s = window_seconds_by_event[event_id]
        minimum_duration = max(
            5,
            int(ceil(action_s + spoken_s + inference_s + reaction_s)),
            int(ceil(event_window_s)) if not is_support else 0,
        )
        shot.duration_s = min(
            config.VIDEO_DURATION_MAX_S,
            max(config.VIDEO_DURATION_MIN_S, minimum_duration),
        )

        if position == 0:
            shot.narrative_boundary_from_previous = None
        else:
            previous_shot = normalized_shots[-1]
            shot.narrative_boundary_from_previous = (
                NarrativeBoundaryContract.model_validate({
                "boundary_id": (
                    f"NB-{previous_shot.shot_id}-{shot.shot_id}"
                ),
                "previous_shot_id": previous_shot.shot_id,
                "next_shot_id": shot.shot_id,
                "narrative_relation": "相邻镜头按事件因果与状态链继续",
                "required_state_invariants": list(
                    shot.planned_state_in_fact_ids
                ),
                "allowed_state_deltas": [],
                "state_delta_transitions": [],
                "forbidden_replay_action_ids": sorted(completed_actions),
                "handoff_action_phase_id": None,
                "spatial_orientation_contract": {},
                "temporal_orientation_contract": {},
                "audience_state_handoffs": [
                    {
                        "audience_prior_id": prior_id,
                        "previous_state_out_id": path_inputs[prior_id],
                        "next_state_in_id": path_inputs[prior_id],
                    }
                    for prior_id in sorted(paths)
                ],
                "affective_handoff": {},
                "cut_motivation": (
                    "前一镜任务完成，切换到下一事件的可感知交付"
                ),
                })
            )

        completed_phases.update(phase_ids)
        completed_actions.update(action_ids)
        normalized_shots.append(shot)
        changes.append({
            "shot_no": shot.shot_no,
            "shot_id": shot.shot_id,
            "event_id": event_id,
            "role": role,
        })

    windows = []
    for source_window in plan.readability_windows:
        window = source_window.model_copy(deep=True)
        shot_ids: list[str] = []
        for event_id in window.event_ids:
            shot_ids.extend(
                normalized_shots[position].shot_id
                for position in positions_by_event.get(event_id, [])
            )
        for delta_id in window.target_delta_ids:
            owner_position = delta_owner_position.get(delta_id)
            if owner_position is not None:
                shot_ids.append(normalized_shots[owner_position].shot_id)
        window.shot_ids = list(dict.fromkeys(shot_ids))
        linked_duration = sum(
            float(shot.duration_s or 0)
            for shot in normalized_shots
            if shot.shot_id in window.shot_ids
        )
        window.planned_available_s = min(
            linked_duration,
            max(
                float(window.scheduled_processing_s or 0),
                min(linked_duration, float(window.planned_available_s or 0)),
            ),
        )
        windows.append(window)

    for shot in normalized_shots:
        shot.readability_window_ids = [
            window.readability_window_id
            for window in windows
            if shot.shot_id in window.shot_ids
        ]

    normalized = StoryboardOutline.model_validate({
        "episode_no": outline.episode_no,
        "shots": [
            shot.model_dump(mode="json")
            for shot in normalized_shots
        ],
        "readability_windows": [
            window.model_dump(mode="json")
            for window in windows
        ],
        "cognitive_bridge_plans": [],
    })
    outline.shots = normalized.shots
    outline.readability_windows = normalized.readability_windows
    outline.cognitive_bridge_plans = []
    changes.extend(
        normalize_split_action_owner_completions(
            outline,
            screenplay,
        )
    )
    return changes


def compile_narrative_storyboard_outline(
    screenplay: EpisodeScreenplay,
) -> StoryboardOutline:
    """Compile one compact event skeleton without asking a model for ledgers.

    The screenplay already owns event order, scene boundaries, source
    ownership, dialogue IDs and state transitions. The outline model used to
    restate all of those fields in one very large JSON response. This compiler
    keeps only the directing prose needed by the later scene-pack stage; the
    existing narrative projection then derives every graph-owned field.
    """
    plan = screenplay.narrative_plan
    scenes = list(screenplay.scene_outline or [])
    events = list(plan.events if plan is not None else [])
    scene_contracts = list(plan.scene_contracts if plan is not None else [])
    if plan is None:
        raise ValueError("叙事剧本缺少 narrative_plan")
    if not events:
        raise ValueError("叙事剧本没有可编译事件")
    participant_errors = action_participant_delivery_errors(screenplay)
    if participant_errors:
        raise ValueError("；".join(participant_errors))
    if not scenes:
        raise ValueError("叙事剧本没有场次结构")
    if len(scene_contracts) != len(scenes):
        raise ValueError(
            "scene_outline 与 narrative_plan.scene_contracts 数量不一致："
            f"{len(scenes)} != {len(scene_contracts)}"
        )
    if len(events) < len(scenes):
        raise ValueError(
            "叙事事件少于场次数，无法保证每场至少一个镜头："
            f"{len(events)} < {len(scenes)}"
        )

    event_positions = {
        event.event_id: position
        for position, event in enumerate(events)
    }
    scene_by_event: dict[str, int] = {}
    cursor = 0
    for scene_index, contract in enumerate(scene_contracts):
        remaining_scenes = len(scenes) - scene_index - 1
        latest_allowed = len(events) - remaining_scenes - 1
        declared_turns = [
            event_positions[event_id]
            for event_id in contract.turn_event_ids
            if (
                event_id in event_positions
                and cursor <= event_positions[event_id] <= latest_allowed
            )
        ]
        if scene_index == len(scenes) - 1:
            end = len(events) - 1
        elif declared_turns:
            end = min(max(declared_turns), latest_allowed)
        else:
            end = min(cursor, latest_allowed)
        if end < cursor:
            raise ValueError(
                f"场次 {contract.scene_id} 没有可分配的顺序事件"
            )
        for position in range(cursor, end + 1):
            scene_by_event[events[position].event_id] = scene_index
        cursor = end + 1
    if cursor != len(events):
        raise ValueError(
            f"场次合同只覆盖 {cursor}/{len(events)} 个叙事事件"
        )

    legacy_events = {
        event.event_id: event
        for event in screenplay.events or []
    }

    def source_ids(value: str) -> set[str]:
        return set(re.findall(r"SRC\d+", str(value or "")))

    event_source_ids = {
        event.event_id: source_ids(
            legacy_events.get(event.event_id).source_span
            if event.event_id in legacy_events else ""
        )
        for event in events
    }
    beats_by_event: defaultdict[str, list[Any]] = defaultdict(list)
    beats = list(
        screenplay.plot_spine.spine_beats
        if screenplay.plot_spine is not None else []
    )
    for beat_position, beat in enumerate(beats):
        beat_sources = set(beat.source_segment_ids or [])
        _score, _distance, selected_event_id = max(
            (
                len(beat_sources & event_source_ids[event.event_id]),
                -abs(beat_position - event_position),
                event.event_id,
            )
            for event_position, event in enumerate(events)
        )
        beats_by_event[selected_event_id].append(beat)

    key_line_catalog: dict[str, str] = {}
    for key_position, line in enumerate(screenplay.key_lines or [], start=1):
        text = str(line or "").strip()
        speaker, separator, _spoken = text.partition("：")
        if not separator:
            speaker, separator, _spoken = text.partition(":")
        if text and separator and speaker.strip() != "旁白":
            key_line_catalog[f"KL{key_position:02d}"] = text
    narrative_key_lines = _narrative_key_line_catalog(screenplay)
    actions_by_id = {
        action.action_id: action
        for action in plan.atomic_actions
    }

    evidence_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for evidence in plan.evidence:
        if evidence.anchor.type == "event":
            evidence_by_event[evidence.anchor.id].append(
                evidence.observable_claim
            )
    information_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for item in screenplay.information_ledger or []:
        if item.event_id:
            information_by_event[item.event_id].append(item.info_id)

    def scene_parts(scene_heading: str) -> tuple[str, str]:
        heading = re.sub(
            r"^【场[^】]*】\s*",
            "",
            str(scene_heading or ""),
        ).strip()
        parts = re.split(r"\s*[／/]\s*", heading, maxsplit=1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return "", heading

    shots: list[StoryboardOutlineShot] = []
    for shot_no, event in enumerate(events, start=1):
        scene_index = scene_by_event[event.event_id]
        scene = scenes[scene_index]
        scene_contract = scene_contracts[scene_index]
        legacy = legacy_events.get(event.event_id)
        event_beats = beats_by_event[event.event_id]
        spine_key_line_ids = list(dict.fromkeys(
            key_line_id
            for beat in event_beats
            for key_line_id in beat.key_line_ids
            if key_line_id in key_line_catalog
        ))
        action_key_line_ids = _action_key_line_ids(
            list(event.action_ids),
            actions_by_id,
            narrative_key_lines,
        )
        event_key_line_ids = (
            action_key_line_ids
            if action_key_line_ids
            else spine_key_line_ids
        )
        event_information_ids = list(dict.fromkeys([
            *information_by_event[event.event_id],
            *[
                info_id
                for beat in event_beats
                for info_id in beat.information_ids
            ],
        ]))
        scene_time, scene_name = scene_parts(scene.scene_heading)
        observable = next(
            (
                claim.strip()
                for claim in evidence_by_event[event.event_id]
                if claim.strip()
            ),
            "",
        )
        visible_change = str(
            getattr(legacy, "visible_change", "") or ""
        ).strip()
        trigger = str(getattr(legacy, "trigger", "") or "").strip()
        beat_text = (
            visible_change
            or observable
            or trigger
            or scene.summary
        )
        covers = "；".join(dict.fromkeys(filter(None, [
            *[
                str(beat.does or "").strip()
                for beat in event_beats
            ],
            observable,
            visible_change,
        ])))
        speakers = list(dict.fromkeys(
            key_line_catalog[key_line_id].partition("：")[0].strip()
            for key_line_id in event_key_line_ids
            if key_line_id in key_line_catalog
        ))
        previous_scene_id = shots[-1].scene_id if shots else ""
        shots.append(StoryboardOutlineShot(
            shot_no=shot_no,
            scene_time=scene_time,
            scene_name=scene_name,
            scene_setting=(
                f"{scene_time}，{scene_name}"
                if scene_time and scene_name else scene_name or scene_time
            ),
            beat=beat_text,
            covers=covers or beat_text,
            story_event_id=event.event_id,
            spine_beat_ids=[
                beat.beat_id
                for beat in event_beats
                if beat.beat_id
            ],
            key_line_ids=event_key_line_ids,
            information_ids=event_information_ids,
            new_information_ids=event_information_ids,
            state_in=(
                str(getattr(legacy, "state_in", "") or "").strip()
                or scene.entry_state
            ),
            primary_action=trigger or visible_change or observable,
            state_out=(
                str(getattr(legacy, "state_out", "") or "").strip()
                or scene.exit_state
            ),
            continuity_mode=(
                "scene_change"
                if previous_scene_id != scene_contract.scene_id
                else "same_scene_cut"
            ),
            duration_s=config.DEFAULT_VIDEO_DURATION_S,
            characters_visible=[],
            audio_cast=speakers,
            purpose=observable or scene.story_function,
            resulting_change=(
                str(getattr(legacy, "state_out", "") or "").strip()
                or scene.exit_state
            ),
            readability_focus=(
                "dialogue" if event_key_line_ids else "action"
            ),
            scene_id=scene_contract.scene_id,
            event_ids=[event.event_id],
        ))

    outline = StoryboardOutline(
        episode_no=screenplay.episode_no,
        shots=shots,
    )
    object.__setattr__(
        outline,
        "_compile_audit",
        {
            "compiler": "narrative-storyboard-outline.v2",
            "event_count": len(events),
            "scene_count": len(scenes),
            "base_shot_count": len(shots),
            "model_calls": 0,
        },
    )
    return outline
