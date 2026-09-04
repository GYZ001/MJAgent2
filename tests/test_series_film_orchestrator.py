"""连播任务台单任务主循环的行为回归：跳过判据、失败即停、取消原样冒泡。

照 tests/test_project_video_queue_outcomes.py 的写法：内存 sqlite + 直接
monkeypatch 各处 ``get_conn``，绕开真实剧本/分镜/视频生成，只验证
``orchestrator.run_task`` 自身的编排逻辑（五步一 monkeypatch 到
``stages.stage_is_complete``/``stages.run_stage`` 两个符号，因为
``orchestrator.py`` 用 ``from . import stages`` 走模块限定访问，这两个符号
就是全部调用点的唯一绑定，不需要额外的 patch_series_ops_everywhere helper）。

取消（暂停/服务重启/明确取消某个任务）的分类落终态逻辑现在归 ``queue.py``
（见 tests/test_series_queue.py），本文件只验证 ``run_task`` 在被取消时原样
冒泡 ``CancelledError``、不触碰终态。
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
           ) VALUES('run-task','series_task','series_task','task-1','CREATED','fp',1)"""
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
    return dict(conn.execute("SELECT * FROM workflow_runs WHERE id='run-task'").fetchone())


@pytest.mark.asyncio
async def test_all_stages_skipped_then_merge_runs_and_succeeds(monkeypatch) -> None:
    conn, entries = _conn([1, 2])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: True)
    merge_calls: list[tuple] = []
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *a: merge_calls.append(a) or {})

    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")
    await orchestrator.run_task("p", "task-1", 1, 2, progress, recorder)

    row = _row(conn)
    assert row["status"] == "SUCCEEDED"
    assert len(merge_calls) == 1
    for entry in progress["episodes"]:
        assert all(v == "skipped" for v in entry["stages"].values())
    assert progress["current_stage"] is None
    assert progress["current_episode_no"] is None


@pytest.mark.asyncio
async def test_stage_failure_skips_episode_and_continues_without_merge(monkeypatch) -> None:
    """一集失败：记下、跳过、继续下一集；结束时不合并成片、任务失败并列出失败的集。"""
    conn, entries = _conn([1, 2])
    _patch_conn(monkeypatch, conn)

    done_stages: set[tuple[str, str]] = set()
    calls: list[tuple[str, str]] = []
    merge_calls: list = []

    def fake_complete(stage, _conn, episode_id):
        return (stage, episode_id) in done_stages

    async def fake_run_stage(stage, episode_id, _run_id):
        calls.append((stage, episode_id))
        if stage == "storyboard" and episode_id == "e1":
            raise RuntimeError("分镜台炸了")
        done_stages.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", fake_complete)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *a: merge_calls.append(a) or {})

    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")
    with pytest.raises(orchestrator.StageFailure) as excinfo:
        await orchestrator.run_task("p", "task-1", 1, 2, progress, recorder)

    row = _row(conn)
    assert row["status"] == "FAILED"
    assert row["failure_code"] == "SERIES_TASK_STAGE_FAILED"
    ep1 = progress["episodes"][0]
    assert ep1["stages"]["screenplay"] == "done"
    assert ep1["stages"]["storyboard"] == "failed"
    assert "分镜台" in ep1["error"]
    assert ep1["stages"]["confirm"] == "pending"  # 本集内部仍 fail-closed：不进下一步
    ep2 = progress["episodes"][1]
    assert all(v == "done" for v in ep2["stages"].values())  # 下一集照常跑完
    assert any(episode_id == "e2" for _stage, episode_id in calls)
    assert merge_calls == []  # 有失败集就不合并
    assert progress["current_episode_no"] is None and progress["current_stage"] is None
    assert "1 集失败" in str(excinfo.value) and "第1集 分镜台 失败" in str(excinfo.value)
    assert progress["error"] == str(excinfo.value) and ep2["error"] is None


@pytest.mark.asyncio
async def test_stage_completing_run_but_criteria_still_unmet_fails_closed(monkeypatch) -> None:
    """步骤跑完但产物信号仍不满足——fail-closed，不静默放行到下一步。"""
    conn, entries = _conn([1])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)

    async def fake_run_stage(_stage, _episode_id, _run_id):
        return None  # 跑完但什么也没改变

    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)

    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")
    with pytest.raises(orchestrator.StageFailure):
        await orchestrator.run_task("p", "task-1", 1, 1, progress, recorder)

    row = _row(conn)
    assert row["status"] == "FAILED"
    assert progress["episodes"][0]["stages"]["screenplay"] == "failed"
    assert "完成判据" in progress["episodes"][0]["error"]


@pytest.mark.asyncio
async def test_merge_failure_marks_run_failed(monkeypatch) -> None:
    conn, entries = _conn([1])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: True)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)

    def boom(*_a):
        raise RuntimeError("ffmpeg 挂了")

    monkeypatch.setattr(merge, "build_series_film", boom)

    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")
    with pytest.raises(orchestrator.StageFailure):
        await orchestrator.run_task("p", "task-1", 1, 1, progress, recorder)

    row = _row(conn)
    assert row["status"] == "FAILED"
    assert row["failure_code"] == "SERIES_TASK_MERGE_FAILED"
    assert "ffmpeg 挂了" in progress["error"]


@pytest.mark.asyncio
async def test_cancelled_propagates_without_touching_terminal_status(monkeypatch) -> None:
    """取消的分类落终态归 queue.py；run_task 自己只管原样冒泡，不touch终态。"""
    conn, entries = _conn([1])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)

    async def cancelling_stage(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(series_stages, "run_stage", cancelling_stage)

    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")
    with pytest.raises(asyncio.CancelledError):
        await orchestrator.run_task("p", "task-1", 1, 1, progress, recorder)

    row = _row(conn)
    assert row["status"] == "RUNNING"  # start() 已跑过，取消没有落任何终态


# --------------------------------------------------- HTTPException 文案折叠


def test_exception_message_plain_string_detail_passthrough() -> None:
    exc = HTTPException(409, "本项目已有连播任务运行中")
    assert orchestrator._exception_message(exc) == "本项目已有连播任务运行中"


def test_exception_message_prefers_message_key() -> None:
    exc = HTTPException(
        409, {"code": "SERIES_TASK_ALREADY_ACTIVE", "message": "本项目已有连播任务运行中"}
    )
    assert orchestrator._exception_message(exc) == "本项目已有连播任务运行中"


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
    """真实故障复现：确认阶段被硬门禁拦下时，进度树的 error 不再是整个契约字典。"""
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

    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")
    with pytest.raises(orchestrator.StageFailure):
        await orchestrator.run_task("p", "task-1", 1, 1, progress, recorder)

    ep1 = progress["episodes"][0]
    assert ep1["stages"]["confirm"] == "failed"
    assert "分镜没有覆盖整集原文" in ep1["error"]
    assert "处理办法：返回分镜台继续修复" in ep1["error"]
    assert "contract_version" not in ep1["error"]
    assert "'passed': False" not in ep1["error"]


async def test_requeued_task_clears_previous_round_errors(monkeypatch) -> None:
    """重新入队：上一轮的 progress.error / 分集 error 必须在开跑时清掉，
    否则界面在"进行中"时还挂着上一轮的失败横幅（2026-09-03 连播台实测）。"""
    conn, entries = _conn([1, 2])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: True)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: True)
    conn.execute(
        "INSERT INTO series_tasks(id,project_id,episode_from,episode_to,progress_json,created_at,updated_at) "
        "VALUES('task-1','p',1,2,'{}',0,0)"
    )
    progress = state.new_progress(entries)
    progress["error"] = "第1集 生成台 失败：上一轮的旧错误"
    progress["episodes"][0]["error"] = "第1集 生成台 失败：上一轮的旧错误"
    progress["episodes"][0]["stages"]["video"] = "failed"
    recorder = WorkflowRecorder("run-task")
    await orchestrator.run_task("p", "task-1", 1, 2, progress, recorder)
    assert progress["error"] is None
    assert all(entry["error"] is None for entry in progress["episodes"])
    assert progress["episodes"][0]["stages"]["video"] == "skipped"
    persisted = state.load_progress(dict(conn.execute(
        "SELECT progress_json FROM series_tasks WHERE id='task-1'"
    ).fetchone()))
    assert persisted["error"] is None and persisted["episodes"][0]["error"] is None



# --------------------------------------------------------------------- 集级并行
@pytest.mark.asyncio
async def test_episodes_inside_one_task_run_in_parallel_up_to_slots(monkeypatch) -> None:
    """并行集数 2、一个任务三集：前两集交错运行，第三集只能在某集整体结束后开跑。"""
    from app.domain.series_ops import concurrency

    conn, entries = _conn([1, 2, 3])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(concurrency, "queue_concurrency", lambda: 2)
    order: list[str] = []
    done_pairs: set[tuple[str, str]] = set()

    async def fake_run_stage(stage, episode_id, _run_id):
        order.append(f"{episode_id}:{stage}:start")
        await asyncio.sleep(0.005)
        order.append(f"{episode_id}:{stage}:end")
        done_pairs.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", lambda stage, _c, eid: (stage, eid) in done_pairs)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})
    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")

    await orchestrator.run_task("p", "task-1", 1, 3, progress, recorder)

    assert all(v == "done" for e in progress["episodes"] for v in e["stages"].values())
    first_e2 = min(i for i, ev in enumerate(order) if ev.startswith("e2:"))
    last_e1 = max(i for i, ev in enumerate(order) if ev.startswith("e1:"))
    assert first_e2 < last_e1, "同一任务里第 1、2 集必须交错（并行）"
    first_e3 = min(i for i, ev in enumerate(order) if ev.startswith("e3:"))
    last_e2 = max(i for i, ev in enumerate(order) if ev.startswith("e2:"))
    assert first_e3 > min(last_e1, last_e2), "第 3 集只能等某集整体结束后再开（并行集数 2）"
    assert progress["running_episode_nos"] == [] and progress["current_episode_no"] is None
    assert concurrency.episode_slots.running("p") == 0


@pytest.mark.asyncio
async def test_parallel_episode_failure_only_marks_that_episode(monkeypatch) -> None:
    from app.domain.series_ops import concurrency

    conn, entries = _conn([1, 2])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(concurrency, "queue_concurrency", lambda: 2)
    done_pairs: set[tuple[str, str]] = set()

    async def fake_run_stage(stage, episode_id, _run_id):
        await asyncio.sleep(0.002)
        if episode_id == "e1" and stage == "storyboard":
            raise RuntimeError("分镜门禁没过")
        done_pairs.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", lambda stage, _c, eid: (stage, eid) in done_pairs)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: pytest.fail("有集失败不得合并"))
    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")

    with pytest.raises(orchestrator.StageFailure):
        await orchestrator.run_task("p", "task-1", 1, 2, progress, recorder)

    assert progress["episodes"][0]["stages"]["storyboard"] == "failed"
    assert all(v == "done" for v in progress["episodes"][1]["stages"].values())
    assert concurrency.episode_slots.running("p") == 0


@pytest.mark.asyncio
async def test_running_episode_nos_and_current_are_derived_while_running(monkeypatch) -> None:
    from app.domain.series_ops import concurrency

    conn, entries = _conn([1, 2])
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(concurrency, "queue_concurrency", lambda: 2)
    release = asyncio.Event()
    snapshots: list[tuple[list[int], int | None, str | None]] = []

    async def fake_run_stage(stage, episode_id, _run_id):
        if stage == "screenplay":
            await asyncio.sleep(0.005)
            snapshots.append((list(progress["running_episode_nos"]), progress["current_episode_no"], progress["current_stage"]))
            await release.wait()

    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    progress = state.new_progress(entries)
    recorder = WorkflowRecorder("run-task")
    task = asyncio.create_task(orchestrator.run_task("p", "task-1", 1, 2, progress, recorder))
    for _ in range(50):
        await asyncio.sleep(0.005)
        if len(snapshots) >= 2:
            break
    release.set()
    with pytest.raises(orchestrator.StageFailure):  # stage_is_complete 恒 False → 两集都判失败
        await task
    assert snapshots and snapshots[-1] == ([1, 2], 1, "screenplay")
