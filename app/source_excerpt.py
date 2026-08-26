"""Deterministic alignment for model-produced source evidence excerpts."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata


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


def quotation_opening(char: str) -> bool:
    """Return whether one Unicode character structurally opens a quote."""
    category = unicodedata.category(char)
    return (
        category == "Pi"
        or (
            category not in {"Pi", "Pf"}
            and "QUOTATION MARK" in unicodedata.name(char, "")
        )
    )


def quotation_closing(opening: str, char: str) -> bool:
    """Return whether ``char`` closes the active structural quote."""
    if unicodedata.category(opening) == "Pi":
        return unicodedata.category(char) == "Pf"
    return (
        "QUOTATION MARK" in unicodedata.name(char, "")
        and char == opening
    )


def unclosed_quotation(
    text: str,
    *,
    start: int = 0,
    end: int | None = None,
) -> tuple[str, int] | None:
    """Return the active opening quote and offset at the end of a span."""
    opening = ""
    opening_offset = -1
    upper = len(text) if end is None else min(len(text), end)
    for offset in range(max(0, start), upper):
        char = text[offset]
        if not opening:
            if quotation_opening(char):
                opening = char
                opening_offset = offset
            continue
        if quotation_closing(opening, char):
            opening = ""
            opening_offset = -1
    return (opening, opening_offset) if opening else None


def _extend_cut_through_closing_quote(
    raw: str,
    *,
    cursor: int,
    cut: int,
    end: int,
) -> int:
    active = unclosed_quotation(raw, start=cursor, end=cut)
    if active is None:
        return cut
    opening, _opening_offset = active
    for offset in range(cut, end):
        if quotation_closing(opening, raw[offset]):
            return offset + 1
    return cut


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

    # A quotation may span paragraph breaks (common in web-novel monologue and
    # multi-line speech). Merge adjacent paragraph spans while a quote stays open
    # so one quotation is never cut into unbalanced fragments that fail source
    # fact extraction downstream. If a quote is never closed through the end of
    # the source it is left as-is; source_segment_facts closes it deterministically.
    if spans:
        merged_spans: list[tuple[int, int]] = []
        index = 0
        while index < len(spans):
            start, end = spans[index]
            while (
                unclosed_quotation(raw, start=start, end=end) is not None
                and index + 1 < len(spans)
            ):
                index += 1
                end = spans[index][1]
            merged_spans.append((start, end))
            index += 1
        spans = merged_spans

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
            cut = _extend_cut_through_closing_quote(
                raw,
                cursor=cursor,
                cut=cut,
                end=end,
            )
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


def _normalize_whitespace_compact(text: str) -> str:
    """Strip every whitespace run for a pure structural-equality comparison
    (归一化空白) -- used only by chapter_title_segment_ids below, never for
    any other kind of text matching in this module (align_source_excerpt's
    own normalization is unrelated and untouched)."""
    return re.sub(r"\s+", "", text or "")


def chapter_title_segment_ids(
    segments: list[SourceSegment],
    chapter_titles: list[str],
) -> set[str]:
    """Return segment_ids whose ENTIRE text is composed of nothing but one of
    ``chapter_titles`` -- i.e. the segment IS a chapter heading, decided from
    the project's own ``chapters.title`` column (a DB-anchored fact) instead
    of a hardcoded name/keyword list or a generic structural guess (project
    rule: judgments must be derived from data, see CLAUDE.md's blacklist
    ban) or the model's own free-text declaration (see app.validators.
    build_prep_pack_span_ledger's chapter_titles parameter / PREP_PACK_
    VERSION's 1.9.0 note in app.production.prep_pack for the regression this
    fixes -- a real EP5 chapter-title segment became a pseudo "显示章节标题"
    event because the model's own paratext_segments declaration is non-
    deterministic and randomly omits it).

    A segment counts as a pure title segment if, after stripping all
    whitespace, its text equals one of ``title``, ``【title】``, ``[title]``,
    or any concatenation of two of those three forms -- the last case covers
    a known web-novel join artifact (not something this function invents):
    app.domain.common._episode_source_text joins each chapter as
    ``"【{chapters.title}】\\n{chapters.content}"``, and chapters.content
    itself frequently repeats its own title as its first line (real EP5
    data: chapters.idx=5, title="第五章此子不错", content starts with
    "第五章此子不错\\n\\n..."), so the wrapper title and content's own first
    line collapse into one index_source_segments paragraph, literally
    "【第五章此子不错】\\n第五章此子不错". Multiple chapters (an episode
    spanning more than one chapter) and multiple candidate titles are all
    checked against every segment, not just the first one -- a title can
    legitimately appear at any chapter-start boundary inside the document,
    not only at offset 0.

    Titles that are empty/whitespace-only are ignored (this is exactly the
    NULL/blank chapters.title case the caller is expected to filter out
    before calling, so that chapter falls back to the regex+model-declare
    path in structural_front_matter_ids/build_prep_pack_span_ledger instead
    of being silently treated as "no title anywhere").
    """
    result: set[str] = set()
    normalized_titles = {
        _normalize_whitespace_compact(title)
        for title in chapter_titles
        if title and title.strip()
    }
    normalized_titles.discard("")
    if not segments or not normalized_titles:
        return result
    for segment in segments:
        compact = _normalize_whitespace_compact(segment.text)
        if not compact:
            continue
        for title in normalized_titles:
            variants = {title, f"【{title}】", f"[{title}]"}
            if compact in variants or any(
                compact == first + second
                for first in variants
                for second in variants
            ):
                result.add(segment.segment_id)
                break
    return result


def chapter_title_segment_indexes(
    segments: list[SourceSegment],
    chapter_titles: list[str],
) -> set[int]:
    """1-based-index form of chapter_title_segment_ids -- both
    app.production.prep_pack._extract_chunk (prompt injection) and
    app.validators.build_prep_pack_span_ledger (ledger gate) key segments by
    their ordinal position among index_source_segments' output, not by the
    fixed-width SRC%04d segment_id string, so this is the one shared
    conversion both call sites use rather than each re-deriving it."""
    if not segments or not chapter_titles:
        return set()
    ids = chapter_title_segment_ids(segments, chapter_titles)
    return {
        index for index, segment in enumerate(segments, start=1)
        if segment.segment_id in ids
    }


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
