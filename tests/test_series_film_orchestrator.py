"""连播台串行主循环的行为回归：跳过判据、失败即停、暂停/取消三分支。

照 tests/test_project_video_queue_outcomes.py 的写法：内存 sqlite + 直接
monkeypatch 各处 ``get_conn``，绕开真实剧本/分镜/视频生成，只验证
``orchestrator.run_series_film`` 自身的编排逻辑（五步一 monkeypatch 到
``stages.stage_is_complete``/``stages.run_stage`` 两个符号，因为
``orchestrator.py`` 用 ``from . import stages`` 走模块限定访问，这两个符号
就是全部调用点的唯一绑定，不需要额外的 patch_series_ops_everywhere helper）。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from app import db
from app.domain.series_ops import merge, orchestrator, state
from app.domain.series_ops import stages as series_stages
from app.orchestration.engine import WorkflowRecorder


def _conn(episode_nos: list[int]) -> tuple[sqlite3.Connection, list[dict]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    entries = []
    for no in episode_nos:
        eid = f"e{no}"
        conn.execute(
            "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES(?,?,?,?,0)",
            (eid, "p", no, "planned"),
        )
        entries.append(state.new_episode_entry(eid, no))
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES('run-series','series_film','project','p','CREATED','fp',1)"""
    )
    conn.commit()
    return conn, entries


def _patch_conn(monkeypatch, conn: sqlite3.Connection) -> None:
    import app.evidence.repository as evidence_repository
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine

    for module in (evidence_repository, orchestration_engine, state_machine, orchestrator, state):
        monkeypatch.setattr(module, "get_conn", lambda: conn)


def _row(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT * FROM workflow_runs WHERE id='run-series'").fetchone())


@pytest.mark.asyncio
async def test_all_stages_skipped_then_merge_runs_and_succeeds(monkeypatch) -> None:
    conn, entries = _conn([1, 2])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: True)
    merge_calls: list[tuple] = []
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *a: merge_calls.append(a) or {})

    run_state = state.new_state(1, 2, entries)
    recorder = WorkflowRecorder("run-series")
    await orchestrator.run_series_film("p", run_state, recorder)

    row = _row(conn)
    assert row["status"] == "SUCCEEDED"
    assert len(merge_calls) == 1
    for entry in run_state["episodes"]:
        assert all(v == "skipped" for v in entry["stages"].values())
    assert run_state["current_stage"] is None
    assert run_state["current_episode_no"] is None


@pytest.mark.asyncio
async def test_stage_failure_stops_serial_loop_before_next_episode(monkeypatch) -> None:
    conn, entries = _conn([1, 2])
    _patch_conn(monkeypatch, conn)

    done_stages: set[tuple[str, str]] = set()
    calls: list[tuple[str, str]] = []

    def fake_complete(stage, _conn, episode_id):
        return (stage, episode_id) in done_stages

    async def fake_run_stage(stage, episode_id, _run_id):
        calls.append((stage, episode_id))
        if stage == "storyboard":
            raise RuntimeError("分镜台炸了")
        done_stages.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", fake_complete)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)

    run_state = state.new_state(1, 2, entries)
    recorder = WorkflowRecorder("run-series")
    await orchestrator.run_series_film("p", run_state, recorder)

    row = _row(conn)
    assert row["status"] == "FAILED"
    assert row["failure_code"] == "SERIES_FILM_STAGE_FAILED"
    ep1 = run_state["episodes"][0]
    assert ep1["stages"]["screenplay"] == "done"
    assert ep1["stages"]["storyboard"] == "failed"
    assert "分镜台" in ep1["error"]
    assert ep1["stages"]["confirm"] == "pending"
    ep2 = run_state["episodes"][1]
    assert all(v == "pending" for v in ep2["stages"].values())
    assert not any(episode_id == "e2" for _stage, episode_id in calls)


@pytest.mark.asyncio
async def test_stage_completing_run_but_criteria_still_unmet_fails_closed(monkeypatch) -> None:
    """步骤跑完但产物信号仍不满足——fail-closed，不静默放行到下一步。"""
    conn, entries = _conn([1])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)

    async def fake_run_stage(_stage, _episode_id, _run_id):
        return None  # 跑完但什么也没改变

    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)

    run_state = state.new_state(1, 1, entries)
    recorder = WorkflowRecorder("run-series")
    await orchestrator.run_series_film("p", run_state, recorder)

    row = _row(conn)
    assert row["status"] == "FAILED"
    assert run_state["episodes"][0]["stages"]["screenplay"] == "failed"
    assert "完成判据" in run_state["episodes"][0]["error"]


@pytest.mark.asyncio
async def test_cancelled_with_pause_request_marks_paused_external(monkeypatch) -> None:
    conn, entries = _conn([1])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)

    async def cancelling_stage(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(series_stages, "run_stage", cancelling_stage)

    run_state = state.new_state(1, 1, entries)
    recorder = WorkflowRecorder("run-series")
    state.request_pause("p")
    try:
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.run_series_film("p", run_state, recorder)
    finally:
        state.clear_pause("p")

    row = _row(conn)
    assert row["status"] == "PAUSED_EXTERNAL"
    assert row["failure_code"] == "USER_PAUSED"
    assert not state.is_pause_requested("p")


@pytest.mark.asyncio
async def test_cancelled_without_pause_or_shutdown_marks_cancelled(monkeypatch) -> None:
    conn, entries = _conn([1])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)

    async def cancelling_stage(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(series_stages, "run_stage", cancelling_stage)

    run_state = state.new_state(1, 1, entries)
    recorder = WorkflowRecorder("run-series")
    assert not state.is_pause_requested("p")
    with pytest.raises(asyncio.CancelledError):
        await orchestrator.run_series_film("p", run_state, recorder)

    row = _row(conn)
    assert row["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_merge_failure_marks_run_failed(monkeypatch) -> None:
    conn, entries = _conn([1])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: True)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)

    def boom(*_a):
        raise RuntimeError("ffmpeg 挂了")

    monkeypatch.setattr(merge, "build_series_film", boom)

    run_state = state.new_state(1, 1, entries)
    recorder = WorkflowRecorder("run-series")
    await orchestrator.run_series_film("p", run_state, recorder)

    row = _row(conn)
    assert row["status"] == "FAILED"
    assert row["failure_code"] == "SERIES_FILM_MERGE_FAILED"
    assert "ffmpeg 挂了" in run_state["error"]


# --------------------------------------------------- HTTPException 文案折叠


def test_exception_message_plain_string_detail_passthrough() -> None:
    exc = HTTPException(409, "本项目已有连播台运行中")
    assert orchestrator._exception_message(exc) == "本项目已有连播台运行中"


def test_exception_message_prefers_message_key() -> None:
    exc = HTTPException(
        409, {"code": "SERIES_FILM_ALREADY_ACTIVE", "message": "本项目已有连播台运行中"}
    )
    assert orchestrator._exception_message(exc) == "本项目已有连播台运行中"


def test_exception_message_falls_back_to_hard_gates_errors() -> None:
    """确认门禁失败的真实契约形状：storyboard-confirm.v3，没有顶层 message。"""
    detail = {
        "contract_version": "storyboard-confirm.v3",
        "episode_id": "e1",
        "hard_gates": {
            "passed": False,
            "errors": ["分镜没有覆盖整集原文：3000 字里有 1294 字（43%）没有任何镜头对应"],
            "retry_exhausted_fallback": False,
            "findings": [],
        },
        "warnings": [],
        "unlocks": [],
        "recovery_action": "返回分镜台继续修复；全部硬门禁通过后再确认",
    }
    message = orchestrator._exception_message(HTTPException(409, detail))
    assert "分镜没有覆盖整集原文" in message
    assert "处理办法：返回分镜台继续修复；全部硬门禁通过后再确认" in message
    assert "contract_version" not in message
    assert "storyboard-confirm.v3" not in message


def test_exception_message_falls_back_to_errors_list() -> None:
    exc = HTTPException(422, {"errors": ["缺少必填字段 a", "缺少必填字段 b"]})
    assert orchestrator._exception_message(exc) == "缺少必填字段 a\n缺少必填字段 b"


def test_exception_message_falls_back_to_issues_list() -> None:
    exc = HTTPException(422, {"issues": ["issue 1"]})
    assert orchestrator._exception_message(exc) == "issue 1"


def test_exception_message_last_resort_str_dict() -> None:
    """message/hard_gates/errors/issues 都没有，才退回整字典兜底，不能报错。"""
    exc = HTTPException(500, {"unexpected_shape": True})
    assert orchestrator._exception_message(exc) == str({"unexpected_shape": True})


@pytest.mark.asyncio
async def test_stage_failure_with_confirmation_gate_detail_shows_human_message(
    monkeypatch,
) -> None:
    """真实故障复现：确认阶段被硬门禁拦下时，run.error 不再是整个契约字典。"""
    conn, entries = _conn([1])
    _patch_conn(monkeypatch, conn)
    # 只让 confirm 走到 run_stage；其余阶段判定为已完成直接跳过。
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda stage, *_a: stage != "confirm")

    gate_detail = {
        "contract_version": "storyboard-confirm.v3",
        "hard_gates": {
            "passed": False,
            "errors": ["分镜没有覆盖整集原文：3000 字里有 1294 字（43%）没有任何镜头对应"],
        },
        "recovery_action": "返回分镜台继续修复；全部硬门禁通过后再确认",
    }

    async def fake_run_stage(_stage, _episode_id, _run_id):
        raise HTTPException(409, gate_detail)

    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)

    run_state = state.new_state(1, 1, entries)
    recorder = WorkflowRecorder("run-series")
    await orchestrator.run_series_film("p", run_state, recorder)

    ep1 = run_state["episodes"][0]
    assert ep1["stages"]["confirm"] == "failed"
    assert "分镜没有覆盖整集原文" in ep1["error"]
    assert "处理办法：返回分镜台继续修复" in ep1["error"]
    assert "contract_version" not in ep1["error"]
    assert "'passed': False" not in ep1["error"]
