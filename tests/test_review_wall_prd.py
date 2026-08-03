from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import api, db, worker


def _published_screenplay_json() -> str:
    from tests.test_screenplay_edit_save import _valid_script

    return _valid_script().model_dump_json()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,status,screenplay_status,
               screenplay_json,
               screenplay_artifact_id,storyboard_artifact_id,
               published_screenplay_artifact_id,published_storyboard_artifact_id,created_at
           ) VALUES('e','p',1,'E','confirmed','ready',?,'screenplay-1','board-1','screenplay-1','board-1',0)""",
        (_published_screenplay_json(),),
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,action_desc,characters,dialogues,
               storyboard_artifact_id
           ) VALUES('s1','e',1,5,'action','[]','[]','board-1')"""
    )
    conn.commit()
    return conn


def test_generation_context_excludes_manual_review_records(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    context = api.review_wall_context("e")
    assert context["upstream"]["eligible_for_production"] is True
    assert "shots" not in context
    assert not hasattr(api, "create_shot_review_item")
    assert not hasattr(api, "update_shot_review_item")
    assert not hasattr(api, "set_shot_review_state")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "shot_review_items" not in tables
    assert "shot_review_states" not in tables


def test_version_archive_is_idempotent_and_audited_once(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v1','s1',1,'p','k','succeeded',1)"""
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    first = api.archive_video_version("v1", {"reason": "保留候选区整洁"})
    repeated = api.archive_video_version("v1", {"reason": "网络重试"})
    first_restore = api.unarchive_video_version("v1")
    repeated_restore = api.unarchive_video_version("v1")

    assert first["idempotent"] is False
    assert repeated["idempotent"] is True
    assert first_restore["idempotent"] is False
    assert repeated_restore["idempotent"] is True
    actions = [
        row["action"] for row in conn.execute(
            "SELECT action FROM review_action_audit ORDER BY created_at"
        ).fetchall()
    ]
    assert actions == ["video_version.archive", "video_version.unarchive"]


def test_positive_actions_fail_closed_for_hard_failed_asset(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('v1','s1',1,'p','k','succeeded',?,0)""",
        (json.dumps({
            "reference_images": [{
                "id": "ref-1",
                "selectedForSeedance": True,
                "qa": {"status": "failed", "hard_failures": ["character_duplicate"]},
            }]
        }),),
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    snapshot = api._review_upstream_snapshot("e")
    assert snapshot["eligible_for_production"] is True
    assert snapshot["assets"]["blockers"] == []
    assert any(
        item["ref_id"] == "ref-1"
        and item["warning"] == "qa_hard_failure:character_duplicate"
        for item in snapshot["assets"]["soft_warnings"]
    )
    assert api._review_assert_positive_action("e")["eligible_for_production"] is True


@pytest.mark.parametrize("value", [-1, 0, float("nan"), float("inf"), 100001])
def test_authorization_numbers_reject_invalid_values(value) -> None:
    with pytest.raises(HTTPException) as rejected:
        api._review_validate_authorization_number(
            value, field="budget_cap_cny", minimum=1, maximum=100000,
        )
    assert rejected.value.status_code == 422


def test_qualification_version_detects_upstream_change(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    before = api._review_upstream_snapshot("e")
    conn.execute("UPDATE episodes SET storyboard_artifact_id='board-2', published_storyboard_artifact_id='board-2' WHERE id='e'")
    conn.commit()

    with pytest.raises(HTTPException) as conflict:
        api._review_assert_positive_action("e", before["qualification_version"])
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "REVIEW_QUALIFICATION_CHANGED"


def test_upstream_snapshot_finds_recoverable_run_without_episode_pointer(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               updated_at
           ) VALUES('run-screenplay','screenplay','episode','e','PAUSED_EXTERNAL','fp',1)"""
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    snapshot = api._review_upstream_snapshot("e")

    assert snapshot["eligible_for_production"] is False
    assert snapshot["active_upstream_runs"] == [{
        "kind": "screenplay",
        "run_id": "run-screenplay",
        "status": "PAUSED_EXTERNAL",
        "stage": None,
        "updated_at": 1.0,
        "source": "workflow_run",
    }]
    assert "编剧或分镜任务仍在运行" in snapshot["blockers"]


def test_upstream_snapshot_ignores_restart_orphan_superseded_by_success(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               updated_at,failure_code
           ) VALUES('run-orphan','storyboard','episode','e','PAUSED_EXTERNAL','old',1,'SERVICE_RESTART')"""
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               updated_at,finished_at
           ) VALUES('run-success','storyboard','episode','e','SUCCEEDED','new',2,2)"""
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    snapshot = api._review_upstream_snapshot("e")

    assert snapshot["eligible_for_production"] is True
    assert snapshot["active_upstream_runs"] == []


def test_upstream_snapshot_finds_live_task_without_durable_pointer(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(
        api.task_registry,
        "active",
        lambda kind, key: kind == "storyboard" and key == "e",
    )

    snapshot = api._review_upstream_snapshot("e")

    assert snapshot["eligible_for_production"] is False
    assert snapshot["active_upstream_runs"] == [{
        "kind": "storyboard",
        "run_id": None,
        "status": "RUNNING",
        "stage": None,
        "updated_at": None,
        "source": "task_registry",
    }]


@pytest.mark.asyncio
async def test_video_completion_spawn_failure_is_retryable_and_rolls_back(monkeypatch) -> None:
    import app.completion_grant as completion_grant
    import app.evidence.repository as evidence_repository
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine

    conn = _conn()
    for module in (
        api,
        completion_grant,
        evidence_repository,
        orchestration_engine,
        state_machine,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)

    def fail_spawn(*_args, **_kwargs):
        raise RuntimeError("event loop rejected task")

    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    monkeypatch.setattr(api.task_registry, "spawn", fail_spawn)

    with pytest.raises(HTTPException) as failed:
        await api._complete_episode_core("e", {"mode": "fresh"})

    assert failed.value.status_code == 503
    assert failed.value.detail["code"] == "VIDEO_COMPLETION_START_FAILED"
    assert failed.value.detail["retryable"] is True
    assert failed.value.detail["completion_grant_id"]
    episode = conn.execute(
        """SELECT status, video_completion_mode, active_video_run_id
             FROM episodes WHERE id='e'"""
    ).fetchone()
    assert dict(episode) == {
        "status": "confirmed",
        "video_completion_mode": "quick",
        "active_video_run_id": None,
    }
    run = conn.execute(
        "SELECT status, failure_code FROM workflow_runs WHERE id=?",
        (failed.value.detail["run_id"],),
    ).fetchone()
    assert dict(run) == {
        "status": "FAILED",
        "failure_code": "RUNTIMEERROR",
    }


@pytest.mark.asyncio
async def test_video_completion_shutdown_pauses_run_for_recovery(monkeypatch) -> None:
    import app.video_supervisor as video_supervisor

    class Recorder:
        run_id = "run-shutdown"
        paused = False
        cancelled = False

        def start(self):
            return None

        def pause_external(self, _message):
            self.paused = True

        def cancel(self):
            self.cancelled = True

    async def interrupted(*_args, **_kwargs):
        raise asyncio.CancelledError

    recorder = Recorder()
    monkeypatch.setattr(video_supervisor, "run_video_completion_resilient", interrupted)
    monkeypatch.setattr(api.task_registry, "shutdown_in_progress", lambda: True)

    with pytest.raises(asyncio.CancelledError):
        await api._recorded_video_completion_task(
            "e", recorder, resume=True, grant_id="grant",
        )

    assert recorder.paused is True
    assert recorder.cancelled is False


@pytest.mark.asyncio
async def test_deadline_fallback_completion_records_success(monkeypatch) -> None:
    import app.video_supervisor as video_supervisor
    from app.media_exec import enqueue as media_enqueue

    class Recorder:
        run_id = "run-fallback"
        succeeded_with = None
        partial_with = None

        def start(self):
            return None

        def succeed(self, outcome):
            self.succeeded_with = outcome

        def partial(self, outcome):
            self.partial_with = outcome

    async def completed(*_args, **_kwargs):
        return SimpleNamespace(
            phase="COMPLETED_DEADLINE_FALLBACK",
            outcome="COMPLETED_DEADLINE_FALLBACK",
        )

    recorder = Recorder()
    monkeypatch.setattr(video_supervisor, "run_video_completion_resilient", completed)
    monkeypatch.setattr(media_enqueue, "reconcile_episode_generation_status", lambda _eid: None)

    await api._recorded_video_completion_task(
        "e", recorder, resume=True, grant_id="grant",
    )

    assert recorder.succeeded_with == "COMPLETED_DEADLINE_FALLBACK"
    assert recorder.partial_with is None


@pytest.mark.asyncio
async def test_project_video_queue_spawn_failure_keeps_started_episode_and_reports_retry(
    monkeypatch,
) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,storyboard_artifact_id,created_at) "
        "VALUES('e2','p',2,'confirmed','sb2',1)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s2','e2',1,5)"
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)

    async def fake_complete(episode_id, _body):
        return {"run_id": f"run-{episode_id}", "completion_grant_id": f"grant-{episode_id}"}

    def fail_project_chain(kind, key, coro, *, project_id=None):
        assert (kind, key) == ("video_completion_project", "p")
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(api, "_complete_episode_core", fake_complete)
    monkeypatch.setattr(api.task_registry, "spawn", fail_project_chain)
    monkeypatch.setattr(
        api.errors,
        "record_and_format",
        lambda *_args, **_kwargs: "（测试错误）",
    )

    result = await api._complete_project_videos_core("p", {})

    assert result["started"][0]["episode_id"] == "e"
    assert result["project_queue_active"] is False
    assert result["retryable_schedule_failures"] == ["e2"]
    second = next(item for item in result["plan"] if item["episode_id"] == "e2")
    assert second["status"] == "failed_to_schedule"


@pytest.mark.asyncio
async def test_project_video_queue_is_persisted_and_recoverable(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,storyboard_artifact_id,created_at) "
        "VALUES('e2','p',2,'confirmed','sb2',1)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s2','e2',1,5)"
    )
    conn.commit()
    import app.evidence.repository as evidence_repository
    import app.orchestration.api as orchestration_api
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine

    for module in (
        api, evidence_repository, orchestration_api, orchestration_engine, state_machine,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)

    async def fake_complete(episode_id, _body):
        return {"run_id": f"run-{episode_id}", "completion_grant_id": f"grant-{episode_id}"}

    spawned = []

    def capture_spawn(kind, key, coro, *, project_id=None):
        spawned.append((kind, key, project_id))
        coro.close()
        return None

    monkeypatch.setattr(api, "_complete_episode_core", fake_complete)
    monkeypatch.setattr(api.task_registry, "spawn", capture_spawn)

    result = await api._complete_project_videos_core("p", {
        "global_budget_cap_cny": 300,
        "per_episode_cap_cny": 100,
    })

    project_run_id = result["project_queue_run_id"]
    assert project_run_id
    persisted = conn.execute(
        "SELECT workflow_type,status,config_snapshot_json FROM workflow_runs WHERE id=?",
        (project_run_id,),
    ).fetchone()
    assert persisted["workflow_type"] == "project_video_completion_queue"
    state = json.loads(persisted["config_snapshot_json"])["queue_state"]
    assert state["global_budget_cap_cny"] == 300
    assert next(item for item in state["plan"] if item["episode_id"] == "e2")["status"] == "queued"
    assert spawned == [("video_completion_project", "p", "p")]

    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL',failure_code='SERVICE_RESTART' "
        "WHERE id=?",
        (project_run_id,),
    )
    conn.commit()
    spawned.clear()

    assert api.recover_project_video_completion_queues() == 1
    child = conn.execute(
        "SELECT id,parent_run_id,trigger_type FROM workflow_runs "
        "WHERE parent_run_id=? ORDER BY updated_at DESC LIMIT 1",
        (project_run_id,),
    ).fetchone()
    assert child["parent_run_id"] == project_run_id
    assert child["trigger_type"] == "resume"
    assert spawned == [("video_completion_project", "p", "p")]

    with pytest.raises(HTTPException) as duplicate_resume:
        await orchestration_api._restart_run(project_run_id, "resume")
    assert duplicate_resume.value.detail["code"] == "RUN_ALREADY_RECOVERED"

    cancelled = await orchestration_api.cancel_run(child["id"])
    assert cancelled["cancelled"] is True
    assert cancelled["current_episode_continues"] is True
    assert evidence_repository.get_run(child["id"])["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_video_completion_rejects_existing_durable_active_run(monkeypatch) -> None:
    import app.completion_grant as completion_grant
    import app.evidence.repository as evidence_repository
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine

    conn = _conn()
    conn.execute(
        """INSERT INTO workflow_runs(
               id, workflow_type, scope_type, scope_id, status,
               input_fingerprint, updated_at
           ) VALUES(
               'run-existing','episode_video_completion','episode','e',
               'PAUSED_EXTERNAL','fp',1
           )"""
    )
    conn.execute(
        "UPDATE episodes SET active_video_run_id='run-existing' WHERE id='e'"
    )
    conn.commit()
    for module in (
        api,
        completion_grant,
        evidence_repository,
        orchestration_engine,
        state_machine,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)

    with pytest.raises(HTTPException) as rejected:
        await api._complete_episode_core("e", {"mode": "fresh"})

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "VIDEO_COMPLETION_ALREADY_ACTIVE"
    assert conn.execute(
        """SELECT COUNT(*) FROM workflow_runs
           WHERE workflow_type='episode_video_completion'"""
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT active_video_run_id FROM episodes WHERE id='e'"
    ).fetchone()[0] == "run-existing"
    grant = conn.execute(
        "SELECT revoked_at FROM completion_grants ORDER BY issued_at DESC LIMIT 1"
    ).fetchone()
    assert grant and grant["revoked_at"] is not None


@pytest.mark.asyncio
async def test_run_resume_reuses_video_checkpoint_and_records_parent(monkeypatch) -> None:
    import app.completion_grant as completion_grant
    import app.orchestration.api as orchestration_api
    import app.video_supervisor as video_supervisor

    conn = _conn()
    old_run = {
        "id": "run-old",
        "workflow_type": "episode_video_completion",
        "scope_id": "e",
        "status": "PAUSED_EXTERNAL",
    }
    new_run = {
        "id": "run-new",
        "workflow_type": "episode_video_completion",
        "scope_id": "e",
        "status": "CREATED",
        "parent_run_id": "run-old",
    }
    monkeypatch.setattr(orchestration_api, "get_conn", lambda: conn)
    monkeypatch.setattr(
        orchestration_api.repository,
        "get_run",
        lambda run_id: old_run if run_id == "run-old" else new_run,
    )
    monkeypatch.setattr(
        orchestration_api.task_registry,
        "active",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        video_supervisor,
        "load_latest_checkpoint",
        lambda _episode_id: SimpleNamespace(grant_id="grant-1"),
    )
    monkeypatch.setattr(
        completion_grant,
        "validate_video_grant",
        lambda *_args, **_kwargs: SimpleNamespace(grant_id="grant-1"),
    )
    captured = {}

    async def fake_complete(episode_id, body, **kwargs):
        captured.update({"episode_id": episode_id, "body": body, **kwargs})
        return {"run_id": "run-new"}

    monkeypatch.setattr(api, "_complete_episode_core", fake_complete)

    result = await orchestration_api._restart_run("run-old", "resume")

    assert result == new_run
    assert captured == {
        "episode_id": "e",
        "body": {"mode": "resume", "completion_grant_id": "grant-1"},
        "parent_run_id": "run-old",
        "trigger_type": "resume",
    }


@pytest.mark.asyncio
async def test_video_resume_without_grant_has_no_state_side_effect(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)

    with pytest.raises(HTTPException) as rejected:
        await api._complete_episode_core("e", {"mode": "resume"})

    assert rejected.value.status_code == 422
    assert rejected.value.detail["code"] == "VIDEO_COMPLETION_GRANT_REQUIRED"
    episode = conn.execute(
        "SELECT status, active_video_run_id FROM episodes WHERE id='e'"
    ).fetchone()
    assert dict(episode) == {"status": "confirmed", "active_video_run_id": None}
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_runs WHERE scope_id='e'"
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_video_parent_run_cannot_be_resumed_twice(monkeypatch) -> None:
    import app.orchestration.api as orchestration_api

    old_run = {
        "id": "run-old",
        "workflow_type": "episode_video_completion",
        "scope_id": "e",
        "status": "PAUSED_EXTERNAL",
        "recovered_by_run_id": "run-new",
    }
    monkeypatch.setattr(
        orchestration_api.repository,
        "get_run",
        lambda _run_id: old_run,
    )

    with pytest.raises(HTTPException) as rejected:
        await orchestration_api._restart_run("run-old", "resume")

    assert rejected.value.status_code == 409
    assert rejected.value.detail == {
        "code": "RUN_ALREADY_RECOVERED",
        "message": "该运行已创建续跑任务，请查看最新运行",
        "recovered_by_run_id": "run-new",
        "action": "open_recovered_run",
    }


@pytest.mark.parametrize(
    ("checkpoint", "running", "expected_state", "expected_action"),
    [
        (None, False, "not_started", "start_completion"),
        (
            SimpleNamespace(
                phase="WAITING_AUTHORIZATION",
                run_id="run-1",
                grant_id="grant-1",
            ),
            False,
            "waiting_authorization",
            "authorize_continue",
        ),
        (
            SimpleNamespace(
                phase="FAILED_CLOSED",
                run_id="run-1",
                grant_id="grant-1",
            ),
            False,
            "failed",
            "repair_preview",
        ),
        (
            SimpleNamespace(
                phase="FAILED_CLOSED",
                run_id="run-1",
                grant_id="grant-1",
            ),
            True,
            "failed",
            "repair_preview",
        ),
    ],
)
def test_video_completion_user_contract_is_actionable(
    checkpoint,
    running,
    expected_state,
    expected_action,
) -> None:
    projection = {
        "phase": getattr(checkpoint, "phase", None),
        "run_id": getattr(checkpoint, "run_id", None),
        "grant_id": getattr(checkpoint, "grant_id", None),
    }

    result = api._video_completion_user_contract(
        "e", checkpoint, projection, running=running,
    )

    assert result["user_state"] == expected_state
    assert result["message"]
    assert result["next_actions"][0]["id"] == expected_action
    assert result["next_actions"][0]["endpoint"].startswith("/api/")


def test_running_video_contract_pauses_active_run_not_stale_checkpoint() -> None:
    checkpoint = SimpleNamespace(
        phase="OBSERVING",
        run_id="run-old",
        grant_id="grant-1",
    )
    result = api._video_completion_user_contract(
        "e",
        checkpoint,
        {
            "phase": "OBSERVING",
            "run_id": "run-old",
            "active_video_run_id": "run-current",
        },
        running=True,
    )

    assert result["user_state"] == "running"
    pause = next(item for item in result["next_actions"] if item["id"] == "pause")
    assert pause["endpoint"] == "/api/runs/run-current/pause"


def test_running_new_run_overrides_old_terminal_checkpoint() -> None:
    checkpoint = SimpleNamespace(
        phase="FAILED_CLOSED",
        run_id="run-old",
        grant_id="grant-old",
    )
    result = api._video_completion_user_contract(
        "e",
        checkpoint,
        {
            "phase": "FAILED_CLOSED",
            "run_id": "run-old",
            "active_video_run_id": "run-current",
        },
        running=True,
    )

    assert result["user_state"] == "running"
    pause = next(item for item in result["next_actions"] if item["id"] == "pause")
    assert pause["endpoint"] == "/api/runs/run-current/pause"


def test_persisted_new_run_overrides_old_terminal_checkpoint_after_restart() -> None:
    checkpoint = SimpleNamespace(
        phase="FAILED_CLOSED",
        run_id="run-old",
        grant_id="grant-old",
    )
    result = api._video_completion_user_contract(
        "e",
        checkpoint,
        {
            "phase": "FAILED_CLOSED",
            "run_id": "run-old",
            "active_video_run_id": "run-current",
            "active_run_status": "RUNNING",
        },
        running=False,
    )

    assert result["user_state"] == "recovering"
    assert result["next_actions"][0]["endpoint"] == "/api/runs/run-current"


@pytest.mark.asyncio
async def test_resume_episode_reports_when_nothing_can_resume(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(
        api.worker,
        "resume_episode_video_tasks",
        lambda _episode_id: {"resumed_jobs": 0},
    )
    monkeypatch.setattr(api.worker, "retry_paused", lambda _episode_id: 0)

    async def empty_generation(_episode_id, _body):
        return {"enqueued": [], "skipped_completed": 1, "selected_shots": 0}

    monkeypatch.setattr(api, "_generate_episode_core", empty_generation)

    with pytest.raises(HTTPException) as rejected:
        await api.resume_episode("e")

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "VIDEO_RESUME_EMPTY"
    assert rejected.value.detail["recoverable"] is True
    assert rejected.value.detail["state"]["skipped_completed"] == 1


@pytest.mark.asyncio
async def test_resume_episode_reports_complete_mode_reset_as_success(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE episodes SET video_completion_mode='complete' WHERE id='e'",
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(
        api.worker,
        "resume_episode_video_tasks",
        lambda _episode_id: {"resumed_jobs": 0},
    )
    monkeypatch.setattr(api.worker, "retry_paused", lambda _episode_id: 0)

    async def reset_completion(_episode_id, *, reason):
        assert reason == "CONTINUED_AS_QUICK"
        return {"cancelled_task": True}

    async def empty_generation(_episode_id, _body):
        return {"enqueued": [], "skipped_completed": 1, "selected_shots": 0}

    monkeypatch.setattr(api, "reset_video_completion_state", reset_completion)
    monkeypatch.setattr(api, "_generate_episode_core", empty_generation)

    result = await api.resume_episode("e")

    assert result["state_changed"] is True
    assert result["video_completion_mode"] == "quick"
    assert result["supervisor_stopped"] is True
    assert result["cancelled_task"] is True
    assert result["selected_shots"] == 0


def test_worker_fences_stale_run_before_candidate_write(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    snapshot = api._review_upstream_snapshot("e")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('v-run','s1',1,'p','run-key','running',?,0)""",
        (json.dumps({
            "review_dependency_snapshot": {
                "qualification_version": snapshot["qualification_version"],
            }
        }),),
    )
    conn.execute(
        "UPDATE episodes SET published_storyboard_artifact_id='board-2', storyboard_artifact_id='board-2' WHERE id='e'"
    )
    conn.commit()

    with pytest.raises(worker.ReviewDependencyFence) as fenced:
        worker._assert_review_dependency_fence(
            {"episode_id": "e"}, "v-run", "candidate",
        )
    assert "REVIEW_DEPENDENCY_STALE" in str(fenced.value)


def test_asset_gate_uses_adopted_gallery_and_missing_verdict_is_unverified(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-adopted','s1',1,'p','k1','succeeded',?,0)""",
        (json.dumps({"reference_images": [{
            "id": "legacy-ref", "selectedForSeedance": True,
            "entity_type": "character", "entity_name": "角色甲",
            "gate_status": "unverified",
        }]}),),
    )
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-new','s1',2,'p','k2','succeeded',?,1)""",
        (json.dumps({"reference_images": [{
            "id": "new-ref", "selectedForSeedance": True,
            "gate_status": "passed", "rule_version": "r2",
        }]}),),
    )
    conn.execute("UPDATE shots SET adopted_version_id='v-adopted' WHERE id='s1'")
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    snapshot = api._review_upstream_snapshot("e")
    assert snapshot["eligible_for_production"] is True
    assert snapshot["assets"]["blockers"] == []
    warnings = snapshot["assets"]["soft_warnings"]
    assert any(
        item["ref_id"] == "legacy-ref" and item["warning"] == "gate_status:unverified"
        for item in warnings
    )
    assert all(item["ref_id"] != "new-ref" for item in snapshot["assets"]["inputs"] + warnings)


def test_asset_rule_version_participates_in_qualification_token(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v1','s1',1,'p','k','succeeded',?,0)""",
        (json.dumps({"reference_images": [{
            "id": "ref", "selectedForSeedance": True,
            "gate_status": "passed", "rule_version": "r1",
        }]}),),
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    before = api._review_upstream_snapshot("e")
    meta = {"reference_images": [{
        "id": "ref", "selectedForSeedance": True,
        "gate_status": "passed", "rule_version": "r2",
    }]}
    conn.execute("UPDATE shot_versions SET image_inputs=? WHERE id='v1'", (json.dumps(meta),))
    conn.commit()
    after = api._review_upstream_snapshot("e")
    assert before["qualification_version"] != after["qualification_version"]


def test_worker_does_not_self_fence_when_gallery_is_copied_to_new_version(monkeypatch) -> None:
    conn = _conn()
    reference = {
        "id": "ref", "selectedForSeedance": True,
        "gate_status": "passed", "rule_version": "r1",
        "library_revision_id": "asset-v1",
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v1','s1',1,'p','k1','succeeded',?,0)""",
        (json.dumps({"reference_images": [reference]}),),
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    snapshot = api._review_upstream_snapshot("e")
    captured = {
        key: snapshot.get(key) for key in (
            "qualification_version", "published_screenplay_artifact_id",
            "confirmed_storyboard_artifact_id", "screenplay_revision",
            "storyboard_revision", "asset_inputs", "asset_soft_warnings",
        )
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v2','s1',2,'p','k2','running',?,1)""",
        (json.dumps({
            "reference_images": [reference],
            "review_dependency_snapshot": captured,
        }),),
    )
    conn.commit()

    worker._assert_review_dependency_fence({"episode_id": "e"}, "v2", "candidate")


def test_worker_does_not_self_fence_on_gallery_generated_by_current_job(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,action_desc,characters,dialogues,
               storyboard_artifact_id
           ) VALUES('s2','e',2,5,'other','[]','[]','board-1')"""
    )
    stable_reference = {
        "id": "stable-ref", "selectedForSeedance": True,
        "gate_status": "passed", "rule_version": "r1",
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-other','s2',1,'p','other-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [stable_reference]}),),
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    snapshot = api._review_upstream_snapshot("e")
    captured = {
        key: snapshot.get(key) for key in (
            "qualification_version", "published_screenplay_artifact_id",
            "confirmed_storyboard_artifact_id", "screenplay_revision",
            "storyboard_revision", "asset_inputs", "asset_soft_warnings",
        )
    }
    generated_reference = {
        "id": "generated-ref", "selectedForSeedance": True,
        "gate_status": "scored", "rule_version": "keyframe_geometry_qa_v3",
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',1,'p','current-key','running',?,1)""",
        (json.dumps({
            "reference_images": [generated_reference],
            "review_dependency_snapshot": captured,
        }),),
    )
    conn.commit()

    worker._assert_review_dependency_fence(
        {"episode_id": "e", "shot_id": "s1"}, "v-current", "candidate",
    )


def test_worker_still_fences_gallery_change_on_another_shot(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,action_desc,characters,dialogues,
               storyboard_artifact_id
           ) VALUES('s2','e',2,5,'other','[]','[]','board-1')"""
    )
    original_reference = {
        "id": "other-ref", "selectedForSeedance": True,
        "gate_status": "passed", "rule_version": "r1",
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-other','s2',1,'p','other-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [original_reference]}),),
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    snapshot = api._review_upstream_snapshot("e")
    captured = {
        key: snapshot.get(key) for key in (
            "qualification_version", "published_screenplay_artifact_id",
            "confirmed_storyboard_artifact_id", "screenplay_revision",
            "storyboard_revision", "asset_inputs", "asset_soft_warnings",
        )
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',1,'p','current-key','running',?,1)""",
        (json.dumps({"review_dependency_snapshot": captured}),),
    )
    changed_reference = {**original_reference, "rule_version": "r2"}
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v-other'",
        (json.dumps({"reference_images": [changed_reference]}),),
    )
    conn.commit()

    with pytest.raises(worker.ReviewDependencyFence):
        worker._assert_review_dependency_fence(
            {"episode_id": "e", "shot_id": "s1"}, "v-current", "candidate",
        )
