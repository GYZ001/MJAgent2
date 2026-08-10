import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import artifacts, completion_grant, db
from app.capabilities.direct import enter_handler


def _database() -> sqlite3.Connection:
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
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e','p',1,'done',0)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,adopted_version_id) "
        "VALUES('s','e',1,5,'v1')"
    )
    conn.commit()
    return conn


def test_video_only_clear_preserves_reference_gallery(tmp_path, monkeypatch) -> None:
    conn = _database()
    root = tmp_path / "projects"
    shot_dir = root / "p" / "episodes" / "1" / "shots" / "1"
    refs = shot_dir / "references"
    refs.mkdir(parents=True)
    ref_path = refs / "keyframe.png"
    ref_path.write_bytes(b"ref")
    videos = []
    for no in (1, 2):
        path = shot_dir / f"v{no}.mp4"
        path.write_bytes(b"video")
        videos.append(path)
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,video_path,image_inputs,created_at
               ) VALUES(?,?,?,?,?,'succeeded',?,?,?)""",
            (
                f"v{no}", "s", no, "prompt", f"key-{no}", str(path),
                json.dumps({"reference_images": [{"id": "ref", "path": str(ref_path)}]}), no,
            ),
        )
    conn.execute(
        "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at) "
        "VALUES('j','video','s','v2','e','p','succeeded',0,0)"
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", root)

    result = artifacts.clear_shot_video_assets("s")

    assert result["videos"] == 2
    assert ref_path.exists()
    assert not any(path.exists() for path in videos)
    rows = conn.execute(
        "SELECT status,video_path,image_inputs FROM shot_versions WHERE shot_id='s'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "references_ready"
    assert rows[0]["video_path"] is None
    assert json.loads(rows[0]["image_inputs"])["reference_images"][0]["id"] == "ref"
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] is None


def test_reference_only_clear_preserves_existing_video(tmp_path, monkeypatch) -> None:
    conn = _database()
    root = tmp_path / "projects"
    shot_dir = root / "p" / "episodes" / "1" / "shots" / "1"
    refs = shot_dir / "references"
    refs.mkdir(parents=True)
    ref_path = refs / "keyframe.png"
    ref_path.write_bytes(b"ref")
    scene_path = shot_dir / "scene.png"
    scene_path.write_bytes(b"scene")
    video_path = shot_dir / "v1.mp4"
    video_path.write_bytes(b"video")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,image_inputs,created_at
           ) VALUES('v1','s',1,'prompt','key','succeeded',?,?,0)""",
        (str(video_path), json.dumps({"reference_images": [{"id": "ref", "path": str(ref_path)}]})),
    )
    conn.execute(
        """INSERT INTO shot_scenes(id,shot_id,version_no,kind,prompt_text,image_path,status,created_at)
           VALUES('scene','s',1,'head','prompt',?,'succeeded',0)""",
        (str(scene_path),),
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", root)

    result = artifacts.clear_shot_reference_assets("s")

    assert result["videos_preserved"] is True
    assert video_path.exists()
    assert not ref_path.exists()
    assert not scene_path.exists()
    row = conn.execute(
        "SELECT status,video_path,image_inputs FROM shot_versions WHERE id='v1'"
    ).fetchone()
    assert row["status"] == "succeeded"
    assert row["video_path"] == str(video_path)
    assert "reference_images" not in json.loads(row["image_inputs"])


def test_resource_clear_removes_video_images_and_reference_indexes(tmp_path, monkeypatch) -> None:
    conn = _database()
    root = tmp_path / "projects"
    shot_dir = root / "p" / "episodes" / "1" / "shots" / "1"
    refs = shot_dir / "references"
    refs.mkdir(parents=True)
    ref_path = refs / "keyframe.png"
    scene_path = shot_dir / "scene.png"
    boundary_path = shot_dir / "boundaries" / "first.jpg"
    boundary_path.parent.mkdir(parents=True)
    video_path = shot_dir / "v1.mp4"
    ref_path.write_bytes(b"ref")
    scene_path.write_bytes(b"scene")
    boundary_path.write_bytes(b"boundary")
    video_path.write_bytes(b"video")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,image_inputs,created_at
           ) VALUES('v1','s',1,'prompt','key','succeeded',?,?,0)""",
        (str(video_path), json.dumps({"reference_images": [{"id": "ref", "path": str(ref_path)}]})),
    )
    conn.execute(
        """INSERT INTO shot_scenes(id,shot_id,version_no,kind,prompt_text,image_path,status,created_at)
           VALUES('scene','s',1,'head','prompt',?,'succeeded',0)""",
        (str(scene_path),),
    )
    conn.execute(
        """INSERT INTO reference_sets(
               id,shot_id,source_version_id,fingerprint,created_at,updated_at
           ) VALUES('set','s','v1','fp',0,0)"""
    )
    conn.execute(
        """INSERT INTO reference_assets(
               id,reference_set_id,asset_type,path,created_at
           ) VALUES('asset','set','plot_key_frame',?,0)""",
        (str(ref_path),),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at
           ) VALUES('j','video','s','v1','e','p','succeeded',0,0)"""
    )
    conn.execute(
        """INSERT INTO video_boundary_assets(
               id,episode_video_plan_id,shot_plan_id,shot_id,role,source,path,
               qa_status,qa_json,fingerprint,created_at
           ) VALUES(
               'boundary','historical-plan','historical-shot-plan','s',
               'first_frame','STATIC_BOUNDARY_ASSET',?,'passed','{}','fp',0
           )""",
        (str(boundary_path),),
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", root)

    result = artifacts.clear_shot_artifacts("s")

    assert result["videos"] == 1
    assert result["references"] == 1
    assert not video_path.exists()
    assert not ref_path.exists()
    assert not scene_path.exists()
    assert not boundary_path.exists()
    for table in (
        "shot_versions", "shot_scenes", "reference_sets", "reference_assets",
        "video_boundary_assets", "jobs",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    shot = conn.execute(
        "SELECT adopted_version_id,approved_scene_id,mode_plan FROM shots WHERE id='s'"
    ).fetchone()
    assert tuple(shot) == (None, None, None)


def test_episode_resource_clear_supersedes_active_video_plan(
    tmp_path,
    monkeypatch,
) -> None:
    conn = _database()
    conn.execute(
        """INSERT INTO provider_video_capability_snapshots(
               id,provider,model,capabilities_json,probe_time,probe_result,
               technical_success,created_at
           ) VALUES('cap','provider','model','{}',0,'succeeded',1,0)"""
    )
    conn.execute(
        """INSERT INTO episode_video_generation_plans(
               id,episode_id,plan_revision,source_storyboard_revision_id,
               capability_snapshot_id,status,created_at
           ) VALUES('plan','e',1,'storyboard','cap','valid',0)"""
    )
    conn.execute(
        """INSERT INTO shot_video_generation_plans(
               id,episode_video_plan_id,shot_id,shot_no,planned_mode,
               capability_snapshot_id,status,created_at,updated_at
           ) VALUES(
               'shot-plan','plan','s',1,'FIRST_LAST_FRAME_MODE',
               'cap','planned',0,0
           )"""
    )
    conn.execute(
        "UPDATE shots SET mode_plan=? WHERE id='s'",
        (json.dumps({"mode": "FIRST_LAST_FRAME_MODE"}),),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES('version-audit','s',1,'prompt','idem','failed',NULL,0)"""
    )
    conn.execute(
        """INSERT INTO video_generation_attempts(
               id,shot_plan_id,version_id,attempt_no,planned_mode,actual_mode,
               status,created_at,updated_at
           ) VALUES(
               'attempt-audit','shot-plan','version-audit',1,
               'FIRST_LAST_FRAME_MODE','FIRST_LAST_FRAME_MODE','failed',0,0
           )"""
    )
    conn.execute(
        """INSERT INTO video_mode_qa_results(
               id,shot_plan_id,version_id,planned_mode,actual_mode,
               technical_success,result_json,created_at
           ) VALUES(
               'qa-audit','shot-plan','version-audit',
               'FIRST_LAST_FRAME_MODE','FIRST_LAST_FRAME_MODE',0,'{}',0
           )"""
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,
               content_json,content_hash,created_at
           ) VALUES(
               'old-checkpoint','video_supervisor_checkpoint','episode','e',1,
               'validated','T2','{}','hash',0
           )"""
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(
        artifacts.config,
        "PROJECTS_DIR",
        tmp_path / "projects",
    )

    artifacts.clear_episode_artifacts("e")

    assert conn.execute(
        "SELECT status FROM episode_video_generation_plans WHERE id='plan'"
    ).fetchone()[0] == "superseded"
    assert conn.execute(
        "SELECT status FROM shot_video_generation_plans WHERE id='shot-plan'"
    ).fetchone()[0] == "stale"
    assert conn.execute(
        "SELECT mode_plan FROM shots WHERE id='s'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='version-audit'"
    ).fetchone()[0] == "cleared"
    from app.api import _public_shot_versions

    assert _public_shot_versions(conn, "s", include_inputs=True) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM video_generation_attempts WHERE id='attempt-audit'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM video_mode_qa_results WHERE id='qa-audit'"
    ).fetchone()[0] == 1
    checkpoint = conn.execute(
        "SELECT status,stale_reason FROM artifacts WHERE id='old-checkpoint'"
    ).fetchone()
    assert tuple(checkpoint) == (
        "superseded",
        "用户已清空本集生成资源",
    )


def _seed_unsettled_provider_task(
    conn: sqlite3.Connection,
    *,
    create_state: str,
    claim_status: str,
    provider_task_id: str | None,
) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    completion_grant.ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,provider_task_id,
               status,image_inputs,created_at
           ) VALUES('v-provider','s',1,'prompt','provider-idem',?,?,'{}',1)""",
        (
            provider_task_id,
            "running" if provider_task_id else "waiting_human",
        ),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,provider_operation_id,
               provider_create_state,created_at,updated_at
           ) VALUES(
               'j-provider','future-provider-work','s','v-provider','e','p',?,
               1,'op-provider',?,1,1
           )""",
        (
            "waiting_provider" if provider_task_id else "waiting_human",
            create_state,
        ),
    )
    conn.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,project_id,episode_id,shot_id,job_id,version_id,
               origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
               amount_cny,status,created_at,updated_at
           ) VALUES(
               'op-provider','p','e','s','j-provider','v-provider',
               'e','s','j-provider','v-provider',4,?,1,1
           )""",
        (claim_status,),
    )
    conn.commit()


def _assert_provider_clear_blocked(
    exc: ValueError,
    *,
    create_state: str,
    claim_status: str,
    recovery_status: str,
) -> None:
    detail = getattr(exc, "detail", None)
    assert detail is not None
    assert detail["code"] == "PROVIDER_TASKS_NOT_TERMINAL"
    assert detail["safe_to_clear"] is False
    assert detail["resume_supported"] is True
    assert detail["blockers"] == [
        {
            "job_id": "j-provider",
            "version_id": "v-provider",
            "provider_operation_id": "op-provider",
            "provider_task_id": (
                "provider-task-1" if create_state == "accepted" else None
            ),
            "job_status": (
                "waiting_provider" if create_state == "accepted" else "waiting_human"
            ),
            "provider_create_state": create_state,
            "claim_status": claim_status,
            "amount_cny": 4.0,
            "recovery_status": recovery_status,
            "recovery_action": (
                "continue_provider_poll"
                if recovery_status == "waiting_provider"
                else "reconcile_provider_create"
            ),
        }
    ]


@pytest.mark.parametrize(
    ("create_state", "claim_status", "provider_task_id", "recovery_status"),
    [
        pytest.param(
            "accepted",
            "accepted",
            "provider-task-1",
            "waiting_provider",
            id="供应商已接单",
        ),
        pytest.param(
            "submitting",
            "reserved",
            None,
            "waiting_human",
            id="提交结果尚未落定",
        ),
        pytest.param(
            "unknown",
            "reserved",
            None,
            "waiting_human",
            id="创建结果未知",
        ),
    ],
)
def test_resource_clear_blocks_unsettled_paid_provider_tasks_without_type_rules(
    tmp_path,
    monkeypatch,
    create_state: str,
    claim_status: str,
    provider_task_id: str | None,
    recovery_status: str,
) -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state=create_state,
        claim_status=claim_status,
        provider_task_id=provider_task_id,
    )
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    with pytest.raises(ValueError) as blocked:
        artifacts.clear_shot_artifacts("s")

    _assert_provider_clear_blocked(
        blocked.value,
        create_state=create_state,
        claim_status=claim_status,
        recovery_status=recovery_status,
    )
    assert conn.execute(
        "SELECT provider_create_state FROM jobs WHERE id='j-provider'"
    ).fetchone()[0] == create_state
    assert conn.execute(
        "SELECT provider_task_id FROM shot_versions WHERE id='v-provider'"
    ).fetchone()[0] == provider_task_id
    assert conn.execute(
        "SELECT status FROM provider_video_budget_claims WHERE operation_id='op-provider'"
    ).fetchone()[0] == claim_status


def test_video_only_clear_preserves_unsettled_provider_handle_and_claim(
    tmp_path,
    monkeypatch,
) -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v-provider'",
        (json.dumps({"reference_images": [{"id": "reference-1"}]}),),
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    with pytest.raises(ValueError) as blocked:
        artifacts.clear_shot_video_assets("s")

    _assert_provider_clear_blocked(
        blocked.value,
        create_state="accepted",
        claim_status="accepted",
        recovery_status="waiting_provider",
    )
    assert conn.execute(
        "SELECT provider_task_id,status FROM shot_versions WHERE id='v-provider'"
    ).fetchone()[:] == ("provider-task-1", "running")
    assert conn.execute(
        "SELECT status FROM provider_video_budget_claims WHERE operation_id='op-provider'"
    ).fetchone()[0] == "accepted"


@pytest.mark.parametrize(
    ("claim_status", "failure_disposition"),
    [
        pytest.param("settled", None, id="费用与结果已结算"),
        pytest.param(
            "accepted",
            "external_terminal",
            id="供应商明确终态失败",
        ),
    ],
)
def test_resource_clear_allows_durable_provider_terminal_evidence(
    tmp_path,
    monkeypatch,
    claim_status: str,
    failure_disposition: str | None,
) -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status=claim_status,
        provider_task_id="provider-task-1",
    )
    conn.execute(
        """UPDATE jobs
              SET status=?,provider_failure_disposition=?
            WHERE id='j-provider'""",
        (
            "failed" if failure_disposition else "succeeded",
            failure_disposition,
        ),
    )
    conn.execute(
        "UPDATE shot_versions SET status=? WHERE id='v-provider'",
        ("failed" if failure_disposition else "succeeded",),
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    result = artifacts.clear_shot_artifacts("s")

    assert result["videos"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE id='j-provider'"
    ).fetchone()[0] == 0
    claim = conn.execute(
        """SELECT project_id,episode_id,shot_id,job_id,version_id,
                  origin_job_id,origin_version_id,status,closure_reason
             FROM provider_video_budget_claims
            WHERE operation_id='op-provider'"""
    ).fetchone()
    assert dict(claim) == {
        "project_id": "p",
        "episode_id": "e",
        "shot_id": "s",
        "job_id": None,
        "version_id": None,
        "origin_job_id": "j-provider",
        "origin_version_id": "v-provider",
        "status": (
            "closed_liability" if failure_disposition else "settled"
        ),
        "closure_reason": (
            "provider_external_terminal" if failure_disposition else None
        ),
    }
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_settled_claim_clear_keeps_project_used_budget(
    tmp_path,
    monkeypatch,
) -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="settled",
        provider_task_id="provider-task-1",
    )
    conn.execute(
        """INSERT INTO episode_video_budget_authorities(
               episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
           ) VALUES('e',0,10,'test',1,1)"""
    )
    conn.execute("UPDATE jobs SET status='succeeded' WHERE id='j-provider'")
    conn.execute(
        "UPDATE shot_versions SET status='succeeded' WHERE id='v-provider'"
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    before = completion_grant.project_video_budget_snapshot("p", conn=conn)
    result = artifacts.clear_shot_artifacts("s")
    after = completion_grant.project_video_budget_snapshot("p", conn=conn)

    assert result["videos"] == 1
    assert before["used_cny"] == 4
    assert after["used_cny"] == before["used_cny"]
    assert conn.execute(
        """SELECT status,project_id,job_id,version_id
             FROM provider_video_budget_claims
            WHERE operation_id='op-provider'"""
    ).fetchone()[:] == ("settled", "p", None, None)


def test_resource_clear_allows_unsubmitted_reservation(
    tmp_path,
    monkeypatch,
) -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="submitting",
        claim_status="reserved",
        provider_task_id=None,
    )
    conn.execute(
        """UPDATE jobs
              SET status='queued',provider_non_cancellable=0,
                  provider_create_state='not_started'
            WHERE id='j-provider'"""
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    result = artifacts.clear_shot_artifacts("s")

    assert result["videos"] == 1
    claim = conn.execute(
        """SELECT status,released_at,job_id,version_id
             FROM provider_video_budget_claims
            WHERE operation_id='op-provider'"""
    ).fetchone()
    assert claim["status"] == "released"
    assert claim["released_at"] is not None
    assert claim["job_id"] is None
    assert claim["version_id"] is None


def test_resource_clear_exposes_manual_provider_recovery_action(
    tmp_path,
    monkeypatch,
) -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    conn.execute(
        """UPDATE jobs
              SET status='waiting_human',
                  provider_failure_disposition='manual_review'
            WHERE id='j-provider'"""
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    with pytest.raises(ValueError) as blocked:
        artifacts.clear_shot_artifacts("s")

    blocker = blocked.value.detail["blockers"][0]
    assert blocker["recovery_status"] == "waiting_human"
    assert blocker["recovery_action"] == "review_provider_failure"
    assert conn.execute(
        "SELECT status FROM provider_video_budget_claims WHERE operation_id='op-provider'"
    ).fetchone()[0] == "accepted"


def test_historical_unsettled_claim_blocks_without_reusing_current_task_handle(
    tmp_path,
    monkeypatch,
) -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="settled",
        provider_task_id="provider-task-current",
    )
    conn.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,project_id,episode_id,shot_id,job_id,version_id,
               origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
               amount_cny,status,created_at,updated_at
           ) VALUES(
               'op-provider-old','p','e','s','j-provider','v-provider',
               'e','s','j-provider','v-provider',3,'accepted',0,0
           )"""
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    with pytest.raises(ValueError) as blocked:
        artifacts.clear_shot_artifacts("s")

    old_claim = blocked.value.detail["blockers"][0]
    assert old_claim["provider_operation_id"] == "op-provider-old"
    assert old_claim["provider_task_id"] is None
    assert old_claim["provider_create_state"] == "unknown"
    assert old_claim["recovery_action"] == "reconcile_provider_create"
    conn.execute(
        """UPDATE provider_video_budget_claims
              SET status='released'
            WHERE operation_id='op-provider-old'"""
    )
    conn.commit()

    result = artifacts.clear_shot_artifacts("s")

    assert result["videos"] == 1


@pytest.mark.parametrize("scope", ["shot", "episode"])
def test_staged_cleanup_rejects_unsettled_provider_task_in_callers_transaction(
    tmp_path,
    monkeypatch,
    scope: str,
) -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")
    conn.execute("BEGIN IMMEDIATE")

    with pytest.raises(ValueError) as blocked:
        if scope == "shot":
            artifacts.stage_shot_artifact_cleanup(conn, "s")
        else:
            artifacts.stage_episode_artifact_cleanup(conn, "e")

    _assert_provider_clear_blocked(
        blocked.value,
        create_state="accepted",
        claim_status="accepted",
        recovery_status="waiting_provider",
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE id='j-provider'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_video_budget_claims WHERE operation_id='op-provider'"
    ).fetchone()[0] == 1
    conn.rollback()


def test_clear_preflight_returns_recoverable_provider_state(monkeypatch) -> None:
    from app.capabilities import preflight

    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="unknown",
        claim_status="reserved",
        provider_task_id=None,
    )
    monkeypatch.setattr(preflight, "get_conn", lambda: conn)

    result = preflight.video_clear_shot(SimpleNamespace(shot_id="s"))

    assert result.allowed is False
    assert result.denial_code == "PROVIDER_TASKS_NOT_TERMINAL"
    assert result.requires_confirmation is False
    assert result.affected.extra["safe_to_clear"] is False
    assert result.affected.extra["blockers"][0]["recovery_status"] == "waiting_human"


def test_shot_clear_rejects_provider_risk_before_stopping_recoverable_job(
    tmp_path,
    monkeypatch,
) -> None:
    from app import api, worker

    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(
        api,
        "_review_upstream_snapshot",
        lambda _episode_id: {"active_upstream_runs": []},
    )
    stop_calls: list[str] = []
    monkeypatch.setattr(
        worker,
        "stop_shot_video_tasks",
        lambda shot_id: stop_calls.append(shot_id),
    )
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    with enter_handler(), pytest.raises(HTTPException) as blocked:
        asyncio.run(api.clear_shot_artifacts("s"))

    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "PROVIDER_TASKS_NOT_TERMINAL"
    assert blocked.value.detail["blockers"][0]["recovery_status"] == "waiting_provider"
    assert stop_calls == []
    assert conn.execute(
        "SELECT status,cancellation_requested,abandoned FROM jobs WHERE id='j-provider'"
    ).fetchone()[:] == ("waiting_provider", 0, 0)


def test_episode_clear_rejects_provider_risk_before_reset_or_pause(
    tmp_path,
    monkeypatch,
) -> None:
    from app import api, worker

    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(
        api,
        "_review_upstream_snapshot",
        lambda _episode_id: {"active_upstream_runs": []},
    )
    reset_calls: list[str] = []
    pause_calls: list[str] = []

    async def record_reset(episode_id: str, *, reason: str) -> dict:
        reset_calls.append(f"{episode_id}:{reason}")
        return {}

    monkeypatch.setattr(api, "reset_video_completion_state", record_reset)
    monkeypatch.setattr(
        worker,
        "pause_episode_video_tasks",
        lambda episode_id: pause_calls.append(episode_id),
    )
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    with enter_handler(), pytest.raises(HTTPException) as blocked:
        asyncio.run(api.clear_episode_artifacts("e"))

    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "PROVIDER_TASKS_NOT_TERMINAL"
    assert reset_calls == []
    assert pause_calls == []
    assert conn.execute(
        "SELECT status FROM provider_video_budget_claims WHERE operation_id='op-provider'"
    ).fetchone()[0] == "accepted"
