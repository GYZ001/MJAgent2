from __future__ import annotations

import asyncio

import pytest

from app import api, db
from app.orchestration.engine import WorkflowRecorder
from app.schemas import EpisodeScreenplay
from app.storyboard_supervisor import SupervisorCheckpoint, save_checkpoint


def test_resume_storyboard_keeps_checkpoint_and_links_parent_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "storyboard-resume.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1', 'P', 'planned', 1)"
    )
    screenplay_json = EpisodeScreenplay(
        episode_no=1,
        title="E",
        full_script_text="【场1】日 / 广场\n角色走到碑前。",
    ).model_dump_json()
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters, target_duration_s,
               screenplay_json, screenplay_status, status, script_error, created_at
           ) VALUES('e1', 'p1', 1, 'E', '[1]', 50, ?, 'ready', 'scripted',
                    '追加镜生成失败，已保留前 13 个 QA 通过镜头', 1)""",
        (screenplay_json,),
    )
    for shot_no in range(1, 14):
        conn.execute(
            """INSERT INTO shots(id, episode_id, shot_no, duration_s)
               VALUES(?, 'e1', ?, 5)""",
            (f"s{shot_no}", shot_no),
        )
    conn.commit()

    parent = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="previous-partial",
    )
    parent.start()
    parent.partial("provider retry exhausted")

    spawned: list[tuple[str, str]] = []

    def fake_spawn(kind, key, coroutine, **_kwargs):
        spawned.append((kind, key))
        coroutine.close()

    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    monkeypatch.setattr(api.task_registry, "spawn", fake_spawn)

    result = asyncio.run(api.resume_storyboard("e1"))

    episode = conn.execute(
        "SELECT status, script_error FROM episodes WHERE id='e1'"
    ).fetchone()
    run = conn.execute(
        "SELECT parent_run_id, trigger_type FROM workflow_runs WHERE id=?", (result["run_id"],)
    ).fetchone()
    assert result["resumed_from_shot"] == 13
    assert result["next_shot_no"] == 14
    assert spawned == [("storyboard", "e1")]
    assert conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'").fetchone()["c"] == 13
    assert episode["status"] == "scripting"
    assert episode["script_error"] is None
    assert run["parent_run_id"] == parent.run_id
    assert run["trigger_type"] == "resume"


def test_resume_storyboard_accepts_needs_edit_checkpoint(tmp_path, monkeypatch) -> None:
    """单镜「需修改镜头」保留 checkpoint 后，resume 入口必须可继续，不能要求特定失败文案。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "storyboard-resume-needs-edit.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1', 'P', 'planned', 1)"
    )
    screenplay_json = EpisodeScreenplay(
        episode_no=1,
        title="E",
        full_script_text="【场1】日 / 广场\n角色走到碑前。",
    ).model_dump_json()
    note = (
        "镜07已达到重试上限，已作为「需修改镜头」保留在分镜台"
        "（逐镜 checkpoint 已保存，可从下一镜继续）；请修改该镜后从下一镜继续。"
    )
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters, target_duration_s,
               screenplay_json, screenplay_status, status, script_error, created_at
           ) VALUES('e1', 'p1', 1, 'E', '[1]', 50, ?, 'ready', 'scripted', ?, 1)""",
        (screenplay_json, note),
    )
    for shot_no in range(1, 8):
        conn.execute(
            """INSERT INTO shots(id, episode_id, shot_no, duration_s)
               VALUES(?, 'e1', ?, 5)""",
            (f"s{shot_no}", shot_no),
        )
    conn.commit()

    spawned: list[tuple[str, str]] = []

    def fake_spawn(kind, key, coroutine, **_kwargs):
        spawned.append((kind, key))
        coroutine.close()

    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    monkeypatch.setattr(api.task_registry, "spawn", fake_spawn)

    result = asyncio.run(api.resume_storyboard("e1"))

    assert result["resumed_from_shot"] == 7
    assert result["next_shot_no"] == 8
    assert spawned == [("storyboard", "e1")]
    assert conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'").fetchone()["c"] == 7


@pytest.mark.parametrize(
    ("phase", "validated_prefix_end", "next_shot_no", "expected_total"),
    [
        ("PAUSED_EXTERNAL", 4, 5, 10),
        ("PLANNING_OUTLINE", 0, 1, 0),
    ],
)
def test_resume_storyboard_reports_checkpoint_only_progress(
    tmp_path,
    monkeypatch,
    phase,
    validated_prefix_end,
    next_shot_no,
    expected_total,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "storyboard-resume-checkpoint-only.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1', 'P', 'planned', 1)"
    )
    screenplay_json = EpisodeScreenplay(
        episode_no=1,
        title="E",
        full_script_text="【场1】日 / 广场\n角色走到碑前。",
    ).model_dump_json()
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters, target_duration_s,
               screenplay_json, screenplay_status, status, created_at
           ) VALUES('e1', 'p1', 1, 'E', '[1]', 50, ?, 'ready', 'scripted', 1)""",
        (screenplay_json,),
    )
    conn.commit()
    save_checkpoint(
        SupervisorCheckpoint(
            episode_id="e1",
            phase=phase,
            validated_prefix_end=validated_prefix_end,
            next_shot_no=next_shot_no,
            expected_total=expected_total,
        )
    )

    def fake_spawn(_kind, _key, coroutine, **_kwargs):
        coroutine.close()

    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    monkeypatch.setattr(api.task_registry, "spawn", fake_spawn)

    result = asyncio.run(api.resume_storyboard("e1"))

    assert result["resumed_from_shot"] == validated_prefix_end
    assert result["next_shot_no"] == next_shot_no
    assert result["checkpoint_only"] is True
