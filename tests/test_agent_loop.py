from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel

from app import api, db
from app.evaluations.issues import issues_from_messages
from app.evidence import repository
from app.harness.context import ContextPack
from app.harness.types import EvidenceArtifact
from app.loops import AgentLoop, AgentLoopPolicy
from app.orchestration.engine import WorkflowRecorder
from app.schemas import EpisodeScreenplay


class Candidate(BaseModel):
    value: int


def _loop(*, allow_warning: bool = False, max_iterations: int = 4) -> AgentLoop[Candidate]:
    return AgentLoop(
        stage_key="screenplay",
        contract_key="screenplay",
        goal="produce a valid candidate",
        scope_type="episode",
        scope_id="e1",
        artifact_type="episode_screenplay",
        policy=AgentLoopPolicy(
            max_iterations=max_iterations,
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=allow_warning,
        ),
    )


def _evaluate(raw: str):
    value = Candidate.model_validate(json.loads(raw))
    messages = [] if value.value >= 2 else [f"value {value.value} is below contract"]
    return value, issues_from_messages(messages, subject="episode:e1")


def test_agent_loop_repairs_then_accepts() -> None:
    outputs = ['{"value": 1}', '{"value": 2}']

    async def producer(iteration, *_args):
        return outputs[iteration - 1]

    result = asyncio.run(_loop().run(producer, _evaluate))

    assert result.status == "accepted"
    assert result.exit_reason == "contract_passed"
    assert result.iterations == 2
    assert result.value.value == 2


def test_agent_loop_stops_same_issue_fingerprint_and_returns_warning() -> None:
    async def producer(_iteration, *_args):
        return '{"value": 1}'

    result = asyncio.run(_loop(allow_warning=True).run(producer, _evaluate))

    assert result.status == "warning"
    assert result.exit_reason == "stalled"
    assert result.iterations == 2
    assert result.issues[0].repairable is True


def test_agent_loop_stops_when_issue_set_changes_without_quality_gain() -> None:
    async def producer(iteration, *_args):
        return json.dumps({"value": -iteration})

    def evaluate(raw: str):
        value = Candidate.model_validate(json.loads(raw))
        return value, issues_from_messages(
            [f"distinct problem {abs(value.value)}"], subject=f"episode:e{abs(value.value)}"
        )

    result = asyncio.run(_loop(allow_warning=True).run(producer, evaluate))

    assert result.exit_reason == "no_quality_gain"
    assert result.iterations == 3


def test_agent_loop_persists_iterations_candidates_and_evaluations(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "agent-loop.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    recorder = WorkflowRecorder.create(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
    )
    recorder.start()
    source_artifact = repository.create_artifact(
        EvidenceArtifact(
            type="novel_source",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T4",
            content={"text": "source"},
        )
    )
    outputs = ['{"value": 1}', '{"value": 2}']

    async def producer(iteration, *_args):
        return outputs[iteration - 1]

    async def operation():
        return await _loop().run(producer, _evaluate)

    _, result = asyncio.run(
        recorder.step(
            "screenplay",
            operation,
            contract_key="screenplay",
            input_artifact_ids=[source_artifact["id"]],
        )
    )
    recorder.succeed()

    steps = repository.get_steps(recorder.run_id)
    iteration_steps = [step for step in steps if step["step_key"] == "screenplay.iteration"]
    artifacts = db.rows_to_dicts(db.get_conn().execute(
        "SELECT * FROM artifacts WHERE scope_id='e1' AND type='episode_screenplay' ORDER BY version"
    ).fetchall())
    evaluations = db.rows_to_dicts(db.get_conn().execute(
        "SELECT * FROM evaluations ORDER BY created_at"
    ).fetchall())

    assert result.artifact_id == artifacts[-1]["id"]
    assert [step["status"] for step in iteration_steps] == ["WARNING", "SUCCEEDED"]
    assert [artifact["trust_level"] for artifact in artifacts] == ["T1", "T2"]
    assert artifacts[-1]["status"] == "approved"
    assert json.loads(artifacts[-1]["parent_artifact_ids_json"]) == [source_artifact["id"]]
    assert len(evaluations) == 2


def test_context_pack_records_hash_and_truncation_without_hiding_it() -> None:
    pack = ContextPack(goal="screenplay")
    selected = pack.add_text(
        "source_text",
        "abcdefghij",
        limit=4,
        source_artifact_id="art_source",
        truncation_strategy="head",
    )

    manifest = pack.manifest()

    assert selected == "abcd"
    assert manifest["items"][0]["source_artifact_id"] == "art_source"
    assert manifest["items"][0]["original_chars"] == 10
    assert manifest["items"][0]["selected_chars"] == 4
    assert manifest["items"][0]["truncated"] is True
    assert len(manifest["items"][0]["content_hash"]) == 64


def test_screenplay_task_persists_warning_candidate_without_marking_ready(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-warning.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) "
        "VALUES('p1','P','planned',NULL,1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) "
        "VALUES('p1',1,'Chapter','source text')"
    )
    conn.execute(
        """INSERT INTO episodes(
            id, project_id, episode_no, title, hook, cliffhanger, synopsis,
            source_chapters, target_duration_s, screenplay_status, status, created_at
        ) VALUES('e1','p1',1,'Episode','','','', '[1]', 50, 'running', 'planned', 1)"""
    )
    conn.commit()
    candidate = EpisodeScreenplay(
        episode_no=1,
        full_script_text="editable warning candidate",
    )
    object.__setattr__(candidate, "residual_errors", ["关键剧情点缺失"])
    object.__setattr__(candidate, "evidence_artifact_id", "art_warning")

    async def fake_generate(*_args, **_kwargs):
        return candidate

    monkeypatch.setattr(api, "generate_screenplay", fake_generate)

    result = asyncio.run(api._screenplay_task("e1"))

    row = conn.execute(
        "SELECT screenplay_status, screenplay_error, screenplay_artifact_id "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result is candidate
    assert row["screenplay_status"] == "warning"
    assert "不能进入分镜" in row["screenplay_error"]
    assert row["screenplay_artifact_id"] == "art_warning"
