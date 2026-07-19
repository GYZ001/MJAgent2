import asyncio
import json
import sqlite3

import pytest

from app import artifacts, db, planning, task_registry, worker
from app.compiler import clip_duration_value
from app.schemas import Shot, Storyboard
from app.validators import normalize_fixed_durations


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
        planning.worker,
        "delete_project_episodes",
        lambda project_id: conn.execute("DELETE FROM episodes WHERE project_id=?", (project_id,)).rowcount,
    )

    asyncio.run(planning.run_regex_plan("p1"))

    rows = conn.execute("SELECT * FROM episodes ORDER BY episode_no").fetchall()
    assert [row["title"] for row in rows] == ["第一章 起点", "第二章 转折"]
    assert [json.loads(row["source_chapters"]) for row in rows] == [[1], [2]]
    assert "  " not in rows[0]["synopsis"]
    assert conn.execute("SELECT plan_status FROM projects WHERE id='p1'").fetchone()[0] == "ready"


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


def test_all_duration_inputs_normalize_to_five_seconds() -> None:
    assert clip_duration_value(None) == clip_duration_value(15) == 5
    shot = Shot(
        shot_no=1,
        duration_s=15,
        shot_size="中景",
        camera_move="固定",
        scene_setting="day, room",
        characters=["A"],
        action_desc="A performs one continuous and visible action from start to finish.",
        first_frame_desc="A starts the action.",
        last_frame_desc="A completes the action.",
        source_excerpt="A performs the action.",
        dialogues=[],
        transition="硬切",
        continuity_from_prev=False,
    )
    board = Storyboard(episode_no=1, shots=[shot])
    normalize_fixed_durations(board)
    assert board.shots[0].duration_s == 5
