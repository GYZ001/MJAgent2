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
    worker._job_retry_counts.clear()

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
    assert worker._job_retry_counts["j1"] == 1
    assert requeued == ["j1"]
    row = conn.execute("SELECT status, error FROM jobs WHERE id='j1'").fetchone()
    assert row["status"] == "queued"
    assert "自动排队" in row["error"]


def test_non_retryable_error_not_requeued(monkeypatch) -> None:
    conn = _conn()
    _seed_job(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    worker._job_retry_counts.clear()

    async def run() -> bool:
        return worker._schedule_job_retry("j1", ProviderError("Seedance 任务失败：版权受限"))

    assert asyncio.run(run()) is False
    assert "j1" not in worker._job_retry_counts


def test_retry_budget_exhausts(monkeypatch) -> None:
    conn = _conn()
    _seed_job(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(config, "VIDEO_JOB_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(config, "VIDEO_JOB_MAX_RETRIES", 3)
    monkeypatch.setattr(worker._queue, "put_nowait", lambda jid: None)
    worker._job_retry_counts.clear()

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
    assert worker._job_retry_counts["j1"] == 3
