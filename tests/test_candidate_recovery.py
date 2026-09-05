"""开机自愈：重启打断在「已落盘、未校验」的视频版本补成候选并释放槽位（2026-09-05）。"""

from __future__ import annotations

import json

from app.db import get_conn
from app.media_exec import candidate_recovery as cr


def _seed(conn, tmp_path, *, with_file: bool = True, technical: str | None = None) -> str:
    conn.execute("INSERT OR IGNORE INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute("INSERT OR IGNORE INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e1','p1',1,'generating',1)")
    conn.execute(
        """INSERT OR IGNORE INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition)
           VALUES('s1','e1',1,15,'中景','固定','室内','[]','人物站定','[]','硬切')"""
    )
    path = tmp_path / "v1.mp4"
    if with_file:
        path.write_bytes(b"\x00" * 16)
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
               video_slot_active,video_path,technical_validation_json,created_at,image_inputs)
           VALUES('v1','s1',1,'prompt','idem','succeeded',1,?,?,1,'{}')""",
        (str(path), technical),
    )
    conn.execute(
        """INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,
               video_slot_active,provider_create_state,created_at,updated_at)
           VALUES('j1','video','s1','v1','e1','p1','succeeded',0,'accepted',1,1)"""
    )
    conn.commit()
    return str(path)


def test_interrupted_candidate_gets_validated_and_releases_slot(monkeypatch, tmp_path):
    conn = get_conn()
    _seed(conn, tmp_path)
    recorded: list[str] = []

    def fake_record(version_id, *, step_run_id=None):
        recorded.append(version_id)
        conn.execute(
            "UPDATE shot_versions SET technical_validation_json=? WHERE id=?",
            (json.dumps({"passed": True, "issues": [], "evidence": {}}), version_id),
        )
        return {"id": "art_1"}

    monkeypatch.setattr(cr, "record_video_candidate", fake_record)
    assert cr.recover_unvalidated_video_candidates() == 1
    assert recorded == ["v1"]
    row = conn.execute("SELECT video_slot_active, qa_json FROM shot_versions WHERE id='v1'").fetchone()
    assert row["video_slot_active"] == 0, "收尾链走完后不得再占镜头槽位"
    assert json.loads(row["qa_json"])["qa_recovered"] is True
    assert json.loads(row["qa_json"])["status"] == "unverified"
    assert cr.recover_unvalidated_video_candidates() == 0, "已补齐的版本第二次不再处理"


def test_missing_file_is_left_alone(monkeypatch, tmp_path):
    conn = get_conn()
    _seed(conn, tmp_path, with_file=False)
    monkeypatch.setattr(cr, "record_video_candidate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不该被调用")))
    assert cr.recover_unvalidated_video_candidates() == 0
    row = conn.execute("SELECT video_slot_active FROM shot_versions WHERE id='v1'").fetchone()
    assert row["video_slot_active"] == 1


def test_validation_failure_does_not_abort_recovery(monkeypatch, tmp_path):
    conn = get_conn()
    _seed(conn, tmp_path)

    def boom(version_id, *, step_run_id=None):
        raise RuntimeError("ffprobe 不在")

    monkeypatch.setattr(cr, "record_video_candidate", boom)
    assert cr.recover_unvalidated_video_candidates() == 0


def test_runtime_reconcile_blocks_when_job_active_and_heals_when_terminal(monkeypatch, tmp_path):
    conn = get_conn()
    _seed(conn, tmp_path)
    healed: list[str] = []

    def fake_record(version_id, *, step_run_id=None):
        healed.append(version_id)
        conn.execute("UPDATE shot_versions SET technical_validation_json='{\"passed\": true}' WHERE id=?", (version_id,))
        return {"id": "art_1"}

    monkeypatch.setattr(cr, "record_video_candidate", fake_record)
    conn.execute("UPDATE jobs SET status='running' WHERE id='j1'"); conn.commit()
    assert cr.reconcile_unvalidated_candidates(conn, ["s1"], ("queued", "running")) == {"s1"}, "QA 进行中：按在途，不派重拍"
    assert healed == []
    conn.execute("UPDATE jobs SET status='succeeded' WHERE id='j1'"); conn.commit()
    assert cr.reconcile_unvalidated_candidates(conn, ["s1"], ("queued", "running")) == set()
    assert healed == ["v1"]
    assert conn.execute("SELECT video_slot_active FROM shot_versions WHERE id='v1'").fetchone()[0] == 0
    assert cr.reconcile_unvalidated_candidates(conn, ["s1"], ("queued", "running")) == set(), "已补齐不再处理"


def test_ledger_treats_postprocessing_version_as_active(monkeypatch, tmp_path):
    from app.video_supervisor.coverage import _latest_video_jobs

    conn = get_conn()
    _seed(conn, tmp_path)
    conn.execute("UPDATE jobs SET status='running' WHERE id='j1'"); conn.commit()
    monkeypatch.setattr(cr, "record_video_candidate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("在途不该校验")))
    active, rejected = _latest_video_jobs(conn, ["s1"], None)
    assert active.get("s1") == "j1" and rejected == set()
    conn.execute("UPDATE jobs SET status='succeeded' WHERE id='j1'"); conn.commit()
    monkeypatch.setattr(cr, "record_video_candidate", lambda version_id, **_k: conn.execute(
        "UPDATE shot_versions SET technical_validation_json='{\"passed\": true}' WHERE id=?", (version_id,)) and {"id": "a"})
    active, rejected = _latest_video_jobs(conn, ["s1"], None)
    assert active == {} and conn.execute("SELECT video_slot_active FROM shot_versions WHERE id='v1'").fetchone()[0] == 0
