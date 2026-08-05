from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import (
    api, atomic_io, db, planning, recovery, rejected_media, system_api, task_registry, worker,
)
from app.evidence import repository
from app.orchestration import api as orchestration_api


def _fresh_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "startup-recovery.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','Project','created',1)"
    )
    conn.commit()
    return conn


def _paused_run(workflow_type: str, scope_type: str, scope_id: str, *, config=None) -> str:
    run_id = repository.create_run(
        workflow_type=workflow_type,
        scope_type=scope_type,
        scope_id=scope_id,
        input_fingerprint="fingerprint",
        config_snapshot=config or {},
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL', failure_code='SERVICE_RESTART', "
        "failure_message='服务重启，可恢复', updated_at=2 WHERE id=?",
        (run_id,),
    )
    conn.commit()
    return run_id


def _capture_spawn(monkeypatch):
    spawned: list[tuple[str, str]] = []

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.append((kind, key))
        coro.close()
        return None

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    return spawned


def test_screenplay_resume_spawn_failure_restores_previous_state(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,screenplay_status,screenplay_error,
               screenplay_started_at,screenplay_updated_at,created_at
           ) VALUES('e1','p1',1,'Episode','failed','上次失败',10,11,1)"""
    )
    conn.commit()
    parent = _paused_run("screenplay", "episode", "e1")

    class Recorder:
        run_id = "run_not_started"
        cancel_message: str | None = None

        def cancel(self, message: str) -> None:
            self.cancel_message = message

    recorder = Recorder()

    async def pending_task():
        return None

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "active", lambda *_args: False)
    monkeypatch.setattr(task_registry, "spawn", fail_spawn)
    monkeypatch.setattr(api, "_new_screenplay_recorder", lambda *args, **kwargs: recorder)
    monkeypatch.setattr(api, "_recorded_screenplay_task", lambda *args, **kwargs: pending_task())

    with pytest.raises(HTTPException) as failed:
        orchestration_api._restart_screenplay_run(parent, "resume")

    assert failed.value.status_code == 503
    assert failed.value.detail["action"] == "retry_resume"
    row = conn.execute(
        "SELECT screenplay_status,screenplay_error,screenplay_started_at,screenplay_updated_at "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(row) == {
        "screenplay_status": "failed",
        "screenplay_error": "上次失败",
        "screenplay_started_at": 10,
        "screenplay_updated_at": 11,
    }
    assert recorder.cancel_message == "任务未能启动，剧集状态已回滚"


def test_reference_spawn_failures_restore_project_state(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "UPDATE projects SET refs_status='failed',refs_error='旧定妆错误',"
        "scene_refs_status='warning',scene_refs_error='旧场景提示' WHERE id='p1'"
    )
    conn.commit()

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)

    with pytest.raises(ValueError, match="定妆任务未能启动"):
        api._start_refs_generation("p1", None)
    with pytest.raises(ValueError, match="场景图任务未能启动"):
        api._start_scene_refs_generation("p1", None)
    with pytest.raises(ValueError, match="场景设定任务未能启动"):
        api._start_scene_bible_preparation("p1")

    project = conn.execute(
        "SELECT refs_status,refs_error,scene_refs_status,scene_refs_error FROM projects WHERE id='p1'"
    ).fetchone()
    assert dict(project) == {
        "refs_status": "failed",
        "refs_error": "旧定妆错误",
        "scene_refs_status": "warning",
        "scene_refs_error": "旧场景提示",
    }


def test_scene_recovery_prepares_missing_list_for_ready_bible(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "UPDATE projects SET bible_status='ready',scene_refs_status='idle',bible_json=? WHERE id='p1'",
        (json.dumps({"characters": [], "world": {"visual_style_canonical": "国漫"}}),),
    )
    conn.commit()
    spawned = _capture_spawn(monkeypatch)

    assert api.recover_scene_ref_tasks() == 1
    assert spawned == [("scene_bible", "p1")]
    assert conn.execute(
        "SELECT scene_refs_status FROM projects WHERE id='p1'"
    ).fetchone()["scene_refs_status"] == "running"


def test_single_view_redo_spawn_failure_cancels_created_runs(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)

    with pytest.raises(RuntimeError, match="人物单视角重做任务未能启动"):
        api._start_portrait_view_redo(
            "p1", "角色甲", "portrait-1", "profile",
            quote_id="quote-1", budget_limit_cny=1,
        )
    with pytest.raises(RuntimeError, match="场景单视角重做任务未能启动"):
        api._start_scene_view_redo(
            "p1", "庭院", "scene-1", "reverse",
            quote_id="quote-2", budget_limit_cny=1,
        )

    statuses = [
        row["status"] for row in conn.execute(
            "SELECT status FROM workflow_runs "
            "WHERE workflow_type IN ('portrait_view_redo','scene_view_redo') ORDER BY workflow_type"
        ).fetchall()
    ]
    assert statuses == ["CANCELLED", "CANCELLED"]


@pytest.mark.asyncio
async def test_scene_review_spawn_failure_does_not_leave_false_queued_batch(
    tmp_path, monkeypatch,
) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)

    with pytest.raises(HTTPException) as exc_info:
        await api.start_scene_history_review("p1")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "SCENE_REVIEW_START_FAILED"
    batch = conn.execute(
        "SELECT status,finished_at FROM scene_review_batches ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert batch["status"] == "failed"
    assert batch["finished_at"] is not None


def test_character_reference_restart_preserves_target_and_parent(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "UPDATE projects SET refs_status='running', refs_target='萧炎' WHERE id='p1'"
    )
    parent = _paused_run("character_references", "project", "p1")
    seen: list[dict] = []
    monkeypatch.setattr(api, "_refs_task_active", lambda _pid: False)
    monkeypatch.setattr(
        api,
        "_start_refs_generation",
        lambda project_id, target, **kwargs: seen.append({
            "project_id": project_id, "target": target, **kwargs,
        }) or True,
    )

    assert api.recover_character_ref_tasks() == 1
    assert seen == [{
        "project_id": "p1", "target": "萧炎", "only_characters": None,
        "resume": True, "fresh_after": None, "parent_run_id": parent,
    }]


def test_character_reference_restart_preserves_fresh_batch_boundary(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "UPDATE projects SET refs_status='running', refs_target=NULL, refs_resume=0, "
        "refs_batch_started_at=123.5 WHERE id='p1'"
    )
    seen: list[dict] = []
    monkeypatch.setattr(api, "_refs_task_active", lambda _pid: False)
    monkeypatch.setattr(
        api,
        "_start_refs_generation",
        lambda project_id, target, **kwargs: seen.append({
            "project_id": project_id, "target": target, **kwargs,
        }) or True,
    )

    assert api.recover_character_ref_tasks() == 1
    assert seen == [{
        "project_id": "p1", "target": None, "only_characters": None,
        "resume": True, "fresh_after": 123.5, "parent_run_id": None,
    }]


def test_character_reference_restart_recovers_paused_run_when_project_flag_is_idle(
    tmp_path, monkeypatch,
) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "UPDATE projects SET refs_status='idle', refs_target=NULL, refs_resume=0, "
        "refs_batch_started_at=456.5 WHERE id='p1'"
    )
    parent = _paused_run("character_references", "project", "p1")
    seen: list[dict] = []
    monkeypatch.setattr(api, "_refs_task_active", lambda _pid: False)
    monkeypatch.setattr(
        api,
        "_start_refs_generation",
        lambda project_id, target, **kwargs: seen.append({
            "project_id": project_id, "target": target, **kwargs,
        }) or True,
    )

    assert api.recover_character_ref_tasks() == 1
    assert seen == [{
        "project_id": "p1", "target": None, "only_characters": None,
        "resume": True, "fresh_after": 456.5, "parent_run_id": parent,
    }]


def test_fresh_character_reference_batch_persists_restart_mode(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    spawned = _capture_spawn(monkeypatch)
    monkeypatch.setattr(api, "_refs_task_active", lambda _pid: False)

    started = api._start_refs_generation(
        "p1", None, only_characters=["萧炎", "药老"], resume=False,
    )
    assert started and started["task_id"] == "refs:p1" and started["run_id"]

    row = conn.execute(
        "SELECT refs_status, refs_target, refs_resume, refs_batch_started_at "
        "FROM projects WHERE id='p1'"
    ).fetchone()
    assert row["refs_status"] == "running"
    assert json.loads(row["refs_target"]) == ["萧炎", "药老"]
    assert row["refs_resume"] == 0
    assert row["refs_batch_started_at"] is not None
    assert spawned == [("refs", "p1")]


def test_running_character_reference_run_keeps_refs_busy_when_project_flag_is_idle(
    tmp_path, monkeypatch,
) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    bible = {
        "characters": [{
            "name": "萧炎",
            "role": "主角",
            "appearance_canonical": "黑发少年，身穿玄色劲装，目光坚定，身形修长，腰佩玉佩",
            "personality": "坚韧",
            "speech_style": "沉稳",
            "relationships": [],
        }],
        "world": {"visual_style_canonical": "国漫风格", "era": "", "genre": ""},
    }
    conn.execute(
        "UPDATE projects SET bible_json=?, refs_status='idle' WHERE id='p1'",
        (json.dumps(bible, ensure_ascii=False),),
    )
    run_id = repository.create_run(
        workflow_type="character_references",
        scope_type="project",
        scope_id="p1",
        input_fingerprint="refs-running",
    )
    conn.execute("UPDATE workflow_runs SET status='RUNNING' WHERE id=?", (run_id,))
    conn.commit()
    monkeypatch.setattr(api, "_refs_task_active", lambda _pid: False)

    assert api._start_refs_generation("p1", None) is None
    progress = asyncio.run(api.refs_progress("p1"))
    assert progress["refs_status"] == "running"
    assert progress["missing"] == 1


def test_portrait_view_redo_is_recreated_from_paused_run(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    parent = _paused_run(
        "portrait_view_redo", "project", "p1",
        config={
            "task_key": "portrait_1:profile", "character_name": "萧炎",
            "portrait_id": "portrait_1", "view_role": "profile",
            "quote_id": "quote_1", "budget_limit_cny": 1.5,
        },
    )
    spawned = _capture_spawn(monkeypatch)
    monkeypatch.setattr(task_registry, "active", lambda *_args: False)

    assert api.recover_portrait_view_redo_tasks() == 1
    child = conn.execute(
        "SELECT id,parent_run_id,trigger_type,config_snapshot_json FROM workflow_runs "
        "WHERE parent_run_id=?", (parent,),
    ).fetchone()
    assert child and child["parent_run_id"] == parent
    assert child["trigger_type"] == "resume"
    assert json.loads(child["config_snapshot_json"])["view_role"] == "profile"
    assert spawned == [("portrait_view_redo", "portrait_1:profile")]


def test_delivery_http_task_is_recreated_as_background_attempt(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,created_at) "
        "VALUES('e1','p1',1,'Episode',1)"
    )
    conn.commit()
    payload = {"package_id": "delivery_stable", "reason": "ship"}
    old_run = _paused_run(
        "delivery_package", "episode", "e1",
        config={"recovery_payload": payload},
    )
    spawned = _capture_spawn(monkeypatch)

    assert orchestration_api.recover_delivery_tasks() == 1

    child = conn.execute(
        "SELECT id, parent_run_id, config_snapshot_json FROM workflow_runs WHERE parent_run_id=?",
        (old_run,),
    ).fetchone()
    assert json.loads(child["config_snapshot_json"])["recovery_payload"] == payload
    assert spawned == [("run", child["id"])]


def test_monitor_exposes_recovering_and_recovered_instead_of_stale_pause(
    tmp_path, monkeypatch,
) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    parent = _paused_run("video_generation", "project", "p1")
    conn.execute(
        "INSERT INTO jobs(id,kind,project_id,status,created_at,updated_at,run_id) "
        "VALUES('job1','video','p1','queued',1,2,?)",
        (parent,),
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", db.get_conn)

    before = system_api.jobs_overview()

    assert next(row for row in before["recent"] if row["id"] == parent)["status"] == "recovering"
    assert before["counts"]["recovering"] == 1

    child = repository.create_run(
        workflow_type="video_generation",
        scope_type="project",
        scope_id="p1",
        input_fingerprint="retry",
        parent_run_id=parent,
        trigger_type="resume",
    )
    after = system_api.jobs_overview()

    parent_row = next(row for row in after["recent"] if row["id"] == parent)
    assert parent_row["status"] == "recovered"
    assert parent_row["recovered_by_run_id"] == child
    assert after["counts"]["recovered"] == 1


def test_monitor_links_interrupted_provider_call_to_successful_retry(
    tmp_path, monkeypatch,
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    first = db.start_provider_call(
        "video_create", "model", request_json={"prompt": "same"},
        meta={"operation_id": "video-create-v1"},
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE provider_calls SET status='INTERRUPTED', "
        "recovery_disposition='AWAITING_RETRY' WHERE id=?",
        (first,),
    )
    conn.commit()
    second = db.start_provider_call(
        "video_create", "model", request_json={"prompt": "same"},
        meta={"operation_id": "video-create-v1"},
    )
    db.finish_provider_call(second, "OK", 200, 10, response_json={"id": "task"})
    monkeypatch.setattr(system_api, "get_conn", db.get_conn)

    calls = system_api.recent_calls()

    interrupted = next(row for row in calls if row["id"] == first)
    retry = next(row for row in calls if row["id"] == second)
    assert interrupted["effective_status"] == "RECOVERED"
    assert interrupted["superseded_by_call_id"] == second
    assert retry["supersedes_call_id"] == first


def test_unified_startup_recovery_runs_parent_before_all_child_adapters(monkeypatch) -> None:
    calls: list[str] = []

    def recover(name: str, result: int = 1):
        def operation(*_args, **_kwargs):
            calls.append(name)
            return result

        return operation

    monkeypatch.setattr(worker, "recover_media_jobs", recover("media"))
    monkeypatch.setattr(worker, "recover_and_start", recover("worker_start", 0))
    monkeypatch.setattr(worker, "start_stale_lease_sweeper", recover("lease_sweeper", 0))
    monkeypatch.setattr(atomic_io, "cleanup_abandoned_parts", recover("partial_cleanup", 2))
    monkeypatch.setattr(
        rejected_media,
        "purge_rejected_media",
        recover("rejected_media", {"artifacts": 2, "records": 3, "files": 2}),
    )
    monkeypatch.setattr(api, "recover_bible_tasks", recover("character_bible"))
    monkeypatch.setattr(api, "recover_character_ref_tasks", recover("character_references"))
    monkeypatch.setattr(api, "recover_portrait_view_redo_tasks", recover("portrait_view_redo"))
    monkeypatch.setattr(api, "recover_scene_ref_tasks", recover("scene_references"))
    monkeypatch.setattr(api, "recover_scene_review_tasks", recover("scene_history_review"))
    monkeypatch.setattr(planning, "recover_plan_tasks", recover("episode_mapping"))
    monkeypatch.setattr(api, "recover_screenplay_tasks", recover("screenplay"))
    monkeypatch.setattr(api, "recover_storyboard_tasks", recover("storyboard"))
    monkeypatch.setattr(
        "app.video_supervisor.recover_video_completion_runs",
        recover("video_completion"),
    )
    monkeypatch.setattr(
        api,
        "recover_project_video_completion_queues",
        recover("project_video_completion"),
    )
    monkeypatch.setattr(orchestration_api, "recover_delivery_tasks", recover("delivery"))

    report = asyncio.run(recovery.recover_all())

    assert calls == [
        "media", "partial_cleanup", "rejected_media", "worker_start", "lease_sweeper",
        "character_bible", "character_references", "portrait_view_redo",
        "scene_references", "scene_history_review", "episode_mapping",
        "screenplay", "storyboard", "video_completion", "project_video_completion", "delivery",
    ]
    assert {key: value for key, value in report.items() if key != "recovery_meta"} == {
        "media": 1, "abandoned_partial_files_removed": 2, "character_bible": 1,
        "rejected_media_purged": {"artifacts": 2, "records": 3, "files": 2},
        "character_references": 1, "portrait_view_redo": 1, "scene_references": 1,
        "scene_history_review": 1,
        "episode_mapping": 1, "screenplay": 1, "storyboard": 1,
        "video_completion": 1, "project_video_completion": 1, "delivery": 1,
    }
    assert report["recovery_meta"]["failed_steps"] == []
    assert report["recovery_meta"]["duration_ms"] >= 0


def test_startup_recovery_isolates_failed_step_and_continues(monkeypatch) -> None:
    import app.errors as app_errors

    calls: list[str] = []

    def ok(name: str):
        def operation(*_args, **_kwargs):
            calls.append(name)
            return 0
        return operation

    def fail_screenplay():
        calls.append("screenplay")
        raise RuntimeError("broken screenplay checkpoint")

    monkeypatch.setattr(worker, "recover_media_jobs", ok("media"))
    monkeypatch.setattr(worker, "recover_and_start", ok("worker_start"))
    monkeypatch.setattr(worker, "start_stale_lease_sweeper", ok("lease_sweeper"))
    monkeypatch.setattr(atomic_io, "cleanup_abandoned_parts", ok("partial_cleanup"))
    monkeypatch.setattr(api, "recover_bible_tasks", ok("character_bible"))
    monkeypatch.setattr(api, "recover_character_ref_tasks", ok("character_references"))
    monkeypatch.setattr(api, "recover_portrait_view_redo_tasks", ok("portrait_view_redo"))
    monkeypatch.setattr(api, "recover_scene_ref_tasks", ok("scene_references"))
    monkeypatch.setattr(api, "recover_scene_view_redo_tasks", ok("scene_view_redo"))
    monkeypatch.setattr(api, "recover_scene_review_tasks", ok("scene_history_review"))
    monkeypatch.setattr(planning, "recover_plan_tasks", ok("episode_mapping"))
    monkeypatch.setattr(api, "recover_screenplay_tasks", fail_screenplay)
    monkeypatch.setattr(api, "recover_storyboard_tasks", ok("storyboard"))
    monkeypatch.setattr(
        "app.video_supervisor.recover_video_completion_runs",
        ok("video_completion"),
    )
    monkeypatch.setattr(
        api,
        "recover_project_video_completion_queues",
        ok("project_video_completion"),
    )
    monkeypatch.setattr(orchestration_api, "recover_delivery_tasks", ok("delivery"))
    monkeypatch.setattr(
        app_errors,
        "log_error",
        lambda *_args, **_kwargs: SimpleNamespace(error_id="ERR-test"),
    )

    report = asyncio.run(recovery.recover_all())

    assert calls.index("storyboard") > calls.index("screenplay")
    assert calls.index("delivery") > calls.index("screenplay")
    assert report["screenplay"] == {
        "error": "broken screenplay checkpoint",
        "error_id": "ERR-test",
        "exc_type": "RuntimeError",
    }
    assert report["recovery_meta"]["failed_steps"] == ["screenplay"]


def test_passive_instance_initialization_does_not_fence_active_run(tmp_path, monkeypatch) -> None:
    conn = _fresh_database(tmp_path, monkeypatch)
    run_id = repository.create_run(
        workflow_type="screenplay", scope_type="project", scope_id="p1",
        input_fingerprint="active-primary",
    )
    step_id = repository.create_step(run_id, "screenplay")
    conn.execute("UPDATE workflow_runs SET status='RUNNING' WHERE id=?", (run_id,))
    conn.execute("UPDATE step_runs SET status='RUNNING' WHERE id=?", (step_id,))
    call_id = db.start_provider_call("chat", "model", request_json={"prompt": "active"})
    conn.commit()

    db.init_db(reconcile_interrupted=False)

    assert conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()["status"] == "RUNNING"
    assert conn.execute("SELECT status FROM step_runs WHERE id=?", (step_id,)).fetchone()["status"] == "RUNNING"
    assert conn.execute("SELECT status FROM provider_calls WHERE id=?", (call_id,)).fetchone()["status"] == "RUNNING"
