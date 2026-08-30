"""Aligning dialogue lines/turns back to verbatim source-text evidence:
candidate sentence scoring, best-evidence-span selection and the pass that
normalizes dialogue lines to their nearest verified source span.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import re
from app import (
    config,
    textmatch,
)
from app.schemas import EpisodeScreenplay
from difflib import SequenceMatcher
from typing import Any

from .gates import (
    _SOURCE_EVIDENCE_STOP_CHARS,
    _SOURCE_SENTENCE_RE,
)


def _dialogue_turn_at(
    script: EpisodeScreenplay,
    chain_index: int,
    turn_index: int,
):
    chains = script.dialogue_chains or []
    if not 0 <= chain_index < len(chains):
        return None
    chain = chains[chain_index]
    turns = chain.turns or []
    if not 0 <= turn_index < len(turns):
        return None
    return chain, turns[turn_index]


def _source_sentence_candidates(source_text: str) -> list[str]:
    candidates: list[str] = []
    for paragraph in re.split(r"\n+", source_text or ""):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        sentences = [
            match.group(0).strip()
            for match in _SOURCE_SENTENCE_RE.finditer(paragraph)
            if match.group(0).strip()
        ]
        for index, sentence in enumerate(sentences):
            candidates.append(sentence)
            if index + 1 < len(sentences):
                candidates.append(f"{sentence}{sentences[index + 1]}")
    return list(dict.fromkeys(candidates))


def _source_evidence_score(candidate: str, target: str, context: str) -> float:
    compact_candidate = re.sub(r"\W+", "", candidate)
    compact_target = re.sub(r"\W+", "", target)
    compact_context = re.sub(r"\W+", "", context)
    if not compact_candidate or not compact_target:
        return 0.0

    meaningful_target = {
        char for char in compact_target
        if char not in _SOURCE_EVIDENCE_STOP_CHARS
    }
    meaningful_overlap = meaningful_target & set(compact_candidate)
    if len(meaningful_overlap) < 2:
        return 0.0

    target_bigrams = {
        compact_target[index:index + 2]
        for index in range(max(0, len(compact_target) - 1))
    }
    candidate_bigrams = {
        compact_candidate[index:index + 2]
        for index in range(max(0, len(compact_candidate) - 1))
    }
    context_bigrams = {
        compact_context[index:index + 2]
        for index in range(max(0, len(compact_context) - 1))
    }
    target_coverage = (
        len(target_bigrams & candidate_bigrams) / len(target_bigrams)
        if target_bigrams else 0.0
    )
    context_coverage = (
        len(context_bigrams & candidate_bigrams) / len(context_bigrams)
        if context_bigrams else 0.0
    )
    sequence = SequenceMatcher(None, compact_target, compact_candidate).ratio()
    char_coverage = len(meaningful_overlap) / max(1, len(meaningful_target))
    length_penalty = min(0.2, max(0, len(compact_candidate) - 100) / 500)
    return (
        target_coverage * 5.0
        + char_coverage * 2.0
        + sequence
        + context_coverage * 0.75
        - length_penalty
    )


def _best_source_evidence_for_turn(
    script: EpisodeScreenplay,
    *,
    chain_index: int,
    turn_index: int,
    source_text: str,
) -> str:
    turn_ref = _dialogue_turn_at(script, chain_index, turn_index)
    if turn_ref is None:
        return ""
    chain, turn = turn_ref
    target = (turn.line or "").strip()
    if not target:
        return ""

    context_parts = [chain.topic or ""]
    full_script = script.full_script_text or ""
    line_offset = full_script.find(target)
    if line_offset >= 0:
        headings = list(re.finditer(r"【场\s*(\d+)】", full_script[:line_offset]))
        if headings:
            scene_no = int(headings[-1].group(1))
            scene = next(
                (
                    item for item in (script.scene_outline or [])
                    if int(item.scene_no) == scene_no
                ),
                None,
            )
            if scene is not None:
                context_parts.extend([
                    scene.source_basis or "",
                    scene.summary or "",
                    scene.conflict or "",
                    scene.turn or "",
                ])
    context = " ".join(part for part in context_parts if part)

    ranked = sorted(
        (
            (_source_evidence_score(candidate, target, context), candidate)
            for candidate in _source_sentence_candidates(source_text)
        ),
        key=lambda item: (item[0], -len(item[1])),
        reverse=True,
    )
    if not ranked or ranked[0][0] < 1.0:
        return ""
    return ranked[0][1]


def _source_evidence_span(
    chapter: str,
    excerpt: str,
    *,
    context: str = "",
) -> tuple[int, int, str | None] | None:
    """Resolve one exact raw span, optionally expanding a proven excerpt elision."""
    from app.narrative import normalize_source_evidence_text

    normalized_excerpt = normalize_source_evidence_text(excerpt)
    raw_positions = [
        raw_index
        for raw_index, char in enumerate(chapter)
        if not char.isspace()
    ]
    normalized_chapter = "".join(chapter[index] for index in raw_positions)
    if not normalized_excerpt or not raw_positions:
        return None

    def occurrences(haystack: str, needle: str) -> list[int]:
        found: list[int] = []
        cursor = 0
        while needle:
            offset = haystack.find(needle, cursor)
            if offset < 0:
                break
            found.append(offset)
            cursor = offset + 1
        return found

    exact_offsets = occurrences(normalized_chapter, normalized_excerpt)
    if len(exact_offsets) == 1:
        offset = exact_offsets[0]
        return (
            raw_positions[offset],
            raw_positions[offset + len(normalized_excerpt) - 1] + 1,
            None,
        )
    if len(exact_offsets) > 1 and context:
        ranked: list[tuple[float, int]] = []
        for offset in exact_offsets:
            raw_start = raw_positions[offset]
            raw_end = raw_positions[offset + len(normalized_excerpt) - 1] + 1
            window = chapter[
                max(0, raw_start - 320):min(len(chapter), raw_end + 320)
            ]
            score = max(
                textmatch.longest_run_ratio(context, window),
                textmatch.bigram_coverage(context, window),
            )
            ranked.append((score, offset))
        ranked.sort(reverse=True)
        best_score, best_offset = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else -1.0
        if best_score >= 0.2 and best_score - second_score >= 0.03:
            return (
                raw_positions[best_offset],
                raw_positions[
                    best_offset + len(normalized_excerpt) - 1
                ] + 1,
                None,
            )
    match_excerpt = re.sub(
        r"(?:…{2,}|\.{3,})",
        "",
        normalized_excerpt,
    )
    if exact_offsets or len(match_excerpt) < 24:
        return None

    # A model may concatenate two exact source regions while omitting an
    # irrelevant paragraph. Recover only when unique prefix/suffix anchors prove
    # one bounded containing span and the authored excerpt is an ordered
    # subsequence of it. This expands evidence; it never invents or fuzzy-edits it.
    anchor_size = min(32, max(12, len(match_excerpt) // 8))
    prefix = match_excerpt[:anchor_size]
    suffix = match_excerpt[-anchor_size:]
    prefix_offsets = occurrences(normalized_chapter, prefix)
    suffix_offsets = occurrences(normalized_chapter, suffix)
    candidates: list[tuple[int, int, int]] = []
    for start in prefix_offsets:
        for suffix_start in suffix_offsets:
            end = suffix_start + len(suffix)
            if end <= start or end - start < len(match_excerpt):
                continue
            segment = normalized_chapter[start:end]
            extra = len(segment) - len(match_excerpt)
            cursor = 0
            for char in segment:
                if cursor < len(match_excerpt) and char == match_excerpt[cursor]:
                    cursor += 1
            matching_coverage = (
                sum(
                    block.size
                    for block in SequenceMatcher(
                        None,
                        match_excerpt,
                        segment,
                        autojunk=False,
                    ).get_matching_blocks()
                )
                / max(1, len(match_excerpt))
            )
            if (
                cursor == len(match_excerpt)
                or matching_coverage >= 0.98
            ):
                candidates.append((extra, start, end))

    if not candidates:
        return None
    candidates.sort()
    best_extra = candidates[0][0]
    best = [item for item in candidates if item[0] == best_extra]
    if len(best) != 1:
        return None
    _extra, start, end = best[0]
    raw_start = raw_positions[start]
    raw_end = raw_positions[end - 1] + 1
    return raw_start, raw_end, chapter[raw_start:raw_end]


def _normalize_dialogue_lines_to_source(
    script: EpisodeScreenplay,
    source_text: str,
) -> list[dict[str, Any]]:
    """Use exact source utterances as spoken text and split only for capacity."""
    if not source_text or not script.dialogue_chains:
        return []
    from app.screenplay_ir import _split_spoken_line
    from app.validators import source_dialogue_fragments

    source_dialogues = source_dialogue_fragments(source_text)
    changes: list[dict[str, Any]] = []
    for chain in script.dialogue_chains:
        turns = list(chain.turns or [])
        normalized_turns = []
        index = 0
        while index < len(turns):
            turn = turns[index]
            citation = str(turn.source_text or "").strip()
            speaker = str(turn.speaker or "").strip()
            group = [turn]
            cursor = index + 1
            while (
                citation
                and cursor < len(turns)
                and str(turns[cursor].speaker or "").strip() == speaker
                and textmatch.condense(turns[cursor].source_text)
                == textmatch.condense(citation)
            ):
                group.append(turns[cursor])
                cursor += 1

            compact_citation = textmatch.condense(citation)
            containing = [
                candidate
                for candidate in source_dialogues
                if compact_citation
                and compact_citation in textmatch.condense(candidate)
            ]
            evidence = (
                containing[0]
                if len(containing) == 1
                else citation if citation in source_text else ""
            )
            spoken_parts = (
                _split_spoken_line(
                    evidence,
                    max_chars=config.MAX_SPOKEN_CHARS_PER_SHOT,
                )
                if evidence
                else []
            )
            if not spoken_parts:
                normalized_turns.extend(group)
                index = cursor
                continue

            current_parts = [str(item.line or "").strip() for item in group]
            current_evidence = [
                str(item.source_text or "").strip() for item in group
            ]
            if (
                current_parts == spoken_parts
                and all(value == evidence for value in current_evidence)
            ):
                normalized_turns.extend(group)
                index = cursor
                continue

            replacements = []
            for part_index, part in enumerate(spoken_parts):
                replacement = group[0].model_copy(deep=True)
                replacement.line = part
                replacement.source_text = evidence
                if part_index:
                    replacement.function = "statement"
                replacements.append(replacement)
            normalized_turns.extend(replacements)

            old_block = "\n".join(
                f"{speaker}：{line}" for line in current_parts
            )
            new_block = "\n".join(
                f"{speaker}：{line}" for line in spoken_parts
            )
            if old_block and old_block in (script.full_script_text or ""):
                script.full_script_text = script.full_script_text.replace(
                    old_block,
                    new_block,
                    1,
                )
            changes.append({
                "kind": "dialogue_source_authority",
                "id": chain.chain_id,
                "turn_index": index,
                "from_lines": current_parts,
                "to_lines": spoken_parts,
                "source_text": evidence,
            })
            index = cursor
        chain.turns = normalized_turns
    return changes


