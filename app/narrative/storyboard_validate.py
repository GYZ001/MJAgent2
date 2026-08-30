"""Storyboard narrative-graph hard gates.

Moved verbatim out of the pre-split ``app/narrative.py`` (see
``app/narrative/__init__.py`` for the package-split rationale). Three
functions that share one concern: projecting the screenplay narrative
authority onto a storyboard (``validate_storyboard_screenplay_authority``,
which ``validate_storyboard_narrative`` calls directly) and validating shot
contribution / action-delta ownership / audience hand-offs against it
(``validate_storyboard_narrative``, a single ~1,255-line function in the
pre-split source -- moved whole, not decomposed further; see the
``function_lines`` baseline entry for this file in
``app/FILE_CONVENTIONS.toml``). ``_outline_as_shots`` is a private one-line
helper used only by ``validate_storyboard_narrative``. Add new
storyboard-narrative validation logic here, not back into ``app/narrative.py``.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app import config
from app.schemas import (
    EpisodeScreenplay,
    NARRATIVE_CONTRACT_VERSION,
    Storyboard,
    StoryboardOutline,
)
from app.spoken_contract import onscreen_text_for_capacity

from .plan_index import action_participant_delivery_errors, index_narrative_plan
from .primitives import (
    _contribution_nonempty,
    _declared_change_matches,
    _norm,
    _require_refs,
    _state_without_identity,
    _target_state_fragment_matches,
)


def _outline_as_shots(outline: StoryboardOutline) -> list[Any]:
    return list(outline.shots or [])


def validate_storyboard_screenplay_authority(
    screenplay: EpisodeScreenplay,
    *,
    expected_scope_id: str | None = None,
    narrative_authority_required: bool = True,
) -> list[str]:
    """Validate only typed facts needed to project a published screenplay.

    Full screenplay quality belongs to its completion certificate.  Replaying
    that evaluator here used to require a code suppression list and could turn
    authoring findings into paid storyboard retries.  This boundary therefore
    checks only version, scope and stable-ID uniqueness.

    ``narrative_authority_required`` defaults to ``True`` so every existing
    caller keeps its exact legacy behaviour (missing ``narrative_plan`` is
    always a hard failure) unless it explicitly opts out.  The one caller that
    should opt out is a shot/episode whose
    ``resolve_downstream_screenplay(...).narrative_authority_required`` is
    declared ``False`` -- today that is exactly ``episode_prep_pack``
    (screenplay contract 6.0.0+), which has no ``narrative_plan`` concept by
    design.  That flag is a declared fact from the authority resolver, not an
    inference made here: for every ``DownstreamScreenplayContext`` it returns,
    ``narrative_authority_required`` is always exactly
    ``screenplay.narrative_plan is not None`` (see
    ``resolve_current_screenplay_authority``'s ``require_narrative`` guard,
    which raises before returning if a legacy episode's narrative_plan is
    missing when required).  So a caller passing
    ``narrative_authority_required=False`` for a screenplay whose
    narrative_plan is genuinely missing-but-required is not a state this
    resolver can produce; it is not this function's job to re-derive that
    distinction from ``plan is None`` alone.
    """
    plan = screenplay.narrative_plan
    if plan is None:
        if not narrative_authority_required:
            return []
        return [
            "[NARRATIVE_PLAN_MISSING] 分镜不能在缺少剧本叙事合同的情况下投影"
        ]
    errors: list[str] = []
    if plan.contract_version != NARRATIVE_CONTRACT_VERSION:
        errors.append(
            f"[NARRATIVE_VERSION_INVALID] contract_version={plan.contract_version}，"
            f"当前要求 {NARRATIVE_CONTRACT_VERSION}"
        )
    if not _norm(plan.scope_id):
        errors.append("[NARRATIVE_SCOPE_MISSING] narrative_plan.scope_id 不能为空")
    elif expected_scope_id is not None and plan.scope_id != str(expected_scope_id):
        errors.append(
            f"[NARRATIVE_SCOPE_MISMATCH] narrative_plan.scope_id={plan.scope_id} "
            f"不等于当前权威作用域 {expected_scope_id}"
        )
    index_narrative_plan(plan, errors)
    return list(dict.fromkeys(errors))


def validate_storyboard_narrative(
    board: Storyboard | None,
    screenplay: EpisodeScreenplay,
    *,
    outline: StoryboardOutline | None = None,
    complete: bool = True,
    expected_scope_id: str | None = None,
    narrative_authority_required: bool = True,
) -> list[str]:
    """Validate shot contribution, action/delta ownership and audience hand-offs.

    Pass ``complete=False`` while generating a prefix; reference and replay
    invariants still run, but future delivery ownership is not demanded yet.

    See ``validate_storyboard_screenplay_authority`` for why
    ``narrative_authority_required`` defaults to ``True`` and what it means to
    pass ``False``: this whole function -- shot contribution ownership, action/
    delta ownership, audience hand-offs, cold-audience readability windows --
    is a projection of ``narrative_plan``.  A screenplay whose architecture
    (``episode_prep_pack``) never has a ``narrative_plan`` cannot be scored
    against a graph it was never built with; that is not the same failure as a
    legacy screenplay that lost its graph.
    """
    plan = screenplay.narrative_plan
    if plan is None:
        if not narrative_authority_required:
            return []
        return ["[NARRATIVE_PLAN_MISSING] 分镜不能在缺少剧本叙事合同的情况下标记 narrative_ready"]
    errors = validate_storyboard_screenplay_authority(
        screenplay,
        expected_scope_id=expected_scope_id,
        narrative_authority_required=narrative_authority_required,
    )
    errors.extend(action_participant_delivery_errors(screenplay))
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
    operational_scene_ids = {
        f"SC{int(scene.scene_no):02d}"
        for scene in screenplay.scene_outline
        if int(scene.scene_no or 0) > 0
    }
    if outline is not None:
        operational_scene_ids.update(
            _norm(context.scene_id)
            for context in outline.scene_contexts
            if _norm(context.scene_id)
        )
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
    from app.identity_contracts import storyboard_action_relation_ids

    action_relations = {
        action_id: storyboard_action_relation_ids(
            screenplay,
            action_event_owner.get(action_id, ""),
            action,
        )
        for action_id, action in index.actions.items()
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
        shot_no = int(getattr(shot, "shot_no", position + 1) or position + 1)
        label = shot_id or f"shot_no={shot_no}"
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
        if index.scenes or operational_scene_ids:
            if not scene_id:
                errors.append(f"[SHOT_SCENE_ID_MISSING] {label} 缺少 SceneDramaticContract 引用")
            elif scene_id not in operational_scene_ids:
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
        for action_id in supporting:
            action = index.actions.get(action_id)
            action_phase_ids = [
                phase.phase_id
                for phase in (action.temporal_phases if action else [])
            ]
            if not action_phase_ids or action_phase_ids[0] not in phase_ids:
                continue
            previous = action_owners.get(action_id)
            if previous and previous != label:
                errors.append(
                    f"[ACTION_PRIMARY_OWNER_DUPLICATE] {action_id} 在 "
                    f"{previous}/{label} 重复开始执行"
                )
            action_owners[action_id] = label
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
        shot_delivery_by_key: dict[tuple[str, str], Any] = {}
        contribution_evidence_ids = {
            _norm(evidence_id)
            for evidence_id in (
                getattr(
                    getattr(shot, "shot_contribution", None),
                    "evidence_ids",
                    [],
                )
                or []
            )
            if _norm(evidence_id)
        }
        for delivery in (
            getattr(shot, "action_participant_deliveries", []) or []
        ):
            delivery_key = (
                _norm(delivery.action_id),
                _norm(delivery.participant_id),
            )
            if delivery_key in shot_delivery_by_key:
                errors.append(
                    f"[SHOT_ACTION_PARTICIPANT_DELIVERY_DUPLICATE] {label}/"
                    f"{delivery_key[0]}/{delivery_key[1]}"
                )
                continue
            shot_delivery_by_key[delivery_key] = delivery
            action = index.actions.get(delivery_key[0])
            if delivery_key[0] not in bound_action_ids or action is None:
                errors.append(
                    f"[SHOT_ACTION_PARTICIPANT_DELIVERY_ACTION_INVALID] {label}/"
                    f"{delivery_key[0]} 不属于本镜绑定动作"
                )
                continue
            effective_actor_ids, effective_target_ids = action_relations.get(
                action.action_id,
                (list(action.actor_ids), list(action.target_ids)),
            )
            if delivery_key[1] not in {
                *effective_actor_ids,
                *effective_target_ids,
            }:
                errors.append(
                    f"[SHOT_ACTION_PARTICIPANT_DELIVERY_PARTICIPANT_INVALID] "
                    f"{label}/{delivery_key[0]}/{delivery_key[1]} "
                    "不是该动作的 actor/target"
                )
                continue
            authoritative = next(
                (
                    item
                    for item in action.participant_deliveries
                    if (
                        _norm(item.action_id),
                        _norm(item.participant_id),
                    ) == delivery_key
                ),
                None,
            )
            if (
                authoritative is None
                or authoritative.model_dump(mode="json")
                != delivery.model_dump(mode="json")
            ):
                errors.append(
                    f"[SHOT_ACTION_PARTICIPANT_DELIVERY_AUTHORITY_MISMATCH] "
                    f"{label}/{delivery_key[0]}/{delivery_key[1]}"
                )
            if delivery_key[1] not in offscreen_actors | offscreen_targets:
                errors.append(
                    f"[SHOT_ACTION_PARTICIPANT_DELIVERY_NOT_OFFSCREEN] "
                    f"{label}/{delivery_key[0]}/{delivery_key[1]}"
                )
            missing_evidence = {
                _norm(evidence_id)
                for evidence_id in delivery.evidence_ids
                if _norm(evidence_id)
            } - contribution_evidence_ids
            if missing_evidence:
                errors.append(
                    f"[SHOT_ACTION_PARTICIPANT_DELIVERY_EVIDENCE_UNCLAIMED] "
                    f"{label}/{delivery_key[0]}/{delivery_key[1]} 的证据 "
                    f"{sorted(missing_evidence)} 未进入 shot_contribution"
                )
        bound_actor_ids = {
            actor_id
            for action_id in bound_action_ids
            for actor_id in action_relations.get(action_id, ([], []))[0]
        }
        bound_target_ids = {
            target_id
            for action_id in bound_action_ids
            for target_id in action_relations.get(action_id, ([], []))[1]
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
            effective_actor_ids, effective_target_ids = action_relations.get(
                action_id,
                (list(action.actor_ids), list(action.target_ids)),
            )
            missing_actors = (
                set(effective_actor_ids)
                - visible_or_audible_entities
                - offscreen_actors
            )
            if missing_actors:
                errors.append(
                    f"[SHOT_ACTION_ACTOR_UNDELIVERED] {label}/{action_id} 的执行者既未可见/可听也未显式画外交付："
                    f"{sorted(missing_actors)}"
                )
            missing_targets = (
                set(effective_target_ids)
                - visible_or_audible_entities
                - offscreen_targets
            )
            if missing_targets:
                errors.append(
                    f"[SHOT_ACTION_TARGET_UNDELIVERED] {label}/{action_id} 的作用对象既未可见/可听"
                    f"也未显式画外交付：{sorted(missing_targets)}"
                )
            for participant_id in sorted(
                (set(effective_actor_ids) & offscreen_actors)
                | (set(effective_target_ids) & offscreen_targets)
            ):
                if (action_id, participant_id) not in shot_delivery_by_key:
                    errors.append(
                        f"[SHOT_ACTION_PARTICIPANT_DELIVERY_MISSING] "
                        f"{label}/{action_id}/{participant_id} 声明画外交付"
                        "却没有结构化可感知证据合同"
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

        running_event_state = set(planned_in)
        event_entry_states: dict[str, set[str]] = {}
        event_effect_fact_ids: set[str] = set()
        for event_id in event_ids:
            event = index.events.get(event_id)
            occurrences = event_occurrences.get(event_id, [])
            if event is None or not occurrences:
                continue
            starts_here = position == occurrences[0][0]
            completes_here = complete and position == occurrences[-1][0]
            if starts_here:
                event_entry_states[event_id] = set(running_event_state)
                missing = (
                    set(event.precondition_fact_ids)
                    - running_event_state
                )
                if missing:
                    errors.append(
                        f"[SHOT_EVENT_PRECONDITION_MISSING] {label}/"
                        f"{event_id} 镜内顺序缺少前置事实 {sorted(missing)}"
                    )
            if completes_here:
                event_effect_fact_ids.update(event.effects_add)
                event_effect_fact_ids.update(event.effects_remove)
                running_event_state.difference_update(event.effects_remove)
                running_event_state.update(event.effects_add)
        if complete and running_event_state != planned_out:
            errors.append(
                f"[SHOT_EVENT_EFFECT_MISSING] {label} 的镜内事件顺序重放"
                "结果不等于 planned_state_out"
            )
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
            owner_event_id = action_event_owner.get(action_id, "")
            action_entry_state = event_entry_states.get(
                owner_event_id,
                planned_in,
            )
            if starts_action and not set(
                action.precondition_fact_ids
            ).issubset(action_entry_state):
                errors.append(
                    f"[SHOT_ACTION_PRECONDITION_MISSING] {label} 未按镜内"
                    f"事件顺序满足 {action_id} 的前置事实"
                )
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
            if not set(contribution.story_delta_fact_ids).issubset(
                delta_add | delta_remove | event_effect_fact_ids
            ):
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
        capacity_label = f"{label}(shot_no={shot_no})"
        if complete and duration_s <= 0:
            errors.append(
                f"[SHOT_DURATION_MISSING] {capacity_label} 完整分镜缺少正时长"
            )
        if complete and budget is None:
            errors.append(
                f"[SHOT_CAPACITY_BUDGET_MISSING] {capacity_label} "
                "缺少联合观看时间预算"
            )
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
                errors.append(
                    f"[SHOT_CAPACITY_NEGATIVE] {capacity_label} "
                    f"时间预算含负值 {negative}"
                )
            if components["other_s"] > 0 and not _norm(budget.other_reason):
                errors.append(
                    f"[SHOT_CAPACITY_OTHER_REASON_MISSING] {capacity_label} "
                    "开放预算项缺少理由"
                )
            if components["action_phase_s"] + 1e-9 < minimum_action_s:
                errors.append(
                    f"[SHOT_ACTION_CAPACITY_EXCEEDED] {capacity_label} "
                    "动作阶段最少需要 "
                    f"{minimum_action_s:.3f}s"
                )
            if bound_action_ids and minimum_action_s <= 0 and components["action_phase_s"] <= 0:
                errors.append(
                    f"[SHOT_ACTION_CAPACITY_UNDECLARED] {capacity_label} "
                    "执行动作却未分配任何执行时间"
                )

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
                    f"[SHOT_SPOKEN_TEXT_CAPACITY_EXCEEDED] {capacity_label} "
                    "口播/屏幕文字最少需要 "
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
                    f"[SHOT_INFERENCE_CAPACITY_EXCEEDED] {capacity_label} "
                    "目标理解最少需要 "
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
                    f"[SHOT_ATTENTION_CAPACITY_EXCEEDED] {capacity_label} "
                    "竞争注意证据最少需要 "
                    f"{competing_evidence_min_s:.3f}s"
                )
            if contribution and (
                contribution.affective_delta
                or contribution.character_state_delta_ids
            ) and components["reaction_registration_s"] <= 0:
                errors.append(
                    f"[SHOT_REACTION_CAPACITY_UNDECLARED] {capacity_label} "
                    "人物/观众情绪变化没有可感知登记时间"
                )
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
                errors.append(
                    f"[SHOT_SPATIAL_CAPACITY_UNDECLARED] {capacity_label} "
                    "时空重定向没有分配观看时间"
                )
            total_budget_s = sum(components.values())
            if duration_s > 0 and total_budget_s > duration_s + 1e-9:
                errors.append(
                    f"[SHOT_JOINT_CAPACITY_EXCEEDED] {capacity_label} "
                    f"联合预算 {total_budget_s:.3f}s "
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
