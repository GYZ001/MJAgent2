from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel

from app import db
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.loops import AgentLoop, AgentLoopPolicy


class Candidate(BaseModel):
    value: int


def test_agent_loop_does_not_recall_producer_for_quality_only_issues() -> None:
    loop: AgentLoop[Candidate] = AgentLoop(
        stage_key="screenplay",
        contract_key="screenplay",
        goal="produce a valid candidate",
        scope_type="episode",
        scope_id="e1",
        artifact_type="episode_screenplay",
        policy=AgentLoopPolicy(max_iterations=4),
    )
    calls: list[int] = []

    async def producer(iteration: int, *_args) -> str:
        calls.append(iteration)
        return json.dumps({"value": 1})

    def evaluator(raw: str):
        return Candidate.model_validate(json.loads(raw)), [
            Issue(
                code="BUSINESS_RULE_FAILED",
                severity=IssueSeverity.BLOCKER,
                subject="episode:e1",
                message="main character motivation is weak",
                repairable=True,
            )
        ]

    result = asyncio.run(loop.run(producer, evaluator))

    assert calls == [1]
    assert result.status == "accepted"
    assert result.exit_reason == "score_only_quality"
    assert result.value.value == 1
    assert result.issues[0].code == "BUSINESS_RULE_FAILED"


def test_required_suffix_narrative_issue_is_not_treated_as_schema_failure() -> None:
    loop: AgentLoop[Candidate] = AgentLoop(
        stage_key="storyboard_outline",
        contract_key="storyboard",
        goal="keep a parseable outline",
        scope_type="episode",
        scope_id="e1",
        artifact_type="storyboard_outline",
        policy=AgentLoopPolicy(max_iterations=2),
    )
    calls: list[int] = []

    async def producer(iteration: int, *_args) -> str:
        calls.append(iteration)
        return json.dumps({"value": 1})

    def evaluator(raw: str):
        return Candidate.model_validate(json.loads(raw)), [
            Issue(
                code="CHARACTER_BELIEF_TRANSITION_REQUIRED",
                severity=IssueSeverity.BLOCKER,
                subject="episode:e1",
                message="人物认知变化表达不足",
                repairable=True,
            )
        ]

    result = asyncio.run(loop.run(producer, evaluator))

    assert calls == [1]
    assert result.exit_reason == "score_only_quality"


def test_commit_artifact_accepts_failed_score_only_qa_when_file_eval_passes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "qa-score-only-phase3.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    artifact = repository.create_artifact(
        EvidenceArtifact(
            type="shot_video",
            scope_type="shot",
            scope_id="s1",
            status="validated",
            trust_level="T3",
            content={"version_id": "v1"},
        )
    )
    file_eval = Evaluation(
        evaluator_type="file",
        evaluator_name="video_file_validator",
        evaluator_version="1",
        status="passed",
        hard_gate_passed=True,
        runtime_blocking=True,
        score=100,
    )
    qa_eval = Evaluation(
        evaluator_type="model",
        evaluator_name="video_vlm_qa",
        evaluator_version="1",
        status="failed",
        hard_gate_passed=False,
        evaluation_role="score_only",
        runtime_blocking=False,
        score=20,
        issues=[
            Issue(
                code="VIDEO_QA_CHARACTER_DUPLICATE",
                severity=IssueSeverity.BLOCKER,
                subject="s1",
                message="duplicate character",
            )
        ],
    )

    committed = repository.commit_artifact(
        None, artifact["id"], [file_eval, qa_eval]
    )

    assert committed["status"] == "approved"
    evaluations = repository.get_evaluations(artifact["id"])
    assert len(evaluations) == 2
    model_eval = next(row for row in evaluations if row["evaluator_type"] == "model")
    assert model_eval["status"] == "failed"
    assert model_eval["hard_gate_passed"] == 0
    assert model_eval["evaluation_role"] == "score_only"
    assert model_eval["runtime_blocking"] == 0
