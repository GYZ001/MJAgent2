"""Screenplay narrative-graph hard gate.

Moved verbatim out of the pre-split ``app/narrative.py`` (see
``app/narrative/__init__.py`` for the package-split rationale). This file
holds exactly one function -- ``validate_screenplay_narrative`` is a single
~1,575-line function in the pre-split source; splitting it further would
change its control flow, so it is moved whole (see the ``function_lines``
baseline entry for this file in ``app/FILE_CONVENTIONS.toml``). Add new
screenplay-narrative validation logic here, not back into ``app/narrative.py``.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from app import config
from app.schemas import (
    EpisodeScreenplay,
    NARRATIVE_CONTRACT_VERSION,
    is_system_environment_entity_id,
    system_environment_entity_id,
)

from .plan_index import action_participant_delivery_errors, index_narrative_plan
from .primitives import (
    _anchor_ref_errors,
    _changed_audience_state_fields,
    _curve_errors,
    _cycle_nodes,
    _norm,
    _require_refs,
    _state_without_identity,
    _target_state_fragment_matches,
    normalize_source_evidence_text,
)


def validate_screenplay_narrative(
    screenplay: EpisodeScreenplay,
    *,
    require: bool = False,
    source_text: str | None = None,
    expected_scope_id: str | None = None,
    authorized_source_chapter_ids: Iterable[str | int] | None = None,
    authorized_source_chapters: dict[str, str] | None = None,
) -> list[str]:
    """Validate the one authoritative screenplay narrative graph.

    Legacy artifacts remain parseable when ``require`` is false.  Every new
    generation/publish path calls this with ``require=True``.
    """
    plan = screenplay.narrative_plan
    if plan is None:
        return (["[NARRATIVE_PLAN_MISSING] narrative_plan 缺失；旧稿可读取，但新生成/发布必须重建叙事合同"]
                if require else [])
    errors: list[str] = []
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
    index = index_narrative_plan(plan, errors)
    environment_entity_id = system_environment_entity_id(plan.scope_id)
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

    adapted_ids: set[str] = set()
    declared_entity_ids = {
        _norm(entity_id)
        for proposition in index.propositions.values()
        for entity_id in proposition.entity_ids
        if _norm(entity_id)
    }
    reserved_environment_ids = {
        entity_id
        for entity_id in declared_entity_ids
        if is_system_environment_entity_id(entity_id)
    }
    foreign_environment_ids = reserved_environment_ids - {environment_entity_id}
    if foreign_environment_ids:
        errors.append(
            "[SYSTEM_NARRATIVE_ENTITY_SCOPE_MISMATCH] 命题身份图包含其他作用域的"
            f"系统环境实体 {sorted(foreign_environment_ids)}"
        )
    identity_display_names: dict[str, str] = {}
    for identity_id, identity in index.identities.items():
        reserved_identity_tokens = {
            token
            for token in [
                identity_id,
                identity.display_name,
                *identity.voice_ids,
            ]
            if is_system_environment_entity_id(token)
        }
        if reserved_identity_tokens:
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {identity_id} 把系统环境实体 "
                f"{sorted(reserved_identity_tokens)} 注册为人物/声音身份"
            )
        display_name = _norm(identity.display_name)
        if display_name in identity_display_names:
            errors.append(
                f"[IDENTITY_DISPLAY_NAME_AMBIGUOUS] display_name={display_name} 同时指向 "
                f"{identity_display_names[display_name]} 与 {identity_id}"
            )
        else:
            identity_display_names[display_name] = identity_id
        evidence = identity.evidence
        _require_refs(
            evidence.source_evidence_ids,
            index.source_evidence,
            errors,
            f"{identity_id}.evidence.source_evidence_ids",
        )
        _require_refs(
            evidence.proposition_ids,
            index.propositions,
            errors,
            f"{identity_id}.evidence.proposition_ids",
        )
        _require_refs(
            evidence.adaptation_decision_ids,
            index.decisions,
            errors,
            f"{identity_id}.evidence.adaptation_decision_ids",
        )
        if not any((
            evidence.source_evidence_ids,
            evidence.proposition_ids,
            evidence.adaptation_decision_ids,
        )):
            errors.append(
                f"[IDENTITY_EVIDENCE_MISSING] {identity_id} 缺少可追溯的 evidence ID"
            )
        if not _norm(evidence.rationale):
            errors.append(
                f"[IDENTITY_RATIONALE_MISSING] {identity_id} 缺少身份意图判定 rationale"
            )
        linked_graph_ids = {identity_id, *identity.voice_ids}
        if (
            not linked_graph_ids.intersection(declared_entity_ids)
            and not evidence.proposition_ids
            and not identity.voice_ids
        ):
            errors.append(
                f"[IDENTITY_GRAPH_LINK_MISSING] {identity_id} 既未进入命题身份图，"
                "也未通过 evidence.proposition_ids/voice_ids 绑定叙事用途"
            )
    proposition_identity_owners: dict[tuple[str, str], str] = {}
    proposition_statement_owners: dict[tuple[str, str], str] = {}
    for proposition_id, proposition in index.propositions.items():
        semantic_identity_key = _norm(proposition.semantic_identity_key)
        if not semantic_identity_key:
            errors.append(
                f"[PROPOSITION_SEMANTIC_IDENTITY_MISSING] "
                f"{proposition_id}.semantic_identity_key 不能为空"
            )
        if not _norm(proposition.canonical_statement):
            errors.append(f"[PROPOSITION_STATEMENT_MISSING] {proposition_id}.canonical_statement 不能为空")
        if proposition.narrative_domain not in {"source_canon", "adapted_story"}:
            errors.append(f"[PROPOSITION_DOMAIN_INVALID] {proposition_id}.narrative_domain 非法")
        if semantic_identity_key and proposition.narrative_domain in {
            "source_canon", "adapted_story",
        }:
            identity = (proposition.narrative_domain, semantic_identity_key)
            previous = proposition_identity_owners.get(identity)
            if previous:
                errors.append(
                    f"[PROPOSITION_SEMANTIC_IDENTITY_DUPLICATE] 同一叙事域内 "
                    f"{previous}/{proposition_id} 共用了语义身份 {semantic_identity_key}；"
                    "同一命题只能保留一个 proposition_id"
                )
            else:
                proposition_identity_owners[identity] = proposition_id
            statement_identity = (
                proposition.narrative_domain,
                _norm(proposition.canonical_statement).casefold(),
            )
            previous_statement = proposition_statement_owners.get(statement_identity)
            if previous_statement:
                errors.append(
                    f"[PROPOSITION_CANONICAL_DUPLICATE] 同一叙事域内 "
                    f"{previous_statement}/{proposition_id} 的 canonical_statement 完全相同；"
                    "不得用不同 semantic_identity_key 绕过命题归一"
                )
            else:
                proposition_statement_owners[statement_identity] = proposition_id
        if proposition.domain_truth_status not in {"true", "false", "undetermined", "not_applicable"}:
            errors.append(f"[PROPOSITION_TRUTH_STATUS_INVALID] {proposition_id}.domain_truth_status 非法")
        _require_refs(proposition.direct_source_evidence_ids, index.source_evidence, errors, proposition_id)
        if proposition.narrative_domain == "source_canon" and not proposition.direct_source_evidence_ids:
            errors.append(f"[SOURCE_PROPOSITION_UNGROUNDED] {proposition_id} 缺少直接来源证据")
        if proposition.narrative_domain == "adapted_story":
            adapted_ids.add(proposition_id)
            if proposition.direct_source_evidence_ids:
                errors.append(
                    f"[ADAPTED_PROPOSITION_DIRECT_SOURCE] {proposition_id} 属于 adapted_story，"
                    "不得把原文证据直接当作改写后真值的证明"
                )
        normalized_entities = [_norm(value) for value in proposition.entity_ids]
        if any(not value for value in normalized_entities) or len(set(normalized_entities)) != len(normalized_entities):
            errors.append(f"[PROPOSITION_ENTITY_ID_INVALID] {proposition_id}.entity_ids 含空值或重复身份")

    decided_adapted: set[str] = set()
    adaptation_relations = {
        "preserve", "condense", "split", "combine", "transform", "omit", "invent", "other",
    }
    for decision_id, decision in index.decisions.items():
        _require_refs(decision.source_proposition_ids, index.propositions, errors, decision_id)
        _require_refs(decision.adapted_proposition_ids, index.propositions, errors, decision_id)
        for proposition_id in decision.source_proposition_ids:
            proposition = index.propositions.get(proposition_id)
            if proposition and proposition.narrative_domain != "source_canon":
                errors.append(f"[ADAPTATION_SOURCE_DOMAIN] {decision_id} 的来源 {proposition_id} 不是 source_canon")
        for proposition_id in decision.adapted_proposition_ids:
            proposition = index.propositions.get(proposition_id)
            if proposition and proposition.narrative_domain != "adapted_story":
                errors.append(f"[ADAPTATION_TARGET_DOMAIN] {decision_id} 的结果 {proposition_id} 不是 adapted_story")
            decided_adapted.add(proposition_id)
        if decision.relation == "other" and not _norm(decision.custom_relation):
            errors.append(f"[ADAPTATION_CUSTOM_RELATION_MISSING] {decision_id} relation=other 时必须说明语义关系")
        if decision.relation not in adaptation_relations:
            errors.append(f"[ADAPTATION_RELATION_INVALID] {decision_id}.relation 非法；未预设关系必须用 other + custom_relation")
        if not _norm(decision.creative_reason):
            errors.append(f"[ADAPTATION_REASON_MISSING] {decision_id} 缺少改编理由")
        _require_refs(
            decision.protected_causal_effect_ids,
            set(index.propositions) | set(index.events),
            errors,
            f"{decision_id}.protected_causal_effect_ids",
        )
        _require_refs(decision.affected_event_ids, index.events, errors, f"{decision_id}.affected_event_ids")
    for proposition_id in sorted(adapted_ids - decided_adapted):
        errors.append(f"[ADAPTED_PROPOSITION_UNDECLARED] {proposition_id} 没有 AdaptationDecision")

    for fact_id, fact in index.facts.items():
        _require_refs([fact.proposition_id], index.propositions, errors, fact_id)
        if _norm(fact.subject_id) not in declared_entity_ids:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {fact_id}.subject_id={fact.subject_id} 未在命题身份图中声明")
        if is_system_environment_entity_id(fact.subject_id):
            if fact.subject_id != environment_entity_id:
                errors.append(
                    f"[SYSTEM_NARRATIVE_ENTITY_SCOPE_MISMATCH] {fact_id}.subject_id="
                    f"{fact.subject_id} 不属于当前作用域"
                )
            proposition = index.propositions.get(fact.proposition_id)
            if (
                proposition is not None
                and fact.subject_id not in proposition.entity_ids
            ):
                errors.append(
                    f"[SYSTEM_NARRATIVE_ENTITY_PROPOSITION_MISSING] {fact_id} 的"
                    f"系统环境主体未由命题 {fact.proposition_id}.entity_ids 声明"
                )
        if not _norm(fact.predicate_id):
            errors.append(f"[STATE_PREDICATE_MISSING] {fact_id}.predicate_id 不能为空")
        if fact.provenance not in {"source", "screenplay", "storyboard"}:
            errors.append(f"[STATE_PROVENANCE_INVALID] {fact_id}.provenance 非法")
        if not _norm(fact.time_scope):
            errors.append(f"[STATE_TIME_SCOPE_MISSING] {fact_id}.time_scope 不能为空")
        if not 0 <= fact.confidence <= 1:
            errors.append(f"[CONFIDENCE_RANGE] {fact_id}.confidence 必须在 0..1")

    for question_id, question in index.questions.items():
        if not _norm(question.question_text):
            errors.append(f"[DRAMATIC_QUESTION_TEXT_MISSING] {question_id}.question_text 不能为空")
        _require_refs(question.target_proposition_ids, index.propositions, errors, question_id)
        _anchor_ref_errors(
            question.open_anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{question_id}.open_anchor",
        )
        if question.resolution_anchor is not None:
            _anchor_ref_errors(
                question.resolution_anchor,
                events=index.events,
                scenes=index.scenes,
                errors=errors,
                subject=f"{question_id}.resolution_anchor",
            )
        if question.status not in {"open", "resolved", "carried"}:
            errors.append(f"[DRAMATIC_QUESTION_STATUS_INVALID] {question_id}.status 非法")
        if question.status == "resolved" and question.resolution_anchor is None:
            errors.append(f"[DRAMATIC_QUESTION_RESOLUTION_MISSING] {question_id} 已 resolved 但没有 resolution_anchor")

    event_or_scene_ids = set(index.events) | set(index.scenes)
    for evidence_id, evidence in index.evidence.items():
        _require_refs(evidence.supports_proposition_ids, index.propositions, errors, evidence_id)
        if evidence.anchor.type in {"event", "scene"}:
            _require_refs([evidence.anchor.id], event_or_scene_ids, errors, f"{evidence_id}.anchor")
        if not evidence.perceivable_by:
            errors.append(f"[EVIDENCE_AUDIENCE_MISSING] {evidence_id}.perceivable_by 不能为空")
        undeclared_perceivers = {
            entity_id
            for entity_id in evidence.perceivable_by
            if entity_id != "audience" and entity_id not in declared_entity_ids
        }
        if undeclared_perceivers:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {evidence_id}.perceivable_by 含未声明身份 {sorted(undeclared_perceivers)}")
        environment_perceivers = {
            entity_id
            for entity_id in evidence.perceivable_by
            if is_system_environment_entity_id(entity_id)
        }
        if environment_perceivers:
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {evidence_id}.perceivable_by "
                f"把系统环境实体当作感知者 {sorted(environment_perceivers)}"
            )
        if not _norm(evidence.observable_claim):
            errors.append(f"[EVIDENCE_CLAIM_MISSING] {evidence_id}.observable_claim 不能为空")
        _anchor_ref_errors(
            evidence.anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{evidence_id}.anchor",
        )
        if not 0 <= evidence.planned_salience <= 1:
            errors.append(f"[SALIENCE_RANGE] {evidence_id}.planned_salience 必须在 0..1")
        if evidence.planned_duration_s is not None and evidence.planned_duration_s < 0:
            errors.append(f"[EVIDENCE_DURATION_INVALID] {evidence_id}.planned_duration_s 不能为负")

    action_event_owner: dict[str, str] = {}
    fact_producer: dict[str, str] = {}
    offscreen_only_identity_ids = {
        contract.identity_id
        for contract in plan.identity_contracts
        if contract.visual_policy == "offscreen_only"
    }
    event_order = {event_id: position for position, event_id in enumerate(index.events)}
    parents: dict[str, list[str]] = {}
    for event_id, event in index.events.items():
        parents[event_id] = list(event.causal_parent_ids)
        _require_refs(event.proposition_ids, index.propositions, errors, event_id)
        _require_refs(event.causal_parent_ids, index.events, errors, event_id)
        _require_refs(event.precondition_fact_ids, index.facts, errors, event_id)
        _require_refs(event.action_ids, index.actions, errors, event_id)
        undeclared_onscreen = {
            entity_id
            for entity_id in event.onscreen_entity_ids
            if entity_id not in declared_entity_ids
        }
        if undeclared_onscreen:
            errors.append(
                f"[NARRATIVE_ENTITY_UNDECLARED] {event_id}.onscreen_entity_ids "
                f"含未声明身份 {sorted(undeclared_onscreen)}"
            )
        environment_onscreen = {
            entity_id
            for entity_id in event.onscreen_entity_ids
            if is_system_environment_entity_id(entity_id)
        }
        if environment_onscreen:
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {event_id}.onscreen_entity_ids "
                f"把系统环境实体当作可见人物 {sorted(environment_onscreen)}"
            )
        invalid_onscreen = (
            set(event.onscreen_entity_ids) & offscreen_only_identity_ids
        )
        if invalid_onscreen:
            errors.append(
                f"[EVENT_ONSCREEN_POLICY_INVALID] {event_id}.onscreen_entity_ids "
                f"含仅允许画外的身份 {sorted(invalid_onscreen)}"
            )
        _require_refs(
            event.downstream_dependency_event_ids,
            index.events,
            errors,
            f"{event_id}.downstream_dependency_event_ids",
        )
        _require_refs([*event.effects_add, *event.effects_remove], index.facts, errors, event_id)
        if event.delivery_scope_id != plan.scope_id:
            errors.append(
                f"[EVENT_SCOPE_MISMATCH] {event_id}.delivery_scope_id={event.delivery_scope_id} "
                f"不属于当前叙事作用域 {plan.scope_id}"
            )
        if set(event.effects_add).intersection(event.effects_remove):
            errors.append(f"[EVENT_EFFECT_CONFLICT] {event_id} 同时增加和删除同一状态事实")
        fact_proposition_ids = {
            index.facts[fact_id].proposition_id
            for fact_id in (
                *event.precondition_fact_ids,
                *event.effects_add,
                *event.effects_remove,
            )
            if fact_id in index.facts
        }
        if not fact_proposition_ids.issubset(event.proposition_ids):
            errors.append(
                f"[EVENT_FACT_PROPOSITION_MISMATCH] {event_id}.proposition_ids 未覆盖其前置/效果事实的命题 "
                f"{sorted(fact_proposition_ids - set(event.proposition_ids))}"
            )
        if not event.effects_add and not event.effects_remove and not event.proposition_ids:
            errors.append(f"[EVENT_NO_DELTA] {event_id} 没有事实、命题或认知变化")
        for parent_id in event.causal_parent_ids:
            if parent_id in event_order and event_order[parent_id] >= event_order[event_id]:
                errors.append(f"[EVENT_CAUSAL_ORDER] {event_id} 的原因 {parent_id} 未先于结果出现")
        for downstream_id in event.downstream_dependency_event_ids:
            if downstream_id in event_order and event_order[downstream_id] <= event_order[event_id]:
                errors.append(f"[EVENT_DOWNSTREAM_ORDER] {event_id} 的下游 {downstream_id} 没有位于其后")
        for action_id in event.action_ids:
            previous = action_event_owner.get(action_id)
            if previous and previous != event_id:
                errors.append(f"[ACTION_EVENT_OWNER_DUPLICATE] {action_id} 同时被 {previous}/{event_id} 作为事件主动作")
            action_event_owner[action_id] = event_id
        for fact_id in event.effects_add:
            previous = fact_producer.get(fact_id)
            if previous and previous != event_id:
                errors.append(f"[FACT_PRODUCER_DUPLICATE] {fact_id} 被 {previous}/{event_id} 重复创建")
            fact_producer[fact_id] = event_id
        if event.delivery_policy not in {"deliver", "withhold", "carry"}:
            errors.append(f"[EVENT_DELIVERY_POLICY_INVALID] {event_id}.delivery_policy 非法")
        if event.delivery_policy == "deliver" and event.must_keep:
            window_id = _norm(event.primary_delivery_window_id)
            window = index.windows.get(window_id)
            if not window_id:
                errors.append(f"[EVENT_PRIMARY_WINDOW_MISSING] {event_id} 是本作用域必交付事件但没有主要窗口")
            elif window is None:
                errors.append(f"[NARRATIVE_REF_MISSING] {event_id}.primary_delivery_window_id 引用了不存在的 {window_id}")
            elif event_id not in window.event_ids:
                errors.append(f"[EVENT_PRIMARY_WINDOW_MISMATCH] {window_id} 没有声明主要交付事件 {event_id}")
        if not 0 <= event.salience <= 1 or not 0 <= event.irreversibility <= 1:
            errors.append(f"[EVENT_IMPORTANCE_RANGE] {event_id}.salience/irreversibility 必须在 0..1")
    cycle = _cycle_nodes(parents)
    if cycle:
        errors.append("[EVENT_DAG_CYCLE] 事件因果图存在环：" + " -> ".join(cycle))
    causal_parent_ids = {
        parent_id for event in index.events.values() for parent_id in event.causal_parent_ids
    }
    consumed_fact_ids = {
        fact_id for event in index.events.values() for fact_id in event.precondition_fact_ids
    }
    decisions_by_event: defaultdict[str, list[Any]] = defaultdict(list)
    for decision in index.decisions.values():
        for event_id in decision.affected_event_ids:
            decisions_by_event[event_id].append(decision)
    for event_id, event in index.events.items():
        causally_required = bool(
            event.downstream_dependency_event_ids
            or event_id in causal_parent_ids
            or set(event.effects_add).intersection(consumed_fact_ids)
        )
        if causally_required and not event.must_keep:
            errors.append(
                f"[CAUSAL_EVENT_MUST_KEEP_DOWNGRADED] {event_id} 仍是后续事件的因果前置，"
                "不得在未重写因果图前改为 must_keep=false"
            )
        if not event.must_keep:
            preserve_decisions = [
                decision for decision in decisions_by_event.get(event_id, [])
                if decision.relation in {"preserve", "split"}
            ]
            if preserve_decisions:
                errors.append(
                    f"[PRESERVED_EVENT_MUST_KEEP_DOWNGRADED] {event_id} 由保留/拆分决策产生，"
                    "不得不经新的省略/变换决策直接改为 must_keep=false"
                )
    for event_id, event in index.events.items():
        for fact_id in event.precondition_fact_ids:
            producer = fact_producer.get(fact_id)
            if producer and event_order.get(producer, -1) >= event_order[event_id]:
                errors.append(f"[EVENT_PRECONDITION_FROM_FUTURE] {event_id} 依赖由未来事件 {producer} 才产生的 {fact_id}")

    initial_facts = set(plan.initial_state_fact_ids)
    produced_facts = {
        *fact_producer,
        *(
            fact_id
            for action in index.actions.values()
            for fact_id in action.effects_add
        ),
    }
    if initial_facts & produced_facts:
        errors.append(
            f"[INITIAL_FACT_HAS_PRODUCER] 初始事实不得同时由本作用域事件产生："
            f"{sorted(initial_facts & produced_facts)}"
        )
    unintroduced_facts = set(index.facts) - initial_facts - produced_facts
    if unintroduced_facts:
        errors.append(
            f"[STATE_FACT_INTRODUCTION_MISSING] 以下事实既非显式初始态也无唯一产生事件："
            f"{sorted(unintroduced_facts)}"
        )

    # Simulate only from the explicitly audited initial facts.  Every later
    # transition must consume an available precondition and cannot silently
    # recreate/remove a fact.
    active_facts = set(initial_facts)
    for event_id, event in index.events.items():
        missing_preconditions = set(event.precondition_fact_ids) - active_facts
        if missing_preconditions:
            errors.append(
                f"[EVENT_PRECONDITION_UNAVAILABLE] {event_id} 的前置事实尚未成立："
                f"{sorted(missing_preconditions)}"
            )
        missing_removals = set(event.effects_remove) - active_facts
        if missing_removals:
            errors.append(
                f"[STATE_REGRESSION] {event_id} 试图移除当前并未成立的事实："
                f"{sorted(missing_removals)}"
            )
        repeated_adds = set(event.effects_add) & active_facts
        if repeated_adds:
            errors.append(
                f"[STATE_REPLAY_WITHOUT_DELTA] {event_id} 再次建立已成立事实："
                f"{sorted(repeated_adds)}"
            )
        active_facts.difference_update(event.effects_remove)
        active_facts.update(event.effects_add)

    for action_id, action in index.actions.items():
        _require_refs(action.precondition_fact_ids, index.facts, errors, action_id)
        _require_refs([*action.effects_add, *action.effects_remove], index.facts, errors, action_id)
        if not _norm(action.semantic_intent) or not _norm(action.completion_condition):
            errors.append(f"[ACTION_SEMANTICS_MISSING] {action_id} 缺少语义意图或可观察完成条件")
        if set(action.effects_add).intersection(action.effects_remove):
            errors.append(f"[ACTION_EFFECT_CONFLICT] {action_id} 同时增加和删除同一状态事实")
        if (
            not action.actor_ids
            and not action.target_ids
            and action.action_agency.identity_bearing
        ):
            errors.append(f"[ACTION_PARTICIPANT_MISSING] {action_id} 没有主体或作用目标")
        undeclared_participants = (
            set(action.actor_ids) | set(action.target_ids)
        ) - declared_entity_ids
        if undeclared_participants:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {action_id} 含未声明动作参与者 {sorted(undeclared_participants)}")
        environment_participants = {
            entity_id
            for entity_id in [*action.actor_ids, *action.target_ids]
            if is_system_environment_entity_id(entity_id)
        }
        if environment_participants:
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {action_id} 把系统环境实体"
                f"当作动作人物 {sorted(environment_participants)}"
            )
        if action.decision_requirement not in {"applies", "not_applicable"}:
            errors.append(f"[ACTION_DECISION_REQUIREMENT_INVALID] {action_id}.decision_requirement 非法")
        if (
            action.decision_requirement == "not_applicable"
            and not _norm(action.decision_not_applicable_reason)
        ):
            errors.append(f"[ACTION_DECISION_ALTERNATIVE_MISSING] {action_id} 不需要人物决策链时必须说明因果依据")
        phase_ids: set[str] = set()
        for phase in action.temporal_phases:
            if not _norm(phase.phase_id) or phase.phase_id in phase_ids:
                errors.append(f"[ACTION_PHASE_ID_INVALID] {action_id} 的阶段 ID 为空或重复")
            phase_ids.add(phase.phase_id)
            if phase.estimated_min_s < 0:
                errors.append(f"[ACTION_PHASE_DURATION_INVALID] {phase.phase_id}.estimated_min_s 不能为负")
            if not _norm(phase.start_condition) or not _norm(phase.end_condition):
                errors.append(f"[ACTION_PHASE_BOUNDARY_MISSING] {phase.phase_id} 缺少开始或结束条件")
        for boundary_id in action.splittable_boundaries:
            if boundary_id not in phase_ids:
                errors.append(f"[ACTION_SPLIT_BOUNDARY_MISSING] {action_id} 引用了不存在阶段 {boundary_id}")

    for event_id, event in index.events.items():
        bound_actions = [
            index.actions[action_id]
            for action_id in event.action_ids
            if action_id in index.actions
        ]
        action_adds = {
            fact_id
            for action in bound_actions
            for fact_id in action.effects_add
        }
        action_removes = {
            fact_id
            for action in bound_actions
            for fact_id in action.effects_remove
        }
        for action_id in event.action_ids:
            action = index.actions.get(action_id)
            if action is None:
                continue
            external_preconditions = (
                set(action.precondition_fact_ids) - action_adds
            )
            net_adds = set(action.effects_add) - action_removes
            net_removes = set(action.effects_remove) - action_adds
            if not external_preconditions.issubset(event.precondition_fact_ids):
                errors.append(f"[ACTION_EVENT_PRECONDITION_MISMATCH] {event_id} 未承接 {action_id} 的全部前置事实")
            if not net_adds.issubset(event.effects_add):
                errors.append(f"[ACTION_EVENT_EFFECT_MISMATCH] {event_id} 未承接 {action_id} 的新增事实")
            if not net_removes.issubset(event.effects_remove):
                errors.append(f"[ACTION_EVENT_EFFECT_MISMATCH] {event_id} 未承接 {action_id} 的移除事实")

    structurally_equivalent_pairs: set[frozenset[str]] = set()
    action_signatures: defaultdict[tuple[Any, ...], list[str]] = defaultdict(list)
    for action_id, action in index.actions.items():
        signature = (
            tuple(sorted(action.actor_ids)),
            tuple(sorted(action.target_ids)),
            tuple(sorted(action.precondition_fact_ids)),
            tuple(sorted(action.effects_add)),
            tuple(sorted(action.effects_remove)),
            _norm(action.completion_condition),
        )
        for previous_action_id in action_signatures[signature]:
            structurally_equivalent_pairs.add(
                frozenset((previous_action_id, action_id)),
            )
        action_signatures[signature].append(action_id)

    def _event_depends_on(descendant_id: str, ancestor_id: str) -> bool:
        pending = list(index.events.get(descendant_id).causal_parent_ids) if descendant_id in index.events else []
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == ancestor_id:
                return True
            if current in visited or current not in index.events:
                continue
            visited.add(current)
            pending.extend(index.events[current].causal_parent_ids)
        return False

    audited_pairs: set[frozenset[str]] = set()
    for audit_id, audit in index.action_audits.items():
        action_pair = [_norm(value) for value in audit.action_ids]
        _require_refs(action_pair, index.actions, errors, audit_id)
        if len(action_pair) != 2 or len(set(action_pair)) != 2:
            errors.append(f"[ACTION_SEMANTIC_AUDIT_PAIR_INVALID] {audit_id} 必须比较两个不同动作")
            continue
        pair_key = frozenset(action_pair)
        if pair_key in audited_pairs:
            errors.append(f"[ACTION_SEMANTIC_AUDIT_DUPLICATE] {audit_id} 重复审计动作对 {sorted(pair_key)}")
        audited_pairs.add(pair_key)
        _require_refs(audit.added_target_delta_ids, index.deltas, errors, audit_id)
        _require_refs(audit.added_character_state_ids, index.character_states, errors, audit_id)
        _require_refs(audit.added_evidence_ids, index.evidence, errors, audit_id)
        _require_refs(audit.causal_basis_event_ids, index.events, errors, audit_id)
        if audit.decision not in {"pass", "reject", "needs_review"}:
            errors.append(f"[ACTION_SEMANTIC_AUDIT_DECISION_INVALID] {audit_id}.decision 非法")
        if not _norm(audit.reason):
            errors.append(f"[ACTION_SEMANTIC_AUDIT_REASON_MISSING] {audit_id} 缺少开放语义比较理由")
        if audit.decision != "pass":
            errors.append(f"[ACTION_SEMANTIC_AUDIT_UNRESOLVED] {audit_id} 尚未通过语义重复审计")
        if pair_key in structurally_equivalent_pairs and not audit.semantically_equivalent:
            errors.append(f"[ACTION_STRUCTURAL_EQUIVALENCE_DENIED] {audit_id} 不得否认参与者、前置、效果和完成条件均相同的动作对")
        if not audit.semantically_equivalent:
            if audit.functional_repeat is True:
                errors.append(f"[ACTION_REPEAT_RELATION_CONFLICT] {audit_id} 声明语义不等价却又标记为功能性重复")
            continue
        if audit.functional_repeat is not True:
            errors.append(f"[ACTION_REDUNDANT_REPEAT] {audit_id} 语义等价动作没有证明新的叙事功能")
            continue
        base_action_id, repeat_action_id = action_pair
        base_event_id = action_event_owner.get(base_action_id)
        repeat_event_id = action_event_owner.get(repeat_action_id)
        if (
            not base_event_id
            or not repeat_event_id
            or not _event_depends_on(repeat_event_id, base_event_id)
            or not {base_event_id, repeat_event_id}.issubset(audit.causal_basis_event_ids)
        ):
            errors.append(f"[ACTION_FUNCTIONAL_REPEAT_CAUSAL_GAP] {audit_id} 后一动作未结构化地依赖前一动作")
        repeat_event = index.events.get(repeat_event_id or "")
        repeat_propositions = set(repeat_event.proposition_ids if repeat_event else [])
        grounded_target = any(
            set(index.deltas[delta_id].proposition_ids).intersection(repeat_propositions)
            for delta_id in audit.added_target_delta_ids
            if delta_id in index.deltas
        )
        grounded_character = any(
            index.character_states[state_id].anchor.type == "event"
            and index.character_states[state_id].anchor.id == repeat_event_id
            for state_id in audit.added_character_state_ids
            if state_id in index.character_states
        )
        grounded_evidence = any(
            index.evidence[evidence_id].anchor.type == "event"
            and index.evidence[evidence_id].anchor.id == repeat_event_id
            for evidence_id in audit.added_evidence_ids
            if evidence_id in index.evidence
        )
        if not any((grounded_target, grounded_character, grounded_evidence)):
            errors.append(f"[ACTION_FUNCTIONAL_REPEAT_DELTA_MISSING] {audit_id} 没有绑定后一事件真实产生的观众、人物或证据增量")
    missing_action_audits = structurally_equivalent_pairs - audited_pairs
    for pair in sorted((sorted(value) for value in missing_action_audits)):
        errors.append(f"[ACTION_SEMANTIC_AUDIT_MISSING] 结构高度等价的不同动作 ID 必须进入 AI 语义审计：{pair}")

    for belief_id, snapshot in index.character_beliefs.items():
        if snapshot.character_id not in declared_entity_ids:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {belief_id}.character_id={snapshot.character_id} 未声明")
        if is_system_environment_entity_id(snapshot.character_id):
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {belief_id}.character_id "
                "不得使用系统环境实体"
            )
        _anchor_ref_errors(
            snapshot.anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{belief_id}.anchor",
        )
        _require_refs(snapshot.perceived_evidence_ids, index.evidence, errors, belief_id)
        perceived = set(snapshot.perceived_evidence_ids)
        for evidence_id in perceived:
            evidence = index.evidence.get(evidence_id)
            if evidence and snapshot.character_id not in evidence.perceivable_by:
                errors.append(
                    f"[CHARACTER_EVIDENCE_NOT_PERCEIVABLE] {belief_id} 让 {snapshot.character_id} "
                    f"依据其不可感知的 {evidence_id} 更新信念"
                )
        for belief in snapshot.beliefs:
            _require_refs([belief.proposition_id], index.propositions, errors, belief_id)
            _require_refs(belief.evidence_ids, index.evidence, errors, belief_id)
            if belief.stance not in {"believed", "suspected", "rejected", "unknown"}:
                errors.append(f"[CHARACTER_BELIEF_STANCE_INVALID] {belief_id}/{belief.proposition_id} stance 非法")
            if not 0 <= belief.confidence <= 1:
                errors.append(f"[CONFIDENCE_RANGE] {belief_id}/{belief.proposition_id}.confidence 必须在 0..1")
            if belief.stance != "unknown" and not set(belief.evidence_ids).issubset(perceived):
                errors.append(f"[CHARACTER_BELIEF_WITHOUT_EVIDENCE] {belief_id} 的已知信念没有进入感知证据集合")
        _require_refs(snapshot.misbelief_proposition_ids, index.propositions, errors, f"{belief_id}.misbelief_proposition_ids")
        _require_refs(snapshot.decision_proposition_ids, index.propositions, errors, f"{belief_id}.decision_proposition_ids")
        _require_refs(snapshot.decision_action_ids, index.actions, errors, f"{belief_id}.decision_action_ids")
        allowed_basis = set(index.propositions) | set(index.evidence)
        _require_refs(snapshot.decision_basis_ids, allowed_basis, errors, f"{belief_id}.decision_basis_ids")
        if any((snapshot.decision_proposition_ids, snapshot.decision_basis_ids, snapshot.decision_action_ids)) and not all((
            snapshot.decision_proposition_ids,
            snapshot.decision_basis_ids,
            snapshot.decision_action_ids,
        )):
            errors.append(f"[CHARACTER_DECISION_BINDING_INCOMPLETE] {belief_id} 的决策必须同时绑定动作、决策命题和已获得依据")
        if snapshot.decision_action_ids and snapshot.anchor.type != "event":
            errors.append(
                f"[CHARACTER_DECISION_ANCHOR_UNORDERED] {belief_id} 授权动作却使用不可在剧本事件图排序的 "
                f"{snapshot.anchor.type} 锚点"
            )
        held_propositions = {
            belief.proposition_id for belief in snapshot.beliefs if belief.stance != "unknown"
        }
        for basis_id in snapshot.decision_basis_ids:
            if basis_id in index.evidence and basis_id not in perceived:
                errors.append(f"[CHARACTER_DECISION_UNPERCEIVED_BASIS] {belief_id} 的决定依据 {basis_id} 未被角色感知")
            if basis_id in index.propositions and basis_id not in held_propositions:
                errors.append(f"[CHARACTER_DECISION_UNHELD_BASIS] {belief_id} 的决定依据 {basis_id} 未形成角色信念")
        if snapshot.anchor.type == "event" and snapshot.anchor.id in event_order:
            belief_position = event_order[snapshot.anchor.id]
            for evidence_id in perceived:
                evidence = index.evidence.get(evidence_id)
                if (
                    evidence
                    and evidence.anchor.type == "event"
                    and evidence.anchor.id in event_order
                    and event_order[evidence.anchor.id] > belief_position
                ):
                    errors.append(
                        f"[CHARACTER_EVIDENCE_FROM_FUTURE] {belief_id} 依据未来事件证据 {evidence_id}"
                    )

    for state_id, state in index.character_states.items():
        if state.character_id not in declared_entity_ids:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {state_id}.character_id={state.character_id} 未声明")
        if is_system_environment_entity_id(state.character_id):
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {state_id}.character_id "
                "不得使用系统环境实体"
            )
        _anchor_ref_errors(
            state.anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{state_id}.anchor",
        )
        _require_refs([*state.goal_proposition_ids, *state.stakes_proposition_ids], index.propositions, errors, state_id)
        if not 0 <= state.pressure <= 1:
            errors.append(f"[PRESSURE_RANGE] {state_id}.pressure 必须在 0..1")
        if not any((
            state.goal_proposition_ids,
            state.stakes_proposition_ids,
            state.relationship_state,
            state.emotion,
            _norm(state.tactic),
        )):
            errors.append(f"[CHARACTER_DRAMATIC_STATE_EMPTY] {state_id} 没有目标、代价、关系、情绪或策略贡献")

    beliefs_by_character: defaultdict[str, list[Any]] = defaultdict(list)
    states_by_character: defaultdict[str, list[Any]] = defaultdict(list)
    for snapshot in index.character_beliefs.values():
        beliefs_by_character[snapshot.character_id].append(snapshot)
    for state in index.character_states.values():
        states_by_character[state.character_id].append(state)
    for event_id, event in index.events.items():
        event_position = event_order[event_id]
        for action_id in event.action_ids:
            action = index.actions.get(action_id)
            if action is None:
                continue
            for actor_id in action.actor_ids:
                eligible_beliefs = [
                    item for item in beliefs_by_character.get(actor_id, [])
                    if item.anchor.type == "event"
                    and event_order.get(item.anchor.id, len(event_order)) <= event_position
                    and action_id in item.decision_action_ids
                ]
                if action.decision_requirement == "applies" and not any(
                    item.decision_proposition_ids and item.decision_basis_ids
                    for item in eligible_beliefs
                ):
                    errors.append(
                        f"[CHARACTER_DECISION_CHAIN_MISSING] {event_id}/{action_id} 的执行者 "
                        f"{actor_id} 缺少感知→判断→选择依据"
                    )
                eligible_states = [
                    item for item in states_by_character.get(actor_id, [])
                    if item.anchor.type == "event"
                    and event_order.get(item.anchor.id, len(event_order)) <= event_position
                ]
                if action.decision_requirement == "applies" and not eligible_states:
                    errors.append(
                        f"[CHARACTER_DRAMATIC_STATE_MISSING] {event_id}/{action_id} 的执行者 "
                        f"{actor_id} 缺少目标/情绪/关系状态"
                    )

    prior_ids = set(index.priors)
    for prior_id, prior in index.priors.items():
        if prior.scope_id != plan.scope_id:
            errors.append(
                f"[AUDIENCE_PRIOR_SCOPE_MISMATCH] {prior_id}.scope_id={prior.scope_id} "
                f"不属于当前叙事作用域 {plan.scope_id}"
            )
        _require_refs([*prior.assumed_known_proposition_ids, *prior.assumed_unknown_proposition_ids], index.propositions, errors, prior_id)
        overlap = set(prior.assumed_known_proposition_ids).intersection(prior.assumed_unknown_proposition_ids)
        if overlap:
            errors.append(f"[AUDIENCE_PRIOR_CONFLICT] {prior_id} 同时假定知道和不知道 {sorted(overlap)}")
        if not _norm(prior.audience_description):
            errors.append(f"[AUDIENCE_PRIOR_DESCRIPTION_MISSING] {prior_id} 缺少一次观看前提")
    for state_id, state in index.audience_states.items():
        _anchor_ref_errors(
            state.anchor,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{state_id}.anchor",
        )
        _require_refs([state.audience_prior_id], index.priors, errors, state_id)
        _require_refs(state.active_question_ids, index.questions, errors, f"{state_id}.active_question_ids")
        for belief in state.beliefs:
            _require_refs([belief.proposition_id], index.propositions, errors, state_id)
            _require_refs(belief.evidence_ids, index.evidence, errors, state_id)
            if belief.stance not in {"believed", "suspected", "rejected", "unknown"}:
                errors.append(f"[AUDIENCE_BELIEF_STANCE_INVALID] {state_id}/{belief.proposition_id} stance 非法")
            if not 0 <= belief.confidence <= 1:
                errors.append(f"[CONFIDENCE_RANGE] {state_id}/{belief.proposition_id}.confidence 必须在 0..1")
            for evidence_id in belief.evidence_ids:
                evidence = index.evidence.get(evidence_id)
                if evidence and "audience" not in evidence.perceivable_by:
                    errors.append(f"[AUDIENCE_EVIDENCE_NOT_PERCEIVABLE] {state_id} 引用了观众不可感知的 {evidence_id}")
                if (
                    evidence
                    and state.anchor.type == "event"
                    and evidence.anchor.type == "event"
                    and event_order.get(evidence.anchor.id, -1) > event_order.get(state.anchor.id, -1)
                ):
                    errors.append(f"[AUDIENCE_EVIDENCE_FROM_FUTURE] {state_id} 依据未来事件证据 {evidence_id}")
        for memory in state.working_memory:
            if not isinstance(memory, dict):
                errors.append(f"[AUDIENCE_MEMORY_INVALID] {state_id}.working_memory 必须是结构化条目")
                continue
            proposition_id = _norm(memory.get("proposition_id"))
            _require_refs([proposition_id], index.propositions, errors, f"{state_id}.working_memory")
            retention = memory.get("retention_confidence")
            if not isinstance(retention, (int, float)) or not 0 <= float(retention) <= 1:
                errors.append(f"[AUDIENCE_MEMORY_CONFIDENCE_INVALID] {state_id}/{proposition_id} 保留置信度必须在 0..1")

    target_delta_ids: set[str] = set()
    for intent_id, intent in index.intents.items():
        allowed_intent_scopes = {plan.scope_id, *index.scenes, *index.arcs}
        if intent.scope_id not in allowed_intent_scopes:
            errors.append(
                f"[EXPERIENCE_INTENT_SCOPE_MISMATCH] {intent_id}.scope_id={intent.scope_id} "
                "未绑定当前集、场景或段落合同"
            )
        _require_refs(intent.anchor_event_ids, index.events, errors, intent_id)
        _require_refs(
            intent.attention_target_ids,
            declared_entity_ids | set(index.propositions),
            errors,
            f"{intent_id}.attention_target_ids",
        )
        path_prior_ids = {path.audience_prior_id for path in intent.audience_paths}
        if len(path_prior_ids) != len(intent.audience_paths):
            errors.append(f"[AUDIENCE_PATH_PRIOR_DUPLICATE] {intent_id} 为同一观众先验声明了多条未分期路径")
        missing_priors = prior_ids - path_prior_ids
        if missing_priors:
            errors.append(f"[AUDIENCE_PATH_MISSING] {intent_id} 缺少观众先验路径 {sorted(missing_priors)}")
        for withheld in intent.withheld_propositions:
            _require_refs([withheld.proposition_id], index.propositions, errors, intent_id)
            if not _norm(withheld.reason) or not (withheld.future_disclosure_anchor or withheld.carried_question_id):
                errors.append(f"[WITHHELD_WITHOUT_CONTRACT] {intent_id}/{withheld.proposition_id} 缺少隐藏理由及未来锚点/延续问题")
            if withheld.future_disclosure_anchor:
                _anchor_ref_errors(
                    withheld.future_disclosure_anchor,
                    events=index.events,
                    scenes=index.scenes,
                    errors=errors,
                    subject=f"{intent_id}/{withheld.proposition_id}.future_disclosure_anchor",
                )
            if withheld.carried_question_id:
                _require_refs([withheld.carried_question_id], index.questions, errors, intent_id)
        withheld_by_proposition = {
            item.proposition_id: item for item in intent.withheld_propositions
        }
        for path in intent.audience_paths:
            _require_refs([path.audience_prior_id], index.priors, errors, path.audience_path_id)
            _require_refs([path.audience_state_in_id, path.audience_state_out_target_id], index.audience_states, errors, path.audience_path_id)
            state_in = index.audience_states.get(path.audience_state_in_id)
            state_out = index.audience_states.get(path.audience_state_out_target_id)
            for state in (state_in, state_out):
                if state and state.audience_prior_id != path.audience_prior_id:
                    errors.append(f"[AUDIENCE_PATH_PRIOR_MISMATCH] {path.audience_path_id} 引用了另一先验的状态")
            if not path.target_deltas:
                errors.append(f"[TARGET_DELTA_MISSING] {path.audience_path_id} 没有目标观众状态变化")
            if state_in and state_out:
                comparable_in = _state_without_identity(state_in)
                comparable_out = _state_without_identity(state_out)
                if comparable_in == comparable_out and path.target_deltas:
                    errors.append(
                        f"[AUDIENCE_TARGET_STATE_UNCHANGED] {path.audience_path_id} "
                        "声明了 target_deltas，但入场与目标出场状态没有结构差"
                    )
            covered_state_fields: set[str] = set()
            covered_belief_propositions: set[str] = set()
            for delta in path.target_deltas:
                target_delta_ids.add(delta.target_delta_id)
                _require_refs(delta.proposition_ids, index.propositions, errors, delta.target_delta_id)
                _require_refs([delta.deadline_event_id], index.events, errors, delta.target_delta_id)
                if delta.dimension == "other" and not _norm(delta.custom_dimension):
                    errors.append(f"[TARGET_CUSTOM_DIMENSION_MISSING] {delta.target_delta_id} dimension=other 时必须说明语义维度")
                if delta.dimension not in {
                    "belief", "character_goal", "spatial_temporal", "affective",
                    "question", "attention", "other",
                }:
                    errors.append(f"[TARGET_DIMENSION_INVALID] {delta.target_delta_id}.dimension 非法；未预设维度必须用 other + custom_dimension")
                if delta.required_processing_s < 0:
                    errors.append(f"[PROCESSING_TIME_INVALID] {delta.target_delta_id}.required_processing_s 不能为负")
                if delta.target_confidence is not None and not 0 <= delta.target_confidence <= 1:
                    errors.append(f"[CONFIDENCE_RANGE] {delta.target_delta_id}.target_confidence 必须在 0..1")
                if delta.from_state == delta.to_state:
                    errors.append(
                        f"[TARGET_DELTA_NO_CHANGE] {delta.target_delta_id}.from_state 与 to_state 相同"
                    )
                if state_in and state_out:
                    if not _target_state_fragment_matches(delta, delta.from_state, state_in):
                        errors.append(
                            f"[TARGET_DELTA_FROM_STATE_MISMATCH] {delta.target_delta_id}.from_state "
                            "不是该观众路径入场状态的真实结构片段"
                        )
                    if not _target_state_fragment_matches(delta, delta.to_state, state_out):
                        errors.append(
                            f"[TARGET_DELTA_TO_STATE_MISMATCH] {delta.target_delta_id}.to_state "
                            "不是该观众路径目标出场状态的真实结构片段"
                        )
                    before_beliefs = {
                        belief.proposition_id: (belief.stance, belief.confidence)
                        for belief in state_in.beliefs
                    }
                    after_beliefs = {
                        belief.proposition_id: (belief.stance, belief.confidence)
                        for belief in state_out.beliefs
                    }
                    if delta.dimension == "belief" and all(
                        before_beliefs.get(proposition_id) == after_beliefs.get(proposition_id)
                        for proposition_id in delta.proposition_ids
                    ):
                        errors.append(
                            f"[TARGET_DELTA_STATE_MISMATCH] {delta.target_delta_id} 声明信念变化，"
                            "但目标命题在入/出 AudienceState 中未变化"
                        )
                    if delta.dimension == "belief":
                        covered_state_fields.add("beliefs")
                        covered_belief_propositions.update(delta.proposition_ids)
                        if delta.target_confidence is not None:
                            for proposition_id in delta.proposition_ids:
                                actual = after_beliefs.get(proposition_id)
                                if actual is None or actual[1] < delta.target_confidence:
                                    errors.append(
                                        f"[TARGET_CONFIDENCE_STATE_MISMATCH] {delta.target_delta_id} "
                                        f"目标状态中 {proposition_id} 未达到置信度 {delta.target_confidence}"
                                    )
                        for proposition_id in delta.proposition_ids:
                            withheld = withheld_by_proposition.get(proposition_id)
                            if withheld is None:
                                continue
                            disclosure_reached = False
                            disclosure = withheld.future_disclosure_anchor
                            if (
                                disclosure is not None
                                and disclosure.type == "event"
                                and state_out.anchor.type == "event"
                            ):
                                disclosure_reached = (
                                    event_order.get(state_out.anchor.id, -1)
                                    >= event_order.get(disclosure.id, len(event_order))
                                )
                            actual = after_beliefs.get(proposition_id)
                            if (
                                not disclosure_reached
                                and actual is not None
                                and actual[0] != "unknown"
                            ):
                                errors.append(
                                    f"[WITHHELD_TARGET_CONFLICT] {path.audience_path_id}/{proposition_id} "
                                    "在披露锚点前把有意隐藏命题设为可信/可疑/可否定的目标"
                                )
                    if (
                        delta.dimension == "question"
                        and set(state_in.active_question_ids) == set(state_out.active_question_ids)
                    ):
                        errors.append(
                            f"[TARGET_DELTA_STATE_MISMATCH] {delta.target_delta_id} 声明问题变化，"
                            "但 active_question_ids 未变化"
                        )
                    if delta.dimension == "question":
                        covered_state_fields.add("active_question_ids")
                    if (
                        delta.dimension == "character_goal"
                        and state_in.character_goal_hypotheses == state_out.character_goal_hypotheses
                    ):
                        errors.append(
                            f"[TARGET_DELTA_STATE_MISMATCH] {delta.target_delta_id} 声明人物目标理解变化，"
                            "但 character_goal_hypotheses 未变化"
                        )
                    if delta.dimension == "character_goal":
                        covered_state_fields.add("character_goal_hypotheses")
                    if (
                        delta.dimension == "spatial_temporal"
                        and state_in.spatial_model == state_out.spatial_model
                        and state_in.temporal_model == state_out.temporal_model
                    ):
                        errors.append(
                            f"[TARGET_DELTA_STATE_MISMATCH] {delta.target_delta_id} 声明时空变化，"
                            "但空间与时间模型均未变化"
                        )
                    if delta.dimension == "spatial_temporal":
                        covered_state_fields.update({"spatial_model", "temporal_model"})
                    if (
                        delta.dimension == "affective"
                        and state_in.affective_state == state_out.affective_state
                    ):
                        errors.append(
                            f"[TARGET_DELTA_STATE_MISMATCH] {delta.target_delta_id} 声明情绪变化，"
                            "但 affective_state 未变化"
                        )
                    if delta.dimension == "affective":
                        covered_state_fields.add("affective_state")
                    if (
                        delta.dimension == "attention"
                        and set(state_in.attention_residue_ids) == set(state_out.attention_residue_ids)
                        and state_in.working_memory == state_out.working_memory
                    ):
                        errors.append(
                            f"[TARGET_DELTA_STATE_MISMATCH] {delta.target_delta_id} 声明注意变化，"
                            "但 attention_residue_ids 与 working_memory 均未变化"
                        )
                    if delta.dimension == "attention":
                        covered_state_fields.update({"attention_residue_ids", "working_memory"})
                    if delta.dimension == "other":
                        covered_state_fields.update(
                            set(delta.from_state).intersection(delta.to_state)
                        )

            if state_in and state_out:
                changed_fields = _changed_audience_state_fields(state_in, state_out)
                uncovered_fields = changed_fields - covered_state_fields
                if uncovered_fields:
                    errors.append(
                        f"[AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED] {path.audience_path_id} "
                        f"入/出状态的结构变化没有 target_delta 负责：{sorted(uncovered_fields)}"
                    )
                before_by_prop = {
                    item.proposition_id: (item.stance, item.confidence)
                    for item in state_in.beliefs
                }
                after_by_prop = {
                    item.proposition_id: (item.stance, item.confidence)
                    for item in state_out.beliefs
                }
                changed_belief_props = {
                    proposition_id
                    for proposition_id in set(before_by_prop) | set(after_by_prop)
                    if before_by_prop.get(proposition_id) != after_by_prop.get(proposition_id)
                }
                if changed_belief_props - covered_belief_propositions:
                    errors.append(
                        f"[AUDIENCE_BELIEF_DIFF_UNASSIGNED] {path.audience_path_id} "
                        f"信念变化没有绑定相应命题：{sorted(changed_belief_props - covered_belief_propositions)}"
                    )

            ordered_deltas = sorted(
                path.target_deltas,
                key=lambda item: (
                    event_order.get(item.deadline_event_id, len(event_order)),
                    item.target_delta_id,
                ),
            )
            total_processing_s = sum(
                max(0.0, delta.required_processing_s)
                for delta in ordered_deltas
            )
            if (
                len(ordered_deltas) > 1
                and total_processing_s > config.VIDEO_DURATION_MAX_S
            ):
                prior_states = [
                    state
                    for state in index.audience_states.values()
                    if (
                        state.audience_prior_id == path.audience_prior_id
                        and state.audience_state_id
                        not in {
                            path.audience_state_in_id,
                            path.audience_state_out_target_id,
                        }
                    )
                ]
                for current_delta, next_delta in zip(
                    ordered_deltas,
                    ordered_deltas[1:],
                ):
                    staged = any(
                        _target_state_fragment_matches(
                            current_delta,
                            current_delta.to_state,
                            state,
                        )
                        and _target_state_fragment_matches(
                            next_delta,
                            next_delta.from_state,
                            state,
                        )
                        for state in prior_states
                    )
                    if not staged:
                        errors.append(
                            "[AUDIENCE_TARGET_DELTA_STAGING_REQUIRED] "
                            f"{path.audience_path_id}/{path.audience_prior_id} 在 "
                            f"{current_delta.target_delta_id} -> "
                            f"{next_delta.target_delta_id} 之间缺少中间 AudienceState；"
                            f"单镜处理总量 {total_processing_s:.3f}s 超过 "
                            f"{config.VIDEO_DURATION_MAX_S}s"
                        )

    child_ids = {
        parent_id for event in index.events.values() for parent_id in event.causal_parent_ids
    }
    critical_events = {
        event_id for event_id, event in index.events.items()
        if event.downstream_dependency_event_ids or event_id in child_ids
    }
    intended_propositions = {
        proposition_id
        for intent in index.intents.values()
        for path in intent.audience_paths
        for delta in path.target_deltas
        for proposition_id in delta.proposition_ids
    }
    withheld_propositions = {
        withheld.proposition_id
        for intent in index.intents.values()
        for withheld in intent.withheld_propositions
    }
    for event_id in sorted(critical_events):
        for proposition_id in index.events[event_id].proposition_ids:
            if proposition_id not in intended_propositions | withheld_propositions:
                errors.append(
                    f"[CRITICAL_PROPOSITION_INTENT_MISSING] {event_id}/{proposition_id} "
                    "被后续剧情依赖却没有逐先验体验意图或有意隐藏合同"
                )

    for task_id, task in index.tasks.items():
        _require_refs([task.experience_intent_id], index.intents, errors, task_id)
        _require_refs([task.audience_path_id], index.paths, errors, task_id)
        _require_refs([task.target_delta_id], index.deltas, errors, task_id)
        _require_refs(task.required_prior_proposition_ids, index.propositions, errors, task_id)
        _require_refs(task.downstream_dependency_event_ids, index.events, errors, task_id)
        path = index.paths.get(task.audience_path_id)
        if path and task.target_delta_id not in {d.target_delta_id for d in path.target_deltas}:
            errors.append(f"[ASSIMILATION_TARGET_MISMATCH] {task_id} 的 target_delta 不属于其 audience_path")
        intent = index.intents.get(task.experience_intent_id)
        if intent and task.audience_path_id not in {item.audience_path_id for item in intent.audience_paths}:
            errors.append(f"[ASSIMILATION_INTENT_PATH_MISMATCH] {task_id} 的 audience_path 不属于其 ExperienceIntent")
        if not _norm(task.satisfaction_criteria):
            errors.append(f"[ASSIMILATION_CRITERIA_MISSING] {task_id} 缺少可由盲审验证的标准")
        if task.status not in {"open", "planned", "satisfied", "needs_review"}:
            errors.append(f"[ASSIMILATION_STATUS_INVALID] {task_id}.status 非法")
        if task.status == "needs_review":
            errors.append(f"[ASSIMILATION_NEEDS_REVIEW] {task_id} 仍不确定，不能标记叙事就绪")

    delta_prior = {
        delta.target_delta_id: path.audience_prior_id
        for intent in index.intents.values()
        for path in intent.audience_paths
        for delta in path.target_deltas
    }
    for window_id, window in index.windows.items():
        _require_refs(window.event_ids, index.events, errors, window_id)
        _require_refs(window.proposition_ids, index.propositions, errors, window_id)
        _require_refs(window.target_delta_ids, index.deltas, errors, window_id)
        _require_refs(window.evidence_ids, index.evidence, errors, window_id)
        if window.scheduled_processing_s < 0 or window.planned_available_s < 0:
            errors.append(f"[READABILITY_TIME_INVALID] {window_id} 的处理时间不能为负")
        if window.status == "satisfied" and window.planned_available_s < window.scheduled_processing_s:
            errors.append(f"[READABILITY_CAPACITY_EXCEEDED] {window_id} 可用时间不足却标记 satisfied")
        if window.status not in {"planned", "satisfied", "needs_replan"}:
            errors.append(f"[READABILITY_STATUS_INVALID] {window_id}.status 非法")
        if not _norm(window.readability_reason):
            errors.append(f"[READABILITY_REASON_MISSING] {window_id} 没有说明为何需要独立注意窗口")
        required_by_prior: defaultdict[str, float] = defaultdict(float)
        for delta_id in window.target_delta_ids:
            delta = index.deltas.get(delta_id)
            if delta:
                required_by_prior[delta_prior.get(delta_id, "unknown")] += max(
                    0.0, delta.required_processing_s,
                )
        required_processing = max(required_by_prior.values(), default=0.0)
        if window.scheduled_processing_s < required_processing:
            errors.append(
                f"[READABILITY_SCHEDULE_UNDERALLOCATED] {window_id} 分配 "
                f"{window.scheduled_processing_s}s，小于低分位路径所需 {required_processing}s"
            )

    for delta_id, delta in index.deltas.items():
        window_id = _norm(delta.primary_delivery_window_id)
        window = index.windows.get(window_id)
        if not window_id:
            errors.append(f"[TARGET_PRIMARY_WINDOW_MISSING] {delta_id} 没有唯一主要交付窗口")
        elif window is None:
            errors.append(f"[NARRATIVE_REF_MISSING] {delta_id}.primary_delivery_window_id 引用了不存在的 {window_id}")
        elif delta_id not in window.target_delta_ids:
            errors.append(f"[TARGET_PRIMARY_WINDOW_MISMATCH] {window_id} 没有声明目标变化 {delta_id}")

    for payoff_id, payoff in index.payoffs.items():
        _require_refs([*payoff.setup_proposition_ids, *payoff.intended_inference_ids], index.propositions, errors, payoff_id)
        _require_refs([*payoff.setup_event_ids, *payoff.payoff_event_ids], index.events, errors, payoff_id)
        _require_refs([payoff.retention_deadline_event_id], index.events, errors, f"{payoff_id}.retention_deadline_event_id")
        if payoff.status == "paid_off" and not payoff.payoff_event_ids:
            errors.append(f"[PAYOFF_EVENT_MISSING] {payoff_id} 已兑现但没有兑现事件")
        if payoff.status not in {"open", "preserved", "paid_off", "intentionally_carried"}:
            errors.append(f"[PAYOFF_STATUS_INVALID] {payoff_id}.status 非法")
        if not 0 <= payoff.minimum_retention_confidence <= 1:
            errors.append(f"[PAYOFF_RETENTION_RANGE] {payoff_id}.minimum_retention_confidence 必须在 0..1")
        setup_positions = [event_order[item] for item in payoff.setup_event_ids if item in event_order]
        payoff_positions = [event_order[item] for item in payoff.payoff_event_ids if item in event_order]
        if setup_positions and payoff_positions and max(setup_positions) >= min(payoff_positions):
            errors.append(f"[SETUP_PAYOFF_ORDER_INVALID] {payoff_id} 的铺垫没有先于兑现")
        deadline_position = event_order.get(payoff.retention_deadline_event_id)
        if deadline_position is not None and setup_positions and deadline_position < max(setup_positions):
            errors.append(f"[SETUP_RETENTION_DEADLINE_INVALID] {payoff_id} 的记忆截止点早于铺垫")
        low_memory_by_prior: dict[str, set[str]] = {}
        if deadline_position is not None:
            for prior_id in prior_ids:
                eligible_states = [
                    (event_order[state.anchor.id], state_position, state)
                    for state_position, state in enumerate(index.audience_states.values())
                    if state.audience_prior_id == prior_id
                    and state.anchor.type == "event"
                    and state.anchor.id in event_order
                    and event_order[state.anchor.id] <= deadline_position
                ]
                latest_state = max(eligible_states, default=None)
                memory = {
                    _norm(item.get("proposition_id")): float(item.get("retention_confidence"))
                    for state in ([latest_state[2]] if latest_state else [])
                    for item in state.working_memory
                    if isinstance(item, dict)
                    and _norm(item.get("proposition_id"))
                    and isinstance(item.get("retention_confidence"), (int, float))
                }
                low = {
                    proposition_id
                    for proposition_id in payoff.setup_proposition_ids
                    if memory.get(proposition_id, 0.0) < payoff.minimum_retention_confidence
                }
                if low:
                    low_memory_by_prior[prior_id] = low
        recall_required = bool(low_memory_by_prior)
        if payoff.recall_needed is None:
            errors.append(f"[SETUP_RECALL_DECISION_MISSING] {payoff_id}.recall_needed 必须由逐先验工作记忆推导")
        elif payoff.recall_needed != recall_required:
            errors.append(
                f"[SETUP_RECALL_DECISION_MISMATCH] {payoff_id}.recall_needed={payoff.recall_needed} "
                f"与低分位记忆结果 {recall_required} 不一致"
            )
        if recall_required:
            for prior_id, low_propositions in low_memory_by_prior.items():
                matching_tasks = [
                    task
                    for task in index.tasks.values()
                    if index.paths.get(task.audience_path_id)
                    and index.paths[task.audience_path_id].audience_prior_id == prior_id
                    and low_propositions.issubset(task.required_prior_proposition_ids)
                    and (
                        payoff.retention_deadline_event_id in task.downstream_dependency_event_ids
                        or set(payoff.payoff_event_ids).intersection(task.downstream_dependency_event_ids)
                    )
                ]
                if not matching_tasks:
                    errors.append(
                        f"[SETUP_RECALL_TASK_MISSING] {payoff_id}/{prior_id} 在使用前已遗忘 "
                        f"{sorted(low_propositions)}，但没有逐路径认知唤回任务"
                    )

    for scene_id, scene in index.scenes.items():
        if (
            scene.point_of_view_character_id
            and scene.point_of_view_character_id not in declared_entity_ids
        ):
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {scene_id}.point_of_view_character_id={scene.point_of_view_character_id} 未声明")
        if is_system_environment_entity_id(scene.point_of_view_character_id):
            errors.append(
                f"[SYSTEM_NARRATIVE_ENTITY_POLICY_INVALID] {scene_id} 的 "
                "point_of_view_character_id 不得使用系统环境实体；无人物场必须为空"
            )
        if scene.applicability not in {"applies", "not_applicable"}:
            errors.append(f"[SCENE_APPLICABILITY_INVALID] {scene_id}.applicability 非法")
        if scene.applicability == "not_applicable":
            if not _norm(scene.not_applicable_reason) or not _norm(scene.alternative_dramatic_function):
                errors.append(f"[SCENE_ALTERNATIVE_FUNCTION_MISSING] {scene_id} 不套传统场景结构时必须说明理由和替代功能")
        else:
            required_dimensions = {
                "scene_question_id": bool(_norm(scene.scene_question_id)),
                "goal_proposition_ids": bool(scene.goal_proposition_ids),
                "obstacle_proposition_ids": bool(scene.obstacle_proposition_ids),
                "stakes_proposition_ids": bool(scene.stakes_proposition_ids),
                "pressure_curve": bool(scene.pressure_curve),
                "turn_or_button": bool(scene.turn_event_ids or _norm(scene.scene_button)),
                "value_polarity_in": bool(_norm(scene.value_polarity_in)),
                "value_polarity_out": bool(_norm(scene.value_polarity_out)),
            }
            missing_dimensions = sorted(
                name for name, present in required_dimensions.items() if not present
            )
            if missing_dimensions:
                errors.append(
                    f"[SCENE_DRAMATIC_DIMENSION_MISSING] {scene_id} applicability=applies "
                    f"却缺少审计维度 {missing_dimensions}"
                )
            if (
                _norm(scene.value_polarity_in)
                and _norm(scene.value_polarity_out)
                and _norm(scene.value_polarity_in) == _norm(scene.value_polarity_out)
                and not scene.relationship_deltas
            ):
                errors.append(f"[SCENE_VALUE_CHANGE_MISSING] {scene_id} 没有价值极性或关系变化")
        _require_refs(scene.turn_event_ids, index.events, errors, scene_id)
        _require_refs([*scene.goal_proposition_ids, *scene.obstacle_proposition_ids, *scene.stakes_proposition_ids], index.propositions, errors, scene_id)
        if scene.scene_question_id:
            _require_refs([scene.scene_question_id], index.questions, errors, f"{scene_id}.scene_question_id")
        _require_refs(
            [*scene.character_state_in_ids, *scene.character_state_out_ids],
            index.character_states,
            errors,
            scene_id,
        )
        scene_path_priors = {item.audience_prior_id for item in scene.audience_state_paths}
        if scene.applicability == "applies" and scene_path_priors != prior_ids:
            errors.append(f"[SCENE_AUDIENCE_PATH_MISSING] {scene_id} 缺少逐先验场景状态路径")
        for path in scene.audience_state_paths:
            _require_refs([path.audience_prior_id], index.priors, errors, scene_id)
            _require_refs(
                [path.audience_state_in_id, path.audience_state_out_target_id],
                index.audience_states,
                errors,
                scene_id,
            )
            for state_ref in (path.audience_state_in_id, path.audience_state_out_target_id):
                state = index.audience_states.get(state_ref)
                if state and state.audience_prior_id != path.audience_prior_id:
                    errors.append(f"[SCENE_AUDIENCE_PRIOR_MISMATCH] {scene_id} 的状态 {state_ref} 不属于 {path.audience_prior_id}")
        _curve_errors(
            scene.pressure_curve,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{scene_id}.pressure_curve",
        )

    for arc_id, arc in index.arcs.items():
        if arc.applicability not in {"applies", "not_applicable"}:
            errors.append(f"[ARC_APPLICABILITY_INVALID] {arc_id}.applicability 非法")
        if arc.applicability == "not_applicable" and (
            not _norm(arc.not_applicable_reason) or not _norm(arc.alternative_dramatic_function)
        ):
            errors.append(f"[ARC_ALTERNATIVE_FUNCTION_MISSING] {arc_id} 必须说明非传统结构的替代功能")
        if arc.applicability == "applies":
            required_dimensions = {
                "question_or_promise": bool(arc.core_question_ids or arc.promise_proposition_ids),
                "escalation_event_ids": bool(arc.escalation_event_ids),
                "climax_event_ids": bool(arc.climax_event_ids),
                "pressure_curve": bool(arc.pressure_curve),
                "information_density_curve": bool(arc.information_density_curve),
                "processing_beats": bool(arc.processing_beats),
            }
            missing_dimensions = sorted(
                name for name, present in required_dimensions.items() if not present
            )
            if missing_dimensions:
                errors.append(
                    f"[ARC_DRAMATIC_DIMENSION_MISSING] {arc_id} applicability=applies "
                    f"却缺少审计维度 {missing_dimensions}"
                )
        _require_refs([*arc.escalation_event_ids, *arc.climax_event_ids], index.events, errors, arc_id)
        _require_refs(arc.promise_proposition_ids, index.propositions, errors, arc_id)
        _require_refs([*arc.core_question_ids, *arc.ending_hook_question_ids, *arc.resolved_question_ids, *arc.carried_question_ids], index.questions, errors, arc_id)
        _require_refs(arc.payoff_contract_ids, index.payoffs, errors, arc_id)
        overlap = set(arc.resolved_question_ids).intersection(arc.carried_question_ids)
        if overlap:
            errors.append(f"[ARC_QUESTION_STATUS_CONFLICT] {arc_id} 同时解决和带入后续 {sorted(overlap)}")
        accounted_questions = (
            set(arc.resolved_question_ids)
            | set(arc.carried_question_ids)
            | set(arc.ending_hook_question_ids)
        )
        unclosed_questions = set(arc.core_question_ids) - accounted_questions
        if unclosed_questions:
            errors.append(f"[ARC_QUESTION_UNCLOSED] {arc_id} 的核心问题未解决也未明确带入后续 {sorted(unclosed_questions)}")
        payoff_promises = {
            proposition_id
            for payoff_id in arc.payoff_contract_ids
            if payoff_id in index.payoffs
            for proposition_id in index.payoffs[payoff_id].setup_proposition_ids
        }
        orphan_promises = set(arc.promise_proposition_ids) - payoff_promises
        if orphan_promises:
            errors.append(f"[ARC_PROMISE_PAYOFF_MISSING] {arc_id} 的承诺没有铺垫—兑现合同 {sorted(orphan_promises)}")
        escalation_positions = [
            event_order[event_id] for event_id in arc.escalation_event_ids if event_id in event_order
        ]
        climax_positions = [
            event_order[event_id] for event_id in arc.climax_event_ids if event_id in event_order
        ]
        if escalation_positions and climax_positions and min(climax_positions) <= min(escalation_positions):
            errors.append(f"[ARC_CLIMAX_ORDER_INVALID] {arc_id} 的高潮没有位于升级之后")
        _curve_errors(
            arc.pressure_curve,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{arc_id}.pressure_curve",
        )
        _curve_errors(
            arc.information_density_curve,
            events=index.events,
            scenes=index.scenes,
            errors=errors,
            subject=f"{arc_id}.information_density_curve",
        )
        for position, beat in enumerate(arc.processing_beats):
            if not isinstance(beat, dict) or not _norm(beat.get("purpose")):
                errors.append(f"[ARC_PROCESSING_BEAT_INVALID] {arc_id}.processing_beats[{position}] 缺少目的")
                continue
            anchor = beat.get("anchor")
            if not isinstance(anchor, dict):
                errors.append(f"[CURVE_ANCHOR_MISSING] {arc_id}.processing_beats[{position}] 缺少锚点")

    return list(dict.fromkeys(errors))
