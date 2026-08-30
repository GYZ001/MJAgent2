"""Dialogue-chain-specific repair helpers: locality checks for a chain
replacement, grounding character-decision-basis fields against source text,
and normalizing dialogue source references/continuity.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

from app import (
    config,
    textmatch,
)
from app.renderability import DIALOGUE_CHAIN_TURNS_HARD_MAX
from app.schemas import EpisodeScreenplay
from typing import Any


def _dialogue_chain_replacement_is_local(
    document: Any,
    *,
    chain_id: str,
    turns: Any,
    source_text: str = "",
) -> bool:
    """Allow body selection, or source-grounded recovery of one empty chain."""
    from app.production.screenplay_document import action_block_spoken_identity
    from app.spoken_contract import content_char_count

    if (
        not isinstance(turns, list)
        or not 1 <= len(turns) <= DIALOGUE_CHAIN_TURNS_HARD_MAX
    ):
        return False
    body_turns = {
        ((turn.speaker or "").strip(), (turn.line or "").strip())
        for block in document.scene_blocks
        for turn in block.dialogue_turns
        if (turn.speaker or "").strip() and (turn.line or "").strip()
    }
    body_turns.update(
        spoken
        for block in document.scene_blocks
        for action in block.action_blocks
        if (spoken := action_block_spoken_identity(action.text)) is not None
    )
    current_chain = next(
        (
            chain for chain in document.dialogue_chains
            if (chain.chain_id or "").strip() == chain_id
        ),
        None,
    )
    if current_chain is None:
        return False
    if not current_chain.turns:
        declared_speakers = {
            str(voice.speaker_id or "").strip()
            for voice in document.voice_bible
            if str(voice.speaker_id or "").strip()
        }
        plan = getattr(document, "narrative_plan", None)
        for identity in getattr(plan, "identity_contracts", []) if plan else []:
            declared_speakers.update({
                str(identity.identity_id or "").strip(),
                str(identity.display_name or "").strip(),
                *(
                    str(voice_id or "").strip()
                    for voice_id in (identity.voice_ids or [])
                ),
            })
        allowed_functions = {
            "trigger",
            "announcement",
            "question",
            "response",
            "decision",
            "statement",
        }
        candidate_turns: list[tuple[str, str]] = []
        for turn in turns:
            if not isinstance(turn, dict):
                return False
            speaker = str(turn.get("speaker") or "").strip()
            line = str(turn.get("line") or "").strip()
            function = str(turn.get("function") or "").strip()
            evidence = str(turn.get("source_text") or "").strip()
            if (
                not speaker
                or speaker not in declared_speakers
                or not line
                or content_char_count(line) > config.MAX_SPOKEN_CHARS_PER_SHOT
                or function not in allowed_functions
                or len(textmatch.condense(evidence)) < 2
                or not source_text
                or evidence not in source_text
                or not _source_references_are_grounded(turn, source_text)
            ):
                return False
            candidate_turns.append((speaker, line))
        return len(candidate_turns) == len(set(candidate_turns))

    current_turns = {
        ((turn.speaker or "").strip(), (turn.line or "").strip())
        for turn in current_chain.turns
    }
    candidate_turns: list[tuple[str, str]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            return False
        identity = (
            str(turn.get("speaker") or "").strip(),
            str(turn.get("line") or "").strip(),
        )
        if (
            not all(identity)
            or identity not in body_turns
        ):
            return False
        candidate_turns.append(identity)
    return (
        len(candidate_turns) == len(set(candidate_turns))
        and current_turns.issubset(set(candidate_turns))
    )


def _source_references_are_grounded(value: Any, source_text: str) -> bool:
    """Validate every nested source-bearing field against the authorized source."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"source_text", "verbatim_excerpt"}:
                excerpt = str(child or "").strip()
                if excerpt and excerpt not in source_text:
                    return False
            if not _source_references_are_grounded(child, source_text):
                return False
    elif isinstance(value, list):
        return all(
            _source_references_are_grounded(child, source_text)
            for child in value
        )
    return True


def _normalize_character_decision_basis(value: Any) -> Any:
    """Constrain decision bases to evidence perceived or propositions held by the node."""
    if isinstance(value, list):
        return [_normalize_character_decision_basis(child) for child in value]
    if not isinstance(value, dict):
        return value
    normalized = {
        key: _normalize_character_decision_basis(child)
        for key, child in value.items()
    }
    basis = normalized.get("decision_basis_ids")
    if not isinstance(basis, list):
        return normalized
    perceived = {
        str(item or "").strip()
        for item in normalized.get("perceived_evidence_ids") or []
        if str(item or "").strip()
    }
    held = {
        str(item.get("proposition_id") or "").strip()
        for item in normalized.get("beliefs") or []
        if isinstance(item, dict) and str(item.get("proposition_id") or "").strip()
    }
    allowed = perceived | held
    normalized["decision_basis_ids"] = [
        str(item)
        for item in basis
        if str(item or "").strip() in allowed
    ]
    return normalized


def _unique_source_dialogue(line: str, source_text: str) -> str | None:
    """Return one uniquely matching source utterance under the validator contract."""
    for opening, closing in (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"')):
        quoted = f"{opening}{line}{closing}"
        if line and source_text.count(quoted) == 1:
            return line

    from app import textmatch
    from app.validators import (
        KEY_LINE_BIGRAM_COVERAGE,
        KEY_LINE_PRESENT_RATIO,
        source_dialogue_fragments,
    )

    ranked: list[tuple[float, str]] = []
    for candidate in source_dialogue_fragments(source_text):
        run_score = textmatch.longest_run_ratio(line, candidate)
        coverage = textmatch.bigram_coverage(line, candidate)
        if (
            run_score >= KEY_LINE_PRESENT_RATIO
            or coverage >= KEY_LINE_BIGRAM_COVERAGE
        ):
            ranked.append((max(run_score, coverage), candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    best_score = ranked[0][0]
    best = {
        candidate
        for score, candidate in ranked
        if abs(score - best_score) < 1e-9
        and source_text.count(candidate) == 1
    }
    return next(iter(best)) if len(best) == 1 else None


def _normalize_dialogue_source_references(
    value: Any,
    source_text: str,
) -> Any:
    """Resolve a non-exact dialogue citation only when one source utterance matches."""
    if isinstance(value, list):
        return [
            _normalize_dialogue_source_references(child, source_text)
            for child in value
        ]
    if not isinstance(value, dict):
        return value

    normalized = {
        key: _normalize_dialogue_source_references(child, source_text)
        for key, child in value.items()
    }
    citation = str(normalized.get("source_text") or "").strip()
    line = str(normalized.get("line") or "").strip()
    speaker = str(normalized.get("speaker") or "").strip()
    if not citation or not line or not speaker:
        return normalized

    from app import textmatch

    citation_supports_line = (
        textmatch.spoken_digit_sequence_equivalent(citation, line)
        or textmatch.longest_run_ratio(line, citation)
        >= textmatch.KEY_LINE_PRESENT_RATIO
        or textmatch.bigram_coverage(line, citation)
        >= textmatch.KEY_LINE_BIGRAM_COVERAGE
    )
    if citation in source_text and citation_supports_line:
        return normalized

    source_dialogue = _unique_source_dialogue(line, source_text)
    if source_dialogue is not None:
        normalized["source_text"] = source_dialogue
    return normalized


def _normalize_dialogue_chain_continuity(
    script: EpisodeScreenplay,
    source_text: str,
) -> list[dict[str, Any]]:
    """Fill omitted intervening source-grounded turns before dependent responses."""
    from app.production.screenplay_document import (
        action_block_spoken_identity,
        screenplay_to_document,
    )
    from app.schemas import KeyDialogueTurn

    if not source_text or not script.dialogue_chains:
        return []
    changes: list[dict[str, Any]] = []
    from app.validators import source_dialogue_fragments

    source_dialogues = source_dialogue_fragments(source_text)
    allowed_speakers = {
        str(voice.speaker_id or "").strip()
        for voice in (script.voice_bible or [])
        if str(voice.speaker_id or "").strip()
    }
    if script.narrative_plan is not None:
        for contract in script.narrative_plan.identity_contracts:
            allowed_speakers.update({
                str(contract.identity_id or "").strip(),
                str(contract.display_name or "").strip(),
                *(
                    str(voice_id or "").strip()
                    for voice_id in (contract.voice_ids or [])
                ),
            })
    allowed_speakers.discard("")
    first_turn = (
        script.dialogue_chains[0].turns[0]
        if script.dialogue_chains[0].turns else None
    )
    if first_turn is not None and source_dialogues:
        opening = source_dialogues[0]
        matched = _unique_source_dialogue(first_turn.line or "", source_text)
        if (
            matched == opening
            and (first_turn.source_text or "").strip() != opening
        ):
            changes.append({
                "kind": "opening_dialogue_source",
                "id": f"{script.dialogue_chains[0].chain_id}-T1",
                "from": (first_turn.source_text or "").strip(),
                "to": opening,
            })
            first_turn.source_text = opening
    document = screenplay_to_document(script)
    observed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for block in document.scene_blocks:
        spoken = [
            (
                (turn.speaker or "").strip(),
                (turn.line or "").strip(),
            )
            for turn in block.dialogue_turns
        ]
        spoken.extend(
            identity
            for action in block.action_blocks
            if (identity := action_block_spoken_identity(action.text)) is not None
            and identity[0] in allowed_speakers
        )
        for speaker, line in spoken:
            identity = (block.scene_id, speaker, line)
            if not speaker or not line or identity in seen:
                continue
            source_dialogue = _unique_source_dialogue(line, source_text)
            if source_dialogue is None:
                continue
            seen.add(identity)
            observed.append({
                "scene_id": block.scene_id,
                "speaker": speaker,
                "line": line,
                "source_text": source_dialogue,
                "source_position": source_text.find(source_dialogue),
            })

    for chain in script.dialogue_chains:
        turns = list(chain.turns or [])
        if len(turns) >= DIALOGUE_CHAIN_TURNS_HARD_MAX:
            continue
        existing = {
            ((turn.speaker or "").strip(), (turn.line or "").strip())
            for turn in turns
        }
        additions: list[dict[str, Any]] = []
        for turn_index, turn in enumerate(turns):
            if (turn.function or "").strip() != "response" or turn_index == 0:
                continue
            response_identity = (
                (turn.speaker or "").strip(),
                (turn.line or "").strip(),
            )
            response_matches = [
                item for item in observed
                if (item["speaker"], item["line"]) == response_identity
            ]
            if len(response_matches) != 1:
                continue
            response = response_matches[0]
            previous = turns[turn_index - 1]
            previous_source = (
                (previous.source_text or "").strip()
                if (previous.source_text or "").strip() in source_text
                else _unique_source_dialogue(previous.line or "", source_text)
            )
            if previous_source is None:
                continue
            previous_position = source_text.find(previous_source)
            eligible = [
                item for item in observed
                if (
                    item["scene_id"] == response["scene_id"]
                    and previous_position < item["source_position"] < response["source_position"]
                    and (item["speaker"], item["line"]) not in existing
                    and (item["speaker"], item["line"]) not in {
                        (added["speaker"], added["line"]) for added in additions
                    }
                )
            ]
            remaining = (
                DIALOGUE_CHAIN_TURNS_HARD_MAX
                - len(turns)
                - len(additions)
            )
            if remaining <= 0 or not eligible:
                continue
            selected = sorted(
                eligible,
                key=lambda item: item["source_position"],
            )[-remaining:]
            if not any(item["speaker"] != response["speaker"] for item in selected):
                continue
            additions.extend(selected)
        if not additions:
            continue

        combined: list[tuple[int, int, KeyDialogueTurn]] = []
        for index, turn in enumerate(turns):
            source_dialogue = (
                (turn.source_text or "").strip()
                if (turn.source_text or "").strip() in source_text
                else _unique_source_dialogue(turn.line or "", source_text)
            )
            position = (
                source_text.find(source_dialogue)
                if source_dialogue is not None
                else len(source_text) + index
            )
            combined.append((position, index, turn))
        for offset, item in enumerate(additions, start=len(turns)):
            combined.append((
                int(item["source_position"]),
                offset,
                KeyDialogueTurn(
                    speaker=item["speaker"],
                    line=item["line"],
                    function=(
                        "question"
                        if item["line"].rstrip().endswith(("?", "？"))
                        else "statement"
                    ),
                    source_text=item["source_text"],
                ),
            ))
        combined.sort(key=lambda item: (item[0], item[1]))
        chain.turns = [item[2] for item in combined]
        changes.append({
            "kind": "dialogue_chain_continuity",
            "id": chain.chain_id,
            "added_turns": [
                {
                    "speaker": item["speaker"],
                    "line": item["line"],
                    "source_text": item["source_text"],
                }
                for item in additions
            ],
        })
    return changes


