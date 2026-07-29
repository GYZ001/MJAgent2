import asyncio
import json
import threading

import pytest
from fastapi import HTTPException

from app import api, db, task_registry
from app.capabilities.direct import enter_handler
from app.evidence import repository
from app.harness.types import EvidenceArtifact
from app import storyboard_workspace as workspace
from app.orchestration import api as orchestration_api
from app.orchestration.engine import WorkflowRecorder
from app.orchestration.state_machine import transition_run
from app.storyboard_supervisor import (
    SupervisorCheckpoint,
    _begin_repair_activation,
    load_latest_checkpoint,
    save_checkpoint,
)
from app.domain.storyboard_ops import _recorded_storyboard_task


@pytest.fixture()
def storyboard_db(tmp_path, monkeypatch):
    database = tmp_path / "storyboard-workspace.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO projects(id,name,bible_json,bible_status,plan_status,created_at)
           VALUES('p1','测试项目','', 'ready','ready',1)"""
    )
    screenplay = {"id": "script-1", "episode_no": 1, "title": "测试", "full_script_text": "测试正文"}
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               screenplay_json,screenplay_status,screenplay_artifact_id,status,created_at
           ) VALUES('e1','p1',1,'第一集','[1]',10,?,'ready','screenplay-v1','scripted',1)""",
        (json.dumps(screenplay, ensure_ascii=False),),
    )
    source = "少年推开房门，看见桌上的信，神色骤然一沉。"
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) VALUES('p1',1,'第一章',?)",
        (source,),
    )
    artifact = repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot",
        scope_type="storyboard_checkpoint",
        scope_id="e1:1",
        status="validated",
        trust_level="T2",
        content={"shot_no": 1, "action_desc": "少年推门查看信件"},
    ))
    contract = {
        "state_in": "少年站在门外", "primary_action": "少年推门拿起信件",
        "state_out": "少年看完信后神色一沉", "characters_visible": ["少年"],
        "audio_cast": [], "audio_timeline": [], "new_information_ids": [],
        "spoken_contract_status": "coherent", "is_final": True,
    }
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
               characters,action_desc,first_frame_desc,last_frame_desc,source_excerpt,
               narration,dialogues,transition,continuity_from_prev,shot_contract_json,
               storyboard_artifact_id
           ) VALUES('s1','e1',1,5,'中景','固定','白天，房间','["少年"]',?,?,?,?,
                    '', '[]','硬切',0,?,?)""",
        (
            "少年推开房门并拿起桌上的信件查看。",
            "少年站在紧闭的房门外。",
            "少年拿着信件神色骤然一沉。",
            source,
            json.dumps(contract, ensure_ascii=False),
            artifact["id"],
        ),
    )
    conn.execute("UPDATE episodes SET storyboard_artifact_id=? WHERE id='e1'", (artifact["id"],))
    conn.commit()
    yield conn
    conn.close()


def test_snapshot_version_is_monotonic_and_action_is_unique(storyboard_db):
    ep = api.episode_detail("e1", view="board")
    first = ep["storyboard_status"]
    assert first["recommended_action"] in {
        "confirm_storyboard", "resume_storyboard", "refresh_status",
    }
    assert isinstance(first["recommended_action"], str)
    assert first["hard_gate_issue_count"] == len(first["hard_gate_issues"])
    assert ep["shots"][0]["qa_warnings"]
    assert not ep["shots"][0].get("preflight_errors")

    storyboard_db.execute("UPDATE episodes SET status='scripting' WHERE id='e1'")
    storyboard_db.commit()
    second = api.episode_detail("e1", view="board")["storyboard_status"]
    assert second["snapshot_version"] > first["snapshot_version"]
    assert second["recommended_action"] == "view_progress"
    assert second["confirmable"] is False


def test_status_distinguishes_visible_drafts_from_zero_safe_checkpoint(storyboard_db):
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="WAITING_RETRY",
        validated_prefix_end=0,
        next_shot_no=1,
        expected_total=5,
    ))
    storyboard_db.execute("UPDATE episodes SET status='scripting' WHERE id='e1'")
    storyboard_db.commit()

    status = api.episode_detail("e1", view="board")["storyboard_status"]

    assert status["state"] == "paused"
    assert status["planned_shots"] == 5
    assert status["draft_shots"] == 1
    assert status["safe_checkpoint_shots"] == 0
    assert status["pending_revalidation_shots"] == 1
    assert status["resume_from_shot"] == 1
    assert "通过" not in status["headline"]


def _cancel_test_run(storyboard_db) -> WorkflowRecorder:
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="cancel-test",
    )
    recorder.start()
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="GENERATING_SHOTS",
        validated_prefix_end=1,
        next_shot_no=2,
        expected_total=5,
    ), run_id=recorder.run_id)
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',script_error=NULL,active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()
    return recorder


def test_recorded_storyboard_task_releases_terminal_write_pointer(storyboard_db, monkeypatch):
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard", scope_type="episode", scope_id="e1",
        input_fingerprint="release-pointer-test",
    )
    storyboard_db.execute(
        "UPDATE episodes SET active_storyboard_run_id=?,status='scripted',script_error='暂停待处理' WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    async def completed_task(*args, **kwargs):
        return None

    monkeypatch.setattr("app.domain.storyboard_ops._storyboard_task", completed_task)
    asyncio.run(_recorded_storyboard_task("e1", recorder, resume=True))

    row = storyboard_db.execute(
        "SELECT active_storyboard_run_id FROM episodes WHERE id='e1'",
    ).fetchone()
    assert row["active_storyboard_run_id"] is None


def test_waiting_retry_resume_starts_a_fresh_activation_budget():
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        phase="WAITING_RETRY",
        repair_epoch=7,
        issue_fingerprint_counts={"same-root-cause": 3},
        last_repair={"strategy": "repair_window"},
        outcome=None,
    )

    previous_epoch = _begin_repair_activation(checkpoint, resume=True)

    assert previous_epoch == 7
    assert checkpoint.repair_epoch == 0
    assert checkpoint.issue_fingerprint_counts == {"same-root-cause": 3}
    assert checkpoint.last_repair == {"strategy": "repair_window"}


def test_non_retry_checkpoint_does_not_reset_repair_budget():
    checkpoint = SupervisorCheckpoint(
        episode_id="e1", phase="GENERATING_SHOTS", repair_epoch=4,
    )

    assert _begin_repair_activation(checkpoint, resume=True) is None
    assert checkpoint.repair_epoch == 4


@pytest.mark.asyncio
async def test_run_cancel_converges_storyboard_episode_and_checkpoint(storyboard_db):
    recorder = _cancel_test_run(storyboard_db)

    result = await orchestration_api.cancel_run(recorder.run_id)

    assert result["cancelled"] is True
    assert result["run"]["status"] == "CANCELLED"
    episode = storyboard_db.execute(
        "SELECT status,script_error,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["status"] == "script_failed"
    assert "第 2 镜继续" in episode["script_error"]
    assert episode["active_storyboard_run_id"] is None
    checkpoint = load_latest_checkpoint("e1")
    assert checkpoint is not None
    assert checkpoint.phase == "CANCELLED"
    assert checkpoint.outcome == "CANCELLED"


def test_board_read_repairs_legacy_cancelled_run_without_losing_shots(storyboard_db):
    recorder = _cancel_test_run(storyboard_db)
    recorder.cancel("模拟旧版本仅取消 Run")

    detail = api.episode_detail("e1", view="board")

    assert detail["status"] == "script_failed"
    assert detail["active_storyboard_run_id"] is None
    assert detail["storyboard_status"]["state"] == "failed"
    assert detail["storyboard_status"]["recommended_action"] == "resume_storyboard"
    assert len(detail["shots"]) == 1
    assert detail["supervisor"]["phase"] == "CANCELLED"


def test_active_storyboard_run_can_clear_its_repair_window_only(storyboard_db):
    from app.artifacts import clear_shot_artifacts

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard", scope_type="episode", scope_id="e1",
        input_fingerprint="repair-window",
    )
    recorder.start()
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    with pytest.raises(ValueError, match="仍在写入"):
        clear_shot_artifacts("s1", active_storyboard_run_id="run_not_current")
    storyboard_db.execute("UPDATE episodes SET script_error='修复计划已写入' WHERE id='e1'")
    assert storyboard_db.in_transaction is True
    result = clear_shot_artifacts("s1", active_storyboard_run_id=recorder.run_id)
    assert result["shot_id"] == "s1"
    assert storyboard_db.execute("SELECT id FROM shots WHERE id='s1'").fetchone() is not None


def test_supervisor_artifact_clear_can_join_one_atomic_repair_transaction(storyboard_db):
    from app.artifacts import clear_shot_artifacts

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard", scope_type="episode", scope_id="e1",
        input_fingerprint="atomic-window",
    )
    recorder.start()
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    clear_shot_artifacts(
        "s1", active_storyboard_run_id=recorder.run_id, commit=False
    )
    assert storyboard_db.in_transaction is True
    storyboard_db.execute("UPDATE shots SET shot_no=2 WHERE id='s1'")
    storyboard_db.rollback()
    assert storyboard_db.execute("SELECT shot_no FROM shots WHERE id='s1'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_resume_persists_active_storyboard_run_before_spawning(storyboard_db, monkeypatch):
    from app import task_registry

    storyboard_db.execute(
        "UPDATE episodes SET status='script_failed',script_error='可恢复' WHERE id='e1'"
    )
    storyboard_db.commit()
    spawned: dict[str, object] = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.update(kind=kind, key=key, project_id=project_id)
        coro.close()
        return object()

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    result = await api.resume_storyboard("e1")

    episode = storyboard_db.execute(
        "SELECT status,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result["run_id"] == episode["active_storyboard_run_id"]
    assert episode["status"] == "scripting"
    assert spawned == {"kind": "storyboard", "key": "e1", "project_id": "p1"}


@pytest.mark.asyncio
async def test_resume_does_not_deduplicate_terminal_run_behind_scripting_projection(
    storyboard_db, monkeypatch,
):
    from app import task_registry

    terminal = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="terminal-partial",
    )
    terminal.start()
    terminal.partial("activation budget yielded")
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (terminal.run_id,),
    )
    storyboard_db.commit()
    spawned: dict[str, object] = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.update(kind=kind, key=key, project_id=project_id)
        coro.close()
        return object()

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    monkeypatch.setattr(task_registry, "active", lambda _kind, _key: False)

    result = await api.resume_storyboard("e1")

    assert result.get("deduplicated") is not True
    assert result["run_id"] != terminal.run_id
    assert spawned == {"kind": "storyboard", "key": "e1", "project_id": "p1"}


@pytest.mark.asyncio
async def test_batch_storyboard_does_not_take_over_durable_active_run(
    storyboard_db, monkeypatch,
):
    from app import task_registry
    from app.capabilities import dispatch

    active = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="other-instance-active",
    )
    active.start()
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (active.run_id,),
    )
    storyboard_db.commit()
    monkeypatch.setattr(task_registry, "active", lambda *_args: False)

    async def bypass_capability_route(*_args, **_kwargs):
        return None

    monkeypatch.setattr(dispatch, "ui_route", bypass_capability_route)

    with pytest.raises(HTTPException) as rejected:
        await api.start_storyboard_all("p1")

    assert rejected.value.status_code == 409
    episode = storyboard_db.execute(
        "SELECT status,active_storyboard_run_id FROM episodes WHERE id='e1'",
    ).fetchone()
    assert dict(episode) == {
        "status": "scripting",
        "active_storyboard_run_id": active.run_id,
    }


@pytest.mark.asyncio
async def test_first_storyboard_spawn_failure_restores_episode_state(
    storyboard_db, monkeypatch,
):
    # “开始任务”只接受干净分镜；已有数据必须先明确清空，不能被 create 暗中覆盖。
    with enter_handler():
        await api.clear_storyboard("e1")
    storyboard_db.execute(
        "UPDATE episodes SET status='planned',script_error='启动前状态' WHERE id='e1'"
    )
    storyboard_db.commit()

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)
    with enter_handler(), pytest.raises(HTTPException) as exc_info:
        await api.start_storyboard("e1")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "STORYBOARD_START_SPAWN_FAILED"
    episode = storyboard_db.execute(
        "SELECT status,script_error,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(episode) == {
        "status": "planned",
        "script_error": "启动前状态",
        "active_storyboard_run_id": None,
    }
    latest = storyboard_db.execute(
        "SELECT status FROM workflow_runs WHERE workflow_type='storyboard' "
        "AND scope_id='e1' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    assert latest["status"] == "CANCELLED"


def test_storyboard_recovery_resumes_service_restart_and_persists_pointer(
    storyboard_db, monkeypatch,
):
    parent = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="restart",
    )
    parent.start()
    parent.pause_external("服务重启")
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (parent.run_id,),
    )
    storyboard_db.commit()
    spawned: dict[str, object] = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.update(kind=kind, key=key, project_id=project_id)
        coro.close()
        return None

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    assert api.recover_storyboard_tasks() == 1
    episode = storyboard_db.execute(
        "SELECT status,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["status"] == "scripting"
    assert episode["active_storyboard_run_id"] != parent.run_id
    child = storyboard_db.execute(
        "SELECT parent_run_id,trigger_type FROM workflow_runs WHERE id=?",
        (episode["active_storyboard_run_id"],),
    ).fetchone()
    assert dict(child) == {"parent_run_id": parent.run_id, "trigger_type": "resume"}
    assert spawned == {"kind": "storyboard", "key": "e1", "project_id": "p1"}


def test_storyboard_recovery_does_not_take_over_user_pause(
    storyboard_db, monkeypatch,
):
    paused = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="user-pause",
    )
    paused.start()
    transition_run(paused.run_id, "RUNNING", "PAUSED_EXTERNAL", "user_pause")
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (paused.run_id,),
    )
    storyboard_db.commit()
    monkeypatch.setattr(
        task_registry,
        "spawn",
        lambda *_args, **_kwargs: pytest.fail("用户暂停不应被启动恢复接管"),
    )

    assert api.recover_storyboard_tasks() == 0
    assert storyboard_db.execute(
        "SELECT active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()[0] == paused.run_id


@pytest.mark.asyncio
async def test_recorded_storyboard_shutdown_becomes_recoverable_pause(
    storyboard_db, monkeypatch,
):
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="shutdown",
    )
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    async def interrupted(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.domain.storyboard_ops._storyboard_task", interrupted)
    monkeypatch.setattr(task_registry, "shutdown_in_progress", lambda: True)

    with pytest.raises(asyncio.CancelledError):
        await _recorded_storyboard_task("e1", recorder, resume=True)

    run = repository.get_run(recorder.run_id)
    assert run["status"] == "PAUSED_EXTERNAL"
    assert run["failure_code"] == "SERVICE_RESTART"
    assert storyboard_db.execute(
        "SELECT active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()[0] is None


@pytest.mark.asyncio
async def test_batch_storyboard_reports_partial_start_failure(
    storyboard_db, monkeypatch,
):
    screenplay = storyboard_db.execute(
        "SELECT screenplay_json,screenplay_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()
    storyboard_db.execute(
        "UPDATE episodes SET status='planned',script_error=NULL,active_storyboard_run_id=NULL "
        "WHERE id='e1'"
    )
    storyboard_db.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,screenplay_json,screenplay_status,
               screenplay_artifact_id,status,created_at
           ) VALUES('e2','p1',2,'第二集',?,'ready',?,'planned',1)""",
        (screenplay["screenplay_json"], screenplay["screenplay_artifact_id"]),
    )
    storyboard_db.commit()

    def selective_spawn(_kind, key, coro, *, project_id=None):
        coro.close()
        if key == "e2":
            raise RuntimeError("queue unavailable")
        return None

    monkeypatch.setattr(task_registry, "spawn", selective_spawn)
    with enter_handler():
        result = await api.start_storyboard_all("p1")

    assert result["started"] == 1
    assert result["retryable_failures"] == 1
    assert result["failed_to_start"][0]["episode_id"] == "e2"
    rows = {
        row["id"]: dict(row)
        for row in storyboard_db.execute(
            "SELECT id,status,active_storyboard_run_id FROM episodes ORDER BY id"
        ).fetchall()
    }
    assert rows["e1"]["status"] == "scripting"
    assert rows["e1"]["active_storyboard_run_id"]
    assert rows["e2"] == {
        "id": "e2",
        "status": "planned",
        "active_storyboard_run_id": None,
    }


@pytest.mark.asyncio
async def test_resume_ignores_legacy_auto_confirm_request(storyboard_db, monkeypatch):
    from app import task_registry

    storyboard_db.execute(
        "UPDATE episodes SET status='script_failed',script_error='可恢复' WHERE id='e1'"
    )
    storyboard_db.commit()
    preview = api.storyboard_start_preflight("e1", {"mode": "resume"})

    def fake_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        return object()

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    result = await api.resume_storyboard("e1", {
        "preflight_token": preview["preview_token"],
        "completion_mode": "auto_confirm",
    })

    assert result["completion_mode"] == "ready_for_manual_confirm"
    assert "completion_grant_id" not in result
    assert storyboard_db.execute(
        "SELECT COUNT(*) AS c FROM completion_grants"
    ).fetchone()["c"] == 0


def test_start_preflight_expires_when_state_drifts(storyboard_db):
    preview = api.storyboard_start_preflight("e1", {"mode": "resume"})
    storyboard_db.execute("UPDATE episodes SET status='planned' WHERE id='e1'")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(preview["preview_token"], "start:resume", "e1")
    assert caught.value.status_code == 409


def test_running_state_cannot_acquire_edit_lease(storyboard_db):
    storyboard_db.execute("UPDATE episodes SET status='scripting' WHERE id='e1'")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.create_edit_session("s1")
    assert caught.value.status_code == 409


def test_stale_edit_session_is_rejected_without_borrowing_new_version(storyboard_db):
    session = workspace.create_edit_session("s1")
    newer = repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot", scope_type="storyboard_checkpoint", scope_id="e1:1",
        status="validated", trust_level="T2", content={"shot_no": 1, "action_desc": "新版本"},
    ))
    storyboard_db.execute("UPDATE shots SET storyboard_artifact_id=? WHERE id='s1'", (newer["id"],))
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.require_edit_session(session["edit_session_token"], "s1")
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "STALE_EDIT_BASELINE"


def test_source_binding_only_accepts_authorized_contiguous_range(storyboard_db):
    source = workspace.chapter_sources("e1")[0]
    start = source["content"].index("桌上的信")
    excerpt, normalized = workspace.validate_source_binding("e1", {
        "chapter_id": source["id"], "source_version_hash": source["source_version_hash"],
        "start_offset": start, "end_offset": start + len("桌上的信"),
    })
    assert excerpt == "桌上的信"
    workspace.persist_source_binding("s1", normalized)
    assert workspace.verify_or_bind_existing_excerpt("e1", "s1", excerpt)["chapter_idx"] == 1

    with pytest.raises(HTTPException):
        workspace.validate_source_binding("e1", {
            "chapter_id": source["id"], "source_version_hash": "wrong",
            "start_offset": start, "end_offset": start + 2,
        })
    with pytest.raises(HTTPException):
        workspace.validate_source_binding("e1", {
            "chapter_id": 999, "source_version_hash": source["source_version_hash"],
            "start_offset": 0, "end_offset": 2,
        })

    storyboard_db.execute("UPDATE chapters SET content=content || '新版' WHERE id=?", (source["id"],))
    storyboard_db.commit()
    with pytest.raises(HTTPException) as drifted:
        workspace.verify_or_bind_existing_excerpt("e1", "s1", excerpt)
    assert drifted.value.status_code == 409


def test_generated_source_binding_repair_replaces_stitched_excerpt(storyboard_db):
    stitched = "少年推开房门，看见桌上的信……神色骤然一沉。"
    storyboard_db.execute(
        "UPDATE shots SET source_excerpt=? WHERE id='s1'",
        (stitched,),
    )
    storyboard_db.commit()

    result = workspace.repair_generated_source_bindings("e1")

    assert result == {"bound": 1, "realigned": 1, "unresolved_shot_nos": []}
    repaired = storyboard_db.execute(
        "SELECT source_excerpt FROM shots WHERE id='s1'",
    ).fetchone()["source_excerpt"]
    assert repaired == "少年推开房门，看见桌上的信"
    assert workspace.verify_or_bind_existing_excerpt("e1", "s1", repaired)["chapter_idx"] == 1


def test_edit_impact_preview_is_noop_safe_and_exact(storyboard_db):
    session = workspace.create_edit_session("s1")
    no_op = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": {"duration_s": 5},
    })
    assert no_op["unchanged"] is True
    assert "preview_token" not in no_op

    changed = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": {"duration_s": 6},
    })
    assert changed["unchanged"] is False
    assert changed["changed_fields"] == ["duration_s"]
    assert changed["preview_token"].startswith("sbpv_")
    assert 1 in changed["revalidation_shots"]


def test_shot_edit_commits_deterministic_gate_without_treating_authorship_as_failure(storyboard_db):
    session = workspace.create_edit_session("s1")
    changes = {"camera_move": "缓慢推近"}
    preview = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": changes,
    })

    result = asyncio.run(api.edit_shot("s1", {
        **changes,
        "expected_version": session["baseline_artifact_id"],
        "edit_session_token": session["edit_session_token"],
        "preview_token": preview["preview_token"],
        "baseline_content_hash": session["baseline_content_hash"],
        "change_source": "test_edit",
    }))

    assert result["ok"] is True
    assert storyboard_db.execute("SELECT camera_move FROM shots WHERE id='s1'").fetchone()[0] == "缓慢推近"
    evaluations = repository.get_evaluations(result["artifact_id"])
    assert any(row["evaluator_name"] == "storyboard_editor" and not row["hard_gate_passed"] for row in evaluations)
    assert any(row["evaluator_name"] == "storyboard_shot_business_gate" and row["hard_gate_passed"] for row in evaluations)


def test_free_text_source_edit_is_rejected(storyboard_db):
    session = workspace.create_edit_session("s1")
    with pytest.raises(HTTPException) as caught:
        api.preview_shot_edit_impact("s1", {
            "edit_session_token": session["edit_session_token"],
            "changes": {"source_excerpt": "我自己编一段原文"},
        })
    assert caught.value.status_code == 422


def test_structure_preview_guards_unique_and_final_shot(storyboard_db):
    with pytest.raises(HTTPException) as unique:
        api.preview_storyboard_structure("e1", {"operation": "delete", "shot_id": "s1"})
    assert "唯一镜头" in str(unique.value.detail)

    preview = api.preview_storyboard_structure("e1", {
        "operation": "duplicate_after", "shot_id": "s1", "target_index": 0,
    })
    assert preview["before_count"] == 1
    assert preview["after_count"] == 2
    assert preview["requires_reconfirm"] is True


def test_structure_commit_keeps_contiguous_numbers_and_one_final(storyboard_db):
    preview = api.preview_storyboard_structure("e1", {
        "operation": "duplicate_after", "shot_id": "s1", "target_index": 0,
    })
    result = api.apply_storyboard_structure("e1", {
        "preview_token": preview["preview_token"], "operation": "duplicate_after",
        "shot_id": "s1", "target_index": 0, "new_final_shot_id": None,
    })
    assert result["shot_count"] == 2
    rows = storyboard_db.execute(
        "SELECT shot_no,shot_contract_json FROM shots WHERE episode_id='e1' ORDER BY shot_no"
    ).fetchall()
    assert [row["shot_no"] for row in rows] == [1, 2]
    assert sum(bool(json.loads(row["shot_contract_json"] or "{}").get("is_final")) for row in rows) == 1
    episode = storyboard_db.execute(
        "SELECT status,storyboard_outline_json FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["status"] == "scripted"
    assert len(json.loads(episode["storyboard_outline_json"])["shots"]) == 2
    status = api.episode_detail("e1", view="board")["storyboard_status"]
    assert status["planned_shots"] == 2
    assert status["produced_shots"] == 2
    assert status["editable"] is True


def test_structure_move_delete_and_add_keep_atomic_plan(storyboard_db):
    duplicate = api.preview_storyboard_structure("e1", {
        "operation": "duplicate_after", "shot_id": "s1", "target_index": 0,
    })
    created = api.apply_storyboard_structure("e1", {
        "preview_token": duplicate["preview_token"], "operation": "duplicate_after",
        "shot_id": "s1", "target_index": 0, "new_final_shot_id": None,
    })["created_shot_id"]

    move = api.preview_storyboard_structure("e1", {
        "operation": "move", "shot_id": created, "target_index": 0,
    })
    api.apply_storyboard_structure("e1", {
        "preview_token": move["preview_token"], "operation": "move",
        "shot_id": created, "target_index": 0, "new_final_shot_id": None,
    })
    assert storyboard_db.execute(
        "SELECT id FROM shots WHERE episode_id='e1' ORDER BY shot_no LIMIT 1"
    ).fetchone()[0] == created

    delete = api.preview_storyboard_structure("e1", {
        "operation": "delete", "shot_id": created,
    })
    api.apply_storyboard_structure("e1", {
        "preview_token": delete["preview_token"], "operation": "delete",
        "shot_id": created, "target_index": delete["target_index"], "new_final_shot_id": None,
    })
    add = api.preview_storyboard_structure("e1", {
        "operation": "add_after", "shot_id": "s1", "target_index": 0,
    })
    api.apply_storyboard_structure("e1", {
        "preview_token": add["preview_token"], "operation": "add_after",
        "shot_id": "s1", "target_index": 0, "new_final_shot_id": None,
    })
    rows = storyboard_db.execute(
        "SELECT shot_no,shot_contract_json FROM shots WHERE episode_id='e1' ORDER BY shot_no"
    ).fetchall()
    assert [row["shot_no"] for row in rows] == [1, 2]
    assert sum(bool(json.loads(row["shot_contract_json"] or "{}").get("is_final")) for row in rows) == 1


def test_confirmation_preview_is_rejected_after_version_drift(storyboard_db):
    preview = workspace.create_preview("confirm", "e1", {"hard_gates": {"passed": True}})
    storyboard_db.execute("UPDATE shots SET duration_s=6 WHERE id='s1'")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(preview["preview_token"], "confirm", "e1")
    assert caught.value.status_code == 409


def test_confirmation_preview_is_rejected_after_rate_drift(storyboard_db, monkeypatch):
    from app import config

    preview = workspace.create_preview("confirm", "e1", {"hard_gates": {"passed": True}})
    monkeypatch.setattr(config, "VIDEO_PRICE_PER_SECOND", config.VIDEO_PRICE_PER_SECOND + 0.1)
    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(preview["preview_token"], "confirm", "e1")
    assert caught.value.status_code == 409


def test_confirmation_preview_is_rejected_after_source_version_drift(storyboard_db):
    preview = workspace.create_preview("confirm", "e1", {"hard_gates": {"passed": True}})
    storyboard_db.execute("UPDATE chapters SET content=content || '变更' WHERE project_id='p1' AND idx=1")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(preview["preview_token"], "confirm", "e1")
    assert caught.value.status_code == 409


def test_emergency_readonly_flag_preserves_browsing_and_blocks_writes(storyboard_db):
    storyboard_db.execute(
        "UPDATE settings SET value='true' WHERE key='storyboard_workspace_safe_readonly'"
    )
    storyboard_db.commit()
    detail = api.episode_detail("e1", view="board")
    assert len(detail["shots"]) == 1
    assert detail["storyboard_status"]["state"] == "syncing"
    assert detail["storyboard_status"]["editable"] is False
    assert detail["storyboard_status"]["confirmable"] is False


def test_failed_draft_is_listed_and_published_version_unchanged(storyboard_db):
    draft = repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot", scope_type="storyboard_checkpoint", scope_id="e1:1",
        status="needs_revision", trust_level="T1", content={"shot_no": 1, "duration_s": 9},
        parent_artifact_ids=[storyboard_db.execute("SELECT storyboard_artifact_id FROM shots WHERE id='s1'").fetchone()[0]],
    ))
    before = storyboard_db.execute("SELECT storyboard_artifact_id FROM shots WHERE id='s1'").fetchone()[0]
    items = api.list_shot_edit_drafts("s1")["items"]
    assert items[0]["id"] == draft["id"]
    assert items[0]["content"]["duration_s"] == 9
    assert storyboard_db.execute("SELECT storyboard_artifact_id FROM shots WHERE id='s1'").fetchone()[0] == before


def test_confirmation_preview_rejects_non_terminal_episode(storyboard_db):
    storyboard_db.execute("UPDATE episodes SET status='script_failed', script_error='尚有问题' WHERE id='e1'")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        api.create_storyboard_confirmation_preview("e1")
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "STORYBOARD_NOT_CONFIRMABLE"


def test_automated_confirmation_reports_hard_errors_before_manual_warning(storyboard_db):
    storyboard_db.execute(
        "UPDATE episodes SET status='script_failed', script_error='尚有问题' WHERE id='e1'"
    )
    storyboard_db.execute(
        "UPDATE shots SET duration_s=8, dialogues=? WHERE id='s1'",
        (json.dumps([{
            "speaker": "少年",
            "line": "我要把这封信从头到尾认真地念完后再做决定",
            "emotion": "凝重",
            "delivery": "spoken_dialogue",
        }], ensure_ascii=False),),
    )
    storyboard_db.commit()

    with pytest.raises(HTTPException) as caught:
        api.create_storyboard_confirmation_preview("e1", automated=True)

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "STORYBOARD_NOT_CONFIRMABLE"
    warnings = caught.value.detail["warnings"]
    assert "存在超过 5 秒的镜头，已纳入 QA 评分报告" in warnings
    # 业务质量问题只进 warnings，不进 hard_gates.errors。
    hard_errors = caught.value.detail["hard_gates"]["errors"]
    assert all("低于硬下限" not in str(err) for err in hard_errors)
    assert any("低于硬下限" in str(w) or "action_desc" in str(w) for w in warnings)


def test_idempotent_confirmation_converges_terminal_runtime_state(storyboard_db):
    recorder = _cancel_test_run(storyboard_db)
    storyboard_db.execute("UPDATE episodes SET status='confirmed' WHERE id='e1'")
    storyboard_db.commit()

    result = api.confirm_episode_core("e1")

    assert result["confirmed"] is True
    assert result["idempotent"] is True
    episode = storyboard_db.execute(
        "SELECT active_storyboard_run_id,script_error FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["active_storyboard_run_id"] is None
    assert episode["script_error"] is None
    checkpoint = load_latest_checkpoint("e1")
    assert checkpoint.phase == "SUCCEEDED"
    assert checkpoint.outcome == "SUCCEEDED_CONFIRMED"
    recorder.cancel("test cleanup")
