"""Semantic picture-spine projection and executable delivery-beat merging."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from app import config
from app.errors import ArtifactNeedsRebuildError
from app.schemas import (
    AudienceStatePathRef,
    EpisodeScreenplay,
    NARRATIVE_CONTRACT_VERSION,
    NarrativeBoundaryContract,
    ShotCapacityBudget,
    ShotContribution,
    StoryboardOutline,
    StoryboardOutlineShot,
)


_CAPACITY_FIELDS = (
    "action_phase_s",
    "spoken_and_text_s",
    "attention_switch_s",
    "inference_processing_s",
    "reaction_registration_s",
    "spatial_reorientation_s",
    "entry_exit_settle_s",
    "other_s",
)


def _dedupe(values) -> list:
    return list(dict.fromkeys(value for value in values if value not in (None, "")))


def _anchor_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(getattr(value, "id", "") or "")


def _set_anchor_id(value: Any, event_id: str) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        value["id"] = event_id
    else:
        value.id = event_id


def _source_ids(value: str) -> set[str]:
    return set(re.findall(r"SRC\d+", str(value or "")))


def _filter_state_fragment(
    value: Any,
    *,
    proposition_ids: set[str],
    evidence_ids: set[str],
) -> Any:
    if isinstance(value, list):
        return [
            filtered
            for item in value
            if not (
                isinstance(item, dict)
                and item.get("proposition_id") in proposition_ids
            )
            for filtered in [
                _filter_state_fragment(
                    item,
                    proposition_ids=proposition_ids,
                    evidence_ids=evidence_ids,
                )
            ]
        ]
    if isinstance(value, dict):
        filtered: dict[str, Any] = {}
        for key, item in value.items():
            if key in proposition_ids:
                continue
            if key == "evidence_ids" and isinstance(item, list):
                filtered[key] = [
                    evidence_id
                    for evidence_id in item
                    if evidence_id not in evidence_ids
                ]
                continue
            filtered[key] = _filter_state_fragment(
                item,
                proposition_ids=proposition_ids,
                evidence_ids=evidence_ids,
            )
        return filtered
    return value


def _scene_event_groups(screenplay: EpisodeScreenplay) -> list[list[str]]:
    plan = screenplay.narrative_plan
    scenes = list(screenplay.scene_outline or [])
    events = list(plan.events if plan is not None else [])
    contracts = list(plan.scene_contracts if plan is not None else [])
    if not events or len(scenes) != len(contracts):
        return []
    event_positions = {
        event.event_id: position
        for position, event in enumerate(events)
    }
    groups: list[list[str]] = []
    cursor = 0
    for scene_index, contract in enumerate(contracts):
        remaining_scenes = len(scenes) - scene_index - 1
        latest_allowed = len(events) - remaining_scenes - 1
        declared = [
            event_positions[event_id]
            for event_id in contract.turn_event_ids
            if (
                event_id in event_positions
                and cursor <= event_positions[event_id] <= latest_allowed
            )
        ]
        end = (
            len(events) - 1
            if scene_index == len(scenes) - 1
            else min(max(declared), latest_allowed)
            if declared
            else min(cursor, latest_allowed)
        )
        if end < cursor:
            return []
        groups.append([
            events[position].event_id
            for position in range(cursor, end + 1)
        ])
        cursor = end + 1
    return groups if cursor == len(events) else []


def _drop_paratext(
    screenplay: EpisodeScreenplay,
    *,
    excluded_event_ids: set[str],
    scene_groups: list[list[str]],
) -> dict[str, Any]:
    plan = screenplay.narrative_plan
    if plan is None or not excluded_event_ids:
        return {
            "excluded_event_ids": [],
            "excluded_source_segment_ids": [],
        }
    kept_events = [
        event for event in plan.events
        if event.event_id not in excluded_event_ids
    ]
    if not kept_events:
        raise ValueError("成片投影不能移除全部叙事事件")
    last_event_id = kept_events[-1].event_id
    removed_action_ids = {
        action_id
        for event in plan.events
        if event.event_id in excluded_event_ids
        for action_id in event.action_ids
    }
    removed_fact_ids = {
        fact_id
        for event in plan.events
        if event.event_id in excluded_event_ids
        for fact_id in (*event.effects_add, *event.effects_remove)
    }
    kept_fact_ids = {
        fact_id
        for event in kept_events
        for fact_id in (
            *event.precondition_fact_ids,
            *event.effects_add,
            *event.effects_remove,
        )
    }
    removed_fact_ids.difference_update(kept_fact_ids)
    fact_propositions = {
        fact.fact_id: fact.proposition_id
        for fact in plan.state_facts
    }
    removed_proposition_ids = {
        fact_propositions[fact_id]
        for fact_id in removed_fact_ids
        if fact_id in fact_propositions
    }
    removed_proposition_ids.difference_update(
        proposition_id
        for event in kept_events
        for proposition_id in event.proposition_ids
    )
    removed_evidence_ids = {
        evidence.evidence_id
        for evidence in plan.evidence
        if (
            evidence.anchor.type == "event"
            and evidence.anchor.id in excluded_event_ids
        )
    }
    removed_adaptation_ids = {
        decision.adaptation_decision_id
        for decision in plan.adaptation_decisions
        if (
            decision.affected_event_ids
            and set(decision.affected_event_ids).issubset(excluded_event_ids)
        )
    }

    plan.events = kept_events
    for event in plan.events:
        event.causal_parent_ids = [
            value for value in event.causal_parent_ids
            if value not in excluded_event_ids
        ]
        event.downstream_dependency_event_ids = [
            value for value in event.downstream_dependency_event_ids
            if value not in excluded_event_ids
        ]
    plan.atomic_actions = [
        action for action in plan.atomic_actions
        if action.action_id not in removed_action_ids
    ]
    plan.state_facts = [
        fact for fact in plan.state_facts
        if fact.fact_id not in removed_fact_ids
        and fact.proposition_id not in removed_proposition_ids
    ]
    plan.propositions = [
        proposition for proposition in plan.propositions
        if proposition.proposition_id not in removed_proposition_ids
    ]
    retained_decisions = []
    for decision in plan.adaptation_decisions:
        if decision.adaptation_decision_id in removed_adaptation_ids:
            continue
        decision.source_proposition_ids = [
            value for value in decision.source_proposition_ids
            if value not in removed_proposition_ids
        ]
        decision.adapted_proposition_ids = [
            value for value in decision.adapted_proposition_ids
            if value not in removed_proposition_ids
        ]
        decision.protected_causal_effect_ids = [
            value for value in decision.protected_causal_effect_ids
            if value not in removed_proposition_ids
        ]
        decision.affected_event_ids = [
            value for value in decision.affected_event_ids
            if value not in excluded_event_ids
        ]
        retained_decisions.append(decision)
    plan.adaptation_decisions = retained_decisions
    plan.evidence = [
        evidence for evidence in plan.evidence
        if evidence.evidence_id not in removed_evidence_ids
    ]
    plan.character_states = [
        state for state in plan.character_states
        if _anchor_id(state.anchor) not in excluded_event_ids
    ]
    plan.character_beliefs = [
        state for state in plan.character_beliefs
        if _anchor_id(state.anchor) not in excluded_event_ids
    ]
    for question in plan.dramatic_questions:
        question.target_proposition_ids = [
            value for value in question.target_proposition_ids
            if value not in removed_proposition_ids
        ]
        if _anchor_id(question.open_anchor) in excluded_event_ids:
            _set_anchor_id(question.open_anchor, last_event_id)
        if _anchor_id(question.resolution_anchor) in excluded_event_ids:
            _set_anchor_id(question.resolution_anchor, last_event_id)
    for prior in plan.audience_priors:
        prior.assumed_known_proposition_ids = [
            value for value in prior.assumed_known_proposition_ids
            if value not in removed_proposition_ids
        ]
        prior.assumed_unknown_proposition_ids = [
            value for value in prior.assumed_unknown_proposition_ids
            if value not in removed_proposition_ids
        ]
    for state in plan.audience_states:
        if _anchor_id(state.anchor) in excluded_event_ids:
            _set_anchor_id(state.anchor, last_event_id)
        state.beliefs = [
            belief for belief in state.beliefs
            if belief.proposition_id not in removed_proposition_ids
        ]
        state.working_memory = [
            item for item in state.working_memory
            if item.get("proposition_id") not in removed_proposition_ids
        ]
    target_delta_ids: list[str] = []
    for intent in plan.experience_intents:
        intent.anchor_event_ids = [
            value for value in intent.anchor_event_ids
            if value not in excluded_event_ids
        ]
        intent.attention_target_ids = [
            value for value in intent.attention_target_ids
            if value not in removed_proposition_ids
        ]
        intent.withheld_propositions = [
            value for value in intent.withheld_propositions
            if value.proposition_id not in removed_proposition_ids
        ]
        for path in intent.audience_paths:
            for delta in path.target_deltas:
                target_delta_ids.append(delta.target_delta_id)
                delta.proposition_ids = [
                    value for value in delta.proposition_ids
                    if value not in removed_proposition_ids
                ]
                delta.from_state = _filter_state_fragment(
                    delta.from_state,
                    proposition_ids=removed_proposition_ids,
                    evidence_ids=removed_evidence_ids,
                )
                delta.to_state = _filter_state_fragment(
                    delta.to_state,
                    proposition_ids=removed_proposition_ids,
                    evidence_ids=removed_evidence_ids,
                )
                if delta.deadline_event_id in excluded_event_ids:
                    delta.deadline_event_id = last_event_id
                    delta.primary_delivery_window_id = f"RW-{last_event_id.lstrip('E')}"
    for task in plan.assimilation_tasks:
        task.downstream_dependency_event_ids = _dedupe(
            last_event_id if value in excluded_event_ids else value
            for value in task.downstream_dependency_event_ids
        )
        task.required_prior_proposition_ids = [
            value for value in task.required_prior_proposition_ids
            if value not in removed_proposition_ids
        ]
    last_window = None
    windows = []
    for window in plan.readability_windows:
        window.event_ids = [
            value for value in window.event_ids
            if value not in excluded_event_ids
        ]
        window.proposition_ids = [
            value for value in window.proposition_ids
            if value not in removed_proposition_ids
        ]
        window.attention_target_ids = [
            value for value in window.attention_target_ids
            if value not in removed_proposition_ids
        ]
        window.evidence_ids = [
            value for value in window.evidence_ids
            if value not in removed_evidence_ids
        ]
        if not window.event_ids:
            continue
        windows.append(window)
        if last_event_id in window.event_ids:
            last_window = window
    if last_window is not None:
        last_window.target_delta_ids = _dedupe([
            *last_window.target_delta_ids,
            *target_delta_ids,
        ])
        last_window.scheduled_processing_s = max(
            float(last_window.scheduled_processing_s or 0),
            max(
                (
                    float(delta.required_processing_s or 0)
                    for intent in plan.experience_intents
                    for path in intent.audience_paths
                    for delta in path.target_deltas
                    if delta.deadline_event_id == last_event_id
                ),
                default=0.0,
            ),
        )
        last_window.planned_available_s = max(
            float(last_window.planned_available_s or 0),
            float(last_window.scheduled_processing_s or 0),
        )
    plan.readability_windows = windows
    for contract in plan.setup_payoff_contracts:
        contract.setup_event_ids = [
            value for value in contract.setup_event_ids
            if value not in excluded_event_ids
        ]
        contract.payoff_event_ids = _dedupe(
            last_event_id if value in excluded_event_ids else value
            for value in contract.payoff_event_ids
        )
        if contract.retention_deadline_event_id in excluded_event_ids:
            contract.retention_deadline_event_id = last_event_id
        contract.setup_proposition_ids = [
            value for value in contract.setup_proposition_ids
            if value not in removed_proposition_ids
        ]
        contract.intended_inference_ids = [
            value for value in contract.intended_inference_ids
            if value not in removed_proposition_ids
        ]
    for arc in plan.arc_contracts:
        arc.escalation_event_ids = [
            value for value in arc.escalation_event_ids
            if value not in excluded_event_ids
        ]
        arc.climax_event_ids = _dedupe(
            last_event_id if value in excluded_event_ids else value
            for value in arc.climax_event_ids
        )
        arc.pressure_curve = [
            value for value in arc.pressure_curve
            if _anchor_id(value.get("anchor")) not in excluded_event_ids
        ]
        arc.information_density_curve = [
            value for value in arc.information_density_curve
            if _anchor_id(value.get("anchor")) not in excluded_event_ids
        ]
        arc.processing_beats = [
            value for value in arc.processing_beats
            if _anchor_id(value.get("anchor")) not in excluded_event_ids
        ]
    for identity in plan.identity_contracts:
        identity.evidence.proposition_ids = [
            value for value in identity.evidence.proposition_ids
            if value not in removed_proposition_ids
        ]
        identity.evidence.adaptation_decision_ids = [
            value for value in identity.evidence.adaptation_decision_ids
            if value not in removed_adaptation_ids
        ]

    kept_scene_indexes = [
        index
        for index, event_ids in enumerate(scene_groups)
        if any(event_id not in excluded_event_ids for event_id in event_ids)
    ]
    plan.scene_contracts = [
        contract for index, contract in enumerate(plan.scene_contracts)
        if index in kept_scene_indexes
    ]
    screenplay.scene_outline = [
        scene for index, scene in enumerate(screenplay.scene_outline)
        if index in kept_scene_indexes
    ]
    excluded_legacy_events = [
        event for event in screenplay.events
        if event.event_id in excluded_event_ids
    ]
    excluded_source_ids = set().union(
        *(_source_ids(event.source_span) for event in excluded_legacy_events),
    )
    screenplay.events = [
        event for event in screenplay.events
        if event.event_id not in excluded_event_ids
    ]
    excluded_info_ids = {
        item.info_id
        for item in screenplay.information_ledger
        if item.event_id in excluded_event_ids
    }
    screenplay.information_ledger = [
        item for item in screenplay.information_ledger
        if item.event_id not in excluded_event_ids
    ]
    if screenplay.plot_spine is not None:
        screenplay.plot_spine.spine_beats = [
            beat for beat in screenplay.plot_spine.spine_beats
            if not (
                set(beat.source_segment_ids).issubset(excluded_source_ids)
                or (
                    beat.information_ids
                    and set(beat.information_ids).issubset(excluded_info_ids)
                )
            )
        ]
        if screenplay.plot_spine.spine_beats:
            screenplay.plot_spine.must_keep_ending = (
                screenplay.plot_spine.spine_beats[-1].turn
            )
        screenplay.key_plot_points = [
            f"{beat.who}{beat.does}，{beat.turn}"
            for beat in screenplay.plot_spine.spine_beats
            if beat.must_keep
        ]
    for decision in screenplay.source_coverage:
        if decision.source_segment_id not in excluded_source_ids:
            continue
        decision.disposition = "audit_only"
        decision.projection_policy = "audit_only"
        decision.beat_ids = []
        decision.duplicate_of = None
        decision.reason = (
            "非剧情旁文本保留在来源覆盖审计中，不进入成片 spine"
        )
    screenplay.episode_premise = (
        screenplay.plot_spine.episode_premise
        if screenplay.plot_spine is not None
        else screenplay.episode_premise
    )
    return {
        "excluded_event_ids": sorted(excluded_event_ids),
        "excluded_source_segment_ids": sorted(excluded_source_ids),
        "removed_proposition_ids": sorted(removed_proposition_ids),
    }


def picture_screenplay_projection(
    screenplay: EpisodeScreenplay,
) -> tuple[EpisodeScreenplay, dict[str, Any]]:
    """Return a non-destructive picture-authority projection."""
    source_plan = screenplay.narrative_plan
    if source_plan is None:
        raise ArtifactNeedsRebuildError(
            artifact_id=str(screenplay.id or ""),
            artifact_type="screenplay",
            reason="缺少 narrative_plan 显式结构语义",
        )
    missing_semantics = [
        f"events[{index}].{field}"
        for index, event in enumerate(source_plan.events)
        for field in (
            "narrative_layer",
            "event_priority",
            "render_policy",
        )
        if field not in event.model_fields_set
    ]
    if (
        source_plan.contract_version != NARRATIVE_CONTRACT_VERSION
        or missing_semantics
    ):
        reason = (
            f"叙事合同为 {source_plan.contract_version or 'missing'}，"
            f"当前要求 {NARRATIVE_CONTRACT_VERSION}"
            if source_plan.contract_version != NARRATIVE_CONTRACT_VERSION
            else "缺少显式事件语义 " + "、".join(missing_semantics[:10])
        )
        raise ArtifactNeedsRebuildError(
            artifact_id=str(screenplay.id or ""),
            artifact_type="screenplay",
            reason=reason,
        )
    projected = EpisodeScreenplay.model_validate(
        screenplay.model_dump(mode="json")
    )
    plan = projected.narrative_plan
    assert plan is not None
    scene_groups = _scene_event_groups(projected)
    excluded = {
        event.event_id
        for event in plan.events
        if (
            event.narrative_layer == "paratext"
            or event.render_policy == "exclude_from_spine"
        )
    }
    report = _drop_paratext(
        projected,
        excluded_event_ids=excluded,
        scene_groups=scene_groups,
    )
    report["legacy_semantics"] = False
    report["story_event_count"] = len(
        projected.narrative_plan.events
        if projected.narrative_plan is not None else []
    )
    return projected, report


def _capacity_values(shot: StoryboardOutlineShot) -> dict[str, float]:
    budget = shot.capacity_budget
    return {
        field: float(getattr(budget, field, 0) or 0)
        for field in _CAPACITY_FIELDS
    }


def _supported_duration(seconds: float) -> int | None:
    required = max(config.VIDEO_DURATION_MIN_S, int(math.ceil(seconds)))
    return next(
        (
            duration
            for duration in sorted(config.ALLOWED_DURATIONS)
            if (
                config.VIDEO_DURATION_MIN_S
                <= duration
                <= config.VIDEO_DURATION_MAX_S
                and duration >= required
            )
        ),
        None,
    )


def _visual_sets_are_compatible(shots: list[StoryboardOutlineShot]) -> bool:
    values = [
        set(shot.visible_entity_ids)
        for shot in shots
        if shot.visible_entity_ids
    ]
    return all(
        left.issubset(right) or right.issubset(left)
        for index, left in enumerate(values)
        for right in values[index + 1:]
    )


def _events_allow_merge(
    left: StoryboardOutlineShot,
    right: StoryboardOutlineShot,
    events: dict[str, Any],
) -> bool:
    left_ids = set(left.event_ids)
    right_ids = set(right.event_ids)
    if left_ids.intersection(right_ids):
        return True
    return any(
        events.get(event_id) is not None
        and events[event_id].render_policy == "merge_adjacent"
        for event_id in left_ids | right_ids
    )


def _can_merge(
    group: list[StoryboardOutlineShot],
    shot: StoryboardOutlineShot,
    events: dict[str, Any],
) -> tuple[bool, int | None]:
    previous = group[-1]
    if previous.scene_id != shot.scene_id:
        return False, None
    if not _events_allow_merge(previous, shot, events):
        return False, None
    candidates = [*group, shot]
    if len({
        speaker
        for item in candidates
        for speaker in item.audio_cast
    }) > 1:
        return False, None
    if not _visual_sets_are_compatible(candidates):
        return False, None
    duration = _supported_duration(sum(
        sum(_capacity_values(item).values())
        for item in candidates
    ))
    return duration is not None, duration


def _merge_contribution(
    base: ShotContribution,
    addition: ShotContribution,
) -> ShotContribution:
    merged = base.model_copy(deep=True)
    for field in (
        "experience_intent_ids",
        "target_delta_ids",
        "assimilation_task_ids",
        "evidence_ids",
        "story_delta_fact_ids",
        "character_state_delta_ids",
        "audience_state_delta_ids",
    ):
        setattr(merged, field, _dedupe([
            *getattr(merged, field),
            *getattr(addition, field),
        ]))
    merged.affective_delta = {
        **merged.affective_delta,
        **addition.affective_delta,
    }
    merged.spatial_temporal_delta = {
        **merged.spatial_temporal_delta,
        **addition.spatial_temporal_delta,
    }
    merged.dramatic_pressure_delta += addition.dramatic_pressure_delta
    return merged


def _merge_paths(
    base: list[AudienceStatePathRef],
    addition: list[AudienceStatePathRef],
) -> list[AudienceStatePathRef]:
    additions = {
        item.audience_prior_id: item
        for item in addition
    }
    return [
        item.model_copy(update={
            "audience_state_out_target_id": (
                additions.get(
                    item.audience_prior_id,
                    item,
                ).audience_state_out_target_id
            ),
        })
        for item in base
    ]


def _merge_into(
    base: StoryboardOutlineShot,
    addition: StoryboardOutlineShot,
    *,
    duration_s: int,
) -> None:
    for field in (
        "beat",
        "covers",
        "primary_action",
        "purpose",
        "resulting_change",
        "readability_focus",
    ):
        setattr(base, field, "；".join(_dedupe([
            str(getattr(base, field) or "").strip(),
            str(getattr(addition, field) or "").strip(),
        ])))
    base.state_out = addition.state_out or base.state_out
    base.event_ids = _dedupe([*base.event_ids, *addition.event_ids])
    base.spine_beat_ids = _dedupe([
        *base.spine_beat_ids,
        *addition.spine_beat_ids,
    ])
    base.key_line_ids = _dedupe([*base.key_line_ids, *addition.key_line_ids])
    base.information_ids = _dedupe([
        *base.information_ids,
        *addition.information_ids,
    ])
    base.new_information_ids = _dedupe([
        *base.new_information_ids,
        *addition.new_information_ids,
    ])
    action_ids = _dedupe([
        base.primary_action_id,
        *base.supporting_action_ids,
        addition.primary_action_id,
        *addition.supporting_action_ids,
    ])
    base.primary_action_id = action_ids[0] if action_ids else None
    base.supporting_action_ids = action_ids[1:]
    base.action_phase_ids = _dedupe([
        *base.action_phase_ids,
        *addition.action_phase_ids,
    ])
    base.visible_entity_ids = sorted(set(
        base.visible_entity_ids
    ) | set(addition.visible_entity_ids))
    base.characters_visible = _dedupe([
        *base.characters_visible,
        *addition.characters_visible,
    ])
    base.audio_cast = _dedupe([*base.audio_cast, *addition.audio_cast])
    visible = set(base.visible_entity_ids)
    base.offscreen_action_actor_ids = sorted((
        set(base.offscreen_action_actor_ids)
        | set(addition.offscreen_action_actor_ids)
    ) - visible)
    base.offscreen_action_target_ids = sorted((
        set(base.offscreen_action_target_ids)
        | set(addition.offscreen_action_target_ids)
    ) - visible)
    offscreen = {
        *base.offscreen_action_actor_ids,
        *base.offscreen_action_target_ids,
    }
    deliveries = [
        *base.action_participant_deliveries,
        *addition.action_participant_deliveries,
    ]
    base.action_participant_deliveries = list({
        (item.action_id, item.participant_id): item
        for item in deliveries
        if item.participant_id in offscreen
    }.values())
    base.planned_state_out_fact_ids = list(
        addition.planned_state_out_fact_ids
    )
    state_in = set(base.planned_state_in_fact_ids)
    state_out = set(base.planned_state_out_fact_ids)
    base.planned_delta_add_fact_ids = sorted(state_out - state_in)
    base.planned_delta_remove_fact_ids = sorted(state_in - state_out)
    base.reserved_future_event_ids = list(
        addition.reserved_future_event_ids
    )
    base.readability_window_ids = _dedupe([
        *base.readability_window_ids,
        *addition.readability_window_ids,
    ])
    base.context_requirement_ids = _dedupe([
        *base.context_requirement_ids,
        *addition.context_requirement_ids,
    ])
    base.audience_state_paths = _merge_paths(
        base.audience_state_paths,
        addition.audience_state_paths,
    )
    if base.shot_contribution is not None and addition.shot_contribution is not None:
        base.shot_contribution = _merge_contribution(
            base.shot_contribution,
            addition.shot_contribution,
        )
    capacity = {
        field: (
            _capacity_values(base)[field]
            + _capacity_values(addition)[field]
        )
        for field in _CAPACITY_FIELDS
    }
    reasons = _dedupe([
        base.capacity_budget.other_reason if base.capacity_budget else "",
        addition.capacity_budget.other_reason if addition.capacity_budget else "",
    ])
    capacity["other_reason"] = "；".join(reasons) or None
    base.capacity_budget = ShotCapacityBudget.model_validate(capacity)
    base.duration_s = duration_s


def _rebuild_boundaries(outline: StoryboardOutline) -> None:
    for index, shot in enumerate(outline.shots):
        shot.shot_no = index + 1
        if index == 0:
            shot.narrative_boundary_from_previous = None
            continue
        previous = outline.shots[index - 1]
        prior_out = {
            item.audience_prior_id: item.audience_state_out_target_id
            for item in previous.audience_state_paths
        }
        current_in = {
            item.audience_prior_id: item.audience_state_in_id
            for item in shot.audience_state_paths
        }
        shot.narrative_boundary_from_previous = (
            NarrativeBoundaryContract.model_validate({
                "boundary_id": f"NB-{previous.shot_id}-{shot.shot_id}",
                "previous_shot_id": previous.shot_id,
                "next_shot_id": shot.shot_id,
                "narrative_relation": "相邻可执行 beat 按事件因果与状态链继续",
                "required_state_invariants": list(
                    shot.planned_state_in_fact_ids
                ),
                "allowed_state_deltas": [],
                "state_delta_transitions": [],
                "forbidden_replay_action_ids": list(
                    shot.completed_before_action_ids
                ),
                "handoff_action_phase_id": None,
                "spatial_orientation_contract": {},
                "temporal_orientation_contract": {},
                "audience_state_handoffs": [
                    {
                        "audience_prior_id": prior_id,
                        "previous_state_out_id": prior_out.get(
                            prior_id,
                            state_id,
                        ),
                        "next_state_in_id": state_id,
                    }
                    for prior_id, state_id in sorted(current_in.items())
                ],
                "affective_handoff": {},
                "cut_motivation": "前一可执行 beat 完成后切换到下一交付任务",
            })
        )


def merge_outline_delivery_beats(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[dict[str, Any]]:
    """Merge adjacent tasks only when their full structured budget fits."""
    plan = screenplay.narrative_plan
    if plan is None or not outline.shots:
        return []
    events = {
        event.event_id: event
        for event in plan.events
    }
    groups: list[list[StoryboardOutlineShot]] = []
    for shot in outline.shots:
        if groups:
            can_merge, duration = _can_merge(groups[-1], shot, events)
            if can_merge and duration is not None:
                groups[-1].append(shot)
                continue
        groups.append([shot])
    if all(len(group) == 1 for group in groups):
        return []

    shot_id_map: dict[str, str] = {}
    merged_shots: list[StoryboardOutlineShot] = []
    changes: list[dict[str, Any]] = []
    for group in groups:
        base = group[0].model_copy(deep=True)
        for addition in group[1:]:
            duration = _supported_duration(
                sum(_capacity_values(base).values())
                + sum(_capacity_values(addition).values())
            )
            if duration is None:
                raise ValueError("已批准 delivery beat 的联合预算无法装入单镜")
            _merge_into(base, addition, duration_s=duration)
            shot_id_map[addition.shot_id] = base.shot_id
        shot_id_map[base.shot_id] = base.shot_id
        merged_shots.append(base)
        if len(group) > 1:
            changes.append({
                "owner_shot_id": base.shot_id,
                "merged_shot_ids": [item.shot_id for item in group],
                "event_ids": list(base.event_ids),
                "duration_s": base.duration_s,
                "reason": "structured_joint_capacity_delivery_beat",
            })
    outline.shots = merged_shots
    for window in outline.readability_windows:
        window.shot_ids = _dedupe(
            shot_id_map.get(shot_id, shot_id)
            for shot_id in window.shot_ids
        )
        window.planned_available_s = min(
            sum(
                float(shot.duration_s or 0)
                for shot in outline.shots
                if shot.shot_id in window.shot_ids
            ),
            max(
                float(window.scheduled_processing_s or 0),
                float(window.planned_available_s or 0),
            ),
        )
    _rebuild_boundaries(outline)
    return changes


def authoritative_outline_duration_s(outline: StoryboardOutline) -> int:
    return sum(int(shot.duration_s or 0) for shot in outline.shots)


def compile_authoritative_delivery_outline(
    screenplay: EpisodeScreenplay,
    *,
    bible=None,
) -> tuple[EpisodeScreenplay, StoryboardOutline, dict[str, Any]]:
    """Build the single deterministic picture and duration authority."""
    from app.narrative_outline import (
        compile_narrative_storyboard_outline,
        normalize_narrative_storyboard_outline,
    )
    from app.validators import (
        assign_outline_delivery_ids,
        normalize_outline_dialogue_ownership,
        normalize_outline_spoken_durations,
        split_outline_on_speaker_changes,
        split_outline_over_action_capacity,
        split_outline_over_key_line_capacity,
        storyboard_shot_count_range,
    )

    projected, projection_report = picture_screenplay_projection(screenplay)
    outline = compile_narrative_storyboard_outline(projected)
    max_shots = storyboard_shot_count_range(0)[1]
    projection_changes = normalize_narrative_storyboard_outline(
        outline,
        projected,
        bible=bible,
    )
    assign_outline_delivery_ids(outline, projected)
    split_changes = [
        *split_outline_over_action_capacity(
            outline,
            max_shots=max_shots,
        ),
        *split_outline_on_speaker_changes(
            outline,
            projected,
            max_shots=max_shots,
        ),
        *split_outline_over_key_line_capacity(
            outline,
            projected,
            max_shots=max_shots,
        ),
        *normalize_outline_dialogue_ownership(
            outline,
            projected,
        ),
    ]
    projection_changes.extend(
        normalize_narrative_storyboard_outline(
            outline,
            projected,
            bible=bible,
        )
    )
    projection_changes.extend(
        normalize_outline_dialogue_ownership(
            outline,
            projected,
        )
    )
    normalize_outline_spoken_durations(outline, projected)
    beat_merge_changes = merge_outline_delivery_beats(
        outline,
        projected,
    )
    return projected, outline, {
        "picture_projection": projection_report,
        "projection_change_count": len(projection_changes),
        "split_change_count": len(split_changes),
        "beat_merge_changes": beat_merge_changes,
        "authoritative_duration_s": authoritative_outline_duration_s(outline),
    }
