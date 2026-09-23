"""连播台成片步骤：同一集在进程内串行，后到者不重复渲染。

补跑任务与主任务同时覆盖一集时（第 24 集实测），两个任务各自等到该集空闲后
几乎同时进入成片台，各自渲染一遍、再争同一个发布租约。锁内串行后，后到者
拿到锁时先看判据：前者已发布就直接返回。
"""
from __future__ import annotations

import asyncio
import threading
import time

from app.domain.series_ops import stages as series_stages
from tests.conftest import patch_worker_everywhere


def test_concurrent_final_runs_for_same_episode_render_once(monkeypatch) -> None:
    calls: list[str] = []
    published = {"done": False}

    def fake_concat(episode_id: str) -> dict:
        calls.append(episode_id)
        time.sleep(0.05)
        published["done"] = True
        return {}

    patch_worker_everywhere(monkeypatch, "concatenate_episode", fake_concat)
    monkeypatch.setattr(series_stages, "get_conn", lambda: None)
    monkeypatch.setattr(series_stages, "final_complete", lambda conn, episode_id: published["done"])

    async def run() -> None:
        await asyncio.gather(series_stages._run_final("ep_lock_once"), series_stages._run_final("ep_lock_once"))

    asyncio.run(run())
    assert calls == ["ep_lock_once"]


def test_final_runs_for_different_episodes_do_not_serialize(monkeypatch) -> None:
    """判据与墙钟无关：``_run_final`` 把 ``concatenate_episode`` 扔进
    ``asyncio.to_thread``（真实 OS 线程池），不同集用不同的 ``asyncio.Lock``
    实例，所以两集互不串行时，两次 ``fake_concat`` 必然会同时身处彼此的临界
    区内。用 ``threading.Barrier(2)`` 直接对这件事做二元判定而不是猜一个耗时
    阈值：两边都真正到场，屏障立刻放行（多慢的机器、多重的负载都只影响放行
    早晚，不影响判定结果）；如果退化成互相串行，先到的一侧最多等 5 秒等不到
    对方，屏障超时打破，抛出 ``BrokenBarrierError``，测试确定性地红。"""
    calls: list[str] = []
    entered = threading.Barrier(2, timeout=5)

    def fake_concat(episode_id: str) -> dict:
        calls.append(episode_id)
        try:
            entered.wait()
        except threading.BrokenBarrierError:
            raise AssertionError(
                f"{episode_id} 的 concatenate_episode 在 5 秒内未等到另一集同时"
                "进入临界区——两集被互相串行化了，不同集不该互相串行"
            ) from None
        return {}

    patch_worker_everywhere(monkeypatch, "concatenate_episode", fake_concat)
    monkeypatch.setattr(series_stages, "get_conn", lambda: None)
    monkeypatch.setattr(series_stages, "final_complete", lambda conn, episode_id: False)

    async def run() -> None:
        await asyncio.gather(series_stages._run_final("ep_lock_a"), series_stages._run_final("ep_lock_b"))

    asyncio.run(run())
    assert sorted(calls) == ["ep_lock_a", "ep_lock_b"]
