"""VIDEO_PLAN_INVALID / GrantValidationError 明细落库回归测试。

见 docs/delivery_pipeline_rca_2026-08-29.md「问题二」：EP8 的视频补齐在启动
30s 内落入 WAITING_AUTHORIZATION，run_events 只留一条
``RUN_PARTIAL :: VIDEO_PLAN_INVALID``，payload 为空 ``{}``；同一时间窗
error_logs 没有任何记录——真正的根因是 ``VideoPlanValidationError.issues``
（模型规划编译/校验失败的完整明细）在 app/video_supervisor.py 的
``except GrantValidationError as exc: cp.outcome = exc.code`` 处被整段丢弃，
``str(exc)``（即那份 issues 的 JSON）从未被记录到任何地方。

本文件锁定修复：app.video_supervisor._record_grant_validation_failure 现在
把完整明细写进 run_events 的 RUN_PARTIAL payload，以及独立连接落库的
error_logs。红/绿两个测试用不同的观察点：绿测试跑修复后的真实函数；红测试
跑一份手写、冻结的「修复前处理逻辑」快照（不回退 app/video_supervisor.py
本体），证明这份细节此前确实完全没有落地。
"""
from __future__ import annotations

import json
import sqlite3

from app import db as db_mod
from app import video_supervisor
from app.completion_grant import GrantValidationError
from app.db import get_conn
from app.video_plan import VideoPlanValidationError

ISSUES = [
    {
        "code": "SHOT_CONTRACT_FINGERPRINT_STALE",
        "shot_id": "shot-7",
        "stored": "aaa",
        "current": "bbb",
    },
    {"code": "DEPENDENCY_CYCLE", "shot_id": "shot-3"},
]


def _seed_run(episode_id: str, project_id: str, run_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES(?,?,?,0)",
        (project_id, "P", "created"),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,status,created_at,
               video_completion_mode,storyboard_artifact_id
           ) VALUES(?,?,1,'generating',0,'complete','sb-1')""",
        (episode_id, project_id),
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES(?,'episode_video_completion','episode',?,'RUNNING','fp',0)""",
        (run_id, episode_id),
    )
    conn.commit()


def _wrapped_grant_validation_error(issues: list[dict]) -> GrantValidationError:
    """Reproduce the exact production wrapping shape used in
    ``app.video_supervisor._ensure_supervisor_video_plan``::

        except (ValueError, VideoPlanValidationError) as exc:
            raise GrantValidationError("VIDEO_PLAN_INVALID", str(exc)) from exc

    Using a real ``raise ... from exc`` (not a hand-set ``.__cause__``
    attribute) so the exception chain looks exactly like what production
    code produces.
    """
    try:
        raise VideoPlanValidationError(issues)
    except VideoPlanValidationError as inner:
        try:
            raise GrantValidationError("VIDEO_PLAN_INVALID", str(inner)) from inner
        except GrantValidationError as wrapped:
            return wrapped


def _read_error_logs(action: str) -> list[sqlite3.Row]:
    """Read back error_logs from a *second*, freshly opened connection to
    the same on-disk database file -- not the connection that wrote it --
    so a pass here proves the write is really committed to disk rather than
    an uncommitted echo visible only on the writer's own handle.
    """
    verify_conn = sqlite3.connect(str(db_mod.DB_PATH))
    verify_conn.row_factory = sqlite3.Row
    try:
        return verify_conn.execute(
            "SELECT message, meta_json, action FROM error_logs WHERE action=? ORDER BY ts",
            (action,),
        ).fetchall()
    finally:
        verify_conn.close()


def test_record_grant_validation_failure_writes_full_detail() -> None:
    episode_id = "ep_plan_invalid_obs"
    project_id = "proj_plan_invalid_obs"
    run_id = "run_plan_invalid_obs"
    _seed_run(episode_id, project_id, run_id)

    cp = video_supervisor.VideoSupervisorCheckpoint(
        episode_id=episode_id,
        run_id=run_id,
        grant_id="grant-plan-invalid-obs",
        phase="PLANNING_COVERAGE",
    )
    exc = _wrapped_grant_validation_error(ISSUES)

    video_supervisor._record_grant_validation_failure(
        cp, exc, run_id=run_id, stage="test_ensure_video_plan",
    )

    # --- run_events: RUN_PARTIAL now carries the full issues payload ---
    conn = get_conn()
    rows = conn.execute(
        "SELECT event_type,severity,message,payload_json FROM run_events "
        "WHERE run_id=? AND event_type='RUN_PARTIAL' ORDER BY id",
        (run_id,),
    ).fetchall()
    assert len(rows) == 1, "expected exactly one RUN_PARTIAL event"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["code"] == "VIDEO_PLAN_INVALID"
    assert payload["issues"] == ISSUES
    assert "SHOT_CONTRACT_FINGERPRINT_STALE" in rows[0]["message"]

    # --- error_logs: written on an independent connection (app.db.insert_error_log) ---
    error_rows = _read_error_logs("video_supervisor.test_ensure_video_plan")
    assert len(error_rows) == 1
    assert "SHOT_CONTRACT_FINGERPRINT_STALE" in error_rows[0]["message"]
    assert "DEPENDENCY_CYCLE" in error_rows[0]["message"]
    meta = json.loads(error_rows[0]["meta_json"] or "{}")
    assert meta.get("issues") == ISSUES


def test_record_grant_validation_failure_without_issues_still_logs_message() -> None:
    """Not every GrantValidationError wraps a VideoPlanValidationError (e.g.
    GRANT_NOT_FOUND / GRANT_REVOKED raised directly in app.completion_grant).
    Those must still get their short message recorded, not crash because
    there is no ``.issues`` to extract."""
    episode_id = "ep_plan_invalid_obs_plain"
    project_id = "proj_plan_invalid_obs_plain"
    run_id = "run_plan_invalid_obs_plain"
    _seed_run(episode_id, project_id, run_id)

    cp = video_supervisor.VideoSupervisorCheckpoint(
        episode_id=episode_id,
        run_id=run_id,
        grant_id="grant-plan-invalid-obs-plain",
        phase="PLANNING_COVERAGE",
    )
    exc = GrantValidationError("GRANT_REVOKED", "视频补齐授权已撤销")

    video_supervisor._record_grant_validation_failure(
        cp, exc, run_id=run_id, stage="test_plain",
    )

    conn = get_conn()
    rows = conn.execute(
        "SELECT payload_json,message FROM run_events WHERE run_id=? AND event_type='RUN_PARTIAL'",
        (run_id,),
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["code"] == "GRANT_REVOKED"
    assert "issues" not in payload
    assert "视频补齐授权已撤销" in rows[0]["message"]

    error_rows = _read_error_logs("video_supervisor.test_plain")
    assert len(error_rows) == 1
    assert "视频补齐授权已撤销" in error_rows[0]["message"]


def _pre_fix_handle_grant_validation_error(cp, exc) -> None:
    """Hand-copied body of every pre-fix ``except GrantValidationError as exc:``
    block in app/video_supervisor.py (e.g. the block that used to sit at
    lines 3390-3394, right after the ``_ensure_supervisor_video_plan`` call),
    reproduced verbatim as a frozen snapshot. Per project rule on red/green
    verification this does not revert app/video_supervisor.py itself -- it
    is an independent observation point proving what the old handler did.
    """
    cp.phase = "WAITING_AUTHORIZATION"
    cp.outcome = exc.code
    # The real pre-fix code then called `await _save_checkpoint_async(...)`
    # here, which only persists the checkpoint artifact -- it never touched
    # run_events or error_logs, so it is irrelevant to what this test proves
    # and is omitted to keep this a synchronous, dependency-free snapshot.


def test_pre_fix_handler_dropped_the_detail() -> None:
    """Red-test lock: confirms the historical bug this task fixes was real.
    Runs a hand-copied snapshot of the old handler (not app/video_supervisor.py
    itself) and asserts it left no trace in run_events or error_logs -- the
    exact blackhole docs/delivery_pipeline_rca_2026-08-29.md describes for
    EP8 (RUN_PARTIAL payload={}, no error_logs row).
    """
    episode_id = "ep_plan_invalid_obs_red"
    project_id = "proj_plan_invalid_obs_red"
    run_id = "run_plan_invalid_obs_red"
    _seed_run(episode_id, project_id, run_id)

    cp = video_supervisor.VideoSupervisorCheckpoint(
        episode_id=episode_id,
        run_id=run_id,
        grant_id="grant-plan-invalid-obs-red",
        phase="PLANNING_COVERAGE",
    )
    exc = _wrapped_grant_validation_error(ISSUES)

    _pre_fix_handle_grant_validation_error(cp, exc)

    assert cp.phase == "WAITING_AUTHORIZATION"
    assert cp.outcome == "VIDEO_PLAN_INVALID"

    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM run_events WHERE run_id=? AND event_type='RUN_PARTIAL'",
        (run_id,),
    ).fetchall()
    assert rows == [], (
        "pre-fix handler wrote no RUN_PARTIAL event -- cp.outcome=exc.code "
        "alone left nothing in run_events, exactly like the real EP8 incident"
    )

    error_rows = _read_error_logs("video_supervisor.test_ensure_video_plan_red")
    assert error_rows == [], "pre-fix handler wrote no error_logs row either"
