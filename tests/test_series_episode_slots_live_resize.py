"""设置台调大每任务并行集数时，正在等槽位的集立刻补位，不必等本任务下一集跑完（2026-09-05 实测 3→5 后 20 分钟没补位）。"""
from __future__ import annotations

import asyncio

import pytest

from app.domain.series_ops import concurrency


@pytest.mark.asyncio
async def test_waiter_picks_up_a_raised_limit_without_a_release(monkeypatch) -> None:
    limit = {"n": 1}
    monkeypatch.setattr(concurrency, "episode_concurrency", lambda: limit["n"])
    monkeypatch.setattr(concurrency, "SLOT_RECHECK_S", 0.05)
    slots = concurrency.EpisodeSlots()
    await slots.acquire("task")
    waiter = asyncio.create_task(slots.acquire("task"))
    await asyncio.sleep(0.15)
    assert not waiter.done() and slots.running("task") == 1
    limit["n"] = 2  # 调大，没有任何 release
    await asyncio.wait_for(waiter, timeout=1.0)
    assert slots.running("task") == 2


@pytest.mark.asyncio
async def test_lowered_limit_stops_new_admissions_until_releases_catch_up(monkeypatch) -> None:
    limit = {"n": 2}
    monkeypatch.setattr(concurrency, "episode_concurrency", lambda: limit["n"])
    monkeypatch.setattr(concurrency, "SLOT_RECHECK_S", 0.05)
    slots = concurrency.EpisodeSlots()
    await slots.acquire("task")
    await slots.acquire("task")
    limit["n"] = 1
    waiter = asyncio.create_task(slots.acquire("task"))
    await asyncio.sleep(0.15)
    assert not waiter.done()
    await slots.release("task")  # 2 → 1，仍达上限
    await asyncio.sleep(0.15)
    assert not waiter.done()
    await slots.release("task")  # 1 → 0，放行
    await asyncio.wait_for(waiter, timeout=1.0)
    assert slots.running("task") == 1
