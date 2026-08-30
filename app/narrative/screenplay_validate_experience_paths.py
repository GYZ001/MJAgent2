"""Per-audience-path target-delta validation for
_validate_experience_intents (screenplay_validate_experience.py).

Split out of screenplay_validate.py -- see that file's module docstring.
This was the innermost loop body of the original single function (nested
inside "for intent" then "for path"); it is by far the largest single
concern in the file (state/delta coverage per dimension, staging-order
checks against the processing-time budget), which is why it gets its own
module rather than folding into screenplay_validate_experience.py.
"""
from __future__ import annotations

from typing import Any

from app import config

from .primitives import (
    _changed_audience_state_fields,
    _norm,
    _require_refs,
    _state_without_identity,
    _target_state_fragment_matches,
)


def _validate_experience_intent_path(
    index: Any,
    event_order: dict[str, int],
    path: Any,
    withheld_by_proposition: dict[str, Any],
    target_delta_ids: set[str],
    errors: list[str],
) -> None:
    """Check one audience_path's state refs, per-dimension target-delta coverage, and delta staging order."""
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

