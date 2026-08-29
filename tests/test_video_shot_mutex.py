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


@pytest.mark.parametrize(
    "active_status",
    ["queued", "waiting_provider", "waiting_retry"],
)
def test_genuinely_active_statuses_still_dedupe_and_never_double_charge(
    monkeypatch: pytest.MonkeyPatch,
    active_status: str,
) -> None:
    """防回归：waiting_human 从复用判据里拿掉，不能连带拆掉双击防重复扣费。

    queued/waiting_provider/waiting_retry 仍然代表"系统仍在自动处理"，
    video_slot_active 必须继续持有，重新生成必须继续复用同一个 job/version，
    不能新建付费版本——这条和本次修复的目标（waiting_human 死锁）是两回事，
    必须分开验证以免顾此失彼。
    """
    conn = _conn()
    _seed_shot(conn)
    _patch_enqueue_runtime(monkeypatch, conn)

    first = worker.enqueue_shot("s1")
    conn.execute(
        "UPDATE jobs SET status=? WHERE id=?",
        (active_status, first["job_id"]),
    )
    conn.execute(
        "UPDATE shot_versions SET status=? WHERE id=?",
        (active_status, first["version_id"]),
    )
    conn.commit()
    assert conn.execute(
        "SELECT video_slot_active FROM jobs WHERE id=?", (first["job_id"],)
    ).fetchone()[0] == 1

    second = worker.enqueue_shot("s1", reroll=True)

    assert second["reused"] is True
    assert second.get("reused_reason") == "in_flight"
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


def test_waiting_human_releases_shot_lock_for_reroll_with_fresh_idempotency_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点「重新生成」永远没反应的死锁：转人工后 video_slot_active 必须清零。

    复刻真实卡死记录（reason_code=VIDEO_PROVIDER_EXECUTION_FAILED，jobs 与
    shot_versions 均 waiting_human + video_slot_active=1）：_set_job 转
    waiting_human 时必须像 failed/cancelled 一样释放镜头级独占锁，否则
    _begin_video_preflight_job 会在任何指纹/幂等键比较之前就短路成
    reused:True——即便调用方每次都换新的幂等键（见
    tests/../CLAUDE.md「Gates and Criteria」）。用文件数据库 + 第二条独立
    连接核验，不依赖同一个连接对象的内存视图。
    """
    database = tmp_path / "video-shot-mutex-waiting-human.db"
    _create_file_database(database)
    conn = sqlite3.connect(database, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _patch_enqueue_runtime(monkeypatch, conn)

    first = worker.enqueue_shot("s1")
    conn.execute(
        """UPDATE jobs SET status='running',lease_owner='worker-a',
                  lease_expires_at=9999999999,provider_create_state='accepted',
                  provider_operation_id='op-1'
           WHERE id=?""",
        (first["job_id"],),
    )
    conn.execute(
        "UPDATE shot_versions SET status='running' WHERE id=?",
        (first["version_id"],),
    )
    conn.commit()

    # 真实两条卡死记录走的正是 _set_job 这条路径（EXECUTION_FAILED ->
    # final_status='waiting_human'），不是三处手写 SQL 站点之一。
    assert worker._set_job(
        first["job_id"],
        "waiting_human",
        "视频供应商执行失败，供应商原文：涉嫌版权问题。系统已停止对本镜的自动"
        "付费重试，转人工处理。请在页面核对供应商任务状态，或编辑本镜提示词后"
        "重抽、或切换视频供应商。",
    ) is True
    assert worker._set_version(
        first["version_id"], status="waiting_human", error="同上"
    ) is True

    verify = sqlite3.connect(database)
    locked = verify.execute(
        "SELECT status,video_slot_active FROM jobs WHERE id=?", (first["job_id"],)
    ).fetchone()
    assert locked == ("waiting_human", 0)
    locked_version = verify.execute(
        "SELECT status,video_slot_active FROM shot_versions WHERE id=?",
        (first["version_id"],),
    ).fetchone()
    assert locked_version == ("waiting_human", 0)
    verify.close()

    second = worker.enqueue_shot(
        "s1", reroll=True, operation_idempotency_key="fresh-key-AAAA",
    )
    assert second["reused"] is False
    assert second["job_id"] != first["job_id"]
    assert second["version_id"] != first["version_id"]

    # 双击防重复扣费必须仍然有效：紧跟着换第二把新鲜幂等键，命中的是刚刚
    # 新建、真正在途的任务，必须复用，不能再建第三份付费版本。
    third = worker.enqueue_shot(
        "s1", reroll=True, operation_idempotency_key="fresh-key-BBBB",
    )
    assert third["reused"] is True
    assert third.get("reused_reason") == "in_flight"

    verify2 = sqlite3.connect(database)
    assert verify2.execute(
        "SELECT COUNT(*) FROM shot_versions WHERE shot_id='s1'"
    ).fetchone()[0] == 2
    verify2.close()


def test_database_rejects_second_active_job_and_version() -> None:
    conn = _conn()
    _seed_shot(conn)
    conn.executescript(db.INTEGRITY_SCHEMA)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               video_slot_active,created_at
           ) VALUES('v1','s1',1,'p','i1','running',1,1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               video_slot_active,created_at,updated_at
           ) VALUES('j1','video','s1','v1','e1','p1','running',1,1,1)"""
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO jobs(
                   id,kind,shot_id,episode_id,project_id,status,
                   video_slot_active,created_at,updated_at
               ) VALUES('j2','video','s1','e1','p1','queued',1,2,2)"""
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,
                   video_slot_active,created_at
               ) VALUES('v2','s1',2,'p','i2','queued',1,2)"""
        )


def test_init_db_reconciles_legacy_duplicate_active_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy-video-duplicates.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    _seed_shot(conn)
    conn.executemany(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        [
            ("v-paid", "s1", 1, "p", "i1", "running", "provider-task", 1),
            ("v-local", "s1", 2, "p", "i2", "queued", None, 2),
        ],
    )
    conn.executemany(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,provider_create_state,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "j-paid", "video", "s1", "v-paid", "e1", "p1", "running",
                1, "accepted", 1, 1,
            ),
            (
                "j-local", "video", "s1", "v-local", "e1", "p1", "queued",
                0, "not_started", 2, 2,
            ),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())

    db.init_db()

    migrated = db.get_conn()
    jobs = {
        row["id"]: dict(row)
        for row in migrated.execute(
            """SELECT id,status,video_slot_active,cancellation_requested
                 FROM jobs ORDER BY id"""
        ).fetchall()
    }
    assert jobs["j-paid"]["video_slot_active"] == 1
    assert jobs["j-local"] == {
        "id": "j-local",
        "status": "cancelled",
        "video_slot_active": 0,
        "cancellation_requested": 1,
    }
    assert migrated.execute(
        "SELECT COUNT(*) FROM jobs WHERE shot_id='s1' AND video_slot_active=1"
    ).fetchone()[0] == 1
    indexes = {
        row[1] for row in migrated.execute("PRAGMA index_list(jobs)").fetchall()
    }
    assert "uq_jobs_active_video_shot" in indexes


def test_manual_retry_cannot_take_slot_from_another_active_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.system_api as system_api

    conn = _conn()
    _seed_shot(conn)
    conn.executescript(db.INTEGRITY_SCHEMA)
    conn.executemany(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,video_slot_active,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        [
            ("v-active", "s1", 1, "p", "i1", "running", None, 1, 1),
            ("v-retry", "s1", 2, "p", "i2", "failed", "provider-task", 0, 2),
        ],
    )
    conn.executemany(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_create_state,provider_non_cancellable,video_slot_active,
               created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "j-active", "video", "s1", "v-active", "e1", "p1", "queued",
                "not_started", 0, 1, 1, 1,
            ),
            (
                "j-retry", "video", "s1", "v-retry", "e1", "p1", "failed",
                "accepted", 1, 0, 2, 2,
            ),
        ],
    )
    conn.commit()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)

    with pytest.raises(system_api.HTTPException) as conflict:
        system_api.retry_job("j-retry")

    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "SHOT_VIDEO_ACTIVE_CONFLICT"
    assert conn.execute(
        "SELECT video_slot_active FROM jobs WHERE id='j-retry'"
    ).fetchone()[0] == 0
