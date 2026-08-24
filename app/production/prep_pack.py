"""Lightweight episode_prep_pack generation & atomic publish.

Screenplay contract 6.0.0 (docs/TRANSFORM_FREEZE_PLAN.md §3). Replaces the
heavy blueprint -> scene-shard -> compile -> repair pipeline
(app/production/screenplay_repair.py and friends -- left dormant, not deleted,
simply no longer called) for new runs. ``workflow_type`` stays ``'screenplay'``:
the state machine, episodes.screenplay_status lifecycle, monitoring and driver
all key off it unchanged. Only the business logic that runs *inside* one
screenplay Run is new; Run/Step/Artifact harness and the completion-certificate
machinery (app/production/certificate.py) are reused as-is.

Frozen artifact payload shape (single source of truth -- field names must not
change; see the task brief / docs/TRANSFORM_FREEZE_PLAN.md §3):
{
  "prep_pack_version": "1.5.0",
  "episode_no": int,
  "episode_scope": {"chapter_indexes": [int], "source_segment_count": int},
  "event_chain": [{
      "event_id": str, "order": int, "summary": str,
      # source_span carries the EXTENDED value (see app.validators.
      # build_prep_pack_span_ledger's 语义分离 note, 1.5.0/ERR-20260824-22cb1c):
      # adjacent events' source_span may legitimately OVERLAP by a segment or
      # two -- that overlap is delivery-evidence spillover (a later event's
      # own verified quote reached one segment into a shared transition),
      # NOT a narrative-boundary claim. P1 storyboard consumers must not
      # treat source_span overlap as "these two events cover the same beat
      # twice"; the model's own declared span (not published here, only the
      # extended result is) is the actual narrative-order claim, and that
      # never overlaps by construction (see coverage_ledger's fatal rules).
      "source_span": {"from_segment": int, "to_segment": int},
      "source_evidence": [{"segment_index": int, "quote": str}],
      "key_lines": [
          {"speaker": str, "line": str, "segment_index": int, "speaker_ref": str},
      ],
  }],
  "asset_manifest": {
      "characters": [{"identity_id": str, "display_name": str,
                       "portrait_id": str, "event_ids": [str], "aliases": [str]}],
      "scenes": [{"scene_id": str, "display_name": str,
                  "scene_reference_id": str, "event_ids": [str]}],
      "functional_extras": [{"label": str, "event_ids": [str]}],
  },
  "coverage_ledger": {"total_segments": int, "delivered": [int], "merged": [int],
      "retained_as_context": [int],
      "proven_duplicates": [{"segment_index": int, "duplicate_of_segment_index": int}],
      "paratext": [int], "uncovered": [int]},
  "hook": str, "cliffhanger": str,
}

1.2.0 (coordinator amendment, real-EP2 field bug): asset_manifest.characters
entries gained ``aliases`` -- the raw in-episode mention strings (e.g. a
nickname like "小胖子") that were disambiguated to this character's canonical
name, for P1 storyboard prompts. This accompanies a correctness fix, not a
cosmetic one: resolution no longer exempts a mention just because the
event-chain extraction model guessed ``is_background_extra=true`` on it --
that guess is untrusted prose from a model call that never looked at the
bible, and treating it as authoritative silently dropped a real, already-
carded, portrait-bearing character ("小胖子" == 李富贵) from a real EP2
artifact. See _resolve_assets below for the corrected flow.

1.3.0 (coordinator amendment, real-EP13 finding): asset_manifest gained
``functional_extras`` -- one-off/occupation-title character mentions
("养丹坊掌柜", "围观弟子") that app.portraits' identity discovery neither
resolved to a real identity nor explicitly failed on, kept under their own
raw source label per app.portraits' long-standing rule for unconfirmed-real-
name one-offs (portraits.py:1727, 7340: typed functional identity, never
silently dropped, never renamed to something generic). A real EP13 run
proved discovery's own candidate phrasing does not always exactly match
prep_pack's chunk-extraction phrasing for the same crowd concept (discovery
said "外宗弟子", the chunk extractor said "一名外宗弟子") even though
discovery ran cleanly and, in the same call, successfully carded+portraited a
genuinely new character ("曹阳"). Silence from a clean discovery run is not
grounds to gate-fail; only discovery explicitly failing on that specific name
still blocks (_discovery_errored_names). ``characters`` keeps its
existing portrait_id-bearing-only meaning; P1 storyboard prompts need
functional_extras to know who else is in frame. See _resolve_assets below.

1.4.0 (coordinator decision, later retired the same day -- see 1.4.1 below):
coverage_ledger gained a fifth account, ``paratext``. The first cut classified
it purely deterministically from the source text's own shape (keyword table +
position rules, no model involvement at all). A real round-15 EP2 regression
against a real chapter (proj_3ac0b627fa46, chapters.idx=2) proved that
approach could not stay both precise and complete against real author
phrasing -- see 1.4.1.

1.4.1 (coordinator decision, real round-15 EP2 regression fix): paratext
classification mechanism replaced -- the model itself already recognizes an
author's note when it reads one (the real regression: it spontaneously
summarized the offending segments as an event named "作者发布留言"), so v1's
mistake was trying to re-derive that recognition from the source text alone
instead of just capturing it. Under 1.4.1, the model DECLARES which of its
own chunk's segments are paratext (``paratext_segments`` in _ChunkResponse,
see _extract_chunk's prompt) and skips building narrative events for them;
app.validators.build_prep_pack_span_ledger then runs three independent
deterministic veto gates over that declaration (position / no-dependency /
exclusivity -- see its module comment above PARATEXT_TAIL_WINDOW_SEGMENTS for
the full argument) before any of it is trusted. This is still a bookkeeping
change, not a coverage-gate weakening: 洞即删戏 still applies to every
segment that is *not* accepted paratext, an over-claimed segment has no
silent path (it just falls back to needing real event coverage, gate-blocked
if none exists), and a segment that somehow ends up both accepted-paratext
*and* inside some event's validated span is a fatal ledger contradiction
(exclusivity gate) exactly as under 1.4.0, not silently tolerated either way.
The event-chain extraction prompt still receives the full chunk text
(paratext segments included, for narrative context) but now asks the model
to identify+declare them itself rather than being told in advance which
numbers are exempt.

1.4.2 (coordinator decision, real round-16 EP5 regression fix): asset
resolution (_resolve_assets) gained a text-evidence gate. Real EP5 output
bound two events describing an unnamed pair of old men on an unrelated
mountain peak near 靠山宗 to a pre-existing character ("丹鬼") and scene
("大青山山顶") from elsewhere in the story -- chapter 5's own text has zero
occurrences of either string (verified directly against the stored chapter
content), and the event-chain extraction model wrote those exact names
directly as characters[]/scenes[].display_name, not the text's own
descriptive terms ("灰袍老者"/"山顶老者" only ever appeared as
key_lines[].speaker, a field _resolve_assets never reads). Root cause:
neither _resolve_portrait_id nor _resolve_scene_reference_id required any
evidence beyond "a DB row with this exact name exists for some episode" --
a bare name-string coincidence was silently trusted. Fix (see
_prep_pack_mention_has_text_evidence and its two call sites in _pass, both
inside _resolve_assets): a direct name bind now additionally requires the
raw mention text to appear verbatim in this episode's own source_text.
Character and scene failure modes are deliberately asymmetric per the
coordinator's instruction: a character mention that resolves (directly or
via a discovery rename) but has no textual evidence for its own mention
text is a named, hard PrepPackGateError-eligible error (rerouting it to
discovery risks repeating the same confident-but-wrong guess); a scene
direct hit with no evidence is instead treated as unresolved and rerouted
into the existing discovery path (app.scenes.ensure_scenes_for_labels),
which is exactly the mechanism already designed to register a genuinely new
scene when nothing existing actually matches. asset_manifest's own shape is
unchanged.

1.5.0 (three coordinator amendments, same batch, real round-16/17
regressions -- schema changed, hence the minor bump):
  a) Prior-knowledge declare-then-verify (user correction: outright banning
     the model's own book knowledge in the extraction prompt was wrong --
     a correct guess like "丹鬼" should be a bonus, not discarded).
     _ModelCharacterMention/_ModelSceneMention gain ``suspected_true_name``
     (required, nullable); display_name still must be the verbatim
     in-episode term, never replaced. See
     _prep_pack_verify_true_name_hypothesis: a hypothesis is only trusted
     once it resolves to an existing bible identity AND the guessed name
     itself is textually corroborated (this episode's text or the same
     forward-looking window app.portraits already uses for real-name
     disambiguation) -- never taken on the model's word alone.
  b) Speaker roster referencing (real EP2 finding: a key line's speaker was
     written as "韩宗", a character absent until chapter 5, with zero
     validation on that field ever). event_chain[].key_lines[] gains
     ``speaker_ref``, resolved deterministically against the ALREADY-gated
     episode roster (asset_manifest.characters/functional_extras) by
     _prep_pack_resolve_key_line_speakers -- a speaker that resolves to
     nothing in this episode's own roster is a named, hard gate failure.
     Also added: prose-field lint (_prep_pack_prose_lint_warnings,
     summary/hook/cliffhanger) -- observability only, not fatal.
  c) Span-overlap semantic separation (ERR-20260824-22cb1c, real round-17
     EP3 regression) -- see app.validators.build_prep_pack_span_ledger's
     "语义分离" docstring note for the full argument. event_chain[].
     source_span keeps publishing the EXTENDED value (unchanged), but
     adjacent events' source_span may now legitimately overlap by a
     segment or two when a later event's own verified quote reaches one
     segment into a shared transition -- that is delivery-evidence
     spillover, not a narrative-boundary claim (the ordering/crossing gate
     itself only ever looks at the model's DECLARED span, never the
     extended one, as of this version). P1 storyboard consumers of this
     payload must not treat source_span overlap between consecutive events
     as "double-booked" story time.

Coverage accounting design (three real EP1 iterations, see
docs/TRANSFORM_FREEZE_PLAN.md and app.validators.build_prep_pack_span_ledger):
the model declares each event's ``source_span`` (a closed [from_segment,
to_segment] interval) instead of enumerating a disposition for every
individual segment. The coverage_ledger is then a deterministic PROJECTION
of the validated spans -- delivered/retained_as_context/uncovered are
derived, not model-declared; merged/proven_duplicates are always empty under
this accounting; paratext (1.4.0/1.4.1) is the one account seeded by a model
declaration rather than derived purely from spans, but even that declaration
only lands in the ledger after surviving deterministic gates (see the 1.4.1
note above) -- nothing in this ledger is ever a bare, unverified model claim.
This replaced an earlier per-segment
disposition-declaration design (2026-08-24) that made the model's bookkeeping
burden scale with segment count and left it randomly dropping ~1 short
segment (2-6 chars, e.g. a single interjection) per real run despite three
rounds of gate-shape patching -- see the git history on this file for that
design if it is ever needed for reference, but do not resurrect it: span
declaration is structurally simpler for the model (closer to "summarize this
range" than "fill out a per-item form") and fixes the failure class instead
of chasing individual instances of it.
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.observability.tracing import bind_trace, current_trace
from app.orchestration.state_machine import transition_step
from app.production.certificate import (
    assert_publish_has_certificate,
    consume_completion_certificate,
    issue_completion_certificate,
    verify_completion_certificate,
)
from app.schemas import Bible
from app.source_excerpt import SourceSegment, align_source_excerpt, index_source_segments
from app.textmatch import bigram_coverage
from app.validators import (
    assert_prep_pack_coverage_complete,
    assert_prep_pack_span_union_matches_ledger,
    build_prep_pack_span_ledger,
)

PREP_PACK_VERSION = "1.5.0"  # 1.1.0: event_chain entries carry source_span (P1 storyboard needs it).
# 1.2.0: asset_manifest.characters entries carry aliases; 1.3.0: asset_manifest
# gained functional_extras; 1.4.0: coverage_ledger gained paratext (deterministic
# keyword/position classifier, since replaced); 1.4.1: paratext classification
# mechanism replaced with model-declares + deterministic-veto-gates (real
# round-15 EP2 regression -- see module docstring's 1.4.1 note and
# app.validators.build_prep_pack_span_ledger's module comment above
# PARATEXT_TAIL_WINDOW_SEGMENTS). coverage_ledger.paratext's own shape is
# unchanged (still a flat [int] list) -- only how it gets populated changed,
# but this still counts as a classification-mechanism change for provenance
# purposes, hence the version bump. 1.4.2: asset resolution (_resolve_assets)
# gained a text-evidence gate for direct character/scene name binds (real
# round-16 EP5 regression -- see module docstring's 1.4.2 note and
# _prep_pack_mention_has_text_evidence's comment). asset_manifest's own shape
# is unchanged; this is a resolution-correctness fix, not a payload-shape
# change, but bumped for the same provenance reason as 1.4.1. 1.5.0 (real
# schema change, see module docstring's 1.5.0 note): _ModelCharacterMention/
# _ModelSceneMention gain ``suspected_true_name`` (model-declared prior-
# knowledge hypothesis, verified not trusted); event_chain[].key_lines[]
# gains ``speaker_ref`` (deterministic roster resolution of the free-text
# speaker field, real EP2 finding: a key line's speaker was written as
# "韩宗" -- a character absent until chapter 5 -- with zero validation).
QA_PROFILE_VERSION = "prep-pack-qa-gate-1"
_QA_EVALUATOR_NAME = "screenplay_production_qa"
_CHUNK_MAX_CHARS = 6000
_HOOK_GROUNDING_COVERAGE = 0.06
# Segment-scoped verbatim check, NOT align_source_excerpt's generic 8-char
# default. Real EP1 output proved the 8-char floor silently rejects correct
# short exact quotes (e.g. "靠山宗。", 4 content chars) -- the search here is
# already scoped to one small segment, so a short but exact match is
# meaningful evidence, not a coincidental generic overlap.
QUOTE_MIN_MATCH_CHARS = 2

# Mirrors app.domain.common._placeholder_bible's literal (that module is not
# importable here -- it is exec()'d into app.api's namespace, not a normal
# module, see app/domain/__init__.py). Only reached when a project's bible_json
# is still empty, which real prep_pack runs never hit by EP2 (EP1's screenplay
# already required a bible); kept only so discovery degrades instead of
# crashing on that edge case.
_FALLBACK_VISUAL_STYLE = "国漫风格，非真人CG渲染，统一电影感光影，暖灰色调"

# app.portraits identity-resolution kinds that resolve a mention without a
# character card/portrait: "functional_identity" is a typed one-off (确定性
# 群演，见 docs 任务描述), "reference_identity" is a stable authority that is
# only referenced, never on-screen this episode. Both mean "resolved, no asset
# required" for asset-mapping purposes -- not a gate failure.
_FUNCTIONAL_RESOLUTION_KINDS = {"functional_identity", "reference_identity"}


class PrepPackGateError(ValueError):
    """One generation attempt failed a deterministic hard gate; retryable."""


# ---------------------------------------------------------------------------
# Model response schemas
# ---------------------------------------------------------------------------

class _ModelSourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_index: int
    quote: str


class _ModelKeyLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker: str
    line: str
    segment_index: int


class _ModelCharacterMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    is_background_extra: bool
    # 1.5.0: model-declared prior-knowledge hypothesis (real EP5 finding:
    # outright banning this discarded a genuinely CORRECT guess -- see
    # _prep_pack_verify_true_name_hypothesis below). display_name must still
    # be the verbatim in-episode term of address; this field is never used
    # to replace it, only as an unverified candidate for _pass to check.
    suspected_true_name: str | None


class _ModelSceneMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    suspected_true_name: str | None  # 1.5.0, isomorphic to the character field above


class _ModelEventSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_segment: int
    to_segment: int


class _ModelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    summary: str
    source_span: _ModelEventSpan
    source_evidence: list[_ModelSourceEvidence]
    key_lines: list[_ModelKeyLine]
    characters: list[_ModelCharacterMention]
    scenes: list[_ModelSceneMention]


class _ChunkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[_ModelEvent]
    # 1.4.1: the model's own paratext claim for this chunk (chapter title /
    # author's note segments it deliberately did not turn into events) --
    # untrusted like every other model claim in this module; see
    # app.validators.build_prep_pack_span_ledger's three deterministic gates,
    # which decide what actually lands in coverage_ledger.paratext. Required
    # (not defaulted), matching every other field's strict-schema convention
    # in this module -- an empty list is a legal, explicit "none in this
    # chunk", not an omission.
    paratext_segments: list[int]


class _HookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hook: str
    hook_event_id: str
    cliffhanger: str
    cliffhanger_event_id: str


def _response_format(model_type: type[BaseModel], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": model_type.model_json_schema(),
        },
    }


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

def _chunk_segments(
    segments: list[SourceSegment], *, max_chars: int = _CHUNK_MAX_CHARS,
) -> list[list[tuple[int, SourceSegment]]]:
    """Group indexed segments into model-call-sized chunks (长章节切块)."""
    indexed = list(enumerate(segments, start=1))
    if not indexed:
        return []
    chunks: list[list[tuple[int, SourceSegment]]] = []
    current: list[tuple[int, SourceSegment]] = []
    current_chars = 0
    for item in indexed:
        _, segment = item
        segment_chars = len(segment.text)
        if current and current_chars + segment_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += segment_chars
    if current:
        chunks.append(current)
    return chunks


def _render_chunk(chunk: list[tuple[int, SourceSegment]]) -> str:
    return "\n\n".join(f"【{index}】\n{segment.text}" for index, segment in chunk)


def _known_character_names(conn, project_id: str, episode_no: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT character_name FROM character_portraits "
        "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY character_name",
        (project_id, episode_no, episode_no),
    ).fetchall()
    return [str(row["character_name"]) for row in rows]


def _known_scene_names(conn, project_id: str, episode_no: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT scene_name FROM scene_references "
        "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY scene_name",
        (project_id, episode_no, episode_no),
    ).fetchall()
    return [str(row["scene_name"]) for row in rows]


def _resolve_portrait_id(conn, project_id: str, character_name: str, episode_no: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY ep_start DESC LIMIT 1",
        (project_id, character_name, episode_no, episode_no),
    ).fetchone()
    return str(row["id"]) if row else None


def _resolve_scene_reference_id(conn, project_id: str, scene_name: str, episode_no: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM scene_references WHERE project_id=? AND scene_name=? "
        "AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY ep_start DESC LIMIT 1",
        (project_id, scene_name, episode_no, episode_no),
    ).fetchone()
    return str(row["id"]) if row else None


# 称谓/场景名证据闸（1.4.2，real round-16 EP5 regression fix）. Real EP5 output
# resolved a completely unrelated pair of mountain-top old men -- the raw
# text only ever calls them "两个老者"/穿灰袍的高大老者", never a proper
# name -- to a pre-existing character ("丹鬼") and scene ("大青山山顶") from
# elsewhere in the story, purely because the event-chain extraction model
# happened to write those exact already-registered names (both are 0
# occurrences in chapter 5's own text; verified directly against the real
# chapters row). Root cause: neither _resolve_portrait_id nor
# _resolve_scene_reference_id require any evidence beyond "a DB row with
# this exact name exists somewhere, for any episode" -- a bare name-string
# coincidence was silently trusted as a real identification. Traced two
# independent binds:
#   - character "丹鬼": the chunk-extraction model wrote "丹鬼" directly as
#     characters[].display_name (NOT "灰袍老者"/"山顶老者" -- those only ever
#     appeared as key_lines[].speaker, a field this module never resolves
#     through) -- a bare direct hit, not a legitimate forward-looking
#     identity resolution (which is why aliases ended up empty: no rename
#     ever happened, so the existing "aliases.append(name) when name !=
#     resolved_name" logic never had anything to record).
#   - scene "大青山山顶": same shape -- scenes[].display_name was written as
#     "大青山山顶" directly, an existing scene_reference from unrelated
#     earlier context, despite the text explicitly saying "靠山宗四周的山峰"
#     / "外宗旁的山峰".
def _prep_pack_mention_has_text_evidence(name: str, source_text: str) -> bool:
    """Does ``name`` -- the raw mention/称谓 text an event actually carries,
    exactly as the event-chain extraction model wrote it -- appear verbatim
    anywhere in this episode's own ``source_text``? A plain substring check
    is deliberately sufficient here (unlike align_source_excerpt's fuzzy
    quote-matching, which exists for full-sentence quotes): character/scene
    names are short proper nouns, not sentences, so an exact substring
    either is or isn't real textual grounding for "this term was actually
    used to refer to something in this chapter."
    """
    return bool(name) and name in (source_text or "")


# 先验知识申报通道（1.5.0，用户修正令：outright 禁止会扔掉真正猜对的真名，
# "丹鬼"这类猜对了本该是加分项）。模型可能在训练语料里读过这部小说，与其
# 假装它不知道（禁止），不如让它把这份先验知识当一个可核验的候选申报出来
# （_ModelCharacterMention/_ModelSceneMention.suspected_true_name），申报本身
# 从不被直接采信——必须先通过下面这条确定性核验，核验不过就丢弃，回退到
# display_name 本身的常规解析路线（消歧/群演/发现），不静默相信任何猜测。
def _prep_pack_verify_true_name_hypothesis(
    conn, *, project_id: str, episode_no: int, source_text: str,
    suspected_true_name: str, resolve_fn,
) -> bool:
    """Verify (not trust) a model-declared ``suspected_true_name`` guess.

    ``resolve_fn`` is ``_resolve_portrait_id`` or ``_resolve_scene_reference_id``
    (character/scene share the same two-part verification shape). A
    hypothesis is verified when BOTH hold:
      a) ``suspected_true_name`` resolves to an EXISTING bible identity --
         nothing to bind the guess to otherwise, no matter how well-attested
         the guess is;
      b) ``suspected_true_name`` itself appears verbatim somewhere in this
         episode's own text OR the same forward-looking window
         app.portraits' real identity-disambiguation already uses for
         exactly this kind of question ("大汉/老者/黑衣人后来叫什么") --
         reusing app.portraits._future_chapter_context (same
         IDENTITY_DISCOVERY_FORWARD_CHAPTERS window/fetcher) rather than a
         second copy of the chapter-fetching logic.
    This performs no model call of its own -- the hypothesis is either
    textually corroborated within a bounded, already-established window, or
    it is not; that is what makes it a genuine fast lane (as fast as a
    handful of DB reads + substring checks) rather than a second guess.
    """
    if not suspected_true_name:
        return False
    if resolve_fn(conn, project_id, suspected_true_name, episode_no) is None:
        return False
    if _prep_pack_mention_has_text_evidence(suspected_true_name, source_text):
        return True
    from app.portraits import _future_chapter_context

    forward_text, _label = _future_chapter_context(conn, project_id, episode_no)
    return _prep_pack_mention_has_text_evidence(suspected_true_name, forward_text)


def _load_project_bible(conn, project_id: str) -> Bible:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    raw = (row["bible_json"] or "").strip() if row else ""
    if raw:
        return Bible.model_validate(json.loads(raw))
    return Bible.model_validate({
        "characters": [], "scenes": [],
        "world": {"era": "", "genre": "", "visual_style_canonical": _FALLBACK_VISUAL_STYLE},
    })


def _character_discovery_dispositions(
    discovery_result: dict[str, Any],
) -> tuple[set[str], dict[str, str], set[str]]:
    """Turn app.portraits.ensure_cards_for_text's result into lookup aids for
    the second resolution pass:
    - skip_names: mentions the discovery mechanism itself (not this file)
      determined need no character card/portrait -- typed functional identity,
      stable reference-only identity, or a ``skipped`` disposition. Recorded
      as a functional extra (unless also in non_person_names), not silently
      dropped -- see _resolve_assets.
    - rename_map: mentions whose confirmed real name differs from the event
      chain's raw mention text (e.g. a title resolved to the true name),
      re-keyed by that real name instead.
    - non_person_names: the subset of skip_names discovery explicitly judged
      is not a person at all (``skipped_not_person`` -- a sect/artifact/pen
      name the chunk extractor mistakenly listed as a character). These are
      still legally skip-able (no portrait required) but must NOT show up in
      functional_extras, which is a list of *people* in frame for P1
      storyboard prompts, not a dumping ground for every non-card mention.
    These only match by exact string equality against discovery's own
    source_label/name, which is a *different* model call's phrasing of the
    same source text and will not always coincide with prep_pack's chunk-
    extraction phrasing (real EP13 case: discovery resolved "外宗弟子" while
    the published chunk extraction said "一名外宗弟子" -- same real-world
    concept, different string). A name this misses is not necessarily
    unclassified; see _resolve_assets' functional-extra default and
    _discovery_errored_names for what actually still blocks.
    """
    skip_names: set[str] = set()
    non_person_names: set[str] = set()
    for item in discovery_result.get("skipped") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        skip_names.add(name)
        if str(item.get("status") or "").strip() == "skipped_not_person":
            non_person_names.add(name)
    rename_map: dict[str, str] = {}
    for item in discovery_result.get("resolutions") or []:
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        resolution = str(item.get("resolution") or "").strip()
        if not source_label:
            continue
        if resolution in _FUNCTIONAL_RESOLUTION_KINDS:
            skip_names.add(source_label)
        elif canonical_name and canonical_name != source_label:
            rename_map[source_label] = canonical_name
    return skip_names, rename_map, non_person_names


def _discovery_errored_names(
    discovery_result: dict[str, Any], candidate_names: list[str],
) -> set[str]:
    """Which of *our* raw mention strings discovery explicitly failed on.

    ensure_cards_for_text's own error strings are name-prefixed
    ("{name}：原因", app/portraits.py:7383/7407) but not schema-guaranteed, so
    this checks containment against each of our own candidate names rather
    than trying to parse discovery's message format -- a name only lands here
    if discovery said something concrete *about that name*, e.g. "身份模型已
    确认真名，但人物卡模型未返回完整稳定卡片" (a confirmed real identity
    whose card generation itself failed -- a real defect, must block) or an
    exception during its own processing. This is deliberately the one thing
    _resolve_assets still hard-blocks on after discovery runs; everything
    else defaults to a functional extra (see its docstring).
    """
    messages = [str(message) for message in discovery_result.get("errors") or []]
    if not messages:
        return set()
    return {
        name for name in candidate_names
        if name and any(name in message for message in messages)
    }


async def _discover_new_characters(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    source_text: str, run_id: str | None,
) -> dict[str, Any]:
    """谱外新角色 → 发现 → 补录人物谱 → 生成定妆照。

    Reuses app.portraits' identity-discovery machinery as-is (does not
    reimplement it): importance = source chapters + CHARACTER_IMPORTANCE_
    FORWARD_CHAPTERS, true-name resolution = its own independent
    IDENTITY_DISCOVERY_FORWARD_CHAPTERS window (portraits.py:384-385), and the
    spoiler rule that forward context may only resolve an already-appeared
    identity's stable name, never pull future plot into this episode
    (ensure_cards_for_text -> discover_character_candidates docstrings). Only
    called when pass 1 of ``_resolve_assets`` below leaves a real,
    non-background-extra character mention unresolved -- see the zero-call
    regression assertion in tests/test_prep_pack_asset_discovery.py.
    """
    from app.portraits import (
        ensure_cards_for_text,
        persist_screenplay_character_resolutions,
        screenplay_identity_scope_fingerprint,
    )
    from app.source_paratext import strip_paratext

    bible = _load_project_bible(conn, project_id)
    # Same purification prep_pack's dead-code predecessor
    # (app.domain.screenplay_ops._screenplay_character_discovery) applied
    # before discovery: stage 0 runs before any paratext judgment exists, so
    # without this an author's pen name in chapter-end commentary gets
    # mistaken for a character. Only the discovery-facing copy is stripped;
    # source_text itself (used for event-chain evidence) is untouched.
    discovery_text = await strip_paratext(
        source_text,
        operation_id=f"episode_prep_pack.character_discovery.paratext:{episode_id}",
    )
    result = await ensure_cards_for_text(
        project_id, episode_no, discovery_text, bible, generate_portraits=True,
    )
    persist_screenplay_character_resolutions(
        conn, episode_id, result.get("resolutions") or [],
        retire_legacy_future_identity=True,
        expected_active_run_id=run_id,
        replace_identity_scope=screenplay_identity_scope_fingerprint(episode_no, source_text),
    )
    return result


async def _discover_new_scenes(
    conn, *, project_id: str, episode_no: int, labels: list[str],
) -> dict[str, Any]:
    """谱外新场景 → 发现 → 补录场景库 → 生成场景参考图。

    Reuses app.scenes' reactive scene-discovery machinery as-is via
    ``ensure_scenes_for_labels`` (a thin adapter added alongside
    ``ensure_scenes_for_storyboard`` for callers, like this one, that have a
    flat label list instead of a compiled screenplay object -- same
    assess_new_scene/_generate_and_register_scene functions underneath, no
    discovery logic duplicated). Only called when pass 1 below leaves a scene
    mention unresolved.
    """
    from app.scenes import ensure_scenes_for_labels

    return await ensure_scenes_for_labels(project_id, episode_no, labels)


async def _resolve_assets(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    source_text: str, events: list[dict[str, Any]], run_id: str | None,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, int],
    list[dict[str, Any]],
]:
    """Resolve every event's characters/scenes (invariant③).

    Every character/scene mention goes through the same resolution attempt,
    regardless of the event-chain extraction model's own ``is_background_extra``
    guess on that mention. That flag is advisory prose from a *different*,
    earlier model call that never looked at the bible -- treating it as an
    exemption from resolution is exactly the bug a real EP2 run surfaced:
    "小胖子" (7 on-screen appearances, real dialogue) was tagged
    is_background_extra=true by the chunk extractor and, under the previous
    build of this function, skipped before ever reaching resolution -- even
    though "小胖子" is 李富贵, already in the bible with a portrait. The
    correct flow (per invariant③: resolve to an existing asset OR a
    deterministic generic-extra class, never a third "silently absent"
    option) is: direct name match against character_portraits/scene_references
    first (cheap, no model call) -> unresolved mentions (whatever the
    extractor guessed about them) go through the discovery/disambiguation
    mechanism inherited from the heavy pipeline (_discover_new_characters /
    _discover_new_scenes below) -> only discovery explicitly failing *on that
    specific name* (_discovery_errored_names) may still hard-block it. Pass 1
    is direct-match only; pass 2 re-resolves after discovery using whatever it
    newly registered (new cards+portraits, a known-alias -> canonical-name
    rename e.g. "小胖子" -> "李富贵", or a functional/no-asset disposition).

    A resolved character's manifest entry carries an ``aliases`` list: the
    distinct raw mention strings (e.g. ["小胖子"]) that resolved to it via a
    rename, for P1 storyboard prompts to use.

    Second real-run finding (EP13, coordinator-reviewed): app.portraits'
    identity discovery does its own independent read of the source text and
    phrases/scopes its own candidates differently from prep_pack's chunk
    extraction -- discovery resolved "外宗弟子" as functional_identity while
    the published chunk extraction said "一名外宗弟子" for what is plainly the
    same one-off crowd concept; several other occupation-title mentions
    ("养丹坊掌柜", "宝阁执事", "围观弟子") got no matching disposition at all
    by exact string, even though discovery ran cleanly (its own ``errors``
    was empty) and *did* resolve the real new character "曹阳" (a portrait was
    generated) in the very same call. Per app.portraits' own long-standing
    rule (portraits.py:1727,7340 -- an unconfirmed-real-name one-off keeps its
    own source label and gets a typed functional identity, never silently
    dropped nor renamed to something generic), a mention that discovery
    neither resolved nor explicitly failed on defaults to a functional extra
    under its own raw text -- not a card, not a portrait, not a gate error.
    The only thing that still hard-blocks after discovery runs is
    _discovery_errored_names: discovery said something concrete about that
    *specific* name (a confirmed real identity whose card generation itself
    failed, or an exception) -- "消歧和发现都没能给出任何归类结论" is the one
    state this function will not paper over.

    Episodes where pass 1 already resolves every mention by exact name never
    call discovery at all (``stats``' counters stay at 0) -- but note this is
    now a narrower case than "no new characters": any mention that is not an
    exact known name (a genuine one-off extra with no real name, not just a
    new named character) also routes through discovery so it can receive a
    real disposition instead of being assumed one way or the other.
    """
    stats = {"character_discovery_calls": 0, "scene_discovery_calls": 0}

    def _pass(
        skip_character_names: set[str],
        character_rename: dict[str, str],
        scene_rename: dict[str, str],
        non_person_names: set[str] = frozenset(),
    ) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, list[str]], list[str], list[str], list[str],
        list[dict[str, Any]],
    ]:
        characters: dict[str, dict[str, Any]] = {}
        scenes: dict[str, dict[str, Any]] = {}
        functional_extras: dict[str, list[str]] = {}
        errors: list[str] = []
        unresolved_characters: list[str] = []
        unresolved_scenes: list[str] = []
        # 1.5.0 观测记录：每条模型申报的 suspected_true_name 假设最终是被核验
        # 采信还是拒绝，都记一条（不影响门禁本身，见函数上方注释）。
        true_name_hints: list[dict[str, Any]] = []
        for event in events:
            event_id = event["event_id"]
            for mention in event["characters"]:
                name = str(mention["display_name"] or "").strip()
                if not name:
                    errors.append(f"事件 {event_id} 存在空白角色名")
                    continue
                if name in skip_character_names:
                    if name not in non_person_names:
                        extra_events = functional_extras.setdefault(name, [])
                        if event_id not in extra_events:
                            extra_events.append(event_id)
                    continue
                resolved_name = character_rename.get(name, name)
                suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
                if suspected_true_name and suspected_true_name != resolved_name:
                    if _prep_pack_verify_true_name_hypothesis(
                        conn, project_id=project_id, episode_no=episode_no,
                        source_text=source_text, suspected_true_name=suspected_true_name,
                        resolve_fn=_resolve_portrait_id,
                    ):
                        resolved_name = suspected_true_name
                        true_name_hints.append({
                            "kind": "character", "mention": name,
                            "suspected_true_name": suspected_true_name, "status": "accepted",
                        })
                    else:
                        true_name_hints.append({
                            "kind": "character", "mention": name,
                            "suspected_true_name": suspected_true_name, "status": "rejected",
                        })
                portrait_id = _resolve_portrait_id(conn, project_id, resolved_name, episode_no)
                if not portrait_id:
                    errors.append(
                        f"事件 {event_id} 的角色「{name}」未解析到已有 portrait_id，"
                        "身份消歧也未能将其归类为已有角色或确定性群演"
                    )
                    if name not in unresolved_characters:
                        unresolved_characters.append(name)
                    continue
                # 称谓证据闸（1.4.2，见 _prep_pack_mention_has_text_evidence 上方
                # 注释）：不管这个 portrait_id 是靠直接命中还是靠身份消歧的前瞻
                # 改名解析拿到的，模型实际写下的称谓 name 本身必须逐字出现在本集
                # 原文——这既拦得住真实 EP5 的裸直接命中幻觉（模型直接写"丹鬼"，
                # 原文 0 次出现），也不误伤合法的前瞻解析（比如"小胖子"→李富贵，
                # "小胖子"本就是原文里对他的真实称呼，天然会出现在原文中）。命中
                # 失败不静默改路由到发现——这是协调方明确要求的"门禁具名拦截"，
                # 不是"当作未解析回炉"（回炉重新发现有可能重犯同样的臆断错误）。
                if not _prep_pack_mention_has_text_evidence(name, source_text):
                    errors.append(
                        f"事件 {event_id} 的角色「{name}」解析到已有角色「{resolved_name}」"
                        f"（portrait_id={portrait_id}），但称谓「{name}」未逐字出现在本集"
                        "原文中，缺少称谓证据，门禁具名拦截"
                    )
                    continue
                entry = characters.setdefault(portrait_id, {
                    "identity_id": f"bible:{resolved_name}",
                    "display_name": resolved_name,
                    "portrait_id": portrait_id,
                    "event_ids": [],
                    "aliases": [],
                })
                if event_id not in entry["event_ids"]:
                    entry["event_ids"].append(event_id)
                if name != resolved_name and name not in entry["aliases"]:
                    entry["aliases"].append(name)
            for mention in event["scenes"]:
                name = str(mention["display_name"] or "").strip()
                if not name:
                    errors.append(f"事件 {event_id} 存在空白场景名")
                    continue
                resolved_via_discovery = name in scene_rename
                resolved_name = scene_rename.get(name, name)
                suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
                if (
                    suspected_true_name
                    and suspected_true_name != resolved_name
                    and not resolved_via_discovery
                ):
                    if _prep_pack_verify_true_name_hypothesis(
                        conn, project_id=project_id, episode_no=episode_no,
                        source_text=source_text, suspected_true_name=suspected_true_name,
                        resolve_fn=_resolve_scene_reference_id,
                    ):
                        resolved_name = suspected_true_name
                        true_name_hints.append({
                            "kind": "scene", "mention": name,
                            "suspected_true_name": suspected_true_name, "status": "accepted",
                        })
                    else:
                        true_name_hints.append({
                            "kind": "scene", "mention": name,
                            "suspected_true_name": suspected_true_name, "status": "rejected",
                        })
                scene_reference_id = _resolve_scene_reference_id(
                    conn, project_id, resolved_name, episode_no,
                )
                # 场景证据闸（1.4.2，见 _prep_pack_mention_has_text_evidence 上方
                # 注释）：只在"裸直接命中"（这个 label 从未被场景发现处理过，即
                # 不在 scene_rename 里——用"是否是该字典的 key"判定，不能用
                # resolved_name == name 的字符串比较：发现判定新场景的规范名恰好
                # 与原始 label 相同也是完全合法的结果，比如新建场景直接沿用了
                # 提及原文，字符串相等不代表没被发现处理过）时核验——一旦这个
                # label 真的经过发现，就信任发现自己更细致的判定，不再重复核验
                # （新建场景的规范名通常是 AI 综合描述出的标签，本就不会逐字出现
                # 在原文里，用同一条子串检查会误伤合法的新建）。没证据 → 当作未
                # 解析，走场景发现（本例应新建"靠山宗外围山峰"），不是直接拒绝
                # ——场景侧允许回炉重新判定，跟角色侧"具名拦截"不对称是刻意的：
                # 新建场景的代价低，发现机制本身就是给"裸命中没证据"设计的下一步。
                if (
                    scene_reference_id
                    and not resolved_via_discovery
                    and not _prep_pack_mention_has_text_evidence(name, source_text)
                ):
                    scene_reference_id = None
                if not scene_reference_id:
                    errors.append(
                        f"事件 {event_id} 的场景「{name}」未解析到已有 scene_reference_id"
                    )
                    if name not in unresolved_scenes:
                        unresolved_scenes.append(name)
                    continue
                entry = scenes.setdefault(scene_reference_id, {
                    "scene_id": f"scene:{resolved_name}",
                    "display_name": resolved_name,
                    "scene_reference_id": scene_reference_id,
                    "event_ids": [],
                })
                if event_id not in entry["event_ids"]:
                    entry["event_ids"].append(event_id)
        return (
            characters, scenes, functional_extras, errors,
            unresolved_characters, unresolved_scenes, true_name_hints,
        )

    characters, scenes, functional_extras, errors, unresolved_chars, unresolved_scenes, true_name_hints = (
        _pass(set(), {}, {})
    )
    # 1.5.0：假设核验发生在每一遍 _pass() 内部；一个假设被拒绝的提及若同时触发
    # 发现（走到下面这个分支），第二遍 _pass() 的返回值会整体替换第一遍的——
    # 但第一遍已经记下的 true_name_hints 不该因此凭空消失（红灯 4b 明确要求
    # "rejected 计数=1" 且"走新场景发现"同时成立）。第一遍的记录单独保留，
    # 最后跟第二遍的合并去重。
    true_name_hints_pass1 = true_name_hints

    if unresolved_chars or unresolved_scenes:
        skip_character_names: set[str] = set()
        character_rename: dict[str, str] = {}
        scene_rename: dict[str, str] = {}
        non_person_names: set[str] = set()
        discovery_diagnostics: list[str] = []

        if unresolved_chars:
            stats["character_discovery_calls"] += 1
            discovery_result = await _run_async_step(
                run_id, "episode_prep_pack_character_discovery",
                lambda: _discover_new_characters(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text, run_id=run_id,
                ),
            )
            skip_character_names, character_rename, non_person_names = (
                _character_discovery_dispositions(discovery_result)
            )
            errored_names = _discovery_errored_names(discovery_result, unresolved_chars)
            # Coordinator-mandated default: anything discovery neither
            # resolved (rename) nor explicitly disposed of (skip) nor
            # explicitly failed on (errored_names) is a typed functional
            # identity under its own source label, not a block -- but only
            # once directly-resolvable names are ruled out first (discovery
            # may have just committed a portrait under this exact raw name,
            # e.g. a genuinely new character discovery carded this call; that
            # must resolve normally, not get swept into the fallback).
            for name in unresolved_chars:
                if name in skip_character_names or name in character_rename or name in errored_names:
                    continue
                if _resolve_portrait_id(conn, project_id, name, episode_no):
                    continue
                skip_character_names.add(name)
            discovery_diagnostics.extend(str(e) for e in discovery_result.get("errors") or [])

        if unresolved_scenes:
            stats["scene_discovery_calls"] += 1
            scene_discovery_result = await _run_async_step(
                run_id, "episode_prep_pack_scene_discovery",
                lambda: _discover_new_scenes(
                    conn, project_id=project_id, episode_no=episode_no,
                    labels=unresolved_scenes,
                ),
            )
            scene_rename = dict(scene_discovery_result.get("resolved_names") or {})
            discovery_diagnostics.extend(str(e) for e in scene_discovery_result.get("errors") or [])

        characters, scenes, functional_extras, errors, unresolved_chars, unresolved_scenes, true_name_hints_pass2 = (
            _pass(skip_character_names, character_rename, scene_rename, non_person_names)
        )
        # 合并两遍，按内容去重（同一个提及在两遍里都核验出相同结论是正常的、
        # 无害的重复计算，不该在观测数据里出现两条一模一样的记录）。
        combined = true_name_hints_pass1 + true_name_hints_pass2
        seen: set[tuple[str, str, str, str]] = set()
        true_name_hints = []
        for hint in combined:
            key = (hint["kind"], hint["mention"], hint["suspected_true_name"], hint["status"])
            if key not in seen:
                seen.add(key)
                true_name_hints.append(hint)
        if errors and discovery_diagnostics:
            errors = list(errors) + [
                f"发现阶段诊断：{message}" for message in discovery_diagnostics[:5]
            ]

    functional_extras_payload = [
        {"label": label, "event_ids": event_ids}
        for label, event_ids in functional_extras.items()
    ]
    return (
        list(characters.values()), list(scenes.values()), functional_extras_payload,
        errors, stats, true_name_hints,
    )


# ---------------------------------------------------------------------------
# Speaker roster resolution (1.5.0, real EP2 finding): key_lines[].speaker was
# free text with zero validation -- a key line's speaker was written as "韩宗"
# (a character absent until chapter 5) for what was actually 绿袍男子. "本集
# 有谁" already has a single, gated source of truth by the time this runs: the
# resolved asset roster (characters + functional_extras from _resolve_assets,
# themselves already gated by the 1.4.2 evidence gate + 1.5.0 true-name
# verification above). Speaker resolution is therefore a pure, deterministic
# LOOKUP against that roster -- no new discovery, no new model call, no
# independent hypothesis mechanism of its own.
# ---------------------------------------------------------------------------

def _prep_pack_build_speaker_roster(
    characters: list[dict[str, Any]], functional_extras: list[dict[str, Any]],
) -> dict[str, str]:
    """Every string a speaker could legitimately be written as this episode,
    mapped to a ``speaker_ref``: a bound character's own ``display_name`` or
    any of its recorded ``aliases`` -> ``"bible:<display_name>"`` (mirrors
    asset_manifest.characters[].identity_id); a functional extra's own
    ``label`` -> ``"extra:<label>"``. Episode-wide, not per-event-scoped --
    anyone on screen anywhere this episode is a legal speaker anywhere else
    in the same episode (deliberate scope simplification, not a per-event
    presence check)."""
    roster: dict[str, str] = {}
    for character in characters:
        display_name = str(character.get("display_name") or "")
        ref = str(character.get("identity_id") or f"bible:{display_name}")
        if display_name:
            roster[display_name] = ref
        for alias in character.get("aliases") or []:
            if alias:
                roster[str(alias)] = ref
    for extra in functional_extras:
        label = str(extra.get("label") or "")
        if label:
            roster[label] = f"extra:{label}"
    return roster


def _prep_pack_resolve_key_line_speakers(
    payload_events: list[dict[str, Any]], roster: dict[str, str],
) -> list[str]:
    """Hard gate: every key_line's ``speaker`` must resolve to a roster
    entry, or it is named-and-blocked (the real EP2 "韩宗" bug: a speaker
    string that resolves to NOTHING in this episode's own roster must never
    reach the published artifact). Mutates each key_line dict in place,
    adding ``speaker_ref``; the original ``speaker`` text is left untouched
    for display. Returns the list of block messages (empty = all resolved)."""
    errors: list[str] = []
    for event in payload_events:
        event_id = event.get("event_id")
        for key_line in event.get("key_lines") or []:
            speaker = str(key_line.get("speaker") or "").strip()
            ref = roster.get(speaker) if speaker else None
            if ref is None:
                errors.append(
                    f"事件 {event_id} 的台词说话人「{speaker}」未能解析到本集资产名册"
                    "任何条目（角色/群演），门禁具名阻断"
                )
                continue
            key_line["speaker_ref"] = ref
    return errors


def _prep_pack_prose_lint_warnings(
    *, payload_events: list[dict[str, Any]], hook: str, cliffhanger: str,
    known_names: list[str], roster_names: set[str],
) -> list[dict[str, Any]]:
    """Observability-level lint (NOT fatal, 1.5.0): a bible-registered proper
    noun appearing in free prose (event summary / hook / cliffhanger) that is
    NOT part of this episode's own roster is flagged for human review, not
    blocked -- "mentioned but not on screen" (e.g. a absent mentor recalled
    in narration) is a legitimate real scenario, not a naming-hallucination
    bug by itself; only an actual asset BIND without evidence is (see the
    1.4.2/1.5.0 gates above, which stay hard). ``known_names`` is this
    project's registered character/scene names scoped to this episode's own
    ep_start/ep_end window (the same list already fetched for the extraction
    prompt) -- a scope approximation of "谱内专名", not the full unscoped
    bible; acceptable for an observability-only signal."""
    warnings: list[dict[str, Any]] = []

    def _scan(field: str, text: str, event_id: str | None) -> None:
        for name in known_names:
            if len(name) >= 2 and name in text and name not in roster_names:
                warnings.append({
                    "field": field, "name": name, "event_id": event_id,
                    "excerpt": text[:80],
                })

    for event in payload_events:
        _scan("summary", str(event.get("summary") or ""), event.get("event_id"))
    _scan("hook", hook, None)
    _scan("cliffhanger", cliffhanger, None)
    return warnings


def _begin_step(run_id: str | None, step_key: str, *, iteration_no: int = 1) -> str | None:
    if not run_id:
        return None
    step_id = evidence_repository.create_step(
        run_id, step_key,
        iteration_no=iteration_no,
        agent_name="episode_prep_pack",
        contract_version=get_contract("screenplay").version,
    )
    transition_step(step_id, "PENDING", "READY", "输入已就绪")
    transition_step(step_id, "READY", "RUNNING", "步骤开始")
    return step_id


def _finish_step(step_id: str | None, exc: BaseException | None) -> None:
    if not step_id:
        return
    if exc is not None:
        transition_step(
            step_id, "RUNNING", "FAILED", str(exc)[:1000],
            decision="escalate", error_code=type(exc).__name__.upper(),
        )
        return
    transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept")


def _run_sync_step(run_id: str | None, step_key: str, fn):
    """Wrap one deterministic (non-model-call) unit of work as an observable
    step, reusing the same create_step/transition_step machinery as the
    model-calling steps below -- so it shows up in the same observability
    trace with a registered business name (see
    app.orchestration.engine._STEP_PRESENTATIONS)."""
    step_id = _begin_step(run_id, step_key)
    try:
        result = fn()
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _run_async_step(run_id: str | None, step_key: str, fn):
    """Async twin of ``_run_sync_step`` for one observable awaited unit of
    work (e.g. an app.portraits/app.scenes discovery call) that is not itself
    a structured model call through ``_call_structured``."""
    step_id = _begin_step(run_id, step_key)
    try:
        result = await fn()
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _call_structured(
    *,
    run_id: str | None,
    step_key: str,
    prompt: str,
    model_type: type[BaseModel],
    schema_name: str,
    operation_id: str,
    max_tokens: int,
    call_meta: dict[str, Any],
    iteration_no: int = 1,
) -> Any:
    step_id = _begin_step(run_id, step_key, iteration_no=iteration_no)
    trace = current_trace()
    ctx = bind_trace(run_id, step_id, trace.trace_id) if run_id else nullcontext()
    try:
        with ctx:
            result = await model_gateway.chat_structured(
                [{"role": "user", "content": prompt}],
                model_type=model_type,
                validate=None,
                operation_id=operation_id,
                max_tokens=max_tokens,
                temperature=0.2,
                format_retry_limit=1,
                semantic_retry_limit=1,
                call_meta=call_meta,
                response_format=_response_format(model_type, schema_name),
                require_response_format=True,
            )
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _extract_chunk(
    *,
    episode_id: str,
    episode_no: int,
    chunk_index: int,
    chunk: list[tuple[int, SourceSegment]],
    known_characters: list[str],
    known_scenes: list[str],
    attempt_hint: str,
    run_id: str | None,
) -> _ChunkResponse:
    rendered = _render_chunk(chunk)
    hint = f"\n上一次尝试未通过校验，请修正：{attempt_hint}\n" if attempt_hint else ""
    prompt = f"""你在为一部网络小说改编的短剧准备第 {episode_no} 集的事件链（不改编台词、不生成分镜）。

任务：把下面按顺序编号的原文片段（编号即 segment_index，本段范围 {chunk[0][0]}~{chunk[-1][0]}）
划分成一串按时间顺序排列的事件。每个事件必须给出：
- event_id：形如 "ev_001" 的字符串，本集内不重复，按发生顺序编号；
- summary：一句话概述该事件；
- source_span：{{"from_segment": 起始编号, "to_segment": 结束编号}}，声明该事件覆盖的原文
  编号闭区间；
- source_evidence：至少一条 {{"segment_index": span 范围内的编号, "quote": "该编号原文中的
  逐字引文片段"}}，quote 必须逐字取自该编号原文（可摘录其中一部分），不得改写、概括或跨编号
  拼接，segment_index 必须落在本事件自己的 source_span 内；
- key_lines：如果该事件包含台词，逐条给出 {{"speaker": "说话人", "line": "台词原文逐字摘录",
  "segment_index": span 范围内的编号}}，line 同样必须逐字取自该编号原文；没有台词就给空列表；
- characters：该事件中出场的角色，每个给 {{"display_name": "角色名", "is_background_extra": 布尔,
  "suspected_true_name": "你认为的真名，不确定就填 null"}}；
  已登记角色名（仅供拼写对齐——如果原文本身就是这样称呼这个角色的，写法要跟登记名
  保持一致；原文没有这样称呼，就不要往上面靠）：{known_characters}；
  没有姓名、不影响剧情走向的纯背景群演（路人、杂役等）标 is_background_extra=true，
  display_name 写功能性描述（如"围观弟子"）即可，不要虚构成主要角色；
- scenes：该事件发生的场景，每个给 {{"display_name": "场景名", "suspected_true_name": "你认为的
  正名，不确定就填 null"}}；已登记场景名（仅供拼写对齐，同上一条的原则）：{known_scenes}。

命名纪律（关于 characters/scenes 的 display_name，硬性）：
- display_name 必须逐字使用本段原文中出现的称谓——原文写"灰袍老者"就填"灰袍老者"，
  禁止填任何本段原文没有出现过的名字，哪怕你认为自己知道这个人物/地点的"真名"；
  display_name 永远不能被下面这条替换；
- 先验知识申报通道：你有可能在训练语料里读过这部小说——如果知道某个称谓背后的真名
  或正式名称，把它填进对应 mention 的 suspected_true_name（不确定就填 null，不要瞎猜
  硬填）；这只是申报，你的猜测会被本集原文/后续章节的文本证据核验，核验不过就不会
  被采用，绝不会被静默相信；
- 场景地点的 display_name 一律使用原文自己的描述词，不得替换成你认为等价的其他
  地名（哪怕原文的地点和你知道的某个地名指的是同一个地方，也只能照抄原文怎么说，
  真名假设同样走 suspected_true_name）。

另外给出 paratext_segments：本段编号中，属于"非故事内容"的编号列表——章节标题、
作者对读者说的话（求收藏/求推荐/月票/上架/加更/催更等）、网站公告，这些不是故事
叙述本身（人物动作/对白/心理/场景描写都不算，哪怕它们提到类似字眼也不算），不需要
为它们创建事件。你自己就能判断哪些是——按内容本身判断，不用管它们在本段的位置。
没有就给空列表。

硬性要求（关于 source_span）：
- 除 paratext_segments 声明的编号外，所有事件的 span 首尾相接，必须完整覆盖本段
  其余全部编号 {chunk[0][0]}~{chunk[-1][0]}，不允许任何编号既不在某个事件的 span
  内、也不在 paratext_segments 里——那等于把那段原文删掉了；
- 相邻事件允许共享一个边界编号（例如事件 A 的 to_segment=20，事件 B 的 from_segment=20），
  但不允许区间交叉或倒退（后一个事件的 from_segment 不能小于前一个事件的 to_segment）；
- 不要为了省事把一大段编号塞进一个事件——跨度明显大于平均值的事件，请至少给两条分别落在
  该跨度前半和后半的 source_evidence，证明你确实看过整段内容而不是笼统打包。
{hint}
原文（本段共 {len(chunk)} 个编号片段）：
{rendered}
"""
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_event_chain_chunk",
        iteration_no=chunk_index,
        prompt=prompt,
        model_type=_ChunkResponse,
        schema_name="episode_prep_pack_chunk_v3",
        operation_id=f"episode_prep_pack:{episode_id}:chunk:{chunk_index}",
        max_tokens=8000,
        call_meta={
            "stage_key": "episode_prep_pack_event_chain",
            "episode_id": episode_id,
            "chunk_index": chunk_index,
        },
    )


async def _extract_hook_cliffhanger(
    *,
    episode_id: str,
    episode_no: int,
    events: list[dict[str, Any]],
    attempt_hint: str,
    run_id: str | None,
) -> _HookResponse:
    compact = [
        {"event_id": event["event_id"], "order": event["order"], "summary": event["summary"]}
        for event in events
    ]
    hint = f"\n上一次尝试未通过校验，请修正：{attempt_hint}\n" if attempt_hint else ""
    prompt = f"""下面是短剧第 {episode_no} 集按顺序排列的事件摘要列表（JSON）：
{compact}

任务：
- hook：本集开场钩子，一句话，必须紧扣列表里靠前的某个真实事件，不得脱离事件链凭空编造；
  hook_event_id 填它最贴合的那个 event_id。
- cliffhanger：本集结尾悬念，一句话，必须紧扣列表里靠后的某个真实事件，同样不得凭空编造；
  cliffhanger_event_id 填它最贴合的那个 event_id。
两者都不能为空。
{hint}
"""
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_hook_cliffhanger",
        prompt=prompt,
        model_type=_HookResponse,
        schema_name="episode_prep_pack_hook_v1",
        operation_id=f"episode_prep_pack:{episode_id}:hook",
        max_tokens=1500,
        call_meta={
            "stage_key": "episode_prep_pack_hook_cliffhanger",
            "episode_id": episode_id,
        },
    )


def _validate_hook_grounding(
    text: str, event_id: str, events_by_id: dict[str, dict[str, Any]], *, label: str,
) -> None:
    stripped = (text or "").strip()
    if not stripped:
        raise PrepPackGateError(f"{label} 为空")
    event = events_by_id.get(event_id)
    if event is None:
        raise PrepPackGateError(f"{label}_event_id={event_id!r} 不是事件链中的真实 event_id")
    order = event["order"]
    window = [
        e for e in events_by_id.values()
        if abs(e["order"] - order) <= 2
    ]
    haystack = "。".join(e["summary"] for e in window)
    coverage = bigram_coverage(stripped, haystack)
    if coverage < _HOOK_GROUNDING_COVERAGE:
        raise PrepPackGateError(
            f"{label}「{stripped}」与其接地事件 {event_id} 及相邻事件的文本重合度过低"
            f"（{coverage:.3f} < {_HOOK_GROUNDING_COVERAGE}），疑似编造"
        )


# ---------------------------------------------------------------------------
# One generation attempt
# ---------------------------------------------------------------------------

async def _generate_prep_pack_once(
    *,
    episode_id: str,
    episode_no: int,
    project_id: str,
    chapter_indexes: list[int],
    source_text: str,
    run_id: str | None,
    attempt_hint: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    conn = get_conn()
    segments = index_source_segments(source_text)
    chunks = _chunk_segments(segments)
    known_characters = _known_character_names(conn, project_id, episode_no)
    known_scenes = _known_scene_names(conn, project_id, episode_no)

    raw_events: list[dict[str, Any]] = []  # fed to build_prep_pack_span_ledger
    events: list[dict[str, Any]] = []  # payload-shaped, built after the gate passes
    # 1.4.1: the model's own paratext claim, aggregated across all chunks --
    # untrusted until app.validators.build_prep_pack_span_ledger's three
    # deterministic gates run over it (see that function's module comment).
    declared_paratext_segments: list[int] = []
    event_counter = 0

    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_by_index = {index: segment for index, segment in chunk}
        response = await _extract_chunk(
            episode_id=episode_id,
            episode_no=episode_no,
            chunk_index=chunk_index,
            chunk=chunk,
            known_characters=known_characters,
            known_scenes=known_scenes,
            attempt_hint=attempt_hint,
            run_id=run_id,
        )
        declared_paratext_segments.extend(response.paratext_segments)
        for model_event in response.events:
            event_counter += 1
            event_id = f"ev_{event_counter:03d}"
            raw_events.append({
                "event_id": event_id,
                "order": event_counter,
                "from_segment": model_event.source_span.from_segment,
                "to_segment": model_event.source_span.to_segment,
                "source_evidence": [
                    {"segment_index": e.segment_index, "quote": e.quote}
                    for e in model_event.source_evidence
                ],
                "key_lines": [
                    {"segment_index": k.segment_index} for k in model_event.key_lines
                ],
            })
            events.append({
                "event_id": event_id,
                "order": event_counter,
                "summary": model_event.summary,
                "chunk_by_index": chunk_by_index,
                "model_event": model_event,
            })

    if not raw_events:
        raise PrepPackGateError("本集未抽取到任何事件")

    ledger, ledger_errors, span_extensions, rejected_paratext_claims = build_prep_pack_span_ledger(
        source_text, events=raw_events, declared_paratext_segments=declared_paratext_segments,
    )
    if ledger_errors:
        raise PrepPackGateError(
            "事件跨度账本存在无效声明：" + "；".join(ledger_errors[:10])
        )
    # Deterministic span extension (ERR-20260824-9babad): a verified quote
    # just outside the raw declared span widens that event's own span --
    # publish the widened boundary, not the raw declaration, so downstream
    # (P1 storyboard) sees the span the ledger actually validated against.
    extended_span_by_event_id = {
        item["event_id"]: (item["from"], item["to"]) for item in span_extensions
    }
    try:
        assert_prep_pack_coverage_complete(ledger)
    except ValueError as exc:
        by_index = {index: segment for index, segment in enumerate(segments, start=1)}
        quoted = "；".join(
            f"编号{index}「{by_index[index].text[:60]}」"
            for index in ledger.get("uncovered") or []
            if index in by_index
        )
        raise PrepPackGateError(
            f"{exc}\n请检查事件的 source_span 是否首尾相接、完整覆盖本集全部编号，"
            f"以下编号未落在任何事件的 span 内：{quoted}"
        ) from exc

    # Gate passed: now build the payload-shaped event_chain, aligning each
    # quote/key_line's excerpt for byte-accurate provenance (reusing the same
    # low-threshold alignment the gate itself used).
    payload_events: list[dict[str, Any]] = []
    for event in events:
        model_event = event["model_event"]
        chunk_by_index = event["chunk_by_index"]
        aligned_evidence: list[dict[str, Any]] = []
        for evidence in model_event.source_evidence:
            source_segment = chunk_by_index.get(evidence.segment_index)
            if source_segment is None:
                continue
            aligned = align_source_excerpt(
                evidence.quote, source_segment.text, min_match_chars=QUOTE_MIN_MATCH_CHARS,
            )
            aligned_evidence.append({
                "segment_index": evidence.segment_index,
                "quote": aligned.excerpt if aligned is not None else evidence.quote,
            })
        aligned_key_lines: list[dict[str, Any]] = []
        for key_line in model_event.key_lines:
            source_segment = chunk_by_index.get(key_line.segment_index)
            aligned = (
                align_source_excerpt(
                    key_line.line, source_segment.text, min_match_chars=QUOTE_MIN_MATCH_CHARS,
                )
                if source_segment is not None else None
            )
            aligned_key_lines.append({
                "speaker": key_line.speaker,
                "line": aligned.excerpt if aligned is not None else key_line.line,
                "segment_index": key_line.segment_index,
            })
        extended = extended_span_by_event_id.get(event["event_id"])
        final_from, final_to = (
            extended if extended is not None
            else (model_event.source_span.from_segment, model_event.source_span.to_segment)
        )
        payload_events.append({
            "event_id": event["event_id"],
            "order": event["order"],
            "summary": event["summary"],
            "source_span": {
                "from_segment": final_from,
                "to_segment": final_to,
            },
            "source_evidence": aligned_evidence,
            "key_lines": aligned_key_lines,
            "characters": [
                {
                    "display_name": c.display_name, "is_background_extra": c.is_background_extra,
                    "suspected_true_name": c.suspected_true_name,
                }
                for c in model_event.characters
            ],
            "scenes": [
                {"display_name": s.display_name, "suspected_true_name": s.suspected_true_name}
                for s in model_event.scenes
            ],
        })

    try:
        assert_prep_pack_span_union_matches_ledger(
            event_spans=[event["source_span"] for event in payload_events],
            ledger=ledger,
        )
    except ValueError as exc:
        # Not a model-variance problem (retrying would reproduce it
        # deterministically) but PrepPackGateError keeps the failure mode
        # uniform with every other gate here rather than a bespoke raise.
        raise PrepPackGateError(str(exc)) from exc

    characters, scenes, functional_extras, asset_errors, discovery_stats, true_name_hints = (
        await _run_async_step(
            run_id, "episode_prep_pack_asset_mapping",
            lambda: _resolve_assets(
                conn, project_id=project_id, episode_id=episode_id, episode_no=episode_no,
                source_text=source_text, events=payload_events, run_id=run_id,
            ),
        )
    )
    if asset_errors:
        raise PrepPackGateError(
            "资产映射未能 100% 解析（已尝试身份/场景发现，调用次数："
            f"角色 {discovery_stats['character_discovery_calls']}、"
            f"场景 {discovery_stats['scene_discovery_calls']}）："
            + "；".join(asset_errors[:10])
        )

    # 1.5.0：本集资产名册（characters+functional_extras）此刻已确定性落定，
    # 台词说话人解析走同一份名册，见 _prep_pack_build_speaker_roster/
    # _prep_pack_resolve_key_line_speakers 上方注释（真实 EP2 回归：台词
    # "割舌头"的 speaker 被写成"韩宗"，韩宗第 5 章才出场，speaker 字段从未
    # 进任何校验管线）。
    speaker_roster = _prep_pack_build_speaker_roster(characters, functional_extras)
    speaker_errors = _run_sync_step(
        run_id, "episode_prep_pack_speaker_resolution",
        lambda: _prep_pack_resolve_key_line_speakers(payload_events, speaker_roster),
    )
    if speaker_errors:
        raise PrepPackGateError(
            "台词说话人未能全部解析到本集资产名册：" + "；".join(speaker_errors[:10])
        )

    hook_response = await _extract_hook_cliffhanger(
        episode_id=episode_id,
        episode_no=episode_no,
        events=payload_events,
        attempt_hint=attempt_hint,
        run_id=run_id,
    )
    events_by_id = {event["event_id"]: event for event in payload_events}
    _validate_hook_grounding(
        hook_response.hook, hook_response.hook_event_id, events_by_id, label="hook",
    )
    _validate_hook_grounding(
        hook_response.cliffhanger, hook_response.cliffhanger_event_id, events_by_id,
        label="cliffhanger",
    )

    # 1.5.0 散文字段 lint（观测级，不致命，见 _prep_pack_prose_lint_warnings
    # 上方注释）：谱内专名出现在 summary/hook/cliffhanger 里但本集没出场，
    # 记入观测供人审，不阻断——"被提及未出场"是合法场景。
    roster_names = set(speaker_roster) | {
        str(scene.get("display_name") or "") for scene in scenes if scene.get("display_name")
    }
    lint_warnings = _prep_pack_prose_lint_warnings(
        payload_events=payload_events,
        hook=hook_response.hook, cliffhanger=hook_response.cliffhanger,
        known_names=known_characters + known_scenes, roster_names=roster_names,
    )

    payload = {
        "prep_pack_version": PREP_PACK_VERSION,
        "episode_no": episode_no,
        "episode_scope": {
            "chapter_indexes": chapter_indexes,
            "source_segment_count": len(segments),
        },
        "event_chain": [
            {
                "event_id": event["event_id"],
                "order": event["order"],
                "summary": event["summary"],
                "source_span": event["source_span"],
                "source_evidence": event["source_evidence"],
                "key_lines": event["key_lines"],
            }
            for event in payload_events
        ],
        "asset_manifest": {
            "characters": characters, "scenes": scenes, "functional_extras": functional_extras,
        },
        "coverage_ledger": ledger,
        "hook": hook_response.hook.strip(),
        "cliffhanger": hook_response.cliffhanger.strip(),
    }
    return payload, rejected_paratext_claims, true_name_hints, lint_warnings


# ---------------------------------------------------------------------------
# Atomic publish (原子发布 + 完成证书)
# ---------------------------------------------------------------------------

def _publish_prep_pack(
    *,
    episode_id: str,
    payload: dict[str, Any],
    run_id: str | None,
    rejected_paratext_claims: list[dict[str, Any]] | None = None,
    true_name_hints: list[dict[str, Any]] | None = None,
    lint_warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    conn = get_conn()
    contract = get_contract("screenplay")
    episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError("待发布剧集不存在")

    step_id = (
        evidence_repository.create_step(
            run_id, "episode_prep_pack_publish",
            agent_name="episode_prep_pack",
            contract_version=contract.version,
        )
        if run_id else None
    )
    if step_id:
        transition_step(step_id, "PENDING", "READY", "输入已就绪")
        transition_step(step_id, "READY", "RUNNING", "步骤开始")
    artifact_row = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="episode_prep_pack",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T2",
            content=payload,
            contract_version=contract.version,
        ),
        step_run_id=step_id,
    )
    artifact_id = str(artifact_row["id"])
    artifact_hash = str(artifact_row["content_hash"])

    input_fingerprint = evidence_repository.content_hash({
        "episode_id": episode_id,
        "episode_scope": payload["episode_scope"],
    })

    if conn.in_transaction:
        raise RuntimeError("分集准备包发布前存在未收口事务")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE artifacts SET status='validated' WHERE id=? AND status='candidate'",
            (artifact_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("待发布 working Artifact 状态发生冲突")

        evaluation_row = evidence_repository.create_evaluation(
            artifact_id,
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name=_QA_EVALUATOR_NAME,
                evaluator_version=QA_PROFILE_VERSION,
                status="passed",
                hard_gate_passed=True,
                evaluation_role="score_only",
                runtime_blocking=False,
                retry_eligible=False,
                score=100.0,
                issues=[],
                evidence={
                    "prep_pack_version": PREP_PACK_VERSION,
                    "coverage_uncovered": payload["coverage_ledger"]["uncovered"],
                    # 1.4.1: model's paratext claims that were vetoed back to
                    # ordinary content -- observability only, never part of
                    # the frozen artifact payload itself (see
                    # app.validators.build_prep_pack_span_ledger's
                    # rejected_paratext_claims docstring).
                    "rejected_paratext_claims": rejected_paratext_claims or [],
                    # 1.5.0: every suspected_true_name hypothesis's outcome
                    # (accepted+bound or rejected+discarded) -- observability
                    # only, see _prep_pack_verify_true_name_hypothesis.
                    "true_name_hints": true_name_hints or [],
                    # 1.5.0: prose-field lint warnings (NOT fatal, see
                    # _prep_pack_prose_lint_warnings) -- for human review.
                    "lint_warnings": lint_warnings or [],
                },
            ),
            step_run_id=step_id,
            conn=conn,
            commit=False,
        )

        cert = issue_completion_certificate(
            kind="screenplay",
            scope_id=episode_id,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            input_fingerprint=input_fingerprint,
            contract_version=contract.version,
            qa_profile_version=QA_PROFILE_VERSION,
            evaluation_ids=[str(evaluation_row["id"])],
            blockers=0,
            must_fix_issues=0,
            production_revision_id=None,
            conn=conn,
            commit=False,
        )
        verify_completion_certificate(
            cert,
            expected_artifact_id=artifact_id,
            expected_artifact_hash=artifact_hash,
            expected_input_fingerprint=input_fingerprint,
            expected_contract_version=contract.version,
            conn=conn,
        )
        assert_publish_has_certificate(
            kind="screenplay", episode_id=episode_id, certificate_id=cert.certificate_id,
        )

        conn.execute(
            "UPDATE artifacts SET status='approved', trust_level='T2' WHERE id=?",
            (artifact_id,),
        )
        episode_cursor = conn.execute(
            "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', "
            "screenplay_error=NULL, screenplay_updated_at=?, screenplay_artifact_id=?, "
            "published_screenplay_artifact_id=?, screenplay_completion_certificate_id=?, "
            "active_screenplay_run_id=NULL, status='planned', script_error=NULL "
            "WHERE id=?",
            (
                json.dumps(payload, ensure_ascii=False),
                now(),
                artifact_id,
                artifact_id,
                cert.certificate_id,
                episode_id,
            ),
        )
        if episode_cursor.rowcount != 1:
            raise ValueError("分集准备包发布 episode 更新发生冲突")
        cliffhanger_value = payload["cliffhanger"]
        conn.execute(
            "UPDATE episodes SET cliffhanger=? WHERE id=?",
            (cliffhanger_value, episode_id),
        )
        conn.execute(
            "UPDATE episodes SET hook=? WHERE project_id=? AND episode_no=?",
            (cliffhanger_value, episode["project_id"], episode["episode_no"] + 1),
        )
        consume_completion_certificate(cert.certificate_id, conn=conn, commit=False)
        conn.commit()
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if step_id:
            transition_step(
                step_id, "RUNNING", "FAILED", str(exc)[:1000],
                decision="escalate", error_code=type(exc).__name__.upper(),
            )
        raise
    if step_id:
        transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept")
    return {
        "episode_id": episode_id,
        "artifact_id": artifact_id,
        "certificate_id": cert.certificate_id,
        "status": "ready",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_episode_prep_pack(
    *,
    episode_id: str,
    episode: dict[str, Any],
    source_text: str,
    run_id: str | None,
) -> dict[str, Any]:
    """Generate + atomically publish one episode's episode_prep_pack.

    Bounded retry (contract.max_iterations, currently 2): each attempt
    regenerates the whole pack from scratch -- there is no partial-checkpoint
    repair loop (that heavier design was explicitly retired, see
    docs/TRANSFORM_FREEZE_PLAN.md §3/§6). If the last attempt still fails a
    hard gate, the run fails with the gate's error message.
    """
    contract = get_contract("screenplay")
    project_id = str(episode["project_id"])
    episode_no = int(episode["episode_no"])
    try:
        raw_chapters = episode.get("source_chapters") or []
        chapter_indexes = (
            json.loads(raw_chapters or "[]")
            if isinstance(raw_chapters, str)
            else list(raw_chapters)
        )
    except (TypeError, ValueError):
        chapter_indexes = []
    chapter_indexes = [int(idx) for idx in chapter_indexes]

    attempt_hint = ""
    last_error: Exception | None = None
    for attempt in range(1, max(1, contract.max_iterations) + 1):
        try:
            payload, rejected_paratext_claims, true_name_hints, lint_warnings = (
                await _generate_prep_pack_once(
                    episode_id=episode_id,
                    episode_no=episode_no,
                    project_id=project_id,
                    chapter_indexes=chapter_indexes,
                    source_text=source_text,
                    run_id=run_id,
                    attempt_hint=attempt_hint,
                )
            )
            _publish_prep_pack(
                episode_id=episode_id, payload=payload, run_id=run_id,
                rejected_paratext_claims=rejected_paratext_claims,
                true_name_hints=true_name_hints, lint_warnings=lint_warnings,
            )
            return payload
        except PrepPackGateError as exc:
            last_error = exc
            attempt_hint = str(exc)[:2000]
            continue
    raise last_error if last_error is not None else RuntimeError("分集准备包生成失败")
