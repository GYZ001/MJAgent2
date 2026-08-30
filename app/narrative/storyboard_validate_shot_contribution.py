"""Per-shot ``shot_contribution``, audience-path and audience-delta-grounding validation.

One slice of ``validate_storyboard_narrative``'s per-shot loop (see
``storyboard_validate.py``'s module docstring for the full phase map):
the graph-contribution contract itself (``_validate_shot_contribution``);
per-prior audience-state paths and their cross-shot handoff continuity
(``_validate_shot_audience_paths``); and grounding the contribution's
declared audience/affective/spatial/pressure deltas against the actual
audience-state changes this shot's paths produced
(``_validate_shot_audience_grounding``). Moved verbatim out of the pre-split
single function -- only the wrapping into named phase functions is new.

``_validate_shot_contribution``'s body assigns a local named ``state`` (an
``index.character_states``/``character_beliefs`` lookup result) that has
nothing to do with this package's cross-shot ``_ShotLoopState`` -- the loop
state parameter here is named ``loop_state`` instead of the usual ``state``
specifically to avoid that shadowing.
"""
from __future__ import annotations

from typing import Any

from .primitives import (
    _contribution_nonempty,
    _declared_change_matches,
    _norm,
    _require_refs,
    _state_without_identity,
    _target_state_fragment_matches,
)
from .storyboard_validate_context import _ShotLoopContext, _ShotLoopState


def _validate_shot_contribution(
    position: int,
    label: str,
    shot_id: str,
    scene_id: str,
    event_ids: list[str],
    bound_action_ids: list[str],
    delta_add: set[str],
    delta_remove: set[str],
    event_effect_fact_ids: set[str],
    shot: Any,
    ctx: _ShotLoopContext,
    loop_state: _ShotLoopState,
    errors: list[str],
) -> Any:
    """Validate this shot's ``shot_contribution`` graph-ownership contract.

    Returns the ``shot_contribution`` object (or ``None``) for the audience
    grounding phase.
    """
    contribution = getattr(shot, "shot_contribution", None)
    if not _contribution_nonempty(contribution):
        errors.append(f"[SHOT_CONTRIBUTION_EMPTY] {label} 没有动作、认知、证据、时空、情绪或压力贡献")
    if contribution:
        _validate_contribution_refs_and_id(contribution, label, loop_state, ctx, errors)
        _record_contribution_ownership(position, contribution, label, event_ids, scene_id, shot_id, loop_state, ctx, errors)
        _validate_contribution_state_and_evidence(
            contribution, label, shot_id, event_ids, bound_action_ids,
            delta_add, delta_remove, event_effect_fact_ids, ctx, errors,
        )
    return contribution


def _validate_contribution_refs_and_id(
    contribution: Any,
    label: str,
    loop_state: _ShotLoopState,
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Validate the contribution's own ID and every ID it references."""
    cid = _norm(contribution.shot_contribution_id)
    if not cid:
        errors.append(f"[SHOT_CONTRIBUTION_ID_MISSING] {label} 缺少 shot_contribution_id")
    elif cid in loop_state.contribution_ids:
        errors.append(f"[SHOT_CONTRIBUTION_ID_DUPLICATE] {cid} 被多个镜头复用")
    loop_state.contribution_ids.add(cid)
    _require_refs(contribution.experience_intent_ids, ctx.index.intents, errors, label)
    _require_refs(contribution.target_delta_ids, ctx.index.deltas, errors, label)
    _require_refs(contribution.assimilation_task_ids, ctx.index.tasks, errors, label)
    _require_refs(contribution.evidence_ids, ctx.index.evidence, errors, label)
    _require_refs(contribution.story_delta_fact_ids, ctx.index.facts, errors, label)
    _require_refs(contribution.character_state_delta_ids, set(ctx.index.character_states) | set(ctx.index.character_beliefs), errors, label)
    _require_refs(contribution.audience_state_delta_ids, ctx.index.audience_states, errors, label)


def _record_contribution_ownership(
    position: int,
    contribution: Any,
    label: str,
    event_ids: list[str],
    scene_id: str,
    shot_id: str,
    loop_state: _ShotLoopState,
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Record delta/task ownership and validate character/audience state-delta ownership+anchors."""
    for delta_id in contribution.target_delta_ids:
        loop_state.delta_owners[delta_id].append(label)
        loop_state.delta_owner_positions[delta_id].append(position)
    for task_id in contribution.assimilation_task_ids:
        loop_state.task_owners[task_id].append(position)
    for state_id in contribution.character_state_delta_ids:
        previous_owner = loop_state.contribution_character_owners.get(state_id)
        if previous_owner:
            errors.append(f"[CHARACTER_STATE_DELTA_OWNER_DUPLICATE] {state_id} 被 {previous_owner}/{label} 重复主交付")
        loop_state.contribution_character_owners[state_id] = label
        state = ctx.index.character_states.get(state_id) or ctx.index.character_beliefs.get(state_id)
        if state and not (
            (state.anchor.type == "event" and state.anchor.id in event_ids)
            or (state.anchor.type == "scene" and state.anchor.id == scene_id)
            or (state.anchor.type == "shot" and state.anchor.id == shot_id)
        ):
            errors.append(f"[CHARACTER_STATE_DELTA_ANCHOR_MISMATCH] {label} 交付了不属于当前锚点的 {state_id}")
    for state_id in contribution.audience_state_delta_ids:
        previous_owner = loop_state.contribution_audience_owners.get(state_id)
        if previous_owner:
            errors.append(f"[AUDIENCE_STATE_DELTA_OWNER_DUPLICATE] {state_id} 被 {previous_owner}/{label} 重复主交付")
        loop_state.contribution_audience_owners[state_id] = label


def _validate_contribution_state_and_evidence(
    contribution: Any,
    label: str,
    shot_id: str,
    event_ids: list[str],
    bound_action_ids: list[str],
    delta_add: set[str],
    delta_remove: set[str],
    event_effect_fact_ids: set[str],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Validate story-state coverage, evidence anchors, and action-evidence presence."""
    if not set(contribution.story_delta_fact_ids).issubset(
        delta_add | delta_remove | event_effect_fact_ids
    ):
        errors.append(f"[SHOT_CONTRIBUTION_STATE_MISMATCH] {label} 声明的故事状态贡献不在本镜 delta 中")
    for evidence_id in contribution.evidence_ids:
        evidence = ctx.index.evidence.get(evidence_id)
        if evidence is None:
            continue
        if evidence.anchor.type == "event" and evidence.anchor.id not in event_ids:
            errors.append(f"[SHOT_EVIDENCE_ANCHOR_MISMATCH] {label} 交付的 {evidence_id} 不属于本镜事件")
        if evidence.anchor.type == "shot" and evidence.anchor.id != shot_id:
            errors.append(f"[SHOT_EVIDENCE_ANCHOR_MISMATCH] {label} 交付了锚定另一镜的 {evidence_id}")
    if bound_action_ids:
        action_evidence = [
            ctx.index.evidence[evidence_id]
            for evidence_id in contribution.evidence_ids
            if evidence_id in ctx.index.evidence
            and (
                (
                    ctx.index.evidence[evidence_id].anchor.type == "event"
                    and ctx.index.evidence[evidence_id].anchor.id in event_ids
                )
                or (
                    ctx.index.evidence[evidence_id].anchor.type == "shot"
                    and ctx.index.evidence[evidence_id].anchor.id == shot_id
                )
            )
        ]
        if not action_evidence:
            errors.append(
                f"[SHOT_ACTION_EVIDENCE_MISSING] {label} 绑定了动作阶段却没有当前事件/镜头的可感知证据"
            )


def _validate_shot_audience_paths(
    shot: Any,
    label: str,
    ctx: _ShotLoopContext,
    loop_state: _ShotLoopState,
    errors: list[str],
) -> dict[str, Any]:
    """Validate per-prior ``audience_state_paths`` and the handoff from the previous shot.

    Returns ``current_paths`` (``audience_prior_id -> path``).
    """
    paths = list(getattr(shot, "audience_state_paths", []) or [])
    current_paths = {path.audience_prior_id: path for path in paths}
    if len(current_paths) != len(paths):
        errors.append(f"[SHOT_AUDIENCE_PATH_DUPLICATE] {label} 为同一先验声明了重复状态路径")
    if ctx.complete and ctx.prior_ids - set(current_paths):
        errors.append(f"[SHOT_AUDIENCE_PATH_MISSING] {label} 缺少先验路径 {sorted(ctx.prior_ids - set(current_paths))}")
    for prior_id, path in current_paths.items():
        _require_refs([prior_id], ctx.index.priors, errors, label)
        _require_refs([path.audience_state_in_id, path.audience_state_out_target_id], ctx.index.audience_states, errors, label)
        state_in = ctx.index.audience_states.get(path.audience_state_in_id)
        state_out = ctx.index.audience_states.get(path.audience_state_out_target_id)
        if state_in and state_in.audience_prior_id != prior_id:
            errors.append(f"[SHOT_AUDIENCE_PRIOR_MISMATCH] {label} 的入口状态不属于 {prior_id}")
        if state_out and state_out.audience_prior_id != prior_id:
            errors.append(f"[SHOT_AUDIENCE_PRIOR_MISMATCH] {label} 的出口状态不属于 {prior_id}")
        previous = loop_state.previous_paths.get(prior_id)
        if previous and previous.audience_state_out_target_id != path.audience_state_in_id:
            errors.append(
                f"[AUDIENCE_STATE_HANDOFF_BROKEN] {label}/{prior_id} 的入口 {path.audience_state_in_id} "
                f"不等于上一镜出口 {previous.audience_state_out_target_id}"
            )
    return current_paths


def _validate_shot_audience_grounding(
    shot: Any,
    label: str,
    contribution: Any,
    current_paths: dict[str, Any],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> Any:
    """Ground the contribution's declared deltas against actual audience-state changes.

    Returns ``boundary`` (``narrative_boundary_from_previous``), reused by the
    capacity-budget and cross-shot boundary-handoff phases.
    """
    boundary = getattr(shot, "narrative_boundary_from_previous", None)

    # Contribution fields are claims about real graph changes, not escape
    # hatches for filler.  Validate them against the current prior-specific
    # snapshots and anchored character states.
    changed_audience_state_ids, audience_pairs = _compute_changed_audience_states(current_paths, ctx)
    if contribution:
        declared_audience_state_ids = set(contribution.audience_state_delta_ids)
        if declared_audience_state_ids != changed_audience_state_ids:
            errors.append(
                f"[SHOT_AUDIENCE_DELTA_LEDGER_MISMATCH] {label} 观众状态贡献必须精确等于本镜实际变化："
                f"declared={sorted(declared_audience_state_ids)} "
                f"actual={sorted(changed_audience_state_ids)}"
            )
        _validate_target_delta_state_matches(label, contribution, current_paths, ctx, errors)
        _validate_affective_spatial_pressure_grounding(label, contribution, audience_pairs, ctx, errors)
    return boundary


def _compute_changed_audience_states(
    current_paths: dict[str, Any],
    ctx: _ShotLoopContext,
) -> tuple[set[str], list[tuple[Any, Any]]]:
    """Compute which audience states actually changed identity across this shot's paths.

    Returns ``(changed_audience_state_ids, audience_pairs)``.
    """
    changed_audience_state_ids: set[str] = set()
    audience_pairs: list[tuple[Any, Any]] = []
    for path in current_paths.values():
        state_in = ctx.index.audience_states.get(path.audience_state_in_id)
        state_out = ctx.index.audience_states.get(path.audience_state_out_target_id)
        if state_in is None or state_out is None:
            continue
        audience_pairs.append((state_in, state_out))
        if _state_without_identity(state_in) != _state_without_identity(state_out):
            changed_audience_state_ids.add(state_out.audience_state_id)
    return changed_audience_state_ids, audience_pairs


def _validate_target_delta_state_matches(
    label: str,
    contribution: Any,
    current_paths: dict[str, Any],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Validate each delivered target delta's from/to state matches its audience path."""
    for delta_id in contribution.target_delta_ids:
        path_contract = ctx.delta_paths.get(delta_id)
        if path_contract is None:
            continue
        prior_id, delta, final_state_id = path_contract
        current_path = current_paths.get(prior_id)
        if current_path is None:
            errors.append(f"[SHOT_TARGET_PRIOR_PATH_MISSING] {label}/{delta_id} 没有对应观众路径")
            continue
        state_in = ctx.index.audience_states.get(current_path.audience_state_in_id)
        state_out = ctx.index.audience_states.get(current_path.audience_state_out_target_id)
        if state_in and not _target_state_fragment_matches(delta, delta.from_state, state_in):
            errors.append(f"[SHOT_TARGET_FROM_STATE_MISMATCH] {label}/{delta_id} 未从合同约定的观众状态出发")
        if state_out and not _target_state_fragment_matches(
            delta,
            delta.to_state,
            state_out,
        ):
            final_state = ctx.index.audience_states.get(final_state_id)
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


def _validate_affective_spatial_pressure_grounding(
    label: str,
    contribution: Any,
    audience_pairs: list[tuple[Any, Any]],
    ctx: _ShotLoopContext,
    errors: list[str],
) -> None:
    """Validate affective/spatial-temporal/dramatic-pressure deltas are grounded in a real change."""
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
        state_id in ctx.index.character_states
        for state_id in contribution.character_state_delta_ids
    ):
        errors.append(f"[SHOT_PRESSURE_DELTA_UNGROUNDED] {label} 压力变化没有当前镜头锚定的人物状态")
