from __future__ import annotations

import time

import pytest

from app import task_registry
from app.db import get_conn
from app import video_supervisor


class _FakeRecorder:
    """Stand-in for app.orchestration.engine.WorkflowRecorder in this test."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.partial_calls: list[str] = []
        self.fail_calls: list[Exception] = []
        self.cancel_calls: list[str] = []

    def start(self) -> None:
        pass

    def partial(self, outcome, conn=None) -> None:
        self.partial_calls.append(outcome)

    def fail(self, exc, conn=None) -> None:
        self.fail_calls.append(exc)

    def cancel(self, reason: str = "", conn=None) -> None:
        self.cancel_calls.append(reason)


def _seed_stale_episode(episode_id: str, project_id: str, run_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES(?,?,?,0)",
        (project_id, "P", "created"),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,status,created_at,
               video_completion_mode,active_video_run_id,storyboard_artifact_id
           ) VALUES(?,?,1,'generating',0,'complete',?,'sb-1')""",
        (episode_id, project_id, run_id),
    )
    # Very old updated_at so the heartbeat looks stale (now() - 0 >> 60s).
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES(?,'episode_video_completion','episode',?,'RUNNING','fp',0)""",
        (run_id, episode_id),
    )
    conn.commit()


def test_watchdog_takeover_rolls_back_pending_adoption_before_marking_failed_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression lock: ``reconcile_stale_video_supervisors``'s inner
    ``reconcile_one`` must roll back before calling ``_mark_failed_closed`` /
    ``recorder.fail`` when ``_deadline_closeout`` raises, exactly like the two
    already-guarded call sites in app/domain/video_ops.py (lines ~2946-2965
    and ~3811-3829). Before this fix, ``reconcile_one``'s except block called
    ``_mark_failed_closed`` (which commits via ``save_checkpoint`` reusing the
    open transaction) with no rollback first, so any half-done write left by
    ``_deadline_closeout`` on the shared task-cached connection would get
    silently carried into the checkpoint's commit instead of being discarded.
    """
    episode_id = "ep_watchdog_rollback_test"
    project_id = "proj_watchdog_rollback_test"
    old_run_id = "run-old"
    _seed_stale_episode(episode_id, project_id, old_run_id)

    conn = get_conn()

    fake_cp = video_supervisor.VideoSupervisorCheckpoint(
        episode_id=episode_id,
        run_id=old_run_id,
        phase="DISPATCHING",
        deadline_at=time.time() + 3600,
        last_heartbeat_at=0.0,
        grant_id="grant-1",
    )

    monkeypatch.setattr(video_supervisor, "load_latest_checkpoint", lambda _eid: fake_cp)
    monkeypatch.setattr(
        video_supervisor, "_verify_supervisor_paid_authority", lambda cp, *, stage: None
    )
    monkeypatch.setattr(task_registry, "active", lambda *_a, **_k: False)

    recorders: list[_FakeRecorder] = []

    class _FakeWorkflowRecorder:
        @staticmethod
        def create(**kwargs):
            del kwargs
            rec = _FakeRecorder(run_id="run-new")
            recorders.append(rec)
            return rec

    monkeypatch.setattr(
        "app.orchestration.engine.WorkflowRecorder", _FakeWorkflowRecorder
    )
    monkeypatch.setattr("app.orchestration.engine.fingerprint", lambda *a, **k: "fp")

    # These must resolve get_conn() themselves at call time rather than close
    # over the test function's own `conn`: they run *inside* the
    # asyncio.run() task, where app.db.get_conn() is cached per
    # asyncio.current_task() — a different cache key (and a different
    # connection object) than the thread-local one this synchronous test
    # function gets when it calls get_conn() outside any running loop. Using
    # video_supervisor.get_conn() here is what makes this test exercise the
    # *same* connection that reconcile_one's own `conn = get_conn()` closure
    # captured, which is the whole point of the regression lock.
    def fake_deadline_closeout(cp, *, run_id, reason):
        del cp, run_id, reason
        task_conn = video_supervisor.get_conn()
        task_conn.execute("CREATE TABLE IF NOT EXISTS fake_pending_adoption(marker TEXT)")
        task_conn.execute(
            "INSERT INTO fake_pending_adoption(marker) VALUES('half_done_adoption')"
        )
        # Real code would still call invalidate_episode_delivery_authority and
        # only conn.commit() after that returns; we stop here uncommitted,
        # exactly like that call raising partway through.
        raise RuntimeError("模拟 _deadline_closeout 写完半途候选采用后、commit 前失败")

    checkpoint_commits: list[bool] = []

    def fake_mark_failed_closed(cp, *, run_id, reason):
        del cp, run_id, reason
        task_conn = video_supervisor.get_conn()
        # Mirrors save_checkpoint reusing an already-open transaction instead
        # of starting a fresh one, then committing it.
        if not task_conn.in_transaction:
            task_conn.execute("BEGIN IMMEDIATE")
        task_conn.execute("CREATE TABLE IF NOT EXISTS fake_checkpoint(marker TEXT)")
        task_conn.execute("INSERT INTO fake_checkpoint(marker) VALUES('checkpoint_write')")
        task_conn.commit()
        checkpoint_commits.append(True)

    monkeypatch.setattr(video_supervisor, "_deadline_closeout", fake_deadline_closeout)
    monkeypatch.setattr(video_supervisor, "_mark_failed_closed", fake_mark_failed_closed)

    import asyncio

    recovered = asyncio.run(video_supervisor.reconcile_stale_video_supervisors())

    assert recovered == 1, "watchdog 应当接管这个 heartbeat 过期的 episode"
    assert checkpoint_commits == [True], (
        "本测试要验证的正是 _mark_failed_closed 落检查点之后的提交时机；"
        "没有走到这一步说明测试提前在别处失败，结论不成立"
    )
    leaked = conn.execute(
        "SELECT COUNT(*) AS c FROM fake_pending_adoption WHERE marker='half_done_adoption'"
    ).fetchone()["c"]
    assert leaked == 0, (
        "watchdog 接管收口失败时，_deadline_closeout 半途的候选采用写入不能被 "
        "_mark_failed_closed 的检查点提交一并带下去"
    )
    assert recorders and recorders[0].fail_calls, "recorder.fail 应当被调用一次"
