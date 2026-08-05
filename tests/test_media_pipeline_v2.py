"""媒体流水线 V2：非阻塞轮询状态、参考图集、调度配额。"""
from __future__ import annotations

import sqlite3

from app import db, worker
from app.media_pipeline.concurrency import CHANNEL_DEFAULTS, channel_limit, ensure_channel
from app.media_pipeline import stages as S
from app.media_pipeline.retry_policy import decide_retry_by_error_class
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
    assert CHANNEL_DEFAULTS[S.RESOURCE_VIDEO_INFLIGHT] == 15
    assert CHANNEL_DEFAULTS[S.RESOURCE_IMAGE] == 4
    assert CHANNEL_DEFAULTS[S.RESOURCE_VLM] == 6
    ensure_channel(S.RESOURCE_VIDEO_SUBMIT)
    assert channel_limit(S.RESOURCE_VIDEO_SUBMIT) >= 1


def test_qa_findings_are_rejected_by_retry_allowlist() -> None:
    for code in ("QA_LOW_SCORE", "VIDEO_QA_STORY_REPEAT", "QUALITY_DRIFT"):
        decision = decide_retry_by_error_class(code)
        assert decision.allow is False
        assert decision.create_new_version is False


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


def _seed_episode_with_gap(conn: sqlite3.Connection, *, queued_retakes: int = 0, upstream_retakes: int = 0) -> None:
    """首轮缺口（s_gap）+ 若干自动重抽任务。"""
    import json

    conn.execute("INSERT INTO projects(id, name, status, created_at) VALUES('p1','t','created',1)")
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES('e1','p1',1,'confirmed',1)"
    )
    # 未覆盖首轮镜
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting, "
        "characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, "
        "transition, continuity_from_prev) "
        "VALUES('s_gap','e1',99,5,'中景','固定','室内','[]','动作','首','尾','原文','','[]','硬切',0)"
    )
    # 已有成功视频的镜 + queued 重抽
    for i in range(queued_retakes):
        sid, vid0, vid1, jid = f"s_q{i}", f"v_q{i}_0", f"v_q{i}_1", f"j_q{i}"
        conn.execute(
            "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting, "
            "characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, "
            "transition, continuity_from_prev) "
            "VALUES(?,?,?,5,'中景','固定','室内','[]','动作','首','尾','原文','','[]','硬切',0)",
            (sid, "e1", i + 1),
        )
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at,video_path) "
            "VALUES(?,?,1,'p',?,'succeeded',1,'/tmp/v.mp4')",
            (vid0, sid, vid0),
        )
        meta = json.dumps({"auto_retake_count": 1, "reference_images": [{"id": "r"}]})
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at,image_inputs) "
            "VALUES(?,?,2,'p',?,'queued',1,?)",
            (vid1, sid, vid1, meta),
        )
        conn.execute(
            "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at,"
            "pipeline_stage,provider_non_cancellable,cancellation_requested,abandoned) "
            "VALUES(?,'video',?,?, 'e1','p1','queued',1,1,'waiting_video_slot',0,0,0)",
            (jid, sid, vid1),
        )
    # 真正上游在途重抽
    for i in range(upstream_retakes):
        sid, vid0, vid1, jid = f"s_u{i}", f"v_u{i}_0", f"v_u{i}_1", f"j_u{i}"
        conn.execute(
            "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting, "
            "characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, "
            "transition, continuity_from_prev) "
            "VALUES(?,?,?,5,'中景','固定','室内','[]','动作','首','尾','原文','','[]','硬切',0)",
            (sid, "e1", 50 + i),
        )
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at,video_path) "
            "VALUES(?,?,1,'p',?,'succeeded',1,'/tmp/v.mp4')",
            (vid0, sid, vid0),
        )
        meta = json.dumps({"auto_retake_count": 1})
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at,"
            "image_inputs,provider_task_id) "
            "VALUES(?,?,2,'p',?,'running',1,?,'pt-x')",
            (vid1, sid, vid1, meta),
        )
        conn.execute(
            "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at,"
            "pipeline_stage,provider_non_cancellable,cancellation_requested,abandoned) "
            "VALUES(?,'video',?,?, 'e1','p1','waiting_provider',1,1,'video_generating',1,0,0)",
            (jid, sid, vid1),
        )
    conn.commit()


def test_queued_retakes_do_not_self_deadlock(monkeypatch) -> None:
    """排队中的自动重抽不得计入在途配额，否则会自锁。"""
    conn = _conn()
    _seed_episode_with_gap(conn, queued_retakes=5, upstream_retakes=0)
    monkeypatch.setattr("app.media_pipeline.scheduler.get_conn", lambda: conn)
    monkeypatch.setattr("app.media_pipeline.concurrency.channel_limit", lambda resource: 8)
    monkeypatch.setattr("app.media_pipeline.retry_policy.get_setting", lambda key: {
        "episode_video_inflight_limit": "8",
        "project_video_inflight_limit": "12",
    }.get(key))
    ok, reason = can_admit_video_submit(episode_id="e1", project_id="p1", is_auto_retake=True)
    assert ok is True, reason


def test_upstream_retake_cap_still_enforced(monkeypatch) -> None:
    """真正上游在途重抽达到 25% 限额时仍应拒绝。"""
    conn = _conn()
    # retake_cap = max(1, int(8*0.25)) = 2
    _seed_episode_with_gap(conn, queued_retakes=0, upstream_retakes=2)
    monkeypatch.setattr("app.media_pipeline.scheduler.get_conn", lambda: conn)
    monkeypatch.setattr("app.media_pipeline.concurrency.channel_limit", lambda resource: 8)
    monkeypatch.setattr("app.media_pipeline.retry_policy.get_setting", lambda key: {
        "episode_video_inflight_limit": "8",
        "project_video_inflight_limit": "12",
    }.get(key))
    ok, reason = can_admit_video_submit(episode_id="e1", project_id="p1", is_auto_retake=True)
    assert ok is False
    assert reason and "自动重抽槽位已满" in reason
    # 首轮不受重抽限额限制
    ok2, _ = can_admit_video_submit(episode_id="e1", project_id="p1", is_auto_retake=False)
    assert ok2 is True
