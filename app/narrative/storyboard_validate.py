"""Storyboard narrative-graph hard gates.

Moved verbatim out of the pre-split ``app/narrative.py`` (see
``app/narrative/__init__.py`` for the package-split rationale). Three
functions that share one concern: projecting the screenplay narrative
authority onto a storyboard (``validate_storyboard_screenplay_authority``,
which ``validate_storyboard_narrative`` calls directly) and validating shot
contribution / action-delta ownership / audience hand-offs against it
(``validate_storyboard_narrative``).

``validate_storyboard_narrative`` was a single ~1,255-line function in the
pre-split source; this is now the orchestrator, split by real phase
boundary across sibling files the same way ``screenplay_validate.py`` was
split (see that file's module docstring for the precedent):

  - ``storyboard_validate_context.py``: the ``_ShotLoopContext`` (read-only,
    built once) / ``_ShotLoopState`` (cross-shot accumulators) dataclasses
    and the pre-loop index-building step.
  - ``storyboard_validate_shot.py``: ``_validate_shot``, the per-shot
    orchestrator called once per item in the loop, in the same phase order
    the pre-split function ran them.
  - ``storyboard_validate_shot_bindings.py``: shot-id/event/scene refs,
    primary/supporting action bindings, structured participant deliveries
    and per-bound-action delivery checks.
  - ``storyboard_validate_shot_state.py``: planned-state-fact deltas, the
    state boundary from the previous shot, in-shot event replay and the
    minimum action-phase seconds.
  - ``storyboard_validate_shot_contribution.py``: the ``shot_contribution``
    contract, audience-state paths and audience-delta grounding.
  - ``storyboard_validate_shot_capacity.py``: the joint viewing-time
    capacity budget across all its dimensions.
  - ``storyboard_validate_shot_boundary.py``: the full boundary contract to
    the previous shot, the completed-action ledger and this shot's own
    bookkeeping for the next one.
  - ``storyboard_validate_post.py``: the seven post-loop, whole-episode
    passes (causal order, action-phase delivery order, intended ambiguity,
    delivery completeness, readability windows, primary-window delivery,
    cognitive bridges).

The sequence and the data each phase reads is unchanged from the pre-split
source; only the decomposition into named, independently readable/testable
steps is new. Add new storyboard-narrative validation logic to the
concern-matching sibling file, not back into this one.
"""
from __future__ import annotations

from typing import Any

from app.schemas import (
    EpisodeScreenplay,
    NARRATIVE_CONTRACT_VERSION,
    Storyboard,
    StoryboardOutline,
)

from .plan_index import action_participant_delivery_errors, index_narrative_plan
from .primitives import _norm
from .storyboard_validate_context import _build_loop_context
from .storyboard_validate_post import (
    _validate_action_phase_delivery_order,
    _validate_cognitive_bridges,
    _validate_delivery_completeness,
    _validate_event_causal_order,
    _validate_intended_ambiguity,
    _validate_primary_window_delivery,
    _validate_storyboard_readability_windows,
)
from .storyboard_validate_shot import _validate_shot


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

    ctx, state = _build_loop_context(screenplay, plan, index, outline, complete, items)
    for position, shot in enumerate(items):
        _validate_shot(position, shot, items, errors, ctx, state)

    first_event_position = _validate_event_causal_order(ctx, state, errors)
    _validate_action_phase_delivery_order(items, ctx, state, errors)
    _validate_intended_ambiguity(items, first_event_position, ctx, errors)
    _validate_delivery_completeness(first_event_position, ctx, state, errors)
    windows = _validate_storyboard_readability_windows(items, ctx, state, errors)
    _validate_primary_window_delivery(windows, ctx, state, errors)
    _validate_cognitive_bridges(ctx, state, errors)

    return list(dict.fromkeys(errors))
