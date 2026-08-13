from __future__ import annotations

import json

from app.continuity import apply_shot_contract, shot_contract_dict
from app.production.screenplay_document import (
    document_to_screenplay,
    rederive_projections,
    screenplay_to_document,
)
from app.schemas import (
    ActionParticipantDelivery,
    AudienceStatePathRef,
    EpisodeScreenplay,
    NarrativeBoundaryContract,
    NarrativeContinuityPlan,
    Shot,
    ShotContribution,
    StoryboardOutline,
    StoryboardOutlineShot,
)


def _narrative_plan() -> NarrativeContinuityPlan:
    """A compact cross-layer plan with one representative of every aggregate."""
    return NarrativeContinuityPlan.model_validate(
        {
            "scope_id": "episode-generic",
            "source_evidence": [
                {
                    "source_evidence_id": "SE-1",
                    "source_span": {"chapter_id": "chapter-1", "start": 3, "end": 21},
                    "verbatim_excerpt": "A source-grounded change occurs.",
                    "confidence": 0.98,
                }
            ],
            "propositions": [
                {
                    "proposition_id": "P-source",
                    "semantic_identity_key": "source-dormant-rule",
                    "canonical_statement": "The source establishes a dormant rule.",
                    "narrative_domain": "source_canon",
                    "entity_ids": ["entity-1"],
                    "direct_source_evidence_ids": ["SE-1"],
                    "domain_truth_status": "true",
                },
                {
                    "proposition_id": "P-adapted",
                    "semantic_identity_key": "adapted-gradual-rule-reveal",
                    "canonical_statement": "The adapted story reveals the rule gradually.",
                    "narrative_domain": "adapted_story",
                    "entity_ids": ["entity-1"],
                    "direct_source_evidence_ids": [],
                    "domain_truth_status": "true",
                },
            ],
            "adaptation_decisions": [
                {
                    "adaptation_decision_id": "AD-1",
                    "source_proposition_ids": ["P-source"],
                    "adapted_proposition_ids": ["P-adapted"],
                    "relation": "transform",
                    "creative_reason": "Preserve causality while changing disclosure order.",
                    "protected_causal_effect_ids": ["E-2"],
                    "affected_event_ids": ["E-1", "E-2"],
                }
            ],
            "state_facts": [
                {
                    "fact_id": "F-before",
                    "proposition_id": "P-adapted",
                    "subject_id": "entity-1",
                    "predicate_id": "rule_awareness",
                    "value": {"kind": "text", "data": "unknown"},
                    "time_scope": "main@1",
                    "visibility": "visible",
                    "provenance": "screenplay",
                    "confidence": 1.0,
                },
                {
                    "fact_id": "F-after",
                    "proposition_id": "P-adapted",
                    "subject_id": "entity-1",
                    "predicate_id": "rule_awareness",
                    "value": {"kind": "text", "data": "suspected"},
                    "time_scope": "main@2",
                    "visibility": "visible",
                    "provenance": "storyboard",
                    "confidence": 1.0,
                },
            ],
            "evidence": [
                {
                    "evidence_id": "EV-1",
                    "anchor": {"type": "event", "id": "E-1"},
                    "observable_claim": "The observable result conflicts with the old assumption.",
                    "perceivable_by": ["character-1", "audience"],
                    "supports_proposition_ids": ["P-adapted"],
                    "planned_salience": 0.8,
                    "planned_duration_s": 1.5,
                }
            ],
            "dramatic_questions": [
                {
                    "dramatic_question_id": "DQ-1",
                    "question_text": "What caused the unexpected result?",
                    "target_proposition_ids": ["P-adapted"],
                    "open_anchor": {"type": "event", "id": "E-1"},
                    "intended_resolution_scope_id": "episode-generic",
                    "desired_state_while_open": "suspected",
                    "status": "open",
                }
            ],
            "atomic_actions": [
                {
                    "action_id": "A-1",
                    "actor_ids": ["character-1"],
                    "target_ids": ["entity-1"],
                        "participant_deliveries": [],
                    "semantic_intent": "Test the dormant rule.",
                    "precondition_fact_ids": ["F-before"],
                    "effects_add": ["F-after"],
                    "effects_remove": ["F-before"],
                    "completion_condition": "The unexpected result is observable.",
                    "temporal_phases": [
                        {
                            "phase_id": "A-1/P1",
                            "start_condition": "The test begins.",
                            "end_condition": "The result appears.",
                            "estimated_min_s": 1.0,
                        }
                    ],
                }
            ],
            "events": [
                {
                    "event_id": "E-1",
                    "proposition_ids": ["P-adapted"],
                    "precondition_fact_ids": ["F-before"],
                    "action_ids": ["A-1"],
                    "effects_add": ["F-after"],
                    "effects_remove": ["F-before"],
                    "downstream_dependency_event_ids": ["E-2"],
                    "delivery_scope_id": "episode-generic",
                },
                {
                    "event_id": "E-2",
                    "proposition_ids": ["P-adapted"],
                    "causal_parent_ids": ["E-1"],
                    "precondition_fact_ids": ["F-after"],
                    "must_keep": True,
                    "delivery_scope_id": "episode-generic",
                },
            ],
            "character_states": [
                {
                    "character_state_id": "CDS-1",
                    "character_id": "character-1",
                    "anchor": {"type": "event", "id": "E-1"},
                    "goal_proposition_ids": ["P-adapted"],
                    "stakes_proposition_ids": ["P-adapted"],
                    "relationship_state": {"entity-1": "cautious"},
                    "emotion": {"label": "alert", "intensity": 0.6},
                    "pressure": 0.7,
                    "tactic": "verify",
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
                            "proposition_id": "P-adapted",
                            "stance": "suspected",
                            "confidence": 0.65,
                            "evidence_ids": ["EV-1"],
                        }
                    ],
                    "decision_proposition_ids": ["P-adapted"],
                    "decision_basis_ids": ["EV-1"],
                }
            ],
            "audience_priors": [
                {
                    "audience_prior_id": "AP-new",
                    "scope_id": "episode-generic",
                    "audience_description": "A first-time viewer with no rule knowledge.",
                    "assumed_unknown_proposition_ids": ["P-adapted"],
                    "calibration_source": "needs_review",
                }
            ],
            "audience_states": [
                {
                    "audience_state_id": "AS-in",
                    "audience_prior_id": "AP-new",
                    "anchor": {"type": "event", "id": "E-1"},
                    "beliefs": [
                        {
                            "proposition_id": "P-adapted",
                            "stance": "unknown",
                            "confidence": 0.0,
                        }
                    ],
                },
                {
                    "audience_state_id": "AS-out",
                    "audience_prior_id": "AP-new",
                    "anchor": {"type": "event", "id": "E-2"},
                    "beliefs": [
                        {
                            "proposition_id": "P-adapted",
                            "stance": "suspected",
                            "confidence": 0.65,
                            "evidence_ids": ["EV-1"],
                        }
                    ],
                },
            ],
            "experience_intents": [
                {
                    "experience_intent_id": "XI-1",
                    "scope_id": "episode-generic",
                    "anchor_event_ids": ["E-1", "E-2"],
                    "director_objective": "Move a new viewer from ignorance to a grounded suspicion.",
                    "attention_target_ids": ["entity-1"],
                    "audience_paths": [
                        {
                            "audience_path_id": "XP-1",
                            "audience_prior_id": "AP-new",
                            "audience_state_in_id": "AS-in",
                            "audience_state_out_target_id": "AS-out",
                            "target_deltas": [
                                {
                                    "target_delta_id": "XD-1",
                                    "dimension": "belief",
                                    "proposition_ids": ["P-adapted"],
                                    "description": "Unknown becomes suspected.",
                                    "from_state": {"stance": "unknown"},
                                    "to_state": {"stance": "suspected"},
                                    "target_confidence": 0.65,
                                    "required_processing_s": 1.2,
                                    "deadline_event_id": "E-2",
                                    "primary_delivery_window_id": "RW-1",
                                }
                            ],
                        }
                    ],
                    "withheld_propositions": [
                        {
                            "proposition_id": "P-adapted",
                            "reason": "Confirmation belongs to a later scope.",
                            "future_disclosure_anchor": {"type": "event", "id": "E-2"},
                            "carried_question_id": "DQ-1",
                        }
                    ],
                }
            ],
            "assimilation_tasks": [
                {
                    "assimilation_task_id": "AT-1",
                    "experience_intent_id": "XI-1",
                    "audience_path_id": "XP-1",
                    "target_delta_id": "XD-1",
                    "required_prior_proposition_ids": [],
                    "downstream_dependency_event_ids": ["E-2"],
                    "satisfaction_criteria": "A blind viewer can state a grounded suspicion.",
                    "status": "planned",
                }
            ],
            "readability_windows": [
                {
                    "readability_window_id": "RW-1",
                    "event_ids": ["E-1"],
                    "proposition_ids": ["P-adapted"],
                    "target_delta_ids": ["XD-1"],
                    "shot_ids": ["SH-1"],
                    "attention_target_ids": ["entity-1"],
                    "evidence_ids": ["EV-1"],
                    "scheduled_processing_s": 1.5,
                    "planned_available_s": 1.3,
                    "readability_reason": "The result needs an uncontested registration beat.",
                }
            ],
            "setup_payoff_contracts": [
                {
                    "setup_payoff_id": "SP-1",
                    "setup_proposition_ids": ["P-adapted"],
                    "setup_event_ids": ["E-1"],
                    "payoff_event_ids": ["E-2"],
                    "intended_inference_ids": ["P-adapted"],
                    "retention_deadline_event_id": "E-2",
                    "minimum_retention_confidence": 0.5,
                    "recall_needed": False,
                    "status": "preserved",
                }
            ],
            "scene_contracts": [
                {
                    "scene_id": "SC-1",
                    "scene_question_id": "DQ-1",
                    "point_of_view_character_id": "character-1",
                    "audience_state_paths": [
                        {
                            "audience_prior_id": "AP-new",
                            "audience_state_in_id": "AS-in",
                            "audience_state_out_target_id": "AS-out",
                        }
                    ],
                    "character_state_in_ids": ["CDS-1"],
                    "goal_proposition_ids": ["P-adapted"],
                    "obstacle_proposition_ids": ["P-adapted"],
                    "stakes_proposition_ids": ["P-adapted"],
                    "pressure_curve": [
                        {"anchor": {"type": "event", "id": "E-1"}, "value": 0.7}
                    ],
                    "turn_event_ids": ["E-1"],
                    "value_polarity_in": "certainty",
                    "value_polarity_out": "doubt",
                    "character_state_out_ids": ["CDS-1"],
                    "scene_button": "The result opens a causal question.",
                }
            ],
            "arc_contracts": [
                {
                    "arc_id": "ARC-1",
                    "scope": "episode",
                    "core_question_ids": ["DQ-1"],
                    "promise_proposition_ids": ["P-adapted"],
                    "escalation_event_ids": ["E-1"],
                    "climax_event_ids": ["E-2"],
                    "payoff_contract_ids": ["SP-1"],
                    "pressure_curve": [
                        {"anchor": {"type": "event", "id": "E-1"}, "value": 0.7}
                    ],
                    "information_density_curve": [
                        {"anchor": {"type": "event", "id": "E-1"}, "value": 0.5}
                    ],
                    "processing_beats": [
                        {"anchor": {"type": "event", "id": "E-1"}, "purpose": "register"}
                    ],
                    "carried_question_ids": ["DQ-1"],
                }
            ],
        }
    )


def _shot(**updates) -> Shot:
    values = {
        "shot_no": 2,
        "duration_s": 5,
        "shot_size": "中景",
        "camera_move": "固定",
        "scene_setting": "日，中性场所",
        "characters": ["character-1"],
        "action_desc": "character-1 observes an unexpected result and pauses to process it.",
        "first_frame_desc": "The test is still in progress before the result is visible.",
        "last_frame_desc": "The result is visible while character-1 registers its meaning.",
        "source_excerpt": "A source-grounded result becomes visible to the character.",
    }
    values.update(updates)
    return Shot(**values)


def _contribution() -> ShotContribution:
    return ShotContribution(
        shot_contribution_id="SCN-1",
        experience_intent_ids=["XI-1"],
        target_delta_ids=["XD-1"],
        assimilation_task_ids=["AT-1"],
        evidence_ids=["EV-1"],
        story_delta_fact_ids=["F-after"],
        character_state_delta_ids=["CDS-1", "CB-1"],
        audience_state_delta_ids=["AS-out"],
        affective_delta={"tension": 0.2},
        spatial_temporal_delta={"orientation_confidence": 0.1},
        dramatic_pressure_delta=0.3,
    )


def _boundary() -> NarrativeBoundaryContract:
    return NarrativeBoundaryContract(
        boundary_id="B-SH-1-SH-2",
        previous_shot_id="SH-1",
        next_shot_id="SH-2",
        narrative_relation="result becomes interpretation",
        required_state_invariants=["F-after"],
        allowed_state_deltas=["F-after"],
        forbidden_replay_action_ids=["A-1"],
        handoff_action_phase_id="A-1/P1",
        spatial_orientation_contract={"maintain_location": True},
        temporal_orientation_contract={"timeline_id": "main"},
        audience_state_handoffs=[
            {
                "audience_prior_id": "AP-new",
                "previous_state_out_id": "AS-in",
                "next_state_in_id": "AS-in",
            }
        ],
        affective_handoff={"tension": "hold"},
        cut_motivation="Shift attention from result to comprehension.",
    )


def _narrative_shot_fields() -> dict:
    return {
        "shot_id": "SH-2",
        "scene_id": "SC-1",
        "event_ids": ["E-1"],
        "primary_action_id": None,
        "supporting_action_ids": ["A-support"],
        "action_participant_deliveries": [
            ActionParticipantDelivery(
                action_id="A-support",
                participant_id="entity-offscreen",
                evidence_ids=["EV-1"],
                visible_effect=True,
            )
        ],
        "shot_contribution": _contribution(),
        "audience_state_paths": [
            AudienceStatePathRef(
                audience_prior_id="AP-new",
                audience_state_in_id="AS-in",
                audience_state_out_target_id="AS-out",
            )
        ],
        "planned_state_in_fact_ids": ["F-before"],
        "planned_delta_add_fact_ids": ["F-after"],
        "planned_delta_remove_fact_ids": ["F-before"],
        "planned_state_out_fact_ids": ["F-after"],
        "completed_before_action_ids": ["A-prior"],
        "reserved_future_event_ids": ["E-2"],
        "readability_window_ids": ["RW-1"],
        "narrative_boundary_from_previous": _boundary(),
    }


def test_screenplay_document_roundtrip_preserves_authoritative_narrative_plan() -> None:
    plan = _narrative_plan()
    screenplay = EpisodeScreenplay(episode_no=1, title="Generic", narrative_plan=plan)

    document = screenplay_to_document(screenplay)

    assert document.narrative_plan == plan
    assert document.narrative_plan is not screenplay.narrative_plan
    assert document.narrative_plan.propositions[0] is not screenplay.narrative_plan.propositions[0]
    rederived = rederive_projections(document)
    assert rederived.narrative_plan == plan

    restored = document_to_screenplay(document)

    assert restored.narrative_plan == plan
    assert restored.narrative_plan is not document.narrative_plan
    assert restored.narrative_plan.model_dump(mode="json") == plan.model_dump(mode="json")


def test_legacy_screenplay_roundtrip_keeps_narrative_plan_optional() -> None:
    screenplay = EpisodeScreenplay(episode_no=7, title="Legacy")

    restored = document_to_screenplay(screenplay_to_document(screenplay))

    assert restored.episode_no == 7
    assert restored.narrative_plan is None


def test_shot_contract_roundtrip_preserves_every_narrative_field() -> None:
    source = _shot(**_narrative_shot_fields())
    payload = shot_contract_dict(source)
    restored = apply_shot_contract(_shot(), json.dumps(payload, ensure_ascii=False))

    narrative_fields = tuple(_narrative_shot_fields())
    for field in narrative_fields:
        actual = getattr(restored, field)
        expected = getattr(source, field)
        if hasattr(actual, "model_dump"):
            assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        elif isinstance(actual, list) and actual and hasattr(actual[0], "model_dump"):
            assert [item.model_dump(mode="json") for item in actual] == [
                item.model_dump(mode="json") for item in expected
            ]
        else:
            assert actual == expected

    assert payload["primary_action_id"] is None
    assert restored.primary_action_id is None


def test_legacy_shot_contract_without_narrative_fields_keeps_safe_defaults() -> None:
    restored = apply_shot_contract(
        _shot(),
        {"story_event_id": "E-legacy", "purpose": "legacy projection"},
    )

    assert restored.story_event_id == "E-legacy"
    assert restored.shot_id == ""
    assert restored.event_ids == []
    assert restored.primary_action_id is None
    assert restored.shot_contribution is None
    assert restored.audience_state_paths == []
    assert restored.narrative_boundary_from_previous is None


def test_storyboard_outline_json_roundtrip_preserves_narrative_shot_fields() -> None:
    outline_shot = StoryboardOutlineShot(
        shot_no=2,
        beat="Give the audience an uncontested comprehension beat.",
        **_narrative_shot_fields(),
    )
    outline = StoryboardOutline(episode_no=1, shots=[outline_shot])

    restored = StoryboardOutline.model_validate_json(outline.model_dump_json())

    assert restored == outline
    assert restored.shots[0].shot_contribution == _contribution()
    assert restored.shots[0].narrative_boundary_from_previous == _boundary()
