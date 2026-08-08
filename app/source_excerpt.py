"""Deterministic alignment for model-produced source evidence excerpts."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re


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


@dataclass(frozen=True)
class SourceSegment:
    segment_id: str
    text: str
    start_offset: int
    end_offset: int


def index_source_segments(
    source: str,
    *,
    max_chars: int = 900,
) -> list[SourceSegment]:
    """Create stable, exhaustive source units without asking the model for offsets."""
    raw = source or ""
    if not raw.strip():
        return []
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", raw, flags=re.S):
        start, end = match.span()
        text = raw[start:end].strip()
        if not text:
            continue
        left = raw.find(text, start, end)
        spans.append((left, left + len(text)))

    chunks: list[tuple[int, int]] = []
    for start, end in spans:
        cursor = start
        while end - cursor > max_chars:
            window_end = min(end, cursor + max_chars)
            cut = max(
                raw.rfind(mark, cursor + max_chars // 2, window_end)
                for mark in ("。", "！", "？", "\n")
            )
            if cut < cursor:
                cut = window_end
            else:
                cut += 1
            chunks.append((cursor, cut))
            cursor = cut
            while cursor < end and raw[cursor].isspace():
                cursor += 1
        if cursor < end:
            chunks.append((cursor, end))

    return [
        SourceSegment(
            segment_id=f"SRC{index:04d}",
            text=raw[start:end].strip(),
            start_offset=start,
            end_offset=end,
        )
        for index, (start, end) in enumerate(chunks, start=1)
        if raw[start:end].strip()
    ]


def render_indexed_source(source: str, *, max_chars: int = 900) -> str:
    return "\n\n".join(
        f"【{segment.segment_id}】\n{segment.text}"
        for segment in index_source_segments(source, max_chars=max_chars)
    )


_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:[【\[]\s*)?第\s*[0-9一二三四五六七八九十百千]+\s*章"
)


def structural_front_matter_ids(
    segments: list[SourceSegment],
) -> set[str]:
    """Return only document headings that are provably not dramatic content."""
    if not segments or not _CHAPTER_HEADING_RE.match(segments[0].text):
        return set()
    result = {segments[0].segment_id}
    if len(segments) < 2:
        return result
    subtitle = segments[1].text.strip()
    if (
        len(re.sub(r"\s+", "", subtitle)) <= 32
        and "\n" not in subtitle
        and not re.search(r"[。！？!?：“”「」『』]", subtitle)
    ):
        result.add(segments[1].segment_id)
    return result


def index_compact_source_segments(
    source: str,
    *,
    max_chars: int = 900,
) -> list[SourceSegment]:
    """Pack adjacent short paragraphs into exhaustive generation units.

    The legacy index intentionally preserves paragraph boundaries. That creates
    hundreds of IDs for novels formatted with one short sentence per paragraph,
    which in turn pressures the model to repeat one event per paragraph. The
    compact index keeps exact offsets and text while packing adjacent legacy
    units up to the same size bound.
    """
    raw = source or ""
    legacy = index_source_segments(raw, max_chars=max_chars)
    if not legacy:
        return []
    packed: list[tuple[int, int]] = []
    start = legacy[0].start_offset
    end = legacy[0].end_offset
    for segment in legacy[1:]:
        if segment.end_offset - start <= max_chars:
            end = segment.end_offset
            continue
        packed.append((start, end))
        start = segment.start_offset
        end = segment.end_offset
    packed.append((start, end))
    return [
        SourceSegment(
            segment_id=f"SRC{index:04d}",
            text=raw[start:end].strip(),
            start_offset=start,
            end_offset=end,
        )
        for index, (start, end) in enumerate(packed, start=1)
        if raw[start:end].strip()
    ]


def render_compact_indexed_source(
    source: str,
    *,
    max_chars: int = 900,
) -> str:
    return "\n\n".join(
        f"【{segment.segment_id}】\n{segment.text}"
        for segment in index_compact_source_segments(
            source,
            max_chars=max_chars,
        )
    )


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
        if len(normalized) < min_match_chars:
            return None
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
