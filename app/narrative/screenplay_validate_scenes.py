"""Scene-contract and arc-contract validation phases of
validate_screenplay_narrative.

Split out of screenplay_validate.py -- see that file's module docstring.
"""
from __future__ import annotations

from typing import Any

from app.schemas import is_system_environment_entity_id

from .primitives import _curve_errors, _norm, _require_refs


def _validate_scene_contracts(
    index: Any,
    declared_entity_ids: set[str],
    prior_ids: set[str],
    errors: list[str],
) -> None:
    """Check each scene contract's POV/applicability/dramatic-dimension coverage, refs and audience paths."""
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


def _validate_arc_contracts(index: Any, event_order: dict[str, int], errors: list[str]) -> None:
    """Check each arc contract's applicability/dramatic-dimension coverage, refs, question closure and ordering."""
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


