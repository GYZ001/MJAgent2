"""媒体流水线 V2：非阻塞轮询状态、参考图集、调度配额。"""
from __future__ import annotations

import sqlite3

from app import db, worker
from app.media_pipeline.concurrency import CHANNEL_DEFAULTS, channel_limit, ensure_channel
from app.media_pipeline import stages as S
from app.media_pipeline.retry_policy import decide_qa_retake
from app.media_pipeline.reference_store import upsert_reference_set_from_meta
from app.media_pipeline.scheduler import can_admit_video_submit, continuity_anchor_ready


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


def test_defer_sets_waiting_provider(monkeypatch) -> None:
    import asyncio

    conn = _conn()
    conn.execute(
        "INSERT INTO jobs(id, kind, status, created_at, updated_at, lease_owner, lease_expires_at) "
        "VALUES('j1','video','running',1,1,'w0',9999999999)"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    main_requeued: list[str] = []
    poll_requeued: list[str] = []
    monkeypatch.setattr(worker._queue, "put_nowait", main_requeued.append)
    monkeypatch.setattr(worker._poll_queue, "put_nowait", poll_requeued.append)

    async def run() -> bool:
        ok = worker._defer_provider_poll("j1", "pt-1", lease_owner="w0", delay=0)
        await asyncio.sleep(0)
        if worker._retry_tasks:
            await asyncio.gather(*list(worker._retry_tasks))
        return ok

    assert asyncio.run(run()) is True
    row = conn.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()
    assert row["status"] == "waiting_provider"
    assert main_requeued == []
    assert poll_requeued == ["j1"]


def test_channel_defaults_balanced() -> None:
    assert CHANNEL_DEFAULTS[S.RESOURCE_VIDEO_INFLIGHT] == 8
    assert CHANNEL_DEFAULTS[S.RESOURCE_IMAGE] == 4
    assert CHANNEL_DEFAULTS[S.RESOURCE_VLM] == 6
    ensure_channel(S.RESOURCE_VIDEO_SUBMIT)
    assert channel_limit(S.RESOURCE_VIDEO_SUBMIT) >= 1


def test_qa_retake_policy_caps_at_two() -> None:
    """PRD：连续失败 2 次后停止自动重抽并转人工。"""
    d0 = decide_qa_retake(auto_retake_count=0, qa_overall=0.2, threshold=0.6)
    assert d0.allow and d0.create_new_version
    d1 = decide_qa_retake(auto_retake_count=1, qa_overall=0.2, threshold=0.6)
    assert d1.allow and d1.attempt == 2
    d2 = decide_qa_retake(auto_retake_count=2, qa_overall=0.2, threshold=0.6)
    assert not d2.allow
    d_hard = decide_qa_retake(
        auto_retake_count=0, qa_overall=0.8, threshold=0.6, hard_failures=["story_repeat"]
    )
    assert d_hard.allow


def test_reference_set_persist(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','t','created',1)"
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES('e1','p1',1,'confirmed',1)"
    )
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting, "
        "characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, "
        "transition, continuity_from_prev) "
        "VALUES('s1','e1',1,5,'中景','固定','室内','[]','动作','首','尾','原文','', '[]','硬切',0)"
    )
    conn.commit()
    meta = {
        "reference_images": [
            {"id": "r1", "type": "generated", "source": "pipeline", "path": "/tmp/a.jpg",
             "selectedForSeedance": True, "deleted": False, "qualityScore": 0.9},
        ],
        "reference_gallery_revision": 1,
    }
    set_id = upsert_reference_set_from_meta(shot_id="s1", version_id=None, meta=meta, conn=conn)
    conn.commit()
    assert set_id
    assert meta["reference_set_id"] == set_id
    row = conn.execute("SELECT COUNT(*) c FROM reference_assets WHERE reference_set_id=?", (set_id,)).fetchone()
    assert row["c"] == 1


def test_continuity_blocks_without_anchor(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','t','created',1)"
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES('e1','p1',1,'confirmed',1)"
    )
    for sid, no in (("s1", 1), ("s2", 2)):
        conn.execute(
            "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting, "
            "characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, "
            "transition, continuity_from_prev) "
            "VALUES(?,?,?,5,'中景','固定','室内','[]','动作','首','尾','原文','','[]','硬切',?)",
            (sid, "e1", no, 1 if no == 2 else 0),
        )
    conn.commit()
    ready, reason = continuity_anchor_ready(conn, "s1")
    assert ready is False
    assert reason


def test_admit_respects_inflight(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr("app.media_pipeline.scheduler.get_conn", lambda: conn)
    monkeypatch.setattr("app.media_pipeline.scheduler.count_inflight_videos", lambda **kw: 99)
    ok, reason = can_admit_video_submit(episode_id="e1", project_id="p1", is_auto_retake=False)
    assert ok is False
    assert reason
