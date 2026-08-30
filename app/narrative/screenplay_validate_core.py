"""Header/version/scope, forbidden-environment-entity, presence/coverage
and source-evidence-span validation phases of validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
"""
from __future__ import annotations

from typing import Any

from app.schemas import (
    EpisodeScreenplay,
    NARRATIVE_CONTRACT_VERSION,
    is_system_environment_entity_id,
)

from .plan_index import action_participant_delivery_errors
from .primitives import _norm, _require_refs, normalize_source_evidence_text


def _validate_narrative_plan_version_and_scope(
    plan: Any,
    errors: list[str],
    expected_scope_id: str | None,
) -> None:
    """Check narrative_plan.contract_version and scope_id against the caller's expectations."""
    if plan.contract_version != NARRATIVE_CONTRACT_VERSION:
        errors.append(
            f"[NARRATIVE_VERSION_INVALID] contract_version={plan.contract_version}，"
            f"当前要求 {NARRATIVE_CONTRACT_VERSION}"
        )
    if not _norm(plan.scope_id):
        errors.append("[NARRATIVE_SCOPE_MISSING] narrative_plan.scope_id 不能为空")
    if expected_scope_id is not None and plan.scope_id != str(expected_scope_id):
        errors.append(
            f"[NARRATIVE_SCOPE_MISMATCH] narrative_plan.scope_id={plan.scope_id} "
            f"不等于当前权威作用域 {expected_scope_id}"
        )


def _validate_forbidden_environment_entities(
    screenplay: EpisodeScreenplay,
    errors: list[str],
) -> None:
    """Flag any use of the reserved system-environment entity id as a speaker/character."""
    forbidden_environment_uses: list[tuple[str, str]] = []
    forbidden_environment_uses.extend(
        (f"voice_bible[{position}].speaker_id", voice.speaker_id)
        for position, voice in enumerate(screenplay.voice_bible)
        if is_system_environment_entity_id(voice.speaker_id)
    )
    forbidden_environment_uses.extend(
        (
            f"dialogue_chains[{chain_position}].turns[{turn_position}].speaker",
            turn.speaker,
        )
        for chain_position, chain in enumerate(screenplay.dialogue_chains)
        for turn_position, turn in enumerate(chain.turns)
        if is_system_environment_entity_id(turn.speaker)
    )
    forbidden_environment_uses.extend(
        (f"information_ledger[{position}].speaker_id", item.speaker_id or "")
        for position, item in enumerate(screenplay.information_ledger)
        if is_system_environment_entity_id(item.speaker_id)
    )
    forbidden_environment_uses.extend(
        (f"scene_outline[{position}].characters", character)
        for position, scene in enumerate(screenplay.scene_outline)
        for character in scene.characters
        if is_system_environment_entity_id(character)
    )
    for path, entity_id in forbidden_environment_uses:
        errors.append(
            f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {path} 不得使用系统环境实体 "
            f"{entity_id}"
        )


def _validate_narrative_presence_and_coverage(
    screenplay: EpisodeScreenplay,
    plan: Any,
    index: Any,
    errors: list[str],
) -> None:
    """Check minimum-cardinality requirements and scene_contracts/scene_outline coverage."""
    errors.extend(action_participant_delivery_errors(screenplay))
    if not index.source_evidence:
        errors.append("[SOURCE_EVIDENCE_MISSING] 至少需要一条逐字来源证据")
    if not index.propositions:
        errors.append("[PROPOSITION_MISSING] 至少需要一条叙事命题")
    if not index.events:
        errors.append("[NARRATIVE_EVENT_MISSING] 至少需要一个造成状态变化的叙事事件")
    if len(index.priors) < 2:
        errors.append("[AUDIENCE_PRIOR_INSUFFICIENT] 关键叙事至少需要两个不同观看前提的观众先验，不能使用平均观众")
    if not index.intents:
        errors.append("[EXPERIENCE_INTENT_MISSING] 缺少观众体验意图与逐先验状态路径")
    if not index.scenes:
        errors.append("[SCENE_CONTRACT_MISSING] 至少需要一个场景戏剧合同；非传统场景应显式声明替代功能")
    expected_scene_count = len([
        scene
        for scene in screenplay.scene_outline
        if int(scene.scene_no or 0) > 0
    ])
    actual_scene_ids = [
        _norm(scene.scene_id)
        for scene in plan.scene_contracts
    ]
    if (
        expected_scene_count
        and (
            len(actual_scene_ids) != expected_scene_count
            or len(set(actual_scene_ids)) != len(actual_scene_ids)
        )
    ):
        errors.append(
            "[SCENE_CONTRACT_COVERAGE_MISMATCH] "
            "narrative_plan.scene_contracts 必须与 scene_outline 逐场一一对应；"
            f"期望 {expected_scene_count} 个唯一合同，当前 {actual_scene_ids}"
        )
    episode_arcs = [arc for arc in index.arcs.values() if arc.scope == "episode"]
    if len(episode_arcs) != 1:
        errors.append("[EPISODE_ARC_CONTRACT_INVALID] 每集必须恰有一个 scope=episode 的整集戏剧合同")
    _require_refs(plan.initial_state_fact_ids, index.facts, errors, "initial_state_fact_ids")


def _validate_source_evidence_spans(
    index: Any,
    errors: list[str],
    source_text: str | None,
    authorized_source_chapter_ids: Any,
    authorized_source_chapters: dict[str, str] | None,
) -> None:
    """Check each source_evidence entry's chapter_id/span/excerpt against the authorized source."""
    raw_source = source_text or ""
    authorized_chapter_ids = (
        {
            _norm(value)
            for value in authorized_source_chapter_ids
            if _norm(value)
        }
        if authorized_source_chapter_ids is not None
        else None
    )
    chapter_text_by_id = (
        {
            _norm(chapter_id): str(text)
            for chapter_id, text in authorized_source_chapters.items()
            if _norm(chapter_id)
        }
        if authorized_source_chapters is not None
        else None
    )
    if chapter_text_by_id is not None:
        authorized_chapter_ids = set(chapter_text_by_id)
    for evidence_id, evidence in index.source_evidence.items():
        excerpt = normalize_source_evidence_text(evidence.verbatim_excerpt)
        span = evidence.source_span
        if not _norm(span.chapter_id):
            errors.append(f"[SOURCE_SPAN_CHAPTER_MISSING] {evidence_id}.source_span.chapter_id 不能为空")
        elif (
            authorized_chapter_ids is not None
            and _norm(span.chapter_id) not in authorized_chapter_ids
        ):
            errors.append(
                f"[SOURCE_SPAN_CHAPTER_OUT_OF_SCOPE] {evidence_id}.source_span.chapter_id="
                f"{span.chapter_id} 不属于当前剧集授权章节"
            )
        if span.start < 0 or span.end <= span.start:
            errors.append(f"[SOURCE_SPAN_INVALID] {evidence_id}.source_span 必须满足 0 <= start < end")
        if not excerpt:
            errors.append(f"[SOURCE_EVIDENCE_EMPTY] {evidence_id}.verbatim_excerpt 不能为空")
        elif chapter_text_by_id is not None and _norm(span.chapter_id) in chapter_text_by_id:
            chapter_text = chapter_text_by_id[_norm(span.chapter_id)]
            if span.end > len(chapter_text):
                errors.append(
                    f"[SOURCE_SPAN_OUT_OF_RANGE] {evidence_id}.source_span.end "
                    "超出对应授权章节正文"
                )
            else:
                exact_slice = normalize_source_evidence_text(
                    chapter_text[span.start:span.end]
                )
                if exact_slice != excerpt:
                    errors.append(
                        f"[SOURCE_SPAN_EXACT_MISMATCH] {evidence_id} 的章节内 "
                        "start/end 切片与逐字摘录不一致"
                    )
        elif raw_source:
            if span.end > len(raw_source):
                errors.append(f"[SOURCE_SPAN_OUT_OF_RANGE] {evidence_id}.source_span.end 超出授权原文")
            else:
                exact_slice = normalize_source_evidence_text(
                    raw_source[span.start:span.end]
                )
                if exact_slice != excerpt:
                    errors.append(
                        f"[SOURCE_SPAN_EXACT_MISMATCH] {evidence_id} 的 start/end 切片与逐字摘录不一致"
                    )
        if not 0 <= evidence.confidence <= 1:
            errors.append(f"[CONFIDENCE_RANGE] {evidence_id}.confidence 必须在 0..1")

