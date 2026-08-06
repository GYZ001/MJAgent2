"""统一管理 API 后台任务的生命周期。

所有可由用户发起的后台任务都在这里登记，确保取消、项目删除和服务退出时
能够找到真实 asyncio.Task，而不是只修改数据库状态。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Coroutine, Any


@dataclass(slots=True)
class TaskRecord:
    kind: str
    key: str
    task: asyncio.Task
    project_id: str | None = None


_records: dict[tuple[str, str], TaskRecord] = {}
_shutdown_in_progress = False


def shutdown_in_progress() -> bool:
    """Return whether cancellation comes from process shutdown, not a user action."""
    return _shutdown_in_progress


def get(kind: str, key: str) -> asyncio.Task | None:
    record = _records.get((kind, key))
    return record.task if record else None


def active(kind: str, key: str) -> bool:
    task = get(kind, key)
    return bool(task and not task.done())


def register(kind: str, key: str, task: asyncio.Task, *, project_id: str | None = None) -> asyncio.Task:
    token = (kind, key)
    previous = _records.get(token)
    if previous and not previous.task.done() and previous.task is not task:
        raise RuntimeError(f"后台任务已在运行：{kind}/{key}")
    _records[token] = TaskRecord(kind=kind, key=key, task=task, project_id=project_id)

    def _forget(done: asyncio.Task) -> None:
        current = _records.get(token)
        if current and current.task is done:
            _records.pop(token, None)

    task.add_done_callback(_forget)
    return task


def spawn(kind: str, key: str, coro: Coroutine[Any, Any, Any], *,
          project_id: str | None = None) -> asyncio.Task:
    if active(kind, key):
        coro.close()
        raise RuntimeError(f"后台任务已在运行：{kind}/{key}")
    task: asyncio.Task | None = None
    try:
        task = asyncio.get_running_loop().create_task(coro)
        return register(kind, key, task, project_id=project_id)
    except BaseException:
        if task is None:
            coro.close()
        else:
            task.cancel()
        raise


def cancel(kind: str, key: str) -> bool:
    record = _records.pop((kind, key), None)
    if not record or record.task.done():
        return False
    record.task.get_loop().call_soon_threadsafe(record.task.cancel)
    return True


async def cancel_and_wait(kind: str, key: str) -> bool:
    """Cancel one task from an async route and wait until it can no longer write state."""
    record = _records.get((kind, key))
    if not record or record.task.done():
        return False
    record.task.cancel()
    await asyncio.gather(record.task, return_exceptions=True)
    return True


async def cancel_project(project_id: str) -> int:
    records = [r for r in list(_records.values()) if r.project_id == project_id and not r.task.done()]
    for record in records:
        _records.pop((record.kind, record.key), None)
        record.task.cancel()
    if records:
        await asyncio.gather(*(r.task for r in records), return_exceptions=True)
    return len(records)


async def stop_all() -> None:
    global _shutdown_in_progress
    _shutdown_in_progress = True
    try:
        records = [r for r in list(_records.values()) if not r.task.done()]
        _records.clear()
        for record in records:
            record.task.cancel()
        if records:
            await asyncio.gather(*(r.task for r in records), return_exceptions=True)
    finally:
        # 测试或嵌入式 lifespan 可能在同一进程内再次启动。
        _shutdown_in_progress = False
