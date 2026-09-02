import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app import artifacts, db, planning, task_registry, worker
from app.capabilities import get_command_bus
from app.capabilities.loader import ensure_catalog_loaded
from app.compiler import clip_duration_value
from tests.conftest import patch_projects_everywhere


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


def _insert_project_with_harness_evidence(conn, project_root, project_id: str) -> Path:
    """回收站测试共用的夹具：一个带完整 Harness 证据链的项目，外加一份磁盘产物。

    所有子表 id 都按 project_id 加前缀，保证同一个 conn 里插入多个项目不会撞
    主键（回收站到期清理测试需要同时存在一个"未到期"和一个"已到期"项目）。
    """
    ep_id, shot_id, run_id, step_id, art_id = (
        f"{project_id}-e1", f"{project_id}-s1", f"{project_id}-r1",
        f"{project_id}-st1", f"{project_id}-a1",
    )
    media = project_root / project_id / "scene_refs" / "scene.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")

    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES(?,?,?,?)",
        (project_id, "P", "planned", 1),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES(?,?,1,'planned',1)",
        (ep_id, project_id),
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,1,5)",
        (shot_id, ep_id),
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES(?,'scene_references','project',?,'SUCCEEDED','fp',1)""",
        (run_id, project_id),
    )
    conn.execute(
        "INSERT INTO step_runs(id,run_id,step_key,status,started_at) "
        "VALUES(?,?,'scene_references','SUCCEEDED',1)",
        (step_id, run_id),
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,file_path,
               content_hash,created_by_step_run_id,created_at
           ) VALUES(?,'scene_reference',?,?,1,
                    'approved','T3',?,?,?,1)""",
        (art_id, "reference_asset", f"{project_id}:scene:1", str(media), f"hash-{project_id}", step_id),
    )
    conn.execute(
        """INSERT INTO evaluations(
               id,artifact_id,step_run_id,evaluator_type,evaluator_name,evaluator_version,
               status,hard_gate_passed,created_at
           ) VALUES(?,?,?,'model','qa','1','passed',1,1)""",
        (f"{project_id}-ev1", art_id, step_id),
    )
    conn.execute(
        """INSERT INTO gate_decisions(
               id,artifact_id,run_id,gate_key,decision,decided_by,reason,created_at
           ) VALUES(?,?,?,'quality','approve','test','ok',1)""",
        (f"{project_id}-g1", art_id, run_id),
    )
    conn.execute(
        """INSERT INTO run_events(
               id,run_id,step_run_id,ts,event_type,severity,message
           ) VALUES(?,?,?,1,'STEP_FINISHED','info','done')""",
        (f"{project_id}-re1", run_id, step_id),
    )
    conn.execute(
        """INSERT INTO review_action_audit(
               id,action,scope_type,scope_id,decided_by,created_at
           ) VALUES(?,'adopt','reference_asset',?,'test',1)""",
        (f"{project_id}-ra1", f"{project_id}:scene:1"),
    )
    conn.commit()
    return media


def test_project_soft_delete_keeps_rows_and_files_then_purge_removes_them(
    tmp_path, monkeypatch,
) -> None:
    """project.delete 现在是软删除：移入回收站后数据库行与磁盘产物原样保留；
    只有随后彻底清理（``_purge_project_core``，回收站到期或用户手动触发）才会
    真正删库删文件——这是 CLAUDE.md 要求的「界面承诺与实际行为一致」在测试
    层面的落地：按钮叫「删除」，回收站里项目却必须完好无损。"""
    from app import config
    from app.domain import projects as projects_api

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "delete-project.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    media = _insert_project_with_harness_evidence(conn, project_root, "p1")

    result = asyncio.run(projects_api._delete_project_core("p1"))
    assert result["deleted"] == "p1"
    assert result["deleted_at"] is not None

    # 软删除阶段：数据库行与磁盘产物一个都不能少——用独立连接验证，不是同一
    # 连接读自己刚写的东西。
    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    row = verify_conn.execute("SELECT deleted_at FROM projects WHERE id='p1'").fetchone()
    assert row is not None and row["deleted_at"] is not None
    for table in ("episodes", "shots", "workflow_runs", "step_runs", "artifacts"):
        assert verify_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
    assert media.exists()
    assert (project_root / "p1").exists()
    verify_conn.close()

    # 正常列表与项目详情入口必须挡住：deleted_at 非空即视为不存在
    # （app.domain.common._project_or_404 是 domain 包共用的项目存在性入口）。
    from app.domain.common import _project_or_404
    with pytest.raises(Exception):
        _project_or_404("p1")

    # 彻底清理：数据库行与磁盘产物才真正消失。
    purge_result = asyncio.run(projects_api._purge_project_core("p1"))
    assert purge_result["evidence_removed"] == {"artifacts": 1, "runs": 1, "steps": 1}
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


def test_project_restore_clears_deleted_at_without_touching_data(tmp_path, monkeypatch) -> None:
    from app import config
    from app.domain import projects as projects_api

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "restore-project.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    _insert_project_with_harness_evidence(conn, project_root, "p1")

    asyncio.run(projects_api._delete_project_core("p1"))
    restore_result = asyncio.run(projects_api._restore_project_core("p1"))
    assert restore_result == {"restored": "p1"}

    row = conn.execute("SELECT deleted_at FROM projects WHERE id='p1'").fetchone()
    assert row["deleted_at"] is None
    # 恢复之后跟一个从未删除过的项目没有区别：常规入口重新放行。
    from app.domain.common import _project_or_404
    assert _project_or_404("p1")["id"] == "p1"
    for table in ("episodes", "shots", "workflow_runs", "artifacts"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0


def test_purge_expired_deleted_projects_only_after_24h(tmp_path, monkeypatch) -> None:
    """自动清理的判据是 deleted_at 时间戳，不是内存计时器：23 小时前删除的
    项目原样保留，25 小时前删除的项目被彻底清理——且这条判据必须在后端
    "重启"（这里用新连接模拟）后依然成立。"""
    from app import config
    from app.domain import projects as projects_api

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "sweep-project.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    _insert_project_with_harness_evidence(conn, project_root, "p-fresh")
    _insert_project_with_harness_evidence(conn, project_root, "p-stale")

    now = db.now()
    conn.execute("UPDATE projects SET deleted_at=? WHERE id='p-fresh'", (now - 23 * 3600,))
    conn.execute("UPDATE projects SET deleted_at=? WHERE id='p-stale'", (now - 25 * 3600,))
    conn.commit()

    # 模拟"重启后"：清空进程内连接缓存，sweep 只能靠 deleted_at 时间戳工作。
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    sweep_result = asyncio.run(projects_api.sweep_expired_deleted_projects())

    assert sweep_result["purged"] == ["p-stale"]
    assert sweep_result["failed"] == []

    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    assert verify_conn.execute("SELECT COUNT(*) FROM projects WHERE id='p-fresh'").fetchone()[0] == 1
    assert verify_conn.execute("SELECT COUNT(*) FROM projects WHERE id='p-stale'").fetchone()[0] == 0
    verify_conn.close()
    assert (project_root / "p-fresh").exists()
    assert not (project_root / "p-stale").exists()


def test_project_evidence_delete_chunks_large_workflow_frontier(tmp_path, monkeypatch) -> None:
    from app.domain import projects as projects_api

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "delete-project-large-frontier.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()

    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p-big','P','planned',1)"
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES('run-root','scene_references','project','p-big','SUCCEEDED','fp',1)"""
    )
    child_count = 30
    conn.executemany(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,parent_run_id,status,
               input_fingerprint,updated_at
           ) VALUES(?,?,?,?,?,'SUCCEEDED','fp',1)""",
        [
            (
                f"run-child-{index}",
                "scene_references",
                "internal",
                f"external:{index}",
                "run-root",
            )
            for index in range(child_count)
        ],
    )
    run_ids = ["run-root", *(f"run-child-{index}" for index in range(child_count))]
    conn.executemany(
        "INSERT INTO step_runs(id,run_id,step_key,status,started_at) "
        "VALUES(?,?,?, 'SUCCEEDED', 1)",
        [(f"step-{index}", run_id, "scene_references") for index, run_id in enumerate(run_ids)],
    )
    conn.executemany(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,
               content_hash,created_by_step_run_id,created_at
           ) VALUES(?,?,?,?,1,'approved','T3',?,?,1)""",
        [
            (
                f"artifact-{index}",
                "trace",
                "internal",
                f"external:{index}",
                f"hash-{index}",
                f"step-{index}",
            )
            for index in range(len(run_ids))
        ],
    )
    conn.executemany(
        """INSERT INTO evaluations(
               id,artifact_id,step_run_id,evaluator_type,evaluator_name,evaluator_version,
               status,hard_gate_passed,created_at
           ) VALUES(?,?,?,?, 'qa', '1', 'passed', 1, 1)""",
        [
            (f"eval-{index}", f"artifact-{index}", f"step-{index}", "rule")
            for index in range(len(run_ids))
        ],
    )
    conn.executemany(
        """INSERT INTO gate_decisions(
               id,artifact_id,run_id,gate_key,decision,decided_by,reason,created_at
           ) VALUES(?,?,?,?, 'approve', 'test', 'ok', 1)""",
        [
            (f"gate-{index}", f"artifact-{index}", run_id, "quality")
            for index, run_id in enumerate(run_ids)
        ],
    )
    conn.executemany(
        """INSERT INTO run_events(
               id,run_id,step_run_id,ts,event_type,severity,message
           ) VALUES(?,?,?,?, 'STEP_FINISHED', 'info', 'done')""",
        [
            (f"event-{index}", run_id, f"step-{index}", 1)
            for index, run_id in enumerate(run_ids)
        ],
    )
    conn.execute(
        """INSERT INTO provider_calls(
               id,ts,kind,status,run_id,step_run_id
           ) VALUES(1,1,'text','INTERRUPTED',?,?)""",
        (run_ids[0], "step-0"),
    )
    conn.execute(
        """INSERT INTO provider_calls(
               id,ts,kind,status,run_id,step_run_id,supersedes_call_id
           ) VALUES(2,2,'text','SUCCEEDED',?,?,1)""",
        (run_ids[1], "step-1"),
    )
    conn.execute("UPDATE provider_calls SET superseded_by_call_id=2 WHERE id=1")
    conn.execute(
        """INSERT INTO provider_calls(
               id,ts,kind,status,supersedes_call_id
           ) VALUES(3,3,'text','SUCCEEDED',1)"""
    )
    conn.executemany(
        """INSERT INTO review_action_audit(
               id,action,scope_type,scope_id,decided_by,created_at
           ) VALUES(?,?,?,?, 'test', 1)""",
        [
            ("audit-project", "adopt", "project", "p-big"),
            ("audit-prefixed", "adopt", "reference_asset", "p-big:scene:1"),
        ],
    )
    conn.commit()

    class LimitedConn:
        def __init__(self, wrapped, max_params: int) -> None:
            self.wrapped = wrapped
            self.max_params = max_params

        def execute(self, sql, params=()):
            params = tuple(params or ())
            assert len(params) <= self.max_params
            return self.wrapped.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    patch_projects_everywhere(monkeypatch, "_SQLITE_IN_CHUNK_SIZE", 8)
    result = projects_api._delete_project_evidence(LimitedConn(conn, 20), "p-big")

    assert result == {"artifacts": len(run_ids), "runs": len(run_ids), "steps": len(run_ids)}
    for table in (
        "workflow_runs",
        "step_runs",
        "run_events",
        "artifacts",
        "evaluations",
        "gate_decisions",
        "review_action_audit",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    remaining_provider_calls = [
        dict(row) for row in conn.execute(
            "SELECT id,supersedes_call_id,superseded_by_call_id FROM provider_calls"
        ).fetchall()
    ]
    assert remaining_provider_calls == [
        {"id": 3, "supersedes_call_id": None, "superseded_by_call_id": None}
    ]


def test_episode_delete_removes_only_target_and_all_downstream_assets(tmp_path, monkeypatch) -> None:
    from app import config
    from app.domain import projects as projects_api

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "delete-episode.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    episode_root = project_root / "p1" / "episodes" / "1"
    video = episode_root / "shots" / "1" / "candidate.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    survivor_root = project_root / "p1" / "episodes" / "2"
    survivor_video = survivor_root / "shots" / "1" / "candidate.mp4"
    survivor_reference = survivor_root / "shots" / "1" / "references" / "hero.jpg"
    survivor_video.parent.mkdir(parents=True)
    survivor_reference.parent.mkdir(parents=True)
    survivor_video.write_bytes(b"keep-video")
    survivor_reference.write_bytes(b"keep-reference")

    conn.execute(
        "INSERT INTO projects(id,name,status,plan_status,created_at) "
        "VALUES('p1','P','planned','ready',1)"
    )
    conn.executemany(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,status,screenplay_json,
               storyboard_outline_json,created_at
           ) VALUES(?,?,?,?,?,?,?,1)""",
        [
            ('e1', 'p1', 1, 'Delete me', 'planned', None, None),
            (
                'e2', 'p1', 2, 'Keep me', 'planned',
                json.dumps({'episode_no': 2, 'title': 'Keep me'}),
                json.dumps({'episode_no': 2, 'shots': []}),
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,1,5)",
        [('s1', 'e1'), ('s2', 'e2')],
    )
    conn.executemany(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,
               technical_validation_json,image_inputs,created_at
           ) VALUES(?,?,1,'prompt',?,'succeeded',?,?,?,1)""",
        [
            ('v1', 's1', 'idem-delete', str(video), None, None),
            (
                'v2', 's2', 'idem-keep', str(survivor_video),
                json.dumps({'video_path': str(survivor_video)}),
                json.dumps({'image_path': str(survivor_reference)}),
            ),
        ],
    )
    conn.execute(
        """INSERT INTO screenplay_drafts(
               id,episode_id,content_json,dirty_at,updated_at
           ) VALUES('draft2','e2',?,1,1)""",
        (json.dumps({'episode_no': 2, 'title': 'Draft'}),),
    )
    conn.executemany(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,ep_end,created_at
           ) VALUES(?,?,?,?,?,1)""",
        [
            ('cp-delete', 'p1', 'Deleted only', 1, 1),
            ('cp-keep', 'p1', 'Hero', 2, None),
        ],
    )
    conn.execute(
        """INSERT INTO scene_references(
               id,project_id,scene_name,ep_start,created_at
           ) VALUES('scene-keep','p1','Courtyard',2,1)"""
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES('r1','screenplay','episode','e1','SUCCEEDED','fp',1)"""
    )
    conn.execute(
        "INSERT INTO step_runs(id,run_id,step_key,status,started_at) "
        "VALUES('st1','r1','screenplay','SUCCEEDED',1)"
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,
               content_hash,created_by_step_run_id,created_at
           ) VALUES('a1','episode_screenplay','episode','e1',1,
                    'approved','T3','hash','st1',1)"""
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,file_path,
               content_hash,created_at
           ) VALUES('a2','shot_video','shot','s2',1,
                    'approved','T3',?,'keep-hash',1)""",
        (str(survivor_video),),
    )
    conn.execute(
        """INSERT INTO evaluations(
               id,artifact_id,evaluator_type,evaluator_name,evaluator_version,
               status,hard_gate_passed,evidence_json,created_at
           ) VALUES('ev2','a2','rule','video','1','passed',1,?,1)""",
        (json.dumps({'video_path': str(survivor_video)}),),
    )
    conn.execute(
        """INSERT INTO reference_sets(
               id,shot_id,fingerprint,created_at,updated_at
           ) VALUES('set2','s2','fp-keep',1,1)"""
    )
    conn.execute(
        """INSERT INTO reference_assets(
               id,reference_set_id,asset_type,path,dependency_manifest_json,created_at
           ) VALUES('ref2','set2','character',?,?,1)""",
        (str(survivor_reference), json.dumps({'path': str(survivor_reference)})),
    )
    conn.commit()

    result = asyncio.run(projects_api._delete_episode_core('e1'))

    assert result['deleted'] == 'e1'
    assert result['evidence_removed'] == {'artifacts': 1, 'runs': 1, 'steps': 1}
    assert result['renumbered'] == 1
    kept = dict(conn.execute("SELECT * FROM episodes WHERE id='e2'").fetchone())
    assert kept['episode_no'] == 1
    # Published projections remain byte-identical to their content-addressed
    # artifacts; the authoritative display number lives on episodes. Drafts,
    # which have no certificate lineage, are updated below.
    assert json.loads(kept['screenplay_json'])['episode_no'] == 2
    assert json.loads(kept['storyboard_outline_json'])['episode_no'] == 2
    draft = conn.execute(
        "SELECT content_json FROM screenplay_drafts WHERE episode_id='e2'"
    ).fetchone()
    assert json.loads(draft['content_json'])['episode_no'] == 1
    assert conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM shot_versions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM step_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    moved_root = project_root / 'p1' / 'episodes' / '1'
    assert (moved_root / 'shots' / '1' / 'candidate.mp4').read_bytes() == b'keep-video'
    assert (moved_root / 'shots' / '1' / 'references' / 'hero.jpg').read_bytes() == b'keep-reference'
    moved_prefix = str(moved_root)
    assert conn.execute("SELECT video_path FROM shot_versions WHERE id='v2'").fetchone()['video_path'].startswith(moved_prefix)
    assert moved_prefix in conn.execute("SELECT image_inputs FROM shot_versions WHERE id='v2'").fetchone()['image_inputs']
    assert conn.execute("SELECT file_path FROM artifacts WHERE id='a2'").fetchone()['file_path'].startswith(moved_prefix)
    assert moved_prefix in conn.execute("SELECT evidence_json FROM evaluations WHERE id='ev2'").fetchone()['evidence_json']
    assert conn.execute("SELECT path FROM reference_assets WHERE id='ref2'").fetchone()['path'].startswith(moved_prefix)
    assert conn.execute("SELECT COUNT(*) FROM character_portraits WHERE id='cp-delete'").fetchone()[0] == 0
    assert conn.execute("SELECT ep_start FROM character_portraits WHERE id='cp-keep'").fetchone()['ep_start'] == 1
    assert conn.execute("SELECT ep_start FROM scene_references WHERE id='scene-keep'").fetchone()['ep_start'] == 1
    assert [ep['id'] for ep in projects_api.project_detail('p1', view='picker')['episodes']] == ['e2']
    assert [ep['id'] for ep in projects_api.project_detail('p1', view='picker_generation')['episodes']] == ['e2']
    with pytest.raises(projects_api.HTTPException) as exc:
        projects_api._episode_or_404('e1')
    assert exc.value.status_code == 404


def test_episode_delete_refuses_while_replanning(tmp_path, monkeypatch) -> None:
    from app.domain import projects as projects_api

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "delete-episode-running-plan.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,plan_status,created_at) "
        "VALUES('p1','P','planned','running',1)"
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'planned',1)"
    )
    conn.commit()

    with pytest.raises(projects_api.HTTPException) as exc:
        asyncio.run(projects_api._delete_episode_core('e1'))

    assert exc.value.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1


def test_episode_delete_compacts_numbers_after_a_middle_episode(tmp_path, monkeypatch) -> None:
    from app import config
    from app.domain import projects as projects_api

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "delete-middle-episode.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    conn.execute(
        "INSERT INTO projects(id,name,status,plan_status,created_at) "
        "VALUES('p1','P','planned','ready',1)"
    )
    conn.executemany(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES(?,'p1',?,'planned',1)",
        [('e1', 1), ('e2', 2), ('e3', 3)],
    )
    conn.commit()

    result = asyncio.run(projects_api._delete_episode_core('e2'))

    assert result['renumbered'] == 1
    assert [
        (row['id'], row['episode_no'])
        for row in conn.execute(
            "SELECT id,episode_no FROM episodes ORDER BY episode_no"
        ).fetchall()
    ] == [('e1', 1), ('e3', 2)]


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
          'p1',1926,'第一千六百二十二章 对决之战！（上）',
          '第一千六百二十二章 对决之战！（上） 正文 第一千六百二十二章 对决之战！（上）'
        );
        INSERT INTO chapters VALUES(
          'p1',1927,'第一千六百二十二章对决之战',
          '第一千六百二十二章对决之战 角色丙踏着血云现身，甲一迎空而起。'
        );
        INSERT INTO episodes VALUES(
          'old','p1',99,'旧分集','','','','[99]',50,'done',1
        );
        """
    )
    rich_body = "角色丙与甲一连续交锋，天地在强者力量下震颤。" * 12
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
    # estimated_cost_cny 随 20b6252 退场删除；「重排不产生模型调用」由下方断言承担
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
        CREATE TABLE shot_versions(
          id TEXT, shot_id TEXT, video_path TEXT,
          provider_task_id TEXT, status TEXT, cost_cny REAL
        );
        CREATE TABLE jobs(
          id TEXT PRIMARY KEY, shot_id TEXT, version_id TEXT, kind TEXT,
          status TEXT, cancellation_requested INTEGER DEFAULT 0,
          abandoned INTEGER DEFAULT 0,
          provider_non_cancellable INTEGER DEFAULT 0,
          provider_operation_id TEXT, provider_create_state TEXT,
          provider_failure_disposition TEXT, created_at REAL
        );
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
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,video_path,provider_task_id,status,cost_cny
           ) VALUES('v1','s1',?,NULL,'succeeded',0)""",
        (str(video),),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,shot_id,version_id,kind,status,provider_create_state,created_at
           ) VALUES('j1','s1','v1','video','succeeded','not_started',1)"""
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", project_root)

    result = worker.invalidate_shot_video_derivatives("s1")

    assert result == {"shot_id": "s1", "videos": 1, "references": 1}
    assert not references.exists() and not video.exists()
    assert final.read_bytes() == b"final"
    assert final.with_suffix(".stale").read_text(encoding="utf-8") == "outdated\n"
    row = conn.execute("SELECT adopted_version_id, mode_plan FROM shots WHERE id='s1'").fetchone()
    assert row["adopted_version_id"] is None and row["mode_plan"] is None


def test_manual_duration_inputs_clamp_to_supported_range() -> None:
    assert clip_duration_value(None) == 5
    assert clip_duration_value(4) == 5
    assert clip_duration_value(7) == 7
    assert clip_duration_value("9") == 9
    assert clip_duration_value(15) == 15
    assert clip_duration_value(20) == 15
