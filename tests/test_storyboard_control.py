"""分镜 Supervisor 协作控制（pause / handoff）单元测试。"""
from __future__ import annotations

import asyncio

import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.orchestration import api as orch_api
from app.orchestration.state_machine import transition_run
from app.storyboard_control import (
    consume_control,
    control_snapshot,
    peek_control,
    request_control,
)
from app.storyboard_supervisor import SupervisorCheckpoint, save_checkpoint


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "control.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    yield
    monkeypatch.setattr(db._local, "conn", None, raising=False)


def _seed_episode(episode_id: str = "e1", project_id: str = "p1") -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,1)",
        (project_id, "P", "planned"),
    )
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters, target_duration_s,
               screenplay_status, status, created_at
           ) VALUES(?,?,1,'陨落的天才','[1]',120,'ready','scripting',1)""",
        (episode_id, project_id),
    )
    conn.commit()


def test_request_peek_consume_roundtrip():
    _seed_episode()
    request_control("e1", "pause")
    assert peek_control("e1") == "pause"
    assert control_snapshot("e1") == {"action": "pause", "pending": True}
    assert consume_control("e1") == "pause"
    assert peek_control("e1") is None
    assert control_snapshot("e1") is None


def test_handoff_overrides_pause():
    _seed_episode()
    request_control("e1", "pause")
    request_control("e1", "handoff")
    assert peek_control("e1") == "handoff"


def test_pause_run_writes_control_and_event(monkeypatch):
    _seed_episode()
    run_id = evidence_repository.create_run(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="fp",
    )
    transition_run(run_id, "CREATED", "RUNNING", "test")
    monkeypatch.setattr(
        "app.orchestration.api.task_registry.active", lambda *_a, **_k: True
    )
    result = asyncio.run(orch_api.pause_run(run_id))
    assert result["paused_requested"] is True
    assert peek_control("e1") == "pause"
    events = evidence_repository.get_events(run_id)
    assert any(e["event_type"] == "SUPERVISOR_PAUSE_REQUESTED" for e in events)


def test_handoff_run_marks_waiting_human_when_idle(monkeypatch):
    _seed_episode()
    run_id = evidence_repository.create_run(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="fp",
    )
    transition_run(run_id, "CREATED", "RUNNING", "test")
    transition_run(run_id, "RUNNING", "PAUSED_EXTERNAL", "test_pause")
    cp = SupervisorCheckpoint(
        episode_id="e1",
        phase="PAUSED_EXTERNAL",
        validated_prefix_end=3,
        next_shot_no=4,
        expected_total=10,
    )
    save_checkpoint(cp, run_id=run_id)
    monkeypatch.setattr(
        "app.orchestration.api.task_registry.active", lambda *_a, **_k: False
    )
    result = asyncio.run(orch_api.handoff_run(run_id))
    assert result["handoff_requested"] is True
    loaded = evidence_repository.get_run(run_id)
    assert loaded["status"] == "WAITING_HUMAN"
    ep = db.get_conn().execute(
        "SELECT status, script_error FROM episodes WHERE id='e1'"
    ).fetchone()
    assert ep["status"] == "scripted"
    assert "转人工" in (ep["script_error"] or "")
