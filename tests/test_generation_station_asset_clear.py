import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import artifacts, completion_grant, db
from app.domain import common as domain_common
from tests.conftest import patch_worker_everywhere, patch_api_everywhere
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
            "project_id": "p",
            "episode_id": "e",
            "shot_id": "s",
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


def test_cancelled_pre_transport_provider_claim_is_released_before_clear() -> None:
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="submitting",
        claim_status="reserved",
        provider_task_id=None,
    )
    conn.execute(
        """UPDATE jobs
              SET status='cancelled',cancellation_requested=1,abandoned=0,
                  provider_non_cancellable=0
            WHERE id='j-provider'"""
    )
    conn.commit()

    clearance = completion_grant.prepare_provider_tasks_for_clear(
        project_id="p",
        conn=conn,
    )

    assert clearance["safe_to_clear"] is True
    claim = conn.execute(
        """SELECT status,released_at
             FROM provider_video_budget_claims
            WHERE operation_id='op-provider'"""
    ).fetchone()
    assert claim["status"] == "released"
    assert claim["released_at"] is not None


def test_project_delete_reconcile_settles_remote_terminal_without_download(
    monkeypatch,
) -> None:
    from app import hiagent

    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )

    async def unexpected_poll(*_args, **_kwargs) -> dict:
        raise AssertionError("durable terminal observation must avoid provider polling")

    monkeypatch.setattr(hiagent, "poll_video_task", unexpected_poll)

    result = asyncio.run(
        completion_grant.reconcile_project_provider_tasks_for_clear(
            "p",
            conn=conn,
            terminal_observations={
                "provider-task-1": {"status": "succeeded"},
            },
            evidence_source="sha256:test-snapshot",
        )
    )

    assert result["reconciled_job_ids"] == ["j-provider"]
    assert result["clearance"]["safe_to_clear"] is True
    assert conn.execute(
        "SELECT status FROM provider_video_budget_claims WHERE operation_id='op-provider'"
    ).fetchone()["status"] == "settled"
    version = conn.execute(
        "SELECT status,video_path,error FROM shot_versions WHERE id='v-provider'"
    ).fetchone()
    assert dict(version) == {
        "status": "quarantined",
        "video_path": None,
        "error": (
            "已核对供应商任务成功终态；费用已结算，"
            "结果保持隔离且不可采用；核对证据=sha256:test-snapshot"
        ),
    }


def test_episode_scoped_reconcile_settles_remote_terminal_without_download(
    monkeypatch,
) -> None:
    """分镜台「清空视频提示词」撞上 409 后新接的恢复入口用的就是这条路径：
    只按 episode_id 缩小范围（不是整项目删除），同样只在供应商自己确认终态
    时才结算，不下载、不采用、不新建任务。"""
    from app import hiagent

    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )

    async def unexpected_poll(*_args, **_kwargs) -> dict:
        raise AssertionError("durable terminal observation must avoid provider polling")

    monkeypatch.setattr(hiagent, "poll_video_task", unexpected_poll)

    result = asyncio.run(
        completion_grant.reconcile_provider_tasks_for_clear(
            episode_id="e",
            conn=conn,
            terminal_observations={
                "provider-task-1": {"status": "failed"},
            },
            evidence_source="sha256:episode-scoped-test",
        )
    )

    assert result["reconciled_job_ids"] == ["j-provider"]
    assert result["clearance"]["safe_to_clear"] is True
    assert conn.execute(
        "SELECT status FROM provider_video_budget_claims WHERE operation_id='op-provider'"
    ).fetchone()["status"] == "settled"
    assert conn.execute(
        "SELECT status,error FROM jobs WHERE id='j-provider'"
    ).fetchone()["status"] == "failed"


def test_reconcile_failed_terminal_settles_zero_cost_not_reserved_estimate() -> None:
    """真实事故复盘（proj_1fce17f77010 镜 5/6）：供应商确认终态失败=零产出，
    此前这条路径无条件按预留估算（``amount_cny``）结算，把没有任何产出的
    失败版本记成了全价——``video_path`` 全程为空，``cost_cny`` 却是 12。修复
    后 failed 分支必须结算为 0，与全仓其余「供应商确认失败 ->
    settle_budget(0, success=False)」的既有口径一致。"""
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES('b-provider','j-provider','episode','e',4,'reserved',1)"""
    )
    conn.commit()

    result = asyncio.run(
        completion_grant.reconcile_provider_tasks_for_clear(
            episode_id="e",
            conn=conn,
            terminal_observations={"provider-task-1": {"status": "failed"}},
            evidence_source="sha256:zero-output-test",
        )
    )

    assert result["reconciled_job_ids"] == ["j-provider"]
    version = conn.execute(
        "SELECT status,video_path,cost_cny FROM shot_versions WHERE id='v-provider'"
    ).fetchone()
    assert dict(version) == {"status": "failed", "video_path": None, "cost_cny": 0.0}
    reservation = conn.execute(
        "SELECT status,actual_cost_cny FROM budget_reservations WHERE job_id='j-provider'"
    ).fetchone()
    assert dict(reservation) == {"status": "settled", "actual_cost_cny": 0.0}


def test_reconcile_succeeded_terminal_still_charges_reserved_estimate() -> None:
    """回归安全网：succeeded（隔离不采用）分支不受上面那条修复影响，仍按预留
    估算保守计费——不能因为修了 failed 分支就把这个也一起改没了。"""
    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES('b-provider','j-provider','episode','e',4,'reserved',1)"""
    )
    conn.commit()

    result = asyncio.run(
        completion_grant.reconcile_provider_tasks_for_clear(
            episode_id="e",
            conn=conn,
            terminal_observations={"provider-task-1": {"status": "succeeded"}},
            evidence_source="sha256:quarantined-test",
        )
    )

    assert result["reconciled_job_ids"] == ["j-provider"]
    assert conn.execute(
        "SELECT cost_cny FROM shot_versions WHERE id='v-provider'"
    ).fetchone()["cost_cny"] == 4.0
    assert conn.execute(
        "SELECT actual_cost_cny FROM budget_reservations WHERE job_id='j-provider'"
    ).fetchone()["actual_cost_cny"] == 4.0


def test_close_superseded_unclaimed_video_jobs_closes_only_provably_uncharged_orphans() -> None:
    """paused_budget/waiting_human 等孤儿任务只有在本地证据已经证明「不可能
    产生费用」（从未提交给供应商、没有在途 claim）且所属镜头已经换到别的
    成功版本时才收口；任何还有供应商footprint 或在途 claim 的任务原样保留，
    不能被这条路径误伤——这是 EP2 那条 paused_budget 孤儿任务的真实形状。"""
    conn = _database()
    completion_grant.ensure_video_budget_authority_tables(conn)
    # 镜头已采用的成功版本（_database() 里 shots.adopted_version_id='v1'，
    # 但没有对应的 shot_versions 行，这里补上）。
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v1','s',3,'prompt','key-v1','succeeded',1)"""
    )
    # 孤儿任务：预算门禁挡下，从未提交给供应商，且所属镜头早已换到 v1。
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v-orphan','s',4,'prompt','key-orphan','paused_budget',2)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,provider_operation_id,
               provider_create_state,cancellation_requested,abandoned,
               created_at,updated_at
           ) VALUES(
               'j-orphan','video','s','v-orphan','e','p','paused_budget',
               0,'op-orphan','not_started',0,0,2,2
           )"""
    )
    # 对照组：仍带供应商 footprint 与在途 claim 的任务，即便镜头也已换版，
    # 也绝不能被这条路径关闭——它复用既有 fixture，job/claim 都挂在 op-provider。
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    conn.commit()

    closed = completion_grant.close_superseded_unclaimed_video_jobs("e", conn=conn)

    assert closed == ["j-orphan"]
    orphan_job = conn.execute(
        "SELECT status,cancellation_requested,abandoned,reserved_cost_cny FROM jobs WHERE id='j-orphan'"
    ).fetchone()
    assert dict(orphan_job) == {
        "status": "stale", "cancellation_requested": 1, "abandoned": 1,
        "reserved_cost_cny": 0.0,
    }
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v-orphan'"
    ).fetchone()["status"] == "stale"
    # 对照组任务与其 claim 原样保留。
    assert conn.execute(
        "SELECT status FROM jobs WHERE id='j-provider'"
    ).fetchone()["status"] == "waiting_provider"
    assert conn.execute(
        "SELECT status FROM provider_video_budget_claims WHERE operation_id='op-provider'"
    ).fetchone()["status"] == "accepted"


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


def test_confirmed_technical_resubmission_closes_old_liability_and_can_clear(
    tmp_path,
    monkeypatch,
) -> None:
    from app import monitoring, system_api

    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    conn.execute(
        """UPDATE jobs
              SET kind='video',status='waiting_human',
                  provider_failure_category='technical',
                  provider_failure_kind='provider_task_not_found',
                  provider_failure_disposition='manual_review',
                  provider_failure_retryable=0,
                  reason_code='VIDEO_PROVIDER_TASK_NOT_FOUND'
            WHERE id='j-provider'"""
    )
    conn.execute(
        "UPDATE shot_versions SET status='waiting_human' WHERE id='v-provider'"
    )
    conn.execute(
        """INSERT INTO episode_video_budget_authorities(
               episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
           ) VALUES('e',0,20,'test',1,1)"""
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", tmp_path / "projects")

    retried = system_api.retry_job(
        "j-provider",
        {"allow_new_submission": True},
    )
    new_operation_id = retried["job"]["provider_operation_id"]
    claims_after_retry = conn.execute(
        """SELECT operation_id,status,closure_reason
             FROM provider_video_budget_claims
            ORDER BY created_at,operation_id"""
    ).fetchall()

    assert retried["retryability"]["action"] == (
        "new_submission_after_technical_failure"
    )
    assert [dict(row) for row in claims_after_retry] == [
        {
            "operation_id": "op-provider",
            "status": "closed_liability",
            "closure_reason": "technical_failure_resubmission_confirmed",
        },
        {
            "operation_id": new_operation_id,
            "status": "reserved",
            "closure_reason": None,
        },
    ]
    assert completion_grant.project_video_budget_snapshot(
        "p",
        conn=conn,
    )["used_cny"] == 8

    cleared = artifacts.clear_shot_artifacts("s")

    assert cleared["videos"] == 1
    final_claims = conn.execute(
        """SELECT operation_id,status,job_id,version_id
             FROM provider_video_budget_claims
            ORDER BY created_at,operation_id"""
    ).fetchall()
    assert [dict(row) for row in final_claims] == [
        {
            "operation_id": "op-provider",
            "status": "closed_liability",
            "job_id": None,
            "version_id": None,
        },
        {
            "operation_id": new_operation_id,
            "status": "released",
            "job_id": None,
            "version_id": None,
        },
    ]
    assert completion_grant.project_video_budget_snapshot(
        "p",
        conn=conn,
    )["used_cny"] == 4


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
    # video_clear_shot now resolves its shot through
    # app.domain.common.owned_shot_row (P0-1 ownership fold) -- that helper
    # calls app.domain.common's own get_conn binding, a separate name from
    # preflight.get_conn even though both originally point at the same
    # function, so it needs its own patch onto this test's isolated conn too.
    monkeypatch.setattr(domain_common, "get_conn", lambda: conn)

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
    from app import api

    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch,
        "_review_upstream_snapshot",
        lambda _episode_id: {"active_upstream_runs": []},
    )
    stop_calls: list[str] = []
    patch_worker_everywhere(monkeypatch,
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
    from app import api

    conn = _database()
    _seed_unsettled_provider_task(
        conn,
        create_state="accepted",
        claim_status="accepted",
        provider_task_id="provider-task-1",
    )
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch,
        "_review_upstream_snapshot",
        lambda _episode_id: {"active_upstream_runs": []},
    )
    reset_calls: list[str] = []
    pause_calls: list[str] = []

    async def record_reset(episode_id: str, *, reason: str) -> dict:
        reset_calls.append(f"{episode_id}:{reason}")
        return {}

    patch_api_everywhere(monkeypatch, "reset_video_completion_state", record_reset)
    patch_worker_everywhere(monkeypatch,
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


def test_clearance_chain_requires_an_explicit_connection() -> None:
    """清空判据链不得给 conn 留默认值。

    这条链是「删项目 / 清空整集 / 清空单镜」的准入闸门。调用方漏传时若回退到
    自己开的连接，读到的是另一个事务里看不见的状态——safe_to_clear 与事务内的
    事实不一致，而且不报错。必传之后漏传在调用那一刻就是 TypeError。
    （CLAUDE.md「Ownership Must Be Explicit」：可选参数是缺陷的温床。）
    """
    import inspect

    from app import provider_task_clearance as pc

    chain = (
        pc._provider_task_clearance_evaluation,
        pc.provider_task_clearance_snapshot,
        pc.assert_provider_tasks_clearable,
        pc.prepare_provider_tasks_for_clear,
    )
    for fn in chain:
        param = inspect.signature(fn).parameters["conn"]
        assert param.default is inspect.Parameter.empty, (
            f"{fn.__name__} 的 conn 又有默认值了；漏传会静默读到另一个事务的状态"
        )

    # 行为红绿：漏传立刻炸，而不是等到线上读到错状态。
    with pytest.raises(TypeError):
        pc.provider_task_clearance_snapshot(project_id="proj_x")
    with pytest.raises(TypeError):
        pc.assert_provider_tasks_clearable(project_id="proj_x")
    with pytest.raises(TypeError):
        pc.prepare_provider_tasks_for_clear(project_id="proj_x")


def test_clearance_chain_does_not_fall_back_to_its_own_connection() -> None:
    """源码级守卫：链上函数体里不得再出现 `conn or get_conn()` 兜底。"""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "app" / "provider_task_clearance.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and any(
            isinstance(v, ast.Call)
            and getattr(v.func, "id", None) == "get_conn"
            for v in node.values
        )
    ]
    assert not offenders, f"第 {offenders} 行出现了 `conn or get_conn()` 兜底"


def test_planned_episode_can_be_cleared_without_active_runs(monkeypatch) -> None:
    """'planned' 的分集没有任何在途运行时必须可清。

    'planned' 是**还没开始**的初始态，而 _clear_episode_artifacts 自己收尾时
    正是把 status 写回 'planned'——把它算进"在写"会让清完之后的下一次清空被
    自己刚造出的状态挡住，报「编剧或分镜任务仍在写入，清空已原子拒绝」，而
    根本没有这样的任务可停。
    """
    conn = _database()
    conn.execute("UPDATE episodes SET status='planned' WHERE id='e'")
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)

    artifacts._begin_clear_transaction(conn, "e")


def test_scripting_episode_clear_still_fails_closed(monkeypatch) -> None:
    """真正在写的状态继续原子拒绝。"""
    conn = _database()
    conn.execute("UPDATE episodes SET status='scripting' WHERE id='e'")
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)

    with pytest.raises(ValueError, match="仍在写入"):
        artifacts._begin_clear_transaction(conn, "e")
