"""Narrative continuity graph, audience-path and storyboard hard gates.

The validators in this module are intentionally relation-driven.  They never
classify a story by title, genre, object or action words.  Text is retained as
evidence for people and models; deterministic code validates provenance,
causality, perception, ownership, capacity and cross-shot hand-offs.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from math import floor
from typing import Any, Iterable

from app import config
from app.schemas import (
    AudiencePriorContract,
    BlindAudienceObservation,
    EpisodeScreenplay,
    NarrativeContinuityPlan,
    NarrativeReviewReport,
    ShotContribution,
    Storyboard,
    StoryboardOutline,
)
from app.spoken_contract import onscreen_text_for_capacity

NARRATIVE_CONTRACT_VERSION = "narrative-continuity.v1"
AUDIENCE_PERCEPTUAL_SURFACE_VERSION = "audience-perceptual-surface.v1"


def storyboard_authority_projection(
    value: Storyboard | dict[str, Any],
) -> dict[str, Any]:
    """Return authored storyboard facts, excluding display-only numbering.

    Episode identity is the stable episode scope id carried by Artifacts and
    certificates. ``episode_no`` controls ordering and directory presentation;
    project compaction may change it without authoring a new story.  Every
    authority comparison must therefore bind the complete typed shot contracts
    while treating that display number as non-narrative metadata.
    """
    board = value if isinstance(value, Storyboard) else Storyboard.model_validate(value)
    payload = board.model_dump(mode="json")
    payload.pop("episode_no", None)
    from app.continuity import PROMPT_CONTRACT_VERSION

    for shot in payload.get("shots") or []:
        # QA/display annotations are mutable sidecar evidence. They must never
        # revoke an immutable storyboard release or trigger paid regeneration.
        shot.pop("risk_tags", None)
        if not shot.get("prompt_contract_version"):
            shot["prompt_contract_version"] = PROMPT_CONTRACT_VERSION
    return payload


def _norm(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_source_evidence_text(value: object) -> str:
    """Ignore import-time wrapping while preserving every non-whitespace character."""
    return "".join(str(value or "").split())


def _ids(items: Iterable[Any], field: str, errors: list[str], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, item in enumerate(items):
        value = _norm(getattr(item, field, ""))
        if not value:
            errors.append(f"[NARRATIVE_ID_MISSING] {label}[{index}].{field} 不能为空")
            continue
        if value in result:
            errors.append(f"[NARRATIVE_ID_DUPLICATE] {label}.{field} 重复：{value}")
            continue
        result[value] = item
    return result


def _require_refs(
    values: Iterable[str],
    target: dict[str, Any] | set[str],
    errors: list[str],
    subject: str,
) -> None:
    known = target if isinstance(target, set) else set(target)
    for value in values:
        ref = _norm(value)
        if not ref:
            errors.append(f"[NARRATIVE_REF_EMPTY] {subject} 含空引用")
        elif ref not in known:
            errors.append(f"[NARRATIVE_REF_MISSING] {subject} 引用了不存在的 {ref}")


def _cycle_nodes(parents: dict[str, list[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle: list[str] = []

    def visit(node: str, trail: list[str]) -> bool:
        if node in visiting:
            start = trail.index(node) if node in trail else 0
            cycle.extend(trail[start:] + [node])
            return True
        if node in visited:
            return False
        visiting.add(node)
        trail.append(node)
        for parent in parents.get(node, []):
            if parent in parents and visit(parent, trail):
                return True
        trail.pop()
        visiting.remove(node)
        visited.add(node)
        return False

    for item in parents:
        if visit(item, []):
            break
    return cycle


def _contribution_nonempty(contribution: ShotContribution | None) -> bool:
    if contribution is None:
        return False
    return any((
        contribution.target_delta_ids,
        contribution.assimilation_task_ids,
        contribution.evidence_ids,
        contribution.story_delta_fact_ids,
        contribution.character_state_delta_ids,
        contribution.audience_state_delta_ids,
        contribution.affective_delta,
        contribution.spatial_temporal_delta,
        abs(contribution.dramatic_pressure_delta) > 1e-9,
    ))


def _anchor_ref_errors(
    anchor: Any,
    *,
    events: dict[str, Any],
    scenes: dict[str, Any],
    errors: list[str],
    subject: str,
) -> None:
    """Validate only anchor kinds whose identity is authoritative at this layer.

    Beat/sequence/shot anchors may be materialized later, so they remain open
    semantic anchors.  Event and scene anchors, however, must resolve inside
    the current episode contract; this also prevents cross-episode ID reuse.
    """
    anchor_type = _norm(getattr(anchor, "type", ""))
    anchor_id = _norm(getattr(anchor, "id", ""))
    if not anchor_type or not anchor_id:
        errors.append(f"[NARRATIVE_ANCHOR_MISSING] {subject} 的锚点类型和 ID 不能为空")
    elif anchor_type == "event" and anchor_id not in events:
        errors.append(f"[NARRATIVE_REF_MISSING] {subject} 引用了不存在的事件锚点 {anchor_id}")
    elif anchor_type == "scene" and anchor_id not in scenes:
        errors.append(f"[NARRATIVE_REF_MISSING] {subject} 引用了不存在的场景锚点 {anchor_id}")


def _curve_errors(
    points: Iterable[dict[str, Any]],
    *,
    events: dict[str, Any],
    scenes: dict[str, Any],
    errors: list[str],
    subject: str,
) -> None:
    for position, point in enumerate(points):
        anchor = point.get("anchor") if isinstance(point, dict) else None
        if not isinstance(anchor, dict):
            errors.append(f"[CURVE_ANCHOR_MISSING] {subject}[{position}] 缺少事件或节拍锚点")
            continue
        _anchor_ref_errors(
            type("Anchor", (), anchor)(),
            events=events,
            scenes=scenes,
            errors=errors,
            subject=f"{subject}[{position}]",
        )
        value = point.get("value")
        if value is not None and (not isinstance(value, (int, float)) or not 0 <= float(value) <= 1):
            errors.append(f"[CURVE_VALUE_RANGE] {subject}[{position}].value 必须在 0..1")


def _state_without_identity(state: Any) -> dict[str, Any]:
    return state.model_dump(mode="json", exclude={"audience_state_id", "anchor"})


def _json_fragment_matches(fragment: Any, actual: Any) -> bool:
    """Return whether ``fragment`` is a non-empty, exact structural fragment.

    Director-authored target deltas may omit unchanged sibling fields, but
    they may not introduce arbitrary keys or values that are absent from the
    authoritative audience snapshot.  This is relation validation, not a
    vocabulary or story-category classifier.
    """
    if isinstance(fragment, dict):
        return bool(fragment) and isinstance(actual, dict) and all(
            key in actual and _json_fragment_matches(value, actual[key])
            for key, value in fragment.items()
        )
    if isinstance(fragment, list):
        return isinstance(actual, list) and fragment == actual
    return fragment == actual


def _belief_fragment_matches(
    fragment: dict[str, Any],
    state: Any,
    proposition_ids: list[str],
) -> bool:
    beliefs = {
        item.proposition_id: item.model_dump(mode="json", exclude={"proposition_id"})
        for item in state.beliefs
    }
    if len(proposition_ids) == 1 and set(fragment).issubset(
        {"stance", "confidence", "evidence_ids"}
    ):
        actual = beliefs.get(proposition_ids[0])
        return actual is not None and _json_fragment_matches(fragment, actual)
    if set(fragment) == {"beliefs"}:
        declared = fragment["beliefs"]
        if isinstance(declared, dict):
            return bool(declared) and all(
                proposition_id in proposition_ids
                and proposition_id in beliefs
                and _json_fragment_matches(value, beliefs[proposition_id])
                for proposition_id, value in declared.items()
            )
        if isinstance(declared, list):
            actual = [
                item.model_dump(mode="json")
                for item in state.beliefs
                if item.proposition_id in proposition_ids
            ]
            return bool(declared) and declared == actual
        return False
    return bool(fragment) and all(
        proposition_id in proposition_ids
        and proposition_id in beliefs
        and isinstance(value, dict)
        and _json_fragment_matches(value, beliefs[proposition_id])
        for proposition_id, value in fragment.items()
    )


def _target_state_fragment_matches(delta: Any, fragment: dict[str, Any], state: Any) -> bool:
    dimension = delta.dimension
    if dimension == "belief":
        return _belief_fragment_matches(fragment, state, delta.proposition_ids)
    if dimension == "character_goal":
        actual = state.character_goal_hypotheses
        if set(fragment) == {"character_goal_hypotheses"}:
            return fragment["character_goal_hypotheses"] == actual
        return _json_fragment_matches(fragment, actual)
    if dimension == "spatial_temporal":
        wrapped = {
            "spatial_model": state.spatial_model,
            "temporal_model": state.temporal_model,
        }
        if fragment and set(fragment).issubset(wrapped):
            return all(fragment[key] == wrapped[key] for key in fragment)
        return _json_fragment_matches(fragment, wrapped)
    if dimension == "affective":
        actual = state.affective_state
        if set(fragment) == {"affective_state"}:
            return fragment["affective_state"] == actual
        return _json_fragment_matches(fragment, actual)
    if dimension == "question":
        return _json_fragment_matches(
            fragment,
            {"active_question_ids": state.active_question_ids},
        )
    if dimension == "attention":
        return _json_fragment_matches(
            fragment,
            {
                "attention_residue_ids": state.attention_residue_ids,
                "working_memory": [
                    (
                        item.model_dump(mode="json")
                        if hasattr(item, "model_dump")
                        else item
                    )
                    for item in state.working_memory
                ],
            },
        )
    # Open semantic dimensions remain expressible, but must point at an actual
    # changed fragment of the snapshot instead of becoming unbound prose.
    return _json_fragment_matches(fragment, _state_without_identity(state))


def _changed_audience_state_fields(state_in: Any, state_out: Any) -> set[str]:
    before = _state_without_identity(state_in)
    after = _state_without_identity(state_out)
    return {
        key for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }


def _declared_change_matches(declared: Any, before: Any, after: Any) -> bool:
    """Whether an open semantic delta describes values that actually changed.

    Keys and values come from the AI-authored state model.  Deterministic code
    only checks their relation to authoritative before/after snapshots; it does
    not enumerate emotions, locations, action kinds or genres.
    """
    if isinstance(declared, dict):
        if not declared or not isinstance(before, dict) or not isinstance(after, dict):
            return False
        return all(
            key in after
            and (
                _declared_change_matches(value, before.get(key), after[key])
                if isinstance(value, dict)
                else before.get(key) != after[key] and value == after[key]
            )
            for key, value in declared.items()
        )
    return before != after and declared == after


def _contains_forbidden_contract_key(value: Any, forbidden: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            str(key) in forbidden
            or _contains_forbidden_contract_key(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_contract_key(item, forbidden) for item in value)
    return False


@dataclass(frozen=True)
class NarrativeIndex:
    source_evidence: dict[str, Any]
    propositions: dict[str, Any]
    decisions: dict[str, Any]
    facts: dict[str, Any]
    evidence: dict[str, Any]
    questions: dict[str, Any]
    events: dict[str, Any]
    actions: dict[str, Any]
    action_audits: dict[str, Any]
    character_states: dict[str, Any]
    character_beliefs: dict[str, Any]
    priors: dict[str, Any]
    audience_states: dict[str, Any]
    intents: dict[str, Any]
    paths: dict[str, Any]
    deltas: dict[str, Any]
    tasks: dict[str, Any]
    windows: dict[str, Any]
    payoffs: dict[str, Any]
    scenes: dict[str, Any]
    arcs: dict[str, Any]
    identities: dict[str, Any]


def index_narrative_plan(plan: NarrativeContinuityPlan, errors: list[str] | None = None) -> NarrativeIndex:
    sink = errors if errors is not None else []
    paths: dict[str, Any] = {}
    deltas: dict[str, Any] = {}
    for intent in plan.experience_intents:
        for path in intent.audience_paths:
            if path.audience_path_id in paths:
                sink.append(f"[NARRATIVE_ID_DUPLICATE] audience_path_id 重复：{path.audience_path_id}")
            paths[path.audience_path_id] = path
            for delta in path.target_deltas:
                if delta.target_delta_id in deltas:
                    sink.append(f"[NARRATIVE_ID_DUPLICATE] target_delta_id 重复：{delta.target_delta_id}")
                deltas[delta.target_delta_id] = delta
    return NarrativeIndex(
        source_evidence=_ids(plan.source_evidence, "source_evidence_id", sink, "source_evidence"),
        propositions=_ids(plan.propositions, "proposition_id", sink, "propositions"),
        decisions=_ids(plan.adaptation_decisions, "adaptation_decision_id", sink, "adaptation_decisions"),
        facts=_ids(plan.state_facts, "fact_id", sink, "state_facts"),
        evidence=_ids(plan.evidence, "evidence_id", sink, "evidence"),
        questions=_ids(plan.dramatic_questions, "dramatic_question_id", sink, "dramatic_questions"),
        events=_ids(plan.events, "event_id", sink, "events"),
        actions=_ids(plan.atomic_actions, "action_id", sink, "atomic_actions"),
        action_audits=_ids(
            plan.action_relation_audits,
            "action_relation_audit_id",
            sink,
            "action_relation_audits",
        ),
        character_states=_ids(plan.character_states, "character_state_id", sink, "character_states"),
        character_beliefs=_ids(plan.character_beliefs, "character_belief_id", sink, "character_beliefs"),
        priors=_ids(plan.audience_priors, "audience_prior_id", sink, "audience_priors"),
        audience_states=_ids(plan.audience_states, "audience_state_id", sink, "audience_states"),
        intents=_ids(plan.experience_intents, "experience_intent_id", sink, "experience_intents"),
        paths=paths,
        deltas=deltas,
        tasks=_ids(plan.assimilation_tasks, "assimilation_task_id", sink, "assimilation_tasks"),
        windows=_ids(plan.readability_windows, "readability_window_id", sink, "readability_windows"),
        payoffs=_ids(plan.setup_payoff_contracts, "setup_payoff_id", sink, "setup_payoff_contracts"),
        scenes=_ids(plan.scene_contracts, "scene_id", sink, "scene_contracts"),
        arcs=_ids(plan.arc_contracts, "arc_id", sink, "arc_contracts"),
        identities=_ids(plan.identity_contracts, "identity_id", sink, "identity_contracts"),
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
    identity_display_names: dict[str, str] = {}
    for identity_id, identity in index.identities.items():
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
    event_order = {event_id: position for position, event_id in enumerate(index.events)}
    parents: dict[str, list[str]] = {}
    for event_id, event in index.events.items():
        parents[event_id] = list(event.causal_parent_ids)
        _require_refs(event.proposition_ids, index.propositions, errors, event_id)
        _require_refs(event.causal_parent_ids, index.events, errors, event_id)
        _require_refs(event.precondition_fact_ids, index.facts, errors, event_id)
        _require_refs(event.action_ids, index.actions, errors, event_id)
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
        if not action.actor_ids and not action.target_ids:
            errors.append(f"[ACTION_PARTICIPANT_MISSING] {action_id} 没有主体或作用目标")
        undeclared_participants = (
            set(action.actor_ids) | set(action.target_ids)
        ) - declared_entity_ids
        if undeclared_participants:
            errors.append(f"[NARRATIVE_ENTITY_UNDECLARED] {action_id} 含未声明动作参与者 {sorted(undeclared_participants)}")
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


def _outline_as_shots(outline: StoryboardOutline) -> list[Any]:
    return list(outline.shots or [])


_STORYBOARD_SCORE_ONLY_SCREENPLAY_CODES = frozenset({
    "EVENT_PRECONDITION_FROM_FUTURE",
    "INITIAL_FACT_HAS_PRODUCER",
    "STATE_REPLAY_WITHOUT_DELTA",
    "CHARACTER_DECISION_BINDING_INCOMPLETE",
    "CHARACTER_DECISION_CHAIN_MISSING",
    "AUDIENCE_EVIDENCE_FROM_FUTURE",
    "TARGET_DELTA_TO_STATE_MISMATCH",
    "TARGET_DELTA_STATE_MISMATCH",
    "AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED",
    "SETUP_RECALL_TASK_MISSING",
    "SCENE_DRAMATIC_DIMENSION_MISSING",
})


def _narrative_error_code(message: str) -> str:
    text = str(message or "")
    return text[1:text.index("]")] if text.startswith("[") and "]" in text else ""


def validate_storyboard_screenplay_authority(
    screenplay: EpisodeScreenplay,
    *,
    expected_scope_id: str | None = None,
) -> list[str]:
    """Keep publication score-only findings score-only in storyboard runtime.

    The screenplay QA report remains unchanged and auditable. Storyboard only
    blocks on authority errors that prevent deterministic projection; audience
    interpretation and authoring-quality findings cannot be repaired by
    regenerating shots and must not be promoted into paid model retry loops.
    """
    return [
        error
        for error in validate_screenplay_narrative(
            screenplay,
            require=True,
            expected_scope_id=expected_scope_id,
        )
        if _narrative_error_code(error)
        not in _STORYBOARD_SCORE_ONLY_SCREENPLAY_CODES
    ]


def validate_storyboard_narrative(
    board: Storyboard | None,
    screenplay: EpisodeScreenplay,
    *,
    outline: StoryboardOutline | None = None,
    complete: bool = True,
    expected_scope_id: str | None = None,
) -> list[str]:
    """Validate shot contribution, action/delta ownership and audience hand-offs.

    Pass ``complete=False`` while generating a prefix; reference and replay
    invariants still run, but future delivery ownership is not demanded yet.
    """
    plan = screenplay.narrative_plan
    if plan is None:
        return ["[NARRATIVE_PLAN_MISSING] 分镜不能在缺少剧本叙事合同的情况下标记 narrative_ready"]
    errors = validate_storyboard_screenplay_authority(
        screenplay,
        expected_scope_id=expected_scope_id,
    )
    index = index_narrative_plan(plan)
    items = list(board.shots if board is not None else _outline_as_shots(outline or StoryboardOutline(episode_no=screenplay.episode_no)))
    if not items:
        return list(dict.fromkeys([*errors, "[NARRATIVE_SHOTS_EMPTY] 没有可验证的分镜任务"]))

    shot_ids: dict[str, Any] = {}
    action_owners: dict[str, str] = {}
    delta_owners: defaultdict[str, list[str]] = defaultdict(list)
    delta_owner_positions: defaultdict[str, list[int]] = defaultdict(list)
    task_owners: defaultdict[str, list[int]] = defaultdict(list)
    event_occurrences: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
    for item_position, item in enumerate(items):
        item_event_ids = list(getattr(item, "event_ids", []) or [])
        if not item_event_ids and _norm(getattr(item, "story_event_id", "")):
            item_event_ids = [_norm(getattr(item, "story_event_id", ""))]
        for event_index, event_id in enumerate(item_event_ids):
            if event_id in index.events:
                event_occurrences[event_id].append((item_position, event_index))
    contribution_ids: set[str] = set()
    prior_ids = set(index.priors)
    phase_owner = {
        phase.phase_id: action
        for action in index.actions.values()
        for phase in action.temporal_phases
    }
    action_event_owner = {
        action_id: event_id
        for event_id, event in index.events.items()
        for action_id in event.action_ids
    }
    phase_deliveries: defaultdict[str, list[tuple[int, int, str]]] = defaultdict(list)
    action_delivery_positions: defaultdict[str, list[int]] = defaultdict(list)
    contribution_character_owners: dict[str, str] = {}
    contribution_audience_owners: dict[str, str] = {}
    delta_paths = {
        delta.target_delta_id: (
            path.audience_prior_id,
            delta,
            path.audience_state_out_target_id,
        )
        for intent in plan.experience_intents
        for path in intent.audience_paths
        for delta in path.target_deltas
    }
    previous_paths: dict[str, Any] = {}
    previous_state_out: set[str] | None = None
    completed_actions: set[str] = set()
    completed_phases: set[str] = set()
    previous_shot_phase_ids: list[str] = []
    for position, shot in enumerate(items):
        shot_id = _norm(getattr(shot, "shot_id", ""))
        label = shot_id or f"shot_no={getattr(shot, 'shot_no', position + 1)}"
        if not shot_id:
            errors.append(f"[SHOT_ID_MISSING] {label} 缺少稳定 shot_id")
        elif shot_id in shot_ids:
            errors.append(f"[SHOT_ID_DUPLICATE] shot_id 重复：{shot_id}")
        else:
            shot_ids[shot_id] = shot

        event_ids = list(getattr(shot, "event_ids", []) or [])
        if not event_ids and _norm(getattr(shot, "story_event_id", "")):
            event_ids = [_norm(getattr(shot, "story_event_id", ""))]
        _require_refs(event_ids, index.events, errors, label)
        scene_id = _norm(getattr(shot, "scene_id", ""))
        if index.scenes:
            if not scene_id:
                errors.append(f"[SHOT_SCENE_ID_MISSING] {label} 缺少 SceneDramaticContract 引用")
            else:
                _require_refs([scene_id], index.scenes, errors, label)
        primary_action_id = _norm(getattr(shot, "primary_action_id", None)) or None
        supporting = [
            _norm(value) for value in (getattr(shot, "supporting_action_ids", []) or [])
        ]
        if len(set(supporting)) != len(supporting) or (
            primary_action_id is not None and primary_action_id in supporting
        ):
            errors.append(f"[SHOT_ACTION_BINDING_DUPLICATE] {label} 的主/辅动作引用重复")
        bound_action_ids = [
            action_id for action_id in [primary_action_id, *supporting] if action_id
        ]
        if primary_action_id:
            _require_refs([primary_action_id], index.actions, errors, label)
            previous = action_owners.get(primary_action_id)
            if previous:
                errors.append(f"[ACTION_PRIMARY_OWNER_DUPLICATE] {primary_action_id} 在 {previous}/{label} 重复作为主要动作")
            action_owners[primary_action_id] = label
        _require_refs(supporting, index.actions, errors, label)
        phase_ids = [
            _norm(value) for value in (getattr(shot, "action_phase_ids", []) or [])
        ]
        if any(not phase_id for phase_id in phase_ids) or len(set(phase_ids)) != len(phase_ids):
            errors.append(f"[SHOT_ACTION_PHASE_ID_INVALID] {label} 含空或重复动作阶段")
        _require_refs(phase_ids, phase_owner, errors, f"{label}.action_phase_ids")
        for phase_index, phase_id in enumerate(phase_ids):
            action = phase_owner.get(phase_id)
            if action and action.action_id not in bound_action_ids:
                errors.append(
                    f"[SHOT_ACTION_PHASE_OWNER_MISMATCH] {label}/{phase_id} 不属于本镜绑定动作"
                )
            if action:
                phase_deliveries[action.action_id].append((position, phase_index, phase_id))
        visible_or_audible_entities = {
            _norm(value)
            for value in (
                *(getattr(shot, "visible_entity_ids", []) or []),
                *(getattr(shot, "characters_visible", []) or []),
                *(getattr(shot, "characters", []) or []),
                *(getattr(shot, "audio_cast", []) or []),
                *(
                    getattr(dialogue, "speaker", "")
                    for dialogue in (getattr(shot, "dialogues", []) or [])
                ),
            )
            if _norm(value)
        }
        offscreen_actors = {
            _norm(value)
            for value in (getattr(shot, "offscreen_action_actor_ids", []) or [])
            if _norm(value)
        }
        offscreen_targets = {
            _norm(value)
            for value in (getattr(shot, "offscreen_action_target_ids", []) or [])
            if _norm(value)
        }
        bound_actor_ids = {
            actor_id
            for action_id in bound_action_ids
            for action in [index.actions.get(action_id)]
            if action is not None
            for actor_id in action.actor_ids
        }
        bound_target_ids = {
            target_id
            for action_id in bound_action_ids
            for action in [index.actions.get(action_id)]
            if action is not None
            for target_id in action.target_ids
        }
        invalid_offscreen_actors = offscreen_actors - bound_actor_ids
        if invalid_offscreen_actors:
            errors.append(
                f"[SHOT_OFFSCREEN_ACTOR_INVALID] {label} 画外执行者不属于本镜绑定动作："
                f"{sorted(invalid_offscreen_actors)}"
            )
        invalid_offscreen_targets = offscreen_targets - bound_target_ids
        if invalid_offscreen_targets:
            errors.append(
                f"[SHOT_OFFSCREEN_TARGET_INVALID] {label} 画外作用对象不属于本镜绑定动作："
                f"{sorted(invalid_offscreen_targets)}"
            )
        for action_id in bound_action_ids:
            action_delivery_positions[action_id].append(position)
            owner_event_id = action_event_owner.get(action_id)
            if owner_event_id is None or owner_event_id not in event_ids:
                errors.append(
                    f"[SHOT_ACTION_EVENT_MISMATCH] {label}/{action_id} 没有绑定该动作的权威事件"
                )
            action = index.actions.get(action_id)
            if action is None:
                continue
            action_phase_set = {phase.phase_id for phase in action.temporal_phases}
            delivered_for_action = [
                phase_id for phase_id in phase_ids if phase_id in action_phase_set
            ]
            if action.temporal_phases and not delivered_for_action:
                errors.append(f"[SHOT_ACTION_PHASE_MISSING] {label}/{action_id} 没有声明本镜负责的阶段")
            if not action.temporal_phases and action_id in supporting:
                errors.append(
                    f"[PHASELESS_SUPPORTING_ACTION_INVALID] {label}/{action_id} 没有可拆阶段，不得作为辅动作提前/重演"
                )
            missing_actors = set(action.actor_ids) - visible_or_audible_entities - offscreen_actors
            if missing_actors:
                errors.append(
                    f"[SHOT_ACTION_ACTOR_UNDELIVERED] {label}/{action_id} 的执行者既未可见/可听也未显式画外交付："
                    f"{sorted(missing_actors)}"
                )
            missing_targets = set(action.target_ids) - visible_or_audible_entities - offscreen_targets
            if missing_targets:
                errors.append(
                    f"[SHOT_ACTION_TARGET_UNDELIVERED] {label}/{action_id} 的作用对象既未可见/可听"
                    f"也未显式画外交付：{sorted(missing_targets)}"
                )

        planned_in = set(getattr(shot, "planned_state_in_fact_ids", []) or [])
        delta_add = set(getattr(shot, "planned_delta_add_fact_ids", []) or [])
        delta_remove = set(getattr(shot, "planned_delta_remove_fact_ids", []) or [])
        planned_out = set(getattr(shot, "planned_state_out_fact_ids", []) or [])
        _require_refs(planned_in | delta_add | delta_remove | planned_out, index.facts, errors, label)
        if delta_add & delta_remove:
            errors.append(f"[SHOT_STATE_DELTA_CONFLICT] {label} 同时增加和移除 {sorted(delta_add & delta_remove)}")
        if delta_remove - planned_in:
            errors.append(f"[SHOT_STATE_REGRESSION] {label} 移除未在入口成立的事实 {sorted(delta_remove - planned_in)}")
        expected_out = (planned_in - delta_remove) | delta_add
        if expected_out != planned_out:
            errors.append(
                f"[SHOT_STATE_OUT_MISMATCH] {label} 的 planned_state_out 不是 "
                "planned_state_in - remove + add"
            )
        if previous_state_out is not None:
            boundary = getattr(shot, "narrative_boundary_from_previous", None)
            allowed = set(boundary.allowed_state_deltas) if boundary else set()
            cross_boundary_delta = previous_state_out.symmetric_difference(planned_in)
            transitions = list(boundary.state_delta_transitions) if boundary else []
            justified = {
                fact_id
                for transition in transitions
                for fact_id in (transition.source_fact_id, transition.target_fact_id)
                if fact_id
            }
            if boundary and allowed != justified:
                errors.append(
                    f"[BOUNDARY_STATE_JUSTIFICATION_MISMATCH] {label} allowed_state_deltas "
                    "必须精确等于结构化转换中的来源/目标事实"
                )
            if cross_boundary_delta != allowed:
                errors.append(
                    f"[SHOT_STATE_HANDOFF_BROKEN] {label} 与上一镜状态差不等于边界可验证转换："
                    f"actual={sorted(cross_boundary_delta)} allowed={sorted(allowed)}"
                )
            if boundary:
                required = set(boundary.required_state_invariants)
                if not required.issubset(previous_state_out & planned_in):
                    errors.append(f"[BOUNDARY_STATE_INVARIANT_BROKEN] {label} 未保持边界要求的世界状态")
                transition_ids: set[str] = set()
                for transition in transitions:
                    transition_id = _norm(transition.transition_id)
                    if not transition_id or transition_id in transition_ids:
                        errors.append(f"[BOUNDARY_TRANSITION_ID_INVALID] {label} 的转换 ID 为空或重复")
                    transition_ids.add(transition_id)
                    source_id = _norm(transition.source_fact_id)
                    target_id = _norm(transition.target_fact_id)
                    _require_refs([source_id, target_id], index.facts, errors, transition_id or label)
                    if not source_id or not target_id or source_id == target_id:
                        errors.append(f"[BOUNDARY_TRANSITION_PAIR_INVALID] {transition_id or label} 必须连接两个不同的状态事实")
                        continue
                    if source_id not in previous_state_out or source_id in planned_in:
                        errors.append(f"[BOUNDARY_TRANSITION_SOURCE_MISMATCH] {transition_id} 来源事实不是上镜离开态")
                    if target_id not in planned_in or target_id in previous_state_out:
                        errors.append(f"[BOUNDARY_TRANSITION_TARGET_MISMATCH] {transition_id} 目标事实不是本镜入场态")
                    if not _norm(transition.reason):
                        errors.append(f"[BOUNDARY_TRANSITION_REASON_MISSING] {transition_id} 缺少可审计转换理由")
                    source_fact = index.facts.get(source_id)
                    target_fact = index.facts.get(target_id)
                    if source_fact is None or target_fact is None:
                        continue
                    same_semantic_slot = (
                        source_fact.proposition_id == target_fact.proposition_id
                        and source_fact.subject_id == target_fact.subject_id
                        and source_fact.predicate_id == target_fact.predicate_id
                    )
                    basis = transition.basis_type
                    if basis == "timeline_change":
                        temporal = boundary.temporal_orientation_contract
                        if (
                            not same_semantic_slot
                            or source_fact.time_scope == target_fact.time_scope
                            or temporal.get("from_time_scope") != source_fact.time_scope
                            or temporal.get("to_time_scope") != target_fact.time_scope
                            or not _norm(temporal.get("orientation_reason"))
                        ):
                            errors.append(f"[BOUNDARY_TIMELINE_RELATION_INVALID] {transition_id} 未绑定真实时域变化")
                    elif basis == "spatial_reorientation":
                        spatial = boundary.spatial_orientation_contract
                        if (
                            not same_semantic_slot
                            or source_fact.time_scope != target_fact.time_scope
                            or source_fact.value.kind != "spatial"
                            or target_fact.value.kind != "spatial"
                            or source_fact.value.data == target_fact.value.data
                            or spatial.get("source_fact_id") != source_id
                            or spatial.get("target_fact_id") != target_id
                            or not _norm(spatial.get("orientation_reason"))
                        ):
                            errors.append(f"[BOUNDARY_SPATIAL_RELATION_INVALID] {transition_id} 未绑定真实空间重定向")
                    elif basis == "viewpoint_visibility_change":
                        spatial = boundary.spatial_orientation_contract
                        if (
                            not same_semantic_slot
                            or source_fact.time_scope != target_fact.time_scope
                            or source_fact.value != target_fact.value
                            or source_fact.visibility == target_fact.visibility
                            or spatial.get("source_fact_id") != source_id
                            or spatial.get("target_fact_id") != target_id
                            or not _norm(spatial.get("orientation_reason"))
                        ):
                            errors.append(f"[BOUNDARY_VIEWPOINT_RELATION_INVALID] {transition_id} 未绑定真实视点可见性变化")
                    elif basis == "action_phase_handoff":
                        phase_id = _norm(transition.basis_action_phase_id)
                        action = phase_owner.get(phase_id)
                        action_facts = set()
                        if action:
                            action_facts.update(action.precondition_fact_ids)
                            action_facts.update(action.effects_add)
                            action_facts.update(action.effects_remove)
                        if (
                            not phase_id
                            or phase_id != _norm(boundary.handoff_action_phase_id)
                            or action is None
                            or not {source_id, target_id}.issubset(action_facts)
                        ):
                            errors.append(f"[BOUNDARY_ACTION_PHASE_RELATION_INVALID] {transition_id} 未绑定真实动作阶段")
                    elif basis == "other":
                        if not _norm(transition.custom_basis):
                            errors.append(f"[BOUNDARY_CUSTOM_BASIS_MISSING] {transition_id} 未说明开放语义关系")
                        errors.append(f"[BOUNDARY_TRANSITION_NEEDS_REVIEW] {transition_id} 的未预设边界关系需要人工复核")
                    else:
                        errors.append(f"[BOUNDARY_TRANSITION_BASIS_INVALID] {transition_id} 的结构依据非法；未预设关系必须用 other")
        previous_state_out = planned_out

        event_preconditions = {
            fact_id
            for event_id in event_ids
            if event_id in index.events
            and event_occurrences[event_id]
            and position == event_occurrences[event_id][0][0]
            for fact_id in index.events[event_id].precondition_fact_ids
        }
        event_adds = {
            fact_id
            for event_id in event_ids
            if complete
            and event_id in index.events
            and event_occurrences[event_id]
            and position == event_occurrences[event_id][-1][0]
            for fact_id in index.events[event_id].effects_add
        }
        event_removes = {
            fact_id
            for event_id in event_ids
            if complete
            and event_id in index.events
            and event_occurrences[event_id]
            and position == event_occurrences[event_id][-1][0]
            for fact_id in index.events[event_id].effects_remove
        }
        if not event_preconditions.issubset(planned_in):
            errors.append(f"[SHOT_EVENT_PRECONDITION_MISSING] {label} 未承接事件前置事实")
        if not event_adds.issubset(delta_add) or not event_removes.issubset(delta_remove):
            errors.append(f"[SHOT_EVENT_EFFECT_MISSING] {label} 的计划状态变化未覆盖所声明事件效果")
        minimum_action_s = 0.0
        for action_id in bound_action_ids:
            action = index.actions.get(action_id)
            if action is None:
                continue
            action_phase_ids = [phase.phase_id for phase in action.temporal_phases]
            delivered_for_action = [
                phase_id for phase_id in phase_ids if phase_id in action_phase_ids
            ]
            starts_action = (
                not action_phase_ids or action_phase_ids[0] in delivered_for_action
            )
            completes_action = (
                not action_phase_ids or action_phase_ids[-1] in delivered_for_action
            )
            if starts_action and not set(action.precondition_fact_ids).issubset(planned_in):
                errors.append(f"[SHOT_ACTION_PRECONDITION_MISSING] {label} 未满足 {action_id} 的前置事实")
            if completes_action and (
                not set(action.effects_add).issubset(delta_add)
                or not set(action.effects_remove).issubset(delta_remove)
            ):
                errors.append(f"[SHOT_ACTION_EFFECT_MISSING] {label} 的完成状态未覆盖 {action_id} 的效果")
            minimum_action_s += sum(
                max(0.0, phase.estimated_min_s)
                for phase in action.temporal_phases
                if phase.phase_id in delivered_for_action
            )

        contribution = getattr(shot, "shot_contribution", None)
        if not _contribution_nonempty(contribution):
            errors.append(f"[SHOT_CONTRIBUTION_EMPTY] {label} 没有动作、认知、证据、时空、情绪或压力贡献")
        if contribution:
            cid = _norm(contribution.shot_contribution_id)
            if not cid:
                errors.append(f"[SHOT_CONTRIBUTION_ID_MISSING] {label} 缺少 shot_contribution_id")
            elif cid in contribution_ids:
                errors.append(f"[SHOT_CONTRIBUTION_ID_DUPLICATE] {cid} 被多个镜头复用")
            contribution_ids.add(cid)
            _require_refs(contribution.experience_intent_ids, index.intents, errors, label)
            _require_refs(contribution.target_delta_ids, index.deltas, errors, label)
            _require_refs(contribution.assimilation_task_ids, index.tasks, errors, label)
            _require_refs(contribution.evidence_ids, index.evidence, errors, label)
            _require_refs(contribution.story_delta_fact_ids, index.facts, errors, label)
            _require_refs(contribution.character_state_delta_ids, set(index.character_states) | set(index.character_beliefs), errors, label)
            _require_refs(contribution.audience_state_delta_ids, index.audience_states, errors, label)
            for delta_id in contribution.target_delta_ids:
                delta_owners[delta_id].append(label)
                delta_owner_positions[delta_id].append(position)
            for task_id in contribution.assimilation_task_ids:
                task_owners[task_id].append(position)
            for state_id in contribution.character_state_delta_ids:
                previous_owner = contribution_character_owners.get(state_id)
                if previous_owner:
                    errors.append(f"[CHARACTER_STATE_DELTA_OWNER_DUPLICATE] {state_id} 被 {previous_owner}/{label} 重复主交付")
                contribution_character_owners[state_id] = label
                state = index.character_states.get(state_id) or index.character_beliefs.get(state_id)
                if state and not (
                    (state.anchor.type == "event" and state.anchor.id in event_ids)
                    or (state.anchor.type == "scene" and state.anchor.id == scene_id)
                    or (state.anchor.type == "shot" and state.anchor.id == shot_id)
                ):
                    errors.append(f"[CHARACTER_STATE_DELTA_ANCHOR_MISMATCH] {label} 交付了不属于当前锚点的 {state_id}")
            for state_id in contribution.audience_state_delta_ids:
                previous_owner = contribution_audience_owners.get(state_id)
                if previous_owner:
                    errors.append(f"[AUDIENCE_STATE_DELTA_OWNER_DUPLICATE] {state_id} 被 {previous_owner}/{label} 重复主交付")
                contribution_audience_owners[state_id] = label
            if not set(contribution.story_delta_fact_ids).issubset(delta_add | delta_remove):
                errors.append(f"[SHOT_CONTRIBUTION_STATE_MISMATCH] {label} 声明的故事状态贡献不在本镜 delta 中")
            for evidence_id in contribution.evidence_ids:
                evidence = index.evidence.get(evidence_id)
                if evidence is None:
                    continue
                if evidence.anchor.type == "event" and evidence.anchor.id not in event_ids:
                    errors.append(f"[SHOT_EVIDENCE_ANCHOR_MISMATCH] {label} 交付的 {evidence_id} 不属于本镜事件")
                if evidence.anchor.type == "shot" and evidence.anchor.id != shot_id:
                    errors.append(f"[SHOT_EVIDENCE_ANCHOR_MISMATCH] {label} 交付了锚定另一镜的 {evidence_id}")
            if bound_action_ids:
                action_evidence = [
                    index.evidence[evidence_id]
                    for evidence_id in contribution.evidence_ids
                    if evidence_id in index.evidence
                    and (
                        (
                            index.evidence[evidence_id].anchor.type == "event"
                            and index.evidence[evidence_id].anchor.id in event_ids
                        )
                        or (
                            index.evidence[evidence_id].anchor.type == "shot"
                            and index.evidence[evidence_id].anchor.id == shot_id
                        )
                    )
                ]
                if not action_evidence:
                    errors.append(
                        f"[SHOT_ACTION_EVIDENCE_MISSING] {label} 绑定了动作阶段却没有当前事件/镜头的可感知证据"
                    )
                if (offscreen_actors or offscreen_targets) and not any(
                    "audience" in evidence.perceivable_by for evidence in action_evidence
                ):
                    errors.append(
                        f"[OFFSCREEN_ACTION_NOT_PERCEIVABLE] {label} 声明画外执行者/作用对象"
                        "却没有观众可感知证据"
                    )

        paths = list(getattr(shot, "audience_state_paths", []) or [])
        current_paths = {path.audience_prior_id: path for path in paths}
        if len(current_paths) != len(paths):
            errors.append(f"[SHOT_AUDIENCE_PATH_DUPLICATE] {label} 为同一先验声明了重复状态路径")
        if complete and prior_ids - set(current_paths):
            errors.append(f"[SHOT_AUDIENCE_PATH_MISSING] {label} 缺少先验路径 {sorted(prior_ids - set(current_paths))}")
        for prior_id, path in current_paths.items():
            _require_refs([prior_id], index.priors, errors, label)
            _require_refs([path.audience_state_in_id, path.audience_state_out_target_id], index.audience_states, errors, label)
            state_in = index.audience_states.get(path.audience_state_in_id)
            state_out = index.audience_states.get(path.audience_state_out_target_id)
            if state_in and state_in.audience_prior_id != prior_id:
                errors.append(f"[SHOT_AUDIENCE_PRIOR_MISMATCH] {label} 的入口状态不属于 {prior_id}")
            if state_out and state_out.audience_prior_id != prior_id:
                errors.append(f"[SHOT_AUDIENCE_PRIOR_MISMATCH] {label} 的出口状态不属于 {prior_id}")
            previous = previous_paths.get(prior_id)
            if previous and previous.audience_state_out_target_id != path.audience_state_in_id:
                errors.append(
                    f"[AUDIENCE_STATE_HANDOFF_BROKEN] {label}/{prior_id} 的入口 {path.audience_state_in_id} "
                    f"不等于上一镜出口 {previous.audience_state_out_target_id}"
                )
        boundary = getattr(shot, "narrative_boundary_from_previous", None)

        # Contribution fields are claims about real graph changes, not escape
        # hatches for filler.  Validate them against the current prior-specific
        # snapshots and anchored character states.
        changed_audience_state_ids: set[str] = set()
        audience_pairs: list[tuple[Any, Any]] = []
        for path in current_paths.values():
            state_in = index.audience_states.get(path.audience_state_in_id)
            state_out = index.audience_states.get(path.audience_state_out_target_id)
            if state_in is None or state_out is None:
                continue
            audience_pairs.append((state_in, state_out))
            if _state_without_identity(state_in) != _state_without_identity(state_out):
                changed_audience_state_ids.add(state_out.audience_state_id)
        if contribution:
            declared_audience_state_ids = set(contribution.audience_state_delta_ids)
            if declared_audience_state_ids != changed_audience_state_ids:
                errors.append(
                    f"[SHOT_AUDIENCE_DELTA_LEDGER_MISMATCH] {label} 观众状态贡献必须精确等于本镜实际变化："
                    f"declared={sorted(declared_audience_state_ids)} "
                    f"actual={sorted(changed_audience_state_ids)}"
                )
            for delta_id in contribution.target_delta_ids:
                path_contract = delta_paths.get(delta_id)
                if path_contract is None:
                    continue
                prior_id, delta, final_state_id = path_contract
                current_path = current_paths.get(prior_id)
                if current_path is None:
                    errors.append(f"[SHOT_TARGET_PRIOR_PATH_MISSING] {label}/{delta_id} 没有对应观众路径")
                    continue
                state_in = index.audience_states.get(current_path.audience_state_in_id)
                state_out = index.audience_states.get(current_path.audience_state_out_target_id)
                if state_in and not _target_state_fragment_matches(delta, delta.from_state, state_in):
                    errors.append(f"[SHOT_TARGET_FROM_STATE_MISMATCH] {label}/{delta_id} 未从合同约定的观众状态出发")
                if state_out and not _target_state_fragment_matches(
                    delta,
                    delta.to_state,
                    state_out,
                ):
                    final_state = index.audience_states.get(final_state_id)
                    coarse_snapshot_holds = (
                        current_path.audience_state_in_id
                        == current_path.audience_state_out_target_id
                        and final_state is not None
                        and _target_state_fragment_matches(
                            delta,
                            delta.to_state,
                            final_state,
                        )
                    )
                    if not coarse_snapshot_holds:
                        errors.append(f"[SHOT_TARGET_TO_STATE_MISMATCH] {label}/{delta_id} 未到达合同约定的观众状态")
            if contribution.affective_delta and not any(
                _declared_change_matches(
                    contribution.affective_delta,
                    state_in.affective_state,
                    state_out.affective_state,
                )
                for state_in, state_out in audience_pairs
            ):
                errors.append(f"[SHOT_AFFECTIVE_DELTA_UNGROUNDED] {label} 情绪贡献与任一权威观众状态变化不符")
            if contribution.spatial_temporal_delta and not any(
                _declared_change_matches(
                    contribution.spatial_temporal_delta,
                    {
                        "spatial_model": state_in.spatial_model,
                        "temporal_model": state_in.temporal_model,
                    },
                    {
                        "spatial_model": state_out.spatial_model,
                        "temporal_model": state_out.temporal_model,
                    },
                )
                for state_in, state_out in audience_pairs
            ):
                errors.append(f"[SHOT_SPATIOTEMPORAL_DELTA_UNGROUNDED] {label} 时空贡献与任一权威观众状态变化不符")
            if abs(contribution.dramatic_pressure_delta) > 1e-9 and not any(
                state_id in index.character_states
                for state_id in contribution.character_state_delta_ids
            ):
                errors.append(f"[SHOT_PRESSURE_DELTA_UNGROUNDED] {label} 压力变化没有当前镜头锚定的人物状态")

        # All viewing work shares one shot duration.  The AI proposes an open
        # dimensional budget; code derives only graph/text lower bounds and
        # validates their sum, so no story/action word list is involved.
        duration_s = float(getattr(shot, "duration_s", 0) or 0)
        budget = getattr(shot, "capacity_budget", None)
        if complete and duration_s <= 0:
            errors.append(f"[SHOT_DURATION_MISSING] {label} 完整分镜缺少正时长")
        if complete and budget is None:
            errors.append(f"[SHOT_CAPACITY_BUDGET_MISSING] {label} 缺少联合观看时间预算")
        if budget is not None:
            components = {
                field: float(getattr(budget, field, 0) or 0)
                for field in (
                    "action_phase_s",
                    "spoken_and_text_s",
                    "attention_switch_s",
                    "inference_processing_s",
                    "reaction_registration_s",
                    "spatial_reorientation_s",
                    "entry_exit_settle_s",
                    "other_s",
                )
            }
            negative = sorted(field for field, value in components.items() if value < 0)
            if negative:
                errors.append(f"[SHOT_CAPACITY_NEGATIVE] {label} 时间预算含负值 {negative}")
            if components["other_s"] > 0 and not _norm(budget.other_reason):
                errors.append(f"[SHOT_CAPACITY_OTHER_REASON_MISSING] {label} 开放预算项缺少理由")
            if components["action_phase_s"] + 1e-9 < minimum_action_s:
                errors.append(
                    f"[SHOT_ACTION_CAPACITY_EXCEEDED] {label} 动作阶段最少需要 "
                    f"{minimum_action_s:.3f}s"
                )
            if bound_action_ids and minimum_action_s <= 0 and components["action_phase_s"] <= 0:
                errors.append(f"[SHOT_ACTION_CAPACITY_UNDECLARED] {label} 执行动作却未分配任何执行时间")

            dialogue_text = "".join(
                _norm(getattr(item, "line", ""))
                for item in (getattr(shot, "dialogues", []) or [])
            )
            narration_text = _norm(getattr(shot, "narration", ""))
            timeline_text = "".join(
                _norm(getattr(item, "text", ""))
                for item in (getattr(shot, "audio_timeline", []) or [])
                if getattr(item, "type", "") in {
                    "spoken_dialogue",
                    "offscreen_voice",
                }
            )
            required_text = getattr(shot, "required_text", None)
            onscreen_text = onscreen_text_for_capacity(required_text)
            from app.spoken_contract import content_char_count

            linguistic_chars = max(
                content_char_count(dialogue_text + narration_text),
                content_char_count(timeline_text),
            ) + content_char_count(onscreen_text)
            text_min_s = (
                linguistic_chars
                * float(config.VIDEO_DURATION_MIN_S)
                / float(config.SPOKEN_CHARS_PER_5_SECONDS)
            )
            timeline_min_s = max(
                (
                    float(getattr(item, "end_s", 0) or 0)
                    for item in (getattr(shot, "audio_timeline", []) or [])
                    if getattr(item, "type", "") in {
                        "spoken_dialogue",
                        "offscreen_voice",
                    }
                ),
                default=0.0,
            )
            spoken_min_s = max(text_min_s, timeline_min_s)
            if components["spoken_and_text_s"] + 1e-9 < spoken_min_s:
                errors.append(
                    f"[SHOT_SPOKEN_TEXT_CAPACITY_EXCEEDED] {label} 口播/屏幕文字最少需要 "
                    f"{spoken_min_s:.3f}s"
                )
            processing_by_prior: defaultdict[str, float] = defaultdict(float)
            for delta_id in set(
                contribution.target_delta_ids if contribution else []
            ):
                if delta_id not in delta_paths:
                    continue
                prior_id, delta, _final_state_id = delta_paths[delta_id]
                processing_by_prior[prior_id] += max(
                    0.0, delta.required_processing_s,
                )
            # Audience priors watch the same screen time in parallel.  Sum
            # sequential work inside each path, then gate on the most demanding
            # path; adding paths together would double-charge one shared second.
            target_processing_min_s = max(
                processing_by_prior.values(),
                default=0.0,
            )
            if components["inference_processing_s"] + 1e-9 < target_processing_min_s:
                errors.append(
                    f"[SHOT_INFERENCE_CAPACITY_EXCEEDED] {label} 目标理解最少需要 "
                    f"{target_processing_min_s:.3f}s"
                )
            competing_evidence_min_s = sum(
                max(0.0, float(index.evidence[evidence_id].planned_duration_s or 0))
                for evidence_id in set(contribution.evidence_ids if contribution else [])
                if evidence_id in index.evidence
                and index.evidence[evidence_id].competing_attention_ids
            )
            if components["attention_switch_s"] + 1e-9 < competing_evidence_min_s:
                errors.append(
                    f"[SHOT_ATTENTION_CAPACITY_EXCEEDED] {label} 竞争注意证据最少需要 "
                    f"{competing_evidence_min_s:.3f}s"
                )
            if contribution and (
                contribution.affective_delta
                or contribution.character_state_delta_ids
            ) and components["reaction_registration_s"] <= 0:
                errors.append(f"[SHOT_REACTION_CAPACITY_UNDECLARED] {label} 人物/观众情绪变化没有可感知登记时间")
            has_spatial_work = bool(
                contribution and contribution.spatial_temporal_delta
            ) or bool(
                boundary
                and (
                    boundary.spatial_orientation_contract
                    or boundary.temporal_orientation_contract
                )
            )
            if has_spatial_work and components["spatial_reorientation_s"] <= 0:
                errors.append(f"[SHOT_SPATIAL_CAPACITY_UNDECLARED] {label} 时空重定向没有分配观看时间")
            total_budget_s = sum(components.values())
            if duration_s > 0 and total_budget_s > duration_s + 1e-9:
                errors.append(
                    f"[SHOT_JOINT_CAPACITY_EXCEEDED] {label} 联合预算 {total_budget_s:.3f}s "
                    f"超过镜头 {duration_s:.3f}s"
                )

        if position == 0 and boundary is not None:
            errors.append(f"[BOUNDARY_ON_FIRST_SHOT] {label} 是首镜却声明了前向边界")
        if position > 0:
            previous_shot = items[position - 1]
            previous_id = _norm(getattr(previous_shot, "shot_id", ""))
            if boundary is None:
                errors.append(f"[NARRATIVE_BOUNDARY_MISSING] {previous_id or position} -> {label} 缺少叙事边界合同")
            else:
                if boundary.previous_shot_id != previous_id or boundary.next_shot_id != shot_id:
                    errors.append(f"[NARRATIVE_BOUNDARY_ID_MISMATCH] {label} 的边界没有连接实际相邻镜头")
                _require_refs(boundary.required_state_invariants, index.facts, errors, label)
                _require_refs(boundary.allowed_state_deltas, index.facts, errors, label)
                _require_refs(boundary.forbidden_replay_action_ids, index.actions, errors, label)
                if boundary.handoff_action_phase_id:
                    known_phase_ids = {
                        phase.phase_id
                        for action_item in index.actions.values()
                        for phase in action_item.temporal_phases
                    }
                    _require_refs([boundary.handoff_action_phase_id], known_phase_ids, errors, label)
                if primary_action_id and primary_action_id in boundary.forbidden_replay_action_ids:
                    errors.append(f"[FORBIDDEN_ACTION_REPLAY] {label} 重演了边界已声明完成的动作 {primary_action_id}")
                if not _norm(boundary.cut_motivation):
                    errors.append(f"[CUT_MOTIVATION_MISSING] {label} 的边界没有解释为何此时切换注意")
                handoffs = {
                    _norm(item.get("audience_prior_id")): item
                    for item in boundary.audience_state_handoffs
                    if isinstance(item, dict)
                }
                if set(handoffs) != prior_ids:
                    errors.append(f"[BOUNDARY_AUDIENCE_HANDOFF_MISSING] {label} 没有逐先验状态交接")
                for prior_id, item in handoffs.items():
                    previous_path = previous_paths.get(prior_id)
                    current_path = current_paths.get(prior_id)
                    if not previous_path or not current_path:
                        continue
                    previous_ref = _norm(
                        item.get("previous_state_out_id")
                        or item.get("audience_state_out_id")
                    )
                    next_ref = _norm(
                        item.get("next_state_in_id")
                        or item.get("audience_state_in_id")
                    )
                    if (
                        previous_ref != previous_path.audience_state_out_target_id
                        or next_ref != current_path.audience_state_in_id
                    ):
                        errors.append(f"[BOUNDARY_AUDIENCE_HANDOFF_MISMATCH] {label}/{prior_id} 与镜头状态路径不一致")

        # The ledger is an exact snapshot *before* this shot, not a permissive
        # list.  This closes both hidden replay (omitted completed IDs) and
        # premature completion (invented IDs) without classifying action text.
        completed_before = {
            _norm(value)
            for value in (getattr(shot, "completed_before_action_ids", []) or [])
            if _norm(value)
        }
        completed_phases_before = {
            _norm(value)
            for value in (
                getattr(shot, "completed_before_action_phase_ids", []) or []
            )
            if _norm(value)
        }
        _require_refs(completed_before, index.actions, errors, label)
        _require_refs(completed_phases_before, phase_owner, errors, label)
        if completed_before != completed_actions:
            errors.append(
                f"[COMPLETED_ACTION_LEDGER_MISMATCH] {label} 完成动作账本必须等于前序实际结果："
                f"declared={sorted(completed_before)} actual={sorted(completed_actions)}"
            )
        if completed_phases_before != completed_phases:
            errors.append(
                f"[COMPLETED_PHASE_LEDGER_MISMATCH] {label} 完成阶段账本必须等于前序实际结果："
                f"declared={sorted(completed_phases_before)} actual={sorted(completed_phases)}"
            )
        replayed_actions = completed_actions.intersection(bound_action_ids)
        if replayed_actions:
            errors.append(
                f"[COMPLETED_ACTION_REPLAY] {label} 再次绑定了已完成动作 "
                f"{sorted(replayed_actions)}"
            )
        replayed_phases = completed_phases.intersection(phase_ids)
        if replayed_phases:
            errors.append(
                f"[COMPLETED_ACTION_PHASE_REPLAY] {label} 再次执行了已完成阶段 "
                f"{sorted(replayed_phases)}"
            )

        # A boundary handoff names the first phase genuinely continued from
        # the immediately preceding shot.  It must be absent for unrelated
        # cuts, so a model cannot use a decorative ID to excuse discontinuity.
        expected_handoff_phase_id: str | None = None
        if previous_shot_phase_ids and phase_ids:
            previous_action_ids = {
                phase_owner[phase_id].action_id
                for phase_id in previous_shot_phase_ids
                if phase_id in phase_owner
            }
            for phase_id in phase_ids:
                action = phase_owner.get(phase_id)
                if action and action.action_id in previous_action_ids:
                    expected_handoff_phase_id = phase_id
                    break
        declared_handoff = _norm(boundary.handoff_action_phase_id) if boundary else ""
        if declared_handoff != _norm(expected_handoff_phase_id):
            errors.append(
                f"[BOUNDARY_ACTION_PHASE_HANDOFF_MISMATCH] {label} 阶段交接必须精确指向相邻镜头续接阶段："
                f"declared={declared_handoff or None} expected={expected_handoff_phase_id}"
            )
        if boundary and set(boundary.forbidden_replay_action_ids) != completed_actions:
            errors.append(
                f"[BOUNDARY_REPLAY_LEDGER_MISMATCH] {label} 边界禁止重演集必须等于已完成动作集"
            )

        completed_phases.update(phase_ids)
        for action_id in bound_action_ids:
            action = index.actions.get(action_id)
            if action is None:
                continue
            required_phase_ids = {phase.phase_id for phase in action.temporal_phases}
            if (
                (not required_phase_ids and action_id == primary_action_id)
                or (required_phase_ids and required_phase_ids.issubset(completed_phases))
            ):
                completed_actions.add(action_id)
        previous_shot_phase_ids = phase_ids
        reserved = list(getattr(shot, "reserved_future_event_ids", []) or [])
        _require_refs(reserved, index.events, errors, label)
        for event_id in reserved:
            occurrences = event_occurrences.get(event_id, [])
            if any(item_position <= position for item_position, _ in occurrences):
                errors.append(f"[RESERVED_EVENT_ALREADY_DELIVERED] {label} 把已出现事件 {event_id} 声明为未来保留")
        _require_refs(getattr(shot, "readability_window_ids", []) or [], index.windows, errors, label)
        previous_paths = current_paths

    first_event_position = {
        event_id: min(occurrences)
        for event_id, occurrences in event_occurrences.items()
        if occurrences
    }
    for event_id, event in index.events.items():
        event_position = first_event_position.get(event_id)
        for parent_id in event.causal_parent_ids:
            parent_position = first_event_position.get(parent_id)
            if event_position is not None and parent_position is not None and parent_position >= event_position:
                errors.append(f"[STORYBOARD_EVENT_ORDER_INVALID] {event_id} 没有排在原因 {parent_id} 之后")
        if complete and event.delivery_policy == "deliver" and event.must_keep and event_position is None:
            errors.append(f"[MUST_KEEP_EVENT_UNDELIVERED] {event_id} 是本作用域必交付事件但未进入分镜")

    # A multi-shot action is one ordered execution, not several shots that each
    # restage the whole gesture.  Phase identity and order are structural, so
    # this remains genre- and wording-independent.
    if complete:
        for action_id, action in index.actions.items():
            if action_id not in action_event_owner:
                continue
            expected_phase_ids = [phase.phase_id for phase in action.temporal_phases]
            deliveries = sorted(phase_deliveries.get(action_id, []))
            delivered_phase_ids = [phase_id for _, _, phase_id in deliveries]
            if expected_phase_ids:
                if delivered_phase_ids != expected_phase_ids:
                    errors.append(
                        f"[ACTION_PHASE_DELIVERY_MISMATCH] {action_id} 阶段必须按定义顺序各交付一次："
                        f"expected={expected_phase_ids} actual={delivered_phase_ids}"
                    )
                phase_counts: defaultdict[str, int] = defaultdict(int)
                for phase_id in delivered_phase_ids:
                    phase_counts[phase_id] += 1
                duplicates = sorted(
                    phase_id for phase_id, count in phase_counts.items() if count > 1
                )
                if duplicates:
                    errors.append(
                        f"[ACTION_PHASE_OWNER_DUPLICATE] {action_id} 重复交付阶段 {duplicates}"
                    )
                if deliveries:
                    first_position = deliveries[0][0]
                    first_label = _norm(getattr(items[first_position], "shot_id", "")) or (
                        f"shot_no={getattr(items[first_position], 'shot_no', first_position + 1)}"
                    )
                    if action_owners.get(action_id) != first_label:
                        errors.append(
                            f"[ACTION_PRIMARY_PHASE_OWNER_MISMATCH] {action_id} 的主要动作所有者"
                            "必须是执行首阶段的镜头"
                        )
            elif action_id not in action_owners:
                errors.append(f"[PHASELESS_ACTION_OWNER_MISSING] {action_id} 没有唯一主要执行镜头")
            positions = action_delivery_positions.get(action_id, [])
            if positions and positions != sorted(positions):
                errors.append(f"[ACTION_DELIVERY_ORDER_INVALID] {action_id} 的镜头交付顺序非单调")

    withheld_contracts = {
        withheld.proposition_id: withheld
        for intent in plan.experience_intents
        for withheld in intent.withheld_propositions
    }
    shot_position_by_id = {
        _norm(getattr(shot, "shot_id", "")): position
        for position, shot in enumerate(items)
        if _norm(getattr(shot, "shot_id", ""))
    }
    first_scene_position: dict[str, int] = {}
    for position, shot in enumerate(items):
        scene_id = _norm(getattr(shot, "scene_id", ""))
        if scene_id:
            first_scene_position.setdefault(scene_id, position)
    for position, shot in enumerate(items):
        contribution = getattr(shot, "shot_contribution", None)
        if not contribution:
            continue
        for evidence_id in contribution.evidence_ids:
            evidence = index.evidence.get(evidence_id)
            if evidence is None:
                continue
            if "audience" not in evidence.perceivable_by:
                continue
            for proposition_id in evidence.supports_proposition_ids:
                withheld = withheld_contracts.get(proposition_id)
                if withheld is None:
                    continue
                disclosure = withheld.future_disclosure_anchor
                disclosure_reached = False
                if disclosure is not None and disclosure.type == "event":
                    disclosure_position = first_event_position.get(disclosure.id)
                    disclosure_reached = (
                        disclosure_position is not None
                        and (position, 0) >= disclosure_position
                    )
                elif disclosure is not None and disclosure.type == "scene":
                    disclosure_position = first_scene_position.get(disclosure.id)
                    disclosure_reached = (
                        disclosure_position is not None and position >= disclosure_position
                    )
                elif disclosure is not None and disclosure.type == "shot":
                    disclosure_position = shot_position_by_id.get(disclosure.id)
                    disclosure_reached = (
                        disclosure_position is not None and position >= disclosure_position
                    )
                if not disclosure_reached:
                    errors.append(
                        f"[INTENDED_AMBIGUITY_BROKEN] shot_id={getattr(shot, 'shot_id', '')} "
                        f"在约定锚点前交付了有意隐藏命题 {proposition_id}"
                    )

    if complete:
        for delta_id in index.deltas:
            owners = delta_owners.get(delta_id, [])
            if len(owners) == 0:
                errors.append(f"[TARGET_DELTA_UNDELIVERED] {delta_id} 没有主要交付镜头")
            elif len(owners) > 1:
                errors.append(f"[TARGET_DELTA_OWNER_DUPLICATE] {delta_id} 在 {owners} 被重复主要交付")
            owner_positions = delta_owner_positions.get(delta_id, [])
            delta = index.deltas[delta_id]
            deadline_position = first_event_position.get(delta.deadline_event_id)
            if owner_positions and deadline_position is not None and (owner_positions[0], 0) > deadline_position:
                errors.append(f"[TARGET_DELTA_AFTER_DEADLINE] {delta_id} 在截止事件 {delta.deadline_event_id} 之后才交付")
        for action_id, action in index.actions.items():
            event_uses = any(action_id in event.action_ids for event in index.events.values())
            if event_uses and action_id not in action_owners:
                errors.append(f"[ACTION_UNFILMED] {action_id} 属于叙事事件但没有主要执行镜头")
        for task_id, task in index.tasks.items():
            owners = task_owners.get(task_id, [])
            if not owners:
                errors.append(f"[ASSIMILATION_TASK_UNDELIVERED] {task_id} 没有镜头证据贡献")
                continue
            if len(owners) > 1:
                errors.append(f"[ASSIMILATION_TASK_OWNER_DUPLICATE] {task_id} 被多个镜头重复主要承担")
            delta = index.deltas.get(task.target_delta_id)
            deadline_ids = list(task.downstream_dependency_event_ids)
            if delta:
                deadline_ids.append(delta.deadline_event_id)
            deadline_positions = [
                first_event_position[event_id]
                for event_id in deadline_ids
                if event_id in first_event_position
            ]
            if deadline_positions and (owners[0], 0) > min(deadline_positions):
                errors.append(f"[ASSIMILATION_TASK_AFTER_DEADLINE] {task_id} 在下游使用后才完成")

    windows = list(outline.readability_windows if outline and outline.readability_windows else plan.readability_windows)
    window_ids = {window.readability_window_id for window in windows}
    for window in windows:
        _require_refs(window.target_delta_ids, index.deltas, errors, window.readability_window_id)
        if complete:
            _require_refs(window.shot_ids, set(shot_ids), errors, window.readability_window_id)
            if (window.event_ids or window.target_delta_ids) and not window.shot_ids:
                errors.append(f"[READABILITY_WINDOW_UNASSIGNED] {window.readability_window_id} 没有绑定实际镜头")
        if window.planned_available_s < window.scheduled_processing_s:
            errors.append(
                f"[READABILITY_CAPACITY_EXCEEDED] {window.readability_window_id} 计划可用 "
                f"{window.planned_available_s}s，小于分配处理时间 {window.scheduled_processing_s}s"
            )
        linked_duration = sum(
            float(getattr(shot_ids.get(shot_id), "duration_s", 0) or 0)
            for shot_id in window.shot_ids
            if shot_id in shot_ids
        )
        if complete and linked_duration and window.planned_available_s > linked_duration:
            errors.append(
                f"[READABILITY_WINDOW_DURATION_EXCEEDED] {window.readability_window_id} 的有效可读时间 "
                "大于所绑定镜头总时长"
            )
        for shot_id in window.shot_ids:
            shot = shot_ids.get(shot_id)
            if shot and window.readability_window_id not in (
                getattr(shot, "readability_window_ids", []) or []
            ):
                errors.append(f"[READABILITY_WINDOW_BACKREF_MISSING] {shot_id} 没有回引 {window.readability_window_id}")
    for shot in items:
        for window_id in getattr(shot, "readability_window_ids", []) or []:
            if window_id not in window_ids:
                errors.append(f"[READABILITY_WINDOW_MISSING] {getattr(shot, 'shot_id', '')} 引用了不存在的 {window_id}")

    windows_by_id = {window.readability_window_id: window for window in windows}
    if complete:
        for event_id, event in index.events.items():
            if event.delivery_policy != "deliver" or not event.must_keep:
                continue
            window = windows_by_id.get(_norm(event.primary_delivery_window_id))
            if window and not any(
                shot_id in shot_ids and event_id in (getattr(shot_ids[shot_id], "event_ids", []) or [])
                for shot_id in window.shot_ids
            ):
                errors.append(f"[EVENT_PRIMARY_WINDOW_UNDELIVERED] {event_id} 没有在其主要窗口内出现")
        for delta_id, delta in index.deltas.items():
            window = windows_by_id.get(_norm(delta.primary_delivery_window_id))
            owners = delta_owners.get(delta_id, [])
            if window and owners and owners[0] not in window.shot_ids:
                errors.append(f"[TARGET_PRIMARY_WINDOW_OWNER_MISMATCH] {delta_id} 的主要交付镜头不在 {window.readability_window_id}")

    bridge_ids: set[str] = set()
    for bridge in (outline.cognitive_bridge_plans if outline else []):
        bridge_id = _norm(bridge.bridge_plan_id)
        if not bridge_id or bridge_id in bridge_ids:
            errors.append(f"[COGNITIVE_BRIDGE_ID_INVALID] 认知桥 ID 为空或重复：{bridge_id or '<empty>'}")
        bridge_ids.add(bridge_id)
        _require_refs(bridge.assimilation_task_ids, index.tasks, errors, bridge_id)
        _require_refs(bridge.affected_shot_ids, set(shot_ids), errors, bridge_id)
        _require_refs(bridge.added_shot_ids, set(shot_ids), errors, bridge_id)
        if set(bridge.removed_shot_ids).intersection(shot_ids):
            errors.append(f"[COGNITIVE_BRIDGE_REMOVAL_STILL_PRESENT] {bridge_id} 声明删除的镜头仍在候选大纲")
        if not bridge.assimilation_task_ids:
            errors.append(f"[COGNITIVE_BRIDGE_TASK_MISSING] {bridge_id} 没有绑定需要修复的认知任务")
        if not bridge.candidate_changes or not bridge.expected_audience_delta:
            errors.append(f"[COGNITIVE_BRIDGE_HYPOTHESIS_MISSING] {bridge_id} 缺少候选改动或预期观众状态增量")
        if not _norm(bridge.selection_reason):
            errors.append(f"[COGNITIVE_BRIDGE_SELECTION_REASON_MISSING] {bridge_id} 缺少选择依据")
        deletion = bridge.deletion_test_result
        marginal = bridge.marginal_gain_result
        if deletion.get("passed") is not True or deletion.get("deletion_is_lossless") is True:
            errors.append(f"[COGNITIVE_BRIDGE_DELETION_TEST_FAILED] {bridge_id} 删除测试未证明该镜头/改动必要")
        gain = marginal.get("expected_gain")
        if (
            marginal.get("passed") is not True
            or not isinstance(gain, (int, float))
            or float(gain) <= 0
        ):
            errors.append(f"[COGNITIVE_BRIDGE_MARGINAL_GAIN_FAILED] {bridge_id} 边际增益测试未证明正向叙事收益")
        added_contributions = [
            getattr(shot_ids[shot_id], "shot_contribution", None)
            for shot_id in bridge.added_shot_ids
            if shot_id in shot_ids
        ]
        if bridge.added_shot_ids and not all(
            contribution
            and set(contribution.assimilation_task_ids).intersection(bridge.assimilation_task_ids)
            and _contribution_nonempty(contribution)
            for contribution in added_contributions
        ):
            errors.append(f"[COGNITIVE_BRIDGE_ADDED_SHOT_UNGROUNDED] {bridge_id} 新增镜头未直接承担所绑定认知任务")

    return list(dict.fromkeys(errors))


def validate_blind_review(
    screenplay: EpisodeScreenplay,
    observations: list[BlindAudienceObservation],
    report: NarrativeReviewReport,
) -> list[str]:
    """Validate isolation outputs and low-percentile narrative-ready decision."""
    plan = screenplay.narrative_plan
    if plan is None:
        return ["[NARRATIVE_PLAN_MISSING] 无法比较冷观众观察"]
    errors: list[str] = []
    index = index_narrative_plan(plan, errors)
    observation_map = _ids(observations, "observation_id", errors, "blind_observations")
    observed_priors: set[str] = set()
    for observation_id, observation in observation_map.items():
        _require_refs([observation.audience_prior_id], index.priors, errors, observation_id)
        if observation.audience_prior_id in observed_priors:
            errors.append(f"[BLIND_PRIOR_DUPLICATE] {observation.audience_prior_id} 有多份未区分轮次的观察")
        observed_priors.add(observation.audience_prior_id)
        if _contains_forbidden_contract_key(
            observation.spontaneous_recall,
            {"target_deltas", "target_delta_id", "director_objective", "withheld_propositions"},
        ):
            errors.append(f"[BLIND_REVIEW_TARGET_LEAK] {observation_id} 的自由复述包含导演目标字段")
        required_recall_fields = {
            "recognized_entities", "inferred_propositions", "causal_hypotheses",
            "character_goal_hypotheses", "active_question_ids",
        }
        if not required_recall_fields.issubset(observation.spontaneous_recall):
            errors.append(f"[BLIND_RECALL_INCOMPLETE] {observation_id} 的冻结自由复述字段不完整")
        if not 0 <= observation.confidence <= 1:
            errors.append(f"[CONFIDENCE_RANGE] {observation_id}.confidence 必须在 0..1")
        _require_refs(
            observation.spontaneous_supporting_evidence_ids,
            index.evidence,
            errors,
            f"{observation_id}.spontaneous_supporting_evidence_ids",
        )
        _require_refs(observation.supporting_evidence_ids, index.evidence, errors, observation_id)
        if not set(observation.spontaneous_supporting_evidence_ids).issubset(
            observation.supporting_evidence_ids
        ):
            errors.append(
                f"[BLIND_SPONTANEOUS_EVIDENCE_INVALID] {observation_id} 首轮冻结证据"
                "不是该观察实际可见证据的子集"
            )
        for evidence_id in observation.supporting_evidence_ids:
            evidence = index.evidence.get(evidence_id)
            if evidence and "audience" not in evidence.perceivable_by:
                errors.append(f"[BLIND_EVIDENCE_NOT_VISIBLE] {observation_id} 引用了观众不可见证据 {evidence_id}")
    missing_priors = set(index.priors) - observed_priors
    if missing_priors:
        errors.append(f"[BLIND_PRIOR_MISSING] 缺少冷观众先验观察 {sorted(missing_priors)}")
    _require_refs(report.experience_intent_ids, index.intents, errors, "NarrativeReviewReport")
    _require_refs(report.observation_ids, observation_map, errors, "NarrativeReviewReport")
    result_keys: set[tuple[str, str]] = set()
    failed = False
    for result in report.target_delta_results:
        key = (result.audience_prior_id, result.target_delta_id)
        if key in result_keys:
            errors.append(f"[REVIEW_RESULT_DUPLICATE] {key} 重复")
        result_keys.add(key)
        _require_refs([result.audience_prior_id], index.priors, errors, "target_delta_results")
        _require_refs([result.target_delta_id], index.deltas, errors, "target_delta_results")
        _require_refs(result.supporting_observation_ids, observation_map, errors, "target_delta_results")
        _require_refs(result.supporting_evidence_ids, index.evidence, errors, "target_delta_results")
        if result.result not in {"satisfied", "missed", "contradicted", "needs_review"}:
            errors.append(f"[REVIEW_RESULT_INVALID] {key} 的 result 非法")
        if (
            result.predicted_score is None
            or not 0 <= float(result.predicted_score) <= 1
        ):
            errors.append(
                f"[REVIEW_PREDICTED_SCORE_INVALID] {key} 必须提供 0..1 的模型预测分数"
            )
        if not _norm(result.reason):
            errors.append(f"[REVIEW_RESULT_REASON_MISSING] {key} 缺少证据比较理由")
        for observation_id in result.supporting_observation_ids:
            observation = observation_map.get(observation_id)
            if observation and observation.audience_prior_id != result.audience_prior_id:
                errors.append(f"[REVIEW_PRIOR_CROSS_CONTAMINATION] {key} 引用了其他先验观察 {observation_id}")
        spontaneous_for_prior = {
            evidence_id
            for observation in observations
            if observation.audience_prior_id == result.audience_prior_id
            for evidence_id in observation.spontaneous_supporting_evidence_ids
        }
        if not set(result.supporting_evidence_ids).issubset(spontaneous_for_prior):
            errors.append(f"[REVIEW_EVIDENCE_NOT_SPONTANEOUS] {key} 的比较证据没有出现在同先验首轮冻结观察中")
        if result.result == "satisfied" and (
            not result.supporting_observation_ids or not result.supporting_evidence_ids
        ):
            errors.append(f"[REVIEW_SATISFIED_WITHOUT_EVIDENCE] {key} 判定 satisfied 却没有可下钻观察与证据")
        failed = failed or result.result != "satisfied"
    expected_keys = {
        (path.audience_prior_id, delta.target_delta_id)
        for intent in plan.experience_intents
        for path in intent.audience_paths
        for delta in path.target_deltas
    }
    if expected_keys - result_keys:
        errors.append(f"[REVIEW_RESULT_MISSING] 缺少逐先验目标比较 {sorted(expected_keys - result_keys)}")
    if result_keys - expected_keys:
        errors.append(f"[REVIEW_RESULT_WRONG_PRIOR_PATH] 比较了不属于该先验路径的目标 {sorted(result_keys - expected_keys)}")
    dimension_results = {
        "character_goal_readability_result": report.character_goal_readability_result,
        "attention_alignment_result": report.attention_alignment_result,
        "spatial_temporal_orientation_result": report.spatial_temporal_orientation_result,
        "affective_alignment_result": report.affective_alignment_result,
        "relationship_change_result": report.relationship_change_result,
        "stakes_readability_result": report.stakes_readability_result,
        "pressure_rhythm_result": report.pressure_rhythm_result,
        "action_functional_repetition_result": report.action_functional_repetition_result,
        "next_expectation_result": report.next_expectation_result,
        "intentional_ambiguity_result": report.intentional_ambiguity_result,
    }
    if report.decision == "pass":
        observed_evidence = {
            evidence_id
            for observation in observations
            for evidence_id in observation.spontaneous_supporting_evidence_ids
        }
        for field, value in dimension_results.items():
            if not value:
                errors.append(f"[REVIEW_DIMENSION_MISSING] pass 报告缺少 {field}；不适用也必须说明")
                continue
            applicability = value.get("applicability")
            if applicability not in {"applies", "not_applicable"}:
                errors.append(f"[REVIEW_DIMENSION_APPLICABILITY_INVALID] {field}.applicability 非法")
            if not _norm(value.get("reason")):
                errors.append(f"[REVIEW_DIMENSION_REASON_MISSING] {field} 缺少可审计理由")
            evidence_ids = list(value.get("evidence_ids") or [])
            _require_refs(evidence_ids, index.evidence, errors, field)
            if not set(evidence_ids).issubset(observed_evidence):
                errors.append(f"[REVIEW_DIMENSION_EVIDENCE_NOT_OBSERVED] {field} 引用了冷观众未实际观察的证据")
            if applicability == "applies":
                if value.get("passed") is not True:
                    errors.append(f"[REVIEW_DIMENSION_FALSE_PASS] {field} 明确未通过，总决策不得判 pass")
                if not evidence_ids:
                    errors.append(f"[REVIEW_DIMENSION_EVIDENCE_MISSING] {field} 判定通过却没有冻结观察证据")
        functional_repeats = [
            audit
            for audit in index.action_audits.values()
            if audit.semantically_equivalent and audit.functional_repeat is True
        ]
        repetition_result = report.action_functional_repetition_result
        if functional_repeats and repetition_result.get("applicability") != "applies":
            errors.append(
                "[REVIEW_ACTION_REPETITION_NOT_AUDITED] 存在功能性重复动作，"
                "但冷观众报告将该维度标记为不适用"
            )

        low = report.low_percentile_result
        if not low:
            errors.append("[REVIEW_DIMENSION_MISSING] pass 报告缺少 low_percentile_result")
        else:
            if low.get("passed") is not True:
                errors.append("[REVIEW_LOW_PERCENTILE_FALSE_PASS] 低分位未通过，总决策不得判 pass")
            if not _norm(low.get("reason")):
                errors.append("[REVIEW_LOW_PERCENTILE_REASON_MISSING] low_percentile_result 缺少理由")
            per_prior = low.get("per_prior")
            if not isinstance(per_prior, dict) or set(per_prior) != set(index.priors):
                errors.append("[REVIEW_LOW_PERCENTILE_PRIORS_MISSING] low_percentile_result 必须逐先验覆盖")
            else:
                expected_by_prior: defaultdict[str, set[str]] = defaultdict(set)
                for prior_id, delta_id in expected_keys:
                    expected_by_prior[prior_id].add(delta_id)
                for prior_id, value in per_prior.items():
                    if not isinstance(value, dict) or value.get("passed") is not True:
                        errors.append(f"[REVIEW_LOW_PERCENTILE_PRIOR_FAILED] {prior_id} 未通过，不得用平均分替代")
                        continue
                    if set(value.get("target_delta_ids") or []) != expected_by_prior[prior_id]:
                        errors.append(f"[REVIEW_LOW_PERCENTILE_PRIOR_TARGETS_MISMATCH] {prior_id} 没有覆盖其全部目标变化")
    if report.decision == "pass" and (failed or expected_keys != result_keys):
        errors.append("[REVIEW_FALSE_PASS] 仍有低分位路径未满足，报告不得判 pass")
    if report.decision not in {"pass", "revise", "needs_human_review"}:
        errors.append("[REVIEW_DECISION_INVALID] decision 非法")
    return list(dict.fromkeys(errors))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, floor((len(ordered) - 1) * percentile)))
    return ordered[index]


def compute_narrative_metrics(
    screenplay: EpisodeScreenplay,
    board: Storyboard,
    report: NarrativeReviewReport | None = None,
    *,
    outline: StoryboardOutline | None = None,
    observations: list[BlindAudienceObservation] | None = None,
    human_calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return auditable metrics; unknown denominators stay ``None``, never 100%."""
    plan = screenplay.narrative_plan
    if plan is None:
        return {"contract_present": False, "narrative_ready": False}
    index_errors: list[str] = []
    index = index_narrative_plan(plan, index_errors)
    validation_errors = validate_storyboard_narrative(
        board, screenplay, outline=outline, complete=True,
    )
    contributions = [shot.shot_contribution for shot in board.shots if shot.shot_contribution]
    delivered_deltas = {
        delta_id
        for contribution in contributions
        for delta_id in contribution.target_delta_ids
    }
    all_deltas = set(index.deltas)
    action_owners = [shot.primary_action_id for shot in board.shots if shot.primary_action_id]
    duplicate_actions = len(action_owners) - len(set(action_owners))
    available_by_delta: defaultdict[str, float] = defaultdict(float)
    for window in plan.readability_windows:
        for delta_id in window.target_delta_ids:
            available_by_delta[delta_id] += max(0.0, window.planned_available_s)
    processing_debts = [
        max(0.0, delta.required_processing_s - available_by_delta.get(delta_id, 0.0))
        for delta_id, delta in index.deltas.items()
    ]
    prior_scores: dict[str, float] = {}
    if report:
        totals: defaultdict[str, int] = defaultdict(int)
        passed: defaultdict[str, int] = defaultdict(int)
        for result in report.target_delta_results:
            totals[result.audience_prior_id] += 1
            passed[result.audience_prior_id] += int(result.result == "satisfied")
        prior_scores = {
            prior_id: passed[prior_id] / total
            for prior_id, total in totals.items()
            if total
        }
    delivery_ratio = len(delivered_deltas & all_deltas) / len(all_deltas) if all_deltas else None
    contribution_ratio = (
        sum(_contribution_nonempty(shot.shot_contribution) for shot in board.shots) / len(board.shots)
        if board.shots else None
    )
    low = _percentile(list(prior_scores.values()), 0.1)
    expected_review_keys = {
        (path.audience_prior_id, delta.target_delta_id)
        for intent in plan.experience_intents
        for path in intent.audience_paths
        for delta in path.target_deltas
    }
    actual_review_keys = {
        (result.audience_prior_id, result.target_delta_id)
        for result in (report.target_delta_results if report else [])
    }
    calibration_ready = bool(
        human_calibration
        and human_calibration.get("ready") is True
        and human_calibration.get("status") == "calibrated"
    )
    ready = (
        not validate_storyboard_narrative(board, screenplay, complete=True)
        and report is not None
        and observations is not None
        and not validate_blind_review(screenplay, observations, report)
        and report.decision == "pass"
        and bool(report.observation_ids)
        and actual_review_keys == expected_review_keys
        and all(result.result == "satisfied" for result in report.target_delta_results)
        and calibration_ready
    )
    def ratio(numerator: int | float, denominator: int | float) -> float | None:
        return float(numerator) / float(denominator) if denominator else None

    adapted = {
        proposition_id for proposition_id, proposition in index.propositions.items()
        if proposition.narrative_domain == "adapted_story"
    }
    mapped_adapted = {
        proposition_id
        for decision in index.decisions.values()
        for proposition_id in decision.adapted_proposition_ids
    }
    covered_events = {
        event_id for shot in board.shots for event_id in (shot.event_ids or [])
    }
    applicable_scenes = [
        scene for scene in index.scenes.values()
        if scene.applicability == "applies"
    ]
    scene_passes = sum(
        all((
            bool(scene.scene_question_id),
            bool(scene.goal_proposition_ids),
            bool(scene.obstacle_proposition_ids),
            bool(scene.stakes_proposition_ids),
            bool(scene.pressure_curve),
            bool(scene.turn_event_ids or scene.scene_button),
            bool(_norm(scene.value_polarity_in)),
            bool(_norm(scene.value_polarity_out)),
            {path.audience_prior_id for path in scene.audience_state_paths}
            == set(index.priors),
        ))
        for scene in applicable_scenes
    )
    applicable_arcs = [
        arc for arc in index.arcs.values()
        if arc.applicability == "applies"
    ]
    arc_passes = sum(
        all((
            bool(arc.core_question_ids or arc.promise_proposition_ids),
            bool(arc.escalation_event_ids),
            bool(arc.climax_event_ids),
            bool(arc.pressure_curve),
            bool(arc.information_density_curve),
            bool(arc.processing_beats),
        ))
        for arc in applicable_arcs
    )
    payoff_closed = sum(
        payoff.status in {"preserved", "paid_off", "intentionally_carried"}
        for payoff in index.payoffs.values()
    )
    intent_covered = sum(
        bool(intent.audience_paths)
        and {path.audience_prior_id for path in intent.audience_paths} == set(index.priors)
        and all(path.target_deltas for path in intent.audience_paths)
        for intent in index.intents.values()
    )
    first_event_position: dict[str, int] = {}
    task_owner_positions: defaultdict[str, list[int]] = defaultdict(list)
    for position, shot in enumerate(board.shots):
        for event_id in shot.event_ids:
            first_event_position.setdefault(event_id, position)
        if shot.shot_contribution is not None:
            for task_id in shot.shot_contribution.assimilation_task_ids:
                task_owner_positions[task_id].append(position)
    deadline_satisfied_tasks: set[str] = set()
    for task_id, task in index.tasks.items():
        owners = task_owner_positions.get(task_id, [])
        if len(owners) != 1 or task.status == "needs_review":
            continue
        deadline_ids = list(task.downstream_dependency_event_ids)
        delta = index.deltas.get(task.target_delta_id)
        if delta is not None:
            deadline_ids.append(delta.deadline_event_id)
        deadlines = [
            first_event_position[event_id]
            for event_id in deadline_ids
            if event_id in first_event_position
        ]
        if not deadlines or owners[0] <= min(deadlines):
            deadline_satisfied_tasks.add(task_id)
    readability_violations = sum(
        window.planned_available_s < window.scheduled_processing_s
        for window in (outline.readability_windows if outline and outline.readability_windows else plan.readability_windows)
    )
    capacity_markers = (
        "SHOT_CAPACITY_",
        "SHOT_ACTION_CAPACITY_",
        "SHOT_SPOKEN_TEXT_CAPACITY_",
        "SHOT_INFERENCE_CAPACITY_",
        "SHOT_ATTENTION_CAPACITY_",
        "SHOT_REACTION_CAPACITY_",
        "SHOT_SPATIAL_CAPACITY_",
        "SHOT_JOINT_CAPACITY_",
    )
    action_capacity_violations = sum(
        any(marker in error for marker in capacity_markers)
        for error in validation_errors
    )
    empty_contributions = sum(
        not _contribution_nonempty(shot.shot_contribution) for shot in board.shots
    )
    results_by_dimension: defaultdict[str, list[bool]] = defaultdict(list)
    # A target delta may be observed for more than one audience prior.  Keep
    # every prior-specific observation; reducing to ``delta_id -> result``
    # would silently let the last row replace a weaker audience path.
    for result in (report.target_delta_results if report else []):
        delta = index.deltas.get(result.target_delta_id)
        if delta is not None:
            results_by_dimension[delta.dimension].append(
                result.result == "satisfied"
            )

    def dimension_rate(*dimensions: str) -> float | None:
        values = [
            passed
            for dimension in dimensions
            for passed in results_by_dimension.get(dimension, [])
        ]
        return ratio(sum(values), len(values))

    def report_rate(value: dict[str, Any]) -> float | None:
        if not value:
            return None
        for key in ("rate", "score", "alignment_rate", "pass_rate"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)):
                number = float(candidate)
                return number / 100.0 if number > 1 else number
        passed = value.get("passed")
        total = value.get("total")
        if isinstance(passed, (int, float)) and isinstance(total, (int, float)):
            return ratio(passed, total)
        return None

    def first_defined(primary: float | None, fallback: float | None) -> float | None:
        return primary if primary is not None else fallback

    false_causal_rate = None
    if report and report.target_delta_results:
        false_causal_rate = ratio(
            len(report.unintended_inference_ids), len(report.target_delta_results)
        )
    withheld = {
        item.proposition_id: item
        for intent in plan.experience_intents
        for item in intent.withheld_propositions
    }
    evidence_by_id = index.evidence
    event_order = {event_id: number for number, event_id in enumerate(index.events)}
    premature_count = 0
    withheld_delivery_count = 0
    for shot in board.shots:
        contribution = shot.shot_contribution
        if not contribution:
            continue
        current_event_position = max(
            (event_order.get(event_id, -1) for event_id in shot.event_ids),
            default=-1,
        )
        for evidence_id in contribution.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            for proposition_id in (evidence.supports_proposition_ids if evidence else []):
                contract = withheld.get(proposition_id)
                if not contract:
                    continue
                withheld_delivery_count += 1
                disclosure = contract.future_disclosure_anchor
                if disclosure is None or disclosure.type != "event":
                    premature_count += 1
                elif current_event_position < event_order.get(disclosure.id, len(event_order)):
                    premature_count += 1
    windows = outline.readability_windows if outline and outline.readability_windows else plan.readability_windows
    attention_collision_count = sum(
        bool(window.competing_attention_ids)
        and window.planned_available_s < window.scheduled_processing_s
        for window in windows
    )
    bridge_plans = list(outline.cognitive_bridge_plans if outline else [])
    bridge_gains = [
        float(item.marginal_gain_result.get("expected_gain"))
        for item in bridge_plans
        if isinstance(item.marginal_gain_result.get("expected_gain"), (int, float))
    ]
    ineffective_bridges = sum(
        item.marginal_gain_result.get("passed") is False
        or item.deletion_test_result.get("deletion_is_lossless") is True
        for item in bridge_plans
    )
    correlation = None
    if human_calibration:
        if isinstance(human_calibration.get("calibration_score"), (int, float)):
            correlation = float(human_calibration["calibration_score"])
        elif human_calibration.get("ai_scores") or human_calibration.get("human_scores"):
            correlation = blind_ai_human_comprehension_correlation(
                list(human_calibration.get("ai_scores") or []),
                list(human_calibration.get("human_scores") or []),
                min_samples=int(human_calibration.get("min_samples") or 8),
            )["correlation"]

    metrics = {
        "contract_present": True,
        "proposition_mapping_coverage_rate": ratio(len(adapted & mapped_adapted), len(adapted)),
        "event_coverage_rate": ratio(len(set(index.events) & covered_events), len(index.events)),
        "unbound_reference_count": len(index_errors) + sum(
            "REF_MISSING" in error for error in validation_errors
        ),
        "event_order_violation_count": sum(
            "EVENT_CAUSAL_ORDER" in error or "EVENT_DAG_CYCLE" in error
            for error in validation_errors
        ),
        "duplicate_primary_action_count": duplicate_actions,
        "state_regression_count": sum(
            marker in error
            for error in validation_errors
            for marker in ("STATE_REGRESSION", "COMPLETED_ACTION_REPLAY", "FORBIDDEN_ACTION_REPLAY")
        ),
        "character_motivation_gap_count": sum(
            "CHARACTER_DECISION" in error or "CHARACTER_BELIEF_WITHOUT" in error
            for error in validation_errors
        ),
        "readability_window_violation_count": readability_violations,
        "shot_capacity_violation_count": action_capacity_violations + readability_violations,
        "empty_shot_contribution_count": empty_contributions,
        "scene_contract_pass_rate": ratio(scene_passes, len(applicable_scenes)),
        "arc_contract_pass_rate": ratio(arc_passes, len(applicable_arcs)),
        "setup_payoff_closure_rate": ratio(payoff_closed, len(index.payoffs)),
        "experience_intent_coverage_rate": ratio(intent_covered, len(index.intents)),
        "assimilation_deadline_pass_rate": ratio(
            len(deadline_satisfied_tasks), len(index.tasks)
        ),
        "cold_audience_target_belief_rate": dimension_rate("belief"),
        "cold_audience_false_causal_inference_rate": false_causal_rate,
        "character_goal_readability_rate": first_defined(
            report_rate(report.character_goal_readability_result) if report else None,
            dimension_rate("character_goal"),
        ),
        "spatial_temporal_orientation_rate": first_defined(
            report_rate(report.spatial_temporal_orientation_result) if report else None,
            dimension_rate("spatial_temporal"),
        ),
        "cold_audience_affective_alignment_rate": first_defined(
            report_rate(report.affective_alignment_result) if report else None,
            dimension_rate("affective"),
        ),
        "relationship_change_readability_rate": (
            report_rate(report.relationship_change_result) if report else None
        ),
        "stakes_readability_rate": (
            report_rate(report.stakes_readability_result) if report else None
        ),
        "pressure_rhythm_alignment_rate": (
            report_rate(report.pressure_rhythm_result) if report else None
        ),
        "action_functional_repetition_rate": (
            report_rate(report.action_functional_repetition_result) if report else None
        ),
        "next_expectation_alignment_rate": (
            report_rate(report.next_expectation_result) if report else None
        ),
        "intentional_ambiguity_fidelity_rate": (
            report_rate(report.intentional_ambiguity_result) if report else None
        ),
        "premature_reveal_rate": ratio(premature_count, withheld_delivery_count),
        "attention_collision_rate": ratio(attention_collision_count, len(windows)),
        "audience_processing_debt": sum(processing_debts),
        "cold_audience_inference_variance": report.inference_variance if report else None,
        "cognitive_bridge_marginal_gain": (
            sum(bridge_gains) / len(bridge_gains) if bridge_gains else None
        ),
        "ineffective_bridge_shot_rate": ratio(ineffective_bridges, len(bridge_plans)),
        "blind_ai_human_comprehension_correlation": correlation,
        # Backward-compatible operational aliases used by the existing UI.
        "target_delta_delivery_ratio": delivery_ratio,
        "shot_contribution_coverage": contribution_ratio,
        "audience_processing_debt_s": sum(processing_debts),
        "max_audience_processing_debt_s": max(processing_debts) if processing_debts else None,
        "per_prior_understanding": prior_scores,
        "low_percentile_understanding": low,
        "inference_variance": report.inference_variance if report else None,
        "narrative_ready": ready,
    }
    return metrics


def blind_ai_human_comprehension_correlation(
    ai_scores: list[float],
    human_scores: list[float],
    *,
    min_samples: int = 8,
) -> dict[str, Any]:
    """Cross-genre calibration primitive with explicit insufficient-sample state."""
    if len(ai_scores) != len(human_scores) or len(ai_scores) < max(2, min_samples):
        return {
            "status": "needs_review",
            "sample_count": min(len(ai_scores), len(human_scores)),
            "correlation": None,
        }
    x = [float(value) for value in ai_scores]
    y = [float(value) for value in human_scores]
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    numerator = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    denominator_x = sum((a - mean_x) ** 2 for a in x) ** 0.5
    denominator_y = sum((b - mean_y) ** 2 for b in y) ** 0.5
    if denominator_x == 0 or denominator_y == 0:
        return {
            "status": "needs_review",
            "sample_count": len(x),
            "correlation": None,
        }
    return {
        "status": "calibrated",
        "sample_count": len(x),
        "correlation": numerator / (denominator_x * denominator_y),
    }


def narrative_review_passes(report: NarrativeReviewReport | None) -> bool:
    return bool(report and report.decision == "pass" and all(
        result.result == "satisfied" for result in report.target_delta_results
    ))


def audience_perceptual_surface_hash(payload: dict[str, Any]) -> str:
    """Return the stable identity of one exact audience-facing payload."""
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _audible_timeline_as_heard(shot: Any) -> list[dict[str, Any]]:
    """Project sound into what a viewer can hear, without production-only voice data."""
    if shot.audio_timeline:
        return [
            {
                "start_s": float(item.start_s),
                "end_s": float(item.end_s),
                "sound_type": item.type,
                "speaker": item.speaker_id,
                "content": item.text,
                "lip_sync_visible": bool(item.lip_sync),
                "performance_emotion": item.emotion,
            }
            for item in shot.audio_timeline
        ]
    # A missing timing contract must remain visible to the cold review.  Keep
    # the dialogue audible, but represent its timing as unknown instead of
    # inventing a schedule that could make an unreadable shot look valid.
    return [
        {
            "start_s": None,
            "end_s": None,
            "sound_type": dialogue.delivery,
            "speaker": dialogue.speaker,
            "content": dialogue.line,
            "lip_sync_visible": dialogue.delivery == "spoken_dialogue",
            "performance_emotion": dialogue.emotion,
        }
        for dialogue in shot.dialogues
    ]


def _on_screen_text_as_seen(shot: Any) -> list[dict[str, Any]]:
    required = shot.required_text
    if required is None or not _norm(required.exact_text):
        return []
    return [{
        "surface": required.surface,
        "content": required.exact_text,
        "appear_start_s": float(required.appear_start_s),
        "stable_until_s": (
            float(required.stable_until_s)
            if required.stable_until_s is not None
            else None
        ),
        "visual_style": required.style,
        "reading_priority": required.reading_priority,
    }]


def audience_perceptual_surface(
    prior: AudiencePriorContract,
    screenplay: EpisodeScreenplay,
    board: Storyboard,
) -> dict[str, Any]:
    """Build the canonical, deliberately target-free surface seen by a viewer.

    Only perceivable picture, sound, text and edit information is projected.
    The screenplay is used solely to resolve context explicitly granted by the
    audience prior; unfilmed screenplay content and all director targets remain
    outside this payload.  This is intentionally a reusable boundary so the
    video compiler can consume the same surface in a later integration.
    """
    plan = screenplay.narrative_plan
    proposition_by_id = {
        item.proposition_id: item for item in (plan.propositions if plan else [])
    }
    remembered_context = [
        proposition_by_id[proposition_id].canonical_statement
        for proposition_id in prior.assumed_known_proposition_ids
        if proposition_id in proposition_by_id
    ]
    ordered_shots: list[dict[str, Any]] = []
    for index, shot in enumerate(board.shots):
        previous = board.shots[index - 1] if index > 0 else None
        ordered_shots.append({
            "shot_id": shot.shot_id or f"shot-{shot.shot_no}",
            "shot_no": int(shot.shot_no),
            "duration_s": int(shot.duration_s),
            "scene_as_seen": {
                "time": shot.scene_time,
                "name": shot.scene_name,
                "setting": shot.scene_setting,
            },
            "visible_characters": list(shot.characters_visible or shot.characters),
            "visual_track": {
                "first_frame": shot.first_frame_desc,
                "visible_action": shot.action_desc,
                "last_frame": shot.last_frame_desc,
            },
            "camera_as_seen": {
                "shot_size": shot.shot_size,
                "angle": shot.camera_angle,
                "movement": shot.camera_move,
                "spatial_anchor": shot.spatial_anchor,
            },
            "edit_as_seen": {
                "incoming_transition": (
                    "episode_open"
                    if previous is None
                    else (previous.transition or "硬切")
                ),
                "continuity_from_previous": bool(shot.continuity_from_prev),
                "outgoing_transition": shot.transition or "硬切",
                "episode_end_after_shot": index == len(board.shots) - 1,
            },
            "audible_timeline": _audible_timeline_as_heard(shot),
            "on_screen_text_timeline": _on_screen_text_as_seen(shot),
            # Opaque handles let a cold reader cite what it actually used
            # without revealing the evidence claim or director target.
            "observable_evidence_handles": list(
                shot.shot_contribution.evidence_ids
                if shot.shot_contribution else []
            ),
        })

    return {
        "perceptual_surface_version": AUDIENCE_PERCEPTUAL_SURFACE_VERSION,
        "audience_prior": {
            "audience_prior_id": prior.audience_prior_id,
            "audience_description": prior.audience_description,
            "remembered_context_as_seen": remembered_context,
            "familiarity_assumptions": list(prior.familiarity_assumptions),
            "language_and_context_assumptions": list(prior.language_and_context_assumptions),
            "attention_memory_assumptions": dict(prior.attention_memory_assumptions),
        },
        "ordered_storyboard_as_seen": ordered_shots,
        "instructions": (
            "先独立自由复述你一次观看后自然记住的实体、因果、人物目标、问题和下一步预期；"
            "冻结自由复述后，才可记录中性追问观察。不要猜测创作者目标。"
        ),
    }


def blind_reader_payload(
    prior: AudiencePriorContract,
    screenplay: EpisodeScreenplay,
    board: Storyboard,
) -> dict[str, Any]:
    """Backward-compatible name for the canonical audience surface."""
    return audience_perceptual_surface(prior, screenplay, board)
