"""连播台并行度：两个数字——同一项目同时跑几个任务、每个任务同时生成几集。

- ``settings.series_queue_concurrency``：同一项目同时在跑的连播任务数（缺省 3，夹 1..8）；
- ``settings.series_episode_concurrency``：每个任务内部同时生成的集数（缺省 3，夹 1..8）。

2026-09-04 用户先后两次拍板：任务内部各集要并行（10 集一组的任务里各集串行「没有并发」）；
任务之间也要并行、不等前一个任务（槽位曾按项目共享，一个 10 集任务把 3 个槽位占满，后面的
任务实际上在排队）。所以槽位改成**按任务计**：每个任务各自最多 N 集在跑，任务数另受任务级上限。
两级相乘就是项目内同时在跑的集数上限（缺省 3×3）；供应商侧的并发配额仍各自约束，不在这里重复。

槽位不用 ``asyncio.Semaphore``：并行数可以在设置台随时改，Semaphore 的容量是建好就定死的。
这里用计数 + Condition，每次醒来重新读设置，调小立刻生效（在跑的集跑完即不再补位），调大也立刻放行。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.db import get_setting

TASK_SETTING_KEY = "series_queue_concurrency"
EPISODE_SETTING_KEY = "series_episode_concurrency"
DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY = 8


def _read_concurrency(key: str) -> int:
    try:
        value = int(float(str(get_setting(key) or "").strip() or DEFAULT_CONCURRENCY))
    except ValueError:
        value = DEFAULT_CONCURRENCY
    return max(1, min(MAX_CONCURRENCY, value))


def queue_concurrency() -> int:
    """同一项目同时在跑的连播任务数：缺省 3，夹在 1..8。"""
    return _read_concurrency(TASK_SETTING_KEY)


def episode_concurrency() -> int:
    """每个连播任务内部同时生成的集数：缺省 3，夹在 1..8。"""
    return _read_concurrency(EPISODE_SETTING_KEY)


class EpisodeSlots:
    """按作用域（任务 id）计数的「同时在跑的集」槽位。"""

    def __init__(self) -> None:
        self._running: dict[str, int] = {}
        self._cond: dict[str, asyncio.Condition] = {}

    def _condition(self, scope: str) -> asyncio.Condition:
        cond = self._cond.get(scope)
        if cond is None:
            cond = self._cond[scope] = asyncio.Condition()
        return cond

    def running(self, scope: str) -> int:
        return self._running.get(scope, 0)

    async def acquire(self, scope: str) -> None:
        cond = self._condition(scope)
        async with cond:
            await cond.wait_for(lambda: self.running(scope) < episode_concurrency())
            self._running[scope] = self.running(scope) + 1

    async def release(self, scope: str) -> None:
        cond = self._condition(scope)
        async with cond:
            self._running[scope] = max(0, self.running(scope) - 1)
            cond.notify_all()
            if self._running[scope] == 0:
                # 任务跑完就把作用域清掉，不让 task_id 键无限累积
                self._running.pop(scope, None)
                self._cond.pop(scope, None)

    @asynccontextmanager
    async def hold(self, scope: str) -> AsyncIterator[None]:
        await self.acquire(scope)
        try:
            yield
        finally:
            await self.release(scope)


#: 进程内唯一实例；测试通过 monkeypatch 替换本模块的 ``episode_concurrency`` 调并行数。
episode_slots = EpisodeSlots()
