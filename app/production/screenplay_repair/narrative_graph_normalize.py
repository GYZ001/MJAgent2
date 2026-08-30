"""_normalize_screenplay_narrative_graph: repairs exact source offsets and
unambiguous event-ID/punctuation drift across an entire narrative_plan graph
in one deterministic pass.

This is the orchestrator: each phase of the original ~1,620-line single
function is now a standalone helper function, split across sibling files by
what it reads/writes (see each sibling's module docstring for its slice):

  - narrative_graph_dialogue.py: dialogue-chain continuity, dialogue-topic
    normalization, and source-evidence span repair.
  - narrative_graph_events.py: event-id reference canonicalization and
    event/action fact-reference derivation.
  - narrative_graph_arcs.py: the core id-lookup dicts, arc-contract
    promise/payoff normalization, missing-effect-fact synthesis,
    evidence-perceiver widening, belief-stance aliasing, and
    missing-critical-proposition attachment.
  - narrative_graph_audience_context.py: the shared mutable lookup/tracking
    state for the audience-experience-path phases, plus coarse
    per-prior/per-scene audience-path synthesis.
  - narrative_graph_audience_deltas.py / narrative_graph_audience_deltas_dims.py:
    per-path target-delta reconciliation (belief/attention/affective/
    active-question dimensions) and no-change-delta pruning.
  - narrative_graph_readability.py: removed-delta reference cleanup,
    readability-window budget normalization, and identity-contract
    evidence-ref normalization.

The sequence and the data each phase reads/writes is unchanged from the
pre-split source; only the decomposition into named, independently
readable/testable steps is new. `_normalize_screenplay_narrative_graph`
remains this package's stable entry point -- see
`app/production/screenplay_repair/__init__.py`.
"""
from __future__ import annotations

from typing import Any

from app.schemas import EpisodeScreenplay

from .narrative_graph_arcs import (
    _attach_missing_critical_propositions,
    _build_core_lookup_dicts,
    _normalize_arc_contract_promises,
    _normalize_belief_stance_aliases,
    _synthesize_missing_effect_facts,
    _widen_evidence_perceivers,
)
from .narrative_graph_audience_context import (
    _build_audience_experience_context,
    _synthesize_coarse_audience_paths_by_prior,
    _synthesize_coarse_scene_audience_paths,
)
from .narrative_graph_audience_deltas import _reconcile_audience_path_deltas
from .narrative_graph_dialogue import (
    _build_authorized_source_chapters,
    _normalize_short_dialogue_topics,
    _repair_dialogue_chain_continuity,
    _repair_dialogue_source_alignment,
    _repair_source_evidence_spans,
)
from .narrative_graph_events import (
    _build_actions_by_id,
    _derive_event_action_fact_refs,
    _normalize_event_id_references,
)
from .narrative_graph_readability import (
    _normalize_identity_contract_evidence_refs,
    _normalize_readability_windows,
    _prune_removed_delta_references,
)


def _normalize_screenplay_narrative_graph(
    script: EpisodeScreenplay,
    *,
    authorized_source_chapters: dict[str, str] | None,
) -> list[dict[str, Any]]:
    """Repair exact source offsets and unambiguous event-ID punctuation drift."""
    plan = script.narrative_plan
    if plan is None:
        return []
    data = plan.model_dump(mode="json")
    changes: list[dict[str, Any]] = []

    _repair_dialogue_chain_continuity(script, changes)
    _normalize_short_dialogue_topics(script, changes)
    chapters = _build_authorized_source_chapters(authorized_source_chapters)
    _repair_dialogue_source_alignment(script, chapters, changes)
    _repair_source_evidence_spans(data, chapters, changes)

    _normalize_event_id_references(data, changes)

    actions_by_id = _build_actions_by_id(data)
    _derive_event_action_fact_refs(data, actions_by_id, changes)

    propositions_by_id, evidence_by_id, events_by_id = _build_core_lookup_dicts(data)
    _normalize_arc_contract_promises(data, propositions_by_id, changes)
    _synthesize_missing_effect_facts(
        data, events_by_id, actions_by_id, evidence_by_id, propositions_by_id, changes,
    )
    _widen_evidence_perceivers(
        data, evidence_by_id, events_by_id, actions_by_id, propositions_by_id, changes,
    )
    _normalize_belief_stance_aliases(data, changes)
    _attach_missing_critical_propositions(data, changes)

    ctx = _build_audience_experience_context(data, evidence_by_id, propositions_by_id)
    _synthesize_coarse_audience_paths_by_prior(data, ctx, events_by_id, changes)
    _synthesize_coarse_scene_audience_paths(data, ctx, changes)
    _reconcile_audience_path_deltas(ctx, changes)
    _prune_removed_delta_references(data, ctx.removed_delta_ids, changes)

    _normalize_readability_windows(data, changes)
    _normalize_identity_contract_evidence_refs(data, changes)

    if changes:
        script.narrative_plan = type(plan).model_validate(data)
    return changes
