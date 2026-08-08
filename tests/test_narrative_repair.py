from __future__ import annotations

import json

import pytest

from app.harness.types import Issue, IssueSeverity
from app.narrative_repair import (
    SemanticCandidateAssessment,
    SemanticOutlineOperation,
    SemanticRepairDiagnosis,
    _compact_context,
    _focus_operation_errors,
    apply_semantic_outline_operations,
    diagnose_narrative_repair,
    reproject_semantic_outline_authority,
    route_narrative_issues,
    validate_semantic_diagnosis,
)
from app.schemas import Storyboard, StoryboardOutline, StoryboardOutlineShot
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


def test_semantic_repair_context_is_bounded_to_the_focus_window() -> None:
    shots = [
        _shot().model_copy(update={"shot_no": number, "shot_id": f"SH-{number}"})
        for number in range(1, 21)
    ]
    board = Storyboard(episode_no=1, shots=shots)
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot.model_validate(shot.model_dump(mode="json"))
            for shot in shots
        ],
    )
    issue = Issue(
        code="SHOT_PROMPT_COMPILE_FAILED",
        severity=IssueSeverity.BLOCKER,
        category="structural",
        subject="storyboard",
        message="第 15 镜下游 Prompt 合同不可编译",
        evidence={"path": "shots[14]"},
        repairable=True,
    )

    context = _compact_context(
        [issue],
        _screenplay(),
        board,
        outline,
        focus_shot_no=15,
    )

    assert context["context_scope"]["included_shot_nos"] == [13, 14, 15, 16, 17]
    assert [shot["shot_no"] for shot in context["shots"]] == [13, 14, 15, 16, 17]


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


def test_semantic_outline_reprojection_restores_graph_owned_ledgers() -> None:
    screenplay = _screenplay()
    outline = _outline()
    reproject_semantic_outline_authority(outline, screenplay)
    authoritative = outline.shots[0].model_copy(deep=True)
    replacement_payload = authoritative.model_dump(mode="json")
    replacement_payload["completed_before_action_ids"] = ["A-bounded-summary"]
    replacement = StoryboardOutlineShot.model_validate(replacement_payload)

    candidate, _events = apply_semantic_outline_operations(
        outline,
        [SemanticOutlineOperation(
            op="repair-visible-assimilation",
            executor="replace_outline_shot",
            target={"shot_id": authoritative.shot_id},
            value=replacement,
        )],
    )

    assert candidate.shots[0].completed_before_action_ids == [
        "A-bounded-summary"
    ]
    reproject_semantic_outline_authority(candidate, screenplay)
    assert candidate.shots[0].completed_before_action_ids == (
        authoritative.completed_before_action_ids
    )


@pytest.mark.asyncio
async def test_diagnosis_validates_reprojected_semantic_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screenplay = _screenplay()
    outline = _outline()
    reproject_semantic_outline_authority(outline, screenplay)
    replacement_payload = outline.shots[0].model_dump(mode="json")
    replacement_payload["completed_before_action_ids"] = ["A-bounded-summary"]
    selected = SemanticCandidateAssessment(
        strategy="repair_current",
        expected_narrative_gain=0.8,
        destructive_cost=0.1,
        satisfies_gap_test=True,
        passes_deletion_test=True,
        passes_marginal_gain_test=True,
        preserves_invariants=True,
        rationale="The local directing change closes the measured audience gap.",
        outline_operations=[SemanticOutlineOperation(
            op="repair-visible-assimilation",
            executor="replace_outline_shot",
            target={"shot_id": outline.shots[0].shot_id},
            value=StoryboardOutlineShot.model_validate(replacement_payload),
        )],
    )
    diagnosis = _diagnosis(assessments=[
        selected,
        _assessment("insert_shot", gain=0.4, cost=0.3),
    ])

    async def fake_chat(*_args, **_kwargs):
        return json.dumps(diagnosis.model_dump(mode="json"), ensure_ascii=False)

    monkeypatch.setattr("app.narrative_repair.model_gateway.chat", fake_chat)
    result = await diagnose_narrative_repair(
        episode_id="episode-generic",
        issues=[Issue(
            code="AUDIENCE_TARGET_MISSED",
            severity=IssueSeverity.BLOCKER,
            subject="shot:1",
            message="The visible assimilation target was missed.",
            repairable=True,
        )],
        screenplay=screenplay,
        board=_board(),
        outline=outline,
        focus_shot_no=1,
        validated_prefix_end=0,
    )

    assert result.selected_strategy == "repair_current"


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
        focus_shot_no=1,
        validated_prefix_end=0,
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
    assert captured["request"]["repair_focus"]["focus_shot_no"] == 1
    assert "未来 brief" in "\n".join(captured["request"]["hard_rules"])
    transition_contract = captured["request"][
        "boundary_state_transition_contract"
    ]
    assert "必填" in transition_contract["transition_id"]
    assert "action_phase_handoff" in transition_contract["basis_type"]
    hard_rules = "\n".join(captured["request"]["hard_rules"])
    assert "不得输出 from_fact_id" in hard_rules
    assert "source_fact_id 与 target_fact_id 相同" in hard_rules
    assert captured["meta"]["stage_key"] == "semantic_repair_planner"


def test_focus_operation_errors_reject_remote_future_shot() -> None:
    replacement = StoryboardOutlineShot.model_validate(
        _settled_followup_shot().model_dump(mode="json")
    )
    selected = SemanticCandidateAssessment(
        strategy="repair_current_and_remote",
        expected_narrative_gain=0.9,
        destructive_cost=0.2,
        satisfies_gap_test=True,
        passes_marginal_gain_test=True,
        preserves_invariants=True,
        outline_operations=[
            SemanticOutlineOperation(
                op="repair-focus",
                executor="replace_outline_shot",
                target={"shot_no": 6},
                value=replacement.model_copy(update={"shot_no": 6}),
            ),
            SemanticOutlineOperation(
                op="repair-remote",
                executor="replace_outline_shot",
                target={"shot_no": 9},
                value=replacement.model_copy(update={"shot_no": 9}),
            ),
        ],
    )
    diagnosis = SemanticRepairDiagnosis(
        diagnosis_id="NRD-focus",
        semantic_gap="The current shot needs a local repair.",
        candidate_assessments=[
            _assessment("repair_current", gain=0.2, cost=0.1),
            selected,
        ],
        selected_strategy=selected.strategy,
        selection_reason="The model incorrectly included a remote future shot.",
    )

    errors = _focus_operation_errors(diagnosis, focus_shot_no=6)

    assert any("禁止跨到远端镜头：[9]" in error for error in errors)


def test_focus_operation_errors_resolve_stable_ids_and_insert_positions() -> None:
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot.model_validate(
                _shot(shot_no=1, shot_id="SH-1").model_dump(mode="json")
            ),
            StoryboardOutlineShot.model_validate(
                _settled_followup_shot(
                    shot_no=2, shot_id="SH-2",
                ).model_dump(mode="json")
            ),
            StoryboardOutlineShot.model_validate(
                _settled_followup_shot(
                    shot_no=3, shot_id="SH-3",
                ).model_dump(mode="json")
            ),
            StoryboardOutlineShot.model_validate(
                _settled_followup_shot(
                    shot_no=4, shot_id="SH-4",
                ).model_dump(mode="json")
            ),
        ],
    )
    replacement = outline.shots[-1].model_copy(deep=True)
    selected = SemanticCandidateAssessment(
        strategy="repair_remote_stable_id",
        expected_narrative_gain=0.9,
        destructive_cost=0.2,
        satisfies_gap_test=True,
        passes_marginal_gain_test=True,
        preserves_invariants=True,
        outline_operations=[
            SemanticOutlineOperation(
                op="replace-remote",
                executor="replace_outline_shot",
                target={"shot_id": "SH-4"},
                value=replacement,
            ),
        ],
    )
    diagnosis = SemanticRepairDiagnosis(
        diagnosis_id="NRD-stable-id",
        semantic_gap="The model targeted a remote stable ID.",
        candidate_assessments=[
            _assessment("repair_current", gain=0.2, cost=0.1),
            selected,
        ],
        selected_strategy=selected.strategy,
        selection_reason="The remote edit must be rejected.",
    )

    errors = _focus_operation_errors(
        diagnosis,
        focus_shot_no=1,
        outline=outline,
    )

    assert any("禁止跨到远端镜头：[4]" in error for error in errors)
    assert any("未触及当前失败镜" in error for error in errors)


@pytest.mark.asyncio
async def test_uncommitted_candidate_retries_current_slot_before_semantic_planning(
    monkeypatch,
) -> None:
    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("uncommitted candidate must not invoke semantic planning")

    monkeypatch.setattr("app.narrative_repair.model_gateway.chat", must_not_run)
    issue = Issue(
        code="CONTRACT_FIELD_INVALID",
        severity=IssueSeverity.BLOCKER,
        subject="shot:2",
        message="The generated candidate violates a local contract.",
        repairable=True,
    )

    plan = await route_narrative_issues(
        [issue],
        episode_id="episode-generic",
        screenplay=_screenplay(),
        board=_board(),
        validated_prefix_end=1,
        next_shot_no=2,
        uncommitted_candidate=True,
    )

    assert plan.strategy == "repair_current"
    assert plan.pause_state is None
    assert plan.needs_semantic_selection is False
    assert plan.semantic_diagnosis["execution_verified"] is True


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


def test_semantic_diagnosis_rejects_whole_outline_rewrite_for_local_issue() -> None:
    replacement = StoryboardOutlineShot.model_validate(
        _shot().model_dump(mode="json")
    )
    rewrite = SemanticCandidateAssessment(
        strategy="rewrite_many_shots",
        expected_narrative_gain=0.9,
        destructive_cost=0.2,
        satisfies_gap_test=True,
        passes_marginal_gain_test=True,
        preserves_invariants=True,
        outline_operations=[
            SemanticOutlineOperation(
                op=f"rewrite-shot-{shot_no}",
                executor="replace_outline_shot",
                target={"shot_no": 1},
                value=replacement,
            )
            for shot_no in range(4)
        ],
    )
    diagnosis = SemanticRepairDiagnosis(
        diagnosis_id="NRD-local-bound",
        semantic_gap="One local capacity relation is invalid.",
        candidate_assessments=[
            _assessment("repair_current", gain=0.3, cost=0.1),
            rewrite,
        ],
        selected_strategy=rewrite.strategy,
        selection_reason="The model attempted to rewrite unrelated shots.",
    )

    assert "选中候选最多允许 3 个局部大纲操作" in (
        validate_semantic_diagnosis(diagnosis)
    )


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
