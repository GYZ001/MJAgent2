from __future__ import annotations

import json

import pytest

from app.continuity import apply_shot_contract, shot_contract_dict
from app.narrative import (
    blind_ai_human_comprehension_correlation,
    blind_reader_payload,
    compute_narrative_metrics,
    validate_blind_review,
    validate_screenplay_narrative,
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
                    "source_span": {"chapter_id": "1", "start": 12, "end": 40},
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
            target_ids=["entity-1"],
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
    decision.offscreen_action_target_ids = ["entity-1"]
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
                },
                {
                    "audience_prior_id": "AP-context",
                    "target_delta_id": "XD-context",
                    "result": "missed",
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
                    "supporting_observation_ids": ["BAO-cold"],
                    "supporting_evidence_ids": ["EV-1"],
                    "reason": "The cold recall registered the visible result.",
                },
                {
                    "audience_prior_id": "AP-context",
                    "target_delta_id": "XD-context",
                    "result": "satisfied",
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
    )

    assert complete_metrics["low_percentile_understanding"] == 1.0
    assert complete_metrics["narrative_ready"] is True

    incomplete_report = complete_report.model_copy(deep=True)
    incomplete_report.target_delta_results.pop()

    incomplete_metrics = compute_narrative_metrics(
        screenplay,
        _board(),
        incomplete_report,
        observations=observations,
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
                },
                {
                    "audience_prior_id": "AP-context",
                    "target_delta_id": "XD-context",
                    "result": "missed",
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
