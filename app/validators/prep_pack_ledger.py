"""prep pack 原文分段账本（paratext ledger / span ledger）：确定性标题裁边、
结构性副文本识别、跨度账本构建与完整性/并集断言。

2026-08-24 协调方决策版本；build_prep_pack_span_ledger 等目前仅被
app.production.prep_pack 部分引用，其余保持「不再调用但未删除」
（与 app.production.screenplay_repair 同一先例，理由见函数内 docstring）。
"""
from __future__ import annotations

from typing import Any

from app.source_excerpt import (
    SourceSegment,
    align_source_excerpt,
    chapter_title_segment_indexes,
    index_source_segments,
    structural_front_matter_ids,
)

PREP_PACK_SPAN_LAZINESS_MULTIPLIER = 3

# Segment-scoped verbatim check, NOT align_source_excerpt's generic 8-char
# default (see app.production.prep_pack.QUOTE_MIN_MATCH_CHARS for the same
# constant and the real-EP1-output rationale: the 8-char floor silently
# rejected a correct 4-character exact quote). Duplicated here rather than
# imported to keep this module's only import from app.production at zero --
# validators.py is a lower layer that prep_pack.py depends on, not the
# reverse.
_PREP_PACK_QUOTE_MIN_MATCH_CHARS = 2


# --- Paratext ledger account v3 (coordinator decision, 2026-08-24) ---------
# Users do not want "chapter heading" or "author's note" text to show up as
# events in the event chain, but 洞即删戏 still applies -- a segment cannot
# just vanish from the ledger to make that happen. Paratext segments get
# their own fifth account instead: exempted from "must be inside some
# event's span", not exempted from being accounted for.
#
# v1/v2 (both retired the same day, 2026-08-24 -- real round-15 EP2
# regression against a real chapter, proj_3ac0b627fa46 chapters.idx=2,
# exposed that a purely deterministic *classifier* -- keyword table +
# position rules, whichever exact shape -- cannot reliably separate a real
# author's judgment call from ordinary narration using only the source
# text. The model itself already recognizes an author's note when it reads
# one (real EP2: it spontaneously summarized segments 47-50 as an event
# named "作者发布留言"); the missing piece was never model comprehension,
# it was that nothing captured that comprehension as a first-class
# declaration instead of an ordinary (and therefore coverage-gated) event.
#
# v3: the model DECLARES which segments in its own chunk are paratext
# (chapter title / author's request-for-votes-collections-etc. / site
# announcement) via a new ``paratext_segments`` field alongside its normal
# event list (see _ChunkResponse / _extract_chunk's prompt in
# app.production.prep_pack), and does not build narrative events for them.
# That declaration is then run through three independent DETERMINISTIC veto
# gates here -- untrusted prose from a model call is never taken as truth by
# itself anywhere else in this file either (see e.g. the is_background_extra
# discussion in app.production.prep_pack._resolve_assets), and this is no
# exception:
#   a) 位置闸 (position): an accepted segment must be either (i) the
#      structural front-matter segment (chapter-heading regex, unchanged
#      across v1/v2/v3 -- reuses structural_front_matter_ids) or (ii) part
#      of the declared candidates that fall in the last
#      PARATEXT_TAIL_WINDOW_SEGMENTS segments AND form an exact contiguous
#      run ending at the very last segment (no holes -- see (d) below). A
#      segment declared anywhere else, or a tail declaration with a gap in
#      it, fails this gate.
#   b) 无依赖闸 (no dependency): a position-gate survivor must not be
#      referenced by any event's source_evidence or key_lines segment_index
#      -- if some event actually quotes or cites it, the model is using its
#      content as real dramatic material, contradicting its own paratext
#      claim on the very same segment. (hook/cliffhanger grounding is not
#      re-checked separately here: hook/cliffhanger only ever grounds
#      against an event's summary, per app.production.prep_pack._validate_
#      hook_grounding, and gate (c) below already guarantees no event's
#      validated span -- what its summary is derived from -- can include an
#      accepted paratext segment, so this is transitively covered.)
#   c) 排他闸 (exclusivity, FATAL, not a silent veto): a survivor of (a)+(b)
#      must not fall inside ANY event's validated span (the existing
#      paratext_conflict mechanism, unchanged in spirit from v1/v2) -- if it
#      does, the ledger is internally self-contradictory (an event claims
#      the segment's territory while it is also being accounted for as
#      non-story), which blocks publish via ``errors`` exactly like rules
#      (b)/(c) of the span ledger itself, not a quiet reroute.
# A segment that fails gate (a) or (b) is NOT silently dropped and is NOT
# fatal either -- it simply returns to being an ordinary segment, still
# subject to the pre-existing "must be inside some event's span" gate
# (洞即删戏). This is the deliberate asymmetry the coordinator specified:
# the model over-claiming a segment as paratext has no silent path (worst
# case, that segment now needs a real event to cover it, and the run fails
# loudly with a named missing segment if none exists); the model under-
# claiming (declaring nothing, or missing one) only costs a slightly silly
# extra event -- 宁漏勿误, but never 宁误勿漏. Rejections are recorded (not
# just discarded) as ``rejected_paratext_claims`` -- observability only,
# never part of the frozen artifact payload, same status as
# ``normalized_span_extensions``.
PARATEXT_TAIL_WINDOW_SEGMENTS = 6  # K: an accepted tail declaration can only
# ever reach this many segments from the end of the document -- a model
# mislabeling something deep in normal chapter content can, at most, get
# vetoed back to ordinary content; it can never smuggle real narration past
# this far from the tail into the paratext account merely by declaring it.


def _prep_pack_structural_paratext_indexes(segments: list[SourceSegment]) -> set[int]:
    """Rule (a)(i) building block: which segment_index values are the
    document's own structural front matter (chapter heading, unchanged
    across v1/v2/v3 -- reuses app.source_excerpt.structural_front_matter_ids,
    the same first-segment-only regex the narrative blueprint layer already
    uses). This alone is NOT the paratext account -- see build_prep_pack_
    span_ledger's gate (a): the model must still have declared the segment
    for it to be accepted, matching every other declared-then-gated
    candidate rather than being auto-applied regardless of the model's own
    output."""
    if not segments:
        return set()
    index_by_segment_id = {
        segment.segment_id: index for index, segment in enumerate(segments, start=1)
    }
    return {
        index_by_segment_id[segment_id]
        for segment_id in structural_front_matter_ids(segments)
        if segment_id in index_by_segment_id
    }


def _prep_pack_deterministic_title_indexes(
    segments: list[SourceSegment], chapter_titles: list[str] | None,
) -> set[int]:
    """DB-anchored chapter-title segments (1.9.0, see PREP_PACK_VERSION's
    1.9.0 note in app.production.prep_pack for the full regression history).

    Unlike ``_prep_pack_structural_paratext_indexes`` above (which is only
    ever a *candidate* the model must still separately declare in
    ``paratext_segments`` before gate (a)(i) accepts it), this is trusted
    unconditionally -- see this function's only caller,
    ``build_prep_pack_span_ledger``, for where it is merged straight into
    the ``paratext`` account without going through the declare-then-veto
    pipeline at all. That is safe specifically *because* it is derived from
    ``chapters.title``, a real database column, not guessed from the
    source text's own shape or a hardcoded name/keyword list (project rule:
    no blacklist/whitelist judgments) -- the same reasoning that sank v1
    (a pure structural/keyword classifier, 5a67511, retired the same day)
    does not apply here, because v1 was trying to *guess* paratext from
    generic text shape; this only ever fires for a segment that is
    byte-for-byte (modulo whitespace) one of THIS episode's own chapters'
    titles.

    ``chapter_titles`` is expected to already exclude NULL/blank titles (see
    app.production.prep_pack's chapter-titles query) -- a chapter with no
    title in the DB simply contributes nothing here, which is exactly the
    "fall back to the pre-existing regex+model-declare path" behavior the
    caller wants for that chapter (see build_prep_pack_span_ledger's
    docstring)."""
    if not segments or not chapter_titles:
        return set()
    return chapter_title_segment_indexes(segments, chapter_titles)


def build_prep_pack_span_ledger(
    source_text: str,
    *,
    events: list[dict[str, Any]],
    declared_paratext_segments: list[int] | None = None,
    chapter_titles: list[str] | None = None,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministically validate event spans and project the coverage ledger.

    Coverage accounting design (screenplay contract 6.0.0, invariant ①; see
    app.production.prep_pack module docstring for the full history of why
    this replaced an earlier per-segment disposition-declaration design that
    three rounds of patching couldn't make reliable against real model
    output). The model declares, per event, a ``source_span`` -- a closed
    [from_segment, to_segment] interval of the indexed source segments that
    event covers -- instead of dispositioning every individual segment.

    ``events``: ordered list of dicts, each ``{"event_id": str, "order": int,
    "from_segment": int, "to_segment": int, "source_evidence":
    [{"segment_index": int, "quote": str}, ...], "key_lines":
    [{"segment_index": int, ...}, ...]}`` (raw model output, not yet
    alignment-verified -- this function does that itself). ``key_lines``
    entries only need ``segment_index`` for this function's purposes (gate
    (b) below); other keys are ignored here.

    ``declared_paratext_segments``: the model's own claim (aggregated across
    all chunks) of which segment_index values are non-story paratext
    (chapter title / author's note) -- see the module comment above
    PARATEXT_TAIL_WINDOW_SEGMENTS for the full v3 design (model declares,
    this function runs three deterministic veto gates before accepting any
    of it). ``None``/empty means the model declared nothing, which is a
    legal (if a little silly) outcome, not an error.

    ``chapter_titles`` (1.9.0): this episode's own ``chapters.title`` values
    (NULL/blank titles already filtered out by the caller). Every segment
    whose entire text is exactly one of these titles (see
    app.source_excerpt.chapter_title_segment_ids for the exact matching
    rule, including the known "content repeats its own title" join
    artifact) is a DB-anchored structural fact, not a model claim -- it is
    merged into the ``paratext`` account unconditionally, WITHOUT going
    through the declared_paratext_segments pipeline above (the model may
    still also declare it; the two are just unioned, see 语义 below). If
    such a segment also happens to fall inside some event's validated span,
    that is not gate (c)'s fatal exclusivity contradiction the way an
    over-claimed ``declared_paratext_segments`` entry is -- the segment is
    deterministically trimmed off that event's span edge instead (an event
    whose span is NOTHING BUT title segments, after trimming, is a
    different fatal case: see 确定性标题裁边 below). A chapter with no DB
    title simply contributes nothing here, which is exactly "fall back to
    the pre-1.9.0 behavior" for that chapter (regex structural guess still
    requires the model's own paratext_segments declaration, unchanged).

    **确定性标题裁边**（1.9.0，见 app.production.prep_pack.PREP_PACK_VERSION
    的 1.9.0 大注释根因）：真实 EP5 回归——章节标题段（SRC0001）只在模型
    这次调用申报了 paratext_segments 时才免于事件覆盖（1.4.1 起的既有设计），
    而模型申报是非确定性的，漏报时"洞即删戏"仍逼着某个事件去覆盖这一段，
    模型最省力的满足方式就是编一个只覆盖标题这一段的伪事件，其逐字抄自
    标题原文的引文又恰好能通过引文锚地闸门——合法通过全部三道致命闸门。
    修复只把"这一段是不是本集自己某一章的标题"这个有 chapters.title 数据库
    锚点的窄场景改回确定性：deterministic_title_indexes 从 chapter_titles
    算出后，每个事件计算出自己的（申报∪证据扩展）span 后，先从这个 span
    的两端向内裁掉任何确定性标题段（例如 [1,5] 因为段 1 是标题被裁成
    [2,5]），裁边后的边界才是最终发布的 source_span、参与 covered/delivered
    记账；裁掉的标题段永远只计入 paratext，不计入 covered，五账因此自动
    保持自洽。裁边只处理跨度两端（真实数据里标题段只可能出现在章节起止
    边界，不可能出现在一个事件跨度的正中间）——如果裁边后仍有确定性标题段
    残留在跨度内部（无法通过裁边消除的反常形状），这不属于"良性边缘重合"，
    仍是账本自相矛盾，致命阻断，不静默处理。若一个事件的 span 裁掉标题段后
    变空（即该事件从头到尾只覆盖标题段——这正是本缺陷的伪事件形态），同样
    致命阻断并明确报出"事件仅覆盖章节标题段"，防止 1.9.0 之后模型公然违背
    _extract_chunk 新增的确定性提示（见 prep_pack 模块）时问题被静默吞掉。

    **确定性跨度扩展**（EP2 首次正式回归暴露，ERR-20260824-9babad：模型对
    ev_003 声明 span=[13,16] 却引用了 segment 17，方差性的"跨度写窄一格"，
    不是新回归缺陷）："申报 ∪ 确定性证明"哲学的直接延伸——逐字核实的引文是
    强证据，跨度声明本身只是弱元数据；一个事件的最终（用于覆盖记账的）span
    归一为 ``[原始 span] ∪ [该事件全部逐字核实引文的 segment_index]``。这不是
    放宽门禁：扩展只能被这个事件**自己**的、已核实（align_source_excerpt
    命中）的引文推动，不核实的东西、别的事件的引文都不能移动它的边界一步。
    扩展记录进返回值第三项 ``normalized_span_extensions``，供观测。

    **语义分离**（1.5.0，ERR-20260824-22cb1c：真实第17轮 EP3 回归，
    ev_010 扩展后起点 38 撞前一事件申报终点 39——相邻事件合法共享过渡段落
    时，后一个事件对该过渡段的逐字引文把自己的 span 往回扩展了一格，被
    误判成"跨度交叉"，即使两个事件申报的原始 span 从未真正交叉）："叙事
    结构主张"（这个事件在故事里排第几、跟邻居的边界在哪）与"交付证明"
    （这个事件到底有没有证据撑住自己声称覆盖的范围）是两种不同语义，不能
    用同一把尺子量：
      - 跨度有序检查（规则 c）**只看模型申报的原始 span**，独立于证据核验
        与扩展计算，从不受扩展结果影响；
      - 覆盖/无洞（规则 a）与反懒惰护栏（规则 b 的一部分）**继续用扩展后
        的 span 并集**——扩展是为了证明"这段确实交付了"，跟"事件之间谁先
        谁后"无关。扩展导致相邻（甚至更远）事件的 span 出现重叠，不再是
        错误，是良性双重覆盖（证据外溢，不是叙事边界主张）。

    Exactly three things are fatal (**洞即删戏、引文锚地、跨度有序** -- all
    three, and only these three, block publish; everything else is
    unrestricted):
      a) **洞即删戏**: the union of all validated (EXTENDED) spans must equal
         ``[1, total_segments]`` with no gaps -- any segment outside every
         event's span is content that got silently cut, full stop, no size
         exemption (an earlier "small holes get interpolated" mechanism was
         explicitly retired: real output showed the "hole" was actually a
         structural artifact, not a size problem, so a size-based exemption
         was the wrong lever).
      b) **引文锚地**: each event must have >=1 verbatim-aligned quote
         (source_evidence) -- proves the event isn't fabricated. A quote
         that fails alignment (does not actually match its claimed
         segment's text) simply does not count, anywhere; it neither
         anchors nor extends anything.
      c) **跨度有序**: spans must advance in ``order`` -- evaluated against
         each event's DECLARED (raw, unextended) span only (1.5.0, see
         above) -- the next event's declared from_segment may not be less
         than the previous event's declared to_segment. Sharing exactly the
         boundary segment is normal and expected, not an error. A real
         declared-span crossing or regression means the model's own
         narrative-structure claim contradicts itself about who owns a
         segment -- fatal; an EXTENDED-span overlap that arises purely from
         evidence spillover is not (see 语义分离 above).
    The anti-laziness guardrail (PREP_PACK_SPAN_LAZINESS_MULTIPLIER) is part
    of (b), evaluated against the EXTENDED span length: a span far larger
    than average is not "well anchored" merely by having one quote
    somewhere in it, so oversized spans need two quotes spread across their
    own front/back halves.

    Returns ``(ledger, errors, normalized_span_extensions,
    rejected_paratext_claims)``. The ledger has five accounts; four are a
    pure PROJECTION of the validated (extended) spans, not a model
    declaration, and the fifth (paratext) is the model's own declaration
    after surviving the three v3 gates (see the module comment above
    PARATEXT_TAIL_WINDOW_SEGMENTS) that exempts chapter-heading /
    author's-note segments from needing event coverage without letting them
    silently disappear:
      delivered = segments with a verbatim-aligned quote in their owning
        event's source_evidence
      retained_as_context = other segments inside some validated span
      paratext = declared segments that passed gates (a) position and (b)
        no-dependency; a segment here is, by construction, never also in
        delivered/retained_as_context -- if some event's span somehow
        covers one anyway, that is gate (c)'s fatal contradiction (see
        paratext_conflict below), not a silent merge
      uncovered = segments outside every span AND not paratext (rule a's
        fatal case)
      merged / proven_duplicates = always [] -- the model no longer declares
        per-segment disposition, so these accounts have nothing to populate
    ``errors`` describes rule (b)/(c) span violations (an event's own claims
    are internally inconsistent, even after extension) *and* gate (c)'s
    paratext/covered overlap (ledger self-contradiction, coordinator v3 red
    test (c)); a non-empty ``uncovered`` is rule (a) and is reported by
    ``assert_prep_pack_coverage_complete`` instead, not here.
    ``normalized_span_extensions`` is
    ``[{"event_id", "from", "to", "extended_by"}]`` for every event whose
    final span differs from its raw declared one -- observability only, the
    caller (app.production.prep_pack) also uses it to publish the extended
    span in the artifact payload's event_chain[].source_span.
    ``rejected_paratext_claims`` is ``[{"segment_index", "gate", "reason"}]``
    for every declared segment that failed gate (a) or (b) -- observability
    only (never part of the frozen artifact payload), not a subset of
    ``errors`` since a silent veto/reroute is by design not fatal by itself
    (see module comment).
    """
    segments = index_source_segments(source_text or "")
    by_index = {index: segment for index, segment in enumerate(segments, start=1)}
    total = len(segments)
    structural_indexes = _prep_pack_structural_paratext_indexes(segments)
    # 1.9.0: DB-anchored chapter-title segments -- see
    # _prep_pack_deterministic_title_indexes and this function's own
    # "确定性标题裁边" docstring section. Unconditional, unlike
    # structural_indexes above (which still needs the model's own
    # paratext_segments declaration to be accepted).
    deterministic_title_indexes = _prep_pack_deterministic_title_indexes(
        segments, chapter_titles,
    )
    errors: list[str] = []
    extensions: list[dict[str, Any]] = []
    ordered = sorted(events, key=lambda e: int(e.get("order") or 0))
    event_count = len(ordered)
    avg_span = (total / event_count) if event_count else 0.0
    laziness_threshold = avg_span * PREP_PACK_SPAN_LAZINESS_MULTIPLIER

    delivered: set[int] = set()
    covered: set[int] = set()
    # gate (b) input: every segment_index any event cites, regardless of
    # whether that event's own span declaration turns out to be valid --
    # citing a segment as real evidence is what matters here, not span
    # validity.
    referenced: set[int] = set()
    # 1.5.0 语义分离（ERR-20260824-22cb1c，真实第17轮 EP3 回归）：有序性/
    # 交叉/倒退检查只用模型申报的原始 span（叙事结构主张），不用确定性扩展
    # 后的 span（交付证明）——旧代码把这两种语义混用同一把尺子，导致相邻
    # 事件合法共享过渡段落时，后一个事件对该过渡段的证据引用（把自己的
    # span 往回扩展一格）被误判成"跨度交叉"，即使两个事件申报的原始 span
    # 从未真正交叉。prev_declared_to 只跟踪申报值，从不被扩展后的值污染。
    prev_declared_to = 0
    for event in ordered:
        event_id = str(event.get("event_id") or "")
        declared_from = int(event.get("from_segment") or 0)
        declared_to = int(event.get("to_segment") or 0)
        for item in event.get("source_evidence") or []:
            idx = int(item.get("segment_index") or 0)
            if idx:
                referenced.add(idx)
        for item in event.get("key_lines") or []:
            idx = int(item.get("segment_index") or 0)
            if idx:
                referenced.add(idx)
        if declared_from < 1 or declared_to > total or declared_from > declared_to:
            errors.append(
                f"事件 {event_id} 的 source_span [{declared_from},{declared_to}] "
                "超出原文范围或首尾颠倒"
            )
            continue
        # 跨度有序（规则c）：只比较申报值，独立于证据核验/扩展计算——这是
        # 一条纯粹的叙事结构主张检查，不依赖也不污染下面的交付证明逻辑。
        # 共享边界（declared_from == prev_declared_to）依旧合法，不算交叉。
        if declared_from < prev_declared_to:
            errors.append(
                f"事件 {event_id} 的 source_span [{declared_from},{declared_to}] "
                f"（申报值）起点早于前一事件申报终点 {prev_declared_to}，跨度交叉或倒退"
            )
            continue
        # Verify every claimed quote against its OWN claimed segment's real
        # text, regardless of whether that segment lies inside the raw
        # declared span -- alignment success is what "verified" means here,
        # not span membership.
        anchored: set[int] = set()
        for item in event.get("source_evidence") or []:
            idx = int(item.get("segment_index") or 0)
            source_segment = by_index.get(idx)
            if source_segment is None:
                continue
            aligned = align_source_excerpt(
                str(item.get("quote") or ""), source_segment.text,
                min_match_chars=_PREP_PACK_QUOTE_MIN_MATCH_CHARS,
            )
            if aligned is not None:
                anchored.add(idx)
        if not anchored:
            errors.append(
                f"事件 {event_id} 在其 span [{declared_from},{declared_to}] 附近"
                "没有任何逐字引文命中原文，缺少可核验证据"
            )
            continue
        # Deterministic span extension (交付证明 only -- 1.5.0 起不再反馈进
        # 跨度有序检查): only this event's own verified quotes may move its
        # boundary, and only outward. An extended span overlapping a
        # neighbor's (declared or extended) span is no longer, by itself,
        # an error -- it is benign double coverage (delivery-evidence
        # spillover), see ERR-20260824-22cb1c above.
        from_segment = min(declared_from, min(anchored))
        to_segment = max(declared_to, max(anchored))

        # 1.9.0 确定性标题裁边（见本函数 docstring "确定性标题裁边"）：从
        # 这个（申报∪证据扩展）span 的两端向内裁掉任何 DB 锚定的章节标题
        # 段——真实数据里标题段只会出现在章节起止边界，不会出现在一个事件
        # 跨度正中间，所以只处理两端；裁边不影响上面已经跑完的跨度有序
        # 检查（那条检查只看申报值，1.5.0 语义分离原则的直接延伸）。
        trimmed_from, trimmed_to = from_segment, to_segment
        while trimmed_from <= trimmed_to and trimmed_from in deterministic_title_indexes:
            trimmed_from += 1
        while trimmed_to >= trimmed_from and trimmed_to in deterministic_title_indexes:
            trimmed_to -= 1
        if trimmed_from > trimmed_to:
            errors.append(
                f"事件 {event_id} 的 source_span [{declared_from},{declared_to}] "
                "裁掉确定性章节标题段后为空——事件仅覆盖章节标题段，不是真实剧情，"
                "禁止为章节标题创建事件"
            )
            continue
        # 反常形状防御：裁边只能消除跨度两端的标题段，如果裁边后跨度内部
        # 仍残留标题段，说明它被夹在两段真实内容中间——这不是"良性边缘
        # 重合"，裁边无法安全解决，必须致命阻断而不是静默吞掉。
        interior_title_conflict = sorted(
            idx for idx in deterministic_title_indexes
            if trimmed_from <= idx <= trimmed_to
        )
        if interior_title_conflict:
            shown = "、".join(str(i) for i in interior_title_conflict)
            errors.append(
                f"事件 {event_id} 的 span 裁边后仍在跨度内部残留确定性章节标题段"
                f"（{shown}，不在跨度边界，无法通过裁边消除）——账本自相矛盾"
            )
            continue

        final_from, final_to = trimmed_from, trimmed_to
        # delivered/laziness 都只看裁边后仍在最终跨度内的证据——一条只
        # 命中已被裁掉的标题段的引文不再算这个事件自己的交付证据（否则会让
        # 同一个段号同时落进 delivered 和 paratext 两个账户，破坏五账互斥）。
        anchored_in_span = {idx for idx in anchored if final_from <= idx <= final_to}
        if not anchored_in_span:
            errors.append(
                f"事件 {event_id} 的 span 裁掉确定性章节标题段后，剩余跨度 "
                f"[{final_from},{final_to}] 内没有任何逐字引文命中原文，缺少可核验证据"
            )
            continue
        if (final_from, final_to) != (declared_from, declared_to):
            extended_by = sorted(
                idx for idx in anchored_in_span if idx < declared_from or idx > declared_to
            )
            extensions.append({
                "event_id": event_id,
                "from": final_from,
                "to": final_to,
                "extended_by": extended_by,
            })
        span_len = final_to - final_from + 1
        if span_len > laziness_threshold:
            midpoint = (final_from + final_to) / 2
            front = [a for a in anchored_in_span if a <= midpoint]
            back = [a for a in anchored_in_span if a > midpoint]
            if len(anchored_in_span) < 2 or not front or not back:
                errors.append(
                    f"事件 {event_id} 的 span 跨度 {span_len} 段，超过均值×"
                    f"{PREP_PACK_SPAN_LAZINESS_MULTIPLIER}（{laziness_threshold:.1f}），"
                    "但引文少于两条或未分布在跨度前后半，疑似整段打包偷懒"
                )
        delivered |= anchored_in_span
        covered |= set(range(final_from, final_to + 1))
        prev_declared_to = max(prev_declared_to, declared_to)

    # --- Paratext v3 gates: model DECLARES, this function DISPOSES -----
    # (see the module comment above PARATEXT_TAIL_WINDOW_SEGMENTS for the
    # full design). declared_paratext_segments is untrusted model prose,
    # same status as any other model claim in this file -- nothing here
    # trusts it until it survives every applicable gate.
    declared = {
        int(i) for i in (declared_paratext_segments or [])
        if 1 <= int(i) <= total
    }
    rejected_paratext_claims: list[dict[str, Any]] = []
    position_accepted: set[int] = set()

    # gate (a)(i): structural front matter (chapter heading) -- only if the
    # model actually declared it; the structural regex alone is no longer
    # sufficient by itself (v1/v2 retired -- see module comment).
    structural_declared = declared & structural_indexes
    position_accepted |= structural_declared

    # gate (a)(ii): tail window, exact contiguous run reaching the final
    # segment -- a declared tail set with any gap in it is rejected whole,
    # not partially salvaged (that is precisely what a real author's-note
    # transition sentence without its own strong signal would otherwise
    # slip past -- but here the MODEL already told us it is paratext, so
    # this gate only needs to confirm shape, not go hunting for evidence).
    window_start = max(1, total - PARATEXT_TAIL_WINDOW_SEGMENTS + 1)
    tail_declared = {i for i in declared if i >= window_start} - structural_declared
    if tail_declared:
        run_len = len(tail_declared)
        expected_suffix = set(range(total - run_len + 1, total + 1))
        if total in tail_declared and tail_declared == expected_suffix:
            position_accepted |= tail_declared
        else:
            for index in sorted(tail_declared):
                rejected_paratext_claims.append({
                    "segment_index": index, "gate": "position",
                    "reason": "尾窗申报不是含最末段的连续块（挖洞或未到文末）",
                })

    # anything declared but neither structural nor even inside the tail
    # window's numeric range at all -- outright outside gate (a)'s reach.
    outside_window_declared = declared - structural_declared - tail_declared
    for index in sorted(outside_window_declared):
        rejected_paratext_claims.append({
            "segment_index": index, "gate": "position",
            "reason": "既非首段章节名，也不在尾窗范围内",
        })

    # gate (b): no dependency -- a position-gate survivor that some event
    # actually cites as evidence/key_line contradicts its own paratext
    # claim; silently rerouted back to ordinary content, not fatal by
    # itself (module comment: 宁漏勿误, never 宁误勿漏).
    dependency_accepted: set[int] = set()
    for index in sorted(position_accepted):
        if index in referenced:
            rejected_paratext_claims.append({
                "segment_index": index, "gate": "dependency",
                "reason": "被某个事件的 source_evidence/key_lines 引用",
            })
        else:
            dependency_accepted.add(index)

    # gate (c): exclusivity -- FATAL, not a silent veto (协调方红灯 c). A
    # survivor of (a)+(b) that ALSO falls inside some event's validated span
    # means the ledger contradicts itself about who owns that segment --
    # walks the same errors-list path as rules (b)/(c) of the span ledger
    # itself (caller sees ledger_errors non-empty -> PrepPackGateError,
    # never reaches the uncovered check below). merged/proven_duplicates are
    # always empty under this accounting, so no separate check needed there.
    paratext_conflict = sorted(dependency_accepted & covered)
    if paratext_conflict:
        shown = "、".join(str(i) for i in paratext_conflict[:20])
        errors.append(
            f"以下编号已通过位置闸/无依赖闸判定为副文本申报，却又被某个事件的 "
            f"source_span 覆盖，账本自相矛盾：{shown}"
        )

    # 1.9.0: DB-anchored chapter-title segments are merged in unconditionally
    # (see this function's "确定性标题裁边" docstring section) -- they never
    # went through the declared/position/dependency pipeline above at all,
    # and the per-event loop's trim-or-fatal handling above already
    # guarantees deterministic_title_indexes is disjoint from ``covered``,
    # so this union can never trigger gate (c)'s paratext_conflict check.
    paratext = dependency_accepted | deterministic_title_indexes
    uncovered = sorted(set(by_index) - covered - paratext)
    retained_as_context = sorted(covered - delivered)
    ledger = {
        "total_segments": total,
        "delivered": sorted(delivered),
        "merged": [],
        "retained_as_context": retained_as_context,
        "proven_duplicates": [],
        "paratext": sorted(paratext),
        "uncovered": uncovered,
    }
    return ledger, errors, extensions, rejected_paratext_claims


def assert_prep_pack_coverage_complete(ledger: dict[str, Any]) -> None:
    """Hard gate (决策②): publish must block while any segment is uncovered.

    The error names every missing segment_index so the failure is
    actionable instead of a generic "coverage incomplete" message --
    this is the "禁止静默删戏" gate the user named as non-negotiable.

    门禁最终形态（三次真实 EP1 生成迭代定型后，改用事件跨度记账彻底重做；
    见 app.production.prep_pack 模块 docstring 和
    build_prep_pack_span_ledger 的完整论证，docs/TRANSFORM_FREEZE_PLAN.md）：
    **洞即删戏（致命）、引文锚地（致命）、跨度有序（致命）、其余不设限**。
      - 洞即删戏：uncovered 非空即阻断，本函数唯一的阻断条件，不再按洞的
        大小区分（"小洞插值"机制已随旧的逐段记账设计一起废弃——真实输出
        证明所谓"小洞"其实是结构性的：无法逐字引用的短促感叹，或跨事件
        边界紧贴非剧情附言，跟洞的大小无关，用大小做豁免是找错了维度）；
      - 引文锚地 / 跨度有序：由 build_prep_pack_span_ledger 的 errors
        返回值单独把关（账本自身内部矛盾，不是"覆盖不足"），与本函数
        （只看 uncovered）是两道独立的门；
      - 其余不设限：模型不再申报任何逐段 disposition，没有"冗余申报"这类
        问题需要归一化——四账是从已验证的 span 确定性投影出来的，不是
        模型的自由声明。
    跨度声明本身只是弱元数据：可以被这个事件自己已核实（逐字对齐命中）的
    引文确定性扩展（见 build_prep_pack_span_ledger），但不可以被任何未核实
    的东西扩展——扩展权限只属于证据，不属于声明或猜测。
    """
    uncovered = list(ledger.get("uncovered") or [])
    if not uncovered:
        return
    shown = "、".join(str(i) for i in uncovered[:30])
    extra = f"（另有 {len(uncovered) - 30} 段）" if len(uncovered) > 30 else ""
    raise ValueError(
        "[PREP_PACK_COVERAGE_INCOMPLETE] coverage_ledger.uncovered 非空，"
        f"共 {len(uncovered)} 个原文段未落在任何事件的 source_span 内：{shown}{extra}；"
        "每个已索引原文段必须被某个事件的跨度覆盖，禁止静默删戏"
    )


def assert_prep_pack_span_union_matches_ledger(
    *, event_spans: list[dict[str, int]], ledger: dict[str, Any],
) -> None:
    """Hard gate (prep_pack_version 1.1.0): the *published* event objects'
    source_span union must exactly equal the published coverage_ledger's
    projected coverage (delivered ∪ retained_as_context).

    build_prep_pack_span_ledger already derives the ledger from the same
    validated spans, so by construction these agree at the point the ledger
    is built. This is a second, independent check at the artifact-assembly
    boundary in app.production.prep_pack -- it protects against the payload's
    event objects and its own coverage_ledger silently drifting apart if a
    future edit ever builds them from different data (e.g. re-deriving one
    after filtering/reordering events without re-deriving the other). A
    mismatch here means the artifact would be internally self-contradictory,
    which is fatal independent of whether build_prep_pack_span_ledger itself
    raised anything.
    """
    span_union: set[int] = set()
    for span in event_spans:
        from_segment = int(span["from_segment"])
        to_segment = int(span["to_segment"])
        span_union |= set(range(from_segment, to_segment + 1))
    ledger_covered = set(ledger.get("delivered") or []) | set(
        ledger.get("retained_as_context") or []
    )
    if span_union != ledger_covered:
        missing_from_span = sorted(ledger_covered - span_union)
        extra_in_span = sorted(span_union - ledger_covered)
        raise ValueError(
            "[PREP_PACK_SPAN_LEDGER_MISMATCH] 发布产物事件对象的 source_span 并集与"
            "自身 coverage_ledger 投影不一致，账本与产物自相矛盾："
            f"账本记为已覆盖但事件 span 未覆盖：{missing_from_span}；"
            f"事件 span 覆盖但账本未记为已覆盖：{extra_in_span}"
        )
