"""项目级连播队列：runner 主循环、入队/出队/暂停/继续、连续失败自动停队。

2026-09-04 起并发度不再恒为 1（用户拍板「连播改成并行任务」）：同一项目同时在跑的连播任务数由
``settings.series_queue_concurrency`` 决定（缺省 3，夹在 1..8；同一个数也限制项目内同时生成的集数，
见 ``concurrency.py``——任务内部各集也并行，10 集一组的任务同样吃到并发）。runner（登记为 ``series_queue``）
只做调度：按 queue_seq 依次把排队任务派成子任务（各自登记为 ``series_task_run``/task_id），子任务
自己开连接、自己跑五步；runner 等任一子任务结束就补位。暂停/服务重启 = 取消 runner，runner 在
``CancelledError`` 里把全部子任务一起取消，子任务按「谁请求的取消」分类：明确点了取消这一个任务
→ ``idle``；其余（暂停/重启）→ 退回 ``queued`` 并保留进度。任务内部各集仍按顺序跑——要让多集
并行不再需要按 group_size=1 切任务。

两个模块级可变单例（``_pause_requests``/``_cancel_requests``）只 add/discard，
不做 global 重绑定，写法照 ``state.py`` 旧版 ``_PAUSE_REQUESTS`` 的先例。
"""
from __future__ import annotations

import asyncio
import time

from fastapi import HTTPException

from app import task_registry
from app.db import get_conn, now

from . import orchestrator, state, tasks
from .concurrency import queue_concurrency

TASK_KIND = "series_queue"
CHILD_KIND = "series_task_run"
WORKFLOW_TYPE = "series_task"

_CONSECUTIVE_FAILURE_LIMIT = 3

#: 任务开跑前要确认没被占用的三种单集任务，值是给用户看的工作台名。检查放在
#: 「取到任务、真要开跑」这一刻而不是入队时：入队到执行之间可能隔几小时，那时
#: 候的检查结论早就过期了，而这里的结论紧挨着实际使用。
_pause_requests: set[str] = set()
_cancel_requests: set[str] = set()


class _PreflightFailed(RuntimeError):
    """任务开跑前的前置检查未通过；``code`` 直接落进 ``workflow_runs.failure_code``。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------- runner

async def _run_queue(project_id: str) -> None:
    """调度器：补位派发子任务直到没有排队任务或被暂停；被取消时连带取消全部子任务。"""
    children: dict[str, asyncio.Task] = {}
    try:
        while True:
            try:
                conn = get_conn()
                if _is_paused(conn, project_id):
                    return
                _reap_finished(children)
                _spawn_children(conn, project_id, children)
                if not children:
                    return
                await asyncio.wait(list(children.values()), return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- 启动期 database is locked 之类的瞬时错误
                # runner 一死，排队的任务就永远停在 queued 而没人知道（2026-09-04 B 上两次重启后实测）。
                # 记错误、歇 5 秒再来，不让一次瞬时错误变成静默停摆。
                from app.errors import log_error  # 延迟导入：app.errors 反向依赖 db/quota 链
                log_error(exc, action="series_queue.runner", context={"project_id": project_id})
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        for child in children.values():
            child.cancel()
        if children:
            await asyncio.gather(*children.values(), return_exceptions=True)
        raise


def _reap_finished(children: dict[str, asyncio.Task]) -> None:
    for task_id in [task_id for task_id, child in children.items() if child.done()]:
        child = children.pop(task_id)
        _record_unexpected_exit(task_id, child)


def _record_unexpected_exit(task_id: str, child: asyncio.Task) -> None:
    """子任务在落终态之前就异常退出（如启动期 database is locked）：任务行还是 running/
    queued，调度器会立刻再派一次、再炸一次，空转到天亮。这里把它落成 failed 并记错误，
    让人看得见；修好后重新入队即可。"""
    if child.cancelled():
        return
    exc = child.exception()
    if exc is None:
        return
    from app.errors import log_error  # 延迟导入：app.errors 反向依赖 db/quota 链，模块级会成环
    rec = log_error(exc, action="series_task.child_exit", context={"task_id": task_id})
    conn = get_conn()
    row = conn.execute("SELECT status FROM series_tasks WHERE id=?", (task_id,)).fetchone()
    if row and row["status"] in ("running", "queued"):
        tasks.mark_failed(conn, task_id, f"连播任务异常退出（{rec.error_id}）：{exc}"[:1000])


def _spawn_children(conn, project_id: str, children: dict[str, asyncio.Task]) -> None:
    """按 queue_seq 补位到并发上限；刚派出但还没把自己改成 running 的任务用 exclude 挡住重复派发。"""
    while len(children) < queue_concurrency():
        row = tasks.next_queued_task(conn, project_id, exclude=set(children))
        if row is None:
            return
        coro = _run_one_task(project_id, row)
        try:
            children[row["id"]] = task_registry.spawn(CHILD_KIND, row["id"], coro, project_id=project_id)
        except RuntimeError:
            coro.close()  # 同一任务的子任务已在跑（重启残留），这一轮不重复派发
            return


async def _run_one_task(project_id: str, row: dict) -> None:
    conn = get_conn()  # 子任务自己的连接：与 runner、与别的子任务互不共享事务
    task_id = row["id"]
    recorder = _create_recorder(project_id, row)
    tasks.mark_running(conn, task_id, recorder.run_id)
    try:
        progress = _preflight(conn, project_id, row)
    except _PreflightFailed as exc:
        # recorder 还没 start() 过（CREATED），必须先转 RUNNING 才能落 FAILED 终态。
        recorder.start()
        recorder.fail_result(str(exc), failure_code=exc.code, conn=None)
        _handle_task_failed(conn, task_id, str(exc))
        _maybe_auto_pause(conn, project_id, task_id)
        return
    try:
        await orchestrator.run_task(
            project_id, task_id, row["episode_from"], row["episode_to"], progress, recorder,
        )
    except asyncio.CancelledError:
        _handle_task_cancelled(conn, project_id, task_id, recorder)
        raise
    except orchestrator.StageFailure as exc:
        _handle_task_failed(conn, task_id, str(exc))
        _maybe_auto_pause(conn, project_id, task_id)
    else:
        tasks.mark_succeeded(conn, task_id)


def _preflight(conn, project_id: str, row: dict) -> dict:
    """开跑前的前置检查（区间内缺集），不过就让这个任务失败并如实说明原因。
    「某集正被单集任务占用」不再在这里拦：2026-09-04 实测服务重启后平台会把上一轮
    的映射台运行恢复起来，占用者正是本任务自己的产出，拦下来等于每次重启都判一次
    假失败；改由编排器逐步等待（``orchestrator._wait_until_episode_free``）。"""
    return _init_progress(conn, project_id, row)


def _init_progress(conn, project_id: str, row: dict) -> dict:
    existing = state.load_progress(row)
    if existing.get("episodes"):
        # 重启/重新入队时进度树里残留的「进行中」是上一轮进程留下的：那一步会按完成
        # 判据重新跑或跳过，先复位成 pending，界面上不再显示一堆假的并行中。
        for entry in existing["episodes"]:
            entry["waiting"] = None
            for stage, value in (entry.get("stages") or {}).items():
                if value == "running":
                    entry["stages"][stage] = "pending"
        return existing
    ordered, missing = tasks.fetch_range_episodes(conn, project_id, row["episode_from"], row["episode_to"])
    if missing:
        raise _PreflightFailed(
            "以下集号缺失，无法执行：" + "、".join(str(n) for n in missing),
            "SERIES_TASK_MISSING_EPISODES",
        )
    entries = [state.new_episode_entry(r["id"], r["episode_no"]) for r in ordered]
    return state.new_progress(entries)


def _create_recorder(project_id: str, row: dict):
    # 函数内 import：app.orchestration.engine 反过来会拉起一整条 evidence/
    # state_machine 依赖链，模块级导入会在 app.domain.series_ops 包初始化期
    # 就拉这条链，照旧例（旧 orchestrator.py 的路由核心函数）延迟到调用时。
    from app.orchestration.engine import WorkflowRecorder, fingerprint

    return WorkflowRecorder.create(
        workflow_type=WORKFLOW_TYPE,
        scope_type="series_task",
        scope_id=row["id"],
        input_fingerprint=fingerprint(row["id"], row["episode_from"], row["episode_to"], time.time()),
        requested_by="user",
        trigger_type="manual",
        policy_snapshot={"serial": queue_concurrency() == 1, "concurrency": queue_concurrency()},
        config_snapshot={
            "project_id": project_id,
            "episode_from": row["episode_from"], "episode_to": row["episode_to"],
        },
    )


def _handle_task_failed(conn, task_id: str, message: str) -> None:
    tasks.mark_failed(conn, task_id, message or "任务执行失败")


def _maybe_auto_pause(conn, project_id: str, task_id: str) -> None:
    streak = tasks.consecutive_failures(conn, project_id)
    if streak < _CONSECUTIVE_FAILURE_LIMIT:
        return
    reason = f"连续 {streak} 个任务失败，队列已自动暂停（最近失败：{task_id}）"
    _set_paused(conn, project_id, True, reason)


def _handle_task_cancelled(conn, project_id: str, task_id: str, recorder) -> None:
    if task_id in _cancel_requests:
        _cancel_requests.discard(task_id)
        tasks.mark_idle(conn, task_id)
        recorder.cancel("用户取消了这个连播任务", conn=None)
        return
    _pause_requests.discard(project_id)
    tasks.mark_queued_again(conn, task_id)
    if task_registry.shutdown_in_progress():
        recorder.pause_external("服务重启，连播任务等待自动恢复", conn=None)
    else:
        recorder.pause_external("用户暂停，连播任务已保留进度", conn=None)


# --------------------------------------------------------------- 队列状态读写

def _is_paused(conn, project_id: str) -> bool:
    row = conn.execute(
        "SELECT paused FROM series_queue_state WHERE project_id=?", (project_id,),
    ).fetchone()
    return bool(row["paused"]) if row else False


def _set_paused(conn, project_id: str, paused: bool, stop_reason: str | None) -> None:
    conn.execute(
        "INSERT INTO series_queue_state(project_id, paused, stop_reason, updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
        "paused=excluded.paused, stop_reason=excluded.stop_reason, updated_at=excluded.updated_at",
        (project_id, int(paused), stop_reason, now()),
    )
    conn.commit()


def _ensure_runner(project_id: str) -> None:
    if task_registry.active(TASK_KIND, project_id):
        return
    coro = _run_queue(project_id)
    try:
        task_registry.spawn(TASK_KIND, project_id, coro, project_id=project_id)
    except RuntimeError:
        coro.close()


def queue_snapshot(conn, project_id: str) -> dict:
    return {**tasks.queue_snapshot(conn, project_id), "concurrency": queue_concurrency()}


# --------------------------------------------------------------------- 路由核心

async def enqueue(project_id: str, task_ids: list[str], force: bool) -> dict:
    conn = get_conn()
    accepted, skipped = tasks.enqueue_many(conn, project_id, task_ids, force=force)
    _set_paused(conn, project_id, False, None)
    if accepted:
        _ensure_runner(project_id)
    return {"enqueued": len(accepted), "skipped": skipped, "queue": queue_snapshot(conn, project_id)}


async def cancel(project_id: str, task_ids: list[str]) -> dict:
    conn = get_conn()
    cancelled: list[str] = []
    running_targets: list[str] = []
    for task_id in dict.fromkeys(task_ids):
        row = tasks.get_task_row(conn, project_id, task_id)
        if row is None:
            continue
        if row["status"] == "queued":
            tasks.mark_idle(conn, task_id)
            cancelled.append(task_id)
        elif row["status"] == "running":
            running_targets.append(task_id)
    for task_id in running_targets:
        # 只取消这一个子任务，runner 与其它在跑的任务不受影响；子任务不在本进程
        # （重启前的残留 running 行）时直接落 idle。
        _cancel_requests.add(task_id)
        if not await task_registry.cancel_and_wait(CHILD_KIND, task_id):
            _cancel_requests.discard(task_id)
            tasks.mark_idle(conn, task_id)
        cancelled.append(task_id)
    if running_targets and not _is_paused(conn, project_id):
        _ensure_runner(project_id)
    return {"cancelled": cancelled, "queue": queue_snapshot(conn, project_id)}


async def pause(project_id: str) -> dict:
    conn = get_conn()
    active = task_registry.active(TASK_KIND, project_id)
    running = tasks.queue_snapshot(conn, project_id)["running_task_id"]
    if not active and running is None:
        raise HTTPException(409, "没有运行中的连播任务队列")
    _pause_requests.add(project_id)
    _set_paused(conn, project_id, True, None)
    await task_registry.cancel_and_wait(TASK_KIND, project_id)
    _pause_requests.discard(project_id)
    return {"ok": True, "status": "paused", "queue": queue_snapshot(conn, project_id)}


async def resume(project_id: str) -> dict:
    conn = get_conn()
    if tasks.count_queued(conn, project_id) == 0:
        raise HTTPException(409, "队列没有可继续的任务")
    _set_paused(conn, project_id, False, None)
    _ensure_runner(project_id)
    return {"ok": True, "status": "running", "queue": queue_snapshot(conn, project_id)}


# --------------------------------------------------------------------- 恢复

def resume_after_recovery(project_id: str) -> bool:
    """开机恢复：项目未处于暂停状态且还有排队任务时，重新启动 runner。"""
    conn = get_conn()
    if _is_paused(conn, project_id):
        return False
    if tasks.count_queued(conn, project_id) == 0:
        return False
    _ensure_runner(project_id)
    return True
