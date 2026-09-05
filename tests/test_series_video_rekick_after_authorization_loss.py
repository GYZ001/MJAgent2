"""补齐运行因流程自身造成的授权失效收口时，连播台再发起一次，不判本集失败（2026-09-05 第 18 集）。

上一段取帧关闭后旧计划不再合法 → RELEASE_QUALIFICATION_INVALID；紧接着重规划的新授权撞上
旧计划变更 → UPSTREAM_VERSION_CHANGED。两者都不是本集内容的问题，按当前分镜再发起即可。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import db
from app.domain.series_ops import stages as series_stages


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e','p',1,'generating',0)"
    )
    conn.commit()
    return conn


def _wire(monkeypatch, conn, outcomes: list[str | None]):
    """每次 kick 写一条终态运行；outcomes[i] 为 None 表示那次补齐成功。"""
    kicks: list[int] = []
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    monkeypatch.setattr(series_stages.task_registry, "active", lambda kind, eid: False)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(series_stages.asyncio, "sleep", no_sleep)

    async def fake_kick(episode_id: str, run_id: str) -> None:
        index = len(kicks)
        kicks.append(index)
        failure = outcomes[index] if index < len(outcomes) else outcomes[-1]
        conn.execute(
            """INSERT INTO workflow_runs(id,workflow_type,scope_type,scope_id,status,failure_message,
                                         input_fingerprint,started_at,updated_at)
               VALUES(?,'episode_video_completion','episode','e',?,?,'fp',?,?)""",
            (f"run-{index}", "PARTIAL" if failure else "SUCCEEDED", failure, index + 1, index + 1),
        )
        conn.commit()

    monkeypatch.setattr(series_stages, "_kick_video_completion", fake_kick)
    monkeypatch.setattr(
        series_stages, "video_complete",
        lambda _c, _e: bool(kicks) and (outcomes[min(len(kicks) - 1, len(outcomes) - 1)] is None),
    )
    monkeypatch.setattr(series_stages, "_stalled_video_reason", lambda _e: "：细节")
    return kicks


@pytest.mark.asyncio
async def test_authorization_loss_is_retried_and_succeeds(monkeypatch) -> None:
    conn = _conn()
    kicks = _wire(monkeypatch, conn, ["RELEASE_QUALIFICATION_INVALID", "UPSTREAM_VERSION_CHANGED: 分镜 Artifact 已变更", None])
    await series_stages._run_video("e", "series-run")
    assert len(kicks) == 3


@pytest.mark.asyncio
async def test_authorization_loss_retry_is_bounded(monkeypatch) -> None:
    conn = _conn()
    kicks = _wire(monkeypatch, conn, ["UPSTREAM_VERSION_CHANGED"])
    with pytest.raises(RuntimeError, match="未能补齐"):
        await series_stages._run_video("e", "series-run")
    assert len(kicks) == series_stages._AUTHORIZATION_LOST_RETRIES + 1


@pytest.mark.asyncio
async def test_other_failures_are_not_retried(monkeypatch) -> None:
    conn = _conn()
    kicks = _wire(monkeypatch, conn, ["PARTIAL_NO_USABLE_CANDIDATE"])
    with pytest.raises(RuntimeError, match="未能补齐"):
        await series_stages._run_video("e", "series-run")
    assert len(kicks) == 1
