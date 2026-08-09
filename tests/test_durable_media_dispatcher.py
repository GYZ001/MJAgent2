import asyncio
import json
import sqlite3

from app import db, worker


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


def _seed_shot(conn: sqlite3.Connection, shot_id: str, shot_no: int) -> None:
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,characters,dialogues) "
        "VALUES(?, 'e1', ?, 5, '[]', '[]')",
        (shot_id, shot_no),
    )


def _seed_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    shot_id: str,
    version_id: str,
    after_shot_id: str | None = None,
    status: str = "queued",
    provider_task_id: str | None = None,
    refs_ready: bool = False,
    retake: bool = False,
) -> None:
    meta = {
        "reference_images": ([{"id": f"ref-{version_id}"}] if refs_ready else []),
        "reference_generation_complete": refs_ready,
        "video_input_manifest_frozen": refs_ready,
        "auto_retake_count": 1 if retake else 0,
    }
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at,"
        "provider_task_id,image_inputs) VALUES(?,?,1,'p',?,'queued',1,?,?)",
        (version_id, shot_id, version_id, provider_task_id, json.dumps(meta)),
    )
    conn.execute(
        "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,created_at,"
        "updated_at,after_shot_id) VALUES(?,'video',?,?, 'e1','p1',?,1,1,?)",
        (job_id, shot_id, version_id, status, after_shot_id),
    )


def test_dispatch_prioritizes_poll_and_unblocked_first_pass(monkeypatch) -> None:
    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','created',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'generating',1)"
    )
    for shot_no in range(1, 6):
        _seed_shot(conn, f"s{shot_no}", shot_no)
    # s1 is a valid provisional continuity anchor for s2.
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,"
        "created_at) VALUES('v-anchor','s1',9,'p','anchor','succeeded','/tmp/v1.mp4',1)"
    )
    _seed_job(conn, job_id="j2", shot_id="s2", version_id="v2",
              after_shot_id="s1", refs_ready=True)
    _seed_job(conn, job_id="j3-blocked", shot_id="s3", version_id="v3",
              after_shot_id="s2", refs_ready=True)
    _seed_job(conn, job_id="j4", shot_id="s4", version_id="v4")
    _seed_job(conn, job_id="j-retake", shot_id="s1", version_id="v-retake",
              refs_ready=True, retake=True)
    _seed_job(conn, job_id="j-poll", shot_id="s5", version_id="v5",
              status="waiting_provider", provider_task_id="provider-5", refs_ready=True)
    conn.commit()

    main: list[str] = []
    video_ready: list[str] = []
    poll: list[str] = []
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker._queue, "put_nowait", main.append)
    monkeypatch.setattr(worker._video_ready_queue, "put_nowait", video_ready.append)
    monkeypatch.setattr(worker._poll_queue, "put_nowait", poll.append)
    monkeypatch.setattr(worker, "_worker_target", 2)
    monkeypatch.setattr(worker, "_reference_worker_target", 2)
    monkeypatch.setattr(worker, "_video_ready_worker_target", 2)
    monkeypatch.setattr(worker, "_poll_worker_target", 1)
    monkeypatch.setattr("app.media_pipeline.retry_policy.scheduler_policy", lambda: "stage_aware")
    monkeypatch.setattr("app.db.get_setting", lambda key: {
        "media_scheduler_policy": "stage_aware",
        "video_ready_low_watermark": "2",
        "video_ready_high_watermark": "6",
        "reference_shot_cohort_limit": "2",
        "reference_prepared_backlog": "8",
    }.get(key, ""))

    result = worker._dispatch_due_jobs()

    assert poll == ["j-poll"]
    # 就绪镜头进 video_ready；未准备参考图的进 reference；阻塞连续镜不入队
    assert "j2" in video_ready
    assert "j-retake" in video_ready or "j-retake" in main
    assert "j4" in main
    assert "j3-blocked" not in main and "j3-blocked" not in video_ready
    assert result["poll"] == 1
    assert result["due"] == 5


def test_stage_aware_dispatcher_finishes_dependency_reads_before_stage_writes(
    monkeypatch,
) -> None:
    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','created',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'generating',1)"
    )
    for shot_no in range(1, 5):
        _seed_shot(conn, f"s{shot_no}", shot_no)
    _seed_job(
        conn,
        job_id="j3",
        shot_id="s3",
        version_id="v3",
        after_shot_id="s1",
        refs_ready=True,
    )
    _seed_job(
        conn,
        job_id="j4",
        shot_id="s4",
        version_id="v4",
        after_shot_id="s2",
        refs_ready=True,
    )
    conn.commit()

    transaction_states: list[bool] = []

    def chain_remaining(*_args, **_kwargs) -> int:
        transaction_states.append(conn.in_transaction)
        return 1

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(
        "app.media_pipeline.scheduler.continuity_chain_remaining",
        chain_remaining,
    )
    monkeypatch.setattr(worker._queue, "put_nowait", lambda *_: None)
    monkeypatch.setattr(worker._video_ready_queue, "put_nowait", lambda *_: None)
    monkeypatch.setattr(worker._poll_queue, "put_nowait", lambda *_: None)
    monkeypatch.setattr(worker, "_worker_target", 1)
    monkeypatch.setattr(worker, "_reference_worker_target", 1)
    monkeypatch.setattr(worker, "_video_ready_worker_target", 1)
    monkeypatch.setattr(worker, "_poll_worker_target", 1)

    worker._dispatch_due_jobs_stage_aware()

    assert transaction_states == [False, False]
    assert conn.in_transaction is False
    assert {
        row["pipeline_stage"]
        for row in conn.execute(
            "SELECT pipeline_stage FROM jobs WHERE id IN ('j3','j4')"
        ).fetchall()
    } == {"waiting_continuity_anchor"}


def test_dispatch_legacy_keeps_single_main_queue(monkeypatch) -> None:
    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','created',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'generating',1)"
    )
    for shot_no in range(1, 5):
        _seed_shot(conn, f"s{shot_no}", shot_no)
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,"
        "created_at) VALUES('v-anchor','s1',9,'p','anchor','succeeded','/tmp/v1.mp4',1)"
    )
    _seed_job(conn, job_id="j2", shot_id="s2", version_id="v2",
              after_shot_id="s1", refs_ready=True)
    _seed_job(conn, job_id="j4", shot_id="s4", version_id="v4")
    _seed_job(conn, job_id="j-retake", shot_id="s1", version_id="v-retake",
              refs_ready=True, retake=True)
    conn.commit()

    main: list[str] = []
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker._queue, "put_nowait", main.append)
    monkeypatch.setattr(worker, "_worker_target", 2)
    monkeypatch.setattr(worker, "_poll_worker_target", 1)
    monkeypatch.setattr("app.media_pipeline.retry_policy.scheduler_policy", lambda: "legacy")

    result = worker._dispatch_due_jobs_legacy()
    assert main == ["j2", "j4", "j-retake"]
    assert result["main"] == 3


def test_dispatcher_rebuilds_a_lost_in_memory_queue(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO jobs(id,kind,status,created_at,updated_at) "
        "VALUES('j1','video','queued',1,1)"
    )
    conn.commit()
    main: asyncio.Queue[str] = asyncio.Queue()
    video_ready: asyncio.Queue[str] = asyncio.Queue()
    poll: asyncio.Queue[str] = asyncio.Queue()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "_queue", main)
    monkeypatch.setattr(worker, "_video_ready_queue", video_ready)
    monkeypatch.setattr(worker, "_poll_queue", poll)
    monkeypatch.setattr(worker, "_worker_target", 1)
    monkeypatch.setattr(worker, "_reference_worker_target", 1)
    monkeypatch.setattr(worker, "_video_ready_worker_target", 1)
    monkeypatch.setattr(worker, "_poll_worker_target", 1)
    monkeypatch.setattr("app.media_pipeline.retry_policy.scheduler_policy", lambda: "stage_aware")

    worker._dispatch_due_jobs()
    assert main.get_nowait() == "j1"
    main.task_done()
    worker._dispatch_due_jobs()
    assert main.get_nowait() == "j1"


def test_video_ready_preempts_reference_when_slot_free(monkeypatch) -> None:
    """有 VIDEO_READY 且有槽时，必须进 video_ready 队列而非参考图队列。"""
    conn = _conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','created',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e1','p1',1,'generating',1)"
    )
    _seed_shot(conn, "s1", 1)
    _seed_shot(conn, "s2", 2)
    _seed_job(conn, job_id="j-ready", shot_id="s1", version_id="v1", refs_ready=True)
    _seed_job(conn, job_id="j-ref", shot_id="s2", version_id="v2", refs_ready=False)
    conn.commit()

    ref_q: list[str] = []
    ready_q: list[str] = []
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker._queue, "put_nowait", ref_q.append)
    monkeypatch.setattr(worker._video_ready_queue, "put_nowait", ready_q.append)
    monkeypatch.setattr(worker._poll_queue, "put_nowait", lambda *_: None)
    monkeypatch.setattr(worker, "_worker_target", 2)
    monkeypatch.setattr(worker, "_reference_worker_target", 2)
    monkeypatch.setattr(worker, "_video_ready_worker_target", 2)
    monkeypatch.setattr(worker, "_poll_worker_target", 1)
    monkeypatch.setattr("app.media_pipeline.retry_policy.scheduler_policy", lambda: "stage_aware")

    worker._dispatch_due_jobs()
    assert "j-ready" in ready_q
    assert "j-ready" not in ref_q
    assert "j-ref" in ref_q
