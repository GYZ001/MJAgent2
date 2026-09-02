from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app import api, db
from tests.conftest import patch_video_supervisor_everywhere, patch_api_everywhere


def _conn(child_statuses: dict[str, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    for episode_no, episode_id in enumerate(child_statuses, start=1):
        conn.execute(
            """INSERT INTO episodes(
                   id,project_id,episode_no,status,storyboard_artifact_id,created_at
               ) VALUES(?, 'p', ?, 'confirmed', ?, 0)""",
            (episode_id, episode_no, f"board-{episode_id}"),
        )
        conn.execute(
            """INSERT INTO workflow_runs(
                   id,workflow_type,scope_type,scope_id,status,input_fingerprint,
                   updated_at,failure_code,failure_message
               ) VALUES(?, 'episode_video_completion', 'episode', ?, ?, 'child', 1, ?, ?)""",
            (
                f"run-{episode_id}",
                episode_id,
                child_statuses[episode_id],
                "CHILD_RESULT" if child_statuses[episode_id] != "SUCCEEDED" else None,
                f"child {child_statuses[episode_id].lower()}",
            ),
        )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES(
               'run-project','project_video_completion_queue','project','p',
               'CREATED','parent',1
           )"""
    )
    conn.commit()
    return conn


def _patch_queue_dependencies(monkeypatch, conn: sqlite3.Connection) -> None:
    import app.evidence.repository as evidence_repository
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine

    for module in (
        evidence_repository,
        orchestration_engine,
        state_machine,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    patch_video_supervisor_everywhere(
        monkeypatch,
        "rebuild_coverage_ledger",
        lambda _episode_id: SimpleNamespace(covered_within_quota=lambda: False),
    )

    async def fake_complete(episode_id, _body):
        return {
            "run_id": f"run-{episode_id}",
            "completion_grant_id": f"grant-{episode_id}",
        }

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)


def _state(episode_ids: list[str]) -> dict:
    return {
        "wall_clock_cap_s": 3600,
        "allow_fallback_adopt": True,
        "allow_storyboard_edit": False,
        "plan": [
            {
                "episode_id": episode_id,
                "episode_no": episode_no,
                "status": "queued",
            }
            for episode_no, episode_id in enumerate(episode_ids, start=1)
        ],
    }


@pytest.mark.asyncio
async def test_project_video_queue_processes_failed_and_queued_siblings(
    monkeypatch,
) -> None:
    """成本预算拦截体系退场（2026-09-01）：跨集队列不再按全局/单集金额上限
    跳过集——``_project_video_spent``/``skipped_budget``/``allocated_cny``
    已随 A3 整体删除。一集失败不影响同批次其它集正常派发。"""
    from app.orchestration.engine import WorkflowRecorder

    conn = _conn({"e1": "FAILED", "e2": "SUCCEEDED"})
    _patch_queue_dependencies(monkeypatch, conn)
    completions: list[str] = []

    async def capture_complete(episode_id: str, _body: dict) -> dict:
        completions.append(episode_id)
        return {"run_id": f"run-{episode_id}", "completion_grant_id": f"grant-{episode_id}"}

    patch_api_everywhere(monkeypatch, "_complete_episode_core", capture_complete)
    state = _state(["e1", "e2"])
    state["plan"][0]["status"] = "failed"

    await api._run_project_video_completion_queue(
        "p",
        state,
        WorkflowRecorder("run-project"),
    )

    assert state["plan"][1]["status"] == "success"
    assert completions == ["e2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_status", "item_status", "parent_status"),
    [
        ("SUCCEEDED", "success", "SUCCEEDED"),
        ("PARTIAL", "partial", "PARTIAL"),
        ("FAILED", "failed", "FAILED"),
        ("WAITING_AUTHORIZATION", "waiting", "WAITING_AUTHORIZATION"),
        ("CANCELLED", "cancelled", "CANCELLED"),
    ],
)
async def test_project_video_queue_propagates_authoritative_child_status(
    monkeypatch,
    child_status: str,
    item_status: str,
    parent_status: str,
) -> None:
    from app.orchestration.engine import WorkflowRecorder

    conn = _conn({"e1": child_status})
    _patch_queue_dependencies(monkeypatch, conn)
    state = _state(["e1"])

    await api._run_project_video_completion_queue(
        "p",
        state,
        WorkflowRecorder("run-project"),
    )

    assert state["plan"][0]["status"] == item_status
    assert state["plan"][0]["child_run_status"] == child_status
    parent = conn.execute(
        "SELECT status FROM workflow_runs WHERE id='run-project'"
    ).fetchone()
    assert parent["status"] == parent_status


@pytest.mark.asyncio
async def test_project_video_queue_does_not_hide_failed_subset(monkeypatch) -> None:
    from app.orchestration.engine import WorkflowRecorder

    conn = _conn({"e1": "SUCCEEDED", "e2": "FAILED"})
    _patch_queue_dependencies(monkeypatch, conn)
    state = _state(["e1", "e2"])

    await api._run_project_video_completion_queue(
        "p",
        state,
        WorkflowRecorder("run-project"),
    )

    assert [item["status"] for item in state["plan"]] == ["success", "failed"]
    parent = conn.execute(
        "SELECT status,failure_code FROM workflow_runs WHERE id='run-project'"
    ).fetchone()
    assert parent["status"] == "PARTIAL"
    assert parent["failure_code"] == "PARTIAL_RESULT"


@pytest.mark.asyncio
async def test_project_video_queue_follows_recovered_child_run(monkeypatch) -> None:
    from app.orchestration.engine import WorkflowRecorder

    conn = _conn({"e1": "PAUSED_EXTERNAL"})
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,parent_run_id,status,
               input_fingerprint,updated_at,failure_code,failure_message
           ) VALUES(
               'run-e1-recovered','episode_video_completion','episode','e1',
               'run-e1','FAILED','recovered',2,'RECOVERY_FAILED','recovery failed'
           )"""
    )
    conn.execute(
        "UPDATE workflow_runs SET recovered_by_run_id='run-e1-recovered' WHERE id='run-e1'"
    )
    conn.commit()
    _patch_queue_dependencies(monkeypatch, conn)
    state = _state(["e1"])

    await api._run_project_video_completion_queue(
        "p",
        state,
        WorkflowRecorder("run-project"),
    )

    assert state["plan"][0]["run_id"] == "run-e1-recovered"
    assert state["plan"][0]["status"] == "failed"
    assert conn.execute(
        "SELECT status FROM workflow_runs WHERE id='run-project'"
    ).fetchone()["status"] == "FAILED"


@pytest.mark.parametrize("item_status", ["partial", "cancelled"])
def test_project_video_queue_retry_requeues_non_successful_child(
    monkeypatch,
    item_status: str,
) -> None:
    import app.evidence.repository as evidence_repository
    import app.orchestration.api as orchestration_api
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine

    conn = _conn({"e1": "PARTIAL"})
    state = _state(["e1"])
    state["plan"][0].update({
        "status": item_status,
        "run_id": "run-e1",
        "completion_grant_id": "grant-e1",
        "child_run_status": item_status.upper(),
        "child_failure_code": "OLD_RESULT",
        "child_message": "old result",
        "error": "old error",
    })
    conn.execute(
        """UPDATE workflow_runs
           SET status=?,config_snapshot_json=?
           WHERE id='run-project'""",
        (
            "PARTIAL" if item_status == "partial" else "CANCELLED",
            json.dumps({"queue_state": state}),
        ),
    )
    conn.commit()
    for module in (
        evidence_repository,
        orchestration_api,
        orchestration_engine,
        state_machine,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(
        orchestration_api.task_registry,
        "active",
        lambda *_args: False,
    )

    def capture_spawn(_kind, _key, coro, *, project_id=None):
        assert project_id == "p"
        coro.close()

    monkeypatch.setattr(orchestration_api.task_registry, "spawn", capture_spawn)

    recovered = orchestration_api._restart_project_video_queue_run(
        "run-project",
        "retry",
    )

    retry_state = recovered["config_snapshot"]["queue_state"]
    retry_item = retry_state["plan"][0]
    assert retry_item["status"] == "queued"
    assert not {
        "run_id",
        "completion_grant_id",
        "child_run_status",
        "child_failure_code",
        "child_message",
        "error",
    }.intersection(retry_item)
