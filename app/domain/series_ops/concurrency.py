"""连播台并行度：一个数字管两件事——同一项目同时跑几个任务、同时生成几集。

用户 2026-09-04 看到 10 集一组的任务里各集仍是串行（「连播台没有并发生成呀」）：
任务级并行对「一个大任务」毫无帮助。所以并行槽位按**项目内的集**计数，跨任务共享：
不管这些集属于一个任务还是三个任务，同一时刻在跑的集数都不超过
``settings.series_queue_concurrency``（缺省 3，夹 1..8）。

槽位不用 ``asyncio.Semaphore``：并行数可以在设置台随时改，Semaphore 的容量是建好就
定死的。这里用计数 + Condition，每次醒来重新读设置，调小立刻生效（在跑的集跑完
即不再补位），调大也立刻放行。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.db import get_setting

SETTING_KEY = "series_queue_concurrency"
DEFAULT_QUEUE_CONCURRENCY = 3
MAX_QUEUE_CONCURRENCY = 8


def queue_concurrency() -> int:
    """同一项目同时生成的集数（也是同时跑的任务数上限）：缺省 3，夹在 1..8。"""
    try:
        value = int(float(str(get_setting(SETTING_KEY) or "").strip() or DEFAULT_QUEUE_CONCURRENCY))
    except ValueError:
        value = DEFAULT_QUEUE_CONCURRENCY
    return max(1, min(MAX_QUEUE_CONCURRENCY, value))


class EpisodeSlots:
    """项目级「同时在跑的集」槽位。"""

    def __init__(self) -> None:
        self._running: dict[str, int] = {}
        self._cond: dict[str, asyncio.Condition] = {}

    def _condition(self, project_id: str) -> asyncio.Condition:
        cond = self._cond.get(project_id)
        if cond is None:
            cond = self._cond[project_id] = asyncio.Condition()
        return cond

    def running(self, project_id: str) -> int:
        return self._running.get(project_id, 0)

    async def acquire(self, project_id: str) -> None:
        cond = self._condition(project_id)
        async with cond:
            await cond.wait_for(lambda: self.running(project_id) < queue_concurrency())
            self._running[project_id] = self.running(project_id) + 1

    async def release(self, project_id: str) -> None:
        cond = self._condition(project_id)
        async with cond:
            self._running[project_id] = max(0, self.running(project_id) - 1)
            cond.notify_all()

    @asynccontextmanager
    async def hold(self, project_id: str) -> AsyncIterator[None]:
        await self.acquire(project_id)
        try:
            yield
        finally:
            await self.release(project_id)


#: 进程内唯一实例；测试通过 monkeypatch 替换本模块的 ``queue_concurrency`` 调并行数。
episode_slots = EpisodeSlots()
