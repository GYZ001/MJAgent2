"""QPSP 调度水位与阶段投影单元测试。"""
from __future__ import annotations

import json
import sqlite3

from app import db
from app.media_pipeline import stages as S
from app.media_pipeline.scheduler import (
    count_true_video_ready_not_submitted,
    is_true_video_ready,
    job_scheduler_score,
    should_start_more_reference_work,
)
from app.media_pipeline.stage_state import set_pipeline_stage, stage_label
from app.media_pipeline.status import episode_pipeline_statuses


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def test_is_true_video_ready_rejects_static_only() -> None:
    meta = {
        "reference_images": [{"id": "a"}],
        "reference_static_ready": True,
        "reference_generation_complete": False,
    }
    assert not is_true_video_ready(meta, continuity_ok=False)
    assert not is_true_video_ready(meta, continuity_ok=True)

    meta2 = {
        "reference_images": [{"id": "a"}],
        "reference_generation_complete": True,
        "video_input_manifest_frozen": True,
    }
    assert is_true_video_ready(meta2, continuity_ok=True)
    assert not is_true_video_ready(meta2, continuity_ok=False)


def test_job_scheduler_score_prefers_first_pass_and_near_complete() -> None:
    a = job_scheduler_score(
        first_pass=True, continuity_remaining=2, completed_slots=3,
        wait_age_minutes=1, auto_retake=False,
    )
    b = job_scheduler_score(
        first_pass=True, continuity_remaining=0, completed_slots=0,
        wait_age_minutes=1, auto_retake=False,
    )
    c = job_scheduler_score(
        first_pass=False, continuity_remaining=5, completed_slots=3,
        wait_age_minutes=30, auto_retake=True,
    )
    assert a > b > c


def test_set_pipeline_stage_and_status_projection(monkeypatch) -> None:
    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','created',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e1','p1',1,'generating',1)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,characters,dialogues) "
        "VALUES('s1','e1',1,5,'[]','[]')"
    )
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v1','s1',1,'p','k','queued',1)"
    )
    conn.execute(
        "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at) "
        "VALUES('j1','video','s1','v1','e1','p1','queued',1,1)"
    )
    conn.commit()
    monkeypatch.setattr("app.media_pipeline.stage_state.get_conn", lambda: conn)
    monkeypatch.setattr("app.media_pipeline.status.get_conn", lambda: conn)

    set_pipeline_stage(
        "j1", S.STAGE_WAITING_VIDEO_SLOT,
        reason_code="EPISODE_VIDEO_INFLIGHT_FULL",
        reason_text="本集 8 个上游视频槽已满",
        scheduler_lane=S.LANE_VIDEO_READY,
        stage_progress={"current": 4, "total": 4, "unit": "reference_slots"},
        conn=conn,
    )
    conn.commit()
    statuses, summary = episode_pipeline_statuses("e1", conn=conn)
    st = statuses["s1"]
    assert st["pipeline_stage"] == S.STAGE_WAITING_VIDEO_SLOT
    assert st["reason_code"] == "EPISODE_VIDEO_INFLIGHT_FULL"
    assert "槽" in (st["stage_label"] or "")
    assert summary["video_ready"] >= 1


def test_stage_label_unknown() -> None:
    assert "未知阶段" in stage_label("totally_new_stage_xyz")


def test_high_watermark_blocks_new_reference(monkeypatch) -> None:
    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','created',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e1','p1',1,'generating',1)"
    )
    for i in range(1, 8):
        sid, vid, jid = f"s{i}", f"v{i}", f"j{i}"
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s,characters,dialogues) "
            "VALUES(?,?,?,5,'[]','[]')",
            (sid, "e1", i),
        )
        meta = json.dumps({
            "reference_images": [{"id": f"r{i}"}],
            "reference_generation_complete": True,
            "video_input_manifest_frozen": True,
        })
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at,image_inputs) "
            "VALUES(?,?,1,'p',?,'queued',1,?)",
            (vid, sid, vid, meta),
        )
        conn.execute(
            "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at,pipeline_stage) "
            "VALUES(?,'video',?,?, 'e1','p1','queued',1,1,?)",
            (jid, sid, vid, S.STAGE_VIDEO_READY),
        )
    conn.commit()
    monkeypatch.setattr("app.media_pipeline.scheduler.get_conn", lambda: conn)
    monkeypatch.setattr("app.media_pipeline.retry_policy.get_setting", lambda key: {
        "media_scheduler_policy": "stage_aware",
        "video_ready_low_watermark": "2",
        "video_ready_high_watermark": "6",
        "reference_shot_cohort_limit": "1",
        "episode_video_inflight_limit": "8",
    }.get(key))
    ready = count_true_video_ready_not_submitted(episode_id="e1", conn=conn)
    assert ready >= 6
    allow, demand = should_start_more_reference_work(episode_id="e1", conn=conn)
    assert allow is False
    assert demand == 0
