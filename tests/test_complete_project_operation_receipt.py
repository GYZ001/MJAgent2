from __future__ import annotations

import json

import pytest

from app import api, db
from app import video_command_operations as operations


class _ProcessCrash(BaseException):
    """Simulate process loss without running the core's Exception cleanup."""


@pytest.fixture
def project_db(tmp_path, monkeypatch):
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "complete-project-receipt.db")
    db._local.conn = None
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p','P','created',1)"
    )
    for episode_no in (1, 2):
        conn.execute(
            """INSERT INTO episodes(
                   id,project_id,episode_no,title,status,storyboard_artifact_id,created_at
               ) VALUES(?, 'p', ?, ?, 'confirmed', ?, 1)""",
            (
                f"e{episode_no}",
                episode_no,
                f"Episode {episode_no}",
                f"storyboard-{episode_no}",
            ),
        )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(operations, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "_project_video_spent", lambda *_args, **_kwargs: 0.0)

    import app.video_supervisor as video_supervisor

    monkeypatch.setattr(
        video_supervisor,
        "rebuild_coverage_ledger",
        lambda _episode_id: type(
            "Ledger", (), {"covered_within_quota": lambda self: False}
        )(),
    )
    yield conn


def _claim_project() -> tuple[str, dict]:
    owner, recovered = operations.claim_video_command_operation(
        command="video.complete_project",
        idempotency_key="project-op",
        request_fingerprint="project-fp",
        scope_type="project",
        scope_id="p",
    )
    assert owner is not None
    assert recovered is None
    return owner, {
        "episode_ids": ["e1", "e2"],
        "global_budget_cap_cny": 100,
        "per_episode_cap_cny": 40,
        "wall_clock_cap_s": 3600,
        "allow_fallback_adopt": True,
        "allow_storyboard_edit": False,
        "idempotency_key": "project-op",
        "operation_request_fingerprint": "project-fp",
        "operation_claim_token": owner,
        "operation_command": "video.complete_project",
    }


def _expire_receipts(conn) -> None:
    conn.execute("UPDATE video_command_operation_receipts SET lease_expires_at=0")
    conn.commit()


@pytest.mark.asyncio
async def test_project_receipt_recovers_exact_first_episode_after_process_loss(
    project_db,
    monkeypatch,
) -> None:
    owner, body = _claim_project()
    body["episode_ids"] = ["e1"]
    exact_child_result = {
        "status": "accepted",
        "run_id": "run-e1-exact",
        "completion_grant_id": "grant-e1-exact",
        "resource_uri": "manju://runs/run-e1-exact",
        "poll_url": "/api/episodes/e1/video-completion",
    }
    calls = 0

    async def crash_after_child_commit(episode_id: str, child_body: dict) -> dict:
        nonlocal calls
        calls += 1
        assert episode_id == "e1"
        operations.bind_video_command_operation(
            command=str(child_body["operation_command"]),
            idempotency_key=str(child_body["idempotency_key"]),
            request_fingerprint=str(child_body["operation_request_fingerprint"]),
            claim_token=str(child_body["operation_claim_token"]),
            binding={
                "operation_complete": True,
                "run_id": exact_child_result["run_id"],
                "completion_grant_id": exact_child_result["completion_grant_id"],
                "result": exact_child_result,
            },
            conn=project_db,
            merge=True,
        )
        project_db.commit()
        raise _ProcessCrash()

    monkeypatch.setattr(api, "_complete_episode_core", crash_after_child_commit)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)

    with pytest.raises(_ProcessCrash):
        await api._complete_project_videos_core("p", body)

    frozen = operations.read_video_command_operation_binding(
        command="video.complete_project",
        idempotency_key="project-op",
        request_fingerprint="project-fp",
    )
    assert frozen["project_plan"]["eligible_episode_ids"] == ["e1"]
    assert "first_episode" not in frozen

    # Current eligibility and coverage may drift after the first durable start.
    # Replay must use the frozen plan and the child's exact receipt.
    project_db.execute("UPDATE episodes SET status='draft' WHERE id='e1'")
    project_db.commit()
    _expire_receipts(project_db)
    replacement_owner, replacement_body = _claim_project()
    replacement_body.update(body)
    replacement_body["operation_claim_token"] = replacement_owner
    result = await api._complete_project_videos_core("p", replacement_body)

    assert calls == 1
    assert result["started"] == [{
        "episode_id": "e1",
        "episode_no": 1,
        "status": "started",
        "allocated_cny": 40.0,
        "run_id": "run-e1-exact",
        "completion_grant_id": "grant-e1-exact",
    }]

    # A lost response after the core completed is replayed from the project
    # receipt itself; neither current episode state nor the child is consulted.
    _expire_receipts(project_db)
    replay_owner, replay = operations.claim_video_command_operation(
        command="video.complete_project",
        idempotency_key="project-op",
        request_fingerprint="project-fp",
        scope_type="project",
        scope_id="p",
    )
    assert replay_owner is None
    assert replay == result
    assert calls == 1


@pytest.mark.asyncio
async def test_project_receipt_reuses_exact_queue_run_after_spawn_process_loss(
    project_db,
    monkeypatch,
) -> None:
    _owner, body = _claim_project()
    child_calls = 0

    async def complete_child(episode_id: str, _child_body: dict) -> dict:
        nonlocal child_calls
        child_calls += 1
        return {
            "status": "accepted",
            "run_id": f"run-{episode_id}-exact",
            "completion_grant_id": f"grant-{episode_id}-exact",
        }

    monkeypatch.setattr(api, "_complete_episode_core", complete_child)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    spawn_run_ids: list[str] = []

    def crash_first_spawn(_kind, _key, coro, **_kwargs) -> None:
        spawn_run_ids.append(coro.cr_frame.f_locals["recorder"].run_id)
        coro.close()
        if len(spawn_run_ids) == 1:
            raise _ProcessCrash()

    monkeypatch.setattr(api.task_registry, "spawn", crash_first_spawn)

    with pytest.raises(_ProcessCrash):
        await api._complete_project_videos_core("p", body)

    binding = operations.read_video_command_operation_binding(
        command="video.complete_project",
        idempotency_key="project-op",
        request_fingerprint="project-fp",
    )
    exact_queue_run_id = binding["project_queue"]["run_id"]
    assert binding["project_queue"]["phase"] == "created"
    assert project_db.execute(
        """SELECT COUNT(*) FROM workflow_runs
           WHERE workflow_type='project_video_completion_queue'"""
    ).fetchone()[0] == 1

    _expire_receipts(project_db)
    replacement_owner, replacement_body = _claim_project()
    replacement_body.update(body)
    replacement_body["operation_claim_token"] = replacement_owner
    result = await api._complete_project_videos_core("p", replacement_body)

    assert child_calls == 1
    assert spawn_run_ids == [exact_queue_run_id, exact_queue_run_id]
    assert result["project_queue_run_id"] == exact_queue_run_id
    assert result["project_queue_active"] is True
    assert project_db.execute(
        """SELECT COUNT(*) FROM workflow_runs
           WHERE workflow_type='project_video_completion_queue'"""
    ).fetchone()[0] == 1
    stored = project_db.execute(
        """SELECT binding_json FROM video_command_operation_receipts
           WHERE operation_key='video.complete_project:project-op'"""
    ).fetchone()
    assert json.loads(stored["binding_json"])["operation_complete"] is True
