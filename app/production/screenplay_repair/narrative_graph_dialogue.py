"""Dialogue-chain, dialogue-topic and source-evidence repair phases of
_normalize_screenplay_narrative_graph.

Split out of narrative_graph_normalize.py (see that file's module docstring
for why the original single function existed and what this split changes --
nothing behaviorally, only where each phase's code lives).
"""
from __future__ import annotations

from typing import Any

from app.schemas import EpisodeScreenplay

from .dialogue_chain_repair import _normalize_dialogue_chain_continuity
from .dialogue_source_alignment import (
    _normalize_dialogue_lines_to_source,
    _source_evidence_span,
)


def _repair_dialogue_chain_continuity(
    script: EpisodeScreenplay,
    changes: list[dict[str, Any]],
) -> None:
    """Re-run dialogue-chain normalization and log it as a change if it moved anything."""
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


def _normalize_short_dialogue_topics(
    script: EpisodeScreenplay,
    changes: list[dict[str, Any]],
) -> None:
    """Regenerate auto-generated topics for dialogue chains whose topic is too short to be real."""
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


def _build_authorized_source_chapters(
    authorized_source_chapters: dict[str, str] | None,
) -> dict[str, str]:
    """Normalize the raw chapter-id/text mapping into a clean str->str dict."""
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
    return chapters


def _repair_dialogue_source_alignment(
    script: EpisodeScreenplay,
    chapters: dict[str, str],
    changes: list[dict[str, Any]],
) -> None:
    """Align dialogue lines and dialogue-chain continuity to the authorized source text."""
    from app.validators import normalize_screenplay_dialogue_chains

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


def _repair_source_evidence_spans(
    data: dict[str, Any],
    chapters: dict[str, str],
    changes: list[dict[str, Any]],
) -> None:
    """Repair each source_evidence entry's chapter_id/span/excerpt against the authorized chapters."""
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

