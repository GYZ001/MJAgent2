"""批量分镜 Supervisor 指标 API 测试。"""
from __future__ import annotations

from app import db
from app.evidence import repository as evidence_repository
from app.orchestration import api as orch_api
from app.orchestration.state_machine import transition_run
from app.storyboard_supervisor import SupervisorCheckpoint, save_checkpoint


def test_storyboard_metrics_counts_episode_scoped_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "metrics.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','P','planned',1)"
    )
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters, target_duration_s,
               screenplay_status, status, active_storyboard_run_id, created_at
           ) VALUES('e1','p1',1,'陨落的天才','[1]',120,'ready','scripting',NULL,1)"""
    )
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters, target_duration_s,
               screenplay_status, status, created_at
           ) VALUES('e2','p1',2,'其它','[2]',120,'ready','planned',1)"""
    )
    conn.commit()

    run_id = evidence_repository.create_run(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="fp",
    )
    transition_run(run_id, "CREATED", "RUNNING", "test")
    conn.execute(
        "UPDATE episodes SET active_storyboard_run_id=? WHERE id='e1'", (run_id,)
    )
    conn.commit()
    save_checkpoint(
        SupervisorCheckpoint(
            episode_id="e1",
            phase="REPAIRING",
            repair_epoch=1,
            validated_prefix_end=4,
            next_shot_no=5,
            expected_total=12,
        ),
        run_id=run_id,
    )

    metrics = orch_api.project_storyboard_metrics("p1")
    assert metrics["project_id"] == "p1"
    assert metrics["active_storyboard_runs"] == 1
    assert metrics["scripting_episodes"] == 1
    assert metrics["repairing"] == 1
    assert metrics["phase_counts"]["REPAIRING"] == 1
    assert len(metrics["episodes"]) == 1
    assert metrics["episodes"][0]["episode_id"] == "e1"
    assert metrics["episodes"][0]["run_id"] == run_id
