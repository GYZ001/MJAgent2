from __future__ import annotations

import asyncio
import json

from app import api, atomic_io, db, planning, recovery, system_api, task_registry, worker
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
        "resume": True, "parent_run_id": parent,
    }]


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
    monkeypatch.setattr(api, "recover_bible_tasks", recover("character_bible"))
    monkeypatch.setattr(api, "recover_character_ref_tasks", recover("character_references"))
    monkeypatch.setattr(api, "recover_scene_ref_tasks", recover("scene_references"))
    monkeypatch.setattr(planning, "recover_plan_tasks", recover("episode_mapping"))
    monkeypatch.setattr(api, "recover_screenplay_tasks", recover("screenplay"))
    monkeypatch.setattr(api, "recover_storyboard_tasks", recover("storyboard"))
    monkeypatch.setattr(
        "app.video_supervisor.recover_video_completion_runs",
        recover("video_completion"),
    )
    monkeypatch.setattr(orchestration_api, "recover_delivery_tasks", recover("delivery"))

    report = asyncio.run(recovery.recover_all())

    assert calls == [
        "media", "partial_cleanup", "worker_start", "lease_sweeper", "character_bible",
        "character_references", "scene_references", "episode_mapping",
        "screenplay", "storyboard", "video_completion", "delivery",
    ]
    assert report == {
        "media": 1, "abandoned_partial_files_removed": 2, "character_bible": 1,
        "character_references": 1, "scene_references": 1,
        "episode_mapping": 1, "screenplay": 1, "storyboard": 1,
        "video_completion": 1, "delivery": 1,
    }
