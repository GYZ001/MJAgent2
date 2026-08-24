from __future__ import annotations

import asyncio
import pytest

from app import db, task_registry
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.orchestration import api as orchestration_api
from app.orchestration import engine
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


@pytest.mark.asyncio
async def test_workflow_recorder_step_uses_async_write_paths(tmp_path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    recorder = WorkflowRecorder.create(
        workflow_type="test_workflow",
        scope_type="project",
        scope_id="p1",
        input_fingerprint="async-step",
    )
    recorder.start()
    write_transactions = 0
    real_run_write_transaction = engine.run_write_transaction

    async def tracked_run_write_transaction(operation, **kwargs):
        nonlocal write_transactions
        write_transactions += 1
        return await real_run_write_transaction(operation, **kwargs)

    def reject_sync_event(*_args, **_kwargs):
        raise AssertionError("WorkflowRecorder.step used synchronous append_event")

    monkeypatch.setattr(engine, "run_write_transaction", tracked_run_write_transaction)
    monkeypatch.setattr(repository, "append_event", reject_sync_event)

    step_id, result = await recorder.step("screenplay", lambda: asyncio.sleep(0, result="ok"))

    assert result == "ok"
    assert write_transactions == 2
    assert repository.get_steps(recorder.run_id)[0]["status"] == "SUCCEEDED"
    assert [
        event["event_type"] for event in repository.get_events(recorder.run_id)
    ][-2:] == ["STEP_STARTED", "STEP_SUCCEEDED"]
    assert step_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_event"),
    [
        (RuntimeError("failed"), "FAILED", "STEP_FAILED"),
        (asyncio.CancelledError(), "CANCELLED", "STEP_CANCELLED"),
    ],
)
async def test_workflow_recorder_step_persists_async_terminal_errors(
    tmp_path,
    monkeypatch,
    error: BaseException,
    expected_status: str,
    expected_event: str,
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    recorder = WorkflowRecorder.create(
        workflow_type="test_workflow",
        scope_type="project",
        scope_id="p1",
        input_fingerprint=expected_status,
    )
    recorder.start()

    async def operation() -> None:
        raise error

    with pytest.raises(type(error)):
        await recorder.step("screenplay", operation)

    assert repository.get_steps(recorder.run_id)[0]["status"] == expected_status
    assert repository.get_events(recorder.run_id)[-1]["event_type"] == expected_event


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


def _spawn_placeholder_task(kind: str, key: str, run_id: str) -> asyncio.Event:
    """挂一个进程内占位任务，代表"当前真正在跑"的续跑/首跑任务。

    真实的剧本/人物谱协程（如 ``_recorded_bible_task``）在收到
    ``asyncio.CancelledError`` 时会调用 ``WorkflowRecorder(run_id).cancel()``
    把 run 落到 CANCELLED 再重新抛出；这里照抄同一收尾方式，让占位任务在被
    ``task_registry.cancel_and_wait`` 真正杀掉时产生和生产代码一致的可观察效果。
    """
    finished = asyncio.Event()

    async def background() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            WorkflowRecorder(run_id).cancel("占位任务收到取消")
            raise
        finally:
            finished.set()

    task_registry.spawn(kind, key, background())
    return finished


@pytest.mark.asyncio
async def test_cancel_run_screenplay_current_owner_cancels_live_task(tmp_path, monkeypatch) -> None:
    """回归保护：run_id 仍是 episode 当前 owner 时，cancel_run 必须照常杀掉真任务。"""
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',?)",
        (db.now(),),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, screenplay_status, status, created_at) "
        "VALUES('e1','p1',1,'第一集','running','planned',?)",
        (db.now(),),
    )
    run_id = repository.create_run(
        workflow_type="screenplay", scope_type="episode", scope_id="e1",
        input_fingerprint=fingerprint("e1", 1),
    )
    conn.execute("UPDATE workflow_runs SET status='RUNNING' WHERE id=?", (run_id,))
    conn.execute("UPDATE episodes SET active_screenplay_run_id=? WHERE id='e1'", (run_id,))
    conn.commit()

    finished = _spawn_placeholder_task("screenplay", "e1", run_id)
    await asyncio.sleep(0)
    assert task_registry.active("screenplay", "e1") is True

    result = await orchestration_api.cancel_run(run_id)

    assert result["cancelled"] is True
    assert "superseded" not in result
    assert result["run"]["id"] == run_id
    assert result["run"]["status"] == "CANCELLED"
    assert finished.is_set()
    assert task_registry.active("screenplay", "e1") is False


@pytest.mark.asyncio
async def test_cancel_run_screenplay_superseded_parent_only_finalizes_itself(tmp_path, monkeypatch) -> None:
    """回归 run_bd99195814b3 / run_72cb62402213 现场：拿着已被续跑取代的父 run_id
    发起取消，绝不能按 scope 误杀当前续跑任务；只终态化这条历史记录自身，
    返回体要能看出这是"取代"而不是真的取消了在跑任务。"""
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',?)",
        (db.now(),),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, screenplay_status, status, created_at) "
        "VALUES('e1','p1',1,'第一集','running','planned',?)",
        (db.now(),),
    )
    parent_run_id = repository.create_run(
        workflow_type="screenplay", scope_type="episode", scope_id="e1",
        input_fingerprint=fingerprint("e1", 1),
    )
    conn.execute("UPDATE workflow_runs SET status='PAUSED_EXTERNAL' WHERE id=?", (parent_run_id,))
    conn.commit()
    child_run_id = repository.create_run(
        workflow_type="screenplay", scope_type="episode", scope_id="e1",
        input_fingerprint=fingerprint("e1", 2),
        trigger_type="resume", parent_run_id=parent_run_id,
    )
    conn.execute("UPDATE workflow_runs SET status='RUNNING' WHERE id=?", (child_run_id,))
    conn.execute("UPDATE episodes SET active_screenplay_run_id=? WHERE id='e1'", (child_run_id,))
    conn.commit()

    # child 的进程内任务代表当前真正在跑的续跑
    finished = _spawn_placeholder_task("screenplay", "e1", child_run_id)
    await asyncio.sleep(0)
    assert task_registry.active("screenplay", "e1") is True

    original_cancel_and_wait = task_registry.cancel_and_wait
    spy_calls: list[tuple[str, str]] = []

    async def spy_cancel_and_wait(kind, key):
        spy_calls.append((kind, key))
        return await original_cancel_and_wait(kind, key)

    monkeypatch.setattr(task_registry, "cancel_and_wait", spy_cancel_and_wait)

    result = await orchestration_api.cancel_run(parent_run_id)

    assert spy_calls == []  # 绝不能按 scope 撤销，否则会误杀 child 的进程内任务
    assert result["cancelled"] is True
    assert result["superseded"] is True
    assert result["active_run_id"] == child_run_id
    assert result["run"]["id"] == parent_run_id
    assert result["run"]["status"] == "CANCELLED"

    # 当前 owner 的任务必须原封不动地存活
    assert task_registry.active("screenplay", "e1") is True
    assert not finished.is_set()
    assert repository.get_run(child_run_id)["status"] == "RUNNING"

    # 收尾，避免占位任务泄漏到下一个测试
    assert await task_registry.cancel_and_wait("screenplay", "e1") is True


@pytest.mark.asyncio
async def test_cancel_run_character_bible_current_owner_cancels_live_task(tmp_path, monkeypatch) -> None:
    """回归保护：character_bible 分支同样不能因为所有权校验而废掉正常取消。"""
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_status, created_at) "
        "VALUES('p1','demo','created','running',?)",
        (db.now(),),
    )
    conn.commit()
    run_id = repository.create_run(
        workflow_type="character_bible", scope_type="project", scope_id="p1",
        input_fingerprint=fingerprint("p1", 1),
    )
    conn.execute("UPDATE workflow_runs SET status='RUNNING' WHERE id=?", (run_id,))
    conn.commit()

    finished = _spawn_placeholder_task("bible", "p1", run_id)
    await asyncio.sleep(0)
    assert task_registry.active("bible", "p1") is True

    result = await orchestration_api.cancel_run(run_id)

    assert result["cancelled"] is True
    assert "superseded" not in result
    assert result["run"]["id"] == run_id
    assert result["run"]["status"] == "CANCELLED"
    assert finished.is_set()
    assert task_registry.active("bible", "p1") is False


@pytest.mark.asyncio
async def test_cancel_run_character_bible_superseded_parent_only_finalizes_itself(
    tmp_path, monkeypatch,
) -> None:
    """character_bible 没有 active_screenplay_run_id 那样的专属指针字段，但
    recover_bible_tasks 续跑时会通过 parent_run_id 把 recovered_by_run_id 打到旧
    run 上；这条信号足以判断旧 run 是否还对应进程内任务。"""
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_status, created_at) "
        "VALUES('p1','demo','created','running',?)",
        (db.now(),),
    )
    conn.commit()
    parent_run_id = repository.create_run(
        workflow_type="character_bible", scope_type="project", scope_id="p1",
        input_fingerprint=fingerprint("p1", 1),
    )
    conn.execute("UPDATE workflow_runs SET status='PAUSED_EXTERNAL' WHERE id=?", (parent_run_id,))
    conn.commit()
    child_run_id = repository.create_run(
        workflow_type="character_bible", scope_type="project", scope_id="p1",
        input_fingerprint=fingerprint("p1", 2),
        trigger_type="resume", parent_run_id=parent_run_id,
    )
    conn.execute("UPDATE workflow_runs SET status='RUNNING' WHERE id=?", (child_run_id,))
    conn.commit()
    assert repository.get_run(parent_run_id)["recovered_by_run_id"] == child_run_id

    finished = _spawn_placeholder_task("bible", "p1", child_run_id)
    await asyncio.sleep(0)
    assert task_registry.active("bible", "p1") is True

    original_cancel_and_wait = task_registry.cancel_and_wait
    spy_calls: list[tuple[str, str]] = []

    async def spy_cancel_and_wait(kind, key):
        spy_calls.append((kind, key))
        return await original_cancel_and_wait(kind, key)

    monkeypatch.setattr(task_registry, "cancel_and_wait", spy_cancel_and_wait)

    result = await orchestration_api.cancel_run(parent_run_id)

    assert spy_calls == []
    assert result["cancelled"] is True
    assert result["superseded"] is True
    assert result["active_run_id"] == child_run_id
    assert result["run"]["id"] == parent_run_id
    assert result["run"]["status"] == "CANCELLED"

    assert task_registry.active("bible", "p1") is True
    assert not finished.is_set()
    assert repository.get_run(child_run_id)["status"] == "RUNNING"

    assert await task_registry.cancel_and_wait("bible", "p1") is True
