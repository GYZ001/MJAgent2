from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from app.continuity import apply_shot_contract, shot_contract_dict
from app.narrative import (
    blind_ai_human_comprehension_correlation,
    blind_reader_payload,
    compute_narrative_metrics,
    validate_blind_review,
    validate_screenplay_narrative,
    validate_storyboard_screenplay_authority,
    validate_storyboard_narrative,
)
from app.production.screenplay_document import document_to_screenplay, screenplay_to_document
from app.schemas import (
    AssimilationTask,
    AtomicAction,
    AudienceStatePathRef,
    BlindAudienceObservation,
    CharacterBeliefSnapshot,
    CharacterDramaticState,
    EpisodeScreenplay,
    NarrativeAnchor,
    NarrativeBoundaryContract,
    NarrativeContinuityPlan,
    NarrativeEvidence,
    NarrativeEvent,
    NarrativeProposition,
    NarrativeReviewReport,
    Shot,
    ShotContribution,
    Storyboard,
    StoryEvent,
    WithheldProposition,
)


def _plan() -> NarrativeContinuityPlan:
    """Small relation-complete contract; no assertion depends on story keywords."""
    return NarrativeContinuityPlan.model_validate(
        {
            "scope_id": "episode-generic",
            "initial_state_fact_ids": ["F-before"],
            "source_evidence": [
                {
                    "source_evidence_id": "SE-1",
                    "source_span": {"chapter_id": "1", "start": 0, "end": 28},
                    "verbatim_excerpt": "An observable change occurs.",
                    "confidence": 1.0,
                }
            ],
            "propositions": [
                {
                    "proposition_id": "P-source",
                    "semantic_identity_key": "source-observable-change",
                    "canonical_statement": "The source establishes a change.",
                    "narrative_domain": "source_canon",
                    "direct_source_evidence_ids": ["SE-1"],
                },
                {
                    "proposition_id": "P-story",
                    "semantic_identity_key": "adapted-observable-change",
                    "canonical_statement": "The adaptation presents the change.",
                    "narrative_domain": "adapted_story",
                    "entity_ids": ["entity-1", "character-1"],
                },
            ],
            "adaptation_decisions": [
                {
                    "adaptation_decision_id": "AD-1",
                    "source_proposition_ids": ["P-source"],
                    "adapted_proposition_ids": ["P-story"],
                    "relation": "preserve",
                    "creative_reason": "Preserve the causal effect in screen time.",
                    "affected_event_ids": ["E-1"],
                }
            ],
            "state_facts": [
                {
                    "fact_id": "F-before",
                    "proposition_id": "P-story",
                    "subject_id": "entity-1",
                    "predicate_id": "observable_state",
                    "value": {"kind": "text", "data": "before"},
                    "time_scope": "main@1",
                },
                {
                    "fact_id": "F-after",
                    "proposition_id": "P-story",
                    "subject_id": "entity-1",
                    "predicate_id": "observable_state",
                    "value": {"kind": "text", "data": "after"},
                    "time_scope": "main@2",
                },
            ],
            "evidence": [
                {
                    "evidence_id": "EV-1",
                    "anchor": {"type": "event", "id": "E-1"},
                    "observable_claim": "The result can be seen and registered.",
                    "perceivable_by": ["character-1", "audience"],
                    "supports_proposition_ids": ["P-story"],
                    "planned_salience": 0.8,
                }
            ],
            "events": [
                {
                    "event_id": "E-1",
                    "proposition_ids": ["P-story"],
                    "precondition_fact_ids": ["F-before"],
                    "effects_add": ["F-after"],
                    "effects_remove": ["F-before"],
                    "delivery_scope_id": "episode-generic",
                    "primary_delivery_window_id": "RW-1",
                }
            ],
            "character_beliefs": [
                {
                    "character_belief_id": "CB-1",
                    "character_id": "character-1",
                    "anchor": {"type": "event", "id": "E-1"},
                    "perceived_evidence_ids": ["EV-1"],
                    "beliefs": [
                        {
                            "proposition_id": "P-story",
                            "stance": "suspected",
                            "confidence": 0.7,
                            "evidence_ids": ["EV-1"],
                        }
                    ],
                }
            ],
            "audience_priors": [
                {
                    "audience_prior_id": "AP-cold",
                    "scope_id": "episode-generic",
                    "audience_description": "A first-time viewer without contextual knowledge.",
                    "assumed_unknown_proposition_ids": ["P-story"],
                },
                {
                    "audience_prior_id": "AP-context",
                    "scope_id": "episode-generic",
                    "audience_description": "A viewer familiar with earlier source context.",
                    "assumed_known_proposition_ids": ["P-source"],
                    "assumed_unknown_proposition_ids": ["P-story"],
                },
            ],
            "audience_states": [
                {
                    "audience_state_id": "AS-cold-in",
                    "audience_prior_id": "AP-cold",
                    "anchor": {"type": "event", "id": "E-1"},
                    "beliefs": [{"proposition_id": "P-story", "stance": "unknown"}],
                },
                {
                    "audience_state_id": "AS-cold-out",
                    "audience_prior_id": "AP-cold",
                    "anchor": {"type": "event", "id": "E-1"},
                    "beliefs": [
                        {
                            "proposition_id": "P-story",
                            "stance": "suspected",
                            "confidence": 0.7,
                            "evidence_ids": ["EV-1"],
                        }
                    ],
                },
                {
                    "audience_state_id": "AS-context-in",
                    "audience_prior_id": "AP-context",
                    "anchor": {"type": "event", "id": "E-1"},
                    "beliefs": [{"proposition_id": "P-story", "stance": "unknown"}],
                },
                {
                    "audience_state_id": "AS-context-out",
                    "audience_prior_id": "AP-context",
                    "anchor": {"type": "event", "id": "E-1"},
                    "beliefs": [
                        {
                            "proposition_id": "P-story",
                            "stance": "suspected",
                            "confidence": 0.7,
                            "evidence_ids": ["EV-1"],
                        }
                    ],
                },
                {
                    "audience_state_id": "AS-cold-settled",
                    "audience_prior_id": "AP-cold",
                    "anchor": {"type": "sequence", "id": "processing"},
                    "beliefs": [
                        {
                            "proposition_id": "P-story",
                            "stance": "suspected",
                            "confidence": 0.7,
                            "evidence_ids": ["EV-1"],
                        }
                    ],
                    "affective_state": {"registration": "settled"},
                },
                {
                    "audience_state_id": "AS-context-settled",
                    "audience_prior_id": "AP-context",
                    "anchor": {"type": "sequence", "id": "processing"},
                    "beliefs": [
                        {
                            "proposition_id": "P-story",
                            "stance": "suspected",
                            "confidence": 0.7,
                            "evidence_ids": ["EV-1"],
                        }
                    ],
                    "affective_state": {"registration": "settled"},
                },
            ],
            "experience_intents": [
                {
                    "experience_intent_id": "XI-1",
                    "scope_id": "episode-generic",
                    "anchor_event_ids": ["E-1"],
                    "director_objective": "Let each prior register the same causal change.",
                    "audience_paths": [
                        {
                            "audience_path_id": "XP-cold",
                            "audience_prior_id": "AP-cold",
                            "audience_state_in_id": "AS-cold-in",
                            "audience_state_out_target_id": "AS-cold-out",
                            "target_deltas": [
                                {
                                    "target_delta_id": "XD-cold",
                                    "dimension": "belief",
                                    "proposition_ids": ["P-story"],
                                    "description": "Unknown becomes grounded suspicion.",
                                    "from_state": {"stance": "unknown"},
                                    "to_state": {"stance": "suspected"},
                                    "target_confidence": 0.7,
                                    "required_processing_s": 1.0,
                                    "deadline_event_id": "E-1",
                                    "primary_delivery_window_id": "RW-1",
                                }
                            ],
                        },
                        {
                            "audience_path_id": "XP-context",
                            "audience_prior_id": "AP-context",
                            "audience_state_in_id": "AS-context-in",
                            "audience_state_out_target_id": "AS-context-out",
                            "target_deltas": [
                                {
                                    "target_delta_id": "XD-context",
                                    "dimension": "belief",
                                    "proposition_ids": ["P-story"],
                                    "description": "Context is converted into grounded suspicion.",
                                    "from_state": {"stance": "unknown"},
                                    "to_state": {"stance": "suspected"},
                                    "target_confidence": 0.7,
                                    "required_processing_s": 1.0,
                                    "deadline_event_id": "E-1",
                                    "primary_delivery_window_id": "RW-1",
                                }
                            ],
                        },
                    ],
                }
            ],
            "readability_windows": [
                {
                    "readability_window_id": "RW-1",
                    "event_ids": ["E-1"],
                    "proposition_ids": ["P-story"],
                    "target_delta_ids": ["XD-cold", "XD-context"],
                    "shot_ids": ["SH-1"],
                    "evidence_ids": ["EV-1"],
                    "scheduled_processing_s": 1.5,
                    "planned_available_s": 2.0,
                    "readability_reason": "The observable result has uncontested attention.",
                    "status": "satisfied",
                }
            ],
            "scene_contracts": [{
                "scene_id": "SC-generic",
                "applicability": "not_applicable",
                "not_applicable_reason": "The minimal fixture validates relations rather than a traditional scene.",
                "alternative_dramatic_function": "Deliver one observable causal state change.",
            }],
            "arc_contracts": [{
                "arc_id": "ARC-generic",
                "scope": "episode",
                "applicability": "not_applicable",
                "not_applicable_reason": "The minimal fixture is one relation beat rather than a conventional arc.",
                "alternative_dramatic_function": "Validate one complete cause-to-observation path.",
            }],
        }
    )


def _screenplay() -> EpisodeScreenplay:
    return EpisodeScreenplay(
        episode_no=1,
        title="Generic relation fixture",
        narrative_plan=_plan(),
    )


def _paths(*, settled: bool = False) -> list[AudienceStatePathRef]:
    return [
        AudienceStatePathRef(
            audience_prior_id="AP-cold",
            audience_state_in_id="AS-cold-out" if settled else "AS-cold-in",
            audience_state_out_target_id="AS-cold-out",
        ),
        AudienceStatePathRef(
            audience_prior_id="AP-context",
            audience_state_in_id="AS-context-out" if settled else "AS-context-in",
            audience_state_out_target_id="AS-context-out",
        ),
    ]


def _shot(**changes: object) -> Shot:
    values: dict[str, object] = {
        "shot_no": 1,
        "duration_s": 5,
        "shot_size": "中景",
        "camera_move": "固定",
        "scene_setting": "日，中性空间",
        "characters": ["character-1"],
        "action_desc": "The visible result is held long enough to be registered.",
        "first_frame_desc": "The prior state remains visible.",
        "last_frame_desc": "The changed state is visible and registered.",
        "source_excerpt": "An observable change occurs.",
        "shot_id": "SH-1",
        "scene_id": "SC-generic",
        "event_ids": ["E-1"],
        "primary_action_id": None,
        "shot_contribution": ShotContribution(
            shot_contribution_id="SCN-1",
            experience_intent_ids=["XI-1"],
            target_delta_ids=["XD-cold", "XD-context"],
            evidence_ids=["EV-1"],
            story_delta_fact_ids=["F-after"],
            audience_state_delta_ids=["AS-cold-out", "AS-context-out"],
        ),
        "audience_state_paths": _paths(),
        "planned_state_in_fact_ids": ["F-before"],
        "planned_delta_add_fact_ids": ["F-after"],
        "planned_delta_remove_fact_ids": ["F-before"],
        "planned_state_out_fact_ids": ["F-after"],
        "readability_window_ids": ["RW-1"],
        "capacity_budget": {
            "inference_processing_s": 2.0,
            "reaction_registration_s": 1.0,
        },
    }
    values.update(changes)
    return Shot.model_validate(values)


def _board() -> Storyboard:
    return Storyboard(episode_no=1, shots=[_shot()])


def _codes(errors: list[str]) -> set[str]:
    return {
        error[1 : error.index("]")]
        for error in errors
        if error.startswith("[") and "]" in error
    }


def test_storyboard_authority_does_not_repromote_screenplay_score_only_findings() -> None:
    screenplay = _screenplay()
    first_event = screenplay.narrative_plan.events[0]
    first_event.precondition_fact_ids = ["F-before"]
    first_event.effects_add = ["F-before", "F-after"]
    first_event.effects_remove = []

    full_codes = _codes(validate_screenplay_narrative(
        screenplay,
        require=True,
        expected_scope_id="episode-generic",
    ))
    operational = validate_storyboard_screenplay_authority(
        screenplay,
        expected_scope_id="episode-generic",
    )

    assert {
        "EVENT_PRECONDITION_FROM_FUTURE",
        "INITIAL_FACT_HAS_PRODUCER",
        "STATE_REPLAY_WITHOUT_DELTA",
    } & full_codes
    assert operational == []


def test_storyboard_authority_keeps_scope_errors_runtime_blocking() -> None:
    screenplay = _screenplay()

    errors = validate_storyboard_screenplay_authority(
        screenplay,
        expected_scope_id="another-episode",
    )

    assert "NARRATIVE_SCOPE_MISMATCH" in _codes(errors)


def _boundary(previous_shot_id: str, next_shot_id: str) -> NarrativeBoundaryContract:
    """A generic exact hand-off; no story vocabulary participates in validation."""
    return NarrativeBoundaryContract(
        boundary_id=f"NB-{previous_shot_id}-{next_shot_id}",
        previous_shot_id=previous_shot_id,
        next_shot_id=next_shot_id,
        narrative_relation="cause_to_consequence",
        audience_state_handoffs=[
            {
                "audience_prior_id": "AP-cold",
                "previous_state_out_id": "AS-cold-out",
                "next_state_in_id": "AS-cold-out",
            },
            {
                "audience_prior_id": "AP-context",
                "previous_state_out_id": "AS-context-out",
                "next_state_in_id": "AS-context-out",
            },
        ],
        cut_motivation="Shift attention after the prior beat has landed.",
    )


def _settled_followup_shot(
    *,
    shot_no: int = 2,
    shot_id: str = "SH-2",
    previous_shot_id: str = "SH-1",
    event_ids: list[str] | None = None,
    contribution: ShotContribution | None = None,
    primary_action_id: str | None = None,
) -> Shot:
    default_contribution = contribution is None
    paths = (
        [
            AudienceStatePathRef(
                audience_prior_id="AP-cold",
                audience_state_in_id="AS-cold-out",
                audience_state_out_target_id="AS-cold-settled",
            ),
            AudienceStatePathRef(
                audience_prior_id="AP-context",
                audience_state_in_id="AS-context-out",
                audience_state_out_target_id="AS-context-settled",
            ),
        ]
        if default_contribution else _paths(settled=True)
    )
    return _shot(
        shot_no=shot_no,
        shot_id=shot_id,
        event_ids=event_ids or [],
        primary_action_id=primary_action_id,
        shot_contribution=contribution
        or ShotContribution(
            shot_contribution_id=f"SCN-{shot_no}",
            audience_state_delta_ids=["AS-cold-settled", "AS-context-settled"],
            affective_delta={"registration": "settled"},
        ),
        audience_state_paths=paths,
        planned_state_in_fact_ids=["F-after"],
        planned_delta_add_fact_ids=[],
        planned_delta_remove_fact_ids=[],
        planned_state_out_fact_ids=["F-after"],
        readability_window_ids=[],
        narrative_boundary_from_previous=_boundary(previous_shot_id, shot_id),
    )


def _append_causal_followup(
    screenplay: EpisodeScreenplay,
    *,
    event_id: str = "E-2",
    parent_id: str = "E-1",
    proposition_ids: list[str] | None = None,
    action_ids: list[str] | None = None,
) -> None:
    screenplay.narrative_plan.events.append(
        NarrativeEvent(
            event_id=event_id,
            proposition_ids=proposition_ids or ["P-story"],
            causal_parent_ids=[parent_id],
            action_ids=action_ids or [],
            must_keep=True,
            delivery_scope_id="episode-generic",
            delivery_policy="carry",
        )
    )


def _rename_entities_and_surface_text(
    screenplay: EpisodeScreenplay,
    board: Storyboard | None = None,
) -> None:
    """Metamorphic rename: keep relation IDs/edges, replace all surface semantics."""
    plan = screenplay.narrative_plan
    entity_ids = {
        "entity-1": "renamed-subject-9",
        "character-1": "renamed-viewpoint-4",
    }
    screenplay.title = "Completely different labels"
    for index, source in enumerate(plan.source_evidence):
        source.verbatim_excerpt = f"Replacement source surface {index}."
    for index, proposition in enumerate(plan.propositions):
        proposition.canonical_statement = f"Replacement proposition surface {index}."
        proposition.entity_ids = [entity_ids.get(item, item) for item in proposition.entity_ids]
    for index, decision in enumerate(plan.adaptation_decisions):
        decision.creative_reason = f"Replacement adaptation reason {index}."
    for fact in plan.state_facts:
        fact.subject_id = entity_ids.get(fact.subject_id, fact.subject_id)
        fact.value.data = f"replacement-{fact.fact_id}"
    for index, evidence in enumerate(plan.evidence):
        evidence.observable_claim = f"Replacement observable claim {index}."
        evidence.perceivable_by = [entity_ids.get(item, item) for item in evidence.perceivable_by]
    for action in plan.atomic_actions:
        action.actor_ids = [entity_ids.get(item, item) for item in action.actor_ids]
        action.target_ids = [entity_ids.get(item, item) for item in action.target_ids]
        action.semantic_intent = f"Replacement intent for {action.action_id}."
        action.completion_condition = f"Replacement completion for {action.action_id}."
    for belief in plan.character_beliefs:
        belief.character_id = entity_ids.get(belief.character_id, belief.character_id)
    for state in plan.character_states:
        state.character_id = entity_ids.get(state.character_id, state.character_id)
        state.tactic = f"Replacement tactic for {state.character_state_id}."
    for index, prior in enumerate(plan.audience_priors):
        prior.audience_description = f"Replacement prior description {index}."
    for intent in plan.experience_intents:
        intent.director_objective = f"Replacement objective for {intent.experience_intent_id}."
        for path in intent.audience_paths:
            for delta in path.target_deltas:
                delta.description = f"Replacement delta for {delta.target_delta_id}."
    for window in plan.readability_windows:
        window.readability_reason = f"Replacement readability reason for {window.readability_window_id}."

    if board is not None:
        for shot in board.shots:
            shot.characters = [entity_ids.get(item, item) for item in shot.characters]
            shot.offscreen_action_actor_ids = [
                entity_ids.get(item, item)
                for item in shot.offscreen_action_actor_ids
            ]
            shot.offscreen_action_target_ids = [
                entity_ids.get(item, item)
                for item in shot.offscreen_action_target_ids
            ]
            shot.scene_setting = f"Replacement setting {shot.shot_id}."
            shot.action_desc = f"Replacement visible beat {shot.shot_id}."
            shot.first_frame_desc = f"Replacement first frame {shot.shot_id}."
            shot.last_frame_desc = f"Replacement last frame {shot.shot_id}."
            shot.source_excerpt = f"Replacement excerpt {shot.shot_id}."


def _episode_6_relationship_golden() -> tuple[EpisodeScreenplay, Storyboard]:
    """Test-data-only golden graph: accident -> misread -> reveal -> decision."""
    screenplay = _screenplay()
    screenplay.episode_no = 6
    plan = screenplay.narrative_plan
    accident_id = "E6-ACCIDENT"
    misjudgment_id = "E6-MISJUDGMENT"
    reveal_id = "E6-EVIDENCE-REVEAL"
    decision_id = "E6-TRAINING-DECISION"

    # Rename the generic first event consistently; this name is fixture data,
    # never an input to production routing or validation.
    plan.events[0].event_id = accident_id
    plan.events[0].downstream_dependency_event_ids = [misjudgment_id]
    plan.adaptation_decisions[0].affected_event_ids = [accident_id]
    plan.evidence[0].anchor.id = accident_id
    plan.character_beliefs[0].anchor.id = accident_id
    for state in plan.audience_states:
        state.anchor.id = accident_id
    plan.experience_intents[0].anchor_event_ids = [accident_id]
    for path in plan.experience_intents[0].audience_paths:
        for delta in path.target_deltas:
            delta.deadline_event_id = accident_id
    plan.readability_windows[0].event_ids = [accident_id]
    next(
        proposition
        for proposition in plan.propositions
        if proposition.proposition_id == "P-story"
    ).entity_ids = ["entity-1", "character-1"]

    plan.atomic_actions.append(
        AtomicAction(
            action_id="A-E6-TRAIN",
            actor_ids=["character-1"],
            participant_deliveries=[],
            semantic_intent="Choose training in response to the disclosed evidence.",
            completion_condition="The training decision is observably committed.",
        )
    )
    plan.events.extend(
        [
            NarrativeEvent(
                event_id=misjudgment_id,
                proposition_ids=["P-story"],
                causal_parent_ids=[accident_id],
                downstream_dependency_event_ids=[reveal_id],
                must_keep=True,
                delivery_scope_id="episode-generic",
                delivery_policy="carry",
            ),
            NarrativeEvent(
                event_id=reveal_id,
                proposition_ids=["P-story"],
                causal_parent_ids=[misjudgment_id],
                downstream_dependency_event_ids=[decision_id],
                must_keep=True,
                delivery_scope_id="episode-generic",
                delivery_policy="carry",
            ),
            NarrativeEvent(
                event_id=decision_id,
                proposition_ids=["P-story"],
                causal_parent_ids=[reveal_id],
                action_ids=["A-E6-TRAIN"],
                must_keep=False,
                delivery_scope_id="episode-generic",
            ),
        ]
    )
    plan.evidence.append(
        NarrativeEvidence(
            evidence_id="EV-E6-REVEAL",
            anchor=NarrativeAnchor(type="event", id=reveal_id),
            observable_claim="New visible evidence overturns the earlier interpretation.",
            perceivable_by=["character-1", "audience"],
            supports_proposition_ids=["P-story"],
            planned_salience=0.9,
        )
    )
    plan.evidence.append(
        NarrativeEvidence(
            evidence_id="EV-E6-DECISION",
            anchor=NarrativeAnchor(type="event", id=decision_id),
            observable_claim="The resulting choice is visibly committed.",
            perceivable_by=["character-1", "audience"],
            supports_proposition_ids=["P-story"],
            planned_salience=0.8,
        )
    )
    plan.character_states.append(
        CharacterDramaticState(
            character_state_id="CDS-E6-DECISION",
            character_id="character-1",
            anchor=NarrativeAnchor(type="event", id=decision_id),
            goal_proposition_ids=["P-story"],
            pressure=0.7,
            tactic="Commit to the chosen response.",
        )
    )
    plan.character_beliefs.extend(
        [
            CharacterBeliefSnapshot.model_validate(
                {
                    "character_belief_id": "CB-E6-MISJUDGMENT",
                    "character_id": "character-1",
                    "anchor": {"type": "event", "id": misjudgment_id},
                    "perceived_evidence_ids": ["EV-1"],
                    "beliefs": [
                        {
                            "proposition_id": "P-story",
                            "stance": "suspected",
                            "confidence": 0.55,
                            "evidence_ids": ["EV-1"],
                        }
                    ],
                    "misbelief_proposition_ids": ["P-story"],
                }
            ),
            CharacterBeliefSnapshot.model_validate(
                {
                    "character_belief_id": "CB-E6-REVEAL",
                    "character_id": "character-1",
                    "anchor": {"type": "event", "id": reveal_id},
                    "perceived_evidence_ids": ["EV-1", "EV-E6-REVEAL"],
                    "beliefs": [
                        {
                            "proposition_id": "P-story",
                            "stance": "believed",
                            "confidence": 0.95,
                            "evidence_ids": ["EV-E6-REVEAL"],
                        }
                    ],
                }
            ),
            CharacterBeliefSnapshot.model_validate(
                {
                    "character_belief_id": "CB-E6-DECISION",
                    "character_id": "character-1",
                    "anchor": {"type": "event", "id": decision_id},
                    "perceived_evidence_ids": ["EV-E6-REVEAL"],
                    "beliefs": [
                        {
                            "proposition_id": "P-story",
                            "stance": "believed",
                            "confidence": 0.95,
                            "evidence_ids": ["EV-E6-REVEAL"],
                        }
                    ],
                    "decision_proposition_ids": ["P-story"],
                    "decision_basis_ids": ["EV-E6-REVEAL"],
                    "decision_action_ids": ["A-E6-TRAIN"],
                }
            ),
        ]
    )

    accident = _shot(shot_id="SH-E6-ACCIDENT", event_ids=[accident_id])
    accident.shot_contribution.shot_contribution_id = "SCN-E6-ACCIDENT"
    plan.readability_windows[0].shot_ids = [accident.shot_id]
    misjudgment = _settled_followup_shot(
        shot_no=2,
        shot_id="SH-E6-MISJUDGMENT",
        previous_shot_id=accident.shot_id,
        event_ids=[misjudgment_id],
        contribution=ShotContribution(
            shot_contribution_id="SCN-E6-MISJUDGMENT",
            character_state_delta_ids=["CB-E6-MISJUDGMENT"],
        ),
    )
    reveal = _settled_followup_shot(
        shot_no=3,
        shot_id="SH-E6-EVIDENCE-REVEAL",
        previous_shot_id=misjudgment.shot_id,
        event_ids=[reveal_id],
        contribution=ShotContribution(
            shot_contribution_id="SCN-E6-EVIDENCE-REVEAL",
            evidence_ids=["EV-E6-REVEAL"],
            character_state_delta_ids=["CB-E6-REVEAL"],
        ),
    )
    decision = _settled_followup_shot(
        shot_no=4,
        shot_id="SH-E6-TRAINING-DECISION",
        previous_shot_id=reveal.shot_id,
        event_ids=[decision_id],
        primary_action_id="A-E6-TRAIN",
        contribution=ShotContribution(
            shot_contribution_id="SCN-E6-TRAINING-DECISION",
            evidence_ids=["EV-E6-DECISION"],
            character_state_delta_ids=["CB-E6-DECISION", "CDS-E6-DECISION"],
            dramatic_pressure_delta=0.2,
        ),
    )
    decision.capacity_budget.action_phase_s = 1.0
    return screenplay, Storyboard(
        episode_no=6,
        shots=[accident, misjudgment, reveal, decision],
    )


def test_narrative_schemas_construct_a_relation_complete_minimal_fixture() -> None:
    screenplay = _screenplay()
    board = _board()

    assert screenplay.narrative_plan is not None
    assert len(screenplay.narrative_plan.audience_priors) == 2
    assert validate_screenplay_narrative(screenplay, require=True) == []
    assert validate_storyboard_narrative(board, screenplay) == []


def test_episode_6_accident_misjudgment_reveal_training_relationship_golden() -> None:
    screenplay, board = _episode_6_relationship_golden()
    plan = screenplay.narrative_plan
    events = {event.event_id: event for event in plan.events}

    assert validate_screenplay_narrative(screenplay, require=True) == []
    assert validate_storyboard_narrative(board, screenplay) == []
    assert events["E6-MISJUDGMENT"].causal_parent_ids == ["E6-ACCIDENT"]
    assert events["E6-EVIDENCE-REVEAL"].causal_parent_ids == ["E6-MISJUDGMENT"]
    assert events["E6-TRAINING-DECISION"].causal_parent_ids == [
        "E6-EVIDENCE-REVEAL"
    ]
    assert events["E6-TRAINING-DECISION"].action_ids == ["A-E6-TRAIN"]
    assert [shot.event_ids[0] for shot in board.shots] == [
        "E6-ACCIDENT",
        "E6-MISJUDGMENT",
        "E6-EVIDENCE-REVEAL",
        "E6-TRAINING-DECISION",
    ]

    # Surface/entity renaming must not change a relation-valid verdict.
    _rename_entities_and_surface_text(screenplay, board)
    assert validate_screenplay_narrative(screenplay, require=True) == []
    assert validate_storyboard_narrative(board, screenplay) == []


@pytest.mark.parametrize(
    ("genre", "form", "surface_seed"),
    [
        ("都市", "误会澄清", "city"),
        ("悬疑", "证据揭示", "mystery"),
        ("修仙", "规则验证", "cultivation"),
        ("喜剧", "反差兑现", "comedy"),
        ("现实", "纯对白", "dialogue"),
        ("动作", "多场景追逐", "chase"),
        ("心理", "回忆梦境", "dream"),
        ("剧情", "戏剧反讽", "irony"),
        ("史诗", "长铺垫兑现", "payoff"),
    ],
)
def test_cross_genre_surface_metamorphisms_preserve_relation_verdict(
    genre: str,
    form: str,
    surface_seed: str,
) -> None:
    screenplay, board = _episode_6_relationship_golden()
    screenplay.title = f"{genre}-{form}"
    _rename_entities_and_surface_text(screenplay, board)
    for index, proposition in enumerate(
        screenplay.narrative_plan.propositions,
        start=1,
    ):
        proposition.canonical_statement = (
            f"{surface_seed} surface proposition {index}"
        )
    for index, action in enumerate(
        screenplay.narrative_plan.atomic_actions,
        start=1,
    ):
        action.semantic_intent = f"{surface_seed} fictional action {index}"
        action.completion_condition = (
            f"{surface_seed} relation {index} becomes observable"
        )

    assert validate_screenplay_narrative(screenplay, require=True) == []
    assert validate_storyboard_narrative(board, screenplay) == []


def test_seeded_random_event_dags_preserve_topology_and_detect_reversal() -> None:
    rng = random.Random(20260804)
    for sample in range(30):
        screenplay = _screenplay()
        plan = screenplay.narrative_plan
        event_count = rng.randint(3, 9)
        events = [plan.events[0]]
        for index in range(1, event_count):
            eligible = [item.event_id for item in events]
            parent_count = rng.randint(1, min(3, len(eligible)))
            parents = sorted(
                rng.sample(eligible, parent_count),
                key=eligible.index,
            )
            event = NarrativeEvent(
                event_id=f"R-{sample}-{index}",
                proposition_ids=["P-story"],
                causal_parent_ids=parents,
                must_keep=True,
                delivery_scope_id="episode-generic",
                delivery_policy="carry",
            )
            events.append(event)
        plan.events = events

        assert validate_screenplay_narrative(screenplay, require=True) == []

        victim_index = rng.randrange(1, len(plan.events))
        victim = plan.events[victim_index]
        parent_id = victim.causal_parent_ids[0]
        parent_index = next(
            index for index, item in enumerate(plan.events)
            if item.event_id == parent_id
        )
        moved_parent = plan.events.pop(parent_index)
        new_victim_index = next(
            index for index, item in enumerate(plan.events)
            if item.event_id == victim.event_id
        )
        plan.events.insert(new_victim_index + 1, moved_parent)

        assert "EVENT_CAUSAL_ORDER" in _codes(
            validate_screenplay_narrative(screenplay, require=True)
        )


def test_screenplay_document_roundtrip_preserves_the_authoritative_graph() -> None:
    screenplay = _screenplay()

    restored = document_to_screenplay(screenplay_to_document(screenplay))

    assert restored.narrative_plan == screenplay.narrative_plan
    assert restored.narrative_plan is not screenplay.narrative_plan
    assert validate_screenplay_narrative(restored, require=True) == []


def test_shot_contract_roundtrip_preserves_narrative_ownership_and_paths() -> None:
    source = _shot()
    restored = apply_shot_contract(
        _shot(shot_id="", event_ids=[], shot_contribution=None, audience_state_paths=[]),
        shot_contract_dict(source),
    )

    assert restored.shot_id == "SH-1"
    assert restored.primary_action_id is None
    assert restored.event_ids == ["E-1"]
    assert restored.shot_contribution == source.shot_contribution
    assert restored.audience_state_paths == source.audience_state_paths
    assert restored.readability_window_ids == ["RW-1"]


def test_legacy_screenplay_can_be_read_but_cannot_pass_a_required_gate() -> None:
    legacy = EpisodeScreenplay(episode_no=9, title="Legacy")

    assert validate_screenplay_narrative(legacy) == []
    assert _codes(validate_screenplay_narrative(legacy, require=True)) == {
        "NARRATIVE_PLAN_MISSING"
    }
    assert document_to_screenplay(screenplay_to_document(legacy)).narrative_plan is None


def test_adapted_proposition_cannot_borrow_direct_source_evidence() -> None:
    screenplay = _screenplay()
    adapted = next(
        proposition
        for proposition in screenplay.narrative_plan.propositions
        if proposition.proposition_id == "P-story"
    )
    adapted.direct_source_evidence_ids = ["SE-1"]

    assert "ADAPTED_PROPOSITION_DIRECT_SOURCE" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )


def test_event_causal_cycle_is_rejected_even_when_all_references_exist() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan.events[0].causal_parent_ids = ["E-2"]
    screenplay.narrative_plan.events.append(
        NarrativeEvent(
            event_id="E-2",
            proposition_ids=["P-story"],
            causal_parent_ids=["E-1"],
            delivery_scope_id="episode-generic",
        )
    )

    assert "EVENT_DAG_CYCLE" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )


def test_event_order_reversal_fails_independently_of_entity_and_copy_names() -> None:
    screenplay = _screenplay()
    _append_causal_followup(screenplay)
    assert validate_screenplay_narrative(screenplay, require=True) == []

    screenplay.narrative_plan.events.reverse()
    original_codes = _codes(validate_screenplay_narrative(screenplay, require=True))
    assert "EVENT_CAUSAL_ORDER" in original_codes

    _rename_entities_and_surface_text(screenplay)
    renamed_codes = _codes(validate_screenplay_narrative(screenplay, require=True))

    assert renamed_codes == original_codes


def test_storyboard_event_order_reversal_fails_while_the_causal_graph_stays_valid() -> None:
    screenplay = _screenplay()
    _append_causal_followup(screenplay)
    board = _board()
    board.shots[0].event_ids = ["E-1", "E-2"]

    assert validate_screenplay_narrative(screenplay, require=True) == []
    assert validate_storyboard_narrative(board, screenplay) == []

    board.shots[0].event_ids.reverse()

    assert "STORYBOARD_EVENT_ORDER_INVALID" in _codes(
        validate_storyboard_narrative(board, screenplay)
    )


def test_character_belief_cannot_use_evidence_the_character_cannot_perceive() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan.evidence[0].perceivable_by = ["audience"]

    assert "CHARACTER_EVIDENCE_NOT_PERCEIVABLE" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )


def test_two_shots_cannot_both_own_the_same_primary_action() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan.atomic_actions.append(
        AtomicAction(
            action_id="A-1",
            participant_deliveries=[],
            semantic_intent="Perform one indivisible action.",
            completion_condition="Its result becomes observable.",
        )
    )
    first = _shot(primary_action_id="A-1")
    second = _shot(
        shot_no=2,
        shot_id="SH-2",
        event_ids=[],
        primary_action_id="A-1",
        shot_contribution=ShotContribution(
            shot_contribution_id="SCN-2",
            evidence_ids=["EV-1"],
            affective_delta={"registration": "held"},
        ),
        audience_state_paths=_paths(settled=True),
        planned_state_in_fact_ids=["F-after"],
        planned_delta_add_fact_ids=[],
        planned_state_out_fact_ids=["F-after"],
        readability_window_ids=[],
        narrative_boundary_from_previous=NarrativeBoundaryContract(
            boundary_id="NB-1-2",
            previous_shot_id="SH-1",
            next_shot_id="SH-2",
            narrative_relation="result_to_registration",
            cut_motivation="Move attention from the event to its reception.",
        ),
    )

    errors = validate_storyboard_narrative(
        Storyboard(episode_no=1, shots=[first, second]), screenplay
    )

    assert "ACTION_PRIMARY_OWNER_DUPLICATE" in _codes(errors)


def test_non_action_shot_is_legal_when_it_has_a_real_functional_contribution() -> None:
    screenplay = _screenplay()
    board = _board()

    assert board.shots[0].primary_action_id is None
    assert validate_storyboard_narrative(board, screenplay) == []

    board.shots[0].shot_contribution = ShotContribution(
        shot_contribution_id="SCN-empty"
    )
    assert "SHOT_CONTRIBUTION_EMPTY" in _codes(
        validate_storyboard_narrative(board, screenplay)
    )


def test_exact_planned_state_handoff_rejects_an_uncontracted_state_change() -> None:
    screenplay = _screenplay()
    second = _settled_followup_shot()
    board = Storyboard(episode_no=1, shots=[_shot(), second])

    # The second shot is deliberately action-free: an affective processing beat
    # is a legitimate contribution when its world-state hand-off is exact.
    assert second.primary_action_id is None
    assert validate_storyboard_narrative(board, screenplay) == []

    second.planned_state_in_fact_ids = ["F-before"]
    second.planned_state_out_fact_ids = ["F-before"]

    assert "SHOT_STATE_HANDOFF_BROKEN" in _codes(
        validate_storyboard_narrative(board, screenplay)
    )


def test_assimilation_task_delivered_after_its_earliest_deadline_is_rejected() -> None:
    screenplay = _screenplay()
    _append_causal_followup(screenplay)
    screenplay.narrative_plan.assimilation_tasks.append(
        AssimilationTask(
            assimilation_task_id="AT-cold",
            experience_intent_id="XI-1",
            audience_path_id="XP-cold",
            target_delta_id="XD-cold",
            downstream_dependency_event_ids=["E-2"],
            satisfaction_criteria="A blind viewer recalls the causal change before it is reused.",
            status="planned",
        )
    )
    on_time = _shot()
    on_time.shot_contribution.assimilation_task_ids = ["AT-cold"]
    followup = _settled_followup_shot(event_ids=["E-2"])
    board = Storyboard(episode_no=1, shots=[on_time, followup])

    assert validate_screenplay_narrative(screenplay, require=True) == []
    assert validate_storyboard_narrative(board, screenplay) == []

    on_time.shot_contribution.assimilation_task_ids = []
    followup.shot_contribution.assimilation_task_ids = ["AT-cold"]

    assert "ASSIMILATION_TASK_AFTER_DEADLINE" in _codes(
        validate_storyboard_narrative(board, screenplay)
    )


def test_withheld_proposition_cannot_leak_before_its_disclosure_anchor() -> None:
    screenplay = _screenplay()
    plan = screenplay.narrative_plan
    plan.propositions.append(
        NarrativeProposition(
            proposition_id="P-secret",
            semantic_identity_key="adapted-later-resolution",
            canonical_statement="A later observation resolves the open interpretation.",
            narrative_domain="adapted_story",
        )
    )
    plan.adaptation_decisions[0].adapted_proposition_ids.append("P-secret")
    plan.adaptation_decisions[0].affected_event_ids.append("E-2")
    _append_causal_followup(
        screenplay,
        proposition_ids=["P-secret"],
    )
    plan.evidence.append(
        NarrativeEvidence(
            evidence_id="EV-secret",
            # A sequence anchor makes timing depend on the contribution edge,
            # not on a story-specific keyword or a duplicate event assertion.
            anchor=NarrativeAnchor(type="sequence", id="episode-generic"),
            observable_claim="A perceivable observation discloses the held-back proposition.",
            perceivable_by=["character-1", "audience"],
            supports_proposition_ids=["P-secret"],
            planned_salience=0.8,
        )
    )
    plan.experience_intents[0].withheld_propositions.append(
        WithheldProposition(
            proposition_id="P-secret",
            reason="Preserve a deliberate uncertainty until the causal reveal.",
            future_disclosure_anchor=NarrativeAnchor(type="event", id="E-2"),
        )
    )
    disclosure = _settled_followup_shot(
        event_ids=["E-2"],
        contribution=ShotContribution(
            shot_contribution_id="SCN-disclosure",
            evidence_ids=["EV-secret"],
        ),
    )
    board = Storyboard(episode_no=1, shots=[_shot(), disclosure])

    assert validate_screenplay_narrative(screenplay, require=True) == []
    assert validate_storyboard_narrative(board, screenplay) == []

    board.shots[0].shot_contribution.evidence_ids.append("EV-secret")
    disclosure.shot_contribution.evidence_ids = []
    disclosure.shot_contribution.affective_delta = {"uncertainty": "held"}

    assert "INTENDED_AMBIGUITY_BROKEN" in _codes(
        validate_storyboard_narrative(board, screenplay)
    )


def test_multi_prior_metrics_use_the_low_path_not_the_average_viewer() -> None:
    screenplay = _screenplay()
    report = NarrativeReviewReport.model_validate(
        {
            "narrative_review_report_id": "NRR-1",
            "scope_id": "episode-generic",
            "experience_intent_ids": ["XI-1"],
            "target_delta_results": [
                {
                    "audience_prior_id": "AP-cold",
                    "target_delta_id": "XD-cold",
                    "result": "satisfied",
                    "predicted_score": 0.95,
                },
                {
                    "audience_prior_id": "AP-context",
                    "target_delta_id": "XD-context",
                    "result": "missed",
                    "predicted_score": 0.2,
                },
            ],
            "decision": "pass",
        }
    )

    metrics = compute_narrative_metrics(screenplay, _board(), report)

    assert metrics["per_prior_understanding"] == {
        "AP-cold": 1.0,
        "AP-context": 0.0,
    }
    assert metrics["low_percentile_understanding"] == 0.0
    assert metrics["target_delta_delivery_ratio"] == 1.0
    assert metrics["shot_contribution_coverage"] == 1.0
    assert metrics["audience_processing_debt_s"] == 0.0
    assert metrics["narrative_ready"] is False


def test_narrative_ready_requires_a_complete_satisfied_result_for_every_prior_path() -> None:
    screenplay = _screenplay()
    observations = _blind_observations()
    complete_report = NarrativeReviewReport.model_validate(
        {
            "narrative_review_report_id": "NRR-complete",
            "scope_id": "episode-generic",
            "experience_intent_ids": ["XI-1"],
            "observation_ids": ["BAO-cold", "BAO-context"],
            "target_delta_results": [
                {
                    "audience_prior_id": "AP-cold",
                    "target_delta_id": "XD-cold",
                    "result": "satisfied",
                    "predicted_score": 0.95,
                    "supporting_observation_ids": ["BAO-cold"],
                    "supporting_evidence_ids": ["EV-1"],
                    "reason": "The cold recall registered the visible result.",
                },
                {
                    "audience_prior_id": "AP-context",
                    "target_delta_id": "XD-context",
                    "result": "satisfied",
                    "predicted_score": 0.95,
                    "supporting_observation_ids": ["BAO-context"],
                    "supporting_evidence_ids": ["EV-1"],
                    "reason": "The contextual recall registered the same visible result.",
                },
            ],
            **_review_dimension_results(),
            "decision": "pass",
        }
    )

    complete_metrics = compute_narrative_metrics(
        screenplay,
        _board(),
        complete_report,
        observations=observations,
        human_calibration={
            "ready": True,
            "status": "calibrated",
            "calibration_score": 1.0,
        },
    )

    assert complete_metrics["low_percentile_understanding"] == 1.0
    assert complete_metrics["narrative_ready"] is True

    uncalibrated_metrics = compute_narrative_metrics(
        screenplay,
        _board(),
        complete_report,
        observations=observations,
    )
    assert uncalibrated_metrics["narrative_ready"] is False

    incomplete_report = complete_report.model_copy(deep=True)
    incomplete_report.target_delta_results.pop()

    incomplete_metrics = compute_narrative_metrics(
        screenplay,
        _board(),
        incomplete_report,
        observations=observations,
        human_calibration={
            "ready": True,
            "status": "calibrated",
            "calibration_score": 1.0,
        },
    )

    assert incomplete_metrics["narrative_ready"] is False


def _blind_observations() -> list[BlindAudienceObservation]:
    recall = {
        "recognized_entities": ["entity-1"],
        "inferred_propositions": ["A visible result occurred."],
        "causal_hypotheses": ["The observable event caused the result."],
        "character_goal_hypotheses": [],
        "active_question_ids": [],
    }
    return [
        BlindAudienceObservation(
            observation_id="BAO-cold",
            audience_prior_id="AP-cold",
            anchor=NarrativeAnchor(type="sequence", id="episode-generic"),
            spontaneous_recall=recall,
            spontaneous_supporting_evidence_ids=["EV-1"],
            supporting_evidence_ids=["EV-1"],
            confidence=0.8,
        ),
        BlindAudienceObservation(
            observation_id="BAO-context",
            audience_prior_id="AP-context",
            anchor=NarrativeAnchor(type="sequence", id="episode-generic"),
            spontaneous_recall=recall,
            spontaneous_supporting_evidence_ids=["EV-1"],
            supporting_evidence_ids=["EV-1"],
            confidence=0.8,
        ),
    ]


def _review_dimension_results() -> dict[str, dict]:
    applicable = {
        "applicability": "applies",
        "passed": True,
        "evidence_ids": ["EV-1"],
        "reason": "Observed.",
    }
    return {
        "character_goal_readability_result": applicable,
        "attention_alignment_result": applicable,
        "spatial_temporal_orientation_result": applicable,
        "affective_alignment_result": applicable,
        "relationship_change_result": applicable,
        "stakes_readability_result": applicable,
        "pressure_rhythm_result": applicable,
        "action_functional_repetition_result": {
            "applicability": "not_applicable",
            "passed": False,
            "evidence_ids": [],
            "reason": "No semantically equivalent action pair exists in this fixture.",
        },
        "next_expectation_result": applicable,
        "intentional_ambiguity_result": applicable,
        "low_percentile_result": {
            "passed": True,
            "per_prior": {
                "AP-cold": {
                    "passed": True,
                    "target_delta_ids": ["XD-cold"],
                    "reason": "The cold prior path passed.",
                },
                "AP-context": {
                    "passed": True,
                    "target_delta_ids": ["XD-context"],
                    "reason": "The contextual prior path passed.",
                },
            },
            "reason": "All prior paths passed.",
        },
    }


def test_blind_reader_payload_cannot_leak_truth_or_director_targets() -> None:
    screenplay = _screenplay()
    prior = screenplay.narrative_plan.audience_priors[0]

    payload = blind_reader_payload(prior, screenplay, _board())
    serialized = json.dumps(payload, ensure_ascii=False)

    for forbidden_key in (
        "narrative_plan",
        "source_evidence",
        "propositions",
        "director_objective",
        "target_deltas",
        "assimilation_tasks",
        "reserved_future_event_ids",
    ):
        assert forbidden_key not in serialized


def test_blind_review_cannot_pass_when_any_prior_path_is_missed() -> None:
    screenplay = _screenplay()
    observations = _blind_observations()
    report = NarrativeReviewReport.model_validate(
        {
            "narrative_review_report_id": "NRR-false-pass",
            "scope_id": "episode-generic",
            "experience_intent_ids": ["XI-1"],
            "observation_ids": [item.observation_id for item in observations],
            "target_delta_results": [
                {
                    "audience_prior_id": "AP-cold",
                    "target_delta_id": "XD-cold",
                    "result": "satisfied",
                    "predicted_score": 0.95,
                },
                {
                    "audience_prior_id": "AP-context",
                    "target_delta_id": "XD-context",
                    "result": "missed",
                    "predicted_score": 0.2,
                },
            ],
            "decision": "pass",
        }
    )

    errors = validate_blind_review(screenplay, observations, report)

    assert "REVIEW_FALSE_PASS" in _codes(errors)


def test_narrative_metrics_publish_the_full_contract_and_keep_unknown_denominators() -> None:
    metrics = compute_narrative_metrics(_screenplay(), _board())
    expected_keys = {
        "contract_present",
        "proposition_mapping_coverage_rate",
        "event_coverage_rate",
        "unbound_reference_count",
        "event_order_violation_count",
        "duplicate_primary_action_count",
        "state_regression_count",
        "character_motivation_gap_count",
        "readability_window_violation_count",
        "shot_capacity_violation_count",
        "empty_shot_contribution_count",
        "scene_contract_pass_rate",
        "arc_contract_pass_rate",
        "setup_payoff_closure_rate",
        "experience_intent_coverage_rate",
        "assimilation_deadline_pass_rate",
        "cold_audience_target_belief_rate",
        "cold_audience_false_causal_inference_rate",
        "character_goal_readability_rate",
        "spatial_temporal_orientation_rate",
        "cold_audience_affective_alignment_rate",
        "relationship_change_readability_rate",
        "stakes_readability_rate",
        "pressure_rhythm_alignment_rate",
        "next_expectation_alignment_rate",
        "intentional_ambiguity_fidelity_rate",
        "premature_reveal_rate",
        "attention_collision_rate",
        "audience_processing_debt",
        "cold_audience_inference_variance",
        "cognitive_bridge_marginal_gain",
        "ineffective_bridge_shot_rate",
        "blind_ai_human_comprehension_correlation",
        "target_delta_delivery_ratio",
        "shot_contribution_coverage",
        "audience_processing_debt_s",
        "max_audience_processing_debt_s",
        "per_prior_understanding",
        "low_percentile_understanding",
        "inference_variance",
        "narrative_ready",
    }

    assert expected_keys <= set(metrics)
    for unknown_without_a_denominator in (
        "scene_contract_pass_rate",
        "arc_contract_pass_rate",
        "setup_payoff_closure_rate",
        "assimilation_deadline_pass_rate",
        "cold_audience_target_belief_rate",
        "character_goal_readability_rate",
        "relationship_change_readability_rate",
        "stakes_readability_rate",
        "pressure_rhythm_alignment_rate",
        "next_expectation_alignment_rate",
        "intentional_ambiguity_fidelity_rate",
        "premature_reveal_rate",
        "cognitive_bridge_marginal_gain",
        "ineffective_bridge_shot_rate",
        "blind_ai_human_comprehension_correlation",
        "low_percentile_understanding",
    ):
        assert metrics[unknown_without_a_denominator] is None


def test_ai_human_calibration_requires_enough_nonconstant_paired_samples() -> None:
    insufficient = blind_ai_human_comprehension_correlation(
        [0.2, 0.8], [0.1, 0.9], min_samples=8
    )
    constant = blind_ai_human_comprehension_correlation(
        [0.5] * 8, [0.1 * index for index in range(8)], min_samples=8
    )
    calibrated = blind_ai_human_comprehension_correlation(
        [0.1 * index for index in range(8)],
        [0.2 * index for index in range(8)],
        min_samples=8,
    )

    assert insufficient == {
        "status": "needs_review",
        "sample_count": 2,
        "correlation": None,
    }
    assert constant["status"] == "needs_review"
    assert constant["correlation"] is None
    assert calibrated["status"] == "calibrated"
    assert calibrated["sample_count"] == 8
    assert calibrated["correlation"] == pytest.approx(1.0)

    metrics = compute_narrative_metrics(
        _screenplay(),
        _board(),
        human_calibration={
            "ai_scores": [0.1 * index for index in range(8)],
            "human_scores": [0.2 * index for index in range(8)],
            "min_samples": 8,
        },
    )
    assert metrics["blind_ai_human_comprehension_correlation"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# episodes.hook / episodes.cliffhanger 承接文案（app.stages._first_shot_rule
# 是逐镜层规则 6/7 的第二套实现，见 app.stages 顶部同名规则）与溯源校验
# ---------------------------------------------------------------------------


def test_first_episode_shot_rule_uses_special_branch_and_stays_blank() -> None:
    from app.stages import _first_shot_rule

    rule = _first_shot_rule(
        {"episode_no": 1, "hook": "", "cliffhanger": ""},
        narrative_authority=False,
    )
    assert "【第一集第一镜=全片开场建场镜" in rule
    assert "本集开场无需承接钩子" in rule
    # Empty hook must never be string-interpolated into a "承接上一集结尾：" style
    # instruction; EP1 has no previous episode so this must read as a plain
    # statement, not a broken reference to a blank value.
    assert "承接上一集真实结尾：" not in rule


def test_second_episode_shot_rule_carries_prev_ending_hook_not_old_ban() -> None:
    from app.stages import _first_shot_rule

    hook = "神秘来电在深夜再次响起，屏幕上显示的是三年前失联的号码。"
    rule = _first_shot_rule(
        {"episode_no": 2, "hook": hook, "cliffhanger": ""},
        narrative_authority=False,
    )
    assert f"第 1 个镜头必须承接上一集真实结尾：{hook}" in rule
    assert f"不得凭空续写 {hook} 之外的新剧情" in rule
    # The old field-empty-implies-forbidden phrasing must be gone from this
    # branch now that hook is non-empty.
    assert "禁止发明额外开场钩子" not in rule
    assert "禁止因 hook 为空发明额外钩子" not in rule


def test_empty_hook_and_cliffhanger_never_interpolate_blank_value() -> None:
    from app.stages import _first_shot_rule

    rule = _first_shot_rule(
        {"episode_no": 3, "hook": "", "cliffhanger": ""},
        narrative_authority=False,
    )
    assert "承接上一集真实结尾：" not in rule
    assert "本集真实尾钩：" not in rule
    assert "（空）" not in rule
    assert "第 1 个镜头按剧本真实开场自然进入" in rule
    assert "最后 1 个镜头只收束到剧本/原文已有状态" in rule
    assert "不得发明下一集钩子" in rule


def test_ending_hook_is_grounded_rejects_fabricated_content() -> None:
    from app.validators import ending_hook_is_grounded

    source = (
        "李明推开吱呀作响的木门，看见桌上摆着一封没有署名的信，"
        "信封已经拆开，里面的信纸却是空的。"
    )
    grounded = "桌上那封没有署名的信，信封已经拆开，信纸却是空的。"
    fabricated = "外星飞船在城市上空缓缓降落，全城陷入一片死寂。"

    assert ending_hook_is_grounded("", source) is True
    assert ending_hook_is_grounded("   ", source) is True
    assert ending_hook_is_grounded(grounded, source) is True
    assert ending_hook_is_grounded(fabricated, source) is False


_ENDING_HOOK_TEST_SCRIPT = (
    "第一场 内景 李明家客厅 夜\n"
    "李明推开木门，看见王芳正端着一杯热茶站在窗边。她转过身，眼神里带着几分犹豫。\n"
    "\"你回来了。\"王芳把热茶放在桌上，声音有些发抖。\n"
    "李明没有说话，只是静静地看着她，心里升起一种说不出的沉重。\n"
    "窗外下起了小雨，屋子里的气氛也跟着凝固起来。\n"
    "王芳低下头，说起了当年离开家乡的往事，那段日子里她一个人扛下了所有债务，"
    "还要照顾年迈的母亲。\n"
    "李明听着，手指无意识地摩挲着木门的边缘，仿佛想抓住什么。\n"
    "\"对不起，\"王芳终于说出这句话，眼泪落了下来。\n"
    "李明伸手，轻轻握住她的手，两人在沉默中达成了某种和解。\n\n"
    "第二场 内景 医院走廊 日\n"
    "李明的母亲躺在病床上，脸色苍白。医生说情况不容乐观，需要尽快手术。\n"
    "王芳赶到医院，看到李明疲惫的样子，心疼地递给他一杯热茶。\n"
    "\"你先歇一会儿，\"她说，\"剩下的交给我。\"\n"
    "李明摇摇头，说这是他自己的责任，不能全推给王芳。\n"
    "两人在走廊里争执了几句，最后还是决定一起面对。\n\n"
    "第三场 内景 老宅厨房 黄昏\n"
    "王芳在厨房里忙碌，锅里炖着汤。她的手机响了，是一个陌生号码。\n"
    "她接起电话，对方沉默了几秒才开口，说了一个让她脸色骤变的消息。\n"
    "王芳挂了电话，靠在灶台边，久久没有说话。\n"
    "李明推开厨房的木门，看到她的表情不对，连忙问怎么了。\n"
    "王芳摇摇头，说没事，转身继续炖汤，但眼神里藏着不安。\n"
)

# 独立 code review 实测的四条绕过样本：全部复用正文真实词汇（李明/王芳/木门/热茶/
# 窗边等），拼出正文里从未发生的事件；对纯 2-gram 覆盖率门禁（旧版
# ending_hook_is_grounded，无 events 参数）逐一实测覆盖率 0.40~0.54，全部高于门槛
# 0.2，即绕过成功。这里复用同一批语料验证新的结构化校验能否拦住它们。
_REVIEWER_BYPASS_FABRICATIONS = [
    "李明看着王芳，忽然想起多年前的一段往事，心里涌起一阵说不清的烦躁。",
    "窗外的天色渐渐暗了下来，李明却在这时收到了一条神秘的短信。",
    "王芳端着热茶走进厨房，却发现李明早已不知去向。",
    "他转身走向窗边，望着楼下车水马龙的街道，忽然做出一个惊人的决定。",
]

_REAL_PARAPHRASED_HOOKS = [
    "王芳接到一个神秘电话，脸色骤变，久久说不出话来。",
    "医生说李明母亲手术情况不容乐观，李明与王芳争执后决定一起面对。",
    "王芳终于说出当年离乡扛债的往事，李明握住她的手，两人和解。",
]


def _ending_hook_test_events() -> list[StoryEvent]:
    return [
        StoryEvent(event_id="E1", trigger="李明推开木门",
                   visible_change="王芳端着热茶站在窗边", state_out="两人相对无言",
                   source_fact="李明回家看见王芳"),
        StoryEvent(event_id="E2", trigger="王芳提起往事",
                   visible_change="王芳说出当年独自扛债的经历", state_out="李明沉默摩挲木门",
                   source_fact="王芳讲述离乡扛债往事"),
        StoryEvent(event_id="E3", trigger="王芳道歉", visible_change="王芳落泪并道歉",
                   state_out="李明握住王芳的手达成和解", source_fact="王芳道歉两人和解"),
        StoryEvent(event_id="E4", trigger="医生诊断",
                   visible_change="李明母亲手术情况不容乐观",
                   state_out="李明王芳在走廊争执后决定共同面对",
                   source_fact="母亲病情严重需要手术"),
        StoryEvent(event_id="E5", trigger="陌生来电",
                   visible_change="王芳接到陌生号码来电脸色骤变",
                   state_out="王芳挂断电话久久不语", source_fact="王芳接到神秘电话"),
    ]


def test_ending_hook_is_grounded_bigram_floor_alone_lets_vocabulary_reuse_through() -> None:
    """不传 events（旧签名/早期生成阶段）时保持原有行为——记录已证实的绕过面，
    证明这一层单独不足以防编造，新增的结构化层（见下一条测试）才是真正的门禁。"""
    from app.validators import ending_hook_is_grounded

    bypassed = [
        s for s in _REVIEWER_BYPASS_FABRICATIONS
        if ending_hook_is_grounded(s, _ENDING_HOOK_TEST_SCRIPT) is True
    ]
    assert bypassed, "预期纯 2-gram 覆盖率门槛对复用词汇的编造样本至少部分失效"


def test_ending_hook_is_grounded_with_events_rejects_reviewer_bypass_fabrications() -> None:
    """结构化校验：传入 events 后，四条已证实的绕过样本必须全部被拦下。"""
    from app.validators import ending_hook_is_grounded

    events = _ending_hook_test_events()
    for fabricated in _REVIEWER_BYPASS_FABRICATIONS:
        assert ending_hook_is_grounded(
            fabricated, _ENDING_HOOK_TEST_SCRIPT, events=events,
        ) is False, f"编造钩子未被拦下：{fabricated}"


def test_ending_hook_is_grounded_with_events_accepts_real_paraphrased_hooks() -> None:
    """反向测试：真实、来自正文事件的钩子（哪怕高度改写）不能被结构化层误杀。"""
    from app.validators import ending_hook_is_grounded

    events = _ending_hook_test_events()
    for grounded in _REAL_PARAPHRASED_HOOKS:
        assert ending_hook_is_grounded(
            grounded, _ENDING_HOOK_TEST_SCRIPT, events=events,
        ) is True, f"真实钩子被误判为编造：{grounded}"


def test_ending_hook_is_grounded_rejects_hook_only_matching_unapproved_addition() -> None:
    """钩子词汇只对得上一条模型自报的未授权改编新增事件时，不能算已溯源。"""
    from app.validators import ending_hook_is_grounded

    unauthorized_events = [
        StoryEvent(
            event_id="E9", trigger="李明失踪", visible_change="李明忽然不知去向",
            state_out="王芳独自在厨房", source_fact="剧本原文暗示",
            adaptation_addition=True, approved=False,
        ),
    ]
    assert ending_hook_is_grounded(
        "王芳端着热茶走进厨房，却发现李明早已不知去向。",
        _ENDING_HOOK_TEST_SCRIPT,
        events=unauthorized_events,
    ) is False


def test_adaptation_hook_errors_alone_misses_unmarked_ending_hook_fabrication() -> None:
    """独立 review 的调研问题：adaptation_hook_errors 现状能否单独拦住编造钩子？

    结论（本测试锁定）：不能。它只在模型*自报*某条事件为
    adaptation_addition 且未 approved 时才报错；对本任务要防的失败模式——
    模型直接写一句编造的 ending_hook 散文，压根不创建任何 events 记录——
    events 为空，unauthorized 列表天然为空，函数返回 []。这正是为什么防编造
    改用 ending_hook_is_grounded(events=...) 结构化比对，而不是单独依赖这个函数。
    """
    from app.continuity import adaptation_hook_errors

    fabricated_script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=_ENDING_HOOK_TEST_SCRIPT,
        ending_hook=_REVIEWER_BYPASS_FABRICATIONS[0],
        events=[],
    )
    assert adaptation_hook_errors(
        fabricated_script, {"cliffhanger": "", "hook": ""},
    ) == []
    assert adaptation_hook_errors(
        fabricated_script, {"cliffhanger": "旧钩子", "hook": "旧钩子"},
    ) == []


_EP4_ENDING_HOOK_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "ep4_ending_hook_grounding_ep_3b07c59c0856.json"
)


def _load_ep4_ending_hook_fixture() -> dict:
    """EP4（episode ep_3b07c59c0856）真实全量生成的只读快照：269 条 events +
    5998 字 full_script_text，取自 episodes.screenplay_json；ending_hook 取自
    art_872dff9a8234.metadata.ending_hook（旧判据清空前的原始钩子）。这是本次
    修复动机的第一手证据：旧的单事件 Tier A（覆盖率需 ≥0.34）在这批 269 条
    原子事件上，单事件最佳匹配只有 0.2162，把这条模型正确、忠实原文的收尾钩子
    误判为编造并清空。"""
    with _EP4_ENDING_HOOK_FIXTURE.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    events = [StoryEvent.model_validate(item) for item in payload["events"]]
    return {
        "ending_hook": payload["ending_hook"],
        "full_script_text": payload["full_script_text"],
        "events": events,
    }


def test_ending_hook_is_grounded_accepts_ep4_real_multi_event_summary_hook() -> None:
    """正向：EP4 真实钩子 + 真实 269 条 events 必须通过（回归此次要修的假阳性）。"""
    from app.validators import (
        ENDING_HOOK_EVENT_COVERAGE,
        ending_hook_grounding_report,
        ending_hook_is_grounded,
    )

    fixture = _load_ep4_ending_hook_fixture()

    assert ending_hook_is_grounded(
        fixture["ending_hook"], fixture["full_script_text"], events=fixture["events"],
    ) is True

    report = ending_hook_grounding_report(
        fixture["ending_hook"], fixture["full_script_text"], events=fixture["events"],
    )
    assert report["grounded"] is True
    # 锁定根因：单事件 Tier A 在这批事件表上确实摸不到 0.34（旧判据据此误清空），
    # 新判据必须靠 Tier B（末尾事件滑动窗口）才能放行，而不是碰巧靠 Tier A。
    assert report["best_event_coverage"] < ENDING_HOOK_EVENT_COVERAGE
    assert report["tier"] == "tierB"
    assert report["window"] is not None
    assert report["window"]["passed"] is True
    assert report["window"]["contributors"] >= 2
    # ending_hook 描述的是"本集结尾"，命中窗口必须落在事件表尾部（最后一条
    # 事件 E269），而不是全篇任意位置碰运气拼出来的覆盖率。
    assert report["window"]["event_ids"][-1] == fixture["events"][-1].event_id


def test_ending_hook_is_grounded_window_rejects_reviewer_bypass_fabrications() -> None:
    """反向补充：四条已证实的绕过样本在新的窗口判据（report 粒度）下，
    仍必须落在 Tier A、Tier B 都不通过的 'ungrounded'，而不是侥幸靠窗口放宽混过去。"""
    from app.validators import ending_hook_grounding_report

    events = _ending_hook_test_events()
    for fabricated in _REVIEWER_BYPASS_FABRICATIONS:
        report = ending_hook_grounding_report(
            fabricated, _ENDING_HOOK_TEST_SCRIPT, events=events,
        )
        assert report["grounded"] is False, f"编造钩子未被拦下：{fabricated}"
        assert report["tier"] in ("layer1_fail", "ungrounded"), (
            f"编造钩子应止步于 layer1_fail 或 ungrounded，"
            f"实际 tier={report['tier']}：{fabricated}"
        )


def test_ending_hook_is_grounded_window_requires_multiple_contributing_events() -> None:
    """Tier B 的核心防线：窗口覆盖率达标但只有 1 条事件独立贡献命中时，仍必须
    拒绝——这正是编造钩子"运气好挂上一条事件，其余事件净贡献为零"的典型信号，
    区别于真实多事件摘要"证据分布在多条事件里"的信号。"""
    from app.validators import ending_hook_grounding_report

    # 构造一个人为窗口：只有最后一条事件真正贡献了命中 2-gram，其余相邻事件的
    # 命中都是该事件已覆盖内容的子集（不新增任何独立命中）——模拟 fab3 绕过
    # 样本"王芳端着热茶走进厨房，却发现李明早已不知去向"对 all-events 窗口的
    # 真实行为：pooled 覆盖率达到 0.3（高于 Tier B 门槛 0.28），但只有 1 条
    # 事件独立贡献。
    events = [
        StoryEvent(event_id="F1", trigger="李明推开木门", visible_change="王芳端着热茶站在窗边",
                   state_out="两人相对无言", source_fact="李明回家看见王芳"),
        StoryEvent(event_id="F2", trigger="无关内容一", visible_change="无关内容一",
                   state_out="无关内容一", source_fact="无关内容一"),
        StoryEvent(event_id="F3", trigger="无关内容二", visible_change="无关内容二",
                   state_out="无关内容二", source_fact="无关内容二"),
    ]
    report = ending_hook_grounding_report(
        "王芳端着热茶走进厨房，却发现李明早已不知去向。",
        "李明推开木门，王芳端着热茶站在窗边。两人相对无言。",
        events=events,
    )
    assert report["grounded"] is False
    assert report["tier"] == "ungrounded"
    # 窗口本身的覆盖率其实达标了（否则这条测试测不出"贡献者数量门禁"本身在
    # 起作用），只是贡献事件数不够——断言这一点，避免这条测试未来因为覆盖率
    # 恰好不达标而"意外通过"，掩盖了真正要锁定的门禁。
    if report["window"]["coverage"] >= 0.28:
        assert report["window"]["contributors"] < 2


def test_ending_hook_grounding_report_empty_hook_is_legitimate() -> None:
    """边界：原文本集确已完结、无遗留悬念时，ending_hook 留空必须合法放行，
    不能被误判为"编造后被清空"。"""
    from app.validators import ending_hook_grounding_report

    report = ending_hook_grounding_report("", _ENDING_HOOK_TEST_SCRIPT, events=_ending_hook_test_events())
    assert report["grounded"] is True
    assert report["tier"] == "empty"

    report_blank = ending_hook_grounding_report("   ", _ENDING_HOOK_TEST_SCRIPT, events=_ending_hook_test_events())
    assert report_blank["grounded"] is True
    assert report_blank["tier"] == "empty"


def test_ending_hook_is_grounded_window_rejects_unapproved_addition_only_window() -> None:
    """边界：钩子只能对上一串未批准 adaptation_addition 事件（哪怕文本上刻意
    让它们相邻、覆盖率很高），Tier B 也必须拒绝——未批准新增在窗口比对之前
    就已被 _ending_hook_eligible_events 整条过滤掉，候选池里根本凑不出
    2 条可用的相邻事件，天然回退到 Tier A 已确认失败的结果。"""
    from app.validators import ending_hook_grounding_report

    unauthorized_events = [
        StoryEvent(
            event_id="U1", trigger="李明失踪", visible_change="李明忽然不知去向",
            state_out="王芳独自在厨房", source_fact="剧本原文暗示",
            adaptation_addition=True, approved=False,
        ),
        StoryEvent(
            event_id="U2", trigger="王芳端着热茶走进厨房", visible_change="王芳端着热茶走进厨房",
            state_out="却发现李明早已不知去向", source_fact="剧本原文暗示",
            adaptation_addition=True, approved=False,
        ),
    ]
    report = ending_hook_grounding_report(
        "王芳端着热茶走进厨房，却发现李明早已不知去向。",
        _ENDING_HOOK_TEST_SCRIPT,
        events=unauthorized_events,
    )
    assert report["grounded"] is False
    assert report["tier"] == "ungrounded"


def test_clear_ungrounded_ending_hook_leaves_observable_evidence() -> None:
    """清空静默性回归测试：app/stages.py 两处生成期清空点（场次分片路径与
    legacy baseline 路径共用的 _clear_ungrounded_ending_hook）以前直接
    `script.ending_hook = ""`，不留任何观测记录——数据上完全无法区分"原文
    真的没钩子（合法留空）"和"被误杀"。EP4 269 条原子事件那次假阳性就是这样
    被人工偶然发现的（追问"为什么这一集生成得比别的慢"才挖出来）。这次修复
    要求清空动作必须留下可在 provider_calls 里查到的证据。"""
    from app import db, stages

    fabricated_hook = "外星飞船在城市上空缓缓降落，全城陷入一片死寂。"
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=_ENDING_HOOK_TEST_SCRIPT,
        ending_hook=fabricated_hook,
        events=_ending_hook_test_events(),
    )
    stages._clear_ungrounded_ending_hook(
        script, episode_id="ep-stages-observability-1", source="unit_test",
    )
    assert script.ending_hook == ""

    rows = db.get_conn().execute(
        """SELECT meta FROM provider_calls
            WHERE kind='ending_hook_grounding_rejected' AND status='REJECTED'
            ORDER BY id DESC""",
    ).fetchall()
    assert rows, "ending_hook 被判定编造并清空时必须留下可查的观测记录"
    meta = json.loads(rows[0]["meta"])
    assert meta["episode_id"] == "ep-stages-observability-1"
    assert meta["source"] == "unit_test"
    assert meta["hook_text"] == fabricated_hook
    assert meta["tier"] in ("layer1_fail", "ungrounded")
    assert isinstance(meta["layer1_coverage"], (int, float))


def test_clear_ungrounded_ending_hook_is_noop_when_grounded() -> None:
    """反向：真实、有依据的钩子不能被这条观测化改动误伤——既不清空，也不该
    产生一条"编造被拒绝"的假记录。"""
    from app import db, stages

    real_hook = _REAL_PARAPHRASED_HOOKS[0]
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=_ENDING_HOOK_TEST_SCRIPT,
        ending_hook=real_hook,
        events=_ending_hook_test_events(),
    )
    before = db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM provider_calls WHERE kind='ending_hook_grounding_rejected'",
    ).fetchone()["n"]
    stages._clear_ungrounded_ending_hook(
        script, episode_id="ep-stages-observability-2", source="unit_test",
    )
    assert script.ending_hook == real_hook
    after = db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM provider_calls WHERE kind='ending_hook_grounding_rejected'",
    ).fetchone()["n"]
    assert after == before
