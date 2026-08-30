"""Screenplay narrative-graph hard gate.

This is the orchestrator: each validation phase of the original ~1,575-line
single function is now a standalone helper function, split across sibling
files by what it reads (see each sibling's module docstring for its slice):

  - screenplay_validate_core.py: version/scope, forbidden-environment-entity,
    presence/coverage and source-evidence-span checks.
  - screenplay_validate_identity.py: declared-entity-id, identity,
    proposition and adaptation-decision checks.
  - screenplay_validate_facts.py: state-fact, dramatic-question and
    evidence checks.
  - screenplay_validate_events.py: event, causal-DAG, causal-retention and
    state-fact-availability checks -- also where event_order/parents/
    action_event_owner/fact_producer are built and returned for reuse.
  - screenplay_validate_actions.py: action, action/event-consistency and
    structural-equivalence-audit checks.
  - screenplay_validate_characters.py: character-belief,
    character-dramatic-state and character-decision-chain checks.
  - screenplay_validate_experience.py /
    screenplay_validate_experience_paths.py: audience-prior, audience-state
    and experience-intent/audience-path/target-delta checks (the largest
    concern in the file, split further into its own module).
  - screenplay_validate_delivery.py: critical-proposition-intent,
    assimilation-task, readability-window, target-delta-window and
    setup/payoff checks.
  - screenplay_validate_scenes.py: scene-contract and arc-contract checks.

The sequence and the data each phase reads is unchanged from the pre-split
source; only the decomposition into named, independently readable/testable
steps is new. Add new screenplay-narrative validation logic to the relevant
sibling file, not back into this one.
"""
from __future__ import annotations

from typing import Iterable

from app.schemas import EpisodeScreenplay, system_environment_entity_id

from .plan_index import index_narrative_plan
from .screenplay_validate_actions import (
    _validate_action_event_effect_consistency,
    _validate_action_structural_equivalence_audit,
    _validate_actions,
)
from .screenplay_validate_characters import (
    _validate_character_beliefs,
    _validate_character_decision_chains,
    _validate_character_dramatic_states,
)
from .screenplay_validate_core import (
    _validate_forbidden_environment_entities,
    _validate_narrative_plan_version_and_scope,
    _validate_narrative_presence_and_coverage,
    _validate_source_evidence_spans,
)
from .screenplay_validate_delivery import (
    _validate_assimilation_tasks,
    _validate_critical_proposition_intent_coverage,
    _validate_readability_windows,
    _validate_setup_payoff_contracts,
    _validate_target_delta_primary_windows,
)
from .screenplay_validate_events import (
    _validate_causal_event_retention,
    _validate_event_dag_acyclic,
    _validate_events,
    _validate_state_fact_availability,
)
from .screenplay_validate_experience import (
    _validate_audience_priors,
    _validate_audience_states,
    _validate_experience_intents,
)
from .screenplay_validate_facts import (
    _validate_dramatic_questions,
    _validate_evidence,
    _validate_state_facts,
)
from .screenplay_validate_identity import (
    _build_declared_entity_ids,
    _validate_adaptation_decisions,
    _validate_identities,
    _validate_propositions,
    _validate_reserved_environment_entities,
)
from .screenplay_validate_scenes import _validate_arc_contracts, _validate_scene_contracts


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

    _validate_narrative_plan_version_and_scope(plan, errors, expected_scope_id)
    index = index_narrative_plan(plan, errors)
    environment_entity_id = system_environment_entity_id(plan.scope_id)

    _validate_forbidden_environment_entities(screenplay, errors)
    _validate_narrative_presence_and_coverage(screenplay, plan, index, errors)
    _validate_source_evidence_spans(
        index, errors, source_text, authorized_source_chapter_ids, authorized_source_chapters,
    )

    adapted_ids: set[str] = set()
    declared_entity_ids = _build_declared_entity_ids(index)
    _validate_reserved_environment_entities(declared_entity_ids, environment_entity_id, errors)
    _validate_identities(index, declared_entity_ids, errors)
    _validate_propositions(index, adapted_ids, errors)
    _validate_adaptation_decisions(index, adapted_ids, errors)

    _validate_state_facts(index, declared_entity_ids, environment_entity_id, errors)
    _validate_dramatic_questions(index, errors)
    _validate_evidence(index, declared_entity_ids, errors)

    event_order, parents, action_event_owner, fact_producer = _validate_events(
        index, plan, declared_entity_ids, errors,
    )
    _validate_event_dag_acyclic(parents, errors)
    _validate_causal_event_retention(index, event_order, fact_producer, errors)
    _validate_state_fact_availability(plan, index, fact_producer, errors)

    _validate_actions(index, declared_entity_ids, errors)
    _validate_action_event_effect_consistency(index, errors)
    _validate_action_structural_equivalence_audit(index, action_event_owner, errors)

    _validate_character_beliefs(index, declared_entity_ids, event_order, errors)
    _validate_character_dramatic_states(index, declared_entity_ids, errors)
    _validate_character_decision_chains(index, event_order, errors)

    prior_ids = _validate_audience_priors(index, plan, errors)
    _validate_audience_states(index, event_order, errors)
    _validate_experience_intents(
        index, plan, declared_entity_ids, prior_ids, event_order, errors,
    )

    _validate_critical_proposition_intent_coverage(index, errors)
    _validate_assimilation_tasks(index, errors)
    _validate_readability_windows(index, errors)
    _validate_target_delta_primary_windows(index, errors)
    _validate_setup_payoff_contracts(index, prior_ids, event_order, errors)

    _validate_scene_contracts(index, declared_entity_ids, prior_ids, errors)
    _validate_arc_contracts(index, event_order, errors)

    return list(dict.fromkeys(errors))
