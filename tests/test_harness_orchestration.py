from __future__ import annotations

import asyncio
import pytest

from app import db
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.orchestration.engine import WorkflowRecorder, fingerprint
from app.orchestration.state_machine import StateConflict, transition_run
from app.observability.tracing import bind_trace, current_trace, detached_trace


def _fresh_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return db.get_conn()


def test_detached_trace_does_not_inherit_parent_workflow() -> None:
    with bind_trace("run-parent", "step-parent", "trace-parent"):
        assert current_trace().run_id == "run-parent"
        with detached_trace():
            assert current_trace().run_id is None
            assert current_trace().step_run_id is None
        assert current_trace().run_id == "run-parent"


def test_workflow_recorder_persists_trace_evidence_and_commit(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    recorder = WorkflowRecorder.create(
        workflow_type="test_workflow",
        scope_type="project",
        scope_id="p1",
        input_fingerprint=fingerprint("p1", {"version": 1}),
    )
    recorder.start()

    async def operation() -> str:
        db.log_provider_call("text", "fake-model", "DONE", 200, 12)
        return "candidate"

    step_id, result = asyncio.run(
        recorder.step("screenplay", operation, contract_key="screenplay", agent_name="test")
    )
    assert result == "candidate"
    call = conn.execute(
        "SELECT run_id, step_run_id, trace_id FROM provider_calls"
    ).fetchone()
    assert call["run_id"] == recorder.run_id
    assert call["step_run_id"] == step_id
    assert call["trace_id"].startswith("trace_")

    artifact = recorder.artifact(
        step_id,
        EvidenceArtifact(
            type="episode_screenplay",
            scope_type="episode",
            scope_id="e1",
            status="validated",
            trust_level="T2",
            content={"episode_no": 1, "beats": []},
            contract_version="1.0.0",
        ),
    )
    committed = repository.commit_artifact(
        step_id,
        artifact["id"],
        [
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="schema",
                evaluator_version="1",
                status="passed",
                hard_gate_passed=True,
                score=100,
            )
        ],
    )
    recorder.succeed()

    assert committed["status"] == "approved"
    assert committed["trust_level"] == "T2"
    assert repository.get_run(recorder.run_id)["status"] == "SUCCEEDED"
    assert {event["event_type"] for event in repository.get_events(recorder.run_id)} >= {
        "RUN_CREATED", "RUN_STARTED", "STEP_STARTED", "ARTIFACT_CREATED",
        "ARTIFACT_COMMITTED", "RUN_SUCCEEDED",
    }


def test_workflow_recorder_process_shutdown_remains_recoverable(tmp_path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    recorder = WorkflowRecorder.create(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input",
    )
    recorder.start()

    recorder.pause_external("服务重启，等待续跑")

    run = repository.get_run(recorder.run_id)
    assert run["status"] == "PAUSED_EXTERNAL"
    assert run["failure_code"] == "SERVICE_RESTART"
    assert "等待续跑" in run["failure_message"]
    assert "RUN_PAUSED_EXTERNAL" in {
        event["event_type"] for event in repository.get_events(recorder.run_id)
    }


def test_workflow_recorder_persists_deterministic_failed_result(tmp_path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    recorder = WorkflowRecorder.create(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="zero-completed-shots",
    )
    recorder.start()

    recorder.fail_result(
        "PARTIAL_NO_USABLE_CANDIDATE",
        failure_code="NO_COMPLETED_OUTPUT",
    )

    run = repository.get_run(recorder.run_id)
    assert run["status"] == "FAILED"
    assert run["failure_code"] == "NO_COMPLETED_OUTPUT"
    assert run["failure_message"] == "PARTIAL_NO_USABLE_CANDIDATE"


def test_commit_rejects_blockers_and_recovered_evidence(tmp_path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    recorder = WorkflowRecorder.create(
        workflow_type="test", scope_type="episode", scope_id="e1", input_fingerprint="x"
    )
    step_id = repository.create_step(recorder.run_id, "screenplay")
    artifact = repository.create_artifact(
        EvidenceArtifact(
            type="episode_screenplay",
            scope_type="episode",
            scope_id="e1",
            status="candidate",
            trust_level="T0",
            content={"episode_no": 1},
        ),
        step_run_id=step_id,
    )
    blocker = Issue(
        code="SOURCE_MISSING",
        severity=IssueSeverity.BLOCKER,
        subject="episode:1",
        message="missing source",
        repairable=True,
    )
    with pytest.raises(ValueError, match="blocker"):
        repository.commit_artifact(
            step_id,
            artifact["id"],
            [
                Evaluation(
                    evaluator_type="deterministic",
                    evaluator_name="source",
                    evaluator_version="1",
                    status="passed",
                    hard_gate_passed=True,
                    issues=[blocker],
                )
            ],
        )
    with pytest.raises(ValueError, match="recovered"):
        repository.commit_artifact(
            step_id,
            artifact["id"],
            [
                Evaluation(
                    evaluator_type="model",
                    evaluator_name="critic",
                    evaluator_version="1",
                    status="warning",
                    hard_gate_passed=True,
                    recovered=True,
                )
            ],
        )


def test_artifact_invalidation_propagates_through_lineage(tmp_path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    root = repository.create_artifact(
        EvidenceArtifact(
            type="character_bible", scope_type="project", scope_id="p1",
            status="approved", trust_level="T4", content={"version": 1},
        )
    )
    child = repository.create_artifact(
        EvidenceArtifact(
            type="episode_screenplay", scope_type="episode", scope_id="e1",
            status="validated", trust_level="T2", content={"version": 1},
            parent_artifact_ids=[root["id"]],
        )
    )
    grandchild = repository.create_artifact(
        EvidenceArtifact(
            type="storyboard", scope_type="episode", scope_id="e1",
            status="validated", trust_level="T2", content={"shots": []},
            parent_artifact_ids=[child["id"]],
        )
    )

    stale = repository.invalidate_descendants(root["id"], "bible changed")

    assert set(stale) == {child["id"], grandchild["id"]}
    assert repository.get_artifact(child["id"])["status"] == "stale"
    assert repository.get_artifact(grandchild["id"])["stale_reason"] == "bible changed"


def test_superseding_artifact_does_not_invalidate_its_own_replacement_lineage(
    tmp_path, monkeypatch
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    old = repository.create_artifact(
        EvidenceArtifact(
            type="episode_screenplay", scope_type="episode", scope_id="e1",
            status="approved", trust_level="T4", content={"version": 1},
        )
    )
    replacement = repository.create_artifact(
        EvidenceArtifact(
            type="episode_screenplay", scope_type="episode", scope_id="e1",
            status="validated", trust_level="T2", content={"version": 2},
            parent_artifact_ids=[old["id"]],
        )
    )

    committed = repository.commit_artifact(
        None,
        replacement["id"],
        [
            Evaluation(
                evaluator_type="human",
                evaluator_name="editor",
                evaluator_version="1",
                status="passed",
                hard_gate_passed=True,
            )
        ],
    )

    assert committed["status"] == "approved"
    assert repository.get_artifact(old["id"])["status"] == "superseded"


def test_state_machine_uses_compare_and_set_and_restart_is_explicit(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    recorder = WorkflowRecorder.create(
        workflow_type="test", scope_type="project", scope_id="p1", input_fingerprint="x"
    )
    recorder.start()
    with pytest.raises(StateConflict):
        transition_run(recorder.run_id, "CREATED", "CANCELLED", "stale writer")

    db.init_db(reconcile_interrupted=True)
    run = conn.execute(
        "SELECT status, failure_code, resume_from_step FROM workflow_runs WHERE id=?",
        (recorder.run_id,),
    ).fetchone()
    assert run["status"] == "PAUSED_EXTERNAL"
    assert run["failure_code"] == "SERVICE_RESTART"
