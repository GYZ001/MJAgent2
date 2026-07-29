"""One-click storyboard clearing must leave a truly fresh episode."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app import api, config, db
from app.capabilities import ensure_catalog_loaded
from app.capabilities.direct import enter_handler
from app.capabilities.registry import get_registry
from app.schemas import EpisodeScreenplay


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


def test_start_is_rejected_until_existing_storyboard_is_cleared() -> None:
    with pytest.raises(HTTPException, match="继续任务") as caught:
        api._storyboard_start_preflight_payload("e1", "create")
    assert caught.value.status_code == 409

    with enter_handler():
        asyncio.run(api.clear_storyboard("e1"))
    preview = api._storyboard_start_preflight_payload("e1", "create")
    assert preview["action"] == "create"
    assert preview["kept_validated_shots"] == 0
    assert preview["checkpoint"]["available"] is False


def test_clear_route_is_registered_as_destructive_capability() -> None:
    ensure_catalog_loaded()
    registry = get_registry()
    assert registry.rest_bindings[
        "DELETE /api/episodes/{episode_id}/storyboard"
    ] == "storyboard.clear"
    assert registry.commands["storyboard.clear"].title == "一键清空分镜"
