"""Per-shot joint viewing-time capacity-budget validation.

One slice of ``validate_storyboard_narrative``'s per-shot loop (see
``storyboard_validate.py``'s module docstring for the full phase map). All
viewing work shares one shot duration; the AI proposes an open dimensional
budget and code derives only graph/text lower bounds and validates their sum
-- no story/action word list is involved.

Split into a "basic" phase (duration/budget presence, negative values, the
open ``other_s`` reason, and the action-phase minimum -- ``_validate_shot_
capacity_basic``) and a "dimensions" phase, itself split by dimension
(spoken/text, inference, attention, reaction+spatial, and the joint total).
Every dimension check reads the same ``components`` dict computed by the
basic phase, and the joint check needs all of them summed together -- so
``components`` is threaded through as a plain parameter rather than
recomputed. Moved verbatim out of the pre-split single function -- only the
wrapping into named phase functions is new.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app import config
from app.spoken_contract import onscreen_text_for_capacity

from .primitives import _norm
from .storyboard_validate_context import _ShotLoopContext

_TEXT_AUDIO_TYPES = {"spoken_dialogue", "offscreen_voice"}


def _validate_shot_capacity_basic(
    shot: Any,
    label: str,
    shot_no: int,
    minimum_action_s: float,
    bound_action_ids: list[str],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> tuple[dict[str, float] | None, float, str]:
    """Validate duration/budget presence, negative values and the action minimum.

    Returns ``(components, duration_s, capacity_label)``; ``components`` is
    ``None`` when this shot has no ``capacity_budget`` (the dimensions phase
    is then skipped, matching the original's ``if budget is not None:`` guard).
    """
    duration_s = float(getattr(shot, "duration_s", 0) or 0)
    budget = getattr(shot, "capacity_budget", None)
    capacity_label = f"{label}(shot_no={shot_no})"
    if ctx.complete and duration_s <= 0:
        errors.append(
            f"[SHOT_DURATION_MISSING] {capacity_label} 完整分镜缺少正时长"
        )
    if ctx.complete and budget is None:
        errors.append(
            f"[SHOT_CAPACITY_BUDGET_MISSING] {capacity_label} "
            "缺少联合观看时间预算"
        )
    if budget is None:
        return None, duration_s, capacity_label
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
    return components, duration_s, capacity_label


def _validate_shot_capacity_dimensions(
    shot: Any,
    capacity_label: str,
    duration_s: float,
    components: dict[str, float],
    contribution: Any,
    boundary: Any,
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Validate the spoken/inference/attention/reaction/spatial minimums and the joint total."""
    _validate_spoken_text_capacity(shot, capacity_label, components, errors)
    _validate_inference_capacity(capacity_label, components, contribution, ctx, errors)
    _validate_attention_capacity(capacity_label, components, contribution, ctx, errors)
    _validate_reaction_and_spatial_capacity(capacity_label, components, contribution, boundary, errors)
    _validate_joint_capacity(capacity_label, duration_s, components, errors)


def _validate_spoken_text_capacity(
    shot: Any, capacity_label: str, components: dict[str, float], errors: list[str],
) -> None:
    """Validate the joint dialogue/narration/on-screen-text minimum."""
    dialogue_text = "".join(
        _norm(getattr(item, "line", ""))
        for item in (getattr(shot, "dialogues", []) or [])
    )
    narration_text = _norm(getattr(shot, "narration", ""))
    timeline_text = "".join(
        _norm(getattr(item, "text", ""))
        for item in (getattr(shot, "audio_timeline", []) or [])
        if getattr(item, "type", "") in _TEXT_AUDIO_TYPES
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
            if getattr(item, "type", "") in _TEXT_AUDIO_TYPES
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


def _validate_inference_capacity(
    capacity_label: str, components: dict[str, float], contribution: Any, ctx: _ShotLoopContext, errors: list[str],
) -> None:
    """Validate the target-comprehension processing-time minimum.

    Audience priors watch the same screen time in parallel: sum sequential
    work inside each path, then gate on the most demanding path -- adding
    paths together would double-charge one shared second.
    """
    processing_by_prior: defaultdict[str, float] = defaultdict(float)
    for delta_id in set(
        contribution.target_delta_ids if contribution else []
    ):
        if delta_id not in ctx.delta_paths:
            continue
        prior_id, delta, _final_state_id = ctx.delta_paths[delta_id]
        processing_by_prior[prior_id] += max(
            0.0, delta.required_processing_s,
        )
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


def _validate_attention_capacity(
    capacity_label: str, components: dict[str, float], contribution: Any, ctx: _ShotLoopContext, errors: list[str],
) -> None:
    """Validate the competing-attention-evidence processing-time minimum."""
    competing_evidence_min_s = sum(
        max(0.0, float(ctx.index.evidence[evidence_id].planned_duration_s or 0))
        for evidence_id in set(contribution.evidence_ids if contribution else [])
        if evidence_id in ctx.index.evidence
        and ctx.index.evidence[evidence_id].competing_attention_ids
    )
    if components["attention_switch_s"] + 1e-9 < competing_evidence_min_s:
        errors.append(
            f"[SHOT_ATTENTION_CAPACITY_EXCEEDED] {capacity_label} "
            "竞争注意证据最少需要 "
            f"{competing_evidence_min_s:.3f}s"
        )


def _validate_reaction_and_spatial_capacity(
    capacity_label: str, components: dict[str, float], contribution: Any, boundary: Any, errors: list[str],
) -> None:
    """Validate reaction-registration and spatial-reorientation time are declared when needed."""
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


def _validate_joint_capacity(
    capacity_label: str, duration_s: float, components: dict[str, float], errors: list[str],
) -> None:
    """Validate the summed dimensional budget does not exceed the shot's duration."""
    total_budget_s = sum(components.values())
    if duration_s > 0 and total_budget_s > duration_s + 1e-9:
        errors.append(
            f"[SHOT_JOINT_CAPACITY_EXCEEDED] {capacity_label} "
            f"联合预算 {total_budget_s:.3f}s "
            f"超过镜头 {duration_s:.3f}s"
        )
