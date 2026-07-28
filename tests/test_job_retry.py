"""job 级瞬时故障自动重试（修复 video_create 超时永久判失败的问题）。

背景：上游 /contents/generations/tasks 正常 <1.5s 返回，但偶发分钟级抖动会让
create_video_task 连续撞到 30s 读超时。_post_json 的调用内重试只覆盖 ~90s，扛不过去，
旧逻辑会把整镜任务永久判失败。现在可重试（retryable）的 ProviderError 会做 job 级延迟重排。
"""
import asyncio
import sqlite3

from app import config, db, worker
from app.hiagent import ProviderError


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


def _seed_job(conn: sqlite3.Connection, job_id: str = "j1", status: str = "running") -> None:
    conn.execute(
        "INSERT INTO jobs(id, kind, status, created_at, updated_at) VALUES(?,?,?,?,?)",
        (job_id, "video", status, 1.0, 1.0),
    )
    conn.commit()


def test_retryable_error_requeues_job(monkeypatch) -> None:
    conn = _conn()
    _seed_job(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    requeued: list[str] = []
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: requeued.append(jid))

    async def run() -> bool:
        # 把退避压到 0，避免测试真的等 30s
        monkeypatch.setattr(config, "VIDEO_JOB_RETRY_BASE_DELAY", 0.0)
        scheduled = worker._schedule_job_retry("j1", ProviderError("调用超时（31379ms）", retryable=True))
        # 让 _requeue_after 协程跑完
        await asyncio.sleep(0)
        await asyncio.gather(*list(worker._retry_tasks))
        return scheduled

    scheduled = asyncio.run(run())
    assert scheduled is True
    assert requeued == ["j1"]
    row = conn.execute("SELECT status, error, retry_count, next_retry_at FROM jobs WHERE id='j1'").fetchone()
    assert row["status"] == "queued"
    assert row["retry_count"] == 1 and row["next_retry_at"] is not None
    assert "自动排队" in row["error"]


def test_non_retryable_error_not_requeued(monkeypatch) -> None:
    conn = _conn()
    _seed_job(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    async def run() -> bool:
        return worker._schedule_job_retry("j1", ProviderError("Seedance 任务失败：版权受限"))

    assert asyncio.run(run()) is False
    assert conn.execute("SELECT retry_count FROM jobs WHERE id='j1'").fetchone()["retry_count"] == 0


def test_running_provider_task_is_deferred_without_consuming_retry_budget(monkeypatch) -> None:
    conn = _conn()
    _seed_job(conn)
    conn.execute(
        "UPDATE jobs SET lease_owner='worker-a', lease_expires_at=9999999999 "
        "WHERE id='j1'"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    main_requeued: list[str] = []
    poll_requeued: list[str] = []
    monkeypatch.setattr(worker._queue, "put_nowait", main_requeued.append)
    monkeypatch.setattr(worker._poll_queue, "put_nowait", poll_requeued.append)

    async def run() -> bool:
        scheduled = worker._defer_provider_poll(
            "j1", "provider-task-1", lease_owner="worker-a", delay=0,
        )
        await asyncio.sleep(0)
        if worker._retry_tasks:
            await asyncio.gather(*list(worker._retry_tasks))
        return scheduled

    assert asyncio.run(run()) is True
    row = conn.execute(
        "SELECT status, error, retry_count, next_retry_at, lease_owner "
        "FROM jobs WHERE id='j1'"
    ).fetchone()
    assert row["status"] == "waiting_provider"
    assert row["retry_count"] == 0
    assert row["next_retry_at"] is not None
    assert row["lease_owner"] is None
    assert "不会重复提交" in row["error"]
    assert main_requeued == []
    assert poll_requeued == ["j1"]


def test_retry_budget_exhausts(monkeypatch) -> None:
    conn = _conn()
    _seed_job(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(config, "VIDEO_JOB_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(config, "VIDEO_JOB_MAX_RETRIES", 3)
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: None)

    async def run() -> list[bool]:
        results = []
        for _ in range(4):
            results.append(
                worker._schedule_job_retry("j1", ProviderError("调用超时", retryable=True)))
            await asyncio.sleep(0)
        if worker._retry_tasks:
            await asyncio.gather(*list(worker._retry_tasks))
        return results

    results = asyncio.run(run())
    # 前 3 次安排重试，第 4 次预算耗尽 → 交回永久失败逻辑
    assert results == [True, True, True, False]
    assert conn.execute("SELECT retry_count FROM jobs WHERE id='j1'").fetchone()["retry_count"] == 3


def test_fenced_worker_cannot_finish_or_requeue_new_owner_job(monkeypatch) -> None:
    conn = _conn()
    _seed_job(conn)
    conn.execute(
        "UPDATE jobs SET lease_owner='new-worker', lease_expires_at=9999999999 WHERE id='j1'"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    assert worker._set_job("j1", "succeeded", lease_owner="old-worker") is False
    assert worker._schedule_job_retry(
        "j1", ProviderError("超时", retryable=True), lease_owner="old-worker",
    ) is False
    row = conn.execute(
        "SELECT status, lease_owner, retry_count FROM jobs WHERE id='j1'"
    ).fetchone()
    assert dict(row) == {
        "status": "running", "lease_owner": "new-worker", "retry_count": 0,
    }


def test_assert_job_lease_can_extend_ttl_for_long_qa_window(monkeypatch) -> None:
    """成功尾段的自动 QA 可能超过默认 180s lease，续租必须支持拉长 TTL。"""
    seen: list[float] = []

    def fake_renew(job_id: str, owner: str, *, lease_seconds: float = 180.0) -> bool:
        assert (job_id, owner) == ("j1", "worker-a")
        seen.append(lease_seconds)
        return True

    monkeypatch.setattr(worker.media_scheduler, "renew_lease", fake_renew)
    worker._assert_job_lease("j1", "worker-a", lease_seconds=360.0)
    assert seen == [360.0]
    # 默认路径仍保持 180s，避免无关续租被意外拉长。
    worker._assert_job_lease("j1", "worker-a")
    assert seen == [360.0, 180.0]


def test_provider_stage_heartbeat_renews_lease_while_reference_work_is_slow(monkeypatch) -> None:
    """Slow keyframe/VLM work must not look abandoned after the initial lease."""
    renewals: list[tuple[str, str, float]] = []

    def fake_renew(job_id: str, owner: str, *, lease_seconds: float = 180.0) -> bool:
        renewals.append((job_id, owner, lease_seconds))
        return True

    monkeypatch.setattr(worker.media_scheduler, "renew_lease", fake_renew)

    async def run() -> str:
        async def slow_reference_work() -> str:
            await asyncio.sleep(0.045)
            return "ready"

        return await worker._await_with_job_lease_heartbeat(
            slow_reference_work(),
            job_id="j1",
            owner="worker-a",
            lease_seconds=0.03,
            heartbeat_interval_s=0.01,
        )

    assert asyncio.run(run()) == "ready"
    assert len(renewals) >= 3
    assert all(item == ("j1", "worker-a", 0.03) for item in renewals)


def test_provider_stage_heartbeat_cancels_fenced_worker_before_stale_write(monkeypatch) -> None:
    """A worker that lost ownership cannot finish and publish its old snapshot."""
    writes: list[str] = []
    cancelled: list[bool] = []

    monkeypatch.setattr(
        worker.media_scheduler,
        "renew_lease",
        lambda *_args, **_kwargs: False,
    )

    async def run() -> None:
        async def stale_worker() -> None:
            try:
                await asyncio.sleep(0.05)
                writes.append("stale checkpoint")
            finally:
                cancelled.append(True)

        try:
            await worker._await_with_job_lease_heartbeat(
                stale_worker(),
                job_id="j1",
                owner="old-worker",
                heartbeat_interval_s=0.01,
            )
        except worker.LeaseLost:
            return
        raise AssertionError("lost ownership must raise LeaseLost")

    asyncio.run(run())
    assert writes == []
    assert cancelled == [True]
