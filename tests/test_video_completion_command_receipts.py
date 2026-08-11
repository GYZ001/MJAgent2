from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import Field

from app import video_command_operations as operations
from app.capabilities.bus import CommandBus
from app.capabilities.registry import CapabilityRegistry, CommandSpec
from app.capabilities.schemas import (
    CommandResult,
    CommandStatus,
    ConfirmationPolicy,
    IdempotencyPolicy,
    RiskLevel,
    StandardCommandInput,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _full_conn() -> sqlite3.Connection:
    from app import db

    conn = _conn()
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('project-1','P',0)")
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,status,storyboard_artifact_id,created_at
           ) VALUES('ep-1','project-1',1,'confirmed','board-1',0)"""
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s,characters,dialogues)
           VALUES('shot-1','ep-1',1,5,'[]','[]')"""
    )
    conn.commit()
    return conn


def test_prepared_episode_completion_restarts_exact_durable_run(monkeypatch) -> None:
    from app.domain import video_ops

    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE episodes(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            active_video_run_id TEXT
        );
        CREATE TABLE workflow_runs(
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            workflow_type TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL
        );
        INSERT INTO episodes VALUES('ep-1','project-1','run-exact');
        INSERT INTO workflow_runs VALUES(
            'run-exact','CREATED','episode_video_completion','episode','ep-1'
        );
        """
    )
    monkeypatch.setattr(operations, "get_conn", lambda: conn)
    monkeypatch.setattr(video_ops, "get_conn", lambda: conn)
    owner, _ = operations.claim_video_command_operation(
        command="video.complete_episode",
        idempotency_key="completion-once",
        request_fingerprint="fp-exact",
        scope_type="episode",
        scope_id="ep-1",
    )
    assert owner
    exact_result = {
        "status": "accepted",
        "run_id": "run-exact",
        "completion_grant_id": "grant-exact",
    }
    operations.bind_video_command_operation(
        command="video.complete_episode",
        idempotency_key="completion-once",
        request_fingerprint="fp-exact",
        claim_token=owner,
        binding={
            "operation_complete": False,
            "phase": "durable_run_installed",
            "run_id": "run-exact",
            "result": exact_result,
            "spawn": {
                "episode_id": "ep-1",
                "project_id": "project-1",
                "grant_id": "grant-exact",
                "resume": False,
                "budget_cap_cny": 100,
                "wall_clock_cap_s": 3600,
                "allow_fallback_adopt": False,
                "max_fallback_shots": 0,
                "allow_storyboard_edit": False,
            },
        },
        conn=conn,
    )
    conn.execute("UPDATE video_command_operation_receipts SET lease_expires_at=0")
    conn.commit()

    replacement_owner, prepared = operations.claim_video_command_operation(
        command="video.complete_episode",
        idempotency_key="completion-once",
        request_fingerprint="fp-exact",
        scope_type="episode",
        scope_id="ep-1",
    )
    assert replacement_owner
    assert prepared and prepared["_resume_prepared"] is True

    spawned: list[tuple[str, str, str | None]] = []

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.append((kind, key, project_id))
        coro.close()
        return object()

    monkeypatch.setattr(video_ops.task_registry, "active", lambda *_: False)
    monkeypatch.setattr(video_ops.task_registry, "spawn", fake_spawn)
    recovered = video_ops._resume_prepared_complete_episode_operation(
        "ep-1",
        {
            "operation_command": "video.complete_episode",
            "idempotency_key": "completion-once",
            "operation_request_fingerprint": "fp-exact",
            "operation_claim_token": replacement_owner,
        },
        prepared,
    )

    assert recovered == exact_result
    assert spawned == [("video_completion", "ep-1", "project-1")]
    binding = json.loads(conn.execute(
        "SELECT binding_json FROM video_command_operation_receipts"
    ).fetchone()[0])
    assert binding["phase"] == "spawn_registered"
    assert binding["operation_complete"] is True
    assert binding["run_id"] == "run-exact"


def test_definitely_not_started_completion_never_promotes_to_success(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(operations, "get_conn", lambda: conn)
    owner, _ = operations.claim_video_command_operation(
        command="video.complete_episode",
        idempotency_key="spawn-failed",
        request_fingerprint="fp-failed",
        scope_type="episode",
        scope_id="ep-1",
    )
    assert owner
    operations.bind_video_command_operation(
        command="video.complete_episode",
        idempotency_key="spawn-failed",
        request_fingerprint="fp-failed",
        claim_token=owner,
        binding={
            "operation_complete": False,
            "operation_failed": True,
            "phase": "definitely_not_started",
            "failure_code": "VIDEO_COMPLETION_START_FAILED",
            "failure_message": "spawn failed",
        },
        conn=conn,
    )
    conn.execute(
        "UPDATE video_command_operation_receipts SET status='failed',lease_expires_at=0"
    )
    conn.commit()

    with pytest.raises(operations.VideoCommandOperationFailed) as failed:
        operations.claim_video_command_operation(
            command="video.complete_episode",
            idempotency_key="spawn-failed",
            request_fingerprint="fp-failed",
            scope_type="episode",
            scope_id="ep-1",
        )
    assert failed.value.error_code == "VIDEO_COMPLETION_START_FAILED"
    assert conn.execute(
        "SELECT status FROM video_command_operation_receipts"
    ).fetchone()[0] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("installed_before_crash", [False, True])
async def test_completion_reuses_exact_run_across_pre_binding_crash(
    monkeypatch,
    installed_before_crash: bool,
) -> None:
    """No second workflow may appear around create/install/receipt crash windows."""
    from app import completion_grant
    from app.domain import common as domain_common
    from app.domain import video_ops
    from app.orchestration.engine import fingerprint

    conn = _full_conn()
    monkeypatch.setattr(operations, "get_conn", lambda: conn)
    monkeypatch.setattr(video_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(domain_common, "get_conn", lambda: conn)
    monkeypatch.setattr(video_ops, "_assert_storyboard_generation_gate", lambda *_: None)
    monkeypatch.setattr(video_ops.task_registry, "active", lambda *_: False)
    spawned: list[str] = []

    def fake_spawn(_kind, key, coro, *, project_id=None):
        assert project_id == "project-1"
        spawned.append(key)
        coro.close()
        return object()

    monkeypatch.setattr(video_ops.task_registry, "spawn", fake_spawn)
    grant = SimpleNamespace(
        grant_id="grant-exact",
        budget_cap_cny=100.0,
        wall_clock_cap_s=3600.0,
        max_fallback_shots=0,
    )
    monkeypatch.setattr(
        completion_grant,
        "issue_video_completion_grant",
        lambda **_kwargs: (grant, None),
    )
    owner, _ = operations.claim_video_command_operation(
        command="video.complete_episode",
        idempotency_key="strict-op",
        request_fingerprint="fp-strict",
        scope_type="episode",
        scope_id="ep-1",
    )
    assert owner
    policy = {
        "operation_key": "video.complete_episode:strict-op",
        "operation_request_fingerprint": "fp-strict",
    }
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               policy_snapshot_json,updated_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "run-exact",
            "episode_video_completion",
            "episode",
            "ep-1",
            "CREATED",
            fingerprint("board-1", "grant-exact", "fresh"),
            json.dumps(policy),
            1,
        ),
    )
    conn.execute(
        "UPDATE episodes SET active_video_run_id=? WHERE id='ep-1'",
        (
            "run-exact"
            if installed_before_crash
            else "starting:1:pre-binding-crash",
        ),
    )
    conn.commit()

    result = await video_ops._complete_episode_core(
        "ep-1",
        {
            "mode": "fresh",
            "idempotency_key": "strict-op",
            "operation_request_fingerprint": "fp-strict",
            "operation_claim_token": owner,
            "operation_command": "video.complete_episode",
        },
    )

    assert result["run_id"] == "run-exact"
    assert spawned == ["ep-1"]
    assert conn.execute(
        """SELECT COUNT(*) FROM workflow_runs
             WHERE workflow_type='episode_video_completion' AND scope_id='ep-1'"""
    ).fetchone()[0] == 1
    receipt = conn.execute(
        "SELECT status,binding_json FROM video_command_operation_receipts"
    ).fetchone()
    assert receipt["status"] == "running"
    assert json.loads(receipt["binding_json"])["operation_complete"] is True


class _PollInput(StandardCommandInput):
    episode_id: str = Field(min_length=1)


@pytest.mark.asyncio
async def test_bus_does_not_cache_domain_in_progress_as_terminal(monkeypatch, tmp_path) -> None:
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bus-domain-receipt.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    finished = False
    calls = 0

    async def handler(_args):
        nonlocal calls
        calls += 1
        if not finished:
            return CommandResult(
                status=CommandStatus.SUCCEEDED,
                summary="domain operation in progress",
                data={"idempotency_in_progress": True},
            )
        return CommandResult(
            status=CommandStatus.SUCCEEDED,
            summary="exact result",
            data={"run_id": "run-exact"},
        )

    registry = CapabilityRegistry()
    registry.register_command(CommandSpec(
        name="video.complete_episode",
        version="1",
        title="test",
        description="test",
        input_model=_PollInput,
        risk=RiskLevel.R0_READ,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes=frozenset(),
        side_effect="test",
        handler=handler,
    ))
    bus = CommandBus(registry)
    args = {"episode_id": "ep-1", "idempotency_key": "same-operation"}

    first = await bus.execute_async("video.complete_episode", args)
    assert first.data["idempotency_in_progress"] is True
    assert db.get_conn().execute(
        "SELECT COUNT(*) FROM command_idempotency"
    ).fetchone()[0] == 0

    finished = True
    second = await bus.execute_async("video.complete_episode", args)
    third = await bus.execute_async("video.complete_episode", args)
    assert second.data == third.data == {"run_id": "run-exact"}
    assert calls == 2
