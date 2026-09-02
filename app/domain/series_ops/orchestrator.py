"""连播台串行主循环，以及四条路由背后的核心函数（启动/暂停/继续）。

主循环照 ``app.domain.video_ops.project_queue_run._run_project_video_completion_queue``
的整体形状：``WorkflowRecorder`` 记终态，``asyncio.CancelledError`` 时区分
「用户暂停」「服务重启」「取消」三分支，状态每步落盘。
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import HTTPException

from app import task_registry
from app.db import get_conn

from . import merge, stages, state


class _StageFailure(RuntimeError):
    """信号：某步骤已失败并记录终态，调用方应立即停止而不再包一层异常。"""


def _exception_message(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return _format_http_exception_detail(detail)
        return str(detail)
    return str(exc)


def _format_http_exception_detail(detail: dict) -> str:
    """把结构化的 ``HTTPException.detail`` 折成一句给用户看的话。

    确认门禁失败时 detail 不是 ``{"message": ...}``，而是
    ``storyboard-confirm.v3`` 契约字典（``hard_gates.errors[]`` +
    ``recovery_action``）——旧实现只认 ``message``/``code``，两者都没有就
    ``str(detail)`` 把整个字典原样吐给用户。按 message → hard_gates.errors
    （逐条换行）→ errors/issues 列表 → 最后才兜底 str(detail) 依次尝试；
    有 ``recovery_action`` 再追加一句处理办法。只改文案，不改 fail-closed
    行为——门禁该拦还是拦，这里只负责把拦截原因说人话。
    """
    message = detail.get("message") or detail.get("code")
    if not message:
        hard_gates = detail.get("hard_gates")
        if isinstance(hard_gates, dict) and hard_gates.get("errors"):
            message = "\n".join(str(item) for item in hard_gates["errors"])
    if not message:
        for key in ("errors", "issues"):
            value = detail.get(key)
            if isinstance(value, list) and value:
                message = "\n".join(str(item) for item in value)
                break
    if not message:
        message = str(detail)
    recovery_action = detail.get("recovery_action")
    if recovery_action:
        message = f"{message}\n处理办法：{recovery_action}"
    return message


# --------------------------------------------------------------------- 主循环

async def run_series_film(project_id: str, run_state: dict, recorder) -> None:
    recorder.start()
    state.persist(recorder.run_id, run_state)
    try:
        await _run_series_film_body(project_id, run_state, recorder)
    except _StageFailure:
        return
    except asyncio.CancelledError:
        _handle_cancelled(project_id, run_state, recorder)
        raise
    except Exception as exc:  # noqa: BLE001 -- 未预期的编排异常，如实落盘后继续冒泡
        state.persist(recorder.run_id, run_state)
        recorder.fail(exc, conn=None)
        raise


async def _run_series_film_body(project_id: str, run_state: dict, recorder) -> None:
    for entry in run_state["episodes"]:
        run_state["current_episode_no"] = entry["episode_no"]
        state.persist(recorder.run_id, run_state)
        await _run_episode(entry, run_state, recorder)
    run_state["current_episode_no"] = None
    run_state["current_stage"] = "merge"
    state.persist(recorder.run_id, run_state)
    await _run_merge(project_id, run_state, recorder)
    run_state["current_stage"] = None
    state.persist(recorder.run_id, run_state)
    recorder.succeed("连播台已完成，连播成片已生成", conn=None)


async def _run_episode(entry: dict, run_state: dict, recorder) -> None:
    episode_id = entry["episode_id"]
    for stage in stages.STAGE_SEQUENCE:
        run_state["current_stage"] = stage
        state.persist(recorder.run_id, run_state)
        await _run_single_stage(stage, episode_id, entry, run_state, recorder)


async def _run_single_stage(
    stage: str, episode_id: str, entry: dict, run_state: dict, recorder,
) -> None:
    if stages.stage_is_complete(stage, get_conn(), episode_id):
        entry["stages"][stage] = "skipped"
        state.persist(recorder.run_id, run_state)
        return
    entry["stages"][stage] = "running"
    state.persist(recorder.run_id, run_state)
    try:
        await stages.run_stage(stage, episode_id, recorder.run_id)
        ok = stages.stage_is_complete(stage, get_conn(), episode_id)
    except Exception as exc:
        _fail_stage(entry, run_state, recorder, stage, _exception_message(exc))
        raise _StageFailure from exc
    if not ok:
        _fail_stage(entry, run_state, recorder, stage, "步骤已运行但未达到完成判据")
        raise _StageFailure
    entry["stages"][stage] = "done"
    state.persist(recorder.run_id, run_state)


def _fail_stage(entry: dict, run_state: dict, recorder, stage: str, detail: str) -> None:
    entry["stages"][stage] = "failed"
    message = f"第{entry['episode_no']}集 {stages.STAGE_LABELS[stage]} 失败：{detail}"[:1000]
    entry["error"] = message
    run_state["error"] = message
    state.persist(recorder.run_id, run_state)
    recorder.fail_result(message, failure_code="SERIES_FILM_STAGE_FAILED", conn=None)


async def _run_merge(project_id: str, run_state: dict, recorder) -> None:
    episode_nos = [e["episode_no"] for e in run_state["episodes"]]
    episode_from, episode_to = run_state["episode_from"], run_state["episode_to"]
    if merge.merge_is_current(project_id, episode_from, episode_to, episode_nos):
        return
    try:
        await asyncio.to_thread(
            merge.build_series_film, project_id, episode_from, episode_to, episode_nos,
        )
    except Exception as exc:
        message = f"连播成片合并失败：{_exception_message(exc)}"[:1000]
        run_state["error"] = message
        state.persist(recorder.run_id, run_state)
        recorder.fail_result(message, failure_code="SERIES_FILM_MERGE_FAILED", conn=None)
        raise _StageFailure from exc


def _handle_cancelled(project_id: str, run_state: dict, recorder) -> None:
    state.persist(recorder.run_id, run_state)
    pause_requested = state.is_pause_requested(project_id)
    state.clear_pause(project_id)
    if task_registry.shutdown_in_progress() or pause_requested:
        recorder.pause_external(
            "用户暂停，连播台已保留进度" if pause_requested else "服务重启，连播台等待自动恢复",
            conn=None,
        )
        if pause_requested:
            conn = get_conn()
            conn.execute(
                "UPDATE workflow_runs SET failure_code='USER_PAUSED' WHERE id=?",
                (recorder.run_id,),
            )
            conn.commit()
    else:
        recorder.cancel("连播台已取消", conn=None)


# --------------------------------------------------------------- 路由核心函数

def _validate_range(episode_from: int, episode_to: int) -> None:
    if episode_from < 1 or episode_to < episode_from:
        raise HTTPException(422, "episode_from/episode_to 不合法")
    if episode_to - episode_from + 1 > 10:
        raise HTTPException(422, "连播跨度最多 10 集")


def _active_run_row(conn, project_id: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM workflow_runs
           WHERE workflow_type=? AND scope_type='project' AND scope_id=?
             AND status IN ('CREATED','RUNNING')
           ORDER BY updated_at DESC LIMIT 1""",
        (state.WORKFLOW_TYPE, project_id),
    ).fetchone()
    return dict(row) if row else None


def _assert_no_active_run(conn, project_id: str) -> None:
    active = _active_run_row(conn, project_id)
    if active or task_registry.active(state.TASK_KIND, project_id):
        raise HTTPException(409, {
            "code": "SERIES_FILM_ALREADY_ACTIVE",
            "message": "本项目已有连播台运行中",
            "run_id": active["id"] if active else None,
        })


def _assert_episodes_not_busy(episodes: list[dict]) -> None:
    for row in episodes:
        for kind in ("screenplay", "storyboard", "video_completion"):
            if task_registry.active(kind, row["id"]):
                raise HTTPException(409, {
                    "code": "SERIES_FILM_EPISODE_BUSY",
                    "message": f"第 {row['episode_no']} 集正被其他任务占用，请稍后再试",
                    "episode_no": row["episode_no"],
                })


async def start_series_film_core(project_id: str, body: dict) -> dict:
    from app.orchestration.engine import WorkflowRecorder, fingerprint

    episode_from = int(body.get("episode_from") or 0)
    episode_to = int(body.get("episode_to") or 0)
    _validate_range(episode_from, episode_to)
    conn = get_conn()
    episodes, missing = state.fetch_range_episodes(conn, project_id, episode_from, episode_to)
    if missing:
        raise HTTPException(422, f"以下集号在本项目不存在：{'、'.join(str(n) for n in missing)}")
    _assert_no_active_run(conn, project_id)
    _assert_episodes_not_busy(episodes)
    entries = [state.new_episode_entry(row["id"], row["episode_no"]) for row in episodes]
    run_state = state.new_state(episode_from, episode_to, entries)
    recorder = WorkflowRecorder.create(
        workflow_type=state.WORKFLOW_TYPE,
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, episode_from, episode_to, time.time()),
        requested_by="user",
        trigger_type="manual",
        policy_snapshot={"serial": True},
        config_snapshot={"series_state": run_state},
    )
    coro = run_series_film(project_id, run_state, recorder)
    try:
        task_registry.spawn(state.TASK_KIND, project_id, coro, project_id=project_id)
    except Exception as exc:
        coro.close()
        recorder.cancel("连播台任务未能启动", conn=None)
        raise HTTPException(503, "连播台任务未能启动，请重试") from exc
    return {
        "run_id": recorder.run_id,
        "status": "running",
        "episode_from": episode_from,
        "episode_to": episode_to,
        "episodes": entries,
    }


async def pause_series_film_core(project_id: str) -> dict:
    from app.orchestration.engine import WorkflowRecorder

    conn = get_conn()
    active = _active_run_row(conn, project_id)
    if not active:
        raise HTTPException(409, "没有运行中的连播台任务")
    state.request_pause(project_id)
    stopped = await task_registry.cancel_and_wait(state.TASK_KIND, project_id)
    if not stopped:
        refreshed = conn.execute(
            "SELECT status FROM workflow_runs WHERE id=?", (active["id"],)
        ).fetchone()
        if refreshed and refreshed["status"] == "RUNNING":
            WorkflowRecorder(active["id"]).pause_external(
                "用户暂停，连播台已保留进度", conn=None,
            )
            conn.execute(
                "UPDATE workflow_runs SET failure_code='USER_PAUSED' WHERE id=?",
                (active["id"],),
            )
            conn.commit()
        state.clear_pause(project_id)
    return {"ok": True, "status": "paused"}


async def resume_series_film_core(project_id: str) -> dict:
    from app.orchestration.engine import WorkflowRecorder

    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM workflow_runs
           WHERE workflow_type=? AND scope_type='project' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (state.WORKFLOW_TYPE, project_id),
    ).fetchone()
    if not row:
        raise HTTPException(409, "本项目还没有连播台运行记录")
    row = dict(row)
    if row["status"] in ("CREATED", "RUNNING"):
        raise HTTPException(409, {
            "code": "SERIES_FILM_ALREADY_ACTIVE",
            "message": "连播台已在运行中",
            "run_id": row["id"],
        })
    if row["status"] == "SUCCEEDED":
        raise HTTPException(409, "连播台已完成，无需继续")
    run_state = state.load_state(row)
    if not run_state:
        raise HTTPException(409, "连播台缺少可续跑的进度快照")
    recorder = WorkflowRecorder.create(
        workflow_type=state.WORKFLOW_TYPE,
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=row["input_fingerprint"],
        requested_by="user",
        trigger_type="resume",
        policy_snapshot=json.loads(row["policy_snapshot_json"] or "{}"),
        config_snapshot={"series_state": run_state},
        parent_run_id=row["id"],
    )
    coro = run_series_film(project_id, run_state, recorder)
    try:
        task_registry.spawn(state.TASK_KIND, project_id, coro, project_id=project_id)
    except Exception as exc:
        coro.close()
        recorder.cancel("连播台续跑未能启动", conn=None)
        raise HTTPException(503, "连播台续跑未能启动，请重试") from exc
    return {"ok": True, "status": "running", "run_id": recorder.run_id}
