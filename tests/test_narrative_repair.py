from __future__ import annotations

import json

import pytest

from app.harness.types import Issue, IssueSeverity
from app.narrative_repair import (
    SemanticCandidateAssessment,
    SemanticOutlineOperation,
    SemanticRepairDiagnosis,
    diagnose_narrative_repair,
    route_narrative_issues,
    validate_semantic_diagnosis,
)
from app.schemas import StoryboardOutline, StoryboardOutlineShot
from tests.test_narrative_continuity import (
    _board,
    _screenplay,
    _settled_followup_shot,
    _shot,
)


def _assessment(strategy: str, *, gain: float, cost: float) -> SemanticCandidateAssessment:
    return SemanticCandidateAssessment(
        strategy=strategy,
        expected_narrative_gain=gain,
        destructive_cost=cost,
        satisfies_gap_test=True,
        passes_deletion_test=True,
        passes_marginal_gain_test=True,
        preserves_invariants=True,
        rationale="The relation-level evidence supports this candidate.",
    )


def _diagnosis(*, assessments: list[SemanticCandidateAssessment]) -> SemanticRepairDiagnosis:
    return SemanticRepairDiagnosis(
        diagnosis_id="NRD-generic",
        semantic_gap="An audience-state handoff has an unmodeled relation.",
        affected_shot_nos=[1],
        affected_relation_ids=["XD-cold"],
        scope="current_shot",
        candidate_assessments=assessments,
        selected_strategy="repair_current",
        selection_reason="The current shot is the least destructive sufficient candidate.",
        unclassified_dimensions=[
            {
                "dimension": "project_specific_relation",
                "description": "Preserve this open semantic dimension without classifying by keywords.",
            }
        ],
    )


def _outline() -> StoryboardOutline:
    screenplay = _screenplay()
    return StoryboardOutline(
        episode_no=1,
        shots=[StoryboardOutlineShot.model_validate(
            _shot().model_dump(mode="json")
        )],
        readability_windows=[
            window.model_copy(deep=True)
            for window in screenplay.narrative_plan.readability_windows
        ],
    )


def test_semantic_diagnosis_requires_multiple_candidates_and_preserves_open_dimensions() -> None:
    one_candidate = _diagnosis(
        assessments=[_assessment("repair_current", gain=0.8, cost=0.1)]
    )
    two_candidates = _diagnosis(
        assessments=[
            _assessment("repair_current", gain=0.8, cost=0.1),
            _assessment("insert_shot", gain=0.5, cost=0.4),
        ]
    )

    assert "语义诊断必须比较至少两个候选，不能把问题码直接映射成唯一修复" in (
        validate_semantic_diagnosis(one_candidate)
    )
    assert validate_semantic_diagnosis(two_candidates) == []

    payload = two_candidates.router_payload()

    assert payload["unclassified_dimensions"] == two_candidates.unclassified_dimensions
    assert payload["candidate_scores"] == {
        "repair_current": pytest.approx(0.7),
        "insert_shot": pytest.approx(0.1),
    }


def test_public_split_alias_normalizes_its_candidate_score_only() -> None:
    diagnosis = SemanticRepairDiagnosis(
        diagnosis_id="NRD-split-alias",
        semantic_gap="One measured contribution exceeds its current window.",
        candidate_assessments=[
            _assessment("repair_current", gain=0.3, cost=0.1),
            _assessment("split_shot", gain=0.9, cost=0.2),
        ],
        selected_strategy="split_shot",
        selection_reason="The adjacent typed candidate has greater measured gain.",
    )

    payload = diagnosis.router_payload(execution_verified=True)

    assert payload["selected_strategy"] == "split_shot"
    assert payload["candidate_scores"]["split_adjacent_shot"] == pytest.approx(0.7)
    assert "split_shot" not in payload["candidate_scores"]


@pytest.mark.asyncio
async def test_model_diagnosis_compares_open_candidates_without_issue_code_routing(
    monkeypatch,
) -> None:
    captured: dict = {}

    async def fake_chat(messages, **kwargs):
        captured["request"] = json.loads(messages[1]["content"])
        captured["meta"] = kwargs["call_meta"]
        return json.dumps(
            _diagnosis(
                assessments=[
                    _assessment("repair_current", gain=0.8, cost=0.1),
                    _assessment("insert_shot", gain=0.4, cost=0.5),
                ]
            ).model_dump(mode="json"),
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.narrative_repair.model_gateway.chat", fake_chat)
    issue = Issue(
        code="SEMANTIC_GAP_OTHER",
        severity=IssueSeverity.BLOCKER,
        subject="shot:1",
        message="An open relation does not match a predefined semantic dimension.",
        evidence={"path": "/shots/1/shot_contribution"},
        repairable=True,
    )

    diagnosis = await diagnose_narrative_repair(
        episode_id="episode-generic",
        issues=[issue],
        screenplay=_screenplay(),
        board=_board(),
    )

    assert diagnosis.selected_strategy == "repair_current"
    assert diagnosis.unclassified_dimensions
    assert len(diagnosis.candidate_assessments) == 2
    assert captured["request"]["context"]["violated_invariants"][0]["code"] == (
        "SEMANTIC_GAP_OTHER"
    )
    assert set(captured["request"]["available_execution_capabilities"]) == {
        "replace_outline_shot",
        "insert_outline_shot",
        "delete_outline_shot",
        "move_outline_shot",
    }
    assert "开放语义意图" in captured["request"]["semantic_intent_contract"]
    assert captured["meta"]["stage_key"] == "semantic_repair_planner"


@pytest.mark.asyncio
async def test_provider_failure_pauses_instead_of_substituting_a_fixed_strategy(
    monkeypatch,
) -> None:
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.narrative_repair.model_gateway.chat", unavailable)
    issue = Issue(
        code="SEMANTIC_GAP_OTHER",
        severity=IssueSeverity.BLOCKER,
        subject="shot:1",
        message="An open narrative relation needs semantic diagnosis.",
        repairable=True,
    )

    plan = await route_narrative_issues(
        [issue],
        episode_id="episode-generic",
        screenplay=_screenplay(),
        board=_board(),
        validated_prefix_end=1,
    )

    assert plan.issue_codes == ["SEMANTIC_GAP_OTHER"]
    assert plan.strategy == "waiting_human"
    assert plan.pause_state == "WAITING_HUMAN"
    assert plan.reason == "semantic_strategy_not_executable"
    assert plan.needs_semantic_selection is True
    assert plan.semantic_diagnosis["selected_strategy"] == (
        "semantic_diagnosis_needs_review"
    )
    assert plan.semantic_diagnosis["execution_verified"] is False
    assert len(plan.candidates) >= 2
    assert all(
        candidate.strategy not in {"redo_suffix", "replan_outline"}
        for candidate in plan.candidates
    )


def test_open_semantic_operation_requires_a_declared_runtime_executor() -> None:
    unsupported = SemanticCandidateAssessment(
        strategy="repair_unmodeled_relation",
        expected_narrative_gain=0.8,
        destructive_cost=0.2,
        satisfies_gap_test=True,
        passes_marginal_gain_test=True,
        preserves_invariants=True,
        outline_operations=[SemanticOutlineOperation(
            op="stage_relation_specific_assimilation",
            target={"after_shot_id": "SH-1"},
            value=StoryboardOutlineShot.model_validate(
                _settled_followup_shot().model_dump(mode="json")
            ),
        )],
    )
    diagnosis = SemanticRepairDiagnosis(
        diagnosis_id="NRD-open-unsupported",
        semantic_gap="An unmodeled relation needs a different audience handoff.",
        candidate_assessments=[
            _assessment("repair_current", gain=0.3, cost=0.1),
            unsupported,
        ],
        selected_strategy=unsupported.strategy,
        selection_reason="The semantic intent needs an unavailable runtime capability.",
    )

    errors = validate_semantic_diagnosis(diagnosis)

    assert any("unavailable executor" in error for error in errors)
    assert any("必须提供可执行" in error for error in errors)


@pytest.mark.asyncio
async def test_open_strategy_and_operation_execute_after_typed_full_graph_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_strategy = "repair_relation_specific_assimilation"
    inserted = StoryboardOutlineShot.model_validate(
        _settled_followup_shot().model_dump(mode="json")
    )
    diagnosis = SemanticRepairDiagnosis(
        diagnosis_id="NRD-open-executable",
        semantic_gap="A relation needs one explicit processing beat.",
        affected_shot_nos=[1],
        affected_relation_ids=["XD-cold"],
        scope="structure",
        candidate_assessments=[
            _assessment("repair_current", gain=0.2, cost=0.1),
            SemanticCandidateAssessment(
                strategy=open_strategy,
                expected_narrative_gain=0.9,
                destructive_cost=0.2,
                satisfies_gap_test=True,
                passes_marginal_gain_test=True,
                preserves_invariants=True,
                rationale="The candidate supplies a measurable processing window.",
                outline_operations=[SemanticOutlineOperation(
                    op="stage_relation_specific_assimilation",
                    executor="insert_outline_shot",
                    target={"after_shot_id": "SH-1"},
                    value=inserted,
                )],
            ),
        ],
        selected_strategy=open_strategy,
        selection_reason="It is the smallest candidate that closes the measured gap.",
    )

    async def fake_chat(*_args, **_kwargs):
        return json.dumps(diagnosis.model_dump(mode="json"), ensure_ascii=False)

    monkeypatch.setattr("app.narrative_repair.model_gateway.chat", fake_chat)
    issue = Issue(
        code="SEMANTIC_GAP_OTHER",
        severity=IssueSeverity.BLOCKER,
        subject="shot:1",
        message="An open relation has no sufficient processing window.",
        repairable=True,
    )

    plan = await route_narrative_issues(
        [issue],
        episode_id="episode-generic",
        screenplay=_screenplay(),
        board=_board(),
        outline=_outline(),
        validated_prefix_end=1,
    )

    assert plan.strategy == open_strategy
    assert plan.pause_state is None
    assert plan.needs_semantic_selection is False
    assert plan.semantic_diagnosis["execution_verified"] is True
    assert plan.selected_candidate_id.startswith("candidate-open-")
    assert plan.candidates[-1].strategy == open_strategy
