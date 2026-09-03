"""连播任务台：跑一个任务的五台（映射/分镜/确认/生成/成片）+ merge 主循环。

只负责「给定一个任务的集号区间和进度树，把它跑完」；队列的入队/出队/暂停/
连续失败停队等生命周期决策一律不在这里，属于 ``queue.py``——``queue.py`` 创建
``WorkflowRecorder``、决定下一个任务是谁，调用本模块的 ``run_task``；本模块只
管单任务内部的串行推进，失败/取消都原样冒泡给调用方决定怎么办。

fail-closed 语义不变：任一步失败即停在那一集（不跳过、不兜底），已满足完成
判据的步骤直接标 ``skipped``。
"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app.db import get_conn

from . import merge, stages, state


class StageFailure(RuntimeError):
    """信号：已经把失败详情记进了进度树 + WorkflowRecorder，调用方只需要
    更新队列侧的任务状态，不必再包一层异常处理终态。"""


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
    ``recovery_action``）——按 message → hard_gates.errors（逐条换行）→
    errors/issues 列表 → 最后才兜底 str(detail) 依次尝试；有 ``recovery_action``
    再追加一句处理办法。只改文案，不改 fail-closed 行为。
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

async def run_task(
    project_id: str,
    task_id: str,
    episode_from: int,
    episode_to: int,
    progress: dict,
    recorder,
) -> None:
    """跑完一个任务的五台 + merge；成功正常返回，失败抛 ``StageFailure``，
    取消原样冒泡 ``asyncio.CancelledError``（不在这里落终态，由 ``queue.py``
    按暂停/取消/服务重启分类后处理）。
    """
    # 重新入队的任务带着上一轮的失败信息进来：tasks.enqueue_many 只清 series_tasks.error 列，
    # 进度树里的 progress.error 由本模块负责——任务一开跑就是新一轮，旧错误不再成立。
    # 不清的话 task_summary 取「列 or 进度树」会让界面在"进行中"时还挂着上一轮的失败横幅。
    progress["error"] = None
    recorder.start()
    state.persist_progress(task_id, progress)
    try:
        await _run_task_body(project_id, task_id, episode_from, episode_to, progress, recorder)
    except StageFailure:
        raise
    except Exception as exc:  # noqa: BLE001 -- 未预期的编排异常，如实落盘后转成 StageFailure
        state.persist_progress(task_id, progress)
        recorder.fail(exc, conn=None)
        raise StageFailure(str(exc)) from exc


async def _run_task_body(
    project_id: str, task_id: str, episode_from: int, episode_to: int,
    progress: dict, recorder,
) -> None:
    for entry in progress["episodes"]:
        progress["current_episode_no"] = entry["episode_no"]
        state.persist_progress(task_id, progress)
        await _run_episode(task_id, entry, progress, recorder)
    progress["current_episode_no"] = None
    progress["current_stage"] = "merge"
    state.persist_progress(task_id, progress)
    await _run_merge(task_id, project_id, episode_from, episode_to, progress, recorder)
    progress["current_stage"] = None
    state.persist_progress(task_id, progress)
    recorder.succeed("连播任务已完成，成片已生成", conn=None)


async def _run_episode(task_id: str, entry: dict, progress: dict, recorder) -> None:
    episode_id = entry["episode_id"]
    entry["error"] = None  # 本集重新进入处理，上一轮留在条目上的失败信息作废
    for stage in stages.STAGE_SEQUENCE:
        progress["current_stage"] = stage
        state.persist_progress(task_id, progress)
        await _run_single_stage(stage, episode_id, task_id, entry, progress, recorder)


async def _run_single_stage(
    stage: str, episode_id: str, task_id: str, entry: dict, progress: dict, recorder,
) -> None:
    if stages.stage_is_complete(stage, get_conn(), episode_id):
        entry["stages"][stage] = "skipped"
        state.persist_progress(task_id, progress)
        return
    entry["stages"][stage] = "running"
    state.persist_progress(task_id, progress)
    try:
        await stages.run_stage(stage, episode_id, recorder.run_id)
        ok = stages.stage_is_complete(stage, get_conn(), episode_id)
    except Exception as exc:
        _fail_stage(entry, progress, task_id, recorder, stage, _exception_message(exc))
        raise StageFailure from exc
    if not ok:
        _fail_stage(entry, progress, task_id, recorder, stage, "步骤已运行但未达到完成判据")
        raise StageFailure
    entry["stages"][stage] = "done"
    state.persist_progress(task_id, progress)


def _fail_stage(entry: dict, progress: dict, task_id: str, recorder, stage: str, detail: str) -> None:
    entry["stages"][stage] = "failed"
    message = f"第{entry['episode_no']}集 {stages.STAGE_LABELS[stage]} 失败：{detail}"[:1000]
    entry["error"] = message
    progress["error"] = message
    state.persist_progress(task_id, progress)
    recorder.fail_result(message, failure_code="SERIES_TASK_STAGE_FAILED", conn=None)


async def _run_merge(
    task_id: str, project_id: str, episode_from: int, episode_to: int,
    progress: dict, recorder,
) -> None:
    episode_nos = [e["episode_no"] for e in progress["episodes"]]
    if merge.merge_is_current(project_id, episode_from, episode_to, episode_nos):
        return
    try:
        await asyncio.to_thread(
            merge.build_series_film, project_id, episode_from, episode_to, episode_nos,
        )
    except Exception as exc:
        message = f"连播成片合并失败：{_exception_message(exc)}"[:1000]
        progress["error"] = message
        state.persist_progress(task_id, progress)
        recorder.fail_result(message, failure_code="SERIES_TASK_MERGE_FAILED", conn=None)
        raise StageFailure from exc
