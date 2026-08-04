"""Layered regression tests for generalized narrative continuity hard gates.

These tests deliberately use fictional IDs and relation mutations.  No verdict
depends on a character name, story genre, action verb, or project whitelist.
"""

from __future__ import annotations

import json

import pytest

from app.narrative import (
    validate_blind_review,
    validate_screenplay_narrative,
    validate_storyboard_narrative,
)
from app.portraits import (
    apply_screenplay_character_resolutions,
    screenplay_character_resolution_errors,
)
from app.schemas import (
    ActionSemanticRelationAudit,
    AssimilationTask,
    AtomicAction,
    AtomicActionPhase,
    AudienceStatePathRef,
    AudienceStateSnapshot,
    CharacterDramaticState,
    NarrativeAnchor,
    NarrativeEvent,
    NarrativeEvidence,
    NarrativeReviewReport,
    SetupPayoffContract,
    ShotCapacityBudget,
    ShotContribution,
    Storyboard,
)
from test_narrative_continuity import (
    _blind_observations,
    _boundary,
    _codes,
    _paths,
    _review_dimension_results,
    _screenplay,
    _settled_followup_shot,
    _shot,
)


def _two_phase_action_story() -> tuple[object, Storyboard]:
    """A valid two-shot action whose event/effect lands only at phase two."""
    screenplay = _screenplay()
    plan = screenplay.narrative_plan
    plan.atomic_actions.append(
        AtomicAction(
            action_id="ACT-RELATION",
            actor_ids=["character-1"],
            target_ids=["entity-1"],
            semantic_intent="Change the target through one ordered execution.",
            precondition_fact_ids=["F-before"],
            effects_add=["F-after"],
            effects_remove=["F-before"],
            completion_condition="The target visibly holds the resulting state.",
            decision_requirement="not_applicable",
            decision_not_applicable_reason=(
                "The action is the direct mechanical continuation of the event trigger."
            ),
            temporal_phases=[
                AtomicActionPhase(
                    phase_id="PHASE-START",
                    start_condition="The declared precondition is visible.",
                    end_condition="The transformation has observably begun.",
                    estimated_min_s=1.0,
                ),
                AtomicActionPhase(
                    phase_id="PHASE-FINISH",
                    start_condition="The first phase has observably completed.",
                    end_condition="The declared result is visible.",
                    estimated_min_s=1.0,
                ),
            ],
            splittable_boundaries=["PHASE-START"],
        )
    )
    plan.events[0].action_ids = ["ACT-RELATION"]
    plan.events.append(
        NarrativeEvent(
            event_id="E-ACTION-DEADLINE",
            proposition_ids=["P-story"],
            causal_parent_ids=["E-1"],
            must_keep=True,
            delivery_scope_id="episode-generic",
            delivery_policy="carry",
        )
    )
    for path in plan.experience_intents[0].audience_paths:
        for delta in path.target_deltas:
            delta.deadline_event_id = "E-ACTION-DEADLINE"
    plan.readability_windows[0].shot_ids = ["SH-PHASE-2"]

    first = _shot(
        shot_id="SH-PHASE-1",
        primary_action_id="ACT-RELATION",
        action_phase_ids=["PHASE-START"],
        visible_entity_ids=["character-1", "entity-1"],
        shot_contribution=ShotContribution(
            shot_contribution_id="SCN-PHASE-1",
            evidence_ids=["EV-1"],
        ),
        audience_state_paths=[
            AudienceStatePathRef(
                audience_prior_id="AP-cold",
                audience_state_in_id="AS-cold-in",
                audience_state_out_target_id="AS-cold-in",
            ),
            AudienceStatePathRef(
                audience_prior_id="AP-context",
                audience_state_in_id="AS-context-in",
                audience_state_out_target_id="AS-context-in",
            ),
        ],
        planned_delta_add_fact_ids=[],
        planned_delta_remove_fact_ids=[],
        planned_state_out_fact_ids=["F-before"],
        readability_window_ids=[],
        capacity_budget=ShotCapacityBudget(action_phase_s=1.0),
    )
    boundary = _boundary(first.shot_id, "SH-PHASE-2")
    boundary.audience_state_handoffs = [
        {
            "audience_prior_id": "AP-cold",
            "previous_state_out_id": "AS-cold-in",
            "next_state_in_id": "AS-cold-in",
        },
        {
            "audience_prior_id": "AP-context",
            "previous_state_out_id": "AS-context-in",
            "next_state_in_id": "AS-context-in",
        },
    ]
    boundary.handoff_action_phase_id = "PHASE-FINISH"
    second = _shot(
        shot_no=2,
        shot_id="SH-PHASE-2",
        primary_action_id=None,
        supporting_action_ids=["ACT-RELATION"],
        action_phase_ids=["PHASE-FINISH"],
        visible_entity_ids=["character-1", "entity-1"],
        completed_before_action_phase_ids=["PHASE-START"],
        narrative_boundary_from_previous=boundary,
        capacity_budget=ShotCapacityBudget(
            action_phase_s=1.0,
            inference_processing_s=2.0,
        ),
    )
    return screenplay, Storyboard(episode_no=1, shots=[first, second])


def test_ordered_multishot_action_has_exact_phase_handoff_and_ledgers() -> None:
    screenplay, board = _two_phase_action_story()

    assert validate_screenplay_narrative(screenplay, require=True) == []
    assert validate_storyboard_narrative(board, screenplay) == []


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("phase_order", "ACTION_PHASE_DELIVERY_MISMATCH"),
        ("phase_duplicate", "COMPLETED_ACTION_PHASE_REPLAY"),
        ("handoff", "BOUNDARY_ACTION_PHASE_HANDOFF_MISMATCH"),
        ("phase_ledger", "COMPLETED_PHASE_LEDGER_MISMATCH"),
    ],
)
def test_multishot_action_rejects_order_duplicate_handoff_and_ledger_drift(
    mutation: str,
    expected_code: str,
) -> None:
    screenplay, board = _two_phase_action_story()
    first, second = board.shots

    if mutation == "phase_order":
        first.action_phase_ids = ["PHASE-FINISH"]
        second.action_phase_ids = ["PHASE-START"]
        second.completed_before_action_phase_ids = ["PHASE-FINISH"]
        second.narrative_boundary_from_previous.handoff_action_phase_id = "PHASE-START"
    elif mutation == "phase_duplicate":
        second.action_phase_ids = ["PHASE-START"]
        second.narrative_boundary_from_previous.handoff_action_phase_id = "PHASE-START"
    elif mutation == "handoff":
        second.narrative_boundary_from_previous.handoff_action_phase_id = None
    elif mutation == "phase_ledger":
        second.completed_before_action_phase_ids = []

    assert expected_code in _codes(validate_storyboard_narrative(board, screenplay))


def test_completed_action_cannot_be_bound_again_after_its_final_phase() -> None:
    screenplay, board = _two_phase_action_story()
    replay = _shot(
        shot_no=3,
        shot_id="SH-REPLAY",
        event_ids=["E-1"],
        primary_action_id=None,
        supporting_action_ids=["ACT-RELATION"],
        action_phase_ids=["PHASE-FINISH"],
        visible_entity_ids=["character-1", "entity-1"],
        completed_before_action_ids=["ACT-RELATION"],
        completed_before_action_phase_ids=["PHASE-START", "PHASE-FINISH"],
        shot_contribution=ShotContribution(
            shot_contribution_id="SCN-REPLAY",
            evidence_ids=["EV-1"],
        ),
        audience_state_paths=_paths(settled=True),
        planned_state_in_fact_ids=["F-after"],
        planned_delta_add_fact_ids=[],
        planned_delta_remove_fact_ids=[],
        planned_state_out_fact_ids=["F-after"],
        readability_window_ids=[],
        capacity_budget=ShotCapacityBudget(action_phase_s=1.0),
        narrative_boundary_from_previous=_boundary("SH-PHASE-2", "SH-REPLAY"),
    )
    replay.narrative_boundary_from_previous.forbidden_replay_action_ids = [
        "ACT-RELATION"
    ]
    replay.narrative_boundary_from_previous.handoff_action_phase_id = "PHASE-FINISH"
    board.shots.append(replay)

    codes = _codes(validate_storyboard_narrative(board, screenplay))

    assert "COMPLETED_ACTION_REPLAY" in codes
    assert "COMPLETED_ACTION_PHASE_REPLAY" in codes


def test_joint_shot_budget_rejects_action_inference_and_speech_overbooking() -> None:
    screenplay, board = _two_phase_action_story()
    shot = board.shots[1]
    shot.narration = "abc"
    shot.capacity_budget = ShotCapacityBudget(
        action_phase_s=1.0,
        inference_processing_s=2.0,
        spoken_and_text_s=3.0,
    )

    codes = _codes(validate_storyboard_narrative(board, screenplay))

    assert "SHOT_JOINT_CAPACITY_EXCEEDED" in codes
    assert "SHOT_ACTION_CAPACITY_EXCEEDED" not in codes
    assert "SHOT_INFERENCE_CAPACITY_EXCEEDED" not in codes
    assert "SHOT_SPOKEN_TEXT_CAPACITY_EXCEEDED" not in codes


def _equivalent_action_story() -> object:
    screenplay = _screenplay()
    plan = screenplay.narrative_plan
    common = {
        "actor_ids": ["character-1"],
        "target_ids": ["entity-1"],
        "semantic_intent": "Create the same observable relation.",
        "completion_condition": "The same observable relation is established.",
        "decision_requirement": "not_applicable",
        "decision_not_applicable_reason": "Both executions are externally triggered.",
    }
    plan.atomic_actions.extend(
        [
            AtomicAction(action_id="ACT-BASE", **common),
            AtomicAction(action_id="ACT-REPEAT", **common),
        ]
    )
    plan.events[0].action_ids = ["ACT-BASE"]
    plan.events.append(
        NarrativeEvent(
            event_id="E-2",
            proposition_ids=["P-story"],
            causal_parent_ids=["E-1"],
            action_ids=["ACT-REPEAT"],
            must_keep=True,
            delivery_scope_id="episode-generic",
            delivery_policy="carry",
        )
    )
    return screenplay


def test_structurally_equivalent_action_ids_require_open_semantic_audit() -> None:
    screenplay = _equivalent_action_story()

    assert "ACTION_SEMANTIC_AUDIT_MISSING" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )


def test_functional_repeat_requires_real_delta_and_causal_dependency() -> None:
    screenplay = _equivalent_action_story()
    plan = screenplay.narrative_plan
    plan.action_relation_audits.append(
        ActionSemanticRelationAudit(
            action_relation_audit_id="AUDIT-1",
            action_ids=["ACT-BASE", "ACT-REPEAT"],
            semantically_equivalent=True,
            functional_repeat=True,
            causal_basis_event_ids=["E-1", "E-2"],
            decision="pass",
            reason="Compare the two open semantic action contracts.",
        )
    )

    assert "ACTION_FUNCTIONAL_REPEAT_DELTA_MISSING" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )

    plan.evidence.append(
        NarrativeEvidence(
            evidence_id="EV-REPEAT-DELTA",
            anchor=NarrativeAnchor(type="event", id="E-2"),
            observable_claim="The repeated execution produces a newly perceivable result.",
            perceivable_by=["character-1", "audience"],
            supports_proposition_ids=["P-story"],
            planned_salience=0.8,
        )
    )
    plan.action_relation_audits[0].added_evidence_ids = ["EV-REPEAT-DELTA"]
    passing_codes = _codes(validate_screenplay_narrative(screenplay, require=True))
    assert "ACTION_FUNCTIONAL_REPEAT_DELTA_MISSING" not in passing_codes
    assert "ACTION_FUNCTIONAL_REPEAT_CAUSAL_GAP" not in passing_codes

    plan.events[1].causal_parent_ids = []
    assert "ACTION_FUNCTIONAL_REPEAT_CAUSAL_GAP" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )


@pytest.mark.parametrize(
    ("claim", "expected_code"),
    [
        ("affective", "SHOT_AFFECTIVE_DELTA_UNGROUNDED"),
        ("spatial", "SHOT_SPATIOTEMPORAL_DELTA_UNGROUNDED"),
        ("pressure", "SHOT_PRESSURE_DELTA_UNGROUNDED"),
    ],
)
def test_open_filler_claims_must_match_authoritative_state_changes(
    claim: str,
    expected_code: str,
) -> None:
    screenplay = _screenplay()
    followup = _settled_followup_shot()
    if claim == "affective":
        followup.shot_contribution.affective_delta = {
            "registration": "a value absent from the authoritative state"
        }
    elif claim == "spatial":
        followup.shot_contribution.spatial_temporal_delta = {
            "spatial_model": {"zone": "an uncontracted location"}
        }
        followup.capacity_budget.spatial_reorientation_s = 0.5
    elif claim == "pressure":
        followup.shot_contribution.dramatic_pressure_delta = 0.4

    board = Storyboard(episode_no=1, shots=[_shot(), followup])

    assert expected_code in _codes(validate_storyboard_narrative(board, screenplay))


def test_source_span_scope_and_entity_references_are_exact_and_authoritative() -> None:
    screenplay = _screenplay()
    source_text = "prefix|An observable change occurs.|suffix"
    excerpt = screenplay.narrative_plan.source_evidence[0]
    excerpt.source_span.start = source_text.index(excerpt.verbatim_excerpt)
    excerpt.source_span.end = excerpt.source_span.start + len(excerpt.verbatim_excerpt)

    assert validate_screenplay_narrative(
        screenplay,
        require=True,
        source_text=source_text,
        expected_scope_id="episode-generic",
    ) == []

    excerpt.source_span.start += 1
    excerpt.source_span.end += 1
    assert "SOURCE_SPAN_EXACT_MISMATCH" in _codes(
        validate_screenplay_narrative(
            screenplay,
            require=True,
            source_text=source_text,
            expected_scope_id="another-scope",
        )
    )
    codes = _codes(
        validate_screenplay_narrative(
            screenplay,
            require=True,
            source_text=source_text,
            expected_scope_id="another-scope",
        )
    )
    assert "NARRATIVE_SCOPE_MISMATCH" in codes

    screenplay.narrative_plan.evidence[0].perceivable_by.append("undeclared-entity")
    assert "NARRATIVE_ENTITY_UNDECLARED" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )


def _setup_payoff_story(
    retention_confidence: float,
    *,
    recall_needed: bool,
) -> object:
    screenplay = _screenplay()
    plan = screenplay.narrative_plan
    plan.events.append(
        NarrativeEvent(
            event_id="E-PAYOFF",
            proposition_ids=["P-story"],
            causal_parent_ids=["E-1"],
            must_keep=True,
            delivery_scope_id="episode-generic",
            delivery_policy="carry",
        )
    )
    plan.setup_payoff_contracts.append(
        SetupPayoffContract(
            setup_payoff_id="SP-1",
            setup_proposition_ids=["P-story"],
            setup_event_ids=["E-1"],
            payoff_event_ids=["E-PAYOFF"],
            intended_inference_ids=["P-story"],
            retention_deadline_event_id="E-PAYOFF",
            minimum_retention_confidence=0.6,
            recall_needed=recall_needed,
            status="paid_off",
        )
    )
    for prior_id in ("AP-cold", "AP-context"):
        plan.audience_states.append(
            AudienceStateSnapshot(
                audience_state_id=f"AS-{prior_id}-payoff",
                audience_prior_id=prior_id,
                anchor=NarrativeAnchor(type="event", id="E-PAYOFF"),
                working_memory=[
                    {
                        "proposition_id": "P-story",
                        "retention_confidence": retention_confidence,
                    }
                ],
            )
        )
    return screenplay


def _add_recall_tasks(screenplay: object) -> None:
    plan = screenplay.narrative_plan
    for prior_suffix, path_id, delta_id in (
        ("cold", "XP-cold", "XD-cold"),
        ("context", "XP-context", "XD-context"),
    ):
        plan.assimilation_tasks.append(
            AssimilationTask(
                assimilation_task_id=f"AT-recall-{prior_suffix}",
                experience_intent_id="XI-1",
                audience_path_id=path_id,
                target_delta_id=delta_id,
                required_prior_proposition_ids=["P-story"],
                downstream_dependency_event_ids=["E-PAYOFF"],
                satisfaction_criteria=(
                    "A blind viewer recalls the setup before interpreting the payoff."
                ),
                status="planned",
            )
        )


def test_low_setup_memory_requires_a_per_prior_recall_task() -> None:
    screenplay = _setup_payoff_story(0.2, recall_needed=True)

    assert "SETUP_RECALL_TASK_MISSING" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )

    _add_recall_tasks(screenplay)
    assert "SETUP_RECALL_TASK_MISSING" not in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )


def test_sufficient_setup_memory_forbids_an_unnecessary_recall_decision() -> None:
    screenplay = _setup_payoff_story(0.9, recall_needed=False)

    assert validate_screenplay_narrative(screenplay, require=True) == []

    screenplay.narrative_plan.setup_payoff_contracts[0].recall_needed = True
    assert "SETUP_RECALL_DECISION_MISMATCH" in _codes(
        validate_screenplay_narrative(screenplay, require=True)
    )


def _passing_blind_report() -> NarrativeReviewReport:
    return NarrativeReviewReport.model_validate(
        {
            "narrative_review_report_id": "NRR-hard-gate",
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
                    "reason": "The frozen cold recall contains the evidence.",
                },
                {
                    "audience_prior_id": "AP-context",
                    "target_delta_id": "XD-context",
                    "result": "satisfied",
                    "supporting_observation_ids": ["BAO-context"],
                    "supporting_evidence_ids": ["EV-1"],
                    "reason": "The frozen contextual recall contains the evidence.",
                },
            ],
            **_review_dimension_results(),
            "decision": "pass",
        }
    )


def test_followup_evidence_cannot_retroactively_satisfy_spontaneous_recall() -> None:
    screenplay = _screenplay()
    observations = _blind_observations()
    report = _passing_blind_report()
    assert validate_blind_review(screenplay, observations, report) == []

    observations[0].spontaneous_supporting_evidence_ids = []
    observations[0].neutral_followup_observations = [
        {"supporting_evidence_ids": ["EV-1"], "answer": "Recovered after prompting."}
    ]
    assert observations[0].supporting_evidence_ids == ["EV-1"]

    assert "REVIEW_EVIDENCE_NOT_SPONTANEOUS" in _codes(
        validate_blind_review(screenplay, observations, report)
    )


def test_character_resolution_atomically_rewrites_the_narrative_graph() -> None:
    screenplay = _screenplay()
    plan = screenplay.narrative_plan
    source_id = "character-1"
    canonical_id = "resolved-character"
    plan.atomic_actions.append(
        AtomicAction(
            action_id="ACT-IDENTITY",
            actor_ids=[source_id],
            target_ids=["entity-1"],
            semantic_intent=f"{source_id} performs the resolved relation.",
            completion_condition=f"{source_id} is visible in the result.",
            decision_requirement="not_applicable",
            decision_not_applicable_reason="Identity resolution does not alter causality.",
        )
    )
    plan.character_states.append(
        CharacterDramaticState(
            character_state_id="CDS-IDENTITY",
            character_id=source_id,
            anchor=NarrativeAnchor(type="event", id="E-1"),
            relationship_state={"observer": source_id},
            emotion={"focus": source_id},
            tactic=f"Keep {source_id} observable.",
        )
    )
    plan.evidence[0].competing_attention_ids = [source_id]
    for state in plan.audience_states[:2]:
        state.character_goal_hypotheses = {source_id: "observe"}
        state.attention_residue_ids = [source_id]
    plan.experience_intents[0].attention_target_ids = [source_id]
    plan.scene_contracts[0].point_of_view_character_id = source_id
    plan.scene_contracts[0].relationship_deltas = [{"observer": source_id}]
    screenplay.full_script_text = f"{source_id}: Proceed.\n{source_id} crosses the frame."
    source_evidence_before = [
        item.model_dump(mode="json") for item in plan.source_evidence
    ]
    resolutions = [
        {
            "source_label": source_id,
            "canonical_name": canonical_id,
            "resolution": "future_identity",
        }
    ]
    assert screenplay_character_resolution_errors(screenplay, resolutions)

    changes = apply_screenplay_character_resolutions(screenplay, resolutions)

    assert changes == [
        {
            "source_label": source_id,
            "canonical_name": canonical_id,
            "resolution": "future_identity",
        }
    ]
    assert screenplay_character_resolution_errors(screenplay, resolutions) == []
    assert source_evidence_before == [
        item.model_dump(mode="json") for item in plan.source_evidence
    ]
    assert source_id not in json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)
    assert canonical_id in json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)
    assert validate_screenplay_narrative(screenplay, require=True) == []
