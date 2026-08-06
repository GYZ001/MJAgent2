"""Internal maintenance reset must leave a truly fresh episode."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from app import api, config, db
from app.capabilities.direct import enter_handler
from app.schemas import EpisodeScreenplay
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "storyboard-clear.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    conn = db.get_conn()
    stamp = db.now()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','demo','ready',?)",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,screenplay_json,
               screenplay_status,screenplay_artifact_id,status,created_at,
               storyboard_outline_json,storyboard_artifact_id,
               working_storyboard_artifact_id,published_storyboard_artifact_id,
               storyboard_production_revision_id,storyboard_completion_certificate_id,
               active_storyboard_run_id,active_video_run_id,delivery_artifact_id,delivery_status
           ) VALUES(
               'e1','p1',1,'第一集','[]',?,'ready','art_screenplay','scripting',?,
               ?, 'art_storyboard','art_storyboard','art_storyboard',
               'rev_storyboard','cert_storyboard','run_storyboard','run_video','art_delivery','ready'
           )""",
        (
            EpisodeScreenplay(
                episode_no=1,
                title="第一集",
                full_script_text="【场1】日 / 广场\n少年走到石碑前等待测试。",
            ).model_dump_json(),
            stamp,
            json.dumps({"shots": [{"shot_no": 1}]}, ensure_ascii=False),
        ),
    )
    artifacts = [
        ("art_screenplay", "episode_screenplay", "episode", "e1", "approved", "screen"),
        ("art_storyboard", "storyboard", "episode", "e1", "approved", "board"),
        ("art_outline", "storyboard_outline", "episode", "e1", "validated", "outline"),
        (
            "art_checkpoint", "storyboard_supervisor_checkpoint", "episode", "e1",
            "validated", "checkpoint",
        ),
        (
            "art_shot", "storyboard_shot", "storyboard_checkpoint", "e1:1",
            "validated", "shot",
        ),
        ("art_delivery", "delivery_package", "episode", "e1", "approved", "delivery"),
    ]
    for index, (artifact_id, kind, scope_type, scope_id, status, content_hash) in enumerate(artifacts, 1):
        conn.execute(
            """INSERT INTO artifacts(
                   id,type,scope_type,scope_id,version,status,trust_level,
                   content_json,content_hash,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                artifact_id, kind, scope_type, scope_id, index, status, "T2",
                "{}", content_hash, stamp + index,
            ),
        )
    conn.execute(
        """INSERT INTO evaluations(
               id,artifact_id,evaluator_type,evaluator_name,evaluator_version,
               status,hard_gate_passed,created_at
           ) VALUES('eval_checkpoint','art_checkpoint','deterministic','storyboard','1','passed',1,?)""",
        (stamp,),
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,storyboard_artifact_id) "
        "VALUES('shot1','e1',1,5,'art_shot')"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('video1','shot1',1,'prompt','idem','succeeded',?)""",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               requested_by,trigger_type,updated_at,started_at
           ) VALUES('run_storyboard','storyboard','episode','e1','RUNNING','fp','user','manual',?,?)""",
        (stamp, stamp),
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               requested_by,trigger_type,updated_at,started_at
           ) VALUES('run_video','video_completion','episode','e1','RUNNING','video-fp','user','manual',?,?)""",
        (stamp, stamp),
    )
    conn.execute(
        """INSERT INTO run_events(id,run_id,ts,event_type,severity,message)
           VALUES('event1','run_storyboard',?,'RUN_STARTED','info','started')""",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO provider_calls(ts,kind,model,status,latency_ms,response_json,run_id,operation_id)
           VALUES(?,'chat','model','OK',1,'{}','run_storyboard','storyboard-cache-key')""",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO production_revisions(
               id,episode_id,kind,status,input_fingerprint,contract_version,
               qa_profile_version,created_at,updated_at
           ) VALUES('rev_storyboard','e1','storyboard','active','fp','1','1',?,?)""",
        (stamp, stamp),
    )
    conn.execute(
        """INSERT INTO completion_certificates(
               id,kind,scope_id,artifact_id,artifact_hash,input_fingerprint,
               contract_version,qa_profile_version,production_revision_id,issued_at
           ) VALUES('cert_storyboard','storyboard','e1','art_storyboard','board','fp','1','1',
                    'rev_storyboard',?)""",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO completion_grants(
               id,episode_id,project_id,screenplay_artifact_id,permission,token_hash,
               issued_by,issued_at,expires_at,kind,storyboard_artifact_id
           ) VALUES('grant1','e1','p1','art_screenplay','confirm','token','user',?,?,
                    'storyboard','art_storyboard')""",
        (stamp, stamp + 3600),
    )
    conn.execute(
        """INSERT INTO storyboard_action_previews(
               token,action_type,episode_id,baseline_fingerprint,payload_json,
               expires_at,created_at
           ) VALUES('preview1','confirm','e1','fp','{}',?,?)""",
        (stamp + 300, stamp),
    )
    conn.execute(
        """INSERT INTO storyboard_workspace_state(
               episode_id,snapshot_version,state_fingerprint,updated_at
           ) VALUES('e1',1,'fp',?)""",
        (stamp,),
    )
    package_dir = config.PROJECTS_DIR / "p1" / "delivery" / "pkg1"
    package_dir.mkdir(parents=True)
    (package_dir / "manifest.json").write_text("{}", encoding="utf-8")
    conn.execute(
        """INSERT INTO delivery_packages(
               id,episode_id,artifact_id,status,package_path,manifest_json,
               quality_report_json,known_issues,created_at
           ) VALUES('pkg1','e1','art_delivery','approved',?,'{}','{}','[]',?)""",
        (str(package_dir), stamp),
    )
    conn.commit()
    yield


def test_clear_storyboard_removes_resumable_data_cache_and_downstream_media() -> None:
    with enter_handler():
        result = asyncio.run(api.clear_storyboard("e1"))

    conn = db.get_conn()
    episode = conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    assert result["shots_deleted"] == 1
    assert result["media_versions_deleted"] == 1
    assert result["storyboard_runs_deleted"] == 1
    assert result["screenplay_preserved"] is True
    assert episode["screenplay_artifact_id"] == "art_screenplay"
    assert episode["screenplay_status"] == "ready"
    assert episode["status"] == "planned"
    assert episode["storyboard_outline_json"] is None
    assert episode["storyboard_artifact_id"] is None
    assert episode["working_storyboard_artifact_id"] is None
    assert episode["published_storyboard_artifact_id"] is None
    assert episode["storyboard_production_revision_id"] is None
    assert episode["storyboard_completion_certificate_id"] is None
    assert episode["active_storyboard_run_id"] is None
    assert episode["active_video_run_id"] is None
    assert episode["delivery_artifact_id"] is None
    assert conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM production_revisions WHERE episode_id='e1' AND kind='storyboard'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM completion_certificates WHERE scope_id='e1' AND kind='storyboard'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM workflow_runs WHERE id='run_storyboard'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM workflow_runs WHERE id='run_video'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM provider_calls WHERE operation_id='storyboard-cache-key'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM artifacts WHERE id='art_checkpoint'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM artifacts WHERE id='art_screenplay'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM storyboard_action_previews WHERE episode_id='e1'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM storyboard_workspace_state WHERE episode_id='e1'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM delivery_packages WHERE episode_id='e1'").fetchone()["c"] == 0
    assert not (config.PROJECTS_DIR / "p1" / "delivery" / "pkg1").exists()


def test_product_clear_requires_current_impact_preview(monkeypatch) -> None:
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET status='script_failed', active_storyboard_run_id=NULL, "
        "active_video_run_id=NULL WHERE id='e1'"
    )
    conn.execute(
        "UPDATE workflow_runs SET status='CANCELLED' "
        "WHERE id IN ('run_storyboard','run_video')"
    )
    conn.commit()
    preview = api.preview_storyboard_clear("e1")
    assert preview["shot_count"] == 1
    assert preview["video_version_count"] == 1
    assert preview["active_task_will_stop"] is False
    assert preview["screenplay_preserved"] is True

    with pytest.raises(HTTPException) as missing:
        asyncio.run(api.apply_storyboard_clear("e1", {}))
    assert missing.value.status_code == 428
    assert db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'"
    ).fetchone()["c"] == 1

    main_thread = threading.get_ident()
    original_delete = api.worker.delete_episode_shots
    delete_thread: list[int] = []

    def recorded_delete(episode_id: str):
        delete_thread.append(threading.get_ident())
        return original_delete(episode_id)

    monkeypatch.setattr(api.worker, "delete_episode_shots", recorded_delete)
    with enter_handler():
        result = asyncio.run(api.apply_storyboard_clear(
            "e1", {"preview_token": preview["preview_token"]},
        ))
    assert result["cleared"] is True
    assert result["shots_deleted"] == 1
    assert delete_thread and delete_thread[0] != main_thread
    assert result["audit_history_preserved"] is True
    episode = db.get_conn().execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    assert episode["screenplay_artifact_id"] == "art_screenplay"
    assert episode["storyboard_artifact_id"] is None
    assert db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM workflow_runs WHERE id='run_storyboard'"
    ).fetchone()["c"] == 1
    assert db.get_conn().execute(
        "SELECT status FROM production_revisions WHERE id='rev_storyboard'"
    ).fetchone()["status"] == "superseded"
    assert api._storyboard_start_preflight_payload("e1")["action"] == "create"


def test_product_clear_preview_supports_metadata_only_storyboard() -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM shot_versions WHERE shot_id='shot1'")
    conn.execute("DELETE FROM shots WHERE id='shot1'")
    conn.execute(
        "UPDATE episodes SET status='script_failed', active_storyboard_run_id=NULL, "
        "active_video_run_id=NULL WHERE id='e1'"
    )
    conn.execute(
        "UPDATE workflow_runs SET status='CANCELLED' "
        "WHERE id IN ('run_storyboard','run_video')"
    )
    conn.commit()

    preview = api.preview_storyboard_clear("e1")

    assert preview["shot_count"] == 0
    assert preview["video_version_count"] == 0
    assert preview["screenplay_preserved"] is True


def test_product_clear_supports_zero_prefix_paused_checkpoint() -> None:
    from app.storyboard_supervisor import SupervisorCheckpoint

    conn = db.get_conn()
    conn.execute("DELETE FROM shot_versions WHERE shot_id='shot1'")
    conn.execute("DELETE FROM shots WHERE id='shot1'")
    conn.execute(
        """UPDATE episodes SET
               status='scripting',
               script_error='用户已暂停分镜任务：已保留 0 个工作镜头和安全检查点',
               storyboard_outline_json=NULL,
               storyboard_artifact_id=NULL,
               working_storyboard_artifact_id=NULL,
               published_storyboard_artifact_id=NULL,
               storyboard_production_revision_id=NULL,
               storyboard_completion_certificate_id=NULL,
               active_storyboard_run_id=NULL,
               active_video_run_id=NULL,
               delivery_artifact_id=NULL,
               delivery_status='not_ready'
           WHERE id='e1'"""
    )
    conn.execute(
        "UPDATE workflow_runs SET status='CANCELLED' "
        "WHERE id IN ('run_storyboard','run_video')"
    )
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        phase="PAUSED_EXTERNAL",
        outcome="PAUSED_BY_USER",
        validated_prefix_end=0,
        next_shot_no=1,
    )
    conn.execute(
        """UPDATE artifacts
              SET content_json=?,status='validated'
            WHERE id='art_checkpoint'""",
        (checkpoint.model_dump_json(),),
    )
    conn.commit()

    preview = api.preview_storyboard_clear("e1")
    assert preview["shot_count"] == 0
    assert preview["workflow_run_count"] == 2

    with enter_handler():
        result = asyncio.run(api.apply_storyboard_clear(
            "e1",
            {"preview_token": preview["preview_token"]},
        ))

    assert result["cleared"] is True
    episode = db.get_conn().execute(
        "SELECT status,script_error FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(episode) == {"status": "planned", "script_error": None}


def test_product_clear_supports_preflight_failure_before_checkpoint() -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM shot_versions WHERE shot_id='shot1'")
    conn.execute("DELETE FROM shots WHERE id='shot1'")
    conn.execute("DELETE FROM evaluations WHERE artifact_id='art_checkpoint'")
    conn.execute("DELETE FROM artifacts WHERE id='art_checkpoint'")
    conn.execute(
        """UPDATE episodes SET
               status='script_failed',
               script_error='[场景图准备] 宝阁内尚未完成自动建库',
               storyboard_outline_json=NULL,
               storyboard_artifact_id=NULL,
               working_storyboard_artifact_id=NULL,
               published_storyboard_artifact_id=NULL,
               storyboard_production_revision_id=NULL,
               storyboard_completion_certificate_id=NULL,
               active_storyboard_run_id=NULL,
               active_video_run_id=NULL,
               delivery_artifact_id=NULL,
               delivery_status='not_ready'
           WHERE id='e1'"""
    )
    conn.execute(
        "UPDATE workflow_runs SET status='FAILED' "
        "WHERE id IN ('run_storyboard','run_video')"
    )
    conn.commit()

    preview = api.preview_storyboard_clear("e1")
    assert preview["shot_count"] == 0
    assert preview["workflow_run_count"] == 2

    with enter_handler():
        result = asyncio.run(api.apply_storyboard_clear(
            "e1",
            {"preview_token": preview["preview_token"]},
        ))

    assert result["cleared"] is True
    episode = db.get_conn().execute(
        "SELECT status,script_error FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(episode) == {"status": "planned", "script_error": None}


def test_product_clear_is_rejected_until_running_storyboard_stops() -> None:
    with pytest.raises(HTTPException) as running:
        api.preview_storyboard_clear("e1")
    assert running.value.status_code == 409
    assert "先暂停任务" in str(running.value.detail)


def test_storyboard_cancel_is_a_resumable_pause(monkeypatch) -> None:
    async def stopped(_kind: str, _episode_id: str) -> bool:
        return True

    monkeypatch.setattr(api.task_registry, "cancel_and_wait", stopped)
    with enter_handler():
        result = asyncio.run(api.cancel_storyboard("e1"))

    episode = db.get_conn().execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    assert result["paused"] is True
    assert result["checkpoint_phase"] == "PAUSED_EXTERNAL"
    assert episode["status"] == "scripting"
    assert episode["active_storyboard_run_id"] is None
    assert "用户已暂停" in episode["script_error"]
    from app.storyboard_supervisor import load_latest_checkpoint

    checkpoint = load_latest_checkpoint("e1")
    shot_row = db.get_conn().execute("SELECT * FROM shots WHERE id='shot1'").fetchone()
    status = api._storyboard_status_snapshot(
        dict(episode), [dict(shot_row)], checkpoint.model_dump(mode="json"),
    )
    assert status["state"] == "paused"
    assert status["recommended_action"] == "resume_storyboard"


def test_live_run_overrides_stale_paused_checkpoint_in_status_projection() -> None:
    from app.storyboard_supervisor import SupervisorCheckpoint, save_checkpoint

    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="PAUSED_EXTERNAL",
        outcome="PAUSED_BY_USER",
        validated_prefix_end=0,
        next_shot_no=1,
    ))
    episode = dict(db.get_conn().execute(
        "SELECT * FROM episodes WHERE id='e1'"
    ).fetchone())
    shot = dict(db.get_conn().execute(
        "SELECT * FROM shots WHERE id='shot1'"
    ).fetchone())

    status = api._storyboard_status_snapshot(
        episode,
        [shot],
        {
            "phase": "PAUSED_EXTERNAL",
            "outcome": "PAUSED_BY_USER",
            "validated_prefix_end": 0,
            "next_shot_no": 1,
        },
    )

    assert status["state"] == "running"
    assert status["recommended_action"] == "view_progress"
    assert status["task_phase"] == "PAUSED_EXTERNAL"


def test_start_auto_resumes_until_internal_reset_clears_existing_work() -> None:
    preview = api._storyboard_start_preflight_payload("e1")
    assert preview["action"] == "resume"

    with enter_handler():
        asyncio.run(api.clear_storyboard("e1"))
    preview = api._storyboard_start_preflight_payload("e1")
    assert preview["action"] == "create"
    assert preview["kept_validated_shots"] == 0
    assert preview["checkpoint"]["available"] is False
