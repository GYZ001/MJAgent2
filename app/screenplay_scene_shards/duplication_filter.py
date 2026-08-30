"""Filters semantic-review findings that flag a creative draft for reproducing
source text verbatim or duplicating another slot's exact-source or
distinct-ownership content, so genuine violations are not drowned out by
expected quoting.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

from app.source_excerpt import (
    quotation_closing,
    quotation_opening,
)
from app.source_facts import (
    SourceFact,
    source_segment_facts,
)

from .constants import (
    SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS,
    SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS,
)
from .models import (
    ScreenplaySceneInputContract,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardCreativeUnit,
    ScreenplaySceneShardSemanticFinding,
    ScreenplaySceneShardSemanticReview,
)


def _scene_shard_exact_source_text(value: str) -> str:
    normalized = "".join(str(value or "").split())
    while (
        len(normalized) >= 2
        and quotation_opening(normalized[0])
        and quotation_closing(normalized[0], normalized[-1])
    ):
        normalized = normalized[1:-1]
    return normalized


def _scene_shard_creative_has_exact_source_support(
    creative: ScreenplaySceneShardCreativeUnit,
    source_fact: SourceFact,
) -> bool:
    source_text = _scene_shard_exact_source_text(source_fact.text)
    content_values = (
        creative.text,
        creative.performance,
        creative.resulting_state,
        creative.required_text,
        creative.prop_text,
        creative.on_screen_text,
    )
    non_empty_values = [
        value
        for value in content_values
        if str(value or "").strip()
    ]
    return bool(source_text and non_empty_values) and all(
        _scene_shard_exact_source_text(value) == source_text
        for value in non_empty_values
    )


def _scene_shard_source_facts_by_slot(
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> dict[str, SourceFact | None]:
    source_facts_by_key = {
        fact.source_unit_key: fact
        for contract in scene_input_contracts
        for segment in contract.source_segments
        for fact in source_segment_facts(
            segment.source_segment_id,
            segment.text,
        )
    }
    return {
        slot.unit_key: source_facts_by_key.get(slot.source_unit_key)
        for contract in scene_input_contracts
        for slot in contract.unit_slots
    }


def _scene_shard_canonicalize_cross_slot_findings(
    review: ScreenplaySceneShardSemanticReview,
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardSemanticReview:
    source_facts_by_slot = _scene_shard_source_facts_by_slot(
        scene_input_contracts
    )
    canonical_findings: list[ScreenplaySceneShardSemanticFinding] = []
    for finding in review.findings:
        if "cross_slot_duplication" not in finding.violation_kinds:
            canonical_findings.append(finding)
            continue
        related_unit_key = finding.related_unit_keys[0]
        target_creative = draft.slots.get(finding.unit_key)
        related_creative = draft.slots.get(related_unit_key)
        target_source_fact = source_facts_by_slot.get(finding.unit_key)
        related_source_fact = source_facts_by_slot.get(related_unit_key)
        if (
            target_creative is None
            or related_creative is None
            or target_source_fact is None
            or related_source_fact is None
        ):
            canonical_findings.append(finding)
            continue
        target_source_text = _scene_shard_exact_source_text(
            target_source_fact.text
        )
        related_source_text = _scene_shard_exact_source_text(
            related_source_fact.text
        )
        if (
            target_source_text
            and related_source_text
            and target_source_text != related_source_text
            and _scene_shard_exact_source_text(target_creative.text)
            == target_source_text
            and _scene_shard_exact_source_text(related_creative.text)
            != related_source_text
        ):
            canonical_findings.append(
                ScreenplaySceneShardSemanticFinding.model_validate({
                    **finding.model_dump(mode="json"),
                    "unit_key": related_unit_key,
                    "related_unit_keys": [finding.unit_key],
                })
            )
        else:
            canonical_findings.append(finding)
    merged_findings: dict[
        tuple[str, str],
        ScreenplaySceneShardSemanticFinding,
    ] = {}
    for finding in canonical_findings:
        key = (finding.unit_key, finding.code)
        existing = merged_findings.get(key)
        if existing is None:
            merged_findings[key] = finding
            continue
        merged_kinds = [
            kind
            for kind in SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS
            if (
                kind in existing.violation_kinds
                or kind in finding.violation_kinds
            )
        ]
        merged_related_keys = list(dict.fromkeys([
            *existing.related_unit_keys,
            *finding.related_unit_keys,
        ]))
        if (
            "cross_slot_duplication" in merged_kinds
            and len(merged_related_keys) != 1
        ):
            # 两条同 (unit_key, code) 的 finding 指向了不同的对手 slot。这不是
            # 不可能发生的矛盾：上面的 canonicalize 步骤会把 finding 改挂到它的
            # related slot 上，后端自己就会制造这种碰撞。而每条 finding 只允许
            # 恰好一个对手，所以合并结果无法同时表达两个。
            #
            # 共识层对「两名审稿人给出不同对手」早有既定处置：撤掉 cross-slot
            # 这一类，保留其余类型。这里沿用同一条规则——抛内部错误的旧行为让
            # 整集停摆，反而连其余类型的门禁一起丢掉。
            merged_kinds = [
                kind for kind in merged_kinds
                if kind != "cross_slot_duplication"
            ]
            merged_related_keys = []
        if "cross_slot_duplication" not in merged_kinds:
            merged_related_keys = []
        if not merged_kinds:
            # 撤掉 cross-slot 后无类型可报：该 finding 不再成立。
            merged_findings.pop(key, None)
            continue
        merged_message = "；".join(dict.fromkeys([
            existing.message,
            finding.message,
        ]))
        merged_findings[key] = (
            ScreenplaySceneShardSemanticFinding.model_validate({
                **existing.model_dump(mode="json"),
                "related_unit_keys": merged_related_keys,
                "violation_kinds": merged_kinds,
                "message": merged_message[
                    :SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS
                ],
            })
        )
    return ScreenplaySceneShardSemanticReview(
        findings=list(merged_findings.values())
    )


def _scene_shard_filter_exact_source_duplication(
    review: ScreenplaySceneShardSemanticReview,
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardSemanticReview:
    source_facts_by_slot = _scene_shard_source_facts_by_slot(
        scene_input_contracts
    )
    filtered: list[ScreenplaySceneShardSemanticFinding] = []
    for finding in review.findings:
        creative = draft.slots.get(finding.unit_key)
        source_fact = source_facts_by_slot.get(finding.unit_key)
        if (
            "cross_slot_duplication" not in finding.violation_kinds
            or creative is None
            or source_fact is None
            or not _scene_shard_creative_has_exact_source_support(
                creative,
                source_fact,
            )
        ):
            filtered.append(finding)
            continue
        remaining_kinds = [
            kind
            for kind in finding.violation_kinds
            if kind != "cross_slot_duplication"
        ]
        if remaining_kinds:
            filtered.append(
                ScreenplaySceneShardSemanticFinding(
                    unit_key=finding.unit_key,
                    related_unit_keys=[],
                    code=finding.code,
                    violation_kinds=remaining_kinds,
                    message=finding.message,
                )
            )
    return ScreenplaySceneShardSemanticReview(findings=filtered)


def _scene_shard_creative_reproduces_source_text(
    creative: ScreenplaySceneShardCreativeUnit,
    source_text: str,
) -> bool:
    """Whether any creative content field verbatim carries the given source."""
    normalized_source = _scene_shard_exact_source_text(source_text)
    if not normalized_source:
        return False
    content_values = (
        creative.text,
        creative.performance,
        creative.resulting_state,
        creative.required_text,
        creative.prop_text,
        creative.on_screen_text,
    )
    return any(
        _scene_shard_exact_source_text(value) == normalized_source
        for value in content_values
        if str(value or "").strip()
    )


def _scene_shard_filter_distinct_source_ownership_duplication(
    review: ScreenplaySceneShardSemanticReview,
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardSemanticReview:
    """Falsify cross_slot_duplication when both slots own distinct frozen source.

    Frozen source ownership is the single authority: every slot carries exactly
    one ``source_unit_key`` whose ``SourceFact`` text is the only thing it may
    reproduce. When a ``cross_slot_duplication`` finding pairs slot A with slot B
    but each owns a *different* ``source_unit_key`` whose normalized source text
    also differs, the two units are structurally distinct source units, so the
    kind cannot hold on ownership grounds. This complements
    ``_scene_shard_creative_has_exact_source_support`` (which requires the
    creative to equal its *own* source); here the decision rests on whether the
    two slots draw from different frozen source, not on whether either creative
    was expanded.

    A genuine cross-slot borrow (one slot verbatim reproducing the *other*
    slot's frozen source instead of its own) still owns a distinct source unit
    on paper, so ownership alone cannot separate it from a false positive. The
    finding is therefore only cleared when neither slot's creative verbatim
    carries the other slot's frozen source; any such borrow keeps the finding
    intact. If either slot cannot be resolved to a ``SourceFact`` the finding is
    conservatively preserved.
    """
    source_facts_by_slot = _scene_shard_source_facts_by_slot(
        scene_input_contracts
    )
    filtered: list[ScreenplaySceneShardSemanticFinding] = []
    for finding in review.findings:
        if "cross_slot_duplication" not in finding.violation_kinds:
            filtered.append(finding)
            continue
        related_unit_key = finding.related_unit_keys[0]
        target_creative = draft.slots.get(finding.unit_key)
        related_creative = draft.slots.get(related_unit_key)
        target_source_fact = source_facts_by_slot.get(finding.unit_key)
        related_source_fact = source_facts_by_slot.get(related_unit_key)
        if (
            target_creative is None
            or related_creative is None
            or target_source_fact is None
            or related_source_fact is None
        ):
            filtered.append(finding)
            continue
        target_source_text = _scene_shard_exact_source_text(
            target_source_fact.text
        )
        related_source_text = _scene_shard_exact_source_text(
            related_source_fact.text
        )
        distinct_source_ownership = (
            target_source_fact.source_unit_key
            != related_source_fact.source_unit_key
            and bool(target_source_text)
            and bool(related_source_text)
            and target_source_text != related_source_text
        )
        cross_slot_borrowing = (
            _scene_shard_creative_reproduces_source_text(
                target_creative,
                related_source_text,
            )
            or _scene_shard_creative_reproduces_source_text(
                related_creative,
                target_source_text,
            )
        )
        if not distinct_source_ownership or cross_slot_borrowing:
            filtered.append(finding)
            continue
        remaining_kinds = [
            kind
            for kind in finding.violation_kinds
            if kind != "cross_slot_duplication"
        ]
        if remaining_kinds:
            filtered.append(
                ScreenplaySceneShardSemanticFinding(
                    unit_key=finding.unit_key,
                    related_unit_keys=[],
                    code=finding.code,
                    violation_kinds=remaining_kinds,
                    message=finding.message,
                )
            )
    return ScreenplaySceneShardSemanticReview(findings=filtered)
