from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import compiler, db, worker
from app.schemas import Bible, Character, World


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


def _seed_shot(conn: sqlite3.Connection) -> None:
    bible = Bible(
        characters=[
            Character(name="A", role="lead", appearance_canonical="black hair"),
        ],
        world=World(visual_style_canonical="anime drama style"),
    )
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,created_at) VALUES(?,?,?,?,?)",
        ("p1", "P", "created", bible.model_dump_json(), 1.0),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES(?,?,?,?,?)",
        ("e1", "p1", 1, "confirmed", 1.0),
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
               characters,action_desc,source_excerpt,dialogues,transition,
               continuity_from_prev,first_frame_desc,last_frame_desc,scene_status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "s1",
            "e1",
            1,
            5,
            "中景",
            "固定",
            "室内",
            '["A"]',
            "A把桌上的文件整理整齐。",
            "A把桌上的文件整理整齐。",
            "[]",
            "硬切",
            0,
            "A坐在散开的文件前。",
            "A面前的文件已经整齐平码。",
            "approved",
        ),
    )
    conn.commit()


def _patch_enqueue_runtime(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
) -> None:
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(
        compiler,
        "compile_prompt",
        lambda *_args, **_kwargs: "A整理桌上的文件 --ratio 9:16 --dur 5",
    )
    monkeypatch.setattr(
        worker.media_scheduler,
        "reserve_budget",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)


def _create_file_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(db.SCHEMA)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    _seed_shot(conn)
    conn.close()


def test_preflight_claim_is_database_atomic_across_human_and_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "video-shot-mutex.db"
    _create_file_database(database)
    local = threading.local()

    def thread_conn() -> sqlite3.Connection:
        conn = getattr(local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(database, timeout=5, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            local.conn = conn
        return conn

    barrier = threading.Barrier(2)
    real_new_id = worker.new_id

    def synchronized_new_id(prefix: str) -> str:
        if prefix == "job":
            barrier.wait(timeout=2)
        return real_new_id(prefix)

    monkeypatch.setattr(worker, "get_conn", thread_conn)
    monkeypatch.setattr(worker, "new_id", synchronized_new_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                worker._begin_video_preflight_job,
                "s1",
                supervisor_run_id=owner,
            )
            for owner in (None, "supervisor-run")
        ]
        for future in futures:
            future.result(timeout=5)

    check = sqlite3.connect(database)
    assert check.execute(
        """SELECT COUNT(*) FROM jobs
           WHERE shot_id='s1' AND kind='video'
             AND status IN ('waiting_retry','waiting_human','queued','running','waiting_provider')"""
    ).fetchone()[0] == 1
    check.close()


@pytest.mark.parametrize(
    "supervisor_run_id",
    [None, "supervisor-run"],
    ids=["人工重抽", "supervisor重抽"],
)
def test_active_shot_reuses_one_job_and_version_across_request_origins(
    monkeypatch: pytest.MonkeyPatch,
    supervisor_run_id: str | None,
) -> None:
    conn = _conn()
    _seed_shot(conn)
    _patch_enqueue_runtime(monkeypatch, conn)

    first = worker.enqueue_shot("s1")
    conn.execute(
        """UPDATE jobs SET status='running',lease_owner='worker-a',
                  lease_expires_at=9999999999
           WHERE id=?""",
        (first["job_id"],),
    )
    conn.execute(
        "UPDATE shot_versions SET status='running' WHERE id=?",
        (first["version_id"],),
    )
    conn.commit()

    second = worker.enqueue_shot(
        "s1",
        reroll=True,
        supervisor_run_id=supervisor_run_id,
    )

    assert second["reused"] is True
    assert second["job_id"] == first["job_id"]
    assert second["version_id"] == first["version_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE shot_id='s1' AND kind='video'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM shot_versions WHERE shot_id='s1'"
    ).fetchone()[0] == 1


def test_trace_failure_cannot_commit_orphan_active_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed_shot(conn)
    _patch_enqueue_runtime(monkeypatch, conn)

    def fail_after_commit(**_kwargs):
        conn.commit()
        raise RuntimeError("trace persistence interrupted")

    monkeypatch.setattr(worker, "ensure_media_trace", fail_after_commit)

    with pytest.raises(RuntimeError, match="trace persistence interrupted"):
        worker.enqueue_shot("s1")

    assert conn.execute(
        "SELECT COUNT(*) FROM shot_versions WHERE shot_id='s1'"
    ).fetchone()[0] == 0
    preflight = conn.execute(
        "SELECT version_id,status FROM jobs WHERE shot_id='s1'"
    ).fetchone()
    assert preflight["version_id"] is None
    assert preflight["status"] == "waiting_human"


def test_expired_job_lease_recovery_keeps_same_shot_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed_shot(conn)
    _patch_enqueue_runtime(monkeypatch, conn)
    first = worker.enqueue_shot("s1")
    conn.execute(
        """UPDATE jobs SET status='running',lease_owner='dead-worker',
                  lease_expires_at=0
           WHERE id=?""",
        (first["job_id"],),
    )
    conn.execute(
        "UPDATE shot_versions SET status='running' WHERE id=?",
        (first["version_id"],),
    )
    conn.commit()

    recovered = worker.media_scheduler.recoverable_jobs()
    second = worker.enqueue_shot(
        "s1",
        reroll=True,
        supervisor_run_id="supervisor-after-restart",
    )

    assert [job_id for job_id, _delay in recovered] == [first["job_id"]]
    assert second["reused"] is True
    assert second["job_id"] == first["job_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE shot_id='s1' AND kind='video'"
    ).fetchone()[0] == 1


def test_external_terminal_failure_releases_shot_for_new_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed_shot(conn)
    _patch_enqueue_runtime(monkeypatch, conn)
    first = worker.enqueue_shot("s1")

    assert worker._set_job(
        first["job_id"],
        "failed",
        "供应商已明确拒绝",
    ) is True
    assert worker._set_version(
        first["version_id"],
        status="failed",
        error="供应商已明确拒绝",
    ) is True
    conn.execute(
        "UPDATE jobs SET provider_create_state='model_rejected' WHERE id=?",
        (first["job_id"],),
    )
    conn.commit()

    second = worker.enqueue_shot("s1", reroll=True)

    assert second["reused"] is False
    assert second["job_id"] != first["job_id"]
    assert second["version_id"] != first["version_id"]
    assert conn.execute(
        """SELECT COUNT(*) FROM jobs
           WHERE shot_id='s1' AND status IN ('queued','running','waiting_provider')"""
    ).fetchone()[0] == 1
