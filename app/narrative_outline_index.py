"""Event-id canonicalization, event ordering, key-line cataloging and
base-shot indexing for normalize_narrative_storyboard_outline.

Split out of narrative_outline.py -- see that function's docstring.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.schemas import EpisodeScreenplay, StoryboardOutline


def _canonicalize_outline_event_ids(outline: StoryboardOutline, events: dict[str, Any]) -> None:
    """Repair typo'd/aliased event ids on each shot to their canonical event_id."""
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


def _build_outline_event_order(plan: Any) -> dict[str, int]:
    """Index each event's position in plan.events."""
    event_order = {
        item.event_id: position
        for position, item in enumerate(plan.events)
    }
    return event_order


def _build_outline_key_line_catalog(
    screenplay: EpisodeScreenplay,
) -> tuple[dict[str, tuple[str, str, str]], dict[str, list[str]]]:
    """Catalog key_lines by (speaker, spoken-text) and assign each dialogue-chain turn a key_line id.

    Returns (key_line_meta, chain_key_ids).
    """
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
    return key_line_meta, chain_key_ids


def _build_outline_base_shot_indices(
    outline: StoryboardOutline,
    events: dict[str, Any],
    event_order: dict[str, int],
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Index the model-authored base shot(s) already covering each event, ordered by earliest-owned event.

    Returns (base_by_event, bases_by_event).
    """
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
    return base_by_event, bases_by_event
