from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import api, db, worker
from tests.conftest import patch_completion_grant_everywhere, patch_video_supervisor_everywhere, patch_worker_everywhere, patch_api_everywhere


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
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,action_desc,characters,dialogues,storyboard_artifact_id
           ) VALUES('s1','e',1,5,'中景','固定','日，测试室内场景',
                    'action','[]','[]','board-1')"""
    )
    conn.commit()
    return conn


def test_generation_context_excludes_manual_review_records(monkeypatch) -> None:
    conn = _conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

    snapshot = api._review_upstream_snapshot("e")

    assert snapshot["eligible_for_production"] is True
    assert snapshot["active_upstream_runs"] == []


def test_upstream_snapshot_ignores_restart_orphan_superseded_by_success_on_storyboard_pack_pipeline(
    monkeypatch,
) -> None:
    """分镜台 2.0.0（storyboard_pack）路径生成完成后只落 status='scripted'，
    从不推进到 'confirmed'。PAUSED_EXTERNAL 孤儿豁免判据若只挂
    episodes.status 白名单，会在这条管线下永远打不开——孤儿因此永久占着
    active，把"编剧或分镜任务仍在运行"锁死在一个已经证明被取代的旧运行
    上，即便产物本身（storyboard_pack_prompts_complete）已经完整。"""
    conn = _conn()
    conn.execute("UPDATE episodes SET status='scripted' WHERE id='e'")
    conn.execute(
        """UPDATE shots SET shot_contract_json=? WHERE id='s1'""",
        (json.dumps({
            "is_final": True,
            "storyboard_pack_segment": {"prompt_text": "一段完整的视频提示词。"},
        }),),
    )
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

    snapshot = api._review_upstream_snapshot("e")

    assert snapshot["eligible_for_production"] is True
    assert snapshot["active_upstream_runs"] == []
    assert "编剧或分镜任务仍在运行" not in snapshot["blockers"]


def test_upstream_snapshot_finds_live_task_without_durable_pointer(monkeypatch) -> None:
    conn = _conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
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
    import app.evidence.repository as evidence_repository
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine

    conn = _conn()
    # completion_grant 已是包（2026-08-31 拆分）：裸的循环 setattr 只改到包属性，
    # 够不到子模块各自绑的 get_conn 副本，必须走 helper。其余三个仍是单文件模块。
    patch_completion_grant_everywhere(monkeypatch, "get_conn", lambda: conn)
    for module in (
        evidence_repository,
        orchestration_engine,
        state_machine,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

    def fail_spawn(*_args, **_kwargs):
        raise RuntimeError("event loop rejected task")

    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    monkeypatch.setattr(api.task_registry, "spawn", fail_spawn)
    patch_api_everywhere(monkeypatch, "_assert_storyboard_generation_gate", lambda *_args: None)

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

    class Recorder:
        run_id = "run-shutdown"
        paused = False
        cancelled = False

        def start(self):
            return None

        def pause_external(self, _message, conn=None):
            self.paused = True

        def cancel(self, conn=None):
            self.cancelled = True

    async def interrupted(*_args, **_kwargs):
        raise asyncio.CancelledError

    recorder = Recorder()
    patch_video_supervisor_everywhere(monkeypatch, "run_video_completion_resilient", interrupted)
    monkeypatch.setattr(api.task_registry, "shutdown_in_progress", lambda: True)

    with pytest.raises(asyncio.CancelledError):
        await api._recorded_video_completion_task(
            "e", recorder, resume=True, grant_id="grant",
        )

    assert recorder.paused is True
    assert recorder.cancelled is False


@pytest.mark.asyncio
async def test_deadline_fallback_completion_records_success(monkeypatch) -> None:
    from app.media_exec import enqueue as media_enqueue

    class Recorder:
        run_id = "run-fallback"
        succeeded_with = None
        partial_with = None

        def start(self):
            return None

        def succeed(self, outcome, conn=None):
            self.succeeded_with = outcome

        def partial(self, outcome, conn=None):
            self.partial_with = outcome

    async def completed(*_args, **_kwargs):
        return SimpleNamespace(
            phase="COMPLETED_DEADLINE_FALLBACK",
            outcome="COMPLETED_DEADLINE_FALLBACK",
        )

    recorder = Recorder()
    patch_video_supervisor_everywhere(monkeypatch, "run_video_completion_resilient", completed)
    monkeypatch.setattr(media_enqueue, "reconcile_episode_generation_status", lambda _eid: None)

    await api._recorded_video_completion_task(
        "e", recorder, resume=True, grant_id="grant",
    )

    assert recorder.succeeded_with == "COMPLETED_DEADLINE_FALLBACK"
    assert recorder.partial_with is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adopted", "expected"),
    [(0, "failed"), (1, "partial")],
)
async def test_video_completion_terminal_status_uses_completed_shot_count(
    monkeypatch,
    adopted: int,
    expected: str,
) -> None:
    from app.media_exec import enqueue as media_enqueue

    class Recorder:
        run_id = "run-terminal-count"
        recorded = None

        def start(self):
            return None

        def partial(self, outcome, conn=None):
            self.recorded = ("partial", outcome, None)

        def fail_result(self, outcome, *, failure_code, conn=None):
            self.recorded = ("failed", outcome, failure_code)

    async def completed(*_args, **_kwargs):
        return SimpleNamespace(
            phase="PARTIAL_NO_USABLE_CANDIDATE",
            outcome="PARTIAL_NO_USABLE_CANDIDATE",
            coverage={"adopted": adopted, "total": 2},
            finished_at=100.0,
        )

    recorder = Recorder()
    patch_video_supervisor_everywhere(monkeypatch, "run_video_completion_resilient", completed)
    monkeypatch.setattr(media_enqueue, "reconcile_episode_generation_status", lambda _eid: None)

    await api._recorded_video_completion_task(
        "e", recorder, resume=True, grant_id="grant",
    )

    assert recorder.recorded[0] == expected
    assert recorder.recorded[1] == "PARTIAL_NO_USABLE_CANDIDATE"
    assert recorder.recorded[2] == (
        "NO_COMPLETED_OUTPUT" if expected == "failed" else None
    )


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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)

    async def fake_complete(episode_id, _body):
        return {"run_id": f"run-{episode_id}", "completion_grant_id": f"grant-{episode_id}"}

    def fail_project_chain(kind, key, coro, *, project_id=None):
        assert (kind, key) == ("video_completion_project", "p")
        coro.close()
        raise RuntimeError("event loop unavailable")

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)
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
        evidence_repository, orchestration_api, orchestration_engine, state_machine,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)

    async def fake_complete(episode_id, _body):
        return {"run_id": f"run-{episode_id}", "completion_grant_id": f"grant-{episode_id}"}

    spawned = []

    def capture_spawn(kind, key, coro, *, project_id=None):
        spawned.append((kind, key, project_id))
        coro.close()
        return None

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)
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
    # completion_grant 已是包（2026-08-31 拆分）：裸的循环 setattr 只改到包属性，
    # 够不到子模块各自绑的 get_conn 副本，必须走 helper。其余三个仍是单文件模块。
    patch_completion_grant_everywhere(monkeypatch, "get_conn", lambda: conn)
    for module in (
        evidence_repository,
        orchestration_engine,
        state_machine,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    patch_api_everywhere(monkeypatch, "_assert_storyboard_generation_gate", lambda *_args: None)

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
    import app.orchestration.api as orchestration_api

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
    patch_video_supervisor_everywhere(
        monkeypatch,
        "load_latest_checkpoint",
        lambda _episode_id: SimpleNamespace(grant_id="grant-1"),
    )
    patch_completion_grant_everywhere(
        monkeypatch,
        "validate_video_grant",
        lambda *_args, **_kwargs: SimpleNamespace(grant_id="grant-1"),
    )
    captured = {}

    async def fake_complete(episode_id, body, **kwargs):
        captured.update({"episode_id": episode_id, "body": body, **kwargs})
        return {"run_id": "run-new"}

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)

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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    patch_api_everywhere(monkeypatch, "_assert_storyboard_generation_gate", lambda *_args: None)

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


# ---------------------------------------------------------------------------
# P0 fix #4：WAITING_HUMAN / PAUSED_EXTERNAL 不得对已被供应商明确拒绝的镜头
# 展示"继续补齐"这个假出路——resume 对这类镜头不会重试它。同一 phase 也
# 用于用户手动暂停/资产待补齐等真正可以 resume 的场景，必须只在
# cp.last_plan 明确记录了不可修复的供应商终态判决时才换文案，其余维持原样。
# ---------------------------------------------------------------------------


def test_waiting_human_provider_rejected_shot_does_not_offer_blind_resume() -> None:
    checkpoint = SimpleNamespace(
        phase="WAITING_HUMAN",
        run_id="run-1",
        grant_id=None,
        last_plan={
            "shot_no": 7,
            "strategy": "handoff_human",
            "issue_codes": ["VIDEO_PROVIDER_TECHNICAL_FAILURE"],
        },
    )
    result = api._video_completion_user_contract(
        "e", checkpoint, {"phase": "WAITING_HUMAN", "run_id": "run-1"}, running=False,
    )

    assert not any(a["id"] == "resume" for a in result["next_actions"])
    assert "已被供应商明确拒绝" in result["message"]
    assert "继续补齐不会重试此镜" in result["message"]


def test_waiting_human_generic_wait_still_offers_resume() -> None:
    """反例：真正可恢复的等待（例如 QA 低分待复核）不能被误伤，必须继续
    给出 resume——不能矫枉过正把所有 WAITING_HUMAN 都当成拒绝。"""
    checkpoint = SimpleNamespace(
        phase="WAITING_HUMAN",
        run_id="run-1",
        grant_id=None,
        last_plan={
            "shot_no": 3,
            "strategy": "retake_directed",
            "issue_codes": ["VIDEO_QA_LOW_SCORE"],
        },
    )
    result = api._video_completion_user_contract(
        "e", checkpoint, {"phase": "WAITING_HUMAN", "run_id": "run-1"}, running=False,
    )

    resume = next(a for a in result["next_actions"] if a["id"] == "resume")
    assert resume["endpoint"] == "/api/runs/run-1/resume"
    assert result["message"] == "任务已暂停，检查评审意见或恢复条件后可继续"


def test_paused_external_manual_pause_still_offers_resume() -> None:
    """PAUSED_EXTERNAL 同时也是用户手动点"暂停"落地的 phase：没有
    last_plan（或 last_plan 不是拒绝判决）时不能被误判为"供应商拒绝"，
    否则就是对着手动暂停撒谎。"""
    checkpoint = SimpleNamespace(
        phase="PAUSED_EXTERNAL",
        run_id="run-1",
        grant_id=None,
        last_plan=None,
    )
    result = api._video_completion_user_contract(
        "e", checkpoint, {"phase": "PAUSED_EXTERNAL", "run_id": "run-1"}, running=False,
    )

    resume = next(a for a in result["next_actions"] if a["id"] == "resume")
    assert resume["endpoint"] == "/api/runs/run-1/resume"
    assert "已被供应商明确拒绝" not in result["message"]


def test_paused_external_provider_rejected_shot_points_to_prompt_edit_or_switch() -> None:
    checkpoint = SimpleNamespace(
        phase="PAUSED_EXTERNAL",
        run_id="run-1",
        grant_id=None,
        last_plan={
            "shot_no": 5,
            "strategy": "handoff_human",
            "issue_codes": ["VIDEO_PROVIDER_MODEL_REJECTED"],
        },
    )
    result = api._video_completion_user_contract(
        "e", checkpoint, {"phase": "PAUSED_EXTERNAL", "run_id": "run-1"}, running=False,
    )

    assert not any(a["id"] == "resume" for a in result["next_actions"])
    assert "已被供应商明确拒绝" in result["message"]
    assert "编辑该镜提示词" in result["message"] or "切换视频供应商" in result["message"]


def test_waiting_retry_without_rejection_signal_still_resumes() -> None:
    checkpoint = SimpleNamespace(
        phase="WAITING_RETRY", run_id="run-1", grant_id=None, last_plan=None,
    )
    result = api._video_completion_user_contract(
        "e", checkpoint, {"phase": "WAITING_RETRY", "run_id": "run-1"}, running=False,
    )

    assert any(a["id"] == "resume" for a in result["next_actions"])


def test_waiting_human_provider_rejected_shot_offers_prompt_edit_action() -> None:
    """有真实镜头可查时，出路要指向具体可操作的端点，不能只是一句空话。"""
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p_rej','P',0)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('ep_rej','p_rej',1,'generating',0)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition
           ) VALUES('shot_rej','ep_rej',7,5,'中景','固定','室内','[]','人物站定','[]','硬切')"""
    )
    conn.commit()

    checkpoint = SimpleNamespace(
        phase="WAITING_HUMAN",
        run_id="run-1",
        grant_id=None,
        last_plan={
            "shot_no": 7,
            "strategy": "handoff_human",
            "issue_codes": ["VIDEO_PROVIDER_TECHNICAL_FAILURE"],
        },
    )
    result = api._video_completion_user_contract(
        "ep_rej", checkpoint, {"phase": "WAITING_HUMAN", "run_id": "run-1"}, running=False,
    )

    edit_action = next(a for a in result["next_actions"] if a["id"] == "edit_shot_prompt")
    assert edit_action["endpoint"] == "/api/shots/shot_rej/generate"
    assert edit_action["method"] == "POST"


@pytest.mark.asyncio
async def test_generate_episode_reused_active_version_is_not_adopted(
    monkeypatch,
    tmp_path,
) -> None:
    from app import multiview

    conn = _conn()
    conn.execute(
        """UPDATE shots
              SET shot_size='中景',camera_move='固定',scene_setting='室内'
            WHERE id='s1'"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v-reused','s1',1,'p','k','running',0)"""
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch,
        "_review_assert_positive_action",
        lambda *_args, **_kwargs: {"qualification_version": "q1"},
    )
    patch_api_everywhere(monkeypatch, "_assert_storyboard_generation_gate", lambda _episode_id: None)
    monkeypatch.setattr(
        multiview,
        "scan_episode_reference_asset_gaps",
        lambda **_kwargs: {"blockers": [], "characters": [], "scenes": []},
    )

    monkeypatch.setattr(
        api.worker,
        "enqueue_shot",
        lambda *_args, **_kwargs: {"reused": True, "version_id": "v-reused"},
    )

    await api._generate_episode_core("e", {})
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()["adopted_version_id"] is None

    video_path = tmp_path / "reused.mp4"
    video_path.write_bytes(b"video")
    conn.execute(
        """UPDATE shot_versions
              SET status='succeeded',video_path=?,
                  technical_validation_json='{"passed":true}'
            WHERE id='v-reused'""",
        (str(video_path),),
    )
    conn.commit()

    await api._generate_episode_core("e", {})
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()["adopted_version_id"] == "v-reused"


@pytest.mark.asyncio
async def test_resume_episode_reports_when_nothing_can_resume(monkeypatch) -> None:
    conn = _conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(
        api.worker,
        "resume_episode_video_tasks",
        lambda _episode_id: {"resumed_jobs": 0},
    )
    monkeypatch.setattr(api.worker, "retry_paused", lambda _episode_id: 0)

    async def empty_generation(_episode_id, _body):
        return {"enqueued": [], "skipped_completed": 1, "selected_shots": 0}

    patch_api_everywhere(monkeypatch, "_generate_episode_core", empty_generation)

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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
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

    patch_api_everywhere(monkeypatch, "reset_video_completion_state", reset_completion)
    patch_api_everywhere(monkeypatch, "_generate_episode_core", empty_generation)

    result = await api.resume_episode("e")

    assert result["state_changed"] is True
    assert result["video_completion_mode"] == "quick"
    assert result["supervisor_stopped"] is True
    assert result["cancelled_task"] is True
    assert result["selected_shots"] == 0


def test_worker_fences_stale_run_before_candidate_write(monkeypatch) -> None:
    conn = _conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
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
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,action_desc,characters,dialogues,storyboard_artifact_id
           ) VALUES('s2','e',2,5,'中景','固定','日，测试室内场景',
                    'other','[]','[]','board-1')"""
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
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
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,action_desc,characters,dialogues,storyboard_artifact_id
           ) VALUES('s2','e',2,5,'中景','固定','日，测试室内场景',
                    'other','[]','[]','board-1')"""
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
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


def test_worker_ignores_sibling_gallery_growth_on_another_shot(monkeypatch) -> None:
    """复现 EP1 段5/6/7 假失败：兄弟镜并发解析出新素材条目，不得让在途镜误判过期。

    与 ``test_worker_still_fences_gallery_change_on_another_shot`` 对照：那个
    测试里兄弟镜是把已存在的条目改了内容（rule_version 变化），仍必须拦住；
    这里兄弟镜只是新增了一条之前不存在的条目（自己的资格解析第一次落库），
    不得让别的在途镜的资格快照被判定过期——两者都不要求 narrative authority，
    直接命中 ``assets_equal`` 的非豁免分支。
    """
    conn = _conn()
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,action_desc,characters,dialogues,storyboard_artifact_id
           ) VALUES('s2','e',2,5,'中景','固定','日，测试室内场景',
                    'other','[]','[]','board-1')"""
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
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
    # 兄弟镜 s2 的画廊只是新增了一条条目（自己首次落库的解析结果），原有的
    # other-ref 原样保留、内容未变。
    new_sibling_reference = {
        "id": "brand-new-sibling-ref", "selectedForSeedance": True,
        "gate_status": "passed", "rule_version": "r1",
    }
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v-other'",
        (json.dumps({"reference_images": [original_reference, new_sibling_reference]}),),
    )
    conn.commit()

    # 修复前：assets_equal 用整集精确列表相等比较，这里会误炸
    # REVIEW_DEPENDENCY_STALE；修复后：只要求 expected 是 current 的子集，
    # 新增条目不影响已被别的在途镜依赖的既有条目，不应报错。
    worker._assert_review_dependency_fence(
        {"episode_id": "e", "shot_id": "s1"}, "v-current", "candidate",
    )


def _terminal_projection(coverage: dict | None, **extra) -> dict:
    proj = {"phase": "SUCCEEDED_COVERED", "run_id": "run-old", "grant_id": "grant-old"}
    if coverage is not None:
        proj["coverage"] = coverage
    proj.update(extra)
    return proj


def test_terminal_success_expires_when_storyboard_no_longer_covered() -> None:
    """分镜重做后旧终态不得继续宣布"已补齐"，且必须给回重跑入口。

    实测 ``ep_0a70ec56e8e9``：分镜重做换掉整张镜头表后，同一条响应里 coverage
    是 adopted 0 / total 4，user_state 却仍是 completed、next_actions 只剩
    「查看成片」——界面宣布已补齐却没有任何入口能重新补齐，脚本化调用也因此
    整段跳过视频阶段，零条供应商调用就报「没有候选版本可采纳」。
    """
    checkpoint = SimpleNamespace(
        phase="SUCCEEDED_COVERED", run_id="run-old", grant_id="grant-old",
    )

    result = api._video_completion_user_contract(
        "e",
        checkpoint,
        _terminal_projection({"total": 4, "adopted": 0, "unadopted": 4}),
        running=False,
    )

    assert result["user_state"] == "not_started"
    assert "4" in result["message"]
    assert [a["id"] for a in result["next_actions"]] == ["start_completion"]
    assert result["next_actions"][0]["endpoint"] == "/api/episodes/e/video-completion"


def test_terminal_success_still_completed_when_every_shot_adopted() -> None:
    checkpoint = SimpleNamespace(
        phase="SUCCEEDED_COVERED", run_id="run-old", grant_id="grant-old",
    )

    result = api._video_completion_user_contract(
        "e", checkpoint,
        _terminal_projection({"total": 3, "adopted": 3, "unadopted": 0}),
        running=False,
    )

    assert result["user_state"] == "completed"
    assert result["next_actions"][0]["id"] == "view_results"


def test_terminal_success_keeps_checkpoint_verdict_without_live_signal() -> None:
    """台账没建起来时没有产物信号可依，维持 checkpoint 结论，不拿缺失当证据。"""
    checkpoint = SimpleNamespace(
        phase="SUCCEEDED_COVERED", run_id="run-old", grant_id="grant-old",
    )

    no_ledger = api._video_completion_user_contract(
        "e", checkpoint, _terminal_projection(None), running=False,
    )
    errored = api._video_completion_user_contract(
        "e", checkpoint,
        _terminal_projection({"total": 4, "adopted": 0, "unadopted": 4},
                             ledger_error="boom"),
        running=False,
    )

    assert no_ledger["user_state"] == "completed"
    assert errored["user_state"] == "completed"


def test_terminal_success_with_zero_shots_is_not_completed() -> None:
    """空集合不等于"无需检查"：一个镜头都没有的分集谈不上"全片已补齐"。"""
    checkpoint = SimpleNamespace(
        phase="COMPLETED_DEADLINE_FALLBACK", run_id="run-old", grant_id="grant-old",
    )

    result = api._video_completion_user_contract(
        "e", checkpoint,
        {"phase": "COMPLETED_DEADLINE_FALLBACK", "run_id": "run-old",
         "coverage": {"total": 0, "adopted": 0, "unadopted": 0}},
        running=False,
    )

    assert result["user_state"] == "not_started"
    assert result["next_actions"][0]["id"] == "open_storyboard"
    assert result["next_actions"][0]["endpoint"] == "/api/episodes/e/storyboard/status"
