"""墙钟到期后：仍在排队就有界续期，整条流水线卡住才收口（2026-09-06 第 14 轮 6 集结构性失败）。"""
from __future__ import annotations

import sqlite3

from app.video_supervisor import deadline
from app.video_supervisor.constants import (
    DEADLINE_EXTENSION_S,
    DEADLINE_STALL_GRACE_S,
    MAX_DEADLINE_EXTENSIONS,
)
from app.video_supervisor.models import VideoSupervisorCheckpoint

_NOW = 1_000_000.0


def _conn(last_terminal_at: float | None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs(id TEXT, kind TEXT, status TEXT, updated_at REAL)")
    if last_terminal_at is not None:
        conn.execute(
            "INSERT INTO jobs VALUES('j1','video','succeeded',?)", (last_terminal_at,)
        )
    conn.commit()
    return conn


def _cp(**over) -> VideoSupervisorCheckpoint:
    base = dict(
        episode_id="ep1",
        phase="PLANNING_COVERAGE",
        started_at=_NOW - 4 * 3600,
        deadline_at=_NOW - 1.0,
        shot_state={"1": {"adopted_version_id": "ver_1"}, "2": {"adopted_version_id": None}},
    )
    base.update(over)
    return VideoSupervisorCheckpoint(**base)


def test_queued_episode_is_extended_not_closed_out(monkeypatch) -> None:
    monkeypatch.setattr(deadline, "now", lambda: _NOW)
    cp = _cp()
    assert deadline.resolve_deadline(cp, conn=_conn(_NOW - 60)) == "extended"
    assert cp.deadline_extensions == 1
    assert cp.deadline_extended_until == _NOW + DEADLINE_EXTENSION_S
    # 续期后未到期：同一 tick 再问一次是 within，不会连环续期
    assert deadline.resolve_deadline(cp, conn=_conn(_NOW - 60)) == "within"
    assert cp.deadline_extensions == 1


def test_stalled_pipeline_closes_out(monkeypatch) -> None:
    monkeypatch.setattr(deadline, "now", lambda: _NOW)
    cp = _cp()
    stale = _NOW - DEADLINE_STALL_GRACE_S - 1
    assert deadline.resolve_deadline(cp, conn=_conn(stale)) == "closeout"
    assert cp.deadline_extensions == 0
    assert deadline.resolve_deadline(_cp(), conn=_conn(None)) == "closeout"  # 一个作业都没有


def test_extension_budget_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(deadline, "now", lambda: _NOW)
    cp = _cp(deadline_extensions=MAX_DEADLINE_EXTENSIONS)
    assert deadline.resolve_deadline(cp, conn=_conn(_NOW - 60)) == "closeout"


def test_episode_without_pending_shots_is_not_extended(monkeypatch) -> None:
    monkeypatch.setattr(deadline, "now", lambda: _NOW)
    cp = _cp(shot_state={"1": {"adopted_version_id": "ver_1"}})
    assert deadline.resolve_deadline(cp, conn=_conn(_NOW - 60)) == "closeout"


def test_before_deadline_is_within_and_extension_wins_over_stale_grant(monkeypatch) -> None:
    monkeypatch.setattr(deadline, "now", lambda: _NOW)
    assert deadline.resolve_deadline(_cp(deadline_at=_NOW + 1), conn=_conn(None)) == "within"
    # 授权墙钟已过、续期未过：生效截止取较晚者
    cp = _cp(deadline_extended_until=_NOW + 600)
    assert deadline.effective_deadline(cp) == _NOW + 600
    assert deadline.resolve_deadline(cp, conn=_conn(None)) == "within"


def test_no_deadline_at_all_is_within(monkeypatch) -> None:
    monkeypatch.setattr(deadline, "now", lambda: _NOW)
    assert deadline.resolve_deadline(_cp(deadline_at=None), conn=_conn(None)) == "within"
