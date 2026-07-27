"""Deterministic alignment for model-produced source evidence excerpts."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher


_PUNCTUATION_EQUIVALENTS = str.maketrans({
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "「": '"',
    "」": '"',
    "『": '"',
    "』": '"',
    "＂": '"',
    "‘": "'",
    "’": "'",
    "，": ",",
    "。": ".",
    "：": ":",
    "；": ";",
    "！": "!",
    "？": "?",
})


@dataclass(frozen=True)
class AlignedExcerpt:
    excerpt: str
    start_offset: int
    end_offset: int
    match_chars: int
    exact: bool


def _alignment_view(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    for offset, char in enumerate(text):
        if char.isspace():
            continue
        chars.append(char.translate(_PUNCTUATION_EQUIVALENTS))
        offsets.append(offset)
    return "".join(chars), offsets


def align_source_excerpt(
    candidate: str,
    source: str,
    *,
    min_match_chars: int = 8,
) -> AlignedExcerpt | None:
    """Return a provable contiguous source slice for an AI-selected excerpt.

    Models occasionally replace Chinese quotation marks or join two nearby source
    fragments with an ellipsis.  The audit field must still be byte-for-byte source
    text.  We therefore keep exact excerpts unchanged and otherwise retain only the
    longest contiguous source-backed fragment.  Weak generic overlaps are rejected
    instead of being presented as evidence.
    """
    candidate = (candidate or "").strip()
    source = source or ""
    if not candidate or not source:
        return None

    exact_start = source.find(candidate)
    if exact_start >= 0:
        normalized, _ = _alignment_view(candidate)
        return AlignedExcerpt(
            excerpt=candidate,
            start_offset=exact_start,
            end_offset=exact_start + len(candidate),
            match_chars=len(normalized),
            exact=True,
        )

    candidate_view, _ = _alignment_view(candidate)
    source_view, source_offsets = _alignment_view(source)
    if not candidate_view or not source_view:
        return None
    match = SequenceMatcher(
        None,
        candidate_view,
        source_view,
        autojunk=False,
    ).find_longest_match()
    # Long rewritten candidates must contain a meaningful source anchor; a shared
    # character name or generic phrase is not enough to establish provenance.
    required = min(
        len(candidate_view),
        max(min_match_chars, min(18, len(candidate_view) // 5)),
    )
    if match.size < required:
        return None

    start = source_offsets[match.b]
    end = source_offsets[match.b + match.size - 1] + 1
    excerpt = source[start:end]
    if len(excerpt.strip()) < min_match_chars:
        return None
    return AlignedExcerpt(
        excerpt=excerpt,
        start_offset=start,
        end_offset=end,
        match_chars=match.size,
        exact=False,
    )
