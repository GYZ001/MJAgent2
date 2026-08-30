"""Narrative-plan ID index and action-participant delivery validation.

Moved verbatim out of the pre-split ``app/narrative.py`` (see
``app/narrative/__init__.py`` for the package-split rationale).
``index_narrative_plan`` and ``action_participant_delivery_errors`` are both
called from ``.screenplay_validate`` and ``.storyboard_validate``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas import EpisodeScreenplay, NarrativeContinuityPlan

from .primitives import _ids, _norm


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


def action_participant_delivery_errors(
    screenplay: EpisodeScreenplay,
) -> list[str]:
    """Validate typed evidence for action participants that are not onscreen."""
    plan = screenplay.narrative_plan
    if plan is None:
        return []
    index = index_narrative_plan(plan)
    event_by_action = {
        action_id: event
        for event in plan.events
        for action_id in event.action_ids
    }
    offscreen_only_ids = {
        identity.identity_id
        for identity in plan.identity_contracts
        if identity.visual_policy == "offscreen_only"
    }
    errors: list[str] = []
    for action in plan.atomic_actions:
        action_id = _norm(action.action_id)
        participants = {
            _norm(participant_id)
            for participant_id in [*action.actor_ids, *action.target_ids]
            if _norm(participant_id)
        }
        if action.action_agency.identity_bearing != bool(participants):
            errors.append(
                f"[ACTION_AGENCY_PARTICIPANT_MISMATCH] {action_id} 的 "
                "identity_bearing 与 actor/target 分区不等价"
            )
        if action.action_agency.is_character_agency and not participants:
            errors.append(
                f"[ACTION_AGENCY_CHARACTER_RELATION_MISSING] {action_id} 的 "
                "character agency 缺少 actor/target 结构关系"
            )
        if (
            not action.action_agency.identity_bearing
            and not action.action_agency.source_segment_ids
        ):
            errors.append(
                f"[ACTION_AGENCY_PROVENANCE_MISSING] {action_id} 的非人物动作"
                "缺少 source_segment_ids"
            )
        owner_event = event_by_action.get(action_id)
        if owner_event is None:
            offscreen_participants = participants & offscreen_only_ids
        elif "onscreen_entity_ids" in owner_event.model_fields_set:
            offscreen_participants = participants - {
                _norm(participant_id)
                for participant_id in owner_event.onscreen_entity_ids
                if _norm(participant_id)
            }
        else:
            offscreen_participants = participants & offscreen_only_ids

        deliveries_by_participant: dict[str, Any] = {}
        for delivery in action.participant_deliveries:
            participant_id = _norm(delivery.participant_id)
            label = f"{action_id}/{participant_id or 'unknown'}"
            if _norm(delivery.action_id) != action_id:
                errors.append(
                    f"[ACTION_PARTICIPANT_DELIVERY_ACTION_MISMATCH] {label} "
                    f"声明 action_id={delivery.action_id}"
                )
            if not participant_id or participant_id not in participants:
                errors.append(
                    f"[ACTION_PARTICIPANT_DELIVERY_PARTICIPANT_INVALID] {label} "
                    "不是该动作的 actor/target"
                )
                continue
            if participant_id in deliveries_by_participant:
                errors.append(
                    f"[ACTION_PARTICIPANT_DELIVERY_DUPLICATE] {label} "
                    "存在重复交付合同"
                )
                continue
            deliveries_by_participant[participant_id] = delivery
            if not delivery.is_perceivable:
                errors.append(
                    f"[ACTION_PARTICIPANT_DELIVERY_CHANNEL_MISSING] {label} "
                    "必须结构化声明可听、可见影响或可见反应"
                )
            evidence_ids = [
                _norm(evidence_id)
                for evidence_id in delivery.evidence_ids
                if _norm(evidence_id)
            ]
            if not evidence_ids:
                errors.append(
                    f"[ACTION_PARTICIPANT_DELIVERY_EVIDENCE_MISSING] {label} "
                    "没有绑定可感知 evidence_id"
                )
                continue
            for evidence_id in evidence_ids:
                evidence = index.evidence.get(evidence_id)
                if evidence is None:
                    errors.append(
                        f"[ACTION_PARTICIPANT_DELIVERY_EVIDENCE_INVALID] {label} "
                        f"引用不存在的 {evidence_id}"
                    )
                    continue
                if (
                    owner_event is not None
                    and (
                        evidence.anchor.type != "event"
                        or evidence.anchor.id != owner_event.event_id
                    )
                ):
                    errors.append(
                        f"[ACTION_PARTICIPANT_DELIVERY_EVIDENCE_ANCHOR_MISMATCH] "
                        f"{label}/{evidence_id} 未锚定动作 owner 事件 "
                        f"{owner_event.event_id}"
                    )
                if "audience" not in evidence.perceivable_by:
                    errors.append(
                        f"[ACTION_PARTICIPANT_DELIVERY_NOT_PERCEIVABLE] "
                        f"{label}/{evidence_id} 未声明观众可感知"
                    )

        missing = offscreen_participants - set(deliveries_by_participant)
        for participant_id in sorted(missing):
            errors.append(
                f"[ACTION_PARTICIPANT_DELIVERY_MISSING] {action_id}/"
                f"{participant_id} 未入画且没有结构化可感知证据合同"
            )
    return list(dict.fromkeys(errors))
