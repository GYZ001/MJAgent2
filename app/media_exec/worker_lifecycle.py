"""Worker 池生命周期（拆分自 ``run_job.py``；派发部分见 2026-08-30 拆出的
``.dispatch``）。

``_worker_target``/``_reference_worker_target``/``_video_ready_worker_target``/
``_poll_worker_target``/``_dispatcher_task`` 五个全局状态**物理声明在本文
件**：前四个只被本文件的 ``ensure_workers()`` 用 ``global`` 语句重新赋值，
``_dispatcher_task`` 只被本文件的 ``stop()`` 用 ``global`` 语句重新赋值——
``global`` 只重绑定语句所在函数的那个模块自己的命名空间，定义处与「用
``global`` 语句写它」的函数必须同模块。``.dispatch`` 模块（``_queue_job``/
``_dispatch_due_jobs_legacy``/``_dispatch_due_jobs_stage_aware``/
``_durable_dispatcher``/``_start_durable_dispatcher``）需要读、且
``_start_durable_dispatcher()`` 还需要写 ``_dispatcher_task``——它们全部改用
**限定属性访问** ``worker_lifecycle._xxx``（不是 ``from .worker_lifecycle
import _xxx`` 裸名字导入，那样只会拿到导入那一刻的快照，本文件之后的重新
赋值它永远看不到），属性赋值直接落在本文件自己的 ``__dict__``，与本文件内
部的 ``global`` 读写天然是同一份，不产生第二份副本（详见 ``.dispatch`` 模块
docstring）。

``ensure_workers()`` 需要 ``.worker_loop._worker_loop``；``_stale_lease_sweeper()``
需要 ``.job_recovery`` 的恢复函数，而 ``.job_recovery.recover_and_start`` 反过来
需要 ``.dispatch`` 与本文件各自的名字——真正的双向依赖，惰性导入打在
``.job_recovery`` 一侧，本文件保持顶层导入（与 ``.enqueue``/``.run_job`` 的既
有惰性导入同一做法）。本文件不需要 ``.dispatch`` 任何名字（``stop()`` 自己
直接管 ``_dispatcher_task`` 的取消），因此不对 ``.dispatch`` 做顶层或惰性
import，避免制造一条本不存在的双向依赖。
"""

from __future__ import annotations

import asyncio

from app.db import get_conn, now, rows_to_dicts

from .common import (
    _poll_queue,
    _poll_workers,
    _queue,
    _retry_tasks,
    _video_ready_queue,
    _video_ready_workers,
    _worker_retire_events,
    _workers,
)
from .job_recovery import _recover_one_media_job, reconcile_stalled_video_jobs
from .worker_loop import _worker_loop


def _drain_memory_queue(queue: asyncio.Queue[str]) -> None:
    """Drop startup duplicates; every durable row is rediscovered immediately."""
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        else:
            queue.task_done()


# 这五个名字必须放在真正用 global 语句重新赋值它们的模块里（ensure_workers()/
# stop() 是唯一的写者）：global 只重绑定当前模块自己的命名空间，放在别处会让
# 写入创建出一份外部模块永远看不到的私有副本，详见 common.py 模块 docstring。
# ``.dispatch`` 对 ``_dispatcher_task`` 的读写走限定属性访问，不是第二个
# ``global`` 写者，见本文件顶部模块 docstring。
_worker_target = 0
_reference_worker_target = 0
_video_ready_worker_target = 0
_poll_worker_target = 0
_dispatcher_task: asyncio.Task | None = None

_SWEEPER_INTERVAL_SECONDS = 60.0
_sweeper_task: asyncio.Task | None = None


async def _stale_lease_sweeper(interval_seconds: float = _SWEEPER_INTERVAL_SECONDS) -> None:
    """周期性回收卡死的媒体 job 的过期 lease。

    worker 进程被 kill -9、容器 OOM、协程异常退出等情况会让 job 卡在
    status='running' 且 lease_expires_at<now；recoverable_jobs() 只在启动时扫一次，
    启动后过期的 lease 不会被自动回收。本协程每 interval_seconds 秒扫一次，
    把过期 lease 的 job 复位回 queued，交给持久调度器在下一轮重新发现。

    幂等：多次扫到同一 job 时，第二次 CAS 会因 status 已是 'queued' 而 rowcount=0，
    不会重复恢复。"""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                conn = get_conn()
                stamp = now()
                rows = rows_to_dicts(conn.execute(
                    """SELECT id, run_id, step_run_id FROM jobs
                       WHERE status='running'
                         AND lease_expires_at IS NOT NULL
                         AND lease_expires_at < ?
                         AND cancellation_requested=0
                         AND abandoned=0""",
                    (stamp,),
                ))
                resumed = 0
                for r in rows:
                    if _recover_one_media_job(
                        conn, r["id"], r["run_id"], r["step_run_id"],
                        "lease 过期，自动回收并重新入队",
                    ):
                        resumed += 1
                conn.commit()
                reconcile_stalled_video_jobs()
            except Exception:  # noqa: BLE001 周期任务不能死
                pass
    except asyncio.CancelledError:
        return


def start_stale_lease_sweeper(interval_seconds: float = _SWEEPER_INTERVAL_SECONDS) -> None:
    """启动周期 lease 回收协程；多次调用幂等（已有任务在跑则不重启）。
    覆盖 worker 崩溃/OOM 等非服务重启场景下的中断恢复需求。"""
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _sweeper_task = loop.create_task(_stale_lease_sweeper(interval_seconds))
    _retry_tasks.add(_sweeper_task)
    _sweeper_task.add_done_callback(_retry_tasks.discard)


def ensure_workers(n: int | None = None) -> None:
    """分别维护参考图 / 视频提交 / 轮询三通道 worker。

    ``n`` 若给出，覆盖参考图通道目标；视频提交与 poll 始终读通道配置，
    修复「热更新只跟 video_submit、启动却取 max」的不一致。
    """
    global _worker_target, _reference_worker_target, _video_ready_worker_target, _poll_worker_target
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.concurrency import channel_limit

    ref_n = max(0, int(n if n is not None else channel_limit(media_stages.RESOURCE_REFERENCE)))
    video_n = max(0, int(channel_limit(media_stages.RESOURCE_VIDEO_SUBMIT)))
    poll_n = max(0, int(channel_limit(media_stages.RESOURCE_VIDEO_POLL)))

    _reference_worker_target = ref_n
    _worker_target = ref_n  # 兼容旧字段：代表参考图 worker
    _video_ready_worker_target = video_n
    _poll_worker_target = poll_n

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def _discard_worker(pool: list[asyncio.Task], task: asyncio.Task) -> None:
        _worker_retire_events.pop(task, None)
        try:
            pool.remove(task)
        except ValueError:
            pass

    def _resize(pool: list[asyncio.Task], target: int, prefix: str, queue: asyncio.Queue[str]) -> None:
        for task in tuple(pool):
            if task.done():
                _discard_worker(pool, task)
        accepting = [
            task for task in pool
            if not _worker_retire_events[task].is_set()
        ]
        while len(accepting) < target:
            used_names = {task.get_name() for task in pool}
            index = 0
            while f"{prefix}{index}" in used_names:
                index += 1
            name = f"{prefix}{index}"
            retirement = asyncio.Event()
            task = loop.create_task(
                _worker_loop(name, queue, retirement),
                name=name,
            )
            _worker_retire_events[task] = retirement
            task.add_done_callback(
                lambda done, worker_pool=pool: _discard_worker(worker_pool, done)
            )
            pool.append(task)
            accepting.append(task)
        for task in reversed(accepting[target:]):
            _worker_retire_events[task].set()

    _resize(_workers, ref_n, "ref", _queue)
    _resize(_video_ready_workers, video_n, "vr", _video_ready_queue)
    _resize(_poll_workers, poll_n, "poll", _poll_queue)


async def stop() -> None:
    """优雅停机：取消常驻 worker 循环。否则 uvicorn --reload/退出时会卡在
    'Waiting for connections to close'——常驻 while-True 任务不退出，停机就挂起。"""
    global _sweeper_task, _dispatcher_task, _worker_target, _poll_worker_target
    global _reference_worker_target, _video_ready_worker_target
    try:
        from app.media_pipeline.bootstrap import stop_media_pipeline
        await stop_media_pipeline()
    except Exception:  # noqa: BLE001
        pass
    if _sweeper_task is not None:
        _sweeper_task.cancel()
    if _dispatcher_task is not None:
        _dispatcher_task.cancel()
        try:
            await _dispatcher_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _dispatcher_task = None
    for t in _retry_tasks:
        t.cancel()
    if _retry_tasks:
        await asyncio.gather(*tuple(_retry_tasks), return_exceptions=True)
    _retry_tasks.clear()
    for t in (*_workers, *_video_ready_workers, *_poll_workers):
        retirement = _worker_retire_events.get(t)
        if retirement is not None:
            retirement.set()
        t.cancel()
    for t in (*_workers, *_video_ready_workers, *_poll_workers):
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _workers.clear()
    _video_ready_workers.clear()
    _poll_workers.clear()
    _worker_retire_events.clear()
    _worker_target = 0
    _reference_worker_target = 0
    _video_ready_worker_target = 0
    _poll_worker_target = 0
    _drain_memory_queue(_queue)
    _drain_memory_queue(_video_ready_queue)
    _drain_memory_queue(_poll_queue)

__all__ = [name for name in globals() if not name.startswith("__")]
