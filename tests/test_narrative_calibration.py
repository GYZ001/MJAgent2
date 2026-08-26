from __future__ import annotations

import json
from pathlib import Path
import threading

from pydantic import ValidationError
import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.narrative_calibration import (
    CalibrationContractError,
    HumanOneWatchFreeze,
    HumanOneWatchObservation,
    HumanTargetDeltaObservation,
    ModelTargetEstimate,
    assert_report_meets_current_calibration,
    build_calibration_report,
    persist_ai_one_watch_simulation_authority,
    persist_calibration_report,
    persist_human_one_watch_freeze,
    persist_human_one_watch_observation,
    require_current_calibration_authority,
    validate_human_one_watch_observation,
)
from app.schemas import EpisodeScreenplay, NARRATIVE_CONTRACT_VERSION, NarrativeReviewReport


def _narrative_plan_schema_example(scope_id: str) -> str:
    """Minimal but structurally complete narrative_plan fixture for calibration tests.

    Relocated from app.stages._narrative_plan_schema_example (deleted: that
    function had zero production callers -- it was a leftover prompt-schema
    example from the retired narrative_plan generation pipeline, see
    docs history for storyboard 2.0.0 / prep_pack 2.0.0). This test module
    still needs a realistic narrative_plan payload to exercise
    app.narrative_calibration, which is unrelated to the deleted pipeline, so
    the fixture moved here instead of being deleted with its old caller.
    """
    example_chapter_id = "current-source-chapter"
    example = {
        "contract_version": NARRATIVE_CONTRACT_VERSION,
        "scope_id": scope_id,
        "source_evidence": [{
            "source_evidence_id": "SE-1",
            "source_span": {
                "chapter_id": example_chapter_id,
                "start": 0,
                "end": 12,
            },
            "verbatim_excerpt": "从本集授权原文逐字摘录",
            "confidence": 1.0,
        }],
        "identity_contracts": [
            {
                "identity_id": "character-id",
                "display_name": "当前来源与戏剧职责定义的显示名",
                "kind": "由来源证据与本集语义推导的开放身份性质",
                "visual_policy": "contextual",
                "visual_canonical": "足以跨镜识别当前身份的中性视觉锚点",
                "asset_requirement": "optional",
                "voice_ids": ["voice-id"],
                "evidence": {
                    "source_evidence_ids": ["SE-1"],
                    "proposition_ids": ["P-SOURCE-1", "P-ADAPTED-1"],
                    "adaptation_decision_ids": ["AD-1"],
                    "rationale": "为什么该身份及其视觉、资产、声音策略对本集是必要且充分的",
                },
            },
            {
                "identity_id": "entity-id",
                "display_name": "当前叙事中被动作或状态引用的实体名",
                "kind": "由当前命题与作用关系推导的开放实体性质",
                "visual_policy": "contextual",
                "visual_canonical": "足以识别当前实体与其状态变化的视觉锚点",
                "asset_requirement": "optional",
                "voice_ids": [],
                "evidence": {
                    "source_evidence_ids": ["SE-1"],
                    "proposition_ids": ["P-SOURCE-1", "P-ADAPTED-1"],
                    "adaptation_decision_ids": ["AD-1"],
                    "rationale": "该实体被本集命题、事实或动作实际引用的证据理由",
                },
            },
        ],
        "propositions": [
            {
                "proposition_id": "P-SOURCE-1",
                "semantic_identity_key": "当前项目内该原文命题的语义身份键",
                "canonical_statement": "不可再拆的原文命题",
                "narrative_domain": "source_canon",
                "entity_ids": ["entity-id", "character-id"],
                "direct_source_evidence_ids": ["SE-1"],
                "domain_truth_status": "true",
            },
            {
                "proposition_id": "P-ADAPTED-1",
                "semantic_identity_key": "当前项目内该改编命题的语义身份键",
                "canonical_statement": "改编后的不可再拆命题",
                "narrative_domain": "adapted_story",
                "entity_ids": ["entity-id", "character-id"],
                "direct_source_evidence_ids": [],
                "domain_truth_status": "true",
            },
        ],
        "adaptation_decisions": [{
            "adaptation_decision_id": "AD-1",
            "source_proposition_ids": ["P-SOURCE-1"],
            "adapted_proposition_ids": ["P-ADAPTED-1"],
            "relation": "preserve",
            "custom_relation": None,
            "creative_reason": "本集改编理由",
            "protected_causal_effect_ids": ["P-ADAPTED-1"],
            "affected_event_ids": ["E-1", "E-2"],
            "uncertainty": None,
        }],
        "state_facts": [
            {
                "fact_id": "F-1",
                "proposition_id": "P-ADAPTED-1",
                "subject_id": "entity-id",
                "predicate_id": "project-semantic-predicate-id",
                "value": {"kind": "text", "data": "事件前状态"},
                "time_scope": "main@1",
                "visibility": "visible",
                "provenance": "screenplay",
                "confidence": 1.0,
            },
            {
                "fact_id": "F-2",
                "proposition_id": "P-ADAPTED-1",
                "subject_id": "entity-id",
                "predicate_id": "project-semantic-predicate-id",
                "value": {"kind": "text", "data": "原因事件完成后、结果行动前的状态"},
                "time_scope": "main@2",
                "visibility": "visible",
                "provenance": "screenplay",
                "confidence": 1.0,
            },
            {
                "fact_id": "F-3",
                "proposition_id": "P-ADAPTED-1",
                "subject_id": "entity-id",
                "predicate_id": "project-semantic-predicate-id",
                "value": {"kind": "text", "data": "结果行动完成后的状态"},
                "time_scope": "main@3",
                "visibility": "visible",
                "provenance": "screenplay",
                "confidence": 1.0,
            },
        ],
        "initial_state_fact_ids": ["F-1"],
        "evidence": [
            {
                "evidence_id": "EV-1",
                "anchor": {"type": "event", "id": "E-1"},
                "observable_claim": "执行者与观众在原因事件当下实际可感知的内容",
                "perceivable_by": ["character-id", "audience"],
                "supports_proposition_ids": ["P-ADAPTED-1"],
                "planned_salience": 0.8,
                "planned_duration_s": 1.5,
                "competing_attention_ids": [],
            },
            {
                "evidence_id": "EV-2",
                "anchor": {"type": "event", "id": "E-2"},
                "observable_claim": "观察者可核对结果行动已完成",
                "perceivable_by": ["character-id", "audience"],
                "supports_proposition_ids": ["P-ADAPTED-1"],
                "planned_salience": 0.7,
                "planned_duration_s": 0.5,
                "competing_attention_ids": [],
            },
        ],
        "dramatic_questions": [{
            "dramatic_question_id": "DQ-1",
            "question_text": "观众此时应追问的问题",
            "target_proposition_ids": ["P-ADAPTED-1"],
            "open_anchor": {"type": "event", "id": "E-1"},
            "intended_resolution_scope_id": scope_id,
            "desired_state_while_open": "unknown",
            "resolution_anchor": None,
            "status": "open",
        }],
        "events": [
            {
                "event_id": "E-1",
                "proposition_ids": ["P-ADAPTED-1"],
                "causal_parent_ids": [],
                "precondition_fact_ids": ["F-1"],
                "action_ids": [],
                "effects_add": ["F-2"],
                "effects_remove": ["F-1"],
                "character_goal_effects": [],
                "downstream_dependency_event_ids": ["E-2"],
                "salience": 0.8,
                "irreversibility": 0.5,
                "must_keep": True,
                "delivery_scope_id": scope_id,
                "delivery_policy": "deliver",
                "primary_delivery_window_id": "RW-1",
            },
            {
                "event_id": "E-2",
                "proposition_ids": ["P-ADAPTED-1"],
                "causal_parent_ids": ["E-1"],
                "precondition_fact_ids": ["F-2"],
                "action_ids": ["A-1"],
                "effects_add": ["F-3"],
                "effects_remove": ["F-2"],
                "character_goal_effects": [],
                "downstream_dependency_event_ids": [],
                "salience": 0.8,
                "irreversibility": 0.6,
                "must_keep": True,
                "delivery_scope_id": scope_id,
                "delivery_policy": "deliver",
                "primary_delivery_window_id": "RW-2",
            },
        ],
        "atomic_actions": [{
            "action_id": "A-1",
            "actor_ids": ["character-id"],
            "target_ids": ["entity-id"],
            "participant_deliveries": [],
            "semantic_intent": "该动作在故事中完成什么",
            "precondition_fact_ids": ["F-2"],
            "effects_add": ["F-3"],
            "effects_remove": ["F-2"],
            "completion_condition": "观察者可验证的完成条件",
            "decision_requirement": "applies",
            "decision_not_applicable_reason": None,
            "temporal_phases": [{
                "phase_id": "A-1/P1",
                "start_condition": "开始条件",
                "end_condition": "结束条件",
                "estimated_min_s": 1.0,
            }],
            "splittable_boundaries": ["A-1/P1"],
        }],
        "action_relation_audits": [],
        "character_states": [{
            "character_state_id": "CDS-1",
            "character_id": "character-id",
            "anchor": {"type": "event", "id": "E-1"},
            "goal_proposition_ids": ["P-ADAPTED-1"],
            "stakes_proposition_ids": [],
            "relationship_state": {},
            "emotion": {"label": "自由语义", "intensity": 0.5, "observable_evidence": ["EV-1"]},
            "pressure": 0.5,
            "tactic": "当前手段",
        }],
        "character_beliefs": [{
            "character_belief_id": "CB-1",
            "character_id": "character-id",
            "anchor": {"type": "event", "id": "E-1"},
            "perceived_evidence_ids": ["EV-1"],
            "beliefs": [{
                "proposition_id": "P-ADAPTED-1",
                "stance": "suspected",
                "confidence": 0.6,
                "evidence_ids": ["EV-1"],
            }],
            "misbelief_proposition_ids": [],
            "decision_proposition_ids": ["P-ADAPTED-1"],
            "decision_basis_ids": ["EV-1"],
            "decision_action_ids": ["A-1"],
        }],
        "audience_priors": [
            {
                "audience_prior_id": "AP-1",
                "scope_id": scope_id,
                "audience_description": "由当前项目语义推导的一次观看先验 A",
                "assumed_known_proposition_ids": [],
                "assumed_unknown_proposition_ids": ["P-ADAPTED-1"],
                "familiarity_assumptions": [],
                "language_and_context_assumptions": [],
                "attention_memory_assumptions": {},
                "calibration_source": "needs_review",
            },
            {
                "audience_prior_id": "AP-2",
                "scope_id": scope_id,
                "audience_description": "与 A 具有不同已知命题或记忆条件的当前项目先验 B",
                "assumed_known_proposition_ids": ["P-ADAPTED-1"],
                "assumed_unknown_proposition_ids": [],
                "familiarity_assumptions": [],
                "language_and_context_assumptions": [],
                "attention_memory_assumptions": {},
                "calibration_source": "needs_review",
            },
        ],
        "audience_states": [
            {
                "audience_state_id": "AS-AP1-IN",
                "audience_prior_id": "AP-1",
                "anchor": {"type": "event", "id": "E-1"},
                "beliefs": [{
                    "proposition_id": "P-ADAPTED-1",
                    "stance": "unknown",
                    "confidence": 0.0,
                    "evidence_ids": [],
                }],
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": [{"proposition_id": "P-ADAPTED-1", "retention_confidence": 0.7}],
                "attention_residue_ids": [],
                "affective_state": {},
            },
            {
                "audience_state_id": "AS-AP1-OUT",
                "audience_prior_id": "AP-1",
                "anchor": {"type": "event", "id": "E-1"},
                "beliefs": [{
                    "proposition_id": "P-ADAPTED-1",
                    "stance": "suspected",
                    "confidence": 0.6,
                    "evidence_ids": ["EV-1"],
                }],
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": [{"proposition_id": "P-ADAPTED-1", "retention_confidence": 0.7}],
                "attention_residue_ids": [],
                "affective_state": {},
            },
            {
                "audience_state_id": "AS-AP2-IN",
                "audience_prior_id": "AP-2",
                "anchor": {"type": "event", "id": "E-1"},
                "beliefs": [],
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": [{"proposition_id": "P-ADAPTED-1", "retention_confidence": 0.7}],
                "attention_residue_ids": [],
                "affective_state": {},
            },
            {
                "audience_state_id": "AS-AP2-OUT",
                "audience_prior_id": "AP-2",
                "anchor": {"type": "event", "id": "E-1"},
                "beliefs": [],
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": [{"proposition_id": "P-ADAPTED-1", "retention_confidence": 0.7}],
                "attention_residue_ids": ["DQ-1"],
                "affective_state": {},
            },
        ],
        "experience_intents": [{
            "experience_intent_id": "XI-1",
            "scope_id": scope_id,
            "anchor_event_ids": ["E-1"],
            "director_objective": "这一段希望观众经历的状态变化",
            "attention_target_ids": ["P-ADAPTED-1"],
            "audience_paths": [
                {
                    "audience_path_id": "XP-AP1-1",
                    "audience_prior_id": "AP-1",
                    "audience_state_in_id": "AS-AP1-IN",
                    "audience_state_out_target_id": "AS-AP1-OUT",
                    "target_deltas": [{
                        "target_delta_id": "XD-AP1-1",
                        "dimension": "belief",
                        "proposition_ids": ["P-ADAPTED-1"],
                        "description": "该先验观众需要发生的状态差",
                        "from_state": {"stance": "unknown", "confidence": 0.0},
                        "to_state": {"stance": "suspected", "confidence": 0.6},
                        "target_confidence": 0.6,
                        "required_processing_s": 1.0,
                        "deadline_event_id": "E-2",
                        "primary_delivery_window_id": "RW-1",
                        "custom_dimension": None,
                    }],
                },
                {
                    "audience_path_id": "XP-AP2-1",
                    "audience_prior_id": "AP-2",
                    "audience_state_in_id": "AS-AP2-IN",
                    "audience_state_out_target_id": "AS-AP2-OUT",
                    "target_deltas": [{
                        "target_delta_id": "XD-AP2-1",
                        "dimension": "attention",
                        "proposition_ids": ["P-ADAPTED-1"],
                        "description": "该先验观众需要把注意集中到仍未解决的问题",
                        "from_state": {"attention_residue_ids": []},
                        "to_state": {"attention_residue_ids": ["DQ-1"]},
                        "target_confidence": None,
                        "required_processing_s": 0.5,
                        "deadline_event_id": "E-2",
                        "primary_delivery_window_id": "RW-1",
                        "custom_dimension": None,
                    }],
                },
            ],
            "withheld_propositions": [],
            "forbidden_misconceptions": [],
        }],
        "assimilation_tasks": [{
            "assimilation_task_id": "AT-1",
            "experience_intent_id": "XI-1",
            "audience_path_id": "XP-AP1-1",
            "target_delta_id": "XD-AP1-1",
            "required_prior_proposition_ids": [],
            "downstream_dependency_event_ids": ["E-2"],
            "satisfaction_criteria": "可由冷观众观察验证的达成条件",
            "status": "planned",
        }],
        "readability_windows": [
            {
                "readability_window_id": "RW-1",
                "event_ids": ["E-1"],
                "proposition_ids": ["P-ADAPTED-1"],
                "target_delta_ids": ["XD-AP1-1", "XD-AP2-1"],
                "shot_ids": [],
                "attention_target_ids": ["P-ADAPTED-1"],
                "evidence_ids": ["EV-1"],
                "scheduled_processing_s": 1.0,
                "planned_available_s": 1.0,
                "competing_attention_ids": [],
                "readability_reason": "在下游事件使用前交付证据并留出逐先验处理时间",
                "status": "planned",
            },
            {
                "readability_window_id": "RW-2",
                "event_ids": ["E-2"],
                "proposition_ids": ["P-ADAPTED-1"],
                "target_delta_ids": [],
                "shot_ids": [],
                "attention_target_ids": ["P-ADAPTED-1"],
                "evidence_ids": ["EV-2"],
                "scheduled_processing_s": 0.5,
                "planned_available_s": 0.5,
                "competing_attention_ids": [],
                "readability_reason": "让行动完成条件在切离前可观察",
                "status": "planned",
            },
        ],
        "setup_payoff_contracts": [{
            "setup_payoff_id": "SP-1",
            "setup_proposition_ids": ["P-ADAPTED-1"],
            "setup_event_ids": ["E-1"],
            "payoff_event_ids": ["E-2"],
            "intended_inference_ids": ["P-ADAPTED-1"],
            "retention_deadline_event_id": "E-2",
            "minimum_retention_confidence": 0.5,
            "recall_needed": False,
            "status": "paid_off",
        }],
        "scene_contracts": [{
            "scene_id": "SC01",
            "applicability": "applies",
            "not_applicable_reason": None,
            "alternative_dramatic_function": None,
            "scene_question_id": "DQ-1",
            "point_of_view_character_id": "character-id",
            "audience_state_paths": [
                {"audience_prior_id": "AP-1", "audience_state_in_id": "AS-AP1-IN", "audience_state_out_target_id": "AS-AP1-OUT"},
                {"audience_prior_id": "AP-2", "audience_state_in_id": "AS-AP2-IN", "audience_state_out_target_id": "AS-AP2-OUT"},
            ],
            "character_state_in_ids": ["CDS-1"],
            "goal_proposition_ids": ["P-ADAPTED-1"],
            "obstacle_proposition_ids": ["P-ADAPTED-1"],
            "stakes_proposition_ids": ["P-ADAPTED-1"],
            "pressure_curve": [{"anchor": {"type": "event", "id": "E-1"}, "value": 0.5}],
            "turn_event_ids": ["E-2"],
            "value_polarity_in": "入场价值",
            "value_polarity_out": "离场价值",
            "relationship_deltas": [],
            "character_state_out_ids": ["CDS-1"],
            "scene_button": "场景结束时交给下一场的决定、问题或冲击",
        }],
        "arc_contracts": [{
            "arc_id": "ARC-EPISODE",
            "scope": "episode",
            "applicability": "applies",
            "not_applicable_reason": None,
            "alternative_dramatic_function": None,
            "core_question_ids": ["DQ-1"],
            "promise_proposition_ids": ["P-ADAPTED-1"],
            "escalation_event_ids": ["E-1"],
            "climax_event_ids": ["E-2"],
            "payoff_contract_ids": ["SP-1"],
            "pressure_curve": [{"anchor": {"type": "event", "id": "E-1"}, "value": 0.5}],
            "information_density_curve": [{"anchor": {"type": "event", "id": "E-1"}, "value": 0.5}],
            "processing_beats": [{"anchor": {"type": "event", "id": "E-1"}, "purpose": "消化、停顿或转向"}],
            "ending_hook_question_ids": [],
            "resolved_question_ids": [],
            "carried_question_ids": ["DQ-1"],
        }],
    }
    return json.dumps(example, ensure_ascii=False, separators=(",", ":"))


def _screenplay(scope_id: str) -> EpisodeScreenplay:
    return EpisodeScreenplay(
        id=f"script-{scope_id}",
        episode_no=1,
        title=scope_id,
        narrative_plan=json.loads(_narrative_plan_schema_example(scope_id)),
    )


def _delta_id(prior_id: str) -> str:
    return "XD-AP1-1" if prior_id == "AP-1" else "XD-AP2-1"


def _observation(
    scope_id: str,
    prior_id: str,
    score: float,
    *,
    review_artifact_id: str | None = None,
    dimensions: dict | None = None,
    ordinal: int = 1,
) -> HumanOneWatchObservation:
    return HumanOneWatchObservation(
        observation_id=f"H-{scope_id}-{prior_id}-{ordinal}",
        participant_id_hash=f"participant-{scope_id}-{prior_id}-{ordinal}",
        scope_id=scope_id,
        audience_prior_id=prior_id,
        narrative_review_artifact_id=review_artifact_id or f"review-{scope_id}",
        watched_once=True,
        watch_count=1,
        replay_or_seek_used=False,
        source_material_seen=False,
        target_answers_seen=False,
        director_intent_seen=False,
        spontaneous_recall_frozen=True,
        spontaneous_recall={"free_text": "只记录一次观看后自然想起的内容"},
        target_delta_observations=[HumanTargetDeltaObservation(
            audience_prior_id=prior_id,
            target_delta_id=_delta_id(prior_id),
            observed_score=score,
            observed_interpretation={"free_semantics": "人类观察的开放语义"},
        )],
        content_dimensions=dimensions or {"genre": "g1", "form": "f1"},
    )


def _estimate(scope_id: str, prior_id: str, score: float) -> ModelTargetEstimate:
    return ModelTargetEstimate(
        scope_id=scope_id,
        audience_prior_id=prior_id,
        target_delta_id=_delta_id(prior_id),
        predicted_score=score,
        narrative_review_artifact_id=f"review-{scope_id}",
    )


def _persist_freeze(
    observation: HumanOneWatchObservation,
    screenplay: EpisodeScreenplay,
    review_artifact_id: str,
) -> dict:
    freeze = HumanOneWatchFreeze.model_validate(
        observation.model_dump(
            mode="json",
            exclude={"neutral_followup_observations", "target_delta_observations"},
        )
    )
    return persist_human_one_watch_freeze(
        freeze,
        screenplay=screenplay,
        narrative_review_artifact_ids=[review_artifact_id],
    )


@pytest.fixture()
def calibration_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "narrative-calibration.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    yield db.get_conn()


@pytest.mark.parametrize(
    "overrides",
    [
        {"watched_once": False},
        {"watch_count": 2},
        {"replay_or_seek_used": True},
        {"source_material_seen": True},
        {"target_answers_seen": True},
        {"director_intent_seen": True},
        {"spontaneous_recall_frozen": False},
    ],
)
def test_human_observation_strictly_enforces_blind_single_watch(overrides) -> None:
    payload = _observation("scope-1", "AP-1", 0.5).model_dump(mode="json")
    payload.update(overrides)

    with pytest.raises(ValidationError):
        HumanOneWatchObservation.model_validate(payload)


def test_observation_must_cover_exact_prior_target_pairs() -> None:
    screenplay = _screenplay("scope-1")
    observation = _observation("scope-1", "AP-1", 0.5)
    observation.target_delta_observations[0].target_delta_id = "XD-NOT-IN-PLAN"

    errors = validate_human_one_watch_observation(observation, screenplay)

    assert any("HUMAN_PAIR_MISSING" in item for item in errors)
    assert any("HUMAN_PAIR_UNKNOWN" in item for item in errors)


def test_sparse_constant_cross_content_sample_stays_needs_review() -> None:
    screenplay = _screenplay("scope-1")
    observations = [
        _observation("scope-1", "AP-1", 1.0),
        _observation("scope-1", "AP-2", 1.0),
    ]
    estimates = [
        _estimate("scope-1", "AP-1", 1.0),
        _estimate("scope-1", "AP-2", 1.0),
    ]

    report = build_calibration_report(
        calibration_report_id="CAL-1",
        calibration_scope_id="corpus-1",
        screenplays=[screenplay],
        observations=observations,
        model_estimates=estimates,
    )

    assert report.decision == "needs_review"
    assert report.calibration_score is None
    assert report.confidence_status == "needs_review"
    assert any("CROSS_DIMENSION_SAMPLE_INSUFFICIENT" in item for item in report.coverage_gaps)
    assert any("CROSS_SCOPE_SAMPLE_INSUFFICIENT" in item for item in report.coverage_gaps)
    assert any("HUMAN_PAIR_REPLICATION_INSUFFICIENT" in item for item in report.coverage_gaps)
    assert any("MODEL_SCORES_CONSTANT" in item for item in report.stability_issues)
    assert any("HUMAN_SCORES_CONSTANT" in item for item in report.stability_issues)


def test_cross_dimension_stable_nonconstant_pairs_can_be_calibrated() -> None:
    dimensions = [
        {"genre": "g1", "form": "f1"},
        {"genre": "g1", "form": "f2"},
        {"genre": "g2", "form": "f1"},
        {"genre": "g2", "form": "f2"},
    ]
    scores = [(0.1, 0.2), (0.3, 0.4), (0.6, 0.7), (0.8, 0.9)]
    screenplays: list[EpisodeScreenplay] = []
    observations: list[HumanOneWatchObservation] = []
    estimates: list[ModelTargetEstimate] = []
    for index, (content_dimensions, pair_scores) in enumerate(
        zip(dimensions, scores, strict=True), start=1,
    ):
        scope_id = f"scope-{index}"
        screenplays.append(_screenplay(scope_id))
        for prior_id, score in zip(("AP-1", "AP-2"), pair_scores, strict=True):
            observations.extend(
                _observation(
                    scope_id,
                    prior_id,
                    score,
                    dimensions=content_dimensions,
                    ordinal=ordinal,
                )
                for ordinal in (1, 2)
            )
            estimates.append(_estimate(scope_id, prior_id, score))

    report = build_calibration_report(
        calibration_report_id="CAL-STABLE",
        calibration_scope_id="corpus-stable",
        screenplays=screenplays,
        observations=observations,
        model_estimates=estimates,
    )

    assert report.decision == "calibrated"
    assert report.calibration_score == pytest.approx(1.0)
    assert report.coverage_gaps == []
    assert report.stability_issues == []
    assert all(item.correlation == pytest.approx(1.0) for item in report.dimension_results)


def test_opposing_cross_dimension_correlations_stay_needs_review() -> None:
    dimensions = [
        {"genre": "g1", "form": "f1"},
        {"genre": "g1", "form": "f2"},
        {"genre": "g2", "form": "f1"},
        {"genre": "g2", "form": "f2"},
    ]
    model_scores = [(0.1, 0.2), (0.3, 0.4), (0.6, 0.7), (0.8, 0.9)]
    human_scores = [(0.1, 0.2), (0.3, 0.4), (0.9, 0.8), (0.7, 0.6)]
    screenplays: list[EpisodeScreenplay] = []
    observations: list[HumanOneWatchObservation] = []
    estimates: list[ModelTargetEstimate] = []
    for index, (content_dimensions, predicted, observed) in enumerate(
        zip(dimensions, model_scores, human_scores, strict=True), start=1,
    ):
        scope_id = f"unstable-{index}"
        screenplays.append(_screenplay(scope_id))
        for prior_id, predicted_score, observed_score in zip(
            ("AP-1", "AP-2"), predicted, observed, strict=True,
        ):
            observations.extend(
                _observation(
                    scope_id,
                    prior_id,
                    observed_score,
                    dimensions=content_dimensions,
                    ordinal=ordinal,
                )
                for ordinal in (1, 2)
            )
            estimates.append(_estimate(scope_id, prior_id, predicted_score))

    report = build_calibration_report(
        calibration_report_id="CAL-UNSTABLE",
        calibration_scope_id="corpus-unstable",
        screenplays=screenplays,
        observations=observations,
        model_estimates=estimates,
    )

    assert report.decision == "needs_review"
    assert report.calibration_score is None
    assert any(
        "CORRELATION_UNSTABLE_ACROSS_DIMENSIONS" in item
        for item in report.stability_issues
    )


def test_model_and_human_scores_must_bind_the_same_review_artifact() -> None:
    screenplay = _screenplay("scope-1")
    observations = [
        _observation("scope-1", "AP-1", 0.2),
        _observation("scope-1", "AP-2", 0.9),
    ]
    estimates = [
        _estimate("scope-1", "AP-1", 0.2),
        _estimate("scope-1", "AP-2", 0.9),
    ]
    estimates[0].narrative_review_artifact_id = "another-review-version"

    with pytest.raises(
        CalibrationContractError,
        match="MODEL_HUMAN_REVIEW_LINEAGE_MISMATCH",
    ):
        build_calibration_report(
            calibration_report_id="CAL-LINEAGE",
            calibration_scope_id="corpus-lineage",
            screenplays=[screenplay],
            observations=observations,
            model_estimates=estimates,
        )


def test_human_and_calibration_artifacts_preserve_review_lineage(
    calibration_db,
) -> None:
    screenplay = _screenplay("scope-1")
    review = evidence_repository.create_artifact(EvidenceArtifact(
        id="review-scope-1",
        type="narrative_review_report",
        scope_type="episode",
        scope_id="scope-1",
        status="validated",
        trust_level="T2",
        content={"decision": "pass"},
    ))
    observations = [
        _observation("scope-1", "AP-1", 1.0, review_artifact_id=review["id"]),
        _observation("scope-1", "AP-2", 1.0, review_artifact_id=review["id"]),
    ]
    observation_artifacts = []
    for observation in observations:
        freeze_artifact = _persist_freeze(
            observation,
            screenplay,
            review["id"],
        )
        observation_artifacts.append(
            persist_human_one_watch_observation(
                observation,
                screenplay=screenplay,
                narrative_review_artifact_ids=[review["id"]],
                frozen_recall_artifact_id=freeze_artifact["id"],
            )
        )
    estimates = [
        ModelTargetEstimate(
            scope_id="scope-1",
            audience_prior_id=prior_id,
            target_delta_id=_delta_id(prior_id),
            predicted_score=1.0,
            narrative_review_artifact_id=review["id"],
        )
        for prior_id in ("AP-1", "AP-2")
    ]
    report = build_calibration_report(
        calibration_report_id="CAL-PERSISTED",
        calibration_scope_id="corpus-persisted",
        screenplays=[screenplay],
        observations=observations,
        model_estimates=estimates,
    )

    artifact = persist_calibration_report(
        report,
        observation_artifact_ids=[item["id"] for item in observation_artifacts],
        narrative_review_artifact_ids=[review["id"]],
    )

    assert artifact["status"] == "needs_revision"
    assert set(artifact["parent_artifact_ids"]) == {
        review["id"],
        *(item["id"] for item in observation_artifacts),
    }
    evaluations = evidence_repository.get_evaluations(artifact["id"])
    assert evaluations[-1]["score"] is None
    assert evaluations[-1]["score_status"] == "unknown"
    assert evaluations[-1]["evidence"]["decision"] == "needs_review"


def test_observation_persistence_rejects_missing_review_report_lineage(
    calibration_db,
) -> None:
    screenplay = _screenplay("scope-1")
    wrong_parent = evidence_repository.create_artifact(EvidenceArtifact(
        id="not-a-review",
        type="storyboard_document",
        scope_type="episode",
        scope_id="scope-1",
        status="validated",
        trust_level="T2",
        content={},
    ))
    observation = _observation(
        "scope-1", "AP-1", 0.5, review_artifact_id=wrong_parent["id"],
    )

    with pytest.raises(CalibrationContractError, match="CALIBRATION_REVIEW_REPORT_MISSING"):
        persist_human_one_watch_observation(
            observation,
            screenplay=screenplay,
            narrative_review_artifact_ids=[wrong_parent["id"]],
            frozen_recall_artifact_id="missing-freeze",
        )


def test_final_human_observation_cannot_rewrite_frozen_recall(
    calibration_db,
) -> None:
    screenplay = _screenplay("scope-1")
    review = evidence_repository.create_artifact(EvidenceArtifact(
        id="review-scope-1",
        type="narrative_review_report",
        scope_type="episode",
        scope_id="scope-1",
        status="validated",
        trust_level="T2",
        content={"decision": "pass"},
    ))
    observation = _observation(
        "scope-1",
        "AP-1",
        0.8,
        review_artifact_id=review["id"],
    )
    freeze = _persist_freeze(observation, screenplay, review["id"])
    observation.spontaneous_recall = {"free_text": "rewritten after targets"}

    with pytest.raises(
        CalibrationContractError,
        match="HUMAN_FREEZE_OBSERVATION_DRIFT",
    ):
        persist_human_one_watch_observation(
            observation,
            screenplay=screenplay,
            narrative_review_artifact_ids=[review["id"]],
            frozen_recall_artifact_id=freeze["id"],
        )


def test_board_ui_exposes_two_stage_human_calibration_without_internal_codes() -> None:
    source = (
        Path(__file__).parents[1]
        / "frontend"
        / "src"
        / "pages"
        / "BoardPage.tsx"
    ).read_text(encoding="utf-8")

    assert "narrative-calibration/freeze" in source
    assert "narrative-calibration/observations" in source
    assert "narrative-calibration/ai-simulate" in source
    assert "运行 AI 一次观看模拟" in source
    assert "不会伪造真人参与者或观察记录" in source
    assert "真人一次观看校准" in source
    assert source.index("冻结首次复述") < source.index("提交真人观察")
    assert "title={freezeBlockedReason || undefined}" in source
    assert "human_one_watch_calibration_report" not in source
    assert "NARRATIVE_CALIBRATION_REQUIRED" not in source


def test_ai_one_watch_waives_only_the_extra_layer_when_simulation_is_weak(
    calibration_db,
) -> None:
    report = NarrativeReviewReport(
        narrative_review_report_id="NRR-WEAK-SIMULATION",
        scope_id="scope-1",
        target_delta_results=[{
            "audience_prior_id": "AP-1",
            "target_delta_id": "XD-AP1-1",
            "result": "satisfied",
            "predicted_score": 0.4,
        }],
        decision="pass",
        reason="The blind review gate passed but its confidence is weak.",
    )
    review = evidence_repository.create_artifact(EvidenceArtifact(
        type="narrative_review_report",
        scope_type="episode",
        scope_id="scope-1",
        status="validated",
        trust_level="T2",
        content=report.model_dump(mode="json"),
    ))

    artifact = persist_ai_one_watch_simulation_authority(
        report,
        narrative_review_artifact_id=review["id"],
    )
    authority = require_current_calibration_authority(
        expected_artifact_id=artifact["id"],
    )

    assert authority.authority_mode == "waived"
    assert authority.model_pass_threshold == 0.0
    assert authority.report.sample_summary["simulation_supported"] is False
    assert_report_meets_current_calibration(
        report,
        expected_calibration_artifact_id=artifact["id"],
    )
