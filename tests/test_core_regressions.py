import asyncio
import json
import sqlite3

import pytest

from app import artifacts, db, planning, task_registry, worker
from app.capabilities import ensure_catalog_loaded, get_command_bus
from app.compiler import clip_duration_value


def test_fresh_database_enforces_parent_links_and_cascades(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','P','created',1)"
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) "
        "VALUES('e1','p1',1,'planned',1)"
    )
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES('s1','e1',1,5)"
    )
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, created_at) "
        "VALUES('v1','s1',1,'prompt','idem',1)"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO episodes(id, project_id, episode_no, status, created_at) "
            "VALUES('bad','missing',1,'planned',1)"
        )

    conn.execute("DELETE FROM projects WHERE id='p1'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM shot_versions").fetchone()[0] == 0
    conn.close()


def test_project_delete_removes_harness_evidence_and_files(tmp_path, monkeypatch) -> None:
    from app import config
    from app.domain import projects as projects_api

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "delete-project.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    media = project_root / "p1" / "scene_refs" / "scene.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")

    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)"
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'planned',1)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e1',1,5)"
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES('r1','scene_references','project','p1','SUCCEEDED','fp',1)"""
    )
    conn.execute(
        "INSERT INTO step_runs(id,run_id,step_key,status,started_at) "
        "VALUES('st1','r1','scene_references','SUCCEEDED',1)"
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,file_path,
               content_hash,created_by_step_run_id,created_at
           ) VALUES('a1','scene_reference','reference_asset','p1:scene:1',1,
                    'approved','T3',?,'hash','st1',1)""",
        (str(media),),
    )
    conn.execute(
        """INSERT INTO evaluations(
               id,artifact_id,step_run_id,evaluator_type,evaluator_name,evaluator_version,
               status,hard_gate_passed,created_at
           ) VALUES('ev1','a1','st1','model','qa','1','passed',1,1)"""
    )
    conn.execute(
        """INSERT INTO gate_decisions(
               id,artifact_id,run_id,gate_key,decision,decided_by,reason,created_at
           ) VALUES('g1','a1','r1','quality','approve','test','ok',1)"""
    )
    conn.execute(
        """INSERT INTO run_events(
               id,run_id,step_run_id,ts,event_type,severity,message
           ) VALUES('re1','r1','st1',1,'STEP_FINISHED','info','done')"""
    )
    conn.execute(
        """INSERT INTO review_action_audit(
               id,action,scope_type,scope_id,decided_by,created_at
           ) VALUES('ra1','adopt','reference_asset','p1:scene:1','test',1)"""
    )
    conn.commit()

    result = asyncio.run(projects_api._delete_project_core("p1"))

    assert result["evidence_removed"] == {"artifacts": 1, "runs": 1, "steps": 1}
    for table in (
        "projects",
        "episodes",
        "shots",
        "workflow_runs",
        "step_runs",
        "run_events",
        "artifacts",
        "evaluations",
        "gate_decisions",
        "review_action_audit",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert not (project_root / "p1").exists()


def test_observability_log_retention_keeps_active_calls() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE provider_calls(
          id INTEGER PRIMARY KEY, ts REAL, kind TEXT, status TEXT
        );
        CREATE TABLE error_logs(
          id TEXT PRIMARY KEY, ts REAL, category TEXT, code TEXT
        );
        INSERT INTO settings VALUES('provider_call_retention_days','30');
        INSERT INTO settings VALUES('error_log_retention_days','30');
        INSERT INTO provider_calls VALUES(1,0,'text','DONE');
        INSERT INTO provider_calls VALUES(2,0,'text','RUNNING');
        INSERT INTO error_logs VALUES('old',0,'system','SYS');
        """
    )

    db._prune_observability_logs(conn)

    assert [row["id"] for row in conn.execute("SELECT id FROM provider_calls ORDER BY id")] == [2]
    assert conn.execute("SELECT COUNT(*) FROM error_logs").fetchone()[0] == 0


def test_regex_planner_creates_exactly_one_episode_per_chapter(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects(
          id TEXT PRIMARY KEY, plan_status TEXT, plan_error TEXT,
          key_timeline TEXT, status TEXT
        );
        CREATE TABLE chapters(
          project_id TEXT, idx INTEGER, title TEXT, content TEXT
        );
        CREATE TABLE episodes(
          id TEXT, project_id TEXT, episode_no INTEGER, title TEXT, hook TEXT,
          cliffhanger TEXT, synopsis TEXT, source_chapters TEXT,
          target_duration_s INTEGER, status TEXT, created_at REAL
        );
        INSERT INTO projects VALUES('p1','running',NULL,NULL,'ingested');
        INSERT INTO chapters VALUES('p1',1,'第一章 起点','第一章 起点\n  A   wakes.');
        INSERT INTO chapters VALUES('p1',2,'第二章 转折','第二章 转折\nB arrives.');
        """
    )
    monkeypatch.setattr(planning, "get_conn", lambda: conn)
    monkeypatch.setattr(
        planning,
        "replan_blockers",
        lambda _conn, _project_id: {"blocked": False},
    )

    asyncio.run(planning.run_regex_plan("p1"))

    rows = conn.execute("SELECT * FROM episodes ORDER BY episode_no").fetchall()
    assert [row["title"] for row in rows] == ["第一章 起点", "第二章 转折"]
    assert [json.loads(row["source_chapters"]) for row in rows] == [[1], [2]]
    assert "  " not in rows[0]["synopsis"]
    assert conn.execute("SELECT plan_status FROM projects WHERE id='p1'").fetchone()[0] == "ready"


def test_regex_replan_skips_existing_title_only_duplicate_and_cleans_media(
    tmp_path, monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects(
          id TEXT PRIMARY KEY, plan_status TEXT, plan_error TEXT,
          key_timeline TEXT, status TEXT
        );
        CREATE TABLE chapters(
          project_id TEXT, idx INTEGER, title TEXT, content TEXT
        );
        CREATE TABLE episodes(
          id TEXT, project_id TEXT, episode_no INTEGER, title TEXT, hook TEXT,
          cliffhanger TEXT, synopsis TEXT, source_chapters TEXT,
          target_duration_s INTEGER, status TEXT, created_at REAL
        );
        INSERT INTO projects VALUES('p1','running',NULL,NULL,'ingested');
        INSERT INTO chapters VALUES(
          'p1',1926,'第一千六百二十二章 双帝之战！（上）',
          '第一千六百二十二章 双帝之战！（上） 正文 第一千六百二十二章 双帝之战！（上）'
        );
        INSERT INTO chapters VALUES(
          'p1',1927,'第一千六百二十二章双帝之战',
          '第一千六百二十二章双帝之战 魂天帝踏着血云现身，萧炎迎空而起。'
        );
        INSERT INTO episodes VALUES(
          'old','p1',99,'旧分集','','','','[99]',50,'done',1
        );
        """
    )
    rich_body = "魂天帝与萧炎连续交锋，天地在帝境力量下震颤。" * 12
    conn.execute(
        "UPDATE chapters SET content=content || ? WHERE idx=1927",
        (rich_body,),
    )
    conn.commit()
    monkeypatch.setattr(planning, "get_conn", lambda: conn)
    monkeypatch.setattr(
        planning,
        "replan_blockers",
        lambda _conn, _project_id: {"blocked": False},
    )
    project_root = tmp_path / "projects"
    old_media = project_root / "p1" / "episodes" / "99" / "old.mp4"
    old_media.parent.mkdir(parents=True)
    old_media.write_bytes(b"old")
    monkeypatch.setattr(planning.config, "PROJECTS_DIR", project_root)

    asyncio.run(planning.run_regex_plan("p1"))

    rows = conn.execute("SELECT * FROM episodes ORDER BY episode_no").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["source_chapters"]) == [1927]
    assert rows[0]["id"] != "old"
    assert not (project_root / "p1" / "episodes").exists()


def test_regex_planner_rolls_back_to_old_plan_and_keeps_media(
    tmp_path, monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects(
          id TEXT PRIMARY KEY, plan_status TEXT, plan_error TEXT,
          key_timeline TEXT, status TEXT
        );
        CREATE TABLE chapters(
          project_id TEXT, idx INTEGER, title TEXT, content TEXT
        );
        CREATE TABLE episodes(
          id TEXT PRIMARY KEY, project_id TEXT, episode_no INTEGER UNIQUE, title TEXT, hook TEXT,
          cliffhanger TEXT, synopsis TEXT, source_chapters TEXT,
          target_duration_s INTEGER, status TEXT, created_at REAL
        );
        CREATE TABLE error_logs(
          id TEXT PRIMARY KEY, ts REAL, action TEXT, category TEXT, code TEXT,
          message TEXT, traceback TEXT, context TEXT
        );
        INSERT INTO projects VALUES('p1','running',NULL,NULL,'ingested');
        INSERT INTO chapters VALUES('p1',1,'第一章','第一章 正文一');
        INSERT INTO chapters VALUES('p1',2,'第二章','第二章 正文二');
        INSERT INTO episodes VALUES(
          'ep_old','p1',9,'旧分集','','','','[9]',50,'done',1
        );
        """
    )
    monkeypatch.setattr(planning, "get_conn", lambda: conn)
    monkeypatch.setattr(
        planning,
        "replan_blockers",
        lambda _conn, _project_id: {"blocked": False},
    )
    monkeypatch.setattr(
        planning.errors,
        "record_and_format",
        lambda *_args, **_kwargs: "（测试错误）",
    )
    ids = iter(("ep_same", "ep_same"))
    monkeypatch.setattr(planning, "new_id", lambda _prefix: next(ids))
    project_root = tmp_path / "projects"
    old_media = project_root / "p1" / "episodes" / "9" / "old.mp4"
    old_media.parent.mkdir(parents=True)
    old_media.write_bytes(b"old")
    monkeypatch.setattr(planning.config, "PROJECTS_DIR", project_root)

    asyncio.run(planning.run_regex_plan("p1"))

    rows = conn.execute("SELECT id, title FROM episodes").fetchall()
    assert [(row["id"], row["title"]) for row in rows] == [("ep_old", "旧分集")]
    assert old_media.read_bytes() == b"old"
    project = conn.execute(
        "SELECT plan_status, plan_error FROM projects WHERE id='p1'"
    ).fetchone()
    assert project["plan_status"] == "failed"
    assert project["plan_error"]


def test_replan_blocks_durable_run_and_paused_budget_job(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "replan-blockers.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    project_root = tmp_path / "projects"
    monkeypatch.setattr(planning.config, "PROJECTS_DIR", project_root)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,plan_status,created_at) "
        "VALUES('p1','P','planned','running',1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) "
        "VALUES('p1',1,'第一章','第一章 新正文')"
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('old','p1',1,'旧分集','done',1)"
    )
    conn.execute(
        """INSERT INTO workflow_runs(
             id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES('run_old','storyboard','episode','old','PAUSED_EXTERNAL','fp',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
             id,kind,episode_id,project_id,status,created_at,updated_at
           ) VALUES('job_old','video','old','p1','paused_budget',1,1)"""
    )
    conn.commit()
    old_media = project_root / "p1" / "episodes" / "1" / "old.mp4"
    old_media.parent.mkdir(parents=True)
    old_media.write_bytes(b"old")

    with pytest.raises(Exception) as exc_info:
        asyncio.run(planning.start_plan("p1", replace_existing=True))

    error = exc_info.value
    assert getattr(error, "status_code", None) == 409
    assert error.detail["code"] == "REPLAN_ACTIVE_WORK"
    assert error.detail["active_media_jobs"] == 1
    assert error.detail["active_runs"][0]["id"] == "run_old"

    asyncio.run(planning.run_regex_plan("p1"))
    assert conn.execute("SELECT title FROM episodes WHERE id='old'").fetchone()["title"] == "旧分集"
    assert old_media.read_bytes() == b"old"
    state = conn.execute(
        "SELECT plan_status,plan_error FROM projects WHERE id='p1'"
    ).fetchone()
    assert state["plan_status"] == "failed"
    assert "原分集和媒体均已保留" in state["plan_error"]


def test_episode_plan_preflight_binds_destructive_impact(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "replan-preflight.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,plan_status,created_at) "
        "VALUES('p1','原著项目','planned','ready',1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) "
        "VALUES('p1',1,'第一章','第一章 正文')"
    )
    conn.execute(
        """INSERT INTO episodes(
             id,project_id,episode_no,title,status,screenplay_status,
             screenplay_artifact_id,storyboard_artifact_id,delivery_artifact_id,created_at
           ) VALUES(
             'e1','p1',1,'旧分集','done','ready','script_art','board_art','delivery_art',1
           )"""
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e1',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
             id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v1','s1',1,'prompt','idem','succeeded',1)"""
    )
    conn.execute(
        """INSERT INTO artifacts(
             id,type,scope_type,scope_id,version,status,trust_level,
             content_hash,created_at
           ) VALUES('package_art','delivery_package','episode','e1',1,
                    'candidate','candidate','hash',1)"""
    )
    conn.execute(
        """INSERT INTO delivery_packages(
             id,episode_id,artifact_id,status,package_path,manifest_json,
             quality_report_json,known_issues,created_at
           ) VALUES('pkg1','e1','package_art','waiting_human','/tmp/pkg',
                    '{}','{}','[]',1)"""
    )
    conn.commit()
    ensure_catalog_loaded()
    bus = get_command_bus()

    missing_confirmation = bus.preflight(
        "episode.plan",
        {"project_id": "p1", "replace_existing": False},
    )
    assert missing_confirmation.allowed is False
    assert missing_confirmation.denial_code == "REPLAN_CONFIRMATION_REQUIRED"

    impact = bus.preflight(
        "episode.plan",
        {"project_id": "p1", "replace_existing": True},
    )
    assert impact.allowed is True
    assert impact.requires_confirmation is True
    assert impact.affected.episodes == ["e1"]
    assert impact.affected.shot_count == 1
    assert impact.affected.invalidated_artifacts == 5
    assert impact.affected.packages == ["pkg1"]
    assert impact.estimated_cost_cny is None
    assert any("不调用模型" in warning for warning in impact.warnings)


def test_task_registry_cancels_and_waits_before_returning() -> None:
    async def scenario() -> None:
        finished = asyncio.Event()

        async def background() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        task_registry.spawn("test", "one", background(), project_id="p1")
        await asyncio.sleep(0)
        assert await task_registry.cancel_and_wait("test", "one") is True
        assert finished.is_set()
        assert not task_registry.active("test", "one")

    asyncio.run(scenario())


def test_task_registry_keeps_cancelling_task_visible_until_it_finishes() -> None:
    async def scenario() -> None:
        cancelling = asyncio.Event()
        release = asyncio.Event()

        async def background() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()
                raise

        task_registry.spawn("test", "slow-cancel", background())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(
            task_registry.cancel_and_wait("test", "slow-cancel")
        )
        await cancelling.wait()
        assert task_registry.active("test", "slow-cancel")
        release.set()
        assert await waiter is True
        assert not task_registry.active("test", "slow-cancel")

    asyncio.run(scenario())


def test_task_registry_distinguishes_process_shutdown_from_user_cancel() -> None:
    async def scenario() -> None:
        observed: list[bool] = []

        async def background() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observed.append(task_registry.shutdown_in_progress())
                raise

        task_registry.spawn("test", "shutdown", background())
        await asyncio.sleep(0)
        await task_registry.stop_all()
        assert observed == [True]
        assert task_registry.shutdown_in_progress() is False

    asyncio.run(scenario())


def test_video_derivatives_are_removed_when_visual_basis_changes(tmp_path, monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE episodes(id TEXT, project_id TEXT, episode_no INTEGER);
        CREATE TABLE shots(
          id TEXT, episode_id TEXT, shot_no INTEGER,
          adopted_version_id TEXT, mode_plan TEXT
        );
        CREATE TABLE shot_versions(id TEXT, shot_id TEXT, video_path TEXT);
        CREATE TABLE jobs(shot_id TEXT, kind TEXT);
        INSERT INTO episodes VALUES('e1','p1',1);
        INSERT INTO shots VALUES('s1','e1',1,'v1','{}');
        """
    )
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p1" / "episodes" / "1" / "shots" / "1"
    references = shot_dir / "references"
    references.mkdir(parents=True)
    (references / "ref.png").write_bytes(b"ref")
    video = shot_dir / "v1.mp4"
    video.write_bytes(b"video")
    final = project_root / "p1" / "episodes" / "1" / "final" / "episode.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"final")
    conn.execute("INSERT INTO shot_versions VALUES('v1','s1',?)", (str(video),))
    conn.execute("INSERT INTO jobs VALUES('s1','video')")
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", project_root)

    result = worker.invalidate_shot_video_derivatives("s1")

    assert result == {"shot_id": "s1", "videos": 1, "references": 1}
    assert not references.exists() and not video.exists() and not final.exists()
    row = conn.execute("SELECT adopted_version_id, mode_plan FROM shots WHERE id='s1'").fetchone()
    assert row["adopted_version_id"] is None and row["mode_plan"] is None


def test_manual_duration_inputs_clamp_to_supported_range() -> None:
    assert clip_duration_value(None) == 5
    assert clip_duration_value(4) == 5
    assert clip_duration_value(7) == 7
    assert clip_duration_value("9") == 9
    assert clip_duration_value(15) == 10
